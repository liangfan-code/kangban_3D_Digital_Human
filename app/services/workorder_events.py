"""员工端工单实时推送中心（Day8 Task 4）。

边界与约定：
- 只维护 account_id → 连接集合，连接的建立/关闭由路由层负责，本模块不感知 HTTP；
- 推送范围：该工单 elder_id 对应的有效医生授权、护理楼栋授权，以及全部启用管理员；
- 事件体统一为 ``{"type": ..., "data": {...}}``，类型见 ``TICKET_CREATED`` 等常量；
- 单连接发送失败只记日志并移除该连接，绝不影响业务事务（WS 是可选增强通道）。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models import (
    Account,
    BuildingAssignment,
    DoctorElderAssignment,
    Elder,
    Room,
    Workorder,
)
from app.models.enums import AccountRole, AccountStatus, AssignmentStatus
from app.services.workorder import STAFF_ROLES, count_open_by_role

logger = get_logger(__name__)

TICKET_CREATED = "ticket.created"
TICKET_UPDATED = "ticket.updated"
TICKET_RESOLVED = "ticket.resolved"
TICKET_COUNTS = "ticket.counts"


class WorkorderEventHub:
    """进程内订阅中心：同一事件循环下运行，连接集合不做跨线程同步。"""

    def __init__(self) -> None:
        self._connections: dict[UUID, set[Any]] = defaultdict(set)

    def register(self, account_id: UUID, connection: Any) -> None:
        self._connections[account_id].add(connection)

    def unregister(self, account_id: UUID, connection: Any) -> None:
        connections = self._connections.get(account_id)
        if connections is None:
            return
        connections.discard(connection)
        if not connections:
            self._connections.pop(account_id, None)

    def has_connections(self) -> bool:
        return any(self._connections.values())

    def connections_for(self, account_id: UUID) -> list[Any]:
        return list(self._connections.get(account_id, ()))

    def clear(self) -> None:
        """测试或应用关闭时清空全部订阅。"""
        self._connections.clear()


workorder_event_hub = WorkorderEventHub()


async def _resolve_recipients(
    session: AsyncSession, elder_id: UUID
) -> dict[UUID, str]:
    """解析某老人的通知对象：有效医生授权 + 命中楼栋的护理 + 全部启用管理员。"""
    recipients: dict[UUID, str] = {}

    doctor_ids = (
        await session.execute(
            select(Account.account_id)
            .join(
                DoctorElderAssignment,
                DoctorElderAssignment.account_id == Account.account_id,
            )
            .where(
                Account.role == AccountRole.DOCTOR,
                Account.status == AccountStatus.ACTIVE,
                DoctorElderAssignment.elder_id == elder_id,
                DoctorElderAssignment.status == AssignmentStatus.ACTIVE,
                or_(
                    DoctorElderAssignment.valid_until.is_(None),
                    DoctorElderAssignment.valid_until > func.now(),
                ),
            )
        )
    ).scalars().all()
    for account_id in doctor_ids:
        recipients[account_id] = AccountRole.DOCTOR

    building_code = (
        await session.execute(
            select(Room.building_code)
            .join(Elder, Elder.room_id == Room.room_id)
            .where(Elder.elder_id == elder_id)
        )
    ).scalar_one_or_none()
    if building_code:
        nurse_ids = (
            await session.execute(
                select(Account.account_id)
                .join(
                    BuildingAssignment,
                    BuildingAssignment.account_id == Account.account_id,
                )
                .where(
                    Account.role == AccountRole.NURSE,
                    Account.status == AccountStatus.ACTIVE,
                    BuildingAssignment.building_code == building_code,
                    BuildingAssignment.status == AssignmentStatus.ACTIVE,
                )
            )
        ).scalars().all()
        for account_id in nurse_ids:
            recipients[account_id] = AccountRole.NURSE

    admin_ids = (
        await session.execute(
            select(Account.account_id).where(
                Account.role == AccountRole.ADMIN,
                Account.status == AccountStatus.ACTIVE,
            )
        )
    ).scalars().all()
    for account_id in admin_ids:
        recipients[account_id] = AccountRole.ADMIN

    return recipients


def _payload(
    workorder: Workorder,
    role: str,
    unhandled: int,
    extra: dict[str, Any] | None,
) -> dict[str, Any]:
    """事件 data：只含工单元数据与计数，不含对话原文与方案全文。"""
    data: dict[str, Any] = {
        "workorder_id": str(workorder.workorder_id),
        "elder_id": str(workorder.elder_id),
        "category": workorder.sensitive_category,
        "status": workorder.status,
        "priority": workorder.priority,
        "occurrence_count": workorder.occurrence_count,
        "role": role,
        "unhandled": unhandled,
    }
    if extra:
        data.update(extra)
    return data


async def _send(
    account_id: UUID,
    connections: list[Any],
    message: dict[str, Any],
    workorder: Workorder,
) -> None:
    """并发发送到同一账号的全部连接，单连接异常不影响其他连接。"""
    if not connections:
        return
    results = await asyncio.gather(
        *(connection.send_json(message) for connection in connections),
        return_exceptions=True,
    )
    for connection, result in zip(connections, results):
        if isinstance(result, Exception):
            workorder_event_hub.unregister(account_id, connection)
            logger.warning(
                "工单事件推送失败",
                extra={
                    "event": "workorder_event_push_failed",
                    "object_type": "workorder",
                    "object_id": str(workorder.workorder_id),
                },
            )


async def _dispatch(
    session: AsyncSession,
    event_type: str,
    workorder: Workorder,
    extra: dict[str, Any] | None,
) -> None:
    recipients = await _resolve_recipients(session, workorder.elder_id)
    for account_id, role in recipients.items():
        connections = workorder_event_hub.connections_for(account_id)
        if not connections:
            continue
        unhandled = (
            await count_open_by_role(session, role=role, account_id=account_id)
            if role in STAFF_ROLES
            else 0
        )
        await _send(
            account_id,
            connections,
            {"type": event_type, "data": _payload(workorder, role, unhandled, extra)},
            workorder,
        )
        # 主事件之后补一条角标计数，前端无需再单独拉取
        if event_type != TICKET_COUNTS and role in STAFF_ROLES:
            await _send(
                account_id,
                connections,
                {
                    "type": TICKET_COUNTS,
                    "data": {"role": role, "unhandled": unhandled},
                },
                workorder,
            )


async def publish_workorder_event(
    event_type: str,
    workorder: Workorder,
    extra: dict[str, Any] | None = None,
    *,
    session: AsyncSession | None = None,
) -> None:
    """推送工单事件；无连接时直接返回，避免无谓查询。

    ``session`` 由业务层传入可复用当前事务（测试事务隔离下也能正确解析可见范围），
    未传时自行创建会话。
    """
    if not workorder_event_hub.has_connections():
        return
    if session is not None:
        await _dispatch(session, event_type, workorder, extra)
        return

    from app.db.session import get_session_factory

    async with get_session_factory()() as db:
        await _dispatch(db, event_type, workorder, extra)
