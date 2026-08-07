/**
 * regression/check_images.ts
 * ----------------------------
 * Permanent regression check: verifies every product's stored page
 * image(s) actually correspond to that product, not a different one.
 *
 * RUN WITH:  npm run check-images
 *
 * WHY THIS EXISTS: a real bug shipped and passed 56+ prior "passing"
 * regression runs because nothing checked images -- only prices. RICHARD's
 * stored image was byte-for-byte identical to two unrelated products'
 * images (BOTERO Wood Round / BOULEVARD Keramik), because both source
 * PDFs (main catalog + Novità supplement) restart page numbering at 1,
 * and RICHARD's one-off manual page-range recovery rendered its images
 * from the wrong PDF file. The text/price data was correct (hand-
 * transcribed independently), so every price-based check passed while the
 * image was completely wrong.
 *
 * METHOD: this doesn't need OCR or a vision model. It's a purely
 * mechanical, byte-level check: compute the checksum of every stored
 * image file. Two DIFFERENT products sharing an identical image is only
 * legitimate when they genuinely share one physical source page --
 * verified by checking that each involved product's own extracted text
 * literally contains the OTHER product's real printed heading
 * (index_heading if the product was renamed during collision
 * disambiguation, since the display name itself may never appear on the
 * page verbatim). If two products share an image checksum WITHOUT that
 * cross-reference, it's flagged as a real mismatch.
 *
 * This also implicitly re-verifies every product's mini_pdf/page_images
 * bookkeeping is internally consistent, since the check operates on
 * exactly the files a real chat response would link to.
 */

import fs from 'fs';
import path from 'path';
import crypto from 'crypto';

const ROOT = path.join(__dirname, '..', '..');
const BRANDS = ['Cattelan Italia', 'Bolzan', 'Bonaldo'];

interface CatalogEntry {
  product_name: string;
  index_heading?: string;
  images: string[];
  text_file: string;
}

function realHeading(entry: CatalogEntry): string {
  return entry.index_heading || entry.product_name;
}

function checkBrand(brand: string): { suspicious: number; legitimate: number } {
  const dataDir = path.join(ROOT, 'data', brand);
  const idxPath = path.join(dataDir, 'catalog_index.json');
  if (!fs.existsSync(idxPath)) {
    console.log(`\n=== ${brand}: no catalog_index.json found, skipping ===`);
    return { suspicious: 0, legitimate: 0 };
  }
  const idx: CatalogEntry[] = JSON.parse(fs.readFileSync(idxPath, 'utf-8'));
  const imagesDir = path.join(dataDir, 'images');

  const checksumToFiles = new Map<string, string[]>();
  for (const fname of fs.readdirSync(imagesDir)) {
    const filePath = path.join(imagesDir, fname);
    const hash = crypto.createHash('md5').update(fs.readFileSync(filePath)).digest('hex');
    if (!checksumToFiles.has(hash)) checksumToFiles.set(hash, []);
    checksumToFiles.get(hash)!.push(fname);
  }

  const fileToProducts = new Map<string, string[]>();
  for (const entry of idx) {
    for (const img of entry.images || []) {
      if (!fileToProducts.has(img)) fileToProducts.set(img, []);
      fileToProducts.get(img)!.push(entry.product_name);
    }
  }

  const textCache = new Map<string, string>();
  function textFor(productName: string): string {
    if (textCache.has(productName)) return textCache.get(productName)!;
    const entry = idx.find(e => e.product_name === productName);
    let text = '';
    if (entry) {
      try {
        text = fs.readFileSync(path.join(ROOT, 'data', entry.text_file), 'utf-8');
      } catch {
        text = '';
      }
    }
    textCache.set(productName, text);
    return text;
  }

  let suspiciousCount = 0;
  let legitimateCount = 0;
  const suspiciousDetails: string[] = [];

  for (const [, files] of checksumToFiles) {
    const products = [...new Set(files.flatMap(f => fileToProducts.get(f) || []))];
    if (products.length < 2) continue; // same product referencing its own file(s), not a collision

    let isLegit = true;
    for (const p of products) {
      const entryP = idx.find(e => e.product_name === p)!;
      const pText = textFor(p).toUpperCase();
      for (const other of products) {
        if (other === p) continue;
        const otherEntry = idx.find(e => e.product_name === other)!;
        if (!pText.includes(realHeading(otherEntry).toUpperCase())) isLegit = false;
      }
    }

    if (isLegit) {
      legitimateCount++;
    } else {
      suspiciousCount++;
      suspiciousDetails.push(`  ${products.join(' / ')}  [${files.join(', ')}]`);
    }
  }

  console.log(`\n=== ${brand}: ${idx.length} products ===`);
  console.log(`Legitimate shared-page image groups: ${legitimateCount}`);
  console.log(`SUSPICIOUS image mismatches: ${suspiciousCount}`);
  if (suspiciousDetails.length > 0) {
    console.log('Details:');
    suspiciousDetails.forEach(d => console.log(d));
  }

  return { suspicious: suspiciousCount, legitimate: legitimateCount };
}

let totalSuspicious = 0;
for (const brand of BRANDS) {
  const result = checkBrand(brand);
  totalSuspicious += result.suspicious;
}

console.log('\n' + '='.repeat(70));
console.log(`Total suspicious image mismatches across all brands: ${totalSuspicious}  <-- must be 0`);
if (totalSuspicious > 0) {
  console.log('\nEXIT 1: image mismatch(es) detected -- fix before anything else.');
  process.exit(1);
}
console.log('\nEXIT 0: every shared image checksum is a verified legitimate same-page case.');
