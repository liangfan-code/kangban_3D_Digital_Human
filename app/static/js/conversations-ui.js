/* ============================================================
   康伴 · 历史对话面板（对话历史可视化 段 5）
   - 门禁：未声纹确认（无 voice token）→ 遮罩「请先说出您的名字确认身份」
   - 确认后：会话列表（新旧并列、当前高亮、收起会话可「继续对话」）
   - 点击会话展开消息气泡（user 右 / assistant 左），「加载更早」游标分页
   - 后端契约：/api/v1/conversations*（错误统一 {"error":{code,message,trace_id}}）
   - 不改 api.js 共享层语义：请求走本地 histApi（需附加 X-Voice-Token 头），
     渲染复用 KB.esc / KB.renderState
   ============================================================ */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var bodyEl = $("history-body"), maskEl = $("history-mask");
  var listEl = $("history-list");

  // 设备凭证读取与 voice.js 同源（session → localStorage → 兼容 kb_token）
  function getDeviceToken() {
    return sessionStorage.getItem("kb_device_token") ||
           localStorage.getItem("kb_device_token") ||
           sessionStorage.getItem("kb_token") || "";
  }

  var voiceToken = null;

  /** 会话接口请求：设备 Bearer + X-Voice-Token 门禁头，非 2xx 抛 KB 风格错误。 */
  function histApi(method, path, body) {
    var headers = { Accept: "application/json" };
    var token = getDeviceToken();
    if (token) headers["Authorization"] = "Bearer " + token;
    if (voiceToken) headers["X-Voice-Token"] = voiceToken;
    var opts = { method: method, headers: headers };
    if (body !== undefined && body !== null) {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    return fetch(path, opts).then(function (resp) {
      if (resp.status === 204) return null;
      return resp.json().catch(function () { return null; }).then(function (data) {
        if (resp.ok) return data;
        var err = (data && data.error) || {};
        var e = new Error(err.message || "请求失败（HTTP " + resp.status + "）");
        e.status = resp.status;
        e.code = err.code || (resp.status === 403 ? "identity_locked" : "");
        e.traceId = err.trace_id || resp.headers.get("X-Trace-ID") || "";
        throw e;
      });
    });
  }

  // ---------- 时间格式 ----------
  function fmtDate(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "—";
    var now = new Date();
    var sameYear = d.getFullYear() === now.getFullYear();
    return (sameYear ? "" : d.getFullYear() + "年") +
      (d.getMonth() + 1) + "月" + d.getDate() + "日";
  }
  function fmtTime(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "—";
    function p(n) { return n < 10 ? "0" + n : "" + n; }
    return p(d.getHours()) + ":" + p(d.getMinutes());
  }
  function fmtDateTime(iso) {
    return fmtDate(iso) + " " + fmtTime(iso);
  }

  // ---------- 门禁遮罩 ----------
  function showMask() {
    maskEl.style.display = "";
    bodyEl.style.display = "none";
  }
  function hideMask() {
    maskEl.style.display = "none";
    bodyEl.style.display = "";
  }

  // ---------- 列表渲染 ----------
  function statusTag(c) {
    if (c.status === "ACTIVE") return "<span class='hist-tag hist-tag-active'>进行中</span>";
    return "<span class='hist-tag'>已收起</span>";
  }

  /** 消息气泡：user 靠右（陶色），assistant 靠左（卡片色）。 */
  function bubbleHtml(m) {
    var side = m.role === "user" ? "hist-bubble-user" : "hist-bubble-assistant";
    return "<div class='hist-row " + side.replace("hist-bubble-", "hist-row-") + "'>" +
      "<div class='hist-bubble " + side + "'>" + KB.esc(m.content) +
      "<div class='hist-bubble-time'>" + KB.esc(fmtDateTime(m.created_at)) + "</div></div></div>";
  }

  /** 渲染消息区（items 正序全量重绘；has_more 时挂「查看更早」游标分页）。 */
  function renderMsgs(conv, container, items, hasMore) {
    if (!items.length) {
      container.innerHTML = "<div class='hist-empty'>这段对话还没有内容</div>";
      return;
    }
    var moreBtn = hasMore
      ? "<div class='hist-more-wrap'><button class='btn btn-ghost btn-sm hist-more'>查看更早消息</button></div>"
      : "";
    container.innerHTML = items.map(bubbleHtml).join("") + moreBtn;
    var more = container.querySelector(".hist-more");
    if (more) {
      more.addEventListener("click", function () {
        more.disabled = true;
        more.textContent = "加载中…";
        histApi("GET", "/api/v1/conversations/" + conv.thread_id +
                "/messages?limit=20&before_seq=" + items[0].seq)
          .then(function (d) { renderMsgs(conv, container, (d.items || []).concat(items), d.has_more); })
          .catch(function (e) { more.disabled = false; more.textContent = "加载失败，点击重试"; });
      });
    }
  }

  function loadMessages(conv, container) {
    container.innerHTML = "<div class='hist-loading'>加载中…</div>";
    histApi("GET", "/api/v1/conversations/" + conv.thread_id + "/messages?limit=20")
      .then(function (data) { renderMsgs(conv, container, data.items || [], data.has_more); })
      .catch(function (e) {
        container.innerHTML = "<div class='hist-empty'>加载失败：" + KB.esc(e.message) + "</div>";
      });
  }

  function itemHtml(c) {
    var cls = c.is_current ? " hist-item-current" : "";
    var btn = "";
    if (c.status !== "ACTIVE" && !c.is_current) {
      btn = "<button class='btn btn-primary btn-sm hist-resume' data-thread='" + KB.esc(c.thread_id) + "'>💬 继续对话</button>";
    }
    return "<div class='hist-item" + cls + "' data-thread='" + KB.esc(c.thread_id) + "'>" +
      "<div class='hist-item-head'>" +
      "<div class='hist-item-title'>第 " + c.session_seq + " 次对话 · " + KB.esc(fmtDate(c.started_at)) +
      " " + statusTag(c) + "</div>" +
      "<div class='hist-item-sub'>" + KB.esc(fmtDateTime(c.started_at)) + " 开始 · " +
      (c.message_count || 0) + " 条消息</div></div>" +
      "<div class='hist-msgs' style='display:none;'></div>" +
      (btn ? "<div class='hist-actions'>" + btn + "</div>" : "") +
      "</div>";
  }

  function renderList(items) {
    if (!items.length) {
      KB.renderState(listEl, { kind: "empty", message: "还没有对话，点上面的「开启新对话」开始吧" });
      return;
    }
    listEl.innerHTML = items.map(itemHtml).join("");

    // 点击条目 → 展开/收起消息气泡（每次展开重新拉取，保证最新）
    listEl.querySelectorAll(".hist-item").forEach(function (el) {
      var threadId = el.dataset.thread;
      var conv = items.filter(function (x) { return x.thread_id === threadId; })[0] || {};
      var msgs = el.querySelector(".hist-msgs");
      el.querySelector(".hist-item-head").addEventListener("click", function () {
        var open = msgs.style.display === "none";
        if (open) {
          msgs.style.display = "";
          if (!msgs.dataset.loaded) {
            msgs.dataset.loaded = "1";
            loadMessages(conv, msgs);
          }
        } else {
          msgs.style.display = "none";
        }
      });
    });

    // 「继续对话」：activate 后刷新（之后说话自动落入该会话，WS 零改动）
    listEl.querySelectorAll(".hist-resume").forEach(function (btn) {
      btn.addEventListener("click", function (ev) {
        ev.stopPropagation();
        var threadId = btn.dataset.thread;
        btn.disabled = true;
        btn.textContent = "切换中…";
        histApi("POST", "/api/v1/conversations/" + threadId + "/activate")
          .then(function () { return refresh(); })
          .then(function () { KB.toast("已切回该对话，请继续说话", "success"); })
          .catch(function (e) { handleError(e); });
      });
    });
  }

  function handleError(e) {
    // 统一错误处理器把 detail 放 message（code 恒为 HTTP_ERROR），
    // 故 identity_locked 同时匹配 code 与 message 两种落点
    if (e.status === 403 && (e.code === "identity_locked" || e.message === "identity_locked")) {
      // 声纹确认 300s 已过期或缺失 → 回到遮罩
      voiceToken = null;
      showMask();
      return;
    }
    KB.renderState(listEl, { kind: "error", message: KB.errText(e), traceId: e.traceId }, refresh);
  }

  /** 拉取会话列表并渲染（门禁 403 时回遮罩）。 */
  function refresh() {
    if (!voiceToken) { showMask(); return; }
    KB.renderState(listEl, { kind: "loading" });
    histApi("GET", "/api/v1/conversations?page=1&page_size=50")
      .then(function (data) { renderList(data.items || []); })
      .catch(handleError);
  }

  /** 开启新对话：旧 ACTIVE 自动收起为 ENDED，列表刷新后两者并列可见。 */
  function startNew() {
    if (!voiceToken) { showMask(); return; }
    var btn = $("btn-history-new");
    btn.disabled = true;
    histApi("POST", "/api/v1/conversations", {})
      .then(function () { return refresh(); })
      .then(function () { KB.toast("已开启新对话，请开始说话", "success"); })
      .catch(handleError)
      .then(function () { btn.disabled = false; });
  }

  // ---------- 对外接口（voice.js 声纹通过后调用） ----------
  window.KangbanHistory = {
    /** 声纹确认通过：记录 300s 门禁 token，解锁历史区并拉取列表。 */
    setVoiceToken: function (token) {
      if (!token) return;
      voiceToken = token;
      hideMask();
      refresh();
    },
    /** 门禁失效（403 identity_locked）时回遮罩。 */
    lock: function () {
      voiceToken = null;
      showMask();
    },
    refresh: refresh,
  };

  $("btn-history-new").addEventListener("click", startNew);
  $("btn-history-refresh").addEventListener("click", refresh);

  // 入口页「历史对话」卡片带 #history 锚点跳转时，滚动到本区
  if (location.hash === "#history" && maskEl && bodyEl) {
    $("history-card").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // 初始态：未声纹确认 → 遮罩
  showMask();
})();
