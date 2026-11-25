"""Heuristic extraction of ISBN, author, and title clues from ebook filenames."""

from __future__ import annotations
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .isbn.extractor import extract_isbns_from_text
from .authors import split_and_normalize_authors

# Common release junk suffixes
JUNK_SUFFIXES = [
    r"[.\s_-]*(?:pdf|epub|mobi|azw3|chm)?[.\s_-]*ebook-[a-z0-9]+$",
    r"[.\s_-]+(?:pdf|epub|mobi|azw3)$",
    r"_(?:no)?drm$",
    r"\.(?:no)?drm$",
    r"\s+(?:no)?drm$",
    r"_\d+k?$",
    r"_v\d+(?:\.\d+)*$",
    r"\s*\(z-lib(?:rary)?(?:\.org)?\)$",
    r"\s*\[z-lib(?:rary)?(?:\.org)?\]$",
    r"\s*\(retail\)$",
    r"\s*\[retail\]$",
]


# Internal DTP / Acrobat / Word production artifact titles (InDesign, Quark, PageMaker, FrameMaker, Word, Amazon image slugs, ISBN-only slugs)
_GARBAGE_EMBEDDED_TITLE_RE = re.compile(
    r"^(?:"
    r"untitled\b|"
    r"[\s\-_]*(?:isbn[:\s\-]*)?(?:97[89][-\s\d]{10,16}|\d{9}[\dXx])(?:[\s\-_.]*(?:web|ebook|retail|book|print|pdf|epub|mobi|azw3|chm|cov\w*))*[\s\-_]*$|"
    r".*\.(?:indd|qxd|qxp|pmd|pm6|p65|fm|mif|doc|docx|pdf|ps|eps|dvi|tex|rtf|tmp|jpg|jpeg|png|gif|bmp|tif|tiff|webp)$|"
    r".*(?:_SCLZZZZ|_SS\d{2,4}_|_SY\d{2,4}_|_SX\d{2,4}_).*|"
    r"\d{2,4}[_\-\s].*(?:ffirs|ftoc|fbetw|flast|fpref|c01|ch01|cover|front|final|title)|"
    r"0\d\s+cover\b|"
    r"cover\s+final|"
    r"\d{3}_[A-Za-z0-9_]+$|"
    r"microsoft\s+word\s*-|"
    r"slide\s+\d+$"
    r")",
    re.IGNORECASE,
)


def strip_trailing_year_tag(text: Optional[str]) -> str:
    """
    Strip trailing parenthetical or bracketed publication year tags such as
    'Way Of the Lawless (1921)', 'Title [1895]', 'Title (2011-02-26)', or 'Title (Penguin, 1987)',
    while leaving standalone year titles like '1984' or '2001: A Space Odyssey' untouched.
    """
    if not text:
        return ""
    s = str(text).strip()
    s = re.sub(
        r"(?<=\S)\s*[\(\[]\s*(?:[^)\]\d]{0,40}?\b)?(?:18\d{2}|19\d{2}|20\d{2})(?:-\d{1,2}(?:-\d{1,2})?)?\s*[\)\]]\s*$",
        "",
        s,
        flags=re.IGNORECASE,
    ).strip()
    return s


def _strip_inline_release_and_isbn_junk(text: str) -> str:
    """Strip inline/trailing (retail), (pdf), (epub), (v5.0), trailing (YYYY) year tags, trailing ISBNs, and double extensions."""
    if not text:
        return ""
    s = text.strip()
    while re.search(r"\.(?:epub|pdf|mobi|azw3?|chm|djvu|fb2|lit|prc|pdb)$", s, flags=re.IGNORECASE):
        s = re.sub(r"\.(?:epub|pdf|mobi|azw3?|chm|djvu|fb2|lit|prc|pdb)$", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(
        r"\s*[\(\[]\s*(?:retail|ebook|e-book|pdf|epub|mobi|azw3?|chm|djvu|rtf|txt|doc|docx|html?|lit|prc|pdb|fb2|nodrm|no\s*drm|z-lib(?:rary)?(?:\.org)?|v\d+(?:\.\d+)*|\d{1,2}|ss)\s*[\)\]]",
        " ",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(
        r"\s*[\(\[]\s*(?:isbn[:\s\-]*)?(?:97[89][-\s]?(?:\d[-\s]?){9}\d|\d{9}[\dXx])\s*[\)\]]",
        " ",
        s,
        flags=re.IGNORECASE,
    )
    # Strip trailing ISBN + optional WEB/ebook/format suffix (when preceded by at least a 2-char title)
    s = re.sub(
        r"(?<=[\w\)\]])(?:\s*[-–—_]+\s*|\s+)(?:isbn[:\s\-]*)?(?:97[89][-\s]?(?:\d[-\s]?){9}\d|\d{9}[\dXx])(?:[\s_\-]*(?:web|ebook|retail|book|print|pdf|epub|mobi|azw3?|chm))*\s*$",
        "",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(r"(?:[-_]+|\s+-\s*)WEB\s*$", "", s, flags=re.IGNORECASE).strip()
    s = strip_trailing_year_tag(s)
    return s.strip()


def is_garbage_embedded_title(raw_title: Optional[str]) -> bool:
    """Return True if `raw_title` is an internal InDesign/Quark/Word production slug rather than a book title."""
    if not raw_title:
        return True
    s = str(raw_title).strip()
    if len(s) < 2:
        return True
    return bool(_GARBAGE_EMBEDDED_TITLE_RE.search(s))


def is_garbage_embedded_author(raw_author: Optional[str]) -> bool:
    """Return True if `raw_author` is an internal OS/DTP username (`fk`, `ncargill`, `jturner`, `admin`) rather than a book author."""
    if not raw_author:
        return True
    s = str(raw_author).strip()
    if len(s) <= 1:
        return True
    low = s.lower()
    if low in (
        "", "unknown", "unknown author", "..", ".", "admin", "administrator",
        "user", "owner", "default", "root", "guest", "author", "yyepg", "asura01",
        "fk", "jess", "bartdevries", "ncargill", "jturner", "ldilk", "asaul", "usuario",
    ):
        return True
    if re.search(r"\b(?:retail|epub|pdf|mobi|azw3|nodrm|z-lib)\b", low):
        return True
    # Single lowercase token with no spaces (e.g. 'fk', 'ncargill', 'jturner', 'ldilk', 'jsmith1')
    if " " not in s and re.match(r"^[a-z][a-z0-9_.-]{0,14}$", s):
        return True
    return False


def clean_book_title(raw_title: Optional[str]) -> Optional[str]:
    """Strip release suffixes (_nodrm, nodrm, (retail), (epub), -978... WEB, etc.) and normalize underscores in a book title."""
    if not raw_title:
        return None
    cleaned = raw_title.strip()
    # Ignore internal Quark/InDesign/Acrobat production codes like '000_ITWK_MicrosoftIFC_13Feb06' or '01 Cover FINAL.indd'
    if is_garbage_embedded_title(cleaned):
        return None
    for pat in JUNK_SUFFIXES:
        cleaned = re.sub(pat, "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"^SS\s+(?=[A-Z])", "", cleaned).strip()
    cleaned = _strip_inline_release_and_isbn_junk(cleaned)
    cleaned = re.sub(r"_\s+", ": ", cleaned)
    cleaned = re.sub(r"\s*_\s*", " ", cleaned)
    cleaned = _strip_inline_release_and_isbn_junk(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned or is_garbage_embedded_title(cleaned):
        return None
    return cleaned or None


def strip_edition_suffix(text: Optional[str]) -> str:
    """
    Strip trailing or bracketed edition markers such as 'Animal Clinical Chemistry 2nd ed',
    'Animal Cognition (3E)', 'Title, Second Edition', or 'Title - 4th ed.' for catalog fallback queries.
    """
    if not text:
        return ""
    s = str(text).strip()
    s = re.sub(
        r"\s*[\(\[]\s*(?:\d+(?:st|nd|rd|th)?\s*(?:ed\.?|edition|e)|(?:first|second|third|fourth|fifth|sixth|revised)\s+edition)\s*[\)\]]",
        " ",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(
        r"(?:,|\s+-)?\s+\b(?:\d+(?:st|nd|rd|th)\s+ed(?:ition|\.)?|(?:first|second|third|fourth|fifth|sixth)\s+edition)\s*$",
        "",
        s,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", s).strip(" -:,;")


def _split_top_level_dash(text: str) -> List[str]:
    """
    Split `text` on `' - '` ONLY when outside `(...)` and `[...]` brackets,
    preventing splits inside '[Vol 2 - Polymer Matrix Composites]'.
    """
    norm = re.sub(r"\s+-\s+", " - ", text)
    parts: List[str] = []
    cur: List[str] = []
    depth_paren = 0
    depth_brack = 0
    i = 0
    n = len(norm)
    while i < n:
        ch = norm[i]
        if ch == "(":
            depth_paren += 1
        elif ch == ")" and depth_paren > 0:
            depth_paren -= 1
        elif ch == "[":
            depth_brack += 1
        elif ch == "]" and depth_brack > 0:
            depth_brack -= 1
        if depth_paren == 0 and depth_brack == 0 and norm[i : i + 3] == " - ":
            parts.append("".join(cur).strip())
            cur = []
            i += 3
            continue
        cur.append(ch)
        i += 1
    parts.append("".join(cur).strip())
    return [p for p in parts if p]


_ET_AL_RE_FN = re.compile(r"(?:,\s*|\s+)et\s+al\.?(?:,)?\s*$", re.IGNORECASE)


def parse_filename(file_path: Union[str, Path]) -> Dict[str, Any]:
    """
    Extract clues from a file name.
    Returns:
    {
        "isbn": Optional[str],
        "title": Optional[str],
        "authors": List[str],
        "raw_stem": str,
    }
    """
    path = Path(file_path)
    stem = path.stem

    # 1. Check for ISBN anywhere in the filename
    isbns = extract_isbns_from_text(stem)
    found_isbn = isbns[0][0] if isbns else None

    # 2. Extract trailing (Publisher, Year) WW / (Publisher) WW BEFORE generic year-tag stripping
    cleaned = stem
    for pat in JUNK_SUFFIXES:
        cleaned = re.sub(pat, "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"^SS\s+(?=[A-Z])", "", cleaned).strip()

    author_candidate = None
    title_candidate = None
    pub_year_candidate = None
    had_trailing_pub_paren = False

    m_pub_tail = re.search(
        r"\s*\((?:[^)]*?\b(18\d{2}|19\d{2}|20\d{2})\b[^)]*)\)\s*(?:WW|YYePG|BLG|TLRG|DDU|LotB)?$",
        cleaned,
        re.I,
    )
    if m_pub_tail:
        pub_year_candidate = m_pub_tail.group(1)
        had_trailing_pub_paren = True
        cleaned = cleaned[: m_pub_tail.start()].strip()
    else:
        # Also match publisher-only parens before release group tag, e.g. "(Humana) WW", "(Springer) WW", "(CRC) WW"
        m_pub_no_yr = re.search(
            r"\s*\([A-Za-z][A-Za-z0-9\s&.,'\-]{1,35}\)\s+(?:WW|YYePG|BLG|TLRG|DDU|LotB)$",
            cleaned,
            re.I,
        )
        if m_pub_no_yr:
            had_trailing_pub_paren = True
            cleaned = cleaned[: m_pub_no_yr.start()].strip()
        else:
            cleaned = re.sub(r"\s+(?:WW|YYePG|BLG|TLRG|DDU|LotB)$", "", cleaned).strip()

    cleaned = _strip_inline_release_and_isbn_junk(cleaned)
    if "." in cleaned and " " not in cleaned and cleaned.count(".") >= 2:
        cleaned = cleaned.replace(".", " ").strip()

    top_dash_parts = _split_top_level_dash(cleaned) if " -- " not in cleaned else []

    # 3. Detect Anna's Archive "Title -- Author -- Publisher, Year -- ISBN -- MD5" or "Author(s) - Title" pattern
    if " -- " in cleaned:
        aa_parts = [p.strip() for p in cleaned.split(" -- ") if p.strip()]
        if aa_parts:
            title_candidate = aa_parts[0]
        if len(aa_parts) >= 2:
            author_candidate = aa_parts[1].split(";")[0].strip()
        for seg in aa_parts[2:]:
            m_yr = re.search(r"\b(19\d{2}|20\d{2})\b", seg)
            if m_yr:
                pub_year_candidate = m_yr.group(1)
                break
    elif len(top_dash_parts) >= 2:
        parts = top_dash_parts
        first_seg = parts[0].strip()
        last_seg = _ET_AL_RE_FN.sub("", parts[-1].strip()).strip(" ,;") if "et al" in parts[-1].lower() else parts[-1].strip()
        from .authors import is_series_number_segment, strip_series_and_redundant_author_from_title

        first_is_series = is_series_number_segment(first_seg)
        first_looks_like_title = bool(
            first_is_series
            or re.match(r"^(?:the|a|an)\s+\S+", first_seg, re.I)
            or re.search(
                r"\b(?:demystified|handbook|guide|manual|introduction|principles|programming|engineering|encyclopedia|dictionary|edition|ed\.|design|management|networks|systems|analysis|fundamentals|reference|complete|bible|secrets|for\s+dummies)\b",
                first_seg,
                re.I,
            )
        )
        last_looks_like_author = bool(
            last_seg
            and not re.match(r"^(?:the|a|an)\s+", last_seg, re.I)
            and (
                re.match(
                    r"^(?:[A-Z]\.\s*(?:[A-Z]\.\s*)*[A-Za-z\-]+|[A-Za-z]+,\s*[A-Z][A-Za-z.\s]*|[A-Z][a-z]+\s+(?:[A-Z]\.?\s+|[A-Z][a-z]+\s+)?[A-Z][a-z\-]+)(?:,\s*(?:[A-Z]\.\s*[A-Za-z\-]+|[A-Z][a-z]+\s+(?:[A-Z]\.?\s+)?[A-Z][a-z\-]+))*$",
                    last_seg,
                )
                or (had_trailing_pub_paren and len(last_seg.split()) <= 3)
            )
        )
        if last_looks_like_author and (had_trailing_pub_paren or first_looks_like_title):
            author_candidate = last_seg
            title_parts = [p.strip() for p in parts[:-1] if p.strip()]
            if len(title_parts) >= 2 and is_series_number_segment(title_parts[0]):
                title_parts = title_parts[1:]
            title_candidate = ": ".join(title_parts)
        elif first_looks_like_title and not first_is_series:
            title_candidate = f"{first_seg}: {' - '.join(parts[1:]).strip()}"
            author_candidate = None
        else:
            rem_parts = [p.strip() for p in parts[1:] if p.strip()]
            # Drop middle series segment in 'Author - [Series 01] - Title'
            if len(rem_parts) >= 2 and is_series_number_segment(rem_parts[0]):
                rem_parts = rem_parts[1:]
            rest_seg = " - ".join(rem_parts).strip()
            # Check if " - " is separating <Publication/Series> - <Issue/Date> (e.g. "Computer Idee 2013 - 01", "PC Games Hardware - 06 2012", "Science - 08 January 2010")
            rest_is_issue_or_date = bool(
                re.match(
                    r"^(?:\d{1,3}|(?:\d{1,2}\s+)?(?:19\d\d|20\d\d)(?:\s+[A-Za-z\-]+.*)?|(?:\d{1,2}\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*(?:[\s,\-]+\d{1,4})+|(?:issue|iss|no|nr|vol|volume|heft|ausgabe)\.?\s*#?\d+.*)$",
                    rest_seg,
                    re.I,
                )
            )
            first_has_year = bool(re.search(r"\b(19\d{2}|20\d{2})\b", first_seg))
            if rest_is_issue_or_date or first_has_year:
                title_candidate = cleaned
                author_candidate = re.sub(r"\s+\b(?:19\d{2}|20\d{2})\b\s*$", "", first_seg).strip() or None
            else:
                author_candidate = first_seg
                title_candidate = strip_series_and_redundant_author_from_title(rest_seg, first_seg) or rest_seg
    elif "-" in cleaned and "(" not in cleaned and "," in cleaned.split("-", 1)[0]:
        # Only split on a tight hyphen when the left side is "Last, First-Title" (e.g., "Dunne, J. W.-An Experiment with Time")
        parts = cleaned.split("-", 1)
        if len(parts) == 2:
            author_candidate = parts[0].strip()
            title_candidate = parts[1].strip()
    elif " by " in cleaned.lower():
        parts = re.split(r"\s+by\s+", cleaned, flags=re.IGNORECASE, maxsplit=1)
        title_candidate = parts[0].strip()
        author_candidate = parts[1].strip()
    else:
        title_candidate = cleaned

    # Clean title candidate: replace underscore separators often used for colons and strip trailing (Publisher, Year) WW tags
    if title_candidate:
        title_candidate = re.sub(r"\s*\([^)]*?\b(?:19\d{2}|20\d{2})\b[^)]*\)\s*(?:WW|YYePG|BLG|TLRG)?$", "", title_candidate, flags=re.I).strip()
        title_candidate = re.sub(r"\s+(?:WW|YYePG|BLG|TLRG)$", "", title_candidate).strip()
        title_candidate = _strip_inline_release_and_isbn_junk(title_candidate)
        # Restore version decimal points where Anna's Archive replaced '.' with '_ ' between digits (e.g. 'JDK 1_ 4' -> 'JDK 1.4')
        title_candidate = re.sub(r"(?<=\d)_\s*(?=\d)", ".", title_candidate)
        # e.g. "Unabridged_ The Thrill of..." -> "Unabridged: The Thrill of..."
        title_candidate = re.sub(r"_\s+", ": ", title_candidate)
        title_candidate = re.sub(r"\s*_\s*", " ", title_candidate)
        title_candidate = _strip_inline_release_and_isbn_junk(title_candidate)
        title_candidate = re.sub(r"\s+", " ", title_candidate).strip()
        if is_garbage_embedded_title(title_candidate):
            title_candidate = None

    # Clean authors candidate
    parsed_authors: List[str] = []
    if author_candidate:
        # Strip leading numeric library/Calibre IDs (e.g., "16685198 Mark Willenbrink")
        author_candidate = re.sub(r"^\d{5,12}\s+", "", author_candidate).strip()
        # Strip Anna's Archive / bibliographic role tags like (auth_), (auth.), (ed_), (ed.), (eds_)
        author_candidate = re.sub(r"\s*\((?:auth|ed|eds|tr|trans|comp)[_.]?\)", "", author_candidate, flags=re.I).strip()
        if author_candidate.lower() not in ("unknown", "unknown author"):
            norm_authors = split_and_normalize_authors(author_candidate)
            parsed_authors = [disp for disp, _ in norm_authors]

    return {
        "isbn": found_isbn,
        "title": title_candidate or None,
        "authors": parsed_authors,
        "published_date": pub_year_candidate,
        "raw_stem": stem,
    }
