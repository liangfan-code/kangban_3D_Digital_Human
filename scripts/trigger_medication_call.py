"""手动触发一次用药外呼（Day7 段4 联调/验收用）。

用法::

    python scripts/trigger_medication_call.py --order-id <medication_order_id>
    python scripts/trigger_medication_call.py --elder 测试长者甲
    python scripts/trigger_medication_call.py --plan-id <plan_id>

行为：
- 缺订单时先走段1 ``ensure_order``（额外幂等层），commit 后再外呼；
- 外呼优先服务端 WebRTC（下行 TTS 音轨），失败/无 WS 时降级为 WS TTS；
- 只打印元数据（设备、通道、耗时、图状态），不打印长者原文或音频。

前置：`.env` 已配置数据库；平板页 `/static/elder.html` 的语音通道已连接（否则降级）。
"""

import argparse
import asyncio
import sys
import uuid

from sqlalchemy import or_, select

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import get_engine, get_session_factory
from app.models import Elder, MedicationOrder, MedicationPlan
from app.models.enums import MedicationEventSource, MedicationPlanStatus
from app.services.medication_order import ensure_order
from app.services.medication_plan import pick_current_slot, today_plans
from app.services.outbound import OutboundError, trigger_medication_call


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="手动触发一次用药外呼")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--order-id", help="已有服药任务单 ID（直接外呼）")
    group.add_argument("--plan-id", help="用药计划 ID（按当前时段建单后外呼）")
    group.add_argument("--elder", help="长者姓名或 elder_id（取其当前时段）")
    return parser.parse_args()


async def _find_elder(session, value: str) -> Elder | None:
    conditions = [Elder.name == value]
    try:
        conditions.append(Elder.elder_id == uuid.UUID(value))
    except ValueError:
        pass
    return (await session.execute(select(Elder).where(or_(*conditions)))).scalars().first()


async def _resolve_order_id(args: argparse.Namespace) -> tuple[uuid.UUID | None, str | None]:
    """把 --order-id / --plan-id / --elder 统一解析为已 commit 的服药任务单。"""
    factory = get_session_factory()
    if args.order_id:
        try:
            return uuid.UUID(args.order_id), None
        except ValueError:
            return None, f"order_id 不是合法 UUID：{args.order_id}"

    async with factory() as session:
        if args.plan_id:
            try:
                plan_id = uuid.UUID(args.plan_id)
            except ValueError:
                return None, f"plan_id 不是合法 UUID：{args.plan_id}"
            plan = await session.get(MedicationPlan, plan_id)
        else:
            elder = await _find_elder(session, args.elder)
            if elder is None:
                return None, f"未找到长者：{args.elder}"
            plan = (
                await session.execute(
                    select(MedicationPlan)
                    .where(
                        MedicationPlan.elder_id == elder.elder_id,
                        MedicationPlan.status == MedicationPlanStatus.ACTIVE,
                    )
                    .order_by(MedicationPlan.created_at.desc())
                )
            ).scalars().first()
        if plan is None:
            return None, "未找到生效中的用药计划"
        if plan.status != MedicationPlanStatus.ACTIVE:
            return None, f"用药计划不生效（status={plan.status}）"
        slots = [
            slot
            for slot in await today_plans(session, elder_id=plan.elder_id)
            if slot.plan.plan_id == plan.plan_id
        ]
        slot = pick_current_slot(slots)
        if slot is None:
            return None, "该计划今天没有可用时段（检查 schedule/start_date/end_date）"
        order, created = await ensure_order(
            session,
            plan=plan,
            scheduled_at=slot.scheduled_at,
            created_source=MedicationEventSource.SCHEDULER,
        )
        await session.commit()
        print(
            f"服药任务单：order_id={order.order_id} scheduled_at={slot.scheduled_at.isoformat()} "
            f"{'（新建）' if created else '（复用）'}"
        )
        return order.order_id, None


async def trigger(args: argparse.Namespace) -> int:
    setup_logging()
    settings = get_settings()
    engine = get_engine()
    try:
        order_id, error = await _resolve_order_id(args)
        if order_id is None:
            print(f"无法外呼：{error}")
            return 1
        try:
            result = await trigger_medication_call(
                order_id, checkpoint_path=settings.medication_checkpoint_path
            )
        except OutboundError as exc:
            print(f"外呼失败：{exc}")
            return 1
        async with get_session_factory()() as session:
            order = await session.get(MedicationOrder, order_id)
            scheduled_at = order.scheduled_at.isoformat() if order is not None else "-"
        print("外呼完成：")
        print(f"  order_id={result.order_id} scheduled_at={scheduled_at}")
        print(f"  elder_id={result.elder_id} device_id={result.device_id or '-'}")
        print(f"  channel={result.channel} degraded={result.degraded or '-'}")
        print(
            f"  attempt={result.attempt} order_status={result.order_status} "
            f"graph_status={result.graph_status or '-'} duration_ms={result.duration_ms}"
        )
        return 0
    finally:
        await engine.dispose()


def main() -> None:
    try:
        sys.exit(asyncio.run(trigger(_parse_args())))
    except KeyboardInterrupt:
        print("\n已取消。")
        sys.exit(130)


if __name__ == "__main__":
    main()
