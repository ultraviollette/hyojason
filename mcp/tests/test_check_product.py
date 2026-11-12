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
