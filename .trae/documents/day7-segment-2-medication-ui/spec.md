# Day7 段2 规格｜医生端最小界面 + 家属只读 + 工单页适配

## 1. 范围与硬约束

**目标**：消费段1/段1.5 已存在的后端接口，补齐用药计划的最小可用前端。

**只改前端**：

- 修改 `app/static/doctor.html`
- 修改 `app/static/family.html`
- 修改 `app/static/js/workorders.js`
- 新增 `app/static/js/medication.js`
- 不改 `app/api/**`、`app/services/**`、`app/models/**`、`alembic/**`、`app/realtime/**`、`app/router/**`
- 不新增页面文件、不引入框架/构建工具、不改登录与角色校验
- 不执行 `git commit` / `git push` / 打 tag
- 不重复实现段1.5 的长者选择器、WS、轮询、筛选

## 2. 已核实契约（直接消费，不另造）

### 2.1 用药计划（医生）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/elders/{elder_id}/medication-plans` | 201，返回 `MedicationPlanDoctorView`，新计划为 `planned` |
| GET | `/api/v1/elders/{elder_id}/medication-plans?status=` | 医生/护士/管理员；医生视图含 `internal_note`/`workorder_id` |
| GET | `/api/v1/medication-plans/{plan_id}` | 单条 |
| PUT | `/api/v1/medication-plans/{plan_id}` | 局部更新，返回医生视图 |
| POST | `/api/v1/medication-plans/{plan_id}/confirm` | 返回医生视图，含 `workorder_id` |
| POST | `/api/v1/medication-plans/{plan_id}/pause\|resume\|cancel` | 返回医生视图 |

创建体：`{drug_name, dose, schedule:{times:["HH:MM"],weekdays:[0..6]}, instructions?, internal_note?, start_date, end_date?}`；`weekdays` 为空 = 每天；`times` 1..6 个、不重复、升序；`end_date >= start_date`。

### 2.2 家属只读

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/family/elders/{elder_id}/medication-plans?status=` | 字段白名单，草稿不可见 |
| GET | `/api/v1/family/elders/{elder_id}/medication-plans/{plan_id}/history` | `[{time, actor, plan_version, changes:[{field,before,after}]}]`，`internal_note` 已被服务端整条丢弃 |
| GET | `/api/v1/family/elders/{elder_id}/medication-history?page&page_size&from&to` | `{items,total,page,page_size}`，`items[]` 含 `order_id/plan_id/drug_name/dose/scheduled_at/status/attempt_count/confirmed_at/confirmed_source/events[{event_type,source,created_at}]` |

### 2.3 工单列表/详情字段

`WorkorderListItem` 已含：`type`、`title`、`category`、`order_kind`（`one_off|standing`）、`medication_plan_id`、`medication_order_id`、`effective_from/effective_until`、`is_read`、`read_at`、`my_status`、`pending`。

工单 `type` 取值：`sensitive`、`medication_plan`、`missed_medication`。

## 3. 详细设计

### 3.1 新增 `app/static/js/medication.js`

IIFE，暴露 `window.KBMedication`：

- `openDoctorPlans(elder)`：`elder = {elder_id, name}`，打开医生计划列表弹层。
- `mountFamily(opts)`：`opts = {rootId}`，渲染家属「用药计划（只读）」+「服药历史」两个区块。
- 内部工具：`fmtDate`、`fmtTime`、`weekdayText`、`planStatusTag`、`orderStatusTag`、`eventLabel`、`scheduleText`、`changeValueText`。
- 中文映射：
  - 计划：`planned→待确认`、`active→生效中`、`paused→已暂停`、`completed→已完成`、`cancelled→已取消`
  - 服药：`pending→待服`、`calling→呼叫中`、`confirmed→已服`、`missed→未服`、`skipped→已跳过`、`cancelled→已取消`
  - 事件：`created→已生成任务`、`reminded→已提醒`、`confirmed→已确认`、`denied→已否认`、`timeout→超时`、`missed→漏服`、`skipped→已跳过`、`cancelled→已取消`
  - 星期：0=周一 … 6=周日；全部选中提交 `[]`（每天），展示为「每天」
- 所有输出经 `KB.esc`，时间统一 `YYYY-MM-DD HH:mm`，日期 `YYYY-MM-DD`。

#### 医生计划列表弹层

- 标题：`用药计划 · {长者姓名}`。
- 工具栏：状态筛选（全部/待确认/生效中/已暂停/已完成/已取消）→ 重新请求；`+ 新建用药计划`；刷新。
- 列表表格列：药名、剂量、服用时间（顿号连接）、星期、起止日期、状态徽标、更新时间、工单号（`workorder_id` 前 8 位，无则 `—`）、操作。
- 操作按状态：
  - `planned`：确认下发、编辑、取消
  - `active`：编辑、暂停、取消
  - `paused`：编辑、恢复、取消
  - `completed/cancelled`：无操作
- **未绑定/无权限**：列表请求 403/404 时只显示「无权限或未绑定该长者」，**不渲染新建按钮**。
- 空态：「暂无用药计划」。
- 暂停/恢复/取消：`KB.confirmModal` 二次确认；取消文案含「将同时关闭长期用药工单」。
- 操作成功后 toast + 刷新列表。

#### 新建表单

`KB.openModal` 表单（复用 `.field` / `.form-error` / `.modal-actions`）：

- 药名（必填）、剂量（必填）
- 服用时间：可增删多个 `<input type="time">`，至少 1 个，前端查重
- 星期：7 个 checkbox，默认全选
- 开始日期（必填，默认今天）、结束日期（可选）
- 用药说明、内部备注（可选）

前端校验（就地 `data-err` 提示，不提交）：药名/剂量非空；至少一个时间且格式 `HH:MM`、不重复；`end_date >= start_date`。

保存 = `POST` 创建（不确认）→ 成功后进入确认页。

#### 确认页（覆盖层）

- 展示：长者、药名、剂量、全部服用时间、星期、起止日期、用药说明、内部备注。
- 警示文案：「确认后将立即向护工下发长期工单，并开始按时间提醒」。
- 「返回修改」：回到预填表单，不调用 confirm。
- 「确认下发」：`POST /medication-plans/{plan_id}/confirm`；成功后关闭、刷新列表、toast「已下发，工单号 {workorder_id 前 8 位}」。
- 禁止保存即下发，禁止 `window.confirm`。

#### 修改已生效计划

- 「编辑」预填表单；保存时先计算差异（药名/剂量/时间/星期/起止/说明/内部备注）。
- 无差异：toast「未检测到修改」，不发请求。
- 有差异：确认页仅列变化字段「原值 → 新值」；确认后才 `PUT`（只提交变化字段）。
- 成功后刷新列表。

#### 家属区块

根容器内两张卡：

1. **用药计划（只读）**
   - 状态筛选：全部/生效中/已暂停/已完成/已取消（服务端 `status`）。
   - 字段：药名、剂量、服用时间、星期、起止日期、用药说明、状态。
   - 「修改记录」按钮：`GET .../{plan_id}/history`，时间线展示时间、操作角色、版本、`field: before → after`；`changeValueText` 对 `schedule` 做时间/星期格式化；`internal_note` 即使出现也用标签兜底过滤（服务端已剔除）。
   - 不显示内部备注、创建人、工单号；无任何编辑/暂停/取消按钮。
   - 空态「暂无用药计划」；失败显示明确错误，不伪造数据。
2. **服药历史**
   - `GET .../medication-history?page=1&page_size=30`，按 `scheduled_at` 倒序（服务端顺序直接使用）。
   - 每条：计划时段（`scheduled_at`）、药名、剂量、状态、尝试次数、确认时间。
   - 「展开事件」显示 `events[]` 的事件类型/来源/时间。
   - 「加载更多」：`page += 1` 追加；`hasMore = page * 30 < total`。
   - 不显示 `skip_reason`、`workorder_id`、`internal_note`、账号 UUID。
   - 空态「暂无服药记录」。

#### 长者选择复用

- 初始从 `sessionStorage.kb_wo_elder` 读取；未选时两个区块都显示「请选择长者」，**不发请求**。
- 监听 `document` 自定义事件 `kb:wo-elder`（`detail.elderId`），切换后两区块一起重置并刷新。
- 不在 localStorage 存任何长者信息。

### 3.2 `app/static/doctor.html`

- 删除「用药方案管理（后续版本 · HITL）」占位卡（其余占位卡保留）。
- 每个长者卡片增加「用药计划」按钮（`data-medication`），点击 `stopPropagation` 后调用 `KBMedication.openDoctorPlans({elder_id, name})`；卡片其余区域仍打开健康档案。
- 在 `/js/workorders.js` 之后引入 `<script src="/js/medication.js"></script>`。

### 3.3 `app/static/family.html`

- 在 `#workorders` 之后新增 `<section class="mt-24" id="family-medication"></section>`。
- 引入 `/js/medication.js`，在 `KBWorkOrders.mount(...)` 之后调用 `KBMedication.mountFamily({rootId:"family-medication"})`。

### 3.4 `app/static/js/workorders.js`

- 新增 `TYPE_LABEL = {sensitive:"敏感事件", medication_plan:"长期用药", missed_medication:"漏服"}`。
- 类别列改 `it.category || TYPE_LABEL[it.type] || it.type || "未分类"`。
- `order_kind === "standing"` 时在类别列与详情标题旁显示「长期用药」徽标（`tag tag-clay`）；一次性不显示。
- 员工端未读圆点改用 `is_read === false`（不再用 `my_status` 推断）；保留 `my_status` 三态标签。
- 详情 kv 增加「工单类型」（`title` 或 `TYPE_LABEL[type]`）与生效期（有 `effective_from/effective_until` 时）。
- 管理员：`order_kind === "standing"` 时不渲染终审关闭输入与按钮，显示提示「长期用药工单随用药计划停用/取消自动关闭」。
- 实时：`admin`/`family` 收到 `ticket.created` 只 `KB.toast("工单已更新")` + 刷新，不弹员工处理弹窗；`ticket.updated/resolved` 同样只 toast + 刷新。员工端保持现状（created 才弹窗）。
- 家属选择器变更时 `document.dispatchEvent(new CustomEvent("kb:wo-elder",{detail:{elderId}}))`；恢复已保存选择时也派发一次。
- 暴露 `window.KBWorkOrders.getElderId()` 返回当前选中长者，便于同页其他模块初始化读取。

## 4. 验收标准（逐条）

1. 医生新建：确认页全字段 → 确认下发 → 列表「生效中」+ 工单号；返回修改不产生工单。
2. 医生修改生效计划：出现「原值 → 新值」确认页，确认后列表更新。
3. 未绑定医生：无新建按钮，403/404 显示无权限提示。
4. 家属：只读计划 + 已取消/已完成筛选 + 修改记录 + 服药历史（含事件展开）；无编辑按钮/内部备注/跳过原因/工单号。
5. 护工：长期用药标识 + 本人未读圆点，点开后消失；提交处理项后工单仍打开；`mine` 筛选翻页生效。
6. 管理员：可见长期工单；关闭按钮禁用/隐藏并有说明；实时更新只 toast。
7. 控制台无未捕获异常；刷新后数据仍正确。

## 5. 验证方式

- `python -m pytest --tb=no -q --disable-warnings`（回归，不新增测试）
- `ruff check app tests`
- 浏览器手工走查四角色（医生/家属/护工/管理员），使用虚构长者与通用药名。
- 由于本段不改后端，重点验证前端不新增 4xx/5xx：列表、确认、修改、暂停/恢复/取消、历史与分页。

## 6. 已知限制

- 不做站外通知、未读角标、看板/图表/统计大盘、家属 H5 日报。
- 不做用药提醒播放、LangGraph、反向 WebRTC、记忆与媒体。
- 星期约定 0=周一…6=周日；若后续调度器采用其他约定，仅需调整 `medication.js` 的 `WEEK_LABELS`。
