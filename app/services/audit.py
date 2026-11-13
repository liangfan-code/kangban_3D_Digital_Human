"""会话/记忆访问审计（数据治理 段 4）。

只记元数据（actor/action/target/detail 摘要），禁止记录对话正文与记忆正文。
写入用 SAVEPOINT 保护：审计失败只记日志，不阻断主链路事务。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models import ConversationAuditLog

logger = get_logger(__name__)


async def record_audit(
    db: AsyncSession,
    *,
    action: str,
    target_type: str,
    target_id: str | None = None,
    actor_account_id: UUID | None = None,
    actor_role: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """写一条审计记录；失败只记日志（SAVEPOINT 回滚），不抛出。"""
    try:
        async with db.begin_nested():
            db.add(
                ConversationAuditLog(
                    actor_account_id=actor_account_id,
                    actor_role=actor_role,
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    detail=detail,
                )
            )
            await db.flush()
    except Exception as exc:  # noqa: BLE001 —— 审计兜底：不阻断主链路
        logger.error(
            "审计写入失败",
            extra={
                "event": "conversation_audit_write_failed",
                "action": action,
                "target_type": target_type,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )


async def list_audit_logs(
    db: AsyncSession,
    *,
    target_type: str | None = None,
    target_id: str | None = None,
    limit: int = 50,
) -> list[ConversationAuditLog]:
    """按目标过滤倒序查询审计（管理端只读）。"""
    from sqlalchemy import select

    stmt = (
        select(ConversationAuditLog)
        .order_by(ConversationAuditLog.created_at.desc())
        .limit(limit)
    )
    if target_type is not None:
        stmt = stmt.where(ConversationAuditLog.target_type == target_type)
    if target_id is not None:
        stmt = stmt.where(ConversationAuditLog.target_id == target_id)
    return list((await db.execute(stmt)).scalars().all())
