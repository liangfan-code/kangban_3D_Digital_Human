"""服药任务单/事实事件契约。

- 家属服药历史走字段白名单，绝不返回 internal_note / skip_reason / workorder_id /
  actor_account_id / device_id / payload 原文；
- 员工核对视图可返回 skip_reason 与关联 workorder_id。
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class FamilyMedicationEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_type: str
    source: str
    created_at: datetime


class FamilyMedicationHistoryItem(BaseModel):
    order_id: uuid.UUID
    plan_id: uuid.UUID
    drug_name: str
    dose: str
    scheduled_at: datetime
    status: str
    attempt_count: int
    confirmed_at: datetime | None = None
    confirmed_source: str | None = None
    events: list[FamilyMedicationEventOut] = Field(default_factory=list)


class FamilyMedicationHistoryResponse(BaseModel):
    items: list[FamilyMedicationHistoryItem]
    total: int
    page: int
    page_size: int


class StaffMedicationOrderItem(BaseModel):
    order_id: uuid.UUID
    plan_id: uuid.UUID
    drug_name: str
    dose: str
    scheduled_at: datetime
    status: str
    attempt_count: int
    confirmed_at: datetime | None = None
    confirmed_source: str | None = None
    skip_reason: str | None = None
    workorder_id: uuid.UUID | None = None
    events: list[FamilyMedicationEventOut] = Field(default_factory=list)


class StaffMedicationOrderResponse(BaseModel):
    items: list[StaffMedicationOrderItem]
    total: int
    page: int
    page_size: int
