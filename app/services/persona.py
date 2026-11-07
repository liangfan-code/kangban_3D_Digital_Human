"""数字人人设管理业务（仅 admin）：code 唯一 409，软删 = is_active=false（幂等）。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import UserIdentity
from app.core.logging import get_logger
from app.models import Persona
from app.schemas.persona import PersonaCreate, PersonaUpdate

logger = get_logger(__name__)


class PersonaValidationError(Exception):
    """业务校验失败；路由层映射 422。"""


class PersonaConflict(Exception):
    """人设 code 重复；路由层映射 409。"""


class PersonaNotFound(Exception):
    """人设不存在；路由层映射 404。"""


async def create_persona(
    session: AsyncSession, data: PersonaCreate, actor: UserIdentity
) -> Persona:
    existing = (
        await session.execute(select(Persona).where(Persona.code == data.code))
    ).scalar_one_or_none()
    if existing is not None:
        raise PersonaConflict("人设编码已存在")

    persona = Persona(
        code=data.code,
        name=data.name,
        avatar_url=data.avatar_url,
        personality=data.personality,
        greeting=data.greeting,
        voice_id=data.voice_id,
        is_active=data.is_active,
    )
    session.add(persona)
    await session.flush()
    logger.info(
        "persona created",
        extra={
            "event": "persona_created",
            "actor_id": str(actor.account_id),
            "object_type": "persona",
            "object_id": str(persona.persona_id),
            "result": "success",
        },
    )
    return persona


async def update_persona(
    session: AsyncSession, persona: Persona, data: PersonaUpdate, actor: UserIdentity
) -> Persona:
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(persona, key, value)
    await session.flush()
    await session.refresh(persona)
    logger.info(
        "persona updated",
        extra={
            "event": "persona_updated",
            "actor_id": str(actor.account_id),
            "object_type": "persona",
            "object_id": str(persona.persona_id),
            "result": "success",
        },
    )
    return persona


async def deactivate_persona(
    session: AsyncSession, persona: Persona, actor: UserIdentity
) -> None:
    """软删：is_active=false（幂等）。已引用的长者保留，后续新建/换绑校验拒绝。"""
    if persona.is_active:
        persona.is_active = False
        await session.flush()
        logger.info(
            "persona deactivated",
            extra={
                "event": "persona_deactivated",
                "actor_id": str(actor.account_id),
                "object_type": "persona",
                "object_id": str(persona.persona_id),
                "result": "success",
            },
        )


async def get_persona_or_404(session: AsyncSession, persona_id) -> Persona:
    persona = (
        await session.execute(select(Persona).where(Persona.persona_id == persona_id))
    ).scalar_one_or_none()
    if persona is None:
        raise PersonaNotFound("人设不存在")
    return persona
