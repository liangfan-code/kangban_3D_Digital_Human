"""设备注册凭证与设备身份测试（Day2 文档 6.4 test_device_auth.py 行）。"""

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy import update as sa_update

from app.models import DeviceToken
from app.models.enums import DeviceTokenKind
from tests.conftest import login


async def _issue_register_token(api, seed, device_key="single") -> dict:
    token = await login(api, "admin", seed["password"])
    device = seed["devices"][device_key]
    resp = await api.post(
        f"/api/v1/admin/devices/{device.device_id}/registration-tokens",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_register_token_issued_and_persisted(api, seed, db_session):
    body = await _issue_register_token(api, seed)
    # 回查数据库：register 凭证已入库、未吊销、明文不在库
    rows = (
        await db_session.execute(
            select(DeviceToken).where(
                DeviceToken.device_id == seed["devices"]["single"].device_id,
                DeviceToken.token_kind == DeviceTokenKind.REGISTER,
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].revoked_at is None
    assert rows[0].expires_at is not None
    assert body["registration_token"] not in rows[0].token_hash
    assert (
        hashlib.sha256(body["registration_token"].encode()).hexdigest() == rows[0].token_hash
    )


async def test_device_register_success_and_hash_persistence(api, seed, db_session):
    body = await _issue_register_token(api, seed)
    resp = await api.post(
        "/api/v1/auth/device/register",
        json={
            "device_code": "TAB-SINGLE",
            "registration_token": body["registration_token"],
        },
    )
    assert resp.status_code == 200, resp.text
    access_token = resp.json()["access_token"]
    # 回查：register 已吊销（一次性），access 已入库且只存哈希
    register_rows = (
        await db_session.execute(
            select(DeviceToken).where(
                DeviceToken.device_id == seed["devices"]["single"].device_id,
                DeviceToken.token_kind == DeviceTokenKind.REGISTER,
            )
        )
    ).scalars().all()
    assert all(r.revoked_at is not None for r in register_rows)

    access_rows = (
        await db_session.execute(
            select(DeviceToken).where(
                DeviceToken.device_id == seed["devices"]["single"].device_id,
                DeviceToken.token_kind == DeviceTokenKind.ACCESS,
            )
        )
    ).scalars().all()
    assert len(access_rows) == 1
    assert access_token not in [r.token_hash for r in access_rows]
    assert (
        hashlib.sha256(access_token.encode()).hexdigest() == access_rows[0].token_hash
    )


async def test_register_token_replay_401(api, seed):
    body = await _issue_register_token(api, seed)
    payload = {
        "device_code": "TAB-SINGLE",
        "registration_token": body["registration_token"],
    }
    first = await api.post("/api/v1/auth/device/register", json=payload)
    assert first.status_code == 200
    replay = await api.post("/api/v1/auth/device/register", json=payload)
    assert replay.status_code == 401


async def test_register_token_expired_401(api, seed, db_session):
    body = await _issue_register_token(api, seed)
    await db_session.execute(
        sa_update(DeviceToken)
        .where(
            DeviceToken.token_hash
            == hashlib.sha256(body["registration_token"].encode()).hexdigest()
        )
        .values(expires_at=datetime.now(UTC) - timedelta(hours=1))
    )
    resp = await api.post(
        "/api/v1/auth/device/register",
        json={
            "device_code": "TAB-SINGLE",
            "registration_token": body["registration_token"],
        },
    )
    assert resp.status_code == 401


async def test_me_device_identity(api, seed):
    body = await _issue_register_token(api, seed)
    reg = await api.post(
        "/api/v1/auth/device/register",
        json={
            "device_code": "TAB-SINGLE",
            "registration_token": body["registration_token"],
        },
    )
    token = reg.json()["access_token"]
    resp = await api.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["identity_type"] == "device"
    assert data["device_code"] == "TAB-SINGLE"
    assert str(data["device_id"]) == str(seed["devices"]["single"].device_id)


async def test_device_token_on_user_endpoint_403(api, seed):
    body = await _issue_register_token(api, seed)
    reg = await api.post(
        "/api/v1/auth/device/register",
        json={
            "device_code": "TAB-SINGLE",
            "registration_token": body["registration_token"],
        },
    )
    token = reg.json()["access_token"]
    resp = await api.get(
        "/api/v1/auth/me/user", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403


async def test_revoked_access_token_401(api, seed, db_session):
    body = await _issue_register_token(api, seed)
    reg = await api.post(
        "/api/v1/auth/device/register",
        json={
            "device_code": "TAB-SINGLE",
            "registration_token": body["registration_token"],
        },
    )
    token = reg.json()["access_token"]
    # 回查确认可用，再吊销
    ok = await api.get("/api/v1/devices/me", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200

    from datetime import UTC, datetime

    await db_session.execute(
        sa_update(DeviceToken)
        .where(
            DeviceToken.token_hash == hashlib.sha256(token.encode()).hexdigest(),
            DeviceToken.token_kind == DeviceTokenKind.ACCESS,
        )
        .values(revoked_at=datetime.now(UTC))
    )
    replay = await api.get(
        "/api/v1/devices/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert replay.status_code == 401
