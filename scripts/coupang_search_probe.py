"""쿠팡 파트너스 /products/search 응답 모양을 눈으로 보는 시험용 스크립트. 효자손 코드와 연결하지 않는다.

사용법: python scripts/coupang_search_probe.py "검색어"

- 키는 환경 변수 COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY 에서만 읽는다
  (mcp/shopping_tools/coupang_partners.py 와 같은 이름). 키·서명·Authorization 헤더는 출력하지 않는다.
- 응답은 화면에만 출력하고 파일로 저장하지 않는다(약관상 API 데이터 저장 금지).
- 표준 라이브러리만 쓴다.

⚠️ 공식 예시로 확인 안 됨: 서명 형식(GMT yyMMdd'T'HHmmss'Z' + 메서드 + 경로 + 쿼리, HmacSHA256 hex,
   "CEA algorithm=HmacSHA256, access-key=…, signed-date=…, signature=…")은 사용자 기억 기준이다.
⚠️ 확인 필요: KNOWN_* 의 칸 목록도 공식 문서로 확인하지 못했다. 사용자가 출력하라고 한 칸과
   coupang_partners.py 가 기대하는 칸을 모은 것이다.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlencode

HOST = "https://api-gateway.coupang.com"
PATH_V1 = "/v2/providers/affiliate_open_api/apis/openapi/v1/products/search"
PATH_NO_V1 = "/v2/providers/affiliate_open_api/apis/openapi/products/search"  # 문서 예시 주소 모양
ENV_NAMES = ("COUPANG_ACCESS_KEY", "COUPANG_SECRET_KEY")

# 확인 필요: 공식 문서와 대조할 "알려진 칸" 목록
KNOWN_TOP = {"rCode", "rMessage", "data"}
KNOWN_DATA = {"landingUrl", "productData"}
KNOWN_PRODUCT = {"rank", "productName", "productPrice", "isRocket", "isFreeShipping", "productUrl",
                 "productId", "productImage", "categoryName"}
PRODUCT_FIELDS = ("rank", "productId", "productName", "productPrice", "isRocket", "isFreeShipping", "productUrl",
                  "productImage")


def authorization(method: str, path: str, query: str, access_key: str, secret_key: str) -> str:
    signed_date = datetime.now(timezone.utc).strftime("%y%m%dT%H%M%SZ")
    message = f"{signed_date}{method}{path}{query}"
    signature = hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"CEA algorithm=HmacSHA256, access-key={access_key}, signed-date={signed_date}, signature={signature}"


def redact(text: str, secrets: list[str]) -> str:
    """혹시 응답 본문에 키가 되돌아와도 화면에 찍히지 않게 가린다."""
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def call(path: str, query: str, access_key: str, secret_key: str) -> tuple[int, str]:
    """(HTTP 상태, 본문) 을 돌려준다. 요청 헤더는 어디에도 출력하지 않는다."""
    req = urllib.request.Request(
        f"{HOST}{path}?{query}",
        method="GET",
        headers={
            "Authorization": authorization("GET", path, query, access_key, secret_key),
            "Content-Type": "application/json;charset=UTF-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def unknown_keys(obj: object, known: set[str]) -> list[str]:
    return sorted(set(obj) - known) if isinstance(obj, dict) else []


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        print('사용법: python scripts/coupang_search_probe.py "검색어"')
        return 2
    keyword = sys.argv[1].strip()

    missing = [n for n in ENV_NAMES if not os.environ.get(n)]
    if missing:
        print("환경 변수가 없습니다: " + ", ".join(missing))
        return 1
    access_key, secret_key = (os.environ[n] for n in ENV_NAMES)

    query = urlencode({"keyword": keyword, "limit": 10})
    path = PATH_V1
    status, body = call(path, query, access_key, secret_key)
    if status == 404:
        print(f"v1 경로 404 → /v1/ 없는 경로로 한 번 더 시도")
        path = PATH_NO_V1
        status, body = call(path, query, access_key, secret_key)

    secrets = [access_key, secret_key]
    print(f"경로: {'v1 있음' if path == PATH_V1 else 'v1 없음'} ({path})")
    print(f"HTTP 상태: {status}")

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        print("JSON 이 아닌 응답 (앞 500자):")
        print(redact(body[:500], secrets))
        return 1
    if not isinstance(data, dict):
        print("예상과 다른 모양(최상위가 객체가 아님) (앞 500자):")
        print(redact(body[:500], secrets))
        return 1

    inner = data.get("data")
    landing = inner.get("landingUrl") if isinstance(inner, dict) else None
    print(f"rCode: {data.get('rCode')}")
    print(f"rMessage: {redact(str(data.get('rMessage')), secrets)}")
    print(f"landingUrl: {landing}")

    products = inner.get("productData") if isinstance(inner, dict) else None
    if not isinstance(products, list):
        print("productData 없음. 최상위 칸: " + ", ".join(sorted(data)))
        if status != 200:
            print("본문 (앞 500자):")
            print(redact(body[:500], secrets))
    else:
        print(f"\n상품 {len(products)}개")
        for p in products:
            if not isinstance(p, dict):
                print(f"- 객체가 아닌 항목: {type(p).__name__}")
                continue
            print("-" * 60)
            for f in PRODUCT_FIELDS:
                print(f"  {f}: {p.get(f, '(없음)')}")

    extra_top = unknown_keys(data, KNOWN_TOP)
    extra_data = unknown_keys(inner, KNOWN_DATA)
    extra_product = sorted({k for p in (products or []) for k in unknown_keys(p, KNOWN_PRODUCT)})
    print("\n문서(확인 필요 목록)에 없는 칸 — 이름만:")
    print(f"  최상위: {', '.join(extra_top) or '없음'}")
    print(f"  data: {', '.join(extra_data) or '없음'}")
    print(f"  상품: {', '.join(extra_product) or '없음'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
