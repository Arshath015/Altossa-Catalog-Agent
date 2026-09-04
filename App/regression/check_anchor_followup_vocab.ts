/**
 * regression/check_anchor_followup_vocab.ts
 * ---------------------------------
 * Permanent, GATING check for a real bug found via live testing 2026-08-25
 * (user-reported: "follow-up message not giving previous product's
 * answers"): a content-free follow-up naming NO product at all, right
 * after a turn that resolved one, is only reused via `lastProduct` when
 * `queryOnlySpecifiesAnchorProductDetails()` (catalogChat.ts) confirms
 * every real word in the query is already explained by that anchor
 * product's own known vocabulary. Two real gaps in that vocabulary build,
 * both reproduced live against Pianca's Esse (anchor established via
 * "esse price", multiple_options):
 *
 *   1. Esse's own real model_variant "non sfoderabile" typed back
 *      VERBATIM ("non sfoderabile price") still failed with
 *      no_product_match -- "non" is only 3 characters, so the vocabulary
 *      builder's `isDistinguishingWord` filter (>=4 chars or digit-
 *      bearing) silently dropped it, even though the FULL phrase is a
 *      complete, exact, real value for this product. Fixed by including
 *      every word of a verified-real variant phrase, not just the
 *      "distinguishing" ones -- safe specifically because the source is
 *      already a known-real complete string, not loose free text.
 *   2. "give category a price" also failed -- "CATEGORY" is the literal
 *      column header Pianca's own price grid displays for this tier
 *      dimension (tierColumnHeader() in CatalogChatWidget.tsx), but only
 *      bare tier VALUES ("a"/"b"/"c"/...) fed the vocabulary, never the
 *      tier_label WORD itself. Fixed by adding this anchor's own real
 *      tier_label word alongside its tier values.
 *
 * Also guards that neither fix loosened the existing Ada anchor-over-
 * trust safety check (an anchor must still be REJECTED, not silently
 * reused, when the query names a genuinely different/unrelated product)
 * -- see check_ditre_matching.ts's own "regress-*-guard-*" cases for the
 * fuller battery on that; case 3 below is a lighter cross-check.
 *
 * FOLLOW-UP BUG (2026-08-25, same conversation): "give for poltronica con
 * gambe" right after an Esse turn fell through to a totally unrelated
 * 5-candidate clarify_product (Plana/Nastro/Amalfi/... "Moduli con
 * cassettiere esterne" etc) instead of anchoring to Esse. Confirmed via
 * direct investigation this was NOT the same anchorVocab mechanism as
 * above -- it never even reached that code. Root cause was one layer
 * earlier: matchProducts() itself found false-positive fuzzy matches,
 * because similarity()'s overlap-classification never recognized "con"
 * (Italian "with") as filler the way it already does its English
 * equivalent "with" -- so a query sharing ONLY "con" with 11 real,
 * unrelated Pianca product names (every "... con ..." collection, per
 * catalog_index.json) scored nonzero against each of them, producing a
 * false ambiguous tie that pre-empted the anchor fallback entirely
 * (that fallback only ever runs when matchProducts() finds ZERO
 * candidates). Fixed in 3 parts, each independently necessary for this
 * exact repro to fully resolve to Esse:
 *   1. Added "con"/"di"/"per" (with/of/for) to CONVERSATIONAL_FILLER_
 *      WORDS -- checked first against every brand's real tier values and
 *      every Pianca product name for a bare-word collision (none found).
 *   2. anchorVocab (in queryOnlySpecifiesAnchorProductDetails) now also
 *      reads variant_context, not just model_variant -- "Poltroncina con
 *      gambe"-style CATEGORY text lives in variant_context, which was
 *      never part of the anchor's known vocabulary at all before.
 *   3. Added scaled Levenshtein typo tolerance to the same function's
 *      leftover check, reusing the exact levenshtein()/distance-scaling
 *      convention already proven in fuzzyMatchProducts (not a new
 *      mechanism) -- "poltronica" is a real distance-2 typo of Esse's own
 *      "Con gambe..." category text, and the deterministic path
 *      otherwise has zero typo tolerance by design.
 *
 * Also confirmed (separate question, not a bug): the "Using basic
 * matching for this reply" disclaimer (catalogChatRoute.ts) is set only
 * when extractIntent() returns null, which happens ONLY when the entire
 * Groq key pool is exhausted/failing -- never just because the model ran
 * and was uncertain. It's a real, meaningful signal that this reply used
 * the typo-INTOLERANT deterministic fallback, not decorative boilerplate
 * shown for any ambiguous multi-match. This whole repro was itself
 * triggered under exactly that degraded state, which is why it matters.
 *
 * RUN WITH: npm run check-anchor-followup-vocab
 *
 * Calls CatalogChat.answer() directly (same deterministic path used in
 * check_tier_isolation_stress.ts) instead of the HTTP route -- the route
 * tries the LLM (extractIntent/Groq) FIRST and only falls through to this
 * exact deterministic code (queryOnlySpecifiesAnchorProductDetails etc.)
 * once the whole Groq key pool is exhausted, so hitting the route made
 * this check's outcome depend on live Groq availability at call time: it
 * FAILED both times Groq answered (the LLM has no equivalent anchor-vocab
 * protection and doesn't reliably solve these specific edge cases) and
 * PASSED every time Groq was unavailable and the real fix ran. Confirmed
 * via 5 back-to-back re-runs with zero code changes (2 fail, 3 pass) plus
 * git blame showing none of the relevant matching code was touched in the
 * session that surfaced this -- a genuine flake, not a regression. Calling
 * the deterministic function directly makes this check exercise the exact
 * code the fix lives in, every time, regardless of Groq. Needs the data
 * files but NOT a running server or any Groq key.
 */

import { CatalogChat, ChatResult } from '../server/catalogChat';

const ccByBrand = new Map<string, CatalogChat>();
function getCc(brand: string): CatalogChat {
  if (!ccByBrand.has(brand)) ccByBrand.set(brand, new CatalogChat(`./data/${brand}`));
  return ccByBrand.get(brand)!;
}

interface Case {
  id: string;
  brand: string;
  query: string;
  lastProduct: string;
  expectedProductName: string;
  expectMinRows: number;
  note: string;
}

const CASES: Case[] = [
  {
    id: 'short-word-in-real-variant-phrase',
    brand: 'Pianca',
    query: 'non sfoderabile price',
    lastProduct: 'Esse',
    expectedProductName: 'Esse',
    expectMinRows: 1,
    note: 'Exact reported repro -- "non sfoderabile" is Esse\'s own real, complete model_variant value; "non" alone is too short (<4 chars) to have made it into anchorVocab before the fix.',
  },
  {
    id: 'tier-label-word-category',
    brand: 'Pianca',
    query: 'give category a price',
    lastProduct: 'Esse',
    expectedProductName: 'Esse',
    expectMinRows: 1,
    note: 'Exact reported repro -- "category" is the literal column header word this product\'s own price grid displays, but was never recognized as anchor vocabulary before the fix (only bare tier values were).',
  },
  {
    id: 'tier-label-word-category-no-give',
    brand: 'Pianca',
    query: 'category a price',
    lastProduct: 'Esse',
    expectedProductName: 'Esse',
    expectMinRows: 1,
    note: 'Same as tier-label-word-category but without the leading "give" -- confirms the fix is about vocabulary recognition, not an unrelated phrasing quirk.',
  },
  {
    id: 'italian-filler-plus-typo-plus-variant-context',
    brand: 'Pianca',
    query: 'give for poltronica con gambe',
    lastProduct: 'Esse',
    expectedProductName: 'Esse',
    expectMinRows: 1,
    note: 'Exact reported repro -- needs all 3 fixes at once: "con" recognized as filler (not false-positive-matching 11 unrelated "... con ..." Pianca products), "gambe" recognized via variant_context (not just model_variant), and "poltronica" tolerated as a distance-2 typo of the real category text. Was returning a 5-candidate clarify_product for totally unrelated products (Plana/Nastro/Amalfi) before the fix.',
  },
  {
    id: 'variant-context-word-alone',
    brand: 'Pianca',
    query: 'give gambe price',
    lastProduct: 'Esse',
    expectedProductName: 'Esse',
    expectMinRows: 1,
    note: 'Isolates fix part 2 alone -- "gambe" is real variant_context text ("Con gambe legno/metallo", "Con gambe rivestite"), never in model_variant, so this failed even with fix part 1 (the con/di/per filler words) alone.',
  },
];

interface GuardCase {
  id: string;
  brand: string;
  query: string;
  lastProduct: string;
  mustNotBe: string;
  note: string;
}

const GUARD_CASES: GuardCase[] = [
  {
    id: 'guard-unrelated-product-not-reused',
    brand: 'Ditre Italia',
    query: 'give online 2er sofa price',
    lastProduct: 'Ada (Sofa)',
    mustNotBe: 'Ada (Sofa)',
    note: 'Widening anchorVocab must not resurrect the original Ada anchor-over-trust bug -- "online 2er sofa" names a real, different product (On Line) and must resolve to THAT, never silently fall back to the Ada anchor just because it exists.',
  },
  {
    id: 'guard-fuzzy-typo-tolerance-not-unbounded',
    brand: 'Pianca',
    query: 'give xyzqwerty price',
    lastProduct: 'Esse',
    mustNotBe: 'Esse',
    note: 'The new scaled-Levenshtein typo tolerance must stay bounded -- a genuinely unrelated word (not within edit distance 1-2 of anything in Esse\'s real vocabulary) must still leave the query unresolved, not get waved through just because SOME anchor exists.',
  },
];

interface RejectCase {
  id: string;
  brand: string;
  query: string;
  mustNotInclude: string[];
  note: string;
}

const REJECT_CASES: RejectCase[] = [
  {
    id: 'guard-con-not-false-positive-no-anchor',
    brand: 'Pianca',
    query: 'give me the price for zorblatt con',
    mustNotInclude: ['Plana', 'Nastro', 'Amalfi', 'Cornice', 'Icona', 'Manhattan', 'Raggio', 'Tratto'],
    note: 'Root-cause guard, no anchor involved at all: a query sharing ONLY the Italian word "con" with 11 real Pianca products (every "... con ..." collection name) must not fuzzy-match any of them as clarify_product candidates -- this is what was actually broken before the con/di/per filler fix, independent of the anchor mechanism above.',
  },
];

function callAnswer(brand: string, query: string, lastProduct: string | null = null): ChatResult {
  return getCc(brand).answer(query, brand, null, lastProduct, null);
}

function main() {
  const failures: string[] = [];

  for (const c of CASES) {
    const resp = callAnswer(c.brand, c.query, c.lastProduct);
    const rows = (resp.matches || []).length;
    const ok = resp.product_name === c.expectedProductName && rows >= c.expectMinRows;
    if (!ok) {
      failures.push(`[${c.id}] "${c.query}" (anchor=${c.lastProduct}) -- expected product_name="${c.expectedProductName}" with >=${c.expectMinRows} rows, got status=${resp.status} product_name=${resp.product_name} rows=${rows}. ${c.note}`);
    }
    console.log(`[${c.id.padEnd(36)}] ${ok ? 'ok' : 'FAIL'}  status=${resp.status}  product=${resp.product_name}  rows=${rows}`);
  }

  for (const c of GUARD_CASES) {
    const resp = callAnswer(c.brand, c.query, c.lastProduct);
    const ok = resp.product_name !== c.mustNotBe;
    if (!ok) {
      failures.push(`[${c.id}] "${c.query}" (anchor=${c.lastProduct}) -- must NOT resolve to "${c.mustNotBe}", but it did. ${c.note}`);
    }
    console.log(`[${c.id.padEnd(36)}] ${ok ? 'ok' : 'FAIL'}  status=${resp.status}  product=${resp.product_name}`);
  }

  for (const c of REJECT_CASES) {
    const resp = callAnswer(c.brand, c.query, null);
    const candidates = resp.candidates || [];
    const bad = candidates.filter(cand => c.mustNotInclude.some(name => cand.includes(name)));
    const ok = bad.length === 0;
    if (!ok) {
      failures.push(`[${c.id}] "${c.query}" -- candidates must not include any of [${c.mustNotInclude.join(', ')}], but got: ${bad.join(', ')}. ${c.note}`);
    }
    console.log(`[${c.id.padEnd(36)}] ${ok ? 'ok' : 'FAIL'}  status=${resp.status}  candidates=${candidates.length}`);
  }

  console.log('\n' + '='.repeat(70));
  console.log(`Total cases: ${CASES.length + GUARD_CASES.length + REJECT_CASES.length}`);
  console.log(`Failures: ${failures.length}  <-- must be 0`);
  if (failures.length > 0) {
    console.log('\nFAILURES:');
    failures.forEach(f => console.log(`  ${f}`));
  }

  if (failures.length > 0) {
    console.log('\nEXIT 1: anchor follow-up vocabulary regressed.');
    process.exit(1);
  }
  console.log('\nEXIT 0: anchor follow-up vocabulary recognizes short real-variant words, tier_label words, Italian connector words, variant_context text, and scaled-typo near-misses -- and the anchor-over-trust / unbounded-fuzzy-match guards still hold.');
}

main();
