"""服药任务单：幂等、重试、漏服建单去重、确认/跳过、家属历史字段白名单。"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.auth.dependencies import UserIdentity
from app.models import (
    BuildingAssignment,
    DoctorElderAssignment,
    FamilyElderLink,
    MedicationEvent,
    MedicationOrder,
    Workorder,
)
from app.models.enums import MedicationOrderStatus, WorkorderStatus
from app.schemas.medication import MedicationPlanCreate
from app.services import medication_order as order_service
from app.services import medication_plan as plan_service
from app.services import workorder as workorder_service

SCHEDULED_AT = datetime(2026, 11, 11, 8, 0, tzinfo=UTC)
PLAN_PAYLOAD = {
    "drug_name": "二甲双胍",
    "dose": "0.5g",
    "schedule": {"times": ["08:00"], "weekdays": []},
    "instructions": "随餐",
    "internal_note": "内部：注意肾功能",
    "start_date": "2026-11-11",
}


def _identity(seed, username: str) -> UserIdentity:
    account = seed["accounts"][username]
    return UserIdentity(
        account_id=account.account_id,
        role=account.role,
        display_name=account.display_name,
    )


async def _setup(db_session, seed):
    db_session.add_all(
        [
            DoctorElderAssignment(
                account_id=seed["accounts"]["doctor"].account_id,
                elder_id=seed["elders"]["d1"].elder_id,
            ),
            BuildingAssignment(
                account_id=seed["accounts"]["nurse_a"].account_id,
                building_code="A",
            ),
            FamilyElderLink(
                account_id=seed["accounts"]["family"].account_id,
                elder_id=seed["elders"]["d1"].elder_id,
            ),
        ]
    )
    await db_session.flush()
    doctor = _identity(seed, "doctor")
    plan = await plan_service.create_plan(
        db_session,
        actor=doctor,
        elder_id=seed["elders"]["d1"].elder_id,
        payload=MedicationPlanCreate(**PLAN_PAYLOAD),
    )
    plan, workorder = await plan_service.confirm_plan(
        db_session, actor=doctor, plan_id=plan.plan_id
    )
    return doctor, plan, workorder


async def _missed_workorders(db_session, order_id) -> list[Workorder]:
    return list(
        (
            await db_session.execute(
                select(Workorder).where(Workorder.medication_order_id == order_id)
            )
        )
        .scalars()
        .all()
    )


async def test_ensure_order_idempotent_and_events_append_only(db_session, seed):
    doctor, plan, _ = await _setup(db_session, seed)

    order, created = await order_service.ensure_order(
        db_session, plan=plan, scheduled_at=SCHEDULED_AT, created_source="scheduler"
    )
    assert created is True
    again, created_again = await order_service.ensure_order(
        db_session, plan=plan, scheduled_at=SCHEDULED_AT, created_source="scheduler"
    )
    assert created_again is False
    assert again.order_id == order.order_id

    events = (
        await db_session.execute(
            select(MedicationEvent).where(MedicationEvent.order_id == order.order_id)
        )
    ).scalars().all()
    assert [event.event_type for event in events] == ["created"]


async def test_retry_exhausted_creates_single_missed_workorder(db_session, seed):
    doctor, plan, standing = await _setup(db_session, seed)
    order, _ = await order_service.ensure_order(
        db_session, plan=plan, scheduled_at=SCHEDULED_AT, created_source="scheduler"
    )

    await order_service.mark_reminded(db_session, order=order, source="scheduler")
    await order_service.mark_reminded(db_session, order=order, source="scheduler")
    await order_service.mark_denied_or_timeout(
        db_session, order=order, kind="denied", source="scheduler"
    )
    assert order.status == MedicationOrderStatus.PENDING
    assert order.attempt_count == 2

    await order_service.mark_reminded(db_session, order=order, source="scheduler")
    await order_service.mark_denied_or_timeout(
        db_session, order=order, kind="denied", source="scheduler"
    )
    assert order.status == MedicationOrderStatus.MISSED

    workorders = await _missed_workorders(db_session, order.order_id)
    assert len(workorders) == 1
    assert workorders[0].occurrence_count == 1
    assert workorders[0].dedupe_key == f"missed:{order.order_id}"
    assert workorders[0].medication_plan_id == plan.plan_id

    # 重复调用唯一入口：只累加 occurrence_count，不新建
    repeated, repeated_created = await workorder_service.create_missed_medication_work_order(
        db_session, order=order, reason_code="retry_exhausted"
    )
    assert repeated_created is False
    assert repeated.workorder_id == workorders[0].workorder_id
    assert repeated.occurrence_count == 2
    assert len(await _missed_workorders(db_session, order.order_id)) == 1

    # 计划生效期间，管理员不能手工关闭长期工单
    admin = _identity(seed, "admin")
    with pytest.raises(workorder_service.WorkOrderConflict):
        await workorder_service.close_work_order(
            db_session, workorder_id=standing.workorder_id, admin_account_id=admin.account_id
        )

    _, closed = await plan_service.cancel_plan(
        db_session, actor=doctor, plan_id=plan.plan_id
    )
    assert closed is not None
    assert closed.status == WorkorderStatus.CANCELLED


async def test_pending_order_cancelled_with_plan(db_session, seed):
    doctor, plan, _ = await _setup(db_session, seed)
    order, _ = await order_service.ensure_order(
        db_session,
        plan=plan,
        scheduled_at=datetime(2026, 11, 11, 20, 0, tzinfo=UTC),
        created_source="scheduler",
    )
    await order_service.mark_reminded(db_session, order=order, source="scheduler")

    await plan_service.cancel_plan(db_session, actor=doctor, plan_id=plan.plan_id)
    refreshed = await db_session.get(MedicationOrder, order.order_id)
    assert refreshed.status == MedicationOrderStatus.CANCELLED
    cancelled_events = (
        await db_session.execute(
            select(MedicationEvent).where(
                MedicationEvent.order_id == order.order_id,
                MedicationEvent.event_type == "cancelled",
            )
        )
    ).scalars().all()
    assert len(cancelled_events) == 1


async def test_startup_backfill_marks_missed_without_denied_event(db_session, seed):
    _, plan, _ = await _setup(db_session, seed)
    order, _ = await order_service.ensure_order(
        db_session,
        plan=plan,
        scheduled_at=datetime(2026, 11, 11, 12, 0, tzinfo=UTC),
        created_source="scheduler",
    )

    await order_service.mark_denied_or_timeout(
        db_session,
        order=order,
        kind="timeout",
        source="scheduler",
        startup_backfill=True,
    )
    assert order.status == MedicationOrderStatus.MISSED
    event_types = [
        event.event_type
        for event in (
            await db_session.execute(
                select(MedicationEvent).where(MedicationEvent.order_id == order.order_id)
            )
        ).scalars().all()
    ]
    assert "timeout" not in event_types
    assert "missed" in event_types

    workorders = await _missed_workorders(db_session, order.order_id)
    assert len(workorders) == 1
    assert "startup_backfill" in workorders[0].description


async def test_confirm_idempotent_and_source_conflict(db_session, seed):
    _, plan, _ = await _setup(db_session, seed)
    order, _ = await order_service.ensure_order(
        db_session, plan=plan, scheduled_at=SCHEDULED_AT, created_source="scheduler"
    )

    first = await order_service.mark_confirmed(
        db_session, order=order, source="voice"
    )
    assert first.status == MedicationOrderStatus.CONFIRMED
    again = await order_service.mark_confirmed(
        db_session, order=order, source="voice"
    )
    assert again.confirmed_at == first.confirmed_at

    with pytest.raises(order_service.MedicationOrderConflict):
        await order_service.mark_confirmed(db_session, order=order, source="text")


async def test_family_history_hides_skip_reason_and_workorder(db_session, seed):
    _, plan, _ = await _setup(db_session, seed)
    order, _ = await order_service.ensure_order(
        db_session, plan=plan, scheduled_at=SCHEDULED_AT, created_source="scheduler"
    )
    nurse = _identity(seed, "nurse_a")
    await order_service.mark_skipped(
        db_session, order=order, actor=nurse, skip_reason="长者拒绝服药"
    )

    family = _identity(seed, "family")
    response = await order_service.family_history(
        db_session,
        actor=family,
        elder_id=seed["elders"]["d1"].elder_id,
        plan_id=plan.plan_id,
    )
    assert response.total == 1
    item = response.items[0]
    assert item.status == MedicationOrderStatus.SKIPPED
    dumped = item.model_dump()
    for forbidden in ("skip_reason", "workorder_id", "internal_note", "actor_account_id"):
        assert forbidden not in dumped
    assert [event.event_type for event in item.events] == ["created", "skipped"]
