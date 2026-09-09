"""Run the coding agent inside an OpenSandbox container.

Why a container at all: implement mode lets the agent write files, and an
unattended writer needs a blast radius. Here the only writable host path is
the issue's own worktree — the rest of the machine is either absent or mounted
read-only — so the worst outcome of a bad run is a branch nobody merges.

What is deliberately *not* in the container: the SSH key. The sandbox produces
a dirty worktree and nothing else; committing, pushing and opening the merge
request happen on the host in :mod:`bloy_dev_agent.features.workspace`. A
credential the agent cannot reach is a credential it cannot leak.

Mount layout::

    /worktrees      <- host worktree root   (read-write, the only one)
    /monorepo       <- monorepo root        (read-only, CLAUDE.md + sibling projects)
    /opt/nvm        <- host nvm             (read-only, node + the claude CLI)
    /skill-packs    <- shared skill store   (read-only, only if any pack is enabled)

``~/.claude`` is deliberately **not** mounted or copied wholesale — that
directory also holds OAuth refresh tokens for four MCP servers this pipeline
never talks to (bloy-knowledge, bloy-data, bloy-diagnose, Atlassian) and, in
``settings.json``, a live third-party API token. Only two small, filtered JSON
blobs are pushed in instead: the Claude subscription login on its own
(``claudeAiOauth``, stripped of every ``mcpOAuth`` entry) written straight to
``$HOME/.claude/.credentials.json``, and ``~/.claude.json`` with its
``projects`` key removed (that key is where this machine's own MCP server
configs and per-project state live — irrelevant to a fresh worktree, and
without it the CLI reports "configuration file not found" and exits before
doing any work).

The agent runs as an unprivileged user inside the container, not root, for two
independent reasons: the CLI refuses ``--dangerously-skip-permissions`` under
root outright, and files it creates in the bind-mounted worktree must come out
owned by the host user rather than by root.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shlex
import subprocess
import threading
import tomllib
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from bloy_dev_agent.features import agent_log, claude_accounts, skill_packs

logger = logging.getLogger(__name__)

DEFAULT_IMAGE = "opensandbox/code-interpreter:v1.0.2"
DEFAULT_TIMEOUT_MINUTES = 30

#: Built from ``sandbox_image/Dockerfile`` — the base image plus a pinned
#: Chromium. Only used for a staging-verify run; every other run keeps
#: DEFAULT_IMAGE unchanged, so a build failure here can never break support
#: tickets that never touch a browser.
STAGING_IMAGE = "bloy-dev-agent/sandbox-chromium:v1"

#: The Chromium binary that image actually installed — confirmed live inside
#: a real container. Without this, @playwright/mcp's default `--browser`
#: channel is "chrome" (real Google Chrome), which this image never installs
#: — a real staging-verify run hit exactly that: "Chromium distribution
#: 'chrome' is not found at /opt/google/chrome/chrome", and the agent user has
#: no permission to `npx playwright install chrome` to fix it itself. Pinned
#: the same way NODE_VERSION is — bump this alongside sandbox_image/Dockerfile
#: if it ever installs a different Playwright/Chromium revision.
STAGING_CHROMIUM_EXECUTABLE = "/opt/ms-playwright/chromium-1237/chrome-linux64/chrome"

#: A staging-verify run compiles the CMS frontend and drives a real Chromium —
#: the 1 CPU / 2Gi default (opensandbox-server's own default, not set here)
#: starves both. Only passed to Sandbox.create() when staging is requested.
STAGING_RESOURCE = {"cpu": "2", "memory": "6Gi"}

#: FQDNs a staging-verify sandbox may reach — everything else is denied by the
#: egress sidecar. Deliberately excludes sbc-gitlab.bsscommerce.com: this
#: pipeline never commits or pushes from inside the container (see module
#: docstring), so the agent has no legitimate reason to reach GitLab at all.
#:
#: Confirmed live on this host: `dns+nft` really does block a direct-by-IP
#: connection to an un-allowlisted address, not just the DNS lookup — a
#: sandbox with this policy cannot reach 172.17.0.1:27017/6379 even though
#: nothing about them is DNS-based. The corollary bit us immediately:
#: staging-control (172.17.0.1:8110) is *also* unreachable this way, because
#: NetworkRule.target only accepts an FQDN, never a bare IP — there is no
#: entry that could have covered it. The fix is the same one already used for
#: the two staging apps: front staging-control with its own Cloudflare tunnel
#: hostname (added to the *existing* personal-dev tunnel's ingress list, see
#: the setup notes in staging_control/service.py) so it becomes an ordinary
#: FQDN like any other entry here, instead of a bare IP this policy can never
#: express.
STAGING_EGRESS_ALLOW: tuple[str, ...] = (
    "api.anthropic.com",
    "statsig.anthropic.com",
    "registry.npmjs.org",
    "admin.shopify.com",
    "accounts.shopify.com",
    "*.myshopify.com",
    "cdn.shopify.com",
    "monorail-edge.shopifysvc.com",
    # admin.shopify.com's WAF occasionally serves a real interactive Cloudflare
    # Turnstile challenge instead of passively trusting the mounted profile's
    # cookies (see SHOPIFY_CHROME_PROFILE_DIRNAME's own comment) — confirmed
    # live: a real run's console log showed ERR_NAME_NOT_RESOLVED for exactly
    # this domain's /turnstile/v0/b/.../api.js, and the page never got past
    # "Just a moment…" as a direct result, even with the profile mounted and
    # headed Chromium. Without this domain, that specific case can never
    # resolve — the challenge script itself is what proves the browser real.
    "challenges.cloudflare.com",
    # The tester-id-scoped names below ("dev-dongdx2k3-bloy-staging-*") went
    # stale a second time: the shared staging environment moved off a
    # per-developer tunnel to a shared one on 2026-08-27, confirmed against
    # /etc/cloudflared/config.yml's actual ingress rules and the CMS
    # checkout's own web/.env (VITE_HOST) and shopify.app.toml
    # (application_url) — those are the real source of truth for these
    # hostnames, not this list. dns+nft blocks even the DNS lookup for
    # anything off this list, so a stale entry here fails exactly like a
    # missing one (confirmed live: a real run got ERR_NAME_NOT_RESOLVED
    # against the old CMS name). staging-control has no tunnel route at all
    # yet under the new scheme — kept here so the allowlist is ready the
    # moment one exists; until then staging-control calls fail at the HTTP
    # layer (502/530), not at DNS.
    "dev-bloy-api-staging.dev-bsscommerce.com",
    "dev-bloy-cms-staging.dev-bsscommerce.com",
    "dev-bloy-staging-control.dev-bsscommerce.com",
    # Shopify's own Theme Editor (opened from Admin to add/configure a theme
    # app block, e.g. "Points on product page") loads this host for its own
    # UI chrome. Confirmed live: a real staging-verify run trying to add that
    # block got a blank page and a stuck load — this host resolving nowhere
    # under dns+nft is why, not a real Theme Editor outage.
    "online-store-web.shopifyapps.com",
)

WORKTREE_MOUNT = "/worktrees"
NVM_MOUNT = "/opt/nvm"
#: Monorepo root, read-only, so the agent can read CLAUDE.md and sibling projects.
MONOREPO_MOUNT = "/monorepo"

#: A logged-in Shopify admin session, captured once by a human on a real
#: display (2FA and Shopify's bot-check interstitial both need a person; a
#: headless container has neither) and reused read-only by every
#: staging-verify run after that. This directory holds ONLY the exported
#: Playwright ``storageState`` (cookies) — never a plaintext password, and
#: never written to from inside the container. Sibling to $HOME, same
#: reasoning as DEFAULT_MONOREPO_MIRROR: outside every worktree, so it can
#: never be swept into a merge request.
SHOPIFY_AUTH_DIR = Path.home() / ".bloy-shopify-auth"
SHOPIFY_AUTH_MOUNT = "/shopify-auth"
SHOPIFY_STORAGE_STATE_FILENAME = "storage-state.json"

#: A full, aged Chrome profile (cookies AND local storage AND whatever else
#: Cloudflare's bot-management scores a session on) — preferred over the
#: bare storageState above whenever present. Confirmed live, repeatedly: a
#: real interactive Playwright session reusing a long-lived profile at
#: ``~/.cache/ms-playwright-mcp/`` walks straight into admin.shopify.com with
#: zero challenge, while this same host's sandbox — a *fresh* headless
#: Chromium seeded with only that session's exported cookies — hits
#: Cloudflare's "Just a moment…" interstitial on the very same store, on the
#: very same day. A replayed cookie in a brand-new browser fingerprint is
#: exactly the pattern bot-management is built to catch; the fix is to hand
#: the container the *whole* aged profile, not just its cookies, so Chromium
#: presents the same fingerprint history a challenge already cleared for.
#: Read-write (unlike SHOPIFY_AUTH_MOUNT): Chrome writes lock files, cache and
#: session state into its own profile dir continuously, even when only
#: reading pages — a read-only mount would fail to launch at all. Safe to
#: share across runs because staging is already single-flight (at most one
#: run holds :mod:`staging_tokens` at a time), and AGENT_UID is the host's own
#: uid (see its own comment), so the mounted profile's ownership just works.
SHOPIFY_CHROME_PROFILE_DIRNAME = "chrome-profile"
SHOPIFY_CHROME_PROFILE_MOUNT = "/shopify-chrome-profile"

#: Virtual display for real headed Chromium — see _staging_mcp_json's own
#: comment for why headed mode matters here at all. ``sandbox_image/Dockerfile``
#: installs the ``xvfb`` package that provides this binary; a container built
#: without that package simply fails this one command (captured in the setup
#: script's own exit-code check, same as every other setup step) rather than
#: silently falling through to a Chromium that can't open a display.
XVFB_DISPLAY = ":99"
XVFB_START_COMMAND = (
    f"Xvfb {XVFB_DISPLAY} -screen 0 1280x800x24 >/tmp/xvfb.log 2>&1 & disown"
)


def _xvfb_prefix(chrome_profile_mounted: bool) -> str:
    """Shell fragment that starts Xvfb before ``claude -p``, only when a
    mounted profile means Chromium is about to run headed (see
    _staging_mcp_json's own comment). A tiny pure function on purpose — the
    surrounding ``inner`` command it feeds into is built inside an async
    function that talks to a real sandbox, out of reach for a plain unit test.
    """
    return f"{XVFB_START_COMMAND}; " if chrome_profile_mounted else ""

#: Where the filtered copy of the monorepo is kept, sibling to the worktree
#: root. A plain bind-mount of the real monorepo hands every sandbox every
#: ``.env`` file, ``cert.pem``, sqlite DB and ``.mcp.json`` bearer token in the
#: tree — confirmed reachable from inside a container. Rsync-ing a filtered
#: copy here before each run keeps everything the agent legitimately reads
#: (CLAUDE.md, docs, sibling repos' source) while dropping what it never needed.
DEFAULT_MONOREPO_MIRROR = Path.home() / "bloy-monorepo-mirror"

#: Matched against any path component (rsync ``--exclude`` semantics), so
#: ``.env`` also catches ``web/.env`` wherever it sits in the tree.
MONOREPO_MIRROR_EXCLUDES = (
    ".env", ".env.*", "*.pem", "*.key", "*.sqlite3", ".mcp.json",
    "credentials*", ".credentials.json", "db",
    # Bulk and regenerable — irrelevant to reading source or docs, and
    # dropping them keeps the mirror sync fast.
    ".git", "node_modules", "dist", "build", ".next", "coverage",
)
#: Shared skill-pack store, read-only. Only mounted when at least one pack is
#: enabled — an empty mount would just be a mkdir nobody asked for.
SKILLS_MOUNT = "/skill-packs"

#: Node release that carries the Claude CLI on this host. Pinned rather than
#: globbed because the container must not silently pick up a different one.
NODE_VERSION = "v20.20.2"

SANDBOX_CONFIG = Path.home() / ".sandbox.toml"
HOST_NVM = Path.home() / ".nvm"
HOST_CLAUDE_JSON = Path.home() / ".claude.json"
#: Where the actual OAuth tokens live — never mounted or copied whole (see
#: module docstring); only its ``claudeAiOauth`` key is ever read out of it.
HOST_CLAUDE_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"

#: Unprivileged user created inside the container. The uid matches the host
#: user so files written into the bind-mounted worktree come back owned by the
#: person who has to review them, not by root.
AGENT_USER = "bloy"
AGENT_UID = os.getuid()
AGENT_GID = os.getgid()
AGENT_HOME = f"/home/{AGENT_USER}"

#: The prompt is delivered as a file and piped in, never interpolated into the
#: shell command: issue bodies contain quotes, backticks and newlines.
PROMPT_PATH = "/tmp/bloy-prompt.txt"

#: Where the CLI's stderr lands when stdout is reserved for the JSON stream.
STDERR_PATH = "/tmp/bloy-stderr.txt"

#: Stamped on every sandbox this plugin creates so a later sweep can tell ours
#: apart from anything else sharing the OpenSandbox server. Without a marker the
#: only safe reaper is no reaper.
OWNER_TAG = "bloy_dev_agent"

#: Header the server wants for the admin REST surface. The SDK has no list/kill
#: by id, so the orphan sweep talks to that surface directly.
API_KEY_HEADER = "OPEN-SANDBOX-API-KEY"

#: Shell fragment setting ``$u`` to whoever owns the agent uid in the image.
RESOLVE_AGENT_USER = f"u=$(getent passwd {AGENT_UID} | cut -d: -f1)"


class SandboxError(RuntimeError):
    """Raised when the sandbox cannot be prepared or the run fails outright."""


@dataclass
class SandboxResult:
    ok: bool
    output: str
    sandbox_id: str = ""
    exit_code: int = 0


def sync_monorepo_mirror(
    source: Path, mirror_root: Path = DEFAULT_MONOREPO_MIRROR
) -> Path | None:
    """Rsync a filtered copy of ``source``, and return the path to mount.

    Never falls back to mounting ``source`` itself on failure — that would
    silently undo the whole point of filtering it. A failed sync keeps
    whichever (still-filtered) copy the last successful sync produced; only a
    first-ever sync that fails returns ``None``, so the run proceeds without
    CLAUDE.md rather than with the unfiltered tree.
    """
    if not source.is_dir():
        return None
    mirror_root.mkdir(parents=True, exist_ok=True)
    had_previous_sync = any(mirror_root.iterdir())

    args = ["rsync", "-a", "--delete"]
    for pattern in MONOREPO_MIRROR_EXCLUDES:
        args += ["--exclude", pattern]
    args += [f"{source}/", f"{mirror_root}/"]

    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=300)
        ok = result.returncode == 0
        detail = (result.stderr or "").strip()[:300]
    except (OSError, subprocess.TimeoutExpired) as exc:
        ok, detail = False, str(exc)

    if not ok:
        if had_previous_sync:
            logger.warning(
                "bloy_dev_agent: monorepo mirror sync failed (%s); using the "
                "last successful copy instead of retrying with the real path",
                detail,
            )
            return mirror_root
        logger.warning(
            "bloy_dev_agent: monorepo mirror sync failed (%s) and no prior "
            "mirror exists; mounting nothing rather than the unfiltered tree",
            detail,
        )
        return None
    return mirror_root


def _connection():
    from opensandbox.config.connection import ConnectionConfig

    if not SANDBOX_CONFIG.exists():
        raise SandboxError(f"OpenSandbox config not found at {SANDBOX_CONFIG}")
    config = tomllib.loads(SANDBOX_CONFIG.read_text(encoding="utf-8"))
    server = config.get("server") or {}
    host = str(server.get("host") or "127.0.0.1")
    port = str(server.get("port") or "8080")
    api_key = str(server.get("api_key") or "").strip()
    if not api_key:
        raise SandboxError("OpenSandbox server has no api_key configured")
    return ConnectionConfig(domain=f"{host}:{port}", api_key=api_key, secure=False)


def _network_policy(allow: tuple[str, ...]):
    """``None`` means "leave the sandbox's network alone", not "deny everything".

    An empty-egress ``NetworkPolicy`` would still carry ``default_action="deny"``
    and get passed to ``Sandbox.create()``, cutting off every ordinary run's
    network — that failure mode is why this returns ``None`` for an empty list
    instead of a locked-down policy object.
    """
    if not allow:
        return None
    from opensandbox.models.sandboxes import NetworkPolicy, NetworkRule

    return NetworkPolicy(
        default_action="deny",
        egress=[NetworkRule(action="allow", target=fqdn) for fqdn in allow],
    )


def _shopify_storage_state_path(shopify_auth_dir: Path | None) -> Path | None:
    """The exported session file, if a human has actually captured one.

    Checked as a file (not just the directory) so a directory that exists but
    is still empty — nobody has logged in yet — is treated the same as no
    directory at all: no volume, no storageState key, a plain logged-out
    browser inside the container.
    """
    if shopify_auth_dir is None:
        return None
    candidate = shopify_auth_dir / SHOPIFY_STORAGE_STATE_FILENAME
    return candidate if candidate.is_file() else None


def _shopify_chrome_profile_path(shopify_auth_dir: Path | None) -> Path | None:
    """The captured Chrome profile directory, if one has actually been set up.

    Checked as a non-empty directory containing real profile state (its
    ``Default`` subdirectory), not just existence — an empty placeholder
    directory must fall back to the plain storageState path exactly like a
    missing one would.
    """
    if shopify_auth_dir is None:
        return None
    candidate = shopify_auth_dir / SHOPIFY_CHROME_PROFILE_DIRNAME
    return candidate if (candidate / "Default").is_dir() else None


#: Chrome's own instance-detection files. A previous run that was killed
#: uncleanly (container OOM, host power loss, a `_run_async` crash before
#: the sandbox tears itself down) leaves these behind, and every future
#: staging-verify run would then see the profile as "already in use" and
#: refuse to launch a browser at all — confirmed live, the very first time
#: this profile-mount feature ran after a prior attempt was interrupted.
#: Safe to always clear before a new run starts: staging is already
#: single-flight (see staging_tokens's own module docstring — at most one
#: run holds it at a time), so by the time this runs, whatever process wrote
#: these is guaranteed to be gone, stale lock or not.
_CHROME_SINGLETON_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie")


def _clear_stale_chrome_singleton_files(profile: Path) -> None:
    for name in _CHROME_SINGLETON_FILES:
        (profile / name).unlink(missing_ok=True)


def _volumes(
    worktree_root: Path,
    monorepo: Path | None = None,
    skill_packs_root: Path | None = None,
    shopify_auth_dir: Path | None = None,
    agent_repos_root: Path | None = None,
):
    from opensandbox.models.sandboxes import Host, Volume

    volumes = [
        Volume(
            name="worktrees",
            host=Host(path=str(worktree_root)),
            mount_path=WORKTREE_MOUNT,
        ),
        Volume(
            name="nvm", host=Host(path=str(HOST_NVM)), mount_path=NVM_MOUNT, read_only=True
        ),
    ]

    # workspace.prepare() branches worktrees from this mirror whenever it
    # exists, never from the developer's own checkout (see its own docstring).
    # A linked worktree's ``.git`` file is a pointer to an ABSOLUTE HOST PATH
    # back into this mirror's ``.git/worktrees/<branch>`` admin dir — mounted
    # at the identical path so that pointer still resolves inside the
    # container. Confirmed live: without this, every git command run from
    # inside such a worktree fails outright ("not a git repository"), because
    # the mirror simply does not exist in the container's filesystem — the
    # agent can still edit files (the worktree's working tree itself lives
    # under ``worktree_root``, mounted above), it just cannot see its own
    # history or diff. Read-write, unlike ``monorepo`` below: git needs to
    # write the worktree's HEAD/index under here on every commit, not just
    # read it.
    if agent_repos_root is not None and agent_repos_root.is_dir():
        volumes.append(
            Volume(
                name="agent-repos",
                host=Host(path=str(agent_repos_root)),
                mount_path=str(agent_repos_root),
            )
        )
    # No volume for ~/.claude: it holds MCP refresh tokens this pipeline never
    # uses and, in settings.json, a live third-party API token (see module
    # docstring). Only the two filtered JSON blobs the setup script writes
    # directly ever reach the container.

    # The monorepo map lives at its root, outside every sub-project, so an agent
    # confined to one worktree cannot see it — the first live run reported "the
    # repo has no CLAUDE.md" and worked without the map. Mounting the root
    # read-only fixes that and lets the agent cross-reference sibling projects,
    # while keeping the issue's own worktree the only writable path.
    if monorepo is not None and monorepo.is_dir():
        volumes.append(
            Volume(
                name="monorepo",
                host=Host(path=str(monorepo)),
                mount_path=MONOREPO_MOUNT,
                read_only=True,
            )
        )

    if skill_packs_root is not None and skill_packs_root.is_dir():
        volumes.append(
            Volume(
                name="skill-packs",
                host=Host(path=str(skill_packs_root)),
                mount_path=SKILLS_MOUNT,
                read_only=True,
            )
        )

    # Mounted whole-directory (not the single file) to match every other
    # volume here — and only when the file itself is actually present, so a
    # human who hasn't captured a session yet (or let it lapse) gets no
    # volume at all rather than an empty mount or a mount-time error.
    if _shopify_storage_state_path(shopify_auth_dir) is not None:
        volumes.append(
            Volume(
                name="shopify-auth",
                host=Host(path=str(shopify_auth_dir)),
                mount_path=SHOPIFY_AUTH_MOUNT,
                read_only=True,
            )
        )

    # A dedicated volume, not a subpath of the read-only mount above: Chrome
    # writes lock files, cache and session state into its own profile
    # continuously, so this one has to be read-write. See
    # SHOPIFY_CHROME_PROFILE_DIRNAME's own comment for why a full profile is
    # mounted here at all instead of only the storageState file above.
    chrome_profile = _shopify_chrome_profile_path(shopify_auth_dir)
    if chrome_profile is not None:
        volumes.append(
            Volume(
                name="shopify-chrome-profile",
                host=Host(path=str(chrome_profile)),
                mount_path=SHOPIFY_CHROME_PROFILE_MOUNT,
            )
        )
    return volumes


def _text(execution) -> str:
    """Flatten an OpenSandbox execution into plain text.

    Its logs are lists of message objects, not strings — joining them naively
    raises, which is worth encoding once here instead of at each call site.
    """
    logs = getattr(execution, "logs", None)
    parts: list[str] = []
    for stream in ("stdout", "stderr"):
        for message in getattr(logs, stream, None) or []:
            parts.append(str(getattr(message, "text", message)))
    return "".join(parts).strip()


def container_path(worktree: Path, worktree_root: Path) -> str:
    """Where ``worktree`` appears inside the sandbox.

    The prompt has to speak the container's paths, not the host's — an agent
    told to work in a directory that does not exist simply reports that and
    stops.
    """
    return f"{WORKTREE_MOUNT}/{worktree.relative_to(worktree_root)}"


def _token_export(staging_token: str) -> str:
    """The staging_control capability token, as a shell-safe env export.

    Rides in an env var, never a file: anything written under the
    bind-mounted worktree is fair game for `git add -A` and could end up
    committed straight into a merge request.
    """
    if not staging_token:
        return ""
    return f" BLOY_STAGING_TOKEN={shlex.quote(staging_token)}"


#: Turn modes a session can run. ``implement`` writes code; ``analyse`` may
#: only read (the original read-only pass); ``evaluate`` is the independent
#: grader — read plus Bash so it can run the project's tests, but deliberately
#: NO edit tool, so the turn that judges the work cannot quietly fix it and
#: then pass it. See ``features/coding/evaluator.py``.
MODE_IMPLEMENT = "implement"
MODE_ANALYSE = "analyse"
MODE_EVALUATE = "evaluate"


def _prompt_script(prompt: str, path: str = PROMPT_PATH) -> str:
    """Overwrite the prompt file between turns of one session.

    Separate from :func:`_setup_script` because the container is created once
    and then prompted several times (generator, evaluator, retry): only the
    prompt changes, and re-running the whole setup would recreate the user and
    re-push the credentials for no reason. Base64 for the same reason as the
    setup script — a ticket body is human-written text and must never be
    interpolated into a shell command.
    """
    return "\n".join(
        [
            "set -e",
            f"printf '%s' {shlex.quote(_b64(prompt))} | base64 -d > {path}",
            f"chmod 644 {path}",
        ]
    )


def claude_flags(implement: bool, staging: bool = False, mode: str = "") -> str:
    """CLI flags for one turn.

    ``--dangerously-skip-permissions`` is the point of the container: the agent
    must not stop for a confirmation nobody is there to answer, and the mounts
    already bound what it can reach. Analysis mode gets neither that flag nor
    any write tool, so a misconfigured routine cannot edit code by accident.

    ``mode`` (one of :data:`MODE_IMPLEMENT` / :data:`MODE_ANALYSE` /
    :data:`MODE_EVALUATE`) supersedes ``implement`` when given; the boolean is
    kept as the first positional argument because every existing caller and
    test passes it that way. :data:`MODE_EVALUATE` is the one genuinely new
    shape: read tools **plus Bash** (an evaluator that cannot run the test
    suite can only grade by reading, which is exactly the self-report problem
    the evaluator exists to fix) and **no edit tool at all**, so the turn that
    judges the work is structurally unable to repair it and pass itself. Note
    it deliberately omits ``--permission-mode plan``: plan mode would block the
    very Bash calls this mode is granted for.

    ``staging`` appends ``--mcp-config <path> --strict-mcp-config`` — the only
    way that actually works to hand a non-interactive, single-shot ``claude
    -p`` call an MCP server. Confirmed live the wrong way first: `claude -p`
    never auto-discovers a bare ``.mcp.json`` sitting in ``$HOME`` (it only
    checks its own cwd, or entries already inside ``~/.claude.json``) — a real
    run reported no Playwright tool was available at all despite the file
    existing there with valid content. ``--strict-mcp-config`` on top means
    nothing else gets a chance to auto-load either, so this stays the one and
    only source of MCP servers for a staging run.
    """
    effective = mode or (MODE_IMPLEMENT if implement else MODE_ANALYSE)
    if effective == MODE_EVALUATE:
        base = "--allowedTools Read Grep Glob Bash"
    elif effective == MODE_IMPLEMENT:
        base = "--dangerously-skip-permissions"
    else:
        base = "--allowedTools Read Grep Glob --permission-mode plan"
    if not staging:
        return base
    return f"{base} --mcp-config {MCP_CONFIG_PATH} --strict-mcp-config"


def _b64(data: str) -> str:
    return base64.b64encode(data.encode("utf-8")).decode("ascii")


def _filtered_credentials(path: Path | None = None) -> str:
    """Only the Claude subscription login, never the other MCP servers' OAuth.

    ``~/.claude/.credentials.json`` also holds refresh tokens for
    bloy-knowledge, bloy-data, bloy-diagnose and Atlassian's MCP servers —
    none of which this pipeline's sandbox ever talks to. Handing all of that
    to every sandboxed run was strictly more than the run needed.

    ``path`` selects *which* login to filter, so a run can use one of the
    accounts BAM's AI Code Factory provisioned rather than only this host's
    default (see :mod:`bloy_dev_agent.features.claude_accounts`). Omitted, it
    is :data:`HOST_CLAUDE_CREDENTIALS` — the behaviour before accounts were
    selectable, and what keeps this function callable with no arguments.
    """
    source = path if path is not None else HOST_CLAUDE_CREDENTIALS
    if not source.is_file():
        return ""
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    oauth = data.get("claudeAiOauth")
    if not oauth:
        return ""
    return json.dumps({"claudeAiOauth": oauth})


def _filtered_claude_json(text: str) -> str:
    """Drop ``projects`` — where this machine's own MCP server configs (and
    their tokens) live — before a copy of ``~/.claude.json`` reaches a
    container. Everything else in that file is UI/telemetry state the CLI
    tolerates being absent or generic; only ``projects`` carried anything a
    fresh worktree had no business receiving.
    """
    if not text.strip():
        return text
    try:
        data = json.loads(text)
    except ValueError:
        return text
    if not isinstance(data, dict) or "projects" not in data:
        return text
    data.pop("projects", None)
    return json.dumps(data)


#: Deliberately under /tmp, not $AGENT_HOME — confirmed live against the real
#: STAGING_IMAGE that its base already ships a uid-1000 "ubuntu" account, so
#: RESOLVE_AGENT_USER adopts that instead of ever creating "bloy", and its
#: real home is /home/ubuntu. AGENT_HOME (a Python-side guess computed before
#: the container even exists) does not track that, and every other file this
#: script writes into $HOME already reaches it through the shell's own "$h"
#: (resolved at runtime), not a precomputed constant — this path is the one
#: exception, so it lives somewhere no image's user layout can get wrong.
#: Written once by root during setup and made world-readable, since the later
#: `claude -p` (and the Playwright MCP process it spawns) run as the
#: unprivileged agent user, not root. Never the worktree either, for the same
#: reason as skill packs: anything there is swept into the merge request by
#: `git add -A`. Only materialized for a staging-verify run; an ordinary
#: ticket's container never sees it, so build_prompt(staging=None) and this
#: setup script both stay byte-identical to today's behaviour when staging is
#: not requested.
PLAYWRIGHT_LAUNCH_CONFIG_PATH = "/tmp/bloy-playwright-mcp-config.json"

#: Where the ``.mcp.json`` stanza itself lives. Deliberately NOT
#: ``$HOME/.mcp.json`` — confirmed live (a real staging-verify run reported
#: "no Playwright/browser tool available" despite the file existing there
#: with valid content) that `claude -p` never auto-discovers a bare
#: ``.mcp.json`` sitting in ``$HOME``. It only auto-discovers one in its own
#: *working directory* (the ticket's worktree here — never an option, see
#: below) or entries already inside ``~/.claude.json``. The reliable way to
#: hand a non-interactive, single-shot `claude -p` call an MCP server is the
#: explicit ``--mcp-config`` flag (paired with ``--strict-mcp-config`` so
#: nothing else gets a chance to auto-load), which is what actually gets used
#: — this constant only needs to be a path Python and the shell agree on.
MCP_CONFIG_PATH = "/tmp/bloy-mcp.json"

#: Where Playwright MCP saves its own snapshot/trace/console-log files by
#: default: ``.playwright-mcp/`` under its CWD, which for a staging run IS
#: the ticket's own git worktree (``claude -p`` is invoked with ``cd
#: <worktree> && ...``). Confirmed live — the hard way: a real run's
#: `browser_navigate` call wrote ``.playwright-mcp/page-*.yml`` straight into
#: a ticket's worktree, `git add -A` swept it in, and it was pushed as a real
#: merge request containing nothing but that leaked file. Same class of bug
#: as ``.mcp.json`` almost being written into the worktree, same fix: an
#: explicit ``--output-dir`` outside every worktree.
STAGING_MCP_OUTPUT_DIR = "/tmp/bloy-playwright-output"


def _staging_mcp_json(chrome_profile_mounted: bool = False) -> str:
    """The exact stanza this host itself uses for the Playwright MCP server.

    ``--headless`` because the container has no display; ``--no-sandbox``
    because ``~/.sandbox.toml`` already drops ``SYS_ADMIN`` for every
    sandbox, so Chromium's own sandbox cannot initialise without it.
    ``--executable-path`` because @playwright/mcp's default browser channel
    ("chrome" — real Google Chrome) is never installed in this image, only
    the pinned Chromium build at :data:`STAGING_CHROMIUM_EXECUTABLE` is, and
    the unprivileged agent user cannot install it itself. ``--output-dir``
    because its own default output location is the worktree — see
    :data:`STAGING_MCP_OUTPUT_DIR`'s docstring for what that leaked in
    practice.

    ``--user-data-dir`` is added only when a full Chrome profile was actually
    mounted (see SHOPIFY_CHROME_PROFILE_DIRNAME's comment for why this exists
    at all) — this launches Playwright's persistent-context mode instead of a
    fresh incognito-style context, so the container's Chromium presents the
    same aged profile a Cloudflare challenge already cleared for, rather than
    a brand-new fingerprint carrying only replayed cookies.

    ``--headless`` is dropped in that same case, in favour of real headed
    Chromium against the virtual display :data:`XVFB_DISPLAY` (started
    separately, before this MCP server, by whoever runs it — see
    :data:`XVFB_START_COMMAND`'s own comment). Confirmed live: mounting the
    profile alone did not get a real run past Cloudflare's "Just a
    moment…" challenge; `--headless` is Chromium's single most direct tell
    that a browser is automated, and every session that has ever sailed
    through this same store's Cloudflare challenge on this host ran headed.
    Without a profile mounted, headed mode buys nothing (there is no aged
    session to protect), so the cheaper, already-proven ``--headless`` path
    stays the default.
    """
    args = [
        "@playwright/mcp@latest",
        "--no-sandbox",
        "--executable-path",
        STAGING_CHROMIUM_EXECUTABLE,
        "--output-dir",
        STAGING_MCP_OUTPUT_DIR,
        "--config",
        PLAYWRIGHT_LAUNCH_CONFIG_PATH,
    ]
    env: dict[str, str] = {}
    if chrome_profile_mounted:
        args += ["--user-data-dir", SHOPIFY_CHROME_PROFILE_MOUNT]
        env["DISPLAY"] = XVFB_DISPLAY
    else:
        args.append("--headless")
    return json.dumps(
        {
            "mcpServers": {
                "playwright": {
                    "type": "stdio",
                    "command": "npx",
                    "args": args,
                    "env": env,
                }
            }
        }
    )


def _playwright_launch_config(storage_state_mounted: bool) -> str:
    """Chromium launch flags, plus a logged-in session when one is mounted.

    ``--disable-dev-shm-usage`` is the second mandatory flag regardless of
    staging content: Docker's default /dev/shm is 64MB and nothing in the
    sandbox server config raises it, so Chromium must be told to spill into
    /tmp instead or it crashes on the first real page. This is a launch
    option, not a CLI flag of the MCP server itself, hence its own small
    config file rather than another --arg.

    ``contextOptions.storageState`` is added only when
    :func:`_shopify_storage_state_path` actually found a file — confirmed
    against the installed ``@playwright/mcp`` (``--help`` and its
    ``config.d.ts``) to be a real, supported key forwarded straight to
    Playwright's ``BrowserContextOptions.storageState``, not a guess. Its
    absence here (no captured session yet, or one that's expired) is the
    graceful-degradation path: Playwright MCP simply opens a fresh, logged-out
    context, and the agent hits Shopify's login wall and reports that in its
    own answer — no Python-side branch needed for that outcome.
    """
    config: dict = {"browser": {"launchOptions": {"args": ["--disable-dev-shm-usage"]}}}
    if storage_state_mounted:
        config["browser"]["contextOptions"] = {
            "storageState": f"{SHOPIFY_AUTH_MOUNT}/{SHOPIFY_STORAGE_STATE_FILENAME}"
        }
    return json.dumps(config)


def _skill_copy_lines(
    enabled_skills: list[skill_packs.SkillPack],
    skill_packs_root: Path,
    skills_dir: str,
) -> list[str]:
    """Shell lines materialising each enabled pack under ``skills_dir``.

    ``skills_dir`` is a path under ``/worktrees`` — the bind-mounted root, not
    the worktree of any one repo — so it survives the container being killed
    and is inspectable on the host afterwards, the same way agent_team's own
    per-task workspaces are real directories a human can go look at rather
    than something thrown away at the end of a run. It is never inside a
    repo's own worktree: that worktree IS the repo root here, ``.claude/`` is
    not gitignored in either sub-project, and anything written inside it would
    be swept into the merge request by ``git add -A``.
    """
    lines: list[str] = [f"mkdir -p {shlex.quote(skills_dir)}"]
    for pack in enabled_skills:
        if not skill_packs.SAFE_NAME.match(pack.name):
            logger.warning(
                "bloy_dev_agent: skipping skill pack with an unsafe name %r", pack.name
            )
            continue
        try:
            rel = pack.path.relative_to(skill_packs_root).as_posix()
        except ValueError:
            continue
        # The pack name is already checked against SAFE_NAME above, so it is
        # safe to interpolate directly; the source path is quoted because
        # directory names under a shared store are not this service's to trust.
        dest = f'"{skills_dir}/{pack.name}"'
        src = shlex.quote(f"{SKILLS_MOUNT}/{rel}")
        lines.append(f"mkdir -p {dest}")
        lines.append(f"cp -r {src}/. {dest}/ 2>/dev/null || true")
    return lines


def _setup_script(
    prompt: str,
    enabled_skills: list[skill_packs.SkillPack] | None = None,
    skill_packs_root: Path | None = None,
    run_id: str = "",
    staging: bool = False,
    shopify_auth_dir: Path | None = None,
    credentials_path: Path | None = None,
) -> str:
    """Create the agent user, install its Claude login, and drop the prompt in.

    Everything is base64-encoded on the way in. The prompt is an issue body
    written by a human — quotes, backticks and newlines are ordinary content
    there, and interpolating it into a shell command would be both fragile and
    an injection route straight from the ticket tracker.
    """
    claude_json = _filtered_claude_json(
        HOST_CLAUDE_JSON.read_text(encoding="utf-8") if HOST_CLAUDE_JSON.exists() else ""
    )
    credentials = _filtered_credentials(credentials_path)
    skill_lines: list[str] = []
    if enabled_skills and skill_packs_root is not None:
        skills_dir = agent_log.container_skills_dir(WORKTREE_MOUNT, run_id or "adhoc")
        skill_lines = _skill_copy_lines(enabled_skills, skill_packs_root, skills_dir)
        # The whole ``.bloy-skills`` tree, not just this run's leaf: the setup
        # script runs as root, so the shared top directory it implicitly
        # creates on a machine's very first run would otherwise stay
        # root-owned forever, and the host user could never clean it up.
        skills_top = f"{WORKTREE_MOUNT}/{agent_log.SKILLS_DIR_NAME}"
        skill_lines += [
            f"chown -R {AGENT_UID}:{AGENT_GID} {shlex.quote(skills_top)}",
            'rm -rf "$h/.claude/skills" 2>/dev/null || true',
            f'ln -s {shlex.quote(skills_dir)} "$h/.claude/skills"',
        ]
    mcp_lines: list[str] = []
    if staging:
        # Mutually exclusive: a persistent profile already carries its own
        # cookies/storage, so contextOptions.storageState is only ever set in
        # its absence — Playwright does not support layering one on top of
        # the other via launchPersistentContext.
        chrome_profile_mounted = _shopify_chrome_profile_path(shopify_auth_dir) is not None
        storage_state_mounted = (
            not chrome_profile_mounted
            and _shopify_storage_state_path(shopify_auth_dir) is not None
        )
        launch_config = _playwright_launch_config(storage_state_mounted)
        mcp_lines = [
            f"printf '%s' {shlex.quote(_b64(_staging_mcp_json(chrome_profile_mounted)))} "
            f"| base64 -d > {shlex.quote(MCP_CONFIG_PATH)}",
            f"printf '%s' {shlex.quote(_b64(launch_config))} | base64 -d "
            f"> {shlex.quote(PLAYWRIGHT_LAUNCH_CONFIG_PATH)}",
            # Both outside "$h", so the chown -R "$h" below never reaches
            # them — the agent user (not root) is who actually reads them,
            # and `claude -p` reads MCP_CONFIG_PATH via --mcp-config, never
            # by auto-discovering a bare .mcp.json (see MCP_CONFIG_PATH's
            # own docstring for why $HOME never worked for this).
            f"chmod 644 {shlex.quote(MCP_CONFIG_PATH)} "
            f"{shlex.quote(PLAYWRIGHT_LAUNCH_CONFIG_PATH)}",
            f"mkdir -p {shlex.quote(STAGING_MCP_OUTPUT_DIR)}",
            f"chown -R {AGENT_UID}:{AGENT_GID} {shlex.quote(STAGING_MCP_OUTPUT_DIR)}",
        ]
    return "\n".join(
        [
            "set -e",
            # Base images usually already ship a uid-1000 account, and useradd
            # refuses to make a second one. Adopt whoever holds the uid.
            RESOLVE_AGENT_USER,
            'if [ -z "$u" ]; then',
            f"  getent group {AGENT_GID} >/dev/null || groupadd -g {AGENT_GID} {AGENT_USER}",
            f"  useradd -m -u {AGENT_UID} -g {AGENT_GID} -s /bin/bash {AGENT_USER}",
            f'  u={AGENT_USER}',
            "fi",
            'h=$(getent passwd "$u" | cut -d: -f6)',
            '[ -n "$h" ] || h=/home/"$u"',
            'mkdir -p "$h/.claude"',
            f"printf '%s' {shlex.quote(_b64(credentials))} | base64 -d "
            '> "$h/.claude/.credentials.json"',
            'chmod 600 "$h/.claude/.credentials.json"',
            f'printf \'%s\' {shlex.quote(_b64(claude_json))} | base64 -d > "$h/.claude.json"',
            f"printf '%s' {shlex.quote(_b64(prompt))} | base64 -d > {PROMPT_PATH}",
            f"chmod 644 {PROMPT_PATH}",
            *skill_lines,
            *mcp_lines,
            f'chown -R {AGENT_UID}:{AGENT_GID} "$h"',
            'test -s "$h/.claude/.credentials.json" || echo NO_CREDENTIALS',
        ]
    )


def image_for(image: str, staging: bool) -> str:
    """Which container image a run actually gets.

    A staging-verify run needs Chromium pre-installed, so it switches to
    :data:`STAGING_IMAGE` — but only when the caller left the image at the
    default, so an explicit override (a test, a one-off) is never silently
    replaced. Shared by every entry point rather than repeated: the single-shot
    path and the loop path picking different images for the same ticket would be
    a difference nobody could see from the outside.
    """
    return STAGING_IMAGE if staging and image == DEFAULT_IMAGE else image


def auth_dir_for(shopify_auth_dir: Path | None, staging: bool) -> Path | None:
    """The captured Shopify session directory a run should mount, if any.

    Defaults to :data:`SHOPIFY_AUTH_DIR` whenever ``staging`` is true and no
    override is given — there is only ever one such directory on a host, so
    requiring every caller to repeat it would just be a chance to forget it (and
    a staging run that forgot it hits a login wall it cannot pass).
    """
    if shopify_auth_dir is not None:
        return shopify_auth_dir
    return SHOPIFY_AUTH_DIR if staging else None


@dataclass
class TurnResult:
    """What one ``claude -p`` turn produced.

    ``output`` is recovered from the stream log rather than stdout, because
    stdout is where the JSON stream itself goes; see :meth:`SandboxSession.turn`.
    """

    ok: bool
    output: str
    exit_code: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    #: Where this turn's stream-json landed on the host, when it was logged.
    log_path: Path | None = None


class SandboxSession:
    """One container, prepared once, prompted many times.

    Why this exists: the pipeline used to create a container, run a single
    ``claude -p``, and kill it. An unattended loop needs several turns against
    the *same* worktree — a generator, then an independent evaluator that has to
    see the diff the generator just made, then a retry that continues from it —
    and each of those in its own container would mean re-mounting, re-installing
    the login, re-copying the skill packs, and (worse) an evaluator that cannot
    see uncommitted work because it is looking at a different container's view.

    So the container's lifecycle is now the *run's* lifecycle. Everything about
    what it can reach is unchanged: the same mounts, the same egress policy, the
    same unprivileged user, and the SSH key still never enters it (committing
    and pushing stay on the host, in :mod:`bloy_dev_agent.features.workspace`).

    Used as an async context manager so the container is killed on every exit
    path, including an exception mid-loop::

        async with SandboxSession(...) as session:
            first = await session.turn(prompt)
            ...
    """

    def __init__(
        self,
        *,
        worktree: Path,
        worktree_root: Path,
        image: str = DEFAULT_IMAGE,
        timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES,
        run_id: str = "",
        monorepo: Path | None = None,
        enabled_skills: list[skill_packs.SkillPack] | None = None,
        skill_packs_root: Path | None = None,
        staging: bool = False,
        shopify_auth_dir: Path | None = None,
        staging_token: str = "",
        agent_repos_root: Path | None = None,
        claude_config_dir: Path | None = None,
    ) -> None:
        self.worktree = worktree
        self.worktree_root = worktree_root
        self.image = image
        self.timeout_minutes = timeout_minutes
        self.run_id = run_id
        self.monorepo = monorepo
        self.enabled_skills = enabled_skills
        self.skill_packs_root = skill_packs_root
        self.staging = staging
        self.shopify_auth_dir = shopify_auth_dir
        self.staging_token = staging_token
        self.agent_repos_root = agent_repos_root

        #: Which provisioned Claude login this session pushes into the
        #: container. Resolved once, here, rather than per turn: every turn of
        #: one run must use the same account, or the run's own rate-limit
        #: budget and its audit trail both stop meaning anything.
        self.account = (
            claude_accounts.ClaudeAccount(claude_config_dir.name, claude_config_dir)
            if claude_config_dir is not None
            else claude_accounts.resolve(run_id)
        )

        self.sandbox_id = ""
        self.setup_output = ""
        #: Set when the container came up but its setup script failed. The loop
        #: reports this instead of prompting a container that has no login.
        self.setup_error = ""
        self._sandbox = None
        self._chrome_profile: Path | None = None
        self._prompt_written = False

    # -- lifecycle ---------------------------------------------------------

    @property
    def workdir(self) -> str:
        """The primary worktree's path *inside* the container."""
        return container_path(self.worktree, self.worktree_root)

    async def __aenter__(self) -> SandboxSession:
        await self.open()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    async def open(self) -> None:
        """Create the container and run the one-time setup script."""
        from opensandbox import Sandbox

        # Mount the filtered mirror, never the real monorepo — see
        # sync_monorepo_mirror's docstring for why.
        monorepo_mount = (
            sync_monorepo_mirror(self.monorepo) if self.monorepo is not None else None
        )

        self._chrome_profile = (
            _shopify_chrome_profile_path(self.shopify_auth_dir) if self.staging else None
        )
        if self._chrome_profile is not None:
            _clear_stale_chrome_singleton_files(self._chrome_profile)

        self._sandbox = await Sandbox.create(
            self.image,
            timeout=timedelta(minutes=self.timeout_minutes),
            connection_config=_connection(),
            volumes=_volumes(
                self.worktree_root,
                monorepo_mount,
                self.skill_packs_root,
                self.shopify_auth_dir,
                self.agent_repos_root,
            ),
            metadata={"owner": OWNER_TAG, "run_id": self.run_id or "adhoc"},
            network_policy=(
                _network_policy(STAGING_EGRESS_ALLOW) if self.staging else None
            ),
            resource=STAGING_RESOURCE if self.staging else None,
        )
        self.sandbox_id = (
            getattr(self._sandbox, "sandbox_id", "")
            or getattr(self._sandbox, "id", "")
        )
        logger.info(
            "bloy_dev_agent: sandbox %s working in %s", self.sandbox_id, self.workdir
        )

        # The setup script still takes a prompt because it writes PROMPT_PATH in
        # the same pass; an empty one is fine and the first turn overwrites it.
        setup = await self._sandbox.commands.run(
            _setup_script(
                "",
                self.enabled_skills,
                self.skill_packs_root,
                self.run_id,
                self.staging,
                self.shopify_auth_dir,
                self.account.credentials,
            )
        )
        self.setup_output = _text(setup)
        if "NO_CREDENTIALS" in self.setup_output:
            logger.warning(
                "bloy_dev_agent: no Claude credentials visible in the sandbox "
                "(account %r at %s)", self.account.name, self.account.config_dir,
            )
        else:
            logger.info(
                "bloy_dev_agent: sandbox %s using Claude account %r",
                self.sandbox_id, self.account.name,
            )
        if int(getattr(setup, "exit_code", 0) or 0) != 0:
            self.setup_error = f"Sandbox setup failed:\n{self.setup_output}"

    async def close(self) -> None:
        """Kill the container. Never raises — a leaked sandbox must not mask a result."""
        if self._sandbox is None:
            return
        try:
            await self._sandbox.kill()
        except Exception:  # noqa: BLE001
            logger.warning(
                "bloy_dev_agent: could not kill sandbox %s", self.sandbox_id
            )
        finally:
            self._sandbox = None

    # -- raw command surface ----------------------------------------------

    async def run_as_root(self, command: str):
        """Run a shell command as root inside the container."""
        if self._sandbox is None:
            raise SandboxError("sandbox is not open")
        return await self._sandbox.commands.run(command)

    async def run_as_agent(self, command: str, *, cwd: str = "") -> tuple[int, str]:
        """Run a command as the unprivileged agent user; return (exit code, text).

        The same ``su - "$u"`` shape the agent's own turns use, so a verify
        command this service runs sees exactly the environment the agent saw —
        same user, same PATH, same node. A check that only passes as root would
        be a check that never reflected the run.
        """
        node_bin = f"{NVM_MOUNT}/versions/node/{NODE_VERSION}/bin"
        inner = (
            f"export PATH={node_bin}:$PATH CI=true; "
            f"cd {shlex.quote(cwd or self.workdir)} && {command}"
        )
        execution = await self.run_as_root(
            f'{RESOLVE_AGENT_USER}; su - "$u" -c {shlex.quote(inner)}'
        )
        return int(getattr(execution, "exit_code", 0) or 0), _text(execution)

    async def push_file(self, path: str, content: str) -> None:
        """Write ``content`` to ``path`` inside the container, world-readable.

        Base64 on the way in for the same reason as the prompt: this carries
        command output and JSON, neither of which survives being interpolated
        into a shell command. World-readable because the agent user, not root,
        is who reads it back.
        """
        await self.run_as_root(
            "\n".join(
                [
                    "set -e",
                    f"printf '%s' {shlex.quote(_b64(content))} | base64 -d "
                    f"> {shlex.quote(path)}",
                    f"chmod 644 {shlex.quote(path)}",
                ]
            )
        )

    async def read_file(self, path: str, *, limit: int = 200_000) -> str:
        """Read a file back out of the container, or ``""`` when absent."""
        execution = await self.run_as_root(
            f"head -c {int(limit)} {shlex.quote(path)} 2>/dev/null || true"
        )
        return _text(execution)

    # -- prompting ---------------------------------------------------------

    async def turn(
        self,
        prompt: str,
        *,
        mode: str = MODE_IMPLEMENT,
        log_path: Path | None = None,
        append: bool = False,
    ) -> TurnResult:
        """Run one ``claude -p`` turn against this container's worktree.

        ``log_path`` is a host path under the bind-mounted worktree root, so the
        stream is readable *while* the turn runs — that is what the run detail
        page tails. ``append`` keeps several turns of one run in a single log
        (stream-json is one JSON object per line, so appending is well-formed,
        and :func:`agent_log.final_text` scans backwards and therefore returns
        the newest turn's answer).

        stdout carries the JSON stream and must stay clean, so stderr goes to
        its own file rather than being merged into it.
        """
        if self._sandbox is None:
            raise SandboxError("sandbox is not open")

        await self.run_as_root(_prompt_script(prompt))
        self._prompt_written = True

        redirect = ""
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if not append or not log_path.exists():
                log_path.write_text("", encoding="utf-8")
            in_container = _container_path_for(log_path, self.worktree_root)
            operator = ">>" if append else ">"
            redirect = (
                f" --output-format stream-json --verbose"
                f" {operator} {shlex.quote(in_container)} 2> {shlex.quote(STDERR_PATH)}"
            )

        flags = claude_flags(mode == MODE_IMPLEMENT, staging=self.staging, mode=mode)
        # Xvfb is started here, not in the setup script: it must still be
        # running by the time `claude -p` spawns the Playwright MCP server as a
        # child of *this* shell, and each `commands.run` is its own shell — a
        # background process from an earlier call would not survive into this
        # one. Started per turn for the same reason.
        node_bin = f"{NVM_MOUNT}/versions/node/{NODE_VERSION}/bin"
        inner = (
            f"export PATH={node_bin}:$PATH CI=true"
            f"{_token_export(self.staging_token)}; "
            f"{_xvfb_prefix(self._chrome_profile is not None)}"
            f"cd {shlex.quote(self.workdir)} && "
            f"cat {PROMPT_PATH} | claude -p {flags}{redirect}"
        )
        execution = await self.run_as_root(
            f'{RESOLVE_AGENT_USER}; su - "$u" -c {shlex.quote(inner)}'
        )
        exit_code = int(getattr(execution, "exit_code", 0) or 0)

        if log_path is None:
            return TurnResult(exit_code == 0, _text(execution), exit_code)

        # The stream went to the log, so stdout is empty by design; the answer
        # and any failure text have to be recovered from the file and stderr.
        output = agent_log.final_text(log_path)
        if not output:
            stderr = await self.run_as_root(
                f"tail -c 2000 {STDERR_PATH} 2>/dev/null"
            )
            output = _text(stderr) or _text(execution)
        usage = agent_log.last_usage(log_path)
        return TurnResult(
            exit_code == 0,
            output,
            exit_code,
            tokens=usage.get("total_tokens", 0),
            cost_usd=usage.get("cost_usd", 0.0),
            log_path=log_path,
        )


def _container_path_for(host_path: Path, worktree_root: Path) -> str:
    """Where a host path under the worktree root appears inside the container.

    Only the worktree root is bind-mounted, so a path outside it has no
    container-side equivalent and that is a programming error, not a runtime
    condition — hence the explicit raise rather than a silent fallback that
    would send a run's log into a directory nobody ever reads.
    """
    try:
        relative = host_path.relative_to(worktree_root)
    except ValueError as exc:
        raise SandboxError(
            f"{host_path} is outside the mounted worktree root {worktree_root}"
        ) from exc
    return f"{WORKTREE_MOUNT}/{relative.as_posix()}"


async def _run_async(
    *,
    prompt: str,
    worktree: Path,
    worktree_root: Path,
    image: str,
    timeout_minutes: int,
    implement: bool,
    run_id: str = "",
    monorepo: Path | None = None,
    enabled_skills: list[skill_packs.SkillPack] | None = None,
    skill_packs_root: Path | None = None,
    staging: bool = False,
    shopify_auth_dir: Path | None = None,
    staging_token: str = "",
    agent_repos_root: Path | None = None,
) -> SandboxResult:
    """One prompt, one container — the original single-turn path.

    Kept as a thin wrapper over :class:`SandboxSession` rather than a second
    implementation: the shell assembly, the mounts and the credential filtering
    are the parts that actually break in production, and having two copies of
    them is how they drift apart.
    """
    async with SandboxSession(
        worktree=worktree,
        worktree_root=worktree_root,
        image=image,
        timeout_minutes=timeout_minutes,
        run_id=run_id,
        monorepo=monorepo,
        enabled_skills=enabled_skills,
        skill_packs_root=skill_packs_root,
        staging=staging,
        shopify_auth_dir=shopify_auth_dir,
        staging_token=staging_token,
        agent_repos_root=agent_repos_root,
    ) as session:
        if session.setup_error:
            return SandboxResult(False, session.setup_error, session.sandbox_id, 1)
        log_path = (
            agent_log.host_log_path(worktree_root, run_id) if run_id else None
        )
        turn = await session.turn(
            prompt,
            mode=MODE_IMPLEMENT if implement else MODE_ANALYSE,
            log_path=log_path,
        )
        return SandboxResult(
            turn.ok, turn.output, session.sandbox_id, turn.exit_code
        )


def _run_coroutine(factory):
    """Drive an async call from a synchronous caller, loop or no loop.

    BAM's routine scheduler invokes actions *directly on its own event loop*
    (``actions.execute_routine_actions`` calls ``action.run(context)`` and only
    awaits the result if it is awaitable). A plain ``asyncio.run`` therefore
    raises "cannot be called from a running event loop" — which is exactly how
    the first live run failed. Handing the coroutine a thread of its own keeps
    this action synchronous without borrowing, or blocking, BAM's loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())

    box: dict[str, object] = {}

    def worker() -> None:
        try:
            box["value"] = asyncio.run(factory())
        except BaseException as exc:  # noqa: BLE001 — re-raised on the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=worker, name="bloy-sandbox", daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box.get("value")


def run_in_sandbox(
    prompt: str,
    worktree: Path,
    *,
    worktree_root: Path,
    image: str = DEFAULT_IMAGE,
    timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES,
    implement: bool = True,
    run_id: str = "",
    monorepo: Path | None = None,
    enabled_skills: list[skill_packs.SkillPack] | None = None,
    skill_packs_root: Path | None = None,
    staging: bool = False,
    shopify_auth_dir: Path | None = None,
    staging_token: str = "",
    agent_repos_root: Path | None = None,
) -> SandboxResult:
    """Run one prompt against ``worktree`` inside a fresh sandbox.

    ``staging=True`` switches to :data:`STAGING_IMAGE` (Chromium pre-installed),
    :data:`STAGING_RESOURCE` (more CPU/RAM for a real build), and a network
    policy that denies everything except :data:`STAGING_EGRESS_ALLOW` — an
    ordinary ticket never sets it, so its sandbox's network stays exactly as
    open (or closed) as it is today.

    ``shopify_auth_dir`` defaults to :data:`SHOPIFY_AUTH_DIR` whenever
    ``staging`` is true and no override is given — there is only ever one such
    directory on a host, so requiring every caller to repeat it would just be
    a chance to forget it. Pass an explicit path (e.g. in tests) to override.
    """
    effective_image = image_for(image, staging)
    effective_auth_dir = auth_dir_for(shopify_auth_dir, staging)
    try:
        return _run_coroutine(
            lambda: _run_async(
                prompt=prompt,
                worktree=worktree,
                worktree_root=worktree_root,
                image=effective_image,
                timeout_minutes=timeout_minutes,
                implement=implement,
                run_id=run_id,
                monorepo=monorepo,
                enabled_skills=enabled_skills,
                skill_packs_root=skill_packs_root,
                staging=staging,
                shopify_auth_dir=effective_auth_dir,
                staging_token=staging_token,
                agent_repos_root=agent_repos_root,
            )
        )
    except SandboxError as exc:
        return SandboxResult(False, str(exc))
    except Exception as exc:  # noqa: BLE001 — report, never crash the routine
        logger.exception("bloy_dev_agent: sandbox run failed")
        return SandboxResult(False, f"Sandbox run failed: {exc}")


# ---------------------------------------------------------------------------
# Orphan sweep
# ---------------------------------------------------------------------------
#
# A sandbox outlives the process that made it. Kill the service mid-run and the
# container keeps burning CPU and a worktree lock until its own timeout expires
# — during development that meant reaching for `docker rm` by hand, repeatedly.
#
# The SDK exposes no list-by-id, so this talks to the server's REST surface.
# Borrowed in shape from agent_team's sandbox GC, which solved the same problem
# first; this version is smaller because a run here owns its sandbox for one
# pass rather than for a whole task's lifetime.


def _server_base() -> tuple[str, str]:
    """``(base_url, api_key)`` for the admin REST surface."""
    config = tomllib.loads(SANDBOX_CONFIG.read_text(encoding="utf-8"))
    server = config.get("server") or {}
    host = str(server.get("host") or "127.0.0.1")
    port = str(server.get("port") or "8080")
    return f"http://{host}:{port}", str(server.get("api_key") or "").strip()


def list_sandboxes() -> list[dict]:
    """Every sandbox the server currently holds, ours or not."""
    import httpx

    base, key = _server_base()
    try:
        response = httpx.get(
            f"{base}/v1/sandboxes",
            headers={API_KEY_HEADER: key},
            params={"pageSize": 100},
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:  # noqa: BLE001 — a sweep must never break the caller
        logger.exception("bloy_dev_agent: could not list sandboxes")
        return []
    items = payload.get("items") if isinstance(payload, dict) else payload
    return [item for item in (items or []) if isinstance(item, dict)]


def kill_sandbox(sandbox_id: str) -> bool:
    import httpx

    base, key = _server_base()
    try:
        response = httpx.delete(
            f"{base}/v1/sandboxes/{sandbox_id}",
            headers={API_KEY_HEADER: key},
            timeout=15.0,
        )
    except Exception:  # noqa: BLE001
        logger.exception("bloy_dev_agent: could not kill sandbox %s", sandbox_id)
        return False
    return response.status_code in (200, 202, 204, 404)


def _owner_of(item: dict) -> tuple[str, str]:
    """``(owner, run_id)`` from a sandbox's metadata, tolerating shape drift."""
    meta = item.get("metadata") or item.get("Metadata") or {}
    if not isinstance(meta, dict):
        return "", ""
    return str(meta.get("owner") or ""), str(meta.get("run_id") or "")


def reap_orphan_sandboxes(active_run_ids: set[str]) -> list[str]:
    """Kill our sandboxes whose run is no longer in flight.

    Only sandboxes carrying :data:`OWNER_TAG` are touched — the server may be
    shared, and killing a stranger's container would be far worse than leaving
    one of ours running a few minutes longer.
    """
    killed: list[str] = []
    for item in list_sandboxes():
        sandbox_id = str(item.get("id") or item.get("sandboxId") or "")
        owner, run_id = _owner_of(item)
        if not sandbox_id or owner != OWNER_TAG:
            continue
        if run_id and run_id in active_run_ids:
            continue
        if kill_sandbox(sandbox_id):
            killed.append(sandbox_id)
            logger.warning(
                "bloy_dev_agent: đã dọn sandbox mồ côi %s (run %s)",
                sandbox_id,
                run_id or "?",
            )
    return killed
