import { useState, useRef, useEffect } from 'react';
import { Send, AlertTriangle, HelpCircle, ImageIcon } from 'lucide-react';
import type { ImagePanelData } from '../ImagePanel';

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

interface PriceRow {
  product_name: string;
  model_variant: string | null;
  size: string | null;
  fabric_tier: string | null;
  /** Real source-PDF category word for this row's fabric_tier value
   * ("Base", "Top", "Rivestimento", "Struttura"...) -- null when not
   * confidently recoverable or when the product has no fabric_tier
   * dimension at all. Falls back to "FABRIC" for display in that case. */
  tier_label: string | null;
  code: string | null;
  price_eur: string;
  ambiguous: boolean;
}

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
  matches?: PriceRow[];
  image_urls?: string[];
}

interface ChatMessage {
  role: 'user' | 'assistant';
  text: string;
  result?: ChatResult;
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

export default function CatalogChatWidget({
  brand,
  onLatestImages,
}: {
  brand: string;
  onLatestImages?: (data: ImagePanelData) => void;
}) {
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      role: 'assistant',
      text: `Ask me about any ${brand} product — e.g. "how much is the Ceylon in 160x200, Extra fabric?"`,
    },
  ]);
  const [loading, setLoading] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const lastProductRef = useRef<string | null>(null);
  const lastModelVariantRef = useRef<string | null>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, loading]);

  async function sendMessage(text: string) {
    if (!text.trim() || loading) return;
    setMessages(prev => [...prev, { role: 'user', text }]);
    setLoading(true);
    try {
      const res = await fetch('/api/catalog/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          brand,
          message: text,
          history: messages.slice(-4).map(m => ({ role: m.role, text: m.text })),
          lastProduct: lastProductRef.current,
          lastModelVariant: lastModelVariantRef.current,
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
      if (result.product_name) {
        lastProductRef.current = result.product_name;
      } else {
        lastProductRef.current = null;
      }
      const variantsInResult = [...new Set((result.matches || []).map(m => m.model_variant).filter(Boolean))] as string[];
      if (variantsInResult.length === 1) {
        lastModelVariantRef.current = variantsInResult[0];
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
        lastModelVariantRef.current = codes.size === 1 ? variantsInResult[0] : null;
      } else {
        lastModelVariantRef.current = null;
      }

      onLatestImages?.({
        urls: result.image_urls || [],
        label: variantsInResult.length === 1 ? variantsInResult[0] : (result.product_name || null),
        status: result.status,
      });

      setMessages(prev => [...prev, { role: 'assistant', text: result.message, result }]);
    } catch (err) {
      setMessages(prev => [...prev, {
        role: 'assistant',
        text: "Something went wrong reaching the catalog. Please try again.",
      }]);
    } finally {
      setLoading(false);
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
          <MessageBubble key={i} message={m} onShowImages={onLatestImages} />
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

function MessageBubble({ message, onShowImages }: { message: ChatMessage; onShowImages?: (data: ImagePanelData) => void }) {
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
    const variants = new Set((message.result.matches || []).map(m => m.model_variant).filter(Boolean));
    onShowImages({
      urls: message.result.image_urls || [],
      label: variants.size === 1 ? [...variants][0] as string : (message.result.product_name || null),
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
          {message.result.candidates.map((c) => (
            <span key={c} className="font-data text-xs px-2.5 py-1 border border-stone-600 text-stone-300">
              {c}
            </span>
          ))}
        </div>
      )}

      {message.result?.matches && message.result.matches.length > 1 && message.result.matches.length <= 6
        && message.result.status !== 'full_price_grid' && message.result.status !== 'multi_product' && (
        <div className="border-2 border-[var(--riso-line)] overflow-hidden">
          <table className="w-full font-data text-xs">
            <thead className="bg-[var(--riso-surface)] text-stone-400">
              <tr>
                <th className="text-left px-3 py-1.5 font-medium">SIZE</th>
                <th className="text-left px-3 py-1.5 font-medium">{tierColumnHeader(message.result.matches)}</th>
                <th className="text-left px-3 py-1.5 font-medium">CODE</th>
                <th className="text-right px-3 py-1.5 font-medium">PRICE</th>
              </tr>
            </thead>
            <tbody>
              {message.result.matches.map((r, i) => (
                <tr key={i} className="border-t border-[var(--riso-line)]">
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
function PriceGrid({ rows }: { rows: PriceRow[] }) {
  const products = [...new Set(rows.map(r => r.product_name))];

  if (products.length <= 1) {
    return <PriceGridSingleProduct rows={rows} />;
  }

  return (
    <div className="space-y-6">
      {products.map(product => (
        <div key={product} className="space-y-2">
          <div className="font-display font-bold text-sm text-[var(--riso-text)] border-b-2 border-[var(--riso-line)] pb-1">
            {product}
          </div>
          <PriceGridSingleProduct rows={rows.filter(r => r.product_name === product)} />
        </div>
      ))}
    </div>
  );
}

function PriceGridSingleProduct({ rows }: { rows: PriceRow[] }) {
  const [expanded, setExpanded] = useState(false);
  const variants = [...new Set(rows.map(r => r.model_variant || '—'))];
  const VISIBLE_COUNT = 2;
  const isBulky = variants.length > VISIBLE_COUNT;
  const hiddenCount = variants.length - VISIBLE_COUNT;

  return (
    <div className="space-y-4">
      {variants.map((variant, i) => {
        const variantRows = rows.filter(r => (r.model_variant || '—') === variant);
        const tiers = [...new Set(variantRows.map(r => r.fabric_tier))].sort(
          (a, b) => tierSortKey(a) - tierSortKey(b)
        );
        const sizes = [...new Set(variantRows.map(r => r.size))].sort(
          (a, b) => sizeSortKey(a) - sizeSortKey(b)
        );
        const cell = (tier: string | null, size: string | null) =>
          variantRows.find(r => r.fabric_tier === tier && r.size === size);

        const table = (
          <div key={variant} className="border-2 border-[var(--riso-line)] overflow-hidden">
            {variants.length > 1 && (
              <div className="px-3 py-1.5 bg-[var(--riso-pink)] text-[#131217] font-display font-bold text-xs">
                {variant}
              </div>
            )}
            <div className="overflow-x-auto">
              <table className="w-full font-data text-xs">
                <thead className="bg-[var(--riso-surface)] text-stone-400">
                  <tr>
                    <th className="text-left px-3 py-1.5 font-medium sticky left-0 bg-[var(--riso-surface)]">{tierColumnHeader(variantRows)}</th>
                    {sizes.map(s => (
                      <th key={s || 'na'} className="text-right px-3 py-1.5 font-medium whitespace-nowrap">
                        {s || '—'}
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
                      {sizes.map(s => {
                        const r = cell(tier, s);
                        return (
                          <td key={s || 'na'} className="px-3 py-1.5 text-right text-[var(--riso-yellow)] whitespace-nowrap">
                            {r ? `€${r.price_eur}` : '—'}
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

        // First VISIBLE_COUNT tables always render normally. Anything
        // past that gets wrapped in the collapsible region below instead.
        return i < VISIBLE_COUNT ? table : null;
      })}

      {isBulky && (
        <div className="relative">
          <div
            className="overflow-hidden transition-[max-height] duration-300 ease-out"
            style={{ maxHeight: expanded ? '10000px' : '0px' }}
          >
            <div className="space-y-4 pt-1">
              {variants.slice(VISIBLE_COUNT).map(variant => {
                const variantRows = rows.filter(r => (r.model_variant || '—') === variant);
                const tiers = [...new Set(variantRows.map(r => r.fabric_tier))].sort(
                  (a, b) => tierSortKey(a) - tierSortKey(b)
                );
                const sizes = [...new Set(variantRows.map(r => r.size))].sort(
                  (a, b) => sizeSortKey(a) - sizeSortKey(b)
                );
                const cell = (tier: string | null, size: string | null) =>
                  variantRows.find(r => r.fabric_tier === tier && r.size === size);
                return (
                  <div key={variant} className="border-2 border-[var(--riso-line)] overflow-hidden">
                    <div className="px-3 py-1.5 bg-[var(--riso-pink)] text-[#131217] font-display font-bold text-xs">
                      {variant}
                    </div>
                    <div className="overflow-x-auto">
                      <table className="w-full font-data text-xs">
                        <thead className="bg-[var(--riso-surface)] text-stone-400">
                          <tr>
                            <th className="text-left px-3 py-1.5 font-medium sticky left-0 bg-[var(--riso-surface)]">{tierColumnHeader(variantRows)}</th>
                            {sizes.map(s => (
                              <th key={s || 'na'} className="text-right px-3 py-1.5 font-medium whitespace-nowrap">
                                {s || '—'}
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
                              {sizes.map(s => {
                                const r = cell(tier, s);
                                return (
                                  <td key={s || 'na'} className="px-3 py-1.5 text-right text-[var(--riso-yellow)] whitespace-nowrap">
                                    {r ? `€${r.price_eur}` : '—'}
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
              })}
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