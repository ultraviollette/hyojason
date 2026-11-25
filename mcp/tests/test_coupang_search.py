"""coupang_search: 쿠팡 파트너스 검색 → pageKey 묶기, 소수 가격, 사용 불가 처리, 저장 금지.

실제 API 는 부르지 않는다. 응답은 2026-11-25 사용자가 시험 스크립트로 본 모양을 흉내 낸 가짜 값이다
(상품 칸: rank, productName, productPrice, isRocket, isFreeShipping, productUrl, keyword).
"""
import asyncio
import io
import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from shopping_tools import coupang_partners, profiles, server

KAKAO_ID = "coupangUser01"
_REAL_SEARCH = coupang_partners.search


def _url(page: str, item: str, vendor: str) -> str:
    return (f"https://link.coupang.com/re/AFFSDP?lptag=AF0000000&subid=&pageKey={page}&itemId={item}"
            f"&vendorItemId={vendor}&traceid=V0-000-0000&requestid=20261125000000&token=31850C%7CMIXED")


A1, A2, A3 = _url("111", "9001", "7001"), _url("111", "9002", "7002"), _url("111", "9003", "7003")
B1 = _url("222", "9101", "7101")
C1, C2 = _url("333", "9201", "7201"), _url("333", "9202", "7202")


def _p(rank, name, price, rocket, free, url):
    return {"rank": rank, "productName": name, "productPrice": price, "isRocket": rocket,
            "isFreeShipping": free, "productUrl": url, "keyword": "록시땅 핸드크림"}


OK = {"rCode": "0", "rMessage": "", "data": {"landingUrl": "https://link.coupang.com/a/landing", "productData": [
    _p(1, "록시땅 시어버터 핸드크림 30ml", 19000.0, False, False, A1),             # 가장 싼 줄, 로켓 아님
    _p(2, "록시땅 시어버터 핸드크림 30ml, 단일 색상, 2개", 21500, True, True, A2),  # 같은 상품(pageKey 111), 로켓
    _p(3, "록시땅 버베나 핸드크림 30ml", "24000.0", True, True, B1),
    _p(4, "록시땅 시어버터 핸드크림 30ml", 23000, True, False, A3),                 # 이름이 완전히 같은 줄
    _p(5, "록시땅 아몬드 핸드크림", 26000, False, True, C1),
    _p(6, "록시땅 아몬드 핸드크림", 26000.0, False, True, C2),                      # 가격 같음
]}}


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    for k in ("COUPANG_ACCESS_KEY", "COUPANG_SECRET_KEY", "COUPANG_PARTNERS_VERIFIED"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(profiles, "CLIENTS_DIR", tmp_path)
    coupang_partners._calls.clear()

    real = httpx.AsyncClient.__init__

    def guarded(self, *a, **kw):  # 어떤 실제 요청도 나가면 실패한다
        if kw.get("transport") is None:
            raise AssertionError("실제 네트워크 요청 시도")
        real(self, *a, **kw)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", guarded)


def _enable(monkeypatch, handler, verified="1"):
    monkeypatch.setenv("COUPANG_ACCESS_KEY", "ak-test-SECRETLIKE")
    monkeypatch.setenv("COUPANG_SECRET_KEY", "sk-test-SECRETLIKE")
    if verified is not None:
        monkeypatch.setenv("COUPANG_PARTNERS_VERIFIED", verified)
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(coupang_partners, "search", lambda kw: _REAL_SEARCH(kw, transport=transport))


def _search(kw="록시땅 핸드크림"):
    return asyncio.run(server.coupang_search(kw))


# ---------- 요청 ----------

def test_request_path_query_and_signature_header(monkeypatch) -> None:
    seen = []
    _enable(monkeypatch, lambda req: seen.append(req) or httpx.Response(200, json=OK))
    assert _search()["status"] == "ok"
    req = seen[0]
    assert req.method == "GET" and req.url.host == "api-gateway.coupang.com"
    assert req.url.path == "/v2/providers/affiliate_open_api/apis/openapi/v1/products/search"
    assert parse_qs(urlsplit(str(req.url)).query) == {"keyword": ["록시땅 핸드크림"], "limit": ["10"]}
    assert req.headers["Authorization"].startswith(
        "CEA algorithm=HmacSHA256, access-key=ak-test-SECRETLIKE, signed-date=")


def test_signature_matches_hmac_of_date_method_path_query() -> None:
    now = datetime(2026, 11, 13, 1, 2, 3, tzinfo=timezone.utc)
    h = coupang_partners.authorization("GET", "/p", "keyword=a&limit=1", "AK", "SK", now=now)
    expected = hmac.new(b"SK", b"261113T010203ZGET/pkeyword=a&limit=1", hashlib.sha256).hexdigest()
    assert h == f"CEA algorithm=HmacSHA256, access-key=AK, signed-date=261113T010203Z, signature={expected}"


# ---------- 묶기·가격 ----------

def test_rows_grouped_by_page_key_cheapest_row_is_representative(monkeypatch) -> None:
    _enable(monkeypatch, lambda req: httpx.Response(200, json=OK))
    r = _search()
    assert r["disclosure"] == coupang_partners.DISCLOSURE == \
        "이 게시물은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."
    assert r["rules"] == server.COUPANG_RULES
    a, b, c = r["products"]                         # 처음 나온 순서: 111, 222, 333
    assert a == {"name": "록시땅 시어버터 핸드크림 30ml", "price_min_krw": 19000, "price_max_krw": 23000,
                 "price_varies": True, "row_count": 3, "is_rocket": False, "is_free_shipping": False, "url": A1,
                 "rocket_option": {"name": "록시땅 시어버터 핸드크림 30ml, 단일 색상, 2개", "price_krw": 21500,
                                   "is_free_shipping": True, "url": A2}}
    assert b == {"name": "록시땅 버베나 핸드크림 30ml", "price_min_krw": 24000, "price_max_krw": 24000,
                 "price_varies": False, "row_count": 1, "is_rocket": True, "is_free_shipping": True, "url": B1}
    assert c["row_count"] == 2 and c["price_varies"] is False and c["url"] == C1   # 값이 같으면 앞 줄
    assert "rocket_option" not in c                                                 # 로켓 줄이 없다


def test_decimal_prices_become_integers(monkeypatch) -> None:
    _enable(monkeypatch, lambda req: httpx.Response(200, json=OK))
    for p in _search()["products"]:
        for v in (p["price_min_krw"], p["price_max_krw"], p.get("rocket_option", {}).get("price_krw", 1)):
            assert type(v) is int
    assert coupang_partners.price_krw(19000.0) == 19000
    assert coupang_partners.price_krw("19000.0") == 19000
    assert coupang_partners.price_krw(None) is None and coupang_partners.price_krw("가격") is None


def test_product_url_is_returned_unchanged(monkeypatch) -> None:
    _enable(monkeypatch, lambda req: httpx.Response(200, json=OK))
    urls = {p["url"] for p in _search()["products"]}
    assert urls == {A1, B1, C1}


# ---------- 사용 불가 / 없음 ----------

@pytest.mark.parametrize("keys,verified", [(False, None), (True, None), (True, "0"), (True, "true")])
def test_unavailable_without_keys_or_switch_and_no_request(monkeypatch, keys, verified) -> None:
    calls = []
    if keys:
        _enable(monkeypatch, lambda req: calls.append(1) or httpx.Response(200, json=OK), verified=verified)
    r = _search()
    assert r == {"status": "unavailable", "products": [], "next_step": server.COUPANG_UNAVAILABLE_NEXT}
    assert calls == []
    assert "쿠팡은 제가 직접 볼 수 없어서 검색어를 드려요" in r["next_step"]   # 옛 줄 그대로
    assert "사용자에게 말하지 않는다" in r["next_step"]


def _raise(exc):
    def handler(req):
        raise exc
    return handler


@pytest.mark.parametrize("handler", [
    lambda req: httpx.Response(401, json={"rCode": "401"}),
    lambda req: httpx.Response(500, text="oops"),
    lambda req: httpx.Response(200, json={"rCode": "1", "rMessage": "bad"}),
    lambda req: httpx.Response(200, text="not json"),
    _raise(httpx.ReadTimeout("slow")),
    _raise(httpx.ConnectError("down")),
    _raise(RuntimeError("anything")),
])
def test_errors_timeouts_and_network_failures_are_unavailable_without_exception(monkeypatch, handler) -> None:
    _enable(monkeypatch, handler)
    assert _search()["status"] == "unavailable"


def test_request_timeout_is_five_seconds() -> None:
    assert coupang_partners.TIMEOUT_S == 5


def test_zero_results_is_empty_not_unavailable(monkeypatch) -> None:
    _enable(monkeypatch, lambda req: httpx.Response(200, json={"rCode": "0", "data": {"productData": []}}))
    r = _search()
    assert r["status"] == "empty" and r["products"] == [] and r["next_step"] == server.COUPANG_EMPTY_NEXT
    assert "쿠팡에서는 못 찾았어요" in r["next_step"] and "disclosure" not in r


def test_per_minute_limit_30(monkeypatch) -> None:
    calls = []
    _enable(monkeypatch, lambda req: calls.append(1) or httpx.Response(200, json=OK))
    for _ in range(30):
        assert _search()["status"] == "ok"
    assert _search()["status"] == "unavailable" and len(calls) == 30


def test_log_has_no_key_signature_or_keyword(monkeypatch) -> None:
    buf = io.StringIO()
    handler = coupang_partners.log.handlers[0]
    old = handler.setStream(buf)                     # stderr 로 가는 그 핸들러가 쓰는 글을 그대로 받는다
    _enable(monkeypatch, lambda req: httpx.Response(401, json={"rCode": "401"}))
    _search("록시땅 핸드크림")
    _enable(monkeypatch, lambda req: httpx.Response(200, json=OK))
    _search("록시땅 핸드크림")
    handler.setStream(old)
    err = buf.getvalue()
    assert "path=coupang_search outcome=unavailable reason=http_401" in err
    assert "path=coupang_search outcome=ok rows=6" in err
    for secret in ("ak-test", "sk-test", "signature", "CEA", "록시땅", "%EB%A1%9D"):
        assert secret not in err


# ---------- place_rules 스위치 ----------

def test_place_rule_switches_with_partners_switch(monkeypatch) -> None:
    r = server.save_client_profile("kakao", KAKAO_ID, payment_places=["쿠팡"])
    assert r["place_rules"] == [server.COUPANG_FIRST_NOTE]                 # 꺼져 있으면 옛 줄
    monkeypatch.setenv("COUPANG_ACCESS_KEY", "a")
    monkeypatch.setenv("COUPANG_SECRET_KEY", "s")
    assert server.resolve_client("kakao", KAKAO_ID)["place_rules"] == [server.COUPANG_FIRST_NOTE]  # 스위치 없음
    monkeypatch.setenv("COUPANG_PARTNERS_VERIFIED", "1")
    assert server.resolve_client("kakao", KAKAO_ID)["place_rules"] == [server.COUPANG_SEARCH_FIRST_NOTE]
    assert "coupang_search" in server.COUPANG_SEARCH_FIRST_NOTE
    other = server.save_client_profile("kakao", "someoneElse", payment_places=["11번가"])
    assert "place_rules" not in other                                     # 쿠팡을 안 쓰는 분은 그대로


# ---------- 저장 금지 ----------

@pytest.mark.parametrize("url", [A1, "https://link.coupang.com/a/bXyZ12", "https://LINK.coupang.com./re/x"])
def test_record_item_refuses_coupang_link_then_retry_without_it_records(url) -> None:
    cid = server.save_client_profile("kakao", KAKAO_ID, display_name="어머니")["client_id"]
    first = server.record_item(cid, "found", "록시땅 시어버터 핸드크림 30ml", brand="록시땅",
                               seen_price_krw=19000, url=url, match="same")
    assert first["recorded"] is False and first["ask_payment"] is False
    assert first["error"] == profiles.COUPANG_RECORD_ERROR
    assert "seller 에는 쿠팡" in first["error"] and "name 에는 사용자가 말한 종류" in first["error"]
    p = profiles.get_profile(cid)
    assert p["items"] == [] and p["payment"]["asked_on"] == ""            # 거절은 아무것도 남기지 않는다

    second = server.record_item(cid, "found", "핸드크림", brand="록시땅", options={"용량": "30ml"},
                                seller="쿠팡", match="same", url="")
    assert second["recorded"] is True and second["ask_payment"] is True     # 결제 질문은 재호출 때 한 번
    assert "ask_payment_note" in second
    p = profiles.get_profile(cid)
    assert p["payment"]["asked_on"] != ""                                  # 재호출 때 채워짐
    it = p["items"][0]
    assert (it["name"], it["brand"], it["options"], it["seller"], it["url"], it["seen_price_krw"]) == \
        ("핸드크림", "록시땅", {"용량": "30ml"}, "쿠팡", "", None)
    third = server.record_item(cid, "found", "핸드크림", brand="록시땅", seller="쿠팡", match="same")
    assert third["ask_payment"] is False                                   # 정확히 한 번
    assert "link.coupang.com" not in (profiles.CLIENTS_DIR / f"{cid}.yaml").read_text(encoding="utf-8")


def test_other_hosts_are_not_coupang_partners_links() -> None:
    assert profiles.is_coupang_partners_url("https://link.coupang.com/re/AFFSDP?x=1")
    for u in ("https://www.coupang.com/vp/products/1", "https://link.coupang.com.evil.test/re/x",
              "https://evil.test/?u=https://link.coupang.com/re/x"):
        assert not profiles.is_coupang_partners_url(u), u
