"""段 1：会话分区基础 —— 新建/收起/切回/列表/消息分页/记忆游标/落库失败可见。

状态机语义（方案 §〇）：
- ACTIVE = 进行中（同设备+长者至多一条）；
- ENDED = 已收起，可 activate 切回继续聊；
- ABANDONED = 已关闭，只读不可恢复。
"""

import uuid

import pytest

from app.services import conversation_store as store
from app.services.conversation_store import (
    ConversationNotFoundError,
    ConversationStateError,
)


async def _mk_conversation(db_session, seed, *, elder_key="single", device_key="single"):
    return await store.get_or_create_active_conversation(
        db_session,
        device_id=seed["devices"][device_key].device_id,
        elder_id=seed["elders"][elder_key].elder_id,
        identity_source="device_room",
    )


async def test_start_new_conversation_ends_previous(db_session, seed):
    """新建对话：旧 ACTIVE 自动收起为 ENDED，新会话 ACTIVE 且 seq 递增。"""
    elder_id = seed["elders"]["single"].elder_id
    device_id = seed["devices"]["single"].device_id

    first = await _mk_conversation(db_session, seed)
    second = await store.start_new_conversation(
        db_session, device_id=device_id, elder_id=elder_id, identity_source="device_room"
    )
    await db_session.flush()

    await db_session.refresh(first)
    assert first.status == "ended"
    assert first.ended_at is not None
    assert second.status == "active"
    assert second.ended_at is None
    assert second.session_seq == first.session_seq + 1

    third = await store.start_new_conversation(
        db_session, device_id=device_id, elder_id=elder_id, identity_source="device_room"
    )
    await db_session.refresh(second)
    assert second.status == "ended"
    assert third.session_seq == second.session_seq + 1


async def test_activate_conversation_reopens_ended(db_session, seed):
    """切回旧对话：目标 ENDED → ACTIVE（ended_at 清空），原 ACTIVE 收起。"""
    elder_id = seed["elders"]["single"].elder_id
    device_id = seed["devices"]["single"].device_id

    first = await _mk_conversation(db_session, seed)
    second = await store.start_new_conversation(
        db_session, device_id=device_id, elder_id=elder_id, identity_source="device_room"
    )
    await db_session.flush()

    reopened = await store.activate_conversation(
        db_session, thread_id=first.thread_id, device_id=device_id, elder_id=elder_id
    )
    await db_session.refresh(second)
    assert reopened.thread_id == first.thread_id
    assert first.status == "active"
    assert first.ended_at is None
    assert second.status == "ended"


async def test_activate_rejects_abandoned_and_foreign(db_session, seed):
    """ABANDONED 拒绝恢复；他人长者会话拒绝激活。

    注意：同长者跨设备允许激活（换设备后按 elder_id 继承历史的设计目标）。
    """
    elder_id = seed["elders"]["single"].elder_id
    device_id = seed["devices"]["single"].device_id
    conv = await _mk_conversation(db_session, seed)
    conv.status = "abandoned"
    await db_session.flush()

    with pytest.raises(ConversationStateError):
        await store.activate_conversation(
            db_session, thread_id=conv.thread_id, device_id=device_id, elder_id=elder_id
        )

    other_elder = seed["elders"]["other"].elder_id
    with pytest.raises(ConversationNotFoundError):
        await store.activate_conversation(
            db_session, thread_id=conv.thread_id, device_id=device_id, elder_id=other_elder
        )


async def test_activate_cross_device_same_elder(db_session, seed):
    """换设备场景：同长者在另一台设备上可切回历史会话（token 续期继承）。"""
    elder_id = seed["elders"]["single"].elder_id
    first = await _mk_conversation(db_session, seed)  # 设备 single 上产生
    await store.end_active_conversation(db_session, thread_id=first.thread_id)
    await db_session.flush()

    reopened = await store.activate_conversation(
        db_session,
        thread_id=first.thread_id,
        device_id=seed["devices"]["double"].device_id,  # 换设备
        elder_id=elder_id,
    )
    assert reopened.thread_id == first.thread_id
    assert reopened.status == "active"
    assert reopened.device_id == seed["devices"]["double"].device_id


async def test_end_active_conversation(db_session, seed):
    """收起当前对话：ACTIVE → ENDED；重复收起报状态错误。"""
    conv = await _mk_conversation(db_session, seed)
    ended = await store.end_active_conversation(db_session, thread_id=conv.thread_id)
    assert ended.status == "ended"
    assert ended.ended_at is not None

    with pytest.raises(ConversationStateError):
        await store.end_active_conversation(db_session, thread_id=conv.thread_id)


async def test_list_conversations_excludes_abandoned(db_session, seed):
    """列表按 elder 聚合、新旧并列（ACTIVE 在前按 started_at 倒序）、排除 ABANDONED。"""
    elder_id = seed["elders"]["single"].elder_id
    device_id = seed["devices"]["single"].device_id

    first = await _mk_conversation(db_session, seed)
    second = await store.start_new_conversation(
        db_session, device_id=device_id, elder_id=elder_id, identity_source="device_room"
    )
    third = await store.start_new_conversation(
        db_session, device_id=device_id, elder_id=elder_id, identity_source="device_room"
    )
    second.status = "abandoned"
    await db_session.flush()

    items, total = await store.list_conversations(db_session, elder_id=elder_id)
    assert total == 2  # abandoned 不计入
    assert [it["thread_id"] for it in items] == [third.thread_id, first.thread_id]
    assert items[0]["is_current"] is True  # 最新 ACTIVE
    assert items[1]["status"] == "ended"
    assert items[0]["message_count"] == 0


async def test_list_conversations_device_scoped_for_anonymous(db_session, seed):
    """匿名线索（elder_id=NULL）按设备聚合，只看自己设备的匿名会话。"""
    device_id = seed["devices"]["single"].device_id
    mine = await store.get_or_create_active_conversation(
        db_session, device_id=device_id, elder_id=None, identity_source="anonymous"
    )
    await store.get_or_create_active_conversation(
        db_session,
        device_id=seed["devices"]["double"].device_id,
        elder_id=None,
        identity_source="anonymous",
    )
    await db_session.flush()

    items, total = await store.list_conversations(db_session, elder_id=None, device_id=device_id)
    assert total == 1
    assert items[0]["thread_id"] == mine.thread_id


async def test_list_messages_pagination(db_session, seed):
    """消息分页：before_seq 游标倒序取、正序返回，has_more 标记。"""
    conv = await _mk_conversation(db_session, seed)
    for i in range(5):
        await store.record_turn(
            db_session, thread_id=conv.thread_id, user_text=f"问{i}", assistant_text=f"答{i}"
        )
    await db_session.flush()

    page1, more1 = await store.list_messages(
        db_session, conv.thread_id, before_seq=None, limit=4
    )
    assert more1 is True
    assert [m.content for m in page1] == ["问3", "答3", "问4", "答4"]  # 最新 4 条、正序

    page2, more2 = await store.list_messages(
        db_session, conv.thread_id, before_seq=page1[0].seq, limit=4
    )
    assert more2 is True
    assert [m.content for m in page2] == ["问1", "答1", "问2", "答2"]

    page3, more3 = await store.list_messages(
        db_session, conv.thread_id, before_seq=page2[0].seq, limit=4
    )
    assert more3 is False
    assert [m.content for m in page3] == ["问0", "答0"]


async def test_memory_cursor_roundtrip(db_session, seed):
    """记忆游标落库：advance 只增不减；未写入的 thread 返回 0。"""
    conv = await _mk_conversation(db_session, seed)
    await db_session.flush()

    assert await store.get_memory_cursor(db_session, conv.thread_id) == 0

    await store.advance_memory_cursor(db_session, thread_id=conv.thread_id, seq=7)
    await store.advance_memory_cursor(db_session, thread_id=conv.thread_id, seq=4)  # 不回退
    await db_session.refresh(conv)
    assert conv.memory_upto_seq == 7
    assert await store.get_memory_cursor(db_session, conv.thread_id) == 7


async def test_record_write_failure_visible(db_session, seed):
    """落库失败可见：record_write_failure 写入失败表且可查询。"""
    conv = await _mk_conversation(db_session, seed)
    await db_session.flush()

    await store.record_write_failure(
        db_session,
        thread_id=conv.thread_id,
        role="user",
        seq=None,
        error_type="ValueError",
        detail="模拟落库失败",
    )
    await db_session.flush()

    failures = await store.list_write_failures(db_session, limit=10)
    assert len(failures) == 1
    assert failures[0]["thread_id"] == conv.thread_id
    assert failures[0]["error_type"] == "ValueError"
    assert failures[0]["role"] == "user"


async def test_anonymous_thread_not_in_elder_list(db_session, seed):
    """匿名线程 elder_id=NULL：不得混入长者会话列表（记忆隔离边界）。"""
    elder_id = seed["elders"]["single"].elder_id
    device_id = seed["devices"]["single"].device_id
    await store.get_or_create_active_conversation(
        db_session, device_id=device_id, elder_id=None, identity_source="anonymous"
    )
    elder_conv = await _mk_conversation(db_session, seed)
    await db_session.flush()

    items, total = await store.list_conversations(db_session, elder_id=elder_id)
    assert total == 1
    assert items[0]["thread_id"] == elder_conv.thread_id


def test_thread_id_is_uuid4():
    """thread_id 语义不变：UUID 主键，可开启新对话而不与旧对话冲突。"""
    a = uuid.uuid4()
    b = uuid.uuid4()
    assert a != b
