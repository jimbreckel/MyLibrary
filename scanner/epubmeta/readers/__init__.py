"""Ebook and document reader/writer registry."""

from __future__ import annotations
from pathlib import Path
from typing import Union
from .base import BaseReader
from .epub import EpubReader
from .mobi import GenericBookReader, MOBI_EXTENSIONS, MobiReader, OTHER_BOOK_EXTENSIONS
from .pdf import PdfReader

SUPPORTED_EXTENSIONS = {".epub", ".pdf"} | MOBI_EXTENSIONS | OTHER_BOOK_EXTENSIONS


def get_reader(file_path: Union[str, Path]) -> BaseReader:
    """Instantiate appropriate reader based on file extension."""
    path = Path(file_path)
    ext = path.suffix.lower()
    if ext == ".epub":
        return EpubReader(path)
    elif ext == ".pdf":
        return PdfReader(path)
    elif ext in MOBI_EXTENSIONS:
        return MobiReader(path)
    elif ext in OTHER_BOOK_EXTENSIONS:
        return GenericBookReader(path)
    else:
        raise ValueError(
            f"Unsupported file format '{ext}'. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}."
        )


__all__ = [
    "BaseReader",
    "EpubReader",
    "PdfReader",
    "MobiReader",
    "GenericBookReader",
    "get_reader",
    "SUPPORTED_EXTENSIONS",
    "MOBI_EXTENSIONS",
    "OTHER_BOOK_EXTENSIONS",
]
