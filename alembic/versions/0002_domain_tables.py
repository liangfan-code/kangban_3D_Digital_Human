"""0002_domain_tables：领域七表（conversations/medication_plans/media_library/health_records/memory_chunks/voiceprints/workorders）

Revision ID: 0002_domain_tables
Revises: 0001_facility_core
Create Date: 2026-11-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers, used by Alembic.
revision: str = "0002_domain_tables"
down_revision: Union[str, None] = "0001_facility_core"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("thread_id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("elder_id", UUID(as_uuid=True), sa.ForeignKey("elders.elder_id"), nullable=True),
        sa.Column("device_id", UUID(as_uuid=True), sa.ForeignKey("devices.device_id"), nullable=True),
        sa.Column("session_seq", sa.BigInteger(), nullable=False),
        sa.Column("identity_source", sa.String(24), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "identity_source IN ('device_room', 'voiceprint', 'explicit_confirmation', 'staff_token', 'anonymous')",
            name="ck_conversations_identity_source",
        ),
        sa.CheckConstraint("status IN ('active', 'ended', 'abandoned')", name="ck_conversations_status"),
        sa.CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name="ck_conversations_time_range"),
        sa.UniqueConstraint("elder_id", "session_seq", name="uq_conversations_elder_session_seq"),
    )
    op.create_index("ix_conversations_elder_started_at", "conversations", ["elder_id", "started_at"])
    op.create_index("ix_conversations_device_started_at", "conversations", ["device_id", "started_at"])

    op.create_table(
        "medication_plans",
        sa.Column("plan_id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("elder_id", UUID(as_uuid=True), sa.ForeignKey("elders.elder_id"), nullable=False),
        sa.Column("drug_name", sa.String(160), nullable=False),
        sa.Column("dose", sa.String(100), nullable=False),
        sa.Column("schedule", JSONB(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="planned"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "status IN ('planned', 'active', 'paused', 'completed', 'cancelled')",
            name="ck_medication_plans_status",
        ),
        sa.CheckConstraint("end_date IS NULL OR end_date >= start_date", name="ck_medication_plans_date_range"),
    )
    op.create_index("ix_medication_plans_elder_status", "medication_plans", ["elder_id", "status"])

    op.create_table(
        "media_library",
        sa.Column("media_id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("genre", sa.String(80), nullable=True),
        sa.Column("era", sa.String(80), nullable=True),
        sa.Column("artist", sa.String(160), nullable=True),
        sa.Column("media_uri", sa.String(500), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("copyright_status", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("status", sa.String(20), nullable=False, server_default="hidden"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("type IN ('opera', 'movie')", name="ck_media_library_type"),
        sa.CheckConstraint(
            "copyright_status IN ('unknown', 'licensed', 'teaching', 'public_domain', 'restricted', 'expired')",
            name="ck_media_library_copyright_status",
        ),
        sa.CheckConstraint("status IN ('active', 'hidden')", name="ck_media_library_status"),
        # 版权边界：仅授权/教学/公有领域资源可进入 active
        sa.CheckConstraint(
            "status <> 'active' OR copyright_status IN ('licensed', 'teaching', 'public_domain')",
            name="ck_media_library_active_requires_licensed",
        ),
    )
    op.create_index("ix_media_library_type_status", "media_library", ["type", "status"])
    op.create_index("ix_media_library_title", "media_library", ["title"])

    op.create_table(
        "health_records",
        sa.Column("record_id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("elder_id", UUID(as_uuid=True), sa.ForeignKey("elders.elder_id"), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bp_systolic", sa.SmallInteger(), nullable=True),
        sa.Column("bp_diastolic", sa.SmallInteger(), nullable=True),
        sa.Column("sleep_minutes", sa.Integer(), nullable=True),
        sa.Column("mood", sa.String(24), nullable=True),
        sa.Column("chief_complaint", sa.Text(), nullable=True),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("bp_systolic IS NULL OR bp_systolic BETWEEN 50 AND 250", name="ck_health_records_bp_systolic_range"),
        sa.CheckConstraint("bp_diastolic IS NULL OR bp_diastolic BETWEEN 30 AND 150", name="ck_health_records_bp_diastolic_range"),
        sa.CheckConstraint("sleep_minutes IS NULL OR sleep_minutes BETWEEN 0 AND 1440", name="ck_health_records_sleep_minutes_range"),
    )
    op.create_index("ix_health_records_elder_recorded_at", "health_records", ["elder_id", "recorded_at"])

    op.create_table(
        "memory_chunks",
        sa.Column("chunk_id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("elder_id", UUID(as_uuid=True), sa.ForeignKey("elders.elder_id"), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        # pgvector vector 列：维度待 Embedding 模型确定后由后续迁移固定，Day1 不设维度、不建 ANN 索引
        sa.Column("embedding", Vector(), nullable=False),
        sa.Column("embedding_model", sa.String(80), nullable=True),
        sa.Column("category", sa.String(24), nullable=False),
        sa.Column("visibility", sa.String(20), nullable=False, server_default="private"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "category IN ('preference', 'health', 'event', 'relationship', 'routine', 'other')",
            name="ck_memory_chunks_category",
        ),
        sa.CheckConstraint("visibility IN ('private', 'care_team')", name="ck_memory_chunks_visibility"),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'suppressed', 'deleted')",
            name="ck_memory_chunks_status",
        ),
    )
    op.create_index(
        "ix_memory_chunks_elder_status_category_created",
        "memory_chunks",
        ["elder_id", "status", "category", "created_at"],
    )
    op.create_index("ix_memory_chunks_elder_visibility", "memory_chunks", ["elder_id", "visibility"])

    op.create_table(
        "voiceprints",
        sa.Column("voiceprint_id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("elder_id", UUID(as_uuid=True), sa.ForeignKey("elders.elder_id"), nullable=False),
        sa.Column("embedding", Vector(), nullable=False),
        sa.Column("embedding_model", sa.String(80), nullable=True),
        sa.Column("sample_uri", sa.String(500), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("status IN ('active', 'revoked', 'expired')", name="ck_voiceprints_status"),
    )
    op.create_index("ix_voiceprints_elder_status", "voiceprints", ["elder_id", "status"])

    op.create_table(
        "workorders",
        sa.Column("workorder_id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("elder_id", UUID(as_uuid=True), sa.ForeignKey("elders.elder_id"), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("priority", sa.String(16), nullable=False, server_default="normal"),
        # 无 staff 表：assignee_ref 暂存外部引用，不伪造外键
        sa.Column("assignee_ref", sa.String(120), nullable=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "status IN ('open', 'acknowledged', 'in_progress', 'resolved', 'cancelled')",
            name="ck_workorders_status",
        ),
        sa.CheckConstraint(
            "priority IN ('low', 'normal', 'high', 'urgent')",
            name="ck_workorders_priority",
        ),
    )
    op.create_index("ix_workorders_status_created_at", "workorders", ["status", "created_at"])
    op.create_index("ix_workorders_elder_status", "workorders", ["elder_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_workorders_elder_status", table_name="workorders")
    op.drop_index("ix_workorders_status_created_at", table_name="workorders")
    op.drop_table("workorders")
    op.drop_index("ix_voiceprints_elder_status", table_name="voiceprints")
    op.drop_table("voiceprints")
    op.drop_index("ix_memory_chunks_elder_visibility", table_name="memory_chunks")
    op.drop_index("ix_memory_chunks_elder_status_category_created", table_name="memory_chunks")
    op.drop_table("memory_chunks")
    op.drop_index("ix_health_records_elder_recorded_at", table_name="health_records")
    op.drop_table("health_records")
    op.drop_index("ix_media_library_title", table_name="media_library")
    op.drop_index("ix_media_library_type_status", table_name="media_library")
    op.drop_table("media_library")
    op.drop_index("ix_medication_plans_elder_status", table_name="medication_plans")
    op.drop_table("medication_plans")
    op.drop_index("ix_conversations_device_started_at", table_name="conversations")
    op.drop_index("ix_conversations_elder_started_at", table_name="conversations")
    op.drop_table("conversations")
