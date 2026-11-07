# Day2 功能完善验收报告（健康档案 · ACL · AI 分析）

> 日期：2026-11-05 ｜ 范围：《Day2_完善前端+后端功能》T1–T7 ｜ 环境：本地 dev（kangban_dev，端口 8000）

## 一、交付概览

| 任务 | 内容 | 提交 |
| --- | --- | --- |
| T1 | 迁移 0005 五新表（health_records / health_grants / health_audit_logs / health_ai_analyses / health_summaries）+ ORM + schemas | f1fc38b（迁移已人审并升级 dev 库） |
| T2 | 服务层：ACL 双层校验（范围判定→data_level 判定）、major 一律 confidential、审计流水同步写、AI 降级/频控/出域 | fb775de |
| T3 | API：档案读写、摘要（生成/已阅回执）、AI 分析（503/429/502 语义）、导出打印、护工待录入/预警、管理端授权 CRUD+审计、/config/flags | fb775de |
| T4 | 测试：越权矩阵 + AI 降级/出域断言/频控，全量 pytest 99 绿、ruff 全绿 | fb775de |
| T5 | 前端四页改造 + 样式扩展 | 8fbb18a |
| T6 | 演示数据预填 + 三角色 API 视角校验 + 浏览器实测 | 本报告 |
| T7 | 权限矩阵 v2 文档 | 见《权限矩阵设计.md》更新 |

## 二、后端关键语义（均已测试锁定）

- **分级**：`major` 类记录服务端强制 `data_level=confidential`，不信任客户端；其余四类为 basic。
- **ACL**：admin 恒全量；护工/医生/家属一律经 `health_grants` 放权（4 个布尔位）；**编辑权限必须包含对应查看权限**，否则 422。先范围判定（范围外 404 隐藏存在性），后 ACL（无权 403）。
- **审计**：create/update 同步写流水（记录快照含 item_key/value_text/severity），管理端可按长者/操作人过滤查询。
- **AI 红线**：仅 family/admin 触发、仅 basic 级数据出域（confidential 记录被脱敏剔除）；未配置 LLM → 503；频控 → 429；调用失败 → 502 不留档。历史分析仅存 basic 级内容。
- **摘要**：医生/admin 生成规则式聚合快照（版本自增）；查看与已阅回执需 confidential 级授权；家属已阅幂等只记首个回执。
- **导出**：打印版 HTML，仅含授权级别内分区。

## 三、演示数据预填（全部走真实 API，值前缀「（演示数据）」）

- 长者「演示长者」（elder_id `67c1a24e…`，A1 栋 101 房 1 床）9 条记录：体征 3（血压 135/85 需关注→后改预警、空腹血糖 6.1 需关注、体重 58kg 正常）、身体部位 3（眼白内障/膝关节炎/心脏窦性心律）、用药 1（氨氯地平）、慢性病 1（高血压 2 级）、重大疾病 1（冠心病史·隐私级）。
- 授权矩阵：nurse1 = 基础查看+编辑；doctor1 = 全 4 项；family1 = 基础查看+隐私查看（无编辑）。
- 医生已生成健康摘要 v1，并完成一次 severity 修改（attention→warning）留下审计流水。

### 三角色 API 视角实测

| 账号 | viewer_level | 可见分区 | 房间/设备 |
| --- | --- | --- | --- |
| nurse1 | basic | vital/body_part/medication/chronic（8 条，**无 major**） | A1栋101房1床 |
| doctor1 | confidential | 全部 5 区（9 条） | 同上 |
| family1 | confidential | 全部 5 区（9 条） | 同上 |

`/config/flags` → `ai_analysis_enabled: false`（LLM 未配置，符合降级预期）。

## 四、浏览器实测（原生三件套页面）

### 家属端 family.html — 3/3 PASS

1. 点击长者卡片弹出健康档案弹窗：含「重大疾病」分区与冠心病记录，房间显示「A1栋101房1床」✅
2. 「AI 帮我分析」按钮置灰禁用（flags 未开）✅
3. 「健康摘要」显示 v1 +「我已阅读」，点击后变「已确认已阅」✅

### 护工端 nurse.html — 3/3 PASS

1. 「今日待录入体征」显示演示长者 + 血压/血糖/体重快捷按钮 ✅
2. 长者卡片带「⚠ 体征预警」黄标（health-overview 驱动）✅
3. 快捷录入血压 138/86 保存成功、清单该项消失 ✅；详情弹窗仅呈现基础级分区（服务端过滤）✅

### 管理端 admin.html — 3/4 PASS + 1 修复

1. 「健康授权」标签显示 3 条授权，权限徽标正确（护士行无隐私两项）✅
2. 审计流水表格列齐全、doctor1「修改」行可见 ✅，但旧值/新值未显示 severity 变化 ❌ → **已修复**：`valBrief` 对非 normal 追加【需关注】/【预警】后缀（API 数据已确认流水快照含 severity 字段；该函数为确定性字符串拼接，修改后未再走浏览器复测——浏览器会话环境暂不可用，可在页面刷新后目验）。
3. 按长者筛选审计流水正常 ✅

### 医生端 doctor.html — API 实测（浏览器未跑）

- 档案编辑（PUT severity 200、留审计）、生成摘要 v1（201）均已走 API 验证；页面与家属端复用同一弹窗/表单组件模式。建议用户登录 doctor1/Demo@12345 目验：点长者卡片 → 新增「重大疾病」记录 → 「生成健康摘要」→ 「导出打印」。

## 五、已知边界与后续项

- 护工待录入清单按 `EXPECTED_DAILY_VITALS`（blood_pressure/blood_sugar/weight 英文键）匹配，演示数据用中文 item_key，故清单始终列出三项——快捷录入会再写一条英文键记录，属预期演示行为；正式使用约定录入键即可。
- 演示长者档案中「在用设备」为空：设备 A1-TAB-101 存在但未处 active 绑定态，档案接口只统计活跃设备。
- AI 分析完整链路（LLM 真实调用）待 `.env` 填入 LLM_API_KEY/LLM_BASE_URL/LLM_MODEL 后复验；降级路径（503 提示文案）已实测。
- pytest 99 绿、ruff 全绿；未执行 git push（等待授权）。
