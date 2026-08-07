"""Environment checks for the BLOY Dev Agent plugin.

The plan for this plugin rests on assumptions about the host: that agent_team
is reachable, that our own table was created, that the routine scheduler is
available to drive the Twenty sync. Rather than trusting the design document,
this module answers each question against the running process and renders the
result on a page.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass

from bloy_dev_agent.features.bridge import agent_team as bridge

logger = logging.getLogger(__name__)

OK = "ok"
WARN = "warn"
FAIL = "fail"

#: Plugin that provides the routine scheduler we want to run the sync on.
ROUTINE_PLUGIN = "agent_routine"

#: Table this plugin owns; created by db_migrations/001 and models.py.
LINK_TABLE = "plugin_bloy_twenty_task_link"


@dataclass
class Check:
    """One environment assertion and what we found."""

    key: str
    label: str
    state: str
    detail: str
    why: str = ""


def _plugin_names() -> list[str]:
    try:
        from core.plugin_sdk.registry import get_registry

        return sorted(get_registry().plugins)
    except Exception:  # noqa: BLE001
        logger.exception("bloy_dev_agent: cannot list plugins")
        return []


def _check_agent_team() -> list[Check]:
    status = bridge.status()
    checks: list[Check] = []

    if not status.installed:
        checks.append(
            Check(
                key="agent_team",
                label="agent_team plugin",
                state=FAIL,
                detail="Not installed in this Agent Manager instance.",
                why=(
                    "Sync targets an Agent Team board. Without it this plugin "
                    "can still load, but has nowhere to put tasks."
                ),
            )
        )
        return checks

    checks.append(
        Check(
            key="agent_team",
            label="agent_team plugin",
            state=OK if status.enabled else WARN,
            detail=(
                f"Installed v{status.version}, "
                f"{'enabled' if status.enabled else 'DISABLED'}."
            ),
            why="" if status.enabled else "Enable it on the Plugins page.",
        )
    )

    exposed = [key for key, present in status.services.items() if present]
    missing = [key for key, present in status.services.items() if not present]
    checks.append(
        Check(
            key="agent_team_services",
            label="agent_team services()",
            state=OK if not missing else WARN,
            detail=(
                f"Exposed: {', '.join(exposed) or 'none'}"
                + (f" · missing: {', '.join(missing)}" if missing else "")
            ),
            why=(
                ""
                if not missing
                else (
                    "The supported cross-plugin channel. Until agent_team "
                    "exposes these, the bridge falls back to importing their "
                    "internals, which breaks on their refactors."
                )
            ),
        )
    )

    checks.append(
        Check(
            key="agent_team_import",
            label="Fallback: import agent_team",
            state=OK if status.importable else FAIL,
            detail=(
                "Importable — community_plugins is on sys.path."
                if status.importable
                else f"Not importable. {status.import_error}"
            ),
            why=(
                ""
                if status.importable
                else "With no services and no import, the bridge has no channel."
            ),
        )
    )

    checks.append(
        Check(
            key="agent_team_channel",
            label="Active bridge channel",
            state=OK if status.usable else FAIL,
            detail=status.channel,
        )
    )
    return checks


def _check_link_table() -> Check:
    try:
        from sqlalchemy import inspect

        from core.database.base import engine

        exists = inspect(engine).has_table(LINK_TABLE)
    except Exception as exc:  # noqa: BLE001
        return Check(
            key="link_table",
            label="Link table",
            state=FAIL,
            detail=f"Could not inspect the database: {type(exc).__name__}: {exc}",
        )
    return Check(
        key="link_table",
        label="Link table",
        state=OK if exists else FAIL,
        detail=(
            f"{LINK_TABLE} exists."
            if exists
            else f"{LINK_TABLE} is missing — did db_migrations run?"
        ),
        why="" if exists else "Check the startup log for migration errors.",
    )


def _check_routine_plugin() -> Check:
    names = _plugin_names()
    present = ROUTINE_PLUGIN in names
    return Check(
        key="routine",
        label="Routine scheduler",
        state=OK if present else WARN,
        detail=(
            f"{ROUTINE_PLUGIN} is loaded."
            if present
            else f"{ROUTINE_PLUGIN} not found."
        ),
        why=(
            "The Twenty sync runs as a RoutineAction on this scheduler "
            "instead of a hand-rolled background ticker."
        ),
    )


def _check_twenty_config() -> Check:
    base_url = os.environ.get("BLOY_TWENTY_BASE_URL", "").strip()
    api_key = os.environ.get("BLOY_TWENTY_API_KEY", "").strip()
    if base_url and api_key:
        return Check(
            key="twenty",
            label="Twenty connection",
            state=OK,
            detail=f"Configured for {base_url}",
        )
    missing = [
        name
        for name, value in (
            ("BLOY_TWENTY_BASE_URL", base_url),
            ("BLOY_TWENTY_API_KEY", api_key),
        )
        if not value
    ]
    return Check(
        key="twenty",
        label="Twenty connection",
        state=WARN,
        detail=f"Not configured — missing {', '.join(missing)}.",
        why="Expected until the Twenty admin issues an API key.",
    )


def _check_sys_path() -> Check:
    hits = [p for p in sys.path if p.rstrip("/").endswith("community_plugins")]
    return Check(
        key="sys_path",
        label="community_plugins on sys.path",
        state=OK if hits else WARN,
        detail=(
            hits[0] if hits else "Not present — cross-plugin imports will fail."
        ),
        why="Inserted by the plugin loader when it loads the first plugin.",
    )


def run_checks() -> list[Check]:
    """Run every check. Never raises — a broken check reports itself."""
    checks: list[Check] = [_check_link_table()]
    checks.extend(_check_agent_team())
    checks.append(_check_routine_plugin())
    checks.append(_check_twenty_config())
    checks.append(_check_sys_path())
    return checks


def summarise(checks: list[Check]) -> dict[str, int]:
    """Count checks by state for the page header."""
    counts = {OK: 0, WARN: 0, FAIL: 0}
    for check in checks:
        counts[check.state] = counts.get(check.state, 0) + 1
    return counts
