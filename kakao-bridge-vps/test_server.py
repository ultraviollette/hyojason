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


# ---------- 링크 → 버튼 ----------
from server import kakao_reply_body, kakao_text_body  # noqa: E402

COUPANG = "https://m.coupang.com/nm/search?q=%EC%97%90%EC%8A%A4%EC%81%98%EC%95%84%20%EC%BF%A0%EC%85%98"
NAVER = "https://m.search.naver.com/search.naver?where=m_shop&query=%EC%97%90%EC%8A%A4%EC%81%98%EC%95%84"
REORDER_REPLY = f"""에스쁘아 비벨벳 커버 쿠션 23.5호 페어 올리브 리필로 찾아봤어요.

쿠팡: {COUPANG}
네이버쇼핑: {NAVER}

볼 것 세 가지예요.
1) 23.5호 페어 올리브가 맞는지
2) 리필인지
3) 언제 도착하는지

재고랑 도착일은 제가 확인하지 못했어요."""


def _texts(body: dict) -> list[str]:
    out = []
    for o in body["template"]["outputs"]:
        out.append(o["simpleText"]["text"] if "simpleText" in o else o["textCard"]["description"])
    return out


def _buttons(body: dict) -> list[tuple[str, str]]:
    card = body["template"]["outputs"][-1]["textCard"]
    return [(b["label"], b["webLinkUrl"]) for b in card["buttons"] if b["action"] == "webLink"]


def test_links_become_buttons_and_leave_the_text() -> None:
    body = kakao_reply_body(REORDER_REPLY)
    assert len(body["template"]["outputs"]) == 1
    assert _buttons(body) == [("쿠팡에서 보기", COUPANG), ("네이버에서 보기", NAVER)]
    text = _texts(body)[0]
    assert "http" not in text and "%EC" not in text
    assert "쿠팡:" not in text and "네이버쇼핑:" not in text  # 링크만 있던 줄은 지운다
    assert text.startswith("에스쁘아") and "볼 것 세 가지예요." in text


def test_reply_without_links_is_unchanged() -> None:
    plain = "에스쁘아 비벨벳 커버 쿠션 23.5호 페어 올리브 맞으세요?"
    assert kakao_reply_body(plain) == kakao_text_body(plain)


def test_long_reply_with_links_respects_kakao_limits() -> None:
    long_body = "\n".join(f"{i}) 확인할 점이 있어요. 호수와 용량을 꼭 보세요." for i in range(40))
    body = kakao_reply_body(f"{long_body}\n쿠팡: {COUPANG}")
    outputs = body["template"]["outputs"]
    assert len(outputs) <= 3                                   # 말풍선 최대 3개
    assert len(outputs[-1]["textCard"]["description"]) <= 400  # textCard 단일형 400자
    assert all("http" not in t for t in _texts(body))
    assert _buttons(body) == [("쿠팡에서 보기", COUPANG)]


def test_product_links_numbered_markdown_and_trailing_punctuation() -> None:
    reply = ("1) 보디수트 · 18,900원\n2) 레오타드 · 24,500원\n"
             "👉 1번 사러 가기: https://www.coupang.com/vp/products/111.\n"
             "[2번 사러 가기](https://www.coupang.com/vp/products/222)\n"
             "다른 곳: https://smartstore.naver.com/x/products/3 https://example.com/4")
    body = kakao_reply_body(reply)
    assert _buttons(body) == [
        ("1번 쿠팡에서 보기", "https://www.coupang.com/vp/products/111"),
        ("2번 쿠팡에서 보기", "https://www.coupang.com/vp/products/222"),
        ("네이버에서 보기", "https://smartstore.naver.com/x/products/3"),
    ]  # 버튼은 최대 3개
    assert _texts(body) == ["1) 보디수트 · 18,900원\n2) 레오타드 · 24,500원"]


def test_callback_sends_buttons() -> None:
    async def run() -> None:
        callbacks: list[dict] = []

        async def handler(req: httpx.Request) -> httpx.Response:
            if req.url.host == "hermes.test":
                return httpx.Response(200, json={"output_text": REORDER_REPLY})
            callbacks.append(json.loads(req.content))
            return httpx.Response(200)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app = create_app(Settings(hermes_api_url=HERMES), client=client)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bridge") as bridge:
                await bridge.post("/kakao/skill", json={"userRequest": {"utterance": "네 리필이요",
                                                                       "callbackUrl": CALLBACK, "user": {"id": "u"}}})
                await asyncio.wait_for(asyncio.gather(*app.state.tasks), timeout=2)
        await client.aclose()
        assert _buttons(callbacks[0]) == [("쿠팡에서 보기", COUPANG), ("네이버에서 보기", NAVER)]

    asyncio.run(run())


# make_search_links("에스쁘아 비벨벳 커버 쿠션 23.5호 페어 올리브 리필") 의 실제 출력 (쿠팡은 모바일 주소)
REAL_COUPANG = ("https://m.coupang.com/nm/search?q=%EC%97%90%EC%8A%A4%EC%81%98%EC%95%84%20%EB%B9%84%EB%B2%A8%EB%B2%B3"
                "%20%EC%BB%A4%EB%B2%84%20%EC%BF%A0%EC%85%98%2023.5%ED%98%B8%20%ED%8E%98%EC%96%B4%20%EC%98%AC%EB%A6%AC%EB%B8%8C"
                "%20%EB%A6%AC%ED%95%84")
REAL_NAVER = ("https://m.search.naver.com/search.naver?where=m_shop&query=%EC%97%90%EC%8A%A4%EC%81%98%EC%95%84%20%EB%B9%84"
              "%EB%B2%A8%EB%B2%B3%20%EC%BB%A4%EB%B2%84%20%EC%BF%A0%EC%85%98%2023.5%ED%98%B8%20%ED%8E%98%EC%96%B4%20%EC%98%AC"
              "%EB%A6%AC%EB%B8%8C%20%EB%A6%AC%ED%95%84")


def test_button_url_is_byte_identical_to_tool_output() -> None:
    """쿠팡 버튼이 '사용권한 없음'을 띄운 건(카카오 실사용) 브릿지가 URL 을 바꿔서가 아니다.
    도구가 만든 URL 이 추출·직렬화를 거쳐도 글자 하나 다르지 않음을 고정한다."""
    variants = [
        f"찾아봤어요.\n\n쿠팡: {REAL_COUPANG}\n네이버쇼핑: {REAL_NAVER}",
        f"찾아봤어요.\n쿠팡: {REAL_COUPANG}.\n네이버쇼핑: <{REAL_NAVER}>",       # 끝 마침표, 꺾쇠
        f"찾아봤어요.\n[쿠팡]({REAL_COUPANG})\n[네이버]({REAL_NAVER})",           # 마크다운 링크
    ]
    for reply in variants:
        wire = json.loads(json.dumps(kakao_reply_body(reply), ensure_ascii=False))  # 콜백 POST 본문 그대로
        assert _buttons(wire) == [("쿠팡에서 보기", REAL_COUPANG), ("네이버에서 보기", REAL_NAVER)], reply


# ---------- 모르는 도메인의 버튼 이름 ----------
from server import site_name  # noqa: E402


def test_unknown_site_uses_name_written_on_the_link_line() -> None:
    reply = ("이 상품 같아요. 23.5호·리필 옵션이 있는지 꼭 확인하세요.\n\n"
             "에스쁘아 공식몰: https://www.espoir.com/product/detail?prdNo=123\n"
             f"네이버쇼핑: {REAL_NAVER}")
    body = kakao_reply_body(reply)
    assert _buttons(body) == [("에스쁘아 공식몰에서 보기", "https://www.espoir.com/product/detail?prdNo=123"),
                              ("네이버에서 보기", REAL_NAVER)]
    assert "공식몰" not in _texts(body)[0]  # 링크 줄은 본문에서 빠진다


def test_site_name_fallbacks() -> None:
    assert site_name("https://shop.espoir.co.kr/p/1") == "espoir"            # 이름이 없으면 도메인에서
    assert site_name("https://m.amoremall.com/kr/ko/product/detail") == "amoremall"
    assert site_name("https://brand.naver.com/espoir/products/1", "에스쁘아 브랜드관") == "네이버"  # 아는 쇼핑몰이 우선
    assert site_name("https://www.espoir.com/p/1", "👉 1번 사러 가기") == "espoir"  # 안내 문구는 이름으로 안 씀


def test_labels_fit_kakao_14_char_limit() -> None:
    # 10자 이름 + "에서 보기" = 15자 → 이름을 잘라 14자에 맞춘다
    long_name = kakao_reply_body("후보예요.\n에스쁘아공식온라인몰: https://www.espoir.com/p/1")
    assert _buttons(long_name) == [("에스쁘아공식온라인에서 보기", "https://www.espoir.com/p/1")]

    # 같은 사이트가 여럿이면 번호를 붙이고, 번호까지 넣어 14자에 맞춘다
    reply = ("후보예요.\n"
             "아주아주긴브랜드공식온라인몰: https://a.example.com/p/1\n"   # 10자 넘는 이름은 안 쓰고 도메인으로
             "[2번 사러 가기](https://b.example.com/p/2)\n"            # 안내 문구도 이름으로 안 씀
             "https://b.example.com/p/3")
    labels = [label for label, _ in _buttons(kakao_reply_body(reply))]
    assert labels == ["1번 exampl에서 보기", "2번 exampl에서 보기", "3번 exampl에서 보기"]
    assert all(len(label) <= 14 for label in labels)
