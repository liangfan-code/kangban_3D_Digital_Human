"""工单 HTTP 与员工端 WS 接口测试（Day8 Task 4 / Task 5）。

沿用 conftest 的 kangban_test 事务隔离：api 与 db_session 共用同一会话，
测试结束整体回滚。WS 仅验证首帧鉴权失败会立即关闭（不依赖数据库）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.db.session import get_session
from app.models import BuildingAssignment, DoctorElderAssignment, FamilyElderLink
from app.services import workorder as svc
from app.services.workorder_events import publish_workorder_event, workorder_event_hub
from tests.conftest import login


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _bind_doctor_and_nurse(db_session, seed) -> None:
    """医生负责 d1、护理负责 A 楼栋（d1 在 A 楼）。"""
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
    db_session,
    seed,
    *,
    category: str = "chest_pain",
    text: str = "我胸口疼，喘不上气",
    elder: str = "d1",
    snapshot: list[dict] | None = None,
):
    workorder, _ = await svc.create_sensitive_work_order(
        db_session,
        elder_id=seed["elders"][elder].elder_id,
        category=category,
        device_id=seed["devices"]["double"].device_id,
        trigger_text=text,
        context_snapshot=snapshot,
    )
    return workorder


# ---- 员工：已读 / 方案 / 计数 ----


async def test_staff_read_marks_own_disposition_only(api, db_session, seed):
    await _bind_doctor_and_nurse(db_session, seed)
    workorder = await _create(db_session, seed)
    doctor = await login(api, "doctor", seed["password"])
    nurse = await login(api, "nurse_a", seed["password"])
    workorder_id = workorder.workorder_id

    listed = await api.get("/api/v1/staff/workorders", headers=_auth(doctor))
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["role"] == "doctor"
    assert body["total"] == 1
    item = body["items"][0]
    assert item["id"] == str(workorder_id)
    assert item["my_status"] == "unread"
    assert item["pending"] is True
    assert {row["role"] for row in item["dispositions"]} == {"doctor", "nurse"}

    counts = await api.get("/api/v1/staff/workorders/counts", headers=_auth(doctor))
    assert counts.status_code == 200, counts.text
    assert counts.json() == {"role": "doctor", "unhandled": 1}

    read = await api.post(
        f"/api/v1/staff/workorders/{workorder_id}/read", headers=_auth(doctor)
    )
    assert read.status_code == 200, read.text
    assert read.json()["my_status"] == "read"
    assert read.json()["pending"] is True  # 已读但未处理，仍计入未处理数

    # 医生的已读不影响护理处理项
    nurse_list = await api.get("/api/v1/staff/workorders", headers=_auth(nurse))
    assert nurse_list.json()["items"][0]["my_status"] == "unread"

    detail = await api.get(
        f"/api/v1/staff/workorders/{workorder_id}", headers=_auth(doctor)
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["context_snapshot"] is None
    assert detail.json()["title"]


async def test_staff_submit_disposition_then_conflict(api, db_session, seed):
    await _bind_doctor_and_nurse(db_session, seed)
    workorder = await _create(db_session, seed)
    doctor = await login(api, "doctor", seed["password"])
    url = f"/api/v1/staff/workorders/{workorder.workorder_id}/disposition"

    submitted = await api.post(
        url, json={"plan_text": "建议吸氧并复测血压"}, headers=_auth(doctor)
    )
    assert submitted.status_code == 200, submitted.text
    item = submitted.json()
    assert item["status"] == "in_progress"
    assert item["my_status"] == "handled"
    assert item["pending"] is False
    doctor_row = next(r for r in item["dispositions"] if r["role"] == "doctor")
    assert doctor_row["plan_text"] == "建议吸氧并复测血压"
    assert doctor_row["handled_at"] is not None

    again = await api.post(url, json={"plan_text": "重复提交"}, headers=_auth(doctor))
    assert again.status_code == 409
    assert again.json()["error"]["message"]

    empty = await api.post(url, json={"plan_text": "  "}, headers=_auth(doctor))
    assert empty.status_code == 422


async def test_staff_detail_out_of_scope_is_404(api, db_session, seed):
    await _bind_doctor_and_nurse(db_session, seed)
    other = await _create(
        db_session, seed, elder="other", category="fall", text="在走廊摔倒了"
    )
    doctor = await login(api, "doctor", seed["password"])

    detail = await api.get(
        f"/api/v1/staff/workorders/{other.workorder_id}", headers=_auth(doctor)
    )
    assert detail.status_code == 404


# ---- 管理员关闭 ----


async def test_admin_close_requires_both_roles(api, db_session, seed):
    await _bind_doctor_and_nurse(db_session, seed)
    workorder = await _create(db_session, seed)
    doctor = await login(api, "doctor", seed["password"])
    nurse = await login(api, "nurse_a", seed["password"])
    admin = await login(api, "admin", seed["password"])
    workorder_id = workorder.workorder_id

    await api.post(
        f"/api/v1/staff/workorders/{workorder_id}/disposition",
        json={"plan_text": "已电话随访"},
        headers=_auth(doctor),
    )
    blocked = await api.post(
        f"/api/v1/admin/workorders/{workorder_id}/close",
        json={"note": "提前关闭"},
        headers=_auth(admin),
    )
    assert blocked.status_code == 409
    error = blocked.json()["error"]
    assert error["code"] == "WORKORDER_CONFLICT"
    assert error["unfinished_roles"] == ["nurse"]

    await api.post(
        f"/api/v1/staff/workorders/{workorder_id}/disposition",
        json={"plan_text": "已到床边查看"},
        headers=_auth(nurse),
    )
    closed = await api.post(
        f"/api/v1/admin/workorders/{workorder_id}/close",
        json={"note": "双方已处理"},
        headers=_auth(admin),
    )
    assert closed.status_code == 200, closed.text
    body = closed.json()
    assert body["status"] == "resolved"
    assert body["closed_note"] == "双方已处理"
    assert body["context_snapshot"] is None

    again = await api.post(
        f"/api/v1/admin/workorders/{workorder_id}/close", json={}, headers=_auth(admin)
    )
    assert again.status_code == 409
    assert again.json()["error"]["unfinished_roles"] == []


# ---- 家属只读与隐私边界 ----


async def test_family_scope_and_no_context_snapshot(api, db_session, seed):
    snapshot = [
        {"role": "user", "content": "我胸口疼", "at": datetime.now(UTC).isoformat()}
    ]
    workorder = await _create(db_session, seed, snapshot=snapshot)
    family = await login(api, "family", seed["password"])
    elder_id = seed["elders"]["d1"].elder_id
    headers = _auth(family)

    denied = await api.get(
        f"/api/v1/family/elders/{elder_id}/workorders", headers=headers
    )
    assert denied.status_code == 404
    denied_detail = await api.get(
        f"/api/v1/family/workorders/{workorder.workorder_id}", headers=headers
    )
    assert denied_detail.status_code == 404

    db_session.add(
        FamilyElderLink(
            account_id=seed["accounts"]["family"].account_id,
            elder_id=elder_id,
        )
    )
    await db_session.flush()

    listed = await api.get(
        f"/api/v1/family/elders/{elder_id}/workorders", headers=headers
    )
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["total"] == 1
    assert body["items"][0]["pending"] is False

    detail = await api.get(
        f"/api/v1/family/workorders/{workorder.workorder_id}", headers=headers
    )
    assert detail.status_code == 200, detail.text
    assert "context_snapshot" not in detail.json()
    assert {row["role"] for row in detail.json()["dispositions"]} == {"doctor", "nurse"}

    # 家属无任何写权限
    forbidden = await api.post(
        f"/api/v1/staff/workorders/{workorder.workorder_id}/read", headers=headers
    )
    assert forbidden.status_code == 403


# ---- 搜索 / 排序 / 分页 ----


async def test_staff_search_sort_and_pagination(api, db_session, seed):
    await _bind_doctor_and_nurse(db_session, seed)
    base = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    first = await _create(db_session, seed, text="我胸口疼，喘不上气")
    second = await _create(db_session, seed, category="fall", text="在走廊摔倒了")
    first.created_at = base
    second.created_at = base + timedelta(minutes=5)
    await db_session.flush()

    doctor = await login(api, "doctor", seed["password"])
    headers = _auth(doctor)

    asc = await api.get(
        "/api/v1/staff/workorders?sort=created_at&order=asc", headers=headers
    )
    assert [row["id"] for row in asc.json()["items"]] == [
        str(first.workorder_id),
        str(second.workorder_id),
    ]

    desc = await api.get(
        "/api/v1/staff/workorders?sort=created_at&order=desc", headers=headers
    )
    assert [row["id"] for row in desc.json()["items"]] == [
        str(second.workorder_id),
        str(first.workorder_id),
    ]

    hit = await api.get("/api/v1/staff/workorders?q=胸口", headers=headers)
    assert hit.json()["total"] == 1
    assert hit.json()["items"][0]["id"] == str(first.workorder_id)

    # 方案全文也可搜索
    await api.post(
        f"/api/v1/staff/workorders/{first.workorder_id}/disposition",
        json={"plan_text": "建议立即吸氧"},
        headers=headers,
    )
    plan_hit = await api.get("/api/v1/staff/workorders?q=吸氧", headers=headers)
    assert plan_hit.json()["total"] == 1
    assert plan_hit.json()["items"][0]["id"] == str(first.workorder_id)

    paged = await api.get("/api/v1/staff/workorders?page=1&page_size=1", headers=headers)
    assert paged.json()["total"] == 2
    assert len(paged.json()["items"]) == 1

    # 非法排序字段回落默认，不报错
    fallback = await api.get(
        "/api/v1/staff/workorders?sort=evil&order=up", headers=headers
    )
    assert fallback.status_code == 200
    assert fallback.json()["total"] == 2

    # 管理员列表不受员工可见范围限制
    admin = await login(api, "admin", seed["password"])
    admin_list = await api.get("/api/v1/admin/workorders", headers=_auth(admin))
    assert admin_list.status_code == 200
    assert admin_list.json()["total"] == 2


# ---- 订阅中心：按可见范围分发 ----


class _FakeConnection:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.messages.append(message)


async def test_event_hub_routes_by_visibility(db_session, seed):
    await _bind_doctor_and_nurse(db_session, seed)
    workorder = await _create(db_session, seed)
    accounts = seed["accounts"]
    doctor_conn = _FakeConnection()
    nurse_conn = _FakeConnection()
    other_nurse_conn = _FakeConnection()  # B 楼护理：不在可见范围
    admin_conn = _FakeConnection()

    workorder_event_hub.register(accounts["doctor"].account_id, doctor_conn)
    workorder_event_hub.register(accounts["nurse_a"].account_id, nurse_conn)
    workorder_event_hub.register(accounts["nurse_b"].account_id, other_nurse_conn)
    workorder_event_hub.register(accounts["admin"].account_id, admin_conn)
    try:
        await publish_workorder_event("ticket.created", workorder, session=db_session)
    finally:
        workorder_event_hub.clear()

    assert [m["type"] for m in doctor_conn.messages] == ["ticket.created", "ticket.counts"]
    created = doctor_conn.messages[0]["data"]
    assert created["workorder_id"] == str(workorder.workorder_id)
    assert created["elder_id"] == str(workorder.elder_id)
    assert created["category"] == "chest_pain"
    assert created["status"] == "open"
    assert created["priority"] == "urgent"
    assert created["unhandled"] == 1
    assert doctor_conn.messages[1]["data"] == {"role": "doctor", "unhandled": 1}

    assert [m["type"] for m in nurse_conn.messages] == ["ticket.created", "ticket.counts"]
    assert other_nurse_conn.messages == []
    # 管理员实时知情，但没有处理项，不发角标计数
    assert [m["type"] for m in admin_conn.messages] == ["ticket.created"]


# ---- WS 首帧鉴权 ----


def _ws_client(app) -> TestClient:
    """WS 鉴权失败路径不触库：覆盖会话依赖避免创建真实数据库连接。"""

    async def _no_session():
        yield None

    app.dependency_overrides[get_session] = _no_session
    return TestClient(app)


def test_staff_ws_closes_on_invalid_token(app):
    client = _ws_client(app)
    try:
        with client.websocket_connect("/api/v1/staff/ws") as ws:
            ws.send_json({"type": "auth", "token": "not-a-jwt"})
            with pytest.raises(WebSocketDisconnect) as excinfo:
                ws.receive_json()
        assert excinfo.value.code == 4401
    finally:
        app.dependency_overrides.pop(get_session, None)


def test_staff_ws_closes_without_auth_frame(app):
    client = _ws_client(app)
    try:
        with client.websocket_connect("/api/v1/staff/ws") as ws:
            ws.send_json({"type": "hello"})
            with pytest.raises(WebSocketDisconnect) as excinfo:
                ws.receive_json()
        assert excinfo.value.code == 4401
    finally:
        app.dependency_overrides.pop(get_session, None)
