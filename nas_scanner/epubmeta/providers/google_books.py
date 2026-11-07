"""Google Books metadata provider."""

from __future__ import annotations
import html
import logging
import os
import re
from typing import Any, Dict, List, Optional
import requests

from ..models import BookMetadata
from ..isbn.validator import clean_isbn, normalize_isbn
from .base import BaseProvider

logger = logging.getLogger(__name__)

USER_AGENT = "epubmeta/1.0"


def _clean_description(raw: Optional[str]) -> Optional[str]:
    """Strip HTML tags and unescape entities from book descriptions."""
    if not raw:
        return None
    unescaped = html.unescape(raw)
    clean = re.sub(r"<[^>]+>", " ", unescaped)
    return re.sub(r"\s+", " ", clean).strip()


class GoogleBooksProvider(BaseProvider):
    """Fetches book metadata from Google Books Volumes API."""

    @property
    def name(self) -> str:
        return "Google Books"

    def __init__(self, api_key: Optional[str] = None, timeout: int = 15, raise_on_quota: bool = False):
        self.api_key = api_key or os.getenv("GOOGLE_BOOKS_API_KEY")
        self.timeout = timeout
        self.raise_on_quota = raise_on_quota
        self.quota_exhausted = False
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def _parse_volume(self, volume: Dict[str, Any]) -> BookMetadata:
        info = volume.get("volumeInfo", {})
        title = info.get("title")
        subtitle = info.get("subtitle")
        if title and subtitle and subtitle.lower() not in title.lower():
            title = f"{title}: {subtitle}"

        authors = [a.strip() for a in info.get("authors", []) if a.strip()]
        description = _clean_description(info.get("description"))
        subjects = [c.strip() for c in info.get("categories", []) if c.strip()]
        publisher = info.get("publisher")
        published_date = info.get("publishedDate")
        language = info.get("language")

        isbn13 = None
        isbn10 = None
        for ident in info.get("industryIdentifiers", []):
            itype = ident.get("type", "").upper()
            val = ident.get("identifier", "")
            if "13" in itype:
                i13, i10 = normalize_isbn(val)
                if i13:
                    isbn13 = i13
            elif "10" in itype:
                i13, i10 = normalize_isbn(val)
                if i10:
                    isbn10 = i10

        links = info.get("imageLinks") or {}
        cover_url = (
            links.get("extraLarge")
            or links.get("large")
            or links.get("medium")
            or links.get("thumbnail")
            or links.get("smallThumbnail")
        )
        if cover_url:
            if cover_url.startswith("http://"):
                cover_url = "https://" + cover_url[7:]
            cover_url = re.sub(r"&edge=curl", "", cover_url)

        return BookMetadata(
            title=title,
            authors=authors,
            description=description,
            subjects=subjects,
            publisher=publisher,
            published_date=published_date,
            isbn13=isbn13,
            isbn10=isbn10,
            language=language,
            cover_url=cover_url,
            source="google-books",
        )

    def fetch_by_isbn(self, isbn: str) -> Optional[BookMetadata]:
        """Query Google Books by ISBN."""
        if getattr(self, "quota_exhausted", False):
            return None

        cleaned = clean_isbn(isbn)
        if not cleaned:
            return None

        url = "https://www.googleapis.com/books/v1/volumes"
        params: Dict[str, str] = {"q": f"isbn:{cleaned}"}
        if self.api_key:
            params["key"] = self.api_key

        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("items", [])
                if items:
                    merged: Optional[BookMetadata] = None
                    for item_vol in items[:3]:
                        cand = self._parse_volume(item_vol)
                        if not cand or cand.is_empty():
                            continue
                        if merged is None:
                            merged = cand
                        else:
                            merged = merged.merge_with(cand, combine_lists=True)
                    return merged
            elif resp.status_code == 429:
                if self.raise_on_quota:
                    from ..exceptions import RateLimitExceededError
                    raise RateLimitExceededError("Google Books", "Google Books API quota exceeded (429)")
                self.quota_exhausted = True
                logger.warning(
                    "Google Books quota exceeded (429). Disabling Google Books for this run. Tip: set GOOGLE_BOOKS_API_KEY for higher quota."
                )
            else:
                logger.debug("Google Books returned status %d", resp.status_code)
        except Exception as e:
            from ..exceptions import RateLimitExceededError
            if isinstance(e, RateLimitExceededError):
                raise
            logger.warning("Google Books API error: %s", e)
        return None

    def fetch_by_query(self, title: str, author: Optional[str] = None) -> Optional[BookMetadata]:
        """Search Google Books by title and author."""
        if getattr(self, "quota_exhausted", False):
            return None

        if not title:
            return None

        q = f'intitle:"{title}"'
        if author:
            q += f' inauthor:"{author}"'

        url = "https://www.googleapis.com/books/v1/volumes"
        params: Dict[str, str] = {"q": q}
        if self.api_key:
            params["key"] = self.api_key

        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
            if resp.status_code == 200:
                from ..matcher import score_title, score_authors
                data = resp.json()
                items = data.get("items", [])
                if items:
                    merged: Optional[BookMetadata] = None
                    for item_vol in items[:5]:
                        cand = self._parse_volume(item_vol)
                        if not cand or cand.is_empty():
                            continue
                        if cand.title and score_title(cand.title, title) < 0.45:
                            continue
                        if author and cand.authors and score_authors(cand.authors, [author]) < 0.40:
                            continue
                        if merged is None:
                            merged = cand
                        else:
                            cand_t = re.sub(r"[^a-z0-9]+", " ", (cand.title or "").lower()).strip()
                            base_t = re.sub(r"[^a-z0-9]+", " ", (merged.title or title).lower()).strip()
                            if base_t and cand_t and (base_t in cand_t or cand_t in base_t):
                                merged = merged.merge_with(cand, combine_lists=True)
                        if (
                            merged
                            and merged.title
                            and merged.authors
                            and merged.description
                            and (merged.isbn13 or merged.isbn10)
                            and merged.publisher
                            and merged.cover_url
                        ):
                            break
                    return merged
            elif resp.status_code == 429:
                if self.raise_on_quota:
                    from ..exceptions import RateLimitExceededError
                    raise RateLimitExceededError("Google Books", "Google Books API quota exceeded (429)")
                self.quota_exhausted = True
                logger.warning("Google Books quota exceeded (429).")
        except Exception as e:
            from ..exceptions import RateLimitExceededError
            if isinstance(e, RateLimitExceededError):
                raise
            logger.warning("Google Books search query error: %s", e)
        return None
