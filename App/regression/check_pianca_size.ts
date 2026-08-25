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
 * Fixed for Shape A first (62% of all Pianca rows, ~7,208), then the
 * other 9 shapes in 3 batched groups of 3 (2026-08-25), same day, once
 * Shape A's own technique was confirmed to transfer cleanly:
 *   - Group 1 (shape_a_pelle, tavoli_piano, tavoli_metallo): byte-identical
 *     row convention to base Shape A, same fix ported unchanged.
 *   - Group 2 (cora, shape_b_named, flat_price): same generic capture, but
 *     dimension-column COUNT varies per table (0-3) rather than always 3 --
 *     verified against Elide (3-dim), Soffio Up (2-dim closed/open-length
 *     pair, NOT L/H), and Mensole legno per boiserie (1-dim). Also fixed a
 *     real pre-existing bug found while doing this: flat_price's label had
 *     NO digit-stripping at all before, so dimension digits were silently
 *     glued onto real label text (e.g. Geometrika's "con luce LED 7.7 W 80
 *     70 2.6" instead of "con luce LED 7.7 W").
 *   - Group 3 (norma_up_2axis, mambo_2axis, siviglia_matrix): initially
 *     looked structurally ambiguous (2 inline numbers per row, unclear
 *     whether they were L/H, H/P, or something else) -- resolved by
 *     finding a real page image for Siviglia (siviglia_p71-71.jpg, the one
 *     sibling among the 3 with actual page images available) and checking
 *     pixel alignment directly: the section-heading number (e.g. "81 (1
 *     vano anta)") is L, and the 2 inline per-row numbers are H then P.
 *     Applied to all 3 siblings since they share the byte-identical "L
 *     [gap] H P CODICI" header and row convention.
 *
 * The dimension columns are NOT always all present on a given row --
 * confirmed via Duo's own real page image (duo_p151-151.jpg): Cuscinetti
 * (round cushion) rows print only L and H, leaving P genuinely blank in
 * the source table. Since pdftotext -layout only omits the token for an
 * empty cell, a short run of trailing numbers is always a PREFIX of
 * [L, H, P] (L and H present, P dropped) -- confirmed directly against the
 * image, not assumed to be a suffix (which would have wrongly labeled the
 * 2 real values as H/P instead of L/H). Cases below cover the standard
 * 3-dimension row, the 2-dimension edge case, the 2-axis H/P-only case,
 * and Cora specifically (the product that started this whole
 * investigation -- confirmed now showing size, byte-exact).
 *
 * NOT fully closed: ~2,540 of 11,586 rows still have size=null after all
 * 11 shapes are covered, but 2,435 of those (96%) are legitimately
 * dimensionless "rivestimento" (fabric-cover-only) companion rows -- e.g.
 * Duo's own "rivestimento F9DU735 40 47 58 83 109 169" line genuinely has
 * no L/H/P printed at all, confirmed real, not a parser gap. The
 * remaining ~105 are a long tail of small accessory rows (remote
 * controls, corner-piece hardware, etc.) with no meaningful size
 * dimension to capture.
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
  {
    id: 'cora-the-original-bug',
    brand: 'Pianca',
    query: 'cora price',
    productName: 'Cora',
    code: '01173',
    expectedSize: '45×80×50',
    note: 'Cora is the product that started the whole size investigation (user-reported: "Sedia con gambe" 01173 vs "Sedia con slitta" 01174 were indistinguishable beyond price). Cora has its OWN dedicated mixed-shape parser (parse_file_pianca_cora), not base Shape A -- this case guards that shape specifically, not just Shape A. Source (cora.txt): "seduta legno 45 80 50 01173 287 310 - - - - - -".',
  },
  {
    id: 'group1-shape-a-pelle',
    brand: 'Pianca',
    query: 'mambo progetti 09 price',
    productName: 'Mambo (Progetti 09)',
    code: '06MA36',
    expectedSize: '66',
    note: 'Group 1 (shape_a_pelle, Mambo\'s "+ Pelle Sint." 7-column variant). Deliberately a single-value case: this row\'s own line only carries "66" (H) before the code -- source (mambo_progetti_09.txt): "cad. anta battente 66 06MA36 196 ...". The matching "L" value (30) prints on a SEPARATE wrapped line above/below this row block and is NOT captured (same class of limitation as Duo\'s P-blank case) -- verified this is a real, non-fabricated partial value, not a parsing error.',
  },
  {
    id: 'group1-tavoli-caption-noise',
    brand: 'Pianca',
    query: 'mono price',
    productName: 'Mono (Tavolini)',
    code: '24MCC4',
    expectedSize: '30×40×30',
    note: 'Group 1 (tavoli_metallo, shared Tavoli helper). Guards the confirmed caption-noise edge case: source line (mono_tavolini.txt) is "40 / 50 ... 30 40 30 24MCC4 460 506" -- the "40 / 50" fragment is an unrelated NEARBY diagram caption that bleeds onto this line from further left; the real L/H/P (30, 40, 30) are the 3 tokens closest to the code, which the cap-at-3-pops rule correctly isolates from the caption noise.',
  },
  {
    id: 'group2-shapeb-named-elide',
    brand: 'Pianca',
    query: 'elide price',
    productName: 'Elide',
    code: '01125',
    expectedSize: '50×81×61',
    note: 'Group 2 (shape_b_named). Standard 3-dim case for this shape -- source (elide.txt): "50 81 61 01125 570".',
  },
  {
    id: 'group2-shapeb-named-soffio-2dim-pair',
    brand: 'Pianca',
    query: 'soffio up price',
    productName: 'Soffio Up',
    code: 'T0SA08C',
    expectedSize: '110×170',
    note: 'Group 2 (shape_b_named), the "not really L/H" edge case: this table\'s 2 header columns are "L chiuso"/"L aperto" (closed/open extended-table length), not L/H at all -- source (soffio_up.txt): "con allunga intera 110 170 T0SA08C ...". The generic capture still works correctly since it just records whatever 2 real numbers are present in printed order, without assuming they mean L/H specifically.',
  },
  {
    id: 'group2-flatprice-geometrika',
    brand: 'Pianca',
    query: 'geometrika price',
    productName: 'Geometrika',
    code: '35G1D',
    expectedSize: '80×70×2.6',
    note: 'Group 2 (flat_price). Also guards the real pre-existing label-pollution bug fixed alongside this: model_variant must be the clean "con luce LED 7.7 W", not "con luce LED 7.7 W 80 70 2.6" (source: geometrika.txt).',
  },
  {
    id: 'group3-norma-up-2axis',
    brand: 'Pianca',
    query: 'norma up materico price',
    productName: 'Norma Up',
    code: '00KFFC',
    expectedSize: '121×35',
    note: 'Group 3 (norma_up_2axis). H,P mapping confirmed via Siviglia\'s real page image (this shape\'s own page has no image available) -- source (norma_up.txt): "Materico 121 35 00KFFC ...", with L=102 (the "102 (2 vani anta)" section heading) never appearing on the row\'s own line.',
  },
  {
    id: 'group3-mambo-2axis',
    brand: 'Pianca',
    query: 'mambo progetti 09 price',
    productName: 'Mambo (Progetti 09)',
    code: '00M3FE',
    expectedSize: '126×35',
    note: 'Group 3 (mambo_2axis, Mambo\'s OWN main 2-axis grid -- different function from group1-shape-a-pelle above, same product). Source (mambo_progetti_09.txt): "Materico 126 35 00M3FE ...".',
  },
  {
    id: 'group3-siviglia-matrix',
    brand: 'Pianca',
    query: 'siviglia materico price',
    productName: 'Siviglia',
    code: '00J4G8',
    expectedSize: '129×49',
    note: 'Group 3 (siviglia_matrix) -- the shape whose real page image (siviglia_p71-71.jpg) resolved the whole family\'s H/P-vs-L ambiguity via direct pixel-level confirmation. Source (siviglia.txt, section "81 (1 vano anta)"): "Materico 129 49 00J4G8 3.924 ...".',
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
