"""生物识别演示登录测试（登录页"老人生物识别进入"）。

覆盖：房间列表（无需登录）、指纹演示直进（签发真实设备 Token 且可通过
/devices/me 校验）、声纹真比对命中/不匹配/未录入、开关关闭一律 403、
房间无活跃设备 409。声纹用确定性假 Provider（同 test_voiceprint_service）。
"""

import base64

import pytest

from app.services.voice import enroll_voiceprint

# 与 test_voiceprint_service 相同的确定性假向量协议：首字节偶数→A，奇数→B
SAMPLE_EVEN = bytes([0]) * 6400
SAMPLE_ODD = bytes([3]) * 6400  # B 与 A 正交，cosine=0


@pytest.fixture
def fake_provider(monkeypatch):
    class FakeVoiceprintProvider:
        def extract_embedding(self, audio_16k: bytes) -> list[float]:
            if len(audio_16k) < 3200:
                raise ValueError("有效语音不足，无法提取声纹")
            return [1.0] + [0.0] * 191 if audio_16k[0] % 2 == 0 else [0.0, 1.0] + [0.0] * 190

        @staticmethod
        def cosine(a: list[float], b: list[float]) -> float:
            return sum(x * y for x, y in zip(a, b))

        def model_fingerprint(self) -> str:
            return "fake-fingerprint-0123456789abcdef"

    monkeypatch.setattr(
        "app.services.voice.get_voiceprint_provider", lambda: FakeVoiceprintProvider()
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


async def test_rooms_list_no_auth_needed(api, seed):
    """房间列表无需登录（登录页使用），含在住长者与活跃设备信息。"""
    resp = await api.get("/api/v1/voice/biometric/rooms")
    assert resp.status_code == 200
    by_label = {i["label"]: i for i in resp.json()["items"]}
    single = by_label["A-1F-101"]
    assert single["device_code"] == "TAB-SINGLE"
    assert single["elders"] == ["王单间"]
    double = by_label["A-1F-102"]
    assert set(double["elders"]) == {"李双一", "赵双二"}
    assert double["device_code"] == "TAB-DOUBLE"


async def test_fingerprint_login_issues_real_device_token(api, seed):
    """指纹演示直进：签发真实设备 Token，且能通过 /devices/me 校验。"""
    resp = await api.post(
        "/api/v1/voice/biometric/login",
        json={"room_id": str(seed["rooms"]["single"].room_id), "mode": "fingerprint"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["device_code"] == "TAB-SINGLE"
    assert body["matched_elder"] is None
    assert body["access_token"]

    me = await api.get(
        "/api/v1/devices/me",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert me.status_code == 200
    assert me.json()["device_code"] == "TAB-SINGLE"


async def test_fingerprint_mode_rejects_audio(api, seed):
    """fingerprint 模式不接受音频样本（防协议混用）。"""
    resp = await api.post(
        "/api/v1/voice/biometric/login",
        json={
            "room_id": str(seed["rooms"]["single"].room_id),
            "mode": "fingerprint",
            "audio_samples": [_b64(SAMPLE_EVEN)],
        },
    )
    assert resp.status_code == 422


async def test_voiceprint_login_match(api, seed, db_session, fake_provider):
    """声纹真比对：命中在住长者声纹 → 放行并返回匹配者与分数。"""
    await enroll_voiceprint(
        db_session,
        elder_id=seed["elders"]["single"].elder_id,
        audio_samples=[SAMPLE_EVEN],
    )
    resp = await api.post(
        "/api/v1/voice/biometric/login",
        json={
            "room_id": str(seed["rooms"]["single"].room_id),
            "mode": "voiceprint",
            "audio_samples": [_b64(SAMPLE_EVEN)],  # 与录入同向量 → cosine=1.0
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["matched_elder"] == "王单间"
    assert body["score"] == 1.0
    assert body["access_token"]


async def test_voiceprint_login_rejected(api, seed, db_session, fake_provider):
    """声纹不匹配（正交向量 cosine=0 < 0.55）→ 422。"""
    await enroll_voiceprint(
        db_session,
        elder_id=seed["elders"]["single"].elder_id,
        audio_samples=[SAMPLE_EVEN],
    )
    resp = await api.post(
        "/api/v1/voice/biometric/login",
        json={
            "room_id": str(seed["rooms"]["single"].room_id),
            "mode": "voiceprint",
            "audio_samples": [_b64(SAMPLE_ODD)],
        },
    )
    assert resp.status_code == 422
    assert "声纹不匹配" in resp.json()["error"]["message"]


async def test_voiceprint_without_enrollment_hint(api, seed, fake_provider):
    """房间长者未录过声纹 → 422 并提示走管理员通道录入。"""
    resp = await api.post(
        "/api/v1/voice/biometric/login",
        json={
            "room_id": str(seed["rooms"]["single"].room_id),
            "mode": "voiceprint",
            "audio_samples": [_b64(SAMPLE_EVEN)],
        },
    )
    assert resp.status_code == 422
    assert "尚未录入声纹" in resp.json()["error"]["message"]


async def test_biometric_disabled_returns_403(api, seed, monkeypatch):
    """开关关闭：列表与登录一律 403（恢复严格设备 Token 鉴权）。"""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "biometric_demo_enabled", False)
    resp = await api.get("/api/v1/voice/biometric/rooms")
    assert resp.status_code == 403
    resp = await api.post(
        "/api/v1/voice/biometric/login",
        json={"room_id": str(seed["rooms"]["single"].room_id), "mode": "fingerprint"},
    )
    assert resp.status_code == 403


async def test_room_without_device_returns_409(api, seed, db_session):
    """房间无活跃设备 → 409（房间存在但进不去）。"""
    from app.models import Room

    empty = Room(building_code="A", floor=9, room_number="901", bed_count=1)
    db_session.add(empty)
    await db_session.flush()
    resp = await api.post(
        "/api/v1/voice/biometric/login",
        json={"room_id": str(empty.room_id), "mode": "fingerprint"},
    )
    assert resp.status_code == 409


async def test_room_not_found_returns_404(api, seed):
    """房间不存在 → 404。"""
    from uuid import uuid4

    resp = await api.post(
        "/api/v1/voice/biometric/login",
        json={"room_id": str(uuid4()), "mode": "fingerprint"},
    )
    assert resp.status_code == 404
