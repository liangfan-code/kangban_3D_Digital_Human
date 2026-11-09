/**
 * 康伴 Day3 语音测试台主逻辑。
 *
 * 链路（Day3 规划 §3.1，用户决策①②）：
 * - 音频：getUserMedia → WebRTC（POST /api/v1/rtc/offer，设备 Token 鉴权）；
 * - 事件：同源 WS /api/v1/voice/ws（首条消息 auth，JSON 事件流）；
 * - 声纹：首句服务端 TEN-VAD + CAMPPlus 验证 → verification 事件 → 300s 免验；
 * - TTS：tts 事件（base64 16k PCM）→ AudioContext 播放；
 * - 降级：error 事件 → 可见提示 + 文字通道（验收项 5，不白屏不静默）。
 */
(function () {
  "use strict";

  // ---------- DOM ----------
  var $ = function (id) { return document.getElementById(id); };
  var statusEl = $("status"), scoreEl = $("score"), badge = $("conn-badge");
  var logAsr = $("log-asr"), logReply = $("log-reply");
  var loginCard = $("login-card"), voiceCard = $("voice-card"), fallbackCard = $("fallback-card");

  // ---------- 状态 ----------
  var deviceToken = sessionStorage.getItem("kb_device_token") || "";
  var ws = null, pc = null, micStream = null;
  var lastVoiceToken = null; // verification 通过后下发，重连时 resume 免验
  var audioCtx = null, ttsQueue = [], ttsPlaying = false;
  var avatarDest = null; // Day4 口型桥：TTS → MediaStreamDestination → 3D 数字人
  var connected = false;

  function setStatus(text, cls) {
    statusEl.textContent = text;
    statusEl.className = "voice-status" + (cls ? " " + cls : "");
  }
  function setBadge(text, ok) {
    badge.innerHTML = '<span class="dot"></span>' + text;
    badge.style.borderColor = ok === true ? "#2e7d32" : ok === false ? "#c62828" : "#ccc";
  }
  function appendLog(el, text, cls) {
    if (el.textContent === "—") el.textContent = "";
    var line = document.createElement("div");
    if (cls) line.className = cls;
    line.textContent = text;
    el.appendChild(line);
    el.scrollTop = el.scrollHeight;
  }
  function showFallback(reason) {
    fallbackCard.style.display = "";
    $("fallback-reason").textContent = reason;
  }
  function hideFallback() { fallbackCard.style.display = "none"; }

  // ---------- WS 事件通道 ----------
  function connectWS(token, onReady) {
    var proto = location.protocol === "https:" ? "wss://" : "ws://";
    ws = new WebSocket(proto + location.host + "/api/v1/voice/ws");
    ws.onopen = function () {
      ws.send(JSON.stringify({ type: "auth", token: token }));
    };
    ws.onmessage = function (e) {
      var ev;
      try { ev = JSON.parse(e.data); } catch (err) { return; }
      handleEvent(ev, onReady);
    };
    ws.onclose = function (e) {
      connected = false;
      setBadge("连接关闭(" + e.code + ")", false);
      setStatus("连接已关闭：" + (e.reason || e.code), "err");
      if (e.code === 4403) appendLog(logAsr, "[鉴权失败] 人 JWT 不能用于设备通道", "e");
      else if (e.code === 4401) appendLog(logAsr, "[鉴权失败] 设备 Token 无效", "e");
    };
    ws.onerror = function () { setBadge("连接异常", false); };
  }

  function handleEvent(ev, onReady) {
    switch (ev.type) {
      case "ready":
        connected = true;
        setBadge("已连接 · " + (ev.mode === "single" ? "单人间" : "双人间"), true);
        setStatus(ev.voiceprint_ready
          ? "已就绪，请说第一句话完成声纹验证"
          : "已就绪（本房间长者尚未录入声纹，可点「录入声纹」）", "warn");
        if (window.KangbanAvatar) KangbanAvatar.setState("in_call");
        if (lastVoiceToken) ws.send(JSON.stringify({ type: "resume", token: lastVoiceToken }));
        if (onReady) onReady();
        break;
      case "verification":
        if (ev.accepted) {
          lastVoiceToken = ev.token || lastVoiceToken;
          scoreEl.textContent = "声纹验证通过 · 相似度 " + ev.score;
          setStatus("声纹通过！请继续说话", "ok");
        } else if (ev.score == null && ev.reason) {
          // Provider 侧异常（如 TEN-VAD 未检测到语音）：如实提示原因，不算"未通过"
          scoreEl.textContent = "本次未采集到有效语音";
          setStatus(ev.reason + "——请靠近设备、开口说一句话", "warn");
        } else {
          scoreEl.textContent = "声纹未通过" + (ev.score != null ? " · 相似度 " + ev.score : "");
          setStatus("声纹未通过，请再说一次（靠近设备）", "warn");
        }
        break;
      case "state":
        if (ev.state === "verified") setStatus("身份已确认，请说话", "ok");
        if (ev.state === "awaiting_voiceprint") setStatus("免验已过期，请再说一句完成验证", "warn");
        break;
      case "transcript":
        // 该 FunASR 镜像不返回置信度（恒为 0）——显示 "—" 而非误导性的 0
        appendLog(logAsr, (ev.is_final ? "【终】" : "【部分】") + ev.text +
          " (" + ev.mode + (ev.confidence > 0 ? " 置信度 " + ev.confidence : "") + ")",
          ev.is_final ? "t" : "");
        break;
      case "action":
        // Day4：动作标记事件（服务端剥净后单独下发，不朗读不进字幕正文）
        appendLog(logReply, "🎬 动作 " + ev.action + (ev.param ? " ×" + ev.param : ""), "r");
        if (window.KangbanAvatar && KangbanAvatar.ready) {
          KangbanAvatar.playAction(ev.action, ev.param || null);
        } else {
          appendLog(logReply, "（3D 未就绪，动作跳过——语音链路不受影响）", "");
        }
        break;
      case "tts":
        appendLog(logReply, "🔊 " + ev.text, "r");
        playTts(ev.audio, ev.sample_rate, ev.visemes);
        break;
      case "done":
        if (!ev.text) { setStatus("没有听清，请再说一次", "warn"); }
        else setStatus("本轮完成，请继续说话", "ok");
        break;
      case "need_confirm":
        setStatus("声纹连续未通过，请在下方选择身份", "err");
        var box = $("confirm-box"), wrap = $("confirm-candidates");
        box.style.display = "";
        wrap.innerHTML = "";
        (ev.candidates || []).forEach(function (name) {
          var b = document.createElement("button");
          b.className = "btn btn-ghost";
          b.textContent = name;
          b.onclick = function () {
            box.style.display = "none";
            setStatus("已选择「" + name + "」，请管理员重新录入声纹后重试", "warn");
            appendLog(logAsr, "[主动确认] 已选择长者：" + name, "e");
          };
          wrap.appendChild(b);
        });
        break;
      case "error":
        setStatus("⚠️ " + ev.message, "err");
        appendLog(logReply, "[降级] " + ev.message, "e");
        if (ev.code === "asr_unavailable" || ev.code === "asr_unreachable" ||
            ev.code === "voiceprint_unavailable") {
          showFallback(ev.message + "（语音链路中断时可在此使用文字输入）");
        }
        if (ev.reply_text) appendLog(logReply, "💬 " + ev.reply_text, "r");
        break;
      case "pong":
        break;
      default:
        break;
    }
  }

  // ---------- TTS 播放（base64 16k PCM + viseme 时间轴 → AudioContext）----------
  function playTts(base64Pcm, sampleRate, visemes) {
    var bin = atob(base64Pcm);
    var bytes = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    ttsQueue.push({
      bytes: bytes,
      rate: sampleRate || 16000,
      visemes: Array.isArray(visemes) ? visemes : [],
      // Day5.2：前端播放起点观测（仅 console.debug，不上报服务端）
      receivedAt: performance.now()
    });
    drainTts();
  }
  function drainTts() {
    if (ttsPlaying || ttsQueue.length === 0) return;
    ttsPlaying = true;
    var item = ttsQueue.shift();
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.resume) audioCtx.resume(); // 自动播放策略可能挂起上下文→onended 永不触发
    var buf = audioCtx.createBuffer(1, item.bytes.length / 2, item.rate);
    var ch = buf.getChannelData(0);
    var view = new DataView(item.bytes.buffer);
    for (var i = 0; i < ch.length; i++) ch[i] = view.getInt16(i * 2, true) / 32768;
    var src = audioCtx.createBufferSource();
    src.buffer = buf;
    src.connect(audioCtx.destination);
    // Day4 口型桥：老师引擎吃 WebRTC 远端流（AnalyserNode），本项目 TTS 走本地
    // AudioContext——用 MediaStreamDestination 把播放流喂给引擎（与扬声出声并行）。
    // 引擎未就绪则跳过，不影响出声（降级底线）。
    if (window.KangbanAvatar && KangbanAvatar.ready) {
      if (!avatarDest) {
        avatarDest = audioCtx.createMediaStreamDestination();
        KangbanAvatar.attachStream(avatarDest.stream);
      }
      src.connect(avatarDest);
    }
    // 播放期间静音麦克上行，防止扬声器朗读被麦克风收录成"回声转写"
    // ⚠️ onended 在浏览器上不可靠（上下文挂起时不触发）→ 若只依赖它恢复，
    // 麦克风会永久静音（"后面说话没反应"）。必须加超时兜底，两者先到先用。
    var unmuted = false;
    function unmute() {
      if (unmuted) return;
      unmuted = true;
      if (window.KangbanAvatar && KangbanAvatar.clearVisemes) KangbanAvatar.clearVisemes();
      if (micStream) micStream.getAudioTracks().forEach(function (t) { t.enabled = true; });
      ttsPlaying = false;
      drainTts();
    }
    if (micStream) micStream.getAudioTracks().forEach(function (t) { t.enabled = false; });
    src.onended = unmute;
    setTimeout(unmute, (ch.length / item.rate) * 1000 + 800); // 播放时长+800ms 兜底
    console.debug("[tts] play_start", performance.now() - item.receivedAt);
    src.start();
    // Day5.1：按该段音频的五口型时间轴驱动嘴部；无时间轴时引擎回落到能量分析
    if (window.KangbanAvatar && KangbanAvatar.attachVisemes) {
      KangbanAvatar.attachVisemes(item.visemes);
    }
  }

  // ---------- WebRTC 音频上行 ----------
  function startVoice() {
    if (!connected) { setStatus("请先连接语音", "warn"); return; }
    if (pc) { setStatus("对话进行中", "ok"); return; }
    navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
    }).then(function (stream) {
      micStream = stream;
      pc = new RTCPeerConnection(); // 本地 host 候选；跨机器时由服务端注入 ICE
      stream.getTracks().forEach(function (t) { pc.addTrack(t, stream); });
      return pc.createOffer().then(function (offer) {
        return pc.setLocalDescription(offer).then(function () {
          return fetch("/api/v1/rtc/offer", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Authorization: "Bearer " + deviceToken
            },
            body: JSON.stringify({ sdp: pc.localDescription.sdp, type: pc.localDescription.type })
          });
        });
      }).then(function (resp) {
        if (resp.status === 403) throw new Error("设备 Token 无权访问 /rtc/offer");
        if (resp.status === 401) throw new Error("设备 Token 已失效，请重新连接");
        if (!resp.ok) throw new Error("信令失败 HTTP " + resp.status);
        return resp.json();
      }).then(function (answer) {
        setStatus("语音通道建立中…", "warn");
        return pc.setRemoteDescription({ sdp: answer.sdp, type: answer.type });
      }).then(function () {
        setStatus("🎤 正在聆听（首句将做声纹验证）…", "warn");
      });
    }).catch(function (err) {
      setStatus("语音启动失败：" + err.message, "err");
      stopVoice();
    });
  }

  function stopVoice() {
    if (pc) { pc.close(); pc = null; }
    if (micStream) { micStream.getTracks().forEach(function (t) { t.stop(); }); micStream = null; }
    if (window.KangbanAvatar && KangbanAvatar.clearVisemes) KangbanAvatar.clearVisemes();
    setStatus("已停止聆听", "");
  }

  // ---------- 声纹录入（按有效语音采集：每段说满 2 秒自动停，失败段可重录）----------
  // channel: "device"（单人间自动锁定）| "admin"（双人间：管理员指定 elder_id，ADR-005）
  var ENROLL_TARGET_VOICED = 2.0;   // 每段目标有效语音秒数
  var ENROLL_MIN_VOICED = 1.2;      // 低于该值该段作废
  var ENROLL_MAX_SECONDS = 8.0;     // 单段最长等待，防卡死
  var ENROLL_SEGMENTS = 2;          // 与后端「至少 2 段有效样本」对应
  var ENROLL_MAX_ATTEMPTS = 3;      // 单段最多重录次数
  var ENROLL_VOICED_RMS = 0.02;     // 有效语音判定（Float32 RMS）

  function rmsOf(chunk) {
    var sum = 0;
    for (var i = 0; i < chunk.length; i++) sum += chunk[i] * chunk[i];
    return chunk.length ? Math.sqrt(sum / chunk.length) : 0;
  }

  function peakRatio(int16arr) {
    var peak = 0;
    for (var i = 0; i < int16arr.length; i++) {
      var v = Math.abs(int16arr[i]);
      if (v > peak) peak = v;
    }
    return peak / 32767;
  }

  function captureOneSegment(node, ctx, index, attempt) {
    var attemptNo = attempt || 1;
    return new Promise(function (resolve, reject) {
      var samples = [], voiced = 0, elapsed = 0, lastShown = -1, done = false;
      function settle() {
        done = true;
        node.port.onmessage = null;
        var pcm = downsampleToInt16(flatten(samples), ctx.sampleRate);
        var peak = peakRatio(pcm);
        if (voiced < ENROLL_MIN_VOICED || peak < ENROLL_VOICED_RMS) {
          if (attemptNo >= ENROLL_MAX_ATTEMPTS) {
            reject(new Error("第 " + (index + 1) + " 段有效语音太少，请靠近麦克风、正常音量连续说 2 秒"));
            return;
          }
          setStatus("第 " + (index + 1) + " 段有效语音不足，请重录（第 " + (attemptNo + 1) + " 次尝试）", "warn");
          setTimeout(function () { resolve(captureOneSegment(node, ctx, index, attemptNo + 1)); }, 600);
          return;
        }
        if (peak > 0.9) appendLog(logReply, "[声纹] 第 " + (index + 1) + " 段音量偏大，可能削波", "warn");
        resolve(pcm);
      }
      node.port.onmessage = function (e) {
        if (done) return;
        var chunk = e.data;
        samples.push(chunk);
        elapsed += chunk.length / ctx.sampleRate;
        if (rmsOf(chunk) >= ENROLL_VOICED_RMS) voiced += chunk.length / ctx.sampleRate;
        var shown = Math.floor(voiced * 10) / 10;
        if (shown !== lastShown) {
          lastShown = shown;
          setStatus("第 " + (index + 1) + "/" + ENROLL_SEGMENTS + " 段：有效语音 "
            + shown.toFixed(1) + "/" + ENROLL_TARGET_VOICED.toFixed(1)
            + " 秒（最多等 " + ENROLL_MAX_SECONDS + " 秒）", "warn");
        }
        if (voiced >= ENROLL_TARGET_VOICED || elapsed >= ENROLL_MAX_SECONDS) settle();
      };
    });
  }

  function captureAllSegments(node, ctx) {
    var segments = [], index = 0;
    function next() {
      if (index >= ENROLL_SEGMENTS) return Promise.resolve(segments);
      var current = index;
      return captureOneSegment(node, ctx, current).then(function (pcm) {
        segments.push(pcm);
        index += 1;
        if (index < ENROLL_SEGMENTS) {
          setStatus("✅ 第 " + (current + 1) + " 段完成，请再念一遍「今天天气真好，我很开心」", "warn");
        }
        return next();
      });
    }
    return next();
  }

  function enroll(channel) {
    if (!deviceToken) { setStatus("请先设备登录", "warn"); return; }
    var adminToken = "", elderId = "";
    if (channel === "admin") {
      adminToken = ($("admin-token") || {}).value || "";
      elderId = (($("admin-elder") || {}).value || "").trim();
      if (!adminToken || !elderId) {
        setStatus("管理员通道：请填写管理员 JWT 和长者 elder_id", "err"); return;
      }
    }
    setStatus("声纹录入：点击后请【立刻开始念】「今天天气真好，我很开心」，每段说满 2 秒自动结束，共 2 段", "warn");
    var activeStream = null, activeCtx = null, activeSrc = null;
    function stopCapture() {
      if (activeSrc) { try { activeSrc.disconnect(); } catch (e) {} activeSrc = null; }
      if (activeStream) { activeStream.getTracks().forEach(function (t) { t.stop(); }); activeStream = null; }
      if (activeCtx) { try { activeCtx.close(); } catch (e) {} activeCtx = null; }
    }
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      activeStream = stream;
      var ctx = new (window.AudioContext || window.webkitAudioContext)();
      activeCtx = ctx;
      return ctx.audioWorklet.addModule("/js/audio-worklet.js").then(function () {
        activeSrc = ctx.createMediaStreamSource(stream);
        var node = new AudioWorkletNode(ctx, "capture-processor");
        activeSrc.connect(node); // 不连 destination，避免回放啸叫
        return captureAllSegments(node, ctx);
      }).then(function (segments) {
        stopCapture();
        var body = {
          audio_samples: segments.map(function (seg) { return toBase64(seg); }),
          sample_uri: null
        };
        var url, headers;
        if (channel === "admin") {
          url = "/api/v1/voice/enroll/admin";
          headers = { "Content-Type": "application/json", Authorization: "Bearer " + adminToken };
          body.elder_id = elderId;
        } else {
          url = "/api/v1/voice/enroll/device";
          headers = { "Content-Type": "application/json", Authorization: "Bearer " + deviceToken };
        }
        setStatus("声纹上传中…", "warn");
        return fetch(url, {
          method: "POST",
          headers: headers,
          body: JSON.stringify(body)
        }).then(function (resp) { return resp.json().then(function (j) { return { ok: resp.ok, j: j }; }); });
      }).then(function (r) {
        if (r.ok) {
          setStatus("✅ 声纹录入成功，请说第一句话验证", "ok");
          appendLog(logReply, "[声纹] 录入成功 id=" + r.j.voiceprint_id, "r");
        } else {
          var msg = (r.j && r.j.error && r.j.error.message) || r.j.detail || JSON.stringify(r.j);
          setStatus("声纹录入失败：" + msg, "err");
        }
      });
    }).catch(function (err) {
      stopCapture();
      setStatus("声纹录入失败：" + err.message, "err");
    });
  }

  function flatten(arrs) {
    var total = arrs.reduce(function (s, a) { return s + a.length; }, 0);
    var out = new Float32Array(total), off = 0;
    arrs.forEach(function (a) { out.set(a, off); off += a.length; });
    return out;
  }
  function downsampleToInt16(f32, fromRate) {
    var toRate = 16000, ratio = fromRate / toRate;
    var n = Math.floor(f32.length / ratio);
    var out = new Int16Array(n);
    for (var i = 0; i < n; i++) {
      var v = f32[Math.floor(i * ratio)];
      out[i] = Math.max(-32768, Math.min(32767, Math.round(v * 32767)));
    }
    return out;
  }
  function toBase64(int16arr) {
    var bytes = new Uint8Array(int16arr.buffer);
    var bin = "";
    var CHUNK = 0x8000;
    for (var i = 0; i < bytes.length; i += CHUNK) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
    }
    return btoa(bin);
  }

  // ---------- 事件绑定 ----------
  $("btn-login").addEventListener("click", function () {
    deviceToken = $("device-token").value.trim();
    if (!deviceToken) { setStatus("请粘贴设备 Token", "warn"); return; }
    sessionStorage.setItem("kb_device_token", deviceToken);
    setStatus("连接中…", "warn");
    connectWS(deviceToken, function () {
      loginCard.style.display = "none";
      voiceCard.style.display = "";
      hideFallback();
    });
  });
  if (deviceToken) $("device-token").value = deviceToken;

  $("btn-voice").addEventListener("click", startVoice);
  $("btn-stop").addEventListener("click", stopVoice);
  $("btn-enroll").addEventListener("click", function () { enroll("device"); });
  $("btn-enroll-admin").addEventListener("click", function () { enroll("admin"); });
  $("btn-text-send").addEventListener("click", sendText);
  $("text-input").addEventListener("keydown", function (e) {
    if (e.key === "Enter") $("btn-text-send").click();
  });

  /**
   * 文字通道（Day5）：ASR 不可用时的可见降级。
   * 走 POST /voice/text（设备 Token），服务端复用语音会话线程与 LLM 链路，
   * 只回文字；模型故障时返回模板兜底并标 degraded，前端如实提示。
   */
  function sendText() {
    var v = $("text-input").value.trim();
    if (!v) return;
    if (!deviceToken) { setStatus("请先连接语音（文字通道需要设备 Token）", "warn"); return; }
    $("text-input").value = "";
    appendLog($("log-text"), "（我）" + v, "t");
    var btn = $("btn-text-send");
    btn.disabled = true;
    fetch("/api/v1/voice/text", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer " + deviceToken
      },
      body: JSON.stringify({ text: v })
    }).then(function (resp) {
      if (resp.status === 403) throw new Error("设备 Token 无权使用文字通道");
      if (resp.status === 401) throw new Error("设备 Token 已失效，请重新连接");
      if (!resp.ok) throw new Error("文字通道失败 HTTP " + resp.status);
      return resp.json();
    }).then(function (data) {
      appendLog($("log-text"), "（康伴）" + data.reply, "r");
      if (data.degraded) appendLog($("log-text"), "[降级] 模型暂不可用，已用兜底回复", "e");
    }).catch(function (err) {
      appendLog($("log-text"), "[失败] " + err.message, "e");
    }).then(function () {
      btn.disabled = false;
    });
  }
})();
