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

/** One column per distinct (size, code-when-ambiguous) combination in
 * `rows`. See module comment for why size alone isn't always enough. */
export function buildSizeColumns(rows: PriceRow[]): SizeColumn[] {
  const codesForSize = new Map<string, Set<string | null>>();
  for (const r of rows) {
    if (r.size === null) continue;
    if (!codesForSize.has(r.size)) codesForSize.set(r.size, new Set());
    codesForSize.get(r.size)!.add(r.code);
  }
  const seen = new Map<string, SizeColumn>();
  for (const r of rows) {
    if (r.size === null) continue;
    const ambiguous = (codesForSize.get(r.size)?.size ?? 0) > 1;
    const key = ambiguous ? `${r.size}::${r.code ?? ''}` : r.size;
    if (!seen.has(key)) {
      seen.set(key, {
        key,
        label: ambiguous ? `${r.size} (${r.code ?? '—'})` : r.size,
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
