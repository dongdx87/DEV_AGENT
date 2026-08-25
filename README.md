# BLOY Dev Agent

An [agent-manager](https://github.com/BSSCommerce/agent-manager) **community
plugin** that pulls BLOY development tickets straight from **Twenty**, writes
the code itself in an isolated sandbox, and opens a verified GitLab merge
request — no board, no other plugin involved.

> **Setup guide (dev + production, step by step): [SETUP.md](SETUP.md).**
> This file stays a short overview; SETUP.md is what a new machine should
> actually follow.

Runs as its own **standalone service**, on its own port, with its own SQLite
database — deliberately separate from BAM's own process. See
[SETUP.md § 0](SETUP.md#0-kiến-trúc-tóm-tắt) for why: while the pipeline used
to live inside BAM, restarting BAM killed whatever run was in flight.
`plugin.py` is a thin shim inside BAM — a sidebar link plus a "run a pass" HTTP
trigger — and imports nothing from the real pipeline, so BAM stays loadable
even when this service is down.

## Scope

| | |
|---|---|
| **Does** | Sync Twenty ↔ its own pipeline · runs agents in per-ticket sandboxes · Shopify staging-verify (screenshots on a real store) · its own skill-pack selection, settings and status pages |
| **Does not** | Depend on `agent_team` or any other plugin · alter another plugin's tables · require BAM to be up to keep a run alive |

## Install (development)

See [SETUP.md](SETUP.md) for the full walkthrough (Docker, OpenSandbox,
Claude CLI, GitLab SSH, Twenty credentials). Short version:

```bash
ln -s /path/to/bloy_dev_agent community_plugins/bloy_dev_agent   # from agent-manager root
cd agent-manager && uv sync && uv run setup-dependencies
PYTHONPATH=community_plugins uv run python -m bloy_dev_agent.service
```

The folder name matters: it becomes the Python package name (underscores, not
hyphens). Open `http://localhost:8100/setup` and follow the wizard.

## Preflight

`/preflight` (and `/api/preflight` as JSON) runs every setup check against the
live process — Docker, OpenSandbox config and server, the monorepo and its
agent-only mirror, Claude CLI/login, GitLab SSH, Twenty credentials, egress
mode. Same checklist SETUP.md walks through by hand.

## Configuration

| Variable | Purpose |
|---|---|
| `BLOY_TWENTY_BASE_URL` | Twenty API base URL |
| `BLOY_TWENTY_API_KEY` | API key bound to a narrow role and a dedicated bot workspace member |

## Staging-verify

When a ticket touches `shopify-app-loyalty-cms`, the pipeline automatically
gives the sandbox a real staging deploy target and a headless Chromium (via
Playwright MCP) so it can look at the real Shopify Admin embedded app after
deploying — no marker in the ticket, no approval step; the agent decides for
itself whether its change is worth verifying.

For the agent to actually reach a *logged-in* Shopify Admin, a human has to
capture a browser session once, on a real display (Shopify's 2FA and its
bot-check interstitial both need a person — a headless sandbox has neither):

1. On a machine with a real screen, run a plain Playwright script (or reuse
   the `shopify-screenshot` skill's own headed-Chrome login flow) and log
   into the "testsite 1" store's Shopify Admin as you normally would.
2. Before closing the browser, export the session:
   ```js
   await context.storageState({
     path: `${process.env.HOME}/.bloy-shopify-auth/storage-state.json`,
   });
   ```
3. `chmod 600 ~/.bloy-shopify-auth/storage-state.json` — it holds live
   session cookies, not a password, but it is still a real credential.

The sandbox only ever receives this file (mounted read-only) — never the
plaintext password. If the file is missing or the session has expired, a run
simply reports that it could not verify and moves on; it never fails the
ticket over this.

**Refresh cadence:** whenever a staging-verify run's report stops showing
"ĐÃ VERIFY TRÊN STAGING" and instead notes it hit a login wall, repeat the
steps above.

## Development

```bash
# from the agent-manager project root
PYTHONPATH=community_plugins uv run pytest community_plugins/bloy_dev_agent/tests -q
uv run ruff check community_plugins/bloy_dev_agent
```

The service owns its schema directly through SQLAlchemy (`db.py`'s
`init_db()`) — there is no `db_migrations/*.sql` here and nothing runs through
BAM's migration runner, on purpose: sharing BAM's database would tie this
service's uptime to BAM's, exactly what running standalone avoids. The SQLite
file defaults to `bloy_dev_agent/db/bloy_dev_agent.sqlite3`, overridable with
`BLOY_AGENT_DB_URL`.
