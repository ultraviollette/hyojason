"""효자손 카카오 브릿지 (VPS 상시 프로세스판)

kakao-bridge/src/index.ts (Cloudflare Worker) 의 동작을 그대로 옮긴 것.
Worker 의 waitUntil 30초 제한 때문에 Hermes 응답이 길면 콜백이 취소돼서,
같은 다리를 VPS 에서 상시 프로세스로 돌린다.

흐름
 1. 카카오가 POST /kakao/skill 로 사용자 발화(+사진)를 보낸다. 스킬 서버는 5초 안에 응답해야 한다.
 2. 즉시 { useCallback: true } 로 답하고, 백그라운드 asyncio 태스크에서 Hermes 에게 묻는다.
 3. Hermes 답을 userRequest.callbackUrl 로 POST 한다 (유효 5분, 1회).

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
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

log = logging.getLogger("hyojason.bridge")

QUICK_ACK = "네, 찾아보고 있어요. 잠시만 기다려 주세요 🙂"
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
_LINK_RAW = re.compile(r"<?(https?://[^\s<>\"']+)>?")
_TRAIL = ".,;:!?)]}>'\"」』"
_LEFTOVER_MAX = 14   # URL 을 뺀 뒤 이 길이 이하만 남은 줄("쿠팡:", "👉 1번 사러 가기:")은 지운다
MAX_BUTTONS = 3
CARD_DESC_MAX = 380  # textCard 단일형 400자 제한에 여유
_SITES = (("coupang.com", "쿠팡"), ("naver.com", "네이버"), ("naver.me", "네이버"),
          ("11st.co.kr", "11번가"), ("gmarket.co.kr", "G마켓"))


LABEL_MAX = 14       # 카카오 버튼 문구 최대 14자
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


def split_links(text: str) -> tuple[str, list[tuple[str, str | None]]]:
    """본문에서 URL 을 빼고 (본문, [(url, 그 줄에 적힌 이름)…]) 을 돌려준다. URL 순서 유지, 중복 제거."""
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


def _fit(name: str, prefix: str = "") -> str:
    room = LABEL_MAX - len(prefix) - len(_LABEL_TAIL)
    return f"{prefix}{name[:max(room, 1)]}{_LABEL_TAIL}"


def link_buttons(links: list[tuple[str, str | None]]) -> list[dict[str, str]]:
    links = links[:MAX_BUTTONS]
    names = [site_name(u, h) for u, h in links]
    buttons = []
    for i, ((u, _), name) in enumerate(zip(links, names)):
        prefix = f"{names[:i + 1].count(name)}번 " if names.count(name) > 1 else ""
        buttons.append({"action": "webLink", "label": _fit(name, prefix), "webLinkUrl": u})
    return buttons


def kakao_reply_body(text: str) -> dict[str, Any]:
    """Hermes 답변 → 카카오 응답. 링크가 없으면 simpleText 그대로, 있으면 본문은 글로, 링크는 버튼으로."""
    body, links = split_links(text)
    if not links:
        return kakao_text_body(text)
    chunks = split_text(body, CARD_DESC_MAX) if body else []
    *head, last = chunks or ["아래 버튼을 눌러 열어 보세요."]
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


# ---------- app ----------

def _log(path: str, outcome: str, started: float) -> None:
    log.info("path=%s outcome=%s duration_ms=%d", path, outcome, (time.monotonic() - started) * 1000)


def create_app(settings: Settings | None = None, client: httpx.AsyncClient | None = None,
               now: Callable[[], float] = time.time) -> Starlette:
    s = settings or Settings.from_env()
    tasks: set[asyncio.Task] = set()  # 백그라운드 태스크가 GC 되지 않도록 참조 유지
    user_locks = UserLocks()
    conversations = Conversations(now=now)

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

    async def callback_job(callback_url: str, user_id: str, text: str, images: list[str], started: float,
                           conversation: str, new_conv: bool) -> None:
        c: httpx.AsyncClient = app.state.client
        outcome = "ok,new_conversation" if new_conv else "ok"
        try:
            # 타임아웃은 앞 메시지를 기다린 시간까지 포함한다. callbackUrl 유효 시간이 대기 중에도 흐르기 때문.
            async with asyncio.timeout(HERMES_TIMEOUT_S):
                async with user_locks.hold(user_id):
                    reply = await ask_hermes(s, c, user_id, text, images, conversation)
        except TimeoutError:
            reply, outcome = FAIL_MSG, "hermes_timeout"
        except Exception as e:  # 발화·URL 이 담길 수 있어 예외 메시지는 남기지 않는다
            reply, outcome = FAIL_MSG, f"hermes_error:{type(e).__name__}"
        try:
            r = await c.post(callback_url, json=kakao_reply_body(reply), timeout=CALLBACK_TIMEOUT_S)
            if not r.is_success:
                outcome += f",callback_http_{r.status_code}"
        except Exception as e:
            outcome += f",callback_error:{type(e).__name__}"
        _log("callback", outcome, started)

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
        text = str(ur.get("utterance") or "").strip()
        images = extract_image_urls(body)
        user = ur.get("user") if isinstance(ur.get("user"), dict) else {}
        user_id = str(user.get("id") or "unknown")
        callback_url = ur.get("callbackUrl")

        # '처음부터'·'새로 시작': Hermes 를 부르지 않고 즉시 새 대화로 바꾼다 (콜백 없이 바로 답한다).
        if is_reset(text) and not images:
            conversations.reset(user_id)
            _log("skill", "reset", started)
            return JSONResponse(kakao_text_body(RESET_MSG))

        # 대화 ID 는 Hermes 를 부를 때가 아니라 메시지가 도착한 시점에 정한다.
        # (처음부터 앞에 대기 중이던 메시지는 옛 대화로 마무리된다)
        conversation, new_conv = conversations.current(user_id)

        # 콜백을 못 쓰는 상황(설정 안 됨)이면 동기 응답을 시도하되 4.5초에서 끊는다.
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

        # 비동기 경로: 즉시 ack → 백그라운드에서 Hermes → callbackUrl 로 최종 답
        t = asyncio.create_task(callback_job(str(callback_url), user_id, text, images, started,
                                             conversation, new_conv))
        tasks.add(t)
        t.add_done_callback(tasks.discard)
        return JSONResponse({"version": "2.0", "useCallback": True, "data": {"text": QUICK_ACK}})

    async def not_found(_: Request, __: Exception) -> Response:
        return PlainTextResponse("not found", status_code=404)

    app = Starlette(
        routes=[Route("/health", health, methods=["GET"]), Route("/kakao/skill", skill, methods=["POST"])],
        exception_handlers={404: not_found, 405: not_found},  # Worker 처럼 경로·메서드 불일치는 모두 404
        lifespan=lifespan,
    )
    app.state.tasks = tasks
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
