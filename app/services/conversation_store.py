"""会话逐轮消息存取：thread 复用、逐轮落库与「最近 N 轮」读取。

Day4 上下文工程地基：每轮对话写 user（终句转写）与 assistant（剥净动作标记的
回复正文）两条消息；LLM 接入后从本模块读取最近 5 轮作为对话历史。
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.conversation_message import ConversationMessage
from app.models.enums import ConversationStatus, IdentitySource

logger = logging.getLogger(__name__)

# 最近 10 条消息 = 5 轮问答，作为 LLM 上下文窗口
HISTORY_MESSAGE_LIMIT = 10


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
