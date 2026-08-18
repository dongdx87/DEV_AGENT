"""Database for the standalone BLOY Dev Agent service.

The service owns its data. Sharing BAM's SQLite file would put two processes on
one write lock while a pipeline holds transactions for minutes at a time — and
it would tie the agent's uptime to BAM's, which is exactly what running as a
separate server is meant to avoid.

``BLOY_AGENT_DB_URL`` overrides the location; the default sits beside the code
so a fresh checkout runs with no setup.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

logger = logging.getLogger(__name__)

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
    """Create missing tables, then add missing columns. Safe on every boot."""
    from bloy_dev_agent import models  # noqa: F401 — registers the mappings

    Base.metadata.create_all(engine, checkfirst=True)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """Bring an existing table up to the model, one ADD COLUMN at a time.

    ``create_all`` only ever creates whole tables, so a column added to a model
    after the first boot is silently absent and every read of it fails. This
    service has no migration framework and does not need one: SQLite can add a
    nullable column in place, and that is the only schema change this plugin
    has ever needed.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present or not column.nullable:
                    continue
                ddl = f"ALTER TABLE {table.name} ADD COLUMN {column.name} {column.type}"
                connection.execute(text(ddl))
                logger.info("bloy_dev_agent: added column %s.%s", table.name, column.name)
