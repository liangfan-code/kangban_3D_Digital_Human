# Tasks

- [ ] Task 1: 建立 MCP Server 配置模型与加载器
  - [ ] SubTask 1.1: 在 `app/agent/mcp_client.py` 增加 `McpServerConfig`（id/enabled/transport/command/args/cwd/env/env_from/url/headers/timeout/tool_prefix/tool_aliases/allow_tools/deny_tools）与 `load_mcp_servers(path)`；校验 id 格式、传输类型与必填项，非法条目跳过并记 `mcp_config_invalid` 警告
  - [ ] SubTask 1.2: 新增 `config/mcp_servers.json`，内置 `weather`（stdio、别名 `get_weather→weather_get`）与一个默认 `enabled:false` 的 `streamable_http` 示例条目
  - [ ] SubTask 1.3: `app/core/config.py` 增加 `mcp_enabled: bool = True`、`mcp_config_path: str | None = None`、`mcp_default_call_timeout_seconds: float = 8.0`；`.env.example` 补配置说明
  - [ ] SubTask 1.4: 测试：文件缺失、JSON 损坏、非法 id、`enabled:false` 跳过

- [ ] Task 2: 实现通用 `McpHost`（发现 + 调用 + 命名 + 降级）
  - [ ] SubTask 2.1: 启动期并发发现：对每个启用 Server 执行 `initialize` + `list_tools`（`asyncio.gather(return_exceptions=True)`，各自超时），缓存 `McpToolInfo(server_id, remote_name, exposed_name, description, input_schema)`；单 Server 失败仅标记 `unavailable`
  - [ ] SubTask 2.2: 暴露名规则：`tool_aliases` 优先 → `tool_prefix` → 默认 `<server_id>__<tool>`；清洗非法字符、截断 64 字符、重名先到先得并记日志；应用 `allow_tools`/`deny_tools`
  - [ ] SubTask 2.3: 调用实现：stdio 通过 `StdioServerParameters` 起短生命周期子进程，HTTP 走 `streamablehttp_client`/`sse_client`；统一超时、`isError` 与非 0 退出处理，返回 `McpCallResult`（含 `content`、耗时、`server_id`）
  - [ ] SubTask 2.4: 子进程环境最小白名单（`PATH`、`SYSTEMROOT`、`PYTHONIOENCODING` 等）+ `env` 字面量 + `env_from` 透传；`headers` 中 `env:VAR` 运行时解析；缺失变量跳过该项并记日志，不打印值
  - [ ] SubTask 2.5: `refresh()` 重建发现快照（供后续管理端/CLI 使用），提供 `tools_snapshot()` 与 `server_status()` 只读接口
  - [ ] SubTask 2.6: `python -m app.agent.mcp_client` 自检：打印 Server 状态、工具、别名、失败原因，正常退出码 0

- [ ] Task 3: 注册表合并与语音观测改造
  - [ ] SubTask 3.1: `app/agent/tools.py`：`build_default_registry(mcp_host=None)` 改为合并本地工具 + `mcp_host.tools_snapshot()`；MCP Tool handler 调 `McpHost.call()` 并映射为 `ToolResult`
  - [ ] SubTask 3.2: `ToolResult` 增加 `mcp_server: str | None = None`；MCP 调用失败时 `degraded="mcp_unavailable:<server_id>"`；无 MCP 天气工具时注册兜底 `weather_get`（`degraded="mcp_unavailable:weather"`）
  - [ ] SubTask 3.3: `app/services/voice.py` 的 `_call_tool` 与 Agent 路径按 `result.mcp_server` 统计 `mcp_calls`（含 `info` 日志口径）
  - [ ] SubTask 3.4: 更新 `tests/test_agent_tools.py`、`tests/test_companion_agent.py`：用 FakeHost 注入替代 `FakeWeather`，断言 `weather_get` 别名、schema、失败降级与 `mcp_server` 元数据

- [ ] Task 4: 生命周期接线
  - [ ] SubTask 4.1: `app/core/lifespan.py` 启动时若 `mcp_enabled` 则创建并 `await host.start()`；超时/异常不阻断启动，输出 `mcp_startup_degraded`
  - [ ] SubTask 4.2: 关闭时清理 Host 缓存状态（无长连接需显式关闭时也要释放引用）；确保不创建新事件循环、不跨循环复用会话
  - [ ] SubTask 4.3: 确认 `get_mcp_host()` 在未启动场景（单测/CLI）也可安全返回空快照

- [ ] Task 5: 天气 Server 升级为真实数据（可与 Task 1-4 并行）
  - [ ] SubTask 5.1: 新增 `mcp-servers/weather/providers.py`：`fetch_open_meteo(city, transport=None)` 两步调用（geocoding + forecast），WMO code → 中文天气与穿衣/带伞建议，输出含真实温度区间与「数据来源 Open-Meteo」
  - [ ] SubTask 5.2: 实现服务端 TTL 缓存（默认 600s、容量上限 32）、请求超时（默认 5s）、中文城市名与「本地」处理（`MCP_WEATHER_DEFAULT_CITY`，未配置则反问城市）
  - [ ] SubTask 5.3: 保留 `fixture` Provider（显式 `MCP_WEATHER_PROVIDER=fixture` 时启用，保留【示例数据】标注），默认 `open_meteo`；删除默认 fixture 行为
  - [ ] SubTask 5.4: `server.py` 改为薄封装：只做参数校验、Provider 选择与 `MCPServer` 注册，不在 Server 内写 HTTP 细节
  - [ ] SubTask 5.5: `pyproject.toml` 将 `httpx>=0.28` 移入运行时依赖
  - [ ] SubTask 5.6: 测试：用 `httpx.MockTransport` 覆盖正常/空结果/超时/天气码映射；测试文件通过 `importlib` 按路径加载 `providers.py`，不发起真实网络

- [ ] Task 6: MCP 集成测试与文档
  - [ ] SubTask 6.1: 新增 `tests/test_mcp_host.py`：以 weather Server 配置两个不同 `id` 验证并发发现、命名空间/别名、`allow_tools`、坏路径降级、`refresh()`
  - [ ] SubTask 6.2: 更新 `tests/test_agent_mcp.py`：默认以 `MCP_WEATHER_PROVIDER=fixture` 跑真实 stdio 子进程；真实 Open-Meteo 用例加 `skipif` 环境开关 `MCP_WEATHER_LIVE_TEST=1`
  - [ ] SubTask 6.3: 更新 `docs/Day6-三级路由验收.md`：MCP 代码地图、Schema 人审表（数据源改为 Open-Meteo、声明免费无 Key）、降级矩阵与验收场景 8
  - [ ] SubTask 6.4: 在 `.env.example` 中给出「新增/更换/停用 Server」的最小示例注释

- [ ] Task 7: 全量验证
  - [ ] SubTask 7.1: `python -m pytest` 全绿（不连数据库、不依赖外网）
  - [ ] SubTask 7.2: `ruff check .` 通过
  - [ ] SubTask 7.3: 手动执行 `python -m app.agent.mcp_client` 与一次本地语音/文字天气问答，确认真实天气与降级可见

# Task Dependencies

- Task 2 depends on Task 1
- Task 3 depends on Task 2
- Task 4 depends on Task 2
- Task 5 独立，可与 Task 1-4 并行
- Task 6 depends on Task 3, Task 5
- Task 7 depends on Task 1-6
