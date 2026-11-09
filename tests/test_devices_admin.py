"""设备管理端补测（Day5.4）：档案 CRUD、绑定、凭证签发与单条吊销分支。"""

import logging
import uuid

from sqlalchemy import select

from app.auth.device_token import issue_access_token
from app.models import Device
from app.models.enums import DeviceStatus
from tests.conftest import login


async def _admin_headers(api, seed) -> dict:
    return {"Authorization": f"Bearer {await login(api, 'admin', seed['password'])}"}


async def _device_row(db_session, device_id) -> Device:
    return (
        await db_session.execute(select(Device).where(Device.device_id == device_id))
    ).scalar_one()


async def test_admin_list_and_get_device(api, seed, db_session):
    headers = await _admin_headers(api, seed)
    resp = await api.get("/api/v1/admin/devices", headers=headers)
    assert resp.status_code == 200
    codes = {d["device_code"] for d in resp.json()}
    assert codes >= {"TAB-SINGLE", "TAB-DOUBLE", "TAB-FREE"}

    device = seed["devices"]["free"]
    resp = await api.get(f"/api/v1/admin/devices/{device.device_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["device_code"] == "TAB-FREE"

    resp = await api.get(f"/api/v1/admin/devices/{uuid.uuid4()}", headers=headers)
    assert resp.status_code == 404


async def test_admin_get_device_requires_admin(api, seed):
    headers = {"Authorization": f"Bearer {await login(api, 'nurse_a', seed['password'])}"}
    resp = await api.get(
        f"/api/v1/admin/devices/{seed['devices']['free'].device_id}", headers=headers
    )
    assert resp.status_code == 403


async def test_admin_update_device_404_and_status_rules(api, seed, db_session):
    headers = await _admin_headers(api, seed)
    resp = await api.put(
        f"/api/v1/admin/devices/{uuid.uuid4()}", json={"status": "disabled"}, headers=headers
    )
    assert resp.status_code == 404

    device = seed["devices"]["free"]
    resp = await api.put(
        f"/api/v1/admin/devices/{device.device_id}", json={"status": "disabled"}, headers=headers
    )
    assert resp.status_code == 200 and resp.json()["status"] == "disabled"
    row = await _device_row(db_session, device.device_id)
    assert row.status == DeviceStatus.DISABLED

    # retired 只能经 DELETE（含凭证吊销），直接改状态必须 422
    resp = await api.put(
        f"/api/v1/admin/devices/{device.device_id}", json={"status": "retired"}, headers=headers
    )
    assert resp.status_code == 422

    # 非法枚举值由 schema 层拦截为 422
    resp = await api.put(
        f"/api/v1/admin/devices/{device.device_id}", json={"status": "unknown"}, headers=headers
    )
    assert resp.status_code == 422


async def test_admin_update_device_rebind_conflict_409(api, seed, db_session):
    headers = await _admin_headers(api, seed)
    double_room = seed["rooms"]["double"]

    # free 先占用双人间 1 号床
    resp = await api.put(
        f"/api/v1/devices/{seed['devices']['free'].device_id}/binding",
        json={"room_id": str(double_room.room_id), "bed_no": 1},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    # 原双人间设备再换绑到同一床位 → 409
    resp = await api.put(
        f"/api/v1/admin/devices/{seed['devices']['double'].device_id}",
        json={"room_id": str(double_room.room_id), "bed_no": 1},
        headers=headers,
    )
    assert resp.status_code == 409
    row = await _device_row(db_session, seed["devices"]["double"].device_id)
    assert row.bed_no is None  # 回查：失败请求未写入


async def test_bind_device_not_found_404(api, seed):
    headers = await _admin_headers(api, seed)
    resp = await api.put(
        f"/api/v1/devices/{uuid.uuid4()}/binding",
        json={"room_id": str(seed["rooms"]["other"].room_id), "bed_no": 1},
        headers=headers,
    )
    assert resp.status_code == 404


async def test_bind_device_idempotency_key_logged(api, seed, caplog):
    headers = await _admin_headers(api, seed)
    headers["Idempotency-Key"] = "bind-free-001"
    with caplog.at_level(logging.INFO, logger="app.api.routes.devices"):
        resp = await api.put(
            f"/api/v1/devices/{seed['devices']['free'].device_id}/binding",
            json={"room_id": str(seed["rooms"]["other"].room_id), "bed_no": 1},
            headers=headers,
        )
    assert resp.status_code == 200, resp.text
    records = [r for r in caplog.records if getattr(r, "event", None) == "idempotency_key"]
    assert records and records[0].idempotency_key == "bind-free-001"
    assert records[0].object_id == str(seed["devices"]["free"].device_id)


async def test_admin_create_device_validation_422(api, seed):
    headers = await _admin_headers(api, seed)
    # 房间不存在
    resp = await api.post(
        "/api/v1/admin/devices",
        json={"device_code": "TAB-BAD-ROOM", "room_id": str(uuid.uuid4())},
        headers=headers,
    )
    assert resp.status_code == 422
    # 床位超容量（单人间只有 1 床）
    resp = await api.post(
        "/api/v1/admin/devices",
        json={
            "device_code": "TAB-BAD-BED",
            "room_id": str(seed["rooms"]["single"].room_id),
            "bed_no": 2,
        },
        headers=headers,
    )
    assert resp.status_code == 422


async def test_registration_token_for_missing_device_404(api, seed):
    headers = await _admin_headers(api, seed)
    resp = await api.post(
        f"/api/v1/admin/devices/{uuid.uuid4()}/registration-tokens", headers=headers
    )
    assert resp.status_code == 404


async def test_list_tokens_for_missing_device_404(api, seed):
    headers = await _admin_headers(api, seed)
    resp = await api.get(
        f"/api/v1/admin/devices/{uuid.uuid4()}/tokens", headers=headers
    )
    assert resp.status_code == 404


async def test_revoke_single_token_and_not_found_branches(api, seed, db_session):
    headers = await _admin_headers(api, seed)
    device = seed["devices"]["free"]
    issued = await issue_access_token(db_session, device.device_id)
    await db_session.commit()

    resp = await api.get(f"/api/v1/admin/devices/{device.device_id}/tokens", headers=headers)
    assert resp.status_code == 200
    token_id = issued.token.token_id
    assert str(token_id) in {i["token_id"] for i in resp.json()}

    resp = await api.delete(
        f"/api/v1/admin/devices/{device.device_id}/tokens/{token_id}", headers=headers
    )
    assert resp.status_code == 204
    await db_session.refresh(issued.token)
    assert issued.token.revoked_at is not None

    # 幂等：已吊销再删仍 204（更新 0 行不报错）
    resp = await api.delete(
        f"/api/v1/admin/devices/{device.device_id}/tokens/{token_id}", headers=headers
    )
    assert resp.status_code == 204

    # 凭证不属于该设备 → 404
    resp = await api.delete(
        f"/api/v1/admin/devices/{seed['devices']['single'].device_id}/tokens/{token_id}",
        headers=headers,
    )
    assert resp.status_code == 404

    # 凭证不存在 → 404
    resp = await api.delete(
        f"/api/v1/admin/devices/{device.device_id}/tokens/{uuid.uuid4()}", headers=headers
    )
    assert resp.status_code == 404


async def test_bind_device_validation_and_conflict_branches(api, seed):
    headers = await _admin_headers(api, seed)
    device = seed["devices"]["free"]

    resp = await api.put(
        f"/api/v1/devices/{device.device_id}/binding",
        json={"room_id": str(uuid.uuid4()), "bed_no": 1},
        headers=headers,
    )
    assert resp.status_code == 422

    # free 先占双人间 1 床，再让另一台设备抢同一床位 → 409
    resp = await api.put(
        f"/api/v1/devices/{device.device_id}/binding",
        json={"room_id": str(seed["rooms"]["double"].room_id), "bed_no": 1},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    resp = await api.put(
        f"/api/v1/devices/{seed['devices']['single'].device_id}/binding",
        json={"room_id": str(seed["rooms"]["double"].room_id), "bed_no": 1},
        headers=headers,
    )
    assert resp.status_code == 409


async def test_admin_create_duplicate_device_code_409(api, seed):
    headers = await _admin_headers(api, seed)
    resp = await api.post(
        "/api/v1/admin/devices",
        json={
            "device_code": "TAB-FREE",
            "room_id": str(seed["rooms"]["other"].room_id),
            "bed_no": 2,
        },
        headers=headers,
    )
    assert resp.status_code == 409


async def test_admin_delete_device_retires_and_revokes(api, seed, db_session):
    headers = await _admin_headers(api, seed)
    device = seed["devices"]["free"]
    issued = await issue_access_token(db_session, device.device_id)
    await db_session.commit()

    resp = await api.delete(f"/api/v1/admin/devices/{device.device_id}", headers=headers)
    assert resp.status_code == 204
    row = await _device_row(db_session, device.device_id)
    assert row.status == DeviceStatus.RETIRED
    await db_session.refresh(issued.token)
    assert issued.token.revoked_at is not None

    # 幂等：再删一次仍 204
    resp = await api.delete(f"/api/v1/admin/devices/{device.device_id}", headers=headers)
    assert resp.status_code == 204


async def test_revoke_all_tokens(api, seed, db_session):
    headers = await _admin_headers(api, seed)
    device = seed["devices"]["free"]
    first = await issue_access_token(db_session, device.device_id)
    second = await issue_access_token(db_session, device.device_id)
    await db_session.commit()

    resp = await api.post(
        f"/api/v1/admin/devices/{device.device_id}/tokens/revoke-all", headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["revoked_count"] == 2

    # 旧 Token 立即失效（设备自查 401）
    resp = await api.get(
        "/api/v1/devices/me", headers={"Authorization": f"Bearer {first.access_token}"}
    )
    assert resp.status_code == 401
    assert second.access_token != first.access_token

