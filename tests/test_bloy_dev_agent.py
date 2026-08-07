"""Tests for the BLOY Dev Agent plugin.

Run from the agent-manager project root::

    PYTHONPATH=community_plugins uv run pytest \
        community_plugins/bloy_dev_agent/tests -q
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bloy_dev_agent import preflight
from bloy_dev_agent.features.bridge import agent_team as bridge
from bloy_dev_agent.plugin import BloyDevAgentPlugin

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
VALID_STATES = {preflight.OK, preflight.WARN, preflight.FAIL}


# ---------------------------------------------------------------------------
# Plugin contract
# ---------------------------------------------------------------------------


def test_meta_declares_no_hard_dependency():
    """A declared-but-missing dependency makes the registry skip the plugin.

    The registry drops a plugin whose ``dependencies`` are not already loaded,
    logging one line and showing nothing in the UI. agent_team is detected at
    runtime instead so the plugin always appears, even when it is absent.
    """
    meta = BloyDevAgentPlugin().meta()
    assert meta.name == "bloy_dev_agent"
    assert meta.dependencies == []


def test_router_prefix_matches_menu_url():
    plugin = BloyDevAgentPlugin()
    (router,) = plugin.routers()
    (item,) = plugin.menu_items()
    assert router.prefix == item.url


def test_models_use_the_plugin_table_prefix():
    for model in BloyDevAgentPlugin().models():
        assert model.__tablename__.startswith("plugin_bloy_")


# ---------------------------------------------------------------------------
# Boundary: only the bridge may touch agent_team
# ---------------------------------------------------------------------------


#: Imports of the top-level ``agent_team`` package only. Importing this
#: plugin's own ``...bridge.agent_team`` module is what every caller should do,
#: so the pattern is anchored to the start of the import statement.
_AGENT_TEAM_IMPORT = re.compile(r"^\s*(?:from|import)\s+agent_team\b", re.MULTILINE)


def test_only_the_bridge_imports_agent_team():
    """Keep the coupling in one file so a refactor upstream has one blast radius."""
    allowed = (PLUGIN_ROOT / "features" / "bridge" / "agent_team.py").resolve()
    offenders = []
    for path in PLUGIN_ROOT.rglob("*.py"):
        if path.resolve() == allowed or "tests" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        if _AGENT_TEAM_IMPORT.search(source):
            offenders.append(str(path.relative_to(PLUGIN_ROOT)))
    assert offenders == [], (
        "These files reach into agent_team directly; route them through "
        f"features/bridge/agent_team.py instead: {offenders}"
    )


def test_bridge_status_is_safe_without_agent_team():
    """The probe must report absence, not raise, when agent_team is missing."""
    status = bridge.status()
    assert set(status.services) == set(bridge.WANTED_SERVICES)
    assert isinstance(status.usable, bool)
    assert status.channel in {
        "none",
        "services",
        "services (partial)",
        "direct import (fallback)",
    }


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def test_run_checks_never_raises_and_reports_valid_states():
    checks = preflight.run_checks()
    assert checks, "preflight should always produce at least one check"
    for check in checks:
        assert check.state in VALID_STATES, f"{check.key} has state {check.state!r}"
        assert check.label and check.detail


def test_summarise_counts_every_check():
    checks = preflight.run_checks()
    counts = preflight.summarise(checks)
    assert sum(counts.values()) == len(checks)


def test_preflight_page_renders_against_the_real_template_env():
    """Catch a broken template here rather than as a 500 in the browser.

    Registers this plugin's template dir the same way discovery does, then
    renders through the shared environment so ``{% extends "base.html" %}``
    is resolved against the real core template.
    """
    from core.template_env import get_templates, register_template_dir, reset_templates

    register_template_dir(PLUGIN_ROOT / "templates")
    reset_templates()

    checks = preflight.run_checks()
    template = get_templates().get_template("bloy_preflight.html")
    html = template.render(
        request=SimpleNamespace(
            state=SimpleNamespace(), url=SimpleNamespace(path="/bloy-dev-agent")
        ),
        title="BLOY Dev Agent",
        checks=checks,
        counts=preflight.summarise(checks),
    )

    assert "BLOY Dev Agent" in html
    assert "Preflight" in html
    for check in checks:
        assert check.label in html


def test_preflight_json_route():
    """Mount the router on a bare app so the core auth middleware is out of scope."""
    app = FastAPI()
    (router,) = BloyDevAgentPlugin().routers()
    app.include_router(router)

    response = TestClient(app).get("/bloy-dev-agent/api/preflight")

    assert response.status_code == 200
    payload = response.json()
    assert {"counts", "checks"} <= payload.keys()
    assert all(check["state"] in VALID_STATES for check in payload["checks"])
    keys = {check["key"] for check in payload["checks"]}
    assert {"link_table", "agent_team", "routine", "twenty"} <= keys
