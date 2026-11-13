/* ============================================================
   家属端「今日日报」（Day8 8.3，原生 JS）
   - GET /api/v1/elders/{id}/health-daily-report（tts=1 附 PCM 朗读）
   - 字号 ≥20px；朗读不可用时按钮置灰并提示，不静默
   ============================================================ */

(function () {
  "use strict";

  var LEVEL_TAG = { green: "tag-moss", yellow: "tag-amber", red: "tag-lacquer" };
  var LEVEL_LABEL = { green: "平稳", yellow: "需留意", red: "需关注" };
  var audioCtx = null;

  function playPcm(base64, rate) {
    var bin = atob(base64);
    var bytes = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    var samples = new Int16Array(bytes.buffer, 0, Math.floor(bin.length / 2));
    var Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) throw new Error("no_audio_context");
    if (audioCtx && audioCtx.state === "suspended") audioCtx.resume();
    audioCtx = audioCtx || new Ctx({ sampleRate: rate });
    var buffer = audioCtx.createBuffer(1, samples.length, rate);
    var channel = buffer.getChannelData(0);
    for (var j = 0; j < samples.length; j++) channel[j] = samples[j] / 32768;
    var src = audioCtx.createBufferSource();
    src.buffer = buffer;
    src.connect(audioCtx.destination);
    src.start();
  }

  function mount(opts) {
    var root = document.getElementById(opts.rootId);
    if (!root) return null;

    root.innerHTML =
      "<div class='card'>" +
      "<div class='card-title'><h3>今日健康日报</h3>" +
      "<div class='toolbar'><select data-elder style='min-width:160px;'></select>" +
      "<button class='btn btn-ghost btn-sm' data-refresh>刷新</button></div></div>" +
      "<div data-body class='mt-8'></div></div>";

    var body = root.querySelector("[data-body]");
    var select = root.querySelector("[data-elder]");
    var state = { elderId: null, seq: 0, playing: false };

    function levelTag(level) {
      if (!level) return "";
      return "<span class='tag " + (LEVEL_TAG[level] || "") + "'>" +
        KB.esc(LEVEL_LABEL[level] || level) + "</span>";
    }

    function render(data, elderId) {
      var niceDate = String(data.date || "").replace(/-/g, "/");
      var pending = data.pending_events
        ? "　<span class='tag tag-amber'>待确认 " + data.pending_events + " 条</span>" : "";
      body.innerHTML =
        "<div class='report-card'>" +
        "<div class='report-head'><b>" + KB.esc(data.elder_name || "长者") + "</b>" +
        "<span class='faint'>" + KB.esc(niceDate) + "</span>" + levelTag(data.level) + pending + "</div>" +
        "<p class='report-text'>" + KB.esc(data.text) + "</p>" +
        "<div class='toolbar'>" +
        "<button class='btn btn-primary' data-play>朗读日报</button>" +
        "<span class='faint' data-play-tip></span>" +
        "</div></div>";

      var btn = body.querySelector("[data-play]");
      var tip = body.querySelector("[data-play-tip]");
      btn.addEventListener("click", function () {
        if (state.playing) return;
        state.playing = true;
        btn.disabled = true;
        tip.textContent = "正在合成语音…";
        // 闭包固定本次渲染的长者：避免切换后点朗读，文字与音频错配
        KB.api("GET", "/api/v1/elders/" + elderId + "/health-daily-report?tts=1")
          .then(function (res) {
            if (!res.audio_pcm_base64) {
              btn.disabled = true;
              tip.textContent = res.tts_degraded === "tts_unavailable"
                ? "语音服务暂不可用，请阅读文字日报"
                : "暂无朗读音频";
              return;
            }
            playPcm(res.audio_pcm_base64, res.audio_sample_rate || 24000);
            tip.textContent = "正在朗读…";
            btn.textContent = "重播";
            btn.disabled = false;
          })
          .catch(function (e) {
            tip.textContent = "朗读失败：" + KB.errText(e);
            btn.disabled = false;
          })
          .then(function () { state.playing = false; });
      });
    }

    function load(elderId) {
      if (!elderId) return;
      state.elderId = elderId;
      var seq = ++state.seq; // 竞态守卫：切换长者后，晚到的旧响应直接丢弃
      KB.renderState(body, { kind: "loading" });
      KB.api("GET", "/api/v1/elders/" + elderId + "/health-daily-report")
        .then(function (data) {
          if (seq !== state.seq) return;
          render(data, elderId);
        })
        .catch(function (e) {
          if (seq !== state.seq) return;
          var msg = e.status === 403 ? "暂无健康数据授权，请联系管理员开通" : KB.errText(e);
          KB.renderState(body, { kind: "error", message: msg, traceId: e.traceId }, function () {
            load(state.elderId);
          });
        });
    }

    select.addEventListener("change", function () { load(select.value); });
    root.querySelector("[data-refresh]").addEventListener("click", function () { load(state.elderId); });

    KB.api("GET", "/api/v1/elders")
      .then(function (rows) {
        if (!rows || !rows.length) {
          select.innerHTML = "<option value=''>暂无关联长者</option>";
          KB.renderState(body, { kind: "empty", message: "暂无关联长者" });
          return;
        }
        select.innerHTML = rows.map(function (e) {
          return "<option value='" + e.elder_id + "'>" + KB.esc(e.name) + "</option>";
        }).join("");
        load(rows[0].elder_id);
      })
      .catch(function (e) {
        KB.renderState(body, { kind: "error", message: KB.errText(e), traceId: e.traceId }, function () {
          window.location.reload();
        });
      });

    return { refresh: function () { load(state.elderId); } };
  }

  window.KangbanDaily = { mount: mount };
})();
