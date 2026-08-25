import { useState } from 'react';
import { ImageOff, ChevronLeft, ChevronRight } from 'lucide-react';
import ImageLightbox from './ImageLightbox';
import { useResizableWidth } from './useResizableWidth';

export interface ImagePanelData {
  urls: string[];
  label: string | null;
  status: string | null;
}

const STATUS_LABEL: Record<string, string> = {
  ok: 'VERIFIED',
  full_price_grid: 'FULL LIST',
  multiple_options: 'MULTIPLE',
  ambiguous_price: 'CONFIRM',
  no_matching_variant: 'NOT FOUND',
  no_price_data: 'NO DATA',
  no_product_match: 'NOT FOUND',
  clarify_product: 'CHOOSE ONE',
};

const DEFAULT_WIDTH = 380; // matches the old fixed w-[380px]
const MIN_WIDTH = 260;
const MAX_WIDTH = 640;

export default function ImagePanel({ data }: { data: ImagePanelData | null }) {
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null);
  const [collapsed, setCollapsed] = useState(false);
  const { width, isResizing, onMouseDown } = useResizableWidth(DEFAULT_WIDTH, MIN_WIDTH, MAX_WIDTH, 'left');

  return (
    // Same collapse pattern as BrandSidebar (see its own comment for why
    // the toggle button lives OUTSIDE the overflow-hidden width-animated
    // div), mirrored: this panel is on the right, so the button floats
    // past its LEFT edge instead, and the chevrons point the opposite way.
    <div className="relative h-full shrink-0 flex">
      {/* Drag handle -- mirrors BrandSidebar's own (see its comment):
          same idea, but on this panel's LEFT border instead, and edge
          direction 'left' passed to the shared hook above so dragging
          left (toward screen center) widens this panel instead of
          shrinking it. */}
      {!collapsed && (
        <div
          onMouseDown={onMouseDown}
          className="absolute top-0 left-0 h-full w-1.5 -ml-0.5 cursor-ew-resize z-[5] hover:bg-[var(--riso-yellow)]/30 active:bg-[var(--riso-yellow)]/50"
        />
      )}

      <button
        type="button"
        onClick={() => setCollapsed(c => !c)}
        aria-label={collapsed ? 'Expand source page panel' : 'Collapse source page panel'}
        title={collapsed ? 'Expand panel' : 'Collapse panel'}
        className="absolute top-6 -left-3 z-10 w-6 h-6 flex items-center justify-center rounded-full border-2 border-[var(--riso-line)] bg-[var(--riso-surface)] text-stone-400 hover:border-[var(--riso-yellow)] hover:text-[var(--riso-yellow)] transition-colors"
      >
        {collapsed ? <ChevronLeft size={13} /> : <ChevronRight size={13} />}
      </button>

      <div
        className={`h-full border-l-2 border-[var(--riso-line)] bg-[var(--riso-surface)] overflow-hidden ${collapsed ? 'border-l-0' : ''} ${isResizing ? '' : 'transition-[width] duration-200 ease-out'}`}
        style={{ width: collapsed ? 0 : width }}
      >
        <div style={{ width }} className="h-full flex flex-col">
          <div className="px-5 py-6 border-b-2 border-[var(--riso-line)]">
            <div className="font-display font-bold text-sm tracking-tight text-[var(--riso-text)]">
              SOURCE PAGE
            </div>
            <div className="font-data text-[10px] text-stone-500 mt-1 uppercase tracking-widest">
              Direct from price list
            </div>
          </div>

          <div className="flex-1 overflow-y-auto px-5 py-6 space-y-6">
            {!data || data.urls.length === 0 ? (
              <div className="flex flex-col items-center justify-center h-full text-center gap-3 text-stone-600">
                <ImageOff size={28} strokeWidth={1.5} />
                <p className="font-data text-xs uppercase tracking-widest">
                  No page to show yet
                </p>
              </div>
            ) : (
              <>
                {data.label && (
                  <div className="font-display font-bold text-[var(--riso-yellow)] text-sm leading-snug">
                    {data.label}
                  </div>
                )}
                {data.status && STATUS_LABEL[data.status] && (
                  <div className="inline-block font-data text-[10px] tracking-widest px-2 py-1 border border-[var(--riso-pink)] text-[var(--riso-pink)]">
                    {STATUS_LABEL[data.status]}
                  </div>
                )}
                {data.urls.map((url, i) => (
                  <RisoFramedImage key={url} url={url} onClick={() => setLightboxIndex(i)} />
                ))}
              </>
            )}
          </div>
        </div>
      </div>

      {data && lightboxIndex !== null && (
        <ImageLightbox
          urls={data.urls}
          index={lightboxIndex}
          onClose={() => setLightboxIndex(null)}
          onNavigate={setLightboxIndex}
        />
      )}
    </div>
  );
}

/** The signature visual moment: the real catalog screenshot rendered with
 * a Risograph "double-hit" duotone overlay (pink + yellow, multiply
 * blended) and a deliberately offset double border, mimicking ink
 * slightly out of registration on a real riso print. Opens the same-tab
 * ImageLightbox on click instead of the old `target="_blank"` new-tab
 * behavior. */
function RisoFramedImage({ url, onClick }: { url: string; onClick: () => void }) {
  return (
    <button type="button" onClick={onClick} className="block w-full text-left relative group">
      <div className="absolute -inset-0 translate-x-[3px] translate-y-[3px] border-2 border-[var(--riso-pink)] pointer-events-none" />
      <div className="absolute -inset-0 -translate-x-[3px] -translate-y-[3px] border-2 border-[var(--riso-yellow)] pointer-events-none" />
      <div className="relative border-2 border-[var(--riso-text)] bg-white overflow-hidden">
        <img
          src={url}
          alt="Catalog page"
          className="w-full h-auto block grayscale contrast-125 group-hover:grayscale-0 transition-all duration-300"
          onError={(e) => { (e.target as HTMLImageElement).style.display = 'none'; }}
        />
        <div
          className="absolute inset-0 mix-blend-multiply opacity-40 group-hover:opacity-0 transition-opacity duration-300"
          style={{ background: 'linear-gradient(135deg, var(--riso-pink), var(--riso-yellow))' }}
        />
      </div>
    </button>
  );
}