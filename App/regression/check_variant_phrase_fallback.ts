/**
 * regression/check_variant_phrase_fallback.ts
 * --------------------------------------------
 * Permanent, GATING check for Issue 5 (found via live testing 2026-09-01):
 * "give all Cuscini opzionali per seduta prices" (Pianca) -- a real
 * sub-item name lifted verbatim from Island Up's own source page -- landed
 * on the WRONG, unrelated product "Cuscini decorativi" purely because both
 * share the single word "cuscini". The `cuscini`/GENERIC_CATEGORY_WORDS fix
 * (same session) stopped the wrong guess, but that alone left the query
 * with NO answer at all: neither `matchProducts()` nor the LLM's own
 * product-name list has ever searched `variant_context`/`model_variant`
 * text -- only product NAMES. A user who names a sub-section/module/
 * accessory by its own descriptive text, without ever saying the parent
 * product's name, had no way to be found.
 *
 * SCOPE, established before building anything (user's explicit
 * requirement -- see the Issue 5 scope-estimate report): NOT Pianca-only.
 * Spot-checking every brand's own variant_context/model_variant text
 * found Ditre Italia's modular-sofa sub-configurations (Ada, Isla, Krisby,
 * Loman, Atlantis, ...) comparably-or-more exposed than Pianca's own
 * "Moduli People a cassetto"/"Cuscini opzionali" shapes. Bolzan/Bonaldo/
 * Cattelan's own hits were overwhelmingly finish/material descriptors
 * ("Legno con bordi naturali", "Ceramica finitura seta") -- NOT
 * independently-searchable sub-items, already handled by the existing
 * fabric_tier path -- so those 3 brands were explicitly deferred as
 * low-priority, logged residual risk, not silently ignored. Varaschini is
 * structurally immune: its `product_name` field already IS the full
 * per-SKU descriptive text (verified directly -- product_name and
 * model_variant are byte-identical for a large sample), so there's no
 * separate hidden layer to index at all.
 *
 * MECHANISM (`CatalogChat.findByVariantPhrase`, `catalogChat.ts`): a
 * separate fallback stage, consulted ONLY when primary product-name
 * matching (`matchProducts`) finds literally nothing -- never folded into
 * `matchProducts()`/`similarity()` itself, deliberately, since that
 * function is already the site of several collision fixes this session
 * (GENERIC_CATEGORY_WORDS/cuscini, "con"/"per" filler dilution, the bare
 * "80" number collision) and widening its own candidate-name space would
 * risk reopening exactly that bug family. Every CatalogChat instance
 * builds its own `variantPhraseIndex` (data-driven per brand, from that
 * brand's own prices.json -- not hardcoded to Pianca or Ditre) at
 * construction time, then SCORES it using the unmodified, reused
 * `similarity()` function, requiring the token-containment tier (80) --
 * the lower diluted-overlap tier (up to 60) is deliberately excluded,
 * since that's the exact scoring shape responsible for every spurious-
 * match bug this session already found. On a match, the result is SCOPED
 * to just the matched phrase's own rows (e.g. Island Up's 18 cushion
 * rows), not the whole product's price grid -- so the answer is precise,
 * not a "here's everything this product sells" dump that buries it.
 *
 * RISK: this DOES touch matching-adjacent territory even though it never
 * modifies matchProducts()/similarity() -- it's a new consumer of
 * similarity(), gated behind `matches.length === 0` in `answer()`, a
 * condition several OTHER fixes this session (anchor-vocabulary
 * widening, the "give all" content-free fallback) are also built around.
 * Verified via a full `regression:full` run (including
 * check_ditre_matching.ts's 48-case battery -- Ditre is both the
 * brand most exposed to this new fallback AND the most matcher-sensitive
 * regression suite in the project) plus check_null_size_price_grid.ts's
 * exhaustive sweep, all green alongside this file.
 *
 * RUN WITH: npm run check-variant-phrase-fallback
 * Requires the dev server running (npm run dev:server).
 */

const BASE_URL = process.env.REGRESSION_BASE_URL || 'http://localhost:3000';

interface PriceRow {
  product_name: string;
  variant_context: string | null;
  model_variant: string | null;
  price_eur: string;
  code: string | null;
}

interface ChatResponse {
  status?: string;
  product_name?: string;
  matches?: PriceRow[];
  message?: string;
  error?: string;
}

async function postChat(brand: string, message: string): Promise<ChatResponse> {
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    const res = await fetch(`${BASE_URL}/api/catalog/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ brand, message, history: [] }),
      signal: controller.signal,
    });
    clearTimeout(timeout);
    if (!res.ok) return { error: `HTTP ${res.status}` };
    return await res.json();
  } catch (err) {
    return { error: err instanceof Error ? err.message : String(err) };
  }
}

function priceRange(rows: PriceRow[]): [number, number] {
  const nums = rows
    .map(r => parseFloat(String(r.price_eur).replace(/[^\d.,]/g, '').replace('.', '').replace(',', '.')))
    .filter(n => !isNaN(n));
  return [Math.min(...nums), Math.max(...nums)];
}

interface Case {
  id: string;
  brand: string;
  query: string;
  expectedProduct: string;
  expectedRowCount: number;
  expectedPriceRange: [number, number];
  note: string;
}

const CASES: Case[] = [
  {
    id: 'pianca-island-up-cuscini-opzionali-exact-repro',
    brand: 'Pianca',
    query: 'give all Cuscini opzionali per seduta prices',
    expectedProduct: 'Island up',
    expectedRowCount: 18,
    expectedPriceRange: [364, 553],
    note: 'The exact user-reported repro. Previously matched the wrong, unrelated product "Cuscini decorativi"; after the cuscini/GENERIC_CATEGORY_WORDS fix it correctly matched NOTHING at all (no path ever searched variant_context text) until this fallback.',
  },
  {
    id: 'ditre-arlott-central-3-seats-extra',
    brand: 'Ditre Italia',
    query: 'price for central 3 seats extra',
    expectedProduct: 'Arlott high',
    expectedRowCount: 12,
    expectedPriceRange: [5807, 11423],
    note: 'Ditre proof case #1 -- a named sofa-system sub-configuration, same shape as Island Up\'s cushions, confirming the mechanism is genuinely cross-brand, not Pianca-specific.',
  },
  {
    id: 'ditre-atlantis-large-armrest-cushion',
    brand: 'Ditre Italia',
    query: 'give large armrest cushion price',
    expectedProduct: 'Atlantis',
    expectedRowCount: 20,
    expectedPriceRange: [254, 8816],
    note: 'Ditre proof case #2 -- a named accessory, no product-name overlap at all with "Atlantis".',
  },
  {
    id: 'ditre-loman-central-corner-base',
    brand: 'Ditre Italia',
    query: 'central corner base price',
    expectedProduct: 'Loman 2.0 (Sofa)',
    expectedRowCount: 24,
    expectedPriceRange: [1418, 6011],
    note: 'Ditre proof case #3 -- a named modular-base sub-component.',
  },
];

async function main() {
  const failures: string[] = [];

  const probe = await postChat('Pianca', 'ping');
  if (probe.error) {
    console.error(`\nCannot reach ${BASE_URL}/api/catalog/chat (${probe.error}).`);
    console.error('Start the server first: npm run dev:server\n');
    process.exit(1);
  }

  for (const c of CASES) {
    const resp = await postChat(c.brand, c.query);
    const rows = resp.matches || [];
    const productOk = resp.product_name === c.expectedProduct;
    const countOk = rows.length === c.expectedRowCount;
    const [lo, hi] = rows.length > 0 ? priceRange(rows) : [NaN, NaN];
    const rangeOk = lo === c.expectedPriceRange[0] && hi === c.expectedPriceRange[1];
    const ok = productOk && countOk && rangeOk;

    if (!ok) {
      failures.push(
        `[${c.id}] query=${JSON.stringify(c.query)} -- ` +
        `product: got ${JSON.stringify(resp.product_name)} expected ${JSON.stringify(c.expectedProduct)}; ` +
        `rowCount: got ${rows.length} expected ${c.expectedRowCount}; ` +
        `priceRange: got [${lo},${hi}] expected [${c.expectedPriceRange[0]},${c.expectedPriceRange[1]}]; ` +
        `status=${resp.status} message=${JSON.stringify(resp.message)}`
      );
      console.log(`[${c.id.padEnd(48)}] FAIL`);
    } else {
      console.log(`[${c.id.padEnd(48)}] ok  (${rows.length} rows, €${lo}-€${hi}, product="${resp.product_name}")`);
    }
    await new Promise(r => setTimeout(r, 80));
  }

  console.log('\n' + '='.repeat(70));
  console.log(`Total cases: ${CASES.length}`);
  console.log(`Total failures: ${failures.length}  <-- must be 0`);
  if (failures.length > 0) {
    console.log('\nFAILURES:');
    failures.forEach(f => console.log(`  ${f}`));
    console.log('\nEXIT 1: variant-phrase fallback regressed.');
    process.exit(1);
  }
  console.log('\nEXIT 0: variant-phrase fallback resolves both Pianca\'s and Ditre\'s real sub-item-name queries to scoped, correct rows.');
}

main();
