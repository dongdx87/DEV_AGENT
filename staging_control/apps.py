"""The fixed table of deployable staging apps.

Modelled on :data:`bloy_dev_agent.setup_wizard.FIXES` — "a fixed table rather
than a lookup by attribute, so a crafted key can never reach an arbitrary
callable." Here the caller sends a bare ``app`` key (``"api"`` or ``"cms"``);
every path, argv list and process name it maps to is declared here in Python,
never assembled from anything the caller sends.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: Sibling of the personal-dev checkout, never inside it — see
#: ``setup_bloy_staging/app.env.example``'s own comment on why this must
#: differ from ``$HOME/BLOY``.
STAGING_ROOT = Path.home() / "bloy-staging" / "repos"


@dataclass(frozen=True)
class StagingApp:
    """One deployable app: fixed paths, fixed commands, nothing caller-supplied."""

    key: str
    repo: str
    checkout: Path
    pm2_processes: tuple[str, ...]
    #: argv lists run with cwd=checkout, in order. Empty for api: it runs
    #: `nest start --watch`, so a deploy is rsync-then-restart, no build step.
    build: tuple[tuple[str, ...], ...]
    restart: tuple[str, ...]
    #: ("tcp", host, port) or ("http", url) — see actions._wait_healthy.
    health_check: tuple[str, ...]
    ready_timeout_s: int


STAGING_APPS: dict[str, StagingApp] = {
    "api": StagingApp(
        key="api",
        repo="shopify-app-loyalty-api",
        checkout=STAGING_ROOT / "shopify-app-loyalty-api",
        pm2_processes=("bloy-stg-api", "bloy-stg-webhook", "bloy-stg-cron"),
        build=(),
        restart=("pm2", "restart", "bloy-stg-api", "bloy-stg-webhook", "bloy-stg-cron"),
        health_check=("tcp", "localhost", "9976"),
        ready_timeout_s=30,
    ),
    "cms": StagingApp(
        key="cms",
        repo="shopify-app-loyalty-cms",
        checkout=STAGING_ROOT / "shopify-app-loyalty-cms",
        pm2_processes=("bloy-stg-cms",),
        build=(
            ("npm", "run", "build", "--prefix", "web/frontend"),
            ("pnpm", "--filter", "bloy-extensions", "run", "build-bloy"),
        ),
        restart=("pm2", "restart", "bloy-stg-cms"),
        health_check=("http", "http://localhost:3012/life-check"),
        ready_timeout_s=180,
    ),
}

#: Paths never copied from a worktree onto the staging checkout — either
#: secrets (`.env`, `shopify.app.toml`) or per-checkout generated state that
#: does not exist in a plain worktree and must not be deleted by `--delete`.
#:
#: `src/keys` is the second kind, found the hard way: a real deploy rsync'd a
#: worktree with no RSA keys of its own onto the staging checkout, and
#: `--delete` removed the keypair `bloy_api_post_install` had already
#: generated there — the API then failed to boot with
#: `ENOENT: ./src/keys/private.pem`, since `DecryptMiddleware` reads it on
#: every request. Losing `.env`/`shopify.app.toml` instead would silently
#: point staging at the developer's own tunnel or database.
#:
#: The lockfiles are the same class of bug, found the same way: both repos
#: gitignore `pnpm-lock.yaml` (cms) — a plain `git worktree add` never has
#: one, since it is never tracked — so the first real cms deploy's `--delete`
#: wiped the copy `pnpm install` had generated in the staging checkout, and
#: the very next build failed to resolve `@shopify/polaris-tokens` because
#: `node_modules` was still there but the lockfile pnpm needed to trust it
#: against was gone. Same reasoning as workspace.py's own `LOCKFILES`: a
#: ticket must never change a lockfile, so the worktree's copy (tracked or
#: not) is never more authoritative than what the checkout already installed.
#:
#: `pnpm-workspace.yaml` is the fourth instance of the exact same bug, found
#: the exact same way: the tracked copy has no `allowBuilds` section at all,
#: only the staging checkout's own (locally approved) copy does — a plain
#: worktree overwriting it every deploy would silently re-arm pnpm's
#: `approve-builds` interactive gate on the very next `pnpm install` the
#: extensions build runs, breaking a deploy that changed nothing about
#: dependencies at all.
RSYNC_EXCLUDES = (
    ".git",
    "node_modules",
    "dist",
    "build",
    ".next",
    "coverage",
    ".env",
    "web/.env",
    "shopify.app.toml",
    ".shopify",
    "src/keys",
    "web/frontend/dist",
    "web/frontend/node_modules",
    "extensions/cdn-dist",
    "pnpm-lock.yaml",
    "package-lock.json",
    "yarn.lock",
    "pnpm-workspace.yaml",
    # Empty directory git never tracks (git has no concept of an empty dir),
    # so a plain worktree never has it — same "checkout-local, not in git"
    # class as the lockfiles above, just for a different reason. Its absence
    # crashed `shopify app deploy`'s theme-app-extension build outright
    # (`ENOENT: scandir .../theme-app-extension/locales`), not just warned.
    "extensions/theme-app-extension/locales",
)
