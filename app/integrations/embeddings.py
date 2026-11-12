"""Embedding Provider（Day7 段5）：远程 OpenAI 兼容 + 本地哈希降级。

口径：
- 远程：读取 ``EMBEDDING_BASE_URL / EMBEDDING_API_KEY / EMBEDDING_MODEL``，
  POST ``{base_url}/embeddings``，超时与错误统一抛 ``EmbeddingUnavailable``；
- 降级：``HashNgramEmbedding`` 对中文按字符 1–2 gram 做特征哈希到固定 256 维并
  L2 归一化，``model="hash-ngram-256"``。这是真实可计算的向量，不下载任何模型权重；
- 统一入口 ``get_embedding_provider()``：配置可用走远程（默认
  ``text-embedding-3-small`` / 1536 维），否则用本地哈希，并在首次使用时记一次
  ``event=embedding_fallback``、``degraded=embedding_unavailable`` 与实际模型/维度；
- 远程调用失败一次即永久切到本地哈希（``available=False`` 语义），保证全链路可用；
- 不同模型/维度不混算：记忆服务按 ``embedding_model == provider.model`` 过滤。
"""

from __future__ import annotations

import hashlib
import math
from abc import ABC, abstractmethod

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_REMOTE_MODEL = "text-embedding-3-small"
DEFAULT_REMOTE_DIMENSION = 1536
HASH_MODEL = "hash-ngram-256"
HASH_DIMENSION = 256


class EmbeddingUnavailable(Exception):
    """Embedding 未配置或调用失败；调用方必须降级，不得中断主流程。"""


class EmbeddingProvider(ABC):
    """Embedding 抽象：只负责文本 → 向量。"""

    provider: str
    model: str
    dimension: int
    available: bool

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量文本转向量；顺序与入参一致。"""


class HashNgramEmbedding(EmbeddingProvider):
    """本地字符 n-gram 特征哈希：确定性、零依赖、可离线。"""

    provider = "hash-ngram"
    model = HASH_MODEL
    dimension = HASH_DIMENSION
    available = True

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        cleaned = "".join(str(text).split())
        if not cleaned:
            return vector
        for n in (1, 2):
            for index in range(len(cleaned) - n + 1):
                gram = cleaned[index : index + n]
                digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
                value = int.from_bytes(digest, "big")
                bucket = value % self.dimension
                # 有符号哈希降低碰撞带来的系统性偏置
                sign = 1.0 if (value >> 63) & 1 else -1.0
                vector[bucket] += sign / n
        norm = math.sqrt(sum(item * item for item in vector))
        if norm == 0.0:
            return vector
        return [item / norm for item in vector]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]


class OpenAICompatibleEmbedding(EmbeddingProvider):
    """OpenAI 兼容 ``/embeddings`` 客户端；失败即置 ``available=False``。"""

    provider = "openai"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str | None = None,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model or DEFAULT_REMOTE_MODEL
        # 默认维度仅在首次响应前使用；拿到真实向量后按实际长度回填
        self.dimension = DEFAULT_REMOTE_DIMENSION
        self.available = True
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._transport = transport

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        url = f"{self._base_url}/embeddings"
        body = {"model": self.model, "input": texts}
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(
                    url, json=body, headers={"Authorization": f"Bearer {self._api_key}"}
                )
        except httpx.HTTPError as exc:
            self.available = False
            raise EmbeddingUnavailable(
                f"embedding 调用失败：{type(exc).__name__}"
            ) from exc
        if response.status_code != 200:
            self.available = False
            raise EmbeddingUnavailable(f"embedding HTTP {response.status_code}")
        try:
            data = response.json()["data"]
            ordered = sorted(data, key=lambda item: item.get("index", 0))
            vectors = [list(item["embedding"]) for item in ordered]
        except (KeyError, TypeError, ValueError) as exc:
            self.available = False
            raise EmbeddingUnavailable("embedding 响应格式错误") from exc
        if len(vectors) != len(texts):
            self.available = False
            raise EmbeddingUnavailable("embedding 返回数量与入参不一致")
        if vectors and vectors[0]:
            self.dimension = len(vectors[0])
        return vectors


class _FallbackEmbedding(EmbeddingProvider):
    """远程优先；首次失败永久切本地哈希（进程内只降级一次）。"""

    def __init__(
        self, primary: EmbeddingProvider, fallback: EmbeddingProvider
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._using_fallback = False

    @property
    def provider(self) -> str:  # type: ignore[override]
        return self._fallback.provider if self._using_fallback else self._primary.provider

    @property
    def model(self) -> str:  # type: ignore[override]
        return self._fallback.model if self._using_fallback else self._primary.model

    @property
    def dimension(self) -> int:  # type: ignore[override]
        return self._fallback.dimension if self._using_fallback else self._primary.dimension

    @property
    def available(self) -> bool:  # type: ignore[override]
        return self._fallback.available if self._using_fallback else self._primary.available

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self._using_fallback:
            return await self._fallback.embed(texts)
        try:
            return await self._primary.embed(texts)
        except EmbeddingUnavailable as exc:
            self._switch(exc)
            return await self._fallback.embed(texts)

    def _switch(self, exc: EmbeddingUnavailable) -> None:
        if self._using_fallback:
            return
        self._using_fallback = True
        logger.warning(
            "embedding 远程不可用，降级为本地哈希",
            extra={
                "event": "embedding_fallback",
                "degraded": "embedding_unavailable",
                "provider": self._fallback.provider,
                "model": self._fallback.model,
                "dimension": self._fallback.dimension,
                "error_type": type(exc).__name__,
            },
        )


_provider: EmbeddingProvider | None = None
_fallback_logged = False


def _log_local_fallback_once() -> None:
    global _fallback_logged
    if _fallback_logged:
        return
    _fallback_logged = True
    logger.warning(
        "embedding 未配置，使用本地哈希降级",
        extra={
            "event": "embedding_fallback",
            "degraded": "embedding_unavailable",
            "provider": HashNgramEmbedding.provider,
            "model": HASH_MODEL,
            "dimension": HASH_DIMENSION,
        },
    )


def get_embedding_provider() -> EmbeddingProvider:
    """进程内单例：配置可用走远程，否则本地哈希（首次使用记一次降级日志）。"""
    global _provider
    if _provider is not None:
        return _provider
    settings = get_settings()
    base_url = (settings.embedding_base_url or "").strip()
    api_key = (settings.embedding_api_key or "").strip()
    force_local = (settings.embedding_provider or "").strip().lower() in {
        "hash-ngram",
        "hash_ngram",
        "local",
    }
    if base_url and api_key and not force_local:
        _provider = _FallbackEmbedding(
            OpenAICompatibleEmbedding(
                base_url=base_url,
                api_key=api_key,
                model=settings.embedding_model,
                timeout=settings.embedding_timeout_seconds,
            ),
            HashNgramEmbedding(),
        )
        return _provider
    _provider = HashNgramEmbedding()
    _log_local_fallback_once()
    return _provider


def reset_embedding_provider() -> None:
    """测试辅助：清空进程内单例与降级日志标记。"""
    global _provider, _fallback_logged
    _provider = None
    _fallback_logged = False


__all__ = [
    "DEFAULT_REMOTE_MODEL",
    "EmbeddingProvider",
    "EmbeddingUnavailable",
    "HashNgramEmbedding",
    "OpenAICompatibleEmbedding",
    "get_embedding_provider",
    "reset_embedding_provider",
]
