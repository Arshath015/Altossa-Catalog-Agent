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
  { name: 'Ditre Italia', status: 'soon', productCount: 0 },
  { name: 'Pianca', status: 'soon', productCount: 0 },
];

export default function BrandSidebar({
  selected,
  onSelect,
}: {
  selected: string;
  onSelect: (brand: string) => void;
}) {
  return (
    <div className="w-56 shrink-0 h-full border-r-2 border-[var(--riso-line)] flex flex-col bg-[var(--riso-surface)]">
      <div className="px-5 py-6 border-b-2 border-[var(--riso-line)]">
        <div className="font-display font-bold text-lg tracking-tight text-[var(--riso-text)] leading-none">
          ALTOSSA
        </div>
        <div className="font-data text-[10px] tracking-widest text-[var(--riso-pink)] mt-1 uppercase">
          Catalog Agent
        </div>
      </div>

      <nav className="flex-1 py-4 px-3 space-y-1.5">
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
              {b.name}
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
  );
}