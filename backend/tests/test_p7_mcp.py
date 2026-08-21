"""P7 测试：MCP 可插拔注册框架（不触发真实网络）。

当前环境未装 langchain_mcp_adapters，故 `build_mcp_tools` 走安全降级路径（空 tools + 提示）。
框架本身（YAML 加载/校验/降级/开关）在此完整验证。
"""
import asyncio

import pytest

from app.agent import mcp
from app.agent.mcp import McpServerConfig


def test_example_mcp_config_valid():
    """示例 configs/mcp/modelscope-fetch.yaml 应通过校验。"""
    cfgs = mcp.load_all_mcp_cfg()
    names = [c.name for c in cfgs]
    assert "modelscope-fetch" in names


def test_example_disabled_not_loaded():
    """multi-servers.example.yaml 被加载器跳过（.example.yaml 命名）。"""
    cfgs = mcp.load_all_mcp_cfg()
    names = [c.name for c in cfgs]
    assert "fetch-multi" not in names  # 不应影响正式加载


def test_invalid_mcp_type_rejected():
    with pytest.raises(ValueError):
        McpServerConfig(name="x", type="unknown", url="http://x")


def test_streamable_http_requires_url():
    with pytest.raises(ValueError):
        McpServerConfig(name="x", type="streamable_http", url="")


def test_stdio_requires_command():
    with pytest.raises(ValueError):
        McpServerConfig(name="x", type="stdio", command="")


def test_mcpServers_dict_parsing():
    """兼容 mcpServers 嵌套写法。"""
    row = McpServerConfig(name="fetch", type="streamable_http", url="http://e/mcp")
    assert row.name == "fetch"


def test_build_mcp_tools_safe_degrade():
    """未装适配器时安全降级：(empty, [提示])，不抛异常。"""
    async def _run():
        assert not mcp._has_adapters()
        tools, errors = await mcp.build_mcp_tools()
        assert tools == []
        assert len(errors) >= 1
        assert "langchain_mcp_adapters" in errors[0]
    asyncio.run(_run())


def test_runtime_default_mcp_disabled():
    """MCP_ENABLED 默认关闭，agent 可正常构建（降级不干扰）。"""
    from app.core.config import settings
    assert settings.mcp_enabled is False
