# shopping_tools (MCP)

```bash
cd mcp
uv sync                      # 또는: pip install -e .
export NAVER_CLIENT_ID=... NAVER_CLIENT_SECRET=... HYOJASON_CLIENTS_DIR=../clients
uv run python -m shopping_tools.server     # stdio MCP 서버
```

키가 없으면 `search_products` 는 mock 3건을 돌려준다. 빠른 확인:

```bash
HYOJASON_CLIENTS_DIR=/tmp/clients python -c "
import asyncio; from shopping_tools.server import search_products, check_product
print(asyncio.run(search_products('살색 보디수트'))['count'])
print(asyncio.run(check_product('https://example.com', title='[해외직구] 보디수트', deadline='이번 주 안')))
"
```

| 도구 | 설명 |
|---|---|
| `resolve_client(channel, channel_user_id)` | 채널 ID → client_id |
| `get_client_profile(client_id)` | 프로필 읽기 |
| `save_client_profile(profile_json)` | 새 프로필 |
| `update_client_profile(client_id, patch_json)` | 부분 갱신(리스트는 합집합) |
| `search_products(query, max_results, sort)` | 네이버 쇼핑 검색 (중고·단종·판매예정 제외) |
| `check_product(product_url, title, mall, deadline, forbidden_terms_json)` | 페이지 읽기 + 4개 게이트 → `passed`, `note`, `buy_url` |
| `log_recommendation(client_id, request_json, chosen_json)` | 이력 |

Jev 연결: `shopping_tools/gates.py` 의 `jev.decide(state, ProductGates)` 한 번으로 네 게이트를 판정한다(상품 1개 = Jev 1회). `TYPESAFE_API_KEY` 가 없으면 정규식 규칙으로 폴백.
