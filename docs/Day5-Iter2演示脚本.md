# Day5 · Iter 2 验收演示脚本

> 对应大纲课时 5.3：完整演示"对平板说话 → 数字人用长者语气回答 → 嘴在动 → 状态条在变"，
> 并附一个可见降级、一张真实延迟基线表。全程约 5 分钟。
> 演示通过后再打 tag `iter2-demo`（本轮不提前打）。

## 一、演示前检查（T-30 分钟）

按顺序逐项确认，任何一项失败先修复再开始，不要带着问题上台。

| 检查项 | 命令 / 动作 | 期望结果 |
| --- | --- | --- |
| PostgreSQL | `docker ps --filter name=kangban-pg` | 状态 `Up`，宿主机端口 `5433` |
| Redis（会话状态） | `docker ps --filter name=kangban-redis`；`docker exec kangban-redis redis-cli ping` | `PONG`（未启动也可演示，但会退化为进程内状态） |
| FunASR | `docker ps --filter name=funasr` | 状态 `Up`，端口 `10096`（容器名当前为 `funasr`，compose 重建后叫 `kangban-funasr`） |
| 声纹模型 | 确认 `models/` 下有 CAM++ 与 `ten-vad.onnx` | 后端启动日志无 `voiceprint_provider_unavailable` |
| 环境变量 | `.env` 中 `LLM_API_KEY/LLM_BASE_URL/LLM_MODEL`、`FUNASR_URL`、`SHERPA_MODEL_DIR` 已填 | 启动日志无 `llm_fallback` 的配置告警 |
| 后端 | `.\.venv\Scripts\python.exe -m uvicorn app.main:app` | `GET /health` 返回 200 |
| 浏览器 | 打开 `/voice.html`，允许麦克风；确认 3D 模型加载完成 | 徽标显示"已连接"，页面无报错 |
| 声纹 | 提前用同一设备完成一次声纹录入 | 随后首句能通过 `verification`，状态条变"身份已确认" |
| 设备 Token | 确认演示设备 Token 未过期（长期 Token 默认 90 天） | WS 不返回 4401/4403 |
| 音量 | 扬声器音量适中，避免回声；麦克风靠近长者 | 转写无"回声复读" |

> 演示前建议重启一次后端，确保熔断状态、声纹缓存都是干净初始态。

## 二、5 分钟时间轴

| 时间 | 操作 | 屏幕/语音预期 | 讲解要点 |
| --- | --- | --- | --- |
| 0:00–0:30 | 展示平板与 3D 数字人，说明场景 | 数字人待机呼吸动画 | "CCRC 社区长者平板上的陪伴入口，不是手机 App" |
| 0:30–2:00 | 点"开始对话"，说：**"小伴你好"** | 首句做声纹：`verification` 通过 → 状态条"身份已确认" → 数字人挥手（`action wave`）→ 语音回答 | 身份铁律：`elder_id` 由设备/声纹在服务端确定，前端与模型都传不进来 |
| 2:00–3:00 | 说：**"今天天气怎么样？"** | 字幕逐字出现 → 嘴型随 TTS 音频动 → 整句朗读完成 | LLM 流式分句 + TTS 逐句下发 + viseme 时间轴驱动 blendshape |
| 3:00–3:45 | 说：**"你点点头，我说得对吧"** 或 **"给我讲个笑话"** | 出现"🎬 动作 nod/笑"日志 → 数字人点头/鼓掌 | 动作标记 `[act:*]` 服务端剥净后单独下发，不朗读、不进字幕 |
| 3:45–4:30 | **降级演示**（见第三节） | 页面出现可见提示 + 文字回复，链路不白屏 | 外部依赖失败必须"看得见"，不允许静默失败 |
| 4:30–5:00 | 切到终端展示 `turn_latency` 日志与基线表 | `a2a_ms`、`llm_first_token_ms`、P95 | "延迟数字是跑出来的，不是估出来的；熔断保证厂商故障时不干等" |

## 三、降级演示（二选一，提前演练）

**方案 A：断 FunASR（推荐，恢复快）**

1. `docker stop funasr`；
2. 对平板说一句话；
3. 预期：页面出现"语音识别服务暂不可用，请使用文字输入"，并展开文字输入兜底；
4. `docker start funasr` 恢复（若容器名已变为 `kangban-funasr`，替换为实际名称）。

**方案 B：LLM 不可达（演示熔断）**

1. 把 `.env` 的 `LLM_BASE_URL` 改为不可达地址并重启后端；
2. 连续说 3 轮，预期日志依次出现 `llm_fallback` → `circuit_state_changed`（to=open）；
3. 第 4 轮预期立刻返回模板回复（`turn_latency` 的 `degraded` 含 `llm_circuit_open`），不再等待厂商超时；
4. 恢复 `.env` 并重启后端（熔断状态仅在进程内，重启即复位）。

## 四、翻车预案

| 现象 | 最可能原因 | 当场处理 | 话术 |
| --- | --- | --- | --- |
| WS 连不上 / 徽标"鉴权失败" | 设备 Token 过期或用成人 JWT | 后台重新签发设备 Token，刷新页面 | "设备身份校验保护，换一张有效设备卡即可" |
| 首句一直"声纹未通过" | 环境噪声、离麦克风远、未录入声纹 | 让长者靠近再说一次；或管理员重新录入 | "系统在做身份确认，避免把两位长者的记录弄混" |
| 转写重复自己说的话 | 扬声器回声被麦克风回拾 | 调低音量/拉开距离；服务端回声防护窗会自动丢弃 | 直接说明这是防自激设计 |
| 有文字无声音 | TTS 失败或超时 | 看终端 `tts_failed`；文字已在屏幕，继续演示 | "语音服务抖动时会自动切文字，不影响沟通" |
| 3D 不加载 / 嘴不动 | 模型资源未加载、WebGL 不可用 | 刷新；仍失败则只演语音链路 | "3D 是表现层，语音与身份链路独立，降级不影响核心功能" |
| 大模型答非所问 | LLM 返回异常 | 换一句简单指令；必要时按方案 B 演示熔断兜底 | "复杂问题会交给更强的模型，今天演示基础陪伴链路" |

## 五、延迟基线采集（演示后完成）

目标：真实跑 20 轮"今天天气怎么样"，用脚本算 P50/P95，数字必须可复算。

```powershell
# 1) 建日志目录（已在 .gitignore，不会入库）
New-Item -ItemType Directory -Force logs | Out-Null

# 2) 把后端 stdout 落到文件（另开终端操作页面）
.\.venv\Scripts\python.exe -m uvicorn app.main:app 2>&1 | Tee-Object -FilePath logs\day5_latency.log

# 3) 说 20 轮"今天天气怎么样"，然后 Ctrl+C 停服

# 4) 生成基线表（直接写入 docs）
.\.venv\Scripts\python.exe scripts\latency_report.py logs\day5_latency.log --out docs\Day5-延迟基线.md
```

记录表需注明采集环境（本机 CPU/是否走真实厂商/模型与音色），例如：

| 项目 | 值 |
| --- | --- |
| 采集时间 | 2026-11-__ __:__ |
| LLM | DeepSeek `deepseek-flash`，`reasoning_effort=low` |
| TTS | edge-tts `zh-CN-XiaoxiaoNeural` |
| 样本 | 20 轮"今天天气怎么样" |

> 大纲基线目标 P95 ≤ 1.5s。当前全走大模型大概率达不到，**如实记录作为基线**，
> Day6 接入规则层/分类器后再对比，不得手改数字。

## 六、演示后动作

1. 演示通过、基线表落档后，由用户确认再执行：

   ```powershell
   git tag -a iter2-demo -m "Iter 2 语音闭环演示：口型同步 + 延迟打点/熔断"
   ```

2. 记录今天新增的坑（若有）到 `AGENTS.md` 的实时模块规则，完成"三次法则"第一次实战。
