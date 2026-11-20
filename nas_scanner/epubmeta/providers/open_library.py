"""Open Library metadata provider."""

from __future__ import annotations
import logging
from typing import Any, Dict, List, Optional
import requests

from ..models import BookMetadata
from ..isbn.validator import clean_isbn, normalize_isbn
from .base import BaseProvider

logger = logging.getLogger(__name__)

USER_AGENT = "epubmeta/1.0 (https://github.com/notgithub/epubmeta; metadata fetcher)"


class OpenLibraryProvider(BaseProvider):
    """Fetches book metadata from Open Library public REST APIs."""

    @property
    def name(self) -> str:
        return "Open Library"

    def __init__(self, timeout: Any = (3.0, 5.5), raise_on_quota: bool = False):
        self.timeout = timeout
        self.raise_on_quota = raise_on_quota
        self._disabled = False
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def _fetch_work_description(self, work_key: str) -> Optional[str]:
        """Fetch the full description from an Open Library work object."""
        if not work_key or self._disabled:
            return None
        url = f"https://openlibrary.org{work_key}.json"
        try:
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                desc = data.get("description")
                if isinstance(desc, str):
                    return desc.strip()
                elif isinstance(desc, dict) and "value" in desc:
                    return str(desc["value"]).strip()
        except Exception as e:
            self._disabled = True
            logger.debug("Failed to fetch work description from %s: %s", url, e)
        return None

    def fetch_by_isbn(self, isbn: str) -> Optional[BookMetadata]:
        """Query Open Library by ISBN."""
        if self._disabled:
            return None
        cleaned = clean_isbn(isbn)
        if not cleaned:
            return None

        # 1. Query Books API with jscmd=data
        bibkey = f"ISBN:{cleaned}"
        url = f"https://openlibrary.org/api/books?bibkeys={bibkey}&format=json&jscmd=data"

        data: Dict[str, Any] = {}
        try:
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code == 200:
                res = resp.json()
                data = res.get(bibkey, {})
            elif resp.status_code == 429 and self.raise_on_quota:
                from ..exceptions import RateLimitExceededError
                raise RateLimitExceededError("Open Library", "Open Library API rate limit reached (429)")
            elif resp.status_code >= 500 or resp.status_code == 429:
                self._disabled = True
                return None
        except Exception as e:
            from ..exceptions import RateLimitExceededError
            if isinstance(e, RateLimitExceededError):
                raise
            self._disabled = True
            logger.warning("Open Library API request error: %s", e)
            return None

        # 2. Query Search API to get work_key and fallback fields
        work_key = None
        search_doc = None
        try:
            search_url = (
                f"https://openlibrary.org/search.json?isbn={cleaned}"
                "&fields=key,title,subtitle,author_name,first_publish_year,isbn,publisher,subject,cover_i"
            )
            s_resp = self.session.get(search_url, timeout=self.timeout)
            if s_resp.status_code == 200:
                docs = s_resp.json().get("docs", [])
                if docs:
                    search_doc = docs[0]
                    work_key = search_doc.get("key")
        except Exception as e:
            logger.debug("Open Library search API error: %s", e)

        if not data and not search_doc:
            return None

        # Extract Title
        title = data.get("title") or (search_doc.get("title") if search_doc else None)
        subtitle = data.get("subtitle") or (search_doc.get("subtitle") if search_doc else None)
        if title and subtitle and subtitle.lower() not in title.lower():
            title = f"{title}: {subtitle}"

        # Extract Authors
        authors: List[str] = []
        if "authors" in data:
            for a in data["authors"]:
                if isinstance(a, dict) and "name" in a:
                    authors.append(a["name"].strip())
        elif search_doc and "author_name" in search_doc:
            authors = [a.strip() for a in search_doc["author_name"] if a.strip()]

        # Extract Description
        description = None
        if work_key:
            description = self._fetch_work_description(work_key)

        # Extract Subjects / Tags
        subjects: List[str] = []
        if "subjects" in data:
            for s in data["subjects"]:
                if isinstance(s, dict) and "name" in s:
                    subjects.append(s["name"].strip())
                elif isinstance(s, str):
                    subjects.append(s.strip())
        elif search_doc and "subject" in search_doc:
            subjects = [s.strip() for s in search_doc["subject"][:15] if s.strip()]

        # Publisher
        publishers = []
        if "publishers" in data:
            for p in data["publishers"]:
                if isinstance(p, dict) and "name" in p:
                    publishers.append(p["name"].strip())
        elif search_doc and "publisher" in search_doc:
            publishers = [p.strip() for p in search_doc["publisher"] if p.strip()]
        publisher = publishers[0] if publishers else None

        # Publish date
        published_date = data.get("publish_date") or (
            str(search_doc.get("first_publish_year")) if search_doc and search_doc.get("first_publish_year") else None
        )

        # Cover URL
        cover_url = None
        if "cover" in data and isinstance(data["cover"], dict):
            cover_url = data["cover"].get("large") or data["cover"].get("medium")
        elif search_doc and search_doc.get("cover_i"):
            cover_url = f"https://covers.openlibrary.org/b/id/{search_doc['cover_i']}-L.jpg"
        elif cleaned:
            cover_url = f"https://covers.openlibrary.org/b/isbn/{cleaned}-L.jpg"

        # ISBNs
        isbn13, isbn10 = normalize_isbn(cleaned)

        return BookMetadata(
            title=title,
            authors=authors,
            description=description,
            subjects=subjects,
            publisher=publisher,
            published_date=published_date,
            isbn13=isbn13,
            isbn10=isbn10,
            cover_url=cover_url,
            source="open-library",
        )

    def fetch_by_query(self, title: str, author: Optional[str] = None) -> Optional[BookMetadata]:
        """Search Open Library by title and author."""
        if not title or self._disabled:
            return None
        import re as _re
        fields_str = "key,title,subtitle,author_name,first_publish_year,isbn,publisher,subject,cover_i"
        params = {"title": title, "fields": fields_str}
        if author:
            params["author"] = author

        try:
            resp = self.session.get("https://openlibrary.org/search.json", params=params, timeout=self.timeout)
            if resp.status_code >= 500 or resp.status_code == 429:
                self._disabled = True
                return None
            docs = resp.json().get("docs", []) if resp.status_code == 200 else []
            if not docs and not self._disabled and (":" in title or " - " in title or (author and "." in author)):
                short_title = _re.split(r"\s*(?::| - )\s*", title, maxsplit=1)[0].strip() or title
                alt_author = author
                if author and "." in author and len(author.split()) >= 2:
                    alt_author = author.split()[-1].strip()
                alt_params = {"title": short_title, "fields": fields_str}
                if alt_author:
                    alt_params["author"] = alt_author
                if alt_params != params:
                    resp2 = self.session.get("https://openlibrary.org/search.json", params=alt_params, timeout=self.timeout)
                    if resp2.status_code == 200:
                        docs = resp2.json().get("docs", [])
            if docs:
                from ..matcher import score_title, score_authors
                if docs:
                    merged: Optional[BookMetadata] = None
                    for doc in docs[:5]:
                        doc_raw_title = doc.get("title") or ""
                        doc_raw_sub = doc.get("subtitle") or ""
                        full_doc_t = f"{doc_raw_title}: {doc_raw_sub}" if (doc_raw_title and doc_raw_sub) else doc_raw_title
                        if full_doc_t and score_title(full_doc_t, title) < 0.45 and score_title(doc_raw_title, title) < 0.45:
                            continue
                        doc_authors = doc.get("author_name", [])
                        if author and doc_authors and score_authors(doc_authors, [author]) < 0.40:
                            continue
                        cand: Optional[BookMetadata] = None
                        isbns = doc.get("isbn", [])
                        for raw_isbn in isbns[:2]:
                            cand_isbn = self.fetch_by_isbn(raw_isbn)
                            if cand_isbn and not cand_isbn.is_empty():
                                if (not cand_isbn.title or score_title(cand_isbn.title, title) >= 0.45) and (
                                    not author or not cand_isbn.authors or score_authors(cand_isbn.authors, [author]) >= 0.40
                                ):
                                    cand = cand_isbn
                                    break

                        if not cand or not cand.description or not cand.publisher or not cand.subjects:
                            authors = doc.get("author_name", [])
                            subjects = doc.get("subject", [])[:10]
                            work_key = doc.get("key")
                            desc = (
                                self._fetch_work_description(work_key)
                                if (work_key and (not cand or not cand.description))
                                else None
                            )
                            doc_title = doc.get("title")
                            doc_subtitle = doc.get("subtitle")
                            if doc_title and doc_subtitle and doc_subtitle.lower() not in doc_title.lower():
                                doc_title = f"{doc_title}: {doc_subtitle}"
                            cov_url = (
                                f"https://covers.openlibrary.org/b/id/{doc['cover_i']}-L.jpg"
                                if doc.get("cover_i")
                                else None
                            )
                            doc_i13, doc_i10 = (None, None)
                            for raw_i in isbns:
                                doc_i13, doc_i10 = normalize_isbn(raw_i)
                                if doc_i13 or doc_i10:
                                    break
                            doc_meta = BookMetadata(
                                title=doc_title,
                                authors=authors,
                                description=desc,
                                subjects=subjects,
                                publisher=doc.get("publisher", [None])[0] if doc.get("publisher") else None,
                                published_date=str(doc.get("first_publish_year")) if doc.get("first_publish_year") else None,
                                isbn13=doc_i13,
                                isbn10=doc_i10,
                                cover_url=cov_url,
                                source="open-library-search",
                            )
                            cand = cand.merge_with(doc_meta, combine_lists=True) if cand else doc_meta

                        if not cand or cand.is_empty():
                            continue
                        if merged is None:
                            merged = cand
                        else:
                            merged = merged.merge_with(cand, combine_lists=True)

                        if (
                            merged.title
                            and merged.authors
                            and merged.description
                            and (merged.isbn13 or merged.isbn10)
                            and merged.publisher
                            and merged.cover_url
                        ):
                            break
                    return merged
        except Exception as e:
            self._disabled = True
            logger.warning("Open Library search query error: %s", e)
        return None
