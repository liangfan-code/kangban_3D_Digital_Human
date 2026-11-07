"""conversation_messages ORM：对话逐轮消息（Day4 上下文工程地基）。

每轮对话写两条：用户终句转写（user）与数字人回复（assistant，剥净动作标记后的正文）。
thread_id 关联 conversations 会话索引；为 Day5 LLM「最近 5 轮原文 + 更早摘要」提供数据源。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, pk_uuid


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant')", name="ck_conversation_messages_role"),
        Index("ix_conversation_messages_thread_created", "thread_id", "created_at"),
    )

    message_id: Mapped[uuid.UUID] = pk_uuid()
    thread_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.thread_id"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(12), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 全局单调序列：同事务内 created_at 相同，同轮 user/assistant 顺序由 seq 保证
    seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("nextval('conversation_messages_seq')")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
