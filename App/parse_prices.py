import re, json, argparse, sys, subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract_catalog import PDFTOTEXT  # noqa: E402 -- reuse the same PATH-shadowing-safe poppler resolution

# Some catalogs' product names include characters (À, Ø...) outside
# Windows' default console codepage (cp1252) -- without this, a plain
# print() of such a name crashes the whole script instead of just showing
# a '?' in place of the unprintable character.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="backslashreplace")

# Known real fabric/leather price-tier category names used across this
# catalog. Restricting to this whitelist (rather than "any short label
# followed by numbers") prevents stray metadata rows -- like a leftover
# 'ml (rivestimento)' or 'Peso colli (kg)' row from an interleaved
# neighboring table -- from ever being mistaken for a real price tier.
KNOWN_TIERS = {
    'b e tcl', 'c', 'd', 'e', 'plus', 'extra',
    'luxury leather', 'super', 'nuvola leather',
}

def tokenize_chunk(chunk):
    return [t for t in re.split(r'\s+', chunk.strip()) if t]

def tokenize_size_chunk(chunk):
    """Like tokenize_chunk, but first joins compound 'WIDTH x h.HEIGHT'
    size labels (e.g. '106 x h.140', used for tall furniture like
    bookcases/wardrobes measured by width and total height, as opposed to
    the usual WIDTHxDEPTH bed format which has no internal spaces) into a
    single token before splitting. Without this, naive whitespace
    splitting breaks such a label into three separate fragments ('106',
    'x', 'h.140'), corrupting the resulting size list."""
    joined = re.sub(r'(\d+)\s+x\s+(h\.\s*\d+)', lambda m: f"{m.group(1)}x{m.group(2).replace(' ', '')}",
                     chunk, flags=re.IGNORECASE)
    return tokenize_chunk(joined)

def slice_chunk(target_line, bounds, c):
    """Slice out table #c's portion of target_line using the column bounds
    computed from the Codice line. For every table EXCEPT the last one, the
    boundary is a real gap between two side-by-side tables, so it's safe to
    cut there. For the LAST table, do NOT cut at bounds[-1] (which is just
    the Codice line's own length) -- cut to the end of target_line instead.
    Otherwise, whenever a price/value line is even one character wider than
    the Codice line it borrowed its bounds from (e.g. a 4-digit price where
    the code was shorter), the trailing digit gets silently chopped off."""
    start = bounds[c]
    if start >= len(target_line):
        return ''
    is_last = (c == len(bounds) - 2)
    end = len(target_line) if is_last else bounds[c + 1]
    return target_line[start:end]

def try_parse_tier_row(ch):
    """If chunk `ch` is a valid 'TierName   value value value' row (where
    TierName is a recognized fabric-tier label), return (label, values).
    Otherwise return (None, []).

    Only the LEADING run of genuine numeric-looking tokens is kept --
    trailing content is discarded rather than rejecting the whole row.
    This matters because on dense two-column pages, unrelated text from a
    neighboring table (e.g. a 'Piedi' accessory sub-table's own row) can
    land on the exact same text line as a real tier row purely by
    coincidence of vertical alignment, appending garbage after the real
    values. Requiring the WHOLE trailing string to be pure numeric would
    silently drop an otherwise-valid row over a few stray words at the end.
    """
    # allow an optional '+' right before the numeric run -- this catalog
    # sometimes writes surcharges as '+ 356' with a space after the plus
    split_m = re.search(r'\s{2,}(?=[\d+-])', ch)
    if not split_m:
        return None, []
    candidate_label = ch[:split_m.start()].strip()
    if candidate_label.lower() not in KNOWN_TIERS:
        return None, []

    candidate_values_str = ch[split_m.end():].strip()
    normalized = re.sub(r'\+\s+', '+', candidate_values_str)
    raw_tokens = tokenize_chunk(normalized)

    values = []
    for tok in raw_tokens:
        if re.fullmatch(r'[+-]?[\d.,]+|-', tok):
            values.append(tok)
        else:
            break  # stop at the first non-numeric token (leaked garbage)
    if not values:
        return None, []
    return candidate_label, values

MISURA_RE = re.compile(r'^\s*MISURA CM\b')


def _parse_cattelan_block(lines, i, seg_start, seg_end, page_of_line, product_name, brand, flags):
    """Parse one 'MISURA CM ... EUR [EUR ...]' price block starting at line i
    (the header line itself). Returns (rows, next_i) -- next_i is where the
    caller should resume scanning for further blocks in this same segment
    (Cattelan products commonly stack more than one block on one page, e.g.
    different top-material families each with their own size/price grid)."""
    header_line = lines[i]
    eur_positions = [m.start() for m in re.finditer(r'\bEUR\b', header_line)]
    eur_count = len(eur_positions)
    per_col_top = None

    if eur_count == 0:
        flags.append((page_of_line[i], product_name,
                       f"MISURA CM header not parseable (no EUR column found) near line {i}"))
        return [], i + 1

    # Minimum gap between adjacent EUR columns. Prices are right-aligned
    # very close to their own "EUR" label, so a tight radius (half the
    # smallest real gap) correctly matches them while still rejecting
    # unrelated m³/colli tokens sitting further away. Column-label text
    # (below) needs a much LOOSER radius: a label describes the whole
    # column and starts well to the LEFT of "EUR", not right up against
    # it -- verified against real data (Sierra pouf, Rado outdoor table,
    # PEYOTE): real labels sit 50-100% of a column's width away from
    # their own EUR position, while the row's own category word
    # ("Rivestimento", "Base", "Seduta", "Top") consistently sits farther
    # than a full column-width away. 100% of the smallest gap reliably
    # includes the former and excludes the latter across every header
    # shape checked -- 90% was tried first and was tight enough to
    # exclude a real label by a handful of characters on PEYOTE, which
    # let a second, unrelated label chunk wrongly claim its column first.
    col_gaps = [eur_positions[k + 1] - eur_positions[k] for k in range(len(eur_positions) - 1)]
    match_radius = (min(col_gaps) // 2) if col_gaps else 20
    label_match_radius = min(col_gaps) if col_gaps else 40

    # Look upward (within this segment only) for the column-label row(s)
    # immediately above this MISURA CM header. Deliberately NOT keyed to
    # specific words like "Top"/"Base": this catalog uses a different
    # category word depending on furniture type -- tables use "Top"/
    # "Base", chairs use "Base"/"Seduta" or just "Rivestimento", storage
    # furniture uses "Struttura"/"Frontali", combined headers use
    # "Base + Top", etc.
    #
    # A long finish-combo label (e.g. "Pelle Nabuk / Magnifica Divani")
    # commonly WRAPS across 2-3 physical text lines instead of staying on
    # one -layout line. Reconstructing this by BY POSITION -- matching
    # each already-split chunk to its nearest EUR column, the same
    # technique verified above for data-row prices -- rather than by
    # character-range slicing: an earlier attempt sliced arbitrary
    # character ranges between column midpoints and that split real
    # labels mid-word (confirmed on BUTTERFLY: "Brushed Bronze / Brushed
    # Grey" came out as "Bru...B" / "shed Bronze/...ushed Grey"). Matching
    # whole pre-split chunks by position instead of slicing avoids that
    # failure mode entirely -- verified against both BUTTERFLY and SIERRA
    # pouf's real pages before trusting this.
    #
    # Tolerant of blank lines while searching: this catalog's -layout
    # extraction inserts a blank line between EVERY visually-separated
    # text row throughout the whole document (confirmed: even
    # mid-paragraph sentences are blank-line-separated), so a single blank
    # does NOT mean "no label row here", only several consecutive ones do.
    def _chunks_with_pos(line):
        return [(m.group().strip(), m.start()) for m in re.finditer(r'\S+(?:[ \t]\S+)*', line)]

    _LABEL_WORD_RE = re.compile(
        r'^(Top|Base(?:\s*\+\s*Top)?|Seduta|Rivestimento|Struttura|Frontali|'
        r'Fronte|Telaio|Retro|Accessori|Inserti|Impianto|Paralume\s*/\s*Attacco)'
        r'\b\s*', re.IGNORECASE)

    def _match_line_to_cols(line):
        """Match this ONE line's chunks to EUR columns."""
        if eur_count == 1:
            # No adjacent column exists to derive a sensible position
            # radius from, and none is needed -- there's nowhere else a
            # label chunk COULD belong. A gap-based radius here is not
            # just unnecessary but actively wrong: a single-column
            # label can legitimately start far to the left of its lone
            # EUR position (confirmed regression on ATRIUM Keramik,
            # whose real label sits ~90 characters from "EUR" with
            # nothing else competing for that space). Just take the
            # whole line, minus a recognized leading category word if
            # present, as column 0's text -- the same simple approach
            # already proven correct for every single-column product
            # tested (Atlantis, Bombè's "Base" line, etc).
            stripped = line.strip()
            if not stripped:
                return []
            m = _LABEL_WORD_RE.match(stripped)
            text = stripped[m.end():].strip() if m else stripped
            return [(text, 0)] if text else []

        # Greedy left-to-right, each column claimed by at most one chunk
        # from this line -- plain independent "nearest column" per chunk
        # breaks when two adjacent real labels (e.g. "Pelle" / "Pelle
        # Glove") both happen to sit closer to the SAME column than to
        # their own, silently merging both into it and leaving the true
        # rightmost column empty (confirmed on SIERRA pouf). Excluding a
        # column once THIS line has already claimed it forces the
        # remaining chunks to their own next-nearest (and correct)
        # column instead.
        used: set[int] = set()
        result = []
        for text, pos in _chunks_with_pos(line):
            best_col, best_dist = None, None
            for col, col_pos in enumerate(eur_positions):
                if col in used:
                    continue
                dist = abs(pos - col_pos)
                if best_dist is None or dist < best_dist:
                    best_col, best_dist = col, dist
            if best_col is not None and best_dist <= label_match_radius:
                used.add(best_col)
                result.append((text, best_col))
        return result

    col_texts: list[list[str]] = [[] for _ in range(eur_count)]
    filled = [False] * eur_count
    # Which recognized category WORD (e.g. "Base", "Top", "Rivestimento",
    # "Struttura", "Seduta") each column's fabric_tier text actually came
    # from -- purely for DISPLAY (the chat UI hardcodes a "FABRIC" column
    # header regardless of product type, which is wrong for anything that
    # isn't upholstery: a marble table's price varies by "Base" finish, a
    # crystal table's by "Top" material, neither of which is "fabric" at
    # all). Populated ADDITIVELY alongside the existing col_texts fill
    # logic below, from the SAME line, using the SAME already-verified
    # _LABEL_WORD_RE -- never changes which text ends up in col_texts
    # itself (that position-based matching logic is untouched), only
    # records which leading word (if any) that line's text started with.
    col_label_words: list[str | None] = [None] * eur_count
    top_label = None
    fill_lines = 0  # how many distinct lines contributed at least one new column fill
    k = i - 1
    steps = 0
    blank_run = 0
    while k >= seg_start and steps < 15:
        cand_strip = lines[k].strip()
        if not cand_strip:
            blank_run += 1
            if blank_run > 2:
                break
            k -= 1
            continue
        blank_run = 0
        # Real prose (designer credit, product description) starts near
        # the left margin and runs long -- every real label/header line
        # in this catalog sits well to the right, roughly over its own
        # column(s). This is what distinguishes a wrapped label fragment
        # (short, indented) from body text, regardless of which category
        # word (if any) starts the line.
        first_pos = len(lines[k]) - len(lines[k].lstrip())
        if first_pos < 30 and len(cand_strip) > 40:
            break
        steps += 1
        line_matches = _match_line_to_cols(lines[k])
        # Lines are processed CLOSEST-to-MISURA-CM first (walking upward),
        # so a close label ("Base"/"Seduta"/"Rivestimento", genuinely 1:1
        # with the EUR columns) always fills real columns before a
        # farther, broader one ("Top", describing the whole block) is
        # ever reached. A chunk landing on an ALREADY-filled column is
        # therefore the farther line encroaching on the close line's
        # territory -- skip just that one chunk (don't overwrite) rather
        # than discarding the whole line, so the line's OTHER chunks
        # (for columns not yet filled) still get merged. Deliberately NOT
        # attempting to also merge genuine multi-line-wrapped fragments
        # into already-filled columns: tried it (to handle a label
        # wrapping across 3+ physical lines with interleaved columns,
        # confirmed on SIERRA pouf's 6-column table) and it's ambiguous
        # enough between "this is the same wrap continuing" vs "this is
        # the farther label's own wrap" that it silently cross-contaminated
        # adjacent columns' text on that same product (verified) -- safer
        # to flag a header this tangled for manual review than guess.
        new_fills = [(text, col) for text, col in line_matches if not filled[col]]
        if not new_fills and top_label is None and re.match(r'^\s*Top\b', lines[k]):
            # This entire line was either unmatched or fully absorbed by
            # already-filled columns -- it contributed nothing new, which
            # is exactly what the farther/"Top" context line looks like.
            # Only trust it as a real model_variant label when the line
            # actually starts with "Top" -- otherwise (e.g. a stray
            # wrapped material-code fragment like "KS23" that simply
            # didn't land near any column) it's not a meaningful label,
            # just noise; better to leave model_variant null than show a
            # fragment.
            top_text = re.sub(r'^\s*Top\s*', '', lines[k]).strip()
            top_label = top_text or None
        if new_fills:
            fill_lines += 1
            # Same line, same regex already used to strip a leading
            # category word in the eur_count==1 branch of
            # _match_line_to_cols above -- applied here to the RAW line
            # text (not the position-matched chunk) purely to record
            # which word it was. A multi-line-wrapped label (e.g.
            # "Base" on its own physical line, values on the next) means
            # this specific line might not itself start with the word --
            # in that case label_word is None and the column's label
            # just stays unset, same as it already does today; this is
            # additive display metadata, not a new correctness
            # requirement.
            label_m = _LABEL_WORD_RE.match(cand_strip)
            label_word = label_m.group(1).strip() if label_m else None
            if label_word:
                for _, col in new_fills:
                    if col_label_words[col] is None:
                        col_label_words[col] = label_word
        for text, col in new_fills:
            col_texts[col].insert(0, text)
            filled[col] = True
        k -= 1

    base_chunks = [' '.join(c).strip() for c in col_texts]

    if not any(filled):
        flags.append((page_of_line[i], product_name,
                       f"no column-label row found above MISURA CM header near line {i}"))
        return [], i + 1

    if not all(filled):
        flags.append((page_of_line[i], product_name,
                       f"only {sum(filled)}/{eur_count} label column(s) resolved above MISURA CM "
                       f"header near line {i} -- column count mismatch, needs manual check"))
        return [], i + 1

    if eur_count > 1 and fill_lines > 1 and len(set(base_chunks)) != len(base_chunks):
        # Two DIFFERENT columns resolved to the IDENTICAL label text, AND
        # it took more than one physical line to resolve them all -- a
        # sure sign some wrapped fragment landed on the wrong column
        # (confirmed on SIERRA pouf's 6-column table, where 4 columns
        # all wrongly ended up as bare "Divani"). NOT applied when a
        # single clean line resolved every column at once (verified real
        # case on ELIOT Keramik Drive: two genuinely different Top
        # material groups legitimately share the exact same 2 Base
        # finish options, so "GFM69 / GFM73" correctly appears twice --
        # that's real repeated data, not a merge failure).
        flags.append((page_of_line[i], product_name,
                       f"label reconstruction produced duplicate column names "
                       f"{base_chunks} near line {i} -- needs manual check"))
        return [], i + 1

    rows = []
    t = i + 1
    blank_run = 0
    mismatched_rows = 0
    pending_size = None  # most recent real size, for rows whose price
                          # wrapped onto a separate continuation line
    while t < seg_end and blank_run < 4:
        raw = lines[t]
        if raw.strip() == '':
            blank_run += 1
            t += 1
            continue
        blank_run = 0
        if MISURA_RE.match(raw) or raw.strip().startswith(('Top', 'Base')):
            break  # next block starts here
        if re.match(r'^\s*(C\.O\.M\.|C\.O\.L\.)\s', raw) or 'N.B.:' in raw or 'Versione conforme' in raw:
            # A footnote/disclaimer sentence -- NOT a malformed data row.
            # Two confirmed patterns so far: a fabric/leather
            # custom-material note ("C.O.M. tessuto cliente/ecopelle per
            # sedia cm 130x140, C.O.L. pelle cliente mq 2. N.B.: per
            # tessuti a righe o fantasia aggiungere il 50% di
            # materiale.") and a weight/compliance note ("Versione
            # conforme alla norma ASTM F2057-23 fornita con zavorra: + kg
            # 38"). Both genuinely contain digits (so the no-digit check
            # below can't catch them) AND, unlike most trailing notes,
            # can coincidentally have one of those digits positioned close
            # enough to a real EUR column to look like a legitimate price
            # (the ASTM note's "38" did) -- so this check must run
            # UNCONDITIONALLY, checked by literal wording, not gated on
            # whether a nearby price was found. Always end the block here
            # when one of these exact markers appears.
            break
        # Size = the leading chunk (2+-space-separated), sometimes with
        # INCHES glued on by single spaces if there wasn't a 2+ gap
        # between them -- harmless, size matching downstream only needs
        # the digits.
        chunks = [c.strip() for c in re.split(r'\s{2,}', raw.strip()) if c.strip()]
        candidate = chunks[0] if chunks else None

        if candidate and '/' in candidate and re.fullmatch(r'[\d/\s]+', candidate):
            # A leftover superscript-fraction rendering fragment (e.g.
            # "1/8   1/4", the stray continuation of an INCHES value's
            # superscript formatting) -- pure digits/slashes/spaces
            # containing at least one "/". Not a size, not a price row,
            # not pending-size-worthy either -- just noise between two
            # real rows. Skip it entirely without touching pending_size.
            # Requiring a "/" (not just "no letters") matters: some
            # products key real variants on bare numbers with NO letters
            # and NO slash at all (confirmed on STILO: sizes "1", "6",
            # "12" for lamp-shade count) -- those must NOT be caught here.
            t += 1
            continue

        # Some rows split their MISURA CM value and its price across TWO
        # physical lines (confirmed on ATRIUM Keramik: the size line has
        # no room left for m³/colli/price, which wrap onto their own
        # line below with no size prefix repeated) -- without this, the
        # leaked m³ value ("1,60") on that continuation line got read as
        # if IT were the size. A real size never contains a comma (sizes
        # use "x" separators; m³ is the only thing in this position that
        # uses Italian comma-decimal notation), so a comma-only leading
        # chunk is never a genuine size -- fall back to the last real
        # size seen instead of introducing a bogus new row.
        if candidate and re.fullmatch(r'[+-]?\d[\d,]*', candidate) and ',' in candidate:
            size = pending_size
        else:
            size = candidate
            if size:
                pending_size = size

        # Assign each numeric token on this row to its EUR column BY
        # POSITION, not by "take the last N tokens". That naive approach
        # breaks silently whenever a row has FEWER real prices than the
        # header's column count (some size/finish combos are legitimately
        # blank for a given row -- confirmed on ELIOT Keramik Drive, whose
        # "Top" spans two material groups but each row only prices ONE of
        # them): the leftover m³/colli values are also numeric-looking, so
        # they got miscounted as prices and silently shifted into the
        # wrong columns instead of being skipped. Position-based matching
        # (verified byte-for-byte against the real page) doesn't have that
        # failure mode: a token only counts as a price for column N if
        # it's actually printed near column N's own EUR label position.
        #
        # m³ values always use a comma (Italian decimal, e.g. "0,40") --
        # EUR prices in this catalog never do (whole numbers or
        # dot-thousands, e.g. "4.519", "791") -- so excluding comma tokens
        # cleanly removes m³ regardless of its position.
        col_values: dict[int, str] = {}
        if size:
            for m in re.finditer(r'[+-]?\d[\d.,]*', raw):
                tok = m.group()
                if ',' in tok:
                    continue  # m³ value, never a price
                pos = m.start()
                nearest_col, nearest_dist = None, None
                for col, col_pos in enumerate(eur_positions):
                    dist = abs(pos - col_pos)
                    if nearest_dist is None or dist < nearest_dist:
                        nearest_col, nearest_dist = col, dist
                if nearest_dist is not None and nearest_dist <= match_radius and nearest_col not in col_values:
                    col_values[nearest_col] = tok

        # A size code is normally digit-based (e.g. "240x120x74h"), but
        # some products key their variants on bare letters instead (e.g.
        # SPINNAKER's "X fissaggio a muro" / "Y fissaggio a soffitto" for
        # wall- vs ceiling-mount, MELODY's lamp shade sizes "A".."E") --
        # confirmed both silently produced ZERO rows because the old
        # digit-only check treated their very first real data row as
        # trailing text and stopped immediately. A real data row -- digit
        # or letter-coded -- always has an actual price positioned near
        # an EUR column; a genuine trailing note (EOL notice, "New
        # Collection", disclaimer sentence) never does. Check for a
        # nearby price FIRST, and only fall back to the digit/length
        # heuristics when there isn't one. Checked against `candidate`
        # (this line's own leading chunk), never `size` -- when size was
        # inherited from `pending_size` (the comma-leak case above),
        # `candidate` is the m³ fragment itself and correctly skips these
        # checks entirely, while `size` (the carried-forward real size)
        # would wrongly look fine either way and mask a genuine trailing
        # line if checked instead.
        if candidate == size and size and not col_values:
            if not re.search(r'\d', size):
                break  # no digit AND no nearby price -- e.g. "Prodotto EOL"
            if len(size) > 45 and len(size.split()) > 4:
                break  # long prose sentence AND no nearby price -- a disclaimer

        if size and col_values:
            for col, price in sorted(col_values.items()):
                rows.append({
                    "brand": brand,
                    "product_name": product_name,
                    "model_variant": per_col_top[col] if per_col_top else top_label,
                    "variant_context": None,
                    "size": size,
                    "fabric_tier": base_chunks[col] if eur_count > 1 else None,
                    "tier_label": col_label_words[col] if eur_count > 1 else None,
                    "code": None,
                    "price_eur": price,
                    "source_pdf_page": page_of_line[t],
                })
        elif size:
            mismatched_rows += 1
        t += 1

    if mismatched_rows:
        flags.append((page_of_line[i], product_name,
                       f"{mismatched_rows} size row(s) in the block near line {i} had no "
                       f"price token confidently positioned under any of the {eur_count} EUR "
                       f"column(s) -- skipped rather than guessed, needs manual check"))

    return rows, t


def parse_file_cattelan(path, product_name, brand, all_headings=None, heading_text=None):
    """Cattelan's price tables have no 'Codice' concept at all (confirmed:
    zero occurrences in the whole catalog) -- instead each product page has
    one or more stacked 'Top' (material family) / 'Base' (finish-combo
    column headers) / 'MISURA CM ... EUR' size-price grids. Also, unlike
    Bolzan's side-by-side multi-table pages, Cattelan stacks multiple
    products/variants VERTICALLY on a shared page (e.g. two material
    variants of the same base product printed one after another) -- so
    instead of Bolzan's horizontal column-ownership slicing, ownership here
    is determined by finding this product's own heading line and only
    parsing text up to the NEXT different product's heading.

    `heading_text` is the literal string to search for in the extracted
    text -- normally identical to `product_name`, but for the handful of
    products disambiguated at extraction time (two genuinely different
    items that share one bare printed name, e.g. "ATRIUM Keramik" the
    table vs. "ATRIUM Keramik Consolle" the console -- the source PDF
    always just prints "ATRIUM Keramik") this is the ORIGINAL un-renamed
    heading, while `product_name` is the disambiguated display name
    recorded on the output rows. `all_headings` is the set of every
    product's real heading text (not display names), used to detect where
    the NEXT different product's content begins."""
    heading_text = heading_text or product_name
    all_headings = set(all_headings) if all_headings else {heading_text}
    with open(path, encoding='utf-8') as f:
        lines = f.read().split('\n')

    page_of_line = [None] * len(lines)
    current_page = None
    for idx, ln in enumerate(lines):
        m = re.match(r'^<<<PDFPAGE:(\d+)>>>$', ln.strip())
        if m:
            current_page = int(m.group(1))
        page_of_line[idx] = current_page

    heading_positions = [(idx, ln.strip()) for idx, ln in enumerate(lines) if ln.strip() in all_headings]
    own_starts = [idx for idx, name in heading_positions if name == heading_text]

    flags = []
    if not own_starts:
        flags.append((None, product_name, "own heading not found in its extracted text -- nothing parsed"))
        return [], flags

    rows = []
    for start in own_starts:
        end = len(lines)
        for idx, name in heading_positions:
            if idx > start and name != heading_text:
                end = idx
                break
        i = start
        while i < end:
            if MISURA_RE.match(lines[i]):
                block_rows, next_i = _parse_cattelan_block(lines, i, start, end, page_of_line, product_name, brand, flags)
                rows.extend(block_rows)
                i = next_i
            else:
                i += 1

    return rows, flags


# Bonaldo chair-shape price tables (confirmed via real page images before
# writing this, e.g. AGEA/ALLEY/ARTIKA/MASK/MIDA p.41-53): every price
# comes with its own alphanumeric CODICE right next to it (unlike
# Cattelan), so anchor extraction on that instead of column-position math.
# A real code is ALWAYS immediately followed by a price number (enforced
# by every regex that uses this pattern) -- that requirement alone
# already excludes plain header words like "GAMBE" (never followed by a
# bare number in that shape), so no separate digit-in-the-code
# requirement is needed. An earlier version DID require a digit/'Ø'
# within the code specifically to rule out "GAMBE" -- confirmed too
# strict on real data: some genuine codes are purely alphabetic (e.g.
# "TBMC", CIRCUS's Dover White finish), and requiring a digit silently
# misread that whole data row as a category-name line instead.
_BONALDO_CODE = r'[A-Z][A-Z0-9Ø]{2,4}'
BONALDO_TIER_ROW_RE = re.compile(
    # a trailing bare "X" (leg-availability checkbox marker, appears on
    # some rows but not others in the real rendered page) can follow
    # either the 1st or 2nd code/price pair -- optional and discarded
    rf'^(.*?)\s{{2,}}({_BONALDO_CODE})\s+([\d.,]+)(?:\s{{2,}}X)?'
    rf'(?:\s{{2,}}({_BONALDO_CODE})\s+([\d.,]+)(?:\s{{2,}}X)?)?\s*$'
)
# A row whose CODICE is identical to the row above it sometimes omits the
# code entirely, printing only the price (confirmed via real page image:
# MASK p.52, "Miss Mask" table, "Must" row) -- caller falls back to the
# nearest code seen earlier in the SAME group/column when this matches.
BONALDO_TIER_ROW_NOCODE_RE = re.compile(
    rf'^(.*?)\s{{2,}}([\d.,]+)(?:\s{{2,}}X)?(?:\s{{2,}}([\d.,]+)(?:\s{{2,}}X)?)?\s*$'
)
# A 2nd, simpler shape variant (confirmed via real page text: Dune/Obel/
# Mistral/Camillo/Bon Ton "Bio-s"/Pin -- console/cabinet/mirror/lamp
# products with a single finish list, no leg-group dimension at all): the
# header line is just "<name>  <TRIGGER WORD>" with nothing else on it,
# and the row grammar underneath (subheader -> optional material-category
# line(s), skipped by the block parser same as any other unrecognized
# line -> CODICE+price rows) is otherwise identical to the classic
# RIVESTIMENTO/CODICE/GAMBE chair shape. Each of these words was
# individually verified against a real rendered page image before being
# whitelisted here -- do not add more without the same check (the catalog
# has many OTHER numbered "CARATTERISTICHE TECNICHE" callout words, e.g.
# RIPIANI INTERNI/CONTENITORE/TOP/MONTANTE, that were seen during this
# search but NOT YET confirmed to follow this same simple grammar --
# Roll's MONTANTE in particular looks like a genuinely different
# multi-named-column shape, not this one).
#   - PIANO: added 2026-08-07, visually confirmed on Gauss p.189 (its own
#     independently-priced "PIANO" sub-table, separate from RIPIANO/
#     CASSETTO on the same page) and Isabey desk p.14. NOTE: adding PIANO
#     alone (without RIPIANO/CASSETTO) silently absorbed Gauss's and
#     Scriba's own RIPIANO/CASSETTO sub-tables into their PIANO block
#     (values still correct, but mislabeled tier_label/size) until those
#     words were ALSO whitelisted -- confirmed via a full-corpus scan of
#     every PIANO-gaining product for a same-page sibling sub-block word;
#     only these 2 of 32 were affected. PIANO/RIPIANO/CASSETTO were kept
#     as 3 separate code changes (each independently visually verified)
#     but deliberately generated/regression-tested/committed together as
#     ONE change, not 3, specifically because of this cross-dependency.
#   - RIPIANO: added 2026-08-07, visually confirmed on Gauss p.189 (its
#     own "RIPIANO" ceramica sub-table, separate CODICE/price list from
#     PIANO/CASSETTO) and Scriba p.191 ("Scriba ripiano" -- Cuoio, YØAX,
#     372, its own single-row RIPIANO sub-table separate from PIANO's
#     Noce Canaletto/JØAX/3.254).
#   - CASSETTO: added 2026-08-07, visually confirmed on Gauss p.189 (its
#     own "▽ CASSETTO" sub-table -- "Nella stessa finitura della base",
#     YØAZ, 292 -- separate from PIANO/RIPIANO on the same page).
#   - TOP: added 2026-08-07, visually confirmed on Aureo p.566 (single
#     material-column "▽ TOP" list, Ceramica opaca/finitura seta) and
#     Partout p.570 (2-group "▽ TOP" -- NOCE/FRASSINO columns, same
#     group-name mechanism already used by chair-shape's leg groups).
#   - COLORE: added 2026-08-07, visually confirmed on 4 rugs (all
#     "Tappeto"): Casablanca p.273, Amman p.52, Lomé p.53, Nairobi p.54 --
#     each has 3 repeated "<size label>  COLORE" blocks (one per size
#     variant), all colors within one size sharing the SAME code+price
#     (color is a free choice, not a price-differentiating axis here).
#     The header's own leading text is the size label itself, which
#     required 2 supporting fixes (see _BONALDO_SIMPLE_HEADER_RE_TEXT):
#     allowing a leading DIGIT (distinguished from a numbered-callout
#     false positive by checking for an immediately-following period) and
#     the U+2019 curly-apostrophe inch mark these labels use.
#   - RIVESTIMENTO: added 2026-08-08, visually confirmed on Roger p.262
#     ("Roger  RIVESTIMENTO", nothing else on the line -- a bare-header,
#     0-column variant of this same grammar: subheader "Roger" repeats
#     the product name, then a normal coded row follows directly, same
#     as Dune/Obel's 0-group case). Deliberately narrow: RIVESTIMENTO
#     headers that DO have trailing named columns (Amour/Ellison/Bull --
#     see _BONALDO_RIVESTIMENTO_NAMED_COLUMNS_RE below) don't match this
#     end-anchored pattern at all (too much trailing text), so there's no
#     overlap between the two mechanisms.
_BONALDO_SIMPLE_HEADER_WORDS = ('ANTE', 'STRUTTURA', 'PARALUME', 'BASE', 'CORNICE', 'PIANO', 'RIPIANO', 'CASSETTO', 'TOP', 'COLORE', 'RIVESTIMENTO')
# A NARROW, explicit, individually-confirmed set of OTHER real section
# trigger words -- NOT parsed themselves (not whitelisted above), but
# recognized as a block-boundary stop so a scan for a DIFFERENT
# whitelisted word's own block doesn't silently absorb their content.
# Confirmed real, found 2026-08-07 investigating a RIPIANO regression:
# Roll's page (p.223-228) prints "Roll Montante a parete / Roll Montante
# a soffitto    MONTANTE" and "Roll Contenitore    CONTENITORE" between
# its two RIPIANO sub-tables -- without stopping there, the RIPIANO scan
# kept consuming their content as if it belonged to RIPIANO, producing
# genuinely corrupted rows (a price like "6.112" ending up in the
# `fabric_tier` field).
#
# A first attempt used a fully generic "<name-like text>  <ANY ALL-CAPS
# WORD>$" pattern instead of this closed set -- reverted after it broke
# Tree/Acquerelli/Gocce/Pepita (all pre-existing, already-whitelisted-word
# blocks): their own real content incidentally contains a bare "CODICE"
# line in a position that happens to fit the same "name + all-caps word"
# shape, which isn't a real section boundary at all. A closed set,
# individually confirmed the same way the trigger-word whitelist itself
# is, avoids that false-positive class entirely.
_BONALDO_OTHER_SECTION_WORDS = ('MONTANTE', 'CONTENITORE')
_BONALDO_OTHER_SECTION_RE = re.compile(
    r'^\s*[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9 \'"-]*\s{2,}(?:'
    + '|'.join(_BONALDO_OTHER_SECTION_WORDS) + r')\s*$'
)
# The classic 2-axis header's trailing word was hardcoded to "GAMBE" alone,
# but that's just the most common of several real words the catalog uses
# for this same 2nd-axis-of-materials grammar (RIVESTIMENTO tier rows x a
# 2nd material axis, each its own CODICE+price, printed as a literal
# "RIVESTIMENTO ... CODICE ... <WORD> ... CODICE ... <WORD>" header line).
# Found 2026-08-07 by scanning every product's own heading-scoped text
# (not a naive whole-file grep, which double-counts words bleeding in from
# a NEIGHBORING product sharing the same physical page) for this exact
# repeated-word shape. Each word individually verified against a real
# rendered page image before being added here, same bar as
# _BONALDO_SIMPLE_HEADER_WORDS above:
#   - GAMBE: the original baseline (AGEA/ALLEY/ARTIKA/MASK/MIDA etc.)
#   - ASTA: Avant-Garde chair p.44 -- same grammar, "leg" is a rod/pole
#     ("asta") rather than a "gamba" for this product's design.
#   - SCHIENALE: Olos Office p.64 -- same grammar, 2nd axis is backrest
#     material (Frassino/Noce) rather than leg material.
_BONALDO_CLASSIC_2AXIS_WORDS = ('GAMBE', 'ASTA', 'SCHIENALE')
# Fixed nav/section words that render in full uppercase just like a real
# product heading would -- excluded so they're never mistaken for one.
_BONALDO_NON_HEADING_WORDS = {
    'CARATTERISTICHE TECNICHE', 'A/Z', 'NEW', 'SEDIE', 'TAVOLI', 'COMPLEMENTI',
    'ILLUMINAZIONE', 'DIVANI', 'POLTRONE & POUF', 'LETTI', 'INDEX',
}
# Anchored to the WHOLE line (name-like text, 2+ spaces, trigger word, end
# of line) -- NOT just "trigger word present anywhere" -- because several
# of these words (esp. STRUTTURA/BASE) also appear constantly as numbered
# spec-callout labels ("1. STRUTTURA   Legno laccato...", "3. BASE...")
# inside CARATTERISTICHE TECNICHE blocks elsewhere on the same page.
# Those callout lines always start with a digit+period, which the
# name-like leading character class here excludes, so they're safely
# rejected without needing a separate digit check. The trigger word is
# captured in its own group so callers can read the REAL trigger even
# when a trailing nav-badge word follows it (see next paragraph) --
# `.split()[-1]` would otherwise pick up the badge instead.
#
# An OPTIONAL trailing nav-badge word (same _BONALDO_NON_HEADING_WORDS
# set already used to reject a badge as a heading candidate elsewhere) is
# tolerated after the trigger word -- confirmed real, found 2026-08-07:
# Roll's "Roll Ripiano 90    RIPIANO    DIVANI" has a "DIVANI" sidebar
# badge bleeding onto the SAME vertical position as the real header line,
# past the trigger word. Without this, the strict end-of-line anchor
# rejected the whole line as not a header at all, silently skipping this
# entire real sub-table (0 rows, no flag, since the outer scan just moved
# on to the next line rather than ever recognizing this as a block start).
_BONALDO_SIMPLE_HEADER_RE_TEXT = (
    # Leading whitespace tolerated (confirmed real: Dune TV stand's "BASE
    # SAGOMATA"/"BASE A ZOCCOLO" sub-model headers are indented, unlike
    # every header seen while first building this) -- still safe from the
    # numbered-callout false positive, since a callout's first non-space
    # character is always a DIGIT, not a letter -- EXCEPT a real header
    # can also legitimately start with a digit itself now (a SIZE label,
    # e.g. Casablanca's "300 x 400 cm - 118'' x 157''    COLORE", p.273 --
    # confirmed real, found 2026-08-07 investigating the rug/colore
    # survey). Distinguished from a numbered callout ("1. RIVESTIMENTO")
    # by the callout's digit always being immediately followed by a
    # period -- a size label's leading digit never is (it's followed by
    # a space, "x", or "cm").  Also allows U+2019 (curly apostrophe),
    # confirmed real as this catalog's doubled-apostrophe inch mark
    # ("118''") in size labels -- the plain ASCII \' alone wasn't enough.
    r'^\s*(?!\d+\.)[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9 \'"’-]*\s{2,}('
    + '|'.join(_BONALDO_SIMPLE_HEADER_WORDS) + r')'
    # Optional single-spaced Italian "E <word>" continuation ("and X") as
    # PART OF the same heading phrase -- confirmed real, found 2026-08-07:
    # Spy's real header is "TOP E FIANCHI" ("TOP AND SIDES", p.21), single
    # spaces throughout, not the 2+-space-separated nav-badge shape below.
    # Deliberately narrow: exactly ONE word after "E", not a whole phrase,
    # so this doesn't swallow an unrelated longer descriptive line that
    # happens to contain "E" as a standalone word further along.
    r'(?:\s+E\s+[A-Za-zÀ-ÿ]+)?'
    r'(?:\s{2,}(?:' + '|'.join(re.escape(w) for w in _BONALDO_NON_HEADING_WORDS) + r'))?\s*$'
)
_BONALDO_CLASSIC_2AXIS_RE_TEXT = (
    r'RIVESTIMENTO.*CODICE.*(?:' + '|'.join(_BONALDO_CLASSIC_2AXIS_WORDS) + r')'
)
# A 3rd RIVESTIMENTO variant, structurally different from both above:
# "<name>  RIVESTIMENTO  <COLUMN1>  [<COLUMN2>]" -- 1-3 EXPLICIT named
# columns (no GAMBE/ASTA/SCHIENALE 2nd axis at all), each with its own
# CODE declared ONCE on a dedicated "CODICE ..." line right after the
# header (never repeated per tier row -- every row below is price-only).
# Confirmed via 6 individually-verified real products (2026-08-08),
# surveyed before writing any code: Amour p.44/Ellison p.45 -- ONE
# implicit column named after the product itself; Bull p.451 -- 2
# EXPLICIT material-choice columns (NOCE/ROVERE); Colibrì soft p.453/
# Nikos p.464 -- 2 explicit variant/accessory columns (COLIBRÌ/FOOTREST,
# NIKOS/CUSCINO). Deliberately capped at 3 columns (none of the 6
# verified products needs more) rather than open-ended, to keep this from
# accidentally swallowing an unrelated longer line.
#
# Each column NAME UNIT may itself be 1-2 words separated by a SINGLE
# space (not the 2+ spaces that separate DIFFERENT columns) -- confirmed
# needed by Nikos's own 2nd page (p.465, same catalog_index entry as
# p.464): its header is "RIVESTIMENTO  NIKOS HI  FOOTREST", a genuinely
# different "Nikos Hi" high-back model_variant, not a repeat of p.464's
# plain "NIKOS"/"CUSCINO" columns. Without this, "NIKOS HI" (1 space)
# wasn't recognized as a column name at all, so this 2nd header wasn't
# seen as a new block boundary -- its data rows were silently absorbed
# into the FIRST block under the WRONG "NIKOS"/"CUSCINO" labels instead
# of their own (found via the real page image, not assumed).
_BONALDO_RIVESTIMENTO_NAME_UNIT = r'[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9\'’]*(?:\s[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9\'’]*)?'
# Excludes CODICE/GAMBE/ASTA/SCHIENALE from ever being read as a column
# name -- confirmed real regression, found by diffing the full-catalog
# parse before trusting this change: several classic 2-axis products'
# OWN header lines happen to have simple single-word group names (e.g.
# "...RIVESTIMENTO  CODICE  GAMBE"), which otherwise looked exactly like
# 2 valid "column name" units to this pattern too, hijacking them away
# from the classic parsing path entirely (Agea/Artika/Itala/Mask/Mida/
# Joy/Ketch/By/Pil/Noor/Venere/Youpi and their "too"/office siblings all
# dropped to 0 or partial rows before this exclusion was added). A
# genuine named-column header (Amour/Ellison/Bull/Colibrì soft/Nikos)
# never has CODICE on the SAME line as the header -- it's always on its
# own dedicated line below -- so this exclusion costs nothing real.
_BONALDO_RIVESTIMENTO_NOT_CLASSIC_LOOKAHEAD = r'(?!.*\b(?:CODICE|GAMBE|ASTA|SCHIENALE)\b)'
_BONALDO_RIVESTIMENTO_NAMED_COLUMNS_RE_TEXT = (
    r'^\s*(?!\d+\.)[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9 \'"’-]*\s{2,}RIVESTIMENTO\s{2,}'
    + _BONALDO_RIVESTIMENTO_NOT_CLASSIC_LOOKAHEAD
    + _BONALDO_RIVESTIMENTO_NAME_UNIT + r'(?:\s{2,}' + _BONALDO_RIVESTIMENTO_NAME_UNIT + r'){0,2}\s*$'
)
_BONALDO_RIVESTIMENTO_NAMED_COLUMNS_RE = re.compile(
    r'^\s*(?!\d+\.)[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9 \'"’-]*\s{2,}RIVESTIMENTO\s{2,}'
    + _BONALDO_RIVESTIMENTO_NOT_CLASSIC_LOOKAHEAD
    + r'(' + _BONALDO_RIVESTIMENTO_NAME_UNIT + r'(?:\s{2,}' + _BONALDO_RIVESTIMENTO_NAME_UNIT + r'){0,2})\s*$',
    re.IGNORECASE
)
# The dedicated code-declaration line for the shape above -- e.g.
# "CODICE   P34Ø" (1 column) or "CODICE   F524   F525" (2 columns).
# Reuses _BONALDO_CODE (the same per-cell code pattern already proven for
# every other Bonaldo shape) rather than inventing a new one.
_BONALDO_CODICE_LINE_RE = re.compile(
    rf'^\s*CODICE\s{{2,}}({_BONALDO_CODE})(?:\s{{2,}}({_BONALDO_CODE}))?(?:\s{{2,}}({_BONALDO_CODE}))?\s*$',
    re.IGNORECASE
)
BONALDO_CHAIR_HEADER_RE = re.compile(
    _BONALDO_CLASSIC_2AXIS_RE_TEXT + '|' + _BONALDO_SIMPLE_HEADER_RE_TEXT
    + '|' + _BONALDO_RIVESTIMENTO_NAMED_COLUMNS_RE_TEXT,
    re.IGNORECASE
)


def _bonaldo_named_columns(line):
    """Returns the list of named RIVESTIMENTO columns from a header line
    like 'Bull  RIVESTIMENTO  NOCE  ROVERE' (see
    _BONALDO_RIVESTIMENTO_NAMED_COLUMNS_RE_TEXT above), or None if the
    line doesn't match this shape."""
    m = _BONALDO_RIVESTIMENTO_NAMED_COLUMNS_RE.match(line)
    if not m:
        return None
    return re.split(r'\s{2,}', m.group(1).strip())


def _bonaldo_header_trigger_word(line):
    """Return the display label for whichever header form matched `line`
    (used as the dynamic tier_label instead of hardcoding "Rivestimento" --
    the classic chair shape and the simple single-list shape use different
    real source-PDF words, e.g. Dune's is "Ante" not "Rivestimento")."""
    if re.search(_BONALDO_CLASSIC_2AXIS_RE_TEXT, line, re.IGNORECASE):
        return 'Rivestimento'
    if _BONALDO_RIVESTIMENTO_NAMED_COLUMNS_RE.match(line):
        return 'Rivestimento'
    m = re.match(_BONALDO_SIMPLE_HEADER_RE_TEXT, line, re.IGNORECASE)
    if m:
        # group(1) is the real trigger word specifically -- NOT
        # group(0).split()[-1], which would pick up a trailing nav-badge
        # word (e.g. "DIVANI") instead whenever one bleeds onto the line.
        return m.group(1).capitalize()
    return None
BONALDO_FOOTER_START_RE = re.compile(r'n\s*[°º]\s*per\s*box', re.IGNORECASE)
# The footer's SECOND line (packaging m3/kg + client-fabric yardage,
# e.g. "1   0,28   5,00   COM m   2,65") doesn't contain "n per box" so
# needs its own check -- without this, its trailing numeric value was
# being misread as a code-less tier-row price (confirmed via real
# output: a phantom "COL m2" row with the previous tier's leftover code).
BONALDO_FOOTER_CONT_RE = re.compile(r'\bCOM\s?m\b|\bCOL\s?m2\b|\bBONALDO\b|\bESCLUSA\b', re.IGNORECASE)


def _bonaldo_heading_candidate(line):
    """Return this line's candidate product-heading text, or None if the
    line doesn't look like one. Bonaldo prints a page's own heading with
    an "INDEX" nav-label stuck on either side (depending on which side of
    the page spread it's on) and often a "Design: <name>" credit trailing
    after it on the SAME line -- neither of Cattelan's simpler "heading
    alone on its own line" assumption holds here, confirmed via direct
    text inspection (AGEA/ALLEY/ARTIKA/MASK/MIDA)."""
    chunks = [c.strip() for c in re.split(r'\s{2,}', line.strip()) if c.strip()]
    chunks = [c for c in chunks if c.upper() != 'INDEX']
    if not chunks:
        return None
    candidate = chunks[0]
    if candidate.upper() in _BONALDO_NON_HEADING_WORDS:
        return None
    # Real headings are short product names, almost always fully-uppercase
    # (spaces allowed, e.g. "MASK GAMBE IN METALLO"; digits allowed for a
    # size suffix, e.g. "FLATIRON 180") -- excludes the numbered
    # "1. RIVESTIMENTO" / "2. GAMBE" spec-annotation labels (CARATTERISTICHE
    # TECNICHE callouts), which are also all-uppercase but ALWAYS start
    # with the digit itself, not a letter, so the required leading-letter
    # anchor below rejects them regardless of digits being allowed elsewhere.
    # A few products print a lowercase trailing descriptor after an
    # all-caps product name instead (confirmed real: "ROLL walk-in
    # closet", "DOGMA H 182 cm") -- only the FIRST word is required to be
    # uppercase for those, not the whole candidate; this doesn't open the
    # door to ordinary prose lines (which would also need this first word
    # to exactly equal or prefix-match a real catalog product name via
    # _is_known_heading below, which no random capitalized prose word does).
    first_word = candidate.split(' ', 1)[0]
    if first_word != first_word.upper() or not (2 <= len(candidate) <= 45):
        return None
    if not re.fullmatch(r"[A-ZÀ-Ý][A-Za-zÀ-ÿ0-9'&. -]*", candidate):
        return None
    return candidate


def _bonaldo_heading_matches(candidate, heading_text):
    """True if `candidate` (from _bonaldo_heading_candidate) is this
    product's own heading. Whole-word prefix match, not just equality --
    the same base product can appear under a longer heading variant on a
    later sub-block of the same page (e.g. "MASK" on one part of the
    page, "MASK GAMBE IN METALLO" -- a different leg-style sibling table
    of the SAME catalog product -- further down it)."""
    if candidate is None:
        return False
    heading_u, candidate_u = heading_text.strip().upper(), candidate.upper()
    return candidate_u == heading_u or candidate_u.startswith(heading_u + ' ')


def _bonaldo_tier_name(label_blob):
    """Strip a leading category-group label (e.g. "Tessuto /" from
    "Tessuto /    Ecopelle    800 - Ecopelle - COM", wrapped across
    physical lines and re-joined by -layout's column spacing) from the
    real tier name, keeping only the last whitespace-delimited chunk --
    the category grouping is discarded (not itself a distinguishing
    price key; the leaf tier name plus its CODICE already is), matching
    the established convention from Cattelan's fabric_tier field (leaf
    name only, no parent-category prefix)."""
    parts = re.split(r'\s{2,}', label_blob.strip())
    return parts[-1] if parts and parts[-1] else label_blob.strip()


def _bonaldo_is_badge_line(line):
    """True if `line` contains ONLY known nav/category badge word(s) (see
    _BONALDO_NON_HEADING_WORDS), e.g. a right-margin "TAVOLI"/"COMPLEMENTI"
    section badge. Confirmed via real text (Dune) that such a badge can sit
    directly between a simple-shape header and its real first sub-header
    line, which would otherwise be wrongly picked up as that sub-header
    itself (establishing a bogus current_size like "Tavoli")."""
    chunks = [c.strip() for c in re.split(r'\s{2,}', line.strip()) if c.strip()]
    return bool(chunks) and all(c.upper() in _BONALDO_NON_HEADING_WORDS for c in chunks)


def _bonaldo_subheader_group_names(line):
    """If `line` looks like a chair sub-header ("<size name> [*]  <group1>
    [  <group2>]"), return (size_name, group_names) -- else (None, [])."""
    parts = re.split(r'\s{2,}', line.strip())
    if not parts or not parts[0].strip():
        return None, []
    size_name = parts[0].rstrip('*').strip()
    # A trailing bare "X" is a leg-availability checkbox marker (seen on
    # some but not all rows in the real rendered page), not a second
    # leg-material group -- confirmed via real page images (AGEA/MIDA
    # both have it despite being single-group tables).
    group_names = [g.strip() for g in parts[1:] if g.strip() and g.strip().upper() != 'X']
    return size_name, group_names


def _bonaldo_match_subheader(line, expected_groups, base_name=None):
    """If `line` is another sub-header repeating this header block's
    ALREADY-established `expected_groups` (exact list), return the
    size/display name for this repeat -- else None.

    Matches on the TRAILING chunks (after dropping a trailing bare "X"
    marker) rather than requiring the whole line to have exactly
    len(expected_groups)+1 parts: confirmed via real text that later
    repeats under a shared header print their own name TWICE on one line
    (e.g. "Mida large    Mida large    Metallo / Plus    X", or
    "Miss Artika gambe rivestite    Miss Artika    Rivestite    X" -- note
    the left-margin label and the real display name can even differ) --
    once as a left-margin table label, once where a fresh header's first
    occurrence would put the size name. Matching from the right and
    taking the chunk immediately before the group-name suffix as the
    display name handles both the clean and duplicated-label cases
    without needing to special-case either.

    `base_name` (the outer product_name) is required to disambiguate the
    0-group case only: with no group-name suffix left to match against,
    ANY single non-blank chunk would otherwise look like "a repeat" --
    confirmed via real text (Dune) that a nav/category badge ("TAVOLI")
    and a material-category line ("Legno impiallacciato") both sit between
    the header and/or real sub-headers and would otherwise be wrongly
    treated as fresh sub-variants, corrupting `current_size` for every row
    after them. Real repeats always start with the base product name
    (e.g. "Dune", "Dune high").
    """
    parts = [p.strip() for p in re.split(r'\s{2,}', line.strip()) if p.strip()]
    if parts and parts[-1].upper() == 'X':
        parts = parts[:-1]
    n = len(expected_groups)
    # parts[-n:] would be WRONG for n == 0 (Python's -0 == 0, so it slices
    # the WHOLE list instead of an empty tail) -- matters now that 0-group
    # headers (Dune-style simple shape, no leg/material dimension at all)
    # are real, not just a defensive edge case.
    tail = parts[-n:] if n > 0 else []
    if len(parts) < n + 1 or tail != expected_groups:
        return None
    display_name = parts[-(n + 1)].rstrip('*').strip()
    if n == 0 and base_name is not None and not display_name.upper().startswith(base_name.strip().upper()):
        return None
    return display_name


def _parse_bonaldo_chair_block(lines, i, seg_end, page_of_line, product_name, brand, flags):
    """Parse one chair-shape "RIVESTIMENTO ... CODICE ... GAMBE" header
    block starting at line i (the header line itself), through ALL of its
    stacked sub-tables, returning next_i for the caller to resume from.

    Confirmed via real page text (ARTIKA/MASK/MIDA all do this): a single
    header is often shared by MULTIPLE named size/model sub-variants
    stacked vertically (e.g. "Artika" then "Miss Artika", or "Mida" /
    "Mida large" / "Miss Mida") -- each introduced by its own sub-header
    line ("<name> [*]  <group1>  [<group2>]") and footer-terminated tier
    rows, WITHOUT repeating the "RIVESTIMENTO...CODICE...GAMBE" line
    itself. So after each sub-table's footer, this checks whether the
    next content line is ANOTHER sub-header for the SAME header (its
    group names must match exactly what this header originally declared
    -- that's what distinguishes "another sub-variant of this table" from
    unrelated following content) and, if so, keeps going.
    """
    named_columns = _bonaldo_named_columns(lines[i])
    if named_columns is not None:
        # RIVESTIMENTO header with 1-3 EXPLICIT named columns and no
        # GAMBE/ASTA/SCHIENALE 2nd axis at all -- see
        # _BONALDO_RIVESTIMENTO_NAMED_COLUMNS_RE_TEXT's own doc comment
        # for the 5 real products this was verified against. Unlike the
        # classic shape, each column's CODE is declared ONCE on its own
        # dedicated "CODICE ..." line right after the header (never
        # repeated per tier row) -- every row below is price-only, so
        # last_codes is seeded from that line up front instead of from a
        # first coded row.
        j = i + 1
        while j < seg_end and (not lines[j].strip() or _bonaldo_is_badge_line(lines[j])):
            j += 1
        codice_match = _BONALDO_CODICE_LINE_RE.match(lines[j]) if j < seg_end else None
        if not codice_match:
            flags.append((page_of_line[i], product_name,
                           f"RIVESTIMENTO named-column header near line {i} has no CODICE line "
                           f"declaring a code for each column -- skipped rather than guessed"))
            return [], j
        codes = [c for c in codice_match.groups() if c is not None]
        if len(codes) != len(named_columns):
            flags.append((page_of_line[i], product_name,
                           f"RIVESTIMENTO named-column header near line {i} declares "
                           f"{len(named_columns)} column(s) but its CODICE line near line {j} has "
                           f"{len(codes)} code(s) -- skipped rather than guessing which code "
                           f"belongs to which column"))
            return [], j + 1
        current_size = product_name
        group_names = named_columns
        effective_groups = named_columns
        trigger_word = 'Rivestimento'
        last_codes = codes
        k = j + 1
    else:
        j = i + 1
        while j < seg_end and (not lines[j].strip() or _bonaldo_is_badge_line(lines[j])):
            j += 1
        if j >= seg_end:
            flags.append((page_of_line[i], product_name,
                           f"chair-shape header near line {i} has no sub-header line "
                           f"(size/leg-group names) before end of block -- skipped"))
            return [], j

        current_size, group_names = _bonaldo_subheader_group_names(lines[j])
        if current_size is None:
            flags.append((page_of_line[i], product_name,
                           f"chair-shape sub-header near line {j} ('{lines[j].strip()}') has no "
                           f"size/model name -- skipped rather than guessed"))
            return [], j + 1
        # The REPEAT-detection path below (_bonaldo_match_subheader) already
        # guards against a material-category line ("Legno impiallacciato",
        # "Cuoio") being wrongly treated as a fresh sub-variant name, by
        # requiring it to start with product_name -- but this INITIAL search
        # had no equivalent guard, so a block whose first real content line
        # (after the header) is a material category instead of a genuine
        # repeat-name line got that category's own name as current_size.
        # Confirmed real: Scriba's RIPIANO block (p.191) goes straight from
        # its header line to a bare "Cuoio" category line with no intervening
        # "Scriba"/"Scriba ripiano" repeat line at all, so every row's `size`
        # came out "Cuoio" instead of the real product name -- values (code/
        # price) were still correct, only this display label was wrong. Falls
        # back to the header line's OWN leading name text (guaranteed
        # product-related, since it's part of what matched this header in the
        # first place) whenever the lookahead candidate doesn't start with
        # product_name. Found 2026-08-07 while individually verifying RIPIANO.
        if not current_size.upper().startswith(product_name.strip().upper()):
            header_name = re.split(r'\s{2,}', lines[i].strip())[0].strip()
            if header_name.upper().startswith(product_name.strip().upper()):
                current_size = header_name
        # Zero groups is a real, verified shape (Dune/Obel/Mistral/Camillo-style
        # "simple list" products -- a single finish list with no leg/material
        # dimension at all, unlike chairs' RIVESTIMENTO x GAMBE combinations).
        # Substitute one implicit unnamed group so the row-count comparisons
        # and zip() below work the same way for 0 and 1+ declared groups,
        # without a separate code path -- model_variant just comes out None.
        effective_groups = group_names if group_names else [None]
        trigger_word = _bonaldo_header_trigger_word(lines[i]) or 'Rivestimento'
        # Tracks the most recently seen CODICE per group/column position, for
        # rows that omit a repeated code (see BONALDO_TIER_ROW_NOCODE_RE) --
        # reset whenever a fresh sub-variant starts, since codes are specific
        # to that sub-variant's own sequence (e.g. Mask's DU92/DU96 vs Miss
        # Mask's DU94/DU98 are unrelated).
        last_codes = [None] * len(effective_groups)
        k = j

    rows = []
    unrecognized = 0
    while k < seg_end:
        line = lines[k]
        repeat_size = _bonaldo_match_subheader(line, group_names, product_name)
        if repeat_size is not None:
            # a fresh sub-variant reusing this same header's groups
            current_size = repeat_size
            last_codes = [None] * len(effective_groups)
            k += 1
            continue
        if BONALDO_FOOTER_START_RE.search(line) or BONALDO_FOOTER_CONT_RE.search(line):
            # packaging-info footer (n per box/m3/kg, then a COM/COL
            # yardage line) -- not real data. Just skip it: whatever
            # comes after (another sub-header, blank/diagram filler
            # lines, or a genuinely new header) is handled by this same
            # loop's own checks on its next iteration, however many
            # blank/decorative lines separate them -- no need to look
            # ahead here.
            k += 1
            continue
        if BONALDO_CHAIR_HEADER_RE.search(line) or _BONALDO_OTHER_SECTION_RE.match(line):
            # A genuinely NEW header -- either a recognized shape (stop so
            # the caller parses it as its own block) or a confirmed OTHER
            # section word (stop so it's silently skipped rather than
            # wrongly absorbed into this block -- see
            # _BONALDO_OTHER_SECTION_WORDS).
            break
        m = BONALDO_TIER_ROW_RE.match(line.rstrip())
        if m:
            label_blob, code1, price1, code2, price2 = m.groups()
            tier_name = _bonaldo_tier_name(label_blob)
            codes_prices = [(code1, price1)]
            if code2 and price2:
                codes_prices.append((code2, price2))
            if len(codes_prices) != len(effective_groups):
                flags.append((page_of_line[k], product_name,
                               f"chair-shape row near line {k} has {len(codes_prices)} "
                               f"code/price pair(s) but the sub-header declared "
                               f"{len(effective_groups)} leg-material group(s) -- skipped rather "
                               f"than guessing which group a price belongs to"))
                unrecognized += 1
                k += 1
                continue
            for idx, (code, price) in enumerate(codes_prices):
                last_codes[idx] = code
            for (code, price), group in zip(codes_prices, effective_groups):
                rows.append({
                    "brand": brand,
                    "product_name": product_name,
                    "model_variant": group,
                    "variant_context": None,
                    "size": current_size,
                    "fabric_tier": tier_name,
                    "tier_label": trigger_word,
                    "code": code,
                    "price_eur": price,
                    "source_pdf_page": page_of_line[k],
                })
            k += 1
            continue
        m2 = BONALDO_TIER_ROW_NOCODE_RE.match(line.rstrip())
        if m2:
            label_blob, price1, price2 = m2.groups()
            tier_name = _bonaldo_tier_name(label_blob)
            prices = [price1] + ([price2] if price2 else [])
            # A trailing bare marker digit -- confirmed real: Amour/Ellison's
            # single-column RIVESTIMENTO rows print a trailing "1" after
            # the real price (likely a footnote/count reference, not a
            # second price), which this regex's own 2nd-price group
            # otherwise swallows whenever there's only supposed to be ONE
            # real price on the row. Trim a trailing implausible (<100)
            # value whenever there are more captured prices than this
            # row's real column count -- never fires for a genuine 2-column
            # row (Bull/Colibrì soft/Nikos), where both captured values
            # are real prices and len(prices) already equals
            # len(effective_groups).
            while len(prices) > len(effective_groups) and prices and not (
                re.fullmatch(r'[\d.]+', prices[-1]) and int(prices[-1].replace('.', '')) >= 100
            ):
                prices.pop()
            # Plausibility guard: without a CODE token to anchor on (the
            # coded-row path's strongest signal), this regex can also
            # match stray page-number/nav-label lines that happen to have
            # a trailing number (confirmed via real output: bare page
            # footers like "42", and "A/Z" + a stray "1") -- every real
            # chair price seen in this catalog is >= 700, so reject
            # anything implausibly small instead of trusting the shape
            # match alone.
            price_shape_ok = all(
                re.fullmatch(r'[\d.]+', p) and int(p.replace('.', '')) >= 100 for p in prices
            )
            plausible = tier_name and price_shape_ok and any(c is not None for c in last_codes)
            if plausible and len(prices) == len(effective_groups) and all(c is not None for c in last_codes):
                for code, price, group in zip(last_codes, prices, effective_groups):
                    rows.append({
                        "brand": brand,
                        "product_name": product_name,
                        "model_variant": group,
                        "variant_context": None,
                        "size": current_size,
                        "fabric_tier": tier_name,
                        "tier_label": trigger_word,
                        "code": code,
                        "price_eur": price,
                        "source_pdf_page": page_of_line[k],
                    })
            elif not tier_name and price_shape_ok:
                # A bare price with NO label at all (confirmed via real
                # text: Mistral's "Bronzo" row -- a long group-name label
                # on the row above pushed its own price onto its own,
                # otherwise-empty line during -layout reconstruction).
                # Silently doing nothing here would drop a real price with
                # zero trace, the exact failure mode this whole mechanism
                # exists to avoid -- flag it instead.
                flags.append((page_of_line[k], product_name,
                               f"chair-shape row near line {k} has a price ({', '.join(prices)}) "
                               f"with no recognizable finish-name label (likely a line-wrap "
                               f"artifact) -- skipped rather than guessed"))
                unrecognized += 1
        k += 1

    if unrecognized:
        flags.append((page_of_line[i], product_name,
                       f"{unrecognized} row(s) in the chair-shape block near line {i} "
                       f"could not be confidently matched to a leg-material group"))
    return rows, k


# Bonaldo table-shape price tables (confirmed via real page images:
# Flatiron table, Dune, Circus, Big Table). Structurally closer to
# Cattelan's size-grid than to Bonaldo's own chair shape: a "LxP  <size1>
# <size2> ..." header (analogous to Cattelan's "MISURA CM ... EUR"), then
# one or more material-CATEGORY sub-blocks (e.g. "Legno massello",
# "Ceramica opaca") sharing that header, each with finish-name rows
# (CODICE+price per size column). Every price still has its own CODICE
# (unlike Cattelan), but confirmed on real data (BIG TABLE) that a
# finish can have prices for only SOME size columns (e.g. a finish only
# offered in the two largest sizes) -- position-based column matching
# (the same technique proven for Cattelan's grids) is required, not
# just counting code/price pairs the way the chair shape could mostly
# get away with.
BONALDO_TABLE_HEADER_RE = re.compile(r'\bLxP\b')
_BONALDO_SIZE_RE = re.compile(r'Ø?\s*\d+(?:\s*x\s*\d+)?\s*cm')
# NOT anchored to line-start: confirmed real case (CIRCUS) where
# "SUPPLEMENTO" shares one physical line with the tail end of an
# unrelated wrapped footnote ("* per verificare la ... SUPPLEMENTO"),
# positioned far to the right rather than at the line's own start.
_BONALDO_SUPPLEMENTO_RE = re.compile(r'\bSUPPLEMENTO\b', re.IGNORECASE)
# Dimension/weight info lines under a category (thickness, m3, kg, load
# capacity) -- not real price data, and NOT a category-name candidate
# either.
_BONALDO_DIM_INFO_RE = re.compile(
    r'^\s*(H\s*=|sp\.\s*\d|Peso\b|Superficie\b|Portata\s*massima\b|PIANO\s+\d)', re.IGNORECASE
)
# Same markers, but NOT anchored to line-start -- some category names
# share one physical line with their own dimension info (confirmed real
# text: "Legno impiallacciato       sp. 50 mm - ... kg 121,00"). Used to
# strip the dim-info tail off a category-name candidate, not to decide
# whether to skip the line entirely (that's still _BONALDO_DIM_INFO_RE).
_BONALDO_DIM_INFO_ANYWHERE_RE = re.compile(
    r'\b(H\s*=|sp\.\s*\d|Peso\b|Superficie\b|Portata\s*massima\b)', re.IGNORECASE
)


# SUPPLEMENTO codes run longer than regular cell codes (confirmed real
# examples: SPFLAT1, SPFLAT2, SPCIRCUS4, SPCIRCUS1, vs. cell codes like
# P275/DY85/TC7Q which are always 3-5 chars) -- a dedicated, wider
# pattern used ONLY in this single-code, single-line, low-risk context,
# so the main per-cell CODE pattern's tighter length stays unchanged.
# Also allows a leading DIGIT (confirmed real example: CIRCUS's
# "Cavalletta singola" supplement uses code "99Ø3"), unlike the main
# CODE pattern which is always letter-first.
_BONALDO_SUPPLEMENT_CODE = r'[A-Z0-9][A-Z0-9Ø]{2,10}'
_BONALDO_SUPPLEMENT_ROW_RE = re.compile(
    rf'^(.*?)\s{{2,}}({_BONALDO_SUPPLEMENT_CODE})\s+(\+?\s*[\d.,]+)\s*$'
)


def _bonaldo_table_row_pairs(line):
    """Extract (position, code, price) triples from a table data row.
    A cell showing "*" instead of a price (confirmed on CIRCUS: "per
    verificare la disponibilità del prodotto... si prega di contattare
    l'Azienda") simply produces no pair for that code -- correctly
    treated the same as a genuinely blank/unavailable cell, not an error,
    since the source itself is declining to state a fixed price.

    Plausibility floor (>=100, same threshold already proven for
    chair-shape/sofa-shape) on the price side -- confirmed real
    corruption without it: a "Combinazione N" / "Opzione N" section
    label (e.g. "COMBINAZIONE 1", "OPZIONE 2") has "ZIONE" as its own
    trailing 5 characters, which happens to be CODE-shaped, immediately
    followed by the section NUMBER -- read as a fake code+price pair
    ("ZIONE" / "1") on Mellow ST/Pivot ST/Torii ST/Padiglioni. No real
    table price in this catalog is a bare 1-2 digit number."""
    pairs = []
    for m in re.finditer(rf'({_BONALDO_CODE})\s+(\+?\s*[\d.,]+)', line):
        price_num = m.group(2).replace('+', '').replace('.', '').replace(',', '').strip()
        if price_num.isdigit() and int(price_num) < 100:
            continue
        pairs.append((m.start(1), m.group(1), m.group(2)))
    return pairs


def _bonaldo_match_pairs_to_columns(pairs, col_positions):
    """Greedy nearest-column matching (same principle as Cattelan's EUR-
    column matching): each pair claims the nearest not-yet-claimed
    column. Verified against real partial-coverage rows (BIG TABLE's
    "Legno con bordi naturali", offered only in the 2 largest of 4
    sizes) -- a row's 2 pairs, positioned under columns 3-4, correctly
    match columns 3-4 and leave 1-2 unfilled, rather than left-packing
    into 1-2."""
    used: set[int] = set()
    result: dict[int, tuple[str, str]] = {}
    for pos, code, price in pairs:
        best_col, best_dist = None, None
        for col, col_pos in enumerate(col_positions):
            if col in used:
                continue
            dist = abs(pos - col_pos)
            if best_dist is None or dist < best_dist:
                best_col, best_dist = col, dist
        if best_col is not None:
            used.add(best_col)
            result[best_col] = (code, price)
    return result


def _bonaldo_table_category_lookback(lines, i, seg_start):
    """Some table sub-tables (confirmed on Flatiron table's Legno-Laccato/
    Legno-Marmo/Vetro-Legno/Ceramica-Legno sections) state their category
    name BEFORE the "LxP" header entirely -- inside the CARATTERISTICHE
    TECNICHE spec panel, e.g. "MONO MATERIAL - LEGNO ... CARATTERISTICHE
    TECNICHE" on one line -- rather than after it in the data area the
    way CIRCUS/Big Table's shared-header multi-category tables do. These
    upfront category names are reliably fully UPPERCASE (unlike the
    mixed-case row-level category words used by the shared-header
    pattern, e.g. "Legno massello"), which is what distinguishes them
    from unrelated preceding text. Scans upward a bounded distance, same
    tolerance for blank runs and body-prose exclusion proven for
    Cattelan's analogous upward label search."""
    k = i - 1
    steps = 0
    while k >= seg_start and steps < 20:
        line = lines[k]
        stripped = line.strip()
        if not stripped:
            # Unlike Cattelan's analogous upward label search (which
            # bounds on a blank-run count, tolerating up to 2), this
            # catalog's decorative diagram spacing produces much wider
            # blank gaps -- confirmed runs of 4 and 9 consecutive blank
            # lines between Flatiron table's "LxP" header and its
            # preceding CARATTERISTICHE TECNICHE text on the SAME page.
            # The `steps` counter (real content lines only, checked
            # below) is the meaningful bound here instead.
            k -= 1
            continue
        steps += 1
        first_pos = len(line) - len(line.lstrip())
        # Judge prose-vs-candidate on the FIRST 2+-space-delimited chunk,
        # not the whole line -- a numbered callout marker ("2") followed
        # (after a wide gap) by an unrelated long description on the
        # SAME line would otherwise look identical to real body prose by
        # raw line length alone (confirmed real case: "2....1. PIANO E
        # BASE....Legno impiallacciato: rovere spazzolato grey", ~190
        # chars total, but the marker itself is one character).
        candidate = re.split(r'\s{2,}CARATTERISTICHE\b', stripped, flags=re.IGNORECASE)[0].strip()
        first_chunk = re.split(r'\s{2,}', stripped)[0].strip()
        if first_pos < 15 and len(first_chunk) > 50 and "CARATTERISTICHE" not in first_chunk.upper():
            return None  # body prose -- gone too far back
        if candidate and candidate == candidate.upper() and 3 <= len(candidate) <= 60 \
                and not candidate.upper().startswith("DESIGN") \
                and candidate.upper() not in _BONALDO_NON_HEADING_WORDS:
            return candidate
        k -= 1
    return None


def _parse_bonaldo_table_block(lines, i, seg_start, seg_end, page_of_line, product_name, brand, flags):
    """Parse one "LxP ... size1 ... size2 ..." header block starting at
    line i (the header line itself), through all of its stacked
    material-category sub-blocks and any trailing SUPPLEMENTO section,
    returning next_i for the caller to resume from (at a genuinely new
    "LxP" header, a different table/chair shape, or end of block)."""
    size_matches = list(_BONALDO_SIZE_RE.finditer(lines[i]))
    if not size_matches:
        flags.append((page_of_line[i], product_name,
                       f"table-shape header near line {i} has 'LxP' but no size "
                       f"column(s) recognized -- skipped rather than guessed"))
        return [], i + 1
    col_sizes = [m.group().strip() for m in size_matches]
    col_positions = [m.start() for m in size_matches]

    # Calibrate column positions from the first FULL-coverage data row in
    # this block, if one exists, instead of trusting the header's own
    # label positions -- confirmed real bug: Big Table's header labels
    # and its data rows' actual price positions are offset by ~35-40
    # characters (more than half the ~42-char gap between columns), so a
    # PARTIAL row (fewer pairs than columns) snapped to the wrong
    # neighboring column using raw label positions, even though a full
    # row's 4 pairs individually still landed nearest their own correct
    # column (each one's own offset error stayed under half a column
    # width when ALL 4 needed to fit in order). A full row's own price
    # positions are exact ground truth for where each size's prices
    # actually render, sidestepping the header-vs-data offset entirely.
    for scan_k in range(i + 1, min(seg_end, i + 60)):
        scan_line = lines[scan_k]
        if BONALDO_TABLE_HEADER_RE.search(scan_line) or BONALDO_CHAIR_HEADER_RE.search(scan_line):
            break
        scan_pairs = _bonaldo_table_row_pairs(scan_line)
        if len(scan_pairs) == len(col_positions):
            col_positions = [pos for pos, _, _ in scan_pairs]
            break

    rows = []
    current_category = _bonaldo_table_category_lookback(lines, i, seg_start)
    in_supplemento = False
    unrecognized = 0
    k = i + 1
    while k < seg_end:
        line = lines[k]
        if not line.strip():
            k += 1
            continue
        if BONALDO_TABLE_HEADER_RE.search(line):
            break  # a genuinely new header -- caller parses it fresh
        if BONALDO_CHAIR_HEADER_RE.search(line):
            break  # a different shape's table starting -- not ours
        if _BONALDO_SUPPLEMENTO_RE.search(line):
            in_supplemento = True
            k += 1
            continue
        if _BONALDO_DIM_INFO_RE.search(line):
            k += 1
            continue
        if BONALDO_FOOTER_CONT_RE.search(line):
            # Page-footer text ("BONALDO Listino Prezzi... IVA ESCLUSA")
            # -- confirmed real corruption without this: "ESCLUSA" ends in
            # "CLUSA" (CODE-shaped), immediately followed (after enough
            # whitespace) by that page's own PAGE NUMBER, misread as a
            # code+price pair (Oshi/Geometric Table Wood/Geometric Console
            # Wood/Padiglioni). The >=100 price floor alone doesn't catch
            # this for pages >= 100, since a 3-digit page number looks
            # exactly like a plausible price.
            k += 1
            continue
        if in_supplemento:
            m = _BONALDO_SUPPLEMENT_ROW_RE.match(line.rstrip())
            if m:
                label, code, price = m.groups()
                # Unrelated text (a sidebar nav word, or -- confirmed real
                # case on CIRCUS -- a wrapped footnote's continuation
                # line, "per ricevere un preventivo,") can land on the
                # exact same visual row as a real supplement line purely
                # by vertical-position coincidence. Same fix as
                # _bonaldo_tier_name: keep only the LAST 2+-space-
                # delimited chunk, discarding whatever unrelated text
                # precedes it -- the real label is always immediately
                # adjacent to its own code, never separated from it by
                # another such gap.
                label = _bonaldo_tier_name(label)
                if label.strip():
                    rows.append({
                        "brand": brand,
                        "product_name": product_name,
                        "model_variant": None,
                        "variant_context": None,
                        "size": None,
                        "fabric_tier": label.strip(),
                        "tier_label": "Supplemento",
                        "code": code,
                        "price_eur": price.replace("+", "").strip(),
                        "source_pdf_page": page_of_line[k],
                    })
            k += 1
            continue
        pairs = _bonaldo_table_row_pairs(line)
        if pairs:
            matched = _bonaldo_match_pairs_to_columns(pairs, col_positions)
            label_end = min(p for p, _, _ in pairs)
            finish_name = line[:label_end].strip()
            if matched and finish_name:
                for col, (code, price) in matched.items():
                    rows.append({
                        "brand": brand,
                        "product_name": product_name,
                        "model_variant": current_category,
                        "variant_context": None,
                        "size": col_sizes[col],
                        "fabric_tier": finish_name,
                        "tier_label": "Piano",
                        "code": code,
                        "price_eur": price,
                        "source_pdf_page": page_of_line[k],
                    })
            else:
                unrecognized += 1
        else:
            # No code/price on this line -- a candidate new category name
            # (e.g. "Legno massello", "Ceramica opaca"), UNLESS it looks
            # like body prose (same heuristic proven for Cattelan: real
            # category labels sit indented and are short; paragraph text
            # starts near the left margin and runs long), a sidebar nav
            # word bleeding in from the page margin (confirmed real
            # output: "COMPLEMENTI" got captured as a category), or a
            # dimension-info line sharing the SAME physical line as its
            # category name (confirmed on Flatiron table: "Legno
            # impiallacciato       sp. 50 mm - ... kg 121,00" -- the
            # dim-info regex only matches when anchored at line start,
            # so it doesn't already exclude this combined case; strip
            # the dim-info portion first and keep just the name before it).
            dim_m = _BONALDO_DIM_INFO_ANYWHERE_RE.search(line)
            text = (line[:dim_m.start()] if dim_m else line).strip()
            first_pos = len(line) - len(line.lstrip())
            is_prose = first_pos < 15 and len(text) > 50
            # A wrapped footnote fragment (confirmed real case on CIRCUS:
            # the "* per verificare la disponibilità..." contact-us
            # disclaimer for a "*"-marked unavailable price wraps onto
            # its own physical line, "disponibilità del prodotto e") can
            # slip past the prose-length check when short enough, but
            # unlike every real category name in this catalog ("Legno
            # massello", "Marmo lucido o opaco") it starts with a
            # lowercase letter -- a real category name always starts
            # uppercase, even when it contains lowercase connector words
            # in the middle.
            starts_upper = text[:1].isupper()
            if text and not is_prose and starts_upper and text.upper() not in _BONALDO_NON_HEADING_WORDS:
                current_category = text
        k += 1

    if unrecognized:
        flags.append((page_of_line[i], product_name,
                       f"{unrecognized} row(s) in the table-shape block near line {i} had "
                       f"a code/price pair that could not be matched to a size column"))
    return rows, k


# Bonaldo modular-sofa price tables (confirmed via real page images:
# BONAMOUR, Superhiro). A hybrid of the other two shapes: like table-shape,
# a header line lists multiple SIZE columns; like chair-shape, each column
# has ONE code (not one per cell) -- but instead of chair-shape's
# fabric-tier-name rows, prices follow a small FIXED, closed tier ladder
# (800-COM/900/Class/Must/Special, sometimes extended with Capri/Procida/
# Panarea/Ponza Nabuk-Anilina for pricier lines), one price per column per
# tier -- structurally closest to Cattelan's fixed-tier grid, just with an
# explicit CODICE row instead of Cattelan's code-less cells. Each element
# type (Centrale, Penisola dx/sx, Chaise longue, Angolo dx/sx, Pouf...) is
# its own repeating header block, same nesting idea as chair-shape's
# stacked sub-variants.
BONALDO_SOFA_HEADER_RE = re.compile(r'\bRIVESTIMENTO\b')
# Fixed, closed set -- confirmed via real text (Bonamour, Superhiro) that
# every tier-price row starts with exactly one of these labels, in this
# order. NOT free-form finish names the way chair-shape's rows are.
_BONALDO_SOFA_TIERS = (
    # 3 forms confirmed real: modular sofas print the bare "800 - COM"
    # (Bonamour, Superhiro); the "MISURA RETE" bed-frame family (Cuff,
    # Holden, Basket p.503/43/489) prints the longer "800 - Ecopelle -
    # COM" instead -- found 2026-08-07 as the reason Cuff's own FIRST
    # tier row (its cheapest, most common one) was silently dropped
    # entirely: the fixed-string match required an exact "800 - COM"
    # prefix, which this family's real text never contains. Owen ego
    # (p.527, same MISURA RETE family) prints a bare "800" with NO
    # suffix at all -- found the same day verifying the model-variant
    # fix, same silent-drop failure mode (its cheapest tier missing,
    # 16 rows instead of the real 20, no flag at all since the other 4
    # tiers parsed fine and nothing LOOKED wrong).
    '800 - Ecopelle - COM', '800 - COM', '800', '900', 'Class', 'Must', 'Special',
    'Capri', 'Procida', 'Panarea',
    # Both forms confirmed real: "Ponza Nabuk/Anilina" (Superhiro),
    # bare "Ponza Nabuk" with no suffix at all (Seki).
    'Ponza Nabuk/Anilina', 'Ponza Nabuk',
)
_BONALDO_SOFA_TIER_RE = re.compile(
    # Longer variants MUST come before their own shorter prefix (e.g.
    # "Ponza Nabuk/Anilina" before "Ponza Nabuk") -- confirmed real that
    # both bare "Ponza Nabuk" (Seki) and the "/Anilina"-suffixed form
    # (Superhiro) exist; alternation stops at the first match, so listing
    # the shorter one first would truncate the longer real label.
    r'^\s*(' + '|'.join(re.escape(t) for t in sorted(_BONALDO_SOFA_TIERS, key=len, reverse=True)) + r')\b\s*(.*)$'
)
# A dimension-diagram callout can share a physical line with a real tier
# row purely by vertical-position coincidence, pushing the tier label off
# the line-start anchor (confirmed real: Seki's "Class" row). Stripped
# before tier-matching, same principle as _BONALDO_DIM_INFO_ANYWHERE_RE
# for table-shape. Four real shapes confirmed so far, all handled by one
# pattern (a CHAIN of "NN - " before "cm", and a trailing "- NN\"" after
# it, both independently optional around the required "NN cm"):
#   - "62 cm - 24\""                    (Seki's original motivating case)
#   - "220 cm"                          (Cuff's Panarea row, p.503 -- no
#                                         trailing inches at all)
#   - "128 - 158 cm"                    (Cuff's Must row, p.503 -- a
#                                         width RANGE, dash BEFORE "cm"
#                                         instead of after)
#   - "194 - 200 - 220 - 234 cm"        (Holden's Must row, p.49 -- a
#                                         chain of FOUR numbers, not just
#                                         one pair, before "cm")
# All found 2026-08-07. All four previously left this prefix un-stripped,
# silently dropping the entire row with NO flag raised at all (worse than
# the usual "skipped rather than guessed" flag -- these just vanished).
_BONALDO_SOFA_DIM_PREFIX_RE = re.compile(
    r'^\s*(?:(?:\d+\s*-\s*)*\d+\s*cm\s*(?:-\s*\d+["”]?)?\s*)+'
)
# A trailing " dx"/" sx" mirror-image suffix on an otherwise plain size
# label (confirmed real: "230 x 100 sx", "144 x 96 dx") -- stripped when
# matching a repeated size label back to its own bare column header.
_BONALDO_DXSX_SUFFIX_RE = re.compile(r'\s+(dx|sx)\s*$', re.IGNORECASE)
# A lone price token, no code -- used only for the Special-row stitching
# look-ahead, where the label and its own prices have been split onto two
# separate physical lines by an interleaved nav-sidebar word (confirmed
# real: "A/Z", "SEDIE" landing at that exact vertical position on both
# Bonamour and Superhiro -- a consistent, position-driven artifact, not
# randomness).
_BONALDO_BARE_PRICE_ROW_RE = re.compile(r'^\s*((?:[\d.,]+\s+)*[\d.,]+)\s*$')
# A sofa size column: a bare number ("278"), "NxM" ("144 x 96"), or
# "N x H M" ("144 x H 63", Schienale/backrest-style elements). Matched
# directly via finditer rather than generic 2+-space chunk splitting --
# confirmed real bug: some headers' adjacent size columns are separated by
# only a SINGLE space (Bonamour's Schienale: "144 x H 63 140 x H 63"),
# which the generic chunk splitter (built for names/codes, which don't
# have this problem) wrongly merges into one label.
_BONALDO_SOFA_SIZE_RE = re.compile(
    # Bounded on both sides so a digit EMBEDDED inside an adjacent
    # alphanumeric code (e.g. the "4" inside "L4WQ") can never match --
    # confirmed real bug found 2026-08-07: without this, a RIVESTIMENTO
    # line whose real size columns are on a DIFFERENT line (see
    # _bonaldo_sofa_size_lookback) and which instead has inline CODICE
    # codes directly after it (Cuff/Holden/Basket/James/Oris) got a
    # phantom single-column "size" scraped from inside the first code's
    # own digit, silently breaking column-count alignment for everything
    # downstream. Deliberately NOT just a leading \b (word boundary),
    # since \d and a letter are both \w -- no boundary exists between "L"
    # and "4" in "L4WQ" for \b to catch.
    r'(?<!\S)\d+(?:\s*x\s*H?\s*\d+)?(?!\S)'
)
# A code-shaped chunk (the same _BONALDO_CODE pattern used everywhere
# else) appearing directly after RIVESTIMENTO instead of any genuine size
# -- the header line IS the code row itself, with no separate "CODICE"
# line anywhere (confirmed real: Cuff p.503, Holden/Basket/James/Oris
# p.43-513, all "MISURA RETE" bed-frame products). Matched against whole
# chunks (not raw substring search like the size regex) since codes are
# always cleanly space-delimited here.
_BONALDO_SOFA_INLINE_CODE_RE = re.compile(_BONALDO_CODE)
# A genuine printed size label used ONLY by _bonaldo_sofa_size_lookback,
# one physical line above a RIVESTIMENTO-with-inline-codes header (see
# above) -- confirmed real in BOTH forms on the same page for Cuff/Holden:
# metric ("90x200cm", "154 x 205 cm") and imperial ("35” x 79”",
# using U+201D RIGHT DOUBLE QUOTATION MARK as the inch mark, confirmed via
# direct byte inspection of the extracted text -- not the ASCII \" alone).
_BONALDO_SOFA_SIZE_LABEL_METRIC_RE = re.compile(r'\d+\s*x\s*\d+\s*cm', re.IGNORECASE)
_BONALDO_SOFA_SIZE_LABEL_IMPERIAL_RE = re.compile(r'\d+[”"]\s*x\s*\d+[”"]')


def _bonaldo_chunks_with_positions(text):
    """Split `text` into whitespace-delimited chunks, allowing single
    internal spaces (so "Chaise longue dx/sx" or "144 x 96" stay one
    chunk, only breaking on a DOUBLE space or more), each paired with its
    starting character position -- needed to align a header's element
    names with its own size columns, which don't sit in a simple
    left-to-right index correspondence (see _bonaldo_sofa_column_owners)."""
    return [(m.start(), m.group()) for m in re.finditer(r'\S(?:[^\s]|\s(?!\s))*', text)]


def _bonaldo_sofa_header_names_sizes(line):
    """Parse a sofa "<name(s)>  RIVESTIMENTO  <size1>  <size2> ..." header
    line into (names_with_pos, sizes_with_pos, inline_codes) -- the first
    two are lists of (character_position, text), the third a bool. Returns
    (None, None, False) if `line` isn't a real sofa header.

    When no genuine size columns follow RIVESTIMENTO on this same line,
    also checks for CODE-shaped chunks there instead (confirmed real:
    Cuff/Holden/Basket/James/Oris -- this header line IS the code row
    itself, with the real size labels on a line ABOVE it instead; see
    _bonaldo_sofa_size_lookback). When that's what's found, `sizes`
    actually holds the (position, code) pairs and `inline_codes` is True
    -- the caller is responsible for treating them as codes, not sizes."""
    m = BONALDO_SOFA_HEADER_RE.search(line)
    if not m:
        return None, None, False
    names = [(pos, txt.strip()) for pos, txt in _bonaldo_chunks_with_positions(line[:m.start()]) if txt.strip()]
    # A bare digit/digit-with-period chunk before RIVESTIMENTO is never a
    # real element name -- it's diagram-annotation noise landing on the
    # header's vertical position by coincidence: a numbered CARATTERISTICHE
    # TECNICHE spec callout ("1. RIVESTIMENTO   Tessuto", confirmed real on
    # Bonamour/Alley/Seki) or a bare dimension-diagram digit (Seki's "70"
    # before a REAL header, "70    RIVESTIMENTO    SEKI 70   SEKI 98").
    # Filtered out rather than rejecting the whole line outright -- the
    # SIZE check below (which requires genuine digit-shaped size columns
    # after RIVESTIMENTO, absent from the callout's prose "Tessuto,
    # Ecopelle, Pelle") is what actually distinguishes a real header from
    # a callout; a real header can legitimately have NO element name at
    # all once the noise is stripped (confirmed real: Seki has no true
    # name here, just its own two size-labeled columns).
    names = [(pos, txt) for pos, txt in names if not re.match(r'^\d+\.?$', txt)]
    sizes = [(mm.start() + m.end(), mm.group()) for mm in _BONALDO_SOFA_SIZE_RE.finditer(line[m.end():])]
    if sizes:
        return names, sizes, False
    code_chunks = [(pos, txt) for pos, txt in _bonaldo_chunks_with_positions(line[m.end():])
                   if re.fullmatch(_BONALDO_SOFA_INLINE_CODE_RE, txt.strip())]
    if code_chunks:
        return names, [(pos + m.end(), txt) for pos, txt in code_chunks], True
    return names, [], False


def _bonaldo_sofa_column_owners(names, sizes, lookahead_lines):
    """Return a list, parallel to `sizes`, of which element name (from
    `names`) owns each data column. Trivial or when there's only one name
    (the overwhelming majority of real blocks: Bonamour's "Penisola dx/sx"
    is ONE name with 2 size columns, not 2 names).

    For the rarer case of multiple DIFFERENT element names sharing one
    header row (confirmed real: Superhiro's "Meridiana dx/sx" + "Chaise
    longue dx/sx", each owning its own single column), the header's own
    name and size positions do NOT align directly -- names sit far to the
    left of RIVESTIMENTO, sizes far to its right, so nearest-position
    matching between them directly would always pick whichever name is
    rightmost. Confirmed via real text instead: a "repeat" line between
    the header and the CODICE line re-states each column's size (with a
    dx/sx suffix) positioned DIRECTLY under that column's OWNING name, not
    under the size column itself. Bridge the two: match each repeat-line
    chunk to a data column by TEXT (after stripping the dx/sx suffix),
    then to a name by POSITION. Returns None if no such bridge line is
    found (caller flags rather than guesses)."""
    if len(names) <= 1:
        owner = names[0][1] if names else None
        return [owner] * len(sizes)
    bare_sizes = [s.replace(' ', '') for _, s in sizes]
    for line in lookahead_lines:
        chunks = _bonaldo_chunks_with_positions(line)
        col_pos = {}
        for pos, chunk in chunks:
            chunk = chunk.strip()
            if chunk.upper() == 'CODICE':
                continue
            bare = _BONALDO_DXSX_SUFFIX_RE.sub('', chunk).replace(' ', '')
            if bare in bare_sizes:
                col_pos[bare_sizes.index(bare)] = pos
        if len(col_pos) != len(sizes):
            continue
        # Boundary-based split, NOT "nearest name wins per column" --
        # confirmed real counterexample: Superhiro's "Pouf" (3 columns) +
        # "Cuscino" (1 column) is an UNEVEN 3-1 split, and nearest-name
        # matching mis-split it 2-2 (the 3rd Pouf column's bridged
        # position sat numerically closer to "Cuscino" than to "Pouf",
        # just from the two names' raw header spacing, with nothing to do
        # with which one actually owns that column). An exact (or
        # near-exact) position match between a name and its OWN first
        # column is a far stronger signal: every 2nd+ name's first column
        # bridges to a position within a couple characters of that name's
        # own header position (confirmed on both this case and the clean
        # 1-1 splits, e.g. "Meridiana dx/sx" / "Chaise longue dx/sx").
        # Column 0 always belongs to the first name by construction.
        TOL = 3
        boundary_name_idx = {0: 0}
        ok = True
        for name_idx in range(1, len(names)):
            name_pos = names[name_idx][0]
            best_col, best_dist = None, None
            for col in range(len(sizes)):
                dist = abs(col_pos[col] - name_pos)
                if best_dist is None or dist < best_dist:
                    best_col, best_dist = col, dist
            if best_dist is None or best_dist > TOL:
                ok = False
                break
            boundary_name_idx[best_col] = name_idx
        if not ok:
            continue
        owners = []
        current = 0
        for col in range(len(sizes)):
            if col in boundary_name_idx:
                current = boundary_name_idx[col]
            owners.append(names[current][1])
        return owners
    return None


def _bonaldo_sofa_codes_for_columns(lines, start, seg_end, col_positions):
    """Scan forward from `start` for the CODICE row and return a list,
    parallel to `col_positions`, of the code(s) in each column -- usually
    one, but two when a mirror-image dx/sx pair shares one price ladder
    (confirmed real: Bonamour's "Penisola dx/sx" prints "F417 dx" on the
    line ABOVE the CODICE-labeled line, "F418 sx" on the CODICE line
    itself, at the same column position -- both are real, separately
    coded SKUs at the shared price). Returns (codes_by_column, next_i) --
    codes_by_column[col] is a list of 1 or 2 codes."""
    k = start
    prev_line_codes = None
    while k < seg_end:
        line = lines[k]
        chunks = _bonaldo_chunks_with_positions(line)
        codice_pos = next((pos for pos, c in chunks if c.strip().upper() == 'CODICE'), None)
        if codice_pos is not None:
            # Only chunks AFTER "CODICE" are real per-column codes -- the
            # chunks BEFORE it on this same line are a repeated size-label
            # row-margin (e.g. "144 x 96   96 x 96   140 x 122   CODICE
            # F415  F416  F41Ø"), not additional codes. Confirmed real bug:
            # including them made the "code" field come out as a size
            # label and produced spurious duplicate rows.
            code_chunks = [(pos, c.strip()) for pos, c in chunks if pos > codice_pos]
            pairs = [(pos, None, c) for pos, c in code_chunks]
            matched = _bonaldo_match_pairs_to_columns(pairs, col_positions)
            codes_by_column = [[] for _ in col_positions]
            for col, (_, code) in matched.items():
                codes_by_column[col].append(code)
            if prev_line_codes:
                prev_pairs = [(pos, None, c) for pos, c in prev_line_codes]
                prev_matched = _bonaldo_match_pairs_to_columns(prev_pairs, col_positions)
                for col, (_, code) in prev_matched.items():
                    codes_by_column[col].insert(0, code)
            return codes_by_column, k + 1
        if BONALDO_SOFA_HEADER_RE.search(line) or BONALDO_CHAIR_HEADER_RE.search(line) \
                or BONALDO_TABLE_HEADER_RE.search(line):
            return None, k
        stripped = line.strip()
        if stripped:
            line_chunks = [(pos, c.strip()) for pos, c in chunks if c.strip()]
            # A plausible "dx codes" pre-row: every chunk looks like a
            # code (optionally dx/sx-suffixed), never a real code AND a
            # price on the same chunk.
            if line_chunks and all(re.fullmatch(r'[A-ZØ][A-Z0-9Ø]{1,6}(?:\s+(?:dx|sx))?', c, re.IGNORECASE)
                                    for _, c in line_chunks):
                prev_line_codes = line_chunks
            else:
                prev_line_codes = None
        k += 1
    return None, k


def _bonaldo_sofa_variant_context_lookback(lines, i, seg_start, product_name):
    """Scan upward for a preceding "<PRODUCT NAME>  <material/variant
    context>" divider line (confirmed real: Boolean prints TWO complete,
    otherwise-identical sets of "Divano"/"Terminale dx/sx" blocks -- one
    under a "BOOLEAN    BASE IN NOCE" divider, another under "BOOLEAN
    BASE IN ROVERE" -- each with its own CODICE/prices). Without this,
    the two sets would be indistinguishable duplicates by name/size/tier
    alone. Does NOT stop at other sofa headers while scanning back --
    multiple element blocks (e.g. both Divano AND Terminale) can share
    ONE divider, so the nearest one found is still the right answer even
    several blocks back. Returns None if none found within a generous
    bound (sofa blocks with extended tier ladders can run long)."""
    heading_u = product_name.strip().upper()
    k = i - 1
    scanned = 0
    while k >= seg_start and scanned < 400:
        scanned += 1
        stripped = lines[k].strip()
        if stripped:
            chunks = [c.strip() for c in re.split(r'\s{2,}', stripped) if c.strip()]
            if len(chunks) > 1 and chunks[0].upper() == heading_u:
                candidate = chunks[1]
                # Every product's OWN page-1 banner is "<NAME> ... INDEX"
                # (or "<NAME>  Design: <credit>") -- matches the same
                # "<PRODUCT NAME>  X" shape as a real divider but isn't
                # one. Confirmed real false-positive: without this,
                # products with NO real divider at all (the overwhelming
                # majority) got "INDEX" set as their variant_context once
                # the scan reached back far enough to hit that banner.
                if candidate.upper() in _BONALDO_NON_HEADING_WORDS or candidate.upper() == 'INDEX' \
                        or candidate.upper().startswith('DESIGN'):
                    k -= 1
                    continue
                return candidate
        k -= 1
    return None


def _bonaldo_sofa_size_lookback(lines, i, seg_start, n_expected):
    """Scan upward from line i (a RIVESTIMENTO-with-inline-codes header --
    see _bonaldo_sofa_header_names_sizes) for the real size-label line.
    Confirmed real (Cuff p.503, Holden p.43): this shape's genuine size
    columns print on a line ABOVE the header instead of after RIVESTIMENTO
    on the same line, with a "(e materasso consigliato)" caption and/or an
    imperial-unit duplicate row often sitting in between. Prefers a metric
    ("...cm") line when one is found within the window (matches the unit
    convention every other Bonaldo size already uses); falls back to an
    imperial ("...”") line only if no metric line with the right column
    count ever appears. Returns a list of size-label strings, left to
    right, or None if no line with exactly `n_expected` size-shaped chunks
    is found within the window -- caller flags rather than guesses."""
    imperial_fallback = None
    k = i - 1
    scanned = 0
    # Wide enough to survive the extra blank-line padding some blocks have
    # between their own metric size line and RIVESTIMENTO -- confirmed
    # real: Cuff's SECOND block ("Cuff plus", p.503) has its metric line
    # 16 physical lines above RIVESTIMENTO, vs. 8 for the first block on
    # the same page. A narrower window (15) missed it and silently fell
    # back to the imperial line instead, which is real data but not the
    # metric convention every other Bonaldo size already uses.
    while k >= seg_start and scanned < 30:
        scanned += 1
        chunks = [txt.strip() for _, txt in _bonaldo_chunks_with_positions(lines[k])]
        metric = [c for c in chunks if _BONALDO_SOFA_SIZE_LABEL_METRIC_RE.fullmatch(c)]
        if len(metric) == n_expected:
            return metric
        if imperial_fallback is None:
            imperial = [c for c in chunks if _BONALDO_SOFA_SIZE_LABEL_IMPERIAL_RE.fullmatch(c)]
            if len(imperial) == n_expected:
                imperial_fallback = imperial
        k -= 1
    return imperial_fallback


def _bonaldo_sofa_model_variant_lookback(lines, i, seg_start, product_name):
    """Scan upward from line i (a RIVESTIMENTO-with-inline-codes header),
    same window as _bonaldo_sofa_size_lookback, for a "<product_name>[
    suffix]  MISURA RETE" line and return the captured name+suffix text,
    or None if not found.

    Confirmed real (Basket p.489-490, Cuff p.502-507): this "MISURA RETE"
    bed-frame family often has SEVERAL distinctly-priced sub-models
    sharing one product page (Basket/Basket hi/Basket plus/Basket hi
    plus/Basket open/Basket hi open/Basket hi plus open -- 7 real,
    DIFFERENT prices for the same tier, confirmed via page image: Basket
    800-Ecopelle-COM=3.070, Basket hi=3.395, Basket plus=3.125, Basket hi
    plus=3.450). The distinguishing suffix ("hi"/"plus"/"open"/"alto"/
    "ego"/etc.) is printed in MIXED case sharing a line with "MISURA
    RETE" -- invisible to _bonaldo_heading_candidate (requires the first
    WORD be uppercase; "Basket hi" starts lowercase after the first
    word) and to _bonaldo_sofa_variant_context_lookback (requires an
    EXACT chunks[0] == product_name match, which "Basket hi" as ONE
    single-space-joined chunk never satisfies). Without this, every
    sub-model's rows were silently merged under the bare product_name
    with no distinguishing field at all -- confirmed a full-catalog scan
    2026-08-07 found exactly 16 products in this "MISURA RETE" family
    affected (out of 22 total that have a MISURA RETE section at all;
    the other 6 are genuinely single-variant, correctly untouched)."""
    k = i - 1
    scanned = 0
    # A leading nav-sidebar badge ("TAVOLI", same _BONALDO_NON_HEADING_WORDS
    # set used elsewhere) can bleed onto the SAME line as the label,
    # BEFORE the product name -- confirmed real: Basket plus's only
    # occurrence is "TAVOLI            Basket plus ... MISURA RETE"
    # (p.490); without tolerating this, that whole sub-model's rows fell
    # back to model_variant=None, indistinguishable from Basket's own.
    badge_prefix = r'(?:(?:' + '|'.join(re.escape(w) for w in _BONALDO_NON_HEADING_WORDS) + r')\s+)?'
    pattern = re.compile(
        r'^\s*' + badge_prefix + r'(' + re.escape(product_name) + r'[A-Za-zÀ-ÿ ]{0,25}?)\s{2,}MISURA RETE',
        re.IGNORECASE
    )
    while k >= seg_start and scanned < 30:
        scanned += 1
        m = pattern.match(lines[k])
        if m:
            return m.group(1).strip()
        k -= 1
    return None


def _bonaldo_sofa_has_nearby_codice_row(lines, i, seg_end):
    """True if a literal "CODICE" row appears within a few lines after
    line i. Used to reject a false-positive inline-codes read (see
    _bonaldo_sofa_header_names_sizes) -- confirmed real: Bodo p.445 has
    "Bodo PIEDI  RIVESTIMENTO  PIEDI  BASE GIREVOLE" (PIEDI/BASE GIREVOLE
    are ELEMENT NAMES, not codes) immediately followed by a genuine
    "CODICE  PBOF  PBOD" row -- but "PIEDI" alone happens to fit the same
    3-5-letter code shape as a real code (this catalog's codes CAN be
    pure letters, e.g. "TBMC"), so the header line alone can't tell the
    two apart. A nearby real CODICE row is the deciding signal: when one
    exists, the trailing header chunks are names for the EXISTING
    names+CODICE-row path to use, not inline codes for the new path."""
    for k in range(i + 1, min(seg_end, i + 6)):
        if any(txt.strip().upper() == 'CODICE' for _, txt in _bonaldo_chunks_with_positions(lines[k])):
            return True
    return False


def _parse_bonaldo_sofa_block(lines, i, seg_start, seg_end, page_of_line, product_name, brand, flags):
    """Parse one sofa "<name(s)>  RIVESTIMENTO  <size1> ..." header block
    starting at line i, through its CODICE row and fixed tier-price
    ladder, returning next_i for the caller to resume from."""
    names, sizes, inline_codes = _bonaldo_sofa_header_names_sizes(lines[i])
    if inline_codes and _bonaldo_sofa_has_nearby_codice_row(lines, i, seg_end):
        inline_codes = False
        sizes = []
    if not sizes:
        flags.append((page_of_line[i], product_name,
                       f"sofa-shape header near line {i} has 'RIVESTIMENTO' but no size "
                       f"column(s) recognized -- skipped rather than guessed"))
        return [], i + 1

    if inline_codes:
        # Cuff/Holden/Basket/James/Oris-style: `sizes` actually holds the
        # (position, code) pairs found directly on the RIVESTIMENTO line
        # itself -- there is no separate CODICE row anywhere for this
        # shape, and the real size labels are on a line above instead.
        col_positions = [pos for pos, _ in sizes]
        codes_by_column = [[code] for _, code in sizes]
        col_sizes = _bonaldo_sofa_size_lookback(lines, i, seg_start, len(sizes))
        if col_sizes is None:
            flags.append((page_of_line[i], product_name,
                           f"sofa-shape header near line {i} has inline codes on its "
                           f"RIVESTIMENTO line but no matching size-label line found above "
                           f"-- skipped rather than guessed"))
            return [], i + 1
        # No element-name dimension in this shape -- but a distinctly-
        # priced sub-model name (see _bonaldo_sofa_model_variant_lookback)
        # takes its place when one is found, so e.g. "Basket hi"'s own
        # rows are distinguishable from bare "Basket"'s instead of both
        # silently sharing product_name with no way to tell them apart.
        model_variant_label = _bonaldo_sofa_model_variant_lookback(lines, i, seg_start, product_name)
        owners = [model_variant_label] * len(sizes)
        variant_context = _bonaldo_sofa_variant_context_lookback(lines, i, seg_start, product_name)
        k = i + 1
    else:
        col_sizes = [s for _, s in sizes]
        col_positions = [pos for pos, _ in sizes]
        variant_context = _bonaldo_sofa_variant_context_lookback(lines, i, seg_start, product_name)

        # Wide enough to survive intervening blank/nav-sidebar lines
        # (confirmed real: Superhiro's "Pouf"/"Cuscino" repeat line sits
        # 12 lines after its own header, separated by a "POLTRONE & POUF"
        # sidebar badge).
        lookahead = lines[i + 1:min(seg_end, i + 20)]
        owners = _bonaldo_sofa_column_owners(names, sizes, lookahead)
        if owners is None:
            flags.append((page_of_line[i], product_name,
                           f"sofa-shape header near line {i} has {len(names)} element names "
                           f"sharing one header row but they could not be matched to their "
                           f"own size column(s) -- skipped rather than guessed"))
            return [], i + 1

        codes_by_column, k = _bonaldo_sofa_codes_for_columns(lines, i + 1, seg_end, col_positions)
        if codes_by_column is None:
            flags.append((page_of_line[i], product_name,
                           f"sofa-shape header near line {i} has no CODICE row found before "
                           f"the next header -- skipped rather than guessed"))
            return [], k

    rows = []
    unrecognized = 0
    prev_tier_prices = None  # per-column prices of the immediately preceding tier row
    pending_label = None     # a tier label seen with no price on its own line yet
    pending_prices = None    # a bare price row seen with no label on its own line yet
    while k < seg_end:
        line = lines[k]
        if BONALDO_SOFA_HEADER_RE.search(line) or BONALDO_CHAIR_HEADER_RE.search(line) \
                or BONALDO_TABLE_HEADER_RE.search(line):
            break
        rstripped = line.rstrip()
        dim_m = _BONALDO_SOFA_DIM_PREFIX_RE.match(rstripped)
        prefix_len = dim_m.end() if dim_m else 0
        m = _BONALDO_SOFA_TIER_RE.match(rstripped[prefix_len:])
        # Matched against the PREFIX-STRIPPED remainder, same as `m` above
        # -- confirmed real bug, found 2026-08-07: a dimension-prefixed
        # BARE price row (Cuff's "Must" row, p.503: "128 - 158 cm   220 cm
        # 3.095   3.285", no label at all on this line) previously always
        # failed here since matching against the untouched `rstripped`
        # (still containing "cm"/"-" text from the prefix) can never
        # satisfy _BONALDO_BARE_PRICE_ROW_RE's "whole string is just
        # digits" requirement -- silently dropping the row with no flag.
        bare_m = _BONALDO_BARE_PRICE_ROW_RE.match(rstripped[prefix_len:]) if not m else None
        if m:
            tier_name = m.group(1)
            # Keep price positions in the ORIGINAL line's coordinate space
            # (offset by the stripped dimension-prefix length) -- they
            # must stay comparable to col_positions, which were calibrated
            # from the header line and know nothing about this prefix.
            rest_start = prefix_len + m.end(1)
            rest = rstripped[rest_start:]
            pairs = [(mm.start() + rest_start, None, mm.group()) for mm in re.finditer(r'[\d.,]+', rest)]
            matched = _bonaldo_match_pairs_to_columns(pairs, col_positions) if pairs else {}
            if not matched:
                # Confirmed real, position-driven artifact (not
                # randomness): a nav-sidebar word landing at this exact
                # vertical position pushes the tier's own prices onto a
                # SEPARATE physical line, leaving just the bare label
                # here -- confirmed BOTH orderings happen (Superhiro/
                # Bonamour's Centrale block: label first, prices follow;
                # Bonamour's Schienale block: prices come FIRST, "Special"
                # label follows several lines later). If the matching bare
                # price row already arrived, pair with it now; otherwise
                # defer until (if) it shows up.
                if pending_prices is not None:
                    matched = pending_prices
                    pending_prices = None
                else:
                    pending_label = tier_name
                    k += 1
                    continue
        elif bare_m:
            price_tokens = re.findall(r'[\d.,]+', bare_m.group(1))
            # Offset by prefix_len for the same reason the `m` branch
            # above does -- bare_m matched against the prefix-stripped
            # remainder, but col_positions were calibrated from the
            # header line, which knows nothing about this line's prefix.
            pairs = [(mmm.start() + prefix_len, None, mmm.group()) for mmm in re.finditer(r'[\d.,]+', bare_m.group(1))]
            matched = _bonaldo_match_pairs_to_columns(pairs, col_positions) if pairs else {}
            if len(price_tokens) != len(col_positions) or not matched:
                k += 1
                continue
            if pending_label is not None:
                tier_name = pending_label
                pending_label = None
            else:
                # No label seen yet -- hold onto these prices in case the
                # label follows on a later line (see Schienale case above).
                pending_prices = matched
                k += 1
                continue
        else:
            k += 1
            continue
        if not matched or len(matched) != len(col_positions):
            unrecognized += 1
            k += 1
            continue
        # Plausibility floor before trusting a stitched (label-less)
        # price row specifically -- a genuinely malformed/unrelated line
        # must not get silently attached to a pending label. Same >=100
        # floor as chair-shape's fallback (NOT a higher sofa-specific
        # value -- confirmed real counterexample: Schienale/Pouf accessory
        # prices run as low as 795-990, well under a naive "sofas are
        # expensive" assumption). Each tier's price must also be >= the
        # previous tier's same-column price (the ladder only ever gets
        # more expensive going down).
        prices_by_col = {col: price for col, (_, price) in matched.items()}
        plausible = all(
            re.fullmatch(r'[\d.]+', p) and int(p.replace('.', '')) >= 100
            for p in prices_by_col.values()
        )
        if plausible and prev_tier_prices:
            plausible = all(
                float(prices_by_col[col].replace('.', '')) >= float(prev_tier_prices[col].replace('.', ''))
                for col in prices_by_col if col in prev_tier_prices
            )
        if not plausible:
            flags.append((page_of_line[k], product_name,
                           f"sofa-shape '{tier_name}' row near line {k} has implausible or "
                           f"out-of-order price(s) -- skipped rather than guessed"))
            unrecognized += 1
            k += 1
            continue
        prev_tier_prices = prices_by_col
        for col, price in prices_by_col.items():
            for code in codes_by_column[col]:
                rows.append({
                    "brand": brand,
                    "product_name": product_name,
                    "model_variant": owners[col],
                    "variant_context": variant_context,
                    "size": col_sizes[col],
                    "fabric_tier": tier_name,
                    "tier_label": "Rivestimento",
                    "code": code,
                    "price_eur": price,
                    "source_pdf_page": page_of_line[k],
                })
        k += 1

    if unrecognized:
        flags.append((page_of_line[i], product_name,
                       f"{unrecognized} row(s) in the sofa-shape block near line {i} could "
                       f"not be confidently matched to a size column"))
    return rows, k


def parse_file_bonaldo(path, product_name, brand, all_headings=None, heading_text=None):
    """Bonaldo price tables. Every price has its own CODICE right next to
    it (unlike Cattelan), which de-risks the price VALUE, but the row
    ambiguity class (which fabric-tier/size/group a price belongs to) is
    the same kind of risk Cattelan had -- genuinely ambiguous blocks are
    flagged for manual review rather than guessed, same as Cattelan.

    Currently handles the CHAIR and TABLE shapes (confirmed via real page
    images: AGEA/ALLEY/ARTIKA/MASK/MIDA for chairs; Flatiron table/Dune/
    Circus/Big Table for tables). The modular-sofa shape is intentionally
    NOT yet handled -- rather than silently mis-parse or silently skip
    it, every product matching neither known shape is explicitly flagged
    so it shows up in review_flags/flag_triage.json instead of quietly
    having zero (or wrong) rows.
    """
    heading_text = heading_text or product_name
    all_headings = set(all_headings) if all_headings else {heading_text}
    with open(path, encoding='utf-8') as f:
        lines = f.read().split('\n')

    page_of_line = [None] * len(lines)
    current_page = None
    for idx, ln in enumerate(lines):
        m = re.match(r'^<<<PDFPAGE:(\d+)>>>$', ln.strip())
        if m:
            current_page = int(m.group(1))
        page_of_line[idx] = current_page

    # Cross-check every heading candidate against the catalog's own known
    # product names before trusting it as a real page heading. Needed
    # because some CARATTERISTICHE TECNICHE spec-callout labels are ALSO
    # short, fully-uppercase, digit-free text ("PIANO IN MARMO" on
    # Circus) and would otherwise falsely end the scan before it ever
    # reaches the real price table -- confirmed via real output (the
    # earlier "1. RIVESTIMENTO"/"2. GAMBE" case had a digit to filter on;
    # this one doesn't).
    all_headings_upper = {h.strip().upper() for h in all_headings}

    def _is_known_heading(candidate):
        cu = candidate.upper()
        return any(cu == h or cu.startswith(h + " ") for h in all_headings_upper)

    heading_positions = [(idx, _bonaldo_heading_candidate(ln)) for idx, ln in enumerate(lines)]
    heading_positions = [(idx, c) for idx, c in heading_positions
                          if c is not None and _is_known_heading(c)]
    # EXACT match only for scan-START points -- a longer heading variant
    # of the SAME base product (e.g. "ARTIKA GAMBE RIVESTITE", a
    # different-leg-style sibling table further down "Artika"'s own page)
    # is already covered by a single continuous scan from the EARLIEST
    # matching position (exact OR longer-variant) all the way to the
    # first truly-different heading -- so a longer variant is included in
    # the match set used to pick that single start, not just exact
    # matches. Exact-match-only was tried first and broke the opposite
    # direction: on Flatiron table, the longer variant ("FLATIRON TABLE
    # MONO MATERIAL") is the FIRST heading in the file, on page 113,
    # while the exact "Flatiron table" heading only reprints starting on
    # page 114 -- exact-match-only picked page 114 as the start,
    # silently skipping page 113's two sub-tables entirely (confirmed via
    # real output: 33 rows instead of the real 48, with zero flag raised
    # since nothing LOOKED wrong, it just silently had less data).
    matching_positions = [idx for idx, c in heading_positions if _bonaldo_heading_matches(c, heading_text)]

    flags = []
    if not matching_positions:
        flags.append((None, product_name, "own heading not found in its extracted text -- nothing parsed"))
        return [], flags
    own_starts = [min(matching_positions)]

    rows = []
    found_table = False
    # Only the FIRST occurrence is a real scan start. Unlike Cattelan
    # (one shared text blob per raw source file, where a product's own
    # heading can legitimately repeat at genuinely disjoint positions),
    # each Bonaldo product's text file is already scoped to just that
    # product's own catalog_index page range -- so a heading repeating
    # exactly (not just as a longer same-product variant, which
    # _bonaldo_heading_matches already tolerates) is just a running
    # per-page header reprint within ONE continuous multi-page product,
    # not a second disjoint section. Treating every repeat as its own
    # start double-scanned (and double-counted) everything from the
    # second page onward -- confirmed via real output on Flatiron table,
    # whose "FLATIRON TABLE" heading reprints on each of its 3 pages.
    for start in own_starts[:1]:
        end = len(lines)
        for idx, c in heading_positions:
            if idx > start and not _bonaldo_heading_matches(c, heading_text):
                end = idx
                break
        i = start
        while i < end:
            if BONALDO_CHAIR_HEADER_RE.search(lines[i]):
                found_table = True
                block_rows, next_i = _parse_bonaldo_chair_block(lines, i, end, page_of_line, product_name, brand, flags)
                rows.extend(block_rows)
                i = max(next_i, i + 1)
            elif BONALDO_TABLE_HEADER_RE.search(lines[i]):
                found_table = True
                block_rows, next_i = _parse_bonaldo_table_block(lines, i, start, end, page_of_line, product_name, brand, flags)
                rows.extend(block_rows)
                i = max(next_i, i + 1)
            elif _bonaldo_sofa_header_names_sizes(lines[i])[1]:
                found_table = True
                block_rows, next_i = _parse_bonaldo_sofa_block(lines, i, start, end, page_of_line, product_name, brand, flags)
                rows.extend(block_rows)
                i = max(next_i, i + 1)
            else:
                i += 1

    if not found_table and not rows:
        flags.append((page_of_line[own_starts[0]] if own_starts else None, product_name,
                       "no chair-shape ('RIVESTIMENTO...CODICE...GAMBE'), table-shape "
                       "('LxP'), or sofa-shape ('RIVESTIMENTO' + size columns) table "
                       "found -- needs manual check"))

    return rows, flags


# ---------------------------------------------------------------------------
# Varaschini -- Shape A (single-item, fabric-category-tier pricing: cat. B -
# COM / C / D / E / Luxury, confirmed via real page images on Allegra/Bahia/
# Bali during the structural walk). ONLY Shape A is handled here -- Shapes
# B/C/D/E are each structurally different (finish-combo flat pricing,
# modular diagram+price-page split, dense flat SKU lists, and a
# combinatorial base x top price matrix respectively) and are intentionally
# out of scope until Shape A is fully live and regression-verified.
#
# ARCHITECTURAL DIFFERENCE from the other 3 brands' parsers: those are
# called once per catalog_index.json ENTRY, each reading its own dedicated
# text file. Varaschini's assets are deliberately deduped BY PAGE (517
# unique page files shared across 1,302 catalog entries, ~2.5 entries/page
# on average) -- so calling this once per entry would re-parse the same
# shared page file redundantly AND risk mislabeling a sibling product's
# rows under the wrong product_name if not scoped correctly. Instead this
# is called once per unique page, given the list of catalog entries that
# page actually contains, and returns rows already correctly attributed to
# each entry's own product_name via its art_code.
# ---------------------------------------------------------------------------

VARASCHINI_CODE_TOKEN = re.compile(r"^[0-9]{3,6}[A-Z]{0,3}[0-9]{0,2}[A-Z]{0,2}$")
VARASCHINI_ART_PREFIX = re.compile(r"\bart\.?\s+([0-9]{3,6}[0-9A-Z]{0,6})\b", re.IGNORECASE)

# Mirrors extract_catalog.py's _VARASCHINI_9C5_CODE_TOKEN -- same digit-
# letter-digits code shape ("9C5001"), same reason it can't match
# VARASCHINI_CODE_TOKEN above (requires 3-6 LEADING digits before any
# letter). Used by parse_file_varaschini_teli_di_copertura, which needs no
# "art." prefix regex of its own -- it pairs codes to prices via TSV
# coordinates, not a text-prefix trigger.
VARASCHINI_9C5_CODE_TOKEN = re.compile(r"^9C5[0-9]{2,4}[A-Z]?$")
VARASCHINI_TIER_LABEL_RE = re.compile(r"\bcat\.\s*(B\s*-\s*COM|C|D|E|Luxury)\b", re.IGNORECASE)
# Some outdoor products (confirmed 2026-08-23: Emma's whole 236M-code
# family, every product type -- sofas, chairs, daybeds, bergeres, all
# checked individually against real source pages, not assumed uniform from
# one sample) offer a structural-material choice (Aluminium vs Iroko/Legno
# wood) CROSSED with the usual 5 fabric "cat." tiers, printed as a 2-column
# sub-table: each tier row has 2 prices side by side instead of 1, under a
# repeated-prefix header line ("STRUTTURA ALLUMINIO    STRUTTURA IROKO" for
# sofas, "GAMBE ALLUMINIO    GAMBE LEGNO" for chairs/daybeds -- confirmed
# both wordings, real prefix varies by product type but the pattern is
# always PREFIX ALLUMINIO ... (same)PREFIX (IROKO|LEGNO)). Requires the
# SAME prefix word to repeat before both material names -- normal single-
# space label wording either side (PREFIX ALLUMINIO, PREFIX IROKO/LEGNO),
# but the real COLUMN GAP between the two labels is 2+ spaces (confirmed:
# "STRUTTURA ALLUMINIO" then 4 spaces then "STRUTTURA IROKO"), which is
# what actually distinguishes this from this catalog's own prose
# "alluminio"/"iroko" in single-spaced running text with no repeated
# prefix word immediately before "iroko" -- confirmed: matches 0 times
# against every Emma product's own descriptive paragraph, only against the
# real column-header line.
VARASCHINI_DUAL_MATERIAL_HEADER_RE = re.compile(
    r"(\S+)\s+ALLUMINIO\b\s{2,}\1\s+(IROKO|LEGNO)\b", re.IGNORECASE
)
# A "frame only" (no cushion) flat-price option that sits ALONGSIDE the 5
# "cat." fabric tiers in the SAME block on upholstered items (confirmed
# widespread: 32 pages, e.g. Barcode p22's "2180E"). Its own price must be
# excluded from the cat.-tier price scan (same reason as "cover") to avoid
# a 5-vs-6 count mismatch, but -- unlike a "cover" accessory -- it's a
# real, separately purchasable variant of THIS product, so it's captured
# as its own row rather than just discarded.
VARASCHINI_FRAME_ONLY_RE = re.compile(
    r"(?:solo\s+scocca|only\s+frame)[^\d€]{0,80}€[\s\x00-\x1f]*([\d][\d.,]*)",
    re.IGNORECASE)
# Belt/Belt Air's own replacement-cover accessory ("2212C ... OUTFIT COVER
# ... € 462") -- same "real, separately purchasable extra, captured as its
# own row rather than counted as a 6th cat.-tier price" pattern as
# VARASCHINI_FRAME_ONLY_RE above. Previously left IN the tier-price scan
# (a prior version of this file's own comment, now corrected, claimed it
# was always a lone unclaimed price with no tier labels alongside it in
# the same block -- true for the 10 occurrences checked at the time, all
# on pages 61/72/129/130/131) -- confirmed 2026-08-23 that's no longer
# true once Belt's page-anchor fix made p104/105/etc. reachable: "OUTFIT
# COVER" there sits in the SAME block as 5 real cat.-tier prices,
# producing a 5-vs-6 mismatch that silently dropped every real row.
VARASCHINI_OUTFIT_COVER_RE = re.compile(
    r"outfit\s+cover[^\d€]{0,80}€[\s\x00-\x1f]*([\d][\d.,]*)",
    re.IGNORECASE)
# Cuscini e Tessuti's tier labels have NO "cat." prefix at all and use
# periods ("B - C.O.M." not "B - COM"), confirmed on p571's raw text. A
# bare single-letter "C"/"D"/"E" is a real false-positive risk if matched
# anywhere in a line the way the "cat."-prefixed pattern safely can be --
# so this requires the REST of the (column-sliced) line after the label to
# be JUST an optional trailing "(ml X,XX /h Y,YY)" metraggio parenthetical
# (only ever trails the "B - C.O.M." row, never the bare C/D/E/Luxury rows)
# and/or an optional trailing price -- p571's 2-column layout puts
# label+price on the SAME line for one column while the other column's
# label and price fall on separate lines (confirmed: which column gets
# which pattern depends on how much horizontal room its dimension text
# used up), so the label match must tolerate a same-line price without
# over-matching into the next column (handled separately by column-slicing
# the line BEFORE this regex ever sees it, in parse_file_varaschini_shape_a).
# The label itself is NOT anchored to the start of the line -- confirmed on
# p571's "2737" (Cuscino "SOFT"/"SOFT" Cushion): a custom item name prints
# BEFORE the B-C.O.M. label on that same physical line, so a start-anchor
# would silently drop that one tier row instead of just its price. Requiring
# a preceding start-of-string-or-whitespace still blocks a label matching
# mid-word (e.g. the "C" in "Cuscino"). The trailing price digit run is
# OPTIONAL even when a "€" is present -- confirmed on p572's "2736": its
# "C" tier's "€" prints alone on the label's own line with no digits after
# it at all, while the digit run ("187") lands on a DIFFERENT, otherwise
# unrelated line with no "€" of its own (a PDF vertical-reflow artifact
# splitting one price glyph run away from its symbol). Requiring digits
# here would make the label regex simply not match this line, silently
# producing an equal (wrong) labels/prices count that hides the gap
# instead of flagging it. Matching the label anyway --with an empty price
# capture-- keeps the label in labels_found so the label/price COUNT
# MISMATCH this creates surfaces as a normal flagged, triaged gap rather
# than a silently missing row.
VARASCHINI_TIER_LABEL_RE_BARE = re.compile(
    r"(?:^|\s)(B\s*-\s*C\.?O\.?M\.?|C|D|E|Luxury)\s*(?:\(ml[^)]*\))?"
    r"\s*(?:€[\s\x00-\x1f]*[\d.,]*)?\s*$", re.IGNORECASE)
# Strip stray control bytes (e.g. '\x08') this PDF's font occasionally
# emits right after the € glyph (confirmed on Dolmen p210, Bali p16)
# before hunting for a price digit run.
VARASCHINI_PRICE_RE = re.compile(r"€[\s\x00-\x1f]*([\d][\d.,]*)")
VARASCHINI_DIMENSION_RE = re.compile(r"W\s*[\d /]+[\"”]\s*-\s*H\s*[\d /]+[\"”]\s*-\s*D\s*[\d /]+[\"”]")


def _varaschini_find_art_blocks(lines):
    """Locate every 'art.' + code occurrence and the line-range block that
    belongs to it (from its own trigger line to the next one, or EOF).

    Confirmed via real text across Allegra/Bahia/Bali that a code can
    appear in three different physical arrangements relative to its "art."
    label, all caused by -layout linearizing side-by-side PDF columns:
      1) same line: "art. 2214" (Belt/Belt Air diagram-grid style)
      2) "art." alone, code some lines BELOW (Allegra/Bahia/System style,
         with an intervening STRUTTURA/TOP column header line in between)
      3) "art." alone, code some lines ABOVE (confirmed on Dolmen p210:
         "1820L" prints one line before its own bare "art." label)
    All three are tried; forward lookahead is preferred, backward lookback
    is the fallback only when forward finds nothing.
    """
    triggers = []
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        for m in VARASCHINI_ART_PREFIX.finditer(line):
            prefix_ctx = line[max(0, m.start() - 15):m.start()].lower()
            if "cover" in prefix_ctx:
                continue
            # "- art. XXXX" (a dash immediately before "art.", only
            # whitespace between) is this catalog's general cross-reference
            # convention, not just for "cover" -- confirmed widespread (328
            # hits outside "cover" alone) for bundle-quantity call-outs
            # ("2 pz - art. 2728"), handling-kit and base-cover accessory
            # lines ("Kit movimentazione tavolo - art. 3899K1"). Excluding
            # only "cover" left every OTHER dash-prefixed cross-reference
            # free to be mistaken for a new product's own trigger, silently
            # truncating whatever block it landed inside (confirmed on
            # System p478/p146). A genuine product's own "art. CODE" trigger
            # is never dash-prefixed in this catalog.
            if prefix_ctx.rstrip().endswith("-"):
                continue
            triggers.append((i, m.group(1).upper()))
        if re.match(r"^art\.?(\s|$)", line, re.IGNORECASE):
            found = False
            for j in range(i + 1, min(i + 6, len(lines))):
                cand = lines[j].strip()
                if not cand:
                    continue
                first_tok = cand.split()[0] if cand.split() else ""
                if VARASCHINI_CODE_TOKEN.match(first_tok):
                    triggers.append((i, first_tok.upper()))
                    found = True
                    break
                if len(cand) > 3 and cand[0].islower():
                    break
            if not found:
                for j in range(i - 1, max(i - 3, -1), -1):
                    cand = lines[j].strip()
                    if not cand:
                        continue
                    first_tok = cand.split()[0] if cand.split() else ""
                    if VARASCHINI_CODE_TOKEN.match(first_tok):
                        triggers.append((i, first_tok.upper()))
                    break

    seen = set()
    uniq = []
    for i, code in triggers:
        key = (i, code)
        if key in seen:
            continue
        seen.add(key)
        uniq.append((i, code))
    uniq.sort(key=lambda t: t[0])

    blocks = {}  # code -> (start, end) of FIRST occurrence
    for idx, (i, code) in enumerate(uniq):
        end = uniq[idx + 1][0] if idx + 1 < len(uniq) else len(lines)
        if code not in blocks:
            blocks[code] = (i, end)
    return blocks


def _varaschini_find_belt_composition_blocks(lines, target_codes):
    """Block finder for Belt/Belt Air's "ESEMPI DI COMPOSIZIONI" pages
    (129-131) -- see _varaschini_belt_composition_codes in
    extract_catalog.py for the discovery-side counterpart and its full
    explanation of why the default `_varaschini_find_art_blocks` fails
    here: these pages have only ONE "art ." trigger (note the space before
    the period) per page, shared by several composition codes, so the
    default finder's "block runs to the next TRIGGER" logic either misses
    every code past the first (confirmed: block detection failed for
    249C2C/249C3/249C3C entirely) or, worse, silently absorbs a
    NEIGHBORING code's whole price table into the wrong block (confirmed:
    249C2's block absorbed all 10 of 249C2+249C3's combined tier prices).

    Unlike the generic block finders, this one is handed the TARGET codes
    directly (from catalog_index, already correctly discovered via TSV
    coordinates) rather than re-discovering triggers itself -- each code
    reliably appears as the first token of its own physical line in this
    page's -layout text (confirmed: "249C2C" prints as one clean line-
    starting token here, unlike some other TSV-split cases), so blocks are
    built the same way as the default finder (start of one code's line to
    the start of the next KNOWN code's line) but seeded with every real
    code's line, not just whichever one sits closest to the shared header.
    """
    code_lines: dict[str, int] = {}
    for i, raw in enumerate(lines):
        first_tok = raw.strip().split()[0] if raw.strip() else ""
        if first_tok in target_codes and first_tok not in code_lines:
            code_lines[first_tok] = i

    ordered = sorted(code_lines.items(), key=lambda kv: kv[1])
    blocks = {}
    for idx, (code, start) in enumerate(ordered):
        end = ordered[idx + 1][1] if idx + 1 < len(ordered) else len(lines)
        blocks[code] = (start, end)
    return blocks


def _varaschini_classify_top_tiers(block_lines, prices_found):
    """Classify a "no cat.-label, multiple unclaimed prices" block as a
    TOP-MATERIAL price grid (HPL / HPL Perla-Ardesia premium edge /
    Ceramica Bocciardata) instead of leaving it a permanent "materials-
    grid, not yet parsed" flag. Confirmed on System/System Star (e.g.
    p463's "2440": HPL=539, HPL premium edge=627, Ceramica Bocciardata=765)
    -- this is the SAME underlying pattern behind many of Shape A's
    already-flagged materials-grid items across OTHER collections too
    (Allegra, Babylon, Cricket, Customade, ...), not something unique to
    System.

    Unlike the "cat." tiers, there is no clean per-price text label here --
    a price's tier is only knowable from which swatch-group section it
    prints closest to (a "CERAMICA BOCCIARDATA" header, or "Perla"/
    "Ardesia"/"black edge" premium-finish color names). Pairing is done by
    scanning the TEXT BETWEEN each price and the previous one for these
    markers, defaulting to "HPL" only when nothing else matches (the
    standard/first tier has no marker of its own).

    Returns [] (never guesses) unless: every price segment finds a marker
    (or is the plain HPL default), the resulting tier labels are all
    distinct, and at least one is "HPL" -- since a page that's actually
    some OTHER kind of multi-price grid (not this TOP-material pattern)
    would fail one of these checks and correctly fall through to the
    existing "flag, don't guess" behavior instead.
    """
    prev_li = 0
    tiers = []
    for li, _pos, price in prices_found:
        segment = "\n".join(block_lines[prev_li:li + 1]).lower()
        if "ceramica" in segment or "bocciardata" in segment:
            tier = "Ceramica Bocciardata"
        elif "perla" in segment or "ardesia" in segment or "black edge" in segment:
            tier = "HPL Perla/Ardesia"
        elif "hpl" in segment or not tiers:
            tier = "HPL"
        else:
            return []
        tiers.append(tier)
        prev_li = li
    if len(set(tiers)) != len(tiers) or "HPL" not in tiers:
        return []
    return list(zip(tiers, (pr for _, _, pr in prices_found)))


def parse_file_varaschini_shape_a(path, page_num, entries_for_page, brand="Varaschini", block_finder=None, tier_label_re=None):
    """entries_for_page: catalog_index.json dicts (must include 'art_code'
    and 'product_name') that this ONE shared page contains. Returns
    (rows, flags) -- flags is a list of (page, product_name, reason).

    block_finder: defaults to _varaschini_find_art_blocks (the "art."
    label detector). Everything AFTER block detection here -- dimension
    extraction, tier label/price pairing, row construction -- is about the
    PRICE TABLE format, not how a code's block boundary was found, so it's
    reusable for any collection using Shape A's 5-tier structure regardless
    of whether its codes are "art."-prefixed or bare. Confirmed on Cuscini
    e Tessuti (p571: art 2713/2709/2708/2701 each get 5 tier rows) --
    passing block_finder=_varaschini_find_flat_code_blocks (the same
    bare-code detector Shape D uses) reuses this logic instead of
    duplicating it for a collection that was only ever mislabeled "D",
    never structurally different from Shape A.

    tier_label_re: defaults to VARASCHINI_TIER_LABEL_RE (requires a literal
    "cat." prefix, e.g. "cat. B - COM"). NOT actually byte-identical
    everywhere, unlike the claim in an earlier version of this docstring:
    Cuscini e Tessuti's labels have NO "cat." prefix at all and use
    periods ("B - C.O.M." not "B - COM", bare "C"/"D"/"E"/"Luxury" with
    nothing before them) -- confirmed by direct inspection of p571's raw
    text, not assumed from the "reuse Shape A" framing. Its override passes
    a whole-line-anchored variant instead, since a bare single-letter "C"/
    "D"/"E" would be a real false-positive risk if matched anywhere in a
    line the way the "cat."-prefixed version safely can.
    """
    finder = block_finder or _varaschini_find_art_blocks
    label_re = tier_label_re or VARASCHINI_TIER_LABEL_RE
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()

    blocks = finder(lines)
    rows = []
    flags = []

    for entry in entries_for_page:
        code = entry["art_code"]
        product_name = entry["product_name"]
        if code not in blocks:
            flags.append((page_num, product_name, f"art_code {code} not found via {finder.__name__} block detection on its recorded page"))
            continue
        block = blocks[code]
        # 4-tuple (start, end, col_start, col_end) means block_finder found
        # this code in a genuine 2-column table (see
        # _varaschini_find_flat_code_blocks) and each of its lines must be
        # sliced to JUST this code's column before any regex sees it --
        # otherwise the OTHER column's label/price text on a shared
        # physical line gets scanned too. 2-tuple (start, end) from the
        # default "art." block finder means single-column, no slicing.
        if len(block) == 4:
            start, end, col_start, col_end = block
            block_lines = [ln[col_start:col_end] for ln in lines[start:end]]
        else:
            start, end = block
            block_lines = lines[start:end]
        block_text = "\n".join(block_lines)

        dim_m = VARASCHINI_DIMENSION_RE.search(block_text)
        size = dim_m.group(0).strip() if dim_m else None

        frame_only_m = VARASCHINI_FRAME_ONLY_RE.search(block_text)
        if frame_only_m:
            rows.append({
                "brand": brand,
                "product_name": product_name,
                "model_variant": product_name,
                "variant_context": None,
                "size": size,
                "fabric_tier": "Solo Scocca / Only Frame",
                "tier_label": "Imbottitura/Rivestimento",
                "code": code,
                "price_eur": frame_only_m.group(1),
                "source_pdf_page": page_num,
            })

        outfit_cover_m = VARASCHINI_OUTFIT_COVER_RE.search(block_text)
        if outfit_cover_m:
            rows.append({
                "brand": brand,
                "product_name": product_name,
                "model_variant": product_name,
                "variant_context": None,
                "size": size,
                "fabric_tier": "Outfit Cover",
                "tier_label": "Imbottitura/Rivestimento",
                "code": code,
                "price_eur": outfit_cover_m.group(1),
                "source_pdf_page": page_num,
            })

        # Tier label and its price are USUALLY on the same physical line,
        # but not always -- confirmed on Bali p16's "2384": "cat. B - COM"
        # sits on one line (interrupted by a "Teak" structure-color name
        # and a dimension line squeezed in after it) while its price only
        # appears two lines further down. Since the 5 tiers always appear
        # in fixed order (B-COM, C, D, E, Luxury) and their prices are
        # always listed in that same increasing-price order, pair them by
        # ORDER OF APPEARANCE across the whole block instead of requiring
        # same-line adjacency.
        labels_found = []
        prices_found = []
        for li, bl in enumerate(block_lines):
            has_label_this_line = False
            for m in label_re.finditer(bl):
                labels_found.append((li, m.start(), re.sub(r"\s+", " ", m.group(1).strip())))
                has_label_this_line = True
            low = bl.lower()
            line_had_euro_price = False
            for m in VARASCHINI_PRICE_RE.finditer(bl):
                # whole-line-prefix check, not a short lookback window --
                # "cover - art. 9451C               €      226" routinely
                # has 20-30+ padding characters between "cover" and its
                # price, wider than a naive fixed window (confirmed bug:
                # this exact gap let 6 "cover" prices get miscounted as a
                # real 6th tier before being caught here).
                # Also exclude any OTHER dash-prefixed "- art. XXXX"
                # accessory cross-reference on the same line (e.g. "Lampada
                # Outdoor Lighting - art. 8001 € 384"), not just "cover" --
                # confirmed on Tibidabo p497's "14250" (Pouf): extending
                # this block's boundary to stop the SAME dash-prefixed
                # pattern from being mistaken for a new trigger (see
                # _varaschini_find_art_blocks) also pulled 2 accessory
                # prices into this block's scan range, turning a clean
                # 5-labels/5-prices match into a 5-vs-7 mismatch that
                # silently dropped all 5 real rows.
                # "solo scocca"/"only frame" (see VARASCHINI_FRAME_ONLY_RE)
                # and "outfit cover" (see VARASCHINI_OUTFIT_COVER_RE) are
                # both captured separately above, not as cat.-tier prices --
                # see VARASCHINI_OUTFIT_COVER_RE's own comment for why
                # "outfit cover" moved from an explicit exception here to an
                # exclusion, matching solo scocca/only frame's treatment.
                prefix = low[:m.start()]
                if ("cover" in prefix or re.search(r"-\s*art\.?\s", prefix)
                        or "solo scocca" in prefix or "only frame" in prefix):
                    continue
                prices_found.append((li, m.start(), m.group(1)))
                line_had_euro_price = True
            # Fallback: a page with a "COLLEZIONI ABBINABILI / MATCHABLE
            # COLLECTIONS" cross-reference side-box (confirmed on Babylon
            # p162) can displace the '€' glyph off one tier row's price
            # entirely, leaving a bare trailing number with no € at all
            # ("cat. C ... 2.195") while every other tier row on the same
            # block keeps its €. Only trusted on a line that ALREADY has a
            # recognized tier label AND no €-price of its own, to avoid
            # treating arbitrary numbers elsewhere as fake prices.
            if has_label_this_line and not line_had_euro_price:
                m = re.search(r"(\d{1,3}(?:\.\d{3})*)\s*$", bl.rstrip())
                if m:
                    prices_found.append((li, m.start(), m.group(1)))
        labels_found.sort(key=lambda t: (t[0], t[1]))
        prices_found.sort(key=lambda t: (t[0], t[1]))

        tier_rows = []
        if labels_found:
            if len(labels_found) == len(prices_found):
                tier_rows = [(lab, pr, None) for (_, _, lab), (_, _, pr) in zip(labels_found, prices_found)]
            elif len(prices_found) == 2 * len(labels_found):
                # Structural-material choice (Aluminium vs Iroko/Legno wood)
                # crossed with the usual 5 fabric tiers -- see
                # VARASCHINI_DUAL_MATERIAL_HEADER_RE. Each tier's 2 prices
                # sit on the SAME physical line as each other (sorted
                # left-to-right, Aluminium's column always first/left in
                # every case checked), so the existing "order of
                # appearance across the whole block" pairing (see the
                # 1:1 branch above) extends cleanly to 2:1 by taking
                # consecutive PAIRS from prices_found rather than single
                # prices -- same principle, wider stride.
                dual_m = VARASCHINI_DUAL_MATERIAL_HEADER_RE.search(block_text)
                if dual_m:
                    prefix = dual_m.group(1).strip().title()
                    variant_a = f"{prefix} Alluminio"
                    variant_b = f"{prefix} {dual_m.group(2).title()}"
                    for (_, _, lab), (_, _, pr_a), (_, _, pr_b) in zip(
                        labels_found, prices_found[0::2], prices_found[1::2]
                    ):
                        tier_rows.append((lab, pr_a, variant_a))
                        tier_rows.append((lab, pr_b, variant_b))
                else:
                    flags.append((page_num, product_name,
                                   f"tier label/price count mismatch for art_code {code}: "
                                   f"{len(labels_found)} labels vs {len(prices_found)} prices -- skipped rather than guessing a pairing"))
                    continue
            else:
                flags.append((page_num, product_name,
                               f"tier label/price count mismatch for art_code {code}: "
                               f"{len(labels_found)} labels vs {len(prices_found)} prices -- skipped rather than guessing a pairing"))
                continue

        if tier_rows:
            for tier_label, price, material in tier_rows:
                rows.append({
                    "brand": brand,
                    "product_name": product_name,
                    "model_variant": product_name,
                    "variant_context": material,
                    "size": size,
                    "fabric_tier": f"cat. {tier_label}",
                    "tier_label": "Imbottitura/Rivestimento",
                    "code": code,
                    "price_eur": price,
                    "source_pdf_page": page_num,
                })
        else:
            # No "cat." tier labels at all -- either a flat single-price
            # item (accessory, table base, coffee table) or a materials
            # grid (e.g. Allegra's 2587 Tavolino: HPL vs Ceramica TOP
            # options, no fabric tiers at all since it has no upholstery).
            # Only trust EXACTLY ONE unclaimed, non-cover price as the flat
            # price -- more than one with no tier labels to disambiguate
            # them is the materials-grid case, tried below via
            # _varaschini_classify_top_tiers before falling back to a flag.
            candidates = [pr for _, _, pr in prices_found]
            if len(candidates) == 1:
                rows.append({
                    "brand": brand,
                    "product_name": product_name,
                    "model_variant": product_name,
                    "variant_context": None,
                    "size": size,
                    "fabric_tier": None,
                    "tier_label": None,
                    "code": code,
                    "price_eur": candidates[0],
                    "source_pdf_page": page_num,
                })
            elif len(candidates) == 0:
                # Not a real gap if frame_only_m/outfit_cover_m already
                # produced this code's own row above -- confirmed
                # 2026-08-23 on Belt/Belt Air's 249C4C: a standalone
                # "OUTFIT COVER" accessory with no cat.-tier prices of its
                # own at all, correctly captured via outfit_cover_m, but
                # this branch used to fire anyway (0 candidates left once
                # outfit-cover prices are excluded from the tier scan) and
                # flag a misleading "no price found" for a code that
                # already had a real row.
                if not (frame_only_m or outfit_cover_m):
                    flags.append((page_num, product_name, f"no price found in block for art_code {code}"))
            else:
                top_tiers = _varaschini_classify_top_tiers(block_lines, prices_found)
                if top_tiers:
                    for tier_label, price in top_tiers:
                        rows.append({
                            "brand": brand,
                            "product_name": product_name,
                            "model_variant": product_name,
                            "variant_context": None,
                            "size": size,
                            "fabric_tier": tier_label,
                            "tier_label": "TOP",
                            "code": code,
                            "price_eur": price,
                            "source_pdf_page": page_num,
                        })
                else:
                    flags.append((page_num, product_name,
                                   f"{len(candidates)} unlabeled prices found for art_code {code} with no fabric tiers -- "
                                   f"likely a materials-grid item (e.g. HPL/Ceramica TOP options), not yet parsed, skipped rather than guessed"))

    return rows, flags


def _varaschini_find_flat_code_blocks(lines):
    """Shape D block detection: unlike Shape A, there is no 'art.' label at
    all per item -- just one shared 'ART./CODE' column header per page/
    table, then each SKU is a bare code as the first token of its own
    COLUMN-CHUNK (a line split on runs of 2+ spaces, matching this
    codebase's existing column-boundary convention -- see
    _varaschini_find_records_flat's docstring in extract_catalog.py for
    the same fix on the discovery side).

    Genuinely 2-column pages (confirmed on Cuscini e Tessuti p571: a
    left-column code like "2713" and a right-column code like "2730"
    share one physical line) need column-position awareness for BLOCK
    BOUNDARIES too, not just trigger detection: a naive "block ends at the
    next trigger's line" collapses the left code's block to zero lines,
    since the right code's trigger sits on that SAME line.

    Column boundaries come from the page's own header row, which prints
    "CODE" once per column-group (e.g. p571: "CODE ... CODE ..." with the
    2nd "CODE" at col 93; p572/p573: col 102) -- NOT from clustering code-
    token trigger positions by proximity. Proximity clustering was tried
    first and is unreliable: VARASCHINI_CODE_TOKEN also matches a bare
    price-digit run with no adjacent price context (confirmed on p572:
    a wrapped price like "187" with its "€" stranded on another line looks
    exactly like a product code), and those false triggers pull a
    proximity-derived boundary to the wrong place -- confirmed on p572,
    where trigger clustering computed col 92 (truncating the left column's
    real price digits, which print out to ~col 100) while the header's
    actual boundary is 102. Falls back to proximity clustering only when
    no repeated "CODE" header is found (single-column pages).

    An earlier version filtered triggers to a caller-supplied target-code
    allowlist to keep false-positive digit-run triggers from corrupting a
    real code's block boundary. That approach was reverted: it ALSO
    discarded a genuine trigger for a code that's simply not one of THIS
    collection's own entries (e.g. Tibidabo's "2729" prints on this same
    Cuscini e Tessuti page as a cross-referenced accessory row) -- and that
    trigger is still needed as a boundary marker even though nobody ever
    looks its price up. Confirmed on p572: allowlisting away "2729" made
    "2732"'s block swallow 2729's own 5 price rows instead of stopping
    before them. has_later_content below is the fix that actually
    distinguishes real codes from price noise, without needing to know
    the target code set at all.

    Chunk-based per-line scanning (multiple triggers per physical line)
    only runs when a page's own header confirms it's genuinely 2-column
    (see header_bounds below). Single-column pages use the ORIGINAL
    first-token-of-the-whole-line check instead of chunk-scanning --
    switching every page to chunk-scanning regressed Marketing
    Communication's "901D1" (page 8, single column): its line also lists
    bundled reference codes "901D2 901D3 901D4 901D5" further along the
    SAME line before the price, and chunk-scanning treated each of those
    as its own trigger too, collapsing 901D1's block to nothing before it
    ever reached its own price.
    """
    header_bounds = []
    for raw in lines[:8]:
        positions = [m.start() for m in re.finditer(r"\bCODE\b", raw)]
        if len(positions) >= 2:
            header_bounds = positions
            break

    triggers = []  # (line_idx, char_pos, code)
    if header_bounds:
        for i, raw in enumerate(lines):
            if not raw.strip():
                continue
            chunks = re.split(r"(\s{2,})", raw)
            chunk_positions = []
            pos = 0
            for chunk in chunks:
                chunk_positions.append((chunk, pos))
                pos += len(chunk)
            for ci, (chunk, cpos) in enumerate(chunk_positions):
                if not chunk.strip():
                    continue
                chunk_toks = chunk.split()
                first_tok = chunk_toks[0] if chunk_toks else ""
                if not VARASCHINI_CODE_TOKEN.match(first_tok):
                    continue
                # A genuine product code is always followed by more content
                # later on the SAME line (a dimension "cm ...", a name, an
                # addon annotation like "B (ml...)"). A bare trailing digit
                # run with nothing after it on the line is a price
                # continuation that happens to match the code-token shape
                # (its '€' printed on an earlier or later row instead) --
                # confirmed on p572: "187"/"220"/"473" etc. are each the
                # LAST token on an otherwise-spent row. Without this check
                # they get treated as real block-boundary triggers and
                # corrupt neighboring blocks.
                has_later_content = any(c.strip() for c, _ in chunk_positions[ci + 1:])
                if not has_later_content:
                    continue
                triggers.append((i, cpos, first_tok.upper()))
    else:
        for i, raw in enumerate(lines):
            line = raw.strip()
            if not line:
                continue
            first_tok = line.split()[0] if line.split() else ""
            if VARASCHINI_CODE_TOKEN.match(first_tok):
                triggers.append((i, 0, first_tok.upper()))
    triggers.sort(key=lambda t: t[1])  # cluster by position first

    clusters: list[list[tuple[int, int, str]]] = []
    if header_bounds:
        # bucket each trigger by which header-derived column it falls in,
        # rather than by proximity to other triggers
        clusters = [[] for _ in header_bounds]
        for t in triggers:
            bi = 0
            for b in range(len(header_bounds)):
                if t[1] >= header_bounds[b]:
                    bi = b
            clusters[bi].append(t)
        # Within a bucket, only keep triggers sitting AT (within a small
        # tolerance of) the header's own "CODE" column position -- a real
        # code always prints in that field. A trigger deeper inside the
        # same bucket is really that column-group's OWN internal
        # PREZZO/PRICE sub-field, which can independently print a bare
        # 3-digit price matching the code-token shape (confirmed on p572:
        # the left group's ART field sits at col 0 while its own price
        # sub-field sits at col 92, both < the col-102 boundary with the
        # right group, so position-bucketing alone still lets "165"/"220"/
        # etc. through as if they were codes). has_later_content doesn't
        # catch these either, since the OTHER column's text often prints
        # further along the very same physical line.
        clusters = [[t for t in c if abs(t[1] - header_bounds[bi]) <= 5]
                    for bi, c in enumerate(clusters)]
        clusters = [c for c in clusters if c]  # drop empty columns
        cluster_col_starts = [header_bounds[i] for i in range(len(clusters))]
        cluster_col_ends = [header_bounds[i + 1] if i + 1 < len(header_bounds) else None
                             for i in range(len(clusters))]
    else:
        for t in triggers:
            if clusters and abs(t[1] - clusters[-1][-1][1]) <= 25:
                clusters[-1].append(t)
            else:
                clusters.append([t])
        cluster_col_starts = [min(t[1] for t in c) for c in clusters]
        cluster_col_ends = [cluster_col_starts[i + 1] if i + 1 < len(clusters) else None
                             for i in range(len(clusters))]

    blocks = {}
    for ci, cluster in enumerate(clusters):
        cluster.sort(key=lambda t: t[0])  # within a column, order by line
        col_start = cluster_col_starts[ci]
        col_end = cluster_col_ends[ci]
        for idx, (i, _pos, code) in enumerate(cluster):
            end = cluster[idx + 1][0] if idx + 1 < len(cluster) else len(lines)
            if code not in blocks:
                blocks[code] = (i, end, col_start, col_end)
    return blocks


def parse_file_varaschini_shape_d(path, page_num, entries_for_page, brand="Varaschini"):
    """Shape D: dense flat SKU list, one flat price per code, no options
    table (Marketing Communication, Outdoor Cooking, Trama, Carpet Design,
    Outdoor Lighting, Strumenti Commerciali, Teli di Copertura, Prodotti
    per la Pulizia, Cuscini e Tessuti, Basi Tavolini).

    One confirmed edge case: Carpet Design's '256M'/'256MR' rug items are
    priced PER SQUARE METER ('€/mq') rather than a flat total -- the price
    NUMBER sits on a different line than the '€/mq' label, sometimes with
    no '€' glyph adjacent to the number at all. Verified this is genuinely
    narrow (exactly these 2 of 194 Shape D entries) by grepping every
    Shape D page range for 'mq'/'sqm'/'square meter' before trusting a
    fallback for it -- the only other "mq" hits catalog-wide are unrelated
    (a cover's fabric weight "205 gr./mq." and a lamp's "coverage 10 sqm"
    remote-control range, neither a price). The per-sqm nature is recorded
    in fabric_tier ("al mq") since there's no dedicated unit field in this
    schema, mirroring how Cattelan reuses tier_label for material
    categories that aren't really "fabric tiers" either.
    """
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()

    blocks = _varaschini_find_flat_code_blocks(lines)
    rows = []
    flags = []

    for entry in entries_for_page:
        code = entry["art_code"]
        product_name = entry["product_name"]
        if code not in blocks:
            flags.append((page_num, product_name, f"art_code {code} not found via bare-code block detection on its recorded page"))
            continue
        # blocks[code] may be a 4-tuple (start, end, col_start, col_end)
        # now that _varaschini_find_flat_code_blocks also reports column
        # bounds for parse_file_varaschini_shape_a's benefit -- this flat
        # single-price parser doesn't need column slicing (none of its
        # collections are confirmed 2-column), so only the line range is
        # used here.
        start, end = blocks[code][0], blocks[code][1]
        block_lines = lines[start:end]

        candidates = []
        for bl in block_lines:
            low = bl.lower()
            for m in VARASCHINI_PRICE_RE.finditer(bl):
                if "cover" in low[:m.start()]:
                    continue
                candidates.append(m.group(1))

        price_unit = None
        if not candidates:
            has_mq_marker = any("mq" in bl.lower() for bl in block_lines)
            if has_mq_marker:
                for bl in block_lines:
                    s = bl.strip()
                    if not s or "mq" in s.lower():
                        continue
                    if re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{3})*", s):
                        candidates.append(s)
                        price_unit = "al mq"
                        break

        if len(candidates) == 1:
            rows.append({
                "brand": brand,
                "product_name": product_name,
                "model_variant": product_name,
                "variant_context": None,
                "size": None,
                "fabric_tier": price_unit,
                "tier_label": None,
                "code": code,
                "price_eur": candidates[0],
                "source_pdf_page": page_num,
            })
        elif len(candidates) == 0:
            flags.append((page_num, product_name, f"no price found in block for art_code {code}"))
        else:
            flags.append((page_num, product_name,
                           f"{len(candidates)} candidate prices found for art_code {code}, "
                           f"ambiguous which is real -- skipped rather than guessing"))

    return rows, flags


VARASCHINI_TSV_PRICE_DIGIT_RE = re.compile(r"^[\d][\d.,]*$")


def _varaschini_tsv_tokens(pdf_path):
    """Word-level (left, top, text) tokens for a single-page PDF via
    `pdftotext -tsv`. Unlike `-layout` text, coordinates don't depend on
    poppler's (sometimes-wrong) reading-order reconstruction -- needed for
    Composizione Tavoli's combinatorial base x top price MATRIX, where
    -layout's linearized text scatters a row's own code and price many
    lines apart (see _varaschini_composizione_tavoli_codes in
    extract_catalog.py, which already uses this same technique for CODE
    discovery; this is the PRICE-side counterpart)."""
    result = subprocess.run([PDFTOTEXT, "-tsv", "-enc", "UTF-8", pdf_path, "-"], capture_output=True)
    tsv_text = result.stdout.decode("utf-8", errors="replace")
    tokens = []
    for line in tsv_text.splitlines()[1:]:  # skip TSV header row
        parts = line.split("\t")
        if len(parts) < 12 or parts[0] != "5":  # level 5 = word-level token
            continue
        try:
            left, top = float(parts[6]), float(parts[7])
        except ValueError:
            continue
        tokens.append((left, top, parts[11]))
    return tokens


def parse_file_varaschini_composizione_tavoli(pdf_path, page_num, entries_for_page, brand="Varaschini"):
    """Composizione Tavoli: a combinatorial BASE x TOP price matrix, not a
    per-item list (confirmed on p574's image: table-base codes down the
    left as ROWS, HPL top codes across the top as COLUMNS, matrix cells
    price a base+top COMBINATION). Only each code's OWN standalone price
    is extracted here (a base's own "Prezzo" column right next to it, or a
    top's own "Prezzo" row right below its header) -- the combined matrix
    cell prices are a fundamentally different thing (a price for a PAIR of
    codes, not one code) that this schema (one row = one code) has no way
    to represent, so they're intentionally left unparsed rather than
    guessed at or forced into the wrong shape.

    Column/row positions are derived from the token coordinates
    THEMSELVES on each page (not hardcoded pixel values), since column
    widths are confirmed to vary page to page (e.g. Cuscini e Tessuti's
    2-column boundary was 93 vs 102 on different pages -- same lesson
    applies here): the header row is whichever set of codes share a
    top coordinate AND there's more than one of them (multiple top codes
    in a horizontal line); a lone code elsewhere on its own row is a base
    code, and its own price is the LEFTMOST price token to the right of
    it on that row (the base's own price column always sits between the
    code and the first matrix cell).
    """
    tokens = _varaschini_tsv_tokens(pdf_path)
    target_codes = {e["art_code"] for e in entries_for_page}

    # "€" tokens routinely carry a stray trailing control byte this PDF's
    # font emits right after the glyph (confirmed elsewhere in this file,
    # e.g. VARASCHINI_PRICE_RE -- same root cause here: "€\x08"), and a
    # per-square-meter price prints as a single "€/mq" token (confirmed
    # Carpet Design p554's "256M"/"256MR") rather than a bare "€". Both
    # are still real price markers, just wider than a literal "€" match.
    euro_tokens = [(l, t, "al mq" if "/mq" in txt else None)
                   for l, t, txt in tokens if txt.startswith("€")]
    digit_tokens = [(l, t, txt) for l, t, txt in tokens
                     if txt != "2026" and VARASCHINI_TSV_PRICE_DIGIT_RE.match(txt)]
    # Pair each "€" with the nearest digit run to its right on the same
    # row -- this codebase's TSV price format always splits them into
    # separate word tokens (confirmed on p574's dump). Vertical offset
    # between "€" and its digit run varies by page (0.4 units on
    # Composizione Tavoli's p574, 6+ units on Basi Tavolini's p586) --
    # kept well under a row's own height (~14 units) to avoid pairing
    # across rows. Horizontal gap also varies (an "€/mq" token is wider
    # than a bare "€", pushing its digit further right, confirmed p554).
    prices = []  # (left, top, price_str, unit) -- top is the DIGIT's own
    # top, not the €'s -- confirmed the € glyph's vertical metric drifts
    # further from a row's text baseline than the digit run does (Carpet
    # Design p554's "2562": code top 384.32, digit top 394.48 (gap 10.16,
    # fine) vs the € symbol's own top 397.44 (gap 13.12, wrongly exceeds
    # tolerance) -- using the digit's top keeps row-matching consistent.
    for el, et, unit in euro_tokens:
        best = None
        for dl, dt, dtxt in digit_tokens:
            if abs(dt - et) <= 8 and 0 <= (dl - el) <= 45:
                if best is None or dl < best[0]:
                    best = (dl, dt, dtxt)
        if best:
            dl, dt, dtxt = best
            prices.append((el, dt, dtxt, unit))

    code_tokens = [(l, t, txt) for l, t, txt in tokens
                    if txt != "2026" and VARASCHINI_CODE_TOKEN.match(txt) and txt in target_codes]

    # Header row = the top coordinate (rounded, small tolerance) shared by
    # the MOST code tokens -- a page with no top-code header at all (pure
    # continuation of base rows) safely finds no such cluster.
    from collections import defaultdict
    by_top_rounded = defaultdict(list)
    for l, t, txt in code_tokens:
        by_top_rounded[round(t)].append((l, t, txt))
    header_top = max(by_top_rounded, key=lambda k: len(by_top_rounded[k])) if by_top_rounded else None
    header_codes = set()
    if header_top is not None and len(by_top_rounded[header_top]) > 1:
        header_codes = {txt for _, _, txt in by_top_rounded[header_top]}

    rows = []
    flags = []
    found_codes = set()

    for l, t, txt in code_tokens:
        if txt in found_codes:
            continue
        if txt in header_codes:
            # Top code: its own price is in the "Prezzo" row below the
            # header, horizontally closest to this code's own column.
            candidates = [(l2, t2, pr, unit) for l2, t2, pr, unit in prices if t2 > t and t2 - t < 30]
            if not candidates:
                continue
            l2, t2, pr, unit = min(candidates, key=lambda c: abs(c[0] - l))
            if abs(l2 - l) > 20:
                continue
        else:
            # Base code: its own price is the LEFTMOST price to the right
            # of it on the same physical row.
            candidates = [(l2, t2, pr, unit) for l2, t2, pr, unit in prices if abs(t2 - t) <= 11 and l2 > l]
            if not candidates:
                continue
            l2, t2, pr, unit = min(candidates, key=lambda c: c[0])

        found_codes.add(txt)
        entry = next((e for e in entries_for_page if e["art_code"] == txt), None)
        product_name = entry["product_name"] if entry else f"Composizione Tavoli {txt}"
        rows.append({
            "brand": brand,
            "product_name": product_name,
            "model_variant": product_name,
            "variant_context": None,
            "size": None,
            "fabric_tier": unit,
            "tier_label": None,
            "code": txt,
            "price_eur": pr,
            "source_pdf_page": page_num,
        })

    for entry in entries_for_page:
        code = entry["art_code"]
        if code not in found_codes:
            flags.append((page_num, entry["product_name"],
                           f"art_code {code} not found or no own-price located via TSV coordinate pass on this page"))

    return rows, flags


def parse_file_varaschini_teli_di_copertura(pdf_path, page_num, entries_for_page, brand="Varaschini"):
    """Teli di Copertura's per-page code/price pairing via TSV coordinates,
    used for the WHOLE collection (not just the base-height x TOP-dimension
    GRID pages 565-569 -- see _varaschini_teli_di_copertura_grid_codes in
    extract_catalog.py for that discovery-side counterpart), including its
    flat "art. CODE" pages (558-564).

    Originally built only for the grid pages, then extended to the flat
    ones too after verifying the default "art." block-finder badly
    undercounts them: p562/p564 pack several "art. CODE name" occurrences
    onto ONE physical line (confirmed p564: 5 codes across one row), which
    the block finder's "this code's block runs to the NEXT trigger" logic
    can't correctly bound (test run: 7/16 priced on p562, 1/31 on p564).
    Every code here (whole collection, confirmed: 0 of the collection's
    existing price rows have ever had a fabric tier) is a single flat-
    priced item, never Shape A's cat. B-COM/C/D/E/Luxury tier structure --
    coordinate pairing needs no block boundary at all, so it isn't
    sensitive to how many codes share a physical line. Retested on
    p562/p564 with this approach: 16/16 and 29/31 (the 2 remaining misses,
    "220"/"245", are a pre-existing, different, genuine gap -- multiple
    unlabeled prices with no tier markers, a materials-grid item neither
    approach can safely parse without guessing).

    Every target code is paired with the CLOSEST price token below it in
    roughly the same column (20-unit horizontal tolerance -- keeps a code
    from ever pairing with an adjacent column's price in the same physical
    row) -- no fixed vertical distance cap, unlike
    parse_file_varaschini_composizione_tavoli's 30-unit ceiling: the
    code->price gap is NOT constant across this collection's pages (~38
    units on p567's first 3 row-groups, but confirmed 126 units on p568,
    whose rows have much bigger diagrams between a code and its price).
    "Closest in this column, whatever the distance" is safe here because
    every code has exactly one real next-price-below in its own column
    (never a puzzle with 2+ genuine candidates at similar depth -- verified
    across all of pages 558-569: adding this fixed a p567-tolerance-563/
    p568/p569 undercounts of 6-24 codes each, taking the collection from
    164/221 real prices to 218/221; the 3 remaining misses -- "3021",
    "220", "245" -- are a pre-existing, unrelated gap: multiple unlabeled
    prices with no tier markers, a materials-grid item neither this nor
    any other approach here can safely parse without guessing).
    """
    tokens = _varaschini_tsv_tokens(pdf_path)
    target_codes = {e["art_code"] for e in entries_for_page}

    euro_tokens = [(l, t, "al mq" if "/mq" in txt else None)
                   for l, t, txt in tokens if txt.startswith("€")]
    digit_tokens = [(l, t, txt) for l, t, txt in tokens
                     if txt != "2026" and VARASCHINI_TSV_PRICE_DIGIT_RE.match(txt)]
    prices = []
    for el, et, unit in euro_tokens:
        best = None
        for dl, dt, dtxt in digit_tokens:
            if abs(dt - et) <= 8 and 0 <= (dl - el) <= 45:
                if best is None or dl < best[0]:
                    best = (dl, dt, dtxt)
        if best:
            dl, dt, dtxt = best
            prices.append((el, dt, dtxt, unit))

    code_tokens = [(l, t, txt) for l, t, txt in tokens
                    if (VARASCHINI_CODE_TOKEN.match(txt) or VARASCHINI_9C5_CODE_TOKEN.match(txt))
                    and txt in target_codes]

    rows = []
    flags = []
    found_codes = set()
    for l, t, txt in code_tokens:
        if txt in found_codes:
            continue
        candidates = [(l2, t2, pr, unit) for l2, t2, pr, unit in prices if t2 > t and abs(l2 - l) <= 20]
        if not candidates:
            continue
        l2, t2, pr, unit = min(candidates, key=lambda c: c[1])

        found_codes.add(txt)
        entry = next((e for e in entries_for_page if e["art_code"] == txt), None)
        product_name = entry["product_name"] if entry else f"Teli di Copertura {txt}"
        rows.append({
            "brand": brand,
            "product_name": product_name,
            "model_variant": product_name,
            "variant_context": None,
            "size": None,
            "fabric_tier": unit,
            "tier_label": None,
            "code": txt,
            "price_eur": pr,
            "source_pdf_page": page_num,
        })

    for entry in entries_for_page:
        code = entry["art_code"]
        if code not in found_codes:
            flags.append((page_num, entry["product_name"],
                           f"art_code {code} not found or no own-price located via TSV coordinate pass on this page"))

    return rows, flags


def parse_file(path, product_name, brand, all_product_names=None):
    all_product_names = all_product_names or [product_name]
    # Sort longest-first so a name like "Poltroncina Jill" is preferred
    # over any shorter name that might also happen to prefix-match.
    other_names_sorted = sorted(
        (n for n in all_product_names if n and n != product_name),
        key=len, reverse=True
    )

    with open(path, encoding='utf-8') as f:
        lines = f.read().split('\n')

    # Pre-compute which PDF page is "in effect" at each line, using the
    # '<<<PDFPAGE:N>>>' markers extract_catalog.py embeds between pages.
    # This lets every row be tagged with its real source page, so later
    # (in the chat layer) we can show just the ONE relevant page image for
    # a specific model variant, instead of the whole product's page range.
    page_of_line = [None] * len(lines)
    current_page = None
    for idx, ln in enumerate(lines):
        m = re.match(r'^<<<PDFPAGE:(\d+)>>>$', ln.strip())
        if m:
            current_page = int(m.group(1))
        page_of_line[idx] = current_page

    rows = []
    for i, line in enumerate(lines):
        if not re.search(r'\bCodice\b', line):
            continue
        # This line may contain TWO tables side-by-side (common in this
        # catalog, e.g. "Comfort" and "Dream" mattresses printed on the same
        # spread). Split at each 'Codice' occurrence's character position,
        # and apply the same cut points to other related lines, since
        # -layout mode keeps columns aligned.
        split_points = [m.start() for m in re.finditer(r'\bCodice\b', line)]
        bounds = split_points + [len(line)]
        codice_chunks = [slice_chunk(line, bounds, c) for c in range(len(split_points))]

        # Decide whether this Codice line leads into a simple Prezzo row or
        # a fabric-tier table. Some pages interleave an UNRELATED row from a
        # neighboring side-by-side table between Codice and its real Prezzo
        # row (e.g. a leftover 'Peso colli (kg)' row from a different item
        # printed alongside). So instead of trusting only the very next
        # non-blank line, look ahead a few lines for whichever comes first:
        # a real 'Prezzo' line, or a row matching a known tier name.
        prezzo_line = None
        fabric_tier_mode = False
        tier_scan_start = None
        t = i + 1
        lookahead_steps = 0
        while t < len(lines) and lookahead_steps < 8:
            raw = lines[t]
            if raw.strip() == '':
                t += 1
                continue
            lookahead_steps += 1
            if re.search(r'\bPrezzo\b', raw):
                prezzo_line = raw
                break
            chunks = [slice_chunk(raw, bounds, c) for c in range(len(split_points))]
            if any(try_parse_tier_row(ch)[0] for ch in chunks):
                fabric_tier_mode = True
                tier_scan_start = t
                break
            t += 1

        if prezzo_line is not None:
            prezzo_chunks = [slice_chunk(prezzo_line, bounds, c) for c in range(len(split_points))]
        elif fabric_tier_mode:
            # PATTERN B: fabric-tier pricing. Several rows follow Codice,
            # each labelled with a known fabric category (e.g. 'B e TCL',
            # 'C', 'E', 'Plus', 'Extra', 'Luxury leather'), each giving that
            # size's price for that tier.
            tier_rows = []  # list of (tier_label, [value_chunks_per_table])
            t = tier_scan_start
            blank_run = 0
            steps = 0
            stop_words = ('maggiorazione', 'metraggio', 'finiture', 'descrizione',
                          'dimensioni', 'note', 'lavorazioni')
            while t < len(lines) and blank_run < 4 and steps < 40:
                raw = lines[t]
                if raw.strip() == '':
                    blank_run += 1
                    t += 1
                    continue
                blank_run = 0
                if raw.strip().lower().startswith(stop_words):
                    break
                chunks = [slice_chunk(raw, bounds, c) for c in range(len(split_points))]
                parsed_chunks = [try_parse_tier_row(ch) for ch in chunks]
                if any(label for label, _ in parsed_chunks):
                    tier_rows.append(parsed_chunks)
                t += 1
                steps += 1
        else:
            # Neither a Prezzo row nor a recognizable tier row found nearby --
            # this 'Codice' mention doesn't lead to a parseable price table
            # (e.g. a stray reference elsewhere in the text). Skip it.
            continue

        # Search upward for the size/dimension header row. Prefer the real
        # 'Materasso' (mattress size) row if present -- but tolerate several
        # interleaved noise lines to find it (an 'Ingombro' overall-
        # dimensions row, and/or stray leaked text from a neighboring
        # accessory table sharing the same dense two-column page).
        # If no 'Materasso' row turns up nearby, fall back to 'Ingombro'
        # itself -- most non-bed accessories (poufs, side tables, benches,
        # mirrors, night tables) never have a Materasso row at all and use
        # Ingombro as their only/primary size field.
        header_line = ''
        fallback_ingombro_line = ''
        k = i - 1
        header_search_steps = 0
        while k >= 0 and header_search_steps < 6:
            cand = lines[k]
            if cand.strip() == '':
                k -= 1
                continue
            header_search_steps += 1
            if re.match(r'^\s*Materasso\b', cand):
                header_line = cand
                break
            if not fallback_ingombro_line and re.match(r'^\s*Ingombro\b', cand):
                fallback_ingombro_line = cand
            k -= 1
        if not header_line:
            header_line = fallback_ingombro_line
        header_chunks = [slice_chunk(header_line, bounds, c) for c in range(len(split_points))]

        # Dedicated search for the MODEL VARIANT heading -- the nearest
        # preceding line that literally starts with this product's own
        # name (e.g. "Cameo Maison h.7" vs "Cameo Maison h.29", or
        # "Jack / Struttura in ferro", or "Awase - basamento box
        # contenitore"). These headings reliably repeat the product name
        # as a prefix across the whole catalog, which makes this a much
        # more trustworthy signal than the generic variant_context
        # heuristic below for distinguishing real sub-variants that share
        # a page (different heights, frame materials, etc).
        model_variant = None
        mv_prefix = product_name.strip()
        if mv_prefix:
            mv = i - 1
            mv_steps = 0
            while mv >= 0 and mv_steps < 30:
                cand = lines[mv].strip()
                if cand:
                    mv_steps += 1
                    if cand.startswith(mv_prefix):
                        # take just the heading itself, not any trailing
                        # content from a side-by-side second table on the
                        # same line (cut at 2+ spaces, which separates
                        # columns in -layout mode)
                        core = re.split(r'\s{2,}', cand)[0].strip()
                        # if there's a "/ subtitle" or "- subtitle" AND
                        # what's before it already distinguishes this
                        # variant on its own (e.g. "Cameo Maison h.29 /
                        # Struttura..." -> the "h.29" already does the
                        # job), drop the subtitle for consistency. But
                        # keep it when the qualifier IS the subtitle
                        # (e.g. "Jack / Struttura in ferro" -- "Jack"
                        # alone wouldn't distinguish it from the base
                        # variant, so the subtitle is the only signal).
                        split_core = re.split(r'\s+[/-]\s+', core, maxsplit=1)
                        if len(split_core) > 1 and split_core[0].strip() != mv_prefix:
                            core = split_core[0].strip()
                        model_variant = core
                        break
                mv -= 1

        # Some products print the SAME generic heading (e.g. "Face /
        # Struttura Legno Massello FSC") repeatedly across multiple pages,
        # each time paired with a DIFFERENT specific base-height section
        # (h.8, h.21, h.27...) -- and each pairing has its own genuinely
        # different codes and prices, even though the heading text is
        # identical every time. If the resolved heading doesn't already
        # contain a height/model number of its own, search the SAME PDF
        # page (using the page markers extract_catalog.py embeds) for the
        # nearest "h.N" qualifier and append it, so these don't all
        # silently collapse into one merged (and effectively random)
        # variant.
        if model_variant and not re.search(r'h\.?\s*\d+', model_variant, re.IGNORECASE):
            this_page = page_of_line[i]
            if this_page is not None:
                for idx in range(i, -1, -1):
                    if page_of_line[idx] != this_page:
                        break
                    if 'basamento' not in lines[idx].lower():
                        continue
                    hm = re.search(r'\bh\.?\s*(\d+)\b', lines[idx], re.IGNORECASE)
                    if hm:
                        model_variant = f"{model_variant} (h.{hm.group(1)})"
                        break

        # nearest preceding "variant context" line (product sub-name / description),
        # searched a bit further up, same chunking applied
        m = k - 1
        context_line = ''
        skip_labels = {'materasso', 'ingombro'}
        steps = 0
        while m >= 0 and steps < 12:
            cand = lines[m].strip()
            if cand and cand.lower() not in skip_labels and not re.match(r'^[\d,.\sx-]+$', cand) \
               and 'codice' not in cand.lower() and 'prezzo' not in cand.lower():
                context_line = lines[m]
                break
            m -= 1
            steps += 1
        context_chunks = [slice_chunk(context_line, bounds, c) for c in range(len(split_points))]

        # Ownership check: two ENTIRELY DIFFERENT named products can share
        # one PDF page side-by-side on the same lines (e.g. "Poltrona e
        # accessori Flag" next to "Poltroncina Jill"), each with its own
        # Codice/price table in its own column. Without this check, BOTH
        # columns would get stamped with whichever product_name this file
        # happens to be for -- silently mixing in the other product's real
        # prices as if they were this one's.
        #
        # This check ONLY runs when there are genuinely multiple tables
        # sharing one line (chunk-sliced comparison is meaningful there,
        # since both tables' columns are aligned on that line). It does
        # NOT attempt to handle denser multi-item grid layouts (e.g. four
        # small items crammed onto one spread) -- an earlier attempt to
        # generalize this to the single-table case caused widespread false
        # exclusions across many unrelated products (verified: dropped
        # catalog-wide rows from ~14,600 to ~11,700). Those grid-layout
        # pages are handled via manual_additions.json instead, which is
        # safer than risking another broad regression.
        chunk_belongs_to_me = [True] * len(split_points)
        if len(other_names_sorted) > 0 and len(split_points) > 1:
            my_name_re = re.compile(re.escape(product_name.strip()) + r'(?![a-zA-Z0-9])')
            other_name_res = [(n, re.compile(re.escape(n) + r'(?![a-zA-Z0-9])')) for n in other_names_sorted]
            for c in range(len(split_points)):
                own = i - 1
                own_steps = 0
                while own >= 0 and own_steps < 30:
                    cand_full = lines[own]
                    if cand_full.strip():
                        own_steps += 1
                        cand_chunk = slice_chunk(cand_full, bounds, c).strip()
                        if cand_chunk:
                            if my_name_re.match(cand_chunk):
                                break  # this column is mine, as expected
                            other_hit = next((n for n, rx in other_name_res if rx.match(cand_chunk)), None)
                            if other_hit:
                                chunk_belongs_to_me[c] = False
                                break
                    own -= 1

        for c in range(len(split_points)):
            if not chunk_belongs_to_me[c]:
                continue
            codes = tokenize_chunk(codice_chunks[c].replace('Codice', '', 1))
            sizes = tokenize_size_chunk(re.sub(r'^\s*(Materasso|Ingombro)\b', '', header_chunks[c])) if header_chunks[c] else []
            context = context_chunks[c].strip() or None

            if not fabric_tier_mode:
                prices = tokenize_chunk(prezzo_chunks[c].replace('Prezzo', '', 1))
                n = min(len(codes), len(prices))
                for idx in range(n):
                    code, price = codes[idx].strip(), prices[idx].strip()
                    size = sizes[idx].strip() if idx < len(sizes) else None
                    if code and code != '-' and price and price != '-' and re.search(r'\d', price):
                        rows.append({
                            "brand": brand,
                            "product_name": product_name,
                            "model_variant": model_variant,
                            "variant_context": context,
                            "size": size,
                            "fabric_tier": None,
                            "tier_label": None,
                            "code": code,
                            "price_eur": price,
                            "source_pdf_page": page_of_line[i],
                        })
            else:
                # one code per size (column), but price depends on which
                # fabric tier row this size's value comes from
                for parsed_row in tier_rows:
                    label, values = parsed_row[c]
                    if not label:
                        continue
                    n = min(len(codes), len(values))
                    for idx in range(n):
                        code, price = codes[idx].strip(), values[idx].strip()
                        size = sizes[idx].strip() if idx < len(sizes) else None
                        if code and code != '-' and price and price != '-' and re.search(r'\d', price):
                            rows.append({
                                "brand": brand,
                                "product_name": product_name,
                                "model_variant": model_variant,
                                "variant_context": context,
                                "size": size,
                                "fabric_tier": label,
                                # Unlike Cattelan's format, Bolzan's price
                                # tables have no separate category-word
                                # header at all above the tier rows -- just
                                # the tier value itself ("B e TCL"/"Plus"/
                                # "Extra", straight from KNOWN_TIERS) with
                                # nothing above it to capture. Confirmed by
                                # reading Bolzan's raw source text directly
                                # (Ceylon, a bed): no "Base"/"Top"/
                                # "Rivestimento" word anywhere near the
                                # table. Bolzan is exclusively upholstered
                                # furniture (beds/sofas/seating), so
                                # "FABRIC" -- the chat UI's existing
                                # hardcoded column header, used whenever
                                # tier_label is None -- is already accurate
                                # here; this is not an oversight to fill in.
                                "tier_label": None,
                                "code": code,
                                "price_eur": price,
                                "source_pdf_page": page_of_line[i],
                            })
    return rows


# ---------------------------------------------------------------------------
# Ditre Italia -- two confirmed price-table shapes (see the extraction-side
# step-2 design conversation for the full survey): Shape 1 is a graduated
# upholstery-category tier grid (sofas, armchairs, beds, cushions, outdoor
# seating); Shape 2 is a named material-finish-code grid (tables,
# sideboards, mirrors, carpets, bookcase). Both share the SAME underlying
# page layout -- a bare SKU-code header line with N codes side by side
# (2-4 confirmed so far), no "Codice:" prefix at all (unlike Bolzan), one
# short descriptive line, a "W..cm D..cm H..cm" dims line, a "Vol..." line,
# then a stack of "Label.......Value €" rows -- so the multi-column
# splitting and the model_variant/size extraction are shared; only the
# PRICE-ROW matching rule (a fixed tier whitelist vs. a "Name (CODE)"
# pattern) and the resulting tier_label differ.
#
# THIS PASS covers exactly the two products verified end-to-end against
# their real rendered pages: "Ada (Sofa)" for Shape 1, "Claire (Tables)"
# for Shape 2. Confirmed real variants NOT yet in scope here (deliberately
# deferred to the "generalize to the rest of the shape" pass, not
# fabricated ahead of verifying them against their own pages): outdoor's
# "Category P/T/U/V outdoor" tier vocabulary (no Leather tiers), beds'
# "Cod. XXXX" header prefix and "Leather Top" (vs "Leather Maxi - Top")
# label, Cut armchair's "Mix by Leather..." extended tier ladder, and any
# Shape-2 product whose named finishes are NOT flat-priced across a whole
# row the way Claire's are (Arcade's marble finishes, sampled during
# structural review, have a DIFFERENT price per finish, not one price for
# the whole row like Claire -- both still fit this same row-matching rule
# since it's per-cell, not per-row, but this hasn't been verified yet).

# A Ditre SKU code as printed on its own header line -- two confirmed
# forms: bare (e.g. "ADAXU1RQ0", "CLAIJTA01", "CALIP1000" -- sofas,
# armchairs, tables) and "Cod. XXXXX"-prefixed (e.g. "Cod. MD0G3" --
# beds). Length 4-10 confirmed across every sampled code.
#
# Digit requirement: a real code USUALLY contains a digit, but confirmed
# NOT always -- sideboard finish codes like "ANGLJMAGW"/"ANGLJMAGB" are
# pure letters (found on Angle, a Shape 2 sideboard; requiring a digit
# produced 0 codes found / 0 flags there, total silence). Simply
# dropping the digit requirement is ALSO confirmed wrong on its own: a
# lone all-letter word is indistinguishable from a genuine English/
# French section-header word that happens to land alone on its own
# line in this catalog's multi-column spec-page layout (confirmed real
# false positives: "REVETEMENT" on Ada's spec page, "FINITIONS" on
# Claire's -- the second one silently attached real nearby price rows
# to a phantom SKU, not just an empty flag). A maintainable-length
# denylist of category-badge words doesn't fully solve this either --
# there's no reason to believe REVETEMENT/FINITIONS are the only such
# words across 197 products' spec pages.
#
# The rule that actually holds: a digit is required UNLESS the line has
# 2+ code-shaped tokens. Every confirmed genuine multi-column SKU header
# (2-4 codes side by side, e.g. Angle's 4 finish codes) has multiple
# tokens; every confirmed false positive found so far is exactly ONE
# isolated word. See _ditre_header_codes, which applies this rule --
# the two regexes below only handle token SHAPE, not this line-level
# multi-token exception.
#
# First character: also confirmed NOT always a letter -- "356" (an
# Armchairs-family base product, no descriptive suffix, distinct from
# its own "356 - Archie"/"356 woven outdoor"/etc. siblings which all use
# normal letter-first codes) prints digit-first codes "356XP1000"/
# "356XP1N00", which the letter-first-only pattern silently matched
# zero times -- 0 rows, 0 flags, no error, found only by noticing this
# was the catalog's one purely-numeric product name and checking its
# real page directly (it has a full Category A-U/Leather price grid,
# not a legitimately-empty page like The breath/Bed-base cover). Same
# mitigation as the pure-letter case applies symmetrically: see
# _ditre_header_codes' dims-lookahead check, now triggered for a
# single-token candidate that's ALL digits too, not just all letters.
_DITRE_SKU_CODE_RE = re.compile(r'\b(?:Cod\.\s*)?([A-Z0-9]{4,10})\b')

# A code-header line has ONLY code tokens on it (nothing else) -- this is
# what distinguishes a genuine SKU-code header row from a line that merely
# contains a code-shaped substring elsewhere. The \b after the code
# pattern is required, not decorative: without it, the repeated group
# (whitespace between repetitions is \s*, i.e. OPTIONAL) can silently
# subdivide one long all-caps word into multiple fake back-to-back
# "codes" with no gap at all -- confirmed real: "INFORMATION" (11
# chars, one char over the 10-char cap) let the whole line "TECHNICAL
# INFORMATION ... INFORMATIONS TECHNIQUES" match by splitting it into
# arbitrary 4-10-char chunks, since nothing required a genuine word
# boundary between one matched chunk and the next.
# A reversible dual-SKU pair token, e.g. "<- MONOL2000S - MONOL2000D ->"
# (still explicitly deferred -- not parsed into its own block, see
# parse_file_ditre's docstring). Real headers mix these WITH plain bare
# codes on one line -- confirmed on 33 products (140 codes): Monolith's
# own "<- MONOL1000S - MONOL1000D -> <- MONOL100MS - MONOL100MD ->
# MONOC1000 MONOC100M" has 2 perfectly ordinary bare codes (MONOC1000/
# MONOC100M) sharing a line with 2 reversible pairs. Before this, the
# bracket syntax made the WHOLE line fail _DITRE_CODE_HEADER_LINE_RE
# (nothing in it looks like a bare code-shaped token), so _ditre_header_
# codes returned [] for the entire line -- silently 0-rowing MONOC1000/
# MONOC100M (no bare-code header ever recognized there) AND, separately,
# letting an in-progress scan for an EARLIER, unrelated code silently
# bleed straight through it and misattribute the reversible pair's own
# price rows to that earlier code (confirmed: Monolith's MONOU1000, a
# legitimate code on an earlier page, picked up a genuine MONOL300MD
# price under a garbled label -- fixed as a side effect of THIS change,
# since the line now correctly registers as a header and stops the
# scan, rather than needing a separate stop-condition rule; an earlier
# attempt at that broke Pacific (Sofa)'s ambiguous-safety-net scan-
# through on PURE reversible-pair-only pages, which have no bare code
# on the same line at all -- this fix only changes recognition for
# lines that DO mix in a bare code, so that safety net is untouched).
# Deliberately re-scoped and re-verified against the range-format-dims
# fix landing first (see that commit) -- 5 of the 33 affected products
# also hit that separate bug, and a same-session attempt at this exact
# fix, before the dims fix existed, was found (via a random-sample
# re-verification) to collapse one of them (Blum) from a safely
# ambiguous 3-value conflict down to a single, confidently WRONG value
# -- reverted at the time specifically because of that interaction, not
# because this fix was wrong on its own.
_DITRE_REVERSIBLE_PAIR_RE = re.compile(
    r'<-\s*[A-Z0-9]{4,10}\s*-\s*[A-Z0-9]{4,10}\s*->'
)
_DITRE_CODE_HEADER_LINE_RE = re.compile(
    r'^\s*(?:(?:(?:Cod\.\s*)?\b[A-Z0-9]{4,10}\b)|(?:<-\s*[A-Z0-9]{4,10}\s*-\s*[A-Z0-9]{4,10}\s*->))'
    r'(?:\s+(?:(?:(?:Cod\.\s*)?\b[A-Z0-9]{4,10}\b)|(?:<-\s*[A-Z0-9]{4,10}\s*-\s*[A-Z0-9]{4,10}\s*->)))*\s*$'
)


def _ditre_header_codes(lines, i):
    """Return [(pos, code), ...] if lines[i] is a genuine Ditre SKU-code
    header line, else []. See the module comment above _DITRE_SKU_CODE_RE
    for the digit-unless-multi-token rule this applies -- a single,
    digit-less token (e.g. a real accessory code like "ANGLJMARP",
    confirmed on Angle) is ambiguous against a stray spec-page section-
    header word (e.g. "REVETEMENT"/"FINITIONS", also confirmed real
    false positives), and the token's own letters alone can't tell them
    apart. Resolved empirically instead: a genuine header, even a
    single-code one, is always followed within a few lines by a real
    dims line ("W..cm.." or bare "Ncm.."); an isolated section-header
    word never is. Needs `lines`/`i` (not just the one line string) to
    do that lookahead, which is why this takes the whole list + index
    rather than a bare line like earlier callers assumed. The same
    lookahead now also gates a single ALL-DIGIT token (e.g. a stray
    page/footnote number) -- symmetric risk to the all-letter case,
    since digit-first codes like "356XP1000" are now valid too (see
    _DITRE_SKU_CODE_RE's comment)."""
    line = lines[i]
    if not line.strip() or not _DITRE_CODE_HEADER_LINE_RE.match(line):
        return []

    # "Composition N" bundle-listing guard. Confirmed real, silently-
    # WRONG-data case on Tao outdoor: a composition's component codes
    # (each a real code, genuinely priced elsewhere on its OWN page)
    # get listed again, several per row, directly under a "Composition
    # N" label -- code-shaped, multi-token, digits present, passing
    # every other check -- and this function then (wrongly) scans
    # forward from there, finds the COMPOSITION's own overall dims/
    # price block, and misattributes it to whichever component code
    # happened to be sitting nearest, including truncated/duplicated
    # rows from column-bounds computed against the wrong line. The
    # discriminator: Ada's OWN (legitimate) numbered compositions print
    # "Composition n°N" AFTER their own real bare-code header
    # (ADAXK0001 first, "Composition n°1" second) -- Tao's spurious
    # case has the order reversed, "Composition N" BEFORE the listing.
    # Rejecting any candidate immediately preceded by a "Composition"
    # line (skipping blanks) catches Tao's case without touching Ada's.
    # Checks the WHOLE lookback window, not just the nearest non-blank
    # line -- Tao's composition component listing stacks several rows
    # of codes (each itself passing every other check) before reaching
    # the actual "Composition N" label further back, so stopping at the
    # first non-blank line here would only catch the row closest to the
    # label, not the ones further down the list (confirmed real: this
    # missed line 448's "OC1000 OC1000 OT10B0D OA1Q00" row on the first
    # version of this guard, which only checked line 447 immediately
    # above it -- also a component row, not the label itself).
    for k in range(i - 1, max(i - 16, -1), -1):
        if re.match(r'^\s*Composition\b', lines[k], re.IGNORECASE):
            return []

    matches = list(_DITRE_SKU_CODE_RE.finditer(line))
    # Drop any match that falls inside a reversible-pair bracket -- those
    # are the (still deferred) pair's own two codes, not standalone bare
    # codes to scan a price block for. See _DITRE_REVERSIBLE_PAIR_RE's
    # own comment for why the line as a whole is still allowed to match
    # as a header despite containing one.
    reversible_spans = [m.span() for m in _DITRE_REVERSIBLE_PAIR_RE.finditer(line)]
    if reversible_spans:
        matches = [m for m in matches if not any(s <= m.start() < e for s, e in reversible_spans)]
        if not matches:
            return []
    if len(matches) == 1 and (not re.search(r'\d', matches[0].group(1)) or not re.search(r'[A-Z]', matches[0].group(1))):
        found_dims = False
        for k in range(i + 1, min(i + 12, len(lines))):
            cand = lines[k]
            if not cand.strip():
                continue
            if _DITRE_DIMS_LINE_RE.match(cand.strip()):
                found_dims = True
                break
            if _ditre_header_codes(lines, k):
                break  # hit the next header first -- give up
        if not found_dims:
            return []
    return [(m.start(), m.group(1)) for m in matches]

# Shape 1: known real upholstery-category tier labels, confirmed present
# verbatim in Ada (Sofa)'s own extracted text (data/Ditre Italia/text/
# ada_sofa.txt) -- NOT the full vocabulary for the whole shape (see module
# comment above for the confirmed-elsewhere variants deliberately left out
# for now). The "Mix by Leather..." ladder WAS added here despite that --
# it's not deferred, unlike the outdoor/bed/Cut-armchair variants -- because
# it's confirmed on Ada's OWN pages (Ada back cushions MIX, printed pages
# 15-16: "Mix by Leather Soft and cust.fabric/Cat.A/E-L/M/P/T/U", then
# "Mix by Leather Maxi - Top/Luxor/Premium/Vip"), i.e. real text from the
# one product this pass is scoped to verify, not borrowed from Cut
# armchair's screenshot (that was only the earlier hint this ladder
# existed at all, confirmed independently here against Ada's real page).
DITRE_UPHOLSTERY_TIERS = {
    "Customer's fabric", "Category A", "Category E-L", "Category M",
    "Category P", "Category T", "Category U",
    "Leather Soft", "Leather Maxi - Top", "Leather Luxor",
    "Leather Premium", "Leather Vip",
    # Outdoor tier vocabulary (no Leather tiers) -- confirmed verbatim on
    # Tao outdoor's real text (data/Ditre Italia/text/tao_outdoor.txt):
    # "Category P/T/U/V outdoor", same "Customer's fabric" leading row.
    "Category P outdoor", "Category T outdoor", "Category U outdoor",
    "Category V outdoor",
    # Beds use "Leather Top" instead of sofa/armchair's "Leather Maxi -
    # Top" -- confirmed verbatim on Bend (Night)'s real text
    # (data/Ditre Italia/text/bend_night.txt line 104: "Leather Top
    # 4.251,00 €"). Flagged as a confirmed-elsewhere variant during
    # structural review; added now with real text from the product it
    # actually appears on, not the earlier screenshot alone.
    "Leather Top",
}

# "Mix by Leather <base> and/of <fill>" tier labels are handled as a PREFIX
# rule ("Mix by Leather") rather than exact whitelist entries. Originally
# enumerated as exact strings from Ada's own page ("Mix by Leather Soft and
# cust.fabric/Cat.A/.../Cat.U", "Mix by Leather Maxi - Top/Luxor/Premium/
# Vip") -- confirmed too narrow via a random 18-product spot-check sample
# this session (Ada/Claire's own deep verification never exercised any OTHER
# product's Mix ladder). At least 3 more real wording variants exist across
# other products, none matching Ada's exact strings: "Mix by Leather
# Premium/Cat.A" (slash, abbreviated -- Alta mix, Alta - Alta mix), "Mix by
# Leather Soft and Category A" (full "Category", not "Cat." -- Bend mix,
# Bliss - Viga, Claire mix, Kailua mix, Kanaha Mix, Kanaha mix 2.0 sofa bed,
# Kim mix, Papilo mix), "Mix by Leather Premium and Category A" (Pacific
# mix, Skin mix), and "Mix by Leather Luxor and Category A" (Blazer, this
# one ALSO wrapped across 2-3 lines -- see _ditre_scan_price_rows' pending-
# label buffer). All silently 0-rowed with no flag: the code itself still
# matched (whitelist only gates ROWS, not the header/code), so no "no price
# rows found for code X" flag ever fired -- these looked complete. A
# maintainable exact-string list can't keep up with base-leather-type (Soft/
# Premium/Luxor) x separator (and/of//) x abbreviation (Cat./Category)
# combinations that keep appearing per-product; "Mix by Leather" itself is
# specific enough that a false-positive prefix match is not a real risk
# (confirmed: grepped every file mentioning the phrase catalog-wide, the
# only non-price-line occurrences are Blazer's own wrapped label text, not
# unrelated content).
_DITRE_MIX_LABEL_PREFIX = "Mix by Leather"

# A second, differently-shaped wrapped-label trigger: "Surcharge for
# Move mechanism" (Freedom S/M/L/XL, Isabel sofa bed, Kanaha 2.0 sofa
# bed, Kanaha mix 2.0 sofa bed, Lulu' 2.0 sofa bed) wraps across 3
# label-only lines before its price appears alone on a 4th ("Only
# suitable for sofa." then ".                € 211,00") -- confirmed
# real, silently 0-rowed with no flag on all 8 products (same value,
# 211,00, on every one). Reuses the exact same pending_label mechanism
# as _DITRE_MIX_LABEL_PREFIX, just a different starting phrase.
_DITRE_SURCHARGE_LABEL_PREFIX = "Surcharge for"


def _ditre_is_whitelisted_upholstery_tier(label: str) -> bool:
    return label in DITRE_UPHOLSTERY_TIERS or label.startswith(_DITRE_MIX_LABEL_PREFIX)


def _ditre_label_looks_clean(label: str) -> bool:
    """Reject a tier/finish label whose first character isn't an uppercase
    letter or a digit -- a genuine Ditre label always starts with one of
    those (confirmed across every product checked this session: "Category
    A", "Customer's fabric", "Mix by Leather...", "150 Beige..." on the one
    digit-led carpet-shade product). A lowercase first character is the
    signature of a column-bounds-off-by-one slice eating the label's own
    first character(s) -- confirmed real and NOT cosmetic-only in every
    case: 88 such rows already existed in the shipped catalog before this
    fix (found via a random-sample re-verification, not proactively), ~40
    of them a genuine cross-CODE price misattribution (e.g. Monolith's
    MONOU1000 -- an unrelated, correct code -- picking up a mangled
    "ategory A" row whose price actually belongs to a totally different
    code, MONOL300MD, several pages later). A handful of the rejected rows
    turn out to be a correct value on the correct code with just a
    truncated label (e.g. Kevin's "piètement Étain (ME13)" surcharge) --
    accepted as collateral loss rather than building unverified logic to
    save a few rows, consistent with this project's standing rule that
    silently wrong data outranks missing data."""
    return bool(label) and (label[0].isupper() or label[0].isdigit())

# Shape 1 row: "TierName.......Value €" or "TierName   Value €" (both
# confirmed real -- Ada/Cali use a dotted leader, the one Night bed sample
# checked during structural review uses a plain 2+-space gap instead, same
# logical row). Label capture is non-greedy so it stops at the FIRST
# qualifying gap, not swallowing the single internal spaces/hyphen in
# labels like "Category E-L" or "Leather Maxi - Top".
#
# Currency-symbol position: normally trailing ("Value €"), but confirmed
# real as LEADING too ("€ Value") on beds' foot/leg accessory rows --
# e.g. Sommier "with black chrome tip      € 348,00" -- silently 0-rowed
# with no flag on 15 products (19 rows) since the trailing-only pattern
# never matched at all. `€?` before AND after the amount (both optional)
# covers either order in one capture group; the `(?=.*€)` lookahead
# requires "€" to appear SOMEWHERE in the remainder regardless of which
# side, so making both sides optional can't start silently matching a
# bare number with no currency symbol at all (a dimension, a weight).
_DITRE_UPHOLSTERY_ROW_RE = re.compile(
    r'^(.+?)(?:\.{2,}|\s{2,})\s*(?=.*€)€?\s*([\d.,]+)\s*€?\s*$'
)

# Shape 2 row: "Finish Name.......Value €" -- confirmed on Claire
# (Tables): "Natural (CR01)......... 4.319,00 €" (name + parenthetical
# code). Originally REQUIRED a "(CODE)" group as a self-validating
# anchor (named finishes vary per product, unlike Shape 1's fixed
# Category/Leather whitelist) -- confirmed WRONG on a carpet product
# (Buckle/Glint/Hertz/.../Reed): its rows are "150 Beige..... 3.716,00
# €", a leading numeric shade code with NO parentheses at all, which
# the "(CODE)" requirement rejected outright (0 rows, review-flagged
# for every SKU). Relaxed to the same generic "label, then dots-or-
# spaces, then price" shape as Shape 1's row regex -- block-scoping
# (this only ever runs between a detected code-header line and the
# next one/a blank-run cutoff) is the real safety net here, same as it
# already is for Shape 1's per-column matching; whatever text precedes
# the price on a matched line becomes fabric_tier verbatim, parens or
# not.
_DITRE_CASEGOODS_ROW_RE = re.compile(
    r'^(.+?)(?:\.{2,}|\s{2,})\s*(?=.*€)€?\s*([\d.,]+)\s*€?\s*$'
)

# The dims line ("W 140cm D 140cm H 73cm" on sofas/armchairs/tables, or
# bare "189cm 230cm 103cm" with no W/D/H letters at all on beds -- both
# confirmed real, positional W-then-D on both) is the one unambiguous,
# always-present marker between a SKU's descriptive text and its price
# rows -- used as the stop condition when scanning forward for
# model_variant text, and as the source for the compact "WxD" size string
# (pulled from real W/D values here rather than depending on an
# inconsistently-present short-form label line -- confirmed NOT always
# present, e.g. Ada's "back cushions" sub-pages have no "NNxNN"-labelled
# line at all near the code, only this dims line). The "W " prefix is
# optional specifically to cover the bed case; capturing every bare
# "Ncm" token positionally (first two = W, D) works for both forms
# without needing to special-case which one a given page uses.
#
# A value can ALSO be a hyphenated low-high RANGE ("W 200-230cm", a
# modular/reclining piece whose width varies by configuration) instead
# of one number -- confirmed real and previously unhandled: the
# original pattern required "cm" immediately after the digits, which a
# range never satisfies ("200-230cm" has a "-230" in between), so
# _ditre_scan_sku_block's forward search for a dims-line stopping point
# never matched at all and silently walked straight through the SKU's
# own price grid into the NEXT SKU's content -- confirmed exact repro:
# Blum's BLUMD2000 ("W 200-230cmD 90-113cm...") picked up BLUMU1000's
# ("Footstool", an unrelated later SKU) price values. The optional
# "(?:-[\d.,]+)?" after the first number covers the range form without
# disturbing the plain single-value form (still matches with nothing
# consumed by the new group). The captured value for a range keeps the
# WHOLE "low-high" string (not just one bound) -- confirmed the more
# useful choice: it's still a legitimate, informative size string
# ("200-230x90-113"), and choosing to keep only one bound would
# silently discard real information with no clear right answer for
# which bound "is" the size.
_DITRE_DIMS_LINE_RE = re.compile(r'^\s*(?:W\s*)?[\d.,]+(?:-[\d.,]+)?\s*cm')
_DITRE_DIMS_WD_VALUES_RE = re.compile(r'([\d.,]+(?:-[\d.,]+)?)\s*cm')

# Category badge words confirmed printed top-right on Ditre pages (SOFA,
# ARMCHAIRS, TABLES, ...) -- used only to reject a page TITLE line during
# the backward description search below, never to accept/skip anything
# else. Deliberately a substring-anywhere-in-the-line check, not an exact
# match, since the badge sits far right on the same physical line as the
# product name (e.g. "Avalon | Avalon ... " has no badge on ITS own line,
# but a line like "Ada base | Piètement Ada ... SOFA" does).
_DITRE_CATEGORY_BADGE_WORDS = (
    'SOFA', 'ARMCHAIRS', 'CHAIRS', 'TABLES', 'SIDEBOARDS', 'BOOKCASE',
    'MIRRORS', 'CARPETS', 'ACCESSORIES',
)


def _ditre_scan_sku_block(lines, i, bounds, n_cols):
    """Shared scan around a Shape 1/Shape 2 code-header line at index i:
    finds the descriptive line (model_variant candidate) and the dims
    line ("W..cm D..cm H..cm", or the bed form with no W/D letters),
    returning (model_chunks, size_chunks, price_scan_start_index).

    Two confirmed, DIFFERENT description positions: sofas/armchairs/
    tables print it AFTER the code line (forward scan, e.g. Ada's "100
    ADAXU1RQ0..." then "103  82x82 base..."); beds print it BEFORE the
    code line instead (e.g. Avalon's "66  Double size bed..." then "70
    Cod. MD0G3..."). Forward scan runs first (unchanged from the
    verified Ada/Claire behavior); if it lands on content that's empty
    or purely numeric per column (confirmed real case: beds' diagram-
    callout-number line, e.g. a lone "189", sitting between the code
    line and the real dims line), that candidate is discarded and a
    backward scan from the code line is tried instead, skipping the
    page's own title line (identified by a category badge word
    elsewhere on that line, e.g. "... SOFA") since that's shared across
    the WHOLE page, not specific to this one SKU block.

    size_chunks is all-blank if no dims line was found within the
    lookahead window (degrades to size=None per SKU rather than
    blocking price extraction on missing metadata)."""
    model_variant_line = None
    dims_line = None
    t = i + 1
    steps = 0
    while t < len(lines) and steps < 20:
        raw = lines[t]
        if raw.strip():
            steps += 1
            first_chunk = slice_chunk(raw, bounds, 0).strip()
            if _DITRE_DIMS_LINE_RE.match(first_chunk):
                dims_line = raw
                t += 1
                break
            if model_variant_line is None and not _ditre_header_codes(lines, t):
                model_variant_line = raw
        t += 1

    def _chunks_look_real(line):
        if not line:
            return False
        for c in range(n_cols):
            ch = slice_chunk(line, bounds, c).strip()
            if ch and not re.fullmatch(r'[\d.,]+', ch):
                return True
        return False

    if not _chunks_look_real(model_variant_line):
        k = i - 1
        back_steps = 0
        while k >= 0 and back_steps < 6:
            cand = lines[k]
            if cand.strip():
                back_steps += 1
                if _ditre_header_codes(lines, k):
                    break  # previous SKU block's own header -- stop
                if any(w in cand.upper() for w in _DITRE_CATEGORY_BADGE_WORDS):
                    k -= 1
                    continue  # page title line, not this block's own description
                if _chunks_look_real(cand):
                    model_variant_line = cand
                    break
            k -= 1

    model_chunks = [slice_chunk(model_variant_line, bounds, c) for c in range(n_cols)] \
        if model_variant_line else [''] * n_cols
    size_chunks = [slice_chunk(dims_line, bounds, c) for c in range(n_cols)] \
        if dims_line else [''] * n_cols
    return model_chunks, size_chunks, t


def _ditre_scan_price_rows(lines, scan_start, bounds, n_cols, row_re, label_filter=None):
    """Shared forward-scan for "Label[...](.......|\\s\\s+)Value €" rows
    across N side-by-side columns, starting at scan_start, stopping at
    the next code-header line or 6 consecutive blank lines. 3 was tried
    first (verified against Ada, whose real price rows are never
    separated by more than 1 blank line) but confirmed too tight on
    beds: Bend (Night) prints a "Price / Prix €" column-header row
    separated from the Vol. line by 5 consecutive blanks before the
    real price rows start, which a 3-blank cutoff exits before ever
    reaching. Raising the threshold doesn't risk bleeding into the
    NEXT SKU block's own price rows even when its own preceding gap is
    shorter (confirmed 4 blanks between Bend's blocks) -- the
    next-code-header-line check above already stops the scan
    unconditionally as soon as it's reached, regardless of blank_run,
    so that block boundary is enforced either way. row_re must have its
    price value as the LAST capture group. label_filter(label) -> bool
    decides whether a matched label is kept (the Shape 1 whitelist) or
    None to accept any (Shape 2, which has no whitelist -- named
    finishes vary per product; block-scoping is its safety net instead).

    Wrapped labels: a long "Mix by Leather <base> and Category <X>" label
    can run out of column width and wrap onto 1-2 continuation lines
    before the price appears alone on its own line further down --
    confirmed real on Blazer ("Mix by Leather Luxor and customer's" /
    "fabric..." / "3.029,00 €" across 3 separate lines, same column).
    row_re only ever matches a single line, so these were silently 0-
    rowed. pending_label buffers an in-progress wrapped label PER COLUMN,
    started only when a chunk looks like the start of a real "Mix by
    Leather" label with no price yet -- narrowly scoped to this one
    confirmed pattern rather than treating any unmatched text as a label
    fragment (which would risk gluing unrelated spec-page text onto a
    later, unrelated price).
    Returns {col_index: [(label, price), ...]}."""
    collected = {c: [] for c in range(n_cols)}
    pending_label = {c: None for c in range(n_cols)}
    # Optional leading "." (a stray separator/placeholder character
    # sometimes precedes the price on its own final wrapped line -- e.g.
    # Sommier's "Surcharge for Move mechanism" row prints its price as
    # ".                € 211,00", not just "€ 211,00") and either
    # currency-symbol order, same reasoning as _DITRE_UPHOLSTERY_ROW_RE.
    price_only_re = re.compile(r'^\.?\s*(?=.*€)€?\s*([\d.,]+)\s*€?\s*$')
    t = scan_start
    blank_run = 0
    while t < len(lines) and blank_run < 6:
        raw = lines[t]
        if raw.strip() == '':
            blank_run += 1
            t += 1
            continue
        if _ditre_header_codes(lines, t):
            break
        blank_run = 0
        for c in range(n_cols):
            ch = slice_chunk(raw, bounds, c).strip()
            if ch in ('', '.', '-'):
                continue  # blank / separator / "not available" filler
            if pending_label[c] is not None and row_re.match(ch) is None:
                # Only treat this line as part of the wrap if it can't
                # independently stand as its own complete row. Confirmed
                # necessary on Petra (Sideboards): "Surcharge for TV
                # cable port" (no price, triggers the buffer) is
                # immediately followed by its OWN separate, complete,
                # already-working row "TV...............230,00 €" --
                # without this check, that real row's price/label get
                # silently swallowed as "more wrapped-label text" that
                # never resolves (pending_label just dangles and is
                # dropped at the end of the scan).
                pm = price_only_re.match(ch)
                if pm:
                    label = pending_label[c]
                    pending_label[c] = None
                    if _ditre_label_looks_clean(label) and (label_filter is None or label_filter(label)):
                        collected[c].append((label, pm.group(1).strip()))
                else:
                    frag = re.sub(r'\.+$', '', ch).strip()
                    pending_label[c] = f"{pending_label[c]} {frag}".strip()
                continue
            pending_label[c] = None
            m = row_re.match(ch)
            if not m:
                if '€' not in ch and (ch.startswith(_DITRE_MIX_LABEL_PREFIX) or ch.startswith(_DITRE_SURCHARGE_LABEL_PREFIX)):
                    pending_label[c] = re.sub(r'\.+$', '', ch).strip()
                continue
            *label_groups, price = m.groups()
            label = ' '.join(g.strip() for g in label_groups if g).strip()
            label = re.sub(r'\.$', '', label).strip()
            if not _ditre_label_looks_clean(label):
                continue
            if label_filter is not None and not label_filter(label):
                continue
            collected[c].append((label, price.strip()))
        t += 1
    return collected


def parse_file_ditre_upholstery(path, product_name, brand, all_headings=None, heading_text=None):
    """Shape 1: graduated upholstery-category tier grid. See this
    section's module comment for scope (verified against Ada (Sofa) only
    so far)."""
    with open(path, encoding='utf-8') as f:
        lines = f.read().split('\n')

    page_of_line = [None] * len(lines)
    current_page = None
    for idx, ln in enumerate(lines):
        m = re.match(r'^<<<PDFPAGE:(\d+)>>>$', ln.strip())
        if m:
            current_page = int(m.group(1))
        page_of_line[idx] = current_page

    rows = []
    flags = []
    for i, line in enumerate(lines):
        codes = _ditre_header_codes(lines, i)
        if not codes:
            continue
        bounds = [pos for pos, _ in codes] + [len(line)]
        n_cols = len(codes)

        model_chunks, size_chunks, scan_start = _ditre_scan_sku_block(lines, i, bounds, n_cols)
        collected = _ditre_scan_price_rows(
            lines, scan_start, bounds, n_cols, _DITRE_UPHOLSTERY_ROW_RE,
            label_filter=_ditre_is_whitelisted_upholstery_tier,
        )

        for c, (_, code) in enumerate(codes):
            size = None
            wd_values = _DITRE_DIMS_WD_VALUES_RE.findall(size_chunks[c])
            if len(wd_values) >= 2:
                size = f"{wd_values[0]}x{wd_values[1]}"
            model_variant = model_chunks[c].strip() or None
            if not collected[c]:
                flags.append((page_of_line[i], product_name,
                              f"no price rows found for code {code}"))
                continue
            for tier_label, price in collected[c]:
                rows.append({
                    "brand": brand,
                    "product_name": product_name,
                    "model_variant": model_variant,
                    "variant_context": None,
                    "size": size,
                    "fabric_tier": tier_label,
                    # Ada's own spec page literally titles this whole
                    # choice "UPHOLSTERING / REVETEMENT" -- reusing that
                    # real printed word rather than inventing one (same
                    # discipline as Bonaldo's "Piano"), so the chat UI's
                    # column header is accurate for this product type
                    # instead of falling back to the hardcoded "FABRIC"
                    # default.
                    "tier_label": "Upholstering",
                    "code": code,
                    "price_eur": price,
                    "source_pdf_page": page_of_line[i],
                })
    return rows, flags


def parse_file_ditre_casegoods(path, product_name, brand, all_headings=None, heading_text=None):
    """Shape 2: named material-finish-code grid. See this section's module
    comment for scope (verified against Claire (Tables) only so far)."""
    with open(path, encoding='utf-8') as f:
        lines = f.read().split('\n')

    page_of_line = [None] * len(lines)
    current_page = None
    for idx, ln in enumerate(lines):
        m = re.match(r'^<<<PDFPAGE:(\d+)>>>$', ln.strip())
        if m:
            current_page = int(m.group(1))
        page_of_line[idx] = current_page

    rows = []
    flags = []
    for i, line in enumerate(lines):
        codes = _ditre_header_codes(lines, i)
        if not codes:
            continue
        bounds = [pos for pos, _ in codes] + [len(line)]
        n_cols = len(codes)

        model_chunks, size_chunks, scan_start = _ditre_scan_sku_block(lines, i, bounds, n_cols)
        collected = _ditre_scan_price_rows(
            lines, scan_start, bounds, n_cols, _DITRE_CASEGOODS_ROW_RE,
        )

        for c, (_, code) in enumerate(codes):
            size = None
            wd_values = _DITRE_DIMS_WD_VALUES_RE.findall(size_chunks[c])
            if len(wd_values) >= 2:
                size = f"{wd_values[0]}x{wd_values[1]}"
            model_variant = model_chunks[c].strip() or None
            if not collected[c]:
                flags.append((page_of_line[i], product_name,
                              f"no price rows found for code {code}"))
                continue
            for finish_label, price in collected[c]:
                rows.append({
                    "brand": brand,
                    "product_name": product_name,
                    "model_variant": model_variant,
                    "variant_context": None,
                    "size": size,
                    "fabric_tier": finish_label,
                    # Claire's own spec page literally titles this section
                    # "FINISHES / FINITIONS" -- reusing that real printed
                    # word, same discipline as Shape 1's "Upholstering"
                    # above and Bonaldo's "Piano".
                    "tier_label": "Finishes",
                    "code": code,
                    "price_eur": price,
                    "source_pdf_page": page_of_line[i],
                })
    return rows, flags


# ---------------------------------------------------------------------------
# Pianca -- first implementation slice scoped to Progetti di Design 08 ONLY
# (see extract_catalog.py's parse_index_pianca module comment for the same
# scope caveat on the index side). Only Shape A (the tessuto/pelle tier
# grid: L/H/P + CODICI + the non-contiguous A/B/C/H/P/Q price columns --
# confirmed the SAME 6-tier convention across Collezione Giorno/Notte,
# Progetti 08/09, and Spazi-10's upholstered items during the Step-1
# structural read-only pass) is implemented here.
#
# Shape B (named finish-family columns -- e.g. Lina's OWN "Sedia con
# seduta legno" table sits on the exact same page as its Shape A "Sedia
# con seduta imbottita" table, headed "L H P CODICI Frassino Nero Essenza
# Laccato Opaco") and Shape C (Snake's per-square-metre rate, found only
# in Sistemi Notte, out of scope for this file anyway) are NOT implemented
# yet. Per explicit instruction: any table whose header contains "CODICI"
# but doesn't match Shape A's exact column signature is FLAGGED (with its
# raw header line) and skipped -- never guessed at as Shape A, and never
# silently absorbed into a generic "else" catch-all. Widening this parser
# to a shape it hasn't been verified against risks a silently wrong price,
# which is worse than a visible, triaged gap.
# ---------------------------------------------------------------------------

_PIANCA_TIER_LETTERS = ['A', 'B', 'C', 'H', 'P', 'Q']
_PIANCA_PRICE_CELL_RE = re.compile(r'^-$|^[\d.]{1,7}$')
_PIANCA_CODE_RE = re.compile(r'^[A-Z0-9]{4,10}$')

# Diagram-noise tokens confirmed to glue onto the FRONT of an otherwise
# clean Shape A row -- a small cushion/module icon's own dimension
# annotation (e.g. "45", the cushion depth in cm) sits in the same
# character columns as the row's real label text further right, so
# pdftotext -layout's linearization occasionally merges them onto one
# output line. Originally handled by two narrow, exact patterns (a bare
# 'S'/'D' orientation marker; a "<N> -" pair) that stopped at the first
# non-matching token -- confirmed insufficient 2026-08-21 on Duo/Time
# (CollezioneGiorno), which surfaced 3 noise shapes those patterns never
# covered: a bare '-' alone (no preceding number), a negative-looking
# fragment like '-27' (actually the tail of a "254 -274" range split
# across two lines by pdftotext), and MULTIPLE consecutive leading noise
# tokens ("1 17 -127" in front of a real "rivestimento" label). All 4
# shapes reduce to the same rule: a diagram annotation is ALWAYS purely
# numeric (optionally negative-looking) or a bare dash/S/D -- real label
# text (rivestimento, senza piede centrale, Ø14, ...) never starts that
# way -- so strip EVERY leading token matching that shape, not just one
# fixed-length pattern, continuing until real text is reached or nothing
# is left.
#
# Verified this doesn't risk deleting real content before applying it:
# audited every Category-tier (Shape A) row across all 4 already-touched
# Pianca files (App/regression -- see the 2026-08-21 investigation) for
# any model_variant that would become PURELY empty after this strip --
# every single one found (Asolo '-12', Duo '-'/'-27', Time '-'/'238 168
# -', Volo '-2 05 -') was ALREADY meaningless noise with no real text at
# all, never a genuine bare-numeric label -- so nothing real is lost.
# Also confirmed this bug was NOT limited to the newly-extracted file
# that surfaced it: Progetti di Design 08's already-live "Peonia
# (Divani)" ('45 rivestimento') and Spazi-10's "Brando"/"Giona" had the
# identical silent corruption already present, undetected until this
# audit -- verified via the real page image (peonia_divani_p19-19.jpg)
# that '45' is the cushion diagram's own depth label, not part of the
# real 'rivestimento' row text.
_PIANCA_LEADING_DIAGRAM_NOISE_RE = re.compile(r'^-?\d+(\.\d+)?$|^-$|^[SD]$')


def _pianca_strip_leading_diagram_noise(label_tokens: list) -> list:
    """Mutates nothing -- returns a new list with every leading
    diagram-noise-shaped token removed. See _PIANCA_LEADING_DIAGRAM_NOISE_RE
    for the exact shape and the evidence behind it."""
    tokens = list(label_tokens)
    while tokens and _PIANCA_LEADING_DIAGRAM_NOISE_RE.match(tokens[0]):
        tokens.pop(0)
    return tokens


def _pianca_wrapped_tier_letters_ahead(lines, idx, max_lookahead=5):
    """Return the index of a following line whose own tokens end in the 6
    Shape A tier letters ('A B C H P Q'), if one appears within
    `max_lookahead` lines of `idx` (blank lines don't count against the
    budget). None otherwise.

    Exists because Chloé's own header prints its title word ('Struttura')
    on the SAME line as CODICI but wraps the actual A-B-C-H-P-Q tier row
    onto its OWN following physical line -- unlike every other Shape A
    table, whose tier letters are the header line's own last 6 tokens
    (see `_pianca_is_shape_a_header`). Used both to recognize this wrapped
    variant (`parse_file_pianca_chloe`) and to keep `shape_b_named` from
    misreading the same header line under a same-titled registry key
    (confirmed real risk: '1+1' derives the identical bare ('Struttura',)
    key from an unrelated, genuinely 2-named-column table -- see the
    registry's own comment on this collision)."""
    j = idx + 1
    seen = 0
    while j < len(lines) and seen < max_lookahead:
        if lines[j].strip() == '':
            j += 1
            continue
        if lines[j].split()[-6:] == _PIANCA_TIER_LETTERS:
            return j
        seen += 1
        j += 1
    return None


def _pianca_is_shape_a_header(line: str) -> bool:
    """A Shape A header line ends in exactly the 6 tier-letter tokens 'A B
    C H P Q', in that order, and contains 'CODICI' somewhere before them.
    Checking the LAST 6 tokens (rather than searching for 'H'/'P' as
    isolated characters anywhere after 'CODICI') sidesteps the dimension
    columns' own 'L H P CODICI' segment entirely -- those tokens are never
    among the line's last 6, so there's no ambiguity to resolve."""
    if 'CODICI' not in line:
        return False
    tokens = line.split()
    return tokens[-6:] == _PIANCA_TIER_LETTERS


def parse_file_pianca_shape_a(path, product_name, brand, all_headings=None, heading_text=None):
    """Shape A (tessuto/pelle tier grid) only -- see module comment above
    for scope. Any table whose header contains 'CODICI' but doesn't match
    Shape A's exact 'CODICI ... A B C H P Q' signature is flagged (with
    its raw header line, for manual follow-up) and skipped.

    Price cells are matched by TOKEN POSITION FROM THE END OF THE LINE,
    not by character offset under the header's own letter positions --
    confirmed necessary: pdftotext -layout right-justifies each price
    column independently, so a row's actual price digits routinely start
    several characters to the left OR right of where the header's single-
    character tier letter (e.g. 'A') sits above it. Slicing by the
    header's column offsets silently mis-cut every price on every row
    (verified: it originally produced 0 rows across all 6 Progetti 08
    products). The trailing-6-token approach works because every Shape A
    row unconditionally prints all 6 cells, using a literal '-' for any
    tier with no price (confirmed on Levante's module rows) rather than
    omitting the cell -- so the last 6 whitespace tokens on a matched row
    are always exactly the 6 tier values, in A/B/C/H/P/Q order."""
    with open(path, encoding='utf-8') as f:
        lines = f.read().split('\n')

    page_of_line = [None] * len(lines)
    current_page = None
    for idx, ln in enumerate(lines):
        m = re.match(r'^<<<PDFPAGE:(\d+)>>>$', ln.strip())
        if m:
            current_page = int(m.group(1))
        page_of_line[idx] = current_page

    rows = []
    flags = []
    # Nearest preceding short sub-heading line (e.g. "Composizione",
    # "Sedia con seduta imbottita", "Modulo laterale") -- attached to
    # every row found until the next one is seen. Purely descriptive
    # metadata; never affects which price is recorded for which code.
    variant_context = None

    i = 0
    while i < len(lines):
        line = lines[i]
        if 'CODICI' not in line:
            i += 1
            continue

        if not _pianca_is_shape_a_header(line):
            flags.append((page_of_line[i], product_name,
                          f"unrecognized price-table header (not Shape A), skipped: {line.strip()[:120]!r}"))
            i += 1
            continue

        i += 1
        blank_run = 0
        # Threshold verified against real data: the widest observed blank
        # run between a sub-heading line and its own price row (diagram
        # spacing) is exactly 4 consecutive blank lines, confirmed by a
        # direct count across lina.txt and levante_divani.txt -- an
        # earlier version used 4 as the cutoff itself, which terminates
        # the scan exactly ONE line too early (the loop condition is
        # checked before processing, so blank_run reaching 4 exits before
        # the row right after it is ever read), silently producing 0 rows
        # for every product. 10 gives real margin beyond the widest
        # observed gap without risking a scan running past its table
        # into unrelated later content (the next Shape A/B header line
        # still ends the scan immediately regardless of blank_run).
        while i < len(lines) and blank_run < 10:
            raw = lines[i]
            stripped = raw.strip()
            if stripped == '':
                blank_run += 1
                i += 1
                continue
            if 'CODICI' in raw and _pianca_is_shape_a_header(raw):
                break  # next table's header -- let the outer loop handle it
            blank_run = 0

            tokens = stripped.split()
            trailing = tokens[-6:]
            if len(tokens) < 7 or not all(_PIANCA_PRICE_CELL_RE.match(t) for t in trailing) or not any(t != '-' for t in trailing):
                # Not a price row -- either a sub-heading (e.g.
                # "Composizione") or unrelated prose/diagram text. Only
                # accept it as a new variant_context if it's short,
                # digit-free, and more than a single stray diagram-letter
                # marker (e.g. the "S"/"D" left/right-orientation notes
                # sprinkled through composition diagrams -- confirmed
                # these would otherwise silently clobber a real
                # "Composizione" context with a 1-character label).
                if 2 < len(stripped) <= 60 and len(tokens) <= 6 and not re.search(r'\d', stripped):
                    variant_context = stripped
                i += 1
                continue

            pre_tokens = tokens[:-6]
            code = None
            label_tokens = []
            if pre_tokens:
                if len(pre_tokens) >= 2 and pre_tokens[-1] == 'D/S' and _PIANCA_CODE_RE.match(pre_tokens[-2]) and re.search(r'\d', pre_tokens[-2]):
                    code = f"{pre_tokens[-2]} D/S"
                    label_tokens = list(pre_tokens[:-2])
                elif _PIANCA_CODE_RE.match(pre_tokens[-1]) and re.search(r'\d', pre_tokens[-1]):
                    code = pre_tokens[-1]
                    label_tokens = list(pre_tokens[:-1])

            if code is None:
                i += 1
                continue

            # Dimension columns (L, H, P) are plain numbers immediately
            # preceding the code -- strip trailing numeric tokens so they
            # don't get glued into the model_variant label, but capture
            # the first 3 popped (read right-to-left, i.e. nearest the
            # code first) as the real L/H/P values instead of discarding
            # them outright.
            #
            # These 3 columns aren't always all present on a given row --
            # confirmed real via Duo's own page 151 image: Cuscinetti rows
            # (round cushions) print only L and H ("60 35 99DU735 ..."),
            # leaving P visibly BLANK in the source table, not just an
            # extraction gap. Since pdftotext -layout only omits the
            # token for a genuinely empty cell (it doesn't insert a
            # placeholder), a missing column always drops OFF THE RIGHT
            # end of this token run -- P is dropped before H, H before L
            # -- so popped tokens are captured in pop order (nearest-code
            # first: P, then H, then L) and reversed back to source L/H/P
            # order. Confirmed NOT the reverse (i.e. NOT that a short run
            # keeps the LAST-N header columns) directly against the real
            # page image, not assumed.
            #
            # Capped at 3 pops for the dimension capture itself -- any
            # further trailing numeric token beyond that (never confirmed
            # to occur, but the loop below still strips it exactly as
            # before) is genuinely unidentified noise, not a 4th
            # dimension column, so it's discarded rather than folded into
            # size.
            dims = []
            while label_tokens and re.match(r'^\d+(\.\d+)?$', label_tokens[-1]):
                popped = label_tokens.pop()
                if len(dims) < 3:
                    dims.append(popped)
            dims.reverse()
            size = '×'.join(dims) if dims else None

            # LEADING noise confirmed real on Levante (Divani): pdftotext
            # -layout sometimes glues a stray diagram annotation onto the
            # FRONT of an otherwise-clean row when its vertical position
            # happens to coincide with this row's, since the module-width
            # diagram sits in the same character columns as the label
            # text further right (originally found via a bare 'S'/'D'
            # orientation marker and a "<N> -" range fragment; widened
            # 2026-08-21 to a general strip after Duo/Time surfaced 3 more
            # noise shapes those exact patterns didn't cover -- see
            # _pianca_strip_leading_diagram_noise's own comment for the
            # full evidence, including which already-live products this
            # was silently affecting before the fix).
            label_tokens = _pianca_strip_leading_diagram_noise(label_tokens)

            label = ' '.join(label_tokens).strip() or None

            any_price = False
            for letter, cell in zip(_PIANCA_TIER_LETTERS, trailing):
                if cell == '-':
                    continue
                any_price = True
                rows.append({
                    "brand": brand,
                    "product_name": product_name,
                    "model_variant": label,
                    "variant_context": variant_context,
                    "size": size,
                    "fabric_tier": letter,
                    "tier_label": "Category",
                    "code": code,
                    "price_eur": cell,
                    "source_pdf_page": page_of_line[i],
                })
            if not any_price:
                flags.append((page_of_line[i], product_name,
                              f"no price rows found for code {code}"))
            i += 1
        # continue outer loop from wherever the inner scan stopped
    return rows, flags


# ---------------------------------------------------------------------------
# Chloé (CollezioneNotte) only, real PDF page 98 -- a WRAPPED Shape A
# header: the title word ('Struttura') sits on the same physical line as
# CODICI, but the actual A-B-C-H-P-Q tier-letter row prints on its own
# following line instead of trailing the CODICI line itself the way every
# other Shape A table does. Found 2026-08-27 while resolving the
# shape_b_named ('Struttura',) collision candidate against 1+1 -- Chloé
# only LOOKED like a match for that registry (same bare tail token on the
# header line); its real table is Shape A, just with a 1-line-deeper wrap,
# confirmed via direct image inspection. Row body (dims/code/diagram-noise
# handling/price cells) is byte-for-byte the same convention as base Shape
# A, so this reuses that logic unchanged -- only the header detection and
# the skip-past-the-tier-line step differ. Scoped to product_name (not just
# the header-content check, which is already confirmed catalog-wide unique
# via grep) as defense-in-depth, matching this session's standing practice
# for every other narrowly-scoped parser.
# ---------------------------------------------------------------------------

def parse_file_pianca_chloe(path, product_name, brand, all_headings=None, heading_text=None):
    """See module comment above for scope."""
    if product_name != 'Chloé':
        return [], []
    with open(path, encoding='utf-8') as f:
        lines = f.read().split('\n')

    page_of_line = [None] * len(lines)
    current_page = None
    for idx, ln in enumerate(lines):
        m = re.match(r'^<<<PDFPAGE:(\d+)>>>$', ln.strip())
        if m:
            current_page = int(m.group(1))
        page_of_line[idx] = current_page

    rows = []
    flags = []
    variant_context = None

    i = 0
    while i < len(lines):
        line = lines[i]
        if 'CODICI' not in line or _pianca_is_shape_a_header(line):
            i += 1
            continue
        tier_idx = _pianca_wrapped_tier_letters_ahead(lines, i)
        if tier_idx is None:
            i += 1
            continue

        i = tier_idx + 1
        blank_run = 0
        while i < len(lines) and blank_run < 10:
            raw = lines[i]
            stripped = raw.strip()
            if stripped == '':
                blank_run += 1
                i += 1
                continue
            if 'CODICI' in raw and (_pianca_is_shape_a_header(raw)
                                     or _pianca_wrapped_tier_letters_ahead(lines, i) is not None):
                break  # next table's header -- let the outer loop handle it
            blank_run = 0

            tokens = stripped.split()
            trailing = tokens[-6:]
            if len(tokens) < 7 or not all(_PIANCA_PRICE_CELL_RE.match(t) for t in trailing) or not any(t != '-' for t in trailing):
                if 2 < len(stripped) <= 60 and len(tokens) <= 6 and not re.search(r'\d', stripped):
                    variant_context = stripped
                i += 1
                continue

            pre_tokens = tokens[:-6]
            code = None
            label_tokens = []
            if pre_tokens:
                if len(pre_tokens) >= 2 and pre_tokens[-1] == 'D/S' and _PIANCA_CODE_RE.match(pre_tokens[-2]) and re.search(r'\d', pre_tokens[-2]):
                    code = f"{pre_tokens[-2]} D/S"
                    label_tokens = list(pre_tokens[:-2])
                elif _PIANCA_CODE_RE.match(pre_tokens[-1]) and re.search(r'\d', pre_tokens[-1]):
                    code = pre_tokens[-1]
                    label_tokens = list(pre_tokens[:-1])

            if code is None:
                i += 1
                continue

            dims = []
            while label_tokens and re.match(r'^\d+(\.\d+)?$', label_tokens[-1]):
                popped = label_tokens.pop()
                if len(dims) < 3:
                    dims.append(popped)
            dims.reverse()
            size = '×'.join(dims) if dims else None

            label_tokens = _pianca_strip_leading_diagram_noise(label_tokens)
            label = ' '.join(label_tokens).strip() or None

            any_price = False
            for letter, cell in zip(_PIANCA_TIER_LETTERS, trailing):
                if cell == '-':
                    continue
                any_price = True
                rows.append({
                    "brand": brand,
                    "product_name": product_name,
                    "model_variant": label,
                    "variant_context": variant_context,
                    "size": size,
                    "fabric_tier": letter,
                    "tier_label": "Category",
                    "code": code,
                    "price_eur": cell,
                    "source_pdf_page": page_of_line[i],
                })
            if not any_price:
                flags.append((page_of_line[i], product_name,
                              f"no price rows found for code {code}"))
            i += 1
        # continue outer loop from wherever the inner scan stopped
    return rows, flags


# ---------------------------------------------------------------------------
# Pianca Shape B, 2-axis variant -- Norma Up (Progetti di Design 09) only,
# verified against real PDF pages 54-64 (visually confirmed at 150dpi, not
# just text extraction). Price varies along TWO independent axes at once:
#   - ROW axis: "Struttura" (frame) finish -- exactly 3 known values,
#     "Materico" / "L. Opaco" / "Essenza / L. Metallico" -- printed ONCE on
#     the first of 3 consecutive dimension sub-rows, omitted on the other 2
#     (a continuation pattern, same idea as Peonia's Zoccolo rows in Shape
#     A, but here the label is fully ABSENT on continuation rows rather
#     than repeated).
#   - COLUMN axis: 6 "Frontali" (drawer-front) finish columns, nested under
#     2 parent "Copertura esterna" (exterior covering) groups of 3 each --
#     confirmed via direct pdftotext -tsv inspection of real page 12's
#     analogous header (see Enea Up investigation) and cross-checked
#     against the identical multi-line header text repeated verbatim on
#     every Norma Up 2-axis page sampled (54/55/56...64).
# The CODE alone identifies dimension only (same code appears across all 3
# struttura-finish rows for one L/P pair, confirmed real: "00KFFC" prints
# on the Materico, L. Opaco, AND Essenza/L. Metallico rows alike) -- a full
# unique price key is (code, struttura_finish, frontali_column), all 3
# captured here, so there is no ambiguity despite the code repeating.
#
# Same leading-diagram-noise risk as Levante's S/D bug, but a DIFFERENT
# fix: rather than positional stripping (which only works when noise sits
# at a fixed position relative to real content), this searches for one of
# the 3 known struttura_finish keywords anywhere in the row's pre-code
# text and ignores everything else -- confirmed necessary on real page 55,
# where a stray "A 116" diagram annotation (anta-type letter + a width
# number) glues onto the front of an "Essenza / L. Metallico" row.
# ---------------------------------------------------------------------------

_PIANCA_2AXIS_COLUMNS = [
    "Copertura L.Opaco/Essenza — Frontali L.Opaco",
    "Copertura L.Opaco/Essenza — Frontali Essenza",
    "Copertura L.Opaco/Essenza — Frontali Lucido/Metall/Laccato/Marmo",
    "Copertura Lucido/Metall/Laccato/Marmo — Frontali L.Opaco",
    "Copertura Lucido/Metall/Laccato/Marmo — Frontali Essenza",
    "Copertura Lucido/Metall/Laccato/Marmo — Frontali Lucido/Metall/Laccato/Marmo",
]
_PIANCA_2AXIS_LEGEND_RE = re.compile(r'^[A-Z]:\s')
_PIANCA_2AXIS_HEADING_RE = re.compile(r'^\d+\s*\(.+\)$')


def _pianca_is_2axis_header(line: str) -> bool:
    """A 2-axis header line ends in exactly 6 repetitions of the bare word
    'Frontali' (the finish-specific sub-label -- 'L. Opaco'/'Essenza'/etc
    -- wraps to the FOLLOWING physical line, confirmed on every sampled
    page), and contains 'CODICI' somewhere before them."""
    if 'CODICI' not in line:
        return False
    tokens = line.split()
    return tokens[-6:] == ['Frontali'] * 6


def _pianca_2axis_struttura_finish(pre_tokens: list) -> str | None:
    """Keyword search rather than positional parsing -- see module comment
    for why (stray diagram annotations can land anywhere in the leading
    text, not just a fixed position). Checked in this order because
    'Essenza' uniquely identifies the 3rd value even though 'L. Opaco'
    could otherwise partially overlap in casual substring checks.

    The 3rd value's own label is NOT always spelled out in full --
    confirmed real via a direct count across norma_up.txt: 'Ess. /
    L-Met.' (14x, abbreviated -- narrower-width sections truncate it to
    fit) vs 'Essenza / L-Met.' (10x) vs 'Essenza / L. Metallico' (5x).
    Matching on a bare 'Essenza' substring silently missed all 14
    abbreviated instances, which then fell through with found_finish=None
    and incorrectly CARRIED FORWARD the previous row's struttura_finish
    (e.g. 'L. Opaco') instead -- confirmed to cause 504 real (code,
    fabric_tier) collisions this way (e.g. code 00K74T's real 'L. Opaco'
    row and its real 'Ess. / L-Met.' row both got labeled 'L. Opaco',
    silently merging two DIFFERENT real prices, 1.459 and 1.722, under
    one ambiguous-marked key). Matching on the 'Ess' PREFIX (not a
    full-word 'Essenza' substring) catches all 3 spellings identically."""
    text = ' '.join(pre_tokens)
    if any(t.startswith('Ess') for t in pre_tokens):
        return 'Essenza / L. Metallico'
    if 'Materico' in text:
        return 'Materico'
    if 'Opaco' in text:
        return 'L. Opaco'
    return None


def parse_file_pianca_norma_up_2axis(path, product_name, brand, all_headings=None, heading_text=None):
    """See module comment above for scope (Norma Up only, verified against
    real PDF pages 54-64)."""
    with open(path, encoding='utf-8') as f:
        lines = f.read().split('\n')

    page_of_line = [None] * len(lines)
    current_page = None
    for idx, ln in enumerate(lines):
        m = re.match(r'^<<<PDFPAGE:(\d+)>>>$', ln.strip())
        if m:
            current_page = int(m.group(1))
        page_of_line[idx] = current_page

    rows = []
    flags = []
    variant_context = None  # e.g. "102 (2 vani anta)" -- the module-width heading
    struttura_finish = None  # carried forward across continuation rows with no label of their own

    i = 0
    while i < len(lines):
        line = lines[i]
        if not _pianca_is_2axis_header(line):
            i += 1
            continue

        # Safety gate: verify the "Copertura esterna" parent header this
        # column-label set assumes is actually present nearby, rather than
        # trusting the hardcoded _PIANCA_2AXIS_COLUMNS blind just because
        # the line ends in 6x "Frontali". Checked in the 6 lines before
        # the header (confirmed real position: 4 lines above on every
        # sampled page).
        lookback = '\n'.join(lines[max(0, i - 6):i])
        if lookback.count('Copertura esterna') < 2:
            flags.append((page_of_line[i], product_name,
                          f"2-axis-shaped header (6x 'Frontali') but 'Copertura esterna' parent labels not confirmed nearby, skipped: {line.strip()[:120]!r}"))
            i += 1
            continue

        i += 1
        blank_run = 0
        struttura_finish = None  # reset per table -- don't leak a prior table's last row-label
        while i < len(lines) and blank_run < 10:
            raw = lines[i]
            stripped = raw.strip()
            if stripped == '':
                blank_run += 1
                i += 1
                continue
            if _pianca_is_2axis_header(raw) or _pianca_is_shape_a_header(raw):
                break  # next table's header (either shape) -- let the outer loop handle it
            blank_run = 0

            if _PIANCA_2AXIS_LEGEND_RE.match(stripped):
                i += 1
                continue  # "A: battente" / "C: cassetto" / etc -- anta-type legend, not context

            tokens = stripped.split()
            trailing = tokens[-6:]
            if len(tokens) < 7 or not all(_PIANCA_PRICE_CELL_RE.match(t) for t in trailing) or not any(t != '-' for t in trailing):
                # Not a price row. Unlike Shape A's context capture, this
                # DELIBERATELY allows digit-bearing headings (e.g. "102 (2
                # vani anta)" -- the module-width group heading), since
                # excluding all-digit-free lines the way Shape A does
                # would silently drop the only context these rows have.
                # But NOT any short digit-bearing line -- confirmed real
                # on page 55: a stray "60    60" diagram width-pair
                # (unrelated to price data) sits between two struttura-
                # finish row groups and would otherwise silently clobber
                # the real "122 (1 vano anta)" heading with garbage,
                # corrupting every row after it until the next real
                # heading. Real headings all match "<digits> (<text>)" --
                # a bare number or number-pair never does, so requiring
                # the parenthesized suffix is a precise, verified filter
                # rather than a loose length/token-count heuristic.
                if _PIANCA_2AXIS_HEADING_RE.match(stripped):
                    variant_context = stripped
                i += 1
                continue

            pre_tokens = tokens[:-6]
            code = None
            remaining = None
            if pre_tokens:
                if len(pre_tokens) >= 2 and pre_tokens[-1] == 'D/S' and _PIANCA_CODE_RE.match(pre_tokens[-2]) and re.search(r'\d', pre_tokens[-2]):
                    code = f"{pre_tokens[-2]} D/S"
                    remaining = list(pre_tokens[:-2])
                elif _PIANCA_CODE_RE.match(pre_tokens[-1]) and re.search(r'\d', pre_tokens[-1]):
                    code = pre_tokens[-1]
                    remaining = list(pre_tokens[:-1])

            if code is None:
                i += 1
                continue

            found_finish = _pianca_2axis_struttura_finish(pre_tokens)
            if found_finish is not None:
                struttura_finish = found_finish

            # H/P dimension capture -- confirmed via direct pixel-level
            # inspection of Siviglia's real page image (siviglia_p71-71.jpg,
            # the same "L [gap] H P CODICI" header convention as this
            # shape): the section-level heading number ("81"/"162" etc,
            # captured separately as variant_context) is L, and the 2
            # inline numbers on each row's own line (e.g. "129 49" before
            # 00J4G8) are H then P, in that reading order -- NOT an L/P
            # pair as an earlier comment in this file loosely assumed
            # without checking column alignment. L itself is never
            # captured into `size` here since it never appears on the
            # row's own line, same class of limitation as Mambo's Pelle
            # Sint. shape leaving an externally-wrapped L uncaptured.
            dims = []
            if remaining:
                while remaining and re.match(r'^\d+(\.\d+)?$', remaining[-1]):
                    popped = remaining.pop()
                    if len(dims) < 3:
                        dims.append(popped)
            dims.reverse()
            size = '×'.join(dims) if dims else None

            # Both axes are folded into fabric_tier (not split across
            # fabric_tier + model_variant) because main()'s cross-brand
            # ambiguous-row detection keys on (product, code, fabric_tier)
            # ONLY -- model_variant is deliberately excluded there (kept
            # that way after confirming widening it would silently un-
            # flag real conflicts in Bolzan/Cattelan, see the Lina
            # investigation). The SAME code prints across all 3 struttura
            # rows for one L/P pair (confirmed real, not a parsing
            # error), so leaving struttura_finish in model_variant made
            # every single Norma Up row collide on (code, column) and
            # get marked ambiguous -- 1566/1566 rows, confirmed via a
            # real parser run, not assumed. Combining both axes into one
            # fabric_tier string is also the semantically honest model:
            # a customer must specify BOTH the Struttura finish and the
            # Frontali/Copertura finish to get an exact price, so
            # "the tier" genuinely is the pair, not either alone.
            any_price = False
            for column_label, cell in zip(_PIANCA_2AXIS_COLUMNS, trailing):
                if cell == '-':
                    continue
                any_price = True
                tier = f"{struttura_finish} — {column_label}" if struttura_finish else column_label
                rows.append({
                    "brand": brand,
                    "product_name": product_name,
                    "model_variant": None,
                    "variant_context": variant_context,
                    "size": size,
                    "fabric_tier": tier,
                    "tier_label": "Finish",
                    "code": code,
                    "price_eur": cell,
                    "source_pdf_page": page_of_line[i],
                })
            if not any_price:
                flags.append((page_of_line[i], product_name,
                              f"no price rows found for code {code}"))
            i += 1
        # continue outer loop from wherever the inner scan stopped
    return rows, flags


# ---------------------------------------------------------------------------
# Pianca Shape B, simple named-columns variant -- Elide, Onda Indoor, Soffio
# Up only (verified against real pages). A single-axis version of the named
# finish-family grid: one row per code (or a few dimension-varying rows
# under one sub-heading), N named finish columns, no struttura/frontali
# 2-axis split. Column labels are looked up in an EXPLICIT, per-table
# VERIFIED registry keyed by the literal tokens the CODICI header line
# itself carries (the first physical line of what may be a multi-line
# wrapped column label) -- same discipline as KNOWN_DITRE_TOC_LABELS/
# VARASCHINI_SECTIONS elsewhere in this project: a table whose header
# isn't in the registry is flagged, never guessed at with an invented
# label or a wrong column count. Column COUNT still comes from the real
# trailing-price-token count on each row (same technique as every other
# Pianca shape here), not from counting header tokens (unreliable when
# labels wrap to 2 physical lines, e.g. Onda Indoor's Marmo table: "Gioia
# di" + "Carrara" on the next line is ONE column, not two).
# ---------------------------------------------------------------------------

_PIANCA_SHAPEB_NAMED_HEADERS = {
    ('Carta', 'Kraft'): ['Carta Kraft'],
    ('L.', 'Opaco', 'Lucido', 'Sp.', 'Essenza'): ['L. Opaco', 'Lucido Sp.', 'Essenza'],
    ('Gioia', 'di', 'Emperador', 'Fior', 'di', 'Verde', 'Alpi', 'Rosso'):
        ['Gioia di Carrara', 'Emperador Grafite', 'Fior di Pesco', 'Verde Alpi Travertino', 'Rosso Lepanto'],
    ('L.', 'Opaco', 'Essenza', 'Fenix®', 'V.', 'Lacc.', 'V.', 'Marmo', 'Gres'):
        ['L. Opaco', 'Essenza', 'Fenix®', 'V. Lacc. / V. L-Met.', 'V. Marmo', 'Gres'],
    # Mambo (Progetti di Design 09), verified against real PDF pages
    # 36, 41-42, 44 -- each confirmed via direct row inspection to have
    # ONE code per row (no struttura-style repeat across multiple
    # labeled rows), so these are genuinely simple single-axis tables
    # despite living on the same product as Mambo's OWN 2-axis grid
    # (parse_file_pianca_mambo_2axis, a structurally different table).
    ('L.', 'Opaco', 'Essenza', 'Lucido', 'Sp.'):
        ['L. Opaco', 'Essenza', 'Lucido Sp. / L. Metallico'],
    ('V.', 'Laccato', 'V.', 'Marmo', 'Specchio', 'Pelle', 'Sint.', 'Cuoio', 'Rig.', 'Marmo'):
        ['V. Laccato / V. L-Met. / V. Metall.', 'V. Marmo', 'Specchio', 'Pelle Sint.', 'Cuoio Rig.', 'Marmo / Terrazzo'],
    ('Struttura', 'e', 'frontali'): ['Struttura e frontali'],
    ('Essenza', 'Poro', 'aperto'): ['Essenza', 'Poro aperto'],
    # Enea Up (Progetti di Design 09), real PDF page 12 -- wildcard-code
    # table, price-safe (see the wildcard-code comment above in the row
    # scan loop): price is resolved by (row, column) regardless of the
    # '*' placeholder in the printed order code.
    ('L.', 'Opaco', 'Essenza', 'Noce', 'Canaletto'): ['L. Opaco', 'Essenza', 'Noce Canaletto'],
    # Siviglia (Progetti di Design 09), verified against real PDF pages
    # 77 and 79. Both confirmed via direct row inspection to have ONE
    # code per row (no repeat across labeled rows) despite Siviglia ALSO
    # having a genuinely 2-axis table (parse_file_pianca_siviglia_matrix,
    # a structurally different table on the same product).
    ('V.', 'Laccato', 'V.', 'Marmo', 'Specchio', 'Cuoio', 'Rig.', 'Marmo'):
        ['V. Laccato / V. L-Met. / V. Metall.', 'V. Marmo', 'Specchio', 'Cuoio Rig.', 'Marmo / Terrazzo'],
    # p.79's "H L P CODICI Frontali x4" table LOOKS like a 2-axis grid at
    # first glance (matches Norma Up's 6x-Frontali family in spirit) but
    # confirmed via direct code-repetition check (grep for each sampled
    # code, e.g. 06J15) that every code appears EXACTLY ONCE -- so unlike
    # Norma Up/Mambo/Siviglia's own Materico/L.Opaco/Essenza/Lucido Sp.
    # matrix, there's no row-type axis needing its own field; the 4
    # columns (2 parent Struttura groups x 2 Frontali sub-choices each)
    # are just 4 flat named columns, safe for the simple registry.
    ('Frontali', 'Frontali', 'Frontali', 'Frontali'):
        ['Struttura L.Opaco/Essenza/PoroAperto — Frontali L.Opaco/Essenza/PoroAperto',
         'Struttura L.Opaco/Essenza/PoroAperto — Frontali LucidoSp/LMetallico',
         'Struttura LucidoSp/LMetallico — Frontali L.Opaco/Essenza/PoroAperto',
         'Struttura LucidoSp/LMetallico — Frontali LucidoSp/LMetallico'],
    # Onda Indoor (Spazi-10), real PDF page 9 ("Tavolo rotondo con piano
    # fisso"/"...piatto girevole"/"Tavolo large" tables) -- Spazi-10's
    # version adds a 4th "V. Martellato" column vs. Progetti 09's 3-col
    # laccato table above; verified via direct code inspection (T0ND16H,
    # T0ND16R, T0ND12T, T0ND12X each appear exactly once here, no
    # cross-row-type repeat), so this is the same simple single-axis
    # shape, just a new column-set registry key -- not a new table shape.
    ('L.', 'Opaco', 'Lucido', 'Sp.', 'Essenza', 'V.', 'Martellato'):
        ['L. Opaco', 'Lucido Sp.', 'Essenza', 'V. Martellato'],
    # Mensole legno per boiserie AND Spazioteca "Passaggio porta per
    # moduli ponte" (Spazi-10) share this exact header signature --
    # verified via direct row inspection on both (real PDF pages ~43-45
    # for Spazioteca, ~70-74 for Mensole legno) that every code appears
    # exactly once, no row-type-axis repeat.
    ('Materico', 'L.', 'Opaco', 'Lucido', 'Sp.'):
        ['Materico', 'L. Opaco / Essenza', 'Lucido Sp. / L. Metallico'],
    # Woody, real pdf page (header "L min L max CODICI Essenza") -- a
    # single named column, simplest possible instance of this shape.
    # Found 2026-08-25 during the full known_gap inventory pass.
    ('Essenza',): ['Essenza'],
    # Fushimi Lounge, real pdf page (header "L H P CODICI Cuoio") -- same
    # single-named-column shape, different real word. Found 2026-08-25.
    ('Cuoio',): ['Cuoio'],
    # CollezioneGiorno remainder, found 2026-08-26 during a full sweep of
    # the never-individually-verified remainder of the original 65-
    # product batch extraction. Each key below was explicitly checked for
    # collision against every OTHER product deriving the same key (this
    # registry is catalog-wide flat, keyed only by the CODICI line's own
    # literal trailing tokens) before being added -- several real
    # candidates found this same sweep (1+1 vs Chloé both deriving bare
    # ('Struttura',); Abaco vs Baio vs Soffio fisso/allungabile all
    # deriving bare ('Piano',) with different real column counts; Intro vs
    # Delta allungabile both deriving bare ('Basamento',)) were
    # DELIBERATELY left out of this registry pending a dedicated
    # collision-resolution pass -- not force-added just because one side
    # happens to look safe.
    #
    # 1+1 (Progetti 06-07... actually CollezioneGiorno "Tavolini"), real
    # PDF page 21 -- resolved 2026-08-27. NOT a genuine collision with
    # Chloé after all: Chloé's own 'CODICI Struttura' line is a WRAPPED
    # Shape A header (its A-B-C-H-P-Q tier letters print on their own
    # following physical line, confirmed via image), a structurally
    # different table this registry never represents -- built as its own
    # dedicated `parse_file_pianca_chloe` instead, and
    # `_pianca_wrapped_tier_letters_ahead` keeps this key from ever
    # matching Chloé's header line. 1+1's own table is genuinely 2 named
    # columns (Struttura's own 2 finish options), confirmed via image: the
    # 'Piano' group to the left is a fixed material note (always Laccato
    # Opaco, not a priced column), only 'Struttura' has 2 real prices.
    ('Struttura',): ['Laccato Opaco', 'Finiture Metallo'],
    #
    # Seida, real PDF page 18: a genuine 4-column hybrid -- 2 named wood-
    # finish columns (Laccato Opaco/Essenza) plus 2 Shape-A-style tier-
    # LETTER columns (A-B-C tessuto cliente / H-P-Q) reused as column
    # headers here instead of row labels, confirmed via direct image
    # inspection, not assumed from the header text alone.
    ('Seduta', 'legno', 'Seduta', 'imbottita'):
        ['Laccato Opaco', 'Essenza', 'Seduta A-B-C / tessuto cliente', 'Seduta H-P-Q'],
    # Duetto's own ('Laccato', 'Opaco', 'Finiture', 'Metallo') key
    # deliberately NOT added here -- a full-catalog recheck (not just
    # against this batch's own candidate list) found it ALSO collides
    # with Brema, Norma (CollezioneGiorno), and Norma (CollezioneNotte),
    # none of which have been individually verified for their own real
    # column labels under this key yet. Caught the same way the Abaco/
    # Scacco 'Top' near-miss below was -- see that comment for the actual
    # incident this generalized the check to prevent.
    #
    # Haik, real PDF page 22 -- each column's own name wraps to 2 further
    # lines (Malva/Oceano/Onice, Argento/Bronzo/Oro), verified via image.
    ('Malva', 'Argento'): ['Malva / Oceano / Onice', 'Argento / Bronzo / Oro'],
    # Servoquadro_Servogiro, real PDF page 26 -- same 2-column shape as
    # the existing 'L. Opaco'/'Essenza'/'Lucido Sp.' family but WITHOUT a
    # trailing Essenza token on this specific header (verified via image
    # it's still 2 real columns, Essenza wraps under the first).
    ('L.', 'Opaco', 'Lucido', 'Sp.'): ['L. Opaco / Essenza', 'Lucido Sp.'],
    # Abaco's own ('Top',) key deliberately NOT added -- a real
    # near-miss caught during this same sweep: it LOOKED unique against
    # this batch's own candidate list, but a full-catalog recheck found
    # Scacco ALSO derives bare ('Top',), with genuinely DIFFERENT real
    # column labels ('Linoleum'/'V. Laccato', confirmed via direct image)
    # that happen to share the same COLUMN COUNT (2) as Abaco's own
    # ('Cuoio Rigenerato...'/'Vetro Marmo...') -- meaning the row-shape
    # safety check (which only validates trailing PRICE COUNT, not label
    # correctness) would NOT have caught this: Scacco's real rows would
    # have parsed successfully but under Abaco's wrong column names,
    # silently mislabeling real data rather than failing loudly. Caught
    # only by manually verifying the bonus match's own image after
    # first adding this key -- prompted widening the collision check
    # from "this batch's candidates" to a full-catalog grep for every
    # new key before trusting any of them, which is what caught the
    # Duetto/Brema/Norma collision above too.
    #
    # Confluence, real PDF page 28 -- a 5-real-column table whose header
    # line only shows 3 literal 'Piano' tokens (2 of the 3 top-level
    # Piano/finish groups each span 2 Basamento-finish sub-columns, same
    # "parent-group token count != real column count" pattern already
    # established for Siviglia's own 2-parent/4-child 'Frontali' key
    # above) -- verified via direct image inspection of the real 2-row
    # header, not assumed 1 column per literal 'Piano' token.
    ('Piano', 'Piano', 'Piano'): [
        'Piano Fenix® Bianco — Basamento Bianco Lucido',
        'Piano Fenix® Nero — Basamento Nero Lucido',
        'Piano Fenix® Nero — Basamento Titanio Lucido',
        'Piano Borgogna/Fr.Antracite — Basamento Nero Lucido',
        'Piano Borgogna/Fr.Antracite — Basamento Titanio Lucido',
    ],
    # Delta fisso, real PDF page 30 -- 5 named columns, verified via image.
    ('L.', 'Opaco', 'Essenza', 'V.', 'Laccato', 'V.', 'Marmo', 'Marmo'):
        ['L. Opaco', 'Essenza', 'V. Laccato / V. Trasp.', 'V. Marmo / Terrazzo', 'Marmo'],
    # Soffio fisso / Soffio allungabile, real PDF pages 42-44 / 45-47 --
    # collision cluster #3 (2026-08-27), the shared half. Both derive the
    # bare ('Piano',) key (also derived by Abaco/Aliseo/Baio, each of which
    # gets its own dedicated product_name-scoped parser instead -- see the
    # module comment above `parse_file_pianca_abaco`/`_aliseo`/`_baio` for
    # the full safety proof of why THIS entry is nonetheless safe to share:
    # Abaco/Aliseo (2 real columns) and Baio (4) can never produce 5
    # consecutive valid trailing price-cell tokens, since their own CODICI
    # code token always falls inside a 5-wide trailing slice and never
    # matches _PIANCA_PRICE_CELL_RE. L. Opaco and Essenza are always priced
    # identically here (confirmed via image -- a real coincidence, not a
    # merged column; genuinely 5 distinct header labels).
    ('Piano',): ['L. Opaco', 'Essenza', 'Fenix® Bianco/Nero', 'V. Laccato', 'V. Marmo'],
    # Duetto / Brema / Norma (CollezioneNotte) -- collision cluster #4
    # (2026-08-27). The original note below (still kept for its accurate
    # description of clusters #5-8) named a 4th product, "Norma
    # (CollezioneGiorno)", as sharing this exact bare ('Laccato','Opaco',
    # 'Finiture','Metallo') key -- disproven on re-verification: that note
    # was written against `norma.txt`, a stale ORPHAN text file (12 found
    # catalog-wide, not referenced by any catalog_index.json entry -- a
    # pre-rename leftover, same family as the already-known people.txt/
    # logos.txt orphans). Norma (CollezioneGiorno)'s REAL, currently-live
    # file (`norma_collezionegiorno.txt`) prints its own header as 'L.
    # Opaco' (abbreviated, confirmed via image), not 'Laccato Opaco' --
    # a genuinely different, catalog-wide-UNIQUE key, given its own
    # separate registry entry just below. The real 3-way collision
    # (Duetto/Brema/Norma (CollezioneNotte)) turned out to have IDENTICAL
    # real column labels across all 3 (confirmed via image on each),
    # unlike every other cluster this session -- safe to share one entry.
    ('Laccato', 'Opaco', 'Finiture', 'Metallo'): ['Laccato Opaco', 'Finiture Metallo'],
    # Norma (CollezioneGiorno) -- the other half of cluster #4, its own
    # catalog-wide-unique key (see comment above).
    ('L.', 'Opaco', 'Finiture', 'Metallo'): ['L. Opaco', 'Finiture Metallo'],
    # CollezioneNotte remainder, found 2026-08-26/27 during the same full
    # sweep as the CollezioneGiorno batch above -- same discipline: every
    # key checked against the FULL catalog (not just this batch's own
    # candidates) before being added. Real collisions found and
    # deliberately NOT added this round: ('Laccato','Opaco','Essenza',
    # 'Lucido','Sp.') (Consolle Elle vs Domino's OWN first table vs Luce
    # Illumia vs Ponti), ('Struttura','Struttura') (Ala vs Dedalo
    # (Progetti 06-07) vs Island up vs People (CollezioneNotte) vs People
    # (SistemiGiorno)), ('Laccato','Opaco','Essenza','Lucido',
    # 'Spazzolato') (Ala's OWN 2nd table vs Spazioteca (SistemiGiorno) vs
    # Venere), ('Laccato','Opaco') (Forma, all 3 of its own tables, vs
    # Boiserie Soft vs Norma Up vs Ponti).
    #
    # Mensole vetro per boiserie, real PDF page (single table).
    ('Vetro', 'Trasparente', 'Vetro', 'per'):
        ['Vetro Trasparente / Piombo', 'Vetro per retroilluminazione'],
    # Mensole metallo per boiserie, real PDF page -- confirmed all 3
    # tokens after CODICI ('Canna di Fucile') are a real column name
    # (a finish), not stray legend text bleeding onto the header line.
    ('Canna', 'di', 'Fucile', 'Laccato', 'Opaco', 'Finiture', 'Metallo'):
        ['Canna di Fucile', 'Laccato Opaco', 'Finiture Metallo'],
    # Boiserie e People, real PDF page 70.
    ('L.', 'Opaco', 'Lucido', 'Sp.', 'Fin.', 'Metallo'):
        ['L. Opaco / Essenza', 'Lucido Sp.', 'Fin. Metallo'],
    # Domino's OWN 2nd table ("Staffe metallo"), real PDF page -- its 1st
    # table shares the deferred ('Laccato','Opaco','Essenza','Lucido',
    # 'Sp.') collision above, but this one has a genuinely unique key.
    ('L.', 'Opaco', 'Lucido', 'Alluminio', 'Metacrilato'):
        ['L. Opaco / Essenza', 'Lucido Spazzolato', 'Alluminio Brunito', 'Metacrilato'],
    # Accessori (Pedane, pianali e scrittoi), real PDF page.
    ('Laccato', 'Opaco', 'Alluminio'): ['Laccato Opaco', 'Alluminio'],
    # Nota, real PDF page 114 -- both columns share a common 'Fianchi
    # Essenza / Frontali e top' prefix, disambiguated only by their own
    # trailing finish word, verified via direct row inspection.
    ('Fianchi', 'Essenza'):
        ['Fianchi Essenza / Frontali e top: Laccato Opaco', 'Frontali e top: Lucido Spazzolato'],
    # Kyoto, real PDF page 102 -- one of the 2 originally-named "outlier"
    # products. A genuinely simple SINGLE-axis 7-column table despite
    # LOOKING like a Norma-Up-style 2-axis danger grid at first glance
    # (3 'Struttura esterna' parent groups x 2 'Frontali' sub-choices,
    # same "parent-group token count != real column count" pattern as
    # Confluence/Siviglia above) -- confirmed via direct row inspection
    # that every code appears EXACTLY ONCE (no repeat-code-different-
    # price collision risk), so this is safe for the simple registry,
    # not a case needing its own dedicated 2-axis function.
    ('Struttura', 'esterna', 'Struttura', 'esterna', 'Struttura', 'esterna', 'Vassoio'): [
        'Struttura esterna Laccato Opaco — Frontali L. Opaco/Essenza',
        'Struttura esterna Laccato Opaco — Frontali Lucido Sp.',
        'Struttura esterna Essenza — Frontali L. Opaco/Essenza',
        'Struttura esterna Essenza — Frontali Lucido Sp.',
        'Struttura esterna Lucido Sp. — Frontali L. Opaco/Essenza',
        'Struttura esterna Lucido Sp. — Frontali Lucido Sp.',
        'Vassoio L. Opaco',
    ],
}

_PIANCA_SHAPEB_HEADING_RE = re.compile(r'^([A-Za-zÀ-ÿ]{3,}|\d+\s+[A-Za-zÀ-ÿ])')


def _pianca_shapeb_named_header_columns(line: str):
    """Return the registered column-label list for this header line, or
    None if it's not a 'CODICI' line, IS a Shape A or 2-axis header
    (mutually exclusive with this shape -- checked first so those never
    fall through here), or its post-CODICI token signature isn't in the
    verified registry."""
    if 'CODICI' not in line:
        return None
    if _pianca_is_shape_a_header(line) or _pianca_is_2axis_header(line):
        return None
    tail = line.split('CODICI', 1)[1].split()
    return _PIANCA_SHAPEB_NAMED_HEADERS.get(tuple(tail))


def parse_file_pianca_shape_b_named(path, product_name, brand, all_headings=None, heading_text=None):
    """See module comment above for scope. Any 'CODICI' line that isn't
    Shape A, isn't 2-axis, and isn't in the verified column registry is
    left alone here (Shape A's own scan already flags it; see the
    dispatcher's flag-merge logic)."""
    with open(path, encoding='utf-8') as f:
        lines = f.read().split('\n')

    page_of_line = [None] * len(lines)
    current_page = None
    for idx, ln in enumerate(lines):
        m = re.match(r'^<<<PDFPAGE:(\d+)>>>$', ln.strip())
        if m:
            current_page = int(m.group(1))
        page_of_line[idx] = current_page

    rows = []
    flags = []
    variant_context = None

    i = 0
    while i < len(lines):
        line = lines[i]
        columns = _pianca_shapeb_named_header_columns(line)
        if columns is not None and _pianca_wrapped_tier_letters_ahead(lines, i) is not None:
            # A wrapped Shape A table (tier letters on their own following
            # line, e.g. Chloé's 'CODICI Struttura' header) -- never a real
            # shape_b_named table despite deriving the same registry key
            # from this line alone. See _pianca_wrapped_tier_letters_ahead's
            # own docstring for the confirmed collision this guards.
            columns = None
        if columns is None:
            i += 1
            continue

        n_cols = len(columns)
        i += 1
        blank_run = 0
        while i < len(lines) and blank_run < 10:
            raw = lines[i]
            stripped = raw.strip()
            if stripped == '':
                blank_run += 1
                i += 1
                continue
            if 'CODICI' in raw and (_pianca_shapeb_named_header_columns(raw) is not None
                                     or _pianca_is_shape_a_header(raw) or _pianca_is_2axis_header(raw)):
                break  # next table's header (any shape) -- let the outer loop handle it
            blank_run = 0

            tokens = stripped.split()
            trailing = tokens[-n_cols:] if n_cols <= len(tokens) else []
            if len(tokens) <= n_cols or not all(_PIANCA_PRICE_CELL_RE.match(t) for t in trailing) or not any(t != '-' for t in trailing):
                # Not a price row -- a sub-heading (e.g. "Sedia con
                # braccioli", "Tavolo con piano laccato", "Tavolo P 80")
                # or diagram noise. Real sub-headings in this shape
                # always start with a real word (>= 3 letters); known
                # diagram noise (bare dimension numbers, single anta-type
                # letters S/D/A/C/G/R, "<N> -" range fragments) never
                # does, confirmed across every other Pianca shape's
                # diagram-bleed bugs this session -- reusing that same
                # signal rather than a digit-free-only check, since some
                # real headings here DO carry digits (e.g. "Tavolo P 80").
                if 2 < len(stripped) <= 60 and _PIANCA_SHAPEB_HEADING_RE.match(stripped):
                    variant_context = stripped
                i += 1
                continue

            pre_tokens = tokens[:-n_cols]
            code = None
            remaining = None
            if pre_tokens:
                # Wildcard-legend code (Enea Up, real PDF page 12: "T0E *
                # 09M") -- the literal printed order-code string has a
                # bare '*' placeholder in the middle, resolved per-COLUMN
                # by a legend elsewhere on the page (e.g. '*P' for L.Opaco/
                # Essenza, '*C' for Noce Canaletto). Confirmed (see the
                # dedicated Enea Up investigation in flag_triage.json)
                # that price is always resolved by (row, column) here,
                # completely independent of the wildcard -- so this
                # deliberately does NOT attempt letter resolution, just
                # preserves the exact printed '<prefix> * <suffix>' text
                # as the code, which is honest (that's what's actually
                # printed) without being wrong (no price decision depends
                # on it). Checked first, before the single-token/D-S
                # checks below, since '<3-char suffix>' alone would fail
                # _PIANCA_CODE_RE's 4-char minimum and fall through
                # silently otherwise.
                if (len(pre_tokens) >= 3 and pre_tokens[-2] == '*'
                        and re.match(r'^[A-Z0-9]{2,6}$', pre_tokens[-3])
                        and re.match(r'^[A-Z0-9]{1,6}$', pre_tokens[-1])):
                    code = f"{pre_tokens[-3]} * {pre_tokens[-1]}"
                    remaining = list(pre_tokens[:-3])
                elif len(pre_tokens) >= 2 and pre_tokens[-1] == 'D/S' and _PIANCA_CODE_RE.match(pre_tokens[-2]) and re.search(r'\d', pre_tokens[-2]):
                    code = f"{pre_tokens[-2]} D/S"
                    remaining = list(pre_tokens[:-2])
                elif _PIANCA_CODE_RE.match(pre_tokens[-1]) and re.search(r'\d', pre_tokens[-1]):
                    code = pre_tokens[-1]
                    remaining = list(pre_tokens[:-1])

            if code is None:
                i += 1
                continue

            # Dimension columns before CODICI aren't uniform across this
            # shape's whole registry -- unlike base Shape A, some tables
            # here have 3 (Elide/Onda Indoor: "L H P CODICI"), some have 2
            # (Soffio Up: "L H CODICI", confirmed to genuinely be a
            # chiuso/aperto extended-length PAIR on its "con allunga"
            # rows, not L/H at all -- e.g. "110 170 T0SA08C" = 110cm
            # closed, 170cm open), some have 1 (Mensole legno per
            # boiserie: just "H CODICI" or "L CODICI" depending on page),
            # and some have 0 (e.g. Struttura e frontali, Enea Up's
            # wildcard rows). The same generic capture (whatever real
            # numeric tokens sit closest to the code, capped at 3, in
            # their original left-to-right order) handles all of these
            # correctly without needing a per-registry-entry column count,
            # since it only ever records values that are genuinely present
            # on the row's own line -- verified byte-exact against Elide
            # (3-dim), Soffio Up (2-dim pair), and Mensole legno (1-dim).
            dims = []
            if remaining:
                while remaining and re.match(r'^\d+(\.\d+)?$', remaining[-1]):
                    popped = remaining.pop()
                    if len(dims) < 3:
                        dims.append(popped)
            dims.reverse()
            size = '×'.join(dims) if dims else None

            any_price = False
            for column_label, cell in zip(columns, trailing):
                if cell == '-':
                    continue
                any_price = True
                rows.append({
                    "brand": brand,
                    "product_name": product_name,
                    "model_variant": None,
                    "variant_context": variant_context,
                    "size": size,
                    "fabric_tier": column_label,
                    "tier_label": "Finish",
                    "code": code,
                    "price_eur": cell,
                    "source_pdf_page": page_of_line[i],
                })
            if not any_price:
                flags.append((page_of_line[i], product_name,
                              f"no price rows found for code {code}"))
            i += 1
        # continue outer loop from wherever the inner scan stopped
    return rows, flags


# ---------------------------------------------------------------------------
# Intro / Delta allungabile -- both derive the identical bare ('Basamento',)
# shape_b_named registry key from their own 'CODICI ... Basamento' header
# line, but have genuinely DIFFERENT real column counts/labels, confirmed
# via image: Intro (real PDF page 16) is 3 columns ('Laccato Opaco /
# Essenza', 'Bianco / Lavagna', 'Finiture Metallo'); Delta allungabile (real
# PDF page 34) is 2 ('Laccato Opaco / Essenza', 'Cromo Lucido'), on rows with
# an extra leading dimension column besides (L chiuso / L aperto / H, not
# the usual L/H/P). Resolved 2026-08-27, collision cluster #2. Deliberately
# NOT added to the shared flat _PIANCA_SHAPEB_NAMED_HEADERS registry at all
# -- that registry has no product-scoping mechanism, so a single
# ('Basamento',) entry could only ever be correct for one side. Each product
# instead gets its own small product_name-scoped parser sharing one row-scan
# core, same architecture as the wardrobe-danger family's shared
# `_parse_file_pianca_wardrobe`. Row convention (dims popped right-to-left
# off pre-code tokens, capped at 3; wildcard/D-S code forms; model_variant
# left unset, matching shape_b_named's own established convention of
# relying on variant_context alone) ported unchanged from shape_b_named's
# own inner loop, since it's the identical table family, just without a
# registry lookup -- both callers are already product-scoped before
# reaching it.
# ---------------------------------------------------------------------------

_PIANCA_INTRO_COLUMNS = ['Laccato Opaco / Essenza', 'Bianco / Lavagna', 'Finiture Metallo']
_PIANCA_DELTA_ALLUNGABILE_COLUMNS = ['Laccato Opaco / Essenza', 'Cromo Lucido']


def _pianca_basamento_row_scan(lines, page_of_line, product_name, brand, start_idx, columns):
    rows = []
    flags = []
    variant_context = None
    n_cols = len(columns)
    i = start_idx
    blank_run = 0
    while i < len(lines) and blank_run < 10:
        raw = lines[i]
        stripped = raw.strip()
        if stripped == '':
            blank_run += 1
            i += 1
            continue
        if 'CODICI' in raw:
            break  # next table's header -- not expected on these single-table pages, but safe
        blank_run = 0

        tokens = stripped.split()
        trailing = tokens[-n_cols:] if n_cols <= len(tokens) else []
        if len(tokens) <= n_cols or not all(_PIANCA_PRICE_CELL_RE.match(t) for t in trailing) or not any(t != '-' for t in trailing):
            if 2 < len(stripped) <= 60 and _PIANCA_SHAPEB_HEADING_RE.match(stripped):
                variant_context = stripped
            i += 1
            continue

        pre_tokens = tokens[:-n_cols]
        code = None
        remaining = None
        if pre_tokens:
            if len(pre_tokens) >= 2 and pre_tokens[-1] == 'D/S' and _PIANCA_CODE_RE.match(pre_tokens[-2]) and re.search(r'\d', pre_tokens[-2]):
                code = f"{pre_tokens[-2]} D/S"
                remaining = list(pre_tokens[:-2])
            elif _PIANCA_CODE_RE.match(pre_tokens[-1]) and re.search(r'\d', pre_tokens[-1]):
                code = pre_tokens[-1]
                remaining = list(pre_tokens[:-1])

        if code is None:
            i += 1
            continue

        dims = []
        if remaining:
            while remaining and re.match(r'^\d+(\.\d+)?$', remaining[-1]):
                popped = remaining.pop()
                if len(dims) < 3:
                    dims.append(popped)
        dims.reverse()
        size = '×'.join(dims) if dims else None

        any_price = False
        for column_label, cell in zip(columns, trailing):
            if cell == '-':
                continue
            any_price = True
            rows.append({
                "brand": brand,
                "product_name": product_name,
                "model_variant": None,
                "variant_context": variant_context,
                "size": size,
                "fabric_tier": column_label,
                "tier_label": "Finish",
                "code": code,
                "price_eur": cell,
                "source_pdf_page": page_of_line[i],
            })
        if not any_price:
            flags.append((page_of_line[i], product_name,
                          f"no price rows found for code {code}"))
        i += 1
    return rows, flags


def _parse_file_pianca_basamento(path, product_name, brand, target_name, columns):
    with open(path, encoding='utf-8') as f:
        lines = f.read().split('\n')
    page_of_line = [None] * len(lines)
    current_page = None
    for idx, ln in enumerate(lines):
        m = re.match(r'^<<<PDFPAGE:(\d+)>>>$', ln.strip())
        if m:
            current_page = int(m.group(1))
        page_of_line[idx] = current_page
    for i, ln in enumerate(lines):
        if 'CODICI' in ln and ln.split('CODICI', 1)[1].split() == ['Basamento']:
            return _pianca_basamento_row_scan(lines, page_of_line, product_name, brand, i + 1, columns)
    return [], []


def parse_file_pianca_intro(path, product_name, brand, all_headings=None, heading_text=None):
    """See module comment above for scope."""
    if product_name != 'Intro':
        return [], []
    return _parse_file_pianca_basamento(path, product_name, brand, 'Intro', _PIANCA_INTRO_COLUMNS)


def parse_file_pianca_delta_allungabile(path, product_name, brand, all_headings=None, heading_text=None):
    """See module comment above for scope."""
    if product_name != 'Delta allungabile':
        return [], []
    return _parse_file_pianca_basamento(path, product_name, brand, 'Delta allungabile', _PIANCA_DELTA_ALLUNGABILE_COLUMNS)


# ---------------------------------------------------------------------------
# Abaco / Aliseo / Baio -- collision cluster #3, resolved 2026-08-27. All
# 3 derive the identical bare ('Piano',) shape_b_named registry key from
# their own header line. A full-catalog grep for this exact key (not just
# the 4 products originally named in this cluster) found 11 files total;
# 6 already have real rows via other, already-verified parsers (Ettore,
# Fushimi x2, Inari (Tavoli consolle), Maestro, Mambo (Progetti 06-07
# Tavolino)) and were left untouched. Of the remaining 5: Abaco (real PDF
# page 22, 2 columns: 'Laccato Opaco / Cemento', 'Marmo / Terrazzo') and
# Aliseo (real PDF page 12, ALSO 2 columns, but genuinely DIFFERENT labels:
# 'Vetro Martellato', 'Gres') are a real near-miss of the exact Abaco/Scacco
# 'Top'-key shape -- same column COUNT, different real labels, which the
# row-shape safety check (validates price count only) would not catch.
# Baio (real PDF page 23) is 4 columns ('Laccato Opaco / Essenza', 'Lucido
# Sp.', 'Terrazzo', 'Marmo'), a 3rd distinct shape under the same key. None
# of these 3 can go in the shared flat registry (no product-scoping
# mechanism there), so each gets its own small product_name-scoped parser.
# Abaco's own rows carry a real leading label ('L. Opaco Bianco / Lavagna'
# vs 'L. Opaco / Fin. Metallo', the Struttura leg-finish each price row
# belongs to, confirmed via image), printed only on the FIRST of each
# 3-row dimension group and omitted on the other 2 -- same continuation
# pattern as norma_up_2axis's own struttura_finish, so it's carried
# forward across label-less rows rather than reset to None each time.
# Unlike the Intro/Delta allungabile pair above (whose own leftover token
# run is always empty) and unlike shape_b_named's own convention (which
# hardcodes model_variant=None). Aliseo/Baio have no such leading label.
#
# Soffio fisso / Soffio allungabile (real PDF pages 42-44, 45-47) are the
# 4th and 5th real shapes under this same key -- but here it WAS safe to
# add a single shared registry entry (5 columns: 'L. Opaco', 'Essenza',
# 'Fenix® Bianco/Nero', 'V. Laccato', 'V. Marmo', confirmed via image;
# L. Opaco and Essenza happen to always be priced identically, a real
# coincidence not a merged column). Verified safe against every other
# ('Piano',) key-holder found by the same grep: Abaco/Aliseo (2 real
# columns each) and Baio (4) can never produce 5 consecutive valid
# trailing price-cell tokens -- their own CODICI code token always falls
# inside a 5-wide trailing slice and never matches _PIANCA_PRICE_CELL_RE
# (verified by hand for every row shape present), so this registry entry
# only ever fires on Soffio fisso/allungabile's own genuine rows. See
# _PIANCA_SHAPEB_NAMED_HEADERS's own entry below for where it's added.
# ---------------------------------------------------------------------------

_PIANCA_ABACO_COLUMNS = ['Laccato Opaco / Cemento', 'Marmo / Terrazzo']
_PIANCA_ALISEO_COLUMNS = ['Vetro Martellato', 'Gres']
_PIANCA_BAIO_COLUMNS = ['Laccato Opaco / Essenza', 'Lucido Sp.', 'Terrazzo', 'Marmo']


def _pianca_piano_family_row_scan(lines, page_of_line, product_name, brand, start_idx, columns, capture_label=False):
    rows = []
    flags = []
    variant_context = None
    current_label = None  # carried forward across continuation rows with no label of their own -- same convention as norma_up_2axis's own struttura_finish
    n_cols = len(columns)
    i = start_idx
    blank_run = 0
    while i < len(lines) and blank_run < 10:
        raw = lines[i]
        stripped = raw.strip()
        if stripped == '':
            blank_run += 1
            i += 1
            continue
        if 'CODICI' in raw:
            break  # next table's header -- let the outer loop / caller handle it
        blank_run = 0

        tokens = stripped.split()
        trailing = tokens[-n_cols:] if n_cols <= len(tokens) else []
        if len(tokens) <= n_cols or not all(_PIANCA_PRICE_CELL_RE.match(t) for t in trailing) or not any(t != '-' for t in trailing):
            if 2 < len(stripped) <= 60 and _PIANCA_SHAPEB_HEADING_RE.match(stripped):
                variant_context = stripped
            i += 1
            continue

        pre_tokens = tokens[:-n_cols]
        code = None
        remaining = None
        if pre_tokens:
            if len(pre_tokens) >= 2 and pre_tokens[-1] == 'D/S' and _PIANCA_CODE_RE.match(pre_tokens[-2]) and re.search(r'\d', pre_tokens[-2]):
                code = f"{pre_tokens[-2]} D/S"
                remaining = list(pre_tokens[:-2])
            elif _PIANCA_CODE_RE.match(pre_tokens[-1]) and re.search(r'\d', pre_tokens[-1]):
                code = pre_tokens[-1]
                remaining = list(pre_tokens[:-1])

        if code is None:
            i += 1
            continue

        dims = []
        if remaining:
            while remaining and re.match(r'^\d+(\.\d+)?$', remaining[-1]):
                popped = remaining.pop()
                if len(dims) < 3:
                    dims.append(popped)
        dims.reverse()
        size = '×'.join(dims) if dims else None

        if capture_label:
            if remaining:
                remaining = _pianca_strip_leading_diagram_noise(remaining)
                current_label = ' '.join(remaining).strip() or current_label

        any_price = False
        for column_label, cell in zip(columns, trailing):
            if cell == '-':
                continue
            any_price = True
            rows.append({
                "brand": brand,
                "product_name": product_name,
                "model_variant": current_label if capture_label else None,
                "variant_context": variant_context,
                "size": size,
                "fabric_tier": column_label,
                "tier_label": "Finish",
                "code": code,
                "price_eur": cell,
                "source_pdf_page": page_of_line[i],
            })
        if not any_price:
            flags.append((page_of_line[i], product_name,
                          f"no price rows found for code {code}"))
        i += 1
    return rows, flags


def _parse_file_pianca_piano_family(path, product_name, brand, columns, capture_label=False):
    with open(path, encoding='utf-8') as f:
        lines = f.read().split('\n')
    page_of_line = [None] * len(lines)
    current_page = None
    for idx, ln in enumerate(lines):
        m = re.match(r'^<<<PDFPAGE:(\d+)>>>$', ln.strip())
        if m:
            current_page = int(m.group(1))
        page_of_line[idx] = current_page
    for i, ln in enumerate(lines):
        if 'CODICI' in ln and ln.split('CODICI', 1)[1].split() == ['Piano']:
            return _pianca_piano_family_row_scan(lines, page_of_line, product_name, brand, i + 1, columns, capture_label)
    return [], []


def parse_file_pianca_abaco(path, product_name, brand, all_headings=None, heading_text=None):
    """See module comment above for scope."""
    if product_name != 'Abaco':
        return [], []
    return _parse_file_pianca_piano_family(path, product_name, brand, _PIANCA_ABACO_COLUMNS, capture_label=True)


def parse_file_pianca_aliseo(path, product_name, brand, all_headings=None, heading_text=None):
    """See module comment above for scope."""
    if product_name != 'Aliseo':
        return [], []
    return _parse_file_pianca_piano_family(path, product_name, brand, _PIANCA_ALISEO_COLUMNS)


def parse_file_pianca_baio(path, product_name, brand, all_headings=None, heading_text=None):
    """See module comment above for scope."""
    if product_name != 'Baio':
        return [], []
    return _parse_file_pianca_piano_family(path, product_name, brand, _PIANCA_BAIO_COLUMNS)


# ---------------------------------------------------------------------------
# Pianca Shape B, Mambo's OWN 2-axis variant -- Mambo (Progetti di Design
# 09) only, verified against real PDF pages 28-35. Structurally similar to
# Norma Up's 2-axis grid (same code repeats across multiple row-type
# labels for one dimension, so both axes must fold into fabric_tier or
# every row collides on the shared ambiguous-detection key -- confirmed
# necessary again here, not assumed from the Norma Up precedent alone),
# but NOT the same shape: 4 columns in an ASYMMETRIC 3+1 split ("Ante e
# fianchi": L.Opaco/Essenza/Lucido Sp., then "Basamento": one combined
# L.Opaco-or-L.Metallico column), and a 4TH row-type ("Basamento") that
# isn't one of Norma Up's 3 Struttura values at all -- it's a physically
# different accessory (the leg/base component) sharing the same table
# block, with its own separate code series, populating ONLY the
# Basamento column (the other 3 always print '-' on those rows).
# Deliberately a SEPARATE function from parse_file_pianca_norma_up_2axis
# rather than a generalized shared one -- confirmed the two tables differ
# in column count/grouping/row-type set, and duplicating the (small)
# scanning logic here avoids any risk of a Mambo-specific change
# silently altering Norma Up's already-verified behavior.
# ---------------------------------------------------------------------------

_PIANCA_MAMBO_2AXIS_HEADER = ('L.', 'Opaco', 'Essenza', 'Lucido', 'Sp.', 'L.', 'Opaco')
_PIANCA_MAMBO_2AXIS_COLUMNS = [
    'Ante e fianchi — L.Opaco',
    'Ante e fianchi — Essenza',
    'Ante e fianchi — Lucido Sp./L.Metallico',
    'Basamento — L.Opaco/L.Metallico',
]


def _pianca_is_mambo_2axis_header(line: str) -> bool:
    if 'CODICI' not in line:
        return False
    tail = line.split('CODICI', 1)[1].split()
    return tuple(tail) == _PIANCA_MAMBO_2AXIS_HEADER


def _pianca_mambo_2axis_row_type(pre_tokens: list) -> str | None:
    """4 known row-type values (unlike Norma Up's 3) -- checked in an
    order where none of the 4 keyword tests can spuriously match another
    (verified: 'Basamento' contains neither 'Opaco' nor 'Materico' nor
    an 'Ess'-prefixed word, so check order among these 4 doesn't matter
    for correctness, only readability)."""
    text = ' '.join(pre_tokens)
    if any(t.startswith('Ess') for t in pre_tokens):
        return 'Essenza / L. Metallico'
    if 'Materico' in text:
        return 'Materico'
    if 'Basamento' in text:
        return 'Basamento'
    if 'Opaco' in text:
        return 'L. Opaco'
    return None


def parse_file_pianca_mambo_2axis(path, product_name, brand, all_headings=None, heading_text=None):
    """See module comment above for scope (Mambo only, verified against
    real PDF pages 28-35)."""
    with open(path, encoding='utf-8') as f:
        lines = f.read().split('\n')

    page_of_line = [None] * len(lines)
    current_page = None
    for idx, ln in enumerate(lines):
        m = re.match(r'^<<<PDFPAGE:(\d+)>>>$', ln.strip())
        if m:
            current_page = int(m.group(1))
        page_of_line[idx] = current_page

    rows = []
    flags = []
    variant_context = None
    row_type = None

    i = 0
    while i < len(lines):
        line = lines[i]
        if not _pianca_is_mambo_2axis_header(line):
            i += 1
            continue

        i += 1
        blank_run = 0
        row_type = None  # reset per table -- don't leak a prior table's last row-type
        while i < len(lines) and blank_run < 10:
            raw = lines[i]
            stripped = raw.strip()
            if stripped == '':
                blank_run += 1
                i += 1
                continue
            if _pianca_is_mambo_2axis_header(raw) or _pianca_is_shape_a_header(raw) or _pianca_is_2axis_header(raw):
                break
            blank_run = 0

            tokens = stripped.split()
            trailing = tokens[-4:]
            if len(tokens) < 5 or not all(_PIANCA_PRICE_CELL_RE.match(t) for t in trailing) or not any(t != '-' for t in trailing):
                # Same heading-detection signal as the named-columns shape
                # (real word >= 3 letters up front) -- confirmed safe
                # against this table's own diagram noise too (e.g. "55
                # 55" width-pair, "A 37" anta-letter+number, both bare-
                # digit-first, both excluded).
                if 2 < len(stripped) <= 60 and _PIANCA_SHAPEB_HEADING_RE.match(stripped):
                    variant_context = stripped
                i += 1
                continue

            pre_tokens = tokens[:-4]
            code = None
            remaining = None
            if pre_tokens:
                if len(pre_tokens) >= 2 and pre_tokens[-1] == 'D/S' and _PIANCA_CODE_RE.match(pre_tokens[-2]) and re.search(r'\d', pre_tokens[-2]):
                    code = f"{pre_tokens[-2]} D/S"
                    remaining = list(pre_tokens[:-2])
                elif _PIANCA_CODE_RE.match(pre_tokens[-1]) and re.search(r'\d', pre_tokens[-1]):
                    code = pre_tokens[-1]
                    remaining = list(pre_tokens[:-1])

            if code is None:
                i += 1
                continue

            found_type = _pianca_mambo_2axis_row_type(pre_tokens)
            if found_type is not None:
                row_type = found_type

            # H/P capture -- same "L [gap] H P CODICI" convention and same
            # confirmed H,P-inline/L-external mapping as Norma Up's own
            # 2-axis grid (see its own comment; verified via Siviglia's
            # real page image, the 3rd sibling in this family).
            dims = []
            if remaining:
                while remaining and re.match(r'^\d+(\.\d+)?$', remaining[-1]):
                    popped = remaining.pop()
                    if len(dims) < 3:
                        dims.append(popped)
            dims.reverse()
            size = '×'.join(dims) if dims else None

            any_price = False
            for column_label, cell in zip(_PIANCA_MAMBO_2AXIS_COLUMNS, trailing):
                if cell == '-':
                    continue
                any_price = True
                tier = f"{row_type} — {column_label}" if row_type else column_label
                rows.append({
                    "brand": brand,
                    "product_name": product_name,
                    "model_variant": None,
                    "variant_context": variant_context,
                    "size": size,
                    "fabric_tier": tier,
                    "tier_label": "Finish",
                    "code": code,
                    "price_eur": cell,
                    "source_pdf_page": page_of_line[i],
                })
            if not any_price:
                flags.append((page_of_line[i], product_name,
                              f"no price rows found for code {code}"))
            i += 1
        # continue outer loop from wherever the inner scan stopped
    return rows, flags


# ---------------------------------------------------------------------------
# Pianca Shape A, "+ Pelle Sint." variant -- Mambo only, verified against
# real PDF pages 37-40. Identical to the base Shape A tier grid (A/B/C/H/
# P/Q) but with a 7TH column, "Pelle Sint." (wraps to its own line below
# "Pelle" on the header, same wrapped-label pattern as several Shape B
# tables), tacked on after Q. Kept as its own function rather than
# widening _pianca_is_shape_a_header/parse_file_pianca_shape_a's 6-token
# check to accept an optional 7th -- confirmed via direct inspection this
# is Mambo-only so far, and touching the base Shape A function risks the
# same class of cross-brand regression already avoided elsewhere this
# session (e.g. the ambiguous-key widening that was confirmed unsafe for
# Bolzan/Cattelan).
# ---------------------------------------------------------------------------

_PIANCA_SHAPE_A_PELLE_TIERS = ['A', 'B', 'C', 'H', 'P', 'Q', 'Pelle Sint.']


def _pianca_is_shape_a_pelle_header(line: str) -> bool:
    if 'CODICI' not in line:
        return False
    tokens = line.split()
    return tokens[-7:] == ['A', 'B', 'C', 'H', 'P', 'Q', 'Pelle']


def parse_file_pianca_shape_a_pelle(path, product_name, brand, all_headings=None, heading_text=None):
    """See module comment above for scope."""
    with open(path, encoding='utf-8') as f:
        lines = f.read().split('\n')

    page_of_line = [None] * len(lines)
    current_page = None
    for idx, ln in enumerate(lines):
        m = re.match(r'^<<<PDFPAGE:(\d+)>>>$', ln.strip())
        if m:
            current_page = int(m.group(1))
        page_of_line[idx] = current_page

    rows = []
    flags = []
    variant_context = None

    i = 0
    while i < len(lines):
        line = lines[i]
        if not _pianca_is_shape_a_pelle_header(line):
            i += 1
            continue

        i += 1
        blank_run = 0
        while i < len(lines) and blank_run < 10:
            raw = lines[i]
            stripped = raw.strip()
            if stripped == '':
                blank_run += 1
                i += 1
                continue
            if _pianca_is_shape_a_pelle_header(raw):
                break
            blank_run = 0

            tokens = stripped.split()
            trailing = tokens[-7:]
            if len(tokens) < 8 or not all(_PIANCA_PRICE_CELL_RE.match(t) for t in trailing) or not any(t != '-' for t in trailing):
                if 2 < len(stripped) <= 60 and _PIANCA_SHAPEB_HEADING_RE.match(stripped):
                    variant_context = stripped
                i += 1
                continue

            pre_tokens = tokens[:-7]
            code = None
            label_tokens = []
            if pre_tokens:
                if len(pre_tokens) >= 2 and pre_tokens[-1] == 'D/S' and _PIANCA_CODE_RE.match(pre_tokens[-2]) and re.search(r'\d', pre_tokens[-2]):
                    code = f"{pre_tokens[-2]} D/S"
                    label_tokens = list(pre_tokens[:-2])
                elif _PIANCA_CODE_RE.match(pre_tokens[-1]) and re.search(r'\d', pre_tokens[-1]):
                    code = pre_tokens[-1]
                    label_tokens = list(pre_tokens[:-1])

            if code is None:
                i += 1
                continue

            # Same L/H/P capture as base Shape A -- see its own comment
            # for the full rationale (prefix-of-[L,H,P] rule, verified
            # against Duo's real page image). Ported unchanged since this
            # is the exact same row convention with only the trailing
            # tier-column count differing (7 vs 6).
            dims = []
            while label_tokens and re.match(r'^\d+(\.\d+)?$', label_tokens[-1]):
                popped = label_tokens.pop()
                if len(dims) < 3:
                    dims.append(popped)
            dims.reverse()
            size = '×'.join(dims) if dims else None
            # Same leading-diagram-noise risk as the base Shape A parser
            # (see _pianca_strip_leading_diagram_noise) -- this variant
            # never had ANY leading-noise protection at all. Not yet
            # confirmed to have actually corrupted any of Mambo/Siviglia's
            # rows (audited 2026-08-21, none of their Category-tier
            # model_variant values matched the noise shape), but the
            # structural risk is identical, so applying the same fix
            # protectively rather than waiting for it to surface here too.
            label_tokens = _pianca_strip_leading_diagram_noise(label_tokens)
            label = ' '.join(label_tokens).strip() or None

            any_price = False
            for letter, cell in zip(_PIANCA_SHAPE_A_PELLE_TIERS, trailing):
                if cell == '-':
                    continue
                any_price = True
                rows.append({
                    "brand": brand,
                    "product_name": product_name,
                    "model_variant": label,
                    "variant_context": variant_context,
                    "size": size,
                    "fabric_tier": letter,
                    "tier_label": "Category",
                    "code": code,
                    "price_eur": cell,
                    "source_pdf_page": page_of_line[i],
                })
            if not any_price:
                flags.append((page_of_line[i], product_name,
                              f"no price rows found for code {code}"))
            i += 1
        # continue outer loop from wherever the inner scan stopped
    return rows, flags


# ---------------------------------------------------------------------------
# Pianca Shape A, "+ Anilina/Essenza" mixed variant -- Cora (CollezioneGiorno
# Sedie) only, verified against real PDF page 10. Structurally the SAME
# base Shape A tier grid (A/B/C/H/P/Q) with 2 EXTRA leading named-finish
# columns (Anilina, Essenza) -- same "extra flat column(s) tacked onto the
# base 6-tier signature" shape as Mambo's own "+ Pelle Sint." variant just
# above, only Cora's 2 extra columns sit BEFORE the tier letters (header
# reads "Anilina Essenza A B C H P Q") rather than after, and are used
# MUTUALLY EXCLUSIVELY per row rather than always-populated: confirmed on
# both of Cora's own row-type pairs (art. 01173/01174 "seduta legno" rows
# populate ONLY the 2 named columns, dash-filling all 6 tier cells; art.
# 01198/01199 "seduta rivestita" rows populate ONLY the 6 tier cells,
# dash-filling both named columns) -- the shared trailing-N-token/dash
# convention this whole file's Shape A family already relies on handles
# this correctly with no extra logic: whichever group is genuinely priced
# simply has real digits, the other group's cells are '-' and get skipped
# by the same "cell == '-': continue" check every other Shape A variant
# already uses.
#
# Kept as its own function, not a widening of the base Shape A/Pelle
# checks, for the same reason as every other Shape A variant in this file:
# confirmed Cora-only so far (checked every other CollezioneGiorno Sedie
# product -- Alunna/Emi/Esse/Gamma/Inari -- individually against their own
# real source pages before building this, none of them have this shape;
# see the base Shape A comment for why touching that function's own check
# is the wrong place for a not-yet-confirmed-general pattern).
#
# One real structural difference from the Pelle variant that DOES need
# its own header check (not just a widened token count): Cora's own
# header line does NOT contain the literal word "CODICI" at all -- on
# this page, "CODICI" prints on a SEPARATE physical line ("L H P CODICI
# ... Seduta") from the actual tier-letter header row ("Anilina Essenza
# A B C H P Q"), confirmed via direct line-by-line inspection of the
# stored text. Every other Shape A variant's own header check requires
# 'CODICI' on the SAME line specifically because that's how each of
# THEIR OWN real headers actually print -- it was never a hard invariant
# of the shape family itself, just what happened to be true for the
# shapes seen before this one. The row-level correctness (a real code
# token, real price cells) doesn't depend on where "CODICI" printed at
# all, so this check is safely narrower without it.
# ---------------------------------------------------------------------------

_PIANCA_CORA_TIERS = ['Anilina', 'Essenza', 'A', 'B', 'C', 'H', 'P', 'Q']


def _pianca_is_cora_header(line: str) -> bool:
    tokens = line.split()
    return tokens[-8:] == ['Anilina', 'Essenza', 'A', 'B', 'C', 'H', 'P', 'Q']


def parse_file_pianca_cora(path, product_name, brand, all_headings=None, heading_text=None):
    """See module comment above for scope."""
    with open(path, encoding='utf-8') as f:
        lines = f.read().split('\n')

    page_of_line = [None] * len(lines)
    current_page = None
    for idx, ln in enumerate(lines):
        m = re.match(r'^<<<PDFPAGE:(\d+)>>>$', ln.strip())
        if m:
            current_page = int(m.group(1))
        page_of_line[idx] = current_page

    rows = []
    flags = []
    variant_context = None

    i = 0
    while i < len(lines):
        line = lines[i]
        if not _pianca_is_cora_header(line):
            i += 1
            continue

        i += 1
        blank_run = 0
        while i < len(lines) and blank_run < 10:
            raw = lines[i]
            stripped = raw.strip()
            if stripped == '':
                blank_run += 1
                i += 1
                continue
            if _pianca_is_cora_header(raw):
                break
            blank_run = 0

            tokens = stripped.split()
            trailing = tokens[-8:]
            if len(tokens) < 9 or not all(_PIANCA_PRICE_CELL_RE.match(t) for t in trailing) or not any(t != '-' for t in trailing):
                if 2 < len(stripped) <= 60 and _PIANCA_SHAPEB_HEADING_RE.match(stripped):
                    variant_context = stripped
                i += 1
                continue

            pre_tokens = tokens[:-8]
            code = None
            label_tokens = []
            if pre_tokens:
                if len(pre_tokens) >= 2 and pre_tokens[-1] == 'D/S' and _PIANCA_CODE_RE.match(pre_tokens[-2]) and re.search(r'\d', pre_tokens[-2]):
                    code = f"{pre_tokens[-2]} D/S"
                    label_tokens = list(pre_tokens[:-2])
                elif _PIANCA_CODE_RE.match(pre_tokens[-1]) and re.search(r'\d', pre_tokens[-1]):
                    code = pre_tokens[-1]
                    label_tokens = list(pre_tokens[:-1])

            if code is None:
                i += 1
                continue

            # Same L/H/P capture as base Shape A -- see its own comment for
            # the full rationale. Confirmed clean 3-token L/H/P on both of
            # Cora's own row types (source cora.txt: "seduta legno 45 80
            # 50 01173 ..." and "seduta rivestita 45 80 50 01198 ...") --
            # this is purely the tier-column handling that differs from
            # base Shape A, not the dimension-column convention.
            dims = []
            while label_tokens and re.match(r'^\d+(\.\d+)?$', label_tokens[-1]):
                popped = label_tokens.pop()
                if len(dims) < 3:
                    dims.append(popped)
            dims.reverse()
            size = '×'.join(dims) if dims else None
            label_tokens = _pianca_strip_leading_diagram_noise(label_tokens)
            label = ' '.join(label_tokens).strip() or None

            any_price = False
            for letter, cell in zip(_PIANCA_CORA_TIERS, trailing):
                if cell == '-':
                    continue
                any_price = True
                rows.append({
                    "brand": brand,
                    "product_name": product_name,
                    "model_variant": label,
                    "variant_context": variant_context,
                    "size": size,
                    "fabric_tier": letter,
                    "tier_label": "Category",
                    "code": code,
                    "price_eur": cell,
                    "source_pdf_page": page_of_line[i],
                })
            if not any_price:
                flags.append((page_of_line[i], product_name,
                              f"no price rows found for code {code}"))
            i += 1
        # continue outer loop from wherever the inner scan stopped
    return rows, flags


# ---------------------------------------------------------------------------
# Pianca "Tavoli consolle" family -- found 2026-08-24 while resolving the
# Inari name collision (see extract_catalog.py's own PIANCA_INDEX_NAME_
# OVERRIDES history: "Inari (Sedie)" and "Inari (Tavoli consolle)" were
# ALREADY correctly disambiguated at extraction time; only their PRICE
# tables were never parsed). Verified real content/codes on 3 products
# (Fushimi, Inari, Mono) across both their "Tavolini" and "Tavoli
# consolle" pages before building anything -- 2 genuinely distinct table
# shapes recur across this whole product family, not just Inari:
#
#   1) A 3-column TOP-material grid ("...CODICI ... Piano" header,
#      confirmed on Fushimi's own 2 pages AND Inari's own page) --
#      Fushimi/Inari, wood-or-glass-top tables.
#   2) A 2-column STRUCTURE-finish grid ("...CODICI Laccato Opaco Corten"
#      header, confirmed on Mono's own 2 pages) -- Mono, metal-only
#      tables (no wood/glass top option at all, hence no "Piano" split).
#
# Row-scan/code-detection/dash-skip logic is the SAME base-Shape-A
# convention as every other variant in this file. Sub-heading lines
# ("Tavolo quadrato" / "Consolle" / "In massello di noce Canaletto" /
# "L 30" etc.) carry the real distinguishing category via the SAME
# shared variant_context mechanism every Pianca parser already uses --
# confirmed real (e.g. Fushimi's own 2 "Consolle" sub-groups, one per
# wood species, are only distinguishable via this text, not the row
# itself). One narrow, LOCAL addition on top of the shared pattern: a
# candidate variant_context line is rejected if it contains '€' --
# confirmed real without this, a trailing surcharge note several lines
# into Fushimi's own wrapped multi-line description ("Maggiorazione per
# struttura Laccato Lucido + € 865") would otherwise win the "last short
# line before the data row" race and become the displayed category
# instead of the real one. Scoped to these 2 new functions only, not the
# shared _PIANCA_SHAPEB_HEADING_RE-based check other Pianca parsers
# already rely on -- touching that shared check risks changing already-
# verified behavior across every other Pianca product using it.
#
# Tier labels are deliberately POSITIONAL ("Top 1"/"Top 2"/"Top 3") for
# the 3-column shape, not the exact printed material names -- confirmed
# via direct inspection that -layout linearization wraps those names
# across 2 physical lines with prose text bleeding in from the LEFT
# column on the same lines (e.g. Fushimi's own header: "Laccato Opaco /
# Lucido Sp. / Marmo" on one line, "Essenza / Terrazzo" wrapping below --
# genuinely ambiguous which wrapped fragment belongs to which column
# without risking a guess). PRICE VALUES and their COLUMN POSITIONS are
# fully unambiguous and verified exact; only the column's own display
# NAME is deliberately generic rather than guessed. Mono's own 2 column
# names ("Laccato Opaco", "Corten") ARE unambiguous -- both print cleanly
# on one line with no wrapping -- so those use the real printed names.
# ---------------------------------------------------------------------------

_PIANCA_TAVOLI_PIANO_TIERS = ['Top 1', 'Top 2', 'Top 3']
_PIANCA_TAVOLI_METALLO_TIERS = ['Laccato Opaco', 'Corten']


def _pianca_is_tavoli_piano_header(line: str) -> bool:
    tokens = line.split()
    return tokens[-2:] == ['CODICI', 'Piano']


def _pianca_is_tavoli_metallo_header(line: str) -> bool:
    if 'CODICI' not in line:
        return False
    tail = line.split('CODICI', 1)[1].split()
    return tuple(tail) == ('Laccato', 'Opaco', 'Corten')


_PIANCA_TAVOLI_L_HEADING_RE = re.compile(r'^L\s+\d+$')


def _pianca_tavoli_variant_context_candidate(stripped: str) -> bool:
    # Mono's own size-family sub-headings are bare "L 30"/"L 50"/"L 80"/
    # "L 100"/"L 130" (confirmed via direct inspection of the stored
    # text, always exactly this shape) -- rejected by the shared
    # _PIANCA_SHAPEB_HEADING_RE (requires 3+ leading letters; "L" alone
    # is only 1), which silently left every Mono row's variant_context as
    # None. Added as its own narrow, LOCAL exception rather than widening
    # the shared regex every other Pianca parser also relies on.
    if _PIANCA_TAVOLI_L_HEADING_RE.match(stripped):
        return True
    return 2 < len(stripped) <= 60 and '€' not in stripped and bool(_PIANCA_SHAPEB_HEADING_RE.match(stripped))


def _parse_file_pianca_tavoli_shared(path, product_name, brand, is_header, trailing_count, tiers, tier_label):
    """Shared row-scan for both Tavoli consolle/Tavolini shapes -- see
    module comment above for why they need their own header checks but
    otherwise reuse the exact same base Shape A convention as every other
    variant in this file."""
    with open(path, encoding='utf-8') as f:
        lines = f.read().split('\n')

    page_of_line = [None] * len(lines)
    current_page = None
    for idx, ln in enumerate(lines):
        m = re.match(r'^<<<PDFPAGE:(\d+)>>>$', ln.strip())
        if m:
            current_page = int(m.group(1))
        page_of_line[idx] = current_page

    rows = []
    flags = []
    variant_context = None

    i = 0
    while i < len(lines):
        line = lines[i]
        if not is_header(line):
            i += 1
            continue

        i += 1
        blank_run = 0
        while i < len(lines) and blank_run < 10:
            raw = lines[i]
            stripped = raw.strip()
            if stripped == '':
                blank_run += 1
                i += 1
                continue
            if is_header(raw):
                break
            blank_run = 0

            tokens = stripped.split()
            trailing = tokens[-trailing_count:]
            if len(tokens) < trailing_count + 1 or not all(_PIANCA_PRICE_CELL_RE.match(t) for t in trailing) or not any(t != '-' for t in trailing):
                if _pianca_tavoli_variant_context_candidate(stripped):
                    variant_context = stripped
                i += 1
                continue

            pre_tokens = tokens[:-trailing_count]
            code = None
            label_tokens = []
            if pre_tokens and _PIANCA_CODE_RE.match(pre_tokens[-1]) and re.search(r'\d', pre_tokens[-1]):
                code = pre_tokens[-1]
                label_tokens = list(pre_tokens[:-1])

            if code is None:
                i += 1
                continue

            # Same L/H/P capture as base Shape A -- see its own comment for
            # the full rationale. The FIRST 3 pops (nearest the code) are
            # the real dimension columns -- confirmed against Mono's own
            # page 29/30 24MCC4 row ("40 / 50 ... 30 40 30 24MCC4 460
            # 506"): the "40 / 50" fragment is a NEARBY diagram caption
            # that bleeds onto this line from further left, so it's always
            # further from the code than the row's own real L/H/P, and the
            # cap-at-3 rule already excludes it the same way it excludes
            # any extra noise on the base Shape A parser.
            dims = []
            while label_tokens and re.match(r'^\d+(\.\d+)?$', label_tokens[-1]):
                popped = label_tokens.pop()
                if len(dims) < 3:
                    dims.append(popped)
            dims.reverse()
            size = '×'.join(dims) if dims else None
            label_tokens = _pianca_strip_leading_diagram_noise(label_tokens)
            # A dimension-diagram caption (a "/"-separated list of
            # alternate depth/height options, e.g. "40 / 50" or "20 / 30
            # / 40 / 50 / 60 / 75") routinely bleeds onto the SAME
            # physical line as a real price row via -layout linearization
            # -- confirmed on Mono's own page 29/30 (e.g. "24MCC4"'s real
            # row shares a line with the "40 / 50" caption for a NEARBY
            # diagram, unrelated to this row's own dimensions). The
            # existing numeric-tail strip above removes the trailing
            # digit run but can't remove digits interleaved with "/"
            # separators, leaving a stray "/" or "/ 30 / 40 / 50 / 60 /"
            # masquerading as a real label. Safe to discard outright: a
            # genuine label for this shape always contains real
            # descriptive words (confirmed across every row checked), so
            # leftover tokens that are ENTIRELY digits/slashes/dots can
            # only be exactly this noise, never a real label.
            if label_tokens and all(re.match(r'^[\d./]+$', t) for t in label_tokens):
                label_tokens = []
            label = ' '.join(label_tokens).strip() or None

            any_price = False
            for col, cell in zip(tiers, trailing):
                if cell == '-':
                    continue
                any_price = True
                rows.append({
                    "brand": brand,
                    "product_name": product_name,
                    "model_variant": label,
                    "variant_context": variant_context,
                    "size": size,
                    "fabric_tier": col,
                    "tier_label": tier_label,
                    "code": code,
                    "price_eur": cell,
                    "source_pdf_page": page_of_line[i],
                })
            if not any_price:
                flags.append((page_of_line[i], product_name,
                              f"no price rows found for code {code}"))
            i += 1
        # continue outer loop from wherever the inner scan stopped
    return rows, flags


def parse_file_pianca_tavoli_piano(path, product_name, brand, all_headings=None, heading_text=None):
    """See module comment above for scope."""
    return _parse_file_pianca_tavoli_shared(
        path, product_name, brand,
        _pianca_is_tavoli_piano_header, 3, _PIANCA_TAVOLI_PIANO_TIERS, "Piano")


def parse_file_pianca_tavoli_metallo(path, product_name, brand, all_headings=None, heading_text=None):
    """See module comment above for scope."""
    return _parse_file_pianca_tavoli_shared(
        path, product_name, brand,
        _pianca_is_tavoli_metallo_header, 2, _PIANCA_TAVOLI_METALLO_TIERS, "Struttura")


# ---------------------------------------------------------------------------
# Pianca Shape B, Siviglia's OWN symmetric 4x4 finish MATRIX -- Siviglia
# (Progetti di Design 09) only, verified against real PDF pages 71-77. A
# third distinct 2-axis variant: unlike Norma Up (6 cols/2 groups, 3 row-
# types) and Mambo (4 cols/asymmetric 3+1 groups, 4 row-types including
# one physically different accessory), Siviglia's row-type set and column
# set are the EXACT SAME 4 finishes (Materico/L.Opaco/Essenza-PoroAperto/
# LucidoSp-LMetallico) -- confirmed real: one shared code per (L,P)
# dimension repeats across all 4 row-types (e.g. code 00J4G8 prints on
# all 4 rows of its group), same collision risk as the other two 2-axis
# grids, so both axes fold into fabric_tier here too.
# ---------------------------------------------------------------------------

_PIANCA_SIVIGLIA_MATRIX_HEADER = ('Materico', 'L.', 'Opaco', 'Essenza', 'Lucido', 'Sp.')
_PIANCA_SIVIGLIA_MATRIX_COLUMNS = ['Materico', 'L. Opaco', 'Essenza / Poro aperto', 'Lucido Sp. / L. Metallico']


def _pianca_is_siviglia_matrix_header(line: str) -> bool:
    if 'CODICI' not in line:
        return False
    tail = line.split('CODICI', 1)[1].split()
    return tuple(tail) == _PIANCA_SIVIGLIA_MATRIX_HEADER


def _pianca_siviglia_matrix_row_type(pre_tokens: list) -> str | None:
    """4 row-type keywords, all mutually exclusive substrings (verified:
    'Lucido Sp. / L. Metallico' contains none of 'Ess'/'Materico'/
    'Opaco'; 'Essenza / Poro aperto' starts with 'Ess' but contains
    neither 'Materico' nor 'Lucido' nor 'Opaco'), so check order doesn't
    affect correctness."""
    text = ' '.join(pre_tokens)
    if any(t.startswith('Ess') for t in pre_tokens):
        return 'Essenza / Poro aperto'
    if 'Materico' in text:
        return 'Materico'
    if 'Lucido' in text:
        return 'Lucido Sp. / L. Metallico'
    if 'Opaco' in text:
        return 'L. Opaco'
    return None


def parse_file_pianca_siviglia_matrix(path, product_name, brand, all_headings=None, heading_text=None):
    """See module comment above for scope (Siviglia only, verified
    against real PDF pages 71-77)."""
    with open(path, encoding='utf-8') as f:
        lines = f.read().split('\n')

    page_of_line = [None] * len(lines)
    current_page = None
    for idx, ln in enumerate(lines):
        m = re.match(r'^<<<PDFPAGE:(\d+)>>>$', ln.strip())
        if m:
            current_page = int(m.group(1))
        page_of_line[idx] = current_page

    rows = []
    flags = []
    variant_context = None
    row_type = None

    i = 0
    while i < len(lines):
        line = lines[i]
        if not _pianca_is_siviglia_matrix_header(line):
            i += 1
            continue

        i += 1
        blank_run = 0
        row_type = None  # reset per table -- don't leak a prior table's last row-type
        while i < len(lines) and blank_run < 10:
            raw = lines[i]
            stripped = raw.strip()
            if stripped == '':
                blank_run += 1
                i += 1
                continue
            if _pianca_is_siviglia_matrix_header(raw) or _pianca_is_shape_a_header(raw) or _pianca_is_2axis_header(raw):
                break
            blank_run = 0

            tokens = stripped.split()
            trailing = tokens[-4:]
            if len(tokens) < 5 or not all(_PIANCA_PRICE_CELL_RE.match(t) for t in trailing) or not any(t != '-' for t in trailing):
                if 2 < len(stripped) <= 60 and _PIANCA_SHAPEB_HEADING_RE.match(stripped):
                    variant_context = stripped
                i += 1
                continue

            pre_tokens = tokens[:-4]
            code = None
            remaining = None
            if pre_tokens:
                if len(pre_tokens) >= 2 and pre_tokens[-1] == 'D/S' and _PIANCA_CODE_RE.match(pre_tokens[-2]) and re.search(r'\d', pre_tokens[-2]):
                    code = f"{pre_tokens[-2]} D/S"
                    remaining = list(pre_tokens[:-2])
                elif _PIANCA_CODE_RE.match(pre_tokens[-1]) and re.search(r'\d', pre_tokens[-1]):
                    code = pre_tokens[-1]
                    remaining = list(pre_tokens[:-1])

            if code is None:
                i += 1
                continue

            found_type = _pianca_siviglia_matrix_row_type(pre_tokens)
            if found_type is not None:
                row_type = found_type

            # H/P capture -- confirmed via direct pixel-level inspection
            # of this exact product's own real page image
            # (siviglia_p71-71.jpg): the section-level heading number
            # ("81"/"162" etc, captured separately as variant_context) is
            # L, and the 2 inline numbers on each row's own line (e.g.
            # "129 49" before 00J4G8) are H then P -- L itself never
            # appears on the row's own line so it's never captured here.
            dims = []
            if remaining:
                while remaining and re.match(r'^\d+(\.\d+)?$', remaining[-1]):
                    popped = remaining.pop()
                    if len(dims) < 3:
                        dims.append(popped)
            dims.reverse()
            size = '×'.join(dims) if dims else None

            any_price = False
            for column_label, cell in zip(_PIANCA_SIVIGLIA_MATRIX_COLUMNS, trailing):
                if cell == '-':
                    continue
                any_price = True
                tier = f"{row_type} — {column_label}" if row_type else column_label
                rows.append({
                    "brand": brand,
                    "product_name": product_name,
                    "model_variant": None,
                    "variant_context": variant_context,
                    "size": size,
                    "fabric_tier": tier,
                    "tier_label": "Finish",
                    "code": code,
                    "price_eur": cell,
                    "source_pdf_page": page_of_line[i],
                })
            if not any_price:
                flags.append((page_of_line[i], product_name,
                              f"no price rows found for code {code}"))
            i += 1
        # continue outer loop from wherever the inner scan stopped
    return rows, flags


# ---------------------------------------------------------------------------
# Pianca Shape D -- bare "CODICI / Prezzo" flat single-price accessory
# tables. Verified on Mambo (kit-luce accessories, real PDF page 35) and
# structurally the simplest possible Pianca shape: one label, one code,
# one price, no finish dimension at all.
#
# ALSO matches a bare "CODICI" tail with NOTHING after it on the same line
# (2026-08-25, found via the full known_gap shape inventory: 121 products,
# confirmed via direct row inspection to always carry exactly one trailing
# price cell per row -- e.g. Contralto (CollezioneGiorno): "38 65 38 35LTA
# ... 773"). This is deliberately NOT the same signature as a genuinely
# wider table whose OWN column labels simply wrapped onto a different
# physical line (confirmed real and structurally different: Icaro's own
# "L H P CODICI" line is also bare on its own line, but its real 2 column
# names print on the NEXT line, and its rows carry 2 trailing prices, not
# 1) -- the header text alone cannot tell these apart, since both print
# nothing after CODICI on that exact line. Safety is enforced in the row
# scan below instead, not the header check: a row is only ever accepted
# here if it has EXACTLY ONE trailing price-cell token, never more --
# widening the header without this would have silently glommed a genuine
# 2nd+ price value into the label as text on every Icaro-shaped table
# sharing this header signature, corrupting real price data rather than
# just leaving it correctly unrecognized.
# ---------------------------------------------------------------------------

_PIANCA_FLAT_PRICE_HEADER = ('Prezzo',)


def _pianca_is_flat_price_header(line: str) -> bool:
    if 'CODICI' not in line:
        return False
    tail = line.split('CODICI', 1)[1].split()
    return tuple(tail) == _PIANCA_FLAT_PRICE_HEADER or len(tail) == 0


def parse_file_pianca_flat_price(path, product_name, brand, all_headings=None, heading_text=None):
    """See module comment above for scope."""
    with open(path, encoding='utf-8') as f:
        lines = f.read().split('\n')

    page_of_line = [None] * len(lines)
    current_page = None
    for idx, ln in enumerate(lines):
        m = re.match(r'^<<<PDFPAGE:(\d+)>>>$', ln.strip())
        if m:
            current_page = int(m.group(1))
        page_of_line[idx] = current_page

    rows = []
    flags = []
    variant_context = None

    i = 0
    while i < len(lines):
        line = lines[i]
        if not _pianca_is_flat_price_header(line):
            i += 1
            continue

        i += 1
        blank_run = 0
        while i < len(lines) and blank_run < 10:
            raw = lines[i]
            stripped = raw.strip()
            if stripped == '':
                blank_run += 1
                i += 1
                continue
            if _pianca_is_flat_price_header(raw) or _pianca_is_shape_a_header(raw) or _pianca_is_2axis_header(raw) \
                    or _pianca_is_mambo_2axis_header(raw) or _pianca_shapeb_named_header_columns(raw) is not None:
                break
            blank_run = 0

            tokens = stripped.split()
            if len(tokens) < 2 or not _PIANCA_PRICE_CELL_RE.match(tokens[-1]) or tokens[-1] == '-':
                if 2 < len(stripped) <= 60 and _PIANCA_SHAPEB_HEADING_RE.match(stripped):
                    variant_context = stripped
                i += 1
                continue

            # Reject anything but a GENUINE single-price row -- see the
            # module comment above for why (distinguishes this shape from
            # a wider table sharing the same bare-CODICI header text).
            # Checked via _PIANCA_CODE_RE directly, not a hand-rolled
            # length floor: an EARLIER version of this check rejected only
            # tokens shorter than 4 characters, reasoning that a valid
            # code can never be that short -- true, but incomplete, and
            # confirmed to have shipped a real data-corruption bug: Amante
            # (a real 6-tier "Letti" bed product, code WAAW35S, prices
            # like "3.710") was being partially captured by THIS shape
            # too, because "3.710" is 5 characters -- long enough to slip
            # past the old length check even though it's obviously a
            # price, not a code (real codes in this catalog, confirmed
            # across every example seen, NEVER contain the "." thousands-
            # separator punctuation Italian price formatting always uses
            # once a value reaches 1.000+). Fixed by reusing
            # _PIANCA_CODE_RE directly instead of reimplementing its own
            # length rule: anything that's price-shaped AND fails real
            # code validation (whether because it's too short, like a
            # bare "72", or because it contains a "." a code can never
            # have, like "3.710") is unambiguously a genuine second price,
            # never a code -- safe to reject either way. Still correctly
            # preserves Mambo/Siviglia's own real numeric codes (47101 etc
            # -- pure digits, no period, 5 chars, passes CODE_RE cleanly).
            if len(tokens) >= 2 and _PIANCA_PRICE_CELL_RE.match(tokens[-2]) and not _PIANCA_CODE_RE.match(tokens[-2]):
                i += 1
                continue

            pre_tokens = tokens[:-1]
            code = None
            if pre_tokens and _PIANCA_CODE_RE.match(pre_tokens[-1]) and re.search(r'\d', pre_tokens[-1]):
                code = pre_tokens[-1]

            if code is None:
                i += 1
                continue

            label_tokens = list(pre_tokens[:-1])
            # Dimension columns before CODICI aren't uniform here either
            # (some flat-price tables have "L H P CODICI Prezzo", e.g.
            # Geometrika; some have a single "L CODICI Prezzo", e.g. Luce
            # Illumia; some have none at all, just "CODICI Prezzo") -- same
            # generic capped capture as shape_b_named. This ALSO fixes a
            # real pre-existing bug found while adding this: with no
            # digit-stripping at all before, Geometrika's own label was
            # silently polluted with its trailing dimension digits (e.g.
            # "con luce LED 7.7 W 80 70 2.6" instead of the real "con luce
            # LED 7.7 W"), not just missing a size value.
            dims = []
            while label_tokens and re.match(r'^\d+(\.\d+)?$', label_tokens[-1]):
                popped = label_tokens.pop()
                if len(dims) < 3:
                    dims.append(popped)
            dims.reverse()
            size = '×'.join(dims) if dims else None

            # Bed-style "WxH" nominal size (e.g. "160x200") and its own
            # "min/max" range companion (e.g. "176/218") aren't pure-digit
            # tokens, so the strip above leaves them stuck in the label --
            # confirmed real on Amante's own "plissé" surcharge table
            # (its own separate, single-price sub-table alongside the
            # main 6-tier one parse_file_pianca_letti_tier already
            # handles): label was coming out as "105 160x200 176/218"
            # instead of a clean None, with the real size hidden in the
            # label text. Same WxH pattern/precedent as
            # parse_file_pianca_letti_tier -- prefer it over the plain
            # dims-derived size when both are present, since it's the
            # customer-recognizable nominal size ("give me the 160x200
            # price") that extractSize() in catalogChat.ts already
            # matches, not an internal manufacturing figure.
            wxh = None
            for t in label_tokens:
                if _PIANCA_WXH_SIZE_RE.match(t):
                    wxh = t
                    break
            if wxh:
                size = wxh
            # Any leftover token that's ENTIRELY digits/slashes/dots (the
            # WxH match itself, plus its own range companion and any bare
            # group-heading number bleeding in from a nearby diagram) is
            # noise once a real WxH size has been found -- same "no real
            # label is ever just digits and slashes" reasoning already
            # proven for the Tavoli shape's own caption-noise fix.
            if wxh:
                label_tokens = [t for t in label_tokens if not re.match(r'^[\d./x]+$', t)]
            label = ' '.join(label_tokens).strip() or None

            rows.append({
                "brand": brand,
                "product_name": product_name,
                "model_variant": label,
                "variant_context": variant_context,
                "size": size,
                "fabric_tier": None,
                "tier_label": None,
                "code": code,
                "price_eur": tokens[-1],
                "source_pdf_page": page_of_line[i],
            })
            i += 1
        # continue outer loop from wherever the inner scan stopped
    return rows, flags


# ---------------------------------------------------------------------------
# Pianca, Primo's dimension-labeled single-column shape -- ArmadioPrimo
# (single-product file, no per-product photographic INDICE at all -- see
# --single-product in extract_catalog.py's main()), real PDF pages 6-7
# ("Composizioni battenti" and "Accessori interni" tables). Header is
# '<dim-letter> CODICI / P 59 / Materico' -- verified via direct page-
# image inspection (primo_p6-6.jpg, primo_p7-7.jpg) to be the simplest
# possible Pianca table shape: ONE named finish column ("Materico"), one
# code per row, no row-type-axis repeat (every code in both tables
# confirmed to appear exactly once). NOT folded into the generic Shape B
# named-columns registry (_PIANCA_SHAPEB_NAMED_HEADERS) despite the
# single-token 'Materico' tail matching that mechanism's shape, because
# each row ALSO prints a leading dimension value (H 238.5/257.7 for
# Composizioni battenti, L 48/98.5 for Accessori interni) on the SAME
# line as the code+price -- the generic parser has no field for this and
# would silently drop it. Codes are already fully distinct per dimension
# value here (AA701 vs AA801, no collision risk either way), but
# dropping a real printed dimension a user might ask about is worse than
# the small cost of a dedicated function -- captured into `size`.
# ---------------------------------------------------------------------------

_PIANCA_PRIMO_DIM_HEADER_TAIL = ('Materico',)


def _pianca_is_primo_dim_header(line: str) -> bool:
    if 'CODICI' not in line:
        return False
    if _pianca_is_shape_a_header(line) or _pianca_is_2axis_header(line):
        return False
    if _pianca_shapeb_named_header_columns(line) is not None:
        return False  # already claimed by the generic named-columns registry
    tail = line.split('CODICI', 1)[1].split()
    return tuple(tail) == _PIANCA_PRIMO_DIM_HEADER_TAIL


def parse_file_pianca_primo_dim_labeled(path, product_name, brand, all_headings=None, heading_text=None):
    """See module comment above for scope."""
    with open(path, encoding='utf-8') as f:
        lines = f.read().split('\n')

    page_of_line = [None] * len(lines)
    current_page = None
    for idx, ln in enumerate(lines):
        m = re.match(r'^<<<PDFPAGE:(\d+)>>>$', ln.strip())
        if m:
            current_page = int(m.group(1))
        page_of_line[idx] = current_page

    rows = []
    flags = []
    variant_context = None

    i = 0
    while i < len(lines):
        line = lines[i]
        if not _pianca_is_primo_dim_header(line):
            i += 1
            continue

        i += 1
        blank_run = 0
        while i < len(lines) and blank_run < 10:
            raw = lines[i]
            stripped = raw.strip()
            if stripped == '':
                blank_run += 1
                i += 1
                continue
            if 'CODICI' in raw and (_pianca_is_primo_dim_header(raw)
                                     or _pianca_shapeb_named_header_columns(raw) is not None
                                     or _pianca_is_shape_a_header(raw) or _pianca_is_2axis_header(raw)):
                break  # next table's header (any shape) -- let the outer loop handle it
            blank_run = 0

            # A real price row's last 3 tokens are always "<dim> <CODE>
            # <price>" -- but pdftotext -layout's linearization interleaves
            # the module-width diagram annotations (e.g. "53.0   48") onto
            # the SAME output line as the row that happens to sit at that
            # vertical position, ahead of the real dim/code/price triple.
            # Reading from the END rather than requiring an exact token
            # count handles both the clean first-row-of-pair ("238.5
            # AA701 356", 3 tokens) and the noisy second-row-of-pair
            # ("53.0 48 257.7 AA801 370", 5+ tokens) uniformly.
            tokens = stripped.split()
            if len(tokens) < 3:
                dim = code = price = None
            else:
                dim, code, price = tokens[-3:]
            valid_dim = dim is not None and re.match(r'^\d+(\.\d+)?$', dim)
            valid_code = code is not None and _PIANCA_CODE_RE.match(code) and re.search(r'\d', code)
            valid_price = price is not None and _PIANCA_PRICE_CELL_RE.match(price)
            if not (valid_dim and valid_code and valid_price):
                # Not a price row -- a sub-heading (e.g. "Ripiani lineari
                # legno Sp 2.5 cm", "Cassettiera H 42 a 2 cassetti") or
                # leftover Maniglie-spec preamble text bleeding in from
                # the facing description column ("Finiture maniglie",
                # "Laccato Opaco (Bianco, Seta, Ecrù)" -- both otherwise
                # shape-match the generic heading regex below, confirmed
                # by direct text inspection of primo.txt). Real sub-
                # headings here never end in ')' (a finish/color
                # parenthetical) and never start with "Finiture" (a bare
                # material-spec label, not a product-type heading) --
                # both exclusions verified against the actual noise
                # rather than assumed.
                if (2 < len(stripped) <= 60 and _PIANCA_SHAPEB_HEADING_RE.match(stripped)
                        and not stripped.endswith(')') and not stripped.startswith('Finiture')):
                    variant_context = stripped
                i += 1
                continue

            if price == '-':
                flags.append((page_of_line[i], product_name,
                              f"no price rows found for code {code}"))
                i += 1
                continue

            rows.append({
                "brand": brand,
                "product_name": product_name,
                "model_variant": None,
                "variant_context": variant_context,
                "size": dim,
                "fabric_tier": "Materico",
                "tier_label": "Finish",
                "code": code,
                "price_eur": price,
                "source_pdf_page": page_of_line[i],
            })
            i += 1
        # continue outer loop from wherever the inner scan stopped
    return rows, flags


# ---------------------------------------------------------------------------
# Pianca "Letti" (beds) tier-ladder shape -- found 2026-08-25 during the
# full known_gap inventory pass, verified on 9 real bed products (Beta up
# (Letti), Beta up trasformabile (Letti), Embrace (Letti), Rialto (Letti),
# Bricola (Letti), Filo, Fushimi, Piumotto, Rada, Dioniso). Structurally
# the SAME A/B/C/H/P/Q tier ladder as base Shape A -- same tier letters,
# same code+dash-skip convention -- but blocked by two things Shape A's
# own header/row scan doesn't handle:
#
#   1. Header order is REVERSED: the tier letters print on their own line
#      ABOVE the "L P CODICI" line (confirmed via Beta up (Letti)'s real
#      page 12), not on the same line as CODICI the way every other Shape
#      A table in this file does. Base Shape A's own header check requires
#      the tier letters as the LAST tokens of the SAME line as CODICI, so
#      it never recognizes this and correctly declines rather than
#      guessing (confirmed: 0 rows lost from Shape A for any of these 9).
#   2. The tier count varies per product -- NOT always the full 6 (Beta
#      up/Embrace/Rialto/Filo/Fushimi/Piumotto/Rada/Dioniso show all 6;
#      Bricola (Letti) shows only 4, "A B C H", confirmed via its own real
#      page: "A B C H" then rows with exactly 4 trailing prices) -- so the
#      tier letters actually present are read from whatever real line is
#      found above CODICI, not assumed to always be the full ladder.
#   3. Row sizing is bed-specific: a leading NOMINAL "WxH" label (e.g.
#      "153x190", matching how a customer would actually ask for a bed
#      size) followed by the row's own internal L/P manufacturing
#      dimensions, which are sometimes a plain number and sometimes a
#      "min/max" range (e.g. "163/179", confirmed real on Embrace).
#      `size` is set to JUST the nominal WxH label -- deliberately NOT
#      combined with the internal L/P figures, so it stays byte-identical
#      to what extractSize() in catalogChat.ts already recognizes from a
#      customer query ("give me the 160x200 price"), rather than risking
#      an exact-match lookup miss against a longer combined string.
#
# NOT the same family as Alfa (Letti)/Alfa (Tatami) -- those use a
# DIFFERENT shape entirely (4 flat NAMED columns -- Materico/Laccato/
# Essenza/Cuoio, confirmed via direct page inspection, not a tier ladder
# at all) with wildcard order codes ("WAF * 03S"), correctly left
# unrecognized here and deferred to their own follow-up investigation
# rather than assumed to match this shape just because they're also beds.
# ---------------------------------------------------------------------------

_PIANCA_LETTI_HEADER_RE = re.compile(r'^L\s+P\s+CODICI$|^L\s+H\s+P\s+CODICI$')


def _pianca_letti_tier_letters_above(lines, header_idx, lookback=12):
    """Searches UP TO `lookback` lines above the header for a line whose
    TRAILING tokens are a real prefix of _PIANCA_TIER_LETTERS (['A','B',
    'C','H','P','Q']) -- e.g. the full 6, or a shorter real subset like
    Bricola (Letti)'s own ['A','B','C','H']. Checked by TRAILING tokens,
    not requiring the whole line to be just the tier letters -- confirmed
    real and necessary: Filo's own tier line has a leading "Piedi" (feet/
    leg-style selector) word glued onto the SAME physical line as the
    tier letters ("Piedi ... A B C H P Q"), unlike Beta up (Letti)'s
    version of the identical convention, where "Piedi" prints on its OWN
    separate line just above -- same class of pdftotext -layout
    linearization inconsistency already seen throughout this file.
    Returns None if no such line is found within the window (safe -- the
    caller then correctly leaves this header unrecognized rather than
    guessing)."""
    for k in range(1, lookback + 1):
        idx = header_idx - k
        if idx < 0:
            break
        tokens = lines[idx].strip().split()
        if not tokens:
            continue
        # Floor of 2 (not 1) -- a lone trailing "A" is common enough as
        # ordinary Italian text (an article, an abbreviation) that a
        # single-letter match risks false-firing on some unrelated
        # table's own nearby text within the lookback window; every real
        # confirmed case this shape covers has at least 2 real tiers, so
        # this costs nothing against the actual data.
        for n in range(min(len(_PIANCA_TIER_LETTERS), len(tokens)), 1, -1):
            if tokens[-n:] == _PIANCA_TIER_LETTERS[:n]:
                return tokens[-n:]
    return None


_PIANCA_WXH_SIZE_RE = re.compile(r'^\d{2,3}x\d{2,3}$')


def parse_file_pianca_letti_tier(path, product_name, brand, all_headings=None, heading_text=None):
    """See module comment above for scope."""
    with open(path, encoding='utf-8') as f:
        lines = f.read().split('\n')

    page_of_line = [None] * len(lines)
    current_page = None
    for idx, ln in enumerate(lines):
        m = re.match(r'^<<<PDFPAGE:(\d+)>>>$', ln.strip())
        if m:
            current_page = int(m.group(1))
        page_of_line[idx] = current_page

    rows = []
    flags = []
    variant_context = None

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped_header = line.strip()
        if not _PIANCA_LETTI_HEADER_RE.match(stripped_header):
            i += 1
            continue
        tier_letters = _pianca_letti_tier_letters_above(lines, i)
        if tier_letters is None:
            i += 1
            continue
        n_cols = len(tier_letters)

        i += 1
        blank_run = 0
        while i < len(lines) and blank_run < 10:
            raw = lines[i]
            stripped = raw.strip()
            if stripped == '':
                blank_run += 1
                i += 1
                continue
            if _PIANCA_LETTI_HEADER_RE.match(stripped):
                break
            blank_run = 0

            tokens = stripped.split()
            trailing = tokens[-n_cols:] if n_cols <= len(tokens) else []
            if len(tokens) <= n_cols or not all(_PIANCA_PRICE_CELL_RE.match(t) for t in trailing) or not any(t != '-' for t in trailing):
                if 2 < len(stripped) <= 60 and _PIANCA_SHAPEB_HEADING_RE.match(stripped):
                    variant_context = stripped
                i += 1
                continue

            pre_tokens = tokens[:-n_cols]
            code = None
            if pre_tokens:
                # Wildcard-legend code -- same convention as Enea Up
                # (shape_b_named's own comment): preserve the literal
                # printed "<prefix> * <suffix>" text, never resolve the
                # letter. Checked first, before the single-token check,
                # for the same reason as there.
                if (len(pre_tokens) >= 3 and pre_tokens[-2] == '*'
                        and re.match(r'^[A-Z0-9]{2,6}$', pre_tokens[-3])
                        and re.match(r'^[A-Z0-9]{1,6}$', pre_tokens[-1])):
                    code = f"{pre_tokens[-3]} * {pre_tokens[-1]}"
                    pre_tokens = pre_tokens[:-3]
                elif _PIANCA_CODE_RE.match(pre_tokens[-1]) and re.search(r'\d', pre_tokens[-1]):
                    code = pre_tokens[-1]
                    pre_tokens = pre_tokens[:-1]

            if code is None:
                i += 1
                continue

            # The nominal WxH size (e.g. "153x190") is whichever remaining
            # pre-code token matches that shape -- confirmed always
            # present and always the SAME token position (immediately
            # after the size range/dim values, working backward) across
            # every sampled row, but found by shape rather than position
            # to stay robust to a row missing its P value (same class of
            # real gap already confirmed elsewhere in this file, e.g.
            # Duo's Cuscinetti rows). The internal L/P manufacturing
            # dims (plain numbers or "163/179"-style ranges) are
            # deliberately NOT captured into size -- see module comment.
            size = None
            for t in reversed(pre_tokens):
                if _PIANCA_WXH_SIZE_RE.match(t):
                    size = t
                    break

            any_price = False
            for letter, cell in zip(tier_letters, trailing):
                if cell == '-':
                    continue
                any_price = True
                rows.append({
                    "brand": brand,
                    "product_name": product_name,
                    "model_variant": None,
                    "variant_context": variant_context,
                    "size": size,
                    "fabric_tier": letter,
                    "tier_label": "Category",
                    "code": code,
                    "price_eur": cell,
                    "source_pdf_page": page_of_line[i],
                })
            if not any_price:
                flags.append((page_of_line[i], product_name,
                              f"no price rows found for code {code}"))
            i += 1
        # continue outer loop from wherever the inner scan stopped
    return rows, flags


# ---------------------------------------------------------------------------
# Pianca "Composizione <code> (<context>)" bundle family (Spazioteca/Spazio/
# People/Designbook's own composition-photo pages, 124 products, found
# during the 2026-08-25 known_gap sweep). Each composition prints a
# component-by-component breakdown (own L/H/P dims, own code, own 2
# finish-tier prices per component) ending in a single "totale" row -- THAT
# row is the composition's own sellable price (2 finish tiers: "Finitura
# base" and a specific catalog finish name read per-page, e.g. "Materico"/
# "Laccato Opaco", never hardcoded), not the component rows, which price
# separately-orderable parts.
#
# Multiple sibling compositions almost always share ONE physical page
# (confirmed: composizione_9201_spazioteca.txt and
# composizione_9202_spazioteca.txt are byte-identical files, both
# containing 9201's AND 9202's own blocks) -- so this can't just take "the"
# totale row, it must find the ONE matching THIS product's own code,
# extracted from product_name itself (the only per-entry signal available,
# since the shared file's content is identical either way).
#
# NOT the same family as the already-live "(Designbook 2022)" Composizione
# products (IOT0xx/TOT0xx, dims+1 batch) -- those share the same "L H P
# CODICI" header text but are a single bare-CODICI flat-price row each (no
# "totale" keyword, no "Finitura base" column at all, confirmed via direct
# check across all 60 of that family's own text files) -- excluded here by
# construction, not a special case, since this parser only fires when BOTH
# "Finitura base" and "Finiture catalogo" appear together on one line.
#
# Real, confirmed exception found while verifying all 124 before building:
# "Composizione COP061 (Designbook)" and "Composizione COP081 (Designbook)"
# -- the page's own section heading reads "...- COP061"/"...- COP081"
# (matching the catalog entry's own name, extracted from that same heading
# text at index time) but that composition's own totale row is printed
# "COS061"/"COS082" instead (every sibling on the same page, e.g. COS062/
# COS063, has its heading and totale code matching normally) -- a genuine
# single-letter P/S inconsistency in Pianca's own source PDF, confirmed by
# direct page inspection, not an extraction bug (logged in
# flag_triage.json). Handled with a fallback: if no totale row's code
# exactly matches, look for exactly one totale row whose own code shares
# the same TRAILING DIGITS (e.g. both end "061"), which resolves both real
# cases without hardcoding either -- and stays safe generally, since it
# still requires a unique match.
# ---------------------------------------------------------------------------

_PIANCA_COMPOSIZIONE_NAME_RE = re.compile(r'^Composizione\s+(\S+)\s*\(')
_PIANCA_COMPOSIZIONE_HEADER_RE = re.compile(r'L\s+H\s+P\s+CODICI\s*$')
_PIANCA_COMPOSIZIONE_TIER2_LABEL_RE = re.compile(r'([A-ZÀ-Ý][a-zà-ÿ]+(?:\s+[A-ZÀ-Ý][a-zà-ÿ]+)?)\s*$')


def parse_file_pianca_composizione_bundle(path, product_name, brand, all_headings=None, heading_text=None):
    """See module comment above for scope."""
    m = _PIANCA_COMPOSIZIONE_NAME_RE.match(product_name)
    if not m:
        return [], []
    entry_code = m.group(1)

    with open(path, encoding='utf-8') as f:
        lines = f.read().split('\n')

    page_of_line = [None] * len(lines)
    current_page = None
    for idx, ln in enumerate(lines):
        pm = re.match(r'^<<<PDFPAGE:(\d+)>>>$', ln.strip())
        if pm:
            current_page = int(pm.group(1))
        page_of_line[idx] = current_page

    if not any(_PIANCA_COMPOSIZIONE_HEADER_RE.search(ln) for ln in lines):
        return [], []

    tier2_name = None
    for idx, ln in enumerate(lines[:15]):
        if 'Finitura base' in ln and 'Finiture catalogo' in ln:
            for nxt in lines[idx + 1:idx + 3]:
                nm = _PIANCA_COMPOSIZIONE_TIER2_LABEL_RE.search(nxt.strip())
                if nm:
                    tier2_name = nm.group(1)
                    break
            break
    if tier2_name is None:
        return [], []

    totale_lines = [(idx, ln) for idx, ln in enumerate(lines) if re.match(r'^\s*totale\b', ln)]
    if not totale_lines:
        return [], []

    exact_matches = [
        (idx, ln.strip().split()) for idx, ln in totale_lines
        if entry_code in ln.strip().split()
    ]
    if len(exact_matches) == 1:
        idx, tokens = exact_matches[0]
        real_code = entry_code
    elif len(exact_matches) == 0:
        suffix_m = re.search(r'\d+$', entry_code)
        fallback_matches = []
        if suffix_m:
            suffix = suffix_m.group(0)
            for fidx, fln in totale_lines:
                ftokens = fln.strip().split()
                for tok in ftokens:
                    if tok != entry_code and _PIANCA_CODE_RE.match(tok) and tok.endswith(suffix):
                        fallback_matches.append((fidx, ftokens, tok))
        if len(fallback_matches) != 1:
            return [], [(None, product_name,
                          f"composizione bundle: no unique totale row found for code {entry_code}")]
        idx, tokens, real_code = fallback_matches[0]
    else:
        return [], [(None, product_name,
                      f"composizione bundle: ambiguous totale rows for code {entry_code}")]

    if tokens[0] != 'totale':
        return [], [(page_of_line[idx], product_name,
                      "composizione bundle: totale row shape not recognized")]
    code_idx = tokens.index(real_code)
    dims_tokens = tokens[1:code_idx]
    trailing = tokens[code_idx + 1:]

    if len(dims_tokens) != 3 or len(trailing) != 2 or not all(_PIANCA_PRICE_CELL_RE.match(t) for t in trailing):
        return [], [(page_of_line[idx], product_name,
                      "composizione bundle: totale row shape not recognized")]

    size = '×'.join(dims_tokens)
    columns = ['Finitura base', tier2_name]

    rows = []
    any_price = False
    for column_label, cell in zip(columns, trailing):
        if cell == '-':
            continue
        any_price = True
        rows.append({
            "brand": brand,
            "product_name": product_name,
            "model_variant": None,
            "variant_context": None,
            "size": size,
            "fabric_tier": column_label,
            "tier_label": "Finish",
            "code": real_code,
            "price_eur": cell,
            "source_pdf_page": page_of_line[idx],
        })
    flags = []
    if not any_price:
        flags.append((page_of_line[idx], product_name, f"no price rows found for code {real_code}"))
    return rows, flags


# ---------------------------------------------------------------------------
# Pianca SIPARIO Armadi Moduli/Composizioni "danger table" shape -- the
# style x door-mechanism family (Plana/Amalfi/Cornice/Icona/Manhattan/
# Murano/Nastro/Raggio/Tratto/Verona/Milano/Crea x battenti/cardine/
# scorrevoli/scorrevoli con anta Tv/complanari x Moduli/Composizioni).
# Genuinely 2-axis (an L row-group label + an H value on each physical
# row), 1 or 2 CODICI columns (P 59 / P 42.3 depth variants -- the
# complanari mechanism only ever offers P 59, confirmed on every sampled
# complanari page), and N named finish-tier columns whose count and exact
# labels are VERIFIED PER STYLE via direct page-image inspection, never
# assumed uniform from header text alone: confirmed real that Plana/
# Amalfi/Icona share one 4-column Materico/Opaco Base/Opaco Colore-
# Essenza/Lucido Sp. set; the cardine mechanism drops Materico to 3 for
# those same styles; Cornice has 6 columns on battenti pages but only 5
# on complanari/scorrevoli (no V. Marmo); Murano/Milano/Manhattan/Nastro/
# Verona/Raggio/Crea each have their own distinct set --
# see _PIANCA_ARMADI_COLUMN_REGISTRY, one entry per real header signature
# actually confirmed against a page image or, where the signature is
# byte-identical to an already-checked sibling, against the raw text.
#
# Registry is keyed by (header_labels, sub_wrap): the header's own tokens
# after the LAST 'CODICI', and the wrap-continuation tokens on the
# following physical line after the 'P 59 [P 42.3]' block. This pair
# uniquely identifies every real shape checked -- confirmed real: Crea's
# bare 'Vetro Laccato' and Raggio's 'Vetro Laccato' (which adds a
# 'Specchio' wrap) share identical header_labels but differ in sub_wrap,
# correctly keeping them separate. A single-CODICI (complanari) table and
# its 2-CODICI sibling share the SAME key, since sub_wrap excludes the
# P-block markers themselves -- code_slots is read directly off the
# matched header line's own literal CODICI count instead, so one
# registry entry safely covers both.
#
# One genuine residual collision found and handled explicitly: Nastro
# battenti's own 5th column picks up a 4th wrap word ('Liscio', a
# corner-unit finish option) on the Moduli page but not on the visually
# near-identical Composizioni page, even though both share the exact
# same (header_labels, sub_wrap) key -- 'Liscio' sits on a further line
# past where sub_wrap's capture window stops. Confirmed via direct grep
# this word appears EXACTLY ONCE across every Armadi text file in this
# family, only on nastro_armadi_battenti_moduli.txt, so a narrow post-hoc
# lookahead check (not a per-file special case) safely resolves it
# without risking a false positive elsewhere.
#
# Row layout is 'H code1 [code2] price1..priceN' (H FIRST, unlike every
# other Pianca shape's trailing-code convention) -- H is identified by
# exact membership in a small, verified closed set ({238.5, 257.7,
# 289.7}, confirmed the ONLY 3 values printed across every sampled page;
# 289.7 only appears for styles/mechanisms offering the taller module),
# never by a generic decimal regex, since ordinary L values are ALSO
# decimals (e.g. '47.8', '153.8') and would otherwise be ambiguous.
#
# The L row-group label and any leading context text need OPPOSITE carry
# directions, confirmed via direct row-by-row inspection, not assumed:
# L prints on the LAST physical row of its 2-3-row H group (needs a
# BACKWARD fill), while the con-anta-Tv family's 'anta TV Opaco Base/
# Colore' disambiguator (needed because the SAME code repeats under both
# labels with different prices -- confirmed real on Icona's own anta-Tv
# page) prints on the FIRST row of its own mini-group (a plain FORWARD
# carry). Handled with a two-pass buffer-then-resolve design rather than
# a single streaming pass, which could only get one direction right.
# ---------------------------------------------------------------------------

_PIANCA_ARMADI_COLUMN_REGISTRY = {
    (('Materico', 'Opaco', 'Base', 'Opaco', 'Colore', 'Lucido', 'Sp.'), ('Essenza',)):
        ['Materico', 'Opaco Base', 'Opaco Colore / Essenza', 'Lucido Sp.'],
    (('Materico', 'Op.', 'Base', 'Op.', 'Colore', 'Lucido', 'Sp.'), ('Essenza',)):
        ['Materico', 'Opaco Base', 'Opaco Colore / Essenza', 'Lucido Sp.'],
    (('Opaco', 'Base', 'Opaco', 'Colore', 'Lucido', 'Sp.'), ('Essenza',)):
        ['Opaco Base', 'Opaco Colore / Essenza', 'Lucido Sp.'],
    (('Materico', 'L.', 'Opaco', 'Lucido', 'Sp.', 'Pelle', 'Sint.', 'V.', 'Laccato', 'V.', 'Marmo'), ('Essenza', 'V.', 'Met.')):
        ['Materico', 'L. Opaco / Essenza', 'Lucido Sp.', 'Pelle Sint.', 'V. Laccato / V. Met. / Specchio', 'V. Marmo'],
    (('Materico', 'L.', 'Opaco', 'Lucido', 'Sp.', 'Pelle', 'Sint.', 'V.', 'Laccato'), ('Essenza', 'V.', 'Met.')):
        ['Materico', 'L. Opaco / Essenza', 'Lucido Sp.', 'Pelle Sint.', 'V. Laccato / V. Met. / Specchio'],
    (('Laccato', 'Opaco', 'Lucido', 'Sp.', 'V.', 'Laccato'), ('Essenza', 'V.Metallizzato')):
        ['Laccato Opaco', 'Lucido Sp. / Essenza', 'V. Laccato / V.Metallizzato / Specchio'],
    (('L.', 'Opaco', 'Essenza', 'Lucido', 'Sp.', 'V.', 'Laccato'), ('V.', 'Met.')):
        ['L. Opaco', 'Essenza', 'Lucido Sp.', 'V. Laccato / V. Met. / Specchio'],
    (('Laccato', 'Opaco', 'Lucido', 'Sp.'), ('Essenza',)):
        ['Laccato Opaco', 'Lucido Sp. / Essenza'],
    (('Laccato', 'Opaco', 'Essenza', 'Lucido', 'Sp.'), ()):
        ['Laccato Opaco', 'Essenza', 'Lucido Sp.'],
    (('V.', 'Trasparente', 'V.', 'Riflettente', 'Vetro', 'Trama'), ('V.', 'Metallizzato', 'Vetro', 'Rigato')):
        ['V. Trasparente / V. Metallizzato / Specchio', 'Vetro Riflettente', 'Vetro Trama / Vetro Rigato'],
    (('V.', 'Trasparente', 'Vetro', 'Riflettente', 'Vetro', 'Trama'), ('V.', 'Metallizzato', 'Vetro', 'Rigato')):
        ['V. Trasparente / V. Metallizzato / Specchio', 'Vetro Riflettente', 'Vetro Trama / Vetro Rigato'],
    (('V.', 'Laccato', 'V.', 'Riflettente', 'V.', 'Trama', 'V.', 'Marmo'), ('V.', 'Metallizzato', 'V.', 'Rigato')):
        ['V. Laccato / V. Metallizzato / Specchio / V. Trasparente', 'V. Riflettente', 'V. Trama / V. Rigato', 'V. Marmo'],
    (('Vetro', 'Laccato'), ('Specchio',)):
        ['Vetro Laccato / Specchio'],
    # Raggio's own complanari page abbreviates 'Vetro' to 'V.' on this
    # one header (confirmed real via direct text inspection, not a typo
    # in this registry) -- every other Raggio page spells it out.
    (('V.', 'Laccato'), ('Specchio',)):
        ['Vetro Laccato / Specchio'],
    (('Vetro', 'Laccato'), ()):
        ['Vetro Laccato'],
    (('Materico', 'Op.', 'Base', 'Op.', 'Colore', 'Lucido', 'Sp.', 'V.', 'Laccato'), ('Essenza', 'V.', 'Met.')):
        ['Materico', 'Op. Base', 'Op. Colore / Essenza', 'Lucido Sp.', 'V. Laccato / V. Met. / Specchio'],
    (('Cornice', 'e', 'pannello', 'Cornice', 'e', 'pannello'), ('Laccato', 'Opaco', 'Essenza')):
        ['Cornice e pannello / Laccato Opaco', 'Cornice e pannello / Essenza'],
    # SIPARIO Fianchi e divisori (battenti/cardine/scorrevoli) -- side-panel/
    # divider family, verified against sipario_fianchi_e_divisori_armadi_
    # battenti_p45-045.jpg. No L row-group value at all (side panels aren't
    # sized by a printed width the way wardrobe modules are) -- each row
    # group's own descriptive label ("Fianco Sp 3 senza cornice" etc) is
    # simply ignored, same as this parser already does for "Terminale"/
    # "Angolo" labels elsewhere, safe here since every code is already
    # unique across groups (no repeat-code disambiguation need).
    (('Materico', 'Op.', 'Base', 'Op.', 'Colore', 'Lucido', 'Sp.', 'Materico'), ('Essenza', 'Interno')):
        ['Materico', 'Opaco Base', 'Opaco Colore / Essenza', 'Lucido Sp.', 'Materico Interno'],
    # SIPARIO Fianchi di finitura (battenti/scorrevoli) -- verified against
    # sipario_fianchi_di_finitura_armadi_battenti_p46-046.jpg, a 3-physical-
    # line wrap (same class as Cornice's own Armadi Moduli 6-column shape).
    (('L.', 'Opaco', 'Lucido', 'Sp.', 'V.', 'Laccato', 'V.', 'Marmo', 'V.', 'Laccato'), ('Essenza', 'V.', 'Met.', 'Specchio')):
        ['L. Opaco / Essenza', 'Lucido Sp.', 'V. Laccato / V. Met. / Specchio / Liscio', 'V. Marmo', 'V. Laccato / Specchio / Inciso'],
    # SIPARIO Anta Tv Moduli scorrevoli's own frame-component table (its
    # SECOND table on the same page, an unrelated "L CODICI ... Accessori"
    # LED-accessory shape with no H column at all, correctly stays
    # unrecognized by this parser -- deliberately not attempted here).
    (('Opaco', 'Base', 'Opaco', 'Colore'), ()):
        ['Opaco Base', 'Opaco Colore'],
    # Sipario (Spazi-10) -- flagged in flag_triage.json as looking like it
    # needed its own dedicated 2-axis function (2 real SKUs per row,
    # sharing one price vector), but confirmed via direct row inspection
    # to be EXACTLY this parser's own existing 2-CODICI-column shape
    # (P 59 / P 42.3 depth variants, same price for both -- not a
    # repeating-code-different-price danger table at all), just needing
    # 3 new registry entries. Verified real PDF pages 46-54.
    (('Materico', 'Opaco', 'Base', 'Opaco', 'Colore', 'Lucido', 'Sp.'), ('Essenza', 'L.', 'Metallico')):
        ['Materico', 'Opaco Base', 'Opaco Colore / Essenza', 'Lucido Sp. / L. Metallico'],
    (('Materico', 'L.', 'Opaco', 'Lucido', 'Pelle', 'V.', 'Laccato', 'V.', 'Marmo'), ('Essenza', 'Sp.', 'Sint.', 'V.', 'L-Met.')):
        ['Materico', 'L. Opaco / Essenza', 'Lucido Sp. / L-Met.', 'Pelle Sint.',
         'V. Laccato / V. L-Met. / V. Metall. / Specchio', 'V. Marmo'],
    (('Materico', 'Opaco', 'Base', 'Opaco', 'Lucido', 'Sp.', 'V.', 'Laccato'), ('Colore', 'L.', 'Metallico', 'V.', 'L-Met.')):
        ['Materico', 'Opaco Base', 'Opaco Colore / Essenza', 'Lucido Sp. / L. Metallico',
         'V. Laccato / V. L-Met. / V. Metall. / Specchio'],
}

_PIANCA_ARMADI_H_VALUES = {'238.5', '257.7', '289.7'}
_PIANCA_ARMADI_ANTATV_RE = re.compile(r'^anta TV Opaco (Base|Colore)$')
_PIANCA_ARMADI_DS_SUFFIX_RE = re.compile(r'^\S*D/S$')
_PIANCA_ARMADI_NUMERIC_RE = re.compile(r'^\d+(\.\d+)?$')


def _pianca_armadi_section_marker(line):
    """A section-title line's own leading ALL-CAPS word(s) -- e.g.
    'PLANA' from ' PLANA Moduli battenti', 'HOME OFFICE' from 'HOME
    OFFICE Moduli con anta Amalfi' -- or None if the line doesn't open
    with one. Confirmed this convention holds on every sampled page."""
    toks = line.split()
    name_toks = []
    for t in toks:
        if t.isupper() and t.isalpha() and len(t) >= 2:
            name_toks.append(t)
        else:
            break
    return ' '.join(name_toks) if name_toks else None


def _pianca_armadi_own_scope(lines, product_name):
    """Returns (start, end) line-index bounds restricting where a NEW
    table is allowed to START to just this product's own section.
    REQUIRED because some pages are shared verbatim between 2 styles --
    confirmed real: Plana's and Cornice's own 'Cabina soffietto' pages
    are byte-identical text files each holding BOTH styles' full tables
    back-to-back, so without this, both products silently absorbed each
    other's codes and prices (caught via a real cross-contamination
    check, not assumed safe). Only gates where a table may BEGIN --
    once a table starts inside the product's own scope, its row-scan
    already has its own independent termination logic (next header /
    blank-run), so this never truncates real rows mid-table, only
    prevents starting a table that belongs to a different style's
    section. Safe no-op for every single-style file (checked: the one
    real marker just becomes the whole range either way)."""
    markers = []
    for idx, ln in enumerate(lines):
        name = _pianca_armadi_section_marker(ln)
        if name:
            markers.append((idx, name))
    if not markers:
        return 0, len(lines)
    style = product_name.split(' — ')[0].strip().upper()

    def is_own(name):
        return name == style or style.startswith(name) or name.startswith(style)

    own = [idx for idx, name in markers if is_own(name)]
    if not own:
        return 0, len(lines)
    start = own[0]
    # A repeated marker of the SAME style (a running page-header on a
    # later physical page of a multi-page product -- confirmed real on
    # 'Plana — Moduli stagionali', whose own title reprints
    # verbatim on page 2 right before its actual price table) does NOT
    # end the scope; only a DIFFERENT style's marker does.
    later = [idx for idx, name in markers if idx > start and not is_own(name)]
    end = later[0] if later else len(lines)
    return start, end


def _pianca_armadi_header_key(line):
    """Returns (header_labels, code_slots) if `line` looks like this
    shape's own header, else None. Requires an 'H' token IMMEDIATELY
    followed by 'CODICI' -- NOT just both tokens present anywhere on the
    line, which was confirmed too loose: Shape A's own 'L H P CODICI ...'
    tier-letter header (H separated from CODICI by 'P') and several other
    unrelated Pianca shapes also contain both words, and matched a first
    draft of this check, flagging dozens of already-correctly-parsed
    products (Levante/Peonia/Delano up/Nice/Tobias/... and more) with
    noisy false-positive flags. Requiring direct adjacency is what every
    real sample of THIS shape has (confirmed on every checked image),
    including the one edge case where a line has 2 'H' tokens (Tratto's
    own 'H a scomparsa per anta interna ... H CODICI CODICI ...' --
    the first H is followed by 'a', not 'CODICI', and is correctly
    ignored). code_slots is the literal CODICI count on THIS line --
    never assumed from the registry, since a complanari page genuinely
    only ever prints one."""
    toks = line.split()
    if not any(toks[i] == 'H' and i + 1 < len(toks) and toks[i + 1] == 'CODICI' for i in range(len(toks))):
        return None
    codici_idxs = [i for i, t in enumerate(toks) if t == 'CODICI']
    if not codici_idxs or len(codici_idxs) > 2:
        return None
    header_labels = tuple(toks[codici_idxs[-1] + 1:])
    if not header_labels:
        return None
    return header_labels, len(codici_idxs)


def _pianca_armadi_sub_wrap(sub_line):
    """Tokens on the header's following physical line AFTER the last
    'P <number>' block -- the real wrap-continuation words (e.g.
    'Essenza'), with the leading hardware-finish legend text (e.g.
    'Canna di Fucile') and the P-block markers themselves excluded."""
    stoks = sub_line.split()
    p_idxs = [i for i, t in enumerate(stoks) if t == 'P' and i + 1 < len(stoks) and re.match(r'^\d', stoks[i + 1])]
    if not p_idxs:
        return tuple(stoks)
    return tuple(stoks[p_idxs[-1] + 2:])


def _pianca_armadi_consume_code(tokens, idx):
    """Greedily consumes one code unit starting at tokens[idx]: a bare
    code ('BA715'), a code+hinge-suffix pair ('MMU73','D/S' or, on the
    Moduli scorrevoli pages specifically, 'PMU75','0/D/S' -- confirmed
    real via direct image check, not an OCR artifact), a wildcard code
    ('4C','*','720' or 'M','*','73','D/S'), or '-' (not offered at this
    depth). Returns (code_or_None, next_idx)."""
    if tokens[idx] == '-':
        return None, idx + 1
    parts = [tokens[idx]]
    idx += 1
    if idx < len(tokens) and tokens[idx] == '*':
        parts.append('*')
        idx += 1
        if idx < len(tokens):
            parts.append(tokens[idx])
            idx += 1
    if idx < len(tokens) and _PIANCA_ARMADI_DS_SUFFIX_RE.match(tokens[idx]):
        parts.append(tokens[idx])
        idx += 1
    return ' '.join(parts), idx


def parse_file_pianca_armadi_danger(path, product_name, brand, all_headings=None, heading_text=None):
    """See module comment above for scope (SIPARIO's Armadi Moduli/
    Composizioni style x mechanism family)."""
    with open(path, encoding='utf-8') as f:
        lines = f.read().split('\n')

    page_of_line = [None] * len(lines)
    current_page = None
    for idx, ln in enumerate(lines):
        m = re.match(r'^<<<PDFPAGE:(\d+)>>>$', ln.strip())
        if m:
            current_page = int(m.group(1))
        page_of_line[idx] = current_page

    rows = []
    flags = []
    scope_start, scope_end = _pianca_armadi_own_scope(lines, product_name)

    i = 0
    while i < len(lines):
        if not (scope_start <= i < scope_end):
            i += 1
            continue
        key = _pianca_armadi_header_key(lines[i])
        if key is None:
            i += 1
            continue
        header_labels, code_slots = key
        sub_line = lines[i + 1] if i + 1 < len(lines) else ''
        sub_wrap = _pianca_armadi_sub_wrap(sub_line)
        columns = _PIANCA_ARMADI_COLUMN_REGISTRY.get((header_labels, sub_wrap))
        if columns is None:
            flags.append((page_of_line[i], product_name,
                          f"unrecognized Armadi danger-table header, skipped: {lines[i].strip()[:120]!r}"))
            i += 1
            continue

        # Nastro battenti's own 5th column picks up a 4th wrap word
        # ('Liscio') that sub_wrap's capture window doesn't reach -- see
        # module comment. Confirmed unique across every Armadi file, safe
        # to check unconditionally rather than as a per-file exception.
        lookahead_block = '\n'.join(lines[i:i + 8])
        if re.search(r'\bLiscio\b', lookahead_block) and 'Liscio' not in columns[-1]:
            columns = columns[:-1] + [columns[-1] + ' / Liscio']

        i += 2  # past header + sub-line

        # Phase 1: buffer every price row in this table (H, code tokens,
        # trailing prices, and its own raw leading-context tokens)
        # without resolving L or the anta-Tv prefix yet -- they need
        # opposite carry directions, see module comment.
        records = []
        blank_run = 0
        j = i
        while j < len(lines) and blank_run < 10:
            raw = lines[j]
            stripped = raw.strip()
            if stripped == '':
                blank_run += 1
                j += 1
                continue
            if _pianca_armadi_header_key(raw) is not None:
                break
            blank_run = 0

            tokens = stripped.split()
            h_idxs = [k for k, t in enumerate(tokens) if t in _PIANCA_ARMADI_H_VALUES]
            if not h_idxs:
                # Leading-context-only line (a carried L value, an
                # anta-Tv prefix, or unrelated legend/diagram text).
                records.append({"line": j, "leading": tokens, "row": None})
                j += 1
                continue

            hi = h_idxs[0]
            leading = tokens[:hi]
            h_val = tokens[hi]
            idx2 = hi + 1
            codes = []
            for _ in range(code_slots):
                if idx2 >= len(tokens):
                    codes.append(None)
                    continue
                code, idx2 = _pianca_armadi_consume_code(tokens, idx2)
                codes.append(code)
            trailing = tokens[idx2:]
            if len(trailing) != len(columns) or not all(_PIANCA_PRICE_CELL_RE.match(t) for t in trailing) \
                    or not any(t != '-' for t in trailing):
                flags.append((page_of_line[j], product_name,
                              f"Armadi danger-table row shape mismatch (expected {len(columns)} price cells), skipped: {stripped[:120]!r}"))
                j += 1
                continue

            records.append({
                "line": j, "leading": leading,
                "row": {"h": h_val, "codes": codes, "trailing": trailing},
            })
            j += 1

        # Phase 2a: forward pass resolves the anta-Tv prefix (prints on
        # the FIRST row of its own mini-group).
        carried_prefix = None
        for rec in records:
            text = ' '.join(rec["leading"])
            if _PIANCA_ARMADI_ANTATV_RE.match(text):
                carried_prefix = text
            rec["prefix"] = carried_prefix

        # Phase 2b: backward pass resolves L (prints on the LAST row of
        # its own 2-3-row H group).
        carried_l = None
        for rec in reversed(records):
            if rec["leading"] and all(_PIANCA_ARMADI_NUMERIC_RE.match(t) for t in rec["leading"]):
                carried_l = rec["leading"][0]
            rec["l"] = carried_l

        # Phase 3: emit.
        depths = ['59', '42.3'][:code_slots]
        for rec in records:
            row = rec["row"]
            if row is None:
                continue
            any_price = False
            for code, depth in zip(row["codes"], depths):
                if code is None:
                    continue
                size = f'{rec["l"]}×{row["h"]}×{depth}' if rec["l"] else f'{row["h"]}×{depth}'
                for column_label, cell in zip(columns, row["trailing"]):
                    if cell == '-':
                        continue
                    any_price = True
                    tier = f'{rec["prefix"]} — {column_label}' if rec["prefix"] else column_label
                    rows.append({
                        "brand": brand,
                        "product_name": product_name,
                        "model_variant": None,
                        "variant_context": rec["prefix"],
                        "size": size,
                        "fabric_tier": tier,
                        "tier_label": "Finish",
                        "code": code,
                        "price_eur": cell,
                        "source_pdf_page": page_of_line[rec["line"]],
                    })
            if not any_price:
                flags.append((page_of_line[rec["line"]], product_name,
                              f'no price rows found for H {row["h"]}'))

        i = j  # continue outer loop from wherever the inner scan stopped
    return rows, flags


# ---------------------------------------------------------------------------
# Pianca "wardrobe-danger" 2-axis family -- Brema, Ginevra, Grafica, Logos
# (CollezioneGiorno), People (CollezioneGiorno), Quadra, Tosca (Madie/
# wardrobe products). Header convention: a legend block ("L A: battente /
# C: cassetto / ...", reusing the SAME _PIANCA_2AXIS_LEGEND_RE-style
# single-letter-colon pattern as Norma Up) followed eventually by a bare
# "H P CODICI" line with NOTHING trailing (never a shape_b_named-style
# named column -- the real column labels sit ABOVE, in a "Struttura /
# Frontali" 2-parent-group header spanning several physical lines).
#
# Individually investigated per product, NOT assumed uniform -- confirmed
# real, structural differences: Ginevra/Logos/People (CollezioneGiorno)/
# Quadra are simple single-code-per-row tables (verified: every code in
# each file appears EXACTLY ONCE, checked directly, not assumed from the
# header shape alone); Grafica and Tosca have a genuine repeating-code/
# different-price danger pattern (a "Basamento"/"Interno" row-type label
# to the LEFT of H, same class of collision risk that caused Norma Up's
# original 1566-row bug) needing the row-type folded into fabric_tier.
#
# Column labels: fully legible and exact for Ginevra, Logos, People
# (CollezioneGiorno), Quadra, and Tosca (verified via direct page-image
# inspection). Brema and Grafica's own "Struttura+top" header wraps
# across 4 stacked physical lines with a genuinely ambiguous exact
# finish-name pairing for their middle columns (their FIRST column,
# "Materico", is unambiguous and used verbatim) -- per explicit user
# decision, those columns use a positional "Struttura+top X — Frontali N"
# label instead of a guessed exact name, the same precedent already
# established for Fushimi/Inari's own genuinely-ambiguous wrapped column
# names (_PIANCA_TAVOLI_PIANO_TIERS). Prices and codes are fully exact
# either way -- this only affects the display label text.
#
# Scoped by product_name (like parse_file_pianca_composizione_bundle's
# own _PIANCA_COMPOSIZIONE_NAME_RE check), not just content -- necessary
# because several of these products' own text_file is SHARED with other,
# already-resolved products (e.g. People (CollezioneGiorno)'s underlying
# structural pattern also appears, byte-identical, in the much larger
# people.txt file backing 13 unrelated "Composizione P5xx (People)" /
# "Boiserie e People" products) -- a content-only trigger would have
# wrongly attributed this table's rows to those unrelated products too.
# ---------------------------------------------------------------------------

_PIANCA_WARDROBE_DS_SUFFIX_RE = re.compile(r'^\S*D/S$')


def _pianca_wardrobe_consume_code(tokens):
    """Consumes the WHOLE code region (every token in `tokens`, already
    isolated by the caller) into one literal code string. Handles a
    plain code, a code+D/S hinge suffix, an embedded wildcard
    ('M * 73 D/S', 4 tokens, armadi-style), and a TRAILING standalone
    wildcard ('0083FF *', 2 tokens, confirmed real on Tosca/People's own
    pages -- a different wildcard convention than armadi's embedded
    style, not an extraction artifact). Returns None if `tokens` doesn't
    fully consume as one recognized code shape (caller then skips the
    row rather than guessing)."""
    if not tokens:
        return None
    if tokens == ['-']:
        return None
    if len(tokens) == 1 and _PIANCA_CODE_RE.match(tokens[0]):
        return tokens[0]
    if len(tokens) == 2 and _PIANCA_CODE_RE.match(tokens[0]) and _PIANCA_WARDROBE_DS_SUFFIX_RE.match(tokens[1]):
        return ' '.join(tokens)
    if len(tokens) == 2 and _PIANCA_CODE_RE.match(tokens[0]) and tokens[1] == '*':
        return ' '.join(tokens)
    if len(tokens) == 4 and tokens[1] == '*' and re.match(r'^[A-Z0-9]{1,4}$', tokens[0]) \
            and re.match(r'^[A-Z0-9]{1,4}$', tokens[2]) and _PIANCA_WARDROBE_DS_SUFFIX_RE.match(tokens[3]):
        return ' '.join(tokens)
    if len(tokens) == 3 and tokens[1] == '*' and re.match(r'^[A-Z0-9]{1,4}$', tokens[0]) \
            and re.match(r'^[A-Z0-9]{1,4}$', tokens[2]):
        return ' '.join(tokens)
    return None


def _pianca_wardrobe_find_headers(lines, anchor_tokens, lookback=15):
    """Yields each line index whose own tokens end in exactly ['H','P',
    'CODICI'] (nothing trailing -- distinguishes this shape from both
    Shape A, whose CODICI is always followed by the 6 tier letters, and
    from armadi_danger's own 'H CODICI' adjacency, which has nothing
    between H and CODICI), PROVIDED a line within `lookback` lines above
    it contains `anchor_tokens` as a contiguous run -- the product's own
    verified, near-unique column-header signature (e.g. Brema's 6x
    'Frontali' run), confirmed checked against a full-catalog grep
    before use, not assumed safe from this file alone."""
    n = len(anchor_tokens)
    anchor_at = set()
    for i, ln in enumerate(lines):
        toks = ln.split()
        for k in range(len(toks) - n + 1):
            if toks[k:k + n] == anchor_tokens:
                anchor_at.add(i)
                break
    for i, ln in enumerate(lines):
        if ln.split()[-3:] != ['H', 'P', 'CODICI']:
            continue
        if any(i - a >= 0 and i - a <= lookback for a in anchor_at):
            yield i


def _pianca_wardrobe_scan_table(lines, page_of_line, header_idx, product_name, brand, columns, has_row_type, rows, flags):
    """Row-scan for ONE table starting after `header_idx`, appending
    directly into the caller's `rows`/`flags` lists. Each row is
    '[leading context] H P CODE price1..priceN'; H/P located as the LAST
    adjacent numeric-token pair before the code (never a fixed set,
    unlike armadi_danger's own closed {238.5,257.7,289.7} -- these
    products' own H/P values vary freely per row). `leading context`
    (if any) is captured as a carried row-type label ONLY when
    `has_row_type` -- forward-carry across rows until the next real
    label, same convention as armadi_danger's own anta-Tv prefix --
    and folded into fabric_tier to avoid a (code, fabric_tier) collision
    when the SAME code legitimately repeats under 2 different row-types
    with different prices (confirmed real on Grafica/Tosca). Returns the
    line index where this table's own scan stopped."""
    n_cols = len(columns)
    i = header_idx + 1
    blank_run = 0
    row_type = None
    while i < len(lines) and blank_run < 12:
        raw = lines[i]
        stripped = raw.strip()
        if stripped == '':
            blank_run += 1
            i += 1
            continue
        if stripped.split()[-3:] == ['H', 'P', 'CODICI']:
            break  # next table's own header -- outer loop handles it
        blank_run = 0

        tokens = stripped.split()
        if len(tokens) <= n_cols:
            i += 1
            continue
        trailing = tokens[-n_cols:]
        if not all(_PIANCA_PRICE_CELL_RE.match(t) for t in trailing) or not any(t != '-' for t in trailing):
            if has_row_type and re.match(r'^[A-Za-zÀ-ÿ]', stripped) and len(stripped) <= 40:
                row_type = stripped
            i += 1
            continue

        pre = tokens[:-n_cols]
        hp_idx = None
        for k in range(len(pre) - 2, -1, -1):
            if re.match(r'^\d+(\.\d+)?$', pre[k]) and re.match(r'^\d+(\.\d+)?$', pre[k + 1]):
                hp_idx = k
                break
        if hp_idx is None:
            flags.append((page_of_line[i], product_name,
                          f"wardrobe-danger row shape mismatch (no H/P pair found), skipped: {stripped[:120]!r}"))
            i += 1
            continue

        leading = _pianca_strip_leading_diagram_noise(pre[:hp_idx])
        h_val, p_val = pre[hp_idx], pre[hp_idx + 1]
        code = _pianca_wardrobe_consume_code(pre[hp_idx + 2:])
        if code is None:
            flags.append((page_of_line[i], product_name,
                          f"wardrobe-danger row shape mismatch (code not recognized), skipped: {stripped[:120]!r}"))
            i += 1
            continue

        if has_row_type and leading:
            row_type = ' '.join(leading)

        size = f'{h_val}×{p_val}'
        any_price = False
        for column_label, cell in zip(columns, trailing):
            if cell == '-':
                continue
            any_price = True
            tier = f'{row_type} — {column_label}' if (has_row_type and row_type) else column_label
            rows.append({
                "brand": brand,
                "product_name": product_name,
                "model_variant": None,
                "variant_context": row_type if has_row_type else None,
                "size": size,
                "fabric_tier": tier,
                "tier_label": "Finish",
                "code": code,
                "price_eur": cell,
                "source_pdf_page": page_of_line[i],
            })
        if not any_price:
            flags.append((page_of_line[i], product_name, f"no price rows found for code {code}"))
        i += 1
    return i


def _parse_file_pianca_wardrobe(path, product_name, brand, expected_name, anchor_tokens, columns, has_row_type):
    if product_name != expected_name:
        return [], []
    with open(path, encoding='utf-8') as f:
        lines = f.read().split('\n')
    page_of_line = [None] * len(lines)
    current_page = None
    for idx, ln in enumerate(lines):
        m = re.match(r'^<<<PDFPAGE:(\d+)>>>$', ln.strip())
        if m:
            current_page = int(m.group(1))
        page_of_line[idx] = current_page

    rows = []
    flags = []
    for header_idx in _pianca_wardrobe_find_headers(lines, anchor_tokens):
        _pianca_wardrobe_scan_table(lines, page_of_line, header_idx, product_name, brand, columns, has_row_type, rows, flags)
    return rows, flags


# Brema, real PDF page 54 -- verified via image. 6 columns; only the 1st
# ("Materico") has an unambiguous exact name, the remaining 5 use the
# positional fallback (see module comment above).
_PIANCA_WARDROBE_BREMA_COLUMNS = ['Struttura+top 0.8 — Frontali Materico'] + \
    [f'Struttura+top 0.4 — Frontali {i}' for i in range(2, 7)]


def parse_file_pianca_brema(path, product_name, brand, all_headings=None, heading_text=None):
    """See module comment above for scope."""
    return _parse_file_pianca_wardrobe(
        path, product_name, brand, 'Brema',
        ['Frontali'] * 6, _PIANCA_WARDROBE_BREMA_COLUMNS, has_row_type=False)


# Ginevra, real PDF pages -- verified via text (fully legible, clean 2x2
# Struttura x Frontali grid). Every code confirmed to appear exactly
# once across the whole file before trusting the simple (no row-type)
# scan.
_PIANCA_WARDROBE_GINEVRA_COLUMNS = [
    'Struttura Laccato Opaco/Essenza — Frontali L. Opaco/Essenza',
    'Struttura Laccato Opaco/Essenza — Frontali Lucido Sp.',
    'Struttura Lucido Spazzolato — Frontali L. Opaco/Essenza',
    'Struttura Lucido Spazzolato — Frontali Lucido Sp.',
]


def parse_file_pianca_ginevra(path, product_name, brand, all_headings=None, heading_text=None):
    """See module comment above for scope."""
    return _parse_file_pianca_wardrobe(
        path, product_name, brand, 'Ginevra',
        ['L.', 'Opaco', 'Lucido', 'Sp.', 'L.', 'Opaco', 'Lucido', 'Sp.'],
        _PIANCA_WARDROBE_GINEVRA_COLUMNS, has_row_type=False)


# Grafica, real PDF page 71 -- verified via image. 8 columns; only the
# 1st ("Materico") is unambiguous, rest use the positional fallback. HAS
# the repeating-code danger pattern (Basamento: L. Opaco / Fin. Metallo
# row-type, confirmed real via direct row inspection -- code G3CH prints
# twice with different prices under each).
_PIANCA_WARDROBE_GRAFICA_COLUMNS = ['Struttura+top 1.4 — Frontali Materico'] + \
    [f'Struttura+top 1.4 — Frontali {i}' for i in range(2, 9)]


def parse_file_pianca_grafica(path, product_name, brand, all_headings=None, heading_text=None):
    """See module comment above for scope."""
    return _parse_file_pianca_wardrobe(
        path, product_name, brand, 'Grafica',
        ['Frontali'] * 8, _PIANCA_WARDROBE_GRAFICA_COLUMNS, has_row_type=True)


# Logos (CollezioneGiorno), real PDF page 74 -- verified via text (fully
# legible: 2 'Top e frontali interni' parent groups x 3 'Struttura e
# frontali esterni' sub-choices). Every code confirmed to appear exactly
# once.
_PIANCA_WARDROBE_LOGOS_COLUMNS = [
    f'Top e frontali interni {parent} — Struttura e frontali esterni {sub}'
    for parent in ('L. Opaco/Essenza', 'Lucido Sp.')
    for sub in ('L. Opaco', 'Essenza', 'Lucido Sp.')
]


def parse_file_pianca_logos_collezionegiorno(path, product_name, brand, all_headings=None, heading_text=None):
    """See module comment above for scope. Anchor is 6 consecutive bare
    'Struttura' tokens -- unlike the other 6 products in this family,
    Logos's own 3-word 'Struttura e frontali esterni' phrase is NOT
    contiguous on one physical line (it wraps across 3 stacked lines,
    'Struttura'x6 / 'e frontali'x6 / 'esterni'x6, confirmed via direct
    text inspection), so only the first word is usable as a same-line
    anchor. Confirmed unique via full-catalog grep (only logos.txt and
    logos_collezionegiorno.txt contain this run at all)."""
    return _parse_file_pianca_wardrobe(
        path, product_name, brand, 'Logos (CollezioneGiorno)',
        ['Struttura'] * 6,
        _PIANCA_WARDROBE_LOGOS_COLUMNS, has_row_type=False)


# People (CollezioneGiorno), real PDF page 87 -- verified via text (fully
# legible: 'Struttura Laccato Opaco/Essenza' parent spans 3 Frontali
# sub-choices, 'Struttura Lucido Spazzolato' spans 2). Scoped by
# product_name specifically because this exact table ALSO appears,
# byte-identical, in the much bigger people.txt shared by 13 unrelated
# already-resolved products -- see module comment above.
_PIANCA_WARDROBE_PEOPLE_CG_COLUMNS = [
    'Struttura Laccato Opaco/Essenza — Frontali L. Opaco/Essenza',
    'Struttura Laccato Opaco/Essenza — Frontali Lucido Sp.',
    'Struttura Laccato Opaco/Essenza — Frontali Cuoio Rig.',
    'Struttura Lucido Spazzolato — Frontali L. Opaco/Essenza',
    'Struttura Lucido Spazzolato — Frontali Lucido Sp.',
]


def parse_file_pianca_people_collezionegiorno(path, product_name, brand, all_headings=None, heading_text=None):
    """See module comment above for scope."""
    return _parse_file_pianca_wardrobe(
        path, product_name, brand, 'People (CollezioneGiorno)',
        ['L.', 'Opaco', 'Lucido', 'Sp.', 'Cuoio', 'Rig.', 'L.', 'Opaco', 'Lucido', 'Sp.'],
        _PIANCA_WARDROBE_PEOPLE_CG_COLUMNS, has_row_type=False)


# Quadra, real PDF page -- verified via text (fully legible: 'Struttura
# Laccato Opaco/Essenza' parent spans 3 Frontali sub-choices, 'Struttura
# Lucido Sp.' spans 1 -- a real domain-consistent asymmetric split
# already confirmed on People (CollezioneGiorno) above: a narrower
# 'special' structure finish only ever pairs with its own matching
# frontali option).
_PIANCA_WARDROBE_QUADRA_COLUMNS = [
    'Struttura Laccato Opaco/Essenza — Frontali Laccato Opaco',
    'Struttura Laccato Opaco/Essenza — Frontali Essenza',
    'Struttura Laccato Opaco/Essenza — Frontali Lucido Sp.',
    'Struttura Lucido Sp. — Frontali Lucido Sp.',
]


def parse_file_pianca_quadra(path, product_name, brand, all_headings=None, heading_text=None):
    """See module comment above for scope."""
    return _parse_file_pianca_wardrobe(
        path, product_name, brand, 'Quadra',
        ['Laccato', 'Opaco', 'Essenza', 'Lucido', 'Sp.', 'Lucido', 'Sp.'],
        _PIANCA_WARDROBE_QUADRA_COLUMNS, has_row_type=False)


# Tosca, real PDF page 97 -- verified via text. Only 3 columns, fully
# legible, no 'Frontali' repeated-word convention at all (a flat
# 'Esterno' group: Laccato Opaco / Lucido Spazzolato / Essenza). HAS the
# repeating-code danger pattern (an 'Interno' row-type -- e.g. "Materico
# Lavagna" vs "Laccato Opaco" -- confirmed real via direct row
# inspection: code 0083FF * prints twice with different prices under
# each). Also exercises the trailing-standalone-wildcard code convention
# ('0083FF *').
_PIANCA_WARDROBE_TOSCA_COLUMNS = ['Esterno Laccato Opaco', 'Esterno Lucido Spazzolato', 'Esterno Essenza']


def parse_file_pianca_tosca(path, product_name, brand, all_headings=None, heading_text=None):
    """See module comment above for scope."""
    return _parse_file_pianca_wardrobe(
        path, product_name, brand, 'Tosca',
        ['Laccato', 'Opaco', 'Lucido', 'Spazzolato', 'Essenza'],
        _PIANCA_WARDROBE_TOSCA_COLUMNS, has_row_type=True)


# ---------------------------------------------------------------------------
# Cornice (Spazi-10) -- genuinely its own dedicated shape, per this
# project's standing rule that a structurally different 2-axis grid gets
# its own function rather than being forced through the wardrobe-danger
# family above or any existing 2-axis parser. Verified via direct text
# inspection (real PDF pages 17-22): header "L H P CODICI L. Opaco
# Lucido Sp. Pelle Sint. Pelle Sint." (sub-line wraps 'Essenza'/
# 'L. Metallico' under the first 2), 4 real columns under 2 parent
# groups ('Esterno' x3, 'Copertura aggiuntiva' x1). Two genuinely
# different row shapes share this ONE header, confirmed via direct code-
# repetition check, not assumed:
#   1. "Interno e cappello" rows: the SAME code (e.g. 00D4FF) prints
#      TWICE, once under a "L. Opaco" interior-finish label and once
#      under "L. Metallico", with different prices each time -- the
#      exact danger class that caused Norma Up's original 1566-row bug.
#      Folded into fabric_tier (same technique as armadi_danger/
#      wardrobe's own row-type fold).
#   2. "Copertura" rows: a genuinely separate, single-code-per-row
#      sub-table (confirmed: never repeats) that only ever populates the
#      4th column (Pelle Sint. under Copertura aggiuntiva) -- these rows
#      have NO leading row-type text of their own, so the row-type
#      carry must be explicitly RESET (not left stale from the previous
#      "Interno e cappello" block) the moment a bare "Copertura" context
#      line is seen, confirmed necessary by direct inspection (otherwise
#      these rows would wrongly inherit a stale "L. Metallico" prefix --
#      cosmetic only, since their own code never collides, but still
#      wrong).
# An "L (description)" heading (e.g. "120 (1 vano anta)") sits on its
# own line ABOVE each row-block (a plain forward carry, unlike
# armadi_danger's own L convention which needed a backward fill).
# ---------------------------------------------------------------------------

_PIANCA_CORNICE_SPAZI10_COLUMNS = [
    'L. Opaco / Essenza', 'Lucido Sp. / L. Metallico',
    'Pelle Sint. (Esterno)', 'Pelle Sint. (Copertura aggiuntiva)',
]


def parse_file_pianca_cornice_spazi10(path, product_name, brand, all_headings=None, heading_text=None):
    """See module comment above for scope."""
    if product_name != 'Cornice':
        return [], []
    with open(path, encoding='utf-8') as f:
        lines = f.read().split('\n')
    page_of_line = [None] * len(lines)
    current_page = None
    for idx, ln in enumerate(lines):
        m = re.match(r'^<<<PDFPAGE:(\d+)>>>$', ln.strip())
        if m:
            current_page = int(m.group(1))
        page_of_line[idx] = current_page

    n_cols = len(_PIANCA_CORNICE_SPAZI10_COLUMNS)
    rows = []
    flags = []
    i = 0
    in_table = False
    l_val = None
    row_type = None
    blank_run = 0
    while i < len(lines):
        stripped = lines[i].strip()
        toks = stripped.split()
        if toks[-8:] == ['L.', 'Opaco', 'Lucido', 'Sp.', 'Pelle', 'Sint.', 'Pelle', 'Sint.']:
            in_table = True
            l_val = None
            row_type = None
            blank_run = 0
            i += 2  # past header + its 'Essenza / L. Metallico' sub-line
            continue
        if not in_table:
            i += 1
            continue

        if stripped == '':
            blank_run += 1
            if blank_run >= 15:
                in_table = False
            i += 1
            continue
        blank_run = 0

        if len(toks) <= n_cols:
            # Context-only line: a new L-group heading ("120 (1 vano
            # anta)"), the "Interno e cappello" section divider (ignored
            # -- immediately superseded by "L. Opaco"/"L. Metallico" on
            # the next real row anyway), a bare "Copertura" reset, or
            # unrelated diagram noise.
            if re.match(r'^\d+(\.\d+)?\s*\(', stripped):
                l_val = toks[0]
            elif stripped == 'Copertura':
                row_type = None
            i += 1
            continue

        trailing = toks[-n_cols:]
        if not all(_PIANCA_PRICE_CELL_RE.match(t) for t in trailing) or not any(t != '-' for t in trailing):
            if stripped == 'Copertura':
                row_type = None
            i += 1
            continue

        pre = toks[:-n_cols]
        hp_idx = None
        for k in range(len(pre) - 2, -1, -1):
            if re.match(r'^\d+(\.\d+)?$', pre[k]) and re.match(r'^\d+(\.\d+)?$', pre[k + 1]):
                hp_idx = k
                break
        if hp_idx is not None:
            leading = _pianca_strip_leading_diagram_noise(pre[:hp_idx])
            h_val, p_val = pre[hp_idx], pre[hp_idx + 1]
            code_tokens = pre[hp_idx + 2:]
        else:
            # The "Copertura" sub-table's own rows print only ONE
            # dimension before the code (e.g. "A  45  06DH4F  - - - 180",
            # the leading "A" a stray diagram-reference letter), not the
            # H/P pair the "Interno e cappello" rows have -- confirmed
            # real via direct inspection, not a parsing failure to work
            # around blindly. Strips leading bare-single-letter diagram
            # noise ("A", confirmed never a real row-type value for this
            # product's own Copertura rows) -- deliberately NOT reusing
            # _pianca_strip_leading_diagram_noise, which also strips bare
            # NUMBERS and would wrongly eat the real H value itself here.
            # P is left unset (None) rather than guessed at which of H/P
            # this lone value actually is.
            noise_stripped = list(pre)
            while noise_stripped and re.match(r'^[A-Z]$', noise_stripped[0]):
                noise_stripped.pop(0)
            if noise_stripped and re.match(r'^\d+(\.\d+)?$', noise_stripped[0]):
                leading = []
                h_val, p_val = noise_stripped[0], None
                code_tokens = noise_stripped[1:]
            else:
                flags.append((page_of_line[i], product_name,
                              f"Cornice (Spazi-10) row shape mismatch (no H or H/P found), skipped: {stripped[:120]!r}"))
                i += 1
                continue

        code = _pianca_wardrobe_consume_code(code_tokens)
        if code is None:
            flags.append((page_of_line[i], product_name,
                          f"Cornice (Spazi-10) row shape mismatch (code not recognized), skipped: {stripped[:120]!r}"))
            i += 1
            continue

        if leading and ' '.join(leading) != 'Interno e cappello':
            row_type = ' '.join(leading)

        dims = [d for d in (l_val, h_val, p_val) if d is not None]
        size = '×'.join(dims)
        any_price = False
        for column_label, cell in zip(_PIANCA_CORNICE_SPAZI10_COLUMNS, trailing):
            if cell == '-':
                continue
            any_price = True
            tier = f'{row_type} — {column_label}' if row_type else column_label
            rows.append({
                "brand": brand,
                "product_name": product_name,
                "model_variant": None,
                "variant_context": row_type,
                "size": size,
                "fabric_tier": tier,
                "tier_label": "Finish",
                "code": code,
                "price_eur": cell,
                "source_pdf_page": page_of_line[i],
            })
        if not any_price:
            flags.append((page_of_line[i], product_name, f"no price rows found for code {code}"))
        i += 1
    return rows, flags


def parse_file_pianca(path, product_name, brand, all_headings=None, heading_text=None):
    """Dispatcher: runs every Pianca shape parser over the same text and
    merges results. All header signatures are mutually exclusive by
    construction (Shape A ends in 'A B C H P Q', Norma Up's 2-axis ends
    in 6x 'Frontali', Mambo's OWN 2-axis matches one exact hardcoded
    header tuple, named-columns only matches a table explicitly listed
    in _PIANCA_SHAPEB_NAMED_HEADERS, flat-price ends in just 'Prezzo',
    Primo's dim-labeled shape ends in 'Materico' too but is checked only
    after named-columns explicitly rules it out, since a bare 'Materico'
    tail isn't itself in that registry), so rows never collide -- a plain
    union is correct, same reasoning as
    Ditre's Shape 1/Shape 2 merge (see parse_file_ditre) though simpler
    here since there's no shared-code overlap to resolve. Shape A's own
    scan flags EVERY 'CODICI' line it doesn't recognize, including every
    other shape's headers -- so any such flag on a page ANY other parser
    actually resolved rows for is dropped here as superseded, rather
    than left as a duplicate/stale flag alongside the real data.

    Armadi danger-table's own header scan has the identical problem for
    the same reason -- it also flags every 'H CODICI...' line it doesn't
    recognize, and 3 real products (Primo, Enea Up, Soffio Up) happen to
    have a coincidentally similar-looking header on a page their OWN
    dedicated shape already resolves for real (confirmed via a direct
    row-count check before adding this filter, not assumed safe). Same
    superseded-by-another-parser's-real-rows rule applied a second time,
    scoped to just this one parser's own flags."""
    sub_parsers = [
        parse_file_pianca_shape_a,
        parse_file_pianca_norma_up_2axis,
        parse_file_pianca_mambo_2axis,
        parse_file_pianca_shape_b_named,
        parse_file_pianca_flat_price,
        parse_file_pianca_shape_a_pelle,
        parse_file_pianca_cora,
        parse_file_pianca_tavoli_piano,
        parse_file_pianca_tavoli_metallo,
        parse_file_pianca_siviglia_matrix,
        parse_file_pianca_primo_dim_labeled,
        parse_file_pianca_letti_tier,
        parse_file_pianca_composizione_bundle,
        parse_file_pianca_armadi_danger,
        parse_file_pianca_brema,
        parse_file_pianca_ginevra,
        parse_file_pianca_grafica,
        parse_file_pianca_logos_collezionegiorno,
        parse_file_pianca_people_collezionegiorno,
        parse_file_pianca_quadra,
        parse_file_pianca_tosca,
        parse_file_pianca_cornice_spazi10,
        parse_file_pianca_chloe,
        parse_file_pianca_intro,
        parse_file_pianca_delta_allungabile,
        parse_file_pianca_abaco,
        parse_file_pianca_aliseo,
        parse_file_pianca_baio,
    ]
    armadi_idx = sub_parsers.index(parse_file_pianca_armadi_danger)
    results = [p(path, product_name, brand, all_headings, heading_text) for p in sub_parsers]
    rows_a, flags_a = results[0]
    rows_armadi, flags_armadi = results[armadi_idx]

    resolved_pages = {r["source_pdf_page"] for _, (rows, _) in zip(sub_parsers[1:], results[1:]) for r in rows}
    flags_a_filtered = [
        f for f in flags_a
        if not (f[0] in resolved_pages and 'unrecognized price-table header' in f[2])
    ]

    resolved_pages_excl_armadi = {
        r["source_pdf_page"] for idx, (rows, _) in enumerate(results) if idx != armadi_idx for r in rows
    }
    flags_armadi_filtered = [
        f for f in flags_armadi
        if not (f[0] in resolved_pages_excl_armadi and 'unrecognized Armadi danger-table header' in f[2])
    ]

    all_rows = [r for rows, _ in results for r in rows]
    other_flags = [
        f for idx, (_, flags) in enumerate(results) if idx not in (0, armadi_idx) for f in flags
    ]
    all_flags = flags_a_filtered + flags_armadi_filtered + other_flags
    return all_rows, all_flags


_DITRE_FLAG_CODE_RE = re.compile(r'code (\S+)$')


def parse_file_ditre(path, product_name, brand, all_headings=None, heading_text=None):
    """Run BOTH Shape 1 and Shape 2 over the same text and merge per-CODE,
    preferring Shape 1. NOT a simple either/or fallback (an earlier
    version tried Shape 1 first, only falling back to Shape 2 if it
    found literally nothing) -- confirmed wrong on Tao outdoor, which
    has BOTH shapes' blocks in the same product's page range (Category-
    tier sofa/chair pages, "Cod. XXXXX" + "Name (CODE)" coffee-table
    pages) since Ditre prints matching indoor/outdoor furniture SETS
    together. Once Shape 1 finds real rows for a product it never even
    gets to Shape 2's blocks with an either/or dispatch, silently
    dropping the table pages.

    A naive concatenation of both shapes' full output is ALSO wrong:
    Shape 2 has no tier whitelist (relaxed deliberately, see its own
    definition), so it re-matches every Shape 1 row too under a
    different tier_label -- confirmed real: running Shape 2 alone
    against Ada (Sofa) reproduces its full 551 rows verbatim. The fix
    is a merge keyed on (code, fabric_tier) -- the same granularity
    main()'s own ambiguous-row detection already uses for Ditre (code
    always implies a specific size/variant here, unlike Bolzan) --
    preferring Shape 1's row for any (code, fabric_tier) it found;
    Shape 2's row for that same pair is only kept when Shape 1 found
    NONE. Flags follow the same code-level rule -- a "no price rows
    found" flag only survives if NEITHER shape resolved that code.

    Originally a per-CODE (not per-tier) decision: if Shape 1 found ANY
    row for a code, Shape 2's contribution for the WHOLE code was
    discarded. Confirmed wrong on Avalon mix once the Mix-ladder
    whitelist was widened to a "Mix by Leather" prefix rule (see
    DITRE_UPHOLSTERY_TIERS' own comment): Shape 1 now captures that
    code's Mix-ladder rows (previously 0, so Shape 2 used to win and
    correctly supply everything), but Shape 1 never captures its
    trailing "Majoration de prix pour sommiers dédoublés" surcharge row
    (not a Category/Leather/Mix label at all) -- the coarse per-code
    rule let Shape 1 "win" the code and silently drop a row only Shape
    2 had ever found, a regression a random-sample re-verification
    caught directly (Avalon mix went from 15/15 correct to 14/15,
    missing exactly that surcharge row).
    """
    rows1, flags1 = parse_file_ditre_upholstery(path, product_name, brand, all_headings, heading_text)
    rows2, flags2 = parse_file_ditre_casegoods(path, product_name, brand, all_headings, heading_text)

    covered_by_shape1 = {(r['code'], r['fabric_tier']) for r in rows1}
    rows2_unique = [r for r in rows2 if (r['code'], r['fabric_tier']) not in covered_by_shape1]
    rows = rows1 + rows2_unique

    # Exact-duplicate rows (same code/tier/price/variant/size) confirmed
    # real on Tao outdoor: a shared accessory line (e.g. "Backrests in
    # Iroko (LE20)") gets re-printed once per "Composition N" bundle
    # listing that includes the same component code, so Shape 2's scan
    # independently re-finds the identical row several times over. Safe
    # to collapse to one instance -- this can only remove BYTE-IDENTICAL
    # repeats, never a genuine conflicting value (those differ in price
    # and are correctly caught by the ambiguous-row detection below,
    # which works off a set of DISTINCT prices per key regardless).
    seen_row_keys = set()
    deduped_rows = []
    for r in rows:
        key = (r['code'], r['fabric_tier'], r['price_eur'], r['model_variant'], r['size'])
        if key in seen_row_keys:
            continue
        seen_row_keys.add(key)
        deduped_rows.append(r)
    rows = deduped_rows

    codes_resolved = {r['code'] for r in rows1} | {r['code'] for r in rows2_unique}

    def _flag_code(flag):
        m = _DITRE_FLAG_CODE_RE.search(flag[2])
        return m.group(1) if m else None

    seen_flags = set()
    flags = []
    for f in flags1 + flags2:
        code = _flag_code(f)
        if code is not None and code in codes_resolved:
            continue  # resolved by the OTHER shape -- not a real gap
        if f in seen_flags:
            continue  # both shapes independently flagged the same code
        seen_flags.add(f)
        flags.append(f)

    return rows, flags


def main():
    ap = argparse.ArgumentParser(
        description="Parse structured prices (code, size, price) out of every "
                     "product's raw text file, using catalog_index.json from "
                     "extract_catalog.py as the product list."
    )
    ap.add_argument("catalog_index", help="Path to catalog_index.json (e.g. data/Bolzan/catalog_index.json)")
    ap.add_argument("--out", default=None,
                     help="Output path for the combined prices JSON "
                          "(default: prices.json next to catalog_index.json)")
    ap.add_argument("--flags-out", default=None,
                     help="Optional output path for review_flags as structured "
                          "JSON (page/product_name/brand/reason), for tooling "
                          "like the orphaned-flags regression check to consume "
                          "instead of scraping stdout text.")
    ap.add_argument("--format", default="bolzan", choices=["bolzan", "cattelan", "bonaldo", "varaschini", "ditre", "pianca"],
                     help="Source table format. 'bolzan' = 'Codice'/'Prezzo' "
                          "tables (default, unchanged). 'cattelan' = "
                          "'Top'/'Base'/'MISURA CM' stacked grids, no Codice "
                          "concept at all. 'bonaldo' = 'RIVESTIMENTO'/"
                          "'CODICE'/'GAMBE' tables, every price has its own "
                          "code (chair shape only so far -- table and "
                          "modular-sofa shapes are flagged, not parsed). "
                          "'varaschini' = Shape A only ('cat. B - COM/C/D/E/"
                          "Luxury' fabric-tier tables); Shapes B/C/D/E are "
                          "not yet handled and every entry tagged with a "
                          "shape other than 'A' is flagged, not parsed. "
                          "'ditre' = bare-SKU-code header lines (no "
                          "'Codice:' prefix), two shapes auto-tried per "
                          "product (upholstery-category tier grid, "
                          "material-finish-code grid) -- verified against "
                          "exactly 2 products (Ada Sofa, Claire Tables) so "
                          "far, not yet generalized to the rest of either "
                          "shape.")
    args = ap.parse_args()

    index_path = Path(args.catalog_index)
    base_dir = index_path.parent.parent  # e.g. data/  (mini_pdf/text paths are relative to this)
    products = json.loads(index_path.read_text(encoding="utf-8"))

    all_names = [p["product_name"] for p in products]
    # For products disambiguated at extraction time (two genuinely
    # different items that share one bare printed heading -- see
    # index_heading in catalog_index.json), the literal text in the PDF is
    # the ORIGINAL un-renamed heading, not the disambiguated display name.
    all_headings = [p.get("index_heading", p["product_name"]) for p in products]

    all_rows = []
    empty_products = []
    review_flags = []  # (page, product_name, reason) -- format='cattelan' only

    if args.format == "varaschini":
        # Different iteration shape than the other 3 formats: Varaschini's
        # assets are deliberately deduped BY PAGE (517 unique page files
        # shared across 1,302 catalog entries), so this groups entries by
        # their shared text_file and parses each page ONCE, rather than
        # looping per-entry and redundantly re-parsing the same shared page
        # once per product on it.
        #
        # SCOPE: only shapes with an implemented parser (currently "A" and
        # "D") are attempted, and only entries with a single-page span
        # (printed_page_start == printed_page_end). Two things are
        # deliberately excluded, not silently included:
        #   1) Shapes with no parser yet (B/C/E) -- each is a genuinely
        #      different table format, added incrementally, one at a time,
        #      same discipline as Shape A/D.
        #   2) Entries whose recorded page range spans MULTIPLE pages (127
        #      of 727 Shape A entries, 10 of 194 Shape D entries, confirmed
        #      via direct counts, not assumed): some are a genuine embedded
        #      Shape-C-style modular sub-pattern inside Emma/Emma Cross
        #      (diagram page far from its price page, e.g. art_code
        #      "236M01" recorded as spanning printed pages 258->262)
        #      misclassified as plain Shape A during the structural walk;
        #      others are cross-references to an accessory code that
        #      already has its own correct entry elsewhere (e.g. Babylon
        #      mentions cushion codes 2708/2716, which are really "Cuscini
        #      e Tessuti" Shape D entries at page 571; Outdoor Cooking/Teli
        #      di Copertura have their own smaller instances of the same
        #      pattern). Neither case is safe to force through these
        #      parsers -- both need their own reclassification pass, not a
        #      guess here.
        SHAPE_PARSERS = {
            "A": parse_file_varaschini_shape_a,
            "B": parse_file_varaschini_shape_a,
            "C": parse_file_varaschini_shape_a,
            "D": parse_file_varaschini_shape_d,
            # No "REFERENCE_MATRIX"/"REFERENCE_MATRIX_GRID"/"C+BUNDLE" here
            # -- Teli di Copertura and Belt/Belt Air (the only collections
            # using those shapes) are both ALWAYS in
            # COLLECTION_PARSER_OVERRIDES below, which the dispatch key
            # picks over shape unconditionally -- an entry here for either
            # would be dead, unreachable code.
        }
        # Per-COLLECTION overrides of the generic per-shape parser, needed
        # when a collection shares Shape A's price-table format but not
        # its code layout. Cuscini e Tessuti is labeled "A" (folded in from
        # "D" -- confirmed its price table is Shape A's exact cat.
        # B-COM/C/D/E/Luxury tier structure) but its codes are bare, not
        # "art."-prefixed, so it needs parse_file_varaschini_shape_a's
        # SAME tier-extraction logic with Shape D's bare-code block finder
        # instead of the default "art." block finder. A plain
        # shape->parser map can't express this (both collections share the
        # shape "A" key but need different block_finder arguments), hence
        # this second, more specific lookup checked first.
        belt_all_codes = {p["art_code"] for p in products if p["collection"] == "Belt / Belt Air"}

        COLLECTION_PARSER_OVERRIDES = {
            "Cuscini e Tessuti": lambda path, page_num, entries, brand: parse_file_varaschini_shape_a(
                path, page_num, entries, brand,
                block_finder=_varaschini_find_flat_code_blocks,
                tier_label_re=VARASCHINI_TIER_LABEL_RE_BARE),
            # Composizione Tavoli needs the per-PAGE PDF (for pdftotext
            # -tsv coordinates), not the .txt file every other parser here
            # takes -- "path" (the .txt path) is ignored in favor of this
            # entry's own "mini_pdf" field.
            "Composizione Tavoli": lambda path, page_num, entries, brand: parse_file_varaschini_composizione_tavoli(
                str(base_dir / entries[0]["mini_pdf"]), page_num, entries, brand),
            # Basi Tavolini (flat SKU list) and Carpet Design (flat prices
            # + one per-square-meter item) both sit on pages too densely
            # packed for line-based block detection (adjacent codes' prices
            # bled together) -- the SAME TSV coordinate technique fixes
            # both, even though neither is an actual base x top MATRIX like
            # Composizione Tavoli itself. Verified 15/15 and 4/4 against
            # their page images (p586, p554).
            "Basi Tavolini": lambda path, page_num, entries, brand: parse_file_varaschini_composizione_tavoli(
                str(base_dir / entries[0]["mini_pdf"]), page_num, entries, brand),
            "Carpet Design": lambda path, page_num, entries, brand: parse_file_varaschini_composizione_tavoli(
                str(base_dir / entries[0]["mini_pdf"]), page_num, entries, brand),
            # Teli di Copertura (both shapes: pre-existing "94XXC"/new flat
            # "9C5XXX" codes on pages 558-564, and new grid "9C5XXX" codes
            # on pages 565-569) goes entirely through the TSV-coordinate
            # parser, not Shape A's "art." block finder -- confirmed the
            # block finder badly undercounts the flat pages too (several
            # "art. CODE" occurrences routinely share one physical line,
            # e.g. 5 across one row on p564; test run 7/16 priced on p562,
            # 1/31 on p564 vs 16/16 and 29/31 via coordinates), and the
            # collection's price table is uniformly a single flat price
            # (confirmed: 0 of its existing price rows have ever had a
            # fabric tier), so there's no Shape A tier structure being lost
            # by skipping that parser. See
            # parse_file_varaschini_teli_di_copertura's docstring.
            "Teli di Copertura": lambda path, page_num, entries, brand: parse_file_varaschini_teli_di_copertura(
                str(base_dir / entries[0]["mini_pdf"]), page_num, entries, brand),
            # Belt/Belt Air's composition codes (pages 129-131) share the
            # exact cat. B-COM/C/D/E/Luxury tier price table as Shape A --
            # only the BLOCK-BOUNDARY detection needs to differ (see
            # _varaschini_find_belt_composition_blocks), not the tier-
            # extraction logic itself.
            #
            # target_codes is deliberately EVERY Belt/Belt Air art_code in
            # the whole collection, not just `entries` (this page's own
            # catalog_index.json-anchored subset) -- confirmed 2026-08-23:
            # many Belt pages hold 2 products (e.g. p104: 2492 then 2493)
            # sharing ONE "art ." trigger between them, so only the FIRST
            # product ever gets its own catalog_index.json entry pointed at
            # that page (see extract_catalog.py's own page-anchor fix and
            # its documented, deliberately-NOT-fixed-here sibling gap: a
            # bare second code with no trigger of its own is invisible to
            # discovery). Scoping target_codes to `entries` alone means the
            # block finder never even LOOKS for "2493" on page 104, so
            # 2492's own block still ran to end-of-page, silently absorbing
            # 2493's entire price ladder plus 2 stray "OUTFIT COVER"
            # accessory prices (confirmed exact match: 5 real tier labels +
            # 5 bled-in ones = 10, 5+5+2 stray prices = 12 -- exactly the
            # "10 labels vs 12 prices" mismatch this was flagged as).
            # Widening to the full collection fixes this without touching
            # discovery at all: the block finder just needs to KNOW 2493 is
            # a real code so it can split on it wherever it happens to
            # appear as the first token of its own line -- it doesn't need
            # catalog_index.json to already agree that's this page's entry.
            # NOT widened on pages 129-131 (the composition-summary pages
            # this function was originally built for) -- confirmed
            # 2026-08-23: those pages list, for each composition, which
            # OTHER codes it's assembled from ("249C5", "2494", "2493", ...
            # as a bare ingredient list), not a new product's own price
            # block starting. Widening target_codes there let an
            # unrelated-but-real Belt code appearing mid-ingredient-list
            # look like a fresh block boundary, truncating 249C4C/249C5C's
            # own block before it ever reached its own real price line --
            # a regression this exact page range's own dedicated
            # entries_for_page scoping (the ORIGINAL, correct behavior)
            # never had, since it only ever considers the small set of
            # codes actually discovered as compositions on THIS page.
            "Belt / Belt Air": lambda path, page_num, entries, brand: parse_file_varaschini_shape_a(
                path, page_num, entries, brand,
                block_finder=lambda lines: _varaschini_find_belt_composition_blocks(
                    lines,
                    {e["art_code"] for e in entries} if 129 <= page_num <= 131 else belt_all_codes,
                )),
        }
        # Collections excluded from Shape D even though still labeled "D"
        # (their price tables genuinely are flat SKU lists -- unlike
        # Cuscini e Tessuti/Teli di Copertura, which were relabeled away
        # from "D" entirely because their PRICE TABLE format doesn't match
        # it at all).
        #   (Outdoor Cooking, Basi Tavolini, and Carpet Design were all
        #   here at various points. Outdoor Cooking's real problem turned
        #   out to be the SAME phantom-catalog-entry pattern fixed
        #   elsewhere this session (dash-prefixed "- art. XXXX" cross-
        #   references independently indexed as if they were their own
        #   products), not a genuine addon-price structural difference --
        #   confirmed by checking several of its "not found"/"ambiguous"
        #   flags directly: e.g. "25253" only ever appears in EXPLANATORY
        #   PROSE ("Utilizzabile in contemporanea con l'articolo 25253"),
        #   never as a real trigger anywhere on its recorded page. No
        #   special sub-rule needed; the parser already correctly flags
        #   these rather than guessing. 13/24 (54%) now parse cleanly,
        #   verified un-excluding it doesn't fabricate on the remainder.
        #   Basi Tavolini/Carpet Design are fixed via
        #   COLLECTION_PARSER_OVERRIDES above instead of excluded.)
        SHAPE_D_EXCLUDED_COLLECTIONS = set()
        # Multi-page-recorded entries individually verified (direct page-text
        # read, one at a time) to have their COMPLETE, clean price table
        # entirely on their recorded FIRST page -- the later pages in the
        # span hold only description/diagram/compatibility content the price
        # table itself doesn't need. Confirmed for all of these: Outdoor
        # Cooking's kitchen units + their inline "cover - art. XXXX €YYY"
        # accessories (p389/390, spans recorded as 389-391/390-391 -- the
        # "+391" page is a shared cellar/waste-holder accessory page that
        # only MENTIONS these art_codes in a compatibility row, confirmed
        # NOT their own trigger), and Victor's cushion/headrest/wheel-set
        # sub-items (p506/507/511, spans recorded as 506-509/506-512/511-513
        # for the same reason -- later pages are OTHER Victor products'
        # own price tables that happen to share the multi-page recording).
        #
        # This is deliberately a narrow, individually-verified ALLOWLIST,
        # not a blanket "always try page 1 of a multi-page span" relaxation
        # -- that would risk silently mis-parsing genuinely multi-page cases
        # the same broader audit ruled out for this exact reason:
        #   - Big/Big Light's own multi-page entries land in the already-
        #     documented p147-157 diagram-cluttered cluster where even
        #     single-page block-boundary detection is known to break down
        #     (see the Shape B comment above) -- attempting page 1 there
        #     risks a corrupted/partial block, not a clean read.
        #   - Emma/Emma Cross's ~88 multi-page entries are a genuine
        #     diagram-page-far-from-price-page case (confirmed art_code
        #     "236M01" spanning printed pages 258->262).
        #   - Plinto's "(pag. 412)"-style entries: the art_code appearing on
        #     the recorded first page is only a CROSS-REFERENCE mention
        #     inside a DIFFERENT product's own accessory line (e.g.
        #     "cuscino schienale - art. 24610H (pag. 412)"), not this
        #     product's own "art." trigger -- its real price table is on
        #     page 412, not the recorded start page. Outdoor Cooking's own
        #     "25220" (Madia) turned out to be this exact same pattern
        #     (p391 only lists it in a compatibility row) and was excluded
        #     from this allowlist for that reason, not included.
        # Extend this set only after the same direct per-entry verification.
        MULTI_PAGE_SINGLE_PAGE_SAFE = {
            "25201", "25202", "25203", "25204", "9456C", "9457C",  # Outdoor Cooking
            "218P", "2822C", "2822CT", "2822R", "2823C", "2823CT", "285P", "285P2",  # Victor
        }
        skipped_wrong_shape = 0
        skipped_multi_page = 0
        skipped_excluded_collection = 0
        # group by (page, dispatch_key) so mixed-shape/mixed-override pages
        # still get each group's entries routed to the right parser.
        # dispatch_key is the collection name when a per-collection
        # override exists, otherwise the shape -- this keeps Cuscini e
        # Tessuti (shape "A", override parser) from being grouped together
        # with real "art."-prefixed Shape A entries that happen to share a
        # page, even though that never actually occurs today (Cuscini e
        # Tessuti's pages are its own), it's the correct general rule.
        pages_to_entries: dict[tuple[int, str], list] = {}
        for p in products:
            shape = p.get("shape")
            collection = p["collection"]
            if collection not in COLLECTION_PARSER_OVERRIDES and shape not in SHAPE_PARSERS:
                skipped_wrong_shape += 1
                continue
            if shape == "D" and collection in SHAPE_D_EXCLUDED_COLLECTIONS:
                skipped_excluded_collection += 1
                continue
            if p["printed_page_start"] != p["printed_page_end"] and p.get("art_code") not in MULTI_PAGE_SINGLE_PAGE_SAFE:
                skipped_multi_page += 1
                continue
            dispatch_key = collection if collection in COLLECTION_PARSER_OVERRIDES else shape
            pages_to_entries.setdefault((p["printed_page_start"], dispatch_key), []).append(p)

        print(f"varaschini format: {len(pages_to_entries)} (page, dispatch) groups covering "
              f"{sum(len(v) for v in pages_to_entries.values())} entries across shapes "
              f"{sorted(SHAPE_PARSERS)} + collection overrides {sorted(COLLECTION_PARSER_OVERRIDES)} "
              f"({skipped_wrong_shape} entries in shapes with no parser yet, "
              f"{skipped_excluded_collection} entries in excluded known-gap collections, and "
              f"{skipped_multi_page} multi-page-span entries excluded from this pass).")

        for (page_num, dispatch_key), entries in pages_to_entries.items():
            text_path = base_dir / entries[0]["text_file"]
            if not text_path.exists():
                print(f"  WARNING: text file missing for page {page_num} ({text_path})")
                continue
            parser_fn = COLLECTION_PARSER_OVERRIDES.get(dispatch_key) or SHAPE_PARSERS[dispatch_key]
            rows, flags = parser_fn(str(text_path), page_num, entries, entries[0]["brand"])
            review_flags.extend((page, name, reason, entries[0]["brand"]) for page, name, reason in flags)
            found_names = {r["product_name"] for r in rows}
            for e in entries:
                if e["product_name"] not in found_names:
                    empty_products.append(e["product_name"])
            all_rows.extend(rows)
    else:
        for p in products:
            text_path = base_dir / p["text_file"]
            if not text_path.exists():
                print(f"  WARNING: text file missing for {p['product_name']} ({text_path})")
                continue
            if args.format == "cattelan":
                heading_text = p.get("index_heading", p["product_name"])
                rows, flags = parse_file_cattelan(str(text_path), p["product_name"], p["brand"], all_headings, heading_text)
                review_flags.extend((page, name, reason, p["brand"]) for page, name, reason in flags)
            elif args.format == "bonaldo":
                heading_text = p.get("index_heading", p["product_name"])
                rows, flags = parse_file_bonaldo(str(text_path), p["product_name"], p["brand"], all_headings, heading_text)
                review_flags.extend((page, name, reason, p["brand"]) for page, name, reason in flags)
            elif args.format == "ditre":
                heading_text = p.get("index_heading", p["product_name"])
                rows, flags = parse_file_ditre(str(text_path), p["product_name"], p["brand"], all_headings, heading_text)
                review_flags.extend((page, name, reason, p["brand"]) for page, name, reason in flags)
            elif args.format == "pianca":
                heading_text = p.get("index_heading", p["product_name"])
                rows, flags = parse_file_pianca(str(text_path), p["product_name"], p["brand"], all_headings, heading_text)
                review_flags.extend((page, name, reason, p["brand"]) for page, name, reason in flags)
            else:
                rows = parse_file(str(text_path), p["product_name"], p["brand"], all_names)
            if not rows:
                empty_products.append(p["product_name"])
            all_rows.extend(rows)

    out_path = Path(args.out) if args.out else index_path.parent / "prices.json"

    # Flag ambiguous rows: same (product, code, fabric_tier) with conflicting
    # prices usually means the source page packs multiple structural
    # variants (e.g. wood vs iron frame) close together in a way that can't
    # be reliably disentangled from text alone. Rather than silently keep
    # one arbitrary value, mark ALL rows sharing that key as ambiguous so
    # the chat layer can fall back to showing the actual page screenshot
    # and asking the customer/team to confirm, instead of stating a price
    # with false confidence.
    #
    # For rows with no "code" at all (Cattelan has no Codice concept), the
    # (product, code, fabric_tier) key alone would collapse EVERY size of a
    # single-price-column product onto one bucket -- since different sizes
    # legitimately have different prices, that would flag nearly everything
    # as a false conflict. Fall back to a size/variant-aware key whenever
    # code is absent; this changes nothing for rows that DO have a real
    # code (all Bolzan rows), since code already implies a specific size
    # there.
    key_to_prices = {}
    for r in all_rows:
        key = (r["product_name"], r["code"], r["fabric_tier"]) if r["code"] is not None \
            else (r["product_name"], r["fabric_tier"], r["size"], r["model_variant"])
        key_to_prices.setdefault(key, set()).add(r["price_eur"])
    ambiguous_keys = {k for k, v in key_to_prices.items() if len(v) > 1}
    for r in all_rows:
        key = (r["product_name"], r["code"], r["fabric_tier"]) if r["code"] is not None \
            else (r["product_name"], r["fabric_tier"], r["size"], r["model_variant"])
        r["ambiguous"] = key in ambiguous_keys

    out_path.write_text(json.dumps(all_rows, ensure_ascii=False, indent=2), encoding="utf-8")

    n_ambiguous = sum(1 for r in all_rows if r["ambiguous"])
    print(f"Parsed {len(all_rows)} price rows across {len(products)} products.")
    print(f"Wrote: {out_path}")

    # A brand with hand-transcribed rows (manual_additions.json, sibling to
    # this brand's prices.json) needs merge_manual_additions.py run
    # immediately after this -- this file just OVERWROTE prices.json with
    # auto-parser-only output, silently dropping every manually-verified
    # row for any product manual_additions.json covers. Found the hard way
    # 2026-08-16: regenerating Bolzan/Cattelan's prices.json without this
    # second step dropped 92+39 products' worth of verified data (Wilma,
    # Hystrix, YODA Marble, Sierra pouf, and 127 others), surfacing as a
    # wave of seemingly-unrelated chat/regression failures days later.
    manual_path = out_path.parent / "manual_additions.json"
    if manual_path.exists():
        print(f"\n*** REMINDER: {manual_path} exists for this brand. ***")
        print(f"    Run this now, or every hand-transcribed row above just got silently dropped:")
        print(f"    python App/merge_manual_additions.py \"{out_path}\" \"{manual_path}\"")
    if n_ambiguous:
        affected = sorted(set(r['product_name'] for r in all_rows if r['ambiguous']))
        print(f"\n{n_ambiguous} rows flagged ambiguous (same code + fabric tier, "
              f"conflicting prices -- likely two structural variants like wood/iron "
              f"frame packed onto one dense page). These are marked \"ambiguous\": true "
              f"so the chat can show the page screenshot instead of guessing. "
              f"Affected products ({len(affected)}):")
        for name in affected:
            print(f"   - {name}")
    if empty_products:
        print(f"\n{len(empty_products)} products had NO price rows extracted "
              f"(likely non-price pages like finish swatches, index pages, or "
              f"legal/reference content -- worth a manual glance):")
        for name in empty_products:
            print(f"   - {name}")
    if review_flags:
        print(f"\n{len(review_flags)} block(s) flagged during {args.format}-format parsing "
              f"for manual review (skipped rather than guessed -- these need hand "
              f"transcription into manual_additions.json after checking the real page):")
        for page, name, reason, brand in review_flags:
            page_str = f"p{page}" if page is not None else "p?"
            print(f"   - [{page_str}] {name}: {reason}")

    if args.flags_out:
        flags_out_path = Path(args.flags_out)
        flags_out_path.write_text(json.dumps(
            [{"page": page, "product_name": name, "brand": brand, "reason": reason}
             for page, name, reason, brand in review_flags],
            ensure_ascii=False, indent=2
        ), encoding="utf-8")
        print(f"Wrote {len(review_flags)} review flag(s) to: {flags_out_path}")


if __name__ == "__main__":
    main()