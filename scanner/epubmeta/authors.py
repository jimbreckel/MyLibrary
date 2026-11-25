"""Author name normalization, splitting, and sort-order (file-as) formatting."""

from __future__ import annotations
import re
from typing import List, Tuple, Union

COMMON_SUFFIXES = {
    "jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v",
    "ph.d.", "phd", "m.d.", "md", "esq", "esq.", "cpa",
}

_ROLE_TAG_RE = re.compile(
    r"\s*(?:[\(\[]\s*(?:ed|eds|editor|editors|edited\s+by|compiled\s+by|comp|auth|author|tr|trans|translated\s+by|illustrator|illus)[_.:]?\s*[\)\]]"
    r"|[-–—]\s*(?:editor|editors|ed\.?|eds\.?|compiled\s+by|translated\s+by|illustrator)\s*$)",
    re.IGNORECASE,
)


def strip_author_role_tags(raw: str) -> str:
    """Strip parenthetical/bracketed bibliographic role markers such as '(ed)', '(editor)', '(Compiled by)'."""
    if not raw:
        return ""
    s = _ROLE_TAG_RE.sub(" ", str(raw))
    return re.sub(r"\s+", " ", s).strip()


def _split_first_last(name: str) -> Tuple[str, str]:
    """Split a natural name like 'Martin Luther King' into ('Martin Luther', 'King')."""
    tokens = [t.strip() for t in name.split() if t.strip()]
    if not tokens:
        return "", ""
    if len(tokens) == 1:
        return "", tokens[0]
    return " ".join(tokens[:-1]), tokens[-1]


def normalize_single_author(raw: str) -> Tuple[str, str]:
    """
    Takes a single author name in arbitrary format ('Dunne, J. W.' or 'Robert C. Martin')
    and returns: (display_name, file_as_name)
      e.g. ('J. W. Dunne', 'Dunne, J. W.')
    """
    if not raw:
        return "", ""

    cleaned = strip_author_role_tags(raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.rstrip(";")
    if not cleaned:
        return "", ""

    # Case 1: Comma format
    if "," in cleaned:
        parts = [p.strip() for p in cleaned.split(",") if p.strip()]
        if len(parts) == 2:
            part0, part1 = parts[0], parts[1]
            if part1.lower() in COMMON_SUFFIXES:
                firsts, last = _split_first_last(part0)
                display_name = f"{part0} {part1}"
                file_as = f"{last}, {firsts}, {part1}" if firsts else f"{last}, {part1}"
                return display_name, file_as
            else:
                display_name = f"{part1} {part0}"
                file_as = f"{part0}, {part1}"
                return display_name, file_as

        elif len(parts) == 3:
            if parts[2].lower() in COMMON_SUFFIXES:
                display_name = f"{parts[1]} {parts[0]} {parts[2]}"
                file_as = f"{parts[0]}, {parts[1]}, {parts[2]}"
                return display_name, file_as
            elif parts[1].lower() in COMMON_SUFFIXES:
                display_name = f"{parts[2]} {parts[0]} {parts[1]}"
                file_as = f"{parts[0]}, {parts[2]}, {parts[1]}"
                return display_name, file_as

    # Case 2: Natural order "Firstname [Middle...] Lastname [Suffix]"
    tokens = cleaned.split(" ")
    if len(tokens) == 1:
        return tokens[0], tokens[0]

    suffix = None
    if tokens[-1].lower() in COMMON_SUFFIXES and len(tokens) > 2:
        suffix = tokens[-1]
        tokens = tokens[:-1]

    last_name = tokens[-1]
    first_names = " ".join(tokens[:-1])

    if suffix:
        display_name = f"{first_names} {last_name} {suffix}"
        file_as = f"{last_name}, {first_names}, {suffix}"
    else:
        display_name = f"{first_names} {last_name}"
        file_as = f"{last_name}, {first_names}"

    return display_name, file_as


_ET_AL_SUFFIX_RE = re.compile(r"(?:,\s*|\s+)et\s+al\.?(?:,)?\s*$", re.IGNORECASE)


def _looks_like_natural_order_person(seg: str) -> bool:
    """Return True if `seg` looks like 'B. Frier', 'G. O. Evans', or 'Vivian A. Fonseca' rather than 'Last' or 'First' alone."""
    s = strip_author_role_tags(seg).strip()
    if not s or "," in s:
        return False
    toks = [t for t in s.split() if t]
    if not (2 <= len(toks) <= 4):
        return False
    if any(any(ch.isdigit() for ch in t) for t in toks):
        return False
    if toks[-1].lower() in COMMON_SUFFIXES:
        return False
    return all(t[0].isupper() or t.lower() in ("de", "del", "van", "von", "le", "la", "da", "di") for t in toks if t[0].isalpha())


def split_and_normalize_authors(
    authors_input: Union[str, List[str]]
) -> List[Tuple[str, str]]:
    """
    Takes a string or list of authors, splits multiple authors if combined,
    and returns a list of (display_name, file_as_name) tuples.

    Handles:
    - Lists: ["Nikolas Bowie", "Daphna Renan"]
    - Semicolon delimited: "Renan, Daphna; Bowie, Nikolas"
    - 'and' / '&' delimited: "Daphna Renan and Nikolas Bowie"
    - Comma-separated natural/initialed co-authors: "B. Frier, M. Fisher" or "A. Graesser, et al."
    - Alternating 'Last, First, Last, First': "Renan, Daphna, Bowie, Nikolas"
    """
    if not authors_input:
        return []

    # If it's already a list of strings
    raw_list: List[str] = []
    if isinstance(authors_input, list):
        for item in authors_input:
            if item and item.strip():
                s_item = _ET_AL_SUFFIX_RE.sub("", item.strip()).strip(" ,;")
                if not s_item:
                    continue
                if ";" in s_item:
                    raw_list.extend([x.strip() for x in s_item.split(";") if x.strip()])
                elif " and " in s_item.lower():
                    raw_list.extend([x.strip() for x in re.split(r"\s+and\s+", s_item, flags=re.IGNORECASE) if x.strip()])
                elif " & " in s_item:
                    raw_list.extend([x.strip() for x in s_item.split("&") if x.strip()])
                elif "," in s_item:
                    c_parts = [p.strip() for p in s_item.split(",") if p.strip()]
                    if len(c_parts) >= 2 and all(_looks_like_natural_order_person(p) for p in c_parts):
                        raw_list.extend(c_parts)
                    else:
                        raw_list.append(s_item)
                else:
                    raw_list.append(s_item)
    else:
        s = _ET_AL_SUFFIX_RE.sub("", authors_input.strip()).strip(" ,;")
        if not s:
            return []
        if ";" in s:
            raw_list = [x.strip() for x in s.split(";") if x.strip()]
        elif " and " in s.lower():
            raw_list = [x.strip() for x in re.split(r"\s+and\s+", s, flags=re.IGNORECASE) if x.strip()]
        elif " & " in s:
            raw_list = [x.strip() for x in s.split("&") if x.strip()]
        elif "," in s:
            comma_parts = [p.strip() for p in s.split(",") if p.strip()]
            if len(comma_parts) >= 2 and all(_looks_like_natural_order_person(p) for p in comma_parts):
                raw_list = comma_parts
            elif len(comma_parts) >= 4 and len(comma_parts) % 2 == 0:
                raw_list = [
                    f"{comma_parts[i]}, {comma_parts[i+1]}"
                    for i in range(0, len(comma_parts), 2)
                ]
            else:
                raw_list = [s]
        else:
            raw_list = [s]

    results: List[Tuple[str, str]] = []
    seen = set()
    for raw_author in raw_list:
        disp, file_as = normalize_single_author(raw_author)
        if disp and disp.lower() not in seen:
            results.append((disp, file_as))
            seen.add(disp.lower())

    return results


_TITLE_INDICATOR_WORDS = {
    "the", "a", "an", "of", "to", "in", "for", "on", "with", "without",
    "against", "beyond", "into", "from", "under", "over", "between",
    "through", "across", "among", "before", "after", "tales", "stories",
    "story", "novel", "novels", "science", "fiction", "adventures",
    "journey", "chronicles", "handbook", "guide", "manual", "book",
    "world", "worlds", "planet", "stars", "space", "time", "empire",
}


_SERIES_SEGMENT_RE = re.compile(
    r"^(?:\[[^\]]+\]|\([^)]*#\d+[^)]*\)|[A-Za-z0-9'’&.,\s]+?\s+#?\d{1,3}(?:\.\d+)?|(?:book|vol\.?|volume|part|tom|band)\s*#?\d{1,3})$",
    re.IGNORECASE,
)


def is_series_number_segment(seg: str) -> bool:
    """
    Return True if `seg` looks like a series/volume tag such as '[Phule 04]', 'Phule 04',
    'Thieves World 03', 'Apprentice Adept 01', 'Night Angel Trilogy 01', or 'Lightworld and Darkworld #03'.
    """
    s = (seg or "").strip()
    if not s or len(s) > 65:
        return False
    # Do not treat pure 4-digit years like '1984' or '2001' as series numbers
    if re.search(r"\b(?:18\d{2}|19\d{2}|20\d{2})\b", s) and not re.search(r"[\[\]#]", s):
        return False
    return bool(_SERIES_SEGMENT_RE.match(s))


def strip_trailing_series_from_author_field(raw_title: str) -> Tuple[str, bool]:
    """
    If `raw_title` has the form '<Author(s)> - <Series ##>' (e.g. 'Robert Asprin - Thieves World 03',
    'Piers Anthony - Apprentice Adept 01', 'Randall Garrett & Vicki Ann Heydron - Gandalara 01'),
    strip the trailing series segment and return `(author_part, True)`. Otherwise return `(raw_title, False)`.
    """
    s = (raw_title or "").strip()
    if not s or not re.search(r"\s+[-–—]\s+", s):
        return s, False
    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", s) if p.strip()]
    if len(parts) == 2 and is_series_number_segment(parts[1]):
        left = strip_author_role_tags(parts[0])
        # Verify left side has no digits and 1..6 words per co-author segment
        segs = [x.strip() for x in re.split(r"\s*(?:&|\band\b|;)\s*", left, flags=re.IGNORECASE) if x.strip()]
        if segs and len(segs) <= 4 and all(1 <= len(x.replace(",", " ").split()) <= 5 and not re.search(r"\d", x) for x in segs):
            return parts[0].strip(), True
    return s, False


def strip_series_and_redundant_author_from_title(
    raw_title: Optional[str],
    known_authors: Union[str, List[str], None],
) -> Optional[str]:
    """
    Clean a book title that redundantly embeds the author name and/or a series-number segment
    when `known_authors` already identifies the author:
      - 'Robert Asprin - Phule 04 - Phule Me Twice' (Author='Robert Asprin, Peter J. Heck') -> 'Phule Me Twice'
      - 'The Whale's Footprints - Rick Boyer' (Author='Rick Boyer') -> 'The Whale's Footprints'
      - 'Night Angel Trilogy 01 - Way of Shadows' (Author='Weeks, Brent') -> 'Way of Shadows'
      - '[Cormac Mac Art 07] - The Sign of the Moonbow' (Author='Andrew J. Offutt') -> 'The Sign of the Moonbow'
      - 'Phule Me Twice Robert Asprin & Peter J Heck' (Author='Robert Asprin, Peter J. Heck') -> 'Phule Me Twice'
    """
    from .filename_parser import clean_book_title
    from .matcher import score_authors

    if not raw_title:
        return None
    s = str(raw_title).strip()
    if not s:
        return None

    # Strip leading uppercase 'SS ' short-story prefix (e.g. 'SS The Goblin' -> 'The Goblin')
    s = re.sub(r"^SS\s+(?=[A-Z])", "", s).strip()

    auth_list: List[str] = []
    if known_authors:
        for disp, _ in split_and_normalize_authors(known_authors):
            if disp and disp.lower() not in ("unknown", "unknown author"):
                auth_list.append(disp)

    # 1. Split on ' - ' / ' – ' / ' — ' and peel redundant author segments and leading series-number segments
    if re.search(r"\s+[-–—]\s+", s):
        parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", s) if p.strip()]
        changed = True
        while len(parts) >= 2 and changed:
            changed = False
            first_p = parts[0]
            if auth_list and not re.search(r"\d", first_p) and len(first_p.split()) <= 6:
                if score_authors(auth_list, [first_p]) >= 0.75:
                    parts = parts[1:]
                    changed = True
                    continue
            if is_series_number_segment(first_p):
                parts = parts[1:]
                changed = True
                continue
            last_p = parts[-1]
            if auth_list and not re.search(r"\d", last_p) and len(last_p.split()) <= 6:
                if score_authors(auth_list, [last_p]) >= 0.75:
                    parts = parts[:-1]
                    changed = True
                    continue
        s = " - ".join(parts).strip()

    # 2. Strip trailing ' by <Author>' or glued trailing '<Author1 & Author2>' when matching `auth_list`
    if auth_list and len(s.split()) >= 4:
        m_by = re.search(r"^(.{3,}?)\s+by\s+([A-Z][A-Za-z.\s,&\-]+)$", s)
        if m_by and score_authors(auth_list, [m_by.group(2)]) >= 0.75:
            s = m_by.group(1).strip()
        else:
            words = s.split()
            for tail_len in range(min(7, len(words) - 2), 1, -1):
                tail_str = " ".join(words[-tail_len:])
                head_str = " ".join(words[:-tail_len]).strip(" -:,;")
                if not head_str or len(head_str) < 3:
                    continue
                _, tail_authors = clean_swapped_author_candidates(tail_str)
                if (
                    tail_authors
                    and len(tail_authors) >= 1
                    and (
                        score_authors(auth_list, tail_authors) >= 0.80
                        or (len(tail_authors) >= 2 and score_authors(auth_list, [tail_authors[0]]) >= 0.80)
                    )
                    and all(w[0].isupper() or w.lower() in ("&", "and", "de", "del", "van", "von", "le", "la") for w in tail_str.split() if w)
                    and head_str.lower() != tail_str.lower()
                ):
                    from .matcher import _extract_surnames
                    t_sur = set(_extract_surnames(tail_authors))
                    k_sur = set(_extract_surnames(auth_list))
                    # Single trailing author must match known surname; multi-author '&'/'and' tail requires primary author surname match + valid person tokens
                    coauthors_valid = all(
                        2 <= len(ca.split()) <= 4 and not (set(w.lower().strip(".") for w in ca.split()) & _TITLE_INDICATOR_WORDS)
                        for ca in tail_authors
                    )
                    if t_sur and (t_sur.issubset(k_sur) or (len(tail_authors) >= 2 and (t_sur & k_sur) and coauthors_valid)):
                        auth_tokens_low = {re.sub(r"[^a-z]", "", tok.lower()) for a in auth_list for tok in a.split() if tok}
                        first_tail_tok = re.sub(r"[^a-z]", "", words[-tail_len].lower())
                        if first_tail_tok and first_tail_tok in auth_tokens_low:
                            s = head_str
                            break

    cleaned = clean_book_title(s) or s.strip()
    return cleaned or None


_NON_PERSON_BY_WORDS = {
    "the", "a", "an", "of", "to", "in", "for", "on", "with", "without", "by", "at", "from",
    "black", "white", "red", "blue", "green", "dark", "light", "hole", "night", "day",
    "fire", "ice", "water", "blood", "death", "life", "war", "peace", "god", "hand", "hands",
    "heart", "design", "example", "default", "choice", "chance", "nature", "numbers",
    "decree", "proxy", "stealth", "storm", "magic", "sword", "court", "committee",
    "united", "states", "new", "york", "san", "francisco", "world", "computer", "machine",
    "gaslight", "force", "law", "sea", "land", "air", "trial", "jury", "step", "side",
    "piece", "bell", "me", "you", "us", "them", "himself", "herself", "itself", "one", "two",
    "all", "none", "champions", "chess", "book", "books", "press", "university", "edition",
}


def extract_inline_author_and_clean_title(
    raw_title: Optional[str],
    raw_authors: Union[str, List[str], None] = None,
    file_name: Optional[str] = None,
) -> Tuple[Optional[str], List[str]]:
    """
    Extract an inline author embedded inside `raw_title` when the book's author field is missing/'Unknown Author'
    (or matches the inline author), such as:
      - 'Long Calculation by Edward Winter' (Author='Unknown Author') -> ('Long Calculation', ['Edward Winter'])
      - 'Reinfeld’s Non-Chess Books (by Edward Winter)' -> ('Reinfeld’s Non-Chess Books', ['Edward Winter'])
      - 'Reminiscences by Capablanca (presented by Edward Winter)' -> ('Reminiscences by Capablanca', ['Edward Winter'])
      - 'Gordon Smyth - How to Beat the Computer at Chess (1995)' (when corroborated by filename 'Gordon Smyth - How to Beat...')
    Returns `(cleaned_title, extracted_authors)` or `(None, [])` if no safe inline author pattern applies.
    """
    from .filename_parser import clean_book_title, parse_filename, strip_trailing_year_tag
    from .matcher import score_authors

    if not raw_title:
        return None, []
    s = str(raw_title).strip()
    if len(s) < 5:
        return None, []

    existing_authors: List[str] = []
    if raw_authors:
        for disp, _ in split_and_normalize_authors(raw_authors):
            if disp and disp.lower() not in ("unknown", "unknown author", "—", "-"):
                existing_authors.append(disp)

    fn_clues = parse_filename(file_name) if file_name else {}
    fn_authors = [a for a in (fn_clues.get("authors") or []) if a and a.lower() not in ("unknown", "unknown author")]
    fn_title = fn_clues.get("title")

    s_no_yr = strip_trailing_year_tag(s) or s

    # 1. Parenthesized '(by Author)', '(presented by Author)', '(edited by Author)', '(compiled by Author)'
    m_paren = re.match(
        r"^(.{2,}?)\s*\(\s*(?:presented\s+by|edited\s+by|compiled\s+by|written\s+by|translated\s+by|by)\s+([A-Z][^()]{2,70})\)\s*$",
        s_no_yr,
        flags=re.IGNORECASE,
    )
    if m_paren:
        cand_t = clean_book_title(m_paren.group(1).strip()) or m_paren.group(1).strip()
        _, cand_a = clean_swapped_author_candidates(m_paren.group(2).strip())
        if cand_t and cand_a and all(not re.search(r"\d", a) for a in cand_a):
            return cand_t, existing_authors or cand_a

    # 2. Trailing '<Title> by <Author>' (e.g. 'Long Calculation by Edward Winter')
    m_by = re.match(r"^(.{2,}?)\s+by\s+([A-Z][A-Za-z.\s,'’&\-]{3,70})$", s_no_yr)
    if m_by:
        head_t = m_by.group(1).strip()
        tail_a_raw = m_by.group(2).strip()
        _, cand_a = clean_swapped_author_candidates(tail_a_raw)
        if cand_a and head_t:
            corroborated = bool(
                (fn_authors and score_authors(cand_a, fn_authors) >= 0.75)
                or (existing_authors and score_authors(cand_a, existing_authors) >= 0.75)
            )
            strict_person_shape = True
            for ca in cand_a:
                w_tokens = [w for w in ca.replace(",", " ").split() if w]
                if not (2 <= len(w_tokens) <= 4):
                    strict_person_shape = False
                    break
                if any(re.search(r"\d", w) for w in w_tokens):
                    strict_person_shape = False
                    break
                if not all(w[0].isupper() or w.lower() in ("de", "del", "van", "von", "le", "la") for w in w_tokens):
                    strict_person_shape = False
                    break
                low_set = {re.sub(r"[^a-z]", "", w.lower()) for w in w_tokens}
                if low_set & _NON_PERSON_BY_WORDS:
                    strict_person_shape = False
                    break

            if corroborated or strict_person_shape:
                cand_t = clean_book_title(head_t) or head_t
                if cand_t and len(cand_t) >= 2:
                    return cand_t, existing_authors or cand_a

    # 3. '<Author> - <Title>' left unsplit in title while `existing_authors` is empty/'Unknown Author', corroborated by filename
    if not existing_authors and fn_authors and fn_title and " - " in s_no_yr:
        left_seg, right_seg = s_no_yr.split(" - ", 1)
        _, left_a = clean_swapped_author_candidates(left_seg.strip())
        if (
            left_a
            and score_authors(left_a, fn_authors) >= 0.80
            and all(2 <= len(ca.split()) <= 4 and not re.search(r"\d", ca) for ca in left_a)
        ):
            cand_t = strip_series_and_redundant_author_from_title(right_seg.strip(), left_a) or clean_book_title(right_seg.strip())
            if cand_t and len(cand_t) >= 2:
                return cand_t, fn_authors

    return None, []


def clean_swapped_author_candidates(raw_title: str) -> Tuple[str, List[str]]:
    """
    Interpret `raw_title` as a potential switched author string (e.g. 'Martin H. Greenberg (ed)',
    'Leo Margulies & Oscar J. Fried (ed)', 'Robert Asprin - Thieves World 03', 'Driscoll, Maureen')
    and return `(primary_author, all_authors)`.
    """
    if not raw_title:
        return "", []
    s, _ = strip_trailing_series_from_author_field(raw_title)
    s = strip_author_role_tags(s)
    s = re.sub(r"\s*[\(\[](?:epub|mobi|azw3?|pdf|rtf|txt|doc|docx|retail|v\d+(?:\.\d+)*)[\)\]]\s*$", "", s, flags=re.IGNORECASE).strip()
    if not s or s.lower() in ("unknown", "unknown author", "untitled"):
        return "", []
    norm = split_and_normalize_authors(s)
    authors = [disp for disp, _ in norm if disp and disp.lower() not in ("unknown", "unknown author")]
    if not authors:
        return "", []
    return authors[0], authors


def clean_swapped_title_candidates(raw_authors: Union[str, List[str]]) -> List[str]:
    """
    Interpret `raw_authors` as a potential switched book title (e.g. ['Journey to Infinity'],
    ['The Emperor', 'the Maula'], 'The Emperor;the Maula', 'A Reason ... [ss]').
    Returns ordered candidate title strings.
    """
    from .filename_parser import clean_book_title

    items: List[str] = []
    if isinstance(raw_authors, (list, tuple)):
        for a in raw_authors:
            if a and str(a).strip():
                s_a = str(a).strip()
                if ";" in s_a:
                    items.extend([p.strip() for p in s_a.split(";") if p.strip()])
                else:
                    items.append(s_a)
    elif raw_authors:
        s_raw = str(raw_authors).strip()
        if ";" in s_raw:
            items = [p.strip() for p in s_raw.split(";") if p.strip()]
        else:
            items = [s_raw]

    items = [x for x in items if x and x.lower() not in ("unknown", "unknown author", "..", ".")]
    if not items:
        return []

    raw_variants: List[str] = []
    if len(items) == 1:
        raw_variants.append(items[0])
    elif len(items) == 2:
        # Calibre splits titles containing ' and ' into two <dc:creator> entries when Title/Author are swapped
        raw_variants.append(f"{items[0]} and {items[1]}")
        raw_variants.append(", ".join(items))
    else:
        raw_variants.append(", ".join(items))

    out: List[str] = []
    seen = set()
    for rv in raw_variants:
        cleaned = re.sub(
            r"\s*[\(\[]\s*(?:ss|short\s*story|novella|novelette|anthology|collection|omnibus)\s*[\)\]]\s*$",
            "",
            rv,
            flags=re.IGNORECASE,
        ).strip()
        cleaned = clean_book_title(cleaned) or cleaned
        if cleaned and len(cleaned) >= 2 and cleaned.lower() not in ("unknown", "unknown author"):
            low = cleaned.lower()
            if low not in seen:
                seen.add(low)
                out.append(cleaned)
    return out


def looks_like_swapped_title_and_author(title: str, authors: Union[str, List[str]]) -> bool:
    """
    Heuristic returning True when `title` strongly resembles a person/editor name
    while `authors` resembles a book title (e.g. Title='Martin H. Greenberg (ed)', Author='Journey to Infinity').
    """
    t = (title or "").strip()
    if not t:
        return False
    a_cands = clean_swapped_title_candidates(authors)
    if not a_cands:
        return False
    a_str = a_cands[0]

    # Explicit bibliographic role marker in title (e.g. '(ed)', '(eds)', '(editor)') is a strong swap signal
    if _ROLE_TAG_RE.search(t):
        return True

    # Compound '<Author(s)> - <Series ##>' in title (e.g. 'Robert Asprin - Thieves World 03' by 'Shadows of Sanctuary')
    t_base, had_series_tail = strip_trailing_series_from_author_field(t)
    if had_series_tail:
        from .matcher import score_authors
        _, cand_a_list = clean_swapped_author_candidates(t_base)
        # If `authors` is NOT the same person (i.e. `authors` holds the book title like 'Shadows of Sanctuary'), it's switched!
        if cand_a_list and score_authors(cand_a_list, [a_str]) < 0.65:
            return True

    t_clean = strip_author_role_tags(t_base)
    # Split on '&' / 'and' to inspect person-name segments
    t_segs = [seg.strip() for seg in re.split(r"\s*(?:&|\band\b|;)\s*", t_clean, flags=re.IGNORECASE) if seg.strip()]
    if not t_segs or len(t_segs) > 3:
        return False

    for seg in t_segs:
        words = seg.replace(",", " ").split()
        if not (2 <= len(words) <= 4):
            return False
        low_words = {re.sub(r"[^a-z]", "", w.lower()) for w in words}
        if low_words & _TITLE_INDICATOR_WORDS:
            return False
        if any(any(ch.isdigit() for ch in w) for w in words):
            return False
        if not all(w[0].isupper() for w in words if w and w[0].isalpha()):
            return False

    a_words = [re.sub(r"[^a-z0-9]", "", w.lower()) for w in a_str.split() if w]
    if not a_words:
        return False
    if a_words[0] in ("the", "a", "an") or (set(a_words) & _TITLE_INDICATOR_WORDS):
        return True
    if any(any(ch.isdigit() for ch in w) for w in a_words):
        return True
    if len(a_words) >= 4 or len(a_words) == 1:
        return True
    return False
