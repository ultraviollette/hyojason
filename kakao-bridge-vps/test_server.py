"""가짜 Hermes 와 가짜 callbackUrl 로 "즉시 useCallback 반환 → 나중에 콜백 POST" 를 확인한다."""
import asyncio
import json

import httpx

from server import QUICK_ACK, Settings, create_app

HERMES = "http://hermes.test"
CALLBACK = "http://kakao.test/callback/abc"


def test_immediate_ack_then_callback() -> None:
    async def run() -> None:
        release = asyncio.Event()
        hermes_calls: list[dict] = []
        callbacks: list[dict] = []

        async def handler(req: httpx.Request) -> httpx.Response:
            if req.url.host == "hermes.test":
                hermes_calls.append({"auth": req.headers["authorization"], "body": json.loads(req.content)})
                await release.wait()  # Hermes 가 오래 걸리는 상황
                return httpx.Response(200, json={"output_text": "1) 보디수트 · 18,900원"})
            if req.url.host == "kakao.test":
                callbacks.append(json.loads(req.content))
                return httpx.Response(200, json={"taskId": "x", "status": "SUCCESS"})
            return httpx.Response(404)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app = create_app(Settings(hermes_api_url=HERMES, hermes_api_key="k", kakao_bot_secret="s"), client=client)
        body = {"userRequest": {"utterance": "살색 내의", "callbackUrl": CALLBACK, "user": {"id": "u1", "type": "botUserKey"}}}

        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bridge") as bridge:
                assert (await bridge.post("/kakao/skill?secret=wrong", json=body)).status_code == 403

                r = await bridge.post("/kakao/skill?secret=s", json=body)
                # 1) Hermes 가 끝나기 전에 즉시 useCallback 응답
                assert r.status_code == 200
                assert r.json() == {"version": "2.0", "useCallback": True, "data": {"text": QUICK_ACK}}
                await asyncio.sleep(0.05)
                assert len(hermes_calls) == 1 and callbacks == []

                # Hermes payload 는 Worker 와 동일
                sent = hermes_calls[0]
                assert sent["auth"] == "Bearer k"
                assert sent["body"]["conversation"] == "kakao-u1"
                assert sent["body"]["metadata"] == {"channel": "kakao", "channel_user_id": "u1"}
                assert sent["body"]["input"][0]["content"][0]["text"] == "[channel=kakao user_id=u1]\n살색 내의"

                # 2) Hermes 가 끝나면 callbackUrl 로 최종 답 POST
                release.set()
                await asyncio.wait_for(asyncio.gather(*app.state.tasks), timeout=2)
                assert callbacks == [{"version": "2.0", "template": {"outputs": [{"simpleText": {"text": "1) 보디수트 · 18,900원"}}]}}]
        await client.aclose()

    asyncio.run(run())


def test_callback_url_token_not_logged(caplog) -> None:
    """httpx INFO 로그('HTTP Request: POST <callbackUrl>')에 카카오 토큰이 찍히지 않는다."""
    import logging

    from server import configure_logging

    configure_logging()
    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING

    async def run() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={})))
        await client.post("http://kakao.test/callback/SECRET-TOKEN-123", json={})
        await client.aclose()

    with caplog.at_level(logging.INFO):  # 루트는 INFO. httpx 로거만 WARNING 이어야 통과한다
        asyncio.run(run())
    assert "SECRET-TOKEN-123" not in caplog.text
