"""클라이언트 프로필 저장소 (YAML 파일 1개 = 클라이언트 1명).

실제 프로필은 HYOJASON_CLIENTS_DIR 아래에 있고 git 에 올리지 않는다.

스키마는 코드로 강제한다 (clients/_example.yaml 과 같은 구조). 예전에는 에이전트가 자유 형식 dict 를 저장해
저장할 때마다 키를 지어냈고(name, shipping.default_region, channel_user_id …), resolve 가 실패했다.
- 파일 이름 규칙: <채널>-<사용자ID의 sha256 앞 16자>.yaml  (client_id 와 같다)
- 채널 사용자 ID 는 channels.<채널>_user_id 에 둔다 (telegram 은 telegram_chat_id).
- 읽을 때는 옛 형식도 standardize() 로 표준 형식으로 바꿔 읽는다. 파일을 고치려면 migrate_profiles 를 쓴다.
- items: 찾아 드린(found)·고르신(chosen) 물건. record_item 도구만 쓰고, 칸·개수(최근 50건)를 코드로 강제한다.
  실제 결제 여부는 알 수 없으므로 기록하지 않는다.
- payment: 주로 사는 곳(place)과 결제하는 사람(by: "" 아직 안 물음 | self | family). 다른 사람에게 연락하는 칸은 없다.
- history: 예전 log_recommendation 의 자유 형식 기록. 읽기만 하고 더 쓰지 않는다.
"""
from __future__ import annotations

import copy
import hashlib
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any

import yaml

CLIENTS_DIR = Path(os.environ.get("HYOJASON_CLIENTS_DIR", "./clients")).resolve()
_SAFE = re.compile(r"[^a-zA-Z0-9_-]")
CHANNELS = ("kakao", "telegram")
KST = ZoneInfo("Asia/Seoul")

ITEM_STATUSES = ("found", "chosen")      # found: 찾아 드림, chosen: 고르심
PAYMENT_BY = ("self", "family")          # self: 본인, family: 다른 가족
MAX_ITEMS = 50                           # 넘으면 오래된 found 부터 지우고 chosen 은 끝까지 남긴다
RECENT_FOUND_IN_VIEW = 10                # resolve_client 에는 chosen 전부 + 최근 found 10건만
MAX_TEXT = 80
MAX_OPTIONS = 6
MAX_PLACE = 20


# ---------- 표준 스키마 ----------

def empty_profile() -> dict[str, Any]:
    return {
        "client_id": "",
        "display_name": "",
        "language": "ko",
        "tone": "elder-polite",
        "channels": {"kakao_user_id": "", "telegram_chat_id": ""},
        "body": {"top_size": "", "bottom_size": "", "shoe_size_mm": "", "height_cm": "", "notes": ""},
        "preferences": {"likes": [], "dislikes": [], "brands_ok": [], "brands_avoid": [], "price_sensitivity": "medium"},
        "shopping_rules": {"default_deadline_days": None, "allow_overseas_when_no_deadline": True,
                           "max_candidates": 3},
        "delivery": {"region": ""},
        "payment": {"place": "", "by": ""},
        "notes": [],
        "items": [],
        "history": [],
        "updated_at": "",
    }


def channel_key(channel: str) -> str:
    return "telegram_chat_id" if channel == "telegram" else f"{channel}_user_id"


def client_id_for(channel: str, channel_user_id: str) -> str:
    """파일 이름·client_id 규칙. 채널 사용자 ID 원문은 파일 이름에 쓰지 않는다."""
    digest = hashlib.sha256(str(channel_user_id).encode("utf-8")).hexdigest()[:16]
    return f"{_SAFE.sub('-', channel)}-{digest}"


def _path(client_id: str) -> Path:
    return CLIENTS_DIR / f"{_SAFE.sub('-', client_id)[:80]}.yaml"


def _as_list(v: Any) -> list[str]:
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple, set)):
        return [str(x) for x in v if x not in (None, "")]
    if isinstance(v, str):
        return [x.strip() for x in re.split(r"[,/]", v) if x.strip()]
    return [str(v)]


def _str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v if x not in (None, ""))
    return str(v)


def _merge_list(a: list[str], b: list[str]) -> list[str]:
    return list(dict.fromkeys([*a, *b]))


def standardize(raw: dict[str, Any], stem: str = "") -> tuple[dict[str, Any], list[str]]:
    """옛 형식(에이전트가 지어낸 키 포함)을 표준 형식으로 바꾼다.
    반환: (표준 프로필, 표준 자리로 옮기지 못해 notes 로 보낸 원래 키 목록). 값은 버리지 않는다."""
    raw = copy.deepcopy(dict(raw or {}))  # 안쪽 dict 에서 값을 꺼내므로 호출한 쪽 데이터를 건드리지 않게
    p = empty_profile()
    leftovers: list[str] = []

    def note(key: str, value: Any) -> None:
        if value not in (None, "", [], {}):
            p["notes"].append(f"{key}: {_str(value) if not isinstance(value, dict) else value}")
            leftovers.append(key)

    p["client_id"] = _str(raw.pop("client_id", "")) or stem
    for k in ("display_name", "name", "호칭", "nickname"):
        if k in raw:
            v = _str(raw.pop(k))
            if v and not p["display_name"]:
                p["display_name"] = v
            elif v and v != p["display_name"]:  # 다른 이름 키가 또 있으면 버리지 않고 메모로
                note(k, v)
    for k in ("language", "tone"):
        if raw.get(k):
            p[k] = _str(raw.pop(k))

    # 채널
    ch = raw.pop("channels", None)
    top_channel = raw.pop("channel", None)
    if isinstance(top_channel, dict):            # channel 을 dict 로 쓴 경우
        ch = {**(top_channel or {}), **(ch or {})}
        top_channel = None
    if isinstance(ch, dict):
        for k, v in ch.items():
            if v not in (None, ""):
                p["channels"][k if k.endswith(("_user_id", "_chat_id")) else channel_key(k)] = _str(v)
    top_id = raw.pop("channel_user_id", None)
    if top_id:
        p["channels"][channel_key(_str(top_channel) or "kakao")] = _str(top_id)
    for k in ("kakao_user_id", "telegram_chat_id"):
        if raw.get(k):
            p["channels"][k] = _str(raw.pop(k))

    # 몸·사이즈
    body = raw.pop("body", None) or {}
    meas = raw.pop("measurements", None) or {}
    src = {**(meas if isinstance(meas, dict) else {}), **(body if isinstance(body, dict) else {})}
    size_keys = {"top_size": ("top_size", "size_top", "clothing_size", "상의"),
                 "bottom_size": ("bottom_size", "size_bottom", "pants_size", "하의"),
                 "shoe_size_mm": ("shoe_size_mm", "shoe_size", "신발"),
                 "height_cm": ("height_cm", "height", "키")}
    for std, keys in size_keys.items():
        for k in keys:
            for container in (src, raw):
                if container.get(k) not in (None, "", []) and not p["body"][std]:
                    p["body"][std] = _str(container.pop(k))
    if src.get("notes"):
        p["body"]["notes"] = _str(src.pop("notes"))
    for k, v in src.items():
        note(f"body.{k}", v)

    # 선호
    prefs = raw.pop("preferences", None)
    if isinstance(prefs, dict):
        for k in ("likes", "dislikes", "brands_ok", "brands_avoid"):
            p["preferences"][k] = _as_list(prefs.pop(k, None))
        if prefs.get("price_sensitivity"):
            p["preferences"]["price_sensitivity"] = _str(prefs.pop("price_sensitivity"))
        for k, v in prefs.items():
            note(f"preferences.{k}", v)
    elif prefs:
        p["preferences"]["likes"] = _as_list(prefs)
    for k in ("avoid", "dislikes"):
        if k in raw:
            p["preferences"]["dislikes"] = _merge_list(p["preferences"]["dislikes"], _as_list(raw.pop(k)))
    if "likes" in raw:
        p["preferences"]["likes"] = _merge_list(p["preferences"]["likes"], _as_list(raw.pop("likes")))

    # 쇼핑 규칙
    rules = raw.pop("shopping_rules", None)
    if isinstance(rules, dict):
        # 옛 결제 칸: delegate 만 payment.by=family 로 옮긴다. 옛 기본값 self 는 실제로 물어서 받은 답인지
        # 알 수 없어 옮기지 않는다. 연락처는 비어 있으면 버리고, 값이 있으면 notes 로 남긴다.
        old_payment = _str(rules.pop("payment", "")).strip()
        if old_payment == "delegate":
            p["payment"]["by"] = "family"
        elif old_payment not in ("", "self"):
            note("shopping_rules.payment", old_payment)
        contact = rules.pop("payment_delegate_contact", None)
        if _str(contact).strip():
            note("shopping_rules.payment_delegate_contact", contact)
        for k, v in rules.items():
            if k in p["shopping_rules"]:
                p["shopping_rules"][k] = v
            else:
                note(f"shopping_rules.{k}", v)

    # 배송 지역 (정확한 주소는 저장하지 않는 원칙: 지역만 옮기고 나머지는 메모)
    for container_key in ("delivery", "shipping", "shipping_address", "address"):
        c = raw.pop(container_key, None)
        if isinstance(c, dict):
            for k in ("region", "default_region", "city", "area"):
                if c.get(k) and not p["delivery"]["region"]:
                    p["delivery"]["region"] = _str(c.pop(k))
            for k, v in c.items():
                note(f"{container_key}.{k}", v)
        elif c and not p["delivery"]["region"]:
            p["delivery"]["region"] = _str(c)
    for k in ("region", "delivery_region", "city"):
        if raw.get(k) and not p["delivery"]["region"]:
            p["delivery"]["region"] = _str(raw.pop(k))

    pay = raw.pop("payment", None)
    if isinstance(pay, dict):
        place = _str(pay.pop("place", "")).strip()
        by = _str(pay.pop("by", "")).strip()
        p["payment"]["place"] = place[:MAX_PLACE]
        if by in PAYMENT_BY:
            p["payment"]["by"] = by
        elif by:
            note("payment.by", by)
        for k, v in pay.items():
            note(f"payment.{k}", v)
    elif pay is not None:
        note("payment", pay)

    for it in (raw.pop("items", None) or []):
        try:
            p["items"].append(_clean_item(it))
        except (ValueError, TypeError, AttributeError):
            note("items", it)               # 형식이 깨진 항목도 버리지 않는다

    p["notes"] = _merge_list(_as_list(raw.pop("notes", None)), p["notes"])
    hist = raw.pop("history", None)
    p["history"] = hist if isinstance(hist, list) else []
    p["updated_at"] = _str(raw.pop("updated_at", ""))
    for k, v in raw.items():
        note(k, v)
    return p, leftovers


# ---------- 찾아 드린 물건(items) ----------

def _text(v: Any, field: str, *, required: bool = False, limit: int = MAX_TEXT) -> str:
    if v is None:
        v = ""
    if not isinstance(v, (str, int, float)):
        raise ValueError(f"{field} 는 글자여야 해요")
    t = str(v).strip()
    if required and not t:
        raise ValueError(f"{field} 가 비어 있어요")
    if len(t) > limit:
        raise ValueError(f"{field} 는 {limit}자 이내여야 해요")
    return t


def _clean_item(it: Any) -> dict[str, Any]:
    """items 한 건을 정해진 칸으로만 만든다. 칸이 틀리면 ValueError."""
    if not isinstance(it, dict):
        raise ValueError("항목 형식이 아니에요")
    extra = set(it) - {"date", "status", "brand", "name", "options", "seller", "seen_price_krw"}
    if extra:
        raise ValueError(f"정해지지 않은 칸: {', '.join(sorted(extra))}")
    status = _text(it.get("status"), "status", required=True)
    if status not in ITEM_STATUSES:
        raise ValueError(f"status 는 {ITEM_STATUSES} 중 하나여야 해요")
    options = it.get("options") or {}
    if not isinstance(options, dict):
        raise ValueError("options 는 {이름: 값} 형태여야 해요 (예: {\"용량\": \"240ml\"})")
    if len(options) > MAX_OPTIONS:
        raise ValueError(f"options 는 {MAX_OPTIONS}개까지예요")
    clean_opts = {_text(k, "options 이름", required=True, limit=20): _text(v, "options 값", required=True)
                  for k, v in options.items()}
    price = it.get("seen_price_krw")
    if price in ("", None):
        price = None
    elif isinstance(price, bool) or not isinstance(price, int) or price <= 0:
        raise ValueError("seen_price_krw 는 확인된 가격(원, 양의 정수)이거나 비워 두어야 해요")
    date = _text(it.get("date"), "date", required=True, limit=10)
    datetime.strptime(date, "%Y-%m-%d")      # 형식 검사
    return {"date": date, "status": status, "brand": _text(it.get("brand"), "brand"),
            "name": _text(it.get("name"), "name", required=True), "options": clean_opts,
            "seller": _text(it.get("seller"), "seller"), "seen_price_krw": price}


def _same_item(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return (a["date"], a["brand"], a["name"], a["options"]) == (b["date"], b["brand"], b["name"], b["options"])


def _trim_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """최신이 앞. 50건을 넘으면 오래된 found 부터, 그래도 넘으면 오래된 chosen 을 지운다."""
    items = list(items)
    while len(items) > MAX_ITEMS:
        idx = next((i for i in range(len(items) - 1, -1, -1) if items[i]["status"] == "found"), len(items) - 1)
        items.pop(idx)
    return items


def record_item(client_id: str, *, status: str, name: str, brand: str = "", options: dict[str, str] | None = None,
                seller: str = "", seen_price_krw: int | None = None, today: str | None = None) -> dict[str, Any]:
    """찾아 드린(found)·고르신(chosen) 물건을 정해진 칸으로 남긴다. 반환: 남긴 항목.
    chosen 은 같은 날 같은 브랜드·이름·옵션의 found 가 있으면 그 항목을 chosen 으로 바꾼다(두 줄로 남지 않게)."""
    cur = get_profile(client_id)
    if cur is None:
        raise ValueError("프로필이 없어요. 먼저 resolve_client / save_client_profile 로 확인하세요")
    item = _clean_item({"date": today or datetime.now(KST).date().isoformat(), "status": status, "brand": brand,
                        "name": name, "options": options, "seller": seller, "seen_price_krw": seen_price_krw})
    items = cur["items"]
    match = next((i for i, x in enumerate(items) if _same_item(x, item)), None)
    if match is not None:
        old = items[match]
        if old["status"] == "chosen" and item["status"] == "found":
            return old                   # 이미 고르신 물건을 다시 찾아 드림: 고르신 기록을 그대로 둔다
        items.pop(match)                 # 같은 물건은 한 줄로: found→chosen 전환, 같은 상태면 최신으로 올림
        item = {**item, "seller": item["seller"] or old["seller"],
                "seen_price_krw": item["seen_price_krw"] if item["seen_price_krw"] is not None
                else old["seen_price_krw"]}
    cur["items"] = _trim_items([item, *items])
    _write(cur)
    return item


def agent_view(profile: dict[str, Any] | None) -> tuple[dict[str, Any] | None, int]:
    """도구가 돌려줄 프로필. items 는 chosen 전부 + 최근 found 10건(최신이 앞). 반환: (프로필, 뺀 found 건수)."""
    if profile is None:
        return None, 0
    view = copy.deepcopy(profile)
    found_kept, kept, omitted = 0, [], 0
    for it in view["items"]:
        if it["status"] == "chosen":
            kept.append(it)
        elif found_kept < RECENT_FOUND_IN_VIEW:
            kept.append(it); found_kept += 1
        else:
            omitted += 1
    view["items"] = kept
    return view, omitted


# ---------- 읽기 ----------

def _read(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return standardize(yaml.safe_load(f) or {}, path.stem)[0]


def _load_all() -> list[dict[str, Any]]:
    if not CLIENTS_DIR.exists():
        return []
    return [_read(p) for p in sorted(CLIENTS_DIR.glob("*.yaml")) if not p.name.startswith("_")]


def resolve_client_id(channel: str, channel_user_id: str) -> str | None:
    """채널 + 사용자 ID 로 client_id 를 찾는다. 규칙대로 만든 파일을 먼저 보고, 없으면 옛 형식 파일도 훑는다."""
    if not channel or not channel_user_id:
        return None
    cid = client_id_for(channel, channel_user_id)
    if _path(cid).exists():
        return cid
    key = channel_key(channel)
    for c in _load_all():
        if c["channels"].get(key) == str(channel_user_id):
            return c["client_id"]
    return None


def get_profile(client_id: str) -> dict[str, Any] | None:
    p = _path(client_id)
    return _read(p) if p.exists() else None


# ---------- 쓰기 ----------

def _write(profile: dict[str, Any]) -> dict[str, Any]:
    CLIENTS_DIR.mkdir(parents=True, exist_ok=True)
    profile["updated_at"] = datetime.now(timezone.utc).isoformat()
    with _path(profile["client_id"]).open("w", encoding="utf-8") as f:
        yaml.safe_dump(profile, f, allow_unicode=True, sort_keys=False)
    return profile


def upsert_profile(channel: str, channel_user_id: str, *, display_name: str = "", top_size: str = "",
                   bottom_size: str = "", shoe_size_mm: str = "", height_cm: str = "", body_notes: str = "",
                   delivery_region: str = "", likes: list[str] | None = None, dislikes: list[str] | None = None,
                   brands_avoid: list[str] | None = None, price_sensitivity: str = "",
                   notes: list[str] | None = None, payment_place: str = "",
                   payment_by: str = "") -> tuple[dict[str, Any], bool]:
    """정해진 필드만 저장한다. 빈 값은 기존 값을 지우지 않고, 목록은 합친다. 반환: (프로필, 새로 만들었는지)"""
    if channel not in CHANNELS:
        raise ValueError(f"channel 은 {CHANNELS} 중 하나여야 해요")
    if not str(channel_user_id).strip():
        raise ValueError("channel_user_id 가 비어 있어요")
    payment_by = str(payment_by or "").strip()
    if payment_by and payment_by not in PAYMENT_BY:
        raise ValueError(f"payment_by 는 {PAYMENT_BY} 중 하나여야 해요 (self: 본인, family: 다른 가족)")
    payment_place = _text(payment_place, "payment_place", limit=MAX_PLACE)
    existing = resolve_client_id(channel, channel_user_id)
    p = get_profile(existing) if existing else None
    created = p is None
    if p is None:
        p = empty_profile()
        p["client_id"] = client_id_for(channel, channel_user_id)
    p["channels"][channel_key(channel)] = str(channel_user_id)

    for path, value in ((("display_name",), display_name), (("body", "top_size"), top_size),
                        (("body", "bottom_size"), bottom_size), (("body", "shoe_size_mm"), shoe_size_mm),
                        (("body", "height_cm"), height_cm), (("body", "notes"), body_notes),
                        (("delivery", "region"), delivery_region),
                        (("preferences", "price_sensitivity"), price_sensitivity),
                        (("payment", "place"), payment_place), (("payment", "by"), payment_by)):
        if str(value or "").strip():
            target = p
            for k in path[:-1]:
                target = target[k]
            target[path[-1]] = str(value).strip()
    for key, values in (("likes", likes), ("dislikes", dislikes), ("brands_avoid", brands_avoid)):
        p["preferences"][key] = _merge_list(p["preferences"][key], _as_list(values))
    p["notes"] = _merge_list(p["notes"], _as_list(notes))
    return _write(p), created


def forbidden_terms(profile: dict[str, Any] | None) -> list[str]:
    """Jev 게이트 'violates_profile' 에 넘길 금지 키워드."""
    if not profile:
        return []
    prefs = profile.get("preferences") or {}
    return [t for t in [*(prefs.get("dislikes") or []), *(prefs.get("brands_avoid") or [])] if t]
