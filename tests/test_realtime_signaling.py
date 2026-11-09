"""Day5.4 实时信令补测：不依赖真实 WebRTC，用 fake PC/Track 覆盖会话与注册表分支。"""

import json
from types import SimpleNamespace

import av
import pytest
from aiortc.mediastreams import MediaStreamError

from app.realtime import routes as rt_routes
from app.realtime import session as rt_session
from app.realtime.session import RealtimeRegistry, VoiceRealtimeSession, _rtc_configuration
from tests.conftest import login

# ---- ICE 配置 ----


def test_rtc_configuration_empty(monkeypatch):
    monkeypatch.setattr(
        rt_session, "get_settings", lambda: SimpleNamespace(rtc_ice_servers=None)
    )
    config = _rtc_configuration()
    assert config.iceServers == []


def test_rtc_configuration_valid_json(monkeypatch):
    servers = [{"urls": "stun:stun.example.com:3478"}]
    monkeypatch.setattr(
        rt_session, "get_settings", lambda: SimpleNamespace(rtc_ice_servers=json.dumps(servers))
    )
    config = _rtc_configuration()
    assert len(config.iceServers) == 1


def test_rtc_configuration_invalid_json_falls_back(monkeypatch, caplog):
    monkeypatch.setattr(
        rt_session, "get_settings", lambda: SimpleNamespace(rtc_ice_servers="{not-json")
    )
    config = _rtc_configuration()
    assert config.iceServers == []
    assert any(
        getattr(r, "event", None) == "rtc_ice_servers_invalid" for r in caplog.records
    )


# ---- 注册表 ----


class FakeSession:
    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


async def test_registry_replace_closes_old_session():
    registry = RealtimeRegistry()
    first, second = FakeSession(), FakeSession()
    await registry.replace("d1", first)
    await registry.replace("d1", second)
    assert first.closed == 1
    assert registry._sessions["d1"] is second

    await registry.close("d1")
    assert second.closed == 1
    assert "d1" not in registry._sessions

    # 关闭不存在的会话必须幂等
    await registry.close("d1")


# ---- accept_offer / close（fake PC） ----


class FakeTransceiver:
    kind = "audio"
    direction = "sendrecv"


class FakePC:
    connectionState = "new"
    localDescription = None

    def __init__(self) -> None:
        self.transceiver = FakeTransceiver()
        self.closed = False

    def getTransceivers(self):
        return [self.transceiver]

    async def setRemoteDescription(self, description):
        self.remote = description

    async def createAnswer(self):
        return "answer"

    async def setLocalDescription(self, answer):
        self.localDescription = SimpleNamespace(sdp="answer-sdp", type="answer")

    async def close(self):
        self.closed = True


def _bare_session() -> VoiceRealtimeSession:
    session = object.__new__(VoiceRealtimeSession)
    session.device_id = "dev-1"
    session.pc = FakePC()
    session._push_task = None
    session._resampler = None
    session._pcm_tail = bytearray()
    return session


async def test_accept_offer_downgrades_to_recvonly():
    session = _bare_session()
    answer = await session.accept_offer("v=0", "offer")
    assert session.pc.transceiver.direction == "recvonly"
    assert answer.type == "answer"
    assert answer.sdp == "answer-sdp"
    await session.close()
    assert session.pc.closed


# ---- _push_audio 分帧推送（fake Track + 真实重采样器） ----


class FakeTrack:
    def __init__(self, frames: list) -> None:
        self._frames = list(frames)

    async def recv(self):
        if not self._frames:
            raise MediaStreamError
        return self._frames.pop(0)


def _audio_frame(samples: int = 4800) -> av.AudioFrame:
    frame = av.AudioFrame(format="s16", layout="stereo", samples=samples)
    frame.sample_rate = 48000
    return frame


async def test_push_audio_resamples_and_feeds_pcm(monkeypatch):
    fed: list[bytes] = []
    fake_manager = SimpleNamespace(feed_pcm=lambda device_id, pcm: fed.append(pcm))
    monkeypatch.setattr(rt_session, "voice_session_manager", fake_manager)

    session = _bare_session()
    with pytest.raises(MediaStreamError):
        await session._push_audio(FakeTrack([_audio_frame(), _audio_frame()]))

    # 48k/stereo 100ms → 16k/mono 1600 样本，按 960 样本（60ms）切帧
    assert len(fed) >= 2
    assert all(len(pcm) == 960 * 2 for pcm in fed)


# ---- /rtc/offer 路由（fake VoiceRealtimeSession，验证替换语义） ----


async def _device_token(api, seed, device_key="single") -> str:
    token = await login(api, "admin", seed["password"])
    device = seed["devices"][device_key]
    resp = await api.post(
        f"/api/v1/admin/devices/{device.device_id}/registration-tokens",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    resp = await api.post(
        "/api/v1/auth/device/register",
        json={
            "device_code": device.device_code,
            "registration_token": resp.json()["registration_token"],
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def test_rtc_offer_replaces_previous_session(api, seed, monkeypatch):
    created: list = []

    class FakeRtcSession:
        def __init__(self, device_id) -> None:
            self.device_id = device_id
            self.closed = 0
            created.append(self)

        async def accept_offer(self, sdp: str, type_: str):
            return rt_routes.RtcAnswerOut(sdp="answer-sdp", type="answer")

        async def close(self) -> None:
            self.closed += 1

    monkeypatch.setattr(rt_routes, "VoiceRealtimeSession", FakeRtcSession)
    headers = {"Authorization": f"Bearer {await _device_token(api, seed)}"}
    device_id = seed["devices"]["single"].device_id

    first = await api.post(
        "/api/v1/rtc/offer", json={"sdp": "v=0", "type": "offer"}, headers=headers
    )
    assert first.status_code == 200 and first.json()["sdp"] == "answer-sdp"
    second = await api.post(
        "/api/v1/rtc/offer", json={"sdp": "v=0", "type": "offer"}, headers=headers
    )
    assert second.status_code == 200

    assert len(created) == 2
    assert created[0].closed == 1
    assert rt_routes.realtime_registry._sessions[device_id] is created[1]

    # 清理全局注册表，避免影响其他用例
    await rt_routes.realtime_registry.close(device_id)
    assert created[1].closed == 1
