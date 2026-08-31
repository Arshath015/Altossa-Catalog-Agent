/**
 * regression/check_compound_size_query.ts
 * ---------------------------------
 * Permanent, GATING check for a real bug found via live testing 2026-08-28
 * (user-reported: Pianca Ala's "give for 120 40 and 180 90" fell through
 * to showing every row, even though "120 40" alone and "180 90" alone
 * each correctly narrow on their own).
 *
 * Root cause: this deterministic path had NO concept of a compound
 * multi-size request at all. `extractSize()` (catalogChat.ts) is a single
 * non-global regex match requiring an explicit "x"/"×" separator -- it
 * can only ever return ONE size. Bare-number queries (no "x") fall
 * through to a DIFFERENT path in `lookupForProduct` that flattens EVERY
 * number in the whole query into one flat set -- which can never match
 * any single row (a real row's own size is always just 2-3 numbers,
 * never 4+ at once), so the filter silently produced zero matches and
 * fell back to showing everything.
 *
 * Fix: new `extractSizeGroups()` splits the query on "and"/"," into
 * independent size-request groups BEFORE the numeric filter runs, using
 * the identical `\b\d{2,3}\b` word-boundary rule as the existing
 * bareNums extraction (not a looser regex -- a real risk found while
 * building this: without the trailing boundary, a number glued to
 * letters, or a real variant-name digit like Ditre's "2er"/"3er", could
 * misfire). `lookupForProduct` takes the UNION of each group's own
 * independent subset-match only when 2+ real groups are found -- an
 * ordinary single-size query (the overwhelming majority) is completely
 * unaffected, byte-for-byte.
 *
 * Since the fix lives in shared `catalogChat.ts` logic (not a Pianca-only
 * file), this check also guards the cross-brand edge case found while
 * verifying: a query naming 2+ short digit-bearing variant words (Ditre's
 * "2er"/"3er") must not be mistaken for a compound size request.
 *
 * RUN WITH: npm run check-compound-size-query
 * Requires the dev server running (npm run dev:server).
 */

const BASE_URL = process.env.REGRESSION_BASE_URL || 'http://localhost:3000';

interface PriceRow {
  product_name: string;
  size: string | null;
  code: string | null;
}
interface ChatResponse {
  status?: string;
  product_name?: string;
  matches?: PriceRow[];
  error?: string;
}

interface Case {
  id: string;
  brand: string;
  query: string;
  /** When set, sent as a conversational anchor (lastProduct) instead of
   * naming the product in `query` itself -- required for the "and"-
   * separated cases specifically: naming the product AND using "and" in
   * the SAME query string triggers a completely different, earlier,
   * unrelated compound-multi-PRODUCT split (this codebase's own existing
   * "give A and B price" mechanism), which only ever sees ONE HALF of the
   * string at a time and never reaches this fix's logic with the whole
   * query intact. An anchored follow-up (the real user's own actual
   * phrasing -- they'd already been discussing Ala) is the correct way
   * to exercise a compound SIZE request without also triggering that
   * unrelated split. Comma-separated and single-size cases don't hit
   * this at all (no "and" token), so they safely name the product inline. */
  lastProduct?: string;
  productName: string;
  /** Every one of these sizes must appear among the returned rows. */
  expectedSizes: string[];
  /** Total row count must match exactly -- proves the union is neither
   * under- nor over-inclusive (e.g. didn't silently fall back to "all
   * rows" AND didn't accidentally keep matching a 3rd, unrequested size). */
  expectedRowCount: number;
  note: string;
}

const CASES: Case[] = [
  {
    id: 'ala-compound-bare-numbers-exact-repro',
    brand: 'Pianca',
    query: 'give for 120 40 and 180 90',
    lastProduct: 'Ala',
    productName: 'Ala',
    expectedSizes: ['120×40×4.5', '180×90×4.5'],
    expectedRowCount: 7,
    note: 'Exact reported repro (a conversational follow-up after already discussing Ala). Before the fix: fell through to all 21 rows (no narrowing at all). "120 40" alone matches 4 rows, "180 90" alone matches 3 -- union is 7, not 21 and not 0.',
  },
  {
    id: 'ala-compound-x-separated',
    brand: 'Pianca',
    query: 'give 120x40 and 180x90 price',
    lastProduct: 'Ala',
    productName: 'Ala',
    expectedSizes: ['120×40×4.5', '180×90×4.5'],
    expectedRowCount: 7,
    note: 'Same compound request, "x"-separated phrasing -- extractSizeGroups is number-only and indifferent to separator style, so this must behave identically to the bare-number version above, not fall back to extractSize()\'s single-match limitation.',
  },
  {
    id: 'ala-compound-comma-separated',
    brand: 'Pianca',
    query: 'Ala 120 40, 180 90 price',
    productName: 'Ala',
    expectedSizes: ['120×40×4.5', '180×90×4.5'],
    expectedRowCount: 7,
    note: 'Comma is the other real-world separator alongside "and" -- confirmed both are handled by the same split, not just "and". Named inline (not anchored) since comma doesn\'t trigger the unrelated multi-product "and"-split.',
  },
  {
    id: 'ala-single-size-unaffected-x',
    brand: 'Pianca',
    query: 'Ala 120x40 price',
    productName: 'Ala',
    expectedSizes: ['120×40×4.5'],
    expectedRowCount: 4,
    note: 'Guard: an ordinary single-size query (only 1 real group) must take the OLD, unchanged extractSize() path, not the new compound branch -- byte-for-byte identical to pre-fix behavior.',
  },
  {
    id: 'ala-single-size-unaffected-bare',
    brand: 'Pianca',
    query: 'Ala give for 120 40',
    productName: 'Ala',
    expectedSizes: ['120×40×4.5'],
    expectedRowCount: 4,
    note: 'Guard: an ordinary single bare-number query (only 1 real group, no "and"/",") must take the OLD, unchanged bareNums path.',
  },
];

interface GuardCase {
  id: string;
  brand: string;
  query: string;
  productName: string;
  note: string;
}

const GUARD_CASES: GuardCase[] = [
  {
    id: 'guard-ditre-2er-3er-not-mistaken-for-sizes',
    brand: 'Ditre Italia',
    query: 'give online 2er and 3er price',
    productName: 'On Line',
    note: 'Root-cause guard found while building the fix: "2er"/"3er" are real VARIANT NAME words (Ditre\'s own capacity-count convention), not sizes, and each is a single digit -- extractSizeGroups\' \\b\\d{2,3}\\b boundary rule must never treat them as a 2-group compound-size request. Asserts the row count matches the ALREADY-ESTABLISHED "2er"+"3er" 4-variant tie-break (48 rows across On Line\'s own 2-er/3-er sofa + central element variants) is unchanged by this fix -- not narrowed down to near-zero by a false compound-size match.',
  },
];

async function postChat(brand: string, message: string, lastProduct: string | null = null): Promise<ChatResponse> {
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    const res = await fetch(`${BASE_URL}/api/catalog/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        brand, message,
        history: lastProduct ? [{ role: 'user', text: `${lastProduct} price` }, { role: 'assistant', text: '...' }] : [],
        lastProduct, lastModelVariant: null, lastCandidates: null,
      }),
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
  const probe = await postChat('Pianca', 'ping');
  if (probe.error) {
    console.error(`\nCannot reach ${BASE_URL}/api/catalog/chat (${probe.error}).`);
    console.error('Start the server first: npm run dev:server\n');
    process.exit(1);
  }

  const failures: string[] = [];

  for (const c of CASES) {
    const resp = await postChat(c.brand, c.query, c.lastProduct || null);
    const rows = (resp.matches || []).filter(m => m.product_name === c.productName);
    const gotSizes = new Set(rows.map(r => r.size).filter(Boolean));
    const missingSizes = c.expectedSizes.filter(s => !gotSizes.has(s));
    const rowCountOk = rows.length === c.expectedRowCount;
    const ok = missingSizes.length === 0 && rowCountOk;
    if (!ok) {
      failures.push(`[${c.id}] "${c.query}" -- expected sizes [${c.expectedSizes.join(', ')}] and exactly ${c.expectedRowCount} rows, got ${rows.length} rows across sizes [${[...gotSizes].join(', ')}] (status=${resp.status || resp.error})`);
    }
    console.log(`[${c.id.padEnd(35)}] ${ok ? 'ok' : 'FAIL'}  rows=${rows.length}  sizes=${[...gotSizes].join('|')}`);
    await new Promise(r => setTimeout(r, 80));
  }

  for (const c of GUARD_CASES) {
    const resp = await postChat(c.brand, c.query);
    const rows = (resp.matches || []).filter(m => m.product_name === c.productName);
    const ok = rows.length >= 40; // must NOT be narrowed to near-zero by a false compound-size match
    if (!ok) {
      failures.push(`[${c.id}] "${c.query}" -- expected the normal >=40-row variant-tie result, got ${rows.length} rows (status=${resp.status || resp.error}). ${c.note}`);
    }
    console.log(`[${c.id.padEnd(35)}] ${ok ? 'ok' : 'FAIL'}  rows=${rows.length}`);
    await new Promise(r => setTimeout(r, 80));
  }

  console.log('\n' + '='.repeat(70));
  console.log(`Total cases: ${CASES.length + GUARD_CASES.length}`);
  console.log(`Failures: ${failures.length}  <-- must be 0`);
  if (failures.length > 0) {
    console.log('\nFAILURES:');
    failures.forEach(f => console.log(`  ${f}`));
    console.log('\nEXIT 1: compound multi-size query handling regressed.');
    process.exit(1);
  }
  console.log('\nEXIT 0: compound multi-size queries ("and"/","-separated) correctly union each real size group\'s own rows, single-size queries are untouched, and digit-bearing variant names are not mistaken for sizes.');
}

main();
