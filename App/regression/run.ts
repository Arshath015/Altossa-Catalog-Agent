/**
 * regression/run.ts
 * -------------------
 * Permanent regression suite for the live catalog chat agent. Replays a
 * fixed set of queries (App/regression/queries.json) against the running
 * /api/catalog/chat endpoint and independently verifies every response
 * against the real source data on disk -- not against memory of what it
 * should say.
 *
 * RUN WITH:  npm run regression
 * (equivalent to: tsx App/regression/run.ts)
 *
 * Requires the dev server to already be running (npm run dev:server) --
 * this script only calls the HTTP endpoint, it never imports server code
 * directly, so it's testing exactly what a real client would see.
 *
 * WHAT IT CHECKS, AND WHAT GATES THE EXIT CODE:
 *   1. FABRICATION (gating, exit 1 if any found): every row in every
 *      response's `matches` array must exist VERBATIM in that brand's
 *      real prices.json/manual_additions.json (exact match on
 *      product_name + model_variant + size + fabric_tier + price_eur).
 *      This is the one invariant that must never break, regardless of
 *      whether the LLM step is available.
 *   2. Per-test expectations (expect_price / expect_prices / etc. in
 *      queries.json) are checked and reported, but do NOT gate the exit
 *      code -- several of them (typo recovery, multi-product combining)
 *      depend on the Groq LLM step being available, and this suite must
 *      still be useful (and not cry wolf) when the daily quota is
 *      exhausted. Read the printed summary to see the real pass rate for
 *      the conditions active at run time.
 *
 * Writes full results to App/regression/last-run.json after every run.
 */

import fs from 'fs';
import path from 'path';

const ROOT = path.join(__dirname, '..', '..');
const BASE_URL = process.env.REGRESSION_BASE_URL || 'http://localhost:3000';

interface Query {
  id: number;
  cat: string;
  brand: string;
  query: string;
  note?: string;
  expect_price?: string | null;
  expect_prices?: string[];
  expect_prices_subset?: string[];
  expect_status?: string;
  expect_row_count?: number;
  expect_all_prices?: string[];
  expect_no_fabricated_price?: boolean;
  expect_note?: string;
  /** Product name(s) that MUST all appear in resp.product_name (comma-
   * joined for multi-product) -- a stronger check than price/row-count
   * alone for name-overlap stress tests, e.g. confirming the shortlist
   * resolved to "MAGDA ML Sgabello" specifically and not one of its 11
   * near-identical siblings that also happen to have 16 rows. */
  expect_product?: string[];
}

interface PriceRow {
  brand: string;
  product_name: string;
  model_variant: string | null;
  size: string | null;
  fabric_tier: string | null;
  price_eur: string;
}

interface ChatResponse {
  status?: string;
  message?: string;
  product_name?: string;
  candidates?: string[];
  matches?: PriceRow[];
  degraded?: boolean;
  error?: string;
}

// Unlikely to appear in any real catalog text -- used as an unambiguous
// field separator so e.g. product_name="AB"+model_variant="C" can never
// collide with product_name="A"+model_variant="BC" in the lookup key.
const KEY_SEP = '~|~';
function rowKey(r: Pick<PriceRow, 'product_name' | 'model_variant' | 'size' | 'fabric_tier' | 'price_eur'>): string {
  return [r.product_name, r.model_variant, r.size, r.fabric_tier, r.price_eur].join(KEY_SEP);
}

function loadBrandRowKeys(brand: string): Set<string> {
  const pricesPath = path.join(ROOT, 'data', brand, 'prices.json');
  const rows: PriceRow[] = JSON.parse(fs.readFileSync(pricesPath, 'utf-8'));
  return new Set(rows.map(rowKey));
}

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
  const queries: Query[] = JSON.parse(fs.readFileSync(path.join(__dirname, 'queries.json'), 'utf-8'));
  const brandRowKeys = new Map<string, Set<string>>();
  for (const q of queries) {
    if (!brandRowKeys.has(q.brand)) brandRowKeys.set(q.brand, loadBrandRowKeys(q.brand));
  }

  // Confirm the server is actually reachable before running anything --
  // fail loudly and immediately rather than reporting 56 confusing errors.
  const probe = await postChat(queries[0].brand, 'ping');
  if (probe.error) {
    console.error(`\nCannot reach ${BASE_URL}/api/catalog/chat (${probe.error}).`);
    console.error('Start the server first: npm run dev:server\n');
    process.exit(1);
  }

  const results: Array<{ query: Query; response: ChatResponse; fabricated: string[]; expectationsMet: boolean | null }> = [];

  for (const q of queries) {
    const resp = await postChat(q.brand, q.query);
    const realKeys = brandRowKeys.get(q.brand)!;
    const matches = resp.matches || [];
    const fabricated = matches.filter(m => !realKeys.has(rowKey(m))).map(m => rowKey(m));

    let expectationsMet: boolean | null = null;
    const pricesReturned = new Set(matches.map(m => m.price_eur));
    if (q.expect_price !== undefined && q.expect_price !== null) {
      expectationsMet = matches.length === 1 && matches[0].price_eur === q.expect_price;
    } else if (q.expect_prices) {
      expectationsMet = q.expect_prices.every(p => pricesReturned.has(p));
    } else if (q.expect_prices_subset) {
      expectationsMet = q.expect_prices_subset.some(p => pricesReturned.has(p));
    } else if (q.expect_status) {
      expectationsMet = q.expect_status.includes(resp.status || '');
    } else if (q.expect_row_count !== undefined) {
      expectationsMet = matches.length === q.expect_row_count;
    }
    // expect_product ANDs with whatever check ran above (or stands alone
    // if that's the only expectation given) -- confirms the shortlist/
    // resolver landed on the CORRECT specific product, not just any
    // product that happens to satisfy the price/row-count check (a real
    // risk when several near-identical siblings share a row count).
    if (q.expect_product) {
      const productMatch = q.expect_product.every(p => (resp.product_name || '').includes(p));
      expectationsMet = expectationsMet === null ? productMatch : expectationsMet && productMatch;
    }

    results.push({ query: q, response: resp, fabricated, expectationsMet });
    const flag = fabricated.length > 0 ? '!! FABRICATION' : (expectationsMet === false ? 'soft-fail' : 'ok');
    console.log(`[${String(q.id).padStart(2)}] ${q.cat.padEnd(20)} status=${(resp.status || resp.error || '?').padEnd(20)} ${flag}`);
    await new Promise(r => setTimeout(r, 100));
  }

  const totalFabricated = results.filter(r => r.fabricated.length > 0);
  const softFails = results.filter(r => r.expectationsMet === false);
  const softPasses = results.filter(r => r.expectationsMet === true);
  const softNA = results.filter(r => r.expectationsMet === null);

  console.log('\n' + '='.repeat(70));
  console.log(`Total queries run: ${results.length}`);
  console.log(`Fabrication (rows not found verbatim in source data): ${totalFabricated.length}  <-- must be 0`);
  console.log(`Soft expectation checks -- passed: ${softPasses.length}, failed: ${softFails.length}, not applicable: ${softNA.length}`);
  console.log('(Soft failures are often just Groq LLM quota/availability, not a code regression -- inspect last-run.json.)');
  if (totalFabricated.length > 0) {
    console.log('\nFABRICATED ROWS (should never happen):');
    for (const r of totalFabricated) {
      console.log(`  [${r.query.id}] ${r.query.query}`);
      r.fabricated.forEach(k => console.log(`      ${k.split(KEY_SEP).join(' | ')}`));
    }
  }
  if (softFails.length > 0) {
    console.log('\nSoft expectation failures:');
    for (const r of softFails) {
      console.log(`  [${r.query.id}] ${r.query.query}  ->  status=${r.response.status}`);
    }
  }

  fs.writeFileSync(path.join(__dirname, 'last-run.json'), JSON.stringify(results, null, 2), 'utf-8');
  console.log(`\nFull results written to App/regression/last-run.json`);

  if (totalFabricated.length > 0) {
    console.log('\nEXIT 1: fabrication detected -- this must be fixed before anything else.');
    process.exit(1);
  }
  console.log('\nEXIT 0: no fabrication detected.');
}

main();
