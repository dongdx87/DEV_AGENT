"""Environment checks for the standalone BLOY Dev Agent service.

The service rests on assumptions about the host: that its own tables exist,
that a Claude CLI is reachable, that the sandbox server answers, that the
worktree root is writable, and that Twenty responds to its key. Rather than
trusting the design document, each question is answered against the running
process and rendered on a page.

Checks that used to look for BAM plugins are gone. In a separate process
``ai_code``, the routine scheduler and the BAM agent row are simply not
reachable, so reporting on them said nothing about whether a run would work.
BAM is now checked as one optional integration among others.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class Check:
    """One environment assertion and what we found."""

    key: str
    label: str
    state: str
    detail: str
    why: str = ""


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_database() -> Check:
    from sqlalchemy import inspect

    from bloy_dev_agent.db import database_url, engine
    from bloy_dev_agent.models import BloyPipelineRun, BloySetting

    wanted = {BloyPipelineRun.__tablename__, BloySetting.__tablename__}
    try:
        found = set(inspect(engine).get_table_names())
    except Exception as exc:  # noqa: BLE001
        return Check(
            key="database",
            label="Database riêng",
            state=FAIL,
            detail=f"Không đọc được: {type(exc).__name__}: {exc}",
            why="Service tự giữ DB của nó, không dùng chung với BAM.",
        )

    missing = sorted(wanted - found)
    return Check(
        key="database",
        label="Database riêng",
        state=OK if not missing else FAIL,
        detail=(
            f"{database_url()}"
            if not missing
            else f"Thiếu bảng: {', '.join(missing)}"
        ),
        why="" if not missing else "init_db() chạy lúc boot — xem log khởi động.",
    )


def find_claude_binary() -> str:
    """Locate the Claude CLI on the host that mounts it into the sandbox."""
    configured = os.environ.get("BLOY_CLAUDE_BIN", "").strip()
    if configured and Path(configured).exists():
        return configured
    found = shutil.which("claude")
    if found:
        return found
    # nvm installs it per node version and never puts it on a service's PATH.
    matches = sorted(Path.home().glob(".nvm/versions/node/*/bin/claude"))
    return str(matches[-1]) if matches else ""


def find_claude_mount_dirs() -> list[Path]:
    """Host directories that must be bind-mounted into the sandbox for
    ``claude`` to run — the single place both
    ``sandbox_runner._resolve_claude_bin_dirs()`` (what actually gets
    mounted, at container paths of ITS OWN choosing — see
    ``sandbox_runner.CLAUDE_BIN_MOUNT_PREFIX``, not these hosts paths
    verbatim) and ``setup_wizard.required_host_paths()`` (what
    ``~/.sandbox.toml``'s allowlist must include, which only cares about the
    HOST side of a mount) resolve this from.

    Anthropic's native installer (``curl -fsSL https://claude.ai/install.sh |
    bash``) leaves ``claude`` on PATH as a symlink — e.g.
    ``~/.local/bin/claude`` pointing at an ABSOLUTE host path in a completely
    different tree, ``~/.local/share/claude/versions/<ver>``. That directory
    holds the real executable under a version NUMBER, not a file named
    ``claude``, so both directories are needed regardless of where either
    ends up mounted: the symlink's own directory (which may hold OTHER
    binaries a shebang script needs, e.g. ``node`` for an nvm install) and,
    only when it points outside that directory, the resolved target's own
    directory. Preserving the host's absolute path for the mount was tried
    first and abandoned: the native installer lives under ``$HOME``, which on
    the production host is ``/root`` — a directory every base image ships
    mode 700, so the unprivileged agent user could not even traverse into it,
    confirmed against the real sandbox image. ``sandbox_runner`` mounts these
    at container paths of its own choosing instead and re-creates the
    ``claude`` name itself (see ``_claude_shim_lines``) — this function only
    needs to return the right HOST directories, in an order that puts the
    directory actually holding a file/symlink named ``claude`` first (some
    caller may still want that one specifically, e.g. for sibling binaries
    like ``node``).
    """
    binary = find_claude_binary()
    if not binary:
        return []
    path = Path(binary)
    dirs = [path.parent]
    resolved_parent = path.resolve().parent
    if resolved_parent != path.parent:
        dirs.append(resolved_parent)
    return dirs


def find_claude_executable() -> Path | None:
    """The real file behind ``claude``, symlinks followed all the way.

    ``find_claude_binary()`` may hand back a symlink whose target is an
    ABSOLUTE host path (Anthropic's native installer does exactly that), which
    cannot be followed from inside a container unless that same absolute path
    is mounted there too. Mounting host paths verbatim turned out to be a dead
    end — the native installer lives under ``$HOME``, and on the production
    host that is ``/root``, which every container image ships as mode 700, so
    the unprivileged agent user cannot even traverse into it. So the sandbox
    mounts the RESOLVED file's directory at a path of its own choosing and
    re-creates the ``claude`` name there itself; this is the function that
    tells it which file to point at. Its ``.name`` is typically a version
    number, not "claude" — that is the whole reason the symlink has to be
    re-created rather than the directory simply put on PATH.
    """
    binary = find_claude_binary()
    return Path(binary).resolve() if binary else None


def _check_claude() -> Check:
    binary = find_claude_binary()
    return Check(
        key="claude_cli",
        label="Claude CLI",
        state=OK if binary else FAIL,
        detail=binary or "Không tìm thấy trên PATH hay dưới nvm.",
        why=(
            ""
            if binary
            else "Đặt BLOY_CLAUDE_BIN. Sandbox mount thư mục nvm này vào container."
        ),
    )


def _check_claude_login() -> Check:
    """The container copies this login in; without it the CLI exits at once."""
    config = Path.home() / ".claude.json"
    creds = Path.home() / ".claude" / ".credentials.json"
    if config.exists() and config.stat().st_size > 0:
        return Check(
            key="claude_login",
            label="Claude login",
            state=OK,
            detail=f"{config} ({config.stat().st_size // 1024} KB)",
        )
    return Check(
        key="claude_login",
        label="Claude login",
        state=FAIL,
        detail=f"{config} không tồn tại hoặc rỗng.",
        why=(
            "File này nằm CẠNH ~/.claude chứ không nằm trong, nên phải đẩy riêng "
            "vào sandbox. Thiếu nó thì CLI báo 'configuration file not found'."
            + ("" if creds.exists() else " Chạy `claude` một lần để đăng nhập.")
        ),
    )


def _check_sandbox_server() -> Check:
    from bloy_dev_agent.features import sandbox_runner

    config = sandbox_runner.SANDBOX_CONFIG
    if not config.exists():
        return Check(
            key="sandbox",
            label="Sandbox server",
            state=FAIL,
            detail=f"Không có {config}",
            why="OpenSandbox đọc file này để biết host, port và api_key.",
        )

    import tomllib

    import httpx

    try:
        server = (tomllib.loads(config.read_text(encoding="utf-8")).get("server") or {})
        host = str(server.get("host") or "127.0.0.1")
        port = str(server.get("port") or "8080")
        if not str(server.get("api_key") or "").strip():
            return Check(
                key="sandbox",
                label="Sandbox server",
                state=FAIL,
                detail="server.api_key đang rỗng.",
                why="Chạy non-interactive mà thiếu key thì server tự thoát.",
            )
        httpx.get(f"http://{host}:{port}/", timeout=3.0)
    except Exception as exc:  # noqa: BLE001 — any failure means "not usable"
        return Check(
            key="sandbox",
            label="Sandbox server",
            state=FAIL,
            detail=f"Không kết nối được: {type(exc).__name__}",
            why="pm2 start opensandbox-server",
        )
    return Check(
        key="sandbox",
        label="Sandbox server",
        state=OK,
        detail=f"http://{host}:{port} trả lời",
    )


def _check_worktree_root() -> Check:
    from bloy_dev_agent.features import workspace

    root = workspace.default_worktree_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".bloy-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check(
            key="worktree_root",
            label="Worktree root",
            state=FAIL,
            detail=f"{root} không ghi được: {exc}",
        )
    return Check(
        key="worktree_root",
        label="Worktree root",
        state=OK,
        detail=f"{root} ghi được",
        why="Đây là đường dẫn duy nhất sandbox được ghi vào.",
    )


def _check_repos() -> Check:
    from bloy_dev_agent.features import pipeline, workspace

    monorepo = pipeline.default_monorepo()
    present = [name for name in workspace.KNOWN_REPOS if (monorepo / name / ".git").exists()]
    missing = [name for name in workspace.KNOWN_REPOS if name not in present]
    return Check(
        key="repos",
        label="Repo đích",
        state=OK if present else FAIL,
        detail=(
            f"{len(present)}/{len(workspace.KNOWN_REPOS)} repo có sẵn"
            + (f" — thiếu: {', '.join(missing)}" if missing else "")
        ),
        why=f"Worktree được tách từ các sub-project dưới {monorepo}.",
    )


def _check_twenty() -> Check:
    base_url = os.environ.get("BLOY_TWENTY_BASE_URL", "").strip()
    api_key = os.environ.get("BLOY_TWENTY_API_KEY", "").strip()

    missing = [
        name
        for name, value in (
            ("BLOY_TWENTY_BASE_URL", base_url),
            ("BLOY_TWENTY_API_KEY", api_key),
        )
        if not value
    ]
    if missing:
        return Check(
            key="twenty",
            label="Twenty",
            state=FAIL,
            detail=f"Chưa đặt: {', '.join(missing)}",
            why="Không có hai biến này thì service không lấy được task.",
        )

    # Configured is not the same as reachable: prove the key works rather than
    # reporting green on the presence of two environment variables.
    from bloy_dev_agent.features.twenty.client import TwentyClient, TwentyError

    try:
        TwentyClient(base_url=base_url, api_key=api_key).ping()
    except TwentyError as exc:
        return Check(
            key="twenty",
            label="Twenty",
            state=FAIL,
            detail=f"{base_url} — {exc.message}",
            why="Kiểm tra API key, quyền của nó, và firewall.",
        )
    return Check(
        key="twenty", label="Twenty", state=OK, detail=f"Đã xác thực với {base_url}"
    )


def _check_bam() -> Check:
    """BAM is an integration, not a dependency — a run works without it."""
    import httpx

    url = os.environ.get("BAM_URL", "http://localhost:8000").rstrip("/")
    try:
        httpx.get(url, timeout=3.0)
    except Exception:  # noqa: BLE001
        return Check(
            key="bam",
            label="BAM console",
            state=WARN,
            detail=f"{url} không trả lời",
            why=(
                "Chỉ ảnh hưởng việc BAM tự kích routine. Service vẫn chạy được "
                "độc lập, và đó là lý do tách ra."
            ),
        )
    return Check(key="bam", label="BAM console", state=OK, detail=f"{url} trả lời")


def _check_git_push() -> Check:
    """MR được mở bằng git push options, nên SSH phải xác thực được."""
    import subprocess

    from bloy_dev_agent.features import pipeline, workspace

    repo = pipeline.default_monorepo() / workspace.KNOWN_REPOS[0]
    if not (repo / ".git").exists():
        return Check(
            key="git_push",
            label="GitLab SSH",
            state=WARN,
            detail="Không có repo để thử.",
        )
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", "HEAD"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check(
            key="git_push",
            label="GitLab SSH",
            state=FAIL,
            detail=f"{type(exc).__name__}: {exc}",
        )
    if result.returncode != 0:
        return Check(
            key="git_push",
            label="GitLab SSH",
            state=FAIL,
            detail=(result.stderr or "").strip()[:200],
            why="Không push được thì không mở được merge request.",
        )
    return Check(
        key="git_push",
        label="GitLab SSH",
        state=OK,
        detail="origin trả lời — push và mở MR được",
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_CHECKS = (
    _check_database,
    _check_claude,
    _check_claude_login,
    _check_sandbox_server,
    _check_worktree_root,
    _check_repos,
    _check_twenty,
    _check_git_push,
    _check_bam,
)


def run_checks() -> list[Check]:
    """Run every check, converting a crash into a reportable failure.

    A preflight page that raises is worse than useless — it hides the very
    problem it exists to surface.
    """
    checks: list[Check] = []
    for probe in _CHECKS:
        try:
            checks.append(probe())
        except Exception as exc:  # noqa: BLE001
            logger.exception("bloy_dev_agent: preflight %s crashed", probe.__name__)
            checks.append(
                Check(
                    key=probe.__name__.removeprefix("_check_"),
                    label=probe.__name__.removeprefix("_check_").replace("_", " ").title(),
                    state=FAIL,
                    detail=f"Check tự lỗi: {type(exc).__name__}: {exc}",
                )
            )
    return checks


def summarise(checks: list[Check]) -> dict[str, int]:
    counts = {OK: 0, WARN: 0, FAIL: 0}
    for check in checks:
        counts[check.state] = counts.get(check.state, 0) + 1
    return counts
