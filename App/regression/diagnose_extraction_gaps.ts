/**
 * regression/diagnose_extraction_gaps.ts
 * ---------------------------------------
 * STANDALONE DIAGNOSTIC -- not wired into extract/parse or any regression
 * gate. Checks, for every brand's catalog_index.json, whether every page
 * in every indexed product's own [pdf_page_start, pdf_page_end] range
 * actually got captured during extraction.
 *
 * Two checks, run per brand depending on that brand's on-disk layout:
 *
 * 1. page_images coverage (works for ALL 6 brands): every catalog_index
 *    entry carries a page_images dict keyed by page number. If a
 *    product's range is e.g. 258-264 (7 pages) but page_images only has
 *    key "258", pages 259-264 were never captured by extract_catalog.py's
 *    page-splitting step for that product -- the same root cause
 *    confirmed by hand for Varaschini's "Emma Sofa" (range 258-264,
 *    page_images only has "258"; page 260 was independently confirmed via
 *    direct pdftotext on the source PDF to contain a real, complete price
 *    grid that never made it into this app).
 *
 * 2. Literal per-page .txt-on-disk check (Varaschini ONLY): Varaschini is
 *    the one brand whose text/ directory is one file per PDF page
 *    (p258.txt, p259.txt, ...), referenced by each catalog_index entry's
 *    own text_file field pointing at the FIRST page's file. The other 5
 *    brands (Cattelan, Bolzan, Bonaldo, Ditre, Pianca) extract one merged
 *    text_file per PRODUCT instead (already confirmed: text/ file count
 *    equals catalog_index.json product count exactly for all 5), so there
 *    is no per-page .txt to check on disk for them the same way -- their
 *    page_images coverage check above is the applicable analogous signal.
 *
 * RUN WITH:  npx tsx App/regression/diagnose_extraction_gaps.ts
 */

import fs from 'fs';
import path from 'path';

const ROOT = path.join(__dirname, '..', '..');
const BRANDS = ['Cattelan Italia', 'Bolzan', 'Bonaldo', 'Varaschini', 'Ditre Italia', 'Pianca'];

interface IndexEntry {
  product_name: string;
  pdf_page_start: number;
  pdf_page_end: number;
  page_images?: Record<string, string>;
}

function checkBrand(brand: string) {
  const dataDir = path.join(ROOT, 'data', brand);
  const indexPath = path.join(dataDir, 'catalog_index.json');
  if (!fs.existsSync(indexPath)) {
    console.log(`\n=== ${brand}: no catalog_index.json found, skipping ===`);
    return;
  }
  const index: IndexEntry[] = JSON.parse(fs.readFileSync(indexPath, 'utf-8'));

  // Check 1: page_images coverage (all brands)
  let productsWithGaps = 0;
  let totalMissingPageInstances = 0;
  const worstOffenders: { name: string; expected: number; actual: number; missing: number }[] = [];

  for (const entry of index) {
    const expected = entry.pdf_page_end - entry.pdf_page_start + 1;
    const actual = entry.page_images ? Object.keys(entry.page_images).length : 0;
    if (actual < expected) {
      productsWithGaps++;
      const missing = expected - actual;
      totalMissingPageInstances += missing;
      worstOffenders.push({ name: entry.product_name, expected, actual, missing });
    }
  }
  worstOffenders.sort((a, b) => b.missing - a.missing);

  console.log(`\n=== ${brand}: ${index.length} products ===`);
  console.log(`page_images coverage gap: ${productsWithGaps} products under-covered, ${totalMissingPageInstances} total missing-page instances`);
  if (worstOffenders.length > 0) {
    console.log(`  Worst 5: ${worstOffenders.slice(0, 5).map(o => `${o.name} (${o.actual}/${o.expected})`).join(', ')}`);
  }

  // Check 2: literal per-page .txt-on-disk (Varaschini only -- see module
  // comment for why this doesn't apply to the other 5 brands' layout)
  if (brand === 'Varaschini') {
    const textDir = path.join(dataDir, 'text');
    let productsMissingTxt = 0;
    let totalMissingTxtInstances = 0;
    const txtOffenders: { name: string; missingPages: number[] }[] = [];

    for (const entry of index) {
      const missingPages: number[] = [];
      for (let p = entry.pdf_page_start; p <= entry.pdf_page_end; p++) {
        const txtPath = path.join(textDir, `p${p}.txt`);
        if (!fs.existsSync(txtPath)) missingPages.push(p);
      }
      if (missingPages.length > 0) {
        productsMissingTxt++;
        totalMissingTxtInstances += missingPages.length;
        txtOffenders.push({ name: entry.product_name, missingPages });
      }
    }
    txtOffenders.sort((a, b) => b.missingPages.length - a.missingPages.length);

    console.log(`\nVaraschini literal per-page .txt-on-disk gap: ${productsMissingTxt} products missing at least one page's .txt, ${totalMissingTxtInstances} total missing-page-.txt instances`);
    if (txtOffenders.length > 0) {
      console.log(`  Worst 5: ${txtOffenders.slice(0, 5).map(o => `${o.name} (missing pages: ${o.missingPages.join(',')})`).join(' | ')}`);
    }
  }
}

function main() {
  for (const brand of BRANDS) checkBrand(brand);
  console.log('\n' + '='.repeat(70));
  console.log('Diagnostic only -- no pass/fail gate, no exit code semantics.');
}

main();
