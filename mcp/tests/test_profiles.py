"""프로필: 스키마를 코드로 강제하고, 저장 → 새 대화에서 resolve → 값이 그대로인지 확인한다.

실제 사례: VPS clients/ 의 두 파일이 _example.yaml 스키마가 아니었다(에이전트가 저장할 때마다 키를 지어냄).
 A) 파일명 = 카카오 사용자 ID: name, channel, measurements.clothing_size(리스트), shipping.default_region,
    preferences.activities(리스트), avoid …
 B) client-20261112120539.yaml: name, channel, channel_user_id, shipping_address.city, preferences, shopping_rules …
B 는 resolve 에 실패했다. 아래 샘플은 키 구조만 같고 값은 가짜다.
"""
import yaml
import pytest

from shopping_tools import migrate_profiles, profiles, server

KAKAO_ID = "a1b2c3d4e5f6"
STANDARD_KEYS = set(profiles.empty_profile())

SAMPLE_A = {  # 파일 이름 = 카카오 user_id
    "client_id": "kakaoStemUserId123", "name": "어머니", "channel": "kakao",
    "measurements": {"clothing_size": ["55", "M"]}, "shipping": {"default_region": "경북 구미"},
    "preferences": {"activities": ["라인댄스"]}, "avoid": ["목 파인 옷"], "history": [], "updated_at": "2026-11-11",
}
SAMPLE_B = {
    "name": "엄마", "channel": "kakao", "channel_user_id": "kakaoUserB999",
    "shipping_address": {"city": "전주", "street": "가짜로 1"}, "preferences": {"likes": ["밝은 색"], "style": "여성스러운"},
    "shopping_rules": {"payment": "self", "budget": "3만원"}, "client_id": "client-20261112120539",
    "history": [{"at": "2026-11-12", "request": {"item": "생활한복"}}], "updated_at": "2026-11-12",
}


@pytest.fixture(autouse=True)
def tmp_clients(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "CLIENTS_DIR", tmp_path)
    return tmp_path


def _write(dirpath, name, data):
    (dirpath / name).write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


# ---------- 왕복 ----------

def test_save_then_resolve_in_new_conversation_keeps_values(tmp_clients) -> None:
    saved = server.save_client_profile(
        "kakao", KAKAO_ID, display_name="어머니", top_size="55", bottom_size="M", shoe_size_mm="235",
        delivery_region="경북 구미", likes=["여성스러운 핏"], dislikes=["목 파인 옷"], notes=["라인댄스를 함"])
    assert saved["saved"] and saved["created"]
    # 파일 이름 규칙은 하나: <채널>-<sha256 앞 16자>.yaml, 원래 user_id 는 파일 이름에 없다
    assert [p.name for p in tmp_clients.glob("*.yaml")] == [f"{profiles.client_id_for('kakao', KAKAO_ID)}.yaml"]
    assert KAKAO_ID not in saved["client_id"]

    # 새 대화: 메시지 앞의 [channel=kakao user_id=...] 만 안다
    r = server.resolve_client("kakao", KAKAO_ID)
    assert r["found"] and r["client_id"] == saved["client_id"]
    p = r["profile"]
    assert p["display_name"] == "어머니"
    assert p["body"] == {"top_size": "55", "bottom_size": "M", "shoe_size_mm": "235", "height_cm": "", "notes": ""}
    assert p["delivery"]["region"] == "경북 구미"
    assert p["preferences"]["likes"] == ["여성스러운 핏"] and p["preferences"]["dislikes"] == ["목 파인 옷"]
    assert p["notes"] == ["라인댄스를 함"]
    assert set(p) == STANDARD_KEYS  # 지어낸 키가 없다


def test_saving_again_updates_same_file_without_erasing() -> None:
    server.save_client_profile("kakao", KAKAO_ID, display_name="어머니", dislikes=["목 파인 옷"])
    r = server.save_client_profile("kakao", KAKAO_ID, delivery_region="부산", dislikes=["야구모자"])
    assert r["created"] is False
    p = server.resolve_client("kakao", KAKAO_ID)["profile"]
    assert p["display_name"] == "어머니"                       # 빈 값은 기존 값을 지우지 않는다
    assert p["delivery"]["region"] == "부산"
    assert p["preferences"]["dislikes"] == ["목 파인 옷", "야구모자"]  # 목록은 합친다


def test_save_requires_channel_and_user_id() -> None:
    assert server.save_client_profile("", KAKAO_ID)["saved"] is False
    assert server.save_client_profile("kakao", " ")["saved"] is False
    assert server.save_client_profile("line", KAKAO_ID)["saved"] is False


def test_other_user_and_record_item() -> None:
    cid = server.save_client_profile("kakao", KAKAO_ID, display_name="어머니")["client_id"]
    assert server.resolve_client("kakao", "someone-else")["found"] is False
    assert server.record_item(cid, "found", "쿠션")["recorded"] is True
    assert server.resolve_client("kakao", KAKAO_ID)["profile"]["items"][0]["name"] == "쿠션"
    assert not hasattr(server, "log_recommendation")   # 자유 형식 기록 도구는 없앴다


# ---------- 옛 형식 A, B ----------

def test_legacy_b_resolves_even_before_migration(tmp_clients) -> None:
    _write(tmp_clients, "client-20261112120539.yaml", SAMPLE_B)
    r = server.resolve_client("kakao", "kakaoUserB999")
    assert r["found"] and r["profile"]["display_name"] == "엄마" and r["profile"]["delivery"]["region"] == "전주"


def test_standardize_a_and_b_keeps_every_value() -> None:
    a, a_left = profiles.standardize(SAMPLE_A, "kakaoStemUserId123")
    assert a["display_name"] == "어머니" and a["body"]["top_size"] == "55, M"
    assert a["delivery"]["region"] == "경북 구미" and a["preferences"]["dislikes"] == ["목 파인 옷"]
    assert "preferences.activities: 라인댄스" in a["notes"] and a_left == ["preferences.activities"]
    assert not any(a["channels"].values())                   # A 는 파일 안에 user_id 가 없다

    b, _ = profiles.standardize(SAMPLE_B, "client-20261112120539")
    assert b["channels"]["kakao_user_id"] == "kakaoUserB999"
    assert b["preferences"]["likes"] == ["밝은 색"]
    assert "payment" not in b["shopping_rules"] and b["payment"]["by"] == ""   # 옛 기본값 self 는 옮기지 않는다
    assert {"shipping_address.street: 가짜로 1", "preferences.style: 여성스러운",
            "shopping_rules.budget: 3만원"} <= set(b["notes"])   # 표준 자리가 없는 값은 notes 로
    assert b["history"] == SAMPLE_B["history"]
    assert set(a) == set(b) == set(profiles.empty_profile())


def test_migration_dry_run_changes_nothing(tmp_clients, capsys) -> None:
    _write(tmp_clients, "client-20261112120539.yaml", SAMPLE_B)
    before = (tmp_clients / "client-20261112120539.yaml").read_text(encoding="utf-8")
    migrate_profiles.main(["--dir", str(tmp_clients)])
    assert (tmp_clients / "client-20261112120539.yaml").read_text(encoding="utf-8") == before
    assert not (tmp_clients / "_legacy").exists()
    out = capsys.readouterr().out
    assert "전주" not in out and "엄마" not in out and "kakaoUserB999" not in out  # 값은 출력하지 않는다


def test_migration_apply_moves_a_and_b_to_standard(tmp_clients, capsys) -> None:
    _write(tmp_clients, "kakaoStemUserId123.yaml", SAMPLE_A)
    _write(tmp_clients, "client-20261112120539.yaml", SAMPLE_B)

    migrate_profiles.main(["--dir", str(tmp_clients), "--apply"])
    out = capsys.readouterr().out
    assert "채널ID=없음" in out and "확인 필요" in out              # A 는 추측하지 않는다
    b_file = f"{profiles.client_id_for('kakao', 'kakaoUserB999')}.yaml"
    assert sorted(p.name for p in tmp_clients.glob("*.yaml")) == sorted([b_file, "kakaoStemUserId123.yaml"])
    assert sorted(p.name for p in (tmp_clients / "_legacy").glob("*.yaml")) == \
        ["client-20261112120539.yaml", "kakaoStemUserId123.yaml"]  # 원본 보관

    on_disk = yaml.safe_load((tmp_clients / b_file).read_text(encoding="utf-8"))
    assert set(on_disk) == STANDARD_KEYS
    r = server.resolve_client("kakao", "kakaoUserB999")
    assert r["client_id"] == b_file[:-5] and r["profile"]["delivery"]["region"] == "전주"
    assert server.resolve_client("kakao", "kakaoStemUserId123")["found"] is False

    # 파일 이름이 카카오 user_id 그대로라고 확인한 뒤 다시 돌리면 A 도 찾아진다
    migrate_profiles.main(["--dir", str(tmp_clients), "--apply", "--stem-as-kakao-id"])
    a = server.resolve_client("kakao", "kakaoStemUserId123")
    assert a["found"] and a["profile"]["display_name"] == "어머니"
    assert a["client_id"] == profiles.client_id_for("kakao", "kakaoStemUserId123")
    assert not (tmp_clients / "kakaoStemUserId123.yaml").exists()


def test_standardize_does_not_mutate_input() -> None:
    import copy
    before = copy.deepcopy(SAMPLE_B)
    profiles.standardize(SAMPLE_B, "client-20261112120539")
    assert SAMPLE_B == before


# ---------- 결제 방식(payment) ----------

def test_old_profile_without_new_fields_still_reads(tmp_clients) -> None:
    """VPS 에 이미 있는 프로필(새 칸 없음, 옛 결제 칸 있음)이 그대로 읽히고 resolve 된다."""
    old = {"client_id": profiles.client_id_for("kakao", KAKAO_ID), "display_name": "어머니",
           "channels": {"kakao_user_id": KAKAO_ID},
           "shopping_rules": {"max_candidates": 3, "payment": "self", "payment_delegate_contact": ""},
           "notes": [], "history": [{"at": "2026-11-10", "request": {"item": "쿠션"}}]}
    _write(tmp_clients, f"{old['client_id']}.yaml", old)
    p = server.resolve_client("kakao", KAKAO_ID)["profile"]
    assert p["display_name"] == "어머니"
    assert p["payment"] == {"place": "", "by": "", "asked_on": ""}   # self 는 옮기지 않아 "모름", 물어본 적도 없음
    assert p["items"] == [] and p["notes"] == []            # 빈 연락처는 버린다
    assert p["history"] == old["history"]                   # history 는 읽기만
    assert set(p) == STANDARD_KEYS


def test_old_delegate_moves_to_family_and_contact_is_kept_in_notes() -> None:
    std, _ = profiles.standardize({"shopping_rules": {"payment": "delegate",
                                                      "payment_delegate_contact": "가짜-딸-010-0000-0000"}})
    assert std["payment"]["by"] == "family"
    assert "shopping_rules.payment_delegate_contact: 가짜-딸-010-0000-0000" in std["notes"]  # 지우지 않고 남긴다
    assert "payment" not in std["shopping_rules"] and "payment_delegate_contact" not in std["shopping_rules"]


def test_payment_fields_save_validate_and_do_not_erase() -> None:
    r = server.save_client_profile("kakao", KAKAO_ID, payment_place="쿠팡", payment_by="family")
    assert r["profile"]["payment"] == {"place": "쿠팡", "by": "family", "asked_on": ""}
    server.save_client_profile("kakao", KAKAO_ID, display_name="어머니")          # 빈 값은 지우지 않는다
    assert server.resolve_client("kakao", KAKAO_ID)["profile"]["payment"] == {"place": "쿠팡", "by": "family",
                                                                               "asked_on": ""}
    assert server.save_client_profile("kakao", KAKAO_ID, payment_by="delegate")["saved"] is False
    assert server.save_client_profile("kakao", KAKAO_ID, payment_place="가" * 21)["saved"] is False


# ---------- 찾아 드린 물건(items) ----------

def _cid() -> str:
    return server.save_client_profile("kakao", KAKAO_ID, display_name="어머니")["client_id"]


def test_record_item_schema_is_enforced() -> None:
    cid = _cid()
    ok = server.record_item(cid, "found", "플라워 텀블러", brand="ELO", options={"용량": "240ml", "색": "네이비"},
                            seller="11번가", seen_price_krw=18900)
    assert ok["recorded"] is True
    item = ok["item"]
    assert set(item) == {"date", "status", "brand", "name", "options", "seller", "seen_price_krw"}
    assert len(item["date"]) == 10 and item["seen_price_krw"] == 18900
    bad = [dict(status="bought"), dict(name=" "), dict(seen_price_krw=0), dict(seen_price_krw=-1),
           dict(seen_price_krw="18900"), dict(seen_price_krw=True), dict(options=["240ml"]),
           dict(options={str(i): "x" for i in range(7)}), dict(seller="가" * 81)]
    for kw in bad:
        args = {"status": "found", "name": "텀블러", **kw}
        assert server.record_item(cid, **args)["recorded"] is False, kw
    with pytest.raises(ValueError):                                      # 정해지지 않은 칸
        profiles._clean_item({"date": "2026-11-19", "status": "found", "name": "x", "paid": True})
    assert server.record_item("kakao-none", "found", "x")["recorded"] is False   # 없는 프로필


def test_found_then_chosen_is_one_entry_and_chosen_is_not_downgraded(tmp_clients) -> None:
    cid = _cid()
    opts = {"용량": "240ml", "색": "네이비"}
    profiles.record_item(cid, status="found", name="플라워 텀블러", brand="ELO", options=opts, seller="11번가",
                         seen_price_krw=18900, today="2026-11-19")
    profiles.record_item(cid, status="chosen", name="플라워 텀블러", brand="ELO", options=opts, today="2026-11-19")
    items = profiles.get_profile(cid)["items"]
    assert len(items) == 1 and items[0]["status"] == "chosen"
    assert items[0]["seller"] == "11번가" and items[0]["seen_price_krw"] == 18900   # 빈 값은 옛 값을 지킨다
    profiles.record_item(cid, status="found", name="플라워 텀블러", brand="ELO", options=opts, today="2026-11-19")
    assert [i["status"] for i in profiles.get_profile(cid)["items"]] == ["chosen"]  # 고르신 기록은 그대로
    profiles.record_item(cid, status="found", name="플라워 텀블러", brand="ELO", options=opts, today="2026-11-25")
    assert [(i["date"], i["status"]) for i in profiles.get_profile(cid)["items"]] == \
        [("2026-11-25", "found"), ("2026-11-19", "chosen")]                       # 다른 날은 새 항목, 최신이 앞


def test_items_cap_50_keeps_chosen() -> None:
    cid = _cid()
    for i in range(5):
        profiles.record_item(cid, status="chosen", name=f"고른 물건 {i}", today="2026-10-01")
    for i in range(60):
        profiles.record_item(cid, status="found", name=f"찾은 물건 {i}", today="2026-11-01")
    items = profiles.get_profile(cid)["items"]
    assert len(items) == profiles.MAX_ITEMS == 50
    assert sum(i["status"] == "chosen" for i in items) == 5          # 오래돼도 고르신 기록은 남는다
    assert items[0]["name"] == "찾은 물건 59" and "찾은 물건 14" not in [i["name"] for i in items]


def test_resolve_returns_all_chosen_and_recent_10_found() -> None:
    cid = _cid()
    for i in range(3):
        profiles.record_item(cid, status="chosen", name=f"고른 물건 {i}", today="2026-10-01")
    for i in range(15):
        profiles.record_item(cid, status="found", name=f"찾은 물건 {i}", today="2026-11-01")
    r = server.resolve_client("kakao", KAKAO_ID)
    names = [i["name"] for i in r["profile"]["items"]]
    assert sum(n.startswith("고른") for n in names) == 3
    assert [n for n in names if n.startswith("찾은")] == [f"찾은 물건 {i}" for i in range(14, 4, -1)]
    assert r["items_omitted"] == 5
    assert len(profiles.get_profile(cid)["items"]) == 18                # 파일에는 그대로 있다
    g = server.get_client_profile(cid)
    assert len(g["profile"]["items"]) == 13 and g["items_omitted"] == 5


def test_new_fields_survive_reread_and_migration(tmp_clients) -> None:
    cid = server.save_client_profile("kakao", KAKAO_ID, payment_place="네이버", payment_by="self")["client_id"]
    profiles.record_item(cid, status="chosen", name="보온병", options={"색": "네이비"}, today="2026-11-19")
    before = profiles.get_profile(cid)
    assert before["notes"] == []                                         # 새 칸이 notes 로 새지 않는다
    migrate_profiles.main(["--dir", str(tmp_clients), "--apply"])
    after = profiles.get_profile(cid)
    assert after["payment"] == before["payment"] and after["items"] == before["items"] and after["notes"] == []


# ---------- 결제 질문은 평생 한 번 (payment.asked_on) ----------

def _found(cid, name, day="2026-11-20"):
    return profiles.record_item(cid, status="found", name=name, today=day)


def test_ask_payment_true_once_per_lifetime_even_without_answer() -> None:
    cid = _cid()
    asks = [_found(cid, f"후보 {i}")[1] for i in range(3)]        # 한 답에서 후보 3개 기록
    assert asks == [True, False, False]
    assert profiles.get_profile(cid)["payment"]["asked_on"] == "2026-11-20"
    # 답을 안 하셨고, 30분 뒤 새 대화·다른 날이어도 다시 묻지 않는다
    assert _found(cid, "다른 날 후보", day="2026-12-01")[1] is False
    r = server.record_item(cid, "found", "도구로 기록")
    assert r["recorded"] is True and r["ask_payment"] is False
    assert profiles.get_profile(cid)["payment"]["asked_on"] == "2026-11-20"   # 처음 물은 날 그대로


def test_ask_payment_false_when_payment_known_or_chosen_or_invalid() -> None:
    cid = server.save_client_profile("kakao", KAKAO_ID, payment_by="self")["client_id"]
    assert _found(cid, "후보")[1] is False                             # 이미 알면 묻지 않는다
    assert profiles.get_profile(cid)["payment"]["asked_on"] == ""

    other = server.save_client_profile("kakao", "other-user", display_name="아버지")["client_id"]
    assert profiles.record_item(other, status="chosen", name="고른 물건", today="2026-11-20")[1] is False
    bad = server.record_item(other, "found", " ")                      # 칸이 틀리면 asked_on 을 채우지 않는다
    assert bad["recorded"] is False and bad["ask_payment"] is False
    assert profiles.get_profile(other)["payment"]["asked_on"] == ""
    assert _found(other, "후보")[1] is True                            # 첫 found 에서 묻는다


def test_user_can_tell_payment_later_regardless_of_asked_on() -> None:
    cid = _cid()
    assert _found(cid, "후보")[1] is True                              # 물었지만 답이 없었다
    r = server.save_client_profile("kakao", KAKAO_ID, payment_place="네이버", payment_by="family")
    assert r["saved"] is True
    assert r["profile"]["payment"] == {"place": "네이버", "by": "family", "asked_on": "2026-11-20"}


def test_asked_on_survives_reread_migration_and_bad_value_is_kept_in_notes(tmp_clients) -> None:
    cid = _cid()
    _found(cid, "후보")
    migrate_profiles.main(["--dir", str(tmp_clients), "--apply"])
    assert profiles.get_profile(cid)["payment"]["asked_on"] == "2026-11-20"
    std, _ = profiles.standardize({"payment": {"by": "", "asked_on": "어제"}})
    assert std["payment"]["asked_on"] == "" and "payment.asked_on: 어제" in std["notes"]


def test_record_item_tells_where_to_put_the_payment_question() -> None:
    """11/20 실사용: 후보를 기록하고도 답에 결제 질문만 썼다. ask_payment=true 응답에 자리를 알려 준다."""
    cid = _cid()
    first = server.record_item(cid, "found", "ELO 플라워 텀블러", options={"용량": "240ml", "색": "네이비"})
    assert first["ask_payment"] is True
    assert first["ask_payment_note"] == server.ASK_PAYMENT_NOTE
    assert "맨 끝" in first["ask_payment_note"] and "결제 질문만 있는 답은 쓰지 않는다" in first["ask_payment_note"]
    second = server.record_item(cid, "found", "ELO 플라워 텀블러", options={"용량": "350ml", "색": "네이비"})
    assert second["ask_payment"] is False and "ask_payment_note" not in second
