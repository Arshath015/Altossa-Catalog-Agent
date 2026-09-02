/**
 * regression/check_anchor_tie_break.ts
 * --------------------------------------
 * Permanent, GATING check for a real bug found via live testing 2026-09-01
 * (user-reported: asking "give all norma price" right after specifying
 * "Norma CollezioneNotte" reset to a generic 3-way clarify instead of
 * staying anchored).
 *
 * Root cause: `lastProduct` is threaded through `answer()`/`answerFromIntent`
 * for several purposes (content-free-followup vocabulary matching, variant
 * continuity via lastModelVariant), but NEITHER of the two places a query
 * can land in a genuine multi-PRODUCT tie ever consulted it to break that
 * tie in the anchor's favor:
 *
 *  1. `answer()`'s own `topMatches.length > 1` branch -- reached when the
 *     query itself scores 2+ real products equally (e.g. "give all norma
 *     price" ties "Norma Up", "Norma (CollezioneNotte)", and "Norma
 *     (CollezioneGiorno)", since "norma" is the only word any of them
 *     share with the query).
 *  2. `answerFromIntent`'s own `checkFamilyAmbiguity` backstop -- reached
 *     via the LLM path specifically, when the LLM confidently guesses ONE
 *     valid but WRONG sibling name (e.g. "Norma Up" instead of the
 *     already-established "Norma (CollezioneNotte)") -- the existing
 *     `isAnchoredGuess`/`isTrustedAnchorContinuation` check only helps
 *     when the LLM's own guess already EQUALS lastProduct, not when it
 *     guesses a different family member entirely, so checkFamilyAmbiguity
 *     still re-flags the whole family and discards the anchor.
 *
 * Both were real, independent gaps -- confirmed by direct reproduction of
 * BOTH code paths (a live curl hitting the LLM-driven route, and a direct
 * `answerFromIntentMulti(['Norma Up'], ...)` call bypassing the LLM to
 * isolate path #2) before concluding a single fix wasn't enough.
 *
 * Fixed by adding the SAME anchor-preference check to both places: when
 * the tied/flagged candidate set includes `lastProduct`, resolve directly
 * to it instead of asking to clarify. Deliberately does NOT fire when the
 * query itself already narrows to a single dominant candidate (a real,
 * different product literally named in the query correctly overrides a
 * stale anchor -- guarded by case 2 below), and does NOT fire when
 * `lastProduct` isn't even among the tied/flagged candidates (a genuinely
 * unrelated anchor from an earlier, different product must not force-
 * resolve an unrelated ambiguity -- guarded by case 3 below).
 *
 * RUN WITH: npm run check-anchor-tie-break
 * Requires the dev server running (npm run dev:server).
 */

const BASE_URL = process.env.REGRESSION_BASE_URL || 'http://localhost:3000';

interface ChatResponse {
  status?: string;
  product_name?: string;
  candidates?: string[];
  message?: string;
  error?: string;
}

async function postChat(brand: string, message: string, lastProduct?: string | null): Promise<ChatResponse> {
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    const res = await fetch(`${BASE_URL}/api/catalog/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ brand, message, history: [], lastProduct: lastProduct ?? null }),
      signal: controller.signal,
    });
    clearTimeout(timeout);
    if (!res.ok) return { error: `HTTP ${res.status}` };
    return await res.json();
  } catch (err) {
    return { error: err instanceof Error ? err.message : String(err) };
  }
}

interface Case {
  id: string;
  brand: string;
  query: string;
  lastProduct: string | null;
  expectedStatusNotIn: string[];
  expectedProductName?: string;
  expectedCandidates?: string[];
  note: string;
}

const CASES: Case[] = [
  {
    id: 'norma-anchor-preferred-over-tied-clarify-exact-repro',
    brand: 'Pianca',
    query: 'give all norma price',
    lastProduct: 'Norma (CollezioneNotte)',
    expectedStatusNotIn: ['clarify_product'],
    expectedProductName: 'Norma (CollezioneNotte)',
    note: 'The exact user-reported repro. Before the fix, this reset to a flat 3-way clarify_product (Norma Up / Norma (CollezioneNotte) / Norma (CollezioneGiorno)) despite lastProduct already establishing which one was meant. Run 5x live against the real (non-deterministic LLM) server before this was trusted, since the bug lived in TWO different code paths depending on what the LLM happened to guess.',
  },
  {
    id: 'norma-query-naming-different-sibling-overrides-stale-anchor',
    brand: 'Pianca',
    query: 'give Norma CollezioneGiorno price',
    lastProduct: 'Norma (CollezioneNotte)',
    expectedStatusNotIn: ['clarify_product'],
    expectedProductName: 'Norma (CollezioneGiorno)',
    note: 'Guard: when the query itself unambiguously names a DIFFERENT tied sibling than the anchor, that name must win -- the anchor-preference fix must only fire when the query genuinely doesn\'t distinguish between the tied candidates on its own, never override an explicit, different, real request.',
  },
  {
    id: 'norma-unrelated-anchor-does-not-force-resolve',
    brand: 'Pianca',
    query: 'give all norma price',
    lastProduct: 'Esse',
    expectedStatusNotIn: [],
    expectedCandidates: ['Norma (CollezioneNotte)', 'Norma Up', 'Norma (CollezioneGiorno)'],
    note: 'Guard: an anchor from a completely unrelated prior product (not among the tied candidates at all) must NOT force-resolve a genuinely fresh ambiguity -- the ordinary 3-way clarify must still fire.',
  },
];

async function main() {
  const failures: string[] = [];

  const probe = await postChat('Pianca', 'ping');
  if (probe.error) {
    console.error(`\nCannot reach ${BASE_URL}/api/catalog/chat (${probe.error}).`);
    console.error('Start the server first: npm run dev:server\n');
    process.exit(1);
  }

  for (const c of CASES) {
    // The exact repro is run 5x since the bug lived in 2 different code
    // paths (answer()'s own tie-break, and the LLM-driven
    // checkFamilyAmbiguity backstop) depending on the non-deterministic
    // LLM's own guess -- a single run could pass by luck.
    const runs = c.id === 'norma-anchor-preferred-over-tied-clarify-exact-repro' ? 5 : 1;
    let allOk = true;
    let lastResp: ChatResponse = {};
    for (let i = 0; i < runs; i++) {
      const resp = await postChat(c.brand, c.query, c.lastProduct);
      lastResp = resp;
      const statusOk = !c.expectedStatusNotIn.includes(resp.status || '');
      const productOk = !c.expectedProductName || resp.product_name === c.expectedProductName;
      const candidatesOk = !c.expectedCandidates ||
        JSON.stringify([...(resp.candidates || [])].sort()) === JSON.stringify([...c.expectedCandidates].sort());
      if (!(statusOk && productOk && candidatesOk)) { allOk = false; break; }
      await new Promise(r => setTimeout(r, 80));
    }

    if (!allOk) {
      failures.push(
        `[${c.id}] query=${JSON.stringify(c.query)} lastProduct=${JSON.stringify(c.lastProduct)} -- ` +
        `status=${lastResp.status} product_name=${JSON.stringify(lastResp.product_name)} candidates=${JSON.stringify(lastResp.candidates)} ` +
        `message=${JSON.stringify(lastResp.message)}`
      );
      console.log(`[${c.id.padEnd(56)}] FAIL`);
    } else {
      console.log(`[${c.id.padEnd(56)}] ok  (${runs} run${runs > 1 ? 's' : ''}, status=${lastResp.status}, product="${lastResp.product_name}")`);
    }
  }

  console.log('\n' + '='.repeat(70));
  console.log(`Total cases: ${CASES.length}`);
  console.log(`Total failures: ${failures.length}  <-- must be 0`);
  if (failures.length > 0) {
    console.log('\nFAILURES:');
    failures.forEach(f => console.log(`  ${f}`));
    console.log('\nEXIT 1: anchor tie-break regressed.');
    process.exit(1);
  }
  console.log('\nEXIT 0: a real, tied product-name ambiguity correctly defers to an already-established anchor, in both the deterministic and LLM-driven code paths, without ever overriding a genuinely different explicit request or an unrelated stale anchor.');
}

main();
