"""Database for the standalone BLOY Dev Agent service.

The service owns its data. Sharing BAM's SQLite file would put two processes on
one write lock while a pipeline holds transactions for minutes at a time — and
it would tie the agent's uptime to BAM's, which is exactly what running as a
separate server is meant to avoid.

``BLOY_AGENT_DB_URL`` overrides the location; the default sits beside the code
so a fresh checkout runs with no setup.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_ROOT / "db" / "bloy_dev_agent.sqlite3"


class Base(DeclarativeBase):
    """Declarative base for this service's own tables."""


def database_url() -> str:
    configured = os.environ.get("BLOY_AGENT_DB_URL", "").strip()
    if configured:
        return configured
    DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{DEFAULT_DB_PATH}"


def _build_engine():
    url = database_url()
    # ``check_same_thread`` off because the pipeline runs in a worker thread
    # while HTTP handlers read from the event loop's threads.
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args, future=True)


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """Create any missing tables. Safe to call on every boot."""
    from bloy_dev_agent import models  # noqa: F401 — registers the mappings

    Base.metadata.create_all(engine, checkfirst=True)
