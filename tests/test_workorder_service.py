"""workorder 服务测试：建单去重、三态处理项、双角色关闭、搜索排序、上下文快照。

沿用 conftest 的 kangban_test 事务隔离方式（db_session + seed），测试结束整体回滚。
"""

import pytest
from sqlalchemy import select

from app.models import (
    BuildingAssignment,
    DoctorElderAssignment,
    FamilyElderLink,
    WorkorderDisposition,
    WorkorderEvent,
)
from app.models.enums import WorkorderDispositionStatus, WorkorderPriority, WorkorderStatus
from app.services import workorder as svc


async def _bind_doctor_and_nurse(db_session, seed):
    """医生负责 d1、护工负责 A 楼栋（d1/d2/single 均在 A 楼）。"""
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
        ]
    )
    await db_session.flush()


async def _create(
    db_session, seed, *, category="chest_pain", text="我胸口疼，喘不上气", elder="d1"
):
    return await svc.create_sensitive_work_order(
        db_session,
        elder_id=seed["elders"][elder].elder_id,
        category=category,
        device_id=seed["devices"]["double"].device_id,
        trigger_text=text,
    )


async def _dispositions(db_session, workorder_id) -> dict:
    rows = (
        await db_session.execute(
            select(WorkorderDisposition).where(
                WorkorderDisposition.workorder_id == workorder_id
            )
        )
    ).scalars().all()
    return {row.role: row for row in rows}


async def _events(db_session, workorder_id) -> list[WorkorderEvent]:
    return list(
        (
            await db_session.execute(
                select(WorkorderEvent)
                .where(WorkorderEvent.workorder_id == workorder_id)
                .order_by(WorkorderEvent.created_at)
            )
        )
        .scalars()
        .all()
    )


# ---- 建单与去重 ----


async def test_create_generates_two_unread_dispositions(db_session, seed):
    workorder, created = await _create(db_session, seed)

    assert created is True
    assert workorder.type == "sensitive"
    assert workorder.source == "sensitive"
    assert workorder.priority == WorkorderPriority.URGENT
    assert workorder.status == WorkorderStatus.OPEN
    assert workorder.occurrence_count == 1
    assert workorder.sensitive_category == "chest_pain"

    dispositions = await _dispositions(db_session, workorder.workorder_id)
    assert set(dispositions) == {"doctor", "nurse"}
    assert all(
        row.status == WorkorderDispositionStatus.UNREAD for row in dispositions.values()
    )

    events = await _events(db_session, workorder.workorder_id)
    assert [event.event_type for event in events] == ["created"]


async def test_duplicate_trigger_accumulates_occurrence(db_session, seed):
    first, created = await _create(db_session, seed)
    second, created_again = await _create(
        db_session, seed, text="还是胸口疼，更闷了"
    )

    assert created is True
    assert created_again is False
    assert second.workorder_id == first.workorder_id
    assert second.occurrence_count == 2
    assert second.description == "还是胸口疼，更闷了"

    events = await _events(db_session, first.workorder_id)
    assert [event.event_type for event in events] == ["created", "occurrence"]
    assert events[-1].payload["occurrence_count"] == 2


# ---- 三态与状态机 ----


async def test_doctor_read_does_not_affect_nurse(db_session, seed):
    workorder, _ = await _create(db_session, seed)
    doctor = seed["accounts"]["doctor"]

    disposition = await svc.mark_read(
        db_session,
        workorder_id=workorder.workorder_id,
        role="doctor",
        account_id=doctor.account_id,
    )
    assert disposition.status == WorkorderDispositionStatus.READ
    assert disposition.read_at is not None
    assert disposition.account_id == doctor.account_id
    assert workorder.status == WorkorderStatus.ACKNOWLEDGED

    dispositions = await _dispositions(db_session, workorder.workorder_id)
    assert dispositions["nurse"].status == WorkorderDispositionStatus.UNREAD

    # 幂等：重复已读不报错、不重复写事件
    await svc.mark_read(
        db_session,
        workorder_id=workorder.workorder_id,
        role="doctor",
        account_id=doctor.account_id,
    )
    events = await _events(db_session, workorder.workorder_id)
    assert [event.event_type for event in events] == ["created", "read"]


async def test_duplicate_disposition_raises_conflict(db_session, seed):
    workorder, _ = await _create(db_session, seed)
    doctor = seed["accounts"]["doctor"]

    await svc.submit_disposition(
        db_session,
        workorder_id=workorder.workorder_id,
        role="doctor",
        account_id=doctor.account_id,
        plan_text="建议吸氧并复测血压",
    )
    assert workorder.status == WorkorderStatus.IN_PROGRESS

    with pytest.raises(svc.WorkOrderConflict):
        await svc.submit_disposition(
            db_session,
            workorder_id=workorder.workorder_id,
            role="doctor",
            account_id=doctor.account_id,
            plan_text="再次提交",
        )


async def test_close_requires_both_roles(db_session, seed):
    workorder, _ = await _create(db_session, seed)
    doctor = seed["accounts"]["doctor"]
    admin = seed["accounts"]["admin"]

    await svc.submit_disposition(
        db_session,
        workorder_id=workorder.workorder_id,
        role="doctor",
        account_id=doctor.account_id,
        plan_text="已电话随访",
    )
    with pytest.raises(svc.WorkOrderConflict) as excinfo:
        await svc.close_work_order(
            db_session,
            workorder_id=workorder.workorder_id,
            admin_account_id=admin.account_id,
        )
    assert "nurse" in str(excinfo.value)


async def test_close_success_after_both_roles_then_locked(db_session, seed):
    workorder, _ = await _create(db_session, seed)
    doctor = seed["accounts"]["doctor"]
    nurse = seed["accounts"]["nurse_a"]
    admin = seed["accounts"]["admin"]

    await svc.submit_disposition(
        db_session,
        workorder_id=workorder.workorder_id,
        role="doctor",
        account_id=doctor.account_id,
        plan_text="建议吸氧",
    )
    await svc.submit_disposition(
        db_session,
        workorder_id=workorder.workorder_id,
        role="nurse",
        account_id=nurse.account_id,
        plan_text="已到床边查看",
    )

    closed = await svc.close_work_order(
        db_session,
        workorder_id=workorder.workorder_id,
        admin_account_id=admin.account_id,
        note="双方已处理",
    )
    assert closed.status == WorkorderStatus.RESOLVED
    assert closed.resolved_at is not None
    assert closed.closed_by == admin.account_id
    assert closed.closed_note == "双方已处理"

    events = await _events(db_session, workorder.workorder_id)
    assert [event.event_type for event in events] == [
        "created",
        "disposition",
        "disposition",
        "closed",
    ]

    # 关闭后不可再提交方案
    with pytest.raises(svc.WorkOrderConflict):
        await svc.submit_disposition(
            db_session,
            workorder_id=workorder.workorder_id,
            role="nurse",
            account_id=nurse.account_id,
            plan_text="补充说明",
        )


async def test_wrong_role_is_forbidden(db_session, seed):
    workorder, _ = await _create(db_session, seed)
    doctor = seed["accounts"]["doctor"]

    # 用医生账号操作护理处理项 → 越权
    with pytest.raises(svc.WorkOrderForbidden):
        await svc.mark_read(
            db_session,
            workorder_id=workorder.workorder_id,
            role="nurse",
            account_id=doctor.account_id,
        )


# ---- 可见范围、搜索与排序 ----


async def test_list_visibility_and_q_search(db_session, seed):
    await _bind_doctor_and_nurse(db_session, seed)
    doctor = seed["accounts"]["doctor"]
    admin = seed["accounts"]["admin"]

    d1_order, _ = await _create(db_session, seed)
    other_order, _ = await _create(
        db_session,
        seed,
        elder="other",
        category="fall",
        text="在走廊摔倒了",
    )

    rows, total = await svc.list_work_orders(
        db_session, role="doctor", account_id=doctor.account_id
    )
    assert total == 1
    assert [row.workorder_id for row in rows] == [d1_order.workorder_id]

    all_rows, all_total = await svc.list_work_orders(
        db_session, role="admin", account_id=admin.account_id
    )
    assert all_total == 2

    # 触发原句命中
    hit, hit_total = await svc.list_work_orders(
        db_session, role="doctor", account_id=doctor.account_id, q="胸口"
    )
    assert hit_total == 1
    assert hit[0].workorder_id == d1_order.workorder_id

    # 处理方案命中
    await svc.submit_disposition(
        db_session,
        workorder_id=d1_order.workorder_id,
        role="doctor",
        account_id=doctor.account_id,
        plan_text="建议立即吸氧",
    )
    plan_hit, plan_total = await svc.list_work_orders(
        db_session, role="admin", account_id=admin.account_id, q="吸氧"
    )
    assert plan_total == 1
    assert plan_hit[0].workorder_id == d1_order.workorder_id

    # % 被转义为字面量，不应匹配全部
    escaped, escaped_total = await svc.list_work_orders(
        db_session, role="admin", account_id=admin.account_id, q="%"
    )
    assert escaped_total == 0
    assert escaped == []

    # 非法排序字段/方向回落默认，不报错
    fallback, _ = await svc.list_work_orders(
        db_session,
        role="admin",
        account_id=admin.account_id,
        sort="evil",
        order="up",
    )
    assert len(fallback) == 2

    # other 楼栋长者的工单不因医生未绑定而泄露给护工可见范围外的账号
    assert other_order.elder_id == seed["elders"]["other"].elder_id


async def test_list_sort_by_priority(db_session, seed):
    await _bind_doctor_and_nurse(db_session, seed)
    doctor = seed["accounts"]["doctor"]

    urgent, _ = await _create(db_session, seed)
    low, _ = await _create(db_session, seed, category="fall", text="轻微擦伤")
    low.priority = WorkorderPriority.LOW
    await db_session.flush()

    rows, total = await svc.list_work_orders(
        db_session,
        role="doctor",
        account_id=doctor.account_id,
        sort="priority",
        order="desc",
    )
    assert total == 2
    assert [row.workorder_id for row in rows] == [urgent.workorder_id, low.workorder_id]

    rows_asc, _ = await svc.list_work_orders(
        db_session,
        role="doctor",
        account_id=doctor.account_id,
        sort="priority",
        order="asc",
    )
    assert [row.workorder_id for row in rows_asc] == [low.workorder_id, urgent.workorder_id]


async def test_family_requires_valid_link(db_session, seed):
    workorder, _ = await _create(db_session, seed)
    family = seed["accounts"]["family"]

    with pytest.raises(svc.WorkOrderNotFound):
        await svc.list_work_orders(
            db_session,
            role="family",
            account_id=family.account_id,
            elder_id=seed["elders"]["d1"].elder_id,
        )

    db_session.add(
        FamilyElderLink(
            account_id=family.account_id,
            elder_id=seed["elders"]["d1"].elder_id,
        )
    )
    await db_session.flush()

    rows, total = await svc.list_work_orders(
        db_session,
        role="family",
        account_id=family.account_id,
        elder_id=seed["elders"]["d1"].elder_id,
    )
    assert total == 1
    assert rows[0].workorder_id == workorder.workorder_id


async def test_list_item_exposes_type_and_order_kind(
    api, db_session, seed, auth_headers_factory
):
    """段 1 契约扩展：列表返回 type/title/order_kind；1.5 起 is_read 按账号回执取值。"""
    await _bind_doctor_and_nurse(db_session, seed)
    await _create(db_session, seed)
    headers = await auth_headers_factory("doctor")

    resp = await api.get("/api/v1/staff/workorders", headers=headers)
    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert item["type"] == "sensitive"
    assert item["title"]
    assert item["order_kind"] == "one_off"
    # staff 的 is_read 有语义：无回执即 False（不再用占位 None）
    assert item["is_read"] is False
    assert item["read_at"] is None


async def test_count_open_by_role(db_session, seed):
    await _bind_doctor_and_nurse(db_session, seed)
    doctor = seed["accounts"]["doctor"]
    nurse = seed["accounts"]["nurse_a"]

    workorder, _ = await _create(db_session, seed)
    assert (
        await svc.count_open_by_role(
            db_session, role="doctor", account_id=doctor.account_id
        )
        == 1
    )

    await svc.submit_disposition(
        db_session,
        workorder_id=workorder.workorder_id,
        role="doctor",
        account_id=doctor.account_id,
        plan_text="已处理",
    )
    assert (
        await svc.count_open_by_role(
            db_session, role="doctor", account_id=doctor.account_id
        )
        == 0
    )
    assert (
        await svc.count_open_by_role(
            db_session, role="nurse", account_id=nurse.account_id
        )
        == 1
    )


# ---- 上下文快照 ----


def test_build_context_snapshot_limits_to_five_turns():
    messages = []
    for index in range(6):
        messages.append({"role": "user", "content": f"u{index}", "created_at": None})
        messages.append({"role": "assistant", "content": f"a{index}", "created_at": None})

    snapshot = svc.build_context_snapshot(messages, "我胸口疼", "我帮您叫医生")
    assert len(snapshot) == 12  # 最近 10 条历史 + 触发句 + 首条回复
    assert [item["content"] for item in snapshot[:2]] == ["u1", "a1"]
    assert snapshot[-2]["role"] == "user"
    assert snapshot[-2]["content"] == "我胸口疼"
    assert snapshot[-1]["role"] == "assistant"
    assert snapshot[-1]["content"] == "我帮您叫医生"


def test_build_context_snapshot_short_history():
    messages = [{"role": "user", "content": "你好", "created_at": None}]
    snapshot = svc.build_context_snapshot(messages, "我不舒服", None)
    assert [item["role"] for item in snapshot] == ["user", "user"]
    assert [item["content"] for item in snapshot] == ["你好", "我不舒服"]
    assert snapshot[0]["at"] is not None  # 缺省时间戳显式落 ISO 字符串
