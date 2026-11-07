"""Metadata providers subpackage."""

from .base import BaseProvider
from .open_library import OpenLibraryProvider
from .google_books import GoogleBooksProvider
from .aggregator import MetadataAggregator

__all__ = [
    "BaseProvider",
    "OpenLibraryProvider",
    "GoogleBooksProvider",
    "MetadataAggregator",
]
