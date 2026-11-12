# Day7 Iter3 任务级基线报告（v1）

- 生成时间（UTC）：2026-11-12 05:49:35
- 代码版本：`1bf7dde`
- 模式：`offline`
- 评测集：`eval/fixtures/day7_scenarios.jsonl`（版本 `day7-scenarios-v1`，样本量 20）
- 评测器版本：`day7-iter3-eval-v1`
- 复现命令：`.\.venv\Scripts\python.exe scripts\eval_day7.py`

## 1. 核心指标（8 项）

| 指标 | 结果 | 公式 | 分母 | 数据来源 |
| --- | --- | --- | --- | --- |
| 任务成功率 | 95.0% | 通过场景数 / 总场景数 | 20 | 实际轨迹对比期望 |
| P95 延迟 | 550.0 ms | 全部轮次模拟耗时 P95 | 26 轮 | 模拟值（固定常量，非真实时钟） |
| S1 闭环率 | 65.4% | 规则/分类器直接处理且未升级的轮次 / 总轮次 | 26 | 路由判定 |
| 分层命中率 | 见表 2 | 各层轮次 / 总轮次 | 26 | 路由判定 |
| 单会话 token 成本 | 0（offline 不调 LLM） | 输入+输出 token | 1 个会话 | offline 记 0 并说明 |
| 敏感召回率 | 100.0% | 敏感场景被判 sensitive / 敏感场景数 | 1 | 路由判定 |
| 工具调用准确率 | 94.4% | 工具名与顺序全匹配场景 / 含工具期望场景 | 18 | 实际调用轨迹 |
| 人工接管率 | 25.0% | 进入 HITL/建单/升级人工场景 / 总场景 | 20 | 实际轨迹与内存工单 |

口径说明：任务成功率按「层级 + 意图 + 工具顺序 + 结果」四项全一致才计通过；工具准确率只统计期望非空工具的场景，顺序不同即算失败；人工接管包含敏感建单、漏服升级与通知家属转人工。

## 2. 分层分布

| 层级 | 轮次 | 占比 |
| --- | --- | --- |
| safety | 1 | 3.8% |
| rule | 17 | 65.4% |
| classifier | 1 | 3.8% |
| agent | 7 | 26.9% |

## 3. 场景明细

| id | 名称 | 期望（层级/意图/工具/结果） | 实际（层级/意图/工具/结果） | 通过 | 模拟耗时(ms) | 降级 |
| --- | --- | --- | --- | --- | --- | --- |
| mem-01 | 回忆孙女口味 | agent/chitchat/memory_recall/memory_referenced | agent/chitchat/memory_recall/memory_referenced | ✅ | 525.0 | 否 |
| mem-02 | 回忆自己口味 | agent/chitchat/memory_recall/memory_referenced | agent/chitchat/memory_recall/memory_referenced | ✅ | 525.0 | 否 |
| mem-03 | 家人近况需召回 | agent/chitchat/memory_recall/memory_referenced | agent/chitchat/—/agent_reply | ❌ | 500.0 | 否 |
| mem-04 | 回忆听戏偏好 | agent/chitchat/memory_recall/memory_referenced | agent/chitchat/memory_recall/memory_referenced | ✅ | 525.0 | 否 |
| med-01 | 服药确认 | rule/medication_query/medication_confirm/medication_confirmed | rule/medication_query/medication_confirm/medication_confirmed | ✅ | 345.0 | 否 |
| med-02 | 查询今日用药 | rule/medication_query/medication_query/medication_reported | rule/medication_query/medication_query/medication_reported | ✅ | 345.0 | 否 |
| med-03 | 连续未确认升级一 | rule/medication_query/medication_confirm,medication_confirm,medication_confirm/medication_escalated | rule/medication_query/medication_confirm,medication_confirm,medication_confirm/medication_escalated | ✅ | 1035.0 | 否 |
| med-04 | 连续未确认升级二 | rule/medication_query/medication_confirm,medication_confirm,medication_confirm/medication_escalated | rule/medication_query/medication_confirm,medication_confirm,medication_confirm/medication_escalated | ✅ | 1035.0 | 否 |
| med-05 | 连续未确认升级三 | rule/medication_query/medication_confirm,medication_confirm,medication_confirm/medication_escalated | rule/medication_query/medication_confirm,medication_confirm,medication_confirm/medication_escalated | ✅ | 1035.0 | 否 |
| media-01 | 越剧点播 | rule/media_play/media_play/media_played | rule/media_play/media_play/media_played | ✅ | 345.0 | 否 |
| media-02 | 换一段 | rule/media_control/media_next/media_played | rule/media_control/media_next/media_played | ✅ | 345.0 | 否 |
| media-03 | 停止播放 | rule/media_control/media_stop/media_stopped | rule/media_control/media_stop/media_stopped | ✅ | 345.0 | 否 |
| media-04 | 黄梅戏点播 | rule/media_play/media_play/media_played | rule/media_play/media_play/media_played | ✅ | 345.0 | 否 |
| multi-01 | 点播加提醒吃药 | agent/other/media_play,medication_query/multi_done | agent/other/media_play,medication_query/multi_done | ✅ | 550.0 | 否 |
| multi-02 | 问时间加点播 | agent/other/time_get,media_play/multi_done | agent/other/time_get,media_play/multi_done | ✅ | 550.0 | 否 |
| multi-03 | 通知家属加点播 | agent/other/notify_family,media_play/multi_done | agent/other/notify_family,media_play/multi_done | ✅ | 550.0 | 否 |
| safe-01 | 胸痛安全兜底 | safety/other/—/safety | safety/other/—/safety | ✅ | 320.0 | 否 |
| clar-01 | 域外问题澄清 | classifier/other/—/clarify | classifier/other/—/clarify | ✅ | 320.0 | 否 |
| deg-01 | 天气服务降级 | rule/weather/weather_get/degraded | rule/weather/weather_get/degraded | ✅ | 345.0 | 是 |
| deg-02 | 媒体音源缺失 | rule/media_play/media_play/degraded | rule/media_play/media_play/degraded | ✅ | 345.0 | 是 |

## 4. 失败分层归因

| id | 归因层级 | 证据字段 | 建议下一步 |
| --- | --- | --- | --- |
| mem-03 | llm | llm:tools=— expected=memory_recall；recall:outcome=agent_reply expected=memory_referenced | 核对 Agent 工具选择提示与 fake 计划的一致性 |

## 5. 与 Day6 路由基线的关系

Day6 基线测的是「路由判定对不对」（分层/意图/敏感/延迟/成本），本报告测的是「事情办成没有」（任务成功、工具顺序、人工接管）。两者分母不同（Day6 为单句样例，本报告为 1-3 轮场景），不能直接比较数字，只能对照看趋势：路由层稳定是任务成功的前提，不等于任务完成。

## 6. 局限与复现

- 模式 `offline`：LLM 为固定 JSON 的 fake，工具为内存隔离实现，ASR/TTS 不接入；延迟为固定模拟常量，只用于回归对比，不代表线上性能；token 成本记 0。
- 场景为人工构造且与规则关键词同源，衡量的是回归稳定性，不是开放域泛化。
- 失败场景已写入 `eval/fixtures/day7_badcases.jsonl`（按 id 去重），本段只归因不修复。
- 复现：`.\.venv\Scripts\python.exe scripts\eval_day7.py --out eval\reports\v1_baseline.md`；gate：`.\.venv\Scripts\python.exe scripts\eval_gate.py --report eval\reports\v1_baseline.md`。

<!-- eval_metrics {"mode": "offline", "samples": 20, "task_success_rate": 0.95, "sensitive_recall": 1.0, "tool_accuracy": 0.9444444444444444, "human_takeover_rate": 0.25, "s1_closed_loop_rate": 0.6538461538461539, "p95_latency_ms": 550.0, "token_cost": 0} -->
