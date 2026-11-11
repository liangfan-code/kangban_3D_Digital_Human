"""用药计划请求/响应契约。

- 创建/修改只允许白名单字段，额外字段（plan_id/elder_id/status 等）一律 422；
- 医生/管理员视图含内部字段；护工视图含 internal_note 但不含账号 UUID；
- 家属视图字段白名单，绝不含 internal_note / 账号 UUID / workorder_id。
"""

import re
import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class MedicationSchedule(BaseModel):
    """固定形状：times=["HH:MM", ...]，weekdays=[0..6, ...]；weekdays 为空表示每天。"""

    model_config = ConfigDict(extra="forbid")

    times: list[str] = Field(min_length=1, max_length=6)
    weekdays: list[int] = Field(default_factory=list, max_length=7)

    @field_validator("times")
    @classmethod
    def _validate_times(cls, value: list[str]) -> list[str]:
        for item in value:
            if not isinstance(item, str) or not _TIME_RE.match(item):
                raise ValueError("times 必须是 24 小时制 HH:MM")
        if len(set(value)) != len(value):
            raise ValueError("times 不能重复")
        return sorted(value)

    @field_validator("weekdays")
    @classmethod
    def _validate_weekdays(cls, value: list[int]) -> list[int]:
        if any(day < 0 or day > 6 for day in value):
            raise ValueError("weekdays 取值范围为 0..6")
        if len(set(value)) != len(value):
            raise ValueError("weekdays 不能重复")
        return sorted(value)


class MedicationPlanCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    drug_name: str = Field(min_length=1, max_length=160)
    dose: str = Field(min_length=1, max_length=100)
    schedule: MedicationSchedule
    instructions: str | None = Field(default=None, max_length=2000)
    internal_note: str | None = Field(default=None, max_length=2000)
    start_date: date
    end_date: date | None = None

    @model_validator(mode="after")
    def _validate_date_range(self) -> "MedicationPlanCreate":
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date 不能早于 start_date")
        return self


class MedicationPlanUpdate(BaseModel):
    """PUT 局部更新：只允许可编辑字段；未提供字段保持原值。"""

    model_config = ConfigDict(extra="forbid")

    drug_name: str | None = Field(default=None, min_length=1, max_length=160)
    dose: str | None = Field(default=None, min_length=1, max_length=100)
    schedule: MedicationSchedule | None = None
    instructions: str | None = Field(default=None, max_length=2000)
    internal_note: str | None = Field(default=None, max_length=2000)
    start_date: date | None = None
    end_date: date | None = None


class MedicationPlanDoctorView(BaseModel):
    """医生/管理员视图：含内部备注与账号/工单关联。"""

    model_config = ConfigDict(from_attributes=True)

    plan_id: uuid.UUID
    elder_id: uuid.UUID
    drug_name: str
    dose: str
    schedule: dict[str, Any]
    instructions: str | None = None
    internal_note: str | None = None
    start_date: date
    end_date: date | None = None
    status: str
    created_by_account_id: uuid.UUID | None = None
    updated_by_account_id: uuid.UUID | None = None
    confirmed_at: datetime | None = None
    confirmed_by_account_id: uuid.UUID | None = None
    workorder_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class MedicationPlanNurseView(BaseModel):
    """护工视图：含内部备注（照护需要），不含任何账号 UUID。"""

    model_config = ConfigDict(from_attributes=True)

    plan_id: uuid.UUID
    elder_id: uuid.UUID
    drug_name: str
    dose: str
    schedule: dict[str, Any]
    instructions: str | None = None
    internal_note: str | None = None
    start_date: date
    end_date: date | None = None
    status: str
    confirmed_at: datetime | None = None
    workorder_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class MedicationPlanFamilyView(BaseModel):
    """家属只读视图：字段白名单，不含内部备注、账号 UUID、工单关联。"""

    model_config = ConfigDict(from_attributes=True)

    plan_id: uuid.UUID
    elder_id: uuid.UUID
    drug_name: str
    dose: str
    schedule: dict[str, Any]
    instructions: str | None = None
    start_date: date
    end_date: date | None = None
    status: str
    confirmed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class FamilyPlanHistoryItem(BaseModel):
    """家属修改记录：只透出角色与字段变更，internal_note 整条丢弃。"""

    time: datetime
    actor: str | None = None
    plan_version: int | None = None
    changes: list[dict[str, Any]] = Field(default_factory=list)
