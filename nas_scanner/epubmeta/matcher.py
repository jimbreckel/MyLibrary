"""Match confidence scoring to verify candidate metadata against book clues."""

from __future__ import annotations
import difflib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from .models import BookMetadata


@dataclass
class MatchResult:
    score: float  # 0.0 to 1.0
    is_confident: bool
    title_score: float
    author_score: float
    details: List[str] = field(default_factory=list)

    @property
    def percentage(self) -> int:
        return int(round(self.score * 100))

    @property
    def confidence(self) -> float:
        return self.score


def _clean_str(text: Optional[str]) -> str:
    """Normalize text by lowercasing and stripping punctuation/whitespace."""
    if not text:
        return ""
    # Remove punctuation
    cleaned = re.sub(r"[^\w\s]", " ", text.lower())
    return " ".join(cleaned.split())


def _get_words(text: Optional[str]) -> Set[str]:
    """Get non-trivial word tokens from text."""
    stop_words = {"the", "a", "an", "and", "or", "of", "to", "in", "for", "on", "with", "by", "at", "from"}
    cleaned = _clean_str(text)
    return {w for w in cleaned.split() if len(w) > 1 and w not in stop_words}


_SURNAME_IGNORE_TOKENS = {
    "jr", "sr", "ii", "iii", "iv", "v", "phd", "md", "esq", "cpa",
    "ed", "eds", "editor", "editors", "auth", "author", "comp", "tr", "trans",
}


def _extract_surnames(authors: List[str]) -> List[str]:
    """Extract approximate surnames from author list (handling '(ed)', 'Last, First', and '&'/'and')."""
    from .authors import split_and_normalize_authors, strip_author_role_tags

    surnames: List[str] = []
    expanded: List[str] = []
    for raw_a in authors:
        if not raw_a:
            continue
        norm_pairs = split_and_normalize_authors(strip_author_role_tags(raw_a))
        if norm_pairs:
            expanded.extend(disp for disp, _ in norm_pairs if disp)
        else:
            expanded.append(raw_a)

    for a in expanded:
        no_role = strip_author_role_tags(a)
        cleaned = re.sub(r"[^\w\s]", " ", no_role).strip()
        parts = [p for p in cleaned.split() if p.lower() not in _SURNAME_IGNORE_TOKENS]
        if parts:
            surnames.append(parts[-1].lower())
    return surnames


def _strip_trailing_year(text: Optional[str]) -> str:
    """Strip trailing parenthetical/bracketed publication years like '(1921)' or Goodreads series tags like '(Thieves' World, #3)'."""
    if not text:
        return ""
    s = str(text).strip()
    s = re.sub(
        r"(?<=\S)\s*[\(\[]\s*(?:[^)\]\d]{0,40}?\b)?(?:18\d{2}|19\d{2}|20\d{2})(?:-\d{1,2}(?:-\d{1,2})?)?\s*[\)\]]\s*$",
        "",
        s,
        flags=re.IGNORECASE,
    ).strip()
    s = re.sub(
        r"(?<=\S)\s*\([^()]{1,60},\s*#\d+(?:\.\d+)?\)\s*$",
        "",
        s,
    ).strip()
    return s


def score_title(candidate_title: Optional[str], clue_title: Optional[str]) -> float:
    """Score similarity between candidate title and known title clue (0.0 - 1.0)."""
    if not candidate_title or not clue_title:
        return 0.0

    cand_clean_yr = _strip_trailing_year(candidate_title) or candidate_title
    clue_clean_yr = _strip_trailing_year(clue_title) or clue_title

    c_norm = _clean_str(cand_clean_yr)
    k_norm = _clean_str(clue_clean_yr)

    if not c_norm or not k_norm:
        return 0.0

    if c_norm == k_norm:
        return 1.0

    # Leading article match (e.g., 'The Way of the Lawless' vs 'Way Of the Lawless')
    c_no_art = re.sub(r"^(?:the|a|an)\s+", "", c_norm)
    k_no_art = re.sub(r"^(?:the|a|an)\s+", "", k_norm)
    if c_no_art and c_no_art == k_no_art:
        return 0.98

    # Subtitle or prefix match (e.g., 'Supremacy' vs 'Supremacy How Rule by the Court...')
    if c_norm.startswith(k_norm) or k_norm.startswith(c_norm) or (c_no_art and k_no_art and (c_no_art.startswith(k_no_art) or k_no_art.startswith(c_no_art))):
        shorter = min(len(c_no_art or c_norm), len(k_no_art or k_norm))
        longer = max(len(c_no_art or c_norm), len(k_no_art or k_norm))
        # If the prefix is substantial
        if shorter >= 6 or shorter / longer > 0.4:
            return 0.90

    # Word set overlap
    c_words = _get_words(cand_clean_yr)
    k_words = _get_words(clue_clean_yr)
    if c_words and k_words:
        intersection = c_words & k_words
        union = c_words | k_words
        jaccard = len(intersection) / len(union) if union else 0.0
        # Recall relative to the shorter title (avoiding false high recall when a multi-book omnibus/boxset contains a short single-book title)
        is_unwanted_omnibus = bool(
            len(c_words) >= max(9, len(k_words) * 3)
            and re.search(r"\b(?:books?\s+\d+\s*[\+&\-]|omnibus|box\s*set|complete\s+series)\b|\b1\.\s+\S+.*\b[23]\.\s+\S+", cand_clean_yr, re.I)
            and not re.search(r"\b(?:books?\s+\d+\s*[\+&\-]|omnibus|box\s*set|complete\s+series)\b", clue_clean_yr, re.I)
        )
        recall = (
            0.0
            if is_unwanted_omnibus
            else (len(intersection) / min(len(c_words), len(k_words)) if min(len(c_words), len(k_words)) > 0 else 0.0)
        )
        word_score = max(jaccard, recall * 0.85)
    else:
        word_score = 0.0

    # Sequence matcher - only meaningful if there is word overlap or high character similarity
    seq_score = difflib.SequenceMatcher(None, c_norm, k_norm).ratio()
    if not (c_words & k_words) and seq_score < 0.70:
        seq_score = 0.0

    raw_score = max(word_score, seq_score)

    # Penalize conflicting numbers (e.g., "50 Things..." vs "100+ Things...", or "Vol 1" vs "Vol 2"), normalizing leading zeros ('04' == '4')
    c_nums = {str(int(x)) for x in re.findall(r"\b\d+\b", c_norm)}
    k_nums = {str(int(x)) for x in re.findall(r"\b\d+\b", k_norm)}
    if c_nums and k_nums and not (c_nums & k_nums):
        raw_score *= 0.45

    return raw_score


def score_authors(candidate_authors: List[str], clue_authors: List[str]) -> float:
    """Score similarity between candidate authors and known author clues (0.0 - 1.0)."""
    if not candidate_authors or not clue_authors:
        return 0.0

    c_surnames = set(_extract_surnames(candidate_authors))
    k_surnames = set(_extract_surnames(clue_authors))

    if not c_surnames or not k_surnames:
        return 0.0

    overlap = c_surnames & k_surnames
    c_str = _clean_str(" ".join(candidate_authors))
    k_str = _clean_str(" ".join(clue_authors))
    sim = difflib.SequenceMatcher(None, c_str, k_str).ratio()
    if overlap:
        # If all known surnames found in candidate
        if overlap == k_surnames:
            return 1.0
        # Partial author match (e.g. 1 out of 2 authors, or minor spelling variant on co-author)
        partial = len(overlap) / max(len(k_surnames), len(c_surnames))
        return max(partial, sim if sim >= 0.80 else 0.0)

    # If no surnames matched, only count if full names are close enough to be a typo/transliteration
    return sim if sim >= 0.75 else 0.0


def calculate_match_confidence(
    candidate: BookMetadata,
    filename_info: Optional[Dict[str, Any]] = None,
    existing_meta: Optional[BookMetadata] = None,
    isbn_matched_in_front_matter: bool = False,
    min_confidence: float = 0.50,
    content_meta: Optional[BookMetadata] = None,
    allow_swapped_clues: bool = True,
) -> MatchResult:
    """
    Calculate confidence score (0.0 - 1.0) verifying if candidate metadata matches
    the book's filename clues, existing embedded metadata, or AI-extracted front-matter metadata.
    """
    details: List[str] = []

    # 1. Gather all potential title clues
    generic_titles = {"book", "document", "ebook", "untitled", "sample", "unknown", "upload"}
    title_clues: List[str] = []
    if filename_info and filename_info.get("title"):
        ft = filename_info["title"].strip()
        if ft.lower() not in generic_titles:
            title_clues.append(ft)
    # Also support Calibre's "Title - Author" filename convention (where parser puts Title in authors)
    if filename_info and filename_info.get("authors") and filename_info.get("raw_stem") and " - " in filename_info["raw_stem"]:
        left_part = filename_info["raw_stem"].split(" - ", 1)[0].strip()
        if left_part and left_part.lower() not in generic_titles and left_part not in title_clues:
            title_clues.append(left_part)
    if existing_meta and existing_meta.title:
        et = existing_meta.title.strip()
        if et.lower() not in generic_titles and et not in title_clues:
            title_clues.append(et)
        if existing_meta.authors:
            from .authors import strip_series_and_redundant_author_from_title
            et_stripped = strip_series_and_redundant_author_from_title(et, existing_meta.authors)
            if et_stripped and et_stripped.lower() not in generic_titles and et_stripped not in title_clues:
                title_clues.append(et_stripped)
    if content_meta and content_meta.title:
        ct = content_meta.title.strip()
        if ct.lower() not in generic_titles and ct not in title_clues:
            title_clues.append(ct)

    # 2. Gather all potential author clues
    author_clues_list: List[List[str]] = []
    if filename_info and filename_info.get("authors"):
        author_clues_list.append(filename_info["authors"])
    if filename_info and filename_info.get("title") and filename_info.get("raw_stem") and " - " in filename_info["raw_stem"]:
        author_clues_list.append([filename_info["title"].strip()])
    if existing_meta and existing_meta.authors:
        author_clues_list.append(existing_meta.authors)
    if content_meta and content_meta.authors:
        author_clues_list.append(content_meta.authors)

    # Compute best title score across all clues
    t_score = 0.0
    best_t_clue = None
    if candidate.title and title_clues:
        for tc in title_clues:
            s = score_title(candidate.title, tc)
            if s > t_score:
                t_score = s
                best_t_clue = tc
        details.append(f"Title match: {int(t_score * 100)}% ('{candidate.title}' vs clue '{best_t_clue}')")

    # Compute best author score across all clues
    a_score = 0.0
    best_a_clue = None
    if candidate.authors and author_clues_list:
        for ac in author_clues_list:
            s = score_authors(candidate.authors, ac)
            if s > a_score:
                a_score = s
                best_a_clue = ac
        details.append(f"Author match: {int(a_score * 100)}% ({candidate.authors} vs clue {best_a_clue})")

    # Combine scores
    has_title_clue = bool(title_clues)
    has_author_clue = bool(author_clues_list)

    if has_title_clue and has_author_clue:
        composite = (t_score * 0.65) + (a_score * 0.35)
    elif has_title_clue:
        composite = t_score
    elif has_author_clue:
        composite = a_score
    else:
        # No clues available to check against
        composite = 0.85 if isbn_matched_in_front_matter else 0.50
        details.append("No local title/author clues in filename or embedded metadata to compare against.")

    # ISBN boost: if the ISBN was extracted directly from the front matter text
    if isbn_matched_in_front_matter:
        # Front-matter ISBN is verified text from the book; only boost when title also agrees (>= 0.55)
        # because 'About the Author' / 'Also by' pages often cite ISBNs of OTHER books by the same author.
        if not has_title_clue or t_score >= 0.55:
            composite = max(composite, 0.90)
            details.append("Boosted: Verified ISBN extracted directly from document front matter.")

    # Switched Title/Author check: if normal clues failed, check if the file's Title and Author were inverted
    # (requires BOTH candidate title matching original author clue >= 0.70 AND candidate author matching original title clue >= 0.65).
    if allow_swapped_clues and composite < min_confidence and candidate.title and candidate.authors:
        from .authors import clean_swapped_author_candidates, clean_swapped_title_candidates

        swapped_title_clues: List[str] = []
        swapped_author_clues_list: List[List[str]] = []

        if existing_meta:
            if existing_meta.authors:
                for st in clean_swapped_title_candidates(existing_meta.authors):
                    if st.lower() not in generic_titles and st not in swapped_title_clues:
                        swapped_title_clues.append(st)
            if existing_meta.title:
                _, sa_list = clean_swapped_author_candidates(existing_meta.title)
                if sa_list:
                    swapped_author_clues_list.append(sa_list)

        if filename_info:
            if filename_info.get("authors"):
                for st in clean_swapped_title_candidates(filename_info["authors"]):
                    if st.lower() not in generic_titles and st not in swapped_title_clues:
                        swapped_title_clues.append(st)
            if filename_info.get("title"):
                _, sa_list = clean_swapped_author_candidates(filename_info["title"])
                if sa_list:
                    swapped_author_clues_list.append(sa_list)

        sw_t_score = 0.0
        best_sw_t = None
        for stc in swapped_title_clues:
            s = score_title(candidate.title, stc)
            if s > sw_t_score:
                sw_t_score = s
                best_sw_t = stc

        sw_a_score = 0.0
        best_sw_a = None
        for sac in swapped_author_clues_list:
            s = score_authors(candidate.authors, sac)
            if s > sw_a_score:
                sw_a_score = s
                best_sw_a = sac

        if sw_t_score >= 0.70 and sw_a_score >= 0.65:
            sw_comp = (sw_t_score * 0.65) + (sw_a_score * 0.35)
            if sw_comp > composite:
                composite = sw_comp
                t_score = sw_t_score
                a_score = sw_a_score
                details.append(
                    f"Switched Title/Author match: title={int(sw_t_score * 100)}% ('{candidate.title}' vs '{best_sw_t}'), "
                    f"author={int(sw_a_score * 100)}% ({candidate.authors} vs {best_sw_a})"
                )

    composite = min(1.0, max(0.0, composite))
    is_confident = composite >= min_confidence

    return MatchResult(
        score=composite,
        is_confident=is_confident,
        title_score=t_score,
        author_score=a_score,
        details=details,
    )
