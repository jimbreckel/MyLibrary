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


def clean_book_title(raw_title: Optional[str]) -> Optional[str]:
    """Strip release suffixes (_nodrm, nodrm, etc.) and normalize underscores in a book title."""
    if not raw_title:
        return None
    cleaned = raw_title.strip()
    # Ignore internal Quark/InDesign/Acrobat production codes like '000_ITWK_MicrosoftIFC_13Feb06'
    if re.match(r"^\d{3}_[A-Za-z0-9_]+$", cleaned):
        return None
    for pat in JUNK_SUFFIXES:
        cleaned = re.sub(pat, "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"_\s+", ": ", cleaned)
    cleaned = re.sub(r"\s*_\s*", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


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

    # 2. Strip junk suffixes
    cleaned = stem
    for pat in JUNK_SUFFIXES:
        cleaned = re.sub(pat, "", cleaned, flags=re.IGNORECASE).strip()
    if "." in cleaned and " " not in cleaned and cleaned.count(".") >= 2:
        cleaned = cleaned.replace(".", " ").strip()

    # 3. Detect Anna's Archive "Title -- Author -- Publisher, Year -- ISBN -- MD5" or "Author(s) - Title" pattern
    author_candidate = None
    title_candidate = None
    pub_year_candidate = None

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
    elif " - " in cleaned:
        parts = cleaned.split(" - ", 1)
        author_candidate = parts[0].strip()
        title_candidate = parts[1].strip()
    elif "-" in cleaned:
        # Avoid splitting inside hyphenated words if only one hyphen
        # e.g., "Dunne, J. W.-An Experiment with Time"
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

    # Clean title candidate: replace underscore separators often used for colons
    if title_candidate:
        # e.g. "Unabridged_ The Thrill of..." -> "Unabridged: The Thrill of..."
        title_candidate = re.sub(r"_\s+", ": ", title_candidate)
        title_candidate = re.sub(r"\s*_\s*", " ", title_candidate)
        title_candidate = re.sub(r"\s+", " ", title_candidate).strip()

    # Clean authors candidate
    parsed_authors: List[str] = []
    if author_candidate:
        # Strip leading numeric library/Calibre IDs (e.g., "16685198 Mark Willenbrink")
        author_candidate = re.sub(r"^\d{5,12}\s+", "", author_candidate).strip()
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
