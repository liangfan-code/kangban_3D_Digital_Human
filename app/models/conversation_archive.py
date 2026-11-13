"""conversation_archives：会话归档表（数据治理 段 4）。

归档后原 conversations / conversation_messages 行物理删除，
本表保留完整拷贝（含消息正文 JSONB）供合规留档；thread_id 无外键。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, pk_uuid


class ConversationArchive(Base):
    __tablename__ = "conversation_archives"
    __table_args__ = (
        UniqueConstraint("thread_id", name="uq_conversation_archives_thread"),
        Index("ix_conversation_archives_elder_started", "elder_id", "started_at"),
    )

    archive_id: Mapped[uuid.UUID] = pk_uuid()
    thread_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    elder_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    device_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    session_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    identity_source: Mapped[str] = mapped_column(String(24), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False)
    messages: Mapped[list] = mapped_column(JSONB, nullable=False)
    archived_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
