"""进程内三态熔断器（Day5.2）：LLM / TTS 连续失败时快速跳过真实调用。

状态机::

    closed ──窗口内连续 N 次失败──▶ open
    open   ──冷却时间到──────────▶ half_open（只放 1 个探针）
    half_open ──成功──▶ closed
    half_open ──失败──▶ open

约束：
- 只在当前进程内计数，多 worker / 多实例各自独立；不假装跨进程共享。
- 用 ``time.monotonic()``，测试可注入假时钟，不依赖真实等待。
- 熔断打开期间调用方**不得**再发起真实请求，必须走现有可见降级
  （LLM → 模板回复，TTS → ``tts_failed`` + 文字），通话不中断。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from enum import StrEnum

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpen(Exception):
    """熔断打开：调用方应立即降级，不再发起真实请求。"""


class CircuitBreaker:
    """按 Provider 各一个实例；所有状态变更都在 ``asyncio.Lock`` 内完成。"""

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 3,
        window_seconds: float = 60.0,
        open_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self._failure_threshold = max(1, failure_threshold)
        self._window_seconds = max(0.0, window_seconds)
        self._open_seconds = max(0.0, open_seconds)
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._failures: deque[float] = deque()
        self._opened_at = 0.0
        self._probe_in_flight = False
        self._lock = asyncio.Lock()

    # ---- 对外接口 ----

    @asynccontextmanager
    async def guard(self) -> AsyncIterator[None]:
        """包住一次真实调用；成功/异常自动记账，熔断时抛 ``CircuitOpen``。"""
        is_probe = await self._acquire()
        try:
            yield
        except asyncio.CancelledError:
            # 任务取消不算 Provider 失败，释放探针名额即可
            await self._release(ok=None, probe=is_probe)
            raise
        except BaseException:
            await self._release(ok=False, probe=is_probe)
            raise
        else:
            await self._release(ok=True, probe=is_probe)

    def snapshot(self) -> dict[str, float | int | str]:
        """供日志与测试观察，不加锁（只读原子字段）。"""
        now = self._clock()
        remaining = 0.0
        if self._state is CircuitState.OPEN:
            remaining = max(0.0, self._open_seconds - (now - self._opened_at)) * 1000
        return {
            "provider": self.name,
            "state": self._state.value,
            "failures": len(self._failures),
            "open_remaining_ms": round(remaining, 1),
        }

    # ---- 状态迁移（全部在锁内）----

    async def _acquire(self) -> bool:
        """返回是否本次为 half-open 探针；熔断打开时抛 CircuitOpen。"""
        async with self._lock:
            now = self._clock()
            if self._state is CircuitState.OPEN:
                if now - self._opened_at < self._open_seconds:
                    raise CircuitOpen(f"{self.name} 熔断中，冷却 {self._open_seconds}s")
                self._set_state(CircuitState.HALF_OPEN)
                self._probe_in_flight = True
                return True
            if self._state is CircuitState.HALF_OPEN:
                if self._probe_in_flight:
                    raise CircuitOpen(f"{self.name} 半开探针进行中")
                self._probe_in_flight = True
                return True
            return False

    async def _release(self, *, ok: bool | None, probe: bool) -> None:
        async with self._lock:
            now = self._clock()
            if probe:
                self._probe_in_flight = False
            if ok is None:
                return
            if ok:
                self._failures.clear()
                if self._state is not CircuitState.CLOSED:
                    self._set_state(CircuitState.CLOSED)
                return

            if self._state is CircuitState.HALF_OPEN:
                self._open(now)
                return
            self._failures.append(now)
            self._trim(now)
            if len(self._failures) >= self._failure_threshold:
                self._open(now)

    def _trim(self, now: float) -> None:
        while self._failures and now - self._failures[0] > self._window_seconds:
            self._failures.popleft()

    def _open(self, now: float) -> None:
        self._opened_at = now
        self._failures.clear()
        if self._state is not CircuitState.OPEN:
            self._set_state(CircuitState.OPEN)

    def _set_state(self, state: CircuitState) -> None:
        previous, self._state = self._state, state
        log = logger.warning if state is CircuitState.OPEN else logger.info
        log(
            "circuit_state_changed",
            extra={
                "event": "circuit_state_changed",
                "provider": self.name,
                "from": previous.value,
                "to": state.value,
                "failure_threshold": self._failure_threshold,
                "open_seconds": self._open_seconds,
            },
        )


# ---- 进程内单例（按 Provider 名称缓存）----

_breakers: dict[str, CircuitBreaker] = {}


def get_breaker(name: str) -> CircuitBreaker:
    breaker = _breakers.get(name)
    if breaker is None:
        settings = get_settings()
        breaker = CircuitBreaker(
            name,
            failure_threshold=settings.breaker_failure_threshold,
            window_seconds=settings.breaker_window_seconds,
            open_seconds=settings.breaker_open_seconds,
        )
        _breakers[name] = breaker
    return breaker


def get_llm_breaker() -> CircuitBreaker:
    return get_breaker("llm")


def get_tts_breaker() -> CircuitBreaker:
    return get_breaker("tts")


def reset_circuit_breakers() -> None:
    """测试用：清空单例，避免用例间状态串扰。"""
    _breakers.clear()
