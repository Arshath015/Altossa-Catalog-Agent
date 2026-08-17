/**
 * catalogChat.ts
 * ---------------
 * Deterministic catalog lookup: given a free-text query, figures out which
 * product/size/fabric-tier is being asked about using string matching only
 * (no LLM), then reads the real price straight out of prices.json.
 *
 * WHY NO LLM HERE: the one hard requirement from the client is "must not
 * give false data." An LLM can be used elsewhere (e.g. to phrase the reply
 * naturally), but the actual number shown to a customer must come from
 * code that reads the verified JSON directly -- never from a model
 * generating text that "sounds like" a price.
 *
 * This file's logic was tested against the real, fully-verified Bolzan
 * dataset before being typed -- see the accompanying conversation/notes
 * for the specific queries it was checked against.
 */

import fs from 'fs';
import path from 'path';

export interface CatalogEntry {
  brand: string;
  product_name: string;
  slug: string;
  // Only present in Varaschini's catalog_index.json (its 39 named
  // collections + 16 back-matter reference sections) -- the other 3
  // brands' single flat per-product catalogs have no equivalent grouping
  // concept, so this stays optional/undefined for them.
  collection?: string;
  printed_page_start: number;
  printed_page_end: number;
  pdf_page_start: number;
  pdf_page_end: number;
  mini_pdf: string;
  images: string[];
  page_images?: Record<string, string>;
  text_file: string;
}

export interface PriceRow {
  brand: string;
  product_name: string;
  model_variant: string | null;
  variant_context: string | null;
  size: string | null;
  fabric_tier: string | null;
  /** The REAL source-PDF category word this row's fabric_tier value came
   * from -- e.g. "Base" (a table's steel base finish), "Top" (a table's
   * crystal/marble top material), "Rivestimento"/"Seduta" (upholstery),
   * "Struttura" (frame). Null when no such word was confidently
   * recoverable, or when the product genuinely has no fabric_tier
   * dimension at all (fabric_tier is also null in that case) -- the UI
   * falls back to "FABRIC" only when this is null, since that's still
   * accurate for Bolzan (exclusively upholstered furniture) and for
   * Cattelan's own upholstered items. Never affects price/tier/size
   * data itself -- purely a display label. */
  tier_label: string | null;
  code: string | null;
  price_eur: string;
  ambiguous: boolean;
  source_pdf_page?: number | null;
}

export type ChatStatus =
  | 'ok'
  | 'multiple_options'
  | 'full_price_grid'
  | 'multi_product'
  | 'ambiguous_price'
  | 'no_matching_variant'
  | 'no_price_data'
  | 'no_product_match'
  | 'clarify_product';

export interface ChatResult {
  status: ChatStatus;
  message: string;
  product_name?: string;
  candidates?: string[];
  matches?: PriceRow[];
  image_urls?: string[];
  /** True whenever this reply was produced without a working LLM call (the
   * request failed, timed out, or no API key is configured) -- set by the
   * route layer, not here, since only it knows whether the LLM step ran.
   * Lets the UI show a short "reduced capability" note instead of silently
   * degrading the user's experience with no signal at all. */
  degraded?: boolean;
}

// Known fabric-tier category names used across Bolzan's catalog (same
// whitelist used by the price-extraction script, so tier names in queries
// match what's actually in the data). Extend this list if other brands
// use different tier vocabulary.
const KNOWN_TIERS = [
  'nuvola leather', 'luxury leather', 'super nuvola fabric', 'extra luxury fabric',
  'b e tcl', 'super', 'extra', 'plus', 'd, e', 'c', 'd', 'e',
];

/** Extracts a short "prefix.number" distinguishing code from text -- e.g.
 * "h.7", "h7" -> "h:7"; "sp.10", "sp 10" -> "sp:10"; "sp.4.5" -> "sp:4.5".
 * These short codes (height variants, panel thickness/"spessore", etc.)
 * are the most common way products in this catalog distinguish between
 * otherwise near-identical variant names, but they're too short to
 * survive generic word-length filtering (a bare "10" or "h" alone would
 * be noise) and too short to appear reliably in a "whole phrase" compact
 * match. Extracting them explicitly, with their prefix, avoids false
 * collisions between unrelated codes that happen to share just a number
 * (e.g. "h.10" is NOT the same signal as "sp.10").
 */
function extractShortCode(text: string): string | null {
  const m = text.match(/\b(h|sp)\.?\s*(\d+(?:\.\d+)?)\b/i);
  return m ? `${m[1].toLowerCase()}:${m[2]}` : null;
}

export function normalize(s: string | null | undefined): string {
  return (s || '')
    .toLowerCase()
    // PDF-extracted text renders "fi"/"fl" as single ligature glyphs
    // (U+FB01/FB02) rather than two letters -- Unicode NFD does NOT
    // decompose these (they're presentation forms, not accent+base pairs),
    // so without this they silently break word-boundary matching on words
    // like "fissaggio" (confirmed: SPINNAKER's "X fissaggio a muro").
    .replace(/\ufb01/g, 'fi')
    .replace(/\ufb02/g, 'fl')
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .trim();
}

/** Formats a whole number with '.' as the thousands separator, matching
 * this catalog's Italian number format (e.g. 1884 -> "1.884"). Deliberately
 * NOT using toLocaleString('it-IT') -- verified that silently fails to
 * apply the separator on at least one real Node build (likely missing
 * full ICU data), which would be a silent, hard-to-spot bug rather than
 * a visible error. This manual approach has no such dependency. */
function formatItalianNumber(n: number): string {
  return Math.round(n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, '.');
}

/** Adds a base structure price and an addon surcharge together, both in
 * this catalog's Italian-style thousands-separator format (e.g. "1.290"
 * and "+158" -> "1.448"). Assumes whole-euro amounts, since that's the
 * only format seen anywhere in this catalog. */
export function combineAddon(basePrice: string, addonPrice: string): string {
  const toInt = (s: string) => parseInt(s.replace(/[^\d]/g, ''), 10) || 0;
  const total = toInt(basePrice) + toInt(addonPrice);
  return formatItalianNumber(total);
}

function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** Does `haystack` contain `needle` as a whole word/phrase (real boundaries), not just any substring? */
function containsWholeWord(haystack: string, needle: string): boolean {
  if (!needle) return false;
  const re = new RegExp(`(?:^|\\W)${escapeRegex(needle)}(?:$|\\W)`, 'i');
  return re.test(haystack);
}

// Generic furniture-category words that recur across many UNRELATED
// Bonaldo products as a literal part of their English names -- e.g.
// "Avant-Garde chair" and "Colibrì chair" are completely different
// product families that just happen to both be chairs. Confirmed via a
// full-catalog word-frequency scan: each of these appears in 4+
// otherwise-unrelated product names. A shared match on one of these
// ALONE is too weak a signal to treat as a real candidate -- it's what
// let a fully made-up query like "how much is the Vexalon armchair"
// surface 4 unrelated real armchairs instead of correctly reporting no
// match (Cattelan/Bolzan don't have this problem: neither uses generic
// English category words as part of real product names the way Bonaldo
// does). Not claimed exhaustive -- extend if a new collision turns up,
// same as every other curated word list in this file.
const GENERIC_CATEGORY_WORDS = new Set([
  'table', 'chair', 'armchair', 'console', 'office', 'bench', 'pouf',
  'stand', 'mirror', 'lounge', 'coffee', 'wood', 'tv', 'sofa', 'bed',
]);

// Ordinary conversational request/question words -- stripped before
// checkFamilyAmbiguity's containment check so a natural-language phrasing
// ("what are the prices for X") is scored the same as a terse one ("X
// price"). Deliberately small and generic (not brand-specific vocabulary),
// same precedent as GENERIC_CATEGORY_WORDS/RISKY_SIZE_CODE_WORDS above.
const CONVERSATIONAL_FILLER_WORDS = new Set([
  'what', 'whats', 'are', 'is', 'the', 'a', 'an', 'for', 'of', 'me', 'give',
  'show', 'tell', 'please', 'how', 'much', 'do', 'you', 'have', 'can', 'i',
  'get', 'price', 'prices', 'pricing', 'cost', 'costs', 'all', 'full',
  'complete', 'every', 'list', 'in', 'about', 'and', 'then',
]);

/** Splits into tokens on any non-alphanumeric character (not just
 * whitespace) -- unlike a plain `.split(/\s+/)`, this treats "Big/Big"
 * (typed with no space) and "Big / Big" (a catalog name, spaced) as the
 * SAME two tokens. Needed by checkFamilyAmbiguity below; same root-cause
 * fix shape as the tier-filter punctuation-tokenization bug fixed
 * elsewhere in this file (a whitespace-only split lets stray punctuation
 * silently break an otherwise-correct token comparison). */
function tokenizeLoose(s: string): string[] {
  return normalize(s).split(/[^a-z0-9]+/).filter(Boolean);
}

/** Length of the longest common leading-token sequence between two
 * already-tokenized names (order matters, unlike the token-SET
 * containment used elsewhere in this file -- see checkFamilyAmbiguity for
 * why an ordered prefix match is the right relation for THIS check). */
function commonPrefixLen(a: string[], b: string[]): number {
  let i = 0;
  while (i < a.length && i < b.length && a[i] === b[i]) i++;
  return i;
}

/** Simple token-overlap similarity score between a query and a candidate name. Higher = better match. */
export function similarity(query: string, candidate: string): number {
  const q = normalize(query);
  const c = normalize(candidate);
  if (q === c) return 100;
  // Strip PARENTHESES specifically (not a full tokenizeLoose split) before
  // the whitespace split, so a tight "(Category)" disambiguation suffix
  // (e.g. Ditre's "Ada (Sofa)", 28% of its catalog) doesn't hide "sofa" as
  // a different token than a bare "sofa" query word -- every real "(Sofa)"-
  // suffixed product was invisible to a bare-category-word query, while
  // the only candidates that COULD match were unrelated products using
  // "sofa"/"bed" unparenthesized in their own name (Isabel/Kanaha 2.0/
  // Kanaha mix 2.0/Lulu' 2.0/Sanders "sofa bed"), which then won by
  // default. Deliberately NOT reusing tokenizeLoose's full non-alnum split
  // here (tried first, reverted): it also splits HYPHENATED compound
  // words that are semantically atomic for product-name matching --
  // "Ker-Wood" -> "ker"+"wood" (silently exposing "wood", already a
  // GENERIC_CATEGORY_WORD, as separate signal and creating new false
  // ambiguity across all 7 "*Ker-Wood*" Cattelan siblings) and "Jack-e"
  // -> "jack"+"e" (reintroducing the exact single-letter-tier collision
  // class already fixed elsewhere in this file for "Bend-e Fabric"/tier
  // "E") -- confirmed by a full regression:full run that surfaced 36 new
  // row-count mismatches across 4 brands before this was narrowed down to
  // parens only.
  const qTokens = q.replace(/[()]/g, '').split(/\s+/).filter(Boolean);
  const cTokens = c.replace(/[()]/g, '').split(/\s+/).filter(Boolean);
  // Token-SET containment (every token on one side appears on the other,
  // any order) -- not a contiguous-phrase check. That distinction matters:
  // "cuff pouf" isn't a substring of "Cuff bench and pouf" (words in
  // between), so a phrase-only check let it fall to the diluted overlap
  // score below while bare "Cuff" kept a flat win. Tying both at 80 instead
  // hands resolution to the existing "maximal" tie-break in answer().
  if (qTokens.length > 0 && qTokens.every(t => cTokens.includes(t))) return 80;
  if (cTokens.length > 0 && cTokens.every(t => qTokens.includes(t))) return 80;
  // Concatenation match: a multi-word candidate name might get typed as
  // ONE run-together word missing the space (or vice versa) -- "online"
  // never token-matches "On Line" (tokens ["on","line"]) under the checks
  // above, since neither side's whitespace-split tokens ever equal the
  // other's. Joins the MULTI-token side into one compact string and looks
  // for it as a whole bounded token in the other (containsWholeWord, not
  // a raw substring test) -- gated to cTokens/qTokens.length > 1 so a
  // SHORT single-word candidate (e.g. "Cop") never risks matching as a
  // coincidental substring inside an unrelated longer word; a multi-word
  // phrase's joined form is long/specific enough that an accidental whole-
  // token collision elsewhere in the catalog is far less likely, same
  // confidence tier as the token-SET containment checks just above.
  if (cTokens.length > 1 && containsWholeWord(q, cTokens.join(''))) return 80;
  if (qTokens.length > 1 && containsWholeWord(c, qTokens.join(''))) return 80;
  const overlap = qTokens.filter(t => cTokens.includes(t));
  if (overlap.length === 0) return 0;
  // If every shared word is a generic category word, AND the query has
  // some OTHER word that's neither generic nor ordinary filler (reusing
  // RISKY_SIZE_CODE_WORDS below -- "price", "give", "how", "much", etc.)
  // and doesn't appear in this candidate at all, that leftover word is
  // real signal the person meant something specific this candidate isn't
  // -- don't count the shared generic word alone as a match. A bare
  // "chair" (nothing left over) or "chair price" ("price" is ordinary
  // filler) still match as before -- this only suppresses the case where
  // the query looks like it's naming something specific that isn't here.
  // Same treatment for a bare NUMBER as for a generic category word above:
  // a shared digit-only token carries no real product-identity signal on
  // its own (a size, a code fragment, a page number -- anything), so
  // scoring it the same as a genuine name-word overlap lets two totally
  // unrelated products tie purely by coincidence. Confirmed real: "give
  // all Composizione dimension 80 price" tied "Composizione Tavoli 13610"
  // (real overlap: "composizione") together with "Emma Sofa | 3 seats 80"
  // (the ONLY overlap is the bare "80", coincidentally Emma Sofa's own
  // seat-width suffix) -- both scored ~10-12 via the diluted-overlap
  // formula below, landing in the same tied candidate set with no
  // relation to each other at all. A query that's JUST a number ("140",
  // nothing else) still matches normally -- `leftover` is empty in that
  // case, same safety valve as the generic-word check.
  //
  // Same treatment again for a shared CONVERSATIONAL FILLER word ("for",
  // "the", "please", ...) -- confirmed real: "give me the price for
  // Zorblatt XQ9000" (a nonexistent product) matched "Cuscini e Tessuti
  // Zavorra per cuscino 2kg - 2 kg Ballast for cushion" (and 3 other
  // unrelated products) purely because both happen to contain the word
  // "for" -- CONVERSATIONAL_FILLER_WORDS/RISKY_SIZE_CODE_WORDS already
  // exclude filler from the LEFTOVER check just below, but the overlap-
  // classification check above it never consulted either list, so a
  // shared filler word was scored exactly like a real name-word overlap.
  // Reuses both existing, already-audited lists (each word individually
  // checked against real size-code/product-name collisions when added)
  // rather than a new one -- CONVERSATIONAL_FILLER_WORDS adds a few
  // request-phrasing words (what, are, you, show, tell, pricing, cost,
  // complete, every, ...) RISKY_SIZE_CODE_WORDS doesn't have.
  if (overlap.every(t => GENERIC_CATEGORY_WORDS.has(t) || /^\d+$/.test(t) || RISKY_SIZE_CODE_WORDS.has(t) || CONVERSATIONAL_FILLER_WORDS.has(t))) {
    const leftover = qTokens.filter(t => !overlap.includes(t) && !cTokens.includes(t) && !RISKY_SIZE_CODE_WORDS.has(t) && !CONVERSATIONAL_FILLER_WORDS.has(t));
    if (leftover.length > 0) return 0;
  }
  return (overlap.length / Math.max(qTokens.length, cTokens.length)) * 60;
}

/** Extract a size like "160x200" (WIDTHxDEPTH) or "240x120x74" (WIDTHxDEPTHxHEIGHT,
 * used by catalogs like Cattelan whose MISURA CM values include height) from
 * free text, in whatever format the user typed it. The third number is only
 * captured when present -- a plain two-number query still returns exactly
 * "WIDTHxDEPTH", unchanged from before. */
export function extractSize(query: string): string | null {
  const m = query.match(/(\d{2,3})\s*[x×]\s*(\d{2,3})(?:\s*[x×]\s*(\d{2,3}))?/i);
  if (!m) return null;
  return m[3] ? `${m[1]}x${m[2]}x${m[3]}` : `${m[1]}x${m[2]}`;
}

/** Removes an already-normalized `name` from an already-normalized query,
 * both as a literal substring AND (if the literal doesn't appear) as its
 * longest leading word-prefix that does. The prefix pass matters because a
 * short query typically only names the FRONT part of a collection/product
 * (e.g. "Cuscini e Tessuti 2701 price"), while a real catalog product_name
 * can be much longer and never appear verbatim in it (Varaschini's Cuscini
 * e Tessuti entries are garbled multi-dimension concatenations) -- without
 * it, a collection's own name that happens to contain a real tier word as
 * a standalone word is never excluded at all. Confirmed real: "Cuscini e
 * Tessuti" contains the Italian conjunction "e" ("and"), which is ALSO
 * Varaschini's bare tier code "E", silently narrowing a 5-tier answer down
 * to 1 in BOTH extractTiers and lookupForProduct's own raw-tier-scan
 * safety net below (two independent tier-matching code paths, same root
 * cause, needed fixing in both). Requires at least 2 shared words for the
 * prefix pass (not 1) to avoid a weak, coincidental single-word strip
 * removing unrelated context. */
function stripNameFromQuery(normalizedQuery: string, normalizedName: string): string {
  if (!normalizedName) return normalizedQuery;
  let q = normalizedQuery.replace(new RegExp(escapeRegex(normalizedName), 'gi'), ' ');
  const nameWords = normalizedName.split(/\s+/).filter(Boolean);
  for (let len = nameWords.length; len >= 2; len--) {
    const prefix = nameWords.slice(0, len).join(' ');
    if (q.includes(prefix)) {
      q = q.replace(new RegExp(escapeRegex(prefix), 'gi'), ' ');
      break;
    }
  }
  return q;
}

/** Extract a known fabric tier mentioned in free text -- as a real whole
 * word/phrase, never a raw substring (otherwise single-letter tiers like
 * 'e' would false-match inside ordinary words like "bed"). */
export function extractTiers(query: string, excludeNames: string[] = []): string[] {
  // Strip any known product name(s) out of the query FIRST -- several of
  // this catalog's own product names contain a hyphenated single-letter
  // segment that collides with a real bare tier code (confirmed: Bolzan's
  // "Bend-e Fabric" -- the "-e" is part of the product's own name, but
  // containsWholeWord treats "-" as a word boundary, so it silently
  // matched tier "E" and narrowed 36 rows down to 6 on a plain "give me
  // all prices for Bend-e Fabric"). Same root cause and same fix pattern
  // as the raw-tier-scan stripping already done in lookupForProduct, just
  // needed here too since this function runs earlier, on the unstripped
  // query, at every one of its call sites in answer().
  let q = normalize(query);
  for (const name of excludeNames) {
    q = stripNameFromQuery(q, normalize(name));
  }
  const found: string[] = [];
  for (const tier of KNOWN_TIERS) {
    if (containsWholeWord(q, tier) && !found.includes(tier)) found.push(tier);
  }
  return found;
}

// Pure unit/separator tokens that appear in almost every size string in
// this catalog and never distinguish one row from another on their own
// (the height suffix "h", the dimension separator "x"/"×", the diameter
// mark "ø", metric units). Stripped out when extracting a size's own
// meaningful code/qualifier words (below).
const SIZE_UNIT_TOKENS = new Set(['h', 'mt', 'mq', 'cm', 'ø']);
// Common short English words that can double as a real size-code token --
// "a" was the original known risk ("give me A price for..."), but a
// systematic scan of both brands' real size codes found the SAME
// collision at 2-4 letters too: PASCAL's "ME 255x252x114h" (code "me")
// silently narrowed 30 clean rows down to 2 on "give me all prices for
// PASCAL" -- the word "me" in the query's own phrasing, nothing to do
// with the product, coincidentally matched a real size-code prefix.
// KATANA's "UP" variants, SPINNAKER's "so" (from "soffitto"), Bolzan's
// Pouf Ares/Edith "to" (from "Riv.to") are the same pattern. A bare
// occurrence of any of these is only trusted as a size signal when it's
// clearly positional (right after the product name or a signal word like
// "size"/"style"), never just because it appears anywhere in the text.
const RISKY_SIZE_CODE_WORDS = new Set([
  'a', 'i', 'o', 'u',
  'me', 'my', 'is', 'am', 'be', 'we', 'us', 'or', 'if', 'in', 'on', 'at',
  'to', 'so', 'no', 'go', 'do', 'up', 'it', 'as', 'an', 'the', 'and',
  'not', 'but', 'out', 'get', 'has', 'had', 'was', 'who', 'how', 'why',
  'him', 'her', 'his', 'its', 'our', 'let', 'put', 'via', 'she', 'may',
  'say', 'too', 'own', 'new', 'use', 'way', 'now', 'old', 'see', 'one',
  'two', 'big', 'top', 'ask', 'try', 'any', 'add', 'day', 'need', 'come',
  'long', 'than', 'more', 'much', 'like', 'just', 'also', 'this', 'that',
  'with', 'from', 'have', 'will', 'been', 'were', 'then', 'when', 'all',
  'for', 'give', 'price', 'prices',
  // Confirmation/politeness words -- added for the "give all"/"yes, give
  // all" follow-up fallback in answer() below, which reuses this same
  // list to detect a content-free continuation message. Checked against
  // every real size value in all 3 brands first -- no collisions found,
  // unlike the words above this comment (which were each added because a
  // real collision was found).
  'yes', 'yeah', 'yep', 'ok', 'okay', 'sure', 'please',
  // Found via the Gap 5 fuzzy-shortlist scope scan: "full" (distance 1
  // from real product "Bull") and "list" (distance 1 from "Lift" inside
  // "AVIATOR Keramik Lift") were the only 2 remaining trigger words after
  // the scaled-distance fix, out of 14 realistic follow-up phrasings
  // tested across all 3 brands. Also checked for real size-value
  // collisions first -- none found.
  'full', 'list',
  // Added for the Issue 3 Phase 2 "give all"/"all of them" auto-resolve
  // distinction -- "all of them" wasn't previously recognized as
  // content-free at all (neither "of" nor "them" was in this list),
  // so it fell straight to the flat no-match message instead of ever
  // reaching the lastCandidates auto-resolve logic. Checked for real
  // size/model-variant/code collisions across all 4 brands first, same
  // precedent as every other addition here -- none found.
  'of', 'them',
]);

/** Pull the meaningful non-numeric "code"/"qualifier" words out of a real
 * size string -- e.g. "A 200x100x73h" -> ["a"], "240x120x74h sag" ->
 * ["sag"], "X fissaggio a muro" -> ["x","fissaggio","a","muro"], "LA" ->
 * ["la"]. This is what lets a query like "melody size c" or "peyote a
 * cristallo..." select a specific row even when the catalog's own size
 * codes are letters/words rather than (or in addition to) dimensions --
 * confirmed needed for 72 products in this catalog whose size values
 * include at least one purely non-numeric code, plus 104 more where two
 * different size labels share IDENTICAL dimensions and only a leading
 * letter or trailing shape qualifier (e.g. "sag", "biscuit", "oval")
 * tells them apart. */
function extractSizeTokens(sizeStr: string): string[] {
  const chunks = sizeStr.match(/[A-Za-zÀ-öø-ÿ]+|\d+/g) || [];
  const isDigitChunk = (c: string | undefined) => !!c && /^\d+$/.test(c);
  const out: string[] = [];
  for (let i = 0; i < chunks.length; i++) {
    if (isDigitChunk(chunks[i])) continue;
    const c = normalize(chunks[i]);
    if (!c) continue;
    // "x"/"×" is only noise when it's acting as the dimension separator
    // BETWEEN two numbers (its normal role in "200x100x73h") -- keep it
    // when it stands alone as a real leading code with no numeric
    // neighbor on both sides (SPINNAKER's "X fissaggio a muro").
    if ((c === 'x' || c === '×') && isDigitChunk(chunks[i - 1]) && isDigitChunk(chunks[i + 1])) continue;
    if (SIZE_UNIT_TOKENS.has(c)) continue;
    out.push(c);
  }
  return out;
}

/** Find every word in the raw query that's safe to treat as a candidate
 * size-code/qualifier signal (to be intersected against extractSizeTokens
 * of each candidate row, by the caller). Two ways a word qualifies:
 *   1. It's not a common English word (not in RISKY_SIZE_CODE_WORDS) --
 *      low collision risk, so it's trusted anywhere in the query (e.g.
 *      "sag", "biscuit", "la").
 *   2. It's the single word immediately after the product's own name, or
 *      immediately after a signal word ("size"/"style"/"version"/
 *      "option"/"model"/"code") -- trusted even if it IS a common word
 *      that also happens to be a real code ("me", "up", "so", ...), since
 *      that position is specifically how people actually say a size code
 *      ("peyote a", "richard b", "melody size c") rather than using the
 *      word in its ordinary English sense.
 */
function findSizeSignalTokens(rawQuery: string, productName: string): Set<string> {
  const q = normalize(rawQuery);
  const signals = new Set<string>();
  // Every query naming this product contains the product's own name as a
  // word, trivially -- that's not a size preference, just which product
  // was asked about, so it must never count as a signal on its own.
  // Confirmed needed: ARENA's own size labels are "ARENA BOND"/"ARENA
  // DOUBLE BOND", which reuse the word "arena" -- so a plain "give me all
  // prices for ARENA" was silently narrowing out the third, differently-
  // labeled size ("ø120x29h") just because the query happened to contain
  // the word "arena" (to name the product at all), which coincidentally
  // matched those two size labels' own leading word.
  const productNameTokens = new Set(normalize(productName).split(/[^a-z0-9]+/).filter(Boolean));

  for (const tok of q.split(/[^a-z0-9]+/)) {
    if (tok.length >= 2 && !RISKY_SIZE_CODE_WORDS.has(tok) && !productNameTokens.has(tok)) signals.add(tok);
  }

  const signalWordRe = /\b(?:size|style|version|option|model|code)\s+([a-z0-9]+)/gi;
  let m: RegExpExecArray | null;
  while ((m = signalWordRe.exec(q))) signals.add(m[1].toLowerCase());

  const pName = normalize(productName);
  if (pName) {
    const afterNameMatch = q.match(new RegExp(`${escapeRegex(pName)}\\s+([a-z0-9]+)`, 'i'));
    if (afterNameMatch) signals.add(afterNameMatch[1].toLowerCase());
  }

  return signals;
}

export class CatalogChat {
  private catalogIndex: CatalogEntry[];
  private prices: PriceRow[];
  private productNames: string[];
  /** Every distinct real fabric_tier VALUE across this brand's whole
   * catalog (not just one product), longest-first -- used to strip a
   * recognized tier phrase out of a query BEFORE fuzzy product-name
   * matching runs (see buildLlmShortlist), so a word that's really just
   * part of a real tier phrase (e.g. "glove" in "Pelle Glove") never gets
   * evaluated as a standalone typo candidate against the whole catalog. */
  private realTierPhrases: string[];
  /** Every distinct real product CODE (price-row `code`, e.g. "13611X",
   * "P275", "RFL") mapped to the product name(s) that use it -- 2+ when a
   * code is genuinely shared across different products. Built from BOTH
   * `this.prices`' own `code` field (works across every brand that has
   * one -- Bolzan/Bonaldo/Varaschini, not Cattelan, which has no Codice
   * concept at all) AND catalog_index's own `art_code` field (Varaschini
   * only, but critically covers products with NO price row at all -- a
   * product the price parser never successfully extracted, like an
   * unpriced "materials-grid" item, would otherwise be entirely invisible
   * to code lookup even though its code is real and its catalog_index
   * entry exists). Built once here rather than scanning on every query.
   * See findProductsByCode below for why this exists at all: no other
   * matching path in this file ever consulted a raw product CODE, only
   * product NAMES -- a query naming just a code (no product name text at
   * all) had no way to resolve, even when that code unambiguously
   * identifies one real product. */
  private codeToProductNames: Map<string, string[]>;
  /** product_name -> its real catalog_index `collection` value, ONLY for
   * brands that have one (Varaschini). Ground truth for grouping tied
   * candidates that genuinely belong together vs. two unrelated
   * collections that only coincidentally share a leading word -- see
   * answer()'s wantsFullList majority-group logic for why a naive
   * shared-first-token heuristic isn't safe on its own (confirmed real:
   * "Big / Big Light" and "Big In&Out" both tokenize to "big" first, but
   * are two completely different collections). Empty for brands with no
   * `collection` field at all. */
  private productNameToCollection: Map<string, string>;

  /** @param dataDir folder containing catalog_index.json and prices.json for one brand */
  constructor(private dataDir: string) {
    this.catalogIndex = JSON.parse(fs.readFileSync(path.join(dataDir, 'catalog_index.json'), 'utf-8'));
    this.prices = JSON.parse(fs.readFileSync(path.join(dataDir, 'prices.json'), 'utf-8'));
    this.productNames = [...new Set(this.catalogIndex.map(p => p.product_name))];
    this.realTierPhrases = [...new Set(this.prices.map(r => r.fabric_tier).filter((t): t is string => !!t))]
      .sort((a, b) => normalize(b).length - normalize(a).length);
    this.productNameToCollection = new Map();
    for (const e of this.catalogIndex) {
      if (e.collection) this.productNameToCollection.set(e.product_name, e.collection);
    }
    this.codeToProductNames = new Map();
    const addCode = (rawCode: string | undefined, productName: string) => {
      if (!rawCode) return;
      const key = normalize(rawCode);
      if (!key) return;
      const names = this.codeToProductNames.get(key) ?? [];
      if (!names.includes(productName)) names.push(productName);
      this.codeToProductNames.set(key, names);
    };
    for (const r of this.prices) addCode(r.code, r.product_name);
    for (const e of this.catalogIndex) addCode(e.art_code, e.product_name);
  }

  /** Scans the query for any WHOLE token that exactly matches a real
   * product code, returning the distinct product name(s) that code
   * belongs to -- empty if no token in the query is a real code at all.
   * Uses the punctuation-stripping tokenizer (not a plain word-boundary
   * regex) for the same reason the tier-filter and family-ambiguity fixes
   * needed it: a code glued to adjacent punctuation in the query text
   * must still match.
   *
   * When a code is shared by 2+ different products -- confirmed real and
   * common, not a rare edge case: 98 art_codes are reused across
   * DIFFERENT Varaschini collections alone, e.g. "13610" is both
   * "Composizione Tavoli 13610" (a real, priced, unrelated product) AND
   * "Big / Big Light A B White" (the actual product meant by "Big Big
   * Light 13610 price" -- unpriced, so invisible to a price-row-only code
   * index, which is why catalog_index's art_code is merged in above too)
   * -- prefer whichever candidate's own name shares the MOST tokens with
   * the rest of the query (excluding the code token itself and ordinary
   * filler), same "a more specific signal wins" principle used everywhere
   * else in this file. Only returns multiple names when the collision is
   * genuinely unresolvable from context (no distinguishing tokens present,
   * or a real tie) -- never silently picks an arbitrary winner.
   *
   * Deliberately does NOT try to guess whether a NON-matching token
   * "looks like" a code (e.g. flagging it as a probably-nonexistent code)
   * -- that's a judgment call on far shakier ground than "this exact
   * string is a real code we have data for," and is being tracked as its
   * own separate, not-yet-decided question (checkFamilyAmbiguity's
   * satisfied.length===0 branch), not folded in here. */
  findProductsByCode(query: string): string[] {
    const tokens = tokenizeLoose(query);
    const codeTokensUsed = new Set<string>();
    const found = new Set<string>();
    for (const token of tokens) {
      const names = this.codeToProductNames.get(token);
      if (names) { names.forEach(n => found.add(n)); codeTokensUsed.add(token); }
    }
    if (found.size <= 1) return [...found];

    let candidates = [...found];
    const contextTokens = new Set(
      tokens.filter(t => !codeTokensUsed.has(t) && !CONVERSATIONAL_FILLER_WORDS.has(t))
    );
    if (contextTokens.size > 0) {
      const scored = candidates.map(name => {
        const nameTokens = new Set(tokenizeLoose(name));
        const overlap = [...contextTokens].filter(t => nameTokens.has(t)).length;
        return { name, overlap };
      });
      const maxOverlap = Math.max(...scored.map(s => s.overlap));
      if (maxOverlap > 0) candidates = scored.filter(s => s.overlap === maxOverlap).map(s => s.name);
    }
    if (candidates.length <= 1) return candidates;

    // Secondary tie-break: prefer candidates with real price data over ones
    // without, when a tie survives context scoring (or there was no
    // context at all, e.g. a bare "price of <code>" query). Confirmed
    // common, not a one-off: 46 of the catalog's 98 real code collisions
    // are exactly this shape -- one candidate is a documented phantom
    // catalog_index entry (e.g. Plinto's "(pag. 412)"-style cross-
    // reference mentions, see the multi-page-filter investigation) with
    // zero price rows, tied against a genuinely priced product that
    // happens to share the same code. Without this, the phantom entry
    // sits in the candidate list forever, offering the user a choice
    // that always dead-ends in "no price data" for one option. Only
    // narrows when the split is genuinely uneven (SOME but not ALL tied
    // candidates have price data) -- never removes anything when every
    // candidate is priced (a real, unresolvable collision) or none are
    // (nothing to prefer).
    const pricedCandidates = candidates.filter(name => this.prices.some(r => r.product_name === name));
    if (pricedCandidates.length > 0 && pricedCandidates.length < candidates.length) {
      candidates = pricedCandidates;
    }
    return candidates;
  }

  /** Removes any recognized real tier phrase (longest-match-first, whole
   * phrase, case/accent-insensitive) from the query text -- purely for
   * feeding a cleaner signal into fuzzy product-name matching, never used
   * for anything price-affecting. Confirmed necessary: "wlima pelle
   * glove" put GLOBE (edit distance 1 from the literal word "glove") at
   * the TOP of the LLM's candidate shortlist, ahead of WILMA itself
   * (distance 2) -- because "Pelle Glove" is a real catalog-wide tier
   * value, and the standalone word "glove" within it was being fuzzy-
   * matched against the whole catalog with no awareness it was already
   * part of a real, fully-explained tier phrase. Same failure family as
   * the already-logged MAGDA ML/CRISTAL spurious-extra-product findings
   * from an earlier round. */
  private stripKnownTierPhrases(query: string): string {
    let q = normalize(query);
    for (const tier of this.realTierPhrases) {
      const t = normalize(tier);
      if (t.length < 4) continue; // too short to safely strip without other collateral risk
      q = q.replace(new RegExp(`(?:^|\\W)${escapeRegex(t)}(?:$|\\W)`, 'gi'), ' ');
    }
    return q;
  }

  /** Find the best-matching product name(s) for a free-text query, sorted best-first. */
  matchProducts(query: string): { name: string; score: number }[] {
    return this.productNames
      .map(name => ({ name, score: similarity(query, name) }))
      .filter(x => x.score > 0)
      .sort((a, b) => b.score - a.score);
  }

  getCatalogEntry(productName: string): CatalogEntry | undefined {
    return this.catalogIndex.find(p => p.product_name === productName);
  }

  /** Longest-match, non-overlapping scan of the raw query text for every
   * REAL product name literally mentioned -- a deterministic backstop for
   * multi-product detection that works whether or not the LLM step ran,
   * and regardless of what its own product_names guess did or didn't
   * include. Real product names are checked longest-first and claim their
   * token span; a SHORTER name only counts if it has an occurrence
   * somewhere else in the text that isn't already claimed by a longer
   * one. That's what correctly tells apart "italia pelle and italia
   * couture pelle" (ITALIA appears once on its own AND once inside
   * "italia couture" -- both real, separate mentions) from a single
   * mention of one longer name like "eliot keramik drive" (the only
   * occurrence of "eliot keramik" is entirely inside that longer phrase,
   * so it must NOT also register as a second product). */
  detectNamedProductsInText(rawQuery: string): string[] {
    const q = normalize(rawQuery);
    const tokenRe = /[a-z0-9]+/g;
    const tokens: string[] = [];
    let tm: RegExpExecArray | null;
    while ((tm = tokenRe.exec(q))) tokens.push(tm[0]);

    const byLengthDesc = [...this.productNames].sort((a, b) => normalize(b).length - normalize(a).length);
    const claimed = new Set<number>();
    const found: string[] = [];

    for (const name of byLengthDesc) {
      const nameTokens = normalize(name).split(/[^a-z0-9]+/).filter(Boolean);
      if (nameTokens.length === 0) continue;
      let matchedFree = false;
      for (let i = 0; i <= tokens.length - nameTokens.length; i++) {
        let ok = true;
        for (let j = 0; j < nameTokens.length; j++) {
          if (tokens[i + j] !== nameTokens[j]) { ok = false; break; }
        }
        if (!ok) continue;
        const span = Array.from({ length: nameTokens.length }, (_, k) => i + k);
        if (span.some(idx => claimed.has(idx))) continue;
        span.forEach(idx => claimed.add(idx));
        matchedFree = true;
      }
      if (matchedFree) found.push(name);
    }
    return found;
  }

  /** Validate LLM-guessed product names against the real catalog list,
   * accepting a normalized (case/accent/whitespace-insensitive) match --
   * not just byte-exact -- so a guess that's correct in substance but
   * differs only in casing isn't silently discarded with no trace. */
  private validateProductNames(guesses: string[] | null): string[] {
    if (!guesses) return [];
    const result: string[] = [];
    for (const g of guesses) {
      if (!g) continue;
      if (this.productNames.includes(g)) { result.push(g); continue; }
      const match = this.productNames.find(real => normalize(real) === normalize(g));
      if (match) result.push(match);
    }
    return [...new Set(result)];
  }

  /** For a SINGLE resolved product (the plain, non-multi-product path),
   * excludes an "and"-joined clause that does NOT mention this product's
   * own name -- closing the other half of the tier-leak bug below,
   * WITHOUT trying to guess what that other clause refers to.
   *
   * A whole-catalog fuzzy guess (e.g. "does the other clause's first
   * word resemble some OTHER real product's name closely enough to be a
   * typo?") was prototyped for this exact purpose and rejected: ordinary
   * domain vocabulary keeps coincidentally resembling some short,
   * single-word product name at edit distance 1 in a catalog this size
   * -- "glove" is distance 1 from "GLOBE", "price" is distance 1 from
   * "PRIVE" -- so guessing "this looks like a typo'd product name" from
   * vocabulary alone risks inventing a phantom product out of an
   * ordinary tier/price word, which would be a WORSE failure than the
   * bug being fixed.
   *
   * Splitting ONLY on the literal word "and" (never a bare comma) is
   * deliberate and was verified against this project's entire query
   * history (queries.json + stress_v2_queries.json): every "and"-joined
   * query across both files (20 total) names 2+ PRODUCTS -- 0% use "and"
   * to join two modifiers of one single product (that pattern uses a
   * comma instead, e.g. "160x200, Extra fabric", which is exactly why
   * comma-splitting stays scoped to the ALREADY-multi-product-confirmed
   * path in scopeQueryPerProduct below, not here). Also safe for the one
   * same-name-repeated case in the corpus ("cody 1 and cody l") -- since
   * "cody" appears in both clauses, nothing gets excluded.
   *
   * This closes the case where `buildMultiProductResult`'s scoping is
   * never even reached: a sibling's name is a typo (e.g. "wlima") and no
   * LLM call resolved it this time, so `detectNamedProductsInText` only
   * ever finds ONE real name and the plain single-product path runs
   * instead -- previously with the FULL unscoped query, reproducing the
   * exact tier-leak bug even after the multi-product fix (confirmed:
   * "gve me greta wood pelle and wlima pelle glove" still returned GRETA
   * Wood's tier as "Pelle Glove" -- WILMA's -- instead of GRETA's own
   * correct "Pelle", specifically because this single-product path was
   * never touched by that first fix).
   *
   * Also returns whatever got excluded (`excludedClause`), so the caller
   * can surface it as an honest "I noticed more but couldn't identify
   * it" note instead of silently dropping it -- this path bypasses
   * `buildMultiProductResult`'s own "couldn't find X" notification
   * entirely (it's a different function), which is what let a named-but-
   * unresolved second product vanish with zero trace even after the
   * tier/size leak itself was fixed. Never claims the excluded text IS a
   * specific product -- just that something was there and wasn't
   * matched, which stays true and non-committal even in the rare case
   * the excluded clause was never a product reference at all (e.g. "...
   * and the matching lamp") -- confirmed intentional, not an oversight:
   * making that clause-detection any smarter would mean fuzzy-matching
   * it against the whole catalog, exactly the false-positive risk
   * (GLOBE/glove, PRIVE/price) already rejected above.
   *
   * A clause also "belongs" to the product if its words are a subset of
   * one of THIS product's own real fabric_tier values -- e.g. "aspen
   * glossy grey and brown" splits into ["aspen glossy grey", "brown"];
   * "brown" doesn't contain "aspen", but IS itself part of a real tier
   * value ("Glossy brown") this specific product actually has. Without
   * this, "brown" was wrongly treated as a reference to some OTHER,
   * unresolved product and silently dropped -- doubly wrong, since it's
   * not a product name at all, and it's a real, valid finish. This stays
   * on the safe side of the GLOBE/glove risk above: checking against a
   * small, exact, per-product value list (typically 5-15 real values,
   * pulled straight from this.prices), never guessing a product name
   * from ordinary vocabulary. Token-SET containment (not a contiguous-
   * phrase check) for the same reason as every other containment check
   * in this file -- "brown" is one word, "Glossy brown" is two. */
  private excludeUnrelatedAndClause(rawQuery: string, productName: string): { scopedQuery: string; excludedClause: string | null } {
    const segments = rawQuery.split(/\band\b/gi).map(s => s.trim()).filter(Boolean);
    if (segments.length <= 1) return { scopedQuery: rawQuery, excludedClause: null };
    const realTierValues = [...new Set(
      this.prices.filter(r => r.product_name === productName).map(r => r.fabric_tier).filter((t): t is string => !!t)
    )];
    const segTokens = (s: string) => normalize(s).split(/\s+/).filter(Boolean);
    const belongsToProduct = (seg: string) => {
      if (containsWholeWord(normalize(seg), normalize(productName))) return true;
      const tokens = segTokens(seg);
      if (tokens.length === 0) return false;
      return realTierValues.some(t => {
        const tierTokens = segTokens(t);
        return tokens.every(tok => tierTokens.includes(tok));
      });
    };
    const own = segments.filter(belongsToProduct);
    if (own.length > 0 && own.length < segments.length) {
      const excluded = segments.filter(seg => !own.includes(seg));
      return { scopedQuery: own.join(' '), excludedClause: excluded.join(' and ') };
    }
    return { scopedQuery: rawQuery, excludedClause: null };
  }

  /** Appends an honest, actionable note when `excludeUnrelatedAndClause`
   * found text it couldn't match to a product -- restores the "never
   * silently drop a named product" guarantee (Bug 3, round 1) for this
   * single-product fallback path, which bypasses buildMultiProductResult's
   * own unresolved-mentions notification entirely (a different function,
   * never reached here). Deliberately names the actual excluded text so
   * the person can see exactly what wasn't understood and act on it --
   * "try naming it more precisely" alone isn't actionable without that.
   * Never claims the excluded text IS a specific product, since it might
   * not be (see excludeUnrelatedAndClause's own doc comment). A no-op
   * when excludedClause is null, so safe to wrap every result with. */
  private withUnresolvedClauseNote(result: ChatResult, excludedClause: string | null): ChatResult {
    if (!excludedClause) return result;
    return {
      ...result,
      message: `${result.message}\n\n(Also mentioned "${excludedClause}" but couldn't match it to a real product in the catalog -- try naming it more precisely, or double-check the spelling.)`,
    };
  }

  /** Splits a multi-product raw query into per-product clauses on list
   * connectors ("and" / "," / "&") and assigns each of this message's
   * CONFIRMED real product names to whichever clause(s) actually mention
   * it -- so tier/size extraction for one product never sees another
   * product's clause at all, not just that other product's name (which
   * the earlier, narrower version of this fix stripped, but left its
   * tier words behind). Exact whole-phrase match first; a name only
   * resolved via typo-correction (its real spelling literally isn't in
   * the raw text, e.g. "wlima" -> WILMA) falls back to fuzzy
   * (Levenshtein) token matching -- but ONLY against this message's
   * OTHER already-confirmed names, never the whole catalog. That
   * restriction is deliberate and was verified necessary: a whole-catalog
   * fuzzy check was tried first and rejected, because a plain tier clause
   * like "extra fabric" fuzzy-matches several real but UNRELATED Bolzan
   * products at distance 0 ("Noah Extra large", "Bend-e Fabric" etc --
   * "extra"/"fabric" are literal words in those names), which would have
   * wrongly excluded a genuine tier clause for an ordinary single-product
   * query. Restricting the fuzzy fallback to just the OTHER names
   * actually named in THIS message avoids that false-positive entirely.
   * When a name can't be confidently isolated to a subset of clauses, its
   * original unscoped rawQuery is used instead of guessing wrong. */
  private scopeQueryPerProduct(rawQuery: string, names: string[]): Map<string, string> {
    const result = new Map<string, string>();
    if (names.length <= 1) {
      for (const name of names) result.set(name, rawQuery);
      return result;
    }
    const segments = rawQuery.split(/,|\band\b|&/gi).map(s => s.trim()).filter(Boolean);
    if (segments.length <= 1) {
      for (const name of names) result.set(name, rawQuery);
      return result;
    }

    const claimed = new Set<number>();
    const unresolved: string[] = [];
    for (const name of names) {
      const idx = segments.findIndex((seg, i) => !claimed.has(i) && containsWholeWord(normalize(seg), normalize(name)));
      if (idx !== -1) {
        claimed.add(idx);
        result.set(name, segments[idx]);
      } else {
        unresolved.push(name);
      }
    }

    for (const name of unresolved) {
      const nameTokens = normalize(name).split(/[^a-z0-9]+/).filter(Boolean);
      let bestIdx = -1;
      let bestDist = Infinity;
      for (let i = 0; i < segments.length; i++) {
        if (claimed.has(i)) continue;
        const segTokens = normalize(segments[i]).split(/[^a-z0-9]+/).filter(w => w.length >= 3);
        for (const nt of nameTokens) {
          for (const st of segTokens) {
            if (Math.abs(nt.length - st.length) > 2) continue;
            const d = this.levenshtein(nt, st);
            if (d < bestDist) { bestDist = d; bestIdx = i; }
          }
        }
      }
      if (bestIdx !== -1 && bestDist <= 2) {
        claimed.add(bestIdx);
        result.set(name, segments[bestIdx]);
      } else {
        result.set(name, rawQuery);
      }
    }
    return result;
  }

  /** Shared multi-product combiner: given 2+ CONFIRMED real product names
   * (from any source -- the raw-text scan, a validated LLM guess, or
   * both) plus any names that were clearly NAMED but never resolved to a
   * real product, build one response that addresses every one of them
   * explicitly -- either with real data or with a plain "couldn't find
   * X" -- never silently drops one (confirmed failure mode: "italia pelle
   * and italia couture pelle" and "...greta wood pelle and wlima pelle
   * glove" each answered only ONE of the two products actually named,
   * with zero mention of the other at all). */
  private buildMultiProductResult(
    validNames: string[],
    unresolvedMentions: string[],
    size: string | null,
    tier: string | string[] | null,
    rawQuery: string,
    brand: string,
    wantsFullList: boolean
  ): ChatResult {
    // Scope each product's own raw-query text to just ITS clause before
    // resolving it -- stripping only the sibling's NAME (the previous
    // version of this fix) isn't enough, because a sibling's own TIER
    // words are still left in the text too. Confirmed: "greta wood pelle
    // and wlima pelle glove" -- GRETA Wood's tier scan saw "pelle glove"
    // (WILMA's clause, left in the text) alongside its own correct
    // "pelle", and the "prefer the more specific tier" containment rule
    // picked "Pelle Glove" over GRETA's own real answer "Pelle" -- same
    // root cause as the Bend-e Fabric/Noah Extra large bug, just with a
    // sibling's TIER leaking in instead of its NAME.
    const scopedPerProduct = this.scopeQueryPerProduct(rawQuery, validNames);
    const perProduct = validNames.map(name => {
      const scopedQuery = scopedPerProduct.get(name) ?? rawQuery;
      // Re-derive size from THIS product's own scoped clause ONLY --
      // never fall back to the original shared `size`, which was
      // extracted from the WHOLE raw query and can belong to a SIBLING
      // instead (e.g. "sierra pouf 100x94x41h pelle and tina pelle" --
      // the size belongs to Sierra pouf only; TINA's own clause has no
      // size at all). Confirmed root cause of TINA (which has exactly 1
      // real size) sometimes safely refusing instead of resolving
      // directly: Sierra pouf's "100x94x41h" was leaking into TINA's
      // lookup too via the shared param, and TINA has no row at that
      // size, so it correctly (but unnecessarily) fell back to "not that
      // exact combination" instead of just using its one real size.
      // A first version of this fix fell back to the shared `size` when
      // the scoped clause had none, which was wrong: existing test id 12
      // ("bishop, richard a 245x234x102 pelle, and ritz lounge 118")
      // explicitly expects BISHOP -- which names no size of its own -- to
      // show its own ambiguous size options, NOT silently borrow
      // RICHARD's or RITZ Lounge's size. No size in this product's own
      // clause means no size constraint for this product, full stop.
      const scopedSize = extractSize(scopedQuery);
      // Scope the LLM's own shared `tier` guess the same way -- it's a
      // SINGLE flat array for the WHOLE multi-product message with no
      // per-product correspondence at all. Confirmed real and separate
      // from the raw-text-scan leak fixed above: with live conversation
      // history, the LLM returned product_names: ["GRETA Wood","WILMA"]
      // (correctly, both!) alongside fabric_tier: ["Pelle","Pelle
      // Glove"] as one shared list -- and this shared array was being
      // passed UNFILTERED to every product's own lookup, so it still
      // contaminated GRETA Wood's tier even though the raw-text scoping
      // above was already correct. Only keep a shared tier value for
      // THIS product if it's textually present (whole phrase) in this
      // product's own already-scoped clause -- purely narrows toward
      // what this product's own text actually supports, never invents
      // one; if none of the shared values match this product's clause,
      // this product gets no LLM-tier signal at all and falls back to
      // the raw-text-scan safety net inside lookupForProduct (which
      // re-derives independently from this same scoped text anyway).
      const tierArray = Array.isArray(tier) ? tier : (tier ? [tier] : []);
      const scopedTierArray = tierArray.filter(t => containsWholeWord(normalize(scopedQuery), normalize(t)));
      return {
        // Every `name` here is already a CONFIRMED specific product (this
        // is the whole point of validNames) -- never a guess still needing
        // disambiguation, so the family-ambiguity backstop must not
        // re-apply per product. Without this, a name belonging to a
        // multi-member family (e.g. a Big/Big Light SKU) whose scoped
        // clause happens not to repeat that SKU's own distinguishing code
        // text verbatim gets silently re-flagged as "ambiguous" all over
        // again here, turning what should be a real price answer for a
        // known product into a nested "which one did you mean?" -- most
        // visible with a content-free scoped clause (every candidate gets
        // the same unscoped rawQuery when there's nothing to split on, see
        // scopeQueryPerProduct above), but the underlying issue is general
        // to this call site, not specific to any one caller.
        name,
        result: this.answerFromIntent(name, scopedSize, scopedTierArray.length > 0 ? scopedTierArray : null, scopedQuery, brand, null, wantsFullList, null, null, true),
      };
    });
    const combinedMatches = perProduct.flatMap(p => p.result.matches || []);
    const combinedImages = [...new Set(perProduct.flatMap(p => p.result.image_urls || []))];
    const notFoundLines = unresolvedMentions.map(n => `${n}: couldn't find a matching product in the catalog.`);
    // Above a certain count, concatenating every sub-lookup's own message
    // (each one already a full sentence, sometimes a full price grid's
    // worth of text) stops being a reply and becomes an unreadable wall
    // of text -- confirmed real: "give me all Emma Cross prices" (96
    // real products) produced a 13,296-character message that's just
    // every per-product status line pasted back to back, BEFORE the
    // actual structured tables/images even render. The full per-product
    // detail already lives in `combinedMatches`/`combinedImages` below
    // (what the UI actually renders as tables) -- the message text only
    // needs to be a short human-readable summary once N is large, not a
    // duplicate trace of the same data. A smaller multi-product reply
    // (confirmed fine up to at least 13, e.g. "give barcode all price")
    // keeps the existing full per-candidate text -- it's still short
    // enough to read, and callers/tests may depend on its exact shape.
    const LARGE_RESULT_THRESHOLD = 20;
    let message: string;
    if (validNames.length > LARGE_RESULT_THRESHOLD) {
      const pricedCount = perProduct.filter(p => (p.result.matches?.length ?? 0) > 0).length;
      const unpricedCount = perProduct.length - pricedCount;
      // Use the real collection name in the summary when every candidate
      // shares one (ground truth, same mechanism as the wantsFullList
      // majority-group fix above) -- falls back to a generic phrasing
      // when they don't (e.g. a mixed-name multi-product request), never
      // guessing a collection that isn't actually true for the whole set.
      const collections = new Set(validNames.map(n => this.productNameToCollection.get(n)));
      const subject = collections.size === 1 && [...collections][0] ? [...collections][0] : 'matching';
      const summaryParts = [`Found ${validNames.length} ${subject} products`];
      if (pricedCount > 0 || unpricedCount > 0) {
        summaryParts.push(`${pricedCount} priced, ${unpricedCount} without price data yet`);
      }
      const summary = `${summaryParts.join(' -- ')}. Full details and page images are shown below.`;
      message = [summary, ...notFoundLines].join('\n\n');
    } else {
      message = [...perProduct.map(p => `${p.name}: ${p.result.message}`), ...notFoundLines].join('\n\n');
    }

    return {
      status: 'multi_product',
      message,
      product_name: validNames.join(', '),
      matches: combinedMatches,
      image_urls: combinedImages,
    };
  }

  /** Full list of real product names for this brand -- used to ground the LLM's guesses. */
  getProductNames(): string[] {
    return [...this.productNames];
  }

  /** Cheap, standard edit-distance (Levenshtein) between two short strings. */
  private levenshtein(a: string, b: string): number {
    const dp: number[][] = Array.from({ length: a.length + 1 }, () => new Array(b.length + 1).fill(0));
    for (let i = 0; i <= a.length; i++) dp[i][0] = i;
    for (let j = 0; j <= b.length; j++) dp[0][j] = j;
    for (let i = 1; i <= a.length; i++) {
      for (let j = 1; j <= b.length; j++) {
        dp[i][j] = a[i - 1] === b[j - 1] ? dp[i - 1][j - 1] : 1 + Math.min(dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1]);
      }
    }
    return dp[a.length][b.length];
  }

  /** Finds real product names with a word closely matching (scaled edit
   * distance -- see below) a word in the query -- catches typos the
   * exact/whole-word matchers in `matchProducts`/`detectNamedProductsInText`
   * miss entirely (transposed letters, a doubled or dropped letter).
   * Confirmed sufficient for every typo pattern seen this session:
   * "wlima"->"wilma" (distance 2, a transposition), "kaay"->"kay"
   * (distance 1, doubled letter), "bishp"->"bishop" and "hystrx"->
   * "hystrix" and "agata"->"agatha" (distance 1, a dropped letter each).
   * Sorted closest-first.
   *
   * Two safeguards, both added after a confirmed real bug (found live-
   * testing a "give all" follow-up after a Bonaldo product was already
   * resolved): a flat distance-2 threshold is far too loose for SHORT
   * words -- "all" is distance 2 from "and" (2 of 3 letters differ,
   * barely a "typo"), and "and" is literally part of "Cuff bench and
   * pouf"'s own name, so an ordinary conversational word like "all" was
   * spuriously matching catalog-wide product-name fragments and
   * poisoning the LLM's shortlist with candidates that had nothing to do
   * with the actual query. Fixed with:
   * (a) excluding RISKY_SIZE_CODE_WORDS from the query-word pool
   *     entirely -- same pattern as the existing tier-phrase stripping
   *     just below (stripKnownTierPhrases), just for common English
   *     words instead of real tier values;
   * (b) scaling the allowed distance DOWN for short words (<=4 letters
   *     get distance<=1, only 5+ letter words get distance<=2) --
   *     verified this doesn't regress any of the 5 confirmed typo
   *     fixtures above: "wlima" (5) dist 2, "kaay" (4) dist 1, "bishp"
   *     (5) dist 1, "hystrx" (6) dist 1, "agata" (5) dist 1 -- all
   *     within their own scaled threshold. */
  private fuzzyMatchProducts(query: string): string[] {
    const qWords = normalize(query)
      .split(/[^a-z0-9]+/)
      .filter(w => w.length >= 3 && !RISKY_SIZE_CODE_WORDS.has(w));
    if (qWords.length === 0) return [];
    const results: { name: string; dist: number }[] = [];
    for (const name of this.productNames) {
      const nameWords = normalize(name).split(/[^a-z0-9]+/).filter(Boolean);
      let best = Infinity;
      for (const qw of qWords) {
        const qMaxDistance = qw.length <= 4 ? 1 : 2;
        for (const nw of nameWords) {
          if (Math.abs(qw.length - nw.length) > qMaxDistance) continue;
          const d = this.levenshtein(qw, nw);
          if (d <= qMaxDistance && d < best) best = d;
        }
      }
      if (best !== Infinity) results.push({ name, dist: best });
    }
    return results.sort((a, b) => a.dist - b.dist).map(r => r.name);
  }

  /** Builds a short candidate list to send the LLM instead of the entire
   * catalog's product names -- the product list dominates the token cost
   * of every call (measured: 75.8% of the system prompt for Cattelan
   * Italia's 533 products), and it's resent from scratch on every single
   * message regardless of how specific the query is.
   *
   * Combines three sources, each already proven this session, so this is
   * strictly a re-use of existing matching logic rather than new
   * heuristics: exact real-name mentions (`detectNamedProductsInText`,
   * from the Bug 3 fix), whole-word/token-overlap scoring
   * (`matchProducts`), and fuzzy edit-distance matching (above, new --
   * specifically to preserve typo tolerance, since the first two are both
   * exact/substring-based and would otherwise silently exclude a heavily
   * typo'd product from ever reaching the LLM at all).
   *
   * This is a CHEAP FIRST PASS, not a hard cap on what the LLM is allowed
   * to find -- see catalogChatRoute.ts, which retries with the full list
   * whenever the shortlisted call comes back with no confident match, so
   * a shortlist miss costs one extra full-price call on a rare query
   * instead of silently losing the product forever. */
  buildLlmShortlist(query: string, limit = 20): string[] {
    const ranked: string[] = [];
    const seen = new Set<string>();
    const add = (name: string) => {
      if (!seen.has(name)) { seen.add(name); ranked.push(name); }
    };
    // Highest priority: an exact real product CODE mentioned in the query
    // (see findProductsByCode) is a far more precise signal than any name
    // match below, so the LLM sees it first regardless of how the rest of
    // the query happens to fuzzy-score against other candidates.
    this.findProductsByCode(query).forEach(add);
    this.detectNamedProductsInText(query).forEach(add);
    this.matchProducts(query).forEach(m => add(m.name));
    // Strip known real tier phrases before fuzzy-matching specifically --
    // exact/substring matching above is unaffected (a real product name
    // never looks like a tier phrase), only the fuzzy typo-tolerance pass
    // needs this, since that's the one that goes word-by-word against
    // the whole catalog with no context at all.
    this.fuzzyMatchProducts(this.stripKnownTierPhrases(query)).forEach(add);
    return ranked.slice(0, limit);
  }

  /**
   * Get image URLs for a product. If specific rows are passed AND the
   * catalog has per-page image data, scope the result to just the PDF
   * page(s) those rows actually came from -- this is what prevents, e.g.,
   * "Cameo Maison h.29"'s page from showing up when the user asked about
   * h.7. Falls back to the whole product's image set if page-level data
   * isn't available (older extractions) or nothing resolves.
   */
  getImageUrls(productName: string, brand: string, rows?: PriceRow[]): string[] {
    const entry = this.getCatalogEntry(productName);
    if (!entry) return [];

    if (rows && rows.length > 0 && entry.page_images) {
      const pages = [...new Set(rows.map(r => r.source_pdf_page).filter((p): p is number => p != null))];
      if (pages.length > 0) {
        const scoped = pages
          .map(p => entry.page_images![String(p)])
          .filter((img): img is string => !!img);
        if (scoped.length > 0) {
          return [...new Set(scoped)].map(img => `/data/${brand}/images/${img}`);
        }
      }
    }

    return entry.images.map(img => `/data/${brand}/images/${img}`);
  }

  /** How many candidates a clarify_product response shows/returns at once.
   * Reused from the deterministic ambiguity branch's own pre-existing
   * limit (rather than inventing a new number) -- the goal here is
   * CONSISTENCY between the 4 places in this file that build a
   * clarify_product result (previously: 5 in one, uncapped -- up to 32 in
   * Big/Big Light's case -- in the other 3), not a fresh design. */
  private static readonly CLARIFY_CANDIDATE_CAP = 5;

  /** Single, shared way to build a clarify_product result, used by every
   * call site in this file that needs one (previously each built its own
   * inline, with 3 of the 4 uncapped and none disclosing a cap at all --
   * confirmed live: the same query type returned 5 candidates via one
   * path and 32 via another). Caps to CLARIFY_CANDIDATE_CAP and appends an
   * explicit "(and N more)" note when there's more to disclose, rather
   * than silently dropping them -- callers should pass the FULL candidate
   * set here and let this be the only place any truncation happens (never
   * pre-slice before calling this, since an early cap can corrupt logic
   * that needs to see the true full set first, e.g. a "which candidate
   * contains all the others" maximal check run on an already-truncated
   * list can miss a genuine dominant candidate that got cut). */
  private buildClarifyProductResult(allCandidates: string[]): ChatResult {
    const shown = allCandidates.slice(0, CatalogChat.CLARIFY_CANDIDATE_CAP);
    const hiddenCount = allCandidates.length - shown.length;
    const disclosure = hiddenCount > 0 ? ` (and ${hiddenCount} more)` : '';
    return {
      status: 'clarify_product',
      message: `I found a few products that could match: ${shown.join(', ')}${disclosure}. Which one did you mean?`,
      candidates: shown,
    };
  }

  /**
   * Main entry point for pure deterministic matching (no LLM). Returns a
   * structured result -- never a bare string -- so the caller can decide
   * exactly how to present it, including whether to show a screenshot and
   * whether to flag ambiguity.
   */
  answer(
    query: string,
    brand: string,
    lastModelVariant: string | null = null,
    lastProduct: string | null = null,
    lastCandidates: string[] | null = null
  ): ChatResult {
    // Deterministic multi-product backstop: run BEFORE the single-product
    // matcher below, so a message literally naming 2+ real products (e.g.
    // "italia pelle and italia couture pelle") is combined into one
    // multi_product reply even when the LLM step is unavailable -- the
    // single-product matcher below has no concept of "more than one
    // product in this message" at all.
    const namedProducts = this.detectNamedProductsInText(query);
    if (namedProducts.length > 1) {
      const size = extractSize(query);
      const tiers = extractTiers(query, namedProducts);
      const wantsFullList = /\b(all|full|complete|every)\b/i.test(query);
      return this.buildMultiProductResult(namedProducts, [], size, tiers, query, brand, wantsFullList);
    }

    // Raw product-CODE match, checked before the fuzzy/family name-matching
    // below -- a code is a far more precise signal than any name-based
    // score, and (unlike a product name) is never itself embedded in every
    // family member's own display name, so the ordinary name-matching path
    // below has no way to use it at all. Confirmed live: "Big Big Light
    // 13610 price" -- 13610 is a real code (Big / Big Light A B White),
    // but that product's own NAME doesn't contain "13610" anywhere, so it
    // was invisible to name matching and the query fell through to a
    // 32-candidate family-ambiguity dump that didn't even include the
    // product actually being asked for. Only fires when exactly one
    // product uses this code -- if a code is genuinely shared (confirmed
    // real: 27-52 such collisions per brand), surface just those specific
    // colliding candidates, a far more precise clarify_product than the
    // generic family dump this used to fall through to.
    if (namedProducts.length === 0) {
      const codeMatches = this.findProductsByCode(query);
      if (codeMatches.length === 1) {
        const productName = codeMatches[0];
        const { scopedQuery, excludedClause } = this.excludeUnrelatedAndClause(query, productName);
        const size = extractSize(scopedQuery);
        const tiers = extractTiers(scopedQuery, [productName]);
        const wantsFullList = /\b(all|full|complete|every)\b/i.test(query);
        return this.withUnresolvedClauseNote(
          this.lookupForProduct(productName, size, tiers, brand, scopedQuery, lastModelVariant, wantsFullList),
          excludedClause
        );
      }
      if (codeMatches.length > 1) {
        return this.buildClarifyProductResult(codeMatches);
      }
    }

    const matches = this.matchProducts(query);

    if (matches.length === 0) {
      // Content-free follow-up fallback (e.g. "give all", "yes, give
      // all") -- the LLM path already handles this via its lastProduct
      // anchor, but that anchor is never threaded into this deterministic
      // fallback at all, so a Groq outage turned every such follow-up
      // into a flat "couldn't find a product" (confirmed live, reproduced
      // deterministically, identical across all 3 brands -- not narrow to
      // Bonaldo). Only fires when the query has NOTHING left after
      // stripping ordinary filler/confirmation words (RISKY_SIZE_CODE_WORDS)
      // -- a query naming something real-but-unmatched (e.g. "the Vexalon
      // armchair") still has real content words left over and correctly
      // falls through to the plain no-match message below, same as today.
      const queryWords = normalize(query).split(/[^a-z0-9]+/).filter(Boolean);
      const isContentFree = queryWords.length > 0 && queryWords.every(w => RISKY_SIZE_CODE_WORDS.has(w));
      if (isContentFree && lastProduct && this.productNames.includes(lastProduct)) {
        const wantsFullList = /\b(all|full|complete|every)\b/i.test(query);
        return this.lookupForProduct(lastProduct, null, [], brand, query, lastModelVariant, wantsFullList);
      }
      // Sibling anchor to lastProduct above, for the case where the PREVIOUS
      // turn was itself unresolved -- a clarify_product candidate list, not
      // a confirmed product. Without this, a content-free follow-up right
      // after being shown a candidate list ("give all", "yes, give all")
      // had nothing to anchor to at all (lastProduct is correctly null in
      // this state, per the Issue 5 fix) and fell straight to the generic
      // "couldn't find a product" message below -- safe, but unhelpful
      // right after the user was just shown real options. Re-validates
      // each name against the current catalog defensively, same precedent
      // as the lastProduct check just above (this.productNames.includes(...)).
      //
      // Two different responses depending on what was actually asked
      // (this is Phase 2 of a deliberately staged rollout -- Phase 1
      // shipped with EVERY content-free follow-up re-asking, regardless of
      // wording, until Issue 4 established a consistent cap to safely
      // auto-resolve within):
      //   - An EXPLICIT "give all"/"all of them"/"everything" signal is a
      //     real, specific request -- auto-resolve every one of the
      //     (already-capped-to-CLARIFY_CANDIDATE_CAP, by construction)
      //     candidates as a genuine multi-product answer instead of just
      //     repeating the same question the user was clearly trying to
      //     move past.
      //   - A bare confirmation ("yes", "ok") carries no such signal --
      //     still re-surface the same list and ask, same as Phase 1.
      if (isContentFree && lastCandidates && lastCandidates.length > 0) {
        const validCandidates = lastCandidates.filter(c => this.productNames.includes(c));
        if (validCandidates.length > 0) {
          const wantsAllExplicitly = /\b(all|full|complete|every)\b/i.test(query);
          if (wantsAllExplicitly && validCandidates.length > 1) {
            return this.buildMultiProductResult(validCandidates, [], null, [], query, brand, true);
          }
          if (wantsAllExplicitly && validCandidates.length === 1) {
            // Same reasoning as buildMultiProductResult's per-product loop:
            // this candidate is already confirmed (it came from the prior
            // clarify_product turn's own candidate list), not a guess, so
            // skip the family-ambiguity backstop -- a content-free "give
            // all" carries no distinguishing text for checkFamilyAmbiguity
            // to find anyway.
            return this.answerFromIntent(validCandidates[0], null, [], query, brand, lastModelVariant, true, null, null, true);
          }
          return this.buildClarifyProductResult(validCandidates);
        }
      }
      return {
        status: 'no_product_match',
        message: "I couldn't find a product matching that in the catalog. Could you check the spelling or try the product's full name?",
      };
    }

    const topScore = matches[0].score;
    // NOT capped here -- the maximal containment check just below needs to
    // see the TRUE full tied set to decide correctly (a cap applied before
    // that check runs could hide the real dominant candidate if more than
    // CLARIFY_CANDIDATE_CAP names tie, silently corrupting the check
    // rather than just under-displaying results). Any display/return cap
    // happens once, at the very end, via buildClarifyProductResult.
    const topMatches = matches.filter(m => m.score >= topScore - 5);

    if (topMatches.length > 1 && topScore < 100) {
      // Any time there's more than one candidate and it's not a clean,
      // unique exact match (100), ask which one -- this includes ties at
      // the whole-word-containment score (80), which previously slipped
      // through silently and picked an arbitrary winner (e.g. "Flag" vs
      // "Flag impilabile" both matching "give me for Flag impilabile").
      //
      // EXCEPTION: if exactly one tied candidate's name contains every
      // OTHER tied candidate's name as a whole word/phrase (e.g. "ELIOT
      // Keramik Drive" contains "ELIOT Keramik" contains "ELIOT"), it's
      // not a real ambiguity -- it's strictly the more complete match,
      // matching everything the shorter name(s) do plus more. Asking to
      // clarify in that case is pure friction (confirmed: "give me every
      // price for eliot keramik drive" -- the exact real product name --
      // still asked to clarify against the shorter "ELIOT Keramik").
      // This does NOT fire for genuinely unrelated ties (e.g. "GRETA
      // Outdoor" vs "NAPOLEON Keramik Outdoor" both matching on the word
      // "outdoor") since neither contains the other -- those still ask.
      //
      // Uses TOKEN-SET containment (every word of the shorter name
      // appears somewhere in the longer one, any order) rather than a
      // literal contiguous-phrase check -- same root cause and same fix
      // shape as the similarity() fix earlier this session: confirmed
      // real, found investigating the Bonaldo coverage-gate integration,
      // "Innesti coffee table" doesn't contain "Innesti table" as a
      // contiguous phrase (the word "coffee" sits in between), so the
      // exact, verbatim product name still asked to clarify against its
      // own shorter sibling instead of resolving directly.
      // Parens stripped for the same reason as similarity()'s own
      // tokenization above -- without it, a tight "(Category)" suffix
      // (e.g. Ditre's "Arcade (Tables)" vs "Arcade (Small Tables)") never
      // registers as a real subset/superset pair here: "(tables)" and
      // "tables)"/"(small" are three different broken tokens instead of
      // the same shared "tables" plus an extra "small", so this maximal
      // check couldn't see that the longer name is strictly the more
      // complete match -- it fell through to a false tie/clarify instead
      // of resolving directly, confirmed via check_coverage's own exact-
      // name self-test for both products.
      const nameTokens = (s: string) => normalize(s).replace(/[()]/g, '').split(/\s+/).filter(Boolean);
      const maximal = topMatches.filter(m => {
        const mTokens = nameTokens(m.name);
        return topMatches.every(other => {
          if (other.name === m.name) return true;
          const otherTokens = nameTokens(other.name);
          return otherTokens.length > 0 && otherTokens.every(t => mTokens.includes(t));
        });
      });
      if (maximal.length !== 1) {
        // A genuinely ambiguous tie normally still asks -- EXCEPT when the
        // query explicitly says "all"/"full"/"complete"/"every": this
        // deterministic path previously had no equivalent of the LLM
        // path's `wants_full_list` handling at this decision point, so the
        // exact same query could return a full resolved multi-product list
        // (LLM path, via answerFromIntentMulti/buildMultiProductResult) or
        // a capped 5-candidate clarify prompt (this path) purely depending
        // on whether Groq happened to be available for that one request --
        // a real inconsistency with no relation to phrasing (confirmed:
        // "give barcode all price" sent 3x back to back returned a full
        // 36-row multi_product answer twice and a 5-candidate clarify once,
        // as the Groq key pool cycled between available and exhausted).
        //
        // Group tied candidates by their REAL catalog `collection` (not
        // blindly resolve the whole tied set) -- confirmed real risk:
        // "barcode" ties 13 genuine Barcode products AND 2 garbled Teli di
        // Copertura cross-reference names that only coincidentally
        // mention "barcode" deep in a long compatibility list, at the
        // SAME score, since a diluted overlap-fraction score doesn't care
        // where in the name a shared token sits.
        //
        // An EARLIER version of this fix grouped by shared FIRST TOKEN
        // instead of real collection -- caught before shipping by testing
        // "give all big price": "Big / Big Light" and "Big In&Out" are
        // two completely different, unrelated Varaschini collections that
        // BOTH tokenize to "big" as their first word, so that heuristic
        // silently merged 29 products from both into one bulk answer.
        // Real `collection` grouping fixes this outright (it's ground
        // truth, not a string guess) -- Varaschini's own "Teli di
        // Copertura barcode..." entries are correctly excluded too, since
        // their real collection is "Teli di Copertura", not "Barcode",
        // regardless of what word their (separately, already-known-
        // garbled) display name happens to start with.
        //
        // Falls back to the OLD per-candidate-name grouping only when a
        // candidate has no `collection` mapping at all (every non-
        // Varaschini brand, which has no collection concept) -- each such
        // candidate becomes its own singleton group, so the largest-group
        // step below can only ever pick a real, ground-truthed group of
        // 2+, never merge two ungrounded guesses together.
        //
        // When 2+ DIFFERENT real collections are substantially tied (both
        // "Big / Big Light" and "Big In&Out" have many members) this
        // still auto-resolves the larger one rather than asking which
        // collection was meant -- a deliberate, narrower scope than full
        // disambiguation: the bug this fixes is silent, incorrect mixing
        // of unrelated products in one answer, which is now impossible:
        // every resolved answer is one single real collection's own
        // complete member list, never a blend.
        const wantsFullList = /\b(all|full|complete|every)\b/i.test(query);
        if (wantsFullList) {
          const groups = new Map<string, typeof topMatches>();
          for (const m of topMatches) {
            const key = this.productNameToCollection.get(m.name) ?? `__ungrouped__:${m.name}`;
            const group = groups.get(key);
            if (group) group.push(m); else groups.set(key, [m]);
          }
          const largestGroup = [...groups.values()].sort((a, b) => b.length - a.length)[0];
          if (largestGroup.length > 1) {
            return this.buildMultiProductResult(largestGroup.map(m => m.name), [], null, [], query, brand, true);
          }
        }
        return this.buildClarifyProductResult(topMatches.map(m => m.name));
      }
      const productName = maximal[0].name;
      const { scopedQuery, excludedClause } = this.excludeUnrelatedAndClause(query, productName);
      const size = extractSize(scopedQuery);
      const tiers = extractTiers(scopedQuery, [productName]);
      const wantsFullList = /\b(all|full|complete|every)\b/i.test(query);
      return this.withUnresolvedClauseNote(
        this.lookupForProduct(productName, size, tiers, brand, scopedQuery, lastModelVariant, wantsFullList),
        excludedClause
      );
    }

    const productName = topMatches[0].name;
    const { scopedQuery, excludedClause } = this.excludeUnrelatedAndClause(query, productName);
    const size = extractSize(scopedQuery);
    const tiers = extractTiers(scopedQuery, [productName]);
    const wantsFullList = /\b(all|full|complete|every)\b/i.test(query);
    return this.withUnresolvedClauseNote(
      this.lookupForProduct(productName, size, tiers, brand, scopedQuery, lastModelVariant, wantsFullList),
      excludedClause
    );
  }

  /**
   * LLM-assisted entry point: the caller (see llmIntent.ts) has already
   * asked an LLM to interpret the user's message -- including handling
   * typos, conversational phrasing, and follow-up context like "and in a
   * bigger size?" -- and produced a guess at the product/size/tier(s).
   *
   * SAFETY: the LLM's product name guess is NEVER trusted blindly. It's
   * checked against the real catalog product list; if it doesn't match
   * exactly, or is missing/null, this falls back to the same tested
   * deterministic matcher used by answer(). The actual price lookup is
   * IDENTICAL to the deterministic path either way -- the LLM only ever
   * influences which row gets looked up, never what the price is.
   */
  /**
   * Ambiguity backstop for the LLM path, specifically for the shape found
   * live with Varaschini's Big/Big Light: many real products (32 SKUs)
   * share a genuinely specific multi-word name prefix ("Big / Big Light"),
   * differing only by a trailing code/descriptor -- and a query naming
   * only the shared family, with nothing distinguishing any one member,
   * always got silently resolved to the SAME arbitrary SKU regardless of
   * what was actually asked (confirmed live: same wrong SKU for "Ceramica
   * finish" and for the bare collection name). The deterministic answer()
   * path already has an analogous guard (the `maximal` token-SET
   * containment tie-break above), but answerFromIntent trusts ANY single
   * valid LLM guess outright with no equivalent check.
   *
   * This is NOT a reuse of that existing check -- reusing it directly was
   * tried and rejected (see commit history/session notes): it produced 22
   * false positives across the real regression fixtures (typo cases like
   * "agata flex pelle" scoring a low, coincidental, UNORDERED tie against
   * unrelated products) and, worse, never caught Big/Big Light at all
   * (its own score band -- diluted overlap from unstripped conversational
   * filler words like "what are the prices for" -- sits well below the
   * exact/containment tier a naive reuse would need to gate on).
   *
   * Instead this uses an ORDERED TOKEN-PREFIX relation (not the unordered
   * token-SET containment used everywhere else in this file): two product
   * names are "family" siblings only if they share a genuinely specific
   * (>=2 token) common LEADING sequence, using whichever grouping is
   * TIGHTEST for the chosen product (the max observed common-prefix
   * length across the whole catalog) -- this is what correctly excludes
   * a coincidental single-generic-word overlap (Varaschini's "Big In&Out"
   * only shares the 1 word "big" with "Big / Big Light", scores far below
   * the max-3 grouping its real SKU siblings share; Bonaldo's "Innesti
   * table"/"Innesti coffee table" only share 1 word too -- correctly left
   * alone for the EXISTING SET-containment maximal check to auto-resolve,
   * since ordered-prefix and unordered-SET containment are deliberately
   * different relations for different shapes of ambiguity).
   *
   * Once a real family is found, a member is "satisfied" by the query if
   * every one of its OWN tokens beyond the shared prefix appears in the
   * (filler-stripped) query text -- vacuously true for a family's "base"
   * member whose full name IS the shared prefix (e.g. bare "MAGDA" needs
   * nothing extra, so "magda price" -- filler-stripped to just "magda" --
   * uniquely satisfies it and no sibling). Zero satisfied members (like
   * "Ceramica finish", a real attribute that lives only in price-row
   * data, never in any product's own NAME) or 2+ still-tied-after-maximal
   * satisfied members both count as genuinely ambiguous.
   *
   * Verified against all 132 real regression fixtures (queries.json +
   * stress_v2_queries.json) before landing: 0 false positives, correctly
   * leaves MAGDA/MAGDA ML/PLANER/Innesti alone, correctly flags both
   * reported Big/Big Light phrasings, correctly leaves a specific SKU or
   * descriptive-suffix query (e.g. "Big / Big Light A B White") alone.
   *
   * EXTENDED to also cover a 1-token-prefix shape (e.g. Ditre's "Puppet
   * (Armchairs)"/"Puppet (Night)"), gated by a reciprocal-best-match
   * check inside the function itself -- see the maxLen===1 branch below
   * for the full reasoning and re-verification against this same fixture
   * set.
   */
  private checkFamilyAmbiguity(rawQuery: string, chosenName: string): string[] | null {
    const chosenTokens = tokenizeLoose(chosenName);

    let maxLen = 0;
    const prefixLens = new Map<string, number>();
    for (const n of this.productNames) {
      if (n === chosenName) continue;
      const len = commonPrefixLen(chosenTokens, tokenizeLoose(n));
      if (len === 0) continue;
      prefixLens.set(n, len);
      if (len > maxLen) maxLen = len;
    }
    if (maxLen === 0) return null;
    // A shared prefix of just 1 token is too weak a signal ON ITS OWN --
    // see the coincidental-single-word cases (Big In&Out / Innesti)
    // discussed above -- UNLESS every such 1-token sibling RECIPROCATES:
    // its own best prefix match anywhere in the WHOLE catalog is also
    // just this same pair, nothing deeper on either side. That's what
    // distinguishes Ditre's "Puppet (Armchairs)"/"Puppet (Night)" (or
    // Cali/Arcade/Vento-style pairs) -- two names whose ONLY real
    // relationship in the entire catalog is this shared first word, with
    // nothing better anywhere -- from "Big In&Out" (whose best match with
    // "Big / Big Light" is also just 1 token, but "Big / Big Light"
    // itself has a much deeper REAL family of 32 siblings sharing 3
    // tokens, so the relationship isn't mutual/best-for-both). Confirmed
    // real and necessary, not theoretical: once the Groq model fix
    // restored the LLM path this session, "give puppet all price" (zero
    // distinguishing signal) had the LLM confidently name just ONE of
    // these two real, unrelated Ditre products. Reusing the EXISTING
    // deterministic tie-check directly (matches/topMatches/maximal, i.e.
    // answer()'s own scoring) was tried here too and rejected for the
    // exact same reason the original Big/Big Light fix above rejected it:
    // the Puppet tie itself only scores 15 (low diluted-overlap), the
    // identical false-positive-prone score band -- confirmed via direct
    // testing, not assumed. This reciprocal check is a NEW, narrower gate
    // on top of the SAME ordered-prefix relation already used below (not
    // a separate mechanism) -- once a maxLen===1 family passes it, it
    // flows through the exact same satisfied/maximal logic as any other
    // family, which is what still correctly auto-resolves Innesti table/
    // Innesti coffee table without asking (the query naming either one
    // satisfies it, and the shared satisfied/maximal step below already
    // prefers the more complete match).
    let familyMembers: string[];
    if (maxLen === 1) {
      const candidates = this.productNames.filter(n => prefixLens.get(n) === 1);
      familyMembers = candidates.filter(n => {
        const nTokens = tokenizeLoose(n);
        let nMax = 0;
        for (const n2 of this.productNames) {
          if (n2 === n) continue;
          const len2 = commonPrefixLen(nTokens, tokenizeLoose(n2));
          if (len2 > nMax) nMax = len2;
        }
        return nMax === 1;
      });
    } else {
      familyMembers = this.productNames.filter(n => prefixLens.get(n) === maxLen);
    }
    if (familyMembers.length === 0) return null;

    const family = [chosenName, ...familyMembers];
    if (family.length <= 1) return null;

    const qTokens = new Set(
      tokenizeLoose(rawQuery).filter(t => !CONVERSATIONAL_FILLER_WORDS.has(t))
    );
    // A family member's OWN product CODE mentioned in the query also
    // counts as satisfying it, same as its name-distinguishing tokens --
    // needed because a member's code frequently ISN'T part of its own
    // display name at all (confirmed real: 6/32 Big/Big Light entries,
    // including the exact one this backstop needs to get right). Restricted
    // to this family's own members, not every code-matched product
    // catalog-wide (findProductsByCode itself can return a match outside
    // this family if the query happens to mention an unrelated product's
    // code too -- irrelevant here).
    const codeMatchedInFamily = new Set(this.findProductsByCode(rawQuery).filter(n => family.includes(n)));
    const distinguishingOf = (n: string) => tokenizeLoose(n).slice(maxLen);
    const satisfied = family.filter(n => codeMatchedInFamily.has(n) || distinguishingOf(n).every(t => qTokens.has(t)));

    if (satisfied.length === 0) return family;
    if (satisfied.length === 1) return null;

    // 2+ satisfied: keep only the MAXIMAL ones (whose confirmed
    // distinguishing set isn't a strict subset of another satisfied
    // member's) -- e.g. "magda ml price" satisfies both bare MAGDA
    // (vacuously, empty distinguishing set) and MAGDA ML ("ml" present);
    // MAGDA ML's confirmed set strictly contains MAGDA's, so only MAGDA
    // ML is maximal and this correctly resolves directly, not ambiguous.
    const maximal = satisfied.filter(n => {
      const nSet = new Set(distinguishingOf(n));
      return !satisfied.some(other => {
        if (other === n) return false;
        const otherSet = new Set(distinguishingOf(other));
        return otherSet.size > nSet.size && [...nSet].every(t => otherSet.has(t));
      });
    });
    if (maximal.length === 1) return null;
    return maximal;
  }

  /** True if `rawQuery`, after removing this product's own name and
   * ordinary filler/size/tier vocabulary, still has real leftover content
   * -- signaling an attempt to independently name something specific,
   * rather than a genuine short/vague continuation of the SAME product
   * (a bare size fragment, a tier follow-up, a model-code snippet -- the
   * shape buildSystemPrompt's anchorNote is actually meant for). Reuses
   * the SAME already-audited word lists as similarity()'s own generic-
   * word suppression, plus this catalog's own real tier vocabulary
   * (`realTierPhrases`), rather than a new list -- same precedent as
   * every other word-list reuse in this file. Used only as the anchor-
   * trust backstop just above; has no effect on any query that doesn't
   * exactly repeat lastProduct. */
  private queryLooksLikeUnrecognizedProductAttempt(rawQuery: string, productName: string): boolean {
    const stripped = stripNameFromQuery(normalize(rawQuery), normalize(productName));
    const tokens = tokenizeLoose(stripped);
    const tierWords = new Set(this.realTierPhrases.flatMap(t => tokenizeLoose(t)));
    const leftover = tokens.filter(t =>
      !CONVERSATIONAL_FILLER_WORDS.has(t) &&
      !RISKY_SIZE_CODE_WORDS.has(t) &&
      !GENERIC_CATEGORY_WORDS.has(t) &&
      !tierWords.has(t) &&
      !/^\d+x\d+/.test(t) &&
      !/^\d+$/.test(t)
    );
    return leftover.length > 0;
  }

  answerFromIntent(
    productNameGuess: string | null,
    size: string | null,
    tier: string | string[] | null,
    rawQuery: string,
    brand: string,
    lastModelVariant: string | null = null,
    wantsFullList: boolean = false,
    lastProduct: string | null = null,
    lastCandidates: string[] | null = null,
    skipFamilyAmbiguityCheck: boolean = false
  ): ChatResult {
    const validProductName = productNameGuess && this.productNames.includes(productNameGuess)
      ? productNameGuess
      : null;

    if (!validProductName) {
      // LLM guess missing or not a real product -- fall back to the
      // tested deterministic matcher on the raw text instead of guessing.
      // lastProduct/lastCandidates were PREVIOUSLY dropped here (a real,
      // pre-existing gap found while wiring up lastCandidates for Issue
      // 3): this is exactly the path a content-free follow-up takes
      // whenever Groq is live but returns an uncertain product_names guess
      // (as opposed to being fully unavailable, which correctly threads
      // effectiveLastProduct via the OUTER null-intent branch in
      // catalogChatRoute.ts) -- so the anchor was silently unavailable in
      // that specific case even before either anchor mattered for the
      // question actually being asked here (which one to reuse).
      return this.answer(rawQuery, brand, lastModelVariant, lastProduct, lastCandidates);
    }

    // A real product CODE mentioned in the query is a more authoritative
    // signal than whatever name the LLM guessed -- prefer it outright.
    // Necessary even with buildLlmShortlist now surfacing the code-matched
    // candidate too: nothing forces the LLM to actually PICK it over
    // another name from the rest of its shortlist, so this override
    // doesn't depend on the LLM choosing correctly at all. See
    // findProductsByCode's own doc comment; ignored when the code is
    // genuinely shared across 2+ products (a real, if rare, collision --
    // deferring to whatever the LLM/checkFamilyAmbiguity below resolve to
    // rather than guessing which of the colliding products was meant).
    const codeMatches = this.findProductsByCode(rawQuery);
    const effectiveProductName = codeMatches.length === 1 ? codeMatches[0] : validProductName;

    // Ambiguity backstop: even though the LLM confidently returned ONE
    // valid product name, check whether it actually belongs to an
    // unresolved SKU family the raw query gives no way to narrow (see
    // checkFamilyAmbiguity's own doc comment for the full reasoning and
    // verification). Only fires for a genuine family + zero/multiple
    // satisfied members -- never touches the ordinary single-product case.
    //
    // skipFamilyAmbiguityCheck exists for callers where productNameGuess
    // is already a CONFIRMED specific product, not a guess needing
    // disambiguation -- buildMultiProductResult's per-product loop is the
    // only such caller (see there). Found via a real reproduction, not
    // theoretical: auto-resolving a content-free "give all" against a
    // multi-member family (Issue 3 Phase 2) called this per candidate
    // with a scoped query of literally just "give all" -- no product's
    // own distinguishing code text anywhere in it -- so this check
    // (correctly, from its own narrow view) found nothing satisfying any
    // SINGLE family member and re-flagged the WHOLE family as ambiguous
    // again, for every one of the already-chosen candidates, turning a
    // multi-product price answer into N nested "which one?" messages.
    // Anchor-trust check, computed BEFORE checkFamilyAmbiguity (not after)
    // because it affects both what follows: the LLM's confident single
    // guess might just be REUSING lastProduct because the system prompt
    // explicitly tells it to for a short/vague continuation
    // (buildSystemPrompt's anchorNote in llmIntent.ts) -- even when the
    // CURRENT message is actually attempting to name a DIFFERENT, specific
    // product it doesn't recognize, not a genuine continuation. Confirmed
    // real and non-deterministic, not theoretical: with a real prior turn
    // resolving "Ada (Sofa)" as lastProduct, "give online 2er sofa price"
    // (attempting to name Ditre's "On Line", unreachable by the
    // deterministic matcher -- see the separately-tracked "online"/"On
    // Line" tokenization gap) confidently returned "Ada (Sofa)" in 4 of 5
    // identical trials with the real HTTP request shape. The anchor note's
    // own given examples ("and in 180x200?", "what about extra fabric",
    // "h.29") are all bare size/tier fragments with nothing resembling an
    // attempted product name, unlike this case.
    //
    // isTrustedAnchorContinuation is ALSO used to skip checkFamilyAmbiguity
    // just below -- found necessary via the SAME real multi-turn testing
    // that caught the bug above: once checkFamilyAmbiguity was extended
    // (this session) to also cover 1-shared-token families like Ditre's
    // "Ada (Sofa)"/"Ada (Night)", a genuine, already-resolved continuation
    // ("82x82" after a prior turn established "Ada (Sofa)" specifically)
    // wrongly re-triggered a fresh "which one?" every time, because
    // checkFamilyAmbiguity only ever looks at the CURRENT turn's bare text
    // with no awareness that the ambiguity was already settled last turn.
    // A trusted anchor continuation means the ambiguity is, by definition,
    // already resolved (lastProduct IS the answer from a previous turn),
    // so re-running a context-blind check here is redundant and actively
    // harmful -- skipFamilyAmbiguityCheck reused as the same "already a
    // confirmed choice, not a fresh guess" opt-out shape as this.
    const isAnchoredGuess = effectiveProductName === lastProduct;
    const looksLikeFreshAttempt = isAnchoredGuess && this.queryLooksLikeUnrecognizedProductAttempt(rawQuery, effectiveProductName);
    const isTrustedAnchorContinuation = isAnchoredGuess && !looksLikeFreshAttempt;

    const familyCandidates = (skipFamilyAmbiguityCheck || isTrustedAnchorContinuation)
      ? null
      : this.checkFamilyAmbiguity(rawQuery, effectiveProductName);
    if (familyCandidates) {
      return this.buildClarifyProductResult(familyCandidates);
    }

    if (!skipFamilyAmbiguityCheck && looksLikeFreshAttempt) {
      return this.answer(rawQuery, brand, lastModelVariant, lastProduct, lastCandidates);
    }

    // Tier(s) from the LLM might not exactly match our normalized whitelist
    // casing, and might be a single string or an array (e.g. "Extra and
    // Plus" -> ["Extra", "Plus"]) -- normalize to an array either way.
    const tierArray = Array.isArray(tier) ? tier : (tier ? [tier] : []);
    // Same "and"-clause exclusion as answer()'s own single-product path --
    // this is a SEPARATE call site (the LLM validated exactly one real
    // product name, e.g. it didn't extract a typo'd sibling like "wlima"
    // either) that was missed by that fix: it calls lookupForProduct
    // directly with the raw, unscoped rawQuery, never going through
    // answer() at all. Confirmed necessary: "gve me greta wood pelle and
    // wlima pelle glove" still returned GRETA Wood's tier as "Pelle
    // Glove" (WILMA's) even with a live, successful LLM call, because the
    // LLM's own product_names guess also only surfaced GRETA Wood here.
    const { scopedQuery, excludedClause } = this.excludeUnrelatedAndClause(rawQuery, effectiveProductName);
    // A THIRD gap in the same bug family, found after the multi-product
    // combiner's shared-tier-array fix (buildMultiProductResult): this
    // exact function is ALSO called directly whenever answerFromIntentMulti
    // resolves only ONE valid product name -- but the incoming `tier`
    // array is still the LLM's raw guess for the WHOLE original message,
    // which can contain a value belonging to a SECOND, unresolved product
    // (e.g. the LLM found only "GRETA Wood" in product_names but still
    // returned fabric_tier: ["Pelle","Pelle Glove"] for the 2-product
    // message). Confirmed by direct reproduction: this call site is what
    // the multi-product combiner's fix never touched. Same scoping
    // principle as that fix -- keep a tier value only if it's textually
    // present in THIS product's own scoped clause -- but falls back to
    // the FULL unfiltered array (not to nothing) when the scoped subset
    // is empty, unlike the multi-product combiner: an ordinary single-
    // product query with no "and"-clause at all (the vast majority of
    // calls to this function) leaves scopedQuery === rawQuery, and an
    // LLM-normalized tier that doesn't literally appear in the raw text
    // (the pre-existing, separately-documented tier-punctuation gap)
    // must not be silently discarded just because this fix exists.
    const scopedTierArray = tierArray.filter(t => containsWholeWord(normalize(scopedQuery), normalize(t)));
    const effectiveTierArray = scopedTierArray.length > 0 ? scopedTierArray : tierArray;
    const normalizedTiers = effectiveTierArray.map(normalize).filter(Boolean);
    // Same leak, same call site, different field: `size` here is ALSO
    // the LLM's raw guess for the WHOLE original message, and can belong
    // to a SECOND, unresolved product (e.g. product_names: ["GRETA
    // Wood"] only, but size: "51x59" -- WILMA's size, from an unresolved
    // "wlima" reference in the same message). Confirmed by direct
    // reproduction: GRETA Wood's OWN size ("62x62x78h") is textually
    // right there in its own scoped clause, yet the shared, wrong size
    // param overrode it, producing "not that exact size/fabric
    // combination" instead of resolving directly. Prefer this product's
    // own extracted size when its own clause has one at all; only fall
    // back to the shared value when it doesn't (same fallback reasoning
    // as the tier fix just above -- the ordinary single-product case,
    // where extractSize's fairly permissive regex might still miss a
    // size the LLM inferred from less literal phrasing).
    const ownSize = extractSize(scopedQuery);
    const effectiveSize = ownSize ?? size;
    // Restores the same "never silently drop a named product" note as
    // answer()'s own single-product branches -- this call site is reached
    // directly from answerFromIntentMulti's single-name shortcut (the
    // LLM confidently named only ONE product), so an excluded "and"-
    // clause here is equally real evidence of an unresolved second
    // reference the LLM didn't catch either. Safe when called from
    // buildMultiProductResult's own per-product loop too: that caller
    // already passes an individually-scoped clause with no "and" left in
    // it, so excludedClause is always null in that context.
    return this.withUnresolvedClauseNote(
      this.lookupForProduct(effectiveProductName, effectiveSize, normalizedTiers, brand, scopedQuery, lastModelVariant, wantsFullList),
      excludedClause
    );
  }

  /**
   * Drops a raw-scan-only name from the union whenever it's just a
   * word-subset fragment of another kept name that the LLM alone
   * resolved -- e.g. "cuff pouf" union'd ["Cuff bench and pouf"] (LLM)
   * with ["Cuff"] (literal scan) wrongly looked like 2 products, when
   * "Cuff" is really just a leftover piece of the one full name the LLM
   * already found. Never drops an LLM-confirmed name, and never drops a
   * fragment whose "parent" name was ALSO found independently by the
   * literal scanner -- that second condition is what protects genuine
   * multi-product queries like "italia pelle and italia couture pelle",
   * where the scanner finds BOTH "ITALIA" and "ITALIA Couture" as their
   * own separate spans (confirmed live: the LLM alone missed "ITALIA"
   * entirely there, so without this guard the fragment-drop logic would
   * have silently thrown away a real, explicitly-named second product).
   */
  private dropSubsumedFragments(validNames: string[], llmValid: string[], rawNamed: string[]): string[] {
    const tokensOf = (s: string) => normalize(s).split(/\s+/).filter(Boolean);
    return validNames.filter(n => {
      if (llmValid.includes(n) || !rawNamed.includes(n)) return true;
      const nTokens = tokensOf(n);
      return !validNames.some(m => {
        if (m === n || rawNamed.includes(m)) return false;
        const mTokens = tokensOf(m);
        return nTokens.length > 0 && nTokens.every(t => mTokens.includes(t));
      });
    });
  }

  /**
   * Handles requests naming MULTIPLE products at once (e.g. "give me
   * Pandora and Selene prices"). This is purely additive: for 0 or 1
   * validated product names, it defers entirely to the existing,
   * extensively-tested answerFromIntent -- nothing about single-product
   * behavior changes. For 2+, it runs that SAME single-product logic
   * once per product (each product's own variant-narrowing, addon
   * detection, and ambiguity handling all apply independently and
   * correctly) and combines the results into one response.
   */
  answerFromIntentMulti(
    productNameGuesses: string[] | null,
    size: string | null,
    tier: string | string[] | null,
    rawQuery: string,
    brand: string,
    lastModelVariant: string | null = null,
    wantsFullList: boolean = false,
    lastProduct: string | null = null,
    lastCandidates: string[] | null = null
  ): ChatResult {
    // Union of what the LLM guessed (normalized-matched against the real
    // catalog list, so a guess that's right in substance but differs only
    // in casing/whitespace isn't silently discarded) and what a
    // deterministic scan finds literally named in the raw text -- this is
    // what guarantees a product explicitly named in the query is never
    // silently dropped just because the LLM only surfaced one of two
    // clearly-named products (confirmed: "italia pelle and italia couture
    // pelle" and "...greta wood pelle and wlima pelle glove" each answered
    // only ONE of the two named products, with the other never mentioned
    // at all -- reproduced identically whether the LLM was live or not).
    const llmValid = this.validateProductNames(productNameGuesses);
    const rawNamed = this.detectNamedProductsInText(rawQuery);
    const validNames = this.dropSubsumedFragments([...new Set([...llmValid, ...rawNamed])], llmValid, rawNamed);

    // Anything the LLM explicitly named that never resolved to a real
    // product (even after normalized matching) is worth surfacing, not
    // silently vanishing -- the user gets either real data or an explicit
    // "couldn't find X", never neither.
    const unresolved = (productNameGuesses || []).filter(
      g => g && !this.productNames.some(real => normalize(real) === normalize(g))
    );

    if (validNames.length <= 1 && unresolved.length === 0) {
      return this.answerFromIntent(validNames[0] || null, size, tier, rawQuery, brand, lastModelVariant, wantsFullList, lastProduct, lastCandidates);
    }
    if (validNames.length === 0) {
      // Nothing resolved at all -- defer to the deterministic matcher's
      // own no-match/clarify handling rather than building an empty
      // multi-product shell around pure LLM noise. lastProduct/
      // lastCandidates threaded through here too, same reasoning as
      // answerFromIntent's own fallback just above.
      return this.answer(rawQuery, brand, lastModelVariant, lastProduct, lastCandidates);
    }

    // lastModelVariant is a single product's own anchor -- it doesn't
    // apply across DIFFERENT products, so each gets a clean lookup.
    return this.buildMultiProductResult(validNames, unresolved, size, tier, rawQuery, brand, wantsFullList);
  }

  /** Shared logic: given a CONFIRMED product name (already resolved, either
   * deterministically or via a validated LLM guess), filter prices by
   * size/tier and build the appropriate result. */
  /**
   * Resolve requested tier(s) (which might be partial or slightly off,
   * e.g. "B" or "b e tcl", and there might be more than one, e.g. "Extra
   * and Plus") against the REAL tier labels this specific product
   * actually uses. Different products use slightly different tier
   * vocabularies (e.g. Pouf Ares says "Extra Luxury fabric" where most
   * products just say "Extra"), so matching against the global whitelist
   * alone isn't enough -- and an exact-only match would fail on short
   * mentions like "B" that don't equal the full "B e TCL" label.
   * Returns the resolved real tier value(s) to filter on (deduplicated);
   * any requested tier that can't be matched is simply dropped rather
   * than causing the whole filter to fail.
   */
  private resolveTiersForProduct(productName: string, requestedTiers: string[]): string[] {
    const availableTiers = [...new Set(
      this.prices
        .filter(r => r.product_name === productName)
        .map(r => r.fabric_tier)
        .filter((t): t is string => !!t)
    )].map(normalize);

    // Generic label word every one of Varaschini's Shape A tier values is
    // wrapped in ("cat. E", "cat. B - COM", "cat. Luxury", ...) -- never
    // distinguishes one tier from another, so it's excluded before any
    // token comparison below. Without this, a short requested code sharing
    // just its first LETTER with this filler word (e.g. "c") would
    // spuriously prefix-match every tier in the list via raw-string
    // startsWith, not just the one actually meant -- confirmed live: a
    // bare "c" resolved to BOTH "cat. B - COM" and "cat. C" (both raw
    // strings begin with the character "c") before this exclusion, and
    // even "cat. B - COM" alone still begins with "c" via its "com" token.
    const TIER_FILLER_WORDS = new Set(['cat', 'category']);
    const significantTokens = (t: string) =>
      t.split(/[^a-z0-9]+/).filter(Boolean).filter(tok => !TIER_FILLER_WORDS.has(tok));

    const resolved = new Set<string>();
    for (const requested of requestedTiers) {
      const reqNorm = normalize(requested);
      if (availableTiers.includes(reqNorm)) {
        resolved.add(reqNorm);
        continue;
      }
      const partial = availableTiers.find(t => {
        const tTokens = significantTokens(t);
        // Exact whole-TOKEN match (e.g. "e" -> "cat. E", tokens ["e"] after
        // the filler word is stripped) -- needed because Varaschini's real
        // tier values wrap the distinguishing code in "cat. ", so it's
        // never a PREFIX of the raw string the way Bolzan's own tier values
        // are ("extra" starts with "e"). Checked first and unconditionally
        // (even for 1-2 char codes): it can only match a tier whose own
        // token is verbatim equal, never one that merely shares a leading
        // character. Confirmed live: "give allegra category e"/"give
        // allegra cat e" against Allegra Poltrona returned all 5 tiers
        // unfiltered before this, because bare "e" never resolved to
        // "cat. e".
        if (tTokens.includes(reqNorm)) return true;
        // Looser prefix/substring matching (Bolzan's original "b" -> "b e
        // tcl" case, etc.) is gated to reqNorm.length >= 3 -- for 1-2 char
        // codes, only the exact token match above is trusted, since a
        // short prefix check has no way to tell "c" deliberately meaning
        // the tier "C" apart from "c" just being the first letter of some
        // unrelated token (e.g. "com", another tier's own significant
        // token) or the stripped filler word itself.
        return reqNorm.length >= 3 &&
          (tTokens.some(tok => tok.startsWith(reqNorm) || reqNorm.startsWith(tok)) || t.includes(reqNorm));
      });
      if (partial) resolved.add(partial);
    }
    return [...resolved];
  }

  private lookupForProduct(
    productName: string,
    size: string | null,
    tiers: string[],
    brand: string,
    rawQueryHint: string = '',
    lastModelVariant: string | null = null,
    wantsFullListHint: boolean = false
  ): ChatResult {
    let rows = this.prices.filter(r => r.product_name === productName);
    if (rows.length === 0) {
      return {
        status: 'no_price_data',
        message: `I found "${productName}" in the catalog, but I don't have price data for it yet. Here's the product page so you can check it directly.`,
        product_name: productName,
        image_urls: this.getImageUrls(productName, brand),
      };
    }

    if (size) {
      const requestedNums = (size.match(/\d+/g) || []).map(Number).sort((a, b) => a - b);
      // If NONE of this product's rows have any real size data at all, a
      // numeric size filter can never mean anything for it -- `realNums`
      // below is always empty, so applying the filter would zero out
      // every row regardless of what number was requested, treating "no
      // size data exists to compare against" as "size mismatch, no
      // results" instead of what it actually is: an unanswerable filter
      // that should just be ignored. Confirmed this isn't a one-
      // collection edge case: a large share of Varaschini's own
      // collections are entirely or mostly null-size (Composizione
      // Tavoli, Cuscini e Tessuti, Teli di Copertura, Basi Tavolini,
      // Outdoor Cooking and several others 100%; Big/Big Light, Smart,
      // Summer Set mostly so) -- any of them hits this the moment a
      // numeric qualifier (the LLM's own `size` guess, or a literal
      // "dimension N"/"size N" the user typed) reaches this function.
      const hasAnyRealSize = rows.some(r => !!r.size);
      if (requestedNums.length > 0 && hasAnyRealSize) {
        const sizeFiltered = rows.filter(r => {
          const realNums = ((r.size || '').match(/\d+/g) || []).map(Number).sort((a, b) => a - b);
          if (requestedNums.length === realNums.length) {
            return requestedNums.every((n, i) => n === realNums[i]);
          }
          // Fewer numbers given than the real size has (e.g. a catalog
          // like Cattelan whose sizes are WIDTHxDEPTHxHEIGHT, but the
          // person only gave WIDTHxDEPTH) -- match as long as every
          // requested number appears among the real ones. Deliberately
          // NOT applied the other direction (more requested numbers than
          // real ones): that's over-specification, not a valid partial
          // match, so it stays a non-match same as before.
          return requestedNums.length < realNums.length && requestedNums.every(n => realNums.includes(n));
        });
        // Safety net: a short code like "h.7" or "sp.4.5" is a model/
        // variant identifier, not a genuine size -- but the LLM (or a
        // literal "sp 4.5" typed by the user) can still end up putting
        // just the number part in the size slot. If applying it as a
        // size filter would wipe out every row, but the raw query text
        // itself contains a recognizable model code that matches
        // something among the UNFILTERED rows, trust that instead of the
        // size filter -- the variant-matching logic below reads the raw
        // query directly and will resolve it correctly on its own.
        const rawCode = extractShortCode(rawQueryHint);
        const codeMatchesSomething = rawCode && rows.some(r => r.model_variant && extractShortCode(r.model_variant) === rawCode);
        // Safety net #2: the extracted `size` itself might not be a real
        // user-specified filter at all -- it can come from a metric size
        // baked directly into the product's own NAME (e.g. Varaschini's
        // "Babylon Coffee table 71x71"), while this product's actual row
        // `size` field is stored in a completely different format
        // (imperial, W/H/D-labeled: 'W 28 " - H 12 5/8 " - D 28 "'),
        // sharing no digits with the name at all. Applying the filter in
        // that case wipes out every row and produces a false
        // no_matching_variant instead of the full price list. Detected by
        // checking whether the extracted size string appears literally in
        // the product's own (normalized) name -- confirmed real for all 5
        // Varaschini coffee tables affected (Babylon 71x71/99x99, Cricket
        // 62x52, Summer Set 70x70/80x80).
        const sizeIsFromProductName = normalize(productName).includes(normalize(size));
        if (sizeFiltered.length === 0 && (codeMatchesSomething || sizeIsFromProductName)) {
          // leave `rows` as the unfiltered set; don't apply this size filter
        } else {
          rows = sizeFiltered;
        }
      }
    } else {
      // No "WIDTHxDEPTH"-style size was extracted, but the raw query might
      // still contain bare dimension numbers with no "x" between them --
      // a single diameter number ("ritz lounge 140"), or space-separated
      // dimensions ("premier wood 240 120") -- which extractSize()'s
      // x-only regex never captures at all. Reuses the exact same
      // subset-match logic as above; if nothing matches, leaves rows
      // unfiltered rather than zeroing out (the numbers might be
      // something else entirely, e.g. a typo -- safer to fall through).
      // Strip the product's own name out first -- same root cause as the
      // tier-matching strip further below: a number that's only present
      // because it's PART OF the product's own name (e.g. "Geometric
      // Table 400") isn't a real user-specified filter. Confirmed real:
      // "give me all prices for geometric table 400" read bareNums=[400]
      // and wrongly dropped the product's own null-size Supplemento row
      // (400 doesn't appear in a size of `null`), reporting 2 rows for a
      // product with 3 genuine price rows (same bug hit Mellow 400).
      const queryForBareNums = stripNameFromQuery(normalize(rawQueryHint), normalize(productName));
      const bareNums = (queryForBareNums.match(/\b\d{2,3}\b/g) || []).map(Number).sort((a, b) => a - b);
      if (bareNums.length > 0) {
        const sizeFiltered = rows.filter(r => {
          const realNums = ((r.size || '').match(/\d+/g) || []).map(Number).sort((a, b) => a - b);
          if (bareNums.length === realNums.length) {
            return bareNums.every((n, i) => n === realNums[i]);
          }
          return bareNums.length < realNums.length && bareNums.every(n => realNums.includes(n));
        });
        if (sizeFiltered.length > 0) rows = sizeFiltered;
      }
    }

    // Letter/code and shape-qualifier size disambiguation -- independent
    // of the purely-numeric filtering above, since many real sizes in
    // this catalog are distinguished ONLY by a leading letter/code
    // (MELODY's "A"-"E", SPINNAKER's "X"/"Y fissaggio...", RICHARD's "A"/
    // "B" which share identical dimensions) or a trailing shape qualifier
    // ("sag", "biscuit", "oval", "polygon") that carries no digits at all
    // for the numeric filter above to use (confirmed needed for 72
    // products with a non-numeric size code, plus 104 more where two
    // sizes share identical dimensions and only the letter/qualifier
    // tells them apart). Purely a narrowing step: only applies when the
    // signal excludes at least one candidate -- narrows to WHICHEVER
    // distinct sizes match (could be one, e.g. "melody size c", or a real
    // subset, e.g. "sag" alone legitimately matches TWO different "sag"
    // variants at different base dimensions -- excluding the 4+ non-"sag"
    // sizes is still a real, safe improvement even when it can't get all
    // the way to a single row). Never narrows to nothing and never
    // touches `rows` at all when the signal doesn't exclude anything.
    const distinctSizesRemaining = [...new Set(rows.map(r => r.size).filter((s): s is string => !!s))];
    if (distinctSizesRemaining.length > 1) {
      const signalTokens = findSizeSignalTokens(rawQueryHint, productName);
      const sizeHits = distinctSizesRemaining.filter(s => extractSizeTokens(s).some(tok => signalTokens.has(tok)));
      if (sizeHits.length > 0 && sizeHits.length < distinctSizesRemaining.length) {
        rows = rows.filter(r => r.size != null && sizeHits.includes(r.size));
      }
    }

    // Computed once, early, so every check in this function (including
    // the variant-rebroadening logic below) sees the SAME complete
    // signal -- combining the LLM's own understanding with a plain
    // keyword fallback, since either one alone can miss real intent.
    const wantsFullList = wantsFullListHint || /\b(all|full|complete|every)\b/i.test(rawQueryHint);

    // Safety net independent of the LLM's own tier guess (or the generic
    // Bolzan-vocabulary KNOWN_TIERS whitelist used by the deterministic
    // fallback): scan the raw query text directly against THIS product's
    // own real fabric_tier values. Catches an exact real tier mention the
    // structured extraction missed (confirmed: "sofia pelle glove" and
    // "stilo size l 220v" returned every tier unfiltered even though the
    // query literally contained the real tier string verbatim) without
    // depending on prompt-following for every possible tier name across
    // every brand's own vocabulary. Purely additive and exact-match only
    // (whole word/phrase, accent-insensitive via normalize) -- it can
    // only help narrow to a real row, never fabricate one.
    const realTierValues = [...new Set(
      this.prices.filter(r => r.product_name === productName).map(r => r.fabric_tier).filter((t): t is string => !!t)
    )];
    // Strip the product's own name out of the query text before scanning
    // for tier mentions -- every query naming this product trivially
    // contains its name, so a tier word that only "matches" because it's
    // PART OF the product's own name (not a separate mention) must never
    // count. Confirmed needed: Bolzan's "Poltrona e accessori Flag" (the
    // Italian word "e" = "and") and "Noah Extra large" both collided with
    // real tier names ("E", "Extra") that are simply substrings of the
    // product's own name, silently narrowing a "give me all prices for X"
    // request down to just that one tier -- same root cause as the
    // ARENA/PASCAL size-token fixes, here in the tier-matching path.
    const normalizedRawQuery = normalize(rawQueryHint);
    let queryMinusProductName = stripNameFromQuery(normalizedRawQuery, normalize(productName));
    // Also strip this product's own MODEL VARIANT names (e.g. Bonaldo's
    // leg-material option "Metallo Special") before scanning for tier
    // mentions -- a variant name can coincidentally share a word with a
    // real, independent fabric tier ("Special") elsewhere in this same
    // product's own data, which would otherwise get double-counted as an
    // extra requested tier nobody asked for. Confirmed real: "avant-garde
    // chair metallo special capri" returned both the "Special" AND
    // "Capri" tier rows instead of just "Capri". The specific variant
    // hasn't been narrowed down yet at this point in the flow, so every
    // one of this product's variant names is stripped, not just the
    // eventual winner -- safe, since this only ever removes text that's
    // part of a variant's own name, never a genuine tier mention.
    const realModelVariants = [...new Set(
      this.prices.filter(r => r.product_name === productName).map(r => r.model_variant).filter((v): v is string => !!v)
    )];
    for (const v of realModelVariants) {
      const nv = normalize(v);
      if (nv) queryMinusProductName = queryMinusProductName.replace(new RegExp(escapeRegex(nv), 'gi'), ' ');
    }
    // Token-SET containment (every word of the tier value appears
    // somewhere in the query, any order) rather than a contiguous-phrase
    // check -- same root cause and same fix shape as every other
    // containment check in this file. Confirmed real: "aspen glossy grey
    // and brown" (after excludeUnrelatedAndClause now correctly keeps
    // "brown" in scope) still doesn't contain "glossy brown" as a
    // contiguous phrase -- "grey and" sits in between -- so a real,
    // valid, explicitly-requested tier was silently dropped even once
    // the clause-exclusion half of this bug was fixed.
    const queryTokens = new Set(queryMinusProductName.split(/[^a-z0-9]+/).filter(Boolean));
    const rawTierHits = realTierValues.filter(t => {
      // Tokenize the SAME way queryTokens was built (split on any
      // non-alphanumeric char) rather than whitespace-only -- otherwise a
      // real tier value with embedded punctuation right against a word
      // (e.g. Varaschini's "cat. E", period with no trailing space) keeps
      // that punctuation as PART of its token ("cat."), which can never
      // equal anything in queryTokens: that side strips punctuation from
      // the raw query text too, so it can only ever produce a clean "cat"
      // token, never "cat." with the period. Confirmed live: this silently
      // broke every phrasing of every Varaschini Shape A tier ("cat e",
      // "cat. b-com", even the standalone word "luxury" within "cat.
      // Luxury"), not just the originally-reported "cat e"/"category e".
      const tierTokens = normalize(t).split(/[^a-z0-9]+/).filter(Boolean)
        // "cat"/"category" is the generic label word every one of
        // Varaschini's Shape A tier values starts with -- it never
        // distinguishes one tier from another, so it's not required to be
        // typed for the phrase to count as a real mention (same spirit as
        // the existing GENERIC_CATEGORY_WORDS/RISKY_SIZE_CODE_WORDS filler
        // lists in this file). Only relaxes what's REQUIRED; the tier's
        // own distinguishing token(s) -- "e", "b", "com", "luxury" -- still
        // must be present, so this can't cause an unrelated tier to match.
        .filter(tok => tok !== 'cat' && tok !== 'category');
      return tierTokens.length > 0 && tierTokens.every(tok => queryTokens.has(tok));
    });
    const tiersWithRawHits = [...new Set([...tiers, ...rawTierHits])];

    // Tracks whether a specific fabric_tier/colore filter actually
    // narrowed `rows` below -- needed so a "give me all/full ... <tier>"
    // query (e.g. "give all nairobi olive green price") doesn't get
    // mislabeled as the FULL price list further down just because it
    // happens to contain "all"/"full" wording. Confirmed real: Nairobi
    // has 3 colore options (Olive green/Acid green/Nut brown) x 3 sizes
    // = 9 real rows, p.54, but a colore-filtered query like this one only
    // ever returns the 3 rows for ONE color -- that's a legitimately
    // narrowed result, not the product's actual full list.
    let tierWasExplicitlyFiltered = false;
    if (tiersWithRawHits.length > 0) {
      const resolvedTiersAll = this.resolveTiersForProduct(productName, tiersWithRawHits);
      // Prefer the most specific match when multiple RESOLVED real tier
      // values are nested substrings of each other (e.g. "Pelle" and
      // "Pelle Glove" both resolve from a single "pelle glove" mention --
      // keep only "Pelle Glove", the objectively more complete match).
      // Deliberately applied here, AFTER resolving every source (the
      // LLM's own structured guess, the deterministic whitelist, AND the
      // raw-text scan) to real tier values -- not just within the raw
      // scan's own hits. Confirmed the LLM's own fabric_tier guess can
      // independently return BOTH "Pelle" and "Pelle Glove" for one
      // "pelle glove" mention (non-deterministically -- "wlima pelle
      // glove" showed 2 rows on one run, 1 row on another), which the
      // earlier, narrower raw-scan-only version of this fix didn't catch
      // since that contamination never touched rawTierHits at all.
      const resolvedTiers = resolvedTiersAll.filter(t =>
        !resolvedTiersAll.some(other => other !== t && containsWholeWord(normalize(other), normalize(t)))
      );
      if (resolvedTiers.length > 0) {
        rows = rows.filter(r => resolvedTiers.includes(normalize(r.fabric_tier)));
        tierWasExplicitlyFiltered = true;
      }
    }

    if (rows.length === 0) {
      const allForProduct = this.prices.filter(r => r.product_name === productName);
      const sizes = [...new Set(allForProduct.map(r => r.size).filter((x): x is string => !!x))];
      const tiers = [...new Set(allForProduct.map(r => r.fabric_tier).filter((x): x is string => !!x))];
      return {
        status: 'no_matching_variant',
        message: `I found "${productName}", but not that exact size/fabric combination. Available sizes: ${sizes.join(', ') || 'n/a'}. Available fabric tiers: ${tiers.join(', ') || 'n/a'}.`,
        product_name: productName,
        image_urls: this.getImageUrls(productName, brand),
      };
    }

    // If multiple rows remain and they span more than one distinct MODEL
    // VARIANT (e.g. "Cameo Maison h.7" vs "Cameo Maison h.29", or Jack's
    // wood vs iron frame), check whether the query mentions a term that
    // uniquely distinguishes one variant -- and narrow to it if so. This
    // is separate from (and more reliable than) the old variant_context
    // heuristic: model_variant is captured from headings that reliably
    // repeat the product name, so it's safe to match against directly.
    const distinctVariants = [...new Set(rows.map(r => r.model_variant).filter((x): x is string => !!x))];
    // Classify each of this product's variants as either a real
    // "main structure" (the actual product you're buying) or a
    // surcharge/add-on (e.g. an optional adjustable-base mechanism
    // upgrade, like Face's "basamento h.27") -- using a signal already
    // present in the data: surcharge prices are stored with a leading
    // '+' (e.g. "+158"), real structure prices aren't. This is computed
    // from ALL of the product's rows (not just whatever `rows` has been
    // filtered down to so far), so it stays stable regardless of which
    // size/tier the user asked about.
    const allProductRows = this.prices.filter(r => r.product_name === productName);
    const variantRowsMap = new Map<string, PriceRow[]>();
    for (const r of allProductRows) {
      if (!r.model_variant) continue;
      if (!variantRowsMap.has(r.model_variant)) variantRowsMap.set(r.model_variant, []);
      variantRowsMap.get(r.model_variant)!.push(r);
    }
    const isAddonVariant = (v: string): boolean => {
      const vrows = variantRowsMap.get(v) || [];
      if (vrows.length === 0) return false;
      const addonCount = vrows.filter(r => (r.price_eur || '').trim().startsWith('+')).length;
      return addonCount / vrows.length > 0.5;
    };
    /** Find the same-size price in the product's single main (non-addon)
     * variant, if there is exactly one -- used to show a combined total
     * when someone asks specifically for an add-on surcharge. */
    const findMainStructurePrice = (size: string | null): PriceRow | null => {
      const mains = [...variantRowsMap.keys()].filter(v => !isAddonVariant(v));
      if (mains.length !== 1 || !size) return null;
      const mainRows = variantRowsMap.get(mains[0]) || [];
      return mainRows.find(r => (r.size || '').replace(/\s/g, '').toLowerCase() === size.replace(/\s/g, '').toLowerCase() && !r.ambiguous) || null;
    };

    if (distinctVariants.length > 1 && rawQueryHint) {
      const qNorm = normalize(rawQueryHint);
      // Compare with punctuation/spacing stripped so "h7", "h.7", and
      // "h 7" are all treated as the same thing -- the distinguishing
      // part of a variant name is usually short (like "h.7"), and
      // requiring an exact literal match (including the dot) is far too
      // brittle for how people actually type.
      const strip = (s: string) => s.replace(/[^a-z0-9]/g, '');
      const qStripped = strip(qNorm);
      // A word counts as real distinguishing signal if it's long enough
      // (>=4 chars -- short PURE-alphabetic tokens are disproportionately
      // stopwords/fragments, not real signal) OR it contains a digit --
      // digit-bearing tokens are never stopwords/filler, and this
      // catalog's own numbered-variant naming convention (Ditre's
      // "2-er"/"3-er"/"3-er maxi" family) puts its ENTIRE distinguishing
      // signal in exactly this shape: split on punctuation, "2-er" becomes
      // ["2","er"], and the bare length>=4 filter discarded BOTH (neither
      // reaches 4 chars), leaving only the shared, non-distinguishing
      // "sofa" behind -- confirmed live: "On Line 2-er price", typed
      // exactly as printed, matched all 10 of On Line's variants instead
      // of narrowing to "2-er sofa"/"2-er central element". Deliberately
      // NOT lowering the bare length threshold itself (tried and
      // rejected as too broad -- see the full before/after diff run
      // across all 5 brands' variant data before this landed).
      const isDistinguishingWord = (w: string) => w.length >= 4 || /\d/.test(w);
      const qWords = new Set(
        qNorm.replace(normalize(productName), '').split(/[^a-z0-9]+/).filter(isDistinguishingWord)
      );
      // "h.NN" (a height/model number like "h.7", "h.21", "h27") is the
      // single most common distinguishing signal across this whole
      // catalog's multi-variant products -- extract it from the query up
      // front and check it FIRST, before other matching strategies. This
      // matters especially when two variants otherwise share most of
      // their wording (e.g. "Face - h.21 basamento regolabile" vs
      // "Face - h.27 basamento regolabile" -- the only real difference
      // is the number, which is too short to survive generic word-length
      // filtering and too short to appear as part of the "whole phrase"
      // compact match on its own).
      const qHeight = extractShortCode(qNorm);
      // Computes the "extra distinguishing" part of a variant name, on
      // top of the product's own name -- e.g. variant "Metallo Special"
      // on product "Avant-Garde chair" has nothing in common with the
      // product name at all, so the suffix is the whole thing; variant
      // "Cuff hi plus" on product "Cuff" strips the shared "Cuff" prefix
      // down to "hi plus".
      //
      // Bidirectional on purpose -- confirmed real, found live-testing
      // Pattern B's new RIVESTIMENTO named-column products: a variant can
      // ALSO be a strict PREFIX of the product's own name (shorter than
      // it), not just equal to or longer than it, e.g. product "Colibrì
      // soft" with variant "COLIBRÌ". The original one-directional
      // `variant.split(productName)` only handles the equal-or-longer
      // case correctly (splitting "nikos".split("nikos") on itself
      // correctly empties out) -- for the shorter-variant case,
      // "colibrì".split("colibrì soft") never finds a match at all
      // (the split pattern is LONGER than the string being split), so it
      // silently left the suffix as the full unmodified variant text,
      // which then spuriously "matched" any query naming the product by
      // its full name (since "colibrì soft" trivially contains "colibrì"
      // as a substring) -- wrongly narrowing "give me all prices for
      // Colibrì soft" down to just the COLIBRÌ column, silently dropping
      // FOOTREST. Confirmed general via a full-catalog scan, not narrow
      // to Colibrì soft: also affects Dune TV stand, Paddle TV stand,
      // Planet Big Planet, Cross lounge chair, Olos bergère, Belt &
      // Cross, Cuff bench and pouf -- 6 of those 8 pre-date this
      // session's own work entirely. When the variant is a strict prefix
      // of the product name, it carries no distinguishing information
      // beyond what the product name already says, so it's treated the
      // same as the equal case -- an empty suffix, correctly falling
      // through to "let every variant show" rather than a spurious match.
      const variantSuffix = (v: string): string => {
        const nv = normalize(v);
        const npn = normalize(productName);
        if (nv.includes(npn)) return nv.split(npn).join('').trim();
        if (npn.includes(nv)) return '';
        return nv;
      };

      let matchingVariant: string | undefined;
      let matched = false;
      let bestWordMatches = 0;
      let freshHeightSignal = false;

      if (qHeight) {
        const heightHits = distinctVariants.filter(v => {
          return extractShortCode(normalize(v)) === qHeight;
        });
        if (heightHits.length === 1) {
          matchingVariant = heightHits[0];
          matched = true;
        } else if (heightHits.length > 1) {
          freshHeightSignal = true;
          // Multiple variants share this height (e.g. Face's "basamento
          // h.8" surcharge AND its "Struttura...(h.8)" structure table
          // both mention "h.8") -- if the query ALSO contains a word that
          // uniquely picks out one of them (e.g. "struttura"), use that to
          // narrow within the height-matched set instead of guessing.
          const narrowed = heightHits.filter(v => {
            const suffix = variantSuffix(v);
            const suffixWords = suffix.split(/[^a-z0-9]+/).filter(isDistinguishingWord);
            return suffixWords.some(w => qWords.has(w));
          });
          if (narrowed.length === 1) {
            matchingVariant = narrowed[0];
            matched = true;
          } else {
            // Can't confidently pick just one, but the height number is
            // still real signal -- narrow the working set to just the
            // height-matched candidates (e.g. "the surcharge" and "the
            // matching structure table" for h.8) rather than losing the
            // signal entirely and showing every variant combined.
            rows = rows.filter(r => r.model_variant && heightHits.includes(r.model_variant));
          }
        }
      }

      let tiedFamily: string[] = [];
      if (!matched) {
        let bestCountAtBest = 0;
        // Every variant whose compact (whitespace/punctuation-stripped)
        // suffix appears in the compact query -- collected rather than
        // taking the first hit, since stripping whitespace erases real
        // word boundaries: "Cuff hi" and "Cuff plus" both compact-match
        // "cuff hi plus" (their suffixes "hi" and "plus" both appear in
        // "...cuffhiplus"), same as the fuller "Cuff hi plus" itself
        // (suffix "hi plus" -> "hiplus"). Confirmed real and not narrow to
        // Cuff: the same word-subset shape recurs across many Bonaldo
        // sofa-family products (Basket's "hi"/"plus"/"open" 3-way nesting,
        // Aliante/Liam/Superhiro's "Terminale dx/sx" vs "Terminale
        // angolare dx/sx", etc.).
        const compactMatches: string[] = [];
        for (const v of distinctVariants) {
          const suffix = variantSuffix(v);
          if (!suffix) continue;
          if (qStripped.includes(strip(suffix))) {
            compactMatches.push(v);
            continue;
          }
          const suffixWords = suffix.split(/[^a-z0-9]+/).filter(isDistinguishingWord);
          const shared = suffixWords.filter(w => qWords.has(w)).length;
          if (shared > 0 && shared === bestWordMatches) {
            bestCountAtBest += 1;
            tiedFamily.push(v);
          } else if (shared > bestWordMatches) {
            bestWordMatches = shared;
            bestCountAtBest = 1;
            matchingVariant = v;
            tiedFamily = [v];
          }
        }
        if (compactMatches.length > 0) {
          // Prefer the LONGEST compact match -- it's the most complete
          // one, accounting for the most of the query. A shorter sibling
          // that also compact-matches is, by construction, a strict
          // fragment of the longer one's own suffix.
          compactMatches.sort((a, b) => {
            const sa = strip(normalize(a).split(normalize(productName)).join('').trim());
            const sb = strip(normalize(b).split(normalize(productName)).join('').trim());
            return sb.length - sa.length;
          });
          matchingVariant = compactMatches[0];
          matched = true;
        } else {
          matched = bestCountAtBest === 1 && !!matchingVariant;
        }
      }

      // A tie (e.g. "struttura" alone matches all of Face's several
      // "Struttura..." sub-variants equally) still tells us something
      // real: the person wants THIS family, just not which member of it.
      // If a height number is available -- either mentioned in this same
      // message, or carried over from the previous turn's anchor (e.g.
      // switching from "Face - h.27 basamento regolabile" to "struttura"
      // should still mean h.27) -- use it to pick the right member of
      // that family, before ever falling back to blindly repeating
      // whatever variant was shown last turn.
      if (!matched && tiedFamily.length > 1) {
        const heightToUse = qHeight || (lastModelVariant ? extractShortCode(lastModelVariant) : null);
        if (heightToUse) {
          const withinFamily = tiedFamily.filter(v => extractShortCode(v) === heightToUse);
          if (withinFamily.length === 1) {
            matchingVariant = withinFamily[0];
            matched = true;
          }
        }
      }

      // If the current message explicitly re-states the product name AND
      // wants the full list (e.g. "give me all Iorca price"), that's a
      // clear signal to broaden scope -- not a narrow continuation. This
      // is what distinguishes it from a bare "give all", which correctly
      // continues whatever narrow context was already established (e.g.
      // staying on "Face - h.27" after asking about it specifically).
      // Without this, a product with several genuinely different
      // configurations (like Iorca's 5 "Soluzione" options, none of which
      // is more "main" than another) would stay stuck on whichever one
      // happened to be discussed last, even when explicitly asked to
      // broaden.
      const productFirstWord = normalize(productName).split(/\W+/)[0];
      const explicitProductRebroaden = wantsFullList && productFirstWord
        && new RegExp(`\\b${productFirstWord.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`).test(qNorm);

      // A tie among THIS turn's own word-matched candidates (tiedFamily,
      // populated by the qWords loop above) is real, contradicting signal
      // -- same status as freshHeightSignal for the purpose of the anchor-
      // reuse fallback just below, even though it isn't the h.NN/sp.NN
      // mechanism that flag was built for. Confirmed real and NOT
      // theoretical: with lastModelVariant="2-er sofa" anchored from an
      // earlier turn, "give online 3-er category U" -- typed correctly,
      // hyphenated, no typo -- genuinely ties all 4 of On Line's "3-er..."
      // siblings (matched stays false, by design, since none of them wins
      // outright), but freshHeightSignal only ever covers the unrelated
      // h.NN/sp.NN code path, so the anchor-reuse fallback below couldn't
      // tell "no signal at all" apart from "real signal that just didn't
      // uniquely resolve" -- it silently reused the STALE "2-er sofa"
      // anchor and returned a single confident (wrong) price, the exact
      // same "LLM/anchor confidently wrong" severity class as the
      // lastProduct-level Ada bug fixed earlier this session, just one
      // level down (variant selection, not product selection). This gate
      // is safety-only: it never auto-picks a different variant, it only
      // blocks blind reuse of a stale anchor when this turn's own text
      // contradicts it -- falls through to the same broad "show every
      // variant" result as if there were no anchor at all.
      const hasContradictingTurnSignal = freshHeightSignal || tiedFamily.length > 1;

      if (matched && matchingVariant) {
        rows = rows.filter(r => r.model_variant === matchingVariant);
      } else if (!hasContradictingTurnSignal && !explicitProductRebroaden && lastModelVariant) {
        // Nothing in THIS message names a specific variant, but we were
        // already narrowed to something in a previous turn. If that
        // anchor has a height number, prefer expanding to ALL current
        // candidates sharing that height -- this correctly restores a
        // soft-narrowed pair (e.g. "give all" after "Face h.8" should
        // bring back BOTH the h.8 surcharge and h.8 structure table, not
        // collapse to just whichever one happened to be remembered), and
        // also handles switching between variant FAMILIES that share a
        // height but have different names (e.g. "Face - h.27 basamento
        // regolabile" -> "Face / Struttura Legno Massello FSC (h.27)").
        const lastHeight = extractShortCode(lastModelVariant);
        const heightMatches = lastHeight
          ? distinctVariants.filter(v => extractShortCode(v) === lastHeight)
          : [];
        if (heightMatches.length > 0) {
          rows = rows.filter(r => r.model_variant && heightMatches.includes(r.model_variant));
        } else if (distinctVariants.includes(lastModelVariant)) {
          // No height number to work with -- fall back to the plain
          // exact-string anchor match (e.g. a variant family that isn't
          // height-based at all, like Jack's wood vs iron frame).
          rows = rows.filter(r => r.model_variant === lastModelVariant);
        }
      } else {
        // No specific variant named and no prior anchor. If exactly one
        // of the distinct variants is the real "main structure" and the
        // rest are surcharge/add-ons (e.g. Face: one real bed structure
        // plus several optional adjustable-base upgrades), default to
        // showing the main structure's real price -- that's what someone
        // asking a generic "how much is X" almost always wants, not a
        // confusing mix of the bed price and unrelated base-mechanism
        // surcharges.
        //
        // NEVER when wantsFullList is true, though -- confirmed via
        // check-coverage.ts (built for the Cattelan WILMA/PASCAL/ARENA
        // fixes) that this narrowing was firing even on an explicit "give
        // me all prices for X", silently dropping an entire real variant
        // block (Bolzan's "Jack" -- 117 clean rows expected, only 103
        // returned, because the bare "Jack" variant was auto-narrowed
        // away in favor of "Jack / Struttura in ferro" even though the
        // user asked for everything). A full-list request means every
        // variant, main and addon alike -- that's what "all" means.
        const mainVariants = distinctVariants.filter(v => !isAddonVariant(v));
        if (mainVariants.length === 1 && !wantsFullList) {
          rows = rows.filter(r => r.model_variant === mainVariants[0]);
        }
      }
    }

    // Ambiguity is handled per-row, not as an all-or-nothing block for the
    // whole response. If EVERY remaining row is ambiguous, there's nothing
    // safe to show -- block and point to the page. But if only SOME rows
    // conflict (e.g. Pouf basic Zoe: codes SCPT/SCP90 genuinely have
    // conflicting listed prices, but POUFFZ at 45x46 does not), show the
    // clean data normally and just note that some other options aren't
    // shown because they conflict in the source catalog -- don't hide
    // perfectly good data behind one unrelated code's conflict.
    const cleanRows = rows.filter(r => !r.ambiguous);
    const ambiguousRows = rows.filter(r => r.ambiguous);

    if (cleanRows.length === 0) {
      return {
        status: 'ambiguous_price',
        message: `"${productName}" has more than one listed price for this configuration in the source catalog (this usually means two structural variants share a page, like wood vs. iron frame). I can't confidently give you a single number -- here's the actual catalog page so you can confirm the right one.`,
        product_name: productName,
        matches: rows,
        image_urls: this.getImageUrls(productName, brand, rows),
      };
    }
    rows = cleanRows;
    const ambiguousNote = ambiguousRows.length > 0
      ? ` (${ambiguousRows.length} other price option(s) for this product have conflicting listed values in the source catalog and aren't shown here -- view the page to confirm those specifically.)`
      : '';

    if (rows.length === 1) {
      const r = rows[0];
      // Same reasoning as the multi-row case below: only show the bare
      // model_variant in place of the product name when it actually
      // identifies the product (e.g. Bolzan's "Cameo Maison h.7") --
      // otherwise (e.g. Cattelan's "cristallo specchiato bronzo") keep
      // the product identifiable by combining both.
      const displayName = r.model_variant
        ? (normalize(r.model_variant).includes(normalize(productName)) ? r.model_variant : `${productName} (${r.model_variant})`)
        : productName;
      let addonInfo = '';
      if (r.model_variant && isAddonVariant(r.model_variant)) {
        const mainPrice = findMainStructurePrice(r.size);
        addonInfo = mainPrice
          ? ` -- this is an add-on surcharge, not a standalone price. The base structure at this size is €${mainPrice.price_eur}, so the combined total is roughly €${combineAddon(mainPrice.price_eur, r.price_eur)}.`
          : ` -- this is an add-on surcharge (not a standalone price) meant to be added to the base structure's price.`;
      }
      return {
        status: 'ok',
        message: `${displayName}${r.size ? ` (${r.size})` : ''}${r.fabric_tier ? `, ${r.fabric_tier} fabric` : ''}: €${r.price_eur}${r.code ? ` (code ${r.code})` : ''}${ambiguousNote}${addonInfo}`,
        product_name: productName,
        matches: rows,
        image_urls: this.getImageUrls(productName, brand, rows),
      };
    }

    // Multiple non-conflicting rows remain (e.g. size given but not tier).
    const remainingVariants = [...new Set(rows.map(r => r.model_variant).filter((x): x is string => !!x))];
    let displayName: string;
    if (remainingVariants.length === 1) {
      // Some catalogs' model_variant already repeats the product name
      // (e.g. Bolzan's "Cameo Maison h.7"), where showing it alone is
      // strictly more informative than the bare product name. Others
      // (e.g. Cattelan's "cristallo specchiato bronzo", a Top-material
      // descriptor with no product identity in it at all) don't --
      // showing it alone silently REPLACES the product name in the
      // reply with something that doesn't identify the product at all
      // (confirmed: a real query for "AMSTERDAM" replied "Here's the
      // full price list for 'cristallo specchiato bronzo'"). Only
      // substitute when the variant text actually contains the product
      // name; otherwise keep the product identifiable by combining both.
      const variant = remainingVariants[0];
      displayName = normalize(variant).includes(normalize(productName))
        ? variant
        : `${productName} (${variant})`;
    } else {
      // If everything remaining shares the same short code (e.g. both
      // the "h.27 basamento" surcharge AND the "Struttura...(h.27)"
      // table), that's a meaningful family, not a failure to narrow --
      // reflect it in the name instead of falling back to the bare
      // generic product name, which looks like the narrowing did nothing
      // even when it correctly did.
      const codes = new Set(remainingVariants.map(v => extractShortCode(v)).filter(Boolean));
      displayName = codes.size === 1 ? `${productName} ${[...codes][0]!.replace(':', '.')}` : productName;
    }
    const sizesAvail = [...new Set(rows.map(r => r.size).filter((x): x is string => !!x))];
    const tiersAvail = [...new Set(rows.map(r => r.fabric_tier).filter((x): x is string => !!x))];

    // If every remaining row belongs to a single addon/surcharge variant
    // (not the main structure), make that explicit -- a full grid of
    // surcharge values with no context reads exactly like a real price
    // list otherwise, which is the confusing part.
    const addonGridNote = (remainingVariants.length === 1 && isAddonVariant(remainingVariants[0]))
      ? ` Note: these are add-on surcharge prices for "${remainingVariants[0]}", meant to be added to the base structure's price, not a standalone product price.`
      : '';

    // If the user explicitly asked for the full/complete price list, return
    // everything with a distinct status so the UI renders a proper
    // pivoted grid (sizes x tiers, like the real PDF page) instead of a
    // truncated one-line summary. NOT when a tier/colore filter above
    // already narrowed `rows` below the product's real full set, though --
    // "give all nairobi olive green price" contains "all" but `rows` is
    // only 3 of Nairobi's real 9 rows at that point, so labeling it "the
    // full price list" would be a real lie, not just an unhelpful one.
    // Falls through to the ordinary narrowed-result summary below
    // instead, same status ('multiple_options'/'ok') an equivalent query
    // without "all" wording already gets.
    if (wantsFullList && !tierWasExplicitlyFiltered) {
      return {
        status: 'full_price_grid',
        message: `Here's the full price list for "${displayName}":${ambiguousNote}${addonGridNote}`,
        product_name: productName,
        matches: rows,
        image_urls: this.getImageUrls(productName, brand, rows),
      };
    }

    // Otherwise summarize instead of dumping every row when there are too
    // many -- the full list still goes back in `matches` for the UI to
    // render as a picker/table if it wants to.
    const priceNums = rows
      .map(r => parseFloat(String(r.price_eur).replace(/[^\d.,]/g, '').replace('.', '').replace(',', '.')))
      .filter(n => !isNaN(n));
    const priceRange = priceNums.length
      ? `€${formatItalianNumber(Math.min(...priceNums))}–€${formatItalianNumber(Math.max(...priceNums))}`
      : null;

    let message: string;
    if (rows.length > 6) {
      message = `"${displayName}" has ${rows.length} price options matching your query`
        + (sizesAvail.length ? ` across sizes: ${sizesAvail.join(', ')}` : '')
        + (tiersAvail.length ? `, fabric tiers: ${tiersAvail.join(', ')}` : '')
        + (priceRange ? `. Prices range ${priceRange}.` : '.')
        + ' Tell me a specific size and/or fabric tier and I can give you the exact price, or ask me for "all prices" to see the full list.'
        + ambiguousNote + addonGridNote;
    } else {
      message = `Found ${rows.length} price options for "${displayName}" matching your query. Here they are, along with the catalog page:${ambiguousNote}${addonGridNote}`;
    }

    return {
      status: 'multiple_options',
      message,
      product_name: productName,
      matches: rows,
      image_urls: this.getImageUrls(productName, brand, rows),
    };
  }
}