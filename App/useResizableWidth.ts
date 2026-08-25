import { useCallback, useEffect, useRef, useState } from 'react';

/** Drag-to-resize a panel's width from a border handle.
 *
 * `edge` says which side of the panel the handle lives on -- 'right' for a
 * panel whose handle sits on its own right edge (dragging further right
 * makes it WIDER), 'left' for one whose handle sits on its own left edge
 * (dragging further left makes it WIDER). Needed because the two sidebars
 * in this app are mirror images of each other: the same rightward mouse
 * movement should widen the left sidebar but narrow the right panel.
 *
 * Deliberately reads live mouse position via document-level listeners
 * (not the dragged element's own onMouseMove) -- the cursor routinely
 * leaves the thin 6px handle strip mid-drag, and a handler scoped to the
 * element would stop firing the moment that happens, freezing the resize
 * well before the user releases the mouse.
 */
export function useResizableWidth(initialWidth: number, min: number, max: number, edge: 'left' | 'right') {
  const [width, setWidth] = useState(initialWidth);
  const [isResizing, setIsResizing] = useState(false);
  const startX = useRef(0);
  const startWidth = useRef(initialWidth);

  const onMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    startX.current = e.clientX;
    startWidth.current = width;
    setIsResizing(true);
  }, [width]);

  useEffect(() => {
    if (!isResizing) return;

    const onMouseMove = (e: MouseEvent) => {
      const delta = e.clientX - startX.current;
      const signedDelta = edge === 'right' ? delta : -delta;
      setWidth(Math.min(max, Math.max(min, startWidth.current + signedDelta)));
    };
    const onMouseUp = () => setIsResizing(false);

    // Cursor forced at the document level (not just the handle) so it
    // stays a resize icon for the whole drag even while the pointer is
    // over unrelated content (the chat panel, the images, etc), same
    // reason the move listener is document-level above. user-select:none
    // stops the drag from also highlighting page text, a normal side
    // effect of fast mouse movement during a drag gesture.
    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
    const prevCursor = document.body.style.cursor;
    const prevUserSelect = document.body.style.userSelect;
    document.body.style.cursor = 'ew-resize';
    document.body.style.userSelect = 'none';

    return () => {
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
      document.body.style.cursor = prevCursor;
      document.body.style.userSelect = prevUserSelect;
    };
  }, [isResizing, edge, min, max]);

  return { width, isResizing, onMouseDown };
}
