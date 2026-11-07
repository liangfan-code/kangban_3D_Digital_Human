# 本机运行环境与大模型接入准备 Spec

## Why
当前项目仅有源码：没有 `.venv`、没有 `.env`、本机没有运行中的 PostgreSQL，FunASR 容器也非按本项目 compose 启动。经核查，**3D 数字人链路已经完整接入且为纯前端实现**（`app/static/avatar/boot.js` + `engine.js`、本地 `/vrm/models/*.vrm` 与 `/vendor` 三维资源、`voice.html` 的 TTS→`KangbanAvatar.attachStream` 口型桥、`elder.html` 懒加载卡片），后端无需新增 3D 接口。因此本轮工作聚焦于：把依赖、数据库、容器、配置链路打通，并验收数字人渲染与语音联动，保证用户补上 `LLM_*` 配置后功能立即可演示。

## What Changes
- 创建 `.venv` 并安装依赖（含 `dev`、`infra` 扩展组），确认 Python 3.13 下 `aiortc`/`sherpa-onnx`/`asyncpg` 等二进制依赖可安装（`uv.lock` 已含 cp313 wheel）
- 由 `.env.example` 生成本地 `.env`（保持不入 Git）：本地数据库串、随机 `JWT_SECRET_KEY`、`FUNASR_URL`、LLM 占位值
- 启动本地 `pgvector` 容器（`kangban-pg`，端口 5433），创建 `kangban_dev`、`kangban_test` 与 `vector` 扩展
- 执行 `alembic upgrade head`（0001–0006 共 6 版迁移），运行 `scripts/seed_admin.py` 创建管理员
- 启动 `uvicorn app.main:app`，冒烟验证 `/health`、登录与 8 个业务页面
- **3D 数字人验收（无代码改动）**：校验 `/vrm/models/Yoimiya.vrm`、`/vendor/three/*`、`/vrm/animations/common/*.vrma` 可访问；浏览器中点开 `avatar.html` 与 `voice.html` 确认模型加载、待机动画、可切换形象；`elder.html` 点击卡片懒加载成功；TTS 可用时验证说话态/口型切换，TTS 不可用时验证仅降级文案
- 复用本机已运行的 FunASR 容器（映射 10096）或以 `compose.funasr.yaml` 启动 `kangban-funasr`，验证 WSS 就绪；模型目录 `funasr-runtime-resources/models` 首次下载耗时不计入交付阻断项
- 验证 LLM 未配置时的可见降级：`/api/v1/config/flags` 返回 `ai_analysis_enabled=false`、AI 分析 503、语音走模板回复
- 用本地 mock 的 OpenAI 兼容接口验证“配置 LLM 后”的完整链路（非流式分析 201 + 流式 SSE 增量），验证后恢复占位配置
- 不改动任何业务代码、3D 引擎、静态资源、迁移脚本与依赖声明；不引入新的 docker-compose 文件

## Impact
- 受影响能力：本机启动、数据库迁移、后端接口冒烟、3D 数字人渲染与口型联动验收、LLM 接入链路、语音链路降级
- 受影响文件/系统：新增 `.env`（gitignored）、`.venv/`（gitignored）、Docker 容器 `kangban-pg`；`app/**`（含 `app/static/avatar/**` 与 `vrm/**`）只读不改；`tests/**` 只运行不修改

## ADDED Requirements

### Requirement: 本机依赖可安装
系统 SHALL 在 Python 3.12+ 环境下创建独立虚拟环境并安装 `pyproject.toml` 声明的全部依赖，`import app.main` 成功。

#### Scenario: 依赖安装完成
- **WHEN** 在项目根目录创建 `.venv` 并安装依赖
- **THEN** `python -c "import app.main"` 无异常，`uvicorn`、`alembic`、`pytest` 可执行

### Requirement: 本地配置就绪
系统 SHALL 通过 `.env` 提供本地运行配置，且 `.env` 不被纳入版本控制。

#### Scenario: 配置文件生成
- **WHEN** 由 `.env.example` 复制生成 `.env` 并填写本地值
- **THEN** `git status` 不显示 `.env`；`get_settings().app_env == "local"`；`DATABASE_URL` 指向本机 5433 的 `kangban_dev`

### Requirement: 本地数据库就绪
系统 SHALL 提供可用的本地 PostgreSQL + pgvector 实例，并完成全部迁移与种子数据。

#### Scenario: 迁移与种子成功
- **WHEN** 启动 `kangban-pg` 容器后执行 `alembic upgrade head` 与 `python scripts/seed_admin.py`
- **THEN** 迁移无报错，`kangban_dev` 表结构与 ORM 一致，管理员账号创建成功（重复执行幂等跳过）

### Requirement: 应用本机可启动
系统 SHALL 能在 `http://127.0.0.1:8000` 正常启动并提供页面与接口。

#### Scenario: 健康检查与页面冒烟
- **WHEN** 启动 `uvicorn app.main:app`
- **THEN** `GET /health` 返回 200 且 `status=ok`；`/index.html` 与 8 个业务页面均返回 200；管理员登录可获得 JWT

### Requirement: 3D 数字人渲染与联动可用
系统 SHALL 使用本地静态资源渲染 3D 数字人，并在语音链路可用时把 TTS 音频接入口型/说话态；任一外部依赖缺失时 SHALL 保持渲染并给出可见降级。

#### Scenario: 静态资源与渲染
- **WHEN** 浏览器打开 `/avatar.html` 或 `/voice.html`
- **THEN** `/vrm/models/Yoimiya.vrm`、`/vendor/three/three.module.min.js`、`/vendor/three-vrm*/…`、动画 `.vrma` 均返回 200；页面无 `Failed to resolve module specifier "three"`；模型加载完成并播放待机动画；`elder.html` 点击数字人卡片后成功懒加载

#### Scenario: TTS 音频驱动口型
- **WHEN** 语音链路返回 TTS PCM 并播放
- **THEN** `KangbanAvatar.attachStream` 收到音频流，数字人从待机切到说话态；TTS 不可用时不报错，仅显示文字回复

### Requirement: 大模型接入链路可用
系统 SHALL 在配置 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` 后启用 AI 健康分析与语音 LLM 回复；未配置时 SHALL 可见降级而非报错崩溃。

#### Scenario: 未配置 LLM 时降级
- **WHEN** `LLM_*` 为空占位启动应用
- **THEN** `/api/v1/config/flags` 返回 `ai_analysis_enabled=false`；家属端 AI 分析返回 503；语音对话走模板回复并在日志记录 `llm_fallback`

#### Scenario: 配置 LLM 后功能生效
- **WHEN** 填入可用的 OpenAI 兼容 API 配置并重启应用
- **THEN** `/api/v1/config/flags` 返回 `ai_analysis_enabled=true`；家属端 AI 分析返回 201 并落库；语音回复使用 LLM 流式输出，失败时仍降级模板

### Requirement: 可选外部依赖降级可见
系统 SHALL 在 ASR/TTS/声纹未就绪时保持应用可用，并通过日志或 WS error 事件暴露原因。

#### Scenario: FunASR 未就绪
- **WHEN** FunASR WSS 不可达或模型未配置
- **THEN** 应用照常启动；语音 WS 返回可见错误事件并保留文字通道，不出现静默失败

## MODIFIED Requirements
无

## REMOVED Requirements
无
