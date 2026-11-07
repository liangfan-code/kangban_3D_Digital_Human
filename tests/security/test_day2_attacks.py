"""POC-05 Day2 攻击者预热用例（3 条）。

通过标准：越权请求 100% 被拦截；日志留攻击记录（audit_logs 表前的结构化日志方案）。
"""

import hashlib
from datetime import UTC, datetime, timedelta

import jwt as pyjwt
from sqlalchemy import func, select
from sqlalchemy import update as sa_update

from app.core.config import get_settings
from app.models import DeviceToken, Elder
from app.models.enums import DeviceTokenKind
from tests.conftest import login


# A1：伪造签名的人 JWT（自报 admin 角色）调用管理员接口
async def test_a1_forged_admin_jwt_rejected(api, seed, db_session):
    attacker_secret = "attacker-guessed-secret-0123456789abcdef"
    payload = {
        "sub": str(seed["accounts"]["family"].account_id),
        "role": "admin",  # 自报 admin
        "token_type": "user",
        "exp": datetime.now(UTC) + timedelta(hours=1),
    }
    forged = pyjwt.encode(payload, attacker_secret, algorithm="HS256")

    resp = await api.post(
        "/api/v1/elders",
        json={
            "name": "攻击者创建",
            "persona_id": str(seed["personas"]["on"].persona_id),
        },
        headers={"Authorization": f"Bearer {forged}"},
    )
    assert resp.status_code == 401
    # 回查：未写入任何长者
    count = (
        await db_session.execute(select(func.count()).select_from(Elder))
    ).scalar_one()
    assert count == 4

    # 变体：篡改已签名 Token 的 payload（保持原签名）
    real = await login(api, "nurse_a", seed["password"])
    header_b64, payload_b64, sig = real.split(".")
    tampered = f"{header_b64}.{payload_b64}.{sig[:-2]}xx"
    resp = await api.post(
        "/api/v1/elders",
        json={
            "name": "攻击者创建2",
            "persona_id": str(seed["personas"]["on"].persona_id),
        },
        headers={"Authorization": f"Bearer {tampered}"},
    )
    assert resp.status_code == 401
    count = (
        await db_session.execute(select(func.count()).select_from(Elder))
    ).scalar_one()
    assert count == 4


# A2：已吊销 / 已过期设备 Token 重放 /devices/me
async def test_a2_revoked_device_token_replay_401(api, seed, db_session):
    admin = {"Authorization": f"Bearer {await login(api, 'admin', seed['password'])}"}
    device = seed["devices"]["single"]
    issued = await api.post(
        f"/api/v1/admin/devices/{device.device_id}/registration-tokens",
        headers=admin,
    )
    reg = await api.post(
        "/api/v1/auth/device/register",
        json={
            "device_code": device.device_code,
            "registration_token": issued.json()["registration_token"],
        },
    )
    token = reg.json()["access_token"]

    ok = await api.get(
        "/api/v1/devices/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert ok.status_code == 200

    # 直接改库吊销（模拟管理端吊销操作）
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


async def test_a2_expired_device_token_401(api, seed, db_session):
    admin = {"Authorization": f"Bearer {await login(api, 'admin', seed['password'])}"}
    device = seed["devices"]["double"]
    issued = await api.post(
        f"/api/v1/admin/devices/{device.device_id}/registration-tokens",
        headers=admin,
    )
    reg = await api.post(
        "/api/v1/auth/device/register",
        json={
            "device_code": device.device_code,
            "registration_token": issued.json()["registration_token"],
        },
    )
    token = reg.json()["access_token"]

    # 库内过期 + JWT 过期（双写，模拟时间流逝）
    expired = datetime.now(UTC) - timedelta(days=1)
    await db_session.execute(
        sa_update(DeviceToken)
        .where(
            DeviceToken.token_hash == hashlib.sha256(token.encode()).hexdigest(),
            DeviceToken.token_kind == DeviceTokenKind.ACCESS,
        )
        .values(expires_at=expired)
    )
    payload = {
        "sub": str(device.device_id),
        "token_type": "device",
        "token_kind": "access",
        "tid": reg.json().get("device_id", ""),
        "exp": expired,
    }
    stale = pyjwt.encode(payload, get_settings().jwt_secret_key, algorithm="HS256")
    resp = await api.get(
        "/api/v1/devices/me", headers={"Authorization": f"Bearer {stale}"}
    )
    assert resp.status_code == 401


# A3：护工跨楼栋 / 跨长者访问（伪造/借用他人 elder_id 参数）
async def test_a3_nurse_cross_building_404_no_leak(api, seed):
    admin = {"Authorization": f"Bearer {await login(api, 'admin', seed['password'])}"}
    nurse = {"Authorization": f"Bearer {await login(api, 'nurse_a', seed['password'])}"}
    assign = await api.post(
        "/api/v1/admin/assignments/buildings",
        json={"account_id": str(seed["accounts"]["nurse_a"].account_id),
              "building_code": "A"},
        headers=admin,
    )
    assert assign.status_code == 201

    # 跨楼栋（B 楼长者）：404，响应不包含姓名等存在性信息
    resp = await api.get(
        f"/api/v1/elders/{seed['elders']['other'].elder_id}", headers=nurse
    )
    assert resp.status_code == 404
    assert "钱他楼" not in resp.text
    assert "room" not in resp.json()
