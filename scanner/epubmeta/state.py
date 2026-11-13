"""SQLite state tracker for tracking processed book files, caching, and resumption."""

from __future__ import annotations
import contextlib
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any, Dict, Iterator, List, Optional

from .models import BookMetadata

logger = logging.getLogger(__name__)

_COPY_SUFFIXES = [
    r"_(?:no)?drm$",
    r"\.(?:no)?drm$",
    r"_\d+k?$",
    r"_v\d+(?:\.\d+)*$",
    r"\s*\(z-lib(?:rary)?(?:\.org)?\)$",
    r"\s*\[z-lib(?:rary)?(?:\.org)?\]$",
    r"\s*\(retail\)$",
    r"\s*\[retail\]$",
    r"\s*-\s*copy(?:\s*\(\d+\))?$",
    r"\s*\(\d+\)$",
    r"_(?:copy|dup|epub|pdf)$",
]


def normalize_cache_stem(file_path: Path) -> str:
    """Normalize a filename stem so duplicate files and EPUB/PDF pairs share the same key."""
    stem = Path(file_path).stem
    for pat in _COPY_SUFFIXES:
        stem = re.sub(pat, "", stem, flags=re.IGNORECASE).strip()
    norm = re.sub(r"[^a-z0-9]+", " ", stem.lower()).strip()
    return norm if len(norm) >= 3 else ""


def normalize_title_author_key(title: Optional[str], author: Optional[str] = None) -> str:
    """Create a normalized lookup key from title and primary author."""
    if not title:
        return ""
    norm_t = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    if len(norm_t) < 3:
        return ""
    norm_a = re.sub(r"[^a-z0-9]+", " ", (author or "").lower()).strip()
    return f"{norm_t}||{norm_a}"


def compute_file_quick_hash(path: Path) -> str:
    """Compute a fast hash combining file size and first/last 64KB."""
    try:
        size = path.stat().st_size
        h = hashlib.sha256()
        h.update(str(size).encode())
        with open(path, "rb") as f:
            head = f.read(65536)
            h.update(head)
            if size > 65536 * 2:
                f.seek(size - 65536)
                tail = f.read(65536)
                h.update(tail)
        return h.hexdigest()
    except Exception:
        return ""


class StateTracker:
    """Manages SQLite state database for resilient batch processing."""

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = Path.cwd() / ".epubmeta_state.db"
        self.db_path = Path(db_path).resolve()
        self._init_db()

    @contextlib.contextmanager
    def _get_connection(self) -> Iterator[sqlite3.Connection]:
        from .pg_scanner_db import connect_scanner_db
        conn = connect_scanner_db(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    _PG_INIT_DONE: bool = False

    def _init_db(self) -> None:
        from .pg_scanner_db import is_postgres_scanner_enabled
        if (
            self.db_path.name.lower() in ("scanner_state.db", ".epubmeta_state.db")
            and is_postgres_scanner_enabled()
        ):
            if StateTracker._PG_INIT_DONE:
                return
            StateTracker._PG_INIT_DONE = True
        with self._get_connection() as conn:
            try:
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.execute("PRAGMA synchronous=NORMAL")
            except Exception:
                pass
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
                CREATE TABLE IF NOT EXISTS processed_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT UNIQUE NOT NULL,
                    file_name TEXT,
                    file_dir TEXT,
                    file_ext TEXT,
                    orig_hash TEXT,
                    orig_sha256 TEXT,
                    orig_size INTEGER,
                    file_hash TEXT,
                    file_sha256 TEXT,
                    file_size INTEGER,
                    mtime REAL,
                    status TEXT NOT NULL,
                    isbn TEXT,
                    title TEXT,
                    author TEXT,
                    orig_title TEXT,
                    orig_author TEXT,
                    orig_isbn TEXT,
                    original_metadata_json TEXT,
                    updated_metadata_json TEXT,
                    match_confidence REAL,
                    error_message TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # Ensure backward-compatible migration if an existing database was created with an older schema
            existing_cols = {
                row["name"] for row in conn.execute("PRAGMA table_info(processed_files)").fetchall()
            }
            new_cols = [
                ("file_name", "TEXT"),
                ("file_dir", "TEXT"),
                ("file_ext", "TEXT"),
                ("orig_hash", "TEXT"),
                ("orig_sha256", "TEXT"),
                ("orig_size", "INTEGER"),
                ("file_sha256", "TEXT"),
                ("orig_title", "TEXT"),
                ("orig_author", "TEXT"),
                ("orig_isbn", "TEXT"),
                ("original_metadata_json", "TEXT"),
                ("updated_metadata_json", "TEXT"),
                ("match_confidence", "REAL"),
            ]
            for col_name, col_type in new_cols:
                if col_name not in existing_cols:
                    conn.execute(f"ALTER TABLE processed_files ADD COLUMN {col_name} {col_type}")

            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON processed_files (status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_updated_at ON processed_files (updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_hash ON processed_files (file_hash)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_orig_hash ON processed_files (orig_hash)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_orig_sha256 ON processed_files (orig_sha256)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_file_sha256 ON processed_files (file_sha256)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_file_name ON processed_files (file_name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_file_dir ON processed_files (file_dir)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_file_path TEXT,
                    source_file_name TEXT,
                    orig_hash TEXT,
                    isbn TEXT,
                    norm_stem TEXT,
                    norm_title_author TEXT,
                    original_metadata_json TEXT,
                    metadata_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            existing_cache_cols = {
                row["name"] for row in conn.execute("PRAGMA table_info(metadata_cache)").fetchall()
            }
            for col_name, col_type in [
                ("source_file_path", "TEXT"),
                ("source_file_name", "TEXT"),
                ("original_metadata_json", "TEXT"),
            ]:
                if col_name not in existing_cache_cols:
                    conn.execute(f"ALTER TABLE metadata_cache ADD COLUMN {col_name} {col_type}")

            conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_hash ON metadata_cache (orig_hash)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_isbn ON metadata_cache (isbn)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_stem ON metadata_cache (norm_stem)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_ta ON metadata_cache (norm_title_author)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ai_usage_daily (
                    usage_date TEXT PRIMARY KEY,
                    requests_count INTEGER DEFAULT 0,
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    estimated_cost_usd REAL DEFAULT 0.0,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def get_file_record(self, file_path: Path) -> Optional[Dict[str, Any]]:
        norm_path = str(Path(file_path).resolve())
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT * FROM processed_files WHERE file_path = ?",
                (norm_path,),
            )
            row = cur.fetchone()
            if row:
                return dict(row)
        return None

    def get_file_record_by_hash(self, file_path: Path) -> Optional[Dict[str, Any]]:
        """Look up a processed record by quick content hash and size (handles NAS vs SMB mount path changes)."""
        try:
            with self._get_connection() as conn:
                # Fast pre-check: if processed_files has no SUCCESS rows at all, return immediately without stat/hash I/O
                has_any = conn.execute(
                    "SELECT 1 FROM processed_files WHERE status = 'SUCCESS' LIMIT 1"
                ).fetchone()
                if not has_any:
                    return None
                size = file_path.stat().st_size
                has_size = conn.execute(
                    "SELECT 1 FROM processed_files WHERE file_size = ? AND status = 'SUCCESS' LIMIT 1",
                    (size,),
                ).fetchone()
                if not has_size:
                    return None
                f_hash = compute_file_quick_hash(file_path)
                if not f_hash:
                    return None
                cur = conn.execute(
                    "SELECT * FROM processed_files WHERE file_hash = ? AND file_size = ? AND status = 'SUCCESS' LIMIT 1",
                    (f_hash, size),
                )
                row = cur.fetchone()
                if row:
                    return dict(row)
        except Exception:
            pass
        return None

    def should_process(self, file_path: Path, force: bool = False, retry_failed: bool = False) -> bool:
        """Determine if a file needs to be processed."""
        if force:
            return True

        record = self.get_file_record(file_path)
        if record:
            status = record.get("status")
            if status == "SUCCESS":
                try:
                    stat = file_path.stat()
                    if stat.st_size != record.get("file_size"):
                        cur_hash = compute_file_quick_hash(file_path)
                        if cur_hash and cur_hash != record.get("file_hash"):
                            return True
                except Exception:
                    pass
                return False
            if status in ("QUARANTINED", "HAS_DRM", "IGNORED", "NO_ISBN"):
                return False
            if status in ("FAILED", "NOT_FOUND", "NEEDS_REVIEW"):
                return retry_failed
            return True

        record_by_hash = self.get_file_record_by_hash(file_path)
        if record_by_hash:
            return False
        return True

    def load_processed_status_map(self) -> Dict[str, str]:
        """Load all `{file_path: status}` entries from `processed_files` in a single query for fast batch filtering."""
        from .external_dbs import extract_relative_book_subpath

        status_map: Dict[str, str] = {}
        with self._get_connection() as conn:
            rows = conn.execute("SELECT file_path, status FROM processed_files").fetchall()
            for row in rows:
                fp = row["file_path"]
                st = row["status"]
                status_map[fp] = st
                status_map[fp.replace("\\", "/").lower()] = st
                rel = extract_relative_book_subpath(fp)
                if rel:
                    status_map[f"rel:{rel.lower()}"] = st
        return status_map

    @staticmethod
    def lookup_status_in_map(file_path: Path | str, status_map: Dict[str, str]) -> Optional[str]:
        """Look up a file's status in `status_map` across Windows SMB, NAS host, and Docker container paths."""
        if not status_map:
            return None
        from .external_dbs import extract_relative_book_subpath

        raw = str(file_path)
        if raw in status_map:
            return status_map[raw]
        norm = raw.replace("\\", "/").lower()
        if norm in status_map:
            return status_map[norm]
        rel = extract_relative_book_subpath(raw)
        if rel:
            return status_map.get(f"rel:{rel.lower()}")
        return None

    def record_success(
        self,
        file_path: Path,
        isbn: Optional[str] = None,
        title: Optional[str] = None,
        author: Optional[str] = None,
        orig_hash: Optional[str] = None,
        orig_sha256: Optional[str] = None,
        orig_size: Optional[int] = None,
        file_sha256: Optional[str] = None,
        original_metadata: Optional[BookMetadata] = None,
        updated_metadata: Optional[BookMetadata] = None,
        match_confidence: Optional[float] = None,
    ) -> None:
        resolved = Path(file_path).resolve()
        norm_path = str(resolved)
        file_name = resolved.name
        file_dir = str(resolved.parent)
        file_ext = resolved.suffix.lower()
        stat = resolved.stat()
        file_hash = compute_file_quick_hash(resolved)
        now = datetime.now(timezone.utc).isoformat()

        orig_title = original_metadata.title if original_metadata else None
        orig_author = (", ".join(original_metadata.authors) if (original_metadata and original_metadata.authors) else None)
        orig_isbn = (original_metadata.isbn13 or original_metadata.isbn10) if original_metadata else None
        orig_meta_json = json.dumps(original_metadata.to_dict(), ensure_ascii=False) if original_metadata else None
        upd_meta_json = json.dumps(updated_metadata.to_dict(), ensure_ascii=False) if updated_metadata else None

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO processed_files
                    (file_path, file_name, file_dir, file_ext, orig_hash, orig_sha256, orig_size,
                     file_hash, file_sha256, file_size, mtime, status, isbn, title, author,
                     orig_title, orig_author, orig_isbn, original_metadata_json,
                     updated_metadata_json, match_confidence, error_message, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SUCCESS', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(file_path) DO UPDATE SET
                    file_name=excluded.file_name,
                    file_dir=excluded.file_dir,
                    file_ext=excluded.file_ext,
                    orig_hash=COALESCE(excluded.orig_hash, processed_files.orig_hash),
                    orig_sha256=COALESCE(excluded.orig_sha256, processed_files.orig_sha256),
                    orig_size=COALESCE(excluded.orig_size, processed_files.orig_size),
                    file_hash=excluded.file_hash,
                    file_sha256=COALESCE(excluded.file_sha256, processed_files.file_sha256),
                    file_size=excluded.file_size,
                    mtime=excluded.mtime,
                    status='SUCCESS',
                    isbn=excluded.isbn,
                    title=excluded.title,
                    author=excluded.author,
                    orig_title=COALESCE(excluded.orig_title, processed_files.orig_title),
                    orig_author=COALESCE(excluded.orig_author, processed_files.orig_author),
                    orig_isbn=COALESCE(excluded.orig_isbn, processed_files.orig_isbn),
                    original_metadata_json=COALESCE(excluded.original_metadata_json, processed_files.original_metadata_json),
                    updated_metadata_json=excluded.updated_metadata_json,
                    match_confidence=COALESCE(excluded.match_confidence, processed_files.match_confidence),
                    error_message=NULL,
                    updated_at=excluded.updated_at
                """,
                (
                    norm_path,
                    file_name,
                    file_dir,
                    file_ext,
                    orig_hash or file_hash,
                    orig_sha256,
                    orig_size if orig_size is not None else stat.st_size,
                    file_hash,
                    file_sha256,
                    stat.st_size,
                    stat.st_mtime,
                    isbn,
                    title,
                    author,
                    orig_title,
                    orig_author,
                    orig_isbn,
                    orig_meta_json,
                    upd_meta_json,
                    match_confidence,
                    now,
                ),
            )

    def record_failure(
        self,
        file_path: Path,
        error_message: str,
        status: str = "FAILED",
        original_metadata: Optional[BookMetadata] = None,
        candidate_metadata: Optional[BookMetadata] = None,
        match_confidence: Optional[float] = None,
    ) -> None:
        resolved = Path(file_path).resolve()
        norm_path = str(resolved)
        file_name = resolved.name
        file_dir = str(resolved.parent)
        file_ext = resolved.suffix.lower()
        try:
            stat = resolved.stat()
            size = stat.st_size
            mtime = stat.st_mtime
            f_hash = compute_file_quick_hash(resolved)
        except Exception:
            size = 0
            mtime = 0
            f_hash = ""
        now = datetime.now(timezone.utc).isoformat()

        orig_title = original_metadata.title if original_metadata else None
        orig_author = (", ".join(original_metadata.authors) if (original_metadata and original_metadata.authors) else None)
        orig_isbn = (original_metadata.isbn13 or original_metadata.isbn10) if original_metadata else None
        orig_meta_json = json.dumps(original_metadata.to_dict(), ensure_ascii=False) if original_metadata else None
        cand_meta_json = json.dumps(candidate_metadata.to_dict(), ensure_ascii=False) if candidate_metadata else None
        cand_title = candidate_metadata.title if candidate_metadata else None
        cand_author = (", ".join(candidate_metadata.authors) if (candidate_metadata and candidate_metadata.authors) else None)
        cand_isbn = (candidate_metadata.isbn13 or candidate_metadata.isbn10) if candidate_metadata else None

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO processed_files
                    (file_path, file_name, file_dir, file_ext, orig_hash, orig_size,
                     file_hash, file_size, mtime, status, isbn, title, author,
                     orig_title, orig_author, orig_isbn, original_metadata_json,
                     updated_metadata_json, match_confidence, error_message, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_path) DO UPDATE SET
                    file_name=excluded.file_name,
                    file_dir=excluded.file_dir,
                    file_ext=excluded.file_ext,
                    orig_hash=COALESCE(processed_files.orig_hash, excluded.orig_hash),
                    orig_size=COALESCE(processed_files.orig_size, excluded.orig_size),
                    status=excluded.status,
                    isbn=COALESCE(excluded.isbn, processed_files.isbn),
                    title=COALESCE(excluded.title, processed_files.title),
                    author=COALESCE(excluded.author, processed_files.author),
                    orig_title=COALESCE(excluded.orig_title, processed_files.orig_title),
                    orig_author=COALESCE(excluded.orig_author, processed_files.orig_author),
                    orig_isbn=COALESCE(excluded.orig_isbn, processed_files.orig_isbn),
                    original_metadata_json=COALESCE(excluded.original_metadata_json, processed_files.original_metadata_json),
                    updated_metadata_json=COALESCE(excluded.updated_metadata_json, processed_files.updated_metadata_json),
                    match_confidence=COALESCE(excluded.match_confidence, processed_files.match_confidence),
                    error_message=excluded.error_message,
                    updated_at=excluded.updated_at
                """,
                (
                    norm_path,
                    file_name,
                    file_dir,
                    file_ext,
                    f_hash,
                    size,
                    f_hash,
                    size,
                    mtime,
                    status,
                    cand_isbn,
                    cand_title,
                    cand_author,
                    orig_title,
                    orig_author,
                    orig_isbn,
                    orig_meta_json,
                    cand_meta_json,
                    match_confidence,
                    error_message,
                    now,
                ),
            )

    def get_summary(self) -> Dict[str, int]:
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM processed_files GROUP BY status"
            )
            rows = cur.fetchall()
            counts = {row["status"]: row["cnt"] for row in rows}
            counts["TOTAL"] = sum(counts.values())
            return counts

    def save_cached_metadata(
        self,
        file_path: Path,
        metadata: BookMetadata,
        orig_hash: Optional[str] = None,
        original_metadata: Optional[BookMetadata] = None,
    ) -> None:
        """Cache resolved BookMetadata for reuse by duplicates or companion PDF/EPUB files."""
        if not metadata or metadata.is_empty():
            return
        resolved = Path(file_path).resolve()
        isbn = metadata.isbn13 or metadata.isbn10
        norm_stem = normalize_cache_stem(resolved)
        primary_author = metadata.authors[0] if metadata.authors else None
        norm_ta = normalize_title_author_key(metadata.title, primary_author)
        orig_payload = json.dumps(original_metadata.to_dict(), ensure_ascii=False) if original_metadata else None
        payload = json.dumps(metadata.to_dict(), ensure_ascii=False)
        now = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO metadata_cache
                    (source_file_path, source_file_name, orig_hash, isbn, norm_stem, norm_title_author, original_metadata_json, metadata_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (str(resolved), resolved.name, orig_hash or None, isbn, norm_stem or None, norm_ta or None, orig_payload, payload, now),
            )

    def get_cached_metadata(
        self,
        orig_hash: Optional[str] = None,
        isbn: Optional[str] = None,
        norm_stem: Optional[str] = None,
        title: Optional[str] = None,
        author: Optional[str] = None,
    ) -> Optional[BookMetadata]:
        """Look up cached metadata by original file hash, ISBN, normalized filename stem, or title/author."""
        queries = []
        if orig_hash:
            queries.append(("orig_hash = ?", (orig_hash,)))
        if isbn:
            queries.append(("isbn = ?", (isbn,)))
        if norm_stem and len(norm_stem) >= 3:
            queries.append(("norm_stem = ?", (norm_stem,)))
        if title:
            norm_ta = normalize_title_author_key(title, author)
            if norm_ta:
                queries.append(("norm_title_author = ?", (norm_ta,)))

        if not queries:
            return None

        with self._get_connection() as conn:
            for clause, params in queries:
                cur = conn.execute(
                    f"SELECT metadata_json FROM metadata_cache WHERE {clause} ORDER BY id DESC LIMIT 5",
                    params,
                )
                for row in cur.fetchall():
                    try:
                        data = json.loads(row["metadata_json"])
                        meta = BookMetadata.from_dict(data)
                        orig_src = (meta.source or "cached").strip()
                        if "mylibrary-db" in orig_src.lower():
                            continue
                        if meta.title and re.match(r"^untitled\b", meta.title.strip(), re.IGNORECASE):
                            continue
                        if meta.published_date and re.match(r"^0[01]\d\d\b", str(meta.published_date).strip()):
                            meta.published_date = None
                        if not orig_src.startswith("local-cache"):
                            meta.source = f"local-cache ({orig_src})"
                        meta.ai_used = False
                        return meta
                    except Exception as e:
                        logger.debug("Failed deserializing cached metadata: %s", e)
        return None

    def clear(self) -> None:
        with self._get_connection() as conn:
            conn.execute("DELETE FROM processed_files")
            conn.execute("DELETE FROM metadata_cache")

    def migrate_legacy_epubmeta_db(self, legacy_db_path: Optional[Path]) -> tuple[int, int]:
        """
        One-time migration of `processed_files` and `metadata_cache` from a standalone
        `.epubmeta_state.db` into the unified `scanner_state.db`.
        Renames the legacy file to `<legacy_db_path>.migrated` after successful import.
        Returns `(migrated_processed_count, migrated_cache_count)`.
        """
        if not legacy_db_path:
            return 0, 0
        legacy_path = Path(legacy_db_path).resolve()
        if not legacy_path.exists() or legacy_path == self.db_path:
            return 0, 0

        migrated_proc = 0
        migrated_cache = 0
        try:
            with self._get_connection() as conn:
                conn.execute("ATTACH DATABASE ? AS legacy_em", (str(legacy_path),))
                try:
                    proc_cur = conn.execute(
                        """
                        INSERT OR IGNORE INTO processed_files (
                            file_path, file_name, file_dir, file_ext, orig_hash, orig_sha256, orig_size,
                            file_hash, file_sha256, file_size, mtime, status, isbn, title, author,
                            orig_title, orig_author, orig_isbn, original_metadata_json,
                            updated_metadata_json, match_confidence, error_message, updated_at
                        )
                        SELECT
                            file_path, file_name, file_dir, file_ext, orig_hash, orig_sha256, orig_size,
                            file_hash, file_sha256, file_size, mtime, status, isbn, title, author,
                            orig_title, orig_author, orig_isbn, original_metadata_json,
                            updated_metadata_json, match_confidence, error_message, updated_at
                        FROM legacy_em.processed_files
                        """
                    )
                    migrated_proc = max(proc_cur.rowcount, 0)

                    existing_cache_cnt = conn.execute("SELECT COUNT(*) FROM metadata_cache").fetchone()[0]
                    if existing_cache_cnt == 0:
                        cache_cur = conn.execute(
                            """
                            INSERT INTO metadata_cache (
                                source_file_path, source_file_name, orig_hash, isbn,
                                norm_stem, norm_title_author, original_metadata_json, metadata_json, updated_at
                            )
                            SELECT
                                source_file_path, source_file_name, orig_hash, isbn,
                                norm_stem, norm_title_author, original_metadata_json, metadata_json, updated_at
                            FROM legacy_em.metadata_cache
                            """
                        )
                    else:
                        cache_cur = conn.execute(
                            """
                            INSERT INTO metadata_cache (
                                source_file_path, source_file_name, orig_hash, isbn,
                                norm_stem, norm_title_author, original_metadata_json, metadata_json, updated_at
                            )
                            SELECT
                                l.source_file_path, l.source_file_name, l.orig_hash, l.isbn,
                                l.norm_stem, l.norm_title_author, l.original_metadata_json, l.metadata_json, l.updated_at
                            FROM legacy_em.metadata_cache l
                            WHERE l.isbn IS NULL
                               OR NOT EXISTS (SELECT 1 FROM metadata_cache m WHERE m.isbn = l.isbn)
                            """
                        )
                    migrated_cache = max(cache_cur.rowcount, 0)
                finally:
                    conn.commit()
                    conn.execute("DETACH DATABASE legacy_em")

            # Rename legacy DB so migration only runs once
            migrated_marker = legacy_path.with_name(legacy_path.name + ".migrated")
            try:
                if migrated_marker.exists():
                    migrated_marker.unlink()
                legacy_path.rename(migrated_marker)
                for wal_ext in ("-wal", "-shm"):
                    sidecar = legacy_path.with_name(legacy_path.name + wal_ext)
                    if sidecar.exists():
                        sidecar.unlink()
            except Exception as rename_err:
                logger.debug("Could not rename migrated legacy DB %s: %s", legacy_path, rename_err)

            logger.info(
                "Migrated legacy epubmeta DB (%s -> %s): %d processed_files, %d metadata_cache rows",
                legacy_path,
                self.db_path,
                migrated_proc,
                migrated_cache,
            )
        except Exception as e:
            logger.warning("Failed migrating legacy epubmeta DB %s: %s", legacy_path, e)
        return migrated_proc, migrated_cache

    def get_daily_ai_usage(self, usage_date: Optional[str] = None) -> Dict[str, Any]:
        """Return today's cumulative Gemini AI usage and estimated USD cost from SQLite."""
        day_str = usage_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT * FROM ai_usage_daily WHERE usage_date = ?",
                    (day_str,),
                ).fetchone()
                if row:
                    return dict(row)
        except Exception as e:
            logger.debug("Error reading ai_usage_daily: %s", e)
        return {
            "usage_date": day_str,
            "requests_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost_usd": 0.0,
        }

    def record_ai_usage(
        self,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        usage_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Atomically record a Gemini AI request's token usage and estimated USD cost for today."""
        day_str = usage_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO ai_usage_daily (
                        usage_date, requests_count, input_tokens, output_tokens, estimated_cost_usd, updated_at
                    ) VALUES (?, 1, ?, ?, ?, ?)
                    ON CONFLICT(usage_date) DO UPDATE SET
                        requests_count = ai_usage_daily.requests_count + 1,
                        input_tokens = ai_usage_daily.input_tokens + excluded.input_tokens,
                        output_tokens = ai_usage_daily.output_tokens + excluded.output_tokens,
                        estimated_cost_usd = ROUND(ai_usage_daily.estimated_cost_usd + excluded.estimated_cost_usd, 6),
                        updated_at = excluded.updated_at
                    """,
                    (day_str, int(input_tokens), int(output_tokens), float(cost_usd), now_iso),
                )
        except Exception as e:
            logger.warning("Failed to record AI usage in SQLite: %s", e)
        return self.get_daily_ai_usage(day_str)


