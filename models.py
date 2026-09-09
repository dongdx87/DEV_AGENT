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
    #: The first merge request, kept for older rows and simple callers.
    merge_request_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    #: Every merge request the run opened, as JSON ``[[repo, url], ...]``. One
    #: ticket can span several sub-projects, and a single link would send the
    #: reviewer to whichever repo happened to be first — they would see half the
    #: change and no sign the other half existed.
    merge_requests_json: Mapped[str | None] = mapped_column(Text, nullable=True)
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


class BloyStagingToken(Base):
    """A capability grant letting one pipeline run deploy to shared staging.

    Not an API key: the token is minted per run, scoped to that run's own
    worktrees, and revoked the moment the run ends (see
    ``features/pipeline.py``'s ``_finish``). Only the SHA-256 is stored, never
    the raw token — the raw value exists only in the sandbox's environment and
    in the moment it was minted.
    """

    __tablename__ = "plugin_bloy_staging_token"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    token_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    issue_key: Mapped[str] = mapped_column(String(64), nullable=False)
    #: JSON object ``{repo_name: host_worktree_path}`` — the only paths this
    #: token's holder may rsync from. A caller-supplied path is never trusted.
    worktrees_json: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deploys_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_deploy_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class BloyLoopAttempt(Base):
    """One generator+evaluator attempt inside a run's coding loop.

    Persisted rather than kept only in the report because the report is a
    comment on a ticket — editable, deletable, and gone from this service's view
    the moment someone tidies the thread. When a reviewer asks why a ticket took
    three attempts, or why the loop stopped, these rows are the answer.
    """

    __tablename__ = "plugin_bloy_loop_attempt"
    __table_args__ = (UniqueConstraint("run_id", "attempt", name="uq_bloy_loop_attempt"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)

    #: ``pass`` / ``fail`` / ``needs_human``, or empty when the evaluator never
    #: produced a readable verdict. Empty is meaningful: it is what the stall
    #: guard counts, so it must be distinguishable from a real ``fail``.
    verdict: Mapped[str | None] = mapped_column(String(16), nullable=True)
    score: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: The evaluator's own words about what was missing — the text that was fed
    #: to the next attempt verbatim, kept so a human can see what the agent was
    #: actually told rather than inferring it.
    missing: Mapped[str | None] = mapped_column(Text, nullable=True)
    generator_ok: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class BloyVerificationReceipt(Base):
    """One verify command **this service** ran, and what it returned.

    The point of the table is provenance, not convenience: an agent can write
    "tests pass" into its answer, but it cannot write a row here — only the
    service that executed the command does. The completion decision reads these,
    never the agent's claim (see ``features/coding/receipts.py``).

    ``output`` is the tail of what the command printed; ``output_sha256`` is over
    the *whole* output, so a truncated row can still be shown to be the one that
    was produced.
    """

    __tablename__ = "plugin_bloy_verification_receipt"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    batch_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    repo: Mapped[str] = mapped_column(String(128), nullable=False)
    command: Mapped[str] = mapped_column(Text, nullable=False)
    exit_code: Mapped[int] = mapped_column(Integer, nullable=False, default=-1)
    #: 1 only when the command ran AND exited zero. A command that never ran
    #: (container died, call raised) has ``ok=0`` and a non-empty ``error``,
    #: which is a different fact from "the check failed".
    ok: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class BloyFeedbackRound(Base):
    """One reviewer comment that started a revision round.

    Keyed by the comment that triggered it, uniquely, and written **before** any
    work starts. That ordering is the whole safety property: the Twenty poll
    runs every few minutes while a revision takes many of them, so without a
    claim recorded up front the same comment would start a second container on
    the next tick, and a third on the one after that.
    """

    __tablename__ = "plugin_bloy_feedback_round"

    #: The triggering comment's Twenty id. Primary key, so a duplicate insert
    #: fails rather than needing a read-then-write nobody can make atomic.
    comment_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    issue_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    issue_key: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The pipeline run this round started, once it has one.
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: First 500 chars of what the reviewer asked for, so the dashboard can show
    #: why a finished ticket went back to work without another Twenty call.
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
