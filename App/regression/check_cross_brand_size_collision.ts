/**
 * regression/check_cross_brand_size_collision.ts
 * ---------------------------------
 * Follow-up scan explicitly deferred at the end of the original Pianca
 * size-collision fix (2026-08-28, see check_pianca_size_collision.ts and
 * [[pianca_venere_size_collision_fix]]): "a same-collision-shape scan of
 * the OTHER brands' own prices.json files, to check whether any of them
 * ALSO have same-size-different-code collisions sitting undiscovered."
 *
 * The FIX itself (priceGridGrouping.ts's code-based secondary column key)
 * has always been brand-agnostic -- it's shared code every brand's chat
 * widget already uses. This check answers the separate, never-actually-
 * checked question: do any of the other 5 brands ALSO have real products
 * exhibiting this collision shape, and is the shared fix actually
 * verified to protect them (not just assumed to, by virtue of being
 * shared code)?
 *
 * Run 2026-09-03: YES, on a much larger scale than Pianca's own 89 --
 * Bolzan (45 products, 822 colliding groups), Bonaldo (37 products, 606
 * groups), Ditre Italia (11 products, 219 groups). Cattelan Italia and
 * Varaschini are genuinely clean (0 groups). Every one of these 93
 * products was individually live-verified (this check, its first real
 * run) to have 0 rows lost via the exact grid traversal the UI uses --
 * the shared fix does already protect them, confirming the "likely
 * already fixed, just unverified" hypothesis from the deferral note, not
 * a live active bug.
 *
 * Unlike check_pianca_size_collision.ts (which reads a hand-curated,
 * point-in-time snapshot list), this check recomputes each brand's own
 * affected-product list FRESH from prices.json on every run -- the whole
 * point is to catch a brand DRIFTING INTO this collision shape in the
 * future (a new product added with a genuine same-size-different-code
 * pair), not just to re-confirm today's fixed list forever.
 *
 * RUN WITH: npm run check-cross-brand-size-collision
 * Requires the dev server running (npm run dev:server).
 */

import fs from 'fs';
import path from 'path';
import { buildVariantGroups, buildSizeColumns, findCell, FLAT_PRICE_COLUMN_KEY } from '../component/priceGridGrouping';
import type { PriceRow } from '../component/priceGridGrouping';

const BASE_URL = process.env.REGRESSION_BASE_URL || 'http://localhost:3000';
const ROOT = path.join(__dirname, '..', '..');

// Pianca is deliberately excluded here -- it already has its own
// dedicated, more detailed check (check_pianca_size_collision.ts,
// including the Servoquadro_Servogiro exclusion note). This check covers
// the 5 brands that check never looked at.
const BRANDS = ['Bolzan', 'Bonaldo', 'Cattelan Italia', 'Ditre Italia', 'Varaschini'];

interface ChatResponse {
  status?: string;
  matches?: PriceRow[];
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

/** Same collision definition as the original Pianca scan: within one
 * (product_name, model_variant, variant_context, fabric_tier, size)
 * group, 2+ DIFFERENT codes with 2+ DIFFERENT prices -- a genuine
 * wrong-price/data-loss risk, not just a harmless duplicate. */
function findAffectedProducts(prices: PriceRow[]): string[] {
  const groups = new Map<string, PriceRow[]>();
  for (const r of prices) {
    const key = [r.product_name, r.model_variant, r.variant_context, r.fabric_tier, r.size].join('');
    (groups.get(key) ?? groups.set(key, []).get(key)!).push(r);
  }
  const affected = new Set<string>();
  for (const rows of groups.values()) {
    const codes = new Set(rows.map(r => r.code));
    const distinctPrices = new Set(rows.map(r => r.price_eur));
    if (codes.size > 1 && distinctPrices.size > 1) affected.add(rows[0].product_name);
  }
  return [...affected];
}

/** Every row in `rows` must be reachable via the exact grid traversal the
 * UI uses -- identical logic to check_pianca_size_collision.ts's own
 * findLostRows, kept in sync manually since it's a small, stable check
 * (a shared helper would need its own module just for this one
 * function). */
function findLostRows(rows: PriceRow[]): PriceRow[] {
  const lost: PriceRow[] = [];
  for (const group of buildVariantGroups(rows)) {
    const columns = buildSizeColumns(group.rows);
    for (const row of group.rows) {
      const rowKey = row.size ?? FLAT_PRICE_COLUMN_KEY;
      const col = columns.find(c => (c.size ?? FLAT_PRICE_COLUMN_KEY) === rowKey && (
        !c.key.includes('::') || c.key === `${rowKey}::${row.code ?? ''}`
      ));
      const found = col ? findCell(group.rows, row.fabric_tier, col.key, columns) : undefined;
      const ok = found && found.code === row.code && found.price_eur === row.price_eur;
      if (!ok) lost.push(row);
    }
  }
  return lost;
}

async function main() {
  const probe = await postChat(BRANDS[0], 'ping');
  if (probe.error) {
    console.error(`\nCannot reach ${BASE_URL}/api/catalog/chat (${probe.error}).`);
    console.error('Start the server first: npm run dev:server\n');
    process.exit(1);
  }

  const failures: string[] = [];
  let totalProductsChecked = 0;
  let totalRowsChecked = 0;

  for (const brand of BRANDS) {
    const pricesPath = path.join(ROOT, 'data', brand, 'prices.json');
    if (!fs.existsSync(pricesPath)) {
      console.log(`\n=== ${brand}: no prices.json found, skipping ===`);
      continue;
    }
    const prices: PriceRow[] = JSON.parse(fs.readFileSync(pricesPath, 'utf-8'));
    const affected = findAffectedProducts(prices);

    console.log(`\n=== ${brand}: ${affected.length} product(s) with a real same-size-different-code collision ===`);
    if (affected.length === 0) continue;

    for (const product of affected) {
      const resp = await postChat(brand, `give all prices for ${product}`);
      const rows = (resp.matches || []).filter(m => m.product_name === product);
      totalProductsChecked++;
      if (rows.length === 0) {
        failures.push(`[${brand}/${product}] expected rows, got 0 (status=${resp.status || resp.error})`);
        console.log(`  [${product.padEnd(45)}] FAIL -- no rows returned`);
        continue;
      }
      const lost = findLostRows(rows);
      totalRowsChecked += rows.length;
      if (lost.length > 0) {
        failures.push(`[${brand}/${product}] ${lost.length} row(s) unreachable via the grid traversal: ${JSON.stringify(lost.slice(0, 3))}`);
        console.log(`  [${product.padEnd(45)}] FAIL -- ${lost.length}/${rows.length} rows lost`);
      } else {
        console.log(`  [${product.padEnd(45)}] ok  (${rows.length} rows, 0 lost)`);
      }
      await new Promise(r => setTimeout(r, 60));
    }
  }

  console.log('\n' + '='.repeat(70));
  console.log(`Products checked: ${totalProductsChecked}`);
  console.log(`Total rows checked: ${totalRowsChecked}`);
  console.log(`Failures: ${failures.length}  <-- must be 0`);
  if (failures.length > 0) {
    console.log('\nFAILURES:');
    failures.forEach(f => console.log(`  ${f}`));
    console.log('\nEXIT 1: size-collision row loss found in a non-Pianca brand.');
    process.exit(1);
  }
  console.log('\nEXIT 0: every non-Pianca brand product with a real same-size-different-code collision is reachable via the price grid, no silent data loss.');
}

main();
