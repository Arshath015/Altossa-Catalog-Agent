# Dev ports (fixed for this project)

- Frontend (Vite): **http://localhost:7070** -- `vite.config.ts` pins it with `strictPort`. Port 5173 on this machine belongs to a different app; never use it for this project.
- Backend (Express): **3001** -- run `$env:PORT="3001"; npm run dev:server`.
- Run the frontend with `$env:BACKEND_PORT="3001"; npm run dev:client` so the Vite proxy (`/api`, `/data`) reaches the backend.
- Restart a process by exact PID (`netstat -ano` + `taskkill`), not PowerShell Stop-Process.

# When the user asks "what is left / what to do"

Leftover work (as of 2026-11-16), in this order. Update this list as items finish.

1. **Client-style sweep (do first).** Ask every product of every brand by its bare name, plus "<name> sofa/bed/outdoor" variations, through the real chat path (`/api/catalog/chat`). Flag answers that say "no price data" while a same-named sibling has prices, and answers that do not say several products share the name. Wire it into `regression:full` as a permanent gate. Known cases: Pianca "give onda" (picks the outdoor table with no prices; Onda Indoor and Onda Indoor (Progetti 09) have prices) and Ditre "give papilo sofa" (answers from the bed, not the sofa "Papilo plain - Papilo curvy").
2. **Fix the Pianca Onda price table.** Outdoor "Onda" (PDF p.14, header `L H P CODICI Piano Cover`) has no parser: OON34 concrete top 6.356, OON35 Iroko top 7.201, cover OON34Z 392. Verify on the page image, then scan Pianca for the same "unrecognized price-table header" flag.
3. **Shared matching fix** (changes shared matching/route code, so confirm with the user first): when a name matches several products say so and list them; an extra query word (sofa, bed, outdoor) steers to the sibling whose variants match; when the best match has no prices offer the siblings that do.
4. **Triage the 166 zero-price products** into intentional / parser gap / no price in the PDF (Pianca 48, Varaschini 82 (66 Group A are intentional), Bonaldo 32, Ditre 3).
5. Minor: three Desiree marble finish names still run Italian/English/Spanish together (cosmetic).
Optional later: vision-model extraction pilot on one brand with a text-layer cross-check, to speed up onboarding.

# New brand playbook (use when the user says "new brand")

1. **Structure first.** Bundled poppler `pdftotext -layout -enc UTF-8`; check whether pages are 2-up spreads (Desiree: `pdf_page = (printed_page + 8) // 2`, two printed pages interleaved per text line). Build `data/<Brand>/catalog_index.json`, `text/`, `images/` via `App/extract_catalog.py`. Only the price-list PDF is a source; other files may only clarify finish names.
2. **Parser.** Add `--format <brand>` and `parse_file_<brand>` to `App/parse_prices.py`. Read column meaning from the printed header labels by x-position, never from ordinal position. If a table cannot be parsed safely, flag it; zero rows is safe, a wrong price is not.
3. **Verify against page images,** including every special-case parser (the Desiree GUANCIALI price was missed by reading a bold figure as noise). Run price-order checks per column series and scan labels for garbled text.
4. **Generate** `data/<Brand>/prices.json` (+ `--flags-out flags.json`). `manual_additions.json` replaces whole products when merged, so prefer code-level verified rows for a few cells. Multi-column brands need the ambiguity key widened (see Desiree).
5. **Wire in:** `BRANDS` in `App/regression/check_coverage.ts` and `check_images.ts`; `FLAG_PRODUCING_BRANDS` in `check_orphaned_flags.ts` plus a `flag_triage.json` entry (`{}` if no flags); queries in `queries.json` and `stress_v2_queries.json` (expected values verified against page images).
6. **Run.** Restart the backend by PID (`netstat -ano` + `taskkill`; it caches prices at start), then `regression:full` once with `REGRESSION_BASE_URL=http://localhost:3001`. Read results from the log, not the task notification.
7. **Go live.** Flip `App/BrandSidebar.tsx`, commit specific files only (never `test.txt` or `snapshot/`), push, write `<brand>_brand_state.md` and a memory pointer.

Standing rules: a wrong price is worse than a missing one; disclose confounds immediately; verify the user's exact reported query; commit and push after each batch.
