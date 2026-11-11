# Day7 段1.5 执行计划：工单四端实时 + 30 秒轮询 + 按账号已读

## Summary

核实项目进度后执行 Day7 段1.5：把工单事件通道从「仅医生/护士」扩到四端（doctor/nurse/admin/family），补 30 秒轮询兜底、按账号已读回执（workorder_reads）、服务端 status/mine 筛选，并修复三个已知问题：管理员推送死代码、家属无实时、护士已读角色级。只做工单实时与已读，不改用药业务逻辑，不新增迁移。

## 进度核实（已确认）

- 最新提交 `b6a45d9` = Day7 段1（用药计划闭环 + 服药双表），工作区干净。
- 段1 产物已就位：`WorkorderRead` 模型（app/models/workorder.py:138，联合主键 workorder_id+account_id）、`WorkorderListItem` 已预留 `type/title/is_read/read_at` 字段（app/schemas/workorder.py:27-47）、迁移 `0008` 已含 workorder_reads 表、`medication.py` 路由已实现「commit 后发布」时序。
- 段1.5 未开始：`WS_ROLES` 不存在、`tests/test_workorder_realtime.py` 与 `tests/test_workorder_reads.py` 不存在、`workorder.py` 服务层仍在事务内调用 `_publish_event`（5 处）、WS 鉴权仍是 `_STAFF_ROLES`（app/api/routes/workorders.py:63,511）。

## Proposed Changes

### 1. app/services/workorder_events.py

- **定义并导出 `WS_ROLES = frozenset({AccountRole.DOCTOR, NURSE, ADMIN, FAMILY})`**；`STAFF_ROLES` 继续从 workorder.py 导入使用（是否算 counts/处理项的唯一依据）。
- **`_resolve_recipients`**：在现有 doctor/nurse/admin 解析后追加家属解析——按工单 `elder_id` 查 `FamilyElderLink`（`status=active` 且 `valid_until IS NULL OR valid_until > now()`）→ `accounts.role=family`、`status=active`；不满足跳过，不缓存。
- **`_payload`**：data 增加 `"type": workorder.type`；仍不返回 description/context_snapshot/方案全文/长者原文。admin/family 的 `unhandled` 固定 0。
- **`_dispatch` 分组**：
  - staff（doctor/nurse）：照旧计算 `unhandled`（新口径，见下）并补发 `ticket.counts`；
  - admin/family：只发 `ticket.created/updated/resolved` 刷新信号，不发 `ticket.counts`。
- **`WorkorderEventHub` 连接改为有序**：`_connections: dict[UUID, list]`（register 追加、unregister 移除、connections_for 返回副本），新增 `register` 返回被挤出的最旧连接（供路由关闭），支撑「单账号上限 3」。

### 2. app/services/workorder.py

- **删除 `_publish_event` 及 5 处调用**（行 278/325/635/685/755，对应 ticket.updated/created/updated/updated/resolved）。发布职责上移到路由层（commit 后）与 voice.py。
- **`mark_read` 改造**：
  1. 先 `INSERT INTO workorder_reads (workorder_id, account_id, read_at) ... ON CONFLICT DO NOTHING`（用 `sqlalchemy.dialects.postgresql.insert`，`read_at` 取 `_now()`，仅当新插入时记 `workorder_read_receipt` 日志与 read 审计事件）；
  2. 保留处理项 `unread→read`、`OPEN→ACKNOWLEDGED` 既有状态机；disposition 已 `READ`（他人所置）或 `HANDLED` 时不再改处理项、不回退；
  3. 已读展示不再取处理项 `account_id`。
- **`count_open_by_role` 新口径**：「该账号未读且角色未 handled」——在现有条件上追加 `NOT EXISTS (SELECT 1 FROM workorder_reads r WHERE r.workorder_id=workorder.workorder_id AND r.account_id=:account_id)`；非 staff 仍返回 0。
- **`list_work_orders` 增加 `mine: str | None` 参数**（仅 staff 语义）：
  - `unread`：无本人回执 且 本人角色处理项 != handled；
  - `read`：有本人回执 且 本人角色处理项 != handled；
  - `handled`：本人角色处理项 == handled；
  - 其他值忽略（或 raise WorkOrderValidationError → 422，取 422 与路由校验一致）。
- **新增 `list_receipts(session, account_id, workorder_ids) -> dict[UUID, datetime]`**：批量查当前账号回执，供路由填充 `is_read/read_at`。

### 3. app/api/routes/workorders.py

- **鉴权**：删除本地 `_STAFF_ROLES`，WS 与广播共用 `WS_ROLES`（从 workorder_events 导入）；非四端角色 4403、账号非 active 4401（现有行为保留）。HTTP 端点的 `require_role`/`require_admin` 权限不变（family 的 mine=422 在路由参数层校验）。
- **发布时机**：`staff_mark_read`/`staff_submit_disposition`/`admin_close_workorder` 在 `session.commit()` 且 `_reload_after_write` 之后调用 `publish_workorder_event("ticket.updated"/"ticket.resolved", workorder)`（不传 session，走新会话解析收件人）。发布异常不冒泡（publish 内部已兜底）。
- **列表契约**：
  - staff 列表新增 `mine` Query 参数，透传 `svc.list_work_orders`；admin/family 传 `mine` → 422；
  - family 列表 `status_filter` 不再写死 None，透传查询参数 `status`。
- **`_item_from`**：新增参数 `receipts: dict[UUID, datetime]`；staff 填 `is_read=bool`/`read_at`；admin/family 恒为 None。列表与详情组装处用 `svc.list_receipts` 批量取。`_detail_from`/`_family_detail_from` 随之透传。
- **WS 生命周期**：
  - 首帧 auth 成功后记录 `account_id/role/exp`；接收循环用 `asyncio.wait_for(ws.receive(), timeout=exp剩余秒数)`，超时 `close(4401)` 并记 `workorder_ws_expired`；
  - 单账号连接上限 3：auth 成功后 `hub.register` 返回被挤出连接则 `close(4409)`，记 `workorder_ws_limit_exceeded`；
  - ping/pong 保留；日志事件名改为 `workorder_ws_connected/disconnected`。

### 4. app/services/voice.py（仅推送时机）

- `_create_work_order`：`create_sensitive_work_order` 不再内部推送；在 `await db.commit()` 之后 `await db.refresh(workorder)`（避免 commit 后属性过期触发 async 惰性加载错误），然后 `publish_workorder_event("ticket.created" if created else "ticket.updated", workorder)`（不传 session），失败降级为日志。

### 5. app/schemas/workorder.py

- 无需改动：`type/title/is_read/read_at` 段1 已预留。仅核对不改。

### 6. 前端 app/static/js/workorders.js（扩展，不重写整体结构）

- **四端连接**：`var WS_ALLOWED = role in {doctor:1,nurse:1,admin:1,family:1}`；`connectWs()` 守卫从 `isStaff` 改为该集合；admin/family 面板挂载时即连接（admin.html 懒挂载、family.html 直接挂载，均满足「挂载即连接」）。
- **消息处理分组**：staff 收 `ticket.counts` 更新角标；四端收 `ticket.created/updated/resolved` 都 `refresh()`（admin 刷全量列表、family 刷当前选中长者列表）；staff 保留 `notifyCreated` 弹窗，admin/family 不弹窗不角标。
- **心跳与轮询**：`onopen` 后每 25 秒发 `{"type":"ping"}`；记录最近 pong 时间，超过 60 秒无上行/无响应主动 `ws.close()`；`onclose/onerror/心跳超时` → `startPolling()`：staff 每 30 秒「列表 + counts」，admin「列表」，family「当前选中长者列表」（未选长者则跳过）；轮询期间每 30 秒尝试重连一次，`onopen` 成功即 `stopPolling()`；console 记 `workorder_poll_fallback_started/stopped`（客户端无结构化日志通道，属已知限制）。
- **服务端筛选**：删除 `filteredItems()`；staff 状态下拉改为 `mine=unread/read/handled`（未读/已读/已处理），admin/family 增加状态下拉 `status=open/acknowledged/in_progress/resolved/cancelled`；筛选变更重置 page=1 并 `refresh()`；翻页提示改为「共 N 条」。
- **家属长者选择器**：选项来自 `GET /api/v1/elders`（家属按绑定收敛，接口已存在）；选中值存 `sessionStorage["kb_wo_elder"]` 并在挂载时恢复；**无选中长者显示空态「请选择长者查看工单与用药记录」且不发请求**；不自动选第一个；切换时清空列表、保存、重新拉取（WS 只作刷新信号，切换后主动拉一次）。

### 7. admin.html / family.html / nurse.html / doctor.html

- 预期无需改动（admin 懒挂载 + family 直接挂载已满足连接时机；页面仅调 `KBWorkOrders.mount`）。若实现中发现 `data-ws` 提示条对 admin/family 需展示，则在 workorders.js 内调整（面板骨架属 js 文件职责）。

### 8. 新增测试

**tests/test_workorder_reads.py**（沿用 conftest 的 `api/db_session/seed/login` 事务回滚模式）：
1. 两名护士分别已读：A 已读后 B 列表仍 `is_read=False`，B 已读后为 True；`workorder_reads` 两行；disposition 均为 read；
2. handled 后 mark_read 幂等：不回退 my_status=handled，回执仍可插入；
3. `count_open_by_role` 新口径：本人已读后本人 counts=0，另一护士 counts 仍为 1；
4. `mine=unread|read|handled` 服务端筛选结果正确；
5. admin/family 传 `mine` → 422；
6. family 列表 `status` 透传生效（resolved 只出 resolved）；
7. 列表项含 `type/title`，staff 的 `is_read/read_at` 有值、family 为 null。

**tests/test_workorder_realtime.py**：
1. WS 鉴权：doctor/nurse/admin/family 四端首帧 auth 均可连接；device token 4403；停用账号 4401；无效 token 4401；
2. 广播分组（用 fake connection 注册进 hub，直接调 `publish_workorder_event`）：staff 收主事件 + `ticket.counts`；admin 收刷新信号、无 counts；family 仅收其绑定长者的工单事件、未绑定长者不收；
3. `_resolve_recipients`：含有效绑定的家属、不含过期/失效绑定；
4. hub 连接上限：第 4 个连接挤出最旧的（register 返回挤出连接）；
5. WS 过期：monkeypatch `settings.jwt_access_token_expire_minutes` 极小值 → 连接很快被 4401 关闭（若不稳定则降级为手工验收项并记录）。

## Assumptions & Decisions

- **已读按账号、处理按角色**写死：`handled` 仍是角色级、仍是关闭门禁，不允许已读替代处理。
- handled 状态下 mark_read 仍插入回执（用户确实查看过），但不改动处理项——幂等且不回退。
- 连接上限挤出用自定义 close code 4409（规范未指定，文档写明）。
- 发布一律「commit 之后」：workorder.py 服务层删推送、路由层与 voice.py 在 commit+refresh 后发布；`medication.py` 段1 已符合，不动。
- 客户端轮询兜底日志只有 console（无服务端结构化通道），列为已知限制。
- 遵守约束：不新增迁移、不改 `0008`、不改 `app/models/**`、不 commit/push、不引入前端框架、不做角标/站外通知。

## Verification

1. `python -m pytest tests/test_workorder_reads.py tests/test_workorder_realtime.py tests/test_workorder_service.py tests/test_workorder_api.py -q` 全过。
2. `ruff check .` 通过。
3. 手工验收（对照段1.5 验收标准 1/2/5/6）：管理员/家属实时刷新、断线 30 秒内转轮询并恢复重连、JWT 过期 4401 转轮询获 401。
4. 交付说明按提示词「完成后请说明」八项输出。
