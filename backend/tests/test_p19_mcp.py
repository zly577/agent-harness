"""测试：MCP 工具源（配置构造 / 仓储 / 管理 API）。

## 不触发真实网络的原则

除了"故意连一个必然失败的地址"来验失败路径（127.0.0.1:1，瞬间 refused），
其余用例都在测**配置是怎么被翻译成连接的** —— 那是本项目自己的代码，
不需要真的有个 MCP server 才能验。

## 为什么重点测 `to_connection()`

旧代码就是栽在这里：把错的键名（transport=/client_name=）交给适配器，
而那些 TypedDict **在运行时不校验**，错误会在很深的调用栈里才炸。
所以"多传一个 timeout 给 websocket"这类问题只能靠断言挡住。
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from app.agent import mcp as mcp_mod
from app.agent import mcp_registry as registry
from app.agent.mcp import McpServerConfig, adapters_missing_hint, probe_mcp_server
from app.core.crypto import decrypt_secret
from app.db.models import McpServer
from app.db.session import SessionLocal
from tests.helpers import create_user, make_auth_client


# ============================================================================
# 配置 → 连接字典
# ============================================================================


def test_streamable_http_connection():
    cfg = McpServerConfig(
        name="fetch", transport="streamable_http", url="https://x/mcp",
        headers={"Authorization": "Bearer t"}, timeout=10, sse_read_timeout=20,
    )
    conn = cfg.to_connection()
    assert conn["transport"] == "streamable_http"
    assert conn["url"] == "https://x/mcp"
    assert conn["headers"] == {"Authorization": "Bearer t"}
    assert conn["timeout"] == 10 and conn["sse_read_timeout"] == 20


def test_sse_connection_has_no_terminate_on_close():
    """`terminate_on_close` 只有 streamable_http 认，别的 transport 不许带。"""
    cfg = McpServerConfig(name="s", transport="sse", url="https://x/sse")
    assert "terminate_on_close" not in cfg.to_connection()


def test_websocket_connection_minimal():
    """websocket 只接受 url（+session_kwargs）；多传 timeout / headers 会在深层炸掉。"""
    cfg = McpServerConfig(name="w", transport="websocket", url="ws://x/ws", timeout=5)
    conn = cfg.to_connection()
    assert conn == {"transport": "websocket", "url": "ws://x/ws"}

    with_headers = McpServerConfig(
        name="w2", transport="websocket", url="ws://x/ws", headers={"A": "1"}
    )
    assert "headers" not in with_headers.to_connection()


def test_stdio_connection():
    cfg = McpServerConfig(
        name="local", transport="stdio", command="npx",
        args=["-y", "pkg"], env={"K": "V"}, cwd="D:/tmp",
    )
    conn = cfg.to_connection()
    assert conn["transport"] == "stdio"
    assert conn["command"] == "npx" and conn["args"] == ["-y", "pkg"]
    assert conn["env"] == {"K": "V"} and conn["cwd"] == "D:/tmp"
    # stdio 不该出现 http 那些键
    assert "url" not in conn and "headers" not in conn


def test_transport_normalized_and_whitelisted():
    assert McpServerConfig(name="a", transport="SSE", url="https://x").transport == "sse"
    with pytest.raises(ValueError):
        McpServerConfig(name="a", transport="grpc", url="https://x")


def test_websocket_requires_url():
    with pytest.raises(ValueError):
        McpServerConfig(name="a", transport="websocket", url="")


# ============================================================================
# 仓储：行 ↔ 配置（含加密）
# ============================================================================


def _new_row(**kw) -> McpServer:
    return McpServer(id=uuid.uuid4().hex, user_id=kw.pop("user_id", "u_test"), **kw)


def test_apply_and_read_back():
    cfg = McpServerConfig(
        name="mine", transport="streamable_http", url="https://x/mcp",
        headers={"Authorization": "Bearer sk-abcdefgh1234"}, enabled=True,
    )
    row = _new_row()
    registry.apply_config(row, cfg)

    assert row.url == "https://x/mcp"
    # 密文里绝不能出现明文
    assert "sk-abcdefgh1234" not in row.headers_encrypted
    # 展示用的键名列不需要解密就能读
    assert json.loads(row.header_keys) == ["Authorization"]
    assert registry.header_keys(row) == ["Authorization"]
    assert registry.has_headers(row) is True
    assert registry.has_env(row) is False

    back = registry.config_from_row(row)
    assert back.headers == cfg.headers
    assert back.transport == "streamable_http" and back.enabled is True


def test_env_roundtrip_for_stdio():
    cfg = McpServerConfig(
        name="cli", transport="stdio", command="node", args=["s.js"], env={"TOKEN": "t-12345678"},
    )
    row = _new_row()
    registry.apply_config(row, cfg)
    assert "t-12345678" not in row.env_encrypted
    assert registry.config_from_row(row).env == {"TOKEN": "t-12345678"}
    assert registry.args_of(row) == ["s.js"]


def test_empty_headers_means_clear():
    """`{}` = 清空（不是"不改"）。这个语义由 API 层的 null/{} 区分，仓储只看结果。"""
    row = _new_row()
    registry.apply_config(row, McpServerConfig(name="a", url="https://x", headers={"A": "1"}))
    assert registry.has_headers(row) is True
    registry.apply_config(row, McpServerConfig(name="a", url="https://x", headers={}))
    assert registry.has_headers(row) is False
    assert registry.header_keys(row) == []


def test_decrypt_failure_is_actionable():
    """密文坏了要给"请重新填写"这种人话，不能把密文/底层异常抛出去。"""
    row = _new_row()
    registry.apply_config(row, McpServerConfig(name="a", url="https://x", headers={"A": "1"}))
    row.headers_encrypted = "not-a-fernet-token"
    with pytest.raises(ValueError) as e:
        registry.config_from_row(row)
    assert "重新填写" in str(e.value)


def test_owned_row_enforces_owner():
    db = SessionLocal()
    row = _new_row(user_id="owner-1", name="x", url="https://x", transport="streamable_http")
    try:
        registry.apply_config(row, McpServerConfig(
            name="x", transport="streamable_http", url="https://x"))
        db.add(row)
        db.commit()
        assert registry.owned_row(db, row.id, "owner-1") is not None
        assert registry.owned_row(db, row.id, "someone-else") is None
        assert registry.owned_row(db, "no-such-id", "owner-1") is None
    finally:
        db.delete(row)
        db.commit()
        db.close()


def test_load_mcp_configs_dedupes_in_favor_of_db():
    """同名时数据库那条赢（YAML 是共享的，不该盖掉用户自己配的）。"""
    user_id = f"u_{uuid.uuid4().hex[:8]}"
    db = SessionLocal()
    row = _new_row(user_id=user_id, name="modelscope-fetch", url="https://mine/mcp",
                   transport="streamable_http")
    try:
        registry.apply_config(row, McpServerConfig(
            name="modelscope-fetch", transport="streamable_http", url="https://mine/mcp"))
        db.add(row)
        db.commit()

        cfgs = registry.load_mcp_configs(user_id)
        mine = [c for c in cfgs if c.name == "modelscope-fetch"]
        assert len(mine) == 1
        assert mine[0].url == "https://mine/mcp"
    finally:
        db.delete(row)
        db.commit()
        db.close()


def test_load_mcp_configs_filters_disabled():
    user_id = f"u_{uuid.uuid4().hex[:8]}"
    db = SessionLocal()
    row = _new_row(user_id=user_id, name="off", transport="streamable_http", url="https://x")
    try:
        registry.apply_config(row, McpServerConfig(
            name="off", transport="streamable_http", url="https://x", enabled=False))
        db.add(row)
        db.commit()
        assert "off" not in {c.name for c in registry.load_mcp_configs(user_id)}
    finally:
        db.delete(row)
        db.commit()
        db.close()


# ============================================================================
# 连通性探测（失败路径）
# ============================================================================


def test_probe_unreachable_endpoint_fails_cleanly():
    """连一个必然 refused 的地址：要 ok=False + 中文原因，不能抛异常。"""
    cfg = McpServerConfig(name="dead", transport="streamable_http", url="http://127.0.0.1:1/mcp")
    result = asyncio.run(probe_mcp_server(cfg, timeout=5))
    assert result["ok"] is False
    assert result["tools"] == []
    assert result["message"]


def test_windows_pywin32_bootstrap_is_safe():
    """Windows 专属兜底：别的平台上必须是空操作，且调用不抛。

    它的存在意义见 `mcp._ensure_windows_pywin32` 的注释：pywin32 的 DLL 目录
    靠 `.pth` 在**解释器启动时**加进 sys.path，所以"包比进程新"时会报
    ModuleNotFoundError —— 而用户侧的感受是"我明明装了 pywintypes"。
    """
    mcp_mod._ensure_windows_pywin32()
    assert True


def test_adapters_hint_is_actionable():
    assert "pip install" in adapters_missing_hint()
    # 装好之后 _has_adapters 必须为真（否则整套 API 都在自欺欺人）
    assert mcp_mod._has_adapters() is True


# ============================================================================
# 管理 API
# ============================================================================


def test_api_requires_auth():
    from fastapi.testclient import TestClient

    from app.main import app

    resp = TestClient(app).get("/api/v1/mcp/servers")
    assert resp.status_code in (401, 403)


def test_api_crud_and_ownership():
    client, user = make_auth_client()
    other, _ = make_auth_client()

    name = f"srv_{uuid.uuid4().hex[:8]}"

    # 增
    resp = client.post("/api/v1/mcp/servers", json={
        "name": name, "transport": "streamable_http",
        "url": "https://example.com/mcp",
        "headers": {"Authorization": "Bearer sk-should-never-come-back"},
    })
    assert resp.status_code == 201, resp.text
    created = resp.json()
    server_id = created["id"]
    # 响应里绝不能回显 header 的值
    assert created["header_keys"] == ["Authorization"]
    assert created["has_headers"] is True
    assert "sk-should-never-come-back" not in resp.text

    # 列
    assert name in {s["name"] for s in client.get("/api/v1/mcp/servers").json()}
    # 别人的列表里没有
    assert name not in {s["name"] for s in other.get("/api/v1/mcp/servers").json()}

    # 改（headers 不传 = 不动）
    resp = client.put(f"/api/v1/mcp/servers/{server_id}", json={"url": "https://e2/mcp"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["url"] == "https://e2/mcp"
    assert resp.json()["header_keys"] == ["Authorization"]

    # 别人改不到（404，不泄漏存在性）
    # 注意 url 必须是合法形状：pydantic 校验跑在归属校验之前，
    # 传个 "x" 会先拿 422，那就不是在测归属了。
    assert other.put(f"/api/v1/mcp/servers/{server_id}",
                     json={"url": "https://evil.example/mcp"}).status_code == 404
    assert other.delete(f"/api/v1/mcp/servers/{server_id}").status_code == 404

    # 删
    assert client.delete(f"/api/v1/mcp/servers/{server_id}").json()["ok"] is True
    assert name not in {s["name"] for s in client.get("/api/v1/mcp/servers").json()}


def test_api_rejects_bad_config():
    client, _ = make_auth_client()

    # streamable_http 没给 url
    resp = client.post("/api/v1/mcp/servers", json={"name": f"bad_{uuid.uuid4().hex[:6]}",
                                                    "transport": "streamable_http"})
    assert resp.status_code in (400, 422)

    # 不支持的 transport
    resp = client.post("/api/v1/mcp/servers", json={"name": f"bad2_{uuid.uuid4().hex[:6]}",
                                                    "transport": "grpc", "url": "https://x"})
    assert resp.status_code in (400, 422)

    # stdio 没给 command
    resp = client.post("/api/v1/mcp/servers", json={"name": f"bad3_{uuid.uuid4().hex[:6]}",
                                                    "transport": "stdio"})
    assert resp.status_code in (400, 422)


def test_api_rejects_duplicate_name():
    client, _ = make_auth_client()
    name = f"dup_{uuid.uuid4().hex[:8]}"
    payload = {"name": name, "transport": "streamable_http", "url": "https://x/mcp"}
    first = client.post("/api/v1/mcp/servers", json=payload)
    assert first.status_code == 201, first.text
    assert client.post("/api/v1/mcp/servers", json=payload).status_code == 409
    client.delete(f"/api/v1/mcp/servers/{first.json()['id']}")


def test_api_status_reports_switch():
    client, _ = make_auth_client()
    data = client.get("/api/v1/mcp/status").json()
    assert "enabled" in data and "adapters_installed" in data
    assert data["connect_timeout_seconds"] > 0


def test_api_test_temp_config_failure_is_readable():
    """临时配置测试：失败要给出中文原因，且不能把密钥回显出去。"""
    client, _ = make_auth_client()
    resp = client.post("/api/v1/mcp/servers/test", json={
        "name": "tmp", "transport": "streamable_http",
        "url": "http://127.0.0.1:1/mcp",
        "headers": {"Authorization": "Bearer sk-secret-value"},
        "timeout": 5,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert "sk-secret-value" not in json.dumps(body, ensure_ascii=False)


# ============================================================================
# inputSchema 规范化（draft-04 布尔排他边界 → draft-06+ 数值写法）
#
# 背景：2026-09-30 接入 `pdf-reader-mcp`，它的 `read_pdf` 里带了
#   {"type": "integer", "minimum": 0, "exclusiveMinimum": true}
# 这个 draft-04 写法被 DeepSeek 的严格校验拒掉：
#   Invalid schema for function 'read_pdf': true is not of type "number"
# 而 schema 是跟着**每一次** model 请求发出去的 ⇒ 整段会话所有任务全 400。
# 所以这里钉住的不是"某个远端服务的脾气"，而是"我们出站前必须体检 schema"。
# ============================================================================


async def _noop_coroutine(runtime=None, **kwargs):
    return "ok"


def _pdf_reader_schema_like() -> dict:
    """裁自真实 `read_pdf` 的 inputSchema（保留出问题的那条路径）。"""
    return {
        "type": "object",
        "properties": {
            "sources": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "pages": {
                            "anyOf": [
                                {
                                    "type": "array",
                                    "items": {
                                        "type": "integer",
                                        "exclusiveMinimum": True,
                                        "minimum": 0,
                                    },
                                    "minItems": 1,
                                },
                                {"type": "string", "minLength": 1},
                            ]
                        }
                    },
                    "additionalProperties": False,
                },
            },
            "include_full_text": {"type": "boolean", "default": False},
            "include_metadata": {"type": "boolean", "default": True},
        },
        "required": ["sources"],
        "additionalProperties": False,
    }


def test_exclusive_minimum_true_becomes_numeric_bound():
    """draft-04「>= 边界 + 开关」→ draft-06+「数值排他边界」，语义等价。"""
    assert mcp_mod.normalize_json_schema(
        {"type": "integer", "exclusiveMinimum": True, "minimum": 0}
    ) == {"type": "integer", "exclusiveMinimum": 0}


def test_exclusive_maximum_true_becomes_numeric_bound():
    assert mcp_mod.normalize_json_schema({"maximum": 10, "exclusiveMaximum": True}) == {
        "exclusiveMaximum": 10
    }


def test_exclusive_flag_false_keeps_inclusive_bound():
    """开关为 false = 不含排他约束，边界本身要留着。"""
    assert mcp_mod.normalize_json_schema({"minimum": 0, "exclusiveMinimum": False}) == {"minimum": 0}


def test_exclusive_true_without_bound_is_dropped():
    """没有兄弟键可提升时保守丢掉该键，绝不留下布尔值。"""
    assert mcp_mod.normalize_json_schema({"exclusiveMinimum": True}) == {}
    assert mcp_mod.normalize_json_schema({"exclusiveMaximum": False}) == {}


def test_legal_schema_booleans_are_untouched():
    """`additionalProperties` / `default` 的布尔值是合法的，不能被误伤。"""
    src = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"x": {"type": "boolean", "default": True}},
    }
    assert mcp_mod.normalize_json_schema(src) == src


def test_nested_draft04_inside_anyof_is_repaired():
    """真实 read_pdf 的嵌套结构（properties → items → anyOf → items）要能钻进去。"""
    fixed = mcp_mod.normalize_json_schema(_pdf_reader_schema_like())
    leaf = fixed["properties"]["sources"]["items"]["properties"]["pages"]["anyOf"][0]["items"]
    assert leaf == {"type": "integer", "exclusiveMinimum": 0}
    # 合法布尔值必须原样保留
    assert fixed["properties"]["include_full_text"] == {"type": "boolean", "default": False}
    assert fixed["additionalProperties"] is False
    assert fixed["properties"]["sources"]["items"]["additionalProperties"] is False


def test_no_boolean_exclusive_keys_survive_anywhere():
    """回归：规范化后的 schema 全树都不能再有布尔型排他边界。"""

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("exclusiveMinimum", "exclusiveMaximum"):
                    assert not isinstance(value, bool), f"{key}={value}"
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(mcp_mod.normalize_json_schema(_pdf_reader_schema_like()))


def test_normalize_is_idempotent():
    once = mcp_mod.normalize_json_schema(_pdf_reader_schema_like())
    assert mcp_mod.normalize_json_schema(once) == once


def test_normalize_tool_schema_swaps_schema_and_invalidates_cache():
    """`model_copy` 必须清掉 tool_call_schema 缓存，否则发出去的还是旧 schema。"""
    from langchain_core.tools import StructuredTool

    tool = StructuredTool(
        name="read_pdf",
        description="read a pdf",
        args_schema=_pdf_reader_schema_like(),
        coroutine=_noop_coroutine,
    )
    # 先访问一次，把 memo 建起来（模拟"已经 bind 过一次"）
    assert isinstance(tool.tool_call_schema, dict)

    fixed = mcp_mod.normalize_tool_schema(tool)
    assert fixed is not tool
    leaf = fixed.tool_call_schema["properties"]["sources"]["items"]["properties"]["pages"]["anyOf"][0][
        "items"
    ]
    assert leaf == {"type": "integer", "exclusiveMinimum": 0}


def test_normalize_tool_schema_returns_same_object_when_clean():
    """干净的 schema 不做无谓重建（保持对象同一性，避免副作用）。"""
    from langchain_core.tools import StructuredTool

    tool = StructuredTool(
        name="t",
        description="d",
        args_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        coroutine=_noop_coroutine,
    )
    assert mcp_mod.normalize_tool_schema(tool) is tool


def test_normalize_tool_schema_ignores_pydantic_args_schema():
    """本地工具的 schema 是 pydantic 模型类，不属于远端 schema 的修复范围。"""
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel

    class Args(BaseModel):
        x: int

    tool = StructuredTool(name="t", description="d", args_schema=Args, coroutine=_noop_coroutine)
    assert mcp_mod.normalize_tool_schema(tool) is tool
