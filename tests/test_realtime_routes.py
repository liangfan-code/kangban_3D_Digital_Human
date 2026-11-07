"""/rtc/offer 权限与参数校验测试（不连真实 WebRTC，验收项 11）。"""

from tests.conftest import login


async def _device_token(api, seed, device_key="single") -> str:
    """走完整注册流取设备 access Token。"""
    token = await login(api, "admin", seed["password"])
    device = seed["devices"][device_key]
    resp = await api.post(
        f"/api/v1/admin/devices/{device.device_id}/registration-tokens",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    reg = resp.json()["registration_token"]
    resp = await api.post(
        "/api/v1/auth/device/register",
        json={"device_code": device.device_code, "registration_token": reg},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def test_rtc_offer_without_token_401(api):
    resp = await api.post(
        "/api/v1/rtc/offer", json={"sdp": "v=0", "type": "offer"}
    )
    assert resp.status_code == 401


async def test_rtc_offer_with_human_token_403(api, seed):
    """人 JWT 调 /rtc/offer 必须 403（token_type 互斥，验收项 11）。"""
    token = await login(api, "admin", seed["password"])
    resp = await api.post(
        "/api/v1/rtc/offer",
        json={"sdp": "v=0", "type": "offer"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_rtc_offer_missing_fields_422(api, seed):
    """设备 Token 合法但 SDP offer 缺字段 → 参数校验 422（不建 PC）。"""
    device = await _device_token(api, seed)
    resp = await api.post(
        "/api/v1/rtc/offer",
        json={"sdp": "v=0"},
        headers={"Authorization": f"Bearer {device}"},
    )
    assert resp.status_code == 422


async def test_enroll_device_rejects_elder_id_param(api, seed):
    """设备 enroll 通道不接受 elder_id（身份服务端确定，验收项 11）。"""
    device = await _device_token(api, seed)
    resp = await api.post(
        "/api/v1/voice/enroll/device",
        json={
            "elder_id": "00000000-0000-0000-0000-000000000000",
            "audio_samples": ["AAAA"],
        },
        headers={"Authorization": f"Bearer {device}"},
    )
    assert resp.status_code == 403


async def test_enroll_admin_channel_validation(api, seed):
    """管理员通道：缺 elder_id → 422；样本过短 → 422；elder 不存在 → 404。

    样本长度校验在路由层先于身份/模型校验，故必须给足合法长度（≥0.1s PCM）
    才能触达 elder 查找。"""
    import base64

    admin = await login(api, "admin", seed["password"])
    resp = await api.post(
        "/api/v1/voice/enroll/admin",
        json={"audio_samples": ["AAAA"]},
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert resp.status_code == 422  # 缺 elder_id

    valid_sample = base64.b64encode(b"\x00\x00" * 1600).decode()  # 0.1s 16k PCM
    resp = await api.post(
        "/api/v1/voice/enroll/admin",
        json={
            "elder_id": "00000000-0000-0000-0000-000000000000",
            "audio_samples": [valid_sample],
        },
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert resp.status_code == 404


async def test_voiceprints_query_requires_admin(api, seed):
    """声纹查询仅管理员：护工/家属 → 403（不入前端普通页面）。"""
    for role in ("nurse_a", "family"):
        token = await login(api, role, seed["password"])
        resp = await api.get(
            f"/api/v1/voice/voiceprints/{seed['elders']['single'].elder_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403
