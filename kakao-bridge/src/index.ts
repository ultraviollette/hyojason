/**
 * 효자손 카카오 브릿지 (Cloudflare Worker)
 *
 * 카카오 i 오픈빌더 스킬 서버 → Hermes Agent API → 카카오 Callback API
 *
 * 흐름
 *  1. 카카오가 POST /kakao/skill 로 사용자 발화(+사진)를 보낸다. 스킬 서버는 5초 안에 응답해야 한다.
 *  2. 우리는 즉시 { useCallback: true } 로 답하고 (블록에서 Callback API 설정을 켜 두어야 함),
 *     백그라운드(waitUntil)에서 Hermes 에게 질문한다. 쇼핑 검색은 30초~1분 걸릴 수 있다.
 *  3. Hermes 답을 userRequest.callbackUrl 로 POST 한다 (유효 5분, 1회).
 *
 * 문서: 카카오비즈니스 가이드 "AI 챗봇 콜백 가이드", "스킬 응답 포맷"
 */

export interface Env {
  HERMES_API_URL: string;   // 예: https://xxxx.trycloudflare.com  (Mac 의 hermes gateway 터널)
  HERMES_API_KEY: string;   // Hermes API_SERVER_KEY
  HERMES_MODEL?: string;    // 비우면 Hermes 기본 모델
  KAKAO_BOT_SECRET?: string; // 선택: 오픈빌더 스킬 URL 에 ?secret=... 를 붙여 검증
}

type KakaoSkillRequest = {
  userRequest: {
    utterance: string;
    callbackUrl?: string;
    user: { id: string; type: string; properties?: Record<string, unknown> };
    params?: Record<string, unknown>;
    lang?: string;
    timezone?: string;
  };
  action?: { params?: Record<string, unknown>; detailParams?: Record<string, unknown> };
  bot?: { id: string; name: string };
};

const QUICK_ACK = "네, 찾아보고 있어요. 잠시만 기다려 주세요 🙂";

export default {
  async fetch(req: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(req.url);

    if (req.method === "GET" && url.pathname === "/health") {
      return json({ ok: true, service: "hyojason-kakao-bridge" });
    }

    if (req.method === "POST" && url.pathname === "/kakao/skill") {
      if (env.KAKAO_BOT_SECRET && url.searchParams.get("secret") !== env.KAKAO_BOT_SECRET) {
        return new Response("forbidden", { status: 403 });
      }
      let body: KakaoSkillRequest;
      try {
        body = (await req.json()) as KakaoSkillRequest;
      } catch {
        return kakaoText("요청을 읽지 못했어요. 다시 보내 주세요.");
      }

      const ur = body.userRequest;
      const text = (ur?.utterance ?? "").trim();
      const imageUrls = extractImageUrls(body);
      const kakaoUserId = ur?.user?.id ?? "unknown";

      // 콜백을 못 쓰는 상황(설정 안 됨)이면 동기 응답을 시도하되 4.5초에서 끊는다.
      if (!ur?.callbackUrl) {
        const reply = await withTimeout(askHermes(env, kakaoUserId, text, imageUrls), 4500)
          .catch(() => "지금은 답이 늦어지고 있어요. 블록에 Callback API 설정을 켜 주세요.");
        return kakaoText(reply);
      }

      // 비동기 경로: 즉시 ack → 백그라운드에서 Hermes → callbackUrl 로 최종 답
      ctx.waitUntil(
        (async () => {
          let reply: string;
          try {
            reply = await withTimeout(askHermes(env, kakaoUserId, text, imageUrls), 240_000);
          } catch (e) {
            reply = `죄송해요, 찾는 데 문제가 생겼어요. 잠시 뒤 다시 말씀해 주세요. (${(e as Error).message})`;
          }
          await fetch(ur.callbackUrl!, {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify(kakaoTextBody(reply)),
          });
        })(),
      );

      return json({ version: "2.0", useCallback: true, data: { text: QUICK_ACK } });
    }

    return new Response("not found", { status: 404 });
  },
} satisfies ExportedHandler<Env>;

// ---------- Hermes ----------

/**
 * Hermes Agent 의 OpenAI 호환 API (/v1/responses) 호출.
 * `conversation` 을 카카오 사용자 ID 로 고정해 사용자별 대화 맥락을 Hermes 쪽에 유지한다.
 * 사용자 식별자는 metadata 로도 넘겨 SKILL.md 의 resolve_client(channel="kakao") 가 쓰게 한다.
 * ⚠️ 요청/응답 필드명은 Hermes 버전에 따라 다를 수 있다. 설치본의 docs/user-guide/features/api-server 로 확인.
 */
async function askHermes(env: Env, kakaoUserId: string, text: string, imageUrls: string[]): Promise<string> {
  const content: Array<Record<string, string>> = [];
  const prefix = `[channel=kakao user_id=${kakaoUserId}]`;
  content.push({ type: "input_text", text: text ? `${prefix}\n${text}` : `${prefix}\n(사진만 보냄)` });
  for (const u of imageUrls) content.push({ type: "input_image", image_url: u });

  const payload: Record<string, unknown> = {
    input: [{ role: "user", content }],
    conversation: `kakao-${kakaoUserId}`,
    metadata: { channel: "kakao", channel_user_id: kakaoUserId },
  };
  if (env.HERMES_MODEL) payload.model = env.HERMES_MODEL;

  const r = await fetch(`${env.HERMES_API_URL.replace(/\/$/, "")}/v1/responses`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${env.HERMES_API_KEY}` },
    body: JSON.stringify(payload),
  });
  if (!r.ok) throw new Error(`hermes ${r.status}`);
  const data = (await r.json()) as Record<string, unknown>;
  return extractText(data) || "답을 만들지 못했어요. 다시 한 번 말씀해 주세요.";
}

function extractText(data: Record<string, unknown>): string {
  if (typeof data.output_text === "string") return data.output_text;
  const out = data.output as Array<Record<string, unknown>> | undefined;
  if (Array.isArray(out)) {
    const parts: string[] = [];
    for (const item of out) {
      const c = item.content as Array<Record<string, unknown>> | undefined;
      if (Array.isArray(c)) for (const p of c) if (typeof p.text === "string") parts.push(p.text);
    }
    if (parts.length) return parts.join("\n");
  }
  const choices = data.choices as Array<Record<string, unknown>> | undefined; // chat/completions 형태 대비
  const msg = choices?.[0]?.message as Record<string, unknown> | undefined;
  if (typeof msg?.content === "string") return msg.content;
  return "";
}

// ---------- Kakao helpers ----------

/**
 * 사용자가 보낸 사진 URL 을 찾는다.
 * 공식 문서에 필드명이 명시돼 있지 않아(2026-11 기준) params 전체에서 URL 처럼 생긴 값을 긁는다.
 * 실제 페이로드를 로그로 확인한 뒤 정확한 키로 좁힐 것 (멤버 제보: secureURL 로 전달됨).
 */
function extractImageUrls(body: KakaoSkillRequest): string[] {
  const urls = new Set<string>();
  const walk = (v: unknown, depth = 0) => {
    if (depth > 5 || v == null) return;
    if (typeof v === "string") {
      if (/^https?:\/\/\S+/i.test(v) && /(secure|image|media|img|photo|kakaocdn)/i.test(v)) urls.add(v);
      return;
    }
    if (Array.isArray(v)) return v.forEach((x) => walk(x, depth + 1));
    if (typeof v === "object") for (const x of Object.values(v as Record<string, unknown>)) walk(x, depth + 1);
  };
  walk(body.userRequest?.params);
  walk(body.action?.params);
  walk(body.action?.detailParams);
  return [...urls].slice(0, 3);
}

function kakaoTextBody(text: string) {
  // simpleText 는 1000자 제한, 500자 넘으면 '전체 보기' 로 접힌다. 길면 둘로 나눈다.
  const chunks = splitText(text, 480).slice(0, 3);
  return { version: "2.0", template: { outputs: chunks.map((t) => ({ simpleText: { text: t } })) } };
}

function kakaoText(text: string) {
  return json(kakaoTextBody(text));
}

function splitText(s: string, n: number): string[] {
  const out: string[] = [];
  let cur = "";
  for (const line of s.split("\n")) {
    if ((cur + "\n" + line).length > n && cur) {
      out.push(cur);
      cur = line;
    } else cur = cur ? cur + "\n" + line : line;
  }
  if (cur) out.push(cur);
  return out.length ? out : [s.slice(0, n)];
}

function json(obj: unknown, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json" } });
}

function withTimeout<T>(p: Promise<T>, ms: number): Promise<T> {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error(`timeout ${ms}ms`)), ms);
    p.then((v) => { clearTimeout(t); resolve(v); }, (e) => { clearTimeout(t); reject(e); });
  });
}
