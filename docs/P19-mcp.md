# P19：MCP 工具源（从"有代码但跑不通"到真的能加）

> 2026-09-18。P7 留下了 MCP 的骨架（YAML + 适配层），但那条路**从来没跑通过**。
> 这一期把它修成可用的，并补上按用户隔离的管理接口。
> 旧设计见 `docs/P7-mcp-design.md`（那份的"安全降级"一节已被本文取代）。

## 一、P7 为什么跑不通（三条，都是硬错误）

| 现象 | 根因 |
|---|---|
| `MCP_ENABLED=true` 也一个工具都没有 | `mcp` 与 `langchain-mcp-adapters` **根本没装**，走的是"安全降级"空返回 |
| 装上之后会 ImportError | 真类名是 **`StreamableHTTPTransport`**（HTTP 全大写），旧代码写 `StreamableHttpTransport` |
| 再往下还会 TypeError | `load_mcp_tools` 的签名是 `(session, *, connection, server_name, ...)`，旧代码传 `transport=` / `client_name=`，两个关键字都不存在 |
| 就算都修好，工具也是死的 | tools 在 `async with transport` **内部**构造，退出 `with` 连接就关了 |

正解：`MultiServerMCPClient` —— 连接生命周期由适配器管（默认"每次工具调用开一个新会话"），
我们只 `await client.get_tools()`。

## 二、现在怎么加一个 MCP 工具源

### 方式 1：接口（推荐，按用户隔离）

```bash
TOKEN=...   # POST /api/v1/auth/login

curl -X POST http://127.0.0.1:8000/api/v1/mcp/servers \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{
        "name": "fetch",
        "transport": "streamable_http",
        "url": "https://mcp.api-inference.modelscope.net/xxxx/mcp",
        "headers": {"Authorization": "Bearer <你的 key>"}
      }'

# 先测后存（不落库）
curl -X POST http://127.0.0.1:8000/api/v1/mcp/servers/test \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{...}'

# 已保存的测一次
curl -X POST http://127.0.0.1:8000/api/v1/mcp/servers/<id>/test -H "Authorization: Bearer $TOKEN"
```

改动**立刻生效**：写操作会自动 `invalidate_agents(user_id=...)`，下次对话重建 agent 时加载新工具。

### 方式 2：YAML（运维预置，对**所有用户**生效）

`backend/configs/mcp/*.yaml`，两种写法都吃：

```yaml
# 扁平
name: modelscope-fetch
type: streamable_http          # 字段名现在叫 transport，type 作为别名仍然接受
url: https://.../mcp
headers: {}
enabled: false
```

```yaml
# 直接贴各家广场给的 mcpServers 整段
mcpServers:
  fetch:
    type: streamable_http
    url: https://.../mcp
    enabled: true
```

规则：
- `*.example.yaml` 与 `.` 开头的文件**不加载**；
- 目录内 `name` 重复 → 直接 RuntimeError（宁可起不来，也不要"哪个生效"变玄学）；
- 与数据库**同名时数据库那条赢**（YAML 是共享的，不该盖掉用户自己配的）；
- 改完 YAML 要重启，或调 `POST /api/v1/mcp/reload`。

⚠️ **YAML 里 `enabled: true` 的条目会被真实连接**，而加载发生在 `get_agent()`
里、也就是**请求路径上**。一条不可达的示例配置会给每个会话的首次对话加上一个超时。
所以仓库里那份 `modelscope-fetch.yaml` 已改成 `enabled: false`（它是示例，不是真配置）。

## 二之二、前端界面

顶栏「**工具源**」按钮（在「模型」右边）打开管理弹层，`frontend/src/McpView.vue`：

- 列表：名称 / 连接方式 / 地址或命令 / 已停用标记 / 是否带鉴权头与环境变量；
  每行有 测试 / 停用-启用 / 编辑 / 删除。
- 表单：按 `transport` 动态切换字段（`stdio` 显示命令+参数+环境变量，HTTP 类显示地址+HTTP 头）。
  参数**每行一个**（用 textarea，避免路径带空格被切坏）。
- **鉴权头与环境变量的值永不回显**（后端只给键名）。编辑时默认「保留已保存的」，
  要改才切到「重新填写（覆盖）」—— 后者提交空就是清空。
  这个二选一是刻意的：`null`=不改 / `{}`=清空 两种语义必须让用户能明确选出来。
- 测试结果显示**拿到的工具名列表**，不只是"连通"。
- 顶部横幅会说清两类"配了也不生效"：`MCP_ENABLED` 没开 / 依赖没装。
- 「重载」按钮 = `POST /api/v1/mcp/reload`（改了 YAML 时用；增删改后端已自动重载）。

## 三、参数清单

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | ✅ | 唯一名（同一用户下不重复） |
| `transport` | ✅（默认 `streamable_http`） | `streamable_http` / `sse` / `stdio` / `websocket` |
| `url` | URL 类必填 | 端点地址。`http/https`（websocket 可用 `ws/wss`）；**不许带用户名口令** |
| `headers` | 可选 | HTTP 头，如 `Authorization`。**整段加密存储，接口只回键名** |
| `command` | `stdio` 必填 | 启动命令，如 `npx` / `python` |
| `args` | 可选 | 命令参数数组，如 `["-y", "@modelcontextprotocol/server-fetch"]` |
| `env` | 可选 | 子进程环境变量。**加密存储，不回显** |
| `cwd` | 可选 | 子进程工作目录 |
| `timeout` | 可选 | 连接/请求超时（秒），仅 `sse` / `streamable_http` |
| `sse_read_timeout` | 可选 | 等一个新事件多久断开（秒），同上两类 |
| `enabled` | 可选（默认 true） | 停用后不加载 |

`to_connection()` **只填该 transport 真正接受的键** —— 这些 `TypedDict` 运行时不校验，
多传一个 `timeout` 给 websocket 会在很深的调用栈里才炸。`tests/test_p19_mcp.py` 有断言守着。

## 四、接口一览（`GET /docs` 里也有）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/mcp/status` | 总开关 / 依赖是否装好 / 我配了几个 / 超时 |
| GET | `/api/v1/mcp/servers` | 我自己的列表（headers 只回键名） |
| POST | `/api/v1/mcp/servers` | 新增（201） |
| PUT | `/api/v1/mcp/servers/{id}` | 改。不传的字段不动；`headers`/`env` 传 `{}` 才是清空 |
| DELETE | `/api/v1/mcp/servers/{id}` | 删 |
| POST | `/api/v1/mcp/servers/test` | 临时配置测试（不落库） |
| POST | `/api/v1/mcp/servers/{id}/test` | 已保存配置的测试 |
| POST | `/api/v1/mcp/reload` | 丢掉我的 agent 缓存（改了 YAML 时用） |

**归属**：每行都有 `user_id`，别人的 MCP 看不见也改不了（越权一律 404，不泄漏存在性）。
管理员**没有**越权例外 —— MCP 常有能读内网的 server，这一层不开口子。

## 五、几个实现上的决定

1. **每个 server 一个独立 client**。全塞进一个 `MultiServerMCPClient` 的话，
   任何一个端点不通，`get_tools()` 整体抛错 —— "配了三个、坏了一个"会变成"三个全没了"。
2. **每个 server 套 `asyncio.wait_for`**（`MCP_CONNECT_TIMEOUT_SECONDS`，默认 15s）。
   加载在请求路径上，没上限的话一个不可达端点能把"建 agent"整段卡住。
3. **ExceptionGroup 要摊平**。anyio 用 TaskGroup 跑连接，失败包成
   `ExceptionGroup`，`str()` 出来只有 "unhandled errors in a TaskGroup (1 sub-exception)"。
   `_error_text()` 递归取出内层真正的 `ConnectError` / `HTTPStatusError`。
4. **失败信息过 `scrub_secrets`**。MCP SDK 报错可能把 `Authorization: Bearer xxx`
   带进 message，而这条会直接显示给用户、也会进 SSE。
5. **`user_id` 不可空**（与 `sessions.user_id` 相反）：那三张老表是为了兼容存量
   才允许 NULL，而 `mcp_servers` 是全新表，允许 NULL 就等于开一个"全局共享 MCP"的后门。
6. **`header_keys` 单独存一列**。列表页想显示"配了哪些头"时不必解密。

## 六、开关与配置（backend/.env）

```
MCP_ENABLED=true                  # 总开关；关掉时配置都留着，只是不加载
MCP_CONNECT_TIMEOUT_SECONDS=15    # 单 server 连接超时
```

`GET /api/v1/mcp/status` 会回 `enabled` 与 `adapters_installed`，
前端据此提示"总开关没开"—— 否则用户会以为自己配错了。

## 七、验证

```bash
# 单元/接口（32 条）
python -m pytest tests/test_p7_mcp.py tests/test_p19_mcp.py -q

# 端到端：真的起一个 MCP server、真的加载、真的调用一次
python _probe/mcp_e2e_probe.py
```

`_probe/mcp_demo_server.py` 是探针用的最小 MCP server（stdio，提供 `add` / `greeting`）。
实测输出：

```
[2] probe ...  ok = True | message = 连通，取到 2 个工具 | tools = ['add', 'greeting']
[4] 真的调用一次 add(2, 3) ... -> 5
[5] 端点不可达时的失败路径 ... 坏的那条报错，好的那条照常加载
```

## 八、排错：`ModuleNotFoundError: No module named 'pywintypes'`

**症状**：`pip show pywin32` 说装了，新开一个 python 也能 `import pywintypes`，
但后端进程里加载 MCP 就报这个错。

**根因**：pywin32 的 DLL（`pywintypes312.dll`）不在 `site-packages` 根下，而在
`site-packages/pywin32_system32/`。那个目录是 pywin32 的 `.pth` 在**解释器启动时**
加进 `sys.path` 的 —— 所以**在装 pywintypes 之前就启动的进程**永远补不上。
`mcp` 在 Windows 上 `import mcp.os.win32.utilities` → `pywintypes`，于是踩中。

（实测：后端进程 08:36 启动，pywin32 14:25 才随 `mcp` 装上 → 只有那个进程失败。）

**修法**：**重启后端进程**。这是唯一彻底的办法 —— `.pth` 只在启动时执行一次。
代码里另加了一道兜底 `_ensure_windows_pywin32()`：即便 `.pth` 没生效，
只要 DLL 目录在磁盘上就直接补进 `sys.path` + DLL 搜索路径，但对"进程里已经是旧代码"
这种情况无能为力（要重启才会加载到新代码）。

## 九、已知遗留

- ~~还没有前端界面~~ → **已补**：`frontend/src/McpView.vue` + 顶栏「工具源」按钮（见"二之二"）。
- **没有做"工具名冲突"的前缀处理**
- 本机 httpx 会走系统代理：探针里访问 `127.0.0.1:1` 回的是 `502 Bad Gateway`（代理给的），
  真实内网/本机 MCP 端点可能同样被代理影响。真出问题先查 `HTTP_PROXY` / `NO_PROXY`。
- 没有做"工具名冲突"的前缀处理：两个 server 提供同名工具时，后加载的会盖掉前一个。
  需要时给 `MultiServerMCPClient` 传 `tool_name_prefix=True`（工具名会变成 `<server>_<tool>`）。
- `stdio` 型 server 的子进程由 `_clients` 字典保活，进程内复用，不会每次调用重启。
