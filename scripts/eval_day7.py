"""Day7 段7：Iter3 任务级离线评测（20 个场景 → v1 基线报告）。

默认 offline：真实驱动 ``route_text`` 与主控 Agent 的 ReAct 循环，但 LLM 用
固定 JSON 的 fake、Embedding 不参与、外部副作用全部内存化：
- 媒体返回固定测试条目，不落库不下载；
- 用药任务单/漏服工单只写内存；
- 通知家属只入内存 HITL 队列，不真的外呼。

``--live`` 才使用真实 LLM；工具、ASR、TTS 仍是隔离实现，报告会标注
``mode=live`` 且不得作为默认基线。

延迟口径：offline 使用固定模拟常量（ASR/TTS/LLM 首 token/每次工具），
不取真实时钟，保证两次复跑除时间/commit 外完全一致；报告里明确写「模拟值」。

所有数字都来自本脚本的计算，报告不手写指标。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

from app.agent import companion
from app.agent.tools import ToolContext, ToolRegistry, ToolResult, ToolSpec
from app.router.classifier import LocalIntentClassifier
from app.router.executor import execute_rule
from app.router.rules import normalize_text
from app.router.service import route_text
from app.router.signals import (
    FAMILY_WORDS,
    MEDIA_GENRES,
    MEDICATION_WORDS,
    NOTIFY_VERBS,
    TIME_WORDS,
    WEATHER_WORDS,
    detect_medication_reply,
    extract_genre,
    looks_media_play,
    looks_notify_intent,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURES = REPO_ROOT / "eval" / "fixtures" / "day7_scenarios.jsonl"
DEFAULT_BADCASES = REPO_ROOT / "eval" / "fixtures" / "day7_badcases.jsonl"
DEFAULT_REPORT = REPO_ROOT / "eval" / "reports" / "v1_baseline.md"
LIVE_REPORT = REPO_ROOT / "eval" / "reports" / "v1_live.md"
EVALUATOR_VERSION = "day7-iter3-eval-v1"
FIXTURES_VERSION = "day7-scenarios-v1"
SAMPLE_SIZE = 20

ALLOWED_LAYERS = ("safety", "rule", "classifier", "agent")
ALLOWED_OUTCOMES = (
    "media_played",
    "media_stopped",
    "medication_confirmed",
    "medication_escalated",
    "medication_reported",
    "memory_referenced",
    "multi_done",
    "hitl_notified",
    "safety",
    "clarify",
    "degraded",
    "agent_reply",
    "media_control",
)

# offline 固定模拟耗时（毫秒）；只为给出可复算的 P95，不代表真实性能
SIM_ASR_MS = 120.0
SIM_LLM_FIRST_TOKEN_MS = 180.0
SIM_TTS_FIRST_BYTE_MS = 200.0
SIM_TOOL_MS = 25.0

_ENDOGENOUS_TOOL_NAMES = (
    "memory_recall",
    "medication_query",
    "medication_confirm",
    "media_play",
    "media_next",
    "media_stop",
    "weather_get",
    "time_get",
    "notify_family",
)


@dataclass
class TurnRecord:
    index: int
    layer: str
    intent: str
    rule_id: str | None
    tools: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    clarify: bool = False
    sensitive: bool = False
    simulated_ms: float = 0.0
    error: str | None = None


@dataclass
class ScenarioOutcome:
    scenario: dict
    turns: list[TurnRecord]
    outcome: str
    human_takeover: bool
    degraded: bool
    simulated_ms: float
    reasons: list[str] = field(default_factory=list)
    attributions: list[dict[str, str]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.reasons

    @property
    def actual_tools(self) -> list[str]:
        return [name for turn in self.turns for name in turn.tools]


@dataclass
class Sandbox:
    """单场景内存态：媒体/用药/工单/HITL 全部不落库、不外呼。"""

    media_available: bool = True
    weather_available: bool = False
    media_played: bool = False
    media_stopped: bool = False
    medication_reported: bool = False
    memory_referenced: bool = False
    medication_status: str = "pending"  # pending / confirmed / missed
    attempt: int = 0
    workorder_id: str | None = None
    hitl_events: list[str] = field(default_factory=list)
    tool_ok: list[str] = field(default_factory=list)
    tool_degraded: list[str] = field(default_factory=list)


def load_scenarios(path: Path) -> list[dict]:
    """读取并校验场景集；字段缺失直接报错，避免静默少测。"""
    scenarios: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno} 不是合法 JSON：{exc}") from exc
            validate_scenario(record, where=f"{path}:{lineno}")
            scenarios.append(record)
    return scenarios


def validate_scenario(record: Any, *, where: str = "fixture") -> None:
    if not isinstance(record, dict):
        raise ValueError(f"{where} 必须是对象")
    for key in ("id", "name", "turns", "expected", "tags", "notes"):
        if key not in record:
            raise ValueError(f"{where} 缺少字段 {key}")
    if not isinstance(record["turns"], list) or not 1 <= len(record["turns"]) <= 3:
        raise ValueError(f"{where} turns 必须是 1-3 条话术")
    if not all(isinstance(item, str) and item.strip() for item in record["turns"]):
        raise ValueError(f"{where} turns 不能包含空话术")
    expected = record["expected"]
    if not isinstance(expected, dict):
        raise ValueError(f"{where} expected 必须是对象")
    for key in ("route_layer", "intent", "tools", "outcome"):
        if key not in expected:
            raise ValueError(f"{where} expected 缺少字段 {key}")
    if expected["route_layer"] not in ALLOWED_LAYERS:
        raise ValueError(f"{where} route_layer 不合法：{expected['route_layer']}")
    if not isinstance(expected["tools"], list):
        raise ValueError(f"{where} expected.tools 必须是数组")
    if expected["outcome"] not in ALLOWED_OUTCOMES:
        raise ValueError(f"{where} outcome 不合法：{expected['outcome']}")
    if not isinstance(record["tags"], list):
        raise ValueError(f"{where} tags 必须是数组")


# ---------------- offline 工具与 LLM ----------------


def _ok(content: str, sandbox: Sandbox, name: str) -> ToolResult:
    sandbox.tool_ok.append(name)
    return ToolResult(True, content)


def _degraded(content: str, reason: str, sandbox: Sandbox, name: str) -> ToolResult:
    sandbox.tool_degraded.append(name)
    return ToolResult(False, content, degraded=reason)


def build_fake_registry(sandbox: Sandbox) -> ToolRegistry:
    """与真实工具同名同参的隔离实现：只读/只写内存，绝不触网或落库。"""
    registry = ToolRegistry()

    def _empty() -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def time_get(args: dict[str, Any], ctx: ToolContext) -> ToolResult:  # noqa: ARG001
        return _ok("现在是上午九点整。", sandbox, "time_get")

    async def weather_get(args: dict[str, Any], ctx: ToolContext) -> ToolResult:  # noqa: ARG001
        if sandbox.weather_available:
            return _ok("今天多云，二十度。", sandbox, "weather_get")
        return _degraded(
            "天气服务暂时用不了，我先帮您记着。", "mcp_unavailable:weather", sandbox, "weather_get"
        )

    async def medication_query(args: dict[str, Any], ctx: ToolContext) -> ToolResult:  # noqa: ARG001
        sandbox.medication_reported = True
        return _ok("今天要吃降压药，一片。", sandbox, "medication_query")

    async def medication_confirm(args: dict[str, Any], ctx: ToolContext) -> ToolResult:  # noqa: ARG001
        reply = str(args.get("reply") or "")
        kind = detect_medication_reply(reply)
        if kind is None:
            return _degraded(
                "您是已经吃过药了，还是还没吃？",
                "medication_reply_unclear",
                sandbox,
                "medication_confirm",
            )
        if sandbox.medication_status != "pending":
            return _ok("这次吃药已经记下了。", sandbox, "medication_confirm")
        if kind == "confirmed":
            sandbox.medication_status = "confirmed"
            return _ok("好，降压药已经记下了。", sandbox, "medication_confirm")
        if kind == "deferred":
            return _ok("好，那您慢慢来，我过一会儿再提醒您。", sandbox, "medication_confirm")
        sandbox.attempt += 1
        if sandbox.attempt >= 3:
            sandbox.medication_status = "missed"
            sandbox.workorder_id = "wo-fake-missed"
            return _ok(
                "这次吃药还没确认，我已经告诉护工，他们会来看看您。",
                sandbox,
                "medication_confirm",
            )
        return _ok("好，我记下了。您方便的时候再吃。", sandbox, "medication_confirm")

    async def memory_recall(args: dict[str, Any], ctx: ToolContext) -> ToolResult:  # noqa: ARG001
        sandbox.memory_referenced = True
        return _ok("我记得：喜欢吃清淡的面食。", sandbox, "memory_recall")

    def _media(genre: str | None, name: str) -> ToolResult:
        if not sandbox.media_available:
            return _degraded(
                "这段暂时没有音源，我帮您记着。", "media_unavailable", sandbox, name
            )
        sandbox.media_played = True
        label = genre or "戏曲"
        return _ok(f"好，给您放一段{label}《测试曲目》。", sandbox, name)

    async def media_play(args: dict[str, Any], ctx: ToolContext) -> ToolResult:  # noqa: ARG001
        return _media(str(args.get("genre") or "").strip() or None, "media_play")

    async def media_next(args: dict[str, Any], ctx: ToolContext) -> ToolResult:  # noqa: ARG001
        return _media(str(args.get("genre") or "").strip() or None, "media_next")

    async def media_stop(args: dict[str, Any], ctx: ToolContext) -> ToolResult:  # noqa: ARG001
        sandbox.media_stopped = True
        return _ok("好，我停下了。", sandbox, "media_stop")

    async def notify_family(args: dict[str, Any], ctx: ToolContext) -> ToolResult:  # noqa: ARG001
        sandbox.hitl_events.append("notify_family")
        return _ok("这件事需要工作人员确认，我已经帮您转过去了。", sandbox, "notify_family")

    registry.register(ToolSpec("time_get", "查询时间", _empty(), time_get))
    registry.register(ToolSpec("weather_get", "查询天气", _empty(), weather_get))
    registry.register(ToolSpec("medication_query", "查询用药计划", _empty(), medication_query))
    registry.register(
        ToolSpec("medication_confirm", "记录服药回复", _empty(), medication_confirm)
    )
    registry.register(ToolSpec("memory_recall", "召回长期记忆", _empty(), memory_recall))
    registry.register(ToolSpec("media_play", "点播媒体", _empty(), media_play))
    registry.register(ToolSpec("media_next", "切换下一段", _empty(), media_next))
    registry.register(ToolSpec("media_stop", "停止播放", _empty(), media_stop))
    registry.register(ToolSpec("notify_family", "通知家属", _empty(), notify_family))
    return registry


def _earliest(text: str, keywords: tuple[str, ...]) -> int:
    positions = [text.find(word) for word in keywords if word and word in text]
    return min(positions) if positions else -1


def plan_tools(text: str) -> list[tuple[str, dict[str, Any]]]:
    """fake LLM 的确定性计划：按话术里需求出现的先后顺序选工具。

    这只是离线替身，不代表真实模型能力；报告会注明限制。
    """
    normalized = normalize_text(text)
    signals: list[tuple[int, str, dict[str, Any]]] = []

    memory_pos = _earliest(normalized, ("孙女", "老伴", "记得", "以前", "爱吃什么", "回忆"))
    if memory_pos >= 0:
        signals.append((memory_pos, "memory_recall", {"query": text}))

    if any(word in normalized for word in MEDICATION_WORDS) or "提醒我吃药" in normalized:
        signals.append(
            (_earliest(normalized, MEDICATION_WORDS + ("提醒我吃药",)), "medication_query", {})
        )

    if looks_media_play(normalized):
        genre = extract_genre(normalized)
        signals.append(
            (_earliest(normalized, MEDIA_GENRES), "media_play", {"genre": genre} if genre else {})
        )

    weather_pos = _earliest(normalized, WEATHER_WORDS)
    if weather_pos >= 0:
        signals.append((weather_pos, "weather_get", {}))

    time_pos = _earliest(normalized, TIME_WORDS)
    if time_pos >= 0:
        signals.append((time_pos, "time_get", {}))

    if looks_notify_intent(text):
        notify_pos = _earliest(normalized, FAMILY_WORDS + NOTIFY_VERBS)
        signals.append((notify_pos, "notify_family", {"message": "长者请家属回电"}))

    signals.sort(key=lambda item: item[0])
    return [(name, args) for _, name, args in signals]


def _fake_completion(messages: list[dict[str, Any]], tools: Any = None) -> dict[str, Any]:
    """fake LLM：首步按关键词给工具计划，看到工具结果后收尾。"""
    if any(message.get("role") == "tool" for message in messages):
        return {"content": "好，都给您办好了。", "tool_calls": []}
    user_text = ""
    for message in reversed(messages):
        if message.get("role") == "user":
            user_text = str(message.get("content") or "")
            break
    plan = plan_tools(user_text)
    if not plan:
        return {"content": "好，我陪着您。", "tool_calls": []}
    return {
        "content": "",
        "tool_calls": [
            {
                "id": f"call-{index}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }
            for index, (name, args) in enumerate(plan)
        ],
    }


async def _fake_complete(messages: list[dict[str, Any]], tools: Any = None, **_: Any) -> dict:
    return _fake_completion(messages, tools)


# ---------------- 跑批 ----------------


def _simulated_turn_ms(layer: str, tool_count: int) -> float:
    total = SIM_ASR_MS + SIM_TTS_FIRST_BYTE_MS + tool_count * SIM_TOOL_MS
    if layer == "agent":
        total += SIM_LLM_FIRST_TOKEN_MS
    return total


async def run_scenario(
    scenario: dict,
    *,
    live: bool = False,
    classifier: LocalIntentClassifier | None = None,
) -> ScenarioOutcome:
    """跑完一个场景（1-3 轮），返回实际轨迹与派生结果。"""
    tags = set(scenario.get("tags") or [])
    sandbox = Sandbox(
        media_available="empty_media" not in tags,
        weather_available="weather_ok" in tags,
    )
    registry = build_fake_registry(sandbox)
    provider = classifier or LocalIntentClassifier()
    turns: list[TurnRecord] = []

    for index, text in enumerate(scenario["turns"], start=1):
        record = TurnRecord(index=index, layer="unknown", intent="unknown", rule_id=None)
        try:
            decision = route_text(text, classifier=provider)
            record.layer = decision.layer.value
            record.intent = decision.intent.value
            record.rule_id = decision.rule_id
            record.sensitive = decision.is_sensitive
            record.clarify = decision.needs_clarification

            if decision.is_sensitive or decision.needs_clarification:
                pass
            elif decision.tool:
                result = await registry.call(decision.tool, decision.tool_args, _ctx(index))
                record.tools.append(decision.tool)
                if result.degraded:
                    record.degraded.append(result.degraded)
            elif decision.action:
                outcome = execute_rule(decision)
                if outcome is None and decision.escalated_to_agent:
                    await _run_agent(text, registry, record, sandbox, live=live)
            elif decision.escalated_to_agent or decision.layer.value in ("classifier", "agent"):
                await _run_agent(text, registry, record, sandbox, live=live)
        except Exception as exc:  # noqa: BLE001 —— 单条异常不能毁掉整份评测
            record.error = type(exc).__name__
            record.degraded.append(f"exception:{type(exc).__name__}")

        record.simulated_ms = _simulated_turn_ms(record.layer, len(record.tools))
        turns.append(record)

    outcome_name = derive_outcome(scenario, sandbox, turns)
    human_takeover = outcome_name in {"safety", "medication_escalated", "hitl_notified"} or bool(
        sandbox.hitl_events
    )
    scored = ScenarioOutcome(
        scenario=scenario,
        turns=turns,
        outcome=outcome_name,
        human_takeover=human_takeover,
        degraded=any(turn.degraded for turn in turns),
        simulated_ms=sum(turn.simulated_ms for turn in turns),
    )
    _score(scored)
    return scored


def _ctx(index: int) -> ToolContext:
    return ToolContext(
        elder_id=str(uuid.UUID(int=index)), device_id="test-device", thread_id="test-thread"
    )


async def _run_agent(
    text: str, registry: ToolRegistry, record: TurnRecord, sandbox: Sandbox, *, live: bool
) -> None:
    context = mock.patch.object(companion, "complete_chat_message", _fake_complete)
    if live:
        result = await companion.respond(
            text, system_prompt="测试人设", ctx=_ctx(record.index), registry=registry
        )
    else:
        with context:
            result = await companion.respond(
                text, system_prompt="测试人设", ctx=_ctx(record.index), registry=registry
            )
    record.tools.extend(result.tool_names)
    if result.degraded:
        record.degraded.append(result.degraded)


def derive_outcome(scenario: dict, sandbox: Sandbox, turns: list[TurnRecord]) -> str:
    """从实际执行轨迹派生任务结果，而不是读期望值。"""
    if any(turn.sensitive for turn in turns):
        return "safety"
    if any(turn.clarify for turn in turns):
        return "clarify"
    if any(turn.degraded for turn in turns):
        return "degraded"
    if sandbox.medication_status == "confirmed":
        return "medication_confirmed"
    if sandbox.medication_status == "missed":
        return "medication_escalated"
    if len(sandbox.tool_ok) >= 2:
        return "multi_done"
    if sandbox.medication_reported:
        return "medication_reported"
    if sandbox.memory_referenced:
        return "memory_referenced"
    if sandbox.media_played:
        return "media_played"
    if sandbox.media_stopped:
        return "media_stopped"
    if sandbox.hitl_events:
        return "hitl_notified"
    if turns and turns[-1].layer == "rule":
        return "media_control"
    return "agent_reply"


def _score(outcome: ScenarioOutcome) -> None:
    """对照期望记录差异；只描述事实，不做主观评价。"""
    expected = outcome.scenario["expected"]
    turns = outcome.turns
    for turn in turns:
        if turn.error:
            outcome.reasons.append(f"turn{turn.index}:exception:{turn.error}")
        if turn.layer != expected["route_layer"]:
            outcome.reasons.append(
                f"turn{turn.index}:layer:{turn.layer}!={expected['route_layer']}"
            )
        if turn.intent != expected["intent"]:
            outcome.reasons.append(
                f"turn{turn.index}:intent:{turn.intent}!={expected['intent']}"
            )
    if outcome.actual_tools != expected["tools"]:
        outcome.reasons.append(
            f"tools:{','.join(outcome.actual_tools) or '—'}!={','.join(expected['tools']) or '—'}"
        )
    if outcome.outcome != expected["outcome"]:
        outcome.reasons.append(f"outcome:{outcome.outcome}!={expected['outcome']}")
    if not outcome.passed:
        outcome.attributions = attribute_failure(outcome)


def attribute_failure(outcome: ScenarioOutcome) -> list[dict[str, str]]:
    """按 layer ∈ {asr, laya, recall, llm, tool, orchestration} 归因。"""
    expected = outcome.scenario["expected"]
    attributions: list[dict[str, str]] = []

    if any(turn.error for turn in outcome.turns):
        attributions.append(
            {
                "layer": "orchestration",
                "evidence": "turn error="
                + ",".join(turn.error or "" for turn in outcome.turns if turn.error),
            }
        )

    layer_mismatch = [
        f"turn{turn.index}:{turn.layer}!={expected['route_layer']}"
        for turn in outcome.turns
        if turn.layer != expected["route_layer"]
    ]
    if layer_mismatch:
        actual_layers = {turn.layer for turn in outcome.turns}
        layer = (
            "laya"
            if actual_layers & {"classifier", "safety"}
            or expected["route_layer"] in {"classifier", "safety"}
            else "orchestration"
        )
        attributions.append({"layer": layer, "evidence": ";".join(layer_mismatch)})

    if outcome.actual_tools != expected["tools"]:
        layer = "llm" if expected["route_layer"] == "agent" else "tool"
        attributions.append(
            {
                "layer": layer,
                "evidence": "tools="
                + (",".join(outcome.actual_tools) or "—")
                + " expected="
                + (",".join(expected["tools"]) or "—"),
            }
        )

    if outcome.outcome != expected["outcome"]:
        if expected["outcome"] in {"memory_referenced"} or outcome.outcome == "memory_referenced":
            layer = "recall"
        elif outcome.degraded:
            layer = "tool"
        else:
            layer = "orchestration"
        attributions.append(
            {
                "layer": layer,
                "evidence": f"outcome={outcome.outcome} expected={expected['outcome']}",
            }
        )

    if not attributions:
        attributions.append(
            {"layer": "orchestration", "evidence": "未分类差异：" + ";".join(outcome.reasons)}
        )
    return attributions


# ---------------- 指标 ----------------


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * p / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def compute_metrics(outcomes: list[ScenarioOutcome], *, mode: str = "offline") -> dict[str, Any]:
    """8 项指标；分母与来源写进返回值，报告直接引用。"""
    total = len(outcomes)
    turns = [turn for outcome in outcomes for turn in outcome.turns]
    passed = sum(1 for outcome in outcomes if outcome.passed)

    sensitive = [
        outcome
        for outcome in outcomes
        if "safety" in (outcome.scenario.get("tags") or [])
        or outcome.scenario["expected"]["outcome"] == "safety"
    ]
    sensitive_hit = sum(
        1 for outcome in sensitive if any(turn.sensitive for turn in outcome.turns)
    )

    tool_scenarios = [
        outcome for outcome in outcomes if outcome.scenario["expected"]["tools"]
    ]
    tool_hit = sum(
        1
        for outcome in tool_scenarios
        if outcome.actual_tools == outcome.scenario["expected"]["tools"]
    )

    hitl = sum(1 for outcome in outcomes if outcome.human_takeover)
    s1_turns = sum(
        1
        for turn in turns
        if turn.layer in {"rule", "classifier"} and not turn.clarify and not turn.sensitive
    )

    layer_distribution = {layer: 0 for layer in ALLOWED_LAYERS}
    for turn in turns:
        if turn.layer in layer_distribution:
            layer_distribution[turn.layer] += 1

    latencies = [turn.simulated_ms for turn in turns]
    return {
        "mode": mode,
        "samples": total,
        "turn_count": len(turns),
        "task_success_rate": _rate(passed, total),
        "task_success_passed": passed,
        "task_success_denominator": total,
        "sensitive_recall": _rate(sensitive_hit, len(sensitive)),
        "sensitive_hit": sensitive_hit,
        "sensitive_denominator": len(sensitive),
        "tool_accuracy": _rate(tool_hit, len(tool_scenarios)),
        "tool_hit": tool_hit,
        "tool_denominator": len(tool_scenarios),
        "human_takeover_rate": _rate(hitl, total),
        "human_takeover_count": hitl,
        "s1_closed_loop_rate": _rate(s1_turns, len(turns)),
        "s1_turns": s1_turns,
        "p95_latency_ms": percentile(latencies, 95),
        "latency_simulated": mode == "offline",
        "token_cost": 0 if mode == "offline" else None,
        "layer_distribution": layer_distribution,
    }


# ---------------- 报告 ----------------


def _fmt_rate(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _fmt_ms(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


def _display_path(path: str | Path) -> str:
    """报告里尽量显示仓库相对路径，避免把本机绝对路径写进基线。"""
    try:
        return str(Path(path).resolve().relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def _git_commit() -> str:
    try:
        return (
            subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout.strip()
            or "unknown"
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def render_markdown(
    outcomes: list[ScenarioOutcome],
    metrics: dict[str, Any],
    *,
    fixtures: str,
    generated_at: str,
    mode: str,
) -> str:
    lines = [
        "# Day7 Iter3 任务级基线报告（v1）",
        "",
        f"- 生成时间（UTC）：{generated_at}",
        f"- 代码版本：`{_git_commit()}`",
        f"- 模式：`{mode}`",
        f"- 评测集：`{fixtures}`（版本 `{FIXTURES_VERSION}`，样本量 {metrics['samples']}）",
        f"- 评测器版本：`{EVALUATOR_VERSION}`",
        "- 复现命令：`.\\.venv\\Scripts\\python.exe scripts\\eval_day7.py`",
        "",
        "## 1. 核心指标（8 项）",
        "",
        "| 指标 | 结果 | 公式 | 分母 | 数据来源 |",
        "| --- | --- | --- | --- | --- |",
        f"| 任务成功率 | {_fmt_rate(metrics['task_success_rate'])} "
        f"| 通过场景数 / 总场景数 | {metrics['task_success_denominator']} | 实际轨迹对比期望 |",
        f"| P95 延迟 | {_fmt_ms(metrics['p95_latency_ms'])} ms "
        f"| 全部轮次模拟耗时 P95 | {metrics['turn_count']} 轮 | "
        f"{'模拟值（固定常量，非真实时钟）' if metrics['latency_simulated'] else '真实耗时'} |",
        f"| S1 闭环率 | {_fmt_rate(metrics['s1_closed_loop_rate'])} "
        f"| 规则/分类器直接处理且未升级的轮次 / 总轮次 | {metrics['turn_count']} | 路由判定 |",
        f"| 分层命中率 | 见表 2 | 各层轮次 / 总轮次 | {metrics['turn_count']} | 路由判定 |",
        f"| 单会话 token 成本 | {'0（offline 不调 LLM）' if mode == 'offline' else '见 live 报告'} "
        "| 输入+输出 token | 1 个会话 | "
        f"{'offline 记 0 并说明' if mode == 'offline' else '响应 usage'} |",
        f"| 敏感召回率 | {_fmt_rate(metrics['sensitive_recall'])} "
        f"| 敏感场景被判 sensitive / 敏感场景数 | {metrics['sensitive_denominator']} | 路由判定 |",
        f"| 工具调用准确率 | {_fmt_rate(metrics['tool_accuracy'])} "
        "| 工具名与顺序全匹配场景 / 含工具期望场景 "
        f"| {metrics['tool_denominator']} | 实际调用轨迹 |",
        f"| 人工接管率 | {_fmt_rate(metrics['human_takeover_rate'])} "
        "| 进入 HITL/建单/升级人工场景 / 总场景 "
        f"| {metrics['task_success_denominator']} | 实际轨迹与内存工单 |",
        "",
        "口径说明：任务成功率按「层级 + 意图 + 工具顺序 + 结果」四项全一致才计通过；"
        "工具准确率只统计期望非空工具的场景，顺序不同即算失败；"
        "人工接管包含敏感建单、漏服升级与通知家属转人工。",
        "",
        "## 2. 分层分布",
        "",
        "| 层级 | 轮次 | 占比 |",
        "| --- | --- | --- |",
    ]
    for layer in ALLOWED_LAYERS:
        count = metrics["layer_distribution"].get(layer, 0)
        lines.append(f"| {layer} | {count} | {_fmt_rate(_rate(count, metrics['turn_count']))} |")

    lines += [
        "",
        "## 3. 场景明细",
        "",
        "| id | 名称 | 期望（层级/意图/工具/结果） | 实际（层级/意图/工具/结果） "
        "| 通过 | 模拟耗时(ms) | 降级 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for outcome in outcomes:
        expected = outcome.scenario["expected"]
        expected_text = (
            f"{expected['route_layer']}/{expected['intent']}/"
            f"{','.join(expected['tools']) or '—'}/{expected['outcome']}"
        )
        layers = ",".join(dict.fromkeys(turn.layer for turn in outcome.turns))
        intents = ",".join(dict.fromkeys(turn.intent for turn in outcome.turns))
        actual_text = (
            f"{layers}/{intents}/{','.join(outcome.actual_tools) or '—'}/{outcome.outcome}"
        )
        degraded = "是" if outcome.degraded else "否"
        lines.append(
            f"| {outcome.scenario['id']} | {outcome.scenario['name']} | {expected_text} "
            f"| {actual_text} | {'✅' if outcome.passed else '❌'} "
            f"| {outcome.simulated_ms:.1f} | {degraded} |"
        )

    failures = [outcome for outcome in outcomes if not outcome.passed]
    lines += [
        "",
        "## 4. 失败分层归因",
        "",
    ]
    if not failures:
        lines.append("本次评测无失败场景。")
    else:
        lines += [
            "| id | 归因层级 | 证据字段 | 建议下一步 |",
            "| --- | --- | --- | --- |",
        ]
        suggestions = {
            "asr": "核对 ASR 转写（本段 offline 不产生 ASR 错误）",
            "laya": "补分类器/规则样例或修 Laya 分类，不在本段改判定",
            "recall": "核对记忆召回库与 query 构造",
            "llm": "核对 Agent 工具选择提示与 fake 计划的一致性",
            "tool": "核对工具入参与返回，必要时另开修复任务",
            "orchestration": "核对路由分支与多轮状态推进",
        }
        for outcome in failures:
            evidence = "；".join(
                f"{item['layer']}:{item['evidence']}" for item in outcome.attributions
            )
            layer = outcome.attributions[0]["layer"] if outcome.attributions else "orchestration"
            lines.append(
                f"| {outcome.scenario['id']} | {layer} | {evidence} "
                f"| {suggestions.get(layer, '人工复核')} |"
            )

    lines += [
        "",
        "## 5. 与 Day6 路由基线的关系",
        "",
        "Day6 基线测的是「路由判定对不对」（分层/意图/敏感/延迟/成本），"
        "本报告测的是「事情办成没有」（任务成功、工具顺序、人工接管）。"
        "两者分母不同（Day6 为单句样例，本报告为 1-3 轮场景），不能直接比较数字，"
        "只能对照看趋势：路由层稳定是任务成功的前提，不等于任务完成。",
        "",
        "## 6. 局限与复现",
        "",
        f"- 模式 `{mode}`："
        + (
            "LLM 为固定 JSON 的 fake，工具为内存隔离实现，ASR/TTS 不接入；"
            "延迟为固定模拟常量，只用于回归对比，不代表线上性能；token 成本记 0。"
            if mode == "offline"
            else "使用真实 LLM，但工具/ASR/TTS 仍为隔离实现，结果不得作为默认基线。"
        ),
        "- 场景为人工构造且与规则关键词同源，衡量的是回归稳定性，不是开放域泛化。",
        "- 失败场景已写入 `eval/fixtures/day7_badcases.jsonl`（按 id 去重），本段只归因不修复。",
        "- 复现：`.\\.venv\\Scripts\\python.exe scripts\\eval_day7.py "
        "--out eval\\reports\\v1_baseline.md`；"
        "gate：`.\\.venv\\Scripts\\python.exe scripts\\eval_gate.py "
        "--report eval\\reports\\v1_baseline.md`。",
        "",
        "<!-- eval_metrics "
        + json.dumps(
            {
                "mode": metrics["mode"],
                "samples": metrics["samples"],
                "task_success_rate": metrics["task_success_rate"],
                "sensitive_recall": metrics["sensitive_recall"],
                "tool_accuracy": metrics["tool_accuracy"],
                "human_takeover_rate": metrics["human_takeover_rate"],
                "s1_closed_loop_rate": metrics["s1_closed_loop_rate"],
                "p95_latency_ms": metrics["p95_latency_ms"],
                "token_cost": metrics["token_cost"],
            },
            ensure_ascii=False,
        )
        + " -->",
    ]
    return "\n".join(lines) + "\n"


def write_badcases(failures: list[ScenarioOutcome], path: Path, generated_at: str) -> int:
    """失败场景按 id 去重写入回流文件；全部通过时删除文件。"""
    if not failures:
        path.unlink(missing_ok=True)
        return 0
    existing: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("id"):
                existing[record["id"]] = record

    for outcome in failures:
        scenario = outcome.scenario
        record = existing.get(scenario["id"], {"first_seen": generated_at})
        record.update(
            {
                "id": scenario["id"],
                "name": scenario["name"],
                "turns": scenario["turns"],
                "expected": scenario["expected"],
                "actual": {
                    "layers": list(dict.fromkeys(turn.layer for turn in outcome.turns)),
                    "intent": [turn.intent for turn in outcome.turns],
                    "tools": outcome.actual_tools,
                    "outcome": outcome.outcome,
                },
                "reasons": outcome.reasons,
                "attribution": outcome.attributions,
                "last_seen": generated_at,
            }
        )
        existing[scenario["id"]] = record

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for key in sorted(existing):
            fh.write(json.dumps(existing[key], ensure_ascii=False) + "\n")
    return len(failures)


async def evaluate(scenarios: list[dict], *, live: bool = False) -> list[ScenarioOutcome]:
    provider = LocalIntentClassifier()
    return [await run_scenario(scenario, live=live, classifier=provider) for scenario in scenarios]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Day7 Iter3 任务级评测（20 个场景）")
    parser.add_argument("--fixtures", default=str(DEFAULT_FIXTURES))
    parser.add_argument("--out", default=str(DEFAULT_REPORT))
    parser.add_argument("--badcases", default=str(DEFAULT_BADCASES))
    parser.add_argument("--no-badcases", action="store_true")
    parser.add_argument(
        "--live", action="store_true", help="使用真实 LLM（工具/ASR/TTS 仍隔离），结果不作默认基线"
    )
    args = parser.parse_args(argv)

    mode = "live" if args.live else "offline"
    out_path = Path(args.out)
    if args.live and out_path == DEFAULT_REPORT:
        out_path = LIVE_REPORT
        print(f"live 模式不覆盖默认基线，改写到 {out_path}")

    scenarios = load_scenarios(Path(args.fixtures))
    outcomes = asyncio.run(evaluate(scenarios, live=args.live))
    metrics = compute_metrics(outcomes, mode=mode)
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    report = render_markdown(
        outcomes,
        metrics,
        fixtures=_display_path(args.fixtures),
        generated_at=generated_at,
        mode=mode,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8", newline="\n")

    failures = [outcome for outcome in outcomes if not outcome.passed]
    if not args.no_badcases:
        write_badcases(failures, Path(args.badcases), generated_at)

    print(
        f"[{mode}] 样本 {metrics['samples']} 条，通过 {metrics['task_success_passed']} 条，"
        f"任务成功率 {_fmt_rate(metrics['task_success_rate'])}，"
        f"敏感召回 {_fmt_rate(metrics['sensitive_recall'])}，"
        f"工具准确率 {_fmt_rate(metrics['tool_accuracy'])}，"
        f"人工接管率 {_fmt_rate(metrics['human_takeover_rate'])}"
    )
    print(f"报告：{out_path}")
    if failures:
        print(f"失败 {len(failures)} 条：" + ", ".join(o.scenario["id"] for o in failures))
    return 0


if __name__ == "__main__":
    sys.exit(main())
