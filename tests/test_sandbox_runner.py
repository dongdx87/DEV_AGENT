"""Tests for the sandbox bridge.

The one behaviour that broke in production: BAM's routine scheduler calls
actions *on its own running event loop*, so a bare ``asyncio.run`` raises
"cannot be called from a running event loop" and the whole implement run is
lost. That case is pinned first.
"""

from __future__ import annotations

import asyncio

import pytest

from bloy_dev_agent.features import sandbox_runner


def test_runs_when_a_loop_is_already_running():
    """The regression: the scheduler's loop must not block the sandbox call."""

    async def caller():
        return sandbox_runner._run_coroutine(_answer)

    async def _answer():
        return "ok"

    assert asyncio.run(caller()) == "ok"


def test_runs_with_no_loop_at_all():
    async def _answer():
        return "ok"

    assert sandbox_runner._run_coroutine(_answer) == "ok"


def test_an_error_inside_the_coroutine_reaches_the_caller():
    """A swallowed exception would look identical to a silent empty run."""

    async def _boom():
        raise ValueError("container refused")

    with pytest.raises(ValueError, match="container refused"):
        sandbox_runner._run_coroutine(_boom)


def test_a_missing_config_is_reported_not_raised(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox_runner, "SANDBOX_CONFIG", tmp_path / "absent.toml")

    result = sandbox_runner.run_in_sandbox(
        "do the thing", tmp_path / "wt" / "issue", worktree_root=tmp_path / "wt"
    )

    assert result.ok is False
    assert "not found" in result.output


def test_execution_text_flattens_message_objects():
    """OpenSandbox logs are message objects; joining them naively raises."""

    class Message:
        def __init__(self, text):
            self.text = text

    class Logs:
        stdout = [Message("hello "), Message("world")]
        stderr = [Message("!")]

    class Execution:
        logs = Logs()

    assert sandbox_runner._text(Execution()) == "hello world!"


def test_analysis_mode_never_gets_the_write_flag():
    """Read-only must stay read-only even though both modes share the runner."""
    assert sandbox_runner.claude_flags(implement=False) == (
        "--allowedTools Read Grep Glob --permission-mode plan"
    )
    assert "--dangerously-skip-permissions" not in sandbox_runner.claude_flags(False)
    assert sandbox_runner.claude_flags(True) == "--dangerously-skip-permissions"


def test_the_prompt_is_never_interpolated_into_the_shell():
    """Issue bodies are untrusted text; they must arrive base64-encoded."""
    hostile = 'title"; rm -rf / #\n`whoami`\n$(id)'

    script = sandbox_runner._setup_script(hostile)

    assert "rm -rf" not in script
    assert "`whoami`" not in script
    assert "$(id)" not in script
    assert sandbox_runner._b64(hostile) in script


def test_the_prompt_names_the_container_path_not_the_host_path():
    """A host path the container cannot see makes the agent give up in seconds."""
    from pathlib import Path

    inside = sandbox_runner.container_path(
        Path("/home/bss-group/bloy-worktrees/bloy-2-api"),
        Path("/home/bss-group/bloy-worktrees"),
    )

    assert inside == "/worktrees/bloy-2-api"
    assert "/home/bss-group" not in inside


def test_setup_adopts_an_existing_uid_instead_of_creating_a_second_user():
    """Base images ship a uid-1000 account; useradd would fail on a duplicate."""
    script = sandbox_runner._setup_script("do the thing")

    assert sandbox_runner.RESOLVE_AGENT_USER in script
    assert 'if [ -z "$u" ]; then' in script


def test_the_monorepo_is_mounted_read_only_for_the_map(tmp_path):
    """CLAUDE.md lives at the monorepo root, outside every sub-project.

    The first live run reported "the repo has no CLAUDE.md" and worked without
    the map, because the worktree only contains one sub-project.
    """
    monorepo = tmp_path / "BLOY"
    monorepo.mkdir()

    volumes = sandbox_runner._volumes(tmp_path / "wt", monorepo)

    mount = next(v for v in volumes if v.mount_path == sandbox_runner.MONOREPO_MOUNT)
    assert mount.read_only is True, "the agent must not edit outside its worktree"
    writable = [v.mount_path for v in volumes if not v.read_only]
    assert writable == [sandbox_runner.WORKTREE_MOUNT], (
        f"only the worktree may be writable, got {writable}"
    )


def test_a_missing_monorepo_is_simply_not_mounted(tmp_path):
    volumes = sandbox_runner._volumes(tmp_path / "wt", tmp_path / "absent")

    assert all(v.mount_path != sandbox_runner.MONOREPO_MOUNT for v in volumes)


def test_the_prompt_tells_the_agent_to_stop_on_the_wrong_repo():
    """A ticket often belongs to a sibling project; guessing wastes a whole run."""
    from bloy_dev_agent.features.twenty import mapping

    issue = mapping.normalize_issue(
        {"id": "x", "issueKey": "BLOY-4", "title": "BLS-1064: translation bundle"}
    )

    prompt = mapping.build_prompt(
        issue, "/worktrees/bls-1064-api", implement=True, monorepo="/monorepo"
    )

    assert "/monorepo/CLAUDE.md" in prompt
    assert "only WRITE inside /worktrees/bls-1064-api" in prompt
    assert "DIFFERENT sub-project" in prompt
