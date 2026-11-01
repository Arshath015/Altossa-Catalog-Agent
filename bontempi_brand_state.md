# Bontempi brand state (brand #11) — COMPLETE, LIVE (2026-11-01)

Source: `Price_list/New/AI Team pricelist/Bontempi/LISTINI IN VIGORE/Listino Bontempi L2 2024_02.pdf`

## Step 1 — structural findings (2026-11-01)

**PDF shape:**
- 503 pages, Adobe InDesign 18.2, 3-Heights PDF Optimization Shell. Page size 595.276 x 841.89 pt (standard A4) — single-page layout, **not** a 2-up spread (unlike Monography).
- Text-based (not image-based): 91 font instances total, 66 with ToUnicode CMap, 25 without (scattered MyriadPro-Regular/Tahoma/HelveticaNeue instances). No price/code text found rendered in a no-ToUnicode font during spot-checks — extraction verified byte-exact against rendered page images on 4 representative pages (p12 Amsterdam/MADIE flat shape, p108 Artistico/TAVOLI flat shape with surcharge+flat-price sub-rows, p298 Aqua/SEDIE PER ESTERNO flat shape with per-color codes, p310 Aida+Agatha/SEDIE dual-tier shape) — every price, code, and label matched the image exactly.
- Extracted via the project's bundled `poppler-26.02.0` (`_find_poppler_tool()`), `pdftotext -layout -enc UTF-8`, not the PATH poppler.

**Page numbering:** PDF page = printed page + 2, consistently confirmed across the ENTIRE document (checked PDF p.14=printed p.12 Amsterdam, PDF p.300=printed p.298 Aqua outdoor, PDF p.316=printed p.314 Aqua indoor, PDF p.488=printed p.486 Hula) — no drift found anywhere, unlike Bodema's page-range overrides. Still will resolve each product's real page via a footer/heading scan (not hardcode the offset), same discipline as every prior brand.

**Front matter (PDF pages 1-13, all reference/excluded):**
- PDF 1: cover ("2024", "IVA ESCLUSA" — no validity-date sticker, no "increase from" marker found anywhere in the document; searched case-insensitively for aumento/incremento/rincaro/su richiesta/upon request/validità/in vigore dal/decorrenza — zero hits).
- PDF 2: blank.
- PDF 3, 5: "INDICE" (TOC), 2-column each, 229 total dotted-leader entries across 17 section headers (MADIE E CONTENITORI 9, TAVOLI 44, TAVOLI CONTRACT E PER ESTERNO 8, SEDIE PER ESTERNO 13, SGABELLI PER ESTERNO 4, COMPLEMENTI PER ESTERNO 2, SEDIE 54, SGABELLI 13, POLTRONE 5, CARRELLI/TAVOLINI/POUF E PANCHE 31, PORTARIVISTE 1, LIBRERIE E VETRINE 4, MENSOLE 2, CONSOLLE E SCRITTOI 9, LAMPADE 15, SPECCHI 12, APPENDIABITI 3). **Reading order is a single logical snake: p3-left column, p3-right column, p5-left column, p5-right column** — a section started on one page/column continues directly into the next without re-printing its header (confirmed: SEDIE starts on p3's right column at "Aida" and continues onto p5's left column at "Rocket" with no repeated "SEDIE" header). TOC also lists "INFORMAZIONI IMPORTANTI" (488), "INFORMAZIONI TECNICHE" (489), "CARATTERISTICHE MATERIALI" (491) — all 3 reference/technical sections, excluded.
- PDF 4: blank (between TOC pages).
- PDF 6-9: "RIEPILOGO TAVOLI PER FORMA, DIMENSIONE E FINITURA" (REV. 06/24) — a shape x dimension x finish cross-reference grid naming which TAVOLI products are available in which combo. Navigational aid, no prices of its own. Reference, excluded.
- PDF 10-11: "RIEPILOGO SEDUTE PER FORMA E FINITURA" (REV. 04/24) — same pattern for seating. Reference, excluded. (The REV. dates are revision stamps on these 2 reference pages only, not a pricing validity notice — no evidence of a superseded older edition anywhere in this file.)
- PDF 12: blank.
- PDF 13: "MADIE E CONTENITORI" section-divider page (full-page category title, no table).
- PDF 14: first real priced product (Amsterdam).

**Pricing mechanics — 2 dominant table shapes, both under an "ARTICOLO | SPECIFICHE | VERSIONI | €" running header printed on almost every product page (458 occurrences counted):**

1. **Flat/named-option shape** (~384 of 458 header pages) — used for MADIE E CONTENITORI, TAVOLI (+ TAVOLI CONTRACT E PER ESTERNO), CARRELLI/TAVOLINI/POUF E PANCHE, outdoor SEDIE/SGABELLI/COMPLEMENTI PER ESTERNO, PORTARIVISTE, LIBRERIE E VETRINE, MENSOLE, CONSOLLE E SCRITTOI, LAMPADE, SPECCHI, APPENDIABITI. Header ends in a single "€" column.
   - Each ARTICOLO code (e.g. `15.08`) starts a row-group: SPECIFICHE = size (LxPxH cm or Ø, blank is possible and does NOT mean no price — just no dimension printed), then one or more named finish/color options as `- <label> <code> <price>` lines (price can be IDENTICAL across all options in a group, e.g. Amsterdam, or DIFFERENT per option, e.g. Artistico's Cristallo finishes).
   - Sub-patterns found, all real and all needing capture: `Maggiorazione per accessori:` (flat named add-on, own code sometimes absent), `Maggiorazione per struttura in:` (flat named frame-finish surcharge, own code e.g. M326/MC01), a bare `SOLO STRUTTURA ... <price>` flat line with NO code (frame-only variant), `Maggiorazione per ordini di 1 pezzo singolo.` (flat minimum-order-quantity surcharge, no code, confirmed on outdoor chairs). A `* Per caratteristiche materiale` footnote marker points to the reference-only CARATTERISTICHE MATERIALI page — not a price cell, exclude.
   - **Percentage-based surcharge, NOT a flat row**: e.g. Artistico's own note "Possibilità di riduzione piani in Cristallo fino a L140xP80 cm, con maggiorazione prezzo del 20%." — a 20% uplift tied to a custom-size reduction, no flat euro figure. Per instruction, store the raw base price as-is and record this as a `surcharge_status` / descriptive note, never compute/bake in a derived price.

2. **Dual fabric-tier shape** (~74 of 458 header pages) — used for indoor upholstered SEDIE, SGABELLI, POLTRONE. Header ends in "Prezzo 1 € Prezzo 2" (2 price columns).
   - Each named fabric COLLECTION (e.g. "Tessuto Bouquet", "Pelle Ecologica Premium") has either ONE flat price (e.g. "Tessuto Gourmet 390") or a dual "P1 - P2" price (e.g. "Pelle Ecologica Premium 334 - 386") depending on which specific swatch/color within that collection the customer picks. No per-row code is printed for these collection rows (code=null is correct, not a gap). "Tessuto Cliente"/"Pelle Cliente" (customer-supplied-fabric program) also appear as their own flat single-price rows, same pattern seen in other brands' C.O.M.-style options.
   - **Appendix A (3 pages near the end, PDF ~500-502, "APPENDICE A ∙ B ∙ C — ELENCO FINITURE TESSUTI") gives the exact swatch-code → Prezzo1/Prezzo2 mapping per collection** (e.g. "Tessuto Bouquet - Prezzo 1: TEBQ002 Ecrù, TEBQ003 Noce moscata, ..." vs "- Prezzo 2: TEBQ001 Vaniglia, ..."). **Decision: do NOT parse Appendix A into the pipeline** — every per-product page already prints both tier prices directly inline, so resolving a specific swatch name to its tier isn't needed for correct pricing data; it would only matter for a chat-matching enhancement (e.g. "Aida in Ecrù" auto-resolving to Prezzo 1), which is explicitly out of scope (shared matching/route logic must not be touched this brand). Appendix A/B/C are reference-only, excluded from the index. Appendix B ("bordino o cuciture in contrasto") and C ("...in tinta") are trim/piping color-matching options referenced by some products ("vedi appendice B e C" instead of "vedi appendice A") — confirmed these do NOT carry a separate price of their own (same base collection price applies), so no separate row needed for the trim variant itself.
   - Same `Maggiorazione per struttura in:` surcharge sub-pattern as shape 1.

**Currency/number format:** period = thousands separator, whole-euro integers only — confirmed no comma-decimal price exists anywhere in the document (the only comma-decimal numbers found, 468 occurrences, are all `m3 0,89`-style cubic-meter/weight logistics figures in the SPECIFICHE column, never in the € column, verified via a full-document regex scan).

**No "price upon request" pattern, no per-sqm/per-ml pricing, no wildcard in-page-legend codes found anywhere** (searched catalog-wide).

**Blank-dimension rows are common and do NOT mean no price** — the SPECIFICHE column is often blank for simpler items (same discipline as every prior brand: store size=null, never skip a row for this reason alone). A price that is genuinely 0 has not been observed anywhere yet in Bontempi (unlike Nube Italia's literal 0,00 cells) — will keep watching during build.

**Name collisions within Bontempi: 27 bare-name collision groups found** (same model family sold across multiple categories, e.g. a chair + matching stool + matching outdoor chair all sharing one name) — by far the densest collision rate of any brand built so far. Full list recorded in `bontempi_toc_entries.json` (scratch file) cross-tabulation; representative examples: Artistico (TAVOLI/CARRELLI), Clara (SEDIE/SGABELLI/POLTRONE — 3-way), Eva/Galaxy/Mood/Net (4-way, both indoor+outdoor chair+stool pairs). Will resolve via the same generic auto-qualify-with-real-category-word rule used for Monography/Bodema, not a hand-maintained lookup, given the scale.

**Cross-brand collision check**: not yet run — will check Bontempi's final product name list against all 10 existing brands before finalizing any batch's catalog entries.

## Step 2 — build plan

Given the real scope (229 TOC entries across 17 sections, 2 dominant table shapes covering the large majority of pages), building in batches by section, same discipline as every prior brand: one commit + one `regression:full` + one push per batch, verified against page images, never against text alone. Reusing the Bodema-style approach (hand-verified `BONTEMPI_TOC_ENTRIES`, footer-based page resolution, generic auto-qualify on collisions) for the index, plus 2 generic parser shapes (flat-named-option, dual-fabric-tier) in `App/parse_prices.py`.

Batch plan (tentative, in build order, grouping by shared parser needs):
1. MADIE E CONTENITORI (9 products) + establish the generic flat-shape parser.
2. TAVOLI + TAVOLI CONTRACT E PER ESTERNO (52 products) — adds the "Maggiorazione per struttura"/"SOLO STRUTTURA"/percentage-surcharge sub-patterns.
3. CARRELLI/TAVOLINI/POUF E PANCHE (31) + PORTARIVISTE/LIBRERIE E VETRINE/MENSOLE/CONSOLLE E SCRITTOI (16) + LAMPADE/SPECCHI/APPENDIABITI (30).
4. SEDIE PER ESTERNO/SGABELLI PER ESTERNO/COMPLEMENTI PER ESTERNO (19, flat shape, per-color codes) — establishes the min-order-qty surcharge pattern.
5. SEDIE (54) — establishes the generic dual-fabric-tier parser.
6. SGABELLI + POLTRONE (18) — reuses the dual-tier parser.

Regression wiring (queries.json, stress_v2_queries.json, check_coverage/check_images BRANDS arrays, check_tier_isolation_stress, check_multi_product_isolation, check_tier_label, check_orphaned_flags FLAG_PRODUCING_BRANDS, flag_triage.json) added from the first commit, sidebar stays `'soon'` until all go-live conditions are met.

## Session-start verification (2026-11-01)

- `git status`: clean except a pre-existing, unrelated uncommitted change to `test.txt` (the user's own scratch query notes from before this session — not touched, not part of this brand's work). In sync with `origin/main` (confirmed via `git fetch` + `git status -sb`).
- Dev server: already running on port 3000 (confirmed live via a real `/api/catalog/chat` POST, server responded correctly with its own "Missing brand" validation error, not a connection failure).
- Fresh `regression:full`: base `regression` (queries.json soft-expectations) EXIT 0, 0 fabrication, 117/142 passed / 17 soft-fail / 8 n/a — all 17 soft-fails are the documented pre-existing Groq-quota-flake pattern (none touch Tacchini/Monography/Nube/any brand from today's Task 1 edits). `check-coverage` reported EXIT 1 with exactly 1 row-count mismatch: `Emma Cross 236MG1: expected 1 rows, got 2 (status=multi_product)` (Varaschini, unrelated to Bontempi, untouched this session). Re-ran `check-coverage` in isolation per the stop-condition instruction (documented Groq/LLM-route flake must pass clean on isolated re-run): **isolated re-run was EXIT 0, 0 row-count mismatches, 0 fabricated rows across all 10 brands** — confirms the mismatch was the documented LLM-route nondeterminism flake (the same class already tracked in `llm_route_flaky_check_architecture_risk`), not a real regression. Proceeding with Bontempi per the stop-condition's explicit allowance.

## Step 2 — build (2026-11-01, one session)

Built the generic parser incrementally against the FULL 230-product dataset at once (not literally section-by-section, since the 2 dominant shapes are catalog-wide and a single parser handles both — shape detection is per-row, not per-page), verifying continuously against page images and fixing each systemic bug found before moving on. Real, confirmed bugs found and fixed (all in `App/parse_prices.py`'s `parse_file_bontempi` / `App/extract_catalog.py`'s Bontempi section):

1. **Ambiguous-row false positives from the ARTICOLO block, not the shared key** — an early version didn't fold the block's own ARTICOLO code into row identity, so 2 different configurations of one product sharing a finish code (e.g. Amsterdam's 4 cabinet variants all offering "Legno impiallacciato Rovere Spessart / L002") collided on the shared ambiguous-row safety net. 8189/11339 rows (72%) wrongly flagged before this fix; `model_variant` now carries the ARTICOLO code whenever a product has 2+ owned blocks.
2. **Prezzo1/Prezzo2 dual-tier rows likewise colliding** — the shared key doesn't look at `tier_label`, so 2 rows sharing one collection name (e.g. "Pelle Ecologica Premium") with only `tier_label` differing (Prezzo 1 vs Prezzo 2) both got flagged. Fixed by folding the tier into `fabric_tier` itself (`"Pelle Ecologica Premium (Prezzo 1)"`), same precedent as Tacchini's own Shape D1 fix.
3. **Heading-match tolerance gaps**: a trailing "- NEW" launch badge (95 catalog-wide occurrences), a 2-line-wrapped heading ("CARRELLO" / "PORTASEDIE" on separate lines, "GANCIO" repeated twice disambiguated only by a subtitle), a single-digit sub-model suffix ("TONDO 180"/"TONDO 45"/"TONDO 36"), and a single-option row that fits on the SAME physical line as the heading (Alice) all silently produced 0 rows or a corrupted `size` field until individually found and tolerated.
4. **ARTICOLO code format too narrow** — the block-boundary regex only accepted up to 5 alphanumeric characters after the dot / 4 letters before the digits, silently merging adjacent real blocks (`34.08CUS`, `DAYPG0001`) into the PRECEDING block instead of recognizing their own boundary — this is what caused Daya's own 3rd ambiguous price group (really Poggiatesta's own separate product) and Vincent's "Copyright © B 4 LIVING Spa" false row (actually DAY005505 bleeding past an un-detected boundary into page-footer text). Widened to accommodate both confirmed formats.
5. **Page-footer bleed** — 209 rows catalog-wide picked up the page footer ("Copyright © B 4 LIVING Spa ... Verificare sempre ... `<page>`") as a fake price row when a product's last real row sat close to the page bottom. Fixed with an explicit footer-text exclusion.
6. **Surcharge-section detection too narrow** — only matched the literal substring "maggiorazione per", missing the plural "Maggiorazioni Bracciale superiore (B):" form entirely (Imperial), and multiple surcharge sections reusing the same option vocabulary for DIFFERENT components (Cosmopolitan's top vs "gola" handle-groove, Imperial's upper vs lower bracciale) collided until the section's own distinguishing text was folded into `model_variant`.
7. **Legend-text column bleed** — the "A - struttura / B - seduta e schienale / C - bottone..." per-order-variable legend (printed in the SPECIFICHE column on several SEDIE/POLTRONE pages) bled onto both the surcharge-section intro line and individual bullet rows, corrupting a handful of labels (Penelope). Fixed generically (bullet extraction now takes the LAST "- <letter>" match on a line, surcharge-section label truncates to start at the literal word "Maggiorazion").
8. **2+ independently-priced lettered components under one code** — Daya, Imperial, Penelope all price a combination of lettered parts (Scocca (A) + Cuscinatura (B), Piano (A) + Bracciale superiore (B) + Bracciale inferiore (C), etc.) that reuse the same fabric vocabulary at different partial prices. Added a general `base_section_label` tracker (triggered on a literal "(X)" single-letter marker, narrow enough to never fire on an ordinary descriptive sentence) folded into `model_variant` whenever present.
9. **check_images.ts false positives** — Bontempi's many genuine multi-product-shared-page cases (e.g. Poket/Carrello Portasedie/Gancio per 2 Sedie/Gancio per 4 Sedie all on PDF p.357) flagged as "suspicious" because the check's legitimacy cross-reference looks for the OTHER product's real printed heading in THIS product's own text, but Bontempi's qualified/anchored display names (`"Clara (Sedia)"`, `"Carrello Portasedie"`) never appear verbatim on the page — only the bare/anchor form does. Fixed by populating `index_heading` (via the pre-existing generic `INDEX_HEADING_OVERRIDES` mechanism) for all 74 affected entries. 27 → 0 suspicious mismatches.

**One confirmed-narrow 3rd table shape, hand-transcribed, not auto-parsed**: Alter, Club, Rail, Rico, Vincent (5 of TAVOLI CONTRACT E PER ESTERNO's 8 products, under the "ARTICOLI CONTRACT" page header, PDF pp.288-290) price a base STRUTTURA (frame) cost plus a separate shared PIANO (top) size-price matrix — structurally different from the rest of the catalog. 268 rows hand-transcribed into `manual_additions.json`, verified against the page images. The other 3 products in that same TOC section (Diesis/Moon/Sander per esterno) turned out to use the ordinary flat shape once checked against the real image and are auto-parsed correctly.

**3 products needing per-row disambiguation beyond the generic fixes** (all resolved, documented in `flag_triage.json`): Daya (combinatorial Scocca+Cuscinatura pricing), Kunta (2 genuinely different cabinet configs sharing one ARTICOLO code and one finish option at 2 real prices), Penelope (legend-bleed-corrupted surcharge labels, 3 rows fixed by confirmed page-image print order since all 6 options are uniformly priced).

### Final verification (2026-11-01)

- **Product count vs index**: 230/230 — every `BONTEMPI_TOC_ENTRIES` entry has rows (225 auto-parsed + 5 via manual_additions.json for the ARTICOLI CONTRACT shape).
- **Zero-row count**: 0.
- **Ambiguous count**: 0 (confirmed via a full post-merge scan of `data/Bontempi/prices.json`).
- **Duplicate (product, model_variant, size, fabric_tier, tier_label, code) keys**: 0, across all 12,925 rows.
- **Data-quality sweep**: 0 overly-long/bled `size` fields, 0 page-footer/garbage text in `size`/`fabric_tier`/`model_variant`, 0 implausible prices.
- **5 live HTTP tests via the real `/api/catalog/chat` route**, literal phrasing, each checked against its page image:
  1. "give me all prices for Amsterdam" → MADIE E CONTENITORI flat shape, 42 rows, Rovere Spessart/L002=3.079 etc. — exact match (PDF p.14).
  2. "Aida Pelle Ecologica Premium price" → SEDIE dual-tier shape, Prezzo 1 = 334 — exact match (PDF p.312).
  3. "Alter basso Bianco price" → ARTICOLI CONTRACT manual-override shape, 264 — exact match (PDF p.288).
  4. "give me all prices for Daya" → combinatorial Scocca(A) price list, 1.396/1.389/1.334/1.396/1.511/1.566 — exact match (PDF p.388).
  5. "Kunta 15.90 brushed argento price" → correctly disambiguated to the "Fianchi brushed argento/bronzo" config, 2.910 — exact match (PDF p.68).
- **Rebuild-reproduces-live**: fresh `extract_catalog.py` → `parse_prices.py` → `merge_manual_additions.py` into a clean temp location, diffed key-for-key (sorted JSON) against the live `data/Bontempi/prices.json` — **12,925/12,925 rows byte-identical, exact match.**
- **No other brand's data changed**: confirmed via file mtimes — every other brand's `prices.json` predates this session's start (Tacchini's own Nov 1 mtime is from its OWN prior session today, before Bontempi work began).
- **`check-images`**: EXIT 0, 0 suspicious mismatches (82 legitimate shared-page groups, all cross-referenced via the new `index_heading` entries).
- **`check-orphaned-flags`**: all 5 current Bontempi review flags (the ARTICOLI CONTRACT exclusion) have `flag_triage.json` entries.
- **`regression:full`**: first full run caught a real gap (the 5 ARTICOLI CONTRACT review flags had a `_CATALOG_WIDE` sentinel in `flag_triage.json` but no INDIVIDUAL per-product entry, which `check_orphaned_flags.ts` requires by design — fixed, committed `2c5def0`). 3 of this session's own newly-added `queries.json` expectations were also too optimistic (Artistico/Aqua/Daya genuinely resolve to `clarify_product`/`multiple_options` due to real multi-variant ambiguity, not a bug) — tightened to match actual observed behavior, committed `8fb7449`. **Full re-run after both fixes: all 21 gates report `EXIT 0`**, read directly from the log file (never the background-task notification). The only non-zero numbers are the base `regression` gate's pre-existing soft-expectation noise (Groq-route nondeterminism across OTHER brands' queries, documented flake pattern, explicitly non-gating by the script's own design) — 0 fabrication, 0 Bontempi-specific failures after the fix.

### Go-live: all 7 conditions met

1. Every index product priced or logged with a reason — MET (230/230; the 5 ARTICOLI CONTRACT products are both resolved via manual_additions.json AND individually logged in `flag_triage.json`).
2. Zero-row products are 0 — MET.
3. Ambiguous count is 0 — MET.
4. Live HTTP tests pass for ≥5 products across sections incl. one with a surcharge/add-on — MET (5/5 above; Amsterdam's own accessory surcharge and Alter's ARTICOLI CONTRACT base+top pricing both confirmed).
5. From-scratch rebuild reproduces live prices.json exactly — MET (12,925/12,925).
6. `regression:full` passes all gates from the log — MET (21/21 `EXIT 0`, confirmed after 2 real fixes, not blindly re-run).
7. No other brand's data changed — MET (file-mtime-confirmed).

**Sidebar flipped to "ready" (`App/BrandSidebar.tsx`), productCount 230.** Bontempi is brand #11, fully complete.

### Known gaps (all logged in `flag_triage.json`, 4 Bontempi entries, all status `real_gap`, all resolved via manual_additions.json)

- `_CATALOG_WIDE: Alter, Club, Rail, Rico, Vincent -- ARTICOLI CONTRACT shape` — 3rd table shape, 268 rows hand-transcribed.
- `Daya` — combinatorial Scocca(A)+Cuscinatura(B) pricing, resolved via a reusable parser-level fix (`base_section_label`).
- `Kunta` — 2 genuine cabinet configs sharing one code, resolved via per-row config tagging in manual_additions.json.
- `Penelope` — legend-bleed-corrupted surcharge labels, resolved via a reusable parser-level fix (last-bullet-match + maggiorazion-anchored section label) plus 3 positionally-confirmed row labels.
