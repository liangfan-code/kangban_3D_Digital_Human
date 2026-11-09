"""Day5.3 延迟基线统计：从 uvicorn JSON 日志中提取 turn_latency，算 P50/P95。

用法（先启动服务并把日志重定向到文件，再跑 20 轮"今天天气怎么样"）::

    .\\.venv\\Scripts\\python.exe -m uvicorn app.main:app --reload > logs\\day5_latency.log 2>&1
    .\\.venv\\Scripts\\python.exe scripts\\latency_report.py logs\\day5_latency.log

也可以直接读剪贴板/终端输出保存的文本（非 JSON 行自动跳过），或写文件::

    .\\.venv\\Scripts\\python.exe scripts\\latency_report.py logs\\d5.log --out docs\\p95.md

口径：只统计 ``event=turn_latency`` 的行；缺失阶段（None）不参与该阶段样本，
避免把「未发生」当成 0ms 拉低 P50。全程离线，不连数据库、不调外部服务。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

# 阶段展示顺序与中文名（与 app/services/latency.py 的 payload 字段一一对应）
STAGE_LABELS: tuple[tuple[str, str], ...] = (
    ("asr_connect_ms", "ASR 建连"),
    ("asr_e2e_ms", "ASR 端到端"),
    ("llm_first_token_ms", "LLM 首 token"),
    ("llm_total_ms", "LLM 总耗时"),
    ("a2a_ms", "终句→首条语音（A2A）"),
    ("viseme_ms", "口型计算"),
    ("tts_first_byte_ms", "TTS 首包"),
    ("tts_decode_ms", "TTS 解码"),
    ("tts_total_ms", "TTS 总耗时"),
    ("turn_total_ms", "整轮对话"),
)


def percentile(values: list[float], p: float) -> float | None:
    """线性插值百分位（与 numpy 默认口径一致），空列表返回 None。"""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * p / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[int(position)]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def detect_encoding(path: Path) -> str:
    """按 BOM 识别编码：PowerShell 5 的 Tee-Object/重定向默认写 UTF-16LE。"""
    with path.open("rb") as fh:
        head = fh.read(2)
    if head in (b"\xff\xfe", b"\xfe\xff"):
        return "utf-16"
    return "utf-8"


def load_turns(line_iter) -> list[dict]:
    """逐行解析 JSON，只保留 turn_latency；非 JSON / 其他事件静默跳过。"""
    turns: list[dict] = []
    for line in line_iter:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("event") == "turn_latency":
            turns.append(record)
    return turns


def summarize(
    turns: list[dict],
) -> list[tuple[str, str, int, float | None, float | None, float | None, float | None]]:
    """按阶段汇总 count/min/P50/P95/max；无样本阶段也保留一行（显示「—」）。"""
    rows = []
    for key, label in STAGE_LABELS:
        samples = [float(t[key]) for t in turns if isinstance(t.get(key), (int, float))]
        if samples:
            rows.append(
                (
                    key,
                    label,
                    len(samples),
                    min(samples),
                    percentile(samples, 50),
                    percentile(samples, 95),
                    max(samples),
                )
            )
        else:
            rows.append((key, label, 0, None, None, None, None))
    return rows


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


def render_markdown(turns: list[dict], source: str) -> str:
    """输出可直接粘进 docs 的 Markdown 基线表。"""
    stamps = [t.get("timestamp") for t in turns if isinstance(t.get("timestamp"), str)]
    degraded = Counter()
    for turn in turns:
        for reason in str(turn.get("degraded", "")).split(","):
            if reason:
                degraded[reason] += 1

    lines = [
        "### 延迟基线（turn_latency）",
        "",
        f"- 数据来源：`{source}`",
        f"- 轮次样本：{len(turns)}",
    ]
    if stamps:
        lines.append(f"- 时间范围：{stamps[0]} ~ {stamps[-1]}")
    lines += [
        "- 口径：只统计每轮实际发生的阶段；缺失阶段不参与该行统计",
        "",
        "| 阶段 | 样本 | Min(ms) | P50(ms) | P95(ms) | Max(ms) |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for key, label, count, vmin, v50, v95, vmax in summarize(turns):
        if count == 0:
            lines.append(f"| {label} (`{key}`) | 0 | — | — | — | — |")
        else:
            lines.append(
                f"| {label} (`{key}`) | {count} | {_fmt(vmin)} | {_fmt(v50)} "
                f"| {_fmt(v95)} | {_fmt(vmax)} |"
            )

    lines += ["", "**降级分布**："]
    if degraded:
        for reason, count in degraded.most_common():
            lines.append(f"- `{reason}`：{count} 次")
    else:
        lines.append("- 无降级（全部轮次走完整链路）")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从 JSON 日志统计 turn_latency 的 P50/P95")
    parser.add_argument(
        "paths",
        nargs="*",
        default=["-"],
        help="日志文件路径，可多个；- 表示从标准输入读取（默认）",
    )
    parser.add_argument("--out", help="把 Markdown 结果写入文件（默认只打印到终端）")
    args = parser.parse_args(argv)

    turns: list[dict] = []
    sources: list[str] = []
    for path in args.paths:
        if path == "-":
            turns.extend(load_turns(sys.stdin))
            sources.append("stdin")
            continue
        file_path = Path(path)
        if not file_path.exists():
            print(f"[跳过] 文件不存在：{path}", file=sys.stderr)
            continue
        with file_path.open(
            encoding=detect_encoding(file_path), errors="replace"
        ) as fh:
            turns.extend(load_turns(fh))
        sources.append(path)

    if not turns:
        print(
            "未找到 event=turn_latency 的日志行：确认采集时服务正在运行、"
            "日志级别为 INFO，且日志文件路径正确。未生成基线文件。",
            file=sys.stderr,
        )
        return 1

    report = render_markdown(turns, ", ".join(sources) or "无输入")
    print(report)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8")
        print(f"[已写入] {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
