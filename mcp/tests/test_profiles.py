"""프로필 왕복: 저장 → (새 대화, 카카오 user_id 만 아는 상태) → 다시 찾기.

실제 사례: 카카오 사용자가 호칭·지역을 알려 주고 "프로필 저장했습니다"까지 받았는데, 다음 날 새 대화에서
"처음 뵙네요"가 됐다. 저장은 client-<시각>.yaml 로 되는데, 프롬프트는 get_client_profile(<카카오 user_id>) 로
찾고 있었다(resolve_client 를 부르지 않음).
"""
import json

import pytest

from shopping_tools import profiles, server

KAKAO_ID = "a1b2c3d4e5"


@pytest.fixture(autouse=True)
def tmp_clients(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "CLIENTS_DIR", tmp_path)
    return tmp_path


def _save(profile: dict, **kw) -> dict:
    return server.save_client_profile(json.dumps(profile, ensure_ascii=False), **kw)


def test_save_then_resolve_in_a_new_conversation(tmp_clients) -> None:
    saved = _save({"display_name": "어머니", "delivery": {"region": "지방 중소도시"}},
                  channel="kakao", channel_user_id=KAKAO_ID)
    assert saved["profile"]["client_id"].startswith("client-")

    # 새 대화: 메시지 앞의 [channel=kakao user_id=...] 만 안다
    r = server.resolve_client("kakao", KAKAO_ID)
    assert r["found"] is True and r["client_id"] == saved["profile"]["client_id"]
    assert r["profile"]["display_name"] == "어머니"
    assert r["profile"]["delivery"]["region"] == "지방 중소도시"


def test_old_instruction_get_profile_with_kakao_id_still_works() -> None:
    _save({"display_name": "어머니"}, channel="kakao", channel_user_id=KAKAO_ID)
    r = server.get_client_profile(KAKAO_ID)  # 예전 SKILL 0단계처럼 카카오 user_id 를 client_id 로
    assert r["exists"] is True and r["profile"]["display_name"] == "어머니"


@pytest.mark.parametrize("shape", [
    {"channel": "kakao", "channel_user_id": KAKAO_ID},     # 실제 저장본에서 본 모양 (최상위)
    {"kakao_user_id": KAKAO_ID},
    {"channels": {"kakao_user_id": KAKAO_ID}},
])
def test_channel_id_written_in_any_shape_is_resolvable(shape) -> None:
    _save({"display_name": "어머니", **shape})  # 도구 인자 없이 JSON 안에만 넣은 경우
    r = server.resolve_client("kakao", KAKAO_ID)
    assert r["found"] is True
    assert r["profile"]["channels"]["kakao_user_id"] == KAKAO_ID
    assert "channel_user_id" not in r["profile"] and "kakao_user_id" not in r["profile"]


def test_saving_again_merges_instead_of_duplicating(tmp_clients) -> None:
    first = _save({"display_name": "어머니"}, channel="kakao", channel_user_id=KAKAO_ID)
    second = _save({"delivery": {"region": "부산"}}, channel="kakao", channel_user_id=KAKAO_ID)
    assert second["merged_into_existing"] is True
    assert second["profile"]["client_id"] == first["profile"]["client_id"]
    assert len(list(tmp_clients.glob("*.yaml"))) == 1
    r = server.resolve_client("kakao", KAKAO_ID)["profile"]
    assert r["display_name"] == "어머니" and r["delivery"]["region"] == "부산"


def test_unknown_user_is_not_found() -> None:
    assert server.resolve_client("kakao", "nobody")["found"] is False
    assert server.resolve_client("kakao", "")["found"] is False
