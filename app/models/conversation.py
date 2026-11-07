"""conversations ORM：会话索引（对应原 conversation_threads 语义）。

thread_id 为会话主键与外部追踪标识；session_seq 为该长者的会话序号，
(elder_id, session_seq) 唯一落实会话生成语义。匿名通用模式允许 elder_id 为空，
但不得访问任何个人记忆。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, pk_uuid
from app.models.enums import ConversationStatus, IdentitySource


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("elder_id", "session_seq", name="uq_conversations_elder_session_seq"),
        CheckConstraint(
            "identity_source IN ('{0}', '{1}', '{2}', '{3}', '{4}')".format(
                IdentitySource.DEVICE_ROOM,
                IdentitySource.VOICEPRINT,
                IdentitySource.EXPLICIT_CONFIRMATION,
                IdentitySource.STAFF_TOKEN,
                IdentitySource.ANONYMOUS,
            ),
            name="ck_conversations_identity_source",
        ),
        CheckConstraint(
            "status IN ('{0}', '{1}', '{2}')".format(
                ConversationStatus.ACTIVE, ConversationStatus.ENDED, ConversationStatus.ABANDONED
            ),
            name="ck_conversations_status",
        ),
        CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at",
            name="ck_conversations_time_range",
        ),
        Index("ix_conversations_elder_started_at", "elder_id", "started_at"),
        Index("ix_conversations_device_started_at", "device_id", "started_at"),
    )

    thread_id: Mapped[uuid.UUID] = pk_uuid()
    elder_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("elders.elder_id"), nullable=True)
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("devices.device_id"), nullable=True
    )
    session_seq: Mapped[int] = mapped_column(nullable=False)
    identity_source: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=ConversationStatus.ACTIVE,
        server_default=ConversationStatus.ACTIVE,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
