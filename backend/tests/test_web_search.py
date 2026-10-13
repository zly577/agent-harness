import asyncio
import importlib
import pytest
from pydantic import ValidationError
search = importlib.import_module("app.agent.web_search")

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
    import httpx
    calls = []
    def respond(request):
        calls.append(request.url.host)
        if request.url.host == "www.so.com":
            return httpx.Response(503)
        return httpx.Response(200, text='<div id="content_left"><div class="c-container" mu="https://python.org"><h3><a href="/link">Python</a></h3><div class="c-abstract">Docs</div></div></div>')
    original = httpx.AsyncClient
    def client(**kwargs):
        assert kwargs["trust_env"] is False
        return original(transport=httpx.MockTransport(respond), **kwargs)
    monkeypatch.setattr(search.httpx, "AsyncClient", client)
    result = asyncio.run(search.web_search.ainvoke({"query":"python"}))
    assert calls == ["www.so.com", "www.baidu.com"]
    assert result["results"][0]["url"] == "https://python.org"
