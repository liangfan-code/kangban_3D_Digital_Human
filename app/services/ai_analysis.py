"""家属端 AI 一键健康分析（Day2 v2 §3.4）。

安全红线（决策 D8/D9）：
- 仅 data_level=basic 记录出域；confidential（重大疾病）绝不出域；
- 出域报文脱敏：不含姓名/UUID/房间号，仅年龄段+性别+档案条目；
- 提示词服务端固定（prompt_version=v1），不信任前端传词；
- 未配置 LLM → 503 友好降级；超时 30s、失败重试 0 次、失败不留档；
- 同一长者每小时最多 1 次（429）；每次成功调用将出域快照与分析结果落库备审计。
"""

import time
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models import Elder, ElderHealthRecord, HealthAiAnalysis
from app.models.enums import HealthDataLevel, HealthRecordCategory

logger = get_logger(__name__)

PROMPT_VERSION = "v1"

SYSTEM_PROMPT = """角色：养老健康助手，面向家属解读长者健康档案（用「长者」代称，不出现姓名）。
输出要求：
1. 中文，总长不超过 300 字，杜绝冗长；
2. 开头一句话总体评价；
3. 「需要注意」分点列出（≤5 条）：直指异常指标/症状——指标名+数值+一句话通俗解释；
4. 每条后紧跟 1-2 条可执行的改善建议（饮食/运动/作息/何时就医）；
5. 禁止：诊断疾病名称结论、推荐处方药、编造档案中不存在的症状；
6. 结尾固定一句：「本分析仅供参考，不构成医疗建议，具体请遵医嘱。」"""

RATE_LIMIT_SECONDS = 3600  # 同一长者每小时最多 1 次


class LlmNotConfigured(Exception):
    """LLM 服务未配置（503 降级）。"""


class LlmCallFailed(Exception):
    """LLM 调用失败/超时（502，不留档）。"""


class AnalysisRateLimited(Exception):
    """同长者 1 小时内已分析过（429）。"""


def is_configured() -> bool:
    s = get_settings()
    return bool(s.llm_api_key and s.llm_base_url and s.llm_model)


def _age_band(elder: Elder) -> str | None:
    if elder.birth_date is None:
        return None
    age = (datetime.now(UTC).date() - elder.birth_date).days // 365
    if age < 60:
        return "60岁以下"
    if age < 70:
        return "60-69岁"
    if age < 80:
        return "70-79岁"
    if age < 90:
        return "80-89岁"
    return "90岁及以上"


def build_outbound_payload(elder: Elder, records: list[ElderHealthRecord]) -> dict:
    """组装出域报文：仅 basic 级 + 年龄段/性别；不含姓名/UUID/房间号。"""
    entries = [
        {
            "category": r.category,
            "item_key": r.item_key,
            "value": r.value_text,
            "severity": r.severity,
        }
        for r in records
        if r.data_level == HealthDataLevel.BASIC
        and r.category != HealthRecordCategory.MAJOR
    ]
    return {
        "age_band": _age_band(elder),
        "gender": elder.gender,
        "records": entries,
    }


async def check_rate_limit(session: AsyncSession, elder_id: UUID) -> None:
    last = (
        await session.execute(
            select(HealthAiAnalysis.created_at)
            .where(HealthAiAnalysis.elder_id == elder_id)
            .order_by(HealthAiAnalysis.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last is not None and datetime.now(UTC) - last < timedelta(seconds=RATE_LIMIT_SECONDS):
        raise AnalysisRateLimited("同一长者每小时仅可分析 1 次，请稍后再试")


async def call_llm(payload: dict) -> tuple[str, str]:
    """调用 System Two（DeepSeek/Qwen 兼容 API）。返回 (analysis_text, model_name)。"""
    s = get_settings()
    if not is_configured():
        raise LlmNotConfigured("未配置 AI 服务")
    user_content = (
        "以下是长者的基础健康档案（JSON），请按规则输出分析：\n" + str(payload)
    )
    body = {
        "model": s.llm_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "stream": False,
    }
    url = s.llm_base_url.rstrip("/") + "/chat/completions"
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=s.llm_timeout_seconds) as client:
            resp = await client.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {s.llm_api_key}"},
            )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        logger.warning(
            "llm call failed",
            extra={
                "event": "ai_analysis_llm_failed",
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "error_type": type(exc).__name__,
            },
        )
        raise LlmCallFailed("AI 服务调用失败，请稍后重试") from exc
    return text, s.llm_model


async def run_analysis(
    session: AsyncSession, elder: Elder, records: list[ElderHealthRecord], requested_by: UUID
) -> HealthAiAnalysis:
    """完整流程：频控 → 出域组装 → LLM → 落档。失败不留档。"""
    await check_rate_limit(session, elder.elder_id)
    payload = build_outbound_payload(elder, records)
    text, model_name = await call_llm(payload)
    row = HealthAiAnalysis(
        elder_id=elder.elder_id,
        input_snapshot=payload,
        analysis_text=text,
        model_name=model_name,
        prompt_version=PROMPT_VERSION,
        requested_by=requested_by,
    )
    session.add(row)
    await session.flush()
    logger.info(
        "ai analysis stored",
        extra={
            "event": "ai_analysis_created",
            "object_type": "health_ai_analysis",
            "object_id": str(row.analysis_id),
            "elder_id": str(elder.elder_id),
            "actor_id": str(requested_by),
        },
    )
    return row
