# Tasks

- [x] Task 1: 创建虚拟环境并安装依赖
  - [x] SubTask 1.1: 用 `uv sync --extra infra --extra dev` 创建 `.venv` 并安装（Python 3.13.9，187 包）
  - [x] SubTask 1.2: 验证 `import app.main`、`uvicorn`、`alembic`、`pytest` 可执行

- [x] Task 2: 生成本地 `.env` 配置
  - [x] SubTask 2.1: 由 `.env.example` 生成 `.env`，写入本地数据库密码与 `DATABASE_URL`/`DATABASE_URL_TEST`（127.0.0.1:5433）
  - [x] SubTask 2.2: 生成随机 `JWT_SECRET_KEY` 写入 `.env`
  - [x] SubTask 2.3: `FUNASR_URL=wss://127.0.0.1:10096`、`FUNASR_INSECURE=1`；`LLM_*` 维持空占位
  - [x] SubTask 2.4: `git check-ignore .env` 命中，`.env` 未入库

- [x] Task 3: 启动本地 PostgreSQL 与测试库
  - [x] SubTask 3.1: 启动 `pgvector/pgvector:0.8.7-pg17-bookworm` 容器 `kangban-pg`（5433→5432）
  - [x] SubTask 3.2: 创建 `kangban_dev`、`kangban_test` 并启用 `vector` 扩展
  - [x] SubTask 3.3: 验证连接与 `vector` 扩展可用

- [x] Task 4: 执行迁移与种子管理员
  - [x] SubTask 4.1: `alembic upgrade head`（0001–0006）
  - [x] SubTask 4.2: 核对表数量（22 张业务表 + `alembic_version`）
  - [x] SubTask 4.3: `seed_admin.py` 创建管理员，二次执行幂等跳过

- [x] Task 5: 启动应用并冒烟验证
  - [x] SubTask 5.1: `uvicorn app.main:app --port 8000` 启动
  - [x] SubTask 5.2: `GET /health` 200 且回传 `trace_id`
  - [x] SubTask 5.3: 9 个页面均 200
  - [x] SubTask 5.4: 管理员登录 200、`/api/v1/auth/me` 200

- [x] Task 6: 3D 数字人渲染与联动验收（只读静态资源，未改代码）
  - [x] SubTask 6.1: 7 个 VRM、20 个 `.vrma`、three/three-vrm/vendor 资源均 200
  - [x] SubTask 6.2: Chrome 实机验证 `/avatar.html`：状态「数字人已就绪（宵宫）」、canvas 597×498、7 形象/9 动作、无异常
  - [x] SubTask 6.3: `/voice.html` 中 `#avatarHomeSlot` canvas 782×587 自动渲染；`elder.html` 懒加载逻辑代码核对
  - [x] SubTask 6.4: 模拟 MediaStream 驱动：`speaking=true`、`mode=in_call`、日志「MediaStream → AnalyserNode」

- [x] Task 7: 验证 LLM 接入链路（未配置 → 已配置 → 恢复未配置）
  - [x] SubTask 7.1: 未配置时 `/api/v1/config/flags` 为 false、AI 分析 503
  - [x] SubTask 7.2: 本地 mock OpenAI 兼容服务（临时目录，未写入项目）验证非流式与 SSE
  - [x] SubTask 7.3: AI 分析 201 并落库 `health_ai_analyses`；`stream_chat` SSE 增量拼接正确
  - [x] SubTask 7.4: 恢复占位配置重启后再次确认 flags=false、AI 分析 503

- [x] Task 8: 验证可选外部依赖（FunASR / 声纹 / 容器）
  - [x] SubTask 8.1: `wss://127.0.0.1:10096` 连接成功；容器 `funasr` healthy，模型挂载 `Desktop\asr\funasr-runtime-resources\models`
  - [x] SubTask 8.2: `FUNASR_URL` 未配置时抛 `ProviderUnavailable`，应用照常启动
  - [x] SubTask 8.3: `SHERPA_MODEL_DIR` 缺模型时抛 `ProviderUnavailable: 声纹模型缺失`

- [x] Task 9: 运行测试并沉淀启停说明
  - [x] SubTask 9.1: `pytest` 192 项全部通过
  - [x] SubTask 9.2: 输出启停命令、容器状态与待填 LLM 三项配置

# Task Dependencies
- Task 2 依赖 Task 1
- Task 3 依赖 Task 2
- Task 4 依赖 Task 3
- Task 5 依赖 Task 4
- Task 6 依赖 Task 5
- Task 7 依赖 Task 5；Task 8 可与 Task 7 并行
- Task 9 依赖 Task 5
