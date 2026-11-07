"""Reader for MOBI, AZW, AZW3, PRC, PDB, FB2, CBZ, and other ebook formats."""

from __future__ import annotations
import re
import struct
import zipfile
from pathlib import Path
from typing import List, Optional

from .. filename_parser import clean_book_title, parse_filename
from ..isbn.validator import normalize_isbn
from ..models import BookMetadata
from .base import BaseReader

MOBI_EXTENSIONS = {".mobi", ".azw", ".azw3", ".prc", ".pdb"}
OTHER_BOOK_EXTENSIONS = {".fb2", ".cbz", ".cbr", ".djvu", ".txt", ".rtf", ".doc", ".docx", ".lit"}


def _decompress_palmdoc_lz77(data: bytes) -> bytes:
    """Decompress a single PalmDOC LZ77-compressed text record."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        c = data[i]
        i += 1
        if c == 0:
            out.append(0)
        elif 1 <= c <= 8:
            out.extend(data[i : i + c])
            i += c
        elif 0x09 <= c <= 0x7F:
            out.append(c)
        elif 0x80 <= c <= 0xBF:
            if i >= n:
                break
            c2 = data[i]
            i += 1
            pair = ((c << 8) | c2) & 0x3FFF
            dist = pair >> 3
            length = (pair & 0x07) + 3
            if dist == 0 or dist > len(out):
                continue
            for _ in range(length):
                out.append(out[-dist])
        else:  # 0xC0..0xFF: space + (c ^ 0x80)
            out.append(0x20)
            out.append(c ^ 0x80)
    return bytes(out)


def _strip_format_suffix(title: Optional[str]) -> Optional[str]:
    if not title:
        return None
    s = re.sub(
        r"\s*\((?:mobi|azw3?|epub|pdf|retail|true epub|v\d+(?:\.\d+)?|nodrm|ocr)\)\s*",
        " ",
        str(title),
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", s).strip() or None


class MobiReader(BaseReader):
    """Extracts EXTH metadata, front-matter text, and embedded cover images from MOBI/AZW/AZW3 files."""

    def _read_palmdb_records(self, max_records: int = 400) -> List[tuple[int, int]]:
        """Return list of (start_offset, end_offset) for PalmDB records."""
        try:
            file_size = self.file_path.stat().st_size
            with open(self.file_path, "rb") as fp:
                hdr = fp.read(78)
                if len(hdr) < 78:
                    return []
                num_records = struct.unpack(">H", hdr[76:78])[0]
                if num_records <= 0 or num_records > 20000:
                    return []
                table_bytes = fp.read(num_records * 8)
            offsets = []
            for idx in range(num_records):
                pos = idx * 8
                if pos + 4 > len(table_bytes):
                    break
                off = struct.unpack(">I", table_bytes[pos : pos + 4])[0]
                if 0 <= off < file_size:
                    offsets.append(off)
            spans = []
            for idx, start in enumerate(offsets[:max_records]):
                end = offsets[idx + 1] if (idx + 1) < len(offsets) else file_size
                if end > start:
                    spans.append((start, end))
            return spans
        except Exception:
            return []

    def read_metadata(self) -> BookMetadata:
        title: Optional[str] = None
        authors: List[str] = []
        publisher: Optional[str] = None
        description: Optional[str] = None
        isbn13: Optional[str] = None
        isbn10: Optional[str] = None
        asin: Optional[str] = None
        published_date: Optional[str] = None
        subjects: List[str] = []

        try:
            with open(self.file_path, "rb") as fp:
                head = fp.read(131072)
            idx = head.find(b"EXTH")
            if idx != -1 and idx + 12 <= len(head):
                _, num_items = struct.unpack(">II", head[idx + 4 : idx + 12])
                pos = idx + 12
                for _ in range(min(num_items, 150)):
                    if pos + 8 > len(head):
                        break
                    rec_type, rec_len = struct.unpack(">II", head[pos : pos + 8])
                    if rec_len < 8 or pos + rec_len > len(head):
                        break
                    raw_val = head[pos + 8 : pos + rec_len]
                    pos += rec_len
                    txt = raw_val.decode("utf-8", errors="replace").strip("\x00 ").strip()
                    if not txt:
                        continue
                    if rec_type == 503 and not title:
                        title = txt
                    elif rec_type == 100:
                        for part in re.split(r"\s*;\s*|\s*&\s*", txt):
                            p = part.strip()
                            if p and p.lower() not in ("unknown", "unknown author") and p not in authors:
                                authors.append(p)
                    elif rec_type == 101 and not publisher:
                        publisher = txt
                    elif rec_type == 103 and not description:
                        description = txt
                    elif rec_type == 104 and not isbn13:
                        i13, i10 = normalize_isbn(txt)
                        if i13:
                            isbn13 = i13
                            isbn10 = i10
                    elif rec_type == 105:
                        for s_part in re.split(r"\s*[,;/]\s*", txt):
                            sp = s_part.strip()
                            if sp and sp not in subjects:
                                subjects.append(sp)
                    elif rec_type == 106 and not published_date:
                        published_date = txt[:10]
                    elif rec_type in (113, 504) and not asin:
                        if re.match(r"^B[0-9A-Z]{9}$", txt.upper()):
                            asin = txt.upper()
                        elif not isbn13:
                            i13, i10 = normalize_isbn(txt)
                            if i13:
                                isbn13 = i13
                                isbn10 = i10
        except Exception:
            pass

        clues = parse_filename(self.file_path)
        title = _strip_format_suffix(clean_book_title(title) or title)
        if not title:
            title = _strip_format_suffix(clues.get("title"))
        if not authors and clues.get("authors"):
            authors = [a for a in clues["authors"] if a and a.lower() not in ("unknown", "unknown author")]
        if not isbn13 and clues.get("isbn"):
            isbn13 = clues["isbn"]

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
            source="embedded-mobi",
        )

    def extract_front_text(self, max_pages: int = 15) -> str:
        spans = self._read_palmdb_records(max_records=12)
        if len(spans) < 2:
            return ""
        try:
            with open(self.file_path, "rb") as fp:
                r0_start, r0_end = spans[0]
                fp.seek(r0_start)
                rec0 = fp.read(min(r0_end - r0_start, 4096))
                if len(rec0) < 16:
                    return ""
                compression = struct.unpack(">H", rec0[0:2])[0]
                chunks: List[str] = []
                for start, end in spans[1 : min(len(spans), 6)]:
                    fp.seek(start)
                    raw = fp.read(min(end - start, 16384))
                    if compression == 1:
                        dec = raw
                    elif compression == 2:
                        dec = _decompress_palmdoc_lz77(raw)
                    else:
                        break
                    txt = dec.decode("utf-8", errors="replace")
                    txt = re.sub(r"<[^>]+>", " ", txt)
                    chunks.append(txt)
                return "\n".join(chunks)[:25000]
        except Exception:
            return ""

    def extract_front_images(self, max_pages: int = 3) -> List[bytes]:
        cov = self.extract_cover_bytes()
        return [cov] if cov else []

    def extract_cover_bytes(self) -> Optional[bytes]:
        spans = self._read_palmdb_records(max_records=600)
        if len(spans) < 2:
            return None
        try:
            with open(self.file_path, "rb") as fp:
                r0_start, r0_end = spans[0]
                fp.seek(r0_start)
                rec0 = fp.read(min(r0_end - r0_start, 65536))
                mobi_pos = rec0.find(b"MOBI")
                if mobi_pos == -1 or mobi_pos + 96 > len(rec0):
                    return None
                first_img_idx = struct.unpack(">I", rec0[mobi_pos + 92 : mobi_pos + 96])[0]
                cover_offset = None
                exth_pos = rec0.find(b"EXTH")
                if exth_pos != -1 and exth_pos + 12 <= len(rec0):
                    _, num_items = struct.unpack(">II", rec0[exth_pos + 4 : exth_pos + 12])
                    pos = exth_pos + 12
                    for _ in range(min(num_items, 150)):
                        if pos + 8 > len(rec0):
                            break
                        rec_type, rec_len = struct.unpack(">II", rec0[pos : pos + 8])
                        if rec_len < 8 or pos + rec_len > len(rec0):
                            break
                        if rec_type == 201 and rec_len == 12:
                            cover_offset = struct.unpack(">I", rec0[pos + 8 : pos + 12])[0]
                            break
                        pos += rec_len

                cand_indices: List[int] = []
                if cover_offset is not None and cover_offset < 10000:
                    cand_indices.append(first_img_idx + cover_offset)
                for extra in range(10):
                    idx_c = first_img_idx + extra
                    if idx_c not in cand_indices:
                        cand_indices.append(idx_c)

                for rec_idx in cand_indices:
                    if 0 < rec_idx < len(spans):
                        s_off, e_off = spans[rec_idx]
                        sz = e_off - s_off
                        if 1500 <= sz <= 10 * 1024 * 1024:
                            fp.seek(s_off)
                            img_bytes = fp.read(sz)
                            if img_bytes.startswith((b"\xff\xd8\xff", b"\x89PNG", b"GIF8")):
                                return img_bytes
        except Exception:
            pass
        return None

    def write_metadata(
        self,
        metadata: BookMetadata,
        backup: bool = False,
        update_cover: bool = False,
        backup_path: Optional[Path] = None,
    ) -> None:
        # Leave the binary .mobi/.azw/.azw3 container untouched on disk while BookProcessor
        # persists the enriched metadata, cover thumbnail, and tags to scanner_state.db & BookDB.
        return None


class GenericBookReader(BaseReader):
    """Reader for FB2, CBZ, CBR, DJVU, TXT, RTF, DOC, DOCX, and LIT files."""

    def read_metadata(self) -> BookMetadata:
        ext = self.file_path.suffix.lower()
        title: Optional[str] = None
        authors: List[str] = []
        description: Optional[str] = None
        isbn13: Optional[str] = None
        subjects: List[str] = []

        if ext == ".fb2":
            try:
                with open(self.file_path, "rb") as fp:
                    head = fp.read(65536).decode("utf-8", errors="replace")
                m_t = re.search(r"<book-title[^>]*>(.*?)</book-title>", head, re.I | re.S)
                if m_t:
                    title = re.sub(r"<[^>]+>", "", m_t.group(1)).strip()
                for m_a in re.finditer(r"<author>(.*?)</author>", head, re.I | re.S):
                    ab = m_a.group(1)
                    fn = re.search(r"<first-name[^>]*>(.*?)</first-name>", ab, re.I)
                    ln = re.search(r"<last-name[^>]*>(.*?)</last-name>", ab, re.I)
                    full = " ".join(p.group(1).strip() for p in (fn, ln) if p and p.group(1).strip())
                    if full and full not in authors:
                        authors.append(full)
                m_d = re.search(r"<annotation[^>]*>(.*?)</annotation>", head, re.I | re.S)
                if m_d:
                    description = re.sub(r"<[^>]+>", " ", m_d.group(1)).strip()
                m_i = re.search(r"<isbn[^>]*>(.*?)</isbn>", head, re.I | re.S)
                if m_i:
                    i13, _ = normalize_isbn(m_i.group(1))
                    if i13:
                        isbn13 = i13
            except Exception:
                pass

        clues = parse_filename(self.file_path)
        title = _strip_format_suffix(clean_book_title(title) or title or clues.get("title"))
        if not authors and clues.get("authors"):
            authors = [a for a in clues["authors"] if a and a.lower() not in ("unknown", "unknown author")]
        if not isbn13 and clues.get("isbn"):
            isbn13 = clues["isbn"]

        return BookMetadata(
            title=title,
            authors=authors,
            description=description,
            subjects=subjects,
            isbn13=isbn13,
            source=f"embedded-{ext.lstrip('.')}",
        )

    def extract_front_text(self, max_pages: int = 15) -> str:
        ext = self.file_path.suffix.lower()
        if ext in (".txt", ".fb2", ".rtf"):
            try:
                with open(self.file_path, "rb") as fp:
                    raw = fp.read(32768).decode("utf-8", errors="replace")
                return re.sub(r"<[^>]+>", " ", raw)
            except Exception:
                return ""
        return ""

    def extract_front_images(self, max_pages: int = 3) -> List[bytes]:
        cov = self.extract_cover_bytes()
        return [cov] if cov else []

    def extract_cover_bytes(self) -> Optional[bytes]:
        ext = self.file_path.suffix.lower()
        if ext == ".cbz":
            try:
                with zipfile.ZipFile(self.file_path, "r") as zf:
                    img_names = sorted(
                        n for n in zf.namelist()
                        if n.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
                    )
                    if img_names:
                        return zf.read(img_names[0])
            except Exception:
                pass
        return None

    def write_metadata(
        self,
        metadata: BookMetadata,
        backup: bool = False,
        update_cover: bool = False,
        backup_path: Optional[Path] = None,
    ) -> None:
        return None
