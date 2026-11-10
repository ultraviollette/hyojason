"""쿠팡 검색 결과 페이지 스크레이퍼.

임시. 쿠팡 파트너스 API 승인 후 교체.
쿠팡 약관상 회색지대이므로 개인 사용·저빈도로만 쓴다.

봇 차단(403, 캡차 페이지, 빈 결과)을 만나면 예외 대신 {"blocked": True, "items": []} 를 돌려준다.
반환 items 는 naver._normalize 와 같은 스키마에 rocket(로켓배송 배지), sold_out(품절 표시)을 더한 것.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .naver import OVERSEAS_HINTS

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.coupang.com/np/search"
BASE = "https://www.coupang.com"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

_PRODUCT_HREF = re.compile(r"/vp/products/(\d+)")
_PRICE = re.compile(r"([\d,]{3,})\s*원")
_WS = re.compile(r"\s+")
_BLOCK_MARKERS = ("captcha", "access denied", "are you a robot", "비정상적인 접근", "보안 문자")


def _blocked(reason: str) -> dict[str, Any]:
    log.warning("coupang scrape blocked: %s", reason)
    return {"blocked": True, "reason": reason, "items": []}


def _card_of(a) -> Any:
    """상품 링크를 감싼 카드(li)를 찾는다. 없으면 링크 자체."""
    return a.find_parent("li") or a


def _parse(html: str, max_results: int) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for a in soup.select('a[href*="/vp/products/"]'):
        m = _PRODUCT_HREF.search(a.get("href", ""))
        if not m or m.group(1) in seen:
            continue
        card = _card_of(a)
        text = _WS.sub(" ", card.get_text(" ")).strip()

        name_el = card.select_one('[class*="name"]')
        img = card.find("img")
        title = (name_el.get_text(" ", strip=True) if name_el else "") or (img.get("alt", "") if img else "")
        if not title:
            continue

        price_el = card.select_one('[class*="price-value"], [class*="Price"] strong, strong[class*="price"]')
        price_src = price_el.get_text(" ", strip=True) + "원" if price_el else text
        pm = _PRICE.search(price_src)

        badge_alts = " ".join(i.get("alt", "") for i in card.find_all("img"))
        rocket = "로켓" in badge_alts or "로켓배송" in text
        sold_out = any(k in text for k in ("품절", "일시품절", "Sold out"))
        lower = f"{title} {text}".lower()

        seen.add(m.group(1))
        items.append({
            "product_id": m.group(1),
            "title": title,
            "price_krw": int(pm.group(1).replace(",", "")) if pm else 0,
            "mall": "쿠팡",
            "brand": "",
            "url": urljoin(BASE, a["href"]),
            "image": (img.get("src") or img.get("data-img-src") or "") if img else "",
            "product_type": "일반 단품",
            "overseas_hint": "로켓직구" in text or any(h in lower for h in OVERSEAS_HINTS),
            "rocket": rocket,
            "sold_out": sold_out,
            "source": "coupang_scrape",
        })
        if len(items) >= max_results:
            break
    return items


async def search_coupang(query: str, max_results: int = 10) -> dict[str, Any]:
    """쿠팡 검색 결과에서 상품 카드를 읽는다. 반환: {"blocked": bool, "items": [...]}"""
    headers = {"User-Agent": _UA, "Accept-Language": "ko-KR,ko;q=0.9"}
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=headers) as c:
            r = await c.get(SEARCH_URL, params={"q": query})
    except httpx.HTTPError as e:
        return _blocked(f"request failed: {type(e).__name__}")

    if r.status_code != 200:
        return _blocked(f"HTTP {r.status_code}")
    low = r.text.lower()
    if any(k in low for k in _BLOCK_MARKERS):
        return _blocked("captcha/robot page")

    items = _parse(r.text, max_results)
    if not items:
        return _blocked("no product cards parsed")
    return {"blocked": False, "items": items}
