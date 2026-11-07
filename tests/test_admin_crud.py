"""阶段一管理 API 测试：rooms/personas/devices/accounts CRUD + elders 列表/软删。

写操作均为「HTTP 断言 + 回查数据库」双验证。
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.auth.device_token import issue_access_token, issue_register_token
from app.auth.passwords import verify_password
from app.models import (
    Account,
    BuildingAssignment,
    Device,
    DeviceToken,
    DoctorElderAssignment,
    Elder,
    FamilyElderLink,
    Room,
)
from app.models.enums import DeviceStatus
from tests.conftest import login

ADMIN = None  # 占位：token 统一在用例内取


async def _admin_headers(api, seed) -> dict:
    token = await login(api, "admin", seed["password"])
    return {"Authorization": f"Bearer {token}"}


# ---- rooms ----


async def test_admin_create_room_201_and_db_check(api, seed, db_session):
    headers = await _admin_headers(api, seed)
    resp = await api.post(
        "/api/v1/admin/rooms",
        json={"building_code": "C", "floor": 3, "room_number": "301", "bed_count": 2},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    room_id = resp.json()["room_id"]
    row = (
        await db_session.execute(select(Room).where(Room.room_id == room_id))
    ).scalar_one()
    assert row.building_code == "C" and row.bed_count == 2 and row.status == "active"


async def test_admin_create_room_duplicate_409(api, seed):
    headers = await _admin_headers(api, seed)
    resp = await api.post(
        "/api/v1/admin/rooms",
        json={"building_code": "A", "floor": 1, "room_number": "101", "bed_count": 1},
        headers=headers,
    )
    assert resp.status_code == 409


async def test_nurse_create_room_403(api, seed):
    headers = {"Authorization": f"Bearer {await login(api, 'nurse_a', seed['password'])}"}
    resp = await api.post(
        "/api/v1/admin/rooms",
        json={"building_code": "C", "floor": 3, "room_number": "301", "bed_count": 1},
        headers=headers,
    )
    assert resp.status_code == 403


async def test_admin_delete_room_occupied_409_then_free_room_204(api, seed, db_session):
    headers = await _admin_headers(api, seed)
    occupied = seed["rooms"]["single"]
    resp = await api.delete(f"/api/v1/admin/rooms/{occupied.room_id}", headers=headers)
    assert resp.status_code == 409
    row = (
        await db_session.execute(select(Room).where(Room.room_id == occupied.room_id))
    ).scalar_one()
    assert row.status == "active"  # 回查：未被停用

    # 空闲房间：新建后删除（无长者无设备）
    resp = await api.post(
        "/api/v1/admin/rooms",
        json={"building_code": "D", "floor": 4, "room_number": "401", "bed_count": 1},
        headers=headers,
    )
    room_id = resp.json()["room_id"]
    resp = await api.delete(f"/api/v1/admin/rooms/{room_id}", headers=headers)
    assert resp.status_code == 204
    row = (
        await db_session.execute(select(Room).where(Room.room_id == room_id))
    ).scalar_one()
    assert row.status == "inactive"


# ---- personas ----


async def test_admin_create_persona_201_and_duplicate_409(api, seed, db_session):
    headers = await _admin_headers(api, seed)
    resp = await api.post(
        "/api/v1/admin/personas",
        json={"code": "p-new", "name": "新人设"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    persona_id = resp.json()["persona_id"]
    row = (
        await db_session.execute(
            select(type(seed["personas"]["on"])).where(
                type(seed["personas"]["on"]).persona_id == persona_id
            )
        )
    ).scalar_one()
    assert row.code == "p-new"

    resp = await api.post(
        "/api/v1/admin/personas",
        json={"code": "p-new", "name": "重复编码"},
        headers=headers,
    )
    assert resp.status_code == 409


async def test_admin_delete_persona_soft(api, seed, db_session):
    from app.models import Persona

    headers = await _admin_headers(api, seed)
    persona_id = seed["personas"]["on"].persona_id
    resp = await api.delete(f"/api/v1/admin/personas/{persona_id}", headers=headers)
    assert resp.status_code == 204
    row = (
        await db_session.execute(select(Persona).where(Persona.persona_id == persona_id))
    ).scalar_one()
    assert row.is_active is False
    # 幂等
    resp = await api.delete(f"/api/v1/admin/personas/{persona_id}", headers=headers)
    assert resp.status_code == 204


async def test_doctor_access_personas_403(api, seed):
    headers = {"Authorization": f"Bearer {await login(api, 'doctor', seed['password'])}"}
    resp = await api.get("/api/v1/admin/personas", headers=headers)
    assert resp.status_code == 403


# ---- devices ----


async def test_admin_device_crud_flow(api, seed, db_session):
    headers = await _admin_headers(api, seed)
    room_id = str(seed["rooms"]["other"].room_id)
    # 建设备 → pending
    resp = await api.post(
        "/api/v1/admin/devices",
        json={"device_code": "TAB-NEW-1", "room_id": room_id},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    device_id = resp.json()["device_id"]
    row = (
        await db_session.execute(select(Device).where(Device.device_id == device_id))
    ).scalar_one()
    assert row.status == DeviceStatus.PENDING

    # 重复编码 409
    resp = await api.post(
        "/api/v1/admin/devices",
        json={"device_code": "TAB-NEW-1", "room_id": room_id},
        headers=headers,
    )
    assert resp.status_code == 409

    # 更新状态 disabled
    resp = await api.put(
        f"/api/v1/admin/devices/{device_id}", json={"status": "disabled"}, headers=headers
    )
    assert resp.status_code == 200 and resp.json()["status"] == "disabled"

    # 软删 → retired
    resp = await api.delete(f"/api/v1/admin/devices/{device_id}", headers=headers)
    assert resp.status_code == 204
    row = (
        await db_session.execute(select(Device).where(Device.device_id == device_id))
    ).scalar_one()
    assert row.status == DeviceStatus.RETIRED


async def test_admin_device_tokens_list_and_revoke_all(api, seed, db_session):
    headers = await _admin_headers(api, seed)
    device = seed["devices"]["free"]
    # 直接签发两条凭证（register + access）
    await issue_register_token(db_session, device.device_id)
    await issue_access_token(db_session, device.device_id)
    await db_session.commit()

    resp = await api.get(
        f"/api/v1/admin/devices/{device.device_id}/tokens", headers=headers
    )
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 2
    assert all("token_hash" not in i and "registration_token" not in i for i in items)

    resp = await api.post(
        f"/api/v1/admin/devices/{device.device_id}/tokens/revoke-all", headers=headers
    )
    assert resp.status_code == 200 and resp.json()["revoked_count"] == 2
    rows = (
        await db_session.execute(
            select(DeviceToken).where(DeviceToken.device_id == device.device_id)
        )
    ).scalars().all()
    assert all(t.revoked_at is not None for t in rows)


async def test_nurse_admin_devices_403(api, seed):
    headers = {"Authorization": f"Bearer {await login(api, 'nurse_a', seed['password'])}"}
    resp = await api.get("/api/v1/admin/devices", headers=headers)
    assert resp.status_code == 403


# ---- accounts ----


async def test_admin_create_account_201_db_hash_and_login(api, seed, db_session):
    headers = await _admin_headers(api, seed)
    resp = await api.post(
        "/api/v1/admin/accounts",
        json={
            "username": "nurse_new",
            "display_name": "新护工",
            "role": "nurse",
            "password": "Sup3rSecret!",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    account_id = resp.json()["account_id"]
    assert "password_hash" not in resp.json() and "password" not in resp.json()
    row = (
        await db_session.execute(select(Account).where(Account.account_id == account_id))
    ).scalar_one()
    assert verify_password("Sup3rSecret!", row.password_hash)
    assert row.password_hash != "Sup3rSecret!"

    # 新账号可登录
    resp = await api.post(
        "/api/v1/auth/login", json={"username": "nurse_new", "password": "Sup3rSecret!"}
    )
    assert resp.status_code == 200

    # 重复用户名 409
    resp = await api.post(
        "/api/v1/admin/accounts",
        json={
            "username": "nurse_new",
            "display_name": "重复",
            "role": "nurse",
            "password": "Sup3rSecret!",
        },
        headers=headers,
    )
    assert resp.status_code == 409


async def test_admin_reset_password_one_time(api, seed, db_session):
    headers = await _admin_headers(api, seed)
    target = seed["accounts"]["nurse_a"]
    resp = await api.post(
        f"/api/v1/admin/accounts/{target.account_id}/reset-password", headers=headers
    )
    assert resp.status_code == 200
    new_password = resp.json()["new_password"]
    assert len(new_password) >= 8
    row = (
        await db_session.execute(
            select(Account).where(Account.account_id == target.account_id)
        )
    ).scalar_one()
    assert verify_password(new_password, row.password_hash)
    assert not verify_password(seed["password"], row.password_hash)  # 旧密码失效


async def test_family_cannot_manage_accounts_403(api, seed):
    headers = {"Authorization": f"Bearer {await login(api, 'family', seed['password'])}"}
    resp = await api.get("/api/v1/admin/accounts", headers=headers)
    assert resp.status_code == 403


# ---- elders 列表与软删 ----


async def _link_models(db_session, seed):
    """给 nurse_a 授权 A 楼栋、doctor 关联王单间、family 关联李双一。"""
    db_session.add(
        BuildingAssignment(
            account_id=seed["accounts"]["nurse_a"].account_id, building_code="A"
        )
    )
    db_session.add(
        DoctorElderAssignment(
            account_id=seed["accounts"]["doctor"].account_id,
            elder_id=seed["elders"]["single"].elder_id,
        )
    )
    db_session.add(
        FamilyElderLink(
            account_id=seed["accounts"]["family"].account_id,
            elder_id=seed["elders"]["d1"].elder_id,
            relation="daughter",
        )
    )
    await db_session.commit()


async def test_elder_list_role_scoping(api, seed, db_session):
    await _link_models(db_session, seed)

    admin_headers = await _admin_headers(api, seed)
    resp = await api.get("/api/v1/elders", headers=admin_headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 4  # admin 全量

    nurse_headers = {
        "Authorization": f"Bearer {await login(api, 'nurse_a', seed['password'])}"
    }
    resp = await api.get("/api/v1/elders", headers=nurse_headers)
    names = {e["name"] for e in resp.json()}
    assert names == {"王单间", "李双一", "赵双二"}  # 仅 A 楼栋，不含 B 楼栋钱他楼

    doctor_headers = {
        "Authorization": f"Bearer {await login(api, 'doctor', seed['password'])}"
    }
    resp = await api.get("/api/v1/elders", headers=doctor_headers)
    assert [e["name"] for e in resp.json()] == ["王单间"]

    family_headers = {
        "Authorization": f"Bearer {await login(api, 'family', seed['password'])}"
    }
    resp = await api.get("/api/v1/elders", headers=family_headers)
    assert [e["name"] for e in resp.json()] == ["李双一"]


async def test_family_can_read_linked_elder_detail(api, seed, db_session):
    await _link_models(db_session, seed)
    headers = {"Authorization": f"Bearer {await login(api, 'family', seed['password'])}"}
    elder_id = seed["elders"]["d1"].elder_id
    resp = await api.get(f"/api/v1/elders/{elder_id}", headers=headers)
    assert resp.status_code == 200 and resp.json()["name"] == "李双一"
    # 范围外 404
    resp = await api.get(f"/api/v1/elders/{seed['elders']['other'].elder_id}", headers=headers)
    assert resp.status_code == 404


async def test_admin_delete_elder_soft_and_bed_freed(api, seed, db_session):
    headers = await _admin_headers(api, seed)
    elder = seed["elders"]["d2"]  # double 房 2 号床
    resp = await api.delete(f"/api/v1/elders/{elder.elder_id}", headers=headers)
    assert resp.status_code == 204
    row = (
        await db_session.execute(select(Elder).where(Elder.elder_id == elder.elder_id))
    ).scalar_one()
    assert row.status == "discharged" and row.room_id is None and row.bed_no is None

    # 床位已释放：新长者可入住原床位
    resp = await api.post(
        "/api/v1/elders",
        json={
            "name": "新住户",
            "persona_id": str(seed["personas"]["on"].persona_id),
            "room_id": str(seed["rooms"]["double"].room_id),
            "bed_no": 2,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text

    # 幂等：再次 DELETE 仍 204
    resp = await api.delete(f"/api/v1/elders/{elder.elder_id}", headers=headers)
    assert resp.status_code == 204


async def test_family_cannot_delete_elder_403(api, seed):
    headers = {"Authorization": f"Bearer {await login(api, 'family', seed['password'])}"}
    resp = await api.delete(
        f"/api/v1/elders/{seed['elders']['d1'].elder_id}", headers=headers
    )
    assert resp.status_code == 403


# ---- 设备注册激活缺陷修复 ----


async def test_device_registration_activates_device(api, seed, db_session):
    """缺陷修复复测：注册成功后 devices.status=active，/devices/me 返回 200。"""
    headers = await _admin_headers(api, seed)
    # 建一台 pending 设备
    resp = await api.post(
        "/api/v1/admin/devices",
        json={
            "device_code": "TAB-ACT-1",
            "room_id": str(seed["rooms"]["other"].room_id),
        },
        headers=headers,
    )
    assert resp.status_code == 201
    device_id = resp.json()["device_id"]
    row = (
        await db_session.execute(select(Device).where(Device.device_id == device_id))
    ).scalar_one()
    assert row.status == DeviceStatus.PENDING

    # pending 设备即使有有效 Token 也 401
    issued = await issue_access_token(db_session, row.device_id)
    await db_session.commit()
    resp = await api.get(
        "/api/v1/devices/me", headers={"Authorization": f"Bearer {issued.access_token}"}
    )
    assert resp.status_code == 401

    # 签发注册凭证并注册
    resp = await api.post(
        f"/api/v1/admin/devices/{device_id}/registration-tokens", headers=headers
    )
    assert resp.status_code == 200
    reg_token = resp.json()["registration_token"]
    resp = await api.post(
        "/api/v1/auth/device/register",
        json={"device_code": "TAB-ACT-1", "registration_token": reg_token},
    )
    assert resp.status_code == 200, resp.text
    access_token = resp.json()["access_token"]

    # 回查数据库：设备已激活
    await db_session.refresh(row)
    assert row.status == DeviceStatus.ACTIVE

    # 设备 Token 现在可用
    resp = await api.get(
        "/api/v1/devices/me", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["device_code"] == "TAB-ACT-1"


async def test_family_expired_link_hidden_404(api, seed, db_session):
    """关联过期后家属视图 404（隐藏存在性）。"""
    await _link_models(db_session, seed)
    headers = {"Authorization": f"Bearer {await login(api, 'family', seed['password'])}"}
    elder_id = seed["elders"]["d1"].elder_id
    # 先可见
    assert (
        await api.get(f"/api/v1/elders/{elder_id}", headers=headers)
    ).status_code == 200
    # 把关联置为已过期
    link = (
        await db_session.execute(
            select(FamilyElderLink).where(
                FamilyElderLink.account_id == seed["accounts"]["family"].account_id
            )
        )
    ).scalar_one()
    link.valid_until = datetime.now(UTC) - timedelta(hours=1)
    await db_session.commit()
    resp = await api.get(f"/api/v1/elders/{elder_id}", headers=headers)
    assert resp.status_code == 404


async def test_unknown_elder_404_valid_uuid(api, seed):
    headers = await _admin_headers(api, seed)
    resp = await api.get(f"/api/v1/elders/{uuid.uuid4()}", headers=headers)
    assert resp.status_code == 404
