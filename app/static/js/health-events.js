/* ============================================================
   健康预警待复核面板（Day8 8.2，原生 JS，无依赖）
   - nurse / doctor 共用：GET /api/v1/health-events/pending
   - 决策：POST /api/v1/health-events/{id}/decision（confirm/escalate/reject）
   - 依赖 /js/api.js 的 KB（api / esc / toast / errText / renderState / confirmModal）
   - 权限由后端判定：护士处理黄色、医生处理红色；403/409 可见提示后刷新
   ============================================================ */

(function () {
  "use strict";

  var LEVEL_LABEL = { green: "平稳", yellow: "需留意", red: "紧急" };
  var LEVEL_TAG = { green: "tag-moss", yellow: "tag-amber", red: "tag-lacquer" };
  var STATUS_LABEL = {
    collecting: "采集中",
    pending_confirm: "待确认",
    confirmed: "已确认",
    escalated: "已升级",
    archived: "已归档",
  };
  var ROLE_LABEL = { nurse: "护工", doctor: "医生", admin: "管理员" };
  var ACTION_LABEL = { confirm: "确认", escalate: "升级", reject: "驳回" };
  var PAGE_SIZE = 10;
  var activeInstance = null;

  function fmtTime(s) {
    if (!s) return "—";
    var d = new Date(s);
    if (isNaN(d)) return String(s).replace("T", " ").slice(0, 16);
    return d.toLocaleString("zh-CN", {
      timeZone: "Asia/Shanghai", hour12: false,
      year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    }).replace(/\//g, "-");
  }

  function levelTag(level) {
    var lv = String(level || "").toLowerCase();
    return "<span class='tag " + (LEVEL_TAG[lv] || "") + "'>" + KB.esc(LEVEL_LABEL[lv] || level || "—") + "</span>";
  }

  function statusText(s) {
    return STATUS_LABEL[s] || s || "—";
  }

  function mount(opts) {
    var root = document.getElementById(opts.rootId);
    if (!root) return null;
    if (root.__healthEvents) return root.__healthEvents;

    var role = opts.role;
    var state = { page: 1, total: 0, items: [], busy: false, fetching: false };

    root.innerHTML =
      "<div class='card'>" +
      "<div class='card-title'><h3>健康预警待复核</h3>" +
      "<span class='faint'>护士处理需留意事件，医生处理紧急事件</span></div>" +
      "<div class='toolbar'><span class='spacer'></span>" +
      "<button class='btn btn-ghost btn-sm' data-refresh>刷新</button></div>" +
      "<div data-list class='mt-8'></div>" +
      "<div data-more class='toolbar' style='margin-top:12px;'></div>" +
      "</div>";

    var listBox = root.querySelector("[data-list]");
    var moreBox = root.querySelector("[data-more]");

    function canDecide(item) {
      if (item.status !== "pending_confirm") return false;
      // 红色只由医生/管理员处理；护士按钮不可点，避免无意义的 403
      if (String(item.level).toLowerCase() === "red" && role === "nurse") return false;
      return true;
    }

    function actionsHtml(item) {
      if (String(item.level).toLowerCase() === "red" && role === "nurse") {
        return "<span class='faint'>需医生确认</span>";
      }
      if (!canDecide(item)) {
        return "<span class='faint'>" + KB.esc(statusText(item.status)) + "</span>";
      }
      var html = "<button class='btn btn-primary btn-sm' data-act='confirm' data-id='" + item.event_id + "'>确认</button>" +
        "<button class='btn btn-danger-ghost btn-sm' data-act='reject' data-id='" + item.event_id + "'>驳回</button>";
      if (role !== "nurse") {
        html += "<button class='btn btn-ghost btn-sm' data-act='escalate' data-id='" + item.event_id + "'>升级</button>";
      }
      return html;
    }

    function itemHtml(item) {
      var lv = String(item.level || "").toLowerCase();
      return "<div class='health-item health-level-" + KB.esc(lv) + "'>" +
        "<div class='health-head'>" +
        levelTag(item.level) +
        "<b class='grow'>" + KB.esc(item.elder_name || "未知长者") + "</b>" +
        "<span class='faint'>" + KB.esc(item.room_label || "房间未分配") + "</span>" +
        "</div>" +
        "<div class='health-summary'>" + KB.esc(item.summary || "—") + "</div>" +
        "<div class='health-foot'>" +
        "<span class='faint'>" + fmtTime(item.created_at) +
        " · 待" + KB.esc(ROLE_LABEL[item.required_role] || item.required_role || "处理") + "确认</span>" +
        "<span class='spacer'></span>" + actionsHtml(item) +
        "</div></div>";
    }

    function renderList() {
      if (!state.items.length) {
        KB.renderState(listBox, { kind: "empty", message: "暂无待复核的健康预警" });
        moreBox.innerHTML = "";
        return;
      }
      listBox.innerHTML = state.items.map(itemHtml).join("");
      listBox.querySelectorAll("[data-act]").forEach(function (btn) {
        btn.addEventListener("click", function () {
          decide(btn.dataset.id, btn.dataset.act);
        });
      });
      renderMore();
    }

    function renderMore() {
      if (state.items.length >= state.total) {
        moreBox.innerHTML = "<span class='faint'>共 " + state.total + " 条，已全部加载</span>";
        return;
      }
      moreBox.innerHTML = "<button class='btn btn-ghost btn-sm' data-load-more>加载更多（" +
        state.items.length + "/" + state.total + "）</button>";
      moreBox.querySelector("[data-load-more]").addEventListener("click", function () {
        if (state.fetching) return; // 防抖：上一页还在加载时不重复请求
        state.page += 1;
        fetchPage(true);
      });
    }

    function fetchPage(append) {
      state.fetching = true;
      KB.renderState(listBox, { kind: "loading" });
      KB.api("GET", "/api/v1/health-events/pending?page=" + state.page + "&page_size=" + PAGE_SIZE)
        .then(function (res) {
          var rows = (res && res.items) || [];
          state.total = (res && res.total) || 0;
          if (append) {
            var seen = {};
            state.items.forEach(function (it) { seen[it.event_id] = true; });
            rows = rows.filter(function (it) { return !seen[it.event_id]; }); // 去重防重复行
          }
          state.items = append ? state.items.concat(rows) : rows;
          renderList();
        })
        .catch(function (e) {
          var msg = e.status === 403 ? "无权限查看健康预警" : KB.errText(e);
          KB.renderState(listBox, { kind: "error", message: msg, traceId: e.traceId }, refresh);
        })
        .then(function () { state.fetching = false; });
    }

    function refresh() {
      state.page = 1;
      state.items = [];
      return fetchPage(false);
    }

    function decide(id, action) {
      if (!id || state.busy) return;
      state.busy = true; // 弹窗打开期间即锁定，防止并发开多个确认弹窗
      var label = ACTION_LABEL[action] || action;
      var ask = action === "reject"
        ? "驳回后该事件将归档并关闭对应工单，确认是误报吗？"
        : (action === "escalate" ? "升级后会转为紧急工单并通知医生，确认继续？" : "确认已核实该长者情况？");
      KB.confirmModal("健康预警 · " + label, ask, action !== "confirm").then(function (ok) {
        if (!ok) {
          state.busy = false;
          return;
        }
        KB.api("POST", "/api/v1/health-events/" + id + "/decision", { action: action })
          .then(function (res) {
            var extra = res && res.referral_id ? "，转诊单 " + res.referral_id : "";
            KB.toast("已" + label + extra, "success");
          })
          .catch(function (e) {
            var msg = e.status === 409 ? "该事件已被处理，已为你刷新列表" : KB.errText(e);
            KB.toast(msg, "error");
          })
          .then(function () {
            state.busy = false;
            refresh();
          });
      });
    }

    var instance = {
      refresh: refresh,
      decide: decide,
    };
    root.__healthEvents = instance;
    activeInstance = instance;

    root.querySelector("[data-refresh]").addEventListener("click", refresh);
    refresh();
    return instance;
  }

  window.KangbanHealth = {
    mount: mount,
    refresh: function () { if (activeInstance) activeInstance.refresh(); },
    decide: function (id, action) { if (activeInstance) activeInstance.decide(id, action); },
  };
})();
