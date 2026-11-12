"""用药到点扫描、启动补漏与 APScheduler 生命周期（Day7 段4）。

分工：
- ``due_items`` 纯函数：给定 now 与计划，算出窗口内应该提醒的时段（可直接单测）；
- ``scan_due_medications``：扫描 → 幂等 ``ensure_order`` + commit → 外呼 / 超时推进；
- ``backfill_medication_orders``：启动补漏（最近 N 小时，过期未终态落 missed）；
- ``start_medication_scheduler``：APScheduler 起停；未安装/关闭/测试环境都可见降级。

约束：
- 只 setInterval 一个 job（``max_instances=1``、``coalesce=True``），job 内不 sleep 等用户回复；
- 一切写库先 commit，再 ``publish_workorder_event``；
- 漏服只走段1 ``create_missed_medication_work_order``（由 ``mark_denied_or_timeout`` 内部调用）。
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.models import MedicationOrder, MedicationPlan, Workorder
from app.models.enums import (
    MedicationEventSource,
    MedicationEventType,
    MedicationOrderStatus,
    MedicationPlanStatus,
)
from app.services import medication_order as order_service
from app.services.medication_plan import schedule_times, schedule_weekdays
from app.services.outbound import trigger_medication_call
from app.services.workorder_events import publish_workorder_event

logger = get_logger(__name__)

TERMINAL_STATUSES = (
    MedicationOrderStatus.CONFIRMED,
    MedicationOrderStatus.MISSED,
    MedicationOrderStatus.SKIPPED,
    MedicationOrderStatus.CANCELLED,
)
# 进程内触发去重只作性能优化（(order_id, 第几次) → 时间戳），DB 幂等仍是唯一真源
_TRIGGERED: dict[tuple[str, int], float] = {}
_TRIGGER_TTL_SECONDS = 3600.0
_SCHEDULER: Any | None = None


@dataclass(frozen=True)
class DueItem:
    """一个待提醒时段；scheduled_at 一律 UTC aware。"""

    plan: Any
    scheduled_at: datetime


def _app_zone(settings: Settings) -> ZoneInfo:
    return ZoneInfo(settings.app_timezone)


def _plan_active_on(plan: Any, local_date: date) -> bool:
    start = getattr(plan, "start_date", None)
    end = getattr(plan, "end_date", None)
    if start is not None and local_date < start:
        return False
    if end is not None and local_date > end:
        return False
    return True


def due_items(
    now: datetime,
    plans: list[Any],
    *,
    window_minutes: int | None = None,
    settings: Settings | None = None,
) -> list[DueItem]:
    """到点判定纯函数：|now - slot| <= 窗口，且计划生效日期/星期匹配。"""
    settings = settings or get_settings()
    window = (
        settings.medication_reminder_window_minutes if window_minutes is None else window_minutes
    )
    zone = _app_zone(settings)
    local_now = now.astimezone(zone)
    items: list[DueItem] = []
    for plan in plans:
        status = getattr(plan, "status", None)
        if status is not None and status != MedicationPlanStatus.ACTIVE:
            continue
        if not _plan_active_on(plan, local_now.date()):
            continue
        weekdays = schedule_weekdays(plan)
        if weekdays is not None and local_now.weekday() not in weekdays:
            continue
        for moment in schedule_times(plan):
            slot_local = datetime.combine(local_now.date(), moment, tzinfo=zone)
            if abs((local_now - slot_local).total_seconds()) <= window * 60:
                items.append(DueItem(plan=plan, scheduled_at=slot_local.astimezone(UTC)))
    items.sort(key=lambda item: item.scheduled_at)
    return items


def _past_slots(
    now: datetime,
    plans: list[Any],
    *,
    settings: Settings,
) -> list[DueItem]:
    """补漏时段：最近 N 小时、已过提醒窗口的时段（新时段交给调度器正常提醒）。"""
    zone = _app_zone(settings)
    local_now = now.astimezone(zone)
    cutoff = local_now - timedelta(minutes=settings.medication_reminder_window_minutes)
    start_local = local_now - timedelta(hours=settings.medication_backfill_lookback_hours)
    items: list[DueItem] = []
    day = start_local.date()
    while day <= local_now.date():
        for plan in plans:
            status = getattr(plan, "status", None)
            if status is not None and status != MedicationPlanStatus.ACTIVE:
                continue
            if not _plan_active_on(plan, day):
                continue
            weekdays = schedule_weekdays(plan)
            if weekdays is not None and day.weekday() not in weekdays:
                continue
            for moment in schedule_times(plan):
                slot_local = datetime.combine(day, moment, tzinfo=zone)
                if start_local <= slot_local <= cutoff:
                    items.append(DueItem(plan=plan, scheduled_at=slot_local.astimezone(UTC)))
        day += timedelta(days=1)
    items.sort(key=lambda item: item.scheduled_at)
    return items


def _is_test_environment(settings: Settings) -> bool:
    return settings.app_env.strip().lower() in {"test", "testing", "pytest"} or (
        "pytest" in sys.modules
    )


def scheduler_unavailable_reason(settings: Settings | None = None) -> str | None:
    """返回不可用原因（disabled/test/not_installed）；None 表示可以启动。"""
    settings = settings or get_settings()
    if not settings.medication_reminder_enabled:
        return "disabled"
    if _is_test_environment(settings):
        return "test"
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler  # noqa: F401
    except ImportError:
        return "not_installed"
    return None


def reset_trigger_cache() -> None:
    """测试辅助：清空进程内触发去重缓存。"""
    _TRIGGERED.clear()


def _mark_triggered(order_id: str, attempt: int) -> bool:
    """同一单同一次尝试只触发一次；返回 False 表示已在缓存中。"""
    now = time.monotonic()
    for key, at in list(_TRIGGERED.items()):
        if now - at > _TRIGGER_TTL_SECONDS:
            _TRIGGERED.pop(key, None)
    key = (order_id, attempt)
    if key in _TRIGGERED:
        return False
    _TRIGGERED[key] = now
    return True


def _timed_out(order: MedicationOrder, now: datetime, timeout_seconds: int) -> bool:
    last = order.last_attempt_at
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return now >= last + timedelta(seconds=max(1, timeout_seconds))


async def _publish_new_workorder(
    session: Any, order_id: Any, *, had_workorder: bool
) -> None:
    if had_workorder:
        return
    workorder = (
        await session.execute(
            select(Workorder).where(Workorder.medication_order_id == order_id)
        )
    ).scalars().first()
    if workorder is not None:
        await publish_workorder_event("ticket.created", workorder)


async def _advance_timeout(
    order_id: Any, *, factory: Callable[[], Any], settings: Settings
) -> str:
    """超时推进（DB 为权威）；返回推进后的订单状态。"""
    async with factory() as session:
        order = await session.get(MedicationOrder, order_id)
        if order is None or order.status in TERMINAL_STATUSES:
            return order.status if order is not None else "unknown"
        had_workorder = (
            await session.scalar(
                select(Workorder.workorder_id).where(
                    Workorder.medication_order_id == order.order_id
                )
            )
        ) is not None
        attempt_before = order.attempt_count or 0
        order = await order_service.mark_denied_or_timeout(
            session,
            order=order,
            kind=MedicationEventType.TIMEOUT,
            source=MedicationEventSource.SCHEDULER,
            retry_limit=max(1, settings.medication_retry_limit),
        )
        await session.commit()
        await _publish_new_workorder(session, order.order_id, had_workorder=had_workorder)
        logger.info(
            "服药超时推进",
            extra={
                "event": "medication_timeout_advanced",
                "object_type": "medication_order",
                "object_id": str(order.order_id),
                "status": order.status,
                "attempt_before": attempt_before,
                "attempt": order.attempt_count or 0,
            },
        )
        return order.status


async def _advance_startup_missed(
    order_id: Any, *, factory: Callable[[], Any], settings: Settings
) -> bool:
    """启动补漏：过期未终态直接落 missed（reason=startup_backfill）。"""
    async with factory() as session:
        order = await session.get(MedicationOrder, order_id)
        if order is None or order.status in TERMINAL_STATUSES:
            return False
        had_workorder = (
            await session.scalar(
                select(Workorder.workorder_id).where(
                    Workorder.medication_order_id == order.order_id
                )
            )
        ) is not None
        order = await order_service.mark_denied_or_timeout(
            session,
            order=order,
            kind=MedicationEventType.TIMEOUT,
            source=MedicationEventSource.SCHEDULER,
            retry_limit=0,
            startup_backfill=True,
        )
        await session.commit()
        await _publish_new_workorder(session, order.order_id, had_workorder=had_workorder)
        logger.info(
            "启动补漏落 missed",
            extra={
                "event": "medication_backfill_missed",
                "object_type": "medication_order",
                "object_id": str(order.order_id),
                "scheduled_at": order.scheduled_at.isoformat(),
            },
        )
        return True


async def scan_due_medications(
    *,
    now: datetime | None = None,
    session_factory: Callable[[], Any] | None = None,
    trigger: Callable[..., Awaitable[Any]] | None = None,
    settings: Settings | None = None,
) -> dict[str, int]:
    """扫描一轮：幂等建单 → 超时推进 → 外呼；返回计数用于日志/手工验收。"""
    settings = settings or get_settings()
    factory = session_factory or get_session_factory()
    do_trigger = trigger or trigger_medication_call
    reference = (now or datetime.now(UTC)).astimezone(UTC)
    summary = {
        "due": 0,
        "created_order": 0,
        "triggered": 0,
        "failed": 0,
        "timed_out": 0,
        "skipped": 0,
    }
    if not settings.medication_reminder_enabled:
        logger.info(
            "用药提醒已关闭，跳过本轮扫描",
            extra={"event": "medication_scheduler_unavailable", "reason": "disabled"},
        )
        return summary

    async with factory() as session:
        plans = (
            await session.execute(
                select(MedicationPlan).where(
                    MedicationPlan.status == MedicationPlanStatus.ACTIVE
                )
            )
        ).scalars().all()

    for item in due_items(reference, plans, settings=settings):
        summary["due"] += 1
        async with factory() as session:
            plan = await session.get(MedicationPlan, item.plan.plan_id)
            if plan is None or plan.status != MedicationPlanStatus.ACTIVE:
                summary["skipped"] += 1
                continue
            order, created = await order_service.ensure_order(
                session,
                plan=plan,
                scheduled_at=item.scheduled_at,
                created_source=MedicationEventSource.SCHEDULER,
            )
            await session.commit()
            if created:
                summary["created_order"] += 1
            order_id = order.order_id
            status = order.status
            attempt = order.attempt_count or 0
            timed_out = status == MedicationOrderStatus.CALLING and _timed_out(
                order, reference, settings.medication_call_timeout_seconds
            )

        if status in TERMINAL_STATUSES:
            summary["skipped"] += 1
            continue
        if status == MedicationOrderStatus.CALLING:
            if not timed_out:
                summary["skipped"] += 1
                continue
            status = await _advance_timeout(order_id, factory=factory, settings=settings)
            summary["timed_out"] += 1
            if status in TERMINAL_STATUSES:
                continue
            # 时长未耗尽：本单回到 pending，本轮立即重呼（仍受去重缓存约束）

        if not _mark_triggered(str(order_id), attempt + 1):
            summary["skipped"] += 1
            continue
        try:
            await do_trigger(
                order_id,
                session_factory=factory,
                checkpoint_path=settings.medication_checkpoint_path,
                settings=settings,
            )
            summary["triggered"] += 1
        except Exception as exc:  # noqa: BLE001 —— 单次外呼失败不终止整轮扫描
            summary["failed"] += 1
            logger.error(
                "用药外呼失败",
                extra={
                    "event": "medication_scan_trigger_failed",
                    "object_type": "medication_order",
                    "object_id": str(order_id),
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )

    # 兜底：进程重启后仍停留在 calling 且已超时的订单（时段可能已出窗口）
    timeout_cutoff = reference - timedelta(seconds=max(1, settings.medication_call_timeout_seconds))
    async with factory() as session:
        stale = (
            await session.execute(
                select(MedicationOrder.order_id, MedicationOrder.attempt_count).where(
                    MedicationOrder.status == MedicationOrderStatus.CALLING,
                    MedicationOrder.last_attempt_at.is_not(None),
                    MedicationOrder.last_attempt_at <= timeout_cutoff,
                    MedicationOrder.scheduled_at
                    >= reference - timedelta(hours=settings.medication_backfill_lookback_hours),
                )
            )
        ).all()
    for order_id, attempt in stale:
        status = await _advance_timeout(order_id, factory=factory, settings=settings)
        summary["timed_out"] += 1
        if status in TERMINAL_STATUSES:
            continue
        if not _mark_triggered(str(order_id), (attempt or 0) + 1):
            continue
        try:
            await do_trigger(
                order_id,
                session_factory=factory,
                checkpoint_path=settings.medication_checkpoint_path,
                settings=settings,
            )
            summary["triggered"] += 1
        except Exception as exc:  # noqa: BLE001
            summary["failed"] += 1
            logger.error(
                "超时重呼失败",
                extra={
                    "event": "medication_scan_trigger_failed",
                    "object_type": "medication_order",
                    "object_id": str(order_id),
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )

    logger.info(
        "用药扫描完成",
        extra={"event": "medication_scan_summary", **summary},
    )
    return summary


async def backfill_medication_orders(
    *,
    now: datetime | None = None,
    session_factory: Callable[[], Any] | None = None,
    settings: Settings | None = None,
) -> dict[str, int]:
    """启动补漏：最近 N 小时的时段缺单先建，过期未终态落 missed（幂等）。"""
    settings = settings or get_settings()
    factory = session_factory or get_session_factory()
    reference = (now or datetime.now(UTC)).astimezone(UTC)
    summary = {"scanned": 0, "created_order": 0, "missed": 0, "skipped": 0}

    async with factory() as session:
        plans = (
            await session.execute(
                select(MedicationPlan).where(
                    MedicationPlan.status == MedicationPlanStatus.ACTIVE
                )
            )
        ).scalars().all()

    for item in _past_slots(reference, plans, settings=settings):
        summary["scanned"] += 1
        async with factory() as session:
            plan = await session.get(MedicationPlan, item.plan.plan_id)
            if plan is None or plan.status != MedicationPlanStatus.ACTIVE:
                summary["skipped"] += 1
                continue
            order, created = await order_service.ensure_order(
                session,
                plan=plan,
                scheduled_at=item.scheduled_at,
                created_source=MedicationEventSource.SCHEDULER,
            )
            await session.commit()
            if created:
                summary["created_order"] += 1
            order_id = order.order_id
            status = order.status
            still_fresh = status == MedicationOrderStatus.CALLING and not _timed_out(
                order, reference, settings.medication_call_timeout_seconds
            )

        if status in TERMINAL_STATUSES or still_fresh:
            summary["skipped"] += 1
            continue
        if await _advance_startup_missed(order_id, factory=factory, settings=settings):
            summary["missed"] += 1

    logger.info(
        "用药启动补漏完成",
        extra={"event": "medication_backfill_summary", **summary},
    )
    return summary


async def _scan_job() -> None:
    """APScheduler job 包装：异常只记日志，绝不让调度器空转崩溃。"""
    try:
        await scan_due_medications()
    except Exception as exc:  # noqa: BLE001 —— job 级兜底
        logger.error(
            "用药扫描 job 异常",
            extra={"event": "medication_scan_job_failed", "error": f"{type(exc).__name__}: {exc}"},
        )


async def run_startup_backfill(
    *, now: datetime | None = None, session_factory: Callable[[], Any] | None = None
) -> dict[str, int] | None:
    """lifespan 启动阶段调用；任何异常都不得阻塞应用启动。"""
    settings = get_settings()
    if not settings.medication_reminder_enabled:
        logger.info(
            "用药提醒已关闭，跳过启动补漏",
            extra={"event": "medication_scheduler_unavailable", "reason": "disabled"},
        )
        return None
    if _is_test_environment(settings):
        logger.info(
            "测试环境不执行启动补漏",
            extra={"event": "medication_scheduler_unavailable", "reason": "test"},
        )
        return None
    try:
        return await backfill_medication_orders(
            now=now, session_factory=session_factory, settings=settings
        )
    except Exception as exc:  # noqa: BLE001 —— 补漏失败只降级，不阻塞启动
        logger.error(
            "用药启动补漏失败",
            extra={"event": "medication_backfill_failed", "error": f"{type(exc).__name__}: {exc}"},
        )
        return None


async def start_medication_scheduler(settings: Settings | None = None) -> Any | None:
    """启动 APScheduler 间隔扫描；不可用时返回 None 并记录可见原因。"""
    global _SCHEDULER
    settings = settings or get_settings()
    reason = scheduler_unavailable_reason(settings)
    if reason is not None:
        logger.info(
            "用药定时扫描未启动",
            extra={"event": "medication_scheduler_unavailable", "reason": reason},
        )
        return None
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    interval = max(5, settings.medication_scan_interval_seconds)
    scheduler = AsyncIOScheduler(timezone=settings.app_timezone)
    scheduler.add_job(
        _scan_job,
        trigger=IntervalTrigger(seconds=interval),
        id="medication_scan_due",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=max(30, interval // 2),
        replace_existing=True,
    )
    scheduler.start()
    _SCHEDULER = scheduler
    logger.info(
        "用药定时扫描已启动",
        extra={
            "event": "medication_scheduler_started",
            "interval_seconds": interval,
            "timezone": settings.app_timezone,
        },
    )
    return scheduler


async def shutdown_medication_scheduler() -> None:
    global _SCHEDULER
    scheduler = _SCHEDULER
    _SCHEDULER = None
    if scheduler is None:
        return
    try:
        scheduler.shutdown(wait=False)
        logger.info("用药定时扫描已停止", extra={"event": "medication_scheduler_stopped"})
    except Exception as exc:  # noqa: BLE001 —— 停止失败只记日志
        logger.error(
            "用药定时扫描停止失败",
            extra={
                "event": "medication_scheduler_stop_failed",
                "error": f"{type(exc).__name__}: {exc}",
            },
        )


__all__ = [
    "DueItem",
    "backfill_medication_orders",
    "due_items",
    "reset_trigger_cache",
    "run_startup_backfill",
    "scan_due_medications",
    "scheduler_unavailable_reason",
    "shutdown_medication_scheduler",
    "start_medication_scheduler",
]
