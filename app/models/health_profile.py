"""健康档案 ORM：档案记录 / 授权 ACL / 审计流水 / AI 分析快照 / 健康摘要。

分级口径（Day2 v2 计划 §3.1）：
- vital/body_part/medication/chronic → data_level=basic（护工经 ACL 授权可见）；
- major → data_level=confidential（隐私级，护工任何路径不可见）。
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, TimestampMixin, pk_uuid
from app.models.enums import (
    HealthAuditAction,
    HealthDataLevel,
    HealthSeverity,
)


class ElderHealthRecord(TimestampMixin, Base):
    """健康档案记录：纵向一条一条写，不原地覆盖历史语义由审计流水补足。"""

    __tablename__ = "elder_health_records"
    __table_args__ = (
        CheckConstraint(
            "category IN ('vital', 'body_part', 'medication', 'chronic', 'major')",
            name="ck_ehr_category",
        ),
        CheckConstraint(
            "severity IN ('normal', 'attention', 'warning')", name="ck_ehr_severity"
        ),
        CheckConstraint(
            "data_level IN ('basic', 'confidential')", name="ck_ehr_data_level"
        ),
        CheckConstraint(
            "recorded_role IN ('nurse', 'doctor', 'admin')", name="ck_ehr_recorded_role"
        ),
        Index("ix_ehr_elder_category", "elder_id", "category"),
        Index("ix_ehr_elder_data_level", "elder_id", "data_level"),
    )

    record_id: Mapped[uuid.UUID] = pk_uuid()
    elder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("elders.elder_id"), nullable=False
    )
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    item_key: Mapped[str] = mapped_column(String(64), nullable=False)
    value_text: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(
        String(20), nullable=False, default=HealthSeverity.NORMAL,
        server_default=HealthSeverity.NORMAL,
    )
    data_level: Mapped[str] = mapped_column(
        String(20), nullable=False, default=HealthDataLevel.BASIC,
        server_default=HealthDataLevel.BASIC,
    )
    recorded_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.account_id"), nullable=False
    )
    recorded_role: Mapped[str] = mapped_column(String(20), nullable=False)


class HealthGrant(TimestampMixin, Base):
    """管理员健康授权 ACL：account × elder 四个 bool，叠加在范围判定之上。"""

    __tablename__ = "health_grants"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'suspended')", name="ck_health_grants_status"),
        Index("ix_health_grants_acct_status", "account_id", "status"),
        Index("ix_health_grants_elder_status", "elder_id", "status"),
    )

    grant_id: Mapped[uuid.UUID] = pk_uuid()
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.account_id"), nullable=False
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("elders.elder_id"), nullable=False
    )
    can_view_basic: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    can_edit_basic: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    can_view_confidential: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    can_edit_confidential: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active"
    )
    granted_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.account_id"), nullable=False
    )


class HealthAuditLog(CreatedAtMixin, Base):
    """健康数据写操作审计流水：旧值→新值，只增不改。"""

    __tablename__ = "health_audit_logs"
    __table_args__ = (
        CheckConstraint(
            "action IN ('create', 'update', 'delete')", name="ck_health_audit_logs_action"
        ),
        Index("ix_health_audit_logs_elder_time", "elder_id", "created_at"),
        Index("ix_health_audit_logs_operator", "operator_id"),
    )

    log_id: Mapped[uuid.UUID] = pk_uuid()
    record_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("elder_health_records.record_id"), nullable=False
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(nullable=False)  # 冗余存，便于按长者查
    action: Mapped[str] = mapped_column(
        String(20), nullable=False, default=HealthAuditAction.CREATE
    )
    old_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    operator_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.account_id"), nullable=False
    )


class HealthAiAnalysis(CreatedAtMixin, Base):
    """AI 分析快照存档：input_snapshot 仅 basic 级脱敏数据，备审计。"""

    __tablename__ = "health_ai_analyses"
    __table_args__ = (
        Index("ix_health_ai_analyses_elder_time", "elder_id", "created_at"),
    )

    analysis_id: Mapped[uuid.UUID] = pk_uuid()
    elder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("elders.elder_id"), nullable=False
    )
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    analysis_text: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(
        String(20), nullable=False, default="v1", server_default="v1"
    )
    requested_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.account_id"), nullable=False
    )


class HealthSummary(CreatedAtMixin, Base):
    """医生生成的健康摘要留档；家属已阅回执直接记在本表。"""

    __tablename__ = "health_summaries"
    __table_args__ = (
        Index("ix_health_summaries_elder_time", "elder_id", "created_at"),
    )

    summary_id: Mapped[uuid.UUID] = pk_uuid()
    elder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("elders.elder_id"), nullable=False
    )
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    generated_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.account_id"), nullable=False
    )
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.account_id"), nullable=True
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
