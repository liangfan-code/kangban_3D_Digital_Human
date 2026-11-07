"""WS 事件协议状态机测试（mock ASR/TTS Provider，不依赖容器与模型）。

httpx 不支持 WebSocket，这里对 WS /voice/ws 所下发的事件序列做等价实测：
VoiceSession 正是 WS 事件流的唯一生产者（routes/voice.py 仅做鉴权与转发），
事件类型顺序 = WS 协议：verification → state → transcript → tts → done。
"""

import asyncio
import time
from uuid import uuid4

import numpy as np

from app.services.voice import VoiceSession, VoiceTokenStore
from tests.test_voiceprint_service import (
    FAKE_VEC_A,
    FakeASRSession,
    FakeTTSProvider,
    FakeVoiceprintProvider,
)


async def test_full_event_protocol_sequence(monkeypatch):
    """首句验证 + 第二句转写：事件类型序列与字段符合 WS 协议约定。"""
    provider = FakeVoiceprintProvider()
    monkeypatch.setattr("app.services.voice.get_voiceprint_provider", lambda: provider)
    monkeypatch.setattr("app.services.voice.get_tts_provider", lambda: FakeTTSProvider())
    fake_asr = FakeASRSession()
    monkeypatch.setattr("app.services.voice.create_asr_session", lambda: fake_asr)

    events: list[dict] = []

    async def send_event(event: dict) -> None:
        events.append(event)

    elder = uuid4()
    session = VoiceSession(
        uuid4(), [(elder, "张奶奶")], {elder: [FAKE_VEC_A]}, send_event
    )

    # 第一句：声纹门控
    session.feed_pcm(b"\x00\x00" * 16000 * 2)
    await session._flush_utterance()
    # 第二句：转写（免验）
    session.feed_pcm(b"\x00\x00" * 16000)
    await session._flush_utterance()

    types = [e["type"] for e in events]
    # verification(accepted=false 的不存在) → verification(token) → state → transcript → tts → done
    assert types[0] == "verification"
    assert events[0]["accepted"] is True
    assert events[0]["token"]
    assert "state" in types and types.index("state") < types.index("transcript")
    assert "transcript" in types and "tts" in types
    assert types[-1] == "done"
    # done 事件带最终文本与模板回复
    done = events[-1]
    assert done["text"] == "你好康伴"
    assert done["reply"] == "你刚才说的是：你好康伴"


async def test_verification_failure_event_shape(monkeypatch):
    """验证失败事件：accepted=false 且不发 token（协议字段完整性）。"""
    provider = FakeVoiceprintProvider()
    monkeypatch.setattr("app.services.voice.get_voiceprint_provider", lambda: provider)

    events: list[dict] = []

    async def send_event(event: dict) -> None:
        events.append(event)

    # 房间档案向量与说话人不匹配（threshold=0.99 必然失败）
    monkeypatch.setattr(
        "app.services.voice.get_settings",
        lambda: type("S", (), {"voiceprint_threshold": 0.99, "voiceprint_token_ttl": 300})(),
    )
    elder = uuid4()
    session = VoiceSession(
        uuid4(), [(elder, "李爷爷")], {elder: [[0.0, 1.0] + [0.0] * 190]}, send_event
    )
    session.feed_pcm(b"\x00\x00" * 16000 * 2)  # 偶数首字节 → A 向量 ≠ 房间 B 向量
    await session._flush_utterance()

    failures = [
        e for e in events if e["type"] == "verification" and e["accepted"] is False
    ]
    assert len(failures) == 1
    assert "token" not in failures[0]
    assert not any(e["type"] == "state" and e.get("state") == "verified" for e in events)


def test_token_ttl_default_300s():
    """免验 Token 默认 300 秒（验收项 9 前提）。"""
    store = VoiceTokenStore()
    _, ttl = store.issue(uuid4(), uuid4(), 0.9)
    assert ttl == 300


# ---- Day4 ASR 专项：VAD 收紧 + TTS 回声防护窗 ----

FRAME_60MS = np.full(960, 2000, dtype=np.int16).tobytes()  # RMS 2000 > 阈值 500
SILENT_60MS = np.zeros(960, dtype=np.int16).tobytes()


def _make_session() -> VoiceSession:
    elder = uuid4()
    return VoiceSession(uuid4(), [(elder, "张奶奶")], {elder: [FAKE_VEC_A]}, _noop_send)


async def _noop_send(event: dict) -> None:
    return None


async def test_endpoint_discards_short_noise_burst():
    """短促噪声段（<180ms 起始 + 有效语音 <500ms）静默丢弃，不送 ASR。

    Day4 实测事故：此类段是 FunASR「对对对」伪终句的主要来源。
    """
    session = _make_session()
    flushed = []

    async def _record(force: bool = False):
        flushed.append(force)

    session._flush_utterance = _record  # type: ignore[method-assign]
    for _ in range(2):  # 120ms 有能量：不足起始判定
        session.feed_pcm(FRAME_60MS)
    for _ in range(15):  # 900ms 静音：超过关句阈值
        session.feed_pcm(SILENT_60MS)
    await asyncio.sleep(0)
    assert flushed == []  # 绝不进 ASR
    assert session._voiced_ms == 0  # 计数已复位
    assert max(session.buffer, default=0) == 0  # 缓冲只残留静音帧
    assert session._speech_seen is False


async def test_endpoint_flushes_sustained_speech():
    """连续 600ms 语音 + 800ms 静音：正常判定整句结束并送 ASR（功能不回退）。"""
    session = _make_session()
    handled: list[tuple[bytes, bool]] = []

    async def _record(pcm: bytes, force: bool = False):
        handled.append((pcm, force))

    session._handle_utterance = _record  # type: ignore[method-assign]
    for _ in range(10):  # 600ms 语音
        session.feed_pcm(FRAME_60MS)
    for _ in range(15):  # 900ms 静音
        session.feed_pcm(SILENT_60MS)
    await asyncio.sleep(0)
    assert len(handled) == 1  # 恰好一次整句（不重复调度）
    assert handled[0][1] is False


async def test_echo_guard_drops_uplink_during_tts():
    """TTS 回声防护窗内上行音频直接丢弃（数字人朗读不得被回拾成转写）。"""
    session = _make_session()
    session._echo_guard_until = time.monotonic() + 10
    session.feed_pcm(FRAME_60MS * 1)
    assert len(session.buffer) == 0


async def test_tts_send_sets_echo_guard(monkeypatch):
    """下发 TTS 后按播放时长+余量设置防护窗（FakeTTS 返回 10ms 音频 → 窗 ≈1510ms）。"""
    from app.services.voice import TTS_ECHO_GUARD_EXTRA_MS

    session = _make_session()
    fake_tts = FakeTTSProvider()
    monkeypatch.setattr("app.services.voice.get_tts_provider", lambda: fake_tts)
    duration_ms = len(await fake_tts.synthesize("你好")) / (16000 * 2) * 1000
    await session._synthesize_and_send("你好")
    expected = duration_ms + TTS_ECHO_GUARD_EXTRA_MS
    remaining_ms = (session._echo_guard_until - time.monotonic()) * 1000
    assert expected - 100 <= remaining_ms <= expected + 100
