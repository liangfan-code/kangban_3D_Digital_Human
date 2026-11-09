"""Day5.3 延迟基线统计脚本测试：日志筛选、百分位口径、Markdown 输出。"""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "latency_report.py"


@pytest.fixture(scope="module")
def report():
    spec = importlib.util.spec_from_file_location("latency_report", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _turn(a2a: float, llm: float, degraded: str | None = None) -> str:
    record = {
        "timestamp": "2026-11-08T10:00:00+00:00",
        "event": "turn_latency",
        "turn_id": "t1",
        "a2a_ms": a2a,
        "llm_first_token_ms": llm,
    }
    if degraded:
        record["degraded"] = degraded
    return json.dumps(record, ensure_ascii=False)


def test_percentile_matches_linear_interpolation(report):
    assert report.percentile([], 50) is None
    assert report.percentile([5.0], 95) == 5.0
    values = [1.0, 2.0, 3.0]
    assert report.percentile(values, 50) == pytest.approx(2.0)
    assert report.percentile(values, 95) == pytest.approx(2.9)


def test_load_turns_filters_non_json_and_other_events(report):
    lines = [
        "INFO:     Started server process",
        '{"event": "tts_synthesized", "tts_total_ms": 200}',
        _turn(1200.0, 500.0),
        "not json at all",
    ]
    turns = report.load_turns(lines)
    assert len(turns) == 1
    assert turns[0]["a2a_ms"] == 1200.0


def test_missing_stage_is_not_counted_as_zero(report):
    """未发生的阶段不参与统计（避免 None→0 拉低 P50）。"""
    turns = [
        {"event": "turn_latency", "a2a_ms": None, "llm_first_token_ms": 500.0},
        {"event": "turn_latency", "a2a_ms": None, "llm_first_token_ms": 700.0},
    ]
    rows = {row[0]: row for row in report.summarize(turns)}
    a2a_row = rows["a2a_ms"]
    llm_row = rows["llm_first_token_ms"]
    assert a2a_row[2] == 0
    assert llm_row[2] == 2
    assert llm_row[4] == pytest.approx(600.0)


def test_render_markdown_contains_table_and_degraded_distribution(report):
    turns = [
        {"event": "turn_latency", "timestamp": "t0", "a2a_ms": 1000.0},
        {"event": "turn_latency", "timestamp": "t1", "a2a_ms": 3000.0, "degraded": "llm_fallback"},
    ]
    markdown = report.render_markdown(turns, "logs/day5_latency.log")
    assert "轮次样本：2" in markdown
    assert "| 终句→首条语音（A2A）" in markdown
    assert "P95(ms)" in markdown
    assert "`llm_fallback`：1 次" in markdown


def test_main_writes_report_file(report, tmp_path, capsys):
    log_file = tmp_path / "day5.log"
    log_file.write_text(_turn(1500.0, 600.0) + "\n" + _turn(900.0, 400.0) + "\n", encoding="utf-8")
    out_file = tmp_path / "baseline.md"

    code = report.main([str(log_file), "--out", str(out_file)])

    assert code == 0
    assert out_file.read_text(encoding="utf-8").count("| 阶段 |") == 1
    assert "轮次样本：2" in capsys.readouterr().out


def test_main_reads_utf16_log_from_powershell_tee(report, tmp_path, capsys):
    """PowerShell 5 的 Tee-Object 默认写 UTF-16LE：必须按 BOM 自动识别。"""
    log_file = tmp_path / "utf16.log"
    log_file.write_text(_turn(1200.0, 500.0) + "\n", encoding="utf-16")
    out_file = tmp_path / "baseline.md"

    assert report.detect_encoding(log_file) == "utf-16"
    assert report.main([str(log_file), "--out", str(out_file)]) == 0
    assert "轮次样本：1" in out_file.read_text(encoding="utf-8")


def test_main_returns_error_without_turn_latency(report, tmp_path, capsys):
    """没有样本时不得生成空基线文件（曾误产出全「—」表格）。"""
    log_file = tmp_path / "empty.log"
    log_file.write_text("INFO: hello\n", encoding="utf-8")
    out_file = tmp_path / "baseline.md"
    assert report.main([str(log_file), "--out", str(out_file)]) == 1
    assert "未找到 event=turn_latency" in capsys.readouterr().err
    assert not out_file.exists()
