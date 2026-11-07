"""Base interface for book metadata providers."""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Optional

from ..models import BookMetadata


class BaseProvider(ABC):
    """Abstract metadata provider."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Name of the metadata provider."""
        pass

    @abstractmethod
    def fetch_by_isbn(self, isbn: str) -> Optional[BookMetadata]:
        """Fetch metadata by ISBN-10 or ISBN-13."""
        pass

    @abstractmethod
    def fetch_by_query(self, title: str, author: Optional[str] = None) -> Optional[BookMetadata]:
        """Fetch metadata by book title and optional author."""
        pass
