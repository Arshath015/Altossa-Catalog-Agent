import { useState } from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { useResizableWidth } from './useResizableWidth';

interface Brand {
  name: string;
  status: 'ready' | 'soon';
  productCount: number;
}

const BRANDS: Brand[] = [
  { name: 'Bolzan', status: 'ready', productCount: 101 },
  { name: 'Cattelan Italia', status: 'ready', productCount: 533 },
  { name: 'Bonaldo', status: 'ready', productCount: 300 },
  { name: 'Varaschini', status: 'ready', productCount: 1294 },
  { name: 'Ditre Italia', status: 'ready', productCount: 197 },
  { name: 'Pianca', status: 'ready', productCount: 499 },
  { name: 'Bodema', status: 'soon', productCount: 0 },
  { name: 'Bontempi', status: 'soon', productCount: 0 },
  { name: 'Calligaris', status: 'soon', productCount: 0 },
  { name: 'Desiree', status: 'soon', productCount: 0 },
  { name: 'Eforma', status: 'soon', productCount: 0 },
  { name: 'Italia Lounge', status: 'soon', productCount: 0 },
  { name: 'Kartell', status: 'soon', productCount: 0 },
  { name: 'Magis', status: 'soon', productCount: 0 },
  { name: 'Midji', status: 'soon', productCount: 0 },
  { name: 'Miniforms', status: 'soon', productCount: 0 },
  { name: 'Mogg', status: 'soon', productCount: 0 },
  { name: 'Nicoline Salotti', status: 'soon', productCount: 0 },
  { name: 'Nube Italia', status: 'soon', productCount: 0 },
  { name: 'Pedrali', status: 'soon', productCount: 0 },
  { name: 'Riflessi', status: 'soon', productCount: 0 },
  { name: 'Saba', status: 'soon', productCount: 0 },
  { name: 'Tacchini', status: 'soon', productCount: 0 },
  { name: 'Twils', status: 'soon', productCount: 0 },
];

const DEFAULT_WIDTH = 224; // matches the old fixed w-56
const MIN_WIDTH = 160;
const MAX_WIDTH = 420;

export default function BrandSidebar({
  selected,
  onSelect,
}: {
  selected: string;
  onSelect: (brand: string) => void;
}) {
  const [collapsed, setCollapsed] = useState(false);
  const { width, isResizing, onMouseDown } = useResizableWidth(DEFAULT_WIDTH, MIN_WIDTH, MAX_WIDTH, 'right');

  return (
    // Outer wrapper deliberately has NO overflow-hidden of its own -- the
    // toggle button below is positioned to float just past the panel's
    // own right edge (macOS-sidebar-style), and needs to stay visible
    // even while the panel is collapsed to width 0. Only the INNER div
    // (immediately below) owns overflow-hidden, so it's the one that
    // clips content during the width transition without also clipping
    // the button that lives outside it.
    <div className="relative h-full shrink-0 flex">
      <div
        className={`h-full border-r-2 border-[var(--riso-line)] bg-[var(--riso-surface)] overflow-hidden ${collapsed ? 'border-r-0' : ''} ${isResizing ? '' : 'transition-[width] duration-200 ease-out'}`}
        style={{ width: collapsed ? 0 : width }}
      >
        {/* Fixed-width inner content -- keeps text/layout stable while the
            OUTER width animates, instead of reflowing/wrapping mid-transition. */}
        <div style={{ width }} className="h-full flex flex-col">
          <div className="px-5 py-6 border-b-2 border-[var(--riso-line)]">
            <div className="font-display font-bold text-lg tracking-tight text-[var(--riso-text)] leading-none">
              ALTOSSA
            </div>
            <div className="font-data text-[10px] tracking-widest text-[var(--riso-pink)] mt-1 uppercase">
              Catalog Agent
            </div>
          </div>

          <nav className="flex-1 min-h-0 py-4 px-3 space-y-1.5 overflow-y-auto">
            {BRANDS.map((b) => {
              const isSelected = b.name === selected;
              const isReady = b.status === 'ready';
              return (
                <button
                  key={b.name}
                  disabled={!isReady}
                  onClick={() => isReady && onSelect(b.name)}
                  className={`relative w-full text-left px-3.5 py-3 border-2 font-display font-bold text-sm transition-colors
                    ${isSelected
                      ? 'bg-[var(--riso-pink)] border-[var(--riso-pink)] text-[#131217]'
                      : isReady
                        ? 'border-[var(--riso-line)] text-[var(--riso-text)] hover:border-[var(--riso-yellow)] hover:text-[var(--riso-yellow)]'
                        : 'border-[var(--riso-line)] text-stone-500 cursor-not-allowed opacity-60'
                    }`}
                >
                  <span className="block truncate">{b.name}</span>
                  {!isReady && (
                    <span className="absolute -top-2 -right-2 rotate-[-8deg] bg-[var(--riso-yellow)] text-[#131217] font-data text-[9px] font-semibold px-1.5 py-0.5 border border-[#131217] tracking-wide">
                      SOON
                    </span>
                  )}
                </button>
              );
            })}
          </nav>

          <div className="px-5 py-4 border-t-2 border-[var(--riso-line)] font-data text-[10px] text-stone-500 tracking-wide uppercase">
            {BRANDS.find((b) => b.name === selected)?.productCount ?? 0} products &middot; verified
          </div>
        </div>
      </div>

      {/* Drag handle -- a thin invisible strip centered on the panel's own
          right border, positioned via `right-0` on this (absolutely
          positioned) element against the OUTER wrapper, whose own
          intrinsic width already tracks the inner div's live width (the
          outer wrapper has no explicit width of its own -- see its own
          comment above), so this stays glued to the true edge whether
          collapsed, at its default width, or mid-drag. Hidden while
          collapsed -- there's no border to grab when the panel is gone. */}
      {!collapsed && (
        <div
          onMouseDown={onMouseDown}
          className="absolute top-0 right-0 h-full w-1.5 -mr-0.5 cursor-ew-resize z-[5] hover:bg-[var(--riso-yellow)]/30 active:bg-[var(--riso-yellow)]/50"
        />
      )}

      <button
        type="button"
        onClick={() => setCollapsed(c => !c)}
        aria-label={collapsed ? 'Expand brand sidebar' : 'Collapse brand sidebar'}
        title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        className="absolute top-6 -right-3 z-10 w-6 h-6 flex items-center justify-center rounded-full border-2 border-[var(--riso-line)] bg-[var(--riso-surface)] text-stone-400 hover:border-[var(--riso-yellow)] hover:text-[var(--riso-yellow)] transition-colors"
      >
        {collapsed ? <ChevronRight size={13} /> : <ChevronLeft size={13} />}
      </button>
    </div>
  );
}