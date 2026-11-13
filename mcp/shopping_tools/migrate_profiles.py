"""일회성 변환: 에이전트가 키를 지어내 저장한 옛 프로필을 표준 형식·표준 파일 이름으로 옮긴다.

    uv run --project mcp python -m shopping_tools.migrate_profiles --dir /path/to/clients            # 계획만 보기
    uv run --project mcp python -m shopping_tools.migrate_profiles --dir /path/to/clients --apply    # 실제로 옮기기

- 기본은 dry-run(아무것도 바꾸지 않음). --apply 를 붙여야 쓴다.
- 원본은 지우지 않고 <dir>/_legacy/ 로 옮긴다. 표준 자리에 못 넣은 값은 버리지 않고 notes 로 옮긴다.
- 파일 안에 채널 사용자 ID 가 없으면 추측하지 않는다. 표준 형식으로만 고치고 "확인 필요"로 보고한다.
  파일 이름이 카카오 user_id 그대로라고 확인되면 --stem-as-kakao-id 로 다시 돌린다.
- 출력에는 파일 이름과 키 이름만 나온다. 프로필 값(호칭·지역·사이즈)은 출력하지 않는다.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import profiles


@dataclass
class Plan:
    source: Path
    target: Path
    channel_found: bool
    moved_to_notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def plan_file(path: Path, *, stem_as_kakao_id: bool = False) -> tuple[Plan, dict]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    std, leftovers = profiles.standardize(raw, path.stem)
    if stem_as_kakao_id and not any(std["channels"].values()) and not path.stem.startswith("client-"):
        std["channels"]["kakao_user_id"] = path.stem
    channel, uid = next(((c, std["channels"].get(profiles.channel_key(c))) for c in profiles.CHANNELS
                         if std["channels"].get(profiles.channel_key(c))), (None, None))
    warnings = []
    if channel:
        std["client_id"] = profiles.client_id_for(channel, uid)
    else:
        std["client_id"] = path.stem
        warnings.append("채널 사용자 ID 없음 → resolve 불가. 확인 필요 "
                        "(파일 이름이 카카오 user_id 그대로면 --stem-as-kakao-id)")
    target = path.with_name(f"{std['client_id']}.yaml")
    return Plan(path, target, channel is not None, leftovers, warnings), std


def migrate(clients_dir: Path, *, apply: bool, stem_as_kakao_id: bool = False) -> list[Plan]:
    plans = []
    targets: dict[Path, Path] = {}
    for path in sorted(clients_dir.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        plan, std = plan_file(path, stem_as_kakao_id=stem_as_kakao_id)
        if plan.target in targets and targets[plan.target] != path:
            plan.warnings.append(f"같은 사용자 파일이 여럿: {targets[plan.target].name} 와 합치지 않고 건너뜀. 확인 필요")
            plans.append(plan)
            continue
        targets[plan.target] = path
        plans.append(plan)
        if apply:
            legacy = clients_dir / "_legacy"
            legacy.mkdir(exist_ok=True)
            shutil.copy2(path, legacy / path.name)          # 원본 보관
            std["updated_at"] = datetime.now(timezone.utc).isoformat()
            with plan.target.open("w", encoding="utf-8") as f:
                yaml.safe_dump(std, f, allow_unicode=True, sort_keys=False)
            if plan.target != path:
                path.unlink()                               # 원본은 _legacy/ 에 있다
    return plans


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, default=profiles.CLIENTS_DIR)
    ap.add_argument("--apply", action="store_true", help="실제로 쓴다 (기본은 계획만)")
    ap.add_argument("--stem-as-kakao-id", action="store_true",
                    help="채널 ID 가 없는 파일은 파일 이름을 카카오 user_id 로 본다 (확인된 경우에만)")
    args = ap.parse_args(argv)
    plans = migrate(args.dir, apply=args.apply, stem_as_kakao_id=args.stem_as_kakao_id)
    print(f"{'적용' if args.apply else '계획(dry-run)'}: {args.dir}  파일 {len(plans)}개")
    for p in plans:
        print(f"- {p.source.name} → {p.target.name}  채널ID={'있음' if p.channel_found else '없음'}")
        if p.moved_to_notes:
            print(f"    notes 로 옮긴 키: {', '.join(p.moved_to_notes)}")
        for w in p.warnings:
            print(f"    ⚠ {w}")
    if not args.apply:
        print("아무것도 바꾸지 않았어요. 실제로 옮기려면 --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
