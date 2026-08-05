/**
 * regression/stress_v2.ts
 * -------------------------
 * One-off (not wired into `regression:full`) 55-query live-endpoint stress
 * round, built specifically to check BOTH price AND page image for every
 * query -- the prior 56-59 query rounds only ever cross-checked price, and
 * a real image-routing bug (RICHARD showing BOTERO/BOULEVARD's page) slipped
 * past every one of them until manual testing found it.
 *
 * For every query:
 *   1. FABRICATION check (same as run.ts): every returned price row must
 *      exist verbatim in that brand's real prices.json.
 *   2. IMAGE check (new): independently recompute, from catalog_index.json,
 *      which image(s) SHOULD be returned for the specific product(s)/page(s)
 *      in this response's `matches`, and diff against the actual
 *      `image_urls` the API returned. Flags two distinct failure modes:
 *      - "unexpected image": an image_url not explainable by any matched
 *        row's product+page -- a routing/leak bug (could show a WRONG
 *        product's picture, the RICHARD-class bug).
 *      - "missing image": a page implied by the matched rows that never
 *        made it into image_urls -- an under-scoping bug.
 *
 * Requires the dev server running. Writes App/regression/stress_v2-run.json.
 */

import fs from 'fs';
import path from 'path';

const ROOT = path.join(__dirname, '..', '..');
const BASE_URL = process.env.REGRESSION_BASE_URL || 'http://localhost:3000';

interface Query {
  id: number;
  cat: string;
  brand: string;
  query: string;
  note?: string;
}

interface PriceRow {
  brand: string;
  product_name: string;
  model_variant: string | null;
  size: string | null;
  fabric_tier: string | null;
  price_eur: string;
  source_pdf_page?: number | null;
  code?: string | null;
  ambiguous?: boolean;
}

interface ChatResponse {
  status?: string;
  message?: string;
  product_name?: string;
  candidates?: string[];
  matches?: PriceRow[];
  image_urls?: string[];
  degraded?: boolean;
  error?: string;
}

interface CatalogEntry {
  product_name: string;
  images: string[];
  page_images?: Record<string, string>;
}

const KEY_SEP = '~|~';
function rowKey(r: Pick<PriceRow, 'product_name' | 'model_variant' | 'size' | 'fabric_tier' | 'price_eur'>): string {
  return [r.product_name, r.model_variant, r.size, r.fabric_tier, r.price_eur].join(KEY_SEP);
}

function loadBrandRowKeys(brand: string): Set<string> {
  const pricesPath = path.join(ROOT, 'data', brand, 'prices.json');
  const rows: PriceRow[] = JSON.parse(fs.readFileSync(pricesPath, 'utf-8'));
  return new Set(rows.map(rowKey));
}

function loadCatalogIndex(brand: string): Map<string, CatalogEntry> {
  const idxPath = path.join(ROOT, 'data', brand, 'catalog_index.json');
  const raw = JSON.parse(fs.readFileSync(idxPath, 'utf-8'));
  const entries: CatalogEntry[] = Array.isArray(raw) ? raw : Object.values(raw);
  const map = new Map<string, CatalogEntry>();
  for (const e of entries) map.set(e.product_name, e);
  return map;
}

/** Recompute, independently of the server, exactly which images the
 * response SHOULD contain given its own `matches` -- mirrors
 * CatalogChat.getImageUrls()'s own logic (page-scoped when possible, whole
 * product image set as fallback) but as a from-scratch cross-check against
 * catalog_index.json, not a call into the server's own code. */
function expectedImageUrls(brand: string, matches: PriceRow[], catalogIdx: Map<string, CatalogEntry>): Set<string> {
  const byProduct = new Map<string, PriceRow[]>();
  for (const r of matches) {
    if (!byProduct.has(r.product_name)) byProduct.set(r.product_name, []);
    byProduct.get(r.product_name)!.push(r);
  }
  const expected = new Set<string>();
  for (const [productName, rows] of byProduct) {
    const entry = catalogIdx.get(productName);
    if (!entry) continue;
    const pages = [...new Set(rows.map(r => r.source_pdf_page).filter((p): p is number => p != null))];
    let files: string[] = [];
    if (pages.length > 0 && entry.page_images) {
      files = pages.map(p => entry.page_images![String(p)]).filter((f): f is string => !!f);
    }
    if (files.length === 0) files = entry.images;
    for (const f of files) expected.add(`/data/${brand}/images/${f}`);
  }
  return expected;
}

async function postChat(brand: string, message: string): Promise<ChatResponse> {
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    const res = await fetch(`${BASE_URL}/api/catalog/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ brand, message }),
      signal: controller.signal,
    });
    clearTimeout(timeout);
    if (!res.ok) return { error: `HTTP ${res.status}` };
    return await res.json();
  } catch (err) {
    return { error: err instanceof Error ? err.message : String(err) };
  }
}

async function main() {
  const queriesFile = process.env.STRESS_QUERIES_FILE || path.join(__dirname, 'stress_v2_queries.json');
  const queries: Query[] = JSON.parse(fs.readFileSync(queriesFile, 'utf-8'));

  const brandRowKeys = new Map<string, Set<string>>();
  const brandCatalogIdx = new Map<string, Map<string, CatalogEntry>>();
  for (const q of queries) {
    if (!brandRowKeys.has(q.brand)) brandRowKeys.set(q.brand, loadBrandRowKeys(q.brand));
    if (!brandCatalogIdx.has(q.brand)) brandCatalogIdx.set(q.brand, loadCatalogIndex(q.brand));
  }

  const probe = await postChat(queries[0].brand, 'ping');
  if (probe.error) {
    console.error(`\nCannot reach ${BASE_URL}/api/catalog/chat (${probe.error}).`);
    console.error('Start the server first: npm run dev:server\n');
    process.exit(1);
  }

  const results: Array<{
    query: Query;
    response: ChatResponse;
    fabricated: string[];
    unexpectedImages: string[];
    missingImages: string[];
    imageFilesOnDisk: { url: string; exists: boolean }[];
  }> = [];

  for (const q of queries) {
    const resp = await postChat(q.brand, q.query);
    const realKeys = brandRowKeys.get(q.brand)!;
    const catalogIdx = brandCatalogIdx.get(q.brand)!;
    const matches = resp.matches || [];
    const fabricated = matches.filter(m => !realKeys.has(rowKey(m))).map(m => rowKey(m));

    const expected = expectedImageUrls(q.brand, matches, catalogIdx);
    const actual = new Set(resp.image_urls || []);
    const unexpectedImages = [...actual].filter(u => !expected.has(u));
    const missingImages = [...expected].filter(u => !actual.has(u));

    const imageFilesOnDisk = (resp.image_urls || []).map(u => {
      const rel = decodeURIComponent(u.replace(/^\/data\//, ''));
      const full = path.join(ROOT, 'data', rel);
      return { url: u, exists: fs.existsSync(full) };
    });

    results.push({ query: q, response: resp, fabricated, unexpectedImages, missingImages, imageFilesOnDisk });

    const missingOnDisk = imageFilesOnDisk.filter(f => !f.exists);
    const flags: string[] = [];
    if (fabricated.length > 0) flags.push('FABRICATION');
    if (unexpectedImages.length > 0) flags.push('UNEXPECTED-IMAGE');
    if (missingImages.length > 0) flags.push('MISSING-IMAGE');
    if (missingOnDisk.length > 0) flags.push('IMAGE-404');
    const flag = flags.length > 0 ? flags.join(',') : 'ok';
    console.log(`[${String(q.id).padStart(2)}] ${q.cat.padEnd(18)} status=${(resp.status || resp.error || '?').padEnd(20)} ${flag}`);
    await new Promise(r => setTimeout(r, 100));
  }

  const totalFabricated = results.filter(r => r.fabricated.length > 0);
  const totalUnexpectedImg = results.filter(r => r.unexpectedImages.length > 0);
  const totalMissingImg = results.filter(r => r.missingImages.length > 0);
  const total404 = results.filter(r => r.imageFilesOnDisk.some(f => !f.exists));

  console.log('\n' + '='.repeat(70));
  console.log(`Total queries run: ${results.length}`);
  console.log(`Fabrication: ${totalFabricated.length}  <-- must be 0`);
  console.log(`Unexpected/wrong images: ${totalUnexpectedImg.length}  <-- must be 0`);
  console.log(`Missing expected images: ${totalMissingImg.length}  <-- must be 0`);
  console.log(`Image files 404 on disk: ${total404.length}  <-- must be 0`);

  if (totalFabricated.length > 0) {
    console.log('\nFABRICATED ROWS:');
    for (const r of totalFabricated) {
      console.log(`  [${r.query.id}] ${r.query.query}`);
      r.fabricated.forEach(k => console.log(`      ${k.split(KEY_SEP).join(' | ')}`));
    }
  }
  if (totalUnexpectedImg.length > 0) {
    console.log('\nUNEXPECTED IMAGES (possible wrong-product leak):');
    for (const r of totalUnexpectedImg) {
      console.log(`  [${r.query.id}] ${r.query.query}`);
      r.unexpectedImages.forEach(u => console.log(`      ${u}`));
    }
  }
  if (totalMissingImg.length > 0) {
    console.log('\nMISSING IMAGES (under-scoped):');
    for (const r of totalMissingImg) {
      console.log(`  [${r.query.id}] ${r.query.query}`);
      r.missingImages.forEach(u => console.log(`      ${u}`));
    }
  }
  if (total404.length > 0) {
    console.log('\nIMAGE 404s:');
    for (const r of total404) {
      console.log(`  [${r.query.id}] ${r.query.query}`);
      r.imageFilesOnDisk.filter(f => !f.exists).forEach(f => console.log(`      ${f.url}`));
    }
  }

  fs.writeFileSync(path.join(__dirname, 'stress_v2-run.json'), JSON.stringify(results, null, 2), 'utf-8');
  console.log(`\nFull results written to App/regression/stress_v2-run.json`);

  const gating = totalFabricated.length + totalUnexpectedImg.length + total404.length;
  if (gating > 0) {
    console.log('\nEXIT 1: fabrication or image-routing bug detected.');
    process.exit(1);
  }
  console.log('\nEXIT 0: no fabrication, no image-routing bugs.');
}

main();
