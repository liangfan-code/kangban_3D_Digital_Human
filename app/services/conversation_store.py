"""会话逐轮消息存取：thread 复用、逐轮落库与「最近 N 轮」读取。

Day4 上下文工程地基：每轮对话写 user（终句转写）与 assistant（剥净动作标记的
回复正文）两条消息；LLM 接入后从本模块读取最近 5 轮作为对话历史。

会话状态机（对话历史可视化，2026-11-13）：
- ACTIVE：进行中，同设备+长者至多一条（get_or_create 只认最近一条 ACTIVE）；
- ENDED：已收起，可 activate 切回继续聊；
- ABANDONED：已关闭，只读不可恢复。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.conversation_message import ConversationMessage
from app.models.conversation_write_failure import ConversationWriteFailure
from app.models.enums import ConversationStatus, IdentitySource

logger = logging.getLogger(__name__)

# 最近 10 条消息 = 5 轮问答，作为 LLM 上下文窗口
HISTORY_MESSAGE_LIMIT = 10


class ConversationStateError(Exception):
    """会话状态不允许该操作（如恢复已 ABANDONED 的会话）。"""


class ConversationNotFoundError(Exception):
    """thread 不存在或不属于该设备/长者。"""


async def get_or_create_active_conversation(
    db: AsyncSession,
    *,
    device_id: uuid.UUID,
    elder_id: uuid.UUID | None,
    identity_source: IdentitySource,
) -> Conversation:
    """复用同设备（且同长者）的 ACTIVE 会话；不存在则新建。

    长者验证通过前 elder_id 为空、验证通过后带 elder_id：两者视作不同线索，
    验证瞬间切换到「设备 + 长者」新线程，验证前的匿名轮次不混入。
    """

    elder_filter = (
        Conversation.elder_id.is_(None) if elder_id is None else Conversation.elder_id == elder_id
    )
    stmt = (
        select(Conversation)
        .where(
            Conversation.device_id == device_id,
            Conversation.status == ConversationStatus.ACTIVE,
            elder_filter,
        )
        .order_by(Conversation.started_at.desc())
        .limit(1)
    )
    conversation = (await db.execute(stmt)).scalar_one_or_none()
    if conversation is not None:
        return conversation

    seq_stmt = select(func.count()).select_from(Conversation)
    if elder_id is not None:
        seq_stmt = seq_stmt.where(Conversation.elder_id == elder_id)
    else:
        seq_stmt = seq_stmt.where(Conversation.device_id == device_id)
    session_seq = int((await db.execute(seq_stmt)).scalar_one()) + 1
    conversation = Conversation(
        device_id=device_id,
        elder_id=elder_id,
        session_seq=session_seq,
        identity_source=identity_source,
        status=ConversationStatus.ACTIVE,
    )
    db.add(conversation)
    await db.flush()
    logger.info(
        "新建对话会话 thread_id=%s device=%s elder=%s seq=%s",
        conversation.thread_id,
        device_id,
        elder_id,
        session_seq,
    )
    return conversation


async def record_turn(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    user_text: str,
    assistant_text: str,
) -> None:
    """一轮问答落库：user 转写 + assistant 回复正文（已剥净动作标记）。"""

    if user_text:
        db.add(ConversationMessage(thread_id=thread_id, role="user", content=user_text))
    if assistant_text:
        db.add(ConversationMessage(thread_id=thread_id, role="assistant", content=assistant_text))
    await db.flush()


async def recent_messages(
    db: AsyncSession, thread_id: uuid.UUID, limit: int = HISTORY_MESSAGE_LIMIT
) -> list[ConversationMessage]:
    """取最近 limit 条消息，按时间正序返回（旧→新，可直接拼进 LLM 历史）。"""

    stmt = (
        select(ConversationMessage)
        .where(ConversationMessage.thread_id == thread_id)
        .order_by(ConversationMessage.seq.desc())
        .limit(limit)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    rows.reverse()
    return rows


async def messages_since(
    db: AsyncSession, thread_id: uuid.UUID, seq: int
) -> list[ConversationMessage]:
    """取 ``seq > :seq`` 的新消息，按 seq 正序（长期记忆萃取游标用）。"""

    stmt = (
        select(ConversationMessage)
        .where(
            ConversationMessage.thread_id == thread_id,
            ConversationMessage.seq > seq,
        )
        .order_by(ConversationMessage.seq)
    )
    return list((await db.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# 会话状态机（新建 / 收起 / 切回 / 列表 / 消息分页）
# ---------------------------------------------------------------------------


def _scope_filter(*, elder_id: uuid.UUID | None, device_id: uuid.UUID | None):
    """会话归属过滤：长者线索按 elder_id 聚合；匿名线索按 device_id 聚合。"""

    if elder_id is not None:
        return Conversation.elder_id == elder_id
    return Conversation.elder_id.is_(None) & (Conversation.device_id == device_id)


async def _end_active_in_scope(
    db: AsyncSession, *, elder_id: uuid.UUID | None, device_id: uuid.UUID | None
) -> None:
    """把同一线索下所有 ACTIVE 会话收起为 ENDED（保证至多一条 ACTIVE）。"""

    stmt = select(Conversation).where(
        _scope_filter(elder_id=elder_id, device_id=device_id),
        Conversation.status == ConversationStatus.ACTIVE,
    )
    for row in (await db.execute(stmt)).scalars().all():
        row.status = ConversationStatus.ENDED
        row.ended_at = datetime.now(timezone.utc)


async def _next_session_seq(
    db: AsyncSession, *, elder_id: uuid.UUID | None, device_id: uuid.UUID | None
) -> int:
    stmt = select(func.count()).select_from(Conversation).where(
        _scope_filter(elder_id=elder_id, device_id=device_id)
    )
    return int((await db.execute(stmt)).scalar_one()) + 1


async def start_new_conversation(
    db: AsyncSession,
    *,
    device_id: uuid.UUID,
    elder_id: uuid.UUID | None,
    identity_source: IdentitySource,
) -> Conversation:
    """新建对话：旧 ACTIVE 收起为 ENDED（仍可见、可切回），新会话 ACTIVE。"""

    await _end_active_in_scope(db, elder_id=elder_id, device_id=device_id)
    conversation = Conversation(
        device_id=device_id,
        elder_id=elder_id,
        session_seq=await _next_session_seq(db, elder_id=elder_id, device_id=device_id),
        identity_source=identity_source,
        status=ConversationStatus.ACTIVE,
    )
    db.add(conversation)
    await db.flush()
    logger.info(
        "新建对话 thread_id=%s elder=%s seq=%s",
        conversation.thread_id,
        elder_id,
        conversation.session_seq,
    )
    return conversation


async def activate_conversation(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    device_id: uuid.UUID,
    elder_id: uuid.UUID | None,
) -> Conversation:
    """切回旧对话继续聊：ENDED → ACTIVE（ended_at 清空），原 ACTIVE 收起。

    ABANDONED 不可恢复；不属于该设备/长者的会话视为不存在。
    """

    stmt = select(Conversation).where(
        Conversation.thread_id == thread_id,
        _scope_filter(elder_id=elder_id, device_id=device_id),
    )
    conversation = (await db.execute(stmt)).scalar_one_or_none()
    if conversation is None:
        raise ConversationNotFoundError("会话不存在或不属于当前设备/长者")
    if conversation.status == ConversationStatus.ABANDONED:
        raise ConversationStateError("会话已关闭，不可恢复")

    await _end_active_in_scope(db, elder_id=elder_id, device_id=device_id)
    conversation.status = ConversationStatus.ACTIVE
    conversation.ended_at = None
    # 跨设备切回时重绑 device_id：WS 每轮按「当前设备 + ACTIVE」选会话，
    # 不重绑则新设备上选不到该会话、会另开新 thread。
    conversation.device_id = device_id
    await db.flush()
    logger.info("切回对话 thread_id=%s elder=%s device=%s", thread_id, elder_id, device_id)
    return conversation


async def end_active_conversation(db: AsyncSession, *, thread_id: uuid.UUID) -> Conversation:
    """收起当前对话：ACTIVE → ENDED。仅 ACTIVE 可收起。"""

    stmt = select(Conversation).where(
        Conversation.thread_id == thread_id,
        Conversation.status == ConversationStatus.ACTIVE,
    )
    conversation = (await db.execute(stmt)).scalar_one_or_none()
    if conversation is None:
        raise ConversationStateError("会话不在进行中，无需收起")
    conversation.status = ConversationStatus.ENDED
    conversation.ended_at = datetime.now(timezone.utc)
    await db.flush()
    return conversation


async def list_conversations(
    db: AsyncSession,
    *,
    elder_id: uuid.UUID | None,
    device_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict], int]:
    """会话列表（长者维度聚合，ACTIVE+ENDED 并列可见；ABANDONED 排除）。

    匿名线索必须传 device_id（elder_id=None 时按设备过滤）。返回
    ``(items, total)``，items 为 dict：thread_id/session_seq/status/
    started_at/ended_at/message_count/is_current。
    """

    total = int(
        (
            await db.execute(
                select(func.count()).select_from(Conversation).where(
                    _scope_filter(elder_id=elder_id, device_id=device_id),
                    Conversation.status != ConversationStatus.ABANDONED,
                )
            )
        ).scalar_one()
    )
    count_sub = (
        select(func.count())
        .where(ConversationMessage.thread_id == Conversation.thread_id)
        .scalar_subquery()
    )
    stmt = (
        select(Conversation, count_sub)
        .where(
            _scope_filter(elder_id=elder_id, device_id=device_id),
            Conversation.status != ConversationStatus.ABANDONED,
        )
        .order_by(Conversation.started_at.desc(), Conversation.session_seq.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await db.execute(stmt)).all()

    latest_active_stmt = (
        select(Conversation.thread_id)
        .where(
            _scope_filter(elder_id=elder_id, device_id=device_id),
            Conversation.status == ConversationStatus.ACTIVE,
        )
        .order_by(Conversation.started_at.desc())
        .limit(1)
    )
    current_thread = (await db.execute(latest_active_stmt)).scalar_one_or_none()

    items = []
    for conversation, message_count in rows:
        items.append(
            {
                "thread_id": conversation.thread_id,
                "session_seq": conversation.session_seq,
                "status": conversation.status,
                "started_at": conversation.started_at,
                "ended_at": conversation.ended_at,
                "message_count": int(message_count or 0),
                "is_current": conversation.thread_id == current_thread,
            }
        )
    return items, total


async def get_elder_conversation(
    db: AsyncSession, *, thread_id: uuid.UUID, elder_id: uuid.UUID
) -> Conversation | None:
    """按 thread_id + 长者归属取会话（消息读取的越权防护；他人会话视为不存在）。"""

    stmt = select(Conversation).where(
        Conversation.thread_id == thread_id,
        Conversation.elder_id == elder_id,
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_messages(
    db: AsyncSession,
    thread_id: uuid.UUID,
    *,
    before_seq: int | None = None,
    limit: int = 30,
) -> tuple[list[ConversationMessage], bool]:
    """消息分页（游标）：``seq < before_seq`` 倒序取 limit+1 条，正序返回。

    返回 ``(messages, has_more)``；``before_seq=None`` 从最新开始。
    """

    conditions = [ConversationMessage.thread_id == thread_id]
    if before_seq is not None:
        conditions.append(ConversationMessage.seq < before_seq)
    stmt = (
        select(ConversationMessage)
        .where(*conditions)
        .order_by(ConversationMessage.seq.desc())
        .limit(limit + 1)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    rows.reverse()
    return rows, has_more


# ---------------------------------------------------------------------------
# 记忆萃取游标（DB 为准）与落库失败可见
# ---------------------------------------------------------------------------


async def get_memory_cursor(db: AsyncSession, thread_id: uuid.UUID) -> int:
    """读取会话的记忆萃取游标（未写入的 thread 返回 0）。"""

    stmt = select(Conversation.memory_upto_seq).where(Conversation.thread_id == thread_id)
    value = (await db.execute(stmt)).scalar_one_or_none()
    return int(value or 0)


async def advance_memory_cursor(db: AsyncSession, *, thread_id: uuid.UUID, seq: int) -> None:
    """萃取完成后回写游标，只增不减（并发/乱序下取较大值）。"""

    stmt = select(Conversation).where(Conversation.thread_id == thread_id)
    conversation = (await db.execute(stmt)).scalar_one_or_none()
    if conversation is None:
        return
    if seq > conversation.memory_upto_seq:
        conversation.memory_upto_seq = seq
        await db.flush()


async def record_write_failure(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    role: str,
    seq: int | None,
    error_type: str,
    detail: str | None,
) -> None:
    """落库失败兜底：写失败表（自身异常只记日志，绝不抛出阻断主链路）。"""

    try:
        db.add(
            ConversationWriteFailure(
                thread_id=thread_id,
                role=role,
                seq=seq,
                error_type=error_type,
                detail=detail,
            )
        )
        await db.flush()
    except Exception:  # pragma: no cover - 兜底路径自身的异常不外抛
        logger.exception("写 conversation_write_failures 失败 thread_id=%s", thread_id)


async def list_write_failures(
    db: AsyncSession, *, limit: int = 50
) -> list[dict]:
    """查询最近的落库失败记录（运维/管理端排查用）。"""

    stmt = (
        select(ConversationWriteFailure)
        .order_by(ConversationWriteFailure.created_at.desc())
        .limit(limit)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    return [
        {
            "failure_id": row.failure_id,
            "thread_id": row.thread_id,
            "role": row.role,
            "seq": row.seq,
            "error_type": row.error_type,
            "detail": row.detail,
            "created_at": row.created_at,
        }
        for row in rows
    ]
