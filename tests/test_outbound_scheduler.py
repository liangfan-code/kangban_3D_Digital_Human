"""Day7 段4：到点扫描、启动补漏与调度器降级（不连真实外呼）。"""

import logging
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import MedicationEvent, MedicationOrder, MedicationPlan, Workorder
from app.models.enums import (
    MedicationEventSource,
    MedicationEventType,
    MedicationOrderStatus,
    MedicationPlanStatus,
)
from app.services import medication_scheduler as scheduler
from app.services.medication_order import ensure_order

ZONE = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 12, 8, 2, tzinfo=ZONE)  # 周四


@pytest.fixture
def session_factory(db_connection):
    """与测试事务同一连接的多会话工厂（模拟应用内 get_session_factory）。"""
    return async_sessionmaker(
        bind=db_connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )


def _plan(times, *, weekdays=None, start=None, end=None, status=MedicationPlanStatus.ACTIVE):
    return SimpleNamespace(
        schedule={"times": times, "weekdays": weekdays or []},
        start_date=start or date(2026, 11, 1),
        end_date=end,
        status=status,
    )


def test_due_items_only_within_window():
    items = scheduler.due_items(NOW, [_plan(["07:40", "08:00", "08:30"])], window_minutes=5)
    assert [i.scheduled_at for i in items] == [datetime(2026, 11, 12, 0, 0, tzinfo=UTC)]


def test_due_items_filters_weekday_and_date_range():
    thursday_not_in_weekdays = _plan(["08:00"], weekdays=[0, 1, 2])
    ended_yesterday = _plan(["08:00"], end=date(2026, 11, 11))
    future_plan = _plan(["08:00"], start=date(2026, 11, 13))
    assert scheduler.due_items(
        NOW,
        [thursday_not_in_weekdays, ended_yesterday, future_plan],
        window_minutes=5,
    ) == []


async def _make_plan(db_session, elder, times, *, start=None):
    plan = MedicationPlan(
        elder_id=elder.elder_id,
        drug_name="降压药",
        dose="1 片",
        schedule={"times": times, "weekdays": []},
        start_date=start or date(2026, 11, 1),
        end_date=None,
        status=MedicationPlanStatus.ACTIVE,
    )
    db_session.add(plan)
    await db_session.flush()
    return plan


async def _make_order(
    db_session,
    plan,
    *,
    status=MedicationOrderStatus.PENDING,
    attempt=0,
    last_attempt_at=None,
    scheduled_at=None,
):
    order = MedicationOrder(
        plan_id=plan.plan_id,
        elder_id=plan.elder_id,
        scheduled_at=scheduled_at or datetime(2026, 11, 12, 0, 0, tzinfo=UTC),
        status=status,
        attempt_count=attempt,
        last_attempt_at=last_attempt_at,
    )
    db_session.add(order)
    await db_session.flush()
    return order


async def test_scan_is_idempotent_and_triggers_once(db_session, session_factory, seed):
    scheduler.reset_trigger_cache()
    plan = await _make_plan(db_session, seed["elders"]["single"], ["08:00"])
    calls: list[str] = []

    async def fake_trigger(order_id, **kwargs):
        calls.append(str(order_id))

    first = await scheduler.scan_due_medications(
        now=NOW, session_factory=session_factory, trigger=fake_trigger
    )
    second = await scheduler.scan_due_medications(
        now=NOW, session_factory=session_factory, trigger=fake_trigger
    )

    orders = (
        await db_session.execute(
            select(MedicationOrder).where(MedicationOrder.plan_id == plan.plan_id)
        )
    ).scalars().all()
    assert len(orders) == 1
    assert first["triggered"] == 1
    assert second["triggered"] == 0
    assert len(calls) == 1


async def test_timeout_counts_and_retries(db_session, session_factory, seed):
    scheduler.reset_trigger_cache()
    plan = await _make_plan(db_session, seed["elders"]["single"], ["08:00"])
    order = await _make_order(
        db_session,
        plan,
        status=MedicationOrderStatus.CALLING,
        attempt=1,
        last_attempt_at=NOW.astimezone(UTC) - timedelta(seconds=31),
    )
    calls: list[str] = []

    async def fake_trigger(order_id, **kwargs):
        calls.append(str(order_id))

    summary = await scheduler.scan_due_medications(
        now=NOW, session_factory=session_factory, trigger=fake_trigger
    )
    await db_session.refresh(order)

    assert summary["timed_out"] >= 1
    assert order.status == MedicationOrderStatus.PENDING
    assert calls, "超时后应在同一轮立即重呼"
    events = (
        await db_session.execute(
            select(MedicationEvent.event_type).where(MedicationEvent.order_id == order.order_id)
        )
    ).scalars().all()
    assert MedicationEventType.TIMEOUT in events


async def test_third_timeout_marks_missed_with_single_workorder(
    db_session, session_factory, seed
):
    scheduler.reset_trigger_cache()
    plan = await _make_plan(db_session, seed["elders"]["single"], ["08:00"])
    order = await _make_order(
        db_session,
        plan,
        status=MedicationOrderStatus.CALLING,
        attempt=3,
        last_attempt_at=NOW.astimezone(UTC) - timedelta(seconds=31),
    )
    calls: list[str] = []

    async def fake_trigger(order_id, **kwargs):
        calls.append(str(order_id))

    summary = await scheduler.scan_due_medications(
        now=NOW, session_factory=session_factory, trigger=fake_trigger
    )
    await db_session.refresh(order)
    workorders = (
        await db_session.execute(
            select(Workorder).where(Workorder.medication_order_id == order.order_id)
        )
    ).scalars().all()

    assert order.status == MedicationOrderStatus.MISSED
    assert summary["timed_out"] == 1
    assert len(workorders) == 1
    assert workorders[0].type == "missed_medication"
    assert not calls, "3 次后不应再外呼"

    # 再扫一轮：终态订单不再建单/建工单
    await scheduler.scan_due_medications(
        now=NOW, session_factory=session_factory, trigger=fake_trigger
    )
    workorders_again = (
        await db_session.execute(
            select(Workorder).where(Workorder.medication_order_id == order.order_id)
        )
    ).scalars().all()
    assert len(workorders_again) == 1


async def test_backfill_marks_past_slot_missed_once(db_session, session_factory, seed):
    plan = await _make_plan(db_session, seed["elders"]["single"], ["08:00"])
    now = datetime(2026, 11, 12, 14, 0, tzinfo=ZONE)

    first = await scheduler.backfill_medication_orders(now=now, session_factory=session_factory)
    second = await scheduler.backfill_medication_orders(now=now, session_factory=session_factory)

    order = (
        await db_session.execute(
            select(MedicationOrder).where(MedicationOrder.plan_id == plan.plan_id)
        )
    ).scalars().one()
    workorders = (
        await db_session.execute(
            select(Workorder).where(Workorder.medication_order_id == order.order_id)
        )
    ).scalars().all()

    assert first["created_order"] == 1
    assert first["missed"] == 1
    assert second["missed"] == 0
    assert order.status == MedicationOrderStatus.MISSED
    assert len(workorders) == 1


async def test_backfill_skips_fresh_calling_order(db_session, session_factory, seed):
    plan = await _make_plan(db_session, seed["elders"]["single"], ["08:00"])

    now = datetime(2026, 11, 12, 8, 30, tzinfo=ZONE)
    async with session_factory() as session:
        order, _ = await ensure_order(
            session,
            plan=plan,
            scheduled_at=datetime(2026, 11, 12, 0, 0, tzinfo=UTC),
            created_source=MedicationEventSource.SCHEDULER,
        )
        order.status = MedicationOrderStatus.CALLING
        order.attempt_count = 1
        order.last_attempt_at = now.astimezone(UTC) - timedelta(seconds=5)
        await session.commit()
        order_id = order.order_id

    summary = await scheduler.backfill_medication_orders(now=now, session_factory=session_factory)

    awaited = await db_session.get(MedicationOrder, order_id)
    assert summary["missed"] == 0
    assert awaited.status == MedicationOrderStatus.CALLING


def test_scheduler_unavailable_reasons(monkeypatch):
    disabled = SimpleNamespace(medication_reminder_enabled=False, app_env="local")
    assert scheduler.scheduler_unavailable_reason(disabled) == "disabled"

    enabled = SimpleNamespace(medication_reminder_enabled=True, app_env="local")
    monkeypatch.setattr(scheduler, "_is_test_environment", lambda _s: False)
    monkeypatch.setitem(__import__("sys").modules, "apscheduler.schedulers.asyncio", None)
    assert scheduler.scheduler_unavailable_reason(enabled) == "not_installed"


async def test_start_scheduler_degrades_visibly(monkeypatch, caplog):
    monkeypatch.setattr(scheduler, "_is_test_environment", lambda _s: True)
    settings = SimpleNamespace(medication_reminder_enabled=True, app_env="local")
    with caplog.at_level(logging.INFO):
        started = await scheduler.start_medication_scheduler(settings)
    assert started is None
    assert any(
        getattr(record, "event", "") == "medication_scheduler_unavailable"
        for record in caplog.records
    )
