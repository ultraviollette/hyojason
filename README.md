# 효자손 (Hyojason) — 부모님 대신 쇼핑 후보를 골라 주는 에이전트

> AX School Season 01 "Put AI to Work" 챌린지 · 실제 클라이언트: 어머니

## 누구를 위한 에이전트이고, 어떤 업무를 대신하나

**클라이언트**는 지방 중소도시에 사시는 어머니다. 온라인 구매를 혼자 해 본 적이 없고, 캡처도 링크 공유도 못 하신다. 홈쇼핑이나 마트에서 마음에 드는 걸 보면 흐릿한 사진을 찍어 딸(나)에게 보내거나, 쿠팡 화면을 보며 전화를 하신다. 품절 상품을 고르시고, 급한 물건인데 해외 직구(2주)를 고르신다. 그 구조를 모르시기 때문이다.

**대신하는 업무**는 "딸에게 물어봐서 사기"다. 지금까지 딸이 해 오던 퍼스널 쇼퍼 역할을 그대로 떼어낸다.

1. 어머니가 카톡으로 말이나 사진을 보낸다. ("라인댄스 옷 안에 입을 살색 쫄쫄이 내의, 이번 주 안에")
2. 효자손이 빠진 조건(사이즈·기한)을 한 번에 되묻는다. 프로필에 있는 건 묻지 않는다.
3. 네이버 쇼핑에서 후보를 찾고, 후보마다 **재고 · 기한 내 도착 · 국내/해외 발송 · 프로필 금지 조건**을 yes/no로 거른다 (Jev 게이트).
4. 통과한 1~3개를 "왜 골랐는지" 한 줄과 함께 보낸다. 탈락한 것도 이유를 남긴다. ("보셨던 건 품절이에요. 외국 발송이라 2주 걸려요.")
5. **결제는 어머니가 직접 한다.** 효자손은 절대 결제하지 않는다.

딸과 다른 점: 눈치를 안 봐도 되고, 잔소리가 없고, 딸이 해외에 있어도 된다.

## 구조

```
어머니 ──카카오톡──▶ 카카오 i 오픈빌더 ──스킬 웹훅──▶ kakao-bridge (Cloudflare Worker)
                                                        │  즉시 useCallback 응답, 백그라운드로 ▼
                                                   Hermes Agent (Mac, hermes gateway, /v1/responses)
                                                        │  SOUL.md 페르소나 + hyojason-shopping 스킬
                                                        ▼
                                              MCP: shopping_tools (Python)
                                              ├─ search_products   네이버 쇼핑 API (키 없으면 mock, 이때 웹 검색으로 대체)
                                              ├─ check_product     페이지 읽기 + Jev yes/no 게이트
                                              └─ get/save_client_profile  클라이언트별 프로필 (git 제외)
                                                        │
                                           callbackUrl ◀─┘  최종 답을 카톡으로
```

AX School 강의 적용: **Hermes Agent**(본체·메모리·스킬·API 서버), **Jev**(후보 검증 yes/no 게이트), **AI Gateway**(예정: Hermes 모델 호출을 Cloudflare AI Gateway 경유), **Claude Code**(구현).

실제 사용 모델: `claude-sonnet-4-5` (Anthropic API 직접 호출).

### 사용자별 맞춤이 되는 이유
에이전트 본체(SOUL.md, 스킬, 도구)와 **클라이언트 프로필**(`clients/<id>.yaml`: 치수, 선호, 금지 조건, 쇼핑 규칙, 이력)을 분리했다. 카카오 사용자 ID → client_id 매핑으로 같은 에이전트가 어머니·아버지·다른 집 부모님을 각각 다르게 응대한다. 프로필은 git에 올리지 않는다(`clients/_example.yaml`만 양식으로 공개).

### 검색 전략
1. 쿠팡 파트너스 API: 승인 후 1순위로 쓴다.
2. 웹 검색: 현재 쓰는 방식이다. 일반 웹 검색으로는 개별 상품 URL을 안정적으로 얻지 못하는 한계가 있다.

네이버 검색 API는 2026년 개인 발급 중단으로 포기했다.

## 진행 상황

### 지금 되는 것
- [x] 효자손 페르소나(`hermes/SOUL.md`)와 쇼핑 워크플로 스킬(`hermes/skills/hyojason-shopping/SKILL.md`)
- [x] MCP 서버 `shopping_tools`: 상품 검증(재고/발송경로/기한/금지조건 → passed), 클라이언트 프로필 CRUD, 추천 이력. 네이버 쇼핑 검색은 코드만 있고 키가 없어 mock 으로 동작
- [x] 기한 표현 파서 ("이번 주 안", "수요일까지", "3일 안", "2026-11-12")
- [x] 카카오 브릿지 Worker: 스킬 요청 수신 → 즉시 콜백 응답 → Hermes 호출 → callbackUrl 로 최종 답 (`npm run typecheck` 통과)
- [x] 클라이언트 프로필 양식과 git 분리
- [x] Hermes 터미널 end-to-end: 프로필 인식 → 되묻기 → 검색 → Jev 검증 → 후보
- [x] Jev 실제 호출 확인 (`gate_engine=jev`)
- [x] 웹 검색 폴백 호출
- [x] 프로필별 도구 제한

### 아직 안 되는 것
- [ ] 실제 상품 데이터
  - 네이버 검색 API: 2026년 개인 발급 중단으로 포기
  - 쿠팡 파트너스: 가입 완료, 최종 승인 대기 (누적 판매 15만 원)
  - 웹 검색 폴백: 호출까지 확인됐으나 일반 웹 검색으로는 개별 상품 URL을 안정적으로 얻지 못함. 쿠팡 API 승인 후 해결 예정
- [ ] 카카오 챗봇 생성·배포, 사진 메시지가 스킬 서버에 어떤 필드로 오는지 실측 (멤버 제보: secureURL)
- [ ] Mac 의 Hermes gateway 를 외부(카카오)에서 닿게 하는 터널 (cloudflared)
- [ ] 쿠팡 상품 페이지의 재고·도착일 정확 판독 (지금은 제목·판매처·페이지 텍스트 키워드 기반)
- [ ] AI Gateway 경유 설정 적용
- [ ] 어머니 첫 실사용

## 실행 (개발자용)

```bash
# 1) MCP 서버 (Python 3.14+, uv)
cd mcp && uv sync && cp ../.env.example ../.env   # 키 채우기
uv run python -m shopping_tools.server              # stdio MCP

# 2) Hermes 프로필
mkdir -p ~/.hermes/profiles/hyojason/skills
cp hermes/SOUL.md ~/.hermes/profiles/hyojason/
cp -r hermes/skills/hyojason-shopping ~/.hermes/profiles/hyojason/skills/
cp hermes/config.example.yaml ~/.hermes/profiles/hyojason/config.yaml   # <REPO>, <ACCOUNT_ID> 치환
hermes --profile hyojason                            # 터미널에서 먼저 테스트
hermes --profile hyojason gateway                    # API 서버 + 카카오 브릿지 연결 시

# 3) 카카오 브릿지
cd kakao-bridge && npm i && wrangler secret put HERMES_API_URL && wrangler secret put HERMES_API_KEY
npm run deploy    # 배포된 URL 을 오픈빌더 스킬 URL 로 등록, 블록에서 Callback API 켜기
```

## 범위 밖 (이번 챌린지)
자동 결제, 장바구니 자동 담기, 반품·교환, 지인 대리 구매, 음성 입력.

## 라이선스
MIT
