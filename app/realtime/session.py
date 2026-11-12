"""实时模块：aiortc WebRTC 会话管理（TDD §4 职责，Day3 建）。

⚠️ 已知坑（大纲课时 3.2 点名）：aiortc 的 asyncio loop 必须与 uvicorn 运行
loop 同一事件循环——本模块所有 PC/任务都在请求处理时创建、绑定当前 loop，
绝不自行 asyncio.new_event_loop 或起独立线程跑 aiortc。该规则若第三次踩坑
须写入 AGENTS.md（Day3 规划 §4.1）。

参考来源：教师样板 C:/Users/yang/Desktop/asr/src/asr/signaling.py 的
offer/answer 处理与轨接收模式；适配本项目改动：
- PC 按 device_id 键控（VoiceSessionManager 同键），重复 offer 关旧建新；
- 浏览器音频轨（Opus 48k stereo）经 PyAV 重采样为 16k mono s16 PCM，
  按 60ms 帧推给 voice 会话管理器（用户决策①：事件走同源 WS，音频走本轨）；
- 未配置 RTC_ICE_SERVERS 时仅用本地 host 候选（单机 POC 即可通）；
- 原始音频不落盘不入日志（TDD §4 observability 禁区）。
"""

import asyncio
import time
import uuid
from fractions import Fraction

import av
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.voice import voice_session_manager

logger = get_logger(__name__)

PCM_SAMPLE_RATE = 16000
FRAME_SAMPLES = 960  # 60ms @16kHz


def _rtc_configuration() -> RTCConfiguration:
    """ICE 服务器配置：RTC_ICE_SERVERS 为 JSON 数组字符串（教师 TURN 拓扑）。"""
    import json

    raw = get_settings().rtc_ice_servers
    if not raw:
        return RTCConfiguration(iceServers=[])
    try:
        servers = json.loads(raw)
        return RTCConfiguration(iceServers=servers)
    except (ValueError, TypeError):
        logger.warning(
            "rtc_ice_servers invalid, fallback to host candidates",
            extra={"event": "rtc_ice_servers_invalid"},
        )
        return RTCConfiguration(iceServers=[])


class RtcOfferIn(BaseModel):
    sdp: str
    type: str


class RtcAnswerOut(BaseModel):
    sdp: str
    type: str


class VoiceRealtimeSession:
    """单设备 PC：收轨 → 重采样 16k PCM → voice 会话管理器。"""

    def __init__(self, device_id) -> None:
        self.device_id = device_id
        self.pc = RTCPeerConnection(configuration=_rtc_configuration())
        self._push_task: asyncio.Task | None = None
        self._resampler: av.AudioResampler | None = None
        self._pcm_tail = bytearray()  # 不足一帧的残余样本

        def on_state() -> None:
            logger.info(
                "rtc state",
                extra={
                    "event": "rtc_state",
                    "device_id": str(device_id),
                    "state": self.pc.connectionState,
                },
            )

        self.pc.on("connectionstatechange", on_state)

        def on_track(track: MediaStreamTrack) -> None:
            logger.info(
                "rtc track received",
                extra={"event": "rtc_track", "device_id": str(device_id), "kind": track.kind},
            )
            if track.kind == "audio":

                async def _push_guarded() -> None:
                    try:
                        await self._push_audio(track)
                    except (asyncio.CancelledError, MediaStreamError):
                        pass  # PC 关闭属正常退出
                    except Exception as exc:  # noqa: BLE001 —— 任务级兜底，异常必须留痕
                        logger.error(
                            "rtc push crashed",
                            extra={
                                "event": "rtc_push_crashed",
                                "device_id": str(device_id),
                                "error": f"{type(exc).__name__}: {exc}",
                            },
                        )

                self._push_task = asyncio.create_task(_push_guarded())

        self.pc.on("track", on_track)

    async def accept_offer(self, sdp: str, type_: str) -> RtcAnswerOut:
        """设置远端 offer → 建收发器 → answer（不回传媒体，Day3 单向收音）。"""
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=type_))
        # 浏览器 offer 为 sendrecv 时，aiortc 默认 answer 会带收方向；
        # 显式降为 recvonly，杜绝意外发送（echo 由 POC 脚本单独验证）
        for transceiver in self.pc.getTransceivers():
            if transceiver.kind == "audio" and transceiver.direction == "sendrecv":
                transceiver.direction = "recvonly"
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)
        return RtcAnswerOut(sdp=self.pc.localDescription.sdp, type=self.pc.localDescription.type)

    async def _push_audio(self, track: MediaStreamTrack) -> None:
        """解码 Opus → 重采样 16k mono s16 → 按 60ms 帧推 voice 会话。

        注意：本协程在 uvicorn 事件循环内运行（loop 绑定铁律）。
        偶发坏帧/重采样异常不退出循环（否则上行永久静默、前端毫无反馈），
        仅在轨道关闭（MediaStreamError）或取消时结束。
        """
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=PCM_SAMPLE_RATE)
        frame_bytes = FRAME_SAMPLES * 2
        while True:
            try:
                frame = await track.recv()
                for resampled in self._resampler.resample(frame):
                    self._pcm_tail.extend(resampled.to_ndarray().astype("int16").tobytes())
                    while len(self._pcm_tail) >= frame_bytes:
                        pcm = bytes(self._pcm_tail[:frame_bytes])
                        del self._pcm_tail[:frame_bytes]
                        voice_session_manager.feed_pcm(self.device_id, pcm)
            except (asyncio.CancelledError, MediaStreamError):
                raise  # 正常关闭由调用方处理
            except Exception as exc:  # noqa: BLE001 —— 单帧异常：重置重采样器继续收流
                logger.warning(
                    "rtc frame error",
                    extra={
                        "event": "rtc_frame_error",
                        "device_id": str(self.device_id),
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                self._pcm_tail.clear()
                self._resampler = av.AudioResampler(
                    format="s16", layout="mono", rate=PCM_SAMPLE_RATE
                )
                await asyncio.sleep(0.05)

    async def close(self) -> None:
        if self._push_task is not None:
            self._push_task.cancel()
            try:
                await self._push_task
            except asyncio.CancelledError:
                pass
        await self.pc.close()


class RealtimeRegistry:
    """device_id → PC 会话；重复 offer 关旧建新（防幽灵连接堆积）。"""

    def __init__(self) -> None:
        self._sessions: dict[object, VoiceRealtimeSession] = {}

    async def replace(self, device_id, session: VoiceRealtimeSession) -> None:
        old = self._sessions.get(device_id)
        if old is not None:
            await old.close()
        self._sessions[device_id] = session

    async def close(self, device_id) -> None:
        session = self._sessions.pop(device_id, None)
        if session is not None:
            await session.close()


realtime_registry = RealtimeRegistry()


# ---------------------------------------------------------------------------
# Day7 段4：服务端主动外呼（独立 PC + 下行 TTS 音轨）
# ---------------------------------------------------------------------------


class OutboundCallError(RuntimeError):
    """外呼建连失败（超时/连接态 failed），调用方据此降级。"""


class TtsAudioTrack(MediaStreamTrack):
    """下行 TTS 音轨：recv() 按 20ms 返回 16k mono s16 帧（pts/time_base 正确）。

    音频只走内存，不落盘、不进日志；PCM 播完抛 MediaStreamError 结束轨。
    """

    kind = "audio"

    def __init__(self, pcm: bytes, sample_rate: int = PCM_SAMPLE_RATE) -> None:
        super().__init__()
        self._pcm = pcm if sample_rate == PCM_SAMPLE_RATE else _resample_to_16k(pcm, sample_rate)
        self._offset = 0
        self._pts = 0
        self._started_at: float | None = None

    async def recv(self) -> av.AudioFrame:
        if self._offset >= len(self._pcm):
            raise MediaStreamError
        chunk = self._pcm[self._offset : self._offset + FRAME_SAMPLES * 2]
        self._offset += len(chunk)
        if len(chunk) < FRAME_SAMPLES * 2:  # 末帧补齐，避免帧长不整
            chunk += b"\x00" * (FRAME_SAMPLES * 2 - len(chunk))

        samples = len(chunk) // 2
        frame = av.AudioFrame(format="s16", layout="mono", samples=samples)
        frame.planes[0].update(chunk)
        frame.sample_rate = PCM_SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = Fraction(1, PCM_SAMPLE_RATE)
        self._pts += samples
        await self._pace()
        return frame

    async def _pace(self) -> None:
        """按真实播放速率出帧（20ms/帧），避免一次性灌满抖动缓冲。"""
        now = time.monotonic()
        if self._started_at is None:
            self._started_at = now
        target = self._started_at + self._pts / PCM_SAMPLE_RATE
        delay = target - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)


def _resample_to_16k(pcm: bytes, sample_rate: int) -> bytes:
    """非 16k 的 TTS 输出统一重采样为 16k mono s16（内存内完成）。"""
    if sample_rate <= 0:
        return pcm
    frame = av.AudioFrame(format="s16", layout="mono", samples=len(pcm) // 2)
    frame.planes[0].update(pcm)
    frame.sample_rate = sample_rate
    resampler = av.AudioResampler(format="s16", layout="mono", rate=PCM_SAMPLE_RATE)
    out = bytearray()
    for resampled in resampler.resample(frame):
        out.extend(resampled.to_ndarray().astype("int16").tobytes())
    return bytes(out)


class OutboundCall:
    """一次服务端主动外呼：先 addTrack 再 createOffer，answer 由 WS 回传。"""

    def __init__(
        self,
        device_id,
        *,
        call_id: str | None = None,
        configuration: RTCConfiguration | None = None,
    ) -> None:
        self.device_id = device_id
        self.call_id = call_id or uuid.uuid4().hex
        self.pc = RTCPeerConnection(configuration=configuration or _rtc_configuration())
        self.answered = asyncio.Event()
        self.failed = asyncio.Event()
        self.audio_track: TtsAudioTrack | None = None
        self._closed = False

        def on_state() -> None:
            state = self.pc.connectionState
            logger.info(
                "rtc outbound state",
                extra={
                    "event": "rtc_outbound_state",
                    "device_id": str(self.device_id),
                    "call_id": self.call_id,
                    "state": state,
                },
            )
            if state in ("failed", "closed"):
                self.failed.set()
                self.answered.set()  # 让等待方立即返回并走降级

        self.pc.on("connectionstatechange", on_state)

    async def create_offer(self, pcm: bytes, sample_rate: int = PCM_SAMPLE_RATE) -> str:
        """addTrack 必须早于 createOffer（否则 offer 里没有下行媒体行）。"""
        self.audio_track = TtsAudioTrack(pcm, sample_rate)
        self.pc.addTrack(self.audio_track)
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        return self.pc.localDescription.sdp

    async def set_answer(self, sdp: str, type_: str = "answer") -> None:
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=type_))
        self.answered.set()

    async def wait_answered(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self.answered.wait(), timeout=timeout)
        except TimeoutError as exc:
            raise OutboundCallError("answer timeout") from exc
        if self.failed.is_set():
            raise OutboundCallError("connection failed")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.audio_track is not None:
            self.audio_track.stop()
        await self.pc.close()


class OutboundCallRegistry:
    """call_id → 外呼 PC；同一设备同一时刻只允许一个外呼。"""

    def __init__(self) -> None:
        self._calls: dict[str, OutboundCall] = {}

    def add(self, call: OutboundCall) -> None:
        self._calls[call.call_id] = call

    def get(self, call_id: str) -> OutboundCall | None:
        return self._calls.get(call_id)

    def find_by_device(self, device_id) -> OutboundCall | None:
        for call in self._calls.values():
            if call.device_id == device_id:
                return call
        return None

    async def close(self, call_id: str) -> None:
        call = self._calls.pop(call_id, None)
        if call is not None:
            await call.close()

    async def close_all(self) -> None:
        calls = list(self._calls.values())
        self._calls.clear()
        for call in calls:
            await call.close()


outbound_call_registry = OutboundCallRegistry()
