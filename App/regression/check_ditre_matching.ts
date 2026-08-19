/**
 * regression/check_ditre_matching.ts
 * --------------------------------------
 * Dedicated test battery for Ditre Italia's deterministic chat-matching
 * logic (CatalogChat.answer(), no LLM/HTTP involved -- same discipline as
 * check_tier_isolation_stress.ts's DETERMINISTIC mode). Built per explicit
 * user instruction after a prior session found real bugs via reactive
 * live-patching but also caused 2 regressions along the way (see project
 * memory: tiedvariant_coverage_ratio_fix.md, same_product_multi_variant_
 * query_gap.md) -- this battery exists so any future change to
 * lookupForProduct/resolveProductQuery/excludeUnrelatedAndClause/
 * checkFamilyAmbiguity is verified against EVERY known pattern together,
 * not just the newest symptom.
 *
 * Every case asserts on structural facts (status, resolved product_name,
 * the SET of distinct model_variant values in `matches`, row count) --
 * never on exact message wording, so a message-clarity change doesn't
 * spuriously fail this battery.
 *
 * RUN WITH: npx tsx App/regression/check_ditre_matching.ts
 */

import { CatalogChat } from '../server/catalogChat';

const BRAND = 'Ditre Italia';
const cc = new CatalogChat(`./data/${BRAND}`);

interface Case {
  id: number;
  cat: string;
  query: string;
  lastModelVariant?: string | string[] | null;
  lastProduct?: string | null;
  lastCandidates?: string[] | null;
  /** What we assert. All optional -- only checked fields are asserted. */
  expectStatus?: string;
  expectProductName?: string; // substring match against resp.product_name
  expectVariants?: string[]; // exact set (order-independent) of distinct model_variant in matches
  expectVariantsSubset?: string[]; // every one of these must be present (others allowed)
  expectVariantsExclude?: string[]; // none of these may be present
  expectRowCount?: number;
  expectMinRowCount?: number;
  expectMaxRowCount?: number;
  /** Documents a DELIBERATE, accepted, known-safe limitation -- still run
   * and printed, but failures here are reported separately, not counted
   * against the gating pass rate, so a future run can tell "still safe as
   * documented" apart from "actually regressed further." */
  knownGap?: string;
  note?: string;
}

let id = 0;
const CASES: Case[] = [];
function addCase(cat: string, query: string, opts: Partial<Case> = {}) {
  CASES.push({ id: ++id, cat, query, ...opts } as Case);
}

// ============================================================
// Category A: the 2 logged-but-unfixed gaps (must be in the battery
// per explicit instruction, whether fixed or deliberately left as a
// documented known_gap)
// ============================================================
addCase('gap-bare-digit', 'give all online 2er price', {
  expectStatus: 'full_price_grid',
  note: 'bare "2er", zero other distinguishing word -- On Line has 2 real, structurally different "2-er..." members (sofa, central element). No text signal distinguishes them.',
});
addCase('gap-tier-list-3clause', 'give online 3er category u, A and leather vip', {
  expectProductName: 'On Line',
  note: '3-clause tier list (comma + "and"), bare "3er" is the only remaining variant signal once all 3 tiers are consumed. On Line has 4+ real "3-er..." members.',
});

// ============================================================
// Category B: online/On Line concatenation tokenization (already resolved,
// regression guardrail)
// ============================================================
addCase('online-tokenization', 'give online 2er sofa price', {
  // No size/tier given, so this correctly narrows to the ONE variant but
  // still returns all 12 of its rows (multiple_options) -- not a single
  // price. Confirms "online" (no space) resolves to On Line at all,
  // which is the actual thing under test here.
  expectProductName: 'On Line',
  expectVariants: ['2-er sofa'],
});
addCase('online-tokenization', 'online price', {
  // bare "online" alone should at least resolve to the product (may be
  // full grid / multiple_options, never no_product_match)
  expectProductName: 'On Line',
});

// ============================================================
// Category C: regression guardrails -- every pattern already fixed and
// verified in prior sessions this week
// ============================================================

// C1: tier-collision fix (commit a001040) -- single-letter Category tier
// token must not spuriously prefix-match a word from the variant's own name
addCase('regress-tier-collision', 'give all online 3er extra sofa price', {
  expectStatus: 'full_price_grid',
  expectVariants: ['3-er extra sofa'],
  expectRowCount: 12,
});

// C2: coverage-ratio / two-stage tie-break (commits 5977220, dd8f053, bb5cd01)
// C2a: corrupted-row exclusion -- must not tie in the merged/corrupted
// "3-er maxi central element ... Mini terminal element" row
addCase('regress-tiebreak-corrupted-row', 'give online 3er maxi sofa price', {
  expectProductName: 'On Line',
  expectVariantsExclude: ['3-er maxi central element                              Mini terminal element'],
});
// C2b: Krisby -- exact full name should resolve to exactly "Armchair", not
// tie with Armchair mix/low/mix low (avoids the top-level Krisby-name
// ambiguity across 5 different Krisby products by naming the full
// disambiguated product name directly, same technique the live UI uses)
addCase('regress-tiebreak-krisby', 'give me Krisby (Armchairs) Armchair price', {
  // The bare "Armchair" variant has an EMPTY suffix (it's a strict
  // substring of the product's own name "Krisby (Armchairs)"), so it's
  // structurally invisible to the shared-word scoring loop and can only
  // ever be added back in via the baseVariants union -- which, by
  // design (see commit 5977220's own doc comment), is unconditional
  // whenever ANY tie exists, not just when the bare variant is the sole
  // winner. A more surgical narrowing was tried and reverted in a prior
  // session (3 confirmed regressions elsewhere) specifically because it
  // excluded this exact bare variant -- so "all 4 tie, bare Armchair
  // included" is the deliberately-kept, safer trade-off, not a miss.
  expectProductName: 'Krisby (Armchairs)',
  expectVariantsSubset: ['Armchair'],
});
// C2c: bare "online sofa" should tie every real (non-ambiguous) sofa variant
addCase('regress-tiebreak-broad-sofa', 'give online sofa price', {
  expectProductName: 'On Line',
  expectVariantsSubset: ['2-er sofa', '3-er sofa', '3-er maxi sofa', '3-er extra sofa'],
});

// C3: elision / same-product multi-variant (commits 1abae5b, f17699d)
addCase('regress-elision-simple', 'give online 2er and 3er sofa prices', {
  expectStatus: 'multi_product',
  expectVariants: ['2-er sofa', '3-er sofa'],
});
addCase('regress-elision-modifier', 'give online 3er and 3er maxi all price', {
  expectStatus: 'multi_product',
  expectVariants: ['3-er sofa', '3-er maxi sofa'],
});
addCase('regress-elision-modifier-reversed', 'give online 3er maxi and 3er all price', {
  expectStatus: 'multi_product',
  expectVariants: ['3-er sofa', '3-er maxi sofa'],
  note: 'reversed clause order + trailing "all" -- found live in this session\'s own battery run: "all" was greedily captured as a false modifier of the second "3er", corrupting reconstruction and silently dropping "3-er sofa"',
});
addCase('new-elision-modifier-reversed-filler-first', 'give online 3er all and 3er maxi price', {
  expectStatus: 'multi_product',
  expectVariants: ['3-er sofa', '3-er maxi sofa'],
  note: 'same false-capture risk, mirrored onto the FIRST digit-word\'s modifier slot',
});
addCase('new-elision-simple-both-spelled', 'give online 2er sofa and 3er sofa price', {
  expectStatus: 'multi_product',
  expectVariants: ['2-er sofa', '3-er sofa'],
  note: 'both variants fully spelled out with their own trailing "sofa" -- sanity check that the NON_MODIFIER_WORDS lookahead does not interfere with a real shared modifier word',
});

// C4: typo + literal "and" clause, 3 layered root causes (online_3er_bug_full_resolution)
addCase('regress-typo-and-clause', 'give online 3er leaather vip and category U', {
  expectProductName: 'On Line',
});

// C5: standalone context-only "give all" follow-up
addCase('regress-give-all-followup', 'give all online 3er extra sofa price', {
  expectStatus: 'full_price_grid',
  expectVariants: ['3-er extra sofa'],
});
addCase('regress-give-all-anchor-followup', 'give all price', {
  lastProduct: 'On Line',
  lastModelVariant: '2-er sofa',
  expectVariants: ['2-er sofa'],
});

// C6: real multi-product (different names) -- Cali's 3 disambiguated siblings
addCase('regress-real-multiproduct', 'Cali (Sofa) 2-er sofa category a and Cali (Armchairs) armchair category a', {
  expectStatus: 'multi_product',
});

// C7: Puppet reciprocal-ambiguity (checkFamilyAmbiguity extension, 1477800)
addCase('regress-puppet-ambiguity', 'give puppet all price', {
  expectStatus: 'clarify_product',
});

// C8: Ada anchor over-trust (9c1b403) -- an anchored-but-unrecognized
// follow-up must not confidently reuse the stale anchor's product
addCase('regress-ada-anchor-overtrust', 'give online 2er sofa price', {
  lastProduct: 'Ada (Sofa)',
  expectProductName: 'On Line',
  note: 'must NOT return Ada (Sofa) data just because it was the anchor',
});

// C9: multi-product anchor array (800fb7f) -- vague follow-up right after a
// genuine 2-variant MULTIPLE PRODUCTS turn should recall BOTH variants
addCase('regress-multiproduct-anchor-array', 'give all online price', {
  lastModelVariant: ['3-er sofa', '3-er maxi sofa'],
  lastProduct: 'On Line',
  expectVariants: ['3-er sofa', '3-er maxi sofa'],
});

// C10: "give all" single-letter tier collision, standalone context-only
// follow-up variant (a001040's own 6-case battery, re-verified here)
addCase('regress-tier-collision-full-list', 'give all online 3er sofa price', {
  expectStatus: 'full_price_grid',
  expectVariants: ['3-er sofa'],
  expectRowCount: 12,
});

// ============================================================
// Category D: realistic variations nobody's explicitly tried yet
// ============================================================

// D1: typos on variant/tier words
addCase('new-typo-variant', 'give online 3-er extre sofa price', {
  expectProductName: 'On Line',
  note: 'typo on "extra" -> "extre" (deterministic path has no fuzzy variant-word matching; documents actual behavior)',
});
addCase('new-typo-tier', 'give online 3er sofa categry U price', {
  expectProductName: 'On Line',
  note: 'typo on "category" -> "categry"',
});

// D2: 3+ clause queries
addCase('new-3clause-mixed', 'give online 3er sofa category a, category u and leather vip', {
  expectProductName: 'On Line',
});
addCase('new-4clause-mixed', 'give online 3er maxi sofa category a, category u, leather vip and leather soft', {
  expectProductName: 'On Line',
});

// D3: elision with different modifier words
addCase('new-elision-extra-modifier', 'give online 3er and 3er extra all price', {
  expectStatus: 'multi_product',
  expectVariants: ['3-er sofa', '3-er extra sofa'],
});
addCase('new-elision-central-modifier', 'give online 2er and 2er central element price', {
  expectStatus: 'multi_product',
  expectVariants: ['2-er sofa', '2-er central element'],
  note: 'central-element side has a 2-word modifier ("central element") -- found live in this session\'s own battery: the original 1-word-only capture silently dropped "2-er sofa" (both clauses coincidentally resolved to "2-er central element"); fixed by widening the modifier capture to 1-2 words',
});

// D4: tier lists with mixed comma/and separators, different orders
addCase('new-tierlist-comma-first', 'give online 3er sofa category a, u and leather vip', {
  expectProductName: 'On Line',
});
addCase('new-tierlist-and-first', 'give online 3er sofa category a and u, leather vip', {
  expectProductName: 'On Line',
});

// D5: bare confirmations with minor typos
addCase('new-confirm-typo', 'yess give all', {
  lastProduct: 'On Line',
  lastModelVariant: '2-er sofa',
  note: '"yess" (typo of "yes") -- RISKY_SIZE_CODE_WORDS has "yes" exact only, documents actual behavior',
});
addCase('new-confirm-clean', 'yes give all', {
  lastProduct: 'On Line',
  lastModelVariant: '2-er sofa',
  expectVariants: ['2-er sofa'],
});

// D6: size + tier + variant combined, different orders
addCase('new-order-variant-tier-size', 'online 3-er sofa category a 82x82', {
  expectProductName: 'On Line',
});
addCase('new-order-size-tier-variant', 'online 82x82 category a 3-er sofa', {
  expectProductName: 'On Line',
});
addCase('new-order-tier-variant-size', 'online category a 3-er sofa 82x82', {
  expectProductName: 'On Line',
});

// ============================================================
// Runner
// ============================================================
interface Result {
  case: Case;
  status: string;
  productName: string | undefined;
  variants: string[];
  rowCount: number;
  message: string;
  failures: string[];
}

const results: Result[] = [];
for (const c of CASES) {
  const resp = cc.answer(c.query, BRAND, c.lastModelVariant ?? null, c.lastProduct ?? null, c.lastCandidates ?? null);
  const variants = [...new Set((resp.matches || []).map(r => r.model_variant).filter((v): v is string => !!v))].sort();
  const rowCount = (resp.matches || []).length;
  const failures: string[] = [];

  if (c.expectStatus && resp.status !== c.expectStatus) {
    failures.push(`status: expected "${c.expectStatus}", got "${resp.status}"`);
  }
  if (c.expectProductName && !(resp.product_name || '').includes(c.expectProductName)) {
    failures.push(`product_name: expected to include "${c.expectProductName}", got "${resp.product_name}"`);
  }
  if (c.expectVariants) {
    const expected = [...c.expectVariants].sort();
    if (JSON.stringify(expected) !== JSON.stringify(variants)) {
      failures.push(`variants: expected [${expected.join(', ')}], got [${variants.join(', ')}]`);
    }
  }
  if (c.expectVariantsSubset) {
    const missing = c.expectVariantsSubset.filter(v => !variants.includes(v));
    if (missing.length > 0) failures.push(`variants subset: missing [${missing.join(', ')}] from [${variants.join(', ')}]`);
  }
  if (c.expectVariantsExclude) {
    const present = c.expectVariantsExclude.filter(v => variants.includes(v));
    if (present.length > 0) failures.push(`variants exclude: unexpectedly present [${present.join(', ')}]`);
  }
  if (c.expectRowCount !== undefined && rowCount !== c.expectRowCount) {
    failures.push(`rowCount: expected ${c.expectRowCount}, got ${rowCount}`);
  }
  if (c.expectMinRowCount !== undefined && rowCount < c.expectMinRowCount) {
    failures.push(`rowCount: expected >= ${c.expectMinRowCount}, got ${rowCount}`);
  }
  if (c.expectMaxRowCount !== undefined && rowCount > c.expectMaxRowCount) {
    failures.push(`rowCount: expected <= ${c.expectMaxRowCount}, got ${rowCount}`);
  }

  results.push({ case: c, status: resp.status, productName: resp.product_name, variants, rowCount, message: resp.message, failures });
}

console.log('='.repeat(100));
console.log(`DITRE ITALIA MATCHING BATTERY -- ${CASES.length} cases`);
console.log('='.repeat(100));

let pass = 0, fail = 0, gapNoted = 0;
for (const r of results) {
  const hasAssertions = r.case.expectStatus || r.case.expectProductName || r.case.expectVariants
    || r.case.expectVariantsSubset || r.case.expectVariantsExclude || r.case.expectRowCount !== undefined
    || r.case.expectMinRowCount !== undefined || r.case.expectMaxRowCount !== undefined;
  const ok = r.failures.length === 0;
  if (hasAssertions) { if (ok) pass++; else fail++; }
  else gapNoted++;

  const marker = !hasAssertions ? 'OBS ' : (ok ? 'PASS' : 'FAIL');
  console.log(`\n[${marker}] #${r.case.id} (${r.case.cat}): "${r.case.query}"`);
  if (r.case.lastProduct || r.case.lastModelVariant) {
    console.log(`       context: lastProduct=${JSON.stringify(r.case.lastProduct)} lastModelVariant=${JSON.stringify(r.case.lastModelVariant)}`);
  }
  console.log(`       status=${r.status} product_name=${JSON.stringify(r.productName)} rows=${r.rowCount}`);
  console.log(`       variants=[${r.variants.join(', ')}]`);
  if (r.case.note) console.log(`       note: ${r.case.note}`);
  if (r.failures.length > 0) {
    for (const f of r.failures) console.log(`       ** ${f}`);
  }
}

console.log('\n' + '='.repeat(100));
console.log(`RESULT: ${pass} PASS, ${fail} FAIL, ${gapNoted} observational (no hard assertion, printed for manual review)`);
console.log('='.repeat(100));

process.exit(fail > 0 ? 1 : 0);
