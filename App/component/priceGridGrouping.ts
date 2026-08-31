/**
 * Pure grouping/column logic for the "full price list" pivot grid,
 * extracted out of CatalogChatWidget.tsx so it can be imported by both
 * the UI and a regression test against the exact same code -- no
 * duplicated logic to drift out of sync.
 *
 * Bug fixed here, found live 2026-08-28 (Venere, user-reported, confirmed
 * against the real source page image): the pivot grid's cell lookup keyed
 * ONLY on (fabric_tier, size), using a single `.find()` that silently
 * returns just the FIRST matching row. Pianca (and likely other brands)
 * regularly prints two genuinely different real products/variants with
 * the IDENTICAL literal L×H×P size string, distinguished only by a real
 * row-group heading too short for the shared heading-capture regex to
 * recognize as a label (Venere: bare "S"/"D"/"A"/"B"; Soffio fisso: "P
 * 80"/"P 90") -- so `variant_context` ends up empty (or, worse, polluted
 * by an unrelated nearby caption line, as it was for Venere's "Titanio
 * Lucido"). A full-dataset scan (2026-08-28) found this exact collision
 * shape -- 2+ DIFFERENT codes sharing one (model_variant, variant_context,
 * fabric_tier, size) key -- in 92 Pianca products, 89 of them with
 * genuinely DIFFERENT prices between the colliding codes (a real
 * wrong-price risk, not just a display nicety). None of this was caught
 * by the existing (code, fabric_tier)-keyed "ambiguous" safety net, which
 * guards a different collision shape (same code, conflicting price) --
 * this one has DIFFERENT codes, so the codes themselves never collide.
 *
 * Fix: `code` is the one thing this catalog format guarantees is unique
 * per real physical row, so it's used as a targeted secondary key --
 * ONLY when a given size string genuinely maps to 2+ different codes
 * within a group (the overwhelming majority of products have zero such
 * collisions and are completely unaffected, byte-for-byte, by this
 * change). When it does collide, each code gets its own column instead of
 * silently overwriting/dropping the other, and the column header spells
 * out the code so it's still clear which physical product is which.
 */

export interface PriceRow {
  product_name: string;
  model_variant: string | null;
  variant_context: string | null;
  size: string | null;
  fabric_tier: string | null;
  tier_label: string | null;
  code: string | null;
  price_eur: string;
  ambiguous: boolean;
}

export interface VariantGroup {
  key: string;
  header: string;
  rows: PriceRow[];
}

export function buildVariantGroups(rows: PriceRow[]): VariantGroup[] {
  const hasMultipleContexts = new Set(rows.map(r => r.variant_context).filter(Boolean)).size > 1;
  const keyOf = (r: PriceRow) => `${r.model_variant || '—'}::${r.variant_context || ''}`;
  const keys = [...new Set(rows.map(keyOf))];
  return keys.map(key => {
    const groupRows = rows.filter(r => keyOf(r) === key);
    const modelVariant = groupRows[0].model_variant || '—';
    const variantContext = groupRows[0].variant_context;
    const header = hasMultipleContexts && variantContext
      ? (modelVariant !== '—' ? `${variantContext} — ${modelVariant}` : variantContext)
      : modelVariant;
    return { key, header, rows: groupRows };
  });
}

export interface SizeColumn {
  /** Unique key for this column -- just the size string when it maps to
   * exactly one code within the group, or "size::code" when it doesn't
   * (see module comment). Always use this for cell lookup, never `size`
   * alone, or a real row can silently disappear again. */
  key: string;
  /** What to actually show in the column header. */
  label: string;
  /** The real size string (for sorting) -- may be shared by multiple
   * columns when a collision split them apart. */
  size: string | null;
}

/** Sentinel grouping key for a dimensionless flat-price row (`size ===
 * null`) -- treated as just another "size" value throughout this
 * function so the SAME collision logic (below) covers both cases with
 * no special-casing. Exported only so `findLostRows`-style regression
 * checks can look a flat-price cell up directly without re-deriving it. */
export const FLAT_PRICE_COLUMN_KEY = '__flat__';

/** One column per distinct (size-or-flat, code-when-ambiguous) combination
 * in `rows`. See module comment for why size alone isn't always enough.
 *
 * Bug fixed here, found live 2026-09-01 (Dedalo (Progetti 06-07)'s own
 * "Accessori kit luce" table, user-reported: 3 real, correctly-priced
 * rows -- 47101/47100/47102, EUR186/186/206 -- all rendered as "-" with
 * no price visible anywhere). Root cause: this function used to
 * unconditionally SKIP every row with `size === null`, which is correct
 * when a group is a MIX of sized and dimensionless rows (skip only the
 * dimensionless ones, they don't need a size column) but wrong when
 * EVERY row in the group has no size at all -- a real, common shape for
 * flat single-price accessories with no L/H/P dimensions. Skipping all
 * of them left the group with ZERO columns, and `VariantTable` renders
 * one `<td>` per column -- with none, the row's own price has nowhere to
 * display, even though `prices.json`/the API response both already have
 * the correct value.
 *
 * A first fix added a single hardcoded flat column for this case, but a
 * blast-radius scan (65 Pianca products, plus 44/34/645/5 across Bolzan/
 * Bonaldo/Varaschini/Ditre Italia -- this component is shared catalog-
 * wide) turned up a second, narrower collision within that same shape:
 * Bolzan's own "Awase" has 5 genuinely DIFFERENT codes (RPFL/RPFM/RPFF/
 * RPFG/RPFP, different bed-frame widths) all sharing one group with
 * size=null AND fabric_tier=null -- a single flat column's own `.find()`
 * would have silently shown only the first and dropped the other 4,
 * the EXACT same shape as this file's own original Venere bug, just on
 * the null-size axis instead of a real one. Fixed by folding the
 * null-size case into the SAME (size, code)-collision logic that already
 * protects real sizes, rather than a separate hardcoded branch -- a
 * dimensionless row's grouping key is just `FLAT_PRICE_COLUMN_KEY`
 * instead of its own size string, everything else (ambiguity detection,
 * code-qualified column key, label) is identical code, not a parallel
 * copy that could drift out of sync. */
export function buildSizeColumns(rows: PriceRow[]): SizeColumn[] {
  const keyFor = (r: PriceRow) => r.size ?? FLAT_PRICE_COLUMN_KEY;
  const codesForKey = new Map<string, Set<string | null>>();
  for (const r of rows) {
    const k = keyFor(r);
    if (!codesForKey.has(k)) codesForKey.set(k, new Set());
    codesForKey.get(k)!.add(r.code);
  }
  const seen = new Map<string, SizeColumn>();
  for (const r of rows) {
    const k = keyFor(r);
    const ambiguous = (codesForKey.get(k)?.size ?? 0) > 1;
    const columnKey = ambiguous ? `${k}::${r.code ?? ''}` : k;
    if (!seen.has(columnKey)) {
      const baseLabel = r.size ?? 'Price';
      seen.set(columnKey, {
        key: columnKey,
        label: ambiguous ? `${baseLabel} (${r.code ?? '—'})` : baseLabel,
        size: r.size,
      });
    }
  }
  return [...seen.values()];
}

/** Cell lookup keyed by the collision-safe column key, not raw size --
 * this is the one call site the original bug lived in (a plain
 * `rows.find(r => r.fabric_tier === tier && r.size === size)` silently
 * returning only the first of 2+ real matches). `columnKey` always
 * either equals a size string exactly (no collision) or is
 * "size::code" (see buildSizeColumns) -- never ambiguous to tell apart,
 * since a real size string can never itself contain "::". */
export function findCell(rows: PriceRow[], tier: string | null, columnKey: string, columns: SizeColumn[]): PriceRow | undefined {
  const col = columns.find(c => c.key === columnKey);
  if (!col) return undefined;
  if (!columnKey.includes('::')) {
    return rows.find(r => r.fabric_tier === tier && r.size === col.size);
  }
  const code = columnKey.slice(columnKey.indexOf('::') + 2);
  return rows.find(r => r.fabric_tier === tier && r.size === col.size && (r.code ?? '') === code);
}
