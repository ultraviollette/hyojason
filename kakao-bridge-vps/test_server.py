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
                assert sent["body"]["conversation"].startswith("kakao-u1-")  # 사용자별 + 대화 세션별
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

    # 후보 번호는 링크 줄에 적힌 것만 쓰고, 번호까지 넣어 14자에 맞춘다. 번호 없는 링크에는 지어내지 않는다.
    reply = ("후보예요.\n"
             "아주아주긴브랜드공식온라인몰: https://a.example.com/p/1\n"   # 10자 넘는 이름은 안 쓰고 도메인으로, 번호 없음
             "[2번 사러 가기](https://b.example.com/p/2)\n"            # 후보 번호 2, 안내 문구는 이름으로 안 씀
             "https://b.example.com/p/3")                              # 번호 없음, 앞 이름표와 겹쳐 " 2"
    labels = [label for label, _ in _buttons(kakao_reply_body(reply))]
    assert labels == ["example에서 보기", "2번 exampl에서 보기", "example에서 보기 2"]
    assert all(len(label) <= 14 for label in labels)


# ---------- 대화 세션 (30분 / 처음부터 / 재시작) ----------
from server import RESET_MSG  # noqa: E402


class _Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


def _session_harness(clock: _Clock):
    """가짜 Hermes 가 받은 conversation 을 기록한다. send(text) → (카카오 즉시 응답, 그 요청의 conversation 또는 None)"""
    seen: list[str] = []

    async def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "hermes.test":
            seen.append(json.loads(req.content)["conversation"])
            return httpx.Response(200, json={"output_text": "네"})
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_app(Settings(hermes_api_url=HERMES), client=client, now=clock)

    async def send(bridge: httpx.AsyncClient, text: str):
        before = len(seen)
        r = await bridge.post("/kakao/skill", json={"userRequest": {
            "utterance": text, "callbackUrl": CALLBACK, "user": {"id": "mom"}}})
        await asyncio.wait_for(asyncio.gather(*app.state.tasks), timeout=2)
        return r.json(), (seen[-1] if len(seen) > before else None)

    return app, client, send


def _run_session(steps):
    async def run() -> None:
        clock = _Clock()
        app, client, send = _session_harness(clock)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bridge") as bridge:
                await steps(clock, lambda text: send(bridge, text))
        await client.aclose()
    asyncio.run(run())


def test_conversation_continues_within_30_minutes_then_starts_new() -> None:
    async def steps(clock, send):
        _, c1 = await send("사진")
        clock.t += 29 * 60 + 59               # 마지막 메시지로부터 29분 59초
        _, c2 = await send("리필이요")
        clock.t += 30 * 60                    # 마지막 메시지로부터 30분
        _, c3 = await send("이거 또 사고 싶어")
        assert c1.startswith("kakao-mom-")
        assert c1 == c2                       # 30분 안에는 이어서
        assert c3 != c2                       # 30분 지나면 새 대화
    _run_session(steps)


def test_reset_words_switch_conversation_immediately_without_hermes() -> None:
    async def steps(clock, send):
        _, c1 = await send("사진")
        for word in ("처음부터", "새로 시작", " 처음부터! ", "새로시작."):
            clock.t += 1
            reply, called = await send(word)
            assert called is None                                   # Hermes 를 부르지 않는다
            assert "useCallback" not in reply                       # 콜백 없이 바로 답한다
            assert reply["template"]["outputs"][0]["simpleText"]["text"] == RESET_MSG
        _, c2 = await send("양말 사고 싶어")
        assert c2 != c1                                             # 30분이 안 지났어도 새 대화
        _, c3 = await send("처음부터 다시 찾아 줘")                  # 문장 속 '처음부터'는 초기화가 아니다
        assert c3 == c2
    _run_session(steps)


def test_bridge_restart_starts_new_conversation() -> None:
    """상태는 메모리에만 있다. 재시작(새 앱)하면 30분 안이어도 새 대화로 시작하고, 옛 대화로 돌아가지 않는다."""
    convs = []

    async def steps(clock, send):
        convs.append((await send("사진"))[1])

    _run_session(steps)
    _run_session(steps)  # 재시작
    assert convs[0] != convs[1]


# ---------- 후보로 내면 안 되는 판매처: 크라우드펀딩 링크는 버튼으로 나가지 않는다 ----------
from server import NO_LINK_MSG, is_blocked_link  # noqa: E402

WADIZ = "https://www.wadiz.kr/web/campaign/detail/123456"
TUMBLBUG = "https://tumblbug.com/hyojason-cushion"
SHOP = "https://smartstore.naver.com/x/products/1"


def test_crowdfunding_links_are_not_buttons_and_do_not_take_a_slot() -> None:
    reply = ("후보예요.\n"
             f"와디즈: {WADIZ}\n"
             f"텀블벅: {TUMBLBUG}\n"
             f"스마트스토어: {SHOP}\n"
             "에스쁘아 공식몰: https://www.espoir.com/p/1\n"
             "11번가: https://www.11st.co.kr/products/9")
    body = kakao_reply_body(reply)
    assert _buttons(body) == [("네이버에서 보기", SHOP),
                              ("에스쁘아 공식몰에서 보기", "https://www.espoir.com/p/1"),
                              ("11번가에서 보기", "https://www.11st.co.kr/products/9")]  # 막힌 2개는 3칸을 차지하지 않는다
    wire = json.dumps(body, ensure_ascii=False)
    assert "wadiz" not in wire and "tumblbug" not in wire


def test_only_blocked_links_sends_text_without_the_urls() -> None:
    body = kakao_reply_body(f"이 상품 같아요.\n와디즈: {WADIZ}\n[텀블벅]({TUMBLBUG})")
    assert body == kakao_text_body("이 상품 같아요.")          # 버튼 없이, URL 도 글자로 새지 않는다
    assert kakao_reply_body(f"와디즈: {WADIZ}") == kakao_text_body(NO_LINK_MSG)


def test_blocked_domain_matching() -> None:
    for url in (WADIZ, "https://wadiz.kr/x", "https://m.wadiz.kr/x", "HTTPS://WWW.WADIZ.KR/x",
                "https://user@wadiz.kr/x", "https://wadiz.kr./x", TUMBLBUG, "https://www.tumblbug.com/x"):
        assert is_blocked_link(url), url
    for url in ("https://notwadiz.kr/x", "https://wadiz.kr.example.com/x", "https://example.com/wadiz.kr",
                "https://mytumblbug.com/x", SHOP):
        assert not is_blocked_link(url), url


# ---------- 버튼 이름표는 본문 후보 번호로 시작한다 ----------
from server import LABEL_MAX, candidate_number  # noqa: E402

HIMART = "https://www.e-himart.co.kr/app/goods/goodsDetail?goodsNo=0001"
ST_M1 = "https://www.11st.co.kr/products/1111"
ST_S = "https://www.11st.co.kr/products/2222"
KETTLE_BODY = ("전기 포트 세 개 찾았어요.\n"
               "1) 위즈웰 접이식 전기 포트\n2) 보아르 이지전기포트M1\n3) 보아르 이지전기포트S 500ml\n"
               "가격·재고·도착일은 링크에서 꼭 확인하세요.\n\n")


def test_real_case_buttons_start_with_candidate_number() -> None:
    """2026-11-18 실사용: 본문 후보 1~3인데 버튼이 'e-himart에서 보기', '1번 11번가', '2번 11번가'로 나갔다
    (같은 쇼핑몰 안의 순번). 링크 줄에 후보 번호를 적으면 그 번호로 이름표가 시작한다."""
    reply = KETTLE_BODY + f"1번 하이마트: {HIMART}\n2번 11번가: {ST_M1}\n3번 11번가: {ST_S}"
    body = kakao_reply_body(reply)
    assert _buttons(body) == [("1번 하이마트에서 보기", HIMART), ("2번 11번가에서 보기", ST_M1),
                              ("3번 11번가에서 보기", ST_S)]
    assert "하이마트:" not in _texts(body)[0] and "3) 보아르 이지전기포트S 500ml" in _texts(body)[0]


def test_without_candidate_numbers_no_number_is_invented() -> None:
    """링크 줄에 번호가 없으면(오늘 실제 답처럼) 후보 번호를 추측하지 않는다. 같은 이름표만 ' 2'로 구분한다."""
    reply = KETTLE_BODY + f"하이마트: {HIMART}\n11번가: {ST_M1}\n11번가: {ST_S}"
    labels = [label for label, _ in _buttons(kakao_reply_body(reply))]
    assert labels == ["하이마트에서 보기", "11번가에서 보기", "11번가에서 보기 2"]
    assert not any(label[0].isdigit() and "번 " in label[:3] for label in labels)


def test_candidate_number_formats_and_11st_is_not_a_number() -> None:
    assert candidate_number("1번 하이마트") == (1, "하이마트")
    assert candidate_number("👉 2번 사러 가기") == (2, "사러 가기")
    assert candidate_number("3) 11번가") == (3, "11번가")
    assert candidate_number("1. 에스쁘아 공식몰") == (1, "에스쁘아 공식몰")
    assert candidate_number("2번") == (2, None)
    assert candidate_number("11번가") == (None, "11번가")     # 11번가의 '11번'은 후보 번호가 아니다
    assert candidate_number("하이마트") == (None, "하이마트")
    assert candidate_number(None) == (None, None)


def test_numbered_labels_fit_14_chars_and_keep_number_after_blocked_link() -> None:
    reply = ("후보예요.\n"
             "1번 에스쁘아공식온라인몰: https://www.espoir.com/p/1\n"     # 이름 10자 → 번호까지 넣어 14자에 맞춤
             "2번 와디즈: https://www.wadiz.kr/web/campaign/detail/1\n"  # 막힌 링크: 버튼 없음, 번호는 당기지 않는다
             f"3번 11번가: {ST_S}")
    buttons = _buttons(kakao_reply_body(reply))
    assert buttons == [("1번 에스쁘아공식에서 보기", "https://www.espoir.com/p/1"), ("3번 11번가에서 보기", ST_S)]
    assert all(len(label) <= LABEL_MAX == 14 for label, _ in buttons)


def test_quick_ack_text_is_exact() -> None:
    assert QUICK_ACK == "가게 몇 군데 돌아보고 올게요. 2~3분쯤 걸려요.\n그동안 다른 일 보고 계세요. 찾으면 바로 알려 드릴게요 🐾"
