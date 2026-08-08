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
  'stand', 'mirror', 'lounge', 'coffee', 'wood', 'tv',
]);

/** Simple token-overlap similarity score between a query and a candidate name. Higher = better match. */
export function similarity(query: string, candidate: string): number {
  const q = normalize(query);
  const c = normalize(candidate);
  if (q === c) return 100;
  const qTokens = q.split(/\s+/).filter(Boolean);
  const cTokens = c.split(/\s+/).filter(Boolean);
  // Token-SET containment (every token on one side appears on the other,
  // any order) -- not a contiguous-phrase check. That distinction matters:
  // "cuff pouf" isn't a substring of "Cuff bench and pouf" (words in
  // between), so a phrase-only check let it fall to the diluted overlap
  // score below while bare "Cuff" kept a flat win. Tying both at 80 instead
  // hands resolution to the existing "maximal" tie-break in answer().
  if (qTokens.length > 0 && qTokens.every(t => cTokens.includes(t))) return 80;
  if (cTokens.length > 0 && cTokens.every(t => qTokens.includes(t))) return 80;
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
  if (overlap.every(t => GENERIC_CATEGORY_WORDS.has(t))) {
    const leftover = qTokens.filter(t => !overlap.includes(t) && !cTokens.includes(t) && !RISKY_SIZE_CODE_WORDS.has(t));
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
    const n = normalize(name);
    if (n) q = q.replace(new RegExp(escapeRegex(n), 'gi'), ' ');
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

  /** @param dataDir folder containing catalog_index.json and prices.json for one brand */
  constructor(private dataDir: string) {
    this.catalogIndex = JSON.parse(fs.readFileSync(path.join(dataDir, 'catalog_index.json'), 'utf-8'));
    this.prices = JSON.parse(fs.readFileSync(path.join(dataDir, 'prices.json'), 'utf-8'));
    this.productNames = [...new Set(this.catalogIndex.map(p => p.product_name))];
    this.realTierPhrases = [...new Set(this.prices.map(r => r.fabric_tier).filter((t): t is string => !!t))]
      .sort((a, b) => normalize(b).length - normalize(a).length);
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
   * (GLOBE/glove, PRIVE/price) already rejected above. */
  private excludeUnrelatedAndClause(rawQuery: string, productName: string): { scopedQuery: string; excludedClause: string | null } {
    const segments = rawQuery.split(/\band\b/gi).map(s => s.trim()).filter(Boolean);
    if (segments.length <= 1) return { scopedQuery: rawQuery, excludedClause: null };
    const own = segments.filter(seg => containsWholeWord(normalize(seg), normalize(productName)));
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
        name,
        result: this.answerFromIntent(name, scopedSize, scopedTierArray.length > 0 ? scopedTierArray : null, scopedQuery, brand, null, wantsFullList),
      };
    });
    const combinedMatches = perProduct.flatMap(p => p.result.matches || []);
    const combinedImages = [...new Set(perProduct.flatMap(p => p.result.image_urls || []))];
    const notFoundLines = unresolvedMentions.map(n => `${n}: couldn't find a matching product in the catalog.`);
    const message = [...perProduct.map(p => `${p.name}: ${p.result.message}`), ...notFoundLines].join('\n\n');

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

  /**
   * Main entry point for pure deterministic matching (no LLM). Returns a
   * structured result -- never a bare string -- so the caller can decide
   * exactly how to present it, including whether to show a screenshot and
   * whether to flag ambiguity.
   */
  answer(query: string, brand: string, lastModelVariant: string | null = null, lastProduct: string | null = null): ChatResult {
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
      return {
        status: 'no_product_match',
        message: "I couldn't find a product matching that in the catalog. Could you check the spelling or try the product's full name?",
      };
    }

    const topScore = matches[0].score;
    const topMatches = matches.filter(m => m.score >= topScore - 5).slice(0, 5);

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
      const nameTokens = (s: string) => normalize(s).split(/\s+/).filter(Boolean);
      const maximal = topMatches.filter(m => {
        const mTokens = nameTokens(m.name);
        return topMatches.every(other => {
          if (other.name === m.name) return true;
          const otherTokens = nameTokens(other.name);
          return otherTokens.length > 0 && otherTokens.every(t => mTokens.includes(t));
        });
      });
      if (maximal.length !== 1) {
        return {
          status: 'clarify_product',
          message: `I found a few products that could match: ${topMatches.map(m => m.name).join(', ')}. Which one did you mean?`,
          candidates: topMatches.map(m => m.name),
        };
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
  answerFromIntent(
    productNameGuess: string | null,
    size: string | null,
    tier: string | string[] | null,
    rawQuery: string,
    brand: string,
    lastModelVariant: string | null = null,
    wantsFullList: boolean = false
  ): ChatResult {
    const validProductName = productNameGuess && this.productNames.includes(productNameGuess)
      ? productNameGuess
      : null;

    if (!validProductName) {
      // LLM guess missing or not a real product -- fall back to the
      // tested deterministic matcher on the raw text instead of guessing.
      return this.answer(rawQuery, brand, lastModelVariant);
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
    const { scopedQuery, excludedClause } = this.excludeUnrelatedAndClause(rawQuery, validProductName);
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
      this.lookupForProduct(validProductName, effectiveSize, normalizedTiers, brand, scopedQuery, lastModelVariant, wantsFullList),
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
    wantsFullList: boolean = false
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
      return this.answerFromIntent(validNames[0] || null, size, tier, rawQuery, brand, lastModelVariant, wantsFullList);
    }
    if (validNames.length === 0) {
      // Nothing resolved at all -- defer to the deterministic matcher's
      // own no-match/clarify handling rather than building an empty
      // multi-product shell around pure LLM noise.
      return this.answer(rawQuery, brand, lastModelVariant);
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

    const resolved = new Set<string>();
    for (const requested of requestedTiers) {
      const reqNorm = normalize(requested);
      if (availableTiers.includes(reqNorm)) {
        resolved.add(reqNorm);
        continue;
      }
      // partial match: requested is a prefix/whole-word piece of a real tier
      // (e.g. "b" -> "b e tcl"), or a real tier is contained in the request.
      // For very short requests (1-2 chars), skip the loose "appears
      // anywhere" substring check -- e.g. a bare "e" could otherwise
      // incorrectly match "extra" (which starts with "e") on some future
      // catalog that doesn't also have a standalone "e" tier to win the
      // exact-match check above first.
      const partial = availableTiers.find(t =>
        t.startsWith(reqNorm) || reqNorm.startsWith(t) || (reqNorm.length >= 3 && t.includes(reqNorm))
      );
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
      if (requestedNums.length > 0) {
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
        if (sizeFiltered.length === 0 && codeMatchesSomething) {
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
      const bareNums = (rawQueryHint.match(/\b\d{2,3}\b/g) || []).map(Number).sort((a, b) => a - b);
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
    let queryMinusProductName = normalize(productName)
      ? normalizedRawQuery.replace(new RegExp(escapeRegex(normalize(productName)), 'gi'), ' ')
      : normalizedRawQuery;
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
    const rawTierHits = realTierValues.filter(t => containsWholeWord(queryMinusProductName, normalize(t)));
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
      const qWords = new Set(
        qNorm.replace(normalize(productName), '').split(/[^a-z0-9]+/).filter(w => w.length >= 4)
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
            const suffix = normalize(v).split(normalize(productName)).join('').trim();
            const suffixWords = suffix.split(/[^a-z0-9]+/).filter(w => w.length >= 4);
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
          const suffix = normalize(v).split(normalize(productName)).join('').trim();
          if (!suffix) continue;
          if (qStripped.includes(strip(suffix))) {
            compactMatches.push(v);
            continue;
          }
          const suffixWords = suffix.split(/[^a-z0-9]+/).filter(w => w.length >= 4);
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

      if (matched && matchingVariant) {
        rows = rows.filter(r => r.model_variant === matchingVariant);
      } else if (!freshHeightSignal && !explicitProductRebroaden && lastModelVariant) {
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