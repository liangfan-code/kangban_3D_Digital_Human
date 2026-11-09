"""Tool 调用死循环防护（Day5.4）。

三道硬约束（Day6 接入工具调度器后直接生效，业务代码不得绕过）：

1. 全局预算：一轮对话最多 ``MAX_TOOL_CALLS_PER_TURN`` 次 Tool 调用；
2. 重复防护：同一 Tool 在 ``DUPLICATE_WINDOW_SECONDS`` 秒内被调
   ``DUPLICATE_CALL_LIMIT`` 次即熔断；
3. 熔断后保持锁定（fail-closed），只能通过 ``reset()`` 开启新一轮。

只记录 Tool 名称、次数与原因，不记录参数内容，守住"不落盘正文/隐私"边界。
"""

import time
from collections.abc import Callable

from app.core.logging import get_logger

logger = get_logger(__name__)

MAX_TOOL_CALLS_PER_TURN = 8
DUPLICATE_WINDOW_SECONDS = 60.0
DUPLICATE_CALL_LIMIT = 3

REASON_BUDGET = "budget_exceeded"
REASON_DUPLICATE = "duplicate_calls"


class ToolLoopTripped(Exception):
    """Tool 调用被熔断：调度器捕获后走降级话术，不再继续循环。"""

    def __init__(self, reason: str, tool_name: str, call_count: int) -> None:
        super().__init__(f"tool loop guard tripped: {reason} ({tool_name}, {call_count})")
        self.reason = reason
        self.tool_name = tool_name
        self.call_count = call_count


class ToolLoopGuard:
    """单轮对话的 Tool 调用守卫；每轮新建实例，轮次结束即丢弃。"""

    def __init__(
        self,
        max_calls: int = MAX_TOOL_CALLS_PER_TURN,
        window_seconds: float = DUPLICATE_WINDOW_SECONDS,
        duplicate_limit: int = DUPLICATE_CALL_LIMIT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_calls = max_calls
        self._window_seconds = window_seconds
        self._duplicate_limit = duplicate_limit
        self._clock = clock
        self._attempts = 0
        self._stamps: dict[str, list[float]] = {}
        self._tripped_reason: str | None = None

    @property
    def tripped(self) -> bool:
        return self._tripped_reason is not None

    @property
    def attempt_count(self) -> int:
        return self._attempts

    def reserve(self, tool_name: str) -> int:
        """预占一次 Tool 调用额度；成功返回本轮到目前的调用次数。

        触发预算或重复防护时抛 ``ToolLoopTripped``，并保持熔断状态。
        """
        if self._tripped_reason is not None:
            raise ToolLoopTripped(self._tripped_reason, tool_name, self._attempts)

        self._attempts += 1
        if self._attempts > self._max_calls:
            self._trip(REASON_BUDGET, tool_name)

        now = self._clock()
        stamps = [
            stamp
            for stamp in self._stamps.get(tool_name, [])
            if now - stamp < self._window_seconds
        ]
        stamps.append(now)
        self._stamps[tool_name] = stamps
        if len(stamps) >= self._duplicate_limit:
            self._trip(REASON_DUPLICATE, tool_name)
        return self._attempts

    def reset(self) -> None:
        """开启新一轮：清空计数、时间窗与熔断状态。"""
        self._attempts = 0
        self._stamps.clear()
        self._tripped_reason = None

    def _trip(self, reason: str, tool_name: str) -> None:
        self._tripped_reason = reason
        logger.warning(
            "tool loop guard tripped",
            extra={
                "event": "tool_loop_guard_tripped",
                "reason": reason,
                "tool_name": tool_name,
                "call_count": self._attempts,
            },
        )
        raise ToolLoopTripped(reason, tool_name, self._attempts)
