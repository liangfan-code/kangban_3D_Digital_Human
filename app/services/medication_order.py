"""服药任务单领域服务。

关键约束：
- `(plan_id, scheduled_at)` 唯一，`ensure_order` 幂等；
- 计划状态（active/paused/cancelled）是调度前置条件；
- 所有事实变化先写 `medication_orders`，再追加 `medication_events`；
- 重试上限内回 pending，耗尽才 missed 并调用唯一漏服建单入口；
- 只把元数据写入事件 payload，不写对话原文。
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import UserIdentity
from app.core.logging import get_logger
from app.models import (
    Elder,
    MedicationEvent,
    MedicationOrder,
    MedicationPlan,
    Workorder,
)
from app.models.enums import (
    AccountRole,
    MedicationEventSource,
    MedicationEventType,
    MedicationOrderStatus,
    MedicationPlanStatus,
)
from app.schemas.medication_order import (
    FamilyMedicationEventOut,
    FamilyMedicationHistoryItem,
    FamilyMedicationHistoryResponse,
    StaffMedicationOrderItem,
    StaffMedicationOrderResponse,
)
from app.services import workorder as workorder_service
from app.services.authorization import AccessDecision, can_access_elder

logger = get_logger(__name__)

DEFAULT_RETRY_LIMIT = 3
FAMILY_PAGE_SIZE_MAX = 100
TERMINAL_STATUSES = (
    MedicationOrderStatus.CONFIRMED,
    MedicationOrderStatus.MISSED,
    MedicationOrderStatus.SKIPPED,
    MedicationOrderStatus.CANCELLED,
)
CREATION_SOURCES = (
    MedicationEventSource.SCHEDULER,
    MedicationEventSource.VOICE,
    MedicationEventSource.TEXT,
)
STAFF_ROLES = (AccountRole.DOCTOR, AccountRole.NURSE, AccountRole.ADMIN)


class MedicationOrderError(Exception):
    """服药任务领域异常基类。"""


class MedicationOrderNotFound(MedicationOrderError):
    """任务单/长者不存在或不可见。"""


class MedicationOrderForbidden(MedicationOrderError):
    """角色无权操作。"""


class MedicationOrderConflict(MedicationOrderError):
    """当前状态不允许该操作（如重复确认）。"""


class MedicationOrderValidationError(MedicationOrderError):
    """入参不合法。"""


def _now() -> datetime:
    return datetime.now(UTC)


async def _ensure_elder_access(
    session: AsyncSession, actor: UserIdentity, elder_id: uuid.UUID
) -> Elder:
    elder = await session.get(Elder, elder_id)
    if elder is None:
        raise MedicationOrderNotFound("长者不存在")
    decision = await can_access_elder(session, actor, elder)
    if decision == AccessDecision.FORBIDDEN:
        raise MedicationOrderForbidden("无权限查看该长者的服药记录")
    if decision != AccessDecision.ALLOWED:
        raise MedicationOrderNotFound("长者不存在")
    return elder


def _add_event(
    session: AsyncSession,
    *,
    order: MedicationOrder,
    event_type: str,
    source: str,
    actor_account_id: uuid.UUID | None = None,
    actor_role: str | None = None,
    payload: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
) -> None:
    session.add(
        MedicationEvent(
            order_id=order.order_id,
            plan_id=order.plan_id,
            elder_id=order.elder_id,
            event_type=event_type,
            source=source,
            actor_account_id=actor_account_id,
            actor_role=actor_role,
            payload=payload,
            dedupe_key=dedupe_key,
            created_at=_now(),
        )
    )


async def ensure_order(
    session: AsyncSession,
    *,
    plan: MedicationPlan,
    scheduled_at: datetime,
    created_source: str,
) -> tuple[MedicationOrder, bool]:
    """幂等创建/读取任务单：唯一键 `(plan_id, scheduled_at)`。"""
    if created_source not in CREATION_SOURCES:
        raise MedicationOrderValidationError("created_source 仅支持 scheduler/voice/text")
    if plan.status != MedicationPlanStatus.ACTIVE:
        raise MedicationOrderConflict("用药计划当前不生效，不能生成服药任务")

    stmt = (
        pg_insert(MedicationOrder)
        .values(
            plan_id=plan.plan_id,
            elder_id=plan.elder_id,
            scheduled_at=scheduled_at,
            status=MedicationOrderStatus.PENDING,
            attempt_count=0,
        )
        .on_conflict_do_nothing(index_elements=["plan_id", "scheduled_at"])
        .returning(MedicationOrder.order_id)
    )
    created_id = (await session.execute(stmt)).scalar_one_or_none()
    if created_id is None:
        existing = (
            await session.execute(
                select(MedicationOrder).where(
                    MedicationOrder.plan_id == plan.plan_id,
                    MedicationOrder.scheduled_at == scheduled_at,
                )
            )
        ).scalar_one_or_none()
        if existing is None:  # pragma: no cover - 唯一键冲突后必然存在
            raise MedicationOrderConflict("服药任务单创建失败")
        return existing, False

    order = await session.get(MedicationOrder, created_id)
    if order is None:  # pragma: no cover
        raise MedicationOrderConflict("服药任务单创建失败")
    _add_event(
        session,
        order=order,
        event_type=MedicationEventType.CREATED,
        source=created_source,
        payload={"scheduled_at": scheduled_at.isoformat()},
        dedupe_key=f"created:{order.order_id}:{created_source}",
    )
    await session.flush()
    logger.info(
        "服药任务单已创建",
        extra={
            "event": "medication_order_created",
            "object_type": "medication_order",
            "object_id": str(order.order_id),
            "plan_id": str(plan.plan_id),
            "elder_id": str(plan.elder_id),
            "source": created_source,
        },
    )
    return order, True


async def mark_reminded(
    session: AsyncSession,
    *,
    order: MedicationOrder,
    source: str,
) -> MedicationOrder:
    if source not in CREATION_SOURCES:
        raise MedicationOrderValidationError("source 仅支持 scheduler/voice/text")
    if order.status in TERMINAL_STATUSES:
        return order

    order.attempt_count = (order.attempt_count or 0) + 1
    order.last_attempt_at = _now()
    order.status = MedicationOrderStatus.CALLING
    _add_event(
        session,
        order=order,
        event_type=MedicationEventType.REMINDED,
        source=source,
        payload={"attempt": order.attempt_count, "channel": source},
        dedupe_key=f"reminded:{order.order_id}:{order.attempt_count}",
    )
    await session.flush()
    logger.info(
        "服药提醒已发起",
        extra={
            "event": "medication_order_reminded",
            "object_type": "medication_order",
            "object_id": str(order.order_id),
            "attempt": order.attempt_count,
            "source": source,
        },
    )
    return order


async def mark_confirmed(
    session: AsyncSession,
    *,
    order: MedicationOrder,
    source: str,
    actor: UserIdentity | None = None,
    actor_role: str | None = None,
) -> MedicationOrder:
    """同一来源重复确认幂等；已确认后换来源 409。"""
    if source not in (MedicationEventSource.VOICE, MedicationEventSource.TEXT):
        raise MedicationOrderValidationError("确认来源仅支持 voice/text")
    if order.status == MedicationOrderStatus.CONFIRMED:
        if order.confirmed_source == source:
            return order
        raise MedicationOrderConflict("服药任务已由其他来源确认")
    if order.status in (
        MedicationOrderStatus.MISSED,
        MedicationOrderStatus.SKIPPED,
        MedicationOrderStatus.CANCELLED,
    ):
        raise MedicationOrderConflict("当前状态的服药任务不可确认")

    order.status = MedicationOrderStatus.CONFIRMED
    order.confirmed_at = _now()
    order.confirmed_source = source
    _add_event(
        session,
        order=order,
        event_type=MedicationEventType.CONFIRMED,
        source=source,
        actor_account_id=actor.account_id if actor else None,
        actor_role=actor_role or (actor.role if actor else None),
        payload={"attempt": order.attempt_count},
        dedupe_key=f"confirmed:{order.order_id}",
    )
    await session.flush()
    logger.info(
        "服药任务已确认",
        extra={
            "event": "medication_order_confirmed",
            "object_type": "medication_order",
            "object_id": str(order.order_id),
            "source": source,
            "attempt": order.attempt_count,
        },
    )
    return order


async def mark_denied_or_timeout(
    session: AsyncSession,
    *,
    order: MedicationOrder,
    kind: str,
    source: str,
    retry_limit: int = DEFAULT_RETRY_LIMIT,
    startup_backfill: bool = False,
) -> MedicationOrder:
    """拒绝/超时：未耗尽回 pending；耗尽置 missed 并建漏服工单。"""
    if kind not in (MedicationEventType.DENIED, MedicationEventType.TIMEOUT):
        raise MedicationOrderValidationError("kind 仅支持 denied/timeout")
    if order.status in TERMINAL_STATUSES:
        return order

    attempt = max(order.attempt_count or 0, 1)
    if not startup_backfill:
        _add_event(
            session,
            order=order,
            event_type=kind,
            source=source,
            payload={"attempt": attempt},
            dedupe_key=f"{kind}:{order.order_id}:{attempt}",
        )

    exhausted = startup_backfill or attempt >= retry_limit
    if not exhausted:
        order.status = MedicationOrderStatus.PENDING
        await session.flush()
        logger.info(
            "服药提醒未完成，等待重试",
            extra={
                "event": "medication_order_retry_pending",
                "object_type": "medication_order",
                "object_id": str(order.order_id),
                "attempt": attempt,
                "kind": kind,
            },
        )
        return order

    order.status = MedicationOrderStatus.MISSED
    reason_code = (
        "startup_backfill"
        if startup_backfill
        else (
            "retry_exhausted"
            if kind == MedicationEventType.DENIED
            else "timeout_exhausted"
        )
    )
    _add_event(
        session,
        order=order,
        event_type=MedicationEventType.MISSED,
        source=source,
        payload={"attempt": attempt, "reason_code": reason_code},
        dedupe_key=f"missed:{order.order_id}",
    )
    await workorder_service.create_missed_medication_work_order(
        session, order=order, reason_code=reason_code
    )
    await session.flush()
    logger.info(
        "服药任务判定漏服",
        extra={
            "event": "medication_order_missed",
            "object_type": "medication_order",
            "object_id": str(order.order_id),
            "attempt": attempt,
            "reason_code": reason_code,
        },
    )
    return order


async def mark_skipped(
    session: AsyncSession,
    *,
    order: MedicationOrder,
    actor: UserIdentity,
    skip_reason: str | None,
) -> MedicationOrder:
    if actor.role not in STAFF_ROLES:
        raise MedicationOrderForbidden("仅员工可标记跳过")
    if order.status == MedicationOrderStatus.SKIPPED:
        return order
    if order.status in TERMINAL_STATUSES:
        raise MedicationOrderConflict("当前状态的服药任务不可跳过")

    order.status = MedicationOrderStatus.SKIPPED
    order.skip_reason = skip_reason
    _add_event(
        session,
        order=order,
        event_type=MedicationEventType.SKIPPED,
        source=MedicationEventSource.STAFF,
        actor_account_id=actor.account_id,
        actor_role=actor.role,
        payload={"has_reason": bool(skip_reason)},
        dedupe_key=f"skipped:{order.order_id}",
    )
    await session.flush()
    logger.info(
        "服药任务已标记跳过",
        extra={
            "event": "medication_order_skipped",
            "object_type": "medication_order",
            "object_id": str(order.order_id),
            "actor_id": str(actor.account_id),
            "actor_role": actor.role,
        },
    )
    return order


async def cancel_orders_for_plan(
    session: AsyncSession, *, plan_id: uuid.UUID
) -> list[MedicationOrder]:
    """计划取消：终态前置任务不退回，今后任务一律 cancelled。"""
    orders = (
        await session.execute(
            select(MedicationOrder).where(
                MedicationOrder.plan_id == plan_id,
                MedicationOrder.status.not_in(TERMINAL_STATUSES),
            )
        )
    ).scalars().all()
    for order in orders:
        order.status = MedicationOrderStatus.CANCELLED
        _add_event(
            session,
            order=order,
            event_type=MedicationEventType.CANCELLED,
            source=MedicationEventSource.SYSTEM,
            payload={"reason": "plan_cancelled"},
            dedupe_key=f"cancelled:{order.order_id}",
        )
    if orders:
        await session.flush()
        logger.info(
            "计划任务单已批量取消",
            extra={
                "event": "medication_orders_cancelled",
                "plan_id": str(plan_id),
                "count": len(orders),
            },
        )
    return list(orders)


def _event_out(event: MedicationEvent) -> FamilyMedicationEventOut:
    return FamilyMedicationEventOut(
        event_type=event.event_type,
        source=event.source,
        created_at=event.created_at,
    )


async def _events_by_order(
    session: AsyncSession, order_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[FamilyMedicationEventOut]]:
    if not order_ids:
        return {}
    rows = (
        await session.execute(
            select(MedicationEvent)
            .where(MedicationEvent.order_id.in_(order_ids))
            .order_by(MedicationEvent.created_at.asc())
        )
    ).scalars().all()
    grouped: dict[uuid.UUID, list[FamilyMedicationEventOut]] = {}
    for row in rows:
        grouped.setdefault(row.order_id, []).append(_event_out(row))
    return grouped


def _history_conditions(
    elder_id: uuid.UUID,
    plan_id: uuid.UUID | None,
    from_datetime: datetime | None,
    to_datetime: datetime | None,
) -> list[Any]:
    conditions: list[Any] = [MedicationOrder.elder_id == elder_id]
    if plan_id is not None:
        conditions.append(MedicationOrder.plan_id == plan_id)
    if from_datetime is not None:
        conditions.append(MedicationOrder.scheduled_at >= from_datetime)
    if to_datetime is not None:
        conditions.append(MedicationOrder.scheduled_at <= to_datetime)
    return conditions


async def family_history(
    session: AsyncSession,
    *,
    actor: UserIdentity,
    elder_id: uuid.UUID,
    plan_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 20,
    from_datetime: datetime | None = None,
    to_datetime: datetime | None = None,
) -> FamilyMedicationHistoryResponse:
    await _ensure_elder_access(session, actor, elder_id)
    if page < 1 or page_size < 1 or page_size > FAMILY_PAGE_SIZE_MAX:
        raise MedicationOrderValidationError("分页参数不合法")
    if from_datetime is not None and to_datetime is not None and from_datetime > to_datetime:
        raise MedicationOrderValidationError("开始时间不能晚于结束时间")
    if plan_id is not None:
        plan = await session.get(MedicationPlan, plan_id)
        if plan is None or plan.elder_id != elder_id:
            raise MedicationOrderNotFound("用药计划不存在")

    conditions = _history_conditions(elder_id, plan_id, from_datetime, to_datetime)
    total = (
        await session.execute(
            select(func.count()).select_from(MedicationOrder).where(*conditions)
        )
    ).scalar_one()

    rows = (
        await session.execute(
            select(MedicationOrder, MedicationPlan.drug_name, MedicationPlan.dose)
            .join(MedicationPlan, MedicationPlan.plan_id == MedicationOrder.plan_id)
            .where(*conditions)
            .order_by(MedicationOrder.scheduled_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    events = await _events_by_order(session, [row[0].order_id for row in rows])
    items = [
        FamilyMedicationHistoryItem(
            order_id=order.order_id,
            plan_id=order.plan_id,
            drug_name=drug_name,
            dose=dose,
            scheduled_at=order.scheduled_at,
            status=order.status,
            attempt_count=order.attempt_count,
            confirmed_at=order.confirmed_at,
            confirmed_source=order.confirmed_source,
            events=events.get(order.order_id, []),
        )
        for order, drug_name, dose in rows
    ]
    return FamilyMedicationHistoryResponse(
        items=items, total=total, page=page, page_size=page_size
    )


async def staff_orders(
    session: AsyncSession,
    *,
    actor: UserIdentity,
    elder_id: uuid.UUID,
    plan_id: uuid.UUID | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> StaffMedicationOrderResponse:
    if actor.role not in STAFF_ROLES:
        raise MedicationOrderForbidden("仅员工可查看服药任务核对视图")
    await _ensure_elder_access(session, actor, elder_id)
    if page < 1 or page_size < 1 or page_size > FAMILY_PAGE_SIZE_MAX:
        raise MedicationOrderValidationError("分页参数不合法")

    conditions: list[Any] = [MedicationOrder.elder_id == elder_id]
    if plan_id is not None:
        conditions.append(MedicationOrder.plan_id == plan_id)
    if status is not None:
        conditions.append(MedicationOrder.status == status)

    total = (
        await session.execute(
            select(func.count()).select_from(MedicationOrder).where(*conditions)
        )
    ).scalar_one()
    rows = (
        await session.execute(
            select(MedicationOrder, MedicationPlan.drug_name, MedicationPlan.dose)
            .join(MedicationPlan, MedicationPlan.plan_id == MedicationOrder.plan_id)
            .where(*conditions)
            .order_by(MedicationOrder.scheduled_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    order_ids = [row[0].order_id for row in rows]
    events = await _events_by_order(session, order_ids)
    workorders = (
        (
            await session.execute(
                select(Workorder).where(Workorder.medication_order_id.in_(order_ids))
            )
        ).scalars().all()
        if order_ids
        else []
    )
    workorder_by_order = {
        workorder.medication_order_id: workorder.workorder_id for workorder in workorders
    }
    items = [
        StaffMedicationOrderItem(
            order_id=order.order_id,
            plan_id=order.plan_id,
            drug_name=drug_name,
            dose=dose,
            scheduled_at=order.scheduled_at,
            status=order.status,
            attempt_count=order.attempt_count,
            confirmed_at=order.confirmed_at,
            confirmed_source=order.confirmed_source,
            skip_reason=order.skip_reason,
            workorder_id=workorder_by_order.get(order.order_id),
            events=events.get(order.order_id, []),
        )
        for order, drug_name, dose in rows
    ]
    return StaffMedicationOrderResponse(
        items=items, total=total, page=page, page_size=page_size
    )
