"""클라이언트 프로필 저장소 (YAML 파일 1개 = 클라이언트 1명).

실제 프로필은 HYOJASON_CLIENTS_DIR 아래에 있고 git 에 올리지 않는다.
채널 사용자 ID(kakao_user_id 등) → client_id 매핑도 여기서 한다.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

CLIENTS_DIR = Path(os.environ.get("HYOJASON_CLIENTS_DIR", "./clients")).resolve()
_SAFE = re.compile(r"[^a-zA-Z0-9_-]")


def _path(client_id: str) -> Path:
    cid = _SAFE.sub("-", client_id)[:64]
    return CLIENTS_DIR / f"{cid}.yaml"


def _load_all() -> list[dict[str, Any]]:
    if not CLIENTS_DIR.exists():
        return []
    out = []
    for p in CLIENTS_DIR.glob("*.yaml"):
        if p.name.startswith("_"):
            continue
        with p.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        data.setdefault("client_id", p.stem)
        out.append(data)
    return out


def _channel_key(channel: str) -> str:
    return f"{channel}_user_id" if channel != "telegram" else "telegram_chat_id"


def resolve_client_id(channel: str, channel_user_id: str) -> str | None:
    """채널 사용자 ID로 client_id 를 찾는다. 없으면 None."""
    if not channel_user_id:
        return None
    key = _channel_key(channel)
    for c in _load_all():
        if str((c.get("channels") or {}).get(key, "")) == str(channel_user_id):
            return c["client_id"]
    return None


def get_profile(client_id: str) -> dict[str, Any] | None:
    p = _path(client_id)
    if not p.exists():
        return None
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def normalize_channels(profile: dict[str, Any], channel: str = "", channel_user_id: str = "") -> dict[str, Any]:
    """채널 ID 를 resolve_client 가 찾는 자리(channels.<채널>_user_id)에 모은다.
    모델이 최상위에 channel / channel_user_id / kakao_user_id 로 써도 옮긴다."""
    channels = profile.get("channels") if isinstance(profile.get("channels"), dict) else {}
    top_channel = str(profile.pop("channel", "") or "")
    top_id = str(profile.pop("channel_user_id", "") or "")
    if top_channel and top_id:
        channels.setdefault(_channel_key(top_channel), top_id)
    for k in ("kakao_user_id", "telegram_chat_id"):
        if profile.get(k):
            channels.setdefault(k, str(profile.pop(k)))
    if channel and channel_user_id:
        channels[_channel_key(channel)] = str(channel_user_id)  # 도구 인자가 가장 확실하다
    profile["channels"] = channels
    return profile


def save_profile(profile: dict[str, Any]) -> dict[str, Any]:
    CLIENTS_DIR.mkdir(parents=True, exist_ok=True)
    normalize_channels(profile)
    cid = profile.get("client_id") or f"client-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    profile["client_id"] = cid
    profile.setdefault("history", [])
    profile["updated_at"] = datetime.now(timezone.utc).isoformat()
    with _path(cid).open("w", encoding="utf-8") as f:
        yaml.safe_dump(profile, f, allow_unicode=True, sort_keys=False)
    return profile


def update_profile(client_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """얕은 병합. 리스트(likes/dislikes 등)는 합집합으로."""
    cur = get_profile(client_id) or {"client_id": client_id}
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(cur.get(k), dict):
            for k2, v2 in v.items():
                if isinstance(v2, list) and isinstance(cur[k].get(k2), list):
                    cur[k][k2] = list(dict.fromkeys([*cur[k][k2], *v2]))
                else:
                    cur[k][k2] = v2
        else:
            cur[k] = v
    return save_profile(cur)


def append_history(client_id: str, entry: dict[str, Any]) -> None:
    cur = get_profile(client_id) or {"client_id": client_id}
    entry = {"at": datetime.now(timezone.utc).isoformat(), **entry}
    cur.setdefault("history", []).append(entry)
    save_profile(cur)


def forbidden_terms(profile: dict[str, Any] | None) -> list[str]:
    """Jev 게이트 'violates_profile' 에 넘길 금지 키워드."""
    if not profile:
        return []
    prefs = profile.get("preferences") or {}
    terms = [t for t in (prefs.get("dislikes") or []) if t]
    terms += [b for b in (prefs.get("brands_avoid") or []) if b]
    return terms
