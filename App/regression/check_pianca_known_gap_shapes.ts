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
 * Batch 1b -- "Letti" (beds) tier ladder, 9 products (Beta up (Letti),
 * Beta up trasformabile (Letti), Embrace (Letti), Rialto (Letti),
 * Bricola (Letti), Filo, Fushimi, Piumotto, Rada, Dioniso -- plus their
 * (Tatami) siblings): the SAME A/B/C/H/P/Q tier ladder as base Shape A,
 * blocked only because the tier-letter line prints ABOVE the "L P
 * CODICI" line instead of on it, tier count varies per product (Bricola
 * only shows "A B C H", not the full 6), and rows use a bed-specific
 * "WxH" nominal size (e.g. "160x200") instead of plain L/H/P. New
 * parse_file_pianca_letti_tier function, reusing the exact same
 * wildcard-code convention as Enea Up (shape_b_named) for the Tatami
 * variants' own "WZ * F03N"-style codes.
 *
 * A REAL, ALREADY-SHIPPED data-corruption bug was found and fixed while
 * building this: the dims+1 safety check above (reject tokens[-2] if
 * price-shaped and <4 chars) was NOT sufficient -- Amante (a real Letti
 * product) was ALSO getting 3 of its 6-tier rows wrongly captured by
 * flat_price, because the SECOND-TO-LAST tier value in those rows
 * ("3.710") is 5 characters long (the "." thousands separator pushes it
 * past the old 4-char floor), long enough to slip past the length check
 * as if it might be a valid code. Fixed by checking _PIANCA_CODE_RE
 * directly instead of a hand-rolled length floor: a real code can never
 * contain a "." (CODE_RE's own character class excludes it), so
 * "price-shaped AND fails CODE_RE" is a strictly more correct rejection
 * rule that still preserves Mambo/Siviglia's own real numeric codes.
 * Amante ALSO has its own genuine separate flat_price sub-table (a
 * "plissé" surcharge variant, different codes, one price each) --
 * confirmed real via source, not a duplicate of the tier-ladder rows.
 * flat_price's own label-cleanup was also widened while fixing this to
 * recognize the same "WxH" size pattern (previously left stuck in the
 * label as raw digit noise, e.g. "105 160x200 176/218" instead of a
 * clean size field) -- verified this fix touched only cosmetic label/
 * size text, never price or code, via a full-dataset diff (0 real
 * regressions, 27 label-only corrections).
 *
 * Batch 1c -- 2 small fixes found while spot-checking the rest of the
 * Letti family for completeness:
 *   1. Filo (one of the 9 Letti products) was STILL 0 rows even with
 *      parse_file_pianca_letti_tier built -- its own tier-letter line has
 *      a leading "Piedi" (feet/leg-style) word glued onto the SAME
 *      physical line as the tier letters ("Piedi ... A B C H P Q"),
 *      unlike Beta up (Letti)'s version of the identical convention
 *      where "Piedi" prints on its own separate line just above. Fixed
 *      by checking the TRAILING tokens of the tier-letter line instead
 *      of requiring the whole line to be just the tier letters -- capped
 *      at a floor of 2 tiers (not 1) so a coincidental lone trailing "A"
 *      elsewhere in the lookback window can't false-fire.
 *   2. Woody and Fushimi Lounge -- 2 simple single-named-column tables
 *      (shape_b_named family) needing only a registry entry each
 *      ('Essenza'; 'Cuoio'), no new logic.
 *
 * Batch 2 -- "Composizione <code> (<context>)" bundle family (Spazioteca/
 * Spazio/People/Designbook's own composition-photo pages), 124 products,
 * the single biggest remaining known_gap cluster found during the sweep.
 * Each composition prints a component-by-component breakdown ending in a
 * "totale" row -- THAT row (2 finish-tier prices: "Finitura base" and a
 * per-page catalog finish name, e.g. "Materico"/"Laccato Opaco") is the
 * composition's own sellable price, not the individual component rows.
 * Multiple sibling compositions almost always share one physical page
 * (confirmed byte-identical files for e.g. 9201/9202), so the new
 * parse_file_pianca_composizione_bundle matches the ONE totale row whose
 * own code corresponds to THIS product's own name-derived code, not by
 * page position. Deliberately excludes the already-live "(Designbook
 * 2022)" IOT0xx/TOT0xx family (dims+1 batch) sharing the same "L H P
 * CODICI" header text but with no "Finitura base" column and no "totale"
 * row at all -- confirmed via direct check of all 60 of that family's own
 * text files, not just assumed from the header similarity.
 *
 * A real, confirmed source-PDF inconsistency was found and handled while
 * verifying all 124 individually before building: "Composizione COP061
 * (Designbook)" and "Composizione COP081 (Designbook)" -- the page's own
 * section heading (and this catalog's own product name, extracted from
 * it) reads "...- COP061"/"...- COP081", but that composition's own
 * totale row is printed "COS061"/"COS081" instead (every sibling on the
 * same page, e.g. COS062/COS063, has matching heading/totale codes) --
 * a genuine single-letter P/S typo in Pianca's own real catalog, not an
 * extraction bug. Resolved with a same-page trailing-digit-suffix
 * fallback (requires a UNIQUE match, never guesses) rather than
 * hardcoding either product.
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
  {
    id: 'letti-beta-up-6tier',
    query: 'Beta up Letti price',
    productName: 'Beta up (Letti)',
    code: 'WBAF03N',
    expectedPrice: '1.469',
    expectedSize: '90x190',
    note: 'New capture from parse_file_pianca_letti_tier -- source (beta_up_letti.txt): "90x190 99 207 WBAF03N 1.469 1.528 1.616 1.837 2.116 2.586", tier letters "A B C H P Q" read from the line above CODICI.',
  },
  {
    id: 'letti-bricola-4tier',
    query: 'Bricola Letti price',
    productName: 'Bricola (Letti)',
    code: 'WBCC13N',
    expectedPrice: '2.410',
    expectedSize: '153x190',
    note: 'Confirms the tier COUNT is read per-product, not assumed to always be 6 -- Bricola (Letti) only shows "A B C H" (4 tiers) above its own CODICI line, and its rows only carry 4 trailing prices.',
  },
  {
    id: 'letti-tatami-wildcard-code',
    query: 'Beta up Tatami price',
    productName: 'Beta up (Tatami)',
    code: 'WZ * F03N',
    expectedPrice: '1.037',
    expectedSize: '90x190',
    note: 'Confirms the wildcard-code convention (same as Enea Up in shape_b_named) is preserved literally, not resolved -- source shows the code with a bare "*" placeholder.',
  },
  {
    id: 'amante-corruption-fix-tier-row',
    query: 'Amante price',
    productName: 'Amante',
    code: 'WAAW35S',
    expectedPrice: '2.748',
    expectedSize: '160x200',
    note: 'Regression guard for the Amante data-corruption bug found and fixed while building this batch -- the real 6-tier row (code WAAW35S) must be captured correctly by parse_file_pianca_letti_tier, distinct from the DIFFERENT "plissé" surcharge codes (WABW35S etc) on the same page.',
  },
  {
    id: 'amante-corruption-fix-plisse-row',
    query: 'Amante price',
    productName: 'Amante',
    code: 'WABW35S',
    expectedPrice: '3.441',
    expectedSize: '160x200',
    note: 'The genuine SEPARATE flat_price sub-table on Amante\'s own page (a "plissé" surcharge variant, single price, different codes than the tier-ladder rows) -- confirms flat_price\'s own label-cleanup now correctly extracts "160x200" into size instead of leaving it polluting the label as "105 160x200 176/218".',
  },
  {
    id: 'letti-filo-trailing-tier-match',
    query: 'Filo price',
    productName: 'Filo',
    code: 'WFMH03S',
    expectedPrice: '1.695',
    expectedSize: '90x190',
    note: 'Regression case for the Filo-specific fix: its tier-letter line has a leading "Piedi" word glued onto the SAME physical line as "A B C H P Q" (unlike Beta up (Letti), where "Piedi" sits on its own separate line). _pianca_letti_tier_letters_above now matches the TRAILING tokens of the line instead of requiring whole-line equality, floored at n>1 tiers to avoid a coincidental lone trailing "A" false-firing elsewhere in the lookback window.',
  },
  {
    id: 'dims2-woody-shapeb-named',
    query: 'Woody price',
    productName: 'Woody',
    code: 'T0W00M',
    expectedPrice: '2.779',
    expectedSize: '120×180',
    note: 'New shape_b_named registry entry (\'Essenza\',): [\'Essenza\'] -- simplest possible instance of this shape, a single named finish column after "L min L max CODICI".',
  },
  {
    id: 'dims2-fushimi-lounge-shapeb-named',
    query: 'Fushimi Lounge price',
    productName: 'Fushimi Lounge',
    code: 'D9FL060',
    expectedPrice: '752',
    expectedSize: '60×40×49',
    note: 'New shape_b_named registry entry (\'Cuoio\',): [\'Cuoio\'] -- same single-named-column shape as Woody, different real header word ("L H P CODICI Cuoio").',
  },
  {
    id: 'composizione-spazioteca-9201',
    query: 'Composizione 9201 price',
    productName: 'Composizione 9201 (Spazioteca)',
    code: '9201',
    expectedPrice: '10.998',
    expectedSize: '303×273×37',
    note: 'New capture from parse_file_pianca_composizione_bundle -- the "totale" row on a page shared with sibling Composizione 9202, source: "totale 303 273 37 9201 10.998 15.371".',
  },
  {
    id: 'composizione-spazio-s501',
    query: 'Composizione S501 price',
    productName: 'Composizione S501 (Spazio)',
    code: 'S501',
    expectedPrice: '5.630',
    expectedSize: '540×190×45',
    note: 'Confirms the Spazio family (13 products) resolves the same way as Spazioteca.',
  },
  {
    id: 'composizione-people-p501',
    query: 'Composizione P501 price',
    productName: 'Composizione P501 (People)',
    code: 'P501',
    expectedPrice: '11.422',
    expectedSize: '560×200×45',
    note: 'Confirms the People family (12 products) -- also confirms the tier-2 label is read per-page, not hardcoded: People\'s own catalog finish is "Laccato Opaco", not "Materico" like Spazioteca/Spazio/most of Designbook.',
  },
  {
    id: 'composizione-designbook-hos011',
    query: 'Composizione HOS011 price',
    productName: 'Composizione HOS011 (Designbook)',
    code: 'HOS011',
    expectedPrice: '4.926',
    expectedSize: '300×180×55',
    note: 'Confirms the Designbook family (90 products, the largest sub-family) -- alphanumeric code (not bare digits), same totale-row shape.',
  },
  {
    id: 'composizione-cop-cos-typo-fix-1',
    query: 'Composizione COP061 price',
    productName: 'Composizione COP061 (Designbook)',
    code: 'COS061',
    expectedPrice: '3.178',
    expectedSize: '320×178×45',
    note: 'Regression case for the confirmed COP/COS source-PDF typo: the catalog entry is named "...COP061" (from its own page heading) but the real totale row is printed with code "COS061" -- resolved via the trailing-digit-suffix fallback, not a hardcoded exception. The returned code is the ACTUAL PRINTED code (COS061), matching the "always store what\'s literally printed" convention used elsewhere (e.g. Enea Up\'s wildcard codes).',
  },
  {
    id: 'composizione-cop-cos-typo-fix-2',
    query: 'Composizione COP081 price',
    productName: 'Composizione COP081 (Designbook)',
    code: 'COS081',
    expectedPrice: '3.707',
    expectedSize: '360×142×45',
    note: 'Second confirmed instance of the same COP/COS typo pattern (different sub-family, same fallback mechanism) -- guards against the fallback being coincidentally correct for only one case.',
  },
  {
    id: 'guard-designbook2022-composizione-unaffected',
    query: 'Composizione IOT011 (Designbook 2022) price',
    productName: 'Composizione IOT011 (Designbook 2022)',
    code: 'IOT011',
    expectedPrice: '3.361',
    expectedSize: '123×243×45',
    note: 'Regression guard: the already-live "(Designbook 2022)" family shares the same "L H P CODICI" header text as the new bundle shape but has no "Finitura base" column and no "totale" row -- must keep resolving via flat_price exactly as it did before this batch, not get swallowed by the new composizione_bundle parser. Query deliberately fully-qualified (matching dims1-composizione-iot-new\'s own style) -- the bare-code short form is ambiguous across IOT011/012/013 siblings, a pre-existing matching-layer nuance unrelated to this batch.',
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
