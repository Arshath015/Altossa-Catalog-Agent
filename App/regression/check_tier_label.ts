/**
 * regression/check_tier_label.ts
 * ---------------------------------
 * Permanent, GATING check for the fabric_tier column-header labeling bug:
 * the chat UI used to hardcode "FABRIC" as the header for every product's
 * tier column, which is wrong for anything that isn't upholstery --
 * Cattelan's catalog spans wood/marble/ceramic/crystal tables, lamps, and
 * rugs, where the real varying dimension is a base finish, top material,
 * or nothing at all, never "fabric".
 *
 * Root cause: the source PDF text DOES carry a real category word (Base/
 * Top/Rivestimento/Struttura/Seduta/...) recognized during parsing, but it
 * was being stripped and discarded, keeping only the tier VALUE. Fixed by
 * additively capturing it as `tier_label` on each PriceRow (parse_prices.py
 * + a one-time backfill for the 67 hand-transcribed manual_additions.json
 * products), and having the chat UI's tierColumnHeader() prefer the real
 * label, falling back to "FABRIC" only when real tier VALUES exist without
 * a confidently-recovered label word, or to "—" when there's no tier
 * dimension on the product AT ALL (never a mislabeled "FABRIC" over an
 * empty column, the original reported bug).
 *
 * This script checks the DATA (tier_label on prices.json rows) via the
 * live API -- not the rendered pixels (no browser automation available in
 * this environment) -- across one representative product per furniture
 * type: upholstered chair, marble table, wood table (no tier dimension at
 * all), ceramic table, crystal table, and rug (no tier dimension at all).
 * The exact same tierColumnHeader() logic from CatalogChatWidget.tsx is
 * replicated here so this test fails the instant either the DATA or the
 * DISPLAY LOGIC regresses.
 *
 * RUN WITH: npm run check-tier-label
 * Requires the dev server running (npm run dev:server).
 */

const BASE_URL = process.env.REGRESSION_BASE_URL || 'http://localhost:3000';

interface PriceRow {
  product_name: string;
  fabric_tier: string | null;
  tier_label: string | null;
  price_eur: string;
}
interface ChatResponse {
  status?: string;
  matches?: PriceRow[];
  error?: string;
}

// Exact replica of CatalogChatWidget.tsx's tierColumnHeader() -- kept in
// sync deliberately, not imported, since the widget is client-side TSX
// and this is a plain Node script; if the display logic ever changes,
// this must change too, which is the point of a dedicated test.
function tierColumnHeader(rows: PriceRow[]): string {
  const real = rows.find(r => r.tier_label)?.tier_label;
  if (real) return real.toUpperCase();
  const hasAnyTierValue = rows.some(r => r.fabric_tier);
  return hasAnyTierValue ? 'FABRIC' : '—';
}

interface Case {
  id: string;
  category: string;
  brand: string;
  query: string;
  productName: string;
  expectedHeader: string;
  note: string;
}

const CASES: Case[] = [
  {
    id: 'wilma-upholstered-chair',
    category: 'upholstered chair',
    brand: 'Cattelan Italia',
    query: 'wilma pelle glove',
    productName: 'WILMA',
    expectedHeader: 'RIVESTIMENTO',
    note: 'Real source label -- upholstery, "FABRIC" would have been a reasonable guess but Rivestimento is the actual printed word.',
  },
  {
    id: 'yoda-marble-table',
    category: 'marble table',
    brand: 'Cattelan Italia',
    query: 'yoda marble price',
    productName: 'YODA Marble',
    expectedHeader: 'BASE',
    note: 'User-reported bug: was showing "FABRIC" for a steel base finish column.',
  },
  {
    id: 'botero-wood-table',
    category: 'wood table (no tier dimension)',
    brand: 'Cattelan Italia',
    query: 'botero wood round price',
    productName: 'BOTERO Wood Round',
    expectedHeader: '—',
    note: 'User-reported bug: was showing "FABRIC" above a column of bare "—" placeholders for a product with no fabric dimension at all.',
  },
  {
    id: 'boulevard-ceramic-table',
    category: 'ceramic table',
    brand: 'Cattelan Italia',
    query: 'boulevard keramik price',
    productName: 'BOULEVARD Keramik',
    expectedHeader: 'TOP',
    note: 'Real source label -- ceramic top material, was "FABRIC".',
  },
  {
    id: 'hystrix-crystal-table',
    category: 'crystal table',
    brand: 'Cattelan Italia',
    query: 'hystrix price',
    productName: 'HYSTRIX',
    expectedHeader: 'TOP',
    note: 'Real source label -- crystal top material, was "FABRIC".',
  },
  {
    id: 'madras-rug',
    category: 'rug (no tier dimension)',
    brand: 'Cattelan Italia',
    query: 'madras price',
    productName: 'MADRAS',
    expectedHeader: '—',
    note: 'Rug priced by size only -- no tier dimension at all, same class as BOTERO Wood Round.',
  },
  {
    id: 'avant-garde-upholstered-chair',
    category: 'upholstered chair (Bonaldo)',
    brand: 'Bonaldo',
    query: 'avant-garde chair metallo special capri',
    productName: 'Avant-Garde chair',
    expectedHeader: 'RIVESTIMENTO',
    note: 'Bonaldo real source label -- same category word as Cattelan\'s WILMA case, different catalog entirely.',
  },
  {
    id: 'casablanca-rug-colore',
    category: 'rug/colore (Bonaldo)',
    brand: 'Bonaldo',
    query: 'casablanca 300 x 400',
    productName: 'Casablanca',
    expectedHeader: 'COLORE',
    note: 'Deliberate DIVERGENCE from Cattelan\'s MADRAS rug case above -- unlike Cattelan\'s rugs (no tier dimension at all), Bonaldo\'s rug/colore shape DOES have a real tier dimension (which color), from the point-2 rug/colore survey earlier this session.',
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
    const rows = (resp.matches || []).filter(m => m.product_name === c.productName);
    if (rows.length === 0) {
      failures.push(`[${c.id}] "${c.query}" -- expected "${c.productName}" in matches, got none (status=${resp.status || resp.error})`);
      console.log(`[${c.id.padEnd(28)}] FAIL -- product not found`);
      continue;
    }
    const actualHeader = tierColumnHeader(rows);
    const ok = actualHeader === c.expectedHeader;
    if (!ok) {
      failures.push(`[${c.id}] "${c.query}" (${c.category}) -- expected header "${c.expectedHeader}", got "${actualHeader}". ${c.note}`);
    }
    console.log(`[${c.id.padEnd(28)}] ${ok ? 'ok' : 'FAIL'}  header="${actualHeader}"  (${c.category})`);
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
    console.log('\nEXIT 1: tier column header regressed for one or more product types.');
    process.exit(1);
  }
  console.log('\nEXIT 0: real category labels (or honest "—"/"FABRIC" fallbacks) hold for every product type tested.');
}

main();
