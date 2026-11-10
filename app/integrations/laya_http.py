"""Laya System One HTTP 适配器（方案 C：常驻 laya-serve）。

与 ``LayaClassifier``（进程内加载）的区别：
- 本进程不再加载 614MB 权重，改为调用 ``laya-serve`` 的 ``POST /v1/systemone``；
- 服务未启动、超时或返回非 2xx → ``ClassifierUnavailable``，上层降级到本地分类器；
- 只用标准库 ``urllib``，不新增运行时依赖。

输出解析与进程内实现保持同一口径（``answers.intent`` 的 ``choice`` / ``probabilities``
/ ``abstain`` / ``unbound``），避免两个 Provider 行为漂移。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request

from app.router.classifier import ClassifierUnavailable
from app.router.safety import match_sensitive
from app.router.schemas import ClassifierResult, Intent
from app.system_one.questions import INTENT_QUESTION, PRIVACY_KEYWORDS

_VALID_LABELS = {intent.value for intent in Intent}


class LayaHttpClassifier:
    """调用常驻 laya-serve 完成一次意图分类。"""

    provider = "laya_http"

    def __init__(
        self,
        base_url: str | None,
        model_name: str | None = None,
        timeout_ms: int = 200,
        api_key: str | None = None,
    ) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._model_name = model_name or None
        self._timeout_s = max(timeout_ms, 1) / 1000
        self._api_key = api_key or None

    @property
    def unavailable_reason(self) -> str | None:
        # 只校验配置，不在构造期发网络请求：服务没起也要让应用正常启动。
        if not self._base_url:
            return "SYSTEM_ONE_BASE_URL 未配置"
        return None

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None

    def classify(self, text: str) -> ClassifierResult:
        if not self.available:
            raise ClassifierUnavailable(self.unavailable_reason or "laya-serve 不可用")

        payload: dict[str, object] = {"state": text, "questions": INTENT_QUESTION}
        if self._model_name:
            payload["model"] = self._model_name

        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = urllib.request.Request(
            f"{self._base_url}/v1/systemone",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise ClassifierUnavailable(f"laya-serve 返回 HTTP {exc.code}") from exc
        except (urllib.error.URLError, socket.timeout) as exc:
            raise ClassifierUnavailable("laya-serve 不可达或超时") from exc

        try:
            result = json.loads(body)
        except ValueError as exc:
            raise ClassifierUnavailable("laya-serve 返回非 JSON") from exc

        return self._to_classifier_result(text, result)

    def _to_classifier_result(self, text: str, result: dict) -> ClassifierResult:
        answer = (result.get("answers") or {}).get("intent") or {}
        choice = answer.get("choice") or Intent.OTHER.value
        probabilities = answer.get("probabilities") or {}
        confidence = (
            max(probabilities.values())
            if probabilities
            else float(answer.get("answer_confidence") or 0.0)
        )
        routing = result.get("routing") or {}
        route = result.get("route") or routing.get("route")
        is_out_of_domain = bool(answer.get("unbound") or answer.get("abstain")) or (
            route == "review"
        )
        return ClassifierResult(
            intent=Intent(choice) if choice in _VALID_LABELS else Intent.OTHER,
            confidence=round(min(max(confidence, 0.0), 1.0), 3),
            is_sensitive=match_sensitive(text) is not None,
            is_private=any(keyword in text for keyword in PRIVACY_KEYWORDS),
            is_out_of_domain=is_out_of_domain,
            provider=self.provider,
        )
