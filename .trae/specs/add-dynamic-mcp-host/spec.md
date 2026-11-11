# 动态多 MCP Server 接入与真实天气 Tool 改造 Spec

## Why

当前项目只硬编码接入了 1 个 MCP Server（`mcp-servers/weather/server.py`），天气数据是本地 fixture 示例值，PM 与长者都无法获得真实天气；同时新增或替换 MCP Server 必须改代码。需要把 MCP 接入改为**配置驱动、可动态装载多个 Server**，并把天气升级为**免费真实数据源**，同时为后续扩展/替换/停用单个 Server 预留清晰接口。

## What Changes

- 新增配置文件 `config/mcp_servers.json`（路径可用 `MCP_CONFIG_PATH` 覆盖），声明式描述 N 个 MCP Server；文件缺失/格式错误不影响应用启动。
- 将 `app/agent/mcp_client.py` 从「单一天气客户端」升级为通用 `McpHost`：启动期并发发现各 Server 的 `tools/list`，按 Server 前缀/别名注册进现有 `ToolRegistry`；调用时按需建立短生命周期会话（stdio / streamable_http / sse）。
- 工具暴露名规则：`tool_aliases` 优先（天气继续暴露为 `weather_get`，保持规则层 `R07` 与分类器链路不变），否则 `<server_id>__<tool_name>`；非法字符清洗、重名跳过并记日志。
- 每个 Server 支持独立 `enabled` / 超时 / `allow_tools` / `deny_tools` / `env_from` / `headers`，密钥只能经 `env:VAR` 从环境变量注入，不落配置文件、不入日志。
- `ToolResult` 增加 MCP 来源元数据（`mcp_server`），`voice.py` 的 `mcp_calls` 统计从「只数 weather_get」改为按来源统计；`test_agent_tools.py` 的 fake 注入方式同步调整。
- 天气 MCP Server 升级为真实数据：新增 `mcp-servers/weather/providers.py`，使用 **Open-Meteo**（免费、无需 API Key）的地理编码 + 当前/当日预报；WMO 天气码映射中文话术；服务端内存 TTL 缓存；超时与失败不编造数据。保留 `fixture` Provider 仅用于测试/离线演示。
- `MCP_WEATHER_PROVIDER=open_meteo|fixture`（默认 `open_meteo`），`MCP_WEATHER_DEFAULT_CITY` 可选；未配置且用户只说「本地」时，明确反问城市，不臆测位置。
- 新增自检入口 `python -m app.agent.mcp_client`：打印已启用 Server、发现的工具、别名与失败原因，作为新增/更换 Server 的验收手段。
- 更新 `.env.example`、`docs/Day6-三级路由验收.md` 的 MCP 段与降级矩阵；`httpx` 从 dev 依赖提升为运行时依赖（天气 Server 需要）。
- **BREAKING（内部接口）**：`McpWeatherClient` / `build_default_registry(weather_client=...)` 被 `McpHost` / `build_default_registry(mcp_host=...)` 取代；仅影响 `app/agent/**` 与相关测试，外部 HTTP/WS 契约不变。

## Impact

- Affected specs：Day6 三级路由（MCP 工具链）、Day3 语音链路（工具调用与降级）、安全边界（外部进程与环境变量）。
- Affected code：
  - `app/agent/mcp_client.py`（改为通用 Host + 配置加载）
  - `app/agent/tools.py`（注册表合并 MCP 工具、`ToolResult` 元数据、weather 兜底）
  - `app/services/voice.py`（`mcp_calls` 统计按来源）
  - `app/core/config.py`、`app/core/lifespan.py`（启动期发现与清理）
  - `mcp-servers/weather/server.py`、新增 `mcp-servers/weather/providers.py`
  - `config/mcp_servers.json`、`.env.example`、`pyproject.toml`
  - `docs/Day6-三级路由验收.md`、`tests/test_agent_mcp.py`、`tests/test_mcp_host.py`、`tests/test_agent_tools.py`
- 高风险目录：`mcp-servers/**`、`app/agent/**`（按 AGENTS.md 需人工复核 diff）。

## 设计决策

### 1. 免费 MCP / 真实数据的选型

| 方案 | 结论 | 原因 |
| --- | --- | --- |
| 直接接社区天气 MCP Server（npx 包） | 不采用 | 需安装 Node 与第三方包，供应链不可控；多数仍依赖付费 Key |
| 和风/高德/OpenWeather 免费额度 | 不采用 | 均需申请 Key，违反「不提交密钥」「不调用未授权服务」的现状约束 |
| **自建 MCP Server + Open-Meteo** | **采用** | Open-Meteo 免费、无需 Key、非商业可用；数据源与 MCP 协议解耦，可随时替换 Provider |
| 远端免费托管 MCP（如 DeepWiki 类） | 作为扩展示例（默认 `enabled:false`） | 用于验证 HTTP 传输与多 Server，不引入默认外部依赖 |

天气仍由本项目自己的 MCP Server 提供，区别只是**数据从 fixture 换成真实 API**；这样「免费」与「安全可控」同时满足。

### 2. 配置 Schema（`config/mcp_servers.json`）

```json
{
  "version": 1,
  "servers": [
    {
      "id": "weather",
      "enabled": true,
      "transport": "stdio",
      "args": ["mcp-servers/weather/server.py"],
      "cwd": ".",
      "env": {"MCP_WEATHER_PROVIDER": "open_meteo"},
      "timeout_seconds": 8,
      "discovery_timeout_seconds": 10,
      "tool_aliases": {"get_weather": "weather_get"},
      "allow_tools": ["get_weather"]
    },
    {
      "id": "remote_example",
      "enabled": false,
      "transport": "streamable_http",
      "url": "https://example.com/mcp",
      "headers": {"Authorization": "env:MCP_REMOTE_TOKEN"},
      "tool_prefix": "remote_",
      "allow_tools": ["search"]
    }
  ]
}
```

字段语义（未列出的字段一律有默认值，缺省即「不启用该能力」）：

- `id`：必填，稳定标识，用于命名空间、日志与降级标记；只允许 `[a-z0-9_-]`。
- `enabled`：默认 `true`；`false` 即停用，不发现、不注册工具。
- `transport`：`stdio`（默认）| `streamable_http` | `sse`。
- stdio 专属：`command`（缺省 `sys.executable`）、`args`、`cwd`（相对项目根）、`env`（字面量键值）、`env_from`（从父进程透传的变量名列表）。
- http 专属：`url`、`headers`（值支持 `env:VAR`，运行时从环境变量解析）。
- `timeout_seconds`：单次工具调用超时，默认 8s。
- `discovery_timeout_seconds`：`initialize + list_tools` 超时，默认 10s。
- `tool_prefix`：暴露名前缀，默认 `<id>__`。
- `tool_aliases`：`{远端工具名: 暴露名}`，优先于前缀规则。
- `allow_tools` / `deny_tools`：工具级白/黑名单，`deny` 优先；`allow` 为空表示不限制。

**更换/新增单个 Server 的操作方式（预留接口）**：
1. 新增：在 `servers` 数组追加一个对象 → 重启应用（或以后接管理端调用 `McpHost.refresh()`）→ 工具自动出现在注册表与 LLM tools 列表中。
2. 替换：把对应 `id` 的 `args`/`url`/`command` 改成新 Server，保留 `id` 与 `tool_aliases` 即可，上层规则（如 `weather_get`）零改动。
3. 停用：`enabled: false`，注册表自动回落到内置降级话术「天气服务暂时不可用」。
4. 只暴露部分工具：配 `allow_tools`，避免外部 Server 把高危写操作暴露给模型。

### 3. 生命周期与事件循环

- `McpHost` 在 FastAPI `lifespan` 启动时创建并并发发现，关闭时清理缓存与状态；**不使用 `new_event_loop`、不把 aiortc 相关任务跨循环**（遵守 AGENTS 实时模块规则）。
- 调用仍采用**每次短生命周期会话**（stdio 每次起子进程、HTTP 每次建连），与现有实现一致，避免长期会话绑定错误的事件循环；常驻会话留作后续优化项，不在本次实现。
- 发现失败不抛出：标记 Server 为 `unavailable`，注册表只注册可用工具；天气缺失时注册本地兜底 Tool，保证规则层 `weather_get` 不会变成 `unknown_tool`。

### 4. 安全与隐私边界

- 子进程环境变量采用**最小白名单**：仅 `PATH`、`SYSTEMROOT`（Windows）、`PYTHONIOENCODING` 等基础变量 + 配置中的 `env` + `env_from` 显式透传；绝不透传 `LLM_API_KEY`、`JWT_SECRET_KEY`、`DATABASE_URL`。
- 配置文件中禁止出现明文密钥；HTTP 头必须写 `env:VAR`。
- 日志只记 `server_id`、工具名、耗时、错误类型、降级标记；不记参数原文、不记城市以外的用户文本、不记长者身份。
- MCP 工具结果只作为文字/事件返回，不直接触发高危副作用；通知家属仍走 HITL。
- 天气外部请求仅对 Open-Meteo 域名，超时 5s，失败返回明确「查不到」而不是示例数据，避免长者误信。

## ADDED Requirements

### Requirement: 配置驱动的多 MCP Server 动态装载

系统 SHALL 从 `config/mcp_servers.json`（可被 `MCP_CONFIG_PATH` 覆盖）读取 Server 列表，支持 `stdio`、`streamable_http`、`sse` 三种传输；单个 Server 的启用/停用、超时、工具白黑名单、别名与命名空间均由配置决定，不修改业务代码。

#### Scenario: 新增第二个 Server
- **WHEN** 在配置数组追加一个 `enabled: true` 的 Server 并重启应用
- **THEN** 该 Server 的工具以 `<id>__<tool>` 或声明的别名出现在 `ToolRegistry.schemas()` 中，LLM 与规则层可直接调用

#### Scenario: 停用 Server
- **WHEN** 将 `weather.enabled` 置为 `false` 并重启
- **THEN** 不注册任何天气 MCP 工具；`weather_get` 回落到内置兜底话术且 `degraded` 标记可见，应用照常启动

#### Scenario: 配置文件缺失或损坏
- **WHEN** 配置文件不存在或 JSON 解析失败
- **THEN** 应用正常启动，日志记录 `mcp_config_invalid`，MCP 工具为空，天气走兜底降级

### Requirement: 启动期工具发现与注册表合并

系统 SHALL 在 `lifespan` 启动阶段并发发现所有启用 Server 的工具，缓存 `name/description/input_schema/server_id`，并按名称规则合并进 `ToolRegistry`；发现超时或失败不影响其他 Server。

#### Scenario: 部分 Server 不可用
- **WHEN** Server A 正常、Server B 子进程启动失败
- **THEN** A 的工具全部可用，B 记为 `unavailable` 并输出 `mcp_discovery_failed` 日志，应用启动成功

### Requirement: MCP 调用与可见降级

系统 SHALL 以短生命周期会话执行 MCP 工具调用，单次超时可在配置中覆盖；任何异常/超时/`isError` 都 SHALL 转为 `ToolResult(ok=False, degraded="mcp_unavailable:<server_id>")` 与可朗读降级话术，不向语音链路抛异常。

#### Scenario: 天气接口超时
- **WHEN** Open-Meteo 5s 未响应
- **THEN** 长者听到「天气服务暂时查不到，稍后再帮您试试。」，回复不包含任何虚构温度，`trace` 记录降级标记

### Requirement: 真实天气工具

天气 MCP Server SHALL 通过 Open-Meteo 免费接口获取真实数据（无需 Key），支持中文城市名地理编码、当前温度/湿度/天气码与当日最高最低温，输出可直接朗读的中文短句并标注数据来源；服务端 SHALL 提供 TTL 缓存与超时控制。

#### Scenario: 查询真实天气
- **WHEN** 长者说「上海天气怎么样」
- **THEN** 返回包含上海、真实温度区间与天气描述的中文短句，且不含「示例数据」字样

#### Scenario: 城市无法识别
- **WHEN** 地理编码无结果
- **THEN** 返回「没有查到您说的这个城市，可以说大一点的城市名」，不猜测坐标

#### Scenario: 用户只说「本地」且未配置默认城市
- **WHEN** `city=本地` 且 `MCP_WEATHER_DEFAULT_CITY` 为空
- **THEN** 反问城市名，不使用任何隐含定位信息

### Requirement: 扩展与自检接口

系统 SHALL 提供 `McpHost.refresh()` 与 `python -m app.agent.mcp_client` 自检入口，用于在不改代码的前提下验证「新增/更换/停用」配置是否生效。

#### Scenario: 配置生效自检
- **WHEN** 运维执行 `python -m app.agent.mcp_client`
- **THEN** 输出每个 Server 的启用状态、发现的工具清单、别名映射与失败原因，退出码 0（即使部分 Server 失败）

## MODIFIED Requirements

### Requirement: 天气工具注册

`build_default_registry()` SHALL 接收 `McpHost`（可为空）并完成「本地确定性工具 + MCP 动态工具」合并；`weather_get` 的工具描述从「本地示例数据」改为「真实天气（Open-Meteo，无需 Key）」，参数保持 `city` 可选。MCP 不可用时注册兜底 `weather_get`，其 `degraded="mcp_unavailable:weather"`。

### Requirement: 语音链路 MCP 观测

`voice.py` SHALL 依据 `ToolResult.mcp_server` 统计 `mcp_calls`，覆盖所有来自 MCP 的工具，而不是只统计 `weather_get`；日志字段口径与 `docs/Day6-三级路由验收.md` 对齐。

## REMOVED Requirements

### Requirement: 固定路径的单一天气 MCP 客户端

**Reason**：只能接 1 个 Server、路径写死、新增/替换必须改代码，无法满足扩展诉求。
**Migration**：`McpWeatherClient` 的能力并入 `McpHost`；天气 Server 在 `config/mcp_servers.json` 中声明，`weather_get` 别名保持不变，现有规则层与分类器无需改动。
