# Bodema brand state (brand #10) — IN PROGRESS

Source: `Price_list/New/AI Team pricelist/Bodema/LISTINI/Bodema_Listino_Italia_2024.pdf`

## Step 1 — structural findings (2026-10-28)

**PDF shape:**
- 226 pages, Adobe InDesign 19.4, real embedded fonts with ToUnicode mapping on the vast majority (a handful of TrueType fonts — Arial-BoldMT, Times-Roman, Tahoma, ArialMT — have no ToUnicode CMap; watch for garbled extraction if any price/code text turns out to use these, verify against images).
- Page size 595.276 x 779.528 pt — single-page layout, **not** a 2-up spread (unlike Monography). Confirmed via `pdfinfo`.
- `pdftotext -layout` extraction verified against rendered page images on 5 representative pages (Ascot p.9, Monica p.94, Richard p.136, Dallas p.203, Cuscinetti p.155, Complementi p.157, Tessuti p.164, Materassi p.192) — text matches images exactly, column alignment intact.
- Page 1 = cover. Pages 2-3 = TOC ("Indice"), 2-column layout per page. Page 4 = "Note informative" (RIVESTIMENTI terms, reference-only, excluded). Real product content starts page 5 onward (PDF page = printed page + 1 consistently, confirmed on multiple samples).

**TOC reconciliation:** 113 raw (name, page) entries across 11 section headers: DIVANI (30), TRASFORMABILI (11), POLTRONE (17), POUFF (4), SEDIE (3), TAVOLINI E COMODINI (17), LETTI (17), TAVOLI DA PRANZO (4), MOBILI E LIBRERIE (3), ACCESSORI (5: Cuscinetti e Accessori / Complementi / Kit Coprimeccanismo / Tessuti / Tappeti), MATERASSI (2: per trasformabili / per letti).

Known TOC duplicate requiring collapse: **RING** is listed under both DIVANI (p.136) and POLTRONE (p.136) — confirmed via direct page check (PDF p.137) that this is ONE physical page/product ("Ring, divano") cross-referenced from both section listings in the TOC for browsability, not two separate price tables. Will collapse to 1 catalog entry. Other same-page-number pairs (CLARA/CLARETTA p.30, CLARISSA p.30, TINO/TINETTO p.150) need the same same-page check during extraction — not yet individually confirmed whether they're genuine distinct products sharing a page vs. another TOC cross-reference duplicate.

**Pricing mechanics (dominant shape, confirmed across Divani/Trasformabili/Poltrone/Pouff/Sedie/Tavolini/Tavoli da pranzo/Mobili/Cuscinetti/Complementi):** tier-grid table, columns PRODOTTO | CODE | METRAGGIO (fabric meterage note, e.g. "Tessuto unico ml 3,50" — descriptive only, not a price) | TESSUTO (Cliente, B, C, EXTRA, TOP, EXTRA TOP, EXTRA LUSSO — 7 sub-columns) | PELLE (PELLE 1, PELLE, PELLE LUSSO — 3 sub-columns) = **10 tier columns, uniform vocabulary across the whole catalog** (a major simplification vs. Nube/Monography/Varaschini's per-product-varying tier shapes). Rows are named size/configuration variants under one product heading (e.g. Richard: Chaise Longue DX/SX, Angolo, Pouff, each own code+row).

**Different shape — MATERASSI (mattresses):** flat per-size single-price list, no tier grid (e.g. "SIMMONS A MOLLE H13 100x200cm € 498"). Confirmed via p.191 (Materassi per trasformabili) — 3 product lines (Beautyrest H13, Beautyrest H18, Poliuretano Espanso H13), ~5 sizes each, third-party Simmons-branded insert, still priced in Bodema's own list so still in scope. Needs its own parser shape.

**Different shape — TESSUTI (p.163, fabric reference):** one priced row (code 09000001, per-linear-meter cost across the same 10 tier columns, with "/" meaning N/A for Cliente and all 3 Pelle columns — store as null, never 0) plus 2 purely-informational tables (care-symbol legend, fiber-composition codes) — reference-only, excluded.

**Embedded multi-product pages:** "Complementi" (p.156) and likely "Cuscinetti e Accessori" (p.154, confirmed itself is NOT reference-only — it's a real priced product table, own codes 990xxx, same tier-grid shape) are themselves fine as single/multi-row pages, but Complementi specifically reuses MAIN collection names as its own row labels (e.g. "Barclay 100x100 H42" ottoman, code BARC022, under the "Complementi" page heading) — these are real, distinct, separately-priced products that collide by bare name with the main sofa products of the same name (Barclay divano on p.9 vs. Barclay complemento/pouf on p.156). Will need per-row extraction from this page into separate catalog entries, qualified (e.g. "Barclay (Pouf)" or similar customer-meaningful qualifier based on the row's own shape/size label — never a generic "(Complementi)" tag that hides what the item actually is).

**Surcharges:** mostly free-text product-description notes tied to specific customization options (e.g. Ascot "Supplemento profilo a contrasto € 85,00", Dallas "versione con basamento perimetrale... supplemento di € 550,00"), not clean separate price-table rows with their own code — confirmed via `grep -c supplemento` = 70 occurrences catalog-wide, essentially all descriptive text, not tabular. No clean generic "surcharge column" to auto-extract like Nube's Rivestimento supplementare. Will NOT fabricate structured surcharge rows from free text; will transcribe the clearly-priced, unambiguous ones per-product into manual_additions.json only where genuinely useful, otherwise leave as product description (not silently dropped — the description text itself is preserved in the per-product text file).

**"Price upon request":** searched `su richiesta` catalog-wide — all occurrences are descriptive text about customization options, not a price-cell value. No "price upon request" cell pattern found yet; will keep watching during per-product verification.

**Name collisions within Bodema** (same bare name, different category/page — all need customer-meaningful qualification): RING (Divani/Poltrone, collapses to 1 per above), BAO (Divani p.14 / Tavolini p.164), BLAKE (Divani p.17 / Tavolini p.166 / Tavoli da pranzo p.169), CI (Tavolini p.171 / Tavoli da pranzo p.172), FLY (Divani p.48 / Tavolini p.177), MAN-GO (Divani p.75 / Tavolini p.181), MR PRESIDENT (Divani p.100 / Tavolini p.182), SOUND (Divani p.144 / Tavolini p.188), DALLAS (Divani p.33 / Letti p.202), MILANO (Divani p.87 / Letti p.209), PROFILE (Tavolini p.187 / Mobili p.196). Plus the Complementi-page collisions noted above. Qualification plan: append the real category word in parens, same convention as Nube/Monography/Ditre (e.g. "Blake (Divano)" / "Blake (Tavolino)" / "Blake (Tavolo da pranzo)").

**Cross-brand collision check:** not yet run — will check Bodema's final product name list against all 9 existing brands' product names before finalizing any batch's catalog entries.

**"ALBERTONE"/"ALL-IN"/"ROMEO"** appear in Materassi-section boilerplate compatibility text but have NO TOC entry and no own product page — confirmed old/carryover model names, not a coverage gap in this catalog.

## Step 2 — build plan

Given the real scope (113 TOC entries, several multi-page/multi-SKU products like Richard spanning 4 pages, plus embedded sub-products on Complementi/Cuscinetti pages), this will be built in batches by section, same discipline as Monography/Nube Italia: one commit + one `regression:full` + one push per batch, verified against page images, never against text alone.

Batch plan (tentative, in build order):
1. POLTRONE + SEDIE + POUFF (simplest, single-shape, no multi-page products confirmed yet) — establishes the generic tier-grid parser.
2. DIVANI (largest section, includes multi-page products like Richard).
3. TRASFORMABILI.
4. TAVOLINI E COMODINI + TAVOLI DA PRANZO + MOBILI E LIBRERIE.
5. LETTI.
6. ACCESSORI (Cuscinetti e Accessori, Complementi — needs the embedded-sub-product extraction, Kit Coprimeccanismo, Tessuti, Tappeti).
7. MATERASSI (flat per-size list, different parser shape).

Regression wiring (queries.json, stress_v2_queries.json, check_coverage/check_images BRANDS arrays, check_tier_isolation_stress, check_multi_product_isolation, check_tier_label, check_orphaned_flags FLAG_PRODUCING_BRANDS, flag_triage.json) added from the first commit, sidebar stays `'soon'` until all go-live conditions are met.

## Step 2 — Batch 1 (2026-10-29): generic tier-grid pipeline built and verified

Built `parse_index_bodema`/`build_bodema_page_map`/`compute_ranges_bodema` in `App/extract_catalog.py` (hand-transcribed+verified `BODEMA_TOC_ENTRIES`, resolved against real pages via footer map, generic auto-qualify on within-brand name collisions using each page's own real category word) and `parse_file_bodema` in `App/parse_prices.py` (the dominant 10-column Tessuto/Pelle tier-grid shape, plus a 7-column Tessuto-only fallback for leather-less products/accessory kit rows).

**Real bugs found and fixed during verification (never shipped silently wrong):**
1. Variant+code regex had no leading-whitespace allowance (`^` anchored with no `\s*`) -- silently dropped EVERY indented row across the whole brand until fixed; caught because Ascot (the very first product checked) came back 0 rows.
2. Code pattern required 2+ trailing digits; Clara/Claretta/Clarissa's real codes (CLARA0/CLARE0/CLARI0) have only 1 -- caused a 3-product shared page to resolve to 0 rows for all 3 until loosened to 1+ digits (re-verified no false-positive collateral: only 5 total 1-digit codes brand-wide, all legitimate).
3. A sub-label line sometimes bleeds onto the line right after the price row (e.g. Monica's "BASAMENTO RIVESTITO" vs "BASAMENTO LUCIDATO", distinguishing 2 otherwise-identically-labeled, differently-priced variants) -- missed entirely at first, silently producing 2 rows with an IDENTICAL `size` string (a real ambiguity risk, caught via a full duplicate-key scan, not by accident). Generalized after finding 3 independently different real text shapes (ALL CAPS, bare dimension pair, lowercase prose) rather than special-casing each.
4. The METRAGGIO column's own "Tessuto unico" header text can itself bleed onto either the sub-label line OR the front of the price row -- both false-positive traps fixed with explicit exclusions once found (the first via a live scan for "tessuto" leaking into `size` strings, the second via Blake (Divano)'s own flagged row).
5. `check_images.ts` and `check_coverage.ts` BRANDS arrays -- only added Bodema to `check_coverage.ts` on the first pass; `check_images.ts` silently never ran for the brand until caught by noticing Bodema was absent from its own regression log section.
6. 2 flag_triage.json entries (Complementi, Kit Coprimeccanismo) were written describing "0 rows" based on a pre-bugfix parser snapshot -- corrected after a full flagged-product row-count audit found Complementi actually has 521 real rows (not 0) and Kit Coprimeccanismo has 7 real rows plus one spurious false-positive flag (a product-availability name list where "ROCK1" coincidentally matched the code pattern). This was an internal-consistency audit catching my OWN stale documentation, not a data-correctness bug.

**Verified via page images (Ascot, Monica, Button, Giulia, Dallas, Richard p.136, Cuscinetti ed Accessori, Complementi, Tessuti, Materassi, Clara/Claretta/Clarissa, Blake continuation) and via a from-scratch rebuild diff (9097/9097 rows byte-identical).**

**Batch 1 close-out numbers:**
- 111 total catalog entries (113 raw TOC entries minus 2 confirmed cross-reference duplicates: Ring, Tino/Tinetto).
- 76 products with ALL their rows cleanly parsed, 0 flags.
- 9 products with SOME real rows plus a small number of residual flagged rows (Barclay 310 rows/2 flagged, Complementi 521/3, Cuscinetti e Accessori 10/1, Jimmy 27/13, Milano Pillow 220/2, Pop.Up 620/2, Regal 47/1, Shanghai 190/4) -- not zero-row, not fully clean either.
- 25 products genuinely 0 rows, all confirmed PARSER-coverage gaps (not source gaps): 20 are the "FINITURE PIANO"/"FINITURA PIANALE E CASSETTO"/"FINITURE RIPIANI" variable-column finish-grid shape (marble/wood table tops) not yet built; 5 are Accessori/Materassi reference-style pages (Tessuti, Materassi x2, Tappeti) needing their own shapes, now that Kit Coprimeccanismo turned out to already work.
- 1 product (Night&Day, alongside the still-flagged Jimmy) uses a shared-code/matrix shape (6 codes sharing just 2 combined price rows) needing a per-product override.
- 9097 total price rows. Cross-brand name collision check: 9 found (Fly/Bolzan, Lazy Susan/Bonaldo, Clessidra/Bonaldo, Luna/Ditre Italia, Sound/Ditre Italia, Regina/Pianca, Maestro/Pianca, Giulia/Tacchini, Carol/Nube Italia) -- no action needed, chat is always brand-scoped, same as every other brand's cross-brand name reuse.
- `regression:full`: 21/21 gates pass (1 Varaschini row-count mismatch hit during one run, confirmed via isolated re-run to be the documented pre-existing LLM-route check-coverage flake, unrelated to Bodema).
- `check-orphaned-flags`: EXIT 0, all 35 Bodema flags triaged.
- `list_orphan_data_files.ts Bodema`: 12 stale files found (from an earlier slug-naming pass before category detection was fixed) and deleted; re-run confirms 0.

## Step 2 — Batch 2 (2026-10-29): finish-grid shape built for 15 products

Built the brand's SECOND real price-table shape: "FINITURE PIANO"/"FINITURA PIANALE E CASSETTO"/"FINITURE RIPIANI" (a variable-column marble/wood/glass top-finish grid, column count 1-10, distinct from the dominant 10-column Tessuto/Pelle fabric-tier grid). Given the real risk of silently misattributing a price to the wrong finish if the wrapped multi-line column headers were reconstructed generically, built this as a hand-verified per-(product, code) override table (`_BODEMA_FINISH_GRID_OVERRIDES` in `App/parse_prices.py`), same discipline as Nube Italia's `_NUBE_CODE_SIZE_OVERRIDES` -- every column list individually read off the real page image, not inferred from text layout alone.

**Confirmed catalog-wide constant:** when a MARMO (marble) group is present, its 7 possible finishes (Brown Emperador, Marron Damasco, Calacatta Oro, Sturdust, Sahara Noir, Saint Laurent, Dover White) always appear in this exact same order -- only WHICH subset is present varies per product (e.g. Blunt has 6 of 7, no Dover White; Fly has 6 of 7, no Marron Damasco; Otto has 6 of 7, no Saint Laurent).

**15 products fully verified and added:** Gitani, Blunt, Box, Ci (Tavolino), Ci (Tavolo da Pranzo), Cinquanta, Clessidra, Fly (Tavolino), Geometric, Lazy Susan, Man-go (Tavolino), Nara, Otto, Profile (Tavolino), Tower. +283 rows (9097 -> 9380).

**1 bug found fixed:** Tower's price row prints all on ONE physical line ("variant CODE value1 value2 value3", no separate price-row line below, unlike every other finish-grid product) -- added `_BODEMA_VARIANT_CODE_INLINE_PRICE_RE` as a fallback match.

**1 product deliberately deferred after investigation, not shipped half-correct:** Bao (Tavolino) -- its BAOT100 code repeats identically across 3 DIFFERENT top-material tables (H.28 TOP MDF/H.27 TOP VETRO/H.29 TOP MARMO) on one page, needing the SAME sub-label-recovery mechanism already built for the tier-grid shape (Monica's "BASAMENTO RIVESTITO" pattern) but not yet ported to the finish-grid parser -- without it, 3 real, differently-priced variants would collapse into one ambiguous label. Its first table (BAOT040) also extracts exactly 9 tokens where the verified 10-column template expects 10, a genuine text-layout edge case not yet root-caused. Caught BEFORE shipping (not after) by checking the actual output against the override, not just trusting the override was right.

**1 product recategorized:** Bipede is NOT a finish-grid product after all -- confirmed via direct text check it has no "FINITURE PIANO" header at all, just 2 flat single-price variants (sharing the literal same printed code "BIPE00", confirmed verbatim in source, not a transcription slip). Moved to the same deferred bucket as Tessuti/Materassi/Tappeti (flat-price shape, not yet built).

Verified: 2 live HTTP tests (Blunt marble-only, Tower inline-price) match page images exactly. From-scratch rebuild reproduces live prices.json byte-identical (9380/9380 rows). `regression:full`: 21/21 gates pass. `check-orphaned-flags`: EXIT 0, 20 current Bodema flags triaged (down from 35). `list_orphan_data_files.ts Bodema`: 0.

Status: Step 1 complete. Step 2 Batches 1-2 complete, committed, verified. 91/111 products now have real data (76 fully clean + 15 new finish-grid + the 9-batch-1 substantial-partial products already counted separately). Remaining 20 flagged products: Bao (Tavolino), Barclay (2 rows), Bipede, Complementi (3 rows), Cubetto, Cuscinetti e Accessori (1 row), Jimmy, Kit Coprimeccanismo (false_alarm, fully resolved), Materassi x2, Milano Pillow (2 rows), Night&Day, Pilar, Pop.Up (2 rows), Profile (Mobile), Regal (1 row), Shanghai (4 rows), Sound (Tavolino), Tappeti, Tessuti. Go-live NOT yet reached (genuine zero-row parser-coverage gaps remain: Bipede, Cubetto, Materassi x2, Pilar, Profile (Mobile), Sound (Tavolino), Tappeti, Tessuti, Bao (Tavolino) = 9 whole products still 0 rows) -- sidebar stays on "soon".
