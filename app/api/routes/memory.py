"""长期记忆只读 API（对话历史可视化 段 3）。

鉴权模型：员工/家属账号 Bearer JWT → get_current_user 注入 UserIdentity；
读权限校验在 service 层（admin 全量 / doctor 绑定 / nurse 楼栋 /
family 有效绑定，未绑定 404 不暴露存在性）。
响应不含 embedding 向量；日志不记记忆正文。
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import UserIdentity, get_current_user
from app.db.session import get_session
from app.models.enums import MemoryCategory, MemoryVisibility
from app.services.audit import record_audit
from app.services.memory import (
    MemoryAccessDenied,
    MemoryElderNotFound,
    list_memory_chunks,
)

router = APIRouter(tags=["memories"])


@router.get("/elders/{elder_id}/memories")
async def list_elder_memories(
    elder_id: UUID,
    user: UserIdentity = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    visibility: MemoryVisibility | None = Query(None),
    category: MemoryCategory | None = Query(None),
):
    try:
        result = await list_memory_chunks(
            session,
            account_id=user.account_id,
            role=user.role,
            elder_id=elder_id,
            visibility=visibility.value if visibility else None,
            category=category.value if category else None,
            page=page,
            page_size=page_size,
        )
    except MemoryElderNotFound as exc:
        raise HTTPException(status_code=404, detail="elder_not_found") from exc
    except MemoryAccessDenied as exc:
        raise HTTPException(status_code=403, detail="elder_not_visible") from exc
    # 审计：记忆查询（只记元数据，不记记忆正文）
    await record_audit(
        session,
        action="memory_list_read",
        target_type="elder",
        target_id=str(elder_id),
        actor_account_id=user.account_id,
        actor_role=user.role,
        detail={"page": page, "page_size": page_size, "total": result["total"]},
    )
    await session.commit()
    return result
