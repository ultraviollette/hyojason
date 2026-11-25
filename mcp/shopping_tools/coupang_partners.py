"""쿠팡 파트너스 Open API 상품 검색 (coupang_search 도구가 쓴다).

- 호출 조건: COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY 가 둘 다 있고 COUPANG_PARTNERS_VERIFIED=1 일 때만.
  하나라도 없으면 요청하지 않고 "사용 불가"를 돌려준다(예외를 내지 않는다).
- 경로·서명·응답 칸은 2026-11-25 사용자가 scripts/coupang_search_probe.py 로 실제 응답을 보고 확인했다:
  v1 경로 HTTP 200 rCode "0", 상품 칸은 rank, productName, productPrice, isRocket, isFreeShipping, productUrl, keyword.
  categoryName·판매자·품절·해외배송·옵션 칸은 없다. productPrice 는 19000.0 처럼 소수로 올 때가 있고 배송비를 뺀 값이다.
- 호출 제한: 문서상 분당 50회. 여유를 두고 분당 30회에서 멈춘다(사용자 결정).
- 채움 결과: 검색 결과가 없으면 API 가 골드박스 상품(검색어와 무관, 호출마다 다름)으로 채워 돌려준다.
  그때 landingUrl 은 AFFGOLDBOX 이고, 정상 검색이면 AFFSRP 다(11/25 사용자 실사용·시험 스크립트로 확인).
  landingUrl 이 AFFSRP 로 시작하지 않으면 상품을 버리고 empty 로 돌려준다(로그 outcome=filler).
- 저장 금지: 이 API 에서 받은 데이터는 약관상 저장·복제하면 안 된다. profiles.record_item 이 link.coupang.com 주소를 거절한다.
- 로그(stderr)에는 결과 종류와 개수만 남긴다. 키·서명·검색어는 남기지 않는다.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

# stdio MCP 라 stdout 은 프로토콜이 쓴다. 로그는 stderr 로만(check_candidates 개수 로그와 같은 방식).
log = logging.getLogger("hyojason.coupang_search")
if not log.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False

HOST = "https://api-gateway.coupang.com"
SEARCH_PATH = "/v2/providers/affiliate_open_api/apis/openapi/v1/products/search"   # 11/25 실제 응답으로 확인
LIMIT = 10                        # 검색 1회 상품 수
SEARCH_PER_MINUTE = 30            # 문서상 분당 50회보다 낮게(사용자 결정)
TIMEOUT_S = 5                     # 넘으면 사용 불가로 보고 웹 검색으로 간다
SEARCH_LANDING_PREFIX = "https://link.coupang.com/re/AFFSRP"   # 정상 검색 결과의 landingUrl (아니면 채움 결과)
# 쿠팡 주소 → 파트너스 주소(deeplink). 11/25 사용자가 scripts/coupang_deeplink_probe.py 로 확인: 검색과 같은 서명(POST, 쿼리 없음),
# 본문 {"coupangUrls": [쿠팡 검색 주소]} → data[0].shortenUrl "https://link.coupang.com/a/…"(카카오 인앱에서 쿠팡 검색 화면이 열림).
# 상품 주소(7088615161)는 HTTP 200 rCode 400 "url convert failed" 였다. 그래서 검색 화면 주소만 바꾼다.
DEEPLINK_PATH = "/v2/providers/affiliate_open_api/apis/openapi/v1/deeplink"
COUPANG_SEARCH_PAGE = "https://www.coupang.com/np/search?q="
PARTNERS_LINK_PREFIX = "https://link.coupang.com/"
# 쿠팡 파트너스 대가성 문구(사용자가 정한 문장). 카카오 브릿지(kakao-bridge-vps/server.py)의 DISCLOSURE 와 같아야 한다.
DISCLOSURE = "이 게시물은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."

_calls: deque[float] = deque()


def has_credentials() -> bool:
    return bool(os.environ.get("COUPANG_ACCESS_KEY") and os.environ.get("COUPANG_SECRET_KEY"))


def is_verified() -> bool:
    """사람이 켜 둔 스위치. 1 이 아니면 키가 있어도 쓰지 않는다."""
    return os.environ.get("COUPANG_PARTNERS_VERIFIED") == "1"


def is_enabled() -> bool:
    return has_credentials() and is_verified()


def signed_date(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%y%m%dT%H%M%SZ")


def authorization(method: str, path: str, query: str, access_key: str, secret_key: str,
                  now: datetime | None = None) -> str:
    """HMAC-SHA256 서명 헤더. 서명 대상 = signed-date + method + path + query('?' 없이).
    이 형식으로 실제 호출이 200 을 받았다(11/25, 시험 스크립트)."""
    dt = signed_date(now)
    message = f"{dt}{method}{path}{query}"
    signature = hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"CEA algorithm=HmacSHA256, access-key={access_key}, signed-date={dt}, signature={signature}"


def _rate_limited(now: float) -> bool:
    while _calls and now - _calls[0] >= 60:
        _calls.popleft()
    return len(_calls) >= SEARCH_PER_MINUTE


def price_krw(v: Any) -> int | None:
    """19000.0 → 19000. 숫자로 읽히지 않거나 0 이하면 None."""
    try:
        n = round(float(v))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def page_key(url: str) -> str:
    """productUrl 의 pageKey(같은 상품). 없으면 주소 그대로를 묶음 키로 쓴다."""
    try:
        return parse_qs(urlsplit(url).query).get("pageKey", [""])[0] or url
    except ValueError:
        return url


def _row(p: dict[str, Any]) -> dict[str, Any] | None:
    url = p.get("productUrl")
    name = p.get("productName")
    price = price_krw(p.get("productPrice"))
    if not isinstance(url, str) or not url or not isinstance(name, str) or not name.strip() or price is None:
        return None
    return {"rank": p.get("rank"), "name": name.strip(), "price_krw": price, "is_rocket": p.get("isRocket") is True,
            # isFreeShipping 칸이 없는 줄이 많다(11/25). 없거나 true 가 아니면 무료배송이 아닌 것으로 본다
            "is_free_shipping": p.get("isFreeShipping") is True, "url": url, "page_key": page_key(url),
            # 줄마다 다른 옵션의 사진(11/25: 사진으로 향을 구분할 수 있었다). 받은 글자 그대로, 없으면 ""
            "image": p.get("productImage") if isinstance(p.get("productImage"), str) else ""}


def _unavailable(reason: str, path: str = "coupang_search") -> dict[str, Any]:
    log.warning("path=%s outcome=unavailable reason=%s", path, reason)
    return {"status": "unavailable", "rows": [], "url": None, "reason": reason}


async def search(keyword: str, *, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """반환: {"status": "ok" | "empty" | "unavailable", "rows": [...], "reason": 사용 불가 이유(로그용)}.
    ok: 상품 줄이 있음. empty: rCode 0 인데 0건이거나 채움 결과(landingUrl 이 AFFSRP 가 아님). 둘 다 찾아봤는데 없음. unavailable: 키·스위치 없음, 한도, 시간 초과,
    네트워크 예외, HTTP 오류, rCode≠0, 형식 오류. 예외를 내지 않는다."""
    if not has_credentials():
        return _unavailable("no_credentials")
    if not is_verified():
        return _unavailable("not_verified")
    now = time.monotonic()
    if _rate_limited(now):
        return _unavailable("rate_limited")

    query_string = urlencode({"keyword": keyword, "limit": LIMIT})
    auth = authorization("GET", SEARCH_PATH, query_string,
                         os.environ["COUPANG_ACCESS_KEY"], os.environ["COUPANG_SECRET_KEY"])
    _calls.append(now)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S, transport=transport) as c:
            r = await c.get(f"{HOST}{SEARCH_PATH}?{query_string}",
                            headers={"Authorization": auth, "Content-Type": "application/json;charset=UTF-8"})
    except httpx.TimeoutException:
        return _unavailable("timeout")
    except Exception as e:  # 네트워크 예외 등. 메시지에는 주소(검색어)가 담길 수 있어 이름만 남긴다
        return _unavailable(f"request_failed:{type(e).__name__}")
    if r.status_code != 200:
        return _unavailable(f"http_{r.status_code}")
    try:
        data = r.json()
    except ValueError:
        return _unavailable("bad_json")
    if not isinstance(data, dict):
        return _unavailable("bad_shape")
    if str(data.get("rCode")) != "0":
        return _unavailable(f"api_{str(data.get('rCode'))[:10]}")
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    products = inner.get("productData")
    if products is None:
        products = []
    if not isinstance(products, list):
        return _unavailable("bad_shape")
    rows = [x for x in (_row(p) for p in products if isinstance(p, dict)) if x]
    landing = inner.get("landingUrl")
    if rows and not (isinstance(landing, str) and landing.startswith(SEARCH_LANDING_PREFIX)):
        log.info("path=coupang_search outcome=filler rows=%d", len(rows))   # 검색어와 무관한 채움 상품은 버린다
        return {"status": "empty", "rows": [], "reason": None}
    status = "ok" if rows else "empty"
    log.info("path=coupang_search outcome=%s rows=%d", status, len(rows))
    return {"status": status, "rows": rows, "reason": None}


def search_page_url(keyword: str) -> str:
    """쿠팡 검색 화면 주소. 검색어는 인코딩하지 않고 한글·공백을 그대로 붙인다(사용자 결정, 11/26).
    실제 호출로 확인: "…/np/search?q=메구리즘 유자향" → rCode 0, landingUrl 의 pageKey 에 "%EB%A9%94…%20%EC%9C%A0…"로
    온전히 들어왔다. 연속 공백·앞뒤 공백만 정리한다. JSON 본문으로 가므로 주소창 인코딩은 쿠팡 쪽이 한다."""
    return COUPANG_SEARCH_PAGE + " ".join(keyword.split())


async def search_link(keyword: str, *, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """쿠팡 검색 화면 주소를 deeplink 로 파트너스 주소로 바꾼다. 반환: {"status": "ok" | "unavailable", "url": shortenUrl}.
    키·스위치 없음, 한도(검색과 같은 분당 한도를 같이 씀), 시간 초과, 네트워크 예외, HTTP 오류, rCode≠0, 형식 오류는
    unavailable. 예외를 내지 않는다. 로그는 결과 종류만(검색어·주소 없음)."""
    path = "coupang_search_link"
    if not has_credentials():
        return _unavailable("no_credentials", path)
    if not is_verified():
        return _unavailable("not_verified", path)
    now = time.monotonic()
    if _rate_limited(now):
        return _unavailable("rate_limited", path)

    auth = authorization("POST", DEEPLINK_PATH, "", os.environ["COUPANG_ACCESS_KEY"], os.environ["COUPANG_SECRET_KEY"])
    _calls.append(now)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S, transport=transport) as c:
            r = await c.post(f"{HOST}{DEEPLINK_PATH}", json={"coupangUrls": [search_page_url(keyword)]},
                             headers={"Authorization": auth, "Content-Type": "application/json;charset=UTF-8"})
    except httpx.TimeoutException:
        return _unavailable("timeout", path)
    except Exception as e:  # 메시지에 주소(검색어)가 담길 수 있어 이름만 남긴다
        return _unavailable(f"request_failed:{type(e).__name__}", path)
    if r.status_code != 200:
        return _unavailable(f"http_{r.status_code}", path)
    try:
        data = r.json()
    except ValueError:
        return _unavailable("bad_json", path)
    if not isinstance(data, dict):
        return _unavailable("bad_shape", path)
    if str(data.get("rCode")) != "0":
        return _unavailable(f"api_{str(data.get('rCode'))[:10]}", path)
    items = data.get("data")
    first = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}
    url = first.get("shortenUrl")
    # 브릿지가 대가성 문구를 붙이는 기준이 link.coupang.com 이라, 다른 주소는 쓰지 않는다
    if not (isinstance(url, str) and url.startswith(PARTNERS_LINK_PREFIX) and url.isascii() and " " not in url):
        return _unavailable("bad_shape", path)
    log.info("path=%s outcome=ok", path)
    return {"status": "ok", "rows": [], "url": url, "reason": None}
