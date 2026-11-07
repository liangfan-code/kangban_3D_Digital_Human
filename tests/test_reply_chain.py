"""Day4 回复链测试：分句器、LLM 流式事件顺序、模板降级与部分失败沿用。

LLM/TTS/DB 全部注入假实现，不出网、不连库。
"""

import uuid

import pytest

from app.integrations.llm import LlmUnavailable
from app.services.voice import VoiceSession, _SentenceAssembler


class _FakeTTS:
    def sample_rate(self) -> int:
        return 16000

    async def synthesize(self, text: str) -> bytes:
        return b"\x01\x00" * 16


@pytest.fixture(autouse=True)
def _fake_tts(monkeypatch):
    monkeypatch.setattr("app.services.voice.get_tts_provider", lambda: _FakeTTS())


def make_session(events: list[dict]) -> VoiceSession:
    async def collect(event: dict) -> None:
        events.append(event)

    return VoiceSession(
        device_id=uuid.uuid4(),
        candidates=[],
        embeddings={},
        send_event=collect,
        db_factory=None,
    )


# ---------- _SentenceAssembler ----------


def test_assembler_splits_on_sentence_ends():
    asm = _SentenceAssembler()
    out = asm.feed("您好")
    assert out == []
    out = asm.feed("呀！今天")
    assert out == ["您好呀！"]
    out = asm.feed("精神不错。")
    assert out == ["今天精神不错。"]
    assert asm.finish() == []


def test_assembler_flushes_tail_without_punctuation():
    asm = _SentenceAssembler()
    assert asm.feed("没带标点的尾巴") == []
    assert asm.finish() == ["没带标点的尾巴"]


def test_assembler_keeps_marker_with_first_sentence():
    asm = _SentenceAssembler()
    assert asm.feed("[act:wave]") == []
    assert asm.feed("你好。") == ["[act:wave]你好。"]


# ---------- 事件顺序与降级 ----------


async def test_llm_stream_emits_action_before_tts(monkeypatch):
    events: list[dict] = []
    session = make_session(events)

    async def fake_stream(messages, **kwargs):
        for delta in ["[act:wave]", "您好", "呀。", "今天", "想聊点", "什么？"]:
            yield delta

    monkeypatch.setattr("app.services.voice.llm_is_configured", lambda: True)
    monkeypatch.setattr("app.services.voice.stream_chat", fake_stream)

    reply = await session._reply_and_respond("你好")
    assert reply == "您好呀。今天想聊点什么？"
    types = [e["type"] for e in events]
    # action 先于其后的 tts；两个句子各产生一次 tts（done 由调用方发送，不在此层）
    assert types == ["action", "tts", "tts"]
    assert events[0] == {"type": "action", "action": "wave", "param": None}
    assert events[1]["text"] == "您好呀。"
    assert "[act:" not in reply


async def test_unconfigured_llm_falls_back_to_template(monkeypatch):
    events: list[dict] = []
    session = make_session(events)
    monkeypatch.setattr("app.services.voice.llm_is_configured", lambda: False)

    reply = await session._reply_and_respond("你好")
    assert reply == "你刚才说的是：你好"
    types = [e["type"] for e in events]
    # 规则触发：模板句含"你好" → 句首补 [act:wave]
    assert types == ["action", "tts"]
    assert events[0]["action"] == "wave"
    assert events[1]["text"] == "你刚才说的是：你好"


async def test_llm_start_failure_falls_back_to_template(monkeypatch):
    events: list[dict] = []
    session = make_session(events)

    async def fake_stream(messages, **kwargs):
        raise LlmUnavailable("boom")
        yield  # pragma: no cover

    monkeypatch.setattr("app.services.voice.llm_is_configured", lambda: True)
    monkeypatch.setattr("app.services.voice.stream_chat", fake_stream)

    reply = await session._reply_and_respond("再见")
    assert reply == "你刚才说的是：再见"
    assert [e["type"] for e in events] == ["action", "tts"]


async def test_llm_partial_failure_keeps_spoken_part(monkeypatch):
    events: list[dict] = []
    session = make_session(events)

    async def fake_stream(messages, **kwargs):
        yield "您"
        yield "好。"
        raise LlmUnavailable("mid-stream boom")

    monkeypatch.setattr("app.services.voice.llm_is_configured", lambda: True)
    monkeypatch.setattr("app.services.voice.stream_chat", fake_stream)

    reply = await session._reply_and_respond("你好")
    assert reply == "您好。"  # 已说部分沿用，不重复补模板
    types = [e["type"] for e in events]
    assert types == ["tts"]
    assert events[0]["text"] == "您好。"


async def test_llm_markers_only_reply_falls_back(monkeypatch):
    """LLM 只回标记不给正文：视为空回复，降级模板。"""
    events: list[dict] = []
    session = make_session(events)

    async def fake_stream(messages, **kwargs):
        yield "[act:wave][act:nod]"

    monkeypatch.setattr("app.services.voice.llm_is_configured", lambda: True)
    monkeypatch.setattr("app.services.voice.stream_chat", fake_stream)

    reply = await session._reply_and_respond("你好")
    assert reply == "你刚才说的是：你好"
