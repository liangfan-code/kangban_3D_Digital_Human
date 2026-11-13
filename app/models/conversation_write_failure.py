"""conversation_write_failures：对话落库失败可见表。

record_turn 写失败时由语音链路的兜底分支写入（用独立会话，不阻断通话主链路），
供运维/管理端排查「哪一轮对话丢了」。
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, pk_uuid


class ConversationWriteFailure(Base):
    __tablename__ = "conversation_write_failures"

    failure_id: Mapped[str] = pk_uuid()
    thread_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.thread_id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    seq: Mapped[int | None] = mapped_column(nullable=True)
    error_type: Mapped[str] = mapped_column(String(120), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
