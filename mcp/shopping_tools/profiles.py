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


def resolve_client_id(channel: str, channel_user_id: str) -> str | None:
    """채널 사용자 ID로 client_id 를 찾는다. 없으면 None."""
    key = f"{channel}_user_id" if channel != "telegram" else "telegram_chat_id"
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


def save_profile(profile: dict[str, Any]) -> dict[str, Any]:
    CLIENTS_DIR.mkdir(parents=True, exist_ok=True)
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
