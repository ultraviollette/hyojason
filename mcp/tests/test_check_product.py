"""check_product: 페이지를 못 읽었을 때 '품절'로 판정하지 않는다.

실제 사례: 스마트스토어가 429 오류 페이지("현재 서비스 접속이 불가합니다")를 돌려줬고,
Jev 가 그 본문을 보고 in_stock=no 로 판정해 '품절이에요'라고 답했다.
"""
import asyncio
import functools

import httpx
import pytest

from shopping_tools import gates, server

SMARTSTORE_429 = ("<html><head><title>[에러] 에러페이지 - 시스템오류</title></head><body>"
                  "주문확인/배송조회 쿠폰 상품 찜 장바구니 현재 서비스 접속이 불가합니다. "
                  "동시에 접속하는 이용자 수가 많거나 인터넷 네트워크 상태가 불안정하여 접속이 불가합니다.</body></html>")
FILLER = "소재 면 95% 스판 5%. 세탁은 손빨래를 권장합니다. " * 20
_ORIGINAL_FETCH = server._fetch_page


@pytest.fixture
def use_page(monkeypatch):
    """check_product 가 네트워크 대신 주어진 상태 코드·HTML 을 받게 한다 (_fetch_page 판정 로직은 그대로 탄다)."""
    def _use(status: int, html: str) -> None:
        transport = httpx.MockTransport(lambda req: httpx.Response(status, text=html))
        monkeypatch.setattr(server, "_fetch_page", functools.partial(_ORIGINAL_FETCH, transport=transport))
    return _use


@pytest.fixture
def jev_says_sold_out(monkeypatch):
    """Jev 가 오류 페이지를 보고 in_stock=no 라고 답한 상황을 흉내 낸다."""
    def fake(evidence, *, deadline=None, forbidden=None):
        return {"in_stock": "no", "domestic_shipping": "yes", "arrives_by_deadline": "yes",
                "violates_profile": "no"}, "jev"
    monkeypatch.setattr(gates, "decide_all", fake)


def _check(**kw):
    return asyncio.run(server.check_product("https://naver.me/xxxx", **kw))


def test_blocked_page_is_unreadable_not_sold_out(use_page, jev_says_sold_out):
    use_page(429, SMARTSTORE_429)
    r = _check(title="여성 보디수트")
    assert r["readable"] is False
    assert r["in_stock"] == "unknown"
    assert r["passed"] is False
    assert "페이지를 읽지 못함" in r["note"]
    assert "품절" not in r["note"]
    assert r["http_status"] == 429
    assert r["evidence_chars"] == 0  # 오류 페이지 본문은 근거로 쓰지 않는다


def test_block_page_with_200_is_unreadable(use_page):
    use_page(200, "<title>G마켓</title>원활한 서비스 이용을 위한 간단한 확인 안내 봇 확인 절차가 진행되고 있습니다.")
    r = _check()
    assert r["readable"] is False and r["unreadable_reason"] == "봇 차단·오류 페이지"


def test_empty_js_page_is_unreadable(use_page):
    use_page(200, "<html><title>11번가</title><body><div id='root'></div></body></html>")
    r = _check()
    assert r["readable"] is False and "JS 렌더링" in r["unreadable_reason"]
    assert r["in_stock"] == "unknown" and r["passed"] is False


def test_readable_page_without_stock_info_is_unknown(use_page):
    use_page(200, f"<body>여성 보디수트 {FILLER}</body>")
    r = _check()
    assert r["readable"] is False and r["unreadable_reason"] == "재고 표시를 찾지 못함"
    assert r["in_stock"] == "unknown" and r["passed"] is False


def test_readable_page_with_buy_button_passes(use_page):
    use_page(200, f"<body>여성 보디수트 18,900원 옵션 선택 {FILLER} 구매하기 장바구니 담기</body>")
    r = _check()
    assert r["readable"] is True and r["unreadable_reason"] is None
    assert r["in_stock"] == "yes" and r["passed"] is True


def test_readable_sold_out_page_is_sold_out(use_page):
    use_page(200, f"<body>여성 보디수트 {FILLER} 일시품절 재입고 알림</body>")
    r = _check()
    assert r["readable"] is True
    assert r["in_stock"] == "no" and r["passed"] is False and "품절" in r["note"]


def test_kids_item_is_flagged_from_title(use_page):
    use_page(200, f"<title>주니어 발레 레오타드 : 댄스몰</title><body>{FILLER} 구매하기</body>")
    r = _check()
    assert r["kids_item"] is True and "아동용 상품이에요" in r["note"]
    assert r["passed"] is True  # 알려 주기만 하고 탈락시키지 않는다


def test_kids_word_only_in_body_is_not_flagged(use_page):
    use_page(200, f"<title>여성 보디수트</title><body>{FILLER} 구매하기 함께 본 상품: 키즈 내복</body>")
    assert _check()["kids_item"] is False


# ---------- check_candidates: 웹 검색 후보를 내기 전에 페이지를 읽는다 ----------
# f3f5b0c 실사용: "록시땅 버베나 핸드크림"에 롯데온(페이지에 "품절된 상품입니다")과
# LF몰("해외직구"·"구매대행", 배송비 5,500원)을 언급 없이 후보로 냈다. 3-b 는 페이지를 읽지 않았다.

def _pages(monkeypatch, by_host: dict[str, tuple[int, str]], seen: list[str] | None = None):
    def handler(req):
        if seen is not None:
            seen.append(req.url.host)
        status, html = by_host[req.url.host]
        return httpx.Response(status, text=html)
    monkeypatch.setattr(server, "_fetch_page",
                        functools.partial(_ORIGINAL_FETCH, transport=httpx.MockTransport(handler)))


SOLD_OUT = f"<body>록시땅 버베나 핸드크림 30ml {FILLER} 품절된 상품입니다</body>"
OVERSEAS = f"<body>[해외직구] 록시땅 버베나 핸드크림 구매대행 {FILLER} 배송비 5,500원 구매하기</body>"
DOMESTIC = f"<body>록시땅 버베나 핸드크림 23,000원 {FILLER} 배송비 3,000원 바로구매</body>"
FREE = f"<body>록시땅 버베나 핸드크림 {FILLER} 무료배송 구매하기</body>"


def _cands(*hosts):
    return [{"url": f"https://{h}/p/1", "title": "록시땅 버베나 핸드크림", "mall": h} for h in hosts]


def _run(cands, **kw):
    return asyncio.run(server.check_candidates(cands, **kw))


def test_sold_out_is_dropped_and_overseas_hidden_when_domestic_exists(monkeypatch):
    _pages(monkeypatch, {"a.test": (200, SOLD_OUT), "b.test": (200, OVERSEAS), "c.test": (200, DOMESTIC)})
    r = {x["url"].split("/")[2]: x for x in _run(_cands("a.test", "b.test", "c.test"))["results"]}
    assert r["a.test"]["verdict"] == "drop_sold_out" and r["a.test"]["show"] is False
    assert r["b.test"]["verdict"] == "overseas" and r["b.test"]["show"] is False   # 국내 후보가 있으면 빼기
    assert r["c.test"]["verdict"] == "ok" and r["c.test"]["show"] is True
    assert r["c.test"]["say"] == "배송비 3,000원 따로" and r["c.test"]["shipping_fee_krw"] == 3000


def test_overseas_only_candidate_is_shown_with_label_and_fee(monkeypatch):
    _pages(monkeypatch, {"a.test": (200, SOLD_OUT), "b.test": (200, OVERSEAS)})
    out = _run(_cands("a.test", "b.test"))
    b = out["results"][1]
    assert b["show"] is True and b["verdict"] == "overseas"
    assert b["say"] == f"{server.OVERSEAS_SAY} · 배송비 5,500원 따로"


def test_proxy_purchase_alone_counts_as_overseas(monkeypatch):
    _pages(monkeypatch, {"a.test": (200, f"<body>핸드크림 구매대행 상품 {FILLER} 구매하기</body>")})
    assert _run(_cands("a.test"))["results"][0]["domestic_shipping"] == "no"


def test_overseas_dropped_when_deadline(monkeypatch):
    _pages(monkeypatch, {"b.test": (200, OVERSEAS)})
    out = _run(_cands("b.test"), deadline="내일")
    assert out["results"][0]["verdict"] == "drop_deadline" and out["results"][0]["show"] is False
    assert "next_step" in out


def test_unreadable_candidate_is_kept_and_says_unverified(monkeypatch):
    _pages(monkeypatch, {"a.test": (429, SMARTSTORE_429), "c.test": (200, FREE)})
    r = _run(_cands("a.test", "c.test"))["results"]
    assert r[0]["show"] is True and r[0]["verdict"] == "unverified"
    assert r[0]["say"] == "재고와 배송비는 확인하지 못했어요"
    assert r[1]["say"] == "무료배송" and r[1]["shipping_fee_krw"] == 0


def test_conditional_free_shipping_and_multiple_fees(monkeypatch):
    _pages(monkeypatch, {
        "a.test": (200, f"<body>핸드크림 {FILLER} 배송비 3,000원 5만원 이상 무료배송 구매하기</body>"),
        "b.test": (200, f"<body>핸드크림 {FILLER} 배송비 3,000원 제주 배송비 6,000원 구매하기</body>")})
    a, b = _run(_cands("a.test", "b.test"))["results"]
    assert a["say"] == "배송비 3,000원 따로 (조건에 따라 무료일 수 있어요)"
    assert b["shipping_fee_krw"] is None and b["say"] == "배송비는 확인하지 못했어요"


def test_coupang_and_crowdfunding_are_not_requested_and_max_three(monkeypatch):
    seen: list[str] = []
    _pages(monkeypatch, {h: (200, DOMESTIC) for h in ("a.test", "b.test", "c.test", "d.test")}, seen)
    out = _run(_cands("www.coupang.com", "wadiz.kr", "a.test", "b.test"))
    assert [x["verdict"] for x in out["results"]] == ["drop_not_candidate", "drop_not_candidate", "ok"]
    assert out["not_checked"] == 1
    assert seen == ["a.test"]                     # 쿠팡·와디즈에는 요청하지 않고, 3개 넘는 후보도 읽지 않는다


def test_slow_page_becomes_unverified_within_budget(monkeypatch):
    async def slow(url, *a, **kw):
        await asyncio.sleep(1)
    monkeypatch.setattr(server, "_fetch_page", slow)
    monkeypatch.setattr(server, "CHECK_BUDGET_S", 0.05)
    r = _run(_cands("a.test"))["results"][0]
    assert r["show"] is True and r["verdict"] == "unverified" and r["unreadable_reason"] == "시간 안에 못 읽음"


def test_drop_reason_line_for_sold_out(monkeypatch):
    _pages(monkeypatch, {"a.test": (200, SOLD_OUT), "c.test": (200, DOMESTIC)})
    a, c = _run(_cands("a.test", "c.test"))["results"]
    assert a["show"] is False and a["drop_reason"] == "품절이에요"     # 기존 규칙: 탈락한 것도 이유 한 줄
    assert "drop_reason" not in c


def test_second_attempt_without_candidates_says_stop(monkeypatch):
    _pages(monkeypatch, {"a.test": (200, SOLD_OUT)})
    first = _run(_cands("a.test"))
    second = _run(_cands("a.test"), attempt=2)
    assert first["next_step"] == server.NO_CANDIDATE_RETRY and "attempt=2" in first["next_step"]
    assert second["next_step"] == server.NO_CANDIDATE_STOP and "제가 찾는 방법으로는 못 찾았어요" in second["next_step"]


def test_log_line_has_counts_only(monkeypatch, tmp_path):
    from shopping_tools import profiles
    monkeypatch.setattr(profiles, "CLIENTS_DIR", tmp_path)
    cid = server.save_client_profile("kakao", "u-secret-1", payment_places=["네이버", "쿠팡"])["client_id"]
    lines: list[str] = []
    monkeypatch.setattr(server._log, "info", lines.append)
    _pages(monkeypatch, {"smartstore.naver.com": (429, SMARTSTORE_429), "a.test": (200, SOLD_OUT),
                         "b.test": (200, OVERSEAS)})
    cands = _cands("smartstore.naver.com", "a.test", "b.test")
    _run(cands, client_id=cid)
    assert lines == ["path=check_candidates attempt=1 n=3 naver_rule=1 naver_hosts=1 shown=1 "
                     "ok=0 unreadable=1 overseas=1 soldout=1 avoid=0 deadline=0 skipped=0"]
    for secret in ("u-secret-1", cid, "록시땅", "a.test", "http"):
        assert secret not in lines[0]                                     # 주소·ID·상품명은 남기지 않는다
    lines.clear()
    _run(cands)
    assert "naver_rule=na" in lines[0]                                    # client_id 를 안 주면 모름


# 8316893 실사용: "배송비 5,500원 따로" 바로 아래에 "재고와 배송비는 확인하지 못했어요"가 붙었다.
def test_fee_seen_in_search_and_unread_page_say_one_consistent_line(monkeypatch):
    _pages(monkeypatch, {"a.test": (429, SMARTSTORE_429)})
    c = _cands("a.test")
    c[0]["shipping_fee_krw"] = 5500                       # 에이전트가 숫자로 넘겨도 받는다
    r = _run(c)["results"][0]
    assert r["say"] == "배송비 5,500원 따로 · 재고는 확인하지 못했어요"
    assert "배송비는 확인하지 못했" not in r["say"] and "재고와 배송비" not in r["say"]
    assert r["shipping_fee_krw"] == 5500 and r["verdict"] == "unverified"


def test_fee_on_page_wins_over_search_and_timeout_keeps_seen_fee(monkeypatch):
    _pages(monkeypatch, {"c.test": (200, DOMESTIC)})
    c = _cands("c.test")
    c[0]["shipping_fee_krw"] = "2,500원"
    assert _run(c)["results"][0]["say"] == "배송비 3,000원 따로"     # 페이지에서 읽은 값이 먼저

    async def slow(url, *a, **kw):
        await asyncio.sleep(1)
    monkeypatch.setattr(server, "_fetch_page", slow)
    monkeypatch.setattr(server, "CHECK_BUDGET_S", 0.05)
    r = _run(c)["results"][0]
    assert r["say"] == "배송비 2,500원 따로 · 재고는 확인하지 못했어요"


def test_budget_is_8_seconds():
    assert server.CHECK_BUDGET_S == 8
