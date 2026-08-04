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
  const { brand, message, history, lastProduct, lastModelVariant } = req.body as {
    brand?: string;
    message?: string;
    history?: ChatTurn[];
    lastProduct?: string | null;
    lastModelVariant?: string | null;
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
  let intent = await extractIntent(message, history || [], shortlist, lastProduct || null);
  if (intent && (!intent.product_names || intent.product_names.length === 0)) {
    console.log(`[shortlist-fallback] "${message}" -- shortlist of ${shortlist.length} had no confident match, retrying with full ${catalogChat.getProductNames().length}-product list`);
    intent = await extractIntent(message, history || [], catalogChat.getProductNames(), lastProduct || null);
  }

  const result: ChatResult = intent
    ? catalogChat.answerFromIntentMulti(intent.product_names, intent.size, intent.fabric_tier, message, brand, lastModelVariant || null, intent.wants_full_list)
    : catalogChat.answer(message, brand, lastModelVariant || null);

  // extractIntent returns null on ANY LLM-step failure (missing API key,
  // network error, timeout, rate limit, bad response) -- never on the
  // model successfully running and just being unsure (that still returns
  // a real LlmIntent with product_names: null). So a null intent here
  // specifically means this reply used the deterministic-only fallback,
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