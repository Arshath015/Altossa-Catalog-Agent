# Tacchini brand state

**Status 2026-10-31**: live in app UI, fully resolved -- the polish list from 2026-10-18 is now closed. Zero-row catalog entries: 0. Ambiguous rows: 0. `flag_triage.json` known_gap count: **5 -> 1** (only Tesis remains, a genuinely deferred News-page shape -- see Part 6). One NEW, serious, previously-undetected bug found and fixed this session (Costela/Lina silently wrong price attribution); one new, disclosed-but-deferred gap found (Lina's own News page, a second unparsed data source). See "Part 6" below for this session's full writeup; Parts 1-5 preserved below it for history.

## Part 6 — Polish list closed: 2 stale-flag corrections, 1 real silently-wrong-data bug fixed, 1 new gap disclosed (2026-10-31)

Asked to close the 5-item Tacchini polish list left open since 2026-10-18 (Basilan Sedia/Rattan's stale status field, plus Roma/Costela/Lina/Basilan's flagged "2-or-3-numeric-column row" codes). One fix per commit, page-image-first, full before/after diff across all 10 brands before each commit.

**Baseline**: git clean and in sync with origin, dev server reloaded for Tacchini, fresh `regression:full` run and read from the log (not the background notification) -- 0 hard failures across every gate, 16 pre-existing Groq-quota soft-failures unrelated to Tacchini (ids outside Tacchini's own query range, same documented flake pattern as every prior session).

**Commit 1 -- Basilan Sedia/Rattan stale status (`App/regression/flag_triage.json` only, no code change)**: its own note already said "RESOLVED 2026-10-12" but the `status` field still read `known_gap`. Re-verified from scratch rather than trusting the note: live `prices.json` has both 0BSLSD01 (Sedia/Armchair, Rattan, EUR1.180) and 0BSLCS01 (Cuscino/Cushion, full 12-tier grid), all 13 rows checked against the page image (basilan_news_p19.jpg) -- exact match. Flipped to `real_gap`. Confirmed safe via `check_orphaned_flags.ts`: the parser currently produces ZERO flags for Basilan at all, so this status is never even reached by the "claimed-fixed-but-missing" check.

**Commit 2 -- Costela + Lina, a real silently-wrong-data bug fixed (`App/parse_prices.py` code change)**: opened each of the 4 flagged products' own page image and read the row by eye, rather than assuming Butter's shape applied. Found 2 distinct groups, not 1:

- **Roma and Basilan: stale flags, nothing to fix.** Both flags were raised 2026-10-04, BEFORE Shape D1 (2+-named-column Rivestimento headers) existed at all. Once Shape D1 shipped 2026-10-12 (commit 7a9439c), both products' real pages parsed completely cleanly with zero flags and zero gaps -- the flag_triage entries were just never revisited afterward. Roma (p35): 3 codes x 12 tiers x 2 columns (Base verniciata/Base cromata) = 72 rows, all verified exact against the image. Basilan (p19): already covered by the single_value_fallback fix above. Neither needed any code change -- just an honest status correction (`known_gap` -> `false_alarm`, since there was never a real gap once the earlier fixes landed).

- **Costela and Lina: a genuine, more serious bug than the original note claimed.** Both have the identical shape: a 2-named-column Rivestimento header ("Standard padding price"/"Wool padding price"), but the 2 columns are NOT 2 options for one code -- they belong to 2 GENUINELY DIFFERENT codes (Costela: 0CSTPL01 standard / 0CSTWLPL01 wool, armchair; 0CSTPF01/0CSTWLPF01, pouf. Lina: 0LNAPL078 standard / 0LNAWLPL078 wool). This is the exact same "2 real codes share one combined grid, 1 code per column" shape already solved for Reversível/Polar/Labanca/Le Mura in Part 2 below -- Reversível's own example in that fix's code comment is literally "standard padding/wool padding", the same pattern. The original flag_triage note claimed "zero additional rows... not a price-correctness issue" -- that was WRONG. Before this fix, Shape D1's own default single-code behavior mis-split the block: tier B got both columns attributed to the first code (one column right, one belonging to the second code misattributed), and every tier from the second code's own first appearance onward got BOTH columns attributed to the SECOND code only -- the first code's own real prices for those tiers were silently missing, and the second code showed a mix of its own real prices and the first code's misattributed ones, all under one code. This was shipping live, silently wrong, confirmed via direct page-image comparison (not caught earlier because, exactly like the original Polar/Labanca bug, the row COUNT looked plausible -- 48 and 24 rows respectively, structurally complete).

  Fixed by adding `'Costela'` and `'Lina'` to the existing `_TACCHINI_D1_TWO_CODE_PRODUCTS` allowlist -- no new logic needed, the two-pass block-scan mechanism already built for Reversível et al. handles this shape correctly once scoped in. Pure code-level fix, no `manual_additions.json` entry needed (same as the other 4 products in that allowlist) -- a from-scratch rebuild reproduces the fix automatically since it's driven by the parser, not hand data.

  **Verification**: full before/after diff across the ENTIRE Tacchini dataset (3738 rows, both before and after) confirmed ONLY Costela and Lina changed -- 0 rows added or removed overall (row count identical, 3738 -> 3738), only content/attribution corrected. Cross-brand MD5 hash of all 9 other brands' `prices.json` files, unchanged. Every corrected value checked against its page image by eye: Costela's 48 rows and Lina's 24 rows both match exactly. Live HTTP route tested with literal phrasing: "Costela standard padding armchair price" now correctly returns only 0CSTPL01's 12 Standard-column rows (B=3.080 through C.O. Leather=Price upon request); "Lina wool padding armchair price" now correctly returns only 0LNAWLPL078's 12 Wool-column rows (B=2.340 through C.O. Leather=Price upon request). `regression:full` clean after the fix, `check_orphaned_flags.ts` clean (0 orphaned, 0 claimed-fixed-but-missing).

**New gap found and disclosed, NOT fixed (out of scope for this ask)**: while reading Lina's page image, found it has a SECOND page registered in `catalog_index.json` (`page_images["1021"]`: lina_news_p21.jpg, the News pricelist, pages 38-39) whose content is NOT reflected in `data/Tacchini/text/lina.txt` at all -- only the main-pricelist page (46) was ever extracted to text. That News page prices the SAME 2 codes (0LNAPL078/0LNAWLPL078) on a completely different axis: 3 named columns ("Struttura T49/T134" frame finish, "Lacca opaca" matt lacquer, "Lacca lucida" glossy lacquer), each with its own full 12-tier grid -- real, additional pricing data, currently entirely unparsed. This is the SAME class of gap already documented for Serie 500/3, Serie 500/4, E63, Clockwise, Torii Love, and Tesis (a News-PDF text-extraction gap, see Part 4's "still open" list below) -- Lina is now a 7th member of that list. Not fixed here: closing it would mean a dedicated News-page text re-extraction pass, a separate and larger task than this polish-list ask. Logged in `flag_triage.json`'s Lina entry so it isn't lost.

**Known gap count**: before this session, 5 Tacchini `flag_triage.json` entries needed action (Roma, Costela, Lina, Basilan, Basilan Sedia/Rattan). After: 1 remains open with an honest, specific, unactioned-on-purpose reason (**Tesis**, deferred since Part 4 -- its own material label wraps across lines with no price of its own, 2 fix attempts tried and rejected, hand-transcribed into `manual_additions.json` instead, not a parser gap). The 4 others are now `false_alarm`/`real_gap` (resolved), and the Lina News-page finding is a brand-new, separately-logged, deliberately-deferred item (not counted against the "5 polish items," since it was not part of the original ask).

---

## Part 5 — Five to Nine ambiguous rows + Butter/Quilt/Victoria known_gap, closed 2026-10-18

Asked to close the 4 remaining Tacchini ledger items, one fix per commit, push after each. All 4 closed, 3 commits (0c43b85 Five to Nine, c76a8e2 Butter, c3240aa Quilt+Victoria grouped since they share one mechanism).

**Five to Nine (commit 0c43b85)**: codes 0FTNDB20001/0FTNWLDB20001 print 3 times on page 18 (plain, with metal table 0FTNTBMT*, with concrete table 0FTNTBCM*). New `_TACCHINI_FTN_SUBVARIANT_SUFFIXES`, a 1-based occurrence counter scoped to exactly these 2 codes, appends the real printed distinguishing text to desc_it for the 2nd/3rd occurrence. 0 ambiguous rows remain (was 28).

**Butter (commit c76a8e2)**: codes 0BTTTB01/0BTTTB02 are Shape A with 3 genuine prices per tier cell (MR1/MR2/MR3 marble-top modules). New `_tacchini_triple_value_fallback`, scoped via `_TACCHINI_MR_PRODUCTS`. +66 rows.

**Quilt/Victoria (commit c3240aa)**: codes 0QLTPSGNZ/0VCTPSGNZ are a joining-plate accessory sitting outside the main 12-tier grid with no label text. New `_tacchini_d1_accessory_match_row`/`_TACCHINI_D1_ACCESSORY_PRODUCTS`. Also corrected a stale note: Victoria's real price is 100/100, not 90/90.

A full from-scratch rebuild reproduces live `prices.json` exactly -- 0 diffs, 3738 total rows (unchanged by Part 6's fix, since that corrected attribution not row count).

**Later polish list (2026-10-18)** -- 5 small, open, non-blocking `flag_triage.json` entries, deliberately NOT investigated that round: Roma, Costela, Lina, Basilan (same boilerplate note, "2-or-3-numeric-column row"), and Basilan Sedia/Rattan's stale status field. **All 5 closed in Part 6 above, 2026-10-31.**

## Part 4 — Shape E (Finitura + 2+ named columns), Cage + 5 more, closed 2026-10-17

New Shape E (`_tacchini_detect_shape` dispatch value `'E'`, `_tacchini_parse_shape_e`): Finitura/Finish header with 2+ named price columns, free-text material labels. Resolved Cage, Chill-Out Table, Soap, Colonna, PietraForma (all previously silently misrouted to Shape B and producing wrong data -- same "silently wrong, not caught by row count" failure class as Part 6's Costela/Lina finding). **Tesis** hand-transcribed into `manual_additions.json` instead of auto-parsed -- its own material label wraps across lines with no price of its own, 2 fix attempts tried and rejected per standing discipline (stop after a 2nd fragile attempt).

A near-miss on data loss was caught and recovered: 5 pre-existing products (Serie 500/3, Serie 500/4, E63, Clockwise, Torii Love) existed only as live `prices.json` content (News-PDF text never extracted), with no git safety net. Recovered via the live HTTP route before regenerating, added to `manual_additions.json`.

**Still open, unchanged from before**: these 5 products' News-PDF text-extraction gap -- re-extracting the News PDF's own text for their specific pages would let them drop out of `manual_additions.json` entirely. Not attempted. **Lina joins this list as a 6th product, found in Part 6 above.** E63's orphaned image reference (page 1030, no `page_images` entry) -- small, disclosed, not fixed.

---

## 2026-10-12 session (Parts 1-3, preserved for history)

**Status as of 2026-10-12**: Three-part session: (1) closed all 4 items from the prior memory snapshot, (2) closed out the ~30-product blast radius that work created, (3) closed D2 (commit 03bd608). Zero-row catalog entries: 11 -> 1 (Cage only, resolved Part 4 above).

## Part 3 — D2 sub-shape, closed (commit 03bd608)

D1's own 2-named-column shape has a sibling: D2, free-text MATERIAL/FINISH name labels (Joaquim, Kelly T) instead of a canonical tier letter. New `_tacchini_d2_match_row`, fallback-only, scoped to `_TACCHINI_D2_PRODUCTS`.

## Part 1 — the 4 original leftovers (commits 7a9439c, 3b5d505, 21dab8c, 5745ea1)

- **7a9439c (Shape D1)**: new dispatch value `'D'` -- a Rivestimento header with 2+ named columns. 12 products resolved.
- **3b5d505**: Agnese's page-range bug + page-verified Cage/Elephant.
- **21dab8c**: Butter/Le Mura XL composition bundle codes.
- **5745ea1**: Basilan/Elephant/Pastilles/Togrul single-value fallback.

## Part 2 — the blast radius, closed (commit ba378a0)

Part 1's own heading-collision fix had a ~30-product blast radius. 10 genuine gains applied (Roma, Sella, Costela, Dialogo, Africa, Lina, Polar Table, Core, T-Table, Reversível). Le Mura XL regression fixed. Core/T-Table cross-contamination fixed. **A genuinely new bug found while verifying Reversível's own gain**: some D1 products print TWO real, independent codes sharing one combined grid -- one code per column (Polar's own Sofa vs Pouf, Labanca same pattern, Le Mura too). `_tacchini_parse_shape_d1` does a 2-pass scan per code-block, scoped via `_TACCHINI_D1_TWO_CODE_PRODUCTS` (deliberately an allowlist, not blanket -- Five to Nine has a superficially similar but structurally different shape). **This is the exact mechanism Part 6 above reused for Costela/Lina, 19 days later.** 5 losses preserved as per-product overrides (Serie 500/3, Serie 500/4, E63, Clockwise, Torii Love) -- real News-sourced data a text-regen can't reproduce.

**How to apply**: this session's discipline -- measure/verify before building (page images, not assumptions), full before/after key-diff across the ENTIRE dataset before shipping, one fix per commit, `regression:full` clean before each commit, disclose anything found outside the explicit ask rather than silently fixing OR silently ignoring it. Repeat for the still-open News-PDF text-extraction list above (now 6 products) whenever that gets picked up.
