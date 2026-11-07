"""早期摘要测试：只摘要窗口外消息、游标推进、LLM 失败保留旧摘要。"""

from sqlalchemy import select

from app.integrations.llm import LlmUnavailable
from app.models.conversation_message import ConversationMessage
from app.models.enums import IdentitySource
from app.services import conversation_store
from app.services.context_summary import refresh_summary
from app.services.session_state import SessionStateStore, new_state


async def _thread_with_rounds(db_session, seed, rounds: int):
    conv = await conversation_store.get_or_create_active_conversation(
        db_session,
        device_id=seed["devices"]["free"].device_id,
        elder_id=None,
        identity_source=IdentitySource.ANONYMOUS,
    )
    for i in range(rounds):
        await conversation_store.record_turn(
            db_session, thread_id=conv.thread_id, user_text=f"u{i}", assistant_text=f"a{i}"
        )
    return conv


async def _all_messages(db_session, thread_id):
    stmt = (
        select(ConversationMessage)
        .where(ConversationMessage.thread_id == thread_id)
        .order_by(ConversationMessage.seq)
    )
    return list((await db_session.execute(stmt)).scalars().all())


async def test_summary_only_covers_messages_before_window(db_session, seed, monkeypatch):
    conv = await _thread_with_rounds(db_session, seed, 8)  # 16 条消息
    messages = await _all_messages(db_session, conv.thread_id)
    recent = await conversation_store.recent_messages(db_session, conv.thread_id)
    boundary = recent[0].seq
    expected = [m for m in messages if m.seq < boundary]
    assert len(expected) == 6

    captured: dict = {}

    async def fake_complete(messages_in, *, transport=None, max_tokens=None):
        captured["messages"] = messages_in
        captured["max_tokens"] = max_tokens
        return "摘要：张奶奶喜欢京剧"

    monkeypatch.setattr("app.services.context_summary.complete_chat", fake_complete)

    store = SessionStateStore(force_memory=True)
    state = new_state(str(conv.thread_id))
    new_upto = await refresh_summary(db_session, conv.thread_id, state, store=store)

    transcript = captured["messages"][1]["content"]
    for message in expected:
        assert message.content in transcript
    assert "u3" not in transcript
    assert new_upto == expected[-1].seq
    assert captured["max_tokens"] >= 200

    saved = await store.load(str(conv.thread_id))
    assert saved.summary == "摘要：张奶奶喜欢京剧"
    assert saved.summary_upto_seq == expected[-1].seq


async def test_summary_failure_keeps_old_summary_and_cursor(db_session, seed, monkeypatch):
    conv = await _thread_with_rounds(db_session, seed, 8)

    async def boom(*args, **kwargs):
        raise LlmUnavailable("down")

    monkeypatch.setattr("app.services.context_summary.complete_chat", boom)

    store = SessionStateStore(force_memory=True)
    state = new_state(str(conv.thread_id))
    state.summary = "旧摘要：李爷爷怕冷"
    await store.save(state)

    upto = await refresh_summary(db_session, conv.thread_id, state, store=store)
    assert upto == 0

    saved = await store.load(str(conv.thread_id))
    assert saved.summary == "旧摘要：李爷爷怕冷"
    assert saved.summary_upto_seq == 0


async def test_window_only_does_not_trigger_summary(db_session, seed, monkeypatch):
    conv = await _thread_with_rounds(db_session, seed, 5)  # 10 条消息，恰好在窗口内
    called = False

    async def fake_complete(*args, **kwargs):
        nonlocal called
        called = True
        return "不该被调用"

    monkeypatch.setattr("app.services.context_summary.complete_chat", fake_complete)

    store = SessionStateStore(force_memory=True)
    await refresh_summary(
        db_session, conv.thread_id, new_state(str(conv.thread_id)), store=store
    )
    assert called is False
