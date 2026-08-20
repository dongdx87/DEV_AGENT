"""The four operations this service exposes — nothing else, by construction.

Every ``subprocess.run`` call here takes a Python list, never ``shell=True``:
no string built from a caller's input is ever handed to a shell to interpret.
The only caller-supplied value in this whole module is an ``app`` key looked
up in :data:`bloy_dev_agent.staging_control.apps.STAGING_APPS` — everything
else (paths, argv, process names) comes from that fixed table.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from bloy_dev_agent.staging_control.apps import RSYNC_EXCLUDES, StagingApp
from bloy_dev_agent.staging_control.tokens import StagingGrant

logger = logging.getLogger(__name__)

BUILD_TIMEOUT_S = 600
RESTART_TIMEOUT_S = 60

#: One deploy at a time, full stop — a concurrent request gets refused, never
#: queued. Same rationale as bloy_dev_agent.service.PassRunner.try_start: a
#: shared staging deployment cannot serve two half-applied deploys at once.
_DEPLOY_LOCK = threading.Lock()


@dataclass
class ActionResult:
    ok: bool
    detail: str
    log_tail: str = ""
    seconds: float = 0.0


#: PM2 manages this whole service via a Node-style IPC channel — even though
#: it is a Python process, PM2 still sets these two so it can talk to a
#: process it thinks is Node. Left in a build subprocess's environment, a
#: child `node` (via pnpm/webpack) reads `NODE_CHANNEL_FD` at startup and
#: tries to use that fd as its OWN IPC channel; fd 3 in the grandchild is
#: never what PM2 meant it to be, and Node aborts (SIGABRT, not a clean
#: nonzero exit) trying to use it. Found live: the extensions build failed
#: identically on every real deploy through this service while the exact
#: same command, argv and env succeeded every time run by hand — the one
#: difference was this pair, only ever present under PM2.
_PM2_IPC_ENV_VARS = ("NODE_CHANNEL_FD", "NODE_CHANNEL_SERIALIZATION_MODE")


def _run(
    argv: list[str], *, cwd: Path, timeout: int, extra_env: dict[str, str] | None = None
) -> tuple[bool, str]:
    env = {**os.environ, **extra_env} if extra_env else dict(os.environ)
    for key in _PM2_IPC_ENV_VARS:
        env.pop(key, None)
    try:
        result = subprocess.run(
            argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=env
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    output = f"{result.stdout}\n{result.stderr}".strip()
    return result.returncode == 0, output


def _rsync_worktree_to_checkout(worktree: Path, checkout: Path) -> tuple[bool, str]:
    if not worktree.is_dir():
        return False, f"worktree không tồn tại: {worktree}"
    checkout.mkdir(parents=True, exist_ok=True)
    if shutil.which("rsync") is None:
        return False, "rsync không có trên máy này"
    argv = ["rsync", "-a", "--delete"]
    for pattern in RSYNC_EXCLUDES:
        argv += ["--exclude", pattern]
    argv += [f"{worktree}/", f"{checkout}/"]
    ok, output = _run(argv, cwd=worktree, timeout=BUILD_TIMEOUT_S)
    return ok, output[:2000]


def _wait_healthy(app: StagingApp, timeout_s: int) -> bool:
    kind = app.health_check[0]
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if kind == "tcp":
                _, host, port = app.health_check
                with __import__("socket").create_connection((host, int(port)), timeout=3):
                    return True
            else:  # "http"
                _, url = app.health_check
                response = httpx.get(url, timeout=3.0)
                if response.status_code < 500:
                    return True
        except Exception:  # noqa: BLE001 — any failure just means "not ready yet"
            pass
        time.sleep(2)
    return False


def deploy(app: StagingApp, grant: StagingGrant) -> ActionResult:
    """Rsync this run's worktree onto ``app``'s checkout, build, restart, wait."""
    worktree = grant.worktrees.get(app.repo)
    if worktree is None:
        return ActionResult(
            False, f"run {grant.run_id} không có worktree cho repo {app.repo!r}"
        )
    if not _DEPLOY_LOCK.acquire(blocking=False):
        return ActionResult(False, "một deploy khác đang chạy, thử lại sau")

    started = time.monotonic()
    try:
        ok, detail = _rsync_worktree_to_checkout(worktree, app.checkout)
        if not ok:
            return ActionResult(False, f"rsync thất bại: {detail}")

        for argv in app.build:
            # pnpm's own "remove node_modules and reinstall" step refuses to
            # run without a TTY unless CI=true — hit live on the first real
            # cms deploy (`bloy-extensions run build-bloy`'s `pnpm install`
            # step), matching the same fix already needed for the developer's
            # own personal setup-pack build command.
            ok, detail = _run(
                argv, cwd=app.checkout, timeout=BUILD_TIMEOUT_S, extra_env={"CI": "true"}
            )
            logger.warning("DEBUG build step %r ok=%s FULL=%r", argv, ok, detail)
            if not ok:
                return ActionResult(False, f"build thất bại ({' '.join(argv)}): {detail[-1500:]}")

        ok, detail = _run(list(app.restart), cwd=app.checkout, timeout=RESTART_TIMEOUT_S)
        if not ok:
            return ActionResult(False, f"restart thất bại: {detail[-1000:]}")

        healthy = _wait_healthy(app, app.ready_timeout_s)
        seconds = time.monotonic() - started
        if not healthy:
            return ActionResult(
                False,
                f"đã restart nhưng health check không pass sau {app.ready_timeout_s}s",
                seconds=seconds,
            )
        return ActionResult(True, "deploy thành công", seconds=seconds)
    finally:
        _DEPLOY_LOCK.release()


def restart(app: StagingApp) -> ActionResult:
    started = time.monotonic()
    ok, detail = _run(list(app.restart), cwd=app.checkout, timeout=RESTART_TIMEOUT_S)
    if not ok:
        return ActionResult(False, f"restart thất bại: {detail[-1000:]}")
    healthy = _wait_healthy(app, app.ready_timeout_s)
    seconds = time.monotonic() - started
    if not healthy:
        return ActionResult(
            False, f"đã restart nhưng health check không pass sau {app.ready_timeout_s}s",
            seconds=seconds,
        )
    return ActionResult(True, "restart thành công", seconds=seconds)


def status(apps: list[StagingApp]) -> list[dict]:
    out = []
    for app in apps:
        healthy = _wait_healthy(app, timeout_s=1)
        out.append({"key": app.key, "healthy": healthy, "pm2_processes": list(app.pm2_processes)})
    return out


#: Strips ANSI SGR sequences (``\x1b[31m`` etc). PM2 colours its version-banner
#: line whenever it decides the output is going to something other than a
#: plain non-interactive pipe — observed here to depend on context in a way
#: an interactive shell test does not reproduce, so it cannot be assumed away.
#: The reason this matters: a colour code's own "[" (as in "\x1b[31m") is a
#: real "[" character, and a naive ``str.find("[")`` for "where does the JSON
#: start" can match one of *those* instead of the array's actual opening
#: bracket — silently producing a JSONDecodeError a few characters in, or (if
#: the truncated slice happens to still parse) an empty or wrong result.
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _pm2_out_log_paths() -> dict[str, str]:
    """``{process_name: out_log_path}`` straight from PM2 itself.

    PM2 names log files ``<name>-out-<pm_id>.log`` — the numeric id is not
    guessable from the process name alone, and it changes across restarts —
    so the actual path has to come from ``pm2 jlist``, not be assembled here.
    A version-mismatch banner is sometimes printed ahead of the JSON on this
    exact command (it once broke BAM's own routine reconciler), so the ANSI
    codes are stripped and the output is trimmed to its first ``[{`` —
    specifically two characters, not one, since a bare ``[`` can also open a
    colour code.
    """
    try:
        result = subprocess.run(
            ["pm2", "jlist"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("bloy_dev_agent: pm2 jlist raised %s: %s", type(exc).__name__, exc)
        return {}
    if result.returncode != 0:
        logger.warning(
            "bloy_dev_agent: pm2 jlist exit %s, stderr=%r",
            result.returncode, (result.stderr or "")[:500],
        )
        return {}
    raw = _ANSI_ESCAPE.sub("", result.stdout)
    start = raw.find("[{")
    if start == -1:
        logger.warning(
            "bloy_dev_agent: pm2 jlist stdout had no '[{' — len=%d head=%r",
            len(raw), raw[:300],
        )
        return {}
    try:
        import json

        processes = json.loads(raw[start:])
    except ValueError as exc:
        logger.warning("bloy_dev_agent: pm2 jlist JSON did not parse: %s", exc)
        return {}
    return {
        str(p.get("name")): str((p.get("pm2_env") or {}).get("pm_out_log_path") or "")
        for p in processes
        if isinstance(p, dict) and p.get("name")
    }


def logs(app: StagingApp, lines: int) -> list[str]:
    lines = max(1, min(lines, 500))
    log_paths = _pm2_out_log_paths()
    collected: list[str] = []
    for name in app.pm2_processes:
        raw_path = log_paths.get(name, "")
        log_path = Path(raw_path) if raw_path else None
        if log_path is None or not log_path.is_file():
            continue
        try:
            content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        collected += [f"[{name}] {line}" for line in content[-lines:]]
    return collected[-lines:]
