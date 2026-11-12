"""Day7 段6 媒体服务测试：授权过滤、品类/偏好、换段循环与历史归属。"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select

from app.models import MediaHistory, MediaLibrary
from app.models.enums import MediaCopyrightStatus, MediaStatus, MediaType
from app.services import media as media_service


async def _add_media(
    db_session,
    *,
    title: str,
    genre: str = "越剧",
    copyright_status: str = MediaCopyrightStatus.LICENSED,
    status: str = MediaStatus.ACTIVE,
    uri: str | None = None,
    duration: int | None = 60,
    media_type: str = MediaType.OPERA,
) -> MediaLibrary:
    row = MediaLibrary(
        title=title,
        type=media_type,
        genre=genre,
        artist="测试",
        media_uri=uri if uri is not None else f"/static/media/library/{uuid.uuid4().hex}.wav",
        duration_seconds=duration,
        copyright_status=copyright_status,
        status=status,
    )
    db_session.add(row)
    await db_session.flush()
    return row


async def test_pick_excludes_hidden_and_external(db_session, seed):
    elder_id = seed["elders"]["single"].elder_id
    await _add_media(
        db_session,
        title="外链条目",
        status=MediaStatus.ACTIVE,
        uri="https://example.com/opera.mp3",
    )
    await _add_media(
        db_session,
        title="未授权隐藏",
        status=MediaStatus.HIDDEN,
        copyright_status=MediaCopyrightStatus.UNKNOWN,
    )

    assert await media_service.pick_for_play(db_session, elder_id, "越剧") is None


async def test_pick_prefers_requested_genre(db_session, seed):
    elder_id = seed["elders"]["single"].elder_id
    await _add_media(db_session, title="越剧甲", genre="越剧")
    await _add_media(db_session, title="京剧乙", genre="京剧")

    picked = await media_service.pick_for_play(db_session, elder_id, "京剧")

    assert picked is not None
    assert picked["genre"] == "京剧"
    assert picked["url"].startswith(media_service.INTERNAL_PREFIX)


async def test_pick_falls_back_when_genre_empty(db_session, seed):
    elder_id = seed["elders"]["single"].elder_id
    await _add_media(db_session, title="越剧甲", genre="越剧")

    picked = await media_service.pick_for_play(db_session, elder_id, "昆曲")

    assert picked is not None
    assert picked["title"] == "越剧甲"


async def test_pick_uses_memory_preference(db_session, seed, monkeypatch):
    elder_id = seed["elders"]["single"].elder_id
    await _add_media(db_session, title="越剧甲", genre="越剧")
    await _add_media(db_session, title="黄梅乙", genre="黄梅戏")

    async def _preference(_db, _elder_id):  # noqa: ARG001
        return "黄梅戏"

    monkeypatch.setattr(media_service, "_preferred_genre", _preference)

    picked = await media_service.pick_for_play(db_session, elder_id)

    assert picked is not None
    assert picked["genre"] == "黄梅戏"


async def test_recently_played_is_ranked_last(db_session, seed):
    elder_id = seed["elders"]["single"].elder_id
    first = await _add_media(db_session, title="甲", genre="越剧")
    await _add_media(db_session, title="乙", genre="越剧")

    await media_service.record_start(
        db_session, elder_id, {"media_id": str(first.media_id)}
    )
    picked = await media_service.pick_for_play(db_session, elder_id, "越剧")

    assert picked is not None
    assert picked["title"] == "乙"


async def test_next_cycles_within_genre(db_session, seed):
    elder_id = seed["elders"]["single"].elder_id
    first = await _add_media(db_session, title="甲", genre="越剧")
    second = await _add_media(db_session, title="乙", genre="越剧")

    nxt = await media_service.next_for(
        db_session,
        elder_id,
        {"media_id": str(first.media_id), "genre": "越剧"},
    )
    assert nxt is not None and nxt["title"] == "乙"

    back = await media_service.next_for(
        db_session,
        elder_id,
        {"media_id": str(second.media_id), "genre": "越剧"},
    )
    assert back is not None and back["title"] == "甲"


async def test_next_falls_back_to_other_genre(db_session, seed):
    elder_id = seed["elders"]["single"].elder_id
    only = await _add_media(db_session, title="越剧独苗", genre="越剧")
    await _add_media(db_session, title="京剧乙", genre="京剧")

    nxt = await media_service.next_for(
        db_session,
        elder_id,
        {"media_id": str(only.media_id), "genre": "越剧"},
    )

    assert nxt is not None
    assert nxt["title"] == "京剧乙"


async def test_record_start_creates_history(db_session, seed):
    elder_id = seed["elders"]["single"].elder_id
    media = await _add_media(db_session, title="甲", genre="越剧")

    history_id = await media_service.record_start(
        db_session, elder_id, {"media_id": str(media.media_id)}
    )

    row = (
        await db_session.execute(
            select(MediaHistory).where(MediaHistory.history_id == history_id)
        )
    ).scalars().first()
    assert row is not None
    assert row.elder_id == elder_id
    assert row.media_id == media.media_id
    assert row.finished_at is None


async def test_record_end_completed_sets_full_rate(db_session, seed):
    elder_id = seed["elders"]["single"].elder_id
    media = await _add_media(db_session, title="甲", genre="越剧", duration=60)
    history_id = await media_service.record_start(
        db_session, elder_id, {"media_id": str(media.media_id)}
    )

    ok = await media_service.record_end(
        db_session, elder_id, history_id, completed=True
    )

    row = await db_session.get(MediaHistory, history_id)
    assert ok is True
    assert row is not None and row.finished_at is not None
    assert row.completion_rate == Decimal("1.000")


async def test_record_end_estimates_partial_rate(db_session, seed):
    elder_id = seed["elders"]["single"].elder_id
    media = await _add_media(db_session, title="甲", genre="越剧", duration=10)
    history_id = await media_service.record_start(
        db_session, elder_id, {"media_id": str(media.media_id)}
    )

    await media_service.record_end(
        db_session, elder_id, history_id, completed=False, elapsed_seconds=5
    )

    row = await db_session.get(MediaHistory, history_id)
    assert row is not None
    assert row.completion_rate == Decimal("0.500")


async def test_record_end_without_duration_leaves_rate_empty(db_session, seed):
    elder_id = seed["elders"]["single"].elder_id
    media = await _add_media(db_session, title="甲", genre="越剧", duration=None)
    history_id = await media_service.record_start(
        db_session, elder_id, {"media_id": str(media.media_id)}
    )

    await media_service.record_end(
        db_session, elder_id, history_id, completed=False, elapsed_seconds=5
    )

    row = await db_session.get(MediaHistory, history_id)
    assert row is not None
    assert row.completion_rate is None


async def test_record_end_rejects_foreign_history(db_session, seed):
    owner = seed["elders"]["d1"].elder_id
    other = seed["elders"]["d2"].elder_id
    media = await _add_media(db_session, title="甲", genre="越剧")
    history_id = await media_service.record_start(
        db_session, owner, {"media_id": str(media.media_id)}
    )

    ok = await media_service.record_end(
        db_session, other, history_id, completed=True
    )

    assert ok is False
    row = await db_session.get(MediaHistory, history_id)
    assert row is not None
    assert row.finished_at is None


async def test_no_playable_media_returns_none(db_session, seed):
    elder_id = seed["elders"]["single"].elder_id
    await _add_media(db_session, title="隐藏", status=MediaStatus.HIDDEN)

    assert await media_service.pick_for_play(db_session, elder_id) is None
    assert await media_service.next_for(db_session, elder_id, None) is None


async def test_history_rows_are_scoped_by_elder(db_session, seed):
    owner = seed["elders"]["d1"].elder_id
    other = seed["elders"]["d2"].elder_id
    media = await _add_media(db_session, title="甲", genre="越剧")
    await media_service.record_start(
        db_session, owner, {"media_id": str(media.media_id)}
    )

    rows = (
        await db_session.execute(
            select(MediaHistory).where(MediaHistory.elder_id == other)
        )
    ).scalars().all()

    assert rows == []
