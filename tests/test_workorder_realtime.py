"""工单四端实时通道测试（Day7 段1.5）。

覆盖：WS 首帧鉴权（四端角色 / device token / 停用账号）、
广播分组（staff 收 counts、admin/family 只收刷新信号）、
家属按绑定收件、hub 连接上限挤出。
沿用 conftest 的 kangban_test 事务隔离；完整 HTTP WS 握手失败路径
在 test_workorder_api.py 已覆盖，此处用鉴权函数直测避免跨事件循环用会话。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

from starlette.websockets import WebSocketDisconnect

from app.api.routes.workorders import _authenticate_ws
from app.auth.jwt import (
    create_device_access_token,
    create_user_access_token,
)
from app.services import workorder as svc
from app.services.workorder_events import (
    WS_CONNECTIONS_PER_ACCOUNT,
    _resolve_recipients,
    workorder_event_hub,
)
from tests.conftest import login


async def _bind_staff_and_family(db_session, seed, *, link_valid_until=None) -> None:
    """医生负责 d1、护理负责 A 楼栋（d1 在 A 楼）、家属绑定 d1。"""
    from app.models import BuildingAssignment, DoctorElderAssignment, FamilyElderLink

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
                status="active",
                valid_until=link_valid_until,
            ),
        ]
    )
    await db_session.flush()


async def _create(db_session, seed, *, category: str = "chest_pain", elder: str = "d1"):
    workorder, _ = await svc.create_sensitive_work_order(
        db_session,
        elder_id=seed["elders"][elder].elder_id,
        category=category,
        device_id=seed["devices"]["double"].device_id,
        trigger_text="我胸口疼，喘不上气",
    )
    return workorder


class _FakeConnection:
    def __init__(self, name: str = "") -> None:
        self.name = name
        self.messages: list[dict] = []
        self.closed_codes: list[int] = []

    async def send_json(self, message: dict) -> None:
        self.messages.append(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_codes.append(code)


class _FakeWS:
    """替身 WebSocket：供 _authenticate_ws 读取首帧与捕获关闭码。"""

    def __init__(self, frames: list[str]) -> None:
        self._frames = list(frames)
        self.closed: tuple[int, str] | None = None

    async def receive_text(self) -> str:
        if not self._frames:
            raise WebSocketDisconnect(code=1000)
        return self._frames.pop(0)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


# ---- 广播分组：staff 收 counts，admin/family 只收刷新信号 ----


async def test_broadcast_groups_staff_vs_admin_family(db_session, seed):
    from app.services.workorder_events import publish_workorder_event

    await _bind_staff_and_family(db_session, seed)
    workorder = await _create(db_session, seed)
    accounts = seed["accounts"]

    doctor_conn = _FakeConnection("doctor")
    admin_conn = _FakeConnection("admin")
    family_conn = _FakeConnection("family")
    workorder_event_hub.register(accounts["doctor"].account_id, doctor_conn)
    workorder_event_hub.register(accounts["admin"].account_id, admin_conn)
    workorder_event_hub.register(accounts["family"].account_id, family_conn)
    try:
        await publish_workorder_event("ticket.created", workorder, session=db_session)
    finally:
        workorder_event_hub.clear()

    # staff：主事件 + ticket.counts（有处理项，参与计数）
    assert [m["type"] for m in doctor_conn.messages] == ["ticket.created", "ticket.counts"]
    created = doctor_conn.messages[0]["data"]
    assert created["type"] == "sensitive"
    assert created["category"] == "chest_pain"
    assert created["unhandled"] == 1

    # admin：只收刷新信号，无 counts、unhandled 恒 0
    assert [m["type"] for m in admin_conn.messages] == ["ticket.created"]
    assert admin_conn.messages[0]["data"]["unhandled"] == 0

    # family（有效绑定）：只收刷新信号，无 counts
    assert [m["type"] for m in family_conn.messages] == ["ticket.created"]
    assert family_conn.messages[0]["data"]["unhandled"] == 0
    assert family_conn.messages[0]["data"]["workorder_id"] == str(workorder.workorder_id)


async def test_family_without_active_link_not_notified(db_session, seed):
    """未绑定 / 绑定过期的家属不收该长者的事件。"""
    from app.services.workorder_events import publish_workorder_event

    await _bind_staff_and_family(
        db_session, seed, link_valid_until=datetime.now(UTC) - timedelta(days=1)
    )
    workorder = await _create(db_session, seed, elder="d2")  # 另一位长者

    family_conn = _FakeConnection("family")
    workorder_event_hub.register(seed["accounts"]["family"].account_id, family_conn)
    try:
        await publish_workorder_event("ticket.created", workorder, session=db_session)
    finally:
        workorder_event_hub.clear()

    assert family_conn.messages == []


async def test_resolve_recipients_includes_family(db_session, seed):
    await _bind_staff_and_family(db_session, seed)
    elder_id = seed["elders"]["d1"].elder_id
    recipients = await _resolve_recipients(db_session, elder_id)

    accounts = seed["accounts"]
    assert recipients[accounts["doctor"].account_id] == "doctor"
    assert recipients[accounts["nurse_a"].account_id] == "nurse"
    assert recipients[accounts["admin"].account_id] == "admin"
    assert recipients[accounts["family"].account_id] == "family"


# ---- hub：单账号连接上限（超出关闭最旧连接） ----


def test_hub_evicts_oldest_connection_over_limit():
    account_id = uuid.uuid4()
    conns = [_FakeConnection(f"c{i}") for i in range(WS_CONNECTIONS_PER_ACCOUNT + 1)]

    evicted_all: list = []
    try:
        for conn in conns:
            evicted_all.extend(workorder_event_hub.register(account_id, conn))

        # 前 3 个都保留，注册第 4 个时挤出最旧的 c0
        assert evicted_all == [conns[0]]
        remaining = workorder_event_hub.connections_for(account_id)
        assert [c.name for c in remaining] == ["c1", "c2", "c3"]

        # 注销中间连接后顺序保持
        workorder_event_hub.unregister(account_id, conns[2])
        remaining = workorder_event_hub.connections_for(account_id)
        assert [c.name for c in remaining] == ["c1", "c3"]
    finally:
        workorder_event_hub.clear()


# ---- WS 首帧鉴权：四端接受与拒绝 ----


async def test_authenticate_ws_accepts_four_roles(db_session, api, seed):
    """doctor/nurse/admin/family 首帧 auth 均通过，返回身份与 token 过期时间。"""
    cases = [
        ("doctor", "doctor"),
        ("nurse_a", "nurse"),
        ("admin", "admin"),
        ("family", "family"),
    ]
    for username, role in cases:
        token = await login(api, username, seed["password"])
        ws = _FakeWS([json.dumps({"type": "auth", "token": token})])
        auth = await _authenticate_ws(ws, db_session)
        assert auth is not None, username
        user, exp = auth
        assert user.role == role
        assert user.account_id is not None
        assert exp > datetime.now(UTC).timestamp()
        assert ws.closed is None


async def test_authenticate_ws_rejects_device_token(db_session, seed):
    token = create_device_access_token(
        str(seed["devices"]["double"].device_id),
        str(uuid.uuid4()),
        datetime.now(UTC) + timedelta(minutes=5),
    )
    ws = _FakeWS([json.dumps({"type": "auth", "token": token})])
    auth = await _authenticate_ws(ws, db_session)
    assert auth is None
    assert ws.closed is not None and ws.closed[0] == 4403


async def test_authenticate_ws_rejects_disabled_account(db_session, seed):
    token = create_user_access_token(
        str(seed["accounts"]["nurse_locked"].account_id), "nurse"
    )
    ws = _FakeWS([json.dumps({"type": "auth", "token": token})])
    auth = await _authenticate_ws(ws, db_session)
    assert auth is None
    assert ws.closed is not None and ws.closed[0] == 4401
