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
    """Server-to-server address of the service — used by trigger_action.py's
    call into ``/api/pipeline/run``, which runs inside BAM's OWN process (the
    dedicated pm2 process driving this routine's scheduler). Correct as
    ``http://localhost:8100`` whenever BAM and this service share a host,
    which is the common case; see ``service_public_url()`` for the separate
    address a browser needs.
    """
    return os.environ.get("BLOY_AGENT_URL", DEFAULT_SERVICE_URL).rstrip("/")


def service_public_url() -> str:
    """Browser-facing address — the sidebar "BLOY Dev Agent" link.

    Deliberately separate from service_url(): that one is correct as
    ``http://localhost:8100`` for BAM's own server-to-server call into this
    service, and exactly as wrong for a browser link as ``BAM_URL`` was for
    the service's own "BAM console" link in service.py (same bug, same fix,
    mirrored — see ``bam_public_url()`` there). A reverse proxy can even put
    the two behind different origins entirely (e.g. a path prefix nginx
    strips before forwarding server-to-server, but the browser must include).
    Defaults to service_url() so a deployment that never sets this stays
    byte-identical to today.
    """
    return os.environ.get("BLOY_AGENT_PUBLIC_URL", service_url()).rstrip("/")


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
                url=service_public_url(),
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
