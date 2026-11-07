# ER 数据模型设计（Day1 11 张基础表 + Day2 九张业务表）

> 版本：Day2 v2.0
> 范围：Day1 基线 11 表（迁移 0001/0002）；Day2 已落地 9 表（迁移 0003 账号与设备凭证、0004 授权关联、0005 健康档案五表）；其余业务表（TDD 6.3）仍列为候选，经评审后通过新迁移加入（ADR-013）。
> 字段与迁移保持一致：以 `alembic/versions/` 已人审迁移文件为权威，本文随新迁移同步更新。

## 1. 通用规范

- 数据库：PostgreSQL + pgvector；主键 UUID（`gen_random_uuid()` 数据库端生成）。
- 时间：`TIMESTAMPTZ`，统一 UTC 存储；`created_at` 非空且数据库默认生成；发生更新的表另有 `updated_at`（`onupdate`）。
- 状态/业务枚举：`VARCHAR + CHECK`，不使用 PostgreSQL 原生枚举。
- 个人数据默认 `RESTRICT` 删除；退房经显式匿名化/删除流程处理（后续阶段实现）。
- 向量列：`memory_chunks.embedding`、`voiceprints.embedding` 使用 pgvector `vector` 类型，**Day1 不指定维度、不建 ANN 索引**；Embedding 模型与维度确定后再由新迁移固定维度并补索引。

## 2. 表关系总览

```
rooms 1 ──< devices（room_id 非空）
rooms 1 ──< elders（room_id 可空，与 bed_no 成对）
personas 1 ──< elders（persona_id 非空）
elders 1 ──< memory_chunks / medication_plans / health_records / voiceprints / workorders（均非空 FK）
elders 0..1 ──< conversations（elder_id 可空，匿名会话允许空）
devices 0..1 ──< conversations（device_id 可空）

# Day2（迁移 0003/0004/0005）
accounts 1 ──< device_tokens（device 关联经 tokens 表表达）
accounts >──< rooms（building_assignments 护工-楼栋）
accounts >──< elders（doctor_elder_assignments 医生-长者；family_elder_links 家属-长者）
accounts 1 ──< elder_health_records（recorded_by 非空）
accounts >──< elders（health_grants 账号×长者授权，account_id+elder_id 唯一）
accounts 1 ──< health_audit_logs（operator_id 非空）/ health_ai_analyses（requested_by）/ health_summaries（generated_by、可空 acknowledged_by）
elders 1 ──< elder_health_records / health_grants / health_audit_logs / health_ai_analyses / health_summaries（均非空 FK）
elder_health_records 1 ──< health_audit_logs（record_id 非空）
```

| 表 | 职责 |
| --- | --- |
| `rooms` | 房间（单人间/双人间），被 devices、elders 引用 |
| `devices` | 平板等设备，必属一个房间；Day1 不存 JWT |
| `elders` | 长者档案；room_id+bed_no 表达住户床位；引用 personas |
| `personas` | 数字人人设模板；可被多个 elders 引用 |
| `memory_chunks` | 长期记忆块；必属一个 elder；含向量列 |
| `medication_plans` | 用药计划；必属一个 elder |
| `media_library` | 可授权媒体目录；Day1 只建结构，不导入媒体 |
| `health_records` | 健康采集记录（生理体征时序）；必属一个 elder |
| `voiceprints` | 声纹向量记录；必属一个 elder |
| `workorders` | 护工工单；必属一个 elder；无 staff 表，assignee 暂存外部引用 |
| `conversations` | 会话索引（对应原 conversation_threads 语义） |
| `accounts` | 人账号（护工/医生/家属/管理员统一身份）；密码只存哈希 |
| `device_tokens` | 设备凭证：Token 哈希、签发/过期/吊销，不存明文 |
| `building_assignments` | 护工-楼栋授权（范围判定依据） |
| `doctor_elder_assignments` | 医生-长者关联（有效期） |
| `family_elder_links` | 家属-长者关联（有效期，家属可见性依据） |
| `elder_health_records` | 健康档案（五类纵向记录，basic/confidential 分级） |
| `health_grants` | 健康授权 ACL（账号×长者×查看/编辑×两级，软吊销） |
| `health_audit_logs` | 健康档案写操作审计流水（旧值→新值，只增不改） |
| `health_ai_analyses` | AI 解读快照留档（仅 basic 级出域） |
| `health_summaries` | 医生健康摘要版本留档 + 家属已阅回执 |

注意：Day1 的 `health_records`（生理体征时序采集，服务健康分级工作流）与 Day2 的 `elder_health_records`（纵向健康档案，服务分级授权查看）是两张职责不同的表，互不替代。

命名映射：`conversation_threads → conversations`、`work_orders → workorders`。

## 3. 逐表字段说明

### 3.1 rooms（TimestampMixin：created_at! / updated_at!）

| 字段 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| room_id | UUID PK | 否 | 数据库默认生成 |
| building_code | VARCHAR(32) | 否 | 楼栋编码 |
| floor | SMALLINT | 否 | 楼层 |
| room_number | VARCHAR(32) | 否 | 房号 |
| bed_count | SMALLINT | 否 | 床位数，CHECK ∈ (1,2) |
| status | VARCHAR(20) | 否 | CHECK ∈ active/maintenance/inactive，默认 active |

约束：唯一 `(building_code, floor, room_number)`；索引 `status`。

### 3.2 personas（created_at! / updated_at!）

| 字段 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| persona_id | UUID PK | 否 | |
| code | VARCHAR(64) | 否 | 唯一 |
| name | VARCHAR(100) | 否 | |
| avatar_url | VARCHAR(500) | 是 | 形象地址 |
| personality | TEXT | 是 | 性格描述 |
| greeting | TEXT | 是 | 问候语 |
| voice_id | VARCHAR(100) | 是 | 音色 |
| is_active | BOOLEAN | 否 | 默认 true |

索引：`(is_active, name)`。

### 3.3 devices（created_at! / updated_at! / last_seen_at?）

| 字段 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| device_id | UUID PK | 否 | |
| device_code | VARCHAR(80) | 否 | 唯一 |
| room_id | UUID FK→rooms | 否 | 必属一个房间 |
| bed_no | SMALLINT | 是 | NULL 或 1/2；NULL 表示房间级设备 |
| device_type | VARCHAR(24) | 否 | CHECK 当前含 tablet，默认 tablet |
| status | VARCHAR(20) | 否 | CHECK ∈ pending/active/disabled/retired，默认 pending |
| last_seen_at | TIMESTAMPTZ | 是 | 最近在线时间 |

索引：`(room_id, status)`。

### 3.4 elders（created_at! / updated_at!）

| 字段 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| elder_id | UUID PK | 否 | |
| name | VARCHAR(100) | 否 | |
| birth_date | DATE | 是 | |
| gender | VARCHAR(20) | 是 | CHECK ∈ female/male/other/unknown |
| room_id | UUID FK→rooms | 是 | 与 bed_no 成对：同空或同有（CHECK） |
| bed_no | SMALLINT | 是 | NULL 或 1/2 |
| persona_id | UUID FK→personas | 否 | |
| status | VARCHAR(20) | 否 | CHECK ∈ active/paused/discharged，默认 active |

约束：部分唯一 `(room_id, bed_no) WHERE room_id IS NOT NULL AND bed_no IS NOT NULL`；索引 `(room_id, status)`。

**单/双人间床位模型**：住户按行表达——单人间最多一条 `(room_id, bed_no=1)`；双人间分别为床位 1、2。
部分唯一约束保证每个床位最多一位长者。数据库 CHECK 无法校验 `bed_no <= rooms.bed_count` 的跨表规则，
该规则由后续写入服务校验。

### 3.5 memory_chunks（created_at! / updated_at!）

| 字段 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| chunk_id | UUID PK | 否 | |
| elder_id | UUID FK→elders | 否 | 记忆隔离依据 |
| content | TEXT | 否 | 记忆内容 |
| embedding | VECTOR（无维度） | 否 | pgvector；维度待模型确定 |
| embedding_model | VARCHAR(80) | 是 | 模型版本 |
| category | VARCHAR(24) | 否 | CHECK ∈ preference/health/event/relationship/routine/other |
| visibility | VARCHAR(20) | 否 | CHECK ∈ private/care_team（本人私有/医护共享），默认 private |
| status | VARCHAR(20) | 否 | CHECK ∈ active/superseded/suppressed/deleted，默认 active；支持冲突记忆替代 |
| expires_at | TIMESTAMPTZ | 是 | 过期时间 |

索引：`(elder_id, status, category, created_at)`、`(elder_id, visibility)`。
**记忆可见性边界**：所有召回强制 `elder_id + visibility + status` 过滤；匿名模式不得访问任何个人记忆。

### 3.6 medication_plans（created_at! / updated_at!）

| 字段 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| plan_id | UUID PK | 否 | |
| elder_id | UUID FK→elders | 否 | |
| drug_name | VARCHAR(160) | 否 | |
| dose | VARCHAR(100) | 否 | |
| schedule | JSONB | 否 | 服药时间表 |
| instructions | TEXT | 是 | |
| start_date | DATE | 否 | |
| end_date | DATE | 是 | CHECK 为空或 ≥ start_date |
| status | VARCHAR(20) | 否 | CHECK ∈ planned/active/paused/completed/cancelled，默认 planned |

索引：`(elder_id, status)`。

### 3.7 media_library（created_at! / updated_at!）

| 字段 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| media_id | UUID PK | 否 | |
| type | VARCHAR(20) | 否 | CHECK ∈ opera/movie |
| title | VARCHAR(240) | 否 | |
| genre | VARCHAR(80) | 是 | 剧种/题材 |
| era | VARCHAR(80) | 是 | 年代 |
| artist | VARCHAR(160) | 是 | |
| media_uri | VARCHAR(500) | 是 | 未授权媒体不填地址 |
| duration_seconds | INTEGER | 是 | |
| copyright_status | VARCHAR(20) | 否 | CHECK ∈ unknown/licensed/teaching/public_domain/restricted/expired，默认 unknown |
| status | VARCHAR(20) | 否 | CHECK ∈ active/hidden，默认 hidden |

约束：**版权边界** CHECK——仅 `copyright_status ∈ (licensed, teaching, public_domain)` 时才允许 `status = active`。
索引：`(type, status)`、`title`。Day1 不导入、不链接任何未授权媒体。

### 3.8 health_records（仅 created_at!，无 updated_at）

| 字段 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| record_id | UUID PK | 否 | |
| elder_id | UUID FK→elders | 否 | |
| recorded_at | TIMESTAMPTZ | 否 | 采集时间 |
| bp_systolic | SMALLINT | 是 | CHECK NULL 或 50–250 |
| bp_diastolic | SMALLINT | 是 | CHECK NULL 或 30–150 |
| sleep_minutes | INTEGER | 是 | CHECK NULL 或 0–1440 |
| mood | VARCHAR(24) | 是 | |
| chief_complaint | TEXT | 是 | 主诉 |
| source | VARCHAR(24) | 否 | 采集来源 |

索引：`(elder_id, recorded_at)`。

### 3.9 voiceprints（created_at! / expires_at?，无 updated_at）

| 字段 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| voiceprint_id | UUID PK | 否 | |
| elder_id | UUID FK→elders | 否 | |
| embedding | VECTOR（无维度） | 否 | 加密方案待 POC，Day1 不实现 |
| embedding_model | VARCHAR(80) | 是 | |
| sample_uri | VARCHAR(500) | 是 | 临时样本引用，非永久资源 |
| status | VARCHAR(20) | 否 | CHECK ∈ active/revoked/expired，默认 active |
| expires_at | TIMESTAMPTZ | 是 | |

索引：`(elder_id, status)`。

### 3.10 workorders（created_at! / updated_at! / resolved_at?）

| 字段 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| workorder_id | UUID PK | 否 | |
| elder_id | UUID FK→elders | 否 | |
| type | VARCHAR(32) | 否 | 工单类型（如漏服提醒）；Day1 不设枚举 CHECK |
| status | VARCHAR(20) | 否 | CHECK ∈ open/acknowledged/in_progress/resolved/cancelled，默认 open |
| priority | VARCHAR(16) | 否 | CHECK ∈ low/normal/high/urgent，默认 normal |
| assignee_ref | VARCHAR(120) | 是 | 处理人外部引用；无 staff 表，不伪造外键 |
| title | VARCHAR(200) | 否 | |
| description | TEXT | 是 | |
| resolved_at | TIMESTAMPTZ | 是 | |

索引：`(status, created_at)`、`(elder_id, status)`。

### 3.11 conversations（started_at! / ended_at?，无 created_at/updated_at）

| 字段 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| thread_id | UUID PK | 否 | 会话主键与外部追踪标识 |
| elder_id | UUID FK→elders | 是 | 匿名通用模式允许为空，但不得访问个人记忆 |
| device_id | UUID FK→devices | 是 | |
| session_seq | BIGINT | 否 | 该长者的会话序号 |
| identity_source | VARCHAR(24) | 否 | CHECK ∈ device_room/voiceprint/explicit_confirmation/staff_token/anonymous |
| status | VARCHAR(20) | 否 | CHECK ∈ active/ended/abandoned，默认 active |
| started_at | TIMESTAMPTZ | 否 | 默认 now() |
| ended_at | TIMESTAMPTZ | 是 | CHECK 为空或 ≥ started_at |

约束：唯一 `(elder_id, session_seq)`（elder_id 为 NULL 时 PostgreSQL 唯一约束不拦截多行，匿名会话不占用序号语义）；
索引 `(elder_id, started_at)`、`(device_id, started_at)`。
**会话序号语义**：对已识别长者，`(elder_id, session_seq)` 唯一约束落实会话生成语义；分配逻辑由后续服务实现。

### 3.12 accounts（Day2 · 0003，created_at! / updated_at!）

| 字段 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| account_id | UUID PK | 否 | 数据库默认生成 |
| username | VARCHAR(64) | 否 | 唯一 |
| phone | VARCHAR(32) | 是 | 唯一 |
| display_name | VARCHAR(100) | 否 | 展示名 |
| role | VARCHAR(16) | 否 | CHECK ∈ nurse/doctor/family/admin，默认 nurse |
| password_hash | VARCHAR(255) | 否 | 只存强哈希，不明文 |
| status | VARCHAR(20) | 否 | CHECK ∈ active/disabled/locked，默认 active |

索引：`(role, status)`。

### 3.13 device_tokens（Day2 · 0003，无 updated_at）

| 字段 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| token_id | UUID PK | 否 | |
| device_id | UUID FK→devices | 否 | |
| token_kind | VARCHAR(16) | 否 | CHECK ∈ register/access，默认 register |
| token_hash | VARCHAR(64) | 否 | 唯一；只存哈希不存明文 |
| issued_at | TIMESTAMPTZ | 否 | 默认 now() |
| expires_at | TIMESTAMPTZ | 否 | |
| revoked_at | TIMESTAMPTZ | 是 | 非空即已吊销（验签合法仍拒绝） |
| last_used_at | TIMESTAMPTZ | 是 | |

索引：`(device_id, token_kind, revoked_at)`。

### 3.14 building_assignments / doctor_elder_assignments / family_elder_links（Day2 · 0004，created_at! / updated_at!）

三表共有列：`assignment_id`（UUID PK）、`account_id`（FK→accounts，非空）、`status`（CHECK ∈ active/suspended，默认 active；吊销一律软吊销）、时间戳。

| 表 | 专有字段 | 约束与索引 |
| --- | --- | --- |
| `building_assignments` | `building_code` VARCHAR(32) 非空 | 唯一 `(account_id, building_code)`；索引 `(account_id, status)` |
| `doctor_elder_assignments` | `elder_id` FK→elders 非空；`valid_until` TIMESTAMPTZ 可空 | 唯一 `(account_id, elder_id)`；索引 `(account_id, status)`、`(elder_id, status)` |
| `family_elder_links` | `elder_id` FK→elders 非空；`relation` VARCHAR(32) 可空；`valid_until` TIMESTAMPTZ 可空 | 唯一 `(account_id, elder_id)`；索引同上 |

过期与否由服务层范围校验判定（不依赖数据库定时任务）；全部外键 RESTRICT，保留追溯。

### 3.15 elder_health_records（Day2 · 0005，created_at! / updated_at!）

| 字段 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| record_id | UUID PK | 否 | |
| elder_id | UUID FK→elders | 否 | |
| category | VARCHAR(20) | 否 | CHECK ∈ vital/body_part/medication/chronic/major |
| item_key | VARCHAR(64) | 否 | 条目名，如 blood_pressure/heart |
| value_text | TEXT | 否 | 内容：症状描述/数值+单位/诊断结论 |
| severity | VARCHAR(20) | 否 | CHECK ∈ normal/attention/warning，默认 normal |
| data_level | VARCHAR(20) | 否 | CHECK ∈ basic/confidential，默认 basic；**major 类由服务端强制 confidential**，不信任客户端 |
| recorded_by | UUID FK→accounts | 否 | 写入人 |
| recorded_role | VARCHAR(20) | 否 | CHECK ∈ nurse/doctor/admin；服务端按身份注入 |

索引：`(elder_id, category)`、`(elder_id, data_level)`。
与 Day1 `health_records` 的关系：本表是纵向健康档案（供分级授权查看），`health_records` 是生理体征时序采集（供健康分级工作流），职责不同、互不替代。

### 3.16 health_grants（Day2 · 0005，created_at! / updated_at!）

| 字段 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| grant_id | UUID PK | 否 | |
| account_id | UUID FK→accounts | 否 | 与 elder_id 组成唯一 |
| elder_id | UUID FK→elders | 否 | |
| can_view_basic | BOOLEAN | 否 | 默认 false |
| can_edit_basic | BOOLEAN | 否 | 默认 false；**编辑位必须含对应查看位，否则 422（服务层校验）** |
| can_view_confidential | BOOLEAN | 否 | 默认 false |
| can_edit_confidential | BOOLEAN | 否 | 默认 false；护工角色即使被配置也拿不到 confidential（查询层按角色兜底过滤） |
| status | VARCHAR(20) | 否 | CHECK ∈ active/suspended，默认 active；软吊销可恢复 |
| granted_by | UUID FK→accounts | 否 | 授权操作人 |

约束：唯一 `(account_id, elder_id)`；索引 `(account_id, status)`、`(elder_id, status)`。

### 3.17 health_audit_logs（Day2 · 0005，仅 created_at!，只增不改）

| 字段 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| log_id | UUID PK | 否 | |
| record_id | UUID FK→elder_health_records | 否 | |
| elder_id | UUID | 否 | 冗余存，便于按长者查询 |
| action | VARCHAR(20) | 否 | CHECK ∈ create/update/delete |
| old_value | JSONB | 是 | 更新前快照 |
| new_value | JSONB | 是 | 更新后快照 |
| operator_id | UUID FK→accounts | 否 | 操作人 |

索引：`(elder_id, created_at)`、`(operator_id)`。仅管理员可查（GET /api/v1/admin/health-audit）。

### 3.18 health_ai_analyses（Day2 · 0005，仅 created_at!）

| 字段 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| analysis_id | UUID PK | 否 | |
| elder_id | UUID FK→elders | 否 | |
| input_snapshot | JSONB | 否 | 出域数据留底（**仅 basic 级**，脱敏：无姓名/UUID/房间号） |
| analysis_text | TEXT | 否 | 解读文本（固定附免责声明） |
| model_name | VARCHAR(100) | 否 | 实际调用模型 |
| prompt_version | VARCHAR(20) | 否 | 服务端固定提示词模板版本，默认 v1 |
| requested_by | UUID FK→accounts | 否 | 发起人（family/admin） |

索引：`(elder_id, created_at)`。调用失败（502）不留档，成功才写入。

### 3.19 health_summaries（Day2 · 0005，仅 created_at!）

| 字段 | 类型 | 空 | 说明 |
| --- | --- | --- | --- |
| summary_id | UUID PK | 否 | |
| elder_id | UUID FK→elders | 否 | |
| summary_text | TEXT | 否 | 医生生成的规则式聚合摘要 |
| version | INTEGER | 否 | 同一长者递增，默认 1 |
| generated_by | UUID FK→accounts | 否 | 生成医生 |
| acknowledged_by | UUID FK→accounts | 是 | 家属已阅回执人 |
| acknowledged_at | TIMESTAMPTZ | 是 | 回执时间；重复回执幂等（不覆盖首次） |

索引：`(elder_id, created_at)`。

## 4. 迁移组织

1. `0001_facility_core`：`CREATE EXTENSION IF NOT EXISTS vector`（及 pgcrypto）→ rooms → personas → devices → elders。
2. `0002_domain_tables`：conversations、medication_plans、media_library、health_records、memory_chunks、voiceprints、workorders。
3. `0003_accounts_device_tokens`：accounts、device_tokens。
4. `0004_authorization_links`：building_assignments、doctor_elder_assignments、family_elder_links。
5. `0005_health_profile`：elder_health_records、health_grants、health_audit_logs、health_ai_analyses、health_summaries。
6. 降级业务表时不删除扩展，避免误删共享扩展。
7. 每次 `alembic upgrade head` 前人工审查迁移文件（外键、可空性、约束、向量类型、downgrade）；不回改已执行迁移（ADR-013）。

## 5. 后续候选表清单（已落地表不再列入）

`staff_tokens`、`medication_events`、`decision_logs`、`media_history`、`health_events`、`family_reports`、
`family_checkins`、`family_care_activities`、`reward_rules`、`reward_grants`、`audit_logs`、`metrics`。
以上表必须经评审后通过新迁移加入，不回改已执行迁移（ADR-013、ADR-016）。
原候选清单中的 `accounts`、授权关联表、`device_tokens` 已分别由迁移 0003、0004 落地；健康档案五表由迁移 0005 落地（§3.12–3.19）。

---

## 附：Day3 实装补充（2026-11-05）

> 本节为 Day3 语音模块落地后的数据层补充，原 §1–§5 内容不变。**Day3 零新增迁移、零新增表**：语音功能完全复用 Day1 既有的 `voiceprints`（§3.9）与 `elders`，生物识别登录（ADR-020）同样不新增表。

### 6. voiceprints 表首次实装使用（对应 §3.9）

Day3 声纹录入（设备通道/管理员通道）实际写入情况（kangban_dev 实查，2026-11-05）：

| 字段 | 实际取值 |
| --- | --- |
| embedding | VECTOR **192 维**（CAMPPlus 输出，`vector_dims()=192` 实证） |
| embedding_model | `campplus-3dspeaker:f682b514c05d`（sherpa-onnx 模型名+哈希） |
| status | `active`（吊销/过期语义仍未触发，留 Day8） |
| sample_uri | 当前为 NULL（原始音频不落盘，仅留 embedding） |
| expires_at | 当前为 NULL（声纹长期有效；300s 免验的是内存临时 Token，与本表无关） |
| 归属 | 2 条记录同属 102 房赵奶奶（双人间管理员通道录入，ADR-005） |

与 §3.9 原设计的差异说明（均为已知边界，非结构变更）：

1. 「加密方案待 POC」——**仍未实现**，embedding 当前明文存储，留 Day8 复核（ADR-019 影响）；
2. 「维度无 CHECK」——实际固定 192 维由应用层保证，未加数据库约束；
3. 检索方式：会话内全量载入该长者 active 声纹做余弦相似度（数据量小，未建 ANN 索引），pgvector 索引待数据量增长后评审。

### 7. 生物识别演示接口的数据访问面（无新表）

- `GET /voice/biometric/rooms`：只读联查 rooms/devices/elders（返回房号、床数、在住长者姓名、活跃设备码），不落库；
- `POST /voice/biometric/login`：指纹模式只读校验后经既有 `issue_access_token` 签发设备 JWT；声纹模式读 voiceprints 比对，通过后同样签发；两者均**不写任何表**（设备 JWT 不落库，与既有设备登录一致）。
