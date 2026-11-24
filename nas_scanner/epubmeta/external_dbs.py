"""Integration with external databases (MyLibrary BookDB, scanner_state.db, and Calibre metadata.db)."""

from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import posixpath
import re
import sqlite3
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

from .isbn.validator import normalize_isbn
from .models import BookMetadata
from .state import StateTracker, normalize_cache_stem, normalize_title_author_key

logger = logging.getLogger(__name__)

# Known mount-point prefixes for translating paths between Windows SMB, NAS SSH host, and Docker container
_KNOWN_BOOK_PREFIXES = (
    "/volume1/common/media/books/",
    "/books/",
    "u:/media/books/",
)


def compute_full_sha256(file_path: Path) -> str:
    """Compute the full-file SHA-256 hex digest (matching MyLibrary's get_file_hash)."""
    try:
        sha256_hash = hashlib.sha256()
        with open(file_path, "rb") as f:
            for byte_block in iter(lambda: f.read(65536), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()
    except Exception as e:
        logger.debug("Failed computing full SHA-256 for %s: %s", file_path, e)
        return ""


def _autocrop_white_border_around_cover(img: Any) -> Any:
    """
    If `img` has an excessive white/light page border around a smaller centered graphic cover
    (for example, a small cover image rendered on a blank 8.5x11" white page, or an 850x1100 EPUB
    cover canvas with light side margins and a blank white bottom half), crop to the cover artwork rectangle.
    """
    try:
        rgb = img.convert("RGB")
        w, h = rgb.size
        if w < 80 or h < 100:
            return img
        SW, SH = 100, 140
        small = rgb.resize((SW, SH))
        px = list(small.get_flattened_data() if hasattr(small, "get_flattened_data") else small.getdata())

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
            return img

        row_fg = [sum(1 for c in range(SW) if not pad_mask[r][c]) for r in range(SH)]
        active_rows = [r for r in range(SH) if row_fg[r] >= 18]
        if not active_rows:
            return img
        r0, r1 = min(active_rows), max(active_rows)

        col_fg_in_rows = [sum(1 for r in range(r0, r1 + 1) if not pad_mask[r][c]) for c in range(SW)]
        min_col_hits = max(8, int((r1 - r0 + 1) * 0.25))
        active_cols = [c for c in range(SW) if col_fg_in_rows[c] >= min_col_hits]
        if not active_cols:
            return img
        c0, c1 = min(active_cols), max(active_cols)

        box_w = (c1 - c0 + 1) / SW
        box_h = (r1 - r0 + 1) / SH
        box_area = box_w * box_h
        if not (0.08 <= box_area <= 0.82 and box_w >= 0.22 and box_h >= 0.22):
            return img

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
            return img
        if (in_fg / in_tot) < 0.70 or (out_pad / out_tot) < 0.94:
            return img

        crop_w = int((c1 - c0 + 1) * w / SW)
        crop_h = int((r1 - r0 + 1) * h / SH)
        if crop_w < 60 or crop_h < 80 or not (0.42 <= (crop_w / crop_h) <= 1.25):
            return img

        x0 = max(0, int(c0 * w / SW))
        y0 = max(0, int(r0 * h / SH))
        x1 = min(w, int((c1 + 1) * w / SW))
        y1 = min(h, int((r1 + 1) * h / SH))
        return img.crop((x0, y0, x1, y1))
    except Exception:
        return img


def generate_cover_thumbnail(
    image_bytes: Optional[bytes],
    max_size: Tuple[int, int] = (200, 300),
) -> Optional[str]:
    """Generate a base64-encoded JPEG thumbnail data URI from raw cover image bytes."""
    if not image_bytes:
        return None
    try:
        import base64
        import io
        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes))
        img = _autocrop_white_border_around_cover(img)
        img.thumbnail(max_size)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        out_buf = io.BytesIO()
        img.save(out_buf, format="JPEG", quality=80)
        base64_str = base64.b64encode(out_buf.getvalue()).decode("utf-8")
        return f"data:image/jpeg;base64,{base64_str}"
    except Exception as e:
        logger.debug("Failed generating cover thumbnail: %s", e)
        return None



def extract_relative_book_subpath(raw_path: str) -> Optional[str]:
    """
    Extract a normalized relative subpath beneath the root Books share so paths like:
      - U:\\Media\\Books\\SciFi\\Dune.epub
      - /volume1/common/Media/Books/SciFi/Dune.epub
      - /books/SciFi/Dune.epub
    all map to 'scifi/dune.epub' (case-insensitive key) and preserve original relative segments.
    """
    norm = raw_path.replace("\\", "/")
    norm_lower = norm.lower()
    for prefix in _KNOWN_BOOK_PREFIXES:
        idx = norm_lower.find(prefix)
        if idx != -1:
            rel = norm[idx + len(prefix) :].lstrip("/")
            if rel:
                return rel
    return None


def to_container_books_path(file_path: Path, container_root: str = "/books") -> str:
    """Map a local/NAS/SMB path under the Books store to the Docker container path (/books/...)."""
    raw = str(file_path.resolve())
    rel = extract_relative_book_subpath(raw)
    if rel:
        return posixpath.join(container_root.rstrip("/"), rel)
    return raw.replace("\\", "/")


class ScannerStateBridge:
    """
    Bridge to MyLibrary's local SQLite scanner cache (`nas_scanner/scanner_state.db`).
    - Queries indexed `file_states` (`filepath TEXT PRIMARY KEY`) on demand without loading
      millions of rows into memory.
    - Reuses pre-computed full SHA-256 hashes when (modified_at, size) match, avoiding disk reads.
    - Updates `file_states` when epubmeta modifies a file so MyLibrary's scanner stays in sync.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path).resolve()

    def lookup_entry(self, file_path: Path) -> Optional[Dict[str, Any]]:
        from .pg_scanner_db import connect_scanner_db, is_postgres_scanner_enabled
        if not is_postgres_scanner_enabled() and not self.db_path.exists():
            return None
        raw = str(file_path)
        candidates = [raw, raw.replace("\\", "/")]
        rel = extract_relative_book_subpath(raw)
        if rel:
            candidates.append(posixpath.join("/books", rel))
            candidates.append(posixpath.join("/volume1/common/Media/Books", rel))
        try:
            with connect_scanner_db(self.db_path, timeout=30.0) as conn:
                conn.row_factory = sqlite3.Row
                for cand in dict.fromkeys(candidates):
                    row = conn.execute(
                        "SELECT filepath, modified_at, size, hash FROM file_states WHERE filepath = ? LIMIT 1",
                        (cand,),
                    ).fetchone()
                    if row:
                        return {
                            "filepath": row["filepath"],
                            "modified_at": str(row["modified_at"]),
                            "size": int(row["size"]) if row["size"] is not None else -1,
                            "hash": row["hash"],
                        }
        except Exception as e:
            logger.debug("Failed querying scanner_state.db for %s: %s", file_path, e)
        return None

    def get_cached_sha256(self, file_path: Path) -> Optional[str]:
        """Return pre-computed SHA-256 from scanner_state.db if size and mtime match."""
        entry = self.lookup_entry(file_path)
        if not entry or not entry.get("hash"):
            return None
        try:
            stat = file_path.stat()
            if entry["size"] != stat.st_size:
                return None
            # Compare mtime (allowing tiny float formatting differences)
            cached_mtime = float(entry["modified_at"])
            if abs(cached_mtime - stat.st_mtime) <= 0.01:
                return entry["hash"]
        except Exception:
            pass
        return None

    def update_file_state(
        self,
        file_path: Path,
        sha256_hash: str,
        size: Optional[int] = None,
        modified_at: Optional[float] = None,
    ) -> None:
        """Update scanner_state.db with the new size/hash after epubmeta modifies a file."""
        if not sha256_hash:
            return
        try:
            stat = file_path.stat()
            actual_size = size if size is not None else stat.st_size
            actual_mtime = str(modified_at if modified_at is not None else stat.st_mtime)
        except Exception:
            return

        existing = self.lookup_entry(file_path)
        target_filepath = (
            existing["filepath"]
            if existing
            else to_container_books_path(file_path)
        )
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

        try:
            from .pg_scanner_db import connect_scanner_db
            with connect_scanner_db(self.db_path, timeout=30.0) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS file_states (
                        filepath TEXT PRIMARY KEY,
                        modified_at TEXT,
                        size INTEGER,
                        hash TEXT,
                        synced_at TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO file_states (filepath, modified_at, size, hash, synced_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(filepath) DO UPDATE SET
                        modified_at = excluded.modified_at,
                        size = excluded.size,
                        hash = excluded.hash,
                        synced_at = excluded.synced_at
                    """,
                    (target_filepath, actual_mtime, actual_size, sha256_hash, now),
                )
        except Exception as e:
            logger.debug("Failed updating scanner_state.db for %s: %s", file_path, e)

    def remove_file_state(self, file_path: Path) -> None:
        """Remove a deleted or quarantined file from scanner_state.db `file_states`."""
        from .pg_scanner_db import connect_scanner_db, is_postgres_scanner_enabled
        if not is_postgres_scanner_enabled() and not self.db_path.exists():
            return
        raw = str(file_path)
        candidates = [raw, raw.replace("\\", "/")]
        rel = extract_relative_book_subpath(raw)
        if rel:
            candidates.append(posixpath.join("/books", rel))
            candidates.append(posixpath.join("/volume1/common/Media/Books", rel))
        try:
            with connect_scanner_db(self.db_path, timeout=30.0) as conn:
                for cand in dict.fromkeys(candidates):
                    conn.execute("DELETE FROM file_states WHERE filepath = ?", (cand,))
        except Exception as e:
            logger.debug("Failed removing %s from scanner_state.db file_states: %s", file_path, e)


def _strip_html_tags(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def seed_cache_from_calibre_db(
    calibre_db_path: Path,
    tracker: StateTracker,
) -> int:
    """
    Read curated book metadata from a Calibre `metadata.db` SQLite database and seed
    `StateTracker.metadata_cache` so loose duplicates/PDFs can reuse it without API/AI calls.
    Returns the number of books seeded into the cache.
    """
    db_file = Path(calibre_db_path).resolve()
    if db_file.is_dir():
        db_file = db_file / "metadata.db"
    if not db_file.exists():
        logger.warning("Calibre metadata.db not found at %s", db_file)
        return 0

    library_root = db_file.parent
    seeded = 0
    try:
        with sqlite3.connect(str(db_file), timeout=30.0) as cal_conn:
            cal_conn.row_factory = sqlite3.Row
            books = cal_conn.execute(
                """
                SELECT
                    b.id AS book_id,
                    b.title,
                    b.pubdate,
                    b.path AS book_dir_path,
                    c.text AS description,
                    (SELECT group_concat(a.name, '||')
                     FROM books_authors_link bal
                     JOIN authors a ON bal.author = a.id
                     WHERE bal.book = b.id) AS authors_raw,
                    (SELECT p.name
                     FROM books_publishers_link bpl
                     JOIN publishers p ON bpl.publisher = p.id
                     WHERE bpl.book = b.id LIMIT 1) AS publisher,
                    (SELECT i.val
                     FROM identifiers i
                     WHERE i.book = b.id AND lower(i.type) = 'isbn' LIMIT 1) AS isbn,
                    (SELECT group_concat(t.name, '||')
                     FROM books_tags_link btl
                     JOIN tags t ON btl.tag = t.id
                     WHERE btl.book = b.id) AS tags_raw
                FROM books b
                LEFT JOIN comments c ON b.id = c.book
                """
            ).fetchall()

            for row in books:
                raw_isbn = row["isbn"]
                isbn13, isbn10 = normalize_isbn(raw_isbn) if raw_isbn else (None, None)
                clean_isbn = isbn13 or isbn10
                title = (row["title"] or "").strip()
                authors_raw = row["authors_raw"] or ""
                authors = [
                    a.strip()
                    for a in authors_raw.split("||")
                    if a.strip() and a.strip().lower() not in ("unknown", "unknown author")
                ]
                description = _strip_html_tags(row["description"])
                publisher = (row["publisher"] or "").strip() or None
                pub_date_str = str(row["pubdate"] or "")
                pub_date = None
                if pub_date_str and not pub_date_str.startswith(("0101", "0001")):
                    pub_date = pub_date_str[:10]
                subjects = [
                    t.strip() for t in (row["tags_raw"] or "").split("||") if t.strip()
                ]

                # Only seed records that actually provide useful curated metadata
                if not title or (not clean_isbn and not (authors and (description or publisher))):
                    continue

                meta = BookMetadata(
                    title=title,
                    authors=authors,
                    description=description,
                    publisher=publisher,
                    published_date=pub_date,
                    isbn13=isbn13,
                    isbn10=isbn10,
                    subjects=subjects,
                    source="calibre-db",
                )

                # Check if already in cache
                primary_author = authors[0] if authors else None
                existing = tracker.get_cached_metadata(
                    isbn=clean_isbn,
                    title=title,
                    author=primary_author,
                )
                if existing:
                    continue

                # Find format filenames in Calibre's `data` table so stem lookups match too
                formats = cal_conn.execute(
                    "SELECT format, name FROM data WHERE book = ?",
                    (row["book_id"],),
                ).fetchall()
                if formats:
                    for fmt in formats:
                        ext = (fmt["format"] or "epub").lower()
                        fname = f"{fmt['name']}.{ext}"
                        rel_dir = row["book_dir_path"] or ""
                        pseudo_path = library_root / rel_dir / fname
                        tracker.save_cached_metadata(pseudo_path, meta)
                else:
                    pseudo_path = library_root / f"{title}.epub"
                    tracker.save_cached_metadata(pseudo_path, meta)
                seeded += 1

        if seeded:
            logger.info("Seeded %d curated book(s) from Calibre DB (%s)", seeded, db_file)
    except Exception as e:
        logger.warning("Failed reading Calibre metadata.db at %s: %s", db_file, e)
    return seeded


def discover_and_seed_calibre_dbs(
    target_dir: Path,
    tracker: StateTracker,
    recursive: bool = True,
) -> int:
    """Discover any `metadata.db` files inside `target_dir` and seed `tracker.metadata_cache`."""
    import os
    target_dir = Path(target_dir).resolve()
    if target_dir.is_file():
        target_dir = target_dir.parent

    total_seeded = 0
    skip_dirs = {"_quarantine", "bak", "@eadir", "#recycle", ".recycle", "computer book code", "file formats", "computer mags & faq"}
    if recursive:
        for root, dirs, filenames in os.walk(target_dir):
            dirs[:] = [d for d in dirs if d.lower() not in skip_dirs and not d.startswith("._")]
            if "metadata.db" in filenames:
                total_seeded += seed_cache_from_calibre_db(Path(root) / "metadata.db", tracker)
    else:
        cand = target_dir / "metadata.db"
        if cand.exists():
            total_seeded += seed_cache_from_calibre_db(cand, tracker)
    return total_seeded


class MyLibraryClient:
    """
    Client for syncing metadata and file state with the MyLibrary Django server (`BookDB`).
    - Pre-fetches curated `CatalogItem` metadata and corrupt/DRM file flags via `GET /api/sync/cache/`.
    - Pushes updated file metadata after `epubmeta` modifies a file via `POST /api/sync/file/`.
    """

    def __init__(
        self,
        server_url: str,
        api_token: str,
        timeout: float = 60.0,
    ):
        self.server_url = server_url.rstrip("/")
        self.api_token = api_token
        self.timeout = timeout
        self.skip_hashes: Set[str] = set()
        self.skip_rel_paths: Set[str] = set()
        self.complete_hashes: Set[str] = set()
        self.complete_rel_paths: Set[str] = set()

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }

    def pull_and_seed_cache(
        self,
        tracker: Optional[StateTracker] = None,
        skip_complete: bool = False,
    ) -> Tuple[int, int]:
        """
        Fetch known catalog items and invalid/DRM/complete file flags directly from PostgreSQL BookDB
        (or fallback to `GET /api/sync/cache/`).
        Returns `(seeded_items_count, skipped_files_registered_count)`.
        """
        already_seeded = False
        if tracker:
            try:
                with tracker._get_connection() as conn:
                    cnt = conn.execute("SELECT COUNT(*) FROM metadata_cache").fetchone()[0]
                    if cnt >= 1000:
                        already_seeded = True
            except Exception:
                pass

        # Fast & low-memory path: query PostgreSQL BookDB directly without a 60 MB HTTP JSON round-trip
        try:
            from .pg_scanner_db import connect_scanner_db, is_postgres_scanner_enabled
            if is_postgres_scanner_enabled():
                conn_pg = connect_scanner_db(Path("scanner_state.db"), timeout=30.0)
                try:
                    cur = conn_pg.cursor()
                    cur.execute(
                        "SELECT file_path FROM catalog_filelocation WHERE is_invalid = TRUE OR has_drm = TRUE"
                    )
                    while True:
                        rows = cur.fetchmany(25000)
                        if not rows:
                            break
                        for r in rows:
                            p = r[0]
                            if not p:
                                continue
                            rel = extract_relative_book_subpath(p)
                            self.skip_rel_paths.add(rel.lower() if rel else p.replace("\\", "/").lower())

                    if skip_complete:
                        cur.execute(
                            """
                            SELECT fl.file_path
                            FROM catalog_filelocation fl
                            JOIN catalog_catalogitem ci ON fl.catalog_item_id = ci.id
                            WHERE fl.is_invalid = FALSE AND fl.has_drm = FALSE
                              AND ((ci.isbn IS NOT NULL AND ci.isbn != '') OR (ci.asin IS NOT NULL AND ci.asin != ''))
                              AND ci.description IS NOT NULL AND ci.description != ''
                              AND ci.authors IS NOT NULL AND ci.authors != '' AND ci.authors != 'Unknown Author'
                            """
                        )
                        while True:
                            rows = cur.fetchmany(25000)
                            if not rows:
                                break
                            for r in rows:
                                p = r[0]
                                if not p:
                                    continue
                                rel = extract_relative_book_subpath(p)
                                self.complete_rel_paths.add(rel.lower() if rel else p.replace("\\", "/").lower())
                    cur.close()
                finally:
                    conn_pg.close()
                if already_seeded:
                    return 0, len(self.skip_rel_paths) + len(self.complete_rel_paths)
        except Exception as pg_err:
            logger.debug("Direct PostgreSQL cache pull skipped (%s); falling back to HTTP.", pg_err)

        url = f"{self.server_url}/api/sync/cache/"
        if already_seeded:
            url += "?skip_items=1"
        seeded = 0
        try:
            resp = requests.get(url, headers=self._headers(), timeout=self.timeout)
            if resp.status_code != 200:
                logger.warning(
                    "MyLibrary /api/sync/cache/ returned HTTP %s: %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return 0, 0
            data = resp.json()
            for h in data.get("invalid_or_drm_hashes", []):
                if h:
                    self.skip_hashes.add(h)
            for p in data.get("invalid_or_drm_paths", []):
                rel = extract_relative_book_subpath(p)
                self.skip_rel_paths.add(rel.lower() if rel else p.replace("\\", "/").lower())

            if skip_complete:
                for p in data.get("complete_paths", []):
                    rel = extract_relative_book_subpath(p)
                    self.complete_rel_paths.add(rel.lower() if rel else p.replace("\\", "/").lower())

            if tracker:
                now = datetime.now(timezone.utc).isoformat()
                with tracker._get_connection() as conn:
                    existing_isbns = {
                        row[0]
                        for row in conn.execute(
                            "SELECT isbn FROM metadata_cache WHERE isbn IS NOT NULL"
                        ).fetchall()
                    }
                    batch_rows = []
                    for item in data.get("catalog_items", []):
                        title = (item.get("title") or "").strip()
                        raw_authors = (item.get("authors") or "").strip()
                        authors = [
                            a.strip()
                            for a in re.split(r"\s*(?:&|,|;)\s*", raw_authors)
                            if a.strip() and a.strip().lower() not in ("unknown", "unknown author")
                        ]
                        raw_isbn = item.get("isbn")
                        isbn13, isbn10 = normalize_isbn(raw_isbn) if raw_isbn else (None, None)
                        clean_isbn = isbn13 or isbn10
                        if not title or not clean_isbn or clean_isbn in existing_isbns:
                            continue
                        existing_isbns.add(clean_isbn)
                        description = _strip_html_tags(item.get("description"))
                        publisher = (item.get("publisher") or "").strip() or None
                        pub_date = item.get("pub_date")

                        meta = BookMetadata(
                            title=title,
                            authors=authors,
                            description=description,
                            publisher=publisher,
                            published_date=pub_date,
                            isbn13=isbn13,
                            isbn10=isbn10,
                            source="mylibrary-db",
                        )
                        sample_name = item.get("sample_file_name") or f"{title}.epub"
                        norm_stem = normalize_cache_stem(Path(sample_name))
                        primary_author = authors[0] if authors else None
                        norm_ta = normalize_title_author_key(title, primary_author)
                        payload = json.dumps(meta.to_dict(), ensure_ascii=False)
                        batch_rows.append(
                            (sample_name, sample_name, None, clean_isbn, norm_stem or None, norm_ta or None, None, payload, now)
                        )
                    if batch_rows:
                        conn.executemany(
                            """
                            INSERT INTO metadata_cache
                                (source_file_path, source_file_name, orig_hash, isbn, norm_stem, norm_title_author, original_metadata_json, metadata_json, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            batch_rows,
                        )
                        seeded = len(batch_rows)

            return seeded, len(self.skip_rel_paths) + len(self.complete_rel_paths)
        except Exception as e:
            logger.warning("Could not pull catalog cache from MyLibrary (%s): %s", url, e)
            return 0, len(self.skip_rel_paths) + len(self.complete_rel_paths)

    def should_skip_file(
        self,
        file_path: Path,
        sha256_hash: Optional[str] = None,
        skip_complete: bool = False,
    ) -> Tuple[bool, Optional[str]]:
        """
        Check if MyLibrary already knows this file is corrupt/DRM-locked (or already complete).
        Returns `(should_skip, reason)`.
        """
        raw = str(file_path)
        norm_p = raw.replace("\\", "/").lower()
        rel = extract_relative_book_subpath(raw)
        rel_lower = rel.lower() if rel else None

        if (sha256_hash and sha256_hash in self.skip_hashes) or norm_p in self.skip_rel_paths or (
            rel_lower and rel_lower in self.skip_rel_paths
        ):
            return True, "flagged is_invalid or has_drm in MyLibrary BookDB"

        if skip_complete:
            if (sha256_hash and sha256_hash in self.complete_hashes) or norm_p in self.complete_rel_paths or (
                rel_lower and rel_lower in self.complete_rel_paths
            ):
                return True, "already has complete metadata in MyLibrary BookDB"

        return False, None

    def push_updated_file(
        self,
        file_path: Path,
        metadata: BookMetadata,
        file_sha256: str,
        orig_sha256: Optional[str] = None,
        container_root: str = "/books",
        cover_thumbnail: Optional[str] = None,
    ) -> bool:
        """Push updated file metadata and new SHA-256 hash directly to MyLibrary `/api/sync/file/`."""
        if not file_sha256:
            return False
        try:
            stat = file_path.stat()
            created_iso = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc).isoformat()
            modified_iso = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
            size = stat.st_size
        except Exception:
            return False

        pub_date = None
        if metadata.published_date:
            m = re.match(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", metadata.published_date.strip())
            if m:
                yr = m.group(1)
                mo = m.group(2) or "01"
                dy = m.group(3) or "01"
                pub_date = f"{yr}-{mo}-{dy}"

        if not cover_thumbnail and metadata.cover_bytes:
            cover_thumbnail = generate_cover_thumbnail(metadata.cover_bytes)

        raw_id = metadata.isbn13 or metadata.isbn10
        asin_val = metadata.asin
        isbn_val = raw_id
        if raw_id and re.match(r"^B[0-9A-Z]{9}$", raw_id.strip(), re.IGNORECASE):
            asin_val = asin_val or raw_id.strip().upper()
            isbn_val = None

        payload = {
            "file_hash": file_sha256,
            "orig_file_hash": orig_sha256,
            "file_path": to_container_books_path(file_path, container_root=container_root),
            "file_name": file_path.name,
            "file_size": size,
            "file_format": file_path.suffix.lstrip(".").upper(),
            "file_created_at": created_iso,
            "file_modified_at": modified_iso,
            "is_invalid": False,
            "has_drm": False,
            "item_type": "BOOK",
            "title": metadata.title or file_path.stem,
            "authors": ", ".join(metadata.authors) if metadata.authors else "Unknown Author",
            "isbn": isbn_val,
            "asin": asin_val,
            "publisher": metadata.publisher,
            "description": metadata.description,
            "pub_date": pub_date,
        }
        if cover_thumbnail:
            payload["cover_thumbnail"] = cover_thumbnail

        url = f"{self.server_url}/api/sync/file/"
        try:
            resp = requests.post(url, json=payload, headers=self._headers(), timeout=self.timeout)
            if resp.status_code in (200, 201):
                logger.info("Synced updated metadata for %s to MyLibrary BookDB", file_path.name)
                return True
            logger.warning(
                "MyLibrary sync returned HTTP %s for %s: %s",
                resp.status_code,
                file_path.name,
                resp.text[:200],
            )
        except Exception as e:
            logger.warning("Failed pushing %s to MyLibrary (%s): %s", file_path.name, url, e)
        return False

    def prune_deleted_file(self, file_path: Path, container_root: str = "/books") -> bool:
        """Immediately prune a quarantined/deleted file location (and orphan CatalogItem) from MyLibrary BookDB."""
        raw = str(file_path)
        deleted_paths = [
            to_container_books_path(file_path, container_root=container_root),
            raw,
            raw.replace("\\", "/"),
        ]
        rel = extract_relative_book_subpath(raw)
        if rel:
            deleted_paths.append(posixpath.join("/volume1/common/Media/Books", rel))
        clean_paths = list(dict.fromkeys(p for p in deleted_paths if p))
        url = f"{self.server_url}/api/sync/cleanup/"
        try:
            resp = requests.post(url, json={"deleted_paths": clean_paths}, headers=self._headers(), timeout=self.timeout)
            if resp.status_code == 200:
                logger.info("Pruned quarantined file %s from MyLibrary BookDB", file_path.name)
                return True
            logger.warning(
                "MyLibrary cleanup returned HTTP %s for %s: %s",
                resp.status_code,
                file_path.name,
                resp.text[:200],
            )
        except Exception as e:
            logger.warning("Failed pruning quarantined file %s from MyLibrary (%s): %s", file_path.name, url, e)
        return False
