"""Extract candidate ISBNs from unstructured text using high-precision regexes."""

from __future__ import annotations
import re
from typing import List, Tuple
from .validator import normalize_isbn, validate_isbn10, validate_isbn13, clean_isbn


_UNICODE_DASH_RE = re.compile(r"[\u00ad\u2010\u2011\u2012\u2013\u2014\u2212]")

# Regex patterns
# 1. Labeled ISBNs: "ISBN-13: 978-0-13-235088-4", "ISBN (set): 0-08 0429939", "eISBN: 9780132350884"
LABELED_ISBN_RE = re.compile(
    r"(?P<label>(?:e-?|print\s+)?isbn(?:-?(?:10|13))?)"
    r"(?:\s*\([^)\n]{1,24}\))?\s*[:#\s_]\s*"
    r"(?P<candidate>(?:97[89][\s\-]*)?(?:\d[\s\-]*){9}[\dX])(?![0-9A-Za-z])",
    re.IGNORECASE,
)

# 2. Raw unlabelled ISBN-13 (starts with 978 or 979, with hyphens or spaces or raw digits, including underscore-delimited filenames like 978-1-4302-5398-3_Book)
RAW_ISBN13_RE = re.compile(
    r"(?<![0-9A-Za-z])(?P<candidate>97[89][\s\-]*(?:\d[\s\-]*){9}\d)(?![0-9A-Za-z])"
)

# 3. Raw unlabelled ISBN-10 (10 digits/X with hyphens e.g. 0-201-63361-2 or 0-080437206)
RAW_ISBN10_RE = re.compile(
    r"(?<![0-9A-Za-z])(?P<candidate>\d[\s\-]+(?:\d[\s\-]*){8}[\dX])(?![0-9A-Za-z])",
    re.IGNORECASE,
)

# 4. Contiguous 10-char ISBN-10 in filenames / Amazon cover image slugs (e.g. 3135239039.01._SS500_SCLZZZZZZZ_.jpg)
SLUG_ISBN10_RE = re.compile(
    r"(?<![0-9A-Za-z])(?P<candidate>[0-8]\d{8}[\dX])(?=(?:\.\d{2}\._[A-Z]{2}|[_\-\s\)]|$))",
    re.IGNORECASE,
)


def extract_isbns_from_text(text: str) -> List[Tuple[str, str]]:
    """
    Extract all valid ISBNs from text.
    Returns list of tuples: (isbn13, label_or_type), ranked with highest confidence first.
    """
    if not text:
        return []

    norm_text = _UNICODE_DASH_RE.sub("-", text)

    found: List[Tuple[str, str, int]] = []  # (isbn13, label, priority_score)
    seen_isbns = set()

    # Pass 1: Labeled patterns
    for match in LABELED_ISBN_RE.finditer(norm_text):
        label = match.group("label").strip().lower()
        cand = match.group("candidate")
        isbn13, _ = normalize_isbn(cand)
        if isbn13 and isbn13 not in seen_isbns:
            # Prefer volume-specific ISBNs over '(set)' multi-volume ISBNs
            full_match_low = match.group(0).lower()
            score = 85 if "(set)" in full_match_low else 100
            if "e" in label:
                score += 20
            elif "13" in label:
                score += 10
            found.append((isbn13, label, score))
            seen_isbns.add(isbn13)

    # Pass 2: Standalone raw ISBN-13 patterns
    for match in RAW_ISBN13_RE.finditer(norm_text):
        cand = match.group("candidate")
        isbn13, _ = normalize_isbn(cand)
        if isbn13 and isbn13 not in seen_isbns:
            found.append((isbn13, "raw-isbn13", 50))
            seen_isbns.add(isbn13)

    # Pass 3: Standalone raw ISBN-10 patterns (must have at least one hyphen/space to avoid random 10-digit numbers)
    for match in RAW_ISBN10_RE.finditer(norm_text):
        cand = match.group("candidate")
        if "-" in cand or " " in cand:
            isbn13, _ = normalize_isbn(cand)
            if isbn13 and isbn13 not in seen_isbns:
                found.append((isbn13, "raw-isbn10", 30))
                seen_isbns.add(isbn13)

    # Pass 4: Contiguous 10-character ISBN-10 in Amazon image slugs / filenames (validated with Mod-11 checksum)
    for match in SLUG_ISBN10_RE.finditer(norm_text):
        cand = match.group("candidate")
        if not cand.startswith(("19", "20")):
            isbn13, _ = normalize_isbn(cand)
            if isbn13 and isbn13 not in seen_isbns:
                found.append((isbn13, "slug-isbn10", 25))
                seen_isbns.add(isbn13)

    # Sort by priority score descending
    found.sort(key=lambda item: item[2], reverse=True)
    return [(isbn13, label) for isbn13, label, _ in found]
