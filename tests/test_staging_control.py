"""Tests for the staging-control service.

This is the one surface a ticket's own untrusted text can reach (once opted
in), so the tests here are adversarial by default: every caller-supplied
value is checked against being rejected, not just the happy path. No test in
this file lets a real ``subprocess.run`` fire — ``actions._run`` is monkeypatched
in every test that exercises deploy/restart, and the assertion that matters
most is the negative one: an invalid app/token must produce **zero** calls.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bloy_dev_agent.staging_control import actions, apps, tokens

# ---------------------------------------------------------------------------
# tokens.py — already covered lightly inline above; deeper cases here
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def staging_db(monkeypatch, tmp_path):
    """Bind every DB-touching module to a throwaway database for every test."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from bloy_dev_agent import db, models

    engine = create_engine(f"sqlite:///{tmp_path / 'staging.sqlite3'}")
    models.Base.metadata.create_all(engine, checkfirst=True)
    session_local = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", session_local)
    monkeypatch.setattr(tokens, "SessionLocal", session_local)
    return session_local


def test_an_unknown_token_resolves_to_nothing():
    assert tokens.resolve("something-nobody-minted") is None


def test_an_expired_token_resolves_to_nothing():
    raw = tokens.mint(
        "run-x", issue_key="BLS-1", worktrees={"api": Path("/tmp/wt")}, ttl_minutes=-100
    )
    assert tokens.resolve(raw) is None


def test_a_token_only_grants_its_own_runs_worktrees():
    raw_a = tokens.mint(
        "run-a", issue_key="BLS-1",
        worktrees={"shopify-app-loyalty-api": Path("/tmp/a")}, ttl_minutes=30,
    )
    raw_b = tokens.mint(
        "run-b", issue_key="BLS-2",
        worktrees={"shopify-app-loyalty-api": Path("/tmp/b")}, ttl_minutes=30,
    )
    grant_a = tokens.resolve(raw_a)
    grant_b = tokens.resolve(raw_b)
    assert grant_a.worktrees["shopify-app-loyalty-api"] == Path("/tmp/a")
    assert grant_b.worktrees["shopify-app-loyalty-api"] == Path("/tmp/b")


def test_the_deploy_ceiling_is_enforced(monkeypatch):
    # Isolate the count ceiling from the min-interval check below — both are
    # real constraints, but this test is only about the former.
    monkeypatch.setattr(tokens, "MIN_SECONDS_BETWEEN_DEPLOYS", 0)
    tokens.mint("run-cap", issue_key="BLS-1", worktrees={}, ttl_minutes=30)
    for _ in range(tokens.MAX_DEPLOYS_PER_TOKEN):
        assert tokens.check_rate_limit("run-cap") == ""
        tokens.record_deploy("run-cap")
    reason = tokens.check_rate_limit("run-cap")
    assert "giới hạn" in reason


def test_deploys_are_throttled_by_minimum_interval():
    tokens.mint("run-throttle", issue_key="BLS-1", worktrees={}, ttl_minutes=30)
    tokens.record_deploy("run-throttle")

    reason = tokens.check_rate_limit("run-throttle")

    assert "chờ thêm" in reason


def test_a_revoked_token_cannot_deploy_even_before_expiry():
    raw = tokens.mint("run-r", issue_key="BLS-1", worktrees={}, ttl_minutes=90)
    tokens.revoke("run-r")
    assert tokens.resolve(raw) is None
    assert "không còn hiệu lực" in tokens.check_rate_limit("run-r")


def test_purge_expired_removes_only_expired_rows():
    tokens.mint("run-live", issue_key="BLS-1", worktrees={}, ttl_minutes=90)
    tokens.mint("run-dead", issue_key="BLS-2", worktrees={}, ttl_minutes=-100)

    removed = tokens.purge_expired()

    assert removed == 1
    assert tokens.check_rate_limit("run-live") == ""


# ---------------------------------------------------------------------------
# actions.py — no subprocess ever fires for a bad input
# ---------------------------------------------------------------------------


def test_deploy_refuses_a_worktree_the_grant_does_not_have(monkeypatch):
    calls = []
    monkeypatch.setattr(actions, "_run", lambda *a, **k: calls.append(a) or (True, ""))
    grant = tokens.StagingGrant(run_id="run-1", issue_key="BLS-1", worktrees={})

    result = actions.deploy(apps.STAGING_APPS["api"], grant)

    assert result.ok is False
    assert calls == []


def test_deploy_refuses_a_worktree_that_does_not_exist_on_disk(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(actions, "_run", lambda *a, **k: calls.append(a) or (True, ""))
    grant = tokens.StagingGrant(
        run_id="run-1",
        issue_key="BLS-1",
        worktrees={"shopify-app-loyalty-api": tmp_path / "does-not-exist"},
    )

    result = actions.deploy(apps.STAGING_APPS["api"], grant)

    assert result.ok is False
    assert "không tồn tại" in result.detail
    assert calls == []


def test_a_concurrent_deploy_is_refused_not_queued(monkeypatch, tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    grant = tokens.StagingGrant(
        run_id="run-1", issue_key="BLS-1", worktrees={"shopify-app-loyalty-api": worktree}
    )
    monkeypatch.setattr(actions, "_rsync_worktree_to_checkout", lambda *a, **k: (True, ""))
    monkeypatch.setattr(actions, "_run", lambda *a, **k: (True, ""))
    monkeypatch.setattr(actions, "_wait_healthy", lambda *a, **k: True)

    actions._DEPLOY_LOCK.acquire()
    try:
        result = actions.deploy(apps.STAGING_APPS["api"], grant)
        assert result.ok is False
        assert "đang chạy" in result.detail
    finally:
        actions._DEPLOY_LOCK.release()


def test_a_successful_deploy_runs_rsync_then_restart_then_health_check(monkeypatch, tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    grant = tokens.StagingGrant(
        run_id="run-1", issue_key="BLS-1", worktrees={"shopify-app-loyalty-api": worktree}
    )
    order = []
    monkeypatch.setattr(
        actions, "_rsync_worktree_to_checkout",
        lambda *a, **k: (order.append("rsync") or (True, "")),
    )
    monkeypatch.setattr(
        actions, "_run", lambda argv, **k: (order.append(("run", argv)) or (True, ""))
    )
    monkeypatch.setattr(
        actions, "_wait_healthy", lambda *a, **k: (order.append("health") or True)
    )

    result = actions.deploy(apps.STAGING_APPS["api"], grant)

    assert result.ok is True
    assert order[0] == "rsync"
    assert order[-1] == "health"
    # api's own restart argv, not something assembled from caller input
    assert ("run", list(apps.STAGING_APPS["api"].restart)) in order


def test_a_failed_health_check_is_reported_as_failure_not_success(monkeypatch, tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    grant = tokens.StagingGrant(
        run_id="run-1", issue_key="BLS-1", worktrees={"shopify-app-loyalty-api": worktree}
    )
    monkeypatch.setattr(actions, "_rsync_worktree_to_checkout", lambda *a, **k: (True, ""))
    monkeypatch.setattr(actions, "_run", lambda *a, **k: (True, ""))
    monkeypatch.setattr(actions, "_wait_healthy", lambda *a, **k: False)

    result = actions.deploy(apps.STAGING_APPS["api"], grant)

    assert result.ok is False
    assert "health check không pass" in result.detail


def test_a_build_step_runs_with_ci_true(monkeypatch, tmp_path):
    """Live bug: `pnpm --filter bloy-extensions run build-bloy`'s own `pnpm
    install` step refuses to remove/reinstall node_modules without a TTY
    unless CI=true is set — the first real cms deploy failed on exactly this,
    reported as a build failure with no other symptom."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    grant = tokens.StagingGrant(
        run_id="run-1", issue_key="BLS-1", worktrees={"shopify-app-loyalty-cms": worktree}
    )
    seen: list[tuple[list[str], dict | None]] = []
    monkeypatch.setattr(actions, "_rsync_worktree_to_checkout", lambda *a, **k: (True, ""))
    monkeypatch.setattr(actions, "_wait_healthy", lambda *a, **k: True)

    def fake_run(argv, *, cwd, timeout, extra_env=None):
        seen.append((list(argv), extra_env))
        return True, ""

    monkeypatch.setattr(actions, "_run", fake_run)

    result = actions.deploy(apps.STAGING_APPS["cms"], grant)

    assert result.ok is True
    build_argvs = [list(argv) for argv in apps.STAGING_APPS["cms"].build]
    assert build_argvs, "this test only means something if cms has a build step"
    build_calls = [env for argv, env in seen if argv in build_argvs]
    assert len(build_calls) == len(build_argvs)
    assert all(env == {"CI": "true"} for env in build_calls)
    # the restart step is not a `pnpm install`; it must not be forced into CI
    # mode just because it happens to share the same _run() call.
    restart_calls = [env for argv, env in seen if argv == list(apps.STAGING_APPS["cms"].restart)]
    assert restart_calls == [None]


def test_every_subprocess_call_is_argv_never_a_shell_string():
    """A regression here would be a shell-injection hole, not a style nit.

    Checked as actual code, not just "the string doesn't appear" — this
    module's own docstrings mention the forbidden pattern by name, so a naive
    substring search would pass even after someone adds a real one, or fail
    on prose that never touched a subprocess call.
    """
    import ast

    tree = ast.parse(Path(actions.__file__).read_text(encoding="utf-8"))
    offenders = [
        kw
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "shell"
    ]
    assert offenders == []


def test_pm2_ipc_env_vars_never_reach_a_build_subprocess(monkeypatch, tmp_path):
    """Live bug: PM2 injects NODE_CHANNEL_FD/NODE_CHANNEL_SERIALIZATION_MODE
    into this service's own environment (it manages this Python process the
    same way it manages a Node one). Left in a child's env, a `node` process
    spawned by pnpm/webpack reads NODE_CHANNEL_FD at startup, tries to use
    that fd as its own IPC channel, and aborts (SIGABRT) — reproduced live:
    the extensions build failed on every real deploy through the PM2-managed
    service, and succeeded every time the exact same argv/cwd/env was run by
    hand, with only this pair of env vars differing between the two."""
    monkeypatch.setenv("NODE_CHANNEL_FD", "3")
    monkeypatch.setenv("NODE_CHANNEL_SERIALIZATION_MODE", "json")
    captured_env = {}

    class FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, cwd, capture_output, text, timeout, env):
        captured_env.update(env or {})
        return FakeResult()

    monkeypatch.setattr(actions.subprocess, "run", fake_run)

    actions._run(["true"], cwd=tmp_path, timeout=5)

    assert "NODE_CHANNEL_FD" not in captured_env
    assert "NODE_CHANNEL_SERIALIZATION_MODE" not in captured_env


def test_pm2_ipc_env_vars_are_stripped_even_with_extra_env(monkeypatch, tmp_path):
    monkeypatch.setenv("NODE_CHANNEL_FD", "3")
    captured_env = {}

    class FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, cwd, capture_output, text, timeout, env):
        captured_env.update(env or {})
        return FakeResult()

    monkeypatch.setattr(actions.subprocess, "run", fake_run)

    actions._run(["true"], cwd=tmp_path, timeout=5, extra_env={"CI": "true"})

    assert "NODE_CHANNEL_FD" not in captured_env
    assert captured_env.get("CI") == "true"


# ---------------------------------------------------------------------------
# apps.py — the fixed table itself
# ---------------------------------------------------------------------------


def test_rsync_excludes_cover_every_secret_and_stack_identifying_path():
    for must_exclude in (".env", "web/.env", "shopify.app.toml", "node_modules", ".git"):
        assert must_exclude in apps.RSYNC_EXCLUDES


def test_rsync_excludes_the_generated_rsa_keypair():
    """A real deploy once rsync'd a worktree with no keys of its own onto the
    staging checkout and --delete removed the keypair generateRSAKeyPair had
    already created there — the API then failed to boot at all.
    """
    assert "src/keys" in apps.RSYNC_EXCLUDES


def test_rsync_excludes_every_lockfile():
    """Live bug: cms gitignores pnpm-lock.yaml, so a fresh worktree never has
    one — the first real cms deploy's --delete wiped the staging checkout's
    own copy, and the very next build failed to resolve a dependency because
    node_modules no longer matched the (now-missing) lockfile it was
    installed against."""
    for lockfile in ("pnpm-lock.yaml", "package-lock.json", "yarn.lock"):
        assert lockfile in apps.RSYNC_EXCLUDES


def test_rsync_excludes_pnpm_workspace_yaml():
    """Live bug: the tracked pnpm-workspace.yaml has no `allowBuilds` section,
    only the staging checkout's locally-approved copy does — an un-excluded
    rsync silently re-armed pnpm's interactive approve-builds gate on the
    very next deploy, breaking a build that changed no dependency at all."""
    assert "pnpm-workspace.yaml" in apps.RSYNC_EXCLUDES


def test_rsync_excludes_the_empty_theme_locales_dir():
    """git never tracks empty directories, so a plain worktree never has
    extensions/theme-app-extension/locales — its absence crashed
    `shopify app deploy` outright (ENOENT: scandir), not just a warning."""
    assert "extensions/theme-app-extension/locales" in apps.RSYNC_EXCLUDES


def test_staging_checkout_paths_are_never_the_personal_dev_checkout():
    for app in apps.STAGING_APPS.values():
        assert "bloy-staging" in str(app.checkout)
        assert str(app.checkout) != str(Path.home() / "BLOY" / app.repo)


# ---------------------------------------------------------------------------
# service.py — the HTTP surface: auth required everywhere, fixed action set
# ---------------------------------------------------------------------------


@pytest.fixture()
def http_client(monkeypatch):
    import importlib

    from bloy_dev_agent.staging_control import service as service_module

    importlib.reload(service_module)
    return TestClient(service_module.create_app())


def test_deploy_without_a_token_is_rejected(http_client):
    response = http_client.post("/v1/deploy", json={"app": "api"})
    assert response.status_code == 401


def test_deploy_with_a_garbage_token_is_rejected(http_client):
    response = http_client.post(
        "/v1/deploy", json={"app": "api"}, headers={"Authorization": "Bearer nope"}
    )
    assert response.status_code == 401


def test_status_also_requires_a_token():
    """Read-only routes are not an exception — nothing here is unauthenticated."""
    import importlib

    from bloy_dev_agent.staging_control import service as service_module

    importlib.reload(service_module)
    client = TestClient(service_module.create_app())

    assert client.get("/v1/status").status_code == 401
    assert client.get("/v1/logs?app=api").status_code == 401


def test_an_unknown_app_key_is_rejected_before_touching_a_grant(http_client, tmp_path):
    raw = tokens.mint(
        "run-1", issue_key="BLS-1", worktrees={"shopify-app-loyalty-api": tmp_path}, ttl_minutes=30
    )
    response = http_client.post(
        "/v1/deploy",
        json={"app": "../../etc/passwd"},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert response.status_code == 400


def test_deploy_with_a_valid_token_calls_actions_deploy(monkeypatch, http_client, tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    raw = tokens.mint(
        "run-1", issue_key="BLS-1",
        worktrees={"shopify-app-loyalty-api": worktree}, ttl_minutes=30,
    )
    seen = {}

    def fake_deploy(app, grant):
        seen["app"] = app.key
        seen["run_id"] = grant.run_id
        return actions.ActionResult(True, "ok", seconds=1.2)

    from bloy_dev_agent.staging_control import service as service_module

    monkeypatch.setattr(service_module.actions, "deploy", fake_deploy)

    response = http_client.post(
        "/v1/deploy", json={"app": "api"}, headers={"Authorization": f"Bearer {raw}"}
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert seen == {"app": "api", "run_id": "run-1"}


def test_a_rate_limited_deploy_returns_429(monkeypatch, http_client, tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    raw = tokens.mint(
        "run-1", issue_key="BLS-1",
        worktrees={"shopify-app-loyalty-api": worktree}, ttl_minutes=30,
    )
    for _ in range(tokens.MAX_DEPLOYS_PER_TOKEN):
        tokens.record_deploy("run-1")

    response = http_client.post(
        "/v1/deploy", json={"app": "api"}, headers={"Authorization": f"Bearer {raw}"}
    )

    assert response.status_code == 429


def test_restart_all_targets_every_app(monkeypatch, http_client, tmp_path):
    raw = tokens.mint("run-1", issue_key="BLS-1", worktrees={}, ttl_minutes=30)
    seen_keys = []

    def fake_restart(app):
        seen_keys.append(app.key)
        return actions.ActionResult(True, "ok")

    from bloy_dev_agent.staging_control import service as service_module

    monkeypatch.setattr(service_module.actions, "restart", fake_restart)

    response = http_client.post(
        "/v1/restart", json={"app": "all"}, headers={"Authorization": f"Bearer {raw}"}
    )

    assert response.status_code == 200
    assert set(seen_keys) == {"api", "cms"}


def test_logs_caps_the_requested_line_count(monkeypatch, http_client):
    raw = tokens.mint("run-1", issue_key="BLS-1", worktrees={}, ttl_minutes=30)

    from bloy_dev_agent.staging_control import service as service_module

    monkeypatch.setattr(
        service_module.actions, "logs", lambda app, lines: [f"n={lines}"]
    )

    response = http_client.get(
        "/v1/logs?app=api&lines=999999",
        headers={"Authorization": f"Bearer {raw}"},
    )

    assert response.status_code == 200
    # The route itself does not cap — actions.logs does (see
    # test_logs_actually_caps_at_500_lines below); this only pins that the
    # route passes the query value through rather than silently dropping it.
    assert response.json()["lines"] == ["n=999999"]


def test_logs_actually_caps_at_500_lines(tmp_path, monkeypatch):
    log_file = tmp_path / "bloy-stg-api-out-54.log"
    log_file.write_text("\n".join(f"line {i}" for i in range(1000)), encoding="utf-8")
    # PM2 embeds its own numeric process id in the log filename — not
    # guessable from the process name — so the real path always comes from
    # `pm2 jlist`, mocked here rather than assuming a filename pattern.
    monkeypatch.setattr(
        actions, "_pm2_out_log_paths", lambda: {"bloy-stg-api": str(log_file)}
    )

    result = actions.logs(apps.STAGING_APPS["api"], lines=999999)

    assert len(result) == 500
    assert result[-1].endswith("line 999")


def test_pm2_log_paths_survive_a_version_banner_before_the_json(monkeypatch):
    """Observed for real on this host: a version-mismatch banner printed
    ahead of the JSON on this exact command once broke BAM's own routine
    reconciler. The parser here must skip straight to the array's real start.
    """
    import json as json_module

    banner = ">>>> In-memory PM2 is out-of-date, do:\n>>>> $ pm2 update\n"
    payload = json_module.dumps(
        [{"name": "bloy-stg-api", "pm2_env": {"pm_out_log_path": "/x/api-out-9.log"}}]
    )

    class FakeResult:
        returncode = 0
        stdout = banner + payload

    monkeypatch.setattr(actions.subprocess, "run", lambda *a, **k: FakeResult())

    assert actions._pm2_out_log_paths() == {"bloy-stg-api": "/x/api-out-9.log"}


def test_pm2_log_paths_survive_a_colour_coded_banner(monkeypatch):
    """Found live, not hypothesised: PM2 sometimes colours that same banner
    with ANSI codes, and a colour code's own "\\x1b[" contains a real "["
    character. A bare ``str.find("[")`` matched *that* one instead of the
    array's opening bracket, truncating the string to something that either
    failed to parse as JSON at all, or parsed into nothing — this is the
    actual bug behind an earlier live deploy silently returning zero logs.
    """
    import json as json_module

    banner = (
        "\n\x1b[31m\x1b[1m>>>> In-memory PM2 is out-of-date, do:\x1b[22m\x1b[39m\n"
        "\x1b[31m\x1b[1m>>>> $ pm2 update\x1b[22m\x1b[39m\n"
        "In memory PM2 version: \x1b[34m\x1b[1m7.0.1\x1b[22m\x1b[39m\n"
        "Local PM2 version: \x1b[34m\x1b[1m7.0.3\x1b[22m\x1b[39m\n\n"
    )
    payload = json_module.dumps(
        [{"name": "bloy-stg-api", "pm2_env": {"pm_out_log_path": "/x/api-out-54.log"}}]
    )

    class FakeResult:
        returncode = 0
        stdout = banner + payload

    monkeypatch.setattr(actions.subprocess, "run", lambda *a, **k: FakeResult())

    assert actions._pm2_out_log_paths() == {"bloy-stg-api": "/x/api-out-54.log"}


def test_pm2_log_paths_is_empty_not_raising_when_pm2_is_unreachable(monkeypatch):
    def boom(*a, **k):
        raise OSError("pm2 not found")

    monkeypatch.setattr(actions.subprocess, "run", boom)

    assert actions._pm2_out_log_paths() == {}


def test_logs_skips_a_process_pm2_does_not_know_about(monkeypatch):
    monkeypatch.setattr(actions, "_pm2_out_log_paths", lambda: {})

    result = actions.logs(apps.STAGING_APPS["api"], lines=200)

    assert result == []
