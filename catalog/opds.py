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

from .models import CatalogItem, FileLocation, format_normalized_authors_str
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
        Path('/toMags'),
        Path('/volume1/common/Media/Books'),
        Path('/volume1/common/Media/Audiobooks'),
        Path('/volume1/common/Media/toMags'),
        Path(r'U:\Media\Books'),
        Path(r'U:\Media\Audiobooks'),
        Path(r'U:\Media\toMags'),
    ]
    candidates = [Path(raw)]
    if raw.startswith('/books/'):
        rel = raw[len('/books/'):]
        candidates.append(Path('/volume1/common/Media/Books') / rel)
        candidates.append(Path(r'U:\Media\Books') / rel)
    elif raw.startswith('/audiobooks/'):
        rel = raw[len('/audiobooks/'):]
        candidates.append(Path('/volume1/common/Media/Audiobooks') / rel)
        candidates.append(Path(r'U:\Media\Audiobooks') / rel)
    elif raw.startswith('/toMags/'):
        rel = raw[len('/toMags/'):]
        candidates.append(Path('/volume1/common/Media/toMags') / rel)
        candidates.append(Path(r'U:\Media\toMags') / rel)

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

    # Fallback 2: check if a `/books/...` file was moved to `/toMags/<filename>`
    if fname and raw.startswith('/books/'):
        tomags_dir = _get_tomags_dir()
        if tomags_dir:
            tm_cand = tomags_dir / fname
            try:
                if tm_cand.exists() and tm_cand.is_file():
                    resolved_tm = tm_cand.resolve()
                    try:
                        FileLocation.objects.filter(file_path=raw).update(file_path=f'/toMags/{fname}')
                    except Exception:
                        pass
                    return resolved_tm
            except Exception:
                pass

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


def _get_tags_cache_disk_paths() -> List[Path]:
    paths: List[Path] = []
    for d in (Path('/books_bad'), Path('/volume1/common/Media/Books_bad'), Path(r'U:\Media\Books_bad')):
        try:
            if d.exists() and d.is_dir():
                paths.append(d / '.tags_shelf_cache.json')
                break
        except Exception:
            continue
    paths.append(Path('/tmp/mylibrary_tags_shelf_cache.json'))
    return paths


def _get_tags_counts_cache_disk_paths() -> List[Path]:
    paths: List[Path] = []
    for d in (Path('/books_bad'), Path('/volume1/common/Media/Books_bad'), Path(r'U:\Media\Books_bad')):
        try:
            if d.exists() and d.is_dir():
                paths.append(d / '.tags_counts_cache.json')
                break
        except Exception:
            continue
    paths.append(Path('/tmp/mylibrary_tags_counts_cache.json'))
    return paths


def _load_tags_shelf_cache_from_disk(need_paths: bool = False) -> bool:
    if not need_paths and not _TAGS_SHELF_CACHE.get('tag_counts'):
        for cp in _get_tags_counts_cache_disk_paths():
            try:
                if cp.exists() and cp.stat().st_size < 1024 * 1024:
                    cdata = json.loads(cp.read_text(encoding='utf-8'))
                    tc = [tuple(x) for x in (cdata.get('tag_counts') or ())]
                    if tc:
                        fc = [tuple(x) for x in (cdata.get('folder_counts') or ())]
                        _TAGS_SHELF_CACHE['expires_at'] = time.monotonic() + 7200.0
                        _TAGS_SHELF_CACHE['tag_counts'] = tc
                        _TAGS_SHELF_CACHE['folder_counts'] = fc
                        _TAGS_SHELF_CACHE['_refreshing'] = False
                        return True
            except Exception:
                continue

    for p in _get_tags_cache_disk_paths():
        try:
            if not p.exists() or p.stat().st_size > 15 * 1024 * 1024:
                continue
            data = json.loads(p.read_text(encoding='utf-8'))
            tag_counts = [tuple(x) for x in (data.get('tag_counts') or ())]
            if not tag_counts:
                continue
            folder_counts = [tuple(x) for x in (data.get('folder_counts') or ())]
            tag_to_paths = data.get('tag_to_paths') or {}
            _TAGS_SHELF_CACHE['expires_at'] = time.monotonic() + 7200.0
            _TAGS_SHELF_CACHE['tag_counts'] = tag_counts
            if need_paths and tag_to_paths:
                _TAGS_SHELF_CACHE['tag_to_paths'] = tag_to_paths
            _TAGS_SHELF_CACHE['tag_to_titles'] = {}
            _TAGS_SHELF_CACHE['path_to_tags'] = {}
            _TAGS_SHELF_CACHE['folder_counts'] = folder_counts
            _TAGS_SHELF_CACHE['folder_to_paths'] = {}
            _TAGS_SHELF_CACHE['folder_to_titles'] = {}
            _TAGS_SHELF_CACHE['_refreshing'] = False
            try:
                counts_raw = json.dumps({'tag_counts': tag_counts[:500], 'folder_counts': folder_counts}, ensure_ascii=False)
                for cp in _get_tags_counts_cache_disk_paths():
                    try:
                        if not cp.exists():
                            cp.write_text(counts_raw, encoding='utf-8')
                    except Exception:
                        continue
            except Exception:
                pass
            return True
        except Exception:
            continue
    return False


def get_enriched_tags_and_folders_index(need_paths: bool = False) -> Dict[str, Any]:
    """
    Build and cache (for 7200s with stale-while-revalidate) the dynamic virtual bookshelves of:
      1. `tag_counts` / `tag_to_paths`: Enriched subject tags from `processed_files.updated_metadata_json`
      2. `folder_counts`: Top-level library categories for enriched books
    """
    now = time.monotonic()
    if need_paths and not _TAGS_SHELF_CACHE.get('tag_to_paths'):
        _load_tags_shelf_cache_from_disk(need_paths=True)
    if _TAGS_SHELF_CACHE['tag_counts'] and now < _TAGS_SHELF_CACHE['expires_at']:
        return _TAGS_SHELF_CACHE

    if _TAGS_SHELF_CACHE['tag_counts'] and not _TAGS_SHELF_CACHE.get('_refreshing'):
        _TAGS_SHELF_CACHE['_refreshing'] = True
        _TAGS_SHELF_CACHE['expires_at'] = now + 7200.0
        import threading
        threading.Thread(target=_rebuild_tags_shelf_cache, daemon=True).start()
        return _TAGS_SHELF_CACHE

    if _load_tags_shelf_cache_from_disk(need_paths=need_paths):
        return _TAGS_SHELF_CACHE

    return _rebuild_tags_shelf_cache()


def _rebuild_tags_shelf_cache() -> Dict[str, Any]:
    db_path = _get_scanner_db_path()
    tag_to_paths: Dict[str, List[str]] = {}
    folder_counts_map: Dict[str, int] = {}

    if is_postgres_scanner_enabled() or db_path.exists():
        conn = None
        try:
            conn = connect_scanner_db(db_path, timeout=10.0)
            conn.text_factory = lambda b: b.decode('utf-8', errors='replace')
            cur = conn.execute(
                """
                SELECT file_path, updated_metadata_json
                FROM processed_files
                WHERE status = 'SUCCESS'
                ORDER BY updated_at DESC
                """
            )
            while True:
                batch = cur.fetchmany(2500)
                if not batch:
                    break
                for row in batch:
                    fp, raw_json = row[0], row[1]
                    if not fp:
                        continue
                    norm_rel = str(fp).replace('\\', '/')
                    for pfx in ('/volume1/common/Media/Books/', '/books/'):
                        if norm_rel.startswith(pfx):
                            norm_rel = norm_rel[len(pfx):]
                            break
                    parts = [p for p in norm_rel.split('/') if p]
                    if len(parts) >= 2:
                        folder_name = parts[0].strip()
                        folder_counts_map[folder_name] = folder_counts_map.get(folder_name, 0) + 1

                    if raw_json:
                        try:
                            meta = json.loads(raw_json)
                            raw_subjs = meta.get('subjects') or meta.get('tags') or meta.get('categories') or []
                            if isinstance(raw_subjs, list):
                                seen_for_book = set()
                                for s in raw_subjs:
                                    if not isinstance(s, str):
                                        continue
                                    clean_tag = s.strip()
                                    if len(clean_tag) < 2 or len(clean_tag) > 65:
                                        continue
                                    tag_key = clean_tag
                                    if tag_key.lower() not in seen_for_book:
                                        seen_for_book.add(tag_key.lower())
                                        tag_to_paths.setdefault(tag_key, []).append(str(fp))
                        except Exception:
                            pass
        except Exception:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    tag_counts = sorted(
        [(tag, len(paths)) for tag, paths in tag_to_paths.items() if len(paths) >= 2],
        key=lambda x: (-x[1], x[0].lower()),
    )
    folder_counts = sorted(
        [(fld, cnt) for fld, cnt in folder_counts_map.items()],
        key=lambda x: (-x[1], x[0].lower()),
    )

    top_tags_set = {t for t, _ in tag_counts[:250]}
    compact_tag_to_paths = {
        t: paths[:500]
        for t, paths in tag_to_paths.items()
        if t in top_tags_set
    }

    _TAGS_SHELF_CACHE['expires_at'] = time.monotonic() + 7200.0
    _TAGS_SHELF_CACHE['tag_counts'] = tag_counts[:500]
    _TAGS_SHELF_CACHE['tag_to_paths'] = compact_tag_to_paths
    _TAGS_SHELF_CACHE['tag_to_titles'] = {}
    _TAGS_SHELF_CACHE['path_to_tags'] = {}
    _TAGS_SHELF_CACHE['folder_counts'] = folder_counts
    _TAGS_SHELF_CACHE['folder_to_paths'] = {}
    _TAGS_SHELF_CACHE['folder_to_titles'] = {}
    _TAGS_SHELF_CACHE['_refreshing'] = False
    try:
        counts_only_raw = json.dumps({
            'tag_counts': tag_counts[:500],
            'folder_counts': folder_counts,
        }, ensure_ascii=False)
        for cp in _get_tags_counts_cache_disk_paths():
            try:
                tmp_cp = cp.with_suffix(f'.tmp.{os.getpid()}')
                tmp_cp.write_text(counts_only_raw, encoding='utf-8')
                tmp_cp.replace(cp)
            except Exception:
                continue
        raw_tags_cache = json.dumps({
            'tag_counts': tag_counts[:500],
            'folder_counts': folder_counts,
            'tag_to_paths': compact_tag_to_paths,
        }, ensure_ascii=False)
        for p in _get_tags_cache_disk_paths():
            try:
                tmp_p = p.with_suffix(f'.tmp.{os.getpid()}')
                tmp_p.write_text(raw_tags_cache, encoding='utf-8')
                tmp_p.replace(p)
            except Exception:
                continue
    except Exception:
        pass
    return _TAGS_SHELF_CACHE


def get_file_tags(file_path: str) -> List[str]:
    """
    Return the enriched subject/tag list for `file_path` from `scanner_state.db` / PostgreSQL `processed_files`.
    Queries `processed_files` directly by indexed `file_path` (`< 0.5ms`) instead of keeping a 78,000-entry dict in RAM.
    """
    if not file_path:
        return []
    norm = str(file_path).strip().replace('\\', '/')
    path_cands = [norm]
    if norm.startswith('/books/'):
        path_cands.append('/volume1/common/Media/Books/' + norm[len('/books/'):])
    elif norm.startswith('/volume1/common/Media/Books/'):
        path_cands.append('/books/' + norm[len('/volume1/common/Media/Books/'):])
    db_path = _get_scanner_db_path()
    if not (is_postgres_scanner_enabled() or db_path.exists()):
        return []
    conn = None
    try:
        conn = connect_scanner_db(db_path, timeout=3.0)
        ph = ','.join(['?'] * len(path_cands))
        row = conn.execute(
            f"SELECT updated_metadata_json FROM processed_files WHERE file_path IN ({ph}) AND status = 'SUCCESS' LIMIT 1",
            path_cands,
        ).fetchone()
        if row and row[0]:
            meta = json.loads(row[0])
            raw_subjs = meta.get('subjects') or meta.get('tags') or meta.get('categories') or []
            if isinstance(raw_subjs, list):
                out = []
                seen = set()
                for s in raw_subjs:
                    if isinstance(s, str):
                        c = s.strip()
                        if 2 <= len(c) <= 65 and c.lower() not in seen:
                            seen.add(c.lower())
                            out.append(c)
                return out
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return []


def _get_bookmarked_cache_disk_paths() -> List[Path]:
    paths: List[Path] = []
    for d in (Path('/books_bad'), Path('/volume1/common/Media/Books_bad'), Path(r'U:\Media\Books_bad')):
        try:
            if d.exists() and d.is_dir():
                paths.append(d / '.bookmarked_shelf_cache.json')
                break
        except Exception:
            continue
    paths.append(Path('/tmp/mylibrary_bookmarked_shelf_cache.json'))
    return paths


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

    if not _BOOKMARKED_SHELF_CACHE['item_ids']:
        import uuid as _uuid
        from types import SimpleNamespace as _NS
        for p in _get_bookmarked_cache_disk_paths():
            try:
                if not p.exists():
                    continue
                data = json.loads(p.read_text(encoding='utf-8'))
                if float(data.get('dir_mtime') or -1.0) != dir_mtime:
                    continue
                raw_ids = data.get('item_ids') or []
                moon_meta_by_id = data.get('moon_meta_by_id') or {}
                if not raw_ids or not moon_meta_by_id:
                    continue
                item_ids = [_uuid.UUID(str(x)) for x in raw_ids]
                items_with_meta = [(_NS(id=cid), moon_meta_by_id.get(str(cid), {})) for cid in item_ids]
                _BOOKMARKED_SHELF_CACHE['expires_at'] = now + 1800.0
                _BOOKMARKED_SHELF_CACHE['dir_mtime'] = dir_mtime
                _BOOKMARKED_SHELF_CACHE['items_with_meta'] = items_with_meta
                _BOOKMARKED_SHELF_CACHE['item_ids'] = item_ids
                _BOOKMARKED_SHELF_CACHE['moon_meta_by_id'] = moon_meta_by_id
                _BOOKMARKED_SHELF_CACHE['total_moon_stems'] = int(data.get('total_moon_stems') or len(item_ids))
                return _BOOKMARKED_SHELF_CACHE
            except Exception:
                continue

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
    try:
        raw_bm = json.dumps({
            'dir_mtime': dir_mtime,
            'item_ids': [str(x) for x in item_ids],
            'moon_meta_by_id': moon_meta_by_id,
            'total_moon_stems': len(stems),
        }, ensure_ascii=False)
        for p in _get_bookmarked_cache_disk_paths():
            try:
                tmp_p = p.with_suffix(f'.tmp.{os.getpid()}')
                tmp_p.write_text(raw_bm, encoding='utf-8')
                tmp_p.replace(p)
            except Exception:
                continue
    except Exception:
        pass
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
                .order_by()
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
                .order_by()
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
                .order_by()
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
                .order_by()
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
                .order_by()
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
    if rows:
        _save_enriched_cache_to_disk()


def _get_enriched_cache_disk_paths() -> List[Path]:
    paths: List[Path] = []
    for d in (Path('/books_bad'), Path('/volume1/common/Media/Books_bad'), Path(r'U:\Media\Books_bad')):
        try:
            if d.exists() and d.is_dir():
                paths.append(d / '.enriched_ids_cache.json')
                break
        except Exception:
            continue
    paths.append(Path('/tmp/mylibrary_enriched_ids_cache.json'))
    return paths


def _flush_enriched_cache_to_disk() -> None:
    try:
        canon_paths = [
            p for p in (_ENRICHED_PATHS_CACHE.get('paths_set') or ())
            if not str(p).startswith('/volume1/common/Media/Books/')
        ]
        canon_non_succ_paths = [
            p for p in (_ENRICHED_PATHS_CACHE.get('non_success_paths_set') or ())
            if not str(p).startswith('/volume1/common/Media/Books/')
        ]
        enr_set = _ENRICHED_PATHS_CACHE.get('enriched_item_ids_set') or set(_ENRICHED_PATHS_CACHE.get('enriched_item_ids') or ())
        excl_set = _ENRICHED_PATHS_CACHE.get('excluded_from_needs_enriching_ids_set') or set(_ENRICHED_PATHS_CACHE.get('excluded_from_needs_enriching_ids') or ())
        non_success_only = [str(x) for x in (excl_set - enr_set)]
        payload = {
            'db_mtime': _ENRICHED_PATHS_CACHE.get('db_mtime', 0.0),
            'last_updated_at': _ENRICHED_PATHS_CACHE.get('last_updated_at', ''),
            'paths_list': canon_paths,
            'non_success_paths_list': canon_non_succ_paths,
            'enriched_item_ids': [str(x) for x in (_ENRICHED_PATHS_CACHE.get('enriched_item_ids') or ())],
            'needs_review_item_ids': [str(x) for x in (_ENRICHED_PATHS_CACHE.get('needs_review_item_ids') or ())],
            'strict_in_review_ids': [str(x) for x in (_ENRICHED_PATHS_CACHE.get('strict_in_review_ids') or ())],
            'non_success_item_ids': non_success_only,
        }
        raw = json.dumps(payload, ensure_ascii=False)
        for p in _get_enriched_cache_disk_paths():
            try:
                tmp_p = p.with_suffix(f'.tmp.{os.getpid()}')
                tmp_p.write_text(raw, encoding='utf-8')
                tmp_p.replace(p)
            except Exception:
                continue
        _ENRICHED_PATHS_CACHE['last_disk_save_at'] = time.monotonic()
    except Exception:
        pass
    finally:
        _ENRICHED_PATHS_CACHE['_disk_saving'] = False


def _save_enriched_cache_to_disk(force: bool = False) -> None:
    if force:
        _flush_enriched_cache_to_disk()
        return
    if _ENRICHED_PATHS_CACHE.get('_disk_saving'):
        return
    now_m = time.monotonic()
    if now_m - _ENRICHED_PATHS_CACHE.get('last_disk_save_at', 0.0) < 90.0:
        return
    _ENRICHED_PATHS_CACHE['_disk_saving'] = True
    import threading

    def _delayed_save():
        time.sleep(3.0)
        _flush_enriched_cache_to_disk()

    threading.Thread(target=_delayed_save, daemon=True).start()


def _load_enriched_cache_from_disk() -> bool:
    import uuid as _uuid
    _ensure_filelocation_path_index()
    for p in _get_enriched_cache_disk_paths():
        try:
            if not p.exists():
                continue
            if p.stat().st_size > 128 * 1024 * 1024:
                continue
            data = json.loads(p.read_text(encoding='utf-8'))
            raw_paths = data.get('paths_list') or []
            if len(raw_paths) < 100:
                continue
            paths_set = set(raw_paths)
            if 'non_success_paths_list' in data:
                non_succ_paths_set = set(data.get('non_success_paths_list') or ())
            else:
                non_succ_paths_set = set()
                try:
                    db_path = _get_scanner_db_path()
                    conn = connect_scanner_db(db_path, timeout=3.0)
                    conn.text_factory = lambda b: b.decode('utf-8', errors='replace')
                    for r in conn.execute("SELECT file_path FROM processed_files WHERE status != 'SUCCESS'").fetchall():
                        if r and r[0]:
                            p_str = str(r[0])
                            if p_str not in paths_set:
                                non_succ_paths_set.add(p_str)
                    conn.close()
                except Exception:
                    pass
            enr_ids = [_uuid.UUID(str(x)) for x in (data.get('enriched_item_ids') or ())]
            rev_ids = [_uuid.UUID(str(x)) for x in (data.get('needs_review_item_ids') or ())]
            strict_rev_ids = [_uuid.UUID(str(x)) for x in (data.get('strict_in_review_ids') or ())]
            if 'non_success_item_ids' in data:
                non_succ_ids = [_uuid.UUID(str(x)) for x in (data.get('non_success_item_ids') or ())]
                excl_set = set(enr_ids) | set(non_succ_ids) | set(rev_ids)
                excl_ids = list(excl_set)
            else:
                excl_raw = [_uuid.UUID(str(x)) for x in (data.get('excluded_from_needs_enriching_ids') or ())]
                excl_set = set(enr_ids) | set(excl_raw) | set(rev_ids)
                excl_ids = list(excl_set)
            _ENRICHED_PATHS_CACHE['db_mtime'] = float(data.get('db_mtime') or 0.0)
            _ENRICHED_PATHS_CACHE['last_updated_at'] = str(data.get('last_updated_at') or '')
            _ENRICHED_PATHS_CACHE['paths_set'] = paths_set
            _ENRICHED_PATHS_CACHE['non_success_paths_set'] = non_succ_paths_set
            _ENRICHED_PATHS_CACHE['non_success_paths_loaded'] = True
            _ENRICHED_PATHS_CACHE['status_by_path'] = {}
            _ENRICHED_PATHS_CACHE['enriched_item_ids'] = enr_ids
            _ENRICHED_PATHS_CACHE['enriched_item_ids_set'] = set(enr_ids)
            _ENRICHED_PATHS_CACHE['needs_review_item_ids'] = rev_ids
            _ENRICHED_PATHS_CACHE['needs_review_item_ids_set'] = set(rev_ids)
            _ENRICHED_PATHS_CACHE['strict_in_review_ids'] = strict_rev_ids
            _ENRICHED_PATHS_CACHE['strict_in_review_ids_set'] = set(strict_rev_ids)
            _ENRICHED_PATHS_CACHE['excluded_from_needs_enriching_ids'] = excl_ids
            _ENRICHED_PATHS_CACHE['excluded_from_needs_enriching_ids_set'] = excl_set
            _ENRICHED_PATHS_CACHE['expires_at'] = time.monotonic() + 3600.0
            _ENRICHED_PATHS_CACHE['last_disk_save_at'] = time.monotonic()
            _ENRICHED_PATHS_CACHE['_refreshing'] = False
            _sync_incremental_enriched_changes()
            return True
        except Exception:
            continue
    return False


_FILELOCATION_PATH_INDEX_READY = False


def _ensure_search_trigram_indexes_bg() -> None:
    try:
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_catalogitem_title_trgm ON catalog_catalogitem USING gin (title gin_trgm_ops)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_catalogitem_authors_trgm ON catalog_catalogitem USING gin (authors gin_trgm_ops)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_filelocation_filename_trgm ON catalog_filelocation USING gin (file_name gin_trgm_ops)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_processed_files_file_path ON processed_files (file_path)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_processed_files_updated_at ON processed_files (updated_at)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_processed_files_status ON processed_files (status)"
            )
    except Exception:
        pass


def _ensure_filelocation_path_index() -> None:
    global _FILELOCATION_PATH_INDEX_READY
    if _FILELOCATION_PATH_INDEX_READY:
        return
    _FILELOCATION_PATH_INDEX_READY = True
    try:
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_catalog_filelocation_file_path ON catalog_filelocation (file_path)"
            )
        import threading
        threading.Thread(target=_ensure_search_trigram_indexes_bg, daemon=True).start()
    except Exception:
        pass


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

    if _load_enriched_cache_from_disk():
        return _ENRICHED_PATHS_CACHE['paths_set']

    _rebuild_enriched_index_cache()
    return _ENRICHED_PATHS_CACHE['paths_set']


def _rebuild_enriched_index_cache() -> None:
    _ensure_filelocation_path_index()
    pg_enabled = is_postgres_scanner_enabled()
    db_path = _get_scanner_db_path()
    paths_set: set = set()
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
                    "SELECT file_path, status FROM processed_files WHERE status != 'SUCCESS' ORDER BY updated_at DESC"
                ).fetchall()
            except Exception:
                diag_rows = []
            conn.close()
            for r in diag_rows:
                if r and r[0]:
                    p_str = str(r[0])
                    st_val = str(r[1] or '').upper()
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

    all_lookup_paths = list(paths_set | set(non_success_paths))
    path_to_cids: Dict[str, List[Any]] = {}
    if all_lookup_paths:
        try:
            for i in range(0, len(all_lookup_paths), 4000):
                chunk = all_lookup_paths[i:i + 4000]
                rows_loc = (
                    FileLocation.objects.filter(file_path__in=chunk)
                    .order_by()
                    .values_list('file_path', 'catalog_item_id')
                )
                for fp_val, cid_val in rows_loc:
                    if fp_val and cid_val:
                        path_to_cids.setdefault(fp_val, []).append(cid_val)
        except Exception:
            pass

    enr_ids_set: set = set()
    for p in paths_set:
        c_list = path_to_cids.get(p)
        if c_list:
            enr_ids_set.update(c_list)
    enr_ids = list(enr_ids_set)

    # Auto-clean companion review rows in processed_files whose CatalogItem is already enriched,
    # or duplicate review rows on the same unenriched CatalogItem so processed_files counts match 1:1.
    companion_success_db_paths: List[str] = []
    duplicate_review_db_paths: List[str] = []
    seen_review_cids: set = set()
    for r in diag_rows:
        if not r or not r[0]:
            continue
        raw_db_p = str(r[0])
        st_val = str(r[1] or '').upper()
        if st_val not in ('NEEDS_REVIEW', 'FAILED', 'NOT_FOUND', 'NO_ISBN'):
            continue
        alt_p = (
            '/books/' + raw_db_p[len('/volume1/common/Media/Books/'):]
            if raw_db_p.startswith('/volume1/common/Media/Books/')
            else ('/volume1/common/Media/Books/' + raw_db_p[len('/books/'):] if raw_db_p.startswith('/books/') else None)
        )
        cids_for_row = set(path_to_cids.get(raw_db_p) or ())
        if alt_p:
            cids_for_row.update(path_to_cids.get(alt_p) or ())
        if not cids_for_row:
            continue
        if any(cid in enr_ids_set for cid in cids_for_row):
            companion_success_db_paths.append(raw_db_p)
            paths_set.add(raw_db_p)
            if alt_p:
                paths_set.add(alt_p)
        elif all(cid in seen_review_cids for cid in cids_for_row):
            duplicate_review_db_paths.append(raw_db_p)
        else:
            seen_review_cids.update(cids_for_row)

    if companion_success_db_paths or duplicate_review_db_paths:
        try:
            conn_clean = connect_scanner_db(db_path, timeout=5.0)
            cur_clean = conn_clean.cursor()
            for cp in companion_success_db_paths:
                cur_clean.execute("UPDATE processed_files SET status = 'SUCCESS' WHERE file_path = ?", (cp,))
            for dp in duplicate_review_db_paths:
                cur_clean.execute("UPDATE processed_files SET status = 'IGNORED' WHERE file_path = ?", (dp,))
            conn_clean.commit()
            conn_clean.close()
        except Exception:
            pass

    strict_rev_ids_set: set = set()
    for p in strict_review_paths:
        if p in paths_set:
            continue
        c_list = path_to_cids.get(p)
        if c_list:
            strict_rev_ids_set.update(c_list)
    strict_rev_ids_set -= enr_ids_set
    strict_rev_ids = list(strict_rev_ids_set)

    raw_rev_ids: set = set()
    for p in review_paths:
        if p in paths_set:
            continue
        c_list = path_to_cids.get(p)
        if c_list:
            raw_rev_ids.update(c_list)
    raw_rev_ids -= enr_ids_set
    rev_ids: List[Any] = list(strict_rev_ids) + [cid for cid in raw_rev_ids if cid not in strict_rev_ids_set]

    non_success_ids_set: set = set()
    for p in non_success_paths:
        c_list = path_to_cids.get(p)
        if c_list:
            non_success_ids_set.update(c_list)

    excl_ids_set = enr_ids_set | non_success_ids_set
    excl_ids = list(excl_ids_set)

    _ENRICHED_PATHS_CACHE['db_mtime'] = cur_mtime
    _ENRICHED_PATHS_CACHE['last_updated_at'] = max_ts
    _ENRICHED_PATHS_CACHE['paths_set'] = paths_set
    _ENRICHED_PATHS_CACHE['non_success_paths_set'] = set(non_success_paths)
    _ENRICHED_PATHS_CACHE['non_success_paths_loaded'] = True
    if not isinstance(_ENRICHED_PATHS_CACHE.get('status_by_path'), dict) or len(_ENRICHED_PATHS_CACHE['status_by_path']) > 400:
        _ENRICHED_PATHS_CACHE['status_by_path'] = {}
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
    _save_enriched_cache_to_disk(force=True)
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


def get_enriched_item_ids_index() -> Dict[str, Any]:
    """
    Return cached lists of CatalogItem UUIDs for:
      - `enriched_ids`: items with at least one file marked `status = 'SUCCESS'` in `scanner_state.db`
      - `needs_review_ids`: items with a file marked `status IN ('NEEDS_REVIEW', 'FAILED', 'NOT_FOUND', 'NO_ISBN')`
      - `strict_in_review_ids`: items with `status = 'NEEDS_REVIEW'` (ordered first in In Review)
      - `excluded_from_needs_enriching_ids`: all items already in `processed_files` (`SUCCESS` + `NEEDS_REVIEW` + `IGNORED` + etc.)
      - `excluded_from_needs_enriching_ids_set`: set of UUIDs for O(1) Python filtering
    """
    get_enriched_paths_set()
    if (
        _ENRICHED_PATHS_CACHE.get('enriched_item_ids') is not None
        and _ENRICHED_PATHS_CACHE.get('needs_review_item_ids') is not None
        and _ENRICHED_PATHS_CACHE.get('excluded_from_needs_enriching_ids') is not None
    ):
        return {
            'enriched_ids': _ENRICHED_PATHS_CACHE['enriched_item_ids'],
            'enriched_ids_set': _ENRICHED_PATHS_CACHE.get('enriched_item_ids_set') or set(),
            'needs_review_ids': _ENRICHED_PATHS_CACHE['needs_review_item_ids'],
            'strict_in_review_ids': _ENRICHED_PATHS_CACHE.get('strict_in_review_ids') or [],
            'excluded_from_needs_enriching_ids': _ENRICHED_PATHS_CACHE['excluded_from_needs_enriching_ids'],
            'excluded_from_needs_enriching_ids_set': _ENRICHED_PATHS_CACHE.get('excluded_from_needs_enriching_ids_set') or set(),
        }
    _rebuild_enriched_index_cache()
    return {
        'enriched_ids': _ENRICHED_PATHS_CACHE.get('enriched_item_ids') or [],
        'enriched_ids_set': _ENRICHED_PATHS_CACHE.get('enriched_item_ids_set') or set(),
        'needs_review_ids': _ENRICHED_PATHS_CACHE.get('needs_review_item_ids') or [],
        'strict_in_review_ids': _ENRICHED_PATHS_CACHE.get('strict_in_review_ids') or [],
        'excluded_from_needs_enriching_ids': _ENRICHED_PATHS_CACHE.get('excluded_from_needs_enriching_ids') or [],
        'excluded_from_needs_enriching_ids_set': _ENRICHED_PATHS_CACHE.get('excluded_from_needs_enriching_ids_set') or set(),
    }


def get_file_enrich_diagnostic(file_path: str) -> Optional[Dict[str, str]]:
    """
    Return `{'status': str, 'error': str, ...}` if `file_path` has a non-SUCCESS entry
    (`FAILED`, `NEEDS_REVIEW`, `NOT_FOUND`, `NO_ISBN`, `HAS_DRM`, `QUARANTINED`) in `scanner_state.db`.
    Fetches on-demand for only the requested file_path and caches up to 400 recent items in memory.
    """
    if not file_path:
        return None
    norm = str(file_path).strip().replace('\\', '/')
    get_enriched_paths_set()
    s_map = _ENRICHED_PATHS_CACHE.get('status_by_path')
    if not isinstance(s_map, dict):
        s_map = {}
        _ENRICHED_PATHS_CACHE['status_by_path'] = s_map
    if norm in s_map:
        return s_map[norm]
    alt = None
    if norm.startswith('/books/'):
        alt = '/volume1/common/Media/Books/' + norm[len('/books/'):]
        if alt in s_map:
            return s_map[alt]
    elif norm.startswith('/volume1/common/Media/Books/'):
        alt = '/books/' + norm[len('/volume1/common/Media/Books/'):]
        if alt in s_map:
            return s_map[alt]

    non_succ_set = _ENRICHED_PATHS_CACHE.get('non_success_paths_set')
    if isinstance(non_succ_set, set) and (non_succ_set or _ENRICHED_PATHS_CACHE.get('non_success_paths_loaded')):
        if norm not in non_succ_set and (not alt or alt not in non_succ_set):
            return None

    try:
        db_path = _get_scanner_db_path()
        conn = connect_scanner_db(db_path, timeout=3.0)
        conn.text_factory = lambda b: b.decode('utf-8', errors='replace')
        row = None
        for cand_p in ([norm, alt] if alt else [norm]):
            row = conn.execute(
                """
                SELECT file_path, status, error_message,
                       title, author, isbn,
                       orig_title, orig_author, orig_isbn,
                       match_confidence, original_metadata_json, updated_metadata_json
                FROM processed_files
                WHERE file_path = ? AND status != 'SUCCESS'
                LIMIT 1
                """,
                (cand_p,),
            ).fetchone()
            if row:
                break
        conn.close()
        if not row:
            if len(s_map) > 1000:
                s_map.clear()
            s_map[norm] = None
            if alt:
                s_map[alt] = None
            return None
        p_str = str(row[0])
        st_val = str(row[1] or '').upper()
        err_val = str(row[2] or '')
        if st_val not in ('NEEDS_REVIEW', 'FAILED', 'NOT_FOUND', 'NO_ISBN', 'HAS_DRM', 'QUARANTINED'):
            s_map[norm] = None
            if alt:
                s_map[alt] = None
            return None
        orig_m = json.loads(row[10]) or {} if (len(row) > 10 and row[10]) else {}
        upd_m = json.loads(row[11]) or {} if (len(row) > 11 and row[11]) else {}
        conf_pct = None
        if len(row) > 9 and row[9] is not None:
            try:
                raw_c = float(row[9])
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
            'candidate_title': str((row[3] if len(row) > 3 else '') or upd_m.get('title') or ''),
            'candidate_authors': str((row[4] if len(row) > 4 else '') or cand_auth_str or ''),
            'candidate_isbn': str((row[5] if len(row) > 5 else '') or upd_m.get('isbn13') or upd_m.get('isbn10') or ''),
            'candidate_publisher': str(upd_m.get('publisher') or ''),
            'candidate_pub_date': str(upd_m.get('published_date') or ''),
            'candidate_description': _strip_html(str(upd_m.get('description') or '')),
            'candidate_cover': str(upd_m.get('cover_url') or ''),
            'candidate_tags': [str(t).strip() for t in (upd_m.get('subjects') or upd_m.get('tags') or []) if str(t or '').strip()],
            'orig_title': str((row[6] if len(row) > 6 else '') or orig_m.get('title') or ''),
            'orig_authors': str((row[7] if len(row) > 7 else '') or orig_auth_str or ''),
            'orig_isbn': str((row[8] if len(row) > 8 else '') or orig_m.get('isbn13') or orig_m.get('isbn10') or ''),
            'orig_publisher': str(orig_m.get('publisher') or ''),
            'orig_pub_date': str(orig_m.get('published_date') or ''),
            'orig_description': _strip_html(str(orig_m.get('description') or '')),
            'orig_cover': str(orig_m.get('cover_url') or ''),
            'orig_tags': [str(t).strip() for t in (orig_m.get('subjects') or orig_m.get('tags') or []) if str(t or '').strip()],
        }
        if len(s_map) > 400:
            s_map.clear()
        s_map[norm] = diag_obj
        if alt:
            s_map[alt] = diag_obj
        return diag_obj
    except Exception:
        return None


def _record_in_memory_diag(file_loc: FileLocation, phys_path: Optional[Path], status: str, error_msg: str) -> None:
    get_enriched_paths_set()
    s_map = _ENRICHED_PATHS_CACHE.get('status_by_path')
    if isinstance(s_map, dict):
        diag_obj = {'status': status, 'error': error_msg}
        for p in (file_loc.file_path if file_loc else None, str(phys_path) if phys_path else None):
            if p:
                s_map[str(p)] = diag_obj
    try:
        item_id = getattr(file_loc, 'catalog_item_id', None) if file_loc else None
        if item_id:
            enr_set = _ENRICHED_PATHS_CACHE.get('enriched_item_ids_set')
            if not (isinstance(enr_set, set) and item_id in enr_set):
                rev_set = _ENRICHED_PATHS_CACHE.get('needs_review_item_ids_set')
                if isinstance(rev_set, set) and item_id not in rev_set:
                    rev_set.add(item_id)
                    rev_lst = _ENRICHED_PATHS_CACHE.get('needs_review_item_ids')
                    if isinstance(rev_lst, list):
                        rev_lst.append(item_id)
                if status == 'NEEDS_REVIEW':
                    s_rev_set = _ENRICHED_PATHS_CACHE.get('strict_in_review_ids_set')
                    if isinstance(s_rev_set, set) and item_id not in s_rev_set:
                        s_rev_set.add(item_id)
                        s_rev_lst = _ENRICHED_PATHS_CACHE.get('strict_in_review_ids')
                        if isinstance(s_rev_lst, list):
                            s_rev_lst.append(item_id)
                excl_set = _ENRICHED_PATHS_CACHE.get('excluded_from_needs_enriching_ids_set')
                if isinstance(excl_set, set) and item_id not in excl_set:
                    excl_set.add(item_id)
                    excl_lst = _ENRICHED_PATHS_CACHE.get('excluded_from_needs_enriching_ids')
                    if isinstance(excl_lst, list):
                        excl_lst.append(item_id)
                _save_enriched_cache_to_disk()
    except Exception:
        pass


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
    if norm.startswith('/books/'):
        alt = '/volume1/common/Media/Books/' + norm[len('/books/'):]
        if alt in enr_set:
            return True
    elif norm.startswith('/volume1/common/Media/Books/'):
        alt = '/books/' + norm[len('/volume1/common/Media/Books/'):]
        if alt in enr_set:
            return True
    return False


def _load_scanner_epubmeta_config() -> Dict[str, Any]:
    """
    Load the `epubmeta` section from `nas_scanner/config.json` and `scanner/config.json`,
    merging keys and preferring the active `gemini_api_key` from `scanner/config.json`.
    """
    base_dir = Path(getattr(settings, 'BASE_DIR', '.'))
    merged: Dict[str, Any] = {}
    keys_seen: List[str] = []
    for sub in ('nas_scanner', 'scanner'):
        cfg_file = base_dir / sub / 'config.json'
        if cfg_file.exists():
            try:
                data = json.loads(cfg_file.read_text(encoding='utf-8'))
                ep = data.get('epubmeta')
                if isinstance(ep, dict):
                    k = str(ep.get('gemini_api_key') or '').strip()
                    if k and k not in keys_seen:
                        keys_seen.append(k)
                    if not merged:
                        merged = dict(ep)
                    elif k:
                        merged['gemini_api_key'] = k
            except Exception:
                continue
    if keys_seen:
        merged['gemini_api_keys'] = list(reversed(keys_seen))
    return merged


_GENERIC_TITLE_STOPWORDS = {
    'the', 'and', 'for', 'with', 'from', 'into', 'your', 'that', 'this', 'what', 'when', 'where',
    'book', 'ebook', 'pdf', 'epub', 'mobi', 'azw', 'azw3', 'retail', 'nodrm', 'scan', 'ocr',
    'mcgraw', 'hill', 'osborne', 'wiley', 'sons', 'john', 'oreilly', 'reilly', 'apress', 'packt',
    'sams', 'pearson', 'addison', 'wesley', 'prentice', 'hall', 'springer', 'elsevier', 'oxford',
    'cambridge', 'university', 'press', 'routledge', 'manning', 'pragmatic', 'nostarch', 'sybex',
    'wrox', 'syngress', 'crc', 'artech', 'house', 'morgan', 'kaufmann', 'new', 'riders', 'que',
    'demystified', 'handbook', 'guide', 'manual', 'introduction', 'principles', 'edition',
    'second', 'third', 'fourth', 'fifth', 'sixth', 'first', 'complete', 'reference', 'dummies',
    'volume', 'vol', 'part', 'series', 'library', 'collection', 'professional', 'technical',
    'self', 'teaching', 'teach', 'yourself', 'made', 'easy', 'simple', 'step', 'quick', 'visual',
}


def _clean_book_stem_for_grouping(raw_name_or_title: str) -> str:
    """
    Strip file extensions, duplicate copy suffixes `(1)`, and trailing `(Publisher, Year) WW`
    release tags from a filename or title.
    """
    s = str(raw_name_or_title or '').strip()
    if not s:
        return ''
    s = os.path.basename(s.replace('\\', '/'))
    s = re.sub(r'\.(?:epub|pdf|mobi|azw3?|djvu|cbz|cbr|fb2|txt|rtf|doc|docx|lit|prc|pdb)$', '', s, flags=re.I)
    s = re.sub(r'\s*\(\d{1,2}\)$', '', s)
    s = re.sub(r'\s*\([^)]*?\b(?:1[89]\d{2}|20\d{2})\b[^)]*\)\s*(?:WW|YYePG|BLG|TLRG|DDU|LotB)?$', '', s, flags=re.I)
    s = re.sub(r'\s+(?:WW|YYePG|BLG|TLRG|DDU|LotB)$', '', s, flags=re.I)
    s = re.sub(r'\b\d+(?:st|nd|rd|th)\s+ed(?:ition)?\.?\b', ' ', s, flags=re.I)
    s = re.sub(r'\b(?:a\s+)?self[\s\-]*teaching\s+guide\b', ' ', s, flags=re.I)
    s = re.sub(r'^\s*mcgraw[\s\-]*hill(?:\s+osborne)?\s+', '', s, flags=re.I)
    s = re.sub(r'^\s*demystified\s*-\s*', '', s, flags=re.I)
    if '.' in s and ' ' not in s and s.count('.') >= 2:
        s = s.replace('.', ' ')
    s = s.replace('_', ' ')
    # If "Title - Author" where the right side is a short author name like "E. Teicholz" or "P. bedell"
    if ' - ' in s:
        parts = [p.strip() for p in s.split(' - ') if p.strip()]
        if len(parts) == 2:
            if re.match(r'^[A-Z]\.\s*[A-Za-z\-]+$', parts[1]):
                s = parts[0]
            elif re.match(r'^[A-Z]\.\s*[A-Za-z\-]+$', parts[0]):
                s = parts[1]
    return re.sub(r'\s+', ' ', s).strip()


def _extract_distinctive_book_tokens(raw_name_or_title: str) -> set:
    cleaned = _clean_book_stem_for_grouping(raw_name_or_title).lower()
    tokens = set()
    for w in re.findall(r'[a-z0-9]{2,}', cleaned):
        if w in _GENERIC_TITLE_STOPWORDS:
            continue
        if re.match(r'^(?:19\d{2}|20\d{2}|\d+)$', w):
            continue
        if len(w) >= 3:
            tokens.add(w)
    return tokens


def are_two_files_same_book(file_a: str, file_b: str, item_isbn: str = '') -> bool:
    """
    Determine if two `FileLocation` filenames/paths on the same `CatalogItem`
    actually represent the same book (e.g. `.epub` + `.pdf` formats or `(1)` duplicate copy)
    versus two completely different books that were falsely grouped together.
    """
    stem_a = _clean_book_stem_for_grouping(file_a)
    stem_b = _clean_book_stem_for_grouping(file_b)
    if not stem_a or not stem_b:
        return True
    norm_a = _norm_lookup_str(stem_a)
    norm_b = _norm_lookup_str(stem_b)
    if norm_a == norm_b:
        return True
    if norm_a and norm_b and (norm_a in norm_b or norm_b in norm_a) and min(len(norm_a), len(norm_b)) >= 6:
        return True

    # Check if one of the filenames is an ISBN filename matching the other or item_isbn
    isbn_a = _extract_book_isbn_from_text(stem_a)
    isbn_b = _extract_book_isbn_from_text(stem_b)
    clean_item_isbn = re.sub(r'[^0-9Xx]', '', str(item_isbn or '')).upper()
    if isbn_a and isbn_b:
        if isbn_a == isbn_b or (len(isbn_a) >= 10 and len(isbn_b) >= 10 and isbn_a[-9:-1] == isbn_b[-9:-1]):
            return True
        return False
    if (isbn_a and clean_item_isbn and isbn_a == clean_item_isbn and len(norm_a) <= 13) or (
        isbn_b and clean_item_isbn and isbn_b == clean_item_isbn and len(norm_b) <= 13
    ):
        return True

    toks_a = _extract_distinctive_book_tokens(stem_a)
    toks_b = _extract_distinctive_book_tokens(stem_b)
    if not toks_a or not toks_b:
        return norm_a == norm_b
    overlap = toks_a & toks_b
    min_sz = min(len(toks_a), len(toks_b))
    max_sz = max(len(toks_a), len(toks_b))
    if min_sz == 1:
        return max_sz == 1 and toks_a == toks_b
    if min_sz == 2:
        return (toks_a <= toks_b or toks_b <= toks_a) and max_sz <= 4
    if toks_a <= toks_b or toks_b <= toks_a:
        return (min_sz / float(max_sz)) >= 0.6
    return (len(overlap) / float(max_sz)) >= 0.65


def ensure_isolated_catalog_item_for_file(
    item: CatalogItem,
    file_loc: FileLocation,
    new_title: str = '',
    new_authors: str = '',
) -> CatalogItem:
    """
    If `item` has multiple `FileLocation` rows attached and some sibling files are different books
    from `file_loc`, keep `file_loc` (and any same-book siblings) on `item` and split the unrelated
    sibling files onto their own `CatalogItem` rows so `item` itself gets enriched cleanly!
    """
    if not item or not file_loc:
        return item
    try:
        all_files = list(item.files.all())
        if len(all_files) <= 1:
            return item
        siblings = [f for f in all_files if f.id != file_loc.id]
        if not siblings:
            return item
        ref_label = file_loc.file_name or os.path.basename(file_loc.file_path or '') or new_title
        unrelated_siblings = [
            sib for sib in siblings
            if not are_two_files_same_book(
                ref_label,
                sib.file_name or os.path.basename(sib.file_path or ''),
                item.isbn or '',
            )
        ]
        if not unrelated_siblings:
            return item

        # Group unrelated siblings into clusters and move them onto separate CatalogItem rows,
        # keeping `file_loc` (and its same-book siblings) on `item`!
        clusters: List[List[FileLocation]] = []
        for sib in unrelated_siblings:
            s_lbl = sib.file_name or os.path.basename(sib.file_path or '')
            placed = False
            for cl in clusters:
                rep_lbl = cl[0].file_name or os.path.basename(cl[0].file_path or '')
                if are_two_files_same_book(s_lbl, rep_lbl, ''):
                    cl.append(sib)
                    placed = True
                    break
            if not placed:
                clusters.append([sib])

        async_hydrate_pairs: List[Tuple[str, str]] = []
        created_clusters = 0
        for idx, cl in enumerate(clusters):
            rep_loc = cl[0]
            cl_ids = [fl.id for fl in cl]
            if not FileLocation.objects.filter(id__in=cl_ids, catalog_item=item).exists():
                continue
            sib_item = CatalogItem.objects.create(
                title=(_clean_book_stem_for_grouping(rep_loc.file_name or '') or rep_loc.file_name or 'Untitled')[:512],
                authors='Unknown Author',
                publisher='',
                item_type=item.item_type or 'BOOK',
            )
            moved_cnt = FileLocation.objects.filter(id__in=cl_ids, catalog_item=item).update(catalog_item=sib_item)
            if moved_cnt == 0:
                sib_item.delete()
                continue
            created_clusters += 1
            if len(clusters) <= 3:
                _hydrate_catalog_item_for_single_file(sib_item, rep_loc, enrich_if_needed=False)
            else:
                async_hydrate_pairs.append((str(sib_item.id), str(rep_loc.id)))

        if async_hydrate_pairs:
            import threading
            def _bg_hydrate(pairs: List[Tuple[str, str]]) -> None:
                for s_item_id, s_loc_id in pairs:
                    try:
                        si = CatalogItem.objects.filter(id=s_item_id).first()
                        sl = FileLocation.objects.filter(id=s_loc_id).first()
                        if si and sl:
                            _hydrate_catalog_item_for_single_file(si, sl, enrich_if_needed=False)
                    except Exception:
                        pass
            threading.Thread(target=_bg_hydrate, args=(async_hydrate_pairs,), daemon=True).start()

        # Reset `item`'s cover_thumbnail and cover_image_url to `file_loc`'s own embedded cover
        # so `item` never retains a cover image that belonged to one of the unrelated siblings!
        if hasattr(item, '_prefetched_objects_cache') and isinstance(item._prefetched_objects_cache, dict):
            item._prefetched_objects_cache.pop('files', None)
        item.cover_image_url = None
        item.cover_thumbnail = None
        emb_cov = extract_original_cover_for_item(item, ignore_backup=True)
        if emb_cov and emb_cov.startswith('data:image'):
            item.cover_thumbnail = emb_cov
        item.save(update_fields=['cover_image_url', 'cover_thumbnail', 'updated_at'])

        try:
            from .views import adjust_cached_books_count
            adjust_cached_books_count(len(clusters))
        except Exception:
            pass
        return item
    except Exception:
        return item


def _apply_enriched_metadata_to_catalog_item(
    item: CatalogItem,
    file_loc: FileLocation,
    target_meta: Any,
    isbn_fallback: Optional[str] = None,
    file_sha256: Optional[str] = None,
    cover_thumbnail: Optional[str] = None,
    phys_path: Optional[Path] = None,
) -> Tuple[CatalogItem, List[str]]:
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
            new_title = re.sub(r'\s+by\s+[A-Z][A-Za-z.\s,\-]+?\s*\(\d{4}(?:-\d{2}){0,2}\)\s*$', '', new_title).strip()
        raw_authors = getattr(target_meta, 'authors', None) or []
        new_authors = format_normalized_authors_str(raw_authors)
        if not new_authors and isinstance(raw_authors, list):
            dedup_authors: List[str] = []
            seen_auth_low: set = set()
            for raw_a in raw_authors:
                clean_a = _clean_single_line(str(raw_a or ''))
                if clean_a and clean_a.lower() not in seen_auth_low:
                    seen_auth_low.add(clean_a.lower())
                    dedup_authors.append(clean_a)
            new_authors = ', '.join(dedup_authors)
        item = ensure_isolated_catalog_item_for_file(item, file_loc, new_title=new_title or '', new_authors=new_authors or '')
        if new_title:
            item.title = new_title[:512]
        if new_authors:
            item.authors = new_authors[:512]
        new_isbn = _clean_single_line(
            getattr(target_meta, 'isbn13', None)
            or getattr(target_meta, 'isbn10', None)
            or isbn_fallback
            or _extract_book_isbn_from_text(file_loc.file_name or '', file_loc.file_path or '')
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
        orig_cover_issue = classify_cover_image(item.cover_thumbnail, item.cover_image_url)
        had_good_orig_cover = orig_cover_issue is None
        # Preserve a genuine graphic cover, or upgrade if the original cover was missing, text-only,
        # or grayscale/low-color when the new cover is the same cover design in color.
        if cover_thumbnail and str(cover_thumbnail).startswith('data:image'):
            if not (item.cover_thumbnail and str(item.cover_thumbnail).startswith('data:image')):
                item.cover_thumbnail = cover_thumbnail
            elif should_prefer_candidate_cover(item.cover_thumbnail, cover_thumbnail):
                item.cover_thumbnail = cover_thumbnail
        if new_cover_url and (not had_good_orig_cover or cover_colorfulness_score(item.cover_thumbnail) < 0.055):
            if not had_good_orig_cover:
                item.cover_image_url = new_cover_url[:1024]
            if orig_cover_issue in ('missing', 'text_only') and classify_cover_image(item.cover_thumbnail) in ('missing', 'text_only'):
                try:
                    apply_book_cover_to_item(item, new_cover_url, user_selected=False)
                except Exception:
                    pass
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
        try:
            evaluate_and_record_item_cover_status(item)
        except Exception:
            pass
        return item, merge_duplicate_catalog_items_for_item(item)
    except Exception:
        return item, []


_MERGED_ITEM_ID_REDIRECTS: Dict[str, str] = {}


def resolve_catalog_item_by_id_or_fallback(
    item_or_id: Any = None,
    file_id: str = '',
    file_path: str = '',
    isbn: str = '',
    title: str = '',
    authors: str = '',
    query: str = '',
) -> Optional[CatalogItem]:
    """
    Resolve a `CatalogItem` by primary key (`item_or_id`), or seamlessly fall back if that UUID
    was merged/split into another `CatalogItem` since the browser page loaded (via redirect table,
    `file_id`, `file_path`, `isbn`, `title` + `authors`, or cover search `query`).
    """
    if isinstance(item_or_id, CatalogItem):
        return item_or_id

    raw_id = str(item_or_id or '').strip()
    if raw_id:
        try:
            it = CatalogItem.objects.prefetch_related('files').filter(id=raw_id).first()
            if it:
                return it
        except Exception:
            pass
        # Follow up to 4 hops in _MERGED_ITEM_ID_REDIRECTS
        cur_id = raw_id
        for _ in range(4):
            nxt_id = _MERGED_ITEM_ID_REDIRECTS.get(cur_id)
            if not nxt_id or nxt_id == cur_id:
                break
            cur_id = nxt_id
            try:
                it = CatalogItem.objects.prefetch_related('files').filter(id=cur_id).first()
                if it:
                    return it
            except Exception:
                pass

    fid = str(file_id or '').strip()
    if fid:
        try:
            loc = FileLocation.objects.select_related('catalog_item').filter(id=fid).first()
            if loc and loc.catalog_item_id:
                return CatalogItem.objects.prefetch_related('files').filter(id=loc.catalog_item_id).first()
        except Exception:
            pass

    fpath = str(file_path or '').strip()
    if fpath:
        try:
            path_cands = [fpath]
            if fpath.startswith('/books/'):
                path_cands.append('/volume1/common/Media/Books/' + fpath[len('/books/'):])
            elif fpath.startswith('/volume1/common/Media/Books/'):
                path_cands.append('/books/' + fpath[len('/volume1/common/Media/Books/'):])
            loc = FileLocation.objects.select_related('catalog_item').filter(file_path__in=path_cands).first()
            if loc and loc.catalog_item_id:
                return CatalogItem.objects.prefetch_related('files').filter(id=loc.catalog_item_id).first()
        except Exception:
            pass

    clean_isbn = re.sub(r'[^0-9Xx]', '', str(isbn or '')).upper()
    if len(clean_isbn) in (10, 13):
        try:
            cands = {str(isbn).strip(), clean_isbn}
            it = CatalogItem.objects.prefetch_related('files').filter(isbn__in=list(cands)).first()
            if it:
                return it
        except Exception:
            pass

    from django.db.models.functions import Trim, Upper

    t_clean = str(title or '').strip()
    a_clean = str(authors or '').strip()
    if t_clean:
        try:
            up_t = t_clean.upper()
            up_base = t_clean.split(':', 1)[0].split(' (', 1)[0].strip().upper()
            cands = list(
                CatalogItem.objects.annotate(_ut=Upper('title'))
                .filter(_ut=up_t)
                .prefetch_related('files')
                .order_by()[:10]
            )
            if not cands and up_base and len(up_base) >= 4:
                cands = list(
                    CatalogItem.objects.extra(
                        where=["UPPER(TRIM(SPLIT_PART(SPLIT_PART(title, ':', 1), ' (', 1))) = %s"],
                        params=[up_base],
                    )
                    .prefetch_related('files')
                    .order_by()[:15]
                )
            if cands:
                if a_clean and a_clean.lower() not in ('unknown', 'unknown author', '..'):
                    a_tokens = [tok.lower() for tok in re.split(r'[^A-Za-z0-9]+', a_clean) if len(tok) >= 3]
                    surname = a_tokens[-1] if a_tokens else ''
                    if surname:
                        auth_matches = [c for c in cands if surname in (c.authors or '').lower()]
                        if auth_matches:
                            cands = auth_matches
                cands.sort(key=lambda c: (bool(c.isbn), bool(c.description), bool(c.cover_thumbnail)), reverse=True)
                return cands[0]
        except Exception:
            pass

    q_clean = str(query or '').strip()
    if q_clean and len(q_clean) >= 5:
        try:
            words = q_clean.split()
            for split_at in range(len(words), max(1, len(words) - 4), -1):
                cand_base = ' '.join(words[:split_at]).split(':', 1)[0].strip().upper()
                rem_auth = ' '.join(words[split_at:]).strip().lower()
                if len(cand_base) < 4:
                    continue
                cands = list(
                    CatalogItem.objects.extra(
                        where=["UPPER(TRIM(SPLIT_PART(SPLIT_PART(title, ':', 1), ' (', 1))) = %s"],
                        params=[cand_base],
                    )
                    .prefetch_related('files')
                    .order_by()[:10]
                )
                if cands:
                    if rem_auth:
                        r_toks = [t for t in re.split(r'[^a-z0-9]+', rem_auth) if len(t) >= 3]
                        if r_toks:
                            am = [c for c in cands if any(rt in (c.authors or '').lower() for rt in r_toks)]
                            if am:
                                cands = am
                    cands.sort(key=lambda c: (bool(c.isbn), bool(c.description), bool(c.cover_thumbnail)), reverse=True)
                    return cands[0]
        except Exception:
            pass

    return None


def _authors_refer_to_same_work(auth_a: str, auth_b: str) -> bool:
    """
    Return True if two `CatalogItem.authors` strings refer to the same author(s)
    (e.g. 'Gardner D. Hiscox, Gardner Dexter Hiscox' vs 'Gardner D. Hiscox' vs 'G. Hiscox').
    """
    from .models import _are_same_person_author_variants, format_normalized_authors_str, parse_and_normalize_authors

    ca = (auth_a or '').strip()
    cb = (auth_b or '').strip()
    if not ca or not cb:
        return False
    if ca.lower() == cb.lower():
        return True
    na = format_normalized_authors_str(ca)
    nb = format_normalized_authors_str(cb)
    if na.lower() == nb.lower():
        return True
    list_a = parse_and_normalize_authors(ca)
    list_b = parse_and_normalize_authors(cb)
    if list_a and list_b and _are_same_person_author_variants(list_a[0], list_b[0]):
        return True
    try:
        from epubmeta.matcher import score_authors
        if score_authors(list_a, list_b) >= 0.75:
            return True
    except Exception:
        pass
    return False


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
    from .models import format_normalized_authors_str
    from .search import _is_garbage_or_placeholder_author, _is_garbage_or_placeholder_title
    authors_clean = (item.authors or '').strip()
    if _is_garbage_or_placeholder_title(item.title) or _is_garbage_or_placeholder_author(authors_clean):
        return merged_ids
    authors_updated = False
    norm_item_authors = format_normalized_authors_str(authors_clean)
    if norm_item_authors and norm_item_authors != item.authors:
        item.authors = norm_item_authors[:512]
        authors_clean = item.authors
        authors_updated = True

    try:
        from django.db.models.functions import Upper
        title_up = item.title.strip().upper()
        cand_map: Dict[Any, CatalogItem] = {}

        # 1. Fast indexed lookup via `idx_catalogitem_upper_title_btree` (`UPPER(title)`)
        for c in (
            CatalogItem.objects.annotate(_ut=Upper('title'))
            .filter(item_type=item.item_type, _ut=title_up)
            .exclude(id=item.id)
            .prefetch_related('files')
            .order_by()[:20]
        ):
            if _authors_refer_to_same_work(c.authors or '', authors_clean):
                cand_map[c.id] = c

        # 2. Fast indexed lookup via `catalog_catalogitem_isbn` B-tree index (`isbn__in`)
        clean_isbn = re.sub(r'[^0-9Xx]', '', str(item.isbn or '')).upper()
        if len(clean_isbn) in (10, 13):
            isbn_cands = list({item.isbn.strip(), clean_isbn, clean_isbn.lower()})
            for c in (
                CatalogItem.objects.filter(item_type=item.item_type, isbn__in=isbn_cands)
                .exclude(id=item.id)
                .prefetch_related('files')
                .order_by()[:20]
            ):
                if _authors_refer_to_same_work(c.authors or '', authors_clean):
                    cand_map[c.id] = c

        dups = list(cand_map.values())
        if not dups:
            if authors_updated:
                item.save(update_fields=['authors', 'updated_at'])
            return merged_ids

        norm_base = _norm_lookup_str(item.title.split(':')[0])
        item_updated = authors_updated
        item_files = list(item.files.all())
        existing_paths = {f.file_path for f in item_files if f.file_path}

        for other in dups:
            other_base = _norm_lookup_str((other.title or '').split(':')[0])
            if norm_base and other_base and norm_base != other_base:
                continue
            other_files = list(other.files.all())
            # Never merge if the underlying physical filenames are unrelated books!
            if item_files and other_files:
                ref_fn = item_files[0].file_name or os.path.basename(item_files[0].file_path or '')
                if any(
                    not are_two_files_same_book(ref_fn, of.file_name or os.path.basename(of.file_path or ''), item.isbn or '')
                    for of in other_files
                ):
                    continue
            for field in ('isbn', 'asin', 'publisher', 'pub_date', 'description', 'cover_thumbnail', 'cover_image_url'):
                if not getattr(item, field, None) and getattr(other, field, None):
                    setattr(item, field, getattr(other, field))
                    item_updated = True
            if (
                getattr(item, 'cover_thumbnail', None)
                and getattr(other, 'cover_thumbnail', None)
                and should_prefer_candidate_cover(item.cover_thumbnail, other.cover_thumbnail)
            ):
                item.cover_thumbnail = other.cover_thumbnail
                if getattr(other, 'cover_image_url', None):
                    item.cover_image_url = other.cover_image_url
                item_updated = True
            for f_loc in other_files:
                if f_loc.file_path and f_loc.file_path in existing_paths:
                    f_loc.delete()
                else:
                    f_loc.catalog_item = item
                    f_loc.save(update_fields=['catalog_item'])
                    if f_loc.file_path:
                        existing_paths.add(f_loc.file_path)
            merged_ids.append(str(other.id))
            _MERGED_ITEM_ID_REDIRECTS[str(other.id)] = str(item.id)
            try:
                if str(other.id) in set(get_interesting_item_ids()):
                    set_book_interesting(str(item.id), True)
                    clear_book_interesting(str(other.id))
            except Exception:
                pass
            try:
                clear_book_bad_metadata(str(other.id))
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
        if merged_ids:
            try:
                from .views import adjust_cached_books_count
                adjust_cached_books_count(-len(merged_ids))
            except Exception:
                pass
    except Exception:
        pass
    return merged_ids


def _hydrate_catalog_item_for_single_file(item: CatalogItem, f_loc: FileLocation, enrich_if_needed: bool = False) -> None:
    """
    Hydrate `item` from `f_loc`'s matching `processed_files` entry in `scanner_state.db`
    (only if that `processed_files` entry's title genuinely matches `f_loc`'s filename stem)
    or from `parse_filename(f_loc.file_name)` + embedded PDF/EPUB cover.
    """
    import sys
    base_dir = Path(getattr(settings, 'BASE_DIR', '.'))
    for sub in ('scanner', 'nas_scanner'):
        cand_dir = str((base_dir / sub).resolve())
        if os.path.isdir(cand_dir) and cand_dir not in sys.path:
            sys.path.insert(0, cand_dir)

    fname = f_loc.file_name or os.path.basename(f_loc.file_path or '')
    parsed_title = _clean_book_stem_for_grouping(fname) or fname
    parsed_authors = 'Unknown Author'
    parsed_isbn = ''
    parsed_year = None
    try:
        from epubmeta.filename_parser import parse_filename
        pf = parse_filename(fname)
        if pf.get('title'):
            parsed_title = str(pf['title']).strip()
        if pf.get('authors'):
            parsed_authors = ', '.join(str(a).strip() for a in pf['authors'] if str(a).strip()) or 'Unknown Author'
        if pf.get('isbn'):
            parsed_isbn = str(pf['isbn']).strip()
        if pf.get('published_date') and re.match(r'^(19\d{2}|20\d{2})$', str(pf['published_date'])):
            parsed_year = int(pf['published_date'])
    except Exception:
        pass

    # Check if `scanner_state.db` has a valid non-poisoned enrichment row for `f_loc.file_path`
    db_path = _get_scanner_db_path()
    used_scanner_row = False
    if f_loc.file_path and (is_postgres_scanner_enabled() or db_path.exists()):
        conn = None
        try:
            conn = connect_scanner_db(db_path, timeout=5.0)
            conn.row_factory = sqlite3.Row
            path_variants = [f_loc.file_path]
            if f_loc.file_path.startswith('/books/'):
                path_variants.append('/volume1/common/Media/Books/' + f_loc.file_path[len('/books/'):])
            elif f_loc.file_path.startswith('/volume1/common/Media/Books/'):
                path_variants.append('/books/' + f_loc.file_path[len('/volume1/common/Media/Books/'):])
            ph = ','.join(['?'] * len(path_variants))
            row = conn.execute(
                f"""
                SELECT file_path, status, title, author, isbn, updated_metadata_json, cover_thumbnail
                FROM processed_files
                WHERE file_path IN ({ph}) AND status = 'SUCCESS'
                LIMIT 1
                """,
                path_variants,
            ).fetchone()
            if row:
                r_title = str(row['title'] or '').strip()
                if r_title and are_two_files_same_book(fname, r_title, str(row['isbn'] or '')):
                    upd_m = {}
                    if row['updated_metadata_json']:
                        try:
                            upd_m = json.loads(row['updated_metadata_json']) or {}
                        except Exception:
                            upd_m = {}
                    item.title = r_title[:512]
                    r_auth = str(row['author'] or '').strip()
                    if not r_auth and isinstance(upd_m.get('authors'), list):
                        r_auth = ', '.join(str(a) for a in upd_m['authors'] if a)
                    item.authors = (r_auth or parsed_authors or 'Unknown Author')[:512]
                    item.isbn = str(row['isbn'] or upd_m.get('isbn13') or upd_m.get('isbn10') or parsed_isbn or '')[:32]
                    item.publisher = str(upd_m.get('publisher') or item.publisher or '')[:256]
                    item.description = _strip_html(str(upd_m.get('description') or ''))
                    pub_d_str = str(upd_m.get('published_date') or '')
                    m_yr = re.search(r'\b(1[89]\d{2}|20\d{2})\b', pub_d_str)
                    if m_yr:
                        item.pub_date = datetime.date(int(m_yr.group(1)), 1, 1)
                    elif parsed_year:
                        item.pub_date = datetime.date(parsed_year, 1, 1)
                    if row['cover_thumbnail'] and str(row['cover_thumbnail']).startswith('data:image'):
                        item.cover_thumbnail = row['cover_thumbnail']
                    if upd_m.get('cover_url'):
                        item.cover_image_url = str(upd_m['cover_url'])[:1024]
                    used_scanner_row = True
                else:
                    # Poisoned row in processed_files (e.g. matched to a different book via "(McGraw-Hill, 2004) WW") -> purge it!
                    conn.execute(f"DELETE FROM processed_files WHERE file_path IN ({ph})", path_variants)
                    conn.commit()
                    p_set = _ENRICHED_PATHS_CACHE.get('paths_set')
                    if isinstance(p_set, set):
                        for pv in path_variants:
                            p_set.discard(pv)
        except Exception:
            pass
        finally:
            if conn is not None:
                conn.close()

    if not used_scanner_row:
        item.title = (parsed_title or fname)[:512]
        item.authors = (parsed_authors or 'Unknown Author')[:512]
        item.isbn = (parsed_isbn or '')[:32]
        item.description = ''
        if parsed_year:
            item.pub_date = datetime.date(parsed_year, 1, 1)
        item.cover_image_url = ''
        item.cover_thumbnail = ''

    # Always extract the actual embedded cover from `f_loc`'s physical file if `cover_thumbnail` is empty or belonged to another book
    if hasattr(item, '_prefetched_objects_cache') and isinstance(item._prefetched_objects_cache, dict):
        item._prefetched_objects_cache.pop('files', None)
    item.save()
    emb_cov = extract_original_cover_for_item(item, ignore_backup=True)
    if emb_cov and emb_cov.startswith('data:image'):
        item.cover_thumbnail = emb_cov
        item.save(update_fields=['cover_thumbnail', 'updated_at'])

    if enrich_if_needed and not used_scanner_row:
        try:
            enrich_file_location_on_demand(f_loc, force=False)
        except Exception:
            pass


def split_unrelated_files_on_catalog_item(
    item_or_id: Any,
    target_file_id: Optional[str] = None,
    enrich_split: bool = False,
    force_split_all: bool = False,
) -> Dict[str, Any]:
    """
    Split unrelated `FileLocation`s on a multi-file `CatalogItem` into their own separate
    `CatalogItem` records, hydrating each book's title, author, cover, and metadata from its
    own physical file / `scanner_state.db` entry.
    """
    item = resolve_catalog_item_by_id_or_fallback(item_or_id, file_id=target_file_id or '')
    if not item:
        return {'ok': False, 'error': f'CatalogItem not found: {item_or_id}'}

    files = sorted(list(item.files.all()), key=lambda f: (f.file_name or '', str(f.id)))
    if len(files) <= 1:
        return {
            'ok': True,
            'split_count': 0,
            'item_id': str(item.id),
            'created_items': [],
            'message': 'This item only has 1 file attached.',
        }

    # If a specific `target_file_id` was requested to be split out into its own book
    if target_file_id:
        target_loc = next((f for f in files if str(f.id) == str(target_file_id)), None)
        if not target_loc:
            return {'ok': False, 'error': f'FileLocation {target_file_id} not found on item {item.id}'}
        new_item = CatalogItem.objects.create(
            title=(_clean_book_stem_for_grouping(target_loc.file_name or '') or target_loc.file_name or item.title)[:512],
            authors='Unknown Author',
            publisher=item.publisher or '',
            item_type=item.item_type or 'BOOK',
        )
        target_loc.catalog_item = new_item
        target_loc.save(update_fields=['catalog_item'])
        _hydrate_catalog_item_for_single_file(new_item, target_loc, enrich_if_needed=enrich_split)
        return {
            'ok': True,
            'split_count': 1,
            'item_id': str(item.id),
            'created_items': [{
                'item_id': str(new_item.id),
                'file_id': str(target_loc.id),
                'title': new_item.title,
                'authors': new_item.authors,
                'file_name': target_loc.file_name,
            }],
            'message': f"Split '{target_loc.file_name}' into its own book: {new_item.title}",
        }

    # Group `files` into clusters of genuinely same-book files
    clusters: List[List[FileLocation]] = []
    if force_split_all:
        clusters = [[f] for f in files]
    else:
        for f in files:
            f_label = f.file_name or os.path.basename(f.file_path or '')
            placed = False
            for cl in clusters:
                rep_label = cl[0].file_name or os.path.basename(cl[0].file_path or '')
                if are_two_files_same_book(f_label, rep_label, item.isbn or ''):
                    cl.append(f)
                    placed = True
                    break
            if not placed:
                clusters.append([f])

    if len(clusters) <= 1:
        return {
            'ok': True,
            'split_count': 0,
            'item_id': str(item.id),
            'created_items': [],
            'message': 'All files on this item appear to be formats/copies of the same book.',
        }

    # Choose which cluster stays on `item` (prefer the cluster that matches `item.title`)
    keep_idx = 0
    for idx, cl in enumerate(clusters):
        rep_label = cl[0].file_name or os.path.basename(cl[0].file_path or '')
        if are_two_files_same_book(rep_label, item.title or '', item.isbn or ''):
            keep_idx = idx
            break

    created_items: List[Dict[str, Any]] = []
    async_hydrate_pairs: List[Tuple[str, str]] = []
    for idx, cl in enumerate(clusters):
        if idx == keep_idx:
            continue
        rep_loc = cl[0]
        cl_ids = [fl.id for fl in cl]
        if not FileLocation.objects.filter(id__in=cl_ids, catalog_item=item).exists():
            continue
        new_item = CatalogItem.objects.create(
            title=(_clean_book_stem_for_grouping(rep_loc.file_name or '') or rep_loc.file_name or 'Untitled')[:512],
            authors='Unknown Author',
            publisher=item.publisher or '',
            item_type=item.item_type or 'BOOK',
        )
        moved_cnt = FileLocation.objects.filter(id__in=cl_ids, catalog_item=item).update(catalog_item=new_item)
        if moved_cnt == 0:
            new_item.delete()
            continue
        if len(clusters) <= 4:
            _hydrate_catalog_item_for_single_file(new_item, rep_loc, enrich_if_needed=enrich_split)
        else:
            async_hydrate_pairs.append((str(new_item.id), str(rep_loc.id)))
        created_items.append({
            'item_id': str(new_item.id),
            'file_id': str(rep_loc.id),
            'title': new_item.title,
            'authors': new_item.authors,
            'file_name': rep_loc.file_name,
        })

    if async_hydrate_pairs:
        import threading
        def _bg_hydrate_split(pairs: List[Tuple[str, str]], do_enrich: bool) -> None:
            for s_item_id, s_loc_id in pairs:
                try:
                    si = CatalogItem.objects.filter(id=s_item_id).first()
                    sl = FileLocation.objects.filter(id=s_loc_id).first()
                    if si and sl and sl.catalog_item_id == si.id:
                        _hydrate_catalog_item_for_single_file(si, sl, enrich_if_needed=do_enrich)
                    elif si and not FileLocation.objects.filter(catalog_item=si).exists():
                        si.delete()
                except Exception:
                    pass
        threading.Thread(target=_bg_hydrate_split, args=(async_hydrate_pairs, enrich_split), daemon=True).start()

    # Re-hydrate `item` from its kept cluster so its cover, synopsis, and title match its remaining file(s) cleanly
    kept_rep = clusters[keep_idx][0]
    _hydrate_catalog_item_for_single_file(item, kept_rep, enrich_if_needed=enrich_split)
    if CatalogItem.objects.filter(id=item.id).exists() and not FileLocation.objects.filter(catalog_item_id=item.id).exists():
        item.delete()
        if kept_rep.catalog_item_id:
            item = CatalogItem.objects.filter(id=kept_rep.catalog_item_id).first() or item

    # Always clean up any empty orphan BOOK/MAGAZINE CatalogItem rows with 0 files
    CatalogItem.objects.filter(item_type__in=['BOOK', 'MAGAZINE'], files__isnull=True).delete()

    return {
        'ok': True,
        'split_count': len(created_items),
        'item_id': str(item.id),
        'title': item.title,
        'authors': item.authors,
        'created_items': created_items,
        'message': f"Split {len(created_items) + 1} distinct books into separate catalog entries.",
    }


def split_all_multi_book_catalog_items() -> Dict[str, Any]:
    """
    Find all `CatalogItem` rows in BookDB that have > 1 `FileLocation` attached and split any
    unrelated book files into their own individual `CatalogItem` records.
    """
    from django.db.models import Count

    multi_ids = list(
        CatalogItem.objects.annotate(f_cnt=Count('files'))
        .filter(f_cnt__gt=1)
        .order_by('-f_cnt')
        .values_list('id', flat=True)[:200]
    )
    items_split = 0
    new_items_created = 0
    split_details: List[Dict[str, Any]] = []

    for mid in multi_ids:
        it = CatalogItem.objects.filter(id=mid).first()
        if not it:
            continue
        old_title = it.title
        old_cnt = it.files.count()
        if old_cnt <= 1:
            continue
        res = split_unrelated_files_on_catalog_item(it, enrich_split=False)
        sc = int(res.get('split_count') or 0)
        if sc > 0:
            items_split += 1
            new_items_created += sc
            split_details.append({
                'item_id': str(mid),
                'old_title': old_title,
                'new_title': res.get('title') or old_title,
                'old_file_count': old_cnt,
                'split_count': sc,
            })

    orphans_deleted, _ = CatalogItem.objects.filter(item_type__in=['BOOK', 'MAGAZINE'], files__isnull=True).delete()
    if new_items_created > 0 or orphans_deleted > 0:
        try:
            from .views import adjust_cached_books_count
            adjust_cached_books_count(new_items_created - orphans_deleted)
        except Exception:
            pass

    return {
        'ok': True,
        'multi_file_items_checked': len(multi_ids),
        'items_split': items_split,
        'new_items_created': new_items_created,
        'orphans_deleted': orphans_deleted,
        'details': split_details[:50],
    }


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
    r'\b(packt|apress|o\'?reilly|addison[\s\-]*wesley|wiley|manning|sams|no\s+starch(?:\s+press)?|'
    r'pragmatic(?:\s+bookshelf)?|mcgraw[\s\-]*hill|pearson|springer|sitepoint|crc\s+press|syngress|'
    r'wrox|peachpit|cisco\s+press|microsoft\s+press|redbooks|for\s+dummies|'
    r'head\s+first|in\s+a\s+nutshell|unleashed|pocket\s+reference|friendsofed|'
    r'penguin|random\s+house|harpercollins|simon\s*(?:&|and)\s*schuster|macmillan|hachette|tor\s+books|orbit|baen|del\s+rey|avon\s+books|bantam|ace\s+books|gollancz|'
    r'oxford(?:\s+university\s+press|univpress)?|cambridge(?:\s+university\s+press)?|university\s+press|'
    r'mit\s+press|princeton|harvard|yale|routledge|elsevier|academic\s+press|kluwer|de\s+gruyter|palgrave|bloomsbury|'
    r'vintage|knopf|doubleday|norton|scholastic|dk\s+publishing|dorling\s+kindersley|focal\s+press|new\s+riders|que\s+publishing|'
    r'morgan\s+kaufmann|artech\s+house|world\s+scientific|very\s+short\s+introduction)\b',
    re.IGNORECASE,
)


def _is_valid_isbn_token(token: str) -> bool:
    """Return True if `token` is a valid 10-digit Book ISBN (non-977) or 13-digit 978/979 Bookland ISBN."""
    clean = re.sub(r'[^0-9Xx]', '', token or '').upper()
    if len(clean) == 13 and clean.isdigit():
        if not clean.startswith(('978', '979')):
            return False
        total = sum((1 if i % 2 == 0 else 3) * int(ch) for i, ch in enumerate(clean[:12]))
        check = (10 - (total % 10)) % 10
        return check == int(clean[12])
    if len(clean) == 10 and not clean.startswith('977'):
        if not clean[:9].isdigit():
            return False
        total = 0
        for i in range(9):
            total += (10 - i) * int(clean[i])
        last_val = 10 if clean[9] == 'X' else (int(clean[9]) if clean[9].isdigit() else -1)
        if last_val < 0:
            return False
        total += last_val
        return (total % 11) == 0
    return False


def _extract_book_isbn_from_text(*texts: str) -> Optional[str]:
    """Scan title/filename/isbn strings for a valid 10-digit or 13-digit Book ISBN."""
    for txt in texts:
        if not txt:
            continue
        for m in re.finditer(r'(?<![0-9])(97[89][\d\- ]{10,14}|\d{9}[\dXx])(?![0-9])', str(txt)):
            cand = re.sub(r'[^0-9Xx]', '', m.group(1)).upper()
            if _is_valid_isbn_token(cand):
                return cand
    return None

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


def _get_books_root_dir() -> Optional[Path]:
    for cand_str in ('/books', '/volume1/common/Media/Books', r'U:\Media\Books'):
        p = Path(cand_str)
        try:
            if p.exists() and p.is_dir():
                return p
        except Exception:
            continue
    return None


def _get_tomags_history_path() -> Path:
    for cand_dir in ('/books_bad', '/volume1/common/Media/Books_bad', r'U:\Media\Books_bad'):
        p = Path(cand_dir)
        try:
            if p.exists() and p.is_dir():
                return p / '.tomags_history.json'
        except Exception:
            continue
    return Path('/tmp/mylibrary_tomags_history.json')


def _load_tomags_history() -> Dict[str, Any]:
    hp = _get_tomags_history_path()
    try:
        if hp.exists():
            data = json.loads(hp.read_text(encoding='utf-8')) or {}
            if isinstance(data, dict):
                data.setdefault('entries', {})
                data.setdefault('not_magazine_paths', [])
                return data
    except Exception:
        pass
    return {'entries': {}, 'not_magazine_paths': []}


def _save_tomags_history(data: Dict[str, Any]) -> None:
    hp = _get_tomags_history_path()
    try:
        hp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    except Exception:
        pass


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
    fname = os.path.basename(file_path or '')
    try:
        hist = _load_tomags_history()
        not_mag = set(hist.get('not_magazine_paths') or [])
        if (file_path and file_path in not_mag) or (fname and fname in not_mag):
            return None
    except Exception:
        pass

    clean_isbn = re.sub(r'[^0-9Xx]', '', str(isbn or ''))
    # Standard books have 978 or 979 Bookland ISBNs (whereas 977 is ISSN for periodicals)
    if clean_isbn.startswith(('978', '979')) and len(clean_isbn) == 13:
        return None
    if len(clean_isbn) == 10 and not clean_isbn.startswith('977'):
        return None

    # Also check if a valid 10-digit or 13-digit Book ISBN is embedded in the title or filename
    # (e.g. `0192804936.Oxford.University.Press.USA.Global.Catastrophes...Oct.2009.pdf`)
    if _extract_book_isbn_from_text(isbn, title, fname):
        return None

    combined = f"{title or ''} | {fname} | {publisher or ''}"
    combined_spaced = re.sub(r'[._\-]+', ' ', combined)

    if _MAG_BOOK_PUBLISHERS_RE.search(combined) or _MAG_BOOK_PUBLISHERS_RE.search(combined_spaced):
        return None

    if clean_isbn.startswith('977'):
        return title or fname or 'ISSN Periodical'

    for rx, pub_label in _MAG_KNOWN_PATTERNS:
        if rx.search(title or '') or rx.search(fname) or rx.search(combined_spaced):
            return pub_label

    in_mag_folder = 'computer mags' in (file_path or '').lower() or '/mags/' in (file_path or '').lower()
    has_issue_date = bool(_MAG_ISSUE_DATE_RE.search(title or '') or _MAG_ISSUE_DATE_RE.search(fname))
    has_periodical_word = bool(
        re.search(
            r'\b(magazine|magazin|journal|zeitschrift|quarterly|weekly|monthly|issue|heft|ausgabe|sonderheft|truepdf|emag|downmagaz|freemagazines)\b',
            combined_spaced,
            re.I,
        )
    )

    if has_issue_date and (in_mag_folder or has_periodical_word):
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
    record its original location in `.tomags_history.json` so the move can be undone at any time,
    and update `FileLocation.file_path = '/toMags/<filename>'` so it appears on the `Moved to toMags`
    review shelf (`?shelf=tomags`).
    """
    tomags_dir = _get_tomags_dir()
    if not tomags_dir:
        return {
            'ok': False,
            'status': 'TOMAGS_NOT_MOUNTED',
            'message': 'toMags directory (/toMags or /volume1/common/Media/toMags) is not mounted.',
        }

    item = file_loc.catalog_item
    old_file_path = str(file_loc.file_path or '')
    file_id_str = str(file_loc.id)
    item_id_str = str(item.id) if item else ''

    if old_file_path.startswith('/toMags/'):
        return {
            'ok': True,
            'status': 'MOVED_TO_MAGS',
            'moved_to_tomags': True,
            'item_deleted': False,
            'item_id': item_id_str,
            'file_id': file_id_str,
            'moved_files': [os.path.basename(old_file_path)],
            'dest_path': old_file_path,
            'message': f"'{os.path.basename(old_file_path)}' is already in toMags.",
        }

    phys_path = _resolve_physical_file_path(old_file_path)
    if not phys_path or not phys_path.exists():
        return {
            'ok': False,
            'status': 'NOT_FOUND',
            'message': f'Physical file not found on NAS: {old_file_path}',
        }

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

    # Save history entry for 1-click Undo / Restore
    try:
        hist = _load_tomags_history()
        hist['entries'][dest_file.name] = {
            'orig_file_path': old_file_path,
            'dest_file_path': f'/toMags/{dest_file.name}',
            'dest_name': dest_file.name,
            'item_id': item_id_str,
            'file_id': file_id_str,
            'file_format': file_loc.file_format,
            'file_size': file_loc.file_size,
            'file_hash': file_loc.file_hash,
            'library_name': file_loc.library_name,
            'orig_item_type': item.item_type if item else 'BOOK',
            'moved_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        _save_tomags_history(hist)
    except Exception:
        pass

    # Remove old /books/... record from scanner_state.db so scanner doesn't treat it as missing
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

    # Update FileLocation in BookDB to point to /toMags/<dest_file.name> so it can be reviewed/undone!
    new_db_path = f'/toMags/{dest_file.name}'
    try:
        file_loc.file_path = new_db_path
        file_loc.file_name = dest_file.name
        file_loc.save(update_fields=['file_path', 'file_name'])
    except Exception:
        pass
    _TOMAGS_SHELF_CACHE['expires_at'] = 0.0

    if item:
        rem_files = list(FileLocation.objects.filter(catalog_item_id=item.id).values_list('file_path', flat=True))
        if rem_files and all(str(p).startswith('/toMags/') for p in rem_files):
            was_book = item.item_type == 'BOOK'
            item.item_type = 'MAGAZINE'
            item.save(update_fields=['item_type', 'updated_at'])
            if was_book:
                try:
                    from .views import adjust_cached_books_count
                    adjust_cached_books_count(-1)
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
                    if isinstance(s, set) and item.id in s:
                        s.discard(item.id)
                        lst = _ENRICHED_PATHS_CACHE.get(list_key)
                        if isinstance(lst, list):
                            try:
                                lst.remove(item.id)
                            except ValueError:
                                pass
                _save_enriched_cache_to_disk()
            except Exception:
                pass

    return {
        'ok': True,
        'status': 'MOVED_TO_MAGS',
        'moved_to_tomags': True,
        'item_deleted': False,
        'item_id': item_id_str,
        'file_id': file_id_str,
        'moved_files': [dest_file.name],
        'dest_path': new_db_path,
        'message': f"Moved '{phys_path.name}' to toMags ({dest_file.name}). Click Undo toMag to restore.",
    }


def move_catalog_item_to_tomags(item: CatalogItem, only_detected_mags: bool = False) -> Dict[str, Any]:
    """
    Move all physical files (or all magazine-detected files when `only_detected_mags=True`)
    of a `CatalogItem` to `/toMags/` and record history for Undo/Review.
    """
    tomags_dir = _get_tomags_dir()
    if not tomags_dir:
        return {
            'ok': False,
            'status': 'TOMAGS_NOT_MOUNTED',
            'message': 'toMags directory (/toMags or /volume1/common/Media/toMags) is not mounted.',
        }

    files_list = [f for f in item.files.all() if not str(f.file_path or '').startswith('/toMags/')]
    if not files_list:
        return {
            'ok': False,
            'status': 'NO_FILES',
            'message': 'This catalog item has no physical book files left to move.',
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

    if moved_names:
        return {
            'ok': True,
            'status': 'MOVED_TO_MAGS',
            'moved_to_tomags': True,
            'item_deleted': False,
            'item_id': item_id_str,
            'moved_files': moved_names,
            'message': f"Moved {len(moved_names)} file(s) to toMags: {', '.join(moved_names[:4])}{'...' if len(moved_names) > 4 else ''}",
        }

    return {
        'ok': False,
        'status': 'MOVE_FAILED',
        'message': '; '.join(errors) if errors else 'No matching magazine files were moved.',
    }


_TOMAGS_SHELF_CACHE: Dict[str, Any] = {
    'dir_mtime': -1.0,
    'expires_at': 0.0,
    'item_ids': [],
}


def get_tomags_item_ids(sync_disk: bool = True) -> List[Any]:
    """
    Return the list of `CatalogItem.id`s that currently have at least one file physically in `/toMags/`.
    Uses `/toMags` directory mtime + `.tomags_history.json` + PK lookups so it executes in <2ms
    without any unindexed `FileLocation.file_path` table scans!
    """
    tomags_dir = _get_tomags_dir()
    if not tomags_dir or not tomags_dir.exists():
        return []

    now_m = time.monotonic()
    try:
        cur_mtime = tomags_dir.stat().st_mtime
    except Exception:
        cur_mtime = 0.0

    if (
        not sync_disk
        and _TOMAGS_SHELF_CACHE['dir_mtime'] == cur_mtime
        and now_m < _TOMAGS_SHELF_CACHE['expires_at']
    ):
        return list(_TOMAGS_SHELF_CACHE['item_ids'])

    if (
        _TOMAGS_SHELF_CACHE['dir_mtime'] == cur_mtime
        and now_m < _TOMAGS_SHELF_CACHE['expires_at']
    ):
        return list(_TOMAGS_SHELF_CACHE['item_ids'])

    try:
        disk_files = {p.name: p for p in tomags_dir.iterdir() if p.is_file() and not p.name.startswith('.')}
    except Exception:
        disk_files = {}

    hist_data = _load_tomags_history()
    entries = hist_data.setdefault('entries', {})
    hist_changed = False

    # 1. Clean up any history entries whose file is no longer in `/toMags` (e.g. processed by OrgPeriodicals)
    stale_names = [fname for fname in list(entries.keys()) if fname not in disk_files]
    for fname in stale_names:
        ent = entries.pop(fname, {}) or {}
        hist_changed = True
        fid = ent.get('file_id')
        cid = ent.get('item_id')
        if fid:
            try:
                FileLocation.objects.filter(id=fid).delete()
            except Exception:
                pass
        if cid:
            try:
                if not FileLocation.objects.filter(catalog_item_id=cid).exists():
                    CatalogItem.objects.filter(id=cid).delete()
            except Exception:
                pass

    # 2. Ensure every file physically in `/toMags` is registered in `.tomags_history.json` and `BookDB`
    active_item_ids: List[str] = []
    for fname, fpath in sorted(disk_files.items(), key=lambda kv: kv[0]):
        ent = entries.get(fname) or {}
        cid = ent.get('item_id')
        fid = ent.get('file_id')

        target_item = CatalogItem.objects.filter(id=cid).first() if cid else None
        if not target_item:
            # Fast indexed title lookup by filename or prefix
            target_item = CatalogItem.objects.filter(title=fname).first()
            if not target_item:
                isbn_cand = _extract_book_isbn_from_text(fname)
                if isbn_cand:
                    target_item = CatalogItem.objects.filter(title=f"{isbn_cand}.pdf").first() or CatalogItem.objects.filter(title=f"{isbn_cand}.epub").first()
            if not target_item:
                target_item = CatalogItem.objects.create(
                    title=fname,
                    authors='Unknown Author',
                    item_type='MAGAZINE',
                )

        new_db_path = f'/toMags/{fname}'
        ext = fpath.suffix.lstrip('.').upper() or 'PDF'
        try:
            fsize = fpath.stat().st_size
        except Exception:
            fsize = 0

        loc = FileLocation.objects.filter(id=fid).first() if fid else None
        if not loc:
            loc = FileLocation.objects.filter(catalog_item_id=target_item.id, file_name=fname).first()
        if not loc:
            loc = FileLocation.objects.create(
                catalog_item=target_item,
                file_path=new_db_path,
                file_name=fname,
                file_format=ext[:16],
                file_size=fsize,
                library_name='NAS toMags Queue',
            )
            hist_changed = True
        elif loc.file_path != new_db_path:
            loc.file_path = new_db_path
            loc.file_name = fname
            loc.save(update_fields=['file_path', 'file_name'])
            hist_changed = True

        if str(ent.get('item_id')) != str(target_item.id) or str(ent.get('file_id')) != str(loc.id):
            ent['dest_name'] = fname
            ent['dest_file_path'] = new_db_path
            ent['item_id'] = str(target_item.id)
            ent['file_id'] = str(loc.id)
            ent.setdefault('file_format', ext[:16])
            ent.setdefault('file_size', fsize)
            entries[fname] = ent
            hist_changed = True

        if str(target_item.id) not in active_item_ids:
            active_item_ids.append(str(target_item.id))

    if hist_changed:
        _save_tomags_history(hist_data)

    _TOMAGS_SHELF_CACHE['dir_mtime'] = cur_mtime
    _TOMAGS_SHELF_CACHE['expires_at'] = now_m + 300.0
    _TOMAGS_SHELF_CACHE['item_ids'] = active_item_ids
    return list(active_item_ids)


def _compute_restore_dest_path(filename: str, item: Optional[CatalogItem], hist_entry: Dict[str, Any]) -> Tuple[Path, str]:
    """
    Determine `(physical_dest_path, db_file_path)` when restoring `filename` from `/toMags/` back to `/books/`.
    """
    books_root = _get_books_root_dir() or Path('/books')
    orig_db_path = str(hist_entry.get('orig_file_path') or '').strip().replace('\\', '/')

    if orig_db_path.startswith('/volume1/common/Media/Books/'):
        orig_db_path = '/books/' + orig_db_path[len('/volume1/common/Media/Books/'):]

    # If no recorded original path, infer category from sibling files on `item` or default to `Misc`
    if not orig_db_path.startswith('/books/'):
        cat_folder = 'Misc'
        if item:
            for sib in item.files.all():
                sp = str(sib.file_path or '').replace('\\', '/')
                if sp.startswith('/books/'):
                    parts = [pt for pt in sp[len('/books/'):].split('/') if pt]
                    if len(parts) >= 2:
                        cat_folder = parts[0]
                        break
        stem = os.path.splitext(filename)[0]
        alnum = re.sub(r'[^A-Za-z0-9]+', '', stem)
        three_dir = (alnum[:3] if len(alnum) >= 3 else (stem[:3] or 'MIS')).upper()
        orig_db_path = f'/books/{cat_folder}/{three_dir}/{filename}'

    rel = orig_db_path[len('/books/'):].lstrip('/')
    phys_dest = books_root / rel
    if phys_dest.exists():
        stem = phys_dest.stem
        suf = phys_dest.suffix
        parent = phys_dest.parent
        c = 1
        while True:
            cand = parent / f"{stem} ({c}){suf}"
            if not cand.exists():
                phys_dest = cand
                rel_parent = str(parent.relative_to(books_root)).replace('\\', '/')
                orig_db_path = f"/books/{rel_parent}/{cand.name}"
                break
            c += 1

    return phys_dest, orig_db_path


def undo_move_from_tomags(
    item_id: str = '',
    file_id: str = '',
    moved_files: Optional[List[str]] = None,
    enrich_after: bool = False,
) -> Dict[str, Any]:
    """
    Undo a move to `/toMags/`:
      1. Moves physical file(s) from `/toMags/` back to their original `/books/<Category>/<3-letter>/` folder.
      2. Updates `FileLocation.file_path` and restores `CatalogItem.item_type = 'BOOK'` in PostgreSQL `BookDB`.
      3. Whitelists the file in `.tomags_history.json` (`not_magazine_paths`) so `detect_magazine_publication`
         never auto-moves it to `/toMags/` again.
      4. When `enrich_after=True`, immediately runs `enrich_catalog_item_on_demand(item, force=True)` to enrich
         the restored book!
    """
    tomags_dir = _get_tomags_dir()
    if not tomags_dir:
        return {
            'ok': False,
            'status': 'TOMAGS_NOT_MOUNTED',
            'message': 'toMags directory is not mounted.',
        }

    # Ensure any unindexed files in /toMags are synced into BookDB first
    get_tomags_item_ids(sync_disk=True)

    hist_data = _load_tomags_history()
    entries = hist_data.get('entries', {})
    not_mag_list: List[str] = list(hist_data.get('not_magazine_paths') or [])

    target_locs: List[FileLocation] = []
    target_item: Optional[CatalogItem] = None

    if file_id:
        loc = FileLocation.objects.select_related('catalog_item').filter(id=file_id).first()
        if loc and str(loc.file_path or '').startswith('/toMags/'):
            target_locs.append(loc)
            target_item = loc.catalog_item

    if item_id and not target_locs:
        target_item = CatalogItem.objects.prefetch_related('files').filter(id=item_id).first()
        if target_item:
            target_locs = [
                f for f in target_item.files.all()
                if str(f.file_path or '').startswith('/toMags/')
            ]

    if moved_files:
        for mf in moved_files:
            clean_mf = os.path.basename(str(mf or '').strip())
            if not clean_mf:
                continue
            h_ent = entries.get(clean_mf) or {}
            loc = None
            if h_ent.get('file_id'):
                loc = FileLocation.objects.select_related('catalog_item').filter(id=h_ent['file_id']).first()
            if loc and loc not in target_locs:
                target_locs.append(loc)
                if not target_item:
                    target_item = loc.catalog_item

    if not target_locs and item_id:
        # Check history entries matching item_id
        for dname, h_ent in entries.items():
            if str(h_ent.get('item_id')) == str(item_id) and h_ent.get('file_id'):
                loc = FileLocation.objects.select_related('catalog_item').filter(
                    id=h_ent['file_id']
                ).first()
                if loc and loc not in target_locs:
                    target_locs.append(loc)
                    if not target_item:
                        target_item = loc.catalog_item

    if not target_locs:
        return {
            'ok': False,
            'status': 'NOT_FOUND_IN_TOMAGS',
            'message': 'No matching files were found in /toMags to restore.',
        }

    restored_files: List[str] = []
    restored_paths: List[str] = []
    errors: List[str] = []

    for loc in target_locs:
        fname = os.path.basename(loc.file_path or loc.file_name or '')
        phys_src = tomags_dir / fname
        if not phys_src.exists():
            alt_src = _resolve_physical_file_path(loc.file_path)
            if alt_src and alt_src.exists():
                phys_src = alt_src
            else:
                errors.append(f"File '{fname}' no longer exists in /toMags.")
                continue

        h_ent = entries.get(fname) or {}
        phys_dest, new_db_path = _compute_restore_dest_path(fname, loc.catalog_item or target_item, h_ent)
        try:
            phys_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(phys_src), str(phys_dest))
            try:
                os.chmod(str(phys_dest), 0o666)
            except Exception:
                pass
        except Exception as mv_err:
            errors.append(f"Failed to restore '{fname}': {mv_err}")
            continue

        loc.file_path = new_db_path
        loc.file_name = phys_dest.name
        if h_ent.get('library_name'):
            loc.library_name = h_ent['library_name']
        elif loc.library_name == 'NAS toMags Queue':
            loc.library_name = 'NAS Library'
        loc.save(update_fields=['file_path', 'file_name', 'library_name'])

        if not target_item:
            target_item = loc.catalog_item

        restored_files.append(phys_dest.name)
        restored_paths.append(new_db_path)
        for token in (phys_dest.name, fname, new_db_path):
            if token and token not in not_mag_list:
                not_mag_list.append(token)
        entries.pop(fname, None)

    hist_data['entries'] = entries
    hist_data['not_magazine_paths'] = not_mag_list[-2000:]
    _save_tomags_history(hist_data)
    _TOMAGS_SHELF_CACHE['expires_at'] = 0.0

    if not restored_files:
        return {
            'ok': False,
            'status': 'RESTORE_FAILED',
            'message': '; '.join(errors) if errors else 'Could not restore file(s) from /toMags.',
        }

    if target_item:
        target_item.refresh_from_db()
        if target_item.item_type == 'MAGAZINE':
            target_item.item_type = 'BOOK'
            target_item.save(update_fields=['item_type', 'updated_at'])

    if enrich_after and target_item:
        enr_res = enrich_catalog_item_on_demand(target_item, force=True)
        enr_res['restored_from_tomags'] = True
        enr_res['restored_files'] = restored_files
        enr_res['restored_paths'] = restored_paths
        if enr_res.get('ok'):
            enr_res['message'] = f"Restored {', '.join(restored_files[:2])} to Books & enriched: {enr_res.get('title', target_item.title)}"
        else:
            enr_res['message'] = f"Restored {', '.join(restored_files[:2])} to Books. {enr_res.get('message', '')}".strip()
        return enr_res

    return {
        'ok': True,
        'status': 'RESTORED_FROM_MAGS',
        'restored_from_tomags': True,
        'item_id': str(target_item.id) if target_item else item_id,
        'title': target_item.title if target_item else restored_files[0],
        'authors': target_item.authors if target_item else 'Unknown Author',
        'restored_files': restored_files,
        'restored_paths': restored_paths,
        'message': f"Restored {len(restored_files)} file(s) from toMags back to Books: {', '.join(restored_files[:3])}",
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
    ai_describe_no_match: bool = False,
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
        if item and item.id:
            if isinstance(_ENRICHED_PATHS_CACHE.get('enriched_item_ids_set'), set):
                if item.id not in _ENRICHED_PATHS_CACHE['enriched_item_ids_set']:
                    _ENRICHED_PATHS_CACHE['enriched_item_ids_set'].add(item.id)
                    if isinstance(_ENRICHED_PATHS_CACHE.get('enriched_item_ids'), list):
                        _ENRICHED_PATHS_CACHE['enriched_item_ids'].append(item.id)
            if isinstance(_ENRICHED_PATHS_CACHE.get('excluded_from_needs_enriching_ids_set'), set):
                if item.id not in _ENRICHED_PATHS_CACHE['excluded_from_needs_enriching_ids_set']:
                    _ENRICHED_PATHS_CACHE['excluded_from_needs_enriching_ids_set'].add(item.id)
                    if isinstance(_ENRICHED_PATHS_CACHE.get('excluded_from_needs_enriching_ids'), list):
                        _ENRICHED_PATHS_CACHE['excluded_from_needs_enriching_ids'].append(item.id)
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
        cov_issue = evaluate_and_record_item_cover_status(item)
        item_has_complete_meta = bool(
            item.title
            and not re.match(r'^untitled\b', (item.title or '').strip(), re.IGNORECASE)
            and item.authors
            and item.authors.strip().lower() not in ('', 'unknown', 'unknown author', '..', '.')
            and (item.isbn or item.asin or item.pub_date or item.publisher)
            and item.description
            and (item.cover_thumbnail or item.cover_image_url)
            and cov_issue is None
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
                'cover_issue': cov_issue or '',
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
            from epubmeta.filename_parser import parse_filename as _parse_fn
            fn_vision_clues = _parse_fn(phys_path.name)
            fn_vision_title = (fn_vision_clues.get('title') or '').strip()
            fn_vision_authors = fn_vision_clues.get('authors') or []
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
            front_txt_snippet = (
                f"FILE PATH CONTEXT: {file_loc.file_path or str(phys_path)}\n"
                f"FILENAME CLUES: title={fn_vision_title!r}, authors={fn_vision_authors!r}\n"
                f"(Note: Do not mistake a short 1-word running header repeated across pages for the book title when the filename contains the full book title.)\n"
            )
            if tmp_reader:
                try:
                    front_txt_snippet += (tmp_reader.extract_front_text(max_pages=6) or '')[:6000]
                except Exception:
                    pass
            if ai.is_available and (img_bytes_list or front_txt_snippet):
                try:
                    ai_res = ai.extract(text=front_txt_snippet or None, images=img_bytes_list or None)
                    if ai_res and (ai_res.get('title') or ai_res.get('isbn13') or ai_res.get('authors')):
                        raw_ai_t = str(ai_res.get('title') or '').strip()
                        if (
                            raw_ai_t
                            and len(raw_ai_t.split()) == 1
                            and len(raw_ai_t) <= 6
                            and len(fn_vision_title.split()) >= 3
                            and raw_ai_t.lower() not in fn_vision_title.lower()
                        ):
                            ai_res['title'] = fn_vision_title
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

    orig_cover_thumb = ''
    user_has_custom_cover = False
    try:
        user_has_custom_cover = has_user_selected_cover(item.id)
    except Exception:
        user_has_custom_cover = False
    if (
        user_has_custom_cover
        and item.cover_thumbnail
        and str(item.cover_thumbnail).startswith('data:image')
        and len(str(item.cover_thumbnail)) > 500
    ):
        orig_cover_thumb = item.cover_thumbnail
    if not orig_cover_thumb:
        try:
            from epubmeta.external_dbs import generate_cover_thumbnail
            emb_cov_bytes = reader.extract_cover_bytes() if reader else None
            if emb_cov_bytes and len(emb_cov_bytes) >= 1000:
                orig_cover_thumb = generate_cover_thumbnail(emb_cov_bytes, max_size=(360, 540)) or ''
        except Exception:
            pass
    if (
        not orig_cover_thumb
        and item.cover_thumbnail
        and str(item.cover_thumbnail).startswith('data:image')
        and len(str(item.cover_thumbnail)) > 500
        and not is_partial_banner_cover(item.cover_thumbnail)
        and not has_excessive_white_border_cover(item.cover_thumbnail)
        and not (ext == '.pdf' and is_legacy_scanner_pdf_cover(item.cover_thumbnail))
    ):
        orig_cover_thumb = item.cover_thumbnail
    if not orig_cover_thumb:
        try:
            extracted_orig = extract_original_cover_for_item(item, ignore_backup=True)
            if extracted_orig and str(extracted_orig).startswith('data:image'):
                orig_cover_thumb = extracted_orig
        except Exception:
            pass
    if orig_cover_thumb and orig_cover_thumb != item.cover_thumbnail:
        try:
            item.cover_thumbnail = orig_cover_thumb
            item.save(update_fields=['cover_thumbnail'])
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
        'orig_tags': [str(t).strip() for t in ((embedded_meta.subjects if embedded_meta and embedded_meta.subjects else None) or get_file_tags(file_loc.file_path) or []) if str(t or '').strip()],
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

        ai_gen_desc = ''
        use_auto_ai_desc = bool(ai_describe_no_match or epubmeta_cfg.get('ai_describe_no_match', False))
        if (
            use_auto_ai_desc
            and fail_status == 'NOT_FOUND'
            and not (orig_meta_payload.get('orig_description') or '').strip()
        ):
            try:
                ai_desc_res = generate_ai_description_from_content(
                    item,
                    file_id=str(file_loc.id),
                    file_path=str(file_loc.file_path or phys_path.resolve()),
                    title=orig_meta_payload.get('orig_title') or '',
                    authors=orig_meta_payload.get('orig_authors') or '',
                    publisher=orig_meta_payload.get('orig_publisher') or '',
                    isbn=orig_meta_payload.get('orig_isbn') or '',
                    apply_immediately=True,
                )
                if ai_desc_res.get('ok') and ai_desc_res.get('description'):
                    ai_gen_desc = str(ai_desc_res['description']).strip()
                    orig_meta_payload['candidate_description'] = ai_gen_desc
                    msg = f'{msg} (✨ AI description generated from book content)'
            except Exception:
                pass

        out_no_match = {
            'ok': False,
            'status': fail_status,
            'already_enriched': False,
            'message': msg,
            **orig_meta_payload,
        }
        if ai_gen_desc:
            out_no_match['ai_generated_description'] = ai_gen_desc
            out_no_match['description'] = ai_gen_desc
        return out_no_match

    orig_title_has_alnum = bool(re.search(r'[A-Za-z0-9]{3,}', (item.title or '')))
    if (review_only and force_ai_vision) or (match_result and not match_result.is_confident and not force and orig_title_has_alnum):
        if not getattr(target_meta, 'cover_url', None) and not orig_cover_for_payload:
            try:
                from epubmeta.matcher import score_title as _cov_score_title
                web_covs = search_book_covers_web(
                    title=target_meta.title or '',
                    authors=', '.join(target_meta.authors or []),
                    isbn=target_meta.isbn13 or target_meta.isbn10 or isbn or '',
                )
                for wc in web_covs:
                    cu = wc.get('cover_url') or wc.get('fallback_url') or ''
                    wc_src = wc.get('source') or ''
                    if cu and wc_src not in ('Original Cover', 'Web Image Search'):
                        if wc_src == 'Open Library (ISBN)' or _cov_score_title(wc.get('title') or '', target_meta.title or '') >= 0.65:
                            target_meta.cover_url = cu
                            break
            except Exception:
                pass
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
            'candidate_authors': format_normalized_authors_str(target_meta.authors or []) or ', '.join(target_meta.authors or []),
            'candidate_publisher': target_meta.publisher or '',
            'candidate_pub_date': target_meta.published_date or '',
            'candidate_isbn': target_meta.isbn13 or target_meta.isbn10 or isbn or '',
            'candidate_description': _strip_html(target_meta.description or ''),
            'candidate_cover': target_meta.cover_url or '',
            'candidate_tags': [str(t).strip() for t in (getattr(target_meta, 'subjects', None) or []) if str(t or '').strip()],
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

    orig_payload_issue = classify_cover_image(orig_cover_thumb, item.cover_image_url or '')
    orig_is_low_color = bool(orig_cover_thumb and cover_colorfulness_score(orig_cover_thumb) < 0.055)
    if not getattr(target_meta, 'cover_url', None) and (orig_payload_issue in ('missing', 'text_only') or orig_is_low_color):
        try:
            from epubmeta.matcher import score_title as _cov_score_title
            web_covs = search_book_covers_web(
                title=target_meta.title or '',
                authors=', '.join(target_meta.authors or []),
                isbn=target_meta.isbn13 or target_meta.isbn10 or isbn or '',
                item_id=str(item.id),
            )
            for wc in web_covs:
                cu = wc.get('cover_url') or wc.get('fallback_url') or ''
                wc_src = wc.get('source') or ''
                if not cu or wc_src == 'Original Cover':
                    continue
                if wc.get('same_cover_in_color'):
                    target_meta.cover_url = cu
                    break
                if orig_payload_issue in ('missing', 'text_only') and 'Web Image Search' not in wc_src:
                    if 'Open Library (ISBN)' in wc_src or _cov_score_title(wc.get('title') or '', target_meta.title or '') >= 0.65:
                        target_meta.cover_url = cu
                        break
        except Exception:
            pass

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
        item, merged_ids = _apply_enriched_metadata_to_catalog_item(
            item=item,
            file_loc=file_loc,
            target_meta=target_meta,
            isbn_fallback=isbn,
            file_sha256=res.get('file_sha256'),
            cover_thumbnail=res.get('cover_thumbnail'),
            phys_path=phys_path,
        )
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
        _save_enriched_cache_to_disk()
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
            'cover_issue': classify_cover_image(item.cover_thumbnail, item.cover_image_url) or '',
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
    ai_describe_no_match: bool = False,
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

    initial_files = list(item.files.all())
    split_cnt = 0
    if len(initial_files) > 1:
        try:
            sp_res = split_unrelated_files_on_catalog_item(item, enrich_split=False)
            split_cnt = int(sp_res.get('split_count') or 0)
            if CatalogItem.objects.filter(id=item.id).exists():
                item.refresh_from_db()
        except Exception:
            pass

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
    ][:3]
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
            ai_describe_no_match=ai_describe_no_match,
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
                'split_count': split_cnt,
                'remaining_files_count': item.files.count(),
            })
        return best_success_res

    final_fallback = best_review_res or last_res or {
        'ok': False,
        'status': 'FAILED',
        'already_enriched': False,
        'message': 'All candidate book files failed enrichment.',
    }
    if isinstance(final_fallback, dict) and CatalogItem.objects.filter(id=item.id).exists():
        item.refresh_from_db()
        final_fallback['split_count'] = split_cnt
        final_fallback['remaining_files_count'] = item.files.count()
        if split_cnt > 0:
            final_fallback['cover_url'] = item.cover_thumbnail or item.cover_image_url or ''
    return final_fallback


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


def _item_has_pdf_file(item: Optional[CatalogItem]) -> bool:
    if not item:
        return False
    try:
        return any(
            (f.file_format or '').upper().lstrip('.') == 'PDF'
            or str(f.file_path or '').lower().endswith('.pdf')
            for f in item.files.all()
        )
    except Exception:
        return False


def extract_original_cover_for_item(item: Optional[CatalogItem], ignore_backup: bool = False) -> Optional[str]:
    """
    Extract the original embedded book cover (as a `data:image/jpeg;base64,...` URI)
    from `item.files` (or `catalog_original_covers` backup / `item.cover_thumbnail` / `item.cover_image_url`), unless missing.
    """
    if not item:
        return None
    is_pdf_item = _item_has_pdf_file(item)
    if not ignore_backup:
        try:
            saved_orig = get_original_cover_backup_for_item(item.id)
            orig_bkp = (saved_orig or {}).get('original_cover')
            if (
                orig_bkp
                and not is_partial_banner_cover(orig_bkp)
                and not has_excessive_white_border_cover(orig_bkp)
                and not (is_pdf_item and is_legacy_scanner_pdf_cover(orig_bkp))
            ):
                return orig_bkp
        except Exception:
            pass
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

        if hasattr(item, '_prefetched_objects_cache') and isinstance(item._prefetched_objects_cache, dict):
            item._prefetched_objects_cache.pop('files', None)
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
                    if thumb and thumb.startswith('data:image') and not is_partial_banner_cover(thumb):
                        return thumb
            except Exception:
                pass
            if phys.suffix.lower() == '.pdf':
                try:
                    import pymupdf
                    doc = pymupdf.open(phys)
                    try:
                        if (not doc.needs_pass or doc.authenticate("")) and len(doc) > 0:
                            pix = doc[0].get_pixmap(dpi=150)
                            png_bytes = pix.tobytes("png")
                            if png_bytes and len(png_bytes) >= 1000:
                                thumb = generate_cover_thumbnail(png_bytes, max_size=(360, 540))
                                if thumb and thumb.startswith('data:image'):
                                    return thumb
                    finally:
                        doc.close()
                except Exception:
                    continue
    except Exception:
        pass

    if item.cover_thumbnail and str(item.cover_thumbnail).startswith('data:image'):
        if has_excessive_white_border_cover(item.cover_thumbnail):
            try:
                from epubmeta.external_dbs import generate_cover_thumbnail
                raw_b = _decode_cover_bytes(item.cover_thumbnail)
                if raw_b:
                    cropped_t = generate_cover_thumbnail(raw_b, max_size=(360, 540))
                    if cropped_t and cropped_t.startswith('data:image'):
                        return cropped_t
            except Exception:
                pass
        return item.cover_thumbnail
    if item.cover_image_url and str(item.cover_image_url).strip():
        return item.cover_image_url.strip()
    return None


def _clean_filename_like_cover_query(raw_text: str, isbn_to_strip: str = '') -> str:
    """
    Convert filename-style book titles or queries like:
      - `0192805053.OxfordUnivPress.Why_Read_Marx_Today.Nov.1999.ISBN.0192805053.mobi`
      - `0192804936.pdf`
    into a clean human-readable search query (`Why Read Marx Today`).
    """
    if not raw_text:
        return ''
    s = str(raw_text).strip()
    # Strip trailing file extensions
    s = re.sub(r'\.(?:epub|pdf|mobi|azw3?|djvu|cbz|cbr|fb2|txt|rtf|doc|docx|lit|prc|pdb)$', '', s, flags=re.I)
    # Replace dots and underscores when the string looks like a dotted/underscored filename
    if ('.' in s and ' ' not in s) or '_' in s or s.count('.') >= 2:
        s = s.replace('.', ' ').replace('_', ' ')
    # Strip embedded ISBN tokens and common filename release tags
    if isbn_to_strip:
        s = re.sub(re.escape(isbn_to_strip), ' ', s, flags=re.I)
    s = re.sub(r'\b(?:97[89]\d{10}|\d{9}[\dXx])\b', ' ', s)
    s = re.sub(
        r'\b(?:isbn|ebook|retail|epub|mobi|pdf|azw3?|nodrm|ocr|v\d+|oxfordunivpress|oxford\s+university\s+press(?:\s+usa)?)\b(?:-[A-Za-z0-9]+)?',
        ' ',
        s,
        flags=re.I,
    )
    s = re.sub(r'\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+(?:19|20)\d{2}\b', ' ', s, flags=re.I)
    s = re.sub(r'\s+', ' ', s).strip(' .,-_:')
    return s


def search_book_covers_web(
    query: str = '',
    title: str = '',
    authors: str = '',
    isbn: str = '',
    item_id: str = '',
) -> List[Dict[str, Any]]:
    """
    Search multiple web book cover sources in parallel (Goodreads, Apple Books, Google Books,
    Open Library, and Web Image Search) just like Moon+ Reader's "Download cover from internet"
    feature, placing the book's original embedded cover first unless missing.
    Returns a deduplicated list of candidate cover dicts:
      `{"cover_url": str, "fallback_url": str, "title": str, "authors": str, "source": str}`
    """
    import concurrent.futures
    import urllib.parse
    import requests

    clean_isbn = re.sub(r'[^0-9Xx]', '', str(isbn or '')).upper()
    if len(clean_isbn) not in (10, 13):
        extracted_isbn = _extract_book_isbn_from_text(isbn, query, title)
        if extracted_isbn:
            clean_isbn = extracted_isbn

    raw_title = (title or '').strip()
    clean_title = _clean_filename_like_cover_query(raw_title, clean_isbn)
    clean_authors = (authors or '').strip()
    if clean_authors.lower() in ('unknown', 'unknown author', '..', '.'):
        clean_authors = ''

    raw_q = (query or '').strip()
    search_q = _clean_filename_like_cover_query(raw_q, clean_isbn) if raw_q else ''
    if not search_q:
        parts = [p for p in (clean_title, clean_authors) if p and p != clean_isbn]
        search_q = ' '.join(parts).strip()

    # Shorten overly long subtitle queries so Apple Books / Open Library return fast, accurate matches
    short_title = clean_title.split(':', 1)[0].strip() if ':' in clean_title else clean_title
    api_q = re.sub(r'\b(?:Inc|Corp|Corporation|LLC|Ltd)\.?(?=\s|$)', '', search_q, flags=re.I)
    api_q = re.sub(r'\s+', ' ', api_q).strip()
    if len(api_q) > 70 and ':' in api_q:
        api_q = f"{api_q.split(':', 1)[0].strip()} {clean_authors}".strip()

    # If the book only had an ISBN filename (e.g. `0192804936.pdf`), quickly resolve its title/author from Goodreads
    if (not api_q or api_q == clean_isbn or len(api_q) < 3) and len(clean_isbn) in (10, 13):
        try:
            r_gr_isbn = requests.get(
                f'https://www.goodreads.com/book/auto_complete?format=json&q={urllib.parse.quote(clean_isbn)}',
                timeout=(1.5, 2.0),
                headers={'User-Agent': 'Mozilla/5.0'},
            )
            if r_gr_isbn.status_code == 200:
                gr_items = r_gr_isbn.json() or []
                if gr_items and isinstance(gr_items, list):
                    first_gr = gr_items[0] or {}
                    gr_t = (first_gr.get('title') or '').strip()
                    gr_a = ((first_gr.get('author') or {}).get('name') or '').strip()
                    if gr_t:
                        clean_title = gr_t
                        short_title = gr_t.split(':', 1)[0].strip() if ':' in gr_t else gr_t
                        if gr_a and not clean_authors:
                            clean_authors = gr_a
                        api_q = f"{short_title} {clean_authors}".strip()
        except Exception:
            pass

    results: List[Dict[str, Any]] = []
    seen_urls: set = set()

    def _add(url: str, c_title: str, c_auth: str, source: str, fallback_url: str = ''):
        if not url and not fallback_url:
            return
        u = (url or fallback_url).strip()
        if u.startswith('http://'):
            u = 'https://' + u[7:]
        norm_u = re.sub(r'&edge=curl', '', u)
        if norm_u in seen_urls:
            return
        seen_urls.add(norm_u)
        fb = (fallback_url or '').strip()
        if fb.startswith('http://'):
            fb = 'https://' + fb[7:]
        results.append({
            'cover_url': norm_u,
            'fallback_url': fb,
            'title': (c_title or clean_title or 'Book Cover').strip(),
            'authors': (c_auth or clean_authors or '').strip(),
            'source': source,
        })

    # 0. Original Book Cover (First Option unless missing) — fetched in parallel so cold NAS disks never block
    ci = None
    if item_id:
        try:
            ci = CatalogItem.objects.prefetch_related('files').filter(id=item_id).first()
        except Exception:
            ci = None

    # 1. Direct ISBN cover from Open Library if ISBN is available
    if len(clean_isbn) in (10, 13):
        _add(
            f'https://covers.openlibrary.org/b/isbn/{clean_isbn}-L.jpg?default=false',
            short_title or clean_title or f'ISBN {clean_isbn}',
            clean_authors,
            'Open Library (ISBN)',
        )

    want_tokens = {
        w for w in re.findall(r'[a-z0-9]{3,}', (short_title or api_q).lower())
        if w not in ('the', 'and', 'for', 'with', 'from', 'book', 'cover', 'edition', 'introduction', 'very', 'short')
    }

    def _fetch_goodreads() -> List[Dict[str, str]]:
        out = []
        queries = []
        if len(clean_isbn) in (10, 13):
            queries.append(clean_isbn)
        if api_q and api_q not in queries:
            queries.append(api_q)
        for q_str in queries[:2]:
            try:
                url = f'https://www.goodreads.com/book/auto_complete?format=json&q={urllib.parse.quote(q_str)}'
                r = requests.get(url, timeout=(2.0, 2.8), headers={'User-Agent': 'Mozilla/5.0'})
                if r.status_code == 200:
                    for it in (r.json() or [])[:6]:
                        img = (it.get('imageUrl') or '').strip()
                        if not img or 'nophoto' in img:
                            continue
                        hi_img = re.sub(r'\._S[XY]\d+(?:_S[XY]\d+)?_\.', '.', img)
                        out.append({
                            'url': hi_img,
                            'fallback_url': img,
                            'title': it.get('title') or '',
                            'authors': ((it.get('author') or {}).get('name') or '').strip(),
                            'source': 'Goodreads',
                        })
            except Exception:
                pass
        return out

    def _fetch_apple_books() -> List[Dict[str, str]]:
        out = []
        if not api_q and not clean_isbn:
            return out
        try:
            if len(clean_isbn) in (10, 13):
                r_i = requests.get(
                    f'https://itunes.apple.com/lookup?isbn={urllib.parse.quote(clean_isbn)}',
                    timeout=(1.8, 2.5),
                    headers={'User-Agent': 'Mozilla/5.0'},
                )
                if r_i.status_code == 200:
                    for item in (r_i.json().get('results') or []):
                        art = item.get('artworkUrl100') or item.get('artworkUrl60') or ''
                        if art:
                            hi_res = re.sub(r'\d+x\d+bb\.jpg', '600x600bb.jpg', art)
                            out.append({
                                'url': hi_res,
                                'fallback_url': art,
                                'title': item.get('trackName') or '',
                                'authors': item.get('artistName') or '',
                                'source': 'Apple Books',
                            })
            if api_q:
                url = f'https://itunes.apple.com/search?media=ebook&entity=ebook&limit=8&term={urllib.parse.quote(api_q)}'
                r = requests.get(url, timeout=(2.0, 2.8), headers={'User-Agent': 'Mozilla/5.0'})
                if r.status_code == 200:
                    for item in (r.json().get('results') or []):
                        art = item.get('artworkUrl100') or item.get('artworkUrl60') or ''
                        t_name = item.get('trackName') or ''
                        a_name = item.get('artistName') or ''
                        if want_tokens:
                            cand_toks = set(re.findall(r'[a-z0-9]{3,}', f"{t_name} {a_name}".lower()))
                            if not (want_tokens & cand_toks):
                                continue
                        if art:
                            hi_res = re.sub(r'\d+x\d+bb\.jpg', '600x600bb.jpg', art)
                            out.append({
                                'url': hi_res,
                                'fallback_url': art,
                                'title': t_name,
                                'authors': a_name,
                                'source': 'Apple Books',
                            })
        except Exception:
            pass
        return out[:6]

    def _fetch_google_books() -> List[Dict[str, str]]:
        out = []
        if (not api_q and not clean_isbn) or time.monotonic() < _GOOGLE_BOOKS_COOLDOWN_UNTIL:
            return out
        try:
            epubmeta_cfg = _load_scanner_epubmeta_config()
            g_key = epubmeta_cfg.get('google_api_key') or os.environ.get('GOOGLE_BOOKS_API_KEY') or ''
            q_str = f'isbn:{clean_isbn}' if len(clean_isbn) in (10, 13) else api_q
            url = f'https://www.googleapis.com/books/v1/volumes?q={urllib.parse.quote(q_str)}&maxResults=6'
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
        return out[:6]

    def _fetch_open_library() -> List[Dict[str, str]]:
        out = []
        if not api_q:
            return out
        try:
            url = f'https://openlibrary.org/search.json?q={urllib.parse.quote(api_q)}&limit=6'
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
        return out[:6]

    def _fetch_web_image_covers() -> List[Dict[str, str]]:
        out = []
        base_web_q = api_q or clean_isbn
        if not base_web_q:
            return out
        try:
            import html as _html
            web_q = f'{base_web_q} book cover'
            if len(clean_isbn) in (10, 13) and clean_isbn not in web_q and len(base_web_q) < 45:
                web_q = f'{base_web_q} {clean_isbn} book cover'
            url = f'https://www.bing.com/images/search?q={urllib.parse.quote(web_q)}&form=HDRSC2'
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
            }
            r = requests.get(url, timeout=(2.2, 3.0), headers=headers)
            if r.status_code == 200:
                raw_blocks = re.findall(r'\bm="(\{[^"]+\})"', r.text)
                for b in raw_blocks[:16]:
                    try:
                        d = json.loads(_html.unescape(b))
                    except Exception:
                        continue
                    u = (d.get('murl') or '').strip()
                    fb_u = (d.get('turl') or '').strip()
                    t_lbl = (d.get('t') or d.get('desc') or short_title or clean_title or base_web_q).strip()
                    if not u and not fb_u:
                        continue
                    # Skip generic vector/stock/template spam
                    low_Check = f"{u} {t_lbl}".lower()
                    if any(bad in low_Check for bad in ('template.net', 'freepik.com', 'vecteezy.com', 'dreamstime.com', 'pngtree.com', 'shutterstock.com')):
                        continue
                    out.append({
                        'url': u or fb_u,
                        'fallback_url': fb_u,
                        'title': t_lbl,
                        'authors': clean_authors,
                        'source': 'Web Image Search',
                    })
                    if len(out) >= 12:
                        break
        except Exception:
            pass
        return out

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=6)
    try:
        f_orig = ex.submit(extract_original_cover_for_item, ci) if ci else None
        f_gr = ex.submit(_fetch_goodreads)
        f_apple = ex.submit(_fetch_apple_books)
        f_google = ex.submit(_fetch_google_books)
        f_ol = ex.submit(_fetch_open_library)
        f_web = ex.submit(_fetch_web_image_covers)
        futs = tuple(f for f in (f_orig, f_gr, f_apple, f_google, f_ol, f_web) if f is not None)
        concurrent.futures.wait(futs, timeout=3.5)
        orig_cov = None
        if ci:
            if f_orig and f_orig.done():
                try:
                    orig_cov = f_orig.result(timeout=0.05)
                except Exception:
                    orig_cov = None
            if not orig_cov and ci.cover_thumbnail and str(ci.cover_thumbnail).startswith('data:image'):
                orig_cov = ci.cover_thumbnail
            elif (
                orig_cov
                and orig_cov.startswith('data:image')
                and ci.cover_thumbnail
                and (
                    is_partial_banner_cover(ci.cover_thumbnail)
                    or (_item_has_pdf_file(ci) and is_legacy_scanner_pdf_cover(ci.cover_thumbnail))
                )
                and not is_partial_banner_cover(orig_cov)
            ):
                try:
                    ci.cover_thumbnail = orig_cov
                    ci.save(update_fields=['cover_thumbnail'])
                except Exception:
                    pass
            if orig_cov:
                results.insert(0, {
                    'cover_url': orig_cov,
                    'fallback_url': '',
                    'title': (ci.title or clean_title or 'Original Cover').strip(),
                    'authors': (ci.authors or clean_authors or '').strip(),
                    'source': 'Original Cover',
                })
                seen_urls.add(orig_cov)
            try:
                saved_gens = get_saved_generated_covers_for_item(ci.id)
                insert_pos = 1 if orig_cov else 0
                for g_entry in saved_gens:
                    g_uri = (g_entry.get('cover_url') or '').strip()
                    if g_uri and g_uri not in seen_urls:
                        results.insert(insert_pos, {
                            'cover_url': g_uri,
                            'fallback_url': '',
                            'title': (ci.title or clean_title or 'Generated Cover').strip(),
                            'authors': (ci.authors or clean_authors or '').strip(),
                            'source': g_entry.get('style_label') or '🎨 Generated Cover',
                            'is_generated': True,
                            'generated_id': g_entry.get('id'),
                        })
                        seen_urls.add(g_uri)
                        insert_pos += 1
            except Exception:
                pass
        for fut in (f_gr, f_apple, f_google, f_ol, f_web):
            if fut.done():
                try:
                    for entry in (fut.result(timeout=0.05) or []):
                        _add(entry['url'], entry['title'], entry['authors'], entry['source'], entry.get('fallback_url', ''))
                except Exception:
                    pass

        # Inspect top candidate covers to prefer covers that match the original cover's design in color
        # (and prioritize color covers over B&W/grayscale covers).
        if len(results) > 1:
            orig_col = cover_colorfulness_score(orig_cov) if orig_cov else 0.0
            orig_issue = classify_cover_image(orig_cov) if orig_cov else 'missing'

            def _probe_cand(idx_and_entry: Tuple[int, Dict[str, Any]]) -> Tuple[int, Optional[bytes]]:
                c_idx, c_ent = idx_and_entry
                if c_ent.get('source') == 'Original Cover':
                    return c_idx, _decode_cover_bytes(c_ent.get('cover_url'))
                probe_u = (c_ent.get('fallback_url') or c_ent.get('cover_url') or '').strip()
                if not probe_u:
                    return c_idx, None
                if probe_u.startswith('data:image'):
                    return c_idx, _decode_cover_bytes(probe_u)
                try:
                    r_p = requests.get(
                        probe_u,
                        timeout=(1.0, 1.3),
                        headers={'User-Agent': 'Mozilla/5.0'},
                    )
                    if r_p.status_code == 200 and len(r_p.content) >= 900:
                        return c_idx, r_p.content
                except Exception:
                    pass
                return c_idx, None

            probe_targets = [(i, results[i]) for i in range(min(len(results), 11)) if results[i].get('source') != 'Original Cover']
            probe_futs = [ex.submit(_probe_cand, pt) for pt in probe_targets]
            concurrent.futures.wait(probe_futs, timeout=1.4)
            bytes_by_idx: Dict[int, bytes] = {}
            for pf in probe_futs:
                if pf.done():
                    try:
                        p_i, p_b = pf.result(timeout=0.02)
                        if p_b:
                            bytes_by_idx[p_i] = p_b
                    except Exception:
                        pass

            ranked: List[Tuple[Tuple[int, float, float, int], Dict[str, Any]]] = []
            for idx, ent in enumerate(results):
                src = ent.get('source') or ''
                if src == 'Original Cover':
                    ent['colorfulness'] = round(orig_col, 3)
                    # If the original cover is B&W/low-color or text-only, let a "Same Cover in Color" candidate rank ahead of it (tier 1 vs tier 0)
                    orig_tier = 1 if (orig_col < 0.065 or orig_issue in ('missing', 'text_only')) else 0
                    ranked.append(((orig_tier, 0.0, -orig_col, idx), ent))
                    continue
                if ent.get('is_generated') or src.startswith('🎨'):
                    ranked.append(((1, 0.0, -1.0, idx), ent))
                    continue

                raw_b = bytes_by_idx.get(idx)
                if not raw_b:
                    ranked.append(((3, 0.0, 0.0, idx), ent))
                    continue

                if classify_cover_image(raw_b) in ('missing', 'text_only'):
                    ranked.append(((5, 0.0, 0.0, idx), ent))
                    continue

                cand_col = cover_colorfulness_score(raw_b)
                ent['colorfulness'] = round(cand_col, 3)
                same_design, sim = are_covers_same_design(orig_cov, raw_b) if orig_cov else (False, 0.0)
                if same_design and cand_col >= 0.065 and (cand_col >= orig_col + 0.035 or orig_col >= 0.065):
                    ent['same_cover_in_color'] = True
                    ent['design_similarity'] = round(sim, 3)
                    if '★' not in src:
                        ent['source'] = f'★ Same Cover in Color ({src})'
                    ranked.append(((0, -round(sim, 3), -round(cand_col, 3), idx), ent))
                elif cand_col >= 0.065:
                    # Prefer color covers over grayscale covers while preserving provider order within tier
                    ranked.append(((2, 0.0, -round(cand_col, 2), idx), ent))
                else:
                    ranked.append(((4, 0.0, -round(cand_col, 2), idx), ent))

            ranked.sort(key=lambda pair: pair[0])
            results = [pair[1] for pair in ranked]
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    return results[:24]


def apply_book_cover_to_item(
    item_or_id: Any,
    cover_url: str,
    fallback_url: str = '',
    file_id: str = '',
    file_path: str = '',
    isbn: str = '',
    title: str = '',
    authors: str = '',
    query: str = '',
    user_selected: bool = True,
) -> Dict[str, Any]:
    """
    Download `cover_url` (or `fallback_url` if the primary host blocks or fails),
    convert it into a crisp base64 JPEG thumbnail (`cover_thumbnail`),
    and save both `cover_image_url` and `cover_thumbnail` onto `CatalogItem` in BookDB
    (plus `scanner_state.db` if tracked).
    """
    import base64
    import io
    import requests

    item = resolve_catalog_item_by_id_or_fallback(
        item_or_id,
        file_id=file_id,
        file_path=file_path,
        isbn=isbn,
        title=title,
        authors=authors,
        query=query,
    )
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
                    img.thumbnail((720, 1080), Image.Resampling.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, format='JPEG', quality=88, optimize=True)
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
    try:
        _snapshot_original_cover_if_needed(item)
    except Exception:
        pass
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

    # When a user applies a cover via Find Cover, remove/dismiss from Missing / Text Cover shelf
    needs_cover_cnt = 0
    try:
        if user_selected:
            set_Res = set_book_needs_cover(item.id, needs_cover=False, reason='Cover applied by user')
            needs_cover_cnt = int(set_Res.get('missing_cover_count', 0))
        else:
            evaluate_and_record_item_cover_status(item)
            needs_cover_cnt = int(get_needs_cover_enriched_shelf().get('count', 0))
    except Exception:
        pass

    return {
        'ok': True,
        'item_id': str(item.id),
        'cover_url': item.cover_thumbnail or item.cover_image_url or cover_url,
        'external_cover_url': item.cover_image_url or '',
        'cover_issue': '',
        'missing_cover_count': needs_cover_cnt,
    }


_GENERATED_COVERS_TABLE_READY = False


def _ensure_generated_covers_table() -> None:
    """
    Ensure PostgreSQL tables `catalog_generated_covers` and `catalog_original_covers` exist in BookDB.
    """
    global _GENERATED_COVERS_TABLE_READY
    if _GENERATED_COVERS_TABLE_READY:
        return
    try:
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS catalog_generated_covers (
                    id SERIAL PRIMARY KEY,
                    item_id VARCHAR(64) NOT NULL,
                    cover_data_uri TEXT NOT NULL,
                    style_label VARCHAR(128) DEFAULT '🎨 Generated Cover',
                    prompt_summary TEXT DEFAULT '',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_generated_covers_item_id
                ON catalog_generated_covers (item_id, id DESC)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS catalog_original_covers (
                    item_id VARCHAR(64) PRIMARY KEY,
                    original_cover_thumbnail TEXT DEFAULT '',
                    original_cover_image_url TEXT DEFAULT '',
                    saved_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        _GENERATED_COVERS_TABLE_READY = True
    except Exception:
        pass


def get_original_cover_backup_for_item(item_id: Any) -> Optional[Dict[str, str]]:
    """
    Return the backed-up original cover for `item_id` from `catalog_original_covers` (if stored).
    """
    item_id_str = str(item_id or '').strip()
    if not item_id_str:
        return None
    _ensure_generated_covers_table()
    try:
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT original_cover_thumbnail, original_cover_image_url
                FROM catalog_original_covers
                WHERE item_id = %s
                """,
                [item_id_str],
            )
            row = cur.fetchone()
            if not row:
                return None
            thumb = (row[0] or '').strip()
            ext_url = (row[1] or '').strip()
            return {
                'original_cover': thumb or ext_url,
                'original_cover_thumbnail': thumb,
                'original_cover_image_url': ext_url,
            }
    except Exception:
        return None


def _snapshot_original_cover_if_needed(item: Optional[CatalogItem]) -> Optional[str]:
    """
    Before replacing a book's cover with a generated or web cover, snapshot its true original cover
    in `catalog_original_covers` (if not already recorded) so the user can always revert back to it.
    """
    if not item or not item.id:
        return None
    item_id_str = str(item.id).strip()
    existing = get_original_cover_backup_for_item(item_id_str)
    if existing is not None:
        return existing.get('original_cover') or ''

    _ensure_generated_covers_table()
    # Snapshot the item's existing cover_thumbnail in BookDB first so reverting restores the exact byte-for-byte original cover
    orig_thumb = ''
    if item.cover_thumbnail and str(item.cover_thumbnail).startswith('data:image'):
        orig_thumb = str(item.cover_thumbnail)

    if not orig_thumb:
        try:
            import sys
            base_dir = Path(getattr(settings, 'BASE_DIR', '.'))
            for sub in ('scanner', 'nas_scanner'):
                cand_dir = str((base_dir / sub).resolve())
                if os.path.isdir(cand_dir) and cand_dir not in sys.path:
                    sys.path.insert(0, cand_dir)
            from epubmeta.external_dbs import generate_cover_thumbnail
            from epubmeta.readers import get_reader

            for f_loc in item.files.all():
                phys = _resolve_physical_file_path(f_loc.file_path)
                if not phys or not phys.exists():
                    continue
                try:
                    rdr = get_reader(phys)
                    cov_b = rdr.extract_cover_bytes() if rdr else None
                    if cov_b and len(cov_b) >= 1000:
                        t = generate_cover_thumbnail(cov_b, max_size=(360, 540))
                        if t and t.startswith('data:image'):
                            orig_thumb = t
                            break
                except Exception:
                    pass
        except Exception:
            pass
    orig_url = str(item.cover_image_url or '').strip()

    try:
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO catalog_original_covers (item_id, original_cover_thumbnail, original_cover_image_url, saved_at)
                VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (item_id) DO NOTHING
                """,
                [item_id_str, orig_thumb, orig_url],
            )
    except Exception:
        pass
    return orig_thumb or orig_url


def get_saved_generated_covers_for_item(item_id: Any) -> List[Dict[str, Any]]:
    """
    Return all saved generated covers for `item_id` from `catalog_generated_covers` in BookDB,
    ordered newest first.
    """
    item_id_str = str(item_id or '').strip()
    if not item_id_str:
        return []
    _ensure_generated_covers_table()
    out: List[Dict[str, Any]] = []
    try:
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, cover_data_uri, style_label, prompt_summary, created_at
                FROM catalog_generated_covers
                WHERE item_id = %s
                ORDER BY id DESC
                LIMIT 24
                """,
                [item_id_str],
            )
            for r in cur.fetchall():
                out.append({
                    'id': int(r[0]),
                    'cover_url': r[1] or '',
                    'style_label': r[2] or '🎨 Generated Cover',
                    'prompt_summary': r[3] or '',
                    'created_at': r[4].isoformat() if r[4] else '',
                })
    except Exception:
        pass
    return out


def revert_book_cover_to_original(
    item_or_id: Any,
    file_id: str = '',
    file_path: str = '',
    isbn: str = '',
    title: str = '',
    authors: str = '',
    query: str = '',
) -> Dict[str, Any]:
    """
    Revert a `CatalogItem`'s active cover back to its original cover (from `catalog_original_covers`
    or extracted from the physical EPUB/PDF file on the NAS), while keeping any generated covers
    stored in `catalog_generated_covers` in BookDB.
    """
    item = resolve_catalog_item_by_id_or_fallback(
        item_or_id,
        file_id=file_id,
        file_path=file_path,
        isbn=isbn,
        title=title,
        authors=authors,
        query=query,
    )
    if not item:
        return {'ok': False, 'error': f'CatalogItem not found: {item_or_id}'}

    backup = get_original_cover_backup_for_item(item.id)
    orig_thumb = (backup.get('original_cover_thumbnail') if backup else '') or ''
    orig_url = (backup.get('original_cover_image_url') if backup else '') or ''

    if not orig_thumb and not orig_url:
        # Try extracting directly from physical file on disk
        try:
            import sys
            base_dir = Path(getattr(settings, 'BASE_DIR', '.'))
            for sub in ('scanner', 'nas_scanner'):
                cand_dir = str((base_dir / sub).resolve())
                if os.path.isdir(cand_dir) and cand_dir not in sys.path:
                    sys.path.insert(0, cand_dir)
            from epubmeta.external_dbs import generate_cover_thumbnail
            from epubmeta.readers import get_reader

            for f_loc in item.files.all():
                phys = _resolve_physical_file_path(f_loc.file_path)
                if not phys or not phys.exists():
                    continue
                rdr = get_reader(phys)
                cov_b = rdr.extract_cover_bytes() if rdr else None
                if cov_b and len(cov_b) >= 1000:
                    t = generate_cover_thumbnail(cov_b, max_size=(360, 540))
                    if t and t.startswith('data:image'):
                        orig_thumb = t
                        break
        except Exception:
            pass

    item.cover_thumbnail = orig_thumb or None
    item.cover_image_url = orig_url[:999] if orig_url else None
    item.save(update_fields=['cover_image_url', 'cover_thumbnail', 'updated_at'])

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
                        [orig_thumb or None, *f_paths],
                    )
                    conn.commit()
                finally:
                    conn.close()
    except Exception:
        pass

    try:
        evaluate_and_record_item_cover_status(item)
        needs_cover_cnt = int(get_needs_cover_enriched_shelf().get('count', 0))
    except Exception:
        needs_cover_cnt = 0

    return {
        'ok': True,
        'reverted': True,
        'item_id': str(item.id),
        'cover_url': orig_thumb or orig_url or '',
        'has_original_cover': bool(orig_thumb or orig_url),
        'saved_generated_covers': get_saved_generated_covers_for_item(item.id),
        'missing_cover_count': needs_cover_cnt,
    }


def _hex_to_rgb_tuple(hex_str: str, fallback: Tuple[int, int, int] = (24, 36, 64)) -> Tuple[int, int, int]:
    s = re.sub(r'[^0-9a-fA-F]', '', str(hex_str or ''))
    if len(s) == 3:
        s = ''.join(ch * 2 for ch in s)
    if len(s) == 6:
        try:
            return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
        except Exception:
            return fallback
    return fallback


def _load_cover_font(size: int, bold: bool = False, serif: bool = False):
    from PIL import ImageFont
    candidates = []
    if serif and bold:
        candidates = [
            '/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf',
            '/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf',
            'C:/Windows/Fonts/georgiab.ttf',
            'C:/Windows/Fonts/timesbd.ttf',
        ]
    elif serif:
        candidates = [
            '/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf',
            '/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf',
            'C:/Windows/Fonts/georgia.ttf',
            'C:/Windows/Fonts/times.ttf',
        ]
    elif bold:
        candidates = [
            '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
            '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
            'C:/Windows/Fonts/segoeuib.ttf',
            'C:/Windows/Fonts/arialbd.ttf',
        ]
    else:
        candidates = [
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
            'C:/Windows/Fonts/segoeui.ttf',
            'C:/Windows/Fonts/arial.ttf',
        ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size=size)
            except Exception:
                pass
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def _wrap_cover_text(draw, text: str, font, max_width: int, max_lines: int = 4) -> List[str]:
    words = [w for w in re.split(r'\s+', (text or '').strip()) if w]
    if not words:
        return []
    lines: List[str] = []
    cur = words[0]
    for w in words[1:]:
        test = f'{cur} {w}'
        bbox = draw.textbbox((0, 0), test, font=font)
        if (bbox[2] - bbox[0]) <= max_width:
            cur = test
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    if len(lines) > max_lines:
        kept = lines[:max_lines]
        last = kept[-1]
        while len(last) > 4:
            bbox = draw.textbbox((0, 0), last + '…', font=font)
            if (bbox[2] - bbox[0]) <= max_width:
                break
            last = last[:-1].rstrip()
        kept[-1] = last + '…'
        return kept
    return lines


def _clean_content_excerpt_for_cover(raw_excerpt: str) -> str:
    """
    Strip repetitive copyright footers, slide IDs (e.g. `0945_05f9_c2x.scr`), bare numbers,
    and URLs from extracted PDF/EPUB opening pages so cover generation focuses on real headings and content.
    """
    if not raw_excerpt:
        return ''
    cleaned_lines: List[str] = []
    seen_low: set = set()
    for raw_ln in str(raw_excerpt).splitlines():
        ln = raw_ln.strip()
        if not ln:
            continue
        low = ln.lower()
        if any(
            pat in low
            for pat in (
                'copyright',
                'all rights reserved',
                'printed in usa',
                'www.',
                'http://',
                'https://',
                '.scr',
                'isbn ',
            )
        ):
            continue
        if re.match(r'^[\d\s\-_./©]+$', ln):
            continue
        if re.match(r'^[0-9a-f_]{6,}$', low):
            continue
        if low in seen_low:
            continue
        seen_low.add(low)
        cleaned_lines.append(ln)
    return '\n'.join(cleaned_lines)[:1800]


def _is_raw_filename_or_junk_title(t: str) -> bool:
    s = (t or '').strip()
    if not s or len(s) < 3:
        return True
    if re.search(r'\.(?:pdf|epub|mobi|azw3?|djvu|cbz|cbr|txt|doc|docx|ppt|pptx)$', s, flags=re.I):
        return True
    if '_' in s or re.match(r'^\d+[\-_][a-z0-9]+', s, flags=re.I):
        return True
    if re.match(r'^(?:97[89]\d{10}|\d{9}[\dXx])\b', s):
        return True
    return False


def _design_cover_spec_with_ai_or_heuristics(
    title: str,
    subtitle: str,
    authors: str,
    publisher: str,
    tags: List[str],
    folder: str,
    description: str,
    content_excerpt: str,
    physical_file_name: str,
    style_hint: str,
    version_num: int,
    item_seed_str: str = '',
) -> Dict[str, Any]:
    """
    Use Gemini AI (`gemini-3.5-flash-lite`) + contextual heuristics to create a bespoke visual design spec
    (including clean content-derived title, subtitle, key concepts, palette, motif, and custom vector artwork)
    based on the book's actual file contents, description, filename, and tags.
    """
    import hashlib

    LAYOUT_STYLES = [
        'modern_publisher',
        'cinematic_fullbleed',
        'classic_hardcover',
        'swiss_editorial',
        'vintage_pulp',
        'minimalist_emblem',
    ]
    MOTIFS = [
        'network_nodes',
        'cosmic_nebula',
        'arcane_astrolabe',
        'circuit_prism',
        'horizon_sunset',
        'topographic_waves',
        'noir_skyline',
        'botanical_crest',
        'code_matrix',
        'architectural_arch',
    ]
    PALETTES = [
        # Deep Cyber / Navy & Cyan Gold
        {'bg_top': '#071126', 'bg_mid': '#0f2547', 'bg_bottom': '#050b16', 'accent_primary': '#38bdf8', 'accent_secondary': '#f59e0b', 'text_light': '#f8fafc'},
        # Crimson Velvet & Warm Gold
        {'bg_top': '#2a0815', 'bg_mid': '#4c1127', 'bg_bottom': '#14040a', 'accent_primary': '#fbbf24', 'accent_secondary': '#fb7185', 'text_light': '#fff1f2'},
        # Emerald Forest & Copper
        {'bg_top': '#062019', 'bg_mid': '#0d3b2e', 'bg_bottom': '#03110d', 'accent_primary': '#34d399', 'accent_secondary': '#fbbf24', 'text_light': '#ecfdf5'},
        # Royal Amethyst & Electric Violet
        {'bg_top': '#170b2e', 'bg_mid': '#2e1659', 'bg_bottom': '#0b0518', 'accent_primary': '#c084fc', 'accent_secondary': '#38bdf8', 'text_light': '#faf5ff'},
        # Sunset Amber & Deep Indigo
        {'bg_top': '#1e1b4b', 'bg_mid': '#431407', 'bg_bottom': '#090d16', 'accent_primary': '#fb923c', 'accent_secondary': '#fde047', 'text_light': '#fff7ed'},
        # Teak / Archival Bronze & Cream
        {'bg_top': '#1c1917', 'bg_mid': '#292524', 'bg_bottom': '#0c0a09', 'accent_primary': '#eab308', 'accent_secondary': '#a8a29e', 'text_light': '#fafaf9'},
        # Teal Ocean & Coral
        {'bg_top': '#042f2e', 'bg_mid': '#115e59', 'bg_bottom': '#021717', 'accent_primary': '#2dd4bf', 'accent_secondary': '#fb7185', 'text_light': '#f0fdfa'},
        # Cobalt Blueprint & Ice Silver
        {'bg_top': '#0c1938', 'bg_mid': '#1d3557', 'bg_bottom': '#060c1c', 'accent_primary': '#60a5fa', 'accent_secondary': '#a8dadc', 'text_light': '#f1faee'},
    ]

    clean_excerpt = _clean_content_excerpt_for_cover(content_excerpt)
    clean_phys_name = _clean_book_stem_for_grouping(physical_file_name) if physical_file_name else ''

    # Determine a clean fallback title if the DB title is still a raw filename like `309_c2x.PDF`
    resolved_title = title
    resolved_subtitle = subtitle
    if _is_raw_filename_or_junk_title(resolved_title):
        # Check if the opening content has a clear title heading on the first few lines
        excerpt_lines = [ln.strip(' •-*') for ln in clean_excerpt.splitlines() if 4 <= len(ln.strip(' •-*')) <= 65]
        non_generic_lines = [
            ln for ln in excerpt_lines
            if ln.lower() not in ('overview', 'table of contents', 'contents', 'introduction', 'agenda', 'summary', 'networkers')
            and not re.match(r'^session\s+\d+$', ln, flags=re.I)
        ]
        if non_generic_lines and clean_phys_name:
            first_heading = non_generic_lines[0]
            if first_heading.lower() not in clean_phys_name.lower():
                resolved_title = first_heading
                resolved_subtitle = clean_phys_name
            else:
                resolved_title = clean_phys_name
        elif clean_phys_name:
            resolved_title = clean_phys_name
        elif non_generic_lines:
            resolved_title = non_generic_lines[0]
            if len(non_generic_lines) > 1:
                resolved_subtitle = non_generic_lines[1]

    combined_text = f"{resolved_title} {resolved_subtitle} {clean_phys_name} {publisher} {' '.join(tags)} {folder} {description} {clean_excerpt[:900]} {style_hint}".lower()
    book_hash = int(
        hashlib.md5(f"{item_seed_str}:{resolved_title}:{clean_phys_name}:{clean_excerpt[:240]}".encode('utf-8', errors='ignore')).hexdigest()[:8],
        16,
    )

    # Pick smart default motif & genre badge from book subject/content, seeded by book_hash so books in the same folder vary!
    if any(k in combined_text for k in ('cisco', 'bgp', 'ibgp', 'ebgp', 'ospf', 'qos', 'network', 'routing', 'tcp', 'ipx', 'appletalk', 'cloud', 'distributed', 'microservices')):
        tech_motifs = ['network_nodes', 'circuit_prism', 'topographic_waves', 'code_matrix']
        default_motif = tech_motifs[(book_hash + version_num - 1) % len(tech_motifs)]
        default_genre = 'NETWORKING & ARCHITECTURE'
    elif any(k in combined_text for k in ('python', 'javascript', 'vue', 'bootstrap', 'linux', 'hack', 'security', 'cyber', 'devops', 'code', 'programming', 'software', 'sql', 'database', 'vmware', 'docker')):
        code_motifs = ['circuit_prism', 'code_matrix', 'network_nodes', 'minimalist_emblem' if 'minimalist_emblem' in MOTIFS else 'architectural_arch']
        default_motif = code_motifs[(book_hash + version_num - 1) % len(code_motifs)]
        default_genre = 'COMPUTER SCIENCE & TECH'
    elif any(k in combined_text for k in ('space', 'starship', 'galaxy', 'planet', 'alien', 'sci-fi', 'science fiction', 'orbit', 'quantum', 'universe', 'mars', 'future')):
        default_motif = 'cosmic_nebula'
        default_genre = 'SCIENCE FICTION'
    elif any(k in combined_text for k in ('dragon', 'magic', 'wizard', 'sword', 'fantasy', 'kingdom', 'rpg', 'dungeon', 'vampire', 'sacred', 'chronicles', 'realm', 'myth')):
        default_motif = 'arcane_astrolabe'
        default_genre = 'FANTASY & SPECULATIVE'
    elif any(k in combined_text for k in ('murder', 'detective', 'crime', 'thriller', 'mystery', 'secret', 'shadow', 'death', 'blood', 'noir', 'spy', 'investigation')):
        default_motif = 'noir_skyline'
        default_genre = 'MYSTERY & THRILLER'
    elif any(k in combined_text for k in ('play', 'drama', 'theatre', 'love', 'romance', 'poem', 'poetry', 'novel', 'memoir', 'life', 'family', 'heart', 'sweet')):
        default_motif = 'botanical_crest'
        default_genre = 'LITERATURE & DRAMA'
    elif any(k in combined_text for k in ('history', 'empire', 'war', 'ancient', 'century', 'king', 'queen', 'rome', 'law', 'philosophy', 'religion', 'politics')):
        default_motif = 'architectural_arch'
        default_genre = 'HISTORY & PHILOSOPHY'
    else:
        default_motif = MOTIFS[(book_hash + version_num - 1) % len(MOTIFS)]
        default_genre = (tags[0].upper() if tags else (folder.upper() if folder else 'FEATURED EDITION'))[:28]

    # Seed layout and palette by `book_hash + version_num` so every book in a folder gets a distinct look, and clicking Generate Cover rotates styles!
    layout_style = LAYOUT_STYLES[(book_hash + version_num - 1) % len(LAYOUT_STYLES)]
    pal = dict(PALETTES[(book_hash + version_num - 1) % len(PALETTES)])
    if version_num > 1:
        motif_idx = (MOTIFS.index(default_motif) + version_num - 1) % len(MOTIFS)
        chosen_motif = MOTIFS[motif_idx]
    else:
        chosen_motif = default_motif

    # Extract fallback key concepts & tagline from cleaned content excerpt (never copyright lines!)
    bullet_concepts: List[str] = []
    for ln in clean_excerpt.splitlines():
        s_ln = ln.strip()
        if s_ln.startswith(('•', '-', '*')) and 4 <= len(s_ln.lstrip('•-* ')) <= 34:
            cand_c = s_ln.lstrip('•-* ').strip()
            if cand_c not in bullet_concepts:
                bullet_concepts.append(cand_c)
            if len(bullet_concepts) >= 3:
                break

    fallback_tagline = resolved_subtitle
    if not fallback_tagline:
        src_text = (description or clean_excerpt or '').strip()
        if src_text:
            for cand_line in re.split(r'[\n.!?]+', src_text):
                c_str = cand_line.strip(' "\'•-*')
                if not c_str or c_str.lower() == resolved_title.lower():
                    continue
                words = c_str.split()
                if 3 <= len(words) <= 13:
                    fallback_tagline = c_str
                    break
                elif len(words) > 13:
                    fallback_tagline = ' '.join(words[:10]).rstrip(',;:') + '…'
                    break

    spec: Dict[str, Any] = {
        'cover_title': resolved_title,
        'subtitle_or_hook': fallback_tagline,
        'cover_author': authors,
        'genre_badge': default_genre,
        'key_concepts': bullet_concepts[:3],
        'illustration_prompt': '',
        'layout_style': layout_style,
        'motif': chosen_motif,
        'palette': pal,
        'custom_shapes': [],
    }

    # Call Gemini AI (`gemini-3.5-flash-lite`) for bespoke content-driven title, concepts, illustration prompt, palette & vector illustration
    try:
        epubmeta_cfg = _load_scanner_epubmeta_config()
        gemini_key = epubmeta_cfg.get('gemini_api_key') or os.environ.get('GEMINI_API_KEY') or ''
        if gemini_key:
            import requests
            gemini_model = epubmeta_cfg.get('gemini_model') or os.environ.get('GEMINI_MODEL') or 'gemini-3.5-flash-lite'
            prompt = (
                "You are a master book cover designer and cataloger. Design a bespoke, visually distinctive book cover specification "
                f"(Variation #{version_num}) based on the book's actual file contents, synopsis, and filename below.\n\n"
                f"Current DB Title: {title}\n"
                f"Physical Filename: {physical_file_name}\n"
                f"Subtitle: {subtitle}\n"
                f"Current Author(s): {authors}\n"
                f"Publisher: {publisher}\n"
                f"Tags/Category: {', '.join(tags[:8])} | Folder: {folder}\n"
                f"User Style/Prompt Hint: {style_hint}\n"
                f"Synopsis: {(description or '')[:900]}\n"
                f"Opening File Content Excerpt:\n{clean_excerpt[:1400]}\n\n"
                "IMPORTANT RULES:\n"
                "1. If Current DB Title looks like a raw filename or code (e.g. '309_c2x.PDF', '0192804936.pdf', or contains underscores/file extensions), "
                "set `cover_title` to the TRUE human-readable title of the book/presentation from the Opening File Content Excerpt and Physical Filename "
                "(for example: 'Deploying BGP' instead of '309_c2x.PDF'). Never include '.pdf' or '.epub' in `cover_title`.\n"
                "2. Set `subtitle_or_hook` to a specific 4-11 word subtitle or thematic hook summarizing the actual content (NEVER a copyright line).\n"
                "3. If Current Author(s) is a generic login/username (like 'mstory', 'admin', 'unknown') and the content/filename shows an organization or real author (e.g. 'Cisco Systems'), set `cover_author` accordingly.\n"
                "4. Extract 2 to 3 short `key_concepts` (2-4 words each, e.g. ['BGP Attributes', 'IBGP & EBGP', 'Scalable Routing']) from the content.\n"
                "5. Write a rich 2-3 sentence `illustration_prompt` describing a specific visual scene, technical architecture diagram, or thematic artwork that directly depicts the book's actual content, concepts, or story.\n\n"
                "Return ONLY a valid JSON object with these keys:\n"
                "{\n"
                '  "cover_title": "clean human-readable title for the front cover",\n'
                '  "subtitle_or_hook": "compelling 4-11 word subtitle based on the book content",\n'
                '  "cover_author": "clean author or organization name",\n'
                '  "genre_badge": "UPPERCASE 1-4 word specific topic or genre header",\n'
                '  "key_concepts": ["Concept 1", "Concept 2", "Concept 3"],\n'
                '  "illustration_prompt": "vivid 2-3 sentence visual description of the cover illustration/diagram/scene based on the book content",\n'
                f'  "layout_style": "one of {LAYOUT_STYLES} (use {layout_style} for Variation #{version_num})",\n'
                f'  "motif": "one of {MOTIFS} that best matches the subject",\n'
                '  "palette": {\n'
                '    "bg_top": "#RRGGBB (rich dark or saturated background top)",\n'
                '    "bg_mid": "#RRGGBB (midtone atmosphere color)",\n'
                '    "bg_bottom": "#RRGGBB (deep bottom color)",\n'
                '    "accent_primary": "#RRGGBB (vibrant focal illustration color)",\n'
                '    "accent_secondary": "#RRGGBB (complementary highlight color)",\n'
                '    "text_light": "#RRGGBB (high-contrast light title color)"\n'
                '  },\n'
                '  "custom_shapes": [\n'
                '    // 4 to 8 symbolic geometric/diagram elements in a 1000x1000 artwork box representing the book content:\n'
                '    // {"type": "circle"|"ring"|"polygon"|"rect"|"diamond"|"line"|"label", "cx": 500, "cy": 500, "r": 160, "w": 220, "h": 90, "text": "short diagram label if type==label", "points": [[x1,y1],[x2,y2]], "color": "primary"|"secondary"|"light", "alpha": 165, "width": 3}\n'
                '  ]\n'
                "}"
            )
            for cand_model in (gemini_model, 'gemini-3.5-flash-lite', 'gemini-3.8-flash'):
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{cand_model}:generateContent?key={gemini_key}"
                resp = requests.post(
                    url,
                    json={
                        'contents': [{'parts': [{'text': prompt}]}],
                        'generationConfig': {'temperature': 0.85, 'maxOutputTokens': 1500},
                    },
                    timeout=8.0,
                )
                if resp.status_code == 200:
                    cand_text = (
                        ((resp.json().get('candidates') or [{}])[0].get('content') or {}).get('parts') or [{}]
                    )[0].get('text') or ''
                    m_json = re.search(r'\{.*\}', cand_text, re.S)
                    if m_json:
                        ai_data = json.loads(m_json.group(0))
                        if isinstance(ai_data, dict):
                            ai_title = str(ai_data.get('cover_title') or '').strip()
                            if ai_title and not _is_raw_filename_or_junk_title(ai_title):
                                spec['cover_title'] = ai_title[:85]
                            if ai_data.get('subtitle_or_hook'):
                                sub_cand = str(ai_data['subtitle_or_hook']).strip()
                                if 'copyright' not in sub_cand.lower():
                                    spec['subtitle_or_hook'] = sub_cand[:110]
                            ai_auth = str(ai_data.get('cover_author') or '').strip()
                            if ai_auth and (not authors or authors.lower() in ('unknown', 'mstory', 'admin', 'user', '..')):
                                spec['cover_author'] = ai_auth[:70]
                            if ai_data.get('genre_badge'):
                                spec['genre_badge'] = str(ai_data['genre_badge']).strip().upper()[:34]
                            if isinstance(ai_data.get('key_concepts'), list):
                                spec['key_concepts'] = [
                                    str(x).strip()[:32]
                                    for x in ai_data['key_concepts']
                                    if str(x).strip()
                                ][:3]
                            if ai_data.get('illustration_prompt'):
                                spec['illustration_prompt'] = str(ai_data['illustration_prompt']).strip()[:600]
                            if ai_data.get('motif') in MOTIFS:
                                spec['motif'] = ai_data['motif']
                            if ai_data.get('layout_style') in LAYOUT_STYLES and version_num == 1:
                                spec['layout_style'] = ai_data['layout_style']
                            ai_pal = ai_data.get('palette')
                            if isinstance(ai_pal, dict):
                                for k in ('bg_top', 'bg_mid', 'bg_bottom', 'accent_primary', 'accent_secondary', 'text_light'):
                                    if ai_pal.get(k):
                                        spec['palette'][k] = str(ai_pal[k]).strip()
                            if isinstance(ai_data.get('custom_shapes'), list):
                                spec['custom_shapes'] = ai_data['custom_shapes'][:10]
                    break
                elif resp.status_code != 404:
                    break
    except Exception:
        pass

    return spec


def _generate_ai_cover_image_with_gemini(
    title: str,
    subtitle: str,
    authors: str,
    publisher: str,
    spec: Dict[str, Any],
    content_excerpt: str = '',
    style_hint: str = '',
    version_num: int = 1,
) -> Optional[str]:
    """
    Use Gemini's native image generation model (`gemini-3.1-flash-lite-image`) to generate a bespoke,
    flat full-bleed 2:3 book cover illustration and typography directly from the book's content,
    synopsis, and resolved title/subtitle/author.
    Returns a `data:image/jpeg;base64,...` URI (resized to 420x630), or `None` if unavailable.
    """
    import base64
    import io
    from PIL import Image

    try:
        epubmeta_cfg = _load_scanner_epubmeta_config()
        gemini_key = epubmeta_cfg.get('gemini_api_key') or os.environ.get('GEMINI_API_KEY') or ''
        if not gemini_key:
            return None

        import requests

        ART_DIRECTIONS = [
            'Modern High-Detail Technical / Editorial Book Cover with glowing diagrammatic or thematic illustration',
            'Cinematic Full-Bleed Atmospheric Concept Art Book Cover with dramatic lighting and depth',
            'Classic Hardcover Architectural / Infographic Edition with richly detailed central illustration',
            'Bold Contemporary Graphic Novel / Swiss Design Book Cover with iconic subject imagery',
            'Vintage Retro-Futuristic / Classic Print Illustration Book Cover with rich texture',
            'Minimalist Symbolic Masterpiece Book Cover with striking focal emblem and fine detail',
        ]
        art_dir = ART_DIRECTIONS[(version_num - 1) % len(ART_DIRECTIONS)]

        clean_title = (spec.get('cover_title') or title or 'Untitled Book').strip()
        clean_sub = (spec.get('subtitle_or_hook') or subtitle or '').strip()
        clean_auth = (spec.get('cover_author') or authors or publisher or '').strip()
        if clean_auth.lower() in ('unknown', 'unknown author', 'mstory', '..', '.'):
            clean_auth = publisher or ''
        concepts = [str(c).strip() for c in (spec.get('key_concepts') or []) if str(c).strip()][:3]
        illus_desc = (spec.get('illustration_prompt') or '').strip()
        clean_excerpt = _clean_content_excerpt_for_cover(content_excerpt)[:700]

        img_prompt_parts = [
            "Design a flat, full-bleed 2D front book cover filling 100% of the vertical 2:3 canvas edge-to-edge "
            "(NO 3D book mockup, NO perspective tilt, NO spine or page edges, NO white border or studio background).",
            f"Artistic Direction (Variation #{version_num}): {art_dir}.",
            f'Book Title (display prominently in crisp, legible typography at the top): "{clean_title}"',
        ]
        if clean_sub and clean_sub.lower() != clean_title.lower():
            img_prompt_parts.append(f'Subtitle (display cleanly below the title): "{clean_sub}"')
        if clean_auth:
            img_prompt_parts.append(f'Author / Organization (display at the bottom): "{clean_auth}"')
        if concepts:
            img_prompt_parts.append(f"Key Content Concepts to feature visually or in diagram callouts: {', '.join(concepts)}.")
        if illus_desc:
            img_prompt_parts.append(f"Cover Illustration & Subject Matter: {illus_desc}")
        elif clean_excerpt:
            img_prompt_parts.append(f"Base the cover illustration directly on this book content:\n{clean_excerpt}")
        if style_hint:
            img_prompt_parts.append(f"Additional User Style Request: {style_hint}")

        img_prompt = '\n'.join(img_prompt_parts)

        for img_model in ('gemini-3.1-flash-lite-image', 'gemini-3.1-flash-image', 'gemini-2.5-flash-image'):
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{img_model}:generateContent?key={gemini_key}"
            resp = requests.post(
                url,
                json={
                    'contents': [{'parts': [{'text': img_prompt}]}],
                    'generationConfig': {
                        'responseModalities': ['IMAGE', 'TEXT'],
                        'imageConfig': {'aspectRatio': '2:3'},
                    },
                },
                timeout=26.0,
            )
            if resp.status_code != 200:
                continue
            parts = ((resp.json().get('candidates') or [{}])[0].get('content') or {}).get('parts') or []
            for p in parts:
                inline = p.get('inlineData') or p.get('inline_data')
                if inline and inline.get('data'):
                    raw_bytes = base64.b64decode(inline['data'])
                    im = Image.open(io.BytesIO(raw_bytes)).convert('RGB')
                    im.thumbnail((848, 1272), Image.Resampling.LANCZOS)
                    buf = io.BytesIO()
                    im.save(buf, format='JPEG', quality=90, optimize=True)
                    b64_str = base64.b64encode(buf.getvalue()).decode('ascii')
                    return f'data:image/jpeg;base64,{b64_str}'
    except Exception:
        pass
    return None


def _render_generated_book_cover_jpeg(
    title: str,
    subtitle: str,
    authors: str,
    publisher: str,
    spec: Dict[str, Any],
    seed_int: int = 1,
) -> str:
    """
    Render a high-resolution `720x1080` book cover from `spec` using Pillow (`PIL`),
    downsample with LANCZOS to `400x600`, and return a `data:image/jpeg;base64,...` URI.
    """
    import base64
    import io
    import math
    import random
    from PIL import Image, ImageDraw

    W, H = 720, 1080
    rng = random.Random(seed_int)

    pal = spec.get('palette') or {}
    c_top = _hex_to_rgb_tuple(pal.get('bg_top', '#071126'), (7, 17, 38))
    c_mid = _hex_to_rgb_tuple(pal.get('bg_mid', '#0f2547'), (15, 37, 71))
    c_bot = _hex_to_rgb_tuple(pal.get('bg_bottom', '#050b16'), (5, 11, 22))
    c_pri = _hex_to_rgb_tuple(pal.get('accent_primary', '#38bdf8'), (56, 189, 248))
    c_sec = _hex_to_rgb_tuple(pal.get('accent_secondary', '#f59e0b'), (245, 158, 11))
    c_txt = _hex_to_rgb_tuple(pal.get('text_light', '#f8fafc'), (248, 250, 252))

    # 1. Smooth 3-stop vertical gradient background
    base = Image.new('RGBA', (W, H), (*c_top, 255))
    bg_draw = ImageDraw.Draw(base)
    half_h = H // 2
    for y in range(H):
        if y < half_h:
            t = y / max(1, half_h)
            r = int(c_top[0] + (c_mid[0] - c_top[0]) * t)
            g = int(c_top[1] + (c_mid[1] - c_top[1]) * t)
            b = int(c_top[2] + (c_mid[2] - c_top[2]) * t)
        else:
            t = (y - half_h) / max(1, H - half_h)
            r = int(c_mid[0] + (c_bot[0] - c_mid[0]) * t)
            g = int(c_mid[1] + (c_bot[1] - c_mid[1]) * t)
            b = int(c_mid[2] + (c_bot[2] - c_mid[2]) * t)
        bg_draw.line([(0, y), (W, y)], fill=(r, g, b, 255))

    art_layer = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    ad = ImageDraw.Draw(art_layer)

    layout_style = spec.get('layout_style') or 'modern_publisher'
    motif = spec.get('motif') or 'network_nodes'

    # Center of the hero artwork zone
    cx, cy = W // 2, int(H * 0.56)
    if layout_style == 'cinematic_fullbleed':
        cy = int(H * 0.52)
    elif layout_style == 'classic_hardcover':
        cy = int(H * 0.57)

    # Soft radial ambient glow behind the central illustration
    for rad in range(310, 20, -18):
        alpha = int(max(4, min(42, (320 - rad) * 0.13)))
        col = c_pri if (rad % 36 == 0) else c_sec
        ad.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=(*col, alpha))

    # 2. Render subject-matched procedural artwork motif
    if motif == 'network_nodes':
        nodes = []
        for i in range(26):
            ang = (i / 26.0) * math.tau + rng.uniform(-0.18, 0.18)
            dist = rng.uniform(45, 255)
            nx = int(cx + math.cos(ang) * dist)
            ny = int(cy + math.sin(ang) * dist * 0.88)
            nodes.append((nx, ny, rng.choice([c_pri, c_sec, c_txt]), rng.randint(5, 13)))
        nodes.append((cx, cy, c_pri, 18))
        for i in range(len(nodes)):
            x1, y1, col1, _ = nodes[i]
            for j in range(i + 1, len(nodes)):
                x2, y2, _, _ = nodes[j]
                d = math.hypot(x2 - x1, y2 - y1)
                if d < 145:
                    line_a = int(max(25, min(170, (145 - d) * 1.2)))
                    ad.line([(x1, y1), (x2, y2)], fill=(*col1, line_a), width=2)
        for ring_r in (95, 175, 245):
            ad.ellipse([cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r], outline=(*c_pri, 75), width=2)
        for nx, ny, ncol, nr in nodes:
            ad.ellipse([nx - nr - 5, ny - nr - 5, nx + nr + 5, ny + nr + 5], fill=(*ncol, 55))
            ad.ellipse([nx - nr, ny - nr, nx + nr, ny + nr], fill=(*ncol, 235), outline=(*c_txt, 200), width=2)

    elif motif in ('circuit_prism', 'code_matrix'):
        # Isometric tech grid + circuit traces + central glowing hexagon core
        for step in range(-240, 260, 40):
            ad.line([(cx + step, cy - 220), (cx + step, cy + 220)], fill=(*c_pri, 32), width=1)
            ad.line([(cx - 240, cy + step), (cx + 240, cy + step)], fill=(*c_pri, 32), width=1)
        for _ in range(18):
            sx = cx + rng.choice(range(-200, 220, 40))
            sy = cy + rng.choice(range(-200, 220, 40))
            mx = sx + rng.choice([-80, -40, 40, 80])
            ey = sy + rng.choice([-80, -40, 40, 80])
            col = c_pri if rng.random() > 0.35 else c_sec
            ad.line([(sx, sy), (mx, sy), (mx, ey)], fill=(*col, 165), width=3)
            ad.ellipse([sx - 5, sy - 5, sx + 5, sy + 5], fill=(*col, 220))
            ad.ellipse([mx - 5, ey - 5, mx + 5, ey + 5], fill=(*c_txt, 220))
        for r_hex, col_h, w_h in ((155, c_sec, 3), (115, c_pri, 5), (70, c_txt, 3)):
            pts = [
                (cx + int(r_hex * math.cos(math.radians(60 * k - 30))), cy + int(r_hex * math.sin(math.radians(60 * k - 30))))
                for k in range(6)
            ]
            ad.polygon(pts, fill=(*col_h, 38), outline=(*col_h, 225), width=w_h)

    elif motif == 'cosmic_nebula':
        # Starfield + orbital ellipses + eclipsed planet
        for _ in range(95):
            sx = rng.randint(45, W - 45)
            sy = rng.randint(240, H - 140)
            sr = rng.choice([1, 1, 2, 3])
            ad.ellipse([sx - sr, sy - sr, sx + sr, sy + sr], fill=(*c_txt, rng.randint(90, 230)))
        for rx, ry, tilt_col in ((265, 92, c_pri), (210, 145, c_sec), (150, 150, c_txt)):
            ad.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], outline=(*tilt_col, 135), width=3)
        pr = 105
        ad.ellipse([cx - pr - 12, cy - pr - 12, cx + pr + 12, cy + pr + 12], fill=(*c_pri, 75))
        ad.ellipse([cx - pr, cy - pr, cx + pr, cy + pr], fill=(*c_bot, 245), outline=(*c_sec, 220), width=4)
        ad.arc([cx - pr - 28, cy - 38, cx + pr + 28, cy + 48], start=350, end=190, fill=(*c_pri, 230), width=6)

    elif motif == 'arcane_astrolabe':
        # Sacred geometry astrolabe rings + 8-point star + compass markings
        for r_ring, a_ring, w_ring in ((235, 150, 3), (205, 95, 2), (160, 190, 3), (105, 160, 2)):
            ad.ellipse([cx - r_ring, cy - r_ring, cx + r_ring, cy + r_ring], outline=(*c_pri, a_ring), width=w_ring)
        for deg in range(0, 360, 15):
            rad_a = math.radians(deg)
            r_in = 205 if deg % 45 != 0 else 160
            x1, y1 = cx + int(r_in * math.cos(rad_a)), cy + int(r_in * math.sin(rad_a))
            x2, y2 = cx + int(235 * math.cos(rad_a)), cy + int(235 * math.sin(rad_a))
            ad.line([(x1, y1), (x2, y2)], fill=(*c_sec, 175), width=2 if deg % 45 == 0 else 1)
        for rot in (0, 45):
            pts = []
            for k in range(4):
                a = math.radians(rot + k * 90)
                pts.append((cx + int(155 * math.cos(a)), cy + int(155 * math.sin(a))))
            ad.polygon(pts, outline=(*c_sec, 210), fill=(*c_pri, 28), width=3)
        ad.ellipse([cx - 32, cy - 32, cx + 32, cy + 32], fill=(*c_sec, 210), outline=(*c_txt, 240), width=3)

    elif motif == 'noir_skyline':
        # Dramatic moon/spotlight + layered city skyline silhouette + reflection lines
        ad.ellipse([cx - 135, cy - 210, cx + 135, cy + 60], fill=(*c_sec, 185))
        base_y = cy + 165
        x_cur = 60
        while x_cur < W - 60:
            bw = rng.randint(36, 78)
            bh = rng.randint(85, 250)
            bx2 = min(W - 60, x_cur + bw)
            ad.rectangle([x_cur, base_y - bh, bx2, base_y], fill=(*c_bot, 245), outline=(*c_pri, 120), width=2)
            for wy in range(base_y - bh + 18, base_y - 18, 22):
                for wx in range(x_cur + 10, bx2 - 10, 16):
                    if rng.random() > 0.45:
                        ad.rectangle([wx, wy, wx + 7, wy + 11], fill=(*c_sec, 190))
            x_cur += bw - rng.randint(4, 12)
        for ry in range(base_y + 8, base_y + 75, 9):
            span = rng.randint(140, 260)
            ad.line([(cx - span, ry), (cx + span, ry)], fill=(*c_pri, 110), width=2)

    elif motif in ('horizon_sunset', 'topographic_waves'):
        # Radiant sun/crest + flowing topographic/mountain ridges
        ad.ellipse([cx - 125, cy - 175, cx + 125, cy + 75], fill=(*c_sec, 210), outline=(*c_txt, 180), width=3)
        for layer_i in range(7):
            y_base = cy - 15 + layer_i * 36
            freq = 0.012 + layer_i * 0.003
            phase = seed_int + layer_i * 1.3
            pts = [(55, cy + 240)]
            for x in range(55, W - 54, 12):
                y_w = y_base + int(math.sin(x * freq + phase) * 28 + math.cos(x * 0.025 - phase) * 14)
                pts.append((x, y_w))
            pts.append((W - 55, cy + 240))
            col_w = c_pri if layer_i % 2 == 0 else c_mid
            alpha_w = min(240, 110 + layer_i * 20)
            ad.polygon(pts, fill=(*col_w, alpha_w), outline=(*c_txt, 130), width=2)

    else:
        # Botanical / Classical Architectural Medallion Crest
        for r_arch in (220, 185, 145):
            ad.ellipse([cx - r_arch, cy - r_arch, cx + r_arch, cy + r_arch], outline=(*c_pri, 165), width=3)
        for k in range(12):
            ang = math.radians(k * 30)
            px = cx + int(165 * math.cos(ang))
            py = cy + int(165 * math.sin(ang))
            ad.ellipse([px - 42, py - 42, px + 42, py + 42], outline=(*c_sec, 135), width=2)
        ad.polygon([(cx, cy - 135), (cx + 115, cy), (cx, cy + 135), (cx - 115, cy)], fill=(*c_sec, 55), outline=(*c_txt, 220), width=4)
        ad.ellipse([cx - 48, cy - 48, cx + 48, cy + 48], fill=(*c_pri, 210), outline=(*c_txt, 240), width=3)

    # 3. Overlay any custom AI-designed symbolic shapes & diagram elements from `spec['custom_shapes']`
    lbl_font = _load_cover_font(17, bold=True, serif=False)
    for sh in (spec.get('custom_shapes') or []):
        if not isinstance(sh, dict):
            continue
        sh_type = str(sh.get('type') or '').lower()
        c_name = str(sh.get('color') or 'primary').lower()
        sh_rgb = c_pri if 'pri' in c_name else (c_sec if 'sec' in c_name else c_txt)
        sh_a = max(45, min(235, int(sh.get('alpha') or 160)))
        sh_w = max(1, min(8, int(sh.get('width') or 3)))
        # Map 0..1000 artwork box into the hero zone (cx-220..cx+220, cy-190..cy+190)
        sx = cx - 220 + int((float(sh.get('cx', 500) or 500) / 1000.0) * 440)
        sy = cy - 190 + int((float(sh.get('cy', 500) or 500) / 1000.0) * 380)
        sr = int((float(sh.get('r', 110) or 110) / 1000.0) * 400)
        if sh_type in ('circle', 'ring') and sr > 8:
            ad.ellipse(
                [sx - sr, sy - sr, sx + sr, sy + sr],
                fill=(*sh_rgb, sh_a // 3) if sh_type == 'circle' else None,
                outline=(*sh_rgb, sh_a),
                width=sh_w,
            )
        elif sh_type == 'diamond' and sr > 8:
            ad.polygon(
                [(sx, sy - sr), (sx + sr, sy), (sx, sy + sr), (sx - sr, sy)],
                fill=(*sh_rgb, sh_a // 4),
                outline=(*sh_rgb, sh_a),
                width=sh_w,
            )
        elif sh_type == 'rect':
            rw = int((float(sh.get('w', 180) or 180) / 1000.0) * 400) // 2
            rh = int((float(sh.get('h', 90) or 90) / 1000.0) * 360) // 2
            if rw > 8 and rh > 8:
                ad.rounded_rectangle(
                    [sx - rw, sy - rh, sx + rw, sy + rh],
                    radius=10,
                    fill=(*c_bot, 185),
                    outline=(*sh_rgb, sh_a),
                    width=sh_w,
                )
        elif sh_type in ('line', 'polygon') and isinstance(sh.get('points'), list) and len(sh['points']) >= 2:
            pts = []
            for pt in sh['points'][:8]:
                if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                    px = cx - 220 + int((float(pt[0]) / 1000.0) * 440)
                    py = cy - 190 + int((float(pt[1]) / 1000.0) * 380)
                    pts.append((px, py))
            if len(pts) >= 2:
                if sh_type == 'polygon' and len(pts) >= 3:
                    ad.polygon(pts, fill=(*sh_rgb, sh_a // 4), outline=(*sh_rgb, sh_a), width=sh_w)
                else:
                    ad.line(pts, fill=(*sh_rgb, sh_a), width=sh_w)
        elif sh_type == 'label' and sh.get('text'):
            lbl_txt = str(sh.get('text') or '').strip()[:24]
            if lbl_txt:
                tb = ad.textbbox((0, 0), lbl_txt, font=lbl_font)
                tw = tb[2] - tb[0]
                th = tb[3] - tb[1]
                ad.rounded_rectangle(
                    [sx - tw // 2 - 12, sy - th // 2 - 7, sx + tw // 2 + 12, sy + th // 2 + 7],
                    radius=10,
                    fill=(*c_bot, 225),
                    outline=(*sh_rgb, 215),
                    width=2,
                )
                ad.text((sx - tw // 2, sy - th // 2 - 2), lbl_txt, font=lbl_font, fill=(*c_txt, 250))

    base = Image.alpha_composite(base, art_layer)
    draw = ImageDraw.Draw(base)

    # 4. Layout-specific framing, borders & header/footer plaques
    if layout_style == 'classic_hardcover':
        draw.rectangle([28, 28, W - 28, H - 28], outline=(*c_pri, 210), width=4)
        draw.rectangle([40, 40, W - 40, H - 40], outline=(*c_sec, 145), width=2)
    elif layout_style == 'modern_publisher':
        draw.rectangle([0, 0, W, 18], fill=(*c_pri, 255))
        draw.rectangle([0, H - 88, W, H], fill=(*c_bot, 235))
        draw.line([(48, H - 88), (W - 48, H - 88)], fill=(*c_pri, 180), width=3)
    elif layout_style == 'swiss_editorial':
        draw.rectangle([0, 0, 22, H], fill=(*c_pri, 255))
        draw.rectangle([22, 0, 30, H], fill=(*c_sec, 220))
    else:
        draw.rectangle([22, 22, W - 22, H - 22], outline=(*c_pri, 110), width=2)

    # Dark translucent gradient scrim behind top title area & bottom author area for 100% crisp text legibility
    scrim = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(scrim)
    sd.rounded_rectangle([38, 48, W - 38, 372], radius=18, fill=(*c_bot, 185), outline=(*c_pri, 105), width=2)
    sd.rounded_rectangle([38, H - 210, W - 38, H - 42], radius=16, fill=(*c_bot, 198), outline=(*c_sec, 105), width=2)
    base = Image.alpha_composite(base, scrim)
    draw = ImageDraw.Draw(base)

    # 5. Typography: Genre Badge, Content-Resolved Title, Subtitle/Hook, Content Topic Pills, Author(s), Publisher
    use_serif = layout_style in ('classic_hardcover', 'vintage_pulp', 'minimalist_emblem')
    badge_font = _load_cover_font(20, bold=True, serif=False)
    sub_font = _load_cover_font(23, bold=False, serif=use_serif)
    pill_font = _load_cover_font(17, bold=True, serif=False)
    auth_font = _load_cover_font(30, bold=True, serif=use_serif)
    pub_font = _load_cover_font(19, bold=False, serif=False)

    genre_badge = (spec.get('genre_badge') or 'LIBRARY EDITION').strip().upper()
    if genre_badge:
        bb = draw.textbbox((0, 0), genre_badge, font=badge_font)
        bw = bb[2] - bb[0]
        bx = (W - bw) // 2
        draw.rounded_rectangle([bx - 18, 64, bx + bw + 18, 100], radius=14, fill=(*c_pri, 52), outline=(*c_pri, 200), width=2)
        draw.text((bx, 70), genre_badge, font=badge_font, fill=(*c_pri, 255))

    clean_t = (spec.get('cover_title') or title or 'Untitled Book').strip()
    t_len = len(clean_t)
    t_size = 52 if t_len <= 24 else (44 if t_len <= 45 else (38 if t_len <= 70 else 32))
    title_font = _load_cover_font(t_size, bold=True, serif=use_serif)
    t_lines = _wrap_cover_text(draw, clean_t, title_font, max_width=W - 124, max_lines=3)

    cur_y = 118
    line_step = int(t_size * 1.21)
    for ln in t_lines:
        lb = draw.textbbox((0, 0), ln, font=title_font)
        lw = lb[2] - lb[0]
        lx = (W - lw) // 2
        draw.text((lx + 2, cur_y + 3), ln, font=title_font, fill=(0, 0, 0, 210))
        draw.text((lx, cur_y), ln, font=title_font, fill=(*c_txt, 255))
        cur_y += line_step

    hook_text = (spec.get('subtitle_or_hook') or subtitle or '').strip()
    if hook_text and hook_text.lower() != clean_t.lower():
        cur_y += 8
        draw.line([(W // 2 - 90, cur_y), (W // 2 + 90, cur_y)], fill=(*c_sec, 195), width=2)
        cur_y += 11
        s_lines = _wrap_cover_text(draw, hook_text, sub_font, max_width=W - 136, max_lines=2)
        for sln in s_lines:
            sb = draw.textbbox((0, 0), sln, font=sub_font)
            sw = sb[2] - sb[0]
            sx = (W - sw) // 2
            draw.text((sx + 1, cur_y + 2), sln, font=sub_font, fill=(0, 0, 0, 190))
            draw.text((sx, cur_y), sln, font=sub_font, fill=(*c_sec, 245))
            cur_y += 28

    # Render content-derived key concept pills above the author footer
    concepts = [str(c).strip() for c in (spec.get('key_concepts') or []) if str(c).strip()][:3]
    if concepts:
        pill_widths = []
        for cp in concepts:
            cb = draw.textbbox((0, 0), cp, font=pill_font)
            pill_widths.append((cp, (cb[2] - cb[0]) + 28))
        total_pw = sum(pw for _, pw in pill_widths) + max(0, len(pill_widths) - 1) * 12
        if total_pw <= W - 76:
            px_cur = (W - total_pw) // 2
            py_top = H - 262
            for cp, pw in pill_widths:
                draw.rounded_rectangle(
                    [px_cur, py_top, px_cur + pw, py_top + 34],
                    radius=17,
                    fill=(*c_bot, 220),
                    outline=(*c_pri, 185),
                    width=2,
                )
                draw.text((px_cur + 14, py_top + 7), cp, font=pill_font, fill=(*c_txt, 245))
                px_cur += pw + 12

    # Bottom Author & Publisher block
    clean_a = (spec.get('cover_author') or authors or '').strip()
    if clean_a.lower() in ('', 'unknown', 'unknown author', '..', '.'):
        clean_a = publisher or 'MyLibrary Collection'
    a_lines = _wrap_cover_text(draw, clean_a, auth_font, max_width=W - 124, max_lines=2)
    ay = H - 180 if len(a_lines) > 1 else H - 164
    for aln in a_lines:
        ab = draw.textbbox((0, 0), aln, font=auth_font)
        aw = ab[2] - ab[0]
        ax = (W - aw) // 2
        draw.text((ax + 2, ay + 2), aln, font=auth_font, fill=(0, 0, 0, 200))
        draw.text((ax, ay), aln, font=auth_font, fill=(*c_txt, 255))
        ay += 38

    clean_pub = (publisher or '').strip()
    if clean_pub and clean_pub.lower() != clean_a.lower():
        pub_lbl = clean_pub.upper()[:42]
        pb = draw.textbbox((0, 0), pub_lbl, font=pub_font)
        pw = pb[2] - pb[0]
        px = (W - pw) // 2
        draw.text((px, H - 78), pub_lbl, font=pub_font, fill=(*c_pri, 225))

    rgb_final = base.convert('RGB')
    buf = io.BytesIO()
    rgb_final.save(buf, format='JPEG', quality=90, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode('ascii')
    return f'data:image/jpeg;base64,{b64}'


def generate_book_cover_for_item(
    item_or_id: Any,
    file_id: str = '',
    file_path: str = '',
    isbn: str = '',
    title: str = '',
    authors: str = '',
    publisher: str = '',
    description: str = '',
    style_hint: str = '',
    query: str = '',
    apply_immediately: bool = True,
) -> Dict[str, Any]:
    """
    On-demand Book Cover Generator (`POST /mylibrary/api/cover-generate/`):
      1. Snapshots the book's original cover in `catalog_original_covers` (if not already backed up)
         so the user can always revert to the original cover with 1 click.
      2. Inspects the book's title, subtitle, author, publisher, tags/folder, synopsis/description,
         and opening content from the physical EPUB/PDF file on the NAS.
      3. Uses Gemini AI + procedural/vector illustration synthesis to generate a new custom cover.
      4. Saves the generated cover into `catalog_generated_covers` in PostgreSQL `BookDB`
         (adding a new row each time a cover is generated).
      5. Applies the generated cover to `CatalogItem` in BookDB (when `apply_immediately=True`).
    """
    import hashlib

    item = resolve_catalog_item_by_id_or_fallback(
        item_or_id,
        file_id=file_id,
        file_path=file_path,
        isbn=isbn,
        title=title,
        authors=authors,
        query=query,
    )
    if not item:
        return {'ok': False, 'error': f'CatalogItem not found: {item_or_id}'}

    _ensure_generated_covers_table()
    orig_backup = _snapshot_original_cover_if_needed(item)
    existing_gens = get_saved_generated_covers_for_item(item.id)
    version_num = len(existing_gens) + 1

    eff_title = (title or item.title or 'Untitled Book').strip()
    eff_subtitle = ''
    if ':' in eff_title:
        parts = eff_title.split(':', 1)
        if len(parts[0].strip()) >= 3:
            eff_title = parts[0].strip()
            eff_subtitle = parts[1].strip()
    elif ' - ' in eff_title:
        parts = eff_title.split(' - ', 1)
        if len(parts[0].strip()) >= 4:
            eff_title = parts[0].strip()
            eff_subtitle = parts[1].strip()

    eff_authors = (authors or item.authors or '').strip()
    eff_pub = (publisher or item.publisher or '').strip()
    eff_desc = (description or item.description or '').strip()

    # Gather tags and folder context
    tags_list: List[str] = []
    folder_name = ''
    primary_file_path = file_path
    try:
        idx_tags = get_enriched_tags_and_folders_index(need_paths=True)
        item_paths = {str(f.file_path) for f in item.files.all() if f.file_path}
        if file_path:
            item_paths.add(file_path)
        for p_str in item_paths:
            if not primary_file_path:
                primary_file_path = p_str
            rel = p_str
            for pref in ('/volume1/common/Media/Books/', '/books/'):
                if rel.startswith(pref):
                    rel = rel[len(pref):]
                    break
            if '/' in rel and not folder_name:
                folder_name = rel.split('/', 1)[0].strip()
        for t_name, t_paths in (idx_tags.get('tag_to_paths') or {}).items():
            if item_paths & set(t_paths):
                tags_list.append(t_name)
                if len(tags_list) >= 8:
                    break
    except Exception:
        pass

    # Always pull opening content excerpt & physical filename from the EPUB/PDF so even un-enriched books get accurate titles & topics!
    content_excerpt = ''
    physical_file_name = os.path.basename(primary_file_path.replace('\\', '/')) if primary_file_path else ''
    if primary_file_path:
        try:
            prev = extract_book_preview_data(file_path=primary_file_path, item_id=str(item.id))
            if prev.get('file_name'):
                physical_file_name = str(prev['file_name'])
            pages = prev.get('pages') or []
            chunks = [str(p.get('text') or '').strip() for p in pages[:5] if p.get('text')]
            content_excerpt = '\n'.join(chunks)[:2400]
        except Exception:
            content_excerpt = ''

    spec = _design_cover_spec_with_ai_or_heuristics(
        title=eff_title,
        subtitle=eff_subtitle,
        authors=eff_authors,
        publisher=eff_pub,
        tags=tags_list,
        folder=folder_name,
        description=eff_desc,
        content_excerpt=content_excerpt,
        physical_file_name=physical_file_name,
        style_hint=style_hint or query,
        version_num=version_num,
        item_seed_str=str(item.id),
    )

    display_title = (spec.get('cover_title') or eff_title).strip()
    display_authors = (spec.get('cover_author') or eff_authors).strip()

    cover_data_uri = _generate_ai_cover_image_with_gemini(
        title=display_title,
        subtitle=eff_subtitle,
        authors=display_authors,
        publisher=eff_pub,
        spec=spec,
        content_excerpt=content_excerpt,
        style_hint=style_hint or query,
        version_num=version_num,
    )
    used_ai_image = bool(cover_data_uri)

    if not cover_data_uri:
        seed_int = int(
            hashlib.md5(f"{item.id}:{display_title}:{version_num}:{time.time()}".encode('utf-8', errors='ignore')).hexdigest()[:8],
            16,
        )
        cover_data_uri = _render_generated_book_cover_jpeg(
            title=display_title,
            subtitle=eff_subtitle,
            authors=display_authors,
            publisher=eff_pub,
            spec=spec,
            seed_int=seed_int,
        )

    layout_pretty = 'AI Illustration' if used_ai_image else str(spec.get('layout_style') or 'modern').replace('_', ' ').title()
    style_label = f"🎨 Generated #{version_num} ({layout_pretty})"
    prompt_summary = f"{display_title} | {spec.get('genre_badge', '')} | {spec.get('illustration_prompt') or spec.get('subtitle_or_hook', '')}"[:490].strip(' |')

    new_gen_id = None
    try:
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO catalog_generated_covers (item_id, cover_data_uri, style_label, prompt_summary, created_at)
                VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                RETURNING id
                """,
                [str(item.id), cover_data_uri, style_label[:128], prompt_summary[:500]],
            )
            row = cur.fetchone()
            if row:
                new_gen_id = int(row[0])
    except Exception:
        pass

    # If the book in BookDB still had a raw filename/code title (e.g. `309_c2x.PDF`) or placeholder author (`mstory`),
    # persist the content-resolved title, author, and synopsis to CatalogItem so searching by the cover title finds it!
    metadata_updated = False
    update_fields_meta: List[str] = []
    sub_hook = str(spec.get('subtitle_or_hook') or '').strip()
    if display_title and not _is_raw_filename_or_junk_title(display_title) and _is_raw_filename_or_junk_title(item.title or ''):
        full_new_title = f"{display_title}: {sub_hook}" if (sub_hook and sub_hook.lower() not in display_title.lower() and len(display_title) + len(sub_hook) <= 95) else display_title
        item.title = full_new_title[:490]
        update_fields_meta.append('title')
        metadata_updated = True
    cur_auth_low = (item.authors or '').strip().lower()
    if display_authors and cur_auth_low in ('', 'unknown', 'unknown author', 'mstory', 'admin', 'user', '..', '.'):
        item.authors = display_authors[:490]
        update_fields_meta.append('authors')
        metadata_updated = True
    if not (item.description or '').strip():
        synth_desc_parts = [p for p in (sub_hook, spec.get('illustration_prompt') or '') if p]
        if synth_desc_parts:
            item.description = ' — '.join(synth_desc_parts)[:1900]
            update_fields_meta.append('description')
            metadata_updated = True
    if update_fields_meta:
        update_fields_meta.append('updated_at')
        try:
            item.save(update_fields=update_fields_meta)
        except Exception:
            pass

    apply_res: Dict[str, Any] = {}
    if apply_immediately:
        apply_res = apply_book_cover_to_item(
            item,
            cover_data_uri,
            file_id=file_id,
            file_path=primary_file_path,
            isbn=isbn,
            title=item.title or display_title or eff_title,
            authors=item.authors or display_authors or eff_authors,
            user_selected=True,
        )

    saved_list = get_saved_generated_covers_for_item(item.id)
    return {
        'ok': True,
        'item_id': str(item.id),
        'generated_id': new_gen_id,
        'version_num': version_num,
        'style_label': style_label,
        'prompt_summary': prompt_summary,
        'cover_url': cover_data_uri,
        'title': item.title or display_title or eff_title,
        'authors': item.authors or display_authors or eff_authors,
        'description': item.description or eff_desc,
        'metadata_updated': metadata_updated,
        'has_original_cover': bool(orig_backup),
        'original_cover': orig_backup or '',
        'saved_generated_covers': saved_list,
        'missing_cover_count': apply_res.get('missing_cover_count'),
    }


def generate_ai_description_from_content(
    item_or_id: Any = None,
    file_id: str = '',
    file_path: str = '',
    isbn: str = '',
    title: str = '',
    authors: str = '',
    publisher: str = '',
    apply_immediately: bool = True,
) -> Dict[str, Any]:
    """
    On-demand AI Content-Based Description Generator (`POST /mylibrary/api/ai-description/`):
    Designed specifically for books that have no description and resulted in a No Match
    (`NOT_FOUND` / `NO_ISBN`), reading the actual content inside the EPUB/PDF/ebook file
    (front matter, table of contents, preface, opening chapters, and/or rendered page images
    for scanned PDFs) and synthesizing a grounded 2-4 sentence synopsis via Gemini AI.
    """
    import base64
    import sys

    item: Optional[CatalogItem] = None
    if item_or_id or file_id or file_path or isbn or title:
        item = resolve_catalog_item_by_id_or_fallback(
            item_or_id,
            file_id=file_id,
            file_path=file_path,
            isbn=isbn,
            title=title,
            authors=authors,
        )

    resolved_loc: Optional[FileLocation] = None
    raw_path = (file_path or '').strip()

    if file_id:
        resolved_loc = FileLocation.objects.select_related('catalog_item').filter(id=file_id).first()
        if resolved_loc:
            if not item and resolved_loc.catalog_item:
                item = resolved_loc.catalog_item
            if not raw_path:
                raw_path = resolved_loc.file_path or ''

    if not raw_path and item:
        files_all = list(item.files.all())
        for ext_pref in ('.epub', '.pdf', '.mobi', '.azw3', '.fb2', '.txt'):
            cand = next((f for f in files_all if (f.file_path or '').lower().endswith(ext_pref)), None)
            if cand:
                resolved_loc = cand
                raw_path = cand.file_path or ''
                break
        if not raw_path and files_all:
            resolved_loc = files_all[0]
            raw_path = files_all[0].file_path or ''

    phys_path = _resolve_physical_file_path(raw_path) if raw_path else None
    if (not phys_path or not phys_path.exists()) and item:
        for cand in item.files.all():
            if cand.file_path:
                cand_phys = _resolve_physical_file_path(cand.file_path)
                if cand_phys and cand_phys.exists():
                    resolved_loc = cand
                    raw_path = cand.file_path
                    phys_path = cand_phys
                    break

    if not phys_path or not phys_path.exists():
        return {
            'ok': False,
            'error': f'Physical book file not accessible on NAS: {raw_path or file_id or item_or_id}',
        }

    base_dir = Path(getattr(settings, 'BASE_DIR', '.'))
    for sub in ('scanner', 'nas_scanner'):
        cand_dir = str((base_dir / sub).resolve())
        if os.path.isdir(cand_dir):
            if cand_dir in sys.path:
                sys.path.remove(cand_dir)
            sys.path.insert(0, cand_dir)

    epubmeta_cfg = _load_scanner_epubmeta_config()
    gemini_key = epubmeta_cfg.get('gemini_api_key') or os.environ.get('GEMINI_API_KEY') or ''
    if not gemini_key:
        return {
            'ok': False,
            'error': 'Gemini API key is not configured (set gemini_api_key in scanner config or GEMINI_API_KEY env var).',
        }
    gemini_model = epubmeta_cfg.get('gemini_model') or os.environ.get('GEMINI_MODEL') or 'gemini-3.5-flash-lite'

    # 1. Extract rich content text (front matter + TOC + opening chapters/pages) and fallback page images for scanned PDFs
    content_chunks: List[str] = []
    page_images_bytes: List[bytes] = []
    emb_title = ''
    emb_authors = ''
    emb_pub = ''

    try:
        from epubmeta.readers import get_reader
        rdr = get_reader(phys_path)
        if rdr:
            try:
                emb = rdr.read_metadata()
                if emb:
                    emb_title = (emb.title or '').strip()
                    emb_authors = ', '.join(a.strip() for a in (emb.authors or []) if a and a.strip())
                    emb_pub = (emb.publisher or '').strip()
            except Exception:
                pass
            try:
                ft = rdr.extract_front_text(max_pages=24) or ''
                if ft.strip():
                    content_chunks.append(ft.strip())
            except Exception:
                pass
            if sum(len(c) for c in content_chunks) < 350:
                try:
                    imgs = rdr.extract_front_images(max_pages=4) or []
                    for im_b in imgs[:4]:
                        if im_b and len(im_b) > 500:
                            page_images_bytes.append(im_b)
                except Exception:
                    pass
    except Exception:
        pass

    # Supplement with deeper chapter/page sampling via preview/PyMuPDF if front matter was short (e.g. cover/copyright only)
    combined_text = '\n\n'.join(content_chunks).strip()
    if len(combined_text) < 1200 and phys_path.suffix.lower() == '.pdf':
        try:
            import pymupdf
            doc = pymupdf.open(phys_path)
            try:
                n_pages = len(doc)
                sample_indices = list(range(min(18, n_pages)))
                if n_pages > 20:
                    mid = min(n_pages - 1, max(18, n_pages // 5))
                    sample_indices.extend(range(mid, min(n_pages, mid + 4)))
                extra_txt: List[str] = []
                seen_idx = set()
                for pi in sample_indices:
                    if pi in seen_idx:
                        continue
                    seen_idx.add(pi)
                    ptxt = (doc[pi].get_text() or '').strip()
                    if ptxt:
                        extra_txt.append(f'[Page {pi + 1}]\n{ptxt[:2500]}')
                if extra_txt:
                    combined_text = '\n\n'.join(extra_txt).strip()
                if len(combined_text) < 350 and not page_images_bytes:
                    for pi in range(min(4, n_pages)):
                        pix = doc[pi].get_pixmap(dpi=120)
                        page_images_bytes.append(pix.tobytes('png'))
            finally:
                doc.close()
        except Exception:
            pass
    elif len(combined_text) < 800:
        try:
            prev = extract_book_preview_data(file_path=raw_path or str(phys_path), item_id=str(item.id) if item else None)
            if prev.get('ok'):
                p_texts = [f"[{p.get('label', 'Section')}]\n{str(p.get('text') or '').strip()}" for p in (prev.get('pages') or []) if str(p.get('text') or '').strip()]
                if p_texts and len('\n\n'.join(p_texts)) > len(combined_text):
                    combined_text = '\n\n'.join(p_texts)
                if len(combined_text) < 350 and not page_images_bytes:
                    for im_obj in (prev.get('images') or [])[:4]:
                        d_uri = str(im_obj.get('data_uri') or '')
                        if d_uri.startswith('data:image') and ',' in d_uri:
                            page_images_bytes.append(base64.b64decode(d_uri.split(',', 1)[1]))
        except Exception:
            pass

    if not combined_text.strip() and not page_images_bytes:
        return {
            'ok': False,
            'error': f'Could not extract readable text or page images from {phys_path.name} to summarize.',
        }

    eff_title = (title or (item.title if item else '') or emb_title or phys_path.stem).strip()
    eff_authors = (authors or (item.authors if item else '') or emb_authors or '').strip()
    eff_pub = (publisher or (item.publisher if item else '') or emb_pub or '').strip()
    rel_hint = raw_path or str(phys_path)

    # Gather folder & tag context if available
    folder_name = ''
    for pref in ('/volume1/common/Media/Books/', '/books/'):
        if rel_hint.startswith(pref):
            rem = rel_hint[len(pref):]
            if '/' in rem:
                folder_name = rem.split('/', 1)[0].strip()
            break

    ai_parsed: Dict[str, Any] = {}
    last_ai_err = ''

    # 2. Try GeminiBookExtractor first (tracks daily AI token usage in scanner_state.db)
    tracker = None
    try:
        from epubmeta.isbn.ai_extractor import GeminiBookExtractor
        from epubmeta.state import StateTracker
        db_path = _get_scanner_db_path()
        if db_path:
            tracker = StateTracker(db_path)
        max_daily_cost = epubmeta_cfg.get('max_daily_cost_usd')
        extractor = GeminiBookExtractor(
            api_key=gemini_key,
            model=gemini_model,
            raise_on_quota=False,
            state_tracker=tracker,
            max_daily_cost_usd=max_daily_cost,
            batch_mode=False,
        )
        if hasattr(extractor, 'generate_description_from_content'):
            ai_parsed = extractor.generate_description_from_content(
                text=combined_text[:16000] if combined_text else None,
                images=page_images_bytes[:4] if page_images_bytes else None,
                title=eff_title,
                authors=eff_authors,
                publisher=eff_pub,
                file_path_hint=rel_hint,
            ) or {}
    except Exception as ext_err:
        last_ai_err = str(ext_err)

    # 3. Direct Gemini REST fallback across candidate models (works even if google-genai SDK is absent in web container)
    if not (ai_parsed.get('description') or '').strip():
        try:
            import requests
            prompt_text = (
                "You are an expert literary and technical book cataloger. "
                "This book/document did not match external ISBN catalogs and currently has no description. "
                "Carefully read the provided actual book content (front matter, table of contents, preface, "
                "introduction, opening chapters, and/or rendered page images) along with the file clues below, "
                "and write an accurate, informative 2-4 sentence description (synopsis) grounded strictly in the book's content.\n\n"
                f"Current Title: {eff_title}\n"
                f"Current Author(s): {eff_authors}\n"
                f"Current Publisher: {eff_pub}\n"
                f"Collection / Folder: {folder_name}\n"
                f"File Path: {rel_hint}\n\n"
            )
            if combined_text.strip():
                prompt_text += f"--- EXTRACTED BOOK CONTENT ---\n{combined_text[:15000]}\n--- END CONTENT ---\n\n"
            prompt_text += (
                "Respond ONLY with a valid JSON object with these keys:\n"
                "{\n"
                '  "description": "2-4 sentence informative synopsis based strictly on the document content",\n'
                '  "title": "clean human-readable title if identifiable from content, else null",\n'
                '  "authors": ["author 1", ...],\n'
                '  "publisher": "publisher or organization if identifiable, else null",\n'
                '  "subjects": ["subject/topic 1", "subject/topic 2", ...]\n'
                "}\n"
                "Rules: Base the description directly on the actual content inside the book. "
                "Never output generic filler like 'This PDF document contains pages.' Keep it 2-4 sentences (40-110 words)."
            )
            parts_payload: List[Dict[str, Any]] = [{'text': prompt_text}]
            for im_b in page_images_bytes[:4]:
                parts_payload.append({
                    'inlineData': {
                        'mimeType': 'image/png',
                        'data': base64.b64encode(im_b).decode('ascii'),
                    }
                })
            seen_models = []
            for cand_m in (gemini_model, 'gemini-3.5-flash-lite', 'gemini-2.5-flash-lite', 'gemini-2.5-flash', 'gemini-2.0-flash'):
                if not cand_m or cand_m in seen_models:
                    continue
                seen_models.append(cand_m)
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{cand_m}:generateContent?key={gemini_key}"
                resp = requests.post(
                    url,
                    json={
                        'contents': [{'parts': parts_payload}],
                        'generationConfig': {'temperature': 0.35, 'maxOutputTokens': 700},
                    },
                    timeout=18.0,
                )
                if resp.status_code == 200:
                    resp_json = resp.json()
                    cand_text = (
                        ((resp_json.get('candidates') or [{}])[0].get('content') or {}).get('parts') or [{}]
                    )[0].get('text') or ''
                    m_json = re.search(r'\{.*\}', cand_text, re.S)
                    if m_json:
                        parsed_obj = json.loads(m_json.group(0))
                        if isinstance(parsed_obj, dict) and parsed_obj.get('description'):
                            ai_parsed = parsed_obj
                            if tracker and hasattr(tracker, 'record_ai_usage'):
                                try:
                                    um = resp_json.get('usageMetadata') or {}
                                    in_tok = int(um.get('promptTokenCount') or max(len(prompt_text) // 3, 600))
                                    out_tok = int(um.get('candidatesTokenCount') or 120)
                                    cost_est = (in_tok / 1_000_000.0) * 0.10 + (out_tok / 1_000_000.0) * 0.40
                                    tracker.record_ai_usage(input_tokens=in_tok, output_tokens=out_tok, cost_usd=cost_est)
                                except Exception:
                                    pass
                            break
                else:
                    last_ai_err = f'HTTP {resp.status_code}: {resp.text[:180]}'
                    if resp.status_code in (402, 429):
                        break
        except Exception as rest_err:
            last_ai_err = str(rest_err)

    # 4. Fallback if Gemini API is unreachable: synthesize a clean 2-4 sentence synopsis
    #    strictly from THIS book's own extracted Preface / Introduction / opening prose.
    if not (ai_parsed.get('description') or '').strip() and combined_text.strip():
        raw_lines = [ln.strip() for ln in combined_text.splitlines()]
        noise_re = re.compile(
            r'^(?:\[Page\s+\d+\]|\[Section.*\]|page\s+\d+|table of contents|contents|copyright|all rights reserved|'
            r'isbn[\s:-]|printed in|published by|library of congress|www\.|http://|https://|\d+\s*$)',
            re.I,
        )
        # Look for Preface / Introduction / Abstract / Overview / About This Book section first
        prose_paras: List[str] = []
        cur_buf: List[str] = []
        in_intro_sec = False
        intro_hdr_re = re.compile(r'^(?:preface|introduction|overview|abstract|about this book|summary|foreword)\b', re.I)
        for ln in raw_lines:
            if not ln:
                if cur_buf:
                    para = re.sub(r'\s+', ' ', ' '.join(cur_buf)).strip()
                    cur_buf = []
                    if len(para) >= 80 and not noise_re.match(para) and sum(c.isalpha() for c in para) > len(para) * 0.55:
                        if in_intro_sec:
                            prose_paras.insert(0, para)
                        else:
                            prose_paras.append(para)
                continue
            if intro_hdr_re.match(ln) and len(ln) < 60:
                in_intro_sec = True
                continue
            if noise_re.match(ln):
                continue
            # Skip TOC leader lines like "Chapter 1 ..... 12"
            if re.search(r'\.{3,}\s*\d+$', ln) or re.match(r'^(?:chapter|part|section)\s+\d+\b', ln, re.I):
                continue
            cur_buf.append(ln)
        if cur_buf:
            para = re.sub(r'\s+', ' ', ' '.join(cur_buf)).strip()
            if len(para) >= 80 and not noise_re.match(para) and sum(c.isalpha() for c in para) > len(para) * 0.55:
                prose_paras.append(para)

        if prose_paras:
            best_text = re.sub(r'(\w)-\s+(\w)', r'\1\2', ' '.join(prose_paras[:5]))
            all_sents = [s.strip() for s in re.split(r'(?<=[.!?])\s+', best_text) if len(s.strip()) >= 40 and s.strip()[0].isupper() and s.strip()[-1] in '.!?']
            clean_sents = [s for s in all_sents if not re.search(r'\[\d+\]|©|ISBN|doi:|pp\.\s*\d', s)]
            chosen = (clean_sents if len(clean_sents) >= 2 else all_sents)[:4]
            if chosen:
                ai_parsed['description'] = ' '.join(chosen)[:900]
            else:
                ai_parsed['description'] = best_text[:650]

    gen_desc = _strip_html(str(ai_parsed.get('description') or '').strip())
    if not gen_desc:
        return {
            'ok': False,
            'error': f'Gemini AI could not generate a description from book content ({last_ai_err or "empty response"}).',
        }

    sugg_title = str(ai_parsed.get('title') or '').strip()
    raw_sugg_authors = ai_parsed.get('authors') or []
    if isinstance(raw_sugg_authors, str):
        raw_sugg_authors = [a.strip() for a in raw_sugg_authors.split(',') if a.strip()]
    sugg_authors = format_normalized_authors_str(raw_sugg_authors) or ', '.join(str(a).strip() for a in raw_sugg_authors if str(a).strip())
    sugg_pub = str(ai_parsed.get('publisher') or '').strip()
    sugg_subjects = [str(s).strip() for s in (ai_parsed.get('subjects') or []) if str(s).strip()][:8]

    applied = False
    if apply_immediately and item is not None:
        item.description = gen_desc[:4000]
        upd_fields = ['description', 'updated_at']
        item.save(update_fields=upd_fields)
        applied = True

    # Also update candidate_metadata_json / updated_metadata_json in scanner_state.db if tracked,
    # while preserving orig_description untouched so Revert to Original remains non-destructive.
    if apply_immediately:
        try:
            db_path = _get_scanner_db_path()
            path_variants: List[str] = []
            for p_cand in [raw_path, str(phys_path)] + ([str(f.file_path) for f in item.files.all() if f.file_path] if item else []):
                if not p_cand:
                    continue
                if p_cand not in path_variants:
                    path_variants.append(p_cand)
                if p_cand.startswith('/books/'):
                    v_nas = '/volume1/common/Media/Books/' + p_cand[len('/books/'):]
                    if v_nas not in path_variants:
                        path_variants.append(v_nas)
                elif p_cand.startswith('/volume1/common/Media/Books/'):
                    v_dk = '/books/' + p_cand[len('/volume1/common/Media/Books/'):]
                    if v_dk not in path_variants:
                        path_variants.append(v_dk)

            if path_variants and (is_postgres_scanner_enabled() or (db_path and db_path.exists())):
                conn = connect_scanner_db(db_path, timeout=8.0)
                conn.row_factory = sqlite3.Row
                try:
                    cur = conn.cursor()
                    placeholders = ','.join(['?'] * len(path_variants))
                    cur.execute(
                        f"SELECT file_path, candidate_metadata_json, updated_metadata_json FROM processed_files WHERE file_path IN ({placeholders})",
                        tuple(path_variants),
                    )
                    rows = cur.fetchall()
                    for r in rows:
                        cand_d = {}
                        upd_d = {}
                        try:
                            cand_d = json.loads(r['candidate_metadata_json'] or '{}') if r['candidate_metadata_json'] else {}
                        except Exception:
                            cand_d = {}
                        try:
                            upd_d = json.loads(r['updated_metadata_json'] or '{}') if r['updated_metadata_json'] else {}
                        except Exception:
                            upd_d = {}
                        cand_d['description'] = gen_desc
                        if sugg_subjects and not cand_d.get('subjects'):
                            cand_d['subjects'] = sugg_subjects
                        upd_d['description'] = gen_desc
                        cur.execute(
                            "UPDATE processed_files SET candidate_metadata_json = ?, updated_metadata_json = ? WHERE file_path = ?",
                            (json.dumps(cand_d, ensure_ascii=False), json.dumps(upd_d, ensure_ascii=False), r['file_path']),
                        )
                    if rows:
                        conn.commit()
                finally:
                    conn.close()

            diag_map = _ENRICHED_PATHS_CACHE.get('diag_by_path')
            if isinstance(diag_map, dict):
                for pv in path_variants:
                    if pv in diag_map and isinstance(diag_map[pv], dict):
                        diag_map[pv]['candidate_description'] = gen_desc
        except Exception:
            pass

    return {
        'ok': True,
        'item_id': str(item.id) if item else '',
        'file_id': str(resolved_loc.id) if resolved_loc else file_id,
        'file_path': raw_path or str(phys_path),
        'description': gen_desc,
        'suggested_title': sugg_title,
        'suggested_authors': sugg_authors,
        'suggested_publisher': sugg_pub,
        'subjects': sugg_subjects,
        'applied': applied,
        'source': 'ai-content',
    }


_KNOWN_BOOK_CATALOG_COVER_HOSTS = (
    'books.google.',
    'googleapis.com',
    'mzstatic.com',
    'openlibrary.org',
    'goodreads.com',
    'gr-assets.com',
    'media-amazon.com',
    'images-amazon.com',
    'ssl-images-amazon.com',
    'amazon.com',
)


_COVER_REPAIR_STATE: Dict[str, Any] = {
    'running': False,
    'finished': False,
    'checked': 0,
    'skipped_user_selected': 0,
    'cleared_auto_bing_wallpaper': 0,
    'repaired_from_catalog_url': 0,
    'repaired_from_original_pdf': 0,
    'total_repaired': 0,
    'details': [],
    'error': '',
}


_COVER_REPAIR_STATE_FILE = Path('/tmp/mylibrary_cover_repair_state.json')


def _save_cover_repair_state(st: Dict[str, Any]) -> None:
    try:
        _COVER_REPAIR_STATE_FILE.write_text(json.dumps(st, ensure_ascii=False), encoding='utf-8')
    except Exception:
        pass


def start_or_get_cover_repair_status(start: bool = False) -> Dict[str, Any]:
    cur_state = dict(_COVER_REPAIR_STATE)
    try:
        if _COVER_REPAIR_STATE_FILE.exists():
            cur_state = json.loads(_COVER_REPAIR_STATE_FILE.read_text(encoding='utf-8'))
    except Exception:
        pass
    if start:
        import threading
        init_state = {
            'running': True,
            'finished': False,
            'checked': 0,
            'target_total': 0,
            'skipped_user_selected': 0,
            'cleared_auto_bing_wallpaper': 0,
            'repaired_from_catalog_url': 0,
            'repaired_from_original_pdf': 0,
            'total_repaired': 0,
            'details': [],
            'error': '',
        }
        _COVER_REPAIR_STATE.update(init_state)
        _save_cover_repair_state(init_state)
        threading.Thread(
            target=repair_stale_enriched_covers_without_user_override,
            kwargs={'dry_run': False, 'limit': 200, 'max_repairs': 5000},
            daemon=True,
        ).start()
        return init_state
    return cur_state


def repair_stale_enriched_covers_without_user_override(dry_run: bool = False, limit: int = 200, max_repairs: int = 500) -> Dict[str, Any]:
    """
    Background worker that inspects already-enriched CatalogItems in BookDB and repairs ONLY those where:
      1. The user did NOT manually select a new cover in Find Cover, AND
      2. Either:
         a) `cover_image_url` was set during enrichment/approval to an auto-picked Bing Web Image
            (non-book-catalog host matching `updated_metadata_json.cover_url`), or
         b) `cover_image_url` is a valid book-catalog URL from enrichment/approval, but `cover_thumbnail`
            was left on a missing/blank/text-only XObject slice because of the old `if not cat_item.cover_thumbnail:` bug, or
         c) `cover_image_url` is empty (no external cover selected) and `cover_thumbnail` is missing
            or a blank/partial XObject slice on a PDF where rendering Page 1 (`extract_original_cover_for_item`)
            produces a better full-page cover.
    """
    from django.db import connection
    from django.db.models import Q

    try:
        # Phase 1: Immediately scan and repair covers with excessive white/light page borders around a smaller centered graphic cover
        # (e.g. Liquid Silver Books / Boson Books PDFs where Page 1 had an embedded cover JPEG on an 8.5x11" white page,
        # or EPUB covers with light/white page padding around the cover graphic).
        # Prioritizes already-enriched books first, then scans the remaining catalog in fast ID-indexed chunks of 150.
        import importlib
        import sys
        base_dir = Path(getattr(settings, 'BASE_DIR', '.'))
        for sub in ('scanner', 'nas_scanner'):
            cand_dir = str((base_dir / sub).resolve())
            if os.path.isdir(cand_dir) and cand_dir not in sys.path:
                sys.path.insert(0, cand_dir)
        for mod_name in ('epubmeta.external_dbs', 'epubmeta.readers.pdf', 'epubmeta.readers.epub'):
            if mod_name in sys.modules:
                try:
                    importlib.reload(sys.modules[mod_name])
                except Exception:
                    pass
        from epubmeta.external_dbs import generate_cover_thumbnail

        enr_idx = get_enriched_item_ids_index()
        raw_enr_ids = [str(x) for x in (enr_idx.get('enriched_ids') or []) if x]
        # Scan both the tail (most recently appended enrichments) and head of `enriched_ids` first, then the middle
        if len(raw_enr_ids) > 4000:
            ordered_scan_ids = list(reversed(raw_enr_ids[-2500:])) + raw_enr_ids[:-2500]
        else:
            ordered_scan_ids = list(reversed(raw_enr_ids))
        _COVER_REPAIR_STATE['target_total'] = len(ordered_scan_ids)
        _save_cover_repair_state(_COVER_REPAIR_STATE)

        db_path_wb = _get_scanner_db_path()
        for wb_start in range(0, len(ordered_scan_ids), 150):
            if _COVER_REPAIR_STATE['total_repaired'] >= max_repairs:
                break
            wb_chunk_ids = ordered_scan_ids[wb_start:wb_start + 150]
            wb_rows = list(
                CatalogItem.objects.filter(id__in=wb_chunk_ids)
                .values_list('id', 'title', 'cover_thumbnail')
            )
            for wb_id, wb_title, wb_thumb in wb_rows:
                _COVER_REPAIR_STATE['checked'] += 1
                if not wb_thumb or not str(wb_thumb).startswith('data:image'):
                    continue
                if not has_excessive_white_border_cover(wb_thumb):
                    continue
                wb_item = CatalogItem.objects.prefetch_related('files').filter(id=wb_id).first()
                if not wb_item:
                    continue
                new_thumb = extract_original_cover_for_item(wb_item)
                if not new_thumb or not new_thumb.startswith('data:image') or has_excessive_white_border_cover(new_thumb):
                    raw_b = _decode_cover_bytes(wb_thumb)
                    if raw_b:
                        new_thumb = generate_cover_thumbnail(raw_b, max_size=(360, 540))
                if new_thumb and new_thumb.startswith('data:image') and not has_excessive_white_border_cover(new_thumb):
                    if not dry_run:
                        wb_item.cover_thumbnail = new_thumb
                        wb_item.save(update_fields=['cover_thumbnail'])
                        try:
                            if db_path_wb and db_path_wb.exists():
                                f_paths = [str(f.file_path) for f in wb_item.files.all() if f.file_path]
                                if f_paths:
                                    conn_wb = connect_scanner_db(db_path_wb, timeout=5.0)
                                    try:
                                        ph = ','.join(['?'] * len(f_paths))
                                        conn_wb.execute(
                                            f"UPDATE processed_files SET cover_thumbnail = ? WHERE file_path IN ({ph})",
                                            [new_thumb, *f_paths],
                                        )
                                        conn_wb.commit()
                                    finally:
                                        conn_wb.close()
                        except Exception:
                            pass
                    _COVER_REPAIR_STATE['repaired_from_original_pdf'] = _COVER_REPAIR_STATE.get('repaired_from_original_pdf', 0) + 1
                    _COVER_REPAIR_STATE['total_repaired'] += 1
                    if len(_COVER_REPAIR_STATE['details']) < limit:
                        _COVER_REPAIR_STATE['details'].append({
                            'item_id': str(wb_id),
                            'title': wb_title,
                            'reason': 'cropped_excessive_white_border_around_cover',
                        })
                    _save_cover_repair_state(_COVER_REPAIR_STATE)
            _save_cover_repair_state(_COVER_REPAIR_STATE)

        user_dismissed_ids: set = set()
        needs_cover_issue_by_id: Dict[str, str] = {}
        try:
            _ensure_needs_cover_table()
            with connection.cursor() as cur:
                cur.execute("SELECT item_id, cover_issue FROM catalog_needs_cover_books")
                for r in cur.fetchall():
                    if not r or not r[0]:
                        continue
                    cid = str(r[0])
                    if cid.startswith('__'):
                        continue
                    if r[1] == 'dismissed':
                        user_dismissed_ids.add(cid)
                    elif r[1] in ('missing', 'text_only'):
                        needs_cover_issue_by_id[cid] = str(r[1])
        except Exception:
            pass

        # Fast query against scanner_state.db ONLY for rows where updated_metadata_json contains cover_url
        auto_cover_url_by_path: Dict[str, str] = {}
        db_paths_with_auto_cover: List[str] = []
        try:
            db_path = _get_scanner_db_path()
            if db_path and db_path.exists():
                conn = connect_scanner_db(db_path, timeout=5.0)
                try:
                    rows = conn.execute(
                        "SELECT file_path, updated_metadata_json FROM processed_files "
                        "WHERE status = 'SUCCESS' AND updated_metadata_json LIKE '%http%'"
                    ).fetchall()
                    for fp, ujson in rows:
                        if not fp or not ujson:
                            continue
                        try:
                            um = json.loads(ujson) or {}
                            cu = str(um.get('cover_url') or '').strip()
                            if cu:
                                norm_fp = str(fp).strip().replace('\\', '/')
                                auto_cover_url_by_path[norm_fp] = cu
                                db_paths_with_auto_cover.append(norm_fp)
                                if norm_fp.startswith('/volume1/common/Media/Books/'):
                                    alt = '/books/' + norm_fp[len('/volume1/common/Media/Books/'):]
                                    auto_cover_url_by_path[alt] = cu
                                    db_paths_with_auto_cover.append(alt)
                                elif norm_fp.startswith('/books/'):
                                    alt = '/volume1/common/Media/Books/' + norm_fp[len('/books/'):]
                                    auto_cover_url_by_path[alt] = cu
                                    db_paths_with_auto_cover.append(alt)
                        except Exception:
                            continue
                finally:
                    conn.close()
        except Exception:
            pass

        auto_cov_item_ids: set = set()
        if db_paths_with_auto_cover:
            for i in range(0, len(db_paths_with_auto_cover), 300):
                chunk_p = db_paths_with_auto_cover[i:i + 300]
                for cid in FileLocation.objects.filter(file_path__in=chunk_p).values_list('catalog_item_id', flat=True):
                    if cid:
                        auto_cov_item_ids.add(str(cid))

        target_ids = list(auto_cov_item_ids | {cid for cid in needs_cover_issue_by_id.keys() if cid not in user_dismissed_ids})
        _COVER_REPAIR_STATE['target_total'] = len(target_ids)
        _save_cover_repair_state(_COVER_REPAIR_STATE)

        for start_i in range(0, len(target_ids), 80):
            chunk_ids = target_ids[start_i:start_i + 80]
            items_batch = list(CatalogItem.objects.filter(id__in=chunk_ids).prefetch_related('files'))
            for item in items_batch:
                item_id_str = str(item.id)
                _COVER_REPAIR_STATE['checked'] += 1
                if _COVER_REPAIR_STATE['checked'] % 10 == 0:
                    _save_cover_repair_state(_COVER_REPAIR_STATE)
                ext_url = str(item.cover_image_url or '').strip()
                thumb = str(item.cover_thumbnail or '').strip()
                files = list(item.files.all())
                f_paths = [str(f.file_path or '').strip().replace('\\', '/') for f in files if f.file_path]
                has_pdf = any((f.file_format or '').upper() == 'PDF' or str(f.file_path or '').lower().endswith('.pdf') for f in files)

                auto_urls = {auto_cover_url_by_path[p] for p in f_paths if p in auto_cover_url_by_path}
                is_ext_url_from_auto_enrich = bool(ext_url and ext_url in auto_urls)
                is_ext_url_book_catalog = bool(ext_url and any(h in ext_url.lower() for h in _KNOWN_BOOK_CATALOG_COVER_HOSTS))

                # Never overwrite a cover where the user manually selected a new cover!
                if ext_url and not is_ext_url_from_auto_enrich and item_id_str in user_dismissed_ids:
                    _COVER_REPAIR_STATE['skipped_user_selected'] += 1
                    continue
                thumb_issue = needs_cover_issue_by_id.get(item_id_str) or (classify_cover_image(thumb) if thumb else 'missing')
                if ext_url and not is_ext_url_from_auto_enrich and thumb_issue is None:
                    _COVER_REPAIR_STATE['skipped_user_selected'] += 1
                    continue

                # Case 1: Enrichment/approval auto-picked a non-book-catalog Bing Web Image Search URL
                if ext_url and is_ext_url_from_auto_enrich and not is_ext_url_book_catalog:
                    if not dry_run:
                        item.cover_image_url = None
                        orig_cov = extract_original_cover_for_item(item)
                        if orig_cov and orig_cov.startswith('data:image'):
                            item.cover_thumbnail = orig_cov
                        item.save(update_fields=['cover_image_url', 'cover_thumbnail', 'updated_at'])
                        evaluate_and_record_item_cover_status(item)
                    _COVER_REPAIR_STATE['cleared_auto_bing_wallpaper'] += 1
                    _COVER_REPAIR_STATE['total_repaired'] += 1
                    if len(_COVER_REPAIR_STATE['details']) < limit:
                        _COVER_REPAIR_STATE['details'].append({
                            'item_id': item_id_str,
                            'title': item.title,
                            'reason': 'auto_bing_web_image_replaced_with_original',
                            'old_ext_url': ext_url,
                        })
                    continue

                # Case 2: Valid book-catalog cover_image_url exists, but cover_thumbnail is missing or still stuck on a text_only/blank XObject slice
                if ext_url and is_ext_url_book_catalog and thumb_issue in ('missing', 'text_only'):
                    if not dry_run:
                        res = apply_book_cover_to_item(item, ext_url, user_selected=False)
                        if not res.get('ok'):
                            orig_cov = extract_original_cover_for_item(item)
                            if orig_cov and orig_cov.startswith('data:image'):
                                item.cover_thumbnail = orig_cov
                                item.save(update_fields=['cover_thumbnail', 'updated_at'])
                                evaluate_and_record_item_cover_status(item)
                    _COVER_REPAIR_STATE['repaired_from_catalog_url'] += 1
                    _COVER_REPAIR_STATE['total_repaired'] += 1
                    if len(_COVER_REPAIR_STATE['details']) < limit:
                        _COVER_REPAIR_STATE['details'].append({
                            'item_id': item_id_str,
                            'title': item.title,
                            'reason': 'sync_thumbnail_from_catalog_cover_url',
                            'ext_url': ext_url,
                        })
                    continue

                # Case 3: No external cover selected (`not ext_url`), item is a PDF, user never dismissed/selected a cover,
                # and `cover_thumbnail` is missing or a blank/partial XObject slice where rendering PDF Page 1 yields a better cover
                if not ext_url and has_pdf and item_id_str not in user_dismissed_ids and thumb_issue in ('missing', 'text_only'):
                    if _COVER_REPAIR_STATE['total_repaired'] >= max_repairs:
                        continue
                    orig_cov = extract_original_cover_for_item(item)
                    if orig_cov and orig_cov.startswith('data:image') and orig_cov != thumb and (
                        not thumb
                        or classify_cover_image(orig_cov) is None
                        or len(orig_cov) > len(thumb) + 2000
                    ):
                        if not dry_run:
                            item.cover_thumbnail = orig_cov
                            item.save(update_fields=['cover_thumbnail', 'updated_at'])
                            evaluate_and_record_item_cover_status(item)
                        _COVER_REPAIR_STATE['repaired_from_original_pdf'] += 1
                        _COVER_REPAIR_STATE['total_repaired'] += 1
                        if len(_COVER_REPAIR_STATE['details']) < limit:
                            _COVER_REPAIR_STATE['details'].append({
                                'item_id': item_id_str,
                                'title': item.title,
                                'reason': 'rendered_pdf_page1_original_cover',
                            })
    except Exception as exc:
        _COVER_REPAIR_STATE['error'] = str(exc)
    finally:
        _COVER_REPAIR_STATE['running'] = False
        _COVER_REPAIR_STATE['finished'] = True
        _save_cover_repair_state(_COVER_REPAIR_STATE)
    return dict(_COVER_REPAIR_STATE)


def classify_cover_image(cover_data_or_bytes: Any, cover_image_url: str = '') -> Optional[str]:
    """
    Inspect a book's cover image (`cover_thumbnail` data URI or raw `bytes`, plus `cover_image_url`)
    and return:
      - `'missing'`: No `cover_thumbnail` and no `cover_image_url`
      - `'text_only'`: A plain white/paper title page with black/gray text (or blank/monochrome page
        without cover artwork)
      - `None`: Has a genuine graphic cover
    """
    raw_bytes: Optional[bytes] = None
    if isinstance(cover_data_or_bytes, (bytes, bytearray)):
        raw_bytes = bytes(cover_data_or_bytes)
    elif isinstance(cover_data_or_bytes, str):
        s_val = cover_data_or_bytes.strip()
        if s_val.startswith('data:image') and ',' in s_val:
            try:
                raw_bytes = base64.b64decode(s_val.split(',', 1)[1])
            except Exception:
                raw_bytes = None

    clean_ext_url = str(cover_image_url or '').strip()
    if not raw_bytes and not clean_ext_url:
        return 'missing'
    if not raw_bytes:
        # External URL exists without decoded thumbnail bytes
        return None
    if len(raw_bytes) < 900:
        return 'missing'

    try:
        import io
        from PIL import Image

        im = Image.open(io.BytesIO(raw_bytes)).convert('RGB')
        w, h = im.size
        if w < 24 or h < 24:
            return 'missing'

        # Crop 8% outer border to ignore scan borders / shadows, then downsample to 32x45 for sub-millisecond analysis
        im_crop = im.crop((int(w * 0.08), int(h * 0.08), max(int(w * 0.08) + 1, int(w * 0.92)), max(int(h * 0.08) + 1, int(h * 0.92))))
        im_small = im_crop.resize((32, 45))
        if hasattr(im_small, 'get_flattened_data'):
            px = list(im_small.get_flattened_data())
        else:
            px = list(im_small.getdata())
        n = len(px)
        if not n:
            return 'missing'

        white_bg = 0
        colored = 0
        dark_ink = 0
        mid_gray = 0
        buckets: Dict[Tuple[int, int, int], int] = {}

        for r, g, b in px:
            mx = max(r, g, b)
            mn = min(r, g, b)
            sat_diff = mx - mn
            if mn >= 210 and sat_diff < 20:
                white_bg += 1
            if sat_diff >= 26:
                colored += 1
            if mx <= 95:
                dark_ink += 1
            elif mx < 210 and sat_diff < 26:
                mid_gray += 1
            b_key = (r // 28, g // 28, b // 28)
            buckets[b_key] = buckets.get(b_key, 0) + 1

        white_ratio = white_bg / n
        color_ratio = colored / n
        dark_ratio = dark_ink / n
        mid_ratio = mid_gray / n

        # 1. Plain white/light-paper title page with black/gray text (e.g. Oxford University Press title pages)
        if white_ratio >= 0.78 and color_ratio <= 0.025 and mid_ratio <= 0.16:
            return 'text_only'

        # 2. Almost completely blank white or black page
        if white_ratio >= 0.93 or dark_ratio >= 0.96:
            return 'text_only'

        # 3. Solid single-color page with sparse text and no illustration/photo gradients
        sorted_b = sorted(buckets.values(), reverse=True)
        top1_ratio = sorted_b[0] / n if sorted_b else 0.0
        top2_ratio = (sorted_b[0] + (sorted_b[1] if len(sorted_b) > 1 else 0)) / n if sorted_b else 0.0
        if top1_ratio >= 0.87 and top2_ratio >= 0.95 and len(buckets) <= 14:
            return 'text_only'

        return None
    except Exception:
        return None


def _decode_cover_bytes(cover_data_or_bytes: Any) -> Optional[bytes]:
    """Decode a base64 `data:image/...` URI or raw bytes into `bytes`."""
    if isinstance(cover_data_or_bytes, (bytes, bytearray)):
        return bytes(cover_data_or_bytes)
    if isinstance(cover_data_or_bytes, str):
        s_val = cover_data_or_bytes.strip()
        if s_val.startswith('data:image') and ',' in s_val:
            try:
                return base64.b64decode(s_val.split(',', 1)[1])
            except Exception:
                return None
    return None


def cover_colorfulness_score(cover_data_or_bytes: Any) -> float:
    """
    Compute a [0.0, 1.0] colorfulness score for a cover (`data:image/...` URI or raw `bytes`).
    Grayscale / black-and-white covers score < 0.045; color covers typically score >= 0.08.
    """
    raw_bytes = _decode_cover_bytes(cover_data_or_bytes)
    if not raw_bytes or len(raw_bytes) < 900:
        return 0.0
    try:
        import io
        from PIL import Image

        im = Image.open(io.BytesIO(raw_bytes)).convert('RGB')
        w, h = im.size
        if w < 24 or h < 24:
            return 0.0
        im_crop = im.crop((int(w * 0.08), int(h * 0.08), max(int(w * 0.08) + 1, int(w * 0.92)), max(int(h * 0.08) + 1, int(h * 0.92))))
        im_small = im_crop.resize((48, 64))
        px = list(im_small.get_flattened_data() if hasattr(im_small, 'get_flattened_data') else im_small.getdata())
        n = len(px)
        if not n:
            return 0.0
        chroma_sum = 0
        colored = 0
        for r, g, b in px:
            mx = max(r, g, b)
            mn = min(r, g, b)
            ch = mx - mn
            chroma_sum += ch
            if ch >= 24 and mx >= 35:
                colored += 1
        return 0.55 * (chroma_sum / (n * 255.0)) + 0.45 * (colored / n)
    except Exception:
        return 0.0


def are_covers_same_design(cover_a: Any, cover_b: Any) -> Tuple[bool, float]:
    """
    Compare two covers (`data:image/...` URI or raw `bytes`) in grayscale + gradient-edge space
    to determine if they represent the same cover layout/design (e.g. B&W vs. color version).
    Returns `(is_same_design, similarity_score)`.
    """
    bytes_a = _decode_cover_bytes(cover_a)
    bytes_b = _decode_cover_bytes(cover_b)
    if not bytes_a or not bytes_b or len(bytes_a) < 900 or len(bytes_b) < 900:
        return False, 0.0
    try:
        import io
        import math
        from PIL import Image

        GW, GH = 24, 32

        def _prep(raw: bytes) -> Tuple[List[float], List[float]]:
            im = Image.open(io.BytesIO(raw)).convert('L')
            w, h = im.size
            im_c = im.crop((int(w * 0.06), int(h * 0.06), max(int(w * 0.06) + 1, int(w * 0.94)), max(int(h * 0.06) + 1, int(h * 0.94))))
            im_s = im_c.resize((GW, GH))
            px = [float(v) for v in (im_s.get_flattened_data() if hasattr(im_s, 'get_flattened_data') else im_s.getdata())]
            ed = [0.0] * (GW * GH)
            for r in range(1, GH - 1):
                row_off = r * GW
                for c in range(1, GW - 1):
                    dx = abs(px[row_off + c + 1] - px[row_off + c - 1])
                    dy = abs(px[row_off + GW + c] - px[row_off - GW + c])
                    ed[row_off + c] = dx + dy
            return px, ed

        pa, ea = _prep(bytes_a)
        pb, eb = _prep(bytes_b)

        def _zncc_shifted(xa: List[float], xb: List[float], dx: int, dy: int) -> float:
            r0 = max(1, 1 - dy)
            r1 = min(GH - 1, GH - 1 - dy)
            c0 = max(1, 1 - dx)
            c1 = min(GW - 1, GW - 1 - dx)
            va: List[float] = []
            vb: List[float] = []
            for r in range(r0, r1):
                ra = r * GW
                rb = (r + dy) * GW
                for c in range(c0, c1):
                    va.append(xa[ra + c])
                    vb.append(xb[rb + c + dx])
            n = len(va)
            if n < 64:
                return 0.0
            ma = sum(va) / n
            mb = sum(vb) / n
            sa = sum((v - ma) ** 2 for v in va)
            sb = sum((v - mb) ** 2 for v in vb)
            if sa <= 1e-6 or sb <= 1e-6:
                return 0.0
            return sum((va[i] - ma) * (vb[i] - mb) for i in range(n)) / math.sqrt(sa * sb)

        best_sim = 0.0
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                gz = _zncc_shifted(pa, pb, dx, dy)
                ez = _zncc_shifted(ea, eb, dx, dy)
                sim = max(gz, 0.45 * gz + 0.55 * ez, ez * 0.90)
                if sim > best_sim:
                    best_sim = sim
        return best_sim >= 0.55, best_sim
    except Exception:
        return False, 0.0


def has_excessive_white_border_cover(cover_data_or_bytes: Any) -> bool:
    """
    Return True if `cover_data_or_bytes` is a full-page render where an outer white/light-colored border
    surrounds a much smaller centered graphic cover (e.g. a small cover JPEG placed on a blank
    8.5x11" PDF page, or an 850x1100 EPUB cover canvas with light side margins and a white bottom half).
    """
    raw_bytes = _decode_cover_bytes(cover_data_or_bytes)
    if not raw_bytes or len(raw_bytes) < 900:
        return False
    try:
        import io
        from PIL import Image

        rgb = Image.open(io.BytesIO(raw_bytes)).convert('RGB')
        w, h = rgb.size
        if w < 60 or h < 80:
            return False
        SW, SH = 100, 140
        small = rgb.resize((SW, SH))
        px = list(small.get_flattened_data() if hasattr(small, 'get_flattened_data') else small.getdata())

        pad_mask = [[False] * SW for _ in range(SH)]
        total_colored = 0
        for r in range(SH):
            ro = r * SW
            lp = px[ro + 0]
            rp = px[ro + (SW - 1)]
            row_bg = lp if (min(lp) >= 205 and max(abs(lp[i] - rp[i]) for i in range(3)) <= 20) else (255, 255, 255)
            for c in range(SW):
                p = px[ro + c]
                mx = max(p)
                mn = min(p)
                if (mx - mn) >= 24 and mx >= 35:
                    total_colored += 1
                is_white = mn >= 225 and (mx - mn) <= 18
                matches_row_bg = min(row_bg) >= 205 and max(abs(p[i] - row_bg[i]) for i in range(3)) <= 16
                if is_white or matches_row_bg:
                    pad_mask[r][c] = True

        if total_colored < 40:
            return False

        row_fg = [sum(1 for c in range(SW) if not pad_mask[r][c]) for r in range(SH)]
        active_rows = [r for r in range(SH) if row_fg[r] >= 18]
        if not active_rows:
            return False
        r0, r1 = min(active_rows), max(active_rows)

        col_fg_in_rows = [sum(1 for r in range(r0, r1 + 1) if not pad_mask[r][c]) for c in range(SW)]
        min_col_hits = max(8, int((r1 - r0 + 1) * 0.25))
        active_cols = [c for c in range(SW) if col_fg_in_rows[c] >= min_col_hits]
        if not active_cols:
            return False
        c0, c1 = min(active_cols), max(active_cols)

        box_w = (c1 - c0 + 1) / SW
        box_h = (r1 - r0 + 1) / SH
        box_area = box_w * box_h
        if not (0.08 <= box_area <= 0.82 and box_w >= 0.22 and box_h >= 0.22):
            return False

        in_fg, in_tot, out_pad, out_tot = 0, 0, 0, 0
        for r in range(SH):
            for c in range(SW):
                if r0 <= r <= r1 and c0 <= c <= c1:
                    in_tot += 1
                    if not pad_mask[r][c]:
                        in_fg += 1
                else:
                    out_tot += 1
                    if pad_mask[r][c]:
                        out_pad += 1
        if in_tot == 0 or out_tot == 0:
            return False
        return bool((in_fg / in_tot) >= 0.70 and (out_pad / out_tot) >= 0.94)
    except Exception:
        return False


def is_partial_banner_cover(cover_data_or_bytes: Any) -> bool:
    """
    Return True if `cover_data_or_bytes` is a partial top-of-cover banner or square graphic slice
    (e.g. `200x133` or `200x195` extracted from `first_page.images[0]` on a PDF cover page where the
    rest of the cover page is vector shapes and title/author text, so `width / height >= 0.80`).
    """
    raw_bytes = _decode_cover_bytes(cover_data_or_bytes)
    if not raw_bytes or len(raw_bytes) < 400:
        return False
    try:
        import io
        from PIL import Image

        with Image.open(io.BytesIO(raw_bytes)) as im:
            w, h = im.size
            if w <= 0 or h <= 0:
                return True
            if (w / float(h)) >= 0.80:
                return True
            if w == 200 and h < 225:
                return True
    except Exception:
        return False
    return False


def is_legacy_scanner_pdf_cover(cover_data_or_bytes: Any) -> bool:
    """
    Return True if `cover_data_or_bytes` is a legacy `200x300` thumbnail (`w <= 200 and h <= 300`)
    extracted by the old `pypdf` `first_page.images[0].data` scanner, which grabbed only the
    embedded background image XObject on Page 1 without rendering overlaid PDF title/author text
    or vector graphics (whereas full-page rendered PDF covers use `max_size=(360, 540)`).
    """
    raw_bytes = _decode_cover_bytes(cover_data_or_bytes)
    if not raw_bytes or len(raw_bytes) < 400:
        return False
    try:
        import io
        from PIL import Image

        with Image.open(io.BytesIO(raw_bytes)) as im:
            w, h = im.size
            if w <= 200 and h <= 300:
                return True
    except Exception:
        return False
    return False


def should_prefer_candidate_cover(orig_cover: Any, cand_cover: Any) -> bool:
    """
    Return True if `cand_cover` should be preferred over `orig_cover`:
      1. `orig_cover` is missing or a text-only page and `cand_cover` is a genuine graphic cover, OR
      2. `orig_cover` is a partial top-of-cover banner/square slice (`is_partial_banner_cover`) and `cand_cover` is not, OR
      3. `orig_cover` is a legacy `200x300` `pypdf` `first_page.images[0]` thumbnail (missing overlaid Page 1 text) and `cand_cover` is not, OR
      4. `orig_cover` has an excessive white page border around a small cover and `cand_cover` does not, OR
      5. `cand_cover` has the same cover layout/design as `orig_cover` AND is in color
         while `orig_cover` is grayscale/monochrome or significantly less colorful.
    """
    if classify_cover_image(cand_cover) is not None:
        return False
    if classify_cover_image(orig_cover) in ('missing', 'text_only'):
        return True
    if is_partial_banner_cover(orig_cover) and not is_partial_banner_cover(cand_cover):
        return True
    if is_legacy_scanner_pdf_cover(orig_cover) and not is_legacy_scanner_pdf_cover(cand_cover):
        return True
    if has_excessive_white_border_cover(orig_cover) and not has_excessive_white_border_cover(cand_cover):
        return True
    orig_col = cover_colorfulness_score(orig_cover)
    cand_col = cover_colorfulness_score(cand_cover)
    if cand_col >= 0.065 and cand_col >= orig_col + 0.045:
        same_design, _ = are_covers_same_design(orig_cover, cand_cover)
        if same_design:
            return True
    return False


_NEEDS_COVER_TABLE_READY = False
_NEEDS_COVER_BACKFILL_STARTED = False
_NEEDS_COVER_SHELF_CACHE: Dict[str, Any] = {
    'expires_at': 0.0,
    'item_ids': [],
    'issue_by_id': {},
    'count': 0,
    'missing_count': 0,
    'text_only_count': 0,
}


def _ensure_needs_cover_table() -> None:
    global _NEEDS_COVER_TABLE_READY
    if _NEEDS_COVER_TABLE_READY:
        return
    try:
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS catalog_needs_cover_books (
                    item_id VARCHAR(64) PRIMARY KEY,
                    cover_issue VARCHAR(32) DEFAULT 'missing',
                    reason TEXT DEFAULT '',
                    marked_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        _NEEDS_COVER_TABLE_READY = True
    except Exception:
        pass


def evaluate_and_record_item_cover_status(item: CatalogItem, force_check: bool = False) -> Optional[str]:
    """
    Evaluate an enriched `CatalogItem`'s cover (`missing`, `text_only`, or `None`)
    and persist its status in `catalog_needs_cover_books` so it automatically appears
    in (or is removed from) the **Missing / Text Cover** shelf (`?shelf=missing_cover`).
    """
    if not item or not item.id:
        return None
    _ensure_needs_cover_table()
    item_id_str = str(item.id)
    issue = classify_cover_image(item.cover_thumbnail, item.cover_image_url)
    try:
        from django.db import connection
        with connection.cursor() as cur:
            if issue in ('missing', 'text_only'):
                reason_txt = (
                    'Enriched book is missing cover artwork'
                    if issue == 'missing'
                    else 'Enriched book cover appears to be a text-only title page'
                )
                if force_check:
                    cur.execute(
                        """
                        INSERT INTO catalog_needs_cover_books (item_id, cover_issue, reason, marked_at)
                        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (item_id) DO UPDATE SET
                            cover_issue = EXCLUDED.cover_issue,
                            reason = EXCLUDED.reason,
                            marked_at = CURRENT_TIMESTAMP
                        """,
                        [item_id_str, issue, reason_txt],
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO catalog_needs_cover_books (item_id, cover_issue, reason, marked_at)
                        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (item_id) DO UPDATE SET
                            cover_issue = CASE
                                WHEN catalog_needs_cover_books.cover_issue = 'dismissed' THEN 'dismissed'
                                ELSE EXCLUDED.cover_issue
                            END,
                            reason = CASE
                                WHEN catalog_needs_cover_books.cover_issue = 'dismissed' THEN catalog_needs_cover_books.reason
                                ELSE EXCLUDED.reason
                            END,
                            marked_at = CURRENT_TIMESTAMP
                        """,
                        [item_id_str, issue, reason_txt],
                    )
            else:
                cur.execute(
                    "DELETE FROM catalog_needs_cover_books WHERE item_id = %s AND cover_issue != 'dismissed'",
                    [item_id_str],
                )
        _NEEDS_COVER_SHELF_CACHE['expires_at'] = 0.0
    except Exception:
        pass
    return issue


def _run_needs_cover_backfill_worker() -> None:
    """
    Background worker that scans existing enriched books in `BookDB` once and populates
    `catalog_needs_cover_books` with any enriched book whose cover is missing or text-only.
    """
    try:
        from django.db import connection
        from django.db.models import Q
        _ensure_needs_cover_table()
        enr_idx = get_enriched_item_ids_index()
        enr_ids = [str(x) for x in (enr_idx.get('enriched_ids') or []) if x]
        if not enr_ids:
            return
        enr_set = set(enr_ids)

        # 1. Fast check for enriched books with completely empty covers
        all_missing_ids = [
            str(x)
            for x in CatalogItem.objects.filter(
                (Q(cover_thumbnail__isnull=True) | Q(cover_thumbnail=''))
                & (Q(cover_image_url__isnull=True) | Q(cover_image_url=''))
            ).values_list('id', flat=True)
        ]
        missing_rows = [mid for mid in all_missing_ids if mid in enr_set]
        if missing_rows:
            with connection.cursor() as cur:
                for mid in missing_rows:
                    cur.execute(
                        """
                        INSERT INTO catalog_needs_cover_books (item_id, cover_issue, reason, marked_at)
                        VALUES (%s, 'missing', 'Enriched book is missing cover artwork', CURRENT_TIMESTAMP)
                        ON CONFLICT (item_id) DO NOTHING
                        """,
                        [str(mid)],
                    )
            _NEEDS_COVER_SHELF_CACHE['expires_at'] = 0.0

        # 2. Scan thumbnails in small paced chunks to detect text-only title page covers without GIL contention
        with connection.cursor() as cur:
            cur.execute("SELECT item_id FROM catalog_needs_cover_books")
            already_checked = {str(r[0]) for r in cur.fetchall() if r and r[0]}

        batch_size = 120
        for start_i in range(0, len(enr_ids), batch_size):
            time.sleep(0.18)
            chunk_ids = [cid for cid in enr_ids[start_i:start_i + batch_size] if cid not in already_checked]
            if not chunk_ids:
                continue
            rows = list(
                CatalogItem.objects.filter(id__in=chunk_ids)
                .values_list('id', 'cover_thumbnail', 'cover_image_url')
            )
            found_issues: List[Tuple[str, str, str]] = []
            for idx_r, (cid, thumb, ext_url) in enumerate(rows):
                if idx_r % 15 == 14:
                    time.sleep(0.005)
                issue = classify_cover_image(thumb, ext_url or '')
                if issue in ('missing', 'text_only'):
                    reason_txt = (
                        'Enriched book is missing cover artwork'
                        if issue == 'missing'
                        else 'Enriched book cover appears to be a text-only title page'
                    )
                    found_issues.append((str(cid), issue, reason_txt))
            if found_issues:
                with connection.cursor() as cur:
                    for cid_str, iss, r_txt in found_issues:
                        cur.execute(
                            """
                            INSERT INTO catalog_needs_cover_books (item_id, cover_issue, reason, marked_at)
                            VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                            ON CONFLICT (item_id) DO NOTHING
                            """,
                            [cid_str, iss, r_txt],
                        )
                _NEEDS_COVER_SHELF_CACHE['expires_at'] = 0.0

        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO catalog_needs_cover_books (item_id, cover_issue, reason, marked_at)
                VALUES ('__backfilled_v1__', 'meta', 'Backfill complete', CURRENT_TIMESTAMP)
                ON CONFLICT (item_id) DO UPDATE SET marked_at = CURRENT_TIMESTAMP
                """
            )
        _NEEDS_COVER_SHELF_CACHE['expires_at'] = 0.0
    except Exception:
        pass


def _start_needs_cover_backfill_if_needed() -> None:
    global _NEEDS_COVER_BACKFILL_STARTED
    if _NEEDS_COVER_BACKFILL_STARTED:
        return
    _NEEDS_COVER_BACKFILL_STARTED = True
    _ensure_needs_cover_table()
    try:
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute("SELECT 1 FROM catalog_needs_cover_books WHERE item_id IN ('__backfilled_v1__', '__backfill_lock__')")
            if cur.fetchone() is not None:
                return
            cur.execute(
                """
                INSERT INTO catalog_needs_cover_books (item_id, cover_issue, reason, marked_at)
                VALUES ('__backfill_lock__', 'meta', 'Backfill in progress', CURRENT_TIMESTAMP)
                ON CONFLICT (item_id) DO NOTHING
                """
            )
        import threading
        threading.Thread(target=_run_needs_cover_backfill_worker, daemon=True).start()
    except Exception:
        pass


def get_needs_cover_enriched_shelf() -> Dict[str, Any]:
    """
    Return the cached dictionary of enriched books whose cover is missing or is just a text page:
      `{"item_ids": [...], "issue_by_id": {id_str: 'missing'|'text_only'}, "count": int, "missing_count": int, "text_only_count": int}`
    """
    now_m = time.monotonic()
    if now_m < _NEEDS_COVER_SHELF_CACHE['expires_at']:
        return _NEEDS_COVER_SHELF_CACHE

    _ensure_needs_cover_table()
    _start_needs_cover_backfill_if_needed()

    try:
        import uuid as _uuid
        from django.db import connection
        enr_idx = get_enriched_item_ids_index()
        enr_uuid_set = enr_idx.get('enriched_ids_set') or set()

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT item_id, cover_issue
                FROM catalog_needs_cover_books
                WHERE cover_issue IN ('missing', 'text_only', 'dismissed')
                ORDER BY marked_at DESC
                """
            )
            rows = cur.fetchall()

        item_ids: List[str] = []
        issue_by_id: Dict[str, str] = {}
        dismissed_set: set = set()
        missing_cnt = 0
        text_cnt = 0
        for r in rows:
            if not r or not r[0]:
                continue
            cid = str(r[0])
            if cid.startswith('__'):
                continue
            iss = str(r[1] or 'missing')
            if iss == 'dismissed':
                dismissed_set.add(cid)
                continue
            if enr_uuid_set:
                try:
                    if _uuid.UUID(cid) not in enr_uuid_set:
                        continue
                except Exception:
                    continue
            item_ids.append(cid)
            issue_by_id[cid] = iss
            if iss == 'text_only':
                text_cnt += 1
            else:
                missing_cnt += 1

        _NEEDS_COVER_SHELF_CACHE.update({
            'expires_at': now_m + 120.0,
            'item_ids': item_ids,
            'issue_by_id': issue_by_id,
            'dismissed_set': dismissed_set,
            'count': len(item_ids),
            'missing_count': missing_cnt,
            'text_only_count': text_cnt,
        })
    except Exception:
        pass
    return _NEEDS_COVER_SHELF_CACHE


def set_book_needs_cover(
    item_id: Any,
    needs_cover: Optional[bool] = None,
    cover_issue: str = 'text_only',
    reason: str = '',
) -> Dict[str, Any]:
    """
    Flag or dismiss (`needs_cover=False` -> `cover_issue='dismissed'`) a `CatalogItem`
    on the **Missing / Text Cover** shelf.
    """
    _ensure_needs_cover_table()
    item_id_str = str(item_id or '').strip()
    if not item_id_str:
        return {'ok': False, 'error': 'item_id is required'}
    issue_clean = cover_issue if cover_issue in ('missing', 'text_only') else 'text_only'
    try:
        from django.db import connection
        with connection.cursor() as cur:
            if needs_cover is None:
                cur.execute(
                    "SELECT cover_issue FROM catalog_needs_cover_books WHERE item_id = %s",
                    [item_id_str],
                )
                row = cur.fetchone()
                is_active = bool(row and row[0] in ('missing', 'text_only'))
                target_state = not is_active
            else:
                target_state = bool(needs_cover)

            if target_state:
                cur.execute(
                    """
                    INSERT INTO catalog_needs_cover_books (item_id, cover_issue, reason, marked_at)
                    VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (item_id) DO UPDATE SET
                        cover_issue = EXCLUDED.cover_issue,
                        reason = EXCLUDED.reason,
                        marked_at = CURRENT_TIMESTAMP
                    """,
                    [item_id_str, issue_clean, (reason or 'Flagged as Missing / Text Cover')[:500]],
                )
            else:
                cur.execute(
                    """
                    INSERT INTO catalog_needs_cover_books (item_id, cover_issue, reason, marked_at)
                    VALUES (%s, 'dismissed', %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (item_id) DO UPDATE SET
                        cover_issue = 'dismissed',
                        reason = EXCLUDED.reason,
                        marked_at = CURRENT_TIMESTAMP
                    """,
                    [item_id_str, (reason or 'Dismissed / Cover kept by user')[:500]],
                )

        _NEEDS_COVER_SHELF_CACHE['expires_at'] = 0.0
        shelf_info = get_needs_cover_enriched_shelf()
        return {
            'ok': True,
            'status': 'success',
            'item_id': item_id_str,
            'needs_cover': target_state,
            'cover_issue': issue_clean if target_state else '',
            'missing_cover_count': shelf_info.get('count', 0),
        }
    except Exception as err:
        return {'ok': False, 'error': str(err)}


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


_INTERESTING_CACHE: Dict[str, Any] = {'expires_at': 0.0, 'ids': []}
_BAD_METADATA_CACHE: Dict[str, Any] = {'expires_at': 0.0, 'ids': []}


def get_interesting_item_ids() -> List[str]:
    """
    Return the list of `CatalogItem` UUID strings currently marked as Interesting,
    ordered by most recently marked first.
    """
    now_m = time.monotonic()
    if now_m < _INTERESTING_CACHE['expires_at']:
        return _INTERESTING_CACHE['ids']
    _ensure_interesting_books_table()
    try:
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute("SELECT item_id FROM catalog_interesting_books ORDER BY marked_at DESC")
            ids = [str(r[0]) for r in cur.fetchall() if r and r[0]]
        _INTERESTING_CACHE['ids'] = ids
        _INTERESTING_CACHE['expires_at'] = now_m + 180.0
        return ids
    except Exception:
        return _INTERESTING_CACHE['ids']


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
        _INTERESTING_CACHE['expires_at'] = 0.0
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
    now_m = time.monotonic()
    if now_m < _BAD_METADATA_CACHE['expires_at']:
        return _BAD_METADATA_CACHE['ids']
    _ensure_bad_metadata_table()
    try:
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute("SELECT item_id FROM catalog_bad_metadata_books ORDER BY marked_at DESC")
            ids = [str(r[0]) for r in cur.fetchall() if r and r[0]]
        _BAD_METADATA_CACHE['ids'] = ids
        _BAD_METADATA_CACHE['expires_at'] = now_m + 180.0
        return ids
    except Exception:
        return _BAD_METADATA_CACHE['ids']


_REMOVED_PROTECTION_CACHE: Dict[str, Any] = {'expires_at': 0.0, 'ids': [], '_refreshing': False}


def _rebuild_removed_protection_cache() -> List[Any]:
    now_m = time.monotonic()
    disk_file = Path('/books_bad/.removed_protection_cache.json')
    try:
        from django.db.models import F
        raw_ids = list(
            CatalogItem.objects.filter(files__file_name__icontains='nodrm')
            .order_by(F('created_at').desc(nulls_last=True), 'title')
            .values_list('id', flat=True)
        )
        seen = set()
        dedup_ids = []
        for cid in raw_ids:
            if cid not in seen:
                seen.add(cid)
                dedup_ids.append(cid)
        _REMOVED_PROTECTION_CACHE['ids'] = dedup_ids
        _REMOVED_PROTECTION_CACHE['expires_at'] = now_m + 600.0
        try:
            if disk_file.parent.exists():
                disk_file.write_text(json.dumps([str(x) for x in dedup_ids]), encoding='utf-8')
        except Exception:
            pass
        return dedup_ids
    except Exception:
        return _REMOVED_PROTECTION_CACHE['ids']
    finally:
        _REMOVED_PROTECTION_CACHE['_refreshing'] = False


def get_removed_protection_item_ids() -> List[Any]:
    """
    Return the list of `CatalogItem.id`s that have at least one file with 'nodrm'
    in `file_name`, ordered newest first (`created_at DESC, title ASC`).
    Cached for 600s in memory with stale-while-revalidate and persisted to disk.
    """
    import uuid as _uuid
    now_m = time.monotonic()
    if now_m < _REMOVED_PROTECTION_CACHE['expires_at'] and _REMOVED_PROTECTION_CACHE['ids']:
        return _REMOVED_PROTECTION_CACHE['ids']
    if _REMOVED_PROTECTION_CACHE['ids']:
        if not _REMOVED_PROTECTION_CACHE.get('_refreshing'):
            _REMOVED_PROTECTION_CACHE['_refreshing'] = True
            _REMOVED_PROTECTION_CACHE['expires_at'] = now_m + 600.0
            import threading
            threading.Thread(target=_rebuild_removed_protection_cache, daemon=True).start()
        return _REMOVED_PROTECTION_CACHE['ids']
    disk_file = Path('/books_bad/.removed_protection_cache.json')
    if disk_file.exists():
        try:
            age_s = max(0.0, time.time() - disk_file.stat().st_mtime)
            raw_disk = json.loads(disk_file.read_text(encoding='utf-8')) or []
            if isinstance(raw_disk, list) and raw_disk:
                _REMOVED_PROTECTION_CACHE['ids'] = [_uuid.UUID(str(x)) for x in raw_disk]
                _REMOVED_PROTECTION_CACHE['expires_at'] = now_m + (600.0 if age_s < 1800.0 else 30.0)
                if age_s >= 1800.0 and not _REMOVED_PROTECTION_CACHE.get('_refreshing'):
                    _REMOVED_PROTECTION_CACHE['_refreshing'] = True
                    import threading
                    threading.Thread(target=_rebuild_removed_protection_cache, daemon=True).start()
                return _REMOVED_PROTECTION_CACHE['ids']
        except Exception:
            pass
    return _rebuild_removed_protection_cache()


_PACKT_BOOKS_CACHE: Dict[str, Any] = {'expires_at': 0.0, 'dir_mtime': 0.0, 'ids': [], '_refreshing': False}


def _rebuild_packt_books_cache() -> List[Any]:
    from datetime import datetime as _dt, timezone as _tz
    from django.db.models import Q

    now_m = time.monotonic()
    packt_dir: Optional[Path] = None
    cur_dir_mtime = 0.0
    for cand_dir in (
        Path('/books/Computer Mags & FAQ/978'),
        Path('/volume1/common/Media/Books/Computer Mags & FAQ/978'),
    ):
        try:
            if cand_dir.exists() and cand_dir.is_dir():
                packt_dir = cand_dir
                cur_dir_mtime = cand_dir.stat().st_mtime
                break
        except Exception:
            continue

    disk_file = Path('/books_bad/.packt_books_cache.json')
    try:
        mtime_by_name: Dict[str, float] = {}
        if packt_dir is not None:
            try:
                for entry in os.scandir(packt_dir):
                    if entry.is_file():
                        try:
                            mtime_by_name[entry.name] = entry.stat().st_mtime
                        except Exception:
                            pass
            except Exception:
                pass

        rows = list(
            FileLocation.objects.filter(
                Q(file_path__startswith='/books/Computer Mags & FAQ/978/')
                | Q(file_path__startswith='/volume1/common/Media/Books/Computer Mags & FAQ/978/')
            ).values_list(
                'id',
                'catalog_item_id',
                'file_name',
                'file_path',
                'file_modified_at',
                'file_created_at',
                'catalog_item__created_at',
                'catalog_item__title',
            )
        )

        best_by_item: Dict[Any, Tuple[float, str]] = {}
        fl_updates: List[FileLocation] = []
        for fl_id, cid, fname, fpath, f_mod, f_cre, ci_cre, ci_title in rows:
            if not cid:
                continue
            bname = os.path.basename(str(fpath or fname or ''))
            disk_ts = mtime_by_name.get(str(fname or '')) or mtime_by_name.get(bname)
            if disk_ts:
                eff_ts = float(disk_ts)
                db_ts = f_mod.timestamp() if f_mod is not None else None
                if db_ts is None or abs(db_ts - eff_ts) > 60.0:
                    fl_obj = FileLocation(
                        id=fl_id,
                        file_modified_at=_dt.fromtimestamp(eff_ts, tz=_tz.utc),
                    )
                    fl_updates.append(fl_obj)
            elif f_mod is not None:
                eff_ts = f_mod.timestamp()
            elif f_cre is not None:
                eff_ts = f_cre.timestamp()
            elif ci_cre is not None:
                eff_ts = ci_cre.timestamp()
            else:
                eff_ts = 0.0

            t_low = str(ci_title or '').strip().lower()
            prev = best_by_item.get(cid)
            if prev is None or eff_ts > prev[0]:
                best_by_item[cid] = (eff_ts, t_low)

        if fl_updates:
            try:
                FileLocation.objects.bulk_update(fl_updates, ['file_modified_at'], batch_size=500)
            except Exception:
                pass

        sorted_ids = [
            cid
            for cid, _meta in sorted(
                best_by_item.items(),
                key=lambda kv: (-kv[1][0], kv[1][1]),
            )
        ]
        _PACKT_BOOKS_CACHE['ids'] = sorted_ids
        _PACKT_BOOKS_CACHE['dir_mtime'] = cur_dir_mtime
        _PACKT_BOOKS_CACHE['expires_at'] = now_m + 300.0
        try:
            if disk_file.parent.exists():
                disk_file.write_text(
                    json.dumps({'dir_mtime': cur_dir_mtime, 'ids': [str(x) for x in sorted_ids]}),
                    encoding='utf-8',
                )
        except Exception:
            pass
        return sorted_ids
    except Exception:
        return _PACKT_BOOKS_CACHE['ids']
    finally:
        _PACKT_BOOKS_CACHE['_refreshing'] = False


def get_packt_books_shelf_ids() -> List[Any]:
    """
    Return the list of `CatalogItem.id`s whose files are in `/books/Computer Mags & FAQ/978/`
    (or `/volume1/common/Media/Books/Computer Mags & FAQ/978/`), sorted by most recent
    physical file modification date (`file_modified_at` / filesystem `st_mtime`) first!
    """
    import uuid as _uuid

    now_m = time.monotonic()
    if now_m < _PACKT_BOOKS_CACHE['expires_at'] and _PACKT_BOOKS_CACHE['ids']:
        return _PACKT_BOOKS_CACHE['ids']

    if _PACKT_BOOKS_CACHE['ids']:
        if not _PACKT_BOOKS_CACHE.get('_refreshing'):
            _PACKT_BOOKS_CACHE['_refreshing'] = True
            _PACKT_BOOKS_CACHE['expires_at'] = now_m + 300.0
            import threading
            threading.Thread(target=_rebuild_packt_books_cache, daemon=True).start()
        return _PACKT_BOOKS_CACHE['ids']

    disk_file = Path('/books_bad/.packt_books_cache.json')
    if disk_file.exists():
        try:
            age_s = max(0.0, time.time() - disk_file.stat().st_mtime)
            raw_disk = json.loads(disk_file.read_text(encoding='utf-8')) or {}
            disk_ids = raw_disk.get('ids') if isinstance(raw_disk, dict) else raw_disk
            saved_dm = raw_disk.get('dir_mtime', 0.0) if isinstance(raw_disk, dict) else 0.0
            if isinstance(disk_ids, list) and disk_ids:
                _PACKT_BOOKS_CACHE['ids'] = [_uuid.UUID(str(x)) for x in disk_ids]
                _PACKT_BOOKS_CACHE['dir_mtime'] = saved_dm
                _PACKT_BOOKS_CACHE['expires_at'] = now_m + (300.0 if age_s < 1800.0 else 30.0)
                if age_s >= 1800.0 and not _PACKT_BOOKS_CACHE.get('_refreshing'):
                    _PACKT_BOOKS_CACHE['_refreshing'] = True
                    import threading
                    threading.Thread(target=_rebuild_packt_books_cache, daemon=True).start()
                return _PACKT_BOOKS_CACHE['ids']
        except Exception:
            pass

    return _rebuild_packt_books_cache()


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
        _BAD_METADATA_CACHE['expires_at'] = 0.0
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
    against BookDB `CatalogItem` records using fast indexed batch lookups (never scanning
    all 1.15M rows), persist to `scanner/amazon_library_state.json`, and invalidate the
    live Amazon shelf cache.
    """
    import uuid as _uuid

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
        base_t = re.split(r'[:(\-–—\[]', t or '', maxsplit=1)[0].strip()
        return _norm_lookup_str(base_t) if len(base_t) >= 4 else _norm_lookup_str(t)

    # 1. Collect candidate ASINs, existing CatalogItem UUIDs, and candidate Title strings
    #    so we only query indexed rows in BookDB (< 50ms total instead of scanning 1.15M rows).
    cand_asins: set = set()
    cand_cids: set = set()
    cand_titles: set = set()

    for r in existing_by_key.values():
        if r.get('asin'):
            a_str = str(r['asin']).strip()
            cand_asins.add(a_str)
            cand_asins.add(a_str.upper())
        if r.get('catalog_item_id'):
            try:
                cand_cids.add(_uuid.UUID(str(r['catalog_item_id'])))
            except Exception:
                pass

    for item_data in raw_items:
        asin_val = str(item_data.get('asin') or '').strip()
        if asin_val:
            cand_asins.add(asin_val)
            cand_asins.add(asin_val.upper())
        t_clean = _clean_single_line(item_data.get('title') or '')
        if t_clean:
            cand_titles.add(t_clean[:512])
            base_t = re.split(r'[:(\-–—\[]', t_clean, maxsplit=1)[0].strip()
            if len(base_t) >= 3:
                cand_titles.add(base_t[:512])
            no_ed = re.sub(r'\s*\([^)]*\)\s*', ' ', t_clean).strip()
            if len(no_ed) >= 3:
                cand_titles.add(no_ed[:512])

    ci_by_id: Dict[str, CatalogItem] = {}
    ci_by_asin: Dict[str, CatalogItem] = {}
    ci_by_norm_title: Dict[str, List[CatalogItem]] = {}
    ci_by_short_title: Dict[str, List[CatalogItem]] = {}

    def _index_ci(ci_obj: CatalogItem) -> None:
        cid_s = str(ci_obj.id)
        if cid_s in ci_by_id:
            return
        ci_by_id[cid_s] = ci_obj
        if ci_obj.asin:
            ci_by_asin[ci_obj.asin.strip().upper()] = ci_obj
        nt_val = _norm_lookup_str(ci_obj.title)
        if nt_val:
            ci_by_norm_title.setdefault(nt_val, []).append(ci_obj)
        st_val = _short_norm_title(ci_obj.title)
        if st_val and len(st_val) >= 4:
            ci_by_short_title.setdefault(st_val, []).append(ci_obj)

    only_fields = ('id', 'title', 'authors', 'asin', 'cover_image_url', 'item_type')

    # 1a. Existing KINDLE / AUDIBLE items
    for ci_obj in CatalogItem.objects.filter(item_type__in=['KINDLE', 'AUDIBLE']).only(*only_fields).order_by():
        _index_ci(ci_obj)

    # 1b. Known UUIDs from previous state file
    cid_list = list(cand_cids)
    for i in range(0, len(cid_list), 1000):
        for ci_obj in CatalogItem.objects.filter(id__in=cid_list[i : i + 1000]).only(*only_fields).order_by():
            _index_ci(ci_obj)

    # 1c. Candidate ASINs
    asin_list = [a for a in cand_asins if a]
    for i in range(0, len(asin_list), 1000):
        for ci_obj in CatalogItem.objects.filter(asin__in=asin_list[i : i + 1000]).only(*only_fields).order_by():
            _index_ci(ci_obj)

    # 1d. Candidate Titles (exact indexed B-tree lookup on `title`)
    title_list = [t for t in cand_titles if t]
    for i in range(0, len(title_list), 1000):
        for ci_obj in CatalogItem.objects.filter(title__in=title_list[i : i + 1000]).only(*only_fields).order_by():
            _index_ci(ci_obj)

    kindle_purchased = 0
    kindle_plus = 0
    audible_total = 0
    with_bookmarks = 0
    matched_catalog_items = 0
    to_update_map: Dict[Any, CatalogItem] = {}
    to_create_list: List[CatalogItem] = []

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
        if asin_upper and asin_upper in ci_by_asin and ci_by_asin[asin_upper].item_type == 'BOOK':
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

        if not ci and asin_upper and asin_upper in ci_by_asin:
            ci = ci_by_asin[asin_upper]

        if not ci:
            prev_rec = existing_by_key.get(asin or f"{itype}:{title}")
            if prev_rec and prev_rec.get('catalog_item_id'):
                ci = ci_by_id.get(str(prev_rec['catalog_item_id']))

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
            ci = CatalogItem(
                id=_uuid.uuid4(),
                title=title[:512],
                authors=authors[:512],
                asin=asin[:32] if asin else None,
                item_type='AUDIBLE' if itype == 'AUDIBLE' else 'KINDLE',
                cover_image_url=cover_url[:1024] if cover_url else None,
            )
            to_create_list.append(ci)
            _index_ci(ci)

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

    if to_create_list:
        CatalogItem.objects.bulk_create(to_create_list, batch_size=500, ignore_conflicts=True)

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
    amz_shelf = get_dynamic_amazon_shelf()
    total_matched_nas = int((amz_shelf.get('counts') or {}).get('amazon_matched') or matched_catalog_items)
    return {
        'ok': True,
        'total_synced': len(merged_list),
        'kindle_purchased': kindle_purchased,
        'kindle_plus': kindle_plus,
        'audible_total': audible_total,
        'with_bookmarks': with_bookmarks,
        'matched_catalog_items': total_matched_nas,
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
            for i in range(0, len(all_ids), 1000):
                chunk_ids = all_ids[i : i + 1000]
                for pk in (
                    FileLocation.objects.filter(catalog_item_id__in=chunk_ids)
                    .order_by()
                    .values_list('catalog_item_id', flat=True)
                    .distinct()
                ):
                    ids_with_local_files.add(str(pk))
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
        if hasattr(item, '_prefetched_objects_cache') and isinstance(item._prefetched_objects_cache, dict):
            item._prefetched_objects_cache.pop('files', None)
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
        missing_cover_count = int(_NEEDS_COVER_SHELF_CACHE.get('count') or 0)

        bm_label = f'Bookmarked & Reading in Moon+ ({bm_count:,} Books)' if bm_count else 'Bookmarked & Reading in Moon+'
        interesting_label = f'Interesting Books ({interesting_count:,} Books)' if interesting_count else 'Interesting Books'
        bad_meta_label = f'Bad Metadata Books ({bad_meta_count:,} Books)' if bad_meta_count else 'Bad Metadata Books'
        missing_cover_label = f'Enriched Books: Missing / Text Cover ({missing_cover_count:,} Books)' if missing_cover_count else 'Enriched Books: Missing / Text Cover'
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
                'id': 'urn:mylibrary:opds:missing_covers',
                'title': missing_cover_label,
                'content': 'Enriched books where the cover is missing or is just a text-only title page.',
                'href': f'{base}/missing-covers/',
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
                'id': 'urn:mylibrary:opds:packt',
                'title': f'Packt Books ({len(get_packt_books_shelf_ids()):,} Books)',
                'content': 'Books in Computer Mags & FAQ / 978, sorted by most recent file date first.',
                'href': f'{base}/packt/',
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


class OPDSPacktFeedView(View):
    """
    Dynamic Virtual Bookshelf of all books in `/books/Computer Mags & FAQ/978/`
    (`/mylibrary/opds/packt/`), sorted by most recent file date first.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        from django.db.models import Case, IntegerField, When

        try:
            page = max(int(request.GET.get('page', 1)), 1)
        except ValueError:
            page = 1
        page_size = 25
        offset = (page - 1) * page_size

        id_list = get_packt_books_shelf_ids()
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
                .annotate(_packt_order=order_expr)
                .order_by('_packt_order')
            )

        return _render_acquisition_feed(
            request,
            feed_id='urn:mylibrary:opds:packt',
            title=f'Packt Books ({len(id_list):,} Books — Newest File Date First)',
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


class OPDSMissingCoverFeedView(View):
    """
    Dynamic Virtual Bookshelf of enriched books whose cover is missing or is just a text page
    (`/mylibrary/opds/missing-covers/`).
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        from django.db.models import Case, IntegerField, When

        try:
            page = max(int(request.GET.get('page', 1)), 1)
        except ValueError:
            page = 1
        page_size = 25
        offset = (page - 1) * page_size

        shelf_data = get_needs_cover_enriched_shelf()
        id_list = shelf_data.get('item_ids') or []
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
                .annotate(_cov_order=order_expr)
                .order_by('_cov_order')
            )

        return _render_acquisition_feed(
            request,
            feed_id='urn:mylibrary:opds:missing_covers',
            title=f'Enriched Books: Missing / Text Cover ({len(id_list):,} Books)',
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
        if page_items and not isinstance(page_items[0], CatalogItem):
            moon_meta_map = bm_data.get('moon_meta_by_id') or {}
            pids = [it.id for it in page_items]
            by_id = {
                ci.id: ci
                for ci in CatalogItem.objects.filter(id__in=pids).prefetch_related('files')
            }
            hydrated = []
            for pid in pids:
                ci = by_id.get(pid)
                if ci:
                    ci.moon_bookmark_info = moon_meta_map.get(str(pid))
                    hydrated.append(ci)
            page_items = hydrated

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
            is_pdf_item = _item_has_pdf_file(item)
            if (
                has_excessive_white_border_cover(item.cover_thumbnail)
                or is_partial_banner_cover(item.cover_thumbnail)
                or (is_pdf_item and is_legacy_scanner_pdf_cover(item.cover_thumbnail))
            ):
                try:
                    cropped_uri = extract_original_cover_for_item(item, ignore_backup=True)
                    if (
                        cropped_uri
                        and cropped_uri.startswith('data:image')
                        and not has_excessive_white_border_cover(cropped_uri)
                        and not is_partial_banner_cover(cropped_uri)
                    ):
                        item.cover_thumbnail = cropped_uri
                        item.save(update_fields=['cover_thumbnail'])
                except Exception:
                    pass
            try:
                header, b64_data = item.cover_thumbnail.split(',', 1)
                mime = header.split(';')[0].replace('data:', '') or 'image/jpeg'
                return HttpResponse(base64.b64decode(b64_data), content_type=mime)
            except Exception:
                pass

        try:
            orig_uri = extract_original_cover_for_item(item)
            if orig_uri and orig_uri.startswith('data:image'):
                item.cover_thumbnail = orig_uri
                item.save(update_fields=['cover_thumbnail'])
                header, b64_data = orig_uri.split(',', 1)
                mime = header.split(';')[0].replace('data:', '') or 'image/jpeg'
                return HttpResponse(base64.b64decode(b64_data), content_type=mime)
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
    if (not phys_path or not phys_path.exists()) and resolved_loc and resolved_loc.file_path and resolved_loc.file_path != raw_path:
        raw_path = resolved_loc.file_path
        phys_path = _resolve_physical_file_path(raw_path)
    if (not phys_path or not phys_path.exists()) and item_id:
        item = CatalogItem.objects.prefetch_related('files').filter(id=item_id).first()
        if item:
            for cand in item.files.all():
                if cand.file_path:
                    cand_phys = _resolve_physical_file_path(cand.file_path)
                    if cand_phys and cand_phys.exists():
                        resolved_loc = cand
                        raw_path = cand.file_path
                        phys_path = cand_phys
                        break
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


