"""长期记忆维护脚本（Day7 段5）。

用法：
    python scripts/memory_maintenance.py --dry-run            # 默认，不写库
    python scripts/memory_maintenance.py --apply              # 全量执行
    python scripts/memory_maintenance.py --elder-id <uuid>    # 只处理单个长者

输出 JSON 统计（扫描数 / 抑制数 / 合并数 / 是否 dry-run）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def _run(elder_id: uuid.UUID | None, dry_run: bool) -> dict:
    from app.db.session import get_session_factory
    from app.services.memory import maintenance

    factory = get_session_factory()
    async with factory() as db:
        stats = await maintenance(db, elder_id=elder_id, dry_run=dry_run)
        if dry_run:
            await db.rollback()
        else:
            await db.commit()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="长期记忆维护：过期抑制 / 重复归并 / 冷热分层")
    parser.add_argument("--elder-id", default=None, help="只处理指定长者 UUID（缺省=全部）")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写库（默认行为）")
    parser.add_argument("--apply", action="store_true", help="实际写库")
    args = parser.parse_args()
    if args.dry_run and args.apply:
        parser.error("--dry-run 与 --apply 不能同时使用")

    elder_id: uuid.UUID | None = None
    if args.elder_id:
        try:
            elder_id = uuid.UUID(args.elder_id)
        except ValueError:
            parser.error("--elder-id 必须是合法 UUID")

    dry_run = not args.apply
    stats = asyncio.run(_run(elder_id, dry_run))
    print(json.dumps(stats, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
