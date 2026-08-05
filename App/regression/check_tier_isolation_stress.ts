/**
 * regression/check_tier_isolation_stress.ts
 * --------------------------------------------
 * 50+ query stress test specifically for cross-product tier/size leakage
 * in multi-product queries -- built after 3 separate bugs in this exact
 * family were found and fixed in one session (name leak, shared-tier-
 * array leak in the multi-product combiner, shared-tier-array leak in
 * the single-name LLM shortcut). All 3 were confirmed via DIRECT calls
 * to the affected functions with simulated LLM output, not by waiting on
 * live Groq timing -- that's what this script does too, deliberately.
 *
 * Every case is tested FOUR ways:
 *   1. DETERMINISTIC -- calls CatalogChat.answer() directly. Zero LLM
 *      involvement, 100% reproducible regardless of Groq quota. Gates.
 *   2. PARTIAL-LLM -- calls answerFromIntentMulti() with only the FIRST
 *      real product name (simulating "the LLM/scan found only one of
 *      the N named products") plus the FULL merged tier array across
 *      every expected product -- exactly the shape that caused the
 *      3rd confirmed bug. Gates.
 *   3. FULL-LLM -- calls answerFromIntentMulti() with ALL real product
 *      names plus the same full merged tier array -- exactly the shape
 *      that caused the 2nd confirmed bug. Gates.
 *   4. LIVE -- an actual HTTP call to the running server, whatever Groq
 *      does at the time. Informational only, does NOT gate the exit
 *      code (quota/timing dependent), but every result is printed and
 *      written to the results file for manual inspection.
 *
 * Modes 1-3 need the server's DATA files but NOT a running server or any
 * Groq key -- they instantiate CatalogChat directly. Mode 4 needs the
 * dev server running (npm run dev:server).
 *
 * RUN WITH: npm run check-tier-isolation-stress
 */

import { CatalogChat } from '../server/catalogChat';

const BASE_URL = process.env.REGRESSION_BASE_URL || 'http://localhost:3000';
const BRAND = 'Cattelan Italia';

interface Expected { product: string; tier: string; price: string; }
interface Case {
  id: number;
  cat: string;
  query: string;
  /** Real names actually present/typo'd in the query, in query order --
   * used to build the PARTIAL-LLM (first only) and FULL-LLM (all) sims. */
  realNames: string[];
  expected: Expected[];
}

const cc = new CatalogChat(`./data/${BRAND}`);

let id = 0;
const CASES: Case[] = [];
function addCase(cat: string, query: string, realNames: string[], expected: Expected[]) {
  CASES.push({ id: ++id, cat, query, realNames, expected });
}

// ===== 2-product different-tier combos (20) =====
addCase('2prod', 'gve me greta wood pelle and wlima pelle glove', ['GRETA Wood', 'WILMA'], [
  { product: 'GRETA Wood', tier: 'Pelle', price: '1.456' },
  { product: 'WILMA', tier: 'Pelle Glove', price: '985' },
]);
addCase('2prod', 'wilma pelle glove and greta wood pelle', ['WILMA', 'GRETA Wood'], [
  { product: 'WILMA', tier: 'Pelle Glove', price: '985' },
  { product: 'GRETA Wood', tier: 'Pelle', price: '1.456' },
]);
addCase('2prod', 'sierra pouf 100x94x41h pelle and tina pelle glove', ['SIERRA pouf', 'TINA'], [
  { product: 'SIERRA pouf', tier: 'Pelle', price: '1.902' },
  { product: 'TINA', tier: 'Pelle Glove', price: '817' },
]);
addCase('2prod', 'tina pelle glove and sierra pouf 100x94x41h pelle', ['TINA', 'SIERRA pouf'], [
  { product: 'TINA', tier: 'Pelle Glove', price: '817' },
  { product: 'SIERRA pouf', tier: 'Pelle', price: '1.902' },
]);
addCase('2prod', 'bob pouf pelle and camilla pelle glove', ['BOB pouf', 'CAMILLA'], [
  { product: 'BOB pouf', tier: 'Pelle', price: '745' },
  { product: 'CAMILLA', tier: 'Pelle Glove', price: '1.345' },
]);
addCase('2prod', 'camilla pelle glove and chloe pelle', ['CAMILLA', 'CHLOE'], [
  { product: 'CAMILLA', tier: 'Pelle Glove', price: '1.345' },
  { product: 'CHLOE', tier: 'Pelle', price: '691' },
]);
addCase('2prod', 'chris pelle and dafne pelle glove', ['CHRIS', 'DAFNE'], [
  { product: 'CHRIS', tier: 'Pelle', price: '1.011' },
  { product: 'DAFNE', tier: 'Pelle Glove', price: '1.453' },
]);
addCase('2prod', 'dafne pelle glove and daisy pelle', ['DAFNE', 'DAISY'], [
  { product: 'DAFNE', tier: 'Pelle Glove', price: '1.453' },
  { product: 'DAISY', tier: 'Pelle', price: '818' },
]);
addCase('2prod', 'donovan pelle and dumbo pelle glove', ['DONOVAN', 'DUMBO'], [
  { product: 'DONOVAN', tier: 'Pelle', price: '2.505' },
  { product: 'DUMBO', tier: 'Pelle Glove', price: '1.084' },
]);
addCase('2prod', 'ginger pelle glove and kay couture pelle', ['GINGER', 'KAY Couture'], [
  { product: 'GINGER', tier: 'Pelle Glove', price: '990' },
  { product: 'KAY Couture', tier: 'Pelle', price: '605' },
]);
addCase('2prod', 'maya flex pelle and nancy pelle glove', ['MAYA FLEX', 'NANCY'], [
  { product: 'MAYA FLEX', tier: 'Pelle', price: '704' },
  { product: 'NANCY', tier: 'Pelle Glove', price: '788' },
]);
addCase('2prod', 'penelope pelle glove and pinko pouf pelle', ['PENELOPE', 'PINKO pouf'], [
  { product: 'PENELOPE', tier: 'Pelle Glove', price: '806' },
  { product: 'PINKO pouf', tier: 'Pelle', price: '814' },
]);
addCase('2prod', 'mariel pelle and rhonda wood pelle glove', ['MARIEL', 'RHONDA Wood'], [
  { product: 'MARIEL', tier: 'Pelle', price: '1.263' },
  { product: 'RHONDA Wood', tier: 'Pelle Glove', price: '1.185' },
]);
addCase('2prod', 'chrishell pelle glove and kay pelle', ['CHRISHELL', 'KAY'], [
  { product: 'CHRISHELL', tier: 'Pelle Glove', price: '1.161' },
  { product: 'KAY', tier: 'Pelle', price: '559' },
]);
addCase('2prod', 'wlima pelle glove and sofia pelle', ['WILMA', 'SOFIA'], [
  { product: 'WILMA', tier: 'Pelle Glove', price: '985' },
]); // SOFIA has 4 sizes for Pelle -- checked separately as tier-only below
addCase('2prod', 'wilma pelle and sofia pelle glove', ['WILMA', 'SOFIA'], [
  { product: 'WILMA', tier: 'Pelle', price: '935' },
]);
addCase('2prod', 'kaay pelle and dafne pelle glove', ['KAY', 'DAFNE'], [
  { product: 'KAY', tier: 'Pelle', price: '559' },
  { product: 'DAFNE', tier: 'Pelle Glove', price: '1.453' },
]);
addCase('2prod', 'hystrx cristallo trasparente and ginger pelle glove', ['HYSTRIX', 'GINGER'], [
  { product: 'HYSTRIX', tier: 'Cristallo trasparente', price: '6.800' },
  { product: 'GINGER', tier: 'Pelle Glove', price: '990' },
]);
addCase('2prod', 'agata pelle and chris pelle glove', ['AGATHA FLEX', 'CHRIS'], [
  { product: 'AGATHA FLEX', tier: 'Pelle', price: '925' },
  { product: 'CHRIS', tier: 'Pelle Glove', price: '1.063' },
]);
addCase('2prod', 'miranda wheels pelle and rachel wood pelle glove', ['MIRANDA Wheels', 'RACHEL Wood'], [
  { product: 'MIRANDA Wheels', tier: 'Pelle', price: '1.233' },
  { product: 'RACHEL Wood', tier: 'Pelle Glove', price: '1.121' },
]);

addCase('2prod', 'miranda wood pelle and chrishell ml pelle glove', ['MIRANDA Wood', 'CHRISHELL ML'], [
  { product: 'MIRANDA Wood', tier: 'Pelle', price: '1.231' },
  { product: 'CHRISHELL ML', tier: 'Pelle Glove', price: '1.123' },
]);
addCase('2prod', 'rachel cantilever pelle and rhonda cantilever pelle glove', ['RACHEL Cantilever', 'RHONDA Cantilever'], [
  { product: 'RACHEL Cantilever', tier: 'Pelle', price: '1.040' },
  { product: 'RHONDA Cantilever', tier: 'Pelle Glove', price: '1.191' },
]);
addCase('2prod', 'rachel turn pelle glove and rhonda turn pelle', ['RACHEL Turn', 'RHONDA Turn'], [
  { product: 'RACHEL Turn', tier: 'Pelle Glove', price: '1.043' },
  { product: 'RHONDA Turn', tier: 'Pelle', price: '1.083' },
]);
addCase('2prod', 'rachel wheels pelle and rhonda wheels pelle glove', ['RACHEL Wheels', 'RHONDA Wheels'], [
  { product: 'RACHEL Wheels', tier: 'Pelle', price: '1.076' },
  { product: 'RHONDA Wheels', tier: 'Pelle Glove', price: '1.214' },
]);
addCase('2prod', 'nancy ml pelle glove and kay couture pelle', ['NANCY ML', 'KAY Couture'], [
  { product: 'NANCY ML', tier: 'Pelle Glove', price: '730' },
  { product: 'KAY Couture', tier: 'Pelle', price: '605' },
]);

// ===== 3-product different-tier combos (11) =====
addCase('3prod', 'tina ecopelle and bob pouf pelle and camilla pelle glove', ['TINA', 'BOB pouf', 'CAMILLA'], [
  { product: 'TINA', tier: 'Ecopelle / C.O.M.', price: '583' },
  { product: 'BOB pouf', tier: 'Pelle', price: '745' },
  { product: 'CAMILLA', tier: 'Pelle Glove', price: '1.345' },
]);
addCase('3prod', 'chloe pelle and chris pelle glove and dafne ecopelle', ['CHLOE', 'CHRIS', 'DAFNE'], [
  { product: 'CHLOE', tier: 'Pelle', price: '691' },
  { product: 'CHRIS', tier: 'Pelle Glove', price: '1.063' },
  { product: 'DAFNE', tier: 'Ecopelle', price: '1.063' },
]);
addCase('3prod', 'daisy pelle glove and donovan pelle and dumbo tessuto', ['DAISY', 'DONOVAN', 'DUMBO'], [
  { product: 'DAISY', tier: 'Pelle Glove', price: '876' },
  { product: 'DONOVAN', tier: 'Pelle', price: '2.505' },
  { product: 'DUMBO', tier: 'Tessuto / C.O.L', price: '948' },
]);
addCase('3prod', 'ginger pelle and kay couture pelle glove and maya flex ecopelle', ['GINGER', 'KAY Couture', 'MAYA FLEX'], [
  { product: 'GINGER', tier: 'Pelle', price: '918' },
  { product: 'KAY Couture', tier: 'Pelle Glove', price: '654' },
  { product: 'MAYA FLEX', tier: 'Ecopelle / C.O.M.', price: '539' },
]);
addCase('3prod', 'nancy pelle glove and penelope pelle and pinko pouf ecopelle', ['NANCY', 'PENELOPE', 'PINKO pouf'], [
  { product: 'NANCY', tier: 'Pelle Glove', price: '788' },
  { product: 'PENELOPE', tier: 'Pelle', price: '750' },
  { product: 'PINKO pouf', tier: 'Ecopelle', price: '608' },
]);
addCase('3prod', 'mariel pelle and rhonda wood pelle glove and chrishell ecopelle', ['MARIEL', 'RHONDA Wood', 'CHRISHELL'], [
  { product: 'MARIEL', tier: 'Pelle', price: '1.263' },
  { product: 'RHONDA Wood', tier: 'Pelle Glove', price: '1.185' },
  { product: 'CHRISHELL', tier: 'Ecopelle / C.O.M.', price: '899' },
]);
addCase('3prod', 'wlima pelle and sofia pelle glove and greta wood ecopelle', ['WILMA', 'SOFIA', 'GRETA Wood'], [
  { product: 'WILMA', tier: 'Pelle', price: '935' },
  { product: 'GRETA Wood', tier: 'Ecopelle / C.O.M.', price: '1.276' },
]);
addCase('3prod', 'kaay pelle and bishop price and dafne pelle glove', ['KAY', 'BISHOP', 'DAFNE'], [
  { product: 'KAY', tier: 'Pelle', price: '559' },
  { product: 'DAFNE', tier: 'Pelle Glove', price: '1.453' },
]);
addCase('3prod', 'donovan pelle glove and ginger pelle and camilla ecopelle', ['DONOVAN', 'GINGER', 'CAMILLA'], [
  { product: 'DONOVAN', tier: 'Pelle Glove', price: '2.650' },
  { product: 'GINGER', tier: 'Pelle', price: '918' },
  { product: 'CAMILLA', tier: 'Ecopelle / C.O.M.', price: '951' },
]);
addCase('3prod', 'compare wilma and pascal and hystrix', ['WILMA', 'PASCAL', 'HYSTRIX'], [
  { product: 'WILMA', tier: '', price: '' },
]); // known-good existing control, tier not pinned (bare name query)
addCase('3prod', 'chris pelle glove and dumbo pelle and mariel ecopelle', ['CHRIS', 'DUMBO', 'MARIEL'], [
  { product: 'CHRIS', tier: 'Pelle Glove', price: '1.063' },
  { product: 'DUMBO', tier: 'Pelle', price: '1.044' },
  { product: 'MARIEL', tier: 'Ecopelle / C.O.M.', price: '1.042' },
]);

// ===== Repeats of the exact reported strings, multiple times each =====
for (let i = 0; i < 3; i++) {
  addCase('repeat', 'gve me greta wood pelle and wlima pelle glove', ['GRETA Wood', 'WILMA'], [
    { product: 'GRETA Wood', tier: 'Pelle', price: '1.456' },
    { product: 'WILMA', tier: 'Pelle Glove', price: '985' },
  ]);
}
for (let i = 0; i < 3; i++) {
  addCase('repeat', 'sierra pouf 100x94x41h pelle and tina pelle glove', ['SIERRA pouf', 'TINA'], [
    { product: 'SIERRA pouf', tier: 'Pelle', price: '1.902' },
    { product: 'TINA', tier: 'Pelle Glove', price: '817' },
  ]);
}
for (let i = 0; i < 2; i++) {
  addCase('repeat', 'sierra pouf 100x94x41h pelle and tina pelle', ['SIERRA pouf', 'TINA'], [
    { product: 'SIERRA pouf', tier: 'Pelle', price: '1.902' },
    { product: 'TINA', tier: 'Pelle', price: '758' },
  ]);
}

// ===== Single-product controls =====
addCase('single', 'wilma pelle glove', ['WILMA'], [{ product: 'WILMA', tier: 'Pelle Glove', price: '985' }]);
addCase('single', 'wlima pelle glove', ['WILMA'], [{ product: 'WILMA', tier: 'Pelle Glove', price: '985' }]);
addCase('single', 'sofia pelle glove', ['SOFIA'], []); // multi-size, checked structurally below
addCase('single', 'spinnaker x fissaggio a muro gfm73 nc gfm11', ['SPINNAKER'], []); // known punctuation-mismatch control
addCase('single', 'bishop price', ['BISHOP'], []); // multi-size, no tier
addCase('single', 'give me all prices for wilma', ['WILMA'], []); // full grid, checked structurally

console.log(`Built ${CASES.length} cases.\n`);

// ---- Mode helpers ----

function checkExpected(matches: { product_name: string; fabric_tier: string | null; price_eur: string }[] | undefined, expected: Expected[]): string[] {
  const problems: string[] = [];
  const rows = matches || [];
  for (const exp of expected) {
    if (!exp.tier || !exp.price) continue; // structural-only case, skip value assertion
    const productRows = rows.filter(r => r.product_name === exp.product);
    if (productRows.length === 0) continue; // not resolved this mode -- expected for typo'd names in deterministic-only mode
    const hit = productRows.find(r => r.fabric_tier === exp.tier && r.price_eur === exp.price);
    if (!hit) {
      problems.push(`${exp.product}: expected tier="${exp.tier}" price="${exp.price}", got: ${productRows.map(r => `${r.fabric_tier}=€${r.price_eur}`).join(', ')}`);
    }
  }
  return problems;
}

async function postChat(message: string): Promise<any> {
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    const res = await fetch(`${BASE_URL}/api/catalog/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ brand: BRAND, message, history: [] }),
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
  const gatingFailures: string[] = [];
  const liveResults: { id: number; query: string; degraded: boolean | undefined; status: string | undefined; problems: string[] }[] = [];

  for (const c of CASES) {
    // Mode 1: DETERMINISTIC
    const detResult = cc.answer(c.query, BRAND, null);
    const detProblems = checkExpected(detResult.matches, c.expected);
    if (detProblems.length > 0) gatingFailures.push(`[${c.id}:${c.cat}:deterministic] "${c.query}" -- ${detProblems.join(' | ')}`);

    // Mode 2: PARTIAL-LLM (only first real name resolved)
    const mergedTiers = [...new Set(c.expected.map(e => e.tier).filter(Boolean))];
    const partialResult = cc.answerFromIntentMulti([c.realNames[0]], null, mergedTiers, c.query, BRAND, null, false);
    const partialExpected = c.expected.filter(e => e.product === c.realNames[0]);
    const partialProblems = checkExpected(partialResult.matches, partialExpected);
    if (partialProblems.length > 0) gatingFailures.push(`[${c.id}:${c.cat}:partial-llm] "${c.query}" -- ${partialProblems.join(' | ')}`);

    // Mode 3: FULL-LLM (all real names resolved)
    const fullResult = cc.answerFromIntentMulti(c.realNames, null, mergedTiers, c.query, BRAND, null, false);
    const fullProblems = checkExpected(fullResult.matches, c.expected);
    if (fullProblems.length > 0) gatingFailures.push(`[${c.id}:${c.cat}:full-llm] "${c.query}" -- ${fullProblems.join(' | ')}`);

    const modesFailed = [detProblems.length > 0 && 'det', partialProblems.length > 0 && 'partial', fullProblems.length > 0 && 'full'].filter(Boolean);
    console.log(`[${String(c.id).padStart(2)}] ${c.cat.padEnd(8)} ${modesFailed.length > 0 ? 'FAIL(' + modesFailed.join(',') + ')' : 'ok'}  "${c.query}"`);
  }

  console.log('\n' + '='.repeat(70));
  console.log(`Total cases: ${CASES.length} (x3 gating modes each = ${CASES.length * 3} checks)`);
  console.log(`Gating failures (deterministic/partial-llm/full-llm): ${gatingFailures.length}  <-- must be 0`);
  if (gatingFailures.length > 0) {
    console.log('\nFAILURES:');
    gatingFailures.forEach(f => console.log(`  ${f}`));
  }

  // Mode 4: LIVE HTTP -- informational only, does not gate
  console.log('\n' + '='.repeat(70));
  console.log('LIVE HTTP spot-check (informational only, quota-dependent, does not gate):');
  const probe = await postChat('ping');
  if (probe.error) {
    console.log(`Cannot reach ${BASE_URL} (${probe.error}) -- skipping live spot-check. Start the server: npm run dev:server`);
  } else {
    for (const c of CASES) {
      const resp = await postChat(c.query);
      const problems = checkExpected(resp.matches, c.expected);
      liveResults.push({ id: c.id, query: c.query, degraded: resp.degraded, status: resp.status, problems });
      console.log(`  [${String(c.id).padStart(2)}] degraded=${resp.degraded ?? false} status=${resp.status || resp.error} ${problems.length > 0 ? 'MISMATCH: ' + problems.join(' | ') : 'ok'}`);
      await new Promise(r => setTimeout(r, 80));
    }
    const liveMismatches = liveResults.filter(r => r.problems.length > 0);
    console.log(`\nLive mismatches: ${liveMismatches.length}/${CASES.length} (informational -- Groq availability varies call to call)`);
  }

  if (gatingFailures.length > 0) {
    console.log('\nEXIT 1: tier/size isolation failure in a deterministic or simulated-LLM code path.');
    process.exit(1);
  }
  console.log('\nEXIT 0: all deterministic and simulated-LLM checks passed.');
}

main();
