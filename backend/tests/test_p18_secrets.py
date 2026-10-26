"""密钥安全测试：加解密、脱敏、以及"任何接口都不回明文密钥"。

需求 §7.6 的验收是"密钥数据库中为密文；任何接口不返回明文密钥；日志中无密钥"。
这个文件把三条都变成断言：
- 落库形态 → 直接读数据库列；
- 接口不泄漏 → 注册/创建/列表/更新/校验失败的**响应体**全查一遍；
- 日志 → 无法直接断言 logger 输出，改为断言"进入日志的那几个入口都过了脱敏函数"
  （`scrub_secrets`）以及错误对象本身已不含明文。
"""
import json

import pytest
from cryptography.fernet import Fernet

from app.core import crypto
from app.core.crypto import (
    decrypt_secret,
    encrypt_secret,
    mask_secret,
    scrub_secrets,
)
from tests.helpers import make_auth_client, create_user, unique_email

PLAINTEXT = "sk-supersecret-abcdefghijklmnop1234"


# ============================================================================
# 1. 加解密
# ============================================================================


def test_encrypt_decrypt_roundtrip():
    token = encrypt_secret(PLAINTEXT)
    assert token != PLAINTEXT
    assert decrypt_secret(token) == PLAINTEXT


def test_ciphertext_does_not_contain_plaintext():
    """密文里不能出现明文片段（这条看着显然，但它同时守住"别看错列"）。"""
    token = encrypt_secret(PLAINTEXT)
    assert PLAINTEXT not in token
    assert "supersecret" not in token


def test_same_plaintext_gives_different_ciphertext():
    """Fernet 每次加密都带新 IV —— 相同密钥两次加密结果不同。

    这一点有用：如果两行密文相同，说明某处把加密结果缓存/复用了，
    那会让"改密钥后旧密文解不开"这类问题变得更难定位。
    """
    assert encrypt_secret(PLAINTEXT) != encrypt_secret(PLAINTEXT)


def test_empty_string_roundtrip():
    assert decrypt_secret(encrypt_secret("")) == ""


def test_wrong_key_raises_actionable_error(monkeypatch):
    """换了 FERNET_KEY 之后，旧密文解不开时必须给出**可操作的**报错。

    绝不把密文或底层异常直接抛出来 —— 用户需要知道的是"重新填一次 key"，
    不是 `InvalidToken`。
    """
    from app.core.config import settings

    token = encrypt_secret(PLAINTEXT)
    try:
        monkeypatch.setattr(settings, "fernet_key", Fernet.generate_key().decode())
        crypto.reset_cipher()
        with pytest.raises(ValueError) as ei:
            decrypt_secret(token)
        assert "无法解密" in str(ei.value)
        assert "重新填写" in str(ei.value)
        assert token not in str(ei.value), "报错里不该带密文"
    finally:
        crypto.reset_cipher()   # 恢复成原密钥（monkeypatch 会还回 settings）


def test_corrupted_ciphertext_raises_value_error():
    with pytest.raises(ValueError):
        decrypt_secret("not-a-valid-fernet-token")


# ============================================================================
# 2. 脱敏
# ============================================================================


@pytest.mark.parametrize("raw,expected", [
    ("sk-1234567890abcd", "sk-****abcd"),
    ("sk-ant-api03-xyz987654321", "sk-****4321"),
    ("abcdef", "******"),          # 太短 → 全遮（否则遮了等于没遮）
    ("", ""),
])
def test_mask_secret(raw, expected):
    assert mask_secret(raw) == expected


def test_mask_secret_never_leaks_middle():
    raw = "sk-AAAAAAAAsecretmiddleBBBB"
    masked = mask_secret(raw)
    assert "secretmiddle" not in masked
    assert len(masked) < len(raw)


@pytest.mark.parametrize("text", [
    "401 for url: https://api.x.com/v1?key=sk-leaked1234567890",
    "Authorization: Bearer sk-leaked1234567890",
    "api_key=sk-leaked1234567890",
    "sk-ant-api03-leaked1234567890",
])
def test_scrub_secrets_removes_key_material(text):
    out = scrub_secrets(text)
    assert "sk-leaked1234567890" not in out
    assert "sk-ant-api03-leaked1234567890" not in out


def test_scrub_secrets_keeps_context_for_debugging():
    """脱敏要保留"是哪个字段出的问题"，否则排查时等于没有信息。"""
    out = scrub_secrets("api_key=sk-leaked1234567890 is invalid")
    assert "api_key" in out
    assert "is invalid" in out


def test_scrub_secrets_handles_empty():
    assert scrub_secrets("") == ""
    assert scrub_secrets(None) == ""


# ============================================================================
# 3. 接口不泄漏明文（端到端）
# ============================================================================


@pytest.fixture(scope="module")
def provider_user():
    from app.db.models import LlmProvider
    from app.db.session import SessionLocal

    user = create_user(email=unique_email("secret"), display_name="密钥测试")
    client, _ = make_auth_client(user, raise_server_exceptions=False)
    created = client.post("/api/v1/llm/providers", json={
        "name": "测试供应商",
        "protocol": "openai",
        "base_url": "https://example.invalid/v1",
        "api_key": PLAINTEXT,
        "model": "test-model",
        "scope": "user",
    })
    assert created.status_code == 201, created.text
    provider_id = created.json()["id"]

    yield client, user, provider_id

    db = SessionLocal()
    try:
        db.query(LlmProvider).filter(LlmProvider.user_id == user["id"]).delete(
            synchronize_session=False
        )
        db.commit()
    finally:
        db.close()


def test_create_response_has_hint_not_plaintext(provider_user):
    client, _user, provider_id = provider_user
    body = client.get("/api/v1/llm/providers").text
    assert PLAINTEXT not in body
    item = next(p for p in json.loads(body) if p["id"] == provider_id)
    assert item["api_key_hint"] == mask_secret(PLAINTEXT)
    assert item["has_api_key"] is True
    assert "api_key" not in item, "响应里不该有 api_key 字段（只有 hint）"


def test_database_stores_ciphertext(provider_user):
    from app.db.models import LlmProvider
    from app.db.session import SessionLocal

    _client, _user, provider_id = provider_user
    db = SessionLocal()
    try:
        row = db.get(LlmProvider, provider_id)
        stored = row.api_key_encrypted
    finally:
        db.close()
    assert stored and stored != PLAINTEXT
    assert PLAINTEXT not in stored
    assert decrypt_secret(stored) == PLAINTEXT     # 能解出来 → 说明真的可用


def test_provider_out_schema_has_no_plaintext_field():
    """结构性断言：把"加了个 api_key 字段"这种事挡在评审之前。"""
    from app.schemas.llm import ProviderOut

    fields = set(ProviderOut.model_fields)
    assert "api_key" not in fields
    assert "api_key_hint" in fields
    assert "api_key_encrypted" not in fields


def test_update_with_empty_key_keeps_existing(provider_user):
    """改名时不必回填密钥 —— 前端不把明文拉回浏览器，也就少一条泄漏路径。"""
    from app.db.models import LlmProvider
    from app.db.session import SessionLocal

    client, _user, provider_id = provider_user
    resp = client.put(f"/api/v1/llm/providers/{provider_id}",
                      json={"name": "改个名字", "api_key": ""})
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "改个名字"

    db = SessionLocal()
    try:
        row = db.get(LlmProvider, provider_id)
        assert decrypt_secret(row.api_key_encrypted) == PLAINTEXT
    finally:
        db.close()


def test_validation_error_body_does_not_echo_key(provider_user):
    """校验失败的响应体里不能回显密钥。

    FastAPI 的 422 会把"触发的输入值"放进错误详情里 —— 如果哪个字段是密钥
    （或者密钥被误当一个非法字段送进来），明文就会从错误响应泄漏出去。
    这条断言专门守这个。
    """
    client, _user, _pid = provider_user
    resp = client.post("/api/v1/llm/providers", json={
        "name": "坏地址",
        "protocol": "openai",
        "base_url": "ftp://bad-scheme",   # 触发校验失败
        "api_key": PLAINTEXT,
        "model": "m",
    })
    assert resp.status_code == 422
    assert PLAINTEXT not in resp.text


def test_other_user_cannot_touch_provider(provider_user):
    """别人的私有供应商：读不到、改不了（404，且响应里也没有密钥）。"""
    _client, _user, provider_id = provider_user
    other = create_user(email=unique_email("other"), display_name="别人")
    stranger, _ = make_auth_client(other, raise_server_exceptions=False)

    listed = stranger.get("/api/v1/llm/providers").json()
    assert all(p["id"] != provider_id for p in listed), "看到了别人的私有供应商"

    assert stranger.put(f"/api/v1/llm/providers/{provider_id}",
                        json={"name": "hack"}).status_code == 404
    assert stranger.delete(f"/api/v1/llm/providers/{provider_id}").status_code == 404
    assert PLAINTEXT not in stranger.get("/api/v1/llm/providers").text


def test_providers_list_only_shows_own(provider_user):
    """列表只返回**自己的**供应商（系统级那一层已退出，见 providers 模块注释）。"""
    client, _user, _pid = provider_user
    rows = client.get("/api/v1/llm/providers").json()
    assert rows, "至少要看到自己刚建的那条"
    assert all(not p["is_system"] for p in rows), "列表里不该出现系统级供应商"


def test_test_endpoint_accepts_temp_config_without_persisting(provider_user):
    """临时配置测试**不落库**：验证完不该多出一条供应商记录。"""
    client, _user, _pid = provider_user
    before = len(client.get("/api/v1/llm/providers").json())

    # 指向一个必然连不上的地址：只关心"没有落库"和"错误分类正常"
    resp = client.post("/api/v1/llm/providers/test", json={
        "protocol": "openai",
        "base_url": "http://127.0.0.1:9/v1",     # 保留端口，必然拒连
        "api_key": PLAINTEXT,
        "model": "m",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert PLAINTEXT not in resp.text, "探测结果里泄漏了密钥"

    after = len(client.get("/api/v1/llm/providers").json())
    assert after == before, "临时配置测试不该写入数据库"


def test_chat_error_detail_is_scrubbed(provider_user, monkeypatch):
    """对话失败时返回给前端的错误信息也必须脱敏。

    真实场景：供应商 SDK 抛的异常 message 里带请求 URL（含 api_key），
    这条 detail 会原样进 HTTP 响应、SSE 事件、以及可回放缓冲 ——
    不处理就等于把密钥交给浏览器和日志。
    """
    from app.agent import runtime

    client, user, _pid = provider_user
    sid = runtime.create_session(user["id"], "错误脱敏测试")

    async def _boom(*_a, **_k):
        raise RuntimeError("upstream said api_key=sk-leaked1234567890 is invalid")
        yield  # An event source, without contacting a provider.

    monkeypatch.setattr(runtime, "agent_event_stream", _boom)
    resp = client.post("/api/v1/chat", json={"message": "hi", "session_id": sid})
    assert resp.status_code == 500
    assert "sk-leaked1234567890" not in resp.text
    assert "upstream said" in resp.text, "脱敏不该把可排查的信息一起抹掉"


def test_probe_invalid_config_message_is_scrubbed():
    """构造失败（pydantic ValidationError）也不能回显密钥。

    pydantic 的报错会把**输入值**原样打进 message —— 而输入里就有 api_key。
    """
    import asyncio

    from app.agent.model import ProviderConfig
    from app.agent import providers as provider_svc

    # timeout 传一个非法值 → 构造时抛错，message 里通常带完整入参
    cfg = ProviderConfig(protocol="openai", model="m", api_key=PLAINTEXT,
                         parameters={"timeout": "not-a-number"}, dialect="openai")
    res = asyncio.run(provider_svc.probe_provider(cfg))
    assert res["ok"] is False
    assert PLAINTEXT not in res["message"]
