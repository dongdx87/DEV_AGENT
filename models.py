"""SQLAlchemy models for the BLOY Dev Agent plugin.

These tables belong to the standalone service and live in its own database
(:mod:`bloy_dev_agent.db`), not in BAM's. The ``plugin_bloy_*`` prefix is kept
so an existing install's data is still recognisable after the split.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from bloy_dev_agent.db import Base


class BloyAgentRun(Base):
    """One issue handed to a BAM agent, awaiting or carrying its result.

    Separate from the task link because the lifetimes differ: a record maps to
    one task, but can be run by an agent repeatedly.
    """

    __tablename__ = "plugin_bloy_agent_run"

    #: Run states. ``pending`` means the job is queued or running; ``reported``
    #: means its outcome already reached Twenty, so later passes skip it.
    STATE_PENDING = "pending"
    STATE_REPORTED = "reported"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    issue_id: Mapped[str] = mapped_column(String(64), nullable=False)
    issue_key: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Row id in the core long-horizon queue (``core_agent_jobs``).
    job_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    agent_alias: Mapped[str] = mapped_column(String(128), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, index=True)

    #: Where to move the issue once the job finishes, remembered at dispatch so
    #: the collecting pass does not need the routine's configuration again.
    done_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_status: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class BloyPipelineRun(Base):
    """One *attempt* at one issue, from claim to merge request.

    A row per attempt rather than per issue is what makes the attempt cap
    possible: an unattended agent that keeps failing on the same ticket burns
    money on every pass, so the pipeline counts the failures here and refuses
    to start once they reach the configured ceiling.

    The same rows drive the admin page — stage plus timestamps is the progress
    bar, and ``log_path`` points at the file the agent streams its reasoning
    into while it works.
    """

    __tablename__ = "plugin_bloy_pipeline_run"

    #: Live states.
    STATE_RUNNING = "running"
    STATE_SUCCESS = "success"
    STATE_FAILED = "failed"
    #: Refused before doing any work because the attempt cap was reached.
    STATE_BLOCKED = "blocked"
    #: Gave up *before* the sandbox started — a Twenty timeout, an unusable
    #: worktree. It consumed no container and no agent session, so it must not
    #: count against the attempt cap: the cap exists to limit spending, and a
    #: network blip spends nothing. Observed live: one 15s PATCH timeout on the
    #: claim burned an attempt for zero work.
    STATE_ABORTED = "aborted"

    TERMINAL_STATES = (STATE_SUCCESS, STATE_FAILED, STATE_BLOCKED, STATE_ABORTED)

    #: Ordered pipeline stages, used for the progress display.
    STAGES = ("claim", "worktree", "sandbox", "verify", "commit", "push", "done")

    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    issue_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    issue_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    issue_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_repo: Mapped[str | None] = mapped_column(String(128), nullable=True)

    #: 1 for the first try. Counted per issue, never reset automatically.
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    state: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    stage: Mapped[str | None] = mapped_column(String(32), nullable=True)

    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    merge_request_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    sandbox_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    #: Absolute path of the agent's streamed reasoning log, tailed by the UI.
    log_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    changed: Mapped[str | None] = mapped_column(Text, nullable=True)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class BloySetting(Base):
    """Plugin configuration that outlives any single routine.

    The routine action already has per-routine config fields, but the attempt
    cap has to hold even when the pipeline is invoked outside a routine — so it
    lives here, editable from the plugin's own page.
    """

    __tablename__ = "plugin_bloy_setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class BloyTwentyTaskLink(Base):
    """Maps one Twenty record to the Agent Team task created from it.

    Kept in this plugin's own table rather than as columns on the Agent Team
    task row: migrations here must never alter another plugin's schema.
    """

    __tablename__ = "plugin_bloy_twenty_task_link"
    __table_args__ = (
        UniqueConstraint("board_id", "twenty_id", name="uq_bloy_link_board_record"),
        UniqueConstraint("task_id", name="uq_bloy_link_task"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    #: Agent Team board this record was synced into.
    board_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    #: Record id on the Twenty side (never a field we created — Twenty's own id).
    twenty_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    twenty_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    #: Task id on the Agent Team side.
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Hash of the last payload applied, so an unchanged record is a no-op.
    last_payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Last state pushed back to Twenty; lets a restart avoid redundant writes.
    last_pushed_state: Mapped[str | None] = mapped_column(String(32), nullable=True)

    #: Set when the Agent Team task no longer exists (orphan sweep marks it).
    orphaned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    note: Mapped[str | None] = mapped_column(Text, nullable=True)
