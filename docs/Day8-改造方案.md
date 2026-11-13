# Day8 改造方案：健康采集决策图 + 三端后台 + 声纹评测

> 记录日期：2026-11-13
> 依据：`docs/康伴3D数字人_大纲.md` Day8
> 前置：Day1–Day7 已完成（当前迁移 `0012_conversation_audit`，全量测试全绿）
> 已拍板：① 采集触发=定时主动+工具被动；② 图表=本地 vendor Chart.js；③ 8.4=脚本+报告+只读页（不改实时声纹链路）

## 一、目标

一条可演示的健康闭环：每晚主动 5 问采集（长者白天说"我头晕"也走同一张图）→ 规则三级分级 → 绿入库 / 黄建单等护工确认 / 红建紧急单 + 医院 mock 转诊等医生确认（HITL）→ 护士/医生看板待办 → 医生 14 天趋势图 + 家属日报 H5 可播报 → 声纹阈值离线评测报告。

## 二、现状要点（已核实）

| 事实 | 证据 |
| --- | --- |
| `health_records` 表结构齐全但**无任何写入方** | `app/models/health.py` |
| 健康档案 `ElderHealthRecord` 已有建档/摘要/AI 分析/导出/授权/审计 | `app/services/health_profile.py`、`app/api/routes/health_profile.py` |
| 用药图是现成 LangGraph 范式（State/节点/条件边/AsyncSqliteSaver） | `app/graphs/medication.py`、`app/graphs/checkpointer.py` |
| 工单 `type` 为自由字符串（sensitive/medication_plan/missed_medication），无 CHECK | `app/services/workorder.py` |
| 护工楼栋范围查询已有实现 | `app/services/workorder.py`、`app/services/health_profile.py` |
| Agent 工具注册于 `build_default_registry`，elder_id 来自 ToolContext | `app/agent/tools.py` |
| 前端四端页面与 `js/api.js`（KB.*）齐备，vendor 下无图表库 | `app/static/**` |
| 依赖已含 langgraph/aiosqlite/apscheduler/httpx，无需新增 pip 包 | `pyproject.toml` |

## 三、分级规则（确定性，不经 LLM）

| 级别 | 条件 | 处置 | 待确认角色 |
| --- | --- | --- | --- |
| 红 | 收缩压 ≥180 或 舒张压 ≥110；或主诉命中 胸痛/胸闷/呼吸困难/喘不上/晕倒/摔倒/意识不清/说不出话 | 建紧急工单 + 医院 mock 转诊 + 事件 pending | 医生 |
| 黄 | 收缩压 160–179 或 舒张压 100–109；或睡眠 <300 分钟；或情绪 ∈ 很差/低落/烦躁/睡不着 | 建 `health_alert` 工单 + 事件 pending | 护士 |
| 绿 | 其余 | 写 `health_records` 并归档 | — |

措辞只做转述与建议求助，禁用诊断词（高血压/糖尿病/冠心病等），由测试断言。

## 四、接口契约

| 方法与路径 | 请求 | 响应 / 错误 |
| --- | --- | --- |
| `POST /api/v1/elders/{elder_id}/health-events` | `{source, bp_systolic?, bp_diastolic?, sleep_minutes?, mood?, chief_complaint?}` | `{event_id, thread_id, level, status, reply, required_role, missing_fields}`；缺字段 `status="collecting"` |
| `GET /api/v1/health-events/pending?page=` | 分页 | `{items:[{event_id, elder_id, elder_name, room_label, level, status, summary, required_role, created_at}],total}` |
| `POST /api/v1/health-events/{event_id}/decision` | `{action:"confirm"\|"escalate"\|"reject", note?}` | `{event_id, status, level, workorder_id, referral_id}`；已决策 409；越权 403 |
| `GET /api/v1/health-events/{event_id}` | — | 事件快照 |
| `GET /api/v1/elders/{elder_id}/health-trend?days=14` | 1–30 | `{days, has_data, series:[{date, bp_systolic, bp_diastolic, sleep_minutes, mood}]}`；缺测日 `null` |
| `GET /api/v1/elders/{elder_id}/health-daily-report?date=&tts=1` | 缺省今天 | `{date, elder_name, text, audio_pcm_base64, audio_sample_rate, tts_degraded}` |
| `GET /api/v1/admin/voiceprint-eval/latest` | — | 最新报告 JSON（`generated_utc, provider, sample_count, threshold_table`）；无报告 404 |

## 五、分段计划（每段 TDD → 自审 → 用户审查 → 下一段）

### 段 1（课时 8.1）
- `app/services/health_triage.py`：`triage(...) -> TriageResult(level, reasons, confidence, advice, missing_fields)`
- `app/models/health_event.py` + `alembic/versions/0013_health_events.py`（新表 + 1 唯一索引 + 2 查询索引，不改既有列）
- `app/services/health_events.py`：create/submit_answers/list_pending/decide/get_event；`visible_elder_ids` 由 workorder 私有函数提升为公开并复用
- `app/graphs/health.py`：thread_id=`health:{elder_id}:{YYYY-MM-DD}`；绿/黄/红三分支；**HITL 用 HTTP decision 接口 + `health_events.status` 权威状态，不使用 `interrupt()` / `Command(resume=...)`**（与 `app/graphs/medication.py` 既有约定一致，数据库为唯一权威）
- `app/services/hospital.py`：Mock 默认、Http 仅在 `hospital_endpoint` 非空启用，失败可见降级
- `app/api/routes/health_events.py` + router 注册
- `app/services/outbound.py`：`trigger_health_intake_call(elder_id)`（同设备已有外呼跳过；仅 WS TTS 引导语，不新建 RTC）
- `app/agent/tools.py`：注册 `health_check` 工具（缺字段追问，齐字段分级）
- `app/services/health_scheduler.py`：每日 `health_collect_hour`（默认 21）批量外呼，job 注册在既有 `medication_scheduler` 上（不新建第二个 APScheduler）
- `app/core/config.py`：`health_collect_hour=21`、`health_collect_enabled=True`、`hospital_endpoint=None`（无 checkpointer，故不设 `health_checkpoint_path`）

测试：`test_health_triage.py`、`test_health_events_api.py`、`test_health_scheduler.py`、`test_health_tool.py`（图内三分支由事件 API 测试覆盖，不单列 `test_health_graph.py`）

### 段 2（课时 8.2）
- `app/static/js/health-events.js`（`window.KangbanHealth.mount/refresh/decide`）+ `nurse.html` / `doctor.html` 接入 `#health-events` 区块 + `main.css` 三级色标
- 验收：护士 A 只见本楼栋；确认/升级/驳回后卡片消失；403/409 有可见提示

### 段 3（课时 8.3）
- 后端：`app/services/health_reports.py`（`build_trend` 14 天按 `app_timezone` 自然日聚合、同日取最后、缺日 `null`；`build_daily_report` 文本 ≤200 字无诊断词；`synthesize_report_audio` 复用 TTS Provider，失败返回 `None`）+ 路由 `/health-trend`、`/health-daily-report`
- 测试：`tests/test_health_reports.py`
- 前端：本地 vendor Chart.js（`app/static/vendor/chartjs/chart.umd.min.js`，禁止外网 CDN；下载受限则降级数值表格）→ `app/static/js/health-trends.js` + `doctor.html` 趋势卡；`family.html` 日报卡（≥20px、375px 不横滚、可播报）

### 段 4（课时 8.4）
- `scripts/voiceprint_eval.py`：读 `data/voiceprint_eval/manifest.json`（`{"pairs":[{"elder_id","same_speaker","audio_a","audio_b"}]}`，音频由用户自备、不入库）；阈值 0.50–0.90 逐档算 FAR/FRR/EER → `data/reports/voiceprint_eval_<UTC>.json`；Provider 未配置退码 2
- `app/api/routes/voiceprint_eval.py`：`GET /api/v1/admin/voiceprint-eval/latest`（admin only，无报告 404）；`app/core/config.py` 新增 `voiceprint_eval_report_dir="data/reports"`
- `admin.html` 只读 tab；测试 `tests/test_voiceprint_eval.py`
- 不改 `app/services/voice.py` 实时声纹判定链路

## 六、安全边界

1. 只做记录、分级、转人工；不做诊断、不给用药建议，不替代 SOS。
2. `elder_id` 只来自服务端上下文（ToolContext / 路径参数 + 权限校验），不接受模型或前端传参。
3. 原始音频不落盘、不入日志；健康事件只记结构化数值与理由。
4. 不新增 pip 依赖；不连接生产库；不回改已执行迁移；不 git push。

## 七、验收清单

1. 180/110 → 事件 pending（医生）→ 确认后状态流转 + mock 转诊号。
2. 160/100 → pending（护士）→ 确认后工单进入处理中。
3. 130/80 + 睡眠 420 → 直接归档、无工单、`health_records` 有记录。
4. 护士 A 看不到 B 楼栋待办。
5. 医生 14 天折线可加载；家属 H5 375px 无横向滚动、可播报。
6. 声纹评测产出 FAR/FRR 表；管理端只读卡展示，文件缺失显示"尚未评测"。
7. 每段完成后全量 pytest 全绿、ruff 全过。
