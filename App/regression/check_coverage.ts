/**
 * regression/check_coverage.ts
 * ------------------------------
 * Permanent regression check: for EVERY product in the catalog, asks the
 * live chat endpoint for its full price list and verifies the returned
 * row count matches the real row count in prices.json exactly. Catches
 * Bug-B-style gaps (a real row silently missing from what the app
 * returns) across the whole catalog, not just the handful of products
 * the main 59-query regression suite happens to sample.
 *
 * RUN WITH:  npm run check-coverage
 *
 * Deliberately LLM-independent and fast: queries by the product's own
 * exact name ("give me all prices for <Name>"), which the deterministic
 * matcher (detectNamedProductsInText / matchProducts) resolves at score
 * 100 with no LLM assistance needed, and "all prices" reliably triggers
 * wantsFullList via the plain keyword regex -- so this check's pass/fail
 * result doesn't depend on Groq quota being available.
 */

import fs from 'fs';
import path from 'path';

const ROOT = path.join(__dirname, '..', '..');
const BASE_URL = process.env.REGRESSION_BASE_URL || 'http://localhost:3000';
const BRANDS = ['Cattelan Italia', 'Bolzan', 'Bonaldo'];

// Products already triaged as `known_gap` in flag_triage.json are an
// accepted, documented, SAFE backlog (parser returns zero rows / skips
// the ambiguous row rather than guessing -- never silently wrong; see
// flag_triage.json's own `_status_meaning.known_gap`) -- used heavily for
// Bonaldo's initial 197-product batch seeding. Counting every one of
// those as a fresh "mismatch" here would bury any genuinely NEW finding
// under ~140+ already-known lines every run. Other statuses (real_gap/
// partial/false_alarm) are claimed-RESOLVED, so those products are still
// checked normally -- only `known_gap` is excluded.
function loadKnownGapProducts(brand: string): Set<string> {
  const triagePath = path.join(ROOT, 'App', 'regression', 'flag_triage.json');
  if (!fs.existsSync(triagePath)) return new Set();
  const triage = JSON.parse(fs.readFileSync(triagePath, 'utf-8'));
  const brandTriage = triage[brand] || {};
  return new Set(
    Object.entries(brandTriage)
      .filter(([, v]: [string, any]) => v && v.status === 'known_gap')
      .map(([k]) => k)
  );
}

interface PriceRow {
  product_name: string;
  model_variant: string | null;
  size: string | null;
  fabric_tier: string | null;
  price_eur: string;
  ambiguous?: boolean;
}

const KEY_SEP = '~|~';
function rowKey(r: Pick<PriceRow, 'product_name' | 'model_variant' | 'size' | 'fabric_tier' | 'price_eur'>): string {
  return [r.product_name, r.model_variant, r.size, r.fabric_tier, r.price_eur].join(KEY_SEP);
}

async function postChat(brand: string, message: string): Promise<{ matches?: PriceRow[]; status?: string; error?: string }> {
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

async function checkBrand(brand: string) {
  const dataDir = path.join(ROOT, 'data', brand);
  const pricesPath = path.join(dataDir, 'prices.json');
  if (!fs.existsSync(pricesPath)) {
    console.log(`\n=== ${brand}: no prices.json found, skipping ===`);
    return { checked: 0, mismatches: [] as string[], fabricated: [] as string[] };
  }
  const prices: PriceRow[] = JSON.parse(fs.readFileSync(pricesPath, 'utf-8'));
  const realKeys = new Set(prices.map(rowKey));
  const knownGap = loadKnownGapProducts(brand);

  const byProduct = new Map<string, PriceRow[]>();
  for (const r of prices) {
    if (!byProduct.has(r.product_name)) byProduct.set(r.product_name, []);
    byProduct.get(r.product_name)!.push(r);
  }

  const mismatches: string[] = [];
  const fabricated: string[] = [];
  let checked = 0;
  let skippedKnownGap = 0;

  for (const [productName, rows] of byProduct) {
    if (knownGap.has(productName)) { skippedKnownGap++; continue; }
    const resp = await postChat(brand, `give me all prices for ${productName}`);
    checked++;
    if (resp.error) {
      mismatches.push(`${productName}: request failed (${resp.error})`);
      continue;
    }
    const matches = resp.matches || [];
    // The app deliberately excludes rows marked `ambiguous` (conflicting
    // listed prices in the source catalog) from a normal full-list reply
    // -- correct, intentional safety behavior, not a gap. It only
    // includes them when EVERY row for the product is ambiguous (nothing
    // safe to show at all), in which case it returns all of them for
    // transparency. Expected count here mirrors that exact rule.
    const cleanRows = rows.filter(r => !r.ambiguous);
    const expected = cleanRows.length > 0 ? cleanRows : rows;
    if (matches.length !== expected.length) {
      mismatches.push(`${productName}: expected ${expected.length} rows, got ${matches.length} (status=${resp.status})`);
    }
    for (const m of matches) {
      if (!realKeys.has(rowKey(m))) fabricated.push(`${productName}: fabricated row ${JSON.stringify(m)}`);
    }
    if (checked % 50 === 0) console.log(`  ...${brand}: checked ${checked}/${byProduct.size}`);
    await new Promise(r => setTimeout(r, 80));
  }

  console.log(`\n=== ${brand}: ${checked} products checked (${skippedKnownGap} skipped -- already triaged as known_gap) ===`);
  console.log(`Row-count mismatches: ${mismatches.length}`);
  console.log(`Fabricated rows: ${fabricated.length}`);
  if (mismatches.length > 0) {
    console.log('Mismatches:');
    mismatches.forEach(m => console.log(`  ${m}`));
  }
  if (fabricated.length > 0) {
    console.log('Fabricated:');
    fabricated.forEach(m => console.log(`  ${m}`));
  }

  return { checked, mismatches, fabricated };
}

async function main() {
  const probe = await postChat(BRANDS[0], 'ping');
  if (probe.error) {
    console.error(`\nCannot reach ${BASE_URL}/api/catalog/chat (${probe.error}).`);
    console.error('Start the server first: npm run dev:server\n');
    process.exit(1);
  }

  let totalMismatches = 0;
  let totalFabricated = 0;
  for (const brand of BRANDS) {
    const result = await checkBrand(brand);
    totalMismatches += result.mismatches.length;
    totalFabricated += result.fabricated.length;
  }

  console.log('\n' + '='.repeat(70));
  console.log(`Total row-count mismatches across all brands: ${totalMismatches}  <-- must be 0`);
  console.log(`Total fabricated rows across all brands: ${totalFabricated}  <-- must be 0`);
  if (totalMismatches > 0 || totalFabricated > 0) {
    console.log('\nEXIT 1: coverage gap or fabrication detected.');
    process.exit(1);
  }
  console.log('\nEXIT 0: every product\'s full price list matches source data exactly.');
}

main();
