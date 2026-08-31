/**
 * regression/check_null_size_price_grid.ts
 * ---------------------------------
 * Permanent, GATING check for a real, WIDESPREAD bug found via live
 * testing 2026-09-01 (user-reported: Dedalo (Progetti 06-07)'s own
 * "Accessori kit luce" table -- 3 real, correctly-priced rows, codes
 * 47101/47100/47102, EUR186/186/206 -- all rendered as "-" with no price
 * visible anywhere in the chat UI, despite the API response already
 * having the correct price for each).
 *
 * Root cause (`App/component/priceGridGrouping.ts`, shared across EVERY
 * brand, not Pianca-specific): `buildSizeColumns` unconditionally SKIPPED
 * every row with `size === null` when building the grid's own size
 * columns. That's correct when a group MIXES sized and dimensionless
 * rows, but wrong when EVERY row in a group has no size at all -- a real,
 * common shape for flat single-price accessories with no L/H/P
 * dimensions. Skipping all of them left the group with ZERO columns, and
 * `VariantTable` renders one `<td>` per column -- with none, the row's
 * own price has nowhere to display at all.
 *
 * **User called this the most serious finding of the whole session, and
 * asked 2 things directly: how long was it live, and add a check that
 * catches this FAILURE CLASS across the whole catalog, not just spot
 * checks.**
 *
 * How long: `git log --follow` on this component's own history shows the
 * ORIGINAL baseline implementation (commit 8945d70, 2026-08-04, the very
 * first version of this file) already handled a null size correctly --
 * `sizes = [...new Set(rows.map(r => r.size))]` included `null` as one of
 * the Set's own distinct values, rendering a real "—"-labelled column for
 * it. The bug was introduced LATER, in commit 803d5a0 (2026-08-29
 * 05:33:54, the Venere same-size-different-code fix, which extracted this
 * logic into `priceGridGrouping.ts` and added `if (r.size === null)
 * continue;` while fixing a DIFFERENT, real problem) -- confirmed via
 * `git show 803d5a0` diffing both the removed and added lines directly,
 * not inferred. Fixed here 2026-09-01 (commit 931c491) -- roughly 3 days
 * of live exposure, not "since the app's inception" as first assumed.
 *
 * Catalog-wide blast-radius scan (all 6 brands' own `prices.json`,
 * counting every (product, model_variant, variant_context) group where
 * EVERY row has size=null): Pianca 65 products / 294 groups, Bolzan 44,
 * Bonaldo 34, Varaschini 645, Ditre Italia 5, Cattelan Italia 0 -- 793
 * products total, silently showing "-" instead of their own real price.
 *
 * SECOND bug, found while verifying the first fix across brands (not
 * assumed safe from Pianca alone): a naive single hardcoded "Price"
 * column collapses multiple DIFFERENT codes sharing one null-size group
 * onto the SAME cell -- Bolzan's own "Awase" has 5 genuinely different
 * codes (RPFL/RPFM/RPFF/RPFG/RPFP, different bed-frame widths) all
 * sharing fabric_tier=null AND size=null, the EXACT same collision shape
 * as this file's own original Venere bug (see check_pianca_size_collision
 * .ts), just on the null-size axis instead of a real one. Fixed by
 * folding the null-size case into the SAME (size, code)-collision logic
 * that already protects real sizes (a dimensionless row's grouping key
 * is just the FLAT_PRICE_COLUMN_KEY sentinel instead of its own size
 * string) rather than a separate hardcoded branch -- one code path, not
 * two that could drift apart.
 *
 * `runExhaustiveSweep()` below is the check the user specifically asked
 * for: reads every brand's own `prices.json` DIRECTLY (no HTTP, no dev
 * server needed for this part -- pure in-memory JS against the same
 * production `buildVariantGroups`/`buildSizeColumns`/`findCell` the UI
 * actually renders with), groups every product's rows exactly the way the
 * UI does, and asserts EVERY row in EVERY group of EVERY product across
 * ALL 6 brands is reachable via the grid traversal. This is NOT a sample
 * -- it is the exhaustive, whole-catalog version of the exact check that
 * would have caught this bug the moment it was introduced, so this
 * specific failure class (rows present in prices.json, invisible in the
 * rendered grid) can never silently recur for ANY product, not just the
 * ones already known to be affected. The live-API CASES below stay too,
 * since they additionally prove the full request/response pipeline (not
 * just the pure grouping functions) for a representative product per
 * brand.
 *
 * The FIRST run of the exhaustive sweep found 104 products failing --
 * every single failing row was `ambiguous: true`, the SAME code sharing
 * one (tier, size) cell with a genuinely conflicting price from this
 * catalog's own PRE-EXISTING ambiguous-price safety net (confirmed via
 * direct source check, e.g. Bolzan's "Flag" prints code "+100" twice
 * with 2 different real prices, "+100" and "+200" -- a real source-data
 * conflict, not an extraction bug). That safety net's own job is to make
 * such a row deliberately NOT resolve to one confident cell (the UI
 * shows a conflict warning instead of guessing) -- a different, already-
 * correct behavior from the null-size bug's own symptom (an
 * UNAMBIGUOUS real price becoming completely invisible). `findLostRows`
 * now excludes `ambiguous: true` rows from the "must match my own exact
 * price" requirement for exactly this reason -- see its own comment.
 *
 * RUN WITH: npm run check-null-size-price-grid
 * Requires the dev server running (npm run dev:server) for the CASES/
 * synthetic sections; the exhaustive sweep itself does not.
 */

import fs from 'fs';
import path from 'path';
import { buildVariantGroups, buildSizeColumns, findCell, FLAT_PRICE_COLUMN_KEY } from '../component/priceGridGrouping';
import type { PriceRow } from '../component/priceGridGrouping';

const BASE_URL = process.env.REGRESSION_BASE_URL || 'http://localhost:3000';
const ROOT = path.join(__dirname, '..', '..');
const ALL_BRANDS = ['Bolzan', 'Bonaldo', 'Cattelan Italia', 'Ditre Italia', 'Pianca', 'Varaschini'];

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

/** Every row in `rows` must be reachable via the exact grid traversal the
 * UI uses -- a null-size row's grouping key is FLAT_PRICE_COLUMN_KEY,
 * the same unification buildSizeColumns itself uses internally, so this
 * one lookup covers both real-size and dimensionless rows identically.
 *
 * `ambiguous: true` rows are deliberately EXCLUDED from the "must match
 * MY OWN exact price" requirement -- found running the exhaustive sweep
 * for the first time, 2026-09-01: 104 products failed on the very first
 * run, but EVERY failing row was `ambiguous: true`, the SAME code sharing
 * one (tier, size) cell with a genuinely conflicting price from this
 * catalog's own pre-existing ambiguous-price safety net (confirmed via
 * direct source check, e.g. Bolzan's "Flag" has code "+100" printed
 * twice with prices "+100" and "+200" -- a real source-data conflict,
 * not an extraction bug). That safety net's own job is precisely to make
 * such a row NOT resolve to one specific confident cell (the UI shows a
 * conflict warning/image instead of guessing) -- this is by-design
 * different from the null-size bug's own symptom (a real, UNAMBIGUOUS
 * price becoming completely invisible, zero columns at all). Applying
 * the null-size fix's "must be reachable" bar to an intentionally-
 * unresolvable ambiguous row would be flagging correct, already-shipped
 * behavior as a regression. */
function findLostRows(rows: PriceRow[]): PriceRow[] {
  const lost: PriceRow[] = [];
  for (const group of buildVariantGroups(rows)) {
    const columns = buildSizeColumns(group.rows);
    for (const row of group.rows) {
      if (row.ambiguous) continue;
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

interface SweepFailure {
  brand: string;
  product: string;
  lostCount: number;
  totalRows: number;
  sample: PriceRow[];
}

/** The exhaustive, whole-catalog version of `findLostRows` -- reads every
 * brand's own `prices.json` directly and asserts every row of every
 * product is reachable via the grid traversal. This is what actually
 * catches "product has real price rows but renders zero visible columns"
 * as a FAILURE CLASS, not a list of already-known offenders: any future
 * product, in any brand, that ends up with an unreachable row for ANY
 * reason (not just size===null -- any grouping/column mismatch this
 * traversal can't resolve) fails this check the moment it's introduced,
 * the same day `regression:full` next runs, rather than waiting for a
 * user to spot-check that one specific page against its source image. */
function runExhaustiveSweep(): SweepFailure[] {
  const failures: SweepFailure[] = [];
  for (const brand of ALL_BRANDS) {
    const pricesPath = path.join(ROOT, 'data', brand, 'prices.json');
    if (!fs.existsSync(pricesPath)) {
      failures.push({ brand, product: '(missing prices.json)', lostCount: -1, totalRows: 0, sample: [] });
      continue;
    }
    const allRows: PriceRow[] = JSON.parse(fs.readFileSync(pricesPath, 'utf-8'));
    const byProduct = new Map<string, PriceRow[]>();
    for (const r of allRows) {
      if (!byProduct.has(r.product_name)) byProduct.set(r.product_name, []);
      byProduct.get(r.product_name)!.push(r);
    }
    for (const [product, rows] of byProduct) {
      const lost = findLostRows(rows);
      if (lost.length > 0) {
        failures.push({ brand, product, lostCount: lost.length, totalRows: rows.length, sample: lost.slice(0, 2) });
      }
    }
  }
  return failures;
}

interface Case {
  id: string;
  brand: string;
  product: string;
  note: string;
}

/** One real, live-verified product per affected brand -- not exhaustive
 * (793 products found affected catalog-wide), a representative spot
 * check per brand plus the specific product that exposed each of the
 * 2 root causes. */
const CASES: Case[] = [
  {
    id: 'pianca-dedalo-kit-luce-exact-repro',
    brand: 'Pianca',
    product: 'Dedalo (Progetti 06-07)',
    note: 'The exact reported repro -- 3 separate model_variant groups (con telecomando/con applicazione/none), one code each, all size=null. The simple, single-code-per-group case the first fix (a hardcoded flat column) already handled correctly on its own.',
  },
  {
    id: 'bolzan-awase-5-code-null-size-collision',
    brand: 'Bolzan',
    product: 'Awase',
    note: 'The 2nd bug found while verifying the fix across brands: 5 genuinely different codes (RPFL/RPFM/RPFF/RPFG/RPFP) share ONE group with size=null AND fabric_tier=null -- a naive single flat column would silently show only the first and drop the other 4, same shape as the original Venere same-size-different-code bug. Requires the unified (size-or-flat, code)-collision logic, not just a bare flat column.',
  },
  {
    id: 'bonaldo-ax-spot-check',
    brand: 'Bonaldo',
    product: 'AX',
    note: 'Cross-brand spot check -- confirms the shared component fix isn\'t accidentally Pianca-scoped.',
  },
  {
    id: 'ditre-alar-spot-check',
    brand: 'Ditre Italia',
    product: 'Alar',
    note: 'Cross-brand spot check, a product with 10 null-size rows spread across several groups.',
  },
];

/** Synthetic edge-case battery, run without any HTTP call -- exercises
 * shapes a live spot-check can't cheaply guarantee stay covered (e.g. a
 * null-size row mixed into an OTHERWISE fully-sized group, which none of
 * the 4 live cases above happen to hit). */
function runSyntheticCases(): string[] {
  const failures: string[] = [];
  const mk = (over: Partial<PriceRow>): PriceRow => ({
    product_name: 'Synthetic', model_variant: null, variant_context: null,
    size: null, fabric_tier: null, tier_label: null, code: null,
    price_eur: '0', ambiguous: false, ...over,
  });

  // Case A: a MIX of one real-size row and one flat (size=null) row in
  // the same group -- the flat row must still get its own column, not
  // be silently skipped just because its siblings have real sizes.
  {
    const rows = [
      mk({ code: 'X1', size: '100×50', price_eur: '111' }),
      mk({ code: 'X2', size: null, price_eur: '222' }),
    ];
    const lost = findLostRows(rows);
    if (lost.length !== 0) failures.push(`[synthetic-mixed-size-and-flat] expected 0 lost, got ${lost.length}`);
  }

  // Case B: 2 different codes, same tier, both size=null -- the exact
  // Awase collision shape, minimal repro.
  {
    const rows = [
      mk({ code: 'Y1', size: null, fabric_tier: 'Tier A', price_eur: '333' }),
      mk({ code: 'Y2', size: null, fabric_tier: 'Tier A', price_eur: '444' }),
    ];
    const lost = findLostRows(rows);
    if (lost.length !== 0) failures.push(`[synthetic-null-size-code-collision] expected 0 lost, got ${lost.length}`);
    const columns = buildSizeColumns(rows);
    if (columns.length !== 2) failures.push(`[synthetic-null-size-code-collision] expected 2 distinct columns, got ${columns.length}`);
  }

  // Case C: single code, single tier, size=null -- the plain Dedalo
  // shape, must resolve to exactly 1 column, not split unnecessarily.
  {
    const rows = [mk({ code: 'Z1', size: null, price_eur: '555' })];
    const columns = buildSizeColumns(rows);
    if (columns.length !== 1) failures.push(`[synthetic-single-flat-row] expected exactly 1 column, got ${columns.length}`);
    const lost = findLostRows(rows);
    if (lost.length !== 0) failures.push(`[synthetic-single-flat-row] expected 0 lost, got ${lost.length}`);
  }

  return failures;
}

async function main() {
  const failures: string[] = [];

  // Exhaustive sweep first -- pure in-memory, no server needed, and this
  // is the check the user specifically asked for: every product, every
  // brand, not a sample. Run before the live-API section so a sweep
  // failure is reported even if the dev server happens to be down.
  const sweepFailures = runExhaustiveSweep();
  const sweepTotalProducts = ALL_BRANDS.reduce((sum, brand) => {
    const p = path.join(ROOT, 'data', brand, 'prices.json');
    if (!fs.existsSync(p)) return sum;
    const rows: PriceRow[] = JSON.parse(fs.readFileSync(p, 'utf-8'));
    return sum + new Set(rows.map(r => r.product_name)).size;
  }, 0);
  if (sweepFailures.length > 0) {
    for (const f of sweepFailures) {
      failures.push(`[exhaustive-sweep] ${f.brand} / "${f.product}" -- ${f.lostCount}/${f.totalRows} row(s) unreachable via the grid traversal: ${JSON.stringify(f.sample)}`);
    }
    console.log(`[exhaustive sweep, all brands]`.padEnd(45), `FAIL -- ${sweepFailures.length} product(s) with unreachable rows (of ${sweepTotalProducts} checked)`);
  } else {
    console.log(`[exhaustive sweep, all brands]`.padEnd(45), `ok  (${sweepTotalProducts} products checked, 0 with unreachable rows)`);
  }

  const probe = await postChat('Pianca', 'ping');
  if (probe.error) {
    console.error(`\nCannot reach ${BASE_URL}/api/catalog/chat (${probe.error}).`);
    console.error('Start the server first: npm run dev:server\n');
    process.exit(1);
  }

  failures.push(...runSyntheticCases());
  console.log(`[synthetic edge cases]`.padEnd(45), failures.length === sweepFailures.length ? 'ok' : `FAIL (${failures.length - sweepFailures.length})`);

  for (const c of CASES) {
    const resp = await postChat(c.brand, `give all ${c.product} prices`);
    const rows = (resp.matches || []).filter(m => m.product_name === c.product);
    if (rows.length === 0) {
      failures.push(`[${c.id}] expected rows for "${c.product}" (${c.brand}), got 0 (status=${resp.status || resp.error})`);
      console.log(`[${c.id.padEnd(40)}] FAIL -- no rows returned`);
      continue;
    }
    const nullSizeCount = rows.filter(r => r.size === null).length;
    const lost = findLostRows(rows);
    if (lost.length > 0) {
      failures.push(`[${c.id}] ${lost.length} row(s) unreachable via the grid traversal: ${JSON.stringify(lost.slice(0, 3))}`);
      console.log(`[${c.id.padEnd(40)}] FAIL -- ${lost.length}/${rows.length} rows lost`);
    } else {
      console.log(`[${c.id.padEnd(40)}] ok  (${rows.length} rows, ${nullSizeCount} null-size, 0 lost)`);
    }
    await new Promise(r => setTimeout(r, 80));
  }

  console.log('\n' + '='.repeat(70));
  console.log(`Exhaustive sweep: ${sweepTotalProducts} products checked across ${ALL_BRANDS.length} brands, ${sweepFailures.length} with unreachable rows  <-- must be 0`);
  console.log(`Live-API cases: ${CASES.length + 1}`);
  console.log(`Total failures: ${failures.length}  <-- must be 0`);
  if (failures.length > 0) {
    console.log('\nFAILURES:');
    failures.forEach(f => console.log(`  ${f}`));
    console.log('\nEXIT 1: null-size price-grid row loss regressed.');
    process.exit(1);
  }
  console.log('\nEXIT 0: every product in every brand\'s prices.json has every row reachable via the price grid (exhaustive), plus the live API pipeline confirmed for a spot-checked product per brand -- this failure class (real price rows silently invisible in the rendered grid) cannot recur undetected.');
}

main();
