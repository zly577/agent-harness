"""MCP 接入适配层（P7，可插拔）。

目标：用户只填 YAML（形如魔塔 MCP 广场提供的 mcpServers 配置），即可新增一个外部 MCP 工具源，
无需改代码。

设计（详见 docs/P7-mcp-design.md）：
- `configs/mcp/*.yaml`：每个文件描述一个 MCP server（type/url/headers/enabled）。
- `load_all_mcp_cfg()`：读取并 Pydantic 校验。
- `build_mcp_tools()`：把 enabled 的 MCP server 转成 LangChain tools（喂给 deepagents）。
  - 依赖 `langchain_mcp_adapters`（可选）。若未安装（当前无网），**安全降级**返回空 tools + 警告。
- `MCP_ENABLED` 开关：默认关闭；网络恢复 + 装依赖后设为 1 即启用真实 MCP。

type 支持：`streamable_http`（当前主流，魔塔 MCP 广场即此类）；`stdio`（预留，需额外参数）。
"""
import logging
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "configs" / "mcp"

# 支持的 MCP server 类型
_SUPPORTED_TYPES = {"streamable_http", "stdio"}


class McpServerConfig(BaseModel):
    """一个 MCP server 定义。对应 mcpServers[<name>] 项结构。"""

    name: str
    type: str = "streamable_http"
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict, description="可选鉴权头")
    command: str = Field(default="", description="stdio 类型：启动命令")
    args: list[str] = Field(default_factory=list, description="stdio 类型：命令参数")
    env: dict[str, str] = Field(default_factory=dict, description="stdio 类型：环境变量")
    enabled: bool = True

    def model_post_init(self, __context) -> None:
        if self.type not in _SUPPORTED_TYPES:
            raise ValueError(f"type 必须是 {sorted(_SUPPORTED_TYPES)} 之一")
        if self.type == "streamable_http" and not self.url:
            raise ValueError("streamable_http 类型必须有 url")
        if self.type == "stdio" and not self.command:
            raise ValueError("stdio 类型必须有 command")


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
                row["name"] = srv.get("name", name) if isinstance(srv, dict) else name
                result.append(McpServerConfig(**row))
        else:
            result.append(McpServerConfig(**data))
    for cfg in result:
        if cfg.name in seen:
            raise RuntimeError(f"MCP server name 重复: {cfg.name}")
        seen.add(cfg.name)
    return result


def _has_adapters() -> bool:
    """是否安装了 langchain_mcp_adapters。"""
    import importlib.util
    return importlib.util.find_spec("langchain_mcp_adapters") is not None


async def _build_tools_one(cfg: McpServerConfig) -> list:
    """用一个 MCP server 配置构建 LangChain tools。"""
    from langchain_mcp_adapters.tools import load_mcp_tools
    from mcp.client.streamable_http import StreamableHttpTransport

    # streamable_http：通过 HTTP/SSE 连接到远程 MCP 端点
    async with StreamableHttpTransport(url=cfg.url, headers=cfg.headers or None) as transport:
        tools = await load_mcp_tools(transport=transport, client_name=cfg.name)
    return tools


async def build_mcp_tools() -> tuple[list, list[str]]:
    """构建所有 enabled MCP server 的 LangChain tools。

    返回 (tools, errors)。error 记录失败的 server（不影响其余）。
    若未装 langchain_mcp_adapters（当前无网），安全降级返回 ([], [warn])。
    """
    tools: list = []
    errors: list[str] = []
    if not _has_adapters():
        errors.append(
            "langchain_mcp_adapters 未安装（当前环境无法联网安装）。"
            "网络恢复并 `pip install mcp langchain-mcp-adapters` 后，设 MCP_ENABLED=1 即启用。"
        )
        return tools, errors

    for cfg in load_all_mcp_cfg():
        if not cfg.enabled:
            continue
        try:
            built = await _build_tools_one(cfg)
            tools.extend(built)
            logger.info("[mcp] loaded %d tools from %s", len(built), cfg.name)
        except Exception as e:
            errors.append(f"MCP server '{cfg.name}' 加载失败: {type(e).__name__}: {e}")
            logger.warning("[mcp] failed to load %s: %s", cfg.name, e)
    return tools, errors


@lru_cache
def available_mcp_names() -> list[str]:
    """返回已配置的 MCP server 名字（辅助/调试）。"""
    return [c.name for c in load_all_mcp_cfg()]


def reload_mcp_cache() -> None:
    available_mcp_names.cache_clear()
