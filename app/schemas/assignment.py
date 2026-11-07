"""授权关联表管理请求/响应结构（护工-楼栋 / 负责医生-长者 / 家属-长者）。"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class BuildingAssignmentCreate(BaseModel):
    account_id: uuid.UUID
    building_code: str = Field(min_length=1, max_length=32)


class BuildingAssignmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    assignment_id: uuid.UUID
    account_id: uuid.UUID
    building_code: str
    status: str
    created_at: datetime
    updated_at: datetime


class DoctorAssignmentCreate(BaseModel):
    account_id: uuid.UUID
    elder_id: uuid.UUID
    valid_until: datetime | None = None


class DoctorAssignmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    assignment_id: uuid.UUID
    account_id: uuid.UUID
    elder_id: uuid.UUID
    valid_until: datetime | None
    status: str
    created_at: datetime
    updated_at: datetime


class FamilyLinkCreate(BaseModel):
    account_id: uuid.UUID
    elder_id: uuid.UUID
    relation: str | None = Field(default=None, max_length=32)
    valid_until: datetime | None = None


class FamilyLinkResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    assignment_id: uuid.UUID
    account_id: uuid.UUID
    elder_id: uuid.UUID
    relation: str | None
    valid_until: datetime | None
    status: str
    created_at: datetime
    updated_at: datetime
