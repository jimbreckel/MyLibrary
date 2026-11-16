"""Metadata aggregator combining multiple providers."""

from __future__ import annotations
import logging
from typing import List, Optional

from ..models import BookMetadata
from .base import BaseProvider
from .open_library import OpenLibraryProvider
from .google_books import GoogleBooksProvider
from .amazon_goodreads import AmazonGoodreadsProvider, is_asin

logger = logging.getLogger(__name__)


class MetadataAggregator:
    """Queries multiple metadata providers and merges fields intelligently."""

    def __init__(self, providers: Optional[List[BaseProvider]] = None):
        if providers is None:
            # Default priority: Open Library, Google Books, and Amazon/Goodreads (Kindle ASINs & retail)
            self.providers: List[BaseProvider] = [
                OpenLibraryProvider(),
                GoogleBooksProvider(),
                AmazonGoodreadsProvider(),
            ]
        else:
            self.providers = providers
        self._rate_limited_providers: set[str] = set()

    def fetch_by_isbn(self, isbn: str) -> Optional[BookMetadata]:
        """Query providers and merge retrieved metadata."""
        merged: Optional[BookMetadata] = None
        last_rate_limit_err = None
        active_providers = [
            p for p in self.providers
            if p.name not in self._rate_limited_providers and not getattr(p, "quota_exhausted", False)
        ]
        if not active_providers:
            self._rate_limited_providers.clear()
            active_providers = [p for p in self.providers if not getattr(p, "quota_exhausted", False)]

        queried_is_asin = is_asin(isbn)
        for provider in active_providers:
            if queried_is_asin and not isinstance(provider, AmazonGoodreadsProvider):
                # Open Library and Google Books isbn: queries do not accept B0... Amazon ASINs
                continue
            try:
                id_label = "ASIN" if queried_is_asin else "ISBN"
                logger.info("Fetching metadata from %s for %s %s...", provider.name, id_label, isbn)
                meta = provider.fetch_by_isbn(isbn)
                if meta and not meta.is_empty():
                    if merged is None:
                        merged = meta
                    else:
                        merged = merged.merge_with(meta, combine_lists=True)

                    # If we already have title, author, description, cover, and publisher/subjects, we're complete
                    if (
                        merged.title
                        and merged.authors
                        and merged.description
                        and merged.cover_url
                        and (merged.publisher or merged.subjects)
                    ):
                        break
            except Exception as e:
                from ..exceptions import RateLimitExceededError
                if isinstance(e, RateLimitExceededError):
                    logger.warning(
                        "Provider %s hit quota/rate limit (429); disabling %s for remainder of cycle and continuing with remaining providers.",
                        provider.name,
                        provider.name,
                    )
                    self._rate_limited_providers.add(provider.name)
                    last_rate_limit_err = e
                    continue
                logger.warning("Error fetching from %s: %s", provider.name, e)

        if len(self._rate_limited_providers) >= len(self.providers) and last_rate_limit_err is not None:
            raise last_rate_limit_err

        return merged

    def fetch_by_query(self, title: str, author: Optional[str] = None) -> Optional[BookMetadata]:
        """Search providers by title/author and merge."""
        merged: Optional[BookMetadata] = None
        last_rate_limit_err = None
        active_providers = [
            p for p in self.providers
            if p.name not in self._rate_limited_providers and not getattr(p, "quota_exhausted", False)
        ]
        if not active_providers:
            self._rate_limited_providers.clear()
            active_providers = [p for p in self.providers if not getattr(p, "quota_exhausted", False)]

        for provider in active_providers:
            try:
                logger.info("Searching %s for '%s'...", provider.name, title)
                meta = provider.fetch_by_query(title, author)
                if meta and not meta.is_empty():
                    if merged is None:
                        merged = meta
                    else:
                        merged = merged.merge_with(meta, combine_lists=True)

                    if (
                        merged.title
                        and merged.authors
                        and merged.description
                        and (merged.isbn13 or merged.isbn10 or merged.asin)
                        and merged.publisher
                        and merged.cover_url
                    ):
                        break
            except Exception as e:
                from ..exceptions import RateLimitExceededError
                if isinstance(e, RateLimitExceededError):
                    logger.warning(
                        "Provider %s hit quota/rate limit (429); disabling %s for remainder of cycle and continuing with remaining providers.",
                        provider.name,
                        provider.name,
                    )
                    self._rate_limited_providers.add(provider.name)
                    last_rate_limit_err = e
                    continue
                logger.warning("Error searching %s: %s", provider.name, e)

        if len(self._rate_limited_providers) >= len(self.providers) and last_rate_limit_err is not None:
            raise last_rate_limit_err

        return merged
