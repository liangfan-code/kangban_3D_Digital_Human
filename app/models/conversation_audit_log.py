"""conversation_audit_logs：会话/记忆访问审计（数据治理 段 4）。

只记元数据（actor/action/target/detail 摘要），禁止记录对话正文与记忆正文。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, pk_uuid


class ConversationAuditLog(Base):
    __tablename__ = "conversation_audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_target_created", "target_type", "target_id", "created_at"),
        Index("ix_audit_logs_created", "created_at"),
    )

    audit_id: Mapped[uuid.UUID] = pk_uuid()
    actor_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.account_id", ondelete="SET NULL"), nullable=True
    )
    actor_role: Mapped[str | None] = mapped_column(String(24), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
