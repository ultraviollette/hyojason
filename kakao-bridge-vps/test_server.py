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


def test_same_user_messages_are_processed_in_order() -> None:
    """사진과 글을 연달아 보내도 같은 사용자의 Hermes 호출은 하나씩, 도착 순서대로 간다.
    다른 사용자는 기다리지 않는다."""
    async def run() -> None:
        active: dict[str, int] = {}
        max_active: dict[str, int] = {}
        order: list[str] = []
        callbacks: list[tuple[str, str]] = []
        gates = {"u1": asyncio.Event(), "u2": asyncio.Event()}

        async def handler(req: httpx.Request) -> httpx.Response:
            if req.url.host == "hermes.test":
                body = json.loads(req.content)
                uid = body["metadata"]["channel_user_id"]
                said = body["input"][0]["content"][0]["text"].split("\n", 1)[1]
                active[uid] = active.get(uid, 0) + 1
                max_active[uid] = max(max_active.get(uid, 0), active[uid])
                order.append(f"{uid}:{said}")
                if said == "사진":
                    await gates[uid].wait()  # 첫 메시지(사진 해석)가 오래 걸린다
                active[uid] -= 1
                return httpx.Response(200, json={"output_text": f"re:{said}"})
            callbacks.append((req.url.path, json.loads(req.content)["template"]["outputs"][0]["simpleText"]["text"]))
            return httpx.Response(200)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app = create_app(Settings(hermes_api_url=HERMES), client=client)

        def msg(uid: str, said: str, n: int) -> dict:
            return {"userRequest": {"utterance": said, "callbackUrl": f"http://kakao.test/cb/{uid}/{n}",
                                    "user": {"id": uid}}}

        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bridge") as bridge:
                for m in (msg("u1", "사진", 1), msg("u1", "이거 또 사고 싶어", 2), msg("u2", "사진", 1), msg("u2", "양말", 2)):
                    r = await bridge.post("/kakao/skill", json=m)
                    assert r.json()["useCallback"] is True  # 기다리는 메시지도 즉시 ack
                await asyncio.sleep(0.05)
                # u1 의 두 번째 메시지는 첫 번째가 끝날 때까지 Hermes 로 가지 않는다. u2 도 마찬가지로 독립적.
                assert order == ["u1:사진", "u2:사진"]
                gates["u1"].set()
                await asyncio.sleep(0.05)
                assert order == ["u1:사진", "u2:사진", "u1:이거 또 사고 싶어"]
                gates["u2"].set()
                await asyncio.wait_for(asyncio.gather(*app.state.tasks), timeout=2)

        assert max_active == {"u1": 1, "u2": 1}
        assert [c for c in callbacks if "/u1/" in c[0]] == [("/cb/u1/1", "re:사진"), ("/cb/u1/2", "re:이거 또 사고 싶어")]
        assert len(app.state.user_locks) == 0  # 끝난 사용자의 락은 정리된다
        await client.aclose()

    asyncio.run(run())
