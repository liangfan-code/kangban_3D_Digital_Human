"""Day5.4 Tool 调用死循环防护测试：预算、重复窗口、熔断粘滞与日志边界。"""

import logging

import pytest

from app.services.tool_guard import (
    DUPLICATE_CALL_LIMIT,
    MAX_TOOL_CALLS_PER_TURN,
    ToolLoopGuard,
    ToolLoopTripped,
)


class FakeClock:
    """可控时钟：避免测试真的等待 60 秒。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _guard(clock: FakeClock | None = None) -> ToolLoopGuard:
    return ToolLoopGuard(clock=clock or FakeClock())


def test_budget_allows_eight_calls_then_trips():
    """一轮最多 8 次：前 8 次放行，第 9 次触发 budget_exceeded。"""
    guard = _guard()
    for index in range(MAX_TOOL_CALLS_PER_TURN):
        guard.reserve(f"tool_{index}")

    with pytest.raises(ToolLoopTripped) as exc:
        guard.reserve("tool_8")
    assert exc.value.reason == "budget_exceeded"
    assert exc.value.call_count == MAX_TOOL_CALLS_PER_TURN + 1


def test_same_tool_third_call_trips_duplicate():
    """同一 Tool 60 秒内第 3 次即熔断。"""
    clock = FakeClock()
    guard = _guard(clock)
    guard.reserve("play_media")
    clock.advance(10)
    guard.reserve("play_media")
    clock.advance(10)
    with pytest.raises(ToolLoopTripped) as exc:
        guard.reserve("play_media")
    assert exc.value.reason == "duplicate_calls"
    assert exc.value.tool_name == "play_media"
    assert exc.value.call_count == 3
    assert DUPLICATE_CALL_LIMIT == 3


def test_different_tools_do_not_trigger_duplicate():
    """不同 Tool 各自独立计数，不互相误伤。"""
    guard = _guard()
    for name in ("weather", "media", "health"):
        guard.reserve(name)
    # 各再调一次仍在窗口内，但都未到 3 次
    for name in ("weather", "media", "health"):
        guard.reserve(name)


def test_duplicate_window_expiry_allows_again():
    """超过 60 秒的旧调用滚出窗口，同一 Tool 可重新放行。"""
    clock = FakeClock()
    guard = _guard(clock)
    guard.reserve("weather")
    clock.advance(30)
    guard.reserve("weather")
    clock.advance(61)  # 第一次已超出 60s 窗口
    guard.reserve("weather")


def test_trip_is_sticky_until_reset():
    """熔断后保持锁定：换一个 Tool 也拒绝，reset 后才恢复。"""
    clock = FakeClock()
    guard = _guard(clock)
    for _ in range(DUPLICATE_CALL_LIMIT - 1):
        guard.reserve("play_media")
    with pytest.raises(ToolLoopTripped):
        guard.reserve("play_media")
    assert guard.tripped

    with pytest.raises(ToolLoopTripped) as exc:
        guard.reserve("weather")
    assert exc.value.reason == "duplicate_calls"
    # 锁定期间不再累加调用次数
    assert guard.attempt_count == 3

    guard.reset()
    assert not guard.tripped
    guard.reserve("weather")


def test_trip_logs_metadata_without_arguments(caplog):
    """熔断日志只含名称/次数/原因，不含任何参数内容。"""
    guard = _guard()
    with caplog.at_level(logging.WARNING, logger="app.services.tool_guard"):
        guard.reserve("play_media")
        guard.reserve("play_media")
        with pytest.raises(ToolLoopTripped):
            guard.reserve("play_media")

    records = [r for r in caplog.records if getattr(r, "event", None) == "tool_loop_guard_tripped"]
    assert len(records) == 1
    assert records[0].reason == "duplicate_calls"
    assert records[0].tool_name == "play_media"
    assert records[0].call_count == 3
    # 参数内容不得出现在任何日志字段中
    forbidden = ("args", "arguments", "params", "raw_text", "query")
    assert not any(getattr(records[0], key, None) for key in forbidden)
