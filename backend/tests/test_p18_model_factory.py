"""模型工厂测试：按协议返回正确的实例、参数容错、供应商解析顺序。

**不发任何真实网络请求**：只断言"构造出来的实例长什么样"。
`probe_provider` 的用例把 `ainvoke` 换成假的，验证的是"错误分类与脱敏"，
不是"能不能连上真供应商"（那属于人工验证，见 `_verify/`）。

⚠️ 这些用例**绝不插入系统级供应商**（`user_id IS NULL`）：那会改掉真实用户的
默认模型。需要验证"回退到系统默认"时，用 monkeypatch 替换解析函数。
"""
import pytest

from app.agent.model import (
    DEFAULT_ANTHROPIC_MAX_TOKENS,
    PROTOCOL_ANTHROPIC,
    PROTOCOL_OPENAI,
    ProviderConfig,
    build_llm,
    effective_parameters,
    env_bootstrap_config,
)
from app.agent import providers as provider_svc
from tests.helpers import create_user, unique_email


# ============================================================================
# 1. 按协议构造正确的实例
# ============================================================================


def test_openai_protocol_builds_chat_openai():
    """openai 协议 → ChatOpenAI，且三家**各自的规范参数名**真的生效。"""
    from langchain_openai import ChatOpenAI

    cfg = ProviderConfig(
        protocol=PROTOCOL_OPENAI,
        model="gpt-4o-mini",
        api_key="sk-test-openai",
        base_url="https://example.invalid/v1",
        parameters={"temperature": 0.3, "timeout": 12},
        dialect="openai",   # 显式指定，避免 base_url 里含 deepseek 字样时走特化分支
    )
    llm = build_llm(cfg)
    assert isinstance(llm, ChatOpenAI)
    assert llm.model_name == "gpt-4o-mini"
    assert llm.openai_api_base == "https://example.invalid/v1"
    assert llm.openai_api_key.get_secret_value() == "sk-test-openai"
    assert llm.temperature == 0.3
    assert llm.request_timeout == 12


def test_deepseek_base_url_keeps_chat_deepseek():
    """base_url 命中 DeepSeek 官方域名 → 仍用 ChatDeepSeek（保住 reasoning_content 解析）。

    这是 P14「长推理期间不再黑屏」的依赖项：换成裸 ChatOpenAI 有丢字段的风险。
    """
    from langchain_deepseek import ChatDeepSeek

    cfg = ProviderConfig(
        protocol=PROTOCOL_OPENAI,
        model="deepseek-flash",
        api_key="sk-test-deepseek",
        base_url="https://api.deepseek.com",
        parameters={"temperature": 0.0, "timeout": 30},
    )
    llm = build_llm(cfg)   # dialect 默认 auto
    assert isinstance(llm, ChatDeepSeek)
    assert llm.model_name == "deepseek-flash"
    assert llm.request_timeout == 30
    assert llm.temperature == 0.0


def test_dialect_override_can_force_plain_openai():
    """dialect=openai 时即便地址是 DeepSeek 也用 ChatOpenAI（留一条逃生通道）。"""
    from langchain_openai import ChatOpenAI

    cfg = ProviderConfig(
        protocol=PROTOCOL_OPENAI,
        model="deepseek-flash",
        api_key="sk-x",
        base_url="https://api.deepseek.com",
        dialect="openai",
    )
    assert isinstance(build_llm(cfg), ChatOpenAI)


def test_anthropic_protocol_builds_chat_anthropic_with_correct_param_names():
    """anthropic 协议 → ChatAnthropic，**参数名与另两家不同**，必须逐个核实。

    这三个类的 `model_config.extra` 都是 `ignore`：用错名字（比如传 `base_url`）
    **不会报错**，只会静默走"默认地址 + 空密钥"，然后回一个看不懂的 401。
    所以这里断言的是实例属性，而不是"构造没抛异常"。
    """
    from langchain_anthropic import ChatAnthropic

    cfg = ProviderConfig(
        protocol=PROTOCOL_ANTHROPIC,
        model="claude-sonnet-4-20250514",
        api_key="sk-ant-test",
        base_url="https://anthropic.example.invalid",
        parameters={"temperature": 0.2, "timeout": 25, "max_tokens": 2048},
    )
    llm = build_llm(cfg)
    assert isinstance(llm, ChatAnthropic)
    assert llm.model == "claude-sonnet-4-20250514"
    assert llm.anthropic_api_url == "https://anthropic.example.invalid"
    assert llm.anthropic_api_key.get_secret_value() == "sk-ant-test"
    assert llm.max_tokens == 2048
    assert llm.temperature == 0.2
    assert llm.default_request_timeout == 25


def test_anthropic_max_tokens_gets_default():
    """Anthropic 的 max_tokens **必填**；没配时必须兜上默认值，否则第一次调用就 400。"""
    from langchain_anthropic import ChatAnthropic

    cfg = ProviderConfig(
        protocol=PROTOCOL_ANTHROPIC,
        model="claude-3-5-haiku-latest",
        api_key="sk-ant-x",
    )
    llm = build_llm(cfg)
    assert isinstance(llm, ChatAnthropic)
    assert llm.max_tokens == DEFAULT_ANTHROPIC_MAX_TOKENS


def test_no_base_url_uses_library_default():
    """base_url 留空时不硬塞地址 —— 交给 SDK 用它自己的官方默认值。"""
    cfg = ProviderConfig(protocol=PROTOCOL_OPENAI, model="gpt-4o-mini",
                         api_key="sk-x", dialect="openai")
    llm = build_llm(cfg)
    assert not (llm.openai_api_base or "").strip()


# ============================================================================
# 2. 参数容错
# ============================================================================


@pytest.mark.parametrize("model", [
    "deepseek-reasoner",
    "o1-preview",
    "o3-mini",
    "gpt-5-thinking",
])
def test_temperature_dropped_for_reasoning_models(model):
    """推理模型不接受 temperature，构造时**主动剔除**（别等 400 再让用户猜）。"""
    cfg = ProviderConfig(protocol=PROTOCOL_OPENAI, model=model,
                         api_key="sk-x", parameters={"temperature": 0.7}, dialect="openai")
    params, dropped = effective_parameters(cfg)
    assert "temperature" not in params
    assert "temperature" in dropped

    llm = build_llm(cfg)
    # 不传 temperature 时由 SDK 决定默认值（不是我们硬塞的 0.7）
    assert getattr(llm, "temperature", None) != 0.7


def test_temperature_kept_for_normal_models():
    cfg = ProviderConfig(protocol=PROTOCOL_OPENAI, model="gpt-4o-mini",
                         api_key="sk-x", parameters={"temperature": 0.9}, dialect="openai")
    params, dropped = effective_parameters(cfg)
    assert params["temperature"] == 0.9
    assert dropped == []


def test_none_parameters_fall_back_to_defaults():
    """显式传 None 的参数按"没配"处理（前端清空输入框时会给 None）。"""
    cfg = ProviderConfig(protocol=PROTOCOL_OPENAI, model="gpt-4o-mini",
                         api_key="sk-x", parameters={"temperature": None, "timeout": None},
                         dialect="openai")
    params, _ = effective_parameters(cfg)
    assert params["temperature"] == 0.0
    assert params["timeout"] == 60


def test_custom_parameters_are_passed_through():
    """未知参数原样透传（供应商各自有扩展参数，白名单会把它们挡掉）。"""
    cfg = ProviderConfig(protocol=PROTOCOL_OPENAI, model="qwen-max", api_key="sk-x",
                         base_url="https://example.invalid/v1",
                         parameters={"top_p": 0.8, "enable_thinking": True})
    llm = build_llm(cfg)
    assert llm.top_p == 0.8


# ============================================================================
# 3. 非法配置
# ============================================================================


def test_unknown_protocol_raises():
    with pytest.raises(ValueError, match="不支持的协议"):
        build_llm(ProviderConfig(protocol="gemini", model="x", api_key="k"))


def test_empty_model_raises():
    with pytest.raises(ValueError, match="没有配置模型名"):
        build_llm(ProviderConfig(protocol=PROTOCOL_OPENAI, model="  ", api_key="k"))


def test_build_llm_without_config_uses_env_bootstrap(monkeypatch):
    """无参调用保持向后兼容（既有测试/探针都是这么用的）。"""
    from langchain_deepseek import ChatDeepSeek
    monkeypatch.setattr("app.agent.model.settings.deepseek_api_key", "sk-test-placeholder")

    cfg = env_bootstrap_config()
    llm = build_llm()
    assert isinstance(llm, ChatDeepSeek)
    assert llm.model_name == cfg.model


# ============================================================================
# 4. 供应商解析顺序：会话 → 用户默认 → 系统默认
# ============================================================================


@pytest.fixture
def user_with_two_providers():
    """一个有两条私有供应商的账号（第一条被自动设成默认）。"""
    from app.core.crypto import encrypt_secret, mask_secret
    from app.db.models import LlmProvider
    from app.db.session import SessionLocal

    user = create_user(email=unique_email("prov"), display_name="供应商解析")
    db = SessionLocal()
    rows = []
    try:
        for i, (name, model) in enumerate([("甲的默认", "model-a"), ("甲的另一条", "model-b")]):
            row = LlmProvider(
                id=f"{user['id'][:24]}{i:08d}",   # 32 hex，保证唯一
                user_id=user["id"],
                name=name,
                protocol=PROTOCOL_OPENAI,
                base_url="https://example.invalid/v1",
                api_key_encrypted=encrypt_secret(f"sk-{model}"),
                api_key_hint=mask_secret(f"sk-{model}"),
                model=model,
                parameters="{}",
                is_default=(i == 0),
                enabled=True,
            )
            db.add(row)
            rows.append(row)
        db.commit()
        ids = [r.id for r in rows]
    finally:
        db.close()
    return user, ids


def test_session_provider_wins(user_with_two_providers):
    """会话绑定的供应商优先级最高（第二条不是默认，但会话绑了它就该用它）。"""
    from app.db.session import SessionLocal

    user, ids = user_with_two_providers
    db = SessionLocal()
    try:
        cfg, source = provider_svc.resolve_provider(db, user["id"], session_provider_id=ids[1])
    finally:
        db.close()
    assert cfg.model == "model-b"
    assert source == "会话绑定"


def test_user_default_used_when_no_session_binding(user_with_two_providers):
    from app.db.session import SessionLocal

    user, _ids = user_with_two_providers
    db = SessionLocal()
    try:
        cfg, source = provider_svc.resolve_provider(db, user["id"], None)
    finally:
        db.close()
    assert cfg.model == "model-a"
    assert source == "用户默认"


def test_no_provider_raises_instead_of_falling_back_to_system():
    """没有任何**自己的**供应商时必须报错 —— 不得回退到系统级或 .env。

    这是"每个用户必须自己配模型，否则没有模型可用"这条策略的守门测试。
    早期版本会回退到系统级共享供应商（登录就有模型用），2026-09-18 按使用方要求取消。
    """
    from app.db.session import SessionLocal

    user = create_user(email=unique_email("noprov"), display_name="没配模型的用户")
    db = SessionLocal()
    try:
        # 这个用户没有任何私有供应商；库里即使还有别的行（系统级/别人的），也不能被用到。
        with pytest.raises(provider_svc.ProviderNotConfigured) as ei:
            provider_svc.resolve_provider(db, user["id"], None)
    finally:
        db.close()
    msg = str(ei.value)
    assert "模型" in msg and ("配置" in msg or "新增" in msg)


def test_disabled_session_provider_falls_back(user_with_two_providers):
    """会话绑定的供应商被停用/删除后必须回退，而不是报错或静默用旧的。"""
    from app.db.models import LlmProvider
    from app.db.session import SessionLocal

    user, ids = user_with_two_providers
    db = SessionLocal()
    try:
        db.get(LlmProvider, ids[1]).enabled = False
        db.commit()
        cfg, source = provider_svc.resolve_provider(db, user["id"], session_provider_id=ids[1])
    finally:
        db.close()
    assert source == "用户默认"
    assert cfg.model == "model-a"


def test_resolve_raises_clear_error_when_nothing_available(monkeypatch):
    """一处都没有时，报的必须是**可操作的中文**，而不是框架异常。"""
    from app.db.session import SessionLocal

    # 把唯一来源堵掉：库里的默认解析返回 None
    monkeypatch.setattr(provider_svc, "_pick_default", lambda db, uid: None)

    db = SessionLocal()
    try:
        with pytest.raises(provider_svc.ProviderNotConfigured) as ei:
            provider_svc.resolve_provider(db, "whoever", None)
    finally:
        db.close()
    msg = str(ei.value)
    # 断言"这句话是给人看、且告诉人下一步怎么做"，而不是抄一遍原文
    assert "模型" in msg
    assert "新增" in msg or "配置" in msg
    assert "模型" in msg and "「模型」" in msg, "提示里要指明按钮/入口的名字"


# ============================================================================
# 5. 连通性探测：错误分类 + 脱敏（不联网）
# ============================================================================


def _fake_llm(raises: Exception | None = None, reply: str = "pong"):
    class _Msg:
        content = reply

    class _Fake:
        async def ainvoke(self, _messages):
            if raises is not None:
                raise raises
            return _Msg()

    return _Fake()


def test_probe_success(monkeypatch):
    monkeypatch.setattr(provider_svc, "build_llm", lambda cfg: _fake_llm(reply="pong"))
    import asyncio

    res = asyncio.run(provider_svc.probe_provider(
        ProviderConfig(protocol=PROTOCOL_OPENAI, model="m", api_key="sk-x")
    ))
    assert res["ok"] is True
    assert res["reply"] == "pong"
    assert "连接成功" in res["message"]


@pytest.mark.parametrize("exc,expect", [
    (Exception("401 Unauthorized: invalid api key"), "API Key 无效"),
    (Exception("404 model not found"), "base_url 或模型名不存在"),
    (Exception("Connection refused"), "连接不上"),
])
def test_probe_error_classification(monkeypatch, exc, expect):
    """把 SDK 的各种报错翻成"下一步该做什么"。"""
    monkeypatch.setattr(provider_svc, "build_llm", lambda cfg: _fake_llm(raises=exc))
    import asyncio

    res = asyncio.run(provider_svc.probe_provider(
        ProviderConfig(protocol=PROTOCOL_OPENAI, model="m", api_key="sk-x")
    ))
    assert res["ok"] is False
    assert expect in res["message"], res


def test_probe_error_detail_is_scrubbed(monkeypatch):
    """探测失败的原因里**绝不能带明文密钥**（SDK 报错常把 URL/头带进 message）。"""
    leaky = Exception("401 for url: https://api.example.com/v1/chat?api_key=sk-leaked1234567890")
    monkeypatch.setattr(provider_svc, "build_llm", lambda cfg: _fake_llm(raises=leaky))
    import asyncio

    res = asyncio.run(provider_svc.probe_provider(
        ProviderConfig(protocol=PROTOCOL_OPENAI, model="m", api_key="sk-leaked1234567890")
    ))
    assert res["ok"] is False
    assert "sk-leaked1234567890" not in res["detail"]
    assert "****" in res["detail"] or "sk-" not in res["detail"]


def test_probe_reports_invalid_config():
    """配置本身不合法（比如没填 model）时，探测应给出"配置不合法"而不是抛异常。"""
    import asyncio

    res = asyncio.run(provider_svc.probe_provider(
        ProviderConfig(protocol=PROTOCOL_OPENAI, model="", api_key="sk-x")
    ))
    assert res["ok"] is False
    assert "配置不合法" in res["message"]


# ============================================================================
# 6. 摘要中间件必须与主模型同源
# ============================================================================


def test_summary_middleware_uses_given_model_instance():
    """上下文压缩用的模型必须就是主 Agent 那个实例。

    否则会出现"对话走用户选的供应商、摘要偷偷走另一个模型" ——
    这条路径在界面上完全不可见，只能靠这条断言守住。
    """
    from app.agent.context import build_summary_middleware

    cfg = ProviderConfig(protocol=PROTOCOL_OPENAI, model="gpt-4o-mini",
                         api_key="sk-x", dialect="openai")
    model = build_llm(cfg)
    mw = build_summary_middleware(model)

    # SummarizationMiddleware 把模型存在 `model` 属性上
    assert getattr(mw, "model", None) is model


# ============================================================================
# 7. 子智能体必须继承主模型
# ============================================================================


def test_subagents_do_not_pin_their_own_model():
    """子智能体（task 委派）**不指定 model**，从而继承主 Agent 的实例。

    这是"选了哪个供应商，子智能体也走哪个"的**唯一机制**：
    `create_deep_agent(subagents=[...])` 里 SubAgent 一旦带了 model，
    它就会用那个模型，跟主模型脱钩 —— 而现象是"主对话正常、委派出去的结果风格不对"，
    很难联想到是模型不同。
    """
    from app.agent.subagents import load_all_subagents_cfg, build_subagent

    configs = load_all_subagents_cfg()
    assert configs, "至少要有一个子智能体配置，否则这条断言没有意义"

    for cfg in configs:
        spec = build_subagent(cfg)
        model = spec.get("model") if isinstance(spec, dict) else getattr(spec, "model", None)
        assert model in (None, ""), (
            f"子智能体 {cfg.name} 指定了 model={model!r}，会与主模型脱钩 —— "
            "请去掉它，让子智能体继承主 Agent 的模型"
        )
