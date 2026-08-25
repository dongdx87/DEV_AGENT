"""Per-run capability tokens — the only thing a sandbox authenticates with.

A token grants exactly one thing: the right to deploy *this run's own*
worktrees to staging, for a bounded time, at a bounded rate. It is not a
credential shared across runs, and it is never the SSH key or any real deploy
secret — the sandbox still never holds one of those (see
``features/pipeline.py``'s docstring on why the container never gets the SSH
key; this is the same boundary, extended to staging).
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bloy_dev_agent.db import SessionLocal
from bloy_dev_agent.models import BloyStagingToken

logger = logging.getLogger(__name__)

#: Slack added on top of the run's own timeout, so a token does not expire out
#: from under a run that overran slightly — but still expires well before an
#: abandoned one could be reused by anything else.
TOKEN_TTL_SLACK_MINUTES = 10

#: Deploys are cheap for api (no build), expensive for cms (full frontend
#: build) — one shared ceiling per run keeps a misbehaving prompt from
#: hammering staging for the run's whole (possibly 90-minute) timeout.
MAX_DEPLOYS_PER_TOKEN = 12
MIN_SECONDS_BETWEEN_DEPLOYS = 45


def _now() -> datetime:
    return datetime.now(UTC)


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StagingGrant:
    """What a resolved, valid token is allowed to do."""

    run_id: str
    issue_key: str
    worktrees: dict[str, Path]


@dataclass(frozen=True)
class ActiveStaging:
    """The one run currently holding a live token, if any."""

    run_id: str
    issue_key: str


def active() -> ActiveStaging | None:
    """The run holding a live token right now, or ``None``.

    Staging is a single shared environment — at most one run may deploy to it
    at a time. ``pipeline.run_issue`` checks this before minting a second
    token so two tickets can never rsync over each other's in-flight deploy.
    """
    session = SessionLocal()
    try:
        row = (
            session.query(BloyStagingToken)
            .filter(
                BloyStagingToken.revoked_at.is_(None),
                BloyStagingToken.expires_at > _now(),
            )
            .first()
        )
        return ActiveStaging(run_id=row.run_id, issue_key=row.issue_key) if row else None
    except Exception:  # noqa: BLE001 — an unreadable check must not crash a run
        logger.exception("bloy_dev_agent: could not check for an active staging token")
        return None
    finally:
        session.close()


def revoke_all_active() -> int:
    """Revoke every still-active token — called once at service boot.

    Only this service ever runs a pass, and only one at a time, so any token
    still "active" when the process starts belongs to a run a killed process
    never got to finish — the same reasoning as ``store.reap_stale_runs``.
    Returns how many rows were revoked.
    """
    session = SessionLocal()
    try:
        rows = (
            session.query(BloyStagingToken)
            .filter(BloyStagingToken.revoked_at.is_(None))
            .all()
        )
        for row in rows:
            row.revoked_at = _now()
        session.commit()
        if rows:
            logger.warning(
                "bloy_dev_agent: revoked %d stale staging token(s) at boot", len(rows)
            )
        return len(rows)
    except Exception:  # noqa: BLE001
        session.rollback()
        logger.exception("bloy_dev_agent: could not revoke active staging tokens at boot")
        return 0
    finally:
        session.close()


def mint(
    run_id: str, *, issue_key: str, worktrees: dict[str, Path], ttl_minutes: int
) -> str:
    """Create a token for ``run_id`` and return the raw value.

    The raw value is returned exactly once — only its hash is ever stored —
    so losing this return value means the token can never be presented again,
    which is fine: a run that lost its own token simply cannot deploy.
    """
    raw = secrets.token_urlsafe(32)
    expires_at = _now() + timedelta(minutes=ttl_minutes + TOKEN_TTL_SLACK_MINUTES)
    session = SessionLocal()
    try:
        session.merge(
            BloyStagingToken(
                run_id=run_id,
                token_sha256=_hash(raw),
                issue_key=issue_key,
                worktrees_json=json.dumps({k: str(v) for k, v in worktrees.items()}),
                expires_at=expires_at,
                revoked_at=None,
                deploys_used=0,
                created_at=_now(),
            )
        )
        session.commit()
    except Exception:  # noqa: BLE001 — minting must not crash the pipeline
        session.rollback()
        logger.exception("bloy_dev_agent: could not mint a staging token for %s", run_id)
        raise
    finally:
        session.close()
    return raw


def resolve(raw: str) -> StagingGrant | None:
    """The grant behind a presented token, or ``None`` if it is not usable.

    Deliberately silent about *why* a token failed (unknown vs expired vs
    revoked) in the return value — the HTTP layer reports a flat 401 either
    way, so a sandbox probing for a hint about which case it hit learns
    nothing more than "try again with a real one".
    """
    session = SessionLocal()
    try:
        row = (
            session.query(BloyStagingToken)
            .filter(BloyStagingToken.token_sha256 == _hash(raw))
            .first()
        )
        if row is None or row.revoked_at is not None:
            return None
        expires_at = row.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at < _now():
            return None
        worktrees = {k: Path(v) for k, v in json.loads(row.worktrees_json).items()}
        return StagingGrant(run_id=row.run_id, issue_key=row.issue_key, worktrees=worktrees)
    except Exception:  # noqa: BLE001 — a lookup failure must read as "unauthorized"
        logger.exception("bloy_dev_agent: could not resolve a staging token")
        return None
    finally:
        session.close()


def check_rate_limit(run_id: str) -> str:
    """``""`` if a deploy may proceed now, else a human-readable refusal reason."""
    session = SessionLocal()
    try:
        row = session.get(BloyStagingToken, run_id)
        if row is None or row.revoked_at is not None:
            return "token không còn hiệu lực"
        if row.deploys_used >= MAX_DEPLOYS_PER_TOKEN:
            return f"đã deploy {row.deploys_used} lần, vượt giới hạn {MAX_DEPLOYS_PER_TOKEN}/run"
        if row.last_deploy_at is not None:
            last = row.last_deploy_at
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            elapsed = (_now() - last).total_seconds()
            if elapsed < MIN_SECONDS_BETWEEN_DEPLOYS:
                wait = int(MIN_SECONDS_BETWEEN_DEPLOYS - elapsed)
                return f"deploy quá nhanh, chờ thêm {wait}s"
        return ""
    finally:
        session.close()


def record_deploy(run_id: str) -> None:
    session = SessionLocal()
    try:
        row = session.get(BloyStagingToken, run_id)
        if row is not None:
            row.deploys_used += 1
            row.last_deploy_at = _now()
            session.commit()
    except Exception:  # noqa: BLE001 — bookkeeping must not fail the deploy itself
        session.rollback()
        logger.exception("bloy_dev_agent: could not record a deploy for %s", run_id)
    finally:
        session.close()


def revoke(run_id: str) -> None:
    """Called from every exit path of a run — see pipeline.py's ``_finish``."""
    if not run_id:
        return
    session = SessionLocal()
    try:
        row = session.get(BloyStagingToken, run_id)
        if row is not None and row.revoked_at is None:
            row.revoked_at = _now()
            session.commit()
    except Exception:  # noqa: BLE001 — revocation must never break the caller
        session.rollback()
        logger.exception("bloy_dev_agent: could not revoke staging token for %s", run_id)
    finally:
        session.close()


def purge_expired() -> int:
    """Delete rows past their expiry, so the table does not grow forever."""
    session = SessionLocal()
    try:
        rows = session.query(BloyStagingToken).filter(BloyStagingToken.expires_at < _now()).all()
        for row in rows:
            session.delete(row)
        session.commit()
        return len(rows)
    except Exception:  # noqa: BLE001
        session.rollback()
        logger.exception("bloy_dev_agent: could not purge expired staging tokens")
        return 0
    finally:
        session.close()
