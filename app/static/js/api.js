/* ============================================================
   康伴前端统一封装（原生 JS，无依赖）
   - api(): 自动附 Authorization，统一解析 error.trace_id
   - sessionStorage 会话：kb_token / kb_role / kb_display_name
   - toast / confirmModal / secretModal / 抽屉 / 页签工具
   安全说明：前端隐藏 ≠ 权限隔离，安全防线在后端（阶段一已实测）
   ============================================================ */

(function () {
  "use strict";

  var SESSION_KEYS = ["kb_token", "kb_role", "kb_display_name", "kb_username"];

  // ---------- 会话 ----------

  function saveSession(data) {
    sessionStorage.setItem("kb_token", data.access_token);
    sessionStorage.setItem("kb_role", data.role || "");
    if (data.display_name) sessionStorage.setItem("kb_display_name", data.display_name);
    if (data.username) sessionStorage.setItem("kb_username", data.username);
  }

  function getSession() {
    return {
      token: sessionStorage.getItem("kb_token"),
      role: sessionStorage.getItem("kb_role"),
      displayName: sessionStorage.getItem("kb_display_name"),
      username: sessionStorage.getItem("kb_username"),
    };
  }

  function clearSession() {
    SESSION_KEYS.forEach(function (k) { sessionStorage.removeItem(k); });
  }

  function logout() {
    clearSession();
    window.location.href = "/index.html";
  }

  /** 角色守卫：token 缺失或角色不符回登录页（仅前端引导，安全由后端保证）。 */
  function requireRole(roles) {
    var s = getSession();
    if (!s.token || roles.indexOf(s.role) === -1) {
      window.location.replace("/index.html");
      throw new Error("redirect");
    }
    return s;
  }

  // ---------- 请求 ----------

  /**
   * 统一请求：非 2xx 抛 {status, code, message, traceId}。
   * 后端错误统一为 {"error": {code, message, trace_id}}。
   */
  function api(method, path, body) {
    var headers = { Accept: "application/json" };
    var token = sessionStorage.getItem("kb_token");
    if (token) headers["Authorization"] = "Bearer " + token;
    var opts = { method: method, headers: headers };
    if (body !== undefined && body !== null) {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    return fetch(path, opts).then(function (resp) {
      if (resp.status === 204) return null;
      return resp
        .json()
        .catch(function () { return null; })
        .then(function (data) {
          if (resp.ok) return data;
          var err = (data && data.error) || {};
          var e = new Error(err.message || "请求失败（HTTP " + resp.status + "）");
          e.status = resp.status;
          e.code = err.code || "";
          e.traceId = err.trace_id || resp.headers.get("X-Trace-ID") || "";
          e.details = err.details || null;
          throw e;
        });
    });
  }

  function errText(e) {
    var msg = e && e.message ? e.message : String(e);
    if (e && e.traceId) msg += "（trace_id: " + e.traceId + "）";
    return msg;
  }

  /**
   * 带登录态打开下载/导出类页面（新标签页不自动携带 Authorization，
   * 直接 window.open 接口地址会 401，故先取回内容再经 blob 打开）。
   */
  function openAuthorized(path) {
    fetch(path, { headers: { Authorization: "Bearer " + sessionStorage.getItem("kb_token") } })
      .then(function (r) {
        if (!r.ok) {
          var e = new Error("打开失败（HTTP " + r.status + "）");
          e.status = r.status;
          throw e;
        }
        return r.blob();
      })
      .then(function (blob) {
        var url = URL.createObjectURL(blob);
        window.open(url, "_blank");
        setTimeout(function () { URL.revokeObjectURL(url); }, 60000);
      })
      .catch(function (e) { toast(errText(e), "error"); });
  }

  // ---------- Toast ----------

  function ensureToastWrap() {
    var wrap = document.getElementById("kb-toast-wrap");
    if (!wrap) {
      wrap = document.createElement("div");
      wrap.id = "kb-toast-wrap";
      document.body.appendChild(wrap);
    }
    return wrap;
  }

  function toast(msg, type) {
    var wrap = ensureToastWrap();
    var el = document.createElement("div");
    el.className = "toast" + (type === "success" ? " toast-success" : type === "error" ? " toast-error" : "");
    el.textContent = msg;
    wrap.appendChild(el);
    setTimeout(function () {
      el.style.transition = "opacity 0.3s";
      el.style.opacity = "0";
      setTimeout(function () { el.remove(); }, 320);
    }, 3200);
  }

  // ---------- 弹窗 ----------

  /** 通用弹窗骨架：返回 {mask, close}；点击遮罩不关闭（防误触），只走按钮。 */
  function openModal(innerHtml) {
    var mask = document.createElement("div");
    mask.className = "modal-mask show";
    var modal = document.createElement("div");
    modal.className = "modal";
    modal.innerHTML = innerHtml;
    mask.appendChild(modal);
    document.body.appendChild(mask);
    function close() { mask.remove(); }
    return { mask: mask, modal: modal, close: close };
  }

  /** 二次确认：Promise<boolean>，Esc/取消 = false。 */
  function confirmModal(title, text, danger) {
    return new Promise(function (resolve) {
      var m = openModal(
        "<h3>" + esc(title) + "</h3>" +
        "<p class='muted'>" + esc(text) + "</p>" +
        "<div class='modal-actions'>" +
        "<button class='btn btn-ghost' data-act='no'>取消</button>" +
        "<button class='btn " + (danger ? "btn-danger" : "btn-primary") + "' data-act='yes'>确认</button>" +
        "</div>"
      );
      m.modal.querySelector("[data-act='no']").addEventListener("click", function () { m.close(); resolve(false); });
      m.modal.querySelector("[data-act='yes']").addEventListener("click", function () { m.close(); resolve(true); });
      document.addEventListener("keydown", function onEsc(ev) {
        if (ev.key === "Escape") { m.close(); resolve(false); document.removeEventListener("keydown", onEsc); }
      });
    });
  }

  /** 一次性机密展示（注册凭证 / 新密码）：明文 + 复制 + 关闭后不可再见警示。 */
  function secretModal(title, secret, warnText) {
    var m = openModal(
      "<h3>" + esc(title) + "</h3>" +
      "<div class='warn-line'>⚠ <span>" + esc(warnText || "该明文仅此一次显示，关闭后不可再见，请立即复制保存。") + "</span></div>" +
      "<div class='secret-box' id='kb-secret'>" + esc(secret) + "</div>" +
      "<div class='modal-actions'>" +
      "<button class='btn btn-ghost' id='kb-secret-copy'>复制</button>" +
      "<button class='btn btn-primary' id='kb-secret-ok'>我已保存，关闭</button>" +
      "</div>"
    );
    m.modal.querySelector("#kb-secret-copy").addEventListener("click", function () {
      copyText(secret).then(function (ok) {
        toast(ok ? "已复制到剪贴板" : "复制失败，请手动选择复制", ok ? "success" : "error");
      });
    });
    m.modal.querySelector("#kb-secret-ok").addEventListener("click", m.close);
  }

  // ---------- 剪贴板 / 转义 ----------

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(function () { return true; }, function () { return legacyCopy(text); });
    }
    return Promise.resolve(legacyCopy(text));
  }

  function legacyCopy(text) {
    try {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      var ok = document.execCommand("copy");
      ta.remove();
      return ok;
    } catch (e) { return false; }
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // ---------- 页签 / 抽屉 / 表单三态 ----------

  function initTabs(tabsEl, onChange) {
    var tabs = tabsEl.querySelectorAll(".tab");
    var panels = document.querySelectorAll(".tab-panel");
    tabs.forEach(function (t) {
      t.addEventListener("click", function () {
        tabs.forEach(function (x) { x.classList.remove("active"); });
        panels.forEach(function (p) { p.classList.remove("active"); });
        t.classList.add("active");
        var panel = document.getElementById("panel-" + t.dataset.tab);
        if (panel) panel.classList.add("active");
        if (onChange) onChange(t.dataset.tab);
      });
    });
  }

  /** 右侧详情抽屉：show(title, kvPairs)。kvPairs: [[label, value], ...] */
  function showDrawer(title, kvPairs) {
    closeDrawer();
    var maskEl = document.createElement("div");
    maskEl.className = "drawer-mask";
    maskEl.id = "kb-drawer-mask";
    var drawer = document.createElement("aside");
    drawer.className = "drawer";
    drawer.id = "kb-drawer";
    var rows = kvPairs.map(function (p) {
      return "<dt>" + esc(p[0]) + "</dt><dd>" + esc(p[1] == null || p[1] === "" ? "—" : p[1]) + "</dd>";
    }).join("");
    drawer.innerHTML = "<h3>" + esc(title) + "</h3><dl class='kv'>" + rows + "</dl>" +
      "<div class='modal-actions'><button class='btn btn-ghost' id='kb-drawer-close'>关闭</button></div>";
    document.body.appendChild(maskEl);
    document.body.appendChild(drawer);
    requestAnimationFrame(function () {
      maskEl.classList.add("show");
      drawer.classList.add("show");
    });
    function close() {
      maskEl.remove();
      drawer.remove();
    }
    maskEl.addEventListener("click", close);
    drawer.querySelector("#kb-drawer-close").addEventListener("click", close);
  }

  function closeDrawer() {
    var m = document.getElementById("kb-drawer-mask");
    var d = document.getElementById("kb-drawer");
    if (m) m.remove();
    if (d) d.remove();
  }

  /** 列表容器三态渲染。state: {kind: 'loading'|'empty'|'error'|'ok', message, traceId} */
  function renderState(container, state, retryFn) {
    if (state.kind === "loading") {
      container.innerHTML =
        "<div class='skeleton'><div class='bar w60'></div><div class='bar'></div><div class='bar w40'></div></div>" +
        "<div class='skeleton mt-8'><div class='bar w40'></div><div class='bar w60'></div><div class='bar'></div></div>";
      return;
    }
    if (state.kind === "empty") {
      container.innerHTML = "<div class='state'><span class='big'>🍃</span>" + esc(state.message || "暂无数据") + "</div>";
      return;
    }
    if (state.kind === "error") {
      container.innerHTML =
        "<div class='state error'><span class='big'>⚠️</span>" + esc(state.message || "加载失败") +
        (state.traceId ? "<div class='tid'>trace_id: " + esc(state.traceId) + "</div>" : "") +
        (retryFn ? "<div class='retry'><button class='btn btn-ghost btn-sm'>重试</button></div>" : "") +
        "</div>";
      if (retryFn) container.querySelector(".retry .btn").addEventListener("click", retryFn);
      return;
    }
    container.innerHTML = "";
  }

  // ---------- 健康角标 ----------

  function bindHealthBadge(el) {
    fetch("/health", { headers: { Accept: "application/json" } })
      .then(function (r) { el.classList.toggle("down", !r.ok); el.innerHTML = r.ok
        ? "<span class='dot dot-on'></span>服务正常"
        : "<span class='dot dot-off'></span>服务异常"; })
      .catch(function () {
        el.classList.add("down");
        el.innerHTML = "<span class='dot dot-off'></span>服务异常";
      });
  }

  // ---------- 主题（三选一，localStorage 记忆，默认陶瓷白） ----------

  var THEMES = [
    { key: "ceramic", title: "陶瓷白" },
    { key: "terracotta", title: "暖陶雅集" },
    { key: "inknight", title: "夜空黑" },
  ];

  function getTheme() {
    var t = localStorage.getItem("kb_theme");
    return THEMES.some(function (x) { return x.key === t; }) ? t : "ceramic";
  }

  function setTheme(key) {
    if (!THEMES.some(function (x) { return x.key === key; })) return;
    if (key === "ceramic") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", key);
    localStorage.setItem("kb_theme", key);
    document.querySelectorAll(".theme-dot").forEach(function (d) {
      d.classList.toggle("active", d.dataset.themeSet === key);
    });
  }

  // 渲染主题色点到任意容器（登录页与工作台顶栏共用）
  function renderThemeDots(el) {
    if (!el) return;
    el.innerHTML = THEMES.map(function (t) {
      return "<button class='theme-dot dot-" + t.key + (t.key === getTheme() ? " active" : "") +
        "' data-theme-set='" + t.key + "' title='" + t.title + "' aria-label='切换到" + t.title + "'></button>";
    }).join("");
    el.querySelectorAll(".theme-dot").forEach(function (d) {
      d.addEventListener("click", function () { setTheme(d.dataset.themeSet); });
    });
  }

  // 页面加载即应用（在渲染顶栏前执行，避免闪色）
  setTheme(getTheme());

  // ---------- 顶栏（登录后的页面通用） ----------

  function renderTopbar(activeRole) {
    var s = getSession();
    var el = document.getElementById("kb-topbar");
    if (!el) return;
    var roleName = { admin: "管理员", nurse: "护工", doctor: "医生", family: "家属" }[s.role] || s.role;
    el.innerHTML =
      "<div class='brand'><span class='logo'>康伴</span><span class='sub'>数字人康养陪伴系统</span></div>" +
      "<div class='whoami'><div class='theme-dots'></div>" +
      "<span>" + esc(roleName) + " · <b>" + esc(s.displayName || s.username || "") + "</b></span>" +
      "<button class='btn btn-ghost btn-sm' id='kb-logout'>退出</button></div>";
    el.querySelector("#kb-logout").addEventListener("click", logout);
    renderThemeDots(el.querySelector(".theme-dots"));
  }

  // ---------- 导出 ----------

  window.KB = {
    api: api,
    errText: errText,
    openAuthorized: openAuthorized,
    toast: toast,
    confirmModal: confirmModal,
    secretModal: secretModal,
    openModal: openModal,
    copyText: copyText,
    esc: esc,
    saveSession: saveSession,
    getSession: getSession,
    clearSession: clearSession,
    logout: logout,
    requireRole: requireRole,
    initTabs: initTabs,
    showDrawer: showDrawer,
    closeDrawer: closeDrawer,
    renderState: renderState,
    bindHealthBadge: bindHealthBadge,
    renderTopbar: renderTopbar,
    renderThemeDots: renderThemeDots,
  };
})();
