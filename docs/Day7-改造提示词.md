# Day7 改造提示词清单

> 配套文档：`docs/Day7-改造方案.md`。
> 用法：全文共 **7 段**，按段号顺序，一次只把**一段**的提示词整段复制给执行改造的 AI；每段独立可执行，不依赖"上一段说过什么"。
> 段间共用约定已写进每段提示词，复制时不要删减"约束"与"验收"。
> 所有提示词都假设：工作区为项目根目录、虚拟环境在 `.venv`、本地 PostgreSQL(5433)/Redis(6379) 已启动、执行者先读 `AGENTS.md`。
> **统一要求**：每段完成后只提交改动与验收结果给用户审查，**不要自行 `git commit` / `git push` / 打 tag**。

## 全局禁止清单（7 段都适用）

- **不得重写/替换主控 Agent**（`app/agent/companion.py`）：只能在工具注册表增量加工具或改单个工具实现。
- **不得修改任何已存在的 Alembic 迁移文件**：只允许新增迁移；旧迁移一行不改。
- **不得替换/重建现有表结构**：只允许 ADD COLUMN、ADD TABLE、扩 CHECK；不得 DROP/RENAME 旧列、不得清空数据。
- **不得引入未经批准的新框架**（前端框架、ORM、任务队列、向量库、Agent 框架）；只允许方案 §1.4 明确列出的 LangGraph 三件套。
- **不得删改现有前端页面的结构与既有功能**：只能扩展 `doctor.html` / `family.html` / `workorders.js` 的用药相关部分。
- **不得新建** `medication_orders` / `medication_events` / `nurse_elder_assignments`。
- **不得做**消息通知、未读角标、看板/图表/统计大盘、家属 H5 日报。
- **外部依赖未配置时必须可启动**：不得让 import 失败阻塞应用启动；必须有明确降级与日志标记。
- **不得执行** `git commit` / `git push` / 打 tag；不得写真实长者数据、密钥或模型权重。

---

## 段 1｜医生端用药计划闭环：权限 + 确认 + 长期工单（对应 7.2-0 后端）

```text
【任务】Day7 段1：实现"医生端用药计划"的后端闭环——绑定医生可创建/修改计划，创建后进入待确认状态，医生确认后生成一张"长期用药工单"投递给护工，管理员可见并存档，家属只读可见。本段只做后端与数据库，不改前端页面。

【背景】
项目是康伴 3D 数字人康养陪伴系统（FastAPI + SQLAlchemy 2.x async + PostgreSQL + Alembic）。已有：
- 账号与角色：`app/models/account.py`、`app/auth/dependencies.py`，角色含 admin/nurse/doctor/family；
- 医生-长者绑定：`app/models/assignment.py` 的 `doctor_elder_assignments`（有 status/valid_from/valid_until）；
- 护工-楼栋绑定：`nurse_building_assignments`；家属-长者绑定：`family_elder_links`；
- 统一范围判定：`app/services/authorization.py#can_access_elder`（返回 ALLOWED / HIDDEN 等）；
- 工单：`app/models/workorder.py`、`app/services/workorder.py`、`app/api/routes/workorders.py`，已有员工/管理员/家属三套路由与处理项、事件、关闭流程；
- 用药计划表：`app/models/medication.py#MedicationPlan`，只有基础字段，没有作者、确认状态、工单关联。
本段要按产品要求补齐用药计划的权限与投递，不重写已有鉴权和工单模块。

【当前状态（先核对，不要凭记忆改）】
1. 先读 `app/models/medication.py`、`app/models/workorder.py`、`app/models/assignment.py`、`app/services/authorization.py`、`app/auth/dependencies.py`、`app/services/workorder.py`、`app/api/routes/workorders.py`、`app/api/router.py`。
2. 确认 `MedicationPlanStatus` 的实际枚举值（`planned/active/paused/...`）；用现有值，不要新增状态。
3. 确认 `workorder_events` 的事件类型 CHECK 实际允许值；迁移里只做追加，不要回改旧迁移。
4. 确认 `workorder_dispositions.role` 只允许 doctor/nurse（本段**不**增加 admin 处理项）。
5. 确认现有角色依赖函数名（如 `require_role`）与当前账号依赖的用法，按 `app/api/routes/health_profile.py` 的既有写法接入。
6. `alembic/versions/` 最新为 `0007_workorder_flow.py`；新迁移编号从 `0008` 起。

【目标】
1. 新增迁移 `0008_medication_order_flow.py`：给用药计划加工商字段，给工单加长期语义，扩工单事件类型；不新建表、不改旧迁移。
2. 新增用药计划领域服务与 API：创建草稿、读取、修改、确认、暂停、恢复、取消。
3. 权限写死：只有"绑定该长者且绑定有效"的医生可以创建/修改/确认/暂停/恢复/取消；其他角色（含未绑定医生）一律不能。
4. 确认后生成一张长期工单：护工在楼栋范围内可见并收到，管理员全量可见并存档，家属只读可见用药计划。
5. 计划取消时系统关闭长期工单；计划仍生效时，管理员不能手工关闭该长期工单。

【具体要求】
一、迁移 `0008_medication_order_flow.py`
- `medication_plans` 增加列（全部可空，避免影响既有数据）：
  - `created_by_account_id` UUID，FK `accounts.account_id` ON DELETE SET NULL；
  - `updated_by_account_id` UUID，同上；
  - `confirmed_at` timestamptz，可空；
  - `confirmed_by_account_id` UUID，同上；
  - `workorder_id` UUID，FK `workorders.workorder_id` ON DELETE SET NULL；
  - `internal_note` TEXT，可空（医生内部备注，家属不可见）。
- `workorders` 增加列：
  - `order_kind` VARCHAR(16) NOT NULL server_default `'one_off'`，CHECK IN (`'one_off'`,`'standing'`)；
  - `medication_plan_id` UUID，FK `medication_plans.plan_id` ON DELETE SET NULL；
  - `effective_from` DATE 可空；`effective_until` DATE 可空。
- `workorder_events` 的事件类型 CHECK 追加 `'revised'`、`'confirmed'`（以实际约束名为准；只 drop/重建该 CHECK，不动数据）。
- 索引：`ix_workorders_medication_plan (medication_plan_id)`、`ix_medication_plans_workorder (workorder_id)`。
- downgrade 精确回退本迁移新增的列、约束、索引；不得删除旧表或旧列。
- 同步修改 ORM 模型；字段名、类型、可空性与迁移一致。

二、可编辑字段与校验（新增 `app/schemas/medication.py`）
- 创建/修改只允许这些字段：`drug_name`、`dose`、`schedule`、`instructions`、`internal_note`、`start_date`、`end_date`。
- 禁止客户端传：`plan_id`、`elder_id`（以路径为准）、`status`、`created_by_account_id`、`confirmed_at/by`、`workorder_id`；出现即 422 或忽略并在测试中固定行为（推荐 422）。
- `schedule` 固定形状：`{"times": ["HH:MM", ...], "weekdays": [0..6, ...]}`。
  - `times` 至少 1 个、最多 6 个，去重升序；格式 24 小时 `HH:MM`；
  - `weekdays` 为空或缺省表示每天；
  - `start_date <= end_date`（若有）。
- 长度：`drug_name <= 160`、`dose <= 100`、`instructions <= 2000`、`internal_note <= 2000`。
- 返回 schema 分三类：
  - `MedicationPlanDoctorView`：医生/管理员可见，含 `internal_note`、`created_by`、`confirmed_at/by`、`workorder_id`；
  - `MedicationPlanNurseView`：护工可见，含 `internal_note`（照护需要），不含账号 UUID；
  - `MedicationPlanFamilyView`：家属只读，字段白名单，**绝不含 `internal_note`、`created_by_account_id`、`confirmed_by_account_id`、`workorder_id`**。

三、领域服务（新增 `app/services/medication_plan.py`）
- `create_plan(db, *, elder_id, payload, account)`：校验医生绑定 → 写 `status=PLANNED`、`created_by_account_id`。
- `list_plans(db, *, elder_id, account, status=None)`：按角色范围过滤；护士只能看到 `active/paused/completed`，**不得看到 `planned` 草稿**；医生/管理员可看全部；家属走独立接口。
- `get_plan(db, *, plan_id, account)`：同一套范围判定；越权按"角色不符 403 / 范围外 404"。
- `update_plan(db, *, plan_id, payload, account)`：只允许 `planned/active/paused`；`cancelled/completed` 返回 409；写 `updated_by_account_id`；若有长期工单，追加 `revised` 事件（payload 含 before/after，仅入库，不进日志）。
- `confirm_plan(db, *, plan_id, account)`：仅 `planned → active`；写 `confirmed_at/by`；创建或复用长期工单；返回计划 + `workorder_id`；非 `planned` 返回 409。
- `pause_plan / resume_plan / cancel_plan`：使用现有枚举；`cancel_plan` 调系统关闭长期工单。
- 权限辅助：
  - 写操作先 `require_role("doctor")`（路由层），服务层再调 `can_access_elder`；非 ALLOWED 视为 404；
  - 读操作按角色：admin 放行；doctor/nurse/family 必须 `can_access_elder == ALLOWED`；
  - 不要复用健康档案的 `can_edit_basic` / `can_view_*` ACL，避免护工通过健康档案编辑位改用药计划。
- 所有查询必须显式带 `elder_id` 或 `plan_id + 范围校验`；禁止"先按 plan_id 查出来再判断"的隐式信任。

四、API（新增 `app/api/routes/medication.py`，在 `app/api/router.py` 以 `/api/v1` 注册）
- `POST /api/v1/elders/{elder_id}/medication-plans`：医生创建草稿，201。
- `GET /api/v1/elders/{elder_id}/medication-plans`：医生/护工/管理员列表，支持 `status` 过滤。
- `GET /api/v1/medication-plans/{plan_id}`：详情。
- `PUT /api/v1/medication-plans/{plan_id}`：修改，200。
- `POST /api/v1/medication-plans/{plan_id}/confirm`：确认并下发，200。
- `POST /api/v1/medication-plans/{plan_id}/pause`、`/resume`、`/cancel`：200。
- `GET /api/v1/family/elders/{elder_id}/medication-plans`：家属只读；返回全部状态（含 `cancelled/completed`），但**永不返回 `planned` 草稿**，不含 `internal_note`。
- `GET /api/v1/family/elders/{elder_id}/medication-plans/{plan_id}/history`：家属只读修改记录；从长期工单的 `revised` 事件生成，字段白名单（时间、变更字段、变更前/后值），**过滤 `internal_note` 的内容与字段名**；若无记录返回空数组。
- 所有写接口返回明确错误：角色不符 403、范围外 404、状态不允许 409、参数错 422。
- 设备/语音侧不直接调这些 HTTP 接口，仍通过工具读服务层（段 3 实现）。

五、确认后生成长期工单（修改 `app/services/workorder.py`）
- 新增 `create_medication_plan_work_order(db, *, plan, doctor_account_id)`：
  - `type="medication_plan"`、`source=WorkorderSource.MANUAL`、`priority=WorkorderPriority.NORMAL`、`status=OPEN`；
  - `order_kind="standing"`、`medication_plan_id=plan.plan_id`；
  - `effective_from=plan.start_date`、`effective_until=plan.end_date`；
  - `title` 用"长期用药计划：{drug_name}"；`description` 只放药名/剂量/服用时间/用药说明，**不放 `internal_note`**（家属会看到 description）；
  - 处理项：医生行 `role=doctor`、`status=handled`、`account_id=doctor_account_id`、`plan_text="发起长期用药计划"`、`handled_at=now`；护士行 `role=nurse`、`status=unread`、`account_id=NULL`；
  - 写一条 `created` 事件，payload 至少含 `{"source":"doctor_medication_plan","order_kind":"standing","archive_roles":["admin"],"plan_id":"..."}`；
  - 沿用现有工单创建后的广播方式（参考 `create_sensitive_work_order` 的 WS/事件发布），让护工端能实时刷新；
  - 幂等：同一 `plan_id` 已有 `order_kind=standing` 且未关闭的工单时复用，不重复建单。
- 修改 `close_work_order`：当 `order_kind=="standing"` 且关联计划状态为 `planned/active/paused` 时，抛业务冲突（HTTP 409），提示"长期用药工单随用药计划停用/取消自动关闭"；计划已 `cancelled/completed` 时允许关闭。
- 新增系统关闭函数 `close_standing_workorder_for_plan(db, *, plan, reason, account)`：不经过管理员权限校验，置 `status=CANCELLED`，写 `closed` 事件；事件 `actor_role/actor_account_id` 用触发取消的医生账号（`ck_workorder_events_actor_role` 只允许 doctor/nurse/admin/family，不能写 `"system"`），payload 含 reason；仅由 `cancel_plan` 调用。

六、工单契约（修改 `app/schemas/workorder.py`）
- 员工/管理员/家属的工单列表项与详情增加只读字段：`order_kind`、`medication_plan_id`、`effective_from`、`effective_until`。
- 家属工单详情继续不返回 `context_snapshot`；长期用药工单的 `description` 已按第五点去除内部备注。
- 不改变现有状态机、处理项接口与权限。

七、审计与日志
- 结构化日志（只记元数据，不记 `instructions/internal_note` 原文）：`medication_plan_created`、`medication_plan_confirmed`、`medication_plan_revised`、`medication_plan_paused/resumed/cancelled`、`medication_plan_workorder_created/closed`。
- 每条日志带 `elder_id`、`plan_id`、`account_id`、`role`、`workorder_id`、`trace_id`（若上下文已有）。
- 修改计划的 before/after 只写工单事件 JSONB，不写应用日志。
- 不把真实长者姓名、房间号、病史写入测试或日志。

八、测试
- 新增 `tests/test_medication_permissions.py`：
  - 绑定医生可创建/修改/确认；未绑定医生 404；护士/家属/管理员写操作 403；
  - 医生绑定过期后不可写；
  - 家属只读接口不含 `internal_note` 等敏感键；家属历史接口不返回 `internal_note` 字段名或内容；
  - 护工只能读本楼栋长者的计划。
- 新增 `tests/test_medication_plan_api.py`：
  - 创建为 `planned`；确认后 `active` 且 `workorder_id` 非空；
  - 重复确认 409；取消后工单 `cancelled`；
  - 修改可编辑字段成功，传只读字段 422；
  - `schedule` 非法时间/星期/起止日期 422。
- 补充 `tests/test_workorder_service.py`：
  - 长期工单处理项为 doctor=handled、nurse=unread；
  - 管理员在计划生效时关闭 → 409；
  - 计划取消后系统关闭成功；
  - 重复确认不产生第二张 standing 工单。

【涉及文件】
- 新增：`alembic/versions/0008_medication_order_flow.py`、`app/schemas/medication.py`、`app/services/medication_plan.py`、`app/api/routes/medication.py`、`tests/test_medication_plan_api.py`、`tests/test_medication_permissions.py`
- 修改：`app/models/medication.py`、`app/models/workorder.py`、`app/schemas/workorder.py`、`app/services/workorder.py`、`app/api/router.py`、`tests/test_workorder_service.py`
- 不改：`app/static/**`、`app/realtime/**`、`app/router/**`、`app/agent/**`、`app/auth/**`、旧 alembic 迁移

【必须遵守的约束】
- 权限必须"角色 + 绑定"双层校验；不得只靠前端隐藏按钮。
- 不得让护士/家属/管理员创建或修改用药计划；不得复用健康档案 ACL 放行。
- 不新建 `medication_orders` / `medication_events` / `nurse_elder_assignments` 表；长期工单复用 `workorders`。
- 不新增 admin 处理项；管理员"存档"通过全量列表与 created 事件体现。
- 家属接口字段白名单，绝不返回 `internal_note`。
- 不回改旧迁移；`0008` 的 downgrade 必须可回退。
- 不实现通知推送、不做看板/图表、不改前端页面。
- 不执行 `git commit` / `git push` / 打 tag。
- 不写真实长者数据；测试用"测试长者A/B"和通用药名（如"降压药"）。

【验收标准】
1. `alembic upgrade head` 成功；`\d medication_plans`、`\d workorders` 字段与索引正确；`downgrade` 可回退 `0008`。
2. `pytest tests/test_medication_plan_api.py tests/test_medication_permissions.py tests/test_workorder_service.py -q` 全过；`ruff check .` 通过。
3. 手工/接口验证：绑定医生创建 → `planned`；确认 → `active` + 一张 `order_kind=standing` 工单；护士端列表能看到；管理员列表能看到；家属只读接口能看到（含已取消/已完成历史与修改记录）且无内部备注。
4. 未绑定医生创建 → 404；护士/家属/管理员创建 → 403；重复确认 → 409。
5. 计划取消 → 工单 `cancelled`；计划生效时管理员关闭工单 → 409。
6. 日志抽查：无 `internal_note/instructions` 原文、无真实长者信息。

【完成后请说明】
- 迁移字段、约束、索引与 downgrade 验证结果；
- 权限矩阵的实际实现位置与 403/404 口径；
- 可编辑字段白名单与校验规则；
- 确认流程与长期工单字段、处理项、事件、幂等逻辑；
- 家属只读接口的字段白名单与历史/修改记录脱敏规则；
- 验收命令与真实输出；
- 已知限制与降级路径（不新增 `medication_events`；家属历史来自工单事件）。

【本段不做什么】
- 不改前端任何页面；不做 LangGraph、不做 APScheduler、不做反向 WebRTC、不做记忆/媒体/评测。
- 不新建 `medication_orders` / `medication_events` / `nurse_elder_assignments`；不做通知、角标、看板。
- 不修改 `app/agent/**`、`app/router/**`、`app/realtime/**`、`app/auth/**` 与任何旧迁移。
```

---

## 段 2｜医生端最小界面 + 家属只读 + 工单页适配（对应 7.2-0 UI）

```text
【任务】Day7 段2：在现有四端页面上补齐"用药计划"的最小可用界面——医生创建/修改/确认页，家属只读视图，护工/管理员工单页的长期工单标识。本段只改前端，不改后端业务逻辑与数据库。

【背景】
项目前端是原生 HTML + JS，无框架。已有：
- `app/static/doctor.html`：医生端，按长者卡片展示，已有健康档案等模块；"用药方案管理"目前只是占位文案；
- `app/static/family.html`：家属端，已有绑定长者的健康档案/工单只读视图；
- `app/static/nurse.html`、`app/static/admin.html`：护工/管理员工单页；
- `app/static/js/workorders.js`：四端共用的工单列表/详情渲染；
- `app/static/js/api.js`：`KB.api(method, path, body)`、`KB.requireRole(...)`、`KB.renderTopbar(...)` 等。
上一段（段 1）已提供用药计划后端接口与长期工单字段。本段只消费这些接口。

【当前状态（先核对）】
1. 打开 `app/static/doctor.html`、`app/static/family.html`、`app/static/js/workorders.js`、`app/static/js/api.js`，确认现有的长者卡片渲染、弹层/模态框写法、登录与角色校验方式。
2. 确认段 1 已实现以下接口（若路径不同，以实际代码为准，不要另造一套）：
   - `POST/GET /api/v1/elders/{elder_id}/medication-plans`
   - `GET/PUT /api/v1/medication-plans/{plan_id}`
   - `POST /api/v1/medication-plans/{plan_id}/confirm|pause|resume|cancel`
   - `GET /api/v1/family/elders/{elder_id}/medication-plans`
3. 确认工单列表项已返回 `order_kind`、`medication_plan_id`、`effective_from`、`effective_until`。

【目标】
1. 医生在现有面板完成"新建用药计划 → 确认页 → 确认下发"；也能修改、暂停、恢复、取消。
2. 家属在现有页面看到只读用药计划，不含内部备注、没有编辑入口。
3. 护工/管理员在现有工单页能看到"长期用药"工单；管理员不能关闭仍在生效的长期工单。
4. 不新增页面、不引入前端框架、不做通知/图表。

【具体要求】
一、医生端（修改 `app/static/doctor.html`）
- 把"用药方案管理（后续版本）"占位替换为可用模块：
  - 每个长者卡片增加"用药计划"按钮；点击后打开该长者的计划列表（可直接复用现有弹层样式）。
  - 列表字段：药名、剂量、服用时间（多个时间用顿号连接）、星期、起止日期、状态、更新时间；状态用中文徽标（待确认/生效中/已暂停/已取消/已完成）。
  - "新建用药计划"表单字段：药名、剂量、服用时间（可增删多个 `time` 输入）、星期（多选，默认全选）、开始日期、结束日期、用药说明、内部备注。
  - 表单前端校验与后端一致：药名/剂量必填，至少一个时间，起止日期顺序正确；错误就地提示，不提交。
- 确认页（产品硬要求，不能用 `window.confirm` 糊弄）：
  - 新建保存成功后，不直接下发，而是打开一个覆盖层，完整展示：长者、药名、剂量、所有服用时间、星期、起止日期、用药说明、内部备注；
  - 覆盖层明确提示"确认后将立即向护工下发长期工单，并开始按时间提醒"；
  - 只有点"确认下发"才调用 `POST /confirm`；点"返回修改"回到表单，不调用 confirm；
  - 确认成功后关闭覆盖层、刷新列表，并显示工单号（后端返回的 `workorder_id`）。
- 修改已生效计划：
  - 点"编辑"载入表单；保存时先展示确认页，显示"原值 → 新值"的差异（至少标出变化的字段），医生确认后才调用 `PUT`；
  - 不要在前端偷偷直接 PUT。
- 暂停/恢复/取消：每个操作都要二次确认（可用轻量弹层），取消时提示"将同时关闭长期工单"。
- 未绑定医生：后端会 403/404；前端在列表为空或 404 时显示"无权限或未绑定该长者"，不要显示创建按钮（角色/绑定以接口结果为准，不靠前端猜测）。

二、家属端（修改 `app/static/family.html`）
- 在长者详情中新增"用药计划（只读）"区块：
  - 字段：药名、剂量、服用时间、星期、起止日期、用药说明、状态；
  - 状态筛选包含已取消/已完成；点开计划可查看"修改记录"时间线（时间、变更字段、变更前/后值）；
  - 不显示内部备注（包括修改记录里的内部备注变更，只显示"内部备注已更新"）、不显示创建人/工单号、不显示任何编辑/暂停/取消按钮；
  - 空态文案："暂无用药计划"；
  - 接口失败时显示明确错误，不伪造数据。
- 只调用 `GET /api/v1/family/elders/{elder_id}/medication-plans` 与 `.../{plan_id}/history`，不要复用医生接口。

三、工单页（修改 `app/static/js/workorders.js`）
- 列表项与详情增加"长期用药"徽标：`order_kind === 'standing'` 时显示；一次性工单不显示。
- 管理员端：当 `order_kind === 'standing'` 时隐藏或禁用"关闭工单"按钮，并提示"长期用药工单随用药计划停用/取消自动关闭"；不要只依赖后端 409。
- 护工端：保留"标记已读/提交处理项"能力；提交处理项后工单仍保持打开（长期），不要在前端把状态改成已关闭。
- 不改变其他工单类型的展示与操作。

四、通用要求
- 复用现有 `KB.api`、`KB.requireRole`、`KB.renderTopbar` 与弹层样式；不引入 React/Vue/构建工具。
- 所有用户可见文案用中文；时间显示统一 `YYYY-MM-DD HH:mm`。
- 不在前端保存长者敏感信息到 localStorage；不写死 elder_id/plan_id。
- 不修改后端 API、数据库、路由、实时模块。

【涉及文件】
- 修改：`app/static/doctor.html`、`app/static/family.html`、`app/static/js/workorders.js`
- 可选修改：`app/static/js/api.js`（仅当现有 `KB.api` 不支持 PUT 时补方法）
- 不改：`app/api/**`、`app/services/**`、`app/models/**`、`alembic/**`、`app/realtime/**`、`app/router/**`

【必须遵守的约束】
- 确认页必须展示全部字段并等待医生显式点击；禁止"保存即下发"。
- 家属视图必须只读，且不得出现内部备注。
- 不新增页面文件、不做通知/推送/图表/统计。
- 不引入前端框架、不改现有登录与角色校验逻辑。
- 不执行 `git commit` / `git push` / 打 tag。
- 演示数据用虚构长者与通用药名，不写真实长者信息。

【验收标准】
1. 医生（绑定）：新建计划 → 出现确认页（全部字段可见）→ 确认下发 → 列表出现"生效中"与工单号；返回修改不会生成工单。
2. 医生修改已生效计划 → 出现"原值→新值"确认页 → 确认后列表更新。
3. 未绑定医生：看不到创建按钮；直接访问接口被后端拒绝，前端显示无权限提示。
4. 家属：能看到只读计划与已取消/已完成历史，能看修改记录，页面无编辑按钮、无内部备注。
5. 护工：工单列表出现"长期用药"标识；提交处理项后工单仍打开。
6. 管理员：能看到该工单；"关闭"按钮对长期工单不可用。
7. 浏览器控制台无未捕获异常；刷新页面后数据仍正确。

【完成后请说明】
- 医生端页面结构、确认页字段与交互；
- 家属只读字段白名单；
- 工单页长期标识与关闭按钮处理；
- 手工验收步骤与结果（四类角色各一条）；
- 未覆盖的边界与已知限制（不做未读角标/通知，不做图表）。

【本段不做什么】
- 不改后端 API、服务层、模型、迁移、路由与实时模块；只消费段 1 已提供的接口。
- 不新增页面文件，不引入 React/Vue/构建工具，不改现有登录与角色校验。
- 不做消息通知、未读角标、看板/图表/统计大盘、家属 H5 日报。
- 不实现用药提醒播放、LangGraph、反向 WebRTC、记忆与媒体（后续段）。
```

---

## 段 3｜用药确认图 + 主控 Agent 工具接入（对应 7.2 前半）

```text
【任务】Day7 段3：实现"用药确认" LangGraph 状态图，并接入主控 Agent 工具（本次不做定时任务、不做反向 WebRTC 外呼）。

【背景】
康伴 3D 数字人项目（Python 3.12 / FastAPI / SQLAlchemy 2.x / PostgreSQL+pgvector / Redis）。Day6 已完成三级路由（安全→规则→Laya 分类器→主控 Agent ReAct）与工具注册表。Day7 要把"用药确认"做成有状态、可恢复、可重试的 LangGraph 子图，对话内与后续定时外呼共用同一张图。先读 AGENTS.md，遵守其中实时/语音模块规则与安全禁区。

【当前状态（先核对代码，不要凭假设）】
1. `app/agent/tools.py` 里 `medication_query` 是占位实现，固定返回"用药计划还没接入…"，标记 degraded=medication_not_configured。
2. `app/models/medication.py` 已有 `medication_plans` 表（elder_id/drug_name/dose/schedule JSONB/instructions/start_date/end_date/status），但没有任何 service、API 或种子数据；`schedule` 的 JSON 形状未定义。
3. `pyproject.toml` **未安装 langgraph**；`apscheduler` 只在 optional-dependencies `infra` 中，lifespan 未接入（本段不要接）。
4. `app/services/workorder.py` 只有 `create_sensitive_work_order`（type 固定为敏感事件、source=sensitive），不能直接复用来建漏服工单。
5. `app/router/service.py` 目前把"我要吃药"类请求映射到 `medication_query`；没有"我吃过药了/我没吃"的确认分支。
6. `app/models/workorder.py` 的 `type` 是自由 VARCHAR，`source` CHECK 只允许 'sensitive'|'manual'。
7. 主控 Agent 是自研 ReAct 循环（`app/agent/companion.py`），不是 LangGraph 图；本段把用药图封装成工具供其调用即可，**不要重写 Agent**。

【目标】
1. 安装 LangGraph 与 SQLite checkpointer，建立可复用的 checkpointer 工厂。
2. 实现"用药确认图"：发起提醒 → 等待确认 → 已确认 / 重试（最多 3 次，循环回到提醒）/ 升级（建漏服工单）。
3. 把图接入主控 Agent：老人问"我该吃什么药"能读到真实计划；老人说"我吃过药了/我没吃"能推进图状态。
4. 状态可按 thread_id 持久化，进程重启后能读回 attempt/status 继续推进。

【具体要求】
一、依赖
- 在项目虚拟环境中新增 `langgraph`、`langgraph-checkpoint-sqlite`、`aiosqlite`（用项目既有包管理方式更新 `pyproject.toml` 与锁文件，版本以实际安装到的稳定版为准）。
- 安装后先跑一个最小验证（构造一个两节点图并写入/读取 state），确认 API 名称与异步 Saver 的用法，再写业务；不要照抄旧版本 import 路径。

二、Checkpointer 工厂
- 新增 `app/graphs/checkpointer.py`：提供异步 context manager 或工厂函数，返回 SQLite Saver。
- 数据库文件路径放在 `logs/checkpoints/medication.sqlite3`（`logs/` 已在 .gitignore），目录不存在时自动创建；路径允许通过 Settings 覆盖，但不要写进 .env。
- 测试必须能传入临时路径或内存库，不得污染开发库与仓库。
- 禁止 `asyncio.new_event_loop`、禁止独立线程运行图/Saver；所有异步调用绑定当前运行循环。

三、图定义（新增 `app/graphs/medication.py`）
- State 至少包含：`elder_id`、`plan_id`、`drug_name`、`dose`、`attempt`(0–3)、`status`(calling/waiting_confirm/confirmed/retry/escalated/done)、`last_reply`、`device_id`、`workorder_id`、`updated_at`。
- 节点：`prepare_reminder`（生成可朗读提醒话术，attempt+1，status=calling）、`await_confirm`（status=waiting_confirm，结束本轮）、`evaluate_reply`（按"吃了/没吃/等会儿"分流）、`create_missed_workorder`（建单，status=escalated）。
- 边：START → 根据输入类型路由（start / reply）→ 对应节点；`evaluate_reply` 用条件边：确认→confirmed→END；未确认且 attempt<3→回到 `prepare_reminder`（**必须是真的循环边**）；attempt>=3→`create_missed_workorder`→END。
- **不要使用 LangGraph 的 interrupt / Command(resume=…)**：HITL 暂停恢复是 Day8 的内容。本段用"按轮推进"：每次调用图时带 `{"action": "start"}` 或 `{"action":"reply","reply":"..."}`。
- `thread_id` 规则：`medication:{elder_id}:{plan_id}:{YYYY-MM-DD}`（Asia/Shanghai 日期），保证同一天同一计划只有一条确认流程，跨天自然重开。
- 图的输入输出用 TypedDict/Pydantic 明确声明；对外只暴露两个函数：`start_confirmation(...)` 与 `submit_reply(...)`，都返回可朗读文本与当前状态摘要。

四、用药服务（扩展 `app/services/medication_plan.py`；段 1 已建，不要另建重复服务）
- 定义 `schedule` JSON 约定并写进 docstring：`{"times": ["08:00"], "weekdays": [0,1,2,3,4,5,6]}`（0=周一，缺省=每天）。
- 提供：`list_active_plans(db, elder_id)`（status=active 且日期在有效期内）、`today_plans(db, elder_id)`、`format_plan_reply(plans)`（口语化、短句、不编造）。
- 无计划时必须明确说"还没有登记用药计划"，绝不编造药名剂量；查询必须强制 `WHERE elder_id = :elder_id`。

五、工具接入（修改 `app/agent/tools.py`）
- `ToolContext` 当前只有 `elder_id/device_id/room_type`，没有数据库会话；工具内用 `app.db.session.get_session_factory()` 开一个短生命周期 `async with` 会话，用完即关，异常不得抛出到语音链路。
- `medication_query`：改为调用上面的服务，返回今日用药清单；无计划走明确降级话术。
- 新增 `medication_confirm`：入参允许 `{"reply": "..."}`（可空），调用 `submit_reply` 推进图；返回结果要包含"已记录/再提醒一次/已转护工"等可朗读话术。
- 两个工具都只从 `ToolContext.elder_id` 取身份，忽略模型传入的任何 elder_id 参数；`elder_id` 为空时明确说"先确认一下您是哪位"，不查库。
- 控制工具总数：不要顺手新增无关工具。

六、路由可达性（最小改动）
- 在 `app/router/signals.py` 增加"服药确认/否认"词表与匹配函数（如：吃过了、吃完了、刚吃了、已经吃了、还没吃、没吃、等会儿吃、一会儿吃）。
- 在 `app/router/service.py` 的"拒绝服药"分支（`_NEGATED_MEDICATION_RE`）**之后**、追问/多意图/规则层**之前**增加前置分支：命中确认/否认词 → `RouteDecision(layer=RULE, intent=MEDICATION_QUERY, rule_id="MED-01", tool="medication_confirm", tool_args={"reply": text})`。
- 该分支必须在规则层之前判定，避免"我吃过药了"被分类器当成查询计划；同时"我不吃药"仍走原拒绝分支（升级主控 Agent），不得被新词表吞掉。
- 不得改动安全一票否决、10 条规则、多意图升级的既有顺序；改动后必须回归 `eval/fixtures/day6_golden_dev.jsonl`（用现有 `scripts/run_day6_golden.py`），通过数不得低于改动前。

七、漏服工单（修改 `app/services/workorder.py`）
- 新增 `create_missed_medication_work_order(session, *, elder_id, plan_id, drug_name, dose, attempts, occurred_on, device_id=None)`：
  - `type="missed_medication"`、`source=WorkorderSource.MANUAL`、`priority=HIGH`、`status=OPEN`、`order_kind="one_off"`、`medication_plan_id=plan_id`；
  - `title` 形如"用药漏服：{drug_name}"；`description` 只写计划/剂量/尝试次数/日期，不写对话原文；
  - 同时创建 doctor/nurse 两条 `WorkorderDisposition`（UNREAD）与一条 `created` 事件，复用现有 `_add_event`；
  - 同一 elder + 同一 plan + 同一日已存在未关闭的漏服工单时不重复建单，累加 `occurrence_count` 并写 `occurrence` 事件。
- 不要修改 `create_sensitive_work_order` 的行为。

八、种子数据（新增 `scripts/seed_medication_plan.py`）
- 命令行脚本：指定 elder 姓名或 id，写入一条教学用计划（例如"降压药 1 片 每日 08:00"），`status` 必须置 `ACTIVE`（表默认是 `PLANNED`），并写 `start_date`。
- 该脚本**仅用于段 3/4 的测试与演示**：不生成长期工单、不替代段 1 的"医生创建→确认"流程；演示医生端时必须走段 1/2。若表中 `created_by_account_id` 可空，可留空并在输出里注明"测试种子"。
- 使用明确虚构的测试长者，不写真实姓名/房间号/病史；脚本重复执行要幂等或明确提示已存在。

九、降级（未配置/依赖不可用时的行为，必须可跑）
- 未安装 langgraph、Saver 打开失败、图执行异常：应用照常启动，不得抛到语音链路；`medication_query` 仍可用（直接查表），`medication_confirm` 退化为**单轮确认**（直接把"已服药/未服药"写入当日记录，不重试、不升级），返回可朗读话术。
- 结构化日志记 `event=medication_graph_degraded`、`degraded=medication_graph_unavailable` 与 `error_type`（不记长者原文）。
- 降级时不得假装"已确认/已记录"；漏服兜底由段 4 的调度器负责，不得因降级而静默丢单。

十、测试（新增 `tests/test_medication_graph.py`，补充 `tests/test_agent_tools.py`）
- 至少覆盖：3 次"没吃"→escalated 且工单只建一次；"吃了"→confirmed 且不建工单；重启恢复（同一临时 sqlite 路径，新建图实例后读回 attempt/status）；跨 elder 隔离；无计划不编造；确认分支路由不误伤"我不吃药"。
- 单测不得依赖开发库；需要 DB 的用例用 `db_session` fixture（kangban_test），未配置时跳过。

【涉及文件】
- 新增：`app/graphs/__init__.py`、`app/graphs/checkpointer.py`、`app/graphs/medication.py`、`scripts/seed_medication_plan.py`、`tests/test_medication_graph.py`
- 修改：`app/services/medication_plan.py`（扩展今日用药查询与确认状态，不重写段 1 的权限/确认/工单逻辑）
- 修改：`pyproject.toml`（依赖）、`app/agent/tools.py`、`app/router/signals.py`、`app/router/service.py`、`app/services/workorder.py`、`tests/test_agent_tools.py`
- 不改：`app/core/lifespan.py`、`app/realtime/**`、`app/static/**`、`alembic/**`

【必须遵守的约束】
- 不新增数据库表、不新增 Alembic 迁移、不改已执行迁移。
- 不实现 APScheduler、定时任务、反向 WebRTC、前端改动（下一段做）。
- 不使用 `interrupt` / `Command(resume=...)`（Day8 内容）。
- 不实现健康采集、健康决策、看板、HITL 落库（Day8 内容）。
- `elder_id` 只来自 `ToolContext` / Runtime Context，不接受模型、URL、前端传参。
- 不记录长者对话原文、音频、PCM 到日志；只记元数据（elder_id、plan_id、attempt、status、耗时）。
- 不下载模型权重、不调用未授权外部服务；未配置/不可用必须可见降级，禁止静默失败。
- 保持 `ruff` 行宽 100；中文 docstring；不执行 git commit/push。

【验收标准】
1. `pytest tests/test_medication_graph.py tests/test_agent_tools.py -q` 全过。
2. `ruff check .` 无新增告警。
3. 手工：种子脚本建计划 → 文本/语音说"我该吃什么药"返回真实药名剂量；说"我吃过药了"返回已记录；连续 3 次"没吃"后数据库出现 type=missed_medication 的工单。
4. 手工：重启后端后，用同一 thread_id 能读取到未完成的状态（提供一段可复现的验证脚本或测试）。
5. 回归：`scripts/run_day6_golden.py` 通过数不少于改动前，并提供改动前后数字。

【完成后请说明】
- 改动文件清单与每处改动目的；
- 图结构（节点/边/条件）、thread_id 规则、State 字段；
- Saver 文件位置与测试隔离方式；
- 新增工具签名与返回话术；
- 漏服工单字段与去重逻辑；
- 验收命令与真实输出；
- 未完成项、已知风险与降级表现（未安装 LangGraph 时是否可跑）。

【本段不做什么】
- 不改主控 Agent 的整体 ReAct 结构，只新增/实现两个工具与一个路由前置分支。
- 不改现有路由的安全一票否决、10 条规则、多意图升级顺序；不重写分类器。
- 不实现 APScheduler、定时任务、反向 WebRTC、前端改动（段 4）。
- 不新增数据表、不加迁移、不改已存在迁移；不使用 `interrupt` / `Command(resume=...)`。
- 不做记忆、媒体、评测（段 5–7）；不做健康采集/决策/看板。
```

---

## 段 4｜定时外呼 + 反向 WebRTC（对应 7.2 后半）

```text
【任务】Day7 段4：把"用药确认图"接到 APScheduler 定时任务，并实现服务端主动发起的反向 WebRTC 外呼（含超时重试与可见降级）。

【背景】
项目已有：三级路由 + 主控 Agent；`app/realtime/session.py` 实现了浏览器 offer → 服务器 answer 的单向收音（音频轨 recvonly）；`app/services/voice.py` 通过同源 WebSocket 下发 TTS 音频事件；上一段已实现用药确认 LangGraph 图、`medication_confirm` 工具与漏服建单函数。本段只做"到点主动叫长者吃药"，不做夜间健康外呼。

【当前状态（先核对）】
1. `app/core/lifespan.py` 只启动 MCP 与释放引擎，没有调度器。
2. `app/realtime/routes.py` 只有 `POST /rtc/offer`；服务器不会主动发 offer，也不发送下行音轨。
3. `app/realtime/session.py` 的 `accept_offer` 把音频 transceiver 显式降为 recvonly。
4. `app/services/voice.py` 的 `VoiceSessionManager` 只有 register/unregister/feed_pcm，没有按 device_id 取会话或发事件的方法。
5. `app/static/js/voice.js` 只作为 offer 发起方，没有处理服务端 offer / 回 answer 的分支。
6. 用药图接口（上一段）：`start_confirmation(...)`、`submit_reply(...)`，thread_id 为 `medication:{elder_id}:{plan_id}:{日期}`。

【目标】
1. 每天按 `medication_plans.schedule` 到点扫描，解析长者对应平板设备，主动发起提醒。
2. 外呼优先走反向 WebRTC（服务端 offer + 下行 TTS 音轨），失败时降级为现有 WS TTS 通道并明确标记。
3. 每次提醒等待 30 秒（可配置）；无回应记一次失败并回到图的重试循环；3 次后建漏服工单。
4. 后端重启后继续当天未完成的确认流程，不从 attempt=1 重来。
5. 严格绑定 uvicorn 事件循环，不新建 loop、不起独立线程跑 aiortc。

【具体要求】
一、配置（`app/core/config.py`）
- 新增（全部有安全默认值）：`medication_reminder_enabled: bool = True`、`medication_scan_interval_seconds: int = 30`、`medication_reminder_window_minutes: int = 5`（到点前后多少分钟内允许触发）、`medication_call_timeout_seconds: int = 30`、`medication_retry_limit: int = 3`。
- `.env.example` 只加占位/默认值说明，不写真实值。

二、调度器（`app/core/lifespan.py` + 新增 `app/services/medication_scheduler.py`）
- 使用 `AsyncIOScheduler(timezone=settings.app_timezone)`，在 lifespan 启动阶段创建并 `start()`，关闭阶段 `shutdown(wait=False)`。
- 注册 interval job：`scan_due_medications`，`max_instances=1`、`coalesce=True`、`misfire_grace_time` 合理设置；job 内所有 await 都跑在 uvicorn loop 上。
- `medication_reminder_enabled=False` 或测试环境不启动调度器；提供纯函数 `due_items(now, plans)` 便于单测（不连库）。
- 扫描逻辑：
  - 查 status=active、日期有效、`schedule.weekdays` 包含今天的计划；
  - 对 `schedule.times` 中与当前时间差在窗口内的时段触发；
  - 幂等：用当日 thread_id 读取图状态，若已 waiting_confirm/confirmed/escalated 则跳过；进程内再加一层 `(plan_id, 日期, 时段)` 去重缓存；
  - 触发失败只记日志并继续下一个，不让一个计划拖垮整轮扫描。

三、设备解析（新增 `app/services/outbound.py`）
- 由 elder 找设备：`elders.room_id` + `elders.bed_no` → `devices`（status=active）；优先 `bed_no` 完全匹配，其次 `bed_no IS NULL` 的房间级设备；多个候选时取更新时间最新的一条并记警告。
- 找不到设备：不发起外呼，记 `event=medication_call_no_device`，并调用上一段的漏服建单函数（原因写"设备未找到"），不要静默跳过。
- 设备存在但 WS 未连接：走降级通道（见五）。

四、反向 WebRTC（修改 `app/realtime/session.py`、`app/api/routes/voice.py`、`app/static/js/voice.js`）
- 服务端在调度 job 内创建**独立于日常上行**的 `RTCPeerConnection`（与 uvicorn 同一 loop），用 `call_id` 关联；**不要复用/重协商日常收音 PC**；同一设备同一时刻只允许一个外呼。**先 addTrack 再 createOffer**，把 offer 通过该设备的 WS 下发：
  `{"type":"rtc_call_offer","call_id":"...","sdp":"...","type":"offer"}`
- 前端 `voice.js` 收到后：`setRemoteDescription(offer)` → `createAnswer()` → `setLocalDescription(answer)` → 通过 WS 回
  `{"type":"rtc_call_answer","call_id":"...","sdp":"...","type":"answer"}`
- 后端 WS 消息循环收到 `rtc_call_answer` 后，按 call_id 找到挂起的 PC，`setRemoteDescription(answer)`；未知 call_id 记警告并忽略。
- 下行音轨：实现一个 `MediaStreamTrack` 子类，`recv()` 返回 16k mono s16 的 `av.AudioFrame`（pts/time_base 正确、每帧 20ms），文本来自 TTS provider（复用 `app/services/voice.py` 的 provider 获取函数），音频只走内存，不落盘、不进日志。
- 清理：外呼结束/超时/失败/PC 连接态 failed/closed 时关闭 track 与 PC，移除 call_id 映射，避免幽灵连接。
- 新增 `VoiceSessionManager.get(device_id)` 或等价的"按设备发事件"方法，供 outbound 模块使用；不要绕过管理器直接持有 WS 对象。

五、降级（必须有、必须可见）
- 以下任一情况走降级：WS 未连接、前端不支持/不回 answer、8 秒内未建立连接、aiortc 抛错。
- 降级方式：用现有 WS TTS 事件把提醒话术播出来（与日常回复同一条通道），并记 `event=medication_call_degraded`、`degraded=rtc_unavailable`。
- 降级后仍按"一次提醒"计数：等 30 秒无确认 → 图进入重试。
- 严禁静默失败：长者没听到、护工也看不到，是不允许的状态。

六、超时与重试
- 每次外呼后等待 `medication_call_timeout_seconds`；收到长者回复（语音/文字）立即 `submit_reply` 推进图。
- 超时由调度器下一轮扫描判定（不要用长时间 sleep 占住 job）：比较图状态 `updated_at` 与当前时间，超时则调用 `submit_reply(reply="timeout")`，走图内的 retry 分支。
- 达到 `medication_retry_limit` 后由图建漏服工单；调度器不重复建单。

七、失败四分类（外呼编排内必须体现）
- 参数错（计划缺时段/格式非法）：不重试，记 `invalid_plan`，跳过并告警；
- 目标不存在（无设备/设备停用/计划已停用）：不重试，按"设备未找到"处理（见三）；
- 临时错（WS 断连、aiortc 建连失败、TTS 临时失败）：退避后重试，最多 3 次；
- 越权（device 不属于该 elder、伪造 plan_id）：直接拒绝并写审计日志，不建单、不外呼。

八、演示脚本（新增 `scripts/trigger_medication_call.py`）
- 参数：`--plan-id` 或 `--elder-id`；手动触发一次外呼（复用 outbound 编排，不绕开图）。
- 输出：设备解析结果、外呼方式（rtc/ws）、耗时、当前图状态；不打印长者原文。

九、测试（新增 `tests/test_outbound_scheduler.py`、`tests/test_outbound_call.py`）
- 纯函数测试：`due_items` 的时段/星期/窗口/幂等；设备解析优先级；找不到设备时的行为。
- 外呼编排测试：用 fake session/manager 与 fake track，断言 addTrack 在 createOffer 之前、超时走 retry、降级标记正确；不要在单测里启动真实 aiortc/网络。
- 调度器测试：测试环境不启动真实 scheduler；job 函数可单独 await 调用。

【涉及文件】
- 新增：`app/services/medication_scheduler.py`、`app/services/outbound.py`、`scripts/trigger_medication_call.py`、`tests/test_outbound_scheduler.py`、`tests/test_outbound_call.py`
- 修改：`app/core/config.py`、`app/core/lifespan.py`、`app/realtime/session.py`、`app/api/routes/voice.py`、`app/services/voice.py`（仅加按设备取会话/发事件的方法）、`app/static/js/voice.js`、`.env.example`
- 不改：`app/router/**`、`app/agent/**`、`alembic/**`

【必须遵守的约束】
- aiortc 的所有任务必须绑定 uvicorn 运行 loop：禁止 `new_event_loop`、禁止在独立线程跑 PC；PC 在此处的创建位置是"调度 job（同 loop）"，不要挪到线程池。
- `addTrack` 必须在 `createOffer` 之前；不要依赖 `on("track")` 的时序做关键动作。
- 原始音频（PCM/Opus/采样）不落盘、不入日志；只记时长、延迟、结果元数据。
- 不实现夜间健康外呼、健康分级、医院对接、HITL 看板（Day8）。
- 不新增数据库表/迁移；不改已执行迁移；`elder_id` 只来自服务端上下文。
- 前端不引入框架；保持现有 `voice.js` 风格；不破坏已有 TTS/口型/音量逻辑。
- 不执行 git commit/push。

【验收标准】
1. `pytest tests/test_outbound_scheduler.py tests/test_outbound_call.py -q` 全过；`ruff check .` 通过。
2. 手工：启动后端与平板页 → `scripts/trigger_medication_call.py --plan-id <uuid>` → 平板在 8 秒内出声说出提醒；日志出现 `medication_call_started` 与结果字段（rtc 或 degraded）。
3. 手工：提醒后不回应 → 30 秒后 attempt 增加；连续 3 次后 `workorders` 出现漏服工单，且只出现一条。
4. 手工：关闭平板 WS 再触发 → 日志明确 `degraded=rtc_unavailable`，且没有未捕获异常。
5. 手工：外呼等待中重启后端 → 再次扫描时从当前 attempt 继续，而不是重新 attempt=1。
6. 代码检查：全局搜索确认没有 `new_event_loop` / `threading.Thread` 跑 aiortc。

【完成后请说明】
- 调度器启动/关闭位置与 job 参数；
- 设备解析规则与无设备/离线的行为；
- 信令消息格式、前端改动点、降级触发条件；
- 超时判定方式（为何不占住 job）；
- 手工验收的真实输出；
- 已知限制（无 TURN、自动播放、单实例约束）与降级表现。

【本段不做什么】
- 不改主控 Agent、不改三级路由判定逻辑、不改用药计划 API/权限（只读计划）。
- 不重协商日常收音 PC、不改造现有 ASR/TTS 主链路；只新增一条独立外呼 PC 与降级分支。
- 不新增数据表/迁移、不改已存在迁移；不实现夜间健康外呼、分级阈值、医院对接、HITL 看板。
- 不做前端框架改造，只给 `voice.js` 加服务端 offer/answer 与降级播放处理。
- 不做记忆、媒体、评测（段 5–7）。
```

---

## 段 5｜长期记忆五步链路（对应 7.1）

```text
【任务】Day7 段5：实现长期记忆的"萃取 → 存储 → 召回过滤 → 冲突检测 → 遗忘归并"，并注入对话；不建看板、不改数据库结构。

【背景】
项目已具备多轮对话、早期摘要（`app/services/context_summary.py`）、Redis 会话状态（`app/services/session_state.py`）与主控 Agent。`memory_chunks` 表已存在（`elder_id/content/embedding vector/embedding_model/category/visibility/status/expires_at`）。本段把"记忆"做成独立服务，供语音/文字对话与主控 Agent 共用。

【当前状态（先核对）】
1. `app/models/memory.py` 只有 ORM；没有任何读写代码。
2. `app/core/config.py` 有 `embedding_provider/base_url/api_key/model` 占位字段，但 `app/integrations/` 没有 embedding 客户端。
3. `app/services/voice.py` 的 `_reply_via_llm` 只注入"人设 + 摘要/槽位"；`_load_context` 不查记忆。
4. 没有会话结束逻辑：`conversations.status=ended` 从未被写入，WS 断开也不会置 ended。
5. `app/services/session_state.py` 的 `SessionState` 已有 summary/slots；可扩展字段，但不能破坏旧数据反序列化。

【目标】
1. 对话结束后（或达到轮数阈值）用 LLM 萃取最多 3 条结构化记忆，写入 `memory_chunks`，embedding 必填。
2. 召回 5 条候选后做二次推理过滤，只把真正相关的记忆拼进 system prompt。
3. 写入时检测冲突（相似度 >0.9 且语义矛盾）→ 旧记忆 `superseded`。
4. 90 天衰减 + 周期性归并（合并重复、冷热分层）。
5. 记忆作为主控 Agent 的直接工具可查询；`elder_id` 隔离必须可测。

【具体要求】
一、Embedding Provider（新增 `app/integrations/embeddings.py`）
- 定义 `EmbeddingProvider` 协议：`provider`、`model`、`dimension`、`available`、`async embed(texts) -> list[list[float]]`。
- 实现 OpenAI 兼容 HTTP Provider：读取 `EMBEDDING_BASE_URL / EMBEDDING_API_KEY / EMBEDDING_MODEL`，POST `{base_url}/embeddings`，带超时与错误包装；未配置或失败时 `available=False`。
- 实现本地降级：`HashNgramEmbedding`——对中文按字符 1–2 gram 做特征哈希到固定 256 维并归一化；`model="hash-ngram-256"`。这是真实可计算的向量，不是伪造数据；不要下载任何模型权重。
- 统一入口 `get_embedding_provider()`：配置可用则用远程（默认 OpenAI 兼容 `text-embedding-3-small`、1536 维），否则用 `HashNgramEmbedding`（256 维），并在首次使用时记一次结构化日志 `event=embedding_fallback`、`degraded=embedding_unavailable`、实际 `model` 与 `dimension`。
- 未配置 `EMBEDDING_*` 必须能跑通全链路（应用启动、萃取、召回都正常），只降精度不报错；报告与日志必须标注实际使用的模型。

二、记忆服务（新增 `app/services/memory.py`）
- 数据访问统一带 `WHERE MemoryChunk.elder_id == elder_id`；任何方法都不允许省略该条件。
- 写入 `remember(db, elder_id, items)`：
  - 每条先算 embedding；写入 `content/category/visibility/status=active/embedding/embedding_model/expires_at=now+90d`；
  - `memory_chunks` **没有** source_thread_id / importance / access_count 列：不要试图持久化这些字段，来源线程只记在结构化日志里；"重要性"只在归并排序时按 category 权重 + 最近 30 天计算，不落库；
  - `visibility` 规则：`health`/慢病/用药相关 → `care_team`；`preference`/`relationship`/`routine` → `private`；
  - 去重：同 elder、同 category、status=active、与候选余弦相似度 >0.95 → 跳过并返回 `duplicate`；
  - 冲突：相似度 >0.9 时让 LLM 判断是否矛盾（严格 JSON：`{"contradict": true/false}`）；矛盾则把旧记录置 `superseded`、写新记录；LLM 不可用时按"不矛盾"处理并记降级日志。
- 召回 `recall(db, elder_id, query, top_k=5)`：
  - 只取 status=active、elder_id 匹配、`embedding_model == 当前 provider.model`（不同维度不能混算）；
  - 按余弦距离排序取 5；
  - 二次推理：把候选 id + 内容 + 当前问题给 LLM，返回 `{"keep":[id...]}`；只保留通过项（最多 3 条）；LLM 失败时退化为 Top2 并记 `event=memory_rerank_degraded`；
  - 通过项批量把 `updated_at` 刷成当前时间，作为"最近访问时间"的代理（表里没有 accessed_at；最多 3 条，失败不影响返回）。
- 遗忘归并 `maintenance(db, elder_id=None, dry_run=True)`：
  - 过期（`expires_at < now`）→ `status=suppressed`（不物理删除，便于审计）；
  - 同 elder + category 内相似度 >0.92 的重复 → 保留最新、旧记录 `superseded`；
  - 冷热分层排序 = `updated_at` 近因（最近 30 天加权）+ category 重要性权重；90 天未被访问且低权重的记忆优先抑制，不物理删除；
  - 返回统计：扫描数、抑制数、合并数。
- 记忆上下文拼装 `build_memory_context(memories)` → `【关于长者的记忆】- …`，最多 3 条，每条不超过 40 字；无记忆返回 None。

三、萃取（新增 `extract_memories`，可在 `memory.py`）
- 输入一段对话（user/assistant 消息列表），输出严格 JSON：
  `{"memories":[{"content":"...","category":"preference|health|event|relationship|routine|other"}]}`
- 最多 3 条（目标 3 条，不足不硬凑）；只记事实，不记情绪评价；不把助手的话当成长者事实。
- LLM 不可用/解析失败：不写库，记 `event=memory_extract_degraded`，不影响主流程。

四、触发时机（修改 `app/services/voice.py`、`app/services/session_state.py`、`app/services/conversation_store.py`）
- `SessionState` 新增可选字段 `memory_upto_seq: int = 0`（默认值保证旧数据可读），用于记录已萃取的 `conversation_messages.seq`。
- 在 `conversation_store.py` 新增 `messages_since(db, thread_id, seq)`（`seq > :seq` 正序），不要改 `recent_messages` 的既有语义。
- 触发 1：WS 会话 `close()` 时（若 `elder_id` 已确认且新增消息 ≥2 条）后台任务萃取一次。`close()` 是同步方法：在 `VoiceSession.__init__` 缓存当前 running loop（取不到就不调度），用 `loop.create_task`，禁止新建 loop。
- 触发 2：每轮保存对话后，若 `最新 seq - memory_upto_seq >= 4`，后台触发一次；同一 thread 同时只跑一个萃取任务，避免并发重复。
- 萃取成功推进 `memory_upto_seq`；失败不推进，下一轮重试。
- 所有萃取在后台任务中执行，不延迟本轮回复；异常只记日志。

五、注入对话（修改 `app/services/voice.py`）
- 仅当 `self.verified_elder_id` 不为空（身份已确认）才召回；匿名/未确认身份时不查记忆、不注入。
- 在 `_prepare_state`（它是 `VoiceSession` 方法，已有 `self.verified_elder_id` 与 `self._db_factory`）里：先取原有摘要/槽位上下文，再追加 `build_memory_context(...)` 的结果；不要把 DB/记忆逻辑塞进模块级 `_prepare_turn_state`（它只负责纯状态）。
- 注入内容最多 3 条、每条不超过 40 字；召回失败不得中断回复，记 `event=memory_recall_degraded`。

六、主控 Agent 工具（修改 `app/agent/tools.py`）
- 新增 `memory_recall`：入参 `{"query": "..."}`，从 `ToolContext.elder_id` 取身份；数据库会话用 `app.db.session.get_session_factory()` 短会话；返回 1–2 句可朗读的相关记忆；无记忆时明确说"我还没记住这件事"。
- 不要新增 `memory_write` 工具：记忆写入只由后台萃取触发，不允许模型直接写。

七、维护脚本与每周任务（新增 `scripts/memory_maintenance.py`）
- 参数：`--elder-id`（可空=全部）、`--dry-run`（默认 true）、`--apply`。
- 输出 JSON 统计；`--dry-run` 不写库。
- 在 `memory.py` 暴露 `weekly_maintenance()` 供调度器调用；若仓库已存在 `app/services/medication_scheduler.py`（上一段产出），在其中注册每周日 03:00 的 job（`max_instances=1`）；若不存在，**不要新建调度器**，只保留脚本入口。

八、隐私与日志
- 日志只记：elder_id、条数、耗时、category 分布、是否降级；**不记记忆正文、不记对话原文**。
- 记忆正文只在数据库与 prompt 中出现；prompt 不落日志。

九、测试（新增 `tests/test_memory_service.py`、`tests/test_memory_isolation.py`）
- 用 fake embedding（确定性向量）+ fake LLM 覆盖：萃取解析、去重、冲突置 superseded、召回二次过滤、LLM 降级、遗忘归并。
- 隔离测试必须验证"李爷爷的 elder_id 查不到王奶奶的记忆"；至少一条用真实 SQL（`db_session`，kangban_test），一条断言仓储调用参数含 elder_id。
- 匿名会话（elder_id=None）不得触发召回；`memory_recall` 无 elder_id 时返回明确话术。

【涉及文件】
- 新增：`app/integrations/embeddings.py`、`app/services/memory.py`、`scripts/memory_maintenance.py`、`tests/test_memory_service.py`、`tests/test_memory_isolation.py`
- 修改：`app/core/config.py`（仅补齐已有字段的默认值/超时）、`app/services/voice.py`、`app/services/session_state.py`、`app/services/conversation_store.py`、`app/agent/tools.py`
- 不改：`alembic/**`、`app/router/**`、`app/realtime/**`、`app/static/**`

【必须遵守的约束】
- 不新增表、不新增迁移；`memory_chunks.embedding` 维度不固定，靠 `embedding_model` 过滤，不要混算不同维度。
- 不下载模型权重、不调用未授权服务；远程 embedding 未配置时必须降级且可见。
- 所有记忆查询必须强制 `elder_id`；禁止"先全量查再过滤"。
- 不把记忆正文/对话原文写日志；不落原始音频。
- 不实现医生/护工记忆视图（Day8）；不实现记忆删除 API（除维护脚本的抑制/合并）。
- 不执行 git commit/push。

【验收标准】
1. `pytest tests/test_memory_service.py tests/test_memory_isolation.py -q` 全过；`ruff check .` 通过。
2. 场景 A：测试长者 A 说"我有糖尿病" → 结束/达到阈值后 `memory_chunks` 出现 health 记忆 → 新会话问"我能吃粽子吗" → 回复主动提到糖尿病（可在日志/回复中验证）。
3. 场景 B：用测试长者 B 的身份召回 → 结果为空数组；SQL 日志可见 `WHERE elder_id = ...`。
4. 场景 C：写入"我不吃甜食"后写入"我最近特别想吃月饼" → 旧记录 `status=superseded`，新记录 active，且不出现两条互相矛盾的 active 记录。
5. 场景 D：召回 5 条中含 2 条无关噪声 → 二次过滤后注入不超过 3 条，噪声不出现。
6. `scripts/memory_maintenance.py --dry-run` 输出统计且不写库；`--apply` 后重复执行结果稳定（幂等）。
7. 日志抽查：无记忆正文与对话原文。

【完成后请说明】
- Embedding 采用远程还是降级、维度、模型名；
- 萃取触发条件与游标推进方式；
- 召回/过滤/冲突/归并的具体规则与阈值；
- 隔离测试如何证明（SQL 或仓储参数）；
- 验收命令与真实输出；
- 已知限制与风险（远程 embedding 未配置时的降级、成本、不同维度不混算）。

【本段不做什么】
- 不改主控 Agent 结构，只新增 `memory_recall` 工具；不新增 `memory_write` 工具。
- 不新增表/迁移，不改 `memory_chunks` 结构；不下载模型权重。
- 不重写 `voice.py` 主链路，只在 `_prepare_state` 追加记忆上下文与后台萃取触发。
- 不新建调度器（仅当段 4 的调度器存在时挂一个周任务）；不做医生/护工记忆视图与删除 API。
- 不做用药、媒体、评测（其他段）。
```

---

## 段 6｜娱乐点播（对应 7.3）

```text
【任务】Day7 段6：把"戏曲/老电影点播"从占位事件变成真实可播放链路，并落 `media_history`；只做长者端点播，不做后台管理页。

【背景】
规则层 R05 已把"我想听越剧"映射到 `media_play` 工具，R04 映射"停"；前端 `app/static/js/voice.js#handleControl` 目前只写日志"（媒体播放 Day7 接入）"，没有真实播放。`media_library` 表已存在（type/genre/artist/media_uri/duration_seconds/copyright_status/status）。`media_history` 尚未建表（ER 文档中为候选表）。

【当前状态（先核对）】
1. `app/agent/tools.py` 的 `media_play/media_next/media_stop` 只返回固定话术 + control 事件，不查库、不选片、不记历史。
2. `app/models/media.py` 有 active 状态的 CHECK：仅 licensed/teaching/public_domain 可 active。
3. `alembic/versions/` 最新为 `0007_workorder_flow.py`；不得回改旧迁移。
4. `app/static/js/voice.js` 用 WebAudio 播 TTS PCM；尚未使用 `<audio>` 播媒体。
5. 上一段已提供记忆偏好接口（如 `recall_preferences(elder_id)` 或 `recall(..., category="preference")`）；若接口名不同，以实际代码为准，允许在 media 服务内做一次轻量适配。
6. `app/router/executor.py#execute_rule` 对 `media_play`/`media_next` 直接返回本地通用话术，导致规则命中后**不会执行 `decision.tool`**；这是本段必须先解决的前置问题（见要求五）。

【目标】
1. 新增 `media_history`（迁移 0009 + ORM），记录点播开始/结束与完成度。
2. 媒体服务按"授权 + active + genre/偏好"选片，返回可播放地址。
3. 规则层点播一次出声；"换一段/停"零模型；播放记录落库。
4. 缺素材/未授权时明确降级，不播放未授权内容。

【具体要求】
一、迁移与模型
- 新增 `alembic/versions/0009_media_history.py`，只建 `media_history` 一张表：
  - `history_id` UUID PK；`elder_id` FK elders 非空；`media_id` FK media_library 非空；
  - `started_at` timestamptz 非空 default now；`finished_at` timestamptz 可空；
  - `completion_rate` numeric(4,3) 可空，CHECK 0..1；
  - 索引：`(elder_id, started_at desc)`、`(media_id)`。
  - downgrade 只 drop 该表与其索引。
- 新增 `app/models/media_history.py` 并注册到 `app/models/__init__.py`；字段与迁移一致。
- 迁移文件必须人工可读，不夹带其他表改动。

二、媒体服务（新增 `app/services/media.py`）
- `pick_for_play(db, elder_id, genre=None)`：
  - 过滤 `status=active` 且 `copyright_status in ('licensed','teaching','public_domain')`；
  - genre 为空时优先取记忆中的偏好 genre（无记忆则默认"越剧"，再无则取库中任意可播项）；
  - 同一 elder 近期播放过的尽量靠后（按 `media_history` 最近记录排序）；
  - 返回 `{media_id,title,genre,artist,url,duration_seconds}`；`url` 必须是应用内相对路径（如 `/static/media/library/xxx.mp3`），不允许外链。
- `next_for(db, elder_id, current)`：按 genre 取同类的下一条（循环），无下一条时回退到其他可播项。
- `record_start(db, elder_id, media)`：插入 `media_history`（started_at=now），返回 history_id。
- `record_end(db, history_id, completed: bool, elapsed_seconds: float | None)`：补 `finished_at` 与 `completion_rate`（completed=True 记 1.000；否则按 elapsed/duration 估算并夹在 0..1；无 duration 时留空）。
- 所有查询必须带 elder 归属校验（history 更新要校验 elder_id 匹配，防止伪造 history_id）。

三、会话状态（修改 `app/services/session_state.py`）
- 在 `app/services/session_state.py` 的 `MediaSlot` 增加可选字段：`media_id: str | None`、`title: str | None`、`history_id: str | None`、`genre: str | None`；默认 None，保证旧 Redis 数据可反序列化。
- 记录"当前在播"的信息，供"换一段/停"使用；会话结束时不需要清理（TTL 自然过期）。

四、工具（修改 `app/agent/tools.py`）
- 工具内需要数据库时用 `app.db.session.get_session_factory()` 开短会话，禁止在工具模块级持有长连接。
- `media_play`：调用 `pick_for_play` → `record_start` → 返回：
  - 可朗读话术（如"好，给您放一段越剧《梁山伯与祝英台》。"）；
  - `client_events`：`{"type":"control","action":"media_play","media_id":...,"title":...,"genre":...,"url":...,"duration_seconds":...,"history_id":...}`。
- `media_next`：取当前会话的 genre/current，`next_for` → `record_start`（旧记录按"未完成"补结束）→ 下发同样的 control 事件。
- `media_stop`：停止事件 + 若有当前 history 则 `record_end(completed=False)`；清空会话媒体字段。
- 无可用媒体：`ok=False`，返回"这段暂时没有音源，我帮您记着"之类降级话术，`degraded=media_unavailable`；不伪造歌曲名。
- 工具仍只从 `ToolContext.elder_id` 取身份；没有 elder_id（匿名）时不允许点播，返回明确话术。

五、路由最小让位（修改 `app/router/rules.py`、`app/router/executor.py`）
- 现状是"规则命中 media_play/media_next 后，`execute_rule` 直接返回一句通用话术 + 只带 genre 的 control 事件"，真实工具根本不会被调用。必须改掉这个短路，否则本段无法落库、无法选片。
- `app/router/rules.py`：R01（换一段）补 `tool="media_next"`，R04（停）补 `tool="media_stop"`；R05 已有 `tool="media_play"`。只加字段，不改正则、不改意图、不改顺序。
- `app/router/executor.py`：`execute_rule` 在 `decision.tool` 非空时返回 `None`，把动作让给工具层；保留 `volume_up/down`、`media_defer`、`time_query`、`farewell` 的确定性话术。
- 同步更新 `tests/test_router_rules.py`（不再断言 media_play 由 execute_rule 返回 control 事件，改为断言 decision 带 tool 且 execute_rule 返回 None）与 `tests/test_agent_tools.py` 的媒体事件断言。

六、前端播放（修改 `app/static/js/voice.js`）
- 在 `handleControl` 中实现：
  - `media_play`：创建/复用一个 `<audio>` 元素，设置 `src=ev.url`，`play()`；**不要禁用麦克风**（长者需要在播放中说"停/换一段"），依赖浏览器回声消除，媒体初始音量设为 0.8，若出现自激再调低；
  - `media_next`：用事件里的新 url 替换 `src` 并播放；
  - `stop`：暂停并清空 `src`；
  - `ended`：通过 WS 发送 `{"type":"media_event","action":"ended","history_id":"..."}`；
  - 用户点"停"时同时发送 `{"type":"media_event","action":"stopped","history_id":"..."}`。
- 处理浏览器自动播放限制：`play()` 被拒绝时不报错中断，改为显示一个明显的"点这里继续播放"按钮，点击后重试；不要静默失败。
- 不引入前端框架；不改 TTS/口型/音量逻辑；`volume_up/down` 继续作用于现有 GainNode（媒体音量可同步调整 `<audio>.volume`，但不要破坏原行为）。

七、后端处理 media_event（修改 `app/api/routes/voice.py`）
- 在 WS 消息循环中处理 `media_event`：校验该 history 属于当前设备绑定的 elder，再调用 `record_end`；未知/越权只记警告，不抛异常。
- 事件只更新历史，不触发 TTS 回复。

八、种子脚本（新增 `scripts/seed_media_library.py`）
- 读取清单 JSON（字段：title/type/genre/artist/era/copyright_status/file/duration_seconds），校验文件真实存在于 `app/static/media/library/` 才置 `status=active` 并写 `media_uri`；文件缺失则置 hidden 并明确报错。
- 提供 `--allow-placeholder`：在本地生成 6–8 秒的占位音频（正弦音/静音），`copyright_status=teaching`、标题标注"占位音源"，仅用于链路验收；不得伪装成真实戏曲。
- 教师授权素材未到位时：用 `--allow-placeholder` 生成 20–30 条占位音源验证链路；一条可用素材都没有时，点播必须走 `degraded=media_unavailable` 的明确话术，不伪造歌名、不外链、不播放未授权内容。
- 不下载、不抓取任何网络资源；脚本不写真实长者数据。

九、测试（新增 `tests/test_media_service.py`，更新路由/工具相关测试）
- 覆盖：授权过滤（unknown/expired 不可播）、genre 优先、偏好命中、next 循环、history 写入与结束更新、越权 history 拒绝、无素材降级。
- 需要 DB 的用例用 `db_session`（kangban_test）；纯选片逻辑用 fake 仓储。

【涉及文件】
- 新增：`alembic/versions/0009_media_history.py`、`app/models/media_history.py`、`app/services/media.py`、`scripts/seed_media_library.py`、`tests/test_media_service.py`
- 修改：`app/models/__init__.py`、`app/services/session_state.py`、`app/agent/tools.py`、`app/router/rules.py`、`app/router/executor.py`、`app/api/routes/voice.py`、`app/static/js/voice.js`、`tests/test_router_rules.py`、`tests/test_agent_tools.py`、`.env.example`（如需新增静态目录配置）
- 不改：`app/realtime/**`、旧 alembic 迁移、`app/router/service.py` 的判定顺序

【必须遵守的约束】
- 媒体采用本地静态文件 + `<audio>` 渐进播放即可；不要求自研分片推流，静态服务不支持 Range 也不阻塞验收。
- 只使用教师授权的本地媒体；不爬取、不下载、不使用外链、不播放未授权内容。
- 不新增 `media_history` 以外的表；不回改旧迁移。
- 不实现护工/医生/家属页面、播放榜单、推荐算法（Day8 之外）。
- 路由只做"加 tool 字段 + 执行器让位"，不改正则、不改意图、不改判定顺序；音量控制与"先别放"仍零模型。
- 媒体文件不提交仓库（`app/static/media/library/` 需在 `.gitignore` 中排除，仅保留清单示例）；不要把大文件塞进 Git。
- 不执行 git commit/push。

【验收标准】
1. `alembic upgrade head` 成功；`\d media_history` 字段与索引正确；`downgrade` 可回退该表。
2. `pytest tests/test_media_service.py -q` 全过；`ruff check .` 通过。
3. 手工：说"我想听越剧" → 规则层命中（日志 `route_layer=rule`、`rule_id=R05`）→ 平板真实出声 → `media_history` 新增一行。
4. 手工：说"换一段" → 切到同 genre 下一条，且日志无 LLM 调用；旧记录 `finished_at` 被补齐。
5. 手工：说"停" → 声音停止，当前记录 `completion_rate` 有值（或按无时长留空），麦克风恢复。
6. 手工：把所有媒体置 hidden 后再点播 → 明确降级话术，不播放、不报错。
7. 日志抽查：不记录音频内容，不记录未授权媒体地址。
8. 回归：`tests/test_router_rules.py` 中 R01/R05 用例改为"带 tool 且执行器让位"，不再断言本地通用话术；其余规则（音量/停/安全）行为不变。

【完成后请说明】
- 迁移与表结构；
- 选片/偏好/换段/历史更新规则；
- 前端播放与自动播放处理方式；
- 种子清单格式与占位音源边界；
- 验收命令与真实输出；
- 素材缺口与需要用户提供的清单。

【本段不做什么】
- 不改主控 Agent 结构，只改三个媒体工具；不改安全/规则判定顺序，只给 R01/R04 加 `tool` 字段并让执行器让位。
- 不新增 `media_history` 以外的表；不改旧迁移；不改造 `app/realtime/**`。
- 不引入前端框架，不动 TTS/口型/音量既有逻辑；不做护工/医生/家属媒体页面、榜单、推荐算法。
- 不下载、不爬取任何媒体资源；不播放未授权内容；不提交媒体文件进仓库。
- 不做记忆、用药、评测（其他段）。
```

---

## 段 7｜Iter3 基线 + 失败分层归因（对应 7.4）

```text
【任务】Day7 段7：建立 Iter3 任务级评测（20 个场景）、输出 v1 基线报告与失败分层归因，并提供 eval_gate 雏形。不要编造数字，不要提前打 tag。

【背景】
Day6 已有路由层评测：`eval/fixtures/day6_intents.jsonl`、`scripts/eval_day6.py`、`eval/reports/day6_router_baseline.md`；Day5 有延迟打点 `app/services/latency.py` 与 `logs/` 下的 `turn_latency` 样本。Day7 已具备记忆、用药图、外呼（含降级）、媒体点播。本段要回答"事情办成没有、卡在哪一层"，而不是重复测路由判定。

【当前状态（先核对）】
1. `eval_day6.py` 的指标是分层命中率/意图准确率/敏感召回/延迟/成本对比，没有任务成功率、工具调用准确率、人工接管率。
2. 没有 `eval/fixtures/day7_scenarios.jsonl`；没有任务级跑批脚本；没有 eval gate。
3. 语音/文字回复链路依赖 DB/Redis/TTS/LLM，直接端到端跑批成本高且不稳定。
4. MCP 成功调用日志缺失（只有 `mcp_call_failed`），会影响工具层归因的可观测性。

【目标】
1. 20 个固定长者场景（多轮、含工具与副作用期望），作为 Iter3 的验收资产入库。
2. 一份可复算的 `eval/reports/v1_baseline.md`：8 项指标 + 每条失败的分层归因。
3. `scripts/eval_gate.py`：阈值不达标时非零退出，供后续 CI 接入。
4. 明确 live / offline 两种模式，报告写清样本量、模式、commit。

【具体要求】
一、场景集（新增 `eval/fixtures/day7_scenarios.jsonl`）
- 恰好 20 条；每条字段：
  `id`、`name`、`turns`（1–3 轮用户话术）、`expected`（`route_layer`、`intent`、`tools`（有序数组，可空）、`outcome`（如 `media_played`/`medication_confirmed`/`medication_escalated`/`memory_referenced`/`clarify`/`safety`））、`tags`、`notes`。
- 覆盖比例建议：记忆 4、用药 5（含 3 次未确认升级）、媒体 4、多意图 3、安全/澄清 2、降级 2。
- 使用明确虚构的"测试长者A/B"，不写真实姓名、房间号、病史；不写具体真实药品名以外的最小必要信息（可用"降压药"这类通用描述）。
- 样例必须是**可判定**的：期望工具顺序与结果要能被离线模式验证，不写"回答得自然"这种主观标准。

二、离线跑批（新增 `scripts/eval_day7.py`）
- 默认 offline：用 fake LLM（固定回复/固定 JSON）+ fake embedding（确定性向量）+ 内存/SQLite 会话状态，直接驱动 `route_text` / Agent / 工具（工具的外部副作用用 fake：媒体返回固定条目、工单记录到内存、外呼不真正发起）。
- `--live`：显式开启真实 LLM/TTS/网络；报告中必须标注 `mode=live`，且不得把 live 结果写成默认基线。
- 每条场景输出：实际 `route_layer`、`intent`、`tools`、`outcome`、耗时、是否降级、是否需人工接管。
- 失败时写入 `eval/fixtures/day7_badcases.jsonl`（按 id 去重），字段含 `layer` 归因。

三、指标（8 项，报告里必须给公式与分母）
1. 任务成功率 = outcome 与期望一致的场景数 / 20；
2. P95 延迟：优先读取本轮产生的 `turn_latency` 样本；offline 用各阶段模拟耗时，并在报告注明"模拟值"；
3. S1 闭环率 = 规则层或分类器层完成且未进入主控 Agent/LLM 的请求数 / 总请求数；
4. 分层命中率 = rule / classifier / agent / safety 各自占比；
5. 单会话 token 成本：live 模式用响应 usage；无 usage 时用字符数估算并标注 `estimated=true`；offline 记 0 并说明；
6. 敏感召回率 = 敏感样例被 `is_sensitive=true` 命中的比例（目标 ≥95%，宁可错杀）；
7. 工具调用准确率 = 工具名与顺序均匹配的场景数 / 含工具期望的场景数；
8. 人工接管率 = 进入 HITL/建单/升级为人工的场景数 / 20。
- 另给"失败分层归因表"：每条 bad case 标注 `layer in {asr, laya, recall, llm, tool, orchestration}`，并写一句可复核的理由（引用实际输出字段，不写主观评价）。

四、报告（生成 `eval/reports/v1_baseline.md`）
- 头部：生成时间、git commit、mode、fixtures 版本、样本量 20。
- 指标表：上述 8 项 + 与 Day6 路由基线的对比说明（口径不同要写清，不能直接比较）。
- 场景明细表：id/name/期望/实际/是否通过/耗时/降级。
- 归因表：失败 id、层级、证据字段、建议下一步。
- 复现命令。
- 不允许手写或猜测数字；所有数字来自脚本输出。

五、Gate（新增 `scripts/eval_gate.py`）
- 读取报告或跑批结果，按阈值判定：默认 `task_success_rate >= 0.80`、`sensitive_recall >= 0.95`、`tool_accuracy >= 0.80`；阈值可用参数或 YAML 覆盖。
- 指标缺失/样本不足（<20）视为失败；退出码 0/1，输出一行结论与失败指标。
- 不接 CI hook（Day10 再做），只保证本地可跑；`--warn-only` 可只告警不阻断。

六、可观测性与 Trace（修改 `app/services/voice.py` / `app/core/logging.py`，可选 `app/agent/mcp_client.py`）
- 每次对话轮次至少落一条结构化 trace：`thread_id / turn_id / route_layer / rule_id / intent / tools(有序) / asr_ms / llm_first_token_ms / tts_first_byte_ms / degraded / reply_chars`。
- 只记元数据，不记对话原文、不记音频；`thread_id` 必须能把规则→Laya→Tool→TTS 串起来（沿用现有 trace_id 机制，不新造 ID 体系）。
- 若 JSON 日志有字段白名单，记得同步放行新字段，否则字段会被静默丢弃（本项目踩过）。
- 不接 Langfuse / 外部可观测平台（Day10）。
- `app/agent/mcp_client.py` 成功调用补一条结构化日志（server_id、tool、elapsed_ms、ok=true），不记参数与长者原文；若不做，报告"工具层"归因要注明"MCP 成功日志缺失"。

七、测试（新增 `tests/test_eval_day7.py`）
- fixtures 结构校验（20 条、字段齐全、id 唯一）；
- 指标计算（构造已知结果集验证成功率/召回率/工具准确率/接管率）；
- gate 退出码（达标=0、不达标=1、样本不足=1）；
- 报告渲染包含必需章节，且不含真实长者姓名。

【涉及文件】
- 新增：`eval/fixtures/day7_scenarios.jsonl`、`scripts/eval_day7.py`、`scripts/eval_gate.py`、`eval/reports/v1_baseline.md`（脚本生成）、`tests/test_eval_day7.py`
- 修改（Trace）：`app/services/voice.py`、`app/core/logging.py`；可选 `app/agent/mcp_client.py`（成功日志）
- 不改：`app/router/**` 判定逻辑、数据库结构

【必须遵守的约束】
- 不修改业务判定逻辑来"让指标变好"；发现 bad case 只记录与归因，修复另开任务。
- 不编造数字；没有样本的指标写"无数据"，不能填 0 冒充结果。
- 不把长者原文、音频写进报告与日志；场景集使用虚构测试长者。
- 不执行 `git tag`、不 commit/push；只在交付说明里给出建议命令（如 `git tag iter3-demo`）。
- 不实现 CI hook、不做 Langfuse 接入（Day10）。
- 不新增 Web 端点。

【验收标准】
1. `pytest tests/test_eval_day7.py -q` 全过；`ruff check .` 通过。
2. `python scripts/eval_day7.py --out eval/reports/v1_baseline.md` 能在 offline 模式稳定复跑，两次结果一致（除时间/commit 字段）。
3. 报告包含 8 项指标（含公式/分母）、20 条明细、失败归因表、复现命令、mode 与样本量。
4. `python scripts/eval_gate.py --report eval/reports/v1_baseline.md` 返回明确结论与退出码；构造一个低于阈值的报告时退出码为 1。
5. 抽查报告与日志：无真实长者数据、无音频、无未授权内容。
6. 抽查一轮真实对话日志：能用 thread_id 串起规则→Laya→Tool→TTS，且日志中没有对话原文。

【完成后请说明】
- 20 条场景的分布与设计依据；
- 每项指标的口径、分母、数据来源（真实/模拟/估算）；
- offline 与 live 的差异与限制；
- 当前基线数字与主要失败层级（不要美化）；
- gate 阈值与建议；
- 建议的 tag/提交命令（由用户执行），以及下一步修复优先级。

【本段不做什么】
- 不改任何业务判定逻辑、路由规则、Agent、工具实现；只新增评测资产与脚本。
- 不新增表/迁移、不新增 Web 端点、不接 CI hook、不接 Langfuse。
- 不编造数字；无样本指标写"无数据"；不把 live 结果当默认基线。
- 不写真实长者数据、不记对话原文与音频；不执行 `git tag` / commit / push。
- 不做用药/记忆/媒体功能修复（发现问题只记录与归因，另开任务）。
```

---

## 附：段间冲突检查表（执行者可自查）

| 检查项 | 段 1 | 段 2 | 段 3 | 段 4 | 段 5 | 段 6 | 段 7 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 新增 Alembic 迁移 | 仅 `0008 medication_order_flow` | 禁止 | 禁止 | 禁止 | 禁止 | 仅 `0009 media_history` | 禁止 |
| 改 `app/models/**` | 仅 `medication.py`、`workorder.py` | 禁止 | 禁止 | 禁止 | 禁止 | 仅新增 `media_history.py` 并注册 | 禁止 |
| 改用药计划 API / 权限 | 允许（唯一写入方） | 只读消费 | 只读服务 | 只读计划 | 禁止 | 禁止 | 禁止 |
| 改 `app/services/workorder.py` | 长期工单建单 + 关闭保护 | 禁止 | 漏服一次性建单 | 禁止 | 禁止 | 禁止 | 禁止 |
| 改 `app/router/**` | 禁止 | 禁止 | 仅加 `MED-01` 前置分支 | 禁止 | 禁止 | 仅 R01/R04 加 `tool`，`executor` 让位 | 禁止 |
| 改 `app/realtime/**` | 禁止 | 禁止 | 禁止 | 允许 | 禁止 | 禁止 | 禁止 |
| 改 `app/static/js/voice.js` | 禁止 | 禁止 | 禁止 | 加 `rtc_call_offer` 处理 | 禁止 | 加媒体播放 | 禁止 |
| 改 `doctor.html/family.html/workorders.js` | 禁止 | 允许 | 禁止 | 禁止 | 禁止 | 禁止 | 禁止 |
| 改 `app/core/lifespan.py` | 禁止 | 禁止 | 禁止 | 允许（调度器） | 禁止 | 禁止 | 禁止 |
| 使用 LangGraph `interrupt` | 禁止 | 禁止 | 禁止 | 禁止 | 禁止 | 禁止 | 禁止 |
| 新增数据表 | 禁止（只加列） | 禁止 | 禁止 | 禁止 | 禁止 | 仅 `media_history` | 禁止 |
| 通知/看板/图表/家属日报 | 禁止 | 禁止 | 禁止 | 禁止 | 禁止 | 禁止 | 禁止 |
| `git commit/push/tag` | 禁止 | 禁止 | 禁止 | 禁止 | 禁止 | 禁止 | 禁止 |
