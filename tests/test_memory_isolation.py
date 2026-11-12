"""长期记忆隔离测试：elder_id 强制、真实 SQL、匿名不召回、工具话术。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.agent.tools import ToolContext, build_default_registry
from app.models.enums import IdentitySource
from app.models.memory import MemoryChunk
from app.services import conversation_store, memory
from app.services.session_state import SessionStateStore
from app.services.voice import VoiceSession


class _NoDbFactory:
    """匿名会话一旦访问数据库即失败：用于证明未确认身份不查记忆。"""

    def __call__(self):
        raise AssertionError("匿名会话不应访问数据库")


async def test_recall_isolated_by_elder_with_real_sql(db_session, seed):
    elder_a = seed["elders"]["single"].elder_id
    elder_b = seed["elders"]["other"].elder_id

    stats = await memory.remember(
        db_session, elder_a, [{"content": "我有糖尿病", "category": "health"}]
    )
    await db_session.commit()
    assert stats["written"] == 1

    rows = list(
        (
            await db_session.execute(
                select(MemoryChunk).where(MemoryChunk.elder_id == elder_a)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1

    # 王奶奶（elder_b）查不到李爷爷（elder_a）的记忆
    assert await memory.recall(db_session, elder_b, "糖尿病") == []
    assert await memory.recall(db_session, elder_a, "糖尿病")


async def test_recall_statement_always_binds_elder_id(seed):
    elder_b = seed["elders"]["other"].elder_id

    stmt = memory._active_recall_stmt(elder_b, "hash-ngram-256", [0.1] * 256, 5)
    compiled = stmt.compile()

    assert "elder_id" in str(compiled)
    assert elder_b in compiled.params.values()


async def test_remember_and_recall_require_elder(db_session):
    with pytest.raises(ValueError):
        await memory.remember(db_session, None, [{"content": "我有糖尿病", "category": "health"}])
    with pytest.raises(ValueError):
        await memory.recall(db_session, None, "糖尿病")


async def test_anonymous_session_does_not_recall(monkeypatch):
    called = {"count": 0}

    async def _fake_recall(*args, **kwargs):
        called["count"] += 1
        return "【关于长者的记忆】\n- 不应该出现"

    monkeypatch.setattr(
        "app.services.voice.memory_service.build_recall_context", _fake_recall
    )
    monkeypatch.setattr(
        "app.services.voice.get_session_store", lambda: SessionStateStore(force_memory=True)
    )

    async def _send(event: dict) -> None:
        return None

    session = VoiceSession(
        device_id=uuid.uuid4(),
        candidates=[],
        embeddings={},
        send_event=_send,
        db_factory=_NoDbFactory(),
    )
    session.verified_elder_id = None

    context = await session._prepare_state(uuid.uuid4(), "我能吃粽子吗")

    assert called["count"] == 0
    assert context is None


async def test_memory_recall_tool_requires_elder():
    registry = build_default_registry()

    result = await registry.call(
        "memory_recall", {"query": "糖尿病"}, ToolContext(elder_id=None, device_id="dev-1")
    )

    assert result.ok is False
    assert result.degraded == "missing_elder_context"
    assert "哪位" in result.content


async def test_messages_since_respects_memory_cursor(db_session, seed):
    conv = await conversation_store.get_or_create_active_conversation(
        db_session,
        device_id=seed["devices"]["free"].device_id,
        elder_id=None,
        identity_source=IdentitySource.ANONYMOUS,
    )
    for index in range(3):
        await conversation_store.record_turn(
            db_session,
            thread_id=conv.thread_id,
            user_text=f"u{index}",
            assistant_text=f"a{index}",
        )

    everything = await conversation_store.messages_since(db_session, conv.thread_id, 0)
    assert len(everything) == 6

    # 游标停在 u1：之后只应返回 a1/u2/a2
    remaining = await conversation_store.messages_since(
        db_session, conv.thread_id, everything[2].seq
    )
    assert [row.content for row in remaining] == ["a1", "u2", "a2"]
