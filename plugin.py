"""BLOY Dev Agent plugin registration.

Contributes its own tables, a preflight page, and a sidebar entry. It reuses
the Agent Team plugin for boards, sandboxes, verification and the cockpit
rather than reimplementing them, and reaches it only through
``features/bridge/agent_team.py``.

``dependencies`` is intentionally left empty: the registry skips a plugin
whose declared dependency is not loaded, which would make this plugin vanish
from the admin UI with only a log line when agent_team is absent. Presence is
detected at runtime and reported on the preflight page instead.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from core.plugin_sdk.base import MenuItem, PluginBase, PluginMeta

logger = logging.getLogger(__name__)

#: Path the sidebar entry links to; matches the router prefix.
PAGE_PATH = "/bloy-dev-agent"


class BloyDevAgentPlugin(PluginBase):
    def meta(self) -> PluginMeta:
        return PluginMeta(
            name="bloy_dev_agent",
            version="0.1.0",
            description=(
                "Runs BLOY development tasks from Twenty on an Agent Team "
                "board, with BLOY-specific agent capabilities."
            ),
            author="BSS Commerce",
            dependencies=[],
        )

    def models(self) -> list:
        from bloy_dev_agent.models import BloyTwentyTaskLink

        return [BloyTwentyTaskLink]

    def routers(self) -> list[APIRouter]:
        from bloy_dev_agent.router import router

        return [router]

    def menu_items(self) -> list[MenuItem]:
        return [
            MenuItem(
                label="BLOY Dev Agent",
                url=PAGE_PATH,
                icon="puzzle",
                order=60,
                key="bloy_dev_agent",
            )
        ]

    def on_startup(self) -> None:
        from bloy_dev_agent.features.bridge import agent_team as bridge

        status = bridge.status()
        logger.info(
            "bloy_dev_agent: agent_team channel=%s (installed=%s enabled=%s)",
            status.channel,
            status.installed,
            status.enabled,
        )
