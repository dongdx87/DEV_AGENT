"""Tests for reading the agent's streamed reasoning.

These event shapes are not a stable contract, so the parser has to survive
lines it does not recognise — including the half-written last line that a live
tail always sees.
"""

from __future__ import annotations

import json

from bloy_dev_agent.features import agent_log


def _write(tmp_path, *events):
    path = tmp_path / "run.jsonl"
    path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )
    return path


def _assistant(*blocks):
    return {"type": "assistant", "message": {"content": list(blocks)}}


def test_thinking_tool_and_text_are_flattened_in_order(tmp_path):
    path = _write(
        tmp_path,
        _assistant({"type": "thinking", "thinking": "đọc CLAUDE.md trước"}),
        _assistant({"type": "tool_use", "name": "Read", "input": {"file_path": "/a/b.ts"}}),
        _assistant({"type": "text", "text": "đã sửa helper"}),
        {"type": "result", "subtype": "success", "result": "xong", "is_error": False},
    )

    events = agent_log.parse(path)

    assert [e.kind for e in events] == ["thinking", "tool", "text", "result"]
    assert events[0].text == "đọc CLAUDE.md trước"
    assert (events[1].tool, events[1].text) == ("Read", "/a/b.ts")


def test_a_failed_result_is_marked_as_an_error(tmp_path):
    path = _write(
        tmp_path, {"type": "result", "result": "hết credit", "is_error": True}
    )

    (event,) = agent_log.parse(path)

    assert event.kind == agent_log.KIND_ERROR


def test_a_half_written_last_line_is_skipped(tmp_path):
    """Tailing a live file always catches a partial line eventually."""
    path = tmp_path / "run.jsonl"
    path.write_text(
        json.dumps(_assistant({"type": "text", "text": "ok"})) + "\n{\"type\": \"assi",
        encoding="utf-8",
    )

    events = agent_log.parse(path)

    assert [e.kind for e in events] == ["text"]


def test_unknown_shapes_do_not_break_the_page(tmp_path):
    path = _write(
        tmp_path,
        {"type": "system", "subtype": "init"},
        ["not", "a", "dict"],
        _assistant({"type": "some_future_block"}),
        _assistant({"type": "text", "text": "vẫn đọc được"}),
    )

    events = agent_log.parse(path)

    assert [e.text for e in events] == ["vẫn đọc được"]


def test_a_missing_log_is_empty_not_an_error(tmp_path):
    assert agent_log.parse(tmp_path / "absent.jsonl") == []
    assert agent_log.final_text(tmp_path / "absent.jsonl") == ""


def test_a_huge_tool_input_is_truncated(tmp_path):
    """Tool inputs can hold a whole file; the admin page must stay readable."""
    path = _write(
        tmp_path,
        _assistant({"type": "tool_use", "name": "Write", "input": {"file_path": "x" * 5000}}),
    )

    (event,) = agent_log.parse(path)

    assert len(event.text) <= 200


def test_limit_keeps_the_most_recent_events(tmp_path):
    path = _write(
        tmp_path,
        *[_assistant({"type": "text", "text": str(i)}) for i in range(50)],
    )

    events = agent_log.parse(path, limit=5)

    assert [e.text for e in events] == ["45", "46", "47", "48", "49"]


def test_final_text_prefers_the_result_event(tmp_path):
    path = _write(
        tmp_path,
        _assistant({"type": "text", "text": "đang làm"}),
        {"type": "result", "result": "báo cáo cuối", "is_error": False},
    )

    assert agent_log.final_text(path) == "báo cáo cuối"


def test_final_text_falls_back_when_the_run_was_killed(tmp_path):
    """A killed run writes no result; reporting nothing would hide real work."""
    path = _write(
        tmp_path,
        _assistant({"type": "text", "text": "đã sửa 3 file"}),
        _assistant({"type": "tool_use", "name": "Edit", "input": {"file_path": "/a"}}),
    )

    assert agent_log.final_text(path) == "đã sửa 3 file"


def test_the_log_lives_outside_every_worktree(tmp_path):
    """A log inside a worktree would show up as an untracked change and be committed."""
    root = tmp_path / "worktrees"

    path = agent_log.host_log_path(root, "abc123")

    assert path.parent == root / agent_log.LOG_DIR_NAME
    assert agent_log.LOG_DIR_NAME.startswith("."), "hidden so tooling skips it"
    assert agent_log.container_log_path("/worktrees", "abc123") == (
        f"/worktrees/{agent_log.LOG_DIR_NAME}/abc123.jsonl"
    )


def test_the_skills_dir_also_lives_outside_every_worktree(tmp_path):
    """Same reasoning as the log: a copy inside a worktree gets committed."""
    root = tmp_path / "worktrees"

    path = agent_log.host_skills_dir(root, "abc123")

    assert path == root / agent_log.SKILLS_DIR_NAME / "abc123"
    assert agent_log.SKILLS_DIR_NAME.startswith("."), "hidden so tooling skips it"
    assert agent_log.container_skills_dir("/worktrees", "abc123") == (
        f"/worktrees/{agent_log.SKILLS_DIR_NAME}/abc123"
    )


def test_progress_counts_what_the_dashboard_shows(tmp_path):
    path = _write(
        tmp_path,
        _assistant({"type": "thinking", "thinking": "a"}),
        _assistant({"type": "tool_use", "name": "Grep", "input": {"pattern": "x"}}),
        _assistant({"type": "tool_use", "name": "Edit", "input": {"file_path": "/y"}}),
    )

    counts = agent_log.progress(path)

    assert counts["thinking"] == 1
    assert counts["tools"] == 2
    assert counts["last_tool"] == "Edit"
