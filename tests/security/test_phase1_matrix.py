"""阶段一越权矩阵测试（权限矩阵设计文档 §6 的 12 条逐条实测）。

写操作均回查 kangban_test 数据库断言。
"""

from sqlalchemy import select

from app.auth.device_token import issue_access_token
from app.models import Account, DeviceToken, Elder
from tests.conftest import login


async def _admin(api, seed) -> dict:
    return {"Authorization": f"Bearer {await login(api, 'admin', seed['password'])}"}


async def _user(api, seed, username: str) -> dict:
    return {
        "Authorization": f"Bearer {await login(api, username, seed['password'])}"
    }


async def test_01_nurse_create_room_403(api, seed):
    headers = await _user(api, seed, "nurse_a")
    resp = await api.post(
        "/api/v1/admin/rooms",
        json={"building_code": "X", "floor": 9, "room_number": "901", "bed_count": 1},
        headers=headers,
    )
    assert resp.status_code == 403


async def test_02_family_delete_elder_403_even_linked(api, seed, db_session):
    """家属即使是关联长者也不能删除。"""
    headers = await _user(api, seed, "family")
    resp = await api.delete(
        f"/api/v1/elders/{seed['elders']['single'].elder_id}", headers=headers
    )
    assert resp.status_code == 403
    row = (
        await db_session.execute(
            select(Elder).where(Elder.elder_id == seed["elders"]["single"].elder_id)
        )
    ).scalar_one()
    assert row.status == "active"  # 回查：未被删除


async def test_03_doctor_out_of_scope_elder_404(api, seed, db_session):
    """医生查无关联长者 → 404 隐藏存在性。"""
    headers = await _user(api, seed, "doctor")
    resp = await api.get(
        f"/api/v1/elders/{seed['elders']['other'].elder_id}", headers=headers
    )
    assert resp.status_code == 404


async def test_04_family_expired_link_404(api, seed, db_session):
    from datetime import UTC, datetime, timedelta

    from app.models import FamilyElderLink

    db_session.add(
        FamilyElderLink(
            account_id=seed["accounts"]["family"].account_id,
            elder_id=seed["elders"]["d1"].elder_id,
            relation="son",
            valid_until=datetime.now(UTC) - timedelta(minutes=1),  # 已过期
        )
    )
    await db_session.commit()
    headers = await _user(api, seed, "family")
    resp = await api.get(
        f"/api/v1/elders/{seed['elders']['d1'].elder_id}", headers=headers
    )
    assert resp.status_code == 404


async def test_05_disabled_account_login_401(api, seed):
    """停用账号登录 401（seed 中 nurse_locked 为 disabled）。"""
    resp = await api.post(
        "/api/v1/auth/login",
        json={"username": "nurse_locked", "password": seed["password"]},
    )
    assert resp.status_code == 401


async def test_06_disabled_account_old_jwt_401(api, seed, db_session):
    """停用后，停用前签发的有效 JWT 立即失效。"""
    headers = await _user(api, seed, "nurse_b")
    # 停用前可用
    assert (await api.get("/api/v1/auth/me", headers=headers)).status_code == 200
    resp = await api.post(
        f"/api/v1/admin/accounts/{seed['accounts']['nurse_b'].account_id}/disable",
        headers=await _admin(api, seed),
    )
    assert resp.status_code == 200
    # 旧 JWT 已失效
    resp = await api.get("/api/v1/auth/me", headers=headers)
    assert resp.status_code == 401
    # 回查
    row = (
        await db_session.execute(
            select(Account).where(Account.account_id == seed["accounts"]["nurse_b"].account_id)
        )
    ).scalar_one()
    assert row.status == "disabled"


async def test_07_device_token_on_elders_403(api, seed, db_session):
    """设备 Token 调人接口 → 403（Token 互斥）。"""
    issued = await issue_access_token(db_session, seed["devices"]["free"].device_id)
    await db_session.commit()
    resp = await api.get(
        "/api/v1/elders", headers={"Authorization": f"Bearer {issued.access_token}"}
    )
    assert resp.status_code == 403


async def test_08_user_token_on_devices_me_403(api, seed):
    """人 JWT 调设备接口 → 403（Token 互斥）。"""
    headers = await _admin(api, seed)
    resp = await api.get("/api/v1/devices/me", headers=headers)
    assert resp.status_code == 403


async def test_09_nurse_list_excludes_other_building(api, seed, db_session):
    """护工列表仅含所辖楼栋长者（回查比对）。"""
    from app.models import BuildingAssignment

    db_session.add(
        BuildingAssignment(
            account_id=seed["accounts"]["nurse_a"].account_id, building_code="A"
        )
    )
    await db_session.commit()
    headers = await _user(api, seed, "nurse_a")
    resp = await api.get("/api/v1/elders", headers=headers)
    assert resp.status_code == 200
    returned_ids = {e["elder_id"] for e in resp.json()}
    # 全库比对：B 楼栋的钱他楼必须不在
    all_ids = {
        str(e.elder_id)
        for e in (
            await db_session.execute(select(Elder))
        ).scalars().all()
        if e.status == "active"
    }
    assert str(seed["elders"]["other"].elder_id) in all_ids  # 库里存在
    assert str(seed["elders"]["other"].elder_id) not in returned_ids  # 但不可见


async def test_10_disable_last_active_admin_409(api, seed, db_session):
    headers = await _admin(api, seed)
    resp = await api.post(
        f"/api/v1/admin/accounts/{seed['accounts']['admin'].account_id}/disable",
        headers=headers,
    )
    assert resp.status_code == 409
    # 回查：admin 仍 active
    row = (
        await db_session.execute(
            select(Account).where(Account.account_id == seed["accounts"]["admin"].account_id)
        )
    ).scalar_one()
    assert row.status == "active"


async def test_11_delete_room_with_active_device_409(api, seed):
    headers = await _admin(api, seed)
    resp = await api.delete(
        f"/api/v1/admin/rooms/{seed['rooms']['double'].room_id}", headers=headers
    )
    assert resp.status_code == 409


async def test_12_revoke_all_then_old_token_401(api, seed, db_session):
    """吊销设备全部凭证后，旧 Token 立即 401。"""
    issued = await issue_access_token(db_session, seed["devices"]["free"].device_id)
    await db_session.commit()
    # 吊销前有效
    resp = await api.get(
        "/api/v1/devices/me", headers={"Authorization": f"Bearer {issued.access_token}"}
    )
    assert resp.status_code == 200
    # 吊销全部
    resp = await api.post(
        f"/api/v1/admin/devices/{seed['devices']['free'].device_id}/tokens/revoke-all",
        headers=await _admin(api, seed),
    )
    assert resp.status_code == 200
    assert resp.json()["revoked_count"] >= 1
    rows = (
        await db_session.execute(
            select(DeviceToken).where(
                DeviceToken.device_id == seed["devices"]["free"].device_id
            )
        )
    ).scalars().all()
    assert all(t.revoked_at is not None for t in rows)
    # 旧 Token 已失效
    resp = await api.get(
        "/api/v1/devices/me", headers={"Authorization": f"Bearer {issued.access_token}"}
    )
    assert resp.status_code == 401


async def test_forged_token_401(api, seed):
    """伪造 Token 401。"""
    resp = await api.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.forged.sig"},
    )
    assert resp.status_code == 401
