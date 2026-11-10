# Tasks

- [x] Task 1: 数据模型与迁移
  - [x] SubTask 1.1: `workorders` 表扩展：`source`（sensitive/manual）、`sensitive_category`、`occurrence_count`（默认 1）、`closed_by`、`closed_note`、`context_snapshot`（JSONB，存最近 5 轮对话 + 触发句 + 首条回复）
  - [x] SubTask 1.2: 新增 `workorder_dispositions`（`workorder_id`、`role`、`status`(unread/read/handled)、`account_id`、`read_at`、`plan_text`、`handled_at`），唯一约束 `(workorder_id, role)`
  - [x] SubTask 1.3: 新增 `workorder_events`（`workorder_id`、`event_type`、`actor_account_id`、`actor_role`、`payload`、`created_at`）用于审计时间线
  - [x] SubTask 1.4: 编写新 Alembic 迁移（不回改历史迁移），在本地 `kangban_dev` 上 `alembic upgrade head` 并人工审查 diff
  - 验证：`alembic current` 为最新版本；`pytest tests/test_models*` 通过

- [x] Task 2: 工单服务层
  - [x] SubTask 2.1: `app/services/workorder.py` 实现 `create_sensitive_work_order(elder_id, category, device_id, text)`，含去重（同老人+同类别+未关闭则累加 `occurrence_count`）
  - [x] SubTask 2.2: 实现 `mark_read(workorder_id, role, account_id)`、`submit_disposition(workorder_id, role, account_id, plan_text)`、`close(workorder_id, admin_account_id, note)`，含状态机与 409 语义
  - [x] SubTask 2.3: 实现未处理计数 `count_open_by_role(role)` 与按角色/老人查询的可见范围过滤
  - [x] SubTask 2.4: 实现上下文快照：从 `conversation_store` 取触发前最近 5 轮消息 + 触发句 + 首条安全回复，写入 `context_snapshot`；不足 5 轮按实际保存
  - [x] SubTask 2.5: 实现列表查询的 `q`（标题/类别/触发原句/方案文本）、`sort`（created_at/priority）、`order`、分页，并在 SQL 层叠加角色可见范围
  - [x] SubTask 2.6: 每次状态变更写 `workorder_events`，日志只记工单 ID/类别，不记老人原文
  - 验证：`pytest tests/test_workorder_service.py` 覆盖去重、双角色完成才能关闭、重复提交 409

- [x] Task 3: 敏感事件接入
  - [x] SubTask 3.1: 修改 `app/services/voice.py::_enqueue_sensitive`，优先建单；`elder_id` 为空时回退 HITL 队列并记录 `workorder_skipped_no_elder`
  - [x] SubTask 3.2: 保持老人端安全话术与 TTS 行为不变（安全回复不等待建单完成）
  - [x] SubTask 3.3: 补充回归测试：敏感命中后工单被创建、处理项为两条 unread
  - 依赖：Task 1、Task 2

- [x] Task 4: 员工端实时推送
  - [x] SubTask 4.1: 新增 `/api/v1/staff/ws`，首帧 `auth` 校验 Bearer/设备无关的账号令牌，仅 doctor/nurse 可连
  - [x] SubTask 4.2: 维护按老人的订阅关系（DoctorElderAssignment、BuildingAssignment），实现 `ticket.created / ticket.updated / ticket.resolved / ticket.counts` 事件
  - [x] SubTask 4.3: 鉴权失败/断线时前端回退轮询，不静默失败
  - 依赖：Task 2

- [x] Task 5: HTTP 接口
  - [x] SubTask 5.1: `GET /api/v1/staff/workorders`（列表，支持 `q`、`sort`、`order`、`page`、`status` 筛选）、`GET /{id}`（含对话快照）、`POST /{id}/read`、`POST /{id}/disposition`、`GET /api/v1/staff/workorders/counts`
  - [x] SubTask 5.2: `GET /api/v1/admin/workorders`（同样的搜索/排序/分页）、`POST /api/v1/admin/workorders/{id}/close`（仅双角色 handled 可关闭）
  - [x] SubTask 5.3: `GET /api/v1/family/elders/{elder_id}/workorders`（支持 `q`、`sort`、`order`、分页；校验 FamilyElderLink，越权返回 404；响应不含对话快照）
  - [x] SubTask 5.4: 三个路由组分别加 `require_role(DOCTOR, NURSE)` / `require_admin` / `require_role(FAMILY)`
  - 依赖：Task 2

- [x] Task 6: 前端页面
  - [x] SubTask 6.1: `doctor.html` / `nurse.html` 增加工单列表（未读圆点、三态、未处理角标、搜索框、时间排序、历史筛选）与详情（含对话快照）/方案提交
  - [x] SubTask 6.2: `admin.html` 增加工单列表（搜索、排序、历史）与终审关闭（未满足条件时禁用并提示缺谁）
  - [x] SubTask 6.3: `family.html` 增加只读工单列表（搜索、排序、历史）与方案全文，不展示对话快照
  - [x] SubTask 6.4: 员工端接入 WS 自动弹窗；断开时降级轮询
  - 依赖：Task 4、Task 5

- [x] Task 7: 联调与验收
  - [x] SubTask 7.1: 三端手工联调：老人说"我胸口疼，喘不上气" → 医生/护理弹窗 → 各自提交方案 → 管理员关闭 → 家属看到完整状态
  - [x] SubTask 7.2: 未识别老人身份时走 HITL 兜底，日志出现 `workorder_skipped_no_elder`
  - [x] SubTask 7.3: 验证关键词搜索、按时间排序、历史工单可查，且家属详情不含对话快照
  - [x] SubTask 7.4: 全量 `pytest` 通过；本次工单相关文件 `ruff` 通过（全仓库既有 lint 债务另记）
  - 依赖：Task 3、Task 6

# Task Dependencies

- Task 2 依赖 Task 1
- Task 3 依赖 Task 1、Task 2
- Task 4 依赖 Task 2
- Task 5 依赖 Task 2
- Task 6 依赖 Task 4、Task 5
- Task 7 依赖 Task 3、Task 6
- Task 4 与 Task 5 可并行；Task 5 完成后即可开始 Task 6 中 admin/family 部分
