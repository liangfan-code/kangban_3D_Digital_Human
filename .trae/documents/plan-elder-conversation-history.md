# 长者端对话历史可视化改造方案（全新实现）

> 记录日期：2026-11-13
> 范围确认：对话历史可见 / 可存校验 / 按 thread 分组 / **新建对话 + 旧对话可切回继续聊（新旧同时可见、均可对话）** + 声纹解锁门禁 + 数据治理（保留期/访问审计）+ 设备 token 90 天续期继承分析
> 实现约束：**全新实现，不借鉴 stash@{0} 已回退代码**（旧版有严重漏洞导致整体崩溃）；本地库 `kangban_dev`，alembic 当前 `0009_media_history`

## 〇、核心需求语义（用户已确认）

1. **新建对话**：长者可随时开启新会话。
2. **同时可见**：新对话与旧对话在历史列表中并列展示，不隐藏、不归档消失。
3. **均可对话**：旧会话不是只读历史——点击「继续对话」可切回旧会话继续聊；同一时刻设备上只有一条「进行中」会话（当前会话），切换通过列表完成，下一轮语音/文字自动落入所选会话。
4. **记忆共享**：所有会话共享该长者的长期记忆（memory_chunks 按 elder_id 归属，天然成立）。

**支撑事实（已核实）**：WS 每轮对话都调用 `_load_context` 重新选择最近一条 ACTIVE 会话（`app/services/voice.py#L1632-1648`、文字通道 `#L598`）→「切换会话」只需 REST 把目标会话置为 ACTIVE，**无需改动 WS 协议**；SessionState 按 thread_id 独立存 Redis，切换互不污染。

**会话状态机（复用现有枚举，不新增状态）**：

| 状态 | 语义 | 转换 |
| --- | --- | --- |
| ACTIVE | 进行中（同设备+长者至多一条） | 新建/继续时置入；被新对话/切换挤下时转 ENDED |
| ENDED | 已收起，**可继续** | activate → ACTIVE（ended_at 清空） |
| ABANDONED | 已关闭，只读不可恢复 | 用户显式关闭或治理清理；列表默认不显示 |

现有 `get_or_create_active_conversation` 的「最近一条 ACTIVE」复用逻辑（`conversation_store.py#L25-77`）与此状态机完全兼容，零改动。

## 一、现状分析（已核实代码）

| # | 事实 | 证据 |
| --- | --- | --- |
| 1 | 对话**已全量落库**（user 转写 + assistant 回复正文），「可存」不缺 | `app/services/conversation_store.py#L80-93` record_turn |
| 2 | 会话表结构齐全（thread_id PK、elder_id、device_id、session_seq、status、started_at、ended_at），status 枚举含 ENDED/ABANDONED | `app/models/conversation.py#L26-70` |
| 3 | **没有任何代码把会话置为 ENDED**，同设备同长者永远复用同一条 ACTIVE 会话 → 分组无从谈起 | `conversation_store.py#L25-77` 只查/新建 ACTIVE |
| 4 | **没有任何 API 读取历史消息** → 前端无展示入口 | 全仓库无 conversations 路由 |
| 5 | 记忆 `memory_chunks` 只按 `elder_id` 归属，无 thread_id → **跨会话记忆共享天然成立** | `app/models/memory.py#L21-63` |
| 6 | 记忆萃取游标 `memory_upto_seq` 只存 Redis（TTL 24h）→ 旧会话重开会从 0 重扫、**重复萃取** | `app/services/session_state.py#L61-89` |
| 7 | 落库失败不可见：record_turn 异常只被吞掉，无记录 | `conversation_store.py#L80-93` |
| 8 | 设备 token 90 天（`device_token_expire_days`），续期 = 管理员签发一次性注册凭证 → 平板 `POST /api/v1/auth/device/register` 换发，**device_id 不变** | `app/core/config.py#L149-152`、`app/auth/device_token.py#L83-118`、`app/services/device.py#L105-139` |
| 9 | 声纹首句门控与 300s 临时 token 已存在（CAMPPlus Provider、voiceprints 表） | `app/services/voice.py`（首句门控段） |
| 10 | 长者端页面：`elder.html`（设备/长者卡片）、`voice.html`（语音对话入口），均无历史 UI | `app/static/elder.html#L82-133`、`voice.html#L43-63` |

**token 续期继承结论（隐藏问题分析）**：
- 同设备续期：device_id 不变 → conversations / memory_chunks 均按 device_id / elder_id 归属，**记忆与历史完整继承，无丢失风险**。
- 真正的继承风险在两处：①**换设备**（新 device_id）后旧会话挂在旧设备名下 → 方案必须按 **elder_id 维度**聚合会话列表（而非 device_id），换设备验证身份后历史照常可见；②记忆游标只在 Redis → 须落库到 conversations 列。
- token 过期本身只导致 401 → 引导回生物识别注册页重新换发，不触碰数据。

## 二、旧版崩溃教训（本次必须遵守的护栏）

1. **每段完成即跑全量 pytest + uvicorn 启动冒烟**（旧版为多段叠加后才发现崩溃）。
2. **不引入任何新依赖**（无新 pip 包；Redis 为现有依赖）。
3. `app/services/voice.py`（高风险文件）只做最小改动且逐处人工审查；涉及 WS 回调时序的改动必须在 `createAnswer` 前显式完成（AGENTS.md 实时规则）。
4. AI 生成后 `app/api/routes/**`、`alembic/versions/**` 由用户审查 diff 后再进入下一段。

## 三、分段实施计划（每段：TDD → 自审 → 用户审阅 → 下一段）

### 段 1：会话分区基础（迁移 0010 + store 扩展）

- 新迁移 `alembic/versions/0010_conversation_partition.py`：
  - `conversations` 增加 `memory_upto_seq INTEGER NOT NULL DEFAULT 0`（游标落库）；
  - 新表 `conversation_write_failures`（thread_id、role、seq、error_type、detail、created_at），落库失败可见。
- `app/services/conversation_store.py` 新增（全新代码）：
  - `start_new_conversation(db, device_id, elder_id, identity_source)` → 旧 ACTIVE 置 ENDED，新建 ACTIVE 会话（session_seq 递增）；
  - `activate_conversation(db, thread_id, device_id, elder_id)` → 目标 ENDED 会话置回 ACTIVE（ended_at 清空），同设备同长者当前 ACTIVE 置 ENDED；ABANDONED 拒绝恢复（422）；
  - `end_active_conversation(db, thread_id)` → 收起当前会话（ACTIVE → ENDED）；
  - `list_conversations(db, elder_id, page)` → **按 elder_id 聚合、含 ACTIVE 与 ENDED**（ABANDONED 默认排除），返回 seq/状态/起止时间/消息数；
  - `list_messages(db, thread_id, before_seq, limit)` → 分页读消息（按 seq 倒序取、正序返回）；
  - `record_turn` 增加失败捕获 → 写 `conversation_write_failures`（不阻断通话主链路）。
- `app/services/voice.py`（最小改动）：萃取完成后把游标回写 `conversations.memory_upto_seq`（Redis 仍作会话内缓存，DB 为准）；**WS 协议与 `_load_context` 选会话逻辑零改动**（切换后每轮自动续上最近 ACTIVE）。
- 测试（TDD，先红后绿）：end/new/list/messages/游标回写/失败落表。
- **验证**：pytest 全量 + uvicorn 启动冒烟 + `alembic upgrade head`（kangban_dev）。

### 段 2：会话操作与历史读取 API + 声纹门禁

- 新路由 `app/api/routes/conversations.py`（长者端，设备 token 鉴权）：
  - `GET /api/v1/conversations` — 会话列表（elder_id 维度，ACTIVE+ENDED 并列可见，分页）；
  - `GET /api/v1/conversations/current` — 当前进行中会话；
  - `POST /api/v1/conversations` — **新建对话**（旧 ACTIVE 自动收起为 ENDED）；
  - `POST /api/v1/conversations/{thread_id}/activate` — **切回旧对话继续聊**（ENDED → ACTIVE，原 ACTIVE 收起）；
  - `POST /api/v1/conversations/current/end` — 收起当前对话；
  - `GET /api/v1/conversations/{thread_id}/messages` — 消息分页（ACTIVE/ENDED 均可读）。
- **声纹门禁**：以上接口要求「当前设备存在有效声纹确认 token」；未确认 → 403 + `identity_locked` 错误码；双人间只返回当前确认长者本人的会话。复用现有 voiceprint token 校验逻辑，不新建鉴权体系。
- `app/api/router.py` 注册路由。
- 测试：未门禁拒绝 / 门禁放行 / 双人间隔离 / 匿名线程不可见 / **新建→旧会话 ENDED→activate 切回→下一轮 WS 落入旧会话** 状态流转全链路。
- **验证**：同段 1。

### 段 3：记忆只读 API

- 新路由 `app/api/routes/memory.py`：
  - `GET /api/v1/elders/{elder_id}/memories` — 只读分页（visibility 过滤），医护/管理员可查；
  - 家属只读自己绑定长者（复用现有家属-长者鉴权）。
- `app/services/memory.py` 新增只读查询函数（不动写入/召回/维护算法）。
- 测试：角色权限、分页、visibility 过滤、家属越权拒绝。
- **验证**：同段 1。

### 段 4：数据治理（迁移 0011/0012 + 保留期 + 审计）

- 新迁移：
  - `0011_conversation_retention.py` — `conversations.expires_at`（NULL 永不清理）+ 归档表 `conversation_archives`；
  - `0012_conversation_audit.py` — `conversation_audit_logs`（actor、action、target_type、target_id、created_at）。
- `app/core/config.py`：`conversation_retention_days: int = 0`（0=不启用，默认关）。
- `app/services/conversation_retention.py`（新）：归档→逻辑下架→物理删除三步；挂到现有 APScheduler 实例（复用 medication_scheduler 的调度器，独立 job，不启用时零开销）。
- `app/services/audit.py`（新）：查看会话消息 / 记忆查询 / 管理员操作 写审计。
- `app/api/routes/conversation_admin.py`：管理员 dry-run 清理报表 + 审计查询。
- 测试：到期归档不误删未到期、审计写入、dry-run 无副作用。
- **验证**：同段 1。

### 段 5：前端接口对接（在后端契约冻结后进行）

> 交付顺序：段 1-4 后端全部完成并冻结「四、接口契约」→ 用户审查后端 diff → 才开始本段前端对接；前端只按契约调接口，不反向改后端。

- `app/static/voice.html`：对话页新增「历史对话」区
  - 未声纹确认 → 遮罩文案「请先说出您的名字确认身份」；
  - 确认后 → 会话列表（**新旧并列**：当前会话高亮「进行中」，收起会话显示「继续对话」按钮，按第 N 次对话 + 日期分组）→ 点击会话展开消息气泡（user/assistant 左右分列）；
  - 「开启新对话」按钮（调 new 接口 + 刷新列表）；「继续对话」按钮（调 activate + 刷新，之后说话自动落入该会话）；
  - 适老字号/大按钮，复用 `app/static/js/api.js` KB.api / KB.renderState / KB.esc，不改共享层语义。
- `app/static/elder.html`：加入口卡片跳转 voice.html 历史区。
- 产出 `docs/设备token续期与记忆继承分析.md`：续期链路图 + 换设备场景说明 + 运维清单（90 天前主动换发流程）。
- **验证**：`node --check` 改动的 js + pytest 全量 + 浏览器手测（登录 → 声纹 → 历史 → 新对话 → 切回旧对话继续聊 → 数字人引用先前记忆）。

## 四、接口契约（后端交付物，前端对接依据）

> 后端（段 1-4）先完成并冻结以下契约；前端（段 5）只依赖契约对接，不再反向改动后端。鉴权统一：设备 token（Bearer）+ 声纹确认门禁（见段 2）。

| 方法与路径 | 请求 | 响应 / 错误码 |
| --- | --- | --- |
| `GET /api/v1/conversations?page=` | 分页参数 | `{items:[{thread_id,session_seq,status,started_at,ended_at,message_count,is_current}],total}`；403 identity_locked |
| `GET /api/v1/conversations/current` | — | `{thread_id,session_seq,started_at}`；404 无进行中会话 |
| `POST /api/v1/conversations` | `{}` | 新建会话对象；旧 ACTIVE 自动转 ENDED；403 identity_locked |
| `POST /api/v1/conversations/{thread_id}/activate` | — | 会话对象；ENDED→ACTIVE、原 ACTIVE→ENDED；404 非本人会话、422 已 ABANDONED |
| `POST /api/v1/conversations/current/end` | — | 会话对象（status=ENDED）；404 无进行中会话 |
| `GET /api/v1/conversations/{thread_id}/messages?before_seq=&limit=` | 游标分页 | `{items:[{seq,role,content,created_at}],has_more}`；正序返回 |
| `GET /api/v1/elders/{elder_id}/memories?page=&visibility=` | 分页+过滤 | 医护/管理员/家属（本人绑定）只读；403 越权 |
| `POST /api/v1/admin/conversations/maintenance` | `{dry_run:bool}` | 清理/归档报表；仅管理员 |

错误码沿用现有 `KB.errText` 映射；所有响应含 `trace_id`。

## 五、关键设计决策（已定）

| 决策 | 结论 | 理由 |
| --- | --- | --- |
| 会话列表维度 | 按 elder_id 聚合 | 换设备/续期后历史仍可见（token 继承问题的根治）；匿名线程（elder_id=NULL）永不展示 |
| 旧会话语义 | ENDED = 已收起可继续，不归档只读 | 用户需求「新旧同时可见均可对话」；复用现有枚举不新增状态 |
| 切换会话机制 | REST activate 置 ACTIVE，WS 每轮自动续上 | `_load_context` 每轮重选最近 ACTIVE（voice.py#L1632-1648），WS 协议零改动 |
| 记忆是否随 thread 拆分 | 不拆，保持 elder_id 级共享 | 用户需求明确「对话之间记忆共享」；模型本就无 thread_id |
| 游标位置 | DB 列为准，Redis 作缓存 | 修复 24h 过期重萃取；迁移 0010 一列即可 |
| 声纹门禁实现 | 复用现有 300s voiceprint token + 首句门控 | 不新建鉴权体系，符合「不引入新依赖」 |
| 保留期默认 | `conversation_retention_days=0` 不启用 | 法务口径未定前零风险上线 |
| 旧 stash | 不 pop、不借鉴；段 5 完成且用户确认后建议 `git stash drop` | 用户明确要求全新实现 |

## 六、验收清单（整体）

1. 长者声纹确认后可在 voice.html 看到会话列表（新旧行并列）；未确认只见遮罩。
2. 「开启新对话」后旧会话转 ENDED、新会话 ACTIVE，两者均在列表中可见；新对话中数字人能引用先前记忆（记忆共享）。
3. 在列表点「继续对话」切回任一收起会话后，下一轮语音/文字自动落入该会话，上下文为该会话自己的历史；ABANDONED 会话拒绝恢复。
4. `conversation_messages` 全量保存校验：任一轮问答均有落库记录；落库失败时 `conversation_write_failures` 有记录且通话不中断。
5. 模拟 token 续期（换发新 access token）后：历史列表与记忆查询结果不变；模拟换设备验证后：按 elder_id 仍能看到全部历史并可继续对话。
6. 未启用保留期时系统行为与现状完全一致（回归）；启用后到期会话进归档表且审计留痕。
7. 全程不连接生产库；每段 pytest 全绿后才进入下一段。
