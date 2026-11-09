"""네이버 쇼핑 검색 API 래퍼.

문서: https://developers.naver.com/docs/serviceapi/search/shopping/shopping.md
키가 없으면 MOCK 결과를 돌려준다 (개발·데모용).
"""
from __future__ import annotations

import html
import os
import re
from typing import Any

import httpx

NAVER_URL = "https://openapi.naver.com/v1/search/shop.json"
_TAG = re.compile(r"<[^>]+>")

# 네이버 응답의 productType: 1~3 일반상품(가격비교/카탈로그/단품), 4~6 중고, 7~9 단종, 10~12 판매예정
_PRODUCT_TYPE_LABEL = {
    "1": "가격비교 일반", "2": "일반 카탈로그", "3": "일반 단품",
    "4": "중고", "5": "중고", "6": "중고",
    "7": "단종", "8": "단종", "9": "단종",
    "10": "판매예정", "11": "판매예정", "12": "판매예정",
}

OVERSEAS_HINTS = ("해외", "직구", "海外", "글로벌", "overseas", "import", "알리", "aliexpress", "큐텐", "qoo10", "아마존", "amazon", "타오바오")


def _clean(s: str) -> str:
    return html.unescape(_TAG.sub("", s or "")).strip()


def _mock(query: str, n: int) -> list[dict[str, Any]]:
    base = [
        {"title": f"{query} 스킨 보디수트 긴팔 M/L", "lprice": 18900, "mallName": "쿠팡", "brand": "",
         "link": "https://example.com/p/1", "image": "", "productType": "3", "productId": "mock-1"},
        {"title": f"{query} 발레 레오타드 7부", "lprice": 24500, "mallName": "스마트스토어 댄스몰", "brand": "",
         "link": "https://example.com/p/2", "image": "", "productType": "3", "productId": "mock-2"},
        {"title": f"[해외직구] {query} 보디수트 무료배송", "lprice": 9800, "mallName": "알리익스프레스", "brand": "",
         "link": "https://example.com/p/3", "image": "", "productType": "3", "productId": "mock-3"},
    ]
    return [_normalize(x) for x in base[:n]]


def _normalize(item: dict[str, Any]) -> dict[str, Any]:
    title = _clean(item.get("title", ""))
    mall = _clean(item.get("mallName", ""))
    text = f"{title} {mall}".lower()
    return {
        "product_id": item.get("productId"),
        "title": title,
        "price_krw": int(item.get("lprice") or 0),
        "mall": mall,
        "brand": _clean(item.get("brand", "")),
        "url": item.get("link"),
        "image": item.get("image"),
        "product_type": _PRODUCT_TYPE_LABEL.get(str(item.get("productType", "")), "알 수 없음"),
        "overseas_hint": any(h in text for h in OVERSEAS_HINTS),
        "source": "naver_shopping",
    }


async def search(query: str, max_results: int = 20, sort: str = "sim") -> list[dict[str, Any]]:
    cid = os.environ.get("NAVER_CLIENT_ID")
    sec = os.environ.get("NAVER_CLIENT_SECRET")
    if not cid or not sec:
        return _mock(query, max_results)

    params = {"query": query, "display": min(max_results, 100), "sort": sort}
    headers = {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": sec}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(NAVER_URL, params=params, headers=headers)
        r.raise_for_status()
        data = r.json()
    items = [_normalize(x) for x in data.get("items", [])]
    # 중고·단종·판매예정은 어르신 추천 후보에서 제외
    return [x for x in items if x["product_type"].startswith(("가격비교", "일반"))]
