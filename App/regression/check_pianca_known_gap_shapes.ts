/**
 * regression/check_pianca_known_gap_shapes.ts
 * ---------------------------------
 * Permanent, GATING check for the batched Pianca known_gap parser-shape
 * builds (2026-08-25 onward), following the full known_gap inventory
 * (~406 real entries, grouped by structural shape -- dims present + real
 * trailing price-column count per row, not by header label text, since
 * label wrapping is wildly inconsistent: same line, next line, one name
 * per line, or even printed BEFORE the CODICI line).
 *
 * Batch 1a -- dims+1 (bare "CODICI" header, single flat price column),
 * ~121 products: widened `_pianca_is_flat_price_header` to also accept a
 * bare CODICI tail (previously required the literal word "Prezzo" after
 * it). Deliberately NOT a blanket widening -- the header text alone
 * cannot tell a genuine 1-column table (Contralto) apart from a wider
 * table whose OWN column labels simply wrapped onto a different physical
 * line (Icaro: 2 columns, Ettorino: 4, Pedane: 3 -- all share the exact
 * same bare-CODICI-on-its-own-line header shape). Safety is enforced in
 * the ROW scan instead: a row is only accepted if it has EXACTLY ONE
 * trailing price-cell token. A real regression risk was found and fixed
 * while building this: Mambo/Siviglia's own "con telecomando"/"con
 * applicazione" accessories use plain 5-digit NUMERIC codes (47100/
 * 47101/47102), which look identical in shape to a price -- a naive
 * "reject if the token before the price is price-shaped" check would
 * have wrongly rejected those already-live rows. Fixed by additionally
 * requiring the rejected token be SHORTER than 4 characters (this file's
 * own CODE_RE floor), since a real code can never be that short, so
 * anything shorter that still looks like a price can only be a genuine
 * second price value, never a code.
 *
 * RUN WITH: npm run check-pianca-known-gap-shapes
 * Requires the dev server running (npm run dev:server).
 */

const BASE_URL = process.env.REGRESSION_BASE_URL || 'http://localhost:3000';

interface PriceRow {
  product_name: string;
  code: string | null;
  price_eur: string;
  size: string | null;
}
interface ChatResponse {
  status?: string;
  matches?: PriceRow[];
  error?: string;
}

interface Case {
  id: string;
  query: string;
  productName: string;
  code: string;
  expectedPrice: string;
  expectedSize?: string;
  note: string;
}

const CASES: Case[] = [
  {
    id: 'dims1-contralto-new',
    query: 'Contralto CollezioneGiorno tavolino alto price',
    productName: 'Contralto (CollezioneGiorno)',
    code: '35LTA',
    expectedPrice: '773',
    expectedSize: '38×65×38',
    note: 'New capture from the dims+1 bare-CODICI widening -- source (contralto_collezionegiorno.txt): "38 65 38 35LTA ... 773".',
  },
  {
    id: 'dims1-composizione-iot-new',
    query: 'Composizione IOT011 (Designbook 2022) price',
    productName: 'Composizione IOT011 (Designbook 2022)',
    code: 'IOT011',
    expectedPrice: '3.361',
    expectedSize: '123×243×45',
    note: 'New capture, a different source file family (Designbook 2022 Composizioni) -- source: "123 243 45 IOT011 3.361".',
  },
  {
    id: 'guard-numeric-code-not-rejected',
    query: 'Mambo progetti 09 con telecomando price',
    productName: 'Mambo (Progetti 09)',
    code: '47101',
    expectedPrice: '186',
    note: 'Regression guard: Mambo\'s own real 5-digit numeric code (47101) must NOT be wrongly rejected by the new "reject if token before price is price-shaped" safety check -- protected by the <4-char floor, since a real code can never be that short.',
  },
];

interface RejectCase {
  id: string;
  productName: string;
  note: string;
}

const REJECT_CASES: RejectCase[] = [
  {
    id: 'guard-icaro-not-corrupted',
    productName: 'Icaro',
    note: 'Icaro shares the exact same bare-CODICI-on-its-own-line header shape as the dims+1 family, but its real rows have 2 trailing prices, not 1 (confirmed: 2 named finish columns, labels wrap to the next line). Must stay at 0 rows (still correctly known_gap for its own real shape), not get a corrupted 1-price capture that silently drops its 2nd column.',
  },
  {
    id: 'guard-ettorino-not-corrupted',
    productName: 'Ettorino',
    note: 'Same class of guard as Icaro -- Ettorino is a real 4-column table (labels print BEFORE the CODICI line) sharing the same bare header shape. Must stay at 0 rows.',
  },
  {
    id: 'guard-pedane-not-corrupted',
    productName: 'Pedane',
    note: 'Same class of guard as Icaro -- Pedane is a real 3-column table. Must stay at 0 rows.',
  },
];

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

async function main() {
  const probe = await postChat('Pianca', 'ping');
  if (probe.error) {
    console.error(`\nCannot reach ${BASE_URL}/api/catalog/chat (${probe.error}).`);
    console.error('Start the server first: npm run dev:server\n');
    process.exit(1);
  }

  const failures: string[] = [];

  for (const c of CASES) {
    const resp = await postChat('Pianca', c.query);
    const row = (resp.matches || []).find(m => m.product_name === c.productName && m.code === c.code);
    if (!row) {
      failures.push(`[${c.id}] "${c.query}" -- expected code "${c.code}" for "${c.productName}" in matches, got none (status=${resp.status || resp.error})`);
      console.log(`[${c.id.padEnd(30)}] FAIL -- code not found`);
      continue;
    }
    const priceOk = row.price_eur === c.expectedPrice;
    const sizeOk = c.expectedSize === undefined || row.size === c.expectedSize;
    const ok = priceOk && sizeOk;
    if (!ok) {
      failures.push(`[${c.id}] "${c.query}" code=${c.code} -- expected price="${c.expectedPrice}"${c.expectedSize ? ` size="${c.expectedSize}"` : ''}, got price="${row.price_eur}" size="${row.size}". ${c.note}`);
    }
    console.log(`[${c.id.padEnd(30)}] ${ok ? 'ok' : 'FAIL'}  code=${c.code}  price=${row.price_eur}  size=${row.size}`);
    await new Promise(r => setTimeout(r, 80));
  }

  for (const c of REJECT_CASES) {
    const resp = await postChat('Pianca', `${c.productName} price`);
    const rows = (resp.matches || []).filter(m => m.product_name === c.productName);
    const ok = rows.length === 0;
    if (!ok) {
      failures.push(`[${c.id}] "${c.productName}" -- expected 0 rows (still correctly known_gap), got ${rows.length}. ${c.note}`);
    }
    console.log(`[${c.id.padEnd(30)}] ${ok ? 'ok' : 'FAIL'}  status=${resp.status}  rows=${rows.length}`);
    await new Promise(r => setTimeout(r, 80));
  }

  console.log('\n' + '='.repeat(70));
  console.log(`Total cases: ${CASES.length + REJECT_CASES.length}`);
  console.log(`Failures: ${failures.length}  <-- must be 0`);
  if (failures.length > 0) {
    console.log('\nFAILURES:');
    failures.forEach(f => console.log(`  ${f}`));
  }

  if (failures.length > 0) {
    console.log('\nEXIT 1: Pianca known_gap shape-batch regressed.');
    process.exit(1);
  }
  console.log('\nEXIT 0: dims+1 (bare-CODICI flat price) batch verified, numeric-code regression guarded, wider-table corruption guarded.');
}

main();
