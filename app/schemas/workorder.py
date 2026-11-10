"""工单接口请求/响应契约（Day8 Task 5）。

- 列表项含处理项全文与「当前查看角色」的未处理标记；
- 员工/管理员详情返回 context_snapshot（对话快照），家属详情显式不含该字段；
- 请求体只收方案与关闭备注，任何身份字段都不来自客户端。
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class WorkorderDispositionOut(BaseModel):
    """处理项：角色 + 三态 + 方案 + 操作人与处理时间。"""

    model_config = ConfigDict(from_attributes=True)

    role: str
    status: str
    account_id: uuid.UUID | None = None
    plan_text: str | None = None
    handled_at: datetime | None = None


class WorkorderListItem(BaseModel):
    id: uuid.UUID
    elder_id: uuid.UUID
    category: str | None = None
    status: str
    priority: str
    occurrence_count: int
    created_at: datetime
    updated_at: datetime
    dispositions: list[WorkorderDispositionOut]
    my_status: str | None = None  # 当前查看角色的处理项状态（admin/family 无处理项）
    pending: bool = False  # 当前查看角色是否仍有未处理项（handler 未 handled）


class WorkorderDetail(WorkorderListItem):
    title: str
    description: str | None = None
    context_snapshot: list[dict[str, Any]] | None = None
    resolved_at: datetime | None = None
    closed_by: uuid.UUID | None = None
    closed_note: str | None = None


class FamilyWorkorderDetail(WorkorderListItem):
    """家属只读详情：隐私边界要求不返回对话快照。"""

    title: str
    description: str | None = None
    resolved_at: datetime | None = None
    closed_by: uuid.UUID | None = None
    closed_note: str | None = None


class WorkorderListResponse(BaseModel):
    role: str
    items: list[WorkorderListItem]
    total: int
    page: int
    page_size: int


class WorkorderCountsResponse(BaseModel):
    role: str
    unhandled: int


class DispositionRequest(BaseModel):
    plan_text: str = Field(min_length=1, max_length=2000)


class CloseRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)
