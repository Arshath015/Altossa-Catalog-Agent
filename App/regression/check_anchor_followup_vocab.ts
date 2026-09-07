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
 *
 * SEDIA CON GAMBE / ANCHOR-BEFORE-BRAND-WIDE-SEARCH FIX (2026-09-07,
 * OVERRIDE_GATE_CASES "gate-bare-category-noun-repro..." below): a
 * DIFFERENT failure shape from the original Gamma repro above, found via
 * live browser testing, not log inspection. "give for sedia con gambe"
 * right after a Pianca "Esse" turn silently resolved to "Cora" (an
 * unrelated product) instead of staying anchored to Esse's own real "Con
 * gambe legno/metallo" category. Root cause was TWO layers, both fixed
 * together:
 *   1. catalogChatRoute.ts's brand-wide findByVariantPhrase pre-check ran
 *      BEFORE this file's anchor-vocab override gate (it used to be
 *      checked only right before the LLM step) -- findByVariantPhrase has
 *      no concept of lastProduct at all, so it unconditionally returned
 *      Cora's own verbatim-matching "Sedia con gambe" phrase before the
 *      anchor was ever consulted. Fixed by moving the gate (and the
 *      currentMessageNamesOwnProduct/effectiveLastProduct computation it
 *      needs) to run first.
 *   2. Once reordered, a SECOND bug surfaced: queryOnlySpecifiesAnchorProductDetails
 *      didn't recognize "sedia" as Esse's own vocabulary at all (Esse's
 *      real category text is "Con gambe legno/metallo" -- the word
 *      "sedia" never appears in any of its variant_context/model_variant
 *      values). Fixed via a NEW vocabulary source, getAnchorTitleBlock:
 *      every product's own raw source-text page title reliably states
 *      its real furniture-TYPE word in Italian ("ESSE di Philippe Tabet
 *      Sedia", "DOMINO Panche") even when that word never appears in its
 *      price-grid vocabulary -- confirmed across every "Sedia"-type
 *      product in this repro's own batch (Cora, Esse, Aria, Elide,
 *      Clelia, Alunna, Intro, Orchestra, Inari, Seida, Gamma all say
 *      "sedia"; Domino says "Panche", Forma says "Scrittoi" -- neither
 *      says "sedia").
 *   3. Fixing #2 alone still wasn't enough: matchProducts("give for sedia
 *      con gambe") returns a nonzero score for "Levante Out (Sedia)"/
 *      "Maestrale (Sedia)" purely because "sedia" is a literal substring
 *      of their own parenthetical category qualifier, even though neither
 *      name explains "con gambe" at all -- this file's OLD gate condition
 *      (`matchProducts(query).length === 0`, a bare emptiness check) still
 *      blocked the override on this WEAK, coincidental match. Fixed via a
 *      new `matchLeavesRealLeftover` method (same "how much of the query
 *      does this candidate leave unexplained" yardstick findByVariantPhrase's
 *      own override logic already uses) -- `evalOverrideGate` below and
 *      catalogChatRoute.ts's real gate both now require every competing
 *      matchProducts() candidate to be genuinely incomplete, not just
 *      merely-nonzero, before deferring to the anchor.
 *
 * TWO ALTERNATIVE FIXES WERE CONSIDERED AND REJECTED WITH EVIDENCE before
 * building this one (see the session's own investigation): (a) adding
 * "sedia" to the shared GENERIC_CATEGORY_WORDS list -- rejected because it
 * also feeds isIndexableVariantPhrase, which would have silently DROPPED
 * Cora's own "Sedia con gambe" phrase from variantPhraseIndex entirely
 * (contentWords count would fall below the >=2 indexability floor); (b) a
 * flat lowered similarity() threshold for anchor phrases -- rejected
 * because Domino (a writing desk) and Aria (an unrelated chair with no
 * "con gambe" variant) both scored identically (24) to Esse's own genuine
 * match on this exact query, confirmed via a live stress test, meaning
 * any threshold low enough to accept the real case also accepts these
 * false ones. The title-block + matchLeavesRealLeftover combination is
 * the only approach tested that resolves the genuine repro AND correctly
 * rejects both false positives -- see gate-closed-domino-false-positive-
 * not-a-chair and gate-closed-aria-false-positive-no-gambe-variant below.
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

/**
 * OVERRIDE_GATE_CASES -- permanent regression for the catalogChatRoute.ts
 * "deterministic anchor-vocab override" (added 2026-09-04) and its exact
 * motivating repro: "give gambe price" right after a Pianca "Esse" turn
 * returned a confident LlmIntent naming "Gamma" -- a real but entirely
 * unrelated product, zero textual grounding anywhere in the query. The
 * LLM's output had no structural signal marking it as wrong (a real,
 * valid product_names entry, normal status), so this can't be caught by
 * inspecting the LLM's answer after the fact -- the route now runs the
 * SAME 4-part guard checked here independently, BEFORE trusting any LLM
 * guess, whenever `!currentMessageNamesOwnProduct && effectiveLastProduct`
 * already hold (see catalogChatRoute.ts's own doc comment for the full
 * reasoning, including the accepted multi-turn-history blind spot this
 * does NOT close).
 *
 * Each case asserts the 4 gate conditions directly (matches what the
 * route computes) AND that CatalogChat.answer() -- what the override
 * calls when the gate opens -- actually resolves correctly, so this
 * fails loudly if either the gate's conditions or the underlying
 * resolution ever regress.
 */
interface OverrideGateCase {
  id: string;
  brand: string;
  query: string;
  anchor: string;
  expectGateOpen: boolean;
  expectedProductName?: string;
  expectMinRows?: number;
  note: string;
}

const OVERRIDE_GATE_CASES: OverrideGateCase[] = [
  {
    id: 'gate-gamma-repro-fires-and-resolves-to-esse',
    brand: 'Pianca',
    query: 'give gambe price',
    anchor: 'Esse',
    expectGateOpen: true,
    expectedProductName: 'Esse',
    expectMinRows: 1,
    note: 'The exact reported repro: a live LLM call confidently named "Gamma" for this query (a real, unrelated Pianca product, zero textual grounding for it in the query) -- confirmed "gambe" is real vocabulary on 8 different Pianca products (Domino, Forma, Alunna, Cora, Esse, Intro, Delta fisso, Esse Lounge), so the LLM had no way to deterministically prefer Esse either; only the anchor does. The gate must open (no competing name match, query fully explained by Esse\'s own vocabulary) and resolving via answer() must land on Esse, not Gamma or any other of those 8.',
  },
  {
    id: 'gate-closed-when-message-names-different-product',
    brand: 'Pianca',
    query: 'give me Cora price',
    anchor: 'Esse',
    expectGateOpen: false,
    note: 'Safety control: the message names a different, real product outright (matchProducts finds "Cora", and currentMessageNamesOwnProduct-equivalent detection also fires) -- the override must never engage here regardless of any stale anchor.',
  },
  {
    id: 'gate-closed-when-vocab-check-fails',
    brand: 'Pianca',
    query: 'give xyzqwerty price',
    anchor: 'Esse',
    expectGateOpen: false,
    note: 'Safety control mirroring guard-fuzzy-typo-tolerance-not-unbounded above: no competing name match, but the leftover word is genuinely unrelated to Esse\'s own vocabulary (not a bounded typo of it either) -- the gate must stay closed.',
  },
  {
    id: 'gate-bare-category-noun-repro-fires-and-resolves-to-esse',
    brand: 'Pianca',
    query: 'give for sedia con gambe',
    anchor: 'Esse',
    expectGateOpen: true,
    expectedProductName: 'Esse',
    expectMinRows: 1,
    note: 'Exact reported repro, a DIFFERENT failure shape from the Gamma case above: right after an Esse turn, this silently resolved to "Cora" (an unrelated product whose own real phrase is verbatim "Sedia con gambe") via catalogChatRoute.ts\'s brand-wide findByVariantPhrase pre-check, which has no concept of lastProduct at all and ran BEFORE this gate ever got a chance to fire. Also exposed a second, independent bug once the gate was moved earlier: matchProducts("give for sedia con gambe") returns a nonzero score for "Levante Out (Sedia)"/"Maestrale (Sedia)" purely because "sedia" is a literal substring of their own parenthetical category qualifier -- a WEAK match that explains none of the query\'s real content ("con gambe") -- so the OLD strict `matchProducts().length === 0` guard blocked the gate even after queryOnlySpecifiesAnchorProductDetails was fixed to recognize "sedia" via Esse\'s own page-title furniture-type word (getAnchorTitleBlock). Needs BOTH fixes at once: the route-level reordering (findByVariantPhrase no longer runs before this gate) AND matchLeavesRealLeftover distinguishing this weak echo from a genuine competing match.',
  },
  {
    id: 'gate-closed-domino-false-positive-not-a-chair',
    brand: 'Pianca',
    query: 'give for sedia con gambe',
    anchor: 'Domino',
    expectGateOpen: false,
    note: 'False-positive guard for the fix above: Domino is a real Pianca product with its own "con gambe" vocabulary ("Scrittoio autoportante con gambe metalliche" -- a writing desk, not a chair), so a flat lowered similarity threshold (the alternative fix considered and rejected) would have wrongly validated it here too (confirmed live: scored identically, 24, to Esse\'s own genuine match on this exact query). The title-block check must correctly reject it -- Domino\'s own page title says "Panche" (benches), never "sedia" -- so the gate stays closed and this falls through to the same brand-wide "Cora" result the no-anchor case gets, not a false "Domino" answer.',
  },
  {
    id: 'gate-closed-aria-false-positive-no-gambe-variant',
    brand: 'Pianca',
    query: 'give for sedia con gambe',
    anchor: 'Aria',
    expectGateOpen: false,
    note: 'Second false-positive guard: Aria genuinely IS a "Sedia" (its own page title confirms it, unlike Domino), so the title-block exemption alone would wrongly open this gate -- but Aria has no "con gambe" variant at all (its own real phrase is "Sedia con tappetino di seduta", a fixed-leg design with an optional seat mat), so "gambe" remains real, unexplained leftover even after "sedia" is exempted. Confirms the fix only exempts the ONE category-noun word, never bypasses the requirement that every OTHER real word still be explained by the anchor\'s own actual vocabulary.',
  },
  {
    id: 'gate-alunna-different-category-phrase-same-mechanism',
    brand: 'Pianca',
    query: 'give for sedia con braccioli',
    anchor: 'Alunna',
    expectGateOpen: true,
    expectedProductName: 'Alunna',
    expectMinRows: 1,
    note: 'Batch coverage across a different Pianca product/phrase pair, same bug family: standalone (no anchor) this phrase genuinely ties across 4 real products (Elide/Alunna/Inari (Sedie)/Orchestra), so an Alunna anchor must resolve it to Alunna specifically rather than defaulting to a catalog-wide tie or a different sibling.',
  },
  {
    id: 'gate-seida-real-tie-resolved-by-anchor',
    brand: 'Pianca',
    query: 'give for sedia con seduta imbottita',
    anchor: 'Seida',
    expectGateOpen: true,
    expectedProductName: 'Seida',
    expectMinRows: 1,
    note: 'Batch coverage: this exact phrase is a genuine verbatim tie between Lina and Seida with no anchor (confirmed elsewhere: findByVariantPhrase correctly returns clarify_product for it) -- a Seida anchor must resolve the tie in Seida\'s favor instead of still asking the user to disambiguate between two products when one was already established.',
  },
];

function callAnswer(brand: string, query: string, lastProduct: string | null = null): ChatResult {
  return getCc(brand).answer(query, brand, null, lastProduct, null);
}

function evalOverrideGate(brand: string, query: string, anchor: string): boolean {
  const cc = getCc(brand);
  const namesOwnProduct = cc.detectNamedProductsInText(query).length > 0;
  // Mirrors catalogChatRoute.ts's actual gate condition (updated
  // 2026-09-07, see the "sedia con gambe" cases below) -- NOT a bare
  // `matchProducts(query).length === 0` any more: that was too strict,
  // since a WEAK/coincidental match (matchLeavesRealLeftover === true)
  // must not block the override, only a genuine one should.
  const noGenuineCompetingMatch = !cc.matchProducts(query).some(m => !cc.matchLeavesRealLeftover(query, m.name));
  const vocabExplained = cc.queryOnlySpecifiesAnchorProductDetails(query, anchor);
  return !namesOwnProduct && noGenuineCompetingMatch && vocabExplained;
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

  for (const c of OVERRIDE_GATE_CASES) {
    const gateOpen = evalOverrideGate(c.brand, c.query, c.anchor);
    let ok = gateOpen === c.expectGateOpen;
    let detail = `gateOpen=${gateOpen}`;
    if (ok && c.expectGateOpen) {
      const resp = callAnswer(c.brand, c.query, c.anchor);
      const rows = (resp.matches || []).length;
      const resolvedOk = resp.product_name === c.expectedProductName && rows >= (c.expectMinRows ?? 1);
      ok = resolvedOk;
      detail += `  status=${resp.status}  product=${resp.product_name}  rows=${rows}`;
      if (!resolvedOk) {
        failures.push(`[${c.id}] "${c.query}" (anchor=${c.anchor}) -- gate opened correctly but answer() resolved to product_name=${resp.product_name} rows=${rows}, expected "${c.expectedProductName}" with >=${c.expectMinRows ?? 1} rows. ${c.note}`);
      }
    }
    if (gateOpen !== c.expectGateOpen) {
      failures.push(`[${c.id}] "${c.query}" (anchor=${c.anchor}) -- expected gate ${c.expectGateOpen ? 'OPEN' : 'CLOSED'}, got ${gateOpen ? 'OPEN' : 'CLOSED'}. ${c.note}`);
    }
    console.log(`[${c.id.padEnd(36)}] ${ok ? 'ok' : 'FAIL'}  ${detail}`);
  }

  console.log('\n' + '='.repeat(70));
  console.log(`Total cases: ${CASES.length + GUARD_CASES.length + REJECT_CASES.length + OVERRIDE_GATE_CASES.length}`);
  console.log(`Failures: ${failures.length}  <-- must be 0`);
  if (failures.length > 0) {
    console.log('\nFAILURES:');
    failures.forEach(f => console.log(`  ${f}`));
  }

  if (failures.length > 0) {
    console.log('\nEXIT 1: anchor follow-up vocabulary regressed.');
    process.exit(1);
  }
  console.log('\nEXIT 0: anchor follow-up vocabulary recognizes short real-variant words, tier_label words, Italian connector words, variant_context text, and scaled-typo near-misses -- the anchor-over-trust / unbounded-fuzzy-match guards still hold -- and the route-level anchor-vocab override gate (Gamma repro) opens/closes correctly.');
}

main();
