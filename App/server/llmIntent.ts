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
 * SETUP:
 *   1. npm install dotenv   (if not already installed)
 *   2. Create a file named ".env" in the project root (same folder as
 *      package.json) containing exactly one line:
 *        GROQ_API_KEY=your_actual_key_here
 *   3. Add ".env" to your .gitignore so the key never gets committed.
 *   4. In your server entry point (App/server/index.ts), add this as the
 *      very first line: `import 'dotenv/config';`
 *
 * Never hardcode the API key in this file or any committed file --
 * it's read from process.env.GROQ_API_KEY at runtime only.
 */

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

/**
 * Calls Groq to interpret the user's message. Returns null (not a thrown
 * error) on ANY failure -- missing API key, network issue, timeout, bad
 * JSON, etc -- so the caller can cleanly fall back to deterministic
 * matching without special-casing every possible failure mode.
 */
export async function extractIntent(
  message: string,
  history: ChatTurn[],
  productNames: string[],
  lastProduct: string | null = null
): Promise<LlmIntent | null> {
  const apiKey = process.env.GROQ_API_KEY;
  if (!apiKey) {
    console.warn('[llmIntent] GROQ_API_KEY not set -- skipping LLM step, using deterministic matching only.');
    return null;
  }

  const messages = [
    { role: 'system', content: buildSystemPrompt(productNames, lastProduct) },
    ...history.slice(-6).map(h => ({ role: h.role, content: h.text })),
    { role: 'user', content: message },
  ];

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
      console.error(`[llmIntent] Groq API returned ${res.status}: ${await res.text()}`);
      return null;
    }

    const data = await res.json();
    const content: string | undefined = data?.choices?.[0]?.message?.content;
    if (!content) return null;

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
  } catch (err) {
    console.error('[llmIntent] Failed to get/parse Groq response, falling back to deterministic matching:', err);
    return null;
  } finally {
    clearTimeout(timeout);
  }
}