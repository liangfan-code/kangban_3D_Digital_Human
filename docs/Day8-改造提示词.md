# Day8 改造提示词（按课时分段执行）

> 配套文档：`docs/Day8-改造方案.md`（分级规则、接口契约、验收清单）
> 使用方式：每课时一段，复制对应提示词给 AI；**一次只做一个课时**，本课时全量测试通过并人工审查 diff 后才进入下一课时。
> 通用红线：不新增 pip 依赖；不连生产库；不回改已执行迁移；不 git push；不使用真实长者数据；不做医疗诊断；`elder_id` 只取服务端上下文。

## 通用验收命令

```powershell
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m pytest -q
node --check app/static/js/<改动文件>.js
```

---

## 课时 8.1 提示词：健康事件决策图 + 夜间采集 + Agent 工具

```
背景：康伴 3D 数字人项目，Day8 课时 8.1。请严格按 docs/Day8-改造方案.md 第三节分级规则与第四节接口契约实现，不要自行改规则阈值。
目标：新增健康事件决策图（LangGraph），支持绿/黄/红三级分级、HTTP 人工决策（HITL）、医院 mock 转诊；夜间定时主动采集 + 主控 Agent 工具被动采集。

涉及文件（新增/修改）：
- 新增 app/services/health_triage.py（纯规则，先写测试）
- 新增 app/models/health_event.py、alembic/versions/0013_health_events.py（只新增表+索引）
- 新增 app/services/health_events.py、app/graphs/health.py、app/services/hospital.py
- 新增 app/api/routes/health_events.py，并在 app/api/router.py 注册
- 修改 app/services/outbound.py（新增 trigger_health_intake_call）、app/agent/tools.py（注册 health_check）
- 新增 app/services/health_scheduler.py；修改 app/core/config.py（health_collect_hour / health_collect_enabled / hospital_endpoint）
- 将 app/services/workorder.py 的 _visible_elder_ids 提升为公开 visible_elder_ids 并替换内部调用

TDD 顺序：
1. 先写 tests/test_health_triage.py（179/180、109/110、299/300 边界、主诉关键词、红色优先、advice 无诊断词），运行确认失败。
2. 实现 health_triage.py 至该测试全绿。
3. 写 models + 迁移 0013，alembic upgrade head，确认 0013。
4. 写图与事件服务（绿入库无工单 / 黄建单+pending 护士 / 红紧急单+referral+pending 医生 / 同日重入幂等），注意：节点必须是 async 函数，条件边映射要含 green 键。
5. 写 tests/test_health_events_api.py（角色权限、楼栋隔离、重复决策 409、越权 403）、tests/test_health_scheduler.py、tests/test_health_tool.py，再实现 API、scheduler、工具。

验收：
- alembic current == 0013
- 目标测试全绿 + 全量 pytest 全绿 + ruff 全过

自审要点：分级不调 LLM；advice 无诊断词；elder_id 不从工具入参取；外呼失败可见降级不静默；工单 type 用 health_alert；HITL 只用 HTTP decision + DB 状态，禁止使用 interrupt()/Command(resume=...)；不新建第二个 APScheduler；不修改 app/services/voice.py。
交付后：列出改动文件与 diff 摘要，等待人工审查再进入 8.2。
```

---

## 课时 8.2 提示词：护工看板与医生待办

```
背景：Day8 8.2，后端 8.1 已完成并冻结接口。只做前端，不改后端行为。
目标：护士/医生页面新增「健康告警待复核」区块，展示待确认事件并可确认/升级。

涉及文件：
- 新增 app/static/js/health-events.js，暴露 window.KangbanHealth = { mount, refresh, decide }
- 修改 app/static/nurse.html（新增 <section id="health-events"> + 引入脚本）
- 修改 app/static/doctor.html（同区块，红色事件高亮 + 「待医生确认」）
- 修改 app/static/css/main.css（.health-item 与三级色标，字号 ≥20px）

接口：GET /api/v1/health-events/pending?page=&page_size=；POST /api/v1/health-events/{event_id}/decision（confirm/escalate/reject）。

实现要求：
- 复用 KB.api / KB.renderState / KB.esc / KB.toast / KB.errText / KB.confirmModal，不自造请求封装；
- 级别色标：红=var(--lacquer)、黄=var(--amber)、绿=var(--moss)；
- 空态「暂无待复核告警」，403 显示「无权限查看」，不静默；
- 决策后局部刷新（或整段 refresh）；防重复点击；409 提示后用 KB.errText 展示并刷新。

验收：
- node --check app/static/js/health-events.js
- 全量 pytest 全绿（应无后端改动）
- 浏览器手测：护士 A 只见本楼栋；确认后卡片消失
```

---

## 课时 8.3 提示词：医生趋势图 + 家属日报 H5

```
背景：Day8 8.3。后端新增两个只读接口，前端两处卡片；图表使用本地 vendor Chart.js（禁止 CDN）。
目标：医生可看长者近 14 天血压/睡眠折线；家属可在 H5 看今日日报并语音播报。

后端步骤（TDD）：
1. tests/test_health_reports.py：14 天窗口、缺日补 null、同日取最后一读、无数据 has_data=false、家属无 HealthGrant→403、有 basic→200。
2. 新增 app/services/health_reports.py：build_trend / build_daily_report / synthesize_report_audio；路由 GET /elders/{id}/health-trend?days=14、GET /elders/{id}/health-daily-report?date=&tts=1；schemas 用 app/schemas/health_report.py。

前端步骤：
3. 下载 Chart.js 到 app/static/vendor/chartjs/chart.umd.min.js（禁止外网 CDN），校验文件 >100KB；下载受限则中止该步并在趋势区渲染降级数值表格。
4. 新增 app/static/js/health-trends.js（window.KangbanTrend.mount(el, elderId)，双折线 + 睡眠柱，无数据/缺库降级表格）；doctor.html 新增「健康趋势」卡。
5. family.html 新增「今日日报」卡（字号 ≥20px、朗读按钮请求 ?tts=1 并播放 PCM、不可用则禁用并提示、375px 不横向滚动）。

验收：
- 全量 pytest 全绿 + ruff 全过 + node --check
- 浏览器：医生趋势图加载；家属页 375px 无横向滚动、可播报
```

---

## 课时 8.4 提示词：声纹阈值评测 + 报告 + 管理端只读页

```
背景：Day8 8.4。用户已选方案 1：不改实时声纹判定链路（禁止修改 app/services/voice.py）。
目标：离线评测阈值并与三档身份级联说明一起成文；管理端只读展示评测结果。

步骤：
1. 新增 scripts/voiceprint_eval.py：读 data/voiceprint_eval/manifest.json（{"pairs":[{"elder_id","same_speaker","audio_a","audio_b"}]}），用 get_voiceprint_provider() 提向量；Provider 未配置打印可操作提示并以退出码 2 结束（不静默成功）。
2. 阈值 0.50–0.90 步长 0.05，计算 FAR/FRR/EER；只打印聚合指标，不打印音频路径与长者姓名；报告写 data/reports/voiceprint_eval_<UTC>.json。
3. tests/test_voiceprint_eval.py：fake provider 固定余弦分数验证 FAR/FRR、报告目录取最新、无文件 404、非 admin 403。
4. 新增 GET /api/v1/admin/voiceprint-eval/latest（仅 admin）；app/core/config.py 新增 voiceprint_eval_report_dir="data/reports"；admin.html 新增只读 tab（阈值/FAR/FRR、生成时间、样本数，缺失显示「尚未评测」）。
5. 不改 app/services/voice.py；不提交 data/voiceprint_eval/** 与 data/reports/**。

验收：
- python scripts/voiceprint_eval.py 在有 manifest 时正常产出；无 Provider 时退码 2 且有提示
- 全量 pytest 全绿 + ruff 全过 + node --check（如改 js）
- 管理端 tab 显示阈值/FAR/FRR 表；删除报告后显示「尚未评测」
```
