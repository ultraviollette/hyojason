"""Jev yes/no 게이트.

Jev(TypeSafe System One)는 "상태 텍스트 → 타입이 정해진 질문들의 답(yes/no 확률 등)"을
LLM 호출보다 싸고 빠르게 돌려준다. 후보 20개면 LLM 20번 대신 Jev 20번을 돌리고,
살아남은 1~3개만 LLM 이 설명한다.

- 패키지: `jev` (PyPI, Python >= 3.14). 키는 환경변수 TYPESAFE_API_KEY (console.typesafe.ai).
- 사용: `jev.decide(state_text, PydanticModel)` → bool 필드는 p(yes) >= 0.5 로 코어싱.
- 키가 없거나 jev 가 설치되지 않았으면 규칙 기반 폴백(_rule_decide)으로 동작한다.
"""
from __future__ import annotations

import os
import re
from datetime import date, timedelta
from typing import Literal

Decision = Literal["yes", "no", "unknown"]
GATE_KEYS = ("in_stock", "domestic_shipping", "arrives_by_deadline", "violates_profile")

# ---------- Jev ----------

try:
    import jev  # type: ignore
    from pydantic import BaseModel, Field

    class ProductGates(BaseModel):
        """상품 페이지 텍스트를 보고 네 가지를 판정한다. 상품 1개 = Jev 호출 1번."""
        in_stock: bool = Field(description="지금 바로 주문할 수 있는 재고가 있다 (품절·재입고 대기·판매 종료가 아니다)")
        domestic_shipping: bool = Field(description="국내에서 발송된다 (해외 직구·해외 배송이 아니다)")
        arrives_by_deadline: bool = Field(description="본문에 적힌 '필요 기한'까지 도착할 수 있다. 기한이 '없음'이면 참")
        violates_profile: bool = Field(description="본문에 적힌 '피해야 할 조건' 중 하나에 해당한다. 조건이 '없음'이면 거짓")

    _JEV_AVAILABLE = True
except Exception:  # jev 미설치 또는 Python < 3.14
    _JEV_AVAILABLE = False


def _jev_decide_all(evidence: str, *, deadline: date | None, forbidden: list[str]) -> dict[str, Decision]:
    state = (
        f"[필요 기한] {deadline.isoformat() if deadline else '없음'} (오늘 {date.today().isoformat()})\n"
        f"[피해야 할 조건] {', '.join(forbidden) or '없음'}\n"
        f"[상품 정보]\n{evidence[:4000]}"
    )
    result = jev.decide(state, ProductGates)  # type: ignore[name-defined]
    return {k: ("yes" if getattr(result, k) else "no") for k in GATE_KEYS}


# ---------- 규칙 기반 폴백 ----------

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


# ---------- 공개 API ----------

def decide_all(evidence: str, *, deadline: date | None = None, forbidden: list[str] | None = None) -> tuple[dict[str, Decision], str]:
    """네 게이트를 한 번에 판정. 반환: (결과 dict, 사용한 엔진 'jev' | 'rules')."""
    forbidden = forbidden or []
    if _JEV_AVAILABLE and os.environ.get("TYPESAFE_API_KEY"):
        try:
            return _jev_decide_all(evidence, deadline=deadline, forbidden=forbidden), "jev"
        except Exception:
            pass  # Jev 장애 시 규칙으로 폴백
    return {k: _rule_decide(k, evidence, deadline=deadline, forbidden=forbidden) for k in GATE_KEYS}, "rules"


def decide(key: str, evidence: str, *, deadline: date | None = None, forbidden: list[str] | None = None) -> Decision:
    """게이트 하나만 (하위 호환)."""
    return decide_all(evidence, deadline=deadline, forbidden=forbidden)[0][key]


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
