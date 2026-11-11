"""Surgical EPUB metadata reader and writer.

Directly updates the OPF package document inside the EPUB zip without re-compressing
or reformatting any HTML, CSS, fonts, or image files. Preserves the strict EPUB specification:
- 'mimetype' written first with ZIP_STORED (uncompressed).
- XML namespaces and internal archive structure preserved.
"""

from __future__ import annotations
import logging
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

from lxml import etree

from ..models import BookMetadata
from ..isbn.validator import normalize_isbn, clean_isbn
from .base import BaseReader

logger = logging.getLogger(__name__)

# Standard XML Namespaces used in EPUB OPF
NS_CONTAINER = "urn:oasis:names:tc:opendocument:xmlns:container"
NS_OPF = "http://www.idpf.org/2007/opf"
NS_DC = "http://purl.org/dc/elements/1.1/"

NSMAP = {
    "opf": NS_OPF,
    "dc": NS_DC,
}


def _read_zip_member_lenient(z: zipfile.ZipFile, name_or_info) -> bytes:
    """Read a member from a ZipFile while ignoring bad CRC-32 checksums in legacy EPUBs."""
    try:
        info = z.getinfo(name_or_info) if isinstance(name_or_info, str) else name_or_info
        if getattr(info, "header_offset", 0) < 0:
            raise zipfile.BadZipFile(
                f"Truncated zip member {info.filename} (negative offset {info.header_offset})"
            )
        with z.open(info, "r") as zf:
            zf._expected_crc = None
            return zf.read()
    except OSError as e:
        raise zipfile.BadZipFile(f"Truncated or unreadable zip member ({e})") from e


def _strip_html(html_bytes: bytes) -> str:
    """Extract plain text from XHTML/HTML content."""
    try:
        parser = etree.HTMLParser(encoding="utf-8")
        tree = etree.fromstring(html_bytes, parser)
        # Drop script and style elements
        for el in tree.xpath("//script|//style"):
            el.getparent().remove(el)
        text = " ".join(tree.itertext())
        return re.sub(r"\s+", " ", text).strip()
    except Exception:
        # Fallback to regex tag stripping
        text = re.sub(r"<[^>]+>", " ", html_bytes.decode("utf-8", errors="ignore"))
        return re.sub(r"\s+", " ", text).strip()


class EpubReader(BaseReader):
    """Reader and in-place surgical writer for EPUB (.epub) files."""

    DRM_INDICATORS = {
        "META-INF/rights.xml",
        "META-INF/license.lcpl",
    }

    def __init__(self, file_path: Path):
        super().__init__(file_path)
        from ..exceptions import DrmProtectedError, InvalidDocumentError
        if not zipfile.is_zipfile(self.file_path):
            raise InvalidDocumentError(f"File is not a valid zip/EPUB archive: {self.file_path.name}")
        try:
            with zipfile.ZipFile(self.file_path, "r") as z:
                names = set(z.namelist())
                if any(ind in names for ind in self.DRM_INDICATORS):
                    raise DrmProtectedError(f"EPUB contains DRM protection files: {self.file_path.name}")
                if "META-INF/encryption.xml" in names:
                    try:
                        enc_bytes = _read_zip_member_lenient(z, "META-INF/encryption.xml")
                        # Font obfuscation is standard; only flag DRM if HTML/XHTML/OPF content is encrypted
                        if b".xhtml" in enc_bytes or b".html" in enc_bytes or b"http://www.w3.org/2001/04/xmlenc#Element" in enc_bytes:
                            raise DrmProtectedError(f"EPUB content is DRM-encrypted: {self.file_path.name}")
                    except DrmProtectedError:
                        raise
                    except Exception:
                        pass
        except (DrmProtectedError, InvalidDocumentError):
            raise
        except zipfile.BadZipFile as e:
            raise InvalidDocumentError(f"Corrupt EPUB zip archive ({e}): {self.file_path.name}")


    def _get_opf_path(self, z: zipfile.ZipFile) -> str:
        """Find the relative path to the OPF file from META-INF/container.xml."""
        from ..exceptions import InvalidDocumentError
        try:
            container_xml = _read_zip_member_lenient(z, "META-INF/container.xml")
            tree = etree.fromstring(container_xml)
            rootfile = tree.find(f".//{{{NS_CONTAINER}}}rootfile")
            if rootfile is not None and "full-path" in rootfile.attrib:
                return rootfile.attrib["full-path"]
        except Exception as e:
            logger.debug("Failed to parse container.xml: %s", e)

        # Fallback: search for any .opf file in the zip
        for name in z.namelist():
            if name.lower().endswith(".opf"):
                return name
        raise InvalidDocumentError(f"Could not locate OPF package document in {self.file_path.name}")

    def _read_ncx_fallback_metadata(self, z: zipfile.ZipFile) -> BookMetadata:
        """Fallback metadata extraction from toc.ncx when the OPF member is missing or truncated."""
        title = None
        authors: List[str] = []
        for info in z.infolist():
            if getattr(info, "header_offset", 0) < 0 or not info.filename.lower().endswith(".ncx"):
                continue
            try:
                ncx_bytes = _read_zip_member_lenient(z, info)
                parser = etree.XMLParser(recover=True, encoding="utf-8")
                root = etree.fromstring(ncx_bytes, parser)
                if root is None:
                    continue
                for el in root.iter():
                    if not isinstance(el.tag, str):
                        continue
                    local = el.tag.split("}")[-1].lower()
                    if local == "doctitle" and not title:
                        txt = " ".join(t.strip() for t in el.itertext() if t and t.strip()).strip()
                        if txt and not re.match(r"^untitled\b", txt, re.IGNORECASE):
                            title = txt
                    elif local == "docauthor":
                        txt = " ".join(t.strip() for t in el.itertext() if t and t.strip()).strip()
                        if txt and txt.lower() not in ("unknown", "unknown author") and txt not in authors:
                            authors.append(txt)
                if title or authors:
                    break
            except Exception:
                continue
        return BookMetadata(title=title, authors=authors, source="epub-embedded")

    def read_metadata(self) -> BookMetadata:
        """Read embedded metadata from the EPUB OPF document (or fallback to toc.ncx if OPF is truncated)."""
        opf_bytes = None
        with zipfile.ZipFile(self.file_path, "r") as z:
            try:
                opf_path = self._get_opf_path(z)
                opf_bytes = _read_zip_member_lenient(z, opf_path)
            except Exception as e:
                logger.debug("OPF unreadable in %s (%s); trying NCX fallback", self.file_path.name, e)
                return self._read_ncx_fallback_metadata(z)

            parser = etree.XMLParser(recover=True, encoding="utf-8")
            root = etree.fromstring(opf_bytes, parser) if opf_bytes else None
            if root is None:
                return self._read_ncx_fallback_metadata(z)

        metadata_el = root.find(f"{{{NS_OPF}}}metadata")
        if metadata_el is None:
            metadata_el = root.find("metadata")
        if metadata_el is None:
            return BookMetadata(source="epub-embedded")

        def get_all_text(tag: str) -> List[str]:
            elements = metadata_el.findall(f"{{{NS_DC}}}{tag}")
            if not elements:
                elements = metadata_el.findall(f"dc:{tag}", namespaces={"dc": NS_DC})
            if not elements:
                elements = [el for el in metadata_el if isinstance(el.tag, str) and el.tag.endswith(tag)]
            return [el.text.strip() for el in elements if el.text and el.text.strip()]

        def get_single_text(tag: str) -> Optional[str]:
            vals = get_all_text(tag)
            return vals[0] if vals else None

        raw_title = get_single_text("title")
        title = raw_title
        if title and re.match(r"^(?:untitled(?:\s+book)?(?:\s+\d+)?|upload|unknown(?:\s+title|\s+book|\s+author)?)$", title.strip(), re.IGNORECASE):
            title = None
        authors = get_all_text("creator")
        if raw_title and raw_title.strip().lower() == "upload" and len(authors) == 1 and " " not in authors[0].strip():
            # Broken uploader metadata where <dc:title> is 'upload' and <dc:creator> is the single-word book title
            if not title:
                title = authors[0].strip()
            authors = []
        description = get_single_text("description")
        subjects = get_all_text("subject")
        publisher = get_single_text("publisher")
        published_date = get_single_text("date")
        if published_date and re.match(r"^0[01]\d\d\b", published_date.strip()):
            published_date = None
        language = get_single_text("language")

        # Find ISBN and Amazon ASIN in identifiers
        identifiers = get_all_text("identifier")
        isbn13 = None
        isbn10 = None
        asin = None
        for ident in identifiers:
            i13, i10 = normalize_isbn(ident)
            if i13 and not isbn13:
                isbn13 = i13
                isbn10 = i10
            m_asin = re.search(r"(?:urn:asin:|asin:|amazon:)?\b(B[0-9A-Z]{9})\b", ident, re.IGNORECASE)
            if m_asin and not asin:
                asin = m_asin.group(1).upper()

        if not asin:
            for el in metadata_el:
                scheme = (el.get(f"{{{NS_OPF}}}scheme") or el.get("scheme") or el.get("name") or "").lower()
                if scheme in ("amazon", "asin", "mobi-asin"):
                    val = (el.text or el.get("content") or "").strip().upper()
                    if re.match(r"^[A-Z0-9]{10}$", val):
                        asin = val
                        break

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
            source="epub-embedded",
        )

    def extract_front_text(self, max_pages: int = 15) -> str:
        """Extract text from the first several spine items in the EPUB (falling back to readable members if OPF is truncated)."""
        with zipfile.ZipFile(self.file_path, "r") as z:
            root = None
            opf_dir = ""
            try:
                opf_path = self._get_opf_path(z)
                opf_dir = os.path.dirname(opf_path)
                opf_bytes = _read_zip_member_lenient(z, opf_path)
                parser = etree.XMLParser(recover=True, encoding="utf-8")
                root = etree.fromstring(opf_bytes, parser) if opf_bytes else None
            except Exception as e:
                logger.debug("OPF unreadable during extract_front_text for %s: %s", self.file_path.name, e)

            texts = []
            promo_exclude_kw = ("also", "other_books", "otherbooks", "teaser", "excerpt", "sample", "adcard", "promo", "backlist")
            keywords = ("copy", "colophon", "imprint", "legal", "title", "edition", "about", "rights")

            if root is not None:
                manifest = {}
                for item in root.findall(f".//{{{NS_OPF}}}item"):
                    item_id = item.get("id")
                    href = item.get("href")
                    media_type = item.get("media-type") or ""
                    if item_id and href:
                        full_href = os.path.normpath(os.path.join(opf_dir, href)).replace("\\", "/")
                        manifest[item_id] = (full_href, media_type)

                spine_ids = []
                for itemref in root.findall(f".//{{{NS_OPF}}}itemref"):
                    idref = itemref.get("idref")
                    if idref:
                        spine_ids.append(idref)

                selected_sids = []
                for sid in spine_ids[:max_pages]:
                    if sid not in selected_sids:
                        href_low = manifest[sid][0].lower() if sid in manifest else ""
                        if not any(pk in href_low or pk in sid.lower() for pk in promo_exclude_kw):
                            selected_sids.append(sid)

                for sid in spine_ids[-6:]:
                    if sid not in selected_sids:
                        href_low = manifest[sid][0].lower() if sid in manifest else ""
                        if not any(pk in href_low or pk in sid.lower() for pk in promo_exclude_kw):
                            selected_sids.append(sid)

                for sid in spine_ids:
                    if sid in manifest:
                        href = manifest[sid][0].lower()
                        if any(k in href or k in sid.lower() for k in keywords) and not any(pk in href or pk in sid.lower() for pk in promo_exclude_kw):
                            if sid not in selected_sids:
                                selected_sids.append(sid)

                namelist_set = set(z.namelist())
                for sid in selected_sids:
                    if sid in manifest:
                        href, mtype = manifest[sid]
                        if href in namelist_set and ("xhtml" in mtype or "html" in mtype or "xml" in mtype):
                            try:
                                content = _read_zip_member_lenient(z, href)
                                plain = _strip_html(content)
                                if plain:
                                    head_low = plain.strip()[:120].lower()
                                    if re.match(r"^(?:also\s+by|other\s+books\s+by|more\s+from|books\s+by)\b", head_low):
                                        continue
                                    texts.append(plain)
                            except Exception as e:
                                logger.debug("Failed reading spine item %s: %s", href, e)

            # Fallback if OPF was truncated or all selected spine items were in a truncated portion of the archive
            if not texts:
                readable_infos = [
                    info for info in z.infolist()
                    if getattr(info, "header_offset", 0) >= 0
                    and info.filename.lower().endswith((".xhtml", ".html", ".htm", ".ncx"))
                    and not any(pk in info.filename.lower() for pk in promo_exclude_kw)
                ]
                priority_infos = [
                    info for info in readable_infos
                    if any(k in info.filename.lower() for k in keywords + ("toc",))
                ]
                other_infos = [info for info in readable_infos if info not in priority_infos]
                cand_infos = priority_infos + other_infos[:max_pages] + other_infos[-4:]
                seen_names = set()
                for info in cand_infos:
                    if info.filename in seen_names:
                        continue
                    seen_names.add(info.filename)
                    try:
                        content = _read_zip_member_lenient(z, info)
                        plain = _strip_html(content)
                        if plain:
                            texts.append(plain)
                    except Exception:
                        continue

            return "\n\n".join(texts)

    def extract_front_images(self, max_pages: int = 3) -> List[bytes]:
        """EPUB files generally contain structured text, but cover image can be extracted if needed."""
        from urllib.parse import unquote
        images: List[bytes] = []
        with zipfile.ZipFile(self.file_path, "r") as z:
            namelist = set(z.namelist())
            try:
                opf_path = self._get_opf_path(z)
                opf_dir = os.path.dirname(opf_path)
                opf_bytes = _read_zip_member_lenient(z, opf_path)
                tree = etree.fromstring(opf_bytes, etree.XMLParser(recover=True))

                cover_id = None
                for meta in tree.findall(f".//{{{NS_OPF}}}meta"):
                    if meta.get("name") == "cover" and meta.get("content"):
                        cover_id = meta.get("content")
                        break

                primary_candidates: List[str] = []
                fallback_candidates: List[str] = []
                for item in tree.findall(f".//{{{NS_OPF}}}item"):
                    props = item.get("properties", "")
                    item_id = item.get("id", "")
                    media_type = item.get("media-type", "").lower()
                    href = item.get("href", "")
                    if not href:
                        continue
                    if (
                        (cover_id and item_id == cover_id)
                        or "cover-image" in props
                        or item_id.lower() in ("cover", "cover-image")
                    ):
                        primary_candidates.append(href)
                    elif "image" in media_type and "cover" in href.lower():
                        fallback_candidates.append(href)

                candidates = primary_candidates if primary_candidates else fallback_candidates

                for raw_href in candidates:
                    rel_path = unquote(raw_href)
                    full_href = os.path.normpath(os.path.join(opf_dir, rel_path)).replace("\\", "/") if opf_dir else rel_path
                    for cand_path in (full_href, full_href.lstrip("/"), rel_path):
                        if cand_path in namelist:
                            images.append(_read_zip_member_lenient(z, cand_path))
                            break
                    if len(images) >= max_pages:
                        break
            except Exception:
                pass
        return images

    def write_metadata(
        self,
        metadata: BookMetadata,
        backup: bool = False,
        update_cover: bool = False,
        backup_path: Optional[Path] = None,
    ) -> None:
        """
        Surgically update OPF metadata inside the EPUB zip.
        Ensures 'mimetype' is first and uncompressed.
        Preserves all other files untouched.
        """
        temp_fd, temp_path_str = tempfile.mkstemp(
            prefix=".epubmeta_tmp_",
            suffix=".tmp",
            dir=self.file_path.parent,
        )
        os.close(temp_fd)
        temp_path = Path(temp_path_str)

        try:
            with zipfile.ZipFile(self.file_path, "r") as src_zip:
                opf_path = self._get_opf_path(src_zip)
                opf_dir = os.path.dirname(opf_path)
                opf_bytes = _read_zip_member_lenient(src_zip, opf_path)

                # Parse and update OPF XML
                parser = etree.XMLParser(remove_blank_text=False, recover=True, encoding="utf-8")
                root = etree.fromstring(opf_bytes, parser)

                metadata_el = root.find(f"{{{NS_OPF}}}metadata")
                if metadata_el is None:
                    metadata_el = root.find("metadata")
                if metadata_el is None:
                    # Create metadata node if missing
                    metadata_el = etree.SubElement(root, f"{{{NS_OPF}}}metadata")

                # Helper to set or update single element
                def set_single_elem(tag: str, value: Optional[str]):
                    if not value:
                        return
                    elem = metadata_el.find(f"{{{NS_DC}}}{tag}")
                    if elem is None:
                        elem = etree.SubElement(metadata_el, f"{{{NS_DC}}}{tag}")
                    elem.text = str(value)

                # Title
                if metadata.title:
                    set_single_elem("title", metadata.title)

                # Authors
                if metadata.authors:
                    for old_c in metadata_el.findall(f"{{{NS_DC}}}creator"):
                        metadata_el.remove(old_c)
                    from ..authors import split_and_normalize_authors
                    for display_name, file_as in split_and_normalize_authors(metadata.authors):
                        attribs = {f"{{{NS_OPF}}}role": "aut"}
                        if file_as:
                            attribs[f"{{{NS_OPF}}}file-as"] = file_as
                        c_elem = etree.SubElement(
                            metadata_el,
                            f"{{{NS_DC}}}creator",
                            attrib=attribs,
                        )
                        c_elem.text = display_name

                # Description
                if metadata.description:
                    set_single_elem("description", metadata.description)

                # Publisher
                if metadata.publisher:
                    set_single_elem("publisher", metadata.publisher)

                # Date
                if metadata.published_date:
                    set_single_elem("date", metadata.published_date)

                # Language
                if metadata.language:
                    set_single_elem("language", metadata.language)

                # Subjects / Tags
                if metadata.subjects:
                    for old_s in metadata_el.findall(f"{{{NS_DC}}}subject"):
                        metadata_el.remove(old_s)
                    for subject in metadata.subjects:
                        s_elem = etree.SubElement(metadata_el, f"{{{NS_DC}}}subject")
                        s_elem.text = subject

                # ISBN Identifier
                if metadata.isbn13:
                    # Check if an ISBN identifier already exists
                    isbn_found = False
                    for ident in metadata_el.findall(f"{{{NS_DC}}}identifier"):
                        scheme = ident.get(f"{{{NS_OPF}}}scheme", "")
                        if scheme.upper() == "ISBN" or "isbn" in (ident.text or "").lower():
                            ident.text = f"urn:isbn:{metadata.isbn13}"
                            ident.set(f"{{{NS_OPF}}}scheme", "ISBN")
                            isbn_found = True
                            break
                    if not isbn_found:
                        ident = etree.SubElement(
                            metadata_el,
                            f"{{{NS_DC}}}identifier",
                            attrib={
                                "id": "isbn",
                                f"{{{NS_OPF}}}scheme": "ISBN",
                            },
                        )
                        ident.text = f"urn:isbn:{metadata.isbn13}"

                # Cover Image handling
                manifest_el = root.find(f"{{{NS_OPF}}}manifest")
                if manifest_el is None:
                    manifest_el = root.find("manifest")

                existing_cover_full_path = None
                if manifest_el is not None:
                    for item in manifest_el.findall(f".//{{{NS_OPF}}}item"):
                        props = item.get("properties", "")
                        item_id = item.get("id", "").lower()
                        media_type = item.get("media-type", "").lower()
                        href = item.get("href", "")
                        if "cover-image" in props or (("cover" in item_id or "cover" in href.lower()) and media_type.startswith("image/")):
                            existing_cover_full_path = os.path.normpath(os.path.join(opf_dir, href)).replace("\\", "/")
                            break

                should_set_cover = False
                new_cover_full_path = None

                if metadata.cover_bytes:
                    if existing_cover_full_path:
                        if update_cover:
                            should_set_cover = True
                            new_cover_full_path = existing_cover_full_path
                            logger.info("Replacing existing EPUB cover image: %s", existing_cover_full_path)
                    else:
                        should_set_cover = True
                        rel_cover_href = "cover.jpg"
                        new_cover_full_path = os.path.normpath(os.path.join(opf_dir, rel_cover_href)).replace("\\", "/")
                        logger.info("Adding new EPUB cover image to manifest: %s", rel_cover_href)
                        if manifest_el is not None:
                            etree.SubElement(
                                manifest_el,
                                f"{{{NS_OPF}}}item",
                                attrib={
                                    "id": "cover-image",
                                    "href": rel_cover_href,
                                    "media-type": "image/jpeg",
                                    "properties": "cover-image",
                                },
                            )
                        # Add <meta name="cover" content="cover-image"/> for EPUB 2
                        etree.SubElement(
                            metadata_el,
                            "meta",
                            attrib={"name": "cover", "content": "cover-image"},
                        )

                updated_opf_bytes = etree.tostring(
                    root,
                    encoding="utf-8",
                    xml_declaration=True,
                    pretty_print=True,
                )

                # Write out new EPUB zip
                with zipfile.ZipFile(temp_path, "w") as dst_zip:
                    # 1. mimetype MUST be the very first file, uncompressed (ZIP_STORED)
                    if "mimetype" in src_zip.namelist():
                        try:
                            mimetype_content = _read_zip_member_lenient(src_zip, "mimetype")
                        except Exception:
                            mimetype_content = b"application/epub+zip"
                    else:
                        mimetype_content = b"application/epub+zip"

                    dst_zip.writestr(
                        "mimetype",
                        mimetype_content,
                        compress_type=zipfile.ZIP_STORED,
                    )

                    # 2. Write all other entries
                    cover_written = False
                    for info in src_zip.infolist():
                        if info.filename == "mimetype":
                            continue
                        if info.filename == opf_path:
                            dst_zip.writestr(
                                info,
                                updated_opf_bytes,
                                compress_type=zipfile.ZIP_DEFLATED,
                            )
                        elif should_set_cover and info.filename == new_cover_full_path:
                            dst_zip.writestr(
                                info,
                                metadata.cover_bytes,
                                compress_type=zipfile.ZIP_DEFLATED,
                            )
                            cover_written = True
                        else:
                            data = _read_zip_member_lenient(src_zip, info)
                            dst_zip.writestr(info, data, compress_type=info.compress_type)

                    if should_set_cover and not cover_written and new_cover_full_path:
                        dst_zip.writestr(
                            new_cover_full_path,
                            metadata.cover_bytes,
                            compress_type=zipfile.ZIP_DEFLATED,
                        )

            from ..file_utils import (
                get_file_timestamps,
                preserve_file_ownership_and_mode,
                restore_file_timestamps,
            )
            orig_timestamps = get_file_timestamps(self.file_path)

            # If backup is requested, create backup in backup_path (mirrored tree) or bak/ subdirectory
            if backup:
                bak_dest = backup_path or (self.file_path.parent / "bak" / self.file_path.name)
                bak_dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.file_path, bak_dest)
                restore_file_timestamps(bak_dest, orig_timestamps)
                logger.info("Backup created: %s", bak_dest)

            # Atomically replace destination file while preserving NAS/POSIX file permissions and uid/gid
            preserve_file_ownership_and_mode(self.file_path, temp_path)
            shutil.move(str(temp_path), str(self.file_path))
            restore_file_timestamps(self.file_path, orig_timestamps)
            logger.info("Successfully updated EPUB metadata (preserved timestamps): %s", self.file_path)

        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass
