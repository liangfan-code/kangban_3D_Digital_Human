"""设备绑定与设备自查测试（Day2 文档 6.4 test_device_binding.py 行）。"""

from sqlalchemy import select

from app.models import Device
from tests.conftest import login


async def _admin_headers(api, seed) -> dict:
    return {"Authorization": f"Bearer {await login(api, 'admin', seed['password'])}"}


async def _bind(api, seed, device_key, room_key, bed_no, headers):
    return await api.put(
        f"/api/v1/devices/{seed['devices'][device_key].device_id}/binding",
        json={"room_id": str(seed["rooms"][room_key].room_id), "bed_no": bed_no},
        headers=headers,
    )


async def _device_row(db_session, device_id) -> Device:
    return (
        await db_session.execute(select(Device).where(Device.device_id == device_id))
    ).scalar_one()


async def _device_token(api, seed, device_key) -> str:
    admin = await _admin_headers(api, seed)
    device = seed["devices"][device_key]
    issued = await api.post(
        f"/api/v1/admin/devices/{device.device_id}/registration-tokens",
        headers=admin,
    )
    assert issued.status_code == 200, issued.text
    reg = await api.post(
        "/api/v1/auth/device/register",
        json={
            "device_code": device.device_code,
            "registration_token": issued.json()["registration_token"],
        },
    )
    assert reg.status_code == 200, reg.text
    return reg.json()["access_token"]


async def test_bind_device_200_and_db_check(api, seed, db_session):
    headers = await _admin_headers(api, seed)
    resp = await _bind(api, seed, "free", "double", 2, headers)
    assert resp.status_code == 200, resp.text
    # 回查数据库：绑定已落库
    row = await _device_row(db_session, seed["devices"]["free"].device_id)
    assert str(row.room_id) == str(seed["rooms"]["double"].room_id)
    assert row.bed_no == 2


async def test_bind_device_bed_conflict_409(api, seed, db_session):
    headers = await _admin_headers(api, seed)
    first = await _bind(api, seed, "free", "double", 2, headers)
    assert first.status_code == 200
    second = await _bind(api, seed, "double", "double", 2, headers)
    assert second.status_code == 409
    # 回查：床位仍归 first 设备
    row = await _device_row(db_session, seed["devices"]["double"].device_id)
    assert row.bed_no != 2


async def test_bind_device_bed_over_capacity_422(api, seed):
    headers = await _admin_headers(api, seed)
    resp = await _bind(api, seed, "free", "single", 2, headers)
    assert resp.status_code == 422


async def test_bind_device_room_maintenance_422(api, seed, db_session):
    seed["rooms"]["other"].status = "maintenance"
    await db_session.flush()
    headers = await _admin_headers(api, seed)
    resp = await _bind(api, seed, "free", "other", 1, headers)
    assert resp.status_code == 422


async def test_bind_device_requires_admin_403(api, seed):
    headers = {
        "Authorization": f"Bearer {await login(api, 'nurse_a', seed['password'])}"
    }
    resp = await _bind(api, seed, "free", "double", 2, headers)
    assert resp.status_code == 403


async def test_device_me_single_room_locked(api, seed):
    token = await _device_token(api, seed, "single")
    resp = await api.get(
        "/api/v1/devices/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "single"
    assert body["elder"]["elder_id"] == str(seed["elders"]["single"].elder_id)
    assert body["elder"]["bed_no"] == 1
    assert body["candidates"] == []


async def test_device_me_double_room_candidates(api, seed):
    token = await _device_token(api, seed, "double")
    resp = await api.get(
        "/api/v1/devices/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "double"
    assert body["elder"] is None
    assert {c["elder_id"] for c in body["candidates"]} == {
        str(seed["elders"]["d1"].elder_id),
        str(seed["elders"]["d2"].elder_id),
    }


async def test_device_me_without_token_401(api):
    assert (await api.get("/api/v1/devices/me")).status_code == 401
