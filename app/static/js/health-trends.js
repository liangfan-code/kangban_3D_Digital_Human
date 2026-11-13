/* ============================================================
   健康趋势（Day8 8.3，原生 JS）
   - 本地 vendor Chart.js（/vendor/chartjs/chart.umd.min.js），不连外网 CDN
   - 缺库/加载失败时降级为数值表格，不空白
   - 数据：GET /api/v1/elders/{id}/health-trend?days=14
   ============================================================ */

(function () {
  "use strict";

  var DAYS = 14;

  function fmtDate(s) {
    return String(s || "").slice(5); // YYYY-MM-DD → MM-DD
  }

  function fmtSleep(min) {
    if (min === null || min === undefined) return "—";
    var h = Math.floor(min / 60);
    var m = min % 60;
    return h ? h + "h" + m + "m" : m + "m";
  }

  function mount(opts) {
    var root = document.getElementById(opts.rootId);
    if (!root) return null;

    root.innerHTML =
      "<div class='card'>" +
      "<div class='card-title'><h3>健康趋势（近 " + DAYS + " 天）</h3>" +
      "<div class='toolbar'><select data-elder style='min-width:180px;'></select>" +
      "<button class='btn btn-ghost btn-sm' data-refresh>刷新</button></div></div>" +
      "<div data-body class='mt-8'></div></div>";

    var body = root.querySelector("[data-body]");
    var select = root.querySelector("[data-elder]");
    var current = null;
    var seq = 0;      // 竞态守卫：切换长者后，晚到的旧响应直接丢弃
    var chart = null; // Chart.js 实例，重渲染前 destroy 防止监听器累积

    function destroyChart() {
      if (chart) {
        try { chart.destroy(); } catch (e) { /* 已失效则忽略 */ }
        chart = null;
      }
    }

    function tableFallback(points) {
      var head = "<tr><th>日期</th><th>收缩压</th><th>舒张压</th><th>睡眠</th><th>情绪</th></tr>";
      var rows = points.map(function (p) {
        return "<tr><td>" + KB.esc(p.date) + "</td><td>" +
          (p.bp_systolic == null ? "—" : p.bp_systolic) + "</td><td>" +
          (p.bp_diastolic == null ? "—" : p.bp_diastolic) + "</td><td>" +
          fmtSleep(p.sleep_minutes) + "</td><td>" + KB.esc(p.mood || "—") + "</td></tr>";
      }).join("");
      return "<div class='faint' style='margin-bottom:8px;'>图表组件不可用，已降级为数值表格</div>" +
        "<div class='table-wrap'><table class='kb'>" + head + rows + "</table></div>";
    }

    function renderChart(points) {
      destroyChart();
      var labels = points.map(function (p) { return fmtDate(p.date); });
      var sys = points.map(function (p) { return p.bp_systolic; });
      var dia = points.map(function (p) { return p.bp_diastolic; });
      var sleep = points.map(function (p) { return p.sleep_minutes; });

      body.innerHTML =
        "<div class='trend-chart'><canvas height='220'></canvas></div>" +
        "<div class='faint mt-8'>实线为血压（mmHg），柱形为睡眠时长（小时）；缺测日留空。</div>";
      var canvas = body.querySelector("canvas");
      chart = new window.Chart(canvas, {
        type: "line",
        data: {
          labels: labels,
          datasets: [
            {
              label: "收缩压",
              data: sys,
              borderColor: "#8a3b2e",
              backgroundColor: "rgba(138,59,46,.12)",
              tension: 0.25,
              spanGaps: false,
              yAxisID: "y",
            },
            {
              label: "舒张压",
              data: dia,
              borderColor: "#c1873b",
              backgroundColor: "rgba(193,135,59,.12)",
              tension: 0.25,
              spanGaps: false,
              yAxisID: "y",
            },
            {
              label: "睡眠（小时）",
              type: "bar",
              data: sleep.map(function (m) { return m == null ? null : +(m / 60).toFixed(1); }),
              backgroundColor: "rgba(95,122,95,.35)",
              yAxisID: "y1",
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: "index", intersect: false },
          plugins: { legend: { labels: { font: { size: 14 } } } },
          scales: {
            y: { position: "left", title: { display: true, text: "血压 mmHg" } },
            y1: {
              position: "right",
              beginAtZero: true,
              suggestedMax: 12,
              grid: { drawOnChartArea: false },
              title: { display: true, text: "睡眠 小时" },
            },
          },
        },
      });
    }

    function render(points, hasData) {
      if (!hasData) {
        destroyChart();
        KB.renderState(body, { kind: "empty", message: "近 14 天暂无健康采集数据" });
        return;
      }
      if (window.Chart) {
        try {
          renderChart(points);
          return;
        } catch (e) {
          destroyChart();
          // 图表初始化异常时降级
        }
      }
      destroyChart();
      body.innerHTML = tableFallback(points);
    }

    function load(elderId) {
      if (!elderId) return;
      current = elderId;
      var mySeq = ++seq;
      destroyChart();
      KB.renderState(body, { kind: "loading" });
      KB.api("GET", "/api/v1/elders/" + elderId + "/health-trend?days=" + DAYS)
        .then(function (res) {
          if (mySeq !== seq) return;
          render(res.series || [], res.has_data);
        })
        .catch(function (e) {
          if (mySeq !== seq) return;
          var msg = e.status === 403 ? "暂无该长者的健康数据授权" : KB.errText(e);
          KB.renderState(body, { kind: "error", message: msg, traceId: e.traceId }, function () {
            load(current);
          });
        });
    }

    select.addEventListener("change", function () { load(select.value); });
    root.querySelector("[data-refresh]").addEventListener("click", function () { load(current); });

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

    return { refresh: function () { load(current); } };
  }

  window.KangbanTrend = { mount: mount };
})();
