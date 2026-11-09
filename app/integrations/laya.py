"""Laya System One 适配器（Day6.2，可选 Provider）。

边界（AGENTS.md 安全禁区第 4 条）：
- 只加载**已预置在本地**的 checkpoint（``SYSTEM_ONE_MODEL_PATH``），绝不触发
  HuggingFace 自动下载；未预置 → ``available=False``，上层回落本地分类器；
- 应用启动不因 Laya 缺失/加载失败而失败，降级必须可见（日志 + trace）。

调用形态按 laya 公开接口：``agent.predict(state, questions)`` 返回
``out["answers"]["intent"]``，其中含 ``choice`` / ``probabilities`` /
``abstain`` / ``unbound``，整体 ``out["route"]`` 可为 ``review``。
"""

from __future__ import annotations

import importlib.util
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path

from app.router.classifier import ClassifierUnavailable
from app.router.safety import match_sensitive
from app.router.schemas import ClassifierResult, Intent
from app.system_one.questions import INTENT_QUESTION, PRIVACY_KEYWORDS

_VALID_LABELS = {intent.value for intent in Intent}
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="laya-s1")


class LayaClassifier:
    """把 Laya 的 choice 输出映射成项目统一的 ``ClassifierResult``。"""

    provider = "laya"

    def __init__(self, model_path: str | None, timeout_ms: int = 200) -> None:
        self._model_path = model_path
        self._timeout_s = max(timeout_ms, 1) / 1000
        self._agent = None
        self._lock = threading.Lock()

    @property
    def unavailable_reason(self) -> str | None:
        if importlib.util.find_spec("laya") is None:
            return "laya 未安装"
        if not self._model_path:
            return "SYSTEM_ONE_MODEL_PATH 未配置"
        if not Path(self._model_path).exists():
            return "本地 checkpoint 不存在"
        return None

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None

    def classify(self, text: str) -> ClassifierResult:
        if not self.available:
            raise ClassifierUnavailable(self.unavailable_reason or "laya 不可用")
        future = _EXECUTOR.submit(self._predict, text)
        try:
            return future.result(timeout=self._timeout_s)
        except FuturesTimeout as exc:
            raise ClassifierUnavailable("laya 分类超时") from exc

    def _predict(self, text: str) -> ClassifierResult:
        with self._lock:
            if self._agent is None:
                import laya  # 延迟导入：未安装时不影响应用启动

                self._agent = laya.Agent(self._model_path)

        out = self._agent.predict(text, INTENT_QUESTION)
        answer = (out.get("answers") or {}).get("intent") or {}
        choice = answer.get("choice") or Intent.OTHER.value
        probabilities = answer.get("probabilities") or {}
        confidence = (
            max(probabilities.values())
            if probabilities
            else float(answer.get("answer_confidence") or 0.0)
        )
        is_out_of_domain = bool(answer.get("unbound") or answer.get("abstain")) or (
            out.get("route") == "review"
        )
        return ClassifierResult(
            intent=Intent(choice) if choice in _VALID_LABELS else Intent.OTHER,
            confidence=round(min(max(confidence, 0.0), 1.0), 3),
            is_sensitive=match_sensitive(text) is not None,
            is_private=any(keyword in text for keyword in PRIVACY_KEYWORDS),
            is_out_of_domain=is_out_of_domain,
            provider=self.provider,
        )
