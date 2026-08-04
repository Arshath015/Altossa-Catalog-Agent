import { ImageOff } from 'lucide-react';

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

export default function ImagePanel({ data }: { data: ImagePanelData | null }) {
  return (
    <div className="w-[380px] shrink-0 h-full border-l-2 border-[var(--riso-line)] bg-[var(--riso-surface)] flex flex-col">
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
            {data.urls.map((url) => (
              <RisoFramedImage key={url} url={url} />
            ))}
          </>
        )}
      </div>
    </div>
  );
}

/** The signature visual moment: the real catalog screenshot rendered with
 * a Risograph "double-hit" duotone overlay (pink + yellow, multiply
 * blended) and a deliberately offset double border, mimicking ink
 * slightly out of registration on a real riso print. */
function RisoFramedImage({ url }: { url: string }) {
  return (
    <a href={url} target="_blank" rel="noopener noreferrer" className="block relative group">
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
    </a>
  );
}