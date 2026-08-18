"""Tests for the standalone service and the thin BAM plugin.

Two contracts are pinned here. The service must work with no BAM at all — that
is the reason it was split out. And the plugin must stay thin: if it ever pulls
the pipeline back into BAM's process, a BAM restart can tear a run in half
again, which is exactly what happened before the split.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bloy_dev_agent import preflight

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
VALID_STATES = {preflight.OK, preflight.WARN, preflight.FAIL}


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """The real service app, on a throwaway database."""
    monkeypatch.setenv("BLOY_AGENT_DB_URL", f"sqlite:///{tmp_path / 'svc.sqlite3'}")

    # db.py binds its engine at import time, so rebuild it for this database.
    import importlib

    from bloy_dev_agent import db as db_module

    importlib.reload(db_module)
    from bloy_dev_agent import models, store

    importlib.reload(models)
    importlib.reload(store)

    from bloy_dev_agent import service

    importlib.reload(service)
    return TestClient(service.create_app()), store


# ---------------------------------------------------------------------------
# The service stands alone
# ---------------------------------------------------------------------------


def test_health_answers_without_bam(client):
    app, _ = client

    payload = app.get("/api/health").json()

    assert payload["ok"] is True
    assert payload["service"] == "bloy_dev_agent"


def test_the_service_imports_nothing_from_bam():
    """The whole point of the split: no ``core.*`` outside the plugin shim.

    ``plugin.py`` and ``trigger_action.py`` are the BAM-facing shims and may use
    its SDK. Everything the service needs at run time must not, or the service
    could only ever boot inside BAM.
    """
    shims = {"plugin.py", "trigger_action.py"}
    offenders = []
    for path in PLUGIN_ROOT.rglob("*.py"):
        if path.name in shims or "tests" in path.parts or "scripts" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        if re.search(r"^\s*from\s+core\.|^\s*import\s+core\b", source, re.M):
            offenders.append(str(path.relative_to(PLUGIN_ROOT)))

    assert offenders == [], (
        "These service modules import BAM internals, so the service cannot run "
        f"on its own: {offenders}"
    )


def test_dashboard_renders_with_nothing_run_yet(client):
    app, _ = client

    response = app.get("/")

    assert response.status_code == 200, response.text[:400]
    assert "BLOY Dev Agent" in response.text
    assert "Không có task nào đang chạy" in response.text


def test_dashboard_shows_a_running_task_and_its_attempt(client):
    app, store = client
    store.start_run(
        issue_id="i-1",
        issue_key="BLS-1064",
        issue_title="Chỉ load translation của published language",
        project_id="p-1",
        target_repo="shopify-app-loyalty-api",
        attempt=2,
    )

    text = app.get("/").text

    assert "BLS-1064" in text
    assert "lần thử 2" in text


def test_blocked_issues_get_a_reset_button(client):
    app, store = client
    store.record_blocked(
        issue_id="i-2", issue_key="BLOY-9", project_id="p-1", attempt=6, detail="5 lần"
    )

    text = app.get("/").text

    assert "Đã dừng vì hết lượt thử" in text
    assert "/attempts/i-2/reset" in text


def test_reset_clears_the_cap(client):
    app, store = client
    run_id = store.start_run(
        issue_id="i-3", issue_key="BLOY-8", project_id="p-1", attempt=1
    )
    store.finish_run(run_id, state="failed", stage="sandbox")

    response = app.post("/attempts/i-3/reset", follow_redirects=False)

    assert response.status_code == 303
    assert store.attempt_status("i-3", "BLOY-8").failed == 0


def test_run_detail_shows_the_agent_reasoning(client, tmp_path):
    app, store = client
    log = tmp_path / "run.jsonl"
    log.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "thinking", "thinking": "đọc CLAUDE.md"}]},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    run_id = store.start_run(
        issue_id="i-4", issue_key="BLOY-7", project_id="p-1", attempt=1
    )
    store.set_stage(run_id, "sandbox", log_path=str(log))

    text = app.get(f"/runs/{run_id}").text

    assert "AI thinking" in text
    assert "đọc CLAUDE.md" in text


def test_run_detail_redirects_for_an_unknown_run(client):
    app, _ = client

    assert app.get("/runs/nope", follow_redirects=False).status_code == 303


def test_polling_sends_only_new_events(client, tmp_path):
    """A long run produces thousands of events; each poll must send the tail."""
    app, store = client
    log = tmp_path / "run.jsonl"
    log.write_text(
        "\n".join(
            json.dumps(
                {"type": "assistant", "message": {"content": [{"type": "text", "text": str(i)}]}}
            )
            for i in range(5)
        )
        + "\n",
        encoding="utf-8",
    )
    run_id = store.start_run(
        issue_id="i-5", issue_key="BLOY-6", project_id="p-1", attempt=1
    )
    store.set_stage(run_id, "sandbox", log_path=str(log))

    payload = app.get(f"/api/runs/{run_id}?after=3").json()

    assert payload["total"] == 5
    assert [event["text"] for event in payload["events"]] == ["3", "4"]
    assert payload["live"] is True


def test_settings_round_trip(client):
    app, store = client

    response = app.post(
        "/settings",
        data={"max_attempts": "3", "project_id": "proj-1", "blocked_status": "Backlog"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert store.max_attempts() == 3
    assert "proj-1" in app.get("/settings").text


def test_preflight_page_and_json_agree(client):
    app, _ = client

    assert app.get("/preflight").status_code == 200
    payload = app.get("/api/preflight").json()
    assert all(check["state"] in VALID_STATES for check in payload["checks"])
    keys = {check["key"] for check in payload["checks"]}
    assert {"database", "claude_cli", "sandbox", "twenty", "worktree_root"} <= keys


# ---------------------------------------------------------------------------
# Only one pass at a time
# ---------------------------------------------------------------------------


def test_a_second_trigger_is_refused_not_queued(client, monkeypatch):
    """Queueing would let a per-minute routine pile up hours it cannot catch up."""
    import threading

    from bloy_dev_agent import service

    gate = threading.Event()
    monkeypatch.setattr(
        service, "run_pass_from_config", lambda config: gate.wait(5) or {"ok": True}
    )
    app, _ = client

    first = app.post("/api/pipeline/run", json={}).json()
    second = app.post("/api/pipeline/run", json={}).json()
    gate.set()

    assert first["accepted"] is True
    assert second["accepted"] is False
    assert "đang chạy" in second["detail"]


def test_a_crashing_pass_does_not_kill_the_service(client, monkeypatch):
    from bloy_dev_agent import service

    def boom(config):
        raise RuntimeError("sandbox exploded")

    monkeypatch.setattr(service, "run_pass_from_config", boom)
    app, _ = client

    assert app.post("/api/pipeline/run", json={}).json()["accepted"] is True
    service.RUNNER._thread.join(timeout=5)

    assert app.get("/api/health").json()["ok"] is True
    assert "exploded" in json.dumps(service.RUNNER.last_summary)


def test_a_pass_without_a_project_id_reports_instead_of_crashing(client):
    from bloy_dev_agent import service

    summary = service.run_pass_from_config({})

    assert "project_id" in summary["error"]


# ---------------------------------------------------------------------------
# The BAM plugin stays thin
# ---------------------------------------------------------------------------


def test_the_plugin_contributes_only_a_link_and_a_trigger():
    from bloy_dev_agent.plugin import BloyDevAgentPlugin

    plugin = BloyDevAgentPlugin()

    assert plugin.models() == [], "models belong to the service's own database"
    assert plugin.routers() == [], "the UI is served by the service"
    (item,) = plugin.menu_items()
    assert item.url.startswith("http"), "the menu points at another server"
    (action,) = plugin.routine_actions()
    assert action.key == "bloy_run_twenty_issues"


def test_every_plugin_hook_is_callable_without_raising():
    """The registry logs and swallows hook errors, so a broken hook is silent."""
    from bloy_dev_agent.plugin import BloyDevAgentPlugin

    plugin = BloyDevAgentPlugin()
    plugin.on_startup()
    plugin.on_shutdown()
    assert plugin.meta().dependencies == []


def test_the_trigger_reports_a_down_service_instead_of_raising(monkeypatch):
    """A stopped service is an operational state, not a broken routine."""
    import httpx

    from bloy_dev_agent import trigger_action

    def refuse(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", refuse)

    payload = json.loads(trigger_action._run({"config": {"max_issues": "2"}}))

    assert "Không gọi được service" in payload["error"]
    assert "pm2" in payload["hint"]


def test_the_trigger_passes_the_configured_values_through(monkeypatch):
    import httpx

    from bloy_dev_agent import trigger_action

    sent = {}

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"accepted": True}

    def capture(url, json=None, timeout=None):
        sent["url"] = url
        sent["json"] = json
        return Response()

    monkeypatch.setattr(httpx, "post", capture)

    trigger_action._run(
        {"config": {"max_issues": "3", "project_id": "p-9", "target_repo": ""}}
    )

    assert sent["url"].endswith("/api/pipeline/run")
    assert sent["json"] == {"max_issues": 3, "project_id": "p-9"}


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def test_run_checks_never_raises_and_reports_valid_states():
    checks = preflight.run_checks()

    assert checks
    for check in checks:
        assert check.state in VALID_STATES, f"{check.key} has state {check.state!r}"
        assert check.label and check.detail


def test_a_crashing_check_becomes_a_failure_not_an_exception(monkeypatch):
    """A preflight page that raises hides the problem it exists to surface."""

    def boom():
        raise RuntimeError("probe broke")

    monkeypatch.setattr(preflight, "_CHECKS", (boom,))

    (check,) = preflight.run_checks()

    assert check.state == preflight.FAIL
    assert "probe broke" in check.detail


def test_summarise_counts_every_check():
    checks = preflight.run_checks()

    assert sum(preflight.summarise(checks).values()) == len(checks)


def test_bam_is_only_a_warning_when_absent(monkeypatch):
    """The service must not report itself broken merely because BAM is down."""
    import httpx

    def refuse(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", refuse)

    check = preflight._check_bam()

    assert check.state == preflight.WARN


# ---------------------------------------------------------------------------
# A multi-repo run must show every merge request
# ---------------------------------------------------------------------------


def test_the_history_lists_one_link_per_repo(client):
    """A single link sends the reviewer to whichever repo came first.

    They would see half the change with nothing hinting the other half exists —
    exactly what happened when BLS-2000 opened an api MR and a cms MR.
    """
    app, store = client
    run_id = store.start_run(
        issue_id="i-1", issue_key="BLS-2000", project_id="p-1", attempt=1
    )
    store.finish_run(
        run_id, state="success", stage="done",
        merge_request_url="https://gitlab/api/mr/1",
        merge_requests_json=json.dumps([
            ["shopify-app-loyalty-api", "https://gitlab/api/mr/1"],
            ["shopify-app-loyalty-cms", "https://gitlab/cms/mr/2"],
        ]),
    )

    text = app.get("/").text

    assert "https://gitlab/api/mr/1" in text
    assert "https://gitlab/cms/mr/2" in text


def test_an_older_single_url_row_still_renders(client):
    """Rows written before multi-repo support must not vanish from history."""
    app, store = client
    run_id = store.start_run(
        issue_id="i-2", issue_key="BLS-1080", project_id="p-1", attempt=1
    )
    store.finish_run(
        run_id, state="success", stage="done",
        merge_request_url="https://gitlab/api/mr/9",
    )

    text = app.get("/").text

    assert "https://gitlab/api/mr/9" in text


def test_unreadable_stored_json_does_not_break_the_page(client):
    """A corrupt row must degrade, not 500 the dashboard."""
    app, store = client
    run_id = store.start_run(
        issue_id="i-3", issue_key="BLS-1", project_id="p-1", attempt=1
    )
    store.finish_run(
        run_id, state="success", stage="done",
        merge_request_url="https://gitlab/api/mr/7",
        merge_requests_json="{not json",
    )

    response = app.get("/")

    assert response.status_code == 200
    assert "https://gitlab/api/mr/7" in response.text
