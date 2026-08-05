/**
 * regression/check_multi_product_isolation.ts
 * ----------------------------------------------
 * Permanent, GATING regression check for cross-product parameter leakage
 * in multi-product queries -- the exact class of bug found twice in this
 * project (a sibling's NAME leaking into another product's tier scan,
 * then a sibling's TIER/SIZE words leaking in too even after the name was
 * stripped). Unlike App/regression/queries.json's soft price-membership
 * checks (which don't gate the exit code, since several legitimately
 * depend on Groq/LLM typo-correction being available), every assertion
 * here is checked on a FIELD the deterministic path must always get
 * right regardless of whether a typo'd sibling ever resolves -- so this
 * script fails loudly (exit 1) the moment this specific bug class
 * regresses, instead of silently soft-failing and being missed.
 *
 * RUN WITH: npm run check-multi-product-isolation
 * Requires the dev server running (npm run dev:server).
 *
 * Each case names a product and asserts a REQUIRED field on every row
 * where that product appears in the response, independent of whatever
 * else is (or isn't) present in `matches` -- e.g. GRETA Wood's tier must
 * be "Pelle" whether or not the typo'd sibling "wlima" also resolved to
 * WILMA this call. This is deliberate: it isolates exactly the invariant
 * that broke, rather than asserting on the whole response shape.
 */

import fs from 'fs';
import path from 'path';

const BASE_URL = process.env.REGRESSION_BASE_URL || 'http://localhost:3000';

interface PriceRow {
  product_name: string;
  fabric_tier: string | null;
  price_eur: string;
}
interface ChatResponse {
  status?: string;
  matches?: PriceRow[];
  error?: string;
}
interface RequiredField {
  product: string;
  tier: string;
  /** Omit when the product has multiple real sizes for this tier (e.g.
   * SOFIA has 4 distinct sizes under "Pelle") -- in that case every
   * returned row for this product must share this tier (never a mix
   * with a sibling's wrong tier), rather than asserting one exact price. */
  price?: string;
}
interface Case {
  id: string;
  brand: string;
  query: string;
  note: string;
  required: RequiredField[];
}

// The two exact queries the user reported broken, verbatim -- plus the
// existing BISHOP/RICHARD/RITZ Lounge case (queries.json id 12) as a
// control, since it directly proves a product with NO size of its own
// must show its own real options rather than borrowing a sibling's size.
const CASES: Case[] = [
  {
    id: 'greta-wilma-tier-leak',
    brand: 'Cattelan Italia',
    query: 'gve me greta wood pelle and wlima pelle glove',
    note: 'Confirmed bug: GRETA Wood was getting WILMA\'s tier "Pelle Glove" instead of its own "Pelle".',
    required: [
      { product: 'GRETA Wood', tier: 'Pelle', price: '1.456' },
    ],
  },
  {
    id: 'sierra-tina-size-leak',
    brand: 'Cattelan Italia',
    query: 'sierra pouf 100x94x41h pelle and tina pelle',
    note: 'Confirmed bug: TINA (1 real size only) was refusing to resolve because SIERRA pouf\'s size leaked into its lookup.',
    required: [
      { product: 'SIERRA pouf', tier: 'Pelle', price: '1.902' },
      { product: 'TINA', tier: 'Pelle', price: '758' },
    ],
  },
  {
    id: 'reversed-tiers-control',
    brand: 'Cattelan Italia',
    query: 'wilma pelle glove and sofia pelle',
    note: 'Control: tiers reversed vs. the other direction -- confirms the fix isn\'t direction-dependent.',
    required: [
      { product: 'WILMA', tier: 'Pelle Glove', price: '985' },
      { product: 'SOFIA', tier: 'Pelle' },
    ],
  },
  {
    id: 'bishop-no-own-size-control',
    brand: 'Cattelan Italia',
    query: 'bishop, richard a 245x234x102 pelle, and ritz lounge 118',
    note: 'Control (pre-existing queries.json id 12): BISHOP names no size of its own and must show its own ambiguous options, never borrow RICHARD\'s or RITZ Lounge\'s size.',
    required: [
      { product: 'RICHARD', tier: 'Pelle', price: '6.037' },
    ],
  },
];

async function postChat(brand: string, message: string): Promise<ChatResponse> {
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    const res = await fetch(`${BASE_URL}/api/catalog/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ brand, message }),
      signal: controller.signal,
    });
    clearTimeout(timeout);
    if (!res.ok) return { error: `HTTP ${res.status}` };
    return await res.json();
  } catch (err) {
    return { error: err instanceof Error ? err.message : String(err) };
  }
}

async function main() {
  const probe = await postChat(CASES[0].brand, 'ping');
  if (probe.error) {
    console.error(`\nCannot reach ${BASE_URL}/api/catalog/chat (${probe.error}).`);
    console.error('Start the server first: npm run dev:server\n');
    process.exit(1);
  }

  const failures: string[] = [];
  const results: Array<{ case: Case; response: ChatResponse; caseFailed: boolean }> = [];

  for (const c of CASES) {
    const resp = await postChat(c.brand, c.query);
    const matches = resp.matches || [];
    let caseFailed = false;

    for (const req of c.required) {
      const rows = matches.filter(m => m.product_name === req.product);
      if (rows.length === 0) {
        failures.push(`[${c.id}] "${c.query}" -- expected "${req.product}" to appear in matches at all, found nothing (status=${resp.status || resp.error})`);
        caseFailed = true;
        continue;
      }
      if (req.price !== undefined) {
        const matchingRow = rows.find(r => r.fabric_tier === req.tier && r.price_eur === req.price);
        if (!matchingRow) {
          const actual = rows.map(r => `${r.fabric_tier}=€${r.price_eur}`).join(', ');
          failures.push(`[${c.id}] "${c.query}" -- expected ${req.product} tier="${req.tier}" price="${req.price}", got: ${actual}`);
          caseFailed = true;
        }
        continue;
      }
      // No price given -- this product has multiple real sizes for this
      // tier, so just assert every returned row shares the required
      // tier (never mixed with a sibling's leaked-in wrong tier).
      const wrongTierRows = rows.filter(r => r.fabric_tier !== req.tier);
      if (wrongTierRows.length > 0) {
        const actual = [...new Set(rows.map(r => r.fabric_tier))].join(', ');
        failures.push(`[${c.id}] "${c.query}" -- expected every ${req.product} row to have tier="${req.tier}", got tiers: ${actual}`);
        caseFailed = true;
      }
    }

    results.push({ case: c, response: resp, caseFailed });
    console.log(`[${c.id.padEnd(28)}] ${caseFailed ? 'FAIL' : 'ok'}`);
    await new Promise(r => setTimeout(r, 100));
  }

  console.log('\n' + '='.repeat(70));
  console.log(`Total cases: ${CASES.length}`);
  console.log(`Failures: ${failures.length}  <-- must be 0`);
  if (failures.length > 0) {
    console.log('\nFAILURES:');
    failures.forEach(f => console.log(`  ${f}`));
  }

  fs.writeFileSync(path.join(__dirname, 'check_multi_product_isolation-run.json'), JSON.stringify(results, null, 2), 'utf-8');
  console.log(`\nFull results written to App/regression/check_multi_product_isolation-run.json`);

  if (failures.length > 0) {
    console.log('\nEXIT 1: cross-product parameter leakage detected.');
    process.exit(1);
  }
  console.log('\nEXIT 0: no cross-product leakage in any case.');
}

main();
