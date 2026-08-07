"""Bridge to the agent_team plugin — the single coupling point of this plugin.

Two channels, in order of preference:

1. ``PluginRegistry.get_service("agent_team", <key>)`` — the supported
   cross-plugin channel. Returns ``None`` when agent_team is absent, disabled,
   or does not expose the key, so callers stay decoupled from its lifecycle.
2. Direct import of ``agent_team.*`` — a fallback that only works because the
   plugin loader puts ``community_plugins/`` on ``sys.path``. It reaches into
   another plugin's internals, so it is expected to break on their refactors.

agent_team does not override ``services()`` today, so channel 2 is what
actually runs. Once they expose the services below, delete the fallback
branch — nothing else in this plugin changes.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: Name agent_team registers itself under (``PluginMeta.name``).
AGENT_TEAM = "agent_team"

#: Services we would like agent_team to expose. Probed for diagnostics; each
#: one we get removes a piece of the direct-import fallback.
WANTED_SERVICES: tuple[str, ...] = (
    "create_task",
    "update_task",
    "get_task_state",
    "post_comment",
)


@dataclass
class AgentTeamStatus:
    """What this process can currently see of the agent_team plugin."""

    installed: bool = False
    enabled: bool = False
    version: str = ""
    services: dict[str, bool] = field(default_factory=dict)
    importable: bool = False
    import_error: str = ""

    @property
    def usable(self) -> bool:
        """True when we have some working channel into agent_team."""
        return self.enabled and (any(self.services.values()) or self.importable)

    @property
    def channel(self) -> str:
        if not self.enabled:
            return "none"
        if all(self.services.get(key) for key in WANTED_SERVICES):
            return "services"
        if any(self.services.values()):
            return "services (partial)"
        if self.importable:
            return "direct import (fallback)"
        return "none"


def _registry() -> Any | None:
    """Return the plugin registry, or ``None`` if core is not ready yet."""
    try:
        from core.plugin_sdk.registry import get_registry
    except Exception:  # noqa: BLE001 — core import must never break this plugin
        logger.exception("bloy_dev_agent: cannot import the plugin registry")
        return None
    try:
        return get_registry()
    except Exception:  # noqa: BLE001
        logger.exception("bloy_dev_agent: registry lookup failed")
        return None


def get_service(key: str):
    """Look up one agent_team service, or ``None`` when it is unavailable."""
    registry = _registry()
    if registry is None:
        return None
    try:
        return registry.get_service(AGENT_TEAM, key)
    except Exception:  # noqa: BLE001
        logger.exception("bloy_dev_agent: get_service(%s) failed", key)
        return None


def status() -> AgentTeamStatus:
    """Probe every channel into agent_team. Safe to call from a request."""
    result = AgentTeamStatus()

    registry = _registry()
    if registry is not None:
        try:
            plugins = registry.plugins
        except Exception:  # noqa: BLE001
            plugins = {}
        plugin = plugins.get(AGENT_TEAM)
        result.installed = plugin is not None
        if plugin is not None:
            try:
                result.enabled = bool(registry.is_enabled(AGENT_TEAM))
            except Exception:  # noqa: BLE001
                result.enabled = False
            try:
                result.version = plugin.meta().version
            except Exception:  # noqa: BLE001
                result.version = "?"

    result.services = {key: get_service(key) is not None for key in WANTED_SERVICES}

    # The fallback channel: can we import their package at all?
    try:
        importlib.import_module(AGENT_TEAM)
    except Exception as exc:  # noqa: BLE001 — absence is a normal outcome here
        result.importable = False
        result.import_error = f"{type(exc).__name__}: {exc}"
    else:
        result.importable = True

    return result
