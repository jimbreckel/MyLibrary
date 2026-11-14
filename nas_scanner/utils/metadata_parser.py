import os
import hashlib
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import unquote
import warnings
import logging

# Suppress spammy pypdf warnings and logs to keep the log file clean and fast
warnings.filterwarnings("ignore", category=UserWarning, module="pypdf")
warnings.filterwarnings("ignore", message=".*wrong pointing object.*")
logging.getLogger("pypdf").setLevel(logging.ERROR)

# Namespaces for EPUB metadata
NAMESPACES = {
    'container': 'urn:oasis:names:tc:opendocument:xmlns:container',
    'opf': 'http://www.idpf.org/2007/opf',
    'dc': 'http://purl.org/dc/elements/1.1/'
}


def _apply_epubmeta_fallbacks(filepath, meta):
    """Enrich missing ISBN/ASIN, title, or Unknown Author using epubmeta's filename parser and ISBN validator."""
    try:
        import re
        from epubmeta.filename_parser import parse_filename, clean_book_title
        from epubmeta.isbn.validator import normalize_isbn

        raw_isbn = meta.get('isbn')
        if raw_isbn:
            m_asin = re.search(r"^(?:urn:asin:|asin:|amazon:)?(B[0-9A-Z]{9})$", str(raw_isbn).strip(), re.IGNORECASE)
            if m_asin:
                meta['asin'] = m_asin.group(1).upper()
                meta['isbn'] = ""
            else:
                i13, i10 = normalize_isbn(raw_isbn)
                if i13 or i10:
                    meta['isbn'] = i13 or i10

        clues = parse_filename(os.path.basename(filepath))
        if not meta.get('isbn') and clues.get('isbn'):
            meta['isbn'] = clues['isbn']
        if (not meta.get('authors') or meta.get('authors') in ('Unknown', 'Unknown Author')) and clues.get('authors'):
            meta['authors'] = ", ".join(clues['authors'])
        stem = os.path.splitext(os.path.basename(filepath))[0]
        if (not meta.get('title') or meta.get('title') == stem) and clues.get('title'):
            meta['title'] = clues['title']
        elif meta.get('title'):
            meta['title'] = clean_book_title(meta['title']) or meta['title']
    except Exception:
        pass
    return meta


def get_file_hash(filepath):
    """Generate SHA-256 hash of a file for change tracking."""
    hasher = hashlib.sha256()
    try:
        with open(filepath, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception:
        return ""


def get_file_hash_for_dir(filepath):
    """Generate a stable SHA-256 hash for a directory based only on its path string."""
    hasher = hashlib.sha256()
    try:
        hasher.update(os.path.abspath(filepath).encode('utf-8'))
        return hasher.hexdigest()
    except Exception:
        return ""


def _extract_epub_cover_from_zip(epub, opf_root, opf_dir, namelist_set):
    """Extract cover image bytes from an already-open EPUB ZipFile and parsed OPF root."""
    try:
        cover_id = None
        metadata = opf_root.find('.//opf:metadata', NAMESPACES)
        if metadata is not None:
            for meta in metadata.findall('.//opf:meta', NAMESPACES):
                if meta.attrib.get('name') == 'cover':
                    cover_id = meta.attrib.get('content')
                    break

        manifest = opf_root.find('.//opf:manifest', NAMESPACES)
        cover_href = None
        if manifest is not None:
            if cover_id:
                for item in manifest.findall('.//opf:item', NAMESPACES):
                    if item.attrib.get('id') == cover_id:
                        cover_href = item.attrib.get('href')
                        break
            if not cover_href:
                for item in manifest.findall('.//opf:item', NAMESPACES):
                    properties = item.attrib.get('properties', '')
                    item_id = item.attrib.get('id', '')
                    if 'cover-image' in properties or item_id in ('cover', 'cover-image'):
                        cover_href = item.attrib.get('href')
                        break
            if not cover_href:
                for item in manifest.findall('.//opf:item', NAMESPACES):
                    media_type = item.attrib.get('media-type', '')
                    href = item.attrib.get('href', '').lower()
                    if 'image' in media_type and 'cover' in href:
                        cover_href = item.attrib.get('href')
                        break

        if cover_href:
            rel_path = unquote(cover_href)
            if opf_dir:
                full_img_path = os.path.normpath(os.path.join(opf_dir, rel_path)).replace('\\', '/')
            else:
                full_img_path = rel_path

            for path_cand in (full_img_path, full_img_path.lstrip('/'), rel_path):
                if path_cand in namelist_set:
                    return epub.read(path_cand)
    except Exception:
        pass
    return None


def parse_epub(filepath, include_cover=False):
    """Extract metadata (and optionally cover bytes in the same zip open) from an EPUB file."""
    has_drm = False
    is_invalid = False
    cover_bytes = None

    try:
        if not zipfile.is_zipfile(filepath):
            res = {'is_invalid': True, 'has_drm': False}
            return (res, None) if include_cover else res

        with zipfile.ZipFile(filepath, 'r') as epub:
            namelist = epub.namelist()
            namelist_set = set(namelist)
            drm_indicators = {
                'META-INF/rights.xml',
                'META-INF/encryption.xml',
                'META-INF/license.lcpl'
            }
            if any(indicator in namelist_set for indicator in drm_indicators):
                has_drm = True

            try:
                container_data = epub.read('META-INF/container.xml')
                root = ET.fromstring(container_data)
                rootfile = root.find('.//container:rootfile', NAMESPACES)
                if rootfile is None:
                    res = {'is_invalid': True, 'has_drm': has_drm}
                    return (res, None) if include_cover else res

                opf_path = rootfile.attrib.get('full-path')
                if not opf_path:
                    res = {'is_invalid': True, 'has_drm': has_drm}
                    return (res, None) if include_cover else res

                opf_dir = os.path.dirname(opf_path)
                opf_data = epub.read(opf_path)
                opf_root = ET.fromstring(opf_data)

                metadata = opf_root.find('.//opf:metadata', NAMESPACES)
                if metadata is None:
                    res = {'is_invalid': True, 'has_drm': has_drm}
                    return (res, None) if include_cover else res

                def get_meta_field(name):
                    elements = metadata.findall(f'.//dc:{name}', NAMESPACES)
                    return ", ".join([el.text.strip() for el in elements if el.text and el.text.strip()]) if elements else ""

                title = get_meta_field('title').strip()
                authors = get_meta_field('creator').strip()
                publisher = get_meta_field('publisher').strip()
                description = get_meta_field('description').strip()
                pub_date = get_meta_field('date').strip()
                isbn = ""
                asin = ""
                import re

                identifiers = metadata.findall('.//dc:identifier', NAMESPACES)
                for idx in identifiers:
                    scheme = idx.attrib.get('{http://www.idpf.org/2007/opf}scheme', '').lower()
                    txt = (idx.text or '').strip()
                    if not txt:
                        continue
                    m_asin = re.search(r'(?:urn:asin:|asin:|amazon:)?\b(B[0-9A-Z]{9})\b', txt, re.IGNORECASE)
                    if m_asin and not asin:
                        asin = m_asin.group(1).upper()
                    elif scheme in ('amazon', 'asin', 'mobi-asin') and re.match(r'^[A-Z0-9]{10}$', txt, re.IGNORECASE):
                        asin = txt.upper()
                    elif ('isbn' in scheme or (txt.replace('-', '').isdigit() and len(txt.replace('-', '')) in (10, 13))) and not isbn:
                        isbn = txt

                if include_cover:
                    cover_bytes = _extract_epub_cover_from_zip(epub, opf_root, opf_dir, namelist_set)

                res = _apply_epubmeta_fallbacks(filepath, {
                    'title': title or os.path.splitext(os.path.basename(filepath))[0],
                    'authors': authors or 'Unknown Author',
                    'publisher': publisher,
                    'description': description,
                    'pub_date': pub_date[:10] if pub_date else None,
                    'isbn': isbn,
                    'asin': asin or None,
                    'has_drm': has_drm,
                    'is_invalid': False
                })
                return (res, cover_bytes) if include_cover else res
            except Exception:
                res = {
                    'title': os.path.splitext(os.path.basename(filepath))[0],
                    'authors': 'Unknown Author',
                    'has_drm': has_drm,
                    'is_invalid': True
                }
                return (res, None) if include_cover else res
    except Exception as e:
        print(f"Error parsing EPUB {filepath}: {e}")
        res = {'is_invalid': True, 'has_drm': False}
        return (res, None) if include_cover else res


def parse_pdf(filepath, include_cover=False):
    """Extract metadata (and optionally cover bytes in the same PDF open) from a PDF file."""
    has_drm = False
    is_invalid = False
    cover_bytes = None

    # Fast, hang-proof path via PyMuPDF (fitz) — avoids pypdf 63.8M-pixel XObject decompression bombs
    try:
        import pymupdf
        doc = pymupdf.open(filepath)
        try:
            needs_pass = bool(getattr(doc, 'needs_pass', False))
            if doc.is_encrypted or needs_pass:
                has_drm = True
            meta = doc.metadata or {}
            title = str(meta.get('title') or '').strip()
            author = str(meta.get('author') or '').strip()
            publisher = str(meta.get('producer') or meta.get('creator') or '').strip()
            description = str(meta.get('subject') or '').strip()
            keywords = str(meta.get('keywords') or '').strip()

            if not title:
                title = os.path.splitext(os.path.basename(filepath))[0]
            if not author:
                author = 'Unknown Author'

            isbn = ''
            try:
                from epubmeta.isbn.extractor import extract_isbns_from_text
                corpus = f"{keywords} {description} {title}"
                if not needs_pass and len(doc) > 0:
                    for p_idx in range(min(4, len(doc))):
                        corpus += ' ' + (doc[p_idx].get_text() or '')[:4000]
                found_isbns = extract_isbns_from_text(corpus)
                if found_isbns:
                    isbn = found_isbns[0][0]
            except Exception:
                pass

            if include_cover and not needs_pass and len(doc) > 0:
                try:
                    pix = doc[0].get_pixmap(dpi=72)
                    cover_bytes = pix.tobytes('jpeg')
                except Exception:
                    pass

            res = _apply_epubmeta_fallbacks(filepath, {
                'title': title,
                'authors': author,
                'publisher': publisher,
                'description': description,
                'isbn': isbn,
                'pub_date': None,
                'has_drm': has_drm,
                'is_invalid': False,
            })
            return (res, cover_bytes) if include_cover else res
        finally:
            doc.close()
    except ImportError:
        pass
    except Exception as fitz_err:
        err_low = str(fitz_err).lower()
        if 'encrypted' in err_low or 'password' in err_low:
            res = {
                'title': os.path.splitext(os.path.basename(filepath))[0],
                'authors': 'Unknown Author',
                'has_drm': True,
                'is_invalid': False,
            }
            return (res, None) if include_cover else res

    try:
        import pypdf
        with open(filepath, 'rb') as f:
            reader = pypdf.PdfReader(f)

            if reader.is_encrypted:
                has_drm = True
                try:
                    info = reader.metadata
                except Exception:
                    res = {
                        'title': os.path.splitext(os.path.basename(filepath))[0],
                        'authors': 'Unknown Author',
                        'has_drm': True,
                        'is_invalid': False
                    }
                    return (res, None) if include_cover else res
            else:
                info = reader.metadata

            title = str(info.title or "").strip() if info and getattr(info, 'title', None) else ""
            author = str(info.author or "").strip() if info and getattr(info, 'author', None) else ""
            publisher = str(info.get('/Publisher', '') or "").strip() if info else ''
            description = str(info.get('/Subject', '') or "").strip() if info else ''
            keywords = str(info.get('/Keywords', '') or "").strip() if info else ''

            if not title or not title.strip():
                title = os.path.splitext(os.path.basename(filepath))[0]
            if not author or not author.strip():
                author = "Unknown Author"

            isbn = ""
            try:
                from epubmeta.isbn.extractor import extract_isbns_from_text
                corpus = f"{keywords} {description} {title}"
                found_isbns = extract_isbns_from_text(corpus)
                if found_isbns:
                    isbn = found_isbns[0][0]
            except Exception:
                pass

            res = _apply_epubmeta_fallbacks(filepath, {
                'title': title,
                'authors': author,
                'publisher': publisher,
                'description': description,
                'isbn': isbn,
                'pub_date': None,
                'has_drm': has_drm,
                'is_invalid': False
            })
            return (res, cover_bytes) if include_cover else res
    except Exception as e:
        print(f"Error parsing PDF {filepath}: {e}")
        error_msg = str(e).lower()
        if "encrypted" in error_msg or "password" in error_msg or "cryptography" in error_msg or "aes" in error_msg:
            has_drm = True
            is_invalid = False
        else:
            is_invalid = True

        res = {
            'title': os.path.splitext(os.path.basename(filepath))[0],
            'authors': 'Unknown Author',
            'has_drm': has_drm,
            'is_invalid': is_invalid
        }
        return (res, None) if include_cover else res


def parse_mobi(filepath):
    """Extract metadata from a MOBI/AZW3 file using fast header-only MobiReader."""
    has_drm = False
    is_invalid = False

    try:
        from pathlib import Path
        from epubmeta.readers.mobi import MobiReader
        reader = MobiReader(Path(filepath))
        mmeta = reader.read_metadata()
        title = (mmeta.title or '').strip() or os.path.splitext(os.path.basename(filepath))[0]
        author = ', '.join(mmeta.authors) if mmeta.authors else 'Unknown Author'
        return _apply_epubmeta_fallbacks(filepath, {
            'title': title,
            'authors': author,
            'publisher': mmeta.publisher or '',
            'description': mmeta.description or '',
            'isbn': mmeta.isbn13 or mmeta.isbn10 or '',
            'asin': mmeta.asin or None,
            'pub_date': (mmeta.published_date or '')[:10] if mmeta.published_date else None,
            'has_drm': False,
            'is_invalid': False,
        })
    except Exception as e:
        error_msg = str(e).lower()
        if "encrypted" in error_msg or "drm" in error_msg or "cipher" in error_msg:
            has_drm = True
            is_invalid = False
        else:
            is_invalid = False
        return _apply_epubmeta_fallbacks(filepath, {
            'title': os.path.splitext(os.path.basename(filepath))[0],
            'authors': 'Unknown Author',
            'has_drm': has_drm,
            'is_invalid': is_invalid
        })


def get_epub_cover(filepath):
    """Extract cover image bytes from an EPUB file."""
    if not zipfile.is_zipfile(filepath):
        return None
    try:
        with zipfile.ZipFile(filepath, 'r') as epub:
            namelist_set = set(epub.namelist())
            container_data = epub.read('META-INF/container.xml')
            root = ET.fromstring(container_data)
            rootfile = root.find('.//container:rootfile', NAMESPACES)
            if rootfile is None:
                return None
            opf_path = rootfile.attrib.get('full-path')
            if not opf_path:
                return None
            opf_dir = os.path.dirname(opf_path)
            opf_data = epub.read(opf_path)
            opf_root = ET.fromstring(opf_data)
            return _extract_epub_cover_from_zip(epub, opf_root, opf_dir, namelist_set)
    except Exception as e:
        print(f"Error extracting EPUB cover for {filepath}: {e}")
    return None


def get_pdf_cover(filepath):
    """Render the full first page cover of a PDF file (avoiding partial header/banner XObject slices)."""
    try:
        import pymupdf
        doc = pymupdf.open(filepath)
        try:
            if (not doc.needs_pass or doc.authenticate("")) and len(doc) > 0:
                p0 = doc[0]
                pix = p0.get_pixmap(dpi=150)
                return pix.tobytes("png")
        finally:
            doc.close()
    except Exception:
        pass
    try:
        import pypdf
        with open(filepath, 'rb') as f:
            reader = pypdf.PdfReader(f)
            if reader.pages:
                first_page = reader.pages[0]
                if first_page.images:
                    return first_page.images[0].data
    except Exception as e:
        print(f"Error extracting PDF cover for {filepath}: {e}")
    return None


def generate_thumbnail(image_bytes, max_size=(360, 540)):
    """Generate a base64 encoded JPEG thumbnail from raw image bytes."""
    if not image_bytes or len(image_bytes) > 8 * 1024 * 1024:
        return None
    try:
        import io
        from PIL import Image
        import base64

        warnings.filterwarnings("ignore", category=Image.DecompressionBombWarning)
        Image.MAX_IMAGE_PIXELS = 30_000_000
        with Image.open(io.BytesIO(image_bytes)) as img:
            if (img.width or 0) * (img.height or 0) > 25_000_000:
                return None
            img.thumbnail(max_size)
            if img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')

            out_buf = io.BytesIO()
            img.save(out_buf, format='JPEG', quality=80)
            base64_str = base64.b64encode(out_buf.getvalue()).decode('utf-8')
            return f"data:image/jpeg;base64,{base64_str}"
    except Exception as e:
        print(f"Error generating thumbnail: {e}")
    return None


def extract_metadata(filepath):
    """Main routing function to extract metadata based on file extension or directory."""
    if os.path.isdir(filepath):
        try:
            stat = os.stat(filepath)
            file_info = {
                'file_name': os.path.basename(filepath),
                'file_path': os.path.abspath(filepath),
                'file_size': 0,
                'file_created_at': datetime.fromtimestamp(stat.st_ctime).isoformat(),
                'file_modified_at': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                'file_format': 'DIR',
                'file_hash': get_file_hash_for_dir(filepath)
            }
            meta = {
                'title': os.path.basename(filepath),
                'authors': 'Unknown Author',
                'has_drm': False,
                'is_invalid': False
            }
            return {**file_info, **meta}
        except Exception as e:
            print(f"Error parsing directory {filepath}: {e}")
            return {
                'file_name': os.path.basename(filepath),
                'file_path': os.path.abspath(filepath),
                'file_size': 0,
                'file_format': 'DIR',
                'file_hash': '',
                'title': os.path.basename(filepath),
                'authors': 'Unknown Author',
                'is_invalid': True,
                'has_drm': False
            }

    ext = os.path.splitext(filepath)[1].lower()

    try:
        stat = os.stat(filepath)
        file_info = {
            'file_name': os.path.basename(filepath),
            'file_path': os.path.abspath(filepath),
            'file_size': stat.st_size,
            'file_created_at': datetime.fromtimestamp(stat.st_ctime).isoformat(),
            'file_modified_at': datetime.fromtimestamp(stat.st_mtime).isoformat(),
            'file_format': ext[1:].upper() if ext else 'UNKNOWN',
            'file_hash': get_file_hash(filepath)
        }
    except Exception:
        return {
            'file_name': os.path.basename(filepath),
            'file_path': os.path.abspath(filepath),
            'file_size': 0,
            'file_format': ext[1:].upper() if ext else 'UNKNOWN',
            'file_hash': '',
            'title': os.path.splitext(os.path.basename(filepath))[0],
            'authors': 'Unknown Author',
            'is_invalid': True,
            'has_drm': False
        }

    meta = {}
    cover_bytes = None
    if ext == '.epub':
        meta, cover_bytes = parse_epub(filepath, include_cover=True)
    elif ext == '.pdf':
        meta, cover_bytes = parse_pdf(filepath, include_cover=True)
    elif ext == '.mobi' or ext == '.azw3':
        meta = parse_mobi(filepath)
    else:
        meta = {
            'title': os.path.splitext(os.path.basename(filepath))[0],
            'authors': 'Unknown Author',
            'has_drm': False,
            'is_invalid': False
        }

    res = {**file_info, **meta}
    if cover_bytes:
        thumbnail = generate_thumbnail(cover_bytes)
        if thumbnail:
            res['cover_thumbnail'] = thumbnail

    return res
