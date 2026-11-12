/* ============================================================
   用药计划前端（Day7 段2，原生 JS，无依赖）
   - 医生：计划列表 / 新建表单 / 确认页 / 修改差异确认 / 暂停恢复取消
   - 家属：只读计划 + 修改记录 + 服药历史时间线（字段白名单）
   - 依赖 /js/api.js 的 KB（api / openModal / confirmModal / toast / esc / renderState）
   - 安全约定：家属视图不渲染 internal_note / skip_reason / workorder_id / 账号 UUID
   ============================================================ */

(function () {
  "use strict";

  var PLAN_STATUS = {
    planned: { label: "待确认", tag: "tag-amber" },
    active: { label: "生效中", tag: "tag-moss" },
    paused: { label: "已暂停", tag: "tag-amber" },
    completed: { label: "已完成", tag: "tag-clay" },
    cancelled: { label: "已取消", tag: "" },
  };
  var PLAN_FILTERS = [
    ["", "全部"], ["planned", "待确认"], ["active", "生效中"],
    ["paused", "已暂停"], ["completed", "已完成"], ["cancelled", "已取消"],
  ];
  var FAMILY_PLAN_FILTERS = [
    ["", "全部"], ["active", "生效中"], ["paused", "已暂停"],
    ["completed", "已完成"], ["cancelled", "已取消"],
  ];
  var ORDER_STATUS = {
    pending: { label: "待服", tag: "tag-amber" },
    calling: { label: "呼叫中", tag: "tag-clay" },
    confirmed: { label: "已服", tag: "tag-moss" },
    missed: { label: "未服", tag: "tag-lacquer" },
    skipped: { label: "已跳过", tag: "" },
    cancelled: { label: "已取消", tag: "" },
  };
  var EVENT_LABEL = {
    created: "已生成任务", reminded: "已提醒", confirmed: "已确认",
    denied: "已否认", timeout: "超时", missed: "漏服",
    skipped: "已跳过", cancelled: "已取消",
  };
  var SOURCE_LABEL = {
    scheduler: "定时调度", voice: "语音", text: "文字",
    staff: "员工", family: "家属", system: "系统",
  };
  var ROLE_LABEL = { doctor: "医生", nurse: "护理", admin: "管理员", family: "家属" };
  var WEEK_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
  var FIELD_LABEL = {
    drug_name: "药名", dose: "剂量", schedule: "服用时间/星期",
    instructions: "用药说明", internal_note: "内部备注",
    start_date: "开始日期", end_date: "结束日期",
  };
  var MIN_TIME = /^([01]\d|2[0-3]):[0-5]\d$/;

  // ---------- 通用格式化 ----------

  function fmtDate(v) {
    if (!v) return "—";
    if (typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v)) return v;
    var d = new Date(v);
    if (isNaN(d)) return String(v).slice(0, 10);
    return d.toLocaleDateString("zh-CN", {
      timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    }).replace(/\//g, "-");
  }

  function fmtTime(v) {
    if (!v) return "—";
    var d = new Date(v);
    if (isNaN(d)) return String(v).replace("T", " ").slice(0, 16);
    return d.toLocaleString("zh-CN", {
      timeZone: "Asia/Shanghai", hour12: false,
      year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    }).replace(/\//g, "-");
  }

  function ymd(v) {
    if (!v) return "";
    if (typeof v === "string") return v.slice(0, 10);
    return fmtDate(v);
  }

  function todayYmd() {
    return new Date().toLocaleDateString("sv-SE", { timeZone: "Asia/Shanghai" });
  }

  function shortId(id) {
    return id ? String(id).slice(0, 8) : "—";
  }

  function timesText(schedule) {
    var times = (schedule && schedule.times) || [];
    return times.length ? times.join("、") : "—";
  }

  function normalizeDays(days) {
    var list = Array.isArray(days) ? days.filter(function (d) { return d >= 0 && d <= 6; }) : [];
    if (!list.length || list.length === 7) return [0, 1, 2, 3, 4, 5, 6];
    return list.slice().sort(function (a, b) { return a - b; });
  }

  function weekdayText(days) {
    var list = normalizeDays(days);
    return list.length === 7 ? "每天" : list.map(function (d) { return WEEK_LABELS[d]; }).join("、");
  }

  function dateRange(plan) {
    return fmtDate(plan.start_date) + " 至 " + (plan.end_date ? fmtDate(plan.end_date) : "长期");
  }

  function planStatusTag(status) {
    var m = PLAN_STATUS[status] || { label: status || "—", tag: "" };
    return "<span class='tag " + m.tag + "'>" + KB.esc(m.label) + "</span>";
  }

  function orderStatusTag(status) {
    var m = ORDER_STATUS[status] || { label: status || "—", tag: "" };
    return "<span class='tag " + m.tag + "'>" + KB.esc(m.label) + "</span>";
  }

  function eventLabel(type) {
    return EVENT_LABEL[type] || type || "—";
  }

  function changeValueText(field, value) {
    if (field === "schedule") {
      if (!value || typeof value !== "object") return "—";
      return timesText(value) + " · " + weekdayText(value.weekdays);
    }
    if (field === "start_date" || field === "end_date") {
      return value ? fmtDate(value) : "—";
    }
    if (value == null || value === "") return "—";
    if (typeof value === "object") return JSON.stringify(value);
    return String(value);
  }

  function emptyState(box, message) {
    KB.renderState(box, { kind: "empty", message: message });
  }

  // ---------- 医生：计划列表 ----------

  function openDoctorPlans(elder) {
    if (!elder || !elder.elder_id) return;
    var elderLabel = elder.name || "长者";
    var state = { status: "", items: [] };

    var m = KB.openModal(
      "<h3>用药计划 · " + KB.esc(elderLabel) + "</h3>" +
      "<div class='toolbar'>" +
      "<select data-status>" +
      PLAN_FILTERS.map(function (o) {
        return "<option value='" + o[0] + "'>" + o[1] + "</option>";
      }).join("") +
      "</select>" +
      "<span class='spacer'></span>" +
      "<button class='btn btn-ghost btn-sm' data-refresh>刷新</button>" +
      "<button class='btn btn-primary btn-sm' data-create style='display:none'>+ 新建用药计划</button>" +
      "</div>" +
      "<div class='faint' data-hint></div>" +
      "<div data-list class='mt-8'></div>" +
      "<div class='modal-actions'><button class='btn btn-ghost' data-act='close'>关闭</button></div>"
    );
    m.modal.style.width = "min(900px, 100%)";
    m.modal.querySelector("[data-act='close']").addEventListener("click", m.close);

    var listBox = m.modal.querySelector("[data-list]");
    var hintBox = m.modal.querySelector("[data-hint]");
    var createBtn = m.modal.querySelector("[data-create]");
    var statusSel = m.modal.querySelector("[data-status]");

    function listUrl() {
      var url = "/api/v1/elders/" + encodeURIComponent(elder.elder_id) + "/medication-plans";
      if (state.status) url += "?status=" + encodeURIComponent(state.status);
      return url;
    }

    function load() {
      KB.renderState(listBox, { kind: "loading" });
      return KB.api("GET", listUrl()).then(function (rows) {
        state.items = rows || [];
        createBtn.style.display = "";
        hintBox.textContent = "";
        renderList();
      }).catch(function (e) {
        createBtn.style.display = "none";
        if (e.status === 403 || e.status === 404) {
          hintBox.textContent = "";
          listBox.innerHTML =
            "<div class='warn-line mt-16'>⚠ <span>无权限或未绑定该长者，无法查看或创建用药计划。</span></div>";
          return;
        }
        KB.renderState(listBox, { kind: "error", message: KB.errText(e), traceId: e.traceId }, load);
      });
    }

    function renderList() {
      if (!state.items.length) {
        emptyState(listBox, "暂无用药计划");
        return;
      }
      var rows = state.items.map(function (p) {
        return "<tr>" +
          "<td>" + KB.esc(p.drug_name) + "</td>" +
          "<td>" + KB.esc(p.dose) + "</td>" +
          "<td>" + KB.esc(timesText(p.schedule)) + "</td>" +
          "<td>" + KB.esc(weekdayText((p.schedule || {}).weekdays)) + "</td>" +
          "<td>" + KB.esc(dateRange(p)) + "</td>" +
          "<td>" + planStatusTag(p.status) + "</td>" +
          "<td>" + fmtTime(p.updated_at) + "</td>" +
          "<td>" + KB.esc(shortId(p.workorder_id)) + "</td>" +
          "<td class='actions'>" + actionButtons(p) + "</td>" +
          "</tr>";
      }).join("");
      listBox.innerHTML = "<div class='table-wrap'><table class='kb'><thead><tr>" +
        "<th>药名</th><th>剂量</th><th>服用时间</th><th>星期</th><th>起止</th>" +
        "<th>状态</th><th>更新时间</th><th>工单</th><th></th>" +
        "</tr></thead><tbody>" + rows + "</tbody></table></div>";
      listBox.querySelectorAll("button[data-act]").forEach(function (btn) {
        btn.addEventListener("click", function () {
          var pid = btn.getAttribute("data-pid");
          var plan = state.items.find(function (x) { return String(x.plan_id) === pid; });
          if (!plan) return;
          var act = btn.getAttribute("data-act");
          if (act === "edit") {
            openPlanForm(elder, plan, load);
          } else if (act === "confirm") {
            showConfirmPage(plan, {
              elderLabel: elderLabel,
              backLabel: "关闭",
              onConfirmed: function () { load(); },
            });
          } else {
            actOnPlan(plan, act, load);
          }
        });
      });
    }

    function actionButtons(p) {
      var acts = [];
      if (p.status === "planned") acts.push(["confirm", "确认下发", "btn-primary"]);
      if (p.status === "planned" || p.status === "active" || p.status === "paused") {
        acts.push(["edit", "编辑", "btn-ghost"]);
      }
      if (p.status === "active") acts.push(["pause", "暂停", "btn-ghost"]);
      if (p.status === "paused") acts.push(["resume", "恢复", "btn-ghost"]);
      if (p.status === "planned" || p.status === "active" || p.status === "paused") {
        acts.push(["cancel", "取消", "btn-ghost"]);
      }
      if (!acts.length) return "<span class='faint'>—</span>";
      return acts.map(function (a) {
        return "<button class='btn " + a[2] + " btn-sm' data-act='" + a[0] +
          "' data-pid='" + KB.esc(p.plan_id) + "'>" + a[1] + "</button>";
      }).join(" ");
    }

    statusSel.addEventListener("change", function () {
      state.status = this.value;
      load();
    });
    m.modal.querySelector("[data-refresh]").addEventListener("click", load);
    createBtn.addEventListener("click", function () {
      openPlanForm(elder, null, load);
    });

    load();
  }

  // ---------- 医生：新建 / 编辑表单 ----------

  function openPlanForm(elder, plan, onSaved) {
    var isEdit = !!plan;
    var schedule = (plan && plan.schedule) || {};
    var init = {
      drug_name: plan ? plan.drug_name : "",
      dose: plan ? plan.dose : "",
      times: (schedule.times && schedule.times.length) ? schedule.times.slice() : ["08:00"],
      weekdays: (schedule.weekdays && schedule.weekdays.length)
        ? schedule.weekdays.slice() : [0, 1, 2, 3, 4, 5, 6],
      instructions: plan && plan.instructions ? plan.instructions : "",
      internal_note: plan && plan.internal_note ? plan.internal_note : "",
      start_date: plan ? ymd(plan.start_date) : todayYmd(),
      end_date: plan && plan.end_date ? ymd(plan.end_date) : "",
    };

    var dayBoxes = WEEK_LABELS.map(function (label, idx) {
      var checked = init.weekdays.indexOf(idx) !== -1 ? " checked" : "";
      return "<label style='display:inline-flex;align-items:center;gap:6px;margin:0 14px 8px 0;'>" +
        "<input type='checkbox' data-day value='" + idx + "'" + checked + " />" + label + "</label>";
    }).join("");
    var timeRows = init.times.map(function (t) {
      return timeRowHtml(t);
    }).join("");

    var m = KB.openModal(
      "<h3>" + (isEdit ? "编辑用药计划 · " : "新建用药计划 · ") + KB.esc(elder.name || "长者") + "</h3>" +
      "<div class='form-error' data-err></div>" +
      "<form>" +
      "<div class='field'><label>药名<span class='req'>*</span></label>" +
      "<input data-drug maxlength='160' value='" + KB.esc(init.drug_name) + "' placeholder='如 阿司匹林肠溶片' /></div>" +
      "<div class='field'><label>剂量<span class='req'>*</span></label>" +
      "<input data-dose maxlength='100' value='" + KB.esc(init.dose) + "' placeholder='如 100mg / 1 片' /></div>" +
      "<div class='field'><label>服用时间<span class='req'>*</span></label>" +
      "<div data-times>" + timeRows + "</div>" +
      "<button type='button' class='btn btn-ghost btn-sm' data-add-time>+ 添加时间</button>" +
      "<div class='hint'>24 小时制 HH:MM，至少 1 个、最多 6 个，不能重复</div></div>" +
      "<div class='field'><label>星期</label><div data-days>" + dayBoxes + "</div>" +
      "<div class='hint'>默认每天（全部选中）；至少选一天</div></div>" +
      "<div class='form-row'>" +
      "<div class='field'><label>开始日期<span class='req'>*</span></label>" +
      "<input type='date' data-start value='" + KB.esc(init.start_date) + "' /></div>" +
      "<div class='field'><label>结束日期</label>" +
      "<input type='date' data-end value='" + KB.esc(init.end_date) + "' /></div></div>" +
      "<div class='field'><label>用药说明</label>" +
      "<textarea data-instructions maxlength='2000' placeholder='给家属与护工看的用药说明（可选）'>" +
      KB.esc(init.instructions) + "</textarea></div>" +
      "<div class='field'><label>内部备注</label>" +
      "<textarea data-internal maxlength='2000' placeholder='仅医护可见（可选）'>" +
      KB.esc(init.internal_note) + "</textarea></div>" +
      "<div class='modal-actions'>" +
      "<button type='button' class='btn btn-ghost' data-cancel>取消</button>" +
      "<button type='submit' class='btn btn-primary'>" +
      (isEdit ? "下一步：确认修改" : "下一步：确认下发") + "</button>" +
      "</div></form>"
    );
    m.modal.style.width = "min(680px, 100%)";
    m.modal.querySelector("[data-cancel]").addEventListener("click", m.close);

    var timesBox = m.modal.querySelector("[data-times]");
    m.modal.querySelector("[data-add-time]").addEventListener("click", function () {
      if (timesBox.querySelectorAll("[data-time-row]").length >= 6) {
        KB.toast("最多 6 个服用时间", "error");
        return;
      }
      var wrap = document.createElement("div");
      wrap.innerHTML = timeRowHtml("08:00");
      timesBox.appendChild(wrap.firstChild);
    });
    timesBox.addEventListener("click", function (ev) {
      var del = ev.target.closest ? ev.target.closest("button[data-del-time]") : null;
      if (!del) return;
      if (timesBox.querySelectorAll("[data-time-row]").length <= 1) return;
      del.parentNode.remove();
    });

    m.modal.querySelector("form").addEventListener("submit", function (ev) {
      ev.preventDefault();
      var errBox = m.modal.querySelector("[data-err]");
      errBox.classList.remove("show");

      function fail(message) {
        errBox.innerHTML = "⚠ " + KB.esc(message);
        errBox.classList.add("show");
      }

      var drug = m.modal.querySelector("[data-drug]").value.trim();
      var dose = m.modal.querySelector("[data-dose]").value.trim();
      if (!drug) return fail("请填写药名");
      if (!dose) return fail("请填写剂量");

      var times = [];
      var inputs = timesBox.querySelectorAll("[data-time-row] input");
      for (var i = 0; i < inputs.length; i += 1) {
        var t = String(inputs[i].value || "").slice(0, 5);
        if (!t) continue;
        if (!MIN_TIME.test(t)) return fail("服用时间必须是 24 小时制 HH:MM");
        if (times.indexOf(t) !== -1) return fail("服用时间不能重复");
        times.push(t);
      }
      times.sort();
      if (!times.length) return fail("请至少填写一个服用时间");
      if (times.length > 6) return fail("最多 6 个服用时间");

      var days = [];
      m.modal.querySelectorAll("[data-day]").forEach(function (cb) {
        if (cb.checked) days.push(Number(cb.value));
      });
      if (!days.length) return fail("请至少选择一天");
      days.sort(function (a, b) { return a - b; });

      var start = m.modal.querySelector("[data-start]").value;
      var end = m.modal.querySelector("[data-end]").value;
      if (!start) return fail("请选择开始日期");
      if (end && end < start) return fail("结束日期不能早于开始日期");

      var values = {
        drug_name: drug,
        dose: dose,
        schedule: { times: times, weekdays: days.length === 7 ? [] : days },
        instructions: m.modal.querySelector("[data-instructions]").value.trim() || null,
        internal_note: m.modal.querySelector("[data-internal]").value.trim() || null,
        start_date: start,
        end_date: end || null,
      };

      var submitBtn = m.modal.querySelector("button[type=submit]");

      if (!isEdit) {
        submitBtn.disabled = true;
        KB.api("POST", "/api/v1/elders/" + encodeURIComponent(elder.elder_id) + "/medication-plans", values)
          .then(function (created) {
            submitBtn.disabled = false;
            m.close();
            showConfirmPage(created, {
              elderLabel: elder.name || "长者",
              backLabel: "返回修改",
              // 返回修改：以草稿为基准重新打开编辑表单，避免重复 POST 生成第二条草稿
              onBack: function () { openPlanForm(elder, created, onSaved); },
              onConfirmed: function () { if (onSaved) onSaved(); },
            });
          })
          .catch(function (e) {
            submitBtn.disabled = false;
            fail(KB.errText(e));
          });
        return;
      }

      var patch = diffPatch(plan, values);
      if (!Object.keys(patch).length) {
        KB.toast("未检测到修改", "success");
        return;
      }
      showDiffConfirm(plan, patch, {
        onConfirmed: function () {
          m.close();
          if (onSaved) onSaved();
        },
      });
    });
  }

  function timeRowHtml(value) {
    return "<div data-time-row style='display:flex;gap:8px;align-items:center;margin-bottom:8px;'>" +
      "<input type='time' value='" + KB.esc(value) + "' style='flex:1;' />" +
      "<button type='button' class='btn btn-ghost btn-sm' data-del-time>删除</button>" +
      "</div>";
  }

  function diffPatch(plan, values) {
    var patch = {};
    if (values.drug_name !== plan.drug_name) patch.drug_name = values.drug_name;
    if (values.dose !== plan.dose) patch.dose = values.dose;

    var oldSchedule = plan.schedule || {};
    var oldTimes = (oldSchedule.times || []).slice().sort().join(",");
    var newTimes = values.schedule.times.slice().sort().join(",");
    var oldDays = normalizeDays(oldSchedule.weekdays).join(",");
    var newDays = normalizeDays(values.schedule.weekdays).join(",");
    if (oldTimes !== newTimes || oldDays !== newDays) patch.schedule = values.schedule;

    if ((values.instructions || "") !== (plan.instructions || "")) {
      patch.instructions = values.instructions;
    }
    if ((values.internal_note || "") !== (plan.internal_note || "")) {
      patch.internal_note = values.internal_note;
    }
    if (values.start_date !== ymd(plan.start_date)) patch.start_date = values.start_date;
    if ((values.end_date || "") !== (ymd(plan.end_date) || "")) {
      patch.end_date = values.end_date;
    }
    return patch;
  }

  // ---------- 医生：确认页 / 差异确认 ----------

  function summaryHtml(plan, elderLabel) {
    return "<dl class='kv mt-16'>" +
      "<dt>长者</dt><dd>" + KB.esc(elderLabel) + "</dd>" +
      "<dt>药名</dt><dd>" + KB.esc(plan.drug_name) + "</dd>" +
      "<dt>剂量</dt><dd>" + KB.esc(plan.dose) + "</dd>" +
      "<dt>服用时间</dt><dd>" + KB.esc(timesText(plan.schedule)) + "</dd>" +
      "<dt>星期</dt><dd>" + KB.esc(weekdayText((plan.schedule || {}).weekdays)) + "</dd>" +
      "<dt>起止日期</dt><dd>" + KB.esc(dateRange(plan)) + "</dd>" +
      "<dt>用药说明</dt><dd>" + KB.esc(plan.instructions || "—") + "</dd>" +
      "<dt>内部备注</dt><dd>" + KB.esc(plan.internal_note || "—") + "</dd>" +
      "</dl>";
  }

  function showConfirmPage(plan, opts) {
    var o = opts || {};
    var m = KB.openModal(
      "<h3>确认下发用药计划</h3>" +
      "<div class='warn-line'>⚠ <span>确认后将立即向护工下发长期工单，并开始按时间提醒。</span></div>" +
      summaryHtml(plan, o.elderLabel || "长者") +
      "<div class='form-error' data-err></div>" +
      "<div class='modal-actions'>" +
      "<button class='btn btn-ghost' data-back>" + KB.esc(o.backLabel || "返回修改") + "</button>" +
      "<button class='btn btn-primary' data-ok>确认下发</button>" +
      "</div>"
    );
    m.modal.style.width = "min(680px, 100%)";
    var errBox = m.modal.querySelector("[data-err]");
    m.modal.querySelector("[data-back]").addEventListener("click", function () {
      m.close();
      if (o.onBack) o.onBack();
    });
    var okBtn = m.modal.querySelector("[data-ok]");
    okBtn.addEventListener("click", function () {
      okBtn.disabled = true;
      KB.api("POST", "/api/v1/medication-plans/" + encodeURIComponent(plan.plan_id) + "/confirm")
        .then(function (confirmed) {
          m.close();
          KB.toast("已下发，工单号 " + shortId(confirmed.workorder_id), "success");
          if (o.onConfirmed) o.onConfirmed(confirmed);
        })
        .catch(function (e) {
          errBox.innerHTML = "⚠ " + KB.esc(KB.errText(e));
          errBox.classList.add("show");
          okBtn.disabled = false;
        });
    });
  }

  function showDiffConfirm(plan, patch, opts) {
    var o = opts || {};
    var keys = Object.keys(patch);
    var rows = keys.map(function (field) {
      var before = field === "schedule" ? plan.schedule : plan[field];
      return "<tr><td>" + KB.esc(FIELD_LABEL[field] || field) + "</td>" +
        "<td>" + KB.esc(changeValueText(field, before)) + "</td>" +
        "<td>" + KB.esc(changeValueText(field, patch[field])) + "</td></tr>";
    }).join("");

    var m = KB.openModal(
      "<h3>确认修改用药计划</h3>" +
      "<div class='warn-line'>⚠ <span>确认后将按新方案更新长期用药工单，并同步给家属可见的修改记录。</span></div>" +
      "<div class='table-wrap mt-8'><table class='kb'><thead><tr>" +
      "<th>字段</th><th>原值</th><th>新值</th></tr></thead><tbody>" + rows + "</tbody></table></div>" +
      "<div class='form-error' data-err></div>" +
      "<div class='modal-actions'>" +
      "<button class='btn btn-ghost' data-back>返回修改</button>" +
      "<button class='btn btn-primary' data-ok>确认修改</button></div>"
    );
    m.modal.style.width = "min(680px, 100%)";
    var errBox = m.modal.querySelector("[data-err]");
    m.modal.querySelector("[data-back]").addEventListener("click", m.close);
    var okBtn = m.modal.querySelector("[data-ok]");
    okBtn.addEventListener("click", function () {
      okBtn.disabled = true;
      KB.api("PUT", "/api/v1/medication-plans/" + encodeURIComponent(plan.plan_id), patch)
        .then(function (updated) {
          m.close();
          KB.toast("用药计划已更新", "success");
          if (o.onConfirmed) o.onConfirmed(updated);
        })
        .catch(function (e) {
          errBox.innerHTML = "⚠ " + KB.esc(KB.errText(e));
          errBox.classList.add("show");
          okBtn.disabled = false;
        });
    });
  }

  function actOnPlan(plan, action, reload) {
    var label = { pause: "暂停", resume: "恢复", cancel: "取消" }[action] || action;
    var text = {
      pause: "暂停后护工端将不再收到该计划的服药提醒，可随时恢复。",
      resume: "恢复后将继续按计划时间提醒护工。",
      cancel: "取消后将同时关闭长期用药工单，且不可恢复。",
    }[action] || "";
    KB.confirmModal("确认" + label + "用药计划", text, action === "cancel").then(function (ok) {
      if (!ok) return;
      KB.api("POST", "/api/v1/medication-plans/" + encodeURIComponent(plan.plan_id) + "/" + action)
        .then(function () {
          KB.toast("已" + label, "success");
          reload();
        })
        .catch(function (e) {
          KB.toast(KB.errText(e), "error");
        });
    });
  }

  // ---------- 家属：只读计划 + 修改记录 + 服药历史 ----------

  function mountFamily(opts) {
    var root = document.getElementById((opts || {}).rootId);
    if (!root) return null;
    if (root.__kbMedication) return root.__kbMedication;

    var state = {
      elderId: sessionStorage.getItem("kb_wo_elder") || null,
      planStatus: "",
      plans: [],
      page: 1,
      pageSize: 30,
      total: 0,
      items: [],
      historyLoading: false,
    };

    root.innerHTML =
      "<div class='grid grid-2'>" +
      "<div class='card'>" +
      "<div class='card-title'><h3>用药计划（只读）</h3>" +
      "<span class='faint'>医生确认后生效</span></div>" +
      "<div class='toolbar'>" +
      "<select data-plan-status>" +
      FAMILY_PLAN_FILTERS.map(function (o) {
        return "<option value='" + o[0] + "'>" + o[1] + "</option>";
      }).join("") +
      "</select>" +
      "<span class='spacer'></span>" +
      "<button class='btn btn-ghost btn-sm' data-plan-refresh>刷新</button>" +
      "</div>" +
      "<div class='faint' data-plan-hint></div>" +
      "<div data-plan-list class='mt-8'></div>" +
      "</div>" +
      "<div class='card'>" +
      "<div class='card-title'><h3>服药历史</h3>" +
      "<span class='faint'>按计划时段倒序</span></div>" +
      "<div data-history-list class='mt-8'></div>" +
      "<div class='toolbar' data-history-more style='margin-top:12px;'></div>" +
      "</div>" +
      "</div>";

    var planList = root.querySelector("[data-plan-list]");
    var planHint = root.querySelector("[data-plan-hint]");
    var planStatusSel = root.querySelector("[data-plan-status]");
    var historyList = root.querySelector("[data-history-list]");
    var moreBox = root.querySelector("[data-history-more]");

    function elderBase() {
      return "/api/v1/family/elders/" + encodeURIComponent(state.elderId);
    }

    function loadPlans() {
      if (!state.elderId) {
        planHint.textContent = "";
        emptyState(planList, "请选择长者");
        return Promise.resolve();
      }
      KB.renderState(planList, { kind: "loading" });
      var url = elderBase() + "/medication-plans";
      if (state.planStatus) url += "?status=" + encodeURIComponent(state.planStatus);
      return KB.api("GET", url).then(function (rows) {
        state.plans = rows || [];
        renderPlans();
      }).catch(function (e) {
        planHint.textContent = "";
        KB.renderState(planList, {
          kind: "error", message: KB.errText(e), traceId: e.traceId,
        }, loadPlans);
      });
    }

    function renderPlans() {
      if (!state.plans.length) {
        emptyState(planList, "暂无用药计划");
        return;
      }
      planList.innerHTML = state.plans.map(function (p) {
        var sch = p.schedule || {};
        return "<div class='record-row'><div class='grow'>" +
          "<b>" + KB.esc(p.drug_name) + "</b> " + planStatusTag(p.status) +
          "<div class='muted'>" + KB.esc(p.dose) + " · 服用 " + KB.esc(timesText(sch)) +
          " · " + KB.esc(weekdayText(sch.weekdays)) + "</div>" +
          "<div class='faint'>起止：" + KB.esc(dateRange(p)) + "</div>" +
          (p.instructions
            ? "<div class='faint'>说明：" + KB.esc(p.instructions) + "</div>" : "") +
          "</div>" +
          "<button class='btn btn-ghost btn-sm' data-plan-history='" +
          KB.esc(p.plan_id) + "'>修改记录</button>" +
          "</div>";
      }).join("");
      planList.querySelectorAll("[data-plan-history]").forEach(function (btn) {
        btn.addEventListener("click", function () {
          var pid = btn.getAttribute("data-plan-history");
          var plan = state.plans.find(function (x) { return String(x.plan_id) === pid; });
          if (plan) openPlanHistory(plan);
        });
      });
    }

    function openPlanHistory(plan) {
      KB.api("GET", elderBase() + "/medication-plans/" + encodeURIComponent(plan.plan_id) + "/history")
        .then(function (items) {
          var rows = (items || []).map(historyItemHtml).filter(function (html) {
            return !!html;
          }).join("");
          var m = KB.openModal(
            "<h3>修改记录 · " + KB.esc(plan.drug_name) + "</h3>" +
            "<p class='faint'>仅显示家属可见字段的变更记录</p>" +
            (rows || "<div class='state mt-16'><span class='big'>📝</span>暂无修改记录</div>") +
            "<div class='modal-actions'><button class='btn btn-ghost' data-close>关闭</button></div>"
          );
          m.modal.style.width = "min(680px, 100%)";
          m.modal.querySelector("[data-close]").addEventListener("click", m.close);
        })
        .catch(function (e) {
          KB.toast(KB.errText(e), "error");
        });
    }

    function historyItemHtml(item) {
      var actor = item.actor ? (ROLE_LABEL[item.actor] || "工作人员") : "系统";
      // 服务端已剔除 internal_note；前端再兜底过滤，仅内部备注变更的条目整条不渲染
      var changes = (item.changes || []).filter(function (c) {
        return c && c.field && c.field !== "internal_note";
      });
      var isConfirm = !item.plan_version || item.plan_version <= 1;
      if (!changes.length && !isConfirm) return "";
      var changeHtml = changes.length
        ? "<div class='mt-8'>" + changes.map(function (c) {
            return "<div class='faint'>" + KB.esc(FIELD_LABEL[c.field] || c.field) + "：" +
              KB.esc(changeValueText(c.field, c.before)) + " → " +
              KB.esc(changeValueText(c.field, c.after)) + "</div>";
          }).join("") + "</div>"
        : "<div class='faint mt-8'>确认下发</div>";
      return "<div class='record-row'><div class='grow'>" +
        "<b>" + (item.plan_version ? "v" + item.plan_version : "记录") + "</b> " +
        "<span class='faint'>" + fmtTime(item.time) + " · " + KB.esc(actor) + "</span>" +
        changeHtml + "</div></div>";
    }

    function loadHistory(reset) {
      if (!state.elderId) {
        emptyState(historyList, "请选择长者");
        moreBox.innerHTML = "";
        return Promise.resolve();
      }
      if (reset) {
        state.page = 1;
        state.items = [];
        state.total = 0;
        moreBox.innerHTML = "";
        KB.renderState(historyList, { kind: "loading" });
      }
      var url = elderBase() + "/medication-history?page=" + state.page +
        "&page_size=" + state.pageSize;
      return KB.api("GET", url).then(function (res) {
        var items = (res && res.items) || [];
        state.total = (res && res.total) || 0;
        state.items = reset ? items : state.items.concat(items);
        renderHistory();
      }).catch(function (e) {
        if (reset) {
          KB.renderState(historyList, {
            kind: "error", message: KB.errText(e), traceId: e.traceId,
          }, function () { loadHistory(true); });
        } else {
          KB.toast(KB.errText(e), "error");
        }
      });
    }

    function renderHistory() {
      if (!state.items.length) {
        emptyState(historyList, "暂无服药记录");
        moreBox.innerHTML = "";
        return;
      }
      historyList.innerHTML = state.items.map(function (it) {
        return "<div class='record-row'><div class='grow'>" +
          "<b>" + fmtTime(it.scheduled_at) + "</b> " + orderStatusTag(it.status) +
          "<div class='muted'>" + KB.esc(it.drug_name) + " · " + KB.esc(it.dose) + "</div>" +
          "<div class='faint'>尝试 " + (it.attempt_count || 0) + " 次" +
          (it.confirmed_at ? " · 确认于 " + fmtTime(it.confirmed_at) : "") + "</div>" +
          "<button class='btn btn-ghost btn-sm mt-8' data-events-toggle='" +
          KB.esc(it.order_id) + "'>展开事件</button>" +
          "<div class='mt-8' data-events style='display:none;'></div>" +
          "</div></div>";
      }).join("");

      historyList.querySelectorAll("[data-events-toggle]").forEach(function (btn) {
        btn.addEventListener("click", function () {
          var item = state.items.find(function (x) {
            return String(x.order_id) === btn.getAttribute("data-events-toggle");
          });
          var box = btn.parentNode.querySelector("[data-events]");
          if (!item || !box) return;
          if (box.style.display === "none") {
            box.innerHTML = eventsHtml(item.events);
            box.style.display = "";
            btn.textContent = "收起事件";
          } else {
            box.innerHTML = "";
            box.style.display = "none";
            btn.textContent = "展开事件";
          }
        });
      });

      var hasMore = state.items.length < state.total;
      moreBox.innerHTML = hasMore
        ? "<button class='btn btn-ghost btn-sm' data-more>加载更多（已显示 " +
          state.items.length + "/" + state.total + "）</button>"
        : "<span class='faint'>共 " + state.total + " 条，已全部显示</span>";
      var moreBtn = moreBox.querySelector("[data-more]");
      if (moreBtn) {
        moreBtn.addEventListener("click", function () {
          if (state.historyLoading) return;
          state.historyLoading = true;
          moreBtn.disabled = true;
          state.page += 1;
          loadHistory(false).then(function () {
            state.historyLoading = false;
          }, function () {
            state.historyLoading = false;
            state.page = Math.max(1, state.page - 1);
          });
        });
      }
    }

    function eventsHtml(events) {
      if (!events || !events.length) return "<div class='faint'>暂无事件记录</div>";
      return events.map(function (ev) {
        return "<div class='faint'>" + KB.esc(eventLabel(ev.event_type)) + " · " +
          KB.esc(SOURCE_LABEL[ev.source] || ev.source || "—") + " · " +
          fmtTime(ev.created_at) + "</div>";
      }).join("");
    }

    function refreshAll() {
      loadPlans();
      loadHistory(true);
    }

    planStatusSel.addEventListener("change", function () {
      state.planStatus = this.value;
      loadPlans();
    });
    root.querySelector("[data-plan-refresh]").addEventListener("click", refreshAll);

    // 复用段1.5 长者选择：`kb_wo_elder` + `kb:wo-elder` 事件
    document.addEventListener("kb:wo-elder", function (ev) {
      var id = (ev.detail && ev.detail.elderId) || null;
      if (id === state.elderId) return;
      state.elderId = id;
      state.planStatus = "";
      planStatusSel.value = "";
      refreshAll();
    });

    var api = { refresh: refreshAll };
    root.__kbMedication = api;

    // 同页工单面板若已恢复选择，以其为准（否则沿用 sessionStorage）
    if (window.KBWorkOrders && typeof window.KBWorkOrders.getElderId === "function") {
      var current = window.KBWorkOrders.getElderId();
      if (current) state.elderId = current;
    }

    if (state.elderId) {
      refreshAll();
    } else {
      emptyState(planList, "请选择长者");
      emptyState(historyList, "请选择长者");
    }
    return api;
  }

  window.KBMedication = { openDoctorPlans: openDoctorPlans, mountFamily: mountFamily };
})();
