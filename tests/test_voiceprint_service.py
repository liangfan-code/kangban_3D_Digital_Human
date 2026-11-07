"""声纹服务与临时 Token 状态机测试（mock sherpa 推理，不依赖模型文件）。"""

import time
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import Voiceprint
from app.models.enums import VoiceprintStatus
from app.services.voice import (
    VoiceSession,
    VoiceTokenStore,
    enroll_voiceprint,
    load_room_voiceprints,
    template_reply,
)
from app.integrations import ProviderUnavailable

FAKE_VEC_A = [1.0] + [0.0] * 191
FAKE_VEC_B = [0.0, 1.0] + [0.0] * 190


class FakeVoiceprintProvider:
    """确定性假声纹 Provider：按音频首字节决定输出向量。"""

    def __init__(self):
        self.model_fp = "fake-fingerprint-0123456789abcdef"

    def extract_embedding(self, audio_16k: bytes) -> list[float]:
        if len(audio_16k) < 3200:
            raise ValueError("有效语音不足，无法提取声纹")
        return FAKE_VEC_A if audio_16k[0] % 2 == 0 else FAKE_VEC_B

    def speech_only(self, audio_16k: bytes) -> bytes:
        return audio_16k

    @staticmethod
    def cosine(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b))

    def model_fingerprint(self) -> str:
        return self.model_fp


@pytest.fixture
def fake_provider(monkeypatch):
    provider = FakeVoiceprintProvider()
    monkeypatch.setattr("app.services.voice.get_voiceprint_provider", lambda: provider)
    monkeypatch.setattr("app.services.voice.get_tts_provider", lambda: FakeTTSProvider())
    return provider


class FakeTTSProvider:
    async def synthesize(self, text: str) -> bytes:
        return b"\x01\x00" * 160

    def sample_rate(self) -> int:
        return 16000


class FakeASRSession:
    """假 FunASR 会话：finish 后吐一条 2pass-offline 终句。"""

    def __init__(self):
        self.fed = 0
        self.closed = False

    async def start_session(self) -> None:
        pass

    async def feed_audio(self, audio_16k: bytes) -> None:
        self.fed += len(audio_16k)

    async def finish(self) -> None:
        pass

    async def results(self):
        from app.integrations import ASRResult

        yield ASRResult(text="你好康伴", is_final=True, mode="2pass-offline", confidence=0.87)

    async def close(self) -> None:
        self.closed = True


# ---- template_reply / Token 存储 ----


def test_template_reply_day3_fixed():
    """Day3 固定模板回复口径（大纲课时 3.4）。"""
    assert template_reply("你好") == "你刚才说的是：你好"


def test_token_store_issue_and_validate():
    store = VoiceTokenStore()
    device = uuid4()
    token, ttl = store.issue(device, uuid4(), 0.9)
    assert ttl == 300
    record = store.validate(token, device)
    assert record is not None and record.score == 0.9


def test_token_store_device_binding():
    """Token 与设备绑定：别的设备拿来无效。"""
    store = VoiceTokenStore()
    token, _ = store.issue(uuid4(), uuid4(), 0.9)
    assert store.validate(token, uuid4()) is None


def test_token_store_expiry(monkeypatch):
    """过期 Token 校验失败（voiceprint_token_ttl 单调时钟基准）。"""
    store = VoiceTokenStore()
    device = uuid4()
    token, _ = store.issue(device, uuid4(), 0.9)
    monkeypatch.setattr(time, "monotonic", lambda: time.time() + 10_000)
    assert store.validate(token, device) is None


# ---- VoiceSession 状态机（WS 协议核心）----


def _make_session(events: list, candidates, embeddings):
    async def send_event(event: dict) -> None:
        events.append(event)

    return VoiceSession(uuid4(), candidates, embeddings, send_event)


async def test_session_first_utterance_verification_pass(fake_provider):
    """首句：声纹通过 → accepted 事件 + 临时 Token + verified 状态。"""
    events: list[dict] = []
    elder = uuid4()
    session = _make_session(events, [(elder, "张奶奶")], {elder: [FAKE_VEC_A]})
    session.feed_pcm(b"\x00\x00" * 16000 * 2)  # 2 秒静音首字节偶数 → FAKE_VEC_A
    await session._flush_utterance()

    types = [e["type"] for e in events]
    assert "verification" in types
    verification = [e for e in events if e["type"] == "verification" and e.get("token")]
    assert len(verification) == 1
    assert verification[0]["accepted"] is True
    assert verification[0]["elder_id"] == str(elder)
    assert session.verified_elder_id == elder
    assert any(e["type"] == "state" and e["state"] == "verified" for e in events)


async def test_session_verification_fail_then_confirm(fake_provider, monkeypatch):
    """声纹不通过 → 连续 3 次触发 need_confirm（ADR-008 主动确认）。"""
    events: list[dict] = []
    elder = uuid4()
    # 房间档案是 B 向量，说话人（偶数首字节）是 A 向量 → 不匹配
    session = _make_session(events, [(elder, "李爷爷")], {elder: [FAKE_VEC_B]})
    monkeypatch.setattr(
        "app.services.voice.get_settings",
        lambda: type("S", (), {"voiceprint_threshold": 0.99, "voiceprint_token_ttl": 300})(),
    )
    for _ in range(3):
        session.feed_pcm(b"\x00\x00" * 16000 * 2)
        await session._flush_utterance()
    rejects = [e for e in events if e["type"] == "verification" and e.get("accepted") is False]
    assert len(rejects) >= 3
    assert any(e["type"] == "need_confirm" for e in events)
    assert session.verified_elder_id is None


async def test_session_double_room_voiceprint_picks_elder(fake_provider):
    """双人间：声纹在在住长者集合中匹配出 elder_id（服务端定身份）。"""
    events: list[dict] = []
    e1, e2 = uuid4(), uuid4()
    session = _make_session(
        events,
        [(e1, "张奶奶"), (e2, "李爷爷")],
        {e1: [FAKE_VEC_A], e2: [FAKE_VEC_B]},
    )
    session.feed_pcm(b"\x01\x00" * 16000 * 2)  # 奇数首字节 → FAKE_VEC_B → 命中 e2
    await session._flush_utterance()
    assert session.verified_elder_id == e2


async def test_session_verified_utterance_runs_asr_tts(fake_provider, monkeypatch):
    """免验期：第二句直接转写 → 模板回复 → TTS → done（不走声纹计算）。"""
    events: list[dict] = []
    elder = uuid4()
    session = _make_session(events, [(elder, "张奶奶")], {elder: [FAKE_VEC_A]})
    session.feed_pcm(b"\x00\x00" * 16000 * 2)
    await session._flush_utterance()
    assert session.verified_elder_id == elder
    events.clear()

    fake_asr = FakeASRSession()
    monkeypatch.setattr("app.services.voice.create_asr_session", lambda: fake_asr)
    session.feed_pcm(b"\x00\x00" * 16000)
    await session._flush_utterance()

    transcripts = [e for e in events if e["type"] == "transcript"]
    assert transcripts and transcripts[-1]["text"] == "你好康伴"
    assert transcripts[-1]["confidence"] == 0.87
    tts = [e for e in events if e["type"] == "tts"]
    assert tts and tts[0]["text"] == "你刚才说的是：你好康伴"
    assert tts[0]["sample_rate"] == 16000
    assert any(e["type"] == "done" for e in events)
    assert fake_asr.closed


async def test_session_asr_unreachable_degrades(fake_provider, monkeypatch):
    """FunASR 容器不可达 → asr_unreachable 可见降级事件（验收项 5）。"""
    events: list[dict] = []
    elder = uuid4()
    session = _make_session(events, [(elder, "张奶奶")], {elder: [FAKE_VEC_A]})
    session.verified_elder_id = elder

    class Boom:
        def __init__(self, *a, **k):
            pass

        async def start_session(self):
            raise OSError("connection refused")

    monkeypatch.setattr("app.services.voice.create_asr_session", Boom)
    session.feed_pcm(b"\x00\x00" * 16000)
    await session._flush_utterance()
    assert any(
        e["type"] == "error" and e["code"] == "asr_unreachable" for e in events
    )


async def test_session_voiceprint_unavailable_degrades(monkeypatch):
    """模型未配置 → voiceprint_unavailable 降级事件，会话不崩（验收项 12）。"""
    events: list[dict] = []
    monkeypatch.setattr(
        "app.services.voice.get_voiceprint_provider", lambda: None
    )
    session = _make_session(events, [(uuid4(), "张奶奶")], {})
    session.feed_pcm(b"\x00\x00" * 16000)
    await session._flush_utterance()
    assert any(
        e["type"] == "error" and e["code"] == "voiceprint_unavailable" for e in events
    )


def test_session_try_resume_with_valid_token(fake_provider, monkeypatch):
    """前端携带免验 Token 重连 → 直接恢复 verified 状态。

    try_resume 走模块级单例 voice_token_store，测试用 monkeypatch 注入同名实例。
    """
    store = VoiceTokenStore()
    monkeypatch.setattr("app.services.voice.voice_token_store", store)
    device = uuid4()
    elder = uuid4()
    token, _ = store.issue(device, elder, 0.88)

    events: list[dict] = []
    session = _make_session(events, [(elder, "张奶奶")], {elder: [FAKE_VEC_A]})
    session.device_id = device
    assert session.try_resume(token) is True
    assert session.verified_elder_id == elder
    assert session.try_resume("bad-token") is False
    assert session.verified_elder_id == elder  # 非法 token 不改变已验证状态


# ---- 入库（连测试库，Provider 为假实现）----


async def test_enroll_voiceprint_writes_record(fake_provider, seed, db_session):
    """enroll：voiceprints 新增 active 记录、embedding_model 带指纹（验收项 6）。"""
    elder = seed["elders"]["single"]
    record = await enroll_voiceprint(
        db_session,
        elder_id=elder.elder_id,
        audio_samples=[b"\x00\x00" * 16000 * 3, b"\x00\x00" * 16000 * 3],
    )
    assert record.status == VoiceprintStatus.ACTIVE
    assert record.embedding_model.startswith("campplus-3dspeaker:fake-finger")
    assert len(list(record.embedding)) == 192
    rows = (
        await db_session.execute(
            select(Voiceprint).where(Voiceprint.elder_id == elder.elder_id)
        )
    ).scalars().all()
    assert len(rows) == 1


async def test_enroll_rejects_missing_elder(fake_provider, seed, db_session):
    with pytest.raises(LookupError):
        await enroll_voiceprint(
            db_session, elder_id=uuid4(), audio_samples=[b"\x00\x00" * 16000 * 3]
        )


async def test_load_room_voiceprints_filters_active(fake_provider, seed, db_session):
    """只取 active 声纹；revoked/expired 不参与匹配。"""
    elder = seed["elders"]["single"]
    await enroll_voiceprint(db_session, elder_id=elder.elder_id, audio_samples=[b"\x00\x00" * 16000 * 3])
    revoked = Voiceprint(
        elder_id=elder.elder_id,
        embedding=FAKE_VEC_B,
        embedding_model="campplus-3dspeaker:old",
        status=VoiceprintStatus.REVOKED,
    )
    db_session.add(revoked)
    await db_session.commit()

    snapshot = await load_room_voiceprints(db_session, [elder.elder_id])
    assert elder.elder_id in snapshot
    assert len(snapshot[elder.elder_id]) == 1  # revoked 被过滤


async def test_enroll_requires_provider(seed, db_session, monkeypatch):
    """模型未配置 → 503 语义前提：ProviderUnavailable 冒泡。"""
    monkeypatch.setattr("app.services.voice.get_voiceprint_provider", lambda: None)
    with pytest.raises(ProviderUnavailable):
        await enroll_voiceprint(
            db_session,
            elder_id=seed["elders"]["single"].elder_id,
            audio_samples=[b"\x00\x00" * 16000 * 3],
        )
