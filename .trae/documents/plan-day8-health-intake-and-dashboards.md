# Day8 健康采集与看板 — 项目计划（含课时拆分与提示词文档要求）

> 本文件是 Day8 的唯一执行计划。执行时严格按课时顺序推进，每课时结束跑目标测试；最后一课时做全量测试。
> 配套团队文档：`docs/Day8-改造方案.md`、`docs/Day8-改造提示词.md`（段 0 产出，段 1 起随实现同步修订）。

---

## 1. 目标与交付边界

**目标**：在既有 FastAPI + SQLAlchemy async + LangGraph 框架内，交付「健康采集 → 三级分级 → 人工确认 → 看板/趋势/家属日报」闭环，并为 8.4 提供声纹阈值评测工具与只读页。

**交付物**

| 编号 | 交付物 | 形态 |
| --- | --- | --- |
| 8.1 | 健康事件决策图 + 夜间采集 + Agent 工具 + 事件 API | 后端 |
| 8.2 | 护工/医生健康预警看板 | 前端接入 |
| 8.3 | 14 天趋势图 + 家属日报（含可选 TTS） | 后端 + 前端 |
| 8.4 | 声纹 FAR/FRR 评测脚本 + 报告 + 管理端只读页 | 脚本 + API + 前端 |
| 终验 | 全量 pytest + ruff + 迁移 + 前端语法/冒烟 | 验证 |

**不做**：医疗诊断、SOS 替代、真实医院对接（默认 mock）、真实长者数据、模型权重下载、`git push`。

---

## 2. 现状核实（执行前必读）

### 2.1 已完成（段 1 主体，代码已落地）

| 文件 | 状态 |
| --- | --- |
| `app/services/health_triage.py` | 纯规则分级：179/180、109/110、299/300 边界；红色优先；缺字段追问；`DIAGNOSIS_WORDS` 白名单 |
| `app/models/health_event.py` + `app/models/enums.py` | `HealthEvent` 模型；`HealthEventLevel/Status/Source` 枚举 |
| `app/models/__init__.py` | 已登记 `HealthEvent` |
| `alembic/versions/0013_health_events.py` | 新建表 + `ux_health_events_thread` / 两个查询索引 |
| `app/graphs/health.py` | LangGraph 三分支（green/yellow/red），未装 LangGraph 时直接分派 |
| `app/services/health_events.py` | 同日幂等、答案合并、升级重开、待确认分页、人工决策 |
| `app/services/hospital.py` | Mock 转诊 + 可选 HTTP 适配器，失败抛 `HospitalUnavailable` |
| `app/services/workorder.py` | `visible_elder_ids` 公开化；`create_health_alert_work_order`（dedupe_key 幂等） |
| `app/api/routes/health_events.py` + `app/schemas/health_event.py` | 4 个接口已注册到 `/api/v1` |
| `app/services/outbound.py` | `trigger_health_intake_call`（WS TTS 引导语，不新建 RTC/逐题状态机） |
| `app/agent/tools.py` | `health_check` 工具已注册 |
| `app/services/health_scheduler.py` + `app/core/lifespan.py` | 夜间 job 注册到既有调度器（不新建第二个 APScheduler） |
| `app/core/config.py` | `health_collect_hour=21`、`health_collect_enabled=True`、`hospital_endpoint=None` |
| 测试 | `test_health_triage.py`、`test_health_events_api.py`、`test_health_tool.py`、`test_health_scheduler.py` 全绿 |

### 2.2 与批准计划的实现差异（已在代码注释与交付说明中记录）

1. **HITL 不用 `interrupt()` / `Command(resume=...)`**：沿用 `app/graphs/medication.py` 既有约定，改为「HTTP decision 接口 + `health_events.status` 权威」，图只做一次判定。理由：跨请求恢复不依赖进程内 checkpoint，符合「数据库为权威」。
2. **不新增第二个调度器**：`medication_scheduler` 注释明确「长期记忆维护复用同一调度器」；健康采集按同一原则注册 job。
3. **不新增 `health_checkpoint_path`**：本方案无 checkpointer，避免无用配置。

### 2.3 前端可复用

| 现有 | 复用点 |
| --- | --- |
| `app/static/js/api.js` | `KB.api`、`KB.renderState`、`KB.esc`、`KB.toast`、`KB.errText`、`KB.logout` |
| `nurse.html` / `doctor.html` | 均有 `#elder-cards`、`#workorders`、占位卡片与 `api.js`/`workorders.js`/`medication.js` 引入 |
| `family.html` | 有 `#family-medication` 卡位，可直接加日报区 |
| `admin.html` | 已有 tab 结构（`#kb-tabs`），可加「声纹评测」只读 tab |
| `main.css` | CSS 变量 `--moss/--amber/--lacquer/--clay/--state-bg/--ink-soft/--line/--card` |

---

## 3. 接口契约（冻结）

| # | 方法/路径 | 角色 | 说明 | 状态 |
| --- | --- | --- | --- | --- |
| 1 | `POST /api/v1/elders/{elder_id}/health-events` | nurse/doctor/admin | 提交采集；缺字段返回 `collecting` + `reply` | 已实现 |
| 2 | `GET /api/v1/health-events/pending?page=&page_size=` | nurse/doctor/admin | 待确认列表（护士楼栋/医生关联/管理员全量） | 已实现 |
| 3 | `POST /api/v1/health-events/{event_id}/decision` | nurse/doctor/admin | `confirm/escalate/reject`；越权 403、重复 409 | 已实现 |
| 4 | `GET /api/v1/health-events/{event_id}` | nurse/doctor/admin | 事件快照（reasons/转诊单/确认人） | 已实现 |
| 5 | `GET /api/v1/elders/{elder_id}/health-trend?days=14` | nurse/doctor/admin；家属需 HealthGrant | 每日聚合点（缺测日为 `null`） | 段 3 新增 |
| 6 | `GET /api/v1/elders/{elder_id}/health-daily-report?date=&tts=1` | nurse/doctor/admin；家属需 HealthGrant | 日报文本；`tts=1` 时可选 PCM | 段 3 新增 |
| 7 | `GET /api/v1/admin/voiceprint-eval/latest` | admin | 最新评测报告 JSON | 段 4 新增 |

错误信封沿用全局：`{"error":{"code","message","trace_id"}}`（detail 落在 `message`，`code` 多为 `HTTP_ERROR`）。

---

## 4. 课时分段（按顺序执行，每段独立可验收）

### 段 0：方案与提示词文档（已完成，段 3/4 前需补一次）

- `docs/Day8-改造方案.md`：目标、现状、分级规则表、接口契约、安全边界、验收清单。
- `docs/Day8-改造提示词.md`：每课时一节，固定结构「背景 / 目标 / 涉及文件 / TDD 顺序 / 验收 / 自审要点 / 审查门」。
- **段 3 开始前**：把接口 5/6 的最终字段补进两份文档；**段 4 开始前**：补接口 7 与脚本用法。

### 段 1：健康事件决策图 + 夜间采集 + Agent 工具 + API（代码已完成，剩余收尾）

**收尾任务**
1. `ruff check .`（全仓）清零。
2. `alembic upgrade head` 在本地 `kangban_dev` 执行 0013，并确认 `downgrade` 可回滚。
3. 全量 `pytest`，确认既有测试无回归。
4. `docs/Day8-改造方案.md` 记录 2.2 的三条实现差异。

**审查门**：`app/graphs/health.py`、`alembic/versions/0013_health_events.py`、`app/api/routes/health_events.py`、`app/agent/tools.py` 必须人工 review diff。

### 段 2：护工/医生健康预警看板（前端）

**新增** `app/static/js/health-events.js`
- `window.KangbanHealth = { mount(), refresh(), decide(id, action) }`，仅用 `KB.*`。
- 列表：级别色块（绿/黄/红）、长者名、房间床号、摘要、时间、`required_role` 文案。
- 操作：`确认 / 升级 / 驳回`，二次确认用 `KB.confirmModal`（若不存在用 `window.confirm` 兜底）；403/404/409 用 `KB.errText` 展示并刷新。
- 分页：`page` 游标 + 「加载更多」。

**修改**
- `nurse.html`：`#workorders` 后新增 `<section id="health-events" class="mt-24"></section>`，引入 `health-events.js`。
- `doctor.html`：同样加区域；红色事件高亮，显示「待医生确认」。
- `main.css`：新增 `.health-item`、`.health-level-green/-yellow/-red`、适老字号 ≥20px。

**验收**：`node --check app/static/js/health-events.js`；浏览器手测护士确认黄色、医生升级红色、409 提示。

### 段 3：14 天趋势 + 家属日报（后端 + 前端）

**后端**
- `app/services/health_reports.py`
  - `build_trend(session, elder_id, days=14)`：按 `app_timezone` 自然日聚合 `health_records`；同日多条取最后一次；缺测日补 `null`；返回 `{date, bp_systolic, bp_diastolic, sleep_minutes, mood}`。
  - `build_daily_report(session, elder_id, day)`：最新血压/睡眠/情绪/主诉 + 当日健康事件级别 + 当日漏服工单数；文本 ≤200 字，无诊断词。
  - `synthesize_report_audio(text)`：复用 `get_tts_provider()`；失败返回 `None`，由路由标 `tts_unavailable`。
- `app/api/routes/health_reports.py`：接口 5/6；长者范围用 `get_elder_for_user`；家属访问先过 `viewer_level`（`confidential/basic`），无授权 403。
- `app/schemas/health_report.py`：趋势点、日报响应（`text`、`audio_pcm_base64`、`audio_sample_rate`、`tts_degraded`）。

**前端**
- `app/static/vendor/chartjs/chart.umd.min.js`：构建期一次性本地 vendor（禁止外网 CDN）。若下载受限，退化方案：趋势区只渲染数值表格，图表容器显示「图表库未就绪」；接口与页面结构不变。
- `app/static/js/health-trends.js`：`KangbanTrend.mount(el, elderId)`，用 Chart.js 画双折线（收缩/舒张）+ 睡眠柱；无数据/缺库时降级表格。
- `family.html`：新增「今日健康日报」卡片；「朗读」按钮请求 `?tts=1`，用 `AudioContext` 播放 16-bit PCM（参考 `voice.js` 的播放实现）；不可用时按钮禁用并提示。
- `doctor.html`：长者详情/卡片处接入 `KangbanTrend`。

**测试** `tests/test_health_reports.py`
- 14 天窗口、缺测日补空、同日取最后一读；
- 日报含数值与「已确认/待确认」状态，不含诊断词；
- 家属无 HealthGrant → 403；有 `basic` → 200；
- TTS 抛错 → `tts_degraded="tts_unavailable"` 且文本仍返回。

### 段 4：声纹 FAR/FRR 评测（脚本 + 报告 + 只读页）

**脚本** `scripts/voiceprint_eval.py`
- 输入 `data/voiceprint_eval/manifest.json`：`{"pairs":[{"elder_id","same_speaker","audio_a","audio_b"}]}`；用户自备本地录音，仓库不提交音频。
- 用 `get_voiceprint_provider()`（未配置则退出码 2，打印可操作提示，不静默成功）。
- 阈值 0.50–0.90 逐档计算 FAR/FRR/EER，写 `data/reports/voiceprint_eval_<UTC时间>.json`（含样本数、provider、模型指纹、阈值表）。
- 只打印聚合指标，不打印音频路径与长者姓名。

**后端** `app/api/routes/voiceprint_eval.py`
- 接口 7：admin only；读取 `Settings.voiceprint_eval_report_dir` 下最新 JSON；无报告 404。
- `app/core/config.py` 新增 `voiceprint_eval_report_dir: str = "data/reports"`。

**前端** `admin.html`
- `#kb-tabs` 增加「声纹评测」tab；只读表格（阈值/FAR/FRR），显示生成时间与样本数；无报告显示空态说明。

**测试** `tests/test_voiceprint_eval.py`
- 用 fake provider 固定余弦分数，验证 FAR/FRR 计算；
- 报告目录取最新文件、无文件 404、非 admin 403。

### 终验（段 5）

```powershell
python -m alembic upgrade head
python -m pytest
ruff check .
node --check app/static/js/health-events.js
node --check app/static/js/health-trends.js
```

+ 浏览器冒烟清单：
1. 护士登录 → 看到黄色预警 → 确认 → 工单变处理中；
2. 医生登录 → 看到红色预警 → 升级 → 状态 `escalated`；
3. 家属登录 → 看到当日日报 → 点朗读（TTS 可用则播放，不可用有提示）；
4. 管理员 → 声纹评测 tab 显示最新报告或空态。

---

## 5. 关键决策（执行时不得再改）

1. 分级纯规则，红色优先，缺字段继续问；红色不因缺字段延误。
2. HITL 走 HTTP 决策 + DB 状态，不用 `interrupt()`。
3. 夜间采集复用既有调度器；单长者失败不终止整轮。
4. 图表必须本地 vendor，不接外网 CDN；缺库时降级表格。
5. 医院默认 Mock；真实 endpoint 仅由环境变量开启。
6. `elder_id` 只来自路径/服务端上下文，工具参数中的 `elder_id` 忽略。
7. 日志只记 id/level/status/计数，不记音频、不记长者原话。

---

## 6. 安全与审查门

- 高风险文件（生成后人工 review diff）：`app/graphs/health.py`、`alembic/versions/0013_health_events.py`、`app/api/routes/**`、`app/agent/tools.py`、`app/services/outbound.py`。
- 不连生产库；测试只写 `kangban_test`。
- 不提交 `data/voiceprint_eval/**` 与 `data/reports/**` 中的真实录音/报告（加入 `.gitignore` 或放临时目录）。
- 不 `git push`。

---

## 7. 风险与回退

| 风险 | 处置 |
| --- | --- |
| Chart.js 下载受限 | 趋势区降级数值表格，接口不变 |
| TTS 未配置/失败 | 日报纯文本 + `tts_degraded` |
| 声纹 Provider 未配置 | 评测脚本退出码 2，页面显示「暂无报告」 |
| LangGraph 未安装 | 决策图直接分派，行为一致（已实现） |
| 既有迁移已执行 | 只新增 0013，不回改历史迁移 |

---

## 8. 验证清单（完成定义）

- [ ] 段 1 收尾：ruff 全仓通过、全量 pytest 通过、0013 在 dev 库 upgrade/downgrade 正常。
- [ ] 段 2：看板可确认/升级/驳回；权限与 409 表现正确；JS 语法检查通过。
- [ ] 段 3：趋势与日报接口测试通过；家属鉴权正确；图表/降级路径可用。
- [ ] 段 4：评测脚本可复算 FAR/FRR；只读页与 admin 鉴权通过。
- [ ] 终验：全量 pytest + ruff + 前端语法 + 浏览器冒烟清单完成。
