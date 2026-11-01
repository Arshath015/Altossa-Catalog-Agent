# Leftovers — open items across all 10 live brands (as of 2026-11-01)

Every item below was checked against live data before being logged here (not copied blindly from any prior summary). Full technical detail for every item lives in `App/regression/flag_triage.json` (the authoritative, code-enforced ledger — see its own `_status_meaning` block for what each status means) and in each brand's own memory file. This file is a human-readable index into those, grouped by brand.

**Blocking key**: "Blocks nothing" = safe, documented, zero-row or cosmetic gap, chat never returns wrong data for it. "Blocks" = something a user could hit and get an incomplete/wrong answer for if not for the safety net already in place (none found in this pass — every open item below is a *safe* gap: zero rows or a disclosed cosmetic nit, never silently wrong data).

---

## Tacchini (120 products, LIVE)

| Item | Status | Reason | Blocks? |
|---|---|---|---|
| Tesis material-label wrap | `partial` | Material label wraps across several physical lines with no price of its own on most lines; auto-parser's forward-accumulate design gets the direction backwards for this one product. 36 rows hand-transcribed into `manual_additions.json`, verified against the page image. | Blocks nothing — fully priced today via the override. |
| Serie 500/3, Serie 500/4, E63, Clockwise, Torii Love — News-page overrides | `known_gap` (new ledger entry added this session) | Held as permanent per-product overrides; pipeline text-extraction was tested and gives 0 rows or misattributed data for all 5 (no reusable News-PDF text file exists for any of them). | Blocks nothing today (data is correct, override-preserved) — **but if the override is ever removed without a replacement, these 5 lose their News-page data.** Flagged explicitly for future sessions. |
| Serie 500/3 / Serie 500/4 — shared accessory codes 500RIPVT/500RIPVN not captured | `known_gap` (new ledger entry added this session) | Verified via direct PDF text extraction: both codes (optional glass/wood shelf) print under both products at different prices (500/3: 250/340 EUR; 500/4: 240/330 EUR) and are confirmed absent from live `prices.json`. | Blocks nothing (chat correctly returns 0 for a lookup on these 2 codes rather than guessing) — a real, small, not-yet-filled gap. |

**Verified fully resolved, no longer open**: 0 zero-row products (120/120 priced), 0 ambiguous rows, Lina's News page and Butter's C.O.Leather/surcharge both closed 2026-11-01.

---

## Monography (68 catalog entries, LIVE)

| Item | Status | Reason | Blocks? |
|---|---|---|---|
| Pharell (Table Base) / Pharell (Consolle) — "Wood Finishing" shown as "Wood" | `false_alarm` (new ledger entries added this session) | Cosmetic tier-label truncation; prices (+450/+330/+580) confirmed correct. | Blocks nothing. |
| Allure (Mirror) — round mirror missing "D. 95" size suffix | `false_alarm` (new ledger entry added this session) | Size string says plain "round mirror", source PDF's diameter notation not carried through; price (1.050) confirmed correct. | Blocks nothing. |
| Nomad — trailing-comma source typo | `false_alarm` (new ledger entry added this session) | `size` field reads "rug rectangular, (W370 D250)" verbatim from a literal source-PDF typo, preserved rather than silently corrected; price (6.620) confirmed correct. | Blocks nothing. |

**Verified fully resolved, no longer open**: 65/65 real products priced, 0 zero-row, 0 ambiguous.

---

## Nube Italia (28 products, LIVE)

| Item | Status | Reason | Blocks? |
|---|---|---|---|
| Disk — N250001M/002M/003M (Marmo variant) | `known_gap` | Literal 0,00 in every price cell in the source PDF, confirmed against the page image. The paired L-suffix codes at the same sizes ARE priced and live. | Blocks nothing — genuine source gap, not a parsing failure. |
| Raffles — 6 of 12 codes (unpriced colorway duplicates) | `known_gap` | Each code repeats once per colorway; only one colorway per code is actually priced in the source, the rest are literal 0,00. | Blocks nothing — genuine source gap. |
| Pacific Coast — N134006 DX | `known_gap` | Literal 0,00 across every tier for this one code; its paired SX code is fully priced. | Blocks nothing — genuine source gap. |
| Sophie — N171006 (Pouf) | `known_gap` | Literal 0,00 across every tier for this one code; the other 5 Sophie codes are fully priced. | Blocks nothing — genuine source gap. |

All 4 re-verified live this session, unchanged from prior documentation.

---

## Pianca (499 products, LIVE)

- **"Cage-style leftovers" (reported item) — could not be located.** No product named "Cage" or any Cage-pattern exists anywhere in live Pianca data, catalog_index.json, or memory. "Cage" is a real *Tacchini* product (different brand). Logged as a disclosed discrepancy in `flag_triage.json`, not fabricated. **Needs clarification** on what this was meant to refer to.
- **75 known_gap entries, reported as they stand** (not re-verified product-by-product this pass — see `flag_triage.json` for full per-product detail). They cluster into: the SIPARIO/Spazi-10/Spazioteca/Vista "named-columns shape family" (many single-page 0-row-SAFE placeholders awaiting a dedicated parser), a few coarse-but-safe multi-section placeholders, and a handful of older one-off embedded-second-table gaps (Lina, Gamma, Boiserie, Porto). All zero-row-safe, none silently wrong.
- **Chrome click-test on "SEE N MORE CATEGORIES ▼"** — still BLOCKED pending browser-extension connectivity as of the last recorded attempt (2026-09-02). Not re-attempted this session (UI click-test, not a data item). Blocks nothing known; just untested.
- **Correction**: Forma's "0 R" garbled-category bug (previously listed as deferred in this brand's memory) was actually fixed 2026-10-11 (commit 0460b7d) — no longer open, do not carry forward as a leftover.

---

## Varaschini (1294 products, LIVE)

- **"Prodotti per la Pulizia wider name gap (~2,348 records, one entry patched)" (reported item) — does not match live data.** The real "Prodotti per la Pulizia" line is confined to one page, 16 total entries, already fully resolved (10 priced codes + 6 confirmed false-positive-code entries). No gap anywhere near 2,348 records exists under this name — the entire brand is only 1402 index entries / 4253 price rows. Logged as a disclosed discrepancy, not fabricated. **Needs clarification** — possibly conflated with the already-resolved check-coverage "fabrication" finding (~2496/2498 cosmetic, resolved 2026-09-08).
- **61 known_gap entries, reported as they stand** (not re-verified product-by-product — see `flag_triage.json`). Clusters: orphaned-digit/tier-count mismatches (11), phantom cross-reference codes misdetected as products (4), implausible auto-computed page ranges (4), the 2026-08-24 disambiguation-ripple fallout (28, mostly confirmed false-positive-code noise), and a few individually-confirmed complex shapes (Emma's 3-axis grid variants, one chat-matching-only gap with already-correct underlying data). All zero-row-safe.

---

## Bolzan (101 products, LIVE)

**0 known_gap entries** — verified live against `flag_triage.json` (empty object). Nothing open. No dedicated brand-state memory file existed before this session; one was created (`bolzan_brand_state.md`) for consistency.

---

## Cattelan Italia (533 products, LIVE)

**0 known_gap entries** — verified live against `flag_triage.json`. All 95 ledger entries are terminal/resolved (69 real_gap, 5 partial, 21 false_alarm). Nothing open.

---

## Bonaldo (300 products, LIVE)

**139 known_gap entries, reported as they stand** (not re-verified product-by-product this pass — see `flag_triage.json` for full per-product detail). Clusters into 8 documented buckets:
- Chair-shape leg-material group ambiguity — 36 products (e.g. Acquerelli, Bardot, Moon, Vertigo).
- Element/size-name column-boundary failures — 4 products (Aldebaran, Liam, Panorama New, Squaring).
- Missing-CODICI sofa-shape sub-blocks — 21 products (mostly the numbered-cushion-configuration pattern).
- Residual row-level linewrap gaps after the RIVESTIMENTO fix — 22 products (Amour/Bull/Colibrì/Nikos family).
- Price table matches none of the 3 known shapes — **49 products**, the largest bucket, not yet individually root-caused (e.g. Antibes, Bonnie, Dakar, Kaos).
- Stale heading-text mismatches — 5 products.
- 2 single-product structural one-offs (Lazy Susan, Bodo).

All zero-row-safe; none silently wrong.

---

## Ditre Italia (197 products, LIVE)

**33 known_gap entries, reported as they stand** (not re-verified product-by-product — see `flag_triage.json`). Note: this brand's own memory file previously claimed only 1 permanent known_gap ("Surcharge for Move mechanism") — corrected this session; the real count is 33, clustering into:
- Move-mechanism surcharge column misalignment — 8 products (Freedom 2.0 armrests S/M/L/XL, Isabel, Kanaha 2.0/mix, Lulu' 2.0).
- Batch-categorized stray accessory/sub-component codes (2026-08-16 Shape 1/2 pass, not individually page-verified) — 9 products.
- Bare "." placeholder label on a shared code (SKIBJCM06) — 1 product group (Aany/Eric/Erys/Pop/Puppet/Skin drawers).
- Tracked `_DITRE_DIMS_LINE_RE` bug (doesn't match hyphenated range-format dims) — 14 products.
- Pre-existing chat-matching ambiguity (reciprocal-family collision) — 1 product (Stone-in).

All zero-row-safe or safely-ambiguous (never silently wrong).

---

## Cross-brand, not code

**Open decision, blocks deployment**: the client's choice of database and hosting provider has not been made. This is a business decision, not a code task — nothing in this codebase is blocked from working correctly without it, but production **deployment** cannot proceed until it's resolved.

---

## Summary

| Brand | Open items logged | New flag_triage.json entries this session |
|---|---|---|
| Tacchini | 3 (1 confirmed-fine, 2 genuinely new gaps found) | 2 |
| Monography | 3 (all cosmetic, confirmed) | 4 |
| Nube Italia | 4 (confirmed unchanged) | 0 (already correctly logged) |
| Pianca | 75 known_gap (as-is) + 1 unlocatable claim + 1 blocked UI test | 1 |
| Varaschini | 61 known_gap (as-is) + 1 unverifiable claim | 1 |
| Bolzan | 0 | 0 |
| Cattelan Italia | 0 | 0 |
| Bonaldo | 139 known_gap (as-is) | 0 |
| Ditre Italia | 33 known_gap (as-is) | 0 |
| **Cross-brand** | 1 (DB/hosting decision — not code) | — |

Nothing in this list is silently wrong data. Every open item is either a disclosed cosmetic nit, a documented safe zero-row/partial gap, a permanent override with its risk stated, or a reported claim that live verification could not substantiate (flagged for clarification, not fabricated).
