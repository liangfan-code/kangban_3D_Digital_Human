"""Day7 段3：用药确认图（DB 权威、真循环、可恢复、降级落库）与路由前置分支。"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.graphs import medication as medication_graph
from app.graphs.medication import start_confirmation, submit_reply, thread_id_for
from app.models import MedicationEvent, MedicationOrder, MedicationPlan, Workorder
from app.models.enums import MedicationOrderStatus
from app.router.schemas import Intent, RouteLayer
from app.router.service import route_text
from app.services import medication_order as order_service
from app.services.medication_plan import format_plan_reply, today_plans

SCHEDULED_AT = datetime(2026, 11, 11, 0, 0, tzinfo=UTC)


def _session_factory(db_session):
    @asynccontextmanager
    async def factory():
        yield db_session

    return factory


def _local_today() -> date:
    return datetime.now(ZoneInfo(get_settings().app_timezone)).date()


async def _make_plan(
    db_session, elder_id, *, drug_name: str = "降压药", time: str = "00:00"
) -> MedicationPlan:
    plan = MedicationPlan(
        elder_id=elder_id,
        drug_name=drug_name,
        dose="1 片",
        schedule={"times": [time], "weekdays": []},
        start_date=_local_today(),
        status="active",
    )
    db_session.add(plan)
    await db_session.flush()
    return plan


async def _make_order(db_session, plan: MedicationPlan) -> MedicationOrder:
    order, _ = await order_service.ensure_order(
        db_session, plan=plan, scheduled_at=SCHEDULED_AT, created_source="text"
    )
    return order


async def _event_types(db_session, order_id) -> list[str]:
    rows = (
        await db_session.execute(
            select(MedicationEvent.event_type).where(MedicationEvent.order_id == order_id)
        )
    ).scalars().all()
    return list(rows)


async def _workorder_count(db_session, order_id) -> int:
    return (
        await db_session.execute(
            select(func.count())
            .select_from(Workorder)
            .where(Workorder.medication_order_id == order_id)
        )
    ).scalar_one()


async def test_two_denials_escalate_once(db_session, seed, tmp_path):
    """2026-11-13 用户拍板：重问一次，二次拒绝即建漏服工单（原为 3 次）。"""
    plan = await _make_plan(db_session, seed["elders"]["d1"].elder_id)
    order = await _make_order(db_session, plan)
    factory = _session_factory(db_session)
    checkpoint = tmp_path / "medication.sqlite3"

    started = await start_confirmation(order.order_id, session_factory=factory,
                                       checkpoint_path=checkpoint)
    assert started.attempt == 1
    assert order.status == MedicationOrderStatus.CALLING
    assert "降压药" in started.reply

    first = await submit_reply(order.order_id, "还没吃", session_factory=factory,
                               checkpoint_path=checkpoint)
    # 未耗尽：同一轮里已重新提醒，图停在等待确认
    assert first.status == "waiting_confirm"
    assert first.attempt == 2
    assert "降压药" in first.reply
    assert order.status == MedicationOrderStatus.CALLING
    assert order.attempt_count == 2

    final = await submit_reply(order.order_id, "还没吃", session_factory=factory,
                               checkpoint_path=checkpoint)

    assert final.status == "escalated"
    assert order.status == MedicationOrderStatus.MISSED
    assert order.attempt_count == 2
    types = await _event_types(db_session, order.order_id)
    assert types.count("reminded") == 2
    assert types.count("denied") == 2
    assert types.count("missed") == 1
    assert await _workorder_count(db_session, order.order_id) == 1

    # 终态重复回复：不重复建单、不重复写事件
    again = await submit_reply(order.order_id, "还没吃", session_factory=factory,
                               checkpoint_path=checkpoint)
    assert again.status == "done"
    assert await _workorder_count(db_session, order.order_id) == 1
    assert (await _event_types(db_session, order.order_id)).count("missed") == 1


async def test_confirmed_reply_writes_db_without_workorder(db_session, seed, tmp_path):
    plan = await _make_plan(db_session, seed["elders"]["d1"].elder_id)
    order = await _make_order(db_session, plan)
    factory = _session_factory(db_session)
    checkpoint = tmp_path / "medication.sqlite3"

    await start_confirmation(order.order_id, session_factory=factory, checkpoint_path=checkpoint)
    result = await submit_reply(order.order_id, "我吃过了", session_factory=factory,
                                checkpoint_path=checkpoint)

    assert result.status == "confirmed"
    assert order.status == MedicationOrderStatus.CONFIRMED
    assert order.confirmed_source == "text"
    assert (await _event_types(db_session, order.order_id)).count("confirmed") == 1
    assert await _workorder_count(db_session, order.order_id) == 0


async def test_reply_without_start_confirms_and_records(db_session, seed, tmp_path):
    plan = await _make_plan(db_session, seed["elders"]["d1"].elder_id)
    order = await _make_order(db_session, plan)

    result = await submit_reply(
        order.order_id,
        "已经吃了",
        session_factory=_session_factory(db_session),
        checkpoint_path=tmp_path / "x.sqlite3",
    )

    assert result.status == "confirmed"
    assert order.status == MedicationOrderStatus.CONFIRMED
    assert (await _event_types(db_session, order.order_id)).count("confirmed") == 1


# ---------------- 拒药判定（「我不吃」绝不能判成已吃） ----------------


def test_detect_medication_reply_refusals_are_denied():
    from app.router.signals import detect_medication_reply

    # 截图事故原句：「难吃了」曾被确认正则误判为已吃
    assert detect_medication_reply("这个药太难吃了，我不吃。") == "denied"
    assert detect_medication_reply("我不想吃药") == "denied"
    assert detect_medication_reply("我吃不下，咽不下") == "denied"
    assert detect_medication_reply("别让我吃这个药") == "denied"
    # 不回归：确认/否认/延后原语义保持
    assert detect_medication_reply("我吃过了") == "confirmed"
    assert detect_medication_reply("已经吃了药") == "confirmed"
    assert detect_medication_reply("还没吃") == "denied"
    assert detect_medication_reply("等会儿再吃") == "deferred"


async def test_refusal_reasks_once_then_missed_workorder(db_session, seed, tmp_path):
    """用户拍板：拒药重问一次，二次拒绝即建漏服工单。"""
    plan = await _make_plan(db_session, seed["elders"]["d1"].elder_id)
    order = await _make_order(db_session, plan)
    factory = _session_factory(db_session)
    checkpoint = tmp_path / "refusal.sqlite3"

    await start_confirmation(order.order_id, session_factory=factory, checkpoint_path=checkpoint)
    first = await submit_reply(order.order_id, "这个药太难吃了，我不吃。",
                               session_factory=factory, checkpoint_path=checkpoint)
    # 第一次拒绝：绝不判成已吃，重问一次
    assert first.status == "waiting_confirm"
    assert order.status == MedicationOrderStatus.CALLING

    final = await submit_reply(order.order_id, "我就是不想吃。",
                               session_factory=factory, checkpoint_path=checkpoint)
    assert final.status == "escalated"
    assert order.status == MedicationOrderStatus.MISSED
    types = await _event_types(db_session, order.order_id)
    assert types.count("denied") == 2
    assert types.count("missed") == 1
    assert await _workorder_count(db_session, order.order_id) == 1


async def test_state_recovers_after_new_graph_instance(db_session, seed, tmp_path):
    plan = await _make_plan(db_session, seed["elders"]["d1"].elder_id)
    order = await _make_order(db_session, plan)
    factory = _session_factory(db_session)
    checkpoint = tmp_path / "recover.sqlite3"

    await start_confirmation(order.order_id, session_factory=factory, checkpoint_path=checkpoint)

    # 直接读 checkpoint：attempt/status 已持久化到 thread_id=medication:{order_id}
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async with AsyncSqliteSaver.from_conn_string(str(checkpoint)) as saver:
        tup = await saver.aget_tuple(
            {"configurable": {"thread_id": thread_id_for(order.order_id)}}
        )
        assert tup is not None
        values = tup.checkpoint["channel_values"]
        assert values["attempt"] == 1
        assert values["status"] == "waiting_confirm"

    # 新建图实例（每次调用都会重建）继续推进，attempt 在 DB 与 checkpoint 之间保持一致
    result = await submit_reply(order.order_id, "还没吃", session_factory=factory,
                                checkpoint_path=checkpoint)
    assert result.attempt == 2
    assert order.attempt_count == 2


async def test_cross_elder_isolation(db_session, seed, tmp_path):
    plan_a = await _make_plan(db_session, seed["elders"]["d1"].elder_id)
    plan_b = await _make_plan(db_session, seed["elders"]["d2"].elder_id, drug_name="降糖药")
    order_a = await _make_order(db_session, plan_a)
    order_b = await _make_order(db_session, plan_b)
    factory = _session_factory(db_session)
    checkpoint = tmp_path / "isolation.sqlite3"

    await start_confirmation(order_a.order_id, session_factory=factory, checkpoint_path=checkpoint)
    await start_confirmation(order_b.order_id, session_factory=factory, checkpoint_path=checkpoint)
    await submit_reply(order_a.order_id, "吃过了", session_factory=factory,
                       checkpoint_path=checkpoint)

    assert order_a.status == MedicationOrderStatus.CONFIRMED
    assert order_b.status == MedicationOrderStatus.CALLING
    assert order_b.attempt_count == 1


async def test_no_plan_never_fabricates(db_session, seed):
    slots = await today_plans(db_session, elder_id=seed["elders"]["other"].elder_id)
    assert slots == []
    reply = format_plan_reply(slots)
    assert "还没有登记用药计划" in reply


async def test_today_plans_returns_stored_drug(db_session, seed):
    await _make_plan(db_session, seed["elders"]["d1"].elder_id)
    slots = await today_plans(db_session, elder_id=seed["elders"]["d1"].elder_id)
    assert len(slots) == 1
    assert "降压药" in format_plan_reply(slots)


async def test_degraded_mode_still_writes_db(db_session, seed, tmp_path, monkeypatch):
    plan = await _make_plan(db_session, seed["elders"]["d1"].elder_id)
    order = await _make_order(db_session, plan)
    factory = _session_factory(db_session)
    checkpoint = tmp_path / "degraded.sqlite3"
    monkeypatch.setattr(medication_graph, "LANGGRAPH_AVAILABLE", False)

    started = await start_confirmation(order.order_id, session_factory=factory,
                                       checkpoint_path=checkpoint)
    assert started.degraded == "medication_graph_unavailable"
    assert order.status == MedicationOrderStatus.CALLING
    assert order.attempt_count == 1

    result = await submit_reply(order.order_id, "吃过了", session_factory=factory,
                                checkpoint_path=checkpoint)
    assert result.degraded == "medication_graph_unavailable"
    assert order.status == MedicationOrderStatus.CONFIRMED
    assert (await _event_types(db_session, order.order_id)).count("confirmed") == 1


def test_route_confirmation_branch_does_not_swallow_refusal():
    confirmed = route_text("我吃过药了")
    assert confirmed.layer is RouteLayer.RULE
    assert confirmed.rule_id == "MED-01"
    assert confirmed.tool == "medication_confirm"
    assert confirmed.tool_args == {"reply": "我吃过药了"}

    deferred = route_text("等会儿吃")
    assert deferred.rule_id == "MED-01"

    denied = route_text("我不吃药")
    assert denied.layer is RouteLayer.AGENT
    assert denied.intent is Intent.MEDICATION_QUERY

    query = route_text("我该吃什么药")
    assert query.tool == "medication_query"
    assert query.rule_id != "MED-01"


def test_route_question_is_not_confirmation():
    assert route_text("我吃过药了吗").rule_id != "MED-01"
    assert route_text("药吃了没").rule_id != "MED-01"


async def test_unknown_order_id_is_domain_error(db_session):
    with pytest.raises(medication_graph.MedicationGraphError):
        await submit_reply(uuid.uuid4(), "吃过了", session_factory=_session_factory(db_session))
