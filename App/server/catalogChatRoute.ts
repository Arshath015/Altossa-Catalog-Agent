/**
 * catalogChatRoute.ts
 * --------------------
 * Express route for the brand-scoped catalog chat.
 *
 * INTEGRATION:
 *   In your main server file (e.g. src/server/index.ts), add:
 *
 *     import catalogChatRouter from './catalogChatRoute';
 *     app.use('/api/catalog', catalogChatRouter);
 *
 *   Also make sure the data folder is served statically so image URLs
 *   like /data/Bolzan/images/awase_p17-017.jpg actually resolve:
 *
 *     app.use('/data', express.static(path.join(__dirname, '../../data')));
 *
 *   (Adjust the relative path to wherever your `data/` folder actually
 *   lives relative to this file -- the one created by extract_catalog.py.)
 *
 * ENDPOINT:
 *   POST /api/catalog/chat
 *   body: { brand: "Bolzan", message: "how much is the ceylon in 160x200 extra fabric" }
 *   response: ChatResult (see catalogChat.ts) as JSON
 *
 * One CatalogChat instance is cached per brand so prices.json/catalog_index.json
 * aren't re-read from disk on every message. If you update a brand's data
 * files while the server is running, restart the server (or add a
 * /api/catalog/reload endpoint if you want hot-reloading later).
 */

import express, { Router, Request, Response } from 'express';
import path from 'path';
import { CatalogChat, ChatResult } from './catalogChat';
import { extractIntent, ChatTurn } from './llmIntent';

const router: Router = express.Router();
router.use(express.json());

// data/ lives at the project root (sibling of App/), e.g.
// D:\Altossa\AI Catalog agent\data\Bolzan\...
// -- this matches the --out path used when running extract_catalog.py.
// From App/server/catalogChatRoute.ts, that's two levels up.
const DATA_ROOT = path.join(__dirname, '../../data');

const catalogCache = new Map<string, CatalogChat>();

function getCatalogChat(brand: string): CatalogChat | null {
  if (catalogCache.has(brand)) return catalogCache.get(brand)!;
  try {
    const cc = new CatalogChat(path.join(DATA_ROOT, brand));
    catalogCache.set(brand, cc);
    return cc;
  } catch (err) {
    console.error(`[catalog-chat] Failed to load data for brand "${brand}":`, err);
    return null;
  }
}

router.post('/chat', async (req: Request, res: Response) => {
  const { brand, message, history, lastProduct, lastModelVariant, lastCandidates } = req.body as {
    brand?: string;
    message?: string;
    history?: ChatTurn[];
    lastProduct?: string | null;
    lastModelVariant?: string | string[] | null;
    lastCandidates?: string[] | null;
  };

  if (!brand || typeof brand !== 'string') {
    return res.status(400).json({ error: 'Missing "brand" in request body.' });
  }
  if (!message || typeof message !== 'string' || !message.trim()) {
    return res.status(400).json({ error: 'Missing "message" in request body.' });
  }

  const catalogChat = getCatalogChat(brand);
  if (!catalogChat) {
    return res.status(404).json({
      error: `No catalog data found for brand "${brand}". Expected data at ${path.join(DATA_ROOT, brand)}.`,
    });
  }

  // Variant-phrase fallback (see findByVariantPhrase's own doc comment,
  // catalogChat.ts) is checked HERE, BEFORE the LLM step below, not only
  // inside answer()'s own no-match branch -- found necessary via live
  // testing 2026-09-01, not assumed: when the message doesn't literally
  // name a real product, the LLM is handed either a cheap shortlist or (on
  // a shortlist miss) the WHOLE catalog's product-NAME list, and it can
  // return a confident-but-WRONG product_names guess built purely from
  // surface word resemblance ("give all Cuscini opzionali per seduta
  // prices" -> "Cuscini decorativi", "give large armrest cushion price" ->
  // "Freedom 2.0 sofa-bed armrests") -- since neither guess is EMPTY,
  // answerFromIntent trusts it directly and never calls answer() at all,
  // so a fallback that only fires on answer()'s own "matches.length === 0"
  // branch never gets a chance to run for these. Checking here instead --
  // mirroring findProductsByCode's own "a more precise signal wins
  // outright" precedent inside answer() -- resolves the query BEFORE the
  // LLM (which only ever sees product NAMES, never variant_context/
  // model_variant text) has a chance to guess wrong from a name-only list.
  //
  // NOT gated on detectNamedProductsInText any more -- a second live-
  // testing round (2026-09-01) found "Round footstool diameter 60 price"
  // still failed, because "Round" is ITSELF a real, unrelated one-word
  // Ditre product name literally present in the query, so the original
  // gate ("only run this when nothing is literally named") skipped the
  // fallback entirely and the query silently resolved to the wrong
  // product. findByVariantPhrase now does its OWN internal comparison
  // (see its doc comment) between a competing named-product match and the
  // variant-phrase match, only overriding the named match when the phrase
  // demonstrably explains MORE of the query -- safe to call
  // unconditionally, since it defers to the named match in every other
  // case, including when nothing is named at all (its original behavior).
  {
    const variantMatch = catalogChat.findByVariantPhrase(message, brand);
    if (variantMatch) {
      return res.json(variantMatch);
    }
  }

  // Try the LLM intent step first (handles typos, natural phrasing, and
  // follow-ups using conversation history + the explicit lastProduct
  // anchor). If it's unavailable, times out, or fails in any way,
  // extractIntent returns null and we fall through to pure deterministic
  // matching -- the app never breaks and never trusts an LLM-invented
  // price, only its guess at WHICH row to look up (and even that guess
  // gets validated inside answerFromIntent).
  //
  // COST: the full product list dominates the token cost of every call
  // (measured: 75.8% of the system prompt for Cattelan Italia's 533
  // products, resent unchanged on every single message). So the first
  // attempt uses a cheap SHORTLIST of candidate names (buildLlmShortlist
  // -- exact mentions + similarity scoring + fuzzy edit-distance typo
  // matching, all reused from the same logic that already resolves these
  // deterministically elsewhere in the app) instead of the whole catalog.
  // If that shortlisted call comes back with no confident match, retry
  // ONCE with the full list -- this is the safety valve for the rare case
  // where the shortlist itself missed the right candidate (an unusually
  // aggressive typo the fuzzy threshold didn't catch), so a shortlist
  // miss costs one extra full-price call instead of silently losing the
  // product the way Bug 3's original silent-drop did.
  const shortlist = catalogChat.buildLlmShortlist(message);

  // The lastProduct "assume they still mean X" anchor is only trustworthy
  // when the CURRENT message itself doesn't already name a real product --
  // the moment it does, that's strong evidence this is a fresh,
  // self-contained reference, and feeding a stale anchor in anyway only
  // risks the LLM substituting an old (possibly multi-product, comma-
  // joined) name for one it should resolve fresh from the shortlist/
  // message text itself. Confirmed real risk, not hypothetical: this is
  // exactly the shape of a user-reported hypothesis ("TINA leaking into a
  // GRETA Wood/WILMA query" after a prior "SIERRA pouf, TINA" turn) --
  // GRETA Wood IS named exactly in that message, so under this rule the
  // anchor is omitted entirely for that call, closing off that channel
  // regardless of whether it was ever the actual cause. Uses the same
  // deterministic exact-name scan already used elsewhere (no LLM call
  // needed to decide this).
  const currentMessageNamesOwnProduct = catalogChat.detectNamedProductsInText(message).length > 0;
  const effectiveLastProduct = currentMessageNamesOwnProduct ? null : (lastProduct || null);
  // Same gating as effectiveLastProduct just above, same reasoning: a
  // message that already names something fresh shouldn't have a stale
  // candidate-list anchor injected either.
  const effectiveLastCandidates = currentMessageNamesOwnProduct
    ? null
    : (Array.isArray(lastCandidates) && lastCandidates.length > 0 ? lastCandidates : null);

  // Deterministic anchor-vocab override, checked BEFORE the LLM call --
  // closes a real gap found 2026-09-04: queryOnlySpecifiesAnchorProductDetails
  // (see its own doc comment, catalogChat.ts) previously only ever ran
  // once the WHOLE Groq key pool was down (the `!intent` branch below),
  // so a live LLM call with an anchor present had NO equivalent
  // protection at all. Confirmed real, not theoretical: "give gambe
  // price" right after a Pianca "Esse" turn -- Esse's own real
  // variant_context text -- returned a confident, structurally normal-
  // looking LlmIntent naming "Gamma" (a real but entirely unrelated
  // product, zero textual grounding for it anywhere in the query). There
  // is no confidence signal on LlmIntent to detect this after the fact
  // (checked llmIntent.ts -- product_names/size/fabric_tier/
  // wants_full_list only), so this can't be a post-hoc "does the LLM's
  // answer look weak" filter; it has to run independently of what the
  // LLM would say.
  //
  // Gated on ALL FOUR of the following, not just the vocab check alone,
  // because queryOnlySpecifiesAnchorProductDetails proves the query is
  // CONSISTENT with the anchor, never that it's EXCLUSIVE to it --
  // confirmed live: "give gambe price" independently passes this same
  // check against Esse, Cora, AND Domino (3 unrelated real Pianca
  // products all share real "gambe" vocabulary), so passing it alone is
  // not sufficient grounds to override a fresh, differently-grounded
  // resolution:
  //   1. currentMessageNamesOwnProduct is false -- the message doesn't
  //      already name something fresh (same gate effectiveLastProduct
  //      itself already uses, just below).
  //   2. effectiveLastProduct exists -- there's a real anchor to defer to.
  //   3. matchProducts(message) is EMPTY -- the deterministic name/code
  //      matcher independently finds ZERO candidates in the raw text.
  //      This is the guard that keeps the override narrow: it only fires
  //      when there is no OTHER textual signal at all pointing to any
  //      product, so it can never suppress a genuinely different,
  //      independently-grounded match (typo'd or exact) the deterministic
  //      matcher (or, by extension, the LLM) would otherwise have found.
  //   4. queryOnlySpecifiesAnchorProductDetails confirms every real word
  //      left is explained by the anchor's own known vocabulary.
  //
  // Accepted, bounded residual risk (not closed by this guard, and not
  // in scope to close here): the LLM sees the full `history` array
  // across every turn, while this check only ever sees the single most
  // recent `lastProduct`. A genuine multi-turn topic switch back to an
  // OLDER product, expressed only in shared/generic vocabulary (no
  // product name in the current message at all), would still be
  // incorrectly pulled back to the most recent anchor here -- this
  // exact blind spot already exists today in the `!intent` branch below
  // (Groq-down fallback also only ever sees `lastProduct`), just rarely
  // triggered; this change makes it reachable more often (any anchored,
  // vocab-only follow-up, not just a full Groq outage) without changing
  // its shape. Full multi-turn anchor tracking would be a separate,
  // larger redesign.
  if (!currentMessageNamesOwnProduct && effectiveLastProduct
    && catalogChat.matchProducts(message).length === 0
    && catalogChat.queryOnlySpecifiesAnchorProductDetails(message, effectiveLastProduct)) {
    console.log(
      `[anchor-vocab-override] message=${JSON.stringify(message)} anchor=${JSON.stringify(effectiveLastProduct)} ` +
      `-- resolving deterministically without calling the LLM (query fully explained by anchor's own vocabulary, no competing name match)`
    );
    return res.json(catalogChat.answer(message, brand, lastModelVariant || null, effectiveLastProduct, effectiveLastCandidates));
  }

  // Logged unconditionally (not just for multi-product calls) -- cheap,
  // and this is exactly the trail needed to catch a real recurrence of
  // the anchor-contamination hypothesis in production instead of trying
  // to reconstruct it after the fact from memory.
  console.log(
    `[lastProduct-anchor] message=${JSON.stringify(message)} clientLastProduct=${JSON.stringify(lastProduct || null)} ` +
    `usedInPrompt=${JSON.stringify(effectiveLastProduct)} reason=${currentMessageNamesOwnProduct ? 'omitted (message already names a real product)' : (effectiveLastProduct ? 'included (message names nothing on its own)' : 'none available')}`
  );

  let intent = await extractIntent(message, history || [], shortlist, effectiveLastProduct);
  if (intent && (!intent.product_names || intent.product_names.length === 0)) {
    console.log(`[shortlist-fallback] "${message}" -- shortlist of ${shortlist.length} had no confident match, retrying with full ${catalogChat.getProductNames().length}-product list`);
    intent = await extractIntent(message, history || [], catalogChat.getProductNames(), effectiveLastProduct);
  }

  const result: ChatResult = intent
    ? catalogChat.answerFromIntentMulti(intent.product_names, intent.size, intent.fabric_tier, message, brand, lastModelVariant || null, intent.wants_full_list, effectiveLastProduct, effectiveLastCandidates)
    : catalogChat.answer(message, brand, lastModelVariant || null, effectiveLastProduct, effectiveLastCandidates);

  // Full raw request/response capture for every MULTI-PRODUCT resolution
  // specifically -- this is the exact class of call where cross-product
  // contamination (tier leak, shared-array leak, or a stale anchor
  // substituting a wrong product) can happen. Captures enough to diagnose
  // a real recurrence without needing to reconstruct the session
  // afterward: the raw client-supplied lastProduct/history, what actually
  // went into the prompt, the LLM's raw product_names/fabric_tier/size
  // guess, and the final per-product tier/price breakdown actually
  // returned.
  const isMultiProduct = result.status === 'multi_product' || (intent?.product_names && intent.product_names.length > 1);
  if (isMultiProduct) {
    const perProductBreakdown = (result.matches || []).map(m => `${m.product_name}:${m.fabric_tier}=€${m.price_eur}`);
    console.log(
      `[multi-product-capture] message=${JSON.stringify(message)} ` +
      `clientLastProduct=${JSON.stringify(lastProduct || null)} usedLastProduct=${JSON.stringify(effectiveLastProduct)} ` +
      `historyLen=${(history || []).length} ` +
      `llmProductNames=${JSON.stringify(intent?.product_names ?? null)} llmFabricTier=${JSON.stringify(intent?.fabric_tier ?? null)} llmSize=${JSON.stringify(intent?.size ?? null)} ` +
      `finalRows=${JSON.stringify(perProductBreakdown)}`
    );
  }

  // extractIntent returns null only once it has tried EVERY configured
  // Groq key (see groqKeyPool.ts -- key 1, then 2, then 3, in that fixed
  // order) and all of them failed or were still cooling down from a
  // prior rate limit -- never on the model successfully running and just
  // being unsure (that still returns a real LlmIntent with
  // product_names: null), and never just because ONE key is exhausted
  // (that fails over to the next key invisibly, no signal shown). So a
  // null intent here specifically means this reply used the
  // deterministic-only fallback because the WHOLE key pool is down,
  // which has no typo tolerance and can't combine multiple products in
  // one message the way the LLM path can. Surface that plainly instead of
  // letting the user silently get a lower-quality reply with no signal
  // that anything changed.
  if (!intent) {
    result.degraded = true;
    result.message += '\n\n(Using basic matching for this reply -- some phrasing variations, typos, or multi-product requests may not be recognized.)';
  }

  res.json(result);
});

// Optional: force-reload a brand's data (e.g. after re-running parse_prices.py)
router.post('/reload/:brand', (req: Request, res: Response) => {
  const brandParam = req.params.brand;
  const brand = Array.isArray(brandParam) ? brandParam[0] : brandParam;
  catalogCache.delete(brand);
  const cc = getCatalogChat(brand);
  if (!cc) {
    return res.status(404).json({ error: `Could not reload brand "${brand}".` });
  }
  res.json({ reloaded: brand });
});

export default router;