"""Persistence for pipeline runs and plugin settings.

Kept apart from :mod:`bloy_dev_agent.features.pipeline` so the pipeline stays
readable as a sequence of steps, and so the admin page can query history
without importing the machinery that produces it.

Every function opens and closes its own session. The pipeline runs for minutes
at a time inside a worker process; holding one session across that would pin a
connection and, on SQLite, keep a write lock far longer than any read needs.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from bloy_dev_agent.db import SessionLocal
from bloy_dev_agent.models import BloyPipelineRun, BloySetting

logger = logging.getLogger(__name__)

#: Stop after this many failed attempts on one issue. An unattended agent that
#: cannot solve a ticket will not solve it on the sixth pass either, and every
#: pass costs a sandbox plus a full agent session.
DEFAULT_MAX_ATTEMPTS = 5

SETTING_MAX_ATTEMPTS = "max_attempts"
SETTING_PROJECT_ID = "project_id"
SETTING_TARGET_REPO = "target_repo"
SETTING_MONOREPO = "monorepo"
SETTING_SOURCE_STATUS = "source_status"
SETTING_WORKING_STATUS = "working_status"
SETTING_DONE_STATUS = "done_status"
SETTING_ERROR_STATUS = "error_status"
SETTING_BLOCKED_STATUS = "blocked_status"
SETTING_TIMEOUT_MINUTES = "timeout_minutes"

#: Twenty credentials. Stored here so a new machine is configured from the
#: browser rather than by editing someone else's ``.env`` — the environment is
#: still honoured, and still wins, for deployments that inject secrets that way.
SETTING_TWENTY_URL = "twenty_base_url"
SETTING_TWENTY_KEY = "twenty_api_key"

#: Prefix used when the setup page clones a missing sub-project.
SETTING_GIT_REMOTE = "git_remote"


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def get_settings() -> dict[str, str]:
    """All stored settings as a plain dict."""
    session = SessionLocal()
    try:
        return {row.key: (row.value or "") for row in session.query(BloySetting).all()}
    except Exception:  # noqa: BLE001 — a settings read must never break a page
        logger.exception("bloy_dev_agent: could not read settings")
        return {}
    finally:
        session.close()


def save_settings(values: dict[str, str]) -> None:
    """Upsert the given settings, leaving any key not mentioned untouched."""
    session = SessionLocal()
    try:
        for key, value in values.items():
            row = session.get(BloySetting, key)
            if row is None:
                session.add(BloySetting(key=key, value=value, updated_at=_now()))
            else:
                row.value = value
                row.updated_at = _now()
        session.commit()
    except Exception:  # noqa: BLE001
        session.rollback()
        logger.exception("bloy_dev_agent: could not save settings")
        raise
    finally:
        session.close()


def max_attempts() -> int:
    """Configured attempt ceiling, or the default when unset or nonsense.

    A misconfigured ``0`` would block every issue forever, so the floor is 1.
    """
    raw = get_settings().get(SETTING_MAX_ATTEMPTS, "")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# Attempts
# ---------------------------------------------------------------------------


@dataclass
class AttemptStatus:
    """How many times an issue has been tried, and whether it may run again."""

    issue_key: str
    failed: int
    limit: int

    @property
    def exhausted(self) -> bool:
        return self.failed >= self.limit

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.failed)


def attempt_status(issue_id: str, issue_key: str = "") -> AttemptStatus:
    """Count failed attempts for one issue against the configured ceiling.

    Only ``failed`` rows count. A ``blocked`` row is the record of the refusal
    itself — counting it too would shift the ceiling down by one every time the
    issue is retried, and ``running`` rows have not failed yet.
    """
    limit = max_attempts()
    session = SessionLocal()
    try:
        failed = (
            session.query(BloyPipelineRun)
            .filter(
                BloyPipelineRun.issue_id == issue_id,
                BloyPipelineRun.state == BloyPipelineRun.STATE_FAILED,
            )
            .count()
        )
    except Exception:  # noqa: BLE001
        logger.exception("bloy_dev_agent: could not count attempts for %s", issue_key)
        failed = 0
    finally:
        session.close()
    return AttemptStatus(issue_key=issue_key, failed=failed, limit=limit)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def start_run(
    *,
    issue_id: str,
    issue_key: str,
    issue_title: str = "",
    project_id: str,
    target_repo: str = "",
    attempt: int,
    log_path: str = "",
) -> str:
    """Record a run that is about to begin and return its id."""
    run_id = uuid.uuid4().hex
    session = SessionLocal()
    try:
        session.add(
            BloyPipelineRun(
                id=run_id,
                issue_id=issue_id,
                issue_key=issue_key,
                issue_title=issue_title[:512] or None,
                project_id=project_id,
                target_repo=target_repo or None,
                attempt=attempt,
                state=BloyPipelineRun.STATE_RUNNING,
                stage="claim",
                log_path=log_path or None,
                started_at=_now(),
            )
        )
        session.commit()
    except Exception:  # noqa: BLE001 — bookkeeping must not abort the work
        session.rollback()
        logger.exception("bloy_dev_agent: could not record the start of a run")
    finally:
        session.close()
    return run_id


def set_stage(run_id: str, stage: str, **fields) -> None:
    """Advance a run's stage, optionally stamping fields discovered there."""
    _update(run_id, stage=stage, **fields)


def finish_run(
    run_id: str,
    *,
    state: str,
    stage: str = "",
    detail: str = "",
    **fields,
) -> None:
    """Close a run out with its final state."""
    _update(
        run_id,
        state=state,
        stage=stage or None,
        detail=detail or None,
        finished_at=_now(),
        **fields,
    )


def record_blocked(
    *,
    issue_id: str,
    issue_key: str,
    issue_title: str = "",
    project_id: str,
    target_repo: str = "",
    attempt: int,
    detail: str,
) -> str:
    """Write the refusal itself down, so the page explains why nothing ran."""
    run_id = start_run(
        issue_id=issue_id,
        issue_key=issue_key,
        issue_title=issue_title,
        project_id=project_id,
        target_repo=target_repo,
        attempt=attempt,
    )
    finish_run(run_id, state=BloyPipelineRun.STATE_BLOCKED, stage="claim", detail=detail)
    return run_id


def _update(run_id: str, **fields) -> None:
    if not run_id:
        return
    session = SessionLocal()
    try:
        run = session.get(BloyPipelineRun, run_id)
        if run is None:
            return
        for key, value in fields.items():
            if value is not None or key in {"stage", "detail"}:
                setattr(run, key, value)
        session.commit()
    except Exception:  # noqa: BLE001
        session.rollback()
        logger.exception("bloy_dev_agent: could not update run %s", run_id)
    finally:
        session.close()


def reap_stale_runs() -> list[BloyPipelineRun]:
    """Close out runs left ``running`` by a process that no longer exists.

    Only this service executes passes, and only one at a time, so at boot any
    row still marked running belongs to a process that was killed — a restart, a
    crash, an operator with a reason. Left alone those rows show as "đang chạy"
    forever and their issues sit in the working column where nothing will pick
    them up again.

    Counting them as failures is deliberate: the attempt did consume a sandbox,
    and a run nobody finished is not a success.

    Returns the reaped rows so the caller can put their issues back.
    """
    session = SessionLocal()
    try:
        rows = (
            session.query(BloyPipelineRun)
            .filter(BloyPipelineRun.state == BloyPipelineRun.STATE_RUNNING)
            .all()
        )
        for row in rows:
            row.state = BloyPipelineRun.STATE_FAILED
            row.detail = (
                "Run bị bỏ dở — process chạy nó không còn tồn tại (service restart "
                "hoặc bị kill). Tính là một lần thất bại vì nó đã tiêu một sandbox."
            )
            row.finished_at = _now()
        session.commit()
        for row in rows:
            session.expunge(row)
        if rows:
            logger.warning("bloy_dev_agent: reaped %d stale run(s)", len(rows))
        return rows
    except Exception:  # noqa: BLE001
        session.rollback()
        logger.exception("bloy_dev_agent: could not reap stale runs")
        return []
    finally:
        session.close()


def minutes_since_last_pass() -> float | None:
    """Minutes since the newest run started, or ``None`` when there are none.

    This is how the page detects "nobody is triggering me". The service has no
    scheduler of its own — BAM's routine calls it — and when that scheduler dies
    the dashboard would otherwise look identical to "there is no work", which is
    exactly the trap that hid a dead scheduler for hours.
    """
    session = SessionLocal()
    try:
        newest = (
            session.query(BloyPipelineRun)
            .order_by(BloyPipelineRun.started_at.desc())
            .first()
        )
        if newest is None or newest.started_at is None:
            return None
        started = newest.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        return max(0.0, (_now() - started).total_seconds() / 60.0)
    except Exception:  # noqa: BLE001
        logger.exception("bloy_dev_agent: could not read the last pass time")
        return None
    finally:
        session.close()


def recent_runs(limit: int = 30) -> list[BloyPipelineRun]:
    """Most recent runs first, detached so templates can read them safely."""
    session = SessionLocal()
    try:
        rows = (
            session.query(BloyPipelineRun)
            .order_by(BloyPipelineRun.started_at.desc())
            .limit(limit)
            .all()
        )
        for row in rows:
            session.expunge(row)
        return rows
    except Exception:  # noqa: BLE001
        logger.exception("bloy_dev_agent: could not list runs")
        return []
    finally:
        session.close()


def get_run(run_id: str) -> BloyPipelineRun | None:
    session = SessionLocal()
    try:
        run = session.get(BloyPipelineRun, run_id)
        if run is not None:
            session.expunge(run)
        return run
    except Exception:  # noqa: BLE001
        logger.exception("bloy_dev_agent: could not read run %s", run_id)
        return None
    finally:
        session.close()


def active_runs() -> list[BloyPipelineRun]:
    """Runs still in flight — what the dashboard shows as "running now"."""
    session = SessionLocal()
    try:
        rows = (
            session.query(BloyPipelineRun)
            .filter(BloyPipelineRun.state == BloyPipelineRun.STATE_RUNNING)
            .order_by(BloyPipelineRun.started_at.desc())
            .all()
        )
        for row in rows:
            session.expunge(row)
        return rows
    except Exception:  # noqa: BLE001
        logger.exception("bloy_dev_agent: could not list active runs")
        return []
    finally:
        session.close()


def blocked_issues() -> list[BloyPipelineRun]:
    """One row per issue that hit the ceiling, newest refusal first."""
    seen: set[str] = set()
    out: list[BloyPipelineRun] = []
    for run in recent_runs(limit=200):
        if run.state == BloyPipelineRun.STATE_BLOCKED and run.issue_id not in seen:
            seen.add(run.issue_id)
            out.append(run)
    return out


def reset_attempts(issue_id: str) -> int:
    """Clear an issue's failure history so it may be tried again.

    The cap is deliberately sticky — nothing expires it on a timer — so a human
    who has fixed the underlying problem needs an explicit way to release it.
    Returns how many rows were dropped.
    """
    session = SessionLocal()
    try:
        rows = (
            session.query(BloyPipelineRun)
            .filter(
                BloyPipelineRun.issue_id == issue_id,
                BloyPipelineRun.state.in_(
                    [BloyPipelineRun.STATE_FAILED, BloyPipelineRun.STATE_BLOCKED]
                ),
            )
            .all()
        )
        for row in rows:
            session.delete(row)
        session.commit()
        return len(rows)
    except Exception:  # noqa: BLE001
        session.rollback()
        logger.exception("bloy_dev_agent: could not reset attempts for %s", issue_id)
        return 0
    finally:
        session.close()
