"""쿠팡 파트너스 deeplink(쿠팡 상품 주소 → 파트너스 주소) 응답 모양을 눈으로 보는 시험용 스크립트. 효자손 코드와 연결하지 않는다.

사용법: python3 scripts/coupang_deeplink_probe.py [--clean] "<쿠팡 상품 주소>"
  --clean  물음표 뒤를 productId·itemId·vendorItemId 만 남기고 보낸다(기본은 받은 주소 그대로).

배경: 11/25 쿠팡 사이트에 로켓배송으로 있는 상품(상품 번호 7088615161)이 검색 API 에서는 이름 그대로 찾아도
AFFGOLDBOX 채움 결과였다. 상품 주소를 파트너스 주소로 바꾸는 길을 시험하려는 것이다.

- 키는 환경 변수 COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY 에서만 읽는다. 키·서명·Authorization 헤더는 출력하지 않는다.
- 응답은 화면에만 출력하고 파일로 저장하지 않는다(약관상 API 데이터 저장 금지). 표준 라이브러리만 쓴다.

⚠️ 공식 문서로 확인 안 됨: 경로(POST …/openapi/v1/deeplink)와 본문({"coupangUrls": [...]})은 사용자가 준 형식이다.
   파트너스 도움말·개발자 사이트는 자동 접근이 403 이라 읽지 못했다.
⚠️ 서명은 coupang_search_probe.py 와 같은 방식(signed-date + 메서드 + 경로 + 쿼리, 쿼리 없음)이다. 검색 API 에서는 이 방식이
   200 을 받았지만(11/25), POST·deeplink 에도 같은지는 확인하지 않았다.
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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

HOST = "https://api-gateway.coupang.com"
PATH = "/v2/providers/affiliate_open_api/apis/openapi/v1/deeplink"
ENV_NAMES = ("COUPANG_ACCESS_KEY", "COUPANG_SECRET_KEY")
KEEP_PARAMS = ("productId", "itemId", "vendorItemId")
USAGE = '사용법: python3 scripts/coupang_deeplink_probe.py [--clean] "<쿠팡 상품 주소>"'


def authorization(method: str, path: str, query: str, access_key: str, secret_key: str) -> str:
    signed_date = datetime.now(timezone.utc).strftime("%y%m%dT%H%M%SZ")
    message = f"{signed_date}{method}{path}{query}"
    signature = hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"CEA algorithm=HmacSHA256, access-key={access_key}, signed-date={signed_date}, signature={signature}"


def clean_url(url: str) -> str:
    """물음표 뒤를 productId·itemId·vendorItemId 만 남긴다(받은 순서대로). 경로·호스트는 그대로, # 뒤는 버린다."""
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k in KEEP_PARAMS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))


def redact(text: str, secrets: list[str]) -> str:
    """혹시 응답 본문에 키가 되돌아와도 화면에 찍히지 않게 가린다."""
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def call(product_url: str, access_key: str, secret_key: str) -> tuple[int, str]:
    """(HTTP 상태, 본문). 요청 헤더는 어디에도 출력하지 않는다."""
    body = json.dumps({"coupangUrls": [product_url]}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{HOST}{PATH}",
        data=body,
        method="POST",
        headers={
            "Authorization": authorization("POST", PATH, "", access_key, secret_key),
            "Content-Type": "application/json;charset=UTF-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def print_value(prefix: str, value: object, secrets: list[str]) -> None:
    """칸 이름과 값을 전부 찍는다. 안쪽 객체·목록은 이름을 이어 붙여 펼친다."""
    if isinstance(value, dict):
        if not value:
            print(f"  {prefix}: {{}}")
        for k, v in value.items():
            print_value(f"{prefix}.{k}" if prefix else str(k), v, secrets)
    elif isinstance(value, list):
        if not value:
            print(f"  {prefix}: []")
        for i, v in enumerate(value):
            print_value(f"{prefix}[{i}]", v, secrets)
    else:
        print(f"  {prefix}: {redact(json.dumps(value, ensure_ascii=False), secrets)}")


def main() -> int:
    args = sys.argv[1:]
    clean = "--clean" in args
    rest = [a for a in args if a != "--clean"]
    if len(rest) != 1 or not rest[0].strip().lower().startswith(("http://", "https://")):
        print(USAGE)
        return 2
    url = rest[0].strip()

    missing = [n for n in ENV_NAMES if not os.environ.get(n)]
    if missing:
        print("환경 변수가 없습니다: " + ", ".join(missing))
        return 1
    access_key, secret_key = (os.environ[n] for n in ENV_NAMES)
    secrets = [access_key, secret_key]

    sent = clean_url(url) if clean else url
    print(f"보낸 주소({'--clean 정리' if clean else '받은 그대로'}): {sent}")
    status, body = call(sent, access_key, secret_key)
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

    print(f"rCode: {data.get('rCode')}")
    print(f"rMessage: {redact(str(data.get('rMessage')), secrets)}")
    print("data 의 칸과 값:")
    if "data" in data:
        print_value("data", data["data"], secrets)
    else:
        print("  (data 칸 없음)")
    others = sorted(k for k in data if k not in ("rCode", "rMessage", "data"))
    if others:
        print("최상위의 다른 칸: " + ", ".join(others))
    return 0


if __name__ == "__main__":
    sys.exit(main())
