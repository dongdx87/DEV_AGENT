"""Routine action that asks the BLOY Dev Agent service to run a pass.

Registered through ``PluginBase.routine_actions()`` so BAM's Agent Routine owns
the cadence, the configuration form and the run history — none of which is worth
rebuilding in the service.

The action returns as soon as the service accepts the request. A pass takes
minutes of container time; blocking the scheduler for that long is what let a
BAM restart tear a run in half in the first place.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from core.plugin_sdk.base import RoutineAction, RoutineActionField

logger = logging.getLogger(__name__)

ACTION_KEY = "bloy_run_twenty_issues"


def _config_fields() -> list[RoutineActionField]:
    return [
        RoutineActionField(
            key="max_issues",
            label="Issues per pass",
            field_type="number",
            default="1",
            help_text=(
                "Capped at 3 by the service — the concurrency ceiling for this agent."
            ),
        ),
        RoutineActionField(
            key="project_id",
            label="Twenty project id (optional)",
            help_text=(
                "Leave blank to use whatever is configured on the service's own "
                "Settings page. Filling it here overrides that for this routine."
            ),
        ),
        RoutineActionField(
            key="target_repo",
            label="Repository (optional)",
            help_text="Blank means the service's configured default.",
        ),
    ]


def _int(config: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(str(config.get(key) or default))
    except ValueError:
        return default


def _run(context: dict[str, Any]) -> str:
    import httpx

    from bloy_dev_agent.plugin import TRIGGER_TIMEOUT, service_url

    config = context.get("config") or {}
    payload: dict[str, Any] = {"max_issues": _int(config, "max_issues", 1)}
    for key in ("project_id", "target_repo"):
        value = str(config.get(key) or "").strip()
        if value:
            payload[key] = value

    url = f"{service_url()}/api/pipeline/run"
    record = context.get("record_request")
    if callable(record):
        record(f"POST {url}\n{json.dumps(payload, ensure_ascii=False)}")

    try:
        response = httpx.post(url, json=payload, timeout=TRIGGER_TIMEOUT)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        # A down service is a normal operational state, not a crash: say so
        # plainly instead of raising and marking the routine run as broken.
        logger.warning("bloy_dev_agent: could not reach the service: %s", exc)
        return json.dumps(
            {
                "error": f"Không gọi được service tại {url}",
                "detail": str(exc)[:300],
                "hint": "pm2 start bloy-dev-agent",
            },
            ensure_ascii=False,
        )

    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:500]}
    return json.dumps(body, ensure_ascii=False)


def build_actions() -> list[RoutineAction]:
    return [
        RoutineAction(
            key=ACTION_KEY,
            display_name="BLOY: run Twenty issues",
            description=(
                "Asks the BLOY Dev Agent service to pick issues from its Twenty "
                "board and take each to a merge request. Returns as soon as the "
                "service accepts; watch progress on the service's own dashboard."
            ),
            order=10,
            config_fields=_config_fields(),
            run=_run,
        )
    ]
