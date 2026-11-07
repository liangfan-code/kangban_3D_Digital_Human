"""0005_health_profile：健康档案五张表（档案/授权 ACL/审计流水/AI 分析快照/健康摘要）

Revision ID: 0005_health_profile
Revises: 0004_authorization_links
Create Date: 2026-11-05

设计依据：《Day_plan/Day2_完善前端+后端功能.md》§3.1（v2）。
- elder_health_records：纵向记录式健康档案；major 类记录 data_level=confidential（护工不可见）。
- health_grants：管理员逐人放权 ACL，叠加在范围判定之上。
- health_audit_logs：写操作审计流水（旧值→新值）。
- health_ai_analyses：AI 分析出域快照留档（仅 basic 级出域）。
- health_summaries：医生生成的健康摘要留档 + 家属已阅回执（计划表 A-D 之外的补充表，
  承载 API 清单中 POST /health/summary 与 summaries/{sid}/ack 的落库需求）。
- 删除/吊销一律软删（status=suspended）或保留历史行；外键 RESTRICT。
不回改已执行迁移（ADR-013）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_health_profile"
down_revision: Union[str, None] = "0004_authorization_links"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_GEN_UUID = sa.text("gen_random_uuid()")
_TS_NOW = sa.text("now()")

_CATEGORIES = ("vital", "body_part", "medication", "chronic", "major")
_SEVERITIES = ("normal", "attention", "warning")
_DATA_LEVELS = ("basic", "confidential")
_RECORDED_ROLES = ("nurse", "doctor", "admin")
_AUDIT_ACTIONS = ("create", "update", "delete")


def upgrade() -> None:
    # ---- 表 A：elder_health_records（健康档案，纵向记录式） ----
    op.create_table(
        "elder_health_records",
        sa.Column(
            "record_id", UUID(as_uuid=True), primary_key=True,
            server_default=_GEN_UUID, nullable=False,
        ),
        sa.Column(
            "elder_id", UUID(as_uuid=True),
            sa.ForeignKey("elders.elder_id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column(
            "category", sa.String(20), nullable=False,
            comment="vital/body_part/medication/chronic/major",
        ),
        sa.Column(
            "item_key", sa.String(64), nullable=False, comment="条目名，如 blood_pressure/heart"
        ),
        sa.Column(
            "value_text", sa.Text(), nullable=False, comment="内容：症状描述/数值+单位/诊断结论"
        ),
        sa.Column(
            "severity", sa.String(20), nullable=False, server_default="normal",
            comment="normal/attention/warning",
        ),
        sa.Column(
            "data_level", sa.String(20), nullable=False, server_default="basic",
            comment="basic=护工可看；confidential=隐私级（major 类），护工不可见",
        ),
        sa.Column(
            "recorded_by", UUID(as_uuid=True),
            sa.ForeignKey("accounts.account_id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column(
            "recorded_role", sa.String(20), nullable=False,
            comment="写入时服务端注入：nurse/doctor/admin",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS_NOW),
        sa.CheckConstraint(
            "category IN ({0})".format(", ".join(repr(c) for c in _CATEGORIES)),
            name="ck_ehr_category",
        ),
        sa.CheckConstraint(
            "severity IN ({0})".format(", ".join(repr(s) for s in _SEVERITIES)),
            name="ck_ehr_severity",
        ),
        sa.CheckConstraint(
            "data_level IN ({0})".format(", ".join(repr(d) for d in _DATA_LEVELS)),
            name="ck_ehr_data_level",
        ),
        sa.CheckConstraint(
            "recorded_role IN ({0})".format(", ".join(repr(r) for r in _RECORDED_ROLES)),
            name="ck_ehr_recorded_role",
        ),
    )
    op.create_index(
        "ix_ehr_elder_category", "elder_health_records", ["elder_id", "category"]
    )
    op.create_index(
        "ix_ehr_elder_data_level", "elder_health_records", ["elder_id", "data_level"]
    )

    # ---- 表 B：health_grants（管理员健康授权 ACL） ----
    op.create_table(
        "health_grants",
        sa.Column(
            "grant_id", UUID(as_uuid=True), primary_key=True,
            server_default=_GEN_UUID, nullable=False,
        ),
        sa.Column(
            "account_id", UUID(as_uuid=True),
            sa.ForeignKey("accounts.account_id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column(
            "elder_id", UUID(as_uuid=True),
            sa.ForeignKey("elders.elder_id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column(
            "can_view_basic", sa.Boolean(), nullable=False, server_default=sa.false(),
        ),
        sa.Column(
            "can_edit_basic", sa.Boolean(), nullable=False, server_default=sa.false(),
        ),
        sa.Column(
            "can_view_confidential", sa.Boolean(), nullable=False, server_default=sa.false(),
        ),
        sa.Column(
            "can_edit_confidential", sa.Boolean(), nullable=False, server_default=sa.false(),
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column(
            "granted_by", UUID(as_uuid=True),
            sa.ForeignKey("accounts.account_id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS_NOW),
        sa.CheckConstraint("status IN ('active', 'suspended')", name="ck_health_grants_status"),
        sa.UniqueConstraint("account_id", "elder_id", name="uq_health_grants_acct_elder"),
    )
    op.create_index("ix_health_grants_acct_status", "health_grants", ["account_id", "status"])
    op.create_index("ix_health_grants_elder_status", "health_grants", ["elder_id", "status"])

    # ---- 表 C：health_audit_logs（写操作审计流水） ----
    op.create_table(
        "health_audit_logs",
        sa.Column(
            "log_id", UUID(as_uuid=True), primary_key=True,
            server_default=_GEN_UUID, nullable=False,
        ),
        sa.Column(
            "record_id", UUID(as_uuid=True),
            sa.ForeignKey("elder_health_records.record_id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("elder_id", UUID(as_uuid=True), nullable=False, comment="冗余存，便于按长者查"),
        sa.Column("action", sa.String(20), nullable=False, comment="create/update/delete"),
        sa.Column("old_value", JSONB(), nullable=True),
        sa.Column("new_value", JSONB(), nullable=True),
        sa.Column(
            "operator_id", UUID(as_uuid=True),
            sa.ForeignKey("accounts.account_id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS_NOW),
        sa.CheckConstraint(
            "action IN ({0})".format(", ".join(repr(a) for a in _AUDIT_ACTIONS)),
            name="ck_health_audit_logs_action",
        ),
    )
    op.create_index(
        "ix_health_audit_logs_elder_time", "health_audit_logs", ["elder_id", "created_at"]
    )
    op.create_index("ix_health_audit_logs_operator", "health_audit_logs", ["operator_id"])

    # ---- 表 D：health_ai_analyses（AI 分析快照存档） ----
    op.create_table(
        "health_ai_analyses",
        sa.Column(
            "analysis_id", UUID(as_uuid=True), primary_key=True,
            server_default=_GEN_UUID, nullable=False,
        ),
        sa.Column(
            "elder_id", UUID(as_uuid=True),
            sa.ForeignKey("elders.elder_id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column(
            "input_snapshot", JSONB(), nullable=False,
            comment="出域数据留底（仅 basic 级，脱敏：无姓名/UUID/房间号）",
        ),
        sa.Column("analysis_text", sa.Text(), nullable=False),
        sa.Column("model_name", sa.String(100), nullable=False),
        sa.Column("prompt_version", sa.String(20), nullable=False, server_default="v1"),
        sa.Column(
            "requested_by", UUID(as_uuid=True),
            sa.ForeignKey("accounts.account_id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS_NOW),
    )
    op.create_index(
        "ix_health_ai_analyses_elder_time", "health_ai_analyses", ["elder_id", "created_at"]
    )

    # ---- 表 E：health_summaries（医生健康摘要留档 + 家属已阅回执） ----
    op.create_table(
        "health_summaries",
        sa.Column(
            "summary_id", UUID(as_uuid=True), primary_key=True,
            server_default=_GEN_UUID, nullable=False,
        ),
        sa.Column(
            "elder_id", UUID(as_uuid=True),
            sa.ForeignKey("elders.elder_id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("summary_text", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "generated_by", UUID(as_uuid=True),
            sa.ForeignKey("accounts.account_id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column(
            "acknowledged_by", UUID(as_uuid=True),
            sa.ForeignKey("accounts.account_id", ondelete="RESTRICT"), nullable=True,
            comment="家属已阅回执：回执人",
        ),
        sa.Column(
            "acknowledged_at", sa.DateTime(timezone=True), nullable=True,
            comment="家属已阅回执：时间",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS_NOW),
    )
    op.create_index(
        "ix_health_summaries_elder_time", "health_summaries", ["elder_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_health_summaries_elder_time", table_name="health_summaries")
    op.drop_table("health_summaries")
    op.drop_index("ix_health_ai_analyses_elder_time", table_name="health_ai_analyses")
    op.drop_table("health_ai_analyses")
    op.drop_index("ix_health_audit_logs_operator", table_name="health_audit_logs")
    op.drop_index("ix_health_audit_logs_elder_time", table_name="health_audit_logs")
    op.drop_table("health_audit_logs")
    op.drop_index("ix_health_grants_elder_status", table_name="health_grants")
    op.drop_index("ix_health_grants_acct_status", table_name="health_grants")
    op.drop_table("health_grants")
    op.drop_index("ix_ehr_elder_data_level", table_name="elder_health_records")
    op.drop_index("ix_ehr_elder_category", table_name="elder_health_records")
    op.drop_table("elder_health_records")
