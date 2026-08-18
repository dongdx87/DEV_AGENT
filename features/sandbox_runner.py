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
    /host-claude    <- host ~/.claude       (read-only, the subscription login)

``~/.claude`` is copied to the container's own HOME on entry rather than
mounted writable: the CLI rewrites files there during a session, and those
writes have no business reaching the login this machine uses interactively.
``~/.claude.json`` sits *beside* that directory rather than inside it, so it is
pushed in separately — without it the CLI reports "configuration file not
found" and exits before doing any work.

The agent runs as an unprivileged user inside the container, not root, for two
independent reasons: the CLI refuses ``--dangerously-skip-permissions`` under
root outright, and files it creates in the bind-mounted worktree must come out
owned by the host user rather than by root.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import shlex
import threading
import tomllib
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from bloy_dev_agent.features import agent_log

logger = logging.getLogger(__name__)

DEFAULT_IMAGE = "opensandbox/code-interpreter:v1.0.2"
DEFAULT_TIMEOUT_MINUTES = 30

WORKTREE_MOUNT = "/worktrees"
NVM_MOUNT = "/opt/nvm"
CLAUDE_MOUNT = "/host-claude"
#: Monorepo root, read-only, so the agent can read CLAUDE.md and sibling projects.
MONOREPO_MOUNT = "/monorepo"

#: Node release that carries the Claude CLI on this host. Pinned rather than
#: globbed because the container must not silently pick up a different one.
NODE_VERSION = "v20.20.2"

SANDBOX_CONFIG = Path.home() / ".sandbox.toml"
HOST_NVM = Path.home() / ".nvm"
HOST_CLAUDE = Path.home() / ".claude"
HOST_CLAUDE_JSON = Path.home() / ".claude.json"

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


def _volumes(worktree_root: Path, monorepo: Path | None = None):
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
        Volume(
            name="claude-config",
            host=Host(path=str(HOST_CLAUDE)),
            mount_path=CLAUDE_MOUNT,
            read_only=True,
        ),
    ]

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


def claude_flags(implement: bool) -> str:
    """CLI flags for the run.

    ``--dangerously-skip-permissions`` is the point of the container: the agent
    must not stop for a confirmation nobody is there to answer, and the mounts
    already bound what it can reach. Analysis mode gets neither that flag nor
    any write tool, so a misconfigured routine cannot edit code by accident.
    """
    if implement:
        return "--dangerously-skip-permissions"
    return "--allowedTools Read Grep Glob --permission-mode plan"


def _b64(data: str) -> str:
    return base64.b64encode(data.encode("utf-8")).decode("ascii")


def _setup_script(prompt: str) -> str:
    """Create the agent user, install its Claude login, and drop the prompt in.

    Everything is base64-encoded on the way in. The prompt is an issue body
    written by a human — quotes, backticks and newlines are ordinary content
    there, and interpolating it into a shell command would be both fragile and
    an injection route straight from the ticket tracker.
    """
    claude_json = HOST_CLAUDE_JSON.read_text(encoding="utf-8") if HOST_CLAUDE_JSON.exists() else ""
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
            f'cp -r {CLAUDE_MOUNT}/. "$h/.claude/" 2>/dev/null || true',
            f'printf \'%s\' {shlex.quote(_b64(claude_json))} | base64 -d > "$h/.claude.json"',
            f"printf '%s' {shlex.quote(_b64(prompt))} | base64 -d > {PROMPT_PATH}",
            f"chmod 644 {PROMPT_PATH}",
            f'chown -R {AGENT_UID}:{AGENT_GID} "$h"',
            'test -s "$h/.claude.json" || echo NO_CREDENTIALS',
        ]
    )


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
) -> SandboxResult:
    from opensandbox import Sandbox

    workdir = container_path(worktree, worktree_root)
    node_bin = f"{NVM_MOUNT}/versions/node/{NODE_VERSION}/bin"

    # With a run id the agent streams its reasoning to a file on the shared
    # volume, so the admin page can follow along while the container works.
    host_log = agent_log.host_log_path(worktree_root, run_id) if run_id else None
    if host_log is not None:
        host_log.parent.mkdir(parents=True, exist_ok=True)
        host_log.write_text("", encoding="utf-8")

    sandbox = await Sandbox.create(
        image,
        timeout=timedelta(minutes=timeout_minutes),
        connection_config=_connection(),
        volumes=_volumes(worktree_root, monorepo),
        metadata={"owner": OWNER_TAG, "run_id": run_id or "adhoc"},
    )
    sandbox_id = getattr(sandbox, "sandbox_id", "") or getattr(sandbox, "id", "")
    logger.info("bloy_dev_agent: sandbox %s working in %s", sandbox_id, workdir)

    try:
        setup = await sandbox.commands.run(_setup_script(prompt))
        setup_output = _text(setup)
        if "NO_CREDENTIALS" in setup_output:
            logger.warning("bloy_dev_agent: no Claude credentials visible in the sandbox")
        if int(getattr(setup, "exit_code", 0) or 0) != 0:
            return SandboxResult(
                False, f"Sandbox setup failed:\n{setup_output}", sandbox_id, 1
            )

        flags = claude_flags(implement)
        redirect = ""
        if run_id:
            log_in_container = agent_log.container_log_path(WORKTREE_MOUNT, run_id)
            # stdout carries the JSON stream and must stay clean, so stderr goes
            # to its own file rather than being merged into it.
            redirect = (
                f" --output-format stream-json --verbose"
                f" > {shlex.quote(log_in_container)} 2> {shlex.quote(STDERR_PATH)}"
            )
        inner = (
            f"export PATH={node_bin}:$PATH CI=true; "
            f"cd {shlex.quote(workdir)} && "
            f"cat {PROMPT_PATH} | claude -p {flags}{redirect}"
        )
        execution = await sandbox.commands.run(
            f'{RESOLVE_AGENT_USER}; su - "$u" -c {shlex.quote(inner)}'
        )
        exit_code = int(getattr(execution, "exit_code", 0) or 0)

        if host_log is None:
            return SandboxResult(exit_code == 0, _text(execution), sandbox_id, exit_code)

        # The stream went to the log, so stdout is empty by design; the answer
        # and any failure text have to be recovered from the file and stderr.
        output = agent_log.final_text(host_log)
        if not output:
            stderr = await sandbox.commands.run(f"tail -c 2000 {STDERR_PATH} 2>/dev/null")
            output = _text(stderr) or _text(execution)
        return SandboxResult(exit_code == 0, output, sandbox_id, exit_code)
    finally:
        try:
            await sandbox.kill()
        except Exception:  # noqa: BLE001 — a leaked sandbox must not mask the result
            logger.warning("bloy_dev_agent: could not kill sandbox %s", sandbox_id)


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
) -> SandboxResult:
    """Run one prompt against ``worktree`` inside a fresh sandbox."""
    try:
        return _run_coroutine(
            lambda: _run_async(
                prompt=prompt,
                worktree=worktree,
                worktree_root=worktree_root,
                image=image,
                timeout_minutes=timeout_minutes,
                implement=implement,
                run_id=run_id,
                monorepo=monorepo,
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
