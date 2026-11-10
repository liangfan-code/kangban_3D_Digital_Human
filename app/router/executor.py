"""规则层执行器：只做确定性本地动作，不产生任何模型调用。

返回 ``None`` 表示「本层不处理」：天气/用药等需要 Tool 的动作在执行器就位前
回落到上层链路（Day6.3 接 MCP），重复一遍需要会话上下文则由语音会话处理。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.config import get_settings
from app.router.schemas import RouteDecision

_SAFETY_REPLIES: dict[str, str] = {
    "急症": "我听到您不舒服，先别动，我这就帮您叫医生。您慢慢呼吸，我在这儿陪着您。",
    "跌倒": "您先别急着起来，我这就叫人来帮您。您能听见我说话吗？",
    "走失": "您别着急，我这就帮您联系工作人员。您现在在什么地方，周围有什么？",
    "情绪低落": "我在呢，您慢慢说，我陪着您。要是难受，我也可以帮您叫人来聊聊。",
    "身体不适": "我听您说不太舒服。先坐下歇一会儿，我这就帮您告诉护工。",
}
_SAFETY_FALLBACK = "我听到了，这就帮您叫人来。您先别着急，我陪着您。"


@dataclass(frozen=True)
class RuleOutcome:
    """规则命中后的确定性结果：一句固定话术 + 需要下发给前端的控制事件。"""

    reply: str
    events: tuple[dict[str, str], ...] = field(default_factory=tuple)


def safety_reply(category: str | None) -> str:
    """预设安抚话术：不做诊断、不承诺医疗结论，只承诺「叫人」。"""
    return _SAFETY_REPLIES.get(category or "", _SAFETY_FALLBACK)


def time_reply() -> str:
    """当前时间话术：规则层与主控 Agent 的 time_get 工具共用。"""
    now = datetime.now(ZoneInfo(get_settings().app_timezone))
    period = "上午" if now.hour < 12 else "下午"
    hour = now.hour % 12 or 12
    return f"现在是{period}{hour}点{now.minute:02d}分。"


def execute_rule(decision: RouteDecision) -> RuleOutcome | None:
    """执行本地确定性动作；Tool 类动作与「重复一遍」返回 None 交由上层处理。"""
    action = decision.action
    if action == "media_next":
        return RuleOutcome("好，给您换一段。", ({"type": "control", "action": "media_next"},))
    if action == "volume_up":
        return RuleOutcome("好，我大声一点。", ({"type": "control", "action": "volume_up"},))
    if action == "volume_down":
        return RuleOutcome("好，我小声一点。", ({"type": "control", "action": "volume_down"},))
    if action == "media_stop":
        return RuleOutcome("好，我停下了。", ({"type": "control", "action": "stop"},))
    if action == "media_play":
        genre = decision.tool_args.get("genre") or "戏曲"
        return RuleOutcome(
            f"好，给您放一段{genre}。",
            ({"type": "control", "action": "media_play", "genre": genre},),
        )
    if action == "time_query":
        return RuleOutcome(time_reply())
    if action == "farewell":
        return RuleOutcome("好，您慢点儿，我随时都在。")
    # weather_query / medication_query 等 Tool 动作、repeat 由上层处理
    return None
