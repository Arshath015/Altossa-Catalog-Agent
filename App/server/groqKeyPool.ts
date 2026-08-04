/**
 * groqKeyPool.ts
 * ---------------
 * Manages failover across multiple Groq API keys (GROQ_API_KEY_1..3) so
 * one key's daily rate limit doesn't take the LLM-assisted matching path
 * down for the rest of the day. Tries keys in a FIXED priority order
 * (key 1 first, always -- not round-robin) and remembers each key's own
 * exhaustion state independently, using Groq's own "please try again in
 * Xs" retry hint so a key already known to be exhausted is skipped with
 * NO network call at all (confirmed important this session: even a
 * rejected 429 request still counts against that key's quota, so
 * skipping known-bad keys outright matters, not just for speed).
 *
 * SAFETY: no key value is ever logged, printed, returned to a client, or
 * written anywhere besides being read from process.env -- every log line
 * and every exported status shape uses a key's 1-based INDEX only.
 */

export interface GroqKeySlot {
  index: number; // 1-based, for logging only -- never the key itself
  apiKey: string;
  exhaustedUntil: number | null; // epoch ms; null = available now
}

const DEFAULT_COOLDOWN_MS = 60_000; // non-429 errors (network blip, bad response): retry fairly soon
const FALLBACK_429_COOLDOWN_MS = 5 * 60_000; // 429 with no parseable retry hint

function loadKeySlots(): GroqKeySlot[] {
  const slots: GroqKeySlot[] = [];
  for (let i = 1; i <= 3; i++) {
    const key = process.env[`GROQ_API_KEY_${i}`];
    if (key) slots.push({ index: i, apiKey: key, exhaustedUntil: null });
  }
  return slots;
}

// Module-level singleton so exhaustion state persists across requests for
// the life of the server process -- the whole point is remembering a key
// is down between calls, not re-discovering it every time.
const keySlots: GroqKeySlot[] = loadKeySlots();

export function hasAnyKeyConfigured(): boolean {
  return keySlots.length > 0;
}

export function configuredKeyCount(): number {
  return keySlots.length;
}

/** Parses Groq's "Please try again in 26m50.496s" (or just "50.4s") hint
 * out of a 429 error message, in milliseconds. Returns null if it can't
 * find/parse one, so the caller can fall back to a fixed cooldown. */
function parseRetryAfterMs(errorText: string): number | null {
  const m = errorText.match(/try again in\s+(?:(\d+)m)?(\d+(?:\.\d+)?)s/i);
  if (!m) return null;
  const minutes = m[1] ? parseInt(m[1], 10) : 0;
  const seconds = parseFloat(m[2]);
  return Math.round((minutes * 60 + seconds) * 1000);
}

/** Returns key slots in fixed priority order (1, 2, 3), skipping any
 * still inside their own known cooldown window. An empty result means
 * every configured key is currently believed exhausted -- the caller
 * should treat that exactly like a total LLM failure (no network call
 * needed to find out; we already know). */
export function getAvailableKeySlots(): GroqKeySlot[] {
  const now = Date.now();
  return keySlots
    .filter(s => s.exhaustedUntil === null || s.exhaustedUntil <= now)
    .sort((a, b) => a.index - b.index);
}

export function markKeyExhausted(slot: GroqKeySlot, errorText: string, isRateLimit: boolean): void {
  const parsed = isRateLimit ? parseRetryAfterMs(errorText) : null;
  const cooldown = parsed ?? (isRateLimit ? FALLBACK_429_COOLDOWN_MS : DEFAULT_COOLDOWN_MS);
  slot.exhaustedUntil = Date.now() + cooldown;
}

export function markKeyHealthy(slot: GroqKeySlot): void {
  slot.exhaustedUntil = null;
}

/** For diagnostics/monitoring only -- key INDEX and cooldown status,
 * never the key value itself. Safe to log or return in an API response. */
export function keyPoolStatus(): { index: number; available: boolean; cooldownRemainingMs: number }[] {
  const now = Date.now();
  return keySlots.map(s => ({
    index: s.index,
    available: s.exhaustedUntil === null || s.exhaustedUntil <= now,
    cooldownRemainingMs: s.exhaustedUntil && s.exhaustedUntil > now ? s.exhaustedUntil - now : 0,
  }));
}
