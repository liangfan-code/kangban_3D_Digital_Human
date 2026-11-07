"""语音接口路由（Day3）：WS 事件通道 + 声纹录入/查询。

权限口径（验收项 11）：
- WS /voice/ws：首条消息必须携带设备 Token（type=auth）；人 JWT → 4403 关闭，
  无效/过期 → 4401；elder_id 一律由服务端级联/声纹得出，绝不收前端传参；
- POST /voice/enroll/device：单人间设备 Token（自动锁定长者）；
- POST /voice/enroll/admin：管理员指定 elder_id（双人间口径，ADR-005）；
- GET /voice/voiceprints/{elder_id}：管理员查询声纹记录（不含向量本体）。

事件协议（用户决策①：同源 WS）：ready / verification / state / transcript /
tts / done / need_confirm / error；音频从 WebRTC 轨进入（app/realtime），
WS 只传 JSON 事件（用户决策②：静音检测在服务端 TEN-VAD）。
"""

import asyncio
import base64
import binascii
import json
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.device_token import DeviceCredentialError, validate_device_token
from app.auth.dependencies import UserIdentity, get_current_device, require_role
from app.core.logging import get_logger
from app.core.config import get_settings
from app.db.session import get_session, get_session_factory
from app.integrations import ProviderUnavailable
from app.models.enums import AccountRole
from app.services.device import DeviceNotFound, device_self_view
from app.services.voice import (
    BiometricDeviceMissing,
    VoiceSession,
    VoiceprintMismatch,
    biometric_list_rooms,
    biometric_login,
    enroll_voiceprint,
    list_voiceprints,
    load_room_voiceprints,
    voice_session_manager,
)

logger = get_logger(__name__)

router = APIRouter(tags=["voice"])

_WS_AUTH_TIMEOUT_SECONDS = 10
_MAX_ENROLL_SAMPLES = 4  # 录入最多 4 段（Day3 规划：2 段 × 约 3 秒）
_MAX_SAMPLE_BYTES = 16_000 * 2 * 10  # 单段 ≤10s 的 16k int16 PCM


# ---------------------------------------------------------------------------
# 声纹录入与查询
# ---------------------------------------------------------------------------


class EnrollIn(BaseModel):
    audio_samples: list[str] = Field(
        ..., description="16kHz mono int16 PCM 的 base64 数组（每段约 3 秒）"
    )
    sample_uri: str | None = None
    elder_id: UUID | None = Field(
        default=None, description="仅管理员通道可指定；设备通道禁止（身份服务端确定）"
    )


def _decode_samples(raw: list[str]) -> list[bytes]:
    if not 1 <= len(raw) <= _MAX_ENROLL_SAMPLES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "样本数量须为 1–4 段")
    samples: list[bytes] = []
    for item in raw:
        try:
            pcm = base64.b64decode(item, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "样本不是合法 base64"
            ) from exc
        if not 1600 <= len(pcm) <= _MAX_SAMPLE_BYTES:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "单段样本须为 0.05–10 秒的 16k PCM"
            )
        samples.append(pcm)
    return samples


@router.post("/voice/enroll/device")
async def enroll_by_device(
    data: EnrollIn,
    device_id: UUID = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """单人间设备 Token 录入：elder 由设备→房间级联锁定，不收前端 elder_id。"""
    if data.elder_id is not None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "设备通道不接受 elder_id，身份由服务端确定"
        )
    try:
        view = await device_self_view(session, device_id)
    except DeviceNotFound as exc:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "设备未绑定房间，无法录入声纹"
        ) from exc
    if view["mode"] != "single" or view["elder"] is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "双人间设备请由管理员指定长者录入声纹"
        )
    samples = _decode_samples(data.audio_samples)
    try:
        record = await enroll_voiceprint(
            session,
            elder_id=view["elder"].elder_id,
            audio_samples=samples,
            sample_uri=data.sample_uri,
        )
    except ProviderUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return {"voiceprint_id": str(record.voiceprint_id), "status": record.status}


@router.post("/voice/enroll/admin")
async def enroll_by_admin(
    data: EnrollIn,
    user: UserIdentity = Depends(require_role(AccountRole.ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """管理员为指定长者录入（双人间口径）；elder_id 由管理员请求体指定。"""
    if data.elder_id is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "管理员通道必须指定 elder_id"
        )
    samples = _decode_samples(data.audio_samples)
    try:
        record = await enroll_voiceprint(
            session, elder_id=data.elder_id, audio_samples=samples, sample_uri=data.sample_uri
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "长者不存在") from exc
    except ProviderUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    logger.info(
        "voiceprint enroll by admin",
        extra={
            "event": "voiceprint_enroll_admin",
            "operator": str(user.account_id),
            "elder_id": str(data.elder_id),
        },
    )
    return {"voiceprint_id": str(record.voiceprint_id), "status": record.status}


@router.get("/voice/voiceprints/{elder_id}")
async def list_voiceprints_route(
    elder_id: UUID,
    _user: UserIdentity = Depends(require_role(AccountRole.ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """管理员查询长者声纹记录（不返回向量本体）。"""
    records = await list_voiceprints(session, elder_id)
    return {
        "elder_id": str(elder_id),
        "items": [
            {
                "voiceprint_id": str(r.voiceprint_id),
                "embedding_model": r.embedding_model,
                "status": r.status,
                "sample_uri": r.sample_uri,
                "created_at": r.created_at.isoformat(),
            }
            for r in records
        ],
    }


# ---------------------------------------------------------------------------
# 生物识别演示登录（登录页；开关 BIOMETRIC_DEMO_ENABLED，默认开）
# ---------------------------------------------------------------------------


class BiometricLoginIn(BaseModel):
    room_id: UUID
    mode: str = Field(..., description="fingerprint（教学演示直进）| voiceprint（真比对）")
    audio_samples: list[str] | None = Field(
        default=None, description="voiceprint 模式必填：16k mono int16 PCM base64（约 5 秒 1 段）"
    )


def _ensure_biometric_enabled() -> None:
    if not get_settings().biometric_demo_enabled:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "生物识别演示登录未开启（BIOMETRIC_DEMO_ENABLED）"
        )


@router.get("/voice/biometric/rooms")
async def biometric_rooms_route(
    session: AsyncSession = Depends(get_session),
) -> dict:
    """演示登录房间列表（全部房间 + 在住长者 + 活跃设备状态）。"""
    _ensure_biometric_enabled()
    rooms = await biometric_list_rooms(session)
    return {"items": rooms}


@router.post("/voice/biometric/login")
async def biometric_login_route(
    data: BiometricLoginIn,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """演示登录：通过后签发房间活跃设备的真实设备 Token（前端进 elder.html）。

    声纹模式复用 Day3 CAMPPlus + voiceprints 真比对；指纹模式为无硬件教学演示直进。
    开关关闭时一律 403，恢复严格设备 Token 鉴权。
    """
    _ensure_biometric_enabled()
    if data.mode not in ("fingerprint", "voiceprint"):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "mode 须为 fingerprint 或 voiceprint"
        )
    if data.mode == "fingerprint" and data.audio_samples:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "fingerprint 模式不接受音频样本"
        )
    samples: list[bytes] | None = None
    if data.mode == "voiceprint":
        if not data.audio_samples:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "voiceprint 模式必须携带音频样本"
            )
        samples = _decode_samples(data.audio_samples[:1])  # 演示登录只取 1 段
    try:
        result = await biometric_login(
            session, room_id=data.room_id, mode=data.mode, audio_samples=samples
        )
    except BiometricDeviceMissing as exc:  # LookupError 子类，须在 LookupError 之前
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except VoiceprintMismatch as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except ProviderUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return result


# ---------------------------------------------------------------------------
# WS 事件通道
# ---------------------------------------------------------------------------


async def _auth_websocket(ws: WebSocket, session: AsyncSession) -> UUID:
    """首条消息鉴权：设备 Token → device_id；人 JWT 4403 / 无效 4401。"""
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=_WS_AUTH_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        await ws.close(code=4401, reason="auth timeout")
        raise WebSocketDisconnect(code=4401) from exc
    try:
        message = json.loads(raw)
    except ValueError as exc:
        await ws.close(code=4401, reason="auth message must be json")
        raise WebSocketDisconnect(code=4401) from exc
    token = message.get("token") if message.get("type") == "auth" else None
    if not token:
        await ws.close(code=4401, reason="first message must be auth with token")
        raise WebSocketDisconnect(code=4401)

    try:
        identity = await validate_device_token(session, token)
    except TokenTypeMismatch as exc:
        await ws.close(code=4403, reason="human token not allowed on device channel")
        raise WebSocketDisconnect(code=4403) from exc
    except DeviceCredentialError as exc:
        await ws.close(code=4401, reason="invalid device token")
        raise WebSocketDisconnect(code=4401) from exc
    return identity.device_id


async def _build_session_context(
    session: AsyncSession, device_id: UUID
) -> tuple[list[tuple[UUID, str]], dict[UUID, list[list[float]]], dict]:
    """设备→房间→长者级联 + 声纹快照（elder_id 全部来自服务端，ADR-005）。"""
    view = await device_self_view(session, device_id)
    if view["mode"] == "single":
        candidates = (
            [(view["elder"].elder_id, view["elder"].name)] if view["elder"] else []
        )
    else:
        candidates = [(e.elder_id, e.name) for e in view["candidates"]]
    embeddings = await load_room_voiceprints(session, [eid for eid, _ in candidates])
    return candidates, embeddings, view


@router.websocket("/voice/ws")
async def voice_ws(ws: WebSocket, session: AsyncSession = Depends(get_session)) -> None:
    """语音事件通道：设备 Token 鉴权 → 会话状态机 → JSON 事件流。

    DB 会话由 get_session 依赖注入（可被测试覆盖）；连接期间持有该会话，
    Day3 会话不写库，无长事务风险。
    """
    await ws.accept()
    try:
        device_id = await _auth_websocket(ws, session)
    except WebSocketDisconnect:
        return

    try:
        candidates, embeddings, view = await _build_session_context(session, device_id)
    except DeviceNotFound:
        await ws.send_json(
            {"type": "error", "code": "device_unbound", "message": "设备未绑定房间"}
        )
        await ws.close(code=4403, reason="device not bound to room")
        return

    send_lock = asyncio.Lock()

    async def send_event(event: dict) -> None:
        async with send_lock:
            await ws.send_json(event)

    db_factory = get_session_factory()

    async def reload_embeddings() -> dict[UUID, list[list[float]]]:
        """现场重查房间声纹（会话建立后才录入的场景，录入后无需重连）。"""
        session_factory = get_session_factory()
        async with session_factory() as db:
            return await load_room_voiceprints(db, [eid for eid, _ in candidates])

    voice_session = VoiceSession(
        device_id,
        candidates,
        embeddings,
        send_event,
        embedding_reloader=reload_embeddings,
        db_factory=db_factory,
    )
    voice_session_manager.register(voice_session)
    await send_event(
        {
            "type": "ready",
            "mode": view["mode"],
            "room_number": view["room_number"],
            "candidates": [name for _, name in candidates] if view["mode"] == "double" else [],
            "voiceprint_ready": bool(any(embeddings.values())),
        }
    )
    logger.info(
        "voice session ready",
        extra={
            "event": "voice_session_ready",
            "device_id": str(device_id),
            "mode": view["mode"],
        },
    )

    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            if (text := message.get("text")) is None:
                continue  # 二进制消息不处理：音频走 WebRTC 轨（决策①②）
            try:
                event = json.loads(text)
            except ValueError:
                continue
            if event.get("type") == "resume" and isinstance(event.get("token"), str):
                resumed = voice_session.try_resume(event["token"])
                await send_event(
                    {
                        "type": "state",
                        "state": "verified" if resumed else "awaiting_voiceprint",
                    }
                )
            elif event.get("type") == "ping":
                await send_event({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        voice_session_manager.unregister(device_id)
        logger.info(
            "voice session closed",
            extra={"event": "voice_session_closed", "device_id": str(device_id)},
        )
