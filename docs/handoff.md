# 효자손 인수인계 (2026-11-25 기준, 쿠팡 파트너스 검색 반영)

이어서 작업할 사람(또는 에이전트)을 위한 메모다. 구조 설명은 [architecture.md](architecture.md)에 있고, 여기에는 11/19~20 실사용 시험을 반영하며 바뀐 것만 적는다.
실제 프로필 내용·시크릿·사용자 ID 는 이 문서에 적지 않는다.

## 1. 커밋 목록 (11/19~20)

| 커밋 | 한 일 |
|---|---|
| 889f97e | 11/19 시험 반영: 브랜드를 모르는 사진은 "닮은 제품"이라 말하기, 용량은 위아래 30%까지 넓혀 찾기, 사진에 맞는 숫자 예시, 이미 나온 조건은 다시 묻지 않기 |
| 0e7e153 | 프로필에 items(찾아 드림/고르심 기록)와 payment(주로 사는 곳·결제하는 사람) 추가, `record_item` 도구 추가(`log_recommendation` 제거) |
| 7be3469 | 결제 방식 질문을 평생 한 번만: `payment.asked_on` 을 `record_item(found)` 이 정한다 |
| 5f4fc69 | 못 찾았을 때 검색어 바꿔 최대 3번 다시 찾기(SKILL 3-c), 확인 못 한 사실(국적·국내 판매·단종) 말하지 않기 |
| 3c6e349 | 카카오 브릿지에서 이어서 온 메시지 합치기(대기 후 한 턴으로, 처리 중 온 것은 다음 턴으로, 질문형 답 보내지 않기) |
| 4a8d43d | 결제 질문은 후보 답 맨 끝에만(`ask_payment_note`), 질문 묶음은 한 번만, 브랜드가 있으면 "브랜드 + 종류"로 넓게 검색, 대기 말 금지 |
| 23e409b | 기한은 사용자가 꺼냈을 때만, 크기는 누구나 아는 물건 둘 중에 고르게 묻기, 사진 utterance 형태 로그 스위치 |
| ab1bee4 | 사진 utterance 의 주소 글자 빼기, 사진만 온 경우 대기 12초/15초, items.url, 링크만 있는 답 금지, `confirm_phrase`, `payment.places` + 쿠팡 `place_rules`, 정가 대신 판매가 |
| 9b34eb9 | 주로 사는 곳에 네이버가 있으면 스마트스토어·브랜드스토어 상품 페이지를 앞에(`place_rules`), 네이버 쇼핑 검색 API 경로에 "2026-07-31 종료" 주석 |
| f807d13 | (site) 소개 페이지 쿠팡 파트너스 링크 교체(엠보엘로우 텀블러) |
| e9a41eb | (site) 같은 링크의 상품 표시 이름 변경(엠보엘로우 → 가든화이트) |
| f3f5b0c | 이 인수인계 문서 |
| 8316893 | 확인 2번 상한(`attempt`), `drop_reason`, 개수 로그 |
| 7b50a5e | 작업 규칙에 push |
| (이번 커밋) | 페이지 읽기 상한 15 → 8초, `say` 를 한 줄로(읽은 것·못 읽은 것 따로), 검색 결과 배송비를 `candidates[].shipping_fee_krw` 로 받기 |
| dd285c8 | 쿠팡 파트너스 응답 모양 확인용 시험 스크립트(`scripts/coupang_search_probe.py`, 효자손 코드와 연결 안 함) |
| 10ecd19 | 쿠팡 파트너스 검색 `coupang_search`(아래 2-5), record_item 의 쿠팡 주소 거절, 브릿지 대가성 문구 보장 |
| (이번 커밋) | 11/25 실사용 반영: 채움 결과(AFFGOLDBOX)를 empty 로, 검색어 바꿔 최대 3번, 브랜드 다른 표기, 옵션 나열 상품, httpx 요청 로그 끄기 |
| c79139a | f3f5b0c 시험 반영: 후보 페이지 확인(`check_candidates`), 기록의 `match`(같은 물건/비슷한 것), 해요체·내부 용어·군말 금지(`confirm_note`), SKILL description |

## 2. 새로 생긴 구조

### 2-1. 카카오 브릿지의 메시지 합치기 (`kakao-bridge-vps/server.py`)

**대기 시간** (`Settings`, 초)

| 칸 | 값 | 뜻 |
|---|---|---|
| `wait_first_text_s` | 4 | 글로 시작한 첫 메시지 뒤 대기 |
| `wait_first_photo_s` | 12 | 아직 사진만 와 있을 때 마지막 사진 뒤 대기(여러 장이어도 같음) |
| `wait_more_s` | 4 | 글이 섞인 뒤 새 메시지가 오면 마지막 메시지부터 다시 대기 |
| `wait_max_text_s` | 10 | 글로 시작했을 때 첫 메시지부터 최대 대기 |
| `wait_max_photo_s` | 15 | 사진으로 시작했을 때 첫 메시지부터 최대 대기 |

보내는 시각 = min(첫 도착 + 최대 대기, 마지막 도착 + 대기). 처리 중에 온 메시지는 끊지 않고 모아서 다음 한 턴으로 보낸다.

**질문형 답 보내지 않기** (`is_question_reply`)
- 다음이 모두 맞으면 Hermes 답을 보내지 않는다.
  - 처리 중에 새 메시지가 와 있다.
  - 답 원문에 `http(s)://` 가 없다.
  - 물음표(`?` 또는 `？`)가 있다.
  - 결제 질문("주로 어디서 사세요")이 아니다.
- 보내지 않은 턴의 콜백은 닫기 문구로 닫는다. 다음 턴에는 `BRIDGE_NOTE`("[브릿지 안내: 직전 당신의 답은 사용자에게 전달되지 않았다…]")를 붙이고, 그 턴의 메시지(사진 포함)를 다시 넣는다.
- 사용자 글 속 "[브릿지 안내" 표식은 대괄호를 없애 진짜 안내와 구분한다(`neutralize_user_text`).

**문구**

| 상수 | 문구 | 언제 |
|---|---|---|
| `QUICK_ACK` | "가게 몇 군데 돌아보고 올게요. 길면 2~3분 걸려요.…" | 즉시 응답(관리자센터의 응답대기 메시지와 같게 맞춤) |
| `CLOSE_MSG` | "이어서 보내 주신 것까지 같이 볼게요." | 합쳐진 앞 메시지의 콜백, 보내지 않은 질문형 답의 콜백 |
| `RESET_DROP_MSG` | "앞에 보내 주신 건 접어 두고 처음부터 할게요." | "처음부터"·"새로 시작"이 왔을 때 대기·처리 중이던 메시지 |
| `LINKS_ONLY_BODY` | "아래 버튼을 눌러 열어 보세요." | 답에 링크만 있고 본문이 비었을 때 카드 설명 칸 |
| `FAIL_MSG` | "죄송해요, 지금은 찾기가 어려워요.…" | Hermes 실패·타임아웃, 안전망 |

**사진 utterance 처리** (`strip_image_utterance`)
- 카카오 사진 메시지의 utterance 는 사진 주소 그 자체다(형태 로그로 확인: `starts_url=1 has_param_image=1`).
- utterance 가 params 에서 찾은 이미지 주소와 같거나 그 주소를 담고 있을 때만 그 글자를 뺀다. 다른 주소는 건드리지 않는다.
- 사진만 온 메시지는 Hermes 에 "(사진만 보냄)"으로, 합쳐진 메시지에서는 "(사진)"으로 간다. 사진은 같은 턴의 `input_image` 로 함께 보낸다.

**로그 줄 형식** (사용자 글·사용자 ID·callbackUrl·시크릿은 남기지 않는다)
```
path=arrive seq=<n> image=<0|1> text_len=<n> merged=<0|1>[,new_conversation]
path=batch seq=<a>-<b> size=<n>
path=callback seq=<n> outcome=<값> duration_ms=<도착부터 ms>
path=skill outcome=<forbidden|bad_json|reset> duration_ms=<n>
path=sync outcome=<sync_ok|sync_fail:<예외 이름>> duration_ms=<n>
path=utterance_shape seq=<n> len=<n> starts_url=<0|1> has_param_image=<0|1> json=<0|1> shape=<모양>
```

callback `outcome` 값:
- 기본값
  - `ok`: 답을 보냄
  - `closed_merged`: 합쳐진 앞 메시지를 닫음
  - `suppressed_question`: 질문형 답을 보내지 않음
  - `dropped_by_reset`: 처음부터로 버림
  - `hermes_timeout`, `hermes_error:<예외 이름>`: Hermes 실패
  - `fail_safe`: 예외로 빠진 경우의 안전망
- 뒤에 붙는 표시
  - `,payment_q_no_links`: 결제 질문만 있고 주소가 없는 답
  - `,links_only`: 본문 없이 링크만 온 답
  - `,callback_http_<code>`, `,callback_error:<예외 이름>`: 콜백 전송 실패

**형태 로그 스위치**
- 표시 파일 `~/.config/hyojason/debug-utterance-shape` 가 있을 때만, 사진이 있는 메시지마다 한 줄을 남긴다. 기본은 꺼짐이다.
- 서비스가 `hermes` 사용자로 돌아서 실제 위치는 `/home/hermes/.config/hyojason/debug-utterance-shape` 이다.
- 켜기: `mkdir -p ~/.config/hyojason && touch ~/.config/hyojason/debug-utterance-shape`
- 끄기: `rm ~/.config/hyojason/debug-utterance-shape`
- 켜고 끌 때 재시작은 필요 없다.
- shape 는 글자를 지운 모양만 남긴다: 한글→가, 영문→a, 숫자→9, 공백→_, 다른 글자→x, ASCII 기호는 그대로, 200자까지.

### 2-2. 프로필 (`mcp/shopping_tools/profiles.py`, 양식 `clients/_example.yaml`)
- 스키마는 코드가 강제한다. 옛 형식 파일도 `standardize()` 로 읽는다.
- 실제 파일은 `HYOJASON_CLIENTS_DIR` 아래에 있고 git 에 올리지 않는다.

**items** (`record_item` 으로만 쓴다)

| 칸 | 뜻 |
|---|---|
| `date` | 한국 날짜, 서버가 채움 |
| `status` | `found`(찾아 드림) \| `chosen`(고르심). 결제 여부는 기록하지 않음 |
| `brand`, `name`, `options` | 옵션은 `{이름: 값}`, 6개까지 |
| `seller` | 판매처 이름 |
| `seen_price_krw` | 그때 본 실제 판매가(정가 아님). 모르면 null |
| `url` | 선택. 답에 드린 상품 페이지 주소. 옛 기록은 `""` 로 읽힘. 크라우드펀딩(wadiz.kr·tumblbug.com) 주소면 기록 자체를 거절 |
| `match` | `same`(찾던 그 물건) \| `similar`(비슷한 것·닮은 제품으로 드림) \| `""`(모름, 옛 기록). found 를 같은 물건으로 합칠 때 빈 값은 옛 값을 지킴 |

- 최근 50건까지 보관한다. 넘으면 오래된 found 부터 지우고, chosen 은 끝까지 남긴다.
- 같은 날·같은 브랜드·이름·옵션이면 한 줄로 합친다. found 를 chosen 으로 남기면 그 줄이 chosen 으로 바뀐다. 빈 값은 옛 값을 지킨다.
- 도구가 돌려주는 items 는 chosen 전부와 최근 found 10건이다(`items_omitted` 로 뺀 건수를 알림).
- 쿠팡 파트너스 API 데이터는 약관상 저장·복제 금지다. `record_item` 은 호스트가 `link.coupang.com` 인 주소(/re/·/a/ 모두)를 받으면 저장하지 않고 거절한다(`COUPANG_RECORD_ERROR`: "name 에는 사용자가 말한 종류를, seller 에는 쿠팡을 넣어 다시 기록"). 거절은 칸 검사에서 나므로 `asked_on` 이 채워지지 않고, 결제 질문은 재호출 때 한 번 나간다. 쿠팡 기록에는 url 이 없어 1-d(재검색 없이 돌려주기) 대상이 아니다.

**payment**

| 칸 | 뜻 |
|---|---|
| `places` | 주로 사는 곳 목록(가게마다 20자, 5곳까지). 새로 말하면 바꿈. 옛 `place: "쿠팡, 네이버"` 한 줄은 목록으로 읽음 |
| `by` | `""`(모름) \| `self` \| `family` |
| `asked_on` | 결제 방식을 물어본 날. `record_item(found)` 이 채움. 채워지면 다시 묻지 않음 |

### 2-3. 도구 응답에 실리는 규칙 (`mcp/shopping_tools/server.py`)
글 규칙만으로는 실사용에서 잘 안 지켜졌다. 반면 도구 응답에 실은 규칙은 지켜졌다. 그래서 중요한 규칙은 응답에 싣는다.

| 이름 | 어디에 | 언제 |
|---|---|---|
| `ask_payment` / `ask_payment_note` | `record_item` | 결제 방식을 모르고 물은 적도 없을 때 found 첫 기록에서 한 번만. 노트는 "후보와 링크를 다 쓰고 맨 끝에 결제 질문 한 줄" |
| `place_rules` | `resolve_client`, `get_client_profile`, `save_client_profile`, `record_item(found)` | `places` 에 쿠팡이 있으면: 쿠팡 검색이 켜져 있으면(키 둘 + `COUPANG_PARTNERS_VERIFIED=1`) `COUPANG_SEARCH_FIRST_NOTE`(coupang_search 먼저), 아니면 옛 `COUPANG_FIRST_NOTE`(쿠팡 검색어 줄을 답 맨 앞에). 네이버가 있으면 `NAVER_FIRST_NOTE`(웹 검색 후보 중 smartstore.naver.com·brand.naver.com 상품 페이지를 앞에). 둘 다면 쿠팡이 먼저. 해당 없으면 칸 자체가 없음 |
| `items[].confirm_phrase` | 프로필 보기(파일에는 저장 안 함) | chosen → "지난번 ○○ 맞으세요?" / found+same → "지난번에 찾아 드렸던 ○○ 말씀이세요?" / found+similar → "지난번에 비슷한 걸로 ○○ 등 N개 찾아 드렸어요. 그중에서 고르실래요, 다시 찾아볼까요?" / found+""(옛 기록) → 같은 문장에서 "비슷한 걸로"만 뺌. N 은 같은 날·같은 match 의 found 묶음, ○○ 는 묶음에서 먼저 기록된 것. 1개면 "그걸로 보실래요, 다시 찾아볼까요?" |
| `confirm_note` | 프로필에 items 가 있을 때 `resolve_client` 등 `place_rules` 와 같은 응답 | 확인 질문은 confirm_phrase 로 바로 시작, 사진 설명·기록 이야기 금지, 내부 용어 금지, 해요체만 |
| `check_candidates` 의 `results[].show`·`say`, `note` | `check_candidates` | 아래 2-4 |
| `disclosure`, `rules`(`COUPANG_RULES`), `next_step`(`COUPANG_EMPTY_NEXT`·`COUPANG_UNAVAILABLE_NEXT`) | `coupang_search` | 아래 2-5 |

- `save_client_profile` 이 `saved=false` 를 주면 "기억해 둘게요"라고 말하지 않게 했다(도구 설명과 SOUL·SKILL).
- 네이버 쇼핑 검색 API 는 2026-07-31 에 종료됐다. 경로는 지우지 않고 주석만 달았다.
  - `mcp/shopping_tools/naver.py` 모듈 설명과 `NAVER_URL`
  - `mcp/shopping_tools/server.py` 의 `_naver_provider` 와 `SEARCH_PROVIDERS` 의 naver 줄
- `make_search_links` 의 네이버쇼핑 검색 링크(m.search.naver.com)는 이 API 가 아니다.

### 2-4. 후보 페이지 확인 (`check_candidates`, `mcp/shopping_tools/server.py`)
- 웹 검색 후보(3-b·1-c)를 답에 내기 전에 부른다. `candidates=[{url, title, mall}]`, 최대 `MAX_CHECK=3`. `client_id`(로그의 naver_rule 용), `attempt`(1 또는 2).
- 한 요청에 2번까지(`MAX_CHECK_ATTEMPTS=2`, 사용자 결정). 후보가 없으면 `next_step` 이 attempt=1 에서는 "3-c 로 한 번 더"(`NO_CANDIDATE_RETRY`), 2 에서는 "더 찾지 말고 '제가 찾는 방법으로는 못 찾았어요.'"(`NO_CANDIDATE_STOP`). attempt 는 에이전트가 넘기는 값이라 코드가 요청 경계를 아는 것은 아니다.
- `drop_reason`: drop_sold_out "품절이에요", drop_profile "피하시는 조건에 해당해요", drop_deadline "기한 안에 못 와요". 답에서 "✗ 쇼핑몰: 이유" 한 줄로 남긴다(SOUL "탈락한 것도 이유를 한 줄"). 쿠팡·크라우드펀딩·잘못된 주소, 국내 후보가 있어 숨긴 overseas 에는 없다.
- 로그(stderr, 주소·사용자 ID·상품명 없음, 호출마다 한 줄):
  `path=check_candidates attempt=<n> n=<확인한 수> naver_rule=<1|0|na> naver_hosts=<smartstore·brand.naver 후보 수> shown=<낸 수> ok= unreadable= overseas= soldout= avoid= deadline= skipped=`
  - naver_rule: 프로필 places 에 네이버가 있으면 1, 없으면 0, client_id 가 없거나 프로필이 없으면 na.
  - 읽는 법: naver_rule=1 이고 naver_hosts=0 이면 낸 후보에 네이버가 없었다. 검색 결과에 아예 없었는지는 이 줄로 알 수 없다(Hermes 세션 기록의 web_search 결과로 본다).
  - stdio MCP 의 stderr 가 어디에 남는지(Hermes 로그 등)는 확인 필요.
- 후보마다 상품 페이지를 GET 한 번 읽는다(`_check_one`, `check_product` 와 같은 판정). 동시에 읽고, 한 후보에 `CHECK_BUDGET_S=8`초를 넘기면 `unverified`("시간 안에 못 읽음")로 둔다.
- 쿠팡(`coupang.com`)·크라우드펀딩 주소에는 요청하지 않는다(`drop_not_candidate`). 다시 시도·링크 따라가기·주기적 확인은 없다.
- `verdict`: `ok` / `unverified`(못 읽음·재고 근거 없음, 내되 밝힘) / `overseas`(해외직구·해외배송·구매대행) / `drop_sold_out` / `drop_profile` / `drop_deadline` / `drop_not_candidate` / `drop_bad_url`.
- `show`: ok·unverified 는 true. overseas 는 ok·unverified 가 하나도 없을 때만 true.
- `say` 는 한 줄 글자다(조각을 " · " 로 잇는다). 배송비를 알면(페이지 → 없으면 `candidates[].shipping_fee_krw`, 검색 결과에서 본 값) "배송비는 확인하지 못했어요"를 넣지 않고 "배송비 5,500원 따로 · 재고는 확인하지 못했어요"처럼 따로 말한다. 조각: "해외배송 · 외국에서 오는 거라 2주쯤 걸려요", "아동용 상품이에요", "재고와 배송비는 확인하지 못했어요" / "재고는 확인하지 못했어요" / "배송비는 확인하지 못했어요", "배송비 N원 따로"(값과 "무료배송" 문구가 같이 있으면 "(조건에 따라 무료일 수 있어요)"), "무료배송".
- 배송비(`_shipping_fee`): "배송비 X" 값이 하나로 정해질 때만. 여러 값(지역·옵션별)이면 모름.
- `check_product` 는 1-b(사용자가 보낸 링크)용으로 남겼다. 반환에 `shipping_fee_krw`·`verdict`·`say` 가 더해졌다. `passed` 의 뜻은 그대로다.
- `gates._OVERSEAS` 에 "구매대행"이 더해졌다.

### 2-5. 쿠팡 파트너스 검색 (`coupang_search`, 2026-11-25)

**언제**: 프로필 `payment.places` 에 쿠팡이 있는 분만. place_rules 가 알려 준다. 웹 검색보다 먼저 부르고, 검색어를 바꿔 세 번 다 맞는 것이 없을 때만 웹 검색으로 간다. 그 밖의 분은 지금 흐름 그대로다. `search_products` 에서는 쿠팡을 뺐다(쿠팡을 안 쓰는 분에게 쿠팡 결과가 나가지 않게, 호출 한도를 두 번 쓰지 않게).

**호출** (`mcp/shopping_tools/coupang_partners.py`)
- `GET /v2/providers/affiliate_open_api/apis/openapi/v1/products/search?keyword=…&limit=10`, HMAC 서명. 경로·서명은 11/25 시험 스크립트로 200 을 확인했다.
- 분당 30회(문서 한도 50보다 낮게), 요청 시간 상한 5초.
- 반환 `status`:
  - `ok`: 상품이 있다.
  - `empty`: rCode 0 인데 0건이거나, 채움 결과(landingUrl 이 `https://link.coupang.com/re/AFFSRP` 로 시작하지 않음). 찾아봤는데 없는 것이다.
    - 11/25 실사용: 검색 결과가 없으면 API 가 골드박스 상품(코카콜라·냄비·노트북 등, 호출마다 다름)으로 채워 돌려준다. 그때 landingUrl 은 `AFFGOLDBOX`, 정상 검색이면 `AFFSRP?…pageKey=<검색어>`.
  - `unavailable`: 키·스위치가 없거나, 한도·시간 초과·네트워크 예외·HTTP 오류·rCode≠0·형식 오류. 예외를 내지 않는다.
- 로그(stderr): `path=coupang_search outcome=<ok|empty|filler|unavailable> rows=<n>` / `reason=<이유>`. 키·서명·검색어는 남기지 않는다.
- httpx·httpcore 로거는 WARNING 이상만 남긴다(`server.py` 맨 위). 11/25 mcp-stderr.log 에 httpx 가 INFO 로 요청 주소(검색어 포함)를 남기고 있었다. 후보 확인(check_candidates)의 상품 주소도 같이 빠진다.
- `isFreeShipping` 칸이 없는 줄이 많다. 없으면 무료배송이 아닌 것으로 본다.

**다시 찾기** (`coupang_search(keyword, attempt)`, `MAX_COUPANG_ATTEMPTS=3`)
- 검색어 순서(`COUPANG_QUERY_ORDER`, place_rules 에도 실림): ① 브랜드 + 사용자가 말한 특징을 말 그대로 ② 브랜드를 빼고 종류 + 특징 ③ 브랜드만. 특징은 향·라인·시리즈 이름이고 용량·색·유통 관련 말은 넣지 않는다(사용자 결정). 같아지거나 넣을 말이 없는 단계는 건너뛴다.
  - 11/25 실사용 검색어별 결과: "메구리즘 유자"·"메구리즘 아이마스크"·"멕리듬 아이마스크" 채움 / "메구리즘" 정상 1건(다른 제품) / "메구리즘 유자향" 정상(유자 포함 4줄) / "스팀 아이마스크 유자" 정상.
- empty 의 `next_step`: attempt 1·2 는 `COUPANG_RETRY_NEXT`(다음 단계로 attempt+1, 사용자에게 아직 말하지 않음), 3 은 `COUPANG_STOP_NEXT`("쿠팡에서는 제가 찾는 방법으로는 못 찾았어요"로 시작하고 웹 검색).
- ok 에는 `next_if_no_match`(거른 뒤 맞는 것이 없을 때 같은 순서로)가 실린다. attempt 4 이상은 요청하지 않고 STOP 을 돌려준다. attempt 는 에이전트가 넘기는 값이라 코드가 물건 경계를 아는 것은 아니다.

**묶기** (`server._group_products`)
- productUrl 의 `pageKey` 로 묶는다. pageKey 가 같고 itemId 가 다른 줄은 옵션 차이다. 순서는 처음 나온 줄 순서다.
- 묶음 칸: `name`, `price_min_krw`·`price_max_krw`(정수, 19000.0 → 19000), `price_varies`, `row_count`, `is_rocket`, `is_free_shipping`, `url`.
  - 대표 값(이름·로켓·무료배송·주소)은 가장 싼 줄 것이다. 값이 같으면 앞 줄이다.
- `rocket_option`: 가장 싼 줄이 로켓이 아닌데 같은 묶음에 로켓 줄이 있을 때, 그중 가장 싼 줄의 `name`, `price_krw`, `is_free_shipping`, `url`.
- 이름에 옵션 글자가 있는지는 추정하지 않는다("…-200ml-가든화이트" 같은 이름에서 틀린다). 에이전트가 name 을 읽는다.
- url 은 한 글자도 바꾸지 않는다.

**응답에 싣는 규칙** (`COUPANG_RULES`)
- 맨 첫 줄은 disclosure("이 게시물은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다.").
- name 으로 거르기: 말한 브랜드의 상품만. 다른 표기(음차, 한국 판매명, 영문)는 같은 브랜드로 보고 답에 한 줄로 밝힌다("쿠팡에서는 '멕리듬'이라는 이름으로 팔아요"). 확신이 없으면 버리지 않고 "같은 브랜드인지 확인해 주세요". 별칭 표는 코드에 두지 않는다(사용자 결정). 말하지 않은 세트·다른 향/라인은 버린다.
- 조건은 name 에 있을 때만 맞다고 말하고, 없으면 "들어가서 골라 주세요". "7종 택1 (…/유자/…)"처럼 나열된 옵션 안에 말씀하신 옵션이 있으면 후보로 삼고 "유자는 들어가서 골라 주세요".
- 고르는 순서(의뢰인 기준):
  - 브랜드 상품: 로켓 먼저, `rocket_option` 을 고른다. 로켓이 아니면 "판매자와 배송을 들어가서 확인해 주세요". 공식·정품이라고 말하지 않는다.
  - 그 밖: 조건에 맞는 것 중 싼 순서.
- 가격·배송 문구:
  - 가격: "지금 약 …원, 들어가시면 달라질 수 있어요". price_varies 면 "약 …원부터, 옵션에 따라 달라요".
  - 배송: 로켓은 "로켓배송"만, 무료배송은 is_free_shipping 일 때만, 둘 다 아니면 "배송비가 더 붙을 수 있어요".
- 링크·기록:
  - 링크 줄은 `N번 쿠팡: url`. check_candidates 는 부르지 않는다(코드도 coupang.com 하위 주소에는 요청하지 않는다).
  - record_item 에는 사용자가 말한 것 + seller="쿠팡"만 넣는다.
- `empty` → 위 "다시 찾기". 세 번 다 없으면 "쿠팡에서는 제가 찾는 방법으로는 못 찾았어요"로 시작하고 웹 검색, 쿠팡 검색어 줄 없음. `unavailable` → 옛 흐름 그대로(옛 COUPANG_FIRST_NOTE), 사용자에게 말하지 않음.

**카카오 브릿지** (`kakao-bridge-vps/server.py`)
- 버튼으로 나가는 링크(최대 3개)에 `link.coupang.com` 주소가 있으면, 본문에서 `DISCLOSURE` 문장을 모두 지우고 맨 첫 줄에 한 번 둔다. 링크만 온 답은 `DISCLOSURE` + `LINKS_ONLY_BODY` 로 나간다.
- `DISCLOSURE` 는 브릿지와 `coupang_partners.py` 두 곳에 있다. 바꿀 때는 둘 다 바꾼다.
- 본문 속 주소는 ASCII 글자까지만 주소로 본다. 주소 바로 뒤의 한글 조사가 주소에 섞이지 않는다.
- 400자를 넘으면 나뉜다. 말풍선이 3개를 넘으면 가운데 조각이 빠지고, 카드 설명은 끝이 잘린다. 앞(첫 줄)은 남는다.
- 카카오 공식 문서(answer_json_format)의 Button `webLinkUrl` 에는 길이 제한이 적혀 있지 않다(제한은 "URL"뿐). 실제 주소 길이로 열리는지는 실사용으로 본다.

## 3. 이번에 바뀐 프롬프트 규칙의 위치

Hermes 프로필 쪽 사본은 따로 갱신해야 한다. `~/.hermes/` 는 작업자가 건드리지 않는다.

**SKILL.md** (`hermes/skills/hyojason-shopping/SKILL.md`)

| 섹션 | 바뀐 규칙 |
|---|---|
| 1. 요청 해석 | 지난 기록 확인은 `confirm_phrase` 를 그대로 쓴다. 기한은 1단계에서 뽑기만 한다 |
| 1-a. 상표·제품 이름이 안 보이는 사진 | 크기는 "종이컵만 한가요, 생수병만 한가요?"처럼 아는 물건 둘 중에 고르게. ③ 예시는 색 |
| 1-d. 확인받은 지난 물건 다시 드리기 (새 섹션) | url 이 있으면 다시 검색하지 않는다. 제품 줄 + "그때 본 가격이라 지금은 다를 수 있어요. 재고·도착일은 링크에서 확인하세요." |
| 2. 되묻기 (한 번에) | 질문 묶음은 한 번만. 기한은 사용자가 꺼냈을 때만. 예시 질문은 색 |
| 3. 검색 | 브랜드가 있으면 "브랜드 + 종류"로 넓게. 쇼핑몰 표기로 바꾸기. 크기 비유는 검색어에 넣지 않기 |
| 3-b. 웹 검색으로 후보 찾기 | 1단계의 기한 조건. 가격은 실제 판매가 |
| 3-c. 못 찾았을 때 검색어 바꿔 다시 찾기 | 최대 3번 다시 찾기 |
| 7. 기록 | `record_item` 에 url |
| 후보를 드리는 답에서 할 일 | url 기록, 가격(판매가 / 확신 없으면 단정 금지), `payment_places`, `saved=false`, `place_rules`(쿠팡·네이버), 답 맨 앞 줄 |
| 링크 표기 규칙 | 링크만 있는 답 금지(물건마다 제품·옵션·가격 줄 + "재고·도착일은 링크에서 확인하세요") |
| 대화 규칙 | 찾기 전 군말("알겠어요. 사진 속 ○○ 찾아볼게요.") 금지, 도구를 부르기 전에 글을 쓰지 않기 |
| (c79139a) description | 웹 검색 + 페이지 확인 흐름으로 |
| (c79139a) 1. 요청 해석 | confirm_phrase 세 가지, 확인 질문은 그 문장으로 시작, 비슷한 것 묶음에서 고르기/다시 찾기 |
| (c79139a) 1-a | 비슷한 것·닮은 제품은 `match="similar"` |
| (c79139a) 1-c 4·5·7 | 낼 상품 페이지를 `check_candidates` 로 확인, say 줄 + "도착일은 링크에서 확인하세요" |
| (c79139a) 3-b 5·6 | 검증 생략 → `check_candidates` 로 확인, say 줄을 후보 줄 아래에 |
| (c79139a) 후보 페이지 확인 (새 섹션) | `check_candidates` 사용법(show·say·overseas·unverified·buy_url) |
| (c79139a) 4. 검증 | 사용자가 보낸 링크(1-b)용으로 이름을 좁힘 |
| (c79139a) 후보를 드리는 답에서 할 일 | record_item 의 `match` 필수, 배송비 say 를 가격 아래에 |
| (c79139a) 대화 규칙 | 군말을 "확인 질문 앞 설명"까지, 해요체만, 내부 용어 금지 |

| (11/25) description, 1-c 3·4·7, 1-d, 3, 3-b 3 | 쿠팡이 주로 사는 곳이면 coupang_search 먼저. 웹 검색의 coupang.com 주소만 제외. 쿠팡을 찾아봤는데 없으면 검색어 줄 대신 "쿠팡에서는 못 찾았어요" |
| (11/25) 쿠팡 검색 (새 섹션) | coupang_search 사용법(status 셋, 거르기·순서·가격·배송·기록, 예시) |
| (11/25) 후보를 드리는 답에서 할 일 | 쿠팡 기록 규칙, place_rules 의 쿠팡 규칙 둘, 답 맨 앞 줄은 대가성 문구, 끝 줄 순서에서 대가성 문구 뺌 |
| (11/25) 링크 표기 규칙 | 대가성 문구는 맨 첫 줄(전에는 링크 줄 바로 위), `1번 쿠팡: url` |
| (11/25 실사용 반영) 쿠팡 검색, 1-c 7, 후보를 드리는 답에서 할 일 | 검색어 순서 ①②③·attempt·최대 3번, 브랜드 다른 표기, 옵션 나열 상품, 세 번 다 없으면 "쿠팡에서는 제가 찾는 방법으로는 못 찾았어요" |

**SOUL.md** (`hermes/SOUL.md`)

| 섹션 | 바뀐 규칙 |
|---|---|
| 네가 하는 일 | 2번: 기한은 사용자가 꺼냈을 때만 |
| 절대 규칙 | 정가 대신 실제 판매가 / 확인 못 한 사실 말하지 않기 |
| 대화 규칙 | 질문 묶음 한 번, 사진 확인 ①②③(③ 예시는 색) |
| 말투 | 결제 질문 자리, 군말 금지, 크기 비교 질문, 버튼 답에는 제품·옵션·가격 줄, 도착일은 확인한 경우에만 |
| 클라이언트 프로필 | `confirm_phrase`, 확인받은 기록의 url 먼저, `payment_places`, `saved=false`, `place_rules`(쿠팡·네이버) |
| (c79139a) 네가 하는 일 3 | 후보 페이지를 check_candidates 로 확인 |
| (c79139a) 절대 규칙 | 해외배송 줄에 check_candidates show·say, 배송비, 못 읽은 후보 문장 |
| (c79139a) 말투 | 해요체만, 군말 범위 넓힘, 내부 용어 금지 |
| (c79139a) 클라이언트 프로필 | confirm_phrase 세 가지·첫 글자부터, record_item 의 match |
| (11/25) 3번째 줄, 네가 하는 일 3, 클라이언트 프로필 place_rules | 웹 검색의 쿠팡 주소 제외, coupang_search 먼저, 쿠팡 규칙 둘 |

## 4. 테스트

```bash
cd kakao-bridge-vps && uv run --project . python -m pytest -q   # 브릿지: 50 passed
cd mcp && uv run python -m pytest -q                            # MCP: 102 passed
```
- 다른 venv 가 켜져 있으면 uv 가 `VIRTUAL_ENV ... will be ignored` 경고를 내지만 결과에는 영향이 없다.
- 프롬프트(SOUL·SKILL) 규칙은 테스트로 확인되지 않는다. 실사용으로 본다.

## 5. 작업 규칙
- `~/.hermes/` 는 읽지도 쓰지도 않는다.
- `.env` 는 건드리지 않는다.
- README 는 지시가 있기 전까지 건드리지 않는다.
- 쿠팡을 포함해 쇼핑몰에 자동 요청을 보내지 않는다.
- 실제 프로필 파일은 git 에 올리지 않는다(`clients/*` 는 `.gitignore`). 옛 프로필이 그대로 읽히게 한다.
- 확실하지 않으면 추측하지 말고 멈춰서 묻는다. 확인하지 못한 사실은 코드가 아니라 보고에 "확인 필요"로 적는다.
- 외부 API·서비스는 공식 문서를 직접 읽고 확인한 내용으로만 구현한다. 못 읽으면 멈추고 무엇이 필요한지 말한다.
- 수정 전에 원인을 먼저 설명한다.
- 보고는 항목마다 원인 / 고친 것 / 정할 것으로 나누고, 테스트 결과와 커밋 해시를 적는다.
- 파괴적 명령은 먼저 확인한다.
- 커밋 메시지는 한국어로 쓰고, 왜 바꿨는지 적는다.
- 커밋하면 origin 으로 push 까지 하고, 보고에 push 했는지 적는다(force push 금지. main 이 아니거나 origin/main 과 갈라져 있으면 push 하지 않고 멈춰서 알린다).

## 6. 확인된 사실
- VPS 시계는 UTC 다. 날짜를 정하는 코드는 한국 시간(`Asia/Seoul`)을 쓴다(`record_item` 의 date·asked_on).
- 네이버 쇼핑 검색 API 는 2026-07-31 에 종료됐다(네이버 개발자센터 공지, 사용자가 원문 확인).
- 카카오 사진 메시지의 utterance 는 사진 주소다. 형태 로그로 확인했다: `starts_url=1 has_param_image=1`, 길이 196.
- 카카오 콜백(`callbackUrl`)은 메시지마다 1회, 최대 5분 유효하다.
- (11/25, 사용자가 시험 스크립트로 확인) 쿠팡 파트너스 검색:
  - 응답: v1 경로, HTTP 200, rCode "0". 상품 칸은 rank, productName, productPrice, isRocket, isFreeShipping, productUrl, keyword 이고, categoryName·판매자·품절·해외배송·옵션 칸은 없다.
  - 가격: productPrice 는 소수로 올 때가 있고 배송비를 뺀 값이다. 로켓·무료배송이 둘 다 아닌 상품 하나는 배송비 2,500원에 판매자가 해외 법인이었다.
  - 주소: productUrl 은 `https://link.coupang.com/re/AFFSDP?…pageKey=…&itemId=…&vendorItemId=…` 이고 카카오 인앱 브라우저에서 열린다. 같은 상품이 여러 줄로 온다(pageKey 같음, itemId 다름).
  - 검색어: "R.LUX"·"알럭스"를 더하면 결과가 나빠진다.
  - 호출 제한: 문서상 분당 50회.

## 7. 아직 정해지지 않은 것
- 후보 검증을 계속 둘지 끌지. 8316893 실사용: "록시땅 버베나 핸드크림" 한 건이 브릿지 기준 163초(전 약 120초)였는데, 얻은 것이 없었다(롯데온 품절은 "확인하지 못했어요", LF몰 해외직구·구매대행은 못 잡음). 상한을 8초로 줄인 뒤 다시 잰다.
- 5개 읽고 3개 내기(빠지는 후보를 메울 여유 vs 쇼핑몰 요청 증가). 지금은 `MAX_CHECK=3`.
- 네이버 후보 고르기를 코드로 옮기기(웹 검색 상품 주소를 최대 10개 넘기고 코드가 smartstore·brand.naver 를 앞에 두어 고름, `seen=… naver_seen=…` 로그).
- check_candidates 개수 로그가 어디에 남는지(아래 9). 파일 로그를 둘지.
- LF몰 해외직구·구매대행을 못 잡은 원인(아래 9). 확인 필요.
- (c79139a) 쇼핑몰별로 페이지를 실제로 읽을 수 있는지(롯데온·LF몰 등)는 확인하지 않았다(작업자는 쇼핑몰에 요청하지 않는다). 실사용 로그로 본다. 확인 필요.
- (c79139a) "해외직구"·"품절"이 상품이 아니라 메뉴·추천 영역 글자에 있으면 잘못 걸릴 수 있다(본문 앞 6,000자를 통째로 본다). 확인 필요.

- (11/25) 쿠팡 파트너스 productUrl 실제 길이와 그 길이의 버튼이 카카오에서 열리는지. 사용자가 실사용으로 확인한다. 확인 필요.
- (11/25) 응답 규칙(거르기·로켓 우선·가격 표현)이 실사용에서 지켜지는지. 확인 필요.
- (11/25) 채움 판정은 landingUrl 접두사 하나에 기대고 있다. 쿠팡이 이 주소 형식을 바꾸면 정상 결과도 empty 가 된다(로그 outcome=filler 가 늘면 의심). 확인 필요.
- (11/25) 다른 표기 브랜드를 같은 브랜드로 볼지는 에이전트 판단이다. 잘못 묶거나 버리는지 실사용으로 본다.

## 8. 정해진 것 (2026-11-20)
- 비슷한 것 묶음은 날짜로만 나눈다. 같은 날 다른 물건을 비슷한 것으로 또 찾아 드리면 한 묶음이 된다.
- check_candidates 는 한 요청에 2번까지. 두 번째에도 없으면 "제가 찾는 방법으로는 못 찾았어요".
- 품절 등으로 뺀 후보는 기존 규칙대로 이유 한 줄을 남긴다.
- 네이버 관찰은 check_candidates 의 개수 로그 한 줄로 한다.
- SOUL.md "절대 규칙"의 쇼핑과 무관한 입력 줄에 있는 "그건 자녀분께 직접 물어보시는 게 좋겠어요" 표현. 지금 파일에서는 38행이다.
  - SKILL 3-b 쪽은 5f4fc69 에서 "가족이나 주변 분께"로 바뀌었다.
- 배송비를 기록(items)에 남길지.
- 찾기 전 군말이 계속 나오면, Hermes 응답의 글 덩어리 개수를 로그로 남길지. 개수만 남기고 내용은 남기지 않는다.
  - 도구를 부르기 전에 쓴 글이 최종 답에 합쳐 오는지 확인하려는 것이다. 지금은 넣지 않기로 했다.

## 9. 8316893 시험에서 본 것 (원인 후보, 확인 필요)

**LF몰에서 배송비는 나왔는데 해외직구·구매대행을 못 잡은 이유 (코드상 가능한 것)**
1. 그 배송비는 페이지에서 읽은 것이 아닐 가능성이 크다. 8316893 코드에서 "재고와 배송비는 확인하지 못했어요"는 페이지 배송비가 없을 때(`fee is None`)만 나온다. 따라서 "배송비 5,500원 따로"는 에이전트가 검색 결과를 보고 따로 쓴 줄이고, 페이지는 못 읽었을(차단·JS 렌더링·15초 초과) 가능성이 크다.
2. 페이지를 못 읽으면 해외 판정 근거는 에이전트가 넘긴 title·mall 뿐이다. 거기에 "해외직구"가 없으면 규칙 판정(`gates._rule_decide`)은 `domestic_shipping` 을 "yes"로 둔다(모를 때도 "unknown"이 아니라 "yes").
3. 읽었더라도: 본문은 앞 6,000자만 본다(`_fetch_page` limit). 표시가 그 뒤에 있으면 못 본다. Jev 엔진이면 앞 4,000자만 넘긴다.
4. 읽었더라도: "해외직구"가 이미지 배지(글자 없음)나 JS 로 그려지는 영역에 있으면 정적 HTML 텍스트에 없다.
5. check_candidates 가 아예 불리지 않았을 수도 있다. 개수 로그 줄이 없었고, "재고와 배송비는 확인하지 못했어요"는 SKILL 에도 적힌 문장이라 에이전트가 직접 썼을 수 있다. 롯데온(품절 페이지)이 "확인하지 못했어요"로 나온 것도 같은 두 가능성(못 읽음 / 안 부름)이다.

**개수 로그가 어디로 가는지**
- 로그는 MCP 서버(`shopping_tools.server`)의 stderr 로 나간다. 이 서버는 Hermes 가 stdio 자식 프로세스로 띄우므로, stderr 가 journal 로 가는지·Hermes 가 따로 받는지·버려지는지는 Hermes 쪽 처리에 달렸다. 확인 필요.
- VPS 에서 볼 명령(사용자가 실행):
  ```bash
  pgrep -af shopping_tools.server                 # MCP 서버가 떠 있는지, PID
  ps -o pid,lstart,cmd -p <PID>                   # 시작 시각: git pull 뒤에 다시 떴는지(안 떴으면 옛 코드라 check_candidates 가 없다)
  ls -l /proc/<PID>/fd/2                          # stderr 가 어디로 가는지(journal 소켓 / 파일 / /dev/null / pipe)
  grep -n "def check_candidates" <REPO>/mcp/shopping_tools/server.py   # VPS 코드에 도구가 있는지
  journalctl --user -u hermes-gateway --since today | grep -c "path=check_candidates"
  grep -rn "path=check_candidates" ~/.hermes/logs 2>/dev/null | tail -5   # Hermes 가 MCP stderr 를 자기 로그에 남기는 경우
  ```
  - fd/2 가 pipe 면 Hermes 가 받는 것이다. 그때는 Hermes 로그 위치를 사용자가 확인한다.
- 위로도 안 보이면 파일 로그 안(제안, 넣지 않음):
  - 경로: `~/.local/state/hyojason/check_candidates.log`(환경변수 `HYOJASON_LOG_DIR` 로 바꿀 수 있게). `~/.hermes` 밖.
  - 내용: 지금 줄 앞에 한국 시각만 붙인다. `2026-11-20T15:04:05+09:00 path=check_candidates attempt=1 n=3 naver_rule=1 naver_hosts=0 shown=2 ok=0 unreadable=2 …` 주소·사용자 ID·상품명은 없다.
  - 크기: `RotatingFileHandler` 1MB × 3개(최대 약 4MB). 한 줄 약 150바이트라 1MB 에 약 7천 건.

