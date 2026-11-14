"""
PostgreSQL adapter for MyLibrary Scanner & epubmeta State Database (`BookDB`).
Provides a drop-in `sqlite3.Connection`-compatible wrapper backed by DevStack PostgreSQL
(`DATABASE_URL=postgres://admin:...@postgres:5432/BookDB`), eliminating SQLite file locking,
WAL sidecar splits, and bind-mount inode corruption across containers.

Falls back transparently to `sqlite3` only when PostgreSQL is not configured/reachable
(e.g. offline local unit tests).
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import threading
from typing import Any, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

_PG_AVAILABILITY_CACHE: dict[str, Any] = {
    "checked": False,
    "available": False,
    "dsn": None,
    "schema_initialized": False,
}

_THREAD_LOCAL = threading.local()


class PgRow(Mapping):
    """
    Hybrid row object supporting both positional tuple indexing/unpacking (`row[0]`, `a, b = row`)
    and case-insensitive column-name dictionary access (`row['file_path']`, `dict(row)`, `row.keys()`),
    matching `sqlite3.Row` and raw `sqlite3` tuple behavior simultaneously without per-row dict overhead.
    """

    __slots__ = ("_vals", "_cols")

    def __init__(self, cols: Sequence[str], vals: Sequence[Any]):
        self._cols = cols if isinstance(cols, tuple) else tuple(cols)
        self._vals = vals if isinstance(vals, tuple) else tuple(vals)

    def __getitem__(self, key: int | slice | str) -> Any:
        if isinstance(key, (int, slice)):
            return self._vals[key]
        k_low = key.lower()
        for idx, c in enumerate(self._cols):
            if c == key or c.lower() == k_low:
                return self._vals[idx]
        raise KeyError(key)

    def __iter__(self):
        return iter(self._vals)

    def __len__(self) -> int:
        return len(self._vals)

    def keys(self) -> List[str]:
        return list(self._cols)

    def items(self):
        return list(zip(self._cols, self._vals))

    def values(self):
        return list(self._vals)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def __repr__(self) -> str:
        return f"PgRow({dict(zip(self._cols, self._vals))!r})"


def _discover_postgres_dsn() -> Optional[str]:
    """Resolve PostgreSQL connection URL from environment, .env, or config.json."""
    for env_key in ("SCANNER_DATABASE_URL", "DATABASE_URL"):
        val = os.environ.get(env_key, "").strip()
        if val.startswith(("postgres://", "postgresql://")):
            return val

    # Check .env files in common locations
    candidates = [
        Path("/app/.env"),
        Path.cwd() / ".env",
        Path.cwd().parent / ".env",
        Path(__file__).resolve().parent.parent.parent / ".env",
    ]
    for env_file in candidates:
        try:
            if env_file.exists():
                for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if line.startswith("DATABASE_URL="):
                        val = line.split("=", 1)[1].strip().strip("'\"")
                        if val.startswith(("postgres://", "postgresql://")):
                            return val
        except Exception:
            continue

    # Check config.json in common locations
    cfg_candidates = [
        Path("/config/config.json"),
        Path("/app/nas_scanner/config.json"),
        Path.cwd() / "config.json",
        Path(__file__).resolve().parent.parent / "config.json",
    ]
    for cfg_file in cfg_candidates:
        try:
            if cfg_file.exists():
                import json as _json
                cfg_data = _json.loads(cfg_file.read_text(encoding="utf-8", errors="replace")) or {}
                val = str(cfg_data.get("database_url") or "").strip()
                if val.startswith(("postgres://", "postgresql://")):
                    return val
        except Exception:
            continue

    return None


def _ensure_psycopg():
    """Import psycopg (v3) or psycopg2, auto-installing psycopg[binary] in scanner container if needed."""
    try:
        import psycopg
        return psycopg, "v3"
    except ImportError:
        pass
    try:
        import psycopg2
        return psycopg2, "v2"
    except ImportError:
        pass

    # If inside a Linux container wherepostgres is reachable, install psycopg[binary] once
    if os.name != "nt":
        try:
            logger.info("Installing psycopg[binary] for PostgreSQL scanner state backend...")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-q", "psycopg[binary]>=3.1"],
                check=True,
                timeout=90,
            )
            import psycopg
            return psycopg, "v3"
        except Exception as e:
            logger.warning("Could not auto-install psycopg[binary]: %s", e)
    return None, None


def _replace_qmarks_outside_quotes(sql: str) -> str:
    """Replace SQLite `?` positional placeholders with `%s` while preserving quoted string literals."""
    out: list[str] = []
    in_single = False
    in_double = False
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'" and not in_double:
            out.append(ch)
            if in_single and i + 1 < n and sql[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            in_single = not in_single
        elif ch == '"' and not in_single:
            out.append(ch)
            in_double = not in_double
        elif ch == "?" and not in_single and not in_double:
            out.append("%s")
        elif ch == "%" and not in_single and not in_double:
            # Escape literal % when the query uses ? placeholders
            if "?" in sql:
                out.append("%%")
            else:
                out.append("%")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def translate_sqlite_to_pg(sql: str) -> Optional[str]:
    """Translate SQLite SQL statements into PostgreSQL-compatible SQL."""
    s = sql.strip()
    if not s:
        return None

    upper = s.upper()

    # Handle PRAGMA statements
    if upper.startswith("PRAGMA"):
        m_ti = re.match(r"PRAGMA\s+table_info\(\s*([A-Za-z0-9_]+)\s*\)", s, re.IGNORECASE)
        if m_ti:
            tbl = m_ti.group(1).lower()
            return (
                "SELECT ordinal_position - 1 AS cid, column_name AS name, data_type AS type, "
                "0 AS notnull, column_default AS dflt_value, 0 AS pk "
                f"FROM information_schema.columns WHERE table_schema = 'public' AND table_name = '{tbl}' "
                "ORDER BY ordinal_position"
            )
        return None

    if upper.startswith(("ATTACH DATABASE", "DETACH DATABASE", "VACUUM")):
        return None

    # sqlite_master table check
    if "SQLITE_MASTER" in upper:
        return "SELECT table_name AS name FROM information_schema.tables WHERE table_schema = 'public'"

    # Auto-increment primary key syntax
    s = re.sub(
        r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
        "BIGSERIAL PRIMARY KEY",
        s,
        flags=re.IGNORECASE,
    )

    # ROUND(a + b, 6) in Postgres requires numeric cast
    s = re.sub(
        r"ROUND\(\s*ai_usage_daily\.estimated_cost_usd\s*\+\s*excluded\.estimated_cost_usd\s*,\s*6\s*\)",
        "ROUND((ai_usage_daily.estimated_cost_usd + excluded.estimated_cost_usd)::numeric, 6)::double precision",
        s,
        flags=re.IGNORECASE,
    )

    # INSERT OR REPLACE INTO file_states
    if re.search(r"INSERT\s+OR\s+REPLACE\s+INTO\s+file_states", s, re.IGNORECASE):
        s = re.sub(r"INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s, flags=re.IGNORECASE)
        if "ON CONFLICT" not in s.upper():
            s = s.rstrip("; \t\r\n") + (
                " ON CONFLICT (filepath) DO UPDATE SET "
                "modified_at = EXCLUDED.modified_at, "
                "size = EXCLUDED.size, "
                "hash = EXCLUDED.hash, "
                "synced_at = EXCLUDED.synced_at"
            )

    # INSERT OR REPLACE INTO processed_files
    if re.search(r"INSERT\s+OR\s+REPLACE\s+INTO\s+processed_files", s, re.IGNORECASE):
        s = re.sub(r"INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s, flags=re.IGNORECASE)
        if "ON CONFLICT" not in s.upper():
            s = s.rstrip("; \t\r\n") + (
                " ON CONFLICT (file_path) DO UPDATE SET "
                "file_name = EXCLUDED.file_name, "
                "file_ext = EXCLUDED.file_ext, "
                "status = EXCLUDED.status, "
                "title = EXCLUDED.title, "
                "author = EXCLUDED.author, "
                "isbn = EXCLUDED.isbn, "
                "updated_metadata_json = EXCLUDED.updated_metadata_json, "
                "updated_at = EXCLUDED.updated_at"
            )

    # INSERT OR IGNORE INTO ...
    if re.search(r"INSERT\s+OR\s+IGNORE\s+INTO", s, re.IGNORECASE):
        s = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", s, flags=re.IGNORECASE)
        if "ON CONFLICT" not in s.upper():
            s = s.rstrip("; \t\r\n") + " ON CONFLICT DO NOTHING"

    # SQLite LIKE is case-insensitive; use ILIKE in PostgreSQL
    s = re.sub(r"\bLIKE\b", "ILIKE", s, flags=re.IGNORECASE)

    # Fix column name typo in DELETE FROM file_states WHERE file_path = ?
    s = re.sub(
        r"DELETE\s+FROM\s+file_states\s+WHERE\s+file_path\s*=",
        "DELETE FROM file_states WHERE filepath =",
        s,
        flags=re.IGNORECASE,
    )

    s = _replace_qmarks_outside_quotes(s)
    return s


def _clean_pg_param(val: Any) -> Any:
    """Strip NUL (0x00) bytes from strings, as PostgreSQL TEXT columns reject NUL bytes."""
    if isinstance(val, str) and "\x00" in val:
        return val.replace("\x00", "")
    return val


class PgCursorWrapper:
    """sqlite3.Cursor-compatible wrapper around a psycopg cursor."""

    def __init__(self, raw_cur: Any):
        self._cur = raw_cur
        self.rowcount: int = -1

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> "PgCursorWrapper":
        pg_sql = translate_sqlite_to_pg(sql)
        if not pg_sql:
            self.rowcount = 0
            return self
        if params is not None:
            self._cur.execute(pg_sql, tuple(_clean_pg_param(x) for x in params))
        else:
            self._cur.execute(pg_sql)
        self.rowcount = getattr(self._cur, "rowcount", -1)
        return self

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]) -> "PgCursorWrapper":
        pg_sql = translate_sqlite_to_pg(sql)
        if not pg_sql:
            self.rowcount = 0
            return self
        params_list = [tuple(_clean_pg_param(x) for x in p) for p in seq_of_params]
        if not params_list:
            self.rowcount = 0
            return self
        self._cur.executemany(pg_sql, params_list)
        self.rowcount = getattr(self._cur, "rowcount", len(params_list))
        return self

    def _wrap_row(self, row: Any) -> Optional[PgRow]:
        if row is None:
            return None
        desc = getattr(self._cur, "description", None)
        if not desc:
            return PgRow([str(i) for i in range(len(row))], row)
        cols = [d.name if hasattr(d, "name") else d[0] for d in desc]
        return PgRow(cols, row)

    def fetchone(self) -> Optional[PgRow]:
        if getattr(self._cur, "description", None) is None:
            return None
        row = self._cur.fetchone()
        return self._wrap_row(row)

    def fetchmany(self, size: int = 100) -> List[PgRow]:
        if getattr(self._cur, "description", None) is None:
            return []
        rows = self._cur.fetchmany(size)
        if not rows:
            return []
        desc = self._cur.description
        cols = tuple(d.name if hasattr(d, "name") else d[0] for d in desc)
        return [PgRow(cols, r) for r in rows]

    def fetchall(self) -> List[PgRow]:
        if getattr(self._cur, "description", None) is None:
            return []
        rows = self._cur.fetchall()
        if not rows:
            return []
        desc = self._cur.description
        cols = tuple(d.name if hasattr(d, "name") else d[0] for d in desc)
        return [PgRow(cols, r) for r in rows]

    def close(self) -> None:
        try:
            self._cur.close()
        except Exception:
            pass

    def __iter__(self):
        return iter(self.fetchall())


class PgConnectionWrapper:
    """sqlite3.Connection-compatible wrapper around a psycopg connection."""

    def __init__(self, raw_conn: Any, persistent: bool = False):
        self._conn = raw_conn
        self._persistent = persistent
        self.row_factory: Any = None
        self.text_factory: Any = None

    def _ensure_alive(self) -> Any:
        if getattr(self._conn, "closed", False) or getattr(self._conn, "broken", False):
            drv, drv_ver = _ensure_psycopg()
            dsn = _PG_AVAILABILITY_CACHE.get("dsn") or _discover_postgres_dsn()
            if drv_ver == "v3":
                self._conn = drv.connect(dsn, connect_timeout=10, autocommit=True)
            else:
                self._conn = drv.connect(dsn, connect_timeout=10)
                self._conn.autocommit = True
            if self._persistent:
                _THREAD_LOCAL.pg_conn = self._conn
        return self._conn

    def cursor(self) -> PgCursorWrapper:
        conn = self._ensure_alive()
        return PgCursorWrapper(conn.cursor())

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> PgCursorWrapper:
        try:
            cur = self.cursor()
            cur.execute(sql, params)
            return cur
        except Exception as exc:
            # Reconnect once if connection was closed by server after idle period
            if getattr(self._conn, "closed", False) or getattr(self._conn, "broken", False) or "closed" in str(exc).lower():
                try:
                    self._conn.close()
                except Exception:
                    pass
                cur = self.cursor()
                cur.execute(sql, params)
                return cur
            raise

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]) -> PgCursorWrapper:
        cur = self.cursor()
        cur.executemany(sql, seq_of_params)
        return cur

    def executescript(self, sql_script: str) -> None:
        cur = self.cursor()
        for stmt in sql_script.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)

    def commit(self) -> None:
        try:
            if not getattr(self._conn, "autocommit", False):
                self._conn.commit()
        except Exception:
            pass

    def rollback(self) -> None:
        try:
            if not getattr(self._conn, "autocommit", False):
                self._conn.rollback()
        except Exception:
            pass

    def close(self) -> None:
        if self._persistent:
            # Keep thread-local persistent connection open for instant reuse
            return
        try:
            self._conn.close()
        except Exception:
            pass

    def __enter__(self) -> "PgConnectionWrapper":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS file_states (
    filepath TEXT PRIMARY KEY,
    modified_at TEXT,
    size BIGINT,
    hash TEXT,
    synced_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_file_states_synced_at ON file_states (synced_at);

CREATE TABLE IF NOT EXISTS processed_files (
    id BIGSERIAL PRIMARY KEY,
    file_path TEXT UNIQUE NOT NULL,
    file_name TEXT,
    file_dir TEXT,
    file_ext TEXT,
    orig_hash TEXT,
    orig_sha256 TEXT,
    orig_size BIGINT,
    file_hash TEXT,
    file_sha256 TEXT,
    file_size BIGINT,
    mtime DOUBLE PRECISION,
    status TEXT NOT NULL,
    isbn TEXT,
    title TEXT,
    author TEXT,
    orig_title TEXT,
    orig_author TEXT,
    orig_isbn TEXT,
    original_metadata_json TEXT,
    updated_metadata_json TEXT,
    match_confidence DOUBLE PRECISION,
    error_message TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_status ON processed_files (status);
CREATE INDEX IF NOT EXISTS idx_updated_at ON processed_files (updated_at);
CREATE INDEX IF NOT EXISTS idx_hash ON processed_files (file_hash);
CREATE INDEX IF NOT EXISTS idx_orig_hash ON processed_files (orig_hash);
CREATE INDEX IF NOT EXISTS idx_orig_sha256 ON processed_files (orig_sha256);
CREATE INDEX IF NOT EXISTS idx_file_sha256 ON processed_files (file_sha256);
CREATE INDEX IF NOT EXISTS idx_file_name ON processed_files (file_name);
CREATE INDEX IF NOT EXISTS idx_file_dir ON processed_files (file_dir);

CREATE TABLE IF NOT EXISTS metadata_cache (
    id BIGSERIAL PRIMARY KEY,
    source_file_path TEXT,
    source_file_name TEXT,
    orig_hash TEXT,
    isbn TEXT,
    norm_stem TEXT,
    norm_title_author TEXT,
    original_metadata_json TEXT,
    metadata_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_hash ON metadata_cache (orig_hash);
CREATE INDEX IF NOT EXISTS idx_cache_isbn ON metadata_cache (isbn);
CREATE INDEX IF NOT EXISTS idx_cache_stem ON metadata_cache (norm_stem);
CREATE INDEX IF NOT EXISTS idx_cache_ta ON metadata_cache (norm_title_author);
CREATE INDEX IF NOT EXISTS idx_cache_updated_at ON metadata_cache (updated_at);

CREATE TABLE IF NOT EXISTS ai_usage_daily (
    usage_date TEXT PRIMARY KEY,
    requests_count BIGINT DEFAULT 0,
    input_tokens BIGINT DEFAULT 0,
    output_tokens BIGINT DEFAULT 0,
    estimated_cost_usd DOUBLE PRECISION DEFAULT 0.0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gemini_batch_jobs (
    id BIGSERIAL PRIMARY KEY,
    job_name TEXT UNIQUE NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    items_count BIGINT DEFAULT 0,
    estimated_input_tokens BIGINT DEFAULT 0,
    estimated_cost_usd DOUBLE PRECISION DEFAULT 0.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_batch_jobs_status ON gemini_batch_jobs (status);

CREATE TABLE IF NOT EXISTS gemini_batch_items (
    id BIGSERIAL PRIMARY KEY,
    file_path TEXT UNIQUE NOT NULL,
    job_name TEXT,
    status TEXT NOT NULL,
    prompt_text TEXT,
    fallback_title TEXT,
    fallback_author TEXT,
    original_metadata_json TEXT,
    ai_result_json TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_batch_items_status ON gemini_batch_items (status);
CREATE INDEX IF NOT EXISTS idx_batch_items_job ON gemini_batch_items (job_name);
"""


def is_postgres_scanner_enabled() -> bool:
    """Return True if the DevStack PostgreSQL database is reachable and configured."""
    if _PG_AVAILABILITY_CACHE["checked"]:
        return bool(_PG_AVAILABILITY_CACHE["available"])

    dsn = _discover_postgres_dsn()
    if not dsn:
        _PG_AVAILABILITY_CACHE["checked"] = True
        _PG_AVAILABILITY_CACHE["available"] = False
        return False

    drv, _ = _ensure_psycopg()
    if drv is None:
        _PG_AVAILABILITY_CACHE["checked"] = True
        _PG_AVAILABILITY_CACHE["available"] = False
        return False

    try:
        raw_conn = drv.connect(dsn, connect_timeout=3)
        wrapper = PgConnectionWrapper(raw_conn)
        wrapper.executescript(SCHEMA_SQL)
        wrapper.commit()
        wrapper.close()
        _PG_AVAILABILITY_CACHE["checked"] = True
        _PG_AVAILABILITY_CACHE["available"] = True
        _PG_AVAILABILITY_CACHE["dsn"] = dsn
        _PG_AVAILABILITY_CACHE["schema_initialized"] = True
        return True
    except Exception as e:
        logger.debug("PostgreSQL scanner state DB not reachable (%s); falling back to SQLite", e)
        _PG_AVAILABILITY_CACHE["checked"] = True
        _PG_AVAILABILITY_CACHE["available"] = False
        return False


def connect_scanner_db(db_path: str | Path | None = None, timeout: float = 30.0) -> Any:
    """
    Connect to the DevStack PostgreSQL scanner state database (`BookDB`), or fall back
    to `sqlite3.connect(db_path)` if PostgreSQL is not reachable (e.g. local unit tests)
    or if `db_path` explicitly points to a non-default test file.
    """
    if db_path is not None:
        name = Path(db_path).name.lower()
        # If unit tests pass a temporary custom SQLite path (not scanner_state.db / .epubmeta_state.db), honor it
        if name not in ("scanner_state.db", ".epubmeta_state.db"):
            conn = sqlite3.connect(str(db_path), timeout=timeout)
            conn.row_factory = sqlite3.Row
            return conn

    if is_postgres_scanner_enabled():
        existing_conn = getattr(_THREAD_LOCAL, "pg_conn", None)
        if existing_conn is not None and not getattr(existing_conn, "closed", False) and not getattr(existing_conn, "broken", False):
            return PgConnectionWrapper(existing_conn, persistent=True)

        drv, drv_ver = _ensure_psycopg()
        ct = int(max(3, min(timeout, 30)))
        if drv_ver == "v3":
            raw_conn = drv.connect(_PG_AVAILABILITY_CACHE["dsn"], connect_timeout=ct, autocommit=True)
        else:
            raw_conn = drv.connect(_PG_AVAILABILITY_CACHE["dsn"], connect_timeout=ct)
            raw_conn.autocommit = True
        _THREAD_LOCAL.pg_conn = raw_conn
        return PgConnectionWrapper(raw_conn, persistent=True)

    target = str(db_path or "scanner_state.db")
    conn = sqlite3.connect(target, timeout=timeout)
    conn.row_factory = sqlite3.Row
    return conn


def migrate_sqlite_scanner_db_to_postgres(sqlite_path: str | Path) -> dict[str, Any]:
    """
    One-time bulk migration of all tables from `scanner_state.db` (SQLite) into DevStack PostgreSQL (`BookDB`).
    Idempotent (`ON CONFLICT DO NOTHING` / `DO UPDATE`).
    """
    src_path = Path(sqlite_path)
    if not src_path.exists():
        return {"ok": False, "error": f"SQLite file not found: {src_path}"}
    if not is_postgres_scanner_enabled():
        return {"ok": False, "error": "PostgreSQL is not reachable from this environment"}

    s_conn = sqlite3.connect(str(src_path), timeout=30.0)
    p_conn = connect_scanner_db()
    stats: dict[str, int] = {}

    try:
        # 1. processed_files
        pf_cols = [
            "file_path", "file_name", "file_dir", "file_ext",
            "orig_hash", "orig_sha256", "orig_size",
            "file_hash", "file_sha256", "file_size", "mtime",
            "status", "isbn", "title", "author",
            "orig_title", "orig_author", "orig_isbn",
            "original_metadata_json", "updated_metadata_json",
            "match_confidence", "error_message", "updated_at",
        ]
        pf_col_str = ", ".join(pf_cols)
        pf_placeholders = ", ".join(["?"] * len(pf_cols))
        cur = s_conn.execute(f"SELECT {pf_col_str} FROM processed_files")
        pf_count = 0
        while True:
            rows = cur.fetchmany(2500)
            if not rows:
                break
            p_conn.executemany(
                f"""
                INSERT INTO processed_files ({pf_col_str})
                VALUES ({pf_placeholders})
                ON CONFLICT (file_path) DO UPDATE SET
                    status = EXCLUDED.status,
                    isbn = COALESCE(EXCLUDED.isbn, processed_files.isbn),
                    title = COALESCE(EXCLUDED.title, processed_files.title),
                    author = COALESCE(EXCLUDED.author, processed_files.author),
                    updated_metadata_json = COALESCE(EXCLUDED.updated_metadata_json, processed_files.updated_metadata_json),
                    updated_at = GREATEST(processed_files.updated_at, EXCLUDED.updated_at)
                """,
                rows,
            )
            p_conn.commit()
            pf_count += len(rows)
        stats["processed_files"] = pf_count

        # 2. metadata_cache
        existing_mc = p_conn.execute("SELECT COUNT(*) FROM metadata_cache").fetchone()[0]
        if existing_mc < 40000:
            p_conn.execute("DELETE FROM metadata_cache")
            mc_cols = [
                "source_file_path", "source_file_name", "orig_hash", "isbn",
                "norm_stem", "norm_title_author", "original_metadata_json",
                "metadata_json", "updated_at",
            ]
            mc_col_str = ", ".join(mc_cols)
            mc_placeholders = ", ".join(["?"] * len(mc_cols))
            cur = s_conn.execute(f"SELECT {mc_col_str} FROM metadata_cache")
            mc_count = 0
            while True:
                rows = cur.fetchmany(2500)
                if not rows:
                    break
                p_conn.executemany(
                    f"INSERT INTO metadata_cache ({mc_col_str}) VALUES ({mc_placeholders})",
                    rows,
                )
                p_conn.commit()
                mc_count += len(rows)
            stats["metadata_cache"] = mc_count
        else:
            stats["metadata_cache"] = existing_mc

        # 3. ai_usage_daily
        au_cols = ["usage_date", "requests_count", "input_tokens", "output_tokens", "estimated_cost_usd", "updated_at"]
        au_rows = s_conn.execute(f"SELECT {', '.join(au_cols)} FROM ai_usage_daily").fetchall()
        if au_rows:
            p_conn.executemany(
                """
                INSERT INTO ai_usage_daily (usage_date, requests_count, input_tokens, output_tokens, estimated_cost_usd, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (usage_date) DO UPDATE SET
                    requests_count = GREATEST(ai_usage_daily.requests_count, EXCLUDED.requests_count),
                    input_tokens = GREATEST(ai_usage_daily.input_tokens, EXCLUDED.input_tokens),
                    output_tokens = GREATEST(ai_usage_daily.output_tokens, EXCLUDED.output_tokens),
                    estimated_cost_usd = GREATEST(ai_usage_daily.estimated_cost_usd, EXCLUDED.estimated_cost_usd),
                    updated_at = GREATEST(ai_usage_daily.updated_at, EXCLUDED.updated_at)
                """,
                au_rows,
            )
            p_conn.commit()
        stats["ai_usage_daily"] = len(au_rows)

        # 4. gemini_batch_jobs
        jb_cols = ["job_name", "model", "status", "items_count", "estimated_input_tokens", "estimated_cost_usd", "created_at", "updated_at"]
        jb_rows = s_conn.execute(f"SELECT {', '.join(jb_cols)} FROM gemini_batch_jobs").fetchall()
        if jb_rows:
            p_conn.executemany(
                f"INSERT OR IGNORE INTO gemini_batch_jobs ({', '.join(jb_cols)}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                jb_rows,
            )
            p_conn.commit()
        stats["gemini_batch_jobs"] = len(jb_rows)

        # 5. gemini_batch_items
        bi_cols = [
            "file_path", "job_name", "status", "prompt_text",
            "fallback_title", "fallback_author", "original_metadata_json",
            "ai_result_json", "error_message", "created_at", "updated_at",
        ]
        bi_rows = s_conn.execute(f"SELECT {', '.join(bi_cols)} FROM gemini_batch_items").fetchall()
        if bi_rows:
            p_conn.executemany(
                f"INSERT OR IGNORE INTO gemini_batch_items ({', '.join(bi_cols)}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                bi_rows,
            )
            p_conn.commit()
        stats["gemini_batch_items"] = len(bi_rows)

        # 6. file_states (use psycopg v3 COPY for 1.53M rows if available -> finishes in ~4 seconds!)
        existing_fs = p_conn.execute("SELECT COUNT(*) FROM file_states").fetchone()[0]
        if existing_fs < 1500000:
            raw_pg = getattr(p_conn, "_conn", None)
            fs_count = 0
            cur_fs = s_conn.execute("SELECT filepath, modified_at, size, hash, synced_at FROM file_states")
            raw_cur = raw_pg.cursor() if raw_pg else None
            if raw_cur is not None and hasattr(raw_cur, "copy"):
                raw_cur.execute("DROP TABLE IF EXISTS tmp_file_states")
                raw_cur.execute("CREATE TEMP TABLE tmp_file_states (filepath TEXT, modified_at TEXT, size BIGINT, hash TEXT, synced_at TEXT)")
                with raw_cur.copy("COPY tmp_file_states (filepath, modified_at, size, hash, synced_at) FROM STDIN") as copy:
                    while True:
                        rows = cur_fs.fetchmany(25000)
                        if not rows:
                            break
                        for r in rows:
                            copy.write_row(tuple(_clean_pg_param(x) for x in r))
                        fs_count += len(rows)
                raw_cur.execute(
                    """
                    INSERT INTO file_states (filepath, modified_at, size, hash, synced_at)
                    SELECT DISTINCT ON (filepath) filepath, modified_at, size, hash, synced_at
                    FROM tmp_file_states
                    ON CONFLICT (filepath) DO UPDATE SET
                        modified_at = EXCLUDED.modified_at,
                        size = EXCLUDED.size,
                        hash = EXCLUDED.hash,
                        synced_at = EXCLUDED.synced_at
                    """
                )
                raw_cur.execute("DROP TABLE IF EXISTS tmp_file_states")
                p_conn.commit()
            else:
                while True:
                    rows = cur_fs.fetchmany(10000)
                    if not rows:
                        break
                    p_conn.executemany(
                        "INSERT OR REPLACE INTO file_states (filepath, modified_at, size, hash, synced_at) VALUES (?, ?, ?, ?, ?)",
                        rows,
                    )
                    p_conn.commit()
                    fs_count += len(rows)
            stats["file_states"] = fs_count
        else:
            stats["file_states"] = existing_fs

        return {"ok": True, "migrated": stats}
    finally:
        s_conn.close()
        p_conn.close()
