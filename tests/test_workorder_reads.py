"""工单按账号已读（Day7 段1.5）：回执粒度、counts 新口径、mine/服务端筛选。

沿用 conftest 的 kangban_test 事务隔离：api 与 db_session 共用同一会话，
测试结束整体回滚。语义写死：已读按账号（workorder_reads），处理按角色
（disposition 三态，handled 仍是关闭门禁）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models import (
    BuildingAssignment,
    DoctorElderAssignment,
    FamilyElderLink,
    WorkorderRead,
)
from app.services import workorder as svc
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


async def _bind_family(db_session, seed, *, elder: str = "d1") -> None:
    db_session.add(
        FamilyElderLink(
            account_id=seed["accounts"]["family"].account_id,
            elder_id=seed["elders"][elder].elder_id,
            status="active",
        )
    )
    await db_session.flush()


async def _create(
    db_session, seed, *, category: str = "chest_pain", text: str = "我胸口疼，喘不上气"
):
    workorder, _ = await svc.create_sensitive_work_order(
        db_session,
        elder_id=seed["elders"]["d1"].elder_id,
        category=category,
        device_id=seed["devices"]["double"].device_id,
        trigger_text=text,
    )
    return workorder


async def _receipt_count(db_session, workorder_id) -> int:
    return int(
        (
            await db_session.execute(
                select(func.count())
                .select_from(WorkorderRead)
                .where(WorkorderRead.workorder_id == workorder_id)
            )
        ).scalar_one()
    )


async def _login_and_list(api, username, seed, *, query: str = "") -> dict:
    token = await login(api, username, seed["password"])
    resp = await api.get(f"/api/v1/staff/workorders{query}", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---- 回执按账号 ----


async def test_read_receipt_is_per_account(api, db_session, seed):
    await _bind_doctor_and_nurse(db_session, seed)
    workorder = await _create(db_session, seed)

    # 医生已读：自己 is_read=True，护士仍 False
    doctor_token = await login(api, "doctor", seed["password"])
    resp = await api.post(
        f"/api/v1/staff/workorders/{workorder.workorder_id}/read",
        headers=_auth(doctor_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_read"] is True

    doctor = await _login_and_list(api, "doctor", seed)
    assert doctor["total"] == 1
    assert doctor["items"][0]["is_read"] is True
    assert doctor["items"][0]["read_at"] is not None
    assert doctor["items"][0]["my_status"] == "read"

    nurse = await _login_and_list(api, "nurse_a", seed)
    assert nurse["items"][0]["is_read"] is False
    assert nurse["items"][0]["read_at"] is None
    # 处理项仍是角色级三态：医生已读不改变护士未读
    assert nurse["items"][0]["my_status"] == "unread"

    # 护士再读：自己变已读，回执两行，医生不受影响
    nurse_token = await login(api, "nurse_a", seed["password"])
    resp = await api.post(
        f"/api/v1/staff/workorders/{workorder.workorder_id}/read",
        headers=_auth(nurse_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_read"] is True
    assert await _receipt_count(db_session, workorder.workorder_id) == 2

    doctor_again = await _login_and_list(api, "doctor", seed)
    assert doctor_again["items"][0]["is_read"] is True


async def test_mark_read_idempotent_after_handled(api, db_session, seed):
    await _bind_doctor_and_nurse(db_session, seed)
    workorder = await _create(db_session, seed)
    token = await login(api, "doctor", seed["password"])

    resp = await api.post(
        f"/api/v1/staff/workorders/{workorder.workorder_id}/disposition",
        headers=_auth(token),
        json={"plan_text": "已电话联系值班医生上门查看"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["my_status"] == "handled"

    # handled 后重复 mark_read：幂等返回，不回退状态，回执仍可插入
    resp = await api.post(
        f"/api/v1/staff/workorders/{workorder.workorder_id}/read",
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["my_status"] == "handled"
    assert resp.json()["is_read"] is True
    assert await _receipt_count(db_session, workorder.workorder_id) == 1


async def test_count_open_by_role_excludes_own_receipt(api, db_session, seed):
    await _bind_doctor_and_nurse(db_session, seed)
    workorder = await _create(db_session, seed)

    doctor_counts = await _login_and_list(api, "doctor", seed, query="/counts")
    assert doctor_counts["unhandled"] == 1

    # 医生本人已读：本人计数归零，护士计数不受影响
    doctor_token = await login(api, "doctor", seed["password"])
    resp = await api.post(
        f"/api/v1/staff/workorders/{workorder.workorder_id}/read",
        headers=_auth(doctor_token),
    )
    assert resp.status_code == 200

    doctor_counts = await _login_and_list(api, "doctor", seed, query="/counts")
    assert doctor_counts["unhandled"] == 0
    nurse_counts = await _login_and_list(api, "nurse_a", seed, query="/counts")
    assert nurse_counts["unhandled"] == 1


# ---- mine / status 服务端筛选 ----


async def test_mine_filter_server_side(api, db_session, seed):
    await _bind_doctor_and_nurse(db_session, seed)
    workorder = await _create(db_session, seed)
    token = await login(api, "doctor", seed["password"])

    unread = await _login_and_list(api, "doctor", seed, query="?mine=unread")
    assert unread["total"] == 1
    read = await _login_and_list(api, "doctor", seed, query="?mine=read")
    assert read["total"] == 0
    handled = await _login_and_list(api, "doctor", seed, query="?mine=handled")
    assert handled["total"] == 0

    resp = await api.post(
        f"/api/v1/staff/workorders/{workorder.workorder_id}/read", headers=_auth(token)
    )
    assert resp.status_code == 200

    unread = await _login_and_list(api, "doctor", seed, query="?mine=unread")
    assert unread["total"] == 0
    read = await _login_and_list(api, "doctor", seed, query="?mine=read")
    assert read["total"] == 1

    resp = await api.post(
        f"/api/v1/staff/workorders/{workorder.workorder_id}/disposition",
        headers=_auth(token),
        json={"plan_text": "已上门查看，情况稳定"},
    )
    assert resp.status_code == 200
    handled = await _login_and_list(api, "doctor", seed, query="?mine=handled")
    assert handled["total"] == 1
    # handled 不再出现在 read 口径
    read = await _login_and_list(api, "doctor", seed, query="?mine=read")
    assert read["total"] == 0


async def test_invalid_mine_value_returns_422(api, db_session, seed):
    await _bind_doctor_and_nurse(db_session, seed)
    token = await login(api, "doctor", seed["password"])
    resp = await api.get(
        "/api/v1/staff/workorders?mine=other", headers=_auth(token)
    )
    assert resp.status_code == 422


async def test_admin_and_family_mine_returns_422(api, db_session, seed):
    await _bind_family(db_session, seed)
    admin_token = await login(api, "admin", seed["password"])
    resp = await api.get(
        "/api/v1/admin/workorders?mine=unread", headers=_auth(admin_token)
    )
    assert resp.status_code == 422

    family_token = await login(api, "family", seed["password"])
    resp = await api.get(
        f"/api/v1/family/elders/{seed['elders']['d1'].elder_id}/workorders?mine=read",
        headers=_auth(family_token),
    )
    assert resp.status_code == 422


async def test_family_status_filter_passthrough(api, db_session, seed):
    await _bind_doctor_and_nurse(db_session, seed)
    await _bind_family(db_session, seed)
    open_order = await _create(db_session, seed, text="我胸口疼，喘不上气")
    await _create(db_session, seed, category="fall", text="我刚才摔了一跤")

    # 关闭第一单：双角色处理 + 管理员终审
    for username in ("doctor", "nurse_a"):
        token = await login(api, username, seed["password"])
        resp = await api.post(
            f"/api/v1/staff/workorders/{open_order.workorder_id}/disposition",
            headers=_auth(token),
            json={"plan_text": "已现场确认，无需进一步处置"},
        )
        assert resp.status_code == 200, resp.text
    admin_token = await login(api, "admin", seed["password"])
    resp = await api.post(
        f"/api/v1/admin/workorders/{open_order.workorder_id}/close",
        headers=_auth(admin_token),
        json={"note": "已复核"},
    )
    assert resp.status_code == 200, resp.text

    family_token = await login(api, "family", seed["password"])
    base = f"/api/v1/family/elders/{seed['elders']['d1'].elder_id}/workorders"
    resp = await api.get(f"{base}?status=resolved", headers=_auth(family_token))
    assert resp.status_code == 200
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["status"] == "resolved"

    resp = await api.get(f"{base}?status=open", headers=_auth(family_token))
    assert resp.status_code == 200
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["status"] == "open"

    # 家属无 is_read 语义
    resp = await api.get(base, headers=_auth(family_token))
    items = resp.json()["items"]
    assert len(items) == 2
    assert all(it["is_read"] is None and it["read_at"] is None for it in items)
    # 列表项携带 type/title（敏感工单 title 兜底为类别名）
    assert all(it.get("type") == "sensitive" for it in items)
    assert all(it.get("title") for it in items)


# ---- 服务层 list_receipts ----


async def test_list_receipts_maps_by_workorder(db_session, seed):
    await _bind_doctor_and_nurse(db_session, seed)
    workorder = await _create(db_session, seed)
    account_id = seed["accounts"]["doctor"].account_id

    receipts = await svc.list_receipts(
        db_session, account_id=account_id, workorder_ids=[workorder.workorder_id]
    )
    assert receipts == {}

    await svc.mark_read(
        db_session,
        workorder_id=workorder.workorder_id,
        role="doctor",
        account_id=account_id,
    )
    receipts = await svc.list_receipts(
        db_session, account_id=account_id, workorder_ids=[workorder.workorder_id]
    )
    assert workorder.workorder_id in receipts
    assert receipts[workorder.workorder_id] is not None


@pytest.mark.parametrize(
    "role,account", [("admin", "admin"), ("family", "family"), ("nurse", "nurse_a")]
)
async def test_count_open_by_role_non_doctor_semantics(db_session, seed, role, account):
    """admin/family 恒 0；nurse 依赖处理项（无绑定护士看不到）。"""
    await _bind_doctor_and_nurse(db_session, seed)
    await _create(db_session, seed)
    count = await svc.count_open_by_role(
        db_session, role=role, account_id=seed["accounts"][account].account_id
    )
    if role == "nurse":
        assert count == 1
    else:
        assert count == 0
