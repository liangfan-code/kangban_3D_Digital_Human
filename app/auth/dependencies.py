"""FastAPI 鉴权依赖：人身份与设备身份严格互斥。

- get_current_user：Bearer 人 JWT → 验签 → 查 accounts（存在且 active）→ 注入 UserIdentity。
- get_current_device：Bearer 设备 Token → 验签 + 查库吊销/过期（device_token.validate）→ 注入身份。
- require_role(*roles) / require_admin：角色门禁。

映射规则：签名非法/过期/凭证无效/账号非 active → 401；Token 类型与接口不符 → 403。
路由不得信任请求体或查询参数中的任何身份字段（TDD §1）。
"""

from dataclasses import dataclass
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.device_token import DeviceCredentialError, validate_device_token
from app.auth.jwt import USER_TOKEN_TYPE, TokenTypeMismatch, decode_token
from app.core.logging import get_logger
from app.db.session import get_session
from app.models import Account
from app.models.enums import AccountRole, AccountStatus

logger = get_logger(__name__)

bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class UserIdentity:
    """当前人账号身份（来自数据库，不信任客户端自报）。"""

    account_id: UUID
    role: str
    display_name: str


def _unauthorized(reason: str = "invalid") -> HTTPException:
    # 鉴权失败结构化告警（POC-05 审计留痕：不记 Token 内容，只记失败类型）
    logger.warning(
        "authentication failed",
        extra={"event": "auth_failed", "result": reason},
    )
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="认证失败",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _forbidden(reason: str = "denied") -> HTTPException:
    logger.warning(
        "authorization denied",
        extra={"event": "auth_forbidden", "result": reason},
    )
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权限访问该资源")


async def get_current_user(
    session: AsyncSession = Depends(get_session),
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> UserIdentity:
    """解析人 JWT 并注入账号身份；设备 Token 到达人接口返回 403。"""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized("missing_credentials")

    try:
        payload = decode_token(credentials.credentials, USER_TOKEN_TYPE)
    except jwt.InvalidTokenError as exc:
        raise _unauthorized("invalid_or_expired_token") from exc
    except TokenTypeMismatch as exc:
        raise _forbidden("token_type_mismatch") from exc

    account_id = payload.get("sub")
    if not account_id:
        raise _unauthorized("missing_subject")

    account = (
        await session.execute(select(Account).where(Account.account_id == UUID(account_id)))
    ).scalar_one_or_none()
    if account is None or account.status != AccountStatus.ACTIVE:
        raise _unauthorized("account_inactive")

    return UserIdentity(
        account_id=account.account_id, role=account.role, display_name=account.display_name
    )


async def get_current_device(
    session: AsyncSession = Depends(get_session),
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> UUID:
    """解析设备 Token 并注入 device_id；人 Token 到达设备接口返回 403。"""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized("missing_credentials")

    try:
        identity = await validate_device_token(session, credentials.credentials)
    except TokenTypeMismatch as exc:
        raise _forbidden("token_type_mismatch") from exc
    except DeviceCredentialError as exc:
        raise _unauthorized("invalid_device_token") from exc

    return identity.device_id


def require_role(*roles: str):
    """角色门禁依赖工厂：角色不匹配返回 403（不泄露资源存在性信息）。"""

    async def checker(user: UserIdentity = Depends(get_current_user)) -> UserIdentity:
        if user.role not in roles:
            raise _forbidden(f"role_not_allowed:{user.role}")
        return user

    return checker


require_admin = require_role(AccountRole.ADMIN)
