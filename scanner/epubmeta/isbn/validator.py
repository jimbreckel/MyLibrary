"""ISBN validation, normalization, and conversion utilities."""

from __future__ import annotations
import re
from typing import Optional, Tuple


def clean_isbn(raw: str) -> str:
    """Strip common prefixes, hyphens, spaces, and punctuation."""
    if not raw:
        return ""
    # Strip any leading 'urn:isbn:' or 'isbn:' prefix
    cleaned = re.sub(r"^(?:urn:)?isbn(?:-?1[03])?:?\s*", "", raw, flags=re.IGNORECASE)
    # Remove hyphens, spaces, dots, slashes
    cleaned = re.sub(r"[\s\-\./\\]+", "", cleaned)
    # Remove trailing non-alphanumeric (like semicolons or closing parens)
    cleaned = re.sub(r"[^\dX]+$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.upper()


def validate_isbn10(isbn: str) -> bool:
    """Validate an ISBN-10 checksum."""
    cleaned = clean_isbn(isbn)
    if len(cleaned) != 10:
        return False
    if not re.match(r"^\d{9}[\dX]$", cleaned):
        return False

    total = 0
    for i, char in enumerate(cleaned):
        val = 10 if char == "X" else int(char)
        total += (10 - i) * val

    return total % 11 == 0


def validate_isbn13(isbn: str) -> bool:
    """Validate an ISBN-13 checksum."""
    cleaned = clean_isbn(isbn)
    if len(cleaned) != 13:
        return False
    if not cleaned.isdigit():
        return False
    if not (cleaned.startswith("978") or cleaned.startswith("979")):
        return False

    total = 0
    for i, char in enumerate(cleaned):
        weight = 1 if i % 2 == 0 else 3
        total += int(char) * weight

    return total % 10 == 0


def calculate_isbn13_check_digit(prefix12: str) -> str:
    """Compute the 13th check digit for a 12-digit ISBN prefix."""
    if len(prefix12) != 12 or not prefix12.isdigit():
        raise ValueError("Prefix must be exactly 12 digits")
    total = sum(int(char) * (1 if i % 2 == 0 else 3) for i, char in enumerate(prefix12))
    remainder = total % 10
    check = (10 - remainder) % 10
    return str(check)


def isbn10_to_isbn13(isbn10: str) -> Optional[str]:
    """Convert a valid ISBN-10 to an ISBN-13 (prefixed with 978)."""
    cleaned = clean_isbn(isbn10)
    if not validate_isbn10(cleaned):
        return None
    prefix12 = "978" + cleaned[:9]
    check = calculate_isbn13_check_digit(prefix12)
    return prefix12 + check


def normalize_isbn(raw: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Given an arbitrary ISBN string, validate and return (isbn13, isbn10).
    Returns (None, None) if invalid.
    """
    cleaned = clean_isbn(raw)
    if validate_isbn13(cleaned):
        # We have a valid ISBN-13
        # If it starts with 978, it might have an ISBN-10 equivalent
        isbn10 = None
        if cleaned.startswith("978"):
            core = cleaned[3:12]
            total = sum((10 - i) * int(c) for i, c in enumerate(core))
            rem = (11 - (total % 11)) % 11
            check10 = "X" if rem == 10 else str(rem)
            cand10 = core + check10
            if validate_isbn10(cand10):
                isbn10 = cand10
        return cleaned, isbn10

    if validate_isbn10(cleaned):
        isbn13 = isbn10_to_isbn13(cleaned)
        return isbn13, cleaned

    return None, None
