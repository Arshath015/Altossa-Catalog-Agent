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
    # Bonaldo (00_LISTINO-2026_ITALIA-italiano.pdf): "Roll walk-in closet"
    # is the LAST entry in the alphabetical index (starts printed page
    # 576) so it has no next-entry to bound it, and the auto-extension
    # ("no next entry -> extend to end of document") swallowed 50 pages
    # of completely unrelated back matter: page 579 is its own separate
    # "ACCESSORI LETTO" mini-index (RETI/MATERASSI/GUANCIALI/BIANCHERIA/
    # PIUMINI, pages 580-609ish -- not in the main alphabetical index at
    # all), followed by a general "RIVESTIMENTO SUPPLEMENTARE" fabric-tier
    # reference table (pages ~610-627) that applies broadly to DIVANI/
    # LETTI, not specifically to Roll. Verified pages 576-578 are Roll's
    # own real content (heading "ROLL walk-in closet" / "ROLL" on all 3),
    # page 579 is unambiguously the ACCESSORI LETTO section divider.
    "Roll walk-in closet": (576, 578),
}

# Bonaldo: products whose name appears in BOTH 00_LISTINO-2026 (main) and
# BONALDO_INTEGRAZIONE (supplement) do NOT always mean the supplement is a
# full replacement -- confirmed by hand-comparing all 6 name-overlaps
# against real rendered pages (2026-08-07):
#   - Arragan shelf / Arragan TV stand / Belloalto bed: byte-identical
#     between the two documents -- doesn't matter which wins.
#   - Nairobi / Belloalto: the integrazione version is a strict superset
#     (Nairobi adds a "Nut brown" colorway; Belloalto matches element-for-
#     element and additionally corrects 2 "Cuscini decorativi" column
#     prices) -- safe to let it win, which --merge already does by default.
#   - Flatiron table: the ONE real exception. The main catalog has SIX
#     material-combination sub-tables (MONO MATERIAL-LEGNO in both
#     rectangular 210/260/310 AND square 180/220, LEGNO-LACCATO,
#     LEGNO-MARMO, VETRO-LEGNO, CERAMICA-LEGNO) across pages 113-115; the
#     integrazione only reprints TWO of them (the square MONO MATERIAL --
#     an exact duplicate, same codes/prices -- and VETRO-LEGNO, which adds
#     one genuinely new row: "Cristallo: Glossy black", codes TC8M/TC8N/
#     TC8P). Letting --merge's default "new run wins on name match" apply
#     here would silently DELETE the 4 rectangular/marble/ceramic sub-
#     tables that only exist in the main catalog. So: keep the main
#     catalog's version (it's the more complete one), and separately note
#     (see project memory) that the "Glossy black" row still needs adding
#     by hand once the parser exists -- it's real, just not worth blocking
#     the rest of the catalog extraction over one row.
MERGE_KEEP_EXISTING = {"Flatiron table"}


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


# Bonaldo's index pages intersperse these section/navigation words (its
# 7 main-catalog top-nav categories, plus the integrazione supplement's
# own finer-grained ~13 categories) among the real product names -- both
# sets unioned here since a name that isn't a real product in EITHER
# document is safe to always skip.
KNOWN_BONALDO_CATEGORIES = {
    'ILLUMINAZIONE', 'COMPLEMENTI', 'TAVOLI', 'SEDIE', 'LETTI',
    'POLTRONE & POUF', 'DIVANI', 'INDEX', 'INDICE ALFABETICO', 'A/Z',
    'INDICE', 'INDICE GENERALE',
    'SEDIE IMBOTTITE', 'TAVOLINI', 'SPECCHI', 'MENSOLE', 'PORTA TV',
    'SCRITTOI', 'POLTRONE', 'CREDENZE', 'COMODINI', 'TAPPETI',
    # The integrazione supplement's index opens with an "Informazioni
    # generali" mini-TOC (Nuovi materiali/Piani in marmo/Portata massima
    # -- reference/appendix pages, not real products). "Informazioni
    # generali" itself has NO page number of its own (its 3 sub-items do),
    # so leaving it in the name stream shifts every single name/number
    # pairing after it by one position for the rest of the page -- caught
    # by direct page-content verification, not by the name/number COUNT
    # check (the counts came out equal by coincidence, masking the shift).
    # The sub-items are skipped too since they're not real furniture
    # products and would otherwise pollute catalog_index.json.
    'INFORMAZIONI GENERALI', 'NUOVI MATERIALI', 'PIANI IN MARMO',
    'PORTATA MASSIMA',
}


# The main catalog's index (pages 6-9) prints its A-Z list in TWO
# side-by-side columns per page (confirmed via real page content: an
# "F/G" column on the left, a "J/K" column on the right of the SAME
# page). Both a raw-text-stream approach AND a fixed-character-column
# -layout split were tried and both broke on real data:
#   - raw (non-layout) text interleaves the two columns' names and
#     numbers in an order that does NOT preserve column identity --
#     confirmed directly: on page 7, "Kayla" (real page 50) got zipped
#     with "461" and "Liam" (real page 356) got zipped with "50" instead.
#     Invisible from name/number COUNTS alone (they still matched) and
#     from a handful of lucky spot-checks (several entries on the same
#     page zipped correctly by coincidence) -- only a BROAD sample
#     checked against real page headings surfaced it.
#   - a fixed character-column split (e.g. always cut at column 63) works
#     on some pages but slices straight through the middle of real names
#     on others, because the exact gutter position drifts a few
#     characters between pages (confirmed: page 8 produced garbled
#     fragments like "Padd"/"Pean"/"Pror" -- truncated "Paddle"/"Peanut"/
#     "Prora" -- because its right column starts a few characters to the
#     left of where page 7's does).
# The fix that actually holds: read each word's real (x, y) position via
# `pdftotext -tsv`, not character-grid text. This confirmed something
# important -- a name and its own page number genuinely share the SAME
# row (y/"top" coordinate) in the underlying PDF; the apparent
# name-on-one-line/number-on-the-next staggering seen in plain -layout
# text was purely a rendering artifact of how poppler merges overlapping
# vertical space, not a real positional gap. Splitting words into a left
# half and right half by x-position (using the gap whose MIDPOINT falls
# closest to the page's own half-width as the cut point -- the true
# inter-column gutter is NOT always the single widest gap on the page:
# the intra-column gap between a name and its own right-aligned page
# number can be wider than the gutter between the two columns, confirmed
# on every sampled page) and then grouping each half's words by row
# eliminates both failure modes at once.
def _bonaldo_index_split_x(lefts: list[float], page_width: float) -> float:
    """Given every word's left-x on a page, return the x-coordinate to
    split LEFT-column content from RIGHT-column content. See this
    section's module-level comment for why "the widest gap" is the wrong
    heuristic and "the gap closest to the page's own half-width" is the
    one that held up across every sampled page."""
    xs = sorted(set(round(x) for x in lefts))
    if len(xs) < 2:
        return page_width / 2
    gaps = [(xs[i], xs[i + 1]) for i in range(len(xs) - 1)]
    half = page_width / 2
    best = min(gaps, key=lambda g: abs((g[0] + g[1]) / 2 - half))
    return (best[0] + best[1]) / 2


def parse_index_bonaldo(pdf_path: str, index_pages: range) -> list[tuple[str, int]]:
    """Parse Bonaldo's alphabetical index: a flat A-Z list with NO dot
    leaders and no "p." prefix -- just a name and its own page number,
    laid out in two side-by-side columns per page (see the module-level
    comment above _bonaldo_index_split_x for why this needs real word
    positions, not character-grid text). Also skips: single uppercase
    letters (alphabet-group markers, e.g. "A", "B"), known category/nav
    words, and this page's own footer page number (which must NOT be
    counted as a product's page number).
    """
    single_letter_re = re.compile(r"^[A-Z]$")
    entries: list[tuple[str, int]] = []
    for pg in index_pages:
        result = subprocess.run(
            [PDFTOTEXT, "-tsv", "-enc", "UTF-8", "-f", str(pg), "-l", str(pg), pdf_path, "-"],
            capture_output=True,
        )
        tsv_text = result.stdout.decode("utf-8", errors="replace")
        tsv_rows = [ln.rstrip("\r").split("\t") for ln in tsv_text.split("\n") if ln.strip()]
        if not tsv_rows:
            continue
        header = tsv_rows[0]
        col = {name: i for i, name in enumerate(header)}
        page_width = None
        words = []  # (top, left, text)
        for row in tsv_rows[1:]:
            if len(row) <= max(col.values()):
                continue
            level = row[col["level"]]
            if level == "1" and page_width is None:
                page_width = float(row[col["width"]])
            if level != "5":
                continue
            words.append((float(row[col["top"]]), float(row[col["left"]]), row[col["text"]]))
        if not words or page_width is None:
            continue

        split_x = _bonaldo_index_split_x([w[1] for w in words], page_width)
        for is_right in (False, True):
            half_words = [w for w in words if (w[1] >= split_x) == is_right]
            # group into rows: words sharing the same (rounded) top
            rows_by_top: dict[int, list[tuple[float, str]]] = {}
            for top, left, text in half_words:
                rows_by_top.setdefault(round(top), []).append((left, text))
            names: list[str] = []
            numbers: list[int] = []
            for top_key in sorted(rows_by_top):
                row_words = [t for _, t in sorted(rows_by_top[top_key])]
                row_text = " ".join(row_words)
                if row_text.upper() in KNOWN_BONALDO_CATEGORIES or single_letter_re.match(row_text):
                    continue
                if "BONALDO" in row_text.upper() or "ESCLUSA" in row_text.upper():
                    continue
                # a row pairing a name with its own page number has the
                # number as the LAST word (confirmed: name and number
                # share one row; a name-only continuation row has no
                # trailing digit-only word at all)
                if row_words and re.fullmatch(r"\d{1,4}", row_words[-1]):
                    name_part = " ".join(row_words[:-1]).strip()
                    if name_part and name_part.upper() not in KNOWN_BONALDO_CATEGORIES:
                        names.append(name_part)
                        numbers.append(int(row_words[-1]))
                # else: a category/letter-only row already skipped above,
                # or this page's own footer/page-number row (no name
                # part) -- neither is a product entry
            if len(names) != len(numbers):
                print(f"      WARNING: page {pg} -- {len(names)} name(s) but "
                      f"{len(numbers)} number(s) found in one column; "
                      f"verify this page by hand")
            entries.extend(zip(names, numbers))
    seen = set()
    unique = []
    for name, pg in entries:
        key = (name, pg)
        if key not in seen:
            seen.add(key)
            unique.append((name, pg))
    return unique  # NOT sorted by page -- this index is alphabetical, not page-ordered


def build_bonaldo_page_map(pdf_path: str, total_pages: int) -> dict[int, int]:
    """Bonaldo uses a single running page-number counter for the whole
    document (confirmed: PDF page N carries printed page number N, sampled
    every 50 pages end to end) -- but this still reads every page's real
    footer rather than assuming identity, so any gap (an unnumbered
    divider page, back matter) is caught instead of silently mismapping
    everything after it. Footer is either a bare number or the full
    "BONALDO Listino Prezzi Italia <edition> - EURO - IVA ESCLUSA  N" line.
    """
    page_map: dict[int, int] = {}
    for pg in range(1, total_pages + 1):
        text = pdftotext_page(pdf_path, pg)
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        if not lines:
            continue
        last = lines[-1]
        if re.fullmatch(r"\d{1,4}", last):
            page_map[int(last)] = pg
        elif "LISTINO" in last.upper() or "ESCLUSA" in last.upper():
            m = re.search(r"(\d{1,4})\s*$", last)
            if m:
                page_map[int(m.group(1))] = pg
    return page_map


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
    ap.add_argument("--style", default="bolzan", choices=["bolzan", "cattelan", "bonaldo"],
                     help="Index format + page-footer style. 'bolzan' = "
                          "'p.N' index, two-number-per-spread footer "
                          "(default, unchanged). 'cattelan' = dot-leader "
                          "'NAME .... N' index with un-indented category "
                          "headers, single running-counter footer. "
                          "'bonaldo' = flat A-Z index (name-block then "
                          "matching number-block, no dot leaders/prefix), "
                          "single running-counter footer (verified 1:1 "
                          "with PDF page index, not just assumed).")
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
    if args.style == "cattelan":
        entries = parse_index_dot_leader(pdf_path, index_pages)
    elif args.style == "bonaldo":
        entries = parse_index_bonaldo(pdf_path, index_pages)
        # this index is alphabetical, NOT page-ordered -- compute_ranges
        # below assumes sorted-by-page input like the other two styles'
        # parsers already return, so sort explicitly here.
        entries.sort(key=lambda x: x[1])
    else:
        entries = parse_index(pdf_path, index_pages)
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
    elif args.style == "bonaldo":
        page_map = build_bonaldo_page_map(pdf_path, total_pages)
        # verified 1:1 identity mapping catalog-wide (see build_bonaldo_page_map's
        # own docstring) -- any page missing from the read-footers pass falls
        # back to identity rather than a formula.
        page_fallback = lambda p: p
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
        if args.merge and name in MERGE_KEEP_EXISTING:
            # This run's own version of this product is known (see
            # MERGE_KEEP_EXISTING's comment) to be less complete than the
            # existing one -- skip writing its mini_pdf/images/text now,
            # not just its catalog_index.json entry later at the merge
            # step below. Writing them unconditionally here and only
            # discarding the JSON entry afterward left the FILES on disk
            # mismatched with the kept entry's page range (confirmed:
            # Flatiron table's catalog_index entry correctly pointed to
            # the main catalog's pages 113-115, but flatiron_table.txt on
            # disk still held the integrazione's pages 8-9 content from
            # this exact overwrite, silently truncating the price parser
            # to a fraction of the real data with no error at all).
            continue
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
        # Products in MERGE_KEEP_EXISTING were already skipped entirely
        # in the extraction loop above (see its own comment) -- their
        # name never enters `catalog`, so the normal "kept = existing
        # entries not in new_names" logic below already preserves them
        # correctly, with no special-casing needed here. (An earlier
        # version of this fix DID special-case them here, AFTER letting
        # the extraction loop generate-then-discard their files -- that
        # order double-counted them into a duplicate catalog_index.json
        # entry once the extraction loop started skipping them instead.)
        protected = [n for n in MERGE_KEEP_EXISTING if any(c["product_name"] == n for c in existing)]
        if protected:
            print(f"\n[merge] Kept the EXISTING entry (not this run's) for "
                  f"{protected} -- verified more complete, see "
                  f"MERGE_KEEP_EXISTING's comment.")
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