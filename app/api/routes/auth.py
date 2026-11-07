"""认证路由：登录、设备注册、身份自查（人/设备双向）。"""

import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.device_token import DeviceCredentialError, validate_device_token
from app.auth.jwt import USER_TOKEN_TYPE, TokenTypeMismatch, decode_token
from app.core.config import get_settings
from app.db.session import get_session
from app.models import Account, Device
from app.schemas.auth import (
    DeviceMeResponse,
    DeviceRegisterRequest,
    DeviceTokenResponse,
    LoginRequest,
    TokenResponse,
    UserMeResponse,
)
from app.services import account as account_service
from app.services import device as device_service
from app.services.account import InvalidCredentials

router = APIRouter(prefix="/auth", tags=["auth"])
bearer_scheme = HTTPBearer(auto_error=False)


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest, session: AsyncSession = Depends(get_session)
) -> TokenResponse:
    """人账号登录：成功 200，用户名/密码错误或账号不可用统一 401（不泄露差异）。"""
    try:
        account = await account_service.authenticate(session, body.username, body.password)
    except InvalidCredentials as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误"
        ) from exc

    token = create_user_token_for_account(account)
    settings = get_settings()
    return TokenResponse(
        access_token=token,
        role=account.role,
        expires_in=settings.jwt_access_token_expire_minutes * 60,
    )


def create_user_token_for_account(account: Account) -> str:
    from app.auth.jwt import create_user_access_token

    return create_user_access_token(str(account.account_id), account.role)


@router.post("/device/register", response_model=DeviceTokenResponse)
async def device_register(
    body: DeviceRegisterRequest, session: AsyncSession = Depends(get_session)
) -> DeviceTokenResponse:
    """平板注册：device_code + 一次性注册凭证换发长期设备 Token；凭证无效统一 401。"""
    try:
        issued = await device_service.register_device(
            session, body.device_code, body.registration_token
        )
    except DeviceCredentialError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="注册凭证无效"
        ) from exc
    return DeviceTokenResponse(
        access_token=issued.access_token,
        device_id=issued.token.device_id,
        expires_at=issued.expires_at,
    )


@router.get("/me")
async def me(
    session: AsyncSession = Depends(get_session),
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> UserMeResponse | DeviceMeResponse:
    """身份自查：人 JWT 返回账号身份，设备 Token 返回设备身份。"""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="认证失败",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = credentials.credentials

    try:
        payload = decode_token(token, USER_TOKEN_TYPE)
    except TokenTypeMismatch:
        # 设备 Token → 设备身份
        try:
            identity = await validate_device_token(session, token)
        except DeviceCredentialError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="认证失败"
            ) from exc
        device = (
            await session.execute(select(Device).where(Device.device_id == identity.device_id))
        ).scalar_one()
        return DeviceMeResponse(
            device_id=device.device_id,
            device_code=device.device_code,
            room_id=device.room_id,
            bed_no=device.bed_no,
            status=device.status,
        )
    except pyjwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="认证失败"
        ) from exc

    account = (
        await session.execute(select(Account).where(Account.account_id == payload["sub"]))
    ).scalar_one_or_none()
    if account is None or account.status != "active":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="认证失败")
    return UserMeResponse(
        account_id=account.account_id,
        username=account.username,
        display_name=account.display_name,
        role=account.role,
    )


@router.get("/me/user", response_model=UserMeResponse)
async def me_user(
    user=Depends(get_current_user), session: AsyncSession = Depends(get_session)
) -> UserMeResponse:
    """人身份自查（依赖注入路径，供测试与后台使用）。"""
    account = (
        await session.execute(select(Account).where(Account.account_id == user.account_id))
    ).scalar_one()
    return UserMeResponse(
        account_id=account.account_id,
        username=account.username,
        display_name=account.display_name,
        role=account.role,
    )
