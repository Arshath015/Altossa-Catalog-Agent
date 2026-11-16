# Desiree Brand State

## Status: LIVE (2026-11-16) -- brand #12, sidebar "ready", 101/102 products priced

## Source
- **Price list**: `Price_list/New/AI Team pricelist/Desiree/LISTINO IN VIGORE DAL 28 OTT 24/desireelistinoprezzia0.pdf`
- **Valid from**: 28 Oct 2024 (folder name + confirmed "[ 05 2024 ]" version stamp on cover spread)
- **Additional files in folder**:
  - `rugs-and-finishes-fld-tf0723.pdf` — finishes/fabric reference (July 2023), NOT a price list or price-update sheet
  - `CATALOGO/desiree-brochure-dw_2023_video.pdf` — 2023 marketing brochure, NOT a price list

## Structural Findings (Step 1)

### PDF basics
- 234 PDF pages, 10.5 MB
- **2-up spreads**: each PDF page contains 2 printed pages side-by-side
- **Text-based**: embedded fonts (Futura Std family, Slimbach Std), all readable via pdftotext
- Some `uni=no` font objects → occasional encoding risk on individual glyphs; verified on first pass as readable
- Syntax errors in extracted text: cosmetic only (Marked Content metadata), do not affect text content

### Page numbering
- PDF pages 1-4: cover/finishes/navigation (no printed page numbers)
- PDF page 5 onwards: printed pages, pairs per spread
- Formula: `pdf_page = (printed_page + 8) // 2` (integer division)
- Or equivalently: pdf_page k contains printed pages (2k-8) and (2k-7)

### Index / TOC structure
- Printed pages 2-13 (PDF pages 5-10): full multi-section product index
- Sections: DIVANI (sofas), POLTRONE (armchairs), CUSCINI (cushions), LETTI (beds), ACCESSORI, COMPLEMENTI, TAPPETI (rugs), OUTDOOR
- Reference-only sections (excluded): TESSUTO E PELLE (pg 453), CAMPIONARIO (pg 454), CONDIZIONI DI VENDITA (pg 455)
- Printed pg 14 = AVVERTENZE IMPORTANTI (important information / terms), reference-only

### Product count from index
- **Sofas (Divani)**: 29 products (pg 17–220)
- **Armchairs (Poltrone)**: 20 products (pg 221–266)
- **Cushions (Cuscini)**: 3 products — ARREDA, FASCIA, EASY (pg 267–272)
- **Beds (Letti)**: 15 products (pg 273–332)
- **Accessories**: 3 items — RETI, MATERASSI, GUANCIALI (pg 333–336, flat prices)
- **Complementary Items**: 19 products (pg 337–396)
- **Rugs (Tappeti)**: 7 products + custom-size entries (pg 397–422)
- **Outdoor**: 7 entries — 3 sofas, 2 armchairs, 1 complementi, 1 rug (pg 423–452)
- **Total**: ~103 catalog entries (before name-qualification for duplicates)

### Name collisions (same bare name, different sections)
Several product names appear in 2+ sections:
- AVÌ: sofa (pg 34) + armchair (pg 226)
- KARIN: sofa (pg 116) + armchair (pg 244)
- LACOON: sofa (pg 128) + armchair (pg 248)
- TULISS: sofa (pg 200) + armchair (pg 262)
- RITO: sofa (pg 174) + armchair (pg 258) + complementi (pg 374)
- HAB: sofa (pg 112) + bed (pg 302)
- LUDWIG: sofa (pg 150) + bed (pg 310) + complementi (pg 368)
- ZENIT: sofa (pg 214) + bed (pg 330)
- DORVAN: sofa (pg 52) + complementi (pg 348) + outdoor sofa (pg 424) + outdoor complementi (pg 446)
- LACOON ISLAND: sofa (pg 132) + outdoor sofa (pg 438)
- ARLON: sofa (pg 26) + complementi (pg 338)
- KARA: armchair (pg 240) + complementi (pg 362)
- ROLLKING: sofa (pg 180) + complementi (pg 376)
- ELI FLY: armchair (pg 238) + outdoor armchair (pg 442)
- RITO outdoor: standalone outdoor armchair

All collisions qualified with section in parentheses during extraction.

### Price table shapes (verified against page images)
1. **Shape S4 — Indoor Sofas only** (4 price columns):
   - Columns: Feather complete | Fibre complete | Structure cover | Cushion cover
   - Rows: COM (H.1.40, with meters), C, D, E, F, G, H, Top (fabric) + K, W, Y, Z (leather)
   - Code format: 6-digit integer (e.g., 003040)
   - Product code: 2-digit integer header (e.g., "43  AGON")
   
2. **Shape S2 — Armchairs, Beds, Cushions, Outdoor sofas, Outdoor armchairs** (2 price columns):
   - Columns: Complete | Cover
   - Same tier rows as Shape S4
   - Code format: 6-digit integer
   
3. **Shape F — Flat material/finish price** (Complementi, Accessories, Rugs):
   - One price per finish/color variant
   - Finish names appear in Italian/English/Spanish then price
   - Rugs: standard sizes have named color options with flat price
   - Rugs: custom size (SU MISURA) has per-m² price
   - Accessories: flat price by size (beds only: slatted bases, mattresses, pillows)

### Pricing mechanics
- Prices in euros, thousands-dot format (e.g., "2.785" = €2785)
- No VAT included (confirmed: "I PREZZI SONO ESPRESSI IN EURO E NON SONO COMPRENSIVI DI I.V.A.")
- Tier vocabulary:
  - Fabric: COM (customer's own material), C, D, E, F, G, H, Top
  - Leather: K, W, Y, Z
- COM rows include fabric yardage (e.g., "COM H. 1.40 ML 8,50")
- No surcharge stickers or validity date changes found in the document
- Price list dates valid from 28 Oct 2024 as stated in folder name

### Notes on additional reference files used
- `rugs-and-finishes-fld-tf0723.pdf`: consulted to confirm finish names (extra light, bronzo, siena, barolo, ottanio, piombo, cannettato extra light, etc.) match what appears in price tables. All finish names are fully present in the main price list PDF itself.

---
## Build Log

### 2026-11-13 — Step 1 complete
Structural pass done. Baseline regression:full passing (clean across all 11 brands). Proceeding to Step 2 build.

### 2026-11-16 — Price parse built, verified, wired (pre-go-live)

**Result:** 25,842 price rows, 101 of 102 products priced (all but GUANCIALI), 0 ambiguous rows, 0 exact duplicates. `data/Desiree/prices.json` regenerates deterministically from `parse_prices.py --format desiree` (no manual_additions.json; hand-verified blocks live in code, see below).

**Parser design (`parse_file_desiree`, App/parse_prices.py)**
- The PDF is 2-up: pdftotext -layout interleaves the left and right printed pages on the same lines. Each PDF page is cut into a LEFT and RIGHT zone at the x-position of the right page's article codes, and each zone is parsed as its own stream. A product only takes zones whose printed page is in its own index range, or that no other product claims (printed page = 2*pdf-8 left, 2*pdf-7 right).
- FASCIA and EASY share PDF p.139: FASCIA = the whole left printed page (3 cushions: 007224/007234/007260), EASY = the right page (+ pp.140-141). Confirmed on the page image. An earlier "interleaved, can't parse -> known_gap" plan was wrong and was dropped.
- Column MEANING comes from the printed header labels (Completo piuma/fibra/ecopiuma, Fodera, Fodera struttura/cuscinatura/sedile(i)/schienali, Bordino, Zip), mapped to the PDF's own English names; each price is assigned to the header whose x-centre it sits under, never by ordinal position (e.g. POUF LARGE has no Structure-cover column).
- Displaced prices (a diagram number on the same baseline pushes a price onto a neighbouring line) are adopted only if the loose prices under that column equal the missing cells in number, keep page order, sit between their own tier row and the next, and keep the material's prices non-decreasing against the known neighbours; otherwise the cell is left out and flagged. Found one real case this way (SAVOYE Terminale 264 dx/sx Top Complete = 6.230, verified on p.97).
- Article codes: 6 digits, `C`+5 digits (whole compositions), optional suffix `dx`/`sx` (right/left-hand pair sharing one table -> same rows for both codes), `int`, `/ L`.
- `_DESIREE_VERIFIED_ROWS` (code-level, applied only when the parser produced no row for that code): BAOBAB 435343 custom-size rug per sq.m (495 x bianco/corda/terra; p.204 has an empty right half so the zone cut misreads it) and ELI FLY outdoor 006512 chaise (2.628 x lucido bianco/nero + outdoor cover 121; prices sit on the neighbouring baseline). All read off the page images.
- Ambiguity key widened for Desiree only (adds variant_context + size): same code+tier legitimately carries different prices per column/finish.

**Checks run:** every column series is non-decreasing within each material across 2,282 series (the only Top->K steps are the fabric->leather switch); 29 hand-picked values compared with the page images / source text (AGON, HAB bed incl. the split-row Z tier, FASCIA/EASY/ARREDA cushions, ALASIA, SAVOYE).

**Known gaps:** GUANCIALI -- codes and sizes are printed but NO price (flag_triage.json: known_gap, confirmed on the page image).

**Wiring:** BRANDS lists in check_coverage.ts / check_images.ts, FLAG_PRODUCING_BRANDS in check_orphaned_flags.ts, flag_triage.json (GUANCIALI), queries.json #150-159, stress_v2_queries.json #99-102. Server restart needed after any prices.json regen (CatalogChat caches at construction).

### 2026-11-16 -- Go-live

regression:full read from the log (not the task notification): fabrication 0; check-images 0 mismatches; check-coverage 0 row-count mismatches across all brands (Desiree 101 checked, 0 zero-row untriaged); stress-v2 0 fabrication / 0 image-routing; orphaned-flags clean (GUANCIALI triaged); all remaining checks 0 failures. The main `regression` stage reported 17 SOFT expectation failures, all on OTHER brands (Cattelan 11, Bonaldo 1, Pianca 4, Monography 1), none Desiree (queries #150-159 all pass); the server log showed every Groq key rate-limited ("deterministic matching only"), 5 of the 17 are marked degraded. Not proven pre-existing by a baseline replay -- no server or other-brand data was touched in this build.

Deviation from the go-live list: "prices.json has >=1 row per product" is met for 101/102; GUANCIALI (3 pillows) has 0 rows because the PDF prints no price for them (triaged known_gap, chat answers `no_price_data` honestly).

Bare-name collisions (RITO, DORVAN, KARIN, LACOON, LUDWIG, ZENIT, HAB, ARLON, KARA, ELI FLY ...) resolve through `clarify_product` in the chat (tested: RITO). This is NOT the same path as the Pianca "Onda" / Ditre "Papilo" misses, where an exact-name match won silently over same-named siblings -- tracked separately.
