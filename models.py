"""SQLAlchemy models for the BLOY Dev Agent plugin.

All tables use the shared ``core.database.base.Base`` and the
``plugin_bloy_*`` prefix. This plugin never adds columns to another plugin's
tables — the Twenty ↔ task relation lives in its own link table so upgrading
or removing either side stays clean.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base


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
