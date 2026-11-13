"""Surgical PDF metadata reader and writer.

Updates document Info dictionary and XMP metadata directly without re-rasterizing
or modifying the pages, fonts, or vector contents.
"""

from __future__ import annotations
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import List, Optional

import pymupdf

from ..models import BookMetadata
from ..isbn.validator import normalize_isbn
from ..isbn.extractor import extract_isbns_from_text
from .base import BaseReader

logger = logging.getLogger(__name__)


def _build_xmp_packet(metadata: BookMetadata) -> str:
    """Build a standard XMP Dublin Core metadata packet."""
    title = (metadata.title or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    desc = (metadata.description or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    publisher = (metadata.publisher or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    date = (metadata.published_date or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    isbn = metadata.isbn13 or metadata.isbn10 or ""

    from ..authors import split_and_normalize_authors
    norm_authors = split_and_normalize_authors(metadata.authors)

    authors_xml = "".join(
        f"<rdf:li>{disp.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')}</rdf:li>"
        for disp, _ in norm_authors
    )
    subjects_xml = "".join(
        f"<rdf:li>{s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')}</rdf:li>"
        for s in metadata.subjects
    )

    keywords_str = ", ".join(metadata.subjects).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    return f"""<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about=""
        xmlns:dc="http://purl.org/dc/elements/1.1/"
        xmlns:pdf="http://ns.adobe.com/pdf/1.3/"
        xmlns:xmp="http://ns.adobe.com/xap/1.0/">
      <dc:format>application/pdf</dc:format>
      <dc:title>
        <rdf:Alt>
          <rdf:li xml:lang="x-default">{title}</rdf:li>
        </rdf:Alt>
      </dc:title>
      <dc:creator>
        <rdf:Seq>
          {authors_xml}
        </rdf:Seq>
      </dc:creator>
      <dc:description>
        <rdf:Alt>
          <rdf:li xml:lang="x-default">{desc}</rdf:li>
        </rdf:Alt>
      </dc:description>
      <dc:subject>
        <rdf:Bag>
          {subjects_xml}
        </rdf:Bag>
      </dc:subject>
      <dc:publisher>
        <rdf:Bag>
          <rdf:li>{publisher}</rdf:li>
        </rdf:Bag>
      </dc:publisher>
      <dc:date>
        <rdf:Seq>
          <rdf:li>{date}</rdf:li>
        </rdf:Seq>
      </dc:date>
      <dc:identifier>urn:isbn:{isbn}</dc:identifier>
      <pdf:Keywords>{keywords_str}</pdf:Keywords>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""


class PdfReader(BaseReader):
    """Reader and in-place metadata writer for PDF documents."""

    def __init__(self, file_path: Path):
        super().__init__(file_path)
        from ..exceptions import DrmProtectedError, InvalidDocumentError
        try:
            doc = pymupdf.open(self.file_path)
            if doc.needs_pass and not doc.authenticate(""):
                doc.close()
                raise DrmProtectedError(f"PDF is encrypted / password protected: {self.file_path.name}")
            if len(doc) == 0:
                doc.close()
                raise InvalidDocumentError(f"PDF has 0 pages / is empty: {self.file_path.name}")
            doc.close()
        except Exception as e:
            if isinstance(e, (InvalidDocumentError, DrmProtectedError)):
                raise
            raise InvalidDocumentError(f"Corrupt or invalid PDF document: {e}")

    def read_metadata(self) -> BookMetadata:
        """Extract metadata from PDF Document Info and XMP stream."""
        doc = pymupdf.open(self.file_path)
        try:
            from ..filename_parser import is_garbage_embedded_title, is_garbage_embedded_author, parse_filename
            meta = doc.metadata or {}
            raw_title = (meta.get("title") or "").strip()
            title = raw_title if (raw_title and not is_garbage_embedded_title(raw_title)) else None
            if title and len(title.split()) == 1 and len(title) <= 6:
                fn_clues = parse_filename(self.file_path.name)
                fn_title = (fn_clues.get("title") or "").strip()
                if len(fn_title.split()) >= 3 and title.lower() not in fn_title.lower():
                    title = None
            author_str = meta.get("author") or ""
            authors = [
                a.strip()
                for a in re.split(r"[,;&]\s*", author_str)
                if a.strip() and not is_garbage_embedded_author(a.strip())
            ]

            subject = meta.get("subject") or None  # Often used as summary / description in PDFs
            keywords_str = meta.get("keywords") or ""
            subjects = [k.strip() for k in re.split(r"[,;]\s*", keywords_str) if k.strip()]

            # Look for ISBN in keywords, subject, or title
            isbn13 = None
            isbn10 = None
            search_corpus = f"{keywords_str} {subject or ''} {title or ''}"
            isbns = extract_isbns_from_text(search_corpus)
            if isbns:
                isbn13, _ = normalize_isbn(isbns[0][0])

            # Check XMP metadata if available
            xmp = doc.get_xml_metadata() or ""
            if xmp and not isbn13:
                xmp_isbns = extract_isbns_from_text(xmp)
                if xmp_isbns:
                    isbn13, _ = normalize_isbn(xmp_isbns[0][0])

            return BookMetadata(
                title=title,
                authors=authors,
                description=subject,
                subjects=subjects,
                isbn13=isbn13,
                isbn10=isbn10,
                source="pdf-embedded",
            )
        finally:
            doc.close()

    def extract_front_text(self, max_pages: int = 15) -> str:
        """Extract text from the first N pages."""
        doc = pymupdf.open(self.file_path)
        try:
            texts = []
            page_count = min(max_pages, len(doc))
            for i in range(page_count):
                txt = doc[i].get_text()
                if txt.strip():
                    texts.append(txt.strip())
            return "\n\n--- PAGE BREAK ---\n\n".join(texts)
        finally:
            doc.close()

    def extract_front_images(self, max_pages: int = 3) -> List[bytes]:
        """Render the first few pages as PNG images for visual OCR / AI inspection."""
        doc = pymupdf.open(self.file_path)
        try:
            images = []
            page_count = min(max_pages, len(doc))
            for i in range(page_count):
                # Render at 150 DPI for crisp readability
                pix = doc[i].get_pixmap(dpi=150)
                images.append(pix.tobytes("png"))
            return images
        finally:
            doc.close()

    def extract_cover_bytes(self) -> Optional[bytes]:
        """
        Extract cover image bytes from Page 1 of the PDF.
        If Page 1 is a cover/title page (`< 300` chars of text) with a single cover-proportioned raster image
        floating inside wide white page margins (e.g. Liquid Silver Books / Boson Books PDFs where a 240x360 or
        510x680 cover JPEG is placed on a blank 8.5x11" page), extract that embedded cover image directly so it
        fills the cover frame without an excessive white page border.
        """
        doc = pymupdf.open(self.file_path)
        try:
            if len(doc) == 0:
                return None
            p0 = doc[0]
            p_rect = p0.rect
            p_area = max(1.0, float(p_rect.width * p_rect.height))
            p_text = (p0.get_text() or "").strip()
            imgs = p0.get_images(full=True)
            if len(imgs) == 1 and len(p_text) < 300:
                xref = imgs[0][0]
                try:
                    extracted = doc.extract_image(xref)
                    raw_img = extracted.get("image") if extracted else None
                    iw = int(extracted.get("width") or 0) if extracted else 0
                    ih = int(extracted.get("height") or 0) if extracted else 0
                    if raw_img and len(raw_img) >= 2000 and iw >= 120 and ih >= 150 and 0.42 <= (iw / ih) <= 1.20:
                        rects = p0.get_image_rects(xref)
                        if not rects:
                            return raw_img
                        r0 = rects[0] & p_rect
                        r_area = float(r0.width * r0.height)
                        if 0.04 <= (r_area / p_area) <= 0.94:
                            return raw_img
                except Exception:
                    pass
            pix = p0.get_pixmap(dpi=150)
            return pix.tobytes("png")
        finally:
            doc.close()

    def write_metadata(
        self,
        metadata: BookMetadata,
        backup: bool = False,
        update_cover: bool = False,
        backup_path: Optional[Path] = None,
    ) -> None:
        """Surgically write updated metadata into the PDF document."""
        doc = pymupdf.open(self.file_path)
        temp_fd, temp_path_str = tempfile.mkstemp(
            prefix=".epubmeta_tmp_",
            suffix=".tmp",
            dir=self.file_path.parent,
        )
        os.close(temp_fd)
        temp_path = Path(temp_path_str)

        try:
            meta = dict(doc.metadata or {})
            if metadata.title:
                meta["title"] = metadata.title
            if metadata.authors:
                from ..authors import split_and_normalize_authors
                norm_authors = split_and_normalize_authors(metadata.authors)
                meta["author"] = ", ".join(disp for disp, _ in norm_authors)
            if metadata.description:
                meta["subject"] = metadata.description
            if metadata.subjects:
                meta["keywords"] = ", ".join(metadata.subjects)

            doc.set_metadata(meta)

            # Also inject standard XMP packet
            xmp = _build_xmp_packet(metadata)
            doc.set_xml_metadata(xmp)

            # Save cleanly to temporary file
            doc.save(
                str(temp_path),
                garbage=3,
                deflate=True,
            )
            doc.close()

            from ..file_utils import (
                get_file_timestamps,
                preserve_file_ownership_and_mode,
                restore_file_timestamps,
            )
            orig_timestamps = get_file_timestamps(self.file_path)

            # Backup if requested in backup_path (mirrored tree) or bak/ subdirectory
            if backup:
                bak_dest = backup_path or (self.file_path.parent / "bak" / self.file_path.name)
                bak_dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.file_path, bak_dest)
                restore_file_timestamps(bak_dest, orig_timestamps)
                logger.info("Backup created: %s", bak_dest)

            preserve_file_ownership_and_mode(self.file_path, temp_path)
            shutil.move(str(temp_path), str(self.file_path))
            restore_file_timestamps(self.file_path, orig_timestamps)
            logger.info("Successfully updated PDF metadata (preserved timestamps): %s", self.file_path)

        finally:
            if not doc.is_closed:
                doc.close()
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass
