"""Common data models for book metadata."""

from __future__ import annotations
from dataclasses import dataclass, field
import re
from typing import List, Optional

_TRUNC_CONNECTOR_WORDS = {
    "to", "of", "in", "and", "or", "the", "a", "an", "by", "with", "for", "from",
    "that", "which", "who", "whom", "whose", "his", "her", "their", "its", "our",
    "your", "my", "as", "at", "on", "into", "onto", "upon", "but", "when", "while",
    "where", "if", "is", "was", "are", "were", "be", "been", "has", "have", "had",
    "will", "would", "can", "could", "should", "must", "not", "about", "between",
    "through", "during", "before", "after", "above", "below", "under", "over",
}


_EN_STOPWORDS = {
    "the", "and", "of", "to", "in", "is", "that", "for", "with", "his", "her",
    "from", "as", "by", "on", "are", "was", "but", "who", "have", "has", "into",
    "their", "this", "when", "will", "can", "not", "one", "all", "about", "what",
}

_NON_EN_STOPWORDS = {
    # German
    "der", "die", "das", "und", "nicht", "sich", "mit", "dem", "den", "ein", "eine",
    "einer", "einem", "einen", "auf", "für", "auch", "wird", "aus", "nach", "über",
    "noch", "durch", "gegen", "seine", "seinem", "seinen", "oder", "aber", "wenn",
    "nur", "dass", "daß", "erneut", "diesmal",
    # French
    "les", "des", "est", "une", "dans", "pour", "pas", "sur", "plus", "par", "avec",
    "tout", "mais", "ses", "aux", "cette", "comme", "sont", "leur", "sans", "entre",
    # Spanish
    "los", "las", "del", "una", "por", "con", "para", "como", "más", "pero", "sus",
    "esta", "este", "cuando", "todo", "también", "fue", "sobre", "años",
    # Italian
    "della", "delle", "degli", "nel", "nella", "che", "sono", "più", "suo", "sua",
    "anche", "dalla", "questo", "questa",
    # Portuguese
    "dos", "das", "uma", "não", "com", "mais", "mas", "foi", "pelo", "pela",
    "muito", "também",
    # Dutch
    "het", "een", "van", "dat", "niet", "voor", "met", "zijn", "naar", "ook",
    "maar", "door", "wordt", "deze", "haar", "hun",
}


def is_non_english_text(text: Optional[str], min_words: int = 8) -> bool:
    """
    Return True if `text` is clearly written in a non-English language (e.g. German, French,
    Spanish, Italian, Portuguese, Dutch, Cyrillic, CJK, Arabic) rather than English.
    """
    if not text:
        return False
    s = str(text).strip()
    if not s:
        return False
    letters = [ch for ch in s if ch.isalpha()]
    if len(letters) < 12:
        return False
    non_latin = sum(
        1 for ch in letters
        if ("\u0400" <= ch <= "\u052f")  # Cyrillic
        or ("\u0370" <= ch <= "\u03ff")  # Greek
        or ("\u0590" <= ch <= "\u06ff")  # Hebrew / Arabic
        or ("\u3040" <= ch <= "\u9fff")  # Kana / CJK
        or ("\uac00" <= ch <= "\ud7af")  # Hangul
    )
    if non_latin / len(letters) >= 0.18:
        return True

    words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿß]+", s.lower())
    if len(words) < min_words:
        return False
    en_hits = sum(1 for w in words if w in _EN_STOPWORDS)
    non_en_hits = sum(1 for w in words if w in _NON_EN_STOPWORDS)
    if non_en_hits >= 3 and non_en_hits > en_hits * 2:
        return True
    if en_hits == 0 and non_en_hits >= 2:
        return True
    return False


def is_non_english_for_book(description: Optional[str], title: Optional[str] = None) -> bool:
    """
    Return True when `description` is non-English while `title` is NOT itself a non-English title
    (preventing German/French/Spanish shared work blurbs from polluting English editions).
    """
    if not description or not is_non_english_text(description, min_words=8):
        return False
    if title and is_non_english_text(title, min_words=3):
        return False
    return True


def is_truncated_description(text: Optional[str]) -> bool:
    """
    Return True if `text` is a cut-off/truncated book description ending in `…` or `...`
    (such as Goodreads `auto_complete` snippets ending in ` …`), while returning False for
    full multi-sentence publisher blurbs that end with a deliberate dramatic ellipsis.
    """
    if not text:
        return False
    s = str(text).strip()
    if not s:
        return False
    m = re.search(r"(\.\.\.|\.\s+\.\s+\.|…)\s*$", s)
    if not m:
        return False
    prefix = s[: m.start()]
    if not prefix.strip():
        return True
    # Goodreads & catalog snippets almost always have a space or punctuation before the trailing ellipsis (e.g. "body to …")
    if prefix[-1] in (" ", "\t", "\n", ",", ":", ";", "-", "–", "—", "("):
        return True
    if s.endswith("…") and len(s) < 420:
        return True
    if len(s) < 280:
        return True
    last_word_m = re.search(r"([A-Za-z']+)\s*$", prefix)
    if last_word_m and last_word_m.group(1).lower() in _TRUNC_CONNECTOR_WORDS:
        return True
    # Single unfinished sentence with no prior sentence terminator
    if not re.search(r"[.!?]\s+[A-Z\"']", prefix):
        return True
    return False


@dataclass
class BookMetadata:
    """Standardized metadata representation for a book."""
    title: Optional[str] = None
    authors: List[str] = field(default_factory=list)
    description: Optional[str] = None
    subjects: List[str] = field(default_factory=list)  # tags, categories, genres
    publisher: Optional[str] = None
    published_date: Optional[str] = None
    isbn13: Optional[str] = None
    isbn10: Optional[str] = None
    asin: Optional[str] = None
    language: Optional[str] = None
    cover_url: Optional[str] = None
    cover_bytes: Optional[bytes] = None
    source: Optional[str] = None
    ai_used: bool = False

    def merge_with(self, other: "BookMetadata", combine_lists: bool = False) -> "BookMetadata":
        """
        Merge this metadata with another, filling in missing fields.
        Self takes precedence unless a field is empty.
        If combine_lists is True, authors and subjects from both are merged and deduplicated.
        If combine_lists is False, self's lists are preferred if non-empty.
        """
        title = self.title or other.title
        publisher = self.publisher or other.publisher
        published_date = self.published_date or other.published_date
        ref_title = title or ""
        self_desc_bad_lang = is_non_english_for_book(self.description, ref_title)
        other_desc_bad_lang = is_non_english_for_book(other.description, ref_title)
        if self_desc_bad_lang and other.description and not other_desc_bad_lang:
            description = other.description
        elif other_desc_bad_lang and self.description and not self_desc_bad_lang:
            description = self.description
        elif (
            self.description
            and is_truncated_description(self.description)
            and other.description
            and not other_desc_bad_lang
            and (
                not is_truncated_description(other.description)
                or len(other.description.strip()) > len(self.description.strip()) + 40
            )
        ):
            description = other.description
        else:
            description = (None if self_desc_bad_lang else self.description) or (None if other_desc_bad_lang else other.description)
        isbn13 = self.isbn13 or other.isbn13
        isbn10 = self.isbn10 or other.isbn10
        asin = self.asin or other.asin
        language = self.language or other.language
        source = f"{self.source}+{other.source}" if (self.source and other.source) else (self.source or other.source)
        ai_used = self.ai_used or other.ai_used

        self_authors = [a for a in self.authors if a and a.strip().lower() not in ("unknown", "unknown author")]
        other_authors = [a for a in other.authors if a and a.strip().lower() not in ("unknown", "unknown author")]

        if combine_lists:
            authors: List[str] = []
            for a in self_authors + other_authors:
                cleaned = a.strip()
                if cleaned and cleaned not in authors:
                    authors.append(cleaned)

            subjects: List[str] = []
            for s in self.subjects + other.subjects:
                cleaned = s.strip()
                if cleaned and cleaned not in subjects:
                    subjects.append(cleaned)
        else:
            authors = list(self_authors) if self_authors else list(other_authors)
            subjects = list(self.subjects) if self.subjects else list(other.subjects)

        return BookMetadata(
            title=title,
            authors=authors,
            description=description,
            subjects=subjects,
            publisher=publisher,
            published_date=published_date,
            isbn13=isbn13,
            isbn10=isbn10,
            asin=asin,
            language=language,
            cover_url=self.cover_url or other.cover_url,
            cover_bytes=self.cover_bytes or other.cover_bytes,
            source=source,
            ai_used=ai_used,
        )

    def merge(self, other: "BookMetadata", combine_lists: bool = False) -> "BookMetadata":
        """Alias for merge_with()."""
        return self.merge_with(other, combine_lists=combine_lists)

    def is_empty(self) -> bool:
        return not any([
            self.title,
            self.authors,
            self.description,
            self.subjects,
            self.isbn13,
            self.isbn10,
            self.asin,
        ])

    def to_dict(self) -> dict:
        """Serialize metadata fields (excluding raw cover_bytes) for caching."""
        return {
            "title": self.title,
            "authors": list(self.authors),
            "description": self.description,
            "subjects": list(self.subjects),
            "publisher": self.publisher,
            "published_date": self.published_date,
            "isbn13": self.isbn13,
            "isbn10": self.isbn10,
            "asin": self.asin,
            "language": self.language,
            "cover_url": self.cover_url,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BookMetadata":
        """Deserialize metadata from cached dictionary."""
        return cls(
            title=data.get("title"),
            authors=list(data.get("authors") or []),
            description=data.get("description"),
            subjects=list(data.get("subjects") or []),
            publisher=data.get("publisher"),
            published_date=data.get("published_date"),
            isbn13=data.get("isbn13"),
            isbn10=data.get("isbn10"),
            asin=data.get("asin"),
            language=data.get("language"),
            cover_url=data.get("cover_url"),
            source=data.get("source") or "local-cache",
            ai_used=False,
        )

