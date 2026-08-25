"""Give each issue its own git worktree, so the agent never edits your checkout.

A worktree is cheap — it shares the object store with the main clone — and it
gives three things a shared directory cannot: the agent's changes are on their
own branch, several issues can be worked at once, and abandoning an attempt is
``git worktree remove`` rather than an unpick.

The BLOY monorepo directory is not itself a repository; each sub-project is.
So a run always names the sub-project it targets.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


def default_worktree_root() -> Path:
    """Where per-issue worktrees live, overridable per host via ``BLOY_WORKTREE_ROOT``.

    Kept outside the repos so a stray glob or a clean-up in the main checkout
    cannot touch them. Read at call time, not baked in as a module constant —
    this module is imported before ``service.load_env()`` fills ``os.environ``
    from ``BLOY_DEV_AGENT/.env``, so a plain constant would miss that file.
    """
    return Path(os.environ.get("BLOY_WORKTREE_ROOT", "/home/bss-group/bloy-worktrees"))


def default_agent_repos_root() -> Path:
    """Where the agent's own independent clones live — never the developer's
    personal checkout.

    Without this, ``prepare()`` ran ``git fetch``/``git worktree add`` *inside
    the developer's own working copy* (``monorepo/repo``), because that
    checkout was the only clone on the machine. That is a real coupling, not
    just a theoretical one: it shares the same ``.git`` object store and
    remote-tracking refs as whatever the developer is doing by hand at that
    moment, and an unattended run's fetch mutates that checkout's own
    `origin/<base>` ref as a side effect. A developer's local branch state,
    an in-progress rebase, or simply having the wrong branch checked out at
    the wrong moment has nothing to do with what an automated ticket should
    branch from.

    ``prepare()`` prefers a repo cloned here over ``monorepo/repo`` whenever
    both exist, and falls back to the old behaviour only when this mirror
    has not been provisioned yet (see ``setup_wizard.check_agent_repos_mirror``
    for the one-time clone). Overridable via ``BLOY_AGENT_REPOS_ROOT``.
    """
    return Path(
        os.environ.get("BLOY_AGENT_REPOS_ROOT", str(Path.home() / "bloy-dev-agent-repos"))
    )


#: Sub-projects the agent may work in, relative to the monorepo directory.
KNOWN_REPOS = (
    "shopify-app-loyalty-api",
    "shopify-app-loyalty-cms",
    "shopify-app-loyalty-headless-commerce",
)

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class WorkspaceError(RuntimeError):
    """Raised when a worktree cannot be prepared."""


@dataclass
class Workspace:
    """One prepared worktree."""

    issue_key: str
    repo: str
    path: Path
    branch: str
    base_branch: str


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=120
    )


def _require(result: subprocess.CompletedProcess, what: str) -> str:
    if result.returncode != 0:
        raise WorkspaceError(f"{what}: {(result.stderr or result.stdout).strip()[:300]}")
    return result.stdout.strip()


def branch_name(issue_key: str) -> str:
    return f"bloy/{_SAFE.sub('-', issue_key).strip('-').lower()}"


def default_base(source: Path) -> str:
    """The repository's integration branch, from ``origin/HEAD``.

    Deliberately NOT the branch that happens to be checked out. A developer
    leaves their own feature branch open for days, and an unattended agent that
    inherited it produced a merge request carrying two of their unpushed commits
    and targeting their branch instead of master — the reviewer saw 19 changed
    files where the agent had touched 3.

    Falls back to the checked-out branch only when the remote has no HEAD, which
    happens on a bare local repository with no upstream.
    """
    ref = _git(["symbolic-ref", "refs/remotes/origin/HEAD"], source)
    if ref.returncode == 0:
        name = ref.stdout.strip().removeprefix("refs/remotes/origin/")
        if name:
            return name

    # Ask the remote directly before giving up; a fresh clone may not have the
    # symbolic ref yet even though the remote knows its default branch.
    remote = _git(["remote", "show", "origin"], source)
    if remote.returncode == 0:
        for line in remote.stdout.splitlines():
            if "HEAD branch:" in line:
                name = line.split("HEAD branch:", 1)[1].strip()
                if name and name != "(unknown)":
                    return name

    fallback = _require(
        _git(["rev-parse", "--abbrev-ref", "HEAD"], source), "read the current branch"
    )
    logger.warning(
        "bloy_dev_agent: %s có origin/HEAD không xác định, dùng nhánh đang mở (%s)",
        source.name, fallback,
    )
    return fallback


def prepare(
    issue_key: str,
    *,
    monorepo: Path,
    repo: str,
    root: Path | None = None,
    base_branch: str = "",
) -> Workspace:
    """Create (or reuse) a worktree for ``issue_key`` in ``repo``.

    Reuse matters: a second pass on the same issue should continue on the same
    branch rather than lose the first attempt.
    """
    root = root if root is not None else default_worktree_root()
    if repo not in KNOWN_REPOS:
        raise WorkspaceError(f"Unknown repo {repo!r}; expected one of {KNOWN_REPOS}")

    # Prefer the agent's own independent clone — see default_agent_repos_root's
    # docstring for why sharing the developer's own checkout was a real
    # coupling, not just a theoretical one. Falling back to the personal
    # checkout only when the mirror has not been provisioned yet keeps this
    # backward compatible rather than breaking every run on hosts that
    # haven't set it up.
    mirror_source = default_agent_repos_root() / repo
    source = mirror_source if (mirror_source / ".git").exists() else monorepo / repo
    if not (source / ".git").exists():
        raise WorkspaceError(f"{source} is not a git repository")

    root.mkdir(parents=True, exist_ok=True)
    branch = branch_name(issue_key)
    target = root / f"{_SAFE.sub('-', issue_key).lower()}-{repo}"

    base = base_branch or default_base(source)

    # Branch from what the remote has, not from whatever this checkout last
    # pulled. A machine running the agent unattended goes stale within a day,
    # and a merge request built on stale master is noise for the reviewer:
    # it carries conflicts, or "fixes" something already fixed upstream.
    # Observed here with master two commits behind while a run was starting.
    start_point = base
    if _git(["fetch", "--quiet", "origin", base], source).returncode == 0:
        remote_ref = f"origin/{base}"
        if _git(["rev-parse", "--verify", "--quiet", remote_ref], source).returncode == 0:
            start_point = remote_ref
    else:
        # Offline or the remote is down. Working from a stale base still beats
        # refusing the ticket, but the reviewer should know which it was.
        logger.warning(
            "bloy_dev_agent: không fetch được origin/%s, tách nhánh từ bản local", base
        )

    if target.exists():
        # Already prepared. Verify it is really a worktree of this repo before
        # handing it back, so a leftover directory cannot be mistaken for one.
        head = _git(["rev-parse", "--abbrev-ref", "HEAD"], target)
        if head.returncode == 0:
            logger.info("bloy_dev_agent: reusing worktree %s (%s)", target, head.stdout.strip())
            return Workspace(issue_key, repo, target, head.stdout.strip(), base)
        raise WorkspaceError(f"{target} exists but is not a usable worktree")

    exists = _git(["rev-parse", "--verify", "--quiet", branch], source).returncode == 0
    args = ["worktree", "add"]
    if exists:
        args += [str(target), branch]
    else:
        args += ["-b", branch, str(target), start_point]
    _require(_git(args, source), f"create a worktree for {issue_key}")

    logger.info(
        "bloy_dev_agent: worktree %s on %s (tách từ %s)", target, branch, start_point
    )
    return Workspace(issue_key, repo, target, branch, base)


#: Files a run must never bring into a merge request. The agent installs
#: dependencies while exploring — reading a package's source, running a test —
#: and the lockfile churn that follows swamps the real change: BLS-1080 shipped
#: one meaningful line beside 179 lines of ``package-lock.json``. A dependency
#: bump is a deliberate act with its own review, never a side effect.
LOCKFILES = frozenset(
    {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "composer.lock",
        "Gemfile.lock",
        "poetry.lock",
        "uv.lock",
        "Pipfile.lock",
        "cargo.lock",
        "Cargo.lock",
    }
)


#: Directories a run must never bring into a merge request either, for the
#: exact same reason as LOCKFILES: they are build output, not source, and an
#: agent legitimately runs the build while investigating (checking a
#: constraint, comparing before/after) without meaning to commit the result.
#: Found live: an agent investigating a checkout-extension build limit ran
#: `node build-cdn.js`, and the resulting minify/format diff — thousands of
#: lines across all 9 bundles, including a ~40k-line truncation of
#: `headless.bloy.js` — rode into a real merge request under an unrelated
#: ticket that had touched no source at all. staging_control/apps.py's
#: RSYNC_EXCLUDES already treats this exact path as checkout-local, never
#: authoritative from a worktree, for the same reason.
GENERATED_DIRS = ("extensions/cdn-dist",)


def discard_generated_changes(workspace: Workspace) -> list[str]:
    """Undo any lockfile or generated-output-dir change; return the paths reverted.

    Enforced here rather than only asked for in the prompt: an instruction is a
    request, and this is the single place where anything gets staged, so it is
    the one place the rule can actually hold.
    """
    status = _git(["status", "--porcelain", "--untracked-files=all"], workspace.path)
    reverted: list[str] = []

    for line in status.stdout.splitlines():
        if len(line) < 4:
            continue
        code, path = line[:2], line[3:].strip().strip('"')
        # Renames read "old -> new"; the destination is what is staged.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        is_lockfile = Path(path).name in LOCKFILES
        is_generated = any(
            path == d or path.startswith(f"{d}/") for d in GENERATED_DIRS
        )
        if not is_lockfile and not is_generated:
            continue

        if code.strip() == "??":
            (workspace.path / path).unlink(missing_ok=True)
        else:
            _git(["checkout", "--", path], workspace.path)
        reverted.append(path)

    if reverted:
        logger.info(
            "bloy_dev_agent: bỏ thay đổi lockfile/generated khỏi %s: %s",
            workspace.issue_key,
            ", ".join(reverted),
        )
    return reverted


def reset_to_base(workspace: Workspace) -> bool:
    """Throw away everything on the branch and start again from its base.

    A retry has to begin from a clean base. Without this, a second attempt
    stacks a commit on top of the first — so a run that was retried precisely
    *because* its commit was wrong keeps that commit in the merge request, and
    the push is a non-fast-forward besides.

    Returns True when something was actually discarded.
    """
    ahead = _git(["rev-list", "--count", f"{workspace.base_branch}..HEAD"], workspace.path)
    had_commits = (ahead.stdout or "0").strip() not in ("", "0")
    dirty = bool(_git(["status", "--porcelain"], workspace.path).stdout.strip())
    if not had_commits and not dirty:
        return False

    _require(
        _git(["reset", "--hard", workspace.base_branch], workspace.path),
        f"reset {workspace.branch} to {workspace.base_branch}",
    )
    # Untracked leftovers would otherwise be committed by the next `add -A`.
    _git(["clean", "-fd"], workspace.path)
    logger.info(
        "bloy_dev_agent: %s reset về %s (bỏ commit/thay đổi của lượt trước)",
        workspace.branch,
        workspace.base_branch,
    )
    return True


def has_changes(workspace: Workspace) -> bool:
    """True when the agent actually changed something worth committing."""
    result = _git(["status", "--porcelain"], workspace.path)
    return bool(result.stdout.strip())


def diffstat(workspace: Workspace) -> str:
    """Short summary of the working-tree changes, for the issue comment.

    Untracked files are listed separately because ``git diff`` ignores them
    entirely — and a new file is the *typical* output of an implement run, so
    a plain diffstat would report "no changes" on exactly the runs that worked.
    """
    parts: list[str] = []
    tracked = _git(["diff", "--stat", "HEAD"], workspace.path).stdout.strip()
    if tracked:
        parts.append(tracked)

    untracked = _git(
        ["ls-files", "--others", "--exclude-standard"], workspace.path
    ).stdout.split()
    if untracked:
        listed = "\n".join(f" {name} (mới)" for name in untracked[:20])
        if len(untracked) > 20:
            listed += f"\n ... và {len(untracked) - 20} file mới khác"
        parts.append(listed)

    return "\n".join(parts).strip()


def commit_and_push(
    workspace: Workspace, *, title: str, body: str = "", create_mr: bool = True
) -> dict:
    """Commit everything in the worktree and push the branch.

    The merge request is opened with git push options rather than the GitLab
    API: pushing already works over the existing SSH key, so this needs no
    token, and no credential ever has to reach the agent.
    """
    if not has_changes(workspace):
        return {"ok": False, "detail": "the agent changed nothing"}

    _require(_git(["add", "-A"], workspace.path), "stage changes")
    message = f"{title}\n\n{body}".strip() if body else title
    commit = _git(["commit", "-m", message], workspace.path)
    if commit.returncode != 0 and "nothing to commit" not in (commit.stdout or ""):
        raise WorkspaceError(f"commit failed: {(commit.stderr or commit.stdout)[:300]}")

    push = ["push", "--set-upstream", "origin", workspace.branch]
    if create_mr:
        push += [
            "-o",
            "merge_request.create",
            "-o",
            f"merge_request.target={workspace.base_branch}",
            "-o",
            f"merge_request.title={title}",
            "-o",
            "merge_request.remove_source_branch",
        ]
    result = _git(push, workspace.path)
    output = f"{result.stdout}\n{result.stderr}".strip()
    if result.returncode != 0:
        return {"ok": False, "detail": output[:600]}

    # GitLab prints the merge-request URL on the push response.
    match = re.search(r"https?://\S*merge_requests\S*", output)
    return {
        "ok": True,
        "branch": workspace.branch,
        "merge_request_url": match.group(0) if match else "",
        "output": output[:600],
    }
