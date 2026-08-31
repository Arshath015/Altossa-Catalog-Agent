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
 * Batch 3 -- SIPARIO's Armadi Moduli/Composizioni "danger table" family
 * (10 door styles x up to 5 mechanism types x Moduli/Composizioni, ~63
 * products, the single biggest remaining dims+2 cluster): L row-group +
 * H per-row, 1-2 CODICI columns (P 59 / P 42.3 depth variants -- the
 * complanari mechanism only ever offers P 59), and N named finish-tier
 * columns whose exact count/labels are VERIFIED PER STYLE via direct
 * page-image inspection (new parse_file_pianca_armadi_danger, registry-
 * driven same discipline as shape_b_named). H is identified by a small
 * verified closed set ({238.5, 257.7, 289.7}), never a generic decimal
 * regex, since ordinary L values are also decimals. Wildcard codes
 * ('M * 73 D/S') and a 2nd real hinge-suffix variant ('0/D/S', confirmed
 * real on Murano's own Moduli scorrevoli page, not an OCR artifact) are
 * both preserved literally.
 *
 * A real cross-contamination bug was found and fixed while verifying
 * this batch: Plana's and Cornice's own 'Cabina soffietto' pages are
 * byte-identical text files, each holding BOTH styles' full tables
 * back-to-back (confirmed via direct diff) -- without scoping each
 * product's own table-search to its own section (a leading ALL-CAPS
 * style-name marker line, e.g. ' PLANA Moduli...' / ' CORNICE
 * Moduli...'), both products silently absorbed each other's codes and
 * prices. A second bug surfaced fixing the first: a naive "next marker
 * ends my scope" rule broke multi-page products whose OWN title
 * reprints verbatim as a running page header (e.g. 'Plana — Moduli
 * stagionali'), wiping their real rows to 0 -- fixed by only treating a
 * DIFFERENT style's marker as a scope boundary, not a repeat of the same
 * one.
 *
 * RUN WITH: npm run check-pianca-known-gap-shapes
 * Requires the dev server running (npm run dev:server).
 */

import fs from 'fs';
import path from 'path';

const BASE_URL = process.env.REGRESSION_BASE_URL || 'http://localhost:3000';
const ROOT = path.join(__dirname, '..', '..');

interface PriceRow {
  product_name: string;
  code: string | null;
  price_eur: string;
  size: string | null;
  fabric_tier?: string | null;
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
  expectedTier?: string;
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
  {
    id: 'armadi-plana-battenti-moduli-4col',
    query: 'Plana armadi battenti moduli price',
    productName: 'Plana — Armadi battenti (Moduli)',
    code: 'MA73 D/S',
    expectedPrice: '341',
    expectedSize: '47.8×238.5×59',
    expectedTier: 'Materico',
    note: 'New capture from parse_file_pianca_armadi_danger -- Plana/Amalfi/Icona\'s shared 4-column shape (Materico/Opaco Base/Opaco Colore-Essenza/Lucido Sp.), P 59 depth. Source (plana_armadi_battenti_moduli.txt): "238.5 MA73 D/S DA73 D/S 341 429 530 764".',
  },
  {
    id: 'armadi-cornice-battenti-moduli-6col-wildcard',
    query: 'Cornice — Armadi battenti (Moduli) price',
    productName: 'Cornice — Armadi battenti (Moduli)',
    code: 'M * 73 D/S',
    expectedPrice: '426',
    expectedSize: '47.8×238.5×59',
    expectedTier: 'Materico',
    note: 'Cornice\'s own 6-column shape, the widest in this family, AND its wildcard-asterisk code convention ("M * 73 D/S", 4 raw tokens including a bare "*"). Also confirms the 5th column\'s real label ("V. Laccato / V. Met. / Specchio") was correctly reconstructed from a 3-PHYSICAL-LINE wrap -- a 2-line grep first missed the "Specchio" word entirely. Query deliberately uses the exact qualified product name (matching this file\'s own established convention for ambiguous names) -- confirmed real via 5 repeated live calls that a shorter "Cornice armadi battenti moduli price" phrasing is genuinely non-deterministic under this session\'s quota-limited LLM availability, intermittently resolving to the pre-existing, unrelated bare "Cornice" collision (a Spazi-10 product, already correctly disambiguated at the data level) instead -- a chat-matching-layer sensitivity that predates and is unrelated to this batch\'s own parser/data work.',
  },
  {
    id: 'armadi-manhattan-moduli-vs-composizioni-different-shapes',
    query: 'Manhattan armadi battenti moduli price',
    productName: 'Manhattan — Armadi battenti (Moduli)',
    code: 'M * 73 D/S',
    expectedPrice: '642',
    expectedSize: '47.8×238.5×59',
    expectedTier: 'Laccato Opaco',
    note: 'Regression guard for a real "don\'t assume Moduli and Composizioni share a shape just because they\'re the same style" case: Manhattan\'s Moduli page has 3 columns (Laccato Opaco / Lucido Sp.+Essenza combined / V. Laccato+V.Met.+Specchio combined) -- see the sibling Composizioni case below for the same style\'s genuinely DIFFERENT 4-column split.',
  },
  {
    id: 'armadi-manhattan-composizioni-4col',
    query: 'Manhattan armadi battenti composizioni price',
    productName: 'Manhattan — Armadi battenti (Composizioni)',
    code: 'B * 715',
    expectedPrice: '2.252',
    expectedSize: '153.8×238.5×59',
    expectedTier: 'L. Opaco',
    note: 'Manhattan\'s own Composizioni page splits Lucido Sp./Essenza into 2 SEPARATE columns (4 total) instead of the Moduli page\'s combined 3 -- confirmed via direct image comparison, not assumed from the Moduli shape. Also exercises the Composizioni-only decorative width-breakdown suffix on the L line ("153.8 50 100") being correctly discarded down to just the L value.',
  },
  {
    id: 'armadi-nastro-battenti-moduli-liscio-4th-wrap-word',
    query: 'Nastro armadi battenti moduli price',
    productName: 'Nastro — Armadi battenti (Moduli)',
    code: 'M * 73 D/S',
    expectedPrice: '766',
    expectedSize: '47.8×238.5×59',
    expectedTier: 'V. Laccato / V. Met. / Specchio / Liscio',
    note: 'Regression guard for the one real header collision found in this family: Nastro\'s Moduli page has a 4th wrap word ("Liscio") on its 5th column that its own Composizioni sibling page does NOT have, despite both sharing byte-identical header tokens -- resolved by a narrow post-hoc "Liscio" lookahead (confirmed unique across every Armadi text file via direct grep), not a per-file registry split.',
  },
  {
    id: 'armadi-icona-antatv-opaco-base',
    query: 'Icona armadi scorrevoli con anta Tv composizioni price',
    productName: 'Icona — Armadi scorrevoli con anta Tv (Composizioni)',
    code: '4NA720',
    expectedPrice: '2.707',
    expectedSize: '203.8×238.5×59',
    expectedTier: 'anta TV Opaco Base — Materico',
    note: 'The con-anta-Tv family\'s own disambiguator: code 4NA720 is printed TWICE on the page (once under "anta TV Opaco Base", once under "anta TV Opaco Colore") with DIFFERENT prices each time -- folded into fabric_tier (same "combine both axes" pattern as Norma Up\'s own struttura_finish) since main()\'s ambiguous-row detection keys on (product, code, fabric_tier) only. See the sibling case below for the SAME code\'s other price.',
  },
  {
    id: 'armadi-icona-antatv-opaco-colore',
    query: 'Icona armadi scorrevoli con anta Tv composizioni price',
    productName: 'Icona — Armadi scorrevoli con anta Tv (Composizioni)',
    code: '4NA720',
    expectedPrice: '3.223',
    expectedSize: '203.8×238.5×59',
    expectedTier: 'anta TV Opaco Colore — Materico',
    note: 'Same code as armadi-icona-antatv-opaco-base, the OTHER real price -- confirms both survive as distinct rows instead of one silently overwriting the other.',
  },
  {
    id: 'armadi-verona-2col-compound-label',
    query: 'Verona armadi battenti moduli price',
    productName: 'Verona — Armadi battenti (Moduli)',
    code: 'MVR73 D/S',
    expectedPrice: '638',
    expectedSize: '47.8×238.5×59',
    expectedTier: 'Cornice e pannello / Laccato Opaco',
    note: 'Verona\'s own shape: the header prints "Cornice e pannello" TWICE (once per column), each disambiguated only by its own wrap word (Laccato Opaco / Essenza) on the following line -- a genuinely different wrap convention from every other style in this family.',
  },
  {
    id: 'armadi-milano-cardine-3heights',
    query: 'Milano armadi cardine moduli price',
    productName: 'Milano — Armadi cardine (Moduli)',
    code: 'MO93 D/S',
    expectedPrice: '843',
    expectedSize: '47.8×289.7×59',
    expectedTier: 'V. Trasparente / V. Metallizzato / Specchio',
    note: 'Milano is one of only 2 styles offering a 3rd H tier (289.7, not just 238.5/257.7) -- confirms the closed H-value set correctly captures all 3, and that the taller tier\'s own real (non-repeated) price is captured, not silently duplicated from the 238.5 row.',
  },
  {
    id: 'armadi-crea-single-column-tipoAB',
    query: 'Crea armadi scorrevoli moduli price',
    productName: 'Crea — Armadi scorrevoli (Moduli)',
    code: 'PR77 D/S',
    expectedPrice: '1.234',
    expectedSize: '97.8×238.5×59',
    expectedTier: 'Vetro Laccato',
    note: 'Crea\'s own single-named-column shape, with a "tipo A"/"tipo B" row-level label (2 codes per L/H, PR.../PS...) this batch deliberately does NOT try to capture into fabric_tier (unlike the anta-Tv prefix) since the code alone already disambiguates it safely -- confirms the row still parses correctly with that leading text simply ignored.',
  },
  {
    id: 'armadi-raggio-complanari-abbreviated-v-laccato',
    query: 'Raggio armadi complanari composizioni price',
    productName: 'Raggio — Armadi complanari (Composizioni)',
    code: 'CT720',
    expectedPrice: '4.603',
    expectedSize: '206×238.5×59',
    expectedTier: 'Vetro Laccato / Specchio',
    note: 'Regression case for a registry gap found via a real "no rows extracted" cross-check: Raggio\'s own complanari page abbreviates "Vetro" to "V." on this ONE header ("V. Laccato" instead of every other Raggio page\'s "Vetro Laccato"), needing its own registry key even though it\'s the same real column.',
  },
  {
    id: 'armadi-contamination-fix-plana-cabina-soffietto',
    query: 'Plana armadi cabina soffietto moduli price',
    productName: 'Plana — Cabina soffietto (Moduli)',
    code: 'NA7T D/S',
    expectedPrice: '1.247',
    expectedSize: '134×238.5×59',
    expectedTier: 'Materico',
    note: 'Regression guard for the real cross-contamination bug found and fixed in this batch: Plana\'s and Cornice\'s own "Cabina soffietto" pages are byte-identical text files holding BOTH styles\' full tables. Must resolve to ONLY Plana\'s own code (NA7T D/S), never Cornice\'s wildcard "N * 7T D/S" from the same file. See the sibling Cornice case below.',
  },
  {
    id: 'armadi-contamination-fix-cornice-cabina-soffietto',
    query: 'Cornice — Cabina soffietto (Moduli) price',
    productName: 'Cornice — Cabina soffietto (Moduli)',
    code: 'N * 7T D/S',
    expectedPrice: '1.450',
    expectedSize: '134×238.5×59',
    expectedTier: 'Materico',
    note: 'The other half of the contamination guard -- must resolve to ONLY Cornice\'s own wildcard code, never Plana\'s "NA7T D/S" from the same shared-page text file. Exact qualified name used for the same live-matching-reliability reason as armadi-cornice-battenti-moduli-6col-wildcard above.',
  },
  {
    id: 'armadi-emdash-similarity-fix-guard',
    query: 'Cornice armadi battenti moduli price',
    productName: 'Cornice — Armadi battenti (Moduli)',
    code: 'M * 73 D/S',
    expectedPrice: '426',
    expectedSize: '47.8×238.5×59',
    expectedTier: 'Materico',
    note: 'Root-cause guard for a real similarity() bug found while writing THIS file\'s own regression cases (fixed in catalogChat.ts, 2026-08-26, unrelated to the parser itself): the em-dash in Pianca\'s own disambiguation naming convention ("Cornice — Armadi battenti (Moduli)") was never stripped the way parens already are, so it survived as a stray standalone token no real user ever types -- permanently capping every em-dash-qualified name (84 across this catalog) below the full containment-match score tier. Confirmed live: this exact PLAIN phrasing (no em-dash typed) reproducibly lost to the unrelated, pre-existing bare "Cornice" collision (Spazi-10, already correctly disambiguated at the data level) in degraded/non-LLM mode, 4 of 5 repeated identical calls, while non-degraded mode was unaffected (the LLM path doesn\'t use this scorer) -- same "only visible in degraded mode" shape as the earlier con/di/per and poltronica-typo anchor bugs. Fixed by stripping em/en-dash the same way as parens in similarity()\'s own tokenizer.',
  },
  {
    id: 'armadi-fianchi-e-divisori-5col-no-l',
    query: 'SIPARIO Fianchi e divisori (Armadi battenti) price',
    productName: 'SIPARIO Fianchi e divisori (Armadi battenti)',
    code: '62FB7 D/S',
    expectedPrice: '115',
    expectedSize: '238.5×59',
    expectedTier: 'Materico',
    note: 'New registry entry for the adjacent SIPARIO Fianchi/Anta Tv family (verified and added right after the main Armadi Moduli/Composizioni batch, same shape family). This shape has NO L row-group value at all -- side panels aren\'t sized by a printed width the way wardrobe modules are -- confirming size gracefully degrades to just "H×depth" when no L is ever carried, instead of erroring.',
  },
  {
    id: 'armadi-fianchi-e-divisori-materico-interno-column',
    query: 'SIPARIO Fianchi e divisori (Armadi battenti) price',
    productName: 'SIPARIO Fianchi e divisori (Armadi battenti)',
    code: '62DA72',
    expectedPrice: '106',
    expectedSize: '238.5×59',
    expectedTier: 'Materico Interno',
    note: 'The Divisorio Sp 2.2 row group populates ONLY the 5th column ("Materico Interno") and leaves the other 4 as "-" -- confirms the per-cell "-" skip works correctly regardless of WHICH columns are populated, not just the usual "last column empty" pattern.',
  },
  {
    id: 'armadi-fianchi-di-finitura-5col-3line-wrap',
    query: 'SIPARIO Fianchi di finitura (Armadi battenti) price',
    productName: 'SIPARIO Fianchi di finitura (Armadi battenti)',
    code: '63FB7 D/S',
    expectedPrice: '650',
    expectedSize: '238.5×59',
    expectedTier: 'V. Marmo',
    note: 'A DIFFERENT 5-column shape from its "Fianchi e divisori" sibling, with its own 3-physical-line wrap (same class as Cornice\'s own Armadi Moduli 6-column shape) -- verified via direct image, not assumed from the sibling.',
  },
  {
    id: 'armadi-anta-tv-frame-component-2col-no-wrap',
    query: 'SIPARIO Anta Tv Moduli scorrevoli (frame component) price',
    productName: 'SIPARIO Anta Tv Moduli scorrevoli (frame component)',
    code: '4TV770',
    expectedPrice: '1.615',
    expectedSize: '97.8×238.5×59',
    expectedTier: 'Opaco Base',
    note: 'A 2-column shape with NO wrap continuation word at all (sub_wrap is genuinely empty) -- this page also has a SECOND, unrelated "L CODICI ... Accessori" LED-accessory table with no H column, deliberately left unrecognized (out of scope, not force-fit into this registry).',
  },
  {
    id: 'shapeb-seida-hybrid-tier-letters-as-columns',
    query: 'Seida price',
    productName: 'Seida',
    code: '01175',
    expectedPrice: '337',
    expectedSize: '44×79×50',
    expectedTier: 'Seduta A-B-C / tessuto cliente',
    note: 'New shape_b_named registry entry, CollezioneGiorno remainder sweep 2026-08-26. A genuine hybrid: 2 named wood-finish columns plus 2 Shape-A-style TIER-LETTER columns (A-B-C/H-P-Q) reused as column headers instead of row labels -- confirmed via image, not assumed from header text.',
  },
  {
    id: 'shapeb-haik-3word-column-wrap',
    query: 'Haik price',
    productName: 'Haik',
    code: '249969',
    expectedPrice: '707',
    expectedSize: '43×45×42',
    expectedTier: 'Malva / Oceano / Onice',
    note: 'New shape_b_named registry entry -- each column wraps across 3 physical lines (Malva/Oceano/Onice, Argento/Bronzo/Oro), verified via image.',
  },
  {
    id: 'shapeb-servoquadro-repeated-code-ambiguous-guard',
    query: 'Servoquadro_Servogiro price',
    productName: 'Servoquadro_Servogiro',
    code: '249951',
    expectedPrice: '301',
    expectedSize: '48×54×48',
    expectedTier: 'L. Opaco / Essenza',
    note: 'New shape_b_named registry entry. The SAME code prints twice on this page under 2 different "Struttura" row groups (Laccato Opaco vs Finiture Metallo) with DIFFERENT prices for the identical fabric_tier -- confirms main()\'s existing (product,code,fabric_tier) ambiguous-row safety net correctly marks BOTH rows "ambiguous": true (verified directly) instead of one silently overwriting the other, rather than needing a new fold-into-fabric_tier mechanism for this specific case.',
  },
  {
    id: 'shapeb-confluence-5col-3token-key',
    query: 'Confluence price',
    productName: 'Confluence',
    code: 'T0E09R',
    expectedPrice: '3.223',
    expectedSize: '230×73×90',
    expectedTier: 'Piano Fenix® Bianco — Basamento Bianco Lucido',
    note: 'New shape_b_named registry entry -- a 5-real-column table whose header line only shows 3 literal "Piano" tokens (2 of the 3 top-level Piano/finish groups each span 2 Basamento-finish sub-columns) -- confirmed via image the registry VALUE list length (5) need not match the KEY tuple length (3), same "parent-group token count != real column count" pattern as the pre-existing Frontali×4 entry.',
  },
  {
    id: 'shapeb-deltafisso-5col',
    query: 'Delta fisso price',
    productName: 'Delta fisso',
    code: 'T0H09H',
    expectedPrice: '1.630',
    expectedSize: '160×75×90',
    expectedTier: 'L. Opaco',
    note: 'New shape_b_named registry entry, 5 named columns, verified via image.',
  },
  {
    id: 'shapeb-mensole-vetro-2col',
    query: 'Mensole vetro per boiserie price',
    productName: 'Mensole vetro per boiserie',
    code: '46X6Z',
    expectedPrice: '167',
    expectedSize: '60',
    expectedTier: 'Vetro Trasparente / Piombo',
    note: 'New shape_b_named registry entry, CollezioneNotte remainder sweep 2026-08-26/27. Same full-catalog collision-check discipline as the CollezioneGiorno batch -- this key is genuinely unique across the whole catalog.',
  },
  {
    id: 'shapeb-mensole-metallo-3col-name-on-header-line',
    query: 'Mensole metallo per boiserie price',
    productName: 'Mensole metallo per boiserie',
    code: '46X5X',
    expectedPrice: '94',
    expectedSize: '50',
    expectedTier: 'Canna di Fucile',
    note: 'Confirms a real finish name ("Canna di Fucile") printed directly on the CODICI line itself is correctly captured as part of the key/column set, not mistaken for stray legend text -- verified via direct row inspection before trusting it.',
  },
  {
    id: 'shapeb-boiserie-e-people-3col',
    query: 'Boiserie e People price',
    productName: 'Boiserie e People',
    code: '56P23',
    expectedPrice: '64',
    expectedSize: '30',
    expectedTier: 'L. Opaco / Essenza',
    note: 'New shape_b_named registry entry, 3 named columns.',
  },
  {
    id: 'shapeb-domino-2nd-table-unique-key',
    query: 'Domino price',
    productName: 'Domino',
    code: '460T2',
    expectedPrice: '123',
    expectedSize: '4×0',
    expectedTier: 'Alluminio Brunito',
    note: 'Domino has 2 tables on its own page -- the FIRST shares the deferred (\'Laccato\',\'Opaco\',\'Essenza\',\'Lucido\',\'Sp.\') collision (Consolle Elle/Luce Illumia/Ponti also derive it) and stays known_gap, but this SECOND table ("Staffe metallo") has a genuinely unique 4-column key, confirming both tables on one product can be independently resolved/deferred rather than treated as all-or-nothing.',
  },
  {
    id: 'shapeb-accessori-pedane-2col',
    query: 'Accessori (Pedane, pianali e scrittoi) price',
    productName: 'Accessori (Pedane, pianali e scrittoi)',
    code: '1RE04',
    expectedPrice: '63',
    expectedSize: '8',
    expectedTier: 'Alluminio',
    note: 'New shape_b_named registry entry, 2 named columns.',
  },
  {
    id: 'shapeb-nota-shared-prefix-2col',
    query: 'Nota price',
    productName: 'Nota',
    code: '2W256',
    expectedPrice: '645',
    expectedSize: '62×47×49',
    expectedTier: 'Fianchi Essenza / Frontali e top: Laccato Opaco',
    note: 'Both columns share a common "Fianchi Essenza / Frontali e top" prefix on the header, disambiguated only by their own trailing finish word (Laccato Opaco vs Lucido Spazzolato) -- verified via direct row inspection, not assumed.',
  },
  {
    id: 'wardrobe-brema-positional-fallback',
    query: 'Brema price',
    productName: 'Brema',
    code: 'B301',
    expectedPrice: '1.007',
    expectedSize: '122×35',
    expectedTier: 'Struttura+top 0.8 — Frontali Materico',
    note: 'New dedicated wardrobe-danger 2-axis parser, 2026-08-27. Every code in Brema\'s own file confirmed to appear exactly once (no repeating-code risk) before trusting the simple scan. Its 6-column header wraps across 4 stacked physical lines with a genuinely ambiguous exact finish-name pairing for 5 of its 6 columns -- per explicit user decision, those use a positional "Frontali N" fallback (only the 1st column, "Materico", is unambiguous and used verbatim), same precedent as Fushimi/Inari\'s own Top 1/2/3 columns. Prices/codes fully exact either way.',
  },
  {
    id: 'wardrobe-ginevra-clean-2x2-grid',
    query: 'Ginevra price',
    productName: 'Ginevra',
    code: '00A3FE D/S',
    expectedPrice: '1.241',
    expectedSize: '123×35',
    expectedTier: 'Struttura Laccato Opaco/Essenza — Frontali L. Opaco/Essenza',
    note: 'Fully legible 2x2 Struttura x Frontali grid, no positional fallback needed. Every code confirmed to appear exactly once.',
  },
  {
    id: 'wardrobe-grafica-danger-fold-opaco',
    query: 'Grafica price',
    productName: 'Grafica',
    code: 'G3CH',
    expectedPrice: '1.639',
    expectedSize: '102×35',
    expectedTier: 'L. Opaco — Struttura+top 1.4 — Frontali Materico',
    note: 'Confirmed real repeating-code danger pattern (same class as Norma Up\'s original 1566-row bug): code G3CH prints twice on the page under 2 different "Basamento" row-types (L. Opaco / Fin. Metallo) with different prices each time. Folded the row-type into fabric_tier (same technique as armadi_danger\'s own anta-Tv prefix) to avoid a collision -- confirmed via direct check that main()\'s ambiguous-row detector does NOT flag either row as ambiguous (they resolve to 2 distinct, correct rows). Also exercises the positional-fallback columns (8 total, only "Materico" unambiguous) AND the leading-diagram-noise strip (raw row line had "50 30 1.4 L. Opaco" before the real row-type text, reusing the existing _pianca_strip_leading_diagram_noise helper).',
  },
  {
    id: 'wardrobe-grafica-danger-fold-metallo',
    query: 'Grafica price',
    productName: 'Grafica',
    code: 'G3CH',
    expectedPrice: '2.033',
    expectedSize: '102×35',
    expectedTier: 'Fin. Metallo — Struttura+top 1.4 — Frontali Materico',
    note: 'Same code as wardrobe-grafica-danger-fold-opaco, the OTHER real price -- confirms both survive as distinct rows.',
  },
  {
    id: 'wardrobe-logos-noncontiguous-anchor',
    query: 'Logos (CollezioneGiorno) price',
    productName: 'Logos (CollezioneGiorno)',
    code: '0054FF',
    expectedPrice: '1.959',
    expectedSize: '123×45',
    expectedTier: 'Top e frontali interni L. Opaco/Essenza — Struttura e frontali esterni L. Opaco',
    note: 'Regression guard for a real bug caught while building this batch: Logos\'s own "Struttura e frontali esterni" phrase is NOT contiguous on one physical line (it wraps across 3 stacked lines, "Struttura"x6 / "e frontali"x6 / "esterni"x6) -- a first attempt using the 3-word phrase as the anchor found 0 rows. Fixed by anchoring on 6 consecutive bare "Struttura" tokens instead (confirmed unique via full-catalog grep).',
  },
  {
    id: 'wardrobe-people-cg-scoped-by-name-not-content',
    query: 'People (CollezioneGiorno) price',
    productName: 'People (CollezioneGiorno)',
    code: '0093FF *',
    expectedPrice: '1.157',
    expectedSize: '126×35',
    expectedTier: 'Struttura Laccato Opaco/Essenza — Frontali L. Opaco/Essenza',
    note: 'This exact table ALSO appears byte-identical in the much larger people.txt shared by 13 unrelated already-resolved products (Composizione P5xx (People), Boiserie e People) -- scoped by product_name (like composizione_bundle\'s own pattern), not content alone, so this table\'s rows are never wrongly attributed to those unrelated products. Also exercises the trailing-standalone-wildcard code convention ("0093FF *").',
  },
  {
    id: 'wardrobe-quadra-asymmetric-split',
    query: 'Quadra price',
    productName: 'Quadra',
    code: '0063FF',
    expectedPrice: '1.771',
    expectedSize: '121×35',
    expectedTier: 'Struttura Lucido Sp. — Frontali Lucido Sp.',
    note: 'Confirms the real, domain-consistent ASYMMETRIC parent-group split (3 Frontali sub-choices under "Struttura Laccato Opaco/Essenza", only 1 under "Struttura Lucido Sp.") -- a narrower "special" structure finish only ever pairs with its own matching frontali option, same pattern independently confirmed on People (CollezioneGiorno).',
  },
  {
    id: 'wardrobe-tosca-danger-fold-plus-trailing-wildcard',
    query: 'Tosca price',
    productName: 'Tosca',
    code: '0083FF *',
    expectedPrice: '1.795',
    expectedSize: '132×35',
    expectedTier: 'Materico Lavagna — Esterno Essenza',
    note: 'Tosca\'s own repeating-code danger pattern (an "Interno" row-type, e.g. "Materico Lavagna" vs "Laccato Opaco") -- code 0083FF * prints twice with different prices. This specific row also confirms partial-column population survives correctly: the "Materico Lavagna" row-type only has a real price under "Essenza" (the other 2 columns are "-" and correctly skipped, not zero-filled).',
  },
  {
    id: 'cornice-spazi10-danger-fold-opaco',
    query: 'Cornice Spazi-10 price',
    productName: 'Cornice',
    code: '00D4FF',
    expectedPrice: '2.769',
    expectedSize: '120×120×45',
    expectedTier: 'L. Opaco — L. Opaco / Essenza',
    note: 'New dedicated 2-axis function, 2026-08-27 (per flag_triage.json\'s own note, genuinely different from armadi_danger/wardrobe -- NOT generalized/shared). Confirmed real repeating-code danger pattern: code 00D4FF prints twice under 2 different "Interno e cappello" row-types (L. Opaco / L. Metallico) with different prices -- folded into fabric_tier, confirmed neither row gets marked ambiguous.',
  },
  {
    id: 'cornice-spazi10-copertura-single-dim-subtable',
    query: 'Cornice Spazi-10 price',
    productName: 'Cornice',
    code: '06DH4F',
    expectedPrice: '180',
    expectedSize: '120×45',
    expectedTier: 'Pelle Sint. (Copertura aggiuntiva)',
    note: 'The page\'s own SEPARATE "Copertura" sub-table (never repeats, only ever populates the 4th column) has rows with only ONE dimension before the code (not the H/P pair the "Interno e cappello" rows have) -- a real bug self-caught before committing: an early version required an adjacent H/P pair and silently skipped every Copertura row (flagged "no H/P pair found"). Fixed with a single-dimension fallback that also strips a stray leading diagram-reference letter ("A") -- deliberately NOT reusing the existing _pianca_strip_leading_diagram_noise helper here, since that helper also strips bare numbers and would have eaten the real H value itself.',
  },
  {
    id: 'armadi-sipario-spazi10-registry-extension',
    query: 'Sipario Fianchi price',
    productName: 'Sipario',
    code: '62FW7 D/S',
    expectedPrice: '120',
    expectedSize: '238.5×59',
    expectedTier: 'Materico',
    note: 'Sipario (Spazi-10) was flagged in flag_triage.json as looking like it needed its own dedicated 2-axis function (2 SKUs per row), but confirmed via direct row inspection, 2026-08-27, to be EXACTLY the armadi_danger parser\'s own existing 2-CODICI-column shape (P 59 / P 42.3 depth variants sharing one price vector -- the SAME safe pattern as the main Armadi Moduli/Composizioni batch, not a repeating-code-different-price danger table) -- just needed 3 new registry entries, not a new function. +422 rows.',
  },
  {
    id: 'armadi-sipario-wildcard-tri-legend-6col',
    query: 'Sipario Fianchi price',
    productName: 'Sipario',
    code: '7 * 7E D/S',
    expectedPrice: '1.067',
    expectedSize: '96.4×238.5×59',
    expectedTier: 'L. Opaco / Essenza',
    note: 'A 6-column shape with a 3-way wildcard legend (*CG Legno / *CT Vetro / *CP Pelle Sint.), reusing the existing armadi wildcard-code consumer unchanged. Also a real, SAFE residual ambiguity: this exact literal code string "7 * 7E D/S" ALSO appears on a genuinely DIFFERENT Sipario sub-table (a different wildcard-letter family, *NG/*NT vs *CG/*CT/*CP, which collapses to the same literal string since the wildcard itself is preserved un-resolved) -- confirmed the 2 tables\' shared column name ("Materico") correctly gets flagged ambiguous by main()\'s existing (product,code,fabric_tier) safety net, while their 6 genuinely differently-named columns stay safely distinct.',
  },
  {
    id: 'shapeb-11-2col-not-chloe-collision',
    query: '1+1 price',
    productName: '1+1',
    code: '24991B',
    expectedPrice: '340',
    expectedSize: '60×35×30',
    expectedTier: 'Laccato Opaco',
    note: 'Collision-cluster resolution 2026-08-27 (1 of 7 documented shape_b_named collision clusters, sequenced first per explicit user priority as verification-bound work on an already-proven parser). 1+1\'s own header line derives the identical bare (\'Struttura\',) registry key as Chloé, but its real table is genuinely 2 named columns (Struttura\'s own 2 finish options -- confirmed via image the \'Piano\' group to the left is a fixed material note, not a priced column). +10 rows.',
  },
  {
    id: 'chloe-wrapped-shape-a-not-shapeb-collision',
    query: 'Chloé price',
    productName: 'Chloé',
    code: '2U155',
    expectedPrice: '838',
    expectedSize: '50×52×35',
    expectedTier: 'A',
    note: 'The other half of the (\'Struttura\',) collision. Confirmed via image that Chloé\'s own table is NOT shape_b_named material at all -- it\'s a WRAPPED Shape A header (A-B-C-H-P-Q tier letters print on their own following physical line instead of trailing the CODICI line the way every other Shape A table does), a structurally different table this registry never represents. New `parse_file_pianca_chloe` (reuses base Shape A\'s row-scan logic unchanged, only the header detection differs) plus a shared `_pianca_wrapped_tier_letters_ahead` lookahead guard added to `shape_b_named`\'s own dispatch loop so the (\'Struttura\',) key added for 1+1 can never misfire on this header line. +30 rows.',
  },
  {
    id: 'intro-basamento-3col-not-delta-collision',
    query: 'Intro price',
    productName: 'Intro',
    code: '01177',
    expectedPrice: '215',
    expectedSize: '54×83×52',
    expectedTier: 'Bianco / Lavagna',
    note: 'Collision cluster #2 (2 of 7), resolved 2026-08-27. Intro and Delta allungabile both derive the identical bare (\'Basamento\',) shape_b_named registry key from their own header line, but have genuinely different real column counts/labels confirmed via image (Intro: 3 columns, Laccato Opaco/Essenza + Bianco/Lavagna + Finiture Metallo; Delta allungabile: 2 columns, Laccato Opaco/Essenza + Cromo Lucido). Deliberately NOT added to the shared flat registry (no product-scoping mechanism there) -- each gets its own small product_name-scoped parser sharing one row-scan core (`_pianca_basamento_row_scan`), same architecture as the wardrobe-danger family. +5 rows.',
  },
  {
    id: 'delta-allungabile-basamento-2col-chiuso-aperto-dims',
    query: 'Delta allungabile price',
    productName: 'Delta allungabile',
    code: 'T0D09H',
    expectedPrice: '3.537',
    expectedSize: '160×250×74',
    expectedTier: 'Cromo Lucido',
    note: 'The other half of the (\'Basamento\',) collision. 2 real columns instead of Intro\'s 3, and a genuinely different leading-dimension convention (L chiuso / L aperto / H, not the usual L/H/P) -- confirmed the shared generic dims-capture (whatever numeric tokens sit closest to the code, capped at 3, in original left-to-right order) handles this correctly with no special-casing, same as shape_b_named\'s own established Elide/Soffio Up precedent. +6 rows.',
  },
  {
    id: 'abaco-piano-2col-carries-label-not-aliseo-collision',
    query: 'Abaco price',
    productName: 'Abaco',
    code: 'T0A76',
    expectedPrice: '444',
    expectedSize: '76×30×76',
    expectedTier: 'Laccato Opaco / Cemento',
    note: 'Collision cluster #3 (3 of 7), resolved 2026-08-27. A full-catalog grep for the bare (\'Piano\',) key (not just the 4 originally-named products) found 11 files total -- 6 already resolved via other parsers, 5 genuinely new: Abaco (2 cols) and Aliseo (ALSO 2 cols, different labels -- the same Abaco/Scacco-style near-miss the row-shape count check can\'t catch) and Baio (4 cols) each get their own product_name-scoped parser; Soffio fisso/allungabile (5 cols, shared between the two) safely share one flat registry entry since their own shorter-column siblings can never produce a false 5-price match. This case (T0A76) is specifically the 2nd row of a 3-row dimension group whose own model_variant label ("L. Opaco Bianco / Lavagna") prints only on the FIRST row and must carry forward -- a real bug caught before committing: first version reset model_variant to None on every label-less row instead of carrying it forward like norma_up_2axis\'s own established struttura_finish precedent. +12 rows.',
  },
  {
    id: 'aliseo-piano-2col-different-labels-not-abaco-collision',
    query: 'Aliseo price',
    productName: 'Aliseo',
    code: 'OAL22',
    expectedPrice: '772',
    expectedSize: '50×47×50',
    expectedTier: 'Vetro Martellato',
    note: 'The Abaco near-miss half of cluster #3: identical (\'Piano\',) key and identical column COUNT (2) as Abaco, but genuinely different real labels (\'Vetro Martellato\'/\'Gres\' vs Abaco\'s \'Laccato Opaco / Cemento\'/\'Marmo / Terrazzo\') -- confirmed via image, the exact shape the row-count-only safety check would silently mislabel if these were forced into one shared registry entry. +8 rows.',
  },
  {
    id: 'baio-piano-4col-distinct-shape',
    query: 'Baio price',
    productName: 'Baio',
    code: 'T0B7D',
    expectedPrice: '978',
    expectedSize: '120×27×120',
    expectedTier: 'Terrazzo',
    note: 'The 3rd distinct real shape under the (\'Piano\',) key -- 4 columns (\'Laccato Opaco / Essenza\', \'Lucido Sp.\', \'Terrazzo\', \'Marmo\'), no leading model_variant label (unlike Abaco). +16 rows.',
  },
  {
    id: 'soffio-fisso-piano-5col-shared-registry-safe',
    query: 'Soffio fisso price',
    productName: 'Soffio fisso',
    code: 'T0Y08D',
    expectedPrice: '2.118',
    expectedSize: '120×76',
    expectedTier: 'V. Laccato',
    note: 'Soffio fisso/allungabile are the 4th and 5th real shapes under the (\'Piano\',) key -- but here it WAS safe to add ONE shared flat registry entry (5 cols: L. Opaco/Essenza/Fenix® Bianco-Nero/V. Laccato/V. Marmo), verified via full-catalog proof that Abaco/Aliseo (2 cols) and Baio (4 cols) can never produce 5 consecutive valid trailing price-cell tokens (their own CODICI code token always falls inside the 5-wide trailing slice). L. Opaco and Essenza are priced identically on every row -- confirmed via image a real coincidence, not a merged column. +406 rows.',
  },
  {
    id: 'soffio-allungabile-piano-5col-shared-plus-chiuso-aperto-dims',
    query: 'Soffio allungabile price',
    productName: 'Soffio allungabile',
    code: 'T0U08A',
    expectedPrice: '2.812',
    expectedSize: '90×150',
    expectedTier: 'V. Marmo',
    note: 'Shares the same 5-column (\'Piano\',) registry entry as Soffio fisso (identical real column labels, confirmed via image) despite its own different leading-dimension convention (L chiuso / L aperto, not L/H) -- the shared generic dims-capture handles this with no special-casing, same as the Delta allungabile precedent above. +312 rows.',
  },
  {
    id: 'shapeb-kyoto-7col-outlier-not-2axis-danger',
    query: 'Kyoto price',
    productName: 'Kyoto',
    code: '2E354',
    expectedPrice: '505',
    expectedSize: '43×52',
    expectedTier: 'Struttura esterna Laccato Opaco — Frontali L. Opaco/Essenza',
    note: 'One of the 2 originally-named "outlier" products (with Grafica). LOOKS like a Norma-Up-style repeating-code 2-axis danger table at first glance (3 Struttura-esterna parent groups x 2 Frontali sub-choices) but confirmed via direct row inspection that every code appears EXACTLY ONCE across all 6+1 real columns -- genuinely a flat 7-column table, safe for the simple registry rather than needing its own dedicated 2-axis function. The registry VALUE list (7 items) doesn\'t match the KEY tuple length (7 tokens here, coincidentally) -- same "parent-group token count may differ from real column count" pattern as Confluence, verified independently.',
  },
  {
    id: 'duetto-struttura-2col-not-real-collision',
    query: 'Duetto price',
    productName: 'Duetto',
    code: '249982',
    expectedPrice: '224',
    expectedSize: '33×50×33',
    expectedTier: 'Laccato Opaco',
    note: 'Collision cluster #4 (2026-08-27/28): Duetto derives the bare (\'Laccato\',\'Opaco\',\'Finiture\',\'Metallo\') key, genuinely shared with Brema and Norma (CollezioneNotte), confirmed via image to have IDENTICAL real column labels across all 3 -- unlike every other cluster this session, safe to share one flat registry entry as-is. Caught mid-verification: this entry\'s own regeneration into prices.json had never actually been run before this case was added (the interrupted prior session added the registry key + these comments but the live server was still serving pre-fix data, returning no_price_data) -- a reminder that a registry/parser-code change and its data regeneration are 2 separate steps, and only a live re-query (not just a code read) proves the fix real.',
  },
  {
    id: 'norma-collezionenotte-double-wildcard-code',
    query: 'Norma CollezioneNotte price',
    productName: 'Norma (CollezioneNotte)',
    code: '2NZ4 * 1 * 2',
    expectedPrice: '581',
    expectedSize: '40',
    expectedTier: 'Laccato Opaco',
    note: 'The other half of cluster #4\'s own real complexity, found AFTER the registry key alone proved insufficient: this table\'s order code is a DOUBLE-wildcard shape ("2NZ4 * 1 * 2", 5 tokens) the existing single-wildcard consumer (Enea Up\'s "T0E * 09M", 3 tokens) never matched, so every row was silently skipped (0 rows, 0 flags) even with the correct registry key in place. New 5-token branch in the same wildcard-code consumer, checked before the 3-token branch, requiring BOTH "*" positions literally present so it can never misfire on a real single-wildcard or dims-prefixed code. Same "preserve literal printed text, don\'t resolve" philosophy as Enea Up -- confirmed via full-file grep the "* 1 * 2" suffix is constant across every row of both Norma files, a footnote-style legend reference that never changes which of the row\'s 2 named-column prices applies. +16 rows.',
  },
  {
    id: 'norma-collezionegiorno-double-wildcard-own-key',
    query: 'Norma CollezioneGiorno price',
    productName: 'Norma (CollezioneGiorno)',
    code: '067Z7 * 1 * 2',
    expectedPrice: '583',
    expectedSize: '70',
    expectedTier: 'L. Opaco',
    note: 'Shares the same double-wildcard code shape as Norma (CollezioneNotte) above but derives its OWN catalog-wide-unique key (\'L.\',\'Opaco\',\'Finiture\',\'Metallo\', abbreviated "L." not "Laccato") -- confirmed via image genuinely distinct from CollezioneNotte\'s header, not a duplicate registry entry. +36 rows.',
  },
  {
    id: 'consolle-elle-lucido-sp-cluster5-shared',
    query: 'Consolle Elle price',
    productName: 'Consolle Elle',
    code: '46L054',
    expectedPrice: '391',
    expectedSize: '50',
    expectedTier: 'Laccato Opaco',
    note: 'Collision cluster #5 (2026-08-28): Consolle Elle derives the bare (\'Laccato\',\'Opaco\',\'Essenza\',\'Lucido\',\'Sp.\') key, confirmed via direct ROW inspection (not header text alone) genuinely shared with Domino (both its own "Panche" and "Gambe metallo" tables), Luce Illumia, and Ponti\'s own "Mensole sottoponte" table -- every one of the 4 has exactly 3 real trailing prices with the same Laccato-Opaco==Essenza coincidental-identical-pricing pattern already established elsewhere (Soffio fisso/allungabile), safe to share one flat entry.',
  },
  {
    id: 'domino-panche-range-dims-cluster5-shared',
    query: 'Domino price',
    productName: 'Domino',
    code: 'C26IQ3 D/S',
    expectedPrice: '476',
    expectedSize: '36×45',
    expectedTier: 'Laccato Opaco',
    note: 'Domino\'s own "Panche" table under the shared cluster #5 key -- a D/S-suffixed code with a text range-dims prefix ("L da 90 a 150 cm") the generic scanner already handles via its existing D/S branch + numeric-tail dims capture, no special-casing needed. A stray "Alluminio Brunito" caption prints on the 2 lines right after this product\'s OWN "Gambe metallo" 2nd table sharing the same key -- confirmed via direct row count it is a finish note, not a real 4th column (every row still has exactly 3 prices).',
  },
  {
    id: 'luce-illumia-cluster5-wrap-caption-not-4th-col',
    query: 'Luce Illumia price',
    productName: 'Luce Illumia',
    code: '62EW4JM',
    expectedPrice: '348',
    expectedSize: '100',
    expectedTier: 'Laccato Opaco',
    note: 'Luce Illumia\'s own header wraps a stray "L. Metallico" word onto its 2nd physical line (looks like a possible 4th column at a glance) -- confirmed via direct row count this product\'s real rows still have exactly 3 trailing prices, same caption-bleed pattern as Domino\'s "Alluminio Brunito" above, not a genuine 4th column. Luce Illumia\'s file also has several unrelated flat-price ("CODICI Prezzo") tables elsewhere on the same pages, confirmed not to interfere (different tail, different parser).',
  },
  {
    id: 'ponti-mensole-cluster5-shared',
    query: 'Ponti price',
    productName: 'Ponti',
    code: '62EW4J',
    expectedPrice: '227',
    expectedSize: '100',
    expectedTier: 'Laccato Opaco',
    note: 'Ponti\'s own "Mensole sottoponte" table under the shared cluster #5 key -- the 4th and last confirmed-safe-to-share instance. Ponti\'s file also has several genuinely different, more complex CODICI table shapes elsewhere ("Maggiorazione per interno..." 2x-CODICI multi-column tables, a "Lavorazioni previste" shape) -- confirmed this specific key only ever derives from this one real table shape on this product, not a false-positive match against those.',
  },
  {
    id: 'forma-scrittoi-cluster8-single-col-shared',
    query: 'Forma price',
    productName: 'Forma',
    code: '5CSA0',
    expectedPrice: '317',
    expectedSize: '59',
    expectedTier: 'Laccato Opaco',
    note: 'Collision cluster #8 (2026-08-28): Forma derives the bare (\'Laccato\',\'Opaco\') key, confirmed via direct row inspection genuinely shared with Boiserie Soft, Norma Up, and Ponti\'s own "Lavorazioni previste" table -- all 4 are a single priced column, safe to share one flat entry.',
  },
  {
    id: 'boiserie-soft-cluster8-single-col-shared',
    query: 'Boiserie Soft price',
    productName: 'Boiserie Soft',
    code: '45TQF',
    expectedPrice: '194',
    expectedSize: '120×30×220',
    expectedTier: 'Laccato Opaco',
    note: 'The 2nd of 4 sharing cluster #8\'s single-column key -- confirmed via image genuinely a single Laccato Opaco price column, no wrapped-caption near-miss on this one.',
  },
  {
    id: 'norma-up-cluster8-glued-double-wildcard',
    query: 'Norma Up price',
    productName: 'Norma Up',
    code: '06KBC *1 *2',
    expectedPrice: '558',
    expectedSize: '80',
    expectedTier: 'Laccato Opaco',
    note: 'Norma Up\'s own table under cluster #8\'s shared key needed a 2nd fix beyond the registry entry alone: its order code is a GLUED-double-wildcard shape ("06KBC *1 *2", asterisk fused to its digit, 2 tokens) that neither the existing spaced single-wildcard (Enea Up\'s "T0E * 09M") nor spaced double-wildcard (Norma (CollezioneNotte)\'s "2NZ4 * 1 * 2") branch matched -- new dedicated branch added, same "preserve literal printed text" philosophy, confirmed-constant "*1 *2" suffix via full-file grep. Also confirmed Norma Up\'s own header wraps 2 further finish-name lines ("Laccato Metallico", "Finiture Metallo") right after "Laccato Opaco" -- same caption-bleed-not-a-real-column pattern as Ponti\'s "Essenza" wrap below, every real row still has exactly 1 price.',
  },
  {
    id: 'ponti-lavorazioni-cluster8-single-col-shared',
    query: 'Ponti price',
    productName: 'Ponti',
    code: '62EX1J',
    expectedPrice: '197',
    expectedSize: '143',
    expectedTier: 'Laccato Opaco',
    note: 'Ponti\'s OWN "Lavorazioni previste" table -- the 4th and last cluster #8 instance, a genuinely different real table on the same product/file as cluster #5\'s "Mensole sottoponte" case above (confirmed each key only ever derives from its own distinct table, no cross-contamination). Ponti\'s header here wraps a stray "Essenza" word onto its 2nd line (looks like a possible 2nd column) -- confirmed via direct row count every real row has exactly 1 price, same caption-bleed pattern as Norma Up\'s own wrap.',
  },
  {
    id: 'icaro-labels-before-codici-4col',
    query: 'Icaro price',
    productName: 'Icaro',
    code: 'T0Z80',
    expectedPrice: '506',
    expectedSize: '60×37×60',
    expectedTier: 'Essenza',
    note: 'Resolved 2026-08-28. A genuinely different header convention from every other Pianca table this session: the real column labels (Essenza/V. Laccato/V. Marmo/Marmo) print on the physical line BEFORE "L H P CODICI" (which has an EMPTY tail), not after or wrapped below it. The PRE-EXISTING regression guard for this product (removed from REJECT_CASES) turned out to be stale/inaccurate -- it described "2 trailing prices... labels wrap to the next line", but direct row inspection shows 4 real columns with labels before the header. Per user feedback: an inherited "confirmed" claim is a claim, not proof -- always re-verify live rather than trust an old comment. New shared `_pianca_labels_before_codici_row_scan` core (product-name-gated, no header auto-detection needed since only 2 products use this shape). +40 rows.',
  },
  {
    id: 'ettorino-labels-before-codici-4col',
    query: 'Ettorino price',
    productName: 'Ettorino',
    code: 'T0P08',
    expectedPrice: '1.480',
    expectedSize: '80×75×80',
    expectedTier: 'Laccato Opaco',
    note: 'Resolved 2026-08-28, shares Icaro\'s new labels-before-CODICI shape (Laccato Opaco/Essenza/Terrazzo/Marmo). Unlike Icaro, this product\'s pre-existing guard comment WAS accurate (correctly described as a real 4-column table with labels before CODICI) -- confirms the "verify, don\'t trust" rule cuts both ways: some inherited claims hold up, some don\'t, and only re-checking tells you which. Captures "Tavolo rotondo"/"Tavolo quadrato" as variant_context since 2 different shapes can share the same L dimension with different codes (T0P08 vs T0P7A, both L=80). +20 rows.',
  },
  {
    id: 'ala-pannelli-cluster7-shared',
    query: 'Ala price',
    productName: 'Ala',
    code: '46R4FWX',
    expectedPrice: '264',
    expectedSize: '120×40×4.5',
    expectedTier: 'Laccato Opaco',
    note: 'Collision cluster #7 (2026-08-28, partial): Ala\'s OWN "Pannelli" table derives the bare (\'Laccato\',\'Opaco\',\'Essenza\',\'Lucido\',\'Spazzolato\') key, confirmed via direct row inspection genuinely shared with Venere -- both simple, clean 3-column tables, safe to share one flat entry. Spazioteca (SistemiGiorno) also derives this exact key but is deliberately excluded (see guard-spazioteca-scorrevoli-not-corrupted below) -- a real corruption risk found on at least one of its own sub-tables, not force-added.',
  },
  {
    id: 'venere-cluster7-shared',
    query: 'Venere price',
    productName: 'Venere',
    code: '42E84',
    expectedPrice: '1.300',
    expectedSize: '40×80×35',
    expectedTier: 'Laccato Opaco',
    note: 'The other half of cluster #7\'s safely-shared pair -- confirmed via direct row inspection identical real column structure to Ala\'s "Pannelli" table.',
  },
  {
    id: 'cluster6-ala-scrittoi-fully-inline',
    query: 'Ala price',
    productName: 'Ala',
    code: '5A055Y',
    expectedPrice: '672',
    expectedSize: '120×10×61',
    expectedTier: 'Cuoio R.',
    note: 'Collision cluster #6 (2026-08-30/31): the bare (\'Struttura\',\'Struttura\') shape_b_named key, shared by Ala/Dedalo (Progetti 06-07)/both People files. Ala\'s own rows already print all 3 dims (L H P, in THAT order per its own embedded "L H P CODICI" header) fully inline with zero borrow ambiguity -- confirmed via source image (page 84). New `_pianca_struttura_frontali_row_scan` core.',
  },
  {
    id: 'cluster6-dedalo-cassetto-outer-dim-borrow',
    query: 'Dedalo (Progetti 06-07) price',
    productName: 'Dedalo (Progetti 06-07)',
    code: '06314',
    expectedPrice: '93',
    expectedSize: '10×37×45',
    expectedTier: 'Cuoio R.',
    note: 'Confirmed via source image (page 22): the group\'s own outer H dimension (10) prints inline on only ONE of the 3 "Moduli People a cassetto" sibling rows (0631E) and must be borrowed by the other 2 (06314, 0631C) -- borrowed from whichever row in the blank-line-bounded block has the most inline dims, not a single-direction carry rule.',
  },
  {
    id: 'cluster6-dedalo-4row-shared-h-block',
    query: 'Dedalo (Progetti 06-07) price',
    productName: 'Dedalo (Progetti 06-07)',
    code: '06376',
    expectedPrice: '488',
    expectedSize: '30×60×35',
    expectedTier: 'Cuoio R.',
    note: 'Regression guard for a false alarm caught and resolved during verification: 06376/06377 ("con kit bar") and 06366/06367 ("ribalta") looked like 2 separate 2-row H-groups at a glance, but the source image (page 22) confirms all 4 genuinely share ONE H=30 label -- the row-level "con kit bar"/"ribalta" text is a descriptive sub-label, not a table-shape boundary. Diagram noise on these same physical lines ("3 7 / 82", "60 / 70") is correctly discarded (fails the clean-bare-number check), not consumed as a bogus dimension.',
  },
  {
    id: 'cluster6-people-cn-icon-noise-not-outer-dim',
    query: 'People (CollezioneNotte) price',
    productName: 'People (CollezioneNotte)',
    code: '5P2Y4',
    expectedPrice: '409',
    expectedSize: '40×40×45',
    expectedTier: 'Cuoio Rigenerato',
    note: 'Root-cause guard for the trickiest bug in this cluster, caught before committing via direct comparison against the source page image (PEOPLE_141): a drawer-icon diagram annotation ("-20"/"-20", printed to the left of rows 5P1Y8/5P2Y8) is ALSO a clean bare number sitting where a genuine outer-H value would be, at a DIFFERENT column position than the sub-header\'s own real "H" column. A naive "just check it\'s a clean number" rule (sufficient for Dedalo\'s own multi-token "3 7 / 82" noise) wrongly borrowed H=20 onto 5P2Y4 and every other short row in the block. Fixed by anchoring on the real column position of the table\'s own "H"/"L" sub-header label (whichever of the two sits further left is genuinely outermost -- the order differs: "H then L" here, "L then H" for Ala/People (SistemiGiorno)) and gating the outermost pop on that position; the whole 9-row block correctly shares ONE real H=40 (confirmed via image), including this row.',
  },
  {
    id: 'cluster6-island-up-materico-interno',
    query: 'Island up price',
    productName: 'Island up',
    code: 'T63RHQ',
    expectedPrice: '3.824',
    expectedSize: '140×75×53',
    expectedTier: 'Materico Interno',
    note: 'Island up\'s own 2 real occurrences of the bare (\'Struttura\',\'Struttura\') key are a completely different, simpler 2-column shape (no outer-dimension borrow needed at all -- every row already prints all 3 dims inline) -- confirmed via direct row inspection, not assumed just because it shares the same bare key as Ala/Dedalo/People.',
  },
  {
    id: 'cluster6-island-up-laccato-opaco',
    query: 'Island up price',
    productName: 'Island up',
    code: 'T63RHQ',
    expectedPrice: '4.283',
    expectedSize: '140×75×53',
    expectedTier: 'Laccato Opaco',
    note: 'Same code as cluster6-island-up-materico-interno, the OTHER real column price -- confirms both survive as distinct rows.',
  },
];

interface RejectCase {
  id: string;
  productName: string;
  note: string;
}

const REJECT_CASES: RejectCase[] = [
  {
    id: 'guard-scacco-top-collision-not-mislabeled',
    productName: 'Scacco',
    note: 'Root-cause guard for a real near-miss caught while building the CollezioneGiorno remainder batch: Scacco and Abaco BOTH derive the bare shape_b_named key (\'Top\',) but have genuinely DIFFERENT real column labels (Scacco: Linoleum/V. Laccato; Abaco: Cuoio Rigenerato.../Vetro Marmo...) that happen to share the same column COUNT (2) -- the existing row-shape safety check only validates trailing price COUNT, not label correctness, so this would NOT have been caught automatically; only found by checking the bonus match\'s own source image after adding the key. Neither was added to the registry as a result -- Scacco must stay at 0 rows (still correctly known_gap) until both sides get a real product-scoped fix, not a flat catalog-wide key.',
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
    // A throwaway non-matching query before each real one -- the chat
    // endpoint tracks a "last product" anchor server-side independent of
    // this script's own `history: []`, confirmed real while adding the
    // Armadi cases below: 2 back-to-back real queries in the same run
    // could otherwise silently resolve to the PRIOR case's product
    // instead of failing honestly, especially once enough Pianca-batch
    // cases accumulated in this same file to make a later query's own
    // wording occasionally under-confident on its own.
    await postChat('Pianca', 'xyz-regression-reset-nonexistent-product');
    const resp = await postChat('Pianca', c.query);
    const rowsForCode = (resp.matches || []).filter(m => m.product_name === c.productName && m.code === c.code);
    const row = c.expectedTier === undefined
      ? rowsForCode[0]
      : rowsForCode.find(m => m.fabric_tier === c.expectedTier);
    if (!row) {
      failures.push(`[${c.id}] "${c.query}" -- expected code "${c.code}"${c.expectedTier ? ` tier "${c.expectedTier}"` : ''} for "${c.productName}" in matches, got none (status=${resp.status || resp.error})`);
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

  // Root-cause guard for collision cluster #7's deliberately-excluded
  // half: Spazioteca (SistemiGiorno) derives the same bare ('Laccato',
  // 'Opaco','Essenza','Lucido','Spazzolato') key as Ala/Venere, but a
  // side-legend column (a separate "Anta scorrevole L" width-options
  // list) bleeds onto the SAME physical line as some real data rows via
  // columnar pdftotext extraction -- confirmed the generic dims-capture
  // cannot currently tell that stray leading number apart from a genuine
  // 2nd dimension (code 47Q9C would parse as size "90×97" instead of the
  // correct single "97"). Excluded via a product_name guard in
  // parse_file_pianca_shape_b_named. NOT a REJECT_CASE (unlike Icaro/
  // Ettorino/Pedane/Scacco) because this product is NOT wholly
  // known_gap -- it already has substantial real price data from OTHER,
  // unrelated tables in the same file, so "0 rows total" is the wrong
  // assertion; the guard instead checks the specific corruption-prone
  // code never appears at all.
  {
    const resp = await postChat('Pianca', 'Spazioteca (SistemiGiorno) price');
    const rows = (resp.matches || []).filter(m => m.product_name === 'Spazioteca (SistemiGiorno)' && m.code === '47Q9C');
    const ok = rows.length === 0;
    if (!ok) {
      failures.push(`[guard-spazioteca-scorrevoli-not-corrupted] expected code "47Q9C" to be absent (excluded, corruption risk), found ${rows.length} row(s): ${JSON.stringify(rows)}`);
    }
    console.log(`[guard-spazioteca-scorrevoli-not-corrupted] ${ok ? 'ok' : 'FAIL'}  code-47Q9C-rows=${rows.length}`);
  }

  // Cluster #6's own "Vetro" 2-column fix (People (SistemiGiorno)'s 6
  // "Telaio" aluminum-frame tables, out of 49 total occurrences of the
  // bare key, print only 2 real Frontali columns instead of the standard
  // 5 -- silently produced ZERO rows before this fix, since the hardcoded
  // 5-column config made `trailing` fail to match a real 2-cell row).
  // Checked via a DIRECT prices.json read rather than the chat endpoint:
  // every one of these codes ALSO genuinely duplicates a same-size code
  // in People (SistemiGiorno)'s own standard 5-column table elsewhere in
  // the source (confirmed real, same "same code/size, two source tables"
  // pattern as the cop-cos/5P2MHY cases above) -- the pre-existing
  // ambiguous-price safety net correctly hides ALL of this fabric_tier's
  // rows from any live chat query as a result, which is correct behavior
  // but makes the chat endpoint unable to exercise this specific fix.
  {
    const pricesPath = path.join(ROOT, 'data', 'Pianca', 'prices.json');
    const prices: PriceRow[] = JSON.parse(fs.readFileSync(pricesPath, 'utf-8'));
    const vetroRows = prices.filter(r =>
      r.product_name === 'People (SistemiGiorno)' && r.code === '5P526' && r.fabric_tier === 'Vetro'
    );
    const prices328 = vetroRows.some(r => r.price_eur === '328');
    const prices379 = vetroRows.some(r => r.price_eur === '379');
    const ok = vetroRows.length === 2 && prices328 && prices379;
    if (!ok) {
      failures.push(`[cluster6-people-sg-vetro-2col-not-dropped] expected 2 "Vetro" rows for code 5P526 (328, 379), got ${JSON.stringify(vetroRows)}`);
    }
    console.log(`[cluster6-people-sg-vetro-2col-not-dropped] ${ok ? 'ok' : 'FAIL'}  vetro-rows=${vetroRows.length}`);
  }

  console.log('\n' + '='.repeat(70));
  console.log(`Total cases: ${CASES.length + REJECT_CASES.length + 2}`);
  console.log(`Failures: ${failures.length}  <-- must be 0`);
  if (failures.length > 0) {
    console.log('\nFAILURES:');
    failures.forEach(f => console.log(`  ${f}`));
  }

  if (failures.length > 0) {
    console.log('\nEXIT 1: Pianca known_gap shape-batch regressed.');
    process.exit(1);
  }
  console.log('\nEXIT 0: dims+1/Letti/Composizione-bundle/Armadi-danger-table batches verified, numeric-code and wider-table corruption guarded, cross-page contamination guarded.');
}

main();
