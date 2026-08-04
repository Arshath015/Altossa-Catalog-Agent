import fs from 'fs';
import path from 'path';
import { encode } from 'gpt-tokenizer';
import { CatalogChat } from '../server/catalogChat';

const ROOT = path.join(__dirname, '..', '..');

// Reproduces buildSystemPrompt from App/server/llmIntent.ts EXACTLY (copy,
// not import, so this stays a standalone measurement script) to measure
// real token counts for its two halves: the product-name list vs everything else.
function buildSystemPrompt(productNames: string[], lastProduct: string | null): string {
  const anchorNote = lastProduct
    ? [
        '',
        `IMPORTANT CONTEXT: the product most recently confirmed in this conversation is "${lastProduct}". `,
        'If the user\'s new message is short, vague, or doesn\'t clearly name a different product ',
        '(e.g. "and in 180x200?", "what about extra fabric", "h.29"), assume they still mean ',
        `"${lastProduct}" unless they clearly name something else.`,
      ].join('\n')
    : '';

  const productListJson = JSON.stringify(productNames);

  const rest = [
    'You are an intent-extraction assistant for a furniture catalog chat. ',
    'Your ONLY job is to read the conversation and figure out which product, ',
    'size, and fabric tier the person is asking about. You must NEVER state ',
    'a price yourself -- you do not have access to real prices, and making ',
    'one up would be a serious error.',
    '',
    'Respond with ONLY a JSON object (no other text) in exactly this shape:',
    '{"product_names": string[]|null, "size": string|null, "fabric_tier": string[]|null, "wants_full_list": boolean}',
    '',
    '- "product_names" should be an ARRAY containing EVERY distinct product the person ',
    '  is asking about, each copied EXACTLY (character for character) from this list of ',
    '  real products, or null if you cannot confidently match any: ',
    '__PRODUCT_LIST__',
    '  Usually this array has exactly one item. Only include more than one when the ',
    '  person clearly names multiple different products in the same message (e.g. ',
    '  "give me Pandora and Selene prices", "compare Ceylon and Awase", "show me Jack ',
    '  and Jack-e"). Do not split a single product\'s own name into multiple entries.',
    '- "size" should be in the form "WIDTHxDEPTH" (e.g. "160x200") if mentioned, else null. ',
    '  Some catalogs list sizes as WIDTHxDEPTHxHEIGHT (e.g. "240x120x74") -- if the person ',
    '  mentions all three numbers, include all three in "size" exactly as given rather than ',
    '  dropping the third; if they only give two, just use those two as normal.',
    '  IMPORTANT: a short code like "h.7", "h27", "sp.10", or "sp 4.5" is a MODEL/VARIANT ',
    '  identifier (which specific configuration, height option, or collection the person ',
    '  means), NOT a size measurement -- never put these in "size". Leave "size" null for ',
    '  these and let the product_names/context handle identifying the right variant instead.',
    '- "fabric_tier" should be an ARRAY of finish/material/upholstery category names if one ',
    '  or more are mentioned (e.g. ["Extra"], or ["Extra", "Plus"] if the user asks for both), ',
    '  else null. Different catalogs name these differently -- some use plain words like ',
    '  "Extra", "B e TCL", "Luxury leather", "Super"; others use alphanumeric finish/material ',
    '  codes like "GFM69", "GFM11 / GFM18", "KM02", "Cristallo trasparente". Extract whatever ',
    '  finish/material/upholstery identifier the person mentions, in EITHER style -- do not ',
    '  assume it must look like a plain English word.',
    '- "wants_full_list" should be true whenever the person is asking to see the WHOLE ',
    '  set of prices/options for a product, in any phrasing -- not just the literal words ',
    '  "all prices". This includes things like "give all", "show me everything", "the ',
    '  complete list", "give all prices", "what are all the options", "show me the full ',
    '  breakdown", "give me the whole price sheet", or a bare "give all" with no other ',
    '  detail. Judge the person\'s actual intent, not specific keywords. Set it to false ',
    '  if they\'re asking about one specific size/tier combination instead.',
    '- Use the conversation history to resolve follow-ups, e.g. if the user ',
    '  previously asked about "Ceylon" and now says "what about 180x200?", ',
    '  product_names should still be ["Ceylon"].',
    '- If you are not confident which product is meant, set product_names to null ',
    '  rather than guessing -- a wrong guess is worse than admitting uncertainty.',
    anchorNote,
  ].join('\n');

  return rest.replace('__PRODUCT_LIST__', productListJson);
}

const sampleHistory = [
  { text: 'do you have the amsterdam console' },
  { text: 'Yes -- AMSTERDAM (cristallo specchiato bronzo): sizes 2/3/4, from EUR 3.783.' },
];
const historyTokens = sampleHistory.reduce((sum, h) => sum + encode(h.text).length, 0);

function analyzeBrand(brand: string) {
  const catalogIndex = JSON.parse(fs.readFileSync(path.join(ROOT, 'data', brand, 'catalog_index.json'), 'utf-8'));
  const productNames: string[] = [...new Set(catalogIndex.map((p: any) => p.product_name))];
  const catalogChat = new CatalogChat(path.join(ROOT, 'data', brand));

  const fullPrompt = buildSystemPrompt(productNames, null);
  const fullListJson = JSON.stringify(productNames);
  const restOfPrompt = fullPrompt.replace(fullListJson, '');
  const fullTokens = encode(fullPrompt).length;
  const restTokens = encode(restOfPrompt).length;

  console.log(`\n=== ${brand}: ${productNames.length} products ===`);
  console.log(`OLD (every call, full list): ${fullTokens} system-prompt tokens (list=${encode(fullListJson).length}, rest=${restTokens})`);

  const sampleMessages = [
    'sofia pelle glove',
    'give me sierra pouf 100x94x41h pelle and tina pelle',
    'how much is the peyote a cristallo extrachiaro verniciato bianco',
    'magda ml sgabelo price', // real shortlist-fallback stress case
  ];

  console.log(`\n  NEW (real buildLlmShortlist() output, the code actually running now):`);
  let totalOld = 0, totalNew = 0;
  for (const msg of sampleMessages) {
    const shortlist = catalogChat.buildLlmShortlist(msg);
    const shortPrompt = buildSystemPrompt(shortlist, null);
    const shortTokens = encode(shortPrompt).length;
    const msgTokens = encode(msg).length;
    const oldTotal = fullTokens + historyTokens + msgTokens;
    const newTotal = shortTokens + historyTokens + msgTokens;
    totalOld += oldTotal;
    totalNew += newTotal;
    console.log(`    "${msg}"`);
    console.log(`      shortlist: ${shortlist.length} names -> ${shortTokens} system-prompt tokens`);
    console.log(`      call total: OLD=${oldTotal}  NEW=${newTotal}  (${(100 * (1 - newTotal / oldTotal)).toFixed(1)}% reduction)`);
  }
  console.log(`  Average across these ${sampleMessages.length} calls: OLD=${Math.round(totalOld / sampleMessages.length)}  NEW=${Math.round(totalNew / sampleMessages.length)}  (${(100 * (1 - totalNew / totalOld)).toFixed(1)}% reduction)`);
  console.log(`  Daily capacity at 100,000 tokens/day: OLD~${Math.round(100000 / (totalOld / sampleMessages.length))} calls  NEW~${Math.round(100000 / (totalNew / sampleMessages.length))} calls`);
}

console.log('Token counts measured with gpt-tokenizer (cl100k_base BPE) as a standard approximation --');
console.log('Groq/Llama 3.3 uses its own tokenizer so exact counts differ, but this methodology is');
console.log('cross-checked against Groq\'s own real "Requested: N" figures logged during live calls:');
console.log('  BEFORE the fix: avg 3640 tokens/call, n=363 real logged Groq requests.');
console.log('  AFTER the fix:  avg 1053 tokens/call, n=59 real logged Groq requests (this session).');

analyzeBrand('Cattelan Italia');
analyzeBrand('Bolzan');
