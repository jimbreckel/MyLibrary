"""Extract candidate ISBNs from unstructured text using high-precision regexes."""

from __future__ import annotations
import re
from typing import List, Tuple
from .validator import normalize_isbn, validate_isbn10, validate_isbn13, clean_isbn


# Regex patterns
# 1. Labeled ISBNs: "ISBN-13: 978-0-13-235088-4", "ISBN: 0-201-63361-2", "eISBN: 9780132350884"
LABELED_ISBN_RE = re.compile(
    r"(?P<label>(?:e-?|print\s+)?isbn(?:-?(?:10|13))?)\s*[:#\s]\s*"
    r"(?P<candidate>(?:97[89][\s\-]*)?(?:\d[\s\-]*){9}[\dX])\b",
    re.IGNORECASE,
)

# 2. Raw unlabelled ISBN-13 (starts with 978 or 979, with hyphens or spaces or raw digits)
RAW_ISBN13_RE = re.compile(
    r"\b(?P<candidate>97[89][\s\-]*(?:\d[\s\-]*){9}\d)\b"
)

# 3. Raw unlabelled ISBN-10 (10 digits/X with hyphens e.g. 0-201-63361-2)
RAW_ISBN10_RE = re.compile(
    r"\b(?P<candidate>\d[\s\-]+(?:\d[\s\-]*){8}[\dX])\b",
    re.IGNORECASE,
)


def extract_isbns_from_text(text: str) -> List[Tuple[str, str]]:
    """
    Extract all valid ISBNs from text.
    Returns list of tuples: (isbn13, label_or_type), ranked with highest confidence first.
    """
    if not text:
        return []

    found: List[Tuple[str, str, int]] = []  # (isbn13, label, priority_score)
    seen_isbns = set()

    # Pass 1: Labeled patterns
    for match in LABELED_ISBN_RE.finditer(text):
        label = match.group("label").strip().lower()
        cand = match.group("candidate")
        isbn13, _ = normalize_isbn(cand)
        if isbn13 and isbn13 not in seen_isbns:
            # Score: ebook ISBNs scored highest for digital files, then isbn13, then others
            score = 100
            if "e" in label:
                score += 20
            elif "13" in label:
                score += 10
            found.append((isbn13, label, score))
            seen_isbns.add(isbn13)

    # Pass 2: Standalone raw ISBN-13 patterns
    for match in RAW_ISBN13_RE.finditer(text):
        cand = match.group("candidate")
        isbn13, _ = normalize_isbn(cand)
        if isbn13 and isbn13 not in seen_isbns:
            found.append((isbn13, "raw-isbn13", 50))
            seen_isbns.add(isbn13)

    # Pass 3: Standalone raw ISBN-10 patterns (must have at least one hyphen/space to avoid random 10-digit numbers)
    for match in RAW_ISBN10_RE.finditer(text):
        cand = match.group("candidate")
        if "-" in cand or " " in cand:
            isbn13, _ = normalize_isbn(cand)
            if isbn13 and isbn13 not in seen_isbns:
                found.append((isbn13, "raw-isbn10", 30))
                seen_isbns.add(isbn13)

    # Sort by priority score descending
    found.sort(key=lambda item: item[2], reverse=True)
    return [(isbn13, label) for isbn13, label, _ in found]
