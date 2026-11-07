# 会话上下文工程补齐计划（Redis 状态 + 早期摘要 + 结构化槽位）

## Summary
在现有「最近 5 轮原文」上下文基础上，补齐三件事：
1. Redis 保存会话状态（thread_id 维度，含轮次、最后活跃、摘要游标、槽位），Redis 不可用时自动降级为进程内字典；
2. 超过 10 条消息后，把更早的对话滚动压缩成 ≤150 字摘要，注入 system prompt；
3. 规则化抽取 3 类结构化槽位（用药确认、戏曲/媒体控制、身份确认），随状态注入 prompt，供后续 Day5+ 工具层使用。

不改数据库结构，不改前端，不改变身份铁律（槽位永不修改 `verified_elder_id`）。

## Current State Analysis
- 上下文组装：[voice.py](file:///c:/Users/lmxyrqlxr/Desktop/kangban_3D_Digital_Human-main/app/services/voice.py#L831-L835) 仅 `system=人设+动作协议` + 最近 10 条消息；无摘要、无状态。
- 历史读取：[conversation_store.py](file:///c:/Users/lmxyrqlxr/Desktop/kangban_3D_Digital_Human-main/app/services/conversation_store.py#L96-L109) 只提供 `recent_messages`，没有「更早消息」查询。
- Redis：`redis>=5.2` 已在 [pyproject.toml](file:///c:/Users/lmxyrqlxr/Desktop/kangban_3D_Digital_Human-main/pyproject.toml#L28) 声明，venv 实装 8.1.0；`redis_url` 在 [config.py](file:///c:/Users/lmxyrqlxr/Desktop/kangban_3D_Digital_Human-main/app/core/config.py#L31) 已存在但全项目无任何 Redis 调用。
- 本机无 Redis 服务（`127.0.0.1:6379` 不可达，Docker 仅有 `kangban-pg` 与 `funasr`）。
- 生命周期：[lifespan.py](file:///c:/Users/lmxyrqlxr/Desktop/kangban_3D_Digital_Human-main/app/core/lifespan.py#L12-L20) 只释放数据库引擎，无缓存关闭钩子。
- 现有测试 192 passed / 2 skipped；`tests/test_conversation_store.py` 已覆盖 thread 复用与最近 N 条读取。

## Proposed Changes

### 1. 新增 `app/services/session_state.py`（Redis 状态 + 降级 + 原子更新）
**做什么**
- Pydantic 模型：
  - `SessionState`：`thread_id`、`summary`、`summary_upto_seq`、`turn_count`、`last_active`、`slots`。
  - `SessionSlots`：`intent`、`medication`（drug/time/confirmed）、`media`（action/keyword）、`identity`（candidate/confirmed）。
- `SessionStateStore`：
  - 键 `kangban:session:{thread_id}`，值为 JSON，TTL 读 `session_state_ttl_seconds`（默认 86400 秒）。
  - 懒加载 `redis.asyncio.from_url`，`socket_connect_timeout=0.5`、`socket_timeout=0.5`、`decode_responses=True`；首次 `ping` 失败后置 `_redis_ok=False`，只记一次 `session_state_redis_unavailable` 日志，后续走进程内字典。
  - `load(thread_id)` / `save(state)`；`mutate(thread_id)` 异步上下文：进程内 `asyncio.Lock` 内读-改-写，降低并发覆盖。
  - 模块级 `get_session_store()` 单例、`close_session_store()`；测试可传 `force_memory=True` 不碰 Redis。
- 任何 Redis 异常都吞掉并降级，绝不影响通话（符合 AGENTS「缺配置照常启动」）。

**为什么**：把「进行中状态」从单次 WebSocket 对象里搬出来，支持断线重连、多入口与快速读取；同时给摘要游标一个稳定落点。

### 2. 新增 `app/services/slot_extractor.py`（规则槽位）
**做什么**
- `extract_slots(text, previous) -> SessionSlots`，只做确定性规则，不额外调模型：
  - 用药：命中 `药/吃药/服药/用药/提醒`；药品 `([\u4e00-\u9fffA-Za-z0-9]{1,10})(药|片|胶囊)`；时间 `([零一二三四五六七八九十两0-9]{1,3})\s*(点|时|:：)`；确认词（好/对/是/确认/吃过了）→ `confirmed=True`，否定词（不/不用/取消/等会）→ `False`。
  - 媒体：播放/暂停/停/换一段/下一段/大声/小声/来一段/戏曲/评书 → `media.action` 与 `media.keyword`。
  - 身份：`我是(.{2,4})` → `identity.candidate`；确认/否定词更新 `confirmed`。
- 合并策略：只覆盖本轮识别到的字段，其余沿用 `previous`。
- 明确不执行任何动作，不修改 `verified_elder_id`，只把结果写进状态供 prompt 与后续工具层使用。

**为什么**：让「提醒我八点吃降压药」「换一段评书」这类多轮任务有机器可读状态，而不是全靠模型猜。

### 3. 新增 `app/services/context_summary.py`（早期摘要）
**做什么**
- `build_context_message(state)`：把非空摘要与槽位拼成一段紧凑的 `【会话摘要】【当前状态】` 文本；全空返回 `None`。
- `refresh_summary(db, thread_id, state)`：
  - 查该 thread 的最大 `seq`；若 `<= HISTORY_MESSAGE_LIMIT(10)` 直接返回。
  - 取 `seq <= max_seq - 10` 且 `seq > state.summary_upto_seq` 的消息，升序最多 20 条。
  - 调 `complete_chat`（新增非流式方法）把「旧摘要 + 新增旧消息」合并成 ≤150 字中文摘要；固定提示词要求只保留称呼/偏好/健康主诉/约定事项。
  - 成功才更新 `summary` 与 `summary_upto_seq`；失败（`LlmUnavailable`）保留旧摘要、不推进游标，下一轮重试。
  - 用 `store.mutate` 只合并摘要字段，避免覆盖槽位/轮次。
- 摘要内容不写日志，仅记录长度与耗时。

**为什么**：解决第 6 轮后早期信息被硬截断的问题，满足「10 轮不爆、记得前面称呼」的验收方向。

### 4. 修改 `app/integrations/llm.py`：新增非流式 `complete_chat`
- 复用现有配置与鉴权头，`body={"model","messages","stream":False}`，透传 `reasoning_effort`，支持 `max_tokens`。
- 失败统一抛 `LlmUnavailable`；测试可注入 `httpx.MockTransport`。

### 5. 修改 `app/services/voice.py`：接入三件套
- `_reply_via_llm`（L831 起）：
  1. `thread_id, history = await _load_context(...)`；
  2. `state = await self._prepare_state(thread_id, final_text)`（内部 `store.mutate`：抽槽、`turn_count += 1`、`last_active`）；
  3. `messages = [{"role":"system","content": _build_system_prompt() + 动态上下文}] + history + user`；动态上下文为空时保持现状。
- `_reply_via_template`（L857 起）：同样落状态与槽位（不注入 prompt）。
- `_save_turn` 成功后调用 `_schedule_summary(thread_id)`：`asyncio.get_running_loop().create_task`，绑定 uvicorn 事件循环；单个会话同时只允许一个摘要任务；任务内自建 DB session，异常只记日志。
- `_handle_verification` 不变，安全边界不动。

### 6. 配置与生命周期
- `app/core/config.py` 新增：`session_state_enabled: bool = True`、`session_state_ttl_seconds: int = 86400`、`session_summary_max_chars: int = 150`、`session_summary_batch: int = 20`。
- `app/core/lifespan.py` 关闭时 `await close_session_store()`。
- `.env.example` 在 Redis 段后补 3 个占位：`SESSION_STATE_ENABLED=1`、`SESSION_STATE_TTL_SECONDS=86400`、`SESSION_SUMMARY_MAX_CHARS=150`。

### 7. 新增 `compose.redis.yaml`
- `redis:7-alpine`，容器名 `kangban-redis`，端口 `6379:6379`，AOF 持久化 + volume + `redis-cli ping` 健康检查。
- 本机当前无 Redis，该文件让功能可真实启用；不修改 `compose.funasr.yaml`，不碰数据库容器。

### 8. 测试
- `tests/test_session_state.py`：内存模式默认状态、保存/读取回环、槽位 JSON 序列化、`build_context_message` 空/非空。
- `tests/test_slot_extractor.py`：用药（药名+时间+确认）、媒体（换一段评书）、身份（我是王奶奶）、否定词、空文本保持原值。
- `tests/test_context_summary.py`：用 `db_session` + `seed` 造 8 轮消息；monkeypatch `complete_chat` 返回固定摘要，断言只摘要最近 10 条之前的内容、游标推进；LLM 失败时摘要与游标不变；≤5 轮不触发。
- `tests/test_llm_complete.py`：`MockTransport` 验证 `complete_chat` 解析与 `reasoning_effort` 透传、非 200 抛 `LlmUnavailable`。

## Assumptions & Decisions
- **不建新表**：状态与摘要都放 Redis，TTL 24h；重启丢失可接受，长期记忆留给 Day7。
- **摘要用 LLM**：与现有 DeepSeek 配置共用；失败软降级，保留旧摘要，不阻断回复。
- **槽位规则化**：不新增 LLM 调用、不增加语音时延；先覆盖 3 类高频意图，Day6 工具层再扩展。
- **系统提示词拼接方式**：动态摘要/槽位接在人设之后、同一 system 消息内，兼容性最好；代价是 prompt 缓存前缀部分失效，可接受。
- **Redis 可选**：未启动 Redis 时自动进程内降级，应用照常启动；但多 worker 不共享状态，需在文档中说明。
- **安全**：槽位只作为上下文，不改变身份判定；`elder_id` 仍只来自服务端设备/声纹。
- 唯一新增依赖文件是 `compose.redis.yaml`；不新增 Python 包。

## Verification
1. `pytest -q -p no:warnings`：原 192 项 + 新增用例全部通过。
2. `ruff check app tests`：无新增告警。
3. `docker compose -f compose.redis.yaml up -d` 后 `docker exec kangban-redis redis-cli ping` 返回 `PONG`。
4. 启动应用，`GET /health` 200；用 `docker exec kangban-redis redis-cli keys 'kangban:session:*'` 能看到会话键，`GET` 内容含 `summary`/`slots`。
5. 停掉 Redis 再启动应用：`/health` 仍 200，日志出现一次 `session_state_redis_unavailable`，通话走内存降级。
6. 语音联调：连续对话超过 5 轮后，检查 Redis 中 `summary` 非空、`summary_upto_seq` 推进；说「提醒我八点吃降压药」后槽位出现 `intent=medication`、`drug`、`time`。
