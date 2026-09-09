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
| **Does** | Sync Twenty ↔ its own pipeline · runs agents in per-ticket sandboxes · self-checking generator/evaluator loop with service-run verification · reviewer-feedback revision rounds · Shopify staging-verify (screenshots on a real store) · its own skill-pack selection, settings and status pages |
| **Does not** | Depend on `agent_team` or any other plugin · alter another plugin's tables · require BAM to be up to keep a run alive · stop for a human mid-run |

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
| `AI_CODE_CLAUDE_CONFIG_BASES` | BAM's own var: roots (`:`-separated) holding provisioned Claude config dirs |
| `BLOY_CLAUDE_CONFIG_DIR` | Pin every run to one Claude account, bypassing discovery |

Everything about the loop and the feedback rounds is configured on the
service's **Settings** page rather than by env var, because it is operational
policy an on-call person changes, not deployment wiring: `loop_enabled`,
`loop_max_attempts`, `loop_max_tokens`, `loop_max_cost_usd`,
`verify_commands`, `feedback_enabled`.

## The coding loop

A run is not one `claude -p` any more. Each **attempt** is:

```
generator turn    the agent writes code in the ticket's worktree
verify commands   THIS SERVICE runs the operator's command list itself,
                  in the same container, and stores receipts
evaluator turn    a separate agent — read + Bash, NO edit tool — reads the
                  diff, the receipts and the staging screenshots, and returns
                  a JSON verdict
controller        pass -> done | fail -> another attempt carrying the
                  evaluator's own "what is missing" text | out of budget -> stop
```

All of it in **one container** per run: the evaluator has to see the
generator's uncommitted diff, and a retry has to continue from work already on
disk. The container's timeout is therefore scaled by the attempt cap.

**There is no human approval step inside a run.** The agent works the ticket to
a stop on its own; a person enters at the end, reading a merge request an
independent evaluator already tried to reject. Every outcome that is not
`complete` (capped, stalled, needs_human, budget) routes the ticket to a human
rather than finishing quietly — and still opens its merge request, because work
that exists is work a reviewer should be able to look at. Unaccepted work lands
in the blocked column, never beside verified work.

### Why the service runs the verify commands, not the agent

Nothing stops a coding agent from writing "✅ tests pass" into its answer, and
the old report matched on exactly that kind of self-authored string. So the
command list is a **setting an operator configured once** — never from a ticket,
never from the agent — and this service executes it and stores the result. The
evaluator reads a projection of those receipts inside the container but cannot
write them, and a `pass` verdict is **overruled** when a receipt still says a
command failed.

Configure it on the Settings page, one `<repo>: <command>` per line:

```
shopify-app-loyalty-api: npm test
shopify-app-loyalty-cms: npm run build
```

One line must be exactly one command. An `&&`, `;`, `|` or backtick makes the
whole list invalid, and the run proceeds with **no** verification while telling
the evaluator so explicitly — silently running a subset would be worse. Only
repos an attempt actually changed get their commands run.

Budget guards (`loop_max_attempts`, `loop_max_tokens`, `loop_max_cost_usd`) are
on the same page. Hitting any of them is a hard stop that reports back, and the
evaluator's own token spend counts against them — it is a real agent run.

## Reviewer feedback

A reviewer who disagrees just **comments on the ticket**. No column to change,
no button. The feedback pass looks for a comment written after this service's
own last report and works it as a revision round:

* the comment is the authoritative instruction, outranking the description;
* the same worktree and branch, so the existing merge request is updated
  rather than a second one opened;
* each comment is claimed in the database before any work, so the poll running
  again five minutes later cannot start a second container for it;
* the agent's own reports are marked and skipped — otherwise the report ending
  a revision round would read as new feedback and the ticket would work itself
  forever;
* bare acknowledgements ("ok", "thanks", 👍) are ignored.

Both the review column and the blocked column are scanned: a human explaining
what went wrong on work the evaluator rejected is the most useful input this
pipeline can get.

It runs as its own routine (`BLOY: work reviewer feedback`) or from
`POST /api/pipeline/feedback` — deliberately separate from the new-work pass,
since it only has anything to do after somebody has read a merge request.

## Claude accounts

The Claude login comes from the accounts BAM's **AI Code Factory** already
provisioned: any directory holding a `.credentials.json` under
`AI_CODE_CLAUDE_CONFIG_BASES` (BAM's own env var, read from the `.env` this
service already loads). Runs are spread across them by a hash of the run id, so
an unattended loop spending several turns per ticket is not stuck behind one
subscription's rate limit, while a single run keeps one account across all its
turns.

Reused by **directory contract, not by import** — the same way skill packs are,
and for the same reason: this service has to boot with BAM absent. The
consequence is that the pool's `enabled`/`weight` flags live in BAM's database
and are *not* honoured here; pin one account with `BLOY_CLAUDE_CONFIG_DIR` if
that matters. Nothing discovered falls back to `~/.claude`, exactly as before.

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
