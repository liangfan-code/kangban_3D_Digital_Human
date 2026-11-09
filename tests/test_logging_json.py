"""Day5.2 结构化日志字段白名单测试：延迟/熔断字段必须进入 JSON 输出。"""

import json
import logging

from app.core.logging import JsonFormatter


def _format(**fields) -> dict:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "evt", None, None)
    for key, value in fields.items():
        setattr(record, key, value)
    return json.loads(JsonFormatter().format(record))


def test_turn_latency_fields_are_serialized():
    """turn_latency 的时长字段不能被 JsonFormatter 丢弃（否则基线表无数据）。"""
    payload = _format(
        event="turn_latency",
        turn_id="abc12345",
        a2a_ms=1234.5,
        llm_first_token_ms=800.0,
        tts_sentences=2,
        degraded="llm_fallback,tts_timeout",
        device_id="dev-1",
    )
    assert payload["event"] == "turn_latency"
    assert payload["a2a_ms"] == 1234.5
    assert payload["llm_first_token_ms"] == 800.0
    assert payload["tts_sentences"] == 2
    assert payload["degraded"] == "llm_fallback,tts_timeout"
    assert payload["device_id"] == "dev-1"


def test_circuit_and_provider_fields_are_serialized():
    """熔断状态迁移与 Provider 元数据字段同样需要可观测。"""
    payload = _format(
        event="circuit_state_changed",
        provider="llm",
        **{"from": "closed"},
        to="open",
        failure_threshold=3,
        open_seconds=30.0,
    )
    assert payload["provider"] == "llm"
    assert payload["from"] == "closed"
    assert payload["to"] == "open"
    assert payload["failure_threshold"] == 3


def test_unlisted_extra_is_not_serialized():
    """白名单外字段（如误传正文）不得进入 JSON 日志，守住隐私边界。"""
    payload = _format(event="turn_latency", raw_text="不该出现的转写正文")
    assert "raw_text" not in payload
