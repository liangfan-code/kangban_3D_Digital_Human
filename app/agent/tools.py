"""Tool 注册表（Day6.3）：统一 Schema、服务端上下文与降级语义。

约束：
- ``elder_id`` 只来自 ``ToolContext``（服务端 Runtime Context），模型参数即使带了
  同名字段也会被忽略；
- 高危副作用（通知家属）只入 HITL 队列，不直接执行；
- Tool 失败返回可朗读的降级话术，不向长者抛异常。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.agent.mcp_client import McpWeatherClient
from app.services.hitl import hitl_queue


@dataclass(frozen=True)
class ToolContext:
    """服务端注入的执行上下文，模型不可见、不可改。"""

    elder_id: str | None = None
    device_id: str = ""
    room_type: str = "single"


@dataclass
class ToolResult:
    ok: bool
    content: str
    client_events: list[dict[str, Any]] = field(default_factory=list)
    degraded: str | None = None


ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler


class ToolRegistry:
    """按名字分发 Tool；同时生成 OpenAI tools 协议描述。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            }
            for spec in self._tools.values()
        ]

    async def call(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(False, "这个功能我暂时还不会用。", degraded="unknown_tool")
        try:
            return await spec.handler(args, ctx)
        except Exception as exc:  # noqa: BLE001 —— Tool 异常不穿透到语音链路
            return ToolResult(
                False,
                "这个功能暂时不可用，我先帮您记着。",
                degraded=f"tool_error:{type(exc).__name__}",
            )


def _weather_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "城市名，可省略，默认本地"},
        },
        "required": [],
    }


def _empty_parameters() -> dict[str, Any]:
    return {"type": "object", "properties": {}, "required": []}


def _media_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "genre": {"type": "string", "description": "越剧、京剧、黄梅戏、老歌、电影等"},
        },
        "required": [],
    }


def _notify_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "要转告家属的内容"},
        },
        "required": ["message"],
    }


def build_default_registry(
    weather_client: McpWeatherClient | None = None,
) -> ToolRegistry:
    """构建默认 Tool 集合；weather 通过 MCP，其余为 Day6 的确定性实现。"""
    client = weather_client or McpWeatherClient()
    registry = ToolRegistry()

    async def weather_get(args: dict[str, Any], ctx: ToolContext) -> ToolResult:  # noqa: ARG001
        result = await client.call_weather(str(args.get("city") or "本地"))
        return ToolResult(
            ok=result.ok,
            content=result.content,
            degraded=None if result.ok else "mcp_unavailable",
        )

    async def medication_query(args: dict[str, Any], ctx: ToolContext) -> ToolResult:  # noqa: ARG001
        # Day7 接真实用药计划；当前明确告知尚未接入，绝不编造药名/剂量
        return ToolResult(
            ok=False,
            content="用药计划还没接入，我先帮您记着，等护士站确认后再提醒您。",
            degraded="medication_not_configured",
        )

    async def media_play(args: dict[str, Any], ctx: ToolContext) -> ToolResult:  # noqa: ARG001
        genre = str(args.get("genre") or "戏曲")
        return ToolResult(
            ok=True,
            content=f"好，给您放一段{genre}。",
            client_events=[{"type": "control", "action": "media_play", "genre": genre}],
        )

    async def media_next(args: dict[str, Any], ctx: ToolContext) -> ToolResult:  # noqa: ARG001
        return ToolResult(
            ok=True,
            content="好，给您换一段。",
            client_events=[{"type": "control", "action": "media_next"}],
        )

    async def media_stop(args: dict[str, Any], ctx: ToolContext) -> ToolResult:  # noqa: ARG001
        return ToolResult(
            ok=True,
            content="好，我停下了。",
            client_events=[{"type": "control", "action": "stop"}],
        )

    async def notify_family(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        message = str(args.get("message") or "家属通知")
        hitl_queue.enqueue(
            kind="notify_family",
            elder_id=ctx.elder_id,
            device_id=ctx.device_id,
            summary="家属通知待确认",
            detail=message,
        )
        return ToolResult(
            ok=True,
            content="这件事需要工作人员确认，我已经帮您转过去了，确认后会通知家属。",
            client_events=[{"type": "hitl", "kind": "notify_family", "status": "pending"}],
        )

    registry.register(
        ToolSpec("weather_get", "查询本地天气（本地示例数据）", _weather_parameters(), weather_get)
    )
    registry.register(
        ToolSpec(
            "medication_query",
            "查询今天的用药计划与服药确认",
            _empty_parameters(),
            medication_query,
        )
    )
    registry.register(
        ToolSpec("media_play", "点播戏曲、老歌或电影", _media_parameters(), media_play)
    )
    registry.register(
        ToolSpec("media_next", "切换到下一段媒体", _empty_parameters(), media_next)
    )
    registry.register(
        ToolSpec("media_stop", "停止当前媒体播放", _empty_parameters(), media_stop)
    )
    registry.register(
        ToolSpec(
            "notify_family",
            "通知家属（需人工确认，不会直接发送）",
            _notify_parameters(),
            notify_family,
        )
    )
    return registry
