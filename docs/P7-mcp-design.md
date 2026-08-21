# P7 MCP 接入——可插拔 YAML 注册 + 适配层

## 目标

用户只需填一个 YAML（形如魔塔 MCP 广场返回的 mcpServers 配置：type/url），
即可新增一个外部 MCP 工具源接入 Agent，无需改代码。

## 关于 FastMCP 的技术判断

- **FastMCP 是 MCP server 侧框架**（把能力封装成 MCP 暴露出去），不是 client 接入工具。
- 本需求是**把外部 MCP（魔塔 MCP 广场）作为 client 接入 Agent** → 使用
  `mcp` 官方 SDK 的 `StreamableHttpTransport` + `langchain-mcp-adapters`
  （把 MCP 工具转成 LangChain tools 喂给 deepagents）。
- FastMCP 不适用于 client 接入；若未来开放 Harness 能力为 MCP server 才用 FastMCP。

## 架构

```
configs/mcp/*.yaml
   │  (name/type/url/headers/enabled，或 mcpServers 嵌套结构)
   ▼
app/agent/mcp.py  load_all_mcp_cfg()  →  McpServerConfig(Pydantic)
   ▼
build_mcp_tools()  →  (LangChain tools, errors)
   • streamable_http: StreamableHttpTransport + load_mcp_tools
   • 未装 langchain_mcp_adapters → 安全降级返回 ([], [warn])，不崩溃
   ▼
runtime.get_agent()  若 settings.mcp_enabled → create_deep_agent(tools=extra_tools)
```

## 为何做"安全降级"

当前环境 HTTPS 出网被断（连魔塔端点/装 mcp 包都失败）。为不影响 P1-P6 功能，
`MCP_ENABLED` 默认关闭；未装适配器时 `build_mcp_tools` 返回空 + 清晰提示。
网络恢复后：
```
pip install mcp langchain-mcp-adapters
# .env: MCP_ENABLED=true
```
即启用真实 mcp 工具（配置无需改动，即插即用）。

## 文件

- `backend/app/agent/mcp.py`：注册表 + 加载适配层 + 降级
- `backend/configs/mcp/modelscope-fetch.yaml`：示例（你给的魔塔端点）
- `backend/configs/mcp/multi-servers.example.yaml`：mcpServers 多端点半示例（.example.yaml 不参与加载）
- `backend/app/agent/runtime.py`：接入 `tools=extra_tools`
- `backend/app/core/config.py`：`mcp_enabled` 开关
- `backend/tests/test_p7_mcp.py`

## P7 附带的通用修复

- 新增 `backend/conftest.py`：pytest 结束用 `os._exit` 规避 aiosqlite 后台连接导致的
  解释器退出挂起（此前每个 pytest 进程结束都 hang 120s）。全套测试 36 passed ~8s 干净退出。

## 验证

- P7 8 项测试通过（YAML 校验/降级/开关/兼容写法）。
- 全套 36 项测试 8s 干净退出。
- Agent 在 MCP 禁用下降级正常构建运行（不破坏 P1-P6）。
