"""健康档案请求/响应契约（Day2 v2）。

分级与 ACL 语义：
- category=major 的记录 data_level 一律 confidential，由服务端派生，不信任客户端；
- recorded_role 由服务端注入（取当前身份角色），客户端不可传。
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import (
    HealthDataLevel,
    HealthRecordCategory,
    HealthSeverity,
)


class HealthRecordCreate(BaseModel):
    category: HealthRecordCategory
    item_key: str = Field(min_length=1, max_length=64)
    value_text: str = Field(min_length=1, max_length=2000)
    severity: HealthSeverity = HealthSeverity.NORMAL


class HealthRecordUpdate(BaseModel):
    item_key: str | None = Field(default=None, min_length=1, max_length=64)
    value_text: str | None = Field(default=None, min_length=1, max_length=2000)
    severity: HealthSeverity | None = None


class HealthRecordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    record_id: uuid.UUID
    elder_id: uuid.UUID
    category: HealthRecordCategory
    item_key: str
    value_text: str
    severity: HealthSeverity
    data_level: HealthDataLevel
    recorded_by: uuid.UUID
    recorded_role: str
    created_at: datetime
    updated_at: datetime


class HealthProfileResponse(BaseModel):
    """按查看者权限过滤后的分组档案；groups 只含查看者可见的分区。"""

    elder_id: uuid.UUID
    viewer_level: HealthDataLevel | None  # basic / confidential / None=无任何授权
    groups: dict[str, list[HealthRecordResponse]]
    # 长者安置上下文（房间/楼栋/在用设备）
    elder_name: str
    room_label: str | None
    building_code: str | None
    device_codes: list[str]


class HealthAuditLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    log_id: uuid.UUID
    record_id: uuid.UUID
    elder_id: uuid.UUID
    action: str
    old_value: dict | None
    new_value: dict | None
    operator_id: uuid.UUID
    created_at: datetime


class HealthGrantCreate(BaseModel):
    account_id: uuid.UUID
    elder_id: uuid.UUID
    can_view_basic: bool = False
    can_edit_basic: bool = False
    can_view_confidential: bool = False
    can_edit_confidential: bool = False


class HealthGrantUpdate(BaseModel):
    can_view_basic: bool | None = None
    can_edit_basic: bool | None = None
    can_view_confidential: bool | None = None
    can_edit_confidential: bool | None = None


class HealthGrantResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    grant_id: uuid.UUID
    account_id: uuid.UUID
    elder_id: uuid.UUID
    can_view_basic: bool
    can_edit_basic: bool
    can_view_confidential: bool
    can_edit_confidential: bool
    status: str
    granted_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


class HealthSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    summary_id: uuid.UUID
    elder_id: uuid.UUID
    summary_text: str
    version: int
    generated_by: uuid.UUID
    acknowledged_by: uuid.UUID | None
    acknowledged_at: datetime | None
    created_at: datetime


class AiAnalysisResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    analysis_id: uuid.UUID
    elder_id: uuid.UUID
    analysis_text: str
    model_name: str
    prompt_version: str
    requested_by: uuid.UUID
    created_at: datetime


class PendingMeasureItem(BaseModel):
    """今日待录入清单项：某长者某个体征类 item_key 今日尚未录入。"""

    elder_id: uuid.UUID
    elder_name: str
    room_label: str | None
    item_key: str


class ConfigFlagsResponse(BaseModel):
    """前端功能开关：AI 服务是否已配置（决定按钮置灰）。"""

    ai_analysis_enabled: bool
