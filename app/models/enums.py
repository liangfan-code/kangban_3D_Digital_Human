"""字符串枚举定义：数据库使用 VARCHAR + CHECK，不使用 PostgreSQL 原生枚举。"""

from enum import StrEnum


class RoomStatus(StrEnum):
    ACTIVE = "active"
    MAINTENANCE = "maintenance"
    INACTIVE = "inactive"


class DeviceStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    DISABLED = "disabled"
    RETIRED = "retired"


class DeviceType(StrEnum):
    TABLET = "tablet"


class ElderStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    DISCHARGED = "discharged"


class Gender(StrEnum):
    FEMALE = "female"
    MALE = "male"
    OTHER = "other"
    UNKNOWN = "unknown"


class MemoryCategory(StrEnum):
    PREFERENCE = "preference"
    HEALTH = "health"
    EVENT = "event"
    RELATIONSHIP = "relationship"
    ROUTINE = "routine"
    OTHER = "other"


class MemoryVisibility(StrEnum):
    PRIVATE = "private"
    CARE_TEAM = "care_team"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    SUPPRESSED = "suppressed"
    DELETED = "deleted"


class VoiceprintStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class MedicationPlanStatus(StrEnum):
    PLANNED = "planned"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class MediaType(StrEnum):
    OPERA = "opera"
    MOVIE = "movie"


class MediaCopyrightStatus(StrEnum):
    UNKNOWN = "unknown"
    LICENSED = "licensed"
    TEACHING = "teaching"
    PUBLIC_DOMAIN = "public_domain"
    RESTRICTED = "restricted"
    EXPIRED = "expired"


class MediaStatus(StrEnum):
    ACTIVE = "active"
    HIDDEN = "hidden"


class WorkorderStatus(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"


class WorkorderPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class IdentitySource(StrEnum):
    DEVICE_ROOM = "device_room"
    VOICEPRINT = "voiceprint"
    EXPLICIT_CONFIRMATION = "explicit_confirmation"
    STAFF_TOKEN = "staff_token"
    ANONYMOUS = "anonymous"


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    ENDED = "ended"
    ABANDONED = "abandoned"


# ---- Day2：身份与权限 ----


class AccountRole(StrEnum):
    NURSE = "nurse"
    DOCTOR = "doctor"
    FAMILY = "family"
    ADMIN = "admin"


class AccountStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    LOCKED = "locked"


class DeviceTokenKind(StrEnum):
    REGISTER = "register"
    ACCESS = "access"


class AssignmentStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


# ---- Day2 v2：健康档案 ----


class HealthRecordCategory(StrEnum):
    VITAL = "vital"  # 体征：血压/血糖/体重/体温…
    BODY_PART = "body_part"  # 身体部位：头/眼/耳/心/肺/消化/关节/皮肤…
    MEDICATION = "medication"  # 当前用药、服药时间、剂量
    CHRONIC = "chronic"  # 慢性病标注：高血压/糖尿病…
    MAJOR = "major"  # 重大疾病·隐私级


class HealthSeverity(StrEnum):
    NORMAL = "normal"
    ATTENTION = "attention"
    WARNING = "warning"


class HealthDataLevel(StrEnum):
    BASIC = "basic"  # 护工经 ACL 授权可见
    CONFIDENTIAL = "confidential"  # 隐私级（major 类），护工不可见


class HealthAuditAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
