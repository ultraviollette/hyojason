"""효자손 MCP 서버.

도구:
  get_client_profile / save_client_profile / update_client_profile / resolve_client
  make_search_links        정확한 상품명 → 네이버쇼핑 검색 링크 + 쿠팡 앱 검색어 (페이지는 읽지 않음)
  search_products          네이버 쇼핑 검색 → (키 없으면) 쿠팡 임시 스크레이퍼 → (차단되면) mock
  check_product            상품 페이지를 읽고 Jev 게이트(재고·국내발송·기한·금지조건) 통과 여부
  log_recommendation       추천·선택 기록

실행:  uv run --project mcp python -m shopping_tools.server   (stdio)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup
try:  # mcp SDK 2.x
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from . import coupang_scrape, gates, naver, profiles

mcp = _Server("hyojason-shopping-tools")


# ---------- 클라이언트 프로필 ----------

@mcp.tool()
def resolve_client(channel: str, channel_user_id: str) -> dict[str, Any]:
    """채널 사용자 ID(kakao/telegram)로 client_id 를 찾는다. 없으면 found=false."""
    cid = profiles.resolve_client_id(channel, channel_user_id)
    return {"found": cid is not None, "client_id": cid}


@mcp.tool()
def get_client_profile(client_id: str) -> dict[str, Any]:
    """클라이언트 프로필(치수·선호·금지 조건·쇼핑 규칙·이력)을 읽는다. 없으면 exists=false."""
    p = profiles.get_profile(client_id)
    return {"exists": p is not None, "profile": p}


@mcp.tool()
def save_client_profile(profile_json: str) -> dict[str, Any]:
    """새 클라이언트 프로필 저장. profile_json 은 clients/_example.yaml 구조의 JSON 문자열."""
    data = json.loads(profile_json)
    return {"saved": True, "profile": profiles.save_profile(data)}


@mcp.tool()
def update_client_profile(client_id: str, patch_json: str) -> dict[str, Any]:
    """프로필 일부 갱신(얕은 병합, 리스트는 합집합). 예: {"preferences": {"dislikes": ["목 파인 옷"]}}"""
    patch = json.loads(patch_json)
    return {"updated": True, "profile": profiles.update_profile(client_id, patch)}


@mcp.tool()
def log_recommendation(client_id: str, request_json: str, chosen_json: str = "") -> dict[str, Any]:
    """무엇을 요청받아 무엇을 추천/선택했는지 이력에 남긴다."""
    profiles.append_history(client_id, {
        "request": json.loads(request_json),
        "chosen": json.loads(chosen_json) if chosen_json else None,
    })
    return {"logged": True}


# ---------- 검색 ----------

@mcp.tool()
async def search_products(query: str, max_results: int = 20, sort: str = "sim") -> dict[str, Any]:
    """상품을 찾는다. sort: sim(정확도) | asc(가격 낮은순) | dsc | date (네이버에만 적용).
    반환 items[].url을 check_product(product_url=…)에 넘길 것.
    순서: 네이버 키 있으면 네이버 → 없으면 쿠팡 임시 스크레이퍼(source="coupang_scrape")
    → 차단·빈 결과면 mock(source="mock", 실제 상품 아님).
    """
    if naver.has_credentials():
        items = await naver.search(query, max_results=max_results, sort=sort)
    else:
        r = await coupang_scrape.search_coupang(query, max_results=min(max_results, 10))
        items = r["items"] if not r.get("blocked") and r["items"] else await naver.search(query, max_results=max_results)
    return {"query": query, "count": len(items), "items": items}


_SPACES = re.compile(r"\s+")
# 쿠팡 검색 링크는 카카오 인앱 브라우저에서 www·m 주소 모두 "사용권한이 없습니다"로 막혀 링크 대신 검색어만 준다.
NAVER_SHOP_SEARCH = "https://m.search.naver.com/search.naver?where=m_shop&query="


@mcp.tool()
def make_search_links(query: str) -> dict[str, Any]:
    """정확한 상품명(브랜드 + 제품명 + 호수/색상·용량·개수)으로 네이버쇼핑 검색 링크와 쿠팡 앱 검색어를 만든다.
    반환: naver_shopping(URL), coupang_query(쿠팡 앱에서 검색할 글자. 링크 아님).
    쿠팡은 카카오 인앱 브라우저에서 검색 링크가 막혀 URL 을 주지 않는다. 쿠팡 URL 을 직접 만들지 않는다.
    링크 URL 은 직접 조립하지 말고 이 도구가 돌려준 것을 그대로 보낸다.
    이 도구는 페이지를 읽지 않는다. 재고·가격·도착일은 확인되지 않은 상태다.
    """
    q = _SPACES.sub(" ", query or "").strip()
    if not q:
        return {"error": "상품명이 비어 있어요"}
    enc = quote(q, safe="")
    return {"query": q, "naver_shopping": NAVER_SHOP_SEARCH + enc, "coupang_query": q}


# ---------- 검증 ----------

_WS = re.compile(r"\s+")
_MIN_TEXT = 300      # 실제 상품 페이지 본문은 수천 자. 이보다 짧으면 빈 본문·JS 렌더링 페이지로 본다
_BLOCK_SCAN = 2000   # 차단·오류 페이지는 짧다. 이보다 긴 본문에서는 아래 문구를 차단 근거로 쓰지 않는다
_BLOCK_PAGE = re.compile(
    r"(captcha|access denied|are you a robot|봇\s*확인|봇\(bot\)|보안\s*문자|비정상적인\s*접근"
    r"|사용권한이\s*(없|제한)|접속이\s*불가|에러\s*페이지|시스템\s*오류)", re.I)
_KIDS = re.compile(r"(아동|키즈|주니어|kids|junior)", re.I)  # 상품명에서만 본다 (본문 추천 영역 오탐 방지)
_BUYABLE = re.compile(r"(구매하기|바로\s*구매|주문하기|장바구니\s*담기|add\s*to\s*cart|buy\s*now)", re.I)


@dataclass
class Page:
    text: str                       # 읽지 못했으면 "" (오류 페이지 본문을 판정 근거로 쓰지 않는다)
    final_url: str
    status: int | None
    unreadable_reason: str | None   # None 이면 읽음
    page_title: str = ""            # 읽었을 때만 <title>


async def _fetch_page(url: str, limit: int = 6000, *, transport: httpx.AsyncBaseTransport | None = None) -> Page:
    """상품 페이지를 가져와 본문 텍스트와 '읽었는지'를 판정한다.
    차단(403/429 등), 캡차·오류 페이지, 빈 본문(JS 렌더링 필요)은 읽지 못한 것으로 본다."""
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True, transport=transport,
                                     headers={"User-Agent": "Mozilla/5.0 (hyojason-bot)"}) as c:
            r = await c.get(url)
    except Exception as e:
        return Page("", url, None, f"페이지 요청 실패({type(e).__name__})")

    soup = BeautifulSoup(r.text, "html.parser")
    page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    text = _WS.sub(" ", soup.get_text(" ")).strip()[:limit]

    if r.status_code != 200:
        reason = f"HTTP {r.status_code} (차단 또는 오류 페이지)"
    elif len(text) < _BLOCK_SCAN and _BLOCK_PAGE.search(text):
        reason = "봇 차단·오류 페이지"
    elif len(text) < _MIN_TEXT:
        reason = "본문이 거의 없음 (JS 렌더링이 필요하거나 빈 페이지)"
    else:
        reason = None
    if reason is not None:
        return Page("", str(r.url), r.status_code, reason)
    return Page(text, str(r.url), r.status_code, None, page_title)


@mcp.tool()
async def check_product(product_url: str, title: str = "", mall: str = "",
                        deadline: str = "", forbidden_terms_json: str = "[]") -> dict[str, Any]:
    """product_url에는 search_products가 돌려준 url을 그대로 넣는다. product_id가 아니다.
    상품 하나를 검증한다. deadline 은 '이번 주 안', '수요일까지', '2026-11-12' 등.
    반환: in_stock / domestic_shipping / arrives_by_deadline / violates_profile (yes|no|unknown), note, buy_url,
          readable(페이지를 읽었는지), unreadable_reason, kids_item(상품명에 아동/키즈/주니어).
    readable=false 이면 in_stock 은 unknown 이고 passed 는 false 다. 이때 '품절'이라고 말하면 안 된다.
    """
    forbidden = json.loads(forbidden_terms_json or "[]")
    dl = gates.parse_deadline(deadline) if deadline else None
    page = await _fetch_page(product_url)
    page_text = page.text
    evidence = f"{title} {mall} {page_text}"

    result, engine = gates.decide_all(evidence, deadline=dl, forbidden=forbidden)

    # 재고는 페이지에 근거(품절 표시 또는 구매 버튼 문구)가 있을 때만 판정한다.
    unreadable = page.unreadable_reason
    if unreadable is None and not (gates._OUT_OF_STOCK.search(page_text) or _BUYABLE.search(page_text)):
        unreadable = "재고 표시를 찾지 못함"
    if unreadable is not None:
        result["in_stock"] = "unknown"
    elif result["in_stock"] == "unknown" and _BUYABLE.search(page_text):
        result["in_stock"] = "yes"

    kids = _KIDS.search(f"{title} {page.page_title}")

    notes = []
    if kids:
        notes.append(f"아동용 상품이에요 (상품명에 '{kids.group(1)}')")
    if unreadable is not None:
        notes.append(f"페이지를 읽지 못함: {unreadable}")
    if result["in_stock"] == "no":
        notes.append("품절 표시가 있어요")
    if result["domestic_shipping"] == "no":
        notes.append("외국에서 오는 상품이라 2주쯤 걸려요" if dl is None else "외국 발송이라 기한 안에 못 와요")
    if result["violates_profile"] == "yes":
        hit = [t for t in forbidden if t and t.lower() in evidence.lower()]
        notes.append(f"피하시는 조건에 해당: {', '.join(hit)}")

    passed = (unreadable is None
              and result["in_stock"] != "no"
              and result["violates_profile"] != "yes"
              and (dl is None or result["arrives_by_deadline"] != "no")
              and (dl is None or result["domestic_shipping"] != "no"))

    return {
        **result,
        "passed": passed,
        "readable": unreadable is None,
        "unreadable_reason": unreadable,
        "kids_item": kids is not None,
        "http_status": page.status,
        "deadline_parsed": dl.isoformat() if dl else None,
        "note": " / ".join(notes) or "특이사항 없음",
        "buy_url": page.final_url,
        "evidence_chars": len(page_text),
        "gate_engine": engine,  # 'jev' | 'rules'
    }


if __name__ == "__main__":
    mcp.run()
