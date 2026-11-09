"""효자손 MCP 서버.

도구:
  get_client_profile / save_client_profile / update_client_profile / resolve_client
  search_products          네이버 쇼핑 검색 (키 없으면 mock)
  check_product            상품 페이지를 읽고 Jev 게이트(재고·국내발송·기한·금지조건) 통과 여부
  log_recommendation       추천·선택 기록

실행:  uv run --project mcp python -m shopping_tools.server   (stdio)
"""
from __future__ import annotations

import json
import re
from typing import Any

import httpx
from bs4 import BeautifulSoup
try:  # mcp SDK 2.x
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from . import gates, naver, profiles

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
    """네이버 쇼핑에서 상품을 찾는다. sort: sim(정확도) | asc(가격 낮은순) | dsc | date.
    반환 items[].url을 check_product(product_url=…)에 넘길 것.
    """
    items = await naver.search(query, max_results=max_results, sort=sort)
    return {"query": query, "count": len(items), "items": items}


# ---------- 검증 ----------

_WS = re.compile(r"\s+")


async def _fetch_text(url: str, limit: int = 6000) -> tuple[str, str]:
    """상품 페이지 본문 텍스트(요약)와 최종 URL. 실패하면 빈 문자열."""
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (hyojason-bot)"}) as c:
            r = await c.get(url)
            final = str(r.url)
            soup = BeautifulSoup(r.text, "html.parser")
            for t in soup(["script", "style", "noscript"]):
                t.decompose()
            text = _WS.sub(" ", soup.get_text(" "))
            return text[:limit], final
    except Exception:
        return "", url


@mcp.tool()
async def check_product(product_url: str, title: str = "", mall: str = "",
                        deadline: str = "", forbidden_terms_json: str = "[]") -> dict[str, Any]:
    """product_url에는 search_products가 돌려준 url을 그대로 넣는다. product_id가 아니다.
    상품 하나를 검증한다. deadline 은 '이번 주 안', '수요일까지', '2026-11-12' 등.
    반환: in_stock / domestic_shipping / arrives_by_deadline / violates_profile (yes|no|unknown), note, buy_url
    """
    forbidden = json.loads(forbidden_terms_json or "[]")
    dl = gates.parse_deadline(deadline) if deadline else None
    page_text, final_url = await _fetch_text(product_url)
    evidence = f"{title} {mall} {page_text}"

    result, engine = gates.decide_all(evidence, deadline=dl, forbidden=forbidden)

    notes = []
    if result["in_stock"] == "no":
        notes.append("품절 표시가 있어요")
    if result["domestic_shipping"] == "no":
        notes.append("외국에서 오는 상품이라 2주쯤 걸려요" if dl is None else "외국 발송이라 기한 안에 못 와요")
    if result["violates_profile"] == "yes":
        hit = [t for t in forbidden if t and t.lower() in evidence.lower()]
        notes.append(f"피하시는 조건에 해당: {', '.join(hit)}")
    if not page_text:
        notes.append("상품 페이지를 읽지 못해 제목·판매처만으로 판단했어요")
    if result["in_stock"] == "unknown" and page_text:
        result["in_stock"] = "yes"  # 품절 표시가 없으면 구매 가능으로 간주 (근거는 note 에)
        notes.append("품절 표시 없음")

    passed = (result["in_stock"] != "no"
              and result["violates_profile"] != "yes"
              and (dl is None or result["arrives_by_deadline"] != "no")
              and (dl is None or result["domestic_shipping"] != "no"))

    return {
        **result,
        "passed": passed,
        "deadline_parsed": dl.isoformat() if dl else None,
        "note": " / ".join(notes) or "특이사항 없음",
        "buy_url": final_url,
        "evidence_chars": len(page_text),
        "gate_engine": engine,  # 'jev' | 'rules'
    }


if __name__ == "__main__":
    mcp.run()
