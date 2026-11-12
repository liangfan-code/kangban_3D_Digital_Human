# Day7 段6 规格：娱乐点播真实播放链路 + media_history

> 依据：`docs/Day7-改造提示词.md` 段 6（932–1058 行）、`AGENTS.md`、当前代码实况。
> 本段只做长者端点播；不做后台管理页、榜单、推荐算法。禁止 git commit/push。

## 1. 目标

1. 新增 `media_history`（迁移 `0009` + ORM），记录点播开始/结束与完成度。
2. 新增媒体服务：授权 + active + genre/偏好选片，返回应用内相对 URL。
3. 规则层 `R01/R04/R05` 命中后真正执行媒体工具：一次出声、零模型、落库。
4. 前端 `<audio>` 真播放；自动播放被拒时显示「点这里继续播放」。
5. 缺素材/未授权时明确降级（`degraded=media_unavailable`），不伪造歌名、不外链。

## 2. 现状核对结论（以代码为准）

- `app/models/media.py`：`MediaLibrary`，active 仅允许 `licensed/teaching/public_domain`。
- `app/models/__init__.py` 尚无 `MediaHistory`。
- 最新迁移 `alembic/versions/0008_medication_order_flow.py`；本段新增 `0009_media_history.py`，旧迁移一行不改。
- `app/agent/tools.py`：`ToolContext(elder_id, device_id, room_type)`；`media_play/next/stop` 只返回固定话术 + 控制事件，不查库。`_elder_uuid(ctx)` 可复用。
- `app/router/rules.py`：R01 无 `tool`，R04 无 `tool`，R05 已有 `tool="media_play"`。
- `app/router/executor.py#execute_rule`：media_play/media_next/media_stop 被本地话术短路。
- `app/services/session_state.py`：`MediaSlot(action, keyword)`；`get_session_store().mutate(thread_id)` 可异步读改写。
- `app/services/voice.py` 两处构造 `ToolContext`（文字通道约 599 行、语音通道约 1445 行），均无 thread_id。
- `app/api/routes/voice.py` WS 循环只处理 `resume / rtc_call_answer / ping`。
- `app/static/js/voice.js#handleControl` 只打日志；TTS 走 WebAudio；`volumeGain/gainNode` 已存在。
- `app/services/memory.py#recall(db, elder_id, query, top_k)` 无 category 参数；可在媒体服务内按 `row.category == "preference"` 轻量适配，异常一律降级。
- 测试基建：`db_session`（savepoint）、`seed["elders"]`；`tests/test_agent_tools.py` 已有 `_session_factory(db_session)` 注入会话的写法。

## 3. 数据模型与迁移

新增 `alembic/versions/0009_media_history.py`（`revision="0009_media_history"`, `down_revision="0008_medication_order_flow"`）：

- 表 `media_history`：
  - `history_id` UUID PK `server_default=gen_random_uuid()`
  - `elder_id` UUID NOT NULL FK `elders(elder_id)` ON DELETE CASCADE
  - `media_id` UUID NOT NULL FK `media_library(media_id)` ON DELETE CASCADE
  - `started_at` TIMESTAMPTZ NOT NULL DEFAULT now()
  - `finished_at` TIMESTAMPTZ NULL
  - `completion_rate` NUMERIC(4,3) NULL，CHECK `completion_rate IS NULL OR (completion_rate >= 0 AND completion_rate <= 1)`
  - 索引：`ix_media_history_elder_started (elder_id, started_at DESC)`、`ix_media_history_media (media_id)`
- `downgrade()` 只删该表（索引随表删除）。
- 文件头部注释写清用途；不夹带其他表。

新增 `app/models/media_history.py`：`MediaHistory`，字段/可空性/约束名与迁移一致；`status` 无；注册到 `app/models/__init__.py` 的 import 与 `__all__`。

## 4. 媒体服务 `app/services/media.py`

常量：`PLAYABLE_COPYRIGHT = ("licensed","teaching","public_domain")`、`DEFAULT_GENRE = "越剧"`、`RECENT_LIMIT = 20`、`INTERNAL_PREFIX = "/static/media/"`。

对外函数：

1. `async def pick_for_play(db, elder_id, genre=None) -> dict | None`
   - genre 为空时：`_preferred_genre(db, elder_id)`（记忆偏好 → 默认越剧）。
   - 候选：`status=active` + `copyright_status IN PLAYABLE_COPYRIGHT` + `media_uri` 以 `/static/media/` 开头（外链/空地址一律排除）。
   - 排序：该 elder 近 20 条 `media_history` 的 media_id 靠后；未播过的优先；同档按 `title` 稳定排序。
   - 指定 genre 无候选时回退到全部可播项。
   - 返回 `{media_id,title,genre,artist,url,duration_seconds}`（uuid 转 str，duration 转 int|None）。
2. `async def next_for(db, elder_id, current: dict | None) -> dict | None`
   - `current` 为空：等价 `pick_for_play(db, elder_id)`。
   - 优先同 genre 循环下一条；同 genre 只有当前一条时回退到其他可播项（排除当前）；仍无则 None。
3. `async def record_start(db, elder_id, media: dict) -> UUID`
   - 插入 `media_history`（`started_at=now(UTC)`），`flush()`，返回 `history_id`；不提交。
4. `async def record_end(db, elder_id, history_id, *, completed: bool, elapsed_seconds: float | None = None) -> bool`
   - 查询必须同时带 `history_id` 与 `elder_id`，查不到返回 `False`（越权/伪造即拒绝）。
   - `finished_at=now(UTC)`；`completed=True` → `Decimal("1.000")`；否则读取 `media_library.duration_seconds`，有时长与 elapsed 时 `min(1,max(0,elapsed/duration))` 保留 3 位，否则 `None`。
   - `flush()`；不提交。

内部：
- `_preferred_genre`：`memory_service.recall(db, elder_id, "喜欢听的戏曲 老歌 电影")`，仅看 `category == "preference"` 的正文，命中已知品类词（越剧/京剧/黄梅戏/沪剧/评弹/昆曲/老歌/电影）即返回；异常/无命中返回 `DEFAULT_GENRE`，只记 `event=media_preference_degraded`（不记正文）。
- `_recent_media_ids`：按 `started_at DESC` 取该 elder 最近 20 条。
- 日志只记 `media_id/elder_id/genre/count`，不记音频内容与未授权地址。

## 5. 会话状态

`app/services/session_state.py#MediaSlot` 追加可选字段（默认 None，旧 Redis 数据可反序列化）：
`media_id: str | None`、`title: str | None`、`history_id: str | None`、`genre: str | None`。

## 6. 工具层 `app/agent/tools.py`

- `ToolContext` 追加 `thread_id: str | None = None`（服务端注入）。
- `app/services/voice.py` 两处构造 `ToolContext` 时补 `thread_id=str(thread_id) if thread_id else None`。
- 三个媒体工具改为 DB 短会话（`async with get_session_factory()() as session`），异常/无素材均返回可朗读降级，不向上抛。
- `media_play`：
  - `_elder_uuid(ctx)` 为空 → `ok=False`, `degraded="missing_elder_context"`，话术「我先确认一下您是哪位，再帮您放戏。」
  - `pick_for_play` → 无结果 → `ok=False`, `degraded="media_unavailable"`，话术「这段暂时没有音源，我帮您记着，等会儿再试试。」
  - `record_start` + `commit` → 更新会话 `MediaSlot`（thread_id 存在时）→ 返回话术「好，给您放一段{genre}《{title}》。 」
  - `client_events=[{"type":"control","action":"media_play","media_id","title","genre","url","duration_seconds","history_id"}]`
- `media_next`：
  - 读当前 `MediaSlot`；旧 `history_id` 先 `record_end(completed=False)`（无 elapsed）。
  - `next_for(db, elder_id, current)` → `record_start` → 更新槽位 → 同样的 control 事件（action=`media_next`）。无素材/无身份降级同 `media_play`。
  - `args.genre` 可作为兜底（模型主动指定时）。
- `media_stop`：
  - 有 `history_id` 则 `record_end(completed=False, elapsed_seconds=args.elapsed_seconds)`；清空槽位媒体字段（保留 action/keyword 由语音层维护）。
  - 事件 `{"type":"control","action":"stop"}`，话术「好，我停下了。」；无 elder 也允许停（不落库）。
- 会话读写失败（Redis/DB）只记警告，不阻断话术。

## 7. 路由让位

- `app/router/rules.py`：R01 补 `tool="media_next"`；R04 补 `tool="media_stop"`；R05 不动。正则/意图/顺序不改。
- `app/router/executor.py#execute_rule`：函数开头 `if decision.tool: return None`；删除已不可达的 media_play/media_next/media_stop 分支。保留 `volume_up/volume_down/media_defer/time_query/farewell` 确定性话术。
- `app/services/voice.py` 规则层已有 `if decision.tool` 分支，无需改判定顺序。

## 8. 前端 `app/static/js/voice.js`

新增模块级：`mediaAudio = null`、`mediaHistoryId = null`、`mediaUnlockBtn = null`。

- `handleControl`：
  - `media_play` / `media_next`：复用/创建 `<audio>`（`preload="auto"`），`volume=0.8`，`src=ev.url`，`play()`。
    - 成功：记录 `mediaHistoryId=ev.history_id`，日志「🎵 正在播放：{title}」。
    - 被拒：显示固定按钮「▶ 点这里继续播放」，点击后重试并隐藏；不静默失败。
    - 不操作麦克风轨（长者需在播放中说「停/换一段」）。
  - `stop`：暂停、`src=""`、清 `mediaHistoryId`（先发送 `media_event/stopped` 再清）。
  - `volume_up/down`：原有 GainNode 逻辑保留，同时按 ±0.2 调整 `mediaAudio.volume`（夹在 0..1）。
- `audio.ended`：发送 WS `{"type":"media_event","action":"ended","history_id":...}`，日志「🎵 播放结束」。
- 用户说「停」由服务端下发 stop；前端在 stop 时发送 `{"type":"media_event","action":"stopped","history_id":...,"elapsed_seconds":currentTime}`。
- 不改 TTS/口型/音量既有逻辑；不引入框架。

## 9. WS 媒体事件 `app/api/routes/voice.py`

- 连接建立时保留 `view` 与 `voice_session`。
- 新增 `async def _handle_media_event(event, session_factory, allowed_elder_ids, verified_elder_id)`：
  - `action in ("ended","stopped")`；`history_id` 必填。
  - 目标 elder：单人间 `view["elder"].elder_id`；双人间优先 `voice_session.verified_elder_id`；不在 `allowed_elder_ids` 内 → 只记 `event=media_event_forbidden` 警告，返回。
  - `completed = action == "ended"`；`elapsed_seconds` 仅接受 0–86400 的数字。
  - 用 `get_session_factory()` 新短会话 `record_end(...)` 后 `commit`；异常只记 `media_event_failed` 警告；不触发 TTS。
- 循环中新增 `elif event.get("type") == "media_event": await _handle_media_event(...)`。

## 10. 种子脚本

新增 `scripts/seed_media_library.py`：
- `--manifest` 默认 `scripts/media_manifest.example.json`；`--allow-placeholder`；`--placeholder-count` 默认 24；`--database-url` 可选（默认读 Settings）。
- 清单字段：`title/type/genre/artist/era/copyright_status/file/duration_seconds`。
- 文件必须存在于 `app/static/media/library/` 且清单 `file` 不得含 `..`/URL；存在 → `media_uri="/static/media/library/<file>"`, `status=active`；缺失 → `status=hidden` 并打印错误，最终非零退出。
- `--allow-placeholder`：本地用 `wave` 生成 6–8 秒 16k/16bit 单声道正弦音 WAV，`copyright_status=teaching`，`type=opera`，标题前缀「占位音源-」，`duration_seconds` 真实写入；不下载、不伪装真实戏曲。
- 幂等：按 `title` 查已有行并更新，不重复插入；不写长者数据。
- 新增清单示例 `scripts/media_manifest.example.json`（0 条真实素材也可运行；示例字段齐全）。
- `.gitignore` 增加 `app/static/media/library/`。

## 11. 测试

新增 `tests/test_media_service.py`（`db_session` + `seed`）：
1. 授权过滤：`unknown/expired/restricted` 与 `media_uri` 为外链的不可播。
2. genre 优先：指定「京剧」只返回京剧；无候选回退全部。
3. 偏好命中：monkeypatch `_preferred_genre` 返回「黄梅戏」→ 选中黄梅戏。
4. next 循环：同 genre 两条时从第一条切到第二条；只有一条时回退其他品类。
5. history 写入与结束更新：`record_start` 后有行；`record_end(completed=True)` → `1.000`；`completed=False + elapsed` 按比例；无时长留空。
6. 越权 history：elder B 更新 elder A 的 history 返回 False 且不改行。
7. 无素材：全部 hidden → `pick_for_play is None`。

更新 `tests/test_router_rules.py`：
- R01/R04 decision 带 `tool`，`execute_rule` 返回 None。
- `test_execute_media_stop_emits_control` 改为让位断言。
- `test_execute_volume_and_genre` 拆为：音量仍返回事件；点播 decision 带 tool 且 execute_rule 为 None。

更新 `tests/test_agent_tools.py`：
- `test_media_play_emits_control_event` 改为：无 elder → `missing_elder_context`；有 elder + active 素材 → control 事件含 `url/history_id`，`media_history` 新增一行。
- 新增 stop 终结合集：play 后 stop → `finished_at` 非空。
- 会话状态用 `SessionStateStore(force_memory=True)` 注入，避免 Redis 抖动。

## 12. 验收

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_media_service.py tests/test_router_rules.py tests/test_agent_tools.py -o addopts="" -q
.\.venv\Scripts\python.exe -m pytest -o addopts="" -q
.\.venv\Scripts\python.exe -m ruff check app/services/media.py app/agent/tools.py app/router/executor.py app/router/rules.py app/api/routes/voice.py app/models/media_history.py scripts/seed_media_library.py tests/test_media_service.py tests/test_router_rules.py tests/test_agent_tools.py
.\.venv\Scripts\python.exe scripts\run_day6_golden.py
# 迁移（人工审查后）
$env:DATABASE_URL="postgresql+asyncpg://.../kangban_test"; .\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic downgrade 0008_medication_order_flow
```

手工链路：语音说「我想听越剧」→ 规则层 R05 → `<audio>` 出声 → `media_history` 新增；「换一段」切同品类且旧记录补 `finished_at`；「停」停止并补结束。

## 13. 风险与素材缺口

- 仓库无授权戏曲素材：只能生成占位音源验证链路；真实素材需用户提供「授权证明 + 文件名 + 标题/品类/时长」清单。
- 远程 embedding 未配置时偏好召回走本地哈希，只影响偏好命中率，不影响播放。
- 双人间记录在声纹免验过期后拒绝 `media_event`，属预期安全收紧。
- 静态服务不支持 Range 不阻塞验收（整文件渐进播放）。
