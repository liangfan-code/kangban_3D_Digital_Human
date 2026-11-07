# Day1-Day4 逐课时对账 Spec（只对比，不改代码）

## Why
仓库缺少「大纲验收标准 ↔ 实际实现」的逐课时对账，无法判断哪些验收项真正达成、哪些是替代实现或未落地。本规格只产出对账，不改任何代码；代码层修改待用户逐项确认后另行立项。

## What Changes
- 按大纲 Day1-Day4 的 17 个课时逐项对账，每项给出：大纲验收标准、实际结果、判定（完成/部分/替代/未实测）。
- 登记大纲之外的超纲功能（实现了什么、没实现什么）。
- 列出差距清单（标注「未执行，待确认」）与 Day5-Day10 续接建议。
- 不修改业务代码、迁移与依赖。

## Impact
- Affected specs: 无
- Affected code: 无；仅新增 `.trae/specs/audit-day1-day4-alignment/` 文档

## 逐课时对账

### Day1 工程基建
| 课时 | 大纲验收标准 | 实际结果 | 判定 |
| --- | --- | --- | --- |
| 1.1 认知 + Agent 闭环 | 提交 200 字「最容易翻车的三件事」；闭环图讲清 Workflow/Agent Loop/子图 | `docs/架构与Agent闭环图.md`、`docs/工作流选型说明.md` 在；个人 200 字与风险清单未入库 | 部分 |
| 1.2 工作台 + Git + AGENTS | 首次 commit；AGENTS 含高风险清单与模型路由表；`.env` 在 gitignore | 均有（`pyproject.toml`、`.env.example`、`AGENTS.md`、`.gitignore`）；首次 commit 已做 | 完成 |
| 1.3 11 张表 ER + 迁移 | `alembic upgrade head` 成功；`\d elders` 字段齐全；11 表文档评审；无多余字段 | `0001` 建 4 表、`0002` 建全量领域表，实际 22 表；`docs/ER_数据模型设计.md` 在；memory_chunks/voiceprints 带 vector | 完成 |
| 1.4 FastAPI 骨架 | `curl /health` 200；日志含 trace_id；pyproject 声明 fastapi/uvicorn/pydantic-settings/sqlalchemy/alembic/redis/apscheduler | 全部满足；`redis`、`apscheduler` 已声明 | 完成 |

### Day2 身份与权限
| 课时 | 大纲验收标准 | 实际结果 | 判定 |
| --- | --- | --- | --- |
| 2.1 提示词模板库 | `docs/prompts/` 存 ≥6 个模板 | `docs/prompts/01-06` + README | 完成 |
| 2.2 人 JWT + 设备 Token | 错密码/伪造 token 401；`/auth/me` 解出角色；设备 token 解出 `device_id`；pytest 覆盖 | `/auth/me`、`/auth/device/register` 存在；`tests/security/test_day2_attacks.py` 覆盖伪造/吊销/过期/越权 | 完成 |
| 2.3 长者 CRUD + 设备绑定 | 非管理员调 `POST /elders` 403；pytest ≥3 条；写操作回查 DB | `elders.py`、`admin.py`、`services/elder.py` 实现；测试覆盖权限与冲突 | 完成 |
| 2.4 Iter1 验收 + 攻击者预热 | 演示通过；`git log` ≥5 次小步 commit；3 条攻击用例入库 | 3 条攻击用例在（A1 伪造 JWT、A2 设备重放/过期、A3 跨楼栋越权）；**`git log` 仅 2 次提交**；prompt 日志/200 字未入库 | 部分 |

### Day3 WebRTC 与语音
| 课时 | 大纲验收标准 | 实际结果 | 判定 |
| --- | --- | --- | --- |
| 3.1 WebRTC 概念 + 拓扑 | 自画时序图；说清 STUN/TURN；明确本机走 STUN 还是 TURN | 仅 `RTC_ICE_SERVERS` 配置；仓库无专有时序/拓扑图；未部署 coturn，跨机未验证 | 部分 |
| 3.2 aiortc 最小回声 | 耳机听到自己说话；跨机 TURN 也能回声；无报错 | `POST /api/v1/rtc/offer` 真实建 PC，显式 recvonly 防回声，音频桥接 ASR/TTS；**无回声回传**、无跨机 | 替代实现 |
| 3.3 ASR 流式 + 置信度 | 说完 1s 内出文字+置信度；trace 有置信度字段 | FunASR WSS 2pass 真实转写；置信度字段解析并埋点，但本地镜像返回 0 | 部分 |
| 3.4 TTS 流式 + 降级 | 现场闭环；ASR/TTS 任一关闭不白屏 | edge-tts 真实流式合成、16k PCM 播放；WS error + 文字通道降级；未做真实闭环演示 | 部分 |

### Day4 3D 与身份闭环
| 课时 | 大纲验收标准 | 实际结果 | 判定 |
| --- | --- | --- | --- |
| 4.1 vrm-playground → pad | `pnpm dev` 起页面；单个 VRM 柔和背景；无控制台报错 | 未建 `frontend/`、`vrm-playground/`；改为 `app/static/avatar/**` 原生页面，three-vrm 本地 vendored，33 VRM/16 VRMA/7 形象，自动眨眼/相机/背景/动作切换，已浏览器实测渲染 | 替代实现 |
| 4.2 联调 + 设备身份锁定 | 说完听到 TTS；状态条变化；`elder_id` 来自设备 | 设备 JWT 鉴权、`elder_id` 服务端注入、状态条均有；真实 TTS 听感未实测 | 部分 |
| 4.3 CAM++ 声纹 + 主动确认 | 单人间不被误判；双人间主动确认；服务关闭走兜底 | 192 维、余弦阈值默认 0.55（大纲 0.85）；多段样本平均但未强制 3 段；连续 3 次失败触发 `need_confirm`；Provider 缺失时抛 `ProviderUnavailable` | 部分 |
| 4.4 上下文工程 + LLM | 医疗问题必须拒答；10 轮不爆；Redis 能看到状态 | 提示词含 few-shot 医疗拒答；最近 5 轮从 DB 读取；DeepSeek 流式已通；**无 Redis、无早期摘要、无结构化槽位** | 部分 |
| 4.5 端到端「今天天气」 | 完整演示不翻车；延迟基线入档 | 组件齐全，本次仅验证到数字人渲染与口型桥；未做真实麦克风全链路 | 未实测 |

### 汇总
- 17 项：**完成 6、部分 8、替代实现 2、未实测 1**。
- 未达成原始验收口径的关键项：3.1 时序/拓扑与 TURN、3.2 回声、3.3 置信度、4.2/4.5 真实语音 E2E、4.4 Redis/摘要/槽位。

## 大纲之外新增功能
| 功能 | 实际位置 | 实现了什么 | 没实现什么 |
| --- | --- | --- | --- |
| 健康档案体系（Day8 内容提前） | `app/api/routes/health_profile.py`、`app/services/health_profile.py`、迁移 0005 | 四角色 ACL、basic/confidential 分级、重大疾病隔离、授权、审计、摘要、打印、AI 一键分析（脱敏+频控+降级） | 采集入口、预警推送、HITL 复核、家属日报定时任务 |
| 管理台 + 四角色工作台 + 多入口登录 | `app/static/{index,elder,nurse,doctor,family,admin}.html` | 管理员七页签、角色范围收敛、大字号适老、账号可视化与筛选 | 图表看板、批量导入导出、审计查询页 |
| 3D 形象库/画廊/口型 | `app/static/avatar/**`、`avatar-gallery.html` | 33 VRM、16 VRMA、7 形象切换、动作试播、能量驱动口型、失败回退 | 音素级 viseme、表情情绪联动、性能降级 |
| `scripts/admin_cli.py` | `scripts/` | 建号/建楼栋房间/建长者/配关联 | 非交互模式、幂等批处理 |
| 生物识别演示登录 | `BIOMETRIC_DEMO_ENABLED`、登录页 | 指纹点击即进 | 真实生物识别、风控；答辩前建议置 0 |
| FunASR 容器化 | `compose.funasr.yaml` | 本地 WSS ASR（10096） | 挂载目录与项目不一致、无健康检查 |
| DeepSeek `reasoning_effort` 适配 | `config.py`、`llm.py`、`ai_analysis.py` | 解决 `deepseek-flash` 不带档位必 500 | 无；`deepseek-v4-pro` 上游 500 |
| 记忆表/Redis 预留 | `memory_chunks`、`REDIS_URL` | 表结构与配置就位 | 无 Embedding Provider、无维度约束、无 ANN、Redis 未接入 |

## 差距清单（仅登记，待用户确认后才改代码）
| 优先级 | 差距 | 建议动作（未执行） |
| --- | --- | --- |
| P0 | 4.4 上下文工程 | 补 Redis 会话状态、早期摘要、结构化槽位 |
| P0 | 4.2/4.5 真实语音 E2E | 真实麦克风「今天天气」全链路并留证 |
| P0 | 3.1/3.2 跨机与回声 | 部署 coturn + 补时序图；回声二选一：补 loopback 或修订验收口径 |
| P0 | 4.3 声纹口径 | ADR 记录 0.55 与 0.85 差异；补 3 段注册与确认测试 |
| P1 | 3.3 置信度 | 换带 confidence 的配置，或 UI 明示不可用 |
| P1 | 2.4 提交与过程物 | 按功能拆分提交；补归档 prompt 日志/200 字/风险清单 |
| P2 | 超纲收口 | 生物识别开关默认关闭、账号看板统计、FunASR 挂载统一 |

## 后续规划建议（Day5-Day10，不执行）
| 阶段 | 重点 | 与现状衔接 |
| --- | --- | --- |
| Day5 | viseme 时间轴、E2E 延迟指标、超时熔断、死循环防护 | 现有 `asr_e2e_ms`/`tts_first_byte_ms` 埋点可扩展 |
| Day6 | 规则层 + Laya + MCP 天气 + 敏感一票否决 | 无 MCP/System One，需从零建 |
| Day7 | 记忆治理、用药状态机、戏曲点播、评测基线 | `memory_chunks` 已预留；Redis 可此阶段接入 |
| Day8 | 健康采集 + HITL、护工看板、医生视图、家属 H5 | 档案/ACL/AI 分析提前完成，只差采集与流程 |
| Day9 | 权限闸门、注入/越权用例、MCP 扩展与审计 | 现有 ACL/审计可复用 |
| Day10 | compose 全栈、Langfuse、演示与复盘 | FunASR compose 已有，需补应用/DB 编排 |

**合理性判断**：超纲功能多为产品化前置，方向合理；主要风险是验收口径漂移、基础设施欠账（Redis/TURN/提交历史）、文档与实现不同步。建议先收口 P0，再进入 Day5。

## ADDED Requirements
### Requirement: 逐课时对账可追溯
文档 SHALL 覆盖 Day1-Day4 全部课时，并给出大纲验收、实际结果与判定。

#### Scenario: 查看对账
- **WHEN** 打开 `spec.md`
- **THEN** 能定位每个课时的达成情况与差距

### Requirement: 超纲功能登记
大纲外功能 SHALL 记录「实现了什么 / 没实现什么」。

#### Scenario: 评审超纲功能
- **WHEN** 查看超纲表
- **THEN** 能判断是产品化增益还是验收口径漂移

### Requirement: 代码修改需确认
任何代码、迁移、依赖改动 SHALL 保持未执行，待用户逐项确认后另行立项。

#### Scenario: 确认前保持只读
- **WHEN** 对账完成
- **THEN** 业务代码与迁移无改动

## MODIFIED Requirements
无。

## REMOVED Requirements
无。
