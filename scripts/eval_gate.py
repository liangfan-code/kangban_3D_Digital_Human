"""Day7 段7：eval gate 雏形（本地可跑，暂不接 CI）。

读取 ``scripts/eval_day7.py`` 生成的报告里的机器可读指标块：

    <!-- eval_metrics {"mode": "offline", "samples": 20, "task_success_rate": 1.0, ...} -->

默认阈值：task_success_rate >= 0.80、sensitive_recall >= 0.95、
tool_accuracy >= 0.80。样本不足 20 或指标缺失一律视为失败。
退出码 0 = 达标，1 = 不达标；``--warn-only`` 只告警不阻断。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

DEFAULT_THRESHOLDS: dict[str, float] = {
    "task_success_rate": 0.80,
    "sensitive_recall": 0.95,
    "tool_accuracy": 0.80,
}
MIN_SAMPLES = 20
_METRICS_RE = re.compile(r"<!--\s*eval_metrics\s*(\{.*?\})\s*-->", re.DOTALL)


def parse_report_metrics(report_text: str) -> dict[str, Any] | None:
    """从 Markdown 报告提取指标块；缺失或 JSON 损坏返回 None。"""
    match = _METRICS_RE.search(report_text)
    if match is None:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def load_metrics(
    report: str | Path | None = None, metrics_json: str | Path | None = None
) -> dict[str, Any] | None:
    if metrics_json:
        try:
            payload = json.loads(Path(metrics_json).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None
    if report:
        try:
            text = Path(report).read_text(encoding="utf-8")
        except OSError:
            return None
        return parse_report_metrics(text)
    return None


def load_thresholds(config: str | Path | None = None) -> dict[str, float]:
    """读取 YAML 阈值；只支持 ``key: value`` 形状，避免为 gate 引入硬依赖。"""
    thresholds = dict(DEFAULT_THRESHOLDS)
    if not config:
        return thresholds
    try:
        text = Path(config).read_text(encoding="utf-8")
    except OSError:
        return thresholds
    try:
        import yaml  # type: ignore[import-untyped]

        payload = yaml.safe_load(text) or {}
    except Exception:  # noqa: BLE001 —— 无 PyYAML 时退回逐行解析
        payload = {}
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip().strip("'\"")
            if key in DEFAULT_THRESHOLDS and value:
                payload[key] = value
    if not isinstance(payload, dict):
        return thresholds
    nested = payload.get("thresholds")
    source = nested if isinstance(nested, dict) else payload
    for key in DEFAULT_THRESHOLDS:
        if key in source:
            try:
                thresholds[key] = float(source[key])
            except (TypeError, ValueError):
                continue
    return thresholds


def evaluate(
    metrics: dict[str, Any] | None, thresholds: dict[str, float]
) -> tuple[bool, list[str]]:
    """返回 (是否达标, 失败说明列表)。"""
    if not metrics:
        return False, ["指标缺失：报告中未找到 eval_metrics 指标块"]

    failures: list[str] = []
    samples = metrics.get("samples")
    if not isinstance(samples, int) or samples < MIN_SAMPLES:
        failures.append(f"样本不足：samples={samples}，要求 >= {MIN_SAMPLES}")

    for key, threshold in thresholds.items():
        value = metrics.get(key)
        if value is None:
            failures.append(f"{key} 无数据")
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            failures.append(f"{key} 不是数字：{value!r}")
            continue
        if numeric < threshold:
            failures.append(f"{key}={numeric:.4f} < 阈值 {threshold:.2f}")
    return not failures, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Day7 eval gate：按阈值判定基线是否达标")
    parser.add_argument("--report", default="eval/reports/v1_baseline.md")
    parser.add_argument("--metrics-json", default=None, help="直接读取指标 JSON（优先级高于报告）")
    parser.add_argument("--config", default=None, help="YAML 阈值文件")
    parser.add_argument("--task-success-rate", type=float, default=None)
    parser.add_argument("--sensitive-recall", type=float, default=None)
    parser.add_argument("--tool-accuracy", type=float, default=None)
    parser.add_argument("--warn-only", action="store_true", help="只告警不阻断（退出码仍为 0）")
    args = parser.parse_args(argv)

    thresholds = load_thresholds(args.config)
    overrides = {
        "task_success_rate": args.task_success_rate,
        "sensitive_recall": args.sensitive_recall,
        "tool_accuracy": args.tool_accuracy,
    }
    for key, value in overrides.items():
        if value is not None:
            thresholds[key] = value

    metrics = load_metrics(args.report, args.metrics_json)
    passed, failures = evaluate(metrics, thresholds)
    threshold_text = ", ".join(f"{key}>={value:.2f}" for key, value in thresholds.items())
    if passed:
        print(f"[PASS] 指标达标（{threshold_text}），report={args.report}")
        return 0

    print(f"[FAIL] 指标未达标（{threshold_text}）：" + "；".join(failures))
    if args.warn_only:
        print("[WARN] --warn-only 已开启，不阻断（退出码 0）")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
