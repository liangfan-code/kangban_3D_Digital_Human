"""Task 3 测试：敏感事件优先建持久化工单，HITL 内存队列只做兜底。

全部使用假会话工厂与假建单函数，不连任何数据库（不依赖 PostgreSQL）。
"""

import logging
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.services import voice
from app.services.hitl import hitl_queue

CATEGORY = "急症"
TRIGGER = "我胸口疼，喘不上气"
REPLY = "我听到您不舒服，已经帮您叫人了。"


class _FakeSession:
    """只记录 commit 次数的假会话。"""

    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


def _fake_factory(session: _FakeSession):
    """模拟 async_sessionmaker：``async with factory() as db``。"""

    @asynccontextmanager
    async def factory():
        yield session

    return factory


def _msg(role: str, content: str) -> SimpleNamespace:
    """模拟 conversation_messages ORM 行（只要 role/content/created_at）。"""
    return SimpleNamespace(role=role, content=content, created_at=None)


def _patch_create(monkeypatch, *, result=None, error: Exception | None = None) -> dict:
    calls: dict = {}

    async def fake_create(session, **kwargs):
        calls["session"] = session
        calls.update(kwargs)
        if error is not None:
            raise error
        return result, True

    monkeypatch.setattr(voice, "create_sensitive_work_order", fake_create)
    return calls


def _events(caplog) -> list[str]:
    return [getattr(record, "event", None) for record in caplog.records]


@pytest.fixture(autouse=True)
def _clean_queue():
    hitl_queue.clear()
    yield
    hitl_queue.clear()


async def test_elder_known_creates_workorder_and_skips_hitl(monkeypatch):
    """有 elder_id：建单成功，且不再写 HITL 队列（避免重复告警）。"""
    session = _FakeSession()
    workorder = SimpleNamespace(workorder_id=uuid.uuid4())
    calls = _patch_create(monkeypatch, result=workorder)
    elder_id = uuid.uuid4()

    task = voice._enqueue_sensitive(
        TRIGGER,
        CATEGORY,
        elder_id,
        "device-1",
        history=[_msg("user", "今天有点累"), _msg("assistant", "那您先歇会儿。")],
        thread_id=uuid.uuid4(),
        assistant_reply=REPLY,
        db_factory=_fake_factory(session),
    )
    assert task is not None
    await task

    assert calls["session"] is session
    assert calls["elder_id"] == elder_id
    assert calls["category"] == CATEGORY
    assert calls["device_id"] == "device-1"
    assert calls["trigger_text"] == TRIGGER
    assert calls["context_snapshot"][-2]["content"] == TRIGGER
    assert calls["context_snapshot"][-1]["content"] == REPLY
    assert session.commits == 1
    assert hitl_queue.pending() == []


async def test_no_elder_skips_workorder_and_enqueues_hitl(monkeypatch, caplog):
    """elder_id 为空：不建单，保持原 HITL 入队，并记录 workorder_skipped_no_elder。"""
    called = {"n": 0}

    async def fake_create(session, **kwargs):  # pragma: no cover —— 不应被调用
        called["n"] += 1
        return SimpleNamespace(workorder_id=uuid.uuid4()), True

    monkeypatch.setattr(voice, "create_sensitive_work_order", fake_create)
    caplog.set_level(logging.INFO)

    task = voice._enqueue_sensitive(TRIGGER, CATEGORY, None, "device-2")

    assert task is None
    assert called["n"] == 0
    pending = hitl_queue.pending()
    assert len(pending) == 1
    assert pending[0].summary == CATEGORY
    assert pending[0].elder_id is None
    assert "workorder_skipped_no_elder" in _events(caplog)


async def test_workorder_failure_falls_back_to_hitl_without_raising(monkeypatch, caplog):
    """建单抛异常：回退 HITL、不冒泡，日志记 workorder_create_failed 不含原文。"""
    _patch_create(monkeypatch, error=RuntimeError(TRIGGER))
    caplog.set_level(logging.INFO)

    task = voice._enqueue_sensitive(
        TRIGGER,
        CATEGORY,
        uuid.uuid4(),
        "device-3",
        history=[_msg("assistant", "我在呢。")],
        db_factory=_fake_factory(_FakeSession()),
    )
    assert task is not None
    await task  # 不应抛出

    pending = hitl_queue.pending()
    assert len(pending) == 1
    assert pending[0].detail == TRIGGER
    assert pending[0].summary == CATEGORY
    events = _events(caplog)
    assert "workorder_create_failed" in events
    for record in caplog.records:
        if getattr(record, "event", None) == "workorder_create_failed":
            # 只记异常类型，不把异常串（可能含 SQL 参数/原文）写进日志
            assert getattr(record, "error", None) == "RuntimeError"
            assert TRIGGER not in record.getMessage()


async def test_snapshot_keeps_at_most_five_turns(monkeypatch):
    """快照最多保留触发前 5 轮（10 条消息）+ 触发句 + 首条安全回复。"""
    calls = _patch_create(monkeypatch, result=SimpleNamespace(workorder_id=uuid.uuid4()))
    history = []
    for turn in range(8):
        history.append(_msg("user", f"第{turn}轮提问"))
        history.append(_msg("assistant", f"第{turn}轮回复"))

    await voice._create_work_order(
        elder_id=uuid.uuid4(),
        category=CATEGORY,
        device_id="device-4",
        trigger_text=TRIGGER,
        assistant_reply=REPLY,
        history=history,
        thread_id=uuid.uuid4(),
        db_factory=_fake_factory(_FakeSession()),
    )

    snapshot = calls["context_snapshot"]
    # 10 条历史 + 触发句 + 首条回复
    assert len(snapshot) == 12
    assert snapshot[0]["content"] == "第3轮提问"
    assert snapshot[-2]["role"] == "user" and snapshot[-2]["content"] == TRIGGER
    assert snapshot[-1]["role"] == "assistant" and snapshot[-1]["content"] == REPLY


async def test_snapshot_falls_back_to_conversation_store(monkeypatch):
    """会话内历史不可得时，从会话存储读取触发前真实消息，不虚构内容。"""
    calls = _patch_create(monkeypatch, result=SimpleNamespace(workorder_id=uuid.uuid4()))
    thread_id = uuid.uuid4()
    queried = {}

    async def fake_recent_messages(db, tid, limit=10):
        queried["thread_id"] = tid
        return [_msg("user", "昨天睡得不太好"), _msg("assistant", "那今晚早点休息。")]

    monkeypatch.setattr(voice.conversation_store, "recent_messages", fake_recent_messages)

    await voice._create_work_order(
        elder_id=uuid.uuid4(),
        category=None,
        device_id="device-5",
        trigger_text=TRIGGER,
        assistant_reply=None,
        history=[],
        thread_id=thread_id,
        db_factory=_fake_factory(_FakeSession()),
    )

    assert queried["thread_id"] == thread_id
    assert calls["category"] == ""
    snapshot = calls["context_snapshot"]
    assert [item["content"] for item in snapshot] == [
        "昨天睡得不太好",
        "那今晚早点休息。",
        TRIGGER,
    ]
