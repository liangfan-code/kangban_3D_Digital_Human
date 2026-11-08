"""Day5.2 语音链路接线测试：LLM 超时降级、TTS 超时降级、熔断跳过真实调用。"""

import asyncio
from uuid import uuid4

import pytest

from app.core.config import get_settings
from app.services.latency import TurnTrace
from app.services.resilience import (
    CircuitState,
    get_llm_breaker,
    get_tts_breaker,
    reset_circuit_breakers,
)
from app.services.voice import VoiceSession
from tests.test_voiceprint_service import FakeTTSProvider


class RecordingTTSProvider(FakeTTSProvider):
    """带 Provider 上报字段的假 TTS：验证打点确实取自 Provider。"""

    def __init__(self) -> None:
        self.last_first_byte_ms = 88.0
        self.last_total_ms = 210.0
        self.last_decode_ms = 35.0


class SlowTTSProvider(FakeTTSProvider):
    async def synthesize(self, text: str) -> bytes:  # type: ignore[override]
        await asyncio.sleep(0.2)
        return b"\x01\x00" * 160


@pytest.fixture(autouse=True)
def _reset_breakers():
    """用例间隔离熔断单例，避免失败计数串扰。"""
    reset_circuit_breakers()
    yield
    reset_circuit_breakers()


def _patched_settings(monkeypatch, **updates):
    base = get_settings()
    monkeypatch.setattr(
        "app.services.voice.get_settings", lambda: base.model_copy(update=updates)
    )


def _session(events: list[dict]) -> VoiceSession:
    async def send_event(event: dict) -> None:
        events.append(event)

    return VoiceSession(uuid4(), [], {}, send_event)


async def test_llm_first_token_timeout_falls_back_to_template(monkeypatch):
    """首 token 超时 → 可见降级为模板回复；trace 标记超时与兜底。"""
    events: list[dict] = []
    session = _session(events)
    monkeypatch.setattr("app.services.voice.llm_is_configured", lambda: True)
    monkeypatch.setattr("app.services.voice.get_tts_provider", lambda: RecordingTTSProvider())
    _patched_settings(
        monkeypatch, llm_first_token_timeout_seconds=0.05, tts_timeout_seconds=5.0
    )

    async def slow_stream(messages):
        await asyncio.sleep(0.2)
        yield "迟到的回复。"

    monkeypatch.setattr("app.services.voice.stream_chat", slow_stream)

    trace = TurnTrace()
    reply = await session._reply_and_respond("你好", trace)

    payload = trace.payload()
    assert reply == "你刚才说的是：你好"
    assert "llm_first_token_timeout" in payload["degraded"]
    assert "llm_fallback" in payload["degraded"]
    assert payload["llm_first_token_ms"] is None  # 未拿到首 token
    assert any(event["type"] == "tts" for event in events)


async def test_llm_circuit_open_skips_real_call(monkeypatch):
    """熔断打开后不再调用 LLM，直接模板兜底（通话不断）。"""
    events: list[dict] = []
    session = _session(events)
    monkeypatch.setattr("app.services.voice.llm_is_configured", lambda: True)
    monkeypatch.setattr("app.services.voice.get_tts_provider", lambda: RecordingTTSProvider())

    called = False

    async def fast_stream(messages):
        nonlocal called
        called = True
        yield "不该被调用。"

    monkeypatch.setattr("app.services.voice.stream_chat", fast_stream)

    breaker = get_llm_breaker()
    for _ in range(3):
        with pytest.raises(RuntimeError):
            async with breaker.guard():
                raise RuntimeError("boom")
    assert breaker.snapshot()["state"] == CircuitState.OPEN

    trace = TurnTrace()
    reply = await session._reply_and_respond("你好", trace)

    payload = trace.payload()
    assert called is False
    assert reply == "你刚才说的是：你好"
    assert "llm_circuit_open" in payload["degraded"]
    assert "llm_fallback" in payload["degraded"]


async def test_tts_timeout_emits_visible_error_and_counts_failure(monkeypatch):
    """单句合成超时 → tts_failed 错误事件 + 熔断器记一次失败。"""
    events: list[dict] = []
    session = _session(events)
    monkeypatch.setattr("app.services.voice.get_tts_provider", lambda: SlowTTSProvider())
    _patched_settings(monkeypatch, tts_timeout_seconds=0.05)

    trace = TurnTrace()
    await session._synthesize_and_send("你好", trace)

    failures = [event for event in events if event["type"] == "error"]
    assert len(failures) == 1
    assert failures[0]["code"] == "tts_failed"
    assert failures[0]["reply_text"] == "你好"  # 文字兜底不能让长者无回复

    assert "tts_timeout" in trace.payload()["degraded"]
    assert get_tts_breaker().snapshot()["failures"] == 1
    assert get_tts_breaker().snapshot()["state"] == CircuitState.CLOSED


async def test_success_path_records_provider_durations(monkeypatch):
    """正常一轮：记录 Provider 首包/解码/总耗时与句数，无降级字段。"""
    events: list[dict] = []
    session = _session(events)
    monkeypatch.setattr("app.services.voice.llm_is_configured", lambda: True)
    monkeypatch.setattr("app.services.voice.get_tts_provider", lambda: RecordingTTSProvider())
    _patched_settings(monkeypatch, llm_first_token_timeout_seconds=5.0, tts_timeout_seconds=5.0)

    async def fast_stream(messages):
        yield "你好呀。"

    monkeypatch.setattr("app.services.voice.stream_chat", fast_stream)

    trace = TurnTrace()
    reply = await session._reply_and_respond("你好", trace)

    payload = trace.payload()
    assert reply == "你好呀。"
    assert "degraded" not in payload
    assert payload["llm_first_token_ms"] is not None
    assert payload["llm_total_ms"] is not None
    assert payload["tts_first_byte_ms"] == 88.0
    assert payload["tts_decode_ms"] == 35.0
    assert payload["tts_total_ms"] == 210.0
    assert payload["tts_sentences"] == 1
    assert payload["a2a_ms"] is not None
