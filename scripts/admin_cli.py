"""康伴管理命令行工具（仅管理员）。

启动先验管理员身份（与 /auth/login 同一套 bcrypt 校验），通过后进入中文菜单，
对 房间/人设/设备/账号/长者/授权关联 执行增删改查。

约定：
- 所有写入复用 service 层函数（与 API 同一套校验），不直写裸 SQL；
- 建号与重置密码经 hash_password（bcrypt），密码输入不回显、不落日志；
- 退出时打印本次操作摘要。

用法：./.venv/Scripts/python.exe scripts/admin_cli.py
"""

import asyncio
import getpass
import sys
import uuid
from datetime import datetime

from sqlalchemy import select

from app.auth.dependencies import UserIdentity
from app.db.session import dispose_engine, get_session_factory
from app.models import Account, Device, Elder, Persona, Room
from app.models.enums import AccountRole
from app.schemas.account_admin import AccountCreate
from app.schemas.assignment import (
    BuildingAssignmentCreate,
    DoctorAssignmentCreate,
    FamilyLinkCreate,
)
from app.schemas.device import BindingUpdate, DeviceCreate
from app.schemas.elder import ElderCreate, ElderUpdate
from app.schemas.persona import PersonaCreate
from app.schemas.room import RoomCreate, RoomUpdate
from app.services import account as account_service
from app.services import device as device_service
from app.services import elder as elder_service
from app.services import persona as persona_service
from app.services import room as room_service
from app.services.authorization import (
    AssignmentConflict,
    AssignmentValidationError,
    ObjectInactive,
    ObjectNotFound,
    create_building_assignment,
    create_doctor_assignment,
    create_family_link,
    suspend_assignment,
)

OPERATIONS: list[str] = []  # 操作摘要（退出时打印）


def _note(text: str) -> None:
    OPERATIONS.append(f"[{datetime.now().strftime('%H:%M:%S')}] {text}")
    print(f"  ✔ {text}")


def _fail(exc: Exception) -> None:
    print(f"  ✘ 操作失败：{exc}")


def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f"（默认 {default}）" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or (default or "")


def _ask_password(prompt: str) -> str:
    """密码输入不回显；非交互环境（stdin 为管道）降级为 input，无终端回显。"""
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    return input(prompt)


def _ask_uuid(prompt: str) -> uuid.UUID:
    raw = _ask(prompt)
    return uuid.UUID(raw)


def _ask_int(prompt: str, default: int | None = None) -> int:
    raw = _ask(prompt, str(default) if default is not None else None)
    return int(raw)


def _ask_optional_int(prompt: str) -> int | None:
    raw = _ask(prompt).strip()
    return int(raw) if raw else None


def _pick(session, rows: list, label: str, attr: str) -> uuid.UUID:
    """列出候选并按编号选择；也可直接粘贴 UUID。"""
    print(f"可选{label}：")
    for i, row in enumerate(rows, start=1):
        desc = getattr(row, attr, "")
        if hasattr(row, "building_code"):
            desc = f"{row.building_code}-{row.floor}-{row.room_number}"
        extra = getattr(row, "status", None) or getattr(row, "is_active", None)
        print(f"  {i}. {desc}  [{extra}]")
    raw = _ask(f"输入{label}编号或 UUID")
    if raw.isdigit() and 1 <= int(raw) <= len(rows):
        return getattr(rows[int(raw) - 1], _pk(rows[0]))
    return uuid.UUID(raw)


def _pk(row) -> str:
    for name in ("room_id", "persona_id", "device_id", "account_id", "elder_id"):
        if hasattr(row, name):
            return name
    raise ValueError("未知主键")


# ---- 登录 ----


async def login_admin(session) -> UserIdentity | None:
    print("== 康伴管理工具（需管理员身份） ==")
    username = _ask("管理员用户名")
    password = _ask_password("密码（不回显）")
    try:
        account = await account_service.authenticate(session, username, password)
    except account_service.InvalidCredentials:
        return None
    if account.role != AccountRole.ADMIN:
        print("  ✘ 该账号不是管理员，拒绝进入")
        return None
    return UserIdentity(
        account_id=account.account_id, role=account.role, display_name=account.display_name
    )


# ---- 房间 ----


async def menu_rooms(session, actor: UserIdentity) -> None:
    while True:
        print("\n--- 房间管理 ---")
        print(" 1.新建房间  2.房间列表  3.修改房间  4.停用房间  0.返回")
        choice = _ask("选择").strip()
        try:
            if choice == "1":
                room = await room_service.create_room(
                    session,
                    RoomCreate(
                        building_code=_ask("楼栋编码"),
                        floor=_ask_int("楼层"),
                        room_number=_ask("房号"),
                        bed_count=_ask_int("床位数(1-2)"),
                    ),
                    actor,
                )
                await session.commit()
                _note(f"建房间的 {room.building_code}-{room.floor}-{room.room_number}")
            elif choice == "2":
                rows = (await session.execute(select(Room))).scalars().all()
                for r in rows:
                    print(
                        f"  {str(r.room_id)[:8]}  {r.building_code}-{r.floor}-"
                        f"{r.room_number}  {r.bed_count}床  {r.status}"
                    )
            elif choice == "3":
                rooms = (await session.execute(select(Room))).scalars().all()
                room_id = _pick(session, list(rooms), "房间", "room_number")
                room = await room_service.get_room_or_404(session, room_id)
                await room_service.update_room(
                    session,
                    room,
                    RoomUpdate(
                        bed_count=_ask_optional_int("新床位数(留空不改)"),
                        status=_ask("新状态(active/inactive，留空不改)") or None,
                    ),
                    actor,
                )
                await session.commit()
                _note(f"更新房间 {room.room_number}")
            elif choice == "4":
                rooms = (await session.execute(select(Room))).scalars().all()
                room_id = _pick(session, list(rooms), "房间", "room_number")
                room = await room_service.get_room_or_404(session, room_id)
                await room_service.deactivate_room(session, room, actor)
                await session.commit()
                _note(f"停用房间 {room.room_number}")
            elif choice == "0":
                return
        except (
            room_service.RoomValidationError,
            room_service.RoomConflict,
            room_service.RoomOccupied,
            room_service.RoomNotFound,
            ValueError,
        ) as exc:
            await session.rollback()
            _fail(exc)


# ---- 人设 ----


async def menu_personas(session, actor: UserIdentity) -> None:
    while True:
        print("\n--- 人设管理 ---")
        print(" 1.新人设  2.人设列表  3.停用人设  0.返回")
        choice = _ask("选择").strip()
        try:
            if choice == "1":
                persona = await persona_service.create_persona(
                    session,
                    PersonaCreate(
                        code=_ask("编码"),
                        name=_ask("名称"),
                        greeting=_ask("问候语(可空)") or None,
                    ),
                    actor,
                )
                await session.commit()
                _note(f"新人设 {persona.code}/{persona.name}")
            elif choice == "2":
                rows = (await session.execute(select(Persona))).scalars().all()
                for p in rows:
                    state = "启用" if p.is_active else "停用"
                    print(f"  {str(p.persona_id)[:8]}  {p.code}  {p.name}  {state}")
            elif choice == "3":
                rows = (await session.execute(select(Persona))).scalars().all()
                persona_id = _pick(session, list(rows), "人设", "name")
                persona = await persona_service.get_persona_or_404(session, persona_id)
                await persona_service.deactivate_persona(session, persona, actor)
                await session.commit()
                _note(f"停用人设 {persona.name}")
            elif choice == "0":
                return
        except (
            persona_service.PersonaConflict,
            persona_service.PersonaNotFound,
            ValueError,
        ) as exc:
            await session.rollback()
            _fail(exc)


# ---- 设备 ----


async def menu_devices(session, actor: UserIdentity) -> None:
    while True:
        print("\n--- 设备管理 ---")
        print(" 1.新建设备  2.设备列表  3.绑定/换绑房间  4.签发注册凭证  5.凭证列表")
        print(" 6.吊销全部凭证  7.退役设备  0.返回")
        choice = _ask("选择").strip()
        try:
            if choice == "1":
                rooms = (
                    (
                        await session.execute(
                            select(Room).order_by(
                                Room.building_code, Room.floor, Room.room_number
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                room_id = _pick(session, list(rooms), "房间", "room_number")
                device = await device_service.create_device(
                    session,
                    DeviceCreate(
                        device_code=_ask("设备编码"),
                        room_id=room_id,
                        bed_no=_ask_optional_int("床位号(可空)"),
                    ),
                    actor,
                )
                await session.commit()
                _note(f"新建设备 {device.device_code}（pending，待注册激活）")
            elif choice == "2":
                rows = (await session.execute(select(Device))).scalars().all()
                for d in rows:
                    print(
                        f"  {str(d.device_id)[:8]}  {d.device_code}  "
                        f"room={str(d.room_id)[:8]}  {d.status}"
                    )
            elif choice == "3":
                rows = (await session.execute(select(Device))).scalars().all()
                device_id = _pick(session, list(rows), "设备", "device_code")
                device = await device_service.get_device_or_404(session, device_id)
                rooms = (await session.execute(select(Room))).scalars().all()
                room_id = _pick(session, list(rooms), "房间", "room_number")
                await device_service.bind_device(
                    session,
                    device,
                    BindingUpdate(room_id=room_id, bed_no=_ask_optional_int("床位号(可空)")),
                    actor,
                )
                await session.commit()
                _note(f"设备 {device.device_code} 已换绑")
            elif choice == "4":
                rows = (await session.execute(select(Device))).scalars().all()
                device_id = _pick(session, list(rows), "设备", "device_code")
                issued = await device_service.issue_registration_token(session, device_id, actor)
                await session.commit()
                _note("已签发注册凭证（24h 内有效，仅此一次显示）")
                print(f"  注册凭证：{issued.plaintext}")
            elif choice == "5":
                rows = (await session.execute(select(Device))).scalars().all()
                device_id = _pick(session, list(rows), "设备", "device_code")
                for t in await device_service.list_device_tokens(session, device_id):
                    state = "已吊销" if t.revoked_at else "有效"
                    print(
                        f"  {str(t.token_id)[:8]}  {t.token_kind}  "
                        f"{t.issued_at:%m-%d %H:%M}  {state}"
                    )
            elif choice == "6":
                rows = (await session.execute(select(Device))).scalars().all()
                device_id = _pick(session, list(rows), "设备", "device_code")
                count = await device_service.revoke_all_device_tokens(session, device_id, actor)
                await session.commit()
                _note(f"吊销设备全部凭证 {count} 条")
            elif choice == "7":
                rows = (await session.execute(select(Device))).scalars().all()
                device_id = _pick(session, list(rows), "设备", "device_code")
                device = await device_service.get_device_or_404(session, device_id)
                await device_service.deactivate_device(session, device, actor)
                await session.commit()
                _note(f"设备 {device.device_code} 已退役并吊销凭证")
            elif choice == "0":
                return
        except (
            device_service.DeviceValidationError,
            device_service.DeviceConflict,
            device_service.DeviceNotFound,
            device_service.DeviceCredentialError,
            ValueError,
        ) as exc:
            await session.rollback()
            _fail(exc)


# ---- 账号 ----


async def menu_accounts(session, actor: UserIdentity) -> None:
    while True:
        print("\n--- 账号管理 ---")
        print(" 1.建号  2.账号列表  3.重置密码  4.停用  5.启用  0.返回")
        choice = _ask("选择").strip()
        try:
            if choice == "1":
                print("  角色：admin/nurse/doctor/family")
                account = await account_service.create_account(
                    session,
                    AccountCreate(
                        username=_ask("用户名"),
                        display_name=_ask("显示名"),
                        role=AccountRole(_ask("角色")),
                        phone=_ask("手机号(可空)") or None,
                        password=_ask_password("初始密码（不回显）"),
                    ),
                    actor,
                )
                await session.commit()
                _note(f"建号 {account.username}（{account.role}）")
            elif choice == "2":
                rows = (await session.execute(select(Account))).scalars().all()
                for a in rows:
                    print(
                        f"  {str(a.account_id)[:8]}  {a.username:<14} {a.role:<7} "
                        f"{a.display_name}  {a.status}"
                    )
            elif choice == "3":
                rows = (await session.execute(select(Account))).scalars().all()
                account_id = _pick(session, list(rows), "账号", "username")
                account = await account_service.get_account_or_404(session, account_id)
                new_password = await account_service.reset_password(session, account, actor)
                await session.commit()
                _note(f"重置 {account.username} 的密码（请立即告知本人）")
                print(f"  新密码（仅此一次显示）：{new_password}")
            elif choice == "4":
                rows = (await session.execute(select(Account))).scalars().all()
                account_id = _pick(session, list(rows), "账号", "username")
                account = await account_service.get_account_or_404(session, account_id)
                await account_service.disable_account(session, account, actor)
                await session.commit()
                _note(f"停用账号 {account.username}")
            elif choice == "5":
                rows = (await session.execute(select(Account))).scalars().all()
                account_id = _pick(session, list(rows), "账号", "username")
                account = await account_service.get_account_or_404(session, account_id)
                await account_service.enable_account(session, account, actor)
                await session.commit()
                _note(f"启用账号 {account.username}")
            elif choice == "0":
                return
        except (
            account_service.AccountConflict,
            account_service.AccountNotFound,
            account_service.AccountStateConflict,
            account_service.AccountValidationError,
            ValueError,
        ) as exc:
            await session.rollback()
            _fail(exc)


# ---- 长者 ----


async def menu_elders(session, actor: UserIdentity) -> None:
    while True:
        print("\n--- 长者档案 ---")
        print(" 1.新建长者  2.长者列表  3.修改长者  4.退住(软删)  0.返回")
        choice = _ask("选择").strip()
        try:
            if choice == "1":
                personas = (await session.execute(select(Persona))).scalars().all()
                persona_id = _pick(session, list(personas), "人设", "name")
                name = _ask("姓名")
                room_id = None
                bed_no = None
                if _ask("是否分配床位(y/n)", "n").lower() == "y":
                    rooms = (await session.execute(select(Room))).scalars().all()
                    room_id = _pick(session, list(rooms), "房间", "room_number")
                    bed_no = _ask_int("床位号")
                elder = await elder_service.create_elder(
                    session,
                    ElderCreate(
                        name=name,
                        birth_date=_ask("出生日期(YYYY-MM-DD)"),
                        gender=_ask("性别(M/F/U)", "U"),
                        persona_id=persona_id,
                        room_id=room_id,
                        bed_no=bed_no,
                    ),
                    actor,
                )
                await session.commit()
                _note(f"新建长者 {elder.name}")
            elif choice == "2":
                for e in await elder_service.list_elders_for_user(session, actor):
                    room = f"{str(e.room_id)[:8]}床{e.bed_no}" if e.room_id else "未分床"
                    print(f"  {str(e.elder_id)[:8]}  {e.name}  {room}  {e.status}")
            elif choice == "3":
                rows = await elder_service.list_elders_for_user(session, actor)
                elder_id = _pick(session, list(rows), "长者", "name")
                elder = (
                    await session.execute(select(Elder).where(Elder.elder_id == elder_id))
                ).scalar_one()
                elder_updates: dict = {}
                if new_name := _ask("新姓名(留空不改)"):
                    elder_updates["name"] = new_name
                if elder_updates:
                    await elder_service.update_elder(
                        session, elder, ElderUpdate(**elder_updates), actor
                    )
                await session.commit()
                _note(f"更新长者 {elder.name}")
            elif choice == "4":
                rows = await elder_service.list_elders_for_user(session, actor)
                elder_id = _pick(session, list(rows), "长者", "name")
                elder = (
                    await session.execute(select(Elder).where(Elder.elder_id == elder_id))
                ).scalar_one()
                await elder_service.deactivate_elder(session, elder, actor)
                await session.commit()
                _note(f"长者 {elder.name} 已退住（discharged，床位释放）")
            elif choice == "0":
                return
        except (
            elder_service.ElderValidationError,
            elder_service.ElderConflict,
            elder_service.ElderNotFound,
            ValueError,
        ) as exc:
            await session.rollback()
            _fail(exc)


# ---- 授权关联 ----


async def menu_assignments(session, actor: UserIdentity) -> None:
    while True:
        print("\n--- 授权关联 ---")
        print(" 1.护工-楼栋  2.医生-长者  3.家属-长者  4.吊销关联  0.返回")
        choice = _ask("选择").strip()
        try:
            if choice in ("1", "2", "3"):
                accounts = (
                    (
                        await session.execute(
                            select(Account).order_by(Account.created_at)
                        )
                    )
                    .scalars()
                    .all()
                )
                role = {"1": "nurse", "2": "doctor", "3": "family"}[choice]
                candidates = [a for a in accounts if a.role == role]
                if not candidates:
                    print("  ✘ 没有该角色的账号")
                    continue
                account_id = _pick(session, candidates, "账号", "username")
                if choice == "1":
                    await create_building_assignment(
                        session,
                        BuildingAssignmentCreate(
                            account_id=account_id,
                            building_code=_ask("楼栋编码"),
                        ),
                    )
                    _note("护工已授权楼栋")
                else:
                    elders = await elder_service.list_elders_for_user(session, actor)
                    elder_id = _pick(session, list(elders), "长者", "name")
                    raw_until = _ask("有效期至(YYYY-MM-DD，可空)") or None
                    valid_until = (
                        datetime.fromisoformat(raw_until).replace(tzinfo=None)
                        if raw_until
                        else None
                    )
                    if choice == "2":
                        await create_doctor_assignment(
                            session,
                            DoctorAssignmentCreate(
                                account_id=account_id, elder_id=elder_id, valid_until=valid_until
                            ),
                        )
                        _note("医生-长者关联已建立")
                    else:
                        await create_family_link(
                            session,
                            FamilyLinkCreate(
                                account_id=account_id,
                                elder_id=elder_id,
                                relation=_ask("关系(如 女儿)"),
                                valid_until=valid_until,
                            ),
                        )
                        _note("家属-长者关联已建立")
                await session.commit()
            elif choice == "4":
                kind = _ask("类型 building/doctor/family")
                object_id = _ask_uuid("关联 ID")
                if not await suspend_assignment(session, kind, object_id):
                    raise ObjectNotFound("关联不存在")
                await session.commit()
                _note(f"关联 {kind}/{str(object_id)[:8]} 已吊销")
            elif choice == "0":
                return
        except (
            ObjectNotFound,
            ObjectInactive,
            AssignmentValidationError,
            AssignmentConflict,
            ValueError,
        ) as exc:
            await session.rollback()
            _fail(exc)


MENU = {
    "1": ("房间管理", menu_rooms),
    "2": ("人设管理", menu_personas),
    "3": ("设备管理", menu_devices),
    "4": ("账号管理", menu_accounts),
    "5": ("长者档案", menu_elders),
    "6": ("授权关联", menu_assignments),
}


async def main() -> int:
    session_factory = get_session_factory()
    async with session_factory() as session:
        actor = await login_admin(session)
        if actor is None:
            print("身份验证失败，退出。")
            return 1
        print(f"欢迎，{actor.display_name}。输入 0 返回上级，菜单内循环操作。\n")
        try:
            while True:
                print("=" * 46)
                print(" 康伴管理台：1房间 2人设 3设备 4账号 5长者 6关联 0退出")
                choice = _ask("选择").strip()
                if choice == "0":
                    break
                if choice in MENU:
                    await MENU[choice][1](session, actor)
        finally:
            print("\n== 本次操作摘要 ==")
            if OPERATIONS:
                for line in OPERATIONS:
                    print(f"  {line}")
            else:
                print("  （无写操作）")
            print(f"共 {len(OPERATIONS)} 项操作。再见！")
            await dispose_engine()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
