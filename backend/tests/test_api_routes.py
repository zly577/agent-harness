"""路由注册测试（不触发真实模型调用）。

注意：FastAPI 0.141.x 的 include_router 使用惰性 _IncludedRouter 代理，
app.routes 中无法直接读取 path，因此用 openapi 与行为断言代替。

业务接口改为"默认拒绝"，client 必须带登录凭证。
"""
from tests.helpers import auth_client

from app.main import app

client = auth_client(raise_server_exceptions=False)


def test_chat_route_in_openapi():
    assert "/api/v1/chat" in app.openapi()["paths"]


def test_chat_empty_body_422():
    # 空 body → 422 校验错误（证明路由存在，且未触发模型调用）
    resp = client.post("/api/v1/chat", json={})
    assert resp.status_code == 422


def test_chat_unknown_session_404():
    resp = client.post("/api/v1/chat", json={"message": "hi", "session_id": "bogus"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "session not found"
