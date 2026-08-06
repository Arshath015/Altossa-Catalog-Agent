"""
backfill_manual_tier_labels.py
--------------------------------
Adds a "tier_label" field to manual_additions.json's hand-transcribed rows
-- the real source-PDF category word (e.g. "Base", "Top", "Rivestimento",
"Struttura", "Seduta") that a product's fabric_tier VALUES actually came
from, so the chat UI can show the real column header instead of a
hardcoded "FABRIC" that's wrong for anything that isn't upholstery.

WHY A SEPARATE SCRIPT, NOT PART OF parse_prices.py: these 67 products'
price rows were hand-transcribed (the automatic column-position-matching
in parse_prices.py couldn't reliably parse their particular page layouts
-- dense multi-table pages, wrapped labels, etc, same reason they're in
manual_additions.json at all) -- so parse_prices.py's own additive
tier_label capture (added alongside its position-matching logic) never
runs for them. Re-deriving the label here independently, WITHOUT touching
parse_prices.py's hardened matching logic at all.

METHOD, deliberately conservative: for each of these 67 products, scan its
already-extracted raw text file (the same file parse_prices.py reads) for
every occurrence of a recognized category word, and check whether the text
immediately following it on that line CONTAINS one of this product's own
ALREADY-TRUSTED fabric_tier values (straight from manual_additions.json,
verified by a human when it was transcribed) as a substring. A label is
only assigned when exactly one candidate word is confirmed this way --
never guessed, and never overrides the untouched price/tier/size data
itself. Ambiguous or unconfirmed products are left with tier_label: null,
same as today (a safe no-op, not a regression) -- flagged for manual
review rather than guessed at.

USAGE:
  python backfill_manual_tier_labels.py "data/Cattelan Italia" [--apply]

  Without --apply: dry-run, prints what WOULD be set per product for
  review. With --apply: writes the result back to manual_additions.json.
"""
import json
import re
import sys
import argparse
from pathlib import Path

_LABEL_WORD_RE = re.compile(
    r'^\s*(Top|Base(?:\s*\+\s*Top)?|Seduta|Rivestimento|Struttura|Frontali|'
    r'Fronte|Telaio|Retro|Accessori|Inserti|Impianto|Paralume\s*/\s*Attacco)'
    r'\b\s*(.*)$', re.IGNORECASE)


def normalize(s):
    return re.sub(r'\s+', ' ', (s or '')).strip().lower()


def find_label_for_product(text_path, real_tier_values):
    """Returns (label_word, confidence_note) or (None, reason)."""
    if not text_path.exists():
        return None, f"text file missing: {text_path}"
    lines = text_path.read_text(encoding='utf-8').split('\n')
    real_tier_norms = {normalize(t) for t in real_tier_values if t}
    if not real_tier_norms:
        return None, "product has no fabric_tier values at all -- nothing to label"

    candidates = {}  # label_word (lowercased) -> display form
    for line in lines:
        m = _LABEL_WORD_RE.match(line)
        if not m:
            continue
        label_word, rest = m.group(1), m.group(2)
        rest_norm = normalize(rest)
        if not rest_norm:
            continue
        # Confirm this line's text actually contains one of the product's
        # OWN real tier values (not just any text) -- e.g. "Base   GFM69 /
        # GFM73" and the product really has fabric_tier "GFM69 / GFM73".
        if any(t in rest_norm or rest_norm in t for t in real_tier_norms):
            key = re.sub(r'\s+', ' ', label_word).strip().lower()
            candidates[key] = re.sub(r'\s+', ' ', label_word).strip()

    if len(candidates) == 1:
        return next(iter(candidates.values())), "confirmed: unique label word whose text matches a real tier value"
    if len(candidates) == 0:
        return None, "no label word's text matched any real tier value -- left unlabeled"
    return None, f"AMBIGUOUS: {sorted(candidates.values())} all matched -- left unlabeled, needs manual check"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("brand_dir", help='e.g. "data/Cattelan Italia"')
    ap.add_argument("--apply", action="store_true", help="write changes back to manual_additions.json (default: dry-run)")
    args = ap.parse_args()

    brand_dir = Path(args.brand_dir)
    manual_path = brand_dir / "manual_additions.json"
    index_path = brand_dir / "catalog_index.json"
    data_root = brand_dir.parent

    manual_rows = json.loads(manual_path.read_text(encoding='utf-8'))
    index = json.loads(index_path.read_text(encoding='utf-8'))
    text_file_by_name = {e["product_name"]: e.get("text_file") for e in index}

    by_product = {}
    for r in manual_rows:
        by_product.setdefault(r["product_name"], []).append(r)

    results = {}
    for product_name, rows in by_product.items():
        text_file = text_file_by_name.get(product_name)
        if not text_file:
            results[product_name] = (None, "no catalog_index.json entry / text_file for this product")
            continue
        real_tiers = [r["fabric_tier"] for r in rows]
        label, note = find_label_for_product(data_root / text_file, real_tiers)
        results[product_name] = (label, note)

    labeled = {p: l for p, (l, _) in results.items() if l}
    unlabeled = {p: n for p, (l, n) in results.items() if not l}

    print(f"{len(by_product)} products in manual_additions.json.")
    print(f"\nConfirmed a label for {len(labeled)}:")
    for p, l in sorted(labeled.items()):
        print(f"   - {p}: {l}")
    print(f"\nLeft unlabeled ({len(unlabeled)}) -- tier_label stays null, same as today:")
    for p, n in sorted(unlabeled.items()):
        print(f"   - {p}: {n}")

    if args.apply:
        for r in manual_rows:
            label, _ = results[r["product_name"]]
            r["tier_label"] = label
        manual_path.write_text(json.dumps(manual_rows, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f"\nWrote tier_label into {len(manual_rows)} rows: {manual_path}")
    else:
        print("\nDry run only -- pass --apply to write these into manual_additions.json.")


if __name__ == "__main__":
    main()
