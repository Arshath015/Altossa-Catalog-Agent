/**
 * regression/check_orphaned_flags.ts
 * ------------------------------------
 * Permanent, GATING check that closes the exact hole found 2026-08-06:
 * App/parse_prices.py's Cattelan-format parser correctly DECLINES to guess
 * at a genuinely tangled column header and logs a review flag instead --
 * but there was no guaranteed step ensuring every flag actually got
 * triaged. SAN MARCO, BLUEBELL, NAHUN, REGATA and LAVANDER were flagged,
 * never followed up, and the app kept serving accessory-only data for
 * them with no error of any kind.
 *
 * This re-runs the parser fresh (to a throwaway output, never touching the
 * real prices.json) to get the CURRENT set of review flags, then checks
 * every flagged product name against flag_triage.json, the permanent,
 * git-tracked triage ledger (NOT under data/, which is gitignored -- this
 * file must survive a fresh clone).
 *
 * Fails the build if:
 *  - ORPHANED: a flagged product has no entry in flag_triage.json at all
 *    (never triaged -- the exact original bug).
 *  - CLAIMED-BUT-MISSING: the ledger claims a product was fixed
 *    ("real_gap" or "partial") but manual_additions.json has no rows for
 *    it -- i.e. someone recorded the triage decision but never actually
 *    wrote the transcription (a "said we fixed it" gap, one level up from
 *    "never looked at it").
 *
 * RUN WITH: npm run check-orphaned-flags
 * Does NOT require the dev server -- runs parse_prices.py directly.
 */

import fs from 'fs';
import path from 'path';
import os from 'os';
import { execFileSync } from 'child_process';

const ROOT = path.join(__dirname, '..', '..');
const LEDGER_PATH = path.join(__dirname, 'flag_triage.json');

// Brands whose catalog is parsed with a --format that produces review_flags
// (currently: cattelan, bonaldo, varaschini). Add a brand here the day it
// starts using one of these formats so its flags are tracked from day one
// -- also add a matching entry (even if just `{}`) to flag_triage.json.
const FLAG_PRODUCING_BRANDS: Record<string, string> = {
  'Cattelan Italia': 'cattelan',
  'Bonaldo': 'bonaldo',
  'Varaschini': 'varaschini',
  'Ditre Italia': 'ditre',
  'Pianca': 'pianca',
};

interface Flag {
  page: number | null;
  product_name: string;
  brand: string;
  reason: string;
}

interface LedgerEntry {
  status: 'real_gap' | 'partial' | 'false_alarm' | 'known_gap';
  note: string;
}

function runParserForFlags(brand: string, format: string): Flag[] {
  const catalogIndexPath = path.join(ROOT, 'data', brand, 'catalog_index.json');
  if (!fs.existsSync(catalogIndexPath)) {
    console.log(`  (skipping ${brand}: no catalog_index.json found)`);
    return [];
  }
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'orphaned-flags-'));
  const tmpPrices = path.join(tmpDir, 'prices_throwaway.json');
  const tmpFlags = path.join(tmpDir, 'flags.json');
  try {
    execFileSync('python', [
      path.join(ROOT, 'App', 'parse_prices.py'),
      catalogIndexPath,
      '--format', format,
      '--out', tmpPrices,
      '--flags-out', tmpFlags,
    ], { stdio: 'pipe' });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    throw new Error(`Failed to re-run parse_prices.py for ${brand}: ${msg}`);
  }
  const flags: Flag[] = JSON.parse(fs.readFileSync(tmpFlags, 'utf-8'));
  fs.rmSync(tmpDir, { recursive: true, force: true });
  return flags;
}

function main() {
  if (!fs.existsSync(LEDGER_PATH)) {
    console.error(`\nMissing triage ledger: ${LEDGER_PATH}`);
    process.exit(1);
  }
  const ledger = JSON.parse(fs.readFileSync(LEDGER_PATH, 'utf-8'));

  const orphaned: string[] = [];
  const claimedButMissing: string[] = [];
  let totalFlags = 0;
  let totalFlaggedProducts = 0;

  for (const [brand, format] of Object.entries(FLAG_PRODUCING_BRANDS)) {
    console.log(`\n=== ${brand}: re-running parser for current review flags ===`);
    const flags = runParserForFlags(brand, format);
    totalFlags += flags.length;

    const manualPath = path.join(ROOT, 'data', brand, 'manual_additions.json');
    const manualNames: Set<string> = fs.existsSync(manualPath)
      ? new Set(JSON.parse(fs.readFileSync(manualPath, 'utf-8')).map((r: { product_name: string }) => r.product_name))
      : new Set();

    const brandLedger: Record<string, LedgerEntry> = ledger[brand] || {};
    const byProduct = new Map<string, Flag[]>();
    for (const f of flags) {
      if (!byProduct.has(f.product_name)) byProduct.set(f.product_name, []);
      byProduct.get(f.product_name)!.push(f);
    }
    totalFlaggedProducts += byProduct.size;

    for (const [productName, productFlags] of byProduct) {
      const entry = brandLedger[productName];
      if (!entry) {
        orphaned.push(`[${brand}] ${productName}: ${productFlags.length} flag(s), no flag_triage.json entry -- ${productFlags[0].reason}`);
        continue;
      }
      if ((entry.status === 'real_gap' || entry.status === 'partial') && !manualNames.has(productName)) {
        claimedButMissing.push(`[${brand}] ${productName}: ledger says "${entry.status}" but manual_additions.json has no rows for it -- the fix was recorded but never actually written`);
      }
    }

    console.log(`  ${flags.length} flag(s) across ${byProduct.size} product(s).`);
  }

  console.log('\n' + '='.repeat(70));
  console.log(`Total review flags: ${totalFlags}`);
  console.log(`Total distinct flagged products: ${totalFlaggedProducts}`);
  console.log(`Orphaned (untriaged) flags: ${orphaned.length}  <-- must be 0`);
  console.log(`Claimed-fixed-but-missing: ${claimedButMissing.length}  <-- must be 0`);

  if (orphaned.length > 0) {
    console.log('\nORPHANED FLAGS (no flag_triage.json entry -- add one to App/regression/flag_triage.json):');
    orphaned.forEach(o => console.log(`  ${o}`));
  }
  if (claimedButMissing.length > 0) {
    console.log('\nCLAIMED-FIXED-BUT-MISSING (ledger status requires a manual_additions.json entry that does not exist):');
    claimedButMissing.forEach(c => console.log(`  ${c}`));
  }

  if (orphaned.length > 0 || claimedButMissing.length > 0) {
    console.log('\nEXIT 1: one or more parser review flags were never properly triaged.');
    process.exit(1);
  }
  console.log('\nEXIT 0: every current review flag has a triage ledger entry, and every claimed fix is actually present in manual_additions.json.');
}

main();
