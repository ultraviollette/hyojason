"""search_products provider 구조. 실제 네트워크 요청은 하지 않는다(쿠팡·네이버 모두 가짜 응답)."""
import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from shopping_tools import coupang_partners, server

PARTNERS_OK = {"rCode": "0", "rMessage": "", "data": {"landingUrl": "https://link.coupang.com/a/landing",
    "productData": [
        {"productId": 111, "productName": "에스쁘아 비벨벳 커버 쿠션 리필", "productPrice": 18900,
         "productImage": "https://img/1.jpg", "productUrl": "https://link.coupang.com/a/abc",
         "isRocket": True, "isFreeShipping": True, "categoryName": "뷰티"}]}}


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


def _with_partners(monkeypatch, handler, verified=True):
    monkeypatch.setenv("COUPANG_ACCESS_KEY", "ak-test")
    monkeypatch.setenv("COUPANG_SECRET_KEY", "sk-test")
    if verified:
        monkeypatch.setenv("COUPANG_PARTNERS_VERIFIED", "1")
    transport = httpx.MockTransport(handler)
    orig = coupang_partners.search
    monkeypatch.setattr(coupang_partners, "search",
                        lambda q, max_results=10: orig(q, max_results=max_results, transport=transport))


def _search(q="에스쁘아 쿠션 리필"):
    return asyncio.run(server.search_products(q))


def test_without_keys_nothing_is_called_and_web_search_is_suggested(no_network) -> None:
    r = _search()
    assert r["count"] == 0 and r["fallback"] == "web_search" and r["tried"] == []
    assert "example.com" not in json.dumps(r)


def test_partners_used_when_keys_exist_and_disclosure_attached(monkeypatch, no_network) -> None:
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=PARTNERS_OK)
    _with_partners(monkeypatch, handler)
    r = _search()
    assert r["provider"] == "coupang_partners" and r["count"] == 1
    item = r["items"][0]
    assert item["url"] == "https://link.coupang.com/a/abc" and item["affiliate"] is True and item["rocket"] is True
    assert r["disclosure"] == coupang_partners.DISCLOSURE
    req = seen[0]
    assert req.url.path == coupang_partners.SEARCH_PATH
    assert parse_qs(urlsplit(str(req.url)).query) == {"keyword": ["에스쁘아 쿠션 리필"], "limit": ["10"]}
    assert req.headers["Authorization"].startswith("CEA algorithm=HmacSHA256, access-key=ak-test, signed-date=")


def test_signature_matches_hmac_of_date_method_path_query() -> None:
    now = datetime(2026, 11, 13, 1, 2, 3, tzinfo=timezone.utc)
    h = coupang_partners.authorization("GET", "/p", "keyword=a&limit=1", "AK", "SK", now=now)
    expected = hmac.new(b"SK", b"261113T010203ZGET/pkeyword=a&limit=1", hashlib.sha256).hexdigest()
    assert h == f"CEA algorithm=HmacSHA256, access-key=AK, signed-date=261113T010203Z, signature={expected}"


@pytest.mark.parametrize("status,body,error", [
    (401, {"rCode": "401"}, "http_401"),
    (200, {"rCode": "1", "rMessage": "bad"}, "api_1"),
    (200, {"rCode": "0", "data": {"productData": []}}, "no_results"),
])
def test_partners_failure_falls_back_to_web_search(monkeypatch, no_network, status, body, error) -> None:
    _with_partners(monkeypatch, lambda req: httpx.Response(status, json=body))
    r = _search()
    assert r["count"] == 0 and r["fallback"] == "web_search"
    assert r["tried"] == [{"provider": "coupang_partners", "error": error}]


def test_hourly_limit_stops_calls(monkeypatch, no_network) -> None:
    calls = []
    _with_partners(monkeypatch, lambda req: calls.append(1) or httpx.Response(200, json=PARTNERS_OK))
    for _ in range(coupang_partners.SEARCH_PER_HOUR):
        assert _search()["provider"] == "coupang_partners"
    r = _search()
    assert len(calls) == coupang_partners.SEARCH_PER_HOUR
    assert r["tried"] == [{"provider": "coupang_partners", "error": "rate_limited"}]


def test_naver_used_when_only_naver_keys(monkeypatch, no_network) -> None:
    monkeypatch.setenv("NAVER_CLIENT_ID", "id")
    monkeypatch.setenv("NAVER_CLIENT_SECRET", "secret")

    async def fake(query, max_results=20, sort="sim"):
        return [{"title": "생활한복", "url": "https://smartstore.naver.com/x/products/1", "source": "naver_shopping"}]
    monkeypatch.setattr(server.naver, "search", fake)
    r = _search("생활한복")
    assert r["provider"] == "naver_shopping" and r["count"] == 1 and "disclosure" not in r


@pytest.mark.parametrize("flag", [None, "", "0", "true"])
def test_keys_without_verified_flag_never_call_partners(monkeypatch, no_network, flag) -> None:
    """미검증 코드: 키가 있어도 COUPANG_PARTNERS_VERIFIED=1 이 아니면 쿠팡에 요청하지 않는다."""
    calls = []
    _with_partners(monkeypatch, lambda req: calls.append(1) or httpx.Response(200, json=PARTNERS_OK), verified=False)
    if flag is not None:
        monkeypatch.setenv("COUPANG_PARTNERS_VERIFIED", flag)
    r = _search()
    assert calls == [] and r["tried"] == [] and r["fallback"] == "web_search"
    # search() 를 직접 불러도 막힌다
    assert asyncio.run(coupang_partners.search("쿠션")) == {"items": [], "error": "unverified"}
    assert calls == []
