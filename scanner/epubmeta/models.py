"""Common data models for book metadata."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional


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
        description = self.description or other.description
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

