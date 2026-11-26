"""coupang_search: 쿠팡 파트너스 검색 → pageKey 묶기, 소수 가격, 사용 불가 처리, 저장 금지.

실제 API 는 부르지 않는다. 응답은 2026-11-25 사용자가 시험 스크립트로 본 모양을 흉내 낸 가짜 값이다
(상품 칸: rank, productName, productPrice, isRocket, isFreeShipping, productUrl, keyword).
"""
import asyncio
import io
import json
import logging
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


def _img(url: str) -> str:
    """줄마다 다른 가짜 사진 주소(itemId 로 구분)."""
    item = url.split("itemId=")[1].split("&")[0]
    return f"https://thumbnail.coupangcdn.test/thumbnails/remote/230x230ex/image/retail/images/{item}-ab%2Bcd.jpg?q=1&x=2"


def _p(rank, name, price, rocket, free, url):
    return {"rank": rank, "productName": name, "productPrice": price, "isRocket": rocket,
            "isFreeShipping": free, "productUrl": url, "productImage": _img(url), "keyword": "록시땅 핸드크림"}


def _opt(price, rocket, free, url):
    return {"price_krw": price, "is_rocket": rocket, "is_free_shipping": free, "url": url, "image": _img(url)}


SRP = "https://link.coupang.com/re/AFFSRP?lptag=AF0000000&pageKey=%EB%A1%9D%EC%8B%9C%EB%95%85&traceid=V0-1"
GOLDBOX = "https://link.coupang.com/re/AFFGOLDBOX?lptag=AF0000000&traceid=V0-2"
OK = {"rCode": "0", "rMessage": "", "data": {"landingUrl": SRP, "productData": [
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
                                   "is_free_shipping": True, "url": A2},
                 "options": [_opt(19000, False, False, A1), _opt(21500, True, True, A2), _opt(23000, True, False, A3)]}
    assert b == {"name": "록시땅 버베나 핸드크림 30ml", "price_min_krw": 24000, "price_max_krw": 24000,
                 "price_varies": False, "row_count": 1, "is_rocket": True, "is_free_shipping": True, "url": B1,
                 "options": [_opt(24000, True, True, B1)]}                      # 한 줄이어도 image 가 있다
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
    _enable(monkeypatch, lambda req: httpx.Response(200, json={"rCode": "0", "data": {"landingUrl": SRP,
                                                                                         "productData": []}}))
    r = _search()
    assert r["status"] == "empty" and r["products"] == [] and "disclosure" not in r
    assert r["next_step"] == server.COUPANG_RETRY_NEXT.format(next=2)


# ---------- 채움 결과(검색 결과 없음 → 골드박스 상품) ----------

def _filler(landing):
    return {"rCode": "0", "data": {**({"landingUrl": landing} if landing is not None else {}), "productData": [
        _p(1, "코카콜라 제로 355ml 24캔", 21900, True, True, _url("901", "1", "1")),
        _p(2, "스테인리스 냄비 3종", 39000, False, False, _url("902", "2", "2")),
        _p(3, "노트북 15인치", 899000, True, True, _url("903", "3", "3"))]}}


@pytest.mark.parametrize("landing", [GOLDBOX, None, "", "https://link.coupang.com/re/AFFSDP?x=1",
                                     "https://evil.test/re/AFFSRP"])
def test_filler_results_without_affsrp_landing_are_empty(monkeypatch, landing) -> None:
    _enable(monkeypatch, lambda req: httpx.Response(200, json=_filler(landing)))
    r = _search("메구리즘 유자")
    assert r["status"] == "empty" and r["products"] == [] and "disclosure" not in r


def test_affsrp_landing_is_ok(monkeypatch) -> None:
    _enable(monkeypatch, lambda req: httpx.Response(200, json=_filler(SRP)))
    r = _search("메구리즘 유자향")
    assert r["status"] == "ok" and len(r["products"]) == 3


def test_missing_is_free_shipping_means_not_free(monkeypatch) -> None:
    row = _p(1, "멕리듬 온열 순환 스팀 아이마스크 12매 7종 택1 (무향/라벤더/캐모마일/유자/숲속/히노키/금목서)",
             15900.0, False, False, B1)
    del row["isFreeShipping"]
    rocket = _p(2, "멕리듬 온열 순환 스팀 아이마스크 12매 7종 택1 (무향/라벤더/캐모마일/유자/숲속/히노키/금목서)",
                16900, True, False, _url("222", "9102", "7102"))
    del rocket["isFreeShipping"]
    _enable(monkeypatch, lambda req: httpx.Response(200, json={"rCode": "0", "data": {"landingUrl": SRP,
                                                                                         "productData": [row, rocket]}}))
    (p,) = _search("메구리즘 유자향")["products"]
    assert p["is_free_shipping"] is False and p["rocket_option"]["is_free_shipping"] is False


# ---------- 다시 찾기(한 물건에 3번) ----------

def test_no_match_next_steps_retry_twice_then_stop(monkeypatch) -> None:
    _enable(monkeypatch, lambda req: httpx.Response(200, json=_filler(GOLDBOX)))
    r1 = asyncio.run(server.coupang_search("메구리즘 유자향", attempt=1))
    r2 = asyncio.run(server.coupang_search("스팀 아이마스크 유자향", attempt=2))
    r3 = asyncio.run(server.coupang_search("메구리즘", attempt=3))
    assert "attempt=2" in r1["next_step"] and "attempt=3" in r2["next_step"]
    for r in (r1, r2):
        assert "웹 검색으로 가지 않고" in r["next_step"] and "'유자향'을 '유자'로 줄이지 않는다" in r["next_step"]
    assert r3["next_step"] == server.COUPANG_STOP_NEXT
    assert "'쿠팡에서는 제가 찾는 방법으로는 못 찾았어요'로 시작" in r3["next_step"]


def test_ok_carries_next_if_no_match_for_filtering_everything_out(monkeypatch) -> None:
    _enable(monkeypatch, lambda req: httpx.Response(200, json=OK))
    assert "attempt=2" in asyncio.run(server.coupang_search("록시땅 핸드크림", attempt=1))["next_if_no_match"]
    assert asyncio.run(server.coupang_search("록시땅", attempt=3))["next_if_no_match"] == server.COUPANG_STOP_NEXT


def test_fourth_call_is_not_requested(monkeypatch) -> None:
    calls = []
    _enable(monkeypatch, lambda req: calls.append(1) or httpx.Response(200, json=OK))
    r = asyncio.run(server.coupang_search("메구리즘", attempt=4))
    assert calls == [] and r["status"] == "empty" and r["next_step"] == server.COUPANG_STOP_NEXT


def test_rules_text_for_brand_spelling_and_listed_options() -> None:
    rules = " ".join(server.COUPANG_RULES)
    assert "name 에 그 브랜드가 없는 상품은 버린다" not in rules
    assert "다른 표기(음차, 한국 판매명, 영문)" in rules and "쿠팡에서는 '멕리듬'이라는 이름으로 팔아요" in rules
    assert "같은 브랜드인지 확인해 주세요" in rules
    assert "유자는 들어가서 골라 주세요" not in rules                       # 11/25 2차 시험: 그 페이지에서 못 고른다
    # 11/26: 사진 4장 비전 확인에 2분 넘게 걸려 카카오 시간 초과. 규칙에서 사진 확인을 뺐다
    assert "비전" not in rules and "최대 5장" not in rules and "image 를 하나씩" not in rules
    assert "사진(image)은 보지 않는다" in rules
    assert "그 상품은 후보에서 빼고 '들어가서 골라 주세요'라고 하지 않는다" in rules
    assert "'이 링크가 어느 향(옵션)인지는 들어가서 확인해 주세요'" in rules
    assert "name 에 여러 값이 나열돼 있으면 그 상품은 후보에서 뺀다" in rules
    assert "name 에 아예 없을 때만 '용량은 들어가서 골라 주세요'" in rules


def test_order_rules_keep_answers_fast() -> None:
    rules = " ".join(server.COUPANG_RULES)
    assert "사이에 다른 도구를 부르지 않고 이어서 부른다" in rules
    assert "coupang_search_link 를 먼저 부르고 그다음 웹 검색" in rules
    assert "사이에 다른 도구를 부르지 않고" in server.COUPANG_RETRY_NEXT
    stop = server.COUPANG_STOP_NEXT
    assert stop.index("coupang_search_link") < stop.index("웹 검색")
    assert "① 브랜드 + 사용자가 말한 특징을 말 그대로" in server.COUPANG_SEARCH_FIRST_NOTE


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
    err = buf.getvalue()
    assert "path=coupang_search outcome=unavailable reason=http_401" in err
    assert "path=coupang_search outcome=ok rows=6" in err
    _enable(monkeypatch, lambda req: httpx.Response(200, json=_filler(GOLDBOX)))
    _search("록시땅 핸드크림")
    handler.setStream(old)
    err = buf.getvalue()
    assert "path=coupang_search outcome=filler rows=3" in err
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
    err = first["error"]
    assert "seller='쿠팡'" in err and "name 은 사용자가 말한 말 그대로" in err and "후보마다 기록하지 않는다" in err
    assert "한 번만 다시 기록하고, 그 뒤에 사용자에게 보낼 답을 쓴다" in err
    assert "해요" not in err                                              # 사용자에게 하는 말처럼 보이지 않게
    assert first["answer_note"] == server.ANSWER_NOTE
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


def test_http_library_request_log_does_not_carry_keyword(monkeypatch, caplog) -> None:
    """11/25 mcp-stderr.log: httpx 가 INFO 로 요청 주소(검색어 포함)를 남겼다. 서버는 httpx·httpcore 를 WARNING 이상만 남긴다."""
    _enable(monkeypatch, lambda req: httpx.Response(200, json=OK))
    with caplog.at_level(logging.DEBUG):
        _search("메구리즘 유자향")
    assert not [r for r in caplog.records if r.name in ("httpx", "httpcore")]
    for word in ("메구리즘", "%EB%A9%94", "keyword="):
        assert word not in caplog.text
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


# 11/25 22:58 실사용 응답을 흉내 낸 것: 이름이 같은 4줄, pageKey 같음, itemId 만 다름, 줄마다 다른 향 페이지·사진
MEG = "멕리듬 온열 순환 스팀 아이마스크 12매 7종 택1 (무향/라벤더/캐모마일/유자/숲속/히노키/금목서)"
MEG_URLS = [_url("9834261421", f"2400000{n}", f"8800000{n}") for n in ("030", "031", "032", "033")]


def test_options_keep_every_row_url_and_image_unchanged(monkeypatch) -> None:
    rows = [_p(i + 1, MEG, price, False, False, u) for i, (price, u) in
            enumerate(zip((29500, 29500, 29500, 27800), MEG_URLS))]
    del rows[1]["isFreeShipping"]
    del rows[2]["productImage"]                                             # 사진이 없는 줄
    sent = json.loads(json.dumps(rows))
    _enable(monkeypatch, lambda req: httpx.Response(200, json={"rCode": "0", "data": {"landingUrl": SRP,
                                                                                         "productData": rows}}))
    (p,) = _search("메구리즘 유자향")["products"]
    assert p["row_count"] == 4 and p["url"] == MEG_URLS[3] and p["price_min_krw"] == 27800   # 대표 값은 그대로
    assert [o["url"] for o in p["options"]] == [r["productUrl"] for r in sent]               # 처음 나온 순서, 글자 그대로
    assert [o["image"] for o in p["options"]] == [_img(MEG_URLS[0]), _img(MEG_URLS[1]), "", _img(MEG_URLS[3])]
    assert [o["price_krw"] for o in p["options"]] == [29500, 29500, 29500, 27800]
    assert p["options"][1]["is_free_shipping"] is False


# ---------- 쿠팡 검색 버튼(coupang_search_link, deeplink) ----------
# 11/25 사용자 실제 확인: 검색 화면 주소 → data[0].shortenUrl "https://link.coupang.com/a/…", landingUrl AFFSRP.
# 상품 주소(7088615161)는 HTTP 200 rCode 400 "url convert failed".
SHORT = "https://link.coupang.com/a/bZx9Qk"
_REAL_LINK = coupang_partners.search_link


def _enable_link(monkeypatch, handler, verified="1"):
    monkeypatch.setenv("COUPANG_ACCESS_KEY", "ak-test-SECRETLIKE")
    monkeypatch.setenv("COUPANG_SECRET_KEY", "sk-test-SECRETLIKE")
    if verified is not None:
        monkeypatch.setenv("COUPANG_PARTNERS_VERIFIED", verified)
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(coupang_partners, "search_link", lambda kw: _REAL_LINK(kw, transport=transport))


def _deeplink_ok(req):
    url = json.loads(req.content)["coupangUrls"][0]
    return httpx.Response(200, json={"rCode": "0", "rMessage": "", "data": [
        {"originalUrl": url, "shortenUrl": SHORT,
         "landingUrl": "https://link.coupang.com/re/AFFSRP?lptag=AF0000000&pageKey=%EB%A9%94%EA%B5%AC%EB%A6%AC%EC%A6%98"}]})


def _link(kw="메구리즘 유자향"):
    return asyncio.run(server.coupang_search_link(kw))


def test_search_link_posts_raw_search_page_url_and_returns_shorten_url_unchanged(monkeypatch) -> None:
    seen = []
    _enable_link(monkeypatch, lambda req: seen.append(req) or _deeplink_ok(req))
    r = _link("  메구리즘   유자향 ")
    assert r == {"status": "ok", "url": SHORT, "disclosure": coupang_partners.DISCLOSURE,
                 "rules": server.COUPANG_LINK_RULES}
    req = seen[0]
    assert req.method == "POST" and req.url.host == "api-gateway.coupang.com"
    assert req.url.path == "/v2/providers/affiliate_open_api/apis/openapi/v1/deeplink" and req.url.query == b""
    # 인코딩하지 않고 한글·공백 그대로(사용자 실제 확인, 11/26). 연속 공백만 하나로
    assert json.loads(req.content) == {"coupangUrls": ["https://www.coupang.com/np/search?q=메구리즘 유자향"]}
    assert req.headers["Authorization"].startswith("CEA algorithm=HmacSHA256, access-key=ak-test-SECRETLIKE, ")


def test_search_link_signature_is_post_with_empty_query(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(coupang_partners, "authorization",
                        lambda m, p, q, a, s, now=None: seen.append((m, p, q)) or "CEA x")
    _enable_link(monkeypatch, _deeplink_ok)
    assert _link()["status"] == "ok"
    assert seen == [("POST", coupang_partners.DEEPLINK_PATH, "")]


@pytest.mark.parametrize("handler", [
    lambda req: httpx.Response(200, json={"rCode": "400", "rMessage": "url convert failed"}),
    lambda req: httpx.Response(401, json={"rCode": "401"}),
    lambda req: httpx.Response(200, text="not json"),
    lambda req: httpx.Response(200, json={"rCode": "0", "data": []}),
    lambda req: httpx.Response(200, json={"rCode": "0", "data": [{"shortenUrl": "https://evil.test/a/x"}]}),
    lambda req: httpx.Response(200, json={"rCode": "0", "data": [{"originalUrl": "x"}]}),
    _raise(httpx.ReadTimeout("slow")),
    _raise(httpx.ConnectError("down")),
])
def test_search_link_failures_are_unavailable(monkeypatch, handler) -> None:
    _enable_link(monkeypatch, handler)
    assert _link() == {"status": "unavailable", "next_step": server.COUPANG_LINK_UNAVAILABLE_NEXT}


@pytest.mark.parametrize("keys,verified", [(False, None), (True, None), (True, "0")])
def test_search_link_without_keys_or_switch_makes_no_request(monkeypatch, keys, verified) -> None:
    calls = []
    if keys:
        _enable_link(monkeypatch, lambda req: calls.append(1) or _deeplink_ok(req), verified=verified)
    assert _link()["status"] == "unavailable" and calls == []


def test_search_link_shares_per_minute_limit_with_search(monkeypatch) -> None:
    _enable(monkeypatch, lambda req: httpx.Response(200, json=OK))
    _enable_link(monkeypatch, _deeplink_ok)
    for _ in range(29):
        assert _search()["status"] == "ok"
    assert _link()["status"] == "ok"                      # 30번째
    assert _link()["status"] == "unavailable" and _search()["status"] == "unavailable"


def test_search_link_log_has_only_outcome(monkeypatch) -> None:
    buf = io.StringIO()
    handler = coupang_partners.log.handlers[0]
    old = handler.setStream(buf)
    _enable_link(monkeypatch, _deeplink_ok)
    _link("메구리즘 유자향")
    _enable_link(monkeypatch, lambda req: httpx.Response(200, json={"rCode": "400", "rMessage": "url convert failed"}))
    _link("메구리즘 유자향")
    handler.setStream(old)
    lines = buf.getvalue().strip().splitlines()
    assert lines == ["path=coupang_search_link outcome=ok",
                     "path=coupang_search_link outcome=unavailable reason=api_400"]


def test_link_rules_and_stop_next_text() -> None:
    link_rules = " ".join(server.COUPANG_LINK_RULES)
    assert "쿠팡에서는 딱 맞는 걸 제가 짚지 못했어요. 마지막 버튼을 누르면 쿠팡 검색 화면이 열려요." in link_rules
    assert "웹 후보는 최대 2개" in link_rules and "가격·상품명을 붙이지 않는다" in link_rules
    assert "coupang_search_link" in server.COUPANG_STOP_NEXT
    rules = " ".join(server.COUPANG_RULES)
    assert "대략 1.5배 이상" in rules and "Amazon, 직구, 해외, 구매대행, 일본 한정" in rules
    assert "쿠팡 상품 후보를 하나도 내지 못한 답" in rules


# ---------- 답 쓰는 순서(answer_note), record_item 로그 ----------

def test_record_item_always_carries_answer_note_and_rules_text() -> None:
    cid = server.save_client_profile("kakao", KAKAO_ID, display_name="어머니")["client_id"]
    ok = server.record_item(cid, "found", "쌀 소면", seller="쿠팡")
    bad = server.record_item(cid, "found", "")                              # name 이 비어 거절
    for r in (ok, bad):
        assert r["answer_note"] == server.ANSWER_NOTE
    assert server.ANSWER_NOTE == ("기록은 사용자에게 보낼 답을 쓰기 전에 끝낸다. 도구 호출을 모두 끝낸 뒤 마지막에 쓰는 글 하나만 "
                                  "사용자에게 전달된다. 그 글에 후보와 링크를 전부 담는다. 도구 호출 뒤에 요약·보고·사과를 "
                                  "쓰지 않는다. 한국어 해요체로만.")
    rules = " ".join(server.COUPANG_RULES)
    assert server.ANSWER_NOTE in server.COUPANG_RULES
    assert "쿠팡 후보를 드린 답은 물건 하나에 record_item 한 번" in rules and "후보마다 기록하지 않는다" in rules
    assert "먼저" not in server.ASK_PAYMENT_NOTE


def test_record_item_log_line_has_outcome_only() -> None:
    buf = io.StringIO()
    handler = server._record_log.handlers[0]
    old = handler.setStream(buf)
    cid = server.save_client_profile("kakao", KAKAO_ID, display_name="어머니")["client_id"]
    server.record_item(cid, "found", "쌀 소면", seller="쿠팡")
    server.record_item(cid, "found", "쌀 소면", seller="쿠팡")                # 같은 날 같은 물건 → 합침
    server.record_item(cid, "found", "샘표 쌀 소면", url=A1)                  # 쿠팡 주소 거절
    server.record_item(cid, "maybe", "쌀 소면")                              # 다른 이유로 거절
    server.record_item(cid, "chosen", "쌀 소면", seller="쿠팡")               # found → chosen 전환도 합침
    handler.setStream(old)
    assert buf.getvalue().splitlines() == [
        "path=record_item outcome=recorded status=found",
        "path=record_item outcome=merged status=found",
        "path=record_item outcome=rejected_coupang_url status=found",
        "path=record_item outcome=rejected status=?",
        "path=record_item outcome=merged status=chosen",
    ]
    for leak in ("쌀", "샘표", "link.coupang", cid, KAKAO_ID):
        assert leak not in buf.getvalue()
