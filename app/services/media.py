"""媒体点播服务（Day7 段6）：授权选片 → 开始/结束落 media_history。

铁律：
- 只返回应用内相对地址（``/static/media/`` 前缀），外链一律视为不可播；
- 只选 ``status=active`` 且授权状态为 licensed/teaching/public_domain 的条目；
- 所有 ``media_history`` 读写都带 ``elder_id``，防止伪造 history_id 越权补结束；
- 日志只记 elder_id/media_id/genre 等元数据，不记音频内容与地址。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.enums import MediaCopyrightStatus, MediaStatus
from app.models.media import MediaLibrary
from app.models.media_history import MediaHistory

logger = get_logger(__name__)

PLAYABLE_COPYRIGHT: tuple[str, ...] = (
    MediaCopyrightStatus.LICENSED,
    MediaCopyrightStatus.TEACHING,
    MediaCopyrightStatus.PUBLIC_DOMAIN,
)
DEFAULT_GENRE = "越剧"
RECENT_LIMIT = 20
INTERNAL_PREFIX = "/static/media/"
GENRE_HINTS: tuple[str, ...] = (
    "越剧",
    "京剧",
    "黄梅戏",
    "沪剧",
    "评弹",
    "昆曲",
    "老歌",
    "电影",
)


def _to_view(row: MediaLibrary) -> dict:
    return {
        "media_id": str(row.media_id),
        "title": row.title,
        "genre": row.genre,
        "artist": row.artist,
        "url": row.media_uri,
        "duration_seconds": row.duration_seconds,
    }


def _playable_stmt(genre: str | None):
    conditions = [
        MediaLibrary.status == MediaStatus.ACTIVE,
        MediaLibrary.copyright_status.in_(PLAYABLE_COPYRIGHT),
        MediaLibrary.media_uri.is_not(None),
        MediaLibrary.media_uri.like(f"{INTERNAL_PREFIX}%"),
    ]
    if genre:
        conditions.append(MediaLibrary.genre == genre)
    return select(MediaLibrary).where(*conditions)


async def _load_playable(db: AsyncSession, genre: str | None) -> list[MediaLibrary]:
    rows = (await db.execute(_playable_stmt(genre))).scalars().all()
    return list(rows)


def _ranked(rows: list[MediaLibrary], recent_ids: list[str]) -> list[MediaLibrary]:
    """未播过的优先；播过的按「越久以前播过越靠前」，最近播放的排最后。"""
    order = {media_id: index for index, media_id in enumerate(recent_ids)}
    total = len(recent_ids)

    def key(row: MediaLibrary) -> tuple[int, str]:
        media_id = str(row.media_id)
        if media_id not in order:
            return (0, row.title or "")
        return (total - order[media_id], row.title or "")

    return sorted(rows, key=key)


async def _recent_media_ids(db: AsyncSession, elder_id: UUID) -> list[str]:
    rows = (
        await db.execute(
            select(MediaHistory.media_id)
            .where(MediaHistory.elder_id == elder_id)
            .order_by(MediaHistory.started_at.desc())
            .limit(RECENT_LIMIT)
        )
    ).scalars().all()
    return [str(item) for item in rows]


async def _preferred_genre(db: AsyncSession, elder_id: UUID) -> str:
    """记忆里的偏好品类；未配置/失败/无命中一律回落默认品类。"""
    from app.services import memory as memory_service

    try:
        memories = await memory_service.recall(db, elder_id, "喜欢听的戏曲 老歌 电影")
    except Exception as exc:  # noqa: BLE001 —— 偏好召回失败不影响点播
        logger.warning(
            "媒体偏好召回降级",
            extra={
                "event": "media_preference_degraded",
                "elder_id": str(elder_id),
                "error_type": type(exc).__name__,
            },
        )
        return DEFAULT_GENRE
    for row in memories:
        if getattr(row, "category", None) != "preference":
            continue
        content = str(getattr(row, "content", "") or "")
        for genre in GENRE_HINTS:
            if genre in content:
                return genre
    return DEFAULT_GENRE


async def pick_for_play(
    db: AsyncSession, elder_id: UUID, genre: str | None = None
) -> dict | None:
    """按授权 + active + 品类/偏好选一条可播媒体；没有可用素材返回 None。"""
    requested = (genre or "").strip() or None
    target_genre = requested or await _preferred_genre(db, elder_id)
    recent = await _recent_media_ids(db, elder_id)

    rows = await _load_playable(db, target_genre)
    if not rows:
        rows = await _load_playable(db, None)
    if not rows:
        logger.info(
            "无可播媒体",
            extra={
                "event": "media_pick_empty",
                "elder_id": str(elder_id),
                "genre": target_genre,
            },
        )
        return None

    chosen = _ranked(rows, recent)[0]
    logger.info(
        "媒体选片完成",
        extra={
            "event": "media_picked",
            "elder_id": str(elder_id),
            "media_id": str(chosen.media_id),
            "genre": chosen.genre,
        },
    )
    return _to_view(chosen)


async def next_for(
    db: AsyncSession, elder_id: UUID, current: dict | None = None
) -> dict | None:
    """同品类循环下一条；同品类无可换时回退到其他可播项（排除当前）。"""
    current = current or {}
    current_id = str(current.get("media_id")) if current.get("media_id") else None
    genre = (current.get("genre") or "").strip() or None
    recent = await _recent_media_ids(db, elder_id)

    candidates = _ranked(await _load_playable(db, genre), recent) if genre else []
    ids = [str(row.media_id) for row in candidates]

    if current_id is None:
        pool = candidates or _ranked(await _load_playable(db, None), recent)
        return _to_view(pool[0]) if pool else None

    if current_id in ids and len(ids) > 1:
        return _to_view(candidates[(ids.index(current_id) + 1) % len(ids)])

    fallback = [
        row
        for row in _ranked(await _load_playable(db, None), recent)
        if str(row.media_id) != current_id
    ]
    return _to_view(fallback[0]) if fallback else None


async def record_start(db: AsyncSession, elder_id: UUID, media: dict) -> UUID:
    """插入一条播放历史（started_at=now），返回 history_id；由调用方提交。"""
    history = MediaHistory(
        elder_id=elder_id,
        media_id=UUID(str(media["media_id"])),
        started_at=datetime.now(UTC),
    )
    db.add(history)
    await db.flush()
    logger.info(
        "媒体播放开始",
        extra={
            "event": "media_start",
            "elder_id": str(elder_id),
            "media_id": str(media["media_id"]),
            "history_id": str(history.history_id),
        },
    )
    return history.history_id


async def record_end(
    db: AsyncSession,
    elder_id: UUID,
    history_id: UUID | str,
    *,
    completed: bool,
    elapsed_seconds: float | None = None,
) -> bool:
    """补结束时间与完成度；history 必须属于该 elder，否则返回 False。"""
    try:
        target = UUID(str(history_id))
    except (TypeError, ValueError):
        return False

    history = (
        await db.execute(
            select(MediaHistory).where(
                MediaHistory.history_id == target,
                MediaHistory.elder_id == elder_id,
            )
        )
    ).scalars().first()
    if history is None:
        logger.warning(
            "媒体历史不存在或越权",
            extra={
                "event": "media_history_forbidden",
                "elder_id": str(elder_id),
                "history_id": str(target),
            },
        )
        return False

    history.finished_at = datetime.now(UTC)
    history.completion_rate = await _completion_rate(
        db, history.media_id, completed=completed, elapsed_seconds=elapsed_seconds
    )
    await db.flush()
    logger.info(
        "媒体播放结束",
        extra={
            "event": "media_end",
            "elder_id": str(elder_id),
            "history_id": str(target),
            "completed": completed,
        },
    )
    return True


async def _completion_rate(
    db: AsyncSession,
    media_id: UUID,
    *,
    completed: bool,
    elapsed_seconds: float | None,
) -> Decimal | None:
    if completed:
        return Decimal("1.000")
    if elapsed_seconds is None or elapsed_seconds < 0:
        return None
    duration = (
        await db.execute(
            select(MediaLibrary.duration_seconds).where(MediaLibrary.media_id == media_id)
        )
    ).scalar_one_or_none()
    if not duration:
        return None
    ratio = min(1.0, max(0.0, float(elapsed_seconds) / float(duration)))
    return Decimal(str(round(ratio, 3)))


__all__ = [
    "DEFAULT_GENRE",
    "INTERNAL_PREFIX",
    "PLAYABLE_COPYRIGHT",
    "next_for",
    "pick_for_play",
    "record_end",
    "record_start",
]
