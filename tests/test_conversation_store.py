"""conversation_store 测试：thread 复用、逐轮落库、最近 N 轮读取（kangban_test，随事务回滚）。"""

from app.models.enums import ConversationStatus, IdentitySource
from app.services import conversation_store as store


async def _open_thread(db_session, seed, *, device_key="free", elder_id=None):
    return await store.get_or_create_active_conversation(
        db_session,
        device_id=seed["devices"][device_key].device_id,
        elder_id=elder_id,
        identity_source=IdentitySource.ANONYMOUS if elder_id is None else IdentitySource.VOICEPRINT,
    )


async def test_create_then_reuse_active_thread(db_session, seed):
    first = await _open_thread(db_session, seed)
    second = await _open_thread(db_session, seed)
    assert first.thread_id == second.thread_id
    assert first.session_seq == 1
    assert first.status == ConversationStatus.ACTIVE


async def test_anonymous_and_verified_get_separate_threads(db_session, seed):
    elder = seed["elders"]["single"]
    anonymous = await _open_thread(db_session, seed)
    bound = await _open_thread(db_session, seed, elder_id=elder.elder_id)
    again = await _open_thread(db_session, seed, elder_id=elder.elder_id)
    assert anonymous.thread_id != bound.thread_id
    assert again.thread_id == bound.thread_id


async def test_session_seq_increments_after_thread_ends(db_session, seed):
    elder = seed["elders"]["d1"]
    first = await _open_thread(db_session, seed, elder_id=elder.elder_id)
    first.status = ConversationStatus.ENDED
    await db_session.flush()
    second = await _open_thread(db_session, seed, elder_id=elder.elder_id)
    assert second.thread_id != first.thread_id
    assert second.session_seq == first.session_seq + 1


async def test_same_elder_different_devices_get_new_thread(db_session, seed):
    """同一长者在不同设备：物理会话不同，开新线程且 session_seq 递增。"""
    elder = seed["elders"]["single"]
    on_single = await _open_thread(db_session, seed, elder_id=elder.elder_id)
    on_double = await store.get_or_create_active_conversation(
        db_session,
        device_id=seed["devices"]["double"].device_id,
        elder_id=elder.elder_id,
        identity_source=IdentitySource.VOICEPRINT,
    )
    assert on_single.thread_id != on_double.thread_id
    assert on_double.session_seq == on_single.session_seq + 1


async def test_record_turn_writes_user_and_assistant(db_session, seed):
    conv = await _open_thread(db_session, seed)
    await store.record_turn(
        db_session, thread_id=conv.thread_id, user_text="你好呀", assistant_text="您好"
    )
    await store.record_turn(
        db_session, thread_id=conv.thread_id, user_text="今天天气", assistant_text="不错"
    )
    msgs = await store.recent_messages(db_session, conv.thread_id)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "你好呀"),
        ("assistant", "您好"),
        ("user", "今天天气"),
        ("assistant", "不错"),
    ]


async def test_record_turn_skips_empty_text(db_session, seed):
    conv = await _open_thread(db_session, seed)
    await store.record_turn(
        db_session, thread_id=conv.thread_id, user_text="", assistant_text="只有回复"
    )
    msgs = await store.recent_messages(db_session, conv.thread_id)
    assert [(m.role, m.content) for m in msgs] == [("assistant", "只有回复")]


async def test_recent_messages_limit_returns_latest(db_session, seed):
    conv = await _open_thread(db_session, seed)
    for i in range(6):
        await store.record_turn(
            db_session, thread_id=conv.thread_id, user_text=f"u{i}", assistant_text=f"a{i}"
        )
    msgs = await store.recent_messages(db_session, conv.thread_id, limit=4)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "u4"),
        ("assistant", "a4"),
        ("user", "u5"),
        ("assistant", "a5"),
    ]


async def test_recent_messages_empty_thread(db_session, seed):
    conv = await _open_thread(db_session, seed)
    assert await store.recent_messages(db_session, conv.thread_id) == []
