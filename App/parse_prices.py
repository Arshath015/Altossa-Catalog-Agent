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
# RIVESTIMENTO/CODICE/GAMBE chair shape. Each of these 5 words was
# individually verified against real page text before being whitelisted
# here -- do not add more without the same check (the catalog has many
# OTHER numbered "CARATTERISTICHE TECNICHE" callout words, e.g. PIANO/
# RIPIANO/CONTENITORE/TOP/MONTANTE, that were seen during this search but
# NOT confirmed to follow this same simple grammar -- Roll's MONTANTE in
# particular looks like a genuinely different multi-named-column shape).
_BONALDO_SIMPLE_HEADER_WORDS = ('ANTE', 'STRUTTURA', 'PARALUME', 'BASE', 'CORNICE')
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
# Anchored to the WHOLE line (name-like text, 2+ spaces, trigger word, end
# of line) -- NOT just "trigger word present anywhere" -- because several
# of these words (esp. STRUTTURA/BASE) also appear constantly as numbered
# spec-callout labels ("1. STRUTTURA   Legno laccato...", "3. BASE...")
# inside CARATTERISTICHE TECNICHE blocks elsewhere on the same page.
# Those callout lines always start with a digit+period, which the
# name-like leading character class here excludes, so they're safely
# rejected without needing a separate digit check.
_BONALDO_SIMPLE_HEADER_RE_TEXT = (
    # Leading whitespace tolerated (confirmed real: Dune TV stand's "BASE
    # SAGOMATA"/"BASE A ZOCCOLO" sub-model headers are indented, unlike
    # every header seen while first building this) -- still safe from the
    # numbered-callout false positive, since a callout's first non-space
    # character is always a DIGIT, not a letter.
    r'^\s*[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9 \'"-]*\s{2,}(?:'
    + '|'.join(_BONALDO_SIMPLE_HEADER_WORDS) + r')\s*$'
)
_BONALDO_CLASSIC_2AXIS_RE_TEXT = (
    r'RIVESTIMENTO.*CODICE.*(?:' + '|'.join(_BONALDO_CLASSIC_2AXIS_WORDS) + r')'
)
BONALDO_CHAIR_HEADER_RE = re.compile(
    _BONALDO_CLASSIC_2AXIS_RE_TEXT + '|' + _BONALDO_SIMPLE_HEADER_RE_TEXT, re.IGNORECASE
)


def _bonaldo_header_trigger_word(line):
    """Return the display label for whichever header form matched `line`
    (used as the dynamic tier_label instead of hardcoding "Rivestimento" --
    the classic chair shape and the simple single-list shape use different
    real source-PDF words, e.g. Dune's is "Ante" not "Rivestimento")."""
    if re.search(_BONALDO_CLASSIC_2AXIS_RE_TEXT, line, re.IGNORECASE):
        return 'Rivestimento'
    m = re.match(_BONALDO_SIMPLE_HEADER_RE_TEXT, line, re.IGNORECASE)
    if m:
        return m.group(0).strip().split()[-1].capitalize()
    return None
BONALDO_FOOTER_START_RE = re.compile(r'n\s*[°º]\s*per\s*box', re.IGNORECASE)
# The footer's SECOND line (packaging m3/kg + client-fabric yardage,
# e.g. "1   0,28   5,00   COM m   2,65") doesn't contain "n per box" so
# needs its own check -- without this, its trailing numeric value was
# being misread as a code-less tier-row price (confirmed via real
# output: a phantom "COL m2" row with the previous tier's leftover code).
BONALDO_FOOTER_CONT_RE = re.compile(r'\bCOM\s?m\b|\bCOL\s?m2\b|\bBONALDO\b|\bESCLUSA\b', re.IGNORECASE)

# Fixed nav/section words that render in full uppercase just like a real
# product heading would -- excluded so they're never mistaken for one.
_BONALDO_NON_HEADING_WORDS = {
    'CARATTERISTICHE TECNICHE', 'A/Z', 'NEW', 'SEDIE', 'TAVOLI', 'COMPLEMENTI',
    'ILLUMINAZIONE', 'DIVANI', 'POLTRONE & POUF', 'LETTI', 'INDEX',
}


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
    # Zero groups is a real, verified shape (Dune/Obel/Mistral/Camillo-style
    # "simple list" products -- a single finish list with no leg/material
    # dimension at all, unlike chairs' RIVESTIMENTO x GAMBE combinations).
    # Substitute one implicit unnamed group so the row-count comparisons
    # and zip() below work the same way for 0 and 1+ declared groups,
    # without a separate code path -- model_variant just comes out None.
    effective_groups = group_names if group_names else [None]
    trigger_word = _bonaldo_header_trigger_word(lines[i]) or 'Rivestimento'

    rows = []
    unrecognized = 0
    # Tracks the most recently seen CODICE per group/column position, for
    # rows that omit a repeated code (see BONALDO_TIER_ROW_NOCODE_RE) --
    # reset whenever a fresh sub-variant starts, since codes are specific
    # to that sub-variant's own sequence (e.g. Mask's DU92/DU96 vs Miss
    # Mask's DU94/DU98 are unrelated).
    last_codes = [None] * len(effective_groups)
    k = j
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
        if BONALDO_CHAIR_HEADER_RE.search(line):
            # a genuinely NEW header (different group shape) -- stop here
            # so the caller parses it as its own block
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
    '800 - COM', '900', 'Class', 'Must', 'Special',
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
# A dimension-diagram callout ("62 cm - 24\"    70 cm - 28\"") can share a
# physical line with a real tier row purely by vertical-position
# coincidence, pushing the tier label off the line-start anchor (confirmed
# real: Seki's "Class" row). Stripped before tier-matching, same principle
# as _BONALDO_DIM_INFO_ANYWHERE_RE for table-shape.
_BONALDO_SOFA_DIM_PREFIX_RE = re.compile(r'^\s*(?:\d+\s*cm\s*-\s*\d+["”]?\s*)+')
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
_BONALDO_SOFA_SIZE_RE = re.compile(r'\d+(?:\s*x\s*H?\s*\d+)?')


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
    line into (names_with_pos, sizes_with_pos) -- both lists of
    (character_position, text). Returns (None, None) if `line` isn't a
    real sofa header."""
    m = BONALDO_SOFA_HEADER_RE.search(line)
    if not m:
        return None, None
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
    return names, sizes


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


def _parse_bonaldo_sofa_block(lines, i, seg_start, seg_end, page_of_line, product_name, brand, flags):
    """Parse one sofa "<name(s)>  RIVESTIMENTO  <size1> ..." header block
    starting at line i, through its CODICE row and fixed tier-price
    ladder, returning next_i for the caller to resume from."""
    names, sizes = _bonaldo_sofa_header_names_sizes(lines[i])
    if not sizes:
        flags.append((page_of_line[i], product_name,
                       f"sofa-shape header near line {i} has 'RIVESTIMENTO' but no size "
                       f"column(s) recognized -- skipped rather than guessed"))
        return [], i + 1
    col_sizes = [s for _, s in sizes]
    col_positions = [pos for pos, _ in sizes]
    variant_context = _bonaldo_sofa_variant_context_lookback(lines, i, seg_start, product_name)

    # Wide enough to survive intervening blank/nav-sidebar lines (confirmed
    # real: Superhiro's "Pouf"/"Cuscino" repeat line sits 12 lines after
    # its own header, separated by a "POLTRONE & POUF" sidebar badge).
    lookahead = lines[i + 1:min(seg_end, i + 20)]
    owners = _bonaldo_sofa_column_owners(names, sizes, lookahead)
    if owners is None:
        flags.append((page_of_line[i], product_name,
                       f"sofa-shape header near line {i} has {len(names)} element names "
                       f"sharing one header row but they could not be matched to their own "
                       f"size column(s) -- skipped rather than guessed"))
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
        bare_m = _BONALDO_BARE_PRICE_ROW_RE.match(rstripped) if not m else None
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
            pairs = [(mmm.start(), None, mmm.group()) for mmm in re.finditer(r'[\d.,]+', bare_m.group(1))]
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
    ap.add_argument("--flags-out", default=None,
                     help="Optional output path for review_flags as structured "
                          "JSON (page/product_name/brand/reason), for tooling "
                          "like the orphaned-flags regression check to consume "
                          "instead of scraping stdout text.")
    ap.add_argument("--format", default="bolzan", choices=["bolzan", "cattelan", "bonaldo"],
                     help="Source table format. 'bolzan' = 'Codice'/'Prezzo' "
                          "tables (default, unchanged). 'cattelan' = "
                          "'Top'/'Base'/'MISURA CM' stacked grids, no Codice "
                          "concept at all. 'bonaldo' = 'RIVESTIMENTO'/"
                          "'CODICE'/'GAMBE' tables, every price has its own "
                          "code (chair shape only so far -- table and "
                          "modular-sofa shapes are flagged, not parsed).")
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
            review_flags.extend((page, name, reason, p["brand"]) for page, name, reason in flags)
        elif args.format == "bonaldo":
            heading_text = p.get("index_heading", p["product_name"])
            rows, flags = parse_file_bonaldo(str(text_path), p["product_name"], p["brand"], all_headings, heading_text)
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