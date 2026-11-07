"""Abstract base interface for reading and writing ebook/document files."""

from __future__ import annotations
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional, Tuple

from ..models import BookMetadata


class BaseReader(ABC):
    """Abstract interface for book file inspection and surgical metadata updating."""

    def __init__(self, file_path: Path):
        self.file_path = Path(file_path).resolve()
        if not self.file_path.exists():
            raise FileNotFoundError(f"File not found: {self.file_path}")

    @abstractmethod
    def read_metadata(self) -> BookMetadata:
        """Extract existing embedded metadata from the file."""
        pass

    @abstractmethod
    def extract_front_text(self, max_pages: int = 15) -> str:
        """Extract text from the beginning of the book (title, copyright, front matter)."""
        pass

    @abstractmethod
    def extract_front_images(self, max_pages: int = 3) -> List[bytes]:
        """Extract rendered images of the first few pages (for visual OCR / AI inspection)."""
        pass

    def extract_cover_bytes(self) -> Optional[bytes]:
        """Extract raw cover image bytes from the document if available."""
        images = self.extract_front_images(max_pages=1)
        return images[0] if images else None


    @abstractmethod
    def write_metadata(
        self,
        metadata: BookMetadata,
        backup: bool = False,
        update_cover: bool = False,
    ) -> None:
        """
        Surgically write updated metadata directly to the file without altering
        other content, layout, styling, or structure.
        """
        pass
