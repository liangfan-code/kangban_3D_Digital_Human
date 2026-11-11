"""真实语料验证：把真实长者说法灌进与黄金集完全相同的路由管道。

用法：
    python scripts/validate_real_corpus.py corpus.txt
    python scripts/validate_real_corpus.py corpus.jsonl --out logs/corpus_review/run1
    python scripts/validate_real_corpus.py corpus.csv --format csv --limit 50

输入（按后缀自动识别，可用 --format 覆盖）：
- .txt  每行一句；空行与 # 开头行跳过
- .jsonl 每行 {"text": ..., 可选 id/category/expect/note}
- .csv  必须含 text 列；可选 id/category/expect(JSON 字符串)/note

两种模式：
1. 已标注（行内带 expect）：复用 run_day6_golden.check 的同一套判定，直接给通过率。
2. 未标注：只输出判定、风险标记与补词候选，供人工标注后再进黄金集。

输出（默认 logs/corpus_review/<文件名>-<时间>/，禁止写入 eval/ 或 tests/）：
- review.jsonl               每句的判定与风险（含原始文本，不要提交）
- risk_cases.jsonl           疑似误判，补好 expect 即可作为黄金集候选
- supplement_candidates.jsonl 反向补词候选（未覆盖的品类/药名/控制/通知说法）

退出码恒为 0（报告模式）；输入错误返回 2。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from run_day6_golden import check as golden_check  # noqa: E402

from app.agent.companion import looks_multi_intent  # noqa: E402
from app.router.classifier import LocalIntentClassifier  # noqa: E402
from app.router.rules import normalize_text  # noqa: E402
from app.router.safety import match_sensitive  # noqa: E402
from app.router.service import route_text  # noqa: E402
from app.router.signals import (  # noqa: E402
    CONTROL_WORDS,
    FAMILY_WORDS,
    MEDIA_GENRES,
    MEDICATION_WORDS,
    NOTIFY_VERBS,
    detect_media_control,
    extract_genre,
    is_deferred_play_command,
    is_denied_media_command,
    is_followup_question,
    is_media_evaluation,
    is_media_preference,
    is_negated_play_command,
    looks_medication_query,
    looks_notify_intent,
)

# 仅用于诊断：路由层已先拦截「我不吃药」，这里再判一次是为了在真实语料里
# 发现回归，不参与任何生产判定。
_NEGATED_MEDICATION_RE = re.compile(r"(?:不|不想|不要|不用|别)(?:再)?(?:吃|服|喝)药")
_PLAY_HINT_RE = re.compile(r"播放|放|播|听|唱")
_CONTROL_HINT_RE = re.compile(
    r"声音|音量|声儿|大声|小声|调大|调小|关小|弄小|听不清|听不见"
)
_NOTIFY_HINT_RE = re.compile(r"发|打|说|讲|联系|告诉|通知|微信|视频|语音")
_KNOWN_WORDS = (
    MEDIA_GENRES
    + MEDICATION_WORDS
    + CONTROL_WORDS
    + NOTIFY_VERBS
    + FAMILY_WORDS
)

GUARDED_DIRS = (ROOT / "eval", ROOT / "tests")


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _read_txt(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            rows.append({"text": line})
    return rows


def _read_csv(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            row = {key: (value or "").strip() for key, value in raw.items() if key}
            if not row.get("text") or row["text"].startswith("#"):
                continue
            expect = row.get("expect") or ""
            try:
                row["expect"] = json.loads(expect) if expect else {}
            except json.JSONDecodeError:
                row["expect"] = {}
            rows.append(row)
    return rows


def _detect_format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jsonl", ".ndjson", ".json"}:
        return "jsonl"
    if suffix == ".csv":
        return "csv"
    return "txt"


def load_items(path: Path, fmt: str) -> tuple[list[dict], int, int]:
    """读入并去重：同一句只判一次，freq 记录出现次数。"""
    readings = {"jsonl": _read_jsonl, "csv": _read_csv, "txt": _read_txt}[fmt](path)
    items: dict[str, dict] = {}
    skipped = duplicates = 0
    for index, row in enumerate(readings, 1):
        text = str(row.get("text") or "").strip()
        if not text:
            skipped += 1
            continue
        if text in items:
            items[text]["freq"] += 1
            duplicates += 1
            continue
        items[text] = {
            "id": row.get("id") or f"rc-{index:04d}",
            "text": text,
            "category": row.get("category") or "",
            "expect": row.get("expect") or {},
            "note": row.get("note") or "",
            "freq": 1,
        }
    return list(items.values()), duplicates, skipped


def _risks(text: str, decision, sensitive) -> list[str]:
    normalized = normalize_text(text)
    layer = decision.layer.value
    if sensitive is not None:
        return ["安全拦截"]

    risks: list[str] = []
    if is_deferred_play_command(text) and decision.action != "media_defer":
        risks.append("延后指令误执行")
    if is_negated_play_command(text) and decision.action not in {"media_stop", "media_defer"}:
        risks.append("否定播放误执行")
    if _NEGATED_MEDICATION_RE.search(normalized) and decision.tool == "medication_query":
        risks.append("否定服药误执行")
    if is_denied_media_command(text) and decision.tool == "media_play":
        risks.append("否认句误执行")
    if is_followup_question(text) and decision.tool is not None:
        risks.append("追问误执行")
    if is_media_preference(normalized) and decision.tool == "media_play":
        risks.append("偏好句误执行")
    if is_media_evaluation(text) and decision.tool == "media_play":
        risks.append("评价句误执行")
    if (
        looks_multi_intent(text)
        and not decision.multi_intent
        and layer in {"rule", "classifier"}
    ):
        risks.append("多意图漏升级")
    if looks_notify_intent(text) and layer != "agent":
        risks.append("通知漏升级")
    return risks


def _supplements(text: str, decision, sensitive) -> list[str]:
    """反向补词候选：有明确意图线索，但现有信号词表没有覆盖。"""
    if sensitive is not None:
        return []
    normalized = normalize_text(text)
    found: list[str] = []

    if (
        extract_genre(text) is None
        and _PLAY_HINT_RE.search(normalized)
        and not is_media_preference(normalized)
        and not is_followup_question(text)
        and not is_denied_media_command(text)
        and detect_media_control(normalized) is None
    ):
        found.append("品类")
    if (
        "药" in normalized
        and not looks_medication_query(normalized)
        and not _NEGATED_MEDICATION_RE.search(normalized)
        and decision.intent.value != "medication_query"
    ):
        found.append("用药")
    if (
        _CONTROL_HINT_RE.search(normalized)
        and detect_media_control(normalized) is None
        and decision.action is None
    ):
        found.append("控制")
    family_hit = any(word in normalized for word in FAMILY_WORDS)
    if family_hit and not looks_notify_intent(text) and _NOTIFY_HINT_RE.search(normalized):
        found.append("通知")
    return found


def _residual(text: str) -> str:
    """去掉已收录信号词后剩下的片段，用来提示可能是新词。"""
    left = text
    for word in _KNOWN_WORDS:
        left = left.replace(word, " ")
    return re.sub(r"\s+", " ", left).strip()


def _decision_record(item: dict) -> dict:
    text = item["text"]
    # 与 tests/test_day6_golden.py 完全一致：显式使用本地分类器，
    # 不让外部 Laya provider 的在线状态影响验证结果的可复算性。
    decision = route_text(text, classifier=LocalIntentClassifier())
    sensitive = match_sensitive(text)
    risks = _risks(text, decision, sensitive)
    supplements = _supplements(text, decision, sensitive)
    return {
        "id": item["id"],
        "text": text,
        "freq": item["freq"],
        "category": item["category"],
        "layer": decision.layer.value,
        "intent": decision.intent.value,
        "action": decision.action,
        "tool": decision.tool,
        "safety": sensitive.category if sensitive is not None else None,
        "multi_intent": decision.multi_intent,
        "notify_intent": decision.notify_intent,
        "clarify": decision.needs_clarification,
        "genre": extract_genre(text),
        "risks": risks,
        "supplements": supplements,
        "residual": _residual(text) if supplements else "",
    }


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _is_guarded(out_dir: Path) -> bool:
    resolved = out_dir.resolve()
    for guarded in GUARDED_DIRS:
        target = guarded.resolve()
        if resolved == target or target in resolved.parents:
            return True
    return False


def _print_section(title: str, lines: list[str], limit: int) -> None:
    if not lines:
        return
    print(f"\n{title}：")
    for line in lines[:limit]:
        print(f"- {line}")
    if len(lines) > limit:
        print(f"  …… 其余 {len(lines) - limit} 条见输出文件")


def main() -> int:
    parser = argparse.ArgumentParser(description="真实语料路由验证")
    parser.add_argument("corpus", type=Path, help="语料文件（txt/jsonl/csv）")
    parser.add_argument(
        "--format",
        choices=["auto", "txt", "jsonl", "csv"],
        default="auto",
        help="输入格式；默认按后缀识别",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="输出目录；默认 logs/corpus_review/<文件名>-<时间>",
    )
    parser.add_argument("--limit", type=int, default=30, help="控制台每类最多打印多少条")
    args = parser.parse_args()

    if not args.corpus.is_file():
        print(f"找不到语料文件：{args.corpus}")
        return 2

    fmt = _detect_format(args.corpus) if args.format == "auto" else args.format
    items, duplicates, skipped = load_items(args.corpus, fmt)
    if not items:
        print(f"语料为空或格式不匹配（format={fmt}）：{args.corpus}")
        return 2

    out_dir = args.out or (
        ROOT
        / "logs"
        / "corpus_review"
        / f"{args.corpus.stem}-{datetime.now():%Y%m%d-%H%M%S}"
    )
    if _is_guarded(out_dir):
        print("输出目录不能放在 eval/ 或 tests/：真实语料不得进入黄金集目录")
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)

    records = [_decision_record(item) for item in items]
    labeled = [item for item in items if item["expect"]]

    label_failures: list[str] = []
    label_pass = 0
    for item in labeled:
        bad = golden_check({"text": item["text"], "expect": item["expect"]})
        if bad:
            label_failures.append(f"{item['id']} 《{item['text']}》 " + "; ".join(bad))
        else:
            label_pass += 1

    risk_counter: Counter[str] = Counter()
    supplement_counter: Counter[str] = Counter()
    layer_counter: Counter[str] = Counter()
    risk_rows: list[dict] = []
    supplement_rows: list[dict] = []
    for record in records:
        layer_counter[record["layer"]] += 1
        for risk in record["risks"]:
            risk_counter[risk] += 1
        for kind in record["supplements"]:
            supplement_counter[kind] += 1
        if record["risks"]:
            risk_rows.append(record)
        if record["supplements"]:
            supplement_rows.append(record)

    _write_jsonl(out_dir / "review.jsonl", records)
    _write_jsonl(out_dir / "risk_cases.jsonl", risk_rows)
    _write_jsonl(out_dir / "supplement_candidates.jsonl", supplement_rows)

    print(
        f"真实语料验证（format={fmt}）：共 {len(items)} 句，"
        f"重复合并 {duplicates} 句，空行跳过 {skipped} 行"
    )
    if labeled:
        print(f"已标注 {len(labeled)} 句：通过 {label_pass}，失败 {len(label_failures)}")
    else:
        print("未发现 expect 标注：本次只做风险扫描，通过率需人工标注后复跑")

    print("\n层级分布：" + "  ".join(f"{k}={v}" for k, v in layer_counter.most_common()))
    if risk_counter:
        print("风险分布：" + "  ".join(f"{k}={v}" for k, v in risk_counter.most_common()))
    else:
        print("风险分布：未发现疑似误判")
    if supplement_counter:
        print("补词候选：" + "  ".join(f"{k}={v}" for k, v in supplement_counter.most_common()))

    _print_section(
        "疑似误判",
        [f"{r['id']} 《{r['text']}》 " + "、".join(r["risks"]) for r in risk_rows],
        args.limit,
    )
    _print_section("标注失败", label_failures, args.limit)
    _print_section(
        "补词候选",
        [
            f"{r['id']} 《{r['text']}》 {','.join(r['supplements'])} 残留：{r['residual']}"
            for r in supplement_rows
        ],
        args.limit,
    )

    print(
        "\n输出（含原始语料，请勿提交）："
        f"\n  {out_dir / 'review.jsonl'}"
        f"\n  {out_dir / 'risk_cases.jsonl'}"
        f"\n  {out_dir / 'supplement_candidates.jsonl'}"
    )
    print(
        "下一步：把 risk_cases.jsonl 逐条补上 expect 后放入 eval/fixtures，"
        "再跑 scripts/run_day6_golden.py 与 pytest tests/test_day6_golden.py。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
