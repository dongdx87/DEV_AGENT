"""Thin BAM plugin for the BLOY Dev Agent service.

The engine, the UI and the database now live in a separate process
(:mod:`bloy_dev_agent.service`), so this plugin holds nothing that can break a
run. It contributes exactly two things to BAM:

* a sidebar entry pointing at the service, and
* a routine action that asks the service to start a pass.

Why the split: while the pipeline ran inside BAM, restarting BAM killed work in
flight. BAM's routine reconciler shut down the scheduler process driving a run,
leaving an orphaned container and an issue stranded in "In Progress". Triggering
over HTTP means BAM can restart mid-run and the agent never notices.

Nothing here imports the service's models, database or pipeline. That keeps the
plugin loadable even when the service is down — it just reports that it is.
"""

from __future__ import annotations

import logging
import os

from core.plugin_sdk.base import MenuItem, PluginBase, PluginMeta, RoutineAction

logger = logging.getLogger(__name__)

#: Default address of the service. Override with ``BLOY_AGENT_URL``.
DEFAULT_SERVICE_URL = "http://localhost:8100"

#: Seconds to wait on the trigger call. The service starts the pass in the
#: background and answers at once, so this only covers the handshake.
TRIGGER_TIMEOUT = 15.0


def service_url() -> str:
    return os.environ.get("BLOY_AGENT_URL", DEFAULT_SERVICE_URL).rstrip("/")


class BloyDevAgentPlugin(PluginBase):
    """Registers the link to, and the trigger for, the standalone service."""

    def meta(self) -> PluginMeta:
        return PluginMeta(
            name="bloy_dev_agent",
            version="0.2.0",
            description=(
                "Links BAM to the standalone BLOY Dev Agent service, which pulls "
                "tasks from Twenty, writes code in a sandbox and opens merge "
                "requests. Runs in its own process on its own port."
            ),
            # Declared empty on purpose: the registry silently skips a plugin
            # whose dependencies are not already loaded, and everything this
            # plugin needs is checked at call time instead.
            dependencies=[],
        )

    def menu_items(self) -> list[MenuItem]:
        return [
            MenuItem(
                label="BLOY Dev Agent",
                url=service_url(),
                icon="cpu-chip",
            )
        ]

    def routine_actions(self) -> list[RoutineAction]:
        from bloy_dev_agent.trigger_action import build_actions

        return build_actions()

    def on_startup(self) -> None:
        # Only report what is knowable this early. A reachability probe belongs
        # in the action, which runs when BAM actually needs the service.
        logger.info(
            "bloy_dev_agent: thin plugin loaded, service expected at %s", service_url()
        )
