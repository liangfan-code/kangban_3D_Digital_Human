# Day7 段6 任务清单

> 规格：`docs/day7-segment6/spec.md`。每个任务完成即勾选；最后一项为回归与汇报。
> 禁止 git commit / git push / 回改旧迁移。

- [x] T1 迁移 `alembic/versions/0009_media_history.py`：建 `media_history` 表、2 个索引、completion_rate CHECK；downgrade 只删该表；头部写用途注释。
- [x] T2 ORM `app/models/media_history.py`（`MediaHistory`）并在 `app/models/__init__.py` import + `__all__` 注册。
- [x] T3 新增 `app/services/media.py`：`pick_for_play / next_for / record_start / record_end` + `_preferred_genre / _recent_media_ids`；所有 history 查询强制 `elder_id`；外链与非授权排除；日志不记地址/音频。
- [x] T4 `app/services/session_state.py#MediaSlot` 增加 `media_id/title/history_id/genre`（默认 None）。
- [x] T5 `app/agent/tools.py`：`ToolContext` 加 `thread_id`；三个媒体工具接真实选片/历史/会话槽位；无身份、无素材、DB 异常均可见降级。
- [x] T6 `app/services/voice.py` 两处 `ToolContext(...)` 注入 `thread_id`（文字通道 + 语音通道）。
- [x] T7 `app/router/rules.py`：R01 加 `tool="media_next"`，R04 加 `tool="media_stop"`；`app/router/executor.py` 开头 `if decision.tool: return None` 并删除不可达媒体分支，保留音量/先别放/时间/告别。
- [x] T8 `app/static/js/voice.js`：`<audio>` 复用播放、media_play/media_next/stop、ended/stopped 回传、自动播放被拒显示「点这里继续播放」；音量同步 `<audio>.volume`；不动麦克风与 TTS。
- [x] T9 `app/api/routes/voice.py`：WS 处理 `media_event`，按当前设备绑定 elder 校验 history 归属后 `record_end`；未知/越权只警告；不触发 TTS。
- [x] T10 `scripts/seed_media_library.py` + `scripts/media_manifest.example.json`：清单校验、缺失文件置 hidden 非零退出、`--allow-placeholder` 生成 6–8s WAV（teaching、标题占位）；`.gitignore` 排除 `app/static/media/library/`。
- [x] T11 新增 `tests/test_media_service.py`（授权过滤/品类优先/偏好/next 循环/history 写入与结束/越权拒绝/无素材），更新 `tests/test_router_rules.py`（R01/R04/R05 让位断言）与 `tests/test_agent_tools.py`（媒体 control 事件 + 落库 + stop 补齐）；同步 `eval/fixtures/day6_three_layer.jsonl` 的 R01/R04 expected_tool。
- [x] T12 回归：目标测试 → 全量 pytest → 改动文件 ruff → `scripts/run_day6_golden.py` → 迁移 upgrade/downgrade（先人工审查）→ 汇报真实输出与素材缺口。

## 验收记录（2026-11-12）

- 目标测试：`tests/test_media_service.py tests/test_router_rules.py tests/test_agent_tools.py` → 83 passed。
- 全量：`pytest -o addopts="" -q` → **661 passed, 1 skipped**（6 条 Starlette 弃用告警为历史遗留）。
- ruff：本段改动/新增 13 个 Python 文件 → All checks passed。
- 黄金集：`scripts/run_day6_golden.py` → 122/122 通过。
- 迁移：测试库 `alembic current` → `0009_media_history (head)`；`downgrade 0008` → `upgrade head` 均成功。
- 种子：`scripts/seed_media_library.py --allow-placeholder` → `active=24 hidden=0 errors=0`（写入 dev 库；音源在 `app/static/media/library/`，已被 gitignore）。
