import { useCallback, useEffect, useRef, useState } from 'react';
import { X, ChevronLeft, ChevronRight, Download } from 'lucide-react';

// Fixed zoom level for the click-to-zoom interaction (not continuous --
// scroll-wheel zoom was tried first and reported unusable; this replaces
// it entirely with a click/click-again + drag-to-pan model instead,
// matching a standard image-viewer/PDF-lightbox pattern).
const ZOOM_LEVEL = 2.5;
// Minimum pointer movement (px) before a mousedown+mouseup counts as a
// DRAG rather than a CLICK -- without this, ordinary hand jitter during
// a click would accidentally nudge the pan and/or suppress the zoom
// toggle.
const DRAG_THRESHOLD = 4;

/** Full-screen, same-tab viewer for a source-page image -- opened from
 * ImagePanel's thumbnail list instead of the old `target="_blank"` new-
 * tab behavior. Prev/next cycles (wrap-around) through the SAME result's
 * own `urls` array -- a product spanning multiple catalog pages already
 * returns multiple URLs in one response, so no new data plumbing is
 * needed, just navigation over what's already there.
 *
 * Zoom/pan: click zooms to ZOOM_LEVEL centered on the image; click again
 * (a genuine click, not a drag) zooms back to fit; while zoomed, click-
 * and-drag pans, clamped so the image can't be dragged past its own
 * edges into empty space. Panning is done via a CSS transform (not
 * native scroll) -- clampPan measures the container's and image's real
 * rendered boxes each drag so the clamp stays correct regardless of the
 * actual page-image aspect ratio (catalog pages vary in shape). */
export default function ImageLightbox({
  urls,
  index,
  onClose,
  onNavigate,
}: {
  urls: string[];
  index: number;
  onClose: () => void;
  onNavigate: (newIndex: number) => void;
}) {
  const hasMultiple = urls.length > 1;
  const [zoomed, setZoomed] = useState(false);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [isDragging, setIsDragging] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  const dragState = useRef({ startX: 0, startY: 0, startPanX: 0, startPanY: 0, moved: false, dragging: false });

  useEffect(() => {
    setZoomed(false);
    setPan({ x: 0, y: 0 });
  }, [index]);

  const goPrev = useCallback(() => {
    onNavigate((index - 1 + urls.length) % urls.length);
  }, [index, urls.length, onNavigate]);

  const goNext = useCallback(() => {
    onNavigate((index + 1) % urls.length);
  }, [index, urls.length, onNavigate]);

  useEffect(() => {
    function handleKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
      else if (e.key === 'ArrowLeft' && hasMultiple) goPrev();
      else if (e.key === 'ArrowRight' && hasMultiple) goNext();
    }
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [onClose, goPrev, goNext, hasMultiple]);

  const clampPan = useCallback((x: number, y: number) => {
    const container = containerRef.current;
    const img = imgRef.current;
    if (!container || !img || !img.naturalWidth) return { x, y };
    const rect = container.getBoundingClientRect();
    // The image's own rendered box at zoom=1 (how `object-contain` fits
    // it into the container) -- needed to know how much of the SCALED
    // image actually overhangs the container on each axis.
    const containerRatio = rect.width / rect.height;
    const imgRatio = img.naturalWidth / img.naturalHeight;
    const baseW = imgRatio > containerRatio ? rect.width : rect.height * imgRatio;
    const baseH = imgRatio > containerRatio ? rect.width / imgRatio : rect.height;
    const maxX = Math.max(0, (baseW * ZOOM_LEVEL - rect.width) / 2);
    const maxY = Math.max(0, (baseH * ZOOM_LEVEL - rect.height) / 2);
    return { x: Math.min(maxX, Math.max(-maxX, x)), y: Math.min(maxY, Math.max(-maxY, y)) };
  }, []);

  const handleMouseDown = (e: React.MouseEvent) => {
    if (e.button !== 0) return;
    dragState.current = { startX: e.clientX, startY: e.clientY, startPanX: pan.x, startPanY: pan.y, moved: false, dragging: true };
    setIsDragging(true);
  };

  useEffect(() => {
    function handleMouseMove(e: MouseEvent) {
      const ds = dragState.current;
      if (!ds.dragging) return;
      const dx = e.clientX - ds.startX;
      const dy = e.clientY - ds.startY;
      if (Math.abs(dx) > DRAG_THRESHOLD || Math.abs(dy) > DRAG_THRESHOLD) ds.moved = true;
      if (zoomed && ds.moved) setPan(clampPan(ds.startPanX + dx, ds.startPanY + dy));
    }
    function handleMouseUp() {
      const ds = dragState.current;
      if (!ds.dragging) return;
      ds.dragging = false;
      setIsDragging(false);
      if (!ds.moved) {
        // A genuine click, no real drag -- toggle zoom.
        if (zoomed) {
          setZoomed(false);
          setPan({ x: 0, y: 0 });
        } else {
          setZoomed(true);
        }
      }
    }
    window.addEventListener('mousemove', handleMouseMove);
    window.addEventListener('mouseup', handleMouseUp);
    return () => {
      window.removeEventListener('mousemove', handleMouseMove);
      window.removeEventListener('mouseup', handleMouseUp);
    };
  }, [zoomed, clampPan]);

  const url = urls[index];
  const stop = (e: React.MouseEvent) => e.stopPropagation();

  return (
    <div
      className="fixed inset-0 z-50 bg-black/90 flex items-center justify-center"
      onClick={onClose}
    >
      <button
        type="button"
        onClick={onClose}
        className="absolute top-5 right-5 w-10 h-10 flex items-center justify-center border-2 border-[var(--riso-line)] bg-[var(--riso-surface)] text-[var(--riso-text)] hover:border-[var(--riso-pink)] hover:text-[var(--riso-pink)] transition-colors z-10"
        aria-label="Close"
      >
        <X size={20} />
      </button>

      <a
        href={url}
        download
        onClick={stop}
        className="absolute top-5 right-[70px] w-10 h-10 flex items-center justify-center border-2 border-[var(--riso-line)] bg-[var(--riso-surface)] text-[var(--riso-text)] hover:border-[var(--riso-yellow)] hover:text-[var(--riso-yellow)] transition-colors z-10"
        aria-label="Download image"
      >
        <Download size={18} />
      </a>

      {hasMultiple && (
        <button
          type="button"
          onClick={(e) => { stop(e); goPrev(); }}
          className="absolute left-5 top-1/2 -translate-y-1/2 w-12 h-12 flex items-center justify-center border-2 border-[var(--riso-line)] bg-[var(--riso-surface)] text-[var(--riso-text)] hover:border-[var(--riso-pink)] hover:text-[var(--riso-pink)] transition-colors z-10"
          aria-label="Previous image"
        >
          <ChevronLeft size={24} />
        </button>
      )}

      <div
        ref={containerRef}
        className="relative w-[90vw] h-[90vh] overflow-hidden flex items-center justify-center"
        onClick={stop}
        onMouseDown={handleMouseDown}
        style={{ cursor: zoomed ? (isDragging ? 'grabbing' : 'grab') : 'zoom-in' }}
      >
        <img
          ref={imgRef}
          src={url}
          alt="Catalog page"
          draggable={false}
          className="max-w-full max-h-full object-contain block select-none border-2 border-[var(--riso-text)] bg-white"
          style={{
            transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoomed ? ZOOM_LEVEL : 1})`,
            transition: isDragging ? 'none' : 'transform 0.2s ease-out',
          }}
        />
      </div>

      {hasMultiple && (
        <button
          type="button"
          onClick={(e) => { stop(e); goNext(); }}
          className="absolute right-5 top-1/2 -translate-y-1/2 w-12 h-12 flex items-center justify-center border-2 border-[var(--riso-line)] bg-[var(--riso-surface)] text-[var(--riso-text)] hover:border-[var(--riso-pink)] hover:text-[var(--riso-pink)] transition-colors z-10"
          aria-label="Next image"
        >
          <ChevronRight size={24} />
        </button>
      )}

      {hasMultiple && (
        <div className="absolute bottom-5 left-1/2 -translate-x-1/2 font-data text-xs text-white/70 tracking-widest">
          {index + 1} / {urls.length}
        </div>
      )}
    </div>
  );
}
