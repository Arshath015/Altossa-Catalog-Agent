/**
 * regression/fix_pianca_composizione_art_codes.ts
 *
 * One-time (but re-runnable) data-population fix for Pianca's 184
 * "Composizione <code> (...)" entries (Composizioni Catalogo, SistemiGiorno
 * pdf pages 332-395). Their real identifying bundle code (e.g. "9201",
 * "S501", "TOT101") already lives in catalog_index.json's own
 * `index_heading` field (set during the original extraction pass so
 * check_images.ts could match each entry's literal printed heading), but
 * was never copied into `art_code` -- the field catalogChat.ts's
 * `findProductsByCode` fast-path actually indexes (constructor: `for (const
 * e of this.catalogIndex) addCode(e.art_code, e.product_name);`).
 *
 * Real user-facing consequence, found 2026-08-23 during a post-extraction
 * chat sanity check: "give me the price for Composizione 9201" fell through
 * to generic name-token matching (which has no numeric-substring
 * narrowing) and returned all 184 Composizioni tied on the shared word
 * "Composizione", instead of narrowing to the one matching entry. This is
 * the chat layer failing to use a code it already has correctly indexed,
 * not a real known_gap (the parser isn't declining to guess on messy
 * data -- the data's already right, it's just in the wrong field).
 *
 * Deliberately narrow, targeted fix -- no findProductsByCode changes.
 * Populates art_code := index_heading for every Composizione entry missing
 * it, since index_heading is already exactly the bare bundle code for all
 * 184 (verified: zero non-code index_heading values in this set).
 *
 * RUN WITH: npx tsx App/regression/fix_pianca_composizione_art_codes.ts
 * Re-run after any future re-extraction of SistemiGiorno that touches the
 * Composizioni Catalogo range, in case catalog_index.json gets rebuilt
 * from scratch and this field is lost again.
 */
import * as fs from 'fs';
import * as path from 'path';

const indexPath = path.join(__dirname, '..', '..', 'data', 'Pianca', 'catalog_index.json');
const catalog = JSON.parse(fs.readFileSync(indexPath, 'utf-8'));

let fixed = 0;
let alreadyOk = 0;
let skippedNoHeading = 0;

for (const entry of catalog) {
  if (typeof entry.product_name !== 'string' || !entry.product_name.startsWith('Composizione ')) continue;
  if (entry.art_code) { alreadyOk++; continue; }
  if (!entry.index_heading) { skippedNoHeading++; console.warn(`  SKIP (no index_heading): ${entry.product_name}`); continue; }
  entry.art_code = entry.index_heading;
  fixed++;
}

fs.writeFileSync(indexPath, JSON.stringify(catalog, null, 2));

console.log(`Composizione entries: ${fixed} fixed, ${alreadyOk} already had art_code, ${skippedNoHeading} skipped (no index_heading to derive from).`);
if (skippedNoHeading > 0) {
  console.log('Skipped entries need manual investigation before they can be code-matched.');
}
