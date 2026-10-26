import asyncio
import importlib
import httpx
import pytest
from pydantic import ValidationError
search = importlib.import_module("app.agent.web_search")


RSS = b'''<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><item><title>Python</title><link>https://python.org</link>
<description>Official &amp; docs</description></item></channel></rss>'''
DDG = '''<div class="result results_links"><h2><a class="result__a"
href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fpython.org">Python</a></h2>
<a class="result__snippet">Official <b>docs</b></a></div>'''


@pytest.fixture(autouse=True)
def search_defaults(monkeypatch):
    monkeypatch.setattr(search.settings, "tavily_api_key", "")
    monkeypatch.setattr(search.settings, "web_search_proxy", "")
    monkeypatch.setattr(search.settings, "web_search_timeout_seconds", 30.0)
    monkeypatch.setattr(search.settings, "web_search_provider_timeout_seconds", 5.0)
    monkeypatch.setattr(search.settings, "mcp_tool_call_timeout_seconds", 120.0)


def mock_client(monkeypatch, respond, proxy=None):
    original = httpx.AsyncClient
    def client(**kwargs):
        assert kwargs["trust_env"] is False
        # A real proxy installs its own transport mounts in httpx; verify the
        # configuration, then keep all requests on MockTransport for this test.
        assert kwargs.pop("proxy") == proxy
        return original(transport=httpx.MockTransport(respond), **kwargs)
    monkeypatch.setattr(search.httpx, "AsyncClient", client)

def test_default_and_bounds():
    assert search.WebSearchInput(query="python").max_results == 5
    for count in (0, 11):
        with pytest.raises(ValidationError):
            search.WebSearchInput(query="python", max_results=count)

def test_filters_results():
    page = """<ul><li class="res-list"><h3><a href="/link" data-mdurl="https://python.org">Python</a></h3><span data-result-summary>Docs</span></li>
    <li class="res-list"><h3><a href="https://python.org">Duplicate</a></h3></li>
    <li class="res-list"><h3><a href="javascript:alert(1)">Bad</a></h3></li></ul>"""
    results = search._parse_results(page, "360", 5)
    assert results == [{"title":"Python", "url":"https://python.org", "snippet":"Docs"}]


def test_baidu_and_captcha():
    page = '<div id="content_left"><div class="result c-container" mu="https://python.org"><h3><a href="/link">Python</a></h3><div class="c-abstract">Docs</div></div></div>'
    assert search._parse_results(page, "baidu", 1)[0]["url"] == "https://python.org"
    with pytest.raises(ValueError):
        search._parse_results('<html><title>百度安全验证</title></html>', "baidu", 5)

@pytest.mark.parametrize("mode", ["empty", "failure", "timeout"])
def test_failures(monkeypatch, mode):
    async def fake(*args):
        if mode == "failure": raise RuntimeError("secret")
        if mode == "timeout": raise TimeoutError()
        return []
    monkeypatch.setattr(search, "_search", fake)
    result = asyncio.run(search.web_search.ainvoke({"query":"python"}))
    assert result.startswith("Error:")
    assert "secret" not in result

def test_whitespace_query():
    assert asyncio.run(search.web_search.ainvoke({"query":"  "})).startswith("Error:")


def test_provider_fallback(monkeypatch):
    calls = []
    def respond(request):
        calls.append(request.url.host)
        if request.url.host == "html.duckduckgo.com":
            return httpx.Response(202, text="Please complete this challenge")
        if request.url.host != "www.baidu.com":
            return httpx.Response(503)
        return httpx.Response(200, text='<div id="content_left"><div class="c-container" mu="https://python.org"><h3><a href="/link">Python</a></h3><div class="c-abstract">Docs</div></div></div>')
    mock_client(monkeypatch, respond)
    result = asyncio.run(search.web_search.ainvoke({"query":"python"}))
    assert calls == ["cn.bing.com", "html.duckduckgo.com", "www.bing.com", "www.so.com", "www.baidu.com"]
    assert result["results"][0]["url"] == "https://python.org"


def test_rss_filters_and_decodes():
    page = '''<?xml version="1.0" encoding="UTF-8"?><rss><channel>
    <item><title> 官方 文档 </title><link>https://python.org</link><description><![CDATA[<b>中文</b> &amp; docs]]></description></item>
    <item><title>Duplicate</title><link>https://python.org</link></item>
    <item><title>Bad</title><link>javascript:alert(1)</link></item>
    <item><title>Malformed</title><link>http://[</link></item>
    <item><title>Missing URL</title></item>
    <item><title>More</title><link>https://docs.python.org</link></item>
    </channel></rss>'''.encode()
    assert search._parse_rss(page, 5) == [
        {"title": "官方 文档", "url": "https://python.org", "snippet": "中文 & docs"},
        {"title": "More", "url": "https://docs.python.org", "snippet": ""},
    ]
    assert len(search._parse_rss(page, 1)) == 1
    with pytest.raises(ValueError):
        search._parse_rss(b'<html><title>Verification</title></html>', 5)


def test_rss_does_not_expand_external_entities():
    page = b'''<!DOCTYPE rss [<!ENTITY external SYSTEM "file:///etc/passwd">]>
    <rss><channel><item><title>Python</title><link>https://python.org</link>
    <description>&external;</description></item></channel></rss>'''
    assert search._parse_rss(page, 5)[0]["snippet"] == ""


def test_duckduckgo_redirect_and_snippet():
    assert search._parse_results(DDG, "duckduckgo", 5) == [
        {"title": "Python", "url": "https://python.org", "snippet": "Official docs"}
    ]


def test_rss_success_stops_fallbacks(monkeypatch):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, content=RSS)
    mock_client(monkeypatch, respond)
    result = asyncio.run(search.web_search.ainvoke({"query": "  Python 官方文档  ", "max_results": 1}))
    assert result == {"query": "Python 官方文档", "results": [
        {"title": "Python", "url": "https://python.org", "snippet": "Official & docs"}
    ]}
    assert len(calls) == 1
    assert calls[0].url.params["q"] == "Python 官方文档"
    assert calls[0].url.params["format"] == "rss"


@pytest.mark.parametrize("first_response", [
    b'<html><title>Captcha</title></html>', b'<rss>broken', b'<rss><channel/></rss>',
])
def test_invalid_or_empty_feed_falls_back(monkeypatch, first_response):
    def respond(request):
        if request.url.host == "cn.bing.com":
            return httpx.Response(200, content=first_response)
        assert request.url.host == "html.duckduckgo.com"
        return httpx.Response(200, text=DDG)
    mock_client(monkeypatch, respond)
    result = asyncio.run(search.web_search.ainvoke({"query": "python"}))
    assert result["results"][0]["snippet"] == "Official docs"


def test_slow_provider_leaves_time_for_fallback(monkeypatch):
    cancelled = []
    async def respond(request):
        if request.url.host == "cn.bing.com":
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.append(True)
        return httpx.Response(200, text=DDG)
    monkeypatch.setattr(search.settings, "web_search_provider_timeout_seconds", 0.02)
    mock_client(monkeypatch, respond)
    async def run():
        return await asyncio.wait_for(search.web_search.ainvoke({"query": "python"}), 1)
    result = asyncio.run(run())
    assert cancelled == [True]
    assert result["results"][0]["url"] == "https://python.org"


def test_all_providers_unavailable(monkeypatch):
    mock_client(monkeypatch, lambda request: httpx.Response(429))
    assert asyncio.run(search.web_search.ainvoke({"query": "python"})).startswith("Error:")


def test_tavily_uses_header_and_normalizes_results(monkeypatch):
    import json
    monkeypatch.setattr(search.settings, "tavily_api_key", "test-secret-key")
    def respond(request):
        assert request.url.host == "api.tavily.com"
        assert request.method == "POST"
        assert request.headers["Authorization"] == "Bearer test-secret-key"
        body = json.loads(request.content)
        assert body["max_results"] == 1
        assert body["query"] == "python"
        assert "api_key" not in body
        return httpx.Response(200, json={"results": [
            None, {"url": "https://invalid.test"},
            {"title": "Python", "url": "https://python.org", "content": "API docs"},
            {"title": "More", "url": "https://docs.python.org", "content": "More docs"},
        ]})
    mock_client(monkeypatch, respond)
    result = asyncio.run(search.web_search.ainvoke({"query": "python", "max_results": 1}))
    assert result["results"] == [{"title": "Python", "url": "https://python.org", "snippet": "API docs"}]


@pytest.mark.parametrize("response", [
    httpx.Response(401), httpx.Response(429), httpx.Response(200, text="bad json"),
    httpx.Response(200, json=[]), httpx.Response(200, json={"results": None}),
    httpx.Response(200, json={"results": []}),
])
def test_tavily_failure_falls_back_without_leaking_key(monkeypatch, response, caplog):
    monkeypatch.setattr(search.settings, "tavily_api_key", "test-secret-key")
    calls = []
    def respond(request):
        calls.append(request.url.host)
        if request.url.host == "api.tavily.com":
            return response
        # Credentials must not be forwarded to fallback providers.
        assert "Authorization" not in request.headers
        return httpx.Response(200, content=RSS)
    mock_client(monkeypatch, respond)
    result = asyncio.run(search.web_search.ainvoke({"query": "python"}))
    assert calls == ["api.tavily.com", "cn.bing.com"]
    assert result["results"][0]["url"] == "https://python.org"
    assert "test-secret-key" not in caplog.text


def test_explicit_proxy(monkeypatch):
    proxy = "http://host.docker.internal:7890"
    monkeypatch.setattr(search.settings, "web_search_proxy", proxy)
    mock_client(monkeypatch, lambda request: httpx.Response(200, content=RSS), proxy=proxy)
    assert isinstance(asyncio.run(search.web_search.ainvoke({"query": "python"})), dict)


@pytest.mark.parametrize("tool_timeout,search_timeout,expected", [(120, 20, 20), (2, 30, 2), (0, 30, 30)])
def test_search_respects_total_timeout(monkeypatch, tool_timeout, search_timeout, expected):
    monkeypatch.setattr(search.settings, "mcp_tool_call_timeout_seconds", tool_timeout)
    monkeypatch.setattr(search.settings, "web_search_timeout_seconds", search_timeout)
    async def fake(query, max_results, timeout):
        assert timeout == expected
        return []
    monkeypatch.setattr(search, "_search", fake)
    assert asyncio.run(search.web_search.ainvoke({"query": "python"})).startswith("Error:")
