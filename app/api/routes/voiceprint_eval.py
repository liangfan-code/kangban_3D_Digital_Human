"""声纹阈值评测报告（只读，仅管理员，Day8 段4）。

报告由 ``scripts/voiceprint_eval.py`` 离线产出到 ``voiceprint_eval_report_dir``，
本接口只读取最新一份，不触发任何模型加载或评测。
"""

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth.dependencies import UserIdentity, require_admin
from app.core.config import Settings, get_settings

router = APIRouter(tags=["voiceprint-eval"])

REPORT_PREFIX = "voiceprint_eval_"


def _latest_report(directory: str) -> Path | None:
    base = Path(directory)
    if not base.is_dir():
        return None
    files = sorted(base.glob(f"{REPORT_PREFIX}*.json"), key=lambda p: p.name, reverse=True)
    return files[0] if files else None


@router.get("/admin/voiceprint-eval/latest")
async def get_latest_voiceprint_eval(
    _admin: UserIdentity = Depends(require_admin),
    settings: Settings = Depends(get_settings),
) -> dict:
    """返回最新评测报告；尚未评测时 404（前端显示「尚未评测」）。"""
    path = _latest_report(settings.voiceprint_eval_report_dir)
    if path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="尚无声纹评测报告，请先运行 scripts/voiceprint_eval.py",
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="评测报告损坏，请重新运行评测脚本",
        ) from exc
