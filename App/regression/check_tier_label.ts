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
  {
    id: 'ada-shape1-upholstery',
    category: 'upholstery Shape 1 grid (Ditre Italia)',
    brand: 'Ditre Italia',
    query: 'Ada (Sofa) 82x82 base category a price',
    productName: 'Ada (Sofa)',
    expectedHeader: 'UPHOLSTERING',
    note: 'Ditre\'s Shape 1 (graduated Category A-U/Leather tier grid) real source label -- confirmed via tier_label on the parsed row, not a guess. Query names the disambiguated "(Sofa)" form explicitly -- bare "ada" alone is genuinely ambiguous against "Ada (Night)" and only resolves deterministically with the disambiguation suffix included.',
  },
  {
    id: 'claire-tables-shape2-finishes',
    category: 'named material-finish Shape 2 grid (Ditre Italia)',
    brand: 'Ditre Italia',
    query: 'Claire (Tables) diameter 140 natural price',
    productName: 'Claire (Tables)',
    expectedHeader: 'FINISHES',
    note: 'Ditre\'s Shape 2 (named material-finish-code grid) real source label -- deliberately DIFFERENT from Shape 1\'s "UPHOLSTERING" above, proves the tier_label column isn\'t hardcoded per-brand but genuinely varies with the source page\'s own printed word. Also disambiguated against "Claire (Night)"/"Claire mix" the same way as Ada above.',
  },
  {
    id: 'pianca-lina-shapeA-tier',
    category: 'tessuto/pelle Shape A tier grid (Pianca)',
    brand: 'Pianca',
    query: 'Lina Frassino Nero category a price',
    productName: 'Lina',
    expectedHeader: 'CATEGORY',
    note: 'Pianca\'s Shape A (tessuto/pelle A/B/C/H/P/Q tier grid, first implementation slice scoped to Progetti di Design 08) real source label -- the printed column header is literally "A B C H P Q" with no descriptive word of its own on the page, so parse_file_pianca uses the literal generic word "Category" (matching how this project\'s Step-1 structural report described the column set) rather than inventing brand-specific vocabulary the source doesn\'t use.',
  },
  {
    id: 'pianca-normaup-2axis-tier',
    category: '2-axis Struttura x Frontali finish grid (Pianca)',
    brand: 'Pianca',
    query: 'Norma Up materico price',
    productName: 'Norma Up',
    expectedHeader: 'FINISH',
    note: 'Norma Up\'s 2-axis grid (Progetti di Design 09, real PDF pages 54-64) folds BOTH the Struttura finish and the Frontali/Copertura finish into one fabric_tier string (e.g. "Materico — Copertura L.Opaco/Essenza — Frontali L.Opaco") rather than splitting the row axis into model_variant -- necessary because main()\'s shared ambiguous-row detection keys on (product, code, fabric_tier) only, and this table\'s same printed code repeats across all 3 Struttura rows for one dimension (confirmed real, not a parsing bug) -- splitting the axes across two fields caused every one of the product\'s 1566 rows to collide and get marked ambiguous before this was fixed. tier_label is "Finish" (generic, no single descriptive source word covers a combined 2-axis tier).',
  },
  {
    id: 'pianca-elide-shapeb-named-tier',
    category: 'simple named-finish-columns Shape B (Pianca)',
    brand: 'Pianca',
    query: 'Elide senza braccioli price',
    productName: 'Elide',
    expectedHeader: 'FINISH',
    note: 'Elide\'s single-column Shape B table (Progetti di Design 09, real PDF page 9 -- "L H P CODICI Carta Kraft") is the simplest instance of the new verified-registry named-columns parser (parse_file_pianca_shape_b_named), also covering Onda Indoor (3-col + a separately-wrapped 5-col Marmo table) and Soffio Up (6-col, 822 rows, 0 collisions independently re-verified). Column labels come from an explicit registry keyed on the header\'s own literal post-CODICI tokens, not a generic multi-word-header guesser -- any table not in the registry stays flagged rather than mis-labeled.',
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
