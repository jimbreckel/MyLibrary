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


def _extract_surnames(authors: List[str]) -> List[str]:
    """Extract approximate surnames from author list."""
    surnames = []
    for a in authors:
        cleaned = re.sub(r"[^\w\s]", "", a).strip()
        parts = cleaned.split()
        if parts:
            # Usually last token is surname (e.g. 'John W. Dunne' -> 'dunne')
            surnames.append(parts[-1].lower())
    return surnames


def score_title(candidate_title: Optional[str], clue_title: Optional[str]) -> float:
    """Score similarity between candidate title and known title clue (0.0 - 1.0)."""
    if not candidate_title or not clue_title:
        return 0.0

    c_norm = _clean_str(candidate_title)
    k_norm = _clean_str(clue_title)

    if not c_norm or not k_norm:
        return 0.0

    if c_norm == k_norm:
        return 1.0

    # Subtitle or prefix match (e.g., 'Supremacy' vs 'Supremacy How Rule by the Court...')
    if c_norm.startswith(k_norm) or k_norm.startswith(c_norm):
        shorter = min(len(c_norm), len(k_norm))
        longer = max(len(c_norm), len(k_norm))
        # If the prefix is substantial
        if shorter >= 6 or shorter / longer > 0.4:
            return 0.90

    # Word set overlap
    c_words = _get_words(candidate_title)
    k_words = _get_words(clue_title)
    if c_words and k_words:
        intersection = c_words & k_words
        union = c_words | k_words
        jaccard = len(intersection) / len(union) if union else 0.0
        # Recall relative to the shorter title
        recall = len(intersection) / min(len(c_words), len(k_words)) if min(len(c_words), len(k_words)) > 0 else 0.0
        word_score = max(jaccard, recall * 0.85)
    else:
        word_score = 0.0

    # Sequence matcher - only meaningful if there is word overlap or high character similarity
    seq_score = difflib.SequenceMatcher(None, c_norm, k_norm).ratio()
    if not (c_words & k_words) and seq_score < 0.70:
        seq_score = 0.0

    return max(word_score, seq_score)


def score_authors(candidate_authors: List[str], clue_authors: List[str]) -> float:
    """Score similarity between candidate authors and known author clues (0.0 - 1.0)."""
    if not candidate_authors or not clue_authors:
        return 0.0

    c_surnames = set(_extract_surnames(candidate_authors))
    k_surnames = set(_extract_surnames(clue_authors))

    if not c_surnames or not k_surnames:
        return 0.0

    overlap = c_surnames & k_surnames
    if overlap:
        # If all known surnames found in candidate
        if overlap == k_surnames:
            return 1.0
        # Partial author match (e.g. 1 out of 2 authors)
        return len(overlap) / max(len(k_surnames), len(c_surnames))

    # If no surnames matched, only count if full names are close enough to be a typo/transliteration
    c_str = _clean_str(" ".join(candidate_authors))
    k_str = _clean_str(" ".join(clue_authors))
    sim = difflib.SequenceMatcher(None, c_str, k_str).ratio()
    return sim if sim >= 0.75 else 0.0


def calculate_match_confidence(
    candidate: BookMetadata,
    filename_info: Optional[Dict[str, Any]] = None,
    existing_meta: Optional[BookMetadata] = None,
    isbn_matched_in_front_matter: bool = False,
    min_confidence: float = 0.50,
    content_meta: Optional[BookMetadata] = None,
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

    composite = min(1.0, max(0.0, composite))
    is_confident = composite >= min_confidence

    return MatchResult(
        score=composite,
        is_confident=is_confident,
        title_score=t_score,
        author_score=a_score,
        details=details,
    )
