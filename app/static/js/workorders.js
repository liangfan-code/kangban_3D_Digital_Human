/* ============================================================
   敏感事件工单面板（Day8 Task 6，原生 JS，无依赖）
   - 四类页面共用：doctor / nurse（可读可提交）、admin（终审关闭）、family（只读）
   - 依赖 /js/api.js 的 KB（api / openModal / toast / esc / renderState）
   - 员工端额外连接 /api/v1/staff/ws；断线回退为 30 秒轮询未处理数
   - 安全约定：不在 console 输出 token、对话原文或方案全文
   ============================================================ */

(function () {
  "use strict";

  var ROLE_LABEL = { doctor: "医生", nurse: "护理", admin: "管理员", family: "家属" };
  var STATUS_LABEL = {
    open: "待处理", acknowledged: "已读待处理", in_progress: "处理中",
    resolved: "已关闭", cancelled: "已取消",
  };
  var STATUS_TAG = {
    open: "tag-amber", acknowledged: "tag-clay", in_progress: "tag-clay",
    resolved: "tag-moss", cancelled: "",
  };
  var DSTATE_LABEL = { unread: "未读", read: "已读", handled: "已处理" };
  var DSTATE_TAG = { unread: "tag-amber", read: "tag-clay", handled: "tag-moss" };
  var PRIORITY_LABEL = {
    urgent: ["tag-lacquer", "紧急"], high: ["tag-amber", "优先"], normal: ["", "一般"], low: ["", "低"],
  };

  function fmtTime(s) {
    if (!s) return "—";
    var d = new Date(s);
    if (isNaN(d)) return String(s).replace("T", " ").slice(0, 16);
    return d.toLocaleString("zh-CN", {
      timeZone: "Asia/Shanghai", hour12: false,
      year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    }).replace(/\//g, "-");
  }

  function statusTag(s) {
    var label = STATUS_LABEL[s] || s;
    return "<span class='tag " + (STATUS_TAG[s] || "") + "'>" + KB.esc(label) + "</span>";
  }

  function dstateTag(s) {
    var label = DSTATE_LABEL[s] || s;
    return "<span class='tag " + (DSTATE_TAG[s] || "") + "'>" + KB.esc(label) + "</span>";
  }

  function priorityTag(p) {
    var m = PRIORITY_LABEL[p] || ["", p];
    return "<span class='tag " + m[0] + "'>" + KB.esc(m[1]) + "</span>";
  }

  function mount(opts) {
    var root = document.getElementById(opts.rootId);
    if (!root) return null;
    if (root.__workorders) return root.__workorders;

    var role = opts.role;
    var isStaff = role === "doctor" || role === "nurse";
    var needStatusFilter = isStaff;

    var state = {
      q: "", sort: "created_at", order: "desc", statusFilter: "",
      page: 1, pageSize: 20, elderId: null, total: 0, items: [], elders: [],
    };
    var ws = null;
    var pollTimer = null;
    var popupOpen = false;

    // ---------- 骨架 ----------

    var sortOptions = [
      ["created_at:desc", "时间（新→旧）"],
      ["created_at:asc", "时间（旧→新）"],
      ["priority:desc", "优先级（高→低）"],
      ["priority:asc", "优先级（低→高）"],
    ];

    root.innerHTML =
      "<div class='card'>" +
      "<div class='card-title'><h3>敏感事件工单</h3>" +
      (isStaff ? "<span class='faint' data-ws>实时连接中…</span>" : "<span class='faint'>历史工单可查可搜</span>") +
      "</div>" +
      "<div class='toolbar wo-toolbar'>" +
      "<input type='search' data-q placeholder='搜索关键词：类别 / 触发原句 / 处理方案' />" +
      "<button class='btn btn-primary btn-sm' data-search>搜索</button>" +
      "<select data-sort>" +
      sortOptions.map(function (o) {
        return "<option value='" + o[0] + "'>" + o[1] + "</option>";
      }).join("") +
      "</select>" +
      (needStatusFilter
        ? "<select data-status><option value=''>全部</option>" +
          "<option value='pending'>未处理</option><option value='handled'>已处理</option></select>"
        : "") +
      (role === "family" ? "<select data-elder></select>" : "") +
      "<span class='spacer'></span>" +
      "<button class='btn btn-ghost btn-sm' data-refresh>刷新</button>" +
      "</div>" +
      (needStatusFilter ? "<div class='faint' data-status-hint></div>" : "") +
      "<div data-list class='mt-8'></div>" +
      "<div class='toolbar' data-pager style='margin-top:12px;'></div>" +
      "</div>";

    var listBox = root.querySelector("[data-list]");
    var pagerBox = root.querySelector("[data-pager]");

    // ---------- 长者名称 ----------

    function elderName(id) {
      if (!id) return "—";
      var hit = state.elders.find(function (e) { return e.elder_id === id; });
      return hit ? hit.name : "长者 " + String(id).slice(0, 8);
    }

    function loadElders() {
      return KB.api("GET", "/api/v1/elders").then(function (rows) {
        state.elders = rows || [];
        if (role !== "family") return;
        var sel = root.querySelector("[data-elder]");
        if (!sel) return;
        if (!state.elders.length) {
          sel.innerHTML = "<option value=''>暂无关联长者</option>";
          return;
        }
        sel.innerHTML = state.elders.map(function (e) {
          return "<option value='" + e.elder_id + "'>" + KB.esc(e.name) + "</option>";
        }).join("");
        state.elderId = state.elders[0].elder_id;
        sel.value = state.elderId;
        sel.addEventListener("change", function () {
          state.elderId = this.value;
          state.page = 1;
          refresh();
        });
      }).catch(function () {
        state.elders = [];
      });
    }

    // ---------- 列表 ----------

    function listUrl() {
      var p = "?page=" + state.page + "&page_size=" + state.pageSize +
        "&sort=" + encodeURIComponent(state.sort) + "&order=" + encodeURIComponent(state.order);
      if (state.q) p += "&q=" + encodeURIComponent(state.q);
      if (role === "family") {
        return "/api/v1/family/elders/" + encodeURIComponent(state.elderId) + "/workorders" + p;
      }
      return (role === "admin" ? "/api/v1/admin/workorders" : "/api/v1/staff/workorders") + p;
    }

    function filteredItems() {
      if (!state.statusFilter) return state.items;
      return state.items.filter(function (it) {
        var handled = it.my_status === "handled";
        return state.statusFilter === "handled" ? handled : !handled;
      });
    }

    function stateCell(it) {
      if (isStaff) {
        var dot = it.pending && it.my_status === "unread" ? "<span class='wo-dot' title='未读'></span>" : "";
        var label = it.my_status ? dstateTag(it.my_status) : statusTag(it.status);
        return dot + label;
      }
      return statusTag(it.status);
    }

    function renderList() {
      var rows = filteredItems();
      if (!rows.length) {
        KB.renderState(listBox, {
          kind: "empty",
          message: state.statusFilter ? "当前页没有符合条件的工单，可切换筛选或翻页" : "暂无工单，敏感事件命中后会自动建单",
        });
        renderPager();
        return;
      }
      var tbody = rows.map(function (it) {
        return "<tr class='wo-row' data-id='" + it.id + "'>" +
          "<td>" + stateCell(it) + "</td>" +
          "<td>" + KB.esc(it.category || "未分类") + "</td>" +
          "<td>" + KB.esc(elderName(it.elder_id)) + "</td>" +
          "<td>" + fmtTime(it.created_at) + "</td>" +
          "<td>×" + (it.occurrence_count || 1) + "</td>" +
          "<td>" + priorityTag(it.priority) + "</td>" +
          "<td class='actions'><button class='btn btn-ghost btn-sm' data-id='" + it.id + "'>查看</button></td>" +
          "</tr>";
      }).join("");
      listBox.innerHTML = "<div class='table-wrap'><table class='kb'><thead><tr>" +
        "<th>状态</th><th>类别</th><th>长者</th><th>创建时间</th><th>发生次数</th><th>优先级</th><th></th>" +
        "</tr></thead><tbody>" + tbody + "</tbody></table></div>";
      listBox.querySelectorAll("tr[data-id]").forEach(function (tr) {
        tr.addEventListener("click", function () { openDetail(tr.dataset.id); });
      });
      listBox.querySelectorAll("button[data-id]").forEach(function (btn) {
        btn.addEventListener("click", function (ev) {
          ev.stopPropagation();
          openDetail(btn.dataset.id);
        });
      });
      renderPager();
    }

    function renderPager() {
      var pages = Math.max(1, Math.ceil(state.total / state.pageSize));
      var shown = filteredItems().length;
      var info = state.statusFilter
        ? "本页显示 " + shown + " 条 · 共 " + state.total + " 条"
        : "共 " + state.total + " 条";
      pagerBox.innerHTML = "<span class='faint'>" + info + " · 第 " + state.page + "/" + pages + " 页</span>" +
        "<span class='spacer'></span>" +
        "<button class='btn btn-ghost btn-sm' data-prev" + (state.page <= 1 ? " disabled" : "") + ">上一页</button>" +
        "<button class='btn btn-ghost btn-sm' data-next" + (state.page >= pages ? " disabled" : "") + ">下一页</button>";
      pagerBox.querySelector("[data-prev]").addEventListener("click", function () {
        if (state.page <= 1) return;
        state.page -= 1;
        refresh();
      });
      pagerBox.querySelector("[data-next]").addEventListener("click", function () {
        if (state.page >= pages) return;
        state.page += 1;
        refresh();
      });
    }

    function refresh() {
      if (role === "family" && !state.elderId) {
        return KB.renderState(listBox, { kind: "empty", message: "暂无关联长者，请联系社区建立家属-长者关联" });
      }
      KB.renderState(listBox, { kind: "loading" });
      return KB.api("GET", listUrl()).then(function (res) {
        state.items = (res && res.items) || [];
        state.total = (res && res.total) || 0;
        renderList();
      }).catch(function (e) {
        KB.renderState(listBox, { kind: "error", message: KB.errText(e), traceId: e.traceId }, refresh);
      });
    }

    // ---------- 未处理角标 ----------

    function ensureBadge() {
      var who = document.querySelector("#kb-topbar .whoami");
      if (!who) return null;
      var badge = document.getElementById("kb-wo-badge");
      if (!badge) {
        badge = document.createElement("button");
        badge.type = "button";
        badge.id = "kb-wo-badge";
        badge.className = "wo-badge zero";
        badge.title = "跳到工单列表";
        who.insertBefore(badge, who.querySelector("#kb-logout"));
        badge.addEventListener("click", function () {
          root.scrollIntoView({ behavior: "smooth", block: "start" });
        });
      }
      return badge;
    }

    function setBadge(n) {
      var badge = ensureBadge();
      if (!badge) return;
      var count = typeof n === "number" ? n : 0;
      badge.innerHTML = "工单 <b>" + count + "</b>";
      badge.classList.toggle("zero", count === 0);
    }

    function refreshCounts() {
      if (!isStaff) return Promise.resolve();
      return KB.api("GET", "/api/v1/staff/workorders/counts").then(function (c) {
        setBadge(c && typeof c.unhandled === "number" ? c.unhandled : 0);
      }).catch(function () { /* 轮询失败不打扰，下次再试 */ });
    }

    // ---------- 员工端实时推送 ----------

    function setWsState(alive) {
      var el = root.querySelector("[data-ws]");
      if (!el) return;
      el.textContent = alive ? "实时连接中" : "实时连接已断开，正在轮询";
      el.style.color = alive ? "var(--moss)" : "var(--lacquer)";
    }

    function wsUrl() {
      return (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/api/v1/staff/ws";
    }

    function notifyCreated(data) {
      var info = data || {};
      KB.toast("新的敏感事件工单：" + (info.category || "未分类"), "error");
      if (popupOpen) return;
      popupOpen = true;
      var m = KB.openModal(
        "<h3>新的敏感事件工单</h3>" +
        "<div class='warn-line'>⚠ <span>长者对话命中敏感词，请尽快确认情况并填写处理方案。</span></div>" +
        "<dl class='kv mt-16'>" +
        "<dt>长者</dt><dd>" + KB.esc(elderName(info.elder_id)) + "</dd>" +
        "<dt>类别</dt><dd>" + KB.esc(info.category || "未分类") + "</dd>" +
        "<dt>发生次数</dt><dd>第 " + (info.occurrence_count || 1) + " 次</dd>" +
        "</dl>" +
        "<div class='modal-actions'>" +
        "<button class='btn btn-ghost' data-act='later'>稍后处理</button>" +
        "<button class='btn btn-primary' data-act='open'>查看并处理</button>" +
        "</div>"
      );
      m.modal.querySelector("[data-act='later']").addEventListener("click", function () {
        popupOpen = false;
        m.close();
      });
      m.modal.querySelector("[data-act='open']").addEventListener("click", function () {
        popupOpen = false;
        m.close();
        if (info.workorder_id) openDetail(info.workorder_id);
      });
    }

    function connectWs() {
      var token = sessionStorage.getItem("kb_token");
      if (!token) return;
      try {
        ws = new WebSocket(wsUrl());
      } catch (e) {
        setWsState(false);
        startPolling();
        return;
      }
      ws.onopen = function () {
        ws.send(JSON.stringify({ type: "auth", token: token }));
        setWsState(true);
        stopPolling();
      };
      ws.onmessage = function (ev) {
        var msg;
        try { msg = JSON.parse(ev.data); } catch (e) { return; }
        var data = msg && msg.data ? msg.data : {};
        if (msg.type === "ticket.counts") {
          if (typeof data.unhandled === "number") setBadge(data.unhandled);
          return;
        }
        if (msg.type === "ticket.created") {
          notifyCreated(data);
          refresh();
          refreshCounts();
          return;
        }
        if (msg.type === "ticket.updated" || msg.type === "ticket.resolved") {
          refresh();
          refreshCounts();
        }
      };
      ws.onclose = function () {
        ws = null;
        setWsState(false);
        startPolling();
      };
      ws.onerror = function () { /* 关闭事件统一兜底 */ };
    }

    function startPolling() {
      if (pollTimer) return;
      pollTimer = setInterval(refreshCounts, 30000);
      refreshCounts();
    }

    function stopPolling() {
      if (!pollTimer) return;
      clearInterval(pollTimer);
      pollTimer = null;
    }

    // ---------- 详情 ----------

    function detailUrl(id) {
      if (role === "admin") return "/api/v1/admin/workorders/" + id;
      if (role === "family") return "/api/v1/family/workorders/" + id;
      return "/api/v1/staff/workorders/" + id;
    }

    function openDetail(id) {
      if (!id) return;
      var pre = Promise.resolve();
      if (isStaff) {
        // 已读幂等；工单已关闭时后端返回 409，不阻断查看
        pre = KB.api("POST", "/api/v1/staff/workorders/" + id + "/read").catch(function (e) {
          if (e.status !== 409) KB.toast(KB.errText(e), "error");
        });
      }
      pre.then(function () {
        return KB.api("GET", detailUrl(id));
      }).then(function (d) {
        renderDetail(d);
        refresh();
      }).catch(function (e) {
        KB.toast(KB.errText(e), "error");
      });
    }

    function snapshotHtml(snapshot) {
      if (!snapshot || !snapshot.length) {
        return "<p class='muted'>暂无对话快照</p>";
      }
      return "<div class='wo-chat'>" + snapshot.map(function (m) {
        var isUser = m.role === "user";
        return "<div class='wo-msg " + (isUser ? "wo-msg-user" : "wo-msg-assistant") + "'>" +
          "<span class='who'>" + (isUser ? "长者" : "数字人") + " · " + fmtTime(m.at) + "</span>" +
          KB.esc(m.content || "") + "</div>";
      }).join("") + "</div>";
    }

    function dispositionHtml(x) {
      var plan = x.plan_text
        ? "<p class='ai-text wo-plan'>" + KB.esc(x.plan_text) + "</p>"
        : "<p class='muted'>尚未提交处理方案</p>";
      return "<div class='record-row'><div class='grow'>" +
        "<b>" + KB.esc(ROLE_LABEL[x.role] || x.role) + "</b> " + dstateTag(x.status) +
        (x.handled_at ? "<span class='faint'>　处理于 " + fmtTime(x.handled_at) + "</span>" : "") +
        plan + "</div></div>";
    }

    function renderDetail(d) {
      var handled = d.my_status === "handled";
      var closed = d.status === "resolved" || d.status === "cancelled";

      var html = "<h3>工单详情 · " + KB.esc(d.title || "敏感事件") + "</h3>" +
        "<p class='faint'>工单号 " + KB.esc(String(d.id).slice(0, 8)) +
        " ｜ 创建于 " + fmtTime(d.created_at) +
        " ｜ 发生 " + (d.occurrence_count || 1) + " 次</p>" +
        "<div class='toolbar' style='margin:10px 0 6px;'>" + statusTag(d.status) + priorityTag(d.priority) + "</div>" +
        "<dl class='kv'>" +
        "<dt>长者</dt><dd>" + KB.esc(elderName(d.elder_id)) + "</dd>" +
        "<dt>类别</dt><dd>" + KB.esc(d.category || "未分类") + "</dd>" +
        "<dt>更新时间</dt><dd>" + fmtTime(d.updated_at) + "</dd>" +
        (d.resolved_at ? "<dt>关闭时间</dt><dd>" + fmtTime(d.resolved_at) + "</dd>" : "") +
        "</dl>" +
        "<h3 class='hp-head'>触发原句</h3>" +
        "<p class='ai-text'>" + KB.esc(d.description || "—") + "</p>";

      if (role !== "family") {
        // 隐私边界：对话快照仅医生、护理、管理员可见
        html += "<h3 class='hp-head'>对话快照</h3>" + snapshotHtml(d.context_snapshot);
      }

      html += "<h3 class='hp-head'>处理情况</h3>";
      var disps = d.dispositions || [];
      html += disps.length
        ? disps.map(dispositionHtml).join("")
        : "<p class='muted'>暂无处理项</p>";

      if (d.closed_note) {
        html += "<div class='warn-line mt-16'>终审备注：" + KB.esc(d.closed_note) + "</div>";
      }

      if (isStaff && !handled && !closed) {
        html += "<h3 class='hp-head'>提交处理方案</h3>" +
          "<div class='form-error' data-err></div>" +
          "<div class='field'><label>处理方案<span class='req'>*</span></label>" +
          "<textarea data-plan maxlength='2000' placeholder='记录判断、处置措施与后续跟进（1-2000 字）'></textarea></div>";
      }

      if (role === "admin" && !closed) {
        html += "<h3 class='hp-head'>终审关闭</h3>" +
          "<div class='form-error' data-err></div>" +
          "<div class='field'><label>终审备注（可选，500 字内）</label>" +
          "<input data-note maxlength='500' placeholder='如 已电话回访家属并确认情况' /></div>";
      }

      html += "<div class='modal-actions'>";
      if (isStaff && !handled && !closed) {
        html += "<button class='btn btn-primary' data-act='submit'>完成处理</button>";
      }
      if (role === "admin" && !closed) {
        html += "<button class='btn btn-primary' data-act='close'>标记已处理</button>";
      }
      html += "<button class='btn btn-ghost' data-act='dismiss'>关闭</button></div>";

      var m = KB.openModal(html);
      m.modal.style.width = "min(760px, 100%)";
      m.modal.querySelector("[data-act='dismiss']").addEventListener("click", m.close);
      var errBox = m.modal.querySelector("[data-err]");

      var submitBtn = m.modal.querySelector("[data-act='submit']");
      if (submitBtn) {
        submitBtn.addEventListener("click", function () {
          var plan = m.modal.querySelector("[data-plan]").value.trim();
          if (!plan) {
            errBox.innerHTML = "⚠ 请填写处理方案";
            errBox.classList.add("show");
            return;
          }
          errBox.classList.remove("show");
          submitBtn.disabled = true;
          KB.api("POST", "/api/v1/staff/workorders/" + d.id + "/disposition", { plan_text: plan })
            .then(function () {
              KB.toast("处理方案已提交", "success");
              m.close();
              refresh();
              refreshCounts();
              openDetail(d.id);
            })
            .catch(function (e) {
              errBox.innerHTML = "⚠ " + KB.esc(
                e.status === 409 ? (e.message || "该处理项已处理，不能重复提交") : KB.errText(e)
              );
              errBox.classList.add("show");
              submitBtn.disabled = false;
              if (e.status === 409) {
                refresh();
                refreshCounts();
              }
            });
        });
      }

      var closeBtn = m.modal.querySelector("[data-act='close']");
      if (closeBtn) {
        closeBtn.addEventListener("click", function () {
          var noteEl = m.modal.querySelector("[data-note]");
          var note = noteEl ? noteEl.value.trim() : "";
          closeBtn.disabled = true;
          KB.api("POST", "/api/v1/admin/workorders/" + d.id + "/close", { note: note || null })
            .then(function (nd) {
              KB.toast("工单已终审关闭", "success");
              m.close();
              refresh();
              renderDetail(nd);
            })
            .catch(function (e) {
              if (e.status === 409) {
                var pending = (e.unfinishedRoles || []).map(function (r) { return ROLE_LABEL[r] || r; });
                errBox.innerHTML = "⚠ " + KB.esc(pending.length
                  ? "还差" + pending.join("、") + "未处理，暂不能关闭"
                  : (e.message || "尚有角色未处理，暂不能关闭"));
                errBox.classList.add("show");
                closeBtn.disabled = true;
                closeBtn.textContent = "尚有角色未处理";
                refresh();
              } else {
                errBox.innerHTML = "⚠ " + KB.esc(KB.errText(e));
                errBox.classList.add("show");
                closeBtn.disabled = false;
              }
            });
        });
      }
    }

    // ---------- 工具栏 ----------

    var qInput = root.querySelector("[data-q]");
    function applySearch() {
      state.q = qInput.value.trim();
      state.page = 1;
      refresh();
    }
    root.querySelector("[data-search]").addEventListener("click", applySearch);
    qInput.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") {
        ev.preventDefault();
        applySearch();
      }
    });
    root.querySelector("[data-sort]").addEventListener("change", function () {
      var parts = this.value.split(":");
      state.sort = parts[0];
      state.order = parts[1];
      state.page = 1;
      refresh();
    });
    var statusSel = root.querySelector("[data-status]");
    if (statusSel) {
      statusSel.addEventListener("change", function () {
        state.statusFilter = this.value;
        var hint = root.querySelector("[data-status-hint]");
        if (hint) {
          hint.textContent = state.statusFilter ? "状态筛选按处理项三态在当前页内生效，翻页仍按时间排序。" : "";
        }
        renderList();
      });
    }
    root.querySelector("[data-refresh]").addEventListener("click", function () {
      refresh();
      refreshCounts();
    });

    // ---------- 启动 ----------

    var api = {
      refresh: function () { refresh(); refreshCounts(); },
    };
    root.__workorders = api;

    loadElders().then(function () {
      refresh();
    });

    if (isStaff) {
      ensureBadge();
      refreshCounts();
      connectWs();
    }

    return api;
  }

  window.KBWorkOrders = { mount: mount };
})();
