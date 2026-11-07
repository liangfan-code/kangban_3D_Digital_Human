"""会话状态存储（Day4.4）：Redis 为主，进程内字典兜底。

键 ``kangban:session:{thread_id}``，值为 ``SessionState`` JSON，TTL 由
``SESSION_STATE_TTL_SECONDS`` 控制（默认 24h）。Redis 不可用时自动降级为
进程内字典，任何异常都不得中断通话（AGENTS：缺配置时应用照常启动）。

安全边界：本模块只承载摘要/槽位/轮次等上下文，不参与身份判定；
``elder_id`` 仍只来自服务端设备/声纹。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class MedicationSlot(BaseModel):
    """用药意图槽位（只记录，不执行提醒）。"""

    drug: str | None = None
    time: str | None = None
    confirmed: bool | None = None


class MediaSlot(BaseModel):
    """戏曲/媒体控制槽位（只记录，不执行播放）。"""

    action: str | None = None
    keyword: str | None = None


class IdentitySlot(BaseModel):
    """身份确认槽位（只记录，永不修改 verified_elder_id）。"""

    candidate: str | None = None
    confirmed: bool | None = None


class SessionSlots(BaseModel):
    intent: str | None = None
    medication: MedicationSlot = Field(default_factory=MedicationSlot)
    media: MediaSlot = Field(default_factory=MediaSlot)
    identity: IdentitySlot = Field(default_factory=IdentitySlot)


class SessionState(BaseModel):
    thread_id: str
    summary: str = ""
    summary_upto_seq: int = 0
    turn_count: int = 0
    last_active: str = ""
    slots: SessionSlots = Field(default_factory=SessionSlots)


def new_state(thread_id: str) -> SessionState:
    return SessionState(thread_id=thread_id, last_active=datetime.now(UTC).isoformat())


class SessionStateStore:
    """线程会话状态读写；``force_memory=True`` 供测试使用，完全不碰 Redis。"""

    def __init__(self, *, force_memory: bool = False) -> None:
        self._memory: dict[str, SessionState] = {}
        self._lock = asyncio.Lock()
        self._client = None
        self._force_memory = force_memory
        self._redis_checked = False
        self._redis_ok = False
        self._warned = False

    @staticmethod
    def _key(thread_id: str) -> str:
        return f"kangban:session:{thread_id}"

    async def _redis(self):
        """惰性连接并 ping；失败一次即永久降级为内存（不再重试，避免每轮卡 0.5s）。"""
        settings = get_settings()
        if self._force_memory or not settings.session_state_enabled:
            return None
        if self._redis_checked:
            return self._client if self._redis_ok else None
        self._redis_checked = True
        if not settings.redis_url:
            return None
        try:
            import redis.asyncio as redis_async

            client = redis_async.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=0.5,
                socket_timeout=0.5,
            )
            await client.ping()
            self._client = client
            self._redis_ok = True
        except Exception as exc:  # noqa: BLE001 —— 缓存不可用绝不阻断通话
            self._degrade("connect", exc)
        return self._client if self._redis_ok else None

    def _degrade(self, action: str, exc: Exception) -> None:
        self._redis_ok = False
        if not self._warned:
            self._warned = True
            logger.warning(
                "session state redis unavailable, fallback to memory",
                extra={
                    "event": "session_state_redis_unavailable",
                    "action": action,
                    "error_type": type(exc).__name__,
                },
            )

    async def load(self, thread_id: str) -> SessionState:
        key = self._key(thread_id)
        client = await self._redis()
        if client is not None:
            try:
                raw = await client.get(key)
                if raw:
                    return SessionState.model_validate_json(raw)
            except Exception as exc:  # noqa: BLE001
                self._degrade("load", exc)
        return self._memory.get(key) or new_state(thread_id)

    async def save(self, state: SessionState) -> None:
        key = self._key(state.thread_id)
        state.last_active = datetime.now(UTC).isoformat()
        client = await self._redis()
        if client is not None:
            try:
                await client.set(
                    key,
                    state.model_dump_json(),
                    ex=get_settings().session_state_ttl_seconds,
                )
                self._memory[key] = state  # 缓存一份，Redis 中途掉线也能继续
                return
            except Exception as exc:  # noqa: BLE001
                self._degrade("save", exc)
        self._memory[key] = state

    @asynccontextmanager
    async def mutate(self, thread_id: str) -> AsyncIterator[SessionState]:
        """读-改-写加进程内锁，降低并发覆盖；异常时不落盘。"""
        async with self._lock:
            state = await self.load(thread_id)
            yield state
            await self.save(state)

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001 —— 关闭失败不影响退出
                pass
            self._client = None
            self._redis_checked = False
            self._redis_ok = False


_store: SessionStateStore | None = None


def get_session_store() -> SessionStateStore:
    """全局单例：同一进程内所有 VoiceSession 共享状态与降级缓存。"""
    global _store
    if _store is None:
        _store = SessionStateStore()
    return _store


async def close_session_store() -> None:
    global _store
    if _store is not None:
        await _store.close()
        _store = None
