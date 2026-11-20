"""효자손 MCP 서버.

도구:
  resolve_client / get_client_profile / save_client_profile(정해진 필드만, 저장·수정 겸용)
  make_search_links        정확한 상품명 → 네이버쇼핑 검색 링크 + 쿠팡 앱 검색어 (페이지는 읽지 않음)
  search_products          provider 구조: 쿠팡 파트너스 → 네이버 (키 있는 것만). 없으면 웹 검색 안내
  check_product            상품 페이지를 읽고 Jev 게이트(재고·국내발송·기한·금지조건) 통과 여부
  record_item              찾아 드린(found)·고르신(chosen) 물건 기록 (칸·50건 상한을 코드로 강제)

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

from . import coupang_partners, gates, naver, profiles

mcp = _Server("hyojason-shopping-tools")


# ---------- 클라이언트 프로필 ----------

def _view_response(cid: str | None) -> tuple[dict[str, Any] | None, int]:
    return profiles.agent_view(profiles.get_profile(cid) if cid else None)


@mcp.tool()
def resolve_client(channel: str, channel_user_id: str) -> dict[str, Any]:
    """대화 시작 시 가장 먼저 부른다. 채널 + 사용자 ID 로 저장된 프로필을 찾는다.
    channel 은 "kakao", channel_user_id 는 메시지 앞 [channel=kakao user_id=...] 의 user_id 값이다.
    반환: found, client_id(record_item 에 쓸 값), profile, items_omitted.
    profile.items 는 고르신 것(chosen) 전부 + 찾아 드린 것(found) 최근 10건만 담는다(최신이 앞).
    items_omitted 는 빼고 돌려준 오래된 found 건수. found=false 면 처음 온 분이다."""
    cid = profiles.resolve_client_id(channel, channel_user_id)
    view, omitted = _view_response(cid)
    return {"found": cid is not None, "client_id": cid, "profile": view, "items_omitted": omitted}


@mcp.tool()
def get_client_profile(client_id: str) -> dict[str, Any]:
    """client_id 로 프로필을 읽는다. 없으면 exists=false. 보통은 resolve_client 를 쓴다.
    client_id 자리에 카카오 user_id 를 넣어도 찾아 준다. items 는 resolve_client 와 같은 방식으로 줄여 돌려준다."""
    cid = client_id if profiles.get_profile(client_id) is not None else profiles.resolve_client_id("kakao", client_id)
    view, omitted = _view_response(cid)
    return {"exists": view is not None, "profile": view, "items_omitted": omitted}


@mcp.tool()
def save_client_profile(channel: str, channel_user_id: str, display_name: str = "", top_size: str = "",
                        bottom_size: str = "", shoe_size_mm: str = "", height_cm: str = "", body_notes: str = "",
                        delivery_region: str = "", likes: list[str] | None = None,
                        dislikes: list[str] | None = None, brands_avoid: list[str] | None = None,
                        price_sensitivity: str = "", notes: list[str] | None = None,
                        payment_place: str = "", payment_by: str = "") -> dict[str, Any]:
    """프로필을 저장하거나 고친다(같은 사용자면 합친다). 정해진 필드만 받는다. 다른 키는 만들지 않는다.
    channel="kakao", channel_user_id=<[channel=kakao user_id=...] 의 user_id> 는 필수.
    display_name: 부를 호칭(예: 어머니). top_size/bottom_size: 예 "55", "M". shoe_size_mm: 예 "235".
    delivery_region: 배송 지역(시·군 정도, 정확한 주소는 받지 않는다). likes/dislikes: 좋아하는 것/싫어하는 것 목록.
    payment_place: 주로 사는 곳(자유 입력, 20자 이내, 예: "쿠팡"). payment_by: "self"(본인) | "family"(다른 가족).
    결제 방식은 물어본 적이 있든 없든(asked_on), 사용자가 말하면 언제든 저장한다.
    결제 정보(카드·계좌)와 다른 사람 연락처는 받지 않는다.
    notes: 위에 없는 기타 메모 목록(예: "라인댄스를 함"). 빈 값은 기존 값을 지우지 않고, 목록은 합친다."""
    try:
        profile, created = profiles.upsert_profile(
            channel, channel_user_id, display_name=display_name, top_size=top_size, bottom_size=bottom_size,
            shoe_size_mm=shoe_size_mm, height_cm=height_cm, body_notes=body_notes, delivery_region=delivery_region,
            likes=likes, dislikes=dislikes, brands_avoid=brands_avoid, price_sensitivity=price_sensitivity,
            notes=notes, payment_place=payment_place, payment_by=payment_by)
    except ValueError as e:
        return {"saved": False, "error": str(e)}
    view, _ = profiles.agent_view(profile)
    return {"saved": True, "created": created, "client_id": profile["client_id"], "profile": view}


@mcp.tool()
def record_item(client_id: str, status: str, name: str, brand: str = "", options: dict[str, str] | None = None,
                seller: str = "", seen_price_krw: int | None = None) -> dict[str, Any]:
    """찾아 드린 물건(status="found")과 사용자가 고른 물건(status="chosen")을 프로필에 남긴다. client_id 는 resolve_client 가 준 값.
    name: 제품 이름(필수). brand: 브랜드(모르면 비움). options: 그 물건에서 갈리는 것 {"용량": "240ml", "색": "네이비"} (6개까지).
    seller: 판매처 이름. seen_price_krw: 그때 본 가격(원). 검색 결과나 페이지에 적혀 있던 경우만, 모르면 비운다.
    결제했는지는 알 수 없으므로 기록하지 않는다. 날짜는 서버가 채운다.
    같은 날 같은 브랜드·이름·옵션의 found 를 chosen 으로 남기면 그 항목이 chosen 으로 바뀐다. 최근 50건까지 보관.
    ask_payment 가 true 일 때만 이번 답 끝에 결제 방식 질문을 붙인다. 도구가 평생 한 번만 true 를 준다.
    결제 질문은 후보와 링크를 모두 쓴 답의 맨 끝에만 붙인다. 결제 질문만 있는 답은 쓰지 않는다(ask_payment_note)."""
    try:
        item, ask_payment = profiles.record_item(client_id, status=status, name=name, brand=brand, options=options,
                                                 seller=seller, seen_price_krw=seen_price_krw)
    except ValueError as e:
        return {"recorded": False, "error": str(e), "ask_payment": False}
    out = {"recorded": True, "item": item, "ask_payment": ask_payment}
    if ask_payment:  # 11/20 실사용: 후보를 기록하고도 답에 결제 질문만 써서 후보가 사라졌다
        out["ask_payment_note"] = ASK_PAYMENT_NOTE
    return out


# ---------- 검색 ----------

async def _naver_provider(query: str, max_results: int, sort: str) -> dict[str, Any]:
    return {"items": await naver.search(query, max_results=max_results, sort=sort), "error": None}


async def _partners_provider(query: str, max_results: int, sort: str) -> dict[str, Any]:
    return await coupang_partners.search(query, max_results=max_results)


# (이름, 키가 있는지, 검색 함수). 키가 있는 provider 만 순서대로 시도한다. 키 없는 provider 는 요청하지 않는다.
# 쿠팡 임시 스크레이퍼(coupang_scrape)는 키 없이 쿠팡에 자동 요청을 보내고 매번 403 이라 기본 경로에서 뺐다.
SEARCH_PROVIDERS = (
    ("coupang_partners", coupang_partners.is_enabled, _partners_provider),  # 키 + COUPANG_PARTNERS_VERIFIED=1
    ("naver_shopping", naver.has_credentials, _naver_provider),
)


@mcp.tool()
async def search_products(query: str, max_results: int = 20, sort: str = "sim") -> dict[str, Any]:
    """상품을 찾는다. sort: sim(정확도) | asc(가격 낮은순) | dsc | date (네이버에만 적용).
    반환 items[].url을 check_product(product_url=…)에 넘길 것.
    provider: 쿠팡 파트너스(키 + COUPANG_PARTNERS_VERIFIED=1 일 때. 미검증이라 기본은 꺼짐) → 네이버(키 있을 때).
    affiliate=true 인 결과(쿠팡 파트너스 링크)를 보여 줄 때는 disclosure 문구를 반드시 함께 보낸다.
    실제 결과가 없으면 items=[] 와 fallback="web_search" 를 돌려준다. 그때는 SKILL 3-b(웹 검색 후보)로 간다.
    """
    tried = []
    for name, ready, run in SEARCH_PROVIDERS:
        if not ready():
            continue
        try:
            r = await run(query, max_results, sort)
        except Exception as e:  # provider 하나가 실패해도 다음으로 넘어간다
            r = {"items": [], "error": f"exception:{type(e).__name__}"}
        items = [x for x in r["items"] if x.get("source") != "mock"]  # 가짜 예시 상품은 절대 내보내지 않는다
        if items:
            out = {"query": query, "count": len(items), "items": items, "provider": name}
            if any(x.get("affiliate") for x in items):
                out["disclosure"] = coupang_partners.DISCLOSURE
            return out
        tried.append({"provider": name, "error": r.get("error") or "no_results"})
    return {"query": query, "count": 0, "items": [], "fallback": "web_search", "next_step": NO_RESULT_NEXT_STEP,
            "tried": tried}


NO_RESULT_NEXT_STEP = (
    "검색 API 결과가 없다. 여기서 끝내거나 링크를 달라고 하지 말고, web_search 로 상품 페이지 후보를 찾는다(SKILL 3-b). "
    "상품 페이지만 최대 3개(검색 결과·블로그·광고·쿠팡 링크 제외), 후보마다 검색 결과에서 확인된 사실로 한 줄, "
    "가격·재고·도착일은 보인 것만 말하고 '링크에서 꼭 확인하세요'. 하나도 못 찾았을 때만 못 찾았다고 말한다."
)


_SPACES = re.compile(r"\s+")
ASK_PAYMENT_NOTE = ("후보와 링크를 먼저 모두 쓰고, 그 답의 맨 끝에 결제 방식 질문 한 줄을 붙인다. "
                    "결제 질문만 있는 답은 쓰지 않는다. 이번 답에서 기록한 후보는 모두 보여 준다.")
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
