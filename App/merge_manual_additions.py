"""
merge_manual_additions.py
--------------------------
Merges manual_additions.json (hand-transcribed rows for the 6 Bolzan
products the auto-parser couldn't handle -- 3 signature headboards with a
single fixed price, plus Pouf Ares/Edith and Jack-e Fabric, whose pages
mix two tables closely enough to confuse the automatic column-splitter)
into the main prices.json produced by parse_prices.py.

USAGE:
  python merge_manual_additions.py \
      "D:\\Altossa\\AI Catalog agent\\data\\Bolzan\\prices.json" \
      "D:\\Altossa\\AI Catalog agent\\manual_additions.json"

Safe to re-run: rows for the same 6 products are replaced, not duplicated.
"""

import json
import argparse


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prices_json", help="Path to the main prices.json")
    ap.add_argument("manual_json", help="Path to manual_additions.json")
    args = ap.parse_args()

    with open(args.prices_json, encoding="utf-8") as f:
        main_rows = json.load(f)
    with open(args.manual_json, encoding="utf-8") as f:
        manual_rows = json.load(f)

    manual_products = set(r["product_name"] for r in manual_rows)

    # drop any existing (likely empty/wrong) rows for these products, then
    # append the verified manual rows -- so this is safe to re-run
    kept = [r for r in main_rows if r["product_name"] not in manual_products]
    combined = kept + manual_rows

    with open(args.prices_json, "w", encoding="utf-8") as f:
        json.dump(combined, f, ensure_ascii=False, indent=2)

    print(f"Removed {len(main_rows) - len(kept)} old rows for: {sorted(manual_products)}")
    print(f"Added {len(manual_rows)} manually-verified rows.")
    print(f"Total rows now: {len(combined)}")
    print(f"Wrote: {args.prices_json}")


if __name__ == "__main__":
    main()