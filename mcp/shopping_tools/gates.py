"""Jev yes/no 게이트.

Jev(System One)는 "질문 + 근거 텍스트 → yes/no" 를 LLM 호출보다 싸고 빠르게 처리한다.
후보가 20개면 LLM 20번 대신 Jev 20번을 돌리고, 살아남은 1~3개만 LLM 이 설명한다.

⚠️ Jev SDK 호출부(_jev_decide)는 강의에서 배운 실제 SDK 시그니처로 채워야 한다.
    JEV_API_KEY 가 없으면 규칙 기반 폴백(_rule_decide)으로 동작한다.
"""
from __future__ import annotations

import os
import re
from datetime import date, timedelta
from typing import Literal

Decision = Literal["yes", "no", "unknown"]

QUESTIONS = {
    "in_stock": "이 상품 페이지 내용으로 볼 때 지금 구매(주문) 가능한 재고가 있는가?",
    "domestic_shipping": "이 상품은 국내에서 발송되는가? (해외 직구·해외 배송이면 no)",
    "arrives_by_deadline": "이 상품은 주어진 기한({deadline})까지 도착할 수 있는가?",
    "violates_profile": "이 상품은 다음 금지 조건 중 하나에 해당하는가: {forbidden}",
}


def _jev_decide(question: str, evidence: str) -> Decision:
    """Jev SDK 호출. TODO: 강의 기준 실제 호출로 교체.

    예상 형태 (확인 필요):
        from jev import Jev
        client = Jev(api_key=os.environ["JEV_API_KEY"])
        r = client.decide(question=question, context=evidence)
        return "yes" if r.answer else "no"
    """
    raise NotImplementedError


_OUT_OF_STOCK = re.compile(r"(품절|일시\s*품절|재입고|sold\s*out|구매\s*불가|판매\s*종료|단종)", re.I)
_OVERSEAS = re.compile(r"(해외\s*직구|해외\s*배송|직구|관세|통관|배송\s*기간\s*\d+\s*~?\s*\d*\s*주|aliexpress|알리익스프레스|큐텐|qoo10|taobao|아마존|amazon)", re.I)
_FAST = re.compile(r"(로켓배송|로켓와우|오늘\s*출발|당일\s*출발|내일\s*도착|익일\s*배송|새벽배송|빠른배송)", re.I)


def _rule_decide(key: str, evidence: str, *, deadline: date | None, forbidden: list[str]) -> Decision:
    ev = evidence or ""
    if key == "in_stock":
        return "no" if _OUT_OF_STOCK.search(ev) else "unknown"
    if key == "domestic_shipping":
        return "no" if _OVERSEAS.search(ev) else "yes"
    if key == "arrives_by_deadline":
        if deadline is None:
            return "yes"
        days_left = (deadline - date.today()).days
        if _OVERSEAS.search(ev):
            return "yes" if days_left >= 14 else "no"
        if _FAST.search(ev):
            return "yes" if days_left >= 1 else "no"
        return "yes" if days_left >= 3 else "unknown"   # 일반 국내 택배 2~3일 가정
    if key == "violates_profile":
        low = ev.lower()
        return "yes" if any(t and t.lower() in low for t in forbidden) else "no"
    return "unknown"


def decide(key: str, evidence: str, *, deadline: date | None = None, forbidden: list[str] | None = None) -> Decision:
    forbidden = forbidden or []
    if os.environ.get("JEV_API_KEY"):
        q = QUESTIONS[key].format(deadline=deadline.isoformat() if deadline else "없음",
                                  forbidden=", ".join(forbidden) or "없음")
        try:
            return _jev_decide(q, evidence)
        except NotImplementedError:
            pass
    return _rule_decide(key, evidence, deadline=deadline, forbidden=forbidden)


def parse_deadline(text: str | None) -> date | None:
    """'이번 주 안', '수요일까지', '2026-11-12', '3일 안' 같은 표현을 date 로. 못 알아들으면 None."""
    if not text:
        return None
    t = text.strip()
    today = date.today()
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3]))
    m = re.search(r"(\d+)\s*일\s*(안|이내|내)", t)
    if m:
        return today + timedelta(days=int(m[1]))
    if "이번 주" in t or "이번주" in t:
        return today + timedelta(days=(6 - today.weekday()))
    if "다음 주" in t or "다음주" in t:
        return today + timedelta(days=(13 - today.weekday()))
    if "내일" in t:
        return today + timedelta(days=1)
    if "모레" in t:
        return today + timedelta(days=2)
    days = ["월", "화", "수", "목", "금", "토", "일"]
    for i, d in enumerate(days):
        if f"{d}요일" in t:
            delta = (i - today.weekday()) % 7 or 7
            return today + timedelta(days=delta)
    return None
