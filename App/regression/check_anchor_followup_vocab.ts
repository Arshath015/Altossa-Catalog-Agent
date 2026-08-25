/**
 * regression/check_anchor_followup_vocab.ts
 * ---------------------------------
 * Permanent, GATING check for a real bug found via live testing 2026-08-25
 * (user-reported: "follow-up message not giving previous product's
 * answers"): a content-free follow-up naming NO product at all, right
 * after a turn that resolved one, is only reused via `lastProduct` when
 * `queryOnlySpecifiesAnchorProductDetails()` (catalogChat.ts) confirms
 * every real word in the query is already explained by that anchor
 * product's own known vocabulary. Two real gaps in that vocabulary build,
 * both reproduced live against Pianca's Esse (anchor established via
 * "esse price", multiple_options):
 *
 *   1. Esse's own real model_variant "non sfoderabile" typed back
 *      VERBATIM ("non sfoderabile price") still failed with
 *      no_product_match -- "non" is only 3 characters, so the vocabulary
 *      builder's `isDistinguishingWord` filter (>=4 chars or digit-
 *      bearing) silently dropped it, even though the FULL phrase is a
 *      complete, exact, real value for this product. Fixed by including
 *      every word of a verified-real variant phrase, not just the
 *      "distinguishing" ones -- safe specifically because the source is
 *      already a known-real complete string, not loose free text.
 *   2. "give category a price" also failed -- "CATEGORY" is the literal
 *      column header Pianca's own price grid displays for this tier
 *      dimension (tierColumnHeader() in CatalogChatWidget.tsx), but only
 *      bare tier VALUES ("a"/"b"/"c"/...) fed the vocabulary, never the
 *      tier_label WORD itself. Fixed by adding this anchor's own real
 *      tier_label word alongside its tier values.
 *
 * Also guards that neither fix loosened the existing Ada anchor-over-
 * trust safety check (an anchor must still be REJECTED, not silently
 * reused, when the query names a genuinely different/unrelated product)
 * -- see check_ditre_matching.ts's own "regress-*-guard-*" cases for the
 * fuller battery on that; case 3 below is a lighter cross-check.
 *
 * RUN WITH: npm run check-anchor-followup-vocab
 * Requires the dev server running (npm run dev:server).
 */

const BASE_URL = process.env.REGRESSION_BASE_URL || 'http://localhost:3000';

interface ChatResponse {
  status?: string;
  product_name?: string;
  matches?: unknown[];
  error?: string;
}

interface Case {
  id: string;
  brand: string;
  query: string;
  lastProduct: string;
  expectedProductName: string;
  expectMinRows: number;
  note: string;
}

const CASES: Case[] = [
  {
    id: 'short-word-in-real-variant-phrase',
    brand: 'Pianca',
    query: 'non sfoderabile price',
    lastProduct: 'Esse',
    expectedProductName: 'Esse',
    expectMinRows: 1,
    note: 'Exact reported repro -- "non sfoderabile" is Esse\'s own real, complete model_variant value; "non" alone is too short (<4 chars) to have made it into anchorVocab before the fix.',
  },
  {
    id: 'tier-label-word-category',
    brand: 'Pianca',
    query: 'give category a price',
    lastProduct: 'Esse',
    expectedProductName: 'Esse',
    expectMinRows: 1,
    note: 'Exact reported repro -- "category" is the literal column header word this product\'s own price grid displays, but was never recognized as anchor vocabulary before the fix (only bare tier values were).',
  },
  {
    id: 'tier-label-word-category-no-give',
    brand: 'Pianca',
    query: 'category a price',
    lastProduct: 'Esse',
    expectedProductName: 'Esse',
    expectMinRows: 1,
    note: 'Same as tier-label-word-category but without the leading "give" -- confirms the fix is about vocabulary recognition, not an unrelated phrasing quirk.',
  },
];

interface GuardCase {
  id: string;
  brand: string;
  query: string;
  lastProduct: string;
  mustNotBe: string;
  note: string;
}

const GUARD_CASES: GuardCase[] = [
  {
    id: 'guard-unrelated-product-not-reused',
    brand: 'Ditre Italia',
    query: 'give online 2er sofa price',
    lastProduct: 'Ada (Sofa)',
    mustNotBe: 'Ada (Sofa)',
    note: 'Widening anchorVocab must not resurrect the original Ada anchor-over-trust bug -- "online 2er sofa" names a real, different product (On Line) and must resolve to THAT, never silently fall back to the Ada anchor just because it exists.',
  },
];

async function postChat(brand: string, message: string, lastProduct: string): Promise<ChatResponse> {
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    const res = await fetch(`${BASE_URL}/api/catalog/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        brand, message,
        history: [{ role: 'user', text: `${lastProduct} price` }, { role: 'assistant', text: '...' }],
        lastProduct, lastModelVariant: null, lastCandidates: null,
      }),
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
  const probe = await postChat(CASES[0].brand, 'ping', CASES[0].lastProduct);
  if (probe.error) {
    console.error(`\nCannot reach ${BASE_URL}/api/catalog/chat (${probe.error}).`);
    console.error('Start the server first: npm run dev:server\n');
    process.exit(1);
  }

  const failures: string[] = [];

  for (const c of CASES) {
    const resp = await postChat(c.brand, c.query, c.lastProduct);
    const rows = (resp.matches || []).length;
    const ok = resp.product_name === c.expectedProductName && rows >= c.expectMinRows;
    if (!ok) {
      failures.push(`[${c.id}] "${c.query}" (anchor=${c.lastProduct}) -- expected product_name="${c.expectedProductName}" with >=${c.expectMinRows} rows, got status=${resp.status || resp.error} product_name=${resp.product_name} rows=${rows}. ${c.note}`);
    }
    console.log(`[${c.id.padEnd(36)}] ${ok ? 'ok' : 'FAIL'}  status=${resp.status}  product=${resp.product_name}  rows=${rows}`);
    await new Promise(r => setTimeout(r, 80));
  }

  for (const c of GUARD_CASES) {
    const resp = await postChat(c.brand, c.query, c.lastProduct);
    const ok = resp.product_name !== c.mustNotBe;
    if (!ok) {
      failures.push(`[${c.id}] "${c.query}" (anchor=${c.lastProduct}) -- must NOT resolve to "${c.mustNotBe}", but it did. ${c.note}`);
    }
    console.log(`[${c.id.padEnd(36)}] ${ok ? 'ok' : 'FAIL'}  status=${resp.status}  product=${resp.product_name}`);
    await new Promise(r => setTimeout(r, 80));
  }

  console.log('\n' + '='.repeat(70));
  console.log(`Total cases: ${CASES.length + GUARD_CASES.length}`);
  console.log(`Failures: ${failures.length}  <-- must be 0`);
  if (failures.length > 0) {
    console.log('\nFAILURES:');
    failures.forEach(f => console.log(`  ${f}`));
  }

  if (failures.length > 0) {
    console.log('\nEXIT 1: anchor follow-up vocabulary regressed.');
    process.exit(1);
  }
  console.log('\nEXIT 0: anchor follow-up vocabulary recognizes short real-variant words and tier_label words, and the anchor-over-trust guard still holds.');
}

main();
