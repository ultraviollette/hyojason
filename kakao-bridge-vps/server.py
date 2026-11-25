"""효자손 카카오 브릿지 (VPS 상시 프로세스판)

kakao-bridge/src/index.ts (Cloudflare Worker) 의 동작을 그대로 옮긴 것.
Worker 의 waitUntil 30초 제한 때문에 Hermes 응답이 길면 콜백이 취소돼서,
같은 다리를 VPS 에서 상시 프로세스로 돌린다.

흐름
 1. 카카오가 POST /kakao/skill 로 사용자 발화(+사진)를 보낸다. 스킬 서버는 5초 안에 응답해야 한다.
 2. 즉시 { useCallback: true } 로 답한다. 이어서 온 메시지는 잠깐 기다렸다 합치고(처리 중에 온 것은 다음 한 번으로),
    백그라운드 asyncio 태스크에서 Hermes 에게 한 번 묻는다 ("메시지 합치기" 참고).
 3. Hermes 답을 마지막 메시지의 userRequest.callbackUrl 로 POST 한다 (유효 5분, 1회). 합쳐진 앞 메시지의 콜백은 짧은 안내로 닫는다.

로그는 요청당 한 줄(시각, 경로, 결과, 소요 시간). 시크릿·API 키·사용자 발화·사용자 ID·callbackUrl 은 남기지 않는다.
uvicorn access log 는 쿼리스트링(?secret=...)을 찍으므로 끈다.

실행: uv run --project <dir> python server.py   (127.0.0.1:$PORT 에만 바인딩)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import string
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

log = logging.getLogger("hyojason.bridge")

# 카카오 관리자센터 블록의 Callback 설정 "응답대기 메시지"와 같은 문구로 맞춘다 (둘 중 어느 쪽이 보이는지 문서에 없음).
QUICK_ACK = "가게 몇 군데 돌아보고 올게요. 길면 2~3분 걸려요.\n여쭤볼 게 있으면 먼저 물어볼게요. 다른 일 보고 계세요 🐾"
NO_CALLBACK_MSG = "지금은 답이 늦어지고 있어요. 블록에 Callback API 설정을 켜 주세요."
FAIL_MSG = "죄송해요, 지금은 찾기가 어려워요. 잠시 뒤에 다시 말씀해 주세요."
EMPTY_MSG = "답을 만들지 못했어요. 다시 한 번 말씀해 주세요."
BAD_REQUEST_MSG = "요청을 읽지 못했어요. 다시 보내 주세요."

SYNC_TIMEOUT_S = 4.5      # 콜백 없을 때 (Worker 와 동일)
HERMES_TIMEOUT_S = 240    # 콜백 경로 (Worker 와 동일)
CALLBACK_TIMEOUT_S = 10   # callbackUrl POST (Worker 는 타임아웃 없음)


@dataclass(frozen=True)
class Settings:
    hermes_api_url: str = "http://127.0.0.1:8642"
    hermes_api_key: str = ""
    kakao_bot_secret: str = ""
    hermes_model: str = ""
    port: int = 8700
    # 이어서 온 메시지를 합치는 대기 시간(초). 마지막 메시지에서 wait_more_s, 첫 메시지에서 최대 wait_max_*.
    # 사진만 온 경우는 길게 기다린다. 23e409b 실사용: 사진 뒤 글이 6초·9초·11초 뒤에 온 일이 세 번 있었고,
    # 그때마다 사진 턴을 45초쯤 돌린 뒤 버렸다(질문형 답 보내지 않기). 글로 시작한 경우는 그대로 4초/10초.
    wait_first_text_s: float = 4.0
    wait_first_photo_s: float = 12.0
    wait_more_s: float = 4.0
    wait_max_text_s: float = 10.0
    wait_max_photo_s: float = 15.0
    # 이 파일이 있으면 사진 메시지 utterance 의 "형태"만 로그에 남긴다(기본 꺼짐). 내용·주소·ID 는 남기지 않는다.
    utterance_shape_flag: str = "~/.config/hyojason/debug-utterance-shape"

    @classmethod
    def from_env(cls, env: dict[str, str] | os._Environ = os.environ) -> "Settings":
        return cls(
            hermes_api_url=env.get("HERMES_API_URL") or cls.hermes_api_url,
            hermes_api_key=env.get("HERMES_API_KEY", ""),
            kakao_bot_secret=env.get("KAKAO_BOT_SECRET", ""),
            hermes_model=env.get("HERMES_MODEL", ""),
            port=int(env.get("PORT") or cls.port),
        )


# ---------- Hermes ----------

async def ask_hermes(s: Settings, client: httpx.AsyncClient, kakao_user_id: str, text: str,
                     image_urls: list[str], conversation: str) -> str:
    """Hermes Agent 의 OpenAI 호환 API (/v1/responses) 호출. payload 는 Worker 와 동일."""
    prefix = f"[channel=kakao user_id={kakao_user_id}]"
    content: list[dict[str, str]] = [
        {"type": "input_text", "text": f"{prefix}\n{text}" if text else f"{prefix}\n(사진만 보냄)"}
    ]
    content += [{"type": "input_image", "image_url": u} for u in image_urls]

    payload: dict[str, Any] = {
        "input": [{"role": "user", "content": content}],
        "conversation": conversation,
        "metadata": {"channel": "kakao", "channel_user_id": kakao_user_id},
    }
    if s.hermes_model:
        payload["model"] = s.hermes_model

    r = await client.post(
        f"{s.hermes_api_url.rstrip('/')}/v1/responses",
        json=payload,
        headers={"authorization": f"Bearer {s.hermes_api_key}"},
    )
    if not r.is_success:
        raise RuntimeError(f"hermes {r.status_code}")
    return extract_text(r.json()) or EMPTY_MSG


def extract_text(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    out = data.get("output")
    if isinstance(out, list):
        parts = [p["text"] for item in out if isinstance(item, dict)
                 for p in (item.get("content") if isinstance(item.get("content"), list) else [])
                 if isinstance(p, dict) and isinstance(p.get("text"), str)]
        if parts:
            return "\n".join(parts)
    choices = data.get("choices")  # chat/completions 형태 대비
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        msg = choices[0].get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            return msg["content"]
    return ""


# ---------- Kakao helpers ----------

_URL = re.compile(r"^https?://\S+", re.I)
_IMG_HINT = re.compile(r"(secure|image|media|img|photo|kakaocdn)", re.I)


def extract_image_urls(body: dict[str, Any]) -> list[str]:
    """params 전체에서 URL 처럼 생긴 값을 긁는다 (Worker 와 동일한 휴리스틱, 최대 3개)."""
    urls: dict[str, None] = {}  # 삽입 순서 유지하는 set

    def walk(v: Any, depth: int = 0) -> None:
        if depth > 5 or v is None:
            return
        if isinstance(v, str):
            if _URL.search(v) and _IMG_HINT.search(v):
                urls[v] = None
            return
        if isinstance(v, list):
            for x in v:
                walk(x, depth + 1)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x, depth + 1)

    ur = body.get("userRequest") or {}
    action = body.get("action") or {}
    walk(ur.get("params") if isinstance(ur, dict) else None)
    if isinstance(action, dict):
        walk(action.get("params"))
        walk(action.get("detailParams"))
    return list(urls)[:3]


def strip_image_utterance(text: str, images: list[str]) -> str:
    """사진 메시지의 utterance 가 params 의 이미지 주소와 같거나 그 주소를 담고 있으면 그 주소 글자를 뺀다.
    23e409b 형태 로그: 사진 메시지 utterance 는 starts_url=1, has_param_image=1 (주소 그 자체, len=196).
    그 주소가 글로 Hermes 에 가고, 사진만 온 메시지가 "글이 있는 메시지"로 잡혀 사진 대기도 쓰이지 않았다.
    params 의 주소와 겹치지 않는 글은 건드리지 않는다(사용자가 직접 붙인 링크일 수 있다)."""
    hits = [u for u in images if u and u in text]
    if not hits:
        return text
    for u in sorted(hits, key=len, reverse=True):
        text = text.replace(u, " ")
    return re.sub(r"\s+", " ", text).strip()


def split_text(s: str, n: int) -> list[str]:
    out: list[str] = []
    cur = ""
    for line in s.split("\n"):
        if len(cur + "\n" + line) > n and cur:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out or [s[:n]]


def kakao_text_body(text: str) -> dict[str, Any]:
    # simpleText 는 1000자 제한, 500자 넘으면 '전체 보기' 로 접힌다. 길면 나눈다 (최대 3개).
    chunks = split_text(text, 480)[:3]
    return {"version": "2.0", "template": {"outputs": [{"simpleText": {"text": t}} for t in chunks]}}


# ---------- 링크 → 버튼 ----------
# 카카오 스킬 응답(콜백 포함)은 textCard 의 webLink 버튼을 지원한다 (세로 최대 3개, 버튼 문구 14자,
# 단일 카드 title+description 최대 400자). quickReplies 는 message/block 만 돼서 링크에 못 쓴다.
# Hermes 표기 규칙에 기대지 않고, 답변에서 http(s) URL 을 직접 찾아 버튼으로 옮긴다.

_LINK_MD = re.compile(r"\[([^\]\n]{0,40})\]\((https?://[^\s)]+)\)")   # [라벨](url)
# 주소는 ASCII 글자까지만 본다. 도구가 주는 상품 주소는 퍼센트 인코딩된 ASCII 라서, 주소 바로 뒤에 붙은
# 한글 조사("…vendorItemId=7001에서")가 주소에 섞여 버튼 주소가 바뀌지 않게 한다.
_LINK_RAW = re.compile(r"<?(https?://[^\s<>\"'\u0080-\U0010ffff]+)>?")
_TRAIL = ".,;:!?)]}>'\"」』"
_LEFTOVER_MAX = 14   # URL 을 뺀 뒤 이 길이 이하만 남은 줄("쿠팡:", "👉 1번 사러 가기:")은 지운다
MAX_BUTTONS = 3
CARD_DESC_MAX = 380  # textCard 단일형 400자 제한에 여유
_SITES = (("coupang.com", "쿠팡"), ("naver.com", "네이버"), ("naver.me", "네이버"),
          ("11st.co.kr", "11번가"), ("gmarket.co.kr", "G마켓"), ("e-himart.co.kr", "하이마트"))


# 후보로 내면 안 되는 판매처. SKILL "후보에서 빼는 판매처" 규칙이 새도 버튼으로는 나가지 않게 막는다.
# 크라우드펀딩은 펀딩이 끝났으면 살 수 없고, 진행 중이어도 바로 살 수 있는 물건이 아니다.
BLOCKED_LINK_DOMAINS = ("wadiz.kr", "tumblbug.com")
LINKS_ONLY_BODY = "아래 버튼을 눌러 열어 보세요."   # 답에 링크만 있고 본문이 비었을 때 카드 설명 칸에 넣는 기본 문구
NO_LINK_MSG = "보내 드릴 수 있는 링크가 없어요. 사고 싶은 물건을 한 번 더 말씀해 주세요."
# 쿠팡 파트너스 대가성 문구(사용자가 정한 문장). mcp/shopping_tools/coupang_partners.py 의 DISCLOSURE 와 같아야 한다.
# 버튼으로 나가는 링크에 쿠팡 파트너스 주소가 하나라도 있으면, 이 문장을 본문 맨 첫 줄에 한 번만 둔다
# (에이전트가 빠뜨리거나 다른 자리에 써도 브릿지가 보장한다. 사용자 결정, 2026-11-25).
DISCLOSURE = "이 게시물은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."
COUPANG_PARTNERS_HOST = "link.coupang.com"


def is_coupang_partners_link(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").rstrip(".")
    except ValueError:
        return False
    return host == COUPANG_PARTNERS_HOST


def _without_disclosure(body: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", body.replace(DISCLOSURE, "")).strip()


def is_blocked_link(url: str) -> bool:
    """wadiz.kr·tumblbug.com 과 그 하위 도메인. user@host, 대문자, 끝 점에도 속지 않게 hostname 으로 본다."""
    try:
        host = (urlsplit(url).hostname or "").rstrip(".")
    except ValueError:
        return False
    return any(host == d or host.endswith("." + d) for d in BLOCKED_LINK_DOMAINS)


LABEL_MAX = 14       # 카카오 Button.label 제한 "버튼 14자(가로배열 2개 8자)". 세로 배치를 쓰므로 14자. 공백 포함해 센다
# 링크 줄 맨 앞의 후보 번호: "1번 하이마트:", "👉 2번 사러 가기:", "3) 11번가:", "1. 공식몰:". "11번가"의 "11번"은 아니다.
_CANDIDATE_NO = re.compile(r"^[^\w]*([1-9])\s*(?:번(?!가)|[).])\s*(.*)$", re.S)
_LABEL_TAIL = "에서 보기"
_NOT_A_SITE = re.compile(r"(사러|보기|링크|열기|여기|클릭|바로\s*가기|\d+\s*번)")
_HOST_PREFIX = re.compile(r"^(www|m|mobile|shop|store|mall)\.")
_HOST_SUFFIX = re.compile(r"\.(co\.kr|or\.kr|com|kr|net|co|shop|store|io)$")


def _host(url: str) -> str:
    return re.sub(r"^https?://", "", url).split("/", 1)[0].split("?", 1)[0].split(":", 1)[0].lower()


def _known_site(url: str) -> str | None:
    host = _host(url)
    return next((name for dom, name in _SITES if host == dom or host.endswith("." + dom)), None)


def _host_name(url: str) -> str:
    """모르는 도메인에서 사이트 이름을 뽑는다. shop.espoir.co.kr → espoir"""
    host = _HOST_SUFFIX.sub("", _host(url))
    while _HOST_PREFIX.match(host):
        host = _HOST_PREFIX.sub("", host, count=1)
    return host.rsplit(".", 1)[-1] or "링크"


def _site_hint(text: str) -> str | None:
    """링크 줄의 앞부분("에스쁘아 공식몰: URL")이 사이트 이름처럼 보이면 쓴다. 안내 문구("1번 사러 가기")는 버린다."""
    hint = text.strip().strip(":：-→👉·• ").strip()
    if 1 <= len(hint) <= 10 and not _NOT_A_SITE.search(hint) and re.search(r"\w", hint):
        return hint
    return None


def site_name(url: str, hint: str | None = None) -> str:
    """버튼에 쓸 사이트 이름: 아는 쇼핑몰 > 링크 줄에 적힌 이름 > 도메인에서 뽑은 이름."""
    return _known_site(url) or (hint and _site_hint(hint)) or _host_name(url)


def candidate_number(hint: str | None) -> tuple[int | None, str | None]:
    """링크 줄 앞부분에서 후보 번호를 읽는다. (번호, 번호를 뺀 나머지). 번호가 없으면 (None, hint)."""
    if not hint:
        return None, hint
    m = _CANDIDATE_NO.match(hint)
    return (int(m.group(1)), m.group(2).strip() or None) if m else (None, hint)


def split_links(text: str) -> tuple[str, list[tuple[str, str | None]]]:
    """본문에서 URL 을 빼고 (본문, [(url, 그 줄에 적힌 글)…]) 을 돌려준다. URL 순서 유지, 중복 제거."""
    links: list[tuple[str, str | None]] = []

    lines = []
    for line in text.split("\n"):
        found: list[tuple[str, str | None]] = []
        stripped = _LINK_MD.sub(lambda m: found.append((m.group(2), m.group(1))) or "", line)
        stripped = _LINK_RAW.sub(lambda m: found.append((m.group(1), None)) or "", stripped)
        if found:  # 링크가 있던 줄
            rest = stripped.strip().rstrip(":：-→👉 ").strip()
            for url, md_label in found:
                url = url.rstrip(_TRAIL)
                hint = md_label if md_label is not None else (rest if len(found) == 1 else None)
                if url not in [u for u, _ in links]:
                    links.append((url, hint))
            if len(re.sub(r"[\W_]", "", rest)) == 0 or len(rest) <= _LEFTOVER_MAX:
                continue
            stripped = rest
        lines.append(stripped.rstrip())
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return body, links


def _fit(name: str, prefix: str = "", suffix: str = "") -> str:
    room = LABEL_MAX - len(prefix) - len(_LABEL_TAIL) - len(suffix)
    return f"{prefix}{name[:max(room, 1)]}{_LABEL_TAIL}{suffix}"


def link_buttons(links: list[tuple[str, str | None]]) -> list[dict[str, str]]:
    """버튼 이름표 = "<후보 번호>번 <사이트>에서 보기".
    후보 번호는 링크 줄에 적힌 번호(SKILL: "1번 하이마트: URL")만 쓴다. 번호가 없는 링크에는 번호를 지어내지 않는다
    (예전에는 같은 쇼핑몰 안의 순번을 "1번/2번"으로 붙여 후보 번호처럼 보였다). 이름표가 겹치면 뒤에 " 2"를 붙인다."""
    buttons, seen = [], []
    for url, hint in links[:MAX_BUTTONS]:
        number, rest = candidate_number(hint)
        name = site_name(url, rest)
        prefix = f"{number}번 " if number else ""
        key = (prefix, name)
        suffix = f" {seen.count(key) + 1}" if key in seen else ""
        seen.append(key)
        buttons.append({"action": "webLink", "label": _fit(name, prefix, suffix), "webLinkUrl": url})
    return buttons


def kakao_reply_body(text: str) -> dict[str, Any]:
    """Hermes 답변 → 카카오 응답. 링크가 없으면 simpleText 그대로, 있으면 본문은 글로, 링크는 버튼으로."""
    body, links = split_links(text)
    if not links:
        return kakao_text_body(text)
    links = [link for link in links if not is_blocked_link(link[0])]  # 막힌 링크는 버튼 칸도 차지하지 않는다
    if not links:  # 막힌 링크만 있었다: 원문을 보내면 URL 이 글자로 새므로, URL 을 뺀 본문만 보낸다
        return kakao_text_body(body or NO_LINK_MSG)
    if any(is_coupang_partners_link(u) for u, _ in links[:MAX_BUTTONS]):   # 실제로 버튼이 되는 링크 기준
        body = f"{DISCLOSURE}\n\n{_without_disclosure(body) or LINKS_ONLY_BODY}"
    # 400자를 넘으면 나눈다. 말풍선은 최대 3개라 넘치면 가운데 조각이 빠지고, 카드 설명은 끝이 잘린다. 첫 줄은 항상 남는다.
    chunks = split_text(body, CARD_DESC_MAX) if body else []
    *head, last = chunks or [LINKS_ONLY_BODY]
    outputs: list[dict[str, Any]] = [{"simpleText": {"text": t}} for t in head[:2]]  # 말풍선 최대 3개
    outputs.append({"textCard": {"description": last[:CARD_DESC_MAX], "buttons": link_buttons(links),
                                 "buttonLayout": "vertical"}})
    return {"version": "2.0", "template": {"outputs": outputs}}


# ---------- 사용자별 순서 보장 ----------

class UserLocks:
    """같은 사용자의 요청을 도착 순서대로 하나씩 Hermes 에 보낸다.

    사진과 글을 연달아 보내면 두 요청이 같은 conversation 으로 동시에 Hermes 에 가서,
    뒤 메시지가 앞 메시지의 결과를 모른 채 답했다. asyncio.Lock 은 대기 순서(FIFO)를 지킨다.
    아무도 쓰지 않는 사용자의 락은 지워서 메모리가 쌓이지 않게 한다.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._refs: dict[str, int] = {}

    @asynccontextmanager
    async def hold(self, key: str):
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._refs[key] = self._refs.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._refs[key] -= 1
            if self._refs[key] == 0:
                del self._refs[key], self._locks[key]

    def __len__(self) -> int:
        return len(self._locks)


# ---------- 대화 세션 ----------

SESSION_IDLE_S = 30 * 60
RESET_MSG = "네, 처음부터 다시 할게요. 필요한 물건을 말씀하시거나 사진을 보내 주세요."
_RESET_WORDS = {"처음부터", "새로시작"}
_RESET_NOISE = re.compile(r"[\s.!~?…]+")


def is_reset(text: str) -> bool:
    """'처음부터', '새로 시작' (띄어쓰기·끝 문장부호 무시)."""
    return _RESET_NOISE.sub("", text) in _RESET_WORDS


class Conversations:
    """사용자별 Hermes conversation ID.

    예전에는 conversation 을 kakao-<사용자ID> 로 고정해 첫 대화부터 영원히 이어 썼고, 효자손이
    옛 스킬 규칙·옛 링크·옛 판정을 기억으로 재사용했다. 마지막 메시지로부터 30분이 지났거나
    '처음부터'를 받으면 새 ID(kakao-<사용자ID>-<시작시각>-<난수>)로 바꾼다. 프로필은 MCP clients/ 에 따로 있다.

    상태는 메모리에만 있다. 브릿지가 재시작되면 모든 사용자가 새 대화로 시작한다(옛 대화로 돌아가지 않는다).
    """

    def __init__(self, now: Callable[[], float] = time.time, idle_s: float = SESSION_IDLE_S) -> None:
        self._now, self._idle = now, idle_s
        self._s: dict[str, tuple[str, float]] = {}   # user_id -> (conversation, 마지막 메시지 시각)

    def _new_id(self, user_id: str, t: float) -> str:
        return f"kakao-{user_id}-{int(t)}-{uuid.uuid4().hex[:6]}"

    def current(self, user_id: str) -> tuple[str, bool]:
        """메시지 도착 시 호출. (conversation, 새로 시작했는지)"""
        t = self._now()
        cur = self._s.get(user_id)
        new = cur is None or t - cur[1] >= self._idle
        conv = self._new_id(user_id, t) if new else cur[0]
        self._s[user_id] = (conv, t)
        if len(self._s) > 500:  # 30분 넘게 조용한 사용자는 어차피 새 대화라 지운다
            self._s = {u: v for u, v in self._s.items() if t - v[1] < self._idle}
        return conv, new

    def reset(self, user_id: str) -> str:
        t = self._now()
        conv = self._new_id(user_id, t)
        self._s[user_id] = (conv, t)
        return conv


# ---------- 사진 메시지 utterance 형태 로그 (디버그, 기본 꺼짐) ----------
# 카카오 문서에 일반 사진을 보냈을 때 utterance 에 무엇이 오는지 적혀 있지 않다(실사용 로그: 사진 메시지 text_len=195).
# 형식을 추측해 지우지 않고, 형태만 보고 판단하려고 둔다. 표시 파일이 있을 때만, 사진이 있는 메시지만 남긴다.
_SHAPE_MAX = 200
_ASCII_PUNCT = set(string.punctuation)


def utterance_shape(text: str) -> str:
    """글자를 지우고 모양만 남긴다: 한글→가, 영문→a, 다른 글자→x, 숫자→9, 공백→_, ASCII 기호는 그대로."""
    out = []
    for ch in text[:_SHAPE_MAX]:
        if "가" <= ch <= "힣" or "ㄱ" <= ch <= "ㅣ":
            out.append("가")
        elif ch.isascii() and ch.isalpha():
            out.append("a")
        elif ch.isdigit():
            out.append("9")
        elif ch.isspace():
            out.append("_")
        elif ch in _ASCII_PUNCT:
            out.append(ch)
        else:
            out.append("x")
    return "".join(out)


def utterance_shape_line(seq: int, text: str, images: list[str]) -> str:
    return ("path=utterance_shape seq=%d len=%d starts_url=%d has_param_image=%d json=%d shape=%s"
            % (seq, len(text), 1 if re.match(r"\s*https?://", text, re.I) else 0,
               1 if any(u and u in text for u in images) else 0,
               1 if text.lstrip().startswith(("{", "[")) else 0, utterance_shape(text)))


# ---------- 메시지 합치기 ----------
# 실사용 로그: 사진 뒤의 글은 11~31초 뒤에 와서, 늘 사진 턴(약 53초)을 처리하는 도중에 도착했다.
# 메시지마다 따로 답하니 봇의 답이 한 박자 전 메시지에 대한 답이 되어 "한 칸씩 밀려" 보였다.
#  (가) 첫 메시지 뒤 잠깐 기다렸다가 그 사이 온 메시지를 한 요청으로 합친다.
#  (나) 처리 중에 온 메시지는 끊지 않고 모아서 다음 한 번으로 보낸다.
#  (다) 처리 중 새 메시지가 있고, 돌아온 답이 질문형이면(주소 없음 + 물음표 + 결제 질문 아님) 보내지 않고
#       다음 턴에 "전달되지 않았다"는 브릿지 안내를 붙여 합쳐 보낸다. 이때 답을 보내지 않은 턴의 메시지
#       (사진 포함)도 다음 턴에 다시 넣는다. Hermes 가 앞 턴의 사진을 다시 보는지 확인되지 않아서다.
# 카카오 콜백은 메시지마다 1회라, 합쳐진 앞 메시지의 콜백은 짧은 안내로 닫는다.

CLOSE_MSG = "이어서 보내 주신 것까지 같이 볼게요."
RESET_DROP_MSG = "앞에 보내 주신 건 접어 두고 처음부터 할게요."
BRIDGE_NOTE = ("[브릿지 안내: 직전 당신의 답은 사용자에게 전달되지 않았다. 사용자는 그 답을 보지 못한 채 "
               "아래 메시지를 보냈다. 아래 메시지까지 반영해 다시 판단하라.]")
PAYMENT_QUESTION_MARK = "주로 어디서 사세요"        # SKILL 의 결제 질문 문구. 이 답은 보내지 않기 대상에서 뺀다
_ANY_URL = re.compile(r"https?://", re.I)
_FAKE_NOTE = re.compile(r"\[\s*브릿지\s*안내")


def is_question_reply(text: str) -> bool:
    """보내지 않아도 되는 질문형 답: 원문에 주소가 없고, 물음표가 있고, 결제 질문이 아니다.
    원문 기준이라 차단 도메인 링크만 있던 답도 '후보가 있던 답'으로 본다(결제 질문 기회 asked_on 보호)."""
    return (not _ANY_URL.search(text) and ("?" in text or "？" in text)
            and PAYMENT_QUESTION_MARK not in text)


def is_links_only(text: str) -> bool:
    """버튼으로 나갈 링크는 있는데 본문이 비어, kakao_reply_body 가 LINKS_ONLY_BODY 를 대신 넣는 답."""
    body, links = split_links(text)
    return not _without_disclosure(body) and any(not is_blocked_link(u) for u, _ in links)


def neutralize_user_text(text: str) -> str:
    """사용자 글 속 "[브릿지 안내" 표식의 대괄호를 없애 진짜 브릿지 안내와 구분되게 한다."""
    return _FAKE_NOTE.sub("브릿지 안내", text)


@dataclass
class Incoming:
    seq: int
    text: str
    images: list[str]
    callback_url: str
    conversation: str
    arrived: float                 # time.monotonic()
    done: bool = False             # 콜백을 이미 불렀는지 (메시지마다 정확히 한 번)


def compose_turn(batch: list[Incoming], note: str = "") -> str:
    """Hermes 에 보낼 글. 메시지가 하나면 원문 그대로, 여럿이면 순서대로 번호를 붙여 합친다.
    사진은 같은 턴의 input_image 로 함께 보낸다(여기서는 "(사진)" 표시만)."""
    if len(batch) == 1:
        m = batch[0]
        body = m.text or ("(사진만 보냄)" if m.images else "")
    else:
        lines = [f"(이어서 보낸 메시지 {len(batch)}개)"]
        for i, m in enumerate(batch, 1):
            lines.append(f"{i}) " + " ".join(x for x in ("(사진)" if m.images else "", m.text) if x))
        body = "\n".join(lines)
    return f"{note}\n{body}" if note else body


@dataclass
class UserQueue:
    pending: list[Incoming]
    timer: asyncio.Task | None = None
    busy: bool = False
    suppressed_prev: bool = False  # 직전 턴의 답을 보내지 않았다 → 다음 턴에 BRIDGE_NOTE
    carry: list[Incoming] = field(default_factory=list)  # 답을 보내지 않은 턴의 메시지(사진 포함). 다음 턴에 다시 넣는다
    epoch: int = 0                 # "처음부터"가 오면 올린다. 처리 중이던 턴의 답은 보내지 않는다


# ---------- app ----------

def _log(path: str, outcome: str, started: float) -> None:
    log.info("path=%s outcome=%s duration_ms=%d", path, outcome, (time.monotonic() - started) * 1000)


def create_app(settings: Settings | None = None, client: httpx.AsyncClient | None = None,
               now: Callable[[], float] = time.time) -> Starlette:
    s = settings or Settings.from_env()
    tasks: set[asyncio.Task] = set()  # 백그라운드 태스크가 GC 되지 않도록 참조 유지
    user_locks = UserLocks()           # 콜백 없는 동기 경로에서만 쓴다
    conversations = Conversations(now=now)
    queues: dict[str, UserQueue] = {}
    seq = iter(range(1, 1 << 62))

    def spawn(coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        tasks.add(t)
        t.add_done_callback(tasks.discard)
        return t

    async def drain() -> None:
        """테스트·종료용: 대기 타이머와 처리 중인 턴이 모두 끝날 때까지 기다린다."""
        while tasks:
            await asyncio.gather(*list(tasks), return_exceptions=True)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        app.state.client = client or httpx.AsyncClient(timeout=httpx.Timeout(HERMES_TIMEOUT_S))
        yield
        if tasks:  # 종료 시 진행 중인 콜백을 잠깐 기다린다
            await asyncio.wait(tasks, timeout=30)
        if client is None:
            await app.state.client.aclose()

    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def send_callback(m: Incoming, body: dict[str, Any], outcome: str) -> None:
        """콜백은 메시지마다 정확히 한 번. 이미 불렀으면 아무것도 하지 않는다."""
        if m.done:
            return
        m.done = True
        try:
            r = await app.state.client.post(m.callback_url, json=body, timeout=CALLBACK_TIMEOUT_S)
            if not r.is_success:
                outcome += f",callback_http_{r.status_code}"
        except Exception as e:
            outcome += f",callback_error:{type(e).__name__}"
        log.info("path=callback seq=%d outcome=%s duration_ms=%d", m.seq, outcome,
                 (time.monotonic() - m.arrived) * 1000)

    def schedule(user_id: str) -> None:
        """대기 중인 메시지를 언제 보낼지 정한다: min(첫 도착 + 최대 대기, 마지막 도착 + 대기)."""
        q = queues[user_id]
        if q.busy or not q.pending:
            return
        if q.timer and not q.timer.done():
            q.timer.cancel()
        first, last = q.pending[0], q.pending[-1]
        photo_start = bool(first.images) and not first.text
        if all(m.images and not m.text for m in q.pending):   # 아직 사진만 왔다: 글이 올 때까지 길게
            wait = s.wait_first_photo_s
        elif len(q.pending) == 1:
            wait = s.wait_first_text_s
        else:
            wait = s.wait_more_s
        wait_max = s.wait_max_photo_s if photo_start else s.wait_max_text_s
        delay = max(0.0, min(first.arrived + wait_max, last.arrived + wait) - time.monotonic())

        async def fire() -> None:
            await asyncio.sleep(delay)
            flush(user_id)
        q.timer = spawn(fire())

    def flush(user_id: str) -> None:
        q = queues.get(user_id)
        if q is None or q.busy or not q.pending:
            return
        batch, q.pending, q.timer, q.busy = q.pending, [], None, True
        log.info("path=batch seq=%d-%d size=%d", batch[0].seq, batch[-1].seq, len(batch))
        spawn(run_turn(user_id, batch))

    async def run_turn(user_id: str, batch: list[Incoming]) -> None:
        q = queues[user_id]
        epoch = q.epoch
        try:
            for m in batch[:-1]:                       # 합쳐진 앞 메시지는 바로 닫는다(답은 맨 끝에 붙는다)
                await send_callback(m, kakao_text_body(CLOSE_MSG), "closed_merged")
            last = batch[-1]
            note = BRIDGE_NOTE if q.suppressed_prev else ""
            turn = [*q.carry, *batch]                  # 답을 보내지 않은 턴의 메시지(이미 콜백을 닫음)를 앞에 다시 넣는다
            q.suppressed_prev, q.carry = False, []
            images = [u for m in turn for u in m.images]  # 사진은 반드시 같은 턴에
            outcome, ok = "ok", True
            try:
                # 타임아웃은 이 턴 마지막 메시지 도착부터 센다. callbackUrl 유효 시간이 대기 중에도 흐르기 때문.
                remaining = max(1.0, HERMES_TIMEOUT_S - (time.monotonic() - last.arrived))
                async with asyncio.timeout(remaining):
                    reply = await ask_hermes(s, app.state.client, user_id, compose_turn(turn, note), images,
                                             last.conversation)
            except TimeoutError:
                reply, outcome, ok = FAIL_MSG, "hermes_timeout", False
            except Exception as e:  # 발화·URL 이 담길 수 있어 예외 메시지는 남기지 않는다
                reply, outcome, ok = FAIL_MSG, f"hermes_error:{type(e).__name__}", False
            if q.epoch != epoch:                       # 처리 중에 "처음부터": 답은 종류와 상관없이 보내지 않는다
                await send_callback(last, kakao_text_body(RESET_DROP_MSG), "dropped_by_reset")
            elif ok and q.pending and is_question_reply(reply):
                q.suppressed_prev, q.carry = True, turn
                await send_callback(last, kakao_text_body(CLOSE_MSG), "suppressed_question")
            else:
                if ok and PAYMENT_QUESTION_MARK in reply and not _ANY_URL.search(reply):
                    outcome += ",payment_q_no_links"   # 결제 질문만 있는 답(후보 없음). 답은 그대로 보내고 로그로 남긴다
                if ok and is_links_only(reply):
                    outcome += ",links_only"           # 본문 없이 링크만 온 답. 기본 문구가 대신 나간다(로그로 남긴다)
                await send_callback(last, kakao_reply_body(reply), outcome)
        finally:
            q.busy = False
            for m in batch:                            # 어떤 경우에도 콜백은 한 번씩 (예외로 빠진 경우 대비)
                await send_callback(m, kakao_text_body(FAIL_MSG), "fail_safe")
            if q.pending:
                schedule(user_id)
            elif not q.timer:
                queues.pop(user_id, None)

    def reset_user(user_id: str) -> None:
        """"처음부터": 대기 중이던 메시지는 닫고 버리고, 처리 중인 턴의 답은 보내지 않게 표시한다."""
        q = queues.get(user_id)
        if q is None:
            return
        if q.timer and not q.timer.done():
            q.timer.cancel()
        q.timer = None
        dropped, q.pending = q.pending, []
        for m in dropped:
            spawn(send_callback(m, kakao_text_body(RESET_DROP_MSG), "dropped_by_reset"))
        q.suppressed_prev, q.carry = False, []
        if q.busy:
            q.epoch += 1
        else:
            queues.pop(user_id, None)

    async def skill(req: Request) -> Response:
        started = time.monotonic()
        if s.kakao_bot_secret and req.query_params.get("secret") != s.kakao_bot_secret:
            _log("skill", "forbidden", started)
            return PlainTextResponse("forbidden", status_code=403)
        try:
            body = json.loads(await req.body())
        except ValueError:
            _log("skill", "bad_json", started)
            return JSONResponse(kakao_text_body(BAD_REQUEST_MSG))
        if not isinstance(body, dict):
            body = {}

        ur = body.get("userRequest") if isinstance(body.get("userRequest"), dict) else {}
        raw_text = str(ur.get("utterance") or "").strip()
        images = extract_image_urls(body)
        text = neutralize_user_text(strip_image_utterance(raw_text, images))
        user = ur.get("user") if isinstance(ur.get("user"), dict) else {}
        user_id = str(user.get("id") or "unknown")
        callback_url = ur.get("callbackUrl")

        # '처음부터'·'새로 시작': Hermes 를 부르지 않고 즉시 새 대화로 바꾼다 (콜백 없이 바로 답한다).
        if is_reset(text) and not images:
            reset_user(user_id)
            conversations.reset(user_id)
            _log("skill", "reset", started)
            return JSONResponse(kakao_text_body(RESET_MSG))

        # 대화 ID 는 Hermes 를 부를 때가 아니라 메시지가 도착한 시점에 정한다.
        conversation, new_conv = conversations.current(user_id)

        # 콜백을 못 쓰는 상황(설정 안 됨)이면 합치지 않고 동기 응답을 시도하되 4.5초에서 끊는다.
        if not callback_url:
            try:
                async with asyncio.timeout(SYNC_TIMEOUT_S):
                    async with user_locks.hold(user_id):
                        reply = await ask_hermes(s, app.state.client, user_id, text, images, conversation)
                outcome = "sync_ok"
            except Exception as e:
                reply, outcome = NO_CALLBACK_MSG, f"sync_fail:{type(e).__name__}"
            _log("sync", outcome, started)
            return JSONResponse(kakao_reply_body(reply))

        # 비동기 경로: 즉시 ack → 잠깐 기다렸다 합쳐서 Hermes → 마지막 메시지의 callbackUrl 로 최종 답
        m = Incoming(next(seq), text, images, str(callback_url), conversation, started)
        q = queues.setdefault(user_id, UserQueue(pending=[]))
        merged = 1 if (q.pending or q.busy) else 0
        q.pending.append(m)
        log.info("path=arrive seq=%d image=%d text_len=%d merged=%d%s", m.seq, 1 if images else 0, len(text),
                 merged, ",new_conversation" if new_conv else "")
        if images and Path(s.utterance_shape_flag).expanduser().exists():
            log.info(utterance_shape_line(m.seq, raw_text, images))
        schedule(user_id)
        return JSONResponse({"version": "2.0", "useCallback": True, "data": {"text": QUICK_ACK}})

    async def not_found(_: Request, __: Exception) -> Response:
        return PlainTextResponse("not found", status_code=404)

    app = Starlette(
        routes=[Route("/health", health, methods=["GET"]), Route("/kakao/skill", skill, methods=["POST"])],
        exception_handlers={404: not_found, 405: not_found},  # Worker 처럼 경로·메서드 불일치는 모두 404
        lifespan=lifespan,
    )
    app.state.tasks = tasks
    app.state.drain = drain
    app.state.queues = queues
    app.state.user_locks = user_locks
    app.state.conversations = conversations
    return app


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # httpx 는 INFO 로 요청 URL 을 찍는다. 카카오 callbackUrl 에 토큰이 들어 있어 WARNING 으로 낮춘다.
    logging.getLogger("httpx").setLevel(logging.WARNING)


if __name__ == "__main__":
    import uvicorn

    configure_logging()
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host="127.0.0.1", port=settings.port, access_log=False)
