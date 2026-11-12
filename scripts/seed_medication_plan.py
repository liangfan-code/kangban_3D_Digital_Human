"""写入一条教学用用药计划（Day7 段3/4 测试种子，幂等）。

用法::

    python scripts/seed_medication_plan.py --elder 测试长者甲
    python scripts/seed_medication_plan.py --elder <elder_id> --drug 降压药 --time 08:00

说明：
- 仅用于段3/4 的测试与演示，直接写 ``status=active`` 的计划；
- 不生成长期工单、不替代段1「医生创建 → 确认」流程（演示医生端必须走段1/2）；
- 计划只挂在明确虚构的测试长者上，不写真实姓名/房间号/病史；
- 重复执行按「长者 + 药名 + 时段」判重并提示已存在。
"""

import argparse
import asyncio
import sys
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select

from app.core.config import get_settings
from app.db.session import get_engine, get_session_factory
from app.models import Elder, MedicationPlan
from app.models.enums import MedicationPlanStatus

SEED_NOTE = "测试种子"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="写入教学用用药计划（幂等）")
    parser.add_argument("--elder", required=True, help="长者姓名或 elder_id")
    parser.add_argument("--drug", default="降压药", help="药名（默认：降压药）")
    parser.add_argument("--dose", default="1 片", help="剂量（默认：1 片）")
    parser.add_argument("--time", default="08:00", help="每日用药时间 HH:MM（默认：08:00）")
    return parser.parse_args()


async def _find_elder(session, value: str) -> Elder | None:
    conditions = [Elder.name == value]
    try:
        conditions.append(Elder.elder_id == uuid.UUID(value))
    except ValueError:
        pass
    return (await session.execute(select(Elder).where(or_(*conditions)))).scalars().first()


async def seed_medication_plan(args: argparse.Namespace) -> int:
    settings = get_settings()
    today = datetime.now(ZoneInfo(settings.app_timezone)).date()
    engine = get_engine()
    factory = get_session_factory()
    try:
        async with factory() as session:
            elder = await _find_elder(session, args.elder)
            if elder is None:
                print(f"未找到长者：{args.elder}（请先创建测试长者）")
                return 1

            schedule = {"times": [args.time], "weekdays": []}
            existing_plans = (
                await session.execute(
                    select(MedicationPlan).where(
                        MedicationPlan.elder_id == elder.elder_id,
                        MedicationPlan.drug_name == args.drug,
                        MedicationPlan.status == MedicationPlanStatus.ACTIVE,
                    )
                )
            ).scalars().all()
            existing = next(
                (
                    plan
                    for plan in existing_plans
                    if isinstance(plan.schedule, dict)
                    and plan.schedule.get("times") == [args.time]
                ),
                None,
            )
            if existing is not None:
                print(
                    f"已存在相同计划，跳过创建（幂等）：plan_id={existing.plan_id}"
                )
                return 0

            plan = MedicationPlan(
                elder_id=elder.elder_id,
                drug_name=args.drug,
                dose=args.dose,
                schedule=schedule,
                instructions=None,
                start_date=today,
                end_date=None,
                status=MedicationPlanStatus.ACTIVE,
                created_by_account_id=None,
                updated_by_account_id=None,
                internal_note=SEED_NOTE,
            )
            session.add(plan)
            await session.commit()
            print(
                "已写入测试用药计划："
                f"elder={elder.name} plan_id={plan.plan_id} "
                f"{args.drug} {args.dose} 每日 {args.time}（{SEED_NOTE}）"
            )
            return 0
    finally:
        await engine.dispose()


def main() -> None:
    try:
        sys.exit(asyncio.run(seed_medication_plan(_parse_args())))
    except KeyboardInterrupt:
        print("\n已取消。")
        sys.exit(130)


if __name__ == "__main__":
    main()
