# Day7 段2 任务清单

> 依据 `spec.md`；仅前端改动，禁止 commit/push。

## T1 新增 `app/static/js/medication.js`

- [x] IIFE + `window.KBMedication = { openDoctorPlans, mountFamily }`
- [x] 公共工具：`fmtDate` / `fmtTime` / `weekdayText` / `scheduleText` / 状态与事件中文映射
- [x] 医生列表弹层：状态筛选、表格、工单号短码、空态、403/404 无权限（不渲染新建）
- [x] 新建表单：多时间增删、星期多选默认全选、日期校验、就地错误
- [x] 确认页：全字段 + 确认后下发警示 + 返回修改 / 确认下发
- [x] 编辑差异确认页：仅变化字段「原值 → 新值」→ 确认后 PUT
- [x] 暂停/恢复/取消二次确认（取消提示关闭长期工单）
- [x] 家属只读计划 + 修改记录时间线（`schedule` 格式化、`internal_note` 过滤）
- [x] 家属服药历史：每页 30、加载更多、事件展开、字段白名单
- [x] 复用 `kb_wo_elder` 与 `kb:wo-elder` 事件；未选长者不发请求

## T2 修改 `app/static/doctor.html`

- [x] 删除「用药方案管理（后续版本）」占位卡
- [x] 长者卡片增加「用药计划」按钮（阻止冒泡）
- [x] 引入 `/js/medication.js`

## T3 修改 `app/static/family.html`

- [x] `#workorders` 后新增 `#family-medication` 区块
- [x] 引入 `medication.js` 并在工单面板挂载后调用 `mountFamily`

## T4 修改 `app/static/js/workorders.js`

- [x] `TYPE_LABEL` + 类别列 `category || TYPE_LABEL[type]`
- [x] `order_kind === "standing"` 列表/详情「长期用药」徽标（类别/标题已含该词时不重复挂）
- [x] 员工未读圆点改用 `is_read === false`
- [x] 详情增加工单类型与生效期
- [x] 管理员长期工单隐藏关闭入口并提示
- [x] admin/family 实时事件只 toast + 刷新，不弹员工弹窗
- [x] 长者选择变更派发 `kb:wo-elder`；暴露 `getElderId()`

## T5 验证

- [x] `ruff check app tests`
- [x] `python -m pytest --tb=no -q --disable-warnings`
- [x] 浏览器四角色走查（医生新建/确认/修改/暂停恢复取消；家属只读+历史；护工未读圆点+standing；管理员关闭禁用+toast）
- [x] 控制台无未捕获异常；刷新后数据正确

## 完成标准

- 验收标准 1-7 全部满足（见 `spec.md` 第 4 节）
- 无后端/迁移/实时模块改动
- 工作区仅上述 4 个文件（1 新增 3 修改）有变化
