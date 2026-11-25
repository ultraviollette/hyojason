"""search_products provider 구조. 실제 네트워크 요청은 하지 않는다(가짜 응답)."""
import asyncio
import json

import httpx
import pytest

from shopping_tools import coupang_partners, server


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET", "COUPANG_ACCESS_KEY", "COUPANG_SECRET_KEY",
              "COUPANG_PARTNERS_VERIFIED"):
        monkeypatch.delenv(k, raising=False)
    coupang_partners._calls.clear()


@pytest.fixture
def no_network(monkeypatch):
    """어떤 실제 요청도 나가면 실패한다."""
    real = httpx.AsyncClient.__init__

    def guarded(self, *a, **kw):
        if kw.get("transport") is None:
            raise AssertionError("실제 네트워크 요청 시도")
        real(self, *a, **kw)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", guarded)


def _search(q="에스쁘아 쿠션 리필"):
    return asyncio.run(server.search_products(q))


def test_without_keys_nothing_is_called_and_web_search_is_suggested(no_network) -> None:
    r = _search()
    assert r["count"] == 0 and r["fallback"] == "web_search" and r["tried"] == []
    assert "example.com" not in json.dumps(r)


def test_naver_used_when_only_naver_keys(monkeypatch, no_network) -> None:
    monkeypatch.setenv("NAVER_CLIENT_ID", "id")
    monkeypatch.setenv("NAVER_CLIENT_SECRET", "secret")

    async def fake(query, max_results=20, sort="sim"):
        return [{"title": "생활한복", "url": "https://smartstore.naver.com/x/products/1", "source": "naver_shopping"}]
    monkeypatch.setattr(server.naver, "search", fake)
    r = _search("생활한복")
    assert r["provider"] == "naver_shopping" and r["count"] == 1 and "disclosure" not in r


def test_coupang_keys_do_not_make_search_products_call_coupang(monkeypatch, no_network) -> None:
    """쿠팡은 search_products 에서 뺐다(쿠팡을 안 쓰는 분에게 쿠팡 결과가 나가지 않게). coupang_search 로만 찾는다."""
    monkeypatch.setenv("COUPANG_ACCESS_KEY", "ak-test")
    monkeypatch.setenv("COUPANG_SECRET_KEY", "sk-test")
    monkeypatch.setenv("COUPANG_PARTNERS_VERIFIED", "1")
    r = _search()
    assert r["tried"] == [] and r["fallback"] == "web_search"
    assert [name for name, _, _ in server.SEARCH_PROVIDERS] == ["naver_shopping"]
