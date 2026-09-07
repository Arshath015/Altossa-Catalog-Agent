/**
 * regression/check_variant_display_name_conflation.ts
 * -----------------------------------------------------
 * Permanent, GATING check for a display-name bug found via live testing
 * 2026-09-07: `remainingVariants` (the distinct, non-null model_variant
 * values across a query's matched rows) silently drops rows with NO
 * model_variant at all via its own `.filter(x => !!x)`. When a product
 * has a genuine mix -- some rows with no model_variant (the plain base-
 * structure price, a normal and common shape) alongside rows sharing
 * exactly ONE real model_variant value -- both cases reduced to
 * `remainingVariants.length === 1`, making a genuinely MIXED result look
 * identical to a real single-variant narrowing.
 *
 * Confirmed real via a live sweep, not narrow to one product: 22 Pianca
 * + 6 Bolzan + 3 Bonaldo + 7 Cattelan Italia products have this exact
 * null/single-real-value split. Concretely: Pianca "Naan price" replied
 * "Here's the full price list for 'Naan (rivestimento)'" -- as if the
 * query had narrowed to JUST the rivestimento (upholstery add-on) rows
 * -- while `matches` already correctly included both the rivestimento
 * AND the null-variant base-structure rows the whole time. Data was
 * always complete; only the summary MESSAGE text was misleading.
 *
 * FIX: `allRowsShareTheVariant` additionally requires EVERY row in the
 * matched set to carry that exact variant, not just that it's the only
 * non-null value present. Applied to both the displayName branch AND
 * the addonGridNote branch just after it (the "these are all add-on
 * surcharge prices" note has the identical conflation risk).
 *
 * Also guards the ORIGINAL motivating case for this whole mechanism
 * (Cattelan "AMSTERDAM", 2026-08-xx or earlier): a genuine single-
 * variant narrowing (every row shares the same real, non-null
 * model_variant) must still combine product name + variant, never fall
 * back to a bare, unidentifiable name -- confirming this fix narrows the
 * trigger condition without breaking the case it was built for.
 *
 * Calls CatalogChat.answer() directly (deterministic path, no server/
 * Groq needed), same precedent as check_anchor_followup_vocab.ts.
 *
 * RUN WITH: npm run check-variant-display-name-conflation
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
  expectedProductName: string;
  expectMinRows: number;
  note: string;
}

const CASES: Case[] = [
  {
    id: 'naan-mixed-null-and-rivestimento-not-conflated',
    brand: 'Pianca',
    query: 'Naan price',
    expectedProductName: 'Naan',
    expectMinRows: 24,
    note: 'Exact reported repro: half of Naan\'s 24 rows have model_variant=null (base structure), half have "rivestimento" -- product_name must stay the bare "Naan", not "Naan (rivestimento)", and all 24 rows must still be returned.',
  },
  {
    id: 'gamma-mixed-null-and-rivestimento-not-conflated',
    brand: 'Pianca',
    query: 'Gamma price',
    expectedProductName: 'Gamma',
    expectMinRows: 54,
    note: 'Same shape as Naan, different product -- confirms this isn\'t a one-off, all 54 real rows (base structure + rivestimento add-on) must be returned under the bare "Gamma" name.',
  },
];

interface GenuineSingleVariantCase {
  id: string;
  brand: string;
  query: string;
  expectProductNameContains: string;
  note: string;
}

// The narrowing this file's fix makes STRICTER must not break the case
// it exists to serve: a real, complete single-variant narrowing (every
// row genuinely shares one non-null model_variant) must still combine
// product name + variant for identifiability.
const GENUINE_SINGLE_VARIANT_CASES: GenuineSingleVariantCase[] = [
  {
    id: 'cattelan-amsterdam-original-motivating-case',
    brand: 'Cattelan Italia',
    query: 'AMSTERDAM price',
    expectProductNameContains: 'AMSTERDAM',
    note: 'The original bug this whole mechanism was built for: a bare, non-identifying model_variant text ("cristallo specchiato bronzo") must never silently REPLACE the product name. Must still say "AMSTERDAM" somewhere in the reply, not just the variant text alone.',
  },
  {
    id: 'bolzan-cameo-maison-real-single-variant-still-shown',
    brand: 'Bolzan',
    query: 'Cameo Maison h.7 price',
    expectProductNameContains: 'Cameo Maison h.7',
    note: 'A genuine case where EVERY matched row shares one real model_variant that also repeats the product name -- must still show "Cameo Maison h.7" (more informative than the bare name), confirming allRowsShareTheVariant does not over-correct into never combining name+variant at all.',
  },
];

function main() {
  const failures: string[] = [];

  for (const c of CASES) {
    const resp: ChatResult = getCc(c.brand).answer(c.query, c.brand);
    const rows = (resp.matches || []).length;
    const ok = resp.product_name === c.expectedProductName && rows >= c.expectMinRows;
    if (!ok) {
      failures.push(`[${c.id}] "${c.query}" -- expected product_name="${c.expectedProductName}" with >=${c.expectMinRows} rows, got product_name=${resp.product_name} rows=${rows} message="${resp.message.slice(0, 150)}". ${c.note}`);
    }
    console.log(`[${c.id.padEnd(48)}] ${ok ? 'ok' : 'FAIL'}  product_name=${resp.product_name}  rows=${rows}`);
  }

  for (const c of GENUINE_SINGLE_VARIANT_CASES) {
    const resp: ChatResult = getCc(c.brand).answer(c.query, c.brand);
    const ok = resp.message.includes(c.expectProductNameContains);
    if (!ok) {
      failures.push(`[${c.id}] "${c.query}" -- expected message to include "${c.expectProductNameContains}", got "${resp.message.slice(0, 150)}". ${c.note}`);
    }
    console.log(`[${c.id.padEnd(48)}] ${ok ? 'ok' : 'FAIL'}  message="${resp.message.slice(0, 80)}"`);
  }

  console.log('\n' + '='.repeat(70));
  console.log(`Total cases: ${CASES.length + GENUINE_SINGLE_VARIANT_CASES.length}`);
  console.log(`Failures: ${failures.length}  <-- must be 0`);
  if (failures.length > 0) {
    console.log('\nFAILURES:');
    failures.forEach(f => console.log(`  ${f}`));
  }

  if (failures.length > 0) {
    console.log('\nEXIT 1: variant display-name conflation regressed.');
    process.exit(1);
  }
  console.log('\nEXIT 0: a mixed null/single-real-variant row set is never mislabeled as a complete single-variant narrowing, and a genuine single-variant narrowing still combines product name + variant correctly.');
}

main();
