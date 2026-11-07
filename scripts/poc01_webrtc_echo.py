"""POC-01：aiortc 本地单机 WebRTC 音频回声最小验证（Day3 步骤 1）。

参考来源：教师样板 C:/Users/yang/Desktop/asr 的 aiortc 用法（协议依据），
本脚本为一次性 POC 代码，不进入 app/ 正式模块。

验证目标：
1. 浏览器 getUserMedia 采集麦克风 → SDP offer → 服务端 aiortc answer；
2. 服务端把收到的音频轨原样回传 → 浏览器扬声器播放（听到自己回声）；
3. 记录 answer 生成耗时与首个音频帧到达时间，供回填 POC 文档。

关键教训（Day3 实施记录，写正式模块时必须遵守）：
- aiortc 的 on("track") 对 async 回调不等待完成，addTrack 必须在 createAnswer()
  之前显式完成（从 getTransceivers() 取 receiver.track 回传），否则 answer 的
  音频方向是 recvonly，浏览器永远收不到回传轨（Ontrack 不触发、无声）；
- MediaStreamTrack 没有 "frame" 事件，帧只能经 recv() 获取；诊断要用包装轨，
  不能另起消费者任务（会与 sender 分帧导致回声断续）；
- Windows 多虚拟网卡机器上 ICE 候选收集约 5s，信令往返慢，前端要做防连点/防竞态。

运行：uv run python scripts/poc01_webrtc_echo.py
访问：http://127.0.0.1:8601/  （localhost 属安全上下文，允许 getUserMedia）
"""

import time

from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamTrack
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI(title="POC-01 WebRTC Echo")

pcs: set[RTCPeerConnection] = set()


class EchoTrack(MediaStreamTrack):
    """包装轨：sender 每次 recv() 取帧时顺带计数，不与 sender 抢队列。"""

    kind = "audio"

    def __init__(self, source: MediaStreamTrack) -> None:
        super().__init__()
        self._source = source
        self.count = 0

    async def recv(self):
        frame = await self._source.recv()
        self.count += 1
        if self.count == 1:
            print(f"[POC-01] 首个音频帧到达服务端: pts={frame.pts} "
                  f"rate={frame.sample_rate} samples={frame.samples}")
        elif self.count % 250 == 0:  # Opus 每 20ms 一帧，250 帧 ≈ 5 秒
            print(f"[POC-01] 音频帧持续流动: 已收 {self.count} 帧")
        return frame


class OfferIn(BaseModel):
    sdp: str
    type: str


class AnswerOut(BaseModel):
    sdp: str
    type: str
    answer_ms: float


@app.post("/offer", response_model=AnswerOut)
async def offer(body: OfferIn) -> AnswerOut:
    """接收 SDP offer，创建回声 PC：收到的音频轨原样回传。"""
    t0 = time.perf_counter()
    pc = RTCPeerConnection()
    pcs.add(pc)
    offer_desc = RTCSessionDescription(sdp=body.sdp, type=body.type)

    def on_state() -> None:
        print(f"[POC-01] 连接状态: {pc.connectionState}")

    pc.on("connectionstatechange", on_state)

    # 先设远端描述：收发器（含 receiver.track）此时才创建
    await pc.setRemoteDescription(offer_desc)

    # 回声核心：createAnswer 之前显式把收到的轨道包一层回传，
    # 不依赖 on("track") 事件回调时序（async 回调不等待，见文件头教训）
    wired = False
    for transceiver in pc.getTransceivers():
        if transceiver.kind == "audio" and transceiver.receiver.track:
            pc.addTrack(EchoTrack(transceiver.receiver.track))
            transceiver.direction = "sendrecv"
            wired = True
            print(f"[POC-01] 回传轨已挂接: {transceiver.receiver.track.id}")
            break
    if not wired:
        print("[POC-01] ⚠️ 未找到可回传的音频收发器")

    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    answer_ms = (time.perf_counter() - t0) * 1000
    print(f"[POC-01] answer 已生成，耗时 {answer_ms:.0f} ms")
    return AnswerOut(
        sdp=pc.localDescription.sdp,
        type=pc.localDescription.type,
        answer_ms=answer_ms,
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    # POC 页面禁止缓存，避免改版后浏览器仍用旧 JS
    return HTMLResponse(content=PAGE, headers={"Cache-Control": "no-store"})


PAGE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>POC-01 WebRTC 回声</title>
<style>
  body { font-family: "Microsoft YaHei", sans-serif; max-width: 640px; margin: 40px auto; }
  button { font-size: 20px; padding: 12px 32px; margin-right: 12px; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  #status { font-size: 22px; font-weight: bold; margin: 20px 0; padding: 12px;
            border: 2px solid #ccc; border-radius: 8px; }
  #log { font-size: 14px; color: #555; white-space: pre-wrap; }
  audio { width: 100%; margin-top: 16px; }
</style>
</head>
<body>
  <h2>POC-01：WebRTC 音频回声（本地单机）</h2>
  <div>请佩戴耳机避免啸叫；点击「开始回声」后说话，能从扬声器听到自己的声音即为通过。</div>
  <div id="status">待开始</div>
  <button id="start" onclick="startEcho()">开始回声</button>
  <button id="stop" onclick="stopEcho()">停止</button>
  <button id="play" onclick="document.getElementById('remote').play()">手动播放</button>
  <audio id="remote" autoplay></audio>
  <div id="log"></div>
<script>
var pc = null, localStream = null, connecting = false;
var statusEl = document.getElementById("status");
var logEl = document.getElementById("log");
var startBtn = document.getElementById("start");
function log(msg) { logEl.textContent += msg + "\\n"; }
function setStatus(text, color) {
  statusEl.textContent = text;
  statusEl.style.borderColor = color || "#ccc";
}

async function startEcho() {
  if (connecting) { log("正在连接中，请稍候…"); return; }
  connecting = true;
  startBtn.disabled = true;
  try {
    setStatus("正在请求麦克风…", "#ef6c00");
    localStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false }
    });
    log("麦克风已开启: " + localStream.getAudioTracks()[0].label);

    var myPc = new RTCPeerConnection();
    pc = myPc;
    localStream.getTracks().forEach(function (t) { myPc.addTrack(t, localStream); });

    // aiortc 的 answer 可能不带 msid，e.streams[0] 会是 undefined，
    // 必须兜底用 e.track 手工构造 MediaStream
    myPc.ontrack = function (e) {
      log("收到回传轨道: " + e.track.kind +
          " | streams[0]=" + (e.streams && e.streams[0] ? "有" : "无(用e.track兜底)"));
      var stream = (e.streams && e.streams[0]) ? e.streams[0] : new MediaStream([e.track]);
      var el = document.getElementById("remote");
      el.srcObject = stream;
      el.play().then(function () {
        setStatus("回声进行中——请说话，戴耳机听", "#2e7d32");
        log("音频元素已开始播放");
      }).catch(function (err) {
        setStatus("浏览器拦截了自动播放，请点击「手动播放」按钮", "#ef6c00");
        log("play() 被拦截: " + err.message);
      });
    };
    myPc.onconnectionstatechange = function () {
      log("PC 状态: " + myPc.connectionState);
      if (myPc.connectionState === "failed") {
        setStatus("连接失败", "#c62828");
      }
    };

    var offer = await myPc.createOffer();
    await myPc.setLocalDescription(offer);
    var t0 = performance.now();
    var resp = await fetch("/offer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sdp: myPc.localDescription.sdp, type: myPc.localDescription.type })
    });
    var answer = await resp.json();
    log("信令往返耗时: " + (performance.now() - t0).toFixed(0) +
        " ms（含服务端 answer 生成 " + answer.answer_ms.toFixed(0) + " ms）");
    // 防竞态：等待期间被停止则不再继续
    if (myPc.signalingState === "closed") { log("期间已被停止，放弃本次连接"); return; }
    await myPc.setRemoteDescription({ sdp: answer.sdp, type: answer.type });
    setStatus("已接通，正在建立媒体…", "#ef6c00");
  } catch (err) {
    setStatus("出错: " + err.message, "#c62828");
    log("错误: " + err.message);
  } finally {
    connecting = false;
    startBtn.disabled = false;
  }
}

function stopEcho() {
  if (pc) { pc.close(); pc = null; }
  if (localStream) { localStream.getTracks().forEach(function (t) { t.stop(); }); localStream = null; }
  setStatus("已停止", "#ccc");
  log("已停止");
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8601, log_level="info")
