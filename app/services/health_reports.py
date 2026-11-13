"""健康趋势与家属日报（Day8 段3）。

约束：
- 只读聚合 ``health_records`` / ``health_events`` / 漏服工单，不写库；
- 自然日按 ``Settings.app_timezone`` 切分，同日多条取最后一次；
- 日报只做数值转述与状态说明，**不出现诊断词**、不给用药建议。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models import Elder, HealthEvent, HealthRecord, Workorder
from app.services.health_triage import GREEN, LEVEL_RANK, RED, YELLOW

logger = get_logger(__name__)

LEVEL_LABEL = {GREEN: "平稳", YELLOW: "需留意", RED: "需关注"}
MISSED_MEDICATION_TYPE = "missed_medication"
MAX_REPORT_CHARS = 200


def _tz() -> ZoneInfo:
    return ZoneInfo(get_settings().app_timezone)


def _local_today(now: datetime | None = None) -> date:
    anchor = now or datetime.now(UTC)
    return anchor.astimezone(_tz()).date()


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    tz = _tz()
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


async def build_trend(
    session: AsyncSession,
    elder_id: uuid.UUID,
    *,
    days: int = 14,
    now: datetime | None = None,
) -> dict:
    """近 N 天健康趋势：按自然日聚合，缺测日为 ``null``。"""
    span = max(1, min(int(days), 30))
    today = _local_today(now)
    start_day = today - timedelta(days=span - 1)
    start_dt, _ = _day_bounds(start_day)

    rows = (
        (
            await session.execute(
                select(HealthRecord)
                .where(
                    HealthRecord.elder_id == elder_id,
                    HealthRecord.recorded_at >= start_dt,
                )
                .order_by(HealthRecord.recorded_at)
            )
        )
        .scalars()
        .all()
    )

    tz = _tz()
    # recorded_at 升序遍历，后写的覆盖先写的 → 同日取最后一次
    by_day: dict[date, HealthRecord] = {}
    for row in rows:
        by_day[row.recorded_at.astimezone(tz).date()] = row

    series = []
    for offset in range(span):
        day = start_day + timedelta(days=offset)
        record = by_day.get(day)
        series.append(
            {
                "date": day.isoformat(),
                "bp_systolic": record.bp_systolic if record else None,
                "bp_diastolic": record.bp_diastolic if record else None,
                "sleep_minutes": record.sleep_minutes if record else None,
                "mood": record.mood if record else None,
            }
        )

    return {"days": span, "has_data": bool(rows), "series": series}


def _fmt_sleep(minutes: int | None) -> str:
    if minutes is None:
        return "未记录"
    hours, rest = divmod(int(minutes), 60)
    return f"{hours} 小时 {rest} 分" if hours else f"{rest} 分钟"


def _fmt_bp(systolic: int | None, diastolic: int | None) -> str:
    if systolic is None or diastolic is None:
        return "未记录"
    return f"{systolic}/{diastolic}"


async def build_daily_report(
    session: AsyncSession,
    elder_id: uuid.UUID,
    *,
    day: date | None = None,
    now: datetime | None = None,
) -> dict:
    """单日健康日报：最新体征 + 当日预警状态 + 漏服次数（纯转述，无诊断词）。"""
    target = day or _local_today(now)
    start, end = _day_bounds(target)

    elder_name = (
        await session.execute(select(Elder.name).where(Elder.elder_id == elder_id))
    ).scalar_one_or_none()
    name = elder_name or "长者"

    record = (
        (
            await session.execute(
                select(HealthRecord)
                .where(
                    HealthRecord.elder_id == elder_id,
                    HealthRecord.recorded_at >= start,
                    HealthRecord.recorded_at < end,
                )
                .order_by(HealthRecord.recorded_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )

    events = (
        (
            await session.execute(
                select(HealthEvent)
                .where(
                    HealthEvent.elder_id == elder_id,
                    HealthEvent.created_at >= start,
                    HealthEvent.created_at < end,
                )
                .order_by(HealthEvent.created_at)
            )
        )
        .scalars()
        .all()
    )

    level: str | None = None
    for event in events:
        current = str(event.level)
        if level is None or LEVEL_RANK.get(current, 0) > LEVEL_RANK.get(level, 0):
            level = current
    pending = sum(1 for e in events if str(e.status) == "pending_confirm")

    missed = (
        await session.execute(
            select(func.count())
            .select_from(Workorder)
            .where(
                Workorder.elder_id == elder_id,
                Workorder.type == MISSED_MEDICATION_TYPE,
                Workorder.created_at >= start,
                Workorder.created_at < end,
            )
        )
    ).scalar_one()

    head = f"{target.month}月{target.day}日健康日报 · {name}。"
    if record is None:
        body = "今日尚未采集到体征数据，建议睡前完成一次健康小问答。"
    else:
        body = (
            f"血压 {_fmt_bp(record.bp_systolic, record.bp_diastolic)}；"
            f"睡眠 {_fmt_sleep(record.sleep_minutes)}；"
            f"情绪 {record.mood or '未记录'}。"
        )
        if record.chief_complaint:
            body += f"主诉：{record.chief_complaint}。"

    if events:
        body += (
            f"健康预警 {len(events)} 条（{LEVEL_LABEL.get(level or '', '平稳')}，"
            f"待确认 {pending} 条）。"
        )
    body += f"今日漏服用药 {missed} 次。"

    text = head + body
    if len(text) > MAX_REPORT_CHARS:
        text = text[: MAX_REPORT_CHARS - 1] + "…"

    return {
        "date": target.isoformat(),
        "elder_id": elder_id,
        "elder_name": name,
        "level": level,
        "level_label": LEVEL_LABEL.get(level or "", None),
        "pending_events": pending,
        "missed_medication": int(missed),
        "text": text,
    }


async def synthesize_report_audio(text: str) -> tuple[bytes, int] | None:
    """日报朗读：复用 TTS Provider；未配置或失败返回 ``None``（由路由标降级）。"""
    from app.services.voice import get_tts_provider

    try:
        provider = get_tts_provider()
        if provider is None:
            return None
        # 合成加超时（沿用 TTS_TIMEOUT_SECONDS 口径）：慢供应商不拖住日报接口，
        # 超时走 tts_degraded 降级为文字日报
        async with asyncio.timeout(get_settings().tts_timeout_seconds):
            pcm = await provider.synthesize(text)
        if not pcm:
            return None
        return pcm, provider.sample_rate()
    except Exception as exc:  # noqa: BLE001 —— 朗读失败不阻断文本日报
        # 只记异常类型，不记日报文本
        logger.warning(
            "health_report_tts_failed",
            extra={"event": "health_report_tts_failed", "error": type(exc).__name__},
        )
        return None


__all__ = [
    "LEVEL_LABEL",
    "MAX_REPORT_CHARS",
    "build_daily_report",
    "build_trend",
    "synthesize_report_audio",
]
