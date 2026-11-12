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

from app.auth.dependencies import UserIdentity, get_current_device, require_role
from app.auth.device_token import DeviceCredentialError, validate_device_token
from app.auth.jwt import TokenTypeMismatch
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import get_session, get_session_factory
from app.integrations import ProviderUnavailable
from app.models.enums import AccountRole, IdentitySource
from app.realtime.session import outbound_call_registry
from app.services import media as media_service
from app.services.device import DeviceNotFound, device_self_view
from app.services.voice import (
    BiometricDeviceMissing,
    VoiceprintMismatch,
    VoiceSession,
    biometric_list_rooms,
    biometric_login,
    enroll_voiceprint,
    generate_text_reply,
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
# 文字对话通道（ASR 不可用时的可见降级）
# ---------------------------------------------------------------------------


class TextChatIn(BaseModel):
    text: str = Field(..., max_length=200, description="长者文字输入（1–200 字）")


@router.post("/voice/text")
async def text_chat(
    data: TextChatIn,
    device_id: UUID = Depends(get_current_device),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """文字回复：复用语音通道的会话线程与 LLM 链路，只返回文本。

    ASR 挂掉时前端降级到文字输入，长者仍能获得回复（验收项 5：可见降级）。
    elder_id 仍由服务端「设备→房间」级联得出（单人间锁定长者；双人间走匿名线程），
    不接受前端传参；LLM 未配置/熔断/超时一律模板兜底，绝不因模型故障 5xx。
    """
    text = data.text.strip()
    if not text:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "文字内容不能为空")
    try:
        view = await device_self_view(session, device_id)
    except DeviceNotFound as exc:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "设备未绑定房间，无法使用文字通道"
        ) from exc
    elder = view["elder"]
    elder_id = elder.elder_id if elder is not None else None
    reply, trace = await generate_text_reply(
        device_id=device_id,
        elder_id=elder_id,
        identity=IdentitySource.DEVICE_ROOM if elder_id else IdentitySource.ANONYMOUS,
        text=text,
        db=session,
    )
    return {
        "reply": reply,
        "elder_id": str(elder_id) if elder_id else None,
        "degraded": bool(trace.payload().get("degraded")),
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


async def _handle_call_answer(event: dict, device_id: UUID) -> None:
    """服务端主动外呼的 answer 回执：按 call_id 找到挂起 PC 并设置远端描述。"""
    call_id = str(event.get("call_id") or "")
    call = outbound_call_registry.get(call_id)
    if call is None:
        logger.warning(
            "未知外呼 call_id，忽略 answer",
            extra={
                "event": "rtc_call_answer_unknown",
                "call_id": call_id,
                "device_id": str(device_id),
            },
        )
        return
    if call.device_id != device_id:
        # 越权：answer 必须来自外呼目标设备，直接拒绝并留审计痕迹
        logger.warning(
            "外呼 answer 设备不匹配，拒绝",
            extra={
                "event": "rtc_call_answer_forbidden",
                "call_id": call_id,
                "device_id": str(device_id),
                "target_device_id": str(call.device_id),
            },
        )
        return
    sdp = event.get("sdp")
    if not isinstance(sdp, str) or not sdp:
        logger.warning(
            "外呼 answer 缺少 sdp",
            extra={"event": "rtc_call_answer_invalid", "call_id": call_id},
        )
        return
    try:
        await call.set_answer(sdp, str(event.get("type") or "answer"))
    except Exception as exc:  # noqa: BLE001 —— 单个 answer 失败不得断开 WS
        logger.warning(
            "设置外呼 answer 失败",
            extra={
                "event": "rtc_call_answer_failed",
                "call_id": call_id,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )


async def _handle_media_event(
    event: dict,
    *,
    allowed_elder_ids: set[UUID],
    verified_elder_id: UUID | None,
) -> None:
    """前端播放结束/停止回执：校验归属后补 media_history，不触发 TTS。"""
    action = str(event.get("action") or "")
    history_id = event.get("history_id")
    if action not in ("ended", "stopped") or not history_id:
        logger.warning(
            "忽略非法 media_event",
            extra={"event": "media_event_invalid", "action": action},
        )
        return

    elder_id = verified_elder_id if verified_elder_id in allowed_elder_ids else None
    if elder_id is None and len(allowed_elder_ids) == 1:
        elder_id = next(iter(allowed_elder_ids))
    if elder_id is None:
        logger.warning(
            "media_event 无匹配长者，忽略",
            extra={"event": "media_event_forbidden", "history_id": str(history_id)},
        )
        return

    elapsed: float | None = None
    raw = event.get("elapsed_seconds")
    if raw is not None:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = -1.0
        if 0 <= value <= 86_400:
            elapsed = value

    try:
        async with get_session_factory()() as db:
            await media_service.record_end(
                db,
                elder_id,
                history_id,
                completed=action == "ended",
                elapsed_seconds=elapsed,
            )
            await db.commit()
    except Exception as exc:  # noqa: BLE001 —— 单条回执失败不得断开 WS
        logger.warning(
            "media_event 处理失败",
            extra={
                "event": "media_event_failed",
                "elder_id": str(elder_id),
                "error_type": type(exc).__name__,
            },
        )


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
    allowed_elder_ids = {elder_id for elder_id, _ in candidates}

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
            elif event.get("type") == "rtc_call_answer":
                await _handle_call_answer(event, device_id)
            elif event.get("type") == "media_event":
                await _handle_media_event(
                    event,
                    allowed_elder_ids=allowed_elder_ids,
                    verified_elder_id=voice_session.verified_elder_id,
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
