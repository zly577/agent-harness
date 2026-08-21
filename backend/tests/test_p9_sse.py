"""P9 测试：SSE 流式端点路由（不触发真实模型）。

只验证路由注册、HTTP 层行为（缺 session 返回 404 等），不实际流式调用模型。
"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_stream_route_registered():
    paths = list(app.openapi()["paths"].keys())
    assert "/api/v1/chat/stream" in paths


def test_stream_unknown_session_404():
    resp = client.post(
        "/api/v1/chat/stream",
        json={"message": "hi", "session_id": "bogus-session"},
    )
    # 会话不存在 → 404（路由/校验层生效，不触发模型）
    assert resp.status_code == 404


def test_stream_empty_body_422():
    resp = client.post("/api/v1/chat/stream", json={})
    assert resp.status_code == 422
