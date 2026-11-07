# Checklist

- [x] `.venv` 已创建，项目依赖安装完成且 `import app.main` 成功
- [x] `.env` 已由示例生成本地配置，且被 `.gitignore` 忽略（未入库）
- [x] `JWT_SECRET_KEY` 为本地随机生成值，未写入任何文档或日志
- [x] `kangban-pg` 容器运行中，`kangban_dev` 与 `kangban_test` 均启用 pgvector
- [x] `alembic upgrade head` 执行成功，6 版迁移全部应用
- [x] 管理员账号创建成功，重复执行幂等跳过
- [x] `GET /health` 返回 200 且可回传 trace_id
- [x] 登录页与 8 个业务页面均返回 200
- [x] 3D 数字人静态资源（VRM/vendor/动画）均返回 200
- [x] `avatar.html` 数字人渲染成功且无模块解析错误，`elder.html` 懒加载逻辑核对通过
- [x] `voice.html` 中数字人自动渲染，TTS 音频口型桥经模拟 MediaStream 验证（`speaking=true`）
- [x] 未配置 LLM 时 `/api/v1/config/flags` 为 false、AI 分析 503，无崩溃
- [x] 用 mock 兼容接口验证配置后 AI 分析 201 落库、语音 SSE 流式输出正常
- [x] FunASR 未就绪/未配置时应用照常启动，`ProviderUnavailable` 可见
- [x] `python -m pytest` 全部通过（192 项）
- [x] 未改动任何业务代码、3D 引擎/静态资源、迁移脚本、依赖声明与 Git 跟踪文件
