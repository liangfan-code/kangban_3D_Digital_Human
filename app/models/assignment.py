"""三张授权关联表 ORM：护工-楼栋、负责医生-长者、家属-长者。

删除一律软吊销（status=suspended），保留追溯；对 accounts/elders 外键 RESTRICT。
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
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, pk_uuid
from app.models.enums import AssignmentStatus


def _status_check(table: str) -> CheckConstraint:
    return CheckConstraint(
        "status IN ('{0}', '{1}')".format(AssignmentStatus.ACTIVE, AssignmentStatus.SUSPENDED),
        name=f"ck_{table}_status",
    )


class BuildingAssignment(TimestampMixin, Base):
    """护工-楼栋授权：护工可查询该楼栋内长者。"""

    __tablename__ = "building_assignments"
    __table_args__ = (
        _status_check("building_assignments"),
        UniqueConstraint("account_id", "building_code", name="uq_building_assignments_acct_bldg"),
        Index("ix_building_assignments_acct_status", "account_id", "status"),
    )

    assignment_id: Mapped[uuid.UUID] = pk_uuid()
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.account_id", ondelete="RESTRICT"), nullable=False
    )
    building_code: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=AssignmentStatus.ACTIVE,
        server_default=AssignmentStatus.ACTIVE,
    )


class DoctorElderAssignment(TimestampMixin, Base):
    """负责医生-长者授权：valid_until 为空表示长期有效，过期后范围校验不再通过。"""

    __tablename__ = "doctor_elder_assignments"
    __table_args__ = (
        _status_check("doctor_elder_assignments"),
        UniqueConstraint("account_id", "elder_id", name="uq_doctor_elder_assignments_acct_elder"),
        Index("ix_doctor_elder_assignments_acct_status", "account_id", "status"),
        Index("ix_doctor_elder_assignments_elder_status", "elder_id", "status"),
    )

    assignment_id: Mapped[uuid.UUID] = pk_uuid()
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.account_id", ondelete="RESTRICT"), nullable=False
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("elders.elder_id", ondelete="RESTRICT"), nullable=False
    )
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=AssignmentStatus.ACTIVE,
        server_default=AssignmentStatus.ACTIVE,
    )


class FamilyElderLink(TimestampMixin, Base):
    """家属-长者关联：Day2 只建表维护，不开放家属数据查询接口。"""

    __tablename__ = "family_elder_links"
    __table_args__ = (
        _status_check("family_elder_links"),
        UniqueConstraint("account_id", "elder_id", name="uq_family_elder_links_acct_elder"),
        Index("ix_family_elder_links_acct_status", "account_id", "status"),
        Index("ix_family_elder_links_elder_status", "elder_id", "status"),
    )

    assignment_id: Mapped[uuid.UUID] = pk_uuid()
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.account_id", ondelete="RESTRICT"), nullable=False
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("elders.elder_id", ondelete="RESTRICT"), nullable=False
    )
    relation: Mapped[str | None] = mapped_column(String(32), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=AssignmentStatus.ACTIVE,
        server_default=AssignmentStatus.ACTIVE,
    )
