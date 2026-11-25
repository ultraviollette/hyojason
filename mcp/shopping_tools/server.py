"""효자손 MCP 서버.

도구:
  resolve_client / get_client_profile / save_client_profile(정해진 필드만, 저장·수정 겸용)
  make_search_links        정확한 상품명 → 네이버쇼핑 검색 링크 + 쿠팡 앱 검색어 (페이지는 읽지 않음)
  search_products          provider 구조: 네이버 (키 있을 때만, 2026-07-31 종료). 없으면 웹 검색 안내
  coupang_search           쿠팡 파트너스 검색 → 같은 상품(pageKey) 묶음 + 답 규칙 (주로 사는 곳에 쿠팡이 있는 분만)
  check_product            사용자가 보낸 링크 하나를 읽고 Jev 게이트(재고·국내발송·기한·금지조건) 통과 여부
  check_candidates         웹 검색 후보 최대 3개를 동시에 읽고 낼지(show)·붙일 말(say)을 정함
  record_item              찾아 드린(found)·고르신(chosen) 물건 기록 (칸·50건 상한을 코드로 강제)

실행:  uv run --project mcp python -m shopping_tools.server   (stdio)
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from bs4 import BeautifulSoup
try:  # mcp SDK 2.x
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from . import coupang_partners, gates, naver, profiles

mcp = _Server("hyojason-shopping-tools")

# HTTP 라이브러리(httpx·httpcore)는 INFO 로 요청 주소를 남긴다. 쿠팡 검색 주소에는 검색어가, 후보 확인 주소에는
# 상품 주소가 들어간다(11/25 mcp-stderr.log 에서 확인). 경고 이상만 남긴다.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)


# ---------- 클라이언트 프로필 ----------

def _view_response(cid: str | None) -> tuple[dict[str, Any] | None, int]:
    return profiles.agent_view(profiles.get_profile(cid) if cid else None)


# 주로 사는 곳에 쿠팡이 있을 때 후보 답에 걸 규칙. 23e409b 실사용: "쿠팡도 좋고 네이버도 좋아"라고 답했는데
# 그 뒤 후보에 쿠팡 검색어 줄이 없었다. 글 규칙("쿠팡이면 …")에만 맡기지 않고 도구 응답에 붙인다(ask_payment_note 와 같은 방식).
# 쿠팡 파트너스 검색을 쓸 수 없을 때(키 또는 COUPANG_PARTNERS_VERIFIED 가 없음)의 옛 규칙. 쓸 수 있으면 COUPANG_SEARCH_FIRST_NOTE.
COUPANG_FIRST_NOTE = ("주로 사는 곳에 쿠팡이 있다. 후보를 드리는 답에서는 make_search_links(정확한 상품명)의 coupang_query 로 "
                      "\"쿠팡은 제가 직접 볼 수 없어서 검색어를 드려요. 쿠팡 앱에서는 '<coupang_query>'로 검색해 보세요\" 줄을 "
                      "답 맨 앞에 두고, 다른 곳 후보를 이어서 보여 준다. '쿠팡에서는 못 찾았어요'라고 말하지 않는다.")


# 주로 사는 곳에 네이버가 있을 때(사용자 결정 A, 2026-11-20). 효자손은 네이버 상품 페이지 본문을 읽지 못하므로
# 웹 검색 결과에 나온 스마트스토어·브랜드스토어 상품 페이지를 앞에 두는 것까지만 한다.
NAVER_FIRST_NOTE = ("주로 사는 곳에 네이버가 있다. 웹 검색 후보 중 네이버 스마트스토어(smartstore.naver.com)·"
                    "브랜드스토어(brand.naver.com) 상품 페이지를 다른 가게 후보보다 앞에 둔다(후보 번호도 앞 번호). "
                    "검색 결과 페이지는 후보가 아니다. 웹 검색에 네이버 상품 페이지가 없으면 다른 가게 후보를 그대로 보여 주고, "
                    "네이버를 직접 확인한 것이 아니므로 '네이버에서는 못 찾았어요'라고 말하지 않는다.")


# 쿠팡 검색어 순서(2026-11-25 사용자 결정, 같은 날 실사용 반영). 한 물건에 coupang_search 최대 3번.
# 실사용: "메구리즘 유자향"을 "메구리즘 유자"로 줄여 불렀더니 채움 결과(검색 결과 없음)가 왔다. "메구리즘 유자향"·
# "스팀 아이마스크 유자"는 정상 결과였다. 쿠팡 이름은 "멕리듬 …"이라 브랜드 표기가 달랐다.
# "R.LUX", "알럭스" 같은 유통 관련 말을 더하면 무관한 상품이 섞였다(용량·색·유통 관련 말은 넣지 않는다).
MAX_COUPANG_ATTEMPTS = 3
COUPANG_QUERY_ORDER = ("검색어 순서(한 물건에 coupang_search 최대 3번, attempt=1·2·3): "
                       "① 브랜드 + 사용자가 말한 특징을 말 그대로(예: '메구리즘 유자향'. '유자향'을 '유자'로 줄이지 않는다) "
                       "② 브랜드를 빼고 종류 + 특징(예: '스팀 아이마스크 유자향') ③ 브랜드만(예: '메구리즘'). "
                       "특징은 향·라인·시리즈 이름처럼 제품을 가르는 말이다. 용량·색·유통 관련 말(정품, 공식, 유통사 이름)은 넣지 않는다. "
                       "앞에서 쓴 검색어와 같아지거나 넣을 말이 없는 단계는 건너뛴다.")
# 쿠팡 파트너스 검색이 켜져 있을 때(2026-11-25 사용자 결정).
COUPANG_SEARCH_FIRST_NOTE = ("주로 사는 곳에 쿠팡이 있다. 후보를 찾을 때 웹 검색보다 먼저 coupang_search 를 부른다. "
                             + COUPANG_QUERY_ORDER +
                             " 그 응답의 rules·next_step 을 따른다. 세 번 다 맞는 것이 없을 때만 웹 검색으로 간다.")


def _place_rules(profile: dict[str, Any] | None) -> list[str]:
    rules = []
    if profiles.has_coupang(profile):
        rules.append(COUPANG_SEARCH_FIRST_NOTE if coupang_partners.is_enabled() else COUPANG_FIRST_NOTE)
    if profiles.has_naver(profile):
        rules.append(NAVER_FIRST_NOTE)
    return rules


# f3f5b0c 실사용: 확인 질문 앞에 "사진을 확인했습니다. 베이지색 털 귀마개네요. 프로필에 비슷한 걸 찾아 드린 기록이 있습니다."가
# 붙었다(합쇼체, 내부 용어, 군말). 확인 문장처럼 도구 응답에 싣는다.
CONFIRM_NOTE = ("지난 물건을 확인할 때는 답의 첫 글자부터 그 item 의 confirm_phrase 를 글자 그대로 쓴다. 그 앞에 사진 설명"
                "('사진을 확인했어요', '○○네요')이나 기록 이야기를 붙이지 않는다. '프로필', '기록', '저장', '도구' 같은 말은 "
                "사용자에게 하지 않는다. 말투는 해요체('~해요', '~세요')만 쓰고 '~습니다', '~합니다'는 쓰지 않는다.")


def _with_place_rules(out: dict[str, Any], profile: dict[str, Any] | None) -> dict[str, Any]:
    rules = _place_rules(profile)
    if rules:
        out["place_rules"] = rules
    if profile and profile.get("items"):
        out["confirm_note"] = CONFIRM_NOTE
    return out


@mcp.tool()
def resolve_client(channel: str, channel_user_id: str) -> dict[str, Any]:
    """대화 시작 시 가장 먼저 부른다. 채널 + 사용자 ID 로 저장된 프로필을 찾는다.
    channel 은 "kakao", channel_user_id 는 메시지 앞 [channel=kakao user_id=...] 의 user_id 값이다.
    반환: found, client_id(record_item 에 쓸 값), profile, items_omitted, place_rules(있을 때만).
    profile.items 는 고르신 것(chosen) 전부 + 찾아 드린 것(found) 최근 10건만 담는다(최신이 앞).
    지난 기록을 확인할 때는 그 item 의 confirm_phrase 를 그대로 쓴다(고르심/같은 물건/비슷한 것 구분이 들어 있다).
    confirm_note 가 있으면 따른다(확인 질문은 바로 그 문장으로 시작한다).
    item.url 이 있으면 확인받은 뒤 다시 검색하지 않고 그 주소를 드린다(seen_price_krw 는 그 날짜에 본 가격).
    place_rules 가 있으면 후보를 드리는 답에서 반드시 따른다. items_omitted 는 빼고 돌려준 오래된 found 건수.
    found=false 면 처음 온 분이다."""
    cid = profiles.resolve_client_id(channel, channel_user_id)
    view, omitted = _view_response(cid)
    return _with_place_rules({"found": cid is not None, "client_id": cid, "profile": view, "items_omitted": omitted},
                             view)


@mcp.tool()
def get_client_profile(client_id: str) -> dict[str, Any]:
    """client_id 로 프로필을 읽는다. 없으면 exists=false. 보통은 resolve_client 를 쓴다.
    client_id 자리에 카카오 user_id 를 넣어도 찾아 준다. items 는 resolve_client 와 같은 방식으로 줄여 돌려준다."""
    cid = client_id if profiles.get_profile(client_id) is not None else profiles.resolve_client_id("kakao", client_id)
    view, omitted = _view_response(cid)
    return _with_place_rules({"exists": view is not None, "profile": view, "items_omitted": omitted}, view)


@mcp.tool()
def save_client_profile(channel: str, channel_user_id: str, display_name: str = "", top_size: str = "",
                        bottom_size: str = "", shoe_size_mm: str = "", height_cm: str = "", body_notes: str = "",
                        delivery_region: str = "", likes: list[str] | None = None,
                        dislikes: list[str] | None = None, brands_avoid: list[str] | None = None,
                        price_sensitivity: str = "", notes: list[str] | None = None,
                        payment_places: list[str] | None = None, payment_by: str = "",
                        payment_place: str = "") -> dict[str, Any]:
    """프로필을 저장하거나 고친다(같은 사용자면 합친다). 정해진 필드만 받는다. 다른 키는 만들지 않는다.
    channel="kakao", channel_user_id=<[channel=kakao user_id=...] 의 user_id> 는 필수.
    display_name: 부를 호칭(예: 어머니). top_size/bottom_size: 예 "55", "M". shoe_size_mm: 예 "235".
    delivery_region: 배송 지역(시·군 정도, 정확한 주소는 받지 않는다). likes/dislikes: 좋아하는 것/싫어하는 것 목록.
    payment_places: 주로 사는 곳 목록. 여러 곳을 말하면 모두 넣는다(가게마다 20자 이내, 5곳까지, 예: ["쿠팡", "네이버"]).
    새로 말하면 이전 목록을 바꾼다. payment_place 는 옛 한 줄 형식("쿠팡, 네이버")이며 나눠서 넣는다.
    payment_by: "self"(본인) | "family"(다른 가족).
    saved=false 면 저장되지 않은 것이다. 그때는 "기억해 둘게요"라고 말하지 않는다.
    결제 방식은 물어본 적이 있든 없든(asked_on), 사용자가 말하면 언제든 저장한다.
    결제 정보(카드·계좌)와 다른 사람 연락처는 받지 않는다.
    notes: 위에 없는 기타 메모 목록(예: "라인댄스를 함"). 빈 값은 기존 값을 지우지 않고, 목록은 합친다."""
    try:
        profile, created = profiles.upsert_profile(
            channel, channel_user_id, display_name=display_name, top_size=top_size, bottom_size=bottom_size,
            shoe_size_mm=shoe_size_mm, height_cm=height_cm, body_notes=body_notes, delivery_region=delivery_region,
            likes=likes, dislikes=dislikes, brands_avoid=brands_avoid, price_sensitivity=price_sensitivity,
            notes=notes, payment_place=payment_place, payment_by=payment_by, payment_places=payment_places)
    except ValueError as e:
        return {"saved": False, "error": str(e)}
    view, _ = profiles.agent_view(profile)
    return _with_place_rules({"saved": True, "created": created, "client_id": profile["client_id"], "profile": view},
                             view)


@mcp.tool()
def record_item(client_id: str, status: str, name: str, brand: str = "", options: dict[str, str] | None = None,
                seller: str = "", seen_price_krw: int | None = None, url: str = "",
                match: str = "") -> dict[str, Any]:
    """찾아 드린 물건(status="found")과 사용자가 고른 물건(status="chosen")을 프로필에 남긴다. client_id 는 resolve_client 가 준 값.
    name: 제품 이름(필수). brand: 브랜드(모르면 비움). options: 그 물건에서 갈리는 것 {"용량": "240ml", "색": "네이비"} (6개까지).
    seller: 판매처 이름. seen_price_krw: 그때 본 실제 판매가(원, 줄 그어진 정가가 아님). 적혀 있던 경우만, 모르면 비운다.
    url: 답에 링크로 드린 상품 페이지 주소(도구·검색 결과가 준 그대로). 검색 결과 페이지 주소는 넣지 않는다.
    크라우드펀딩(와디즈·텀블벅) 주소는 거절된다(후보가 아니다).
    쿠팡 파트너스 주소(link.coupang.com)도 거절된다(약관상 저장 금지). 쿠팡 상품은 url·seen_price_krw 없이,
    name 에는 사용자가 말한 종류, seller="쿠팡"으로 남긴다. 쿠팡에서 받은 상품 이름·가격은 넣지 않는다.
    match: found 일 때 반드시 채운다. "same" = 찾던 그 물건으로 드림(브랜드·제품명이 확인된 경우),
    "similar" = "똑같은 건 못 찾았고 비슷한 것"·"닮은 제품"으로 드림(브랜드를 모르는 사진이면 항상 similar).
    결제했는지는 알 수 없으므로 기록하지 않는다. 날짜는 서버가 채운다.
    같은 날 같은 브랜드·이름·옵션의 found 를 chosen 으로 남기면 그 항목이 chosen 으로 바뀐다. 최근 50건까지 보관.
    ask_payment 가 true 일 때만 이번 답 끝에 결제 방식 질문을 붙인다. 도구가 평생 한 번만 true 를 준다.
    결제 질문은 후보와 링크를 모두 쓴 답의 맨 끝에만 붙인다. 결제 질문만 있는 답은 쓰지 않는다(ask_payment_note)."""
    try:
        item, ask_payment = profiles.record_item(client_id, status=status, name=name, brand=brand, options=options,
                                                 seller=seller, seen_price_krw=seen_price_krw, url=url,
                                                 match=match)
    except ValueError as e:
        return {"recorded": False, "error": str(e), "ask_payment": False}
    out = {"recorded": True, "item": item, "ask_payment": ask_payment}
    if ask_payment:  # 11/20 실사용: 후보를 기록하고도 답에 결제 질문만 써서 후보가 사라졌다
        out["ask_payment_note"] = ASK_PAYMENT_NOTE
    if status == "found":
        out = _with_place_rules(out, profiles.get_profile(client_id))
    return out


# ---------- 검색 ----------

# 네이버 쇼핑 검색 API 는 2026-07-31 종료(네이버 개발자센터 공지, 사용자 확인). 이 경로는 지우지 않고 남겨 둔다.
# 키가 없으면 has_credentials 가 False 라 호출하지 않는다.
async def _naver_provider(query: str, max_results: int, sort: str) -> dict[str, Any]:
    return {"items": await naver.search(query, max_results=max_results, sort=sort), "error": None}


# (이름, 키가 있는지, 검색 함수). 키가 있는 provider 만 순서대로 시도한다. 키 없는 provider 는 요청하지 않는다.
# 쿠팡 임시 스크레이퍼(coupang_scrape)는 키 없이 쿠팡에 자동 요청을 보내고 매번 403 이라 기본 경로에서 뺐다.
# 쿠팡 파트너스는 여기서 뺐다(2026-11-25): 여기 두면 쿠팡을 쓰지 않는 분에게도 쿠팡 결과가 나가고 호출 한도도 두 번 쓴다.
# 쿠팡은 주로 사는 곳에 쿠팡이 있는 분에게만 coupang_search 로 찾는다.
SEARCH_PROVIDERS = (
    ("naver_shopping", naver.has_credentials, _naver_provider),            # 2026-07-31 종료된 API (naver.py 참고)
)


@mcp.tool()
async def search_products(query: str, max_results: int = 20, sort: str = "sim") -> dict[str, Any]:
    """상품을 찾는다. sort: sim(정확도) | asc(가격 낮은순) | dsc | date (네이버에만 적용).
    반환 items[].url을 check_product(product_url=…)에 넘길 것.
    provider: 네이버(키 있을 때). 쿠팡은 여기서 찾지 않는다(coupang_search).
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
            return {"query": query, "count": len(items), "items": items, "provider": name}
        tried.append({"provider": name, "error": r.get("error") or "no_results"})
    return {"query": query, "count": 0, "items": [], "fallback": "web_search", "next_step": NO_RESULT_NEXT_STEP,
            "tried": tried}


NO_RESULT_NEXT_STEP = (
    "검색 API 결과가 없다. 여기서 끝내거나 링크를 달라고 하지 말고, web_search 로 상품 페이지 후보를 찾는다(SKILL 3-b). "
    "상품 페이지만 최대 3개(검색 결과·블로그·광고·쿠팡 링크 제외), 후보마다 검색 결과에서 확인된 사실로 한 줄, "
    "가격·재고·도착일은 보인 것만 말하고 '링크에서 꼭 확인하세요'. 하나도 못 찾았을 때만 못 찾았다고 말한다."
)


# ---------- 쿠팡 파트너스 검색 ----------

# 답 규칙(사용자 결정, 2026-11-25). 글 규칙만으로는 잘 안 지켜져 도구 응답에 싣는다(ask_payment_note 와 같은 방식).
COUPANG_RULES = [
    "답의 맨 첫 줄은 disclosure 문장을 글자 그대로 쓴다. 다른 어떤 말보다 앞에 두고, 조건을 붙이거나 줄이지 않는다.",
    "거르기는 상품 name 으로 한다. 브랜드를 말한 요청이면 그 브랜드의 상품만 남긴다. 다른 표기(음차, 한국 판매명, 영문)로 "
    "적힌 같은 브랜드는 같은 브랜드로 본다(예: '메구리즘'과 '멕리듬'). 표기가 다르면 답에 한 줄로 밝힌다"
    "(예: \"쿠팡에서는 '멕리듬'이라는 이름으로 팔아요\"). 같은 브랜드인지 확신이 없으면 버리지 않고 그 후보에 "
    "'같은 브랜드인지 확인해 주세요'를 붙인다. 말하지 않은 세트 상품은 버린다. "
    "향·라인 이름이 다르면 다른 제품이다(예: '버베인 아그륌', '시트러스 버베나'는 '버베나'와 다른 향).",
    "용량·색·향 같은 조건은 사용자가 말한 값이 name 에 그 값 하나로 적혀 있을 때만 맞다고 말한다. name 에 여러 값이 "
    "나열돼 있으면 아래 '옵션이 나열된 상품' 규칙대로 사진으로 확인한다. name 에 아예 없을 때만 '용량은 들어가서 골라 주세요'처럼 말한다.",
    "옵션이 나열된 상품: name 에 옵션이 여러 개 나열돼 있으면(택1, a/b/c 꼴) 같은 상품의 줄(options)마다 다른 옵션이다. "
    "줄의 url 은 그 옵션 하나의 페이지로 가고 그 페이지에서 다른 옵션으로 바꿀 수 없다. name 에 나열된 옵션이 options 에 다 오지도 않는다. "
    "사용자가 그중 하나를 말했으면 이름만으로 후보로 삼지 않는다. options 의 image 를 하나씩 비전 도구로 보고(한 물건에 최대 5장) "
    "사용자가 말한 옵션인 줄을 찾는다. 찾으면 그 줄의 url 과 price_krw 로 후보를 내고 가격은 그 값 하나로 '약 …원'이라고 말한다"
    "(대표 줄·rocket_option 보다 이 줄이 먼저다). 찾지 못했거나 사진으로 알 수 없으면 그 상품은 후보에서 빼고 "
    "'들어가서 골라 주세요'라고 하지 않는다.",
    "옵션이 나열된 상품인데 사용자가 옵션을 말하지 않았으면, 링크로 낼 줄(대표 줄 또는 rocket_option)의 image 를 보고 "
    "그 링크가 어느 옵션인지 한 줄로 밝힌다(예: '이 링크는 숲속향이에요. 다른 향은 말씀해 주세요'). 사진으로 알 수 없으면 "
    "'이 링크의 향은 들어가서 확인해 주세요'. '골라 주세요'라고 하지 않는다. 이 사진도 한 물건에 최대 5장에 센다.",
    "브랜드를 짚어 말한 물건(화장품 등)은 값보다 유통 경로가 먼저다. is_rocket 인 상품을 앞에 두고, 대표 줄이 로켓이 아니고 "
    "rocket_option 이 있으면 rocket_option(이름·가격·주소)을 고른다. 로켓이 아닌 후보에는 '판매자와 배송을 들어가서 확인해 주세요'를 붙인다. "
    "'공식', '정품', 유통사 이름을 확인한 것처럼 말하지 않는다. 물으시면 '공식 유통인지는 제가 확인하지 못했어요'.",
    "브랜드가 중요하지 않은 물건은 조건에 맞는 것 중 싼 순서로 둔다.",
    "후보는 최대 3개, 상품마다 '제품 이름 · 옵션 · 가격' 한 줄. 가격은 '지금 약 19,000원, 들어가시면 달라질 수 있어요'처럼 "
    "확정하지 않는다. price_varies 가 true 면 '약 19,000원부터, 옵션에 따라 달라요'로 말한다. 단, 옵션이 나열된 상품은 "
    "링크로 낸 줄의 price_krw 하나로 '약 …원'이라고 말한다(그 링크는 그 옵션 하나의 페이지다).",
    "is_rocket 이면 '로켓배송'이라고만 하고 배송비는 단정하지 않는다. '무료배송'은 is_free_shipping 이 true 일 때만 말한다. "
    "둘 다 아니면 '배송비가 더 붙을 수 있어요'.",
    "링크 줄은 'N번 쿠팡: url' 로 쓰고 url 은 글자 하나 바꾸지 않는다(카카오에서 '쿠팡에서 보기' 버튼이 된다). "
    "쿠팡 링크에는 check_candidates 를 부르지 않는다.",
    "record_item 에는 쿠팡에서 받은 이름·가격·링크를 넣지 않는다. name 은 사용자가 말한 종류, brand·options 는 사용자가 말한 것, "
    "seller='쿠팡', url·seen_price_krw 는 비운다. 쿠팡 기록은 다음에 다시 물으시면 다시 검색한다.",
    "거른 뒤 맞는 상품이 하나도 없으면 바로 웹 검색으로 가지 않는다. 이 응답의 next_if_no_match 를 따른다"
    "(검색어 순서대로 다시 찾기, 한 물건에 최대 3번).",
]
# 다시 찾기(attempt 1·2 에서 없음) / 세 번 다 없음. 채움 결과(검색 결과 없음)도 empty 로 온다.
COUPANG_RETRY_NEXT = ("쿠팡에서 맞는 상품을 아직 못 찾았다. 아직 웹 검색으로 가지 않고 사용자에게도 말하지 않는다. "
                      "검색어를 다음 단계로 바꿔 coupang_search 를 attempt={next} 로 다시 부른다. " + COUPANG_QUERY_ORDER)
COUPANG_STOP_NEXT = ("쿠팡에서 검색어를 바꿔 가며 찾아봤는데 맞는 상품이 없다. 더 부르지 않는다. 웹 검색(SKILL 3-b)으로 찾고, "
                     "답은 '쿠팡에서는 제가 찾는 방법으로는 못 찾았어요'로 시작한다. 쿠팡 검색어 줄은 드리지 않는다. "
                     "쿠팡 링크가 없으면 대가성 문구도 넣지 않는다.")


def _no_match_next(attempt: int) -> str:
    return COUPANG_STOP_NEXT if attempt >= MAX_COUPANG_ATTEMPTS else COUPANG_RETRY_NEXT.format(next=attempt + 1)
# 사용 불가(키·스위치 없음, 한도, 시간 초과, 오류)면 지금 흐름 그대로. 사용자에게는 말하지 않는다.
COUPANG_UNAVAILABLE_NEXT = ("지금은 쓸 수 없다. 이 일은 사용자에게 말하지 않는다. 지금까지의 흐름 그대로 한다: "
                            + COUPANG_FIRST_NOTE)


def _group_products(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """같은 pageKey(같은 상품, itemId 가 다른 옵션 줄)를 한 상품으로 묶는다. 순서는 처음 나온 줄 순서.
    대표 값(이름·로켓·무료배송·주소)은 가장 싼 줄 것이다. 링크가 그 줄이라 설명과 링크가 맞는다.
    options: 묶음의 줄 전체(처음 나온 순서). 11/25 실사용: pageKey 가 같은 줄은 같은 상품의 옵션이지만 줄의 url 은 그 옵션
    하나의 페이지로 가고(거기서 다른 옵션으로 못 바꿈), 이름에 나열된 옵션이 응답에 다 오지도 않았다. 줄의 image 로 옵션을
    구분할 수 있었다. 그래서 에이전트가 사진으로 맞는 줄을 고를 수 있게 줄마다 url·image 를 그대로 준다.
    옵션 글자가 이름에 있는지는 추정하지 않는다("…-200ml-가든화이트"처럼 틀리는 이름이 있다). 에이전트가 name 을 읽는다."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(r["page_key"], []).append(r)
    out = []
    for g in groups.values():
        cheapest = min(g, key=lambda r: r["price_krw"])          # 값이 같으면 앞 줄
        prices = [r["price_krw"] for r in g]
        p = {"name": cheapest["name"], "price_min_krw": min(prices), "price_max_krw": max(prices),
             "price_varies": len(set(prices)) > 1, "row_count": len(g), "is_rocket": cheapest["is_rocket"],
             "is_free_shipping": cheapest["is_free_shipping"], "url": cheapest["url"],
             "options": [{k: r[k] for k in ("price_krw", "is_rocket", "is_free_shipping", "url", "image")} for r in g]}
        rockets = [r for r in g if r["is_rocket"]]
        if not cheapest["is_rocket"] and rockets:
            ro = min(rockets, key=lambda r: r["price_krw"])
            p["rocket_option"] = {"name": ro["name"], "price_krw": ro["price_krw"],
                                  "is_free_shipping": ro["is_free_shipping"], "url": ro["url"]}
        out.append(p)
    return out


@mcp.tool()
async def coupang_search(keyword: str, attempt: int = 1) -> dict[str, Any]:
    """쿠팡 파트너스 검색. place_rules 가 coupang_search 를 먼저 부르라고 할 때만 쓴다(주로 사는 곳에 쿠팡이 있는 분).
    한 물건에 최대 3번. attempt 는 이번 물건에서 몇 번째 부르는지(1·2·3).
    keyword 순서: ① 브랜드 + 사용자가 말한 특징을 말 그대로(예: '메구리즘 유자향') ② 브랜드를 빼고 종류 + 특징
    ③ 브랜드만. 용량·색·유통 관련 말(정품, 공식, 유통사 이름)은 넣지 않는다.
    반환 status: "ok"(products 있음, disclosure·rules 를 따르고 맞는 것이 없으면 next_if_no_match) |
    "empty"(찾아봤는데 없음, next_step) | "unavailable"(지금 쓸 수 없음, next_step. 사용자에게 말하지 않는다).
    products[]: 같은 상품의 옵션 줄을 묶은 것. name, price_min_krw~price_max_krw, price_varies, row_count,
    is_rocket, is_free_shipping, url(가장 싼 줄, 글자 그대로 쓴다), rocket_option(가장 싼 줄이 로켓이 아니고 로켓 줄이 있을 때),
    options(줄 전체: price_krw, is_rocket, is_free_shipping, url, image. 줄마다 다른 옵션 페이지로 간다).
    가격은 배송비를 뺀 값이다. 판매자·품절·해외배송 정보는 없다."""
    kw = _SPACES.sub(" ", keyword or "").strip()
    try:
        attempt = max(int(attempt), 1)
    except (TypeError, ValueError):
        attempt = 1
    if attempt > MAX_COUPANG_ATTEMPTS:   # 한 물건에 3번까지. 넘으면 요청하지 않는다
        return {"status": "empty", "keyword": kw, "attempt": attempt, "products": [], "next_step": COUPANG_STOP_NEXT}
    if not kw:
        return {"status": "unavailable", "products": [], "next_step": COUPANG_UNAVAILABLE_NEXT}
    r = await coupang_partners.search(kw)
    if r["status"] == "unavailable":
        return {"status": "unavailable", "products": [], "next_step": COUPANG_UNAVAILABLE_NEXT}
    if r["status"] == "empty":
        return {"status": "empty", "keyword": kw, "attempt": attempt, "products": [],
                "next_step": _no_match_next(attempt)}
    return {"status": "ok", "keyword": kw, "attempt": attempt, "products": _group_products(r["rows"]),
            "disclosure": coupang_partners.DISCLOSURE, "rules": COUPANG_RULES,
            "next_if_no_match": _no_match_next(attempt)}


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


_FEE = re.compile(r"배송비\s*[:：]?\s*(무료|\d{1,3}(?:,\d{3})+\s*원|\d+\s*원)")
_FREE_SHIP = re.compile(r"무료\s*배송")


def _shipping_fee(text: str) -> tuple[int | None, bool]:
    """본문에서 배송비를 뽑는다. 반환: (원, 조건부 무료인지). 0 은 무료, None 은 모름.
    "배송비 X" 값이 하나로 정해질 때만 그 값을 쓴다. 값이 여럿이면(옵션·지역별) 모름으로 둔다.
    값이 있는데 "무료배송" 문구도 있으면 "5만원 이상 무료" 같은 조건부로 본다."""
    fees = {0 if m.group(1) == "무료" else int(re.sub(r"\D", "", m.group(1))) for m in _FEE.finditer(text or "")}
    if len(fees) == 1:
        fee = fees.pop()
        return fee, fee > 0 and bool(_FREE_SHIP.search(text))
    if not fees and _FREE_SHIP.search(text or ""):
        return 0, False
    return None, False


async def _check_one(product_url: str, title: str, mall: str, dl, forbidden: list[str],
                     seen_fee: int | None = None) -> dict[str, Any]:
    """상품 페이지 하나를 한 번 읽고(GET 1번, 다시 시도·다른 페이지 따라가기 없음) 판정한다."""
    page = await _fetch_page(product_url)
    page_text = page.text
    evidence = f"{title} {mall} {page_text}"

    # Jev 는 동기 호출이라 여러 후보를 동시에 볼 때 서로 막지 않게 스레드에서 돌린다
    result, engine = await asyncio.to_thread(gates.decide_all, evidence, deadline=dl, forbidden=forbidden)

    # 재고는 페이지에 근거(품절 표시 또는 구매 버튼 문구)가 있을 때만 판정한다.
    unreadable = page.unreadable_reason
    if unreadable is None and not (gates._OUT_OF_STOCK.search(page_text) or _BUYABLE.search(page_text)):
        unreadable = "재고 표시를 찾지 못함"
    if unreadable is not None:
        result["in_stock"] = "unknown"
    elif result["in_stock"] == "unknown" and _BUYABLE.search(page_text):
        result["in_stock"] = "yes"

    kids = _KIDS.search(f"{title} {page.page_title}")
    fee, fee_conditional = _shipping_fee(page_text)
    if fee is None and seen_fee is not None:   # 페이지에서 못 읽었으면 검색 결과에서 본 배송비를 쓴다
        fee = seen_fee

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

    verdict, say = _candidate_verdict(result, unreadable, dl, fee, fee_conditional, kids is not None)
    return {
        **result,
        "passed": passed,
        "readable": unreadable is None,
        "unreadable_reason": unreadable,
        "kids_item": kids is not None,
        "shipping_fee_krw": fee,
        "verdict": verdict,
        "say": say,
        "http_status": page.status,
        "deadline_parsed": dl.isoformat() if dl else None,
        "note": " / ".join(notes) or "특이사항 없음",
        "buy_url": page.final_url,
        "evidence_chars": len(page_text),
        "gate_engine": engine,  # 'jev' | 'rules'
    }


OVERSEAS_SAY = "해외배송 · 외국에서 오는 거라 2주쯤 걸려요"
UNVERIFIED_SAY = "재고와 배송비는 확인하지 못했어요"


def _fee_text(fee: int, conditional: bool) -> str:
    if fee == 0:
        return "무료배송"
    return f"배송비 {fee:,}원 따로" + (" (조건에 따라 무료일 수 있어요)" if conditional else "")


def _say_line(*, overseas: bool, kids: bool, stock_unknown: bool, fee: int | None, fee_conditional: bool) -> str:
    """후보 줄 아래에 붙일 한 줄. 읽은 것과 못 읽은 것을 따로 말한다.
    8316893 실사용: "배송비 5,500원 따로" 바로 아래에 "재고와 배송비는 확인하지 못했어요"가 붙어 서로 어긋났다.
    배송비를 알면(페이지 또는 검색 결과) 배송비를 확인하지 못했다고 말하지 않는다."""
    parts = []
    if overseas:
        parts.append(OVERSEAS_SAY)
    if kids:
        parts.append("아동용 상품이에요")
    if fee is not None:
        parts.append(_fee_text(fee, fee_conditional))
    if stock_unknown and fee is None:
        parts.append(UNVERIFIED_SAY)
    elif stock_unknown:
        parts.append("재고는 확인하지 못했어요")
    elif fee is None:
        parts.append("배송비는 확인하지 못했어요")
    return " · ".join(parts)


def _candidate_verdict(result: dict[str, Any], unreadable: str | None, dl, fee: int | None,
                       fee_conditional: bool, kids: bool) -> tuple[str, str]:
    """후보로 낼지와 그 후보에 붙일 말(한 줄)을 코드가 정한다(글 규칙만으로는 실사용에서 해외직구를 밝히지 않았다).
    verdict: ok | unverified(못 읽음, 내되 밝힘) | overseas(다른 후보가 없을 때만) |
             drop_sold_out | drop_profile | drop_deadline"""
    if result["in_stock"] == "no":
        return "drop_sold_out", ""
    if result["violates_profile"] == "yes":
        return "drop_profile", ""
    if dl is not None and (result["arrives_by_deadline"] == "no" or result["domestic_shipping"] == "no"):
        return "drop_deadline", ""
    overseas = result["domestic_shipping"] == "no"
    stock_unknown = result["in_stock"] == "unknown"
    verdict = "overseas" if overseas else ("unverified" if unreadable is not None or stock_unknown else "ok")
    return verdict, _say_line(overseas=overseas, kids=kids, stock_unknown=stock_unknown, fee=fee,
                              fee_conditional=fee_conditional)


@mcp.tool()
async def check_product(product_url: str, title: str = "", mall: str = "",
                        deadline: str = "", forbidden_terms_json: str = "[]") -> dict[str, Any]:
    """사용자가 보낸 링크 하나를 검증할 때 쓴다(SKILL 1-b). 검색으로 찾은 후보는 check_candidates 로 한 번에 본다.
    product_url에는 사용자가 보낸 주소(또는 search_products 가 돌려준 url)를 그대로 넣는다. product_id가 아니다.
    deadline 은 '이번 주 안', '수요일까지', '2026-11-12' 등.
    반환: in_stock / domestic_shipping / arrives_by_deadline / violates_profile (yes|no|unknown), note, buy_url,
          readable(페이지를 읽었는지), unreadable_reason, kids_item(상품명에 아동/키즈/주니어),
          shipping_fee_krw(0 무료, null 모름), verdict, say(그 상품 줄 아래에 붙일 한 줄).
    readable=false 이면 in_stock 은 unknown 이고 passed 는 false 다. 이때 '품절'이라고 말하면 안 된다.
    """
    forbidden = json.loads(forbidden_terms_json or "[]")
    dl = gates.parse_deadline(deadline) if deadline else None
    return await _check_one(product_url, title, mall, dl, forbidden)


# 후보 검증 상한. 후보마다 상품 페이지를 GET 한 번 읽는다(사용자 요청에 대한 답으로 보여 줄 페이지만).
# 동시에 읽으므로 걸리는 시간은 가장 느린 페이지 하나 + 도구 호출 한 번이다. 카카오 콜백 240초 안에 넣으려고 상한을 둔다.
MAX_CHECK = 3
CHECK_BUDGET_S = 8   # 8316893 실사용: 한 건 163초(전 약 120초). 위험을 줄이려고 15 → 8
_NO_FETCH_HOSTS = ("coupang.com",)  # 쿠팡에는 요청하지 않는다(카카오에서 링크도 막혀 후보가 아니다)

CHECK_CANDIDATES_NOTE = (
    "show=true 인 후보만 답에 낸다(순서는 그대로). 각 후보의 say 한 줄을 그 후보의 '제품 · 옵션 · 가격' 줄 바로 아래에 "
    "글자 그대로 쓴다. say 에 없는 재고·배송비 말을 따로 덧붙이지 않는다(검색 결과에서 본 배송비는 candidates 의 "
    "shipping_fee_krw 로 넘기면 say 에 들어간다). verdict=overseas 는 다른 후보가 없을 때만 show=true 이고, say 가 "
    "'해외배송'으로 시작한다. drop_* 은 후보로 내지 않고, drop_reason 이 있으면 후보 목록 아래에 "
    "'✗ 쇼핑몰이름: drop_reason' 한 줄로 이유를 남긴다.")
# 탈락 이유 한 줄(SOUL "탈락한 것도 이유를 한 줄 남긴다"). 쿠팡·크라우드펀딩·잘못된 주소는 후보가 아니라 이유를 남기지 않는다.
DROP_REASONS = {"drop_sold_out": "품절이에요", "drop_profile": "피하시는 조건에 해당해요",
                "drop_deadline": "기한 안에 못 와요"}
# 한 요청에서 check_candidates 는 2번까지(사용자 결정, 2026-11-20). 240초 안에 넣으려는 것이다.
MAX_CHECK_ATTEMPTS = 2
NO_CANDIDATE_RETRY = "보여 드릴 후보가 없다. SKILL 3-c 대로 검색어를 바꿔 한 번 더 찾고, check_candidates 를 attempt=2 로 부른다."
NO_CANDIDATE_STOP = ("보여 드릴 후보가 없다. 이번 요청의 마지막 확인이다. 더 찾지 말고 '제가 찾는 방법으로는 못 찾았어요.'라고 "
                     "말한 뒤 다음 할 일 하나를 붙인다(SKILL 3-b 7단계).")

# 네이버 우선(NAVER_FIRST_NOTE)이 걸렸는지 보려는 개수 로그. 주소·사용자 ID·상품명은 남기지 않는다.
# stdio MCP 라 stdout 은 프로토콜이 쓴다. 로그는 stderr 로만 낸다.
_log = logging.getLogger("hyojason.check_candidates")
if not _log.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(message)s"))
    _log.addHandler(_h)
    _log.setLevel(logging.INFO)
    _log.propagate = False
_NAVER_PRODUCT_HOSTS = ("smartstore.naver.com", "brand.naver.com")
_COUNT_KEYS = (("ok", ("ok",)), ("unreadable", ("unverified",)), ("overseas", ("overseas",)),
               ("soldout", ("drop_sold_out",)), ("avoid", ("drop_profile",)), ("deadline", ("drop_deadline",)),
               ("skipped", ("drop_not_candidate", "drop_bad_url")))


def _log_line(attempt: int, naver_rule: str, results: list[dict[str, Any]]) -> str:
    counts = " ".join(f"{name}={sum(r['verdict'] in vs for r in results)}" for name, vs in _COUNT_KEYS)
    naver_hosts = sum(_host_in(r["url"], _NAVER_PRODUCT_HOSTS) for r in results)
    return (f"path=check_candidates attempt={attempt} n={len(results)} naver_rule={naver_rule} "
            f"naver_hosts={naver_hosts} shown={sum(r['show'] for r in results)} {counts}")


def _seen_fee(v: Any) -> int | None:
    """검색 결과에서 본 배송비(에이전트가 넘긴 값). 숫자로 읽히지 않으면 모름."""
    digits = re.sub(r"[^\d]", "", str(v if v is not None else ""))
    return int(digits) if digits else None


def _host_in(url: str, domains: tuple[str, ...]) -> bool:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in domains)


@mcp.tool()
async def check_candidates(candidates: list[dict[str, Any]], deadline: str = "",
                           forbidden_terms_json: str = "[]", client_id: str = "",
                           attempt: int = 1) -> dict[str, Any]:
    """웹 검색으로 고른 후보(상품 페이지)를 답에 내기 전에 한 번에 검증한다(SKILL 3-b, 1-c).
    candidates: [{"url": 상품 페이지 주소(검색 결과 그대로), "title": 검색 결과 제목, "mall": 쇼핑몰 이름,
                  "shipping_fee_krw": 검색 결과에 배송비가 보였을 때만 숫자(무료면 "0")}], 최대 3개.
    후보마다 그 상품 페이지를 한 번만 읽는다. 동시에 읽고 8초 안에 못 읽은 것은 '확인하지 못함'으로 돌려준다.
    client_id: resolve_client 가 준 값(있으면). attempt: 이번 요청에서 몇 번째 확인인지(1 또는 2). 한 요청에 2번까지만 부른다.
    반환 results[]: url, show(답에 낼지), verdict, say(그 후보 줄 아래에 붙일 한 줄, 글자 그대로), drop_reason(뺀 이유 한 줄),
    shipping_fee_krw, buy_url. note 와 next_step 을 따른다."""
    forbidden = json.loads(forbidden_terms_json or "[]")
    dl = gates.parse_deadline(deadline) if deadline else None

    async def one(c: dict[str, Any]) -> dict[str, Any]:
        url = str(c.get("url", "")).strip()
        if not re.match(r"^https?://", url, re.I):
            return {"url": url, "verdict": "drop_bad_url", "say": "", "checked": False}
        if profiles._is_blocked_url(url) or _host_in(url, _NO_FETCH_HOSTS):
            return {"url": url, "verdict": "drop_not_candidate", "say": "", "checked": False}
        seen_fee = _seen_fee(c.get("shipping_fee_krw"))
        try:
            r = await asyncio.wait_for(_check_one(url, c.get("title", ""), c.get("mall", ""), dl, forbidden,
                                                  seen_fee), CHECK_BUDGET_S)
        except TimeoutError:
            say = _say_line(overseas=False, kids=False, stock_unknown=True, fee=seen_fee, fee_conditional=False)
            return {"url": url, "verdict": "unverified", "say": say, "checked": False,
                    "unreadable_reason": "시간 안에 못 읽음", "buy_url": url, "shipping_fee_krw": seen_fee}
        return {"url": url, "checked": True, **{k: r[k] for k in (
            "verdict", "say", "shipping_fee_krw", "buy_url", "readable", "unreadable_reason", "in_stock",
            "domestic_shipping", "kids_item", "note")}}

    picked = list(candidates or [])[:MAX_CHECK]
    results = list(await asyncio.gather(*(one(c) for c in picked)))
    has_domestic = any(r["verdict"] in ("ok", "unverified") for r in results)
    for r in results:
        r["show"] = r["verdict"] in ("ok", "unverified") or (r["verdict"] == "overseas" and not has_domestic)
        if r["verdict"] in DROP_REASONS:
            r["drop_reason"] = DROP_REASONS[r["verdict"]]
    out = {"results": results, "note": CHECK_CANDIDATES_NOTE}
    if len(candidates or []) > MAX_CHECK:
        out["not_checked"] = len(candidates) - MAX_CHECK
    if not any(r["show"] for r in results):
        out["next_step"] = NO_CANDIDATE_STOP if attempt >= MAX_CHECK_ATTEMPTS else NO_CANDIDATE_RETRY
    profile = profiles.get_profile(client_id) if client_id else None
    _log.info(_log_line(attempt, "na" if profile is None else str(int(profiles.has_naver(profile))), results))
    return out


if __name__ == "__main__":
    mcp.run()
