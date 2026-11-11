# Day7 分段实施计划（记忆 / 用药 / 娱乐 / Iter3 基线）

## 0. 摘要

依据 `docs/Day7-改造方案.md`（533 行）与 `docs/Day7-改造提示词.md`（1215 行，含 8 段完整提示词），按 **段 1 → 1.5 → 2 → 3 → 4 → 5 → 6 → 7** 顺序实施。

- 执行方式：**逐段暂停审查**。每段完成并通过该段验收后，向用户提交改动清单、验收命令与真实输出；用户确认后再进入下一段。
- 依赖策略：**允许联网安装**。`langgraph` / `langgraph-checkpoint-sqlite` / `aiosqlite` 在段 3 首次需要时安装；`apscheduler`（`infra` 可选组）在段 4 安装。
- Git 边界：**不执行 `git commit` / `git push` / `git tag`**，由用户自行提交。
- 文档优先：提示词文档是该段规格权威；若实际代码与文档不一致，**先报告差异再按代码调整**，差异写入该段交付说明，不按文档硬改。
- 安全边界：遵守 `AGENTS.md` 安全禁区；不连生产库、不写真实长者数据、不把对话原文/音频写日志、`elder_id` 只来自服务端上下文。

## 1. 现状核验（已读代码确认）

| 事实 | 证据 |
| --- | --- |
| 最新迁移为 `0007_workorder_flow.py` | `alembic/versions/` 仅 0001–0007 |
| `medication_plans` 只有基础字段 | [medication.py](file:///C:/Users/lmxyrqlxr/Desktop/kangban_3D_Digital_Human-main/app/models/medication.py#L21-L54)：无 created_by/confirmed/workorder_id/internal_note |
| `WorkorderEvent` 事件类型仅 5 种 | [workorder.py](file:///C:/Users/lmxyrqlxr/Desktop/kangban_3D_Digital_Human-main/app/models/workorder.py#L146-L174)：`created/read/disposition/closed/occurrence` |
| 处理项角色仅 doctor/nurse；无 admin 处理项 | [workorder.py](file:///C:/Users/lmxyrqlxr/Desktop/kangban_3D_Digital_Human-main/app/models/workorder.py#L106-L143) |
| 工单推送在 `flush()` 后、commit 前 | [workorder.py](file:///C:/Users/lmxyrqlxr/Desktop/kangban_3D_Digital_Human-main/app/services/workorder.py#L192-L209)、`create_sensitive_work_order`/`mark_read` 等 |
| WS 仅 doctor/nurse；管理员广播是死代码 | [workorders.py](file:///C:/Users/lmxyrqlxr/Desktop/kangban_3D_Digital_Human-main/app/api/routes/workorders.py#L63)、[workorder_events.py](file:///C:/Users/lmxyrqlxr/Desktop/kangban_3D_Digital_Human-main/app/services/workorder_events.py#L71-L243) |
| 家属列表写死 `status_filter=None` | [workorders.py](file:///C:/Users/lmxyrqlxr/Desktop/kangban_3D_Digital_Human-main/app/api/routes/workorders.py#L407-L431) |
| `medication_orders`/`medication_events`/`workorder_reads`/`media_history` 均不存在 | 全仓搜索仅出现在 docs |
| `langgraph` 未安装；`apscheduler` 仅在 `optional-dependencies.infra` | `pyproject.toml` |
| 本地环境可用 | `.venv` Python 3.13.7、uv 0.12.1、PostgreSQL 5433、Redis 6379 均可连 |
| 测试基座可复用 | `tests/conftest.py` 提供 `db_session`（kangban_test，session 级 `alembic upgrade head`） |
| 工作区有未提交改动 | `docs/Day7-改造方案.md`、`docs/Day7-改造提示词.md` 已被修改（用户编辑，本轮不动） |

## 2. 段间固定约定（每段执行前重申）

1. 不重写主控 Agent、不改路由判定顺序（段 3/6 仅按文档做最小前置分支/让位）。
2. 不回改任何已执行迁移；只新增 `0008`（段 1）与 `0009`（段 6）。
3. 不新增 `nurse_elder_assignments`；护工按楼栋授权。
4. 所有 `publish_workorder_event` 必须在 `commit()` 之后；commit 前只构造轻量 DTO。
5. 外部依赖未配置/不可用时应用必须可启动，并有可见降级日志与话术。
6. 每段验收统一跑：`alembic upgrade head`（段 1/6）、该段 pytest 子集、`ruff check .`。
7. 每段结束输出：改动文件清单、关键实现决策、验收命令与真实输出、已知限制与与文档的差异。

## 3. 分段实施

### 段 1｜用药计划闭环 + 服药双表（7.2-0 后端）

- 目标：`medication_plans` 增加作者/确认/工单字段；新增 `medication_orders`、`medication_events`、`workorder_reads`；落地用药计划领域服务/API、长期工单、漏服建单函数、家属只读与服药历史。
- 新增：`alembic/versions/0008_medication_order_flow.py`、`app/models/medication_order.py`、`app/schemas/medication.py`、`app/schemas/medication_order.py`、`app/services/medication_plan.py`、`app/services/medication_order.py`、`app/api/routes/medication.py`、`tests/test_medication_plan_api.py`、`tests/test_medication_permissions.py`、`tests/test_medication_order.py`。
- 修改：`app/models/__init__.py`、`app/models/medication.py`、`app/models/workorder.py`、`app/schemas/workorder.py`、`app/services/workorder.py`、`app/api/router.py`、`tests/test_workorder_service.py`。
- 关键决策（按文档 §4.2 与提示词段 1）：仅绑定医生可写（角色 403 / 范围外 404）；`planned` 为草稿，确认后 `active` + 一张 `order_kind=standing` 工单；长期工单不随护士处理关闭，管理员在计划生效时关闭返回 409；`ensure_order` 唯一键 `(plan_id, scheduled_at)`；漏服建单唯一入口 `create_missed_medication_work_order`，`dedupe_key=missed:{order_id}`；家属接口字段白名单，永不返回 `internal_note`/`skip_reason`/`workorder_id`/`actor_account_id`/`device_id`；所有写路径 commit 后再推送。
- 验收：`alembic upgrade head` + 0008 downgrade 可回退；`pytest tests/test_medication_plan_api.py tests/test_medication_permissions.py tests/test_medication_order.py tests/test_workorder_service.py -q`；`ruff check .`；接口手工验证 7 条完成标志（见方案 §4.2）。
- 停点：基线迁移/约束/索引/API/权限/家属脱敏全部通过后暂停，等用户审查。

### 段 1.5｜工单四端实时 + 30 秒轮询 + 按账号已读

- 目标：`WS_ROLES` 四端共用；管理员/家属实时刷新；断线 30 秒轮询；已读落 `workorder_reads`；列表补 `type/title/is_read/read_at`，`status/mine` 服务端筛选。
- 修改：`app/services/workorder.py`、`app/services/workorder_events.py`、`app/services/voice.py`（仅推送时机）、`app/api/routes/workorders.py`、`app/schemas/workorder.py`、`app/static/js/workorders.js`、四端 HTML。
- 新增：`tests/test_workorder_realtime.py`、`tests/test_workorder_reads.py`。
- 关键决策：已读按账号、处理按角色，`handled` 仍是关闭门禁；admin/family 不发 `ticket.counts`、不做角标；`_resolve_recipients` 校验家属绑定 active 且未过期；WS 心跳 25s ping / 60s 超时转轮询；单账号连接上限 3；token 到期关闭 4401。
- 验收：管理员/家属新工单自动出现；两名护士独立已读；`mine` 筛选翻页正确；断 WS 后 30 秒轮询补上；`ruff`/pytest 通过。
- 停点：暂停等审查。

### 段 2｜医生端最小界面 + 家属只读/服药历史 + 工单页适配（7.2-0 UI）

- 目标：医生在现有面板完成“创建→确认页→下发”；家属只读计划 + 修改记录 + 服药历史时间线；工单页长期工单标识/本人已读/管理员不可关闭。
- 新增：`app/static/js/medication.js`。
- 修改：`app/static/doctor.html`、`app/static/family.html`、`app/static/js/workorders.js`（必要时 `api.js` 补 PUT）。
- 关键决策：确认页必须覆盖层展示全部字段并显式点击，禁止 `window.confirm` 与保存即下发；修改已生效计划展示“原值→新值”确认页；家属复用段 1.5 长者选择器（`sessionStorage: kb_wo_elder`）、不重复实现 WS/轮询/筛选；管理员/家属实时事件只 toast。
- 验收：文档段 2 的 7 条验收标准（四类角色各一条手工走查 + 控制台无未捕获异常）。
- 停点：暂停等审查。

### 段 3｜用药确认图 + 主控 Agent 工具接入（7.2-a）

- 前置：安装 `langgraph`、`langgraph-checkpoint-sqlite`、`aiosqlite`（`uv add`），先跑最小两节点图验证 API 再写业务。
- 新增：`app/graphs/__init__.py`、`app/graphs/checkpointer.py`、`app/graphs/medication.py`、`scripts/seed_medication_plan.py`、`tests/test_medication_graph.py`。
- 修改：`app/services/medication_plan.py`（今日用药查询）、`app/agent/tools.py`（`medication_query` 真实化 + `medication_confirm`）、`app/router/signals.py`、`app/router/service.py`（MED-01 前置分支）、`pyproject.toml`、`tests/test_agent_tools.py`。
- 关键决策：`thread_id=medication:{order_id}`；DB 订单为权威，每次推进先写订单/事件；重试最多 3 次；不用 `interrupt`；checkpointer 放 `logs/checkpoints/medication.sqlite3`，测试可注入临时路径；langgraph 不可用时单轮确认**仍落库**；路由前置分支必须在 `_NEGATED_MEDICATION_RE` 之后、规则层之前，且回归 `scripts/run_day6_golden.py`。
- 验收：graph/agent_tools/medication_order 测试通过；“我该吃什么药/我吃过药了/连续 3 次没吃”手工链路正确；重启可读回状态；day6 golden 通过数不低于改动前。
- 停点：暂停等审查。

### 段 4｜定时外呼 + 反向 WebRTC（7.2-b）

- 前置：安装 `apscheduler`（`infra`）；确认与 uvicorn 同 loop。
- 新增：`app/services/medication_scheduler.py`、`app/services/outbound.py`、`scripts/trigger_medication_call.py`、`tests/test_outbound_scheduler.py`、`tests/test_outbound_call.py`。
- 修改：`app/core/config.py`、`app/core/lifespan.py`、`app/realtime/session.py`、`app/api/routes/voice.py`、`app/services/voice.py`（仅按设备取会话/发事件）、`app/static/js/voice.js`、`.env.example`、`pyproject.toml`。
- 关键决策：先 `ensure_order` 并 commit 再外呼；三层幂等（DB 唯一键 → 订单终态 → 图状态）；启动补漏只回看 `medication_backfill_lookback_hours=24`；反向 WebRTC 独立 PC、先 `addTrack` 再 `createOffer`；失败降级 WS TTS 并记 `degraded=rtc_unavailable`；无设备落 `missed` + 建漏服单（`reason_code=no_device`）；单实例约束写进文档；禁止 `new_event_loop`/独立线程。
- 验收：手动触发出声；30s 超时重试、3 次建单且仅一张；WS 断开降级；重启补漏；未装 apscheduler 可启动并记 `medication_scheduler_unavailable`。
- 停点：暂停等审查。

### 段 5｜长期记忆五步（7.1）

- 新增：`app/integrations/embeddings.py`、`app/services/memory.py`、`scripts/memory_maintenance.py`、`tests/test_memory_service.py`、`tests/test_memory_isolation.py`。
- 修改：`app/core/config.py`、`app/services/voice.py`、`app/services/session_state.py`、`app/services/conversation_store.py`、`app/agent/tools.py`。
- 关键决策：Embedding 优先 OpenAI 兼容，未配置降级 `HashNgramEmbedding`（256 维，`hash-ngram-256`），日志标注实际模型；所有查询强制 `WHERE elder_id`；写入去重 >0.95、冲突 >0.9 + LLM 判定；召回 5 → 二次过滤最多 3；90 天过期抑制、归并不物理删除；双触发（WS `close()` / 新增 ≥4 条消息），后台任务绑定当前 loop；记忆正文不入日志；只在 `_prepare_state` 注入，且身份已确认才召回。
- 验收：文档段 5 的 7 条验收场景（跨会话糖尿病命中、跨长者为空、矛盾 superseded、噪声被过滤、归并幂等、日志无正文）。
- 停点：暂停等审查。

### 段 6｜娱乐点播（7.3）

- 新增：`alembic/versions/0009_media_history.py`、`app/models/media_history.py`、`app/services/media.py`、`scripts/seed_media_library.py`、`tests/test_media_service.py`。
- 修改：`app/models/__init__.py`、`app/services/session_state.py`、`app/agent/tools.py`、`app/router/rules.py`（仅 R01/R04 加 `tool`）、`app/router/executor.py`（有 tool 时让位）、`app/api/routes/voice.py`（`media_event`）、`app/static/js/voice.js`、`tests/test_router_rules.py`、`tests/test_agent_tools.py`。
- 关键决策：只用本地授权素材；无素材用 `scripts/seed_media_library.py --allow-placeholder` 生成占位音源验链路，不伪造真实戏曲；`url` 必须应用内相对路径；无可用媒体返回 `degraded=media_unavailable`；自动播放被拒时显示“点这里继续播放”；媒体文件不入 Git。
- 验收：`alembic upgrade head` + 0009 downgrade；“我想听越剧”规则直出并落 `media_history`；“换一段”零模型且旧记录补结束；“停”停止并补 `completion_rate`；全 hidden 时明确降级；路由回归。
- 停点：暂停等审查。

### 段 7｜Iter3 基线 + 失败分层归因（7.4）

- 新增：`eval/fixtures/day7_scenarios.jsonl`（恰好 20 条）、`scripts/eval_day7.py`、`scripts/eval_gate.py`、`eval/reports/v1_baseline.md`（脚本生成）、`tests/test_eval_day7.py`。
- 修改：`app/services/voice.py`、`app/core/logging.py`（trace 字段白名单），可选 `app/agent/mcp_client.py`（MCP 成功日志）。
- 关键决策：默认 offline（fake LLM/embedding/工具副作用），`--live` 需显式开启且报告标注；8 项指标必须给公式与分母；无样本写“无数据”不填 0；失败 case 归因到 `asr/laya/recall/llm/tool/orchestration`；gate 默认阈值 `task_success_rate>=0.80`、`sensitive_recall>=0.95`、`tool_accuracy>=0.80`；不打 tag，只在交付说明给出建议命令。
- 验收：`pytest tests/test_eval_day7.py -q`；offline 两次复跑一致；报告章节齐全；gate 达标 0 / 不达标 1；报告无真实长者数据。
- 停点：全部完成，汇总 8 段结果。

## 4. 假设与决策

| # | 决策 | 说明 |
| --- | --- | --- |
| A1 | 文档即规格 | 8 段提示词已足够决策完备，不再逐段额外设计；代码差异先报告再调整 |
| A2 | 逐段暂停 | 用户已确认；每段结束必须等审查，不自动进入下一段 |
| A3 | 依赖按需安装 | 段 3 装 LangGraph 三件套，段 4 装 APScheduler；均允许联网 |
| A4 | 数据库 | 开发库 `kangban_dev` 执行迁移；测试用 `kangban_test`（conftest 自动迁移） |
| A5 | 媒体素材 | 教师授权素材未到位时用占位音源验证链路；真实素材只需替换文件与种子清单 |
| A6 | Git | 全程不 commit/push/tag；用户审查后自行提交 |
| A7 | 文档未提交改动 | 现有 `docs/Day7-*` 修改属于用户，本轮不修改、不覆盖 |

## 5. 总体验证命令

```powershell
# 每段
.venv\Scripts\python.exe -m pytest <该段测试> -q
.venv\Scripts\python.exe -m ruff check .

# 段 1 / 段 6 额外
.venv\Scripts\alembic.exe upgrade head
.venv\Scripts\alembic.exe downgrade -1
.venv\Scripts\alembic.exe upgrade head

# 段 3 额外回归
.venv\Scripts\python.exe scripts/run_day6_golden.py

# 段 7
.venv\Scripts\python.exe scripts/eval_day7.py --out eval/reports/v1_baseline.md
.venv\Scripts\python.exe scripts/eval_gate.py --report eval/reports/v1_baseline.md
```

## 6. 风险与应对

| 风险 | 应对 |
| --- | --- |
| 段 1 迁移约束名/枚举与文档不符 | 执行前先读 `0007` 与 `app/models/enums.py`；只重建文档指定 CHECK，其余不动 |
| 段 1.5/2 前端改动破坏既有页面 | 只做增量扩展，保留员工处理弹窗与既有筛选入口；四类角色逐页走查 |
| 段 3 图状态与 DB 不一致 | DB 权威；每次推进先写订单/事件；测试覆盖“只改图状态”反例 |
| 段 4 反向 WebRTC 环境不可用 | 强制降级 WS TTS + `degraded=rtc_unavailable`，不让环境卡死验收 |
| 段 5 Embedding 未配置 | 哈希 n-gram 降级，全链路可跑，日志标注模型与维度 |
| 段 6 无真实素材 | 占位音源 + `media_unavailable` 降级，不播放未授权内容 |
| 段 7 指标被“美化” | 脚本产出、可复算；不做业务判定改动 |
