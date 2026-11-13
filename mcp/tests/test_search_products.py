"""search_products: 실제 결과가 없으면 가짜 상품 대신 빈 결과 + 웹 검색 안내를 돌려준다.
쿠팡에는 요청하지 않는다(스크레이퍼를 가짜로 바꿔 끼움)."""
import asyncio

import pytest

from shopping_tools import coupang_scrape, server


@pytest.fixture(autouse=True)
def no_naver_key(monkeypatch):
    monkeypatch.delenv("NAVER_CLIENT_ID", raising=False)
    monkeypatch.delenv("NAVER_CLIENT_SECRET", raising=False)


def _search(monkeypatch, scrape_result):
    async def fake(query, max_results=10):
        return scrape_result
    monkeypatch.setattr(coupang_scrape, "search_coupang", fake)
    return asyncio.run(server.search_products("밝은색 생활한복"))


def test_no_results_points_to_web_search_not_fake_items(monkeypatch) -> None:
    r = _search(monkeypatch, {"blocked": True, "items": []})
    assert r["count"] == 0 and r["items"] == []
    assert r["fallback"] == "web_search"
    assert "web_search" in r["next_step"] and "링크를 달라고 하지 말고" in r["next_step"]
    assert "example.com" not in str(r)  # 예전 mock 상품이 새지 않는다


def test_real_results_pass_through(monkeypatch) -> None:
    item = {"title": "생활한복", "url": "https://www.coupang.com/vp/products/1", "source": "coupang_scrape"}
    r = _search(monkeypatch, {"blocked": False, "items": [item]})
    assert r["count"] == 1 and r["items"] == [item] and "fallback" not in r
