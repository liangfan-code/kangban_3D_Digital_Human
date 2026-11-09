"""语音会话编排服务（Day3）：声纹门控 → ASR → 模板回复 → TTS。

链路口径（Day3 规划 §3，四项决策 D1/D2）：
- 音频承载：WebRTC 轨（app/realtime 由 POST /api/v1/rtc/offer 建立），
  realtime 解码重采样为 16k mono int16 PCM 后推入本服务；
- 事件承载：同源 WS /api/v1/voice/ws（用户决策①，DataChannel 留后续演进）；
- 静音检测：服务端 TEN-VAD（用户决策②）；utterance 端点先用能量粗分，
  再交声纹 Provider 的 TEN-VAD 精过滤；
- 声纹存储：voiceprints 表 + pgvector（D2 决策），本服务负责入库与身份判定，
  Provider 只做向量提取/相似度；
- 身份铁律（ADR-005）：单人间 elder_id 由设备→房间级联锁定；双人间由声纹在
  在住长者集合中匹配确定；elder_id 永不接受前端传参；
- 临时 Token：仅内存（进程内字典），不落库，默认 300s（voiceprint_token_ttl）；
- 降级（验收项 5）：FunASR 不可达/声纹未配置 → WS 事件可见降级 + 文字输入通道
  （HTTP 已有），绝不静默；
- 可观测（验收项 10）：终句写 asr_confidence/asr_e2e_ms/tts_first_byte_ms，
  低置信（<0.6）打标 asr_low_conf；原始音频不落盘不入日志（TDD §4 禁区）。
- LLM 回复（Day4 用户决策）：System Two（Qwen 兼容 API）流式分句回复，带最近 5 轮
  历史（conversation_messages）与人设/动作协议 system prompt；未配置或调用失败
  可见降级回模板回复 + 关键词规则动作，通话绝不中断。
"""

import asyncio
import base64
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from websockets.exceptions import WebSocketException

from app.core.config import get_settings
from app.core.logging import get_logger
from app.integrations import (
    ProviderUnavailable,
    TTSProvider,
    VoiceprintProvider,
)
from app.integrations.edge_tts import EdgeTTSProvider
from app.integrations.funasr_ws import FunASRWSProvider
from app.integrations.llm import LlmUnavailable, stream_chat
from app.integrations.llm import is_configured as llm_is_configured
from app.integrations.sherpa_voiceprint import SherpaVoiceprintProvider
from app.models import Elder, Voiceprint
from app.models.enums import DeviceStatus, ElderStatus, IdentitySource, VoiceprintStatus
from app.services import actions as action_service
from app.services import context_summary, conversation_store
from app.services.latency import TurnTrace
from app.services.resilience import CircuitOpen, get_llm_breaker, get_tts_breaker
from app.services.session_state import get_session_store
from app.services.slot_extractor import extract_slots
from app.services.viseme import build_viseme_timeline

logger = get_logger(__name__)

# ---- 可调常量（utterance 端点粗分；TEN-VAD 精过滤在 Provider 内）----
ENERGY_THRESHOLD = 500  # int16 RMS 阈值：低于视为静音
SILENCE_MS_TO_CLOSE = 800  # 静音多久判定一句话结束
MIN_UTTERANCE_MS = 300  # 缓冲总时长下限（喘息/碰撞噪声）
SPEECH_ONSET_MS = 180  # 语音起始判定：需连续有能量帧达到该时长（Day4 实测：单帧即判会把
#                       碰撞/电视等短促噪声当语音送 ASR，paraformer 对纯噪声段产出伪终句）
MIN_SPEECH_MS = 250  # 整句有效语音时长下限：低于该值整句静默丢弃不送 ASR（防伪终句）。
#                     注意不能更高：实测 edge-tts/真人短命令「再见」仅 ~420ms、「好」~300ms
MAX_BUFFER_MS = 15_000  # 单句上限，防缓冲失控
LOW_CONFIDENCE = 0.6  # 低置信打标线（大纲课时 3.3）
MAX_VERIFY_FAILURES = 3  # 连续失败触发「主动确认」（ADR-008）
TTS_ECHO_GUARD_EXTRA_MS = 1500  # TTS 下发后的回声防护余量（实测事故：数字人朗读被麦克风
#                                 回拾 → VAD 误判 → ASR 伪终句 → 再回复，形成自激循环；
#                                 前端播放期静音 + onended 不可靠，服务端必须自有防线）


def template_reply(text: str) -> str:
    """Day3 固定模板回复（LLM 未配置/失败时的降级路径）。"""
    return f"你刚才说的是：{text}"


# ---- Day4：LLM 流式回复辅助 ----

_SENTENCE_END_RE = re.compile(r"[。！？；!?;\n…]")

_PERSONA_PATH = Path(__file__).resolve().parents[1] / "prompts" / "personas" / "elder_companion.md"


class _SentenceAssembler:
    """LLM 增量输出 → 完整句子流：凑到句末标点即产出，流结束冲刷余量。

    动作标记 [act:*] 位于句首且不含句末标点，随首句一起产出，
    因此整句 parse_action_markers 即可安全剥除（无需老师流式过滤器）。
    """

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, delta: str) -> list[str]:
        self._buf += delta
        return self._drain()

    def finish(self) -> list[str]:
        rest, self._buf = self._buf, ""
        return [rest] if rest.strip() else []

    def _drain(self) -> list[str]:
        sentences: list[str] = []
        while True:
            match = _SENTENCE_END_RE.search(self._buf)
            if match is None:
                break
            sentence, self._buf = self._buf[: match.end()], self._buf[match.end() :]
            if sentence.strip():
                sentences.append(sentence)
        return sentences


@lru_cache(maxsize=4)
def _load_persona_cached(path: str, mtime_ns: int) -> str:
    del mtime_ns
    raw = Path(path).read_text(encoding="utf-8")
    # 跳过文件头部标题与使用说明（首个 "## " 小节起才是 prompt 正文）
    idx = raw.find("## ")
    return raw[idx:] if idx >= 0 else raw


def _load_persona() -> str:
    try:
        return _load_persona_cached(str(_PERSONA_PATH), _PERSONA_PATH.stat().st_mtime_ns)
    except OSError:
        logger.warning("persona prompt 缺失，使用最小兜底人设")
        return "你是「康伴」，养老社区里陪伴长者的数字人伙伴。说短句、口语化、温和亲切。"


def _build_system_prompt() -> str:
    """人设 + 动作协议（动作表不可用时协议段缺省，LLM 只出纯文本）。"""
    persona = _load_persona()
    action_prompt = action_service.build_action_prompt()
    return persona + ("\n\n" + action_prompt if action_prompt else "")


# ---------------------------------------------------------------------------
# 会话上下文与 LLM 流式回复（语音/文字双通道共用）
# ---------------------------------------------------------------------------


async def _load_thread(
    db: AsyncSession,
    *,
    device_id: UUID,
    elder_id: UUID | None,
    identity: IdentitySource,
) -> tuple[UUID | None, list]:
    """复用/新建会话线程并取最近 5 轮历史；任何库异常都降级为不落库、无历史。"""
    try:
        conversation = await conversation_store.get_or_create_active_conversation(
            db, device_id=device_id, elder_id=elder_id, identity_source=identity
        )
        history = await conversation_store.recent_messages(db, conversation.thread_id)
        await db.commit()
        return conversation.thread_id, history
    except Exception:  # noqa: BLE001 —— 历史缺失不阻断对话
        logger.exception("conversation context load failed")
        await db.rollback()
        return None, []


async def _prepare_turn_state(thread_id: UUID | None, text: str) -> str | None:
    """抽槽 + 轮次累加，返回注入 system prompt 的动态上下文。

    槽位只写会话状态，不执行任何动作，也不触碰 ``verified_elder_id``。
    """
    if thread_id is None:
        return None
    try:
        store = get_session_store()
        async with store.mutate(str(thread_id)) as state:
            state.slots = extract_slots(text, state.slots)
            state.turn_count += 1
        return context_summary.build_context_message(state)
    except Exception:  # noqa: BLE001 —— 状态缺失不阻断对话
        logger.exception("session state prepare failed")
        return None


async def _persist_turn(
    db: AsyncSession, thread_id: UUID | None, user_text: str, assistant_text: str
) -> bool:
    """落库一轮问答；失败不影响回复，返回是否成功（成功才刷新早期摘要）。"""
    if thread_id is None:
        return False
    try:
        await conversation_store.record_turn(
            db, thread_id=thread_id, user_text=user_text, assistant_text=assistant_text
        )
        await db.commit()
        return True
    except Exception:  # noqa: BLE001 —— 落库失败不影响对话
        logger.exception("conversation turn persist failed")
        await db.rollback()
        return False


async def _emit_or_clean(
    sentence: str, emit: Callable[[str], Awaitable[str]] | None
) -> str:
    """有副作用回调时交给调用方（语音通道发 action/TTS），否则只剥动作标记。"""
    if emit is not None:
        return await emit(sentence)
    cleaned, _ = action_service.parse_action_markers(sentence)
    return cleaned


async def _collect_llm_sentences(
    messages: list[dict[str, str]],
    collected: list[str],
    *,
    trace: TurnTrace | None = None,
    emit: Callable[[str], Awaitable[str]] | None = None,
) -> None:
    """流式消费 LLM 增量 → 整句，剥净的句子追加进 ``collected``。

    ``emit`` 是逐句副作用（语音通道用来先发动作事件再合成语音），缺省只剥标记。
    熔断打开 / 首 token 超时统一抛 ``LlmUnavailable``，已产出的句子留在
    ``collected``，调用方据此决定「沿用部分回复」还是回退模板。
    """
    assembler = _SentenceAssembler()
    iterator = stream_chat(messages).__aiter__()
    first = True
    if trace is not None:
        trace.mark("llm_start")
    try:
        async with get_llm_breaker().guard():
            while True:
                try:
                    if first:
                        delta = await asyncio.wait_for(
                            anext(iterator),
                            timeout=get_settings().llm_first_token_timeout_seconds,
                        )
                    else:
                        delta = await anext(iterator)
                except StopAsyncIteration:
                    break
                if first:
                    first = False
                    if trace is not None:
                        trace.mark("llm_first_token")
                for sentence in assembler.feed(delta):
                    collected.append(await _emit_or_clean(sentence, emit))
            for sentence in assembler.finish():
                collected.append(await _emit_or_clean(sentence, emit))
    except CircuitOpen as exc:
        if trace is not None:
            trace.degrade("llm_circuit_open")
        raise LlmUnavailable(f"LLM 熔断中：{exc}") from exc
    except TimeoutError as exc:
        if trace is not None:
            trace.degrade("llm_first_token_timeout")
        raise LlmUnavailable("LLM 首 token 超时") from exc
    finally:
        close = getattr(iterator, "aclose", None)
        if close is not None:
            await close()
    if trace is not None:
        trace.mark("llm_end")


async def generate_text_reply(
    *,
    device_id: UUID,
    elder_id: UUID | None,
    identity: IdentitySource,
    text: str,
    db: AsyncSession,
) -> tuple[str, TurnTrace]:
    """文字通道（ASR 不可用时的可见降级）：复用 LLM 链路，只返回文本。

    与语音通道共用会话线程、最近历史、槽位与早期摘要；不发 WS 事件、不做 TTS。
    LLM 未配置/熔断/首 token 超时一律可见降级为模板回复，绝不向上抛模型错误。
    ``elder_id`` 由调用方用服务端设备→房间级联得出，不接受前端传参。
    """
    trace = TurnTrace()
    trace.set_meta("device_id", str(device_id))
    trace.set_meta("elder_id", str(elder_id) if elder_id else None)
    trace.mark("text_in")

    thread_id, history = await _load_thread(
        db, device_id=device_id, elder_id=elder_id, identity=identity
    )
    trace.set_meta("thread_id", str(thread_id) if thread_id else None)
    context_text = await _prepare_turn_state(thread_id, text)

    collected: list[str] = []
    if llm_is_configured():
        system_content = _build_system_prompt()
        if context_text:
            system_content = system_content + "\n\n" + context_text
        messages: list[dict[str, str]] = [{"role": "system", "content": system_content}]
        messages += [{"role": m.role, "content": m.content} for m in history]
        messages.append({"role": "user", "content": text})
        try:
            await _collect_llm_sentences(messages, collected, trace=trace)
        except LlmUnavailable as exc:
            trace.degrade("llm_fallback")
            logger.warning(
                "text llm unavailable, fallback to template",
                extra={"event": "llm_fallback", "error": str(exc)},
            )
        except Exception as exc:  # noqa: BLE001 —— 模型侧异常不向长者抛 5xx
            trace.degrade("llm_error")
            logger.exception(
                "text llm error, fallback to template",
                extra={"event": "llm_fallback", "error": str(exc)},
            )
    else:
        trace.degrade("llm_not_configured")

    reply = "".join(collected).strip()
    if not reply:
        reply = action_service.parse_action_markers(template_reply(text))[0]
    await _persist_turn(db, thread_id, text, reply)
    trace.mark("done")
    trace.log(event="text_latency")
    return reply, trace


# ---------------------------------------------------------------------------
# Provider 惰性单例（缺省可启动：未配置时返回 None，由调用方走降级）
# ---------------------------------------------------------------------------

_voiceprint_provider: VoiceprintProvider | None = None
_voiceprint_checked = False

_tts_provider: TTSProvider | None = None
_tts_checked = False


def get_voiceprint_provider() -> VoiceprintProvider | None:
    """返回 CAMPPlus Provider；未配置/缺模型时 None（声纹门控整体降级）。"""
    global _voiceprint_provider, _voiceprint_checked
    if not _voiceprint_checked:
        _voiceprint_checked = True
        settings = get_settings()
        if settings.sherpa_model_dir:
            try:
                _voiceprint_provider = SherpaVoiceprintProvider(
                    settings.sherpa_model_dir,
                    verify_seconds=settings.voiceprint_min_speech_seconds,
                )
                logger.info(
                    "voiceprint provider loaded",
                    extra={"event": "voiceprint_provider_loaded"},
                )
            except ProviderUnavailable as exc:
                logger.warning(
                    "voiceprint provider unavailable",
                    extra={"event": "voiceprint_provider_unavailable", "error": str(exc)},
                )
    return _voiceprint_provider


def get_tts_provider() -> TTSProvider:
    """返回 Edge-TTS Provider（无外部依赖门槛，构造即用）。"""
    global _tts_provider, _tts_checked
    if not _tts_checked:
        _tts_checked = True
        _tts_provider = EdgeTTSProvider(voice=get_settings().edge_tts_voice)
    return _tts_provider


def create_asr_session() -> FunASRWSProvider:
    """按当前配置构造一次 FunASR 会话；未配置抛 ProviderUnavailable。"""
    settings = get_settings()
    return FunASRWSProvider(settings.funasr_url, insecure=settings.funasr_insecure)


# ---------------------------------------------------------------------------
# 临时声纹 Token（仅内存，不落库）
# ---------------------------------------------------------------------------


@dataclass
class VoiceTokenRecord:
    token: str
    device_id: UUID
    elder_id: UUID
    score: float
    expires_at: float  # time.monotonic() 基准


class VoiceTokenStore:
    """进程内免验 Token 存储；重启即失效（Day3 口径，仅内存）。"""

    def __init__(self) -> None:
        self._records: dict[str, VoiceTokenRecord] = {}

    def issue(self, device_id: UUID, elder_id: UUID, score: float) -> tuple[str, float]:
        ttl = get_settings().voiceprint_token_ttl
        token = uuid.uuid4().hex
        expires_at = time.monotonic() + ttl
        self._records[token] = VoiceTokenRecord(
            token=token, device_id=device_id, elder_id=elder_id, score=score, expires_at=expires_at
        )
        self._sweep()
        return token, ttl

    def validate(self, token: str, device_id: UUID) -> VoiceTokenRecord | None:
        record = self._records.get(token)
        if record is None:
            return None
        if record.device_id != device_id or time.monotonic() > record.expires_at:
            self._records.pop(token, None)
            return None
        return record

    def _sweep(self) -> None:
        now = time.monotonic()
        for key in [k for k, v in self._records.items() if v.expires_at <= now]:
            self._records.pop(key, None)


voice_token_store = VoiceTokenStore()


# ---------------------------------------------------------------------------
# 声纹入库与查询（voiceprints 表）
# ---------------------------------------------------------------------------


async def enroll_voiceprint(
    session: AsyncSession,
    *,
    elder_id: UUID,
    audio_samples: list[bytes],
    sample_uri: str | None = None,
) -> Voiceprint:
    """录入声纹：多段音频向量取平均归一化后写 voiceprints（status=active）。

    身份校验在路由层完成（单人间设备级联 / 双人间管理员指定），本函数只做
    「样本有效性 → 向量 → 落库」。样本无效抛 ValueError（路由映射 422）。
    """
    provider = get_voiceprint_provider()
    if not audio_samples:
        raise ValueError("缺少声纹样本")

    elder = (
        await session.execute(select(Elder).where(Elder.elder_id == elder_id))
    ).scalar_one_or_none()
    if elder is None:
        raise LookupError("长者不存在")
    if provider is None:
        if get_settings().sherpa_model_dir:
            raise ProviderUnavailable(
                "声纹模型加载失败（检查 models/ 下 CAM++ 与 ten-vad 文件，或查看启动日志）"
            )
        raise ProviderUnavailable("声纹模型未配置（SHERPA_MODEL_DIR），无法录入")

    settings = get_settings()
    speech_only = getattr(provider, "speech_only", None)
    min_samples = 2 if len(audio_samples) >= 2 else 1
    embeddings: list[list[float]] = []
    speech_ms = 0
    skipped: list[str] = []
    for sample in audio_samples:
        speaker_audio = sample
        try:
            if speech_only is not None:
                speaker_audio = await asyncio.to_thread(speech_only, sample)
            embedding = await asyncio.to_thread(provider.extract_embedding, speaker_audio)
        except ValueError as exc:
            # 单段不合格只作废该段：另一段合格时仍可录入（减轻重复朗读负担）
            skipped.append(str(exc))
            continue
        embeddings.append(embedding)
        # 16kHz int16：字节数 / 32 = 毫秒
        speech_ms += len(speaker_audio) // 32
    if len(embeddings) < min_samples:
        detail = "；".join(dict.fromkeys(skipped)) or "样本无效"
        raise ValueError(
            f"有效样本不足（{len(embeddings)}/{min_samples} 段）：{detail}，请重录未通过的那一段"
        )
    if speech_ms < settings.voiceprint_enroll_min_speech_seconds * 1000:
        raise ValueError(
            f"有效语音合计仅 {speech_ms / 1000:.1f} 秒，需至少 "
            f"{settings.voiceprint_enroll_min_speech_seconds:g} 秒，请每段再多说一会儿"
        )
    mean = [
        sum(col) / len(embeddings) for col in zip(*embeddings, strict=True)
    ]
    normalized = SherpaVoiceprintProvider._normalize_list(mean)

    fingerprint = provider.model_fingerprint()
    record = Voiceprint(
        elder_id=elder_id,
        embedding=normalized,
        embedding_model=f"campplus-3dspeaker:{fingerprint[:12]}",
        sample_uri=sample_uri,
        status=VoiceprintStatus.ACTIVE,
    )
    session.add(record)
    await session.commit()
    logger.info(
        "voiceprint enrolled",
        extra={
            "event": "voiceprint_enrolled",
            "elder_id": str(elder_id),
            "voiceprint_id": str(record.voiceprint_id),
            "samples": len(audio_samples),
            "samples_valid": len(embeddings),
            "samples_skipped": len(skipped),
            "speech_ms": speech_ms,
        },
    )
    return record


async def list_voiceprints(session: AsyncSession, elder_id: UUID) -> list[Voiceprint]:
    """查询某长者的声纹记录（管理员接口用，不返回向量本体）。"""
    return list(
        (
            await session.execute(
                select(Voiceprint)
                .where(Voiceprint.elder_id == elder_id)
                .order_by(Voiceprint.created_at.desc())
            )
        ).scalars()
    )


async def load_room_voiceprints(
    session: AsyncSession, elder_ids: list[UUID]
) -> dict[UUID, list[list[float]]]:
    """加载在住长者的 active 声纹快照（WS 建连时取一次，Day3 不做会话内刷新）。"""
    if not elder_ids:
        return {}
    rows = (
        await session.execute(
            select(Voiceprint).where(
                Voiceprint.elder_id.in_(elder_ids),
                Voiceprint.status == VoiceprintStatus.ACTIVE,
            )
        )
    ).scalars()
    result: dict[UUID, list[list[float]]] = {}
    for row in rows:
        result.setdefault(row.elder_id, []).append(list(row.embedding))
    return result


# ---------------------------------------------------------------------------
# 生物识别演示登录（登录页"老人生物识别进入"；开关 BIOMETRIC_DEMO_ENABLED）
# ---------------------------------------------------------------------------


class BiometricDeviceMissing(LookupError):
    """房间没有可用的活跃设备，无法签发设备身份。"""


class VoiceprintMismatch(ValueError):
    """声纹比对未达阈值（或房间长者全部无声纹）。"""


async def biometric_list_rooms(session: AsyncSession) -> list[dict]:
    """演示登录房间列表：全部房间，附在住长者与活跃设备状态（前端据此提示）。"""
    from app.models import Device, Room

    rooms = list(
        (
            await session.execute(
                select(Room).order_by(
                    Room.building_code, Room.floor, Room.room_number
                )
            )
        ).scalars()
    )
    active_elders = list(
        (
            await session.execute(
                select(Elder).where(
                    Elder.room_id.isnot(None), Elder.status == ElderStatus.ACTIVE
                )
            )
        ).scalars()
    )
    active_devices = list(
        (
            await session.execute(
                select(Device).where(Device.status == DeviceStatus.ACTIVE)
            )
        ).scalars()
    )
    elders_by_room: dict[UUID, list[str]] = {}
    for e in active_elders:
        elders_by_room.setdefault(e.room_id, []).append(e.name)
    devices_by_room: dict[UUID, str] = {
        d.room_id: d.device_code for d in active_devices
    }
    return [
        {
            "room_id": str(r.room_id),
            "label": f"{r.building_code}-{r.floor}F-{r.room_number}",
            "bed_count": r.bed_count,
            "status": r.status,
            "elders": elders_by_room.get(r.room_id, []),
            "device_code": devices_by_room.get(r.room_id),
        }
        for r in rooms
    ]


async def biometric_login(
    session: AsyncSession,
    *,
    room_id: UUID,
    mode: str,
    audio_samples: list[bytes] | None = None,
) -> dict:
    """演示登录：校验通过后为房间活跃设备签发真实设备 Token（复用 Day2 签发链路）。

    - mode=fingerprint：教学演示直接放行（无硬件，点击即进）；
    - mode=voiceprint：audio_samples 走 Day3 同款 CAMPPlus 提取 + 房间在住长者
      active 声纹余弦比对，取最高分且 ≥ voiceprint_threshold 才放行。
    """
    from app.auth.device_token import issue_access_token
    from app.models import Device, Room

    room = (
        await session.execute(select(Room).where(Room.room_id == room_id))
    ).scalar_one_or_none()
    if room is None:
        raise LookupError("房间不存在")
    device = (
        await session.execute(
            select(Device).where(
                Device.room_id == room_id, Device.status == DeviceStatus.ACTIVE
            )
        )
    ).scalars().first()
    if device is None:
        raise BiometricDeviceMissing("该房间没有可用设备，无法进入")
    elders = list(
        (
            await session.execute(
                select(Elder).where(
                    Elder.room_id == room_id, Elder.status == ElderStatus.ACTIVE
                )
            )
        ).scalars()
    )
    if not elders:
        raise LookupError("该房间没有在住长者")

    matched_elder: Elder | None = None
    score: float | None = None
    if mode == "voiceprint":
        provider = get_voiceprint_provider()
        if provider is None:
            raise ProviderUnavailable("声纹模型未配置（SHERPA_MODEL_DIR）")
        if not audio_samples:
            raise ValueError("缺少声纹样本")
        probe = await asyncio.to_thread(provider.extract_embedding, audio_samples[0])
        snapshots = await load_room_voiceprints(
            session, [e.elder_id for e in elders]
        )
        best: tuple[float, Elder] | None = None
        for elder in elders:
            for ref in snapshots.get(elder.elder_id, []):
                s = SherpaVoiceprintProvider.cosine(probe, ref)
                if best is None or s > best[0]:
                    best = (s, elder)
        threshold = get_settings().voiceprint_threshold
        if best is None or best[0] < threshold:
            detail = (
                f"声纹不匹配（相似度 {best[0]:.4f} < {threshold}）"
                if best
                else "该房间长者尚未录入声纹，请先用管理员通道录入"
            )
            raise VoiceprintMismatch(detail)
        score, matched_elder = best
    # mode=fingerprint：无硬件教学演示，直接放行（开关关闭时路由层已拦截）

    issued = await issue_access_token(session, device.device_id)
    await session.commit()
    logger.info(
        "biometric demo login",
        extra={
            "event": "biometric_demo_login",
            "mode": mode,
            "room_id": str(room_id),
            "device_id": str(device.device_id),
            "matched_elder_id": str(matched_elder.elder_id) if matched_elder else None,
            "score": round(score, 4) if score is not None else None,
        },
    )
    return {
        "access_token": issued.access_token,
        "expires_at": issued.expires_at.isoformat(),
        "room": f"{room.building_code}-{room.floor}F-{room.room_number}",
        "device_code": device.device_code,
        "matched_elder": matched_elder.name if matched_elder else None,
        "score": round(score, 4) if score is not None else None,
    }


# ---------------------------------------------------------------------------
# 语音会话状态机（每个 WS 连接一个实例）
# ---------------------------------------------------------------------------

SendMessage = Callable[[dict], Awaitable[None]]


@dataclass
class _ElderSnapshot:
    """房间内可声纹匹配的长者（建连时快照）。"""

    elder_id: UUID
    name: str
    embeddings: list[list[float]]


class VoiceSession:
    """单设备语音会话：能量端点 →（首句）声纹门控 →（后续）ASR→模板→TTS。"""

    def __init__(
        self,
        device_id: UUID,
        candidates: list[tuple[UUID, str]],  # (elder_id, name)
        embeddings: dict[UUID, list[list[float]]],
        send_event: SendMessage,
        embedding_reloader: Callable[[], Awaitable[dict[UUID, list[list[float]]]]]
        | None = None,
        db_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.device_id = device_id
        self.send_event = send_event
        # 会话逐轮落库与 LLM 历史读取（Day4）；测试/无库场景传 None，只跳过持久化
        self._db_factory = db_factory
        self._candidates = [
            _ElderSnapshot(eid, name, embeddings.get(eid, [])) for eid, name in candidates
        ]
        # 会话建立后才录入的声纹不在快照里：验证前允许重查一次库（录入后无需重连）
        self._embedding_reloader = embedding_reloader
        self._embeddings_reloaded = False
        self.buffer = bytearray()
        self._speech_start_ms: float | None = None
        self._silence_ms = 0.0
        self._speech_seen = False  # 当前缓冲内是否出现过语音（静音缓冲禁入 ASR）
        self._voiced_run_ms = 0.0  # 连续有能量帧累计（语音起始判定，防单帧噪声）
        self._voiced_ms = 0.0  # 当前缓冲累计有效语音时长（整句质量门）
        self._echo_guard_until = 0.0  # time.monotonic() 基准；此前上行音频视为 TTS 回声丢弃
        self._processing = False
        self.verified_elder_id: UUID | None = None
        self.verify_failures = 0
        self.closed = False
        # 早期摘要后台任务：同一会话同时只允许一个，关闭时取消
        self._summary_task: asyncio.Task | None = None

    # ---- 音频入口（realtime 推入，16k mono int16 PCM 60ms 帧）----

    def feed_pcm(self, pcm: bytes) -> None:
        if self.closed or self._processing:
            return
        if time.monotonic() < self._echo_guard_until:
            # TTS 回声防护窗内（数字人朗读正在播放）：上行音频必是扬声器回拾，直接丢弃。
            # Day4 实测事故：无此防线时形成「朗读→回拾→伪终句→再回复」自激循环。
            return
        self.buffer.extend(pcm)
        self._endpoint(pcm)

    def _endpoint(self, newest_pcm: bytes) -> None:
        """能量端点：说话开始记起点，静音超过阈值判定整句结束。"""
        import numpy as np

        samples = np.frombuffer(newest_pcm, dtype=np.int16)
        rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) if len(samples) else 0.0
        frame_ms = len(samples) / 16.0  # 16kHz
        max_buffer_bytes = int(MAX_BUFFER_MS / 1000 * 16000 * 2)

        if len(self.buffer) >= max_buffer_bytes:
            if not self._speech_seen:
                # 纯静音攒满缓冲（如 TTS 后无人说话）：直接丢弃，绝不进 ASR。
                # 实测事故：零音频 finish 后 FunASR 永不应答，会话卡死。
                self._take_buffer()
                return
            asyncio.get_running_loop().create_task(
                self._handle_utterance(self._take_buffer(), force=True)
            )
            return

        if rms >= ENERGY_THRESHOLD:
            self._voiced_run_ms += frame_ms
            self._voiced_ms += frame_ms
            if self._voiced_run_ms >= SPEECH_ONSET_MS:
                self._speech_seen = True  # 连续语音起始确认（单帧噪声不算）
            if self._speech_start_ms is None:
                self._speech_start_ms = 0.0
            self._silence_ms = 0.0
        else:
            self._voiced_run_ms = 0.0  # 连续性打断
            if self._speech_start_ms is not None:
                self._silence_ms += frame_ms
                if self._silence_ms >= SILENCE_MS_TO_CLOSE:
                    if self._voiced_ms >= MIN_SPEECH_MS:
                        asyncio.get_running_loop().create_task(
                            self._handle_utterance(self._take_buffer())
                        )
                    else:
                        # 有效语音不足一句（短促噪声段）：静默丢弃，不送 ASR。
                        # Day4 实测事故：此类段是 FunASR「对对对」伪终句的主要来源。
                        self._take_buffer()

    def _take_buffer(self) -> bytes:
        """同步取走缓冲并复位端点状态（必须在调度 flush 前调用，防重复调度竞态）。"""
        pcm = bytes(self.buffer)
        self.buffer.clear()
        self._speech_start_ms = None
        self._silence_ms = 0.0
        self._speech_seen = False
        self._voiced_run_ms = 0.0
        self._voiced_ms = 0.0
        return pcm

    async def _flush_utterance(self, force: bool = False) -> None:
        await self._handle_utterance(self._take_buffer(), force)

    async def _handle_utterance(self, pcm: bytes, force: bool = False) -> None:
        if len(pcm) < int(MIN_UTTERANCE_MS / 1000 * 16000 * 2) and not force:
            return
        if not pcm:
            return
        self._processing = True
        try:
            if self.verified_elder_id is None:
                await self._handle_verification(pcm)
            else:
                await self._handle_transcription(pcm)
        except Exception as exc:  # noqa: BLE001 —— 单句处理崩溃必须可见可恢复，绝不静默
            logger.error(
                "utterance processing crashed",
                extra={"event": "utterance_crashed", "error": type(exc).__name__ + ": " + str(exc)},
            )
            try:
                await self.send_event(
                    {
                        "type": "error",
                        "code": "utterance_failed",
                        "message": "本轮处理出错，请再说一次",
                    }
                )
            except Exception:  # noqa: BLE001 —— 连接已死时不再抛
                pass
        finally:
            self._processing = False

    # ---- 首句：声纹门控 ----

    async def _handle_verification(self, pcm: bytes) -> None:
        provider = get_voiceprint_provider()
        if provider is None:
            await self.send_event(
                {
                    "type": "error",
                    "code": "voiceprint_unavailable",
                    "message": "声纹服务未配置，请使用文字输入",
                }
            )
            return
        if not any(c.embeddings for c in self._candidates):
            if self._embedding_reloader is not None and not self._embeddings_reloaded:
                self._embeddings_reloaded = True
                fresh = await self._embedding_reloader()
                for c in self._candidates:
                    c.embeddings = fresh.get(c.elder_id, c.embeddings)
        if not any(c.embeddings for c in self._candidates):
            await self.send_event(
                {
                    "type": "error",
                    "code": "no_enrolled_voiceprint",
                    "message": "房间内长者尚未录入声纹",
                }
            )
            return

        try:
            embedding = await asyncio.to_thread(provider.extract_embedding, pcm)
        except (ValueError, ProviderUnavailable) as exc:
            await self.send_event(
                {"type": "verification", "accepted": False, "score": None, "reason": str(exc)}
            )
            return

        best: tuple[float, UUID] | None = None
        for candidate in self._candidates:
            for reference in candidate.embeddings:
                score = provider.cosine(embedding, reference)
                if best is None or score > best[0]:
                    best = (score, candidate.elder_id)

        assert best is not None
        score, elder_id = best
        threshold = get_settings().voiceprint_threshold
        passed = score >= threshold
        logger.info(
            "voiceprint verified",
            extra={
                "event": "voiceprint_verified",
                "device_id": str(self.device_id),
                "elder_id": str(elder_id),
                "score": round(score, 4),
                "accepted": passed,
                "identity_source": "voiceprint_match",
            },
        )
        if passed:
            self.verified_elder_id = elder_id
            token, ttl = voice_token_store.issue(self.device_id, elder_id, score)
            expires_at = datetime.now(UTC).timestamp() + ttl
            # 单条 verification 事件同时携带分数与免验 Token
            # （协议：accepted/score/token/expires_at）
            await self.send_event(
                {
                    "type": "verification",
                    "accepted": True,
                    "score": round(score, 4),
                    "token": token,
                    "expires_at": expires_at,
                    "elder_id": str(elder_id),
                }
            )
            await self.send_event({"type": "state", "state": "verified"})
        else:
            self.verify_failures += 1
            await self.send_event(
                {"type": "verification", "accepted": False, "score": round(score, 4)}
            )
            if self.verify_failures >= MAX_VERIFY_FAILURES:
                await self.send_event(
                    {
                        "type": "need_confirm",
                        "message": "连续多次声纹未通过，请在大屏选择长者",
                        "candidates": [c.name for c in self._candidates],
                    }
                )

    # ---- 后续句：ASR → 模板回复 → TTS ----

    async def _handle_transcription(self, pcm: bytes) -> None:
        trace = TurnTrace()
        trace.set_meta("device_id", str(self.device_id))
        trace.set_meta("elder_id", str(self.verified_elder_id) if self.verified_elder_id else None)
        asr: FunASRWSProvider
        try:
            asr = create_asr_session()
            await asr.start_session()
            trace.mark("asr_ready")
        except ProviderUnavailable:
            await self.send_event(
                {
                    "type": "error",
                    "code": "asr_unavailable",
                    "message": "语音识别服务未配置，请使用文字输入",
                }
            )
            return
        except OSError as exc:
            # FunASR 容器不可达：可见降级，不白屏不静默（验收项 5）
            logger.warning(
                "funasr unreachable",
                extra={"event": "funasr_unreachable", "error": str(exc)},
            )
            await self.send_event(
                {
                    "type": "error",
                    "code": "asr_unreachable",
                    "message": "语音识别服务暂不可用，请使用文字输入",
                }
            )
            return
        except WebSocketException as exc:
            # websockets 库异常（握手被拒/连接被关）不是 OSError，必须单独兜住
            logger.warning(
                "funasr ws error",
                extra={"event": "funasr_unreachable", "error": f"WebSocketException: {exc}"},
            )
            await self.send_event(
                {
                    "type": "error",
                    "code": "asr_unreachable",
                    "message": "语音识别服务连接失败，请稍后再说一次",
                }
            )
            return

        t0 = time.perf_counter()
        try:
            chunk = 16000 // 10  # 100ms 一块喂入
            for i in range(0, len(pcm), chunk * 2):
                await asr.feed_audio(pcm[i : i + chunk * 2])
            await asr.finish()

            final_text: str | None = None
            final_confidence = 0.0
            async for result in asr.results():
                if result.text:
                    await self.send_event(
                        {
                            "type": "transcript",
                            "text": result.text,
                            "mode": result.mode,
                            "is_final": result.is_final,
                            "confidence": round(result.confidence, 4),
                        }
                    )
                if result.is_final and "offline" in result.mode:
                    final_text = result.text
                    final_confidence = result.confidence
                    asr_e2e_ms = (time.perf_counter() - t0) * 1000
                    trace.mark("asr_final")
                    logger.info(
                        "asr final",
                        extra={
                            "event": "asr_final",
                            "elder_id": str(self.verified_elder_id),
                            "asr_confidence": round(final_confidence, 4),
                            "asr_e2e_ms": round(asr_e2e_ms, 1),
                            "mode": result.mode,
                            "asr_low_conf": final_confidence < LOW_CONFIDENCE,
                        },
                    )
                    break
        finally:
            await asr.close()

        if not final_text or not re.search(r"[\u4e00-\u9fff a-zA-Z0-9]", final_text):
            # 空文本或纯标点（如"。"）：视为没听清，不产生模板回复
            await self.send_event({"type": "done", "text": ""})
            trace.degrade("empty_transcript")
            trace.mark("done")
            trace.log()
            return

        reply = await self._reply_and_respond(final_text, trace)
        await self.send_event({"type": "done", "text": final_text, "reply": reply})
        trace.mark("done")
        trace.log()

    # ---- Day4：回复链（LLM 流式分句为主，模板 + 规则动作为降级）----

    async def _reply_and_respond(self, final_text: str, trace: TurnTrace | None = None) -> str:
        """终句 → 落库 → 回复生成 → 逐句 action/tts 事件；返回剥净标记的完整回复。

        LLM 未配置或失败时可见降级：模板回复 + 关键词规则动作（通话绝不中断）；
        LLM 已产出部分句子后中断，则沿用已说部分，不重复补模板。
        """
        identity = (
            IdentitySource.VOICEPRINT if self.verified_elder_id else IdentitySource.ANONYMOUS
        )
        if llm_is_configured():
            try:
                return await self._reply_via_llm(final_text, identity, trace)
            except LlmUnavailable as exc:
                if trace:
                    trace.degrade("llm_fallback")
                logger.warning(
                    "llm unavailable, fallback to template",
                    extra={"event": "llm_fallback", "error": str(exc)},
                )
        return await self._reply_via_template(final_text, identity, trace)

    async def _reply_via_llm(
        self,
        final_text: str,
        identity: IdentitySource,
        trace: TurnTrace | None = None,
    ) -> str:
        thread_id, history = await self._load_context(identity)
        if trace:
            trace.set_meta("thread_id", str(thread_id) if thread_id else None)
        context_text = await self._prepare_state(thread_id, final_text)
        system_content = _build_system_prompt()
        if context_text:
            system_content = system_content + "\n\n" + context_text
        messages: list[dict[str, str]] = [{"role": "system", "content": system_content}]
        messages += [{"role": m.role, "content": m.content} for m in history]
        messages.append({"role": "user", "content": final_text})

        clean_parts: list[str] = []
        try:
            await _collect_llm_sentences(
                messages,
                clean_parts,
                trace=trace,
                emit=lambda sentence: self._emit_sentence(sentence, trace),
            )
        except LlmUnavailable:
            partial = "".join(clean_parts).strip()
            if partial:  # 已播出一半：沿用部分回复，不再重说模板
                await self._save_turn(thread_id, final_text, partial)
                return partial
            raise
        reply = "".join(clean_parts).strip()
        if not reply:
            raise LlmUnavailable("LLM 空回复")
        await self._save_turn(thread_id, final_text, reply)
        return reply

    async def _reply_via_template(
        self,
        final_text: str,
        identity: IdentitySource,
        trace: TurnTrace | None = None,
    ) -> str:
        thread_id, _ = await self._load_context(identity)
        if trace:
            trace.set_meta("thread_id", str(thread_id) if thread_id else None)
        await self._prepare_state(thread_id, final_text)
        marked = action_service.apply_reply_rules(template_reply(final_text))
        reply = await self._emit_sentence(marked, trace)
        await self._save_turn(thread_id, final_text, reply)
        return reply

    async def _emit_sentence(self, sentence: str, trace: TurnTrace | None = None) -> str:
        """剥动作标记 → 先发 action 事件（先于语音）→ 再合成发送该句。"""
        cleaned, acts = action_service.parse_action_markers(sentence)
        for action_id, param in acts:
            await self.send_event({"type": "action", "action": action_id, "param": param})
        if cleaned.strip():
            await self._synthesize_and_send(cleaned, trace)
        return cleaned

    async def _load_context(
        self, identity: IdentitySource
    ) -> tuple[UUID | None, list]:
        """复用/新建会话线程并取最近 5 轮历史；任何库异常都降级为不落库、无历史。"""
        if self._db_factory is None:
            return None, []
        try:
            async with self._db_factory() as db:
                return await _load_thread(
                    db,
                    device_id=self.device_id,
                    elder_id=self.verified_elder_id,
                    identity=identity,
                )
        except Exception:  # noqa: BLE001 —— 历史缺失不阻断通话
            logger.exception("conversation context load failed")
            return None, []

    async def _save_turn(
        self, thread_id: UUID | None, user_text: str, assistant_text: str
    ) -> None:
        if thread_id is None or self._db_factory is None:
            return
        try:
            async with self._db_factory() as db:
                persisted = await _persist_turn(db, thread_id, user_text, assistant_text)
        except Exception:  # noqa: BLE001 —— 落库失败不影响通话
            logger.exception("conversation turn persist failed")
            return
        if persisted:
            self._schedule_summary(thread_id)

    async def _prepare_state(self, thread_id: UUID | None, final_text: str) -> str | None:
        return await _prepare_turn_state(thread_id, final_text)

    def _schedule_summary(self, thread_id: UUID) -> None:
        """把早期摘要放到后台任务，绑定当前 uvicorn 事件循环（禁止新建 loop）。"""
        if self._db_factory is None:
            return
        if self._summary_task is not None and not self._summary_task.done():
            return  # 同一会话同时只跑一个摘要任务，避免并发覆盖
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 同步测试或无运行循环：跳过
        self._summary_task = loop.create_task(self._refresh_summary(thread_id))

    async def _refresh_summary(self, thread_id: UUID) -> None:
        if self._db_factory is None:
            return
        try:
            async with self._db_factory() as db:
                state = await get_session_store().load(str(thread_id))
                await context_summary.refresh_summary(db, thread_id, state)
                await db.commit()
        except Exception:  # noqa: BLE001 —— 摘要失败保留旧内容，下一轮重试
            logger.exception("summary refresh failed")

    async def _synthesize_and_send(self, text: str, trace: TurnTrace | None = None) -> None:
        tts = get_tts_provider()
        settings = get_settings()
        if trace:
            trace.mark_once("tts_start")
        try:
            async with get_tts_breaker().guard():
                pcm = await asyncio.wait_for(
                    tts.synthesize(text), timeout=settings.tts_timeout_seconds
                )
        except (CircuitOpen, TimeoutError) as exc:
            if trace:
                trace.degrade(
                    "tts_circuit_open" if isinstance(exc, CircuitOpen) else "tts_timeout"
                )
            await self._send_tts_failure(text, exc)
            return
        except Exception as exc:  # noqa: BLE001 —— TTS 失败可见降级，不中断会话
            await self._send_tts_failure(text, exc)
            return
        if trace:
            trace.set_duration_once("tts_first_byte_ms", getattr(tts, "last_first_byte_ms", None))
            trace.set_duration_once("tts_decode_ms", getattr(tts, "last_decode_ms", None))
            trace.add_duration("tts_total_ms", getattr(tts, "last_total_ms", None))
            trace.incr("tts_sentences")
        # Day5.1：随 TTS 音频一起下发五口型时间轴；计算失败只丢口型，不阻断出声
        viseme_started = time.perf_counter()
        try:
            visemes = build_viseme_timeline(pcm, tts.sample_rate())
        except Exception:  # noqa: BLE001
            logger.exception("viseme timeline build failed")
            visemes = []
        if trace:
            trace.add_duration("viseme_ms", (time.perf_counter() - viseme_started) * 1000)
        await self.send_event(
            {
                "type": "tts",
                "text": text,
                "audio": base64.b64encode(pcm).decode("ascii"),
                "sample_rate": tts.sample_rate(),
                "visemes": visemes,
            }
        )
        if trace:
            trace.mark_once("tts_event_sent")
        # 回声防护窗：按本次音频播放时长 + 余量丢弃上行（前端播放期静音可能因
        # onended 不触发/AudioContext 挂起而漏窗，服务端必须自有防线）
        duration_ms = len(pcm) / (tts.sample_rate() * 2) * 1000  # int16 mono
        self._echo_guard_until = time.monotonic() + (
            duration_ms + TTS_ECHO_GUARD_EXTRA_MS
        ) / 1000

    async def _send_tts_failure(self, text: str, exc: Exception) -> None:
        logger.warning(
            "tts failed",
            extra={"event": "tts_failed", "error": str(exc)},
        )
        await self.send_event(
            {
                "type": "error",
                "code": "tts_failed",
                "message": "语音合成失败，已用文字回复",
                "reply_text": text,
            }
        )

    # ---- Token 免验续期（前端可携带上次 token 重连）----

    def try_resume(self, token: str) -> bool:
        record = voice_token_store.validate(token, self.device_id)
        if record is None:
            return False
        self.verified_elder_id = record.elder_id
        return True

    def close(self) -> None:
        self.closed = True
        if self._summary_task is not None and not self._summary_task.done():
            self._summary_task.cancel()


class VoiceSessionManager:
    """设备级会话注册表：realtime 音频按 device_id 找会话。"""

    def __init__(self) -> None:
        self._sessions: dict[UUID, VoiceSession] = {}

    def register(self, session: VoiceSession) -> None:
        old = self._sessions.get(session.device_id)
        if old is not None:
            old.close()
        self._sessions[session.device_id] = session

    def unregister(self, device_id: UUID) -> None:
        session = self._sessions.pop(device_id, None)
        if session is not None:
            session.close()

    def feed_pcm(self, device_id: UUID, pcm: bytes) -> None:
        session = self._sessions.get(device_id)
        if session is not None:
            session.feed_pcm(pcm)


voice_session_manager = VoiceSessionManager()
