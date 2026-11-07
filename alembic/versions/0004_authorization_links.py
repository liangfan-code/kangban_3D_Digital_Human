"""0004_authorization_links：三张授权关联表（护工-楼栋 / 负责医生-长者 / 家属-长者）

Revision ID: 0004_authorization_links
Revises: 0003_accounts_device_tokens
Create Date: 2026-11-04

设计依据：《Day_plan/Day2_3D康伴数字人.md》第 5.3 节。
- 删除一律软吊销（status=suspended），外键对 accounts/elders RESTRICT，保留追溯。
- 医生/家属关联带可选 valid_until；过期与否由服务层范围校验判定。
不回改已执行迁移（ADR-013）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_authorization_links"
down_revision: Union[str, None] = "0003_accounts_device_tokens"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_GEN_UUID = sa.text("gen_random_uuid()")
_TS_NOW = sa.text("now()")
_STATUS_CHECK = "status IN ('active', 'suspended')"


def _base_columns() -> list[sa.Column]:
    """三张关联表共有的列：UUID 主键、account_id 外键、status 与时间戳。"""
    return [
        sa.Column(
            "assignment_id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=_GEN_UUID,
            nullable=False,
        ),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("accounts.account_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS_NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=_TS_NOW),
    ]


def _elder_fk() -> sa.Column:
    return sa.Column(
        "elder_id",
        UUID(as_uuid=True),
        sa.ForeignKey("elders.elder_id", ondelete="RESTRICT"),
        nullable=False,
    )


def _valid_until() -> sa.Column:
    return sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True)


def upgrade() -> None:
    op.create_table(
        "building_assignments",
        *_base_columns(),
        sa.Column("building_code", sa.String(32), nullable=False),
        sa.CheckConstraint(_STATUS_CHECK, name="ck_building_assignments_status"),
        sa.UniqueConstraint(
            "account_id", "building_code", name="uq_building_assignments_acct_bldg"
        ),
    )
    op.create_index(
        "ix_building_assignments_acct_status",
        "building_assignments",
        ["account_id", "status"],
    )

    op.create_table(
        "doctor_elder_assignments",
        *_base_columns(),
        _elder_fk(),
        _valid_until(),
        sa.CheckConstraint(_STATUS_CHECK, name="ck_doctor_elder_assignments_status"),
        sa.UniqueConstraint(
            "account_id", "elder_id", name="uq_doctor_elder_assignments_acct_elder"
        ),
    )
    op.create_index(
        "ix_doctor_elder_assignments_acct_status",
        "doctor_elder_assignments",
        ["account_id", "status"],
    )
    op.create_index(
        "ix_doctor_elder_assignments_elder_status",
        "doctor_elder_assignments",
        ["elder_id", "status"],
    )

    op.create_table(
        "family_elder_links",
        *_base_columns(),
        _elder_fk(),
        sa.Column("relation", sa.String(32), nullable=True),
        _valid_until(),
        sa.CheckConstraint(_STATUS_CHECK, name="ck_family_elder_links_status"),
        sa.UniqueConstraint("account_id", "elder_id", name="uq_family_elder_links_acct_elder"),
    )
    op.create_index(
        "ix_family_elder_links_acct_status",
        "family_elder_links",
        ["account_id", "status"],
    )
    op.create_index(
        "ix_family_elder_links_elder_status",
        "family_elder_links",
        ["elder_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_family_elder_links_elder_status", table_name="family_elder_links")
    op.drop_index("ix_family_elder_links_acct_status", table_name="family_elder_links")
    op.drop_table("family_elder_links")
    op.drop_index("ix_doctor_elder_assignments_elder_status", table_name="doctor_elder_assignments")
    op.drop_index("ix_doctor_elder_assignments_acct_status", table_name="doctor_elder_assignments")
    op.drop_table("doctor_elder_assignments")
    op.drop_index("ix_building_assignments_acct_status", table_name="building_assignments")
    op.drop_table("building_assignments")
