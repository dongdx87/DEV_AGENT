"""Read the agent's streamed reasoning out of a run log.

The agent runs inside a container as one blocking call, so there is nothing to
watch — unless it writes as it goes. Passing ``--output-format stream-json``
makes the CLI emit one JSON object per event, and because the log lives on the
bind-mounted volume the host can tail the same file while the container is
still working. That is what turns the admin page from a spinner into a live
view.

The parser is deliberately forgiving. These event shapes are not a stable
contract, so an unrecognised line is skipped rather than allowed to break the
page that displays it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Directory holding run logs, as a child of the worktree root. Leading dot and
#: a location outside every worktree keep it invisible to git — a log file
#: inside a worktree would show up as an untracked change and get committed.
LOG_DIR_NAME = ".bloy-logs"

KIND_THINKING = "thinking"
KIND_TEXT = "text"
KIND_TOOL = "tool"
KIND_RESULT = "result"
KIND_ERROR = "error"


@dataclass
class Event:
    """One thing the agent did, flattened for display."""

    kind: str
    text: str
    tool: str = ""


def log_dir(worktree_root: Path) -> Path:
    return worktree_root / LOG_DIR_NAME


def host_log_path(worktree_root: Path, run_id: str) -> Path:
    """Where the host reads the log for ``run_id``."""
    return log_dir(worktree_root) / f"{run_id}.jsonl"


def container_log_path(mount: str, run_id: str) -> str:
    """The same file as the container sees it."""
    return f"{mount}/{LOG_DIR_NAME}/{run_id}.jsonl"


def _tool_summary(block: dict) -> str:
    """A short, safe description of a tool call.

    Only a few well-known keys are surfaced: tool inputs can hold an entire
    file, and the page must stay readable.
    """
    data = block.get("input") or {}
    for key in ("file_path", "path", "pattern", "command", "url", "query"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return ""


def _from_assistant(message: dict) -> list[Event]:
    events: list[Event] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "thinking":
            text = str(block.get("thinking") or "").strip()
            if text:
                events.append(Event(KIND_THINKING, text))
        elif kind == "text":
            text = str(block.get("text") or "").strip()
            if text:
                events.append(Event(KIND_TEXT, text))
        elif kind == "tool_use":
            events.append(
                Event(KIND_TOOL, _tool_summary(block), tool=str(block.get("name") or ""))
            )
    return events


def parse(path: Path, *, limit: int = 400) -> list[Event]:
    """Flatten a stream-json log into display events, oldest first.

    ``limit`` caps how many events are returned, keeping the most recent ones —
    a long run produces thousands and the page only needs the tail.
    """
    if not path.exists():
        return []

    events: list[Event] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    # A partially written last line is normal while tailing.
                    continue
                if not isinstance(payload, dict):
                    continue

                kind = payload.get("type")
                if kind == "assistant":
                    events.extend(_from_assistant(payload.get("message") or {}))
                elif kind == "result":
                    text = str(payload.get("result") or "").strip()
                    events.append(
                        Event(
                            KIND_ERROR if payload.get("is_error") else KIND_RESULT,
                            text,
                        )
                    )
    except OSError:
        logger.exception("bloy_dev_agent: could not read agent log %s", path)
        return events

    return events[-limit:] if len(events) > limit else events


def final_text(path: Path) -> str:
    """The agent's closing answer, or the last thing it said.

    Falling back to the last text block matters: a run killed mid-flight never
    writes a ``result`` event, and reporting "nothing" there would hide work
    the agent had already described.
    """
    events = parse(path, limit=10_000)
    for event in reversed(events):
        if event.kind in (KIND_RESULT, KIND_ERROR) and event.text:
            return event.text
    for event in reversed(events):
        if event.kind == KIND_TEXT and event.text:
            return event.text
    return ""


def progress(path: Path) -> dict:
    """Counts the page uses to show movement without printing everything."""
    events = parse(path, limit=10_000)
    return {
        "events": len(events),
        "thinking": sum(1 for e in events if e.kind == KIND_THINKING),
        "tools": sum(1 for e in events if e.kind == KIND_TOOL),
        "last_tool": next(
            (e.tool for e in reversed(events) if e.kind == KIND_TOOL and e.tool), ""
        ),
    }
