"""Day5.2 单轮延迟打点测试：字段口径、缺失值语义、降级汇总、只输出一次。"""

import asyncio

from pytest import approx

from app.services.latency import TurnTrace

_EXPECTED_KEYS = {
    "turn_id",
    "turn_total_ms",
    "asr_connect_ms",
    "asr_e2e_ms",
    "llm_first_token_ms",
    "llm_total_ms",
    "a2a_ms",
    "viseme_ms",
    "tts_first_byte_ms",
    "tts_total_ms",
    "tts_decode_ms",
    "tts_sentences",
}


def test_payload_has_all_stages_and_missing_is_none():
    """未发生的阶段输出 None（区分「未发生」与「0ms」），字段集合稳定。"""
    trace = TurnTrace()
    payload = trace.payload()
    assert _EXPECTED_KEYS <= set(payload)
    for key in _EXPECTED_KEYS - {"turn_id", "tts_sentences"}:
        assert payload[key] is None


async def test_spans_computed_from_marks():
    """阶段耗时 = 相邻打点差；turn_total 以轮次开始为基准。"""
    trace = TurnTrace()
    await asyncio.sleep(0.01)  # 建连耗时（避免与轮次开始同一分段）
    trace.mark("asr_ready")
    await asyncio.sleep(0.02)
    trace.mark("asr_final")
    trace.mark("done")

    payload = trace.payload()
    assert payload["asr_connect_ms"] > 0
    assert payload["asr_e2e_ms"] == approx(20, abs=15)
    assert payload["turn_total_ms"] > 0


def test_mark_once_keeps_first_tts_stage():
    """多句 TTS：首包/首句事件只保留第一次打点。"""
    trace = TurnTrace()
    trace.mark_once("tts_start")
    trace.mark_once("tts_event_sent")
    first_start = trace._marks["tts_start"]
    first_sent = trace._marks["tts_event_sent"]

    trace.mark_once("tts_start")
    trace.mark_once("tts_event_sent")
    assert trace._marks["tts_start"] == first_start
    assert trace._marks["tts_event_sent"] == first_sent


def test_durations_accumulate_and_first_value_wins():
    """Provider 耗时：首句值只记一次，总耗时逐句累加。"""
    trace = TurnTrace()
    trace.set_duration_once("tts_first_byte_ms", 120.4)
    trace.set_duration_once("tts_first_byte_ms", 300.0)  # 后续句子不覆盖
    trace.add_duration("tts_total_ms", 200.0)
    trace.add_duration("tts_total_ms", 150.0)
    trace.add_duration("viseme_ms", None)  # 缺失上报直接忽略

    payload = trace.payload()
    assert payload["tts_first_byte_ms"] == 120.4
    assert payload["tts_total_ms"] == 350.0
    assert payload["viseme_ms"] is None
    assert payload["tts_sentences"] is None


def test_degrade_and_sentence_count():
    """降级原因去重后以逗号拼接；句数按次累加。"""
    trace = TurnTrace()
    trace.degrade("llm_fallback")
    trace.degrade("llm_fallback")
    trace.degrade("tts_timeout")
    trace.incr("tts_sentences")
    trace.incr("tts_sentences")

    payload = trace.payload()
    assert payload["degraded"] == "llm_fallback,tts_timeout"
    assert payload["tts_sentences"] == 2


def test_meta_merged_into_payload():
    """meta 只接受非空值，并合并进日志字段。"""
    trace = TurnTrace()
    trace.set_meta("device_id", "dev-1")
    trace.set_meta("elder_id", None)
    payload = trace.payload()
    assert payload["device_id"] == "dev-1"
    assert "elder_id" not in payload


def test_log_writes_turn_latency_once(monkeypatch):
    """同一轮只输出一条 turn_latency；字段完整且带 event 名。"""
    calls: list[tuple[str, dict]] = []

    class _FakeLogger:
        def info(self, message, extra=None):
            calls.append((message, extra or {}))

    monkeypatch.setattr("app.services.latency.logger", _FakeLogger())

    trace = TurnTrace()
    trace.set_meta("device_id", "dev-1")
    trace.degrade("empty_transcript")
    trace.mark("done")
    trace.log()
    trace.log()  # 幂等：不重复写

    assert len(calls) == 1
    message, extra = calls[0]
    assert message == "turn_latency"
    assert extra["event"] == "turn_latency"
    assert extra["device_id"] == "dev-1"
    assert extra["degraded"] == "empty_transcript"
    assert extra["turn_id"]
