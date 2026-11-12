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
                     image_urls: list[str]) -> str:
    """Hermes Agent 의 OpenAI 호환 API (/v1/responses) 호출. payload 는 Worker 와 동일."""
    prefix = f"[channel=kakao user_id={kakao_user_id}]"
    content: list[dict[str, str]] = [
        {"type": "input_text", "text": f"{prefix}\n{text}" if text else f"{prefix}\n(사진만 보냄)"}
    ]
    content += [{"type": "input_image", "image_url": u} for u in image_urls]

    payload: dict[str, Any] = {
        "input": [{"role": "user", "content": content}],
        "conversation": f"kakao-{kakao_user_id}",
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


# ---------- app ----------

def _log(path: str, outcome: str, started: float) -> None:
    log.info("path=%s outcome=%s duration_ms=%d", path, outcome, (time.monotonic() - started) * 1000)


def create_app(settings: Settings | None = None, client: httpx.AsyncClient | None = None) -> Starlette:
    s = settings or Settings.from_env()
    tasks: set[asyncio.Task] = set()  # 백그라운드 태스크가 GC 되지 않도록 참조 유지
    user_locks = UserLocks()

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

    async def callback_job(callback_url: str, user_id: str, text: str, images: list[str], started: float) -> None:
        c: httpx.AsyncClient = app.state.client
        outcome = "ok"
        try:
            # 타임아웃은 앞 메시지를 기다린 시간까지 포함한다. callbackUrl 유효 시간이 대기 중에도 흐르기 때문.
            async with asyncio.timeout(HERMES_TIMEOUT_S):
                async with user_locks.hold(user_id):
                    reply = await ask_hermes(s, c, user_id, text, images)
        except TimeoutError:
            reply, outcome = FAIL_MSG, "hermes_timeout"
        except Exception as e:  # 발화·URL 이 담길 수 있어 예외 메시지는 남기지 않는다
            reply, outcome = FAIL_MSG, f"hermes_error:{type(e).__name__}"
        try:
            r = await c.post(callback_url, json=kakao_text_body(reply), timeout=CALLBACK_TIMEOUT_S)
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

        # 콜백을 못 쓰는 상황(설정 안 됨)이면 동기 응답을 시도하되 4.5초에서 끊는다.
        if not callback_url:
            try:
                async with asyncio.timeout(SYNC_TIMEOUT_S):
                    async with user_locks.hold(user_id):
                        reply = await ask_hermes(s, app.state.client, user_id, text, images)
                outcome = "sync_ok"
            except Exception as e:
                reply, outcome = NO_CALLBACK_MSG, f"sync_fail:{type(e).__name__}"
            _log("sync", outcome, started)
            return JSONResponse(kakao_text_body(reply))

        # 비동기 경로: 즉시 ack → 백그라운드에서 Hermes → callbackUrl 로 최종 답
        t = asyncio.create_task(callback_job(str(callback_url), user_id, text, images, started))
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
