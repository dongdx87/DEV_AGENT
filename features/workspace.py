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
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Where per-issue worktrees live. Kept outside the repos so a stray glob or a
#: clean-up in the main checkout cannot touch them.
DEFAULT_WORKTREE_ROOT = Path("/home/bss-group/bloy-worktrees")

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


def prepare(
    issue_key: str,
    *,
    monorepo: Path,
    repo: str,
    root: Path = DEFAULT_WORKTREE_ROOT,
    base_branch: str = "",
) -> Workspace:
    """Create (or reuse) a worktree for ``issue_key`` in ``repo``.

    Reuse matters: a second pass on the same issue should continue on the same
    branch rather than lose the first attempt.
    """
    if repo not in KNOWN_REPOS:
        raise WorkspaceError(f"Unknown repo {repo!r}; expected one of {KNOWN_REPOS}")

    source = monorepo / repo
    if not (source / ".git").exists():
        raise WorkspaceError(f"{source} is not a git repository")

    root.mkdir(parents=True, exist_ok=True)
    branch = branch_name(issue_key)
    target = root / f"{_SAFE.sub('-', issue_key).lower()}-{repo}"

    base = base_branch or _require(
        _git(["rev-parse", "--abbrev-ref", "HEAD"], source), "read the current branch"
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
        args += ["-b", branch, str(target), base]
    _require(_git(args, source), f"create a worktree for {issue_key}")

    logger.info("bloy_dev_agent: worktree %s on %s", target, branch)
    return Workspace(issue_key, repo, target, branch, base)


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
