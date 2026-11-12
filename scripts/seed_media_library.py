"""媒体库种子脚本（Day7 段6）：清单校验 + 本地占位音源。

口径：
- 只用教师授权的本地文件；不下载、不抓取任何网络资源；
- 清单里的 ``file`` 必须真实存在于 ``app/static/media/library/``，否则置 hidden
  并明确报错（最终非零退出）；
- ``--allow-placeholder`` 在本地生成 6–8 秒正弦音 WAV，``copyright_status=teaching``，
  标题标注「占位音源」，仅用于链路验收，不伪装成真实戏曲；
- 幂等：按 ``title`` 更新或插入，不写长者数据。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\seed_media_library.py
    .\\.venv\\Scripts\\python.exe scripts\\seed_media_library.py --allow-placeholder
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import wave
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.enums import MediaCopyrightStatus, MediaStatus, MediaType
from app.models.media import MediaLibrary

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIBRARY_DIR = PROJECT_ROOT / "app" / "static" / "media" / "library"
URI_PREFIX = "/static/media/library/"
PLACEHOLDER_GENRES = ("越剧", "京剧", "黄梅戏", "沪剧", "评弹", "昆曲", "老歌", "电影")
SAMPLE_RATE = 16_000


def _safe_filename(raw: str) -> str | None:
    """只接受目录内的纯文件名：拒绝 URL、绝对路径、目录穿越。"""
    name = str(raw or "").strip().replace("\\", "/")
    if not name or "://" in name or name.startswith("/") or ".." in name:
        return None
    if "/" in name:
        return None
    return name


def _load_manifest(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise ValueError("清单必须是数组，或包含 items 数组的对象")
    return [item for item in items if isinstance(item, dict)]


def _wav_duration(path: Path) -> int | None:
    try:
        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate() or SAMPLE_RATE
            return max(1, round(frames / rate))
    except (wave.Error, OSError):
        return None


def _write_placeholder(path: Path, seconds: int, frequency: float) -> None:
    """生成低音量正弦音占位文件（16k/16bit 单声道），不引入任何外部依赖。"""
    frames = bytearray()
    total = SAMPLE_RATE * seconds
    for index in range(total):
        value = int(0.05 * 32767 * math.sin(2 * math.pi * frequency * index / SAMPLE_RATE))
        frames += int(value).to_bytes(2, byteorder="little", signed=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(bytes(frames))


def _placeholder_items(count: int) -> tuple[list[dict], list[str]]:
    errors: list[str] = []
    items: list[dict] = []
    for index in range(count):
        genre = PLACEHOLDER_GENRES[index % len(PLACEHOLDER_GENRES)]
        seconds = 6 + index % 3  # 6–8 秒
        filename = f"placeholder-{index + 1:02d}.wav"
        target = LIBRARY_DIR / filename
        if not target.exists():
            _write_placeholder(target, seconds, 320.0 + index * 20)
        items.append(
            {
                "title": f"占位音源-{genre}-{index + 1:02d}",
                "type": MediaType.OPERA,
                "genre": genre,
                "artist": "本地占位",
                "era": None,
                "copyright_status": MediaCopyrightStatus.TEACHING,
                "file": filename,
                "duration_seconds": seconds,
            }
        )
    print(f"[placeholder] 已确保 {len(items)} 条占位音源位于 {LIBRARY_DIR}")
    return items, errors


def _normalize(item: dict) -> tuple[dict | None, str | None]:
    """校验并规整一条清单；返回 (record, error)。"""
    title = str(item.get("title") or "").strip()
    if not title:
        return None, "缺少 title"
    media_type = str(item.get("type") or MediaType.OPERA).strip()
    if media_type not in (MediaType.OPERA, MediaType.MOVIE):
        return None, f"type 必须是 opera/movie：{title}"
    copyright_status = str(item.get("copyright_status") or "").strip()
    if copyright_status not in (
        MediaCopyrightStatus.LICENSED,
        MediaCopyrightStatus.TEACHING,
        MediaCopyrightStatus.PUBLIC_DOMAIN,
    ):
        return None, f"copyright_status 不允许用于 active：{title}"
    filename = _safe_filename(item.get("file"))
    if filename is None:
        return None, f"file 必须是 app/static/media/library/ 下的文件名：{title}"
    return (
        {
            "title": title,
            "type": media_type,
            "genre": (str(item.get("genre") or "").strip() or None),
            "artist": (str(item.get("artist") or "").strip() or None),
            "era": (str(item.get("era") or "").strip() or None),
            "copyright_status": copyright_status,
            "file": filename,
            "duration_seconds": item.get("duration_seconds"),
        },
        None,
    )


async def _upsert(session, record: dict, *, active: bool, duration: int | None) -> str:
    row = (
        await session.execute(select(MediaLibrary).where(MediaLibrary.title == record["title"]))
    ).scalars().first()
    if row is None:
        row = MediaLibrary(title=record["title"])
        session.add(row)
    row.type = record["type"]
    row.genre = record["genre"]
    row.artist = record["artist"]
    row.era = record["era"]
    row.copyright_status = record["copyright_status"]
    row.duration_seconds = duration
    if active:
        row.media_uri = URI_PREFIX + record["file"]
        row.status = MediaStatus.ACTIVE
    else:
        row.media_uri = None
        row.status = MediaStatus.HIDDEN
    await session.flush()
    return row.status


async def _run(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = PROJECT_ROOT / manifest_path
    if not manifest_path.exists():
        print(f"[error] 清单不存在：{manifest_path}", file=sys.stderr)
        return 1

    try:
        raw_items = _load_manifest(manifest_path)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"[error] 清单解析失败：{exc}", file=sys.stderr)
        return 1

    errors: list[str] = []
    records: list[dict] = []
    for item in raw_items:
        record, error = _normalize(item)
        if error:
            errors.append(error)
            continue
        records.append(record)

    if args.allow_placeholder:
        placeholders, _ = _placeholder_items(args.placeholder_count)
        records.extend(placeholders)

    if not records:
        print("[warn] 清单为空：未写入任何媒体（可用 --allow-placeholder 生成占位音源）")
        return 1 if errors else 0

    from app.core.config import get_settings

    database_url = args.database_url or get_settings().database_url
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    active = hidden = 0
    try:
        async with factory() as session:
            for record in records:
                target = LIBRARY_DIR / record["file"]
                exists = target.is_file()
                duration = record.get("duration_seconds")
                if exists and not duration:
                    duration = _wav_duration(target)
                if not exists:
                    errors.append(f"文件缺失，已置 hidden：{record['title']} → {record['file']}")
                status = await _upsert(
                    session,
                    record,
                    active=exists,
                    duration=int(duration) if duration else None,
                )
                if status == MediaStatus.ACTIVE:
                    active += 1
                else:
                    hidden += 1
            await session.commit()
    finally:
        await engine.dispose()

    print(f"[done] active={active} hidden={hidden} errors={len(errors)}")
    for line in errors:
        print(f"[error] {line}", file=sys.stderr)
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="媒体库种子（清单校验 + 占位音源）")
    parser.add_argument(
        "--manifest",
        default="scripts/media_manifest.example.json",
        help="清单 JSON 路径（默认 scripts/media_manifest.example.json）",
    )
    parser.add_argument(
        "--allow-placeholder",
        action="store_true",
        help="本地生成 6–8 秒占位音源（teaching，仅链路验收）",
    )
    parser.add_argument(
        "--placeholder-count",
        type=int,
        default=24,
        help="占位音源条数（默认 24）",
    )
    parser.add_argument("--database-url", default=None, help="覆盖 DATABASE_URL")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
