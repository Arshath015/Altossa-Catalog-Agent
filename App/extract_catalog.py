"""
extract_catalog.py
-------------------
Splits a brand price-list PDF (InDesign export, 2 printed pages per PDF page
"spread") into per-product folders: a mini combined PDF, page images, and
raw text — using the catalog's own photographic index instead of guessing.

WHY THIS APPROACH:
  These catalogs (Bolzan, and likely the other 5 brands) include a
  "Photographic Index" section near the front that lists every product
  name with its printed page number (e.g. "Awase ... p.32"). That index
  is the ground truth for where each product starts. We:
    1. Parse that index to get {product_name -> starting printed page}
    2. Sort by page number -> the next product's start - 1 = this
       product's end page (best-effort; verified against real footers)
    3. Map "printed page number" -> "actual PDF page index" by reading
       the footer of every PDF page (catalogs print two page numbers
       per PDF page, one per side of the spread)
    4. For each product, extract exactly those PDF pages: save a
       mini-PDF, JPEG page images, and raw text.

  This avoids (a) manually screenshotting 248+ pages, and (b) naive
  keyword search picking up a product name from unrelated index/footer
  mentions (e.g. "Marty" appearing ~20 times but only 3 real entries).

REQUIREMENTS:
  - poppler-utils installed (pdftotext, pdftoppm) — on Mac: `brew install poppler`
                                                     on Windows: install poppler,
                                                     add its /bin to PATH
  - pip install pypdf

USAGE:
  python extract_catalog.py "Listino_Bolzan_ITA_2026.pdf" \
      --brand Bolzan \
      --index-pages 3-8 \
      --out ./data

  Adjust --index-pages per brand after you inspect where that brand's
  own photographic index lives (open the PDF, find the "Indice
  fotografico / Photographic index" pages, note the PDF page numbers
  shown in your PDF viewer's toolbar).
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from pypdf import PdfReader, PdfWriter

# Some catalogs' product/section names include characters (À, Ø...) outside
# Windows' default console codepage (cp1252) -- without this, a plain
# print() of such a name crashes the whole script instead of just showing
# a '?' in place of the unprintable character.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="backslashreplace")


def _find_poppler_tool(name: str) -> str:
    """Resolve a poppler executable, preferring this project's own bundled
    build over whatever happens to be first on PATH. On Windows, Git for
    Windows ships its own (older) poppler under mingw64/bin, which can
    silently win PATH resolution ahead of a project-local install -- and it
    was confirmed, on this project's Cattelan PDFs, to mis-reconstruct
    dense proportional-font tables with -layout (columns scrambled across
    the wrong lines) where the project's own poppler-*/ build renders them
    correctly. Always prefer a sibling poppler-*/ folder if one exists."""
    exe = f"{name}.exe" if sys.platform == "win32" else name
    project_root = Path(__file__).resolve().parent.parent
    for candidate in sorted(project_root.glob("poppler-*")):
        for sub in ("Library/bin", "bin"):
            p = candidate / sub / exe
            if p.exists():
                return str(p)
    found = shutil.which(name)
    return found or name


PDFTOTEXT = _find_poppler_tool("pdftotext")
PDFTOPPM = _find_poppler_tool("pdftoppm")

# Verified manual corrections for products where the automatic
# "boundary = next photographic-index entry's start page" computation is
# known to be wrong -- usually because the catalog inserts a non-priced
# section-intro or divider between this product's real content and the
# next actual product's index entry, and those intro pages get silently
# absorbed into this product's range. Each entry was confirmed by directly
# reading the actual rendered page headings, not guessed.
#
# Format: "Product Name": (pdf_page_start, pdf_page_end)
PAGE_RANGE_OVERRIDES: dict[str, tuple[int, int]] = {
    # Auto-computed range was 186-192 (7 pages). Verified pages 186-189
    # are genuinely Wall System's 5 sub-collections (Wall System sp.4.5,
    # Wall Custom sp.10, Wall Capitonné sp.10, Wall Maison sp.10, Wall
    # Corolle sp.10). Pages 190-192 are unambiguously "Line" intro/
    # marketing content (heading literally reads "Line" / sofa-bed
    # description) plus sofa-bed accessory/pillow pages -- an inserted
    # section divider before the next real product (Biba), not Wall
    # System content at all.
    "Wall System": (186, 189),
    # Cattelan Italia (20241_listino.pdf / Novità supplement): auto-computed
    # range was just pdf page 27 (RICHARD and ELIAS both start on printed
    # page 24, and the next index entry -- the first of two "BISHOP"
    # occurrences -- starts on printed page 25, so RICHARD's range was cut
    # to a single page). Verified by rendering pdf page 28 directly:
    # RICHARD's own price table (a bed, 4 stacked Rete/Rete-alzabile x
    # fabric-tier blocks) actually continues onto that page, before
    # BISHOP's heading starts partway down it.
    "RICHARD": (27, 28),
}


def slugify(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "_", s)
    return s.strip("_")


def pdftotext_page(pdf_path: str, page: int) -> str:
    result = subprocess.run(
        # "-enc UTF-8" is required, not just decoding the bytes as UTF-8
        # afterward: poppler's pdftotext on Windows silently picks the
        # console's codepage as its OUTPUT encoding when it can't detect a
        # real terminal (e.g. when piped from subprocess), corrupting any
        # accented character (À, Ø, È...) into U+FFFD regardless of how the
        # bytes are decoded on the Python side. Confirmed via a direct A/B
        # test against this project's Cattelan PDFs -- without this flag,
        # "NOVITÀ" and "Ø140x75h" (round-table sizes) came back with the
        # accented character replaced; with it, they decode correctly.
        [PDFTOTEXT, "-layout", "-enc", "UTF-8", "-f", str(page), "-l", str(page), pdf_path, "-"],
        capture_output=True,
    )
    # Force UTF-8 decoding explicitly. On Windows, subprocess with text=True
    # falls back to the system's default codepage (cp1252), which crashes
    # on accented Italian characters in the catalog text.
    return result.stdout.decode("utf-8", errors="replace")


def parse_index(pdf_path: str, index_pages: range) -> list[tuple[str, int]]:
    """Parse the photographic-index pages into (product_name, printed_page) pairs."""
    entries = []
    for pg in index_pages:
        text = pdftotext_page(pdf_path, pg)
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if re.search(r"\bp\.\d+", line):
                page_tokens = re.split(r"\s{2,}", line.strip())
                j = i - 1
                while j >= 0 and lines[j].strip() == "":
                    j -= 1
                if j < 0:
                    continue
                name_tokens = re.split(r"\s{2,}", lines[j].strip())
                for k, tok in enumerate(page_tokens):
                    m = re.search(r"p\.(\d+)", tok)
                    if m and k < len(name_tokens):
                        name = name_tokens[k].strip()
                        if name and "indice" not in name.lower() and "index" not in name.lower():
                            entries.append((name, int(m.group(1))))
    # de-duplicate identical (name, page) pairs that may appear if columns
    # were mis-split; keep first occurrence order, then sort by page.
    seen = set()
    unique = []
    for name, pg in entries:
        key = (name, pg)
        if key not in seen:
            seen.add(key)
            unique.append((name, pg))
    unique.sort(key=lambda x: x[1])
    return unique


def build_printed_to_pdf_page_map(pdf_path: str, total_pages: int) -> dict[int, int]:
    """Read every page's footer ('<N>  Brand-Listino ...  Brand-Listino ...  <N+1>')
    to map printed page numbers -> actual PDF page index. Falls back to a
    formula for any page where the footer can't be read (cover pages,
    full-bleed section dividers, etc.)."""
    page_map: dict[int, int] = {}
    footer_re = re.compile(
        r"^\s*(\d{1,4})\s+.*listino.*listino.*?(\d{1,4})\s*$",
        re.MULTILINE | re.IGNORECASE,
    )
    for pg in range(1, total_pages + 1):
        text = pdftotext_page(pdf_path, pg)
        m = footer_re.search(text)
        if m:
            left, right = int(m.group(1)), int(m.group(2))
            page_map[left] = pg
            page_map[right] = pg
    return page_map


def printed_to_pdf_fallback(printed: int) -> int:
    """Formula fallback (2 printed pages per PDF spread), for pages whose
    footer couldn't be read."""
    return (printed + 1) // 2 + 1 if printed % 2 == 0 else (printed + 1) // 2


# Known section/category headers from Cattelan's own two-level dot-leader
# index (main catalog + "Novità" supplement). This catalog's index is
# supposed to distinguish these from real product entries purely by
# indentation (un-indented = category, indented = product) -- but
# confirmed via direct pdftotext inspection that poppler silently drops
# ALL leading whitespace on some index pages (page 4 of the main
# catalog's index, specifically) while preserving it correctly on
# others (page 3) -- a rendering quirk, not something in our control.
# Relying on indentation alone silently dropped every single product
# indexed on that page (PLISSET through SCOTT Wood, ~17 products
# including the entire PREMIER family) with no error or warning. An
# explicit, finite list of the real section names is immune to this: it
# doesn't matter whether a line happens to be indented or not, only
# whether its name IS one of these.
KNOWN_CATTELAN_SECTIONS = {
    'TAVOLI', 'SCRIVANIE', 'CONSOLLE', 'SEDUTE', 'SGABELLI', 'MADIE',
    'LIBRERIE', 'TAVOLINI', 'PORTA TV', 'COMPLEMENTI', 'SPECCHI',
    'LAMPADE', 'LETTI - COMODINI', 'OUTDOOR', 'NOVITÀ',
}


def parse_index_dot_leader(pdf_path: str, index_pages: range) -> list[tuple[str, int]]:
    """Parse a "NAME ........... N" dot-leader index (Cattelan's format,
    instead of Bolzan's "p.N" format). This index is two-level: section/
    category headers (e.g. "TAVOLI", "OUTDOOR") followed by real product
    entries (e.g. "ATLANTIS Crystalart"). Only the product lines are real
    catalog entries -- category headers are skipped entirely, same as
    Bolzan's parser never emits section headers. Distinguished by exact
    membership in KNOWN_CATTELAN_SECTIONS, NOT by indentation -- see that
    set's own comment for why indentation alone is unreliable here.
    """
    entry_re = re.compile(r'^(\s*)(.+?)\s*\.{3,}\s*(\d{1,4})\s*$')
    entries = []
    for pg in index_pages:
        text = pdftotext_page(pdf_path, pg)
        for line in text.split('\n'):
            if not line.strip() or 'indice' in line.strip().lower():
                continue
            m = entry_re.match(line)
            if not m:
                continue
            name, page_num = m.group(2).strip(), int(m.group(3))
            if name.upper() in KNOWN_CATTELAN_SECTIONS:
                continue  # a category/section header, not a product
            if name:
                entries.append((name, page_num))
    seen = set()
    unique = []
    for name, pg in entries:
        key = (name, pg)
        if key not in seen:
            seen.add(key)
            unique.append((name, pg))
    unique.sort(key=lambda x: x[1])
    return unique


def build_offset_page_map(pdf_path: str, total_pages: int) -> dict[int, int]:
    """Read every page's footer to map printed page numbers -> real PDF
    pages, for catalogs whose footer is a single running page counter
    (e.g. "TAVOLI - 1", "SCRIVANIE - 109", "OUTDOOR - 350") rather than
    Bolzan's two-numbers-per-spread footer. Unlike a hardcoded constant
    offset, this is verified page-by-page -- so non-priced back-matter
    pages (terms & conditions, finish-code legends, technical drawings)
    that don't carry this footer pattern simply never get mapped, which
    is what naturally caps a catalog's last product's range at its real
    last content page instead of over-extending into back matter."""
    page_map: dict[int, int] = {}
    footer_re = re.compile(r'^[A-ZÀ-Ù][A-ZÀ-Ù \-]*-\s*(\d{1,4})$')
    for pg in range(1, total_pages + 1):
        text = pdftotext_page(pdf_path, pg)
        lines = [ln.strip() for ln in text.split('\n') if ln.strip()]
        if not lines:
            continue
        m = footer_re.match(lines[-1])
        if m:
            page_map[int(m.group(1))] = pg
    return page_map


def build_offset_fallback(page_map: dict[int, int]):
    """Returns a fallback printed->pdf function for pages build_offset_page_map
    couldn't read a footer on, based on the most common observed offset."""
    if not page_map:
        return lambda p: p
    offsets: dict[int, int] = {}
    for printed, pg in page_map.items():
        offsets[pg - printed] = offsets.get(pg - printed, 0) + 1
    common_offset = max(offsets.items(), key=lambda kv: kv[1])[0]
    return lambda p: p + common_offset


def compute_ranges(entries: list[tuple[str, int]], last_printed_page_guess: int) -> list[dict]:
    """Turn a sorted (name, start_page) list into (name, start_page, end_page).

    For every product except the last, end = next product's start - 1.
    For the LAST product, we have no "next" entry to bound it against, so
    instead of guessing a small number of pages (which silently truncates
    real content — e.g. a mattress sub-catalog spanning 25+ pages), we
    extend it all the way to the end of the document. This over-includes
    rather than under-includes; flag it for a manual follow-up pass if the
    last product turns out to be a whole subcategory with its own products.
    """
    ranges = []
    for i, (name, start) in enumerate(entries):
        if i + 1 < len(entries):
            next_start = entries[i + 1][1]
            end = max(start, next_start - 1)
        else:
            end = max(start, last_printed_page_guess)
        ranges.append({"name": name, "printed_start": start, "printed_end": end})
    return ranges


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf", help="Path to the brand price-list PDF")
    ap.add_argument("--brand", required=True, help="Brand name, e.g. Bolzan")
    ap.add_argument(
        "--index-pages",
        required=True,
        help="PDF page range of the photographic index, e.g. 3-8",
    )
    ap.add_argument("--out", default="./data", help="Output root folder")
    ap.add_argument("--style", default="bolzan", choices=["bolzan", "cattelan"],
                     help="Index format + page-footer style. 'bolzan' = "
                          "'p.N' index, two-number-per-spread footer "
                          "(default, unchanged). 'cattelan' = dot-leader "
                          "'NAME .... N' index with un-indented category "
                          "headers, single running-counter footer.")
    ap.add_argument("--merge", action="store_true",
                     help="Merge into an existing catalog_index.json instead "
                          "of overwriting it: entries from this run replace "
                          "any existing entry with the same product_name "
                          "(kept, not duplicated), everything else in the "
                          "existing file is preserved as-is.")
    args = ap.parse_args()

    pdf_path = args.pdf
    reader = PdfReader(pdf_path)
    total_pages = len(reader.pages)

    lo, hi = (int(x) for x in args.index_pages.split("-"))
    index_pages = range(lo, hi + 1)

    print(f"[1/5] Parsing photographic index (PDF pages {lo}-{hi})...")
    entries = (
        parse_index_dot_leader(pdf_path, index_pages) if args.style == "cattelan"
        else parse_index(pdf_path, index_pages)
    )
    print(f"      -> found {len(entries)} products")
    if not entries:
        print("ERROR: no products found. Check --index-pages points at the "
              "right pages (open the PDF and confirm).")
        sys.exit(1)

    print(f"[2/5] Reading footers on all {total_pages} PDF pages to map "
          f"printed page numbers -> real PDF pages...")
    if args.style == "cattelan":
        page_map = build_offset_page_map(pdf_path, total_pages)
        page_fallback = build_offset_fallback(page_map)
    else:
        page_map = build_printed_to_pdf_page_map(pdf_path, total_pages)
        page_fallback = printed_to_pdf_fallback
    print(f"      -> mapped {len(page_map)} printed-page numbers "
          f"({total_pages - len({v for v in page_map.values()})} PDF pages "
          f"had no readable footer and will use the fallback formula)")

    print("[3/5] Computing per-product page ranges...")
    last_printed_page_guess = max(page_map.keys()) if page_map else entries[-1][1] + 1
    ranges = compute_ranges(entries, last_printed_page_guess)
    print(f"      -> NOTE: last product in the index ('{ranges[-1]['name']}') has been "
          f"extended to printed page {ranges[-1]['printed_end']} (end of document) "
          f"since there's no next entry to bound it. If it's actually a whole "
          f"subcategory (like a mattress sub-catalog with its own models), "
          f"we'll want to split it further in a follow-up pass.")

    out_root = Path(args.out) / args.brand
    (out_root / "pages").mkdir(parents=True, exist_ok=True)
    (out_root / "images").mkdir(parents=True, exist_ok=True)
    (out_root / "text").mkdir(parents=True, exist_ok=True)

    catalog = []
    used_slugs: dict[str, str] = {}  # slug -> product name that claimed it first
    slug_collisions: list[tuple[str, str, str]] = []  # (name, original_slug, final_slug)
    print("[4/5] Extracting per-product PDFs, images, and text...")
    for item in ranges:
        name = item["name"]
        p_start, p_end = item["printed_start"], item["printed_end"]

        pdf_start = page_map.get(p_start, page_fallback(p_start))
        pdf_end = page_map.get(p_end, page_fallback(p_end))
        pdf_start, pdf_end = min(pdf_start, pdf_end), max(pdf_start, pdf_end)
        pdf_start = max(1, pdf_start)
        pdf_end = min(total_pages, pdf_end)

        # Manual page-range corrections, verified against the actual
        # rendered page content (not just the photographic index's own
        # page references). The automatic "boundary = next index entry's
        # start page" assumption breaks when the catalog inserts a
        # non-priced section-intro/divider between one product's real
        # content and the next product's index entry -- those intro pages
        # get silently absorbed into the WRONG product's range. Each
        # entry here was confirmed by reading the actual page headings.
        if name in PAGE_RANGE_OVERRIDES:
            pdf_start, pdf_end = PAGE_RANGE_OVERRIDES[name]

        slug = base_slug = slugify(name)
        suffix = 2
        while slug in used_slugs:
            # Two genuinely different index entries produced the same slug
            # (e.g. two distinct products that happen to share a bare name
            # like "Ciro" for both a bed and its matching nightstand) --
            # rather than silently letting the second one's files overwrite
            # the first's, disambiguate the slug and flag it for review.
            slug = f"{base_slug}_{suffix}"
            suffix += 1
        if slug != base_slug:
            slug_collisions.append((name, base_slug, slug))
        used_slugs[slug] = name

        # 1) mini combined PDF (0-indexed in pypdf)
        writer = PdfWriter()
        for p in range(pdf_start, pdf_end + 1):
            writer.add_page(reader.pages[p - 1])
        mini_pdf_path = out_root / "pages" / f"{slug}.pdf"
        with open(mini_pdf_path, "wb") as f:
            writer.write(f)

        # 2) page images (one JPEG per PDF page in range)
        image_files = []
        page_images = {}  # pdf page number (as string) -> image filename
        for p in range(pdf_start, pdf_end + 1):
            prefix = out_root / "images" / f"{slug}_p{p}"
            subprocess.run(
                [PDFTOPPM, "-jpeg", "-r", "150", "-f", str(p), "-l", str(p),
                 pdf_path, str(prefix)],
                capture_output=True,
            )
            matches = list((out_root / "images").glob(f"{slug}_p{p}*.jpg"))
            if matches:
                image_files.append(matches[0].name)
                page_images[str(p)] = matches[0].name

        # 3) raw text (for later structured price parsing / chat grounding).
        # Each page's text is preceded by a "<<<PDFPAGE:N>>>" marker so the
        # price parser can track exactly which PDF page any given row came
        # from -- this lets the chat later show just the ONE relevant page
        # for a specific model variant (e.g. "Cameo Maison h.7") instead of
        # every page in the product's whole range (which might also include
        # an unrelated h.29 section).
        text_chunks = [
            f"<<<PDFPAGE:{p}>>>\n{pdftotext_page(pdf_path, p)}"
            for p in range(pdf_start, pdf_end + 1)
        ]
        text_path = out_root / "text" / f"{slug}.txt"
        text_path.write_text("\n\n".join(text_chunks), encoding="utf-8")

        catalog.append({
            "brand": args.brand,
            "product_name": name,
            "slug": slug,
            "printed_page_start": p_start,
            "printed_page_end": p_end,
            "pdf_page_start": pdf_start,
            "pdf_page_end": pdf_end,
            "mini_pdf": str(mini_pdf_path.relative_to(args.out)),
            "images": image_files,
            "page_images": page_images,
            "text_file": str(text_path.relative_to(args.out)),
        })

    if slug_collisions:
        print(f"\nWARNING: {len(slug_collisions)} product name(s) collided on "
              f"the same slug with another entry from this same run -- these "
              f"are likely genuinely different products that just share a "
              f"bare name (verify against the real page before trusting):")
        for name, orig, final in slug_collisions:
            print(f"   - \"{name}\": slug '{orig}' already taken -> used '{final}' instead")

    catalog_path = out_root / "catalog_index.json"
    if args.merge and catalog_path.exists():
        existing = json.loads(catalog_path.read_text(encoding="utf-8"))
        new_names = {c["product_name"] for c in catalog}
        kept = [c for c in existing if c["product_name"] not in new_names]
        print(f"\n[merge] Existing catalog_index.json had {len(existing)} entries. "
              f"{len(existing) - len(kept)} superseded by this run's {len(catalog)} "
              f"entries (same product_name); {len(kept)} untouched entries kept.")
        catalog = kept + catalog

    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[5/5] Done. Wrote {len(catalog)} total products to {out_root}/")
    print(f"      Index file: {catalog_path}")
    print("\nSpot-check a few entries in catalog_index.json, and open a couple")
    print("of the generated files in pages/ and images/ to confirm accuracy")
    print("before we build the price-parsing + chat layer on top of this.")


if __name__ == "__main__":
    main()