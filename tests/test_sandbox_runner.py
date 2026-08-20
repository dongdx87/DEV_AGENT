"""Tests for the sandbox bridge.

The one behaviour that broke in production: BAM's routine scheduler calls
actions *on its own running event loop*, so a bare ``asyncio.run`` raises
"cannot be called from a running event loop" and the whole implement run is
lost. That case is pinned first.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from bloy_dev_agent.features import sandbox_runner


def test_runs_when_a_loop_is_already_running():
    """The regression: the scheduler's loop must not block the sandbox call."""

    async def caller():
        return sandbox_runner._run_coroutine(_answer)

    async def _answer():
        return "ok"

    assert asyncio.run(caller()) == "ok"


def test_runs_with_no_loop_at_all():
    async def _answer():
        return "ok"

    assert sandbox_runner._run_coroutine(_answer) == "ok"


def test_an_error_inside_the_coroutine_reaches_the_caller():
    """A swallowed exception would look identical to a silent empty run."""

    async def _boom():
        raise ValueError("container refused")

    with pytest.raises(ValueError, match="container refused"):
        sandbox_runner._run_coroutine(_boom)


def test_a_missing_config_is_reported_not_raised(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox_runner, "SANDBOX_CONFIG", tmp_path / "absent.toml")

    result = sandbox_runner.run_in_sandbox(
        "do the thing", tmp_path / "wt" / "issue", worktree_root=tmp_path / "wt"
    )

    assert result.ok is False
    assert "not found" in result.output


def test_execution_text_flattens_message_objects():
    """OpenSandbox logs are message objects; joining them naively raises."""

    class Message:
        def __init__(self, text):
            self.text = text

    class Logs:
        stdout = [Message("hello "), Message("world")]
        stderr = [Message("!")]

    class Execution:
        logs = Logs()

    assert sandbox_runner._text(Execution()) == "hello world!"


def test_analysis_mode_never_gets_the_write_flag():
    """Read-only must stay read-only even though both modes share the runner."""
    assert sandbox_runner.claude_flags(implement=False) == (
        "--allowedTools Read Grep Glob --permission-mode plan"
    )
    assert "--dangerously-skip-permissions" not in sandbox_runner.claude_flags(False)
    assert sandbox_runner.claude_flags(True) == "--dangerously-skip-permissions"


def test_the_prompt_is_never_interpolated_into_the_shell():
    """Issue bodies are untrusted text; they must arrive base64-encoded."""
    hostile = 'title"; rm -rf / #\n`whoami`\n$(id)'

    script = sandbox_runner._setup_script(hostile)

    assert "rm -rf" not in script
    assert "`whoami`" not in script
    assert "$(id)" not in script
    assert sandbox_runner._b64(hostile) in script


def test_the_prompt_names_the_container_path_not_the_host_path():
    """A host path the container cannot see makes the agent give up in seconds."""
    from pathlib import Path

    inside = sandbox_runner.container_path(
        Path("/home/bss-group/bloy-worktrees/bloy-2-api"),
        Path("/home/bss-group/bloy-worktrees"),
    )

    assert inside == "/worktrees/bloy-2-api"
    assert "/home/bss-group" not in inside


def test_setup_adopts_an_existing_uid_instead_of_creating_a_second_user():
    """Base images ship a uid-1000 account; useradd would fail on a duplicate."""
    script = sandbox_runner._setup_script("do the thing")

    assert sandbox_runner.RESOLVE_AGENT_USER in script
    assert 'if [ -z "$u" ]; then' in script


def test_the_monorepo_is_mounted_read_only_for_the_map(tmp_path):
    """CLAUDE.md lives at the monorepo root, outside every sub-project.

    The first live run reported "the repo has no CLAUDE.md" and worked without
    the map, because the worktree only contains one sub-project.
    """
    monorepo = tmp_path / "BLOY"
    monorepo.mkdir()

    volumes = sandbox_runner._volumes(tmp_path / "wt", monorepo)

    mount = next(v for v in volumes if v.mount_path == sandbox_runner.MONOREPO_MOUNT)
    assert mount.read_only is True, "the agent must not edit outside its worktree"
    writable = [v.mount_path for v in volumes if not v.read_only]
    assert writable == [sandbox_runner.WORKTREE_MOUNT], (
        f"only the worktree may be writable, got {writable}"
    )


def test_a_missing_monorepo_is_simply_not_mounted(tmp_path):
    volumes = sandbox_runner._volumes(tmp_path / "wt", tmp_path / "absent")

    assert all(v.mount_path != sandbox_runner.MONOREPO_MOUNT for v in volumes)


# ---------------------------------------------------------------------------
# Credential filtering — a sandbox must never receive more than the Claude
# subscription login. Confirmed live on this host: the raw files being
# replaced here hold OAuth refresh tokens for four unrelated MCP servers and,
# in one case, a live third-party API token in plaintext.
# ---------------------------------------------------------------------------


def test_credentials_are_filtered_to_only_the_claude_login(monkeypatch, tmp_path):
    creds = tmp_path / ".credentials.json"
    creds.write_text(
        json.dumps(
            {
                "claudeAiOauth": {"accessToken": "keep-me"},
                "mcpOAuth": {"bloy-data": {"refreshToken": "drop-me"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sandbox_runner, "HOST_CLAUDE_CREDENTIALS", creds)

    filtered = sandbox_runner._filtered_credentials()

    assert json.loads(filtered) == {"claudeAiOauth": {"accessToken": "keep-me"}}
    assert "drop-me" not in filtered


def test_a_missing_credentials_file_yields_an_empty_string(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox_runner, "HOST_CLAUDE_CREDENTIALS", tmp_path / "absent.json")

    assert sandbox_runner._filtered_credentials() == ""


def test_malformed_credentials_do_not_raise(monkeypatch, tmp_path):
    creds = tmp_path / ".credentials.json"
    creds.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(sandbox_runner, "HOST_CLAUDE_CREDENTIALS", creds)

    assert sandbox_runner._filtered_credentials() == ""


def test_credentials_missing_the_claude_login_yield_an_empty_string(monkeypatch, tmp_path):
    """Only mcpOAuth present, no claudeAiOauth — nothing worth sending in."""
    creds = tmp_path / ".credentials.json"
    creds.write_text(json.dumps({"mcpOAuth": {"x": "y"}}), encoding="utf-8")
    monkeypatch.setattr(sandbox_runner, "HOST_CLAUDE_CREDENTIALS", creds)

    assert sandbox_runner._filtered_credentials() == ""


def test_claude_json_drops_projects_but_keeps_everything_else():
    text = json.dumps(
        {
            "numStartups": 5,
            "projects": {"/x/BLOY": {"mcpServers": {"jira": {"env": {"TOKEN": "secret"}}}}},
        }
    )

    filtered = sandbox_runner._filtered_claude_json(text)

    assert json.loads(filtered) == {"numStartups": 5}
    assert "secret" not in filtered


def test_claude_json_with_no_projects_key_is_unchanged():
    text = json.dumps({"numStartups": 5})

    assert json.loads(sandbox_runner._filtered_claude_json(text)) == {"numStartups": 5}


def test_an_empty_claude_json_is_left_alone():
    assert sandbox_runner._filtered_claude_json("") == ""


def test_malformed_claude_json_is_returned_unchanged_not_raised():
    assert sandbox_runner._filtered_claude_json("not json") == "not json"


def test_the_setup_script_carries_filtered_credentials_not_the_raw_file(monkeypatch, tmp_path):
    creds = tmp_path / ".credentials.json"
    creds.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "keep-me"}, "mcpOAuth": {"x": "drop-me"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(sandbox_runner, "HOST_CLAUDE_CREDENTIALS", creds)

    script = sandbox_runner._setup_script("do the thing")

    assert "drop-me" not in script
    assert sandbox_runner._b64(sandbox_runner._filtered_credentials()) in script
    assert ".credentials.json" in script


# ---------------------------------------------------------------------------
# Monorepo mirror — the real monorepo has real .env files, cert.pem, sqlite
# DBs and a .mcp.json with a live bearer token; a plain bind-mount would hand
# all of it to every sandbox. A filtered copy is mounted instead.
# ---------------------------------------------------------------------------


def test_the_mirror_excludes_secrets_but_keeps_real_source(tmp_path):
    source = tmp_path / "BLOY"
    (source / "shopify-app-loyalty-api").mkdir(parents=True)
    (source / "shopify-app-loyalty-api" / ".env").write_text("SECRET=1", encoding="utf-8")
    (source / "shopify-app-loyalty-api" / "src.ts").write_text("real code", encoding="utf-8")
    (source / "CLAUDE.md").write_text("map", encoding="utf-8")
    mirror = tmp_path / "mirror"

    result = sandbox_runner.sync_monorepo_mirror(source, mirror)

    assert result == mirror
    assert not (mirror / "shopify-app-loyalty-api" / ".env").exists()
    assert (mirror / "shopify-app-loyalty-api" / "src.ts").read_text() == "real code"
    assert (mirror / "CLAUDE.md").read_text() == "map"


def test_the_mirror_excludes_mcp_json_and_sqlite_db(tmp_path):
    source = tmp_path / "BLOY"
    source.mkdir()
    (source / ".mcp.json").write_text("{}", encoding="utf-8")
    (source / "db").mkdir()
    (source / "db" / "x.sqlite3").write_text("x", encoding="utf-8")
    mirror = tmp_path / "mirror"

    sandbox_runner.sync_monorepo_mirror(source, mirror)

    assert not (mirror / ".mcp.json").exists()
    assert not (mirror / "db").exists()


def test_a_missing_source_is_not_mirrored(tmp_path):
    assert sandbox_runner.sync_monorepo_mirror(tmp_path / "absent", tmp_path / "mirror") is None


def test_a_failed_sync_with_no_prior_mirror_mounts_nothing(monkeypatch, tmp_path):
    """The unfiltered real path must never be the fallback."""
    source = tmp_path / "BLOY"
    source.mkdir()
    mirror = tmp_path / "mirror"

    def fake_run(*a, **k):
        raise OSError("rsync not found")

    monkeypatch.setattr(sandbox_runner.subprocess, "run", fake_run)

    assert sandbox_runner.sync_monorepo_mirror(source, mirror) is None


def test_a_failed_sync_with_a_prior_mirror_keeps_the_stale_copy(monkeypatch, tmp_path):
    source = tmp_path / "BLOY"
    source.mkdir()
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    (mirror / "stale.txt").write_text("old", encoding="utf-8")

    def fake_run(*a, **k):
        raise OSError("rsync not found")

    monkeypatch.setattr(sandbox_runner.subprocess, "run", fake_run)

    result = sandbox_runner.sync_monorepo_mirror(source, mirror)

    assert result == mirror
    assert (mirror / "stale.txt").exists()


# ---------------------------------------------------------------------------
# Skill packs
# ---------------------------------------------------------------------------


def test_skill_packs_root_is_mounted_read_only_when_given(tmp_path):
    skills_root = tmp_path / "packs"
    skills_root.mkdir()

    volumes = sandbox_runner._volumes(tmp_path / "wt", None, skills_root)

    mount = next(v for v in volumes if v.mount_path == sandbox_runner.SKILLS_MOUNT)
    assert mount.read_only is True
    writable = [v.mount_path for v in volumes if not v.read_only]
    assert writable == [sandbox_runner.WORKTREE_MOUNT]


def test_a_missing_skill_packs_root_is_simply_not_mounted(tmp_path):
    volumes = sandbox_runner._volumes(tmp_path / "wt", None, tmp_path / "absent")

    assert all(v.mount_path != sandbox_runner.SKILLS_MOUNT for v in volumes)


def test_no_skill_packs_root_given_mounts_nothing_extra(tmp_path):
    volumes = sandbox_runner._volumes(tmp_path / "wt")

    assert all(v.mount_path != sandbox_runner.SKILLS_MOUNT for v in volumes)


def test_an_enabled_pack_is_materialised_under_the_run_not_the_worktree(tmp_path):
    """A copy landed straight in the container's own $HOME would vanish with
    the container — nothing on the host would show what a run actually had.
    Landing it under ``/worktrees/.bloy-skills/<run_id>`` instead means it
    survives, sits outside every repo's own worktree (so ``.claude/``, which
    is not gitignored in either sub-project, can never be swept into a merge
    request by ``git add -A``), and is symlinked in at ``~/.claude/skills`` so
    the agent still finds it in the usual place.
    """
    from bloy_dev_agent.features import skill_packs

    root = tmp_path / "packs"
    pack = skill_packs.SkillPack(
        name="bloy-sandbox-dev", description="x", path=root / "shared" / "bloy-sandbox-dev"
    )

    script = sandbox_runner._setup_script("do the thing", [pack], root, run_id="run-1")

    assert "mkdir -p /worktrees/.bloy-skills/run-1" in script
    assert 'mkdir -p "/worktrees/.bloy-skills/run-1/bloy-sandbox-dev"' in script
    assert "/skill-packs/shared/bloy-sandbox-dev/." in script
    assert "chown -R" in script and "/worktrees/.bloy-skills/run-1" in script
    assert 'rm -rf "$h/.claude/skills"' in script
    assert 'ln -s /worktrees/.bloy-skills/run-1 "$h/.claude/skills"' in script


def test_an_adhoc_run_with_no_run_id_still_gets_a_stable_skills_dir(tmp_path):
    from bloy_dev_agent.features import skill_packs

    root = tmp_path / "packs"
    pack = skill_packs.SkillPack(
        name="demo", description="x", path=root / "shared" / "demo"
    )

    script = sandbox_runner._setup_script("do the thing", [pack], root)

    assert "/worktrees/.bloy-skills/adhoc" in script


def test_a_pack_with_an_unsafe_name_is_not_copied(tmp_path):
    """A shared store is not this service's to trust; a hostile directory
    name must not reach the sandbox's shell. (The script legitimately contains
    its own ``rm -rf`` for clearing a stale symlink, so the check has to look
    for the hostile payload specifically, not just that phrase.)
    """
    from bloy_dev_agent.features import skill_packs

    hostile_name = 'evil"; rm -rf ~ #'
    root = tmp_path / "packs"
    pack = skill_packs.SkillPack(name=hostile_name, description="x", path=root / "shared" / "evil")

    script = sandbox_runner._setup_script("do the thing", [pack], root, run_id="run-1")

    assert hostile_name not in script
    assert "rm -rf ~" not in script


def test_no_enabled_skills_adds_no_copy_lines():
    baseline = sandbox_runner._setup_script("do the thing")
    same = sandbox_runner._setup_script("do the thing", [], None, "run-1")

    assert baseline == same
    assert ".claude/skills" not in baseline
    assert ".bloy-skills" not in baseline


def test_the_prompt_tells_the_agent_to_stop_on_the_wrong_repo():
    """A ticket often belongs to a sibling project; guessing wastes a whole run."""
    from bloy_dev_agent.features.twenty import mapping

    issue = mapping.normalize_issue(
        {"id": "x", "issueKey": "BLOY-4", "title": "BLS-1064: translation bundle"}
    )

    prompt = mapping.build_prompt(
        issue, "/worktrees/bls-1064-api", implement=True, monorepo="/monorepo"
    )

    assert "/monorepo/CLAUDE.md" in prompt
    assert "only WRITE inside /worktrees/bls-1064-api" in prompt
    assert "DIFFERENT sub-project" in prompt


def test_the_prompt_has_no_skills_section_when_none_are_enabled():
    from bloy_dev_agent.features.twenty import mapping

    issue = mapping.normalize_issue({"id": "x", "issueKey": "BLOY-4", "title": "t"})

    prompt = mapping.build_prompt(issue, "/worktrees/x", implement=True)

    assert "Available skills" not in prompt


def test_the_prompt_lists_enabled_skills_by_name_and_description_only():
    """Only the catalog reaches the prompt — the agent reads a SKILL.md itself
    when it decides one applies, so its full text must not be nailed in here.
    """
    from bloy_dev_agent.features.twenty import mapping

    issue = mapping.normalize_issue({"id": "x", "issueKey": "BLOY-4", "title": "t"})

    prompt = mapping.build_prompt(
        issue,
        "/worktrees/x",
        implement=True,
        enabled_skills=[("bloy-sandbox-dev", "checklist bug tiềm ẩn cho BLOY")],
    )

    assert "## Available skills" in prompt
    assert "bloy-sandbox-dev" in prompt
    assert "checklist bug tiềm ẩn cho BLOY" in prompt
    assert "~/.claude/skills" in prompt


# ---------------------------------------------------------------------------
# Staging-verify: network policy, image/resource, Playwright MCP
# ---------------------------------------------------------------------------


def test_no_egress_allowlist_means_leave_the_network_alone():
    """An empty NetworkPolicy still carries default_action='deny' — passing one
    for an ordinary run would silently cut off its network. None must mean
    'unchanged', never 'deny everything'."""
    assert sandbox_runner._network_policy(()) is None


def test_the_staging_allowlist_never_includes_gitlab():
    """This pipeline never commits or pushes from inside the container — a
    sandbox with a reason to reach GitLab would be a sandbox with a reason to
    push on its own, which nothing here is meant to do."""
    assert not any(
        "gitlab" in fqdn for fqdn in sandbox_runner.STAGING_EGRESS_ALLOW
    )


def test_the_staging_allowlist_never_names_a_bare_ip():
    """staging-control (172.17.0.1:8110) is reached by raw IP through a host
    iptables pinhole, never through this FQDN policy — NetworkRule.target only
    accepts a domain, so a bare IP here would be silently meaningless at best."""
    for fqdn in sandbox_runner.STAGING_EGRESS_ALLOW:
        assert not fqdn.replace(".", "").isdigit(), fqdn


def test_network_policy_denies_by_default_and_allows_only_the_list():
    policy = sandbox_runner._network_policy(sandbox_runner.STAGING_EGRESS_ALLOW)

    assert policy.default_action == "deny"
    targets = {rule.target for rule in policy.egress}
    assert targets == set(sandbox_runner.STAGING_EGRESS_ALLOW)
    assert all(rule.action == "allow" for rule in policy.egress)


def test_an_ordinary_run_never_writes_mcp_json():
    """.mcp.json in the worktree would be swept into the merge request by
    `git add -A` — it must only ever land in $HOME, and only for staging."""
    script = sandbox_runner._setup_script("do the thing", staging=False)

    assert ".mcp.json" not in script
    assert "playwright-mcp-config" not in script


def test_a_staging_run_writes_mcp_json_and_the_launch_config_into_home():
    script = sandbox_runner._setup_script("do the thing", staging=True)

    assert sandbox_runner._b64(sandbox_runner._staging_mcp_json()) in script
    assert sandbox_runner._b64(sandbox_runner.PLAYWRIGHT_LAUNCH_CONFIG) in script
    assert '"$h/.mcp.json"' in script
    assert sandbox_runner.PLAYWRIGHT_LAUNCH_CONFIG_PATH in script


def test_the_staging_mcp_stanza_forces_headless_and_no_sandbox():
    """Chromium's own sandbox cannot init once ~/.sandbox.toml drops
    SYS_ADMIN, and there is no display in the container."""
    stanza = json.loads(sandbox_runner._staging_mcp_json())

    args = stanza["mcpServers"]["playwright"]["args"]
    assert "--headless" in args
    assert "--no-sandbox" in args


def test_the_playwright_launch_config_disables_dev_shm():
    """Docker's default /dev/shm is 64MB with no server-config knob to raise
    it — Chromium needs to be told to spill into /tmp instead or it crashes."""
    config = json.loads(sandbox_runner.PLAYWRIGHT_LAUNCH_CONFIG)

    assert "--disable-dev-shm-usage" in config["browser"]["launchOptions"]["args"]


def test_run_in_sandbox_switches_to_the_chromium_image_only_for_staging(monkeypatch):
    captured: dict = {}

    def fake_run_coroutine(factory):
        # _run_coroutine takes a zero-arg factory; capture what run_in_sandbox
        # would have handed to _run_async without actually running it.
        import inspect

        source = inspect.getclosurevars(factory).nonlocals
        captured.update(source)
        return sandbox_runner.SandboxResult(True, "ok")

    monkeypatch.setattr(sandbox_runner, "_run_coroutine", fake_run_coroutine)

    from pathlib import Path

    sandbox_runner.run_in_sandbox(
        "do the thing", Path("/wt/x"), worktree_root=Path("/wt"), staging=True,
    )

    assert captured["effective_image"] == sandbox_runner.STAGING_IMAGE


def test_run_in_sandbox_keeps_an_explicit_custom_image(monkeypatch):
    """staging=True must not clobber a caller-chosen image (e.g. a test double)."""
    captured: dict = {}

    def fake_run_coroutine(factory):
        import inspect

        captured.update(inspect.getclosurevars(factory).nonlocals)
        return sandbox_runner.SandboxResult(True, "ok")

    monkeypatch.setattr(sandbox_runner, "_run_coroutine", fake_run_coroutine)

    from pathlib import Path

    sandbox_runner.run_in_sandbox(
        "do the thing", Path("/wt/x"), worktree_root=Path("/wt"),
        image="custom/image:tag", staging=True,
    )

    assert captured["effective_image"] == "custom/image:tag"


def test_run_in_sandbox_without_staging_keeps_the_default_image(monkeypatch):
    captured: dict = {}

    def fake_run_coroutine(factory):
        import inspect

        captured.update(inspect.getclosurevars(factory).nonlocals)
        return sandbox_runner.SandboxResult(True, "ok")

    monkeypatch.setattr(sandbox_runner, "_run_coroutine", fake_run_coroutine)

    from pathlib import Path

    sandbox_runner.run_in_sandbox("do the thing", Path("/wt/x"), worktree_root=Path("/wt"))

    assert captured["effective_image"] == sandbox_runner.DEFAULT_IMAGE
