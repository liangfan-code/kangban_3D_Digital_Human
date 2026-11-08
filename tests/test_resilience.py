"""Day5.2 熔断器状态机测试（假时钟，不依赖真实等待）。"""

import asyncio

import pytest

from app.services.resilience import (
    CircuitBreaker,
    CircuitOpen,
    CircuitState,
    get_breaker,
    get_llm_breaker,
    get_tts_breaker,
    reset_circuit_breakers,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _breaker(clock: FakeClock, **kwargs) -> CircuitBreaker:
    params = {"failure_threshold": 3, "window_seconds": 60.0, "open_seconds": 30.0}
    params.update(kwargs)
    return CircuitBreaker("llm", clock=clock, **params)


async def _fail(breaker: CircuitBreaker) -> None:
    with pytest.raises(RuntimeError):
        async with breaker.guard():
            raise RuntimeError("provider boom")


async def test_opens_after_threshold_failures():
    """窗口内连续 3 次失败 → 打开；失败计数清空。"""
    clock = FakeClock()
    breaker = _breaker(clock)
    await _fail(breaker)
    await _fail(breaker)
    assert breaker.snapshot()["state"] == CircuitState.CLOSED

    await _fail(breaker)
    snapshot = breaker.snapshot()
    assert snapshot["state"] == CircuitState.OPEN
    assert snapshot["failures"] == 0


async def test_open_rejects_calls_during_cooldown():
    """打开期间直接抛 CircuitOpen，调用方不得再发起真实请求。"""
    clock = FakeClock()
    breaker = _breaker(clock)
    for _ in range(3):
        await _fail(breaker)

    with pytest.raises(CircuitOpen):
        async with breaker.guard():
            pass


async def test_half_open_allows_only_one_probe_and_success_closes():
    """冷却结束后放行单个探针；并发调用被拒；探针成功 → closed。"""
    clock = FakeClock()
    breaker = _breaker(clock)
    for _ in range(3):
        await _fail(breaker)
    clock.advance(31)

    entered = asyncio.Event()
    release = asyncio.Event()

    async def probe() -> None:
        async with breaker.guard():
            entered.set()
            await release.wait()

    task = asyncio.create_task(probe())
    await entered.wait()
    assert breaker.snapshot()["state"] == CircuitState.HALF_OPEN

    with pytest.raises(CircuitOpen):
        async with breaker.guard():
            pass

    release.set()
    await task
    assert breaker.snapshot()["state"] == CircuitState.CLOSED
    assert breaker.snapshot()["failures"] == 0


async def test_half_open_failure_reopens_immediately():
    """半开探针失败 → 立即重新打开，并重新开始冷却。"""
    clock = FakeClock()
    breaker = _breaker(clock)
    for _ in range(3):
        await _fail(breaker)
    clock.advance(31)

    await _fail(breaker)
    snapshot = breaker.snapshot()
    assert snapshot["state"] == CircuitState.OPEN
    assert snapshot["open_remaining_ms"] == pytest.approx(30_000, abs=1)

    with pytest.raises(CircuitOpen):
        async with breaker.guard():
            pass


async def test_failures_outside_window_do_not_accumulate():
    """窗口外的旧失败被裁剪，不会与新失败叠加触发熔断。"""
    clock = FakeClock()
    breaker = _breaker(clock, failure_threshold=2, window_seconds=10)
    await _fail(breaker)
    clock.advance(20)
    await _fail(breaker)
    assert breaker.snapshot()["state"] == CircuitState.CLOSED
    assert breaker.snapshot()["failures"] == 1


async def test_success_resets_failure_count():
    """成功调用清零失败计数（连续失败语义，非累计）。"""
    clock = FakeClock()
    breaker = _breaker(clock)
    await _fail(breaker)
    await _fail(breaker)
    async with breaker.guard():
        pass
    assert breaker.snapshot()["failures"] == 0
    assert breaker.snapshot()["state"] == CircuitState.CLOSED


async def test_singletons_are_per_provider_and_resettable():
    """进程内单例按 Provider 名隔离；reset 后重建并回到 closed。"""
    reset_circuit_breakers()
    assert get_llm_breaker().name == "llm"
    assert get_tts_breaker().name == "tts"
    assert get_llm_breaker() is not get_tts_breaker()
    assert get_breaker("llm") is get_llm_breaker()

    for _ in range(3):
        await _fail(get_llm_breaker())
    assert get_llm_breaker().snapshot()["state"] == CircuitState.OPEN
    assert get_tts_breaker().snapshot()["state"] == CircuitState.CLOSED

    reset_circuit_breakers()
    assert get_llm_breaker().snapshot()["state"] == CircuitState.CLOSED
