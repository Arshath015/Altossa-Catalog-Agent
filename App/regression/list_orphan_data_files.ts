/**
 * regression/list_orphan_data_files.ts
 * -------------------------------------
 * STANDALONE DIAGNOSTIC, brand-agnostic -- the sanctioned replacement for
 * hand-written `rm <slug>*` cleanup globs against data/<Brand>/{images,pages,text}.
 *
 * WHY THIS EXISTS: during Pianca's 2026-08-22 Mambo/Clelia/Onda Indoor
 * collision cleanup, a stale JSON entry ("Mambo", legacy pre-rename) was
 * removed and its files cleaned up with `rm images/mambo_p*.jpg`. That
 * glob also matched and deleted 2 files that were NOT stale --
 * mambo_progetti_06-07_tavolino_p8-08.jpg and
 * mambo_progetti_06-07_pouf_p49-49.jpg -- because both filenames happen
 * to start with the same "mambo_p" prefix as the legacy slug. Caught
 * only by a full disk-vs-JSON audit after the fact, then fixed by
 * regenerating the 2 images from source and verifying byte-for-byte
 * against an independent re-render.
 *
 * This is the SAME failure shape as the file-slug collision bug fixed
 * in extract_catalog.py's main() (see pianca_brand_state.md /
 * pianca_file_collision_verification_limits.md): an operation trusting a
 * NAME/PATTERN match instead of the actual source of truth
 * (catalog_index.json's own images/mini_pdf/text_file fields). The fix
 * there was "compare by source_file, not by name prefix" -- this script
 * applies the identical principle to disk cleanup: compute the
 * referenced-file set from JSON, then treat everything else on disk as
 * the ONLY safe deletion candidates. No wildcard/prefix matching against
 * filenames is ever used to decide what's safe to delete.
 *
 * STANDING RULE (see feedback memory pianca_cleanup_glob_standing_rule):
 * no future cleanup of data/<Brand>/{images,pages,text} may use a
 * hand-written glob or prefix pattern. Run this script for the brand
 * first, review the orphan list it prints, and delete exactly (and only)
 * the files it lists -- never a pattern that merely LOOKS like it should
 * match only the intended files.
 *
 * This script only REPORTS. It never deletes anything itself, since
 * deletion should stay a deliberate, reviewed step -- automating the
 * delete would just relocate the same "trusted a pattern instead of the
 * source of truth" risk into this script instead of removing it.
 *
 * Usage: tsx App/regression/list_orphan_data_files.ts <Brand>
 *        tsx App/regression/list_orphan_data_files.ts --all
 */

import fs from 'fs';
import path from 'path';

const ROOT = path.join(__dirname, '..', '..');
const DATA_DIR = path.join(ROOT, 'data');

interface CatalogEntry {
  product_name: string;
  mini_pdf?: string;
  images?: string[];
  text_file?: string;
}

function listOrphansForBrand(brand: string): { images: string[]; pages: string[]; text: string[] } {
  const brandDir = path.join(DATA_DIR, brand);
  const indexPath = path.join(brandDir, 'catalog_index.json');
  if (!fs.existsSync(indexPath)) {
    throw new Error(`No catalog_index.json for brand "${brand}" at ${indexPath}`);
  }
  const idx: CatalogEntry[] = JSON.parse(fs.readFileSync(indexPath, 'utf-8'));

  const referenced = new Set<string>();
  for (const p of idx) {
    for (const im of p.images || []) referenced.add(im);
    if (p.mini_pdf) referenced.add(path.basename(p.mini_pdf));
    if (p.text_file) referenced.add(path.basename(p.text_file));
  }

  const listDir = (sub: string): string[] => {
    const dir = path.join(brandDir, sub);
    if (!fs.existsSync(dir)) return [];
    return fs.readdirSync(dir).filter(f => !referenced.has(f));
  };

  return {
    images: listDir('images'),
    pages: listDir('pages'),
    text: listDir('text'),
  };
}

function main() {
  const arg = process.argv[2];
  if (!arg) {
    console.error('Usage: tsx App/regression/list_orphan_data_files.ts <Brand>  |  --all');
    process.exit(1);
  }

  const brands = arg === '--all'
    ? fs.readdirSync(DATA_DIR).filter(f => fs.statSync(path.join(DATA_DIR, f)).isDirectory())
    : [arg];

  let totalOrphans = 0;
  for (const brand of brands) {
    let orphans;
    try {
      orphans = listOrphansForBrand(brand);
    } catch (e: any) {
      console.log(`=== ${brand}: ${e.message} ===`);
      continue;
    }
    const count = orphans.images.length + orphans.pages.length + orphans.text.length;
    totalOrphans += count;
    console.log(`\n=== ${brand}: ${count} orphaned file(s) ===`);
    for (const f of orphans.images) console.log(`  images/${f}`);
    for (const f of orphans.pages) console.log(`  pages/${f}`);
    for (const f of orphans.text) console.log(`  text/${f}`);
  }

  console.log(`\n${'='.repeat(70)}`);
  console.log(`Total orphaned files across ${brands.length} brand(s): ${totalOrphans}`);
  console.log('(Report-only. Review each entry, then delete exactly this list --');
  console.log(' never a hand-written glob/prefix pattern. Re-run after deleting');
  console.log(' to confirm the count drops to what you expected, no more.)');
}

main();
