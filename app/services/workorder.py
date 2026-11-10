"""工单领域服务（Day8）：敏感事件建单、三态处理项、终审关闭、可见范围查询与上下文快照。

边界：
- 只做业务编排与状态机，不处理 HTTP；
- 可见范围：医生按 doctor_elder_assignments、护工按楼栋（rooms.building_code）、
  管理员全量、家属由上层传 elder_id 且在此校验 family_elder_links；
- 日志只记 workorder_id / sensitive_category / actor_role，禁止记录对话原文与方案全文。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models import (
    Account,
    BuildingAssignment,
    DoctorElderAssignment,
    Elder,
    FamilyElderLink,
    Room,
    Workorder,
    WorkorderDisposition,
    WorkorderEvent,
)
from app.models.enums import (
    AccountRole,
    AssignmentStatus,
    WorkorderDispositionStatus,
    WorkorderPriority,
    WorkorderSource,
    WorkorderStatus,
)

logger = get_logger(__name__)

# 工单类型与终态
SENSITIVE_TYPE = "sensitive"
CLOSED_STATUSES = (WorkorderStatus.RESOLVED, WorkorderStatus.CANCELLED)
STAFF_ROLES = (AccountRole.DOCTOR, AccountRole.NURSE)
SNAPSHOT_MAX_TURNS = 5


class WorkOrderError(Exception):
    """工单领域异常基类。"""


class WorkOrderNotFound(WorkOrderError):
    """工单/处理项/关联不存在（上层映射 404，不暴露存在性）。"""


class WorkOrderConflict(WorkOrderError):
    """状态机冲突或重复提交（上层映射 409）。"""


class WorkOrderForbidden(WorkOrderError):
    """角色与处理项不匹配或越权（上层映射 403）。"""


class WorkOrderValidationError(WorkOrderError):
    """入参不合法（如方案为空，上层映射 400/422）。"""


# ---- 内部工具 ----


def _now() -> datetime:
    return datetime.now(UTC)


def _add_event(
    session: AsyncSession,
    *,
    workorder_id: UUID,
    event_type: str,
    actor_account_id: UUID | None = None,
    actor_role: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """追加一条工单审计事件（只记元数据，不记对话原文/方案全文）。"""
    session.add(
        WorkorderEvent(
            workorder_id=workorder_id,
            event_type=event_type,
            actor_account_id=actor_account_id,
            actor_role=actor_role,
            payload=payload,
        )
    )


async def _load_account(session: AsyncSession, account_id: UUID) -> Account:
    account = await session.get(Account, account_id)
    if account is None:
        raise WorkOrderNotFound("账号不存在")
    return account


def _require_staff_role(account: Account, role: str) -> None:
    if role not in STAFF_ROLES or account.role != role:
        raise WorkOrderForbidden("当前角色无权操作该处理项")


async def _load_work_order(session: AsyncSession, workorder_id: UUID) -> Workorder:
    workorder = await session.get(Workorder, workorder_id)
    if workorder is None:
        raise WorkOrderNotFound("工单不存在")
    return workorder


async def _load_disposition(
    session: AsyncSession, workorder_id: UUID, role: str
) -> WorkorderDisposition:
    disposition = (
        await session.execute(
            select(WorkorderDisposition).where(
                WorkorderDisposition.workorder_id == workorder_id,
                WorkorderDisposition.role == role,
            )
        )
    ).scalar_one_or_none()
    if disposition is None:
        raise WorkOrderNotFound("工单处理项不存在")
    return disposition


def _visible_elder_ids(role: str, account_id: UUID) -> Any | None:
    """按角色返回可见 elder_id 子查询；管理员返回 None 表示不限。

    护工无楼栋字段，经 elders.room_id → rooms.building_code 关联其授权楼栋。
    """
    if role == AccountRole.ADMIN:
        return None
    if role == AccountRole.DOCTOR:
        return select(DoctorElderAssignment.elder_id).where(
            DoctorElderAssignment.account_id == account_id,
            DoctorElderAssignment.status == AssignmentStatus.ACTIVE,
            or_(
                DoctorElderAssignment.valid_until.is_(None),
                DoctorElderAssignment.valid_until > func.now(),
            ),
        )
    if role == AccountRole.NURSE:
        buildings = select(BuildingAssignment.building_code).where(
            BuildingAssignment.account_id == account_id,
            BuildingAssignment.status == AssignmentStatus.ACTIVE,
        )
        return (
            select(Elder.elder_id)
            .join(Room, Room.room_id == Elder.room_id)
            .where(Room.building_code.in_(buildings))
        )
    raise WorkOrderForbidden("当前角色无权查询工单")


async def _assert_family_link(
    session: AsyncSession, account_id: UUID, elder_id: UUID
) -> None:
    """家属必须存在有效绑定，否则按不存在处理（404，不暴露存在性）。"""
    link = (
        await session.execute(
            select(FamilyElderLink).where(
                FamilyElderLink.account_id == account_id,
                FamilyElderLink.elder_id == elder_id,
                FamilyElderLink.status == AssignmentStatus.ACTIVE,
            )
        )
    ).scalar_one_or_none()
    if link is None or (link.valid_until is not None and link.valid_until <= _now()):
        raise WorkOrderNotFound("工单不存在")


def _like_pattern(keyword: str) -> str:
    """转义 LIKE 通配符，避免用户输入 % / _ 变成通配。"""
    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


async def _publish_event(
    session: AsyncSession, event_type: str, workorder: Workorder
) -> None:
    """推送工单事件：延迟导入避免循环依赖；失败只记日志，不影响业务事务。"""
    try:
        from app.services.workorder_events import publish_workorder_event

        await publish_workorder_event(event_type, workorder, session=session)
    except Exception:
        logger.warning(
            f"工单事件推送失败: {event_type}",
            extra={
                "event": "workorder_event_push_failed",
                "object_type": "workorder",
                "object_id": str(workorder.workorder_id),
            },
            exc_info=True,
        )


# ---- 建单 ----


async def create_sensitive_work_order(
    session: AsyncSession,
    *,
    elder_id: UUID,
    category: str,
    device_id: UUID | str | None = None,
    trigger_text: str,
    context_snapshot: list[dict[str, Any]] | None = None,
) -> tuple[Workorder, bool]:
    """敏感事件建单；同老人+同类别存在未关闭工单时只累加次数。

    返回 (工单, 是否新建)。
    """
    category = (category or "").strip() or "unspecified"

    elder = await session.get(Elder, elder_id)
    if elder is None:
        raise WorkOrderNotFound("长者不存在")

    existing = (
        await session.execute(
            select(Workorder)
            .where(
                Workorder.elder_id == elder_id,
                Workorder.sensitive_category == category,
                Workorder.status.not_in(CLOSED_STATUSES),
            )
            .order_by(Workorder.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if existing is not None:
        # 去重：不新建工单，累加发生次数并刷新触发原句
        existing.occurrence_count = (existing.occurrence_count or 1) + 1
        existing.description = trigger_text
        _add_event(
            session,
            workorder_id=existing.workorder_id,
            event_type="occurrence",
            actor_role=None,
            payload={
                "occurrence_count": existing.occurrence_count,
                "sensitive_category": category,
                "device_id": str(device_id) if device_id else None,
            },
        )
        await session.flush()
        await _publish_event(session, "ticket.updated", existing)
        logger.info(
            "敏感事件工单重复触发",
            extra={
                "event": "workorder_occurrence",
                "object_type": "workorder",
                "object_id": str(existing.workorder_id),
                "sensitive_category": category,
                "occurrence_count": existing.occurrence_count,
            },
        )
        return existing, False

    workorder = Workorder(
        elder_id=elder_id,
        type=SENSITIVE_TYPE,
        source=WorkorderSource.SENSITIVE,
        sensitive_category=category,
        occurrence_count=1,
        status=WorkorderStatus.OPEN,
        priority=WorkorderPriority.URGENT,
        title=f"敏感事件：{category}",
        description=trigger_text,
        context_snapshot=context_snapshot or None,
    )
    session.add(workorder)
    await session.flush()

    for role in STAFF_ROLES:
        session.add(
            WorkorderDisposition(
                workorder_id=workorder.workorder_id,
                role=role,
                status=WorkorderDispositionStatus.UNREAD,
            )
        )
    _add_event(
        session,
        workorder_id=workorder.workorder_id,
        event_type="created",
        payload={
            "source": WorkorderSource.SENSITIVE.value,
            "sensitive_category": category,
            "device_id": str(device_id) if device_id else None,
        },
    )
    await session.flush()
    await _publish_event(session, "ticket.created", workorder)
    logger.info(
        "敏感事件工单已创建",
        extra={
            "event": "workorder_created",
            "object_type": "workorder",
            "object_id": str(workorder.workorder_id),
            "sensitive_category": category,
        },
    )
    return workorder, True


# ---- 处理项状态机 ----


async def mark_read(
    session: AsyncSession, *, workorder_id: UUID, role: str, account_id: UUID
) -> WorkorderDisposition:
    """该角色处理项 unread→read（幂等）；handled 不回退。"""
    account = await _load_account(session, account_id)
    _require_staff_role(account, role)
    workorder = await _load_work_order(session, workorder_id)
    if workorder.status in CLOSED_STATUSES:
        raise WorkOrderConflict("工单已关闭，不可再变更")
    disposition = await _load_disposition(session, workorder_id, role)

    if disposition.status == WorkorderDispositionStatus.HANDLED:
        return disposition
    if disposition.status == WorkorderDispositionStatus.READ:
        return disposition

    disposition.status = WorkorderDispositionStatus.READ
    disposition.read_at = _now()
    disposition.account_id = account_id
    if workorder.status == WorkorderStatus.OPEN:
        workorder.status = WorkorderStatus.ACKNOWLEDGED
    _add_event(
        session,
        workorder_id=workorder_id,
        event_type="read",
        actor_account_id=account_id,
        actor_role=role,
    )
    await session.flush()
    await _publish_event(session, "ticket.updated", workorder)
    logger.info(
        "工单处理项已读",
        extra={
            "event": "workorder_read",
            "object_type": "workorder",
            "object_id": str(workorder_id),
            "actor_id": str(account_id),
            "actor_role": role,
        },
    )
    return disposition


async def submit_disposition(
    session: AsyncSession,
    *,
    workorder_id: UUID,
    role: str,
    account_id: UUID,
    plan_text: str,
) -> WorkorderDisposition:
    """提交处理方案（read/unread→handled）；重复提交抛冲突。"""
    plan = (plan_text or "").strip()
    if not plan:
        raise WorkOrderValidationError("处理方案不能为空")

    account = await _load_account(session, account_id)
    _require_staff_role(account, role)
    workorder = await _load_work_order(session, workorder_id)
    if workorder.status in CLOSED_STATUSES:
        raise WorkOrderConflict("工单已关闭，不可再提交方案")
    disposition = await _load_disposition(session, workorder_id, role)
    if disposition.status == WorkorderDispositionStatus.HANDLED:
        raise WorkOrderConflict("该处理项已处理，不能重复提交")

    disposition.status = WorkorderDispositionStatus.HANDLED
    disposition.plan_text = plan
    disposition.handled_at = _now()
    disposition.account_id = account_id
    workorder.status = WorkorderStatus.IN_PROGRESS
    _add_event(
        session,
        workorder_id=workorder_id,
        event_type="disposition",
        actor_account_id=account_id,
        actor_role=role,
        payload={"role": role, "plan_length": len(plan)},
    )
    await session.flush()
    await _publish_event(session, "ticket.updated", workorder)
    logger.info(
        "工单处理方案已提交",
        extra={
            "event": "workorder_disposition",
            "object_type": "workorder",
            "object_id": str(workorder_id),
            "actor_id": str(account_id),
            "actor_role": role,
        },
    )
    return disposition


# ---- 管理员终审关闭 ----


async def close_work_order(
    session: AsyncSession,
    *,
    workorder_id: UUID,
    admin_account_id: UUID,
    note: str | None = None,
) -> Workorder:
    """双角色处理项都 handled 后才允许关闭；否则冲突并列出未完成角色。"""
    account = await _load_account(session, admin_account_id)
    if account.role != AccountRole.ADMIN:
        raise WorkOrderForbidden("仅管理员可关闭工单")

    workorder = await _load_work_order(session, workorder_id)
    if workorder.status in CLOSED_STATUSES:
        raise WorkOrderConflict("工单已关闭，不可重复关闭")

    rows = (
        await session.execute(
            select(WorkorderDisposition).where(
                WorkorderDisposition.workorder_id == workorder_id
            )
        )
    ).scalars().all()
    handled = {
        row.role
        for row in rows
        if row.status == WorkorderDispositionStatus.HANDLED
    }
    pending = [role for role in STAFF_ROLES if role not in handled]
    if pending:
        raise WorkOrderConflict(f"尚有角色未完成处理：{'、'.join(pending)}")

    workorder.status = WorkorderStatus.RESOLVED
    workorder.resolved_at = _now()
    workorder.closed_by = admin_account_id
    workorder.closed_note = note
    _add_event(
        session,
        workorder_id=workorder_id,
        event_type="closed",
        actor_account_id=admin_account_id,
        actor_role=AccountRole.ADMIN,
        payload={"roles_completed": [str(role) for role in STAFF_ROLES]},
    )
    await session.flush()
    await _publish_event(session, "ticket.resolved", workorder)
    logger.info(
        "工单已终审关闭",
        extra={
            "event": "workorder_closed",
            "object_type": "workorder",
            "object_id": str(workorder_id),
            "actor_id": str(admin_account_id),
            "actor_role": AccountRole.ADMIN,
        },
    )
    return workorder


# ---- 查询 ----


async def list_dispositions(
    session: AsyncSession, workorder_ids: list[UUID]
) -> dict[UUID, list[WorkorderDisposition]]:
    """批量加载处理项，按工单分组（角色升序）；空入参不发查询。"""
    grouped: dict[UUID, list[WorkorderDisposition]] = {
        workorder_id: [] for workorder_id in workorder_ids
    }
    if not workorder_ids:
        return grouped
    rows = (
        await session.execute(
            select(WorkorderDisposition)
            .where(WorkorderDisposition.workorder_id.in_(workorder_ids))
            .order_by(WorkorderDisposition.role)
        )
    ).scalars().all()
    for row in rows:
        grouped[row.workorder_id].append(row)
    return grouped


async def get_work_order(
    session: AsyncSession,
    *,
    workorder_id: UUID,
    role: str,
    account_id: UUID,
) -> Workorder:
    """按角色可见范围加载单条工单；范围外/未绑定家属一律 404（不暴露存在性）。"""
    workorder = await _load_work_order(session, workorder_id)
    if role == AccountRole.FAMILY:
        await _assert_family_link(session, account_id, workorder.elder_id)
        return workorder
    visible = _visible_elder_ids(role, account_id)
    if visible is not None:
        allowed = (
            await session.execute(
                select(Workorder.workorder_id).where(
                    Workorder.workorder_id == workorder_id,
                    Workorder.elder_id.in_(visible),
                )
            )
        ).scalar_one_or_none()
        if allowed is None:
            raise WorkOrderNotFound("工单不存在")
    return workorder


async def list_work_orders(
    session: AsyncSession,
    *,
    role: str,
    account_id: UUID,
    elder_id: UUID | None = None,
    q: str | None = None,
    status: str | None = None,
    sort: str = "created_at",
    order: str = "desc",
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Workorder], int]:
    """按角色可见范围返回工单列表与总数；q 匹配标题/类别/触发原句/方案全文。"""
    conditions: list[Any] = []

    if role == AccountRole.FAMILY:
        if elder_id is None:
            raise WorkOrderNotFound("工单不存在")
        await _assert_family_link(session, account_id, elder_id)
    else:
        visible = _visible_elder_ids(role, account_id)
        if visible is not None:
            conditions.append(Workorder.elder_id.in_(visible))

    if elder_id is not None:
        conditions.append(Workorder.elder_id == elder_id)
    if status and status in set(WorkorderStatus):
        conditions.append(Workorder.status == status)

    keyword = (q or "").strip()
    if keyword:
        pattern = _like_pattern(keyword)
        plan_match = exists(
            select(WorkorderDisposition.disposition_id).where(
                WorkorderDisposition.workorder_id == Workorder.workorder_id,
                WorkorderDisposition.plan_text.ilike(pattern, escape="\\"),
            )
        )
        conditions.append(
            or_(
                Workorder.title.ilike(pattern, escape="\\"),
                Workorder.sensitive_category.ilike(pattern, escape="\\"),
                Workorder.description.ilike(pattern, escape="\\"),
                plan_match,
            )
        )

    total = int(
        (
            await session.execute(
                select(func.count()).select_from(Workorder).where(*conditions)
            )
        ).scalar_one()
    )

    sort_key = sort if sort in ("created_at", "priority") else "created_at"
    desc = order != "asc"
    if sort_key == "priority":
        priority_rank = case(
            (Workorder.priority == WorkorderPriority.URGENT, 4),
            (Workorder.priority == WorkorderPriority.HIGH, 3),
            (Workorder.priority == WorkorderPriority.NORMAL, 2),
            else_=1,
        )
        order_by = [
            priority_rank.desc() if desc else priority_rank.asc(),
            Workorder.created_at.desc(),
        ]
    else:
        order_by = [Workorder.created_at.desc() if desc else Workorder.created_at.asc()]

    page = max(1, int(page or 1))
    page_size = min(100, max(1, int(page_size or 20)))
    stmt = (
        select(Workorder)
        .where(*conditions)
        .order_by(*order_by)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return rows, total


async def count_open_by_role(
    session: AsyncSession, *, role: str, account_id: UUID
) -> int:
    """该账号可见范围内、该角色处理项未 handled 且工单未关闭的数量。"""
    if role not in STAFF_ROLES:
        return 0
    account = await _load_account(session, account_id)
    _require_staff_role(account, role)

    conditions: list[Any] = [Workorder.status.not_in(CLOSED_STATUSES)]
    visible = _visible_elder_ids(role, account_id)
    if visible is not None:
        conditions.append(Workorder.elder_id.in_(visible))
    conditions.append(
        exists(
            select(WorkorderDisposition.disposition_id).where(
                WorkorderDisposition.workorder_id == Workorder.workorder_id,
                WorkorderDisposition.role == role,
                WorkorderDisposition.status != WorkorderDispositionStatus.HANDLED,
            )
        )
    )

    return int(
        (
            await session.execute(
                select(func.count()).select_from(Workorder).where(*conditions)
            )
        ).scalar_one()
    )


# ---- 上下文快照 ----


def build_context_snapshot(
    messages: list[dict[str, Any]],
    trigger_text: str,
    assistant_reply: str | None,
    max_turns: int = SNAPSHOT_MAX_TURNS,
) -> list[dict[str, Any]]:
    """取触发前最近 N 轮（最多 2N 条 user/assistant）+ 触发句 + 首条安全回复。

    不足 N 轮按实际返回；不补造内容、不含音频。
    """
    turns = max(1, int(max_turns or SNAPSHOT_MAX_TURNS))
    history = [
        message
        for message in (messages or [])
        if message.get("role") in ("user", "assistant")
    ][-(turns * 2) :]

    # 历史消息缺时间戳时用触发时刻兜底，保证快照每项都有 at
    trigger_at = _iso(history[-1].get("created_at")) if history else None
    trigger_at = trigger_at or _now().isoformat()
    snapshot = [
        {
            "role": message.get("role"),
            "content": message.get("content") or "",
            "at": _iso(message.get("created_at")) or trigger_at,
        }
        for message in history
    ]
    snapshot.append({"role": "user", "content": trigger_text, "at": trigger_at})
    if assistant_reply:
        snapshot.append(
            {"role": "assistant", "content": assistant_reply, "at": trigger_at}
        )
    return snapshot
