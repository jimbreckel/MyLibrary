"""
OPDS 1.2 & Calibre Content Server Emulation (`catalog/opds.py`)
===============================================================
Allows e-reader devices and apps (Moon+ Reader, KOReader, Calibre Sync,
Calibre Companion, Librera, PocketBook, Kobo) to browse, search, view covers,
and download books directly from MyLibrary.

Key Features:
  1. OPDS 1.2 Atom Feeds (`/mylibrary/opds/`):
     - Root Navigation Catalog (`/mylibrary/opds/`)
     - Dynamic Bookmarked & Reading Shelf (`/mylibrary/opds/bookmarked/`) — live from Moon+ Reader WebDAV Cache (`.po` & `.an`)
     - Enriched Tags & Subjects Shelves (`/mylibrary/opds/tags/` & `/mylibrary/opds/tags/?tag=...`)
     - EnrichedEPUBs Shelf (`/mylibrary/opds/enriched/`) — books enriched by `epubmeta`
     - Recently Updated Shelf (`/mylibrary/opds/recent/`)
     - Browse by Author (`/mylibrary/opds/authors/`)
     - Browse by Format (`/mylibrary/opds/formats/`)
     - All Books (`/mylibrary/opds/all/`)
     - OpenSearch Description (`/mylibrary/opds/opensearch.xml`) & Search Feed (`/mylibrary/opds/search/?q=...`)

  2. Calibre-Compatible Book Download & Automatic Moon+ Reader Bookmark Sync:
     - `/mylibrary/opds/download/<uuid>/<filename>`
     - Streams the physical file from `/books` or `/audiobooks` using a Calibre-normalized
       filename (`<Title> - <Author>.epub`).
     - When an enriched `.epub` is downloaded, automatically checks the Moon+ Reader WebDAV Cache
       (`/webdav/Apps/Books/.Moon+/Cache`) and migrates any matching `.po` (reading position) and
       `zlib`-compressed `.an` (highlights/bookmarks) files to the new Calibre filename!

  3. Cover Image Endpoint:
     - `/mylibrary/opds/cover/<uuid>.jpg`
     - Serves decoded base64 thumbnail, extracts embedded cover from `.epub`, or redirects to cover URL.

  4. Calibre Content Server AJAX JSON Emulation:
     - `/mylibrary/opds/ajax/books`
     - `/mylibrary/opds/ajax/book/<uuid>`
     - `/mylibrary/opds/ajax/search`
"""

from __future__ import annotations

import base64
import datetime
import html
import json
import os
import re
import shutil
import sqlite3
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings
from django.db.models import Count, Q
from django.http import (
    FileResponse,
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseRedirect,
    JsonResponse,
)
from django.utils import timezone
from django.views import View

from .models import CatalogItem, FileLocation
from .pg_scanner_db import connect_scanner_db, is_postgres_scanner_enabled
from .search import perform_search
from .views import _clean_single_line, _get_scanner_db_path, _moon_sanitize_stem, _norm_lookup_str, _strip_html

MIME_MAP = {
    'EPUB': 'application/epub+zip',
    'PDF': 'application/pdf',
    'MOBI': 'application/x-mobipocket-ebook',
    'AZW3': 'application/x-mobi8-ebook',
    'CBZ': 'application/vnd.comicbook+zip',
    'CBR': 'application/vnd.comicbook-rar',
    'MP3': 'audio/mpeg',
    'M4B': 'audio/mp4',
}

# Short-lived in-memory caches for fast Web UI & OPDS virtual shelves
_TAGS_SHELF_CACHE: Dict[str, Any] = {
    'expires_at': 0.0,
    'tag_counts': [],           # List of (tag_name, count)
    'tag_to_paths': {},         # tag_name -> List[file_path]
    'path_to_tags': {},         # file_path -> List[tag_name]
    'folder_counts': [],        # List of (folder_name, count)
    'folder_to_paths': {},      # folder_name -> List[file_path]
}

_BOOKMARKED_SHELF_CACHE: Dict[str, Any] = {
    'expires_at': 0.0,
    'dir_mtime': 0.0,
    'items_with_meta': [],      # List of (CatalogItem, moon_meta_dict)
    'item_ids': [],             # Ordered List[UUID]
    'moon_meta_by_id': {},      # str(UUID) -> {'pct': '49.2%', 'has_an': True, 'has_po': True, 'mtime_str': '...'}
}

_AMAZON_SHELF_CACHE: Dict[str, Any] = {
    'expires_at': 0.0,
    'file_mtime': 0.0,
    'by_item_id': {},           # str(CatalogItem.id) -> amazon_info dict
    'by_asin': {},              # ASIN -> amazon_info dict
    'by_norm_title': {},        # normalized title -> amazon_info dict
    'all_ids': [],              # Ordered List[UUID] (all Amazon/Kindle/Audible matched items)
    'kindle_purchased_ids': [], # Strictly Purchased on Amazon
    'kindle_plus_ids': [],      # Kindle+ (Kindle Unlimited / Prime Reading)
    'kindle_bookmarked_ids': [],# Books with Kindle bookmarks / highlights / reading %
    'audible_ids': [],          # Purchased on Audible / Audible+
    'counts': {
        'total': 0,
        'kindle_purchased': 0,
        'kindle_plus': 0,
        'kindle_bookmarked': 0,
        'audible': 0,
    },
}


def _calibre_normalize_filename(title: str, authors: str, ext: str = 'epub') -> str:
    """
    Build the exact `<Title> - <FirstAuthor>.<ext>` filename that Calibre generates
    when exporting/saving to device or Moon+ Reader.
    Strips long `: <subtitle>` suffixes when `<Title> - <FirstAuthor>` would exceed
    Moon+ Reader's 100-character OPDS filename limit.
    """
    clean_title = re.sub(r'[\x00-\x1f\x7f-\x9f\r\n\t\u2028\u2029]+', ' ', title or 'Untitled')
    clean_title = re.sub(r'\s+', ' ', clean_title).strip()
    clean_author = re.sub(r'[\x00-\x1f\x7f-\x9f\r\n\t\u2028\u2029]+', ' ', authors or 'Unknown')
    clean_author = re.sub(r'\s+', ' ', clean_author).strip()
    if ',' in clean_author and '&' not in clean_author:
        clean_author = clean_author.split(',')[0].strip()
    if ':' in clean_title and len(f"{clean_title} - {clean_author}") > 95:
        main_title = clean_title.split(':', 1)[0].strip()
        if len(main_title) >= 4:
            clean_title = main_title
    stem = f"{clean_title} - {clean_author}" if clean_author else clean_title
    stem = re.sub(r'[\\/:*?"<>|]', '_', stem).strip(' .')
    if len(stem) > 95:
        stem = stem[:95].rstrip(' ._-')
    clean_ext = (ext or 'epub').lstrip('.').lower()
    return f"{stem}.{clean_ext}"


def _resolve_physical_file_path(nas_path: str) -> Optional[Path]:
    """
    Safely resolve a `FileLocation.file_path` (e.g. `/books/...` or `/audiobooks/...`)
    inside the container or NAS host filesystem, enforcing strict root sandboxing.
    Also transparently detects files relocated into 1..4 char prefix subdirectories
    by `splitfiles` (e.g. `<parent>/ed-/<filename>` or `<parent>/ED-/<filename>`)
    and updates `FileLocation.file_path` in PostgreSQL automatically.
    """
    if not nas_path:
        return None
    raw = str(nas_path).strip().replace('\\', '/')
    allowed_roots = [
        Path('/books'),
        Path('/audiobooks'),
        Path('/volume1/common/Media/Books'),
        Path('/volume1/common/Media/Audiobooks'),
    ]
    candidates = [Path(raw)]
    if raw.startswith('/books/'):
        rel = raw[len('/books/'):]
        candidates.append(Path('/volume1/common/Media/Books') / rel)
    elif raw.startswith('/audiobooks/'):
        rel = raw[len('/audiobooks/'):]
        candidates.append(Path('/volume1/common/Media/Audiobooks') / rel)

    for cand in candidates:
        try:
            if cand.exists() and cand.is_file():
                resolved = cand.resolve()
                for root in allowed_roots:
                    try:
                        if root.exists() and str(resolved).startswith(str(root.resolve())):
                            return resolved
                    except Exception:
                        continue
        except Exception:
            continue

    # Fallback: check if `splitfiles` moved `<parent>/<filename>` into `<parent>/<prefix>/<filename>`
    base_p = Path(raw)
    fname = base_p.name
    stem = base_p.stem
    if fname and len(stem) >= 2:
        prefixes = []
        for plen in (3, 2, 4, 1):
            if len(stem) >= plen:
                sub = stem[:plen]
                for variant in (sub, sub.lower(), sub.upper(), sub.capitalize()):
                    if variant and variant not in prefixes:
                        prefixes.append(variant)
        alnum_stem = re.sub(r'[^A-Za-z0-9]+', '', stem)
        for plen in (3, 2):
            if len(alnum_stem) >= plen:
                sub = alnum_stem[:plen]
                for variant in (sub, sub.lower(), sub.upper()):
                    if variant and variant not in prefixes:
                        prefixes.append(variant)

        split_candidates: List[Path] = []
        for cand in candidates:
            parent_dir = cand.parent
            try:
                if parent_dir.exists() and parent_dir.is_dir():
                    for pfx in prefixes:
                        split_candidates.append(parent_dir / pfx / fname)
            except Exception:
                continue

        for sc in split_candidates:
            try:
                if sc.exists() and sc.is_file():
                    resolved = sc.resolve()
                    for root in allowed_roots:
                        try:
                            if root.exists() and str(resolved).startswith(str(root.resolve())):
                                new_db_path = str(resolved).replace('\\', '/')
                                if new_db_path.startswith('/volume1/common/Media/Books/'):
                                    new_db_path = '/books/' + new_db_path[len('/volume1/common/Media/Books/'):]
                                try:
                                    FileLocation.objects.filter(file_path=raw).update(file_path=new_db_path)
                                except Exception:
                                    pass
                                return resolved
                        except Exception:
                            continue
            except Exception:
                continue

    return None


def _get_moon_cache_dir() -> Optional[Path]:
    """
    Locate Moon+ Reader WebDAV Cache directory whether running inside Docker (`/webdav/...`)
    or on the NAS host (`/volume1/common/webdav/...`) or Windows (`U:\\webdav\\...`).
    """
    candidates = [
        Path('/webdav/Apps/Books/.Moon+/Cache'),
        Path('/volume1/common/webdav/Apps/Books/.Moon+/Cache'),
        Path(r'U:\webdav\Apps\Books\.Moon+\Cache'),
    ]
    for c in candidates:
        try:
            if c.exists() and c.is_dir():
                return c
        except Exception:
            continue
    return None


def get_enriched_tags_and_folders_index() -> Dict[str, Any]:
    """
    Build and cache (for 1800s with stale-while-revalidate) the dynamic virtual bookshelves of:
      1. `tag_counts` / `tag_to_paths`: Enriched subject tags from `processed_files.updated_metadata_json`
      2. `folder_counts` / `folder_to_paths`: Top-level library categories for enriched books
    """
    now = time.monotonic()
    if _TAGS_SHELF_CACHE['tag_counts'] and now < _TAGS_SHELF_CACHE['expires_at']:
        return _TAGS_SHELF_CACHE

    if _TAGS_SHELF_CACHE['tag_counts'] and not _TAGS_SHELF_CACHE.get('_refreshing'):
        _TAGS_SHELF_CACHE['_refreshing'] = True
        _TAGS_SHELF_CACHE['expires_at'] = now + 1800.0
        import threading
        threading.Thread(target=_rebuild_tags_shelf_cache, daemon=True).start()
        return _TAGS_SHELF_CACHE

    return _rebuild_tags_shelf_cache()


def _rebuild_tags_shelf_cache() -> Dict[str, Any]:
    now = time.monotonic()
    db_path = _get_scanner_db_path()
    tag_to_paths: Dict[str, List[str]] = {}
    tag_to_titles: Dict[str, List[str]] = {}
    path_to_tags: Dict[str, List[str]] = {}
    folder_to_paths: Dict[str, List[str]] = {}
    folder_to_titles: Dict[str, List[str]] = {}

    if is_postgres_scanner_enabled() or db_path.exists():
        try:
            conn = connect_scanner_db(db_path, timeout=10.0)
            conn.text_factory = lambda b: b.decode('utf-8', errors='replace')
            rows = conn.execute(
                """
                SELECT file_path, title, updated_metadata_json
                FROM processed_files
                WHERE status = 'SUCCESS'
                ORDER BY updated_at DESC
                """
            ).fetchall()
            conn.close()

            for fp, row_title, raw_json in rows:
                if not fp:
                    continue
                t_clean = (row_title or '').strip()
                norm_rel = fp.replace('\\', '/')
                for pfx in ('/volume1/common/Media/Books/', '/books/'):
                    if norm_rel.startswith(pfx):
                        norm_rel = norm_rel[len(pfx):]
                        break
                parts = [p for p in norm_rel.split('/') if p]
                if len(parts) >= 2:
                    folder_name = parts[0].strip()
                    folder_to_paths.setdefault(folder_name, []).append(fp)
                    if t_clean:
                        folder_to_titles.setdefault(folder_name, []).append(t_clean)

                if raw_json:
                    try:
                        meta = json.loads(raw_json)
                        if not t_clean and meta.get('title'):
                            t_clean = str(meta['title']).strip()
                        raw_subjs = meta.get('subjects') or meta.get('tags') or meta.get('categories') or []
                        if isinstance(raw_subjs, list):
                            seen_for_book = set()
                            book_tags = []
                            for s in raw_subjs:
                                if not isinstance(s, str):
                                    continue
                                clean_tag = s.strip()
                                if len(clean_tag) < 2 or len(clean_tag) > 65:
                                    continue
                                tag_key = clean_tag
                                if tag_key.lower() not in seen_for_book:
                                    seen_for_book.add(tag_key.lower())
                                    tag_to_paths.setdefault(tag_key, []).append(fp)
                                    if t_clean:
                                        tag_to_titles.setdefault(tag_key, []).append(t_clean)
                                    book_tags.append(tag_key)
                            if book_tags:
                                path_to_tags[str(fp)] = book_tags
                    except Exception:
                        pass
        except Exception:
            pass

    tag_counts = sorted(
        [(tag, len(paths)) for tag, paths in tag_to_paths.items() if len(paths) >= 2],
        key=lambda x: (-x[1], x[0].lower()),
    )
    folder_counts = sorted(
        [(fld, len(paths)) for fld, paths in folder_to_paths.items()],
        key=lambda x: (-x[1], x[0].lower()),
    )

    _TAGS_SHELF_CACHE['expires_at'] = time.monotonic() + 1800.0
    _TAGS_SHELF_CACHE['tag_counts'] = tag_counts
    _TAGS_SHELF_CACHE['tag_to_paths'] = tag_to_paths
    _TAGS_SHELF_CACHE['tag_to_titles'] = tag_to_titles
    _TAGS_SHELF_CACHE['path_to_tags'] = path_to_tags
    _TAGS_SHELF_CACHE['folder_counts'] = folder_counts
    _TAGS_SHELF_CACHE['folder_to_paths'] = folder_to_paths
    _TAGS_SHELF_CACHE['folder_to_titles'] = folder_to_titles
    _TAGS_SHELF_CACHE['_refreshing'] = False
    return _TAGS_SHELF_CACHE


def get_file_tags(file_path: str) -> List[str]:
    """
    Return the enriched subject/tag list for `file_path` from `scanner_state.db`.
    """
    if not file_path:
        return []
    norm = str(file_path).strip().replace('\\', '/')
    idx = get_enriched_tags_and_folders_index()
    p2t = idx.get('path_to_tags', {})
    if norm in p2t:
        return p2t[norm]
    phys = _resolve_physical_file_path(norm)
    if phys and str(phys) in p2t:
        return p2t[str(phys)]
    return []


def get_dynamic_bookmarked_shelf() -> Dict[str, Any]:
    """
    Dynamically inspect the live Moon+ Reader WebDAV Cache (`/webdav/Apps/Books/.Moon+/Cache`)
    and match every `.po` (reading position) and `.an` (annotations/highlights/bookmarks)
    file to its `CatalogItem` in BookDB, sorted by most recently read/bookmarked first!
    Re-evaluates immediately whenever the WebDAV Cache directory mtime changes (or every 30s).
    """
    cache_dir = _get_moon_cache_dir()
    if not cache_dir:
        return {
            'items_with_meta': [],
            'item_ids': [],
            'moon_meta_by_id': {},
            'total_moon_stems': 0,
        }

    now = time.monotonic()
    try:
        dir_mtime = cache_dir.stat().st_mtime
    except Exception:
        dir_mtime = 0.0

    if (
        _BOOKMARKED_SHELF_CACHE['item_ids']
        and now < _BOOKMARKED_SHELF_CACHE['expires_at']
        and dir_mtime == _BOOKMARKED_SHELF_CACHE['dir_mtime']
    ):
        return _BOOKMARKED_SHELF_CACHE

    # 1. Scan all .po and .an files in Moon+ WebDAV Cache
    stems: Dict[str, Dict[str, Any]] = {}
    try:
        for entry in os.scandir(cache_dir):
            if not entry.is_file():
                continue
            name = entry.name
            if not (name.endswith('.po') or name.endswith('.an')):
                continue
            stem = name[:-3]  # e.g. "Wool (Silo) - Howey, Hugh.epub"
            st = entry.stat()
            info = stems.setdefault(
                stem,
                {
                    'stem': stem,
                    'mtime': 0.0,
                    'has_po': False,
                    'has_an': False,
                    'pct': '',
                    'chapter': 0,
                    'char_offset': 0,
                    'pct_val': 0.0,
                },
            )
            if st.st_mtime > info['mtime']:
                info['mtime'] = st.st_mtime
            if name.endswith('.po'):
                info['has_po'] = True
                try:
                    with open(entry.path, 'r', encoding='utf-8', errors='replace') as f:
                        po_line = f.read(128).strip()
                    if ':' in po_line:
                        info['pct'] = po_line.rsplit(':', 1)[-1].strip()
                    m_po = re.search(r'\*(\d+)@\d+#(\d+):([\d.]+)%', po_line)
                    if m_po:
                        info['chapter'] = int(m_po.group(1))
                        info['char_offset'] = int(m_po.group(2))
                        info['pct_val'] = float(m_po.group(3))
                except Exception:
                    pass
            elif name.endswith('.an'):
                info['has_an'] = True
                try:
                    with open(entry.path, 'rb') as f_an:
                        raw_an_bytes = f_an.read(65536)
                    an_text = zlib.decompress(raw_an_bytes).decode('utf-8', errors='replace')
                    if re.search(r'(?:#bad\b|\bbad\s*meta(?:data)?\b|\bfix\s*meta(?:data)?\b)', an_text, flags=re.IGNORECASE):
                        info['flagged_bad_meta'] = True
                except Exception:
                    pass
    except Exception:
        pass

    sorted_stems = sorted(stems.values(), key=lambda x: -x['mtime'])

    # 2. Load enriched mapping from scanner_state.db so renamed/enriched books match seamlessly
    enr_by_norm_title: Dict[str, str] = {}
    enr_by_fname: Dict[str, str] = {}
    db_path = _get_scanner_db_path()
    if is_postgres_scanner_enabled() or db_path.exists():
        try:
            conn = connect_scanner_db(db_path, timeout=5.0)
            rows = conn.execute(
                "SELECT file_path, file_name, title, orig_title FROM processed_files WHERE status = 'SUCCESS'"
            ).fetchall()
            conn.close()
            for fp, fn, t, ot in rows:
                if fn:
                    enr_by_fname[fn.lower()] = fp
                    enr_by_fname[_moon_sanitize_stem(fn).lower()] = fp
                for cand_t in (t, ot):
                    nt = _norm_lookup_str(cand_t)
                    if nt and len(nt) >= 4:
                        enr_by_norm_title[nt] = fp
        except Exception:
            pass

    # 3. Extract candidate titles and filenames in strict sorted_stems order for fast batch query against BookDB
    cand_paths = set()
    cand_fnames = set()
    cand_titles_ordered: List[str] = []
    seen_cand_titles = set()
    stem_parsed = []

    def _add_cand_title(val: str) -> None:
        v = (val or '').strip()
        if len(v) >= 3 and v not in seen_cand_titles:
            seen_cand_titles.add(v)
            cand_titles_ordered.append(v)

    for s_info in sorted_stems:
        stem = s_info['stem']
        stem_lower = stem.lower()
        cand_fnames.add(stem)
        if stem_lower in enr_by_fname:
            cand_paths.add(enr_by_fname[stem_lower])

        base_no_ext, ext = os.path.splitext(stem)
        if ' - ' in base_no_ext:
            raw_title, raw_author = base_no_ext.rsplit(' - ', 1)
        elif '--' in base_no_ext:
            parts = [p.strip() for p in base_no_ext.split('--') if p.strip()]
            raw_title = parts[0] if parts else base_no_ext
            raw_author = parts[1] if len(parts) > 1 else ''
        else:
            raw_title, raw_author = base_no_ext, ''

        clean_title = raw_title.replace('_', ':').strip()
        raw_title_clean = raw_title.strip()
        short_title = re.sub(r'\s*\(.*?\)\s*', ' ', raw_title_clean).strip()
        prefix_title = raw_title_clean.split('_', 1)[0].strip() if '_' in raw_title_clean else ''

        _add_cand_title(raw_title_clean)
        _add_cand_title(clean_title)
        _add_cand_title(short_title)
        _add_cand_title(prefix_title)

        nt = _norm_lookup_str(raw_title_clean)
        if nt in enr_by_norm_title:
            cand_paths.add(enr_by_norm_title[nt])
        if prefix_title:
            nt_pref = _norm_lookup_str(prefix_title)
            if nt_pref in enr_by_norm_title:
                cand_paths.add(enr_by_norm_title[nt_pref])

        stem_parsed.append((s_info, stem_lower, raw_title_clean, clean_title, short_title, prefix_title, raw_author, nt))

    by_path: Dict[str, CatalogItem] = {}
    by_fname_lower: Dict[str, CatalogItem] = {}
    by_norm_title: Dict[str, List[CatalogItem]] = {}

    try:
        if cand_paths or cand_fnames:
            loc_qs = (
                FileLocation.objects.filter(Q(file_path__in=list(cand_paths)) | Q(file_name__in=list(cand_fnames)))
                .select_related('catalog_item')
                .prefetch_related('catalog_item__files')[:2000]
            )
            for loc in loc_qs:
                if loc.catalog_item:
                    by_path[loc.file_path] = loc.catalog_item
                    by_fname_lower[loc.file_name.lower()] = loc.catalog_item
                    by_fname_lower[_moon_sanitize_stem(loc.file_name).lower()] = loc.catalog_item

        if cand_titles_ordered:
            ci_qs = CatalogItem.objects.filter(title__in=cand_titles_ordered[:2500]).prefetch_related('files')[:3000]
            for ci in ci_qs:
                nt = _norm_lookup_str(ci.title)
                if nt:
                    by_norm_title.setdefault(nt, []).append(ci)
    except Exception:
        pass

    items_with_meta: List[Tuple[CatalogItem, Dict[str, Any]]] = []
    item_ids: List[Any] = []
    moon_meta_by_id: Dict[str, Dict[str, Any]] = {}
    seen_item_ids = set()

    for s_info, stem_lower, raw_title_clean, clean_title, short_title, prefix_title, raw_author, nt in stem_parsed:
        matched_item: Optional[CatalogItem] = None

        # Priority 1: Exact file_name or enriched path match
        if stem_lower in by_fname_lower:
            matched_item = by_fname_lower[stem_lower]
        elif stem_lower in enr_by_fname and enr_by_fname[stem_lower] in by_path:
            matched_item = by_path[enr_by_fname[stem_lower]]
        elif nt in enr_by_norm_title and enr_by_norm_title[nt] in by_path:
            matched_item = by_path[enr_by_norm_title[nt]]

        # Priority 2: Normalized title + author token verification
        if not matched_item:
            for cand_nt in (nt, _norm_lookup_str(short_title), _norm_lookup_str(prefix_title)):
                if not cand_nt or cand_nt not in by_norm_title:
                    continue
                cands = by_norm_title[cand_nt]
                na_moon = _norm_lookup_str(raw_author)
                if not na_moon or na_moon == 'unknown':
                    matched_item = cands[0]
                    break
                moon_tokens = {t for t in na_moon.split() if len(t) >= 2}
                for c_item in cands:
                    ci_tokens = set(_norm_lookup_str(c_item.authors).split())
                    if moon_tokens & ci_tokens:
                        matched_item = c_item
                        break
                if not matched_item and cands and cand_nt != _norm_lookup_str(prefix_title):
                    matched_item = cands[0]
                if matched_item:
                    break

        if matched_item:
            str_id = str(matched_item.id)
            if matched_item.id in seen_item_ids:
                existing_meta = moon_meta_by_id.get(str_id)
                if existing_meta:
                    if s_info['mtime'] > existing_meta['mtime']:
                        dt_utc = datetime.datetime.fromtimestamp(s_info['mtime'], tz=datetime.timezone.utc)
                        existing_meta['mtime'] = s_info['mtime']
                        existing_meta['mtime_str'] = dt_utc.strftime('%Y-%m-%d %H:%M UTC')
                        existing_meta['date_short'] = dt_utc.strftime('%Y-%m-%d')
                        existing_meta['mtime_iso'] = dt_utc.strftime('%Y-%m-%dT%H:%M:%SZ')
                    # Always prefer the furthest reading progress if multiple .po files match the same book
                    if (s_info.get('pct_val') or 0.0) > (existing_meta.get('pct_val') or 0.0):
                        existing_meta['pct'] = s_info['pct']
                        existing_meta['pct_val'] = s_info['pct_val']
                        existing_meta['chapter'] = s_info.get('chapter', 0)
                        existing_meta['char_offset'] = s_info.get('char_offset', 0)
                        existing_meta['moon_stem'] = s_info['stem']
                    if s_info['has_an']:
                        existing_meta['has_an'] = True
                    if s_info.get('flagged_bad_meta'):
                        existing_meta['flagged_bad_meta'] = True
                        try:
                            set_book_bad_metadata(str_id, True, reason='Flagged via Moon+ Reader bookmark/note (.an)')
                        except Exception:
                            pass
                continue
            seen_item_ids.add(matched_item.id)
            if s_info.get('flagged_bad_meta'):
                try:
                    set_book_bad_metadata(str_id, True, reason='Flagged via Moon+ Reader bookmark/note (.an)')
                except Exception:
                    pass
            dt_utc = datetime.datetime.fromtimestamp(s_info['mtime'], tz=datetime.timezone.utc) if s_info['mtime'] else None
            mtime_str = dt_utc.strftime('%Y-%m-%d %H:%M UTC') if dt_utc else ''
            date_short = dt_utc.strftime('%Y-%m-%d') if dt_utc else ''
            mtime_iso = dt_utc.strftime('%Y-%m-%dT%H:%M:%SZ') if dt_utc else ''
            meta_badge = {
                'pct': s_info['pct'] or ('Bookmarked' if s_info['has_an'] else 'Reading'),
                'has_an': s_info['has_an'],
                'has_po': s_info['has_po'],
                'mtime': s_info['mtime'],
                'mtime_str': mtime_str,
                'date_short': date_short,
                'mtime_iso': mtime_iso,
                'moon_stem': s_info['stem'],
                'chapter': s_info.get('chapter', 0),
                'char_offset': s_info.get('char_offset', 0),
                'pct_val': s_info.get('pct_val', 0.0),
            }
            # Attach dynamic attribute for template/OPDS rendering
            matched_item.moon_bookmark_info = meta_badge
            items_with_meta.append((matched_item, meta_badge))
            item_ids.append(matched_item.id)
            moon_meta_by_id[str(matched_item.id)] = meta_badge

    # Ensure strict descending sort by most recent WebDAV file mtime first
    items_with_meta.sort(key=lambda pair: -(pair[1].get('mtime') or 0.0))
    item_ids = [it.id for it, _ in items_with_meta]

    _BOOKMARKED_SHELF_CACHE['expires_at'] = now + 1800.0
    _BOOKMARKED_SHELF_CACHE['dir_mtime'] = dir_mtime
    _BOOKMARKED_SHELF_CACHE['items_with_meta'] = items_with_meta
    _BOOKMARKED_SHELF_CACHE['item_ids'] = item_ids
    _BOOKMARKED_SHELF_CACHE['moon_meta_by_id'] = moon_meta_by_id
    _BOOKMARKED_SHELF_CACHE['total_moon_stems'] = len(stems)
    return _BOOKMARKED_SHELF_CACHE


def _rewrite_an_zlib_bytes(raw_bytes: bytes, new_book_filename: str) -> bytes:
    text = zlib.decompress(raw_bytes).decode('utf-8', errors='replace')
    has_crlf = '\r\n' in text
    lines = text.splitlines()
    lower_new = new_book_filename.lower()
    i = 0
    n = len(lines)
    while i < n:
        if lines[i].strip() == '#' and i + 5 < n:
            lines[i + 3] = new_book_filename
            for offset in (4, 5):
                path_line = lines[i + offset]
                slash_idx = path_line.rfind('/')
                target_name = lower_new if offset == 5 else new_book_filename
                if slash_idx >= 0:
                    lines[i + offset] = path_line[:slash_idx + 1] + target_name
                else:
                    lines[i + offset] = target_name
            i += 6
        else:
            i += 1
    sep = '\r\n' if has_crlf else '\n'
    rebuilt = sep.join(lines)
    if text.endswith(('\n', '\r\n')):
        rebuilt += sep
    return zlib.compress(rebuilt.encode('utf-8'), level=6)


def _extract_po_pct_val(po_path: Path) -> float:
    try:
        if not po_path.exists():
            return -1.0
        txt = po_path.read_text(encoding='utf-8', errors='replace').strip()
        m = re.search(r':([\d.]+)%', txt)
        return float(m.group(1)) if m else 0.0
    except Exception:
        return -1.0


def _adapt_po_content_to_epub(po_line: str, epub_path: Optional[Path]) -> str:
    """
    Prepare a migrated Moon+ Reader `.po` line (`<dev_id>*<chap>@<split>#<offset>:<pct>%`)
    so that:
      1. The leading `<dev_id>*` prefix is refreshed to a new millisecond ID (`int(time.time() * 1000)`),
         ensuring Moon+ Reader on ANY device (even the same device that uploaded the old `.po` under
         the previous filename) recognizes it as a cloud sync update instead of ignoring its own device ID.
      2. If `old_chap` exceeds the number of spine items in `epub_path`, recalibrates the chapter/offset
         to match `pct_val`.
    """
    po_clean = (po_line or '').strip()
    if not po_clean:
        return po_clean

    m = re.match(r'^(\d+\*)(\d+)(@\d+#)(\d+)(:([\d.]+)%.*)$', po_clean)
    if not m:
        return po_clean

    _old_prefix, old_chap_s, mid, old_off_s, suffix, pct_s = m.groups()
    old_chap, old_off, pct_val = int(old_chap_s), int(old_off_s), float(pct_s)
    new_prefix = f"{int(time.time() * 1000)}*"
    if pct_val <= 0.0 or not epub_path or not epub_path.exists():
        return f"{new_prefix}{old_chap}{mid}{old_off}{suffix}"

    try:
        with zipfile.ZipFile(epub_path, 'r') as zf:
            names_set = set(zf.namelist())
            opfs = [n for n in zf.namelist() if n.lower().endswith('.opf')]
            spine_files: List[str] = []
            if opfs:
                root = ET.fromstring(zf.read(opfs[0]))
                opf_dir = os.path.dirname(opfs[0])
                manifest = {
                    item.attrib.get('id'): item.attrib.get('href')
                    for item in root.findall('.//{*}manifest/{*}item')
                }
                for itemref in root.findall('.//{*}spine/{*}itemref'):
                    href = urllib.parse.unquote(manifest.get(itemref.attrib.get('idref'), '') or '')
                    s = (f"{opf_dir}/{href}".lstrip('/') if opf_dir else href)
                    if s in names_set:
                        spine_files.append(s)
            if not spine_files:
                spine_files = [
                    n for n in zf.namelist()
                    if n.lower().endswith(('.html', '.xhtml', '.htm', '.xml'))
                ]
            if not spine_files or old_chap < len(spine_files):
                return f"{new_prefix}{old_chap}{mid}{old_off}{suffix}"

            text_lens: List[int] = []
            for s in spine_files:
                raw = zf.read(s).decode('utf-8', errors='ignore')
                raw = re.sub(r'<(head|style|script)[^>]*>.*?</\1>', '', raw, flags=re.S | re.I)
                txt = re.sub(r'<[^>]+>', '', raw)
                text_lens.append(len(txt))

            tot = sum(text_lens)
            if tot <= 0:
                return f"{new_prefix}{old_chap}{mid}{old_off}{suffix}"

            target_pos = int(round((pct_val / 100.0) * tot))
            cum = 0
            for ch, tl in enumerate(text_lens):
                if cum + tl >= target_pos or ch == len(text_lens) - 1:
                    new_off = max(0, min(max(0, tl - 1), target_pos - cum))
                    return f"{new_prefix}{ch}{mid}{new_off}{suffix}"
                cum += tl
    except Exception:
        return f"{new_prefix}{old_chap}{mid}{old_off}{suffix}"

    return f"{new_prefix}{old_chap}{mid}{old_off}{suffix}"


def _auto_migrate_moon_bookmarks_for_download(
    item: CatalogItem,
    file_loc: FileLocation,
    new_calibre_filename: str,
    epub_path: Optional[Path] = None,
) -> Optional[str]:
    """
    When a user downloads an `.epub` via OPDS, check if there is an existing Moon+ Reader
    `.po` or `.an` file in `/webdav/Apps/Books/.Moon+/Cache` under the book's old/original
    title or filename, and copy/rewrite it to `<new_calibre_filename>.po` / `.an`,
    automatically calibrating the `.po` spine chapter index to the downloaded `.epub`.
    """
    cache_dir = _get_moon_cache_dir()
    if not cache_dir:
        return None

    if epub_path is None:
        epub_path = _resolve_physical_file_path(file_loc.file_path)

    new_moon_stem = _moon_sanitize_stem(new_calibre_filename)
    target_po = cache_dir / f"{new_moon_stem}.po"
    target_an = cache_dir / f"{new_moon_stem}.an"
    target_po_pct = _extract_po_pct_val(target_po)

    orig_titles = [item.title]
    db_path = _get_scanner_db_path()
    if is_postgres_scanner_enabled() or db_path.exists():
        try:
            conn = connect_scanner_db(db_path, timeout=5.0)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT orig_title, orig_author, title, author FROM processed_files WHERE file_path = ? LIMIT 1",
                (file_loc.file_path,),
            ).fetchone()
            conn.close()
            if row:
                if row['orig_title']:
                    orig_titles.append(row['orig_title'])
                if row['title']:
                    orig_titles.append(row['title'])
        except Exception:
            pass

    norm_targets = {_norm_lookup_str(t) for t in orig_titles if _norm_lookup_str(t)}
    all_item_fnames = {
        (f.file_name or '').lower() for f in item.files.all() if f.file_name
    } | {
        _moon_sanitize_stem(f.file_name or '').lower() for f in item.files.all() if f.file_name
    }
    orig_fname_lower = (file_loc.file_name or '').lower()
    orig_san_lower = _moon_sanitize_stem(file_loc.file_name or '').lower()

    matched_stem = new_moon_stem if target_po.exists() and target_po_pct > 0 else None
    best_cand_pct = target_po_pct if matched_stem else -1.0

    try:
        bm_data = get_dynamic_bookmarked_shelf()
        bm_meta = bm_data.get('moon_meta_by_id', {}).get(str(item.id))
        if bm_meta and bm_meta.get('moon_stem'):
            bm_pct = float(bm_meta.get('pct_val') or 0.0)
            if bm_pct >= best_cand_pct:
                matched_stem = bm_meta['moon_stem']
                best_cand_pct = bm_pct
    except Exception:
        pass

    try:
        for entry in os.scandir(cache_dir):
            if not entry.is_file() or not entry.name.endswith('.epub.po'):
                continue
            cand_stem = entry.name[:-3]  # strip .po
            cand_lower = cand_stem.lower()
            stem_no_ext = os.path.splitext(cand_stem)[0]
            t_part = stem_no_ext.rsplit(' - ', 1)[0] if ' - ' in stem_no_ext else stem_no_ext
            is_match = (
                cand_stem == new_moon_stem
                or cand_lower in (orig_fname_lower, orig_san_lower)
                or cand_lower in all_item_fnames
                or _norm_lookup_str(t_part) in norm_targets
            )
            if is_match:
                cand_pct = _extract_po_pct_val(Path(entry.path))
                if cand_pct > best_cand_pct or (cand_pct == best_cand_pct and cand_stem != new_moon_stem):
                    best_cand_pct = cand_pct
                    matched_stem = cand_stem
    except Exception:
        pass

    if not matched_stem:
        return None

    old_po = cache_dir / f"{matched_stem}.po"
    old_an = cache_dir / f"{matched_stem}.an"
    try:
        if old_po.exists():
            raw_po = old_po.read_text(encoding='utf-8', errors='replace').strip()
            adapted_po = _adapt_po_content_to_epub(raw_po, epub_path)
            target_po.write_text(adapted_po, encoding='utf-8')
        if old_an.exists() and (not target_an.exists() or matched_stem != new_moon_stem):
            raw_an = old_an.read_bytes()
            rewritten = _rewrite_an_zlib_bytes(raw_an, new_calibre_filename)
            target_an.write_bytes(rewritten)
        return matched_stem
    except Exception:
        return None


_ENRICHED_PATHS_CACHE: Dict[str, Any] = {
    'expires_at': 0.0,
    'db_mtime': 0.0,
    'last_updated_at': '',
    'paths_set': set(),
    'status_by_path': {},
    'enriched_item_ids': None,
    'enriched_item_ids_set': set(),
    'needs_review_item_ids': None,
    'needs_review_item_ids_set': set(),
    'last_dup_merge_at': 0.0,
    '_refreshing': False,
}

_GOOGLE_BOOKS_COOLDOWN_UNTIL: float = 0.0

ENRICHABLE_FORMATS = (
    'EPUB', 'PDF', 'MOBI', 'AZW', 'AZW3', 'PRC', 'PDB',
    'FB2', 'CBZ', 'CBR', 'DJVU', 'TXT', 'RTF', 'DOC', 'DOCX', 'LIT',
)
ENRICHABLE_EXTENSIONS = tuple(f'.{fmt.lower()}' for fmt in ENRICHABLE_FORMATS)


def _sync_incremental_enriched_changes() -> None:
    """
    Fast (< 2ms) delta sync across Gunicorn workers whenever `processed_files` is modified,
    so `Enriched Books` and `Needs Enriching` counts stay 100% up-to-date across all workers
    without rebuilding the full 27,000-item index.
    """
    if not _ENRICHED_PATHS_CACHE.get('paths_set') or _ENRICHED_PATHS_CACHE.get('enriched_item_ids') is None:
        return
    pg_enabled = is_postgres_scanner_enabled()
    db_path = _get_scanner_db_path()
    cur_mtime = 0.0
    if pg_enabled:
        now_m = time.monotonic()
        if now_m - _ENRICHED_PATHS_CACHE.get('last_sync_check_at', 0.0) < 1.0:
            return
        _ENRICHED_PATHS_CACHE['last_sync_check_at'] = now_m
    else:
        if not db_path.exists():
            return
        try:
            cur_mtime = db_path.stat().st_mtime
            wal_path = Path(str(db_path) + '-wal')
            if wal_path.exists():
                cur_mtime = max(cur_mtime, wal_path.stat().st_mtime)
        except Exception:
            return
        if cur_mtime == _ENRICHED_PATHS_CACHE.get('db_mtime', 0.0):
            return

    last_ts = _ENRICHED_PATHS_CACHE.get('last_updated_at') or ''
    if not last_ts:
        _ENRICHED_PATHS_CACHE['db_mtime'] = cur_mtime
        return

    new_success_paths: List[str] = []
    new_review_paths: List[str] = []
    new_strict_review_paths: List[str] = []
    new_excluded_paths: List[str] = []
    new_ignored_paths: List[str] = []
    max_ts = last_ts
    paths_set = _ENRICHED_PATHS_CACHE['paths_set']
    status_by_path = _ENRICHED_PATHS_CACHE['status_by_path']

    try:
        conn = connect_scanner_db(db_path, timeout=3.0)
        conn.text_factory = lambda b: b.decode('utf-8', errors='replace')
        rows = conn.execute(
            """
            SELECT file_path, status, error_message, updated_at,
                   title, author, isbn,
                   orig_title, orig_author, orig_isbn,
                   match_confidence, original_metadata_json, updated_metadata_json
            FROM processed_files
            WHERE updated_at > ?
            ORDER BY updated_at ASC
            LIMIT 2000
            """,
            (last_ts,),
        ).fetchall()
        conn.close()
        for r in rows:
            fp, st_raw, err_raw, upd_ts = r[0], r[1], r[2], r[3]
            if upd_ts and str(upd_ts) > max_ts:
                max_ts = str(upd_ts)
            if not fp:
                continue
            p_str = str(fp)
            if p_str.startswith('/volume1/common/Media/Books/'):
                alt_p = '/books/' + p_str[len('/volume1/common/Media/Books/'):]
            elif p_str.startswith('/books/'):
                alt_p = '/volume1/common/Media/Books/' + p_str[len('/books/'):]
            else:
                alt_p = None
            st_val = str(st_raw or '').upper()
            err_val = str(err_raw or '')
            if st_val == 'SUCCESS':
                paths_set.add(p_str)
                status_by_path.pop(p_str, None)
                new_success_paths.append(p_str)
                new_excluded_paths.append(p_str)
                if alt_p:
                    paths_set.add(alt_p)
                    status_by_path.pop(alt_p, None)
                    new_success_paths.append(alt_p)
                    new_excluded_paths.append(alt_p)
            else:
                if p_str not in paths_set:
                    new_excluded_paths.append(p_str)
                    if alt_p:
                        new_excluded_paths.append(alt_p)
                if st_val == 'IGNORED':
                    status_by_path.pop(p_str, None)
                    new_ignored_paths.append(p_str)
                    if alt_p:
                        status_by_path.pop(alt_p, None)
                        new_ignored_paths.append(alt_p)
                elif st_val in ('NEEDS_REVIEW', 'FAILED', 'NOT_FOUND', 'NO_ISBN', 'HAS_DRM', 'QUARANTINED'):
                    orig_m = {}
                    upd_m = {}
                    if len(r) > 11 and r[11]:
                        try:
                            orig_m = json.loads(r[11]) or {}
                        except Exception:
                            orig_m = {}
                    if len(r) > 12 and r[12]:
                        try:
                            upd_m = json.loads(r[12]) or {}
                        except Exception:
                            upd_m = {}
                    conf_pct = None
                    if len(r) > 10 and r[10] is not None:
                        try:
                            raw_c = float(r[10])
                            conf_pct = int(round(raw_c * 100)) if raw_c <= 1.0 else int(round(raw_c))
                        except Exception:
                            conf_pct = None
                    diag_obj = {
                        'status': st_val,
                        'error': err_val,
                        'message': err_val,
                        'file_path': p_str,
                        'confidence': conf_pct,
                        'candidate_title': str((r[4] if len(r) > 4 else '') or upd_m.get('title') or ''),
                        'candidate_authors': str((r[5] if len(r) > 5 else '') or ''),
                        'candidate_isbn': str((r[6] if len(r) > 6 else '') or upd_m.get('isbn13') or upd_m.get('isbn10') or ''),
                        'candidate_publisher': str(upd_m.get('publisher') or ''),
                        'candidate_pub_date': str(upd_m.get('published_date') or ''),
                        'candidate_description': _strip_html(str(upd_m.get('description') or '')),
                        'candidate_cover': str(upd_m.get('cover_url') or ''),
                        'orig_title': str((r[7] if len(r) > 7 else '') or orig_m.get('title') or ''),
                        'orig_authors': str((r[8] if len(r) > 8 else '') or ''),
                        'orig_isbn': str((r[9] if len(r) > 9 else '') or orig_m.get('isbn13') or orig_m.get('isbn10') or ''),
                        'orig_publisher': str(orig_m.get('publisher') or ''),
                        'orig_pub_date': str(orig_m.get('published_date') or ''),
                        'orig_description': _strip_html(str(orig_m.get('description') or '')),
                        'orig_cover': str(orig_m.get('cover_url') or ''),
                    }
                    status_by_path[p_str] = diag_obj
                    if alt_p:
                        status_by_path[alt_p] = diag_obj
                    if st_val in ('NEEDS_REVIEW', 'FAILED', 'NOT_FOUND', 'NO_ISBN') and p_str not in paths_set:
                        new_review_paths.append(p_str)
                        if alt_p:
                            new_review_paths.append(alt_p)
                        if st_val == 'NEEDS_REVIEW':
                            new_strict_review_paths.append(p_str)
                            if alt_p:
                                new_strict_review_paths.append(alt_p)
    except Exception:
        return

    enr_set = _ENRICHED_PATHS_CACHE.get('enriched_item_ids_set')
    enr_list = _ENRICHED_PATHS_CACHE.get('enriched_item_ids')
    rev_set = _ENRICHED_PATHS_CACHE.get('needs_review_item_ids_set')
    rev_list = _ENRICHED_PATHS_CACHE.get('needs_review_item_ids')
    strict_rev_set = _ENRICHED_PATHS_CACHE.get('strict_in_review_ids_set')
    strict_rev_list = _ENRICHED_PATHS_CACHE.get('strict_in_review_ids')
    excl_set = _ENRICHED_PATHS_CACHE.get('excluded_from_needs_enriching_ids_set')
    excl_list = _ENRICHED_PATHS_CACHE.get('excluded_from_needs_enriching_ids')

    if new_success_paths and isinstance(enr_set, set) and isinstance(enr_list, list):
        try:
            new_cids = list(
                FileLocation.objects.filter(file_path__in=new_success_paths)
                .values_list('catalog_item_id', flat=True)
            )
            for cid in new_cids:
                if cid not in enr_set:
                    enr_set.add(cid)
                    enr_list.append(cid)
                if isinstance(excl_set, set) and isinstance(excl_list, list) and cid not in excl_set:
                    excl_set.add(cid)
                    excl_list.append(cid)
                if isinstance(rev_set, set) and cid in rev_set:
                    rev_set.discard(cid)
                    if isinstance(rev_list, list):
                        try:
                            rev_list.remove(cid)
                        except ValueError:
                            pass
                if isinstance(strict_rev_set, set) and cid in strict_rev_set:
                    strict_rev_set.discard(cid)
                    if isinstance(strict_rev_list, list):
                        try:
                            strict_rev_list.remove(cid)
                        except ValueError:
                            pass
        except Exception:
            pass

    if new_ignored_paths and isinstance(rev_set, set):
        try:
            ign_cids = list(
                FileLocation.objects.filter(file_path__in=new_ignored_paths)
                .values_list('catalog_item_id', flat=True)
            )
            for cid in ign_cids:
                if cid in rev_set:
                    rev_set.discard(cid)
                    if isinstance(rev_list, list):
                        try:
                            rev_list.remove(cid)
                        except ValueError:
                            pass
                if isinstance(strict_rev_set, set) and cid in strict_rev_set:
                    strict_rev_set.discard(cid)
                    if isinstance(strict_rev_list, list):
                        try:
                            strict_rev_list.remove(cid)
                        except ValueError:
                            pass
        except Exception:
            pass

    if new_review_paths and isinstance(rev_set, set) and isinstance(rev_list, list) and isinstance(enr_set, set):
        try:
            rev_cids = list(
                FileLocation.objects.filter(file_path__in=new_review_paths)
                .values_list('catalog_item_id', flat=True)
            )
            for cid in rev_cids:
                if cid not in enr_set and cid not in rev_set:
                    rev_set.add(cid)
                    rev_list.append(cid)
        except Exception:
            pass

    if new_strict_review_paths and isinstance(strict_rev_set, set) and isinstance(strict_rev_list, list) and isinstance(enr_set, set):
        try:
            s_cids = list(
                FileLocation.objects.filter(file_path__in=new_strict_review_paths)
                .values_list('catalog_item_id', flat=True)
            )
            for cid in s_cids:
                if cid not in enr_set and cid not in strict_rev_set:
                    strict_rev_set.add(cid)
                    strict_rev_list.append(cid)
        except Exception:
            pass

    if new_excluded_paths and isinstance(excl_set, set) and isinstance(excl_list, list):
        try:
            ex_cids = list(
                FileLocation.objects.filter(file_path__in=new_excluded_paths)
                .values_list('catalog_item_id', flat=True)
            )
            for cid in ex_cids:
                if cid not in excl_set:
                    excl_set.add(cid)
                    excl_list.append(cid)
        except Exception:
            pass

    _ENRICHED_PATHS_CACHE['db_mtime'] = cur_mtime
    _ENRICHED_PATHS_CACHE['last_updated_at'] = max_ts


def get_enriched_paths_set() -> set:
    """
    Return the set of all `file_path` strings in `scanner_state.db` where `status = 'SUCCESS'`,
    cached in memory for 3600s and updated incrementally when books are enriched.
    """
    now = time.monotonic()
    has_full_cache = len(_ENRICHED_PATHS_CACHE.get('paths_set') or ()) >= 100
    if has_full_cache and now < _ENRICHED_PATHS_CACHE['expires_at']:
        _sync_incremental_enriched_changes()
        return _ENRICHED_PATHS_CACHE['paths_set']

    if has_full_cache and not _ENRICHED_PATHS_CACHE.get('_refreshing'):
        _ENRICHED_PATHS_CACHE['_refreshing'] = True
        _ENRICHED_PATHS_CACHE['expires_at'] = now + 3600.0
        _sync_incremental_enriched_changes()
        import threading
        threading.Thread(target=_rebuild_enriched_index_cache, daemon=True).start()
        return _ENRICHED_PATHS_CACHE['paths_set']

    _rebuild_enriched_index_cache()
    return _ENRICHED_PATHS_CACHE['paths_set']


def _rebuild_enriched_index_cache() -> None:
    pg_enabled = is_postgres_scanner_enabled()
    db_path = _get_scanner_db_path()
    paths_set: set = set()
    status_by_path: Dict[str, Dict[str, Any]] = {}
    review_paths: List[str] = []
    strict_review_paths: List[str] = []
    non_success_paths: List[str] = []
    cur_mtime = 0.0
    max_ts = ''
    if pg_enabled or db_path.exists():
        try:
            if not pg_enabled and db_path.exists():
                cur_mtime = db_path.stat().st_mtime
                wal_path = Path(str(db_path) + '-wal')
                if wal_path.exists():
                    cur_mtime = max(cur_mtime, wal_path.stat().st_mtime)
            conn = connect_scanner_db(db_path, timeout=5.0)
            conn.text_factory = lambda b: b.decode('utf-8', errors='replace')
            try:
                max_row = conn.execute("SELECT MAX(updated_at) FROM processed_files").fetchone()
                if max_row and max_row[0]:
                    max_ts = str(max_row[0])
            except Exception:
                try:
                    max_row = conn.execute("SELECT updated_at FROM processed_files ORDER BY id DESC LIMIT 1").fetchone()
                    if max_row and max_row[0]:
                        max_ts = str(max_row[0])
                except Exception:
                    pass
            try:
                rows = conn.execute(
                    "SELECT file_path FROM processed_files WHERE status = 'SUCCESS'"
                ).fetchall()
                for r in rows:
                    if r and r[0]:
                        p_str = str(r[0])
                        paths_set.add(p_str)
                        if p_str.startswith('/volume1/common/Media/Books/'):
                            paths_set.add('/books/' + p_str[len('/volume1/common/Media/Books/'):])
                        elif p_str.startswith('/books/'):
                            paths_set.add('/volume1/common/Media/Books/' + p_str[len('/books/'):])
            except Exception:
                pass

            diag_rows = []
            try:
                diag_rows = conn.execute(
                    """
                    SELECT file_path, status, error_message,
                           title, author, isbn,
                           orig_title, orig_author, orig_isbn,
                           match_confidence, original_metadata_json, updated_metadata_json
                    FROM processed_files
                    WHERE status != 'SUCCESS'
                    ORDER BY updated_at DESC
                    """
                ).fetchall()
            except Exception:
                try:
                    diag_rows = conn.execute(
                        "SELECT file_path, status, error_message FROM processed_files WHERE status != 'SUCCESS'"
                    ).fetchall()
                except Exception:
                    diag_rows = []
            conn.close()
            for r in diag_rows:
                if r and r[0]:
                    p_str = str(r[0])
                    st_val = str(r[1] or '').upper()
                    err_val = str(r[2] or '')
                    if p_str.startswith('/volume1/common/Media/Books/'):
                        alt_p = '/books/' + p_str[len('/volume1/common/Media/Books/'):]
                    elif p_str.startswith('/books/'):
                        alt_p = '/volume1/common/Media/Books/' + p_str[len('/books/'):]
                    else:
                        alt_p = None
                    if p_str not in paths_set:
                        non_success_paths.append(p_str)
                        if alt_p:
                            non_success_paths.append(alt_p)
                    if st_val in ('NEEDS_REVIEW', 'FAILED', 'NOT_FOUND', 'NO_ISBN', 'HAS_DRM', 'QUARANTINED'):
                        orig_m = {}
                        upd_m = {}
                        if len(r) > 10 and r[10]:
                            try:
                                orig_m = json.loads(r[10]) or {}
                            except Exception:
                                orig_m = {}
                        if len(r) > 11 and r[11]:
                            try:
                                upd_m = json.loads(r[11]) or {}
                            except Exception:
                                upd_m = {}
                        conf_pct = None
                        if len(r) > 9 and r[9] is not None:
                            try:
                                raw_c = float(r[9])
                                conf_pct = int(round(raw_c * 100)) if raw_c <= 1.0 else int(round(raw_c))
                            except Exception:
                                conf_pct = None
                        cand_auth_list = upd_m.get('authors')
                        cand_auth_str = (
                            ', '.join(str(a) for a in cand_auth_list if a)
                            if isinstance(cand_auth_list, list)
                            else str(cand_auth_list or '')
                        )
                        orig_auth_list = orig_m.get('authors')
                        orig_auth_str = (
                            ', '.join(str(a) for a in orig_auth_list if a)
                            if isinstance(orig_auth_list, list)
                            else str(orig_auth_list or '')
                        )
                        diag_obj = {
                            'status': st_val,
                            'error': err_val,
                            'message': err_val,
                            'file_path': p_str,
                            'confidence': conf_pct,
                            'candidate_title': str((r[3] if len(r) > 3 else '') or upd_m.get('title') or ''),
                            'candidate_authors': str((r[4] if len(r) > 4 else '') or cand_auth_str or ''),
                            'candidate_isbn': str((r[5] if len(r) > 5 else '') or upd_m.get('isbn13') or upd_m.get('isbn10') or ''),
                            'candidate_publisher': str(upd_m.get('publisher') or ''),
                            'candidate_pub_date': str(upd_m.get('published_date') or ''),
                            'candidate_description': _strip_html(str(upd_m.get('description') or '')),
                            'candidate_cover': str(upd_m.get('cover_url') or ''),
                            'orig_title': str((r[6] if len(r) > 6 else '') or orig_m.get('title') or ''),
                            'orig_authors': str((r[7] if len(r) > 7 else '') or orig_auth_str or ''),
                            'orig_isbn': str((r[8] if len(r) > 8 else '') or orig_m.get('isbn13') or orig_m.get('isbn10') or ''),
                            'orig_publisher': str(orig_m.get('publisher') or ''),
                            'orig_pub_date': str(orig_m.get('published_date') or ''),
                            'orig_description': _strip_html(str(orig_m.get('description') or '')),
                            'orig_cover': str(orig_m.get('cover_url') or ''),
                        }
                        status_by_path[p_str] = diag_obj
                        if alt_p:
                            status_by_path[alt_p] = diag_obj
                    if st_val in ('NEEDS_REVIEW', 'FAILED', 'NOT_FOUND', 'NO_ISBN') and p_str not in paths_set:
                        review_paths.append(p_str)
                        if alt_p:
                            review_paths.append(alt_p)
                        if st_val == 'NEEDS_REVIEW':
                            strict_review_paths.append(p_str)
                            if alt_p:
                                strict_review_paths.append(alt_p)
        except Exception:
            pass

    enr_ids_set: set = set()
    if paths_set:
        try:
            enr_list = list(paths_set)
            for i in range(0, len(enr_list), 4000):
                chunk = enr_list[i:i + 4000]
                c_ids = (
                    FileLocation.objects.filter(file_path__in=chunk)
                    .values_list('catalog_item_id', flat=True)
                )
                enr_ids_set.update(c_ids)
        except Exception:
            pass
    enr_ids = list(enr_ids_set)

    strict_rev_ids_set: set = set()
    if strict_review_paths:
        try:
            for i in range(0, len(strict_review_paths), 4000):
                chunk = strict_review_paths[i:i + 4000]
                c_ids = (
                    FileLocation.objects.filter(file_path__in=chunk)
                    .values_list('catalog_item_id', flat=True)
                )
                strict_rev_ids_set.update(c_ids)
            strict_rev_ids_set -= enr_ids_set
        except Exception:
            strict_rev_ids_set = set()
    strict_rev_ids = list(strict_rev_ids_set)

    rev_ids: List[Any] = []
    if review_paths:
        try:
            raw_rev_ids: set = set()
            for i in range(0, len(review_paths), 4000):
                chunk = review_paths[i:i + 4000]
                c_ids = (
                    FileLocation.objects.filter(file_path__in=chunk)
                    .values_list('catalog_item_id', flat=True)
                )
                raw_rev_ids.update(c_ids)
            raw_rev_ids -= enr_ids_set
            rev_ids = list(strict_rev_ids) + [cid for cid in raw_rev_ids if cid not in strict_rev_ids_set]
        except Exception:
            rev_ids = []

    non_success_ids_set: set = set()
    if non_success_paths:
        try:
            for i in range(0, len(non_success_paths), 4000):
                chunk = non_success_paths[i:i + 4000]
                c_ids = (
                    FileLocation.objects.filter(file_path__in=chunk)
                    .values_list('catalog_item_id', flat=True)
                )
                non_success_ids_set.update(c_ids)
        except Exception:
            non_success_ids_set = set()

    excl_ids_set = enr_ids_set | non_success_ids_set
    excl_ids = list(excl_ids_set)

    _ENRICHED_PATHS_CACHE['db_mtime'] = cur_mtime
    _ENRICHED_PATHS_CACHE['last_updated_at'] = max_ts
    _ENRICHED_PATHS_CACHE['paths_set'] = paths_set
    _ENRICHED_PATHS_CACHE['status_by_path'] = status_by_path
    _ENRICHED_PATHS_CACHE['enriched_item_ids'] = enr_ids
    _ENRICHED_PATHS_CACHE['enriched_item_ids_set'] = enr_ids_set
    _ENRICHED_PATHS_CACHE['needs_review_item_ids'] = rev_ids
    _ENRICHED_PATHS_CACHE['needs_review_item_ids_set'] = set(rev_ids)
    _ENRICHED_PATHS_CACHE['strict_in_review_ids'] = strict_rev_ids
    _ENRICHED_PATHS_CACHE['strict_in_review_ids_set'] = strict_rev_ids_set
    _ENRICHED_PATHS_CACHE['excluded_from_needs_enriching_ids'] = excl_ids
    _ENRICHED_PATHS_CACHE['excluded_from_needs_enriching_ids_set'] = excl_ids_set
    _ENRICHED_PATHS_CACHE['expires_at'] = time.monotonic() + 3600.0
    _ENRICHED_PATHS_CACHE['_refreshing'] = False
    try:
        summary_file = Path('/tmp/mylibrary_summary_counts.json')
        existing = {}
        if summary_file.exists():
            try:
                existing = json.loads(summary_file.read_text(encoding='utf-8')) or {}
            except Exception:
                existing = {}
        existing['enriched_count'] = len(enr_ids)
        summary_file.write_text(json.dumps(existing), encoding='utf-8')
    except Exception:
        pass


def get_enriched_item_ids_index() -> Dict[str, List[Any]]:
    """
    Return cached lists of CatalogItem UUIDs for:
      - `enriched_ids`: items with at least one file marked `status = 'SUCCESS'` in `scanner_state.db`
      - `needs_review_ids`: items with a file marked `status IN ('NEEDS_REVIEW', 'FAILED', 'NOT_FOUND', 'NO_ISBN')`
      - `strict_in_review_ids`: items with `status = 'NEEDS_REVIEW'` (ordered first in In Review)
      - `excluded_from_needs_enriching_ids`: all items already in `processed_files` (`SUCCESS` + `NEEDS_REVIEW` + `IGNORED` + etc.)
    """
    get_enriched_paths_set()
    if (
        _ENRICHED_PATHS_CACHE.get('enriched_item_ids') is not None
        and _ENRICHED_PATHS_CACHE.get('needs_review_item_ids') is not None
        and _ENRICHED_PATHS_CACHE.get('excluded_from_needs_enriching_ids') is not None
    ):
        return {
            'enriched_ids': _ENRICHED_PATHS_CACHE['enriched_item_ids'],
            'needs_review_ids': _ENRICHED_PATHS_CACHE['needs_review_item_ids'],
            'strict_in_review_ids': _ENRICHED_PATHS_CACHE.get('strict_in_review_ids') or [],
            'excluded_from_needs_enriching_ids': _ENRICHED_PATHS_CACHE['excluded_from_needs_enriching_ids'],
        }
    _rebuild_enriched_index_cache()
    return {
        'enriched_ids': _ENRICHED_PATHS_CACHE.get('enriched_item_ids') or [],
        'needs_review_ids': _ENRICHED_PATHS_CACHE.get('needs_review_item_ids') or [],
        'strict_in_review_ids': _ENRICHED_PATHS_CACHE.get('strict_in_review_ids') or [],
        'excluded_from_needs_enriching_ids': _ENRICHED_PATHS_CACHE.get('excluded_from_needs_enriching_ids') or [],
    }


def get_file_enrich_diagnostic(file_path: str) -> Optional[Dict[str, str]]:
    """
    Return `{'status': str, 'error': str}` if `file_path` has a non-SUCCESS entry
    (`FAILED`, `NEEDS_REVIEW`, `NOT_FOUND`, `NO_ISBN`, `HAS_DRM`, `QUARANTINED`) in `scanner_state.db`.
    """
    if not file_path:
        return None
    norm = str(file_path).strip().replace('\\', '/')
    get_enriched_paths_set()
    s_map = _ENRICHED_PATHS_CACHE.get('status_by_path') or {}
    if norm in s_map:
        return s_map[norm]
    if norm.startswith('/books/'):
        alt = '/volume1/common/Media/Books/' + norm[len('/books/'):]
        if alt in s_map:
            return s_map[alt]
    return None


def _record_in_memory_diag(file_loc: FileLocation, phys_path: Optional[Path], status: str, error_msg: str) -> None:
    s_map = _ENRICHED_PATHS_CACHE.get('status_by_path')
    if not isinstance(s_map, dict):
        return
    diag_obj = {'status': status, 'error': error_msg}
    for p in (file_loc.file_path if file_loc else None, str(phys_path) if phys_path else None):
        if p:
            s_map[str(p)] = diag_obj


def is_file_enriched(file_path: str) -> bool:
    """
    Check whether a physical book file path is already marked `status = 'SUCCESS'` in `scanner_state.db`.
    """
    if not file_path:
        return False
    norm = str(file_path).strip().replace('\\', '/')
    enr_set = get_enriched_paths_set()
    if norm in enr_set:
        return True
    phys = _resolve_physical_file_path(norm)
    if phys and str(phys) in enr_set:
        return True
    return False


def _load_scanner_epubmeta_config() -> Dict[str, Any]:
    """
    Load the `epubmeta` section from `nas_scanner/config.json` or `scanner/config.json`.
    """
    base_dir = Path(getattr(settings, 'BASE_DIR', '.'))
    for sub in ('nas_scanner', 'scanner'):
        cfg_file = base_dir / sub / 'config.json'
        if cfg_file.exists():
            try:
                data = json.loads(cfg_file.read_text(encoding='utf-8'))
                if isinstance(data.get('epubmeta'), dict):
                    return data['epubmeta']
            except Exception:
                continue
    return {}


def _apply_enriched_metadata_to_catalog_item(
    item: CatalogItem,
    file_loc: FileLocation,
    target_meta: Any,
    isbn_fallback: Optional[str] = None,
    file_sha256: Optional[str] = None,
    cover_thumbnail: Optional[str] = None,
    phys_path: Optional[Path] = None,
) -> None:
    """
    Persist newly enriched metadata (`target_meta`) from `epubmeta` onto both `FileLocation`
    and `CatalogItem` in PostgreSQL BookDB.
    """
    try:
        loc_fields = []
        if file_sha256 and file_sha256 != file_loc.file_hash:
            file_loc.file_hash = file_sha256[:64]
            loc_fields.append('file_hash')
        if phys_path and phys_path.exists():
            new_sz = phys_path.stat().st_size
            if new_sz and new_sz != file_loc.file_size:
                file_loc.file_size = new_sz
                loc_fields.append('file_size')
        if loc_fields:
            file_loc.save(update_fields=loc_fields)
    except Exception:
        pass

    try:
        new_title = _clean_single_line(getattr(target_meta, 'title', None))
        if new_title:
            item.title = new_title[:512]
        raw_authors = getattr(target_meta, 'authors', None) or []
        if isinstance(raw_authors, list) and raw_authors:
            new_authors = _clean_single_line(', '.join(str(a) for a in raw_authors if a))
            if new_authors:
                item.authors = new_authors[:512]
        new_isbn = _clean_single_line(
            getattr(target_meta, 'isbn13', None)
            or getattr(target_meta, 'isbn10', None)
            or isbn_fallback
        )
        if new_isbn:
            item.isbn = new_isbn[:32]
        new_asin = _clean_single_line(getattr(target_meta, 'asin', None))
        if new_asin:
            item.asin = new_asin[:32]
        new_pub = _clean_single_line(getattr(target_meta, 'publisher', None))
        if new_pub:
            item.publisher = new_pub[:256]
        new_desc = getattr(target_meta, 'description', None)
        if new_desc and str(new_desc).strip():
            item.description = str(new_desc).strip()
        new_cover_url = _clean_single_line(getattr(target_meta, 'cover_url', None))
        had_orig_cover = bool(
            (item.cover_thumbnail and str(item.cover_thumbnail).startswith('data:image'))
            or item.cover_image_url
        )
        # Preserve the original cover unless it was missing; only replace if no original cover existed
        if cover_thumbnail and str(cover_thumbnail).startswith('data:image'):
            if not (item.cover_thumbnail and str(item.cover_thumbnail).startswith('data:image')):
                item.cover_thumbnail = cover_thumbnail
        if new_cover_url and not had_orig_cover:
            item.cover_image_url = new_cover_url[:1024]
        if item.pub_date and getattr(item.pub_date, 'year', 2000) < 1000:
            item.pub_date = None
        raw_pub_date = _clean_single_line(getattr(target_meta, 'published_date', None))
        if raw_pub_date:
            m_dt = re.match(r'^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?', raw_pub_date)
            if m_dt and int(m_dt.group(1)) >= 1000:
                yr = int(m_dt.group(1))
                mo = int(m_dt.group(2) or 1)
                dy = int(m_dt.group(3) or 1)
                try:
                    item.pub_date = datetime.date(yr, min(max(mo, 1), 12), min(max(dy, 1), 28))
                except Exception:
                    pass
            else:
                m_yr = re.search(r'\b(1[89]\d{2}|20\d{2})\b', raw_pub_date)
                if m_yr:
                    try:
                        item.pub_date = datetime.date(int(m_yr.group(1)), 1, 1)
                    except Exception:
                        pass
        item.save()
        try:
            clear_book_bad_metadata(item.id)
        except Exception:
            pass
        return merge_duplicate_catalog_items_for_item(item)
    except Exception:
        return []


def merge_duplicate_catalog_items_for_item(item: CatalogItem) -> List[str]:
    """
    Find any duplicate `CatalogItem` rows in BookDB that share the same `(title, authors)`
    (where `authors` is not `'Unknown Author'`) or the same valid ISBN + base title,
    merge their `FileLocation`s onto `item`, copy any missing metadata/cover fields onto `item`,
    and delete the duplicate `CatalogItem` rows. Returns list of deleted duplicate UUID strings.
    """
    merged_ids: List[str] = []
    if not item or not item.title:
        return merged_ids
    authors_clean = (item.authors or '').strip()
    if not authors_clean or authors_clean.lower() in ('unknown', 'unknown author', '..', '.'):
        return merged_ids

    try:
        from django.db.models import Q
        q_filter = Q(title__iexact=item.title.strip(), authors__iexact=authors_clean)
        clean_isbn = re.sub(r'[^0-9Xx]', '', str(item.isbn or '')).upper()
        if len(clean_isbn) in (10, 13):
            q_filter = q_filter | Q(isbn__iexact=item.isbn.strip(), authors__iexact=authors_clean)

        dups = list(
            CatalogItem.objects.filter(item_type=item.item_type)
            .filter(q_filter)
            .exclude(id=item.id)
            .prefetch_related('files')[:20]
        )
        if not dups:
            return merged_ids

        norm_base = _norm_lookup_str(item.title.split(':')[0])
        item_updated = False
        existing_paths = {f.file_path for f in item.files.all() if f.file_path}

        for other in dups:
            other_base = _norm_lookup_str((other.title or '').split(':')[0])
            if norm_base and other_base and norm_base != other_base:
                continue
            for field in ('isbn', 'asin', 'publisher', 'pub_date', 'description', 'cover_thumbnail', 'cover_image_url'):
                if not getattr(item, field, None) and getattr(other, field, None):
                    setattr(item, field, getattr(other, field))
                    item_updated = True
            for f_loc in list(other.files.all()):
                if f_loc.file_path and f_loc.file_path in existing_paths:
                    f_loc.delete()
                else:
                    f_loc.catalog_item = item
                    f_loc.save(update_fields=['catalog_item'])
                    if f_loc.file_path:
                        existing_paths.add(f_loc.file_path)
            merged_ids.append(str(other.id))
            try:
                if str(other.id) in set(get_interesting_item_ids()):
                    set_book_interesting(str(item.id), True)
                    clear_book_interesting(str(other.id))
            except Exception:
                pass
            try:
                for set_key, list_key in (
                    ('enriched_item_ids_set', 'enriched_item_ids'),
                    ('needs_review_item_ids_set', 'needs_review_item_ids'),
                    ('strict_in_review_ids_set', 'strict_in_review_ids'),
                    ('excluded_from_needs_enriching_ids_set', 'excluded_from_needs_enriching_ids'),
                ):
                    s = _ENRICHED_PATHS_CACHE.get(set_key)
                    if isinstance(s, set) and other.id in s:
                        s.discard(other.id)
                        lst = _ENRICHED_PATHS_CACHE.get(list_key)
                        if isinstance(lst, list):
                            try:
                                lst.remove(other.id)
                            except ValueError:
                                pass
            except Exception:
                pass
            other.delete()

        if item_updated:
            item.save()
    except Exception:
        pass
    return merged_ids


def merge_all_enriched_duplicate_catalog_items() -> Dict[str, int]:
    """
    Scan BookDB for duplicate `(title, authors, item_type)` groups where at least one file
    has been enriched (or where enriched metadata produced identical title + author cards),
    and merge their `FileLocation`s onto a single canonical `CatalogItem`.
    """
    from django.db.models import Count

    groups_merged = 0
    items_deleted = 0
    files_reassigned = 0

    try:
        dup_groups = list(
            CatalogItem.objects.exclude(authors='Unknown Author')
            .exclude(authors='')
            .values('title', 'authors', 'item_type')
            .annotate(cnt=Count('id'))
            .filter(cnt__gt=1)
            .order_by('-cnt')[:500]
        )
        for grp in dup_groups:
            items = list(
                CatalogItem.objects.filter(
                    title=grp['title'],
                    authors=grp['authors'],
                    item_type=grp['item_type'],
                ).prefetch_related('files')
            )
            if len(items) < 2:
                continue
            # Sort so the richest item (with cover_thumbnail, description, isbn) is kept
            items.sort(
                key=lambda it: (
                    bool(it.cover_thumbnail),
                    bool(it.description),
                    bool(it.isbn),
                    len(list(it.files.all())),
                ),
                reverse=True,
            )
            primary = items[0]
            existing_paths = {f.file_path for f in primary.files.all() if f.file_path}
            primary_changed = False

            for dup in items[1:]:
                for field in ('isbn', 'asin', 'publisher', 'pub_date', 'description', 'cover_thumbnail', 'cover_image_url'):
                    if not getattr(primary, field, None) and getattr(dup, field, None):
                        setattr(primary, field, getattr(dup, field))
                        primary_changed = True
                for f_loc in list(dup.files.all()):
                    if f_loc.file_path and f_loc.file_path in existing_paths:
                        f_loc.delete()
                    else:
                        f_loc.catalog_item = primary
                        f_loc.save(update_fields=['catalog_item'])
                        files_reassigned += 1
                        if f_loc.file_path:
                            existing_paths.add(f_loc.file_path)
                dup.delete()
                items_deleted += 1

            if primary_changed:
                primary.save()
            groups_merged += 1
    except Exception:
        pass

    return {
        'groups_merged': groups_merged,
        'items_deleted': items_deleted,
        'files_reassigned': files_reassigned,
    }


_MAG_BOOK_PUBLISHERS_RE = re.compile(
    r'\b(packt|apress|o\'reilly|addison-wesley|wiley|manning|sams|no starch(?: press)?|'
    r'pragmatic(?: bookshelf)?|mcgraw-hill|pearson|springer|sitepoint|crc press|syngress|'
    r'wrox|peachpit|cisco press|microsoft press|redbooks|for dummies|'
    r'head first|in a nutshell|unleashed|pocket reference|friendsofed|'
    r'penguin|random house|harpercollins|simon & schuster|macmillan|hachette|tor books|orbit|baen|del rey|avon books|bantam|Ace Books|Gollancz)\b',
    re.IGNORECASE,
)

_MAG_KNOWN_PATTERNS = [
    (re.compile(r'\bpc[\s\-]*games(?:\s*hardware)?\b', re.I), 'PC Games'),
    (re.compile(r'\bpc\s*gamer\b', re.I), 'PC Gamer'),
    (re.compile(r'\bpc\s*mag(?:azine)?\b', re.I), 'PC Magazine'),
    (re.compile(r'\bpc\s*world\b', re.I), 'PC World'),
    (re.compile(r'\bpc\s*pro\b', re.I), 'PC Pro'),
    (re.compile(r'\bpc\s*powerplay\b', re.I), 'PC PowerPlay'),
    (re.compile(r'\bcustom\s*pc\b', re.I), 'Custom PC'),
    (re.compile(r'\bmaximum\s*pc\b', re.I), 'Maximum PC'),
    (re.compile(r'\bcomputer\s*active\b|\bcomputeractive\b', re.I), 'Computeractive'),
    (re.compile(r'\bcomputer\s*shopper\b', re.I), 'Computer Shopper'),
    (re.compile(r'\bpersonal\s*computer\s*world\b', re.I), 'Personal Computer World'),
    (re.compile(r'\bmicro\s*mart\b', re.I), 'Micro Mart'),
    (re.compile(r'\bweb\s*user\b', re.I), 'Web User'),
    (re.compile(r'\btech\s*advisor\b', re.I), 'Tech Advisor'),
    (re.compile(r'\bchip\s*(?:19\d\d|20\d\d|\d{2}[-/]\d{2,4}|magazin|sonderheft)\b', re.I), 'CHIP Magazine'),
    (re.compile(r'\b(?:der\s+)?spiegel\s*(?:\d{1,2}[-/]\d{2,4}|19\d\d|20\d\d)\b', re.I), 'Der Spiegel'),
    (re.compile(r'\blinux\s*(?:format|magazine|mag|user|journal|pro|voice)\b', re.I), 'Linux Magazine'),
    (re.compile(r'\bubuntu\s*user\b', re.I), 'Ubuntu User'),
    (re.compile(r'\bfull\s*circle(?:\s*magazine)?\b', re.I), 'Full Circle Magazine'),
    (re.compile(r'\bmac(?:world|format|life|user)\b', re.I), 'Mac Magazine'),
    (re.compile(r'\bicreate\b', re.I), 'iCreate'),
    (re.compile(r'\b(?:the\s*)?magpi\b', re.I), 'The MagPi'),
    (re.compile(r'\bhackspace\b', re.I), 'HackSpace'),
    (re.compile(r'\bwireframe\b', re.I), 'Wireframe'),
    (re.compile(r'\bmake\s*magazine\b', re.I), 'Make Magazine'),
    (re.compile(r'\bnuts\s*(?:&|and)\s*volts\b', re.I), 'Nuts & Volts'),
    (re.compile(r'\bcircuit\s*cellar\b', re.I), 'Circuit Cellar'),
    (re.compile(r'\belektor\b', re.I), 'Elektor'),
    (re.compile(r'\b2600(?:\s*the\s*hacker\s*quarterly)?\b', re.I), '2600 The Hacker Quarterly'),
    (re.compile(r'\b(?:phrack|hakin9|pentest\s*magazine|code\s*magazine|smashing\s*magazine|3d\s*world|imaginefx|computer\s*arts|computer\s*music|sound\s*on\s*sound|msdn\s*magazine|visual\s*studio\s*magazine|retro\s*gamer|amiga\s*format|st\s*format|compute!?\s*magazine|byte\s*magazine|it[.\s_-]*week(?:\b|[.\s_-]*magazine)|\d{3}_itwk_)\b', re.I), 'IT Week / Computer Magazine'),
    (re.compile(r'\b(?:scientific\s*american|popular\s*science|popular\s*mechanics|national\s*geographic|new\s*scientist|the\s*economist|newsweek|rolling\s*stone|game\s*informer|edge\s*magazine|nintendo\s*power|white\s*dwarf|dragon\s*magazine|dungeon\s*magazine)\b', re.I), 'Magazine'),
]

_MAG_ISSUE_DATE_RE = re.compile(
    r'(?:\b(?:0?[1-9]|1[0-2])\s*[/\-]\s*(?:19\d\d|20\d\d|\d{2})\b|'
    r'\b(?:19\d\d|20\d\d)\s*[/\-]\s*(?:0?[1-9]|1[0-2])\b|'
    r'\b(?:0?[1-9]|[1-4]\d|5[0-3])\s*-\s*(?:19\d\d|20\d\d)\b|'
    r'\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)[.\s_-]+(?:\d{1,2}[.\s,_-]+)?(?:19\d\d|20\d\d|\d{2})\b|'
    r'\b\d{1,2}\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s*(?:19\d\d|20\d\d|\d{2})\b|'
    r'\b(?:issue|iss|no|nr|heft|ausgabe)\.?\s*#?\d+.*(?:19\d\d|20\d\d)|'
    r'\b(?:sonderbeilage|sonderheft|truepdf|emag|downmagaz|freemagazines)\b)',
    re.IGNORECASE,
)


def _get_tomags_dir() -> Optional[Path]:
    """
    Resolve `/toMags` (inside Docker) or `/volume1/common/Media/toMags` (on NAS host)
    or `U:\\Media\\toMags` (on Windows).
    """
    for cand_str in ('/toMags', '/volume1/common/Media/toMags', r'U:\Media\toMags'):
        p = Path(cand_str)
        try:
            if p.exists() and p.is_dir():
                return p
        except Exception:
            continue
    return None


def detect_magazine_publication(
    title: str = '',
    authors: str = '',
    isbn: str = '',
    file_path: str = '',
    publisher: str = '',
) -> Optional[str]:
    """
    Determine if an item or file is a Magazine / Periodical that belongs in `toMags`.
    Returns the publication/reason string if it is a magazine, or `None` if it is a book.
    """
    clean_isbn = re.sub(r'[^0-9Xx]', '', str(isbn or ''))
    # Standard books have 978 or 979 Bookland ISBNs (whereas 977 is ISSN for periodicals)
    if clean_isbn.startswith(('978', '979')) and len(clean_isbn) == 13:
        return None
    if len(clean_isbn) == 10 and not clean_isbn.startswith('977'):
        return None

    fname = os.path.basename(file_path or '')
    combined = f"{title or ''} | {fname} | {publisher or ''}"

    if _MAG_BOOK_PUBLISHERS_RE.search(combined):
        return None

    if clean_isbn.startswith('977'):
        return title or fname or 'ISSN Periodical'

    for rx, pub_label in _MAG_KNOWN_PATTERNS:
        if rx.search(title or '') or rx.search(fname):
            return pub_label

    in_mag_folder = 'computer mags' in (file_path or '').lower() or '/mags/' in (file_path or '').lower()
    has_issue_date = bool(_MAG_ISSUE_DATE_RE.search(title or '') or _MAG_ISSUE_DATE_RE.search(fname))
    author_is_placeholder = (authors or '').strip().lower() in ('', '..', '.', 'unknown', 'unknown author', 'n/a', 'various')

    if has_issue_date and (in_mag_folder or author_is_placeholder or re.search(r'\b(magazine|magazin|journal|zeitschrift|quarterly|weekly|monthly)\b', combined, re.I)):
        return title or os.path.splitext(fname)[0] or 'Periodical Issue'

    return None


def _unique_tomags_dest(tomags_dir: Path, filename: str) -> Path:
    dest = tomags_dir / filename
    if not dest.exists():
        return dest
    stem = dest.stem
    suffix = dest.suffix
    counter = 1
    while True:
        cand = tomags_dir / f"{stem} ({counter}){suffix}"
        if not cand.exists():
            return cand
        counter += 1


def move_file_location_to_tomags(file_loc: FileLocation) -> Dict[str, Any]:
    """
    Move a single physical file from `/books/...` into `/toMags/` (`/volume1/common/Media/toMags`),
    remove its old `/books/...` state from `scanner_state.db`, and delete its `FileLocation`
    (and parent `CatalogItem` if no files remain).
    """
    tomags_dir = _get_tomags_dir()
    if not tomags_dir:
        return {
            'ok': False,
            'status': 'TOMAGS_NOT_MOUNTED',
            'message': 'toMags directory (/toMags or /volume1/common/Media/toMags) is not mounted.',
        }

    phys_path = _resolve_physical_file_path(file_loc.file_path)
    if not phys_path or not phys_path.exists():
        return {
            'ok': False,
            'status': 'NOT_FOUND',
            'message': f'Physical file not found on NAS: {file_loc.file_path}',
        }

    item = file_loc.catalog_item
    old_file_path = file_loc.file_path
    dest_file = _unique_tomags_dest(tomags_dir, phys_path.name)
    try:
        shutil.move(str(phys_path), str(dest_file))
        try:
            os.chmod(str(dest_file), 0o666)
        except Exception:
            pass
    except Exception as mv_err:
        return {
            'ok': False,
            'status': 'MOVE_ERROR',
            'message': f'Failed to move {phys_path.name} to toMags: {mv_err}',
        }

    # Remove old /books/... record from scanner_state.db / BookDB so scanner doesn't treat it as missing/book
    db_path = _get_scanner_db_path()
    if is_postgres_scanner_enabled() or db_path.exists():
        try:
            conn = connect_scanner_db(db_path, timeout=10.0)
            conn.execute("DELETE FROM processed_files WHERE file_path = ?", (old_file_path,))
            conn.execute("DELETE FROM file_states WHERE filepath = ?", (old_file_path,))
            conn.commit()
            conn.close()
        except Exception:
            pass

    file_id_str = str(file_loc.id)
    item_id_str = str(item.id) if item else ''
    file_loc.delete()

    item_deleted = False
    if item:
        if not FileLocation.objects.filter(catalog_item_id=item.id).exists():
            CatalogItem.objects.filter(id=item.id).delete()
            item_deleted = True

    return {
        'ok': True,
        'status': 'MOVED_TO_MAGS',
        'moved_to_tomags': True,
        'item_deleted': item_deleted,
        'item_id': item_id_str,
        'file_id': file_id_str,
        'moved_files': [dest_file.name],
        'dest_path': f'/toMags/{dest_file.name}',
        'message': f"Moved '{phys_path.name}' to toMags ({dest_file.name}).",
    }


def move_catalog_item_to_tomags(item: CatalogItem, only_detected_mags: bool = False) -> Dict[str, Any]:
    """
    Move all physical files (or all magazine-detected files when `only_detected_mags=True`)
    of a `CatalogItem` to `/toMags/` and clean up `scanner_state.db` and `BookDB`.
    """
    tomags_dir = _get_tomags_dir()
    if not tomags_dir:
        return {
            'ok': False,
            'status': 'TOMAGS_NOT_MOUNTED',
            'message': 'toMags directory (/toMags or /volume1/common/Media/toMags) is not mounted.',
        }

    files_list = list(item.files.all())
    if not files_list:
        return {
            'ok': False,
            'status': 'NO_FILES',
            'message': 'This catalog item has no physical files to move.',
        }

    moved_names: List[str] = []
    errors: List[str] = []
    item_id_str = str(item.id)

    for f_loc in files_list:
        if only_detected_mags:
            pub = detect_magazine_publication(
                title=item.title,
                authors=item.authors,
                isbn=item.isbn or '',
                file_path=f_loc.file_path or f_loc.file_name or '',
                publisher=item.publisher or '',
            )
            if not pub:
                continue
        res = move_file_location_to_tomags(f_loc)
        if res.get('ok'):
            moved_names.extend(res.get('moved_files', []))
        else:
            errors.append(res.get('message', 'Move failed'))

    if not FileLocation.objects.filter(catalog_item_id=item_id_str).exists():
        CatalogItem.objects.filter(id=item_id_str).delete()
    item_exists = CatalogItem.objects.filter(id=item_id_str).exists()
    if moved_names:
        return {
            'ok': True,
            'status': 'MOVED_TO_MAGS',
            'moved_to_tomags': True,
            'item_deleted': not item_exists,
            'item_id': item_id_str,
            'moved_files': moved_names,
            'message': f"Moved {len(moved_names)} magazine file(s) to toMags: {', '.join(moved_names[:4])}{'...' if len(moved_names) > 4 else ''}",
        }

    return {
        'ok': False,
        'status': 'MOVE_FAILED',
        'message': '; '.join(errors) if errors else 'No matching magazine files were moved.',
    }


def enrich_file_location_on_demand(
    file_loc: FileLocation,
    force: bool = False,
    re_enrich: bool = False,
    hint_title: str = '',
    hint_authors: str = '',
    hint_isbn: str = '',
    ai_image_data_uris: Optional[List[str]] = None,
    force_ai_vision: bool = False,
    review_only: bool = False,
) -> Dict[str, Any]:
    """
    Perform on-demand `epubmeta` enrichment for a single `FileLocation` (`.epub` or `.pdf`):
      1. Inspects embedded metadata, front-matter ISBN/ASIN regex, local metadata cache,
         OpenLibrary, Google Books, Amazon/Goodreads, and Gemini AI fallback.
      2. Rewrites the `.epub` OPF / `.pdf` XMP metadata on disk (with backup in `/books_bad/bak`).
      3. Records `status = 'SUCCESS'` in `scanner_state.db` (`processed_files` & `file_states`).
      4. Updates `CatalogItem` and `FileLocation` in PostgreSQL `BookDB`.
    """
    import sys

    item = file_loc.catalog_item
    phys_path = _resolve_physical_file_path(file_loc.file_path)
    if not phys_path or not phys_path.exists():
        return {
            'ok': False,
            'status': 'NOT_MOUNTED',
            'already_enriched': False,
            'message': f'Physical file not accessible on NAS: {file_loc.file_path}',
        }

    ext = phys_path.suffix.lower()
    if ext not in ENRICHABLE_EXTENSIONS:
        return {
            'ok': False,
            'status': 'UNSUPPORTED_FORMAT',
            'already_enriched': False,
            'message': f'On-demand enrichment supports {", ".join(ENRICHABLE_EXTENSIONS)} files (got {ext}).',
        }

    # Pre-check if this file is a Magazine / Periodical BEFORE querying external book providers!
    # This prevents magazines like IT.Week.Magazine.February.13.2006.PDF.eBook-YYePG.pdf from matching "So B. It".
    pre_mag_pub = detect_magazine_publication(
        title=hint_title or (item.title if item else ''),
        authors=hint_authors or (item.authors if item else ''),
        isbn=hint_isbn or (item.isbn if item else '') or '',
        file_path=file_loc.file_path or '',
        publisher=(item.publisher if item else '') or '',
    )
    if pre_mag_pub and _get_tomags_dir():
        clean_mag_stem = os.path.splitext(os.path.basename(file_loc.file_path or phys_path.name))[0]
        clean_mag_stem = re.sub(r'\.(?:PDF|EPUB|MOBI|AZW3|eBook)(?:-[A-Za-z0-9]+)?$', '', clean_mag_stem, flags=re.I).replace('.', ' ').strip()
        mv_res = move_file_location_to_tomags(file_loc)
        if mv_res.get('ok'):
            mv_res['title'] = f"[Moved to toMags] {clean_mag_stem or (item.title if item else phys_path.name)}"
            mv_res['authors'] = pre_mag_pub
            return mv_res

    if not force and not re_enrich and is_file_enriched(file_loc.file_path):
        mag_pub = detect_magazine_publication(
            title=item.title,
            authors=item.authors,
            isbn=item.isbn or '',
            file_path=file_loc.file_path or '',
            publisher=item.publisher or '',
        )
        if mag_pub and _get_tomags_dir():
            mv_res = move_file_location_to_tomags(file_loc)
            if mv_res.get('ok'):
                return mv_res
        item_has_complete_meta = bool(
            item.title
            and not re.match(r'^untitled\b', (item.title or '').strip(), re.IGNORECASE)
            and item.authors
            and item.authors.strip().lower() not in ('', 'unknown', 'unknown author', '..', '.')
            and (item.isbn or item.asin)
            and item.description
            and item.publisher
            and item.pub_date
            and (item.cover_thumbnail or item.cover_image_url)
        )
        if item_has_complete_meta:
            merged_ids = merge_duplicate_catalog_items_for_item(item)
            return {
                'ok': True,
                'status': 'SUCCESS',
                'already_enriched': True,
                'unchanged': True,
                'message': f'Already enriched by epubmeta: {item.title} by {item.authors}',
                'item_id': str(item.id),
                'file_id': str(file_loc.id),
                'title': item.title,
                'authors': item.authors,
                'isbn': item.isbn or '',
                'asin': item.asin or '',
                'publisher': item.publisher or '',
                'pub_date': str(item.pub_date) if item.pub_date else '',
                'description': _strip_html(item.description or ''),
                'cover_url': item.cover_thumbnail or item.cover_image_url or f'/mylibrary/opds/cover/{item.id}.jpg',
                'tags': get_file_tags(file_loc.file_path),
                'merged_item_ids': merged_ids,
            }

    base_dir = Path(getattr(settings, 'BASE_DIR', '.'))
    for sub in ('scanner', 'nas_scanner'):
        cand_dir = str((base_dir / sub).resolve())
        if os.path.isdir(cand_dir):
            if cand_dir in sys.path:
                sys.path.remove(cand_dir)
            sys.path.insert(0, cand_dir)

    try:
        from epubmeta.external_dbs import ScannerStateBridge
        from epubmeta.isbn.ai_extractor import GeminiBookExtractor
        from epubmeta.processor import BookProcessor
        from epubmeta.providers.aggregator import MetadataAggregator
        from epubmeta.providers.amazon_goodreads import AmazonGoodreadsProvider
        from epubmeta.providers.google_books import GoogleBooksProvider
        from epubmeta.providers.open_library import OpenLibraryProvider
        from epubmeta.state import StateTracker
    except Exception as imp_err:
        return {
            'ok': False,
            'status': 'IMPORT_ERROR',
            'already_enriched': False,
            'message': f'Could not import epubmeta in web container: {imp_err}',
        }

    epubmeta_cfg = _load_scanner_epubmeta_config()
    db_path = _get_scanner_db_path()
    tracker = StateTracker(db_path)
    scanner_bridge = ScannerStateBridge(db_path)

    gemini_key = epubmeta_cfg.get('gemini_api_key') or os.environ.get('GEMINI_API_KEY')
    google_key = epubmeta_cfg.get('google_api_key') or os.environ.get('GOOGLE_BOOKS_API_KEY')

    global _GOOGLE_BOOKS_COOLDOWN_UNTIL
    now_mono = time.monotonic()
    gb_provider = None
    if now_mono >= _GOOGLE_BOOKS_COOLDOWN_UNTIL:
        gb_provider = GoogleBooksProvider(api_key=google_key, raise_on_quota=False)
    providers = [
        OpenLibraryProvider(raise_on_quota=False),
        *([gb_provider] if gb_provider is not None else []),
        AmazonGoodreadsProvider(),
    ]
    aggregator = MetadataAggregator(providers=providers)
    gemini_model = epubmeta_cfg.get('gemini_model') or os.environ.get('GEMINI_MODEL') or 'gemini-3.5-flash-lite'
    ai = GeminiBookExtractor(
        api_key=gemini_key,
        model=gemini_model,
        raise_on_quota=False,
        state_tracker=tracker,
        max_daily_cost_usd=epubmeta_cfg.get('max_daily_cost_usd'),
    )

    backup_dir_str = epubmeta_cfg.get('backup_dir') or '/books_bad/bak'
    quarantine_dir_str = epubmeta_cfg.get('quarantine_dir') or '/books_bad/quarantine'
    backup_dir = Path(backup_dir_str) if os.path.exists(os.path.dirname(backup_dir_str)) else None
    quarantine_dir = Path(quarantine_dir_str) if os.path.exists(os.path.dirname(quarantine_dir_str)) else None

    min_conf = 0.35 if force else float(epubmeta_cfg.get('min_confidence', 0.50))
    processor = BookProcessor(
        aggregator=aggregator,
        ai_extractor=ai,
        state_tracker=tracker,
        quarantine_dir=quarantine_dir,
        backup_dir=backup_dir,
        scan_root=Path('/books') if Path('/books').exists() else phys_path.parent,
        dry_run=False,
        backup=bool(epubmeta_cfg.get('backup', True) and backup_dir is not None),
        update_cover=bool(epubmeta_cfg.get('update_cover', False)),
        min_confidence=min_conf,
        pdf_require_isbn=False,
        scanner_bridge=scanner_bridge,
    )

    if re_enrich or force or force_ai_vision:
        processor.skip_cache = True
        try:
            tracker.requeue_file(str(phys_path.resolve()))
            if file_loc.file_path:
                tracker.requeue_file(str(file_loc.file_path))
        except Exception:
            pass

    try:
        ai_extracted_info: Dict[str, Any] = {}
        precomputed_ai_res: Optional[Dict[str, Any]] = None
        if force_ai_vision or ai_image_data_uris:
            img_bytes_list: List[bytes] = []
            for uri in (ai_image_data_uris or []):
                u_str = str(uri or '').strip()
                if u_str.startswith('data:image') and ',' in u_str:
                    try:
                        img_bytes_list.append(base64.b64decode(u_str.split(',', 1)[1]))
                    except Exception:
                        pass
            from epubmeta.readers import get_reader as _get_rdr
            tmp_reader = _get_rdr(phys_path)
            if not img_bytes_list and tmp_reader:
                try:
                    img_bytes_list = tmp_reader.extract_front_images(max_pages=3) or []
                except Exception:
                    img_bytes_list = []
                if not img_bytes_list:
                    try:
                        cov_b = tmp_reader.extract_cover_bytes()
                        if cov_b and len(cov_b) >= 1000:
                            img_bytes_list = [cov_b]
                    except Exception:
                        pass
            front_txt_snippet = f"FILE PATH CONTEXT: {file_loc.file_path or str(phys_path)}\n"
            if tmp_reader:
                try:
                    front_txt_snippet += (tmp_reader.extract_front_text(max_pages=6) or '')[:6000]
                except Exception:
                    pass
            if ai.is_available and (img_bytes_list or front_txt_snippet):
                try:
                    ai_res = ai.extract(text=front_txt_snippet or None, images=img_bytes_list or None)
                    if ai_res and (ai_res.get('title') or ai_res.get('isbn13') or ai_res.get('authors')):
                        precomputed_ai_res = ai_res
                        ai_extracted_info = ai_res
                        if not hint_isbn and (ai_res.get('isbn13') or ai_res.get('isbn10')):
                            hint_isbn = str(ai_res.get('isbn13') or ai_res.get('isbn10') or '').strip()
                        if not hint_title and ai_res.get('title'):
                            hint_title = str(ai_res.get('title') or '').strip()
                        if not hint_authors and ai_res.get('authors'):
                            hint_authors = ', '.join(str(a).strip() for a in ai_res['authors'] if str(a).strip())
                except Exception:
                    pass

        # If AI Vision identified a Magazine / Periodical from the page image, move to /toMags immediately
        if ai_extracted_info:
            ai_mag_pub = detect_magazine_publication(
                title=hint_title or (item.title if item else ''),
                authors=hint_authors or (item.authors if item else ''),
                isbn=hint_isbn or (item.isbn if item else '') or '',
                file_path=file_loc.file_path or '',
                publisher=str(ai_extracted_info.get('publisher') or (item.publisher if item else '') or ''),
            )
            if ai_mag_pub and _get_tomags_dir():
                mv_res = move_file_location_to_tomags(file_loc)
                if mv_res.get('ok'):
                    mv_res['title'] = f"[Moved to toMags] {hint_title or (item.title if item else phys_path.name)}"
                    mv_res['authors'] = ai_mag_pub
                    return mv_res

        reader, embedded_meta, target_meta, isbn, match_result, err = processor.prepare_metadata(
            phys_path,
            precomputed_ai_result=precomputed_ai_res,
        )
        if (hint_isbn or hint_title) and reader:
            from epubmeta.models import BookMetadata
            from epubmeta.matcher import calculate_match_confidence
            clean_h_isbn = re.sub(r'[^0-9Xx]', '', str(hint_isbn or '')).upper()
            clean_h_title = _clean_single_line(hint_title)
            clean_h_auth = _clean_single_line(hint_authors)
            if clean_h_auth.lower() in ('unknown', 'unknown author', '..', '.'):
                clean_h_auth = ''

            ai_meta = None
            if ai_extracted_info and clean_h_title:
                ai_meta = BookMetadata(
                    title=clean_h_title,
                    authors=[a.strip() for a in clean_h_auth.split(',') if a.strip()] if clean_h_auth else [],
                    publisher=str(ai_extracted_info.get('publisher') or '').strip() or None,
                    published_date=str(ai_extracted_info.get('published_year') or '').strip() or None,
                    description=str(ai_extracted_info.get('description') or '').strip() or None,
                    subjects=[str(s).strip() for s in (ai_extracted_info.get('subjects') or []) if str(s).strip()],
                    isbn13=clean_h_isbn if len(clean_h_isbn) == 13 else None,
                    isbn10=clean_h_isbn if len(clean_h_isbn) == 10 else None,
                    source='gemini-ai-vision',
                    ai_used=True,
                )

            hint_meta = None
            if len(clean_h_isbn) in (10, 13):
                try:
                    hint_meta = aggregator.fetch_by_isbn(clean_h_isbn)
                except Exception:
                    hint_meta = None
            if clean_h_title and (
                hint_meta is None
                or not hint_meta.description
                or not hint_meta.publisher
                or not hint_meta.cover_url
            ):
                try:
                    q_meta = aggregator.fetch_by_query(title=clean_h_title, author=clean_h_auth or None)
                    if q_meta:
                        if ai_meta:
                            # Only accept web catalog query result if it genuinely matches the AI Vision extraction
                            q_conf = calculate_match_confidence(
                                candidate=q_meta,
                                filename_info={},
                                existing_meta=ai_meta,
                                min_confidence=min_conf,
                            ).score
                            if q_conf >= 0.65:
                                hint_meta = hint_meta.merge_with(q_meta) if hint_meta else q_meta
                        else:
                            hint_meta = hint_meta.merge_with(q_meta) if hint_meta else q_meta
                except Exception:
                    pass

            if ai_meta:
                if hint_meta:
                    hint_meta = ai_meta.merge_with(hint_meta, combine_lists=True)
                else:
                    hint_meta = ai_meta
                if target_meta:
                    t_conf = calculate_match_confidence(
                        candidate=target_meta,
                        filename_info={},
                        existing_meta=ai_meta,
                        min_confidence=min_conf,
                    ).score
                    if t_conf >= 0.65:
                        target_meta = hint_meta.merge_with(target_meta, combine_lists=True)
                    else:
                        target_meta = hint_meta
                else:
                    target_meta = hint_meta
            elif hint_meta and (hint_meta.title or hint_meta.authors):
                if len(clean_h_isbn) == 13 and not hint_meta.isbn13:
                    hint_meta.isbn13 = clean_h_isbn
                if target_meta:
                    t_conf = calculate_match_confidence(
                        candidate=target_meta,
                        filename_info={},
                        existing_meta=hint_meta,
                        min_confidence=min_conf,
                    ).score
                    if t_conf >= 0.65:
                        target_meta = hint_meta.merge_with(target_meta)
                    else:
                        target_meta = hint_meta
                else:
                    target_meta = hint_meta

            if target_meta and (target_meta.title or target_meta.authors):
                if len(clean_h_isbn) in (10, 13):
                    isbn = clean_h_isbn
                elif getattr(target_meta, 'isbn13', None) or getattr(target_meta, 'isbn10', None):
                    isbn = target_meta.isbn13 or target_meta.isbn10
                ref_meta = BookMetadata(
                    title=clean_h_title or (embedded_meta.title if embedded_meta else item.title),
                    authors=[a.strip() for a in clean_h_auth.split(',') if a.strip()] if clean_h_auth else (embedded_meta.authors if embedded_meta else []),
                    isbn13=clean_h_isbn if len(clean_h_isbn) == 13 else None,
                    isbn10=clean_h_isbn if len(clean_h_isbn) == 10 else None,
                )
                match_result = calculate_match_confidence(
                    candidate=target_meta,
                    filename_info={},
                    existing_meta=ref_meta,
                    min_confidence=min_conf,
                )
                err = None
        if gb_provider is not None and (getattr(gb_provider, 'quota_exhausted', False) or getattr(gb_provider, '_disabled', False)):
            _GOOGLE_BOOKS_COOLDOWN_UNTIL = time.monotonic() + 900.0
    except Exception as prep_exc:
        err_msg = f'Error preparing metadata for {phys_path.name}: {prep_exc}'
        _record_in_memory_diag(file_loc, phys_path, 'FAILED', err_msg)
        return {
            'ok': False,
            'status': 'FAILED',
            'already_enriched': False,
            'message': err_msg,
            'item_id': str(item.id),
            'file_id': str(file_loc.id),
            'file_path': str(phys_path.resolve()),
            'file_name': phys_path.name,
            'orig_title': item.title or '',
            'orig_authors': item.authors or '',
            'orig_publisher': item.publisher or '',
            'orig_pub_date': str(item.pub_date) if item.pub_date else '',
            'orig_isbn': item.isbn or '',
            'orig_description': _strip_html(item.description or ''),
            'orig_cover': item.cover_thumbnail or item.cover_image_url or '',
        }

    orig_cover_thumb = item.cover_thumbnail if (item.cover_thumbnail and str(item.cover_thumbnail).startswith('data:image')) else ''
    if not orig_cover_thumb:
        try:
            from epubmeta.external_dbs import generate_cover_thumbnail
            emb_cov_bytes = reader.extract_cover_bytes() if reader else None
            if emb_cov_bytes and len(emb_cov_bytes) >= 1000:
                orig_cover_thumb = generate_cover_thumbnail(emb_cov_bytes) or ''
        except Exception:
            pass
    orig_cover_for_payload = orig_cover_thumb or item.cover_image_url or ''

    orig_meta_payload = {
        'item_id': str(item.id),
        'file_id': str(file_loc.id),
        'file_path': str(phys_path.resolve()),
        'file_name': phys_path.name,
        'orig_title': (embedded_meta.title if embedded_meta and embedded_meta.title else item.title) or '',
        'orig_authors': (', '.join(embedded_meta.authors) if embedded_meta and embedded_meta.authors else item.authors) or '',
        'orig_publisher': (embedded_meta.publisher if embedded_meta and embedded_meta.publisher else item.publisher) or '',
        'orig_pub_date': (embedded_meta.published_date if embedded_meta and embedded_meta.published_date else (str(item.pub_date) if item.pub_date else '')) or '',
        'orig_isbn': ((embedded_meta.isbn13 or embedded_meta.isbn10) if embedded_meta else item.isbn) or '',
        'orig_description': _strip_html((embedded_meta.description if embedded_meta and embedded_meta.description else item.description) or ''),
        'orig_cover': orig_cover_for_payload,
    }

    if not target_meta or not reader:
        msg = err or f'No matching catalog metadata found for {phys_path.name}'
        if err and 'quarantine' in err.lower():
            fail_status = 'QUARANTINED'
        elif err and 'drm-protected' in err.lower():
            fail_status = 'HAS_DRM'
        else:
            fail_status = 'NOT_FOUND'
        if fail_status not in ('QUARANTINED', 'HAS_DRM'):
            try:
                tracker.record_failure(phys_path, msg, status=fail_status, original_metadata=embedded_meta)
            except Exception:
                pass
        _record_in_memory_diag(file_loc, phys_path, fail_status, msg)
        return {
            'ok': False,
            'status': fail_status,
            'already_enriched': False,
            'message': msg,
            **orig_meta_payload,
        }

    orig_title_has_alnum = bool(re.search(r'[A-Za-z0-9]{3,}', (item.title or '')))
    if (review_only and force_ai_vision) or (match_result and not match_result.is_confident and not force and orig_title_has_alnum):
        if review_only and force_ai_vision:
            msg = (
                f"✨ Gemini AI Vision identified '{target_meta.title}' by "
                f"{', '.join(target_meta.authors or []) or 'Unknown Author'} from the previewed image. "
                f"Review the fields below and click Approve Selected Fields."
            )
            conf_pct = max(match_result.percentage if match_result else 90, 90)
            conf_score = max(match_result.score if match_result else 0.90, 0.90)
        else:
            msg = (
                f"Low match confidence ({match_result.percentage}% < {int(min_conf * 100)}%) for "
                f"'{target_meta.title}' by {', '.join(target_meta.authors or [])}. "
                f"Added to Needs Review queue — choose an option below."
            )
            conf_pct = match_result.percentage
            conf_score = match_result.score
        try:
            tracker.record_failure(
                phys_path,
                msg,
                status='NEEDS_REVIEW',
                original_metadata=embedded_meta,
                candidate_metadata=target_meta,
                match_confidence=conf_score,
            )
        except Exception:
            pass
        _record_in_memory_diag(file_loc, phys_path, 'NEEDS_REVIEW', msg)
        return {
            'ok': False,
            'status': 'NEEDS_REVIEW',
            'already_enriched': False,
            'confidence': conf_pct,
            'candidate_title': target_meta.title or '',
            'candidate_authors': ', '.join(target_meta.authors or []),
            'candidate_publisher': target_meta.publisher or '',
            'candidate_pub_date': target_meta.published_date or '',
            'candidate_isbn': target_meta.isbn13 or target_meta.isbn10 or isbn or '',
            'candidate_description': _strip_html(target_meta.description or ''),
            'candidate_cover': target_meta.cover_url or '',
            'message': msg,
            **orig_meta_payload,
        }

    # Clean scene tags like "(retail) (epub) (mobi)" from target_meta.title and supplement missing synopsis/tags/cover from Apple Books
    if target_meta.title:
        cleaned_t = re.sub(
            r'\s*\((?:retail|epub|mobi|azw3?|pdf|true epub|v\d+(?:\.\d+)?|nodrm|ocr|bnw ocr)\)\s*',
            ' ',
            target_meta.title,
            flags=re.IGNORECASE,
        ).strip()
        if cleaned_t:
            target_meta.title = cleaned_t

    is_placeholder_title = bool(
        not target_meta.title
        or re.match(r'^untitled\b', target_meta.title.strip(), re.IGNORECASE)
    )
    is_placeholder_date = bool(
        not getattr(target_meta, 'published_date', None)
        or re.match(r'^0[01]\d\d\b', str(getattr(target_meta, 'published_date', '')).strip())
    )
    if is_placeholder_date:
        target_meta.published_date = None

    if (
        is_placeholder_title
        or is_placeholder_date
        or not target_meta.description
        or not getattr(target_meta, 'subjects', None)
        or not getattr(target_meta, 'cover_url', None)
    ):
        try:
            import urllib.parse as _up
            import requests as _req
            ab_candidates = []
            lookup_isbn = target_meta.isbn13 or target_meta.isbn10 or isbn
            if lookup_isbn and len(re.sub(r'[^0-9Xx]', '', str(lookup_isbn))) in (10, 13):
                r_isbn_ab = _req.get(
                    f'https://itunes.apple.com/lookup?isbn={_up.quote(str(lookup_isbn))}',
                    timeout=4.5,
                    headers={'User-Agent': 'Mozilla/5.0'},
                )
                if r_isbn_ab.status_code == 200:
                    ab_candidates.extend(r_isbn_ab.json().get('results') or [])

            if not ab_candidates:
                q_parts = [ '' if is_placeholder_title else (target_meta.title or item.title or '') ]
                if target_meta.authors:
                    q_parts.append(target_meta.authors[0])
                elif item.authors and item.authors.lower() != 'unknown author':
                    q_parts.append(item.authors)
                q_term = ' '.join(p for p in q_parts if p).strip()
                if q_term:
                    r_ab = _req.get(
                        f'https://itunes.apple.com/search?media=ebook&entity=ebook&limit=5&term={_up.quote(q_term)}',
                        timeout=4.5,
                        headers={'User-Agent': 'Mozilla/5.0'},
                    )
                    if r_ab.status_code == 200:
                        want_t = _norm_lookup_str('' if is_placeholder_title else (target_meta.title or item.title))
                        want_a = _norm_lookup_str(', '.join(target_meta.authors or []) or item.authors)
                        for ab_it in (r_ab.json().get('results') or []):
                            ab_t = _norm_lookup_str(ab_it.get('trackName') or '')
                            ab_a = _norm_lookup_str(ab_it.get('artistName') or '')
                            if (not want_t or (ab_t and (want_t in ab_t or ab_t in want_t))) and (
                                not want_a or not ab_a or (set(want_a.split()) & set(ab_a.split()))
                            ):
                                ab_candidates.append(ab_it)
                                break

            if ab_candidates:
                ab_it = ab_candidates[0]
                if is_placeholder_title and ab_it.get('trackName') and not re.match(r'^untitled\b', str(ab_it['trackName']).strip(), re.IGNORECASE):
                    target_meta.title = str(ab_it['trackName']).strip()
                    is_placeholder_title = False
                if not target_meta.description and ab_it.get('description'):
                    target_meta.description = _strip_html(ab_it['description'])
                if not getattr(target_meta, 'subjects', None) and ab_it.get('genres'):
                    target_meta.subjects = [
                        g for g in ab_it['genres']
                        if isinstance(g, str) and g.strip() and g.strip().lower() != 'books'
                    ]
                if not getattr(target_meta, 'published_date', None) and ab_it.get('releaseDate'):
                    target_meta.published_date = str(ab_it['releaseDate'])[:10]
                if not getattr(target_meta, 'cover_url', None) and ab_it.get('artworkUrl100'):
                    target_meta.cover_url = re.sub(
                        r'/\d+x\d+bb\.',
                        '/600x600bb.',
                        str(ab_it['artworkUrl100']),
                    )
        except Exception:
            pass

    if is_placeholder_title:
        for fallback_cand in (
            getattr(embedded_meta, 'title', None) if embedded_meta else None,
            item.title,
        ):
            if fallback_cand and not re.match(r'^untitled\b', str(fallback_cand).strip(), re.IGNORECASE):
                target_meta.title = str(fallback_cand).strip()
                break

    if not getattr(target_meta, 'cover_bytes', None) and getattr(target_meta, 'cover_url', None):
        try:
            import requests as _req
            r_cov = _req.get(target_meta.cover_url, timeout=4.5, headers={'User-Agent': 'Mozilla/5.0'})
            if r_cov.status_code == 200 and len(r_cov.content) >= 1500:
                target_meta.cover_bytes = r_cov.content
        except Exception:
            pass

    res = processor.apply_metadata(phys_path, reader, target_meta, isbn)
    status = res.get('status', 'FAILED')
    if status == 'SUCCESS':
        get_enriched_paths_set()
        s_map = _ENRICHED_PATHS_CACHE.get('status_by_path')
        if isinstance(s_map, dict):
            s_map.pop(str(file_loc.file_path or ''), None)
            s_map.pop(str(phys_path), None)
        if _ENRICHED_PATHS_CACHE.get('paths_set') is not None:
            if file_loc.file_path:
                _ENRICHED_PATHS_CACHE['paths_set'].add(str(file_loc.file_path))
            if phys_path:
                _ENRICHED_PATHS_CACHE['paths_set'].add(str(phys_path))
        if isinstance(_ENRICHED_PATHS_CACHE.get('enriched_item_ids_set'), set):
            if item.id not in _ENRICHED_PATHS_CACHE['enriched_item_ids_set']:
                _ENRICHED_PATHS_CACHE['enriched_item_ids_set'].add(item.id)
                if isinstance(_ENRICHED_PATHS_CACHE.get('enriched_item_ids'), list):
                    _ENRICHED_PATHS_CACHE['enriched_item_ids'].append(item.id)
        elif isinstance(_ENRICHED_PATHS_CACHE.get('enriched_item_ids'), list):
            if item.id not in _ENRICHED_PATHS_CACHE['enriched_item_ids']:
                _ENRICHED_PATHS_CACHE['enriched_item_ids'].append(item.id)
        if isinstance(_ENRICHED_PATHS_CACHE.get('needs_review_item_ids_set'), set):
            if item.id in _ENRICHED_PATHS_CACHE['needs_review_item_ids_set']:
                _ENRICHED_PATHS_CACHE['needs_review_item_ids_set'].discard(item.id)
                if isinstance(_ENRICHED_PATHS_CACHE.get('needs_review_item_ids'), list):
                    try:
                        _ENRICHED_PATHS_CACHE['needs_review_item_ids'].remove(item.id)
                    except ValueError:
                        pass
        if isinstance(_ENRICHED_PATHS_CACHE.get('strict_in_review_ids_set'), set):
            if item.id in _ENRICHED_PATHS_CACHE['strict_in_review_ids_set']:
                _ENRICHED_PATHS_CACHE['strict_in_review_ids_set'].discard(item.id)
                if isinstance(_ENRICHED_PATHS_CACHE.get('strict_in_review_ids'), list):
                    try:
                        _ENRICHED_PATHS_CACHE['strict_in_review_ids'].remove(item.id)
                    except ValueError:
                        pass
        if isinstance(_ENRICHED_PATHS_CACHE.get('excluded_from_needs_enriching_ids_set'), set):
            if item.id not in _ENRICHED_PATHS_CACHE['excluded_from_needs_enriching_ids_set']:
                _ENRICHED_PATHS_CACHE['excluded_from_needs_enriching_ids_set'].add(item.id)
                if isinstance(_ENRICHED_PATHS_CACHE.get('excluded_from_needs_enriching_ids'), list):
                    _ENRICHED_PATHS_CACHE['excluded_from_needs_enriching_ids'].append(item.id)
        merged_ids = _apply_enriched_metadata_to_catalog_item(
            item=item,
            file_loc=file_loc,
            target_meta=target_meta,
            isbn_fallback=isbn,
            file_sha256=res.get('file_sha256'),
            cover_thumbnail=res.get('cover_thumbnail'),
            phys_path=phys_path,
        ) or []
        # If after enrichment this file is identified as a Magazine / Periodical, move it to /toMags automatically!
        mag_pub = detect_magazine_publication(
            title=item.title,
            authors=item.authors,
            isbn=item.isbn or '',
            file_path=file_loc.file_path or '',
            publisher=item.publisher or '',
        )
        if mag_pub and _get_tomags_dir():
            mv_res = move_file_location_to_tomags(file_loc)
            if mv_res.get('ok'):
                mv_res['title'] = f"[Moved to toMags] {item.title}"
                mv_res['authors'] = mag_pub
                return mv_res
        return {
            'ok': True,
            'status': 'SUCCESS',
            'already_enriched': False,
            'unchanged': bool(res.get('unchanged')),
            'source': getattr(target_meta, 'source', '') or 'epubmeta',
            'confidence': match_result.percentage if match_result else 100,
            'message': res.get('message') or f'Successfully enriched {item.title} by {item.authors}',
            'item_id': str(item.id),
            'file_id': str(file_loc.id),
            'title': item.title,
            'authors': item.authors,
            'isbn': item.isbn or '',
            'asin': item.asin or '',
            'publisher': item.publisher or '',
            'pub_date': str(item.pub_date) if item.pub_date else '',
            'description': _strip_html(item.description or ''),
            'cover_url': item.cover_thumbnail or item.cover_image_url or f'/mylibrary/opds/cover/{item.id}.jpg',
            'tags': [str(s).strip() for s in (getattr(target_meta, 'subjects', None) or []) if str(s).strip()],
            'merged_item_ids': merged_ids,
        }

    fail_msg = res.get('message') or f'Failed to enrich {phys_path.name}'
    _record_in_memory_diag(file_loc, phys_path, status, fail_msg)
    return {
        'ok': False,
        'status': status,
        'already_enriched': False,
        'confidence': match_result.percentage if match_result else None,
        'candidate_title': getattr(target_meta, 'title', '') or '',
        'candidate_authors': ', '.join(getattr(target_meta, 'authors', None) or []),
        'candidate_publisher': getattr(target_meta, 'publisher', '') or '',
        'candidate_pub_date': getattr(target_meta, 'published_date', '') or '',
        'candidate_isbn': getattr(target_meta, 'isbn13', '') or getattr(target_meta, 'isbn10', '') or isbn or '',
        'candidate_description': _strip_html(getattr(target_meta, 'description', '') or ''),
        'candidate_cover': getattr(target_meta, 'cover_url', '') or '',
        'message': fail_msg,
        **orig_meta_payload,
    }


def _is_item_metadata_complete(item: CatalogItem) -> bool:
    """
    Return True if `item` has complete enriched metadata (valid title, known author,
    ISBN or ASIN identifier, synopsis, publisher, pub_date, and cover image).
    """
    if not item:
        return False
    title_ok = bool(item.title and not re.match(r'^untitled\b', (item.title or '').strip(), re.IGNORECASE))
    author_ok = bool(
        item.authors
        and item.authors.strip().lower() not in ('', 'unknown', 'unknown author', '..', '.')
    )
    ident_ok = bool((item.isbn or '').strip() or (item.asin or '').strip())
    desc_ok = bool((item.description or '').strip())
    pub_ok = bool((item.publisher or '').strip())
    date_ok = bool(item.pub_date)
    cover_ok = bool(
        (item.cover_thumbnail and str(item.cover_thumbnail).startswith('data:image'))
        or (item.cover_image_url and str(item.cover_image_url).strip())
    )
    return bool(title_ok and author_ok and ident_ok and desc_ok and pub_ok and date_ok and cover_ok)


def enrich_catalog_item_on_demand(
    item: CatalogItem,
    force: bool = False,
    re_enrich: bool = False,
    hint_title: str = '',
    hint_authors: str = '',
    hint_isbn: str = '',
    ai_image_data_uris: Optional[List[str]] = None,
    force_ai_vision: bool = False,
    review_only: bool = False,
) -> Dict[str, Any]:
    """
    Perform on-demand `epubmeta` enrichment for a `CatalogItem` by enriching its
    ebook/document file locations (`.epub`, `.pdf`, `.mobi`, `.azw3`, etc.).
    When a catalog item has multiple book files, automatically tries the next book file
    if a file fails or yields incomplete metadata, merging newly discovered fields.
    """
    def _fmt_prio(fmt_str: str) -> int:
        u = (fmt_str or '').upper().lstrip('.')
        if u == 'EPUB':
            return 0
        if u == 'PDF':
            return 1
        if u in ('MOBI', 'AZW3', 'AZW'):
            return 2
        return 3

    files_list = sorted(
        list(item.files.all()),
        key=lambda f: (
            _fmt_prio(f.file_format),
            bool(f.has_drm),
            bool(f.is_invalid),
            -(f.file_size or 0),
        ),
    )
    enrichable = [
        f for f in files_list
        if (f.file_format or '').upper().lstrip('.') in ENRICHABLE_FORMATS
        or str(f.file_path or '').lower().endswith(ENRICHABLE_EXTENSIONS)
    ]
    if not enrichable:
        return {
            'ok': False,
            'status': 'NO_ENRICHABLE_FILE',
            'already_enriched': False,
            'message': 'This catalog item does not have a supported local book file to enrich.',
        }

    best_success_res: Optional[Dict[str, Any]] = None
    best_review_res: Optional[Dict[str, Any]] = None
    last_res: Optional[Dict[str, Any]] = None
    merged_tags: List[str] = []
    merged_item_ids: List[str] = []

    for idx, f_loc in enumerate(enrichable):
        if not CatalogItem.objects.filter(id=item.id).exists():
            break
        item.refresh_from_db()
        f_loc.catalog_item = item

        # If we already achieved complete metadata from an earlier file in this pass,
        # only enrich remaining unenriched companion files (up to 4 total) from the warm cache.
        if best_success_res is not None and _is_item_metadata_complete(item):
            if idx < 4 and not is_file_enriched(f_loc.file_path):
                try:
                    enrich_file_location_on_demand(f_loc, force=force)
                except Exception:
                    pass
            continue

        res = enrich_file_location_on_demand(
            f_loc,
            force=force,
            re_enrich=re_enrich,
            hint_title=hint_title,
            hint_authors=hint_authors,
            hint_isbn=hint_isbn,
            ai_image_data_uris=ai_image_data_uris if idx == 0 else None,
            force_ai_vision=force_ai_vision,
            review_only=review_only,
        )
        last_res = res

        if res.get('moved_to_tomags'):
            if CatalogItem.objects.filter(id=item.id).exists():
                item.refresh_from_db()
                mag_files_res = move_catalog_item_to_tomags(item, only_detected_mags=True)
                if mag_files_res.get('ok'):
                    all_moved = list(dict.fromkeys((res.get('moved_files') or []) + (mag_files_res.get('moved_files') or [])))
                    mag_files_res['moved_files'] = all_moved
                    mag_files_res['message'] = f"Identified as magazine and moved {len(all_moved)} file(s) to toMags: {', '.join(all_moved[:5])}"
                    return mag_files_res
            return res

        if CatalogItem.objects.filter(id=item.id).exists():
            item.refresh_from_db()
            mag_files_res = move_catalog_item_to_tomags(item, only_detected_mags=True)
            if mag_files_res.get('ok'):
                return mag_files_res

        if res.get('ok'):
            for mid in (res.get('merged_item_ids') or []):
                if mid and mid not in merged_item_ids:
                    merged_item_ids.append(mid)
            for t in (res.get('tags') or []):
                if t and t not in merged_tags:
                    merged_tags.append(t)
            if best_success_res is None:
                best_success_res = dict(res)
            else:
                # Merge any newly filled fields from this companion book file into best_success_res
                for k in ('title', 'authors', 'isbn', 'asin', 'publisher', 'pub_date', 'description', 'cover_url'):
                    if res.get(k) and (
                        not best_success_res.get(k)
                        or (k == 'authors' and str(best_success_res.get(k)).strip().lower() in ('unknown', 'unknown author'))
                    ):
                        best_success_res[k] = res[k]
                if not res.get('already_enriched'):
                    best_success_res['already_enriched'] = False
                    best_success_res['unchanged'] = bool(res.get('unchanged'))
                    best_success_res['message'] = res.get('message') or best_success_res.get('message')
            best_success_res['tags'] = list(merged_tags)
            best_success_res['merged_item_ids'] = list(merged_item_ids)
        else:
            if res.get('status') == 'NEEDS_REVIEW':
                if best_review_res is None or (res.get('confidence') or 0) > (best_review_res.get('confidence') or 0):
                    best_review_res = res

    if best_success_res is not None:
        if CatalogItem.objects.filter(id=item.id).exists():
            item.refresh_from_db()
            for mid in merge_duplicate_catalog_items_for_item(item):
                if mid and mid not in merged_item_ids:
                    merged_item_ids.append(mid)
            best_success_res.update({
                'item_id': str(item.id),
                'title': item.title,
                'authors': item.authors,
                'isbn': item.isbn or best_success_res.get('isbn') or '',
                'asin': item.asin or best_success_res.get('asin') or '',
                'publisher': item.publisher or best_success_res.get('publisher') or '',
                'pub_date': str(item.pub_date) if item.pub_date else (best_success_res.get('pub_date') or ''),
                'description': _strip_html(item.description or '') or best_success_res.get('description') or '',
                'cover_url': item.cover_thumbnail or item.cover_image_url or f'/mylibrary/opds/cover/{item.id}.jpg',
                'tags': list(merged_tags),
                'merged_item_ids': list(merged_item_ids),
            })
        return best_success_res

    return best_review_res or last_res or {
        'ok': False,
        'status': 'FAILED',
        'already_enriched': False,
        'message': 'All candidate book files failed enrichment.',
    }


def revert_catalog_item_enrichment(
    item: Optional[CatalogItem] = None,
    file_loc: Optional[FileLocation] = None,
    file_path: str = '',
) -> Dict[str, Any]:
    """
    Revert an enriched (or mis-enriched / in-review) `CatalogItem` and its `FileLocation`(s)
    back to their original pre-enrichment metadata (`orig_title`, `orig_author`, `orig_isbn`,
    `original_metadata_json`, and original embedded cover), restore any `.bak` backup file if
    present, and remove the enrichment record from `processed_files` and in-memory caches.
    """
    import datetime as _dt
    import shutil as _shutil

    if file_loc and not item:
        item = file_loc.catalog_item

    if not item and file_path:
        path_cands = [file_path]
        if file_path.startswith('/books/'):
            path_cands.append('/volume1/common/Media/Books/' + file_path[len('/books/'):])
        elif file_path.startswith('/volume1/common/Media/Books/'):
            path_cands.append('/books/' + file_path[len('/volume1/common/Media/Books/'):])
        loc = FileLocation.objects.select_related('catalog_item').filter(file_path__in=path_cands).first()
        if loc:
            file_loc = loc
            item = loc.catalog_item

    if not item and not file_path:
        return {'ok': False, 'error': 'CatalogItem or file_path not found to revert.'}

    files_list = list(item.files.all()) if item else ([file_loc] if file_loc else [])
    all_paths: List[str] = []
    all_shas: List[str] = []

    def _add_path_variants(p_str: str) -> None:
        if not p_str:
            return
        for cand in (
            p_str,
            '/volume1/common/Media/Books/' + p_str[len('/books/'):] if p_str.startswith('/books/') else '',
            '/books/' + p_str[len('/volume1/common/Media/Books/'):] if p_str.startswith('/volume1/common/Media/Books/') else '',
        ):
            if cand and cand not in all_paths:
                all_paths.append(cand)
        phys = _resolve_physical_file_path(p_str)
        if phys:
            rp = str(phys.resolve())
            if rp not in all_paths:
                all_paths.append(rp)

    if file_path:
        _add_path_variants(file_path)
    for f in files_list:
        _add_path_variants(f.file_path or '')
        if f.file_hash and f.file_hash not in all_shas:
            all_shas.append(f.file_hash)

    orig_title = ''
    orig_author = ''
    orig_isbn = ''
    orig_pub = ''
    orig_pub_date = ''
    orig_desc = ''
    orig_cover = ''
    orig_sha = ''

    base_dir = Path(getattr(settings, 'BASE_DIR', '.'))
    db_path = base_dir / 'nas_scanner' / 'scanner_state.db'
    if not db_path.exists() and (base_dir / 'scanner' / 'scanner_state.db').exists():
        db_path = base_dir / 'scanner' / 'scanner_state.db'

    conn = None
    try:
        if is_postgres_scanner_enabled() or db_path.exists():
            conn = connect_scanner_db(db_path, timeout=10.0)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            matched_rows = []
            if all_paths:
                ph = ','.join(['?'] * len(all_paths))
                matched_rows.extend(
                    cur.execute(
                        f"""
                        SELECT file_path, file_name, status, title, author, isbn,
                               orig_title, orig_author, orig_isbn, orig_sha256, file_sha256,
                               original_metadata_json, updated_metadata_json
                        FROM processed_files
                        WHERE file_path IN ({ph})
                        """,
                        all_paths,
                    ).fetchall()
                )
            if not matched_rows and all_shas:
                ph_s = ','.join(['?'] * len(all_shas))
                matched_rows.extend(
                    cur.execute(
                        f"""
                        SELECT file_path, file_name, status, title, author, isbn,
                               orig_title, orig_author, orig_isbn, orig_sha256, file_sha256,
                               original_metadata_json, updated_metadata_json
                        FROM processed_files
                        WHERE file_sha256 IN ({ph_s}) OR orig_sha256 IN ({ph_s})
                        """,
                        all_shas + all_shas,
                    ).fetchall()
                )

            for r in matched_rows:
                rk = r.keys()
                rp = r['file_path'] or ''
                if rp:
                    _add_path_variants(rp)
                o_meta = {}
                if 'original_metadata_json' in rk and r['original_metadata_json']:
                    try:
                        o_meta = json.loads(r['original_metadata_json']) or {}
                    except Exception:
                        o_meta = {}
                if not orig_title and (r['orig_title'] or o_meta.get('title')):
                    orig_title = str(r['orig_title'] or o_meta.get('title') or '').strip()
                if not orig_author:
                    oa = r['orig_author'] or ''
                    if not oa and o_meta.get('authors'):
                        oa_val = o_meta.get('authors')
                        oa = ', '.join(str(x) for x in oa_val if x) if isinstance(oa_val, list) else str(oa_val or '')
                    orig_author = str(oa).strip()
                if not orig_isbn:
                    orig_isbn = str(r['orig_isbn'] or o_meta.get('isbn13') or o_meta.get('isbn10') or '').strip()
                if not orig_pub and o_meta.get('publisher'):
                    orig_pub = str(o_meta.get('publisher') or '').strip()
                if not orig_pub_date and o_meta.get('published_date'):
                    orig_pub_date = str(o_meta.get('published_date') or '').strip()
                if not orig_desc and o_meta.get('description'):
                    orig_desc = _strip_html(str(o_meta.get('description') or ''))
                if not orig_cover and o_meta.get('cover_url'):
                    orig_cover = str(o_meta.get('cover_url') or '').strip()
                if not orig_sha and 'orig_sha256' in rk and r['orig_sha256']:
                    orig_sha = str(r['orig_sha256']).strip()

            # Delete the processed_files row(s) so the item is no longer marked Enriched or Carrying Enriched Tags
            if all_paths:
                ph_del = ','.join(['?'] * len(all_paths))
                cur.execute(f"DELETE FROM processed_files WHERE file_path IN ({ph_del})", all_paths)
            if all_shas:
                ph_sdel = ','.join(['?'] * len(all_shas))
                cur.execute(
                    f"DELETE FROM processed_files WHERE file_sha256 IN ({ph_sdel}) OR orig_sha256 IN ({ph_sdel})",
                    all_shas + all_shas,
                )
            # Also purge any poisoned query cache rows matching the item's old or current title
            bad_titles = [t for t in {orig_title, item.title if item else ''} if t]
            for bt in bad_titles:
                try:
                    cur.execute("DELETE FROM metadata_cache WHERE cache_key LIKE ?", (f"%{bt[:32]}%",))
                except Exception:
                    pass
            conn.commit()
    except Exception:
        pass
    finally:
        if conn is not None:
            conn.close()

    # Check if a .bak backup file exists in /books_bad/bak and restore it if present
    bak_dirs = [Path('/books_bad/bak'), Path('/volume1/common/Media/books_bad/bak'), Path(r'U:\Media\books_bad\bak')]
    for f in files_list:
        phys = _resolve_physical_file_path(f.file_path)
        if not phys or not phys.exists():
            continue
        for bdir in bak_dirs:
            try:
                if not bdir.exists():
                    continue
                cand_bak = bdir / f"{phys.name}.bak"
                if cand_bak.exists() and cand_bak.stat().st_size > 0:
                    _shutil.copy2(str(cand_bak), str(phys))
                    try:
                        cand_bak.unlink()
                    except Exception:
                        pass
                    if orig_sha:
                        f.file_hash = orig_sha
                        f.save(update_fields=['file_hash'])
                    break
            except Exception:
                continue

    # Determine clean fallback filename stem in case orig_title is empty or an internal PDF code (like 000_ITWK_...)
    primary_fname = ''
    if files_list:
        primary_fname = files_list[0].file_name or os.path.basename(files_list[0].file_path or '')
    elif file_path:
        primary_fname = os.path.basename(file_path)
    clean_stem = os.path.splitext(primary_fname)[0] if primary_fname else ''
    if clean_stem and '.' in clean_stem and ' ' not in clean_stem:
        clean_stem = re.sub(r'\.(?:PDF|EPUB|MOBI|AZW3|eBook)(?:-[A-Za-z0-9]+)?$', '', clean_stem, flags=re.I)
        clean_stem = clean_stem.replace('.', ' ').strip()

    # If orig_title is missing, untitled, or an internal 3-digit PDF production slug (like 000_ITWK_..., 001_ITWK_...) when clean_stem has real words, prefer clean_stem
    if not orig_title or re.match(r'^(?:untitled\b|\d{3}_[A-Za-z0-9_]+$|0{2,}\d*[_-])', orig_title.strip(), re.I):
        if clean_stem:
            orig_title = clean_stem
    if orig_author and orig_author.strip().lower() in ('asura01', 'yyepg', 'admin', 'administrator'):
        orig_author = ''

    if item:
        if orig_title:
            item.title = orig_title[:500]
        item.authors = (orig_author or 'Unknown Author')[:500]
        item.publisher = orig_pub[:255] if orig_pub else ''
        item.isbn = orig_isbn[:32] if orig_isbn else ''
        item.description = orig_desc or ''
        if orig_pub_date:
            m_dt = re.match(r'^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?', orig_pub_date)
            if m_dt:
                try:
                    item.pub_date = _dt.date(
                        int(m_dt.group(1)),
                        min(max(int(m_dt.group(2) or 1), 1), 12),
                        min(max(int(m_dt.group(3) or 1), 1), 28),
                    )
                except Exception:
                    item.pub_date = None
            else:
                item.pub_date = None
        else:
            item.pub_date = None

        item.cover_image_url = ''
        emb_cover = extract_original_cover_for_item(item)
        if emb_cover and emb_cover.startswith('data:image'):
            item.cover_thumbnail = emb_cover
        elif orig_cover and orig_cover.startswith('data:image'):
            item.cover_thumbnail = orig_cover
        item.save()

    # Clear all in-memory enrichment caches for this item and its file paths
    try:
        p_set = _ENRICHED_PATHS_CACHE.get('paths_set')
        if isinstance(p_set, set):
            for p in all_paths:
                p_set.discard(p)
        s_map = _ENRICHED_PATHS_CACHE.get('status_by_path')
        if isinstance(s_map, dict):
            for p in all_paths:
                s_map.pop(p, None)
        if item:
            for set_key, list_key in (
                ('enriched_item_ids_set', 'enriched_item_ids'),
                ('needs_review_item_ids_set', 'needs_review_item_ids'),
                ('strict_in_review_ids_set', 'strict_in_review_ids'),
                ('excluded_from_needs_enriching_ids_set', 'excluded_from_needs_enriching_ids'),
            ):
                id_set = _ENRICHED_PATHS_CACHE.get(set_key)
                if isinstance(id_set, set):
                    id_set.discard(item.id)
                id_list = _ENRICHED_PATHS_CACHE.get(list_key)
                if isinstance(id_list, list):
                    try:
                        id_list.remove(item.id)
                    except ValueError:
                        pass
        _TAGS_FOLDERS_CACHE['expires_at'] = 0.0
    except Exception:
        pass

    return {
        'ok': True,
        'reverted': True,
        'item_id': str(item.id) if item else '',
        'file_id': str(file_loc.id) if file_loc else (str(files_list[0].id) if files_list else ''),
        'title': item.title if item else orig_title,
        'authors': item.authors if item else (orig_author or 'Unknown Author'),
        'publisher': (item.publisher or '') if item else orig_pub,
        'pub_date': (str(item.pub_date) if item.pub_date else '') if item else orig_pub_date,
        'isbn': (item.isbn or '') if item else orig_isbn,
        'description': _strip_html(item.description or '') if item else orig_desc,
        'cover_url': (item.cover_thumbnail or item.cover_image_url or f'/mylibrary/opds/cover/{item.id}.jpg') if item else orig_cover,
        'tags': [],
        'is_enriched': False,
        'message': f"Reverted '{item.title if item else orig_title}' back to original pre-enrichment metadata.",
    }


def extract_original_cover_for_item(item: Optional[CatalogItem]) -> Optional[str]:
    """
    Extract the original embedded book cover (as a `data:image/jpeg;base64,...` URI)
    from `item.files` (or `item.cover_thumbnail` / `item.cover_image_url`), unless missing.
    """
    if not item:
        return None
    try:
        import sys
        base_dir = Path(getattr(settings, 'BASE_DIR', '.'))
        for sub in ('scanner', 'nas_scanner'):
            cand_dir = str((base_dir / sub).resolve())
            if os.path.isdir(cand_dir) and cand_dir not in sys.path:
                sys.path.insert(0, cand_dir)
        from epubmeta.external_dbs import generate_cover_thumbnail
        from epubmeta.readers import get_reader

        def _fmt_prio(fmt_str: str) -> int:
            u = (fmt_str or '').upper().lstrip('.')
            if u == 'EPUB':
                return 0
            if u == 'PDF':
                return 1
            if u in ('MOBI', 'AZW3', 'AZW'):
                return 2
            return 3

        files_list = sorted(
            list(item.files.all()),
            key=lambda f: (
                _fmt_prio(f.file_format),
                bool(f.has_drm),
                bool(f.is_invalid),
                -(f.file_size or 0),
            ),
        )
        for f_loc in files_list:
            phys = _resolve_physical_file_path(f_loc.file_path)
            if not phys or not phys.exists():
                continue
            try:
                rdr = get_reader(phys)
                cov_bytes = rdr.extract_cover_bytes() if rdr else None
                if cov_bytes and len(cov_bytes) >= 1000:
                    thumb = generate_cover_thumbnail(cov_bytes, max_size=(360, 540))
                    if thumb and thumb.startswith('data:image'):
                        return thumb
            except Exception:
                continue
    except Exception:
        pass

    if item.cover_thumbnail and str(item.cover_thumbnail).startswith('data:image'):
        return item.cover_thumbnail
    if item.cover_image_url and str(item.cover_image_url).strip():
        return item.cover_image_url.strip()
    return None


def search_book_covers_web(
    query: str = '',
    title: str = '',
    authors: str = '',
    isbn: str = '',
    item_id: str = '',
) -> List[Dict[str, Any]]:
    """
    Search multiple web book cover sources in parallel (Apple Books, Google Books, Open Library)
    just like Moon+ Reader's "Download cover from internet" feature, placing the book's
    original embedded cover first unless missing.
    Returns a deduplicated list of candidate cover dicts:
      `{"cover_url": str, "title": str, "authors": str, "source": str}`
    """
    import concurrent.futures
    import urllib.parse
    import requests

    clean_isbn = re.sub(r'[^0-9Xx]', '', str(isbn or '')).upper()
    clean_title = (title or '').strip()
    clean_authors = (authors or '').strip()
    if clean_authors.lower() in ('unknown', 'unknown author', '..', '.'):
        clean_authors = ''

    search_q = (query or '').strip()
    if not search_q:
        parts = [p for p in (clean_title, clean_authors) if p]
        search_q = ' '.join(parts) or clean_isbn

    # Shorten overly long subtitle queries so Apple Books / Open Library return fast, accurate matches
    short_title = clean_title.split(':', 1)[0].strip() if ':' in clean_title else clean_title
    api_q = search_q
    if len(api_q) > 70 and ':' in api_q:
        api_q = f"{api_q.split(':', 1)[0].strip()} {clean_authors}".strip()

    results: List[Dict[str, Any]] = []
    seen_urls: set = set()

    def _add(url: str, c_title: str, c_auth: str, source: str, fallback_url: str = ''):
        if not url:
            return
        u = url.strip()
        if u.startswith('http://'):
            u = 'https://' + u[7:]
        norm_u = re.sub(r'&edge=curl', '', u)
        if norm_u in seen_urls:
            return
        seen_urls.add(norm_u)
        results.append({
            'cover_url': norm_u,
            'fallback_url': (fallback_url or '').strip(),
            'title': (c_title or clean_title or 'Book Cover').strip(),
            'authors': (c_auth or clean_authors or '').strip(),
            'source': source,
        })

    # 0. Original Book Cover (First Option unless missing)
    if item_id:
        try:
            ci = CatalogItem.objects.prefetch_related('files').filter(id=item_id).first()
            orig_cov = extract_original_cover_for_item(ci)
            if orig_cov:
                _add(
                    orig_cov,
                    ci.title if (ci and ci.title) else (clean_title or 'Original Cover'),
                    ci.authors if (ci and ci.authors) else clean_authors,
                    'Original Cover',
                )
        except Exception:
            pass

    # 1. Direct ISBN cover from Open Library if ISBN is available
    if len(clean_isbn) in (10, 13):
        _add(
            f'https://covers.openlibrary.org/b/isbn/{clean_isbn}-L.jpg?default=false',
            short_title or clean_title,
            clean_authors,
            'Open Library (ISBN)',
        )

    def _fetch_apple_books() -> List[Dict[str, str]]:
        out = []
        if not api_q:
            return out
        try:
            url = f'https://itunes.apple.com/search?media=ebook&entity=ebook&limit=8&term={urllib.parse.quote(api_q)}'
            r = requests.get(url, timeout=(2.0, 2.8), headers={'User-Agent': 'Mozilla/5.0'})
            if r.status_code == 200:
                for item in (r.json().get('results') or []):
                    art = item.get('artworkUrl100') or item.get('artworkUrl60') or ''
                    if art:
                        hi_res = re.sub(r'\d+x\d+bb\.jpg', '600x600bb.jpg', art)
                        out.append({
                            'url': hi_res,
                            'title': item.get('trackName') or '',
                            'authors': item.get('artistName') or '',
                            'source': 'Apple Books',
                        })
        except Exception:
            pass
        return out

    def _fetch_google_books() -> List[Dict[str, str]]:
        out = []
        if (not api_q and not clean_isbn) or time.monotonic() < _GOOGLE_BOOKS_COOLDOWN_UNTIL:
            return out
        try:
            epubmeta_cfg = _load_scanner_epubmeta_config()
            g_key = epubmeta_cfg.get('google_api_key') or os.environ.get('GOOGLE_BOOKS_API_KEY') or ''
            q_str = f'isbn:{clean_isbn}' if len(clean_isbn) in (10, 13) else api_q
            url = f'https://www.googleapis.com/books/v1/volumes?q={urllib.parse.quote(q_str)}&maxResults=8'
            if g_key:
                url += f'&key={urllib.parse.quote(g_key)}'
            r = requests.get(url, timeout=(2.0, 2.8), headers={'User-Agent': 'Mozilla/5.0'})
            if r.status_code == 200:
                for item in (r.json().get('items') or []):
                    vi = item.get('volumeInfo') or {}
                    links = vi.get('imageLinks') or {}
                    thumb = links.get('extraLarge') or links.get('large') or links.get('medium') or links.get('thumbnail') or links.get('smallThumbnail') or ''
                    if thumb:
                        if thumb.startswith('http://'):
                            thumb = 'https://' + thumb[7:]
                        thumb = re.sub(r'&edge=curl', '', thumb)
                        out.append({
                            'url': thumb,
                            'title': vi.get('title') or '',
                            'authors': ', '.join(vi.get('authors') or []),
                            'source': 'Google Books',
                        })
        except Exception:
            pass
        return out

    def _fetch_open_library() -> List[Dict[str, str]]:
        out = []
        if not api_q:
            return out
        try:
            url = f'https://openlibrary.org/search.json?q={urllib.parse.quote(api_q)}&limit=8'
            r = requests.get(url, timeout=(2.0, 2.8), headers={'User-Agent': 'Mozilla/5.0'})
            if r.status_code == 200:
                for doc in (r.json().get('docs') or []):
                    cov_id = doc.get('cover_i')
                    if cov_id:
                        out.append({
                            'url': f'https://covers.openlibrary.org/b/id/{cov_id}-L.jpg',
                            'title': doc.get('title') or '',
                            'authors': ', '.join((doc.get('author_name') or [])[:2]),
                            'source': 'Open Library',
                        })
        except Exception:
            pass
        return out

    def _fetch_web_image_covers() -> List[Dict[str, str]]:
        out = []
        if not api_q:
            return out
        try:
            import html as _html
            web_q = f'{api_q} book cover'
            url = f'https://www.bing.com/images/search?q={urllib.parse.quote(web_q)}&form=HDRSC2'
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
            }
            r = requests.get(url, timeout=(2.0, 2.8), headers=headers)
            if r.status_code == 200:
                murls = re.findall(r'murl&quot;:&quot;(https?://[^&]+?)&quot;', r.text)
                turls = re.findall(r'turl&quot;:&quot;(https?://[^&]+?)&quot;', r.text)
                titles = re.findall(r'&quot;t&quot;:&quot;([^&]*?)&quot;', r.text)
                for idx, raw_u in enumerate(murls[:12]):
                    u = _html.unescape(raw_u)
                    fb_u = _html.unescape(turls[idx]) if idx < len(turls) else ''
                    t_lbl = _html.unescape(titles[idx]) if idx < len(titles) else (short_title or clean_title or api_q)
                    out.append({
                        'url': u,
                        'fallback_url': fb_u,
                        'title': t_lbl,
                        'authors': clean_authors,
                        'source': 'Web Image Search',
                    })
        except Exception:
            pass
        return out

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    try:
        f_apple = ex.submit(_fetch_apple_books)
        f_google = ex.submit(_fetch_google_books)
        f_ol = ex.submit(_fetch_open_library)
        f_web = ex.submit(_fetch_web_image_covers)
        futs = (f_apple, f_google, f_ol, f_web)
        concurrent.futures.wait(futs, timeout=3.2)
        for fut in futs:
            if fut.done():
                try:
                    for entry in (fut.result(timeout=0.05) or []):
                        _add(entry['url'], entry['title'], entry['authors'], entry['source'], entry.get('fallback_url', ''))
                except Exception:
                    pass
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    return results[:18]


def apply_book_cover_to_item(item_or_id: Any, cover_url: str, fallback_url: str = '') -> Dict[str, Any]:
    """
    Download `cover_url` (or `fallback_url` if the primary host blocks or fails),
    convert it into a crisp base64 JPEG thumbnail (`cover_thumbnail`),
    and save both `cover_image_url` and `cover_thumbnail` onto `CatalogItem` in BookDB
    (plus `scanner_state.db` if tracked).
    """
    import base64
    import io
    import requests

    if isinstance(item_or_id, CatalogItem):
        item = item_or_id
    else:
        item = CatalogItem.objects.prefetch_related('files').filter(id=item_or_id).first()
    if not item:
        return {'ok': False, 'error': f'CatalogItem not found: {item_or_id}'}

    cover_url = (cover_url or '').strip()
    fallback_url = (fallback_url or '').strip()
    if not cover_url and not fallback_url:
        return {'ok': False, 'error': 'cover_url is required'}

    thumb_data_uri = ''
    chosen_ext_url = ''
    if cover_url.startswith('data:image/'):
        thumb_data_uri = cover_url
    else:
        dl_headers = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/124.0.0.0 Safari/537.36'
            ),
            'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
        }
        last_err = ''
        for cand_url in [u for u in (cover_url, fallback_url) if u and not u.startswith('data:')]:
            try:
                resp = requests.get(cand_url, timeout=8.0, headers=dl_headers)
                if resp.status_code == 200 and len(resp.content) > 400:
                    from PIL import Image
                    img = Image.open(io.BytesIO(resp.content))
                    if img.mode not in ('RGB', 'L'):
                        img = img.convert('RGB')
                    img.thumbnail((360, 540), Image.Resampling.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, format='JPEG', quality=86, optimize=True)
                    b64 = base64.b64encode(buf.getvalue()).decode('ascii')
                    thumb_data_uri = f'data:image/jpeg;base64,{b64}'
                    chosen_ext_url = cand_url
                    break
                else:
                    last_err = f'HTTP {resp.status_code}'
            except Exception as dl_err:
                last_err = str(dl_err)
                continue

        if not thumb_data_uri:
            return {
                'ok': False,
                'error': f'Could not download or decode selected cover image ({last_err or "invalid image"}). Try another cover.',
            }

    if chosen_ext_url:
        item.cover_image_url = chosen_ext_url[:999]
    elif not cover_url.startswith('data:'):
        item.cover_image_url = cover_url[:999]
    item.cover_thumbnail = thumb_data_uri
    item.save(update_fields=['cover_image_url', 'cover_thumbnail', 'updated_at'])

    # Also persist updated cover_thumbnail in scanner_state.db if this item's files are tracked there
    try:
        db_path = _get_scanner_db_path()
        if db_path and db_path.exists():
            f_paths = [str(f.file_path) for f in item.files.all() if f.file_path]
            if f_paths:
                conn = sqlite3.connect(str(db_path), timeout=5.0)
                try:
                    placeholders = ','.join(['?'] * len(f_paths))
                    conn.execute(
                        f"UPDATE processed_files SET cover_thumbnail = ? WHERE file_path IN ({placeholders})",
                        [thumb_data_uri, *f_paths],
                    )
                    conn.commit()
                finally:
                    conn.close()
    except Exception:
        pass

    return {
        'ok': True,
        'item_id': str(item.id),
        'cover_url': item.cover_thumbnail or item.cover_image_url or cover_url,
        'external_cover_url': item.cover_image_url or '',
    }


_INTERESTING_TABLE_READY = False


def _ensure_interesting_books_table() -> None:
    global _INTERESTING_TABLE_READY
    if _INTERESTING_TABLE_READY:
        return
    try:
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS catalog_interesting_books (
                    item_id VARCHAR(64) PRIMARY KEY,
                    marked_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        _INTERESTING_TABLE_READY = True
    except Exception:
        pass


def get_interesting_item_ids() -> List[str]:
    """
    Return the list of `CatalogItem` UUID strings currently marked as Interesting,
    ordered by most recently marked first.
    """
    _ensure_interesting_books_table()
    try:
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute("SELECT item_id FROM catalog_interesting_books ORDER BY marked_at DESC")
            return [str(r[0]) for r in cur.fetchall() if r and r[0]]
    except Exception:
        return []


def set_book_interesting(item_id: Any, is_interesting: Optional[bool] = None) -> Dict[str, Any]:
    """
    Mark or unmark a `CatalogItem` as Interesting (or toggle if `is_interesting` is None).
    Returns `{"ok": True, "item_id": str, "is_interesting": bool, "interesting_count": int}`.
    """
    _ensure_interesting_books_table()
    item_id_str = str(item_id or '').strip()
    if not item_id_str:
        return {'ok': False, 'error': 'item_id is required'}
    try:
        from django.db import connection
        with connection.cursor() as cur:
            if is_interesting is None:
                cur.execute("SELECT 1 FROM catalog_interesting_books WHERE item_id = %s", [item_id_str])
                exists = cur.fetchone() is not None
                target_state = not exists
            else:
                target_state = bool(is_interesting)

            if target_state:
                cur.execute(
                    """
                    INSERT INTO catalog_interesting_books (item_id, marked_at)
                    VALUES (%s, CURRENT_TIMESTAMP)
                    ON CONFLICT (item_id) DO UPDATE SET marked_at = CURRENT_TIMESTAMP
                    """,
                    [item_id_str],
                )
            else:
                cur.execute("DELETE FROM catalog_interesting_books WHERE item_id = %s", [item_id_str])

            cur.execute("SELECT COUNT(*) FROM catalog_interesting_books")
            row = cur.fetchone()
            total_cnt = int(row[0]) if row else 0
        return {
            'ok': True,
            'status': 'success',
            'item_id': item_id_str,
            'is_interesting': target_state,
            'interesting_count': total_cnt,
        }
    except Exception as err:
        return {'ok': False, 'error': str(err)}


def clear_book_interesting(item_id: Any) -> bool:
    """
    Reset (clear) the Interesting flag for `item_id` (called automatically when a book is downloaded).
    """
    item_id_str = str(item_id or '').strip()
    if not item_id_str:
        return False
    res = set_book_interesting(item_id_str, False)
    return bool(res.get('ok'))


_BAD_META_TABLE_READY = False


def _ensure_bad_metadata_table() -> None:
    global _BAD_META_TABLE_READY
    if _BAD_META_TABLE_READY:
        return
    try:
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS catalog_bad_metadata_books (
                    item_id VARCHAR(64) PRIMARY KEY,
                    reason TEXT DEFAULT '',
                    marked_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        _BAD_META_TABLE_READY = True
    except Exception:
        pass


def get_bad_metadata_item_ids() -> List[str]:
    """
    Return the list of `CatalogItem` UUID strings currently flagged as Bad Metadata,
    ordered by most recently flagged first.
    """
    _ensure_bad_metadata_table()
    try:
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute("SELECT item_id FROM catalog_bad_metadata_books ORDER BY marked_at DESC")
            return [str(r[0]) for r in cur.fetchall() if r and r[0]]
    except Exception:
        return []


def set_book_bad_metadata(item_id: Any, is_bad: Optional[bool] = None, reason: str = '') -> Dict[str, Any]:
    """
    Mark or unmark a `CatalogItem` as Bad Metadata (or toggle if `is_bad` is None).
    Returns `{"ok": True, "status": "success", "item_id": str, "is_bad_metadata": bool, "bad_metadata_count": int}`.
    """
    _ensure_bad_metadata_table()
    item_id_str = str(item_id or '').strip()
    if not item_id_str:
        return {'ok': False, 'error': 'item_id is required'}
    try:
        from django.db import connection
        with connection.cursor() as cur:
            if is_bad is None:
                cur.execute("SELECT 1 FROM catalog_bad_metadata_books WHERE item_id = %s", [item_id_str])
                exists = cur.fetchone() is not None
                target_state = not exists
            else:
                target_state = bool(is_bad)

            if target_state:
                cur.execute(
                    """
                    INSERT INTO catalog_bad_metadata_books (item_id, reason, marked_at)
                    VALUES (%s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (item_id) DO UPDATE SET
                        reason = CASE WHEN EXCLUDED.reason != '' THEN EXCLUDED.reason ELSE catalog_bad_metadata_books.reason END,
                        marked_at = CURRENT_TIMESTAMP
                    """,
                    [item_id_str, (reason or 'Flagged as Bad Metadata')[:500]],
                )
            else:
                cur.execute("DELETE FROM catalog_bad_metadata_books WHERE item_id = %s", [item_id_str])

            cur.execute("SELECT COUNT(*) FROM catalog_bad_metadata_books")
            row = cur.fetchone()
            total_cnt = int(row[0]) if row else 0
        return {
            'ok': True,
            'status': 'success',
            'item_id': item_id_str,
            'is_bad_metadata': target_state,
            'bad_metadata_count': total_cnt,
        }
    except Exception as err:
        return {'ok': False, 'error': str(err)}


def clear_book_bad_metadata(item_id: Any) -> bool:
    """
    Reset (clear) the Bad Metadata flag for `item_id` (called automatically when a book is enriched or approved).
    """
    item_id_str = str(item_id or '').strip()
    if not item_id_str:
        return False
    res = set_book_bad_metadata(item_id_str, False)
    return bool(res.get('ok'))


def _get_enriched_paths_list(limit: int = 2500) -> List[str]:
    """
    Return `file_path` values from `scanner_state.db` / `BookDB` where `status = 'SUCCESS'`,
    ordered by most recently enriched first.
    """
    db_path = _get_scanner_db_path()
    if not is_postgres_scanner_enabled() and not db_path.exists():
        return []
    try:
        conn = connect_scanner_db(db_path, timeout=10.0)
        conn.text_factory = lambda b: b.decode('utf-8', errors='replace')
        rows = conn.execute(
            "SELECT file_path FROM processed_files WHERE status = 'SUCCESS' ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return [r[0] for r in rows if r and r[0]]
    except Exception:
        return []


def _opds_base_url(request: HttpRequest) -> str:
    script = (getattr(settings, 'FORCE_SCRIPT_NAME', '') or '').rstrip('/')
    if not script and request.path.startswith('/mylibrary/'):
        script = '/mylibrary'
    return f"{script}/opds"


def _render_navigation_feed(
    request: HttpRequest,
    feed_id: str,
    title: str,
    entries: List[Dict[str, str]],
) -> HttpResponse:
    base = _opds_base_url(request)
    now_iso = timezone.now().strftime('%Y-%m-%dT%H:%M:%SZ')
    xml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<feed xmlns="http://www.w3.org/2005/Atom"',
        '      xmlns:dc="http://purl.org/dc/terms/"',
        '      xmlns:opds="http://opds-spec.org/2010/catalog"',
        '      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">',
        f'  <id>{html.escape(feed_id)}</id>',
        f'  <title>{html.escape(title)}</title>',
        f'  <updated>{now_iso}</updated>',
        '  <author><name>MyLibrary OPDS &amp; Calibre Server</name></author>',
        f'  <link rel="start" href="{base}/" type="application/atom+xml;profile=opds-catalog;kind=navigation" title="Home"/>',
        f'  <link rel="self" href="{html.escape(request.get_full_path())}" type="application/atom+xml;profile=opds-catalog;kind=navigation"/>',
        f'  <link rel="search" href="{base}/opensearch.xml" type="application/opensearchdescription+xml" title="Search MyLibrary"/>',
        f'  <link rel="search" href="{base}/search/?q={{searchTerms}}" type="application/atom+xml" title="Search MyLibrary"/>',
    ]

    for ent in entries:
        ent_kind = ent.get('kind', 'acquisition')
        xml_lines.extend([
            '  <entry>',
            f'    <title>{html.escape(ent["title"])}</title>',
            f'    <id>{html.escape(ent["id"])}</id>',
            f'    <updated>{now_iso}</updated>',
            f'    <content type="text">{html.escape(ent.get("content", ""))}</content>',
            f'    <link rel="subsection" href="{html.escape(ent["href"])}" type="application/atom+xml;profile=opds-catalog;kind={ent_kind}"/>',
            '  </entry>',
        ])

    xml_lines.append('</feed>')
    return HttpResponse(
        '\n'.join(xml_lines),
        content_type='application/atom+xml; charset=utf-8',
    )


def _get_amazon_state_file_path() -> Path:
    """
    Return path to the persisted Amazon Kindle / Kindle+ / Audible state file inside
    the bind-mounted `scanner/` folder so it survives container restarts.
    """
    base = Path(getattr(settings, 'BASE_DIR', '.'))
    scanner_dir = base / 'scanner'
    if scanner_dir.exists():
        return scanner_dir / 'amazon_library_state.json'
    return base / 'amazon_library_state.json'


def save_and_link_amazon_items(raw_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Link incoming Amazon Kindle, Kindle+, and Audible items (and Kindle bookmarks/reading %)
    against BookDB `CatalogItem` records using fast in-memory indexing, persist to
    `scanner/amazon_library_state.json`, and invalidate the live Amazon shelf cache.
    """
    state_file = _get_amazon_state_file_path()
    existing_by_key: Dict[str, Dict[str, Any]] = {}
    if state_file.exists():
        try:
            old_list = json.loads(state_file.read_text(encoding='utf-8'))
            for r in old_list:
                k = r.get('asin') or f"{r.get('item_type', 'KINDLE')}:{r.get('title', '')}"
                if k:
                    existing_by_key[k] = r
        except Exception:
            pass

    def _short_norm_title(t: str) -> str:
        base_t = re.split(r'[:(\-–—]', t or '', maxsplit=1)[0].strip()
        return _norm_lookup_str(base_t) if len(base_t) >= 4 else _norm_lookup_str(t)

    # Single-query in-memory index of all CatalogItem rows for sub-second batch matching
    ci_by_asin: Dict[str, CatalogItem] = {}
    ci_by_norm_title: Dict[str, List[CatalogItem]] = {}
    ci_by_short_title: Dict[str, List[CatalogItem]] = {}
    for ci_obj in CatalogItem.objects.only('id', 'title', 'authors', 'asin', 'cover_image_url', 'item_type'):
        if ci_obj.asin:
            ci_by_asin[ci_obj.asin.strip().upper()] = ci_obj
        nt = _norm_lookup_str(ci_obj.title)
        if nt:
            ci_by_norm_title.setdefault(nt, []).append(ci_obj)
        st = _short_norm_title(ci_obj.title)
        if st and len(st) >= 4:
            ci_by_short_title.setdefault(st, []).append(ci_obj)

    kindle_purchased = 0
    kindle_plus = 0
    audible_total = 0
    with_bookmarks = 0
    matched_catalog_items = 0
    to_update_map: Dict[Any, CatalogItem] = {}

    for idx, item_data in enumerate(raw_items, start=1):
        asin = str(item_data.get('asin') or '').strip()
        asin_upper = asin.upper()
        title = _clean_single_line(item_data.get('title') or 'Untitled')
        authors = _clean_single_line(item_data.get('authors') or 'Unknown Author')
        itype = str(item_data.get('item_type') or 'KINDLE').strip().upper()
        origin_type = str(item_data.get('origin_type') or 'PURCHASE').strip().upper()
        is_kindle_plus = bool(item_data.get('is_kindle_plus', origin_type in ('KINDLE_UNLIMITED', 'PRIME_READING', 'PRIME', 'COMIXOLOGY', 'AUDIBLE_PLUS')))
        badge_label = str(item_data.get('badge_label') or (
            'Audible+ (Plus Catalog)' if (itype == 'AUDIBLE' and is_kindle_plus)
            else 'Purchased on Audible' if itype == 'AUDIBLE'
            else 'Kindle+ (Kindle Unlimited)' if origin_type == 'KINDLE_UNLIMITED'
            else 'Kindle+ (Prime Reading)' if is_kindle_plus
            else 'Purchased on Amazon'
        ))
        short_badge = str(item_data.get('short_badge') or (
            'Audible+' if (itype == 'AUDIBLE' and is_kindle_plus)
            else 'Purchased on Audible' if itype == 'AUDIBLE'
            else 'Kindle+' if is_kindle_plus
            else 'Purchased on Amazon'
        ))
        cover_url = str(item_data.get('cover_image_url') or '').strip()
        pct_read = int(item_data.get('percentage_read') or 0)
        has_bm = bool(item_data.get('has_bookmarks') or pct_read > 0 or item_data.get('last_annotated_date'))
        last_annot = str(item_data.get('last_annotated_date') or '').strip()
        annot_cnt = int(item_data.get('annotations_count') or 0)
        notes_prev = str(item_data.get('notes_preview') or '').strip()
        recency_rank = int(item_data.get('recency_rank') or idx)
        web_url = str(item_data.get('web_reader_url') or (f'https://read.amazon.com/?asin={asin}' if asin and itype == 'KINDLE' else '')).strip()

        nt = _norm_lookup_str(title)
        st = _short_norm_title(title)
        na = _norm_lookup_str(authors)
        author_tokens = {t for t in na.split() if len(t) >= 2}

        ci: Optional[CatalogItem] = None
        if asin_upper and asin_upper in ci_by_asin:
            ci = ci_by_asin[asin_upper]

        if not ci:
            for cand_key, lookup_dict in ((nt, ci_by_norm_title), (st, ci_by_short_title)):
                if not cand_key or cand_key not in lookup_dict:
                    continue
                cands = lookup_dict[cand_key]
                # Prefer matching a BOOK (physical NAS file) first when author tokens overlap
                sorted_cands = sorted(cands, key=lambda c: 0 if c.item_type == 'BOOK' else 1)
                for c_item in sorted_cands:
                    c_tokens = set(_norm_lookup_str(c_item.authors).split())
                    if (author_tokens and (author_tokens & c_tokens)) or not author_tokens or na == 'unknownauthor':
                        ci = c_item
                        break
                if ci:
                    break

        if ci:
            if ci.item_type == 'BOOK':
                matched_catalog_items += 1
            updated = False
            if asin and not ci.asin:
                ci.asin = asin[:32]
                ci_by_asin[asin_upper] = ci
                updated = True
            if cover_url and not ci.cover_image_url:
                ci.cover_image_url = cover_url[:1024]
                updated = True
            if updated:
                to_update_map[ci.id] = ci
        else:
            ci = CatalogItem.objects.create(
                title=title[:512],
                authors=authors[:512],
                asin=asin[:32] if asin else None,
                item_type='AUDIBLE' if itype == 'AUDIBLE' else 'KINDLE',
                cover_image_url=cover_url[:1024] if cover_url else None,
            )
            if asin_upper:
                ci_by_asin[asin_upper] = ci
            if nt:
                ci_by_norm_title.setdefault(nt, []).append(ci)
            if st and len(st) >= 4:
                ci_by_short_title.setdefault(st, []).append(ci)

        rec = {
            'asin': asin,
            'title': title,
            'authors': authors,
            'item_type': itype,
            'origin_type': origin_type,
            'is_kindle_plus': is_kindle_plus,
            'badge_label': badge_label,
            'short_badge': short_badge,
            'cover_image_url': cover_url,
            'percentage_read': pct_read,
            'recency_rank': recency_rank,
            'has_bookmarks': has_bm,
            'annotations_count': annot_cnt,
            'last_annotated_date': last_annot,
            'notes_preview': notes_prev,
            'web_reader_url': web_url,
            'catalog_item_id': str(ci.id),
            'norm_title': nt,
            'norm_author': na,
        }
        key = asin or f"{itype}:{title}"
        existing_by_key[key] = rec

    if to_update_map:
        CatalogItem.objects.bulk_update(list(to_update_map.values()), ['asin', 'cover_image_url'], batch_size=500)

    merged_list = list(existing_by_key.values())
    # Sort merged_list so most recently annotated / lowest recency_rank comes first
    merged_list.sort(
        key=lambda x: (
            0 if x.get('last_annotated_date') else 1,
            -(int(x.get('last_annotated_date', '').replace('-', '') or 0)) if x.get('last_annotated_date', '').replace('-', '').isdigit() else 0,
            int(x.get('recency_rank') or 999999),
        )
    )

    for r in merged_list:
        if r.get('item_type') == 'AUDIBLE':
            audible_total += 1
        elif r.get('is_kindle_plus'):
            kindle_plus += 1
        else:
            kindle_purchased += 1
        if r.get('has_bookmarks'):
            with_bookmarks += 1

    state_file.write_text(json.dumps(merged_list, indent=2, ensure_ascii=False), encoding='utf-8')
    _AMAZON_SHELF_CACHE['expires_at'] = 0.0
    return {
        'ok': True,
        'total_synced': len(merged_list),
        'kindle_purchased': kindle_purchased,
        'kindle_plus': kindle_plus,
        'audible_total': audible_total,
        'with_bookmarks': with_bookmarks,
        'matched_catalog_items': matched_catalog_items,
    }


def get_dynamic_amazon_shelf() -> Dict[str, Any]:
    """
    Load the indexed Amazon Kindle / Kindle+ / Audible ownership and bookmark state.
    Re-evaluates immediately whenever `scanner/amazon_library_state.json` changes.
    """
    state_file = _get_amazon_state_file_path()
    now = time.monotonic()
    file_mtime = state_file.stat().st_mtime if state_file.exists() else 0.0

    if (
        now < _AMAZON_SHELF_CACHE['expires_at']
        and file_mtime == _AMAZON_SHELF_CACHE['file_mtime']
    ):
        return _AMAZON_SHELF_CACHE

    by_item_id: Dict[str, Dict[str, Any]] = {}
    by_asin: Dict[str, Dict[str, Any]] = {}
    by_norm_title: Dict[str, Dict[str, Any]] = {}
    all_ids: List[str] = []
    kindle_purchased_ids: List[str] = []
    kindle_plus_ids: List[str] = []
    kindle_bookmarked_ids: List[str] = []
    audible_ids: List[str] = []

    raw_list: List[Dict[str, Any]] = []
    if state_file.exists():
        try:
            raw_list = json.loads(state_file.read_text(encoding='utf-8'))
        except Exception:
            raw_list = []

    # Also include any existing CatalogItem with item_type in ('KINDLE', 'AUDIBLE') or non-empty ASIN if state_file was empty
    if not raw_list:
        try:
            qs = CatalogItem.objects.filter(Q(item_type__in=['KINDLE', 'AUDIBLE']) | (Q(asin__isnull=False) & ~Q(asin='')))[:2000]
            for idx, ci in enumerate(qs, start=1):
                raw_list.append({
                    'asin': ci.asin or '',
                    'title': ci.title,
                    'authors': ci.authors,
                    'item_type': 'AUDIBLE' if ci.item_type == 'AUDIBLE' else 'KINDLE',
                    'origin_type': 'AUDIBLE_PURCHASE' if ci.item_type == 'AUDIBLE' else 'PURCHASE',
                    'is_kindle_plus': False,
                    'badge_label': 'Purchased on Audible' if ci.item_type == 'AUDIBLE' else 'Purchased on Amazon',
                    'short_badge': 'Purchased on Audible' if ci.item_type == 'AUDIBLE' else 'Purchased on Amazon',
                    'percentage_read': 0,
                    'recency_rank': idx,
                    'has_bookmarks': False,
                    'annotations_count': 0,
                    'last_annotated_date': '',
                    'notes_preview': '',
                    'web_reader_url': f'https://read.amazon.com/?asin={ci.asin}' if ci.asin else '',
                    'catalog_item_id': str(ci.id),
                    'norm_title': _norm_lookup_str(ci.title),
                    'norm_author': _norm_lookup_str(ci.authors),
                })
        except Exception:
            pass

    seen_all = set()
    for r in raw_list:
        cid = r.get('catalog_item_id')
        asin = r.get('asin')
        nt = r.get('norm_title') or _norm_lookup_str(r.get('title'))
        if cid:
            by_item_id[str(cid)] = r
            if cid not in seen_all:
                seen_all.add(cid)
                all_ids.append(cid)
                if r.get('item_type') == 'AUDIBLE':
                    audible_ids.append(cid)
                elif r.get('is_kindle_plus'):
                    kindle_plus_ids.append(cid)
                else:
                    kindle_purchased_ids.append(cid)
                if r.get('has_bookmarks'):
                    kindle_bookmarked_ids.append(cid)
        if asin:
            by_asin[asin.upper()] = r
        if nt and len(nt) >= 3 and nt not in by_norm_title:
            by_norm_title[nt] = r

    # Identify which Amazon items have a local physical NAS file vs Amazon Cloud Only (not on NAS)
    ids_with_local_files: set = set()
    if all_ids:
        try:
            ids_with_local_files = {
                str(pk)
                for pk in CatalogItem.objects.filter(id__in=all_ids, files__isnull=False)
                .values_list('id', flat=True)
                .distinct()
            }
        except Exception:
            ids_with_local_files = set()

    amazon_only_ids: List[str] = []
    amazon_matched_ids: List[str] = []
    for cid in all_ids:
        has_local = str(cid) in ids_with_local_files
        if str(cid) in by_item_id:
            by_item_id[str(cid)]['has_local_file'] = has_local
        if has_local:
            amazon_matched_ids.append(cid)
        else:
            amazon_only_ids.append(cid)

    _AMAZON_SHELF_CACHE.update({
        'expires_at': now + 1800.0,
        'file_mtime': file_mtime,
        'by_item_id': by_item_id,
        'by_asin': by_asin,
        'by_norm_title': by_norm_title,
        'all_ids': all_ids,
        'kindle_purchased_ids': kindle_purchased_ids,
        'kindle_plus_ids': kindle_plus_ids,
        'kindle_bookmarked_ids': kindle_bookmarked_ids,
        'audible_ids': audible_ids,
        'amazon_only_ids': amazon_only_ids,
        'amazon_matched_ids': amazon_matched_ids,
        'counts': {
            'total': len(all_ids),
            'kindle_purchased': len(kindle_purchased_ids),
            'kindle_plus': len(kindle_plus_ids),
            'kindle_bookmarked': len(kindle_bookmarked_ids),
            'audible': len(audible_ids),
            'amazon_only': len(amazon_only_ids),
            'amazon_matched': len(amazon_matched_ids),
        },
    })
    return _AMAZON_SHELF_CACHE


def lookup_amazon_info_for_item(item: CatalogItem) -> Optional[Dict[str, Any]]:
    """
    Check if a `CatalogItem` (even if it is a local NAS `.epub` `BOOK`) is owned on
    Amazon Kindle, Kindle+, or Audible, and return its ownership & bookmark metadata.
    """
    amz = get_dynamic_amazon_shelf()
    str_id = str(item.id)
    if str_id in amz['by_item_id']:
        return amz['by_item_id'][str_id]
    if item.asin and item.asin.upper() in amz['by_asin']:
        return amz['by_asin'][item.asin.upper()]
    nt = _norm_lookup_str(item.title)
    if nt and nt in amz['by_norm_title']:
        cand = amz['by_norm_title'][nt]
        na_item = _norm_lookup_str(item.authors)
        na_cand = cand.get('norm_author') or _norm_lookup_str(cand.get('authors'))
        if not na_item or not na_cand or (set(na_item.split()) & set(na_cand.split())):
            return cand
    return None


def _find_bookmark_matching_file(files_list: List[FileLocation], moon_info: Optional[Dict[str, Any]]) -> Tuple[List[FileLocation], Optional[FileLocation]]:
    """
    When a book has multiple physical `.epub` files and a saved Moon+ Reader bookmark,
    determine which physical `.epub` file matches the saved bookmark's filename or
    (chapter, char_offset, percentage) spine structure, and place it #1 first.
    """
    if not files_list or not moon_info or len(files_list) <= 1:
        return files_list, (files_list[0] if files_list else None)

    moon_stem_lower = (moon_info.get('moon_stem') or '').lower()
    for f in files_list:
        fn_lower = (f.file_name or '').lower()
        if fn_lower and (fn_lower == moon_stem_lower or _moon_sanitize_stem(fn_lower) == moon_stem_lower):
            reordered = [f] + [x for x in files_list if x.id != f.id]
            return reordered, f

    chap = moon_info.get('chapter') or 0
    offset = moon_info.get('char_offset') or 0
    pct_val = moon_info.get('pct_val') or 0.0

    if chap > 0 and pct_val > 0.0:
        best_file = None
        best_diff = 999.0
        for f in files_list:
            p = _resolve_physical_file_path(f.file_path)
            if not p or not str(p).lower().endswith('.epub'):
                continue
            try:
                with zipfile.ZipFile(p, 'r') as zf:
                    htmls = [n for n in zf.namelist() if n.lower().endswith(('.html', '.xhtml', '.htm'))]
                    if chap >= len(htmls):
                        continue
                    lengths = [zf.getinfo(h).file_size for h in htmls]
                    total_len = sum(lengths)
                    if total_len <= 0:
                        continue
                    pos_len = sum(lengths[:chap]) + offset
                    est_pct = (pos_len / total_len) * 100.0
                    diff = abs(est_pct - pct_val)
                    if diff < best_diff:
                        best_diff = diff
                        best_file = f
            except Exception:
                continue

        if best_file is not None:
            reordered = [best_file] + [x for x in files_list if x.id != best_file.id]
            return reordered, best_file

    return files_list, files_list[0]


def _render_acquisition_feed(
    request: HttpRequest,
    feed_id: str,
    title: str,
    items: List[CatalogItem],
    page: int = 1,
    has_next: bool = False,
    total_count: Optional[int] = None,
) -> HttpResponse:
    base = _opds_base_url(request)
    now_iso = timezone.now().strftime('%Y-%m-%dT%H:%M:%SZ')

    xml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<feed xmlns="http://www.w3.org/2005/Atom"',
        '      xmlns:dc="http://purl.org/dc/terms/"',
        '      xmlns:opds="http://opds-spec.org/2010/catalog"',
        '      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">',
        f'  <id>{html.escape(feed_id)}</id>',
        f'  <title>{html.escape(title)}</title>',
        f'  <updated>{now_iso}</updated>',
        '  <author><name>MyLibrary OPDS &amp; Calibre Server</name></author>',
        f'  <link rel="start" href="{base}/" type="application/atom+xml;profile=opds-catalog;kind=navigation" title="Home"/>',
        f'  <link rel="self" href="{html.escape(request.get_full_path())}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>',
        f'  <link rel="search" href="{base}/opensearch.xml" type="application/opensearchdescription+xml" title="Search MyLibrary"/>',
        f'  <link rel="search" href="{base}/search/?q={{searchTerms}}" type="application/atom+xml" title="Search MyLibrary"/>',
    ]

    if total_count is not None:
        xml_lines.append(f'  <opensearch:totalResults>{total_count}</opensearch:totalResults>')

    # Pagination links
    parsed_qs = request.GET.copy()
    if page > 1:
        parsed_qs['page'] = str(page - 1)
        prev_href = f"{request.path}?{parsed_qs.urlencode()}"
        xml_lines.append(f'  <link rel="previous" href="{html.escape(prev_href)}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>')
    if has_next:
        parsed_qs['page'] = str(page + 1)
        next_href = f"{request.path}?{parsed_qs.urlencode()}"
        xml_lines.append(f'  <link rel="next" href="{html.escape(next_href)}" type="application/atom+xml;profile=opds-catalog;kind=acquisition"/>')

    for item in items:
        upd_iso = item.updated_at.strftime('%Y-%m-%dT%H:%M:%SZ') if item.updated_at else now_iso
        authors_list = [a.strip() for a in (item.authors or 'Unknown Author').split(',') if a.strip()] or ['Unknown Author']
        summary_txt = _strip_html(item.description or '')

        moon_info = getattr(item, 'moon_bookmark_info', None)
        # Sort physical files with largest/full edition first (and non-DRM/valid preferred)
        files_list = sorted(
            list(item.files.all()),
            key=lambda f: (bool(f.has_drm), bool(f.is_invalid), -(f.file_size or 0)),
        )
        matched_bm_file = None
        if moon_info and len(files_list) > 1:
            files_list, matched_bm_file = _find_bookmark_matching_file(files_list, moon_info)

        header_lines: List[str] = []
        full_title = item.title or 'Untitled'
        entry_title = full_title
        if ':' in full_title and len(f"{full_title} - {authors_list[0]}") > 95:
            main_part = full_title.split(':', 1)[0].strip()
            if len(main_part) >= 4:
                entry_title = main_part
                header_lines.append(f"📖 Full Title: {full_title}")

        item_is_enriched = any(is_file_enriched(f.file_path) for f in files_list if f.file_path)
        has_enrichable = any(
            (f.file_format or '').upper().lstrip('.') in ('EPUB', 'PDF')
            for f in files_list
        )
        if item_is_enriched:
            header_lines.append("✨ Enriched (epubmeta Verified)")
        elif has_enrichable:
            header_lines.append("⚡ On-Demand Enrichment: Auto-enriches metadata when downloaded")

        amz_info = getattr(item, 'amazon_ownership_info', None) or lookup_amazon_info_for_item(item)
        if amz_info:
            asin_str = f" (ASIN: {amz_info['asin']})" if amz_info.get('asin') else ""
            if amz_info.get('item_type') == 'AUDIBLE':
                lbl = amz_info.get('badge_label') or 'Purchased on Audible'
                header_lines.append(f"🎧 {lbl.upper()}{asin_str} — Available in Audible App")
            elif amz_info.get('is_kindle_plus'):
                lbl = amz_info.get('badge_label') or 'Kindle+'
                header_lines.append(f"⚡ {lbl.upper()}{asin_str} — Prefer Reading in Amazon Kindle App")
            else:
                header_lines.append(f"📦 PURCHASED ON AMAZON{asin_str} — Prefer Reading in Amazon Kindle App")

            amz_bm_parts = []
            if (amz_info.get('percentage_read') or 0) > 0:
                amz_bm_parts.append(f"{amz_info['percentage_read']}%")
            if amz_info.get('last_annotated_date'):
                amz_bm_parts.append(f"Last Annotated: {amz_info['last_annotated_date']}")
            elif amz_info.get('has_bookmarks'):
                amz_bm_parts.append("Has Kindle Highlights/Bookmarks")
            if amz_info.get('notes_preview'):
                amz_bm_parts.append(amz_info['notes_preview'])
            if amz_bm_parts:
                header_lines.append(f"★ Amazon Bookmark / Progress: {' · '.join(amz_bm_parts)}")

        if moon_info:
            if moon_info.get('mtime_iso'):
                upd_iso = moon_info['mtime_iso']
            badge_parts = []
            if moon_info.get('pct'):
                badge_parts.append(moon_info['pct'])
            if moon_info.get('date_short'):
                badge_parts.append(moon_info['date_short'])
            if moon_info.get('has_an'):
                badge_parts.append('Highlights/Bookmarks')
            badge_str = ' · '.join(badge_parts)
            if badge_str:
                header_lines.append(f"★ Saved Bookmark: {badge_str}")
                if matched_bm_file and len(files_list) > 1:
                    bm_kb = round((matched_bm_file.file_size or 0) / 1000)
                    bm_fmt = (matched_bm_file.file_format or 'epub').lower().lstrip('.')
                    bm_name = matched_bm_file.file_name or os.path.basename(matched_bm_file.file_path or '')
                    header_lines.append(f"👉 TAP \"Download 1: {bm_fmt}\" FOR BOOKMARK ({bm_kb:,} KB — {bm_name})")

        if len(files_list) > 1:
            header_lines.append("Download Buttons (in order):")
            for idx_f, f_obj in enumerate(files_list, 1):
                # Moon+ Reader displays decimal KB (file_size / 1000) or "Download N: epub" in its download chooser
                sz_kb_moon = round((f_obj.file_size or 0) / 1000)
                sz_label = f"{sz_kb_moon:,} KB"
                fmt_lbl = (f_obj.file_format or 'epub').lower().lstrip('.')
                nas_name = f_obj.file_name or os.path.basename(f_obj.file_path or '')
                if matched_bm_file and f_obj.id == matched_bm_file.id:
                    header_lines.append(f"  • Download {idx_f}: {fmt_lbl} [{sz_label} ★ BOOKMARK MATCH] — {nas_name}")
                else:
                    header_lines.append(f"  • Download {idx_f}: {fmt_lbl} [{sz_label}] — {nas_name}")

        if header_lines:
            summary_txt = "\n".join(header_lines) + ("\n\n" + summary_txt if summary_txt else "")

        xml_lines.extend([
            '  <entry>',
            f'    <title>{html.escape(entry_title)}</title>',
            f'    <id>urn:uuid:{item.id}</id>',
            f'    <updated>{upd_iso}</updated>',
        ])
        for auth in authors_list:
            xml_lines.append(f'    <author><name>{html.escape(auth)}</name></author>')
        if item.publisher:
            xml_lines.append(f'    <dc:publisher>{html.escape(item.publisher)}</dc:publisher>')
        if item.isbn:
            xml_lines.append(f'    <dc:identifier>urn:isbn:{html.escape(item.isbn)}</dc:identifier>')
        if item.pub_date:
            xml_lines.append(f'    <dc:issued>{item.pub_date.isoformat()}</dc:issued>')
        flag_bad_href = f"{base}/flag-bad-meta/{item.id}/"
        action_bar_html = (
            f'<br/><br/>🚩 <a href="{html.escape(flag_bad_href)}">Tap Here to Flag as Bad Metadata</a> '
            f'(or add a bookmark/note with <b>#bad</b> in Moon+ Reader)'
        )
        summary_html = (html.escape(summary_txt).replace('\n', '<br/>') if summary_txt else '') + action_bar_html
        xml_lines.append(f'    <summary type="html">{html.escape(summary_html)}</summary>')
        xml_lines.append(f'    <content type="html">{html.escape(summary_html)}</content>')
        xml_lines.append(f'    <link rel="alternate" href="{html.escape(flag_bad_href)}" type="text/html" title="🚩 Flag as Bad Metadata"/>')

        # Cover links
        cover_href = f"{base}/cover/{item.id}.jpg"
        xml_lines.append(f'    <link rel="http://opds-spec.org/image" href="{cover_href}" type="image/jpeg"/>')
        xml_lines.append(f'    <link rel="http://opds-spec.org/image/thumbnail" href="{cover_href}" type="image/jpeg"/>')

        # Acquisition links for each physical file (bookmark-matching edition is always Download 1)
        for idx_f, f in enumerate(files_list, 1):
            fmt_upper = (f.file_format or 'EPUB').upper().lstrip('.')
            mime = MIME_MAP.get(fmt_upper, 'application/octet-stream')
            dl_filename = _calibre_normalize_filename(item.title, item.authors, fmt_upper.lower())
            encoded_fname = urllib.parse.quote(dl_filename)
            dl_href = f"{base}/download/{f.id}/{encoded_fname}"
            nas_name = f.file_name or dl_filename
            sz_kb = round((f.file_size or 0) / 1000)
            f_enriched = is_file_enriched(f.file_path)
            enr_tag = "✨ Enriched" if f_enriched else "⚡ Auto-Enrich"
            bm_tag = " ★ BOOKMARK MATCH" if (matched_bm_file and f.id == matched_bm_file.id) else ""
            xml_lines.append(
                f'    <link rel="http://opds-spec.org/acquisition" href="{dl_href}" '
                f'type="{mime}" length="{f.file_size or 0}" title="Download {idx_f}: {html.escape(fmt_upper)} ({sz_kb:,} KB{bm_tag} · {enr_tag}) - {html.escape(nas_name)}"/>'
            )

        xml_lines.append('  </entry>')

    xml_lines.append('</feed>')
    return HttpResponse(
        '\n'.join(xml_lines),
        content_type='application/atom+xml; charset=utf-8',
    )


class OPDSRootView(View):
    """
    Root OPDS 1.2 Navigation Catalog (`/mylibrary/opds/`).
    Always responds in <20ms so Moon+ Reader's strict 3-second catalog-addition
    validation timer never times out and falls back to WebView/WebSite mode.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        import threading

        base = _opds_base_url(request)

        from .views import _MAIN_DASHBOARD_CACHE, _get_cached_dashboard_meta

        def _warm_caches_bg() -> None:
            try:
                _get_cached_dashboard_meta()
                get_enriched_item_ids_index()
                get_dynamic_bookmarked_shelf()
                get_enriched_tags_and_folders_index()
                get_dynamic_amazon_shelf()
            except Exception:
                pass

        now = time.monotonic()
        if (
            now >= _BOOKMARKED_SHELF_CACHE['expires_at']
            or now >= _TAGS_SHELF_CACHE['expires_at']
            or now >= _AMAZON_SHELF_CACHE['expires_at']
            or now >= _MAIN_DASHBOARD_CACHE.get('expires_at', 0.0)
            or now >= _ENRICHED_PATHS_CACHE.get('expires_at', 0.0)
        ):
            threading.Thread(target=_warm_caches_bg, daemon=True).start()

        disk_summary = {}
        try:
            summary_file = Path('/tmp/mylibrary_summary_counts.json')
            if summary_file.exists():
                disk_summary = json.loads(summary_file.read_text(encoding='utf-8')) or {}
        except Exception:
            disk_summary = {}

        dash_stats = _MAIN_DASHBOARD_CACHE.get('stats') or disk_summary
        catalog_books_count = dash_stats.get('books_count') or dash_stats.get('total_items') or 0
        total_items_count = dash_stats.get('total_items') or 0
        total_files_count = dash_stats.get('total_files') or 0
        enriched_count = (
            len(_ENRICHED_PATHS_CACHE.get('enriched_item_ids') or [])
            or disk_summary.get('enriched_count')
            or (len(_ENRICHED_PATHS_CACHE.get('paths_set') or ()) // 2)
        )

        bm_count = len(_BOOKMARKED_SHELF_CACHE.get('item_ids') or [])
        tags_count = len(_TAGS_SHELF_CACHE.get('tag_counts') or [])
        amz_total = (_AMAZON_SHELF_CACHE.get('counts') or {}).get('total', 0)
        interesting_ids = get_interesting_item_ids()
        interesting_count = len(interesting_ids)
        bad_meta_ids = get_bad_metadata_item_ids()
        bad_meta_count = len(bad_meta_ids)

        bm_label = f'Bookmarked & Reading in Moon+ ({bm_count:,} Books)' if bm_count else 'Bookmarked & Reading in Moon+'
        interesting_label = f'Interesting Books ({interesting_count:,} Books)' if interesting_count else 'Interesting Books'
        bad_meta_label = f'Bad Metadata Books ({bad_meta_count:,} Books)' if bad_meta_count else 'Bad Metadata Books'
        amz_label = f'Purchased on Amazon, Kindle+ & Audible ({amz_total:,} Titles)' if amz_total else 'Purchased on Amazon, Kindle+ & Audible'
        tags_label = f'Browse Enriched Books by Tag / Subject ({tags_count:,} Tags)' if tags_count else 'Browse Enriched Books by Tag / Subject'
        enriched_label = (
            f'Enriched EPUBs — epubmeta Verified ({enriched_count:,} Enriched Books)'
            if enriched_count
            else 'Enriched EPUBs (epubmeta Verified)'
        )
        if catalog_books_count and total_files_count:
            all_books_label = (
                f'All Catalog Books ({catalog_books_count:,} Catalog Books · {total_files_count:,} Total Books'
                + (f' · {enriched_count:,} Enriched)' if enriched_count else ')')
            )
            all_books_desc = (
                f'Browse the full MyLibrary catalog: {catalog_books_count:,} unique catalog books '
                f'({total_items_count:,} total catalog items), {total_files_count:,} total book/media files on NAS'
                + (f', and {enriched_count:,} enriched books.' if enriched_count else '.')
            )
            root_feed_title = (
                f'MyLibrary OPDS ({catalog_books_count:,} Catalog · {total_files_count:,} Total · {enriched_count:,} Enriched)'
                if enriched_count
                else f'MyLibrary OPDS ({catalog_books_count:,} Catalog · {total_files_count:,} Total)'
            )
        else:
            all_books_label = 'All Catalog Books (A-Z)'
            all_books_desc = 'Browse the full MyLibrary book catalog.'
            root_feed_title = 'MyLibrary OPDS & Calibre Server'

        entries = [
            {
                'id': 'urn:mylibrary:opds:interesting',
                'title': interesting_label,
                'content': 'Books you marked as Interesting to read next (automatically cleared when downloaded).',
                'href': f'{base}/interesting/',
                'kind': 'acquisition',
            },
            {
                'id': 'urn:mylibrary:opds:bad_metadata',
                'title': bad_meta_label,
                'content': 'Books flagged as having Bad Metadata from Moon+ Reader or the web catalog.',
                'href': f'{base}/bad-metadata/',
                'kind': 'acquisition',
            },
            {
                'id': 'urn:mylibrary:opds:bookmarked',
                'title': bm_label,
                'content': 'Dynamic virtual bookshelf of all books with reading positions (.po) or highlights/bookmarks (.an) in your Moon+ Reader WebDAV Cache.',
                'href': f'{base}/bookmarked/',
                'kind': 'acquisition',
            },
            {
                'id': 'urn:mylibrary:opds:amazon',
                'title': amz_label,
                'content': 'Your synced Amazon Kindle purchases, Kindle+ (Kindle Unlimited / Prime Reading), Kindle Bookmarks/Highlights, and Audible Audiobooks.',
                'href': f'{base}/amazon/',
                'kind': 'navigation',
            },
            {
                'id': 'urn:mylibrary:opds:tags',
                'title': tags_label,
                'content': 'Dynamic bookshelves organized by subject tags and library categories on books enriched by epubmeta.',
                'href': f'{base}/tags/',
                'kind': 'navigation',
            },
            {
                'id': 'urn:mylibrary:opds:enriched',
                'title': enriched_label,
                'content': (
                    f'{enriched_count:,} enriched books with verified ISBN, synopsis, author, and embedded cover artwork.'
                    if enriched_count
                    else 'Books with verified ISBN, synopsis, author, and embedded cover artwork.'
                ),
                'href': f'{base}/enriched/',
                'kind': 'acquisition',
            },
            {
                'id': 'urn:mylibrary:opds:recent',
                'title': 'Recently Updated & Enriched Books',
                'content': 'Most recently updated books in your catalog.',
                'href': f'{base}/recent/',
                'kind': 'acquisition',
            },
            {
                'id': 'urn:mylibrary:opds:authors',
                'title': 'Browse by Author (A-Z)',
                'content': 'Browse your library alphabetically by author.',
                'href': f'{base}/authors/',
                'kind': 'navigation',
            },
            {
                'id': 'urn:mylibrary:opds:formats',
                'title': 'Browse by Format (EPUB, PDF, MOBI, Audiobook)',
                'content': 'Filter books by digital file format.',
                'href': f'{base}/formats/',
                'kind': 'navigation',
            },
            {
                'id': 'urn:mylibrary:opds:all',
                'title': all_books_label,
                'content': all_books_desc,
                'href': f'{base}/all/',
                'kind': 'acquisition',
            },
            {
                'id': 'urn:mylibrary:opds:search_keywords',
                'title': '🔍 Search Keywords & Quick Filters Reference',
                'content': (
                    'Search keywords you can type in Moon+ Reader search: '
                    'author:Name, title:Word, pub:Name, isbn:978..., cat:Fiction, tag:Sci-Fi, '
                    'format:epub, type:book, year:2025 (or year:2020..2026), '
                    'is:enriched, is:unenriched, is:reading, is:unread, is:interesting, is:badmeta, '
                    'missing:cover, -exclude, sort:newest, +enrich, +badmeta'
                ),
                'href': f'{base}/search/',
                'kind': 'navigation',
            },
        ]
        return _render_navigation_feed(
            request,
            feed_id='urn:mylibrary:opds:root',
            title=root_feed_title,
            entries=entries,
        )


class OPDSAmazonFeedView(View):
    """
    Dynamic Virtual Bookshelf for Purchased on Amazon, Kindle+ (Kindle Unlimited / Prime),
    Kindle Bookmarked / Highlights, and Audible Audiobooks (`/mylibrary/opds/amazon/`).
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        from django.db.models import Case, IntegerField, When

        base = _opds_base_url(request)
        filter_mode = (request.GET.get('filter') or '').strip().lower()
        amz_data = get_dynamic_amazon_shelf()
        counts = amz_data.get('counts', {})

        if not filter_mode:
            entries = [
                {
                    'id': 'urn:mylibrary:opds:amazon:bookmarked',
                    'title': f'Amazon Kindle Bookmarked & Reading ({counts.get("kindle_bookmarked", 0):,} Books)',
                    'content': 'Amazon Kindle books with saved reading progress, highlights, or notes (most recent first).',
                    'href': f'{base}/amazon/?filter=bookmarked',
                    'kind': 'acquisition',
                },
                {
                    'id': 'urn:mylibrary:opds:amazon:purchased',
                    'title': f'Purchased on Amazon ({counts.get("kindle_purchased", 0):,} Books)',
                    'content': 'Books purchased on Amazon Kindle.',
                    'href': f'{base}/amazon/?filter=purchased',
                    'kind': 'acquisition',
                },
                {
                    'id': 'urn:mylibrary:opds:amazon:kindle_plus',
                    'title': f'Kindle+ / Kindle Unlimited / Prime ({counts.get("kindle_plus", 0):,} Books)',
                    'content': 'Books in your library from Kindle Unlimited, Prime Reading, or ComiXology.',
                    'href': f'{base}/amazon/?filter=kindle_plus',
                    'kind': 'acquisition',
                },
                {
                    'id': 'urn:mylibrary:opds:amazon:audible',
                    'title': f'Purchased on Audible & Audible+ ({counts.get("audible", 0):,} Audiobooks)',
                    'content': 'Your synced Audible audiobooks and Plus Catalog titles.',
                    'href': f'{base}/amazon/?filter=audible',
                    'kind': 'acquisition',
                },
                {
                    'id': 'urn:mylibrary:opds:amazon:amazon_only',
                    'title': f'Amazon Only — Not on Local NAS ({counts.get("amazon_only", 0):,} Books)',
                    'content': 'Amazon Kindle, Kindle+, and Audible books that do NOT match any local NAS file.',
                    'href': f'{base}/amazon/?filter=amazon_only',
                    'kind': 'acquisition',
                },
                {
                    'id': 'urn:mylibrary:opds:amazon:matched',
                    'title': f'Matched to Local NAS Books ({counts.get("amazon_matched", 0):,} Books)',
                    'content': 'Amazon books that also have a local EPUB/PDF/Audiobook copy on your NAS.',
                    'href': f'{base}/amazon/?filter=matched',
                    'kind': 'acquisition',
                },
                {
                    'id': 'urn:mylibrary:opds:amazon:all',
                    'title': f'All Amazon, Kindle+ & Audible Titles ({counts.get("total", 0):,} Total)',
                    'content': 'Browse all synced Amazon Kindle, Kindle+, and Audible items.',
                    'href': f'{base}/amazon/?filter=all',
                    'kind': 'acquisition',
                },
            ]
            return _render_navigation_feed(
                request,
                feed_id='urn:mylibrary:opds:amazon',
                title='Purchased on Amazon, Kindle+ & Audible',
                entries=entries,
            )

        try:
            page = max(int(request.GET.get('page', 1)), 1)
        except ValueError:
            page = 1
        page_size = 25
        offset = (page - 1) * page_size

        if filter_mode == 'bookmarked':
            id_list = amz_data.get('kindle_bookmarked_ids', [])
            feed_title = f'Amazon Kindle Bookmarked & Reading ({len(id_list):,} Books)'
        elif filter_mode == 'purchased':
            id_list = amz_data.get('kindle_purchased_ids', [])
            feed_title = f'Purchased on Amazon ({len(id_list):,} Books)'
        elif filter_mode == 'kindle_plus':
            id_list = amz_data.get('kindle_plus_ids', [])
            feed_title = f'Kindle+ (Kindle Unlimited / Prime) ({len(id_list):,} Books)'
        elif filter_mode == 'audible':
            id_list = amz_data.get('audible_ids', [])
            feed_title = f'Purchased on Audible & Audible+ ({len(id_list):,} Audiobooks)'
        elif filter_mode in ('amazon_only', 'unmatched'):
            id_list = amz_data.get('amazon_only_ids', [])
            feed_title = f'Amazon Only — Not on Local NAS ({len(id_list):,} Books)'
        elif filter_mode == 'matched':
            id_list = amz_data.get('amazon_matched_ids', [])
            feed_title = f'Matched to Local NAS Books ({len(id_list):,} Books)'
        else:
            id_list = amz_data.get('all_ids', [])
            feed_title = f'All Amazon, Kindle+ & Audible ({len(id_list):,} Titles)'

        slice_ids = id_list[offset: offset + page_size + 1]
        has_next = len(slice_ids) > page_size
        page_ids = slice_ids[:page_size]

        page_items: List[CatalogItem] = []
        if page_ids:
            order_expr = Case(
                *[When(id=pk, then=pos) for pos, pk in enumerate(page_ids)],
                default=len(page_ids),
                output_field=IntegerField(),
            )
            page_items = list(
                CatalogItem.objects.filter(id__in=page_ids)
                .prefetch_related('files')
                .annotate(_amz_order=order_expr)
                .order_by('_amz_order')
            )

        return _render_acquisition_feed(
            request,
            feed_id=f'urn:mylibrary:opds:amazon:{filter_mode}',
            title=feed_title,
            items=page_items,
            page=page,
            has_next=has_next,
            total_count=len(id_list),
        )


class OPDSInterestingFeedView(View):
    """
    Dynamic Virtual Bookshelf of all books marked as Interesting (`/mylibrary/opds/interesting/`).
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        from django.db.models import Case, IntegerField, When

        try:
            page = max(int(request.GET.get('page', 1)), 1)
        except ValueError:
            page = 1
        page_size = 25
        offset = (page - 1) * page_size

        id_list = get_interesting_item_ids()
        slice_ids = id_list[offset: offset + page_size + 1]
        has_next = len(slice_ids) > page_size
        page_ids = slice_ids[:page_size]

        page_items: List[CatalogItem] = []
        if page_ids:
            order_expr = Case(
                *[When(id=pk, then=pos) for pos, pk in enumerate(page_ids)],
                default=len(page_ids),
                output_field=IntegerField(),
            )
            page_items = list(
                CatalogItem.objects.filter(id__in=page_ids)
                .prefetch_related('files')
                .annotate(_int_order=order_expr)
                .order_by('_int_order')
            )

        return _render_acquisition_feed(
            request,
            feed_id='urn:mylibrary:opds:interesting',
            title=f'Interesting Books ({len(id_list):,} Books)',
            items=page_items,
            page=page,
            has_next=has_next,
            total_count=len(id_list),
        )


class OPDSBadMetadataFeedView(View):
    """
    Dynamic Virtual Bookshelf of all books flagged as Bad Metadata (`/mylibrary/opds/bad-metadata/`).
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        from django.db.models import Case, IntegerField, When

        try:
            page = max(int(request.GET.get('page', 1)), 1)
        except ValueError:
            page = 1
        page_size = 25
        offset = (page - 1) * page_size

        id_list = get_bad_metadata_item_ids()
        slice_ids = id_list[offset: offset + page_size + 1]
        has_next = len(slice_ids) > page_size
        page_ids = slice_ids[:page_size]

        page_items: List[CatalogItem] = []
        if page_ids:
            order_expr = Case(
                *[When(id=pk, then=pos) for pos, pk in enumerate(page_ids)],
                default=len(page_ids),
                output_field=IntegerField(),
            )
            page_items = list(
                CatalogItem.objects.filter(id__in=page_ids)
                .prefetch_related('files')
                .annotate(_bad_order=order_expr)
                .order_by('_bad_order')
            )

        return _render_acquisition_feed(
            request,
            feed_id='urn:mylibrary:opds:bad_metadata',
            title=f'Bad Metadata Books ({len(id_list):,} Books)',
            items=page_items,
            page=page,
            has_next=has_next,
            total_count=len(id_list),
        )


class OPDSFlagBadMetadataView(View):
    """
    One-tap endpoint (`/mylibrary/opds/flag-bad-meta/<uuid:item_id>/`) that flags a book
    into the Bad Metadata shelf from Moon+ Reader (or unflags if `?undo=1`).
    """

    def get(self, request: HttpRequest, item_id: Any) -> HttpResponse:
        undo = (request.GET.get('undo') or '').strip().lower() in ('1', 'true', 'yes')
        item = CatalogItem.objects.prefetch_related('files').filter(id=item_id).first()
        res = set_book_bad_metadata(
            item_id,
            is_bad=(not undo),
            reason='Flagged from Moon+ Reader OPDS link',
        )
        title_str = item.title if item else str(item_id)
        authors_str = item.authors if item else ''
        accept = (request.META.get('HTTP_ACCEPT') or '').lower()
        if 'application/atom+xml' in accept and 'text/html' not in accept:
            return _render_acquisition_feed(
                request,
                feed_id=f'urn:mylibrary:opds:flag_bad_meta:{item_id}',
                title=f'{"Unflagged" if undo else "Flagged as Bad Metadata"}: {title_str}',
                items=[item] if item else [],
                page=1,
                has_next=False,
                total_count=1 if item else 0,
            )

        base = _opds_base_url(request)
        toggle_href = f"{base}/flag-bad-meta/{item_id}/" + ("" if undo else "?undo=1")
        toggle_lbl = "🚩 Re-Flag as Bad Metadata" if undo else "↩️ Undo (Remove from Bad Metadata)"
        status_title = "✅ Removed from Bad Metadata Shelf" if undo else "🚩 Added to Bad Metadata Shelf!"
        status_color = "#4ade80" if undo else "#fb7185"
        html_doc = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(status_title)}</title>
<style>
  body {{ background:#0f172a; color:#f8fafc; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; display:flex; align-items:center; justify-content:center; min-height:90vh; margin:0; padding:1rem; }}
  .card {{ max-width:440px; width:100%; background:#1e293b; border:1px solid rgba(255,255,255,0.14); border-radius:14px; padding:1.5rem; text-align:center; box-shadow:0 16px 40px rgba(0,0,0,0.45); }}
  h2 {{ color:{status_color}; margin:0 0 0.6rem 0; font-size:1.2rem; }}
  .book {{ font-weight:700; font-size:1.05rem; margin-bottom:0.25rem; }}
  .auth {{ color:#94a3b8; font-size:0.9rem; margin-bottom:1.1rem; }}
  .meta {{ color:#cbd5e1; font-size:0.85rem; margin-bottom:1.25rem; }}
  .btn {{ display:inline-block; padding:0.6rem 1.1rem; border-radius:8px; text-decoration:none; font-weight:700; font-size:0.88rem; margin:0.25rem; }}
  .btn-undo {{ background:rgba(148,163,184,0.18); border:1px solid rgba(148,163,184,0.4); color:#e2e8f0; }}
  .btn-cat {{ background:rgba(244,63,94,0.2); border:1px solid rgba(244,63,94,0.55); color:#fda4af; }}
</style>
</head>
<body>
  <div class="card">
    <h2>{html.escape(status_title)}</h2>
    <div class="book">{html.escape(title_str)}</div>
    <div class="auth">{html.escape(authors_str)}</div>
    <div class="meta">Bad Metadata Shelf Count: <strong>{res.get("bad_metadata_count", 0)}</strong></div>
    <div>
      <a class="btn btn-undo" href="{html.escape(toggle_href)}">{html.escape(toggle_lbl)}</a>
      <a class="btn btn-cat" href="/mylibrary/?shelf=bad_metadata">Open Bad Metadata Shelf</a>
    </div>
    <p style="color:#64748b; font-size:0.78rem; margin-top:1.1rem;">You can now press Back to return to Moon+ Reader.</p>
  </div>
</body>
</html>"""
        return HttpResponse(html_doc, content_type='text/html; charset=utf-8')


class OPDSBookmarkedFeedView(View):
    """
    Dynamic Virtual Bookshelf of all books currently bookmarked or in-progress
    in Moon+ Reader WebDAV Cache (`/mylibrary/opds/bookmarked/`).
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        try:
            page = max(int(request.GET.get('page', 1)), 1)
        except ValueError:
            page = 1
        page_size = 25
        offset = (page - 1) * page_size

        filter_mode = (request.GET.get('filter') or 'all').strip().lower()
        bm_data = get_dynamic_bookmarked_shelf()
        all_pairs = bm_data.get('items_with_meta', [])

        if filter_mode == 'annotations':
            filtered_items = [it for it, meta in all_pairs if meta.get('has_an')]
        elif filter_mode == 'read':
            filtered_items = [it for it, meta in all_pairs if float(meta.get('pct_val', 0.0)) >= 99.0]
        elif filter_mode == 'unread':
            filtered_items = [it for it, meta in all_pairs if float(meta.get('pct_val', 0.0)) < 99.0]
        elif filter_mode == 'reading':
            filtered_items = [it for it, meta in all_pairs if 0.0 < float(meta.get('pct_val', 0.0)) < 99.0]
        else:
            filtered_items = [it for it, _ in all_pairs]

        slice_items = filtered_items[offset: offset + page_size + 1]
        has_next = len(slice_items) > page_size
        page_items = slice_items[:page_size]

        return _render_acquisition_feed(
            request,
            feed_id='urn:mylibrary:opds:bookmarked',
            title=f'Bookmarked & Reading in Moon+ ({len(filtered_items):,} Books)',
            items=page_items,
            page=page,
            has_next=has_next,
            total_count=len(filtered_items),
        )


class OPDSTagsView(View):
    """
    Dynamic Virtual Bookshelves for Tags / Subjects and Library Categories on
    enriched books (`/mylibrary/opds/tags/`).
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        base = _opds_base_url(request)
        tag = (request.GET.get('tag') or '').strip()
        folder = (request.GET.get('folder') or '').strip()
        tags_index = get_enriched_tags_and_folders_index()

        if tag or folder:
            try:
                page = max(int(request.GET.get('page', 1)), 1)
            except ValueError:
                page = 1
            page_size = 25
            offset = (page - 1) * page_size

            if tag:
                target_paths_all = tags_index['tag_to_paths'].get(tag, [])
                # Case-insensitive fallback lookup
                if not target_paths_all:
                    for k, v in tags_index['tag_to_paths'].items():
                        if k.lower() == tag.lower():
                            target_paths_all = v
                            tag = k
                            break
                shelf_title = f'Enriched Tag: {tag} ({len(target_paths_all):,} Books)'
                feed_id = f'urn:mylibrary:opds:tag:{tag}'
            else:
                target_paths_all = tags_index['folder_to_paths'].get(folder, [])
                shelf_title = f'Enriched Category: {folder} ({len(target_paths_all):,} Books)'
                feed_id = f'urn:mylibrary:opds:folder:{folder}'

            slice_paths = target_paths_all[offset: offset + page_size + 1]
            has_next = len(slice_paths) > page_size
            page_paths = slice_paths[:page_size]

            locs = (
                FileLocation.objects.filter(file_path__in=page_paths)
                .select_related('catalog_item')
                .prefetch_related('catalog_item__files')
            )
            by_path = {loc.file_path: loc.catalog_item for loc in locs}
            seen_ids = set()
            items = []
            for p in page_paths:
                ci = by_path.get(p)
                if ci and ci.id not in seen_ids:
                    seen_ids.add(ci.id)
                    items.append(ci)

            return _render_acquisition_feed(
                request,
                feed_id=feed_id,
                title=shelf_title,
                items=items,
                page=page,
                has_next=has_next,
                total_count=len(target_paths_all),
            )

        entries = []
        for fld_name, cnt in tags_index.get('folder_counts', []):
            entries.append({
                'id': f'urn:mylibrary:opds:folder:{fld_name}',
                'title': f'[Collection] {fld_name} ({cnt:,} Enriched Books)',
                'content': f'Enriched books in {fld_name}',
                'href': f'{base}/tags/?folder={urllib.parse.quote(fld_name)}',
                'kind': 'acquisition',
            })

        for tag_name, cnt in tags_index.get('tag_counts', [])[:250]:
            entries.append({
                'id': f'urn:mylibrary:opds:tag:{tag_name}',
                'title': f'{tag_name} ({cnt:,} Books)',
                'content': f'Enriched books tagged with "{tag_name}"',
                'href': f'{base}/tags/?tag={urllib.parse.quote(tag_name)}',
                'kind': 'acquisition',
            })

        return _render_navigation_feed(
            request,
            feed_id='urn:mylibrary:opds:tags',
            title='Enriched Bookshelves by Tag & Subject',
            entries=entries,
        )


class OPDSOpenSearchView(View):
    """
    OpenSearch XML descriptor (`/mylibrary/opds/opensearch.xml`).
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        base = _opds_base_url(request)
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">
  <ShortName>MyLibrary</ShortName>
  <Description>Search MyLibrary (supports keywords: author:, title:, pub:, cat:, tag:, format:epub, year:2025, is:enriched, is:reading, +enrich, +badmeta)</Description>
  <InputEncoding>UTF-8</InputEncoding>
  <OutputEncoding>UTF-8</OutputEncoding>
  <Url type="application/atom+xml" template="{base}/search/?q={{searchTerms}}"/>
  <Url type="application/atom+xml;profile=opds-catalog;kind=acquisition" template="{base}/search/?q={{searchTerms}}"/>
</OpenSearchDescription>"""
        return HttpResponse(xml, content_type='application/opensearchdescription+xml; charset=utf-8')


class OPDSSearchView(View):
    """
    OPDS Search & Acquisition Feed (`/mylibrary/opds/search/?q=...`).
    Searches across all books in the server with full support for search keyword operators:
      - `title:...`, `author:...`, `pub:...`, `isbn:...`, `cat:...`, `tag:...`, `file:...`
      - `format:epub|pdf|mobi`, `type:book|magazine|kindle|audible`, `year:2025` (`year:2020..2026`, `after:2023`, `before:2000`)
      - `is:enriched`, `is:unenriched`, `is:review`, `is:reading`, `is:unread`, `is:read`, `is:bookmarked`, `is:interesting`, `is:badmeta`, `is:amazon`
      - `has:cover`, `missing:cover`, `has:isbn`, `missing:isbn`, `has:desc`, `missing:desc`, `-exclude`, `sort:newest|oldest|added`
      - `+enrich` (on-demand enrich top results) & `+badmeta` (flag top results as Bad Metadata)
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        base = _opds_base_url(request)
        raw_q = (request.GET.get('q') or request.GET.get('query') or request.GET.get('search') or '').strip()
        if not raw_q:
            preset_entries = [
                {
                    'id': 'urn:mylibrary:opds:search:hint:fields',
                    'title': '💡 Field Keywords: author:, title:, pub:, isbn:, cat:, tag:, file:',
                    'content': 'Examples: author:"Brandon Sanderson" | title:Dune | pub:O\'Reilly | cat:"Science Fiction" | tag:History',
                    'href': f'{base}/search/?q=' + urllib.parse.quote('is:enriched sort:newest'),
                    'kind': 'acquisition',
                },
                {
                    'id': 'urn:mylibrary:opds:search:hint:status',
                    'title': '💡 Status & Shelf Keywords: is:enriched, is:unenriched, is:reading, is:interesting, is:badmeta',
                    'content': 'Combine with any search: e.g. "is:enriched python", "is:unenriched format:epub", "is:reading", "missing:cover"',
                    'href': f'{base}/search/?q=' + urllib.parse.quote('is:enriched format:epub sort:newest'),
                    'kind': 'acquisition',
                },
                {
                    'id': 'urn:mylibrary:opds:search:hint:year_sort',
                    'title': '💡 Year, Format, Exclude & Sort: year:2025, year:2022..2026, -term, sort:newest, +enrich, +badmeta',
                    'content': 'Examples: "year:2025..2026 format:epub", "python -beginner sort:newest", "Helidon +enrich", "Book Title +badmeta"',
                    'href': f'{base}/search/?q=' + urllib.parse.quote('year:2025..2026 format:epub sort:newest'),
                    'kind': 'acquisition',
                },
            ]
            return _render_navigation_feed(
                request,
                feed_id='urn:mylibrary:opds:search:keywords',
                title='Search Keywords & Operators Reference',
                entries=preset_entries,
            )
        enrich_on_search = (request.GET.get('enrich') or '').strip().lower() in ('1', 'true', 'yes')
        flag_bad_on_search = (request.GET.get('bad_meta') or '').strip().lower() in ('1', 'true', 'yes')
        q = raw_q
        if re.search(r'(?:^|\s)[+!]enrich\b', q, flags=re.IGNORECASE):
            enrich_on_search = True
            q = re.sub(r'(?:^|\s)[+!]enrich\b', '', q, flags=re.IGNORECASE).strip()
        if re.search(r'(?:^|\s)[+!](?:bad|badmeta|badmetadata|fixmeta)\b', q, flags=re.IGNORECASE):
            flag_bad_on_search = True
            q = re.sub(r'(?:^|\s)[+!](?:bad|badmeta|badmetadata|fixmeta)\b', '', q, flags=re.IGNORECASE).strip()

        try:
            page = max(int(request.GET.get('page', 1)), 1)
        except ValueError:
            page = 1
        page_size = 25
        offset = (page - 1) * page_size

        qs = perform_search(q)
        rows = list(qs[offset: offset + page_size + 1])
        has_next = len(rows) > page_size
        items = rows[:page_size]

        if flag_bad_on_search and items:
            for it in items[:3]:
                try:
                    set_book_bad_metadata(it.id, True, reason=f'Flagged via Moon+ Reader search: {raw_q}')
                except Exception:
                    pass

        if enrich_on_search and items:
            enriched_now = 0
            for it in items:
                if enriched_now >= 3:
                    break
                f_list = list(it.files.all())
                has_enr = any(is_file_enriched(f.file_path) for f in f_list if f.file_path)
                can_enr = any((f.file_format or '').upper().lstrip('.') in ('EPUB', 'PDF') for f in f_list)
                if can_enr and not has_enr:
                    try:
                        res = enrich_catalog_item_on_demand(it, force=False)
                        if res.get('ok'):
                            enriched_now += 1
                    except Exception:
                        pass

        feed_title = f'Search Results: "{q}"' if q else 'All Books Search Results'
        if flag_bad_on_search:
            feed_title += ' (🚩 Flagged as Bad Metadata)'
        if enrich_on_search:
            feed_title += ' (On-Demand Enriched)'

        return _render_acquisition_feed(
            request,
            feed_id=f'urn:mylibrary:opds:search:{q}',
            title=feed_title,
            items=items,
            page=page,
            has_next=has_next,
        )


class OPDSEnrichedFeedView(View):
    """
    OPDS Feed of books that have been enriched by `epubmeta` (`/mylibrary/opds/enriched/`).
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        try:
            page = max(int(request.GET.get('page', 1)), 1)
        except ValueError:
            page = 1
        page_size = 25
        offset = (page - 1) * page_size

        enriched_paths = _get_enriched_paths_list(limit=2500)
        slice_paths = enriched_paths[offset: offset + page_size + 1]
        has_next = len(slice_paths) > page_size
        target_paths = slice_paths[:page_size]

        locs = (
            FileLocation.objects.filter(file_path__in=target_paths)
            .select_related('catalog_item')
            .prefetch_related('catalog_item__files')
        )
        by_path = {loc.file_path: loc.catalog_item for loc in locs}
        seen_ids = set()
        items = []
        for p in target_paths:
            ci = by_path.get(p)
            if ci and ci.id not in seen_ids:
                seen_ids.add(ci.id)
                items.append(ci)

        enr_total = len(_ENRICHED_PATHS_CACHE.get('enriched_item_ids') or []) or len(enriched_paths)
        return _render_acquisition_feed(
            request,
            feed_id='urn:mylibrary:opds:enriched',
            title=f'Enriched EPUBs — epubmeta Verified ({enr_total:,} Enriched Books)',
            items=items,
            page=page,
            has_next=has_next,
            total_count=enr_total,
        )


class OPDSRecentFeedView(View):
    """
    Recently Updated Books (`/mylibrary/opds/recent/`).
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        try:
            page = max(int(request.GET.get('page', 1)), 1)
        except ValueError:
            page = 1
        page_size = 25
        offset = (page - 1) * page_size

        qs = CatalogItem.objects.prefetch_related('files').order_by('-updated_at')
        rows = list(qs[offset: offset + page_size + 1])
        has_next = len(rows) > page_size
        items = rows[:page_size]

        return _render_acquisition_feed(
            request,
            feed_id='urn:mylibrary:opds:recent',
            title='Recently Updated Books',
            items=items,
            page=page,
            has_next=has_next,
        )


class OPDSAllFeedView(View):
    """
    All Catalog Books A-Z (`/mylibrary/opds/all/`).
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        try:
            page = max(int(request.GET.get('page', 1)), 1)
        except ValueError:
            page = 1
        page_size = 25
        offset = (page - 1) * page_size

        qs = CatalogItem.objects.prefetch_related('files').order_by('title')
        rows = list(qs[offset: offset + page_size + 1])
        has_next = len(rows) > page_size
        items = rows[:page_size]

        from .views import _get_cached_dashboard_meta
        stats, _ = _get_cached_dashboard_meta()
        cat_books = stats.get('books_count') or stats.get('total_items') or 0
        tot_files = stats.get('total_files') or 0
        enr_total = len(_ENRICHED_PATHS_CACHE.get('enriched_item_ids') or [])
        all_title = (
            f'All Catalog Books ({cat_books:,} Catalog Books · {tot_files:,} Total Books'
            + (f' · {enr_total:,} Enriched)' if enr_total else ')')
            if cat_books
            else 'All Books (A-Z)'
        )
        return _render_acquisition_feed(
            request,
            feed_id='urn:mylibrary:opds:all',
            title=all_title,
            items=items,
            page=page,
            has_next=has_next,
            total_count=stats.get('total_items') or None,
        )


class OPDSAuthorsIndexView(View):
    """
    A-Z Author Initial Navigation (`/mylibrary/opds/authors/`).
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        base = _opds_base_url(request)
        letter = (request.GET.get('letter') or '').strip().upper()
        author_exact = (request.GET.get('author') or '').strip()

        if author_exact:
            try:
                page = max(int(request.GET.get('page', 1)), 1)
            except ValueError:
                page = 1
            page_size = 25
            offset = (page - 1) * page_size
            qs = CatalogItem.objects.filter(authors__icontains=author_exact).prefetch_related('files').order_by('title')
            rows = list(qs[offset: offset + page_size + 1])
            return _render_acquisition_feed(
                request,
                feed_id=f'urn:mylibrary:opds:author:{author_exact}',
                title=f'Books by {author_exact}',
                items=rows[:page_size],
                page=page,
                has_next=len(rows) > page_size,
            )

        if letter:
            authors = (
                CatalogItem.objects.filter(authors__istartswith=letter)
                .exclude(authors='Unknown Author')
                .values_list('authors', flat=True)
                .distinct()
                .order_by('authors')[:100]
            )
            entries = [
                {
                    'id': f'urn:mylibrary:opds:author:{a}',
                    'title': a,
                    'content': f'Browse books by {a}',
                    'href': f'{base}/authors/?author={urllib.parse.quote(a)}',
                    'kind': 'acquisition',
                }
                for a in authors if a
            ]
            return _render_navigation_feed(
                request,
                feed_id=f'urn:mylibrary:opds:authors:{letter}',
                title=f'Authors Starting with "{letter}"',
                entries=entries,
            )

        entries = [
            {
                'id': f'urn:mylibrary:opds:authors:{ch}',
                'title': f'Authors — {ch}',
                'content': f'Authors starting with {ch}',
                'href': f'{base}/authors/?letter={ch}',
                'kind': 'navigation',
            }
            for ch in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
        ]
        return _render_navigation_feed(
            request,
            feed_id='urn:mylibrary:opds:authors',
            title='Browse by Author (A-Z)',
            entries=entries,
        )


class OPDSFormatsView(View):
    """
    Browse by Digital Format (`/mylibrary/opds/formats/`).
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        base = _opds_base_url(request)
        fmt = (request.GET.get('format') or '').strip().upper()
        if fmt:
            try:
                page = max(int(request.GET.get('page', 1)), 1)
            except ValueError:
                page = 1
            page_size = 25
            offset = (page - 1) * page_size
            qs = (
                CatalogItem.objects.filter(files__file_format__iexact=fmt)
                .distinct()
                .prefetch_related('files')
                .order_by('-updated_at')
            )
            rows = list(qs[offset: offset + page_size + 1])
            return _render_acquisition_feed(
                request,
                feed_id=f'urn:mylibrary:opds:format:{fmt}',
                title=f'{fmt} Books',
                items=rows[:page_size],
                page=page,
                has_next=len(rows) > page_size,
            )

        entries = [
            {
                'id': f'urn:mylibrary:opds:format:{f_code}',
                'title': f'{f_code} Books',
                'content': f'Browse books in {f_code} format',
                'href': f'{base}/formats/?format={f_code}',
                'kind': 'acquisition',
            }
            for f_code in ('EPUB', 'PDF', 'MOBI', 'AZW3', 'MP3', 'M4B')
        ]
        return _render_navigation_feed(
            request,
            feed_id='urn:mylibrary:opds:formats',
            title='Browse by File Format',
            entries=entries,
        )


class OPDSDownloadFileView(View):
    """
    Streams a physical book file (`/mylibrary/opds/download/<file_id>/<filename>`)
    with a Calibre-normalized filename (`<Title> - <Author>.epub`).
    - Automatically performs on-demand `epubmeta` enrichment if the `.epub` / `.pdf` file
      is not already enriched in `scanner_state.db` (or if `?enrich=1` is passed).
    - Automatically migrates any matching Moon+ Reader `.po` and `.an` bookmarks in
      `/webdav/Apps/Books/.Moon+/Cache`!
    """

    def get(self, request: HttpRequest, file_id: str, filename: str = '') -> HttpResponse:
        try:
            file_loc = FileLocation.objects.select_related('catalog_item').get(id=file_id)
        except Exception:
            item = CatalogItem.objects.prefetch_related('files').filter(id=file_id).first()
            if not item or not item.files.exists():
                raise Http404('Book file not found.')
            file_loc = item.files.filter(file_format__iexact='EPUB').first() or item.files.first()

        phys_path = _resolve_physical_file_path(file_loc.file_path)
        if not phys_path or not phys_path.exists():
            raise Http404(f'Physical file is not mounted or accessible inside container: {file_loc.file_path}')

        item = file_loc.catalog_item
        fmt_upper = (file_loc.file_format or 'EPUB').upper().lstrip('.')

        # Automatic on-demand epubmeta enrichment on download if not already enriched (unless ?enrich=0)
        enrich_param = (request.GET.get('enrich') or '').strip().lower()
        skip_enrich = enrich_param in ('0', 'false', 'no', 'skip')
        force_enrich = enrich_param in ('1', 'true', 'yes', 'force')
        if not skip_enrich and fmt_upper in ('EPUB', 'PDF') and (force_enrich or not is_file_enriched(file_loc.file_path)):
            try:
                enrich_res = enrich_file_location_on_demand(file_loc, force=force_enrich)
                if enrich_res.get('ok'):
                    item.refresh_from_db()
                    file_loc.refresh_from_db()
                    phys_path = _resolve_physical_file_path(file_loc.file_path) or phys_path
            except Exception:
                pass

        mime = MIME_MAP.get(fmt_upper, 'application/octet-stream')
        calibre_filename = _calibre_normalize_filename(item.title, item.authors, fmt_upper.lower())

        # Automatically migrate Moon+ Reader WebDAV reading position (.po) & bookmarks (.an)
        if fmt_upper == 'EPUB':
            _auto_migrate_moon_bookmarks_for_download(item, file_loc, calibre_filename, epub_path=phys_path)

        # Reset the Interesting flag automatically when the book is downloaded
        if item:
            try:
                clear_book_interesting(item.id)
            except Exception:
                pass

        resp = FileResponse(open(phys_path, 'rb'), content_type=mime)
        encoded_name = urllib.parse.quote(calibre_filename)
        resp['Content-Disposition'] = f'attachment; filename="{calibre_filename}"; filename*=UTF-8\'\'{encoded_name}'
        return resp


class OPDSCoverView(View):
    """
    Serves book cover artwork (`/mylibrary/opds/cover/<item_id>.jpg`):
      1. Base64 `cover_thumbnail` stored on `CatalogItem`
      2. Embedded cover extracted directly from the `.epub` archive
      3. Redirect to external `cover_image_url`
      4. Lightweight SVG fallback placeholder
    """

    def get(self, request: HttpRequest, item_id: str) -> HttpResponse:
        item = CatalogItem.objects.prefetch_related('files').filter(id=item_id).first()
        if not item:
            raise Http404('Catalog item not found.')

        if item.cover_thumbnail and item.cover_thumbnail.startswith('data:image'):
            try:
                header, b64_data = item.cover_thumbnail.split(',', 1)
                mime = header.split(';')[0].replace('data:', '') or 'image/jpeg'
                return HttpResponse(base64.b64decode(b64_data), content_type=mime)
            except Exception:
                pass

        epub_loc = item.files.filter(file_format__iexact='EPUB').first()
        if epub_loc:
            phys = _resolve_physical_file_path(epub_loc.file_path)
            if phys and phys.exists():
                try:
                    with zipfile.ZipFile(phys, 'r') as zf:
                        for name in zf.namelist():
                            low = name.lower()
                            if 'cover' in low and low.endswith(('.jpg', '.jpeg', '.png')):
                                data = zf.read(name)
                                mime = 'image/png' if low.endswith('.png') else 'image/jpeg'
                                return HttpResponse(data, content_type=mime)
                except Exception:
                    pass

        if item.cover_image_url:
            return HttpResponseRedirect(item.cover_image_url)

        title_esc = html.escape((item.title or 'Book')[:40])
        author_esc = html.escape((item.authors or '')[:35])
        svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="300" height="450" viewBox="0 0 300 450">
  <rect width="300" height="450" fill="#1e293b"/>
  <rect x="16" y="16" width="268" height="418" rx="8" fill="none" stroke="#38bdf8" stroke-width="2" stroke-opacity="0.4"/>
  <text x="150" y="190" fill="#f8fafc" font-family="sans-serif" font-size="18" font-weight="bold" text-anchor="middle">{title_esc}</text>
  <text x="150" y="235" fill="#94a3b8" font-family="sans-serif" font-size="14" text-anchor="middle">{author_esc}</text>
</svg>"""
        return HttpResponse(svg, content_type='image/svg+xml')


class CalibreAjaxBooksView(View):
    """
    Emulates Calibre Content Server `/ajax/search` and `/ajax/book/<id>` JSON endpoints
    for Calibre Sync / Calibre Companion clients.
    """

    def _serialize_calibre_book(self, request: HttpRequest, item: CatalogItem) -> Dict[str, Any]:
        base = _opds_base_url(request)
        formats = []
        format_Metadata = {}
        main_format_urls = {}
        for f in item.files.all():
            fmt = (f.file_format or 'EPUB').lower().lstrip('.')
            formats.append(fmt)
            dl_name = _calibre_normalize_filename(item.title, item.authors, fmt)
            dl_url = f"{base}/download/{f.id}/{urllib.parse.quote(dl_name)}"
            main_format_urls[fmt] = dl_url
            format_Metadata[fmt] = {
                'path': f.file_path,
                'size': f.file_size or 0,
                'mtime': (f.file_modified_at or item.updated_at or timezone.now()).isoformat(),
            }

        return {
            'application_id': str(item.id),
            'uuid': str(item.id),
            'title': item.title,
            'authors': [a.strip() for a in (item.authors or 'Unknown Author').split(',') if a.strip()],
            'author_sort': item.authors or 'Unknown Author',
            'publisher': item.publisher or '',
            'pubdate': item.pub_date.isoformat() if item.pub_date else None,
            'comments': item.description or '',
            'isbn': item.isbn or '',
            'identifiers': {'isbn': item.isbn} if item.isbn else {},
            'cover': f"{base}/cover/{item.id}.jpg",
            'thumbnail': f"{base}/cover/{item.id}.jpg",
            'formats': formats,
            'main_format': main_format_urls,
            'format_metadata': format_Metadata,
            'timestamp': item.created_at.isoformat() if item.created_at else None,
            'last_modified': item.updated_at.isoformat() if item.updated_at else None,
        }

    def get(self, request: HttpRequest, item_id: Optional[str] = None) -> JsonResponse:
        if item_id:
            item = CatalogItem.objects.prefetch_related('files').filter(id=item_id).first()
            if not item:
                raise Http404('Book not found.')
            return JsonResponse(self._serialize_calibre_book(request, item))

        q = (request.GET.get('query') or request.GET.get('q') or '').strip()
        try:
            num = min(max(int(request.GET.get('num', 50)), 1), 200)
            offset = max(int(request.GET.get('offset', 0)), 0)
        except ValueError:
            num, offset = 50, 0

        qs = perform_search(q)
        items = list(qs[offset: offset + num])
        book_ids = [str(it.id) for it in items]
        books_map = {str(it.id): self._serialize_calibre_book(request, it) for it in items}

        return JsonResponse({
            'total_num': len(book_ids),
            'offset': offset,
            'num': len(book_ids),
            'book_ids': book_ids,
            'books': books_map,
        })


def extract_book_preview_data(
    file_path: Optional[str] = None,
    file_id: Optional[str] = None,
    item_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Extract an inline browser preview for a physical `.epub` or `.pdf` book on the NAS:
      - `images`: Up to 6 rendered page images (for PDF) or cover/front-matter images (for EPUB) as `data:image/...` URIs.
      - `pages`: Up to 10 front-matter / chapter text sections with clean line breaks.
      - `detected_isbns`: Any ISBN-13 / ISBN-10 numbers found inside the book's front/back matter.
      - `raw_stream_url`: Direct URL to open/stream the raw PDF/EPUB in a new browser tab.
    """
    import base64
    import urllib.parse

    resolved_loc: Optional[FileLocation] = None
    raw_path = (file_path or '').strip()

    if file_id:
        resolved_loc = FileLocation.objects.select_related('catalog_item').filter(id=file_id).first()
        if resolved_loc and not raw_path:
            raw_path = resolved_loc.file_path or ''
    if not raw_path and item_id:
        item = CatalogItem.objects.prefetch_related('files').filter(id=item_id).first()
        if item:
            files = list(item.files.all())
            for ext_pref in ('.epub', '.pdf'):
                cand = next((f for f in files if (f.file_path or '').lower().endswith(ext_pref)), None)
                if cand:
                    resolved_loc = cand
                    raw_path = cand.file_path or ''
                    break
            if not raw_path and files:
                resolved_loc = files[0]
                raw_path = files[0].file_path or ''

    phys_path = _resolve_physical_file_path(raw_path) if raw_path else None
    if not phys_path or not phys_path.exists():
        return {
            'ok': False,
            'error': f'Physical book file not found on NAS: {raw_path or file_id or item_id}',
        }

    ext = phys_path.suffix.lower()
    images: List[Dict[str, str]] = []
    pages: List[Dict[str, str]] = []
    detected_isbns: List[str] = []
    total_pages = 0

    def _collect_isbns(text_blob: str) -> None:
        if not text_blob:
            return
        try:
            import sys
            base_dir = Path(getattr(settings, 'BASE_DIR', '.'))
            for sub in ('scanner', 'nas_scanner'):
                cand_dir = str((base_dir / sub).resolve())
                if os.path.isdir(cand_dir) and cand_dir not in sys.path:
                    sys.path.insert(0, cand_dir)
            from epubmeta.isbn.extractor import extract_isbns_from_text
            for isbn_val, _ in extract_isbns_from_text(text_blob):
                if isbn_val and isbn_val not in detected_isbns:
                    detected_isbns.append(isbn_val)
        except Exception:
            for m in re.finditer(r'\b(97[89][-\s]?(?:\d[-\s]?){9}\d)\b', text_blob):
                clean = re.sub(r'[^0-9]', '', m.group(1))
                if len(clean) == 13 and clean not in detected_isbns:
                    detected_isbns.append(clean)

    def _html_to_readable_text(html_bytes: bytes) -> str:
        try:
            from lxml import etree
            parser = etree.HTMLParser(encoding='utf-8')
            tree = etree.fromstring(html_bytes, parser)
            for el in tree.xpath('//script|//style'):
                if el.getparent() is not None:
                    el.getparent().remove(el)
            block_tags = {'p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'tr', 'br', 'blockquote'}
            lines: List[str] = []
            cur_line: List[str] = []
            for elem in tree.iter():
                tag_local = elem.tag.split('}')[-1].lower() if isinstance(elem.tag, str) else ''
                if tag_local in block_tags and cur_line:
                    joined = re.sub(r'[ \t]+', ' ', ''.join(cur_line)).strip()
                    if joined:
                        lines.append(joined)
                    cur_line = []
                if elem.text:
                    cur_line.append(elem.text)
                if elem.tail:
                    cur_line.append(elem.tail)
            if cur_line:
                joined = re.sub(r'[ \t]+', ' ', ''.join(cur_line)).strip()
                if joined:
                    lines.append(joined)
            return '\n\n'.join(lines)
        except Exception:
            raw = html_bytes.decode('utf-8', errors='ignore')
            raw = re.sub(r'</?(?:p|div|h[1-6]|li|br|tr)[^>]*>', '\n', raw, flags=re.IGNORECASE)
            raw = re.sub(r'<[^>]+>', ' ', raw)
            lines = [re.sub(r'[ \t]+', ' ', ln).strip() for ln in raw.splitlines()]
            return '\n\n'.join(ln for ln in lines if ln)

    def _read_zip_member_safe(z: zipfile.ZipFile, member_name: str) -> bytes:
        try:
            return z.read(member_name)
        except Exception:
            try:
                with z.open(member_name, 'r') as zf:
                    zf._expected_crc = None
                    return zf.read()
            except Exception:
                return b''

    if ext == '.pdf':
        try:
            import pymupdf
            doc = pymupdf.open(phys_path)
            try:
                total_pages = len(doc)
                img_count = min(6, total_pages)
                for i in range(img_count):
                    pix = doc[i].get_pixmap(dpi=120)
                    b64 = base64.b64encode(pix.tobytes('png')).decode('ascii')
                    images.append({
                        'label': f'Page {i + 1} of {total_pages}',
                        'data_uri': f'data:image/png;base64,{b64}',
                    })
                txt_count = min(10, total_pages)
                for i in range(txt_count):
                    txt = (doc[i].get_text() or '').strip()
                    if txt:
                        pages.append({
                            'label': f'Page {i + 1}',
                            'text': txt[:6000],
                        })
                        _collect_isbns(txt)
                if total_pages > 10:
                    for i in range(max(10, total_pages - 2), total_pages):
                        txt = (doc[i].get_text() or '').strip()
                        if txt:
                            pages.append({
                                'label': f'Page {i + 1} (Back Matter)',
                                'text': txt[:6000],
                            })
                            _collect_isbns(txt)
            finally:
                doc.close()
        except Exception as pdf_err:
            return {'ok': False, 'error': f'Error reading PDF for preview: {pdf_err}'}

    elif ext == '.epub':
        try:
            from lxml import etree

            with zipfile.ZipFile(phys_path, 'r') as z:
                namelist = set(z.namelist())
                opf_path = None
                if 'META-INF/container.xml' in namelist:
                    try:
                        c_tree = etree.fromstring(_read_zip_member_safe(z, 'META-INF/container.xml'))
                        rf = c_tree.find('.//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile')
                        if rf is not None and rf.get('full-path'):
                            opf_path = rf.get('full-path')
                    except Exception:
                        pass
                if not opf_path:
                    for n in z.namelist():
                        if n.lower().endswith('.opf'):
                            opf_path = n
                            break

                if opf_path and opf_path in namelist:
                    opf_dir = os.path.dirname(opf_path)
                    opf_root = etree.fromstring(_read_zip_member_safe(z, opf_path), etree.XMLParser(recover=True, encoding='utf-8'))
                    cover_meta_id = None
                    for meta_el in opf_root.findall('.//{http://www.idpf.org/2007/opf}meta'):
                        if (meta_el.get('name') or '').lower() == 'cover' and meta_el.get('content'):
                            cover_meta_id = meta_el.get('content')
                            break

                    manifest: Dict[str, Tuple[str, str]] = {}
                    primary_cover_hrefs: List[Tuple[str, str]] = []
                    fallback_cover_hrefs: List[Tuple[str, str]] = []
                    other_image_hrefs: List[Tuple[str, str]] = []
                    for item_el in opf_root.findall('.//{http://www.idpf.org/2007/opf}item'):
                        iid = item_el.get('id')
                        href = item_el.get('href')
                        mtype = (item_el.get('media-type') or '').lower()
                        props = (item_el.get('properties') or '').lower()
                        if iid and href:
                            full_href = os.path.normpath(os.path.join(opf_dir, urllib.parse.unquote(href))).replace('\\', '/')
                            manifest[iid] = (full_href, mtype)
                            if mtype.startswith('image/'):
                                is_primary_cov = (
                                    (cover_meta_id and iid == cover_meta_id)
                                    or 'cover-image' in props
                                    or iid.lower() in ('cover', 'cover-image', 'cover_img')
                                )
                                if is_primary_cov:
                                    primary_cover_hrefs.append(('Cover Image', full_href))
                                elif 'cover' in iid.lower() or 'cover' in href.lower():
                                    fallback_cover_hrefs.append((os.path.basename(full_href), full_href))
                                else:
                                    other_image_hrefs.append((os.path.basename(full_href), full_href))

                    spine_ids = [
                        ir.get('idref')
                        for ir in opf_root.findall('.//{http://www.idpf.org/2007/opf}itemref')
                        if ir.get('idref')
                    ]
                    total_pages = len(spine_ids)

                    # Prioritize the true primary cover first, then images referenced in the first 5 spine sections
                    front_referenced_imgs: List[Tuple[str, str]] = []
                    for sid in spine_ids[:5]:
                        if sid in manifest:
                            shref, smtype = manifest[sid]
                            if shref in namelist and any(t in smtype for t in ('html', 'xml')):
                                try:
                                    s_raw = _read_zip_member_safe(z, shref).decode('utf-8', errors='ignore')
                                    s_dir = os.path.dirname(shref)
                                    for m_src in re.findall(r'(?:src|href)\s*=\s*["\']([^"\']+\.(?:jpg|jpeg|png|webp|gif))["\']', s_raw, flags=re.IGNORECASE):
                                        f_img = os.path.normpath(os.path.join(s_dir, urllib.parse.unquote(m_src))).replace('\\', '/')
                                        front_referenced_imgs.append((os.path.basename(f_img), f_img))
                                except Exception:
                                    pass

                    if primary_cover_hrefs:
                        image_hrefs = primary_cover_hrefs[:1] + front_referenced_imgs + other_image_hrefs
                    elif fallback_cover_hrefs:
                        image_hrefs = [('Cover Image', fallback_cover_hrefs[0][1])] + front_referenced_imgs + fallback_cover_hrefs[1:] + other_image_hrefs
                    else:
                        image_hrefs = front_referenced_imgs + other_image_hrefs
                    seen_img_paths = set()
                    for lbl, ihref in image_hrefs:
                        if len(images) >= 6:
                            break
                        if ihref in namelist and ihref not in seen_img_paths:
                            try:
                                ibytes = _read_zip_member_safe(z, ihref)
                                if len(ibytes) >= 1500:  # skip tiny 1x1 spacer gifs
                                    seen_img_paths.add(ihref)
                                    mime = 'image/png' if ihref.lower().endswith('.png') else 'image/jpeg'
                                    b64 = base64.b64encode(ibytes).decode('ascii')
                                    images.append({
                                        'label': lbl,
                                        'data_uri': f'data:{mime};base64,{b64}',
                                    })
                            except Exception:
                                pass

                    selected_sids: List[str] = []
                    for sid in spine_ids[:10]:
                        if sid not in selected_sids:
                            selected_sids.append(sid)
                    keywords = ('copy', 'colophon', 'imprint', 'legal', 'title', 'toc', 'contents', 'about')
                    for sid in spine_ids:
                        if sid in manifest:
                            href_low = manifest[sid][0].lower()
                            if any(k in href_low or k in sid.lower() for k in keywords) and sid not in selected_sids:
                                selected_sids.append(sid)

                    for idx, sid in enumerate(selected_sids[:12]):
                        if sid not in manifest:
                            continue
                        href, mtype = manifest[sid]
                        if href in namelist and any(t in mtype for t in ('html', 'xml')):
                            try:
                                raw_html = _read_zip_member_safe(z, href)
                                readable = _html_to_readable_text(raw_html)
                                if readable and len(readable.strip()) > 3:
                                    short_name = os.path.basename(href)
                                    pages.append({
                                        'label': f'Section {idx + 1} ({short_name})',
                                        'text': readable[:6000],
                                    })
                                    _collect_isbns(readable)
                            except Exception:
                                pass
        except Exception as epub_err:
            return {'ok': False, 'error': f'Error reading EPUB for preview: {epub_err}'}

    elif ext in ('.mobi', '.azw', '.azw3', '.prc', '.pdb'):
        try:
            import struct
            import sys
            base_dir = Path(getattr(settings, 'BASE_DIR', '.'))
            for sub in ('scanner', 'nas_scanner'):
                cand_dir = str((base_dir / sub).resolve())
                if os.path.isdir(cand_dir) and cand_dir not in sys.path:
                    sys.path.insert(0, cand_dir)
            from epubmeta.readers.mobi import MobiReader, _decompress_palmdoc_lz77

            mreader = MobiReader(phys_path)
            mmeta = mreader.read_metadata()
            meta_lines: List[str] = []
            if mmeta.title:
                meta_lines.append(f"Title: {mmeta.title}")
            if mmeta.authors:
                meta_lines.append(f"Author(s): {', '.join(mmeta.authors)}")
            if mmeta.publisher:
                meta_lines.append(f"Publisher: {mmeta.publisher}")
            if mmeta.published_date:
                meta_lines.append(f"Published Date: {mmeta.published_date}")
            if mmeta.isbn13:
                meta_lines.append(f"ISBN-13: {mmeta.isbn13}")
                if mmeta.isbn13 not in detected_isbns:
                    detected_isbns.append(mmeta.isbn13)
            if mmeta.asin:
                meta_lines.append(f"ASIN: {mmeta.asin}")
            if mmeta.subjects:
                meta_lines.append(f"Subjects: {', '.join(mmeta.subjects)}")
            if mmeta.description:
                clean_desc = _html_to_readable_text(mmeta.description.encode('utf-8', errors='ignore')) or mmeta.description
                meta_lines.append(f"\nDescription / Synopsis:\n{clean_desc}")
            if meta_lines:
                pages.append({
                    'label': 'EXTH Header Info',
                    'text': '\n'.join(meta_lines)[:6000],
                })

            spans = mreader._read_palmdb_records(max_records=800)
            total_pages = max(1, len(spans) - 1) if spans else 0

            # Extract embedded cover & front images from PalmDB records
            cov_bytes = mreader.extract_cover_bytes()
            if cov_bytes and len(cov_bytes) >= 1000:
                mime = 'image/png' if cov_bytes.startswith(b'\x89PNG') else ('image/gif' if cov_bytes.startswith(b'GIF8') else 'image/jpeg')
                images.append({
                    'label': 'Embedded Cover',
                    'data_uri': f"data:{mime};base64,{base64.b64encode(cov_bytes).decode('ascii')}",
                })

            if len(spans) >= 2:
                with open(phys_path, 'rb') as fp:
                    r0_start, r0_end = spans[0]
                    fp.seek(r0_start)
                    rec0 = fp.read(min(r0_end - r0_start, 65536))
                    compression = struct.unpack('>H', rec0[0:2])[0] if len(rec0) >= 16 else 0
                    text_rec_count = struct.unpack('>H', rec0[8:10])[0] if len(rec0) >= 16 else min(len(spans) - 1, 12)
                    mobi_pos = rec0.find(b'MOBI')
                    first_img_idx = (
                        struct.unpack('>I', rec0[mobi_pos + 92 : mobi_pos + 96])[0]
                        if (mobi_pos != -1 and mobi_pos + 96 <= len(rec0))
                        else (text_rec_count + 1)
                    )

                    # Collect up to 5 additional embedded images from MOBI image records
                    if 0 < first_img_idx < len(spans):
                        for rec_i in range(first_img_idx, min(len(spans), first_img_idx + 25)):
                            if len(images) >= 6:
                                break
                            s_off, e_off = spans[rec_i]
                            sz = e_off - s_off
                            if 1500 <= sz <= 8 * 1024 * 1024:
                                fp.seek(s_off)
                                ibytes = fp.read(sz)
                                if ibytes.startswith((b'\xff\xd8\xff', b'\x89PNG', b'GIF8')):
                                    if cov_bytes and ibytes == cov_bytes:
                                        continue
                                    mime = 'image/png' if ibytes.startswith(b'\x89PNG') else ('image/gif' if ibytes.startswith(b'GIF8') else 'image/jpeg')
                                    images.append({
                                        'label': f'Image #{len(images) + 1}',
                                        'data_uri': f"data:{mime};base64,{base64.b64encode(ibytes).decode('ascii')}",
                                    })

                    # Decompress PalmDOC text records (front matter + back matter)
                    max_txt_idx = min(len(spans) - 1, max(1, text_rec_count), first_img_idx - 1 if first_img_idx > 1 else len(spans) - 1)
                    rec_indices = list(range(1, min(11, max_txt_idx + 1)))
                    if max_txt_idx > 11:
                        for tail_i in range(max(11, max_txt_idx - 1), max_txt_idx + 1):
                            if tail_i not in rec_indices:
                                rec_indices.append(tail_i)

                    combined_raw = bytearray()
                    for r_idx in rec_indices:
                        if r_idx >= len(spans):
                            break
                        s_off, e_off = spans[r_idx]
                        fp.seek(s_off)
                        raw_rec = fp.read(min(e_off - s_off, 32768))
                        if compression == 1:
                            dec = raw_rec
                        elif compression == 2:
                            dec = _decompress_palmdoc_lz77(raw_rec)
                        else:
                            dec = b''
                        if dec:
                            combined_raw.extend(dec)
                            readable = _html_to_readable_text(dec)
                            if readable and len(readable.strip()) > 10:
                                lbl = f'Section {r_idx}' if r_idx <= 10 else f'Section {r_idx} (Back Matter)'
                                pages.append({
                                    'label': lbl,
                                    'text': readable[:6000],
                                })
                                _collect_isbns(readable)

            # Fallback to PyMuPDF if KF8/AZW3 or if no text pages were extracted
            if not pages or not images:
                try:
                    import pymupdf
                    doc = pymupdf.open(phys_path)
                    try:
                        if not total_pages:
                            total_pages = len(doc)
                        if not images:
                            for i in range(min(4, len(doc))):
                                pix = doc[i].get_pixmap(dpi=110)
                                b64 = base64.b64encode(pix.tobytes('png')).decode('ascii')
                                images.append({
                                    'label': f'Page {i + 1}',
                                    'data_uri': f'data:image/png;base64,{b64}',
                                })
                        if len(pages) <= 1:
                            for i in range(min(8, len(doc))):
                                txt = (doc[i].get_text() or '').strip()
                                if txt:
                                    pages.append({
                                        'label': f'Page {i + 1}',
                                        'text': txt[:6000],
                                    })
                                    _collect_isbns(txt)
                    finally:
                        doc.close()
                except Exception:
                    pass
        except Exception as mobi_err:
            return {'ok': False, 'error': f'Error reading {ext.upper()} for preview: {mobi_err}'}

    elif ext == '.cbz':
        try:
            with zipfile.ZipFile(phys_path, 'r') as z:
                img_names = sorted(
                    n for n in z.namelist()
                    if n.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif'))
                )
                total_pages = len(img_names)
                for idx, iname in enumerate(img_names[:8]):
                    ibytes = _read_zip_member_safe(z, iname)
                    if len(ibytes) >= 1000:
                        mime = 'image/png' if iname.lower().endswith('.png') else 'image/jpeg'
                        images.append({
                            'label': f'Page {idx + 1} ({os.path.basename(iname)})',
                            'data_uri': f"data:{mime};base64,{base64.b64encode(ibytes).decode('ascii')}",
                        })
                pages.append({
                    'label': 'Comic / Image Archive Info',
                    'text': f"CBZ Archive: {phys_path.name}\nTotal Image Pages: {total_pages}\nFirst {min(8, total_pages)} pages shown above.",
                })
        except Exception as cbz_err:
            return {'ok': False, 'error': f'Error reading CBZ for preview: {cbz_err}'}

    elif ext in ('.fb2', '.html', '.htm', '.xhtml', '.xml', '.txt', '.rtf', '.md'):
        try:
            with open(phys_path, 'rb') as fp:
                raw_bytes = fp.read(196608)
            if ext in ('.fb2', '.html', '.htm', '.xhtml', '.xml'):
                readable = _html_to_readable_text(raw_bytes)
            else:
                readable = raw_bytes.decode('utf-8', errors='replace')
            _collect_isbns(readable)
            chunk_size = 4500
            chunks = [readable[i : i + chunk_size].strip() for i in range(0, min(len(readable), 45000), chunk_size)]
            chunks = [c for c in chunks if c]
            total_pages = len(chunks)
            for idx, ch in enumerate(chunks[:10]):
                pages.append({
                    'label': f'Part {idx + 1}',
                    'text': ch,
                })
        except Exception as txt_err:
            return {'ok': False, 'error': f'Error reading {ext.upper()} for preview: {txt_err}'}


    elif ext in ('.mobi', '.azw', '.azw3', '.prc', '.pdb'):
        try:
            import struct
            base_dir = Path(getattr(settings, 'BASE_DIR', '.'))
            for sub in ('scanner', 'nas_scanner'):
                cand_dir = str((base_dir / sub).resolve())
                if os.path.isdir(cand_dir) and cand_dir not in sys.path:
                    sys.path.insert(0, cand_dir)
            from epubmeta.readers.mobi import MobiReader, _decompress_palmdoc_lz77

            mreader = MobiReader(phys_path)
            mmeta = mreader.read_metadata()
            meta_lines: List[str] = []
            if mmeta.title:
                meta_lines.append(f"Title: {mmeta.title}")
            if mmeta.authors:
                meta_lines.append(f"Author(s): {', '.join(mmeta.authors)}")
            if mmeta.publisher:
                meta_lines.append(f"Publisher: {mmeta.publisher}")
            if mmeta.published_date:
                meta_lines.append(f"Published Date: {mmeta.published_date}")
            if mmeta.isbn13:
                meta_lines.append(f"ISBN-13: {mmeta.isbn13}")
                if mmeta.isbn13 not in detected_isbns:
                    detected_isbns.append(mmeta.isbn13)
            if mmeta.asin:
                meta_lines.append(f"ASIN: {mmeta.asin}")
            if mmeta.subjects:
                meta_lines.append(f"Subjects: {', '.join(mmeta.subjects)}")
            if mmeta.description:
                clean_desc = _html_to_readable_text(mmeta.description.encode('utf-8', errors='ignore')) or mmeta.description
                meta_lines.append(f"\nDescription / Synopsis:\n{clean_desc}")
            if meta_lines:
                pages.append({
                    'label': 'EXTH Header Info',
                    'text': '\n'.join(meta_lines)[:6000],
                })

            spans = mreader._read_palmdb_records(max_records=800)
            total_pages = max(1, len(spans) - 1) if spans else 0

            # Extract embedded cover & front images from PalmDB records
            cov_bytes = mreader.extract_cover_bytes()
            if cov_bytes and len(cov_bytes) >= 1000:
                mime = 'image/png' if cov_bytes.startswith(b'\x89PNG') else ('image/gif' if cov_bytes.startswith(b'GIF8') else 'image/jpeg')
                images.append({
                    'label': 'Embedded Cover',
                    'data_uri': f"data:{mime};base64,{base64.b64encode(cov_bytes).decode('ascii')}",
                })

            if len(spans) >= 2:
                with open(phys_path, 'rb') as fp:
                    r0_start, r0_end = spans[0]
                    fp.seek(r0_start)
                    rec0 = fp.read(min(r0_end - r0_start, 65536))
                    compression = struct.unpack('>H', rec0[0:2])[0] if len(rec0) >= 16 else 0
                    text_rec_count = struct.unpack('>H', rec0[8:10])[0] if len(rec0) >= 16 else min(len(spans) - 1, 12)
                    mobi_pos = rec0.find(b'MOBI')
                    first_img_idx = (
                        struct.unpack('>I', rec0[mobi_pos + 92 : mobi_pos + 96])[0]
                        if (mobi_pos != -1 and mobi_pos + 96 <= len(rec0))
                        else (text_rec_count + 1)
                    )

                    # Collect up to 5 additional embedded images from MOBI image records
                    if 0 < first_img_idx < len(spans):
                        for rec_i in range(first_img_idx, min(len(spans), first_img_idx + 25)):
                            if len(images) >= 6:
                                break
                            s_off, e_off = spans[rec_i]
                            sz = e_off - s_off
                            if 1500 <= sz <= 8 * 1024 * 1024:
                                fp.seek(s_off)
                                ibytes = fp.read(sz)
                                if ibytes.startswith((b'\xff\xd8\xff', b'\x89PNG', b'GIF8')):
                                    if cov_bytes and ibytes == cov_bytes:
                                        continue
                                    mime = 'image/png' if ibytes.startswith(b'\x89PNG') else ('image/gif' if ibytes.startswith(b'GIF8') else 'image/jpeg')
                                    images.append({
                                        'label': f'Image #{len(images) + 1}',
                                        'data_uri': f"data:{mime};base64,{base64.b64encode(ibytes).decode('ascii')}",
                                    })

                    # Decompress PalmDOC text records (front matter + back matter)
                    max_txt_idx = min(len(spans) - 1, max(1, text_rec_count), first_img_idx - 1 if first_img_idx > 1 else len(spans) - 1)
                    rec_indices = list(range(1, min(11, max_txt_idx + 1)))
                    if max_txt_idx > 11:
                        for tail_i in range(max(11, max_txt_idx - 1), max_txt_idx + 1):
                            if tail_i not in rec_indices:
                                rec_indices.append(tail_i)

                    combined_raw = bytearray()
                    for r_idx in rec_indices:
                        if r_idx >= len(spans):
                            break
                        s_off, e_off = spans[r_idx]
                        fp.seek(s_off)
                        raw_rec = fp.read(min(e_off - s_off, 32768))
                        if compression == 1:
                            dec = raw_rec
                        elif compression == 2:
                            dec = _decompress_palmdoc_lz77(raw_rec)
                        else:
                            dec = b''
                        if dec:
                            combined_raw.extend(dec)
                            readable = _html_to_readable_text(dec)
                            if readable and len(readable.strip()) > 10:
                                lbl = f'Section {r_idx}' if r_idx <= 10 else f'Section {r_idx} (Back Matter)'
                                pages.append({
                                    'label': lbl,
                                    'text': readable[:6000],
                                })
                                _collect_isbns(readable)

            # Fallback to PyMuPDF if KF8/AZW3 or if no text pages were extracted
            if not pages or not images:
                try:
                    import pymupdf
                    doc = pymupdf.open(phys_path)
                    try:
                        if not total_pages:
                            total_pages = len(doc)
                        if not images:
                            for i in range(min(4, len(doc))):
                                pix = doc[i].get_pixmap(dpi=110)
                                b64 = base64.b64encode(pix.tobytes('png')).decode('ascii')
                                images.append({
                                    'label': f'Page {i + 1}',
                                    'data_uri': f'data:image/png;base64,{b64}',
                                })
                        if len(pages) <= 1:
                            for i in range(min(8, len(doc))):
                                txt = (doc[i].get_text() or '').strip()
                                if txt:
                                    pages.append({
                                        'label': f'Page {i + 1}',
                                        'text': txt[:6000],
                                    })
                                    _collect_isbns(txt)
                    finally:
                        doc.close()
                except Exception:
                    pass
        except Exception as mobi_err:
            return {'ok': False, 'error': f'Error reading {ext.upper()} for preview: {mobi_err}'}

    elif ext == '.cbz':
        try:
            with zipfile.ZipFile(phys_path, 'r') as z:
                img_names = sorted(
                    n for n in z.namelist()
                    if n.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif'))
                )
                total_pages = len(img_names)
                for idx, iname in enumerate(img_names[:8]):
                    ibytes = _read_zip_member_safe(z, iname)
                    if len(ibytes) >= 1000:
                        mime = 'image/png' if iname.lower().endswith('.png') else 'image/jpeg'
                        images.append({
                            'label': f'Page {idx + 1} ({os.path.basename(iname)})',
                            'data_uri': f"data:{mime};base64,{base64.b64encode(ibytes).decode('ascii')}",
                        })
                pages.append({
                    'label': 'Comic / Image Archive Info',
                    'text': f"CBZ Archive: {phys_path.name}\nTotal Image Pages: {total_pages}\nFirst {min(8, total_pages)} pages shown above.",
                })
        except Exception as cbz_err:
            return {'ok': False, 'error': f'Error reading CBZ for preview: {cbz_err}'}

    elif ext in ('.fb2', '.html', '.htm', '.xhtml', '.xml', '.txt', '.rtf', '.md'):
        try:
            with open(phys_path, 'rb') as fp:
                raw_bytes = fp.read(196608)
            if ext in ('.fb2', '.html', '.htm', '.xhtml', '.xml'):
                readable = _html_to_readable_text(raw_bytes)
            else:
                readable = raw_bytes.decode('utf-8', errors='replace')
            _collect_isbns(readable)
            chunk_size = 4500
            chunks = [readable[i : i + chunk_size].strip() for i in range(0, min(len(readable), 45000), chunk_size)]
            chunks = [c for c in chunks if c]
            total_pages = len(chunks)
            for idx, ch in enumerate(chunks[:10]):
                pages.append({
                    'label': f'Part {idx + 1}',
                    'text': ch,
                })
        except Exception as txt_err:
            return {'ok': False, 'error': f'Error reading {ext.upper()} for preview: {txt_err}'}

    else:
        # Universal PyMuPDF fallback for any other ebook/document format (.djvu, .docx, .xps, etc.)
        try:
            import pymupdf
            doc = pymupdf.open(phys_path)
            try:
                total_pages = len(doc)
                for i in range(min(6, total_pages)):
                    pix = doc[i].get_pixmap(dpi=120)
                    b64 = base64.b64encode(pix.tobytes('png')).decode('ascii')
                    images.append({
                        'label': f'Page {i + 1} of {total_pages}',
                        'data_uri': f'data:image/png;base64,{b64}',
                    })
                for i in range(min(10, total_pages)):
                    txt = (doc[i].get_text() or '').strip()
                    if txt:
                        pages.append({
                            'label': f'Page {i + 1}',
                            'text': txt[:6000],
                        })
                        _collect_isbns(txt)
            finally:
                doc.close()
        except Exception:
            return {'ok': False, 'error': f'Preview is not available for {ext} files.'}

    script_prefix = getattr(settings, 'FORCE_SCRIPT_NAME', '') or ''
    raw_q = urllib.parse.urlencode({'file_path': str(phys_path)})
    raw_stream_url = f"{script_prefix.rstrip('/')}/api/book-preview/raw/?{raw_q}"

    return {
        'ok': True,
        'file_name': phys_path.name,
        'file_path': str(phys_path),
        'file_ext': ext.upper().lstrip('.'),
        'file_size_mb': round(phys_path.stat().st_size / (1024 * 1024), 2),
        'total_sections': total_pages,
        'images': images,
        'pages': pages,
        'detected_isbns': detected_isbns,
        'raw_stream_url': raw_stream_url,
    }


# ==============================================================================
# Enriched Duplicates Detection, Smart Winner Scoring & Safe Resolution Engine
# ==============================================================================

_DUPLICATES_ENGINE_CACHE: Dict[str, Any] = {
    'expires_at': 0.0,
    'groups_by_key': {},
    'all_groups': [],
    'summary': {},
}

_SPLITFILES_DUP_SUFFIX_RE = re.compile(
    r'(?:(?<=[\s_\-A-Za-z0-9\]\)])|\b)(\((?:[1-9]|[1-9]\d)\))'
    r'(?=\s*(?:\([A-Za-z0-9]{2,5}\))?(?:\s*--.*)?\.[A-Za-z0-9]+$)',
    re.IGNORECASE,
)
_COPY_DUP_SUFFIX_RE = re.compile(
    r'(\s+-\s+Copy(?:\s*\(\d+\))?|\s+copy(?:\s*\(\d+\))?|_copy(?:\s*\(\d+\))?)(?=\.[A-Za-z0-9]+$)',
    re.IGNORECASE,
)
_VOL_EDITION_MARKER_RE = re.compile(
    r'\b(?:vol(?:ume)?|book|bk|part|pt|issue|no|nr|tome|band|bd)\.?\s*([0-9]{1,3}|[ivxlc]{1,5})\b'
    r'|\b([1-9])(?:st|nd|rd|th)\s+ed(?:ition)?\b',
    re.IGNORECASE,
)
_FICTION_HINT_RE = re.compile(
    r'\b(fiction|novel|fantasy|mystery|thriller|romance|horror|detective|adventure|saga|trilogy|chronicles|a\s+novel|st\.?\s*martin\'?s|penguin|random\s+house|harpercollins|hachette|simon\s*(?:&|and)\s*schuster|macmillan|tor\s+books)\b',
    re.IGNORECASE,
)
_TECH_HINT_RE = re.compile(
    r'\b(programming|software|computer|python|java|javascript|typescript|rust|golang|c\+\+|c#|linux|unix|database|sql|cloud|kubernetes|docker|ai|artificial\s+intelligence|machine\s+learning|deep\s+learning|data\s+science|data\s+engineering|data\s+mesh|data\s+fabric|cybersecurity|security|networking|devops|algorithm|architecture|developer|apress|o\'reilly|packt|manning|no\s+starch|addison-wesley|wiley|pragmatic|sams|wrox|sybex|crc\s+press)\b',
    re.IGNORECASE,
)

# Specific domain regexes for the category directories above the 3-letter folder
_CATEGORY_DOMAIN_RULES: List[Tuple[str, re.Pattern, int]] = [
    ('Chess Library', re.compile(r'\b(chess|grandmaster|checkmate|endgame|opening\s+repertoire|sicilian|fide|kasparov|carlsen|fischer)\b', re.I), 55),
    ('Chinese-China', re.compile(r'\b(china|chinese|beijing|shanghai|taiwan|hong\s+kong|mandarin|ccp|mao|confuci|dynasty|shenzhen)\b', re.I), 55),
    ('Japan', re.compile(r'\b(japan|japanese|tokyo|kyoto|samurai|shogun|anime|manga|zen|bushido|okinawa)\b', re.I), 55),
    ('Recipes', re.compile(r'\b(recipe|cookbook|cooking|baking|culinary|kitchen|food|chef|meal|diet|nutrition|cuisine|gastronomy|eat|eating|dinner|dessert|bread)\b', re.I), 55),
    ('Political', re.compile(r'\b(politic|government|democracy|authoritarian|geopolitic|foreign\s+policy|diplomacy|statecraft|election|globalization|capitalism|socialism|marx|governance|public\s+policy|international\s+relations|migration|urbanization|warfare|military|imperialism|constitution)\b', re.I), 55),
    ('Religon related texts', re.compile(r'\b(religio|theolog|bible|biblical|christian|church|jesus|islam|quran|koran|muslim|buddhis|hindu|judaism|jewish|torah|talmud|spiritual|faith|gospel|scripture)\b', re.I), 55),
    ('Role Playing Games', re.compile(r'\b(role[\s\-]*playing|rpg|dungeons\s*(?:&|and)\s*dragons|d&d|pathfinder|warhammer|shadowrun|gurps|cthulhu|tabletop|gamemaster|dungeon\s+master|campaign\s+setting|rulebook)\b', re.I), 55),
    ('Game Engine and Game Design', re.compile(r'\b(game\s+engine|game\s+design|game\s+development|game\s+programming|unity|unreal\s+engine|godot|directx|opengl|vulkan|shader|3d\s+graphics|pygame)\b', re.I), 55),
    ('Electronic Schematics', re.compile(r'\b(schematic|circuit|electronics|transistor|oscilloscope|arduino|raspberry\s+pi|microcontroller|pcb|amplifi|voltage|resistor|capacitor|soldering|ham\s+radio|datasheet|semiconductor|fpga|vhdl|verilog)\b', re.I), 55),
    ('MainFrame Files', re.compile(r'\b(mainframe|ibm\s+z|z/os|cobol|jcl|cics|db2|vsam|tso|ispf|as/400|system/360|system/370|system/390|fortran|pl/i|rexx)\b', re.I), 55),
    ('Memory', re.compile(r'\b(memory\s+improvement|mnemonics|memorization|brain\s+training|speed\s+reading|cognitive)\b', re.I), 55),
    ('Jokes', re.compile(r'\b(joke|humor|humour|comedy|funny|satire|parody|wit|pun|riddle|cartoon)\b', re.I), 55),
    ('Science Fiction', re.compile(r'\b(science\s+fiction|sci[\s\-]*fi|cyberpunk|space\s+opera|dystopia|post[\s\-]*apocalyp|time\s+travel|starship|galactic|extraterrestrial|asimov|heinlein|clarke|philip\s+k\.?\s*dick|dune|star\s+wars|star\s+trek)\b', re.I), 50),
    ('Computer Hardware', re.compile(r'\b(computer\s+hardware|pc\s+hardware|hardware\s+hacking|motherboard|bios|uefi|cpu|processor|microprocessor|x86|arm64|usb|pci|raid|upgrading\s+and\s+repairing|computer\s+virus|viruses)\b', re.I), 50),
    ('Computer Book Code', _TECH_HINT_RE, 42),
    ('File Formats', re.compile(r'\b(file\s+format|pdf\s+reference|postscript|epub|xml|json|unicode|font|compression|encoding)\b', re.I), 48),
    ('Computer Mags & FAQ', _TECH_HINT_RE, 45),
    ('Fiction', _FICTION_HINT_RE, 45),
]

_CATCHALL_CATEGORIES = {'misc', 'root', 'unsorted', 'other', 'temp', 'tmp'}


def _evaluate_category_directory_fit(
    category_dir: str,
    group_categories: set,
    meta_blob: str,
) -> Tuple[int, str, List[Dict[str, str]]]:
    """
    Evaluate how well `category_dir` (the directory above the 3-letter `splitfiles` directory)
    fits the book's metadata (`title`, `authors`, `publisher`, `description`, `subjects`),
    especially when copies in a duplicate group live in different category directories.
    """
    cat_clean = (category_dir or 'Root').strip()
    cat_low = cat_clean.lower()
    reasons: List[Dict[str, str]] = []

    # Check if this category matches its domain rule against the book's metadata
    matched_pts = 0
    for rule_cat, rx, pts in _CATEGORY_DOMAIN_RULES:
        if rule_cat.lower() == cat_low:
            if rx.search(meta_blob or ''):
                matched_pts = pts
            break

    # Special refinement: if comparing `Fiction` vs `Science Fiction` and the book has NO sci-fi keywords,
    # `Fiction` is the better category; if it DOES have sci-fi keywords, `Science Fiction` wins (`50` > `45`).
    has_multiple_cats = len(group_categories) > 1
    non_catchall_in_group = {c for c in group_categories if c.lower() not in _CATCHALL_CATEGORIES}

    if not has_multiple_cats:
        if cat_low in _CATCHALL_CATEGORIES:
            reasons.append({
                'text': f'Category: {cat_clean}',
                'type': 'info',
                'icon': 'fa-folder',
            })
            return 0, 'neutral', reasons
        reasons.append({
            'text': f'Category: {cat_clean}',
            'type': 'info',
            'icon': 'fa-folder-open',
        })
        return 0, 'ok', reasons

    # Multiple DIFFERENT categories exist in this duplicate group!
    if cat_low in _CATCHALL_CATEGORIES and non_catchall_in_group:
        reasons.append({
            'text': f'Catch-All Category ({cat_clean})',
            'type': 'warn',
            'icon': 'fa-folder-minus',
        })
        return -40, 'mismatch', reasons

    if matched_pts > 0:
        reasons.append({
            'text': f'Best Category Fit ({cat_clean})',
            'type': 'good',
            'icon': 'fa-folder-tree',
        })
        return matched_pts, 'best', reasons

    # If the other copy is in `Misc`/`Root` and this copy is in a specific category (even without keyword match),
    # prefer the specific category over `Misc`!
    if len(non_catchall_in_group) == 1 and cat_clean in non_catchall_in_group:
        reasons.append({
            'text': f'Better Category ({cat_clean} over Misc)',
            'type': 'good',
            'icon': 'fa-folder-tree',
        })
        return 40, 'best', reasons

    # Check if ANOTHER category in the group matched its domain keywords while this one did not
    other_matched = False
    for other_c in group_categories:
        if other_c.lower() == cat_low:
            continue
        for rule_cat, rx, _pts in _CATEGORY_DOMAIN_RULES:
            if rule_cat.lower() == other_c.lower() and rx.search(meta_blob or ''):
                other_matched = True
                break
    if other_matched:
        reasons.append({
            'text': f'Less Fitting Category ({cat_clean})',
            'type': 'warn',
            'icon': 'fa-folder-minus',
        })
        return -25, 'mismatch', reasons

    reasons.append({
        'text': f'Category: {cat_clean}',
        'type': 'info',
        'icon': 'fa-folder-open',
    })
    return 10, 'ok', reasons


_DUPLICATES_SHARED_CACHE_FILE = Path('/tmp/mylibrary_duplicates_cache.json')


def invalidate_duplicates_cache() -> None:
    _DUPLICATES_ENGINE_CACHE['expires_at'] = 0.0
    _DUPLICATES_ENGINE_CACHE['all_groups'] = []
    _DUPLICATES_ENGINE_CACHE['groups_by_key'] = {}
    _DUPLICATES_ENGINE_CACHE['summary'] = {}
    try:
        if _DUPLICATES_SHARED_CACHE_FILE.exists():
            _DUPLICATES_SHARED_CACHE_FILE.unlink()
    except Exception:
        pass


def _get_duplicates_quarantine_dir() -> Optional[Path]:
    """
    Resolve or create `/books_bad/duplicates` (or `/volume1/common/Media/books_bad/duplicates`
    or `U:\\Media\\books_bad\\duplicates`) for safe quarantine of removed duplicate files.
    """
    for base_str in ('/books_bad', '/volume1/common/Media/books_bad', r'U:\Media\books_bad'):
        base_p = Path(base_str)
        try:
            if base_p.exists() and base_p.is_dir():
                dup_dir = base_p / 'duplicates'
                dup_dir.mkdir(parents=True, exist_ok=True)
                return dup_dir
        except Exception:
            continue
    return None


def _detect_splitfiles_dup_suffix(file_name: str) -> Optional[str]:
    """
    Detect if `file_name` has a `(#)` suffix added by `splitfiles` (e.g. `(1)`, `(2)`)
    or an OS `- Copy` suffix. Ignores 4-digit years like `(2024)` and format tags like `(epub)`.
    """
    if not file_name:
        return None
    m = _SPLITFILES_DUP_SUFFIX_RE.search(file_name)
    if m:
        return m.group(1)
    m_copy = _COPY_DUP_SUFFIX_RE.search(file_name)
    if m_copy:
        return m_copy.group(1).strip()
    return None


def _extract_volume_or_edition_marker(text: str) -> str:
    """
    Extract normalized volume/book/part/edition markers from a filename or original title
    so different volumes of a series are not treated as identical book content unless SHA-256 matches.
    """
    if not text:
        return ''
    markers = []
    for m in _VOL_EDITION_MARKER_RE.finditer(text.lower()):
        if m.group(1):
            markers.append(f"v:{m.group(1)}")
        elif m.group(2):
            markers.append(f"ed:{m.group(2)}")
    return '|'.join(sorted(set(markers)))


def _compute_expected_3letter_prefixes(title: str, authors: str) -> Dict[str, set]:
    """
    Compute the expected 3-letter `splitfiles` directory prefixes (`ABC`) from a book's
    enriched `title` and `authors`.
    """
    title_pfxs: set = set()
    author_pfxs: set = set()

    def _alnum3(s: str) -> str:
        clean = re.sub(r'[^A-Za-z0-9]+', '', s or '').upper()
        return clean[:3] if len(clean) >= 3 else ''

    if title:
        p1 = _alnum3(title)
        if p1:
            title_pfxs.add(p1)
        no_art = re.sub(r'^(?:the|a|an)\s+', '', title.strip(), flags=re.IGNORECASE)
        p2 = _alnum3(no_art)
        if p2:
            title_pfxs.add(p2)

    if authors and authors.strip().lower() not in ('', 'unknown', 'unknown author'):
        first_author = re.split(r'\s*(?:,|;|&|\band\b)\s*', authors.strip(), maxsplit=1)[0].strip()
        pa_full = _alnum3(first_author)
        if pa_full:
            author_pfxs.add(pa_full)
        parts = [p for p in re.split(r'[\s,]+', first_author) if p]
        if parts:
            p_first = _alnum3(parts[0])
            p_last = _alnum3(parts[-1])
            if p_first:
                author_pfxs.add(p_first)
            if p_last:
                author_pfxs.add(p_last)

    return {
        'title_prefixes': title_pfxs,
        'author_prefixes': author_pfxs,
        'all_prefixes': title_pfxs | author_pfxs,
    }


def _parse_book_dir_components(file_path: str) -> Dict[str, str]:
    """
    Parse `/books/<Category>/<3-letter>/<filename>` into `category_dir`, `prefix_dir`, and `parent_dir`.
    """
    norm = (file_path or '').replace('\\', '/')
    rel = norm
    for root_pfx in ('/volume1/common/Media/Books/', '/books/', '/volume1/common/Media/Audiobooks/', '/audiobooks/'):
        if norm.startswith(root_pfx):
            rel = norm[len(root_pfx):]
            break
    parts = [p for p in rel.split('/') if p]
    if len(parts) <= 1:
        return {'category_dir': 'Root', 'prefix_dir': '', 'parent_dir': os.path.dirname(norm)}
    dir_parts = parts[:-1]
    category_dir = dir_parts[0]
    prefix_dir = ''
    if len(dir_parts) >= 2 and 1 <= len(dir_parts[-1]) <= 4:
        prefix_dir = dir_parts[-1]
    elif len(dir_parts) == 1 and 1 <= len(dir_parts[0]) <= 3:
        prefix_dir = dir_parts[0]
        category_dir = 'Root'
    return {
        'category_dir': category_dir,
        'prefix_dir': prefix_dir,
        'parent_dir': os.path.dirname(norm),
    }


def _score_duplicate_file_candidate(
    f_loc: FileLocation,
    item: CatalogItem,
    group_categories: set,
    enriched_paths_set: set,
    moon_stem_to_meta: Dict[str, Dict[str, Any]],
    oldest_ts: Optional[float],
    newest_ts: Optional[float],
    meta_blob: str,
) -> Dict[str, Any]:
    """
    Score a `FileLocation` within an enriched duplicate group to determine which copy to KEEP.
    Prioritizes:
      1. No unfinished/active Moon+ Reader bookmark on this specific filename (`+120` if bookmarked)
      2. Does NOT have a `(#)` duplicate suffix from `splitfiles` (`+60` vs `-60`)
      3. Best Category Directory above the 3-letter folder (e.g. `Political` vs `Misc`,
         `Recipes` vs `Misc`, `Computer Mags & FAQ` vs `Fiction`) (`-40` to `+55`)
      4. Oldest file timestamp (`+25` for oldest copy)
      5. Clean enriched filename & direct `SUCCESS` status in `scanner_state.db` (`+18`)
    """
    score = 0
    reasons: List[Dict[str, str]] = []
    fname = f_loc.file_name or os.path.basename(f_loc.file_path or '')
    stem = os.path.splitext(fname)[0]

    # 1. Check `(#)` splitfiles duplicate suffix
    dup_suffix = _detect_splitfiles_dup_suffix(fname)
    if dup_suffix:
        score -= 60
        reasons.append({
            'text': f'Has {dup_suffix} Duplicate Suffix',
            'type': 'warn',
            'icon': 'fa-triangle-exclamation',
        })
    else:
        score += 60
        reasons.append({
            'text': 'No (#) Suffix',
            'type': 'good',
            'icon': 'fa-check',
        })

    # 2. Check Category Directory Fit (the directory above the 3-letter `splitfiles` directory)
    dir_info = _parse_book_dir_components(f_loc.file_path or '')
    category_dir = dir_info['category_dir']
    prefix_dir = dir_info['prefix_dir']

    cat_pts, dir_fit_tier, cat_reasons = _evaluate_category_directory_fit(
        category_dir=category_dir,
        group_categories=group_categories,
        meta_blob=meta_blob,
    )
    score += cat_pts
    reasons.extend(cat_reasons)

    # Clean filename check (prefer `Title - Author.epub` over `-- Anna's Archive` or `(epub).epub` or MD5 hashes)
    has_raw_noise = bool(
        re.search(
            r'(?:anna[\'\u2019]?s\s*archive|z-lib|\((?:epub|pdf|mobi|azw3|cbz|cbr|retail)\)\.[a-z0-9]+$|\b[0-9a-f]{32}\b|--)',
            fname,
            re.I,
        )
    )
    if not has_raw_noise and ' - ' in stem:
        score += 15
        reasons.append({
            'text': 'Clean Title - Author Filename',
            'type': 'good',
            'icon': 'fa-file-circle-check',
        })
    elif has_raw_noise:
        score -= 10
        reasons.append({
            'text': 'Uncleaned / Raw Filename',
            'type': 'warn',
            'icon': 'fa-file-Signature',
        })

    # 3. Oldest File Timestamp
    dt_candidates = [dt for dt in (f_loc.file_created_at, f_loc.file_modified_at) if dt is not None]
    eff_dt = min(dt_candidates) if dt_candidates else None
    eff_ts = eff_dt.timestamp() if eff_dt else None
    date_str = eff_dt.strftime('%Y-%m-%d %H:%M') if eff_dt else 'Unknown'
    date_short = eff_dt.strftime('%Y-%m-%d') if eff_dt else 'Unknown'
    is_oldest = False

    if eff_ts is not None and oldest_ts is not None and newest_ts is not None:
        if newest_ts - oldest_ts >= 60.0:  # At least 1 minute difference between copies
            if abs(eff_ts - oldest_ts) < 60.0:
                is_oldest = True
                score += 25
                reasons.append({
                    'text': f'Oldest Copy ({date_short})',
                    'type': 'good',
                    'icon': 'fa-clock-rotate-left',
                })
            elif abs(eff_ts - newest_ts) < 60.0:
                reasons.append({
                    'text': f'Newer Copy ({date_short})',
                    'type': 'info',
                    'icon': 'fa-clock',
                })

    # 4. Direct enrichment status in scanner_state.db
    norm_fp = (f_loc.file_path or '').strip().replace('\\', '/')
    file_is_enriched = norm_fp in enriched_paths_set
    if not file_is_enriched and norm_fp.startswith('/books/'):
        file_is_enriched = ('/volume1/common/Media/Books/' + norm_fp[len('/books/'):]) in enriched_paths_set
    if file_is_enriched:
        score += 18
        reasons.append({
            'text': 'Enriched File (Metadata Embedded)',
            'type': 'good',
            'icon': 'fa-wand-magic-sparkles',
        })

    # 5. Moon+ Reader bookmark stem match (protects bookmarked file even if 100% read)
    moon_meta = (
        moon_stem_to_meta.get(fname.lower())
        or moon_stem_to_meta.get(_moon_sanitize_stem(fname).lower())
    )
    if moon_meta:
        score += 120
        reasons.append({
            'text': f"Moon+ Bookmark ({moon_meta.get('pct') or 'Saved'})",
            'type': 'good',
            'icon': 'fa-bookmark',
        })

    return {
        'score': score,
        'reasons': reasons,
        'has_dup_suffix': bool(dup_suffix),
        'dup_suffix_str': dup_suffix or '',
        'category_dir': category_dir,
        'prefix_dir': prefix_dir,
        'dir_fit_tier': dir_fit_tier,
        'is_oldest': is_oldest,
        'effective_ts': eff_ts,
        'date_str': date_str,
        'date_short': date_short,
        'file_is_enriched': file_is_enriched,
        'has_moon_bookmark': bool(moon_meta),
        'moon_pct': moon_meta.get('pct', '') if moon_meta else '',
    }


def _build_all_enriched_duplicate_groups() -> Dict[str, Any]:
    """
    Scan enriched books in BookDB + `scanner_state.db` to find all duplicate file groups
    that have the **exact same format and book content**.
    """
    now = time.monotonic()
    if _DUPLICATES_ENGINE_CACHE['all_groups'] and now < _DUPLICATES_ENGINE_CACHE['expires_at']:
        return _DUPLICATES_ENGINE_CACHE

    # Check shared cross-worker cache file (/tmp/mylibrary_duplicates_cache.json)
    try:
        if _DUPLICATES_SHARED_CACHE_FILE.exists():
            age_sec = time.time() - _DUPLICATES_SHARED_CACHE_FILE.stat().st_mtime
            if age_sec < 1800.0:
                shared_data = json.loads(_DUPLICATES_SHARED_CACHE_FILE.read_text(encoding='utf-8'))
                if shared_data.get('all_groups') is not None:
                    _DUPLICATES_ENGINE_CACHE['expires_at'] = now + max(30.0, 1800.0 - age_sec)
                    _DUPLICATES_ENGINE_CACHE['all_groups'] = shared_data['all_groups']
                    _DUPLICATES_ENGINE_CACHE['groups_by_key'] = {
                        g['group_key']: g for g in shared_data['all_groups']
                    }
                    _DUPLICATES_ENGINE_CACHE['summary'] = shared_data.get('summary') or {}
                    return _DUPLICATES_ENGINE_CACHE
    except Exception:
        pass

    # 1. Load Moon+ Reader bookmark states so we can identify unfinished bookmarks (< 99.5%)
    #    Use warm `_BOOKMARKED_SHELF_CACHE` if available, plus fast direct scan of `.po`/`.an` stems in `/books_bookmark`.
    moon_meta_by_id: Dict[str, Dict[str, Any]] = _BOOKMARKED_SHELF_CACHE.get('moon_meta_by_id') or {}
    moon_stem_to_meta: Dict[str, Dict[str, Any]] = {}
    unfinished_item_ids: set = set()
    unfinished_stems: set = set()

    for str_cid, m_meta in moon_meta_by_id.items():
        pct_val = float(m_meta.get('pct_val') or 0.0)
        m_stem = (m_meta.get('moon_stem') or '').lower()
        if m_stem:
            moon_stem_to_meta[m_stem] = m_meta
            moon_stem_to_meta[_moon_sanitize_stem(m_stem).lower()] = m_meta
        if pct_val < 99.5:
            unfinished_item_ids.add(str_cid)
            if m_stem:
                unfinished_stems.add(m_stem)
                unfinished_stems.add(_moon_sanitize_stem(m_stem).lower())

    cache_dir = _get_moon_cache_dir()
    if cache_dir and not moon_stem_to_meta:
        try:
            for entry in os.scandir(cache_dir):
                if not entry.is_file():
                    continue
                name = entry.name
                if not (name.endswith('.po') or name.endswith('.an')):
                    continue
                stem = name[:-3]
                stem_low = stem.lower()
                san_low = _moon_sanitize_stem(stem).lower()
                info = moon_stem_to_meta.setdefault(
                    stem_low,
                    {'moon_stem': stem, 'pct': 'Bookmarked', 'pct_val': 0.0},
                )
                moon_stem_to_meta[san_low] = info
                if name.endswith('.po'):
                    try:
                        with open(entry.path, 'r', encoding='utf-8', errors='replace') as f_po:
                            po_line = f_po.read(128).strip()
                        if ':' in po_line:
                            info['pct'] = po_line.rsplit(':', 1)[-1].strip()
                        m_po = re.search(r'\*(\d+)@\d+#(\d+):([\d.]+)%', po_line)
                        if m_po:
                            info['pct_val'] = float(m_po.group(3))
                    except Exception:
                        pass
            for st_key, m_info in moon_stem_to_meta.items():
                if float(m_info.get('pct_val') or 0.0) < 99.5:
                    unfinished_stems.add(st_key)
        except Exception:
            pass

    # 2. Load `processed_files WHERE status = 'SUCCESS'` from `scanner_state.db` in a single pass
    #    to populate `enr_paths_set`, `orig_info_by_path`, AND detect enriched duplicate groups in memory!
    enr_paths_set: set = set(_ENRICHED_PATHS_CACHE.get('paths_set') or ())
    enr_ids_set: set = set(_ENRICHED_PATHS_CACHE.get('enriched_item_ids_set') or ())
    orig_info_by_path: Dict[str, Dict[str, str]] = {}
    paths_by_ta: Dict[Tuple[str, str], List[str]] = {}
    raw_titles_by_ta: Dict[Tuple[str, str], str] = {}
    paths_by_isbn: Dict[str, List[str]] = {}
    paths_by_sha: Dict[str, List[str]] = {}
    all_enr_shas: set = set()
    all_enr_titles: set = set()
    all_enr_isbns: set = set()

    db_path = _get_scanner_db_path()
    if is_postgres_scanner_enabled() or db_path.exists():
        try:
            conn = connect_scanner_db(db_path, timeout=5.0)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT file_path, status, title, author, isbn,
                       orig_title, orig_author, orig_isbn, orig_sha256, file_sha256
                FROM processed_files
                WHERE status = 'SUCCESS'
                """
            ).fetchall()
            conn.close()
            for r in rows:
                fp = (r['file_path'] or '').strip()
                if not fp:
                    continue
                if fp.startswith('/volume1/common/Media/Books/'):
                    norm_fp = '/books/' + fp[len('/volume1/common/Media/Books/'):]
                else:
                    norm_fp = fp
                enr_paths_set.add(norm_fp)
                enr_paths_set.add(fp)
                info = {
                    'status': 'SUCCESS',
                    'orig_title': r['orig_title'] or '',
                    'orig_author': r['orig_author'] or '',
                    'orig_isbn': r['orig_isbn'] or '',
                    'orig_sha256': r['orig_sha256'] or '',
                    'file_sha256': r['file_sha256'] or '',
                }
                orig_info_by_path[norm_fp] = info
                orig_info_by_path[fp] = info

                t_val = (r['title'] or '').strip()
                ot_val = (r['orig_title'] or '').strip()
                a_val = (r['author'] or '').strip()
                if t_val:
                    all_enr_titles.add(t_val)
                if ot_val:
                    all_enr_titles.add(ot_val)
                nt = _norm_lookup_str(t_val)
                na = _norm_lookup_str(a_val)
                if nt and na and na not in ('unknown', 'unknown author'):
                    ta_key = (nt, na)
                    paths_by_ta.setdefault(ta_key, []).append(norm_fp)
                    raw_titles_by_ta[ta_key] = t_val

                clean_isbn = re.sub(r'[^0-9Xx]', '', str(r['isbn'] or '')).upper()
                if clean_isbn and len(clean_isbn) in (10, 13):
                    all_enr_isbns.add(clean_isbn)
                    paths_by_isbn.setdefault(clean_isbn, []).append(norm_fp)

                for sha_val in (r['file_sha256'], r['orig_sha256']):
                    if sha_val:
                        s_str = str(sha_val)
                        all_enr_shas.add(s_str)
                        paths_by_sha.setdefault(s_str, []).append(norm_fp)
        except Exception:
            pass

    if not enr_paths_set:
        return {'all_groups': [], 'groups_by_key': {}, 'summary': {}}

    cand_item_ids: set = set()
    dup_raw_titles: set = set()
    dup_isbns: set = set()
    dup_shas: set = set()

    for ta_key, fps in paths_by_ta.items():
        if len(fps) > 1 and ta_key in raw_titles_by_ta:
            dup_raw_titles.add(raw_titles_by_ta[ta_key])

    for isbn_k, fps in paths_by_isbn.items():
        if len(fps) > 1:
            dup_isbns.add(isbn_k)

    for sha_k, fps in paths_by_sha.items():
        if len(fps) > 1:
            dup_shas.add(sha_k)

    # 2a. Enriched CatalogItems that have 2+ FileLocations of the SAME file_format.
    #     Uses indexed `catalog_item__title__in` + `catalog_item_id` and `.order_by()` to avoid
    #     grouping or sorting by unindexed `file_name`.
    t_all_list = list(all_enr_titles)
    for i in range(0, len(t_all_list), 2000):
        same_item_dup_rows = (
            FileLocation.objects.filter(
                catalog_item__title__in=t_all_list[i : i + 2000],
                is_invalid=False,
            )
            .order_by()
            .values('catalog_item_id', 'file_format')
            .annotate(cnt=Count('id'))
            .filter(cnt__gt=1)
        )
        for r in same_item_dup_rows:
            cand_item_ids.add(r['catalog_item_id'])

    # 2b. Map duplicate enriched SHA-256 hashes, titles, and ISBNs using indexed columns only
    if dup_shas:
        sha_list = list(dup_shas)
        for i in range(0, len(sha_list), 3000):
            cids_s = (
                FileLocation.objects.filter(file_hash__in=sha_list[i : i + 3000], is_invalid=False)
                .order_by()
                .values_list('catalog_item_id', flat=True)
            )
            cand_item_ids.update(cids_s)

    if dup_raw_titles:
        t_list = list(dup_raw_titles)
        for i in range(0, len(t_list), 2500):
            cids_t = (
                CatalogItem.objects.filter(title__in=t_list[i : i + 2500])
                .order_by()
                .values_list('id', flat=True)
            )
            cand_item_ids.update(cids_t)

    if dup_isbns:
        i_list = list(dup_isbns)
        for i in range(0, len(i_list), 2500):
            cids_i = (
                CatalogItem.objects.filter(isbn__in=i_list[i : i + 2500])
                .order_by()
                .values_list('id', flat=True)
            )
            cand_item_ids.update(cids_i)

    if not cand_item_ids:
        return {'all_groups': [], 'groups_by_key': {}, 'summary': {}}

    # 3. Fetch all FileLocations and CatalogItems for candidate IDs (deferring base64 `cover_thumbnail` blobs!)
    cand_locs: List[FileLocation] = []
    cand_id_list = list(cand_item_ids)
    for i in range(0, len(cand_id_list), 2000):
        c_chunk = cand_id_list[i : i + 2000]
        cand_locs.extend(
            FileLocation.objects.filter(catalog_item_id__in=c_chunk, is_invalid=False)
            .select_related('catalog_item')
            .defer('catalog_item__cover_thumbnail')
            .order_by()
        )

    for fl in cand_locs:
        if fl.file_path and fl.file_path in enr_paths_set:
            enr_ids_set.add(fl.catalog_item_id)

    # 4. Bucket FileLocations by (canonical book key, file_format)
    buckets: Dict[Tuple[str, str], List[FileLocation]] = {}
    for fl in cand_locs:
        ci = fl.catalog_item
        if not ci:
            continue
        fmt = (fl.file_format or '').strip().upper()
        if not fmt:
            continue
        norm_t = _norm_lookup_str(ci.title)
        norm_a = _norm_lookup_str(ci.authors)
        clean_isbn = re.sub(r'[^0-9Xx]', '', str(ci.isbn or '')).upper()
        if norm_t and norm_a and norm_a not in ('unknown', 'unknown author'):
            book_key = f"ta:{norm_t}::{norm_a}"
        elif clean_isbn and len(clean_isbn) in (10, 13):
            book_key = f"isbn:{clean_isbn}"
        else:
            book_key = f"id:{ci.id}"
        buckets.setdefault((book_key, fmt), []).append(fl)

    # Also merge buckets that share an identical non-empty file_hash + format
    hash_to_bkey: Dict[Tuple[str, str], Tuple[str, str]] = {}
    merged_buckets: Dict[Tuple[str, str], List[FileLocation]] = {}
    for bkey, fl_list in buckets.items():
        fmt = bkey[1]
        target_bkey = bkey
        for fl in fl_list:
            if fl.file_hash:
                hk = (fl.file_hash, fmt)
                if hk in hash_to_bkey:
                    target_bkey = hash_to_bkey[hk]
                    break
        for fl in fl_list:
            if fl.file_hash:
                hash_to_bkey[(fl.file_hash, fmt)] = target_bkey
        existing = merged_buckets.setdefault(target_bkey, [])
        seen_fids = {x.id for x in existing}
        for fl in fl_list:
            if fl.id not in seen_fids:
                existing.append(fl)
                seen_fids.add(fl.id)

    all_groups: List[Dict[str, Any]] = []
    groups_by_key: Dict[str, Dict[str, Any]] = {}

    total_unfinished_bm_skipped = 0

    for (book_key, fmt), fl_list in merged_buckets.items():
        if len(fl_list) < 2:
            continue

        # Must belong to at least one enriched CatalogItem or enriched file_path
        has_enriched_member = any(
            (fl.catalog_item_id in enr_ids_set) or (fl.file_path in enr_paths_set)
            for fl in fl_list
        )
        if not has_enriched_member:
            continue

        # Sub-cluster files within `(book_key, fmt)` by actual content identity so that
        # two genuinely different books or volumes accidentally merged onto the same CatalogItem
        # are NOT treated as the same book content!
        # Two files belong to the same content cluster if:
        #   - They share any SHA-256 (`file_hash`, `orig_sha256`, `file_sha256`), OR
        #   - Their file sizes are identical (or within <= 5% OR <= 150 KB from epubmeta OPF/cover embedding)
        #     AND they do not have conflicting volume/edition markers in their filenames/orig_titles.
        clusters: List[List[FileLocation]] = []
        for fl in sorted(fl_list, key=lambda x: x.file_size or 0):
            fl_hashes = {h for h in (
                fl.file_hash,
                (orig_info_by_path.get(fl.file_path) or {}).get('orig_sha256'),
                (orig_info_by_path.get(fl.file_path) or {}).get('file_sha256'),
            ) if h}
            fl_vol = _extract_volume_or_edition_marker(
                f"{fl.file_name} {(orig_info_by_path.get(fl.file_path) or {}).get('orig_title', '')}"
            )
            fl_size = int(fl.file_size or 0)

            placed = False
            for cl in clusters:
                rep = cl[0]
                rep_hashes = set()
                for member in cl:
                    for h in (
                        member.file_hash,
                        (orig_info_by_path.get(member.file_path) or {}).get('orig_sha256'),
                        (orig_info_by_path.get(member.file_path) or {}).get('file_sha256'),
                    ):
                        if h:
                            rep_hashes.add(h)
                if fl_hashes and (fl_hashes & rep_hashes):
                    cl.append(fl)
                    placed = True
                    break

                rep_size = int(rep.file_size or 0)
                rep_vol = _extract_volume_or_edition_marker(
                    f"{rep.file_name} {(orig_info_by_path.get(rep.file_path) or {}).get('orig_title', '')}"
                )
                # If both have volume/edition markers and they differ (e.g. Vol 1 vs Vol 2), do not cluster!
                if fl_vol and rep_vol and fl_vol != rep_vol:
                    continue

                if fl_size > 0 and rep_size > 0:
                    size_diff = abs(fl_size - rep_size)
                    rel_diff = size_diff / float(max(fl_size, rep_size))
                    if size_diff == 0 or rel_diff <= 0.05 or size_diff <= 153600:
                        cl.append(fl)
                        placed = True
                        break

            if not placed:
                clusters.append([fl])

        for cl_idx, cl_files in enumerate(clusters):
            if len(cl_files) < 2:
                continue

            # Choose canonical CatalogItem (prefer the richest enriched item)
            items_in_cl = [f.catalog_item for f in cl_files if f.catalog_item]
            items_in_cl.sort(
                key=lambda it: (
                    it.id in enr_ids_set,
                    bool(it.description),
                    bool(it.isbn),
                    bool(it.publisher),
                ),
                reverse=True,
            )
            primary_item = items_in_cl[0]

            # Check if any item or file in this cluster has an unfinished Moon+ Reader bookmark
            has_unfinished_bookmark = False
            bookmark_pct_str = ''
            for f in cl_files:
                cid_s = str(f.catalog_item_id)
                if cid_s in unfinished_item_ids:
                    has_unfinished_bookmark = True
                    bookmark_pct_str = (moon_meta_by_id.get(cid_s) or {}).get('pct') or 'Bookmarked'
                    break
                fn_low = (f.file_name or '').lower()
                san_low = _moon_sanitize_stem(f.file_name or '').lower()
                if fn_low in unfinished_stems or san_low in unfinished_stems:
                    has_unfinished_bookmark = True
                    m_info = moon_stem_to_meta.get(fn_low) or moon_stem_to_meta.get(san_low) or {}
                    bookmark_pct_str = m_info.get('pct') or 'Bookmarked'
                    break

            if has_unfinished_bookmark:
                total_unfinished_bm_skipped += 1

            # Determine content match level
            all_shas_list = []
            for f in cl_files:
                f_shas = {h for h in (
                    f.file_hash,
                    (orig_info_by_path.get(f.file_path) or {}).get('orig_sha256'),
                    (orig_info_by_path.get(f.file_path) or {}).get('file_sha256'),
                ) if h}
                all_shas_list.append(f_shas)

            shared_sha = False
            for i in range(len(all_shas_list)):
                for j in range(i + 1, len(all_shas_list)):
                    if all_shas_list[i] & all_shas_list[j]:
                        shared_sha = True
                        break

            sizes = [int(f.file_size or 0) for f in cl_files]
            exact_size = len(set(sizes)) == 1 and sizes[0] > 0

            if shared_sha:
                content_match_type = 'EXACT_HASH'
                content_match_label = '100% Identical SHA-256'
            elif exact_size:
                content_match_type = 'EXACT_SIZE'
                content_match_label = '100% Identical Byte Size'
            else:
                max_sz, min_sz = max(sizes), min(sizes)
                diff_kb = round((max_sz - min_sz) / 1024.0, 1)
                content_match_type = 'SAME_CONTENT_OPF_DELTA'
                content_match_label = f'Same Book Content (±{diff_kb} KB OPF/Cover Delta)'

            # Determine timestamps range
            ts_vals = []
            for f in cl_files:
                dts = [dt for dt in (f.file_created_at, f.file_modified_at) if dt is not None]
                if dts:
                    ts_vals.append(min(dts).timestamp())
            oldest_ts = min(ts_vals) if ts_vals else None
            newest_ts = max(ts_vals) if ts_vals else None

            # Build metadata blob & group categories for evaluating the Category Directory (above the 3-letter folder)
            tags_combined = []
            cached_p2t = _TAGS_SHELF_CACHE.get('path_to_tags') or {}
            group_categories: set = set()
            for f in cl_files:
                tags_combined.extend(cached_p2t.get(f.file_path or '', []))
                d_comp = _parse_book_dir_components(f.file_path or '')
                if d_comp.get('category_dir'):
                    group_categories.add(d_comp['category_dir'])
            meta_blob = (
                f"{primary_item.title} {primary_item.authors or ''} "
                f"{primary_item.publisher or ''} {(primary_item.description or '')[:600]} "
                f"{' '.join(tags_combined)}"
            )

            scored_files = []
            for f in cl_files:
                sc_info = _score_duplicate_file_candidate(
                    f_loc=f,
                    item=primary_item,
                    group_categories=group_categories,
                    enriched_paths_set=enr_paths_set,
                    moon_stem_to_meta=moon_stem_to_meta,
                    oldest_ts=oldest_ts,
                    newest_ts=newest_ts,
                    meta_blob=meta_blob,
                )
                scored_files.append({
                    'file_id': str(f.id),
                    'catalog_item_id': str(f.catalog_item_id),
                    'file_name': f.file_name,
                    'file_path': f.file_path,
                    'file_format': fmt,
                    'file_size': int(f.file_size or 0),
                    'file_size_mb': round((f.file_size or 0) / (1024 * 1024), 2),
                    'file_hash': f.file_hash or '',
                    'file_hash_short': (f.file_hash or '')[:12],
                    'score': sc_info['score'],
                    'reasons': sc_info['reasons'],
                    'has_dup_suffix': sc_info['has_dup_suffix'],
                    'dup_suffix_str': sc_info['dup_suffix_str'],
                    'category_dir': sc_info['category_dir'],
                    'prefix_dir': sc_info['prefix_dir'],
                    'dir_fit_tier': sc_info['dir_fit_tier'],
                    'is_oldest': sc_info['is_oldest'],
                    'effective_ts': sc_info['effective_ts'],
                    'date_str': sc_info['date_str'],
                    'date_short': sc_info['date_short'],
                    'file_is_enriched': sc_info['file_is_enriched'],
                    'has_moon_bookmark': sc_info['has_moon_bookmark'],
                    'moon_pct': sc_info['moon_pct'],
                })

            # Sort files so the highest-scoring copy (Recommended Keep) is first!
            # Tie-breakers:
            #   1. Higher score
            #   2. No (#) suffix
            #   3. Older effective timestamp
            #   4. Shorter file_path (cleaner filename)
            scored_files.sort(
                key=lambda x: (
                    -x['score'],
                    x['has_dup_suffix'],
                    x['effective_ts'] if x['effective_ts'] is not None else float('inf'),
                    len(x['file_path'] or ''),
                    x['file_path'] or '',
                )
            )

            winner = scored_files[0]
            winner['recommended_keep'] = True
            for other in scored_files[1:]:
                other['recommended_keep'] = False

            reclaimable_bytes = sum(x['file_size'] for x in scored_files[1:])
            has_any_dup_suffix = any(x['has_dup_suffix'] for x in scored_files)
            all_files_enriched = all(x['file_is_enriched'] for x in scored_files)
            # Only flag `has_dir_difference` when copies live in DIFFERENT Category directories (above the 3-letter folder)
            has_dir_difference = len({x['category_dir'] for x in scored_files}) > 1

            group_key = f"{primary_item.id}_{fmt}_{cl_idx}"
            grp_obj = {
                'group_key': group_key,
                'item_id': str(primary_item.id),
                'title': primary_item.title,
                'authors': primary_item.authors,
                'publisher': primary_item.publisher or '',
                'pub_date': str(primary_item.pub_date) if primary_item.pub_date else '',
                'isbn': primary_item.isbn or '',
                'item_type': primary_item.item_type,
                'cover_url': primary_item.cover_image_url or f'/mylibrary/opds/cover/{primary_item.id}.jpg',
                'file_format': fmt,
                'copies_count': len(scored_files),
                'redundant_count': len(scored_files) - 1,
                'reclaimable_bytes': reclaimable_bytes,
                'reclaimable_mb': round(reclaimable_bytes / (1024 * 1024), 2),
                'content_match_type': content_match_type,
                'content_match_label': content_match_label,
                'has_dup_suffix': has_any_dup_suffix,
                'has_dir_difference': has_dir_difference,
                'all_files_enriched': all_files_enriched,
                'has_unfinished_bookmark': has_unfinished_bookmark,
                'bookmark_pct_str': bookmark_pct_str,
                'recommended_keep_file_id': winner['file_id'],
                'recommended_remove_file_ids': [x['file_id'] for x in scored_files[1:]],
                'score_margin': winner['score'] - scored_files[1]['score'],
                'files': scored_files,
            }
            all_groups.append(grp_obj)
            groups_by_key[group_key] = grp_obj

    # Sort duplicate groups so the clearest, highest-confidence wins appear first:
    #   1. Exact SHA-256 / Exact Size matches first
    #   2. Groups with (#) splitfiles suffix or clear directory winner (higher score_margin)
    #   3. Larger reclaimable space
    match_rank = {'EXACT_HASH': 0, 'EXACT_SIZE': 1, 'SAME_CONTENT_OPF_DELTA': 2}
    all_groups.sort(
        key=lambda g: (
            match_rank.get(g['content_match_type'], 9),
            not g['has_dup_suffix'],
            -g['score_margin'],
            -g['reclaimable_bytes'],
            g['title'].lower(),
        )
    )

    active_groups = [g for g in all_groups if not g['has_unfinished_bookmark']]
    total_redundant = sum(g['redundant_count'] for g in active_groups)
    total_reclaim_bytes = sum(g['reclaimable_bytes'] for g in active_groups)

    summary = {
        'total_groups': len(active_groups),
        'total_redundant_files': total_redundant,
        'reclaimable_bytes': total_reclaim_bytes,
        'reclaimable_mb': round(total_reclaim_bytes / (1024 * 1024), 2),
        'reclaimable_gb': round(total_reclaim_bytes / (1024 * 1024 * 1024), 2),
        'exact_match_groups': sum(1 for g in active_groups if g['content_match_type'] in ('EXACT_HASH', 'EXACT_SIZE')),
        'splitfiles_suffix_groups': sum(1 for g in active_groups if g['has_dup_suffix']),
        'dir_diff_groups': sum(1 for g in active_groups if g['has_dir_difference']),
        'all_files_enriched_groups': sum(1 for g in active_groups if g['all_files_enriched']),
        'excluded_unfinished_bookmarks': total_unfinished_bm_skipped,
    }

    _DUPLICATES_ENGINE_CACHE['expires_at'] = now + 1800.0
    _DUPLICATES_ENGINE_CACHE['all_groups'] = all_groups
    _DUPLICATES_ENGINE_CACHE['groups_by_key'] = groups_by_key
    _DUPLICATES_ENGINE_CACHE['summary'] = summary
    try:
        _DUPLICATES_SHARED_CACHE_FILE.write_text(
            json.dumps({'all_groups': all_groups, 'summary': summary}),
            encoding='utf-8',
        )
    except Exception:
        pass
    return _DUPLICATES_ENGINE_CACHE


def _remove_resolved_from_duplicates_cache(kept_file_id: str, removed_file_ids: List[str]) -> None:
    """
    Incrementally update `_DUPLICATES_ENGINE_CACHE` and `/tmp/mylibrary_duplicates_cache.json`
    after resolving a duplicate group so pagination and filtering remain instantaneous (<10ms).
    """
    rem_set = {str(x) for x in removed_file_ids if x}
    target_set = rem_set | ({str(kept_file_id)} if kept_file_id else set())
    if not target_set:
        return

    all_groups = _DUPLICATES_ENGINE_CACHE.get('all_groups')
    if not all_groups and _DUPLICATES_SHARED_CACHE_FILE.exists():
        try:
            shared_data = json.loads(_DUPLICATES_SHARED_CACHE_FILE.read_text(encoding='utf-8'))
            all_groups = shared_data.get('all_groups') or []
            _DUPLICATES_ENGINE_CACHE['summary'] = shared_data.get('summary') or {}
        except Exception:
            all_groups = None

    if not all_groups:
        invalidate_duplicates_cache()
        return

    new_groups = []
    for g in all_groups:
        g_fids = {str(f.get('file_id')) for f in (g.get('files') or [])}
        if not (g_fids & target_set):
            new_groups.append(g)
            continue
        surviving = [f for f in (g.get('files') or []) if str(f.get('file_id')) not in rem_set]
        if len(surviving) >= 2:
            g['files'] = surviving
            g['copies_count'] = len(surviving)
            g['redundant_count'] = len(surviving) - 1
            reclaim_b = sum(int(x.get('file_size') or 0) for x in surviving[1:])
            g['reclaimable_bytes'] = reclaim_b
            g['reclaimable_mb'] = round(reclaim_b / (1024 * 1024), 2)
            new_groups.append(g)

    active_groups = [g for g in new_groups if not g.get('has_unfinished_bookmark')]
    total_redundant = sum(g['redundant_count'] for g in active_groups)
    total_reclaim_bytes = sum(g['reclaimable_bytes'] for g in active_groups)
    old_summary = _DUPLICATES_ENGINE_CACHE.get('summary') or {}

    summary = {
        'total_groups': len(active_groups),
        'total_redundant_files': total_redundant,
        'reclaimable_bytes': total_reclaim_bytes,
        'reclaimable_mb': round(total_reclaim_bytes / (1024 * 1024), 2),
        'reclaimable_gb': round(total_reclaim_bytes / (1024 * 1024 * 1024), 2),
        'exact_match_groups': sum(1 for g in active_groups if g['content_match_type'] in ('EXACT_HASH', 'EXACT_SIZE')),
        'splitfiles_suffix_groups': sum(1 for g in active_groups if g['has_dup_suffix']),
        'dir_diff_groups': sum(1 for g in active_groups if g['has_dir_difference']),
        'all_files_enriched_groups': sum(1 for g in active_groups if g['all_files_enriched']),
        'excluded_unfinished_bookmarks': old_summary.get('excluded_unfinished_bookmarks', 0),
    }

    _DUPLICATES_ENGINE_CACHE['all_groups'] = new_groups
    _DUPLICATES_ENGINE_CACHE['groups_by_key'] = {g['group_key']: g for g in new_groups}
    _DUPLICATES_ENGINE_CACHE['summary'] = summary
    _DUPLICATES_ENGINE_CACHE['expires_at'] = time.monotonic() + 1800.0
    try:
        _DUPLICATES_SHARED_CACHE_FILE.write_text(
            json.dumps({'all_groups': new_groups, 'summary': summary}),
            encoding='utf-8',
        )
    except Exception:
        pass


def get_enriched_duplicate_groups(
    filter_mode: str = 'all',
    format_filter: str = '',
    search_q: str = '',
    exclude_unfinished_bookmarks: bool = True,
    require_all_files_enriched: bool = False,
    page: int = 1,
    page_size: int = 25,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """
    Return filtered, paginated enriched duplicate groups and summary statistics.
    """
    if force_refresh:
        invalidate_duplicates_cache()
    data = _build_all_enriched_duplicate_groups()
    groups = list(data.get('all_groups') or [])
    summary = dict(data.get('summary') or {})

    if exclude_unfinished_bookmarks:
        groups = [g for g in groups if not g.get('has_unfinished_bookmark')]

    if require_all_files_enriched:
        groups = [g for g in groups if g.get('all_files_enriched')]

    mode = (filter_mode or 'all').strip().lower()
    if mode == 'exact':
        groups = [g for g in groups if g['content_match_type'] in ('EXACT_HASH', 'EXACT_SIZE')]
    elif mode == 'suffix':
        groups = [g for g in groups if g['has_dup_suffix']]
    elif mode == 'dir_fit':
        groups = [g for g in groups if g['has_dir_difference']]
    elif mode == 'bookmarked':
        groups = [g for g in (data.get('all_groups') or []) if g.get('has_unfinished_bookmark')]

    fmt = (format_filter or '').strip().upper()
    if fmt and fmt != 'ALL':
        groups = [g for g in groups if g['file_format'] == fmt]

    q = (search_q or '').strip().lower()
    if q:
        filtered = []
        for g in groups:
            haystack = f"{g['title']} {g['authors']} {g['isbn']} {g['publisher']} " + ' '.join(
                f"{f['file_name']} {f['file_path']}" for f in g['files']
            )
            if q in haystack.lower():
                filtered.append(g)
        groups = filtered

    total_filtered = len(groups)
    total_filtered_redundant = sum(g['redundant_count'] for g in groups)
    total_filtered_reclaim_mb = round(sum(g['reclaimable_bytes'] for g in groups) / (1024 * 1024), 2)

    page = max(1, int(page or 1))
    page_size = max(1, min(100, int(page_size or 25)))
    total_pages = max(1, (total_filtered + page_size - 1) // page_size)
    if page > total_pages:
        page = total_pages
    start_idx = (page - 1) * page_size
    page_groups = groups[start_idx : start_idx + page_size]

    return {
        'ok': True,
        'summary': summary,
        'filtered_groups_count': total_filtered,
        'filtered_redundant_files': total_filtered_redundant,
        'filtered_reclaimable_mb': total_filtered_reclaim_mb,
        'page': page,
        'page_size': page_size,
        'total_pages': total_pages,
        'filter_mode': mode,
        'format_filter': fmt,
        'search_q': search_q,
        'exclude_unfinished_bookmarks': exclude_unfinished_bookmarks,
        'require_all_files_enriched': require_all_files_enriched,
        'groups': page_groups,
    }


def resolve_enriched_duplicate_group(
    keep_file_id: str,
    remove_file_ids: List[str],
    allow_unfinished_bookmark: bool = False,
) -> Dict[str, Any]:
    """
    Safely resolve a duplicate group by:
      1. Verifying that `keep_file_id` exists on disk and is non-empty.
      2. Checking that none of the files being removed have an unfinished Moon+ Reader bookmark.
      3. Ensuring `keep_file_id` inherits `status = 'SUCCESS'` in `scanner_state.db` if a removed copy was enriched.
      4. Moving the redundant physical file(s) to `/books_bad/duplicates/` (quarantine backup) and removing
         their `FileLocation` rows from BookDB and `scanner_state.db`.
    """
    if not keep_file_id or not remove_file_ids:
        return {'ok': False, 'error': 'Both keep_file_id and remove_file_ids are required.'}

    keep_loc = FileLocation.objects.select_related('catalog_item').filter(id=keep_file_id).first()
    if not keep_loc:
        return {'ok': False, 'error': f'Keep FileLocation {keep_file_id} not found in BookDB.'}

    keep_phys = _resolve_physical_file_path(keep_loc.file_path)
    if not keep_phys or not keep_phys.exists() or keep_phys.stat().st_size <= 0:
        return {
            'ok': False,
            'error': f'Safety stop: The selected Keep file does not exist on disk ({keep_loc.file_path}). Refusing to remove duplicates.',
        }

    rem_ids_clean = [ str(rid) for rid in remove_file_ids if str(rid) != str(keep_loc.id) ]
    if not rem_ids_clean:
        return {'ok': False, 'error': 'No duplicate files specified to remove.'}

    rem_locs = list(FileLocation.objects.select_related('catalog_item').filter(id__in=rem_ids_clean))
    if not rem_locs:
        return {'ok': False, 'error': 'Target duplicate FileLocation records were already removed.'}

    # Verify same format
    keep_fmt = (keep_loc.file_format or '').strip().upper()
    for rl in rem_locs:
        if (rl.file_format or '').strip().upper() != keep_fmt:
            return {
                'ok': False,
                'error': f'Safety stop: Format mismatch ({rl.file_format} vs {keep_fmt}). Only identical formats can be deduplicated.',
            }

    # Check unfinished bookmark protection
    if not allow_unfinished_bookmark:
        bm_shelf = get_dynamic_bookmarked_shelf()
        moon_meta_by_id = bm_shelf.get('moon_meta_by_id') or {}
        for check_loc in [keep_loc] + rem_locs:
            m_meta = moon_meta_by_id.get(str(check_loc.catalog_item_id))
            if m_meta and float(m_meta.get('pct_val') or 0.0) < 99.5:
                return {
                    'ok': False,
                    'error': f"Safety stop: '{check_loc.catalog_item.title}' has an unfinished Moon+ Reader bookmark ({m_meta.get('pct')}).",
                }

    quarantine_dir = _get_duplicates_quarantine_dir()
    keep_item = keep_loc.catalog_item
    removed_files_info: List[Dict[str, Any]] = []
    reclaimed_bytes = 0

    db_path = _get_scanner_db_path()
    conn = None
    if is_postgres_scanner_enabled() or db_path.exists():
        try:
            conn = connect_scanner_db(db_path, timeout=10.0)
            conn.row_factory = sqlite3.Row
        except Exception:
            conn = None

    try:
        # Ensure `keep_loc.file_path` has a `SUCCESS` enrichment record in `processed_files`
        if conn is not None:
            try:
                cur = conn.cursor()
                keep_row = cur.execute(
                    "SELECT status FROM processed_files WHERE file_path = ? LIMIT 1",
                    (keep_loc.file_path,),
                ).fetchone()
                if not keep_row or (keep_row['status'] or '').upper() != 'SUCCESS':
                    # Copy enrichment row from one of the duplicate files if present
                    donor_row = None
                    for rl in rem_locs:
                        donor_row = cur.execute(
                            "SELECT * FROM processed_files WHERE file_path = ? AND status = 'SUCCESS' LIMIT 1",
                            (rl.file_path,),
                        ).fetchone()
                        if donor_row:
                            break
                    if donor_row:
                        cur.execute("DELETE FROM processed_files WHERE file_path = ?", (keep_loc.file_path,))
                        cur.execute(
                            """
                            INSERT INTO processed_files (
                                file_path, file_name, file_ext, file_size, file_mtime,
                                status, isbn, title, author, orig_title, orig_author, orig_isbn,
                                match_confidence, original_metadata_json, updated_metadata_json,
                                cover_thumbnail, updated_at
                            ) VALUES (?, ?, ?, ?, ?, 'SUCCESS', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                            """,
                            (
                                keep_loc.file_path,
                                keep_loc.file_name,
                                os.path.splitext(keep_loc.file_name or '')[1].lower(),
                                keep_loc.file_size,
                                keep_phys.stat().st_mtime,
                                donor_row['isbn'] if 'isbn' in donor_row.keys() else (keep_item.isbn or ''),
                                donor_row['title'] if 'title' in donor_row.keys() else keep_item.title,
                                donor_row['author'] if 'author' in donor_row.keys() else keep_item.authors,
                                donor_row['orig_title'] if 'orig_title' in donor_row.keys() else keep_item.title,
                                donor_row['orig_author'] if 'orig_author' in donor_row.keys() else keep_item.authors,
                                donor_row['orig_isbn'] if 'orig_isbn' in donor_row.keys() else (keep_item.isbn or ''),
                                donor_row['match_confidence'] if 'match_confidence' in donor_row.keys() else 1.0,
                                donor_row['original_metadata_json'] if 'original_metadata_json' in donor_row.keys() else '{}',
                                donor_row['updated_metadata_json'] if 'updated_metadata_json' in donor_row.keys() else '{}',
                                donor_row['cover_thumbnail'] if 'cover_thumbnail' in donor_row.keys() else (keep_item.cover_thumbnail or ''),
                            ),
                        )
                        conn.commit()
                if isinstance(_ENRICHED_PATHS_CACHE.get('paths_set'), set):
                    _ENRICHED_PATHS_CACHE['paths_set'].add(keep_loc.file_path)
            except Exception:
                pass

        for rl in rem_locs:
            rem_phys = _resolve_physical_file_path(rl.file_path)
            f_size = int(rl.file_size or 0)
            q_dest_str = ''

            if rem_phys and rem_phys.exists():
                # Double check we are not moving the exact same physical inode/path as keep_phys!
                if rem_phys.resolve() != keep_phys.resolve():
                    if quarantine_dir:
                        stem, ext = os.path.splitext(rem_phys.name)
                        short_id = str(rl.id)[:8]
                        q_dest = quarantine_dir / f"{stem}__dup_{short_id}{ext}"
                        shutil.move(str(rem_phys), str(q_dest))
                        q_dest_str = str(q_dest)
                    else:
                        rem_phys.unlink()
                        q_dest_str = 'deleted'

            # Clean up scanner_state.db rows for the removed path
            if conn is not None:
                try:
                    for p_var in (
                        rl.file_path,
                        '/volume1/common/Media/Books/' + rl.file_path[len('/books/'):] if (rl.file_path or '').startswith('/books/') else '',
                        '/books/' + rl.file_path[len('/volume1/common/Media/Books/'):] if (rl.file_path or '').startswith('/volume1/common/Media/Books/') else '',
                    ):
                        if p_var:
                            conn.execute("DELETE FROM processed_files WHERE file_path = ?", (p_var,))
                            conn.execute("DELETE FROM file_states WHERE filepath = ?", (p_var,))
                            if isinstance(_ENRICHED_PATHS_CACHE.get('paths_set'), set):
                                _ENRICHED_PATHS_CACHE['paths_set'].discard(p_var)
                    conn.commit()
                except Exception:
                    pass

            other_item = rl.catalog_item
            old_fpath = rl.file_path
            old_fname = rl.file_name
            rl.delete()
            reclaimed_bytes += f_size

            # If the removed file belonged to a separate CatalogItem, preserve any richer metadata and delete orphan item
            if other_item and other_item.id != keep_item.id:
                keep_updated = False
                for field in ('isbn', 'asin', 'publisher', 'pub_date', 'description', 'cover_thumbnail', 'cover_image_url'):
                    if not getattr(keep_item, field, None) and getattr(other_item, field, None):
                        setattr(keep_item, field, getattr(other_item, field))
                        keep_updated = True
                if keep_updated:
                    keep_item.save()
                if not FileLocation.objects.filter(catalog_item_id=other_item.id).exists():
                    other_item.delete()

            removed_files_info.append({
                'file_id': str(rl.id),
                'file_name': old_fname,
                'file_path': old_fpath,
                'file_size': f_size,
                'quarantined_to': q_dest_str,
            })
    finally:
        if conn is not None:
            conn.close()

    _remove_resolved_from_duplicates_cache(
        kept_file_id=str(keep_loc.id),
        removed_file_ids=[x['file_id'] for x in removed_files_info],
    )

    return {
        'ok': True,
        'kept_file_id': str(keep_loc.id),
        'kept_file_path': keep_loc.file_path,
        'kept_file_name': keep_loc.file_name,
        'item_id': str(keep_item.id),
        'title': keep_item.title,
        'removed_count': len(removed_files_info),
        'removed_files': removed_files_info,
        'reclaimed_bytes': reclaimed_bytes,
        'reclaimed_mb': round(reclaimed_bytes / (1024 * 1024), 2),
    }


