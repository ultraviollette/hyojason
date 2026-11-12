"""make_search_links: 모델이 URL 을 직접 조립하지 않도록 인코딩된 검색 링크를 돌려준다."""
from urllib.parse import parse_qs, urlsplit

from shopping_tools.server import make_search_links

NAME = "에스쁘아 비벨벳 커버 쿠션 23.5 페어 올리브"


def _q(url: str, key: str) -> str:
    return parse_qs(urlsplit(url).query)[key][0]


def test_links_round_trip_exact_product_name() -> None:
    r = make_search_links(NAME)
    assert r["coupang"].startswith("https://www.coupang.com/np/search?q=")
    assert r["naver_shopping"].startswith("https://m.search.naver.com/search.naver?where=m_shop&query=")
    assert _q(r["coupang"], "q") == NAME
    assert _q(r["naver_shopping"], "query") == NAME
    assert " " not in r["coupang"] and "에" not in r["coupang"]  # 공백·한글이 인코딩돼 카톡에서 링크가 끊기지 않는다


def test_special_characters_do_not_break_the_query() -> None:
    name = "A&B 샴푸 1L+리필 #2 50/50?"
    r = make_search_links(name)
    assert _q(r["coupang"], "q") == name
    assert _q(r["naver_shopping"], "query") == name
    assert parse_qs(urlsplit(r["naver_shopping"]).query)["where"] == ["m_shop"]  # & 가 파라미터를 쪼개지 않는다


def test_whitespace_is_normalized_and_empty_is_rejected() -> None:
    assert make_search_links("  쿠션\n 23호  ")["query"] == "쿠션 23호"
    assert "error" in make_search_links("   ")
