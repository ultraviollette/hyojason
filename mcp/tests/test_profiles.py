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


def test_other_user_and_log_recommendation() -> None:
    cid = server.save_client_profile("kakao", KAKAO_ID, display_name="어머니")["client_id"]
    assert server.resolve_client("kakao", "someone-else")["found"] is False
    assert server.log_recommendation(cid, '{"item": "쿠션"}')["logged"] is True
    assert server.resolve_client("kakao", KAKAO_ID)["profile"]["history"][0]["request"] == {"item": "쿠션"}


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
    assert b["preferences"]["likes"] == ["밝은 색"] and b["shopping_rules"]["payment"] == "self"
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
