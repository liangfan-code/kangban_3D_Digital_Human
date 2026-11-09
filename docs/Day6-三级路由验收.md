# Day6 三级路由验收

> 范围：大纲课时 6.1–6.4。判定链路「安全一票否决 → 规则层 → 分类器层（System One）→ 主控 Agent（ReAct + Tool + MCP）」全部为纯函数或可插拔实现，语音/文字双通道共用。
> 约束：不加载模型权重、不调用外部天气服务、不新增数据库迁移；Laya 与 MCP 均走「不可用即可见降级」。

## 1. 代码地图

| 层 | 入口 | 关键文件 | 测试 |
| --- | --- | --- | --- |
| 安全（一票否决） | `route_text` 第一步 | `app/router/safety.py`、`app/router/executor.py` | `tests/test_router_rules.py` |
| 规则（10 条，零模型） | `route_text` 第二步 | `app/router/rules.py` | `tests/test_router_rules.py` |
| 分类器（五组问题） | `route_text` 第三步 | `app/router/classifier.py`、`app/system_one/questions.py`、`app/integrations/laya.py` | `tests/test_router_classifier.py` |
| 主控 Agent | 多意图 / 闲聊 / 健康采集 | `app/agent/companion.py`、`app/agent/tools.py` | `tests/test_companion_agent.py`、`tests/test_agent_tools.py` |
| MCP | 天气 Tool | `mcp-servers/weather/server.py`、`app/agent/mcp_client.py` | `tests/test_agent_mcp.py` |
| HITL | 敏感事件 / 通知家属 | `app/services/hitl.py`、`app/api/routes/hitl.py` | `tests/test_hitl_api.py` |
| 评测 | 离线基线 | `eval/fixtures/day6_intents.jsonl`、`scripts/eval_day6.py` | `tests/test_eval_day6.py` |

## 2. 验收场景复现

| # | 场景 | 输入 | 预期判定 | 复现方式 |
| --- | --- | --- | --- | --- |
| 1 | 安全优先 | 我胸口疼得厉害 | `safety`，`is_sensitive=true`，入 HITL，不调 LLM | `pytest tests/test_router_rules.py -k safety` |
| 2 | 规则直答 | 今天天气怎么样 | `rule` / `R07`，`tool=weather_get`，`escalated_to_agent=false` | `pytest tests/test_router_rules.py -k weather` |
| 3 | 规则本地动作 | 大声点 | `rule` / `R02`，`action=volume_up`，零模型 | `pytest tests/test_router_rules.py -k volume` |
| 4 | 高置信分类 | 外面下雨了吗 | `classifier` / `weather`，`tool=weather_get` | `pytest tests/test_router_classifier.py -k weather` |
| 5 | 低置信澄清 | 那个药还够吗 | `classifier`，`needs_clarification=true`，回复澄清话术 | `pytest tests/test_router_classifier.py -k low` |
| 6 | 闲聊升级 | 你陪我聊聊天吧 | `agent`，`escalated_to_agent=true`，走流式 LLM | `pytest tests/test_voice_text.py` |
| 7 | 多意图组合 | 吃完药放戏再告诉我儿子 | 主控 Agent 按序调 `medication_query` / `media_play` / `notify_family`，不被低置信反问截断 | `pytest tests/test_voice_text.py -k multi_intent`、`tests/test_companion_agent.py` |
| 8 | MCP 天气 | 上海天气 | 真实 stdio 子进程返回本地 fixture，标注【示例数据】 | `pytest tests/test_agent_mcp.py` |
| 9 | HITL 管理 | `GET /api/v1/admin/hitl/pending` | 仅 admin；可列出并 resolve | `pytest tests/test_hitl_api.py` |
| 10 | 评测基线 | 50 条标注样例 | 分层/意图/敏感召回/延迟/成本可复算 | `python scripts/eval_day6.py` |

运行时日志核对（结构化 JSON 字段，不记长者原文）：

```text
route_layer=rule    intent=weather  rule_id=R07  escalated_to_agent=false  tool_calls=weather_get  mcp_calls=1
route_layer=safety  is_sensitive=true  sensitive_category=急症  hitl_enqueued=true  llm_called 不出现
route_layer=classifier  route_confidence=0.768  needs_clarification=true
route_layer=agent   escalated_to_agent=true  llm_called=true  agent_steps=2  tool_calls=medication_query,media_play,notify_family
```

## 3. 降级矩阵

| 失效点 | 检测方式 | 行为 | 可见性 |
| --- | --- | --- | --- |
| Laya 未安装 / 未配置权重 / 加载失败 | `LayaClassifier.available` | 回落 `LocalIntentClassifier` | 日志 `system_one_fallback`；评测报告标注 `classifier=local` |
| LLM 未配置 / 超时 / 5xx | `LlmUnavailable` | 模板回复，通话不中断 | 文字接口 `degraded=true`；语音事件流可见 |
| Agent 空回复 / 步数超 4 步 / 工具重试保护 | `AgentUnavailable` | `agent_fallback` 后回落流式 LLM | 结构化日志 `degrade=agent_fallback` |
| MCP 子进程启动失败 / 5s 超时 | `McpCallResult.ok=false` | 返回可朗读的降级话术 | `trace.degrade("tool_unavailable")`，回复不编造天气 |
| 用药工具未接入 | 工具 handler 明确返回未接入 | 引导查看药盒/联系家属，不编造药名剂量 | `ToolResult.ok=false` + 降级标记 |
| 通知家属 | 禁止直接外呼 | 写入 HITL 队列，Agent 只收到「已转人工确认」 | `hitl_enqueued=true`，admin 队列可见 |
| ASR / 声纹不可用（Day3 既有） | Provider 异常 | WS error 事件 + 文字通道 | 原有降级链不变 |

## 4. MCP Schema 人审记录

服务端：`mcp-servers/weather/server.py`（`MCPServer("kangban-weather")`）。

| 项 | 内容 |
| --- | --- |
| Tool 名 | `get_weather` |
| 入参 | `city: string`，默认「本地」 |
| 出参 | 中文短句，固定前缀 `【示例数据】` |
| 数据源 | 进程内 fixture（北京/上海/广州/杭州/默认），**不访问网络** |
| 副作用 | 无（只读） |
| 鉴权 / PII | 无鉴权、不接收长者身份、不落盘 |
| 生命周期 | 每次调用起短生命周期子进程，结束即回收；不跨事件循环持有会话 |
| 超时 | 客户端 5s（测试用 20s） |
| Agent 可见工具 | `weather_get`、`medication_query`、`media_play`、`media_next`、`media_stop`、`notify_family` |

审查状态：**AI 生成，待人工复核 diff**（按 AGENTS.md，`mcp-servers/**` 属高风险目录）。复核重点：不新增网络调用、不读取环境密钥、不把长者原文写入 MCP 参数或日志。

## 5. 评测基线

报告：[eval/reports/day6_router_baseline.md](../../eval/reports/day6_router_baseline.md)（50 条人工标注样例，`day6-router-eval-v1`）。

| 指标 | 结果 | 口径 |
| --- | --- | --- |
| 分层准确率 | 100.0% | 预测层级 = 标注层级 |
| 意图准确率 | 100.0% | 40 条标注了意图的样例 |
| 敏感召回率 | 100.0% | 10 条敏感样例全部被安全层拦截；目标 ≥ 95% |
| S1 闭环率 | 78.0% | 未进入主控 Agent |
| 工具调用准确率 | 100.0% | 11 条期望调用工具的样例 |
| 路由延迟 | P50 0.030ms / P95 0.072ms | 只测 `route_text`，不含 ASR/TTS/网络 |
| 成本 | LLM 调用 50 → 11；输入字符 285 → 82 | 未计费，只给可复算量级 |

口径与局限：样例为人工构造，与规则/关键词存在同源偏差，本基线衡量**回归稳定性**而非开放场景泛化；不含 ASR 转写错误；Laya 权重未加载，其准确率需接入后单独测量。

bad case 回流：评测失败样例自动写入 `eval/fixtures/day6_badcases.jsonl`（按 id 去重，修复后自动移除），人工归因后再补规则或扩标注样例。本轮无失败样例，文件不生成。

## 6. 已知限制与下一步

- 多意图判定是启发式（信号类别 ≥ 2，或 1 类信号 + 「再/然后/顺便」），同义表达可能漏判；失败样例进回流池。
- HITL 目前为进程内存队列，重启即失；Day8 落库并补操作审计。
- 天气为本地 fixture；接入真实天气需单独授权并补 MCP 鉴权/限流。
- 媒体控制仅下发 `control` 事件与前端音量/停止；真实片源点播在 Day7 接入。
- Laya 适配器与 `_predict` 字段映射未经真实权重验证，禁止在未授权环境下载权重。

## 7. 验收清单

- [x] 安全词优先于一切，敏感事件入 HITL 且不调 LLM
- [x] 10 条规则命中不调模型，动作/时间/再见/重复可本地闭环
- [x] 五组问题定义、置信度闸门、低置信/域外澄清
- [x] Laya 可插拔，未安装/无权重/超时自动回落本地分类器
- [x] 多意图进入 ReAct Agent，工具调用有步数与数量上限
- [x] 天气经 MCP stdio 子进程，失败可朗读降级且不编造数据
- [x] 用药工具不编造药名剂量；通知家属只入 HITL
- [x] 管理员 HITL 接口鉴权与处理闭环
- [x] 50 条标注评测集 + 可复算基线 + bad case 回流
- [x] 全量 `python -m pytest` 与改动文件 `ruff check` 通过

人工复核项（未完成前不得视为验收通过）：

- [ ] 人工审查 `app/realtime/**` 之外的本次 diff，重点 `app/services/voice.py` 路由分支与 `mcp-servers/**`
- [ ] 在目标环境确认 Laya 选型与权重路径（当前仅接口与 spike）
- [ ] 真机验证语音通道多意图与 `control` 事件（前端 `volume_up/down/stop`）
