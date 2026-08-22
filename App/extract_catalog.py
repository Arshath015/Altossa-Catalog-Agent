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
    # Ditre Italia: same "last entry in the index -> no next-entry to
    # bound it -> auto-extend to end of document" failure, hit 5 times
    # across the 5 source PDFs (each file's own last TOC entry is always
    # an accessory group, immediately followed by that file's back-matter
    # "MATERIALS, FINISHINGS AND WARNING" / "Technical ... section" /
    # "Model comparison by price bracket" reference pages -- confirmed by
    # rendering every page in each auto-computed range, not assumed from
    # the pattern alone). All 5 found during the post-extraction sanity
    # pass, not before -- worth checking for on any future last-entry-in-
    # a-Ditre-file case too.
    "The breath": (302, 303),  # auto-computed 302-305; 304-305 = "Model comparison by price bracket"
    "Cushions - Fabrics and Leathers (Armchairs)": (78, 83),  # auto-computed 78-85; 84-85 = "MATERIALS, FINISHINGS AND WARNING"
    "Bed-base cover for sofa bed": (120, 121),  # auto-computed 120-123; 122-123 = "Technical bed/sofa-bed section"
    "Cushions - Headrests - Fabrics and Leathers (Living & Dining)": (142, 151),  # auto-computed 142-153; 152-153 = "Marble finishes" / "Wood, glass and bonded leather finishes"
    "Outdoor cushions": (60, 63),  # auto-computed 60-65; 64-65 = "Materials | Matériels"
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

# Pianca: names known (via flag_triage.json, real page content -- not
# assumed) to collide ACROSS different source files, or to be a stale
# reprint that shouldn't be indexed at all. parse_index_pianca's own
# in-file "Name (Category)" auto-qualification (see its own comment) only
# catches recurrence WITHIN one file's own index scan -- it has no idea
# a bare name it just emitted also belongs to a DIFFERENT file's product,
# or that a bare/auto-qualified name is a confirmed stale duplicate of an
# already-live entry. Confirmed the hard way 2026-08-21: CollezioneGiorno's
# first extraction run silently overwrote Progetti di Design 08's live,
# already-parsed "Peonia (Divani)" (408 real price rows) via --merge's
# plain "same product_name wins" rule, because CollezioneGiorno's OWN
# index also auto-qualified its (unrelated, older, ~4%-stale) Peonia as
# "Peonia (Divani)" -- an exact string collision neither side could see
# coming from its own file alone. Same run also overwrote Spazi-10's
# "Cornice". Both had to be restored by re-running their source files'
# extraction again to win the merge back.
#
# Keyed by (source PDF filename, name AS PARSE_INDEX_PIANCA WOULD RETURN
# IT -- i.e. already auto-qualified by in-file category if that fired).
# Value is the desired final product_name, or None to drop the entry
# entirely (confirmed superseded_reprint / duplicate -- never written to
# disk, never enters catalog_index.json). Applied once, right after
# compute_ranges, before ANY per-item extraction work -- so an excluded
# entry never even gets a mini_pdf/image/text file generated for it.
PIANCA_INDEX_NAME_OVERRIDES: dict[tuple[str, str], str | None] = {
    # CollezioneGiorno (Listino 01 Settembre 2023)
    ('2023_09_CollezioneGiorno_1R_+10_.pdf', 'Cornice'): None,  # superseded_reprint -- see flag_triage.json, Spazi-10's version is current
    ('2023_09_CollezioneGiorno_1R_+10_.pdf', 'Peonia (Divani)'): None,  # superseded_reprint -- Progetti di Design 08's version is current
    ('2023_09_CollezioneGiorno_1R_+10_.pdf', 'Peonia (Poltrone e pouf)'): None,  # same product, same supersede decision
    ('2023_09_CollezioneGiorno_1R_+10_.pdf', 'Dedalo'): 'Dedalo (CollezioneGiorno)',
    ('2023_09_CollezioneGiorno_1R_+10_.pdf', 'Logos'): 'Logos (CollezioneGiorno)',
    ('2023_09_CollezioneGiorno_1R_+10_.pdf', 'Contralto'): 'Contralto (CollezioneGiorno)',
    ('2023_09_CollezioneGiorno_1R_+10_.pdf', 'People'): 'People (CollezioneGiorno)',
    # CollezioneNotte (Listino 01 Settembre 2023)
    ('2023_09_CollezioneNotte_1R_+10_.pdf', 'Dedalo'): 'Dedalo (CollezioneNotte)',
    ('2023_09_CollezioneNotte_1R_+10_.pdf', 'Logos'): 'Logos (CollezioneNotte)',
    ('2023_09_CollezioneNotte_1R_+10_.pdf', 'Contralto'): 'Contralto (CollezioneNotte)',
    ('2023_09_CollezioneNotte_1R_+10_.pdf', 'People'): 'People (CollezioneNotte)',
    # Progetti di Design 06-07 (Listino 01 Ottobre 2023)
    ('2023_10_Progetti_di_Design_06-07_1R +6_.pdf', 'Dedalo'): 'Dedalo (Progetti 06-07)',

    # Second round, found 2026-08-22 while extracting CollezioneNotte:
    # Palù and "Mensole legno per boiserie" collided with Progetti di
    # Design 08's and Spazi-10's own already-live bare names respectively
    # -- confirmed genuine collisions (Palù: same designer, Raffaella
    # Mangiarotti, but Progetti 08's is a Sedia/chair vs CollezioneNotte's
    # Comodino/nightstand; Mensole legno per boiserie: completely
    # different SKU code families, Spazi-10's 46D-prefix vs
    # CollezioneNotte's 46E-prefix). Per the established convention,
    # BOTH sides get qualified, not just the newly-colliding one -- the
    # already-live Progetti 08/Spazi-10 entries are renamed here too.
    ('2024_10_Progetti_di_Design_08_1R +6_.pdf', 'Palù'): 'Palù (Progetti 08)',
    ('2023_09_CollezioneNotte_1R_+10_.pdf', 'Palù'): 'Palù (CollezioneNotte)',
    ('2026_02_Spazi-10_1R.pdf', 'Mensole legno per boiserie'): 'Mensole legno per boiserie (Spazi-10)',
    ('2023_09_CollezioneNotte_1R_+10_.pdf', 'Mensole legno per boiserie'): 'Mensole legno per boiserie (CollezioneNotte)',

    # CollezioneGiorno x CollezioneNotte, found 2026-08-22: 4 more
    # colliding names, each investigated at the code level (not text
    # similarity -- see the standing rule in flag_triage.json's
    # superseded_reprint _status_meaning) and confirmed to be HYBRIDS:
    # each catalog's version is primarily its own distinct content
    # (different products' customization/surcharge codes), but a small
    # number of codes are genuinely universal brand-wide accessories
    # verified IDENTICAL (same code, same price) in both -- e.g.
    # Elettrificazione's IR system (5000IR = EUR 221, 5000IS = EUR 26,
    # both catalogs exactly) and passacavi (5000A = EUR 15, both
    # catalogs exactly); Lavorazioni su misura's custom-color surcharge
    # (LSCC = EUR 364, both catalogs exactly). Not a supersede (no price
    # delta at all on the shared codes) and not fully identical either
    # (the bulk of each is catalog-specific) -- disambiguated like any
    # other genuine collision. The few universal codes will legitimately
    # appear as identical rows under both qualified entries once parsed;
    # that's expected and harmless (same code, same price, no ambiguity),
    # not a duplicate-data bug.
    ('2023_09_CollezioneGiorno_1R_+10_.pdf', 'Elettrificazione'): 'Elettrificazione (CollezioneGiorno)',
    ('2023_09_CollezioneNotte_1R_+10_.pdf', 'Elettrificazione'): 'Elettrificazione (CollezioneNotte)',
    ('2023_09_CollezioneGiorno_1R_+10_.pdf', 'Lavorazioni su misura'): 'Lavorazioni su misura (CollezioneGiorno)',
    ('2023_09_CollezioneNotte_1R_+10_.pdf', 'Lavorazioni su misura'): 'Lavorazioni su misura (CollezioneNotte)',
    ('2023_09_CollezioneGiorno_1R_+10_.pdf', 'Maggiorazione Ottone Anticato'): 'Maggiorazione Ottone Anticato (CollezioneGiorno)',
    ('2023_09_CollezioneNotte_1R_+10_.pdf', 'Maggiorazione Ottone Anticato'): 'Maggiorazione Ottone Anticato (CollezioneNotte)',
    # Norma: same hybrid shape (a shared "Basamento" leg/base wildcard-
    # code sub-table alongside genuinely different main furniture pieces
    # -- CollezioneGiorno's own madia, codes 0073FF/0074FF/0075FF etc.,
    # vs CollezioneNotte's own comodino, code 2N2Y4) -- same resolution.
    ('2023_09_CollezioneGiorno_1R_+10_.pdf', 'Norma'): 'Norma (CollezioneGiorno)',
    ('2023_09_CollezioneNotte_1R_+10_.pdf', 'Norma'): 'Norma (CollezioneNotte)',
}

# A second, separate class of the same problem (same pattern already hit
# and fixed for Ditre Italia -- see DITRE_WITHIN_FILE_DISAMBIGUATION's own
# comment): a name can recur MULTIPLE TIMES within one file's own INDICE
# under the SAME category, which parse_index_pianca's in-file "Name
# (Category)" auto-qualification can't distinguish (it only has one
# category label to work with, and several genuinely different products
# can share it). Confirmed on Outdoor's "Levante Out" -- 6 of its 9
# occurrences (Poltrona, Lettino, Lettino plus, Lettino super, Panca,
# Panca super) all fall under the single INDICE category "Poltrone,
# panche e lettini", so the auto-qualifier would produce the exact same
# "Levante Out (Poltrone, panche e lettini)" string for all 6 -- a
# collision the (source_file, name) table above can't resolve either,
# since all 6 raw entries share one key. Printed page number is the only
# thing that reliably distinguishes them. Every value below verified via
# direct page content (designer credit + a distinct furniture-type word
# and/or SKU code family), not assumed from the INDICE listing alone --
# see flag_triage.json's "Levante Out" entry for the full evidence.
PIANCA_WITHIN_FILE_OVERRIDES: dict[tuple[str, str, int], str] = {
    ('2024_10_Outdoor_1R SENZA AUMENTO +10.pdf', 'Levante Out', 5): 'Levante Out (Sedia)',
    ('2024_10_Outdoor_1R SENZA AUMENTO +10.pdf', 'Levante Out', 6): 'Levante Out (Sgabello)',
    ('2024_10_Outdoor_1R SENZA AUMENTO +10.pdf', 'Levante Out', 13): 'Levante Out (Divano)',
    ('2024_10_Outdoor_1R SENZA AUMENTO +10.pdf', 'Levante Out', 21): 'Levante Out (Poltrona)',
    ('2024_10_Outdoor_1R SENZA AUMENTO +10.pdf', 'Levante Out', 22): 'Levante Out (Lettino)',
    ('2024_10_Outdoor_1R SENZA AUMENTO +10.pdf', 'Levante Out', 23): 'Levante Out (Lettino plus)',
    ('2024_10_Outdoor_1R SENZA AUMENTO +10.pdf', 'Levante Out', 24): 'Levante Out (Lettino super)',
    ('2024_10_Outdoor_1R SENZA AUMENTO +10.pdf', 'Levante Out', 25): 'Levante Out (Panca)',
    ('2024_10_Outdoor_1R SENZA AUMENTO +10.pdf', 'Levante Out', 26): 'Levante Out (Panca super)',
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


# ---------------------------------------------------------------------------
# Ditre Italia -- like Bonaldo, a flat two-column "NNN  Name" index with no
# dot leaders, so it reuses the same -tsv coordinate approach and
# _bonaldo_index_split_x helper for the left/right column split. But Ditre's
# column layout is the mirror image of Bonaldo's (page-number FIRST, name
# SECOND, vs. Bonaldo's name-then-number) -- and critically, at least 4
# confirmed product names across the 6 source PDFs are themselves bare
# 3-digit numbers ("356", an armchair/living-chair/outdoor-sofa/outdoor-chair
# model name). A first-word-is-a-1-4-digit-number heuristic alone would
# misread "356" as a page number and corrupt the following entry's page
# (confirmed by hand: an early pass did exactly this). The fix, verified via
# direct pdftotext -tsv inspection: a REAL page number and a product name
# occupy two visually distinct x-position bands within a column (e.g. on
# armchairs2026's TOC, real page numbers sit at left=~59pt while "356" the
# product name sits at left=~95pt -- the exact same x as "Alta", a normal
# name). So instead of trusting any 1-4-digit first word, this computes the
# actual page-number x-band per column (the x-position shared by MOST rows'
# first word) and only accepts a leading number if it falls in that band.
KNOWN_DITRE_TOC_LABELS = {
    # Section headers (both indoor product-type sections and the shared
    # back-matter sections repeated near-verbatim across all 6 source PDFs).
    # Transcribed directly from each file's own "Products Index" page, not
    # guessed -- see the step-1/step-2 conversation transcript this was
    # built from for the source of each line.
    "SOFA / SOFA", "ARMCHAIRS / ARMCHAIRS", "CHAIRS / CHAISES",
    "TABLES / TABLES", "SMALL TABLES / TABLES BASSES",
    "SIDEBOARDS / ENFILADES", "BOOKCASE / BIBLIOTHÈQUE",
    "MIRRORS / MIROIR", "CARPETS / TAPISS", "POUFF / POUF",
    "ACCESSORIES / ACCESSOIRES",
    "MATERIALS, FINISHINGS AND WARNING /",
    "MATÉRIAUX, FINITIONS ET AVERTISSEMENTS",
    "MATERIALS, FINISHINGS AND WARNING / MATÉRIAUX, FINITIONS ET AVERTISSEMENTS",
    "SALE GENERAL TERMS /", "CONDITIONS GÉNÉRALES DE VENTE",
    "SALE GENERAL TERMS / CONDITIONS GÉNÉRALES DE VENTE",
    "INFORMATION / INFORMATIONS",
    "Beds / Letti", "Beds / Lits", "Sofa bed / Divani letto",
    "Sofa bed / Canapés convertibles",
    "Bed side tables / Comodini", "Drawers / Tables de chevet",
    "Accessories for beds / Accessori per letti",
    "Accessories for beds / Accessoires pour lits:",
    "Not available bed / Letti non disponibili",
    "Materials, finishes and warnings /",
    "Materiali, finiture e avvertenze",
    "Materials, finishes and warnings / Materiali, finiture e avvertenze",
    "Materials, finishings and warning /",
    "Matériaux, finitions et avertissements",
    "Materials, finishings and warning / Matériaux, finitions et avertissements",
    "Composition as per catalogue /", "Composizioni da catalogo",
    "Composition as per catalogue / Composizioni da catalogo",
    "Compositions du catalogue",
    "Composition as per catalogue / Compositions du catalogue",
    "Model comparison by price bracket", "Composition as per catalogue",
    "Fabrics samples", "Configuration", "Sale general terms", "Care Kit",
    "Technical bed section", "Technical sofa-bed section",
    # NOTE: "Bed-base cover for bed" / "Bed-base cover for sofa bed" are
    # NOT in this skip-list -- unlike the surrounding reference/appendix
    # entries, these are real priced accessory products (see the step-2
    # conversation's golden recount, which explicitly counts them among
    # night2024/night2026's 3 accessory groups), and were wrongly skip-
    # listed here on the first implementation pass, silently dropping both
    # from parse_index_ditre's output. Caught by the golden-count diff.
    "Marble finishes", "Wood, glass and bonded leather finishes",
    "Upholstering used in the pictures of the",
    "Upholstering used in the pictures of the catalogue",
    "Finishes and materials used",
    "Materials | Matériels",
    "Outdoor Fabric Samples",
    "Upholstery and finishes / Revêtements et finitions",
}

# Two DISTINCT trailing text blocks sit below the real index content on
# every source PDF's TOC page, at different vertical positions but both in
# the same x-range as whichever column's last real entry happens to be
# nearest -- neither carries a page number of its own, so neither can be
# caught by KNOWN_DITRE_TOC_LABELS' exact-match approach (both vary by
# file/date/language). The first is a "-- This price list cancels and
# replaces the previous... / Valid from October 1st, 2024" legal notice.
# The second (found only after the first version of this regex still let
# "Isabel sofa bed" absorb a trailing "Night - Price list / Tarif" on
# night2026 -- confirmed by hand, not assumed fixed) is a running document
# title/date line ("Night - Price list / Tarif 10/2024", "Sofa Collection
# - Price list / Tarif 06/2026", "Armchairs - Price list / Tarif
# 06/2026" ...). The first pattern group below catches the legal notice;
# "price list /" and "listino" catch the running title line generically
# (every sampled instance contains one of those two substrings).
DITRE_LEGAL_FOOTER_RE = re.compile(
    r"cancels and replaces|annule et remplace|annulla e sostituisce"
    r"|effective from|en vigueur|valid from|valable|in vigore"
    r"|price list\s*/|listino",
    re.IGNORECASE,
)

# Ditre Italia is the FIRST brand in this project built from multiple
# separate PDFs merged into one brand folder (Bolzan/Cattelan/Bonaldo are
# each a single PDF; Varaschini is self-contained). That surfaced a new
# failure mode none of the other brands' data ever exercised: Ditre
# deliberately reuses model/design names across furniture categories (a
# "Cali" sofa and a "Cali" armchair are two real, different products that
# share a design-family name) -- but --merge's "same product_name = same
# real product, newer run wins" logic silently treated the second file's
# entry as an UPDATE of the first's, both in catalog_index.json AND on
# disk (confirmed by hand: only one cali.pdf existed after merging
# armchairs2026 into an already-extracted sofa2026 catalog -- the sofa
# version's mini_pdf/images/text were physically overwritten, not just
# dropped from the index, since slug uniqueness is only tracked within a
# single run's used_slugs dict, not across merged runs).
#
# Checked every pairwise combination of the 5 target files (not just the
# one pair that already caused data loss): 20 distinct names collide,
# several across 3 files at once (e.g. "Cali": sofa+armchairs+living).
# outdoor2025 has zero collisions with anything (its names are already
# suffixed "outdoor").
#
# Fix: disambiguate every colliding name with its own REAL printed
# category label, verified per name by reading the actual page (not
# guessed) -- sofa2026/armchairs2026 print a category badge top-right on
# every page ("Cali ... SOFA", "Cali ... ARMCHAIRS"); living2026's badge
# varies BY PRODUCT within the one file ("Cali ... CHAIRS", "Avalon ...
# SMALL TABLES", "Claire ... TABLES" -- confirmed each individually, not
# assumed uniform); night2026's bed/sofa-bed pages print no per-page
# category badge at all (confirmed by direct check), so those use the
# file's own real cover title ("DITRE ITALIA -- NIGHT") instead of an
# invented "(Bed)" label, since the file's collisions span both beds and
# sofa-bed-adjacent accessory entries.
DITRE_NAME_DISAMBIGUATION: dict[str, dict[str, str]] = {
    "sofa": {
        "Ada": "Ada (Sofa)", "Avalon": "Avalon (Sofa)", "Cali": "Cali (Sofa)",
        "Clip": "Clip (Sofa)", "Deck": "Deck (Sofa)", "Isla": "Isla (Sofa)",
        "Krisby": "Krisby (Sofa)", "Loman 2.0": "Loman 2.0 (Sofa)",
        "Melville": "Melville (Sofa)", "Pacific": "Pacific (Sofa)",
        "St. Germain": "St. Germain (Sofa)", "Urban 2.0": "Urban 2.0 (Sofa)",
        "Vento": "Vento (Sofa)",
        "Cushions - Headrests - Fabrics and Leathers":
            "Cushions - Headrests - Fabrics and Leathers (Sofa)",
    },
    "armchairs": {
        "Cali": "Cali (Armchairs)", "Clip": "Clip (Armchairs)",
        "Isla": "Isla (Armchairs)", "Krisby": "Krisby (Armchairs)",
        "Melville": "Melville (Armchairs)", "Pacific": "Pacific (Armchairs)",
        "St. Germain": "St. Germain (Armchairs)", "Vento": "Vento (Armchairs)",
        "Bend": "Bend (Armchairs)", "Puppet": "Puppet (Armchairs)",
        "Cushions - Fabrics and Leathers": "Cushions - Fabrics and Leathers (Armchairs)",
    },
    "living": {
        "Cali": "Cali (Chairs)",
        "Avalon": "Avalon (Small Tables)", "Deck": "Deck (Small Tables)",
        "Loman 2.0": "Loman 2.0 (Small Tables)", "Kailua": "Kailua (Small Tables)",
        "Skin": "Skin (Small Tables)", "Urban 2.0": "Urban 2.0 (Small Tables)",
        "Claire": "Claire (Tables)",
        "Cushions - Headrests - Fabrics and Leathers":
            "Cushions - Headrests - Fabrics and Leathers (Living & Dining)",
    },
    "night": {
        "Ada": "Ada (Night)", "Avalon": "Avalon (Night)", "Bend": "Bend (Night)",
        "Clip": "Clip (Night)", "Claire": "Claire (Night)", "Kailua": "Kailua (Night)",
        "Pacific": "Pacific (Night)", "Puppet": "Puppet (Night)", "Skin": "Skin (Night)",
        "Cushions - Fabrics and Leathers": "Cushions - Fabrics and Leathers (Night)",
    },
}

# A SECOND, separate class of the same problem, found during the post-
# extraction sanity pass: 6 names repeat WITHIN a single file (a different
# product on a different page happens to share the bare name), which
# DITRE_NAME_DISAMBIGUATION above doesn't touch at all (it's keyed by name
# only, since every cross-file collision had exactly one occurrence per
# file). These don't cause file-overwrite data loss the way the cross-file
# case did (main()'s own used_slugs collision handling already keeps their
# mini_pdf/images/text on disk under distinct slugs, e.g. "arcade" /
# "arcade_2") but they DO leave two really-different catalog_index entries
# with the exact same product_name, which is the same ambiguity risk for
# downstream matching. Same fix, same verification discipline (every label
# below read off the real page, not guessed) -- keyed by (name,
# printed_page) since the SAME raw name needs a DIFFERENT suffix per
# occurrence, unlike the cross-file table where one override per name was
# enough.
DITRE_WITHIN_FILE_DISAMBIGUATION: dict[str, dict[tuple[str, int], str]] = {
    "living": {
        ("Arcade", 6): "Arcade (Tables)", ("Arcade", 46): "Arcade (Small Tables)",
        ("Biarritz", 10): "Biarritz (Tables)", ("Biarritz", 32): "Biarritz (Chairs)",
        ("Nell", 22): "Nell (Tables)", ("Nell", 70): "Nell (Small Tables)",
        ("Petra", 72): "Petra (Small Tables)", ("Petra", 116): "Petra (Sideboards)",
        ("Unit", 90): "Unit (Small Tables)", ("Unit", 120): "Unit (Sideboards)",
        ("Unit", 126): "Unit (Bookcase)",
    },
    "outdoor": {
        ("Isamu outdoor", 10): "Isamu outdoor (Sofa)",
        ("Isamu outdoor", 36): "Isamu outdoor (Tables)",
    },
}


def _ditre_file_key(pdf_path: str) -> str:
    """Map a Ditre source PDF path to the short key DITRE_NAME_DISAMBIGUATION
    is keyed by. Matched on distinctive filename substrings (each source
    filename is unique enough that this can't cross-match another file)."""
    base = Path(pdf_path).name.lower()
    if "sofacollection" in base:
        return "sofa"
    if "armchairs" in base:
        return "armchairs"
    if "living" in base or "dining" in base:
        return "living"
    if "night" in base:
        return "night"
    if "outdoor" in base:
        return "outdoor"
    return ""


def parse_index_ditre(pdf_path: str, index_pages: range) -> list[tuple[str, int]]:
    """Parse Ditre Italia's "Products Index" page(s): a flat two-column
    "NNN  Name" list (no dot leaders, no "p." prefix -- number comes FIRST
    on each row, unlike Bonaldo's name-then-number). See this section's
    module comment for why a naive "first word is a number" rule breaks on
    the bare-numeric product name "356".

    Unlike parse_index_bonaldo, this does NOT pre-split the whole page into
    a left half and a right half by one global x-coordinate. A first
    attempt at that (using the same "widest gap closest to page half-width"
    heuristic as _bonaldo_index_split_x) put the split point at x=331.5 on
    sofa2026's TOC while a genuine right-column page number sat at
    x=331.32 -- 0.18pt on the wrong side, silently merging that whole
    right-column row into the left column's entry. Also, the number token
    and its own name token on "the same" printed row don't always share an
    identical `top` (e.g. "008" at top=77.69, "Ada" at top=78.58 -- these
    round to DIFFERENT integers, so grouping by round(top) would split one
    real row into two). Both confirmed by direct -tsv inspection, not
    guessed.

    The fix: detect visual ROWS first, globally, by clustering ALL words
    (regardless of column) within a small top-tolerance -- then, within
    each row, split at whatever gap is actually the column gutter (~200pt+
    on every sampled page) rather than a page-wide constant. Within-column
    word spacing (e.g. between "Papilo" and "plain" and "-" and "curvy" in
    one long wrapped name) never exceeded ~35pt on any sampled page, so an
    80pt threshold cleanly separates "still the same column" from "this is
    the OTHER column's content on the same printed row" with margin on
    both sides.
    """
    # 1.5 was tried first and confirmed too tight: living2026's "128  Boxy
    # Eric Helios Multitude" row has the number at top=286.99 and the name
    # at top=288.64 (a 1.65pt gap). 2.5 was tried next and STILL too tight:
    # night2026's "106  Aany - Eric - Erys - Pop - Puppet - Skin" row has
    # the number at top=179.74 and the name at top=182.26 -- a 2.52pt gap,
    # 0.02pt over that threshold. Both cases silently dropped the whole
    # entry (number-only "row" with no name, name-only "row" with no
    # number to attach to). 3.0 covers both with a little headroom, and
    # real distinct rows are still ~15-17pt apart on every sampled page,
    # so there's no risk of merging two genuine entries at this tolerance.
    ROW_TOP_TOLERANCE = 3.0
    COLUMN_GUTTER_MIN_GAP = 80.0
    name_overrides = DITRE_NAME_DISAMBIGUATION.get(_ditre_file_key(pdf_path), {})
    within_file_overrides = DITRE_WITHIN_FILE_DISAMBIGUATION.get(_ditre_file_key(pdf_path), {})

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
        words = []  # (top, left, text)
        for row in tsv_rows[1:]:
            if len(row) <= max(col.values()):
                continue
            if row[col["level"]] != "5":
                continue
            words.append((float(row[col["top"]]), float(row[col["left"]]), row[col["text"]]))
        if not words:
            continue
        words.sort()

        # 1) Cluster into visual rows by top-tolerance (NOT round(top) --
        # see docstring for why that broke on real data).
        visual_rows: list[list[tuple[float, float, str]]] = []
        for w in words:
            if visual_rows and abs(w[0] - visual_rows[-1][0][0]) <= ROW_TOP_TOLERANCE:
                visual_rows[-1].append(w)
            else:
                visual_rows.append([w])

        # 2) Within each visual row, split into column-groups at whichever
        # internal gap exceeds COLUMN_GUTTER_MIN_GAP (there is at most one
        # such gap per row: the gutter between the two TOC columns). A row
        # with content in only ONE column (common for wrapped-name
        # continuation rows, e.g. a lone "Primopiano" under "Boxy - Eric -
        # Helios - Multitude -") has no internal gap to split on at all --
        # first pass below learns each column's real x-range from rows
        # that DID split, then a second pass classifies those single-
        # column rows by comparing their own x against that learned
        # boundary, instead of defaulting them all to the left column
        # (confirmed by hand: that default silently glued right-column
        # continuations like "086 Configuration" onto the LEFT column's
        # last real entry, e.g. "Chloè luxury").
        rows_sorted = [sorted(row, key=lambda w: w[1]) for row in visual_rows]
        split_indices: dict[int, int] = {}
        for ri, row in enumerate(rows_sorted):
            for i in range(1, len(row)):
                if row[i][1] - row[i - 1][1] > COLUMN_GUTTER_MIN_GAP:
                    split_indices[ri] = i
                    break
        left_starts = [rows_sorted[ri][0][1] for ri in split_indices]
        right_starts = [rows_sorted[ri][split_indices[ri]][1] for ri in split_indices]
        if left_starts and right_starts:
            column_threshold = (max(left_starts) + min(right_starts)) / 2
        else:
            column_threshold = None  # no two-column rows found on this page at all

        left_col_rows: list[list[tuple[float, str]]] = []
        right_col_rows: list[list[tuple[float, str]]] = []
        for ri, row in enumerate(rows_sorted):
            if ri in split_indices:
                split_at = split_indices[ri]
                left_col_rows.append([(w[1], w[2]) for w in row[:split_at]])
                right_col_rows.append([(w[1], w[2]) for w in row[split_at:]])
            elif column_threshold is not None and row[0][1] >= column_threshold:
                right_col_rows.append([(w[1], w[2]) for w in row])
            else:
                left_col_rows.append([(w[1], w[2]) for w in row])

        for ordered_rows in (left_col_rows, right_col_rows):
            if not ordered_rows:
                continue
            # The page-number x-band: the x-position shared by the FIRST
            # word of the most rows in this column, restricted to rows
            # whose first word is actually numeric (so a column whose top
            # rows happen to be all-text section headers doesn't skew it).
            numeric_first_xs = [
                row[0][0] for row in ordered_rows
                if row and re.fullmatch(r"\d{1,4}", row[0][1])
            ]
            if not numeric_first_xs:
                continue
            rounded = [round(x) for x in numeric_first_xs]
            number_x_band = max(set(rounded), key=rounded.count)

            last_entry_idx = None  # index into `entries` for wrapped continuations
            for row_words in ordered_rows:
                if not row_words:
                    continue
                row_text = " ".join(t for _, t in row_words).strip()
                if not row_text or "products index" in row_text.lower():
                    continue
                if DITRE_LEGAL_FOOTER_RE.search(row_text):
                    # The "this price list cancels and replaces..." legal
                    # notice sits below the real index content, in the
                    # same x-range as the last product column, with no
                    # page number of its own -- without this check it gets
                    # silently glued onto the last real entry as if it
                    # were a wrapped continuation (confirmed: "Monolith"
                    # picked up the entire notice paragraph on sofa2026).
                    last_entry_idx = None
                    continue
                first_left, first_text = row_words[0]
                is_real_page_number = (
                    re.fullmatch(r"\d{1,4}", first_text)
                    and abs(round(first_left) - number_x_band) <= 3
                )
                if is_real_page_number:
                    name = " ".join(t for _, t in row_words[1:]).strip()
                    if name and name not in KNOWN_DITRE_TOC_LABELS:
                        page_num = int(first_text)
                        name = within_file_overrides.get((name, page_num), name)
                        name = name_overrides.get(name, name)
                        entries.append((name, page_num))
                        last_entry_idx = len(entries) - 1
                    else:
                        last_entry_idx = None
                elif row_text in KNOWN_DITRE_TOC_LABELS:
                    # a genuine section/reference header, not a wrapped
                    # continuation of the previous real entry -- do NOT
                    # append it onto anything.
                    last_entry_idx = None
                elif last_entry_idx is not None:
                    # wrapped continuation of the previous real entry in
                    # THIS column (e.g. "128  Boxy - Eric - Helios -
                    # Multitude -" / "Primopiano" on the next row).
                    name, pg_num = entries[last_entry_idx]
                    entries[last_entry_idx] = (f"{name} {row_text}".strip(), pg_num)
                # else: unrecognized header-ish text with nothing to
                # attach to yet (e.g. the "SOFA / SOFA" category line
                # sitting above the first real entry) -- safe to drop.
    seen = set()
    unique = []
    for name, pg in entries:
        key = (name, pg)
        if key not in seen:
            seen.add(key)
            unique.append((name, pg))
    unique.sort(key=lambda x: x[1])
    return unique


def build_ditre_page_map(pdf_path: str, total_pages: int) -> dict[int, int]:
    """Read every Ditre Italia page's footer to map printed page numbers ->
    real PDF pages. Two confirmed footer formats coexist across the 6
    source PDFs (verified by direct pdftotext inspection, not assumed):
    the "Night" catalogs print "<N> | <ProductName>" on a left/verso page
    and "<ProductName> | <N>" on the facing right/recto page (confirmed:
    PDF page 8 -> "6 | Ada", PDF page 91 -> "Freedom 2.0 sofa-bed armrests
    S | 89" -- the page NUMBER always sits toward the spread's outer edge,
    mirroring which side of the spread the page is on -- an early version
    of this only matched the second form and silently mapped zero even
    printed-page numbers as a result). The other 4 files print a bare "<N>"
    alongside the "Ditre Italia" wordmark, mirrored the same way (left
    page: "<N> ... Ditre Italia"; right page: "Ditre Italia ... <N>").
    Both regexes are tried on every page since they're mutually exclusive
    formats -- whichever matches wins. No offset/formula fallback is
    assumed: a page with neither pattern simply has no entry in the
    returned map, same as build_bonaldo_page_map, so a genuinely unreadable
    footer is visible as a gap rather than silently mismapped.

    The footer is not always the page's literal LAST non-blank line: on
    pages ending in a bilingual note (an English legend line immediately
    followed by its wrapped French translation), the translation line
    prints below the footer, pushing it up by one or two lines. Checking
    only lines[-1] silently dropped that page's printed-page number from
    the map -- confirmed on Night-catalog PDF page 91 ("Freedom 2.0
    sofa-bed armrests S | 89" followed by a wrapped French note line). A
    missing entry isn't just a gap: compute_ranges' min/max swap (see
    caller) can shift that PRODUCT's entire page range back by one page,
    silently absorbing the previous product's last page into this one's
    range and vice versa -- confirmed to have corrupted "Freedom 2.0
    sofa-bed armrests S"'s own price rows with 36 of neighbor "Sommier"'s
    SKUs. Fix: scan the last few non-blank lines (footers are always near
    the bottom, never mid-page) from the bottom up and take the first
    match -- verified via a direct old-vs-new diff across all 5 current
    Ditre PDFs to recover exactly the missing entries (16, all in the
    Night catalog) with zero changes to any of the 579 already-correct
    mappings, so this is strictly additive, not a behavior change for
    pages that were already being read correctly.
    """
    page_map: dict[int, int] = {}
    name_pipe_re = re.compile(r"^\s*(\d{1,4})\s*\||\|\s*(\d{1,4})\s*$")
    wordmark_re = re.compile(r"^\s*(\d{1,4})\s+.*ditre italia|ditre italia.*?(\d{1,4})\s*$", re.IGNORECASE)
    FOOTER_LOOKBACK = 6
    for pg in range(1, total_pages + 1):
        text = pdftotext_page(pdf_path, pg)
        lines = [ln.rstrip("\r") for ln in text.split("\n") if ln.strip()]
        if not lines:
            continue
        for last in reversed(lines[-FOOTER_LOOKBACK:]):
            m = name_pipe_re.search(last)
            if m:
                printed = m.group(1) or m.group(2)
                page_map[int(printed)] = pg
                break
            m = wordmark_re.search(last)
            if m:
                printed = m.group(1) or m.group(2)
                if printed:
                    page_map[int(printed)] = pg
                    break
    return page_map


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


# ---------------------------------------------------------------------------
# Pianca -- INDICE format so far only verified against
# "2024_10_Progetti_di_Design_08_1R +6_.pdf" (the file this style is scoped
# to for its first implementation slice; the other 9 Pianca source PDFs use
# structurally different INDICE layouts per the Step-1 structural read-only
# pass -- e.g. Sistemi Giorno/Notte's INDICE nests category > sub-section >
# page-number several levels deep, Progetti 09/Spazi-10 use a numbered
# "card" grid instead of a text list -- and have NOT been verified against
# this parser. Do not widen --index-pages usage to those files without
# re-verifying the coordinate bands below against their own -tsv output
# first, the same way this file's bands were confirmed against Progetti
# 08's real page-4 -tsv dump (3 x-bands: category label ~x254, product name
# ~x480-535, page number ~x535-545, right-aligned) rather than assumed from
# the rendered layout alone.
#
# A product name is not always followed immediately by its own category
# label -- e.g. Progetti 08's INDICE prints "Sedie" once (aligned with
# "Lina"), then "Palù" on the next row with NO repeated "Sedie" label,
# since both belong to the same category block. This mirrors how "Divani"
# covers Levante+Peonia and "Poltrone" covers Levante+Peonia again (the
# SAME two names recur under a second category -- Levante and Peonia are
# each both a sofa AND an armchair line, confirmed via Step-1's visual
# inspection of pages 15/20). Per the approved product model, these are
# kept as distinct top-level catalog entries qualified by category
# ("Levante (Divani)" vs "Levante (Poltrone)"), not merged or deduped.
# ---------------------------------------------------------------------------

def parse_index_pianca(pdf_path: str, index_pages: range) -> list[tuple[str, int]]:
    """Parse Pianca's INDICE page (Progetti 08 layout only -- see module
    comment above) via -tsv coordinates: category label / product name /
    page number sit in 3 distinct x-bands on each visual row, with the
    category label only present on the FIRST row of its own block (later
    rows in the same category inherit it). Returns (name, printed_page)
    with name qualified as "Name (Category)" whenever the same bare name
    recurs under more than one category in this index (verified needed:
    Levante and Peonia each appear under both Divani and Poltrone)."""
    CATEGORY_X_MAX = 300.0
    NAME_X_MIN = 400.0
    PAGENUM_X_MIN = 530.0
    ROW_TOP_TOLERANCE = 3.0

    raw_entries: list[tuple[str, str, int]] = []  # (category, name, page)
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
        words = []  # (top, left, text)
        for row in tsv_rows[1:]:
            if len(row) <= max(col.values()):
                continue
            if row[col["level"]] != "5":
                continue
            left = float(row[col["left"]])
            if left < 200.0:
                continue  # page furniture: "INDICE" title, "INTERACTIVE" watermark
            top = float(row[col["top"]])
            if top > 750.0:
                # The INDICE page's OWN page-number footer (e.g. "Progetti
                # di design 01") sits in the exact same x-columns as this
                # page's third category block (confirmed real on Progetti
                # 09's page 3: "Progetti"/"di"/"design" at x=483-517,
                # "01" at x=539.77 -- indistinguishable from a real
                # Madie/Mambo/Norma Up/Siviglia row by x-position alone).
                # Every real INDICE row seen across both verified files
                # sits well above top=750 (an A4 page is ~842pt tall, so
                # this is comfortably inside the bottom margin); the
                # footer is the only content that ever lands this low.
                continue
            words.append((top, left, row[col["text"]]))
        if not words:
            continue
        words.sort()

        visual_rows: list[list[tuple[float, float, str]]] = []
        for w in words:
            if visual_rows and abs(w[0] - visual_rows[-1][0][0]) <= ROW_TOP_TOLERANCE:
                visual_rows[-1].append(w)
            else:
                visual_rows.append([w])

        current_category = None
        for row in visual_rows:
            row = sorted(row, key=lambda w: w[1])
            cat_tokens = [t for _, left, t in row if left < CATEGORY_X_MAX]
            name_tokens = [t for _, left, t in row if NAME_X_MIN <= left < PAGENUM_X_MIN]
            pagenum_tokens = [t for _, left, t in row if left >= PAGENUM_X_MIN]
            if cat_tokens:
                current_category = " ".join(cat_tokens).strip()
            if not name_tokens or not pagenum_tokens:
                continue  # e.g. a trailing back-matter line with no page number
            if not re.fullmatch(r"\d{1,4}", pagenum_tokens[-1]):
                continue
            name = " ".join(name_tokens).strip()
            page_num = int(pagenum_tokens[-1])
            if name and current_category:
                raw_entries.append((current_category, name, page_num))

    # Qualify with category only for names that recur under >1 category --
    # confirmed necessary (Levante, Peonia); leaving single-category names
    # (Lina, Palù) unqualified matches how every other Pianca product is
    # named elsewhere in this project (no brand precedent qualifies a name
    # that doesn't actually collide).
    names_by_category: dict[str, set] = {}
    for cat, name, _ in raw_entries:
        names_by_category.setdefault(name, set()).add(cat)

    entries: list[tuple[str, int]] = []
    for cat, name, page_num in raw_entries:
        display_name = f"{name} ({cat})" if len(names_by_category[name]) > 1 else name
        entries.append((display_name, page_num))

    seen = set()
    unique = []
    for name, pg in entries:
        key = (name, pg)
        if key not in seen:
            seen.add(key)
            unique.append((name, pg))
    unique.sort(key=lambda x: x[1])
    return unique


def build_pianca_page_map(pdf_path: str, total_pages: int) -> dict[int, int]:
    """Read every page's footer to map printed page numbers -> real PDF
    pages. Two confirmed, mutually exclusive footer conventions coexist
    ACROSS DIFFERENT PIANCA SOURCE FILES (not within one file -- each
    file uses exactly one convention throughout, confirmed by direct
    pdftotext inspection, not assumed):
      1) Progetti 08 (and other files from the Step-1 pass -- SEDIE_1,
         UNLESS_7, SIPARIO_15, TEATRO_305/309, ANTEPRIMA_291, PRIMO_2):
         a running counter joined to the current section's own UPPERCASE
         name by an underscore, mirrored by spread side --
         "<N>_SECTIONNAME" (e.g. "4_PROGETTI DI DESIGN") on one side,
         "SECTIONNAME_<N>" (e.g. "PROGETTI DI DESIGN_5") on the other.
      2) Progetti 09: the same mirrored-by-spread-side idea, but with the
         literal MIXED-CASE phrase "Progetti di design" joined by a plain
         space instead of an underscore -- "<N> Progetti di design" (e.g.
         "06 Progetti di design") / "Progetti di design <N>" (e.g.
         "Progetti di design 05"). Confirmed a flat +2 real-vs-printed
         offset throughout this file (real page 6 -> printed 4, real 15
         -> printed 13, etc.), but this still reads every page's actual
         footer rather than trusting that as a blind formula -- only
         build_offset_fallback (below) uses the map's own derived offset,
         and only for pages with no readable footer at all.
      3) Spazi-10: same idea again, with the literal phrase "Spazi" --
         "<N> Spazi" / "Spazi <N>". Confirmed a flat +0 offset (real page
         2 -> printed "02", real page 6 -> printed "06", etc. -- real PDF
         page number equals printed page number exactly), again read from
         every page's real footer rather than assumed.
    All pattern pairs are tried on every page since they're mutually
    exclusive per-file; whichever matches wins. The section-name label
    itself varies per product/section (unlike Ditre's constant "Ditre
    Italia" wordmark), so pattern 1 matches ANY uppercase label rather
    than a hardcoded name, and patterns 2/3 are scoped to the literal
    phrases actually observed per file rather than "any mixed-case label"
    (which would risk matching ordinary prose sentences ending a page).
    No offset/formula fallback here either -- see build_offset_fallback,
    called separately by main() for whichever pages this leaves unmapped
    (full-bleed section-divider pages confirmed to have no footer at
    all, e.g. Progetti 08's own "PEONIA" divider page)."""
    page_map: dict[int, int] = {}
    prefix_re = re.compile(r"^\s*(\d{1,4})_[A-ZÀ-Ù][A-ZÀ-Ù ]*\s*$")
    suffix_re = re.compile(r"^\s*[A-ZÀ-Ù][A-ZÀ-Ù ]*_(\d{1,4})\s*$")
    prefix_re2 = re.compile(r"^\s*(\d{1,4})\s+Progetti di design\s*$", re.IGNORECASE)
    suffix_re2 = re.compile(r"^\s*Progetti di design\s+(\d{1,4})\s*$", re.IGNORECASE)
    prefix_re3 = re.compile(r"^\s*(\d{1,4})\s+Spazi\s*$")
    suffix_re3 = re.compile(r"^\s*Spazi\s+(\d{1,4})\s*$")
    FOOTER_LOOKBACK = 4
    for pg in range(1, total_pages + 1):
        text = pdftotext_page(pdf_path, pg)
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        if not lines:
            continue
        for last in reversed(lines[-FOOTER_LOOKBACK:]):
            m = (prefix_re.match(last) or suffix_re.match(last) or prefix_re2.match(last)
                 or suffix_re2.match(last) or prefix_re3.match(last) or suffix_re3.match(last))
            if m:
                page_map[int(m.group(1))] = pg
                break
    return page_map


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
    ("Teli di Copertura", 557, 569, "REFERENCE_MATRIX", "reference", "NOT a product list -- a compatibility matrix: generic cover codes (e.g. 9400M, priced PER LINEAR METER '/ML', not a flat total) cross-referenced against which OTHER collections' furniture codes each cover fits (bahia/barcode/belt/emma/etc., each at its own size). Reclassified out of 'D' 2026-08-11 after its parser pass came back 0/98 -- structurally unlike anything built so far, needs its own scoping discussion before any parser design (same discipline as Shape E originally). 2026-08-13: the section's other code family, '9C5XXX' base-cover codes (103 total, 0 previously discovered -- structural regex gap, not this compatibility-matrix issue), is now covered too: 40 flat 'art. 9C5XXX' codes (pages 562/564) via VARASCHINI_EXTRA_CODE_PATTERNS, 63 grid codes (pages 565/567/569, shape 'REFERENCE_MATRIX_GRID') via _varaschini_teli_di_copertura_grid_codes."),
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

    KNOWN FOLLOW-UP (found 2026-08-21, not yet fixed): on at least one
    ABACO-style index/legend page (p525, Wellness Therapy sub-catalog),
    whatever downstream step assigns product_name for catalog_index.json
    entries picked up this SAME footer line ("525 - VARASCHIN EXPORT
    2026") as if it were itself a listed product name, producing a
    phantom "Wellness Therapy (catalogue) VARASCHIN EXPORT 2026" entry
    with a real page/image but no real product behind it. This function
    already strips the footer from PAGE text correctly -- the bug is in
    whatever later step reads product names off an index/legend page's
    remaining text and doesn't exclude a bare footer-shaped line from
    being read as a name. That entry was manually removed from
    data/Varaschini/catalog_index.json rather than parser-generated
    every run, so a real fix (excluding footer-shaped lines specifically
    from index/legend product-name extraction) is still needed, and
    other pages of the same ABACO/index-page shape were not audited for
    the same artifact.
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

# Teli di Copertura's "9C5XXX" cover-for-table-base codes (pages 562/564,
# confirmed real: "art. 9C5001 allegra / 81x76 h80 / €275 / 2582") have a
# digit-letter-digits shape (one leading digit, then a letter, THEN 3-4
# digits) that neither regex above can ever match -- both require 3-6
# LEADING digits before any letter appears. Confirmed structural, not a
# layout artifact: verified 103 total "9C5[0-9A-Z]*" occurrences across
# pages 557-569, 0 present in catalog_index.json before this fix. Kept as
# a SEPARATE, narrowly-scoped pattern (checked in addition to, never
# replacing, the base regexes) rather than broadening the shared ones --
# this exact shape is unique to this one collection, so there's no reason
# to widen what every OTHER collection's discovery also matches against.
_VARASCHINI_9C5_CODE_TOKEN = re.compile(r"^9C5[0-9]{2,4}[A-Z]?$")
_VARASCHINI_9C5_ART_PREFIX = re.compile(r"\bart\.?\s+(9C5[0-9]{2,4}[A-Z]?)\b", re.IGNORECASE)

# Per-collection (art_prefix_re, code_token_re) pairs checked IN ADDITION
# to the base regexes in _varaschini_find_records -- see
# _VARASCHINI_9C5_CODE_TOKEN comment above for why this is additive-only
# and collection-scoped rather than a global regex change.
VARASCHINI_EXTRA_CODE_PATTERNS: dict[str, tuple] = {
    "Teli di Copertura": (_VARASCHINI_9C5_ART_PREFIX, _VARASCHINI_9C5_CODE_TOKEN),
}

# Teli di Copertura pages 565-569 are a base-height x TOP-dimension GRID
# (confirmed on p567: a row of 6 "art." labels + 6 "9C5XXX" codes,
# followed a few lines later by a row of 6 prices in matching left-to-
# right order, repeated for each height band; the codes repeat densely
# enough -- e.g. 6 per header row -- that the same per-"art."-occurrence
# scan used for the flat pages (562/564, one code per "art.") would
# either miss 5 of every 6 codes or attach them all to one garbled name).
# Excluded from the regular per-line scan here and handled by a dedicated
# TSV coordinate pass instead (_varaschini_teli_di_copertura_grid_codes),
# same technique already built for Composizione Tavoli/Basi Tavolini/
# Carpet Design -- reused, not reinvented, because the underlying problem
# (a row's code and its price ending up far apart in -layout's linearized
# text) is the same class of issue, confirmed on p567/569's clean grid
# layout and p565's mix of the same grid plus instructional text. Pages
# 566/568's own -layout text turns out to be just as badly scrambled
# (confirmed once actually generated: codes, dimension callouts, and
# fragment words interleaved out of order, e.g. "9C5200 Coffee table
# Ø70A9C5204..." on p566) -- no p566.txt/p568.txt previously existed only
# because asset generation is keyed off which pages an ALREADY-discovered
# catalog entry references, and nothing referenced these two pages before
# this fix (not because -layout produced literally nothing). Either way,
# per-line scanning was never going to work here; included in the
# exclusion set (and the TSV pass's page range) for that reason.
#
# Also covers a second, non-grid case: Belt/Belt Air's "ESEMPI DI
# COMPOSIZIONI" pages 129-131 (see _varaschini_belt_composition_codes) --
# several composition codes share ONE distant "art ." header per page, so
# the standard 6-line lookahead window only ever catches whichever code
# happens to sit closest to it. Different underlying problem than the
# grid pages, same fix shape (TSV coordinates, not per-line scanning), so
# reuses this same per-collection page-exclusion mechanism rather than a
# second parallel one.
VARASCHINI_TSV_ONLY_PAGES: dict[str, set[int]] = {
    "Teli di Copertura": {565, 566, 567, 568, 569},
    "Belt / Belt Air": {129, 130, 131},
}

# Confirmed on real page images (p146-159, the diagram-cluttered Big/Big
# Light cluster already flagged for price extraction, plus Dolmen/Tight/
# Outdoor Lighting/Teli di Copertura -- 11 entries total, checked
# individually against the raw extracted text): a code's own physical line
# on these pages holds scattered SIZE-VARIANT markers (bare "A"/"B" plate
# labels), diameter/dimension callouts (bare numbers, "Ø16" etc.), and/or
# ONE word from a repeating multi-language finish/color legend that gets
# stranded there by pdftotext's column-linearization breaking down --
# never a real description. Each of these tokens individually passes the
# existing junk check above (non-empty, not pure currency/digits), so the
# combination silently became the product's own display name (e.g. "Big /
# Big Light A B White") instead of falling through to the same "collection
# + bare code" fallback that already works fine for every OTHER product on
# these same pages whose own line happens to be empty. This is a NAME
# extraction bug, separate from and not fixing the price-extraction gap
# already logged for this cluster.
_VARASCHINI_FINISH_LEGEND_WORDS = {
    "bianco", "white", "grigio", "seta", "silk", "grey", "gray", "denim",
    "ruggine", "rust", "bronzo", "metal", "bronze", "verde", "green",
    "moka", "dark", "brown", "nero", "black",
}


def _varaschini_is_scattered_diagram_junk(s: str) -> bool:
    """True if EVERY token of an already-whitespace-collapsed fragment is a
    bare single letter (a size-variant marker like "A"/"B"), a bare
    dimension/diameter number (with or without a leading "Ø"), or a known
    finish/color-legend word -- i.e. the fragment has zero real descriptive
    content of its own. Also true for any fragment containing Cyrillic (or
    other non-Latin) script, confirmed only ever cross-language legend text
    stranded by the same linearization corruption, never a real product
    description (Varaschini's real names are Italian/English, Latin
    script). Verified against the whole current catalog_index.json before
    landing: exactly 11 entries match, all individually confirmed against
    their real page text as this exact pattern, zero false positives
    against any genuinely-descriptive name elsewhere in the catalog."""
    if re.search(r"[Ѐ-ӿ]", s):
        return True
    tokens = s.split()
    if not tokens:
        return False
    for tok in tokens:
        low = tok.lower()
        if re.fullmatch(r"[a-z]", low):
            continue
        if re.fullmatch(r"ø?\d+(?:[.,]\d+)?", low):
            continue
        if low in _VARASCHINI_FINISH_LEGEND_WORDS:
            continue
        return False
    return True


# INVESTIGATED, deliberately NOT implemented (2026-08-13): a code's own
# physical line on pages listing several MODULAR/BUNDLE piece codes
# together (Belt/Belt Air's diagram-grid pages, Emma/Emma Cross's
# "236M"/"248M" cushion series, Bento, Reuse, Wellness Therapy -- ~208
# entries total) is a CROSS-REFERENCE LIST of other nearby codes ("art.
# 22102B art. 22105S art. 22105SB..."), not a real description -- same
# surface shape as the diagram-junk case above. A first attempt added a
# rule rejecting this pattern (same way as _varaschini_is_scattered_
# diagram_junk), but verification uncovered a SERIOUS, genuinely
# pre-existing bug in the discovery merge logic that made shipping it
# unsafe: run_varaschini's per-code merge ("if not section_records[code][2]
# and nm: section_records[code][2] = nm") lets ANY later page within a
# collection's full section range fill in a name once the first
# occurrence is empty -- previously this rarely mattered (most rejected
# occurrences had no later occurrence to fall back to), but rejecting this
# NEW, much more common pattern let the search wander much further,
# surfacing names with NO real connection to the code they got attached
# to. Confirmed on 2 independent, different collections (not a one-off):
# Belt/Belt Air's codes 1986/1987 (own real occurrence: p57, a bare
# category-header listing, art_code "1986"/"1987" appear ONLY there) got
# renamed to "Coffee table Ø50"/"Ø70" -- verified that exact text does
# NOT appear anywhere in Belt/Belt Air's own defined section (pages
# 55-131); Emma's code 236M01 (own real occurrence: p258, same category-
# header shape) got renamed to "Armchair | 1 seat 80" -- verified "1 seat
# 80" does not appear anywhere in Emma's own section (pages 221-286)
# either. Both would have been silently WRONG, fabricated-looking names,
# not just uninformative ones -- worse than the cross-reference-list
# garbage this was meant to fix. Safely fixing this needs the merge logic
# itself to stop treating the whole section range as fair game for a
# fallback name (e.g. require the replacement to come from a page close
# to the original occurrence, or verify the code's own "art." trigger --
# not just a bare-token collision -- is what supplied it) -- a genuinely
# larger redesign than a narrow, individually-verifiable string-cleaning
# rule, out of scope for this pass. Not applied to the live catalog_index.
# The already-verified-safe _varaschini_is_scattered_diagram_junk rule
# above (11 entries, single-page, no wandering risk observed) is
# unaffected and stays in place.


def _varaschini_clean_name(s: str) -> str:
    """Collapse internal whitespace runs and reject junk captured instead of
    a real description -- confirmed on Marketing Communication p8, where a
    bare price fragment ("€\\x08 55") got grabbed because the real Italian
    name sits on a line ABOVE the code on Shape D rows, not after it."""
    s = re.sub(r"\s{2,}", " ", s).strip(" -")
    s = s.lstrip("\x08﻿").strip()
    if not s or s.startswith("€") or re.match(r"^[\d.,€\s]+$", s):
        return ""
    if _varaschini_is_scattered_diagram_junk(s):
        return ""
    return s[:60]


def _varaschini_find_records(
    page_num: int, text: str, extra_patterns: tuple | None = None
) -> list[tuple[str, int, str]]:
    """Find every (code, page, name_guess) triple on one page via 'art.'
    detection. Three physical arrangements of a code relative to its 'art.'
    label are all handled, confirmed against real text across Allegra/
    Bahia/Bali/Dolmen:
      1) same line: "art. 2214" (Belt/Belt Air diagram-grid style)
      2) "art." alone, code some lines BELOW (Allegra/Bahia/System style)
      3) "art." alone, code some lines ABOVE (Dolmen p210: "1820L" prints
         one line before its own bare "art." label)

    `extra_patterns`, when given, is a (art_prefix_re, code_token_re) pair
    checked IN ADDITION to the base regexes -- see
    VARASCHINI_EXTRA_CODE_PATTERNS for why this stays opt-in per collection
    rather than widening the base regexes for everyone.
    """
    extra_art_re, extra_code_re = extra_patterns if extra_patterns else (None, None)
    records = []
    lines = text.splitlines()
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        art_matches = list(_VARASCHINI_ART_PREFIX.finditer(line))
        if extra_art_re:
            art_matches += list(extra_art_re.finditer(line))
        for m in art_matches:
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
                if _VARASCHINI_CODE_TOKEN.match(first_tok) or (extra_code_re and extra_code_re.match(first_tok)):
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
                    if _VARASCHINI_CODE_TOKEN.match(first_tok) or (extra_code_re and extra_code_re.match(first_tok)):
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
#   "130" (Belt/Belt Air p130): that page's own footer page number
#   ("130 - VARASCHIN EXPORT 2026") happens to print left-aligned rather
#   than right-aligned like every neighboring page's footer (p129/p131
#   both print theirs far right, confirmed via direct TSV coordinate
#   check) -- landing in the same left-column x-position
#   _varaschini_belt_composition_codes filters on for real "art ."-column
#   codes, and "130" itself happens to satisfy the same digit-shape regex.
VARASCHINI_FALSE_POSITIVE_CODES: set[tuple[int, str]] = {
    (554, "506"),
    (130, "130"),
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


# Cuscini e Tessuti's pages (571-573) also contain ~53 bare 2-3 digit
# numbers that satisfy _VARASCHINI_CODE_TOKEN's shape but are NOT real
# product codes -- individually verified (not assumed from the digit
# count alone): a full scan of every occurrence of every one of these 53
# tokens across all 3 pages found each one sitting immediately adjacent
# to a "€" (a price value, e.g. "€ 275") or embedded inside a "NNN/4"-
# style fraction denominator within a dimension string (e.g. "113" from
# "193/4x113/4"). None ever appear with a dimension/description of their
# own the way a real code does. Confirmed real codes (229H, 2701, 2720,
# etc.) are always 4+ digits or carry a letter suffix; every one of these
# false positives is a bare 2-3 digit run. Scoped to this collection only
# (not a general code-shape tightening, which risks losing real codes
# elsewhere) -- same precedent as VARASCHINI_FALSE_POSITIVE_CODES, just
# collection-scoped (all 3 pages) rather than page-scoped, since these
# aren't one-off artifacts but a systemic property of this collection's
# dense price-table layout.
VARASCHINI_CUSCINI_E_TESSUTI_FALSE_POSITIVE_CODES: set[str] = {
    "102", "104", "109", "110", "112", "113", "116", "118", "124", "127",
    "130", "132", "136", "137", "138", "143", "145", "149", "154", "157",
    "162", "163", "165", "170", "176", "182", "187", "191", "193", "195",
    "207", "215", "220", "225", "231", "234", "239", "253", "271", "275",
    "279", "285", "286", "297", "308", "310", "341", "352", "374", "435",
    "473", "484", "627",
}


def _varaschini_find_cuscini_e_tessuti_records(page_num: int, text: str) -> list[tuple[str, int, str]]:
    """Cuscini e Tessuti-only variant of _varaschini_find_records_flat,
    fixing a real name-capture bug the shared function has: it only ever
    looks INSIDE a code's own 2+-space chunk for its name (`chunk[len(
    first_tok):]`), never at the chunks that follow it on the same line.
    On this collection's genuinely 2-column pages this produces two
    distinct, confirmed-real failure modes:

    1) Empty names for codes whose OWN description sits in a SEPARATE
       following chunk with real content between it and the code (e.g.
       "24620S" + a 2+-space gap + "Zavorra per cuscino 2kg..." on the
       exact same line) -- the shared function captures nothing at all
       here, even though the real name is right there.

    2) (Previously a live, shipped bug in catalog_index.json, not merely
       theoretical) A stale, pre-fix build of the shared logic instead
       captured EVERYTHING to end-of-line, bleeding the RIGHT column's own
       code+dimension into the LEFT column code's name (e.g. "2701"'s
       name including "...2726 cm 60 x 40..." -- 2726's own text, not
       2701's). Confirmed via direct page text: a representative sample of
       affected codes (2701, 2708, 2709, 2713, 2716, 2719, 2727, 2736,
       2737, plus the 229H/24610/24620 family) all show this exact
       garbled-concatenation shape in the live catalog_index.

    Fix: for each code-shaped chunk, capture the rest of its OWN chunk
    PLUS every following chunk on the line, but STOP before the next
    code-shaped chunk (case 2's fix) rather than running to end-of-line
    (which would also cause case 1's original bug of finding nothing when
    there's no next code to bound against, since a code with no
    following-code chunk previously fell through with rest="" anyway).

    NOT applied to `_varaschini_find_records_flat` itself, which 8 OTHER
    collections also use (Marketing Communication, Outdoor Cooking,
    Trama, Carpet Design, Outdoor Lighting, Strumenti Commerciali,
    Prodotti per la Pulizia, Basi Tavolini) -- verified via a direct
    before/after diff across all of them that "capture until the next
    code chunk, else run to end of line" is NOT safe there: on their
    (mostly single-column, one-code-per-line) pages there's usually no
    next code chunk to stop at, so it captured page-header fragments
    ("...STRUCTURE PREZZO/PRICE"), footnote sentences ("N.B. Qualora il
    telecomando..."), and even bare price values into the name instead.
    Kept as its own collection-scoped function instead, same precedent as
    VARASCHINI_EXTRA_CODE_PATTERNS/VARASCHINI_TSV_ONLY_PAGES elsewhere in
    this file, rather than risking those other 8 collections.
    """
    records = []
    lines = text.splitlines()
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        chunks = re.split(r"\s{2,}", line)
        code_chunk_idxs = [
            ci for ci, chunk in enumerate(chunks)
            if chunk.split() and _VARASCHINI_CODE_TOKEN.match(chunk.split()[0])
        ]
        for pos, ci in enumerate(code_chunk_idxs):
            chunk = chunks[ci]
            first_tok = chunk.split()[0]
            if (page_num, first_tok.upper()) in VARASCHINI_FALSE_POSITIVE_CODES:
                continue
            if first_tok.upper() in VARASCHINI_CUSCINI_E_TESSUTI_FALSE_POSITIVE_CODES:
                continue
            window = " ".join(lines[i:i + 3])
            if "€" not in window:
                continue
            rest_of_own_chunk = chunk[len(first_tok):].strip()
            next_code_ci = code_chunk_idxs[pos + 1] if pos + 1 < len(code_chunk_idxs) else len(chunks)
            extra_chunks = chunks[ci + 1:next_code_ci]
            name_parts = ([rest_of_own_chunk] if rest_of_own_chunk else []) + extra_chunks
            name = " ".join(p for p in name_parts if p)
            records.append((first_tok.upper(), page_num, _varaschini_clean_name(name)))
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


def _varaschini_teli_di_copertura_grid_codes(pdf_path: str) -> dict[str, int]:
    """Teli di Copertura's base-height x TOP-dimension cover-price GRID
    (pages 565-569 -- see VARASCHINI_TSV_ONLY_PAGES) has the same
    per-line-scan problem as Composizione Tavoli: a header row of several
    "art. 9C5XXX" codes prints several physical lines above its matching
    price row, so per-line detection either misses codes or attaches them
    to the wrong/garbled name. Reuses the same `pdftotext -tsv` word-
    coordinate technique as _varaschini_composizione_tavoli_codes for the
    same reason -- coordinates don't depend on -layout's linearized
    reading order. Pages 566/568 are real, densely-code-populated grid
    pages whose own -layout text is just as scrambled as the rest of this
    grid (see VARASCHINI_TSV_ONLY_PAGES) -- -tsv finds them fine
    regardless, since it was never using -layout's reading order to begin
    with.

    Returns {code: first_page_seen}.
    """
    result = subprocess.run(
        [PDFTOTEXT, "-tsv", "-enc", "UTF-8", "-f", "565", "-l", "569", pdf_path, "-"],
        capture_output=True,
    )
    tsv_text = result.stdout.decode("utf-8", errors="replace")

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
        if _VARASCHINI_9C5_CODE_TOKEN.match(text) and text not in code_first_page:
            code_first_page[text] = page
    return code_first_page


def _varaschini_belt_composition_codes(pdf_path: str) -> dict[str, int]:
    """Belt/Belt Air's "ESEMPI DI COMPOSIZIONI" (composition examples)
    pages 129-131 each list several composition codes (e.g. "249C2",
    "249C3") in one page's own price table, each with its own "OUTFIT
    COVER" accessory variant ("249C2C"). The default "art." trigger scan
    (_varaschini_find_records) only found the FIRST one per page: this
    page's header literally prints as "art ." (a SPACE before the period,
    confirmed p129) rather than "art.", which still satisfies the bare-
    trigger regex, but there's only ONE such header per page while several
    composition codes sit at increasing distance below it -- the first
    (3 lines below) falls inside the 6-line lookahead window, the rest
    (found 249C3 sits 34 lines below the same one header) don't.

    Fixed via `pdftotext -tsv` coordinates instead: every genuine
    composition/accessory code sits in the SAME leftmost column as that
    "art ." header (confirmed: left~28-33 across all 3 pages), while
    inline cross-reference mentions of a composition's own COMPONENT
    codes (e.g. "2493", "24902" -- the modules that make up 249C2, printed
    in a middle column purely for description, not their own priced item)
    sit far to the right (left~150-240) despite ending up close to an
    unrelated price line after -layout's linearization -- confirmed via
    direct coordinate check this is a clean, reliable separator, not a
    coincidence of these 3 pages alone.

    Two page-specific quirks handled: (1) p130's own footer page number
    ("130") happens to print left-aligned instead of right-aligned like
    its neighbors, landing in this same left column and satisfying the
    code-shape regex -- excluded via VARASCHINI_FALSE_POSITIVE_CODES, same
    precedent as Carpet Design's "506". (2) "249C2C" (p129 only -- its
    siblings 249C3C/249C4C/etc. on other pages come through as one whole
    token) splits into two adjacent tokens ("249C2" + a lone "C") due to a
    font-kerning boundary poppler treats as a word break -- merged back
    together when a lone "C" token immediately follows a leftmost-column
    code at a small horizontal gap on the same row.

    Returns {code: first_page_seen}.
    """
    result = subprocess.run(
        [PDFTOTEXT, "-tsv", "-enc", "UTF-8", "-f", "129", "-l", "131", pdf_path, "-"],
        capture_output=True,
    )
    tsv_text = result.stdout.decode("utf-8", errors="replace")

    tokens: list[tuple[int, float, float, str]] = []  # (page, left, top, text)
    for line in tsv_text.splitlines()[1:]:  # skip TSV header row
        parts = line.split("\t")
        if len(parts) < 12 or parts[0] != "5":  # level 5 = word-level token
            continue
        try:
            page = int(parts[1])
            left, top = float(parts[6]), float(parts[7])
        except ValueError:
            continue
        tokens.append((page, left, top, parts[11]))

    LEFT_COLUMN_MAX = 50.0
    code_first_page: dict[str, int] = {}
    left_col = [t for t in tokens if t[1] < LEFT_COLUMN_MAX and _VARASCHINI_CODE_TOKEN.match(t[3])]
    # The lone "C" continuation sits to the RIGHT of its own base code (that's
    # why it split off as its own token), so it's deliberately NOT bounded by
    # LEFT_COLUMN_MAX here -- only the tight (<=40 horizontal, <=3 vertical)
    # proximity check below constrains which "C" tokens can match.
    lone_c = [t for t in tokens if t[3] == "C"]
    for page, left, top, text in left_col:
        if (page, text) in VARASCHINI_FALSE_POSITIVE_CODES:
            continue
        code = text
        for c_page, c_left, c_top, _ in lone_c:
            if c_page == page and abs(c_top - top) <= 3 and 0 < (c_left - left) <= 40:
                code = text + "C"
                break
        if code not in code_first_page:
            code_first_page[code] = page
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
        # changed. Explicitly checking `name == "Cuscini e Tessuti"` below
        # (its own dedicated branch, not folded into the shape check) keeps
        # that layout-driven decision independent of the shape label.
        tsv_only_pages = VARASCHINI_TSV_ONLY_PAGES.get(name, set())
        extra_patterns = VARASCHINI_EXTRA_CODE_PATTERNS.get(name)
        section_records: dict[str, list] = {}  # code -> [p_first, p_last, name]
        for p in range(start, end + 1):
            if p in tsv_only_pages:
                continue  # handled separately below (TSV-based, not per-line)
            text = page_text.get(p, "")
            if name == "Cuscini e Tessuti":
                # _varaschini_find_records is skipped entirely for this
                # collection, not just supplemented -- its codes are bare
                # (no "art." prefix at all), so the ONLY thing that
                # function ever found here was a false positive: this
                # page range's own "ART." column-header line (printed
                # once per page, uppercase) matches _varaschini_find_
                # records' bare-"art."-trigger regex, whose lookahead then
                # grabs the FIRST code-shaped token on a nearby line as if
                # it followed a real trigger and captures the rest of that
                # WHOLE PHYSICAL LINE (no column-chunk bounding at all) as
                # its name -- confirmed exactly 1 such false match per
                # page (2713/2736/2450H), each with the same garbled
                # left+right-column-bled name this collection's real bug
                # report was about. Since this function contributes zero
                # legitimate signal here, calling it and merging its
                # result in would let this false positive win the "first
                # non-empty name wins" merge race over the correct,
                # properly-bounded name _varaschini_find_cuscini_e_
                # tessuti_records finds for the exact same code.
                recs = _varaschini_find_cuscini_e_tessuti_records(p, text)
            else:
                recs = _varaschini_find_records(p, text, extra_patterns)
                if shape in ("D", "E"):
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

    print("[3b/6] Teli di Copertura grid (pages 565-569): dedicated pdftotext -tsv pass...")
    tdc_grid_codes = _varaschini_teli_di_copertura_grid_codes(pdf_path)
    # 10 codes (9C5264-9C5273) are found by BOTH the flat p564 pass above
    # AND this grid pass -- confirmed genuinely the same product cross-
    # referenced on two pages (its own detail entry on p564, its
    # compatibility-grid entry on p568), not a data conflict: both passes
    # agree on price (e.g. 9C5264 = EUR226 either way). The grid pass wins
    # for attribution (TSV coordinates are more mechanically reliable than
    # -layout's linearized text on this dense a page, same reasoning as
    # everywhere else this technique is used) -- drop the flat duplicate
    # rather than keep two catalog_index entries for one code.
    dupes = [e for e in catalog if e["collection"] == "Teli di Copertura" and e["art_code"] in tdc_grid_codes]
    if dupes:
        catalog = [e for e in catalog if not (e["collection"] == "Teli di Copertura" and e["art_code"] in tdc_grid_codes)]
        print(f"      -> dropping {len(dupes)} flat-pass duplicate(s) also found by the grid pass: "
              f"{sorted(e['art_code'] for e in dupes)}")
    for code, page in sorted(tdc_grid_codes.items()):
        catalog.append({
            "brand": brand,
            "collection": "Teli di Copertura",
            "product_name": f"Teli di Copertura {code}",
            "art_code": code,
            "printed_page_start": page,
            "printed_page_end": page,
            "pdf_page_start": page,
            "pdf_page_end": page,
            # Distinct from the flat "94XXC" cover codes' "REFERENCE_MATRIX"
            # shape (mapped to the Shape A parser in parse_prices.py) --
            # this grid needs its own price-pairing logic (code -> nearest
            # price token below it in the same column), so it needs its own
            # dispatch key, not to be silently forced through Shape A.
            "shape": "REFERENCE_MATRIX_GRID",
            "section_category": "reference",
        })
    print(f"      -> {len(tdc_grid_codes)} verified codes")

    print("[3c/6] Belt/Belt Air compositions (pages 129-131): dedicated pdftotext -tsv pass...")
    belt_codes = _varaschini_belt_composition_codes(pdf_path)
    for code, page in sorted(belt_codes.items()):
        catalog.append({
            "brand": brand,
            "collection": "Belt / Belt Air",
            "product_name": f"Belt / Belt Air {code}",
            "art_code": code,
            "printed_page_start": page,
            "printed_page_end": page,
            "pdf_page_start": page,
            "pdf_page_end": page,
            "shape": "C+BUNDLE",
            "section_category": "collection",
        })
    print(f"      -> {len(belt_codes)} verified codes")

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
        # newline='' is required, not cosmetic: poppler's raw output uses
        # \r\n for real line breaks and a bare \r for blank lines (verified
        # by direct byte inspection). Without newline='', Path.write_text
        # on Windows performs universal-newline translation on WRITE (every
        # \n -> \r\n, doubling the existing \r\n into \r\r\n), and a later
        # .read_text()/open() also translates on READ (any of \r\n, \r, \n
        # collapsed to \n) -- the combination silently multiplies blank-
        # line counts in the stored file (confirmed: a 1-blank-line gap in
        # the source became 2-4 blank lines after one write/read round-
        # trip). Found via a Ditre Shape-1 price parser returning 0 rows
        # because its blank-line-run stop threshold, tuned against a fresh
        # single-page pdftotext pull, was too tight for the corrupted
        # stored text -- likely also why Bolzan's own tier-row scan uses a
        # blank_run<4 threshold instead of the true 1-blank-line source
        # structure.
        (out_root / "text" / f"p{p:03d}.txt").write_text(page_text.get(p, ""), encoding="utf-8", newline='')

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
                     choices=["bolzan", "cattelan", "bonaldo", "varaschini", "ditre", "pianca"],
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
                          " ignores --index-pages and --merge. "
                          "'ditre' = flat two-column '<N>  Name' index "
                          "(number-then-name, the mirror of bonaldo's "
                          "name-then-number), coordinate-based column "
                          "split like bonaldo, footer is either "
                          "'<Name> | <N>' (Night catalogs) or a bare <N> "
                          "beside the 'Ditre Italia' wordmark (all others). "
                          "'pianca' = 3-band INDICE (category/name/page-"
                          "number columns), footer is '<N>_SECTION' or "
                          "'SECTION_<N>' -- ONLY verified against Progetti "
                          "di Design 08 so far, see parse_index_pianca's "
                          "module comment before pointing this at any of "
                          "the other 9 Pianca source PDFs.")
    ap.add_argument("--merge", action="store_true",
                     help="Merge into an existing catalog_index.json instead "
                          "of overwriting it: entries from this run replace "
                          "any existing entry with the same product_name "
                          "(kept, not duplicated), everything else in the "
                          "existing file is preserved as-is.")
    ap.add_argument("--single-product", default=None,
                     help="For --style pianca ONLY: some Pianca source PDFs "
                          "(e.g. ArmadioPrimo, 8 pages) are a single product "
                          "with no per-product photographic INDICE at all -- "
                          "parse_index_pianca has nothing to scan. When set, "
                          "skips index parsing entirely and treats the whole "
                          "file (printed page 1 to the last page an actual "
                          "footer maps to) as one product with this name. "
                          "--index-pages is not required/used in this mode.")
    args = ap.parse_args()

    pdf_path = args.pdf

    if args.style == "varaschini":
        out_root = Path(args.out) / args.brand
        run_varaschini(pdf_path, args.brand, out_root)
        return

    if not args.index_pages and not args.single_product:
        print("ERROR: --index-pages is required for --style "
              f"{args.style!r} (only 'varaschini' and --single-product can omit it).")
        sys.exit(1)

    reader = PdfReader(pdf_path)
    total_pages = len(reader.pages)

    if args.single_product:
        if args.style != "pianca":
            print("ERROR: --single-product is only supported for --style pianca.")
            sys.exit(1)
        print(f"[1/5] Skipping index parsing (--single-product {args.single_product!r})...")
        entries = [(args.single_product, 1)]
    else:
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
        elif args.style == "ditre":
            entries = parse_index_ditre(pdf_path, index_pages)
        elif args.style == "pianca":
            entries = parse_index_pianca(pdf_path, index_pages)
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
    elif args.style == "ditre":
        page_map = build_ditre_page_map(pdf_path, total_pages)
        # Same rationale as bonaldo: don't assume an arithmetic offset.
        # Falling back to identity for the rare unreadable-footer page is
        # safer than a formula neither confirmed nor even hypothesized for
        # this brand.
        page_fallback = lambda p: p
    elif args.style == "pianca":
        page_map = build_pianca_page_map(pdf_path, total_pages)
        # Unlike bonaldo/ditre, Pianca's printed page numbers do NOT equal
        # PDF page index (confirmed offset of +3 throughout Progetti 08 --
        # printed page 1 is real PDF page 4, since the file opens with a
        # cover + front-matter + INDICE before content starts). Also
        # unlike Bolzan/Cattelan, there's no known universal formula
        # (offset differs per source file). build_offset_fallback derives
        # the fallback from the ACTUAL page_map's own most-common offset
        # rather than assuming one, so an unreadable-footer page (e.g. a
        # full-bleed section divider) still gets a sane estimate instead
        # of silently mismapping to itself.
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

    if args.style == "pianca":
        source_filename = Path(pdf_path).name
        dropped_names = []
        renamed = []
        within_file_renamed = []
        kept_ranges = []
        for item in ranges:
            key = (source_filename, item["name"])
            within_file_key = (source_filename, item["name"], item["printed_start"])
            if key in PIANCA_INDEX_NAME_OVERRIDES:
                override = PIANCA_INDEX_NAME_OVERRIDES[key]
                if override is None:
                    dropped_names.append(item["name"])
                    continue
                renamed.append((item["name"], override))
                item["name"] = override
            elif within_file_key in PIANCA_WITHIN_FILE_OVERRIDES:
                # Checked as a separate, more specific table rather than
                # folded into PIANCA_INDEX_NAME_OVERRIDES above -- see
                # PIANCA_WITHIN_FILE_OVERRIDES' own comment: several raw
                # entries can share the exact same (source_file, name) key
                # when they collide WITHIN one file under the same
                # in-file category, so a plain name-keyed table can't
                # distinguish them at all; printed page number can.
                override = PIANCA_WITHIN_FILE_OVERRIDES[within_file_key]
                within_file_renamed.append((item["name"], item["printed_start"], override))
                item["name"] = override
            kept_ranges.append(item)
        ranges = kept_ranges
        if dropped_names or renamed or within_file_renamed:
            print(f"      -> PIANCA_INDEX_NAME_OVERRIDES applied ({source_filename}):")
            for n in dropped_names:
                print(f"           dropped (confirmed superseded_reprint/duplicate): {n!r}")
            for old, new in renamed:
                print(f"           renamed (confirmed cross-file collision): {old!r} -> {new!r}")
            for old, pg, new in within_file_renamed:
                print(f"           renamed (confirmed within-file collision, printed p.{pg}): {old!r} -> {new!r}")

    out_root = Path(args.out) / args.brand
    (out_root / "pages").mkdir(parents=True, exist_ok=True)
    (out_root / "images").mkdir(parents=True, exist_ok=True)
    (out_root / "text").mkdir(parents=True, exist_ok=True)

    # Cross-run slug collisions silently overwrite mini_pdf/images/text
    # FILES on disk even when --merge correctly protects catalog_index.json
    # itself. Confirmed as live data loss twice: Ditre Italia's sofa/
    # armchairs merge (see DITRE_NAME_DISAMBIGUATION's own comment above --
    # fixed there by exhaustively pre-renaming every collision found by
    # checking all pairwise file combinations up front), and Pianca
    # 2026-08-21 (Peonia (Divani)/Cornice, then Palù/"Mensole legno per
    # boiserie" -- 2 MORE collisions neither anticipated nor caught by the
    # PIANCA_INDEX_NAME_OVERRIDES table, found only by chance while
    # re-verifying an unrelated entry). Pre-renaming every collision is
    # necessary but not SUFFICIENT on its own -- it only protects names
    # someone thought to check in advance. This is the structural
    # complement.
    #
    # Deliberately keyed by SOURCE FILE, not product_name -- an earlier
    # version of this fix compared product_name instead and would have
    # been a no-op for the exact bug it was meant to catch: Palù and
    # Mensole legno per boiserie collide precisely BECAUSE their raw
    # names are identical across files, so "does the name match" can
    # never distinguish them. Source file is the one thing that's always
    # different between two genuinely different products' extraction
    # runs, and always the SAME when a run is legitimately re-extracting
    # its own prior output (e.g. this project's own restore-by-rerunning
    # recovery procedure).
    #
    # Legacy entries predating the source_file field (added 2026-08-21)
    # have no source_file to compare -- falls back to product_name for
    # those specifically (the same weaker signal this whole fix exists
    # to move away from, but it's the only one available for them, and
    # only matters until they're next re-extracted and pick up a real
    # source_file). Deliberately NOT "unknown source = always foreign":
    # that would make re-extracting a legacy file's OWN unchanged
    # products (e.g. to fix an unrelated missing_from_index gap in the
    # same file) spuriously fragment every one of its slugs into "_2"
    # duplicates on every run, which is real friction, not just an
    # abundance of caution -- name equality is a fine signal for "is
    # this run re-touching its own prior output" once source_file itself
    # is unavailable.
    existing_slug_source: dict[str, str | None] = {}
    existing_slug_name: dict[str, str] = {}
    catalog_path_for_ownership_check = out_root / "catalog_index.json"
    if args.merge and catalog_path_for_ownership_check.exists():
        for e in json.loads(catalog_path_for_ownership_check.read_text(encoding="utf-8")):
            if "slug" in e:
                existing_slug_source[e["slug"]] = e.get("source_file")
                existing_slug_name[e["slug"]] = e.get("product_name")
    current_source_file = Path(pdf_path).name

    def _slug_owned_by_someone_else(slug: str, name: str) -> bool:
        if slug not in existing_slug_source:
            return False
        owner_source = existing_slug_source[slug]
        if owner_source is not None:
            return owner_source != current_source_file
        return existing_slug_name.get(slug) != name

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
        while slug in used_slugs or _slug_owned_by_someone_else(slug, name):
            # Two genuinely different index entries produced the same slug
            # (e.g. two distinct products that happen to share a bare name
            # like "Ciro" for both a bed and its matching nightstand) --
            # rather than silently letting the second one's files overwrite
            # the first's, disambiguate the slug and flag it for review.
            # _slug_owned_by_someone_else extends this across runs, not
            # just within this one (see its own comment above) -- if this
            # slug is already on disk from a DIFFERENT source file, that's
            # a collision too, even though used_slugs (this run's own
            # claims) doesn't know about it. A slug already owned by THIS
            # SAME source file is fine -- that's an intentional
            # re-extraction/update of this file's own prior output, not a
            # collision with something else.
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
        # newline='' required -- see the matching write_text call in
        # run_varaschini for the full explanation of the round-trip
        # blank-line-doubling bug this avoids.
        text_path.write_text("\n\n".join(text_chunks), encoding="utf-8", newline='')

        entry = {
            "brand": args.brand,
            "product_name": name,
            "slug": slug,
            "source_file": Path(pdf_path).name,
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
              f"the same slug with another entry (either from this same run, "
              f"or already on disk from a PREVIOUS extraction run of a "
              f"different source file -- both are now caught) -- these are "
              f"likely genuinely different products that just share a bare "
              f"name (verify against the real page before trusting):")
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