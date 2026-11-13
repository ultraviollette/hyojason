"""미검증: 공식 문서 대조 전까지 사용 금지.

쿠팡 파트너스 Open API 상품 검색 (승인 후 쓸 자리). 이 파일은 공식 문서를 읽지 못한 채 기억으로 짰다.
파트너스 콘솔의 API 문서와 대조해 고치기 전까지는 키가 있어도 호출하지 않는다.
- 호출 조건: COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY 가 둘 다 있고, COUPANG_PARTNERS_VERIFIED=1 일 때만.
  COUPANG_PARTNERS_VERIFIED 는 공식 문서와 대조를 마친 뒤에만 1 로 둔다.

⚠️ 확인 필요: 아래 엔드포인트·서명 형식·응답 필드·호출 제한·대가성 문구는 공식 문서로 확인하지 못했다
   (partners.coupang.com, developers.coupang.com 이 자동 접근에 403). 작성자의 사전 지식 기준이다.
   키를 받은 뒤 파트너스 콘솔의 Open API 문서와 이 파일 맨 위 상수들을 대조할 것.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)

# ---- 확인 필요: 공식 문서와 대조할 값들 ----
HOST = "https://api-gateway.coupang.com"
SEARCH_PATH = "/v2/providers/affiliate_open_api/apis/openapi/v1/products/search"
MAX_LIMIT = 10                    # 검색 1회 최대 상품 수
SEARCH_PER_HOUR = 10              # 검색 호출 제한(시간당). 넘으면 호출하지 않고 다음 provider 로 넘긴다
DISCLOSURE = "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."
# --------------------------------------------

_calls: deque[float] = deque()


def has_credentials() -> bool:
    return bool(os.environ.get("COUPANG_ACCESS_KEY") and os.environ.get("COUPANG_SECRET_KEY"))


def is_verified() -> bool:
    """공식 문서 대조를 마쳤다고 사람이 표시했는지. 미검증 코드는 키가 있어도 쓰지 않는다."""
    return os.environ.get("COUPANG_PARTNERS_VERIFIED") == "1"


def is_enabled() -> bool:
    return has_credentials() and is_verified()


def signed_date(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%y%m%dT%H%M%SZ")


def authorization(method: str, path: str, query: str, access_key: str, secret_key: str,
                  now: datetime | None = None) -> str:
    """HMAC-SHA256 서명 헤더. 서명 대상 = signed-date + method + path + query('?' 없이). (확인 필요)"""
    dt = signed_date(now)
    message = f"{dt}{method}{path}{query}"
    signature = hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"CEA algorithm=HmacSHA256, access-key={access_key}, signed-date={dt}, signature={signature}"


def _rate_limited(now: float) -> bool:
    while _calls and now - _calls[0] >= 3600:
        _calls.popleft()
    return len(_calls) >= SEARCH_PER_HOUR


def _normalize(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "product_id": str(p.get("productId", "")),
        "title": str(p.get("productName", "")),
        "price_krw": int(p.get("productPrice") or 0),
        "mall": "쿠팡",
        "brand": "",
        "url": p.get("productUrl", ""),
        "image": p.get("productImage", ""),
        "product_type": "일반 단품",
        "overseas_hint": False,
        "rocket": bool(p.get("isRocket")),
        "free_shipping": bool(p.get("isFreeShipping")),
        "category": p.get("categoryName", ""),
        "affiliate": True,
        "source": "coupang_partners",
    }


async def search(query: str, max_results: int = 10, *, transport: httpx.AsyncBaseTransport | None = None
                 ) -> dict[str, Any]:
    """반환: {"items": [...], "error": None | "no_credentials" | "rate_limited" | "http_<code>" | "api_<rCode>" | ...}"""
    if not has_credentials():
        return {"items": [], "error": "no_credentials"}  # 키 없이는 요청하지 않는다
    if not is_verified():
        return {"items": [], "error": "unverified"}      # 미검증: 공식 문서 대조 전까지 사용 금지
    now = time.monotonic()
    if _rate_limited(now):
        log.warning("coupang partners search skipped: hourly limit %d reached", SEARCH_PER_HOUR)
        return {"items": [], "error": "rate_limited"}

    query_string = urlencode({"keyword": query, "limit": min(max_results, MAX_LIMIT)})
    auth = authorization("GET", SEARCH_PATH, query_string,
                         os.environ["COUPANG_ACCESS_KEY"], os.environ["COUPANG_SECRET_KEY"])
    _calls.append(now)
    try:
        async with httpx.AsyncClient(timeout=10, transport=transport) as c:
            r = await c.get(f"{HOST}{SEARCH_PATH}?{query_string}",
                            headers={"Authorization": auth, "Content-Type": "application/json;charset=UTF-8"})
    except httpx.HTTPError as e:
        log.warning("coupang partners search failed: %s", type(e).__name__)
        return {"items": [], "error": f"request_failed:{type(e).__name__}"}
    if r.status_code != 200:
        log.warning("coupang partners search http %d", r.status_code)
        return {"items": [], "error": f"http_{r.status_code}"}
    data = r.json()
    if str(data.get("rCode", "0")) != "0":
        log.warning("coupang partners search rCode %s", data.get("rCode"))
        return {"items": [], "error": f"api_{data.get('rCode')}"}
    products = ((data.get("data") or {}).get("productData") or [])[:max_results]
    return {"items": [_normalize(p) for p in products], "error": None}
