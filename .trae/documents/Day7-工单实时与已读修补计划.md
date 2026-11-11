# Day7 方案与提示词改写计划（服药双表 / 工单实时 / 按账号已读）

> 范围：**本轮只改两份文档**，不改代码、不建迁移、不动前端。
> 目标文档：
> - `docs/Day7-改造方案.md`
> - `docs/Day7-改造提示词.md`
>
> 已拍板口径：
> 1. 实时采用「四端 WS + 30 秒轮询兜底」；
> 2. 已读采用「新增按账号回执表」；
> 3. 服药事实采用**双表** `medication_orders` + `medication_events`；
> 4. 任务单**按需创建 + 启动补漏**；
> 5. 家属可见**完整服药历史**（分页，字段白名单）。

---

## 一、核验结论（全部来自当前代码/文档）

### 1.1 用户点名的四个问题

| # | 结论 | 代码证据 | 根因 |
| --- | --- | --- | --- |
| U1 | 管理员推送是死代码 | `app/services/workorder_events.py` 的 `_resolve_recipients` 把全部启用管理员加入 recipients；`app/api/routes/workorders.py` 的 `_authenticate_staff_ws` 只放行 doctor/nurse；`app/static/js/workorders.js` 的 `isStaff` 只含 doctor/nurse，且仅 staff 调 `connectWs()`；`admin.html` 以 `role:"admin"` 挂载 | 广播侧算了管理员，接入侧从未允许管理员 |
| U2 | 家属无实时、无轮询，且必须先选长者 | `workorders.js` 的 `loadElders()` 自动取 `state.elders[0]`；`listUrl()` 家属必须带 `elder_id`；仅 staff 连接 WS；无列表定时刷新 | 家属接口按长者维度建模，前端没有"无选择态/多长者切换/刷新"的完整状态机 |
| U3 | 已读是角色级 | `workorder_dispositions` 唯一键 `(workorder_id, role)`；`mark_read` 把整行置 `read` 并覆盖 `account_id`；`my_status` 与 `count_open_by_role` 都按角色聚合 | 表结构只能表达"该角色已读"，无法表达"某账号已读" |
| U4 | 漏服建单函数不存在 | 全仓仅 `docs/*` 出现 `missed_medication`；`app/services/workorder.py` 只有 `create_sensitive_work_order`，`app/` 下无 `create_missed_medication_work_order` 及调用点 | 提示词写了函数名与字段，但从未进入实现清单 |

### 1.2 用户追问：为什么原方案不建 `medication_orders` / `medication_events`，替代物是什么

原文档口径在 [Day7-改造方案.md](file:///c:/Users/lmxyrqlxr/Desktop/kangban_3D_Digital_Human-main/docs/Day7-改造方案.md#L103) 的 T5，以及 [Day7-改造提示词.md](file:///c:/Users/lmxyrqlxr/Desktop/kangban_3D_Digital_Human-main/docs/Day7-改造提示词.md#L16) 的禁令：

| 原表职责 | 原文档的替代物 | 实际问题 |
| --- | --- | --- |
| `medication_orders`：每个计划每个时段的待执行任务 | 长期投递用 `workorders(order_kind=standing)`；单次提醒与重试用 LangGraph 图状态（SQLite checkpointer） | 图状态是运行时产物，不是业务任务；无法按"某天某时段"查询，也无法在页面展示"今日待办/漏服" |
| `medication_events`：已服/未服/跳过的事实流水 | 提示词只写"写入当日记录"，但全仓没有这张表；实际只能落图状态与结构化日志 | **没有持久化落点**；日志不可查询，图状态清理/换机即丢，"已记录"无法自证，更无法给家属看历史 |

结论：
- `medication_orders` 用 `workorders + 图状态` 替代**不成立**：工单是"派单/处理"语义，不是"每个时段一次服药任务"。
- `medication_events` **没有被真正替代**，只是被含糊表述遮蔽。
- 因此本次改为双表：`medication_orders` 保存"时段任务当前态"，`medication_events` 保存"追加事实"，`workorders` 只负责漏服后的派单。

### 1.3 本轮补充核验出的其他隐患

| # | 隐患 | 代码证据 | 影响 |
| --- | --- | --- | --- |
| N1 | **事件在事务提交前推送** | `create_sensitive_work_order` 在 `flush()` 后调 `_publish_event`，调用方 `app/services/voice.py` 在之后才 `db.commit()`；`mark_read` 等写路径同理 | 客户端收到 `ticket.created` 立刻刷新时，新工单尚未提交；提交失败会出现幽灵事件 |
| N2 | **列表项缺 `type`/`title`** | `WorkorderListItem` 只有 `category`（映射 `sensitive_category`）；`_item_from` 不返回 `type`/`title` | 长期用药与漏服工单 `sensitive_category` 为 NULL，四端统一显示"未分类" |
| N3 | **家属接口没有状态筛选参数** | `list_family_workorders` 把 `status_filter=None` 写死；方案 §4.3 却要求家属状态筛选 | 该验收在当前接口下不可能完成 |
| N4 | **状态筛选是当前页客户端过滤** | `filteredItems()` 只过滤 `state.items`；`list_work_orders` 的 `status` 只接受 `WorkorderStatus`，不能表达 `pending/handled` | 翻页后筛选丢失；员工无法筛"未读/未处理" |
| N5 | **`my_status` 被复用表达已读** | `_item_from` 的 `my_status` 取处理项状态 | 改为按账号回执后，"本人未读但角色已处理"无法同时表达 |
| N6 | **read 会改工单全局状态** | `mark_read` 在 `OPEN` 时置 `ACKNOWLEDGED` | 一名护士已读后，管理员/家属看到"已读待处理"，但可能无人处理 |
| N7 | **WS 生命周期无约束** | 只在首帧鉴权一次；无服务端探测、无 token 到期关闭、无连接数上限；前端从不发 ping | 掉线连接残留；token 过期/账号停用后仍可收消息 |
| N8 | **管理员/家属没有计数语义** | `count_open_by_role` 对非 staff 返回 0；`setBadge` 只在 staff 初始化 | 四端 WS 后照搬 `ticket.counts` 会出现恒 0 角标 |
| N9 | **`notifyCreated` 是员工向弹窗** | 文案"请尽快确认情况并填写处理方案"，带"查看并处理"按钮 | 管理员/家属复用会产生错误引导 |
| N10 | **漏服去重只有查询、无唯一约束** | 提示词只写"先查当日是否存在"；`workorders` 无 `dedupe_key` | 调度重试/并发会建出两张漏服单 |
| N11 | **长期工单系统关闭会被门禁阻塞** | `close_work_order` 要求 doctor/nurse 处理项都 `handled` | 计划取消时的系统关闭走不通；系统关闭没有定义状态与事件 |
| N12 | **事件总线是进程内单例** | `workorder_event_hub` 为模块级对象；无 Redis 广播 | 多 worker 时 WS 只覆盖本进程；轮询是唯一兜底 |
| N13 | **APScheduler 不在默认依赖** | `pyproject.toml` 中 `apscheduler>=3.10` 位于 `optional-dependencies.infra`；`lifespan.py` 未接入 | 段 4 直接执行会 `ImportError` |
| N14 | **调度幂等依赖进程内缓存** | 段 4 提示词写"进程内 `(plan_id, 日期, 时段)` 去重缓存" | 重启后缓存丢失；双表后应改为数据库唯一键 |
| N15 | **管理员/家属面板是懒加载** | `admin.html`/`family.html` 仅在打开工单面板时才 `mount(...)` | WS 只在面板打开后建立，验收不能假设"登录即连接" |
| N16 | **家属多长者无聚合视图** | 家属接口按 `elder_id`；前端无选择器持久化 | 多长者家庭只能看第一个 |
| N17 | **约束与枚举的硬边界** | `ck_workorder_events_type` 仅 `created/read/disposition/closed/occurrence`；`ck_workorders_source` 仅 `sensitive/manual`；`ck_workorder_events_actor_role` 无 `system` | 确认/修改事件、系统建单若照自然语言实现会违反约束 |
| N18 | **家属 WS 授权边界未定义** | `FamilyElderLink` 有 `status` 与 `valid_until`；`_resolve_recipients` 不含家属 | 扩到家属时必须同时校验账号启用、绑定有效、未过期 |
| N19 | **原提示词"不新建表"与本轮设计冲突** | 提示词第 16/169/196 行禁止 `medication_orders`/`medication_events` | 执行者会直接跳过双表，回到"无服药事实"的旧问题 |

---

## 二、统一设计口径（写入两份文档的"标准答案"）

### D1 服药任务单 `medication_orders`（当前态）

```text
order_id            UUID PK
plan_id             UUID NOT NULL FK medication_plans(plan_id) ON DELETE CASCADE
elder_id            UUID NOT NULL FK elders(elder_id) ON DELETE CASCADE
scheduled_at        TIMESTAMPTZ NOT NULL
status              VARCHAR(16) NOT NULL DEFAULT 'pending'
                    CHECK IN ('pending','calling','confirmed','missed','skipped','cancelled')
attempt_count       INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0)
last_attempt_at     TIMESTAMPTZ NULL
confirmed_at        TIMESTAMPTZ NULL
confirmed_source    VARCHAR(16) NULL CHECK IN ('voice','text')
skip_reason         TEXT NULL          -- 仅员工可见，家属接口不返回
created_at / updated_at
UNIQUE (plan_id, scheduled_at)         -- uq_medication_orders_plan_scheduled
INDEX (elder_id, scheduled_at DESC), (status, scheduled_at)
```

- 语义：一条订单 = 某计划某时段的**唯一任务实例**；`(plan_id, scheduled_at)` 唯一键是幂等边界。
- 不建 `medication_orders.workorder_id`；反向由 `workorders.medication_order_id` 指向订单，避免双向写不一致。
- `confirmed` 只能来自长者语音/文字；员工与家属只能 `skipped`，不能代替长者确认。

### D2 服药事实 `medication_events`（追加型）

```text
event_id            UUID PK
order_id            UUID NOT NULL FK medication_orders(order_id) ON DELETE CASCADE
plan_id             UUID NOT NULL FK medication_plans(plan_id) ON DELETE CASCADE
elder_id            UUID NOT NULL FK elders(elder_id) ON DELETE CASCADE
event_type          VARCHAR(24) NOT NULL
                    CHECK IN ('created','reminded','confirmed','denied','timeout','missed','skipped','cancelled')
source              VARCHAR(16) NOT NULL
                    CHECK IN ('scheduler','voice','text','staff','family','system')
actor_account_id    UUID NULL FK accounts(account_id) ON DELETE SET NULL
actor_role          VARCHAR(16) NULL CHECK IN ('doctor','nurse','admin','family')
payload             JSONB NULL      -- 只放元数据：attempt/channel/reason_code/workorder_id
dedupe_key          VARCHAR(160) NULL
created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
UNIQUE (dedupe_key) WHERE dedupe_key IS NOT NULL
INDEX (order_id, created_at), (plan_id, created_at), (elder_id, created_at DESC)
```

- 只追加不修改；不存对话原文、音频、PCM、`internal_note`。
- 幂等键示例：`reminded:{order_id}:{attempt}`、`missed:{order_id}`、`confirmed:{order_id}`。
- `actor_role` 不含 `system`；系统动作写 `NULL`（与现有 `occurrence` 事件一致）。

### D3 状态机与事件映射

| 触发 | 订单状态 | 事件 |
| --- | --- | --- |
| 扫描到点 `ensure_order` | `pending` | `created`（`source=scheduler`） |
| 发起外呼/TTS | `calling`，`attempt_count += 1`，`last_attempt_at=now` | `reminded`（payload `{attempt, channel}`） |
| 长者确认 | `confirmed`，写 `confirmed_at/source` | `confirmed` |
| 长者否认 | 尝试未耗尽回 `pending`；耗尽转 `missed` | `denied`；耗尽再写 `missed` 并建工单 |
| 超时 | 同否认 | `timeout`；耗尽再写 `missed` 并建工单 |
| 员工/家属标记跳过 | `skipped`，写 `skip_reason` | `skipped`（带 `actor_role`） |
| 计划暂停/取消 | 未终态订单转 `cancelled` | `cancelled` |
| 启动补漏发现已过时段且无订单 | `missed`，建工单 | `created` + `missed`（`reason_code=startup_backfill`） |

- 重试上限沿用 `medication_retry_limit=3`；超时沿用 `medication_call_timeout_seconds=30`。
- 所有状态变更与事件写入**同一数据库事务**；提交后再推送 WS（见 D11）。

### D4 任务生成：按需创建 + 启动补漏

- 扫描：每 `medication_scan_interval_seconds`（默认 30 秒）对 `active` 计划计算窗口内时段；`ensure_order` 用：
  ```sql
  INSERT INTO medication_orders (...) VALUES (...)
  ON CONFLICT (plan_id, scheduled_at) DO NOTHING
  ```
  然后 `SELECT` 回读；已终态的订单跳过。
- 启动补漏（lifespan 启动后、首轮扫描前）：
  - 对 `active` 计划枚举今天已过时段的计划时间；
  - 缺订单的，创建为 `missed` 并写 `created` + `missed`（`reason_code=startup_backfill`），同时建漏服工单；
  - 只补 `medication_backfill_lookback_hours`（默认 24 小时）内，避免长时间停机后生成大量历史噪声。
- 不再依赖进程内 `(plan_id, 日期, 时段)` 缓存作为唯一去重；唯一键才是幂等边界。
- 单实例部署；多实例唯一键能防重复订单，但不能防重复外呼，文档必须写明。

### D5 漏服派单与工单关联

- `workorders` 增加 `medication_order_id UUID NULL FK medication_orders(order_id) ON DELETE SET NULL`。
- `create_missed_medication_work_order(session, *, order, reason_code)`：
  - `type="missed_medication"`、`source=MANUAL`、`priority=HIGH`、`order_kind=one_off`、
    `medication_plan_id=order.plan_id`、`medication_order_id=order.order_id`、
    `dedupe_key=f"missed:{order.order_id}"`；
  - `title=f"用药漏服：{drug_name}"`；`description` 只写时段/尝试次数/原因码，不写对话原文；
  - 处理项 doctor/nurse 各一条 `unread`；事件 `created` 的 `actor_role=None`；
  - 先查 `dedupe_key`，再插入；捕获 `IntegrityError` 后回滚保存点并重查，命中则 `occurrence_count += 1` 并写 `occurrence`。
- 调用点只有两个：段 3 图"重试耗尽"、段 4 调度器"超时耗尽/启动补漏/无设备"。禁止另写建单逻辑。

### D6 家属完整服药历史

- 新接口：`GET /api/v1/family/elders/{elder_id}/medication-history?plan_id=&from=&to=&page=&page_size=`
- 权限：`FamilyElderLink` 有效且未过期；否则 404。
- 返回 `items[]`：`order_id, plan_id, drug_name, dose, scheduled_at, status, attempt_count, confirmed_at, confirmed_source, events[]`；
  `events[]` 只含 `event_type, source, created_at`。
- **绝不返回**：`internal_note`、`actor_account_id`、`device_id`、`payload` 原文、`workorder_id`、`skip_reason`。
- 分页默认 30、最大 100；`from/to` 可选，无范围时按 `scheduled_at DESC` 返回全部历史。
- 员工侧可选：`GET /api/v1/elders/{elder_id}/medication-orders`（doctor 绑定/nurse 楼栋/admin 全量），返回内部字段供核对。

### D7 四端工单事件通道

- 路径保留 `/api/v1/staff/ws`（历史命名），文档注明"实为工单事件通道"。
- 新增唯一常量：
  ```python
  WS_ROLES = frozenset({AccountRole.DOCTOR, AccountRole.NURSE, AccountRole.ADMIN, AccountRole.FAMILY})
  ```
  WS 鉴权与 `_resolve_recipients` 共用，禁止两处各写一份。
- `_resolve_recipients` 增补家属：`link.status=ACTIVE`、`valid_until IS NULL OR valid_until > now()`、`Account.role=FAMILY`、`Account.status=ACTIVE`。
- `_dispatch`：只有 `role in STAFF_ROLES` 才计算 `unhandled` 并补发 `ticket.counts`；admin/family 不发 counts。
- `_payload` 增加 `type`；仍不返回 `description/context_snapshot/方案全文`；WS 只作刷新信号。
- 心跳：客户端每 25 秒 `ping`，服务端 `pong`；60 秒无上行由客户端关闭并转轮询；服务端按 JWT `exp` 到期主动关闭。
- 单账号连接上限建议 3；账号停用/角色变更不承诺实时生效，下次重连生效。

### D8 轮询兜底

- 四端统一：WS `onclose/onerror` 或心跳超时后启动 30 秒 `setInterval`；重连成功停止。
- staff 轮询"列表 + counts"；admin 轮询"列表"；family 轮询"当前选中长者列表"。
- 无选中长者的家属不请求，只显示空态。

### D9 按账号已读

- 新表 `workorder_reads`（`0008`）：
  ```sql
  workorder_id UUID NOT NULL REFERENCES workorders(workorder_id) ON DELETE CASCADE,
  account_id   UUID NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
  read_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (workorder_id, account_id)
  ```
- `mark_read` 写回执（`ON CONFLICT DO NOTHING`）；保留处理项 `unread→read` 与 `OPEN→ACKNOWLEDGED` 的既有行为，但已读展示不再取 `account_id`。
- 角色级 `handled` 不变，继续承担关闭门禁；文档明确"已读按账号、处理按角色"。
- `WorkorderListItem` 增加 `is_read: bool | None`、`read_at`；staff 返回实际值，admin/family 为 `None`。
- `count_open_by_role` 改为"该账号未读且角色未 handled"的数量，条件增加 `NOT EXISTS (workorder_reads ... account_id=:account_id)`。

### D10 列表与筛选契约

- `WorkorderListItem` 增加 `type`、`title`；前端类别列 `category || TYPE_LABEL[type]`（`medication_plan→长期用药`、`missed_medication→漏服`、`sensitive→敏感事件`）。
- 列表接口统一支持 `status`（`WorkorderStatus` 值）；家属接口补该参数。
- 员工端新增 `mine`：`unread|read|handled`；admin/family 传 `mine` 返回 422。
- 前端删除当前页 `filteredItems()`，把 `status/mine` 下推到服务端分页。

### D11 推送时机

- 服务层不再自行 publish；所有写路径统一"**commit 成功后**发布"：
  - `voice._create_work_order_safely`：`await db.commit()` 后发布；
  - `mark_read` / `submit_disposition` / `close_work_order`：路由 `commit()` 后发布；
  - 漏服/系统关闭/服药状态变更：同样在 commit 后发布。
- commit 前构造轻量 DTO，避免 commit 后 ORM 属性过期触发额外查询。
- 提示词写死："禁止在事务提交前调用 `publish_workorder_event`"。

### D12 长期工单系统关闭

- 新增 `close_standing_work_order_for_plan(session, *, plan_id, actor_account_id, reason)`：状态 `CANCELLED`、写 `closed_by/closed_note`、追加 `closed` 事件（`actor_role=ADMIN` 或发起医生，payload `{"reason": reason, "by": "plan_cancel"}`），**绕过** `close_work_order` 的双角色 `handled` 门禁。
- `close_work_order` 保留并加保护：`order_kind=standing` 且计划仍为 `planned/active/paused` → 409。
- 系统关闭同样 commit 后推送 `ticket.resolved`。

### D13 迁移 `0008` 最终清单

1. `medication_plans` 增加：`created_by_account_id`、`updated_by_account_id`、`confirmed_at`、`confirmed_by_account_id`、`workorder_id`、`internal_note`。
2. 新建 `medication_orders`（D1）。
3. 新建 `medication_events`（D2）。
4. `workorders` 增加：`order_kind`（`one_off/standing`，默认 `one_off`）、`medication_plan_id`、`medication_order_id`、`effective_from`、`effective_until`、`dedupe_key VARCHAR(160)`。
5. 新建 `workorder_reads`（D9）。
6. `ck_workorder_events_type` 重建为：`created/read/disposition/closed/occurrence/confirmed/revised`。
7. 索引/唯一键：
   - `uq_medication_orders_plan_scheduled (plan_id, scheduled_at)`
   - `uq_medication_events_dedupe_key (dedupe_key) WHERE dedupe_key IS NOT NULL`
   - `uq_workorders_dedupe_key (dedupe_key) WHERE dedupe_key IS NOT NULL`
   - `ix_medication_orders_elder_scheduled`、`ix_medication_orders_status_scheduled`
   - `ix_medication_events_order_created`、`ix_medication_events_plan_created`
   - `ix_workorders_medication_plan`、`ix_medication_plans_workorder`
8. 同步 ORM；`downgrade` 精确回退本迁移新增内容。
9. **不改** `ck_workorders_source`、`ck_workorder_events_actor_role`、`ck_workorder_dispositions_role`。
10. `0009_media_history` 不变。

---

## 三、`docs/Day7-改造方案.md` 改写清单

### 3.1 §0 结论速览
- 交付物追加："服药任务单与事实事件（`medication_orders` + `medication_events`）、家属完整服药历史、工单四端实时与按账号已读"。
- 拆段数保持 8 段（新增段 1.5），同步 §4.1 总览与 §6 顺序。

### 3.2 §1.2 口径冲突表
新增 7 行：
- 已读粒度（角色级 vs 账号级）→ `workorder_reads`。
- 管理员实时（广播算了、接入没放行）→ 四端 WS。
- 家属实时/轮询与多长者 → 选择器 + 当前长者刷新。
- 漏服建单函数缺失 → 段 1 落地，段 3/4 只调用。
- 服药事实无落点 → 双表替代。
- 推送早于提交 → commit 后发布。
- 列表缺 `type/title` 与筛选契约 → 补齐字段与 `status/mine`。

### 3.3 §1.3 边界
- "Day7 允许做"增加：
  - 新增 `medication_orders`、`medication_events`、`workorder_reads` 三张表；
  - 新增 `app/models/medication_order.py`、`app/services/medication_order.py`、`app/schemas/medication_order.py`；
  - 修改 `workorder_events.py`、`routes/workorders.py` 的 WS 鉴权、`workorders.js` 与四端页面；
  - 新增 `create_missed_medication_work_order`、`close_standing_work_order_for_plan`。
- "Day7 明确不做"把"消息通知、未读角标"改为："不做站外通知（短信/微信/推送）、运营看板、审批中心；**允许**工单 WS、轮询、按账号回执与服药历史"。
- 补一句：WS 路径沿用 `/api/v1/staff/ws`，仅命名历史遗留。

### 3.4 §1.4 已拍板口径
- **重写 T5**：删除"不新增 `medication_events`"；改为双表分工、唯一键、状态机、事实不可变、家属字段白名单。
- 改写 P3：管理员通过 WS 收 `ticket.*` 刷新信号并全量可见，但不发 counts、不做角标、不做站外通知。
- 改写 P4：家属实时只覆盖有效绑定长者；多长者用选择器；家属可见完整服药历史（只读、字段白名单）。
- 新增：
  - P7 按账号回执 `workorder_reads`，`handled` 仍按角色共享。
  - P8 四端 WS + 30 秒轮询；单进程假设。
  - P9 漏服工单 `dedupe_key` + 部分唯一索引；系统建单 `actor_role=None`。
  - P10 工单事件一律 commit 后发布；WS 仅刷新信号。
  - P11 列表补齐 `type/title` 与 `status/mine`。
  - P12 服药任务单按需创建 + 启动补漏，唯一键 `(plan_id, scheduled_at)`。
  - P13 服药事件只追加、不存原文；家属历史只读白名单。
  - P14 员工/家属只能 `skipped`，不能代替长者 `confirmed`。

### 3.5 §2 现状差距分析
- 第 22 行改为"三端 HTTP 可用；WS 仅 doctor/nurse；管理员/家属无实时"。
- 第 29 行改为"需专用系统关闭函数，普通关闭会被 handled 门禁阻塞"。
- 新增行：按账号已读、四端实时与轮询、漏服建单函数、列表 `type/title` 与筛选契约、**服药任务单/事实事件/家属历史**（均为未开始）。
- §3.1 前置阻塞补：段 4 需安装 `infra` 可选依赖；未安装时调度器不启动并记 `medication_scheduler_unavailable`。

### 3.6 §4.2 段 1
- 迁移清单改为 D13 的 10 条，给出完整字段与约束名。
- 新增领域服务 `medication_order.py`：
  - `ensure_order`、`mark_reminded`、`mark_confirmed`、`mark_denied_or_timeout`、`mark_skipped`、`cancel_orders_for_plan`、`list_family_history`。
- 新增 `create_missed_medication_work_order` 与 `close_standing_work_order_for_plan` 的签名、字段、事件、去重、调用点。
- Schema 增加 `type/title/is_read/read_at`；新增 `MedicationOrderFamilyView`、`MedicationEventFamilyView`。
- 长期工单创建后 commit 再 publish；写明禁止 commit 前 publish。
- 验收补：同一 `(plan_id, scheduled_at)` 只生成一条订单；重复调用建单只返回一张工单；计划取消后工单 `cancelled`。

### 3.7 新增 §4.3 段 1.5（原段 2 顺延）
"工单实时（四端）与按账号已读"：
- 后端文件：`app/models/workorder.py`、`app/services/workorder.py`、`app/services/workorder_events.py`、`app/api/routes/workorders.py`、`app/schemas/workorder.py`。
- 前端文件：`app/static/js/workorders.js`、`admin.html`、`family.html`、`nurse.html`、`doctor.html`。
- 步骤按 D7/D8/D9/D10 写死。
- 验收 6 条：管理员实时、家属实时且不越权、两名护士独立已读、`mine` 服务端生效、断线 30 秒轮询、token 到期关闭。

### 3.8 §4.4 段 2
- 家属端增加：
  - 工单长者选择器（`sessionStorage` 持久化 `kb_wo_elder`），切换立即刷新，无长者空态；
  - "用药计划（只读）"与"服药历史"时间线；按订单展示 `scheduled_at/status/attempt_count/events`；
  - 不显示 `internal_note`、`workorder_id`、跳过原因、账号 UUID。
- 员工端：类别列用 `type/title`；未读圆点用 `is_read`；筛选走 `status/mine`。
- 文案：admin/family 不出现"查看并处理"弹窗。

### 3.9 §4.5 段 3
- 图的 `thread_id` 改为 `medication:{order_id}`（不再用 plan+date+slot）。
- 图每次状态推进同步写 `medication_orders` 与 `medication_events`；**DB 订单状态是权威**，图状态只负责可恢复执行。
- 重试耗尽调用段 1 的建单函数；验收加"重复调用不新建"。
- 降级为单轮确认时也必须写订单与事件，不得只回话术。

### 3.10 §4.6 段 4
- 写明 `apscheduler` 属 `infra` 可选依赖、安装命令、未安装降级。
- 扫描逻辑改为 D4：`ensure_order` + 唯一键；启动补漏；不再以进程内缓存为唯一去重。
- 无设备/超时耗尽统一调用段 1 建单函数；commit 后 publish。
- 明确单实例；多实例唯一键防重复订单但不防重复外呼。

### 3.11 §5 风险 / §8 验收 / §9 不做
- §5 新增风险：双表数据量、时区换算、重复外呼、家属误读历史、token 过期连接、多 worker 事件丢失、推送早于提交。
- §8 新增：`alembic upgrade head`、`pytest tests/test_medication_order.py tests/test_workorder_api.py`、两护士双浏览器已读对照、管理员/家属双开实时对照、家属历史不含敏感字段。
- §9 与 §1.3 一致，删除"未读角标"的一刀切，改为"不做站外通知与运营角标"。

---

## 四、`docs/Day7-改造提示词.md` 改写清单

### 4.1 全局禁止清单
- 删除"不得新建 `medication_orders` / `medication_events`"，改为："**允许**新增 `medication_orders`、`medication_events`、`workorder_reads`；仍不得新建 `nurse_elder_assignments`"。
- 原"不得做消息通知、未读角标"改为："不得做站外通知（短信/微信/推送）、运营看板、审批中心；允许工单 WS、30 秒轮询、按账号回执与服药历史"。
- 新增硬约束："禁止在数据库事务提交前调用 `publish_workorder_event`"。

### 4.2 段 1
- 迁移清单替换为 D13 的 10 条，明确约束名、唯一键与部分索引。
- 新增 `medication_orders` / `medication_events` 的 DDL、状态枚举、事件枚举、`dedupe_key` 规则。
- 新增 `app/models/medication_order.py`、`app/services/medication_order.py`、`app/schemas/medication_order.py` 的职责与函数签名。
- Schema 增加 `type/title/is_read/read_at`；新增家属历史字段白名单。
- 新增 `create_missed_medication_work_order` 完整签名与 D5 逻辑；写明 `actor_role=None`，禁止 `system`。
- 新增 `close_standing_work_order_for_plan`；写明与 `close_work_order` 的分工。
- 验收增加：同一 `(plan_id, scheduled_at)` 只一条订单；家属历史不含 `internal_note`/`workorder_id`/`device_id`；commit 后才有 WS 事件。

### 4.3 新增段 1.5
按 D7/D8/D9/D10 写成可执行步骤，必须包含：
1. `WS_ROLES` 常量与四端鉴权；
2. `_resolve_recipients` 家属解析（账号启用 + 绑定有效 + 未过期）；
3. `_dispatch` 仅 staff 发 counts；
4. `workorder_reads` 写入与 `is_read/read_at` 返回；
5. `count_open_by_role` 按账号；
6. 前端四端连接、心跳、30 秒轮询、家属选择器、`status/mine` 服务端筛选；
7. 验收 6 条。

### 4.4 段 2
- 删除"不改后端 API"的绝对表述（家属 `status` 与服药历史接口属于段 1 后端改动）。
- 家属端补选择器、空态、切换刷新、服药历史时间线；员工端补 `is_read` 圆点与 `mine` 筛选。
- 明确 admin/family 只 toast，不弹"填写处理方案"。

### 4.5 段 3
- `thread_id` 改为 `medication:{order_id}`。
- 图状态推进必须同步写订单与事件；DB 订单是权威，图状态只做恢复。
- 删除自建漏服单步骤，改为调用段 1 函数；补去重验收。
- 单轮确认降级也必须落订单与事件。

### 4.6 段 4
- 补 `infra` 依赖安装与未安装降级；
- 补 `ensure_order` + 唯一键 + 启动补漏；
- 补 commit 后 publish；
- 补单实例限制；
- 保留"无设备也要建单，不静默跳过"。

### 4.7 段间冲突检查表
新增四行：
- `app/services/workorder.py`：段 1 改 `create_*`/`close_*`，段 1.5 改 `mark_read`/`count_open_by_role`。
- `app/api/routes/workorders.py`：段 1 改列表 schema 与家属 `status`，段 1.5 改 WS 鉴权。
- `app/static/js/workorders.js`：段 1.5 改连接/轮询/已读，段 2 改用药展示与筛选。
- `app/services/medication_order.py`：段 1 建服务，段 3/4 只调用，禁止重写。

### 4.8 文末"可行性自检清单"
逐条对应 U1–U4、N1–N19，每条给检查命令，例如：
- `grep -rn "missed_medication" app/` 必须命中函数与两个调用点；
- `grep -n "ck_workorder_events_type" alembic/versions/0008_*.py` 必须命中重建；
- `grep -n "medication_orders" alembic/versions/0008_*.py` 必须命中建表；
- `grep -n "role not in _STAFF_ROLES" app/api/routes/workorders.py` 必须已替换为 `WS_ROLES`；
- `grep -rn "medication_events" app/services/` 必须只有 `medication_order.py` 写入。

---

## 五、提示词技术可行性与项目适配度结论

| 现有提示词表述 | 可行性 | 与当前项目的适配问题 | 本次改写 |
| --- | --- | --- | --- |
| "不得新建 `medication_orders`/`medication_events`" | 不可行 | "已服药"没有持久化落点；图状态/日志不可查询，家属历史无法实现 | 改为双表，并给 DDL、状态机、唯一键 |
| "沿用现有广播，护工端能实时刷新" | 部分可行 | 只有 doctor/nurse 能连 WS；管理员广播分支是死代码；家属无入口 | 四端 WS + 30 秒轮询 |
| "不做消息通知、未读角标" | 与需求冲突 | 与"管理员/家属实时""按账号已读"矛盾 | 收窄为"不做站外通知与运营看板" |
| "不新建表" | 不可行 | 与 `workorder_reads`、双表冲突 | 改为"允许三张新表" |
| "扩事件类型" | 可行但表述不足 | 未列全 `confirmed/revised`，未给约束名 | 明确 7 种类型与约束重建 |
| "漏服时创建工单" | 当前不可行 | 函数、字段、去重、调用点全缺 | 给签名、`dedupe_key`、唯一索引与两个调用点 |
| "系统关闭长期工单" | 当前不可行 | 普通关闭被 handled 门禁阻塞 | 新增专用系统关闭函数 |
| "APScheduler 已装/到点外呼" | 部分不可行 | `apscheduler` 在 `infra` 可选依赖，lifespan 未接入 | 写安装前置、lifespan 接入、启动补漏、单实例 |
| "写入当日记录" | 不可行 | 没有"当日记录"表 | 明确写 `medication_orders` + `medication_events` |
| "家属刷新后可见" | 不满足验收 | 无实时、无轮询、无历史接口 | 四端实时 + 轮询 + 家属历史接口 |
| "家属状态筛选" | 当前不可行 | 家属接口写死 `status_filter=None` | 段 1 补 `status` 参数，段 2 服务端筛选 |
| "沿用 `notifyCreated` 弹窗" | 不适配 | 文案与按钮面向可处理员工 | staff 弹窗，admin/family 仅 toast |

结论：提示词目标可行，但**不能按现有文字直接施工**；必须先写死"双表落点、迁移约束、鉴权范围、广播范围、已读粒度、发布时机、依赖安装"七类前置条件。

---

## 六、两份文档的交叉一致性约束

改写完成后，两份文档必须对以下标识符给出一致定义（逐字一致）：

| 标识符 | 唯一口径 |
| --- | --- |
| `WS_ROLES` | `{doctor, nurse, admin, family}`，唯一鉴权与广播常量 |
| `workorder_reads` | `(workorder_id, account_id)` 主键，`read_at` |
| `is_read` / `read_at` | 列表项字段；admin/family 为 `None` |
| `mine` | `unread/read/handled`，仅 staff |
| `medication_orders` | `(plan_id, scheduled_at)` 唯一；状态 `pending/calling/confirmed/missed/skipped/cancelled` |
| `medication_events` | 只追加；类型 `created/reminded/confirmed/denied/timeout/missed/skipped/cancelled` |
| `dedupe_key` | 事件 `reminded:{order_id}:{attempt}`；工单 `missed:{order_id}` |
| `ensure_order` | 唯一任务生成入口；`ON CONFLICT DO NOTHING` |
| `create_missed_medication_work_order` | 唯一漏服建单函数 |
| `close_standing_work_order_for_plan` | 唯一系统关闭函数，状态 `cancelled` |
| 工单事件类型 | `created/read/disposition/closed/occurrence/confirmed/revised` |
| 工单类型 | `sensitive` / `medication_plan` / `missed_medication` |
| 家属历史 | `GET /api/v1/family/elders/{elder_id}/medication-history`，字段白名单 |
| 发布时机 | 一律 commit 后，禁止事务内发布 |

禁止再出现的表述：
- "不新建表"（与三张新表冲突）
- "不新增 `medication_events`"（与双表冲突）
- "写入当日记录"（无此表）
- "不做通知/未读角标"（与四端实时、按账号已读冲突）
- "沿用现有广播，护工端能实时刷新"（当前只有护士能连）
- `actor_role="system"`（CHECK 不允许）

---

## 七、验证步骤（文档级，不跑代码测试）

1. `git diff --stat` 只出现 `docs/Day7-改造方案.md` 与 `docs/Day7-改造提示词.md`。
2. 交叉 grep：
   - `medication_orders`、`medication_events`、`workorder_reads`、`ensure_order`、`dedupe_key`、`WS_ROLES`、`is_read`、`mine`、`close_standing_work_order_for_plan`、`create_missed_medication_work_order` 在两份文档中均出现且定义一致；
   - `0008` 条目数一致（10 条）；
   - 段号连续：段 1 → 段 1.5 → 段 2 → … → 段 7。
3. 矛盾词检查：不得出现"不新建表""不新增 `medication_events`""写入当日记录""沿用现有广播"。
4. 事实核对：引用的现有函数名、表名、约束名、角色名与仓库一致（`close_work_order`、`mark_read`、`_resolve_recipients`、`_payload`、`ck_workorder_events_type`、`ck_workorders_source`、`ck_workorder_events_actor_role`、`STAFF_ROLES`）。
5. 可选回归（不改代码，仅确认基线）：`pytest tests/test_workorder_api.py tests/test_workorder_service.py -q` 保持通过。

---

## 八、假设与边界

- Day7 部署为**单 uvicorn 进程**；多副本需 Redis 广播；唯一键防重复订单，但不防重复外呼。
- WS 路径保持 `/api/v1/staff/ws`，文档注明历史命名。
- 管理员/家属不写工单已读回执、不展示已读状态、不收 counts。
- 处理项 `handled` 继续按角色共享，作为关闭门禁；已读按账号独立。
- 家属实时仅覆盖有效绑定长者；多长者用选择器，不做聚合看板。
- 服药事实双表：`medication_orders` 是当前态，`medication_events` 是追加事实；`workorders` 只负责漏服派单。
- 家属只能看服药历史，不能代替长者确认；员工/家属只能标记 `skipped`。
- 所有时间按 `settings.app_timezone`（Asia/Shanghai）计算后存 `TIMESTAMPTZ`；文档写明时区换算规则。
- 不新增站外通知渠道，不回改已执行迁移，不改 Day6 路由层。
