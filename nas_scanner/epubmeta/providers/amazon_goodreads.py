"""Amazon / Goodreads & Apple Books metadata provider for Kindle (ASIN) and retail ebooks."""

from __future__ import annotations
from datetime import datetime, timezone
import html
import json
import logging
import re
from typing import Any, Dict, List, Optional
import requests

from ..models import BookMetadata, is_non_english_for_book, is_truncated_description
from ..isbn.validator import normalize_isbn
from .base import BaseProvider

logger = logging.getLogger(__name__)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


def _clean_html_text(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    unescaped = html.unescape(raw)
    text = re.sub(r"<br\s*/?>", "\n", unescaped, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text or None


def _upgrade_goodreads_cover(url: Optional[str]) -> Optional[str]:
    """Convert Goodreads/Amazon thumbnail URL (._SY75_.jpg / ._SX98_.jpg) into high-res cover URL."""
    if not url:
        return None
    if "nophoto" in url:
        return None
    return re.sub(r"\._S[XY]\d+_\.", ".", url)


def is_asin(identifier: Optional[str]) -> bool:
    """Return True if identifier is a 10-character Amazon ASIN (e.g. B0HJ92GY64)."""
    if not identifier:
        return False
    cleaned = identifier.strip().upper()
    return bool(re.match(r"^B[0-9A-Z]{9}$", cleaned))


class AmazonGoodreadsProvider(BaseProvider):
    """
    Fetches book and Kindle Unlimited metadata from Amazon's Goodreads catalog
    (supports both Amazon ASINs like B0... and ISBNs, plus title/author search)
    with Apple Books enrichment for full synopses and genres.
    """

    @property
    def name(self) -> str:
        return "Amazon/Goodreads"

    def __init__(self, timeout: int = 12):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)

    def _parse_autocomplete_item(self, item: Dict[str, Any], queried_asin: Optional[str] = None) -> BookMetadata:
        title = (item.get("title") or item.get("bookTitleBare") or "").strip() or None
        author_obj = item.get("author") or {}
        author_name = (author_obj.get("name") or "").strip()
        authors = [author_name] if author_name else []

        desc_obj = item.get("description") or {}
        description = _clean_html_text(desc_obj.get("html"))
        if description and is_non_english_for_book(description, title):
            description = None
        cover_url = _upgrade_goodreads_cover(item.get("imageUrl"))

        return BookMetadata(
            title=title,
            authors=authors,
            description=description,
            cover_url=cover_url,
            asin=queried_asin if is_asin(queried_asin) else None,
            source="amazon-goodreads",
        )

    def _try_fetch_next_data_by_isbn_or_asin(
        self,
        identifier: str,
        book_id: Optional[str] = None,
    ) -> Optional[BookMetadata]:
        """Attempt to fetch full Apollo book details from `/book/show/{book_id}.json` (or `/book/isbn/{identifier}`)."""
        candidate_urls: List[str] = []
        clean_bid = re.sub(r"[^0-9]", "", str(book_id or ""))
        if clean_bid:
            candidate_urls.append(f"https://www.goodreads.com/book/show/{clean_bid}.json")
        clean_ident = (identifier or "").strip()
        if clean_ident and clean_ident != clean_bid:
            candidate_urls.append(f"https://www.goodreads.com/book/isbn/{clean_ident}")

        resp = None
        for url in candidate_urls:
            try:
                r = self.session.get(
                    url,
                    headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
                    timeout=self.timeout,
                )
                if r.status_code == 200 and "__NEXT_DATA__" in r.text:
                    resp = r
                    break
            except Exception as req_e:
                logger.debug("Goodreads page fetch failed for %s: %s", url, req_e)

        if resp is None:
            return None
        try:

            m = re.search(r'id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', resp.text, re.S)
            if not m:
                return None

            nd = json.loads(m.group(1))
            apollo = nd.get("props", {}).get("pageProps", {}).get("apolloState", {})
            if not apollo:
                return None

            book_obj = None
            for k, v in apollo.items():
                if k.startswith("Book:") and isinstance(v, dict) and (v.get("titleComplete") or v.get("title")):
                    book_obj = v
                    break

            if not book_obj:
                return None

            title = (book_obj.get("titleComplete") or book_obj.get("title") or "").strip() or None
            description = _clean_html_text(book_obj.get("description"))
            if description and is_non_english_for_book(description, title):
                description = None
            cover_url = _upgrade_goodreads_cover(book_obj.get("imageUrl"))

            # Contributors (Authors)
            authors: List[str] = []
            primary_edge = book_obj.get("primaryContributorEdge") or {}
            edges = [primary_edge] + list(book_obj.get("secondaryContributorEdges") or [])
            for edge in edges:
                if not isinstance(edge, dict):
                    continue
                role = (edge.get("role") or "Author").lower()
                if role not in ("author", "primary"):
                    continue
                node_ref = (edge.get("node") or {}).get("__ref")
                if node_ref and node_ref in apollo:
                    c_name = (apollo[node_ref].get("name") or "").strip()
                    if c_name and c_name not in authors:
                        authors.append(c_name)

            details = book_obj.get("details") or {}
            asin = details.get("asin") or (identifier.upper() if is_asin(identifier) else None)
            raw_isbn13 = details.get("isbn13") or details.get("isbn")
            isbn13, isbn10 = normalize_isbn(raw_isbn13) if raw_isbn13 else (None, None)
            publisher = (details.get("publisher") or "").strip() or None

            published_date = None
            pub_ms = details.get("publicationTime")
            if isinstance(pub_ms, (int, float)):
                try:
                    dt = datetime.fromtimestamp(pub_ms / 1000.0, tz=timezone.utc)
                    published_date = dt.strftime("%Y-%m-%d")
                except Exception:
                    pass

            lang_obj = details.get("language") or {}
            language = (lang_obj.get("name") or "").strip() or None
            if language and language.lower() == "english":
                language = "en"

            subjects: List[str] = []
            for g in book_obj.get("bookGenres") or []:
                g_name = ((g.get("genre") or {}).get("name") or "").strip()
                if g_name and g_name not in subjects:
                    subjects.append(g_name)

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
                cover_url=cover_url,
                source="amazon-goodreads",
            )
        except Exception as e:
            logger.debug("Goodreads Apollo lookup failed for %s: %s", identifier, e)
            return None

    def _enrich_from_audible_catalog(self, title: str, author: Optional[str] = None) -> Optional[BookMetadata]:
        """Query Audible's public catalog JSON API for official untruncated Amazon/Audible publisher_summary."""
        if not title:
            return None
        short_title = re.split(r"\s*(?::|\()\s*", title, maxsplit=1)[0].strip() or title.strip()
        params: Dict[str, Any] = {
            "title": short_title,
            "response_groups": "product_desc,product_attrs,contributors",
            "num_results": 5,
        }
        if author and author.lower() not in ("unknown", "unknown author"):
            params["author_name"] = author
        try:
            resp = self.session.get(
                "https://api.audible.com/1.0/catalog/products",
                params=params,
                headers={"Accept": "application/json"},
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                return None
            products = (resp.json() or {}).get("products") or []
            if not products:
                return None
            from ..matcher import score_title, score_authors
            for prod in products:
                cand_t = (prod.get("title") or "").strip()
                if not cand_t or (score_title(cand_t, title) < 0.45 and score_title(cand_t, short_title) < 0.45):
                    continue
                cand_authors = [
                    (a.get("name") or "").strip()
                    for a in (prod.get("authors") or [])
                    if isinstance(a, dict) and (a.get("name") or "").strip()
                ]
                if author and author.lower() not in ("unknown", "unknown author") and cand_authors:
                    if score_authors(cand_authors, [author]) < 0.40:
                        continue
                desc = _clean_html_text(prod.get("publisher_summary") or prod.get("merchandising_summary"))
                if desc and is_non_english_for_book(desc, cand_t):
                    desc = None
                if not desc or is_truncated_description(desc):
                    continue
                return BookMetadata(
                    title=cand_t,
                    authors=cand_authors,
                    description=desc,
                    publisher=(prod.get("publisher_name") or "").strip() or None,
                    published_date=(prod.get("release_date") or "")[:10] or None,
                    source="audible-catalog",
                )
        except Exception as e:
            logger.debug("Audible catalog lookup failed for '%s': %s", short_title, e)
        return None

    def _enrich_from_apple_books(self, title: str, author: Optional[str] = None) -> Optional[BookMetadata]:
        """Query Apple Books ebook catalog for full untruncated description, genres, and release date."""
        if not title:
            return None
        short_title = re.split(r"\s*(?::|\()\s*", title, maxsplit=1)[0].strip() or title.strip()
        term = f"{short_title} {author}".strip() if author else short_title
        try:
            resp = self.session.get(
                "https://itunes.apple.com/search",
                params={"term": term, "media": "ebook", "limit": 3},
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                return None
            results = resp.json().get("results") or []
            if not results:
                return None

            # Pick best title + author match
            from ..matcher import score_title, score_authors
            best = None
            for r in results:
                cand_t = (r.get("trackName") or "").strip()
                artist_cand = (r.get("artistName") or "").strip()
                cand_authors = [a.strip() for a in re.split(r"\s*(?:&|,| and )\s*", artist_cand) if a.strip()] if artist_cand else []
                if not cand_t or (score_title(cand_t, title) < 0.45 and score_title(cand_t, short_title) < 0.45):
                    continue
                if author and author.lower() not in ("unknown", "unknown author") and cand_authors:
                    if score_authors(cand_authors, [author]) < 0.40:
                        continue
                best = r
                break
            if not best:
                return None

            artist = (best.get("artistName") or "").strip()
            authors = [a.strip() for a in re.split(r"\s*(?:&|,| and )\s*", artist) if a.strip()] if artist else []
            desc = _clean_html_text(best.get("description"))
            genres = [g.strip() for g in (best.get("genres") or []) if g and g.strip() and g.lower() != "books"]
            pub_date = (best.get("releaseDate") or "")[:10] or None
            art_url = best.get("artworkUrl100")
            if art_url:
                art_url = art_url.replace("100x100bb", "600x600bb")

            return BookMetadata(
                title=(best.get("trackName") or "").strip() or None,
                authors=authors,
                description=desc,
                subjects=genres,
                published_date=pub_date,
                cover_url=art_url,
                source="apple-books",
            )
        except Exception as e:
            logger.debug("Apple Books lookup failed for '%s': %s", term, e)
            return None

    def fetch_by_isbn(self, isbn: str) -> Optional[BookMetadata]:
        """Query Amazon/Goodreads by ISBN-13, ISBN-10, or 10-character Amazon ASIN (e.g. B0HJ92GY64)."""
        ident = (isbn or "").strip()
        if not ident:
            return None

        # 1. Fast JSON auto_complete lookup (works for both ASIN and ISBN)
        base_meta: Optional[BookMetadata] = None
        matched_book_id: Optional[str] = None
        try:
            resp = self.session.get(
                "https://www.goodreads.com/book/auto_complete",
                params={"format": "json", "q": ident},
                timeout=self.timeout,
            )
            if resp.status_code == 200:
                items = resp.json()
                if isinstance(items, list) and items:
                    matched_book_id = str(items[0].get("bookId") or "").strip() or None
                    base_meta = self._parse_autocomplete_item(items[0], queried_asin=ident)
        except Exception as e:
            logger.debug("Amazon/Goodreads auto_complete error for %s: %s", ident, e)

        # 2. Try full Apollo page details via `/book/show/{bookId}.json` for untruncated description, publisher, date, genres
        full_meta = self._try_fetch_next_data_by_isbn_or_asin(ident, book_id=matched_book_id)
        if full_meta and base_meta:
            merged = full_meta.merge_with(base_meta, combine_lists=True)
        else:
            merged = full_meta or base_meta

        # 3. If description is still truncated/missing, enrich from Apple Books
        if merged and (not merged.description or is_truncated_description(merged.description) or not merged.subjects):
            apple_meta = self._enrich_from_apple_books(
                merged.title or "",
                merged.authors[0] if merged.authors else None,
            )
            if apple_meta:
                if apple_meta.description and (
                    not merged.description
                    or is_truncated_description(merged.description)
                    or len(apple_meta.description) > len(merged.description)
                ):
                    merged.description = apple_meta.description
                merged = merged.merge_with(apple_meta, combine_lists=True)

        # 4. If description is STILL truncated/missing, query Audible's public JSON catalog API
        if merged and (not merged.description or is_truncated_description(merged.description)):
            aud_meta = self._enrich_from_audible_catalog(
                merged.title or "",
                merged.authors[0] if merged.authors else None,
            )
            if aud_meta and aud_meta.description:
                merged.description = aud_meta.description
                merged = merged.merge_with(aud_meta, combine_lists=True)

        return merged

    def fetch_by_query(self, title: str, author: Optional[str] = None) -> Optional[BookMetadata]:
        """Search Amazon/Goodreads by title and author."""
        if not title:
            return None

        from ..filename_parser import strip_trailing_year_tag, strip_edition_suffix
        from ..authors import strip_author_role_tags
        clean_t = re.sub(r"\s+(?:no)?drm$", "", title.strip(), flags=re.IGNORECASE).strip()
        clean_t = strip_trailing_year_tag(clean_t) or clean_t
        no_ed_t = strip_edition_suffix(clean_t)
        clean_author = strip_author_role_tags(author) if author else author
        if clean_author and "," in clean_author and len(clean_author.split(",")) == 2:
            p_last, p_first = [p.strip() for p in clean_author.split(",", 1)]
            if p_first and p_last:
                clean_author = f"{p_first} {p_last}"
        queries = []
        if clean_author and clean_author.lower() not in ("unknown", "unknown author"):
            queries.append(f"{clean_t} {clean_author}")
        queries.append(clean_t)

        if no_ed_t and no_ed_t != clean_t:
            if clean_author and clean_author.lower() not in ("unknown", "unknown author"):
                queries.append(f"{no_ed_t} {clean_author}")
            queries.append(no_ed_t)

        if ":" in clean_t or "(" in clean_t or "[" in clean_t:
            short_t = re.split(r"\s*(?::|\(|\[)\s*", no_ed_t or clean_t, maxsplit=1)[0].strip()
            short_t = strip_edition_suffix(short_t) or short_t
            if short_t:
                if clean_author and clean_author.lower() not in ("unknown", "unknown author"):
                    queries.append(f"{short_t} {clean_author}")
                queries.append(short_t)

        seen_q = set()
        found_meta: Optional[BookMetadata] = None
        matched_book_id: Optional[str] = None
        for q in queries:
            q_norm = q.lower().strip()
            if not q_norm or q_norm in seen_q:
                continue
            seen_q.add(q_norm)
            try:
                resp = self.session.get(
                    "https://www.goodreads.com/book/auto_complete",
                    params={"format": "json", "q": q},
                    timeout=self.timeout,
                )
                if resp.status_code == 200:
                    from ..matcher import score_title, score_authors
                    items = resp.json()
                    if isinstance(items, list) and items:
                        for it in items[:4]:
                            cand = self._parse_autocomplete_item(it)
                            if not cand or cand.is_empty():
                                continue
                            if cand.title and score_title(cand.title, clean_t) < 0.45:
                                continue
                            if author and author.lower() not in ("unknown", "unknown author") and cand.authors:
                                if score_authors(cand.authors, [author]) < 0.40:
                                    continue
                            if found_meta is None:
                                found_meta = cand
                                matched_book_id = str(it.get("bookId") or "").strip() or None
                            else:
                                found_meta = found_meta.merge_with(cand, combine_lists=False)
                                if not matched_book_id and it.get("bookId"):
                                    matched_book_id = str(it.get("bookId")).strip()
                            if (
                                found_meta.title
                                and found_meta.authors
                                and found_meta.description
                                and not is_truncated_description(found_meta.description)
                                and found_meta.cover_url
                            ):
                                break
                        if found_meta:
                            break
            except Exception as e:
                logger.debug("Amazon/Goodreads query error for '%s': %s", q, e)

        if matched_book_id and found_meta and (
            not found_meta.description
            or is_truncated_description(found_meta.description)
            or not found_meta.subjects
            or not found_meta.publisher
        ):
            full_q_meta = self._try_fetch_next_data_by_isbn_or_asin(matched_book_id, book_id=matched_book_id)
            if full_q_meta:
                found_meta = full_q_meta.merge_with(found_meta, combine_lists=True)

        apple_meta = self._enrich_from_apple_books(clean_t, author)
        if found_meta and apple_meta:
            if apple_meta.description and (
                not found_meta.description
                or is_truncated_description(found_meta.description)
                or len(apple_meta.description) > len(found_meta.description)
            ):
                found_meta.description = apple_meta.description
            return found_meta.merge_with(apple_meta, combine_lists=True)

        if found_meta and (not found_meta.description or is_truncated_description(found_meta.description)):
            aud_q_meta = self._enrich_from_audible_catalog(clean_t, clean_author)
            if aud_q_meta and aud_q_meta.description:
                found_meta.description = aud_q_meta.description
                found_meta = found_meta.merge_with(aud_q_meta, combine_lists=True)

        return found_meta or apple_meta
