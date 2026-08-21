/**
 * regression/diagnose_pianca_index_gaps.ts
 * ------------------------------------------
 * STANDALONE DIAGNOSTIC -- not wired into extract_catalog.py or any
 * regression gate. Same purpose as diagnose_extraction_gaps.ts's
 * Varaschini page_images check, one layer up: that check caught pages
 * MISSING from an already-indexed product's own range; this one catches
 * whole PRODUCTS missing from catalog_index.json in the first place.
 *
 * Found by accident (Progetti di Design 09's own "Onda Indoor" table,
 * pdf pages 13-14, sitting in a plain page-range gap between Enea Up and
 * Soffio Up that nothing was checking for -- then a second instance,
 * Progetti 09's own "Clelia" chair, pdf pages 6-7, found only by
 * separately cross-checking the catalog's own printed INDICE). Every
 * Pianca source file confirmed so far prints its own literal INDICE
 * (table of contents: product name + printed start page) within its
 * first ~10 pages -- that INDICE is the catalog's own authoritative
 * product list, so it's the most reliable signal to diff against
 * catalog_index.json, rather than trying to pattern-match "designer
 * credit" headers across every page.
 *
 * METHOD: pull raw text from each file's first INDICE_SEARCH_PAGES
 * pages, regex out candidate (Name, page-number) pairs in both printed
 * orders ("Name NN" and "NN Name" -- both layouts are used, sometimes on
 * the same INDICE spread as a list view + a repeated "card grid" view),
 * drop known category/noise words, and flag any candidate name with no
 * reasonable match anywhere in catalog_index.json's product_name list.
 *
 * Diagnostic only: flags CANDIDATES for a human/agent to verify against
 * the real page (like Varaschini's page_range_boundary_unverified
 * bucket) -- not a claim that every flagged name is definitely a real
 * missing product, and not a guarantee that silence means nothing was
 * missed. Check 1 (per-file, against the full index) alone COULD miss a
 * same-name-different-file collision (an INDICE-listed name matching an
 * existing entry that actually came from a DIFFERENT file) -- this is
 * exactly the ambiguity Onda Indoor and Clelia both turned out to have,
 * and is why check 2 (cross-file collision scan) exists and matters more
 * than check 1 for this specific failure class: on the only 2 files
 * checked so far, check 2 found 2 real hits, both invisible to check 1.
 *
 * extract_catalog.py now writes a source_file field on every NEW/
 * re-extracted entry (added 2026-08-21, specifically to make check 1
 * itself collision-proof going forward, closing the gap check 2 exists
 * to cover) -- check 1 above already uses it when present: a candidate
 * only counts as matched if a matching entry's source_file equals the
 * file being scanned, or the entry predates the field entirely (falls
 * back to the old name-only match). All 31 of Pianca's current entries
 * predate this field, so check 1 still can't tell them apart yet --
 * check 2 remains load-bearing until the touched files are re-extracted.
 * Once a file is re-extracted with source_file populated, check 1 alone
 * becomes reliable for it and check 2 becomes a redundant sanity check
 * rather than the primary signal.
 *
 * RUN WITH:  npx tsx App/regression/diagnose_pianca_index_gaps.ts
 */

import { execFileSync } from 'child_process';
import fs from 'fs';
import path from 'path';

const ROOT = path.join(__dirname, '..', '..');
const PDFTOTEXT = 'pdftotext'; // see memory: windows_poppler_gotchas -- PATH shadowing, always pass -enc UTF-8

const FILES: { label: string; file: string; indiceSearchPages: number }[] = [
  { label: 'Progetti di Design 08', file: '2024_10_Progetti_di_Design_08_1R +6_.pdf', indiceSearchPages: 8 },
  { label: 'Progetti di Design 09', file: '2025_06_Progetti_di_Design_09_1R +6_.pdf', indiceSearchPages: 10 },
  { label: 'Spazi-10', file: '2026_02_Spazi-10_1R.pdf', indiceSearchPages: 6 },
];

const NOISE_WORDS = new Set([
  'Indice', 'Sedie', 'Tavoli', 'Poltrone', 'Madie', 'Divani', 'Sistemi', 'Illuminazione',
  'Complementi', 'Boiserie', 'Passaggio', 'Porta', 'Volumi', 'Pesi', 'Tessuti', 'Pelli',
  'Personalizzazione', 'Sensi', 'Vena', 'Spazio', 'Interactive', 'Legenda', 'Icone',
  'Listino', 'Prezzi', 'Prezzo', 'Progetti', 'Design', 'Spazi', 'Su', 'Misura',
]);

function extractText(pdfPath: string, firstPage: number, lastPage: number): string {
  return execFileSync(PDFTOTEXT, ['-enc', 'UTF-8', '-layout', '-f', String(firstPage), '-l', String(lastPage), pdfPath, '-'], {
    maxBuffer: 20 * 1024 * 1024,
  }).toString('utf-8');
}

function extractCandidates(text: string): Set<string> {
  const candidates = new Set<string>();
  const nameRe = /[A-ZÀ-Ý][a-zà-ÿ']+(?:\s[A-ZÀ-Ý][a-zà-ÿ']+){0,2}/g;

  // "Name ... NN" (name followed within a short distance by a 1-2 digit number)
  const forward = /([A-ZÀ-Ý][a-zà-ÿ']+(?:\s[A-ZÀ-Ý][a-zà-ÿ']+){0,2})\s{1,20}(\d{1,2})\b/g;
  // "NN ... Name" (number followed within a short distance by a name)
  const backward = /\b(\d{1,2})\s{1,20}([A-ZÀ-Ý][a-zà-ÿ']+(?:\s[A-ZÀ-Ý][a-zà-ÿ']+){0,2})/g;

  let m: RegExpExecArray | null;
  while ((m = forward.exec(text))) candidates.add(m[1].trim());
  while ((m = backward.exec(text))) candidates.add(m[2].trim());

  return new Set([...candidates].filter(c => {
    const firstWord = c.split(' ')[0];
    return !NOISE_WORDS.has(firstWord) && c.length > 2;
  }));
}

function normalize(s: string): string {
  return s.toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/[^a-z0-9]/g, '');
}

function main() {
  const idx: Array<{ product_name: string; source_file?: string }> = JSON.parse(
    fs.readFileSync(path.join(ROOT, 'data', 'Pianca', 'catalog_index.json'), 'utf-8')
  );
  const withSourceFile = idx.filter(e => e.source_file).length;
  console.log(`catalog_index.json: ${idx.length} entries, ${withSourceFile} carry a source_file field ` +
    `(added 2026-08-21 -- entries extracted before that fix have none and fall back to name-only matching below).`);

  const perFileCandidates: { label: string; candidates: Set<string> }[] = [];

  for (const f of FILES) {
    const pdfPath = path.join(ROOT, 'Price_list', 'Pianca', f.file);
    if (!fs.existsSync(pdfPath)) {
      console.log(`\n=== ${f.label}: source PDF not found at ${pdfPath}, skipping ===`);
      continue;
    }
    const text = extractText(pdfPath, 1, f.indiceSearchPages);
    const candidates = extractCandidates(text);
    perFileCandidates.push({ label: f.label, candidates });

    const unmatched: string[] = [];
    for (const c of candidates) {
      const cn = normalize(c);
      // Prefer source_file when the matching entry/entries have one: only
      // count it as covered by THIS file if at least one matching entry's
      // source_file is actually this file's own filename. An entry with no
      // source_file (legacy, pre-2026-08-21) still falls back to a bare
      // name match, same as before -- we don't know better for those yet.
      const matchingEntries = idx.filter(e => {
        const n = normalize(e.product_name);
        return n.includes(cn) || cn.includes(n);
      });
      const matched = matchingEntries.some(e => !e.source_file || e.source_file === f.file);
      if (!matched) unmatched.push(c);
    }

    console.log(`\n=== ${f.label} (scanned pages 1-${f.indiceSearchPages} for INDICE) ===`);
    console.log(`Candidate names found in INDICE text: ${candidates.size}`);
    console.log(`Unmatched against catalog_index.json (candidates for a human/agent to verify): ${unmatched.length}`);
    if (unmatched.length > 0) {
      console.log(`  ${unmatched.sort().join(' | ')}`);
    }
  }

  // Check 2: cross-file INDICE name collisions. Check 1 above has a real
  // blind spot -- if file A's INDICE lists "X" and file B's
  // catalog_index.json entry for "X" already exists, check 1 reports "X"
  // as matched for file A too, even if file A's own "X" is a completely
  // different product that was never actually captured (exactly what
  // happened with Onda Indoor and Clelia -- both names appear in BOTH
  // Progetti di Design 09's and Spazi-10's own printed INDICE, as two
  // genuinely different products, but catalog_index.json only has one
  // entry per name). Any name appearing in more than one file's own
  // INDICE is a same-name-collision candidate that needs individual
  // verification regardless of what check 1 found.
  console.log('\n' + '='.repeat(70));
  console.log('Cross-file INDICE name collisions (each needs individual verification --');
  console.log('catalog_index.json has no source-file field, so a name matching in check 1');
  console.log('does NOT prove it was captured from every file that prints it):');
  const nameToFiles = new Map<string, Set<string>>();
  for (const { label, candidates } of perFileCandidates) {
    for (const c of candidates) {
      const cn = normalize(c);
      if (!nameToFiles.has(cn)) nameToFiles.set(cn, new Set());
      nameToFiles.get(cn)!.add(label);
    }
  }
  let collisionCount = 0;
  for (const [cn, files] of nameToFiles) {
    if (files.size > 1) {
      collisionCount++;
      console.log(`  "${cn}" appears in INDICE of: ${[...files].join(', ')}`);
    }
  }
  if (collisionCount === 0) console.log('  none found');

  console.log('\n' + '='.repeat(70));
  console.log('Diagnostic only -- flags candidates for manual verification, not a pass/fail gate.');
}

main();
