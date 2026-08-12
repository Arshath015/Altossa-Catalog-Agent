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

# Bonaldo: products whose literal printed page heading doesn't match their
# catalog product_name closely enough for parse_prices.py's own heading-
# matching (whole-word prefix match, tolerant of digits/punctuation but not
# of a totally different word) to find on its own -- verified individually
# against the real extracted text, not guessed. Populates catalog_index.json's
# "index_heading" field, which parse_prices.py already reads (falls back to
# product_name when absent) -- this dict is the only thing that currently
# writes it for Bonaldo. Found 2026-08-07 while triaging the "own heading not
# found" flag bucket (see project memory bonaldo_index_and_structure).
INDEX_HEADING_OVERRIDES: dict[str, str] = {
    # Punctuation-only mismatch: index name uses a comma, the printed page
    # heading uses " - " instead ("PLANET - BIG PLANET").
    "Planet, Big Planet": "PLANET - BIG PLANET",
    # "Innesti table" and "Innesti coffee table" are two genuinely different
    # products on two different pages (127 and 245) that both print the
    # SAME bare "INNESTI" heading with no disambiguating suffix at all --
    # safe to give both the same override since each is independently
    # scoped to its own page range already.
    "Innesti table": "INNESTI",
    "Innesti coffee table": "INNESTI",
    # Font/rendering quirk, not a text typo: this page's printed heading
    # uses U+00C8 (uppercase E GRAVE, "SALOMÈ") where the catalog index's
    # own product name uses U+00E9 lowercase E ACUTE ("Salomé") -- the two
    # don't match even after uppercasing, since they're different base
    # letters. Confirmed via direct byte inspection of the extracted text
    # (poppler's font-substitution behavior for this one glyph, not
    # something -enc UTF-8 controls -- a new instance of the same family
    # of gotchas as the earlier windows_poppler_gotchas memory).
    "Salomé": "SALOMÈ",
}

# Bonaldo: index entries that are NOT independently-parseable products at
# all -- each is a "variant" name (a lower/alternate model of a sibling
# product, e.g. a shorter-height TV stand) that the printed catalog's own
# alphabetical index happens to list separately, but whose price table is
# ALWAYS printed combined with its base sibling's on the exact same page,
# distinguished only by an internal row-level model_variant/size label
# (e.g. "Dune TV stand"'s own parsed rows already include BOTH
# model_variant="DUNE" and model_variant="DUNE LIGHT" -- confirmed directly
# by inspecting parse_file_bonaldo's output, not guessed). Giving one of
# these its own catalog_index entry can never produce distinct data of its
# own: at best it silently duplicates rows already attributed to the base
# product under a second product_name if the heading-matching happened to
# succeed, at worst (the status quo before this was found) it just sits as
# a permanent "own heading not found" flag. Found 2026-08-07 in the same
# "own heading not found" triage as INDEX_HEADING_OVERRIDES above -- see
# project memory bonaldo_index_and_structure for the full writeup and the
# remaining un-triaged names in this same flag bucket that might turn out
# to be more instances of this once their base sibling's own table shape
# is handled (Frinfri Wood/Nubo boiserie/Paddle TV stand light are
# suspected but not yet confirmed the same way).
DUPLICATE_VARIANT_ENTRIES: set[str] = {
    "Dune TV stand light",   # -> "Dune TV stand", model_variant="DUNE LIGHT"
    "Olos mirror light",     # -> "Olos mirror", model_variant="<size> LIGHT"
    "Salomé light",          # -> "Salomé", size="Salomé light"
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


# ---------------------------------------------------------------------------
# Varaschini (listino_2026_export.pdf) -- structurally unlike the other 3
# brands: there is no per-PRODUCT photographic index at all. Instead there's
# a per-COLLECTION table of contents (39 named collections + 16 back-matter
# reference sections, verified page-by-page against real rendered images and
# text, not trusted from the TOC's own printed numbers alone -- Barcode and
# Belt/Belt Air both proved the TOC number was NOT the true end page), and
# within each collection's page range, individual products ("art. NNNN")
# have to be discovered by scanning the actual page text, not read off an
# index. Five distinct price-table shapes were found (A: fabric-tier B-COM/
# C/D/E/Luxury; B: finish-combo flat price; C: modular diagram+decoupled
# price page, incl. Belt/Belt Air's "SOLUZIONI DI ARREDO" bundle-kit
# sub-pattern; D: dense flat SKU lists; E: Composizione Tavoli's
# combinatorial base x top price matrix -- not in the original A/B/C/D
# hypothesis, only found by testing against real pages).
#
# Because of this, Varaschini gets its own dedicated pipeline
# (run_varaschini) rather than being forced through parse_index/
# compute_ranges/the generic per-product main() loop the other 3 styles
# share -- those all assume exactly one catalog_index entry per index
# item, which doesn't hold here (a single collection page routinely holds
# 2-10+ distinct articles). This mirrors how parse_index_bonaldo/
# parse_index_dot_leader are already brand-specific, just diverging further
# because the SOURCE DATA genuinely has no flat per-product index to parse.
# ---------------------------------------------------------------------------

# Master section table: (name, printed_start, printed_end, shape, category,
# note). Verified empirically against page-banner text using a broadened
# header-matching pass (requiring "DESIGN" or "PROFONDIT" on the same line
# as the collection name to reject cross-reference/matchable-collections
# noise), then spot-checked against real rendered page images for every
# shape and every ambiguous boundary. Confirmed fully contiguous 1-708, no
# gaps or overlaps (checked programmatically, not just by eye).
#
# Shapes: A=fabric-tier single item, B=finish-combo flat price,
#   C=modular diagram+decoupled price page (Belt/Belt Air also contains a
#   "SOLUZIONI DI ARREDO" bundle-kit sub-pattern, not separately modeled
#   here), D=dense flat SKU list, E=combinatorial base x top price matrix,
#   NONPRICED=no price data, must be skipped entirely.
VARASCHINI_SECTIONS: list[tuple] = [
    ("TOC", 1, 5, "NONPRICED", "front", "table of contents"),
    ("Wellness Therapy (teaser)", 6, 7, "NONPRICED", "front", "2-page teaser, full catalogue insert is separate at 524-550"),
    ("Marketing Communication", 8, 9, "D", "reference", "priced display/logo panels"),

    ("Allegra", 10, 11, "A", "collection", None),
    ("Bahia", 12, 15, "A", "collection", None),
    ("Bali", 16, 20, "A", "collection", "includes 'lapis' sub-line by different designer on p16"),
    ("Barcode", 21, 54, "C", "collection", "modular; TOC said '21' only, true end 54 (verified via PROF./PROF. MIX continuation banners)"),
    ("Belt / Belt Air", 55, 131, "C+BUNDLE", "collection", "TOC said '55-100', true end 131 (belt air alone is 100-131); contains SOLUZIONI DI ARREDO bundle-kit pages (composite shape label preserved for exact reproducibility -- parse_prices.py's Shape A filter only checks == 'A', so this doesn't change parser scope either way)"),
    ("Bento", 132, 140, "A", "collection", None),
    ("Big / Big Light", 141, 159, "B", "collection", "table-base finish grid, incl. Big Low/Big Light sub-variants"),
    ("Big In&Out", 160, 161, "A", "collection", None),
    ("Babylon", 162, 167, "A", "collection", None),
    ("Cricket", 168, 176, "A", "collection", None),
    ("Clever", 177, 186, "A", "collection", None),
    ("Customade", 187, 208, "A", "collection", None),
    ("Dolmen", 209, 213, "A", "collection", None),
    ("Ellisse", 214, 220, "A", "collection", None),
    ("Emma", 221, 286, "A", "collection", "large collection, incl Emma Low/Emma Sunscreen sub-variants"),
    ("Emma Cross", 287, 329, "A", "collection", None),
    ("Flexion", 330, 337, "A", "collection", None),
    ("Gianna", 338, 355, "A", "collection", None),
    ("In&Out", 356, 356, "A", "collection", "only 1 page"),
    ("Kolonaki", 357, 360, "A", "collection", None),
    ("Link", 361, 375, "A", "collection", None),
    ("Maat", 376, 377, "A", "collection", None),
    ("Noss", 378, 387, "A", "collection", "incl Noss Low"),
    ("Outdoor Cooking", 388, 396, "D", "collection", "kitchen modules/appliances, flat SKU pricing. NOTE (2026-08-11): internally mixed -- some items are plain bare-code flat-price, others use an 'art.'-prefixed block with extra same-block addon prices (lighting-kit surcharge, a cross-referenced '+ backpanel' variant at a different price) the flat-price parser correctly flags rather than guesses between (42% parse rate). Still labeled 'D' since most of it fits; excluded from the current Shape D parser pass pending a decision on a small addon-price sub-rule vs. staying a permanent flagged known_gap."),
    ("Plinto", 397, 422, "A", "collection", "incl Plinto Low"),
    ("Reuse", 423, 428, "A", "collection", None),
    ("Smart", 429, 435, "A", "collection", "incl Smart Low"),
    ("Summer Set", 436, 454, "A", "collection", "incl Summer Set Low"),
    ("Sunmoon", 455, 462, "A", "collection", None),
    ("System", 463, 477, "B", "collection", "table-base finish grid, incl System Low"),
    ("System Star", 478, 484, "B", "collection", None),
    ("Tibidabo", 485, 501, "A", "collection", "incl 'Loop' table-base variant p501"),
    ("Tight", 502, 502, "A", "collection", "only 1 page"),
    ("Victor", 503, 523, "A", "collection", None),
    ("Wellness Therapy (catalogue)", 524, 550, "A+B+NONPRICED", "collection", "composite: config/design-rules pages (524-530ish) are non-priced sub-pages within this range; component pages use flat-price and fabric-tier shapes (label preserved for exact reproducibility, see Belt/Belt Air note above)"),
    ("Amalfi", 551, 551, "B", "collection", "umbrellas, BASE x TELAIO x COPERTURA fixed combo"),
    ("Copacabana", 552, 552, "B", "collection", "umbrellas"),
    ("Trama", 553, 553, "D", "collection", "blankets, flat SKU list"),
    ("Carpet Design", 554, 554, "D", "collection", "rugs, dense flat SKU grid"),

    ("Outdoor Lighting", 555, 555, "D", "reference", "flat SKU list"),
    ("Strumenti Commerciali", 556, 556, "D", "reference", "sales tools, flat SKU list"),
    ("Teli di Copertura", 557, 569, "REFERENCE_MATRIX", "reference", "NOT a product list -- a compatibility matrix: generic cover codes (e.g. 9400M, priced PER LINEAR METER '/ML', not a flat total) cross-referenced against which OTHER collections' furniture codes each cover fits (bahia/barcode/belt/emma/etc., each at its own size). Reclassified out of 'D' 2026-08-11 after its parser pass came back 0/98 -- structurally unlike anything built so far, needs its own scoping discussion before any parser design (same discipline as Shape E originally)."),
    ("Prodotti per la Pulizia", 570, 570, "D", "reference", "cleaning products, flat SKU list"),
    ("Cuscini e Tessuti", 571, 573, "A", "reference", "reclassified out of 'D' 2026-08-11: NOT a flat SKU list -- confirmed on p571 (art 2713/2709/2708/2701) it's Shape A's exact cat. B-COM/C/D/E/Luxury 5-tier structure, just with bare codes instead of an 'art.' prefix. Needs Shape A's tier-pairing logic combined with Shape D's bare-code block detection, not Shape D's flat-price assumption."),
    ("Composizione Tavoli", 574, 585, "E", "reference", "base x top-size x top-finish price MATRIX -- handled by a dedicated pdftotext -tsv pass, see _varaschini_composizione_tavoli_codes"),
    ("Basi Tavolini", 586, 587, "D", "reference", "table bases alone, flat SKU list (same codes as rows in Composizione Tavoli matrix). NOTE (2026-08-11): still genuinely flat-price (not a mislabeling like Teli di Copertura/Cuscini e Tessuti), but its parser pass came back only 27% correct -- the page is as densely packed as Composizione Tavoli's matrix pages, and line-based code-block-boundary detection bleeds adjacent codes' prices into each other. Still labeled 'D' (that's the real shape); excluded from the current Shape D parser pass, to be revisited using the same pdftotext -tsv coordinate-based technique as Shape E rather than line-based detection."),
    ("Info Tecniche + Specifiche Tecniche Tessuti", 588, 625, "NONPRICED", "reference", "fabric category legend (defines cat. B/C/D/E/Luxury!), material specs, care/maintenance, montage -- all non-priced, 6 languages"),
    ("Imballi (Packaging)", 626, 697, "NONPRICED", "reference", "per-collection box dimensions/weights, non-priced"),
    ("Brand story / Mission & Vision", 698, 704, "NONPRICED", "reference", "not in TOC's 16-item list; marketing content"),
    ("Condizioni Generali di Vendita", 705, 707, "NONPRICED", "reference", "legal sales/warranty terms, 6 languages"),
    ("Contact page", 708, 708, "NONPRICED", "reference", None),
]


def _varaschini_full_text_by_page(pdf_path: str, total_pages: int) -> dict[int, str]:
    """Extract every page's text in ONE batched pdftotext call (not one
    subprocess per page -- confirmed ~5-10x faster: 708 pages in ~1 minute
    batched vs. an estimated 10+ minutes at ~1 call/page) and split it back
    into per-page text using the printed footer ("N - VARASCHIN EXPORT
    2026").

    Two footer quirks, both confirmed against real extracted text, are
    handled here:
      1) Even-numbered pages in the 146-158 range print the footer WITHOUT
         its page number at all (just a bare "VARASCHIN EXPORT 2026" line)
         while their odd-numbered neighbors have the number merged onto a
         content line instead of its own line. Numberless footers are
         inferred by interpolating between the nearest numbered footers
         before and after them, only when that gap is exactly 2 (i.e. the
         missing page is unambiguously "between" two known ones).
      2) Pages 146, 154, 470, and 565 have NO footer trace at all (fully
         bled content, confirmed by direct inspection) -- these 4 specific
         pages are re-extracted individually via pdftotext_page() as a
         fallback patch.
    """
    result = subprocess.run(
        [PDFTOTEXT, "-layout", "-enc", "UTF-8", pdf_path, "-"],
        capture_output=True,
    )
    # Normalize ALL line-ending variants to bare "\n" before splitting.
    # poppler's stdout on Windows mixes "\r\n" and lone "\r" (confirmed by
    # direct byte inspection: 79 CRLF + 80 bare-CR-with-no-following-LF in
    # a single page's output) -- a naive .split("\n") leaves a stray
    # trailing "\r" on roughly half the lines, which broke downstream
    # regex $ -anchoring in parse_prices.py's tier-row matching (814 rows
    # extracted instead of the verified-correct 886 before this was found).
    text = result.stdout.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")

    footer_re = re.compile(r"(\d{1,3})\s*-\s*VARASCHIN EXPORT 2026\s*$")
    bare_footer_re = re.compile(r"^\s*VARASCHIN EXPORT 2026\s*$")

    footers: list[tuple[int, int]] = []
    bare_footer_lines: list[int] = []
    for i, ln in enumerate(lines):
        m = footer_re.search(ln)
        if m:
            footers.append((i, int(m.group(1))))
        elif bare_footer_re.match(ln):
            bare_footer_lines.append(i)

    for bidx in bare_footer_lines:
        before = [(idx, num) for idx, num in footers if idx < bidx]
        after = [(idx, num) for idx, num in footers if idx > bidx]
        if before and after:
            prev_idx, prev_num = max(before, key=lambda x: x[0])
            next_idx, next_num = min(after, key=lambda x: x[0])
            if next_num - prev_num == 2 and next_idx > bidx > prev_idx:
                footers.append((bidx, prev_num + 1))
    footers.sort(key=lambda x: x[0])

    page_text: dict[int, str] = {}
    prev_idx = 0
    for idx, num in footers:
        page_text[num] = "\n".join(lines[prev_idx:idx + 1])
        prev_idx = idx + 1

    for missing_page in (146, 154, 470, 565):
        if missing_page not in page_text:
            page_text[missing_page] = pdftotext_page(pdf_path, missing_page)

    return page_text


_VARASCHINI_CODE_TOKEN = re.compile(r"^[0-9]{3,6}[A-Z]{0,3}[0-9]{0,2}[A-Z]{0,2}$")
_VARASCHINI_ART_PREFIX = re.compile(r"\bart\.?\s+([0-9]{3,6}[0-9A-Z]{0,6})\b", re.IGNORECASE)


def _varaschini_clean_name(s: str) -> str:
    """Collapse internal whitespace runs and reject junk captured instead of
    a real description -- confirmed on Marketing Communication p8, where a
    bare price fragment ("€\\x08 55") got grabbed because the real Italian
    name sits on a line ABOVE the code on Shape D rows, not after it."""
    s = re.sub(r"\s{2,}", " ", s).strip(" -")
    s = s.lstrip("\x08﻿").strip()
    if not s or s.startswith("€") or re.match(r"^[\d.,€\s]+$", s):
        return ""
    return s[:60]


def _varaschini_find_records(page_num: int, text: str) -> list[tuple[str, int, str]]:
    """Find every (code, page, name_guess) triple on one page via 'art.'
    detection. Three physical arrangements of a code relative to its 'art.'
    label are all handled, confirmed against real text across Allegra/
    Bahia/Bali/Dolmen:
      1) same line: "art. 2214" (Belt/Belt Air diagram-grid style)
      2) "art." alone, code some lines BELOW (Allegra/Bahia/System style)
      3) "art." alone, code some lines ABOVE (Dolmen p210: "1820L" prints
         one line before its own bare "art." label)
    """
    records = []
    lines = text.splitlines()
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        for m in _VARASCHINI_ART_PREFIX.finditer(line):
            prefix_ctx = line[max(0, m.start() - 15):m.start()].lower()
            if "cover" in prefix_ctx:
                continue
            name_part = line[m.end():].strip()
            records.append((m.group(1).upper(), page_num, _varaschini_clean_name(name_part)))
        if re.match(r"^art\.?(\s|$)", line, re.IGNORECASE):
            found = False
            for j in range(i + 1, min(i + 6, len(lines))):
                cand_line = lines[j].strip()
                if not cand_line:
                    continue
                first_tok = cand_line.split()[0] if cand_line.split() else ""
                if _VARASCHINI_CODE_TOKEN.match(first_tok):
                    rest = cand_line[len(first_tok):].strip()
                    records.append((first_tok.upper(), page_num, _varaschini_clean_name(rest)))
                    found = True
                    break
                if len(cand_line) > 3 and cand_line[0].islower():
                    break
            if not found:
                for j in range(i - 1, max(i - 3, -1), -1):
                    cand_line = lines[j].strip()
                    if not cand_line:
                        continue
                    first_tok = cand_line.split()[0] if cand_line.split() else ""
                    if _VARASCHINI_CODE_TOKEN.match(first_tok):
                        rest = cand_line[len(first_tok):].strip()
                        records.append((first_tok.upper(), page_num, _varaschini_clean_name(rest)))
                    break
    return records


# Verified false positives from the bare-code detector below: NOT real
# product codes, just a price NUMBER that happened to land as the first
# token of its own physical line due to -layout linearization, satisfying
# the same digit-shape regex as a genuine code. Each entry here was
# confirmed against the real page image before being excluded, same
# category/precedent as Bonaldo's DUPLICATE_VARIANT_ENTRIES.
#   "506" (Carpet Design p554): the per-square-meter price for item "256M"
#   ("€/mq" label on one line, its number "506" landing alone on the NEXT
#   line, ahead of the real next code "256MR" later on that same line) --
#   confirmed by direct inspection of the raw extracted text, not guessed.
#   Format: (page_num, code) -- scoped to the specific page it was found
#   on, not a blanket exclusion of the number everywhere.
VARASCHINI_FALSE_POSITIVE_CODES: set[tuple[int, str]] = {
    (554, "506"),
}

# Collections whose codes are bare (no "art." prefix) even though their
# shape label doesn't imply that on its own -- see the discovery-loop
# comment in run_varaschini() for why this must be decoupled from "shape".
VARASCHINI_FLAT_CODE_DISCOVERY_COLLECTIONS: set[str] = {
    "Cuscini e Tessuti",
}


def _varaschini_find_records_flat(page_num: int, text: str) -> list[tuple[str, int, str]]:
    """Shape D/E fallback: a bare CODE as the first token of its own
    column-chunk, with a '€' within the next 2 lines. Needed because dense
    flat-list shapes print one 'ART./CODE' column header ONCE per page/
    table, not a per-item 'art.' label -- confirmed on Carpet Design p554.

    A line is split on runs of 2+ spaces (matching this codebase's existing
    column-boundary convention, e.g. Bolzan's slice_chunk/tokenize_chunk in
    parse_prices.py) rather than checked only at line-start. Needed for
    genuinely 2-COLUMN pages like Cuscini e Tessuti's, where a left-column
    code (e.g. "2713") and a right-column code (e.g. "2730") share one
    physical line -- confirmed via real output: checking only
    line.split()[0] silently discovered ZERO of the ~18 right-column codes
    on that page (they were never a first token on ANY line at all, not
    merely mis-parsed), a real coverage gap, not just a parsing gap. This
    naturally avoids false-positiving on price VALUES too (a lone 3-digit
    price like "104" always sits in its own "€ 104"-style chunk, so "€",
    not the digits, is that chunk's first token).
    """
    records = []
    lines = text.splitlines()
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        for chunk in re.split(r"\s{2,}", line):
            chunk_toks = chunk.split()
            if not chunk_toks:
                continue
            first_tok = chunk_toks[0]
            if not _VARASCHINI_CODE_TOKEN.match(first_tok):
                continue
            if (page_num, first_tok.upper()) in VARASCHINI_FALSE_POSITIVE_CODES:
                continue
            window = " ".join(lines[i:i + 3])
            if "€" in window:
                rest = chunk[len(first_tok):].strip()
                records.append((first_tok.upper(), page_num, _varaschini_clean_name(rest)))
    return records


def _varaschini_composizione_tavoli_codes(pdf_path: str) -> dict[str, int]:
    """Composizione Tavoli (pages 574-585) is a combinatorial base x top
    price MATRIX, not a per-item list -- per-line 'art.' detection badly
    undercounted it (found 3 real codes instead of the true 119) because
    poppler's -layout text order breaks down for this specific table: a
    row's base code and its price cells can end up many lines apart in the
    linearized text stream. Word-level (x, y) coordinates via
    `pdftotext -tsv` don't have that problem -- every code-shaped token is
    found regardless of its position in the (broken) reading-order text.

    Verified two ways before trusting this: (1) cross-checked the raw code
    list against a direct visual read of page 574's rendered image (matched
    exactly, modulo one false positive), (2) confirmed page 575 is a
    continuation spread (more top-size columns for the SAME base rows, no
    repeated codes) so no double-counting risk between "same-family" pages.

    Returns {code: first_page_seen}. The literal "2026" (the catalog's
    print year, bleeding in from the "N - VARASCHIN EXPORT 2026" footer,
    which happens to match the code-shape regex) is the one confirmed false
    positive and is excluded explicitly, not filtered by regex tightening
    (tightening the regex further risks losing real codes instead).
    """
    result = subprocess.run(
        [PDFTOTEXT, "-tsv", "-enc", "UTF-8", "-f", "574", "-l", "585", pdf_path, "-"],
        capture_output=True,
    )
    tsv_text = result.stdout.decode("utf-8", errors="replace")

    code_re = re.compile(r"^[0-9]{4,6}[A-Z]{0,2}[0-9]{0,1}[A-Z]{0,1}$")
    code_first_page: dict[str, int] = {}
    lines = tsv_text.splitlines()
    if not lines:
        return code_first_page
    for line in lines[1:]:  # skip TSV header row
        parts = line.split("\t")
        if len(parts) < 12 or parts[0] != "5":  # level 5 = word-level token
            continue
        page = int(parts[1])
        text = parts[11]
        if text == "2026":
            continue
        if code_re.match(text) and text not in code_first_page:
            code_first_page[text] = page
    return code_first_page


# PDF column-table headers that occasionally bleed into a captured
# product_name -- confirmed on flat-price accessory/table items, where the
# price-table header line sits close enough to the product name line for
# the name-capture heuristic above to grab it. Stripped as a trailing run
# once it starts, never mid-string, so a legitimately name-containing word
# is never touched.
_VARASCHINI_NAME_JUNK_RE = re.compile(
    r"\s+(?:STRUCTURE|STRUTTURA|IMBOTTITURA|RIVESTIMENTO|UPHOLSTERY|"
    r"COVERING|INTRECCIO|WEAVING|TOP|OUTDOOR|PREZZO|PRICE|/)+\s*$",
    re.IGNORECASE,
)


def _varaschini_strip_name_junk(name: str) -> str:
    prev = None
    s = name
    while prev != s:
        prev = s
        s = _VARASCHINI_NAME_JUNK_RE.sub("", s)
    return s.strip()


def run_varaschini(pdf_path: str, brand: str, out_root: Path) -> None:
    reader = PdfReader(pdf_path)
    total_pages = len(reader.pages)

    print(f"[1/6] Extracting text for all {total_pages} pages (batched)...")
    page_text = _varaschini_full_text_by_page(pdf_path, total_pages)
    print(f"      -> {len(page_text)} pages of text (expect {total_pages - 1}, "
          f"page 1 is the footerless TOC cover)")

    print(f"[2/6] Walking {len(VARASCHINI_SECTIONS)} sections, discovering articles...")
    catalog: list[dict] = []
    for name, start, end, shape, category, _note in VARASCHINI_SECTIONS:
        if shape == "NONPRICED":
            continue
        if name == "Composizione Tavoli":
            continue  # handled separately below (TSV-based, not per-line)

        # Code DISCOVERY method is about the observed page layout (does a
        # code have an "art." label, or is it bare?) -- it must NOT be
        # coupled to the "shape" field, which is about the PRICE TABLE
        # format and can legitimately differ from the layout. Confirmed by
        # a real regression: relabeling Cuscini e Tessuti from "D" to "A"
        # (its price table matches Shape A's tier structure) silently
        # dropped all 18 of its entries, because its codes are bare (no
        # "art." prefix) and the flat-code fallback below was gated on
        # shape in ("D", "E") -- losing "D" lost the only detection method
        # that could ever find them, even though nothing about their LAYOUT
        # changed. VARASCHINI_FLAT_CODE_DISCOVERY_COLLECTIONS keeps the
        # layout-driven decision independent of the shape label.
        section_records: dict[str, list] = {}  # code -> [p_first, p_last, name]
        for p in range(start, end + 1):
            text = page_text.get(p, "")
            recs = _varaschini_find_records(p, text)
            if shape in ("D", "E") or name in VARASCHINI_FLAT_CODE_DISCOVERY_COLLECTIONS:
                recs += _varaschini_find_records_flat(p, text)
            for code, page, nm in recs:
                if code not in section_records:
                    section_records[code] = [page, page, nm]
                else:
                    section_records[code][1] = max(section_records[code][1], page)
                    if not section_records[code][2] and nm:
                        section_records[code][2] = nm

        for code, (p_start, p_end, nm) in sorted(section_records.items()):
            product_name = f"{name} {nm}".strip() if nm else f"{name} {code}"
            catalog.append({
                "brand": brand,
                "collection": name,
                "product_name": product_name,
                "art_code": code,
                "printed_page_start": p_start,
                "printed_page_end": p_end,
                "pdf_page_start": p_start,
                "pdf_page_end": p_end,
                "shape": shape,
                "section_category": category,
            })

    print("[3/6] Composizione Tavoli (Shape E): dedicated pdftotext -tsv pass...")
    ct_codes = _varaschini_composizione_tavoli_codes(pdf_path)
    for code, page in sorted(ct_codes.items()):
        catalog.append({
            "brand": brand,
            "collection": "Composizione Tavoli",
            "product_name": f"Composizione Tavoli {code}",
            "art_code": code,
            "printed_page_start": page,
            "printed_page_end": page,
            "pdf_page_start": page,
            "pdf_page_end": page,
            "shape": "E",
            "section_category": "reference",
        })
    print(f"      -> {len(ct_codes)} verified codes")

    print("[4/6] Cleaning + disambiguating product names...")
    for e in catalog:
        e["product_name"] = _varaschini_strip_name_junk(e["product_name"])
    name_counts: dict[str, int] = {}
    for e in catalog:
        name_counts[e["product_name"]] = name_counts.get(e["product_name"], 0) + 1
    dupe_names = {n for n, c in name_counts.items() if c > 1}
    disambiguated = 0
    for e in catalog:
        if e["product_name"] in dupe_names:
            e["index_heading"] = e["product_name"]
            e["product_name"] = f"{e['product_name']} ({e['art_code']})"
            disambiguated += 1
    print(f"      -> {disambiguated} entries disambiguated across {len(dupe_names)} colliding names")

    print("[5/6] Assigning page-keyed shared asset paths...")
    # Assets are deduped BY PAGE (not duplicated per product like the other
    # 3 brands): Varaschini averages ~2.5 catalog entries per page, so
    # per-entry duplication would multiply the real 517-page asset count
    # into 1,300+ redundant copies of the same content. Reviewed against
    # the actual image-lookup code before adopting this (catalogChat.ts's
    # getImageUrls() and stress_v2.ts's expected-image computation both
    # already resolve images via page_images[pageNum], not by entry
    # identity, so multiple entries sharing one filename is natively
    # supported, not a special case). NOTE: printed_page_start/end still
    # reflect the real first/last page a code was found on (can differ --
    # e.g. an accessory code mentioned as a cross-reference on an earlier
    # page before its own price table) -- only the ASSET FILENAME uses
    # printed_page_start, matching the page whose content the parser
    # should actually look at.
    for e in catalog:
        p = e["printed_page_start"]
        fname = f"p{p:03d}"
        e["mini_pdf"] = f"{brand}\\pages\\{fname}.pdf"
        e["images"] = [f"{fname}.jpg"]
        e["page_images"] = {str(p): f"{fname}.jpg"}
        e["text_file"] = f"{brand}\\text\\{fname}.txt"

    pages_needed = sorted({e["printed_page_start"] for e in catalog})
    print(f"      -> {len(pages_needed)} distinct pages referenced")

    print(f"[6/6] Generating {len(pages_needed)} mini-PDFs, images, and text files...")
    (out_root / "pages").mkdir(parents=True, exist_ok=True)
    (out_root / "images").mkdir(parents=True, exist_ok=True)
    (out_root / "text").mkdir(parents=True, exist_ok=True)

    for p in pages_needed:
        writer = PdfWriter()
        writer.add_page(reader.pages[p - 1])
        with open(out_root / "pages" / f"p{p:03d}.pdf", "wb") as f:
            writer.write(f)
        (out_root / "text" / f"p{p:03d}.txt").write_text(page_text.get(p, ""), encoding="utf-8")

    # Images: batched pdftoppm calls over contiguous page runs (not one
    # subprocess per page) for the same efficiency reason as step 1 --
    # confirmed ~10x faster in practice (11 pages in ~2s batched vs. an
    # estimated ~1s/page one-call-per-page).
    runs = []
    if pages_needed:
        run_start = prev = pages_needed[0]
        for p in pages_needed[1:]:
            if p == prev + 1:
                prev = p
                continue
            runs.append((run_start, prev))
            run_start = prev = p
        runs.append((run_start, prev))
    for a, b in runs:
        subprocess.run(
            [PDFTOPPM, "-jpeg", "-r", "150", "-f", str(a), "-l", str(b),
             pdf_path, str(out_root / "images" / "_tmp")],
            capture_output=True,
        )
    for f in (out_root / "images").glob("_tmp-*.jpg"):
        page_num = int(f.stem.split("-")[-1])
        f.rename(out_root / "images" / f"p{page_num:03d}.jpg")

    catalog_path = out_root / "catalog_index.json"
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDone. Wrote {len(catalog)} entries to {catalog_path}")
    print("Spot-check a few entries and open a couple of the generated files")
    print("before treating this as ready for parser work.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf", help="Path to the brand price-list PDF")
    ap.add_argument("--brand", required=True, help="Brand name, e.g. Bolzan")
    ap.add_argument(
        "--index-pages",
        required=False,
        default=None,
        help="PDF page range of the photographic index, e.g. 3-8. Not "
             "applicable (and not required) for --style varaschini, which "
             "has no per-product index to point at.",
    )
    ap.add_argument("--out", default="./data", help="Output root folder")
    ap.add_argument("--style", default="bolzan",
                     choices=["bolzan", "cattelan", "bonaldo", "varaschini"],
                     help="Index format + page-footer style. 'bolzan' = "
                          "'p.N' index, two-number-per-spread footer "
                          "(default, unchanged). 'cattelan' = dot-leader "
                          "'NAME .... N' index with un-indented category "
                          "headers, single running-counter footer. "
                          "'bonaldo' = flat A-Z index (name-block then "
                          "matching number-block, no dot leaders/prefix), "
                          "single running-counter footer (verified 1:1 "
                          "with PDF page index, not just assumed). "
                          "'varaschini' = no per-product index at all -- "
                          "a hardcoded, verified per-collection section "
                          "table (VARASCHINI_SECTIONS) plus per-page "
                          "article discovery instead. Runs its own "
                          "self-contained pipeline (run_varaschini),"
                          " ignores --index-pages and --merge.")
    ap.add_argument("--merge", action="store_true",
                     help="Merge into an existing catalog_index.json instead "
                          "of overwriting it: entries from this run replace "
                          "any existing entry with the same product_name "
                          "(kept, not duplicated), everything else in the "
                          "existing file is preserved as-is.")
    args = ap.parse_args()

    pdf_path = args.pdf

    if args.style == "varaschini":
        out_root = Path(args.out) / args.brand
        run_varaschini(pdf_path, args.brand, out_root)
        return

    if not args.index_pages:
        print("ERROR: --index-pages is required for --style "
              f"{args.style!r} (only 'varaschini' can omit it).")
        sys.exit(1)

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
        dropped = [name for name, _ in entries if name in DUPLICATE_VARIANT_ENTRIES]
        if dropped:
            print(f"      -> dropping {len(dropped)} duplicate-variant index "
                  f"entr{'y' if len(dropped) == 1 else 'ies'} (see "
                  f"DUPLICATE_VARIANT_ENTRIES): {dropped}")
        entries = [(name, page) for name, page in entries if name not in DUPLICATE_VARIANT_ENTRIES]
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

        entry = {
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
        }
        if name in INDEX_HEADING_OVERRIDES:
            entry["index_heading"] = INDEX_HEADING_OVERRIDES[name]
        catalog.append(entry)

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