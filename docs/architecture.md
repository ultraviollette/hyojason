# 효자손 아키텍처 메모

## 왜 이 구조인가
- **Hermes Agent를 본체로** 쓴다. 페르소나(SOUL.md), 스킬(SKILL.md), 메모리, 크론, MCP, 그리고 OpenAI 호환 HTTP API 서버(`API_SERVER_ENABLED=true`, 기본 `127.0.0.1:8642`)가 이미 있다. 우리가 만들 것은 "도구"와 "창구"뿐이다.
- Hermes에 **카카오톡 어댑터는 없다**(Telegram, LINE, Discord 등은 있음). 그래서 카카오 ↔ Hermes 사이에 작은 브릿지를 둔다.
- 카카오 스킬 서버는 **5초** 안에 응답해야 한다. 쇼핑 검색은 그보다 오래 걸리므로 **Callback API**(`useCallback: true` → `userRequest.callbackUrl`로 나중에 POST, 5분 유효·1회)를 쓴다.

## 구성 요소
| 구성 | 위치 | 역할 |
|---|---|---|
| SOUL.md | `hermes/SOUL.md` | 효자손 정체성, 절대 규칙(결제 금지 등), 말투, 결과 형식 |
| 스킬 | `hermes/skills/hyojason-shopping/SKILL.md` | 요청 해석 → 되묻기 → 검색 → 검증 → 선택 → 답장 → 기록 |
| MCP 도구 | `mcp/shopping_tools/` | `search_products`(네이버), `check_product`(Jev 게이트), 프로필 CRUD, 이력 |
| 클라이언트 프로필 | `clients/<id>.yaml` (git 제외) | 치수, 선호/금지, 쇼핑 규칙(결제 주체 등), 채널 ID 매핑, 이력 |
| 카카오 브릿지 | `kakao-bridge/` (Cloudflare Worker) | 스킬 웹훅 수신, 즉시 콜백 응답, Hermes 호출, callbackUrl 전송 |
| 터널 | `cloudflared tunnel --url http://127.0.0.1:8642` | Mac 의 Hermes API 를 Worker 가 닿게 함 (MVP) |
| AI Gateway | Hermes `model.base_url` | 모델 호출 로그·캐시·비용 관측 |

## 사용자별 맞춤 설계
1. 브릿지가 카카오 `userRequest.user.id`를 메시지 앞에 `[channel=kakao user_id=…]`로 붙여 Hermes에 넘긴다 (그리고 `conversation: kakao-<id>`로 대화 맥락 분리).
2. 스킬 0단계에서 `resolve_client(channel, channel_user_id)` → 없으면 짧은 온보딩(이름·사이즈·싫어하는 것·배송 지역) 후 `save_client_profile`.
3. 이후 모든 판단(되묻기 생략, `violates_profile` 금지어, 기본 기한, 결제 링크 수신자)은 프로필을 읽어서 한다.
4. 아버지는 같은 봇에 다른 카카오 ID로 들어오면 자동으로 다른 프로필. 다른 집 부모님도 동일.

재판매 관점: 공개 레포 = 에이전트 본체. 고객사별로 `clients/`만 다르다. 프로필 스키마(`clients/_example.yaml`)가 곧 온보딩 설문지다.

## Jev 게이트가 하는 일
후보 N개 × 질문 4개 = 4N번의 yes/no. LLM으로 하면 느리고 비싸다. Jev로 거른 뒤 통과한 1~3개만 LLM이 근거 문장을 쓴다.

| 게이트 | 질문 | 탈락 조건 |
|---|---|---|
| in_stock | 지금 주문 가능한가 | no |
| arrives_by_deadline | 기한 안에 도착하나 | 기한 있음 && no |
| domestic_shipping | 국내 발송인가 | 탈락 아님. 기한 없으면 "외국 발송, 2주, 대신 쌈" 표시 |
| violates_profile | 프로필 금지 조건 해당 | yes |

`JEV_API_KEY` 없으면 규칙 기반 폴백(키워드 정규식). Jev SDK 연결은 `gates._jev_decide` 한 함수만 채우면 된다.

## 검증(과제 섹션 5)과의 연결
- 후보마다 `note`(근거 한 줄)와 `buy_url`을 돌려주므로 "왜 골랐는지"가 답장에 그대로 들어간다.
- 초반 10건은 딸이 같은 요청을 받아 직접 골라 본 것과 비교(사람 QA). `log_recommendation` 이력이 비교 자료.
- 성공 기준: 4주간 어머니가 딸에게 묻지 않고 끝낸 구매 5건 이상, 반품 0건.

## 카카오 세팅 순서 (미묘)
1. 카카오비즈니스 가입 → 카카오톡 채널 개설 → 챗봇 관리자센터 가입 → 봇 생성 (문서상 별도 심사 없음)
2. 스킬 등록: URL = Worker 주소 `/kakao/skill?secret=…`
3. 폴백 블록에 스킬 연결, 블록 상세 > 더보기 > **Callback API 설정 ON**
4. 배포 후 실제 카톡에서 테스트 (봇테스트는 콜백을 완전히 지원하지 않음)
5. 사진 1장 보내서 Worker 로그로 페이로드 필드 확인 → `extractImageUrls` 를 정확한 키로 좁히기

## 알려진 불확실성
- Hermes `config.yaml`의 `mcp_servers` / `model` 블록 정확한 키 이름 (설치본 문서로 확인)
- Hermes `/v1/responses` 의 `input_image` 처리 방식
- 카카오 이미지 페이로드 필드명 (문서 미기재)
- callbackUrl 유효 시간: 가이드 본문 5분 vs 오류표 1분 — 60초 안에 답하는 것을 목표로
