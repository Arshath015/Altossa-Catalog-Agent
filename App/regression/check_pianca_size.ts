/**
 * regression/check_pianca_size.ts
 * ---------------------------------
 * Permanent, GATING check for a real bug found via live testing 2026-08-25:
 * Pianca's `size` (L/H/P dimensions) field was hardcoded to null across
 * every parser shape except Primo's -- 11,567 of 11,586 Pianca rows had no
 * size data at all, even though `parse_file_pianca_shape_a` was reading the
 * L/H/P columns off the source page and then actively discarding them
 * (they were only being stripped off the model_variant label, never kept).
 * User-reported: Cora's "Sedia con gambe" (01173, L45×H80×P50) and "Sedia
 * con slitta" (01174, L48×H80×P49) were otherwise indistinguishable in the
 * UI beyond price -- a real order needs both the code AND the size.
 *
 * Fixed for Shape A only in this pass (62% of all Pianca rows, ~7,208 --
 * the other 9 shapes hardcoding `"size": None` are tracked as separate
 * follow-up work, not fixed here, per this project's standing practice of
 * verifying one shape fully before generalizing, same as Cora/SIPARIO/
 * Composizioni Catalogo).
 *
 * The 3 dimension columns are NOT always all present on a given row --
 * confirmed via Duo's own real page image (duo_p151-151.jpg): Cuscinetti
 * (round cushion) rows print only L and H, leaving P genuinely blank in
 * the source table. Since pdftotext -layout only omits the token for an
 * empty cell, a short run of trailing numbers is always a PREFIX of
 * [L, H, P] (L and H present, P dropped) -- confirmed directly against the
 * image, not assumed to be a suffix (which would have wrongly labeled the
 * 2 real values as H/P instead of L/H). Cases below cover both the
 * standard 3-dimension row and this 2-dimension edge case explicitly.
 *
 * RUN WITH: npm run check-pianca-size
 * Requires the dev server running (npm run dev:server).
 */

const BASE_URL = process.env.REGRESSION_BASE_URL || 'http://localhost:3000';

interface PriceRow {
  product_name: string;
  code: string | null;
  size: string | null;
  price_eur: string;
}
interface ChatResponse {
  status?: string;
  matches?: PriceRow[];
  error?: string;
}

interface Case {
  id: string;
  brand: string;
  query: string;
  productName: string;
  code: string;
  expectedSize: string;
  note: string;
}

const CASES: Case[] = [
  {
    id: 'esse-3dim',
    brand: 'Pianca',
    query: 'esse price',
    productName: 'Esse',
    code: '01187',
    expectedSize: '48×78×53',
    note: 'Standard 3-dimension Shape A row -- source text (esse.txt): "non sfoderabile 48 78 53 01187 512 564 589 666 717 922". One of the 2 user-reported products (Cora/Esse) whose UI never showed L/H/P before this fix.',
  },
  {
    id: 'gamma-3dim',
    brand: 'Pianca',
    query: 'gamma price',
    productName: 'Gamma',
    code: '01164',
    expectedSize: '53×76×56',
    note: 'Second already-"done" product spot-checked for the systemic-scope claim (not just Cora/Esse) -- source text (gamma.txt): "non sfoderabile 53 76 56 01164 487 509 531 574 661 835".',
  },
  {
    id: 'alunna-3dim',
    brand: 'Pianca',
    query: 'alunna price',
    productName: 'Alunna',
    code: '011A',
    expectedSize: '55×77×53',
    note: 'Third already-"done" product spot-checked -- source text (alunna.txt): "con gambe Laccato Opaco 55 77 53 011A 442 460 478 531 619 752".',
  },
  {
    id: 'duo-2dim-cuscinetto',
    brand: 'Pianca',
    query: 'duo price',
    productName: 'Duo',
    code: '99DU735',
    expectedSize: '60×35',
    note: 'The P-genuinely-blank edge case -- confirmed visually against duo_p151-151.jpg that this Cuscinetti (round cushion) row prints only L=60/H=35 with NO third dimension column at all, not a parsing gap. Guards against a future change reintroducing the wrong trailing-alignment assumption (H/P instead of L/H) for short dimension runs.',
  },
  {
    id: 'duo-3dim-divano',
    brand: 'Pianca',
    query: 'duo price',
    productName: 'Duo',
    code: 'D9DU194',
    expectedSize: '194×88×107',
    note: 'Same product as duo-2dim-cuscinetto, different table section (Divani) -- confirms the fix handles BOTH row shapes correctly within one product/file, not just in isolation.',
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
  const probe = await postChat(CASES[0].brand, 'ping');
  if (probe.error) {
    console.error(`\nCannot reach ${BASE_URL}/api/catalog/chat (${probe.error}).`);
    console.error('Start the server first: npm run dev:server\n');
    process.exit(1);
  }

  const failures: string[] = [];

  for (const c of CASES) {
    const resp = await postChat(c.brand, c.query);
    const row = (resp.matches || []).find(m => m.product_name === c.productName && m.code === c.code);
    if (!row) {
      failures.push(`[${c.id}] "${c.query}" -- expected code "${c.code}" for "${c.productName}" in matches, got none (status=${resp.status || resp.error})`);
      console.log(`[${c.id.padEnd(24)}] FAIL -- code not found`);
      continue;
    }
    const ok = row.size === c.expectedSize;
    if (!ok) {
      failures.push(`[${c.id}] "${c.query}" code=${c.code} -- expected size "${c.expectedSize}", got "${row.size}". ${c.note}`);
    }
    console.log(`[${c.id.padEnd(24)}] ${ok ? 'ok' : 'FAIL'}  code=${c.code}  size="${row.size}"`);
    await new Promise(r => setTimeout(r, 80));
  }

  console.log('\n' + '='.repeat(70));
  console.log(`Total cases: ${CASES.length}`);
  console.log(`Failures: ${failures.length}  <-- must be 0`);
  if (failures.length > 0) {
    console.log('\nFAILURES:');
    failures.forEach(f => console.log(`  ${f}`));
  }

  if (failures.length > 0) {
    console.log('\nEXIT 1: Pianca Shape A size extraction regressed for one or more product.');
    process.exit(1);
  }
  console.log('\nEXIT 0: Pianca Shape A size (L/H/P) values match source exactly, including the 2-dimension edge case.');
}

main();
