"""Tool 注册表（Day6.3）：统一 Schema、服务端上下文与降级语义。

约束：
- ``elder_id`` 只来自 ``ToolContext``（服务端 Runtime Context），模型参数即使带了
  同名字段也会被忽略；
- 高危副作用（通知家属）只入 HITL 队列，不直接执行；
- Tool 失败返回可朗读的降级话术，不向长者抛异常。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.agent.mcp_client import McpHost, McpToolInfo, get_mcp_host
from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.graphs.medication import submit_reply
from app.router.executor import time_reply
from app.router.signals import detect_medication_reply
from app.services import medication_order as order_service
from app.services import memory as memory_service
from app.services.hitl import hitl_queue
from app.services.medication_plan import (
    format_plan_reply,
    pick_current_slot,
    today_plans,
)

logger = get_logger(__name__)


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
    # MCP 来源 Server；本地确定性工具为 None。放末尾保持原位置参数兼容。
    mcp_server: str | None = None


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


def _medication_confirm_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "reply": {
                "type": "string",
                "description": "长者对服药提醒的原话，如「吃过了」「还没吃」「等会儿吃」",
            },
        },
        "required": [],
    }


def _memory_recall_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "长者想回忆的事情或关键词，如「糖尿病」「孙女」「喜欢吃什么」",
            },
        },
        "required": ["query"],
    }


def _elder_uuid(ctx: ToolContext) -> uuid.UUID | None:
    """只认服务端 Runtime Context；模型传入的 elder_id 一律忽略。"""
    if not ctx.elder_id:
        return None
    try:
        return uuid.UUID(str(ctx.elder_id))
    except (TypeError, ValueError):
        return None


def _mcp_parameters(schema: Any) -> dict[str, Any]:
    """MCP 入参 Schema 兜底：非 dict 或为空时给空对象 Schema。"""
    if isinstance(schema, dict) and schema:
        return schema
    return _empty_parameters()


def _mcp_handler(host: McpHost, info: McpToolInfo) -> ToolHandler:
    """把一次 MCP 调用映射成 ``ToolResult``，失败带 Server 维度降级标记。"""

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:  # noqa: ARG001
        result = await host.call(info.exposed_name, args)
        server_id = result.server_id or info.server_id
        if result.ok:
            return ToolResult(ok=True, content=result.content, mcp_server=server_id)
        return ToolResult(
            ok=False,
            content=result.content,
            degraded=f"mcp_unavailable:{server_id}",
            mcp_server=server_id,
        )

    return handler


def build_default_registry(mcp_host: McpHost | None = None) -> ToolRegistry:
    """构建默认 Tool 集合：本地确定性工具 + MCP 动态发现工具。"""
    host = mcp_host or get_mcp_host()
    registry = ToolRegistry()

    # MCP 动态工具：未启动或发现失败时快照为空，此时不注册任何远端工具
    for info in host.tools_snapshot():
        registry.register(
            ToolSpec(
                info.exposed_name,
                info.description or f"{info.server_id} 提供的工具",
                _mcp_parameters(info.input_schema),
                _mcp_handler(host, info),
            )
        )

    # 天气兜底：规则层 R07 与分类器固定调用 weather_get，不能退化成 unknown_tool
    if "weather_get" not in registry.names():

        async def weather_unavailable(
            args: dict[str, Any], ctx: ToolContext  # noqa: ARG001
        ) -> ToolResult:
            return ToolResult(
                False,
                "天气服务暂时用不了，我先帮您记着，等会儿再试试。",
                degraded="mcp_unavailable:weather",
            )

        registry.register(
            ToolSpec(
                "weather_get",
                "查询天气（MCP 天气服务当前不可用）",
                _weather_parameters(),
                weather_unavailable,
            )
        )

    async def time_get(args: dict[str, Any], ctx: ToolContext) -> ToolResult:  # noqa: ARG001
        return ToolResult(ok=True, content=time_reply())

    async def medication_query(args: dict[str, Any], ctx: ToolContext) -> ToolResult:  # noqa: ARG001
        """查今天真实用药计划；无计划明确说明，绝不编造药名/剂量。"""
        elder_id = _elder_uuid(ctx)
        if elder_id is None:
            return ToolResult(
                ok=False,
                content="我先确认一下您是哪位，再帮您看用药计划。",
                degraded="missing_elder_context",
            )
        try:
            async with get_session_factory()() as session:
                slots = await today_plans(session, elder_id=elder_id)
        except Exception as exc:  # noqa: BLE001 —— 查询失败不向长者抛异常
            logger.warning(
                "今日用药查询失败",
                extra={
                    "event": "medication_query_failed",
                    "elder_id": str(elder_id),
                    "error_type": type(exc).__name__,
                },
            )
            return ToolResult(
                ok=False,
                content="用药计划暂时查不到，我先帮您记着，等会儿再试试。",
                degraded=f"medication_query_error:{type(exc).__name__}",
            )
        if not slots:
            return ToolResult(
                ok=False,
                content="今天还没有登记用药计划，等医生确认后我再提醒您。",
                degraded="medication_not_configured",
            )
        return ToolResult(ok=True, content=format_plan_reply(slots))

    async def medication_confirm(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """推进用药确认图；DB 会话短生命周期，异常不穿透语音链路。"""
        reply = str(args.get("reply") or "")
        if detect_medication_reply(reply) is None:
            return ToolResult(
                ok=True,
                content="您是已经吃过药了，还是还没吃？",
                degraded="medication_reply_unclear",
            )
        elder_id = _elder_uuid(ctx)
        if elder_id is None:
            return ToolResult(
                ok=False,
                content="我先确认一下您是哪位，再帮您记吃药的事。",
                degraded="missing_elder_context",
            )

        try:
            async with get_session_factory()() as session:
                slots = await today_plans(session, elder_id=elder_id)
                slot = pick_current_slot(slots)
                if slot is None:
                    return ToolResult(
                        ok=False,
                        content="今天还没有登记用药计划，我先帮您记着。",
                        degraded="medication_not_configured",
                    )
                order, _ = await order_service.ensure_order(
                    session,
                    plan=slot.plan,
                    scheduled_at=slot.scheduled_at,
                    created_source="text",
                )
                await session.commit()
                result = await submit_reply(
                    order.order_id,
                    reply,
                    source="text",
                    device_id=ctx.device_id,
                )
        except Exception as exc:  # noqa: BLE001 —— 图/库异常只做可见降级
            logger.warning(
                "服药确认推进失败",
                extra={
                    "event": "medication_confirm_failed",
                    "elder_id": str(elder_id),
                    "error_type": type(exc).__name__,
                },
            )
            return ToolResult(
                ok=False,
                content="这次吃药我先没记上，等会儿再跟我说一声。",
                degraded=f"medication_confirm_error:{type(exc).__name__}",
            )
        return ToolResult(ok=True, content=result.reply, degraded=result.degraded)

    async def memory_recall(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """只读召回长期记忆；身份只认服务端上下文，无记忆时不编造。"""
        elder_id = _elder_uuid(ctx)
        if elder_id is None:
            return ToolResult(
                ok=False,
                content="我先确认一下您是哪位，再帮您回忆。",
                degraded="missing_elder_context",
            )
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult(
                ok=False,
                content="您想让我回忆哪件事？",
                degraded="memory_query_empty",
            )
        try:
            async with get_session_factory()() as session:
                memories = await memory_service.recall(session, elder_id, query)
        except Exception as exc:  # noqa: BLE001 —— 记忆查询失败不向长者抛异常
            logger.warning(
                "长期记忆查询失败",
                extra={
                    "event": "memory_recall_failed",
                    "elder_id": str(elder_id),
                    "error_type": type(exc).__name__,
                },
            )
            return ToolResult(
                ok=False,
                content="我一时没想起来，等会儿再问问我。",
                degraded=f"memory_recall_error:{type(exc).__name__}",
            )
        if not memories:
            return ToolResult(ok=True, content="我还没记住这件事。")
        snippets = [
            str(memory.content).strip()[:40] for memory in memories[:2] if memory.content
        ]
        return ToolResult(ok=True, content="我记得：" + "；".join(snippets) + "。")

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
        ToolSpec("time_get", "查询当前日期与时间", _empty_parameters(), time_get)
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
        ToolSpec(
            "medication_confirm",
            "记录长者对服药提醒的回复（吃过了/还没吃/等会儿吃）",
            _medication_confirm_parameters(),
            medication_confirm,
        )
    )
    registry.register(
        ToolSpec(
            "memory_recall",
            "回忆关于这位长者的长期记忆（喜好、健康、家人、日常习惯）",
            _memory_recall_parameters(),
            memory_recall,
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
