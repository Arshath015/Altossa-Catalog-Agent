import { useState, useRef, useEffect } from 'react';
import { Send, AlertTriangle, HelpCircle, ImageIcon } from 'lucide-react';
import type { ImagePanelData } from '../ImagePanel';
import { buildVariantGroups, buildSizeColumns, findCell } from './priceGridGrouping';
import type { PriceRow, VariantGroup } from './priceGridGrouping';

/**
 * CatalogChatWidget
 * ------------------
 * Brand-scoped chat: ask about a product's price, get the answer as text
 * + data tables here in the center column. The real catalog page
 * screenshot for each response is pushed up to the parent via
 * `onLatestImages` and rendered in the dedicated right-hand ImagePanel
 * instead of inline -- keeps the conversation itself text-focused.
 *
 * Expects a backend route at POST /api/catalog/chat (see catalogChatRoute.ts)
 * that returns a ChatResult shaped like:
 *   { status, message, product_name?, matches?, image_urls? }
 */

// PriceRow is now defined in ./priceGridGrouping (imported above) so the
// grouping/column logic there and the API response shape here can never
// drift apart.

/** The column header to show for a row's fabric_tier value. Three cases,
 * not two -- collapsing them into one hardcoded "FABRIC" is the exact bug
 * this fixes:
 *   1. A real source-PDF label was recovered ("Base", "Top",
 *      "Rivestimento"...) -- show it.
 *   2. No label recovered, but real fabric_tier VALUES exist (either a
 *      genuinely upholstered item, where "FABRIC" is accurate, or an
 *      unlabeled edge case where the data is real but the label word
 *      wasn't confidently recoverable) -- "FABRIC" as a fallback, same
 *      as the status quo, not a regression.
 *   3. NO row has a fabric_tier value at all -- the product has no such
 *      dimension whatsoever (e.g. BOTERO Wood Round: price only varies
 *      by size). Showing "FABRIC" above a column of "—" placeholders is
 *      actively wrong, not just imprecise -- match the "—" placeholder
 *      convention already used for a missing value elsewhere in this
 *      table instead of inventing a category that isn't there. */
function tierColumnHeader(rows: PriceRow[]): string {
  const real = rows.find(r => r.tier_label)?.tier_label;
  if (real) return real.toUpperCase();
  const hasAnyTierValue = rows.some(r => r.fabric_tier);
  return hasAnyTierValue ? 'FABRIC' : '—';
}

interface ChatResult {
  status: 'ok' | 'multiple_options' | 'full_price_grid' | 'multi_product' | 'ambiguous_price' | 'no_matching_variant'
        | 'no_price_data' | 'no_product_match' | 'clarify_product';
  message: string;
  product_name?: string;
  candidates?: string[];
  /** Display-only labels parallel to `candidates` (see catalogChat.ts's
   * formatProductDisplayName) -- render these, but keep sending back
   * `candidates[i]` (unchanged) wherever the raw name is needed. */
  candidateLabels?: string[];
  matches?: PriceRow[];
  image_urls?: string[];
}

interface ChatMessage {
  role: 'user' | 'assistant';
  text: string;
  result?: ChatResult;
}

/** One brand's entire chat session -- messages plus the anchor context used
 * to resolve content-free follow-ups ("give all", "yes"). Lives in a
 * Record<brand, BrandSession> at the CatalogApp level so switching brands
 * swaps which slot this widget reads/writes instead of unmounting the
 * widget and destroying state (the old `key={brand}` approach). Anchors
 * are per-slot so a product resolved while chatting about one brand can
 * never leak into another brand's follow-up resolution. */
export interface BrandSession {
  messages: ChatMessage[];
  loading: boolean;
  lastProduct: string | null;
  lastModelVariant: string | string[] | null;
  lastCandidates: string[] | null;
}

export function createInitialBrandSession(brand: string): BrandSession {
  return {
    messages: [
      {
        role: 'assistant',
        text: `Ask me about any ${brand} product - e.g. "how much is the Ceylon in 160x200, Extra fabric?"`,
      },
    ],
    loading: false,
    lastProduct: null,
    lastModelVariant: null,
    lastCandidates: null,
  };
}

const STATUS_STYLES: Record<string, { badge: string; color: string; icon?: 'warn' | 'help' }> = {
  ok: { badge: 'VERIFIED PRICE', color: 'text-[var(--riso-yellow)] border-[var(--riso-yellow)]' },
  full_price_grid: { badge: 'FULL PRICE LIST', color: 'text-[var(--riso-yellow)] border-[var(--riso-yellow)]' },
  multi_product: { badge: 'MULTIPLE PRODUCTS', color: 'text-[var(--riso-yellow)] border-[var(--riso-yellow)]' },
  multiple_options: { badge: 'MULTIPLE MATCHES', color: 'text-[var(--riso-pink)] border-[var(--riso-pink)]' },
  ambiguous_price: { badge: 'NEEDS CONFIRMATION', color: 'text-orange-400 border-orange-400', icon: 'warn' },
  no_matching_variant: { badge: 'NOT FOUND', color: 'text-stone-400 border-stone-500', icon: 'help' },
  no_price_data: { badge: 'NO PRICE DATA', color: 'text-stone-400 border-stone-500', icon: 'help' },
  no_product_match: { badge: "COULDN'T FIND PRODUCT", color: 'text-stone-400 border-stone-500', icon: 'help' },
  clarify_product: { badge: 'WHICH ONE?', color: 'text-[var(--riso-pink)] border-[var(--riso-pink)]', icon: 'help' },
};

// Same summarization need as buildMultiProductResult's message fix, just for
// the ImagePanel's label -- result.product_name is a raw comma-joined
// concatenation of every candidate name (all ~77 for a large multi_product
// result), never meant to be displayed whole as a single-line label.
const LABEL_NAME_THRESHOLD = 6;
function buildImagePanelLabel(result: ChatResult, variantsInResult: string[]): string | null {
  if (variantsInResult.length === 1) return variantsInResult[0];
  const names = (result.product_name || '').split(', ').filter(Boolean);
  if (names.length > LABEL_NAME_THRESHOLD) {
    return `${names.length} matching products`;
  }
  return result.product_name || null;
}

export default function CatalogChatWidget({
  brand,
  session,
  onSessionChange,
  onLatestImages,
}: {
  brand: string;
  session: BrandSession;
  onSessionChange: (updater: (prev: BrandSession) => BrandSession) => void;
  onLatestImages?: (data: ImagePanelData) => void;
}) {
  const { messages, loading, lastProduct, lastModelVariant, lastCandidates } = session;
  const bottomRef = useRef<HTMLDivElement>(null);
  /** Tracks the currently-selected brand so a response that resolves after
   * the user has already switched brands doesn't push its images into the
   * ImagePanel for whatever brand is now on screen. The session data itself
   * still lands in the correct (originating) brand's slot regardless. */
  const currentBrandRef = useRef(brand);
  useEffect(() => {
    currentBrandRef.current = brand;
  }, [brand]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, loading]);

  async function sendMessage(text: string) {
    if (!text.trim() || loading) return;
    const requestBrand = brand;
    const historyForRequest = messages.slice(-4).map(m => ({ role: m.role, text: m.text }));
    onSessionChange(prev => ({ ...prev, messages: [...prev.messages, { role: 'user', text }], loading: true }));
    try {
      const res = await fetch('/api/catalog/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          brand: requestBrand,
          message: text,
          history: historyForRequest,
          lastProduct,
          lastModelVariant,
          lastCandidates,
        }),
      });
      const result: ChatResult = await res.json();
      // Every status that DOES represent a definitively identified product
      // (or joined multi-product set) sets product_name -- clarify_product
      // and no_product_match never do. Without this else branch, an
      // ambiguous/unresolved turn left the PREVIOUS turn's anchor
      // untouched, so a stale product from several turns back could
      // silently survive any number of ambiguous exchanges and get reused
      // by a later content-free follow-up ("yes, give all") that has
      // nothing to do with it -- confirmed live: "price of Bahia 2260M"
      // (resolves) -> two separate ambiguous Big/Big Light turns (neither
      // sets product_name) -> "yes, give all" still silently returned
      // Bahia 2260M's price grid. Mirrors the existing lastModelVariantRef
      // else-clears-to-null branch just below, which never had this bug.
      //
      // lastCandidatesRef is the mutually-exclusive sibling: a
      // clarify_product turn sets IT (and clears lastProduct via the else
      // branch above), a resolved turn clears IT (and sets lastProduct),
      // any other outcome (no_product_match, etc.) clears both -- exactly
      // one of the two anchors is ever populated at a time, never both, so
      // a later content-free follow-up always has a single unambiguous
      // source of context to fall back on.
      let newLastProduct: string | null;
      let newLastCandidates: string[] | null;
      if (result.product_name) {
        newLastProduct = result.product_name;
        newLastCandidates = null;
      } else if (result.status === 'clarify_product' && result.candidates && result.candidates.length > 0) {
        newLastProduct = null;
        newLastCandidates = result.candidates;
      } else {
        newLastProduct = null;
        newLastCandidates = null;
      }
      const variantsInResult = [...new Set((result.matches || []).map(m => m.model_variant).filter(Boolean))] as string[];
      let newLastModelVariant: string | string[] | null;
      if (variantsInResult.length === 1) {
        newLastModelVariant = variantsInResult[0];
      } else if (variantsInResult.length > 1) {
        // Not a single exact variant, but if they all share the same
        // short distinguishing code -- "h.NN" (e.g. "h.8 basamento" +
        // "h.8 struttura") or "sp.NN" (e.g. Wall System's 4 different
        // "sp.10" collections) -- remember one of them anyway. The
        // server-side fallback only extracts this same short code from
        // the string, so this preserves "we were talking about this
        // family" for a follow-up like "give all" instead of losing all
        // context and broadening to everything.
        const shortCode = (v: string) => {
          const m = v.match(/\b(h|sp)\.?\s*(\d+(?:\.\d+)?)\b/i);
          return m ? `${m[1].toLowerCase()}:${m[2]}` : null;
        };
        const codes = new Set(variantsInResult.map(shortCode).filter(Boolean));
        // No single short code unifies them -- rather than losing the
        // anchor entirely (the old behavior: falling back to null), keep
        // the FULL list. Needed since tonight's own same-product multi-
        // variant elision feature can produce a genuine MULTIPLE PRODUCTS
        // turn discussing 2+ real, differently-named variants at once
        // (e.g. "3-er sofa" AND "3-er maxi sofa", no shared h.NN/sp.NN
        // code between them at all) -- a vague follow-up right after that
        // turn ("give all online price") needs to recall BOTH, not
        // silently broaden to every variant of the product as if nothing
        // had just been discussed.
        newLastModelVariant = codes.size === 1 ? variantsInResult[0] : variantsInResult;
      } else {
        newLastModelVariant = null;
      }

      if (currentBrandRef.current === requestBrand) {
        onLatestImages?.({
          urls: result.image_urls || [],
          label: buildImagePanelLabel(result, variantsInResult),
          status: result.status,
        });
      }

      onSessionChange(prev => ({
        ...prev,
        messages: [...prev.messages, { role: 'assistant', text: result.message, result }],
        loading: false,
        lastProduct: newLastProduct,
        lastCandidates: newLastCandidates,
        lastModelVariant: newLastModelVariant,
      }));
    } catch (err) {
      onSessionChange(prev => ({
        ...prev,
        messages: [...prev.messages, {
          role: 'assistant',
          text: "Something went wrong reaching the catalog. Please try again.",
        }],
        loading: false,
      }));
    }
  }

  return (
    <div className="flex flex-col h-full bg-[var(--riso-bg)]">
      <div className="px-6 py-5 border-b-2 border-[var(--riso-line)]">
        <div className="font-display font-bold text-xl tracking-tight text-[var(--riso-text)]">
          ASK <span className="text-[var(--riso-pink)]">{brand.toUpperCase()}</span>
        </div>
        <div className="font-data text-[10px] text-stone-500 mt-0.5 uppercase tracking-widest">
          Every number below is read from the verified price list
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-6 py-6 space-y-7">
        {messages.map((m, i) => (
          <MessageBubble key={i} message={m} onShowImages={onLatestImages} onSelectCandidate={sendMessage} />
        ))}
        {loading && (
          <div className="flex items-center gap-1.5 pl-1">
            <span className="w-2 h-2 bg-[var(--riso-pink)] animate-bounce [animation-delay:-0.3s]" />
            <span className="w-2 h-2 bg-[var(--riso-yellow)] animate-bounce [animation-delay:-0.15s]" />
            <span className="w-2 h-2 bg-[var(--riso-pink)] animate-bounce" />
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      <div className="border-t-2 border-[var(--riso-line)] px-4 py-4">
        <ChatInput brand={brand} loading={loading} onSend={sendMessage} />
      </div>
    </div>
  );
}

function ChatInput({ brand, loading, onSend }: { brand: string; loading: boolean; onSend: (text: string) => void }) {
  const [value, setValue] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 140)}px`;
  }, [value]);

  function submit() {
    if (!value.trim() || loading) return;
    onSend(value);
    setValue('');
  }

  return (
    <div className="flex items-end gap-2 border-2 border-[var(--riso-line)] bg-[var(--riso-surface)] px-3 py-2 focus-within:border-[var(--riso-pink)] transition-colors">
      <textarea
        ref={textareaRef}
        rows={1}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            submit();
          }
        }}
        placeholder={`Ask about a ${brand} product…`}
        className="flex-1 resize-none bg-transparent font-data text-sm leading-6 text-[var(--riso-text)] placeholder:text-stone-500 focus:outline-none py-1 max-h-[140px]"
      />
      <button
        type="button"
        onClick={submit}
        disabled={loading || !value.trim()}
        className="shrink-0 w-9 h-9 border-2 border-[var(--riso-line)] bg-[var(--riso-pink)] disabled:bg-transparent disabled:border-stone-700 disabled:cursor-not-allowed flex items-center justify-center transition-colors"
        aria-label="Send message"
      >
        <Send size={15} className={value.trim() ? 'text-[#131217]' : 'text-stone-600'} />
      </button>
    </div>
  );
}

function MessageBubble({ message, onShowImages, onSelectCandidate }: {
  message: ChatMessage;
  onShowImages?: (data: ImagePanelData) => void;
  /** Clicking a candidate chip (see the `candidates` rendering below) sends
   * its exact raw value as the next message -- reuses the SAME sendMessage
   * path the text input already uses, so it behaves identically to typing
   * the candidate and hitting enter. No new backend logic: candidates/
   * lastCandidates already round-trip correctly, this just gives the user
   * a way to pick one without having to type it back (previously required,
   * even when the qualifier text wasn't something they could safely type --
   * e.g. Tier 2 collision qualifiers with no customer-facing label yet). */
  onSelectCandidate?: (candidate: string) => void;
}) {
  const isUser = message.role === 'user';

  if (isUser) {
    return (
      <div className="flex justify-end">
        <div className="max-w-[75%] bg-[var(--riso-pink)] text-[#131217] font-data text-sm leading-6 px-4 py-2.5 border-2 border-[var(--riso-pink)]">
          {message.text}
        </div>
      </div>
    );
  }

  const status = message.result?.status;
  const style = status ? STATUS_STYLES[status] : null;
  const hasImages = (message.result?.image_urls?.length ?? 0) > 0;

  function recallImages() {
    if (!message.result || !onShowImages) return;
    const variants = [...new Set((message.result.matches || []).map(m => m.model_variant).filter(Boolean))] as string[];
    onShowImages({
      urls: message.result.image_urls || [],
      label: buildImagePanelLabel(message.result, variants),
      status: message.result.status,
    });
  }

  return (
    <div className="space-y-3">
      <p className="text-[15px] leading-6 text-[var(--riso-text)]">{message.text}</p>

      {style && (
        <div className="flex items-center gap-2 flex-wrap">
          <div className={`kinetic-in inline-flex items-center gap-1.5 font-data text-[10px] tracking-widest px-2.5 py-1 border ${style.color}`}>
            {style.icon === 'warn' && <AlertTriangle size={11} />}
            {style.icon === 'help' && <HelpCircle size={11} />}
            {style.badge}
          </div>
          {hasImages && (
            <button
              onClick={recallImages}
              className="inline-flex items-center gap-1.5 font-data text-[10px] tracking-widest px-2.5 py-1 border border-stone-600 text-stone-400 hover:border-[var(--riso-yellow)] hover:text-[var(--riso-yellow)] transition-colors"
            >
              <ImageIcon size={11} />
              VIEW PAGE
            </button>
          )}
        </div>
      )}

      {message.result?.candidates && message.result.candidates.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {message.result.candidates.map((c, i) => (
            <button
              key={c}
              onClick={() => onSelectCandidate?.(c)}
              className="font-data text-xs px-2.5 py-1 border border-stone-600 text-stone-300 hover:border-[var(--riso-yellow)] hover:text-[var(--riso-yellow)] transition-colors"
            >
              {message.result?.candidateLabels?.[i] ?? c}
            </button>
          ))}
        </div>
      )}

      {message.result?.matches && message.result.matches.length > 1 && message.result.matches.length <= 6
        && message.result.status !== 'full_price_grid' && message.result.status !== 'multi_product' && (
        <div className="border-2 border-[var(--riso-line)] overflow-hidden">
          <table className="w-full font-data text-xs">
            <thead className="bg-[var(--riso-surface)] text-stone-400">
              <tr>
                {/* Only shown when at least one row actually has a
                    model_variant -- many products have no variant
                    dimension at all, and a column of nothing but "—"
                    would just be noise (same "don't show an inapplicable
                    dimension" precedent as tierColumnHeader's own "—"
                    fallback just to the right of this). Needed because
                    this table is the ONLY place a multi-row response
                    shows size/tier/code/price side by side without also
                    showing WHICH named variant each row belongs to --
                    the full_price_grid/multi_product path already groups
                    by variant via ProductSection headers, but this
                    smaller multiple_options table had no equivalent,
                    even though model_variant is already resolved
                    correctly on every row (confirmed live: "3-er sofa"/
                    "3-er maxi sofa"/"3-er extra sofa"/"3-er central
                    element" rows were indistinguishable from each other
                    beyond their raw size string). */}
                {message.result.matches.some(r => r.model_variant) && (
                  <th className="text-left px-3 py-1.5 font-medium">VARIANT</th>
                )}
                <th className="text-left px-3 py-1.5 font-medium">SIZE</th>
                <th className="text-left px-3 py-1.5 font-medium">{tierColumnHeader(message.result.matches)}</th>
                <th className="text-left px-3 py-1.5 font-medium">CODE</th>
                <th className="text-right px-3 py-1.5 font-medium">PRICE</th>
              </tr>
            </thead>
            <tbody>
              {message.result.matches.map((r, i) => (
                <tr key={i} className="border-t border-[var(--riso-line)]">
                  {message.result!.matches!.some(m => m.model_variant) && (
                    <td className="px-3 py-1.5 text-stone-300">{r.model_variant || '—'}</td>
                  )}
                  <td className="px-3 py-1.5 text-stone-300">{r.size || '—'}</td>
                  <td className="px-3 py-1.5 text-stone-300">{r.fabric_tier || '—'}</td>
                  <td className="px-3 py-1.5 text-stone-500">{r.code || '—'}</td>
                  <td className="px-3 py-1.5 text-right font-semibold text-[var(--riso-yellow)]">€{r.price_eur}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {(message.result?.status === 'full_price_grid' || message.result?.status === 'multi_product') && message.result.matches && (
        <PriceGrid rows={message.result.matches} />
      )}
    </div>
  );
}

// Canonical top-to-bottom order tiers appear in across the catalog --
// used so the grid's row order matches the real PDF page instead of
// whatever order rows happened to come back in.
const TIER_ORDER = [
  'b e tcl', 'c', 'd', 'e', 'd, e', 'plus',
  'extra', 'extra luxury fabric', 'luxury leather',
  'super', 'super nuvola fabric', 'nuvola leather',
];

function tierSortKey(tier: string | null): number {
  const idx = TIER_ORDER.indexOf((tier || '').toLowerCase());
  return idx === -1 ? TIER_ORDER.length : idx;
}

function sizeSortKey(size: string | null): number {
  const m = (size || '').match(/^(\d+)/);
  return m ? parseInt(m[1], 10) : Number.MAX_SAFE_INTEGER;
}

/** Pivots flat {size, fabric_tier, price} rows into a proper grid table --
 * fabric tiers down the left, sizes across the top, price in each cell --
 * matching the layout of the real catalog page. Groups by model_variant
 * first (e.g. "Cameo Maison h.7" vs "h.29") so different structural
 * variants never get mixed into one confusing grid.
 *
 * When a result spans many variants (e.g. Iorca's 5 different "Soluzione"
 * configurations), showing every grid at once is overwhelming. Past a
 * threshold, the extra ones collapse behind a fade with a "see more"
 * toggle -- the content is already rendered (just visually clipped via
 * max-height), so expanding is instant with no layout jump or reflow. */
/** Top-level entry point: groups by PRODUCT first when a result spans
 * multiple different products (e.g. "give me Pandora and Selene"),
 * showing each product's own name as a clear section header, then
 * delegates to the existing single-product grid renderer unchanged for
 * each one. For the normal single-product case this is a pass-through --
 * nothing about that behavior changes. */
/** One product's own section: name header + its price grid(s) -- factored
 * out of PriceGrid so both the always-visible first batch and the
 * collapsed rest render it identically. */
function ProductSection({ product, rows }: { product: string; rows: PriceRow[] }) {
  return (
    <div className="space-y-2">
      <div className="font-display font-bold text-sm text-[var(--riso-text)] border-b-2 border-[var(--riso-line)] pb-1">
        {product}
      </div>
      <PriceGridSingleProduct rows={rows} />
    </div>
  );
}

function PriceGrid({ rows }: { rows: PriceRow[] }) {
  const [expanded, setExpanded] = useState(false);
  const products = [...new Set(rows.map(r => r.product_name))];

  if (products.length <= 1) {
    return <PriceGridSingleProduct rows={rows} />;
  }

  // Same collapse/expand treatment as PriceGridSingleProduct just below
  // (fade + "see more" toggle, already-rendered content just visually
  // clipped via max-height so expanding is instant) -- applied one level
  // up, to the PRODUCT grouping this component adds on top of that one.
  // Needed because that existing mechanism only ever collapsed VARIANTS
  // within a single product (e.g. Cameo Maison's "h.7" vs "h.29"); a
  // multi_product result spanning many distinct products (confirmed
  // real: "give me all Emma Cross prices", 20 rendered product sections)
  // had no equivalent at this level, forcing a long scroll through every
  // one of them at once regardless of count.
  const VISIBLE_PRODUCTS = 4;
  const isBulky = products.length > VISIBLE_PRODUCTS;
  const hiddenCount = products.length - VISIBLE_PRODUCTS;
  const visibleProducts = isBulky ? products.slice(0, VISIBLE_PRODUCTS) : products;
  const restProducts = isBulky ? products.slice(VISIBLE_PRODUCTS) : [];

  return (
    <div className="space-y-6">
      {visibleProducts.map(product => (
        <ProductSection key={product} product={product} rows={rows.filter(r => r.product_name === product)} />
      ))}

      {isBulky && (
        <div className="relative">
          <div
            className="overflow-hidden transition-[max-height] duration-300 ease-out"
            style={{ maxHeight: expanded ? '1000000px' : '0px' }}
          >
            <div className="space-y-6 pt-1">
              {restProducts.map(product => (
                <ProductSection key={product} product={product} rows={rows.filter(r => r.product_name === product)} />
              ))}
            </div>
          </div>

          {!expanded && (
            <div className="relative -mt-4 pt-4 bg-gradient-to-t from-[var(--riso-bg)] via-[var(--riso-bg)]/90 to-transparent" />
          )}

          <button
            onClick={() => setExpanded(e => !e)}
            className="w-full border-2 border-[var(--riso-line)] hover:border-[var(--riso-pink)] py-2 font-data text-[11px] tracking-widest text-stone-300 hover:text-[var(--riso-pink)] transition-colors"
          >
            {expanded ? 'SHOW LESS ▲' : `SEE ${hiddenCount} MORE PRODUCT${hiddenCount === 1 ? '' : 'S'} ▼`}
          </button>
        </div>
      )}
    </div>
  );
}

// VariantGroup + buildVariantGroups now live in ./priceGridGrouping
// (imported above) -- see that module's own comment for the grouping
// rationale (Esse/Gamma's variant_context split) and the size/code
// column-collision fix (Venere/Soffio fisso and 87+ other products
// found sharing a printed size across 2 real different products).

function VariantTable({ group, showHeader }: { group: VariantGroup; showHeader: boolean }) {
  const tiers = [...new Set(group.rows.map(r => r.fabric_tier))].sort(
    (a, b) => tierSortKey(a) - tierSortKey(b)
  );
  const columns = buildSizeColumns(group.rows).sort(
    (a, b) => sizeSortKey(a.size) - sizeSortKey(b.size)
  );

  return (
    <div className="border-2 border-[var(--riso-line)] overflow-hidden">
      {showHeader && (
        <div className="px-3 py-1.5 bg-[var(--riso-pink)] text-[#131217] font-display font-bold text-xs">
          {group.header}
        </div>
      )}
      <div className="overflow-x-auto">
        <table className="w-full font-data text-xs">
          <thead className="bg-[var(--riso-surface)] text-stone-400">
            <tr>
              <th className="text-left px-3 py-1.5 font-medium sticky left-0 bg-[var(--riso-surface)]">{tierColumnHeader(group.rows)}</th>
              {columns.map(c => (
                <th key={c.key} className="text-right px-3 py-1.5 font-medium whitespace-nowrap">
                  {c.label || '—'}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {tiers.map(tier => (
              <tr key={tier || 'na'} className="border-t border-[var(--riso-line)]">
                <td className="px-3 py-1.5 font-medium text-stone-300 sticky left-0 bg-[var(--riso-bg)] whitespace-nowrap">
                  {tier || '—'}
                </td>
                {columns.map(c => {
                  const r = findCell(group.rows, tier, c.key, columns);
                  return (
                    <td key={c.key} className="px-3 py-1.5 text-right whitespace-nowrap">
                      {r ? (
                        <div className="flex flex-col items-end leading-tight">
                          <span className="text-[var(--riso-yellow)]">€{r.price_eur}</span>
                          {/* Shown per-cell, not once per group -- confirmed
                              real on Primo that code can vary by SIZE within
                              an otherwise-identical tier/variant group
                              (AA701 @ 238.5 vs AA801 @ 257.7), so a single
                              group-level code would silently show the wrong
                              order code for every other cell. */}
                          {r.code && <span className="text-stone-500 text-[10px]">{r.code}</span>}
                        </div>
                      ) : '—'}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function PriceGridSingleProduct({ rows }: { rows: PriceRow[] }) {
  const [expanded, setExpanded] = useState(false);
  const groups = buildVariantGroups(rows);
  const showHeader = groups.length > 1;
  const VISIBLE_COUNT = 2;
  const isBulky = groups.length > VISIBLE_COUNT;
  const hiddenCount = groups.length - VISIBLE_COUNT;
  const visibleGroups = groups.slice(0, VISIBLE_COUNT);
  const restGroups = groups.slice(VISIBLE_COUNT);

  return (
    <div className="space-y-4">
      {visibleGroups.map(group => (
        <VariantTable key={group.key} group={group} showHeader={showHeader} />
      ))}

      {isBulky && (
        <div className="relative">
          <div
            className="overflow-hidden transition-[max-height] duration-300 ease-out"
            style={{ maxHeight: expanded ? '10000px' : '0px' }}
          >
            <div className="space-y-4 pt-1">
              {restGroups.map(group => (
                <VariantTable key={group.key} group={group} showHeader={showHeader} />
              ))}
            </div>
          </div>

          {!expanded && (
            <div className="relative -mt-4 pt-4 bg-gradient-to-t from-[var(--riso-bg)] via-[var(--riso-bg)]/90 to-transparent">
              <div className="border border-[var(--riso-yellow)] px-3 py-2 mb-2">
                <p className="font-data text-[11px] leading-relaxed text-[var(--riso-yellow)]">
                  This product has multiple configurations, sizes, and categories.
                  Check the <span className="font-semibold">source page image</span> on
                  the right for the full visual reference.
                </p>
              </div>
            </div>
          )}

          <button
            onClick={() => setExpanded(e => !e)}
            className="w-full border-2 border-[var(--riso-line)] hover:border-[var(--riso-pink)] py-2 font-data text-[11px] tracking-widest text-stone-300 hover:text-[var(--riso-pink)] transition-colors"
          >
            {expanded ? 'SHOW LESS ▲' : `SEE ${hiddenCount} MORE CATEGOR${hiddenCount === 1 ? 'Y' : 'IES'} ▼`}
          </button>
        </div>
      )}
    </div>
  );
}