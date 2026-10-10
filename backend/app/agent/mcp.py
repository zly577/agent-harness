"""MCP 接入适配层（骨架 → 重写为真实可用）。

目标：新增一个外部 MCP 工具源只需填配置（YAML 文件 或 数据库里的一条记录），不改代码。

## 重写的原因（旧代码是**跑不通**的，不是"没测过"）

1. `mcp` / `langchain-mcp-adapters` 当时没装，旧代码只能走"安全降级"空返回；
2. 就算装了也会立刻报错 —— 它用的是过时 API：
   - `mcp.client.streamable_http` 里的真类名是 **`StreamableHTTPTransport`**（HTTP 全大写），
     旧代码写 `StreamableHttpTransport` → ImportError；
   - `load_mcp_tools` 的签名是 `(session, *, connection, server_name, ...)`，
     旧代码传的是 `transport=` / `client_name=` → 两个关键字都不存在，TypeError；
3. 更根本的一条：旧代码在 `async with transport` **内部**构造 tools，退出 `with`
   连接就关了，拿回来的是一堆死对象。

正解是 `MultiServerMCPClient`：连接生命周期由它管（默认"每次工具调用开一个新会话"），
我们只管 `await client.get_tools()`。

## 失败隔离

**每个 server 一个独立 client**。不要图省事把所有 server 塞进一个
`MultiServerMCPClient` —— 那样任何一个端点不通，`get_tools()` 整体抛错，
"配了三个、坏了一个"会变成"三个全没了"。

## 超时

`build_mcp_tools()` 跑在 `runtime.get_agent()` 里，而那是在**请求路径上**。
一个不可达的 MCP 端点会把"建 agent"卡住，所以每个 server 的加载都套
`asyncio.wait_for`（`settings.mcp_connect_timeout_seconds`）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.core.config import settings
from app.core.crypto import scrub_secrets

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "configs" / "mcp"

#: 支持的 MCP server 类型（与 langchain-mcp-adapters 的 Connection 联合类型对齐）
SUPPORTED_TYPES = ("streamable_http", "sse", "stdio", "websocket")
#: 这几类靠 URL 连远程端点
_URL_TYPES = ("streamable_http", "sse", "websocket")

#: 保活：MultiServerMCPClient 自身不持有事件循环，但 stdio 子进程与 HTTP 会话
#: 都由它创建。不存引用会被 GC 掉（stdio 子进程尤其明显 —— 进程被回收就再也起不来）。
#: key 用 server name，重建时自然覆盖。
_clients: dict[str, object] = {}


class McpServerConfig(BaseModel):
    """一个 MCP server 定义。

    字段名叫 `transport`，但同时接受 `type`（旧 YAML 的写法），
    所以 `backend/configs/mcp/*.yaml` 不用改。
    """

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(..., min_length=1, max_length=128, description="唯一名（工具前缀的来源）")
    transport: str = Field(
        default="streamable_http",
        validation_alias=AliasChoices("transport", "type"),
        description="streamable_http / sse / stdio / websocket",
    )
    #: 远程端点（streamable_http / sse / websocket 必填）
    url: str = ""
    #: 鉴权头，如 {"Authorization": "Bearer xxx"}。含密钥，列表接口只回键名
    headers: dict[str, str] = Field(default_factory=dict)
    #: stdio：启动命令
    command: str = ""
    #: stdio：命令参数
    args: list[str] = Field(default_factory=list)
    #: stdio：传给子进程的环境变量。含密钥，不回显
    env: dict[str, str] = Field(default_factory=dict)
    #: stdio：子进程工作目录
    cwd: str = ""
    #: http 类：连接/请求超时（秒）
    timeout: float | None = None
    #: sse / streamable_http：等一个新事件多久断开（秒）
    sse_read_timeout: float | None = None
    #: streamable_http：关闭时是否终结会话
    terminate_on_close: bool = True
    enabled: bool = True

    def model_post_init(self, __context) -> None:
        self.transport = (self.transport or "").strip().lower()
        if self.transport not in SUPPORTED_TYPES:
            raise ValueError(f"transport 必须是 {'/'.join(SUPPORTED_TYPES)} 之一，收到 {self.transport!r}")
        if self.transport in _URL_TYPES and not (self.url or "").strip():
            raise ValueError(f"{self.transport} 类型必须有 url")
        if self.transport == "stdio" and not (self.command or "").strip():
            raise ValueError("stdio 类型必须有 command")

    def to_connection(self) -> dict:
        """转成 langchain-mcp-adapters 的 Connection 字典。

        只填该 transport 真正接受的键 —— 这些 TypedDict 在运行时不会校验，
        多传一个 `timeout` 给 websocket 只会在很深的调用栈里炸，很难查。
        """
        if self.transport == "stdio":
            conn: dict = {"transport": "stdio", "command": self.command, "args": list(self.args)}
            if self.env:
                conn["env"] = dict(self.env)
            if self.cwd:
                conn["cwd"] = self.cwd
            return conn

        conn = {"transport": self.transport, "url": self.url}
        # websocket 的 Connection **没有 headers**（只有 url + session_kwargs），
        # 多传一个键会在适配器深处炸 —— 所以只对这两类 http 传输加。
        if self.headers and self.transport in ("sse", "streamable_http"):
            conn["headers"] = dict(self.headers)
        if self.transport in ("sse", "streamable_http"):
            # 兜底：数据库/ YAML 里没显式配超时时，也塞一个合理的默认值，
            # 否则底层 HTTP 客户端用"无限等待"，服务端挂起时工具永久卡死
            # （2026-09-18 bing_search 卡死即此类）。用工具执行超时兜底。
            timeout = self.timeout if self.timeout else settings.mcp_tool_call_timeout_seconds
            if timeout:
                conn["timeout"] = timeout
            if self.sse_read_timeout:
                conn["sse_read_timeout"] = self.sse_read_timeout
        if self.transport == "streamable_http" and not self.terminate_on_close:
            conn["terminate_on_close"] = False
        return conn


# ============================================================================
# YAML 来源（遗留，保留：管理员可以直接落文件）
# ============================================================================


def load_all_mcp_cfg() -> list[McpServerConfig]:
    """读取 configs/mcp/ 下所有 YAML，做 Pydantic 校验。"""
    result: list[McpServerConfig] = []
    if not _CONFIG_DIR.exists():
        return result
    seen: set[str] = set()
    for f in sorted(_CONFIG_DIR.glob("*.yaml")):
        # 跳过示例文件（.example.*）与 dotfile
        if f.name.endswith(".example.yaml") or f.name.startswith("."):
            continue
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        # 兼容两种写法：直接是 {"name","type","url",...}，或 {"mcpServers": {...}}
        if "mcpServers" in data and isinstance(data["mcpServers"], dict):
            for name, srv in data["mcpServers"].items():
                row = dict(srv) if isinstance(srv, dict) else {}
                row.setdefault("name", name)
                result.append(McpServerConfig(**row))
        else:
            result.append(McpServerConfig(**data))
    for cfg in result:
        if cfg.name in seen:
            raise RuntimeError(f"MCP server name 重复: {cfg.name}")
        seen.add(cfg.name)
    return result


def _has_adapters() -> bool:
    """是否安装了 mcp 与 langchain_mcp_adapters。

    两个都查：只查后者的话，`mcp` 缺失时的报错会是一句看不懂的
    ModuleNotFoundError，而不是"依赖没装"这句人话。
    """
    import importlib.util

    return (
        importlib.util.find_spec("langchain_mcp_adapters") is not None
        and importlib.util.find_spec("mcp") is not None
    )


def adapters_missing_hint() -> str:
    """依赖没装时的可操作提示。"""
    return (
        "MCP 依赖未安装。执行：pip install \"mcp>=1.24,<2.0\" langchain-mcp-adapters"
        "（注意 langchain-mcp-adapters 0.3.2 要求 mcp<2.0，装 2.x 会不匹配）。"
    )


# ============================================================================
# 构建工具
# ============================================================================


def _error_text(e: BaseException) -> str:
    """把异常（含 ExceptionGroup）摊成人话。

    anyio 用 TaskGroup 跑连接，失败一律包成 `ExceptionGroup`，而
    `str(ExceptionGroup)` 只有一句 "unhandled errors in a TaskGroup
    (1 sub-exception)" —— 真正的 `ConnectError: All connection attempts
    failed` 在 `.exceptions` 里藏着。不摊开的话，用户看到的错误等于什么都没说。
    """
    parts: list[str] = []

    def walk(exc: BaseException) -> None:
        subs = getattr(exc, "exceptions", None)
        if subs:
            for s in subs:
                walk(s)
        else:
            text = str(exc).strip()
            parts.append(f"{type(exc).__name__}: {text}" if text else type(exc).__name__)

    walk(e)
    return "; ".join(parts) if parts else f"{type(e).__name__}: {e}"


def _ensure_windows_pywin32() -> None:
    """Windows 上把 pywin32 的 DLL 目录补进 sys.path。

    ## 为什么需要

    `mcp` 在 Windows 上会 `import mcp.os.win32.utilities` → `pywintypes`，
    而 `pywintypes312.dll` 不在 site-packages 根下，在 `pywin32_system32/` ——
    那个目录是 pywin32 的 `.pth` 在**解释器启动时**加进 sys.path 的。

    于是有一类极容易误判的报错：

        ModuleNotFoundError: No module named 'pywintypes'

    明明 `pip show pywin32` 说装了、新开一个 python 也能 import，
    却只在**某个进程**里失败 —— 因为那个进程是在装 pywin32 **之前**启动的，
    `.pth` 早已执行完毕、再也不会补。
    （本项目就踩到了：后端早上 08:36 启动，pywin32 下午才随 `mcp` 装上。）

    修法首选**重启进程**。这里再加一道兜底：即使 `.pth` 没生效，
    只要 DLL 目录在磁盘上，就直接补进 sys.path / DLL 搜索路径。
    """
    if sys.platform != "win32":
        return
    try:
        import pywintypes  # noqa: F401  能 import 就什么都不用做
        return
    except ImportError:
        pass

    dll_dir = Path(sys.prefix) / "Lib" / "site-packages" / "pywin32_system32"
    if not dll_dir.is_dir():
        return
    text = str(dll_dir)
    if text not in sys.path:
        sys.path.append(text)
    # pythoncom312.dll 与 pywintypes312.dll 互相依赖，目录也要进 DLL 搜索路径
    add_dll_directory = getattr(os, "add_dll_directory", None)
    if add_dll_directory is not None:
        try:
            add_dll_directory(text)
        except Exception:  # noqa: BLE001 加了更好，加不上就指望 PATH
            pass
    if text not in os.environ.get("PATH", ""):
        os.environ["PATH"] = text + os.pathsep + os.environ.get("PATH", "")


def _wrap_tool_call_timeout(tool, *, timeout: float) -> object:
    """给一个 MCP 工具的执行加上超时兜底。

    ## 为什么必须这么做

    MCP 的**连接**阶段有 `asyncio.wait_for` 保护（`build_mcp_tools`），但**工具真正
    被调用执行**时没有任何超时：像 `bing_search` 这类服务端收到 `tools/call` 后，
    还要自己再跑一次真实搜索，可能几十秒甚至更久不返回 —— 此时 `await tool.ainvoke()`
    永久挂起，agent 的 `tools` 节点执行不完，整个会话的 SSE 一起卡死、前端一个字都
    刷不出来（2026-09-18 实测：`bing_search` 卡在 `running` 8 分钟+，同批并行的
    `crawl_webpage` 却已 `done`）。

    熔断器（middleware._is_tool_failure）只认"已返回的失败"，对"挂起不返回"根本
    无能为力 —— 因为 `after_agent` 都执行不到。所以兜底必须发生在**工具执行本身**。

    包装策略：复用原工具的 `coroutine`（真正业务函数，签名 `(runtime=None, **arguments)`，
    第一个参数是框架注入的 InjectedToolArg），只把它的执行套上 `wait_for`，然后用
    `StructuredTool` 重新构造。name / description / args_schema（JSON Schema dict）/
    response_format（content_and_artifact）全部原样保留，对 graph 编译与消息结构
    完全透明。不能直接给实例赋 `ainvoke` —— StructuredTool 是 Pydantic 模型，
    `__setattr__` 会拦（没有该 field）。超时/异常统一以 `Error:` 开头的字符串返回，
    让熔断器能识别并冷却。
    """
    import asyncio as _asyncio

    from langchain_core.tools import StructuredTool

    original_coroutine = tool.coroutine
    # content_and_artifact 类型的工具要求返回 (content, artifact) 二元组，
    # 失败分支也必须保持这个形状，否则框架会再抛 ValueError 把错误盖掉。
    response_format = getattr(tool, "response_format", None)

    async def _coroutine_with_timeout(runtime=None, **arguments):
        try:
            return await _asyncio.wait_for(
                original_coroutine(runtime=runtime, **arguments), timeout=timeout
            )
        except _asyncio.TimeoutError:
            logger.warning("[mcp] 工具 %s 执行超时（>%g 秒），已中断", tool.name, timeout)
            msg = (
                f"Error: MCP 工具 {tool.name} 执行超时（超过 {timeout:g} 秒无响应）。"
                "远端服务可能仍在处理或已挂起。请改用其他方式获取信息，不要重复调用本工具。"
            )
            return (msg, None) if response_format == "content_and_artifact" else msg
        except Exception as e:  # noqa: BLE001 工具挂了也不能拖垮 agent
            logger.warning("[mcp] 工具 %s 执行失败: %s: %s", tool.name, type(e).__name__, e)
            msg = (
                f"Error: MCP 工具 {tool.name} 执行失败（{type(e).__name__}）：{e}。"
                "请改用其他方式，不要重复调用本工具。"
            )
            return (msg, None) if response_format == "content_and_artifact" else msg

    wrapped = StructuredTool(
        name=tool.name,
        description=tool.description,
        args_schema=normalize_json_schema(tool.args_schema),
        coroutine=_coroutine_with_timeout,
        response_format=response_format,
        metadata=getattr(tool, "metadata", None),
        return_direct=getattr(tool, "return_direct", False),
    )
    return wrapped


#: JSON Schema draft-04 的「布尔排他边界」关键字 → 它对应的 d-06+ 包含边界关键字。
#: `{"minimum": n, "exclusiveMinimum": true}` 等价于 d-06+ 的 `{"exclusiveMinimum": n}`。
_EXCLUSIVE_BOUNDS = (("exclusiveMinimum", "minimum"), ("exclusiveMaximum", "maximum"))
_EXCLUSIVE_KEYS = frozenset(key for key, _ in _EXCLUSIVE_BOUNDS)


def normalize_json_schema(node: Any) -> Any:
    """把 JSON Schema 里的 draft-04 布尔排他边界，翻成 draft-06+ 认的数值写法。

    ## 为什么必须做这一步

    第三方 MCP 服务端经常吐 draft-04 写法（Python 生态里手工拼 schema 的尤其多）：

        {"type": "integer", "minimum": 0, "exclusiveMinimum": true}

    draft-04 里 `exclusiveMinimum` 是个**布尔开关**（配合 `minimum` 表示"不含下界"）；
    但从 draft-06 起它必须是一个**数字**。DeepSeek / OpenAI 的 tools 校验很严格，
    遇到布尔值直接拒绝整个请求：

        Invalid schema for function 'read_pdf': true is not of type "number"

    致命的点在于 schema 是**跟着每一次 model 请求一起发**的 —— 一个坏 schema
    不是"那个工具不能用"，而是**整段会话所有任务全部 400**。2026-09-30 接入
    `pdf-reader-mcp` 时就是这么把会话打死的（该 server 的 `read_pdf` 里
    `properties.sources.items.properties.pages.anyOf[0].items` 带了
    `"exclusiveMinimum": true`）。

    ## 转换规则

        {"exclusiveMinimum": true,  "minimum": n} → {"exclusiveMinimum": n}   # 语义等价
        {"exclusiveMinimum": false, "minimum": n} → {"minimum": n}            # 去掉布尔开关
        {"exclusiveMinimum": true}                → 删掉该键                  # 无边界可表达，保守丢弃
        {"exclusiveMinimum": false}               → 删掉该键

    `exclusiveMaximum` / `maximum` 同理。其余节点（含 `properties` / `items` /
    `anyOf` 等任意嵌套）原样递归，不做任何其它改写 —— 这个函数是纯函数且**幂等**，
    规范化过的 schema 再跑一次结果完全一致。

    注意 `{"additionalProperties": false}`、`{"default": true}` 这类布尔值是**合法**
    的 JSON Schema，不在这里的处理范围内，原样保留。
    """
    if isinstance(node, dict):
        out: dict = {}
        for key, value in node.items():
            # 排他边界要连兄弟键一起判断，所以先跳过，留到下面统一处理
            if key in _EXCLUSIVE_KEYS and isinstance(value, bool):
                continue
            out[key] = normalize_json_schema(value)

        for exclusive_key, inclusive_key in _EXCLUSIVE_BOUNDS:
            flag = node.get(exclusive_key)
            if not isinstance(flag, bool):
                continue  # 没有这个键，或已经是 d-06+ 的数字写法 → 原样留着
            out.pop(exclusive_key, None)
            bound = node.get(inclusive_key)
            # 真正的数字边界（要排除 bool —— 在 Python 里它是 int 的子类）
            is_number = isinstance(bound, (int, float)) and not isinstance(bound, bool)
            if flag and is_number:
                out[exclusive_key] = bound
                out.pop(inclusive_key, None)
            # flag=False，或 flag=True 但没有可用的数字边界 → 只删掉布尔开关
        return out

    if isinstance(node, list):
        return [normalize_json_schema(item) for item in node]

    return node


def normalize_tool_schema(tool):
    """给一个工具的 `args_schema` 做规范化；无改动时原样返回同一个对象。

    只动 **裸 JSON Schema dict** 型 schema —— 那正是 langchain-mcp-adapters 的产物
    （它把远端 `inputSchema` 原样塞进 `StructuredTool(args_schema=<dict>)`）。
    本地工具的 schema 是 pydantic 模型类、由我们自己写，不归这里管。

    用 `model_copy(update=...)` 而不是直接赋值：`StructuredTool` 是 Pydantic 模型，
    而且 `model_copy` 在 update 命中 `args_schema` 时会清掉
    `_tool_call_schema_memo` 缓存（`langchain_core/tools/base.py`），
    所以改完立即生效、不会再把旧 schema 发出去。
    """
    schema = getattr(tool, "args_schema", None)
    if not isinstance(schema, dict):
        return tool

    fixed = normalize_json_schema(schema)
    if fixed == schema:
        return tool

    logger.info(
        "[mcp] 工具 %s 的 inputSchema 含已废弃写法，已规范化后使用", getattr(tool, "name", "?")
    )
    return tool.model_copy(update={"args_schema": fixed})


async def _load_one(cfg: McpServerConfig) -> list:
    """用一个 MCP server 配置构建 LangChain tools。"""
    _ensure_windows_pywin32()
    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient({cfg.name: cfg.to_connection()})
    tools = await client.get_tools()
    _clients[cfg.name] = client
    # 第三方 schema 什么写法都有，出站前统一体检一遍 —— 一个不合规的 schema
    # 会让每一次 model 请求都 400，整段会话报废（见 normalize_json_schema）。
    # 这里是所有取工具的路径的唯一出口（探测与构建 agent 都走它）。
    return [normalize_tool_schema(t) for t in tools]


async def build_mcp_tools(
    cfgs: list[McpServerConfig] | None = None,
    *,
    timeout: float | None = None,
) -> tuple[list, list[str]]:
    """构建 MCP tools。

    Args:
        cfgs: 不传 = 只读 YAML（行为）。传了就用它（数据库来源走这条）。
        timeout: 单 server 连接超时（秒）；不传用 `settings.mcp_connect_timeout_seconds`。

    Returns:
        (tools, errors)。**失败只影响那一个 server**，其余照常加载。
    """
    tools: list = []
    errors: list[str] = []

    if cfgs is None:
        cfgs = load_all_mcp_cfg()
    cfgs = [c for c in cfgs if c.enabled]
    if not cfgs:
        return tools, errors

    if not _has_adapters():
        return tools, [adapters_missing_hint()]

    limit = timeout if timeout is not None else settings.mcp_connect_timeout_seconds
    exec_timeout = settings.mcp_tool_call_timeout_seconds
    for cfg in cfgs:
        try:
            got = await asyncio.wait_for(_load_one(cfg), timeout=limit)
            # 给每个工具的执行套上超时兜底（连接超时 ≠ 调用超时，见 _wrap_tool_call_timeout）
            if exec_timeout and exec_timeout > 0:
                got = [_wrap_tool_call_timeout(t, timeout=exec_timeout) for t in got]
            tools.extend(got)
            logger.info("[mcp] %s 加载 %d 个工具", cfg.name, len(got))
        except asyncio.TimeoutError:
            errors.append(f"MCP server '{cfg.name}' 连接超时（超过 {limit:g} 秒），已跳过")
            logger.warning("[mcp] %s 连接超时", cfg.name)
        except Exception as e:  # noqa: BLE001 一个端点坏了不能拖垮整个 agent 构建
            errors.append(f"MCP server '{cfg.name}' 加载失败：{scrub_secrets(_error_text(e))}")
            logger.warning("[mcp] %s 加载失败: %s: %s", cfg.name, type(e).__name__, e)
    return tools, errors


async def probe_mcp_server(cfg: McpServerConfig, *, timeout: float | None = None) -> dict:
    """连通性探测：真的连一次并把拿到的工具名列出来。

    "连上了"和"能用"是两件事 —— 只回 ok=True 的话，用户无法判断
    到底拿到 0 个工具还是 20 个。所以结果里一定带 `tools`。
    """
    if not _has_adapters():
        return {"ok": False, "message": adapters_missing_hint(), "tools": [], "detail": ""}

    limit = timeout if timeout is not None else settings.mcp_connect_timeout_seconds
    try:
        got = await asyncio.wait_for(_load_one(cfg), timeout=limit)
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "message": f"连接超时（超过 {limit:g} 秒）。请检查地址是否可达、是否需要代理。",
            "tools": [],
            "detail": "",
        }
    except Exception as e:  # noqa: BLE001 探测就是把失败说清楚
        flat = scrub_secrets(_error_text(e))
        return {
            "ok": False,
            "message": f"连接失败：{flat}",
            "tools": [],
            "detail": flat,
        }

    names = [getattr(t, "name", "") for t in got]
    names = [n for n in names if n]
    return {
        "ok": True,
        "message": f"连通，取到 {len(names)} 个工具" if names else "连通，但该服务没有提供任何工具",
        "tools": names,
        "detail": "",
    }


# ============================================================================
# 辅助
# ============================================================================


@lru_cache
def available_mcp_names() -> list[str]:
    """返回 YAML 里已配置的 MCP server 名字（辅助/调试）。"""
    return [c.name for c in load_all_mcp_cfg()]


def reload_mcp_cache() -> None:
    """清掉 `available_mcp_names` 的缓存（改完 YAML 调一下）。"""
    available_mcp_names.cache_clear()


def reset_clients() -> None:
    """丢掉保活的 client（测试用）。"""
    _clients.clear()


__all__ = [
    "McpServerConfig",
    "SUPPORTED_TYPES",
    "adapters_missing_hint",
    "available_mcp_names",
    "build_mcp_tools",
    "load_all_mcp_cfg",
    "probe_mcp_server",
    "reload_mcp_cache",
    "reset_clients",
]
