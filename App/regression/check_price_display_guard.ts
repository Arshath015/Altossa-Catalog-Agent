/**
 * regression/check_price_display_guard.ts
 * -----------------------------------------
 * Permanent, GATING check for formatPriceDisplay() -- the "€" prefix
 * guard added 2026-10-03 (catalogChat.ts single-row message at ~line
 * 4162, CatalogChatWidget.tsx table cells at ~line 590 and ~line 759).
 *
 * WHY: Tacchini's C.O. Leather tier stores price_eur as the literal
 * string "Price upon request" (a real, human-readable non-priced STATE,
 * not a number) -- rendering it unprefixed would otherwise show
 * nonsensical "€Price upon request". But price_eur is a free-text string
 * everywhere in this codebase, and Bolzan ALREADY has 52 pre-existing
 * non-numeric price_eur values ("MRD40"/"MRD50", from an unrelated,
 * separately-tracked extraction defect -- see flag_triage.json's
 * "_CATALOG_WIDE: Maggiorazioni addon-surcharge capture gap" entry) that
 * must keep showing their current "€MRD40"-style display UNCHANGED, not
 * be silently reclassified as a legitimate "state" just because they're
 * also non-numeric.
 *
 * The guard's rule: skip the "€" prefix only when the value has NO
 * digits at all. "MRD40"/"MRD50" contain digits -> unaffected. "Price
 * upon request" has none -> prefix skipped. Verified 2026-10-03 against
 * every row in all 6 live brands' prices.json: zero rows of any kind
 * have a digit-free price_eur today, so this check locks in that this
 * stays true (a future brand/parser change introducing a digit-free
 * numeric-ish price_eur, e.g. "N/A", would also lose its € here --
 * intentional, same reasoning as "Price upon request").
 *
 * Pure unit test on the exported function -- no HTTP, no dev server
 * required, deterministic and fast like check_coverage.ts's design goal.
 *
 * RUN WITH: npm run check-price-display-guard
 */

import { formatPriceDisplay } from '../server/catalogChat';

interface Case {
  id: string;
  input: string;
  expected: string;
}

const CASES: Case[] = [
  // Real numeric formats seen across the 6 live brands today -- must keep
  // getting the € prefix exactly as before this guard existed.
  { id: 'cattelan-plain', input: '4.397', expected: '€4.397' },
  { id: 'bolzan-plain', input: '342', expected: '€342' },
  { id: 'varaschini-plain', input: '2.662', expected: '€2.662' },
  { id: 'addon-offset-plus', input: '+200', expected: '€+200' },
  { id: 'addon-offset-plus-large', input: '+1.699', expected: '€+1.699' },
  { id: 'decimal-ish', input: '3,07', expected: '€3,07' },

  // Bolzan's real, pre-existing, UNRELATED anomaly values (code/price
  // swap bug, logged separately, not fixed by this guard) -- contain
  // digits, so must stay €-prefixed exactly as they render live today.
  // Regression-critical: this is the one case that could be silently
  // broken by a well-intentioned "make non-numeric values look nicer"
  // change.
  { id: 'bolzan-anomaly-mrd40', input: 'MRD40', expected: '€MRD40' },
  { id: 'bolzan-anomaly-mrd50', input: 'MRD50', expected: '€MRD50' },
  { id: 'bolzan-anomaly-mrd90', input: 'MRD90', expected: '€MRD90' },

  // Tacchini's real non-priced STATE (C.O. Leather tier) -- the actual
  // case this guard was built for. No digits -> no € prefix, shown
  // verbatim so the message reads naturally ("...: Price upon request").
  { id: 'tacchini-price-upon-request', input: 'Price upon request', expected: 'Price upon request' },

  // Defensive: a digit-free value with mixed case/punctuation should
  // still pass through verbatim, not partially prefixed or mangled.
  { id: 'generic-no-digit-state', input: 'N/A', expected: 'N/A' },
];

function main() {
  let failures = 0;
  for (const c of CASES) {
    const actual = formatPriceDisplay(c.input);
    const ok = actual === c.expected;
    if (!ok) failures++;
    console.log(`[${c.id.padEnd(32)}] ${ok ? 'ok' : `FAIL (expected ${JSON.stringify(c.expected)}, got ${JSON.stringify(actual)})`}`);
  }

  console.log('\n' + '='.repeat(70));
  console.log(`Total cases: ${CASES.length}`);
  console.log(`Failures: ${failures}  <-- must be 0`);

  if (failures > 0) {
    console.log(`\nEXIT 1: price display guard regressed.`);
    process.exit(1);
  }
  console.log(`\nEXIT 0: € prefix correctly applied to every numeric/addon/anomaly value, skipped only for genuine digit-free non-priced states.`);
}

main();
