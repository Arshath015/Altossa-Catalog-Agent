import re, json, argparse, sys
from pathlib import Path

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
    ap.add_argument("--format", default="bolzan", choices=["bolzan", "cattelan"],
                     help="Source table format. 'bolzan' = 'Codice'/'Prezzo' "
                          "tables (default, unchanged). 'cattelan' = "
                          "'Top'/'Base'/'MISURA CM' stacked grids, no Codice "
                          "concept at all.")
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
    for p in products:
        text_path = base_dir / p["text_file"]
        if not text_path.exists():
            print(f"  WARNING: text file missing for {p['product_name']} ({text_path})")
            continue
        if args.format == "cattelan":
            heading_text = p.get("index_heading", p["product_name"])
            rows, flags = parse_file_cattelan(str(text_path), p["product_name"], p["brand"], all_headings, heading_text)
            review_flags.extend(flags)
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
        print(f"\n{len(review_flags)} block(s) flagged during Cattelan-format parsing "
              f"for manual review (skipped rather than guessed -- these need hand "
              f"transcription into manual_additions.json after checking the real page):")
        for page, name, reason in review_flags:
            page_str = f"p{page}" if page is not None else "p?"
            print(f"   - [{page_str}] {name}: {reason}")


if __name__ == "__main__":
    main()