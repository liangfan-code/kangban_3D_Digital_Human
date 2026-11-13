"""医院转诊适配器（Day8 段1）。

边界：
- 默认 ``MockHospitalAdapter``：不联网，只生成 ``MOCK-xxxxxxxx`` 单号；
- 仅当 ``Settings.hospital_endpoint`` 非空才启用 HTTP 适配器；
- 失败抛 :class:`HospitalUnavailable`，由调用方记降级并继续建单，
  严禁静默失败（AGENTS.md 实时规则第 6 条同理）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class HospitalUnavailable(RuntimeError):
    """医院接口不可用（网络失败、返回缺单号）。"""


@dataclass(frozen=True)
class Referral:
    referral_id: str
    provider: str


class MockHospitalAdapter:
    """本地演示用：确定性前缀 + 随机后缀，不访问网络。"""

    name = "mock"

    async def create_referral(
        self, *, elder_id: uuid.UUID, event_id: uuid.UUID, level: str
    ) -> Referral:
        suffix = uuid.uuid4().hex[:8].upper()
        return Referral(referral_id=f"MOCK-{suffix}", provider=self.name)


class HttpHospitalAdapter:
    """真实医院接口占位：POST endpoint，期待返回 ``{"referral_id": "..."}``。"""

    name = "http"

    def __init__(self, endpoint: str, *, timeout_seconds: float = 5.0) -> None:
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds

    async def create_referral(
        self, *, elder_id: uuid.UUID, event_id: uuid.UUID, level: str
    ) -> Referral:
        payload: dict[str, Any] = {
            "elder_id": str(elder_id),
            "event_id": str(event_id),
            "level": level,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(self.endpoint, json=payload)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:  # noqa: BLE001 —— 外部接口异常统一转领域异常
            raise HospitalUnavailable(
                f"医院接口失败: {type(exc).__name__}"
            ) from exc
        referral_id = str(data.get("referral_id") or "").strip()
        if not referral_id:
            raise HospitalUnavailable("医院接口未返回 referral_id")
        return Referral(referral_id=referral_id, provider=self.name)


def get_hospital_adapter(settings: Settings | None = None) -> Any:
    """有 endpoint 用 HTTP，否则默认 Mock；不在导入期联网。"""
    settings = settings or get_settings()
    endpoint = (settings.hospital_endpoint or "").strip()
    if endpoint:
        return HttpHospitalAdapter(endpoint)
    return MockHospitalAdapter()


__all__ = [
    "HospitalUnavailable",
    "HttpHospitalAdapter",
    "MockHospitalAdapter",
    "Referral",
    "get_hospital_adapter",
]
