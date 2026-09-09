"""Diagnose a fresh machine and fix what can honestly be fixed from a browser.

Moving this service to another machine used to mean a shell session and a
half-remembered checklist; the first attempt failed simply because
``~/.sandbox.toml`` did not exist. This module turns that checklist into
inspectable steps, each of which either repairs itself or hands over the exact
command to run.

The split matters, so it is explicit in the data: ``fix`` names an action the
service may perform as its own unprivileged user, and ``command`` is what a
human must run because the service genuinely cannot. Installing Docker needs
root, and ``sudo`` on this host asks for a password — a web form pretending
otherwise would just fail in a more confusing place.

Nothing here executes text supplied by the caller. Actions are a fixed set of
named functions; the HTTP layer picks one by key and passes typed arguments.
"""

from __future__ import annotations

import logging
import re
import secrets
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

OK = "ok"
WARN = "warn"
FAIL = "fail"

SANDBOX_CONFIG = Path.home() / ".sandbox.toml"

#: Written when the file is absent. Mirrors the upstream sample, minus the
#: commentary, plus the two settings a fresh install always gets wrong: an
#: api_key (empty means the server exits when run non-interactively) and the
#: bind-mount allowlist (empty means every host path is permitted).
SANDBOX_TEMPLATE = """\
[server]
host = "{host}"
port = {port}
max_sandbox_timeout_seconds = 86400
api_key = "{api_key}"

[log]
level = "INFO"

[runtime]
type = "docker"
execd_image = "opensandbox/execd:v1.0.21"

[storage]
allowed_host_paths = [{allowed}]
volume_default_size = "1Gi"

[store]
type = "sqlite"
path = "~/.opensandbox/opensandbox.db"

[docker]
network_mode = "bridge"
port_range_min = 40000
port_range_max = 60000
drop_capabilities = ["AUDIT_WRITE", "MKNOD", "NET_ADMIN", "NET_RAW", "SYS_ADMIN",
                     "SYS_MODULE", "SYS_PTRACE", "SYS_TIME", "SYS_TTY_CONFIG"]
no_new_privileges = true
pids_limit = 4096

[ingress]
mode = "direct"

[egress]
image = "opensandbox/egress:v1.1.4"
mode = "dns"
"""


@dataclass
class Step:
    """One setup requirement and what to do about it."""

    key: str
    label: str
    state: str
    detail: str
    #: Name of an action this service can run itself, or "" when it cannot.
    fix: str = ""
    fix_label: str = ""
    #: Command a human must run, when no safe automatic fix exists.
    command: str = ""
    why: str = ""


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _run(args: list[str], timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def check_docker() -> Step:
    if shutil.which("docker") is None:
        return Step(
            "docker", "Docker", FAIL, "Chưa cài docker.",
            command="curl -fsSL https://get.docker.com | sudo sh"
                    " && sudo usermod -aG docker $USER   # rồi đăng xuất/đăng nhập lại",
            why="Cài đặt cần quyền root, mà sudo trên máy này hỏi mật khẩu — "
                "service chạy user thường nên không tự làm được.",
        )
    try:
        result = _run(["docker", "info"], timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Step("docker", "Docker", FAIL, f"Không gọi được: {exc}")
    if result.returncode != 0:
        return Step(
            "docker", "Docker", FAIL,
            "Có docker nhưng user hiện tại không dùng được.",
            command="sudo usermod -aG docker $USER   # rồi đăng xuất/đăng nhập lại",
            why="Chạy được docker mà không cần sudo là điều kiện để sandbox hoạt động.",
        )
    return Step("docker", "Docker", OK, "Chạy được, không cần sudo.")


def _sandbox_settings() -> dict:
    if not SANDBOX_CONFIG.exists():
        return {}
    try:
        return tomllib.loads(SANDBOX_CONFIG.read_text(encoding="utf-8")).get("server") or {}
    except (OSError, ValueError):
        return {}


def required_host_paths(
    *,
    monorepo: Path,
    worktree_root: Path,
    skill_packs_root: Path | None = None,
    monorepo_mirror: Path | None = None,
    shopify_auth_dir: Path | None = None,
    agent_repos_root: Path | None = None,
) -> list[str]:
    """Every host path ``~/.sandbox.toml``'s ``allowed_host_paths`` must list.

    The SINGLE source of truth for that list — both ``diagnose()``'s check and
    the "Bổ sung đường dẫn" fix action (``write_sandbox_config``, called from
    ``service.py``'s ``/setup/fix/write_sandbox_config`` route) must build it
    from here rather than keeping their own copies. They used to: the fix
    action's copy quietly fell out of sync and dropped ``agent_repos_root``,
    so clicking the button rewrote the file still missing the one path the
    check was actually complaining about — an unfixable "fix", found live.
    """
    required = [str(monorepo), str(worktree_root), str(Path.home() / ".nvm"),
                str(Path.home() / ".claude")]
    if skill_packs_root is not None:
        # Only required once a pack is actually mounted — the sandbox has no
        # opinion on a directory it never binds — but listing it here means
        # the same "Tạo file cấu hình" fix that repairs everything else also
        # covers this path, instead of a second silent-rejection bug to find.
        required.append(str(skill_packs_root))
    if monorepo_mirror is not None:
        # The filtered mirror sandbox_runner mounts instead of the real
        # monorepo (see its module docstring) — a sibling directory, so it
        # needs its own entry in the allowlist.
        required.append(str(monorepo_mirror))
    if shopify_auth_dir is not None:
        # A ticket touching the cms repo activates staging-verify with no
        # marker and no approval step (see mapping.touches_ui_repo) — so
        # this path being missing from the allowlist is a silent failure
        # waiting to happen on the very next such ticket, not a hypothetical.
        required.append(str(shopify_auth_dir))
    if agent_repos_root is not None:
        # workspace.prepare() branches every worktree from here whenever the
        # mirror exists, and the resulting worktree's ``.git`` file is a
        # pointer straight back to this absolute host path — sandbox_runner
        # mounts it at the identical path for exactly that reason (see its
        # own _volumes() comment). Missing from the allowlist meant every
        # git command run from inside such a worktree failed outright; found
        # live on a real run, not hypothetical like the comment above reads.
        required.append(str(agent_repos_root))
    return required


def check_sandbox_config(required_paths: list[str]) -> Step:
    if not SANDBOX_CONFIG.exists():
        return Step(
            "sandbox_config", "Cấu hình OpenSandbox", FAIL,
            f"Chưa có {SANDBOX_CONFIG}.",
            fix="write_sandbox_config", fix_label="Tạo file cấu hình",
            why="Thiếu đúng file này là lý do lần chuyển máy trước bị lỗi.",
        )

    server = _sandbox_settings()
    if not str(server.get("api_key") or "").strip():
        return Step(
            "sandbox_config", "Cấu hình OpenSandbox", FAIL,
            "File có nhưng api_key rỗng.",
            fix="write_sandbox_config", fix_label="Sinh api_key",
            why="Key rỗng thì server tự thoát khi chạy non-interactive — nó restart "
                "16 lần rồi errored, mà nút Start vẫn báo thành công.",
        )

    try:
        allowed = tomllib.loads(SANDBOX_CONFIG.read_text(encoding="utf-8"))
        allowed_paths = (allowed.get("storage") or {}).get("allowed_host_paths") or []
    except (OSError, ValueError):
        allowed_paths = []

    missing = [p for p in required_paths if p not in allowed_paths]
    if missing:
        return Step(
            "sandbox_config", "Cấu hình OpenSandbox", FAIL,
            "allowed_host_paths thiếu: " + ", ".join(missing),
            fix="write_sandbox_config", fix_label="Bổ sung đường dẫn",
            why="Server từ chối mount đường ngoài danh sách, nên agent sẽ không "
                "thấy worktree hay CLAUDE.md.",
        )
    return Step("sandbox_config", "Cấu hình OpenSandbox", OK, str(SANDBOX_CONFIG))


def check_sandbox_server() -> Step:
    import httpx

    server = _sandbox_settings()
    host = str(server.get("host") or "127.0.0.1")
    port = str(server.get("port") or "8080")
    if not SANDBOX_CONFIG.exists():
        return Step("sandbox_server", "OpenSandbox server", FAIL, "Chưa có cấu hình.")
    try:
        httpx.get(f"http://{host}:{port}/", timeout=3.0)
    except Exception:  # noqa: BLE001 — any failure means "not usable"
        return Step(
            "sandbox_server", "OpenSandbox server", FAIL,
            f"Không kết nối được http://{host}:{port}",
            fix="start_sandbox_server", fix_label="Khởi động server",
        )
    return Step("sandbox_server", "OpenSandbox server", OK, f"http://{host}:{port} trả lời")


def check_worktree_root(root: Path) -> Step:
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".bloy-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Step("worktree_root", "Thư mục worktree", FAIL, f"{root}: {exc}")
    return Step("worktree_root", "Thư mục worktree", OK, f"{root} ghi được")


def check_repos(monorepo: Path, repos: tuple[str, ...]) -> Step:
    if not monorepo.is_dir():
        return Step(
            "repos", "Repo BLOY", FAIL, f"Không có thư mục {monorepo}.",
            command=f"mkdir -p {monorepo}",
        )
    missing = [name for name in repos if not (monorepo / name / ".git").exists()]
    if not missing:
        return Step("repos", "Repo BLOY", OK, f"{len(repos)}/{len(repos)} repo có sẵn")
    return Step(
        "repos", "Repo BLOY", FAIL, "Thiếu: " + ", ".join(missing),
        fix="clone_repos", fix_label="Clone repo còn thiếu",
        why="Sandbox đọc CLAUDE.md và các repo anh em từ đây (chỉ đọc, mount "
            "read-only) — không phải nơi worktree được tách ra (xem "
            "'Mirror độc lập cho agent' bên dưới).",
    )


def check_agent_repos_mirror(agent_repos_root: Path, repos: tuple[str, ...]) -> Step:
    """A separate clone the agent branches from — never the developer's own
    checkout. See workspace.default_agent_repos_root's docstring: without
    this, prepare() ran `git fetch`/`git worktree add` *inside* whatever
    checkout happened to be at monorepo/repo, coupling an unattended run to
    the developer's own working copy (shared .git object store, and a fetch
    that silently mutates that checkout's own remote-tracking ref).
    """
    if not agent_repos_root.is_dir():
        return Step(
            "agent_repos_mirror", "Mirror độc lập cho agent", FAIL,
            f"Không có thư mục {agent_repos_root}.",
            fix="clone_agent_repos_mirror", fix_label="Clone mirror độc lập",
            why="Không có mirror, agent tạm dùng bản checkout cá nhân trên máy "
                "để tách worktree — vẫn chạy được, nhưng ghép chung .git object "
                "store với đúng cái bạn đang code tay.",
        )
    missing = [name for name in repos if not (agent_repos_root / name / ".git").exists()]
    if not missing:
        return Step(
            "agent_repos_mirror", "Mirror độc lập cho agent", OK,
            f"{len(repos)}/{len(repos)} repo có mirror riêng, tách khỏi checkout cá nhân",
        )
    return Step(
        "agent_repos_mirror", "Mirror độc lập cho agent", FAIL,
        "Thiếu mirror cho: " + ", ".join(missing),
        fix="clone_agent_repos_mirror", fix_label="Clone mirror độc lập",
        why="Không có mirror, agent tạm dùng bản checkout cá nhân trên máy để "
            "tách worktree — vẫn chạy được, nhưng ghép chung .git object store "
            "với đúng cái bạn đang code tay.",
    )


def check_claude_cli() -> Step:
    from bloy_dev_agent.preflight import find_claude_binary

    binary = find_claude_binary()
    if binary:
        return Step("claude_cli", "Claude CLI", OK, binary)
    return Step(
        "claude_cli", "Claude CLI", FAIL, "Không tìm thấy trên PATH hay dưới nvm.",
        command="npm install -g @anthropic-ai/claude-code",
        why="Sandbox mount thư mục nvm của host vào container để dùng CLI này.",
    )


def check_claude_login() -> Step:
    config = Path.home() / ".claude.json"
    if config.exists() and config.stat().st_size > 0:
        return Step("claude_login", "Claude login", OK, f"{config}")
    return Step(
        "claude_login", "Claude login", FAIL, f"Chưa có {config}.",
        command="claude   # đăng nhập một lần, rồi thoát",
        why="Đăng nhập cần trình duyệt nên không làm được từ form. File này nằm "
            "CẠNH ~/.claude chứ không nằm trong.",
    )


def check_git_ssh(monorepo: Path, repos: tuple[str, ...]) -> Step:
    repo = next((monorepo / r for r in repos if (monorepo / r / ".git").exists()), None)
    if repo is None:
        return Step("git_ssh", "GitLab SSH", WARN, "Chưa có repo để thử.")
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", "HEAD"],
            cwd=str(repo), capture_output=True, text=True, timeout=25,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Step("git_ssh", "GitLab SSH", FAIL, f"{type(exc).__name__}: {exc}")
    if result.returncode != 0:
        return Step(
            "git_ssh", "GitLab SSH", FAIL, (result.stderr or "").strip()[:160],
            command='ssh-keygen -t ed25519 -C "$USER@bloy" '
                    "&& cat ~/.ssh/id_ed25519.pub   # thêm vào GitLab → SSH Keys",
            why="Host dùng SSH để fetch và push; không push được thì không mở được MR.",
        )
    return Step("git_ssh", "GitLab SSH", OK, "origin trả lời — push và mở MR được")


def check_egress_mode() -> Step:
    if not SANDBOX_CONFIG.exists():
        return Step("egress_mode", "Egress mode", WARN, "Chưa có cấu hình để kiểm tra.")
    try:
        config = tomllib.loads(SANDBOX_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Step("egress_mode", "Egress mode", WARN, "Không đọc được file cấu hình.")
    mode = str((config.get("egress") or {}).get("mode") or "")
    if mode == "dns+nft":
        return Step(
            "egress_mode", "Egress mode", OK,
            "dns+nft — chặn theo địa chỉ IP, không chỉ theo câu hỏi DNS.",
        )
    detail = f'Đang là "{mode}"' if mode else "Chưa có [egress].mode"
    return Step(
        "egress_mode", "Egress mode", WARN,
        f"{detail} — chỉ lọc DNS, không chặn nối thẳng bằng IP.",
        fix="write_egress_mode", fix_label='Đổi [egress].mode thành "dns+nft"',
        why="Một sandbox biết trước địa chỉ IP vẫn nối thẳng được dù DNS bị chặn — "
            "đây là cách mọi run trước đây vẫn với tới được MongoDB/Redis/MySQL trên "
            "gateway Docker. Đổi xong phải tự bấm restart server (không tự động, để "
            "không cắt ngang sandbox đang chạy dở).",
    )


def check_staging() -> Step:
    """Whether the staging-control service (:8110) answers at all.

    Only meaningful once a run actually opts into staging-verify — an ordinary
    ticket never calls this service — so the caller decides whether to include
    this probe rather than it running unconditionally.
    """
    import httpx

    from bloy_dev_agent.staging_control import service as staging_service

    host, port = staging_service.host(), staging_service.port()
    try:
        response = httpx.get(f"http://{host}:{port}/v1/status", timeout=3.0)
    except Exception as exc:  # noqa: BLE001 — any failure just means "not reachable"
        return Step(
            "staging", "Staging-control", WARN,
            f"Không kết nối được {host}:{port}: {exc}",
        )
    # A 401 still proves the process itself is up and answering — every route
    # requires a bearer token, so an unauthenticated probe never gets a 200.
    if response.status_code in (200, 401):
        return Step("staging", "Staging-control", OK, f"{host}:{port} đang chạy")
    return Step("staging", "Staging-control", WARN, f"HTTP {response.status_code}")


def check_twenty(base_url: str, api_key: str) -> Step:
    missing = [n for n, v in (("URL", base_url), ("API key", api_key)) if not v]
    if missing:
        return Step(
            "twenty", "Twenty", FAIL, "Chưa điền: " + ", ".join(missing),
            fix="form", fix_label="Điền ở form bên dưới",
        )
    from bloy_dev_agent.features.twenty.client import TwentyClient, TwentyError

    try:
        TwentyClient(base_url=base_url, api_key=api_key).ping()
    except TwentyError as exc:
        return Step("twenty", "Twenty", FAIL, f"{base_url} — {exc.message}",
                    fix="form", fix_label="Sửa ở form bên dưới")
    return Step("twenty", "Twenty", OK, f"Đã xác thực với {base_url}")


# ---------------------------------------------------------------------------
# Fixes the service may perform itself
# ---------------------------------------------------------------------------


def write_sandbox_config(allowed_paths: list[str], *, host: str = "127.0.0.1",
                         port: int = 8080) -> str:
    """Create or repair ``~/.sandbox.toml``, keeping any api_key already there."""
    existing = _sandbox_settings()
    api_key = str(existing.get("api_key") or "").strip() or secrets.token_urlsafe(32)

    if SANDBOX_CONFIG.exists():
        # Preserve whatever else the file holds; only rewrite the two settings a
        # broken install gets wrong. A blunt overwrite would discard hardening
        # someone added by hand.
        text = SANDBOX_CONFIG.read_text(encoding="utf-8")
        text = re.sub(r'^api_key\s*=.*$', f'api_key = "{api_key}"', text, count=1, flags=re.M)
        listed = ", ".join(f'"{p}"' for p in allowed_paths)
        text = re.sub(r"^allowed_host_paths\s*=.*$",
                      f"allowed_host_paths = [{listed}]", text, count=1, flags=re.M)
    else:
        text = SANDBOX_TEMPLATE.format(
            host=host, port=port, api_key=api_key,
            allowed=", ".join(f'"{p}"' for p in allowed_paths),
        )

    SANDBOX_CONFIG.write_text(text, encoding="utf-8")
    SANDBOX_CONFIG.chmod(0o600)  # it holds the only secret protecting the server
    logger.info("bloy_dev_agent: wrote %s", SANDBOX_CONFIG)
    return f"Đã ghi {SANDBOX_CONFIG} (quyền 600)"


def write_egress_mode(mode: str = "dns+nft") -> str:
    """Repair only the ``[egress]`` block's ``mode`` key.

    ``[ingress]`` has a key of the same name — a blind top-level
    ``^mode\\s*=`` regex over the whole file would hit whichever one comes
    first, which happens to be ``[ingress]``. This isolates the ``[egress]``
    block's own lines before substituting.
    """
    if not SANDBOX_CONFIG.exists():
        return f"Chưa có {SANDBOX_CONFIG} — tạo file cấu hình trước."
    text = SANDBOX_CONFIG.read_text(encoding="utf-8")
    match = re.search(r"^\[egress\]\n(?:(?!^\[).*\n?)*", text, flags=re.M)
    if match is None:
        return "Không tìm thấy block [egress] trong file — không sửa gì."
    block = match.group(0)
    if re.search(r"^mode\s*=", block, flags=re.M):
        new_block = re.sub(r'^mode\s*=.*$', f'mode = "{mode}"', block, count=1, flags=re.M)
    else:
        new_block = block.rstrip("\n") + f'\nmode = "{mode}"\n'
    text = text[: match.start()] + new_block + text[match.end() :]
    SANDBOX_CONFIG.write_text(text, encoding="utf-8")
    SANDBOX_CONFIG.chmod(0o600)
    logger.info("bloy_dev_agent: [egress].mode -> %s in %s", mode, SANDBOX_CONFIG)
    return (
        f'Đã đổi [egress].mode thành "{mode}" — CHƯA restart opensandbox-server '
        "(cố ý, để không cắt ngang sandbox đang chạy dở); bấm nút restart riêng "
        "khi không còn run nào đang hoạt động."
    )


def start_sandbox_server() -> str:
    """Start the server under PM2 when available, otherwise detached."""
    if shutil.which("pm2"):
        existing = _run(["pm2", "restart", "opensandbox-server"], timeout=30)
        if existing.returncode == 0:
            return "Đã restart opensandbox-server qua PM2"
        started = _run(
            ["pm2", "start", "bash", "--name", "opensandbox-server",
             "--", "-c", "uvx opensandbox-server"],
            timeout=40,
        )
        if started.returncode == 0:
            _run(["pm2", "save"], timeout=30)
            return "Đã tạo tiến trình PM2 opensandbox-server (đã pm2 save)"
        return f"PM2 không khởi động được: {(started.stderr or '')[:200]}"

    if shutil.which("uvx") is None:
        return "Không có pm2 lẫn uvx — cài uv rồi thử lại"
    subprocess.Popen(
        ["uvx", "opensandbox-server"],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return "Đã chạy uvx opensandbox-server (nền, không có PM2 nên không tự sống lại)"


def clone_repos(monorepo: Path, repos: tuple[str, ...], remote_prefix: str) -> str:
    """Clone whichever sub-projects are missing, over the host's SSH key."""
    monorepo.mkdir(parents=True, exist_ok=True)
    done: list[str] = []
    failed: list[str] = []
    for name in repos:
        if (monorepo / name / ".git").exists():
            continue
        url = f"{remote_prefix.rstrip('/')}/{name}.git"
        result = subprocess.run(
            ["git", "clone", url, str(monorepo / name)],
            capture_output=True, text=True, timeout=900,
        )
        (done if result.returncode == 0 else failed).append(name)
        if result.returncode != 0:
            logger.warning("bloy_dev_agent: clone %s failed: %s", name, result.stderr[:200])
    parts = []
    if done:
        parts.append("đã clone: " + ", ".join(done))
    if failed:
        parts.append("thất bại: " + ", ".join(failed) + " (kiểm tra khoá SSH)")
    return "; ".join(parts) or "Không có repo nào cần clone"


#: clone_repos() only cares that its first argument is a directory to clone
#: `<name>.git` into per entry in `repos` — reusing it for the independent
#: mirror avoids a near-duplicate clone loop.
clone_agent_repos_mirror = clone_repos


#: Actions the HTTP layer may invoke, by name. A fixed table rather than a
#: lookup by attribute, so a crafted key can never reach an arbitrary callable.
FIXES = {
    "write_sandbox_config",
    "write_egress_mode",
    "start_sandbox_server",
    "clone_repos",
    "clone_agent_repos_mirror",
}


def diagnose(
    *,
    monorepo: Path,
    worktree_root: Path,
    repos: tuple[str, ...],
    twenty_url: str,
    twenty_key: str,
    skill_packs_root: Path | None = None,
    monorepo_mirror: Path | None = None,
    shopify_auth_dir: Path | None = None,
    agent_repos_root: Path | None = None,
    include_staging: bool = False,
) -> list[Step]:
    """Run every check, converting a crash into a reportable failure."""
    required = required_host_paths(
        monorepo=monorepo,
        worktree_root=worktree_root,
        skill_packs_root=skill_packs_root,
        monorepo_mirror=monorepo_mirror,
        shopify_auth_dir=shopify_auth_dir,
        agent_repos_root=agent_repos_root,
    )
    probes = [
        lambda: check_docker(),
        lambda: check_sandbox_config(required),
        lambda: check_sandbox_server(),
        lambda: check_worktree_root(worktree_root),
        lambda: check_repos(monorepo, repos),
    ]
    if agent_repos_root is not None:
        probes.append(lambda: check_agent_repos_mirror(agent_repos_root, repos))
    probes += [
        lambda: check_claude_cli(),
        lambda: check_claude_login(),
        lambda: check_git_ssh(monorepo, repos),
        lambda: check_twenty(twenty_url, twenty_key),
        lambda: check_egress_mode(),
    ]
    if include_staging:
        # Only meaningful once a caller actually cares about staging-verify —
        # an ordinary run never talks to this service, so it stays out of the
        # default probe list.
        probes.append(lambda: check_staging())
    steps: list[Step] = []
    for probe in probes:
        try:
            steps.append(probe())
        except Exception as exc:  # noqa: BLE001 — a setup page must never 500
            logger.exception("bloy_dev_agent: setup probe crashed")
            steps.append(Step("unknown", "Check lỗi", FAIL, f"{type(exc).__name__}: {exc}"))
    return steps
