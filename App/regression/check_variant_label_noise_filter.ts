/**
 * regression/check_variant_label_noise_filter.ts
 * ------------------------------------------------
 * Permanent, GATING check for the isUnbalancedParenFragment sanitization
 * added to CatalogChat's constructor (2026-09-07), and specifically for
 * the regression that a first, broader attempt at the same fix caused.
 *
 * ROOT CAUSE: PDF-extraction noise occasionally leaks into a price row's
 * `variant_context`/`model_variant` field instead of a real category/
 * variant name. Confirmed real, not hypothetical -- two live examples in
 * Pianca's own data:
 *   - Soffio fisso: "Laccato e Vetro Marmo)" -- the truncated tail of a
 *     wrapped 3-line limitation note ("P 140 (Non sono previsti piani in
 *     Essenza, Vetro / Laccato e Vetro Marmo)"), leaking into 34 real
 *     price rows' variant_context. Price/size/code data on those rows is
 *     completely correct; only the label was garbled.
 *   - Forma: "0        R" -- a leaked radius-dimension callout from a
 *     technical drawing. NOT fixed by the current, narrower sanitization
 *     (see below) -- a known, disclosed, accepted gap.
 *
 * THE REGRESSION (why this check exists, not just the fix itself): the
 * first fix attempt reused isIndexableVariantPhrase's existing noise
 * checks wholesale (leading digit, unbalanced parens, duplicated
 * segment) to decide whether to DELETE a raw field value -- but those
 * checks were only ever proven safe for a much lower-stakes decision
 * (whether to index a phrase for fuzzy search; excluding a real value
 * there just means no extra search path). Applying the SAME leading-
 * digit check to outright delete data destroyed Ditre's own real
 * model_variant values -- "2-er sofa", "3-er maxi sofa", "1-er base
 * element", dozens more -- since they legitimately start with a digit.
 * Caught by check-ditre-matching: 27 of 48 cases failed, every "On
 * Line" query returning ALL 120 rows (Island/Square corner/Square
 * corner maxi) instead of narrowing to the specific "N-er ..." variant
 * asked for.
 *
 * THE FIX (final, narrower version): isUnbalancedParenFragment checks
 * ONLY for a literal unmatched "(" or ")" -- confirmed via a catalog-
 * wide scan across all 6 brands' variant_context/model_variant values
 * that this signal has ZERO false positives (every one of the 6 real
 * values it flags, across Pianca and Varaschini, is a genuine truncated-
 * note fragment, none is legitimate category text). A short-length
 * heuristic and a leading-digit heuristic were also tried and rejected
 * after each turned up real false positives elsewhere in the catalog
 * (Ditre's own real "H1"-"H5"/"USB"/"BOX" short codes, Bonaldo's bare-
 * number sizes, on top of the "2-er sofa" case above).
 *
 * Calls CatalogChat.answer() directly (deterministic path, no server/
 * Groq needed), same precedent as check_anchor_followup_vocab.ts.
 *
 * RUN WITH: npm run check-variant-label-noise-filter
 */

import { CatalogChat, ChatResult } from '../server/catalogChat';

const ccByBrand = new Map<string, CatalogChat>();
function getCc(brand: string): CatalogChat {
  if (!ccByBrand.has(brand)) ccByBrand.set(brand, new CatalogChat(`./data/${brand}`));
  return ccByBrand.get(brand)!;
}

interface NoiseCase {
  id: string;
  brand: string;
  query: string;
  mustNotIncludeInMessage: string[];
  note: string;
}

const NOISE_CASES: NoiseCase[] = [
  {
    id: 'soffio-fisso-truncated-note-fragment',
    brand: 'Pianca',
    query: 'Soffio fisso price',
    mustNotIncludeInMessage: ['Laccato e Vetro Marmo)'],
    note: 'The exact repro: this garbled label (a truncated wrapped limitation note, unbalanced parens) must never appear in the summary message, even though the 34 real price rows carrying it must still all be returned.',
  },
];

interface RowIntegrityCase {
  id: string;
  brand: string;
  query: string;
  expectMinRows: number;
  note: string;
}

const ROW_INTEGRITY_CASES: RowIntegrityCase[] = [
  {
    id: 'soffio-fisso-rows-still-complete',
    brand: 'Pianca',
    query: 'Soffio fisso price',
    expectMinRows: 406,
    note: 'The label sanitization must never drop rows -- only null the garbled label field. Soffio fisso has 406 real price rows total.',
  },
];

interface DitreGuardCase {
  id: string;
  brand: string;
  query: string;
  expectedVariant: string;
  note: string;
}

// Permanent regression for the exact false-positive shape the first fix
// attempt caused: a leading-digit check destroying Ditre's own real,
// digit-prefixed model_variant values. Each case must resolve to its own
// specific real variant, NOT collapse into the unnarrowed full catalog
// (Island/Square corner/Square corner maxi -- what a nulled-out
// model_variant field looks like from the outside).
const DITRE_GUARD_CASES: DitreGuardCase[] = [
  {
    id: 'ditre-2er-sofa-not-destroyed',
    brand: 'Ditre Italia',
    query: 'give online 2er sofa price',
    expectedVariant: '2-er sofa',
    note: 'Real, digit-prefixed model_variant value -- must survive the noise sanitization, not be treated as a leading-digit diagram callout.',
  },
  {
    id: 'ditre-3er-maxi-sofa-not-destroyed',
    brand: 'Ditre Italia',
    query: 'give online 3er maxi sofa price',
    expectedVariant: '3-er maxi sofa',
    note: 'Second digit-prefixed real variant, different product family shape, same guard.',
  },
];

function main() {
  const failures: string[] = [];

  for (const c of NOISE_CASES) {
    const resp: ChatResult = getCc(c.brand).answer(c.query, c.brand);
    const bad = c.mustNotIncludeInMessage.filter(s => resp.message.includes(s));
    const ok = bad.length === 0;
    if (!ok) {
      failures.push(`[${c.id}] "${c.query}" -- message must not include [${bad.join(', ')}], but it did: "${resp.message}". ${c.note}`);
    }
    console.log(`[${c.id.padEnd(40)}] ${ok ? 'ok' : 'FAIL'}  status=${resp.status}`);
  }

  for (const c of ROW_INTEGRITY_CASES) {
    const resp: ChatResult = getCc(c.brand).answer(c.query, c.brand);
    const rows = (resp.matches || []).length;
    const ok = rows >= c.expectMinRows;
    if (!ok) {
      failures.push(`[${c.id}] "${c.query}" -- expected >=${c.expectMinRows} rows, got ${rows}. ${c.note}`);
    }
    console.log(`[${c.id.padEnd(40)}] ${ok ? 'ok' : 'FAIL'}  rows=${rows}`);
  }

  for (const c of DITRE_GUARD_CASES) {
    const resp: ChatResult = getCc(c.brand).answer(c.query, c.brand);
    const variants = [...new Set((resp.matches || []).map(m => m.model_variant))];
    const ok = variants.length === 1 && variants[0] === c.expectedVariant;
    if (!ok) {
      failures.push(`[${c.id}] "${c.query}" -- expected variant "${c.expectedVariant}", got [${variants.join(', ')}]. ${c.note}`);
    }
    console.log(`[${c.id.padEnd(40)}] ${ok ? 'ok' : 'FAIL'}  variants=[${variants.join(', ')}]`);
  }

  console.log('\n' + '='.repeat(70));
  console.log(`Total cases: ${NOISE_CASES.length + ROW_INTEGRITY_CASES.length + DITRE_GUARD_CASES.length}`);
  console.log(`Failures: ${failures.length}  <-- must be 0`);
  if (failures.length > 0) {
    console.log('\nFAILURES:');
    failures.forEach(f => console.log(`  ${f}`));
  }

  if (failures.length > 0) {
    console.log('\nEXIT 1: variant-label noise filter regressed.');
    process.exit(1);
  }
  console.log('\nEXIT 0: garbled extraction-noise labels (Soffio fisso) are correctly hidden with all rows intact, and real digit-prefixed Ditre variant names are correctly NOT destroyed by the same filter.');
}

main();
