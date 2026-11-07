"""0001_facility_core：pgvector 扩展与核心四表（rooms/personas/devices/elders）

Revision ID: 0001_facility_core
Revises:
Create Date: 2026-11-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision: str = "0001_facility_core"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # pgvector 扩展由初始迁移确保存在
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # gen_random_uuid() 依赖（PG13+ 内置；低版本需 pgcrypto）
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "rooms",
        sa.Column("room_id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("building_code", sa.String(32), nullable=False),
        sa.Column("floor", sa.SmallInteger(), nullable=False),
        sa.Column("room_number", sa.String(32), nullable=False),
        sa.Column("bed_count", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("bed_count IN (1, 2)", name="ck_rooms_bed_count"),
        sa.CheckConstraint("status IN ('active', 'maintenance', 'inactive')", name="ck_rooms_status"),
        sa.UniqueConstraint("building_code", "floor", "room_number", name="uq_rooms_building_floor_number"),
    )
    op.create_index("ix_rooms_status", "rooms", ["status"])

    op.create_table(
        "personas",
        sa.Column("persona_id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("avatar_url", sa.String(500), nullable=True),
        sa.Column("personality", sa.Text(), nullable=True),
        sa.Column("greeting", sa.Text(), nullable=True),
        sa.Column("voice_id", sa.String(100), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("code", name="uq_personas_code"),
    )
    op.create_index("ix_personas_is_active_name", "personas", ["is_active", "name"])

    op.create_table(
        "devices",
        sa.Column("device_id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("device_code", sa.String(80), nullable=False),
        sa.Column("room_id", UUID(as_uuid=True), sa.ForeignKey("rooms.room_id"), nullable=False),
        sa.Column("bed_no", sa.SmallInteger(), nullable=True),
        sa.Column("device_type", sa.String(24), nullable=False, server_default="tablet"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("bed_no IS NULL OR bed_no IN (1, 2)", name="ck_devices_bed_no"),
        sa.CheckConstraint("device_type IN ('tablet')", name="ck_devices_device_type"),
        sa.CheckConstraint("status IN ('pending', 'active', 'disabled', 'retired')", name="ck_devices_status"),
        sa.UniqueConstraint("device_code", name="uq_devices_device_code"),
    )
    op.create_index("ix_devices_room_id_status", "devices", ["room_id", "status"])

    op.create_table(
        "elders",
        sa.Column("elder_id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("birth_date", sa.Date(), nullable=True),
        sa.Column("gender", sa.String(20), nullable=True),
        sa.Column("room_id", UUID(as_uuid=True), sa.ForeignKey("rooms.room_id"), nullable=True),
        sa.Column("bed_no", sa.SmallInteger(), nullable=True),
        sa.Column("persona_id", UUID(as_uuid=True), sa.ForeignKey("personas.persona_id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("gender IS NULL OR gender IN ('female', 'male', 'other', 'unknown')", name="ck_elders_gender"),
        sa.CheckConstraint("bed_no IS NULL OR bed_no IN (1, 2)", name="ck_elders_bed_no"),
        sa.CheckConstraint("status IN ('active', 'paused', 'discharged')", name="ck_elders_status"),
        sa.CheckConstraint("(room_id IS NULL) = (bed_no IS NULL)", name="ck_elders_room_bed_pair"),
    )
    # 部分唯一：每个已占用床位最多一位长者
    op.create_index(
        "uq_elders_room_bed",
        "elders",
        ["room_id", "bed_no"],
        unique=True,
        postgresql_where=sa.text("room_id IS NOT NULL AND bed_no IS NOT NULL"),
    )
    op.create_index("ix_elders_room_id_status", "elders", ["room_id", "status"])


def downgrade() -> None:
    # 注意：不删除 vector / pgcrypto 扩展，避免误删共享扩展
    op.drop_index("ix_elders_room_id_status", table_name="elders")
    op.drop_index("uq_elders_room_bed", table_name="elders")
    op.drop_table("elders")
    op.drop_index("ix_devices_room_id_status", table_name="devices")
    op.drop_table("devices")
    op.drop_index("ix_personas_is_active_name", table_name="personas")
    op.drop_table("personas")
    op.drop_index("ix_rooms_status", table_name="rooms")
    op.drop_table("rooms")
