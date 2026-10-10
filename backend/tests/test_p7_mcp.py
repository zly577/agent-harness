"""遗留的 MCP YAML 加载测试（修订）。

这里只测**配置文件**这一层：YAML 解析、兼容性写法、非法值被拒、依赖缺失时降级。
真实的连接/工具构建/API 在 `test_p19_mcp.py`。

2026-09-18 修订：`mcp` 与 `langchain-mcp-adapters` 装上之后，
`_has_adapters()` 返回 True，原来"默认走降级"的断言不再成立 ——
降级分支现在用 monkeypatch 强制触发（它依然是要保证的行为：
没装依赖的机器上不能因为 import 失败就让整个 agent 起不来）。
"""
import asyncio

import pytest

from app.agent import mcp
from app.agent.mcp import McpServerConfig
from app.core.config import settings


def test_example_mcp_config_valid():
    """示例 configs/mcp/modelscope-fetch.yaml 应通过校验。"""
    cfgs = mcp.load_all_mcp_cfg()
    names = [c.name for c in cfgs]
    assert "modelscope-fetch" in names


def test_example_disabled_not_loaded():
    """multi-servers.example.yaml 被加载器跳过（.example.yaml 命名）。"""
    cfgs = mcp.load_all_mcp_cfg()
    names = [c.name for c in cfgs]
    assert "fetch-multi" not in names


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


def test_type_alias_maps_to_transport():
    """旧 YAML 写 `type`，字段名叫 `transport` —— 两种都该能构造出来。"""
    assert McpServerConfig(name="a", type="stdio", command="npx").transport == "stdio"
    assert McpServerConfig(name="b", transport="sse", url="http://e/sse").transport == "sse"


def test_build_mcp_tools_safe_degrade(monkeypatch):
    """未装适配器时安全降级：(空, [提示])，不抛异常。"""
    monkeypatch.setattr(mcp, "_has_adapters", lambda: False)

    async def _run():
        tools, errors = await mcp.build_mcp_tools([McpServerConfig(
            name="x", transport="streamable_http", url="http://127.0.0.1:1/mcp"
        )])
        assert tools == []
        assert len(errors) == 1
        assert "pip install" in errors[0]

    asyncio.run(_run())


def test_no_config_means_no_work():
    """没有配置时不该产生任何"依赖未装"之类的噪音。"""
    async def _run():
        tools, errors = await mcp.build_mcp_tools([])
        assert tools == [] and errors == []

    asyncio.run(_run())


def test_example_server_is_disabled_by_default():
    """示例端点必须是停用的。

    它是一条**真的会被连接**的远程配置。enabled: true 且地址不可达时，
    每次构建 agent 都要白等一个超时（get_agent 在请求路径上）——
    2026-09-18 就因为它从 true 改成 false 才敢打开 MCP_ENABLED 总开关。
    """
    cfgs = {c.name: c for c in mcp.load_all_mcp_cfg()}
    assert cfgs["modelscope-fetch"].enabled is False


def test_runtime_mcp_settings_exist():
    """总开关与连接超时都有配置项（超时是在请求路径上的，必须有上限）。"""
    assert hasattr(settings, "mcp_enabled")
    assert settings.mcp_connect_timeout_seconds > 0
