"""账号查询、登录校验与管理（建号/重置密码/停用启用，仅 admin 调用）。

密码明文与哈希均不写日志；重置密码的新明文仅在响应中一次性返回。
"""

import secrets

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import UserIdentity
from app.auth.passwords import hash_password, verify_password
from app.core.logging import get_logger
from app.models import Account
from app.models.enums import AccountRole, AccountStatus
from app.schemas.account_admin import AccountCreate, AccountUpdate

logger = get_logger(__name__)


class InvalidCredentials(Exception):
    """用户名或密码错误 / 账号不可用；路由层统一映射 401，不区分具体原因。"""


class AccountValidationError(Exception):
    """账号管理校验失败；路由层映射 422。"""


class AccountConflict(Exception):
    """用户名/手机号已被占用；路由层映射 409。"""


class AccountNotFound(Exception):
    """账号不存在；路由层映射 404。"""


class AccountStateConflict(Exception):
    """状态操作冲突（如停用最后一个 active 管理员）；路由层映射 409。"""


def generate_random_password(length: int = 12) -> str:
    """生成随机密码（字母+数字，保证含大小写与数字），仅供重置密码一次性返回。"""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.isupper() for c in pwd)
            and any(c.islower() for c in pwd)
            and any(c.isdigit() for c in pwd)
        ):
            return pwd


async def authenticate(session: AsyncSession, username: str, password: str) -> Account:
    """按用户名或手机号查找账号并校验密码；失败一律抛 InvalidCredentials。"""
    account = (
        await session.execute(
            select(Account).where(
                or_(Account.username == username, Account.phone == username)
            )
        )
    ).scalar_one_or_none()

    if (
        account is None
        or account.status != AccountStatus.ACTIVE
        or not verify_password(password, account.password_hash)
    ):
        logger.warning(
            "login failed",
            extra={"event": "login_failed"},
        )
        raise InvalidCredentials("invalid credentials")

    logger.info(
        "login succeeded",
        extra={
            "event": "login_succeeded",
            "actor_id": str(account.account_id),
            "actor_role": account.role,
        },
    )
    return account


# ---- 管理（仅 admin） ----


async def _ensure_username_free(
    session: AsyncSession, username: str, exclude_account_id=None
) -> None:
    stmt = select(Account).where(Account.username == username)
    if exclude_account_id is not None:
        stmt = stmt.where(Account.account_id != exclude_account_id)
    if (await session.execute(stmt)).scalar_one_or_none() is not None:
        raise AccountConflict("用户名已存在")


async def _ensure_phone_free(
    session: AsyncSession, phone: str, exclude_account_id=None
) -> None:
    stmt = select(Account).where(Account.phone == phone)
    if exclude_account_id is not None:
        stmt = stmt.where(Account.account_id != exclude_account_id)
    if (await session.execute(stmt)).scalar_one_or_none() is not None:
        raise AccountConflict("手机号已被占用")


async def create_account(
    session: AsyncSession, data: AccountCreate, actor: UserIdentity
) -> Account:
    await _ensure_username_free(session, data.username)
    if data.phone is not None:
        await _ensure_phone_free(session, data.phone)
    account = Account(
        username=data.username,
        display_name=data.display_name,
        role=data.role,
        phone=data.phone,
        password_hash=hash_password(data.password),
        status=AccountStatus.ACTIVE,
    )
    session.add(account)
    await session.flush()
    logger.info(
        "account created",
        extra={
            "event": "account_created",
            "actor_id": str(actor.account_id),
            "object_type": "account",
            "object_id": str(account.account_id),
            "target_role": account.role,
            "result": "success",
        },
    )
    return account


async def update_account(
    session: AsyncSession, account: Account, data: AccountUpdate, actor: UserIdentity
) -> Account:
    fields = data.model_dump(exclude_unset=True)
    if "username" in fields and fields["username"] != account.username:
        await _ensure_username_free(session, fields["username"], account.account_id)
    if "phone" in fields and fields["phone"] is not None and fields["phone"] != account.phone:
        await _ensure_phone_free(session, fields["phone"], account.account_id)
    for key, value in fields.items():
        setattr(account, key, value)
    await session.flush()
    await session.refresh(account)
    logger.info(
        "account updated",
        extra={
            "event": "account_updated",
            "actor_id": str(actor.account_id),
            "object_type": "account",
            "object_id": str(account.account_id),
            "result": "success",
        },
    )
    return account


async def reset_password(session: AsyncSession, account: Account, actor: UserIdentity) -> str:
    """重置密码：后端生成随机明文（仅此一次返回），库内只存哈希。"""
    new_password = generate_random_password()
    account.password_hash = hash_password(new_password)
    await session.flush()
    logger.info(
        "account password reset",
        extra={
            "event": "account_password_reset",
            "actor_id": str(actor.account_id),
            "object_type": "account",
            "object_id": str(account.account_id),
            "result": "success",
        },
    )
    return new_password


async def _active_admin_count(session: AsyncSession) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(Account).where(
                Account.role == AccountRole.ADMIN, Account.status == AccountStatus.ACTIVE
            )
        )
    ).scalar_one()


async def disable_account(session: AsyncSession, account: Account, actor: UserIdentity) -> None:
    """停用账号（幂等）；停用最后一个 active 管理员拒绝。"""
    if account.status == AccountStatus.DISABLED:
        return
    if account.role == AccountRole.ADMIN and await _active_admin_count(session) <= 1:
        raise AccountStateConflict("不能停用最后一个可用的管理员账号")
    account.status = AccountStatus.DISABLED
    await session.flush()
    logger.info(
        "account disabled",
        extra={
            "event": "account_disabled",
            "actor_id": str(actor.account_id),
            "object_type": "account",
            "object_id": str(account.account_id),
            "result": "success",
        },
    )


async def enable_account(session: AsyncSession, account: Account, actor: UserIdentity) -> None:
    """启用账号（幂等）。"""
    if account.status != AccountStatus.ACTIVE:
        account.status = AccountStatus.ACTIVE
        await session.flush()
        logger.info(
            "account enabled",
            extra={
                "event": "account_enabled",
                "actor_id": str(actor.account_id),
                "object_type": "account",
                "object_id": str(account.account_id),
                "result": "success",
            },
        )


async def get_account_or_404(session: AsyncSession, account_id) -> Account:
    account = (
        await session.execute(select(Account).where(Account.account_id == account_id))
    ).scalar_one_or_none()
    if account is None:
        raise AccountNotFound("账号不存在")
    return account
