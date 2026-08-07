# BLOY Dev Agent

An [agent-manager](https://github.com/BSSCommerce/agent-manager) **community
plugin** that lets BLOY development tasks flow from **Twenty** onto an
**Agent Team** board, where agents pick them up, implement them in an isolated
sandbox, and produce a verified merge request.

> This plugin deliberately owns very little. Boards, per-task sandboxes,
> planning contracts, verification receipts, the cockpit and Mattermost
> notifications all come from the `agent_team` plugin. What lives here is the
> Twenty connection, BLOY-specific agent capabilities, and the glue.

---

## Scope

| | |
|---|---|
| **Does** | Sync Twenty ↔ Agent Team board · Shopify session capture for agents · BLOY skill pack · its own settings and status pages |
| **Does not** | Run agents · manage sandboxes · provide a board or cockpit · alter another plugin's tables · modify agent-manager core |

Two rules keep that boundary real:

1. **One door to `agent_team`.** Only `features/bridge/agent_team.py` may import
   or call it. `tests/` enforces this.
2. **Own tables only.** Migrations here create `plugin_bloy_*` tables. The
   Twenty ↔ task relation lives in a link table rather than as extra columns on
   the Agent Team task row, so either side can be upgraded or removed cleanly.

## Install (development)

From the agent-manager project root:

```bash
ln -s /path/to/bloy_dev_agent community_plugins/bloy_dev_agent
uv run agent-manager
```

`PLUGINS_EXTERNAL_DIR=community_plugins` must be set in `.env`. The core
migration runner applies `db_migrations/*.sql` on startup; the plugin then
appears on the **Plugins** page and in the sidebar.

The folder name matters: it becomes the Python package name (underscores, not
hyphens), and plugins load in alphabetical order — a name sorting before
`agent_team` would break a declared dependency on it.

## Preflight

The landing page (`/bloy-dev-agent`) runs its checks against the live process
rather than trusting the design document: whether `agent_team` is installed and
enabled, which of its `services()` are exposed, whether the direct-import
fallback works, whether this plugin's table exists, whether the routine
scheduler is available, and whether Twenty credentials are configured.

Same data as JSON at `/bloy-dev-agent/api/preflight`.

## Bridging to `agent_team`

Two channels, preferred order:

1. `PluginRegistry.get_service("agent_team", key)` — the supported cross-plugin
   channel; returns `None` when the plugin is absent or disabled.
2. Direct import of `agent_team.*` — works only because the loader puts
   `community_plugins/` on `sys.path`, and reaches into another plugin's
   internals.

`agent_team` does not override `services()` yet, so channel 2 is what runs
today. Once it exposes `create_task`, `update_task`, `get_task_state` and
`post_comment`, the fallback branch can be deleted and nothing else changes.

## Configuration

| Variable | Purpose |
|---|---|
| `BLOY_TWENTY_BASE_URL` | Twenty API base URL |
| `BLOY_TWENTY_API_KEY` | API key bound to a narrow role and a dedicated bot workspace member |

## Development

```bash
# from the agent-manager project root
PYTHONPATH=community_plugins uv run pytest community_plugins/bloy_dev_agent/tests -q
uv run ruff check community_plugins/bloy_dev_agent
```

Never edit a migration that has already been applied — the runner stores a
checksum per file and will refuse the change. Add a new file instead.

## Status

Skeleton: plugin registration, own table, bridge probe, preflight page.
Twenty sync, write-back and the Shopify session vault are not implemented yet.
