"""Day6.4 三级路由离线评测：50 条标注样例 → 分层/准确率/召回/延迟/成本基线。

用法::

    .\\.venv\\Scripts\\python.exe scripts\\eval_day6.py
    .\\.venv\\Scripts\\python.exe scripts\\eval_day6.py --out eval\\reports\\x.md

口径（报告里会原样写出，避免「准确率很高」式空话）：
- 判定只调 ``app.router.service.route_text``，纯函数、零网络、零数据库；
- 基线固定使用 ``LocalIntentClassifier``（确定性关键词），Laya 接入后用同一集重跑对比；
- 分层准确率 = 预测层级与标注层级一致；意图准确率只统计标注了意图的样例；
- 敏感召回率 = 标注敏感中被判敏感的占比（安全层先于一切，目标 ≥ 95%）；
- S1 闭环率 = 未进入主控 Agent 的占比（安全/规则/分类器直接处理，不调大模型）；
- 工具准确率 = 标注了期望 Tool 的样例中 tool 字段一致的占比；
- 成本对比给「LLM 调用次数 + 字符数 + 估算 token」，不写价格（随厂商变动，无法离线复算）。

失败样例自动写入 ``eval/fixtures/day6_badcases.jsonl``（按 id 去重），供后续回归与归因。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from app.router.classifier import LocalIntentClassifier
from app.router.schemas import RouteDecision
from app.router.service import route_text

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURES = REPO_ROOT / "eval" / "fixtures" / "day6_intents.jsonl"
DEFAULT_BADCASES = REPO_ROOT / "eval" / "fixtures" / "day6_badcases.jsonl"
DEFAULT_REPORT = REPO_ROOT / "eval" / "reports" / "day6_router_baseline.md"
EVALUATOR_VERSION = "day6-router-eval-v1"
CHARS_PER_TOKEN = 1.5  # 中文粗估：1 token ≈ 1.5 字（仅用于量级对比，不声称精确）


@dataclass
class SampleOutcome:
    sample: dict
    decision: RouteDecision | None
    elapsed_ms: float
    error: str | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.reasons


def load_samples(path: Path) -> list[dict]:
    """读 JSONL；空行跳过，缺字段直接报错，避免「静默少测」。"""
    samples: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno} 不是合法 JSON：{exc}") from exc
            for key in ("id", "text", "expected_layer", "expected_sensitive"):
                if key not in record:
                    raise ValueError(f"{path}:{lineno} 缺少字段 {key}")
            samples.append(record)
    return samples


def _add_reason(outcome: SampleOutcome) -> None:
    sample = outcome.sample
    decision = outcome.decision
    if outcome.error is not None:
        outcome.reasons.append(f"exception:{outcome.error}")
        return
    assert decision is not None
    if decision.layer.value != sample["expected_layer"]:
        outcome.reasons.append(
            f"layer:{decision.layer.value}!={sample['expected_layer']}"
        )
    if sample.get("expected_intent") and decision.intent.value != sample["expected_intent"]:
        outcome.reasons.append(
            f"intent:{decision.intent.value}!={sample['expected_intent']}"
        )
    if bool(sample["expected_sensitive"]) != decision.is_sensitive:
        outcome.reasons.append(
            f"sensitive:{decision.is_sensitive}!={bool(sample['expected_sensitive'])}"
        )
    expected_category = sample.get("expected_category")
    if expected_category and decision.sensitive_category != expected_category:
        outcome.reasons.append(
            f"category:{decision.sensitive_category}!={expected_category}"
        )
    if sample.get("expected_tool") and decision.tool != sample["expected_tool"]:
        outcome.reasons.append(f"tool:{decision.tool}!={sample['expected_tool']}")
    if sample.get("expected_clarify") and not decision.needs_clarification:
        outcome.reasons.append("clarify:missing")


def evaluate(
    samples: list[dict], classifier: LocalIntentClassifier | None = None
) -> list[SampleOutcome]:
    """逐条跑路由并记录耗时；异常不中断评测，计入异常率。"""
    provider = classifier or LocalIntentClassifier()
    outcomes: list[SampleOutcome] = []
    for sample in samples:
        started = perf_counter()
        decision: RouteDecision | None = None
        error: str | None = None
        try:
            decision = route_text(sample["text"], classifier=provider)
        except Exception as exc:  # noqa: BLE001 —— 单条异常不能毁掉整份评测
            error = type(exc).__name__
        elapsed_ms = (perf_counter() - started) * 1000
        outcome = SampleOutcome(sample, decision, elapsed_ms, error)
        _add_reason(outcome)
        outcomes.append(outcome)
    return outcomes


def percentile(values: list[float], p: float) -> float | None:
    """线性插值百分位，空列表返回 None。"""
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


def compute_metrics(outcomes: list[SampleOutcome]) -> dict:
    """按报告口径汇总；分母为 0 的指标返回 None（显示「—」）。"""
    total = len(outcomes)
    layers = ("safety", "rule", "classifier", "agent")
    predicted_layers = {layer: 0 for layer in layers}
    expected_layers = {layer: 0 for layer in layers}
    for outcome in outcomes:
        expected_layers[outcome.sample["expected_layer"]] += 1
        if outcome.decision is not None:
            predicted_layers[outcome.decision.layer.value] += 1

    layer_ok = sum(
        1
        for o in outcomes
        if o.decision is not None and o.decision.layer.value == o.sample["expected_layer"]
    )
    intent_samples = [o for o in outcomes if o.sample.get("expected_intent")]
    intent_ok = sum(
        1
        for o in intent_samples
        if o.decision is not None and o.decision.intent.value == o.sample["expected_intent"]
    )
    sensitive_samples = [o for o in outcomes if o.sample["expected_sensitive"]]
    sensitive_hit = sum(
        1 for o in sensitive_samples if o.decision is not None and o.decision.is_sensitive
    )
    non_sensitive_samples = [o for o in outcomes if not o.sample["expected_sensitive"]]
    false_positive = sum(
        1 for o in non_sensitive_samples if o.decision is not None and o.decision.is_sensitive
    )
    tool_samples = [o for o in outcomes if o.sample.get("expected_tool")]
    tool_ok = sum(
        1
        for o in tool_samples
        if o.decision is not None and o.decision.tool == o.sample["expected_tool"]
    )
    closed_loop = sum(
        1 for o in outcomes if o.decision is not None and o.decision.layer.value != "agent"
    )
    human_takeover = sum(
        1 for o in outcomes if o.decision is not None and o.decision.is_sensitive
    )
    errors = sum(1 for o in outcomes if o.error is not None)
    latencies = [o.elapsed_ms for o in outcomes]

    return {
        "total": total,
        "layer_accuracy": _rate(layer_ok, total),
        "intent_accuracy": _rate(intent_ok, len(intent_samples)),
        "intent_samples": len(intent_samples),
        "sensitive_recall": _rate(sensitive_hit, len(sensitive_samples)),
        "sensitive_samples": len(sensitive_samples),
        "sensitive_false_positive": _rate(false_positive, len(non_sensitive_samples)),
        "false_positive_count": false_positive,
        "closed_loop": _rate(closed_loop, total),
        "tool_accuracy": _rate(tool_ok, len(tool_samples)),
        "tool_samples": len(tool_samples),
        "error_rate": _rate(errors, total),
        "error_count": errors,
        "human_takeover": _rate(human_takeover, total),
        "latency_mean_ms": sum(latencies) / total if total else None,
        "latency_p50_ms": percentile(latencies, 50),
        "latency_p95_ms": percentile(latencies, 95),
        "latency_max_ms": max(latencies) if latencies else None,
        "predicted_layers": predicted_layers,
        "expected_layers": expected_layers,
        "failures": [o for o in outcomes if not o.passed],
    }


def cost_comparison(outcomes: list[SampleOutcome]) -> dict:
    """Day5「全量大模型」 vs Day6 三级路由：按调用次数与字符量对比。"""
    total_chars = sum(len(o.sample["text"]) for o in outcomes)
    agent_chars = sum(
        len(o.sample["text"])
        for o in outcomes
        if o.decision is not None and o.decision.layer.value == "agent"
    )
    agent_turns = sum(
        1 for o in outcomes if o.decision is not None and o.decision.layer.value == "agent"
    )
    return {
        "total_turns": len(outcomes),
        "baseline_calls": len(outcomes),
        "router_calls": agent_turns,
        "total_chars": total_chars,
        "agent_chars": agent_chars,
        "baseline_tokens": round(total_chars / CHARS_PER_TOKEN),
        "router_tokens": round(agent_chars / CHARS_PER_TOKEN),
    }


def _fmt_rate(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _fmt_ms(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}"


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def render_markdown(
    outcomes: list[SampleOutcome],
    metrics: dict,
    cost: dict,
    fixtures: str,
    generated_at: str,
) -> str:
    """生成可复算的 Markdown 基线报告。"""
    lines = [
        "# Day6 三级路由基线报告",
        "",
        f"- 生成时间（UTC）：{generated_at}",
        f"- 评测集：`{fixtures}`（{metrics['total']} 条）",
        f"- 评测器版本：`{EVALUATOR_VERSION}`",
        f"- 代码版本：`{_git_commit()}`",
        "- 分类器：`local`（确定性关键词；Laya 未加载，接入后用同一集重跑对比）",
        "- 复算命令：`.\\.venv\\Scripts\\python.exe scripts\\eval_day6.py`",
        "",
        "## 1. 核心指标",
        "",
        "| 指标 | 结果 | 口径 |",
        "| --- | --- | --- |",
        f"| 分层准确率 | {_fmt_rate(metrics['layer_accuracy'])} | 预测层级 = 标注层级 |",
        f"| 意图准确率 | {_fmt_rate(metrics['intent_accuracy'])} "
        f"| {metrics['intent_samples']} 条标注了意图的样例 |",
        f"| 敏感召回率 | {_fmt_rate(metrics['sensitive_recall'])} "
        f"| {metrics['sensitive_samples']} 条敏感样例中被安全层拦截 |",
        f"| 敏感误杀率 | {_fmt_rate(metrics['sensitive_false_positive'])} "
        f"| 非敏感样例被判敏感（{metrics['false_positive_count']} 条） |",
        f"| S1 闭环率 | {_fmt_rate(metrics['closed_loop'])} "
        "| 未进入主控 Agent（安全/规则/分类器直接处理） |",
        f"| 工具调用准确率 | {_fmt_rate(metrics['tool_accuracy'])} "
        f"| {metrics['tool_samples']} 条期望调用工具的样例 |",
        f"| 异常率 | {_fmt_rate(metrics['error_rate'])} "
        f"| 路由抛异常（{metrics['error_count']} 条） |",
        f"| 人工接管率 | {_fmt_rate(metrics['human_takeover'])} "
        "| 判敏感并进入 HITL 待确认队列 |",
        "",
        "## 2. 分层分布",
        "",
        "| 层级 | 标注样本 | 实际命中 | 占比（实际/总数） |",
        "| --- | --- | --- | --- |",
    ]
    for layer in ("safety", "rule", "classifier", "agent"):
        hit = metrics["predicted_layers"][layer]
        lines.append(
            f"| {layer} | {metrics['expected_layers'][layer]} | {hit} "
            f"| {_fmt_rate(_rate(hit, metrics['total']))} |"
        )

    lines += [
        "",
        "## 3. 路由延迟",
        "",
        "| 指标 | 耗时（ms） |",
        "| --- | --- |",
        f"| 平均 | {_fmt_ms(metrics['latency_mean_ms'])} |",
        f"| P50 | {_fmt_ms(metrics['latency_p50_ms'])} |",
        f"| P95 | {_fmt_ms(metrics['latency_p95_ms'])} |",
        f"| 最大 | {_fmt_ms(metrics['latency_max_ms'])} |",
        "",
        "口径：只测 `route_text` 纯判定，不含 ASR/TTS/网络/落库；规则与安全层为内存匹配。",
        "",
        "## 4. 成本对比（Day5 全量大模型 → Day6 三级路由）",
        "",
        "| 项目 | Day5 全量大模型 | Day6 三级路由 | 降幅 |",
        "| --- | --- | --- | --- |",
        f"| LLM 调用次数 | {cost['baseline_calls']} | {cost['router_calls']} "
        f"| {_fmt_rate(1 - _rate(cost['router_calls'], cost['baseline_calls']) or 0)} |",
        f"| 输入字符数 | {cost['total_chars']} | {cost['agent_chars']} "
        f"| {_fmt_rate(1 - _rate(cost['agent_chars'], cost['total_chars']) or 0)} |",
        f"| 估算输入 token | {cost['baseline_tokens']} | {cost['router_tokens']} "
        f"| {_fmt_rate(1 - _rate(cost['router_tokens'], cost['baseline_tokens']) or 0)} |",
        "",
        f"口径：`估算 token = 字符数 / {CHARS_PER_TOKEN}`（中文粗估）；未计费，"
        "因为单价随厂商与上下文长度变动，写死价格不可复算。",
        "Agent 轮次按「至少 1 次 LLM 调用」估算，未计 ReAct 多步；实际成本以线上 trace 为准。",
        "",
        "## 5. bad case 回流",
        "",
    ]
    failures = metrics["failures"]
    if failures:
        lines += [
            f"失败 {len(failures)} 条，已写入 `eval/fixtures/day6_badcases.jsonl`（按 id 去重）。",
            "",
            "| id | 输入 | 标注 | 实际 | 失败原因 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for outcome in failures:
            sample = outcome.sample
            decision = outcome.decision
            actual = (
                f"{decision.layer.value}/{decision.intent.value}"
                if decision is not None
                else "exception"
            )
            expected = f"{sample['expected_layer']}/{sample.get('expected_intent') or '—'}"
            lines.append(
                f"| {sample['id']} | {sample['text']} | {expected} | {actual} "
                f"| {'；'.join(outcome.reasons)} |"
            )
    else:
        lines.append("本次评测无失败样例。")

    lines += [
        "",
        "## 6. 验收结论",
        "",
        f"- 敏感召回率 {_fmt_rate(metrics['sensitive_recall'])}，"
        f"{'达标' if (metrics['sensitive_recall'] or 0) >= 0.95 else '未达标'}"
        "（目标 ≥ 95%，宁可错杀）。",
        f"- 样本量 {metrics['total']} 条；测量方式为离线纯函数评测，可重复执行。",
        "- 局限：样例为人工构造，与规则/关键词存在同源偏差；"
        "本报告衡量回归稳定性而非开放场景泛化；"
        "不含 ASR 转写错误；Laya 权重未加载，Laya 层准确率需接入后单独测量；"
        "同义表达覆盖有限，失败样例经人工归因后再补规则或扩样例。",
    ]
    return "\n".join(lines) + "\n"


def write_badcases(failures: list[SampleOutcome], path: Path, generated_at: str) -> int:
    """把当前失败样例按 id 去重写入回流文件；已修复的样例自动移除。

    回流文件只保留「当前仍失败」的样例，避免回归集长期带着已修复项；
    历史记录由 Git 保留。无失败时删除该文件。
    """
    if not failures:
        path.unlink(missing_ok=True)
        return 0

    existing: dict[str, dict] = {}

    for outcome in failures:
        sample = outcome.sample
        decision = outcome.decision
        record = existing.get(sample["id"], {})
        record.update(
            {
                "id": sample["id"],
                "text": sample["text"],
                "reasons": outcome.reasons,
                "expected": {
                    "layer": sample["expected_layer"],
                    "intent": sample.get("expected_intent"),
                    "sensitive": bool(sample["expected_sensitive"]),
                    "category": sample.get("expected_category"),
                    "tool": sample.get("expected_tool"),
                    "clarify": bool(sample.get("expected_clarify")),
                },
                "predicted": {
                    "layer": decision.layer.value if decision else None,
                    "intent": decision.intent.value if decision else None,
                    "sensitive": decision.is_sensitive if decision else None,
                    "category": decision.sensitive_category if decision else None,
                    "tool": decision.tool if decision else None,
                    "clarify": decision.needs_clarification if decision else None,
                },
                "fixture": "day6_intents.jsonl",
                "last_seen": generated_at,
            }
        )
        record.setdefault("first_seen", generated_at)
        existing[sample["id"]] = record

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for key in sorted(existing):
            fh.write(json.dumps(existing[key], ensure_ascii=False) + "\n")
    return len(failures)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Day6 三级路由离线评测（50 条标注样例）")
    parser.add_argument("--fixtures", default=str(DEFAULT_FIXTURES), help="标注样例 JSONL")
    parser.add_argument("--out", default=str(DEFAULT_REPORT), help="Markdown 报告输出路径")
    parser.add_argument(
        "--badcases", default=str(DEFAULT_BADCASES), help="bad case 回流 JSONL"
    )
    parser.add_argument(
        "--no-badcases", action="store_true", help="只出报告，不写回流文件"
    )
    args = parser.parse_args(argv)

    fixtures_path = Path(args.fixtures)
    if not fixtures_path.exists():
        print(f"[失败] 评测集不存在：{fixtures_path}", file=sys.stderr)
        return 1
    samples = load_samples(fixtures_path)
    if not samples:
        print(f"[失败] 评测集为空：{fixtures_path}，不生成空报告。", file=sys.stderr)
        return 1

    outcomes = evaluate(samples)
    metrics = compute_metrics(outcomes)
    cost = cost_comparison(outcomes)
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    report = render_markdown(
        outcomes, metrics, cost, str(fixtures_path.relative_to(REPO_ROOT)), generated_at
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8", newline="\n")
    print(report)
    print(f"[已写入] {out_path}", file=sys.stderr)

    if not args.no_badcases:
        count = write_badcases(metrics["failures"], Path(args.badcases), generated_at)
        if count:
            print(f"[回流] {count} 条失败样例 → {args.badcases}", file=sys.stderr)
        else:
            print(f"[回流] 无失败样例，已清空 {args.badcases}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
