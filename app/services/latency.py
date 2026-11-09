"""单轮语音延迟打点（Day5.2）：只记录阶段耗时与降级标记。

口径（一次完整对话 = 一个 TurnTrace）：
- ``asr_connect_ms``：终句触发 → ASR 会话建连完成；
- ``asr_e2e_ms``：开始喂 PCM → 终句返回（沿用 Day3 口径，不含建连）；
- ``llm_first_token_ms``：发起请求 → 首个 delta；
- ``llm_total_ms``：发起请求 → 流结束；
- ``tts_first_byte_ms`` / ``tts_total_ms`` / ``tts_decode_ms``：Provider 上报的第一句耗时；
- ``viseme_ms``：PCM 就绪 → 口型时间轴算完（各句累加）；
- ``a2a_ms``：终句触发 → 第一条 ``tts`` 事件下发（最关键的端到端指标）；
- ``turn_total_ms``：终句触发 → ``done`` 事件。

隐私边界（AGENTS §实时/语音模块规则 4）：只写时长、数量、降级原因与
服务端 Runtime Context 标识（device_id / elder_id / thread_id），
不写转写文本、回复正文与音频字节。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from app.core.logging import get_logger

logger = get_logger(__name__)


def _round_ms(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


@dataclass
class TurnTrace:
    """一次对话的延迟采集器；由调用方显式传递，不用全局单例。"""

    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    _started: float = field(default_factory=time.perf_counter)
    _marks: dict[str, float] = field(default_factory=dict)
    _durations: dict[str, float] = field(default_factory=dict)
    _counts: dict[str, int] = field(default_factory=dict)
    _degraded: list[str] = field(default_factory=list)
    _meta: dict[str, str] = field(default_factory=dict)
    _logged: bool = False

    # ---- 采集入口 ----

    def mark(self, stage: str) -> None:
        """覆盖式打点：用于 end 类阶段（同一轮可能多次，保留最后一次）。"""
        self._marks[stage] = time.perf_counter()

    def mark_once(self, stage: str) -> None:
        """只打第一次：用于首句 TTS 相关阶段。"""
        self._marks.setdefault(stage, time.perf_counter())

    def add_duration(self, name: str, ms: float | None) -> None:
        """累加 Provider 上报的耗时（如多句 TTS 的合成总时长）。"""
        if ms is None:
            return
        self._durations[name] = self._durations.get(name, 0.0) + float(ms)

    def set_duration_once(self, name: str, ms: float | None) -> None:
        """只保留第一次上报值（如首句首包延迟，不被后续句子覆盖）。"""
        if ms is None or name in self._durations:
            return
        self._durations[name] = float(ms)

    def incr(self, name: str) -> None:
        self._counts[name] = self._counts.get(name, 0) + 1

    def degrade(self, reason: str) -> None:
        if reason not in self._degraded:
            self._degraded.append(reason)

    def set_meta(self, key: str, value: str | None) -> None:
        if value:
            self._meta[key] = value

    # ---- 汇总与输出 ----

    def _span(self, start: str | None, end: str) -> float | None:
        """两个打点之间的毫秒差；start 为 None 时以轮次开始为基准。"""
        begin = self._started if start is None else self._marks.get(start)
        finish = self._marks.get(end)
        if begin is None or finish is None:
            return None
        return (finish - begin) * 1000

    def payload(self) -> dict[str, float | int | str | None]:
        """组装日志字段；缺失阶段输出 None，便于区分「未发生」与「0ms」。"""
        payload: dict[str, float | int | str | None] = {
            "turn_id": self.turn_id,
            "turn_total_ms": _round_ms(self._span(None, "done")),
            "asr_connect_ms": _round_ms(self._span(None, "asr_ready")),
            "asr_e2e_ms": _round_ms(self._span("asr_ready", "asr_final")),
            "llm_first_token_ms": _round_ms(self._span("llm_start", "llm_first_token")),
            "llm_total_ms": _round_ms(self._span("llm_start", "llm_end")),
            "a2a_ms": _round_ms(self._span(None, "tts_event_sent")),
            "viseme_ms": _round_ms(self._durations.get("viseme_ms")),
            "tts_first_byte_ms": _round_ms(self._durations.get("tts_first_byte_ms")),
            "tts_total_ms": _round_ms(self._durations.get("tts_total_ms")),
            "tts_decode_ms": _round_ms(self._durations.get("tts_decode_ms")),
            "tts_sentences": self._counts.get("tts_sentences"),
        }
        if self._degraded:
            payload["degraded"] = ",".join(self._degraded)
        payload.update(self._meta)
        return payload

    def log(self, *, event: str = "turn_latency", **extra: str | None) -> None:
        """输出一条结构化延迟日志；同一轮只写一次。

        ``event`` 默认 ``turn_latency``（语音轮次）；文字通道传 ``text_latency``，
        避免没有 TTS 的样本污染语音基线统计。
        """
        if self._logged:
            return
        self._logged = True
        payload = self.payload()
        payload.update({k: v for k, v in extra.items() if v})
        logger.info(event, extra={"event": event, **payload})
