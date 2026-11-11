# Day7 改造方案（记忆 / 用药 / 娱乐点播 / Iter3 基线）

> 依据：`docs/康伴3D数字人_大纲.md`（工作区副本与附件 SHA256 一致：`2EC7403A70052493DD223D22CC7C69D272528B3416B31D40E4CC0B36AFE95925`），第十节 Day7 四个课时（7.1–7.4）及第六、八章的相关约束。
> 状态：**仅审阅与方案，未修改任何代码与大纲**。
> 结论先行：Day7 不是"加三个功能"，而是**把 Day6 的主控 Agent 从"会调工具"升级为"有记忆、有状态、可主动发起、可评测"**；同时按产品要求，把"用药计划"从一张表升级为**医生发起 → 医生确认 → 长期工单多方投递 → 家属只读**的完整闭环。

---

## 0. 结论速览

| 项 | 结论 |
| --- | --- |
| Day7 实际交付 | ⓪医生端用药计划闭环（绑定医生创建/修改、确认页、长期工单投递护工 + 管理员存档 + 家属只读）；①长期记忆五步链路；②用药确认 LangGraph 图 + 定时反向 WebRTC 外呼 + 漏服工单 + **服药任务单/事实事件双表 + 家属完整服药历史**；③授权媒体库 + 规则层点播 + `media_history`；④**工单四端实时（WS + 轮询）与按账号已读**；⑤20 场景 Iter3 基线 + 失败分层归因 |
| 当前完成度 | 约 **13%**：相关表已建，账号/角色/医生-长者绑定/工单三端页面已存在；但用药计划没有 API、没有作者与确认状态、没有长期工单语义，也没有服药任务单/事实事件；工单 WS 仅 doctor/nurse，已读仍是角色级；记忆、媒体、评测链路未落地 |
| 建议拆段 | **8 段**（见 §4），每段一次会话可完成并独立验收。原 5 段方案因新增"医生端用药闭环"扩为 7 段，本次再增加"段 1.5 工单实时与按账号已读" |
| 最该先做 | **段 1：用药计划域 + 权限 + 确认 + 长期工单（后端）**。它是新增产品要求的根，也是段 2/3/4 的数据来源 |
| 已拍板 | 产品 6 条 + 技术 5 条已全部落定，见 §1.4；**无待决项**。核心口径：长期工单复用 `workorders`、护工按楼栋、管理员仅全量可见、家属含历史与修改记录、修改走确认页+审计、Embedding 走 OpenAI 兼容 + 哈希降级、Checkpointer 用 SQLite、媒体先用占位音源、RTC 失败降级 WS TTS、**新增 `medication_orders` + `medication_events` 双表**、工单四端 WS + 30 秒轮询、已读按账号 |
| 前置阻塞 | `medication_plans` 缺 `created_by/confirmed_*/workorder_id`，`workorders` 缺长期语义；缺 `medication_orders`/`medication_events`/`workorder_reads` 三张表；不新增迁移 `0008` 则 7.2 的新要求无法验收。`media_history` 缺表，顺延到迁移 `0009` |

---

## 1. 审阅结论：Day7 到底要交付什么

### 1.1 四个课时的交付物与硬验收

| 课时 | 交付物 | 大纲规定硬验收 | 依赖 |
| --- | --- | --- | --- |
| 7.1 长期记忆 | 对话后萃取 3 条结构化记忆（偏好/病史/家庭）→ `memory_chunks`；召回 5 条后二次推理过滤；写入冲突检测（相似度 >0.9 且矛盾 → 旧记忆 `superseded`）；按重要性 + 90 天衰减的遗忘归并 | 新会话问"能吃粽子吗"主动提糖尿病；李爷爷查不到王奶奶记忆；矛盾记忆旧条被标记；召回噪声被过滤 | `memory_chunks`（已建）、Embedding、LLM、会话结束触发点 |
| 7.2 用药提醒 | **医生端**：仅绑定该长者的医生可创建/修改计划；创建后进入确认页，确认才下发；确认后生成**长期工单**投递护工，管理员收到存档，家属只读可见。**长者端**：计划录入；APScheduler 到点反向 WebRTC 外呼；"用药确认图"与主控 Agent 共用；重试 ≤3 次；漏服建单；checkpointer 持久化；**每个时段生成一条 `medication_orders`，每次提醒/确认/漏服追加 `medication_events`，家属可查完整服药历史** | 未绑定医生/其他角色不能创建或修改；确认后护工能在工单列表看到、管理员能看到、家属能只读看到；故意不确认 → 重试 3 次 → 漏服工单；重启后待确认状态不丢；同一计划同一时段只生成一条任务单；家属历史能看到已服/未服/跳过；`graph.get_state` 可逐档演示 | 医生-长者绑定（已有）、工单页面（已有）、LangGraph（未装）、APScheduler（`infra` 可选依赖，未接入）、反向 WebRTC（未做）、迁移 `0008` |
| 7.3 娱乐点播 | `media_library` 录入 20–30 条**教师授权**戏曲/老电影；规则层"我想听越剧"→ 记忆偏好 → 流式播放；"换一段/大声点/停"走规则层零模型；播放记录写 `media_history` | 说"我想听越剧"走规则直接播；"换一段"切下一首；`media_history` 落库 | 媒体素材（缺）、`media_history`（缺表）、记忆偏好（7.1）、前端播放（未做） |
| 7.4 Iter3 验收 | 20 个固定长者场景跑通；`eval/reports/v1_baseline.md` 含 8 项指标；失败 case 分层归因表（ASR/Laya/召回/LLM/Tool/编排）；Trace 全链路（同一 thread_id 串起规则→Laya→Tool→TTS）；`eval_gate.py` 雏形；打 tag `iter3-demo` | 基线可复算、含分层归因；本地 gate 可跑 | 7.1–7.3 全部完成；Day5 延迟打点、Day6 评测脚本可复用 |

### 1.2 大纲/产品要求与现状的口径冲突，以及本方案采用的口径

| # | 冲突点 | 出处 | 本方案口径 | 理由 |
| --- | --- | --- | --- | --- |
| A | **Checkpointer 存储**：一处写"持久化到 PostgreSQL"，一处写"SQLite 持久化用 `langgraph-checkpoint-sqlite`" | 6.2 Agent Harness 表 vs 课时 7.2"本日新增依赖" | **Day7 按 SQLite 落地**，封装为工厂（`app/graphs/checkpointer.py`）；Day8 若要求 PG 只换工厂实现 | 7.2 是唯一具体到包名的描述；项目当前无 `psycopg`；SQLite 不改库结构，符合"Day7 不扩数据模型"的克制口径 |
| B | **表数量与新增迁移**：第八章列 14 张核心表；仓库现有 24 个 ORM 模型，`media_history` 仍未建；本次产品要求又需要长期工单语义与服药事实落点 | 第八章数据模型表 vs 课时 7.3 vs 本次产品要求 | **新增两个迁移**：`0008_medication_order_flow.py`（改 `medication_plans`/`workorders`，新建 `medication_orders`、`medication_events`、`workorder_reads`，扩工单事件类型）+ `0009_media_history.py`（只建 `media_history`）。仍不建 `nurse_elder_assignments` | 长期工单复用 `workorders` 复用三端页面；但"每个时段一次服药任务"和"服药事实"无法用工单或图状态表达，必须落双表；`workorder_reads` 用于按账号已读 |
| C | **记忆萃取时机**："每段对话结束后"；但当前没有会话结束逻辑 | 课时 7.1 vs `app/services/conversation_store.py` | **双触发**：①WS 会话 `close()` 时；②同一 thread 新增消息 ≥4 条且游标未推进时。游标存 Redis 会话状态 | 不在 Day7 造"会话生命周期"新概念；双触发覆盖"长者一直不挂断" |
| D | **反向 WebRTC 与 Day3 单向收音冲突**：现只有浏览器 offer → 服务器 answer，`recvonly` | 课时 7.2 vs `app/realtime/session.py`、`app/realtime/routes.py` | 新增 WS 信令 `rtc_call_offer` / `rtc_call_answer` + aiortc 下行音轨；失败降级为现有 WS TTS，并记 `degraded=rtc_unavailable` | 不预留降级会让 7.2 被环境问题卡死，违反"可见降级"总原则 |
| E | **7.4 的"20 个场景"与 Day6 样例集关系不清** | 课时 6.4 vs 7.4 | 两套资产并存：`day6_*.jsonl` 做路由回归；新增 `day7_scenarios.jsonl` 做任务级验收 | 一个测"判定对不对"，一个测"事情办成没有" |
| F | **Embedding 未指定模型/维度** | 课时 7.1 vs `app/models/memory.py`、`app/core/config.py` | Provider 协议 + OpenAI 兼容实现；未配置时降级为本地哈希 n-gram（仍强制 `WHERE elder_id`） | 未配置可启动、降级可见；不下载权重 |
| G | **7.2 验收说"工单进护工看板"，原方案把看板划给 8.2** | 课时 7.2 vs 8.2 vs 仓库现状 | **口径修正**：仓库已存在 `doctor/nurse/admin/family` 四端页面与 `app/static/js/workorders.js`（代码标注 Day8 Task4/5）。Day7 **复用这些页面**，只增加用药模块所需的最小展示；不重做看板、不做趋势图 | 事实是页面已存在；再"留到 Day8"会让本次产品要求无法演示。复用不等于实现 8.2 的完整看板 |
| H | **"注册为主控 Agent 子图"与当前 Agent 形态不符** | 课时 7.2 vs `app/agent/companion.py` | 用药图封装为 `medication_query` / `medication_confirm` 工具；不重写 Agent | 重写 Agent 跨课时且会伤 Day6 已验收边界 |
| I | **医生权限与现有健康档案权限混用风险**：`health_records(category='medication')` 允许有 `can_edit_basic` 的护工写入 | 本次产品要求 vs `docs/权限矩阵设计.md` §4 | **用药计划与健康档案分离**：`medication_plans` 只允许绑定医生写，护工的 `can_edit_basic` 不能写计划；`health_records` 仍是自由文本档案，**不是提醒真源** | 否则护工可改计划，直接违反"只有绑定医生可以发起和修改" |
| J | **"长期工单"与现有一次性工单模型冲突**：workorders 状态机按"处理→关闭"设计，无周期/有效期语义 | 本次产品要求 vs `app/models/workorder.py`、`app/services/workorder.py` | 给 `workorders` 增加 `order_kind`（`one_off`/`standing`）、`medication_plan_id`、`effective_from/until`；长期工单**不随护士提交处理而关闭**，随计划停用/取消由系统关闭 | 复用页面与状态机，但用 `order_kind` 隔离关闭语义，避免长期工单被误关 |
| K | **"绑定该长者的护工"与现有绑定粒度不一致**：护工绑定的是**楼栋**（`nurse_building_assignments`），不是单个长者 | 本次产品要求 vs `app/models/assignment.py`、`app/services/authorization.py` | **已拍板**：按"该长者所在楼栋的护工"投递与可见，不新增人到人绑定表（见 §1.4） | 复用现有权限体系，避免影响既有排班与权限 |
| L | **"管理员收到作为存档"与现有处理项角色冲突**：`workorder_dispositions` 只允许 `doctor`/`nurse` | 本次产品要求 vs `app/models/workorder.py` | **已拍板**：不新增 `admin` 处理项；管理员通过全量工单列表看到并存档，`created` 事件写 `archive_roles=["admin"]`；管理员接入工单 WS 接收刷新信号，但不发 `ticket.counts`、不做角标、不做站外通知（见 §1.4） | 避免改处理项 CHECK、避免造没有处理接口的"管理员待办"悬挂状态 |
| M | **确认页与"草稿/生效"状态**：现有 `MedicationPlanStatus` 有 `planned`，但没有任何确认流程 | 本次产品要求 vs `app/models/medication.py` | `planned` 作为**草稿**；确认后置 `active` 并生成长期工单；已生效计划的修改由医生在确认页二次确认后提交，后端写审计事件 | 复用现有枚举，不新增状态机；确认页是产品硬要求，不能省 |
| N | **修改历史可还原性**：`workorders` 是**原地覆盖的当前快照**（`mark_read`/`submit_disposition`/`close_work_order` 均 UPDATE 同行），`workorder_events` 现只记动作（`created/read/disposition/closed/occurrence`）且 `disposition` 只存 `plan_length`，**不存字段旧值/新值** | §4.2 家属修改记录 vs `app/services/workorder.py`、`app/models/workorder.py` | 字段级历史**依赖 `0008` 新增的 `revised`/`confirmed` 事件**：`payload.changes=[{field,before,after}]` + `plan_version`；`internal_note` 只记 `changed`；不新增版本表 | 现有事件表不能还原"谁把剂量从 X 改成 Y"；复用既有 JSONB `payload` 是唯一"只增不改"的落点，新建版本表对 Day7 过重 |
| O | **已读粒度**：`workorder_dispositions` 唯一键是 `(workorder_id, role)`，`mark_read` 覆盖 `account_id` | 本次产品要求 vs `app/models/workorder.py` | 新增 `workorder_reads(workorder_id, account_id, read_at)`；已读按账号，`handled` 仍按角色共享 | 角色级唯一键无法表达"某护士已读"；新增回执表不改处理项状态机与关闭门禁 |
| P | **管理员实时是死代码**：`_resolve_recipients` 算上管理员，WS 鉴权拒绝 admin，`admin.html` 也不建连接 | 本次产品要求 vs `app/services/workorder_events.py`、`app/api/routes/workorders.py` | 新增唯一常量 `WS_ROLES`，四端共用；管理员接入 WS，仅做列表刷新信号，不发 counts | 广播与接入必须同源，否则"全量可见"只能手动刷新 |
| Q | **家属无实时、无轮询，且必须先选长者** | 本次产品要求 vs `app/static/js/workorders.js` | 四端 WS + 断线 30 秒轮询；家属用长者选择器（`sessionStorage` 持久化），切换立即刷新，无长者显示空态 | 家属接口按 `elder_id` 建模；不引入聚合看板 |
| R | **漏服建单函数不存在**：`type="missed_medication"` 只在文档里 | 本次产品要求 vs `app/services/workorder.py` | 段 1 落地 `create_missed_medication_work_order`，段 3/4 只调用 | 函数、字段、去重、调用点必须一次写清，避免段 3/4 各写一份 |
| S | **推送早于事务提交**：服务层 `flush()` 后推送，调用方之后才 `commit()` | 本次产品要求 vs `app/services/workorder.py`、`app/services/voice.py` | 所有写路径改为 commit 后推送；commit 前构造轻量 DTO | 否则客户端收到事件后刷新看不到数据，提交失败会出现幽灵事件 |
| T | **列表缺 `type/title`，筛选是当前页客户端过滤** | 本次产品要求 vs `app/schemas/workorder.py`、`workorders.js` | 列表项补 `type/title`；`status` 服务端筛选；员工新增 `mine=unread/read/handled`；家属接口补 `status` | 用药/漏服工单 `sensitive_category` 为 NULL，不补字段会显示"未分类"；页内筛选翻页即失效 |
| U | **约束硬边界**：`ck_workorder_events_type` 仅 5 种，`ck_workorders_source` 仅 `sensitive/manual`，`ck_workorder_events_actor_role` 无 `system` | 本次产品要求 vs `alembic/versions/0007_workorder_flow.py` | `0008` 重建事件类型为 7 种；系统建单 `actor_role=None`；不改 `source` 与处理项角色 CHECK | 照自然语言写 `system` 或新 source 会直接违反约束 |

### 1.3 与相邻课时的边界（必须写死）

**已经具备（Day7 直接复用，不重写）**

- 三级路由：安全一票否决 → 10 条规则 → Laya 分类器 → 主控 Agent（`app/router/service.py`）。
- 主控 Agent ReAct + 工具注册表（`app/agent/companion.py`、`app/agent/tools.py`），`elder_id` 由 `ToolContext` 强制注入。
- 账号与角色鉴权：`app/models/account.py`、`app/auth/dependencies.py`；`doctor_elder_assignments`、`nurse_building_assignments`、`family_elder_links`；`app/services/authorization.py#can_access_elder`。
- 健康档案 ACL：医生/护工/家属按关联与查看位读写；**该 ACL 不用于用药计划**。
- 工单：`app/services/workorder.py`（建单/处理项/事件/关闭）、`app/api/routes/workorders.py`（员工/管理员/家属三套路由）、`app/static/js/workorders.js` 与四端页面。
- 评测框架 `scripts/eval_day6.py`、延迟打点 `app/services/latency.py`。

**Day7 允许做**

- 新增迁移 `0008`（用药生命周期 + 服药任务单/事实事件 + 按账号已读）与 `0009`（`media_history`）。
- 新增 `app/services/medication_plan.py`、`app/schemas/medication.py`、`app/api/routes/medication.py`，并在 `app/api/router.py` 注册。
- **新增三张表**：`medication_orders`、`medication_events`、`workorder_reads`；新增 `app/models/medication_order.py`、`app/services/medication_order.py`、`app/schemas/medication_order.py`。
- 修改 `app/models/medication.py`、`app/models/workorder.py`（只加字段/扩事件类型，不改旧迁移）。
- 修改 `app/services/workorder.py`：新增漏服建单函数、长期用药工单建单函数、长期工单系统关闭函数、按账号已读与计数。
- 修改 `app/services/workorder_events.py`：`WS_ROLES` 四端共用、家属收件人解析、`_payload` 补 `type`、仅 staff 发 counts。
- 修改 `app/api/routes/workorders.py`：WS 四端鉴权、家属列表 `status` 参数、员工 `mine` 参数、commit 后推送。
- 修改 `app/static/doctor.html`、`family.html`、`nurse.html`、`admin.html`、`app/static/js/workorders.js`：用药模块、长期工单标识、四端实时/轮询、长者选择器、服药历史。
- 新增 `app/graphs/**`、`app/services/outbound.py`、`app/services/memory.py`、`app/services/media.py`、`app/integrations/embeddings.py`。
- 修改 `app/agent/tools.py`（真实化 `medication_query`，新增 `medication_confirm`、`memory_recall`）。
- 修改 `app/router/rules.py` 只给 R01/R04 补 `tool`，`app/router/executor.py` 对带 `tool` 的动作让位。
- 反向 WS 信令与 aiortc 下行音轨；`voice.js` 接媒体播放。
- `workorders` 新增 `type="missed_medication"`（一次性）与 `type="medication_plan"`（长期）。
- WS 路径沿用 `/api/v1/staff/ws`，仅命名历史遗留，实际为工单事件通道。

**Day7 明确不做（留给 Day8）**

- 健康事件决策图、夜间 5 问健康采集、分级阈值、医院 HIS 对接（8.1）。
- LangGraph `interrupt` + `Command(resume=...)`（8.1；7.2 的确认按轮推进）。
- 完整护工看板/医生趋势图/管理员运营看板/家属 H5 日报（8.2/8.3）；Day7 只复用已有页面做用药模块与服药历史，不做图表、不做统计大盘。
- 站外通知（短信/微信/推送）、运营角标、审批中心（Day8+）；**允许**工单 WS、30 秒轮询、按账号回执与服药历史。
- 声纹阈值调优、三档身份级联验证（8.4）。
- 不重构 Day6 路由层、不改 `app/auth/**`、不回改任何已执行迁移、不执行 `git push`。

---

### 1.4 已拍板口径（产品 6 条 + 技术 5 条 + 本次新增 8 条）

> 全部按"只增不改"落地：**加字段不改表、加迁移不改旧迁移、加可选模块不替换主链路、外部依赖缺失可运行**。任何推荐都不要求重写主控 Agent、路由层或现有四端页面。

| # | 决策 | 结论 | 理由 | 新增依赖/外部资源 | 未配置/失败时的降级 | 后续切换成本 |
| --- | --- | --- | --- | --- | --- | --- |
| P1 | 长期工单 | **复用 `workorders`**，加 `order_kind=standing`、`medication_plan_id`、`effective_from/until` | 直接复用护工/管理员/家属三端页面与处理项、事件、关闭流程；新增独立表会迫使三端页面重写 | 无 | 无（纯扩展字段） | 若改独立表：需新表 + 三端查询改造 + 数据迁移，成本高 |
| P2 | 护工绑定量级 | **按楼栋**：该长者所在楼栋的护工均可收到 | 复用 `nurse_building_assignments` 与 `can_access_elder`，不动排班与权限 | 无 | 无 | 若改人到人：新增绑定表 + 管理接口 + 权限判定，成本中 |
| P3 | 管理员"存档" | **全量列表可见 + 工单 WS 刷新信号**；不发 `ticket.counts`、不做未读角标、不做站外通知 | 不改 `workorder_dispositions` 的 doctor/nurse CHECK；管理员不需要处理项，但需要实时看到新工单 | 无 | 无 | 若加 counts/角标：需定义管理员口径 + 前端角标，成本中 |
| P4 | 家属可见范围 | **含 `cancelled/completed` 历史与修改记录、完整服药历史**；不含 `planned` 草稿与 `internal_note` | 满足知情权，同时不暴露未确认草稿与医生内部备注；服药历史是"已记录"的可信落点 | 无新增外部依赖；**依赖 `0008` 的 `revised`/`confirmed` 事件与 `medication_orders`/`medication_events`** | 无记录时只显示计划本体与空历史 | 若加独立修订表：新增表 + 回填，成本中 |
| P5 | 已生效计划修改 | **确认页显式确认 + 后端审计**；不做 `pending_changes` 暂存与二次确认接口 | 满足"医生确认"要求，避免引入双份计划状态与提醒读错版本 | 无 | 审计事件写失败时拒绝本次修改（不静默） | 若加强约束：加暂存字段/接口，成本中 |
| P6 | "其他医生不能" | **未绑定该长者的医生不能**；绑定该长者的任意医生可改（含发起人） | 与现有 `doctor_elder_assignments` 语义一致，避免再造"作者专属"规则 | 无 | 无 | 若改发起人专属：服务层加 `created_by` 判定，成本低 |
| P7 | 工单已读粒度 | **按账号回执**：新增 `workorder_reads(workorder_id, account_id, read_at)`；处理项 `handled` 仍按角色共享 | 角色级唯一键无法表达"某护士已读"；回执表不改状态机与关闭门禁 | 无 | 回执表缺失时退回角色级已读并记 `workorder_read_receipt_unavailable` | 若改处理项按账号：重建唯一键与关闭门禁，成本高 |
| P8 | 工单实时 | **四端 WS + 断线 30 秒轮询**；`WS_ROLES={doctor,nurse,admin,family}` 唯一常量；仅 staff 收 `ticket.counts` | 管理员广播已是死代码、家属无入口；四端共用一套通道与收件人解析 | 无 | WS 不可用/断线时纯轮询；单进程部署，多副本需 Redis | 若换 Redis 广播：替换 hub 实现，前端协议不变 |
| P9 | 漏服建单 | **唯一函数 `create_missed_medication_work_order`**；`dedupe_key=missed:{order_id}` + 部分唯一索引 | 调度重试/并发会重复建单；函数只保留一个入口，段 3/4 只调用 | 无 | 建单失败不静默：写 `medication_events` 失败原因并告警日志 | 若改批量建单：仅换调用方，函数签名不变 |
| P10 | 事件发布时机 | **一律 commit 成功后发布**；commit 前只构造轻量 DTO | 现有服务层先推送后提交，客户端刷新会看不到数据或收到幽灵事件 | 无 | 推送失败只记日志，不影响业务事务 | 无 |
| P11 | 列表与筛选契约 | 列表补 `type/title`；`status` 服务端筛选；员工 `mine=unread/read/handled`；家属接口补 `status` | 用药/漏服工单 `sensitive_category` 为空会显示"未分类"；页内筛选翻页失效 | 无 | 旧客户端不传新参数时行为不变 | 无 |
| P12 | 任务单生成 | **按需 `ensure_order` + 启动补漏**；唯一键 `(plan_id, scheduled_at)`；补漏窗口默认 24 小时 | 预生成会因计划修改产生大量无效任务；纯图状态重启后无法对账 | 无 | 唯一键冲突时回读既有订单，不新建 | 若改每日预生成：新增日任务，唯一键不变 |
| P13 | 服药事实写入 | **`medication_events` 只追加**；不存对话原文/音频；家属接口只返回字段白名单 | 事实可追溯且不扩大隐私面；家属历史不暴露 `internal_note`/`workorder_id`/账号 UUID | 无 | 事件写入失败则回滚订单状态变更（同事务） | 无 |
| P14 | 确认权限 | **只有长者本人语音/文字可 `confirmed`**；员工/家属只能 `skipped` | 防止家属或护工代替长者确认服药；`confirmed_source` 只允许 `voice/text` | 无 | 无法确认时保留 `calling/pending`，由重试或补漏转 `missed` | 若允许员工代确认：需新增来源与权限审计，成本中 |
| T1 | Embedding | **Provider 协议 + OpenAI 兼容 `/v1/embeddings` 实现**；默认模型 `text-embedding-3-small`、维度 **1536**；配置项 `EMBEDDING_*` + `embedding_dimension` | 项目已有 `httpx`，不新增依赖；不下载本地权重；配置即可切换 | 外部 embedding API（可选） | **`HashNgramEmbedding`（256 维，`model="hash-ngram-256"`）**，仍强制 `WHERE elder_id`；日志/报告标注实际模型 | 换模型/维度只改环境变量；`vector` 无维度约束，无需迁移；旧行按 `embedding_model` 过滤，必要时重嵌入，成本低 |
| T2 | Checkpointer | **SQLite**：`langgraph-checkpoint-sqlite` + `aiosqlite`，文件放 `logs/checkpoints/medication.sqlite3`，由 `app/graphs/checkpointer.py` 工厂创建 | 大纲 7.2 明确点名；项目无 `psycopg`；不改库结构 | `langgraph`、`langgraph-checkpoint-sqlite`、`aiosqlite` | 图/依赖不可用时：用药确认退化为**单轮确认**（仍写 `medication_orders` + `medication_events`），提醒退化为**单次 TTS**，不重试不升级，记 `degraded=medication_graph_unavailable`；应用照常启动 | 换 PG：只改工厂 + 加 `psycopg`，业务代码不动；配置化 URL，成本低 |
| T3 | 媒体素材 | **由课程/教师提供 20–30 条授权素材**，放 `app/static/media/library/`；在此之前用**本地生成占位音源**验链路 | 不爬取、不下载版权内容；占位音源不引入依赖（stdlib `wave`） | 授权音频文件（人工提供） | 无文件时 `degraded=media_unavailable` + 明确话术；不播放未授权内容 | 换真实素材只换文件与种子清单，代码不动，成本低 |
| T4 | 反向 WebRTC 失败 | **允许降级到现有 WS TTS 通道**播提醒，并记 `degraded=rtc_unavailable` | 复用已验收的 WS TTS/ASR 主链路；不让环境问题卡死 7.2 | 无（aiortc 已在项目内） | 已定义：WS 未连/8 秒未建连/aiortc 异常 → WS TTS + 文字确认；仍走确认图 | 若禁用降级：只改 `outbound.py` 一个分支，成本低 |
| T5 | 服药事实 | **新增 `medication_orders` + `medication_events` 双表**：订单存"某计划某时段"的当前态，事件存 `created/reminded/confirmed/denied/timeout/missed/skipped/cancelled` 追加事实 | "已记录"必须是可查询业务事实；图状态是运行时产物、日志不可查询，无法支撑家属完整历史与重启对账 | 无外部依赖；纯数据库表 | 未迁移时应用照常启动，但用药提醒功能不可用并记 `medication_schema_unavailable` | 若合并为单表：需重建状态机与历史查询，成本高 |

**"只增不改"的落地检查**

- 数据：只新增迁移 `0008`（加列/扩 CHECK/新建 `medication_orders`、`medication_events`、`workorder_reads`）与 `0009`（`media_history`）；旧迁移一行不改。
- 代码：新增 `medication_plan` / `medication_order` / `outbound` / `memory` / `media` 模块；主链路只加"可选分支 + import 失败降级"，不替换 Agent、路由、实时主流程。
- 前端：只在 `doctor.html` / `family.html` / `nurse.html` / `admin.html` / `workorders.js` 上扩展，不删改现有页面结构，不引入框架。
- 依赖：只新增 LangGraph 三件套 + `infra` 可选的 APScheduler；Embedding、媒体、RTC 均有"未配置也能跑"的降级路径。

---

## 2. 现状差距分析（落到文件）

| # | 能力 | 关键文件 | 现状 | 判定 | Day7 要补 |
| --- | --- | --- | --- | --- | --- |
| 1 | 三级路由 / Agent / 工具注册 | `app/router/service.py`、`app/agent/companion.py`、`app/agent/tools.py` | 可用；已注册 7 个工具 | ✅ 已完成 | 只增量加工具 |
| 2 | 用药查询工具 | `app/agent/tools.py`（`medication_query`） | 固定返回"用药计划还没接入" | 🟡 半成品 | 读真实计划；无计划不编造 |
| 3 | 用药计划数据/服务 | `app/models/medication.py`、迁移 `0002` | 只有表；无 service/API/种子；无作者、无确认字段、无工单关联 | 🟡 半成品 | 迁移 `0008` + 领域服务 |
| 4 | LangGraph / Checkpointer | `pyproject.toml` | **未安装** | ❌ 未开始 | `uv add langgraph langgraph-checkpoint-sqlite aiosqlite` |
| 5 | 定时调度 | `app/core/lifespan.py`、`pyproject.toml` | `apscheduler` 位于 `optional-dependencies.infra`，未安装、未接入 | ❌ 未开始 | 安装 `infra` 可选依赖 + lifespan 启停 + 启动补漏 |
| 6 | 反向 WebRTC 外呼 | `app/realtime/session.py`、`app/realtime/routes.py`、`voice.js` | 仅 `recvonly`，无服务端 offer/下行轨 | ❌ 未开始 | 信令 + 下行音轨 + 降级 |
| 7 | 漏服工单 | `app/services/workorder.py` | 只有敏感事件建单；`missed_medication` 无函数、无调用点 | ❌ 未开始 | `create_missed_medication_work_order` + `dedupe_key` |
| 8 | 记忆表 | `app/models/memory.py` | 表已建 | 🟡 结构就绪 | 无迁移 |
| 9 | 记忆服务 / Embedding | `app/integrations/` | 完全未开始 | ❌ 未开始 | Provider + 降级 + 五步链路 |
| 10 | 记忆注入对话 | `app/services/voice.py`、`context_summary.py` | 只有摘要 + 槽位 | ❌ 未开始 | 记忆上下文拼装 |
| 11 | 媒体库数据 | `app/models/media.py` | 表在，无数据/服务 | 🟡 半成品 | 种子 + 选片服务 |
| 12 | `media_history` | — | 表/模型都不存在 | ❌ 未开始 | 迁移 `0009` + ORM |
| 13 | 媒体播放前端 | `voice.js#handleControl` | 只打日志 | 🟡 占位 | `<audio>` 播放/换段/停止 |
| 14 | 媒体偏好 | — | 无 | ❌ 未开始 | 从记忆取 genre |
| 15 | 评测 v1 | `scripts/eval_day6.py` 等 | 只有路由层指标 | 🟡 可复用 | 任务级场景 + 8 指标 + 归因 |
| 16 | MCP 成功日志 | `app/agent/mcp_client.py` | 只有失败日志 | 🟡 遗留 | 段 7 顺手补 |
| 17 | 规则执行器短路媒体动作 | `app/router/executor.py#execute_rule` | 命中 `media_play` 后不调工具 | 🟡 半成品 | 段 6 让位 |
| 18 | 账号与角色鉴权 | `app/models/account.py`、`app/auth/dependencies.py` | `admin/nurse/doctor/family` 四角色 + JWT | ✅ 已完成 | 直接复用 |
| 19 | 医生-长者绑定 | `app/models/assignment.py`、`app/api/routes/admin.py` | `doctor_elder_assignments`（有效期/状态）+ 管理接口 | ✅ 已完成 | 用药写权限的判定依据 |
| 20 | 护工-楼栋绑定 | `app/models/assignment.py`、`authorization.py` | `nurse_building_assignments` | ✅ 已完成 | 工单可见范围；**不是人到人** |
| 21 | 家属-长者绑定 | `app/models/assignment.py`、`authorization.py` | `family_elder_links` | ✅ 已完成 | 家属只读可见范围 |
| 22 | 工单三端页面与接口 | `workorders.py`、`workorders.js`、四端 html | 三端 HTTP 可用；WS 仅 doctor/nurse；管理员/家属无实时；已读是角色级 | 🟡 半成品 | 段 1.5：四端 WS + 轮询 + 按账号回执 |
| 23 | 用药计划 API | — | 无路由、无 schema、无服务 | ❌ 未开始 | 段 1 全量新增 |
| 24 | 长期工单语义 | `app/models/workorder.py` | 无 `order_kind`、无计划关联、无有效期；关闭要求处理项完成 | ❌ 未开始 | 迁移 `0008` + 关闭保护 + 系统关闭函数 |
| 25 | 确认页/确认状态 | `app/models/medication.py` | 只有默认 `planned`，无确认动作/页面 | ❌ 未开始 | 段 1 API + 段 2 页面 |
| 26 | 家属用药只读 | — | 无 | ❌ 未开始 | 段 1 只读接口 + 段 2 展示 + 服药历史 |
| 27 | 医生用药界面 | `app/static/doctor.html` | 用药模块是"后续版本"占位 | 🟡 占位 | 段 2 最小可用界面 |
| 28 | 工单返回字段 | `app/schemas/workorder.py` | 列表/详情未暴露 `order_kind`/`type`/`title`/`is_read` | ❌ 未开始 | 段 1 契约 + 段 2 展示 |
| 29 | 长期工单关闭保护 | `app/services/workorder.py#close_work_order` | 管理员可在计划仍生效时关闭工单；系统关闭会被 handled 门禁阻塞 | ❌ 未开始 | 段 1 加保护 + `close_standing_work_order_for_plan` |
| 30 | 服药任务单 | — | 表/模型/服务都不存在；图状态不是业务任务 | ❌ 未开始 | 段 1 建 `medication_orders` + `ensure_order` |
| 31 | 服药事实事件 | — | 表/模型/服务都不存在；旧方案只说"记到当日记录"，但全仓没有该落点 | ❌ 未开始 | 段 1 建 `medication_events` + 只追加写入 |
| 32 | 家属完整服药历史 | — | 无接口、无页面 | ❌ 未开始 | 段 1 接口 + 段 2 时间线 |
| 33 | 按账号已读 | `app/models/workorder.py`、`workorder.py` | 唯一键 `(workorder_id, role)`，`mark_read` 覆盖 `account_id` | ❌ 未开始 | 段 1 建 `workorder_reads` 表，段 1.5 写回执与 `is_read` |
| 34 | 管理员/家属实时 | `workorder_events.py`、`workorders.js` | 管理员广播是死代码；家属无 WS/轮询 | ❌ 未开始 | 段 1.5 四端 WS + 轮询 |
| 35 | 事件发布时机 | `workorder.py`、`voice.py` | 服务层 `flush` 后推送，调用方之后才 commit | ❌ 未开始 | 段 1.5 改为 commit 后发布 |

### 完成度估算

| 课时 | 估算完成度 | 说明 |
| --- | --- | --- |
| 7.1 记忆 | ~5% | 只有表 |
| 7.2 用药 | ~10% | 表 + stub 工具 + 已有绑定/工单页面；医生端闭环、确认、长期工单、服药任务单/事实事件、家属服药历史均未做 |
| 7.3 娱乐 | ~15% | 表 + 规则动作 + 前端占位 |
| 7.4 评测 | ~30% | Day6 框架可复用 |
| 加权整体 | **~13%** | — |

---

## 3. 前置阻塞与外部依赖

### 3.1 必须先解决（否则对应验收做不了）

| 阻塞 | 影响 | 处理 |
| --- | --- | --- |
| **`medication_plans` 缺少作者/确认/工单字段** | 无法证明"谁发起、是否确认、对应哪张长期工单"；产品要求无法验收 | 段 1 新增迁移 `0008`（加列 + 建表），不改旧迁移 |
| **`workorders` 缺少长期语义** | 长期工单会被护士一次处理就当成"已完成"，或被管理员误关闭 | 段 1 加 `order_kind`/`medication_plan_id`/`medication_order_id`/`effective_from/until` + 关闭保护 |
| **服药任务与事实无落点** | "已服药"无法查询、重启无法对账、家属历史无法实现 | 段 1 新建 `medication_orders` + `medication_events`，唯一键 `(plan_id, scheduled_at)` |
| **工单已读是角色级** | 一名护士已读，所有护士都显示已处理 | 段 1 建 `workorder_reads` 表，段 1.5 按 `(workorder_id, account_id)` 写回执 |
| **工单 WS 只允许员工** | 管理员/家属只能手动刷新；管理员广播分支是死代码 | 段 1.5 `WS_ROLES` 四端共用 + 30 秒轮询 |
| **`media_history` 表缺失** | 7.3 硬验收无法通过 | 段 6 新增迁移 `0009`（不阻塞段 1–5） |
| **无授权媒体素材** | 7.3"录入 20–30 条授权素材"无法完成 | **已拍板**：课程/教师提供真实素材；先用本地生成占位音源验链路，不爬取、不下载版权内容 |
| **Embedding 未配置** | 7.1 语义召回只能走本地哈希降级 | **已拍板**：OpenAI 兼容实现 + 哈希 n-gram 降级；未配置也能启动，日志标注实际模型 |

### 3.2 外部依赖

- 本地 PostgreSQL（5433）+ Redis（6379）；LLM 走 `.env` 配置，不入库。
- 安装 `langgraph` / `langgraph-checkpoint-sqlite` 需要网络；不下载模型权重。
- `apscheduler` 在 `optional-dependencies.infra`：不安装时应用照常启动，调度器不启动并记 `medication_scheduler_unavailable`；安装后才启用定时提醒。
- 启动顺序：`laya-serve`（8001）→ 主后端（8000），**单实例**；多实例会重复外呼，唯一键只能防重复订单。
- 平板浏览器自动播放限制：媒体播放与主动外呼都需要一次用户手势解锁。

---

## 4. 分段方案

### 4.1 总览（8 段）

| 段 | 对应课时 | 目标（一句话） | 规模 | 前置 | 完成标志 | 风险 |
| --- | --- | --- | --- | --- | --- | --- |
| **段 1** | 7.2-0 | 用药计划域：迁移 `0008` + 权限 + 草稿/确认 + 长期工单 + 服药任务单/事实事件 + 多方投递 + 家属只读/服药历史 API | L（新增约 9，改约 7） | 无 | 未绑定医生/其他角色被拒；确认后生成 `order_kind=standing` 工单；护士/管理员/家属可见；计划停用自动关单；同一计划同一时段只一条订单 | 中高 |
| **段 1.5** | 7.2-0 实时 | 工单四端 WS + 30 秒轮询 + `workorder_reads` 按账号已读 + 列表字段/筛选契约 | M（新增约 2，改约 6） | 段 1 | 管理员/家属自动刷新；两名护士独立已读；员工可按 `mine` 服务端筛选；断线 30 秒内轮询 | 中 |
| **段 2** | 7.2-0 UI | 医生端最小界面（创建/修改/确认页）+ 家属只读视图与**完整服药历史** + 工单页长期标识/选择器 | M（改约 5） | 段 1、段 1.5 | 医生可完成"创建→确认→下发"；家属看到只读计划与服药历史；管理员/护工在工单页看到长期用药工单 | 中 |
| **段 3** | 7.2-a | 用药确认 LangGraph 图 + checkpointer + 工具接入 + 订单/事件写入 + 漏服一次性建单（不含外呼） | M（新增约 6，改约 6） | 段 1 | 3 次未确认 → `missed_medication`；重启恢复；`medication_query` 返回真实计划；每次确认都落订单与事件 | 中 |
| **段 4** | 7.2-b | APScheduler 到点 → `ensure_order` → 设备解析 → 反向 WebRTC 外呼 → 超时重试 → 工单 + 启动补漏 | L（新增约 3，改约 7） | 段 3 | 手动触发平板出声；3 次失败出工单；离线可见降级；重启不重复生成订单；不新建事件循环 | **最高** |
| **段 5** | 7.1 | 记忆五步 + 注入对话 | L（新增约 4，改约 5） | 建议段 4 后 | 4 条验收场景 + 伪造 `elder_id` 返回空 + 冲突置 `superseded` | 中高 |
| **段 6** | 7.3 | `media_history`（`0009`）+ 媒体服务 + 工具 + 前端真实播放 | M（新增约 5，改约 7） | 段 5（偏好可降级） | "我想听越剧"直接播；"换一段"；`media_history` 落库 | 中 |
| **段 7** | 7.4 | 20 场景任务级基线 + 8 项指标 + 失败归因 + trace + gate | M（新增约 4，改约 2） | 段 1、1.5、2–6 | 报告可复算、含归因；gate 退出码可用 | 低中 |

> 若你想减少一段，可把段 2 前端并入段 1；不推荐——确认页与权限验收会让单段过长。段 1.5 不建议合并：实时与已读会横跨工单模型、WS 鉴权、四端前端三处。

### 4.2 段 1｜用药计划域 + 权限 + 确认 + 长期工单 + 服药双表（7.2-0 后端）

- **目标**：把 `medication_plans` 从"一张表"变成有作者、有草稿/确认、有长期工单、有多方投递的领域闭环；同时落地"每个时段一条任务单 + 追加事实事件"，供段 3/4 与家属历史复用；不做前端。
- **权限口径（写死）**：
  - 创建/修改/确认/暂停/恢复/取消：**仅 `doctor` 角色且 `can_access_elder == ALLOWED`**（绑定有效、未过期）。未绑定医生与其他角色一律拒绝；为避免泄露长者存在性，未绑定医生返回 404，角色不符返回 403。
  - 读取：`admin` 全量；`doctor` 仅绑定；`nurse` 仅楼栋范围且**只返回已确认计划（`active/paused/completed`），不返回 `planned` 草稿**；`family` 仅关联长者且只读。
  - `internal_note` 仅 `doctor/nurse/admin` 可见，家属接口永不返回。
  - 健康档案 ACL（`can_edit_basic` 等）**不参与**用药计划授权。
- **涉及文件**：
  - 新增 `alembic/versions/0008_medication_order_flow.py`
  - 修改 `app/models/medication.py`、`app/models/workorder.py`（含 `WorkorderEvent` 事件类型扩展）
  - 新增 `app/models/medication_order.py`、`app/schemas/medication_order.py`、`app/services/medication_order.py`
  - 新增 `app/schemas/medication.py`、`app/services/medication_plan.py`、`app/api/routes/medication.py`
  - 修改 `app/api/router.py`、`app/schemas/workorder.py`、`app/services/workorder.py`
  - 新增 `tests/test_medication_plan_api.py`、`tests/test_medication_permissions.py`、`tests/test_medication_order.py`，补充 `tests/test_workorder_service.py`
- **迁移 `0008` 内容（只加不改）**：
  - `medication_plans` 增加：`created_by_account_id`（FK accounts，SET NULL）、`updated_by_account_id`、`confirmed_at`、`confirmed_by_account_id`、`workorder_id`（FK workorders，SET NULL）、`internal_note`（TEXT，可空）。
  - 新建 `medication_orders`：
    - `order_id UUID PK`、`plan_id`（FK medication_plans，CASCADE）、`elder_id`（FK elders，CASCADE）、`scheduled_at TIMESTAMPTZ`、`status VARCHAR(16)`、`attempt_count INTEGER DEFAULT 0`、`last_attempt_at`、`confirmed_at`、`confirmed_source VARCHAR(16)`、`skip_reason TEXT`、`created_at/updated_at`；
    - CHECK `ck_medication_orders_status IN ('pending','calling','confirmed','missed','skipped','cancelled')`；CHECK `confirmed_source IN ('voice','text')`；
    - 唯一键 `uq_medication_orders_plan_scheduled (plan_id, scheduled_at)`；索引 `ix_medication_orders_elder_scheduled (elder_id, scheduled_at DESC)`、`ix_medication_orders_status_scheduled (status, scheduled_at)`。
  - 新建 `medication_events`：
    - `event_id UUID PK`、`order_id`（FK medication_orders，CASCADE）、`plan_id`（FK medication_plans，CASCADE）、`elder_id`（FK elders，CASCADE）、`event_type VARCHAR(24)`、`source VARCHAR(16)`、`actor_account_id`（FK accounts，SET NULL）、`actor_role VARCHAR(16)`、`payload JSONB`、`dedupe_key VARCHAR(160)`、`created_at`；
    - CHECK `event_type IN ('created','reminded','confirmed','denied','timeout','missed','skipped','cancelled')`；CHECK `source IN ('scheduler','voice','text','staff','family','system')`；CHECK `actor_role IS NULL OR actor_role IN ('doctor','nurse','admin','family')`；
    - 部分唯一索引 `uq_medication_events_dedupe_key (dedupe_key) WHERE dedupe_key IS NOT NULL`；索引 `ix_medication_events_order_created`、`ix_medication_events_plan_created`。
  - `workorders` 增加：`order_kind`（VARCHAR(16)，NOT NULL，server_default `one_off`，CHECK `one_off/standing`）、`medication_plan_id`（FK medication_plans，SET NULL）、`medication_order_id`（FK medication_orders，SET NULL）、`effective_from`/`effective_until`（DATE，可空）、`dedupe_key VARCHAR(160)`；部分唯一索引 `uq_workorders_dedupe_key (dedupe_key) WHERE dedupe_key IS NOT NULL`。
  - 新建 `workorder_reads`：`workorder_id`（FK workorders，CASCADE）、`account_id`（FK accounts，CASCADE）、`read_at TIMESTAMPTZ NOT NULL DEFAULT now()`；主键 `(workorder_id, account_id)`。
  - `workorder_events` 的事件类型 CHECK **重建**为 `created/read/disposition/closed/occurrence/confirmed/revised`（约束名 `ck_workorder_events_type`；不改旧迁移文件）。
  - 索引：`ix_workorders_medication_plan`、`ix_medication_plans_workorder`。
- **服药任务单与事实事件（服务层）**：
  - `ensure_order(session, *, plan, scheduled_at)`：`INSERT ... ON CONFLICT (plan_id, scheduled_at) DO NOTHING` 后回读；返回订单与是否新建。
  - `mark_reminded`：`pending→calling`，`attempt_count += 1`，`last_attempt_at=now`，追加 `reminded` 事件。
  - `mark_confirmed`：仅长者语音/文字，`confirmed` + `confirmed_at/confirmed_source`，追加 `confirmed` 事件。
  - `mark_denied_or_timeout`：追加 `denied/timeout`；未耗尽回 `pending`，耗尽转 `missed` + 追加 `missed` 事件，并调用漏服建单函数。
  - `mark_skipped`：员工/家属标记，`skipped` + `skip_reason`（仅员工可见），追加 `skipped` 事件。
  - `cancel_orders_for_plan`：计划暂停/取消时把未终态订单转 `cancelled`，追加 `cancelled` 事件。
  - 所有写操作与事件写入**同一事务**；发布 WS 一律在 commit 之后。
- **API**：
  - `POST /api/v1/elders/{elder_id}/medication-plans`：创建草稿（`status=planned`），写 `created_by_account_id`，返回完整计划。
  - `GET /api/v1/elders/{elder_id}/medication-plans`：按角色范围列出。
  - `GET /api/v1/medication-plans/{plan_id}`：详情（含确认状态、工单号）。
  - `PUT /api/v1/medication-plans/{plan_id}`：修改可编辑字段；写 `updated_by` + `revised` 事件（有工单时）：`payload={"plan_version": n, "changes":[{"field","before","after"}]}`，`plan_version` 取该计划 `confirmed/revised` 事件最大值 +1；只收白名单字段的**实际变化**；`internal_note` 只记 `{"field":"internal_note","changed":true}`，绝不记值。
  - `POST /api/v1/medication-plans/{plan_id}/confirm`：`planned → active`，写 `confirmed_at/by`，创建/复用长期工单；写 `confirmed` 事件 `payload={"plan_version": 1, "workorder_id": "..."}`；非 `planned` 返回 409。
  - `POST /api/v1/medication-plans/{plan_id}/pause|resume|cancel`：状态流转；`cancel` 同时调用 `close_standing_work_order_for_plan`，并把未终态订单转 `cancelled`。
  - `GET /api/v1/family/elders/{elder_id}/medication-plans`：家属只读；返回全部状态（含 `cancelled/completed`），但**永不返回 `planned` 草稿**，不含 `internal_note`。
  - `GET /api/v1/family/elders/{elder_id}/medication-plans/{plan_id}/history`：家属只读修改记录；数据来源为该计划关联长期工单的 `revised` 与 `confirmed` 事件，映射固定：时间 ← `event.created_at`、操作人 ← `actor_role`、内容 ← `payload.changes`、版本 ← `payload.plan_version`；`field == "internal_note"` 的项整条丢弃（不返回字段名、`changed` 标记与值）；`confirmed` 的 `changes` 为空数组，前端显示"计划已确认"；无事件时返回空数组。
  - `GET /api/v1/family/elders/{elder_id}/medication-history`：家属完整服药历史；参数 `plan_id/from/to/page/page_size`；按 `scheduled_at DESC` 分页（默认 30，最大 100）；返回 `order_id/plan_id/drug_name/dose/scheduled_at/status/attempt_count/confirmed_at/confirmed_source/events[]`，`events[]` 只含 `event_type/source/created_at`；**绝不返回** `internal_note`、`actor_account_id`、`device_id`、`payload` 原文、`workorder_id`、`skip_reason`。
  - `GET /api/v1/elders/{elder_id}/medication-orders`：员工侧核对接口（doctor 绑定/nurse 楼栋/admin 全量），返回内部字段。
- **长期工单**：
  - `type="medication_plan"`、`source=manual`、`priority=normal`、`order_kind=standing`、`medication_plan_id=plan_id`、`effective_from/until` 取计划起止日期。
  - 处理项：医生行 `handled`（记录发起人），护士行 `unread`（楼栋护工可见）；**不建 admin 处理项**。
  - `created` 事件 payload 写 `archive_roles=["admin"]`；管理员通过全量列表存档。
  - 计划修改后，护士处理项回到 `unread`，并写 `revised` 事件，让护工看到变更。
  - `close_work_order` 增加保护：`order_kind=standing` 且计划仍为 `planned/active/paused` 时，管理员关闭返回 409。
  - 新增 `close_standing_work_order_for_plan(session, *, plan_id, actor_account_id, reason)`：状态 `cancelled`、写 `closed_by/closed_note`、追加 `closed` 事件（`actor_role` 为发起医生或 `admin`，payload `{"reason": reason, "by": "plan_cancel"}`），绕过 `close_work_order` 的双角色 `handled` 门禁；commit 后推送 `ticket.resolved`。
- **事件发布时机**：
  - 段 1 起，所有工单/用药写路径**禁止在 commit 前 publish**；commit 前只构造轻量 DTO（`workorder_id/elder_id/type/category/status/priority/occurrence_count`）。
  - `voice._create_work_order_safely`、`mark_read`、`submit_disposition`、`close_work_order`、漏服建单、系统关闭均在 commit 成功后发布。
- **完成标志**：
  1. 未绑定医生创建 → 404；护士/家属/管理员创建 → 403；
  2. 绑定医生创建 → `planned`；确认 → `active` 且生成一张 `standing` 工单；
  3. 护工楼栋范围内可见该工单；管理员列表可见；家属只读接口可见（含历史与修改记录）且无 `internal_note`；
  4. 计划取消 → 工单系统关闭（`cancelled`）；管理员在计划仍生效时关闭工单 → 409；
  5. 同一 `(plan_id, scheduled_at)` 重复 `ensure_order` 只返回一条订单；
  6. 家属服药历史能看到 `confirmed/missed/skipped`，且不出现 `internal_note/workorder_id/device_id`；
  7. `alembic upgrade head`、`pytest`、`ruff` 通过。
- **与前后段关系**：段 1.5 复用本段的工单模型与事件；段 2 直接调用本段 API；段 3 的 `medication_query` 读本段数据；段 4 的外呼按本段 `active` 计划扫描并调用 `ensure_order`。
- **风险**：权限判定遗漏（必须用 `require_role` + `can_access_elder` 双层）；长期工单关闭语义；家属字段泄露；订单/事件与图状态一致性。

### 4.3 段 1.5｜工单四端实时 + 按账号已读（7.2-0 实时）

- **目标**：让管理员/家属也能自动看到工单变化；让医生/护士的"已读"落到具体账号；把列表筛选从"当前页客户端过滤"改为服务端筛选。
- **涉及文件**：
  - 后端：`app/models/workorder.py`、`app/services/workorder.py`、`app/services/workorder_events.py`、`app/api/routes/workorders.py`、`app/schemas/workorder.py`
  - 前端：`app/static/js/workorders.js`、`app/static/admin.html`、`app/static/family.html`、`app/static/nurse.html`、`app/static/doctor.html`
  - 测试：`tests/test_workorder_api.py`、`tests/test_workorder_service.py`、`tests/test_workorder_events.py`（如无则新建）
- **四端 WS（写死）**：
  - 新增唯一常量 `WS_ROLES = frozenset({doctor, nurse, admin, family})`；WS 鉴权与 `_resolve_recipients` 共用，禁止两处各写一份。
  - `_resolve_recipients` 增补家属：`link.status=active`、`valid_until IS NULL OR valid_until > now()`、`Account.role=family`、`Account.status=active`。
  - `_dispatch` 只有 `role in STAFF_ROLES` 才计算 `unhandled` 并补发 `ticket.counts`；admin/family 不发 counts。
  - `_payload` 增加 `type`；仍不返回 `description/context_snapshot/方案全文`；WS 只作刷新信号。
  - 心跳：客户端每 25 秒 `ping`，服务端 `pong`；60 秒无上行客户端关闭并转轮询；服务端按 JWT `exp` 到期主动关闭；单账号连接上限 3。
- **轮询兜底**：
  - 四端 WS `onclose/onerror` 或心跳超时后启动 30 秒 `setInterval`；重连成功停止。
  - staff 轮询"列表 + counts"；admin 轮询"列表"；family 轮询"当前选中长者列表"。
  - 无选中长者的家属不请求，只显示空态。
- **按账号已读**：
  - `workorder_reads` 写入用 `ON CONFLICT DO NOTHING`；保留处理项 `unread→read` 与 `OPEN→ACKNOWLEDGED` 的既有行为，但已读展示不再取 `account_id`。
  - `WorkorderListItem` 增加 `is_read: bool | None`、`read_at`；staff 返回实际值，admin/family 为 `None`。
  - `count_open_by_role` 改为"该账号未读且角色未 handled"的数量，条件增加 `NOT EXISTS (workorder_reads ... account_id=:account_id)`。
  - 前端未读圆点用 `!it.is_read`，处理状态标签继续用 `my_status`；文案区分"本人未读 / 角色已处理"。
- **列表与筛选契约**：
  - `WorkorderListItem` 增加 `type`、`title`；前端类别列 `category || TYPE_LABEL[type]`（`medication_plan→长期用药`、`missed_medication→漏服`、`sensitive→敏感事件`）。
  - 列表接口统一支持 `status`（`WorkorderStatus` 值）；家属接口补该参数。
  - 员工端新增 `mine=unread|read|handled`；admin/family 传 `mine` 返回 422。
  - 前端删除当前页 `filteredItems()`，把 `status/mine` 下推到服务端分页。
- **推送时机**：
  - `mark_read` / `submit_disposition` / `close_work_order` 路由在 `commit()` 之后发布；禁止事务内 publish。
- **完成标志**：
  1. 管理员登录后打开工单面板，新工单无需手动刷新即可出现；
  2. 家属选中长者后，新工单自动出现；未绑定长者收不到事件；
  3. 两名护士各自打开同一工单，A 点已读后，B 仍显示本人未读；
  4. 员工选择"未读/已读/已处理"筛选，翻页后结果仍正确；
  5. 手动断开 WS 后 30 秒内轮询补上变化；
  6. token 到期后连接被关闭，客户端重连并重新鉴权。
- **与前后段关系**：复用段 1 的工单模型与事件；段 2 的工单页适配依赖本段的 `is_read/type/title/mine`。
- **风险**：进程内 hub 只覆盖单 worker；多 worker 时轮询是唯一兜底；管理员/家属不要误用 `notifyCreated` 员工弹窗。

### 4.4 段 2｜医生端最小界面 + 家属只读/服药历史 + 工单页适配（7.2-0 UI）

- **目标**：让段 1/1.5 的接口可演示：医生在现有面板完成"创建→确认页→下发"，家属看到只读计划与**完整服药历史**，护工/管理员在现有工单页看到长期用药工单并自动刷新。
- **涉及文件**：
  - 修改 `app/static/doctor.html`（把"用药方案管理 后续版本"占位替换为可用模块）
  - 修改 `app/static/family.html`（新增"用药计划（只读）"与"服药历史"区块）
  - 新增 `app/static/js/medication.js`（计划表单、确认页、家属历史时间线；被 doctor/family 页面共用）
  - 修改 `app/static/js/workorders.js`（长期工单标识；管理员不可关闭 standing 工单；四端 WS/轮询/已读/筛选）
  - 可能修改 `app/static/js/api.js`（如现有 `KB.api` 已支持 PUT 则不动）
- **界面要求**：
  - 医生端：按长者卡片进入"用药计划"；列表显示药名/剂量/时间/状态/起止；新建与编辑表单字段：药名、剂量、服用时间（可多个）、星期、起止日期、用药说明、内部备注；保存后**弹出确认页**，完整展示全部字段与"确认后将下发长期工单并开始提醒"的提示，医生点"确认下发"才调用 confirm。
  - 已生效计划修改：表单提交前同样展示确认页（旧值 → 新值），确认后才 PUT。
  - 家属端：只读列表 + 状态筛选（含已取消/已完成历史）；显示药名、剂量、时间、说明、状态、起止；点开可看修改记录时间线；**服药历史按订单倒序分页**，每条显示时段、状态（待确认/已服/漏服/已跳过/已取消）、尝试次数、确认来源、事件时间线；不显示内部备注、工单号、跳过原因、账号 UUID，不显示任何编辑按钮。
  - 家属用药历史与工单共用长者选择器：`sessionStorage` 键 `kb_wo_elder`；切换长者立即刷新两个区块；无长者显示空态且不发请求。
  - 工单页：列表增加"长期用药/漏服"标识；管理员详情页对 `order_kind=standing` 隐藏/禁用关闭按钮；护工仍可提交处理项（表示已知悉），但不会关闭计划；四端按段 1.5 连接 WS 与轮询。
- **完成标志**：
  1. 医生（绑定）能完成创建→确认页→下发；未绑定医生看不到创建入口且直接调接口也被拒；
  2. 家属页面可见只读计划（含已取消/已完成历史与修改记录）与完整服药历史，刷新后仍可见，且无内部备注/工单号/账号 UUID；
  3. 家属切换长者后，计划与服药历史同时切换，无长者时不发请求；
  4. 护工/管理员工单页可见该长期工单并自动刷新；管理员无法在计划生效时关闭；
  5. 不引入前端框架；`ruff` 通过（后端未改逻辑）。
- **与前后段关系**：只消费段 1/1.5 接口；不改路由/实时/数据库；段 3/4 不依赖本段。
- **风险**：确认页被绕过（直接 PUT）——后端仍有审计与权限兜底；自动播放/登录态问题；家属历史分页与筛选组合。

### 4.5 段 3｜用药确认图 + 工具接入（7.2-a）

- **目标**：打通 LangGraph/checkpointer，用"对话内确认"完成图的全部状态流转；外呼留到段 4。
- **涉及文件**：
  - 新增 `app/graphs/__init__.py`、`app/graphs/checkpointer.py`、`app/graphs/medication.py`
  - 修改 `app/services/medication_plan.py`（今日用药查询；避免与段 1 服务重复）
  - 修改 `app/services/medication_order.py`（图内状态推进写订单与事件：`ensure_order`/`mark_reminded`/`mark_confirmed`/`mark_denied_or_timeout`）
  - 修改 `app/agent/tools.py`（`medication_query` 真实化，新增 `medication_confirm`）
  - 新增 `scripts/seed_medication_plan.py`（仅测试/演示用；不得替代医生端创建）
  - 新增 `tests/test_medication_graph.py`，补充 `tests/test_agent_tools.py`、`tests/test_medication_order.py`
- **前置**：段 1（计划、订单与事件表）；`langgraph` 需新增依赖。
- **图的边界（写死）**：
  - `thread_id = medication:{order_id}`；一条订单对应一个图实例，不再用 `plan+date+slot` 拼接。
  - **DB 订单状态是权威**：图状态只负责可恢复执行；每次节点推进先写订单/事件，再写图状态（同一业务事务内先 PG，后 checkpointer；崩溃后以 PG 为准对账）。
  - 重试次数以 `medication_orders.attempt_count` 为准，图内计数不得单独成为唯一来源。
- **完成标志**：
  1. 存在 `active` 计划 → "我该吃什么药" → 返回真实药名/剂量/时间；
  2. `thread_id=medication:{order_id}`，节点/条件边/重试可在 `graph.get_state` 查看；
  3. 连续 3 次"没吃" → `escalated` + `medication_orders.status=missed` + `workorders` 出现 `type=missed_medication`、`order_kind=one_off`、`medication_order_id` 指向该订单；
  4. 每次提醒/确认/否认/超时都写入 `medication_events`，事件可按订单回放；
  5. 重启后同 `order_id` 能读回 `attempt/status`，且不会重复建单；
  6. 降级为单轮确认时，也必须写订单与事件，不得只回话术；
  7. 不修改计划本身（对话确认只改变服药事实，不改医生下发的计划）。
- **与前后段关系**：段 4 复用本段图与订单服务；本段不处理"到点触发"。
- **风险**：PG 与 SQLite checkpointer 无法同一事务（必须写清权威顺序）；确认语义与医生计划混淆；重复建单。

### 4.6 段 4｜定时外呼 + 反向 WebRTC（7.2-b）

- **目标**：把段 3 的图接到"到点主动发起"，完成反向 WebRTC、超时重试与启动补漏。
- **涉及文件**：`app/core/lifespan.py`、`app/core/config.py`、新增 `app/services/outbound.py`、`app/realtime/session.py`、`app/api/routes/voice.py`、`app/static/js/voice.js`、`scripts/trigger_medication_call.py`、`tests/test_outbound_scheduler.py`、`tests/test_outbound_call.py`。
- **前置**：段 3；平板 WS 已连接；`apscheduler` 属 `infra` 可选依赖，未安装时调度器不启动并记 `medication_scheduler_unavailable`。
- **调度口径（写死）**：
  - 每 `medication_scan_interval_seconds`（默认 30 秒）扫描 `active` 计划窗口内时段；`ensure_order` 用唯一键 `(plan_id, scheduled_at)` + `ON CONFLICT DO NOTHING`。
  - 启动补漏：lifespan 启动后、首轮扫描前，补齐今天已过时段且缺订单的项，创建为 `missed` 并建漏服工单；只补 `medication_backfill_lookback_hours`（默认 24 小时）内。
  - 进程内缓存只作加速，不作为唯一去重；DB 唯一键才是幂等边界。
  - 单实例部署；多实例唯一键防重复订单，但不防重复外呼。
- **外呼与重试**：到点 → 设备解析 → 反向 WebRTC（失败降级 WS TTS）→ 30 秒超时 → `mark_denied_or_timeout` → 未耗尽重试，耗尽 `missed` + 建单。
- **完成标志**：手动触发出声；30s 不回应 → 重试；3 次 → 漏服工单；设备离线 → `degraded=rtc_unavailable` + WS TTS；失败四分类；重启不重复生成订单、不重置 `attempt_count`；无 `new_event_loop`/独立线程；补漏只覆盖 24 小时内。
- **风险**：全项目最高（aiortc 服务端 offer、前端 answer、自动播放、无 TURN）；双实例重复外呼。

### 4.7 段 5｜长期记忆五步（7.1）

- **目标**：记忆从表变成可召回、可过滤、可冲突治理、可遗忘的服务，并注入对话。
- **涉及文件**：新增 `app/integrations/embeddings.py`、`app/services/memory.py`、`scripts/memory_maintenance.py`、`tests/test_memory_service.py`、`tests/test_memory_isolation.py`；修改 `voice.py`、`session_state.py`、`conversation_store.py`、`agent/tools.py`、`config.py`。
- **前置**：无硬依赖；无 Embedding 走本地哈希降级。
- **完成标志**：糖尿病记忆跨会话命中；跨长者召回为空；矛盾置 `superseded`；5 条召回经二次过滤；归并幂等；日志无原文。
- **风险**：Embedding 选型；LLM 二次过滤成本；重复萃取。

### 4.8 段 6｜娱乐点播（7.3）

- **目标**：媒体库可播，规则层点播出声，播放记录落库。
- **涉及文件**：新增 `alembic/versions/0009_media_history.py`、`app/models/media_history.py`、`app/services/media.py`、`scripts/seed_media_library.py`、`tests/test_media_service.py`；修改 `agent/tools.py`、`router/rules.py`、`router/executor.py`、`session_state.py`、`api/routes/voice.py`、`voice.js`、模型注册与相关测试。
- **前置**：段 5（偏好）；素材文件；`0009`。
- **完成标志**：越剧直接播；换段零模型；停并补结束；无音源明确降级；迁移/测试通过。
- **风险**：素材版权/缺失；自动播放；前后端协议。

### 4.9 段 7｜Iter3 基线 + 失败归因（7.4）

- **目标**：把 1–6 段变成可复算数字与可归因 bad case，并补齐 trace。
- **涉及文件**：新增 `eval/fixtures/day7_scenarios.jsonl`、`scripts/eval_day7.py`、`scripts/eval_gate.py`、`eval/reports/v1_baseline.md`、`tests/test_eval_day7.py`；修改 `voice.py`/`logging.py`，可选 `mcp_client.py`。
- **前置**：段 1、1.5、2–6。
- **完成标志**：8 项指标 + 20 条明细 + 分层归因 + gate 退出码；offline 可复算；日志无原文。
- **风险**：LLM 非确定性；token 成本估算；live/offline 口径。

---

## 5. 风险与返工点

| 风险 | 概率 | 影响 | 应对 |
| --- | --- | --- | --- |
| 用药权限判定遗漏（护士/未绑定医生可写） | 中 | 医疗安全与合规事故 | `require_role("doctor")` + `can_access_elder` 双层；测试覆盖 4 类角色 + 未绑定医生 |
| 家属接口泄露 `internal_note` | 中 | 隐私投诉 | 家属 schema 独立，字段白名单；测试断言响应不含该键 |
| 长期工单被护士一次处理即关闭 | 高 | "长期"语义失效 | `order_kind=standing`；护士处理只改处理项，不改工单状态；管理员关闭加保护 |
| 计划仍生效时管理员误关工单 | 中 | 护工看不到长期计划 | `close_work_order` 409 保护；计划取消才系统关单 |
| "绑定护工"粒度不符产品预期 | 低 | 投递范围争议 | 已拍板楼栋级；若改人到人再加表（§1.4 P2） |
| 确认页被绕过（直接 PUT 生效） | 中 | 未确认的用药变更 | 后端记 `revised` 事件与操作人；UI 强制确认页；已拍板不做暂存变更（§1.4 P5） |
| 家属历史/修改记录泄露 `internal_note` | 中 | 隐私投诉 | 历史接口字段白名单；`field == "internal_note"` 的变更项**整条过滤**（不返回字段名、`changed`、值）；测试断言 |
| 用药计划与健康档案双真源 | 中 | 提醒读错数据 | 明确 `medication_plans` 为唯一提醒源；`health_records` 不参与调度 |
| 反向 WebRTC 打不通 | 中高 | 7.2 验收失败 | 已拍板：WS TTS 降级 + `degraded=rtc_unavailable`，先保证链路可用 |
| 自动播放策略拦截 | 高 | 演示无声 | 首次点击解锁；验收脚本写明 |
| Checkpointer 口径改变 | 低 | 段 3/4 返工 | 已拍板 SQLite；工厂隔离，换 PG 只改一个文件 |
| Embedding 服务不可用 | 中 | 记忆召回降级 | 已拍板哈希 n-gram 降级；日志/报告标注实际模型 |
| 媒体素材缺失/版权不清 | 高 | 7.3 验收不完整 | 已拍板先用本地占位音源验链路；真实清单由课程/教师提供 |
| 给药/漏服双表数据量增长 | 中 | 查询变慢、存储上涨 | `medication_orders` 唯一键 + 仅保留必要字段；`medication_events` 只追加元数据；按 `elder_id/scheduled_at` 建索引；历史分页 |
| 时区换算错误（本地时间 vs UTC） | 中 | 提醒错点、漏服误判 | 计划时间按 `settings.app_timezone` 解释后存 `TIMESTAMPTZ`；`ensure_order` 统一走一个时间函数；测试覆盖跨日/边界 |
| 双实例重复外呼 | 中 | 长者被重复打扰 | 文档写死单实例；唯一键只防重复订单；多副本需 Redis 锁/广播（Day8+） |
| 推送早于事务提交 | 中高 | 客户端刷新看不到数据、幽灵事件 | 所有写路径 commit 后 publish；commit 前只构造 DTO；测试断言提交前无推送 |
| 家属误读服药历史（把"待确认"当"未服"） | 中 | 家属焦虑/投诉 | 历史状态文案固定为"待确认/已服/漏服/已跳过/已取消"；时间线与尝试次数一并展示；不做依从率结论 |
| 管理员/家属 WS token 过期后仍收消息 | 中 | 越权风险 | 连接按 JWT `exp` 到期主动关闭；客户端重连重新鉴权；账号停用不承诺实时生效 |
| 多 worker 下 WS 事件丢失 | 中 | 实时不完整 | 30 秒轮询兜底；文档写明单进程假设 |
| 已读回执与角色处理语义混淆 | 中 | 前端显示错误 | 文档写死"已读按账号、处理按角色"；`is_read` 与 `my_status` 分开返回 |

---

## 6. 推进顺序与可调整性

**推荐顺序：段 1 → 段 1.5 → 段 2 → 段 3 → 段 4 → 段 5 → 段 6 → 段 7。**

- 段 1 先做：它是新增产品要求的根，也是段 1.5/2/3/4 的数据来源；同时修掉"用药计划没有入口"的硬伤。
- 段 1.5 紧随段 1：工单模型刚改完，实时与已读一起验收成本最低。
- 段 2 紧随：确认页只有做了界面才能演示；后端已完成，前端风险低。
- 段 3 在段 1 之后：`medication_query` 要读真实计划，图要写订单与事件。
- 段 4 风险最高，越早暴露越好；失败也只影响本段，不阻塞记忆/媒体。
- 段 5 可与段 4 并行（互不依赖）；若更想先看到"有记忆"，可提前到段 4 之前。
- 段 6 必须在段 5 之后（偏好）；可先做 `0009` 迁移与种子，再留前端。
- 段 7 严格最后，数字必须能复算。

**不建议的顺序**

- 先做段 7：没有功能，指标只能编。
- 先做段 6 再做段 5：点播没有记忆偏好，7.3 验收缺一半。
- 段 1 只做 API 不做长期工单：段 2/4 的"护工收到"无法验收。
- 段 1 只建工单不建订单/事件：段 3/4 会各自补一套，最终语义分裂。
- 段 1.5 留到最后：实时与已读会反过来改工单 schema 和四端页面，返工面最大。

---

## 7. 已拍板结论（无待决项）

产品 6 条、技术 5 条与本次新增 8 条已全部落定，结论、理由、降级与切换成本见 **§1.4**。执行时不再询问，按 §1.4 与 §4 的段内要求直接做即可。

补充确认一条非产品决策但必须写死的口径：**`medication_plans` 是提醒的唯一真源**，`health_records(category='medication')` 仅作自由文本档案，不自动镜像、不参与调度。该口径已并入 §1.2 I 与 §5 风险表。

---

## 8. 每段验收命令（供执行后自检）

```powershell
# 启动前（项目约定）
powershell -File scripts\start_all.ps1

# 段 1
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m pytest tests\test_medication_plan_api.py tests\test_medication_permissions.py tests\test_medication_order.py tests\test_workorder_service.py -q
.\.venv\Scripts\python.exe -m ruff check .

# 段 1.5
.\.venv\Scripts\python.exe -m pytest tests\test_workorder_api.py tests\test_workorder_service.py tests\test_workorder_events.py -q
# 手工：管理员/家属双开自动刷新；两名护士双浏览器独立已读；断 WS 后 30 秒轮询

# 段 2（浏览器手工）
# 医生登录 → 长者卡片 → 用药计划 → 新建 → 确认页 → 下发 → 护工/管理员/家属三端核对
# 家属端：切换长者 → 计划与服药历史同步切换 → 历史不含内部备注/工单号/账号 UUID

# 段 3
.\.venv\Scripts\python.exe -m pytest tests\test_medication_graph.py tests\test_agent_tools.py -q
.\.venv\Scripts\python.exe -m ruff check .

# 段 4（单测 + 手工）
.\.venv\Scripts\python.exe -m pytest tests\test_outbound_scheduler.py tests\test_outbound_call.py -q
.\.venv\Scripts\python.exe scripts\trigger_medication_call.py --plan-id <uuid>

# 段 5
.\.venv\Scripts\python.exe -m pytest tests\test_memory_service.py tests\test_memory_isolation.py -q
.\.venv\Scripts\python.exe scripts\memory_maintenance.py --dry-run

# 段 6
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe scripts\seed_media_library.py --manifest <path>
.\.venv\Scripts\python.exe -m pytest tests\test_media_service.py -q

# 段 7
.\.venv\Scripts\python.exe scripts\eval_day7.py --out eval\reports\v1_baseline.md
.\.venv\Scripts\python.exe scripts\eval_gate.py --report eval\reports\v1_baseline.md
```

---

## 9. 附：本方案不做的事

- 不改 `docs/康伴3D数字人_大纲.md`，不改任何已执行迁移，不执行 `git push`。
- 只新建 `medication_orders`、`medication_events`、`workorder_reads` 三张表；仍不新建 `nurse_elder_assignments`（已拍板，见 §1.4）。
- 不重做看板、不做趋势图/统计大盘/家属 H5 日报；不做站外通知（短信/微信/推送）与运营角标；允许工单 WS、30 秒轮询、按账号回执与服药历史。
- 不做医疗诊断、不虚构长者数据、不下载/加载模型权重、不接入未授权媒体。
- 不把真实长者姓名、房间号、病史写入种子、测试或报告。
