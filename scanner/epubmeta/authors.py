"""Author name normalization, splitting, and sort-order (file-as) formatting."""

from __future__ import annotations
import re
from typing import List, Tuple, Union

COMMON_SUFFIXES = {
    "jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v",
    "ph.d.", "phd", "m.d.", "md", "esq", "esq.", "cpa",
}


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

    cleaned = re.sub(r"\s+", " ", raw).strip()
    cleaned = cleaned.rstrip(";")

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
    - Alternating 'Last, First, Last, First': "Renan, Daphna, Bowie, Nikolas"
    """
    if not authors_input:
        return []

    # If it's already a list of strings
    raw_list: List[str] = []
    if isinstance(authors_input, list):
        for item in authors_input:
            if item and item.strip():
                # Check if item itself contains semicolons or ' and '
                if ";" in item:
                    raw_list.extend([x.strip() for x in item.split(";") if x.strip()])
                elif " and " in item.lower():
                    raw_list.extend([x.strip() for x in re.split(r"\s+and\s+", item, flags=re.IGNORECASE) if x.strip()])
                elif " & " in item:
                    raw_list.extend([x.strip() for x in item.split("&") if x.strip()])
                else:
                    raw_list.append(item.strip())
    else:
        s = authors_input.strip()
        if ";" in s:
            raw_list = [x.strip() for x in s.split(";") if x.strip()]
        elif " and " in s.lower():
            raw_list = [x.strip() for x in re.split(r"\s+and\s+", s, flags=re.IGNORECASE) if x.strip()]
        elif " & " in s:
            raw_list = [x.strip() for x in s.split("&") if x.strip()]
        elif "," in s:
            comma_parts = [p.strip() for p in s.split(",") if p.strip()]
            # If 4 or more parts, e.g. ["Renan", "Daphna", "Bowie", "Nikolas"]
            # Even number of parts often indicates pairs of (Lastname, Firstname)
            if len(comma_parts) >= 4 and len(comma_parts) % 2 == 0:
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
