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


def _is_valid_cover_graphic_bytes(raw_bytes: Optional[bytes]) -> bool:
    """Return True if `raw_bytes` decodes to a non-blank, non-text-only cover image."""
    if not raw_bytes or len(raw_bytes) < 1500:
        return False
    try:
        import io
        from PIL import Image

        im = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
        w, h = im.size
        if w < 90 or h < 130:
            return False
        small = im.resize((64, 96))
        px = list(small.get_flattened_data() if hasattr(small, "get_flattened_data") else small.getdata())
        n = len(px)
        if not n:
            return False
        white = 0
        dark = 0
        colored = 0
        mid = 0
        for r, g, b in px:
            mx = max(r, g, b)
            mn = min(r, g, b)
            sat = mx - mn
            if mn >= 215 and sat < 20:
                white += 1
            elif mx <= 85:
                dark += 1
            elif sat >= 26:
                colored += 1
            else:
                mid += 1
        if (white / n) >= 0.85 or (dark / n) >= 0.95:
            return False
        if (white / n) >= 0.72 and (colored / n) <= 0.03 and (mid / n) <= 0.16:
            return False
        return True
    except Exception:
        return False


def extract_pdf_page0_cover_bytes(doc) -> Optional[bytes]:
    """
    Extract cover image bytes from Page 1 (`doc[0]`) of an open PyMuPDF document:
      1. If Page 1 has NO external text (`len(p_text) == 0`) and contains a portrait book-cover
         image (`0.48 <= iw / ih <= 0.78`), return that embedded image directly.
      2. If Page 1 is a hybrid jacket / header / prologue page (an embedded portrait cover image
         occupying `4%..52%` of the page alongside `>= 180` chars or `>= 5` lines of body/synopsis
         text on a mostly-white page), extract the embedded portrait cover image directly instead
         of rendering the whole text page.
      3. Otherwise (e.g. Springer / Routledge / Elsevier / O'Reilly covers combining vector panels,
         banners, and title/author text), render full Page 1 at 150 DPI.
    """
    if len(doc) == 0:
        return None
    p0 = doc[0]
    p_rect = p0.rect
    p_area = max(1.0, float(p_rect.width * p_rect.height))
    p_text = (p0.get_text() or "").strip()
    imgs = p0.get_images(full=True)

    def _get_xref_image(img_entry):
        xref = img_entry[0]
        smask = img_entry[1] if len(img_entry) > 1 else 0
        try:
            extracted = doc.extract_image(xref)
            raw_img = extracted.get("image") if extracted else None
            iw = int(extracted.get("width") or 0) if extracted else 0
            ih = int(extracted.get("height") or 0) if extracted else 0
            ext_fmt = (extracted.get("ext") or "").lower() if extracted else ""
            cs = int(extracted.get("colorspace") or 3) if extracted else 3
            if smask == 0 and raw_img and ext_fmt in ("jpg", "jpeg", "png") and cs in (1, 3):
                return raw_img, iw, ih, xref
        except Exception:
            pass
        try:
            pix = pymupdf.Pixmap(doc, xref)
            if pix.n - pix.alpha > 3 or pix.alpha:
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
            return pix.tobytes("png"), pix.width, pix.height, xref
        except Exception:
            return None, 0, 0, xref

    portrait_cands = []
    for entry in imgs[:8]:
        raw_img, iw, ih, xref = _get_xref_image(entry)
        if not raw_img or len(raw_img) < 2000 or iw < 100 or ih < 150:
            continue
        aspect = iw / float(ih)
        if not (0.48 <= aspect <= 0.78):
            continue
        rects = p0.get_image_rects(xref)
        r0 = (rects[0] & p_rect) if rects else None
        r_area = float(r0.width * r0.height) if r0 else 0.0
        portrait_cands.append((r_area or (iw * ih), raw_img, iw, ih, r0, r_area))

    if portrait_cands:
        portrait_cands.sort(key=lambda t: t[0], reverse=True)
        _, raw_img, iw, ih, r0, r_area = portrait_cands[0]
        area_frac = (r_area / p_area) if r0 else 0.5
        if len(p_text) == 0 and len(portrait_cands) == 1:
            if (r0 is None or 0.05 <= area_frac <= 0.96) and _is_valid_cover_graphic_bytes(raw_img):
                return raw_img
        elif r0 is not None and 0.04 <= area_frac <= 0.52 and len(p_text) >= 140:
            try:
                blocks = p0.get_text("blocks") or []
                pad_rect = pymupdf.Rect(r0.x0 - 4, r0.y0 - 4, r0.x1 + 4, r0.y1 + 4)
                ext_parts = []
                for b in blocks:
                    if len(b) >= 7 and b[6] != 0:
                        continue
                    txt_b = (b[4] or "").strip() if len(b) >= 5 else ""
                    if not txt_b:
                        continue
                    cx = (float(b[0]) + float(b[2])) * 0.5
                    cy = (float(b[1]) + float(b[3])) * 0.5
                    if not pad_rect.contains(pymupdf.Point(cx, cy)):
                        ext_parts.append(txt_b)
                ext_text = "\n".join(ext_parts).strip()
                ext_lines = [ln.strip() for ln in ext_text.splitlines() if len(ln.strip()) >= 15]
                if len(ext_text) >= 180 or (len(ext_text) >= 120 and len(ext_lines) >= 5):
                    # Verify the surrounding page is a white-margin document page (not a colored vector book cover)
                    tpix = p0.get_pixmap(dpi=36)
                    if tpix.n > 3:
                        tpix = pymupdf.Pixmap(pymupdf.csRGB, tpix)
                    tw, th, tn = tpix.width, tpix.height, tpix.n
                    samples = tpix.samples
                    sx = tw / max(1.0, float(p_rect.width))
                    sy = th / max(1.0, float(p_rect.height))
                    ix0, iy0 = int(r0.x0 * sx), int(r0.y0 * sy)
                    ix1, iy1 = int(r0.x1 * sx), int(r0.y1 * sy)
                    bx_m = max(2, int(tw * 0.07))
                    by_m = max(2, int(th * 0.07))
                    out_tot = out_white = bdr_tot = bdr_white = 0
                    for py in range(th):
                        row_off = py * tw * tn
                        in_y_bdr = py < by_m or py >= (th - by_m)
                        for px_x in range(tw):
                            off = row_off + px_x * tn
                            if tn >= 3:
                                r_v, g_v, b_v = samples[off], samples[off + 1], samples[off + 2]
                            else:
                                r_v = g_v = b_v = samples[off]
                            is_w = min(r_v, g_v, b_v) >= 225 and (max(r_v, g_v, b_v) - min(r_v, g_v, b_v)) <= 18
                            if in_y_bdr or px_x < bx_m or px_x >= (tw - bx_m):
                                bdr_tot += 1
                                if is_w:
                                    bdr_white += 1
                            if not (ix0 <= px_x <= ix1 and iy0 <= py <= iy1):
                                out_tot += 1
                                if is_w:
                                    out_white += 1
                    if (
                        bdr_tot > 0
                        and out_tot > 0
                        and (bdr_white / bdr_tot) >= 0.85
                        and (out_white / out_tot) >= 0.60
                        and _is_valid_cover_graphic_bytes(raw_img)
                    ):
                        return raw_img
            except Exception:
                pass

    pix = p0.get_pixmap(dpi=150)
    return pix.tobytes("png")


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

            # Look for ISBN in keywords, subject, or title (including raw_title before garbage filter, e.g. 3135239039.01._SS500_SCLZZZZZZZ_.jpg)
            isbn13 = None
            isbn10 = None
            search_corpus = f"{keywords_str} {subject or ''} {raw_title or ''} {title or ''}"
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
        Extract cover image bytes from Page 1 of the PDF (extracting embedded portrait covers directly
        on blank-margin or hybrid jacket/prologue pages, while rendering full Page 1 for vector/composite covers).
        """
        doc = pymupdf.open(self.file_path)
        try:
            return extract_pdf_page0_cover_bytes(doc)
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
        try:
            f_size = self.file_path.stat().st_size
        except Exception:
            f_size = 0
        if f_size > 20 * 1024 * 1024:
            raise RuntimeError(
                f"Large PDF ({f_size / (1024 * 1024):.1f} MB > 20 MB limit); skipping full-file stream rewrite"
            )
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
