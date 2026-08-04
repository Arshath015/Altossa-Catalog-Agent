/**
 * llmIntent.ts
 * -------------
 * Uses Groq's API to interpret a user's message into structured intent:
 * which product, size, and fabric tier they're asking about. This is the
 * ONLY thing the LLM is trusted to do -- it never sees or states a price.
 *
 * SAFETY: the caller (catalogChatRoute.ts / catalogChat.ts) validates the
 * returned product name against the real catalog list before using it. If
 * the LLM is wrong, unsure, hallucinates a product, or the API call fails
 * for any reason, the system falls back to the tested deterministic
 * matcher -- it never silently trusts the model's output.
 *
 * KEY ROTATION: tries up to 3 Groq API keys (GROQ_API_KEY_1/2/3) in fixed
 * priority order via groqKeyPool.ts -- key 1 first, always, falling over
 * to key 2 then key 3 only when the one before it is rate-limited or
 * erroring. Each key's own exhaustion state is tracked independently and
 * remembered between calls, so a key already known to be exhausted is
 * skipped with NO network call (a rejected 429 still counts against that
 * key's own daily quota, so skipping known-bad keys outright matters).
 * extractIntent() only returns null -- triggering the app's degraded-mode
 * signal -- once every configured key has been tried (or is still in its
 * own cooldown window) and none worked; a single exhausted key fails over
 * invisibly to the caller.
 *
 * SETUP:
 *   1. npm install dotenv   (if not already installed)
 *   2. Create a file named ".env" in the project root (same folder as
 *      package.json) containing up to 3 lines:
 *        GROQ_API_KEY_1=your_first_key
 *        GROQ_API_KEY_2=your_second_key
 *        GROQ_API_KEY_3=your_third_key
 *      (1 key is enough to run; 2-3 add rate-limit failover.)
 *   3. Add ".env" to your .gitignore so no key ever gets committed.
 *   4. In your server entry point (App/server/index.ts), add this as the
 *      very first line: `import 'dotenv/config';`
 *
 * Never hardcode an API key in this file or any committed file -- keys
 * are read from process.env at runtime only, and never logged, printed,
 * or included in any response this app sends.
 */

import { getAvailableKeySlots, markKeyExhausted, markKeyHealthy, hasAnyKeyConfigured, GroqKeySlot } from './groqKeyPool';

export interface ChatTurn {
  role: 'user' | 'assistant';
  text: string;
}

export interface LlmIntent {
  product_name: string | null; // derived: product_names[0], for backward compatibility
  product_names: string[] | null;
  size: string | null;
  fabric_tier: string[] | null;
  wants_full_list: boolean;
  clarification_reply: string | null; // set when the LLM thinks it should just ask a natural follow-up question itself
}

const GROQ_URL = 'https://api.groq.com/openai/v1/chat/completions';
const MODEL = 'llama-3.3-70b-versatile';
const TIMEOUT_MS = 8000;

function buildSystemPrompt(productNames: string[], lastProduct: string | null): string {
  const anchorNote = lastProduct
    ? [
        '',
        `IMPORTANT CONTEXT: the product most recently confirmed in this conversation is "${lastProduct}". `,
        'If the user\'s new message is short, vague, or doesn\'t clearly name a different product ',
        '(e.g. "and in 180x200?", "what about extra fabric", "h.29"), assume they still mean ',
        `"${lastProduct}" unless they clearly name something else.`,
      ].join('\n')
    : '';

  return [
    'You are an intent-extraction assistant for a furniture catalog chat. ',
    'Your ONLY job is to read the conversation and figure out which product, ',
    'size, and fabric tier the person is asking about. You must NEVER state ',
    'a price yourself -- you do not have access to real prices, and making ',
    'one up would be a serious error.',
    '',
    'Respond with ONLY a JSON object (no other text) in exactly this shape:',
    '{"product_names": string[]|null, "size": string|null, "fabric_tier": string[]|null, "wants_full_list": boolean}',
    '',
    '- "product_names" should be an ARRAY containing EVERY distinct product the person ',
    '  is asking about, each copied EXACTLY (character for character) from this list of ',
    '  real products, or null if you cannot confidently match any: ',
    JSON.stringify(productNames),
    '  Usually this array has exactly one item. Only include more than one when the ',
    '  person clearly names multiple different products in the same message (e.g. ',
    '  "give me Pandora and Selene prices", "compare Ceylon and Awase", "show me Jack ',
    '  and Jack-e"). Do not split a single product\'s own name into multiple entries.',
    '- "size" should be in the form "WIDTHxDEPTH" (e.g. "160x200") if mentioned, else null. ',
    '  Some catalogs list sizes as WIDTHxDEPTHxHEIGHT (e.g. "240x120x74") -- if the person ',
    '  mentions all three numbers, include all three in "size" exactly as given rather than ',
    '  dropping the third; if they only give two, just use those two as normal.',
    '  IMPORTANT: a short code like "h.7", "h27", "sp.10", or "sp 4.5" is a MODEL/VARIANT ',
    '  identifier (which specific configuration, height option, or collection the person ',
    '  means), NOT a size measurement -- never put these in "size". Leave "size" null for ',
    '  these and let the product_names/context handle identifying the right variant instead.',
    '- "fabric_tier" should be an ARRAY of finish/material/upholstery category names if one ',
    '  or more are mentioned (e.g. ["Extra"], or ["Extra", "Plus"] if the user asks for both), ',
    '  else null. Different catalogs name these differently -- some use plain words like ',
    '  "Extra", "B e TCL", "Luxury leather", "Super"; others use alphanumeric finish/material ',
    '  codes like "GFM69", "GFM11 / GFM18", "KM02", "Cristallo trasparente". Extract whatever ',
    '  finish/material/upholstery identifier the person mentions, in EITHER style -- do not ',
    '  assume it must look like a plain English word.',
    '- "wants_full_list" should be true whenever the person is asking to see the WHOLE ',
    '  set of prices/options for a product, in any phrasing -- not just the literal words ',
    '  "all prices". This includes things like "give all", "show me everything", "the ',
    '  complete list", "give all prices", "what are all the options", "show me the full ',
    '  breakdown", "give me the whole price sheet", or a bare "give all" with no other ',
    '  detail. Judge the person\'s actual intent, not specific keywords. Set it to false ',
    '  if they\'re asking about one specific size/tier combination instead.',
    '- Use the conversation history to resolve follow-ups, e.g. if the user ',
    '  previously asked about "Ceylon" and now says "what about 180x200?", ',
    '  product_names should still be ["Ceylon"].',
    '- If you are not confident which product is meant, set product_names to null ',
    '  rather than guessing -- a wrong guess is worse than admitting uncertainty.',
    anchorNote,
  ].join('\n');
}

function parseLlmIntent(content: string): LlmIntent | null {
  const parsed = JSON.parse(content);
  const productNamesArr: string[] | null = Array.isArray(parsed.product_names)
    ? parsed.product_names.filter((p: unknown) => typeof p === 'string')
    : (typeof parsed.product_names === 'string' ? [parsed.product_names] : null);
  return {
    product_names: productNamesArr && productNamesArr.length > 0 ? productNamesArr : null,
    product_name: productNamesArr && productNamesArr.length > 0 ? productNamesArr[0] : null,
    size: typeof parsed.size === 'string' ? parsed.size : null,
    fabric_tier: Array.isArray(parsed.fabric_tier)
      ? parsed.fabric_tier.filter((t: unknown) => typeof t === 'string')
      : (typeof parsed.fabric_tier === 'string' ? [parsed.fabric_tier] : null),
    wants_full_list: parsed.wants_full_list === true,
    clarification_reply: null,
  };
}

type GroqCallResult =
  | { ok: true; intent: LlmIntent | null }
  | { ok: false; isRateLimit: boolean; errorText: string };

/** One attempt against Groq with ONE specific key. Never logs the key
 * itself -- only the caller (extractIntent) logs anything, and only a
 * key INDEX, never this function's `apiKey` parameter. */
async function callGroqOnce(apiKey: string, messages: unknown[]): Promise<GroqCallResult> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(GROQ_URL, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${apiKey}`,
      },
      body: JSON.stringify({
        model: MODEL,
        messages,
        response_format: { type: 'json_object' },
        temperature: 0,
        max_tokens: 200,
      }),
      signal: controller.signal,
    });

    if (!res.ok) {
      const errorText = await res.text();
      return { ok: false, isRateLimit: res.status === 429, errorText };
    }

    const data = await res.json();
    const content: string | undefined = data?.choices?.[0]?.message?.content;
    if (!content) return { ok: true, intent: null };
    return { ok: true, intent: parseLlmIntent(content) };
  } catch (err) {
    return { ok: false, isRateLimit: false, errorText: err instanceof Error ? err.message : String(err) };
  } finally {
    clearTimeout(timeout);
  }
}

/**
 * Calls Groq to interpret the user's message, trying each configured API
 * key in priority order (key 1 first) until one succeeds. Returns null
 * (not a thrown error) only once EVERY configured key has failed or is
 * still in its own cooldown from a previous failure -- missing keys,
 * network issues, timeouts, bad JSON, and rate limits are all handled
 * the same way here, so the caller can cleanly fall back to
 * deterministic matching without special-casing any of them.
 */
export async function extractIntent(
  message: string,
  history: ChatTurn[],
  productNames: string[],
  lastProduct: string | null = null
): Promise<LlmIntent | null> {
  if (!hasAnyKeyConfigured()) {
    console.warn('[llmIntent] No GROQ_API_KEY_1/2/3 set -- skipping LLM step, using deterministic matching only.');
    return null;
  }

  const availableSlots: GroqKeySlot[] = getAvailableKeySlots();
  if (availableSlots.length === 0) {
    console.warn('[llmIntent] All configured Groq keys are currently cooling down from a prior rate limit -- skipping LLM step, using deterministic matching only.');
    return null;
  }

  const messages = [
    { role: 'system', content: buildSystemPrompt(productNames, lastProduct) },
    ...history.slice(-6).map(h => ({ role: h.role, content: h.text })),
    { role: 'user', content: message },
  ];

  for (const slot of availableSlots) {
    const result = await callGroqOnce(slot.apiKey, messages);
    if (result.ok) {
      markKeyHealthy(slot);
      return result.intent;
    }
    markKeyExhausted(slot, result.errorText, result.isRateLimit);
    console.warn(`[llmIntent] Groq key ${slot.index} ${result.isRateLimit ? 'rate-limited' : 'failed'}, ${slot === availableSlots[availableSlots.length - 1] ? 'no more keys to try' : 'failing over to next key'}.`);
  }

  // Every available key failed on this call.
  return null;
}
