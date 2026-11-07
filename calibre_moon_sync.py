#!/usr/bin/env python3
"""
Calibre Library & Moon+ Reader Multi-Pass Enrichment Sync (`calibre_moon_sync.py`)
==================================================================================
Two-pass staged utility for syncing enriched `.epub` books from MyLibrary (`U:\\Media\\books`)
into a local Calibre library (`I:\\Calibre Library`), syncing deletions/additions with your
Android reader device, and migrating Moon+ Reader WebDAV bookmarks (`U:\\webdav\\Apps\\Books\\.Moon+\\Cache`).

Why Two Passes?
---------------
When a book's title/author/file is updated, the old `.epub` file is still sitting on your
tablet/phone. Doing this in two passes prevents duplicate old `.epub` files on your device
and ensures Moon+ Reader picks up your reading position (`.po`) and highlights/bookmarks (`.an`)
under the new Calibre filename when re-imported:

  PASS 1 (`python calibre_moon_sync.py pass1 --apply`):
    1. Scans `I:\\Calibre Library` and checks `/mylibrary/api/calibre/check-enriched/` to identify
       which books have an enriched `.epub` on the NAS (`U:\\Media\\books\\...`).
    2. Snapshots/backs up each book's current Moon+ Reader `.po` and `.an` files from
       `U:\\webdav\\Apps\\Books\\.Moon+\\Cache` into the staging folder (`I:\\Calibre Library\\_EnrichStaging`)
       so your bookmarks are safely preserved even if Moon+ Reader clears them when you delete the book on the device.
    3. Tags the identified books in Calibre with `ENRICH_1_REMOVE_FROM_DEVICE` (or deletes them
       immediately from Calibre if `--delete-from-calibre` is passed).
    --> YOUR DEVICE STEP BETWEEN PASS 1 & PASS 2:
        Connect your device to Calibre, filter by `tags:"=ENRICH_1_REMOVE_FROM_DEVICE"`, remove those
        books from the device (and/or delete them in Moon+ Reader), then run Pass 2!

  PASS 2 (`python calibre_moon_sync.py pass2 --apply`):
    1. Reads the staged manifest (`I:\\Calibre Library\\_EnrichStaging\\staging_manifest.json`).
    2. Deletes the old book entries from `I:\\Calibre Library` (if not already deleted in Pass 1).
    3. Re-adds the enriched `.epub` files from `U:\\Media\\books\\...` into `I:\\Calibre Library`
       with full enriched metadata and tags them with `ENRICH_2_SEND_TO_DEVICE`.
    4. Reads the exact new `.epub` filename that Calibre generated (`<NewTitle> - <NewAuthor>.epub`).
    5. Writes the updated `.po` reading position and zlib-rewritten `.an` bookmark/highlight file
       into `U:\\webdav\\Apps\\Books\\.Moon+\\Cache` under the new Calibre filename!
    --> FINAL DEVICE STEP:
        In Calibre, filter by `tags:"=ENRICH_2_SEND_TO_DEVICE"` -> click "Send to device",
        import into Moon+ Reader, and sync WebDAV bookmarks!
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_SERVER_URL = os.environ.get("MYLIBRARY_URL", "http://192.168.1.43:8080/mylibrary").rstrip("/")
DEFAULT_CALIBRE_DIR = os.environ.get("CALIBRE_LIBRARY_DIR", r"I:\Calibre Library")
DEFAULT_MOON_CACHE_DIR = os.environ.get("MOON_CACHE_DIR", r"U:\webdav\Apps\Books\.Moon+\Cache")
DEFAULT_NAS_PREFIX = os.environ.get("MYLIBRARY_PATH_PREFIX", r"U:\Media")

TAG_PASS1_REMOVE = "ENRICH_1_REMOVE_FROM_DEVICE"
TAG_PASS2_SEND = "ENRICH_2_SEND_TO_DEVICE"


# ==============================================================================
# Helpers: Filename Sanitization, Hashing & Moon+ Reader `.po` / `.an` Codec
# ==============================================================================

def clean_filename_text(val: str) -> str:
    """Strip newlines, tabs, control chars, and Unicode line/paragraph separators from filenames."""
    s = re.sub(r"[\x00-\x1f\x7f-\x9f\r\n\t\u2028\u2029]+", " ", val or "")
    return re.sub(r"\s+", " ", s).strip(" .")


def sanitize_windows_book_filename(book_filename: str) -> str:
    """Ensure a book filename is a valid single-line Windows/SMB filename and within length limits."""
    cleaned = clean_filename_text(book_filename)
    cleaned = re.sub(r'[\\/:*?"<>|]', "_", cleaned).strip(" .")
    stem, ext = os.path.splitext(cleaned)
    if len(stem) > 180:
        stem = stem[:180].rstrip(" ._-")
    return f"{stem}{ext}" if ext else stem


def moon_sanitize_filename(book_filename: str) -> str:
    """
    Convert an `.epub` filename (e.g. `Death's End: A Novel - Cixin Liu.epub`)
    into Moon+ Reader's outer WebDAV filename stem (`Death_s End_ A Novel - Cixin Liu.epub`).
    """
    cleaned = sanitize_windows_book_filename(book_filename)
    return re.sub(r"[':?*\"<>|/\\]", "_", cleaned).strip(" .")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def decompress_moon_an(an_path: Path) -> str:
    """Decompress a Moon+ Reader `.an` file (zlib deflate -> UTF-8 string)."""
    raw = an_path.read_bytes()
    return zlib.decompress(raw).decode("utf-8", errors="replace")


def rewrite_moon_an_book_filename(decompressed_text: str, new_book_filename: str) -> Tuple[str, int]:
    """
    Update the embedded book filename and `/storage/...` device paths inside every `#`
    bookmark/highlight record of a decompressed Moon+ Reader `.an` file.
    """
    lines = decompressed_text.split("\n")
    replacements = 0
    new_lower = new_book_filename.lower()

    i = 0
    n = len(lines)
    while i < n:
        if lines[i].strip() == "#" and (i + 4) < n:
            rec_id = lines[i + 1].strip()
            old_fname = lines[i + 2].strip()
            dev_path = lines[i + 3].strip()
            dev_path_lower = lines[i + 4].strip()

            if (
                rec_id.isdigit()
                and not old_fname.isdigit()
                and old_fname.lower().endswith((".epub", ".pdf", ".mobi", ".azw3"))
            ):
                if old_fname != new_book_filename:
                    lines[i + 2] = new_book_filename
                    replacements += 1

                if "/" in dev_path:
                    parent_dir = dev_path.rsplit("/", 1)[0]
                    new_dev_path = f"{parent_dir}/{new_book_filename}"
                    if dev_path != new_dev_path:
                        lines[i + 3] = new_dev_path

                if "/" in dev_path_lower:
                    parent_lower = dev_path_lower.rsplit("/", 1)[0]
                    new_dev_lower = f"{parent_lower}/{new_lower}"
                    if dev_path_lower != new_dev_lower:
                        lines[i + 4] = new_dev_lower
                i += 5
                continue
        i += 1

    return "\n".join(lines), replacements


def compress_moon_an(text: str) -> bytes:
    """Compress updated `.an` text back into Moon+ Reader's zlib format."""
    return zlib.compress(text.encode("utf-8"))


# ==============================================================================
# API Client for `/mylibrary/api/calibre/check-enriched/`
# ==============================================================================

def post_check_enriched(
    server_url: str,
    books: List[Dict[str, Any]],
    moon_filenames: List[str],
    nas_prefix: str = DEFAULT_NAS_PREFIX,
    timeout: int = 60,
) -> Dict[str, Any]:
    url = f"{server_url.rstrip('/')}/api/calibre/check-enriched/"
    payload = {
        "path_prefix": nas_prefix,
        "books": books,
        "moon_filenames": moon_filenames,
    }
    data_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data_bytes,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


# ==============================================================================
# Calibre Library Inspection & `calibredb.exe` Operations
# ==============================================================================

def find_calibredb_exe(explicit_path: Optional[str] = None) -> Optional[str]:
    if explicit_path and os.path.exists(explicit_path):
        return explicit_path
    candidates = [
        r"C:\Program Files\Calibre2\calibredb.exe",
        r"C:\Program Files (x86)\Calibre2\calibredb.exe",
        shutil.which("calibredb"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def scan_local_calibre_epubs(calibre_dir: Path) -> List[Dict[str, Any]]:
    db_file = calibre_dir / "metadata.db"
    if not db_file.exists():
        raise FileNotFoundError(f"Calibre metadata.db not found at: {db_file}")

    conn = sqlite3.connect(str(db_file), timeout=15.0)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    rows = cur.execute(
        """
        SELECT
            b.id AS book_id,
            b.title,
            b.path AS book_dir_path,
            c.text AS description,
            d.name AS data_name,
            d.format AS data_format,
            (SELECT group_concat(a.name, ' & ')
             FROM books_authors_link bal
             JOIN authors a ON bal.author = a.id
             WHERE bal.book = b.id) AS authors,
            (SELECT p.name
             FROM books_publishers_link bpl
             JOIN publishers p ON bpl.publisher = p.id
             WHERE bpl.book = b.id LIMIT 1) AS publisher,
            (SELECT i.val
             FROM identifiers i
             WHERE i.book = b.id AND i.type = 'isbn' LIMIT 1) AS isbn
        FROM books b
        JOIN data d ON d.book = b.id
        LEFT JOIN comments c ON c.book = b.id
        WHERE upper(d.format) = 'EPUB'
        ORDER BY b.id ASC
        """
    ).fetchall()
    conn.close()

    calibre_books: List[Dict[str, Any]] = []
    print(f"Found {len(rows):,} EPUB entries in Calibre metadata.db ({db_file}). Hashing local files...")

    for idx, r in enumerate(rows, start=1):
        rel_dir = r["book_dir_path"] or ""
        data_name = r["data_name"] or ""
        epub_filename = f"{data_name}.epub"
        epub_path = calibre_dir / rel_dir / epub_filename
        if not epub_path.exists():
            continue

        try:
            file_sha = sha256_file(epub_path)
        except Exception as e:
            print(f"  [WARN] Could not hash {epub_path}: {e}")
            continue

        calibre_books.append({
            "calibre_id": int(r["book_id"]),
            "title": r["title"] or "",
            "authors": r["authors"] or "",
            "publisher": r["publisher"] or "",
            "isbn": r["isbn"] or "",
            "description": r["description"] or "",
            "book_dir_path": rel_dir,
            "data_name": data_name,
            "file_name": epub_filename,
            "local_epub_path": str(epub_path),
            "sha256": file_sha,
        })
        if idx % 250 == 0:
            print(f"  Hashed {idx:,}/{len(rows):,} Calibre EPUBs...")

    return calibre_books


def get_calibre_epub_filename(calibre_dir: Path, book_id: int) -> Optional[str]:
    """Read `data.name` from `metadata.db` for a given `book_id`."""
    db_file = calibre_dir / "metadata.db"
    conn = sqlite3.connect(str(db_file), timeout=15.0)
    cur = conn.cursor()
    row = cur.execute(
        "SELECT name FROM data WHERE book = ? AND upper(format) = 'EPUB' LIMIT 1",
        (book_id,),
    ).fetchone()
    conn.close()
    if row and row[0]:
        return f"{row[0]}.epub"
    return None


def tag_calibre_book(calibredb_exe: str, calibre_dir: Path, book_id: int, tag_name: str) -> bool:
    cmd = [
        calibredb_exe,
        "set_metadata",
        str(book_id),
        "-f",
        f"tags:{tag_name}",
        "--with-library",
        str(calibre_dir),
    ]
    res = subprocess.run(cmd, check=False, capture_output=True, text=True)
    return res.returncode == 0


def remove_calibre_book(calibredb_exe: str, calibre_dir: Path, book_id: int) -> bool:
    cmd = [
        calibredb_exe,
        "remove",
        str(book_id),
        "--with-library",
        str(calibre_dir),
    ]
    res = subprocess.run(cmd, check=False, capture_output=True, text=True)
    return res.returncode == 0


def add_enriched_book_to_calibre(
    calibredb_exe: str,
    calibre_dir: Path,
    nas_epub_path: Path,
    enriched: Dict[str, Any],
) -> Optional[int]:
    """
    Add the enriched `.epub` file to Calibre using `calibredb add`, set its full metadata
    and `ENRICH_2_SEND_TO_DEVICE` tag, and return the newly created Calibre `book_id`.
    """
    cmd_add = [
        calibredb_exe,
        "add",
        str(nas_epub_path),
        "--tags",
        TAG_PASS2_SEND,
        "--with-library",
        str(calibre_dir),
    ]
    if enriched.get("title"):
        cmd_add.extend(["--title", str(enriched["title"])])
    if enriched.get("authors"):
        cmd_add.extend(["--authors", str(enriched["authors"])])
    if enriched.get("isbn"):
        cmd_add.extend(["--isbn", str(enriched["isbn"])])

    res = subprocess.run(cmd_add, check=False, capture_output=True, text=True)
    out = (res.stdout or "") + "\n" + (res.stderr or "")
    m = re.search(r"Added book ids:\s*(\d+)", out, re.IGNORECASE)
    if not m:
        return None

    new_id = int(m.group(1))
    fields: List[str] = []
    if enriched.get("publisher"):
        fields.extend(["-f", f"publisher:{enriched['publisher']}"])
    if enriched.get("description"):
        fields.extend(["-f", f"comments:{enriched['description']}"])
    if fields:
        subprocess.run(
            [calibredb_exe, "set_metadata", str(new_id), *fields, "--with-library", str(calibre_dir)],
            check=False,
            capture_output=True,
            text=True,
        )
    return new_id


# ==============================================================================
# Moon+ Reader Cache Indexing & Migration (`U:\webdav\Apps\Books\.Moon+\Cache`)
# ==============================================================================

def index_moon_cache(moon_cache_dir: Path) -> Dict[str, Dict[str, Any]]:
    entries: Dict[str, Dict[str, Any]] = {}
    if not moon_cache_dir.exists():
        return entries

    for p in sorted(moon_cache_dir.iterdir()):
        if not p.is_file():
            continue
        name = p.name
        if name.endswith(".po"):
            book_stem = name[:-3]
            rec = entries.setdefault(
                book_stem,
                {"outer_book_name": book_stem, "inner_book_name": "", "po_path": None, "an_path": None},
            )
            rec["po_path"] = p
        elif name.endswith(".an"):
            book_stem = name[:-3]
            rec = entries.setdefault(
                book_stem,
                {"outer_book_name": book_stem, "inner_book_name": "", "po_path": None, "an_path": None},
            )
            rec["an_path"] = p
            try:
                txt = decompress_moon_an(p)
                for ln in txt.splitlines():
                    s = ln.strip()
                    if s.lower().endswith((".epub", ".pdf", ".mobi", ".azw3")) and not s.startswith("/"):
                        rec["inner_book_name"] = s
                        break
            except Exception:
                pass

    return entries


def find_matching_moon_entry(
    moon_entries: Dict[str, Dict[str, Any]],
    candidate_filenames: List[str],
) -> Optional[Dict[str, Any]]:
    """Find a Moon+ cache entry matching any of the candidate book filenames."""
    lower_map = {k.lower(): v for k, v in moon_entries.items()}
    for cand in candidate_filenames:
        if not cand:
            continue
        san = moon_sanitize_filename(cand).lower()
        if san in lower_map:
            return lower_map[san]
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]
    return None


def write_updated_moon_files(
    moon_cache_dir: Path,
    source_po_path: Optional[Path],
    source_an_path: Optional[Path],
    new_book_filename: str,
    apply_changes: bool = False,
) -> Dict[str, Any]:
    """
    Write `<MoonSanitize(new_book_filename)>.po` and `.an` (with zlib-rewritten inner filename lines)
    into `U:\\webdav\\Apps\\Books\\.Moon+\\Cache`.
    """
    new_book_filename = sanitize_windows_book_filename(new_book_filename)
    new_outer = moon_sanitize_filename(new_book_filename)
    new_po_path = moon_cache_dir / f"{new_outer}.po"
    new_an_path = moon_cache_dir / f"{new_outer}.an"

    res = {
        "new_outer": new_outer,
        "new_inner_filename": new_book_filename,
        "po_written": False,
        "an_written": False,
        "an_inner_replacements": 0,
    }

    if source_po_path and source_po_path.exists():
        res["po_written"] = True
        if apply_changes:
            raw_po = source_po_path.read_text(encoding="utf-8", errors="replace").strip()
            refreshed_po = re.sub(r"^\d+\*", f"{int(time.time() * 1000)}*", raw_po)
            new_po_path.write_text(refreshed_po, encoding="utf-8")

    if source_an_path and source_an_path.exists():
        try:
            txt = decompress_moon_an(source_an_path)
            updated_txt, rep_cnt = rewrite_moon_an_book_filename(txt, new_book_filename)
            res["an_written"] = True
            res["an_inner_replacements"] = rep_cnt
            if apply_changes:
                new_an_path.write_bytes(compress_moon_an(updated_txt))
                shutil.copystat(source_an_path, new_an_path)
        except Exception as e:
            res["error"] = str(e)

    return res


# ==============================================================================
# Pass 1, Pass 2 & Status Workflows
# ==============================================================================

def get_staging_dir(calibre_dir: Path, custom_staging: Optional[str] = None) -> Path:
    if custom_staging:
        return Path(custom_staging)
    if calibre_dir.exists():
        return calibre_dir / "_EnrichStaging"
    return Path.cwd() / "_EnrichStaging"


def run_pass1(args: argparse.Namespace) -> int:
    """
    PASS 1:
      - Identify Calibre books that have an enriched `.epub` on the NAS
      - Snapshot their original `.epub` and any Moon+ `.po`/`.an` bookmark files into `_EnrichStaging`
      - Tag them in Calibre with `ENRICH_1_REMOVE_FROM_DEVICE` (or delete them immediately if `--delete-from-calibre`)
      - Save `staging_manifest.json` for Pass 2
    """
    calibre_dir = Path(args.calibre_dir)
    moon_cache_dir = Path(args.moon_cache)
    staging_dir = get_staging_dir(calibre_dir, args.staging_dir)
    manifest_path = staging_dir / "staging_manifest.json"
    calibredb_exe = find_calibredb_exe(args.calibredb)

    mode_str = "APPLY MODE" if args.apply else "DRY-RUN PREVIEW (Pass --apply to execute Pass 1)"
    print("=" * 88)
    print(f" PASS 1: IDENTIFY ENRICHED BOOKS, SNAPSHOT MOON+ BOOKMARKS & STAGE REMOVAL [{mode_str}]")
    print("=" * 88)
    print(f" Calibre Library  : {calibre_dir} ({'FOUND' if calibre_dir.exists() else 'NOT MOUNTED'})")
    print(f" Moon+ Cache Dir  : {moon_cache_dir} ({'FOUND' if moon_cache_dir.exists() else 'NOT FOUND'})")
    print(f" Staging Folder   : {staging_dir}")
    print(f" calibredb.exe    : {calibredb_exe or 'NOT FOUND (will use SQLite/manifest only)'}")
    print("-" * 88)

    moon_entries = index_moon_cache(moon_cache_dir) if moon_cache_dir.exists() else {}
    print(f"Indexed {len(moon_entries):,} book entries in Moon+ Reader WebDAV Cache.")

    if not calibre_dir.exists() or not (calibre_dir / "metadata.db").exists():
        print(f"\n[NOTE] '{calibre_dir}' is not mounted on this PC.")
        print("       Copy `calibre_moon_sync.py` to the PC with `I:\\Calibre Library` to run Pass 1 & Pass 2,")
        print("       or run `python calibre_moon_sync.py moon-only` on this PC to preview/sync Moon+ bookmarks directly.")
        return 0

    calibre_books = scan_local_calibre_epubs(calibre_dir)
    calibre_by_id = {b["calibre_id"]: b for b in calibre_books}

    matched_calibre: List[Dict[str, Any]] = []
    batch_size = max(50, args.batch_size)
    for i in range(0, len(calibre_books), batch_size):
        batch = calibre_books[i:i + batch_size]
        resp = post_check_enriched(args.url, batch, [], nas_prefix=args.nas_prefix)
        matched_calibre.extend(resp.get("matched_books", []))

    needs_update = [
        m for m in matched_calibre
        if not m.get("already_same_binary")
        or (m["enriched"].get("isbn") and not m.get("calibre_isbn"))
        or (m["enriched"].get("title") and m["enriched"]["title"] != m.get("calibre_title"))
    ]

    print(f"\nIdentified {len(needs_update):,} Calibre books with enriched NAS versions (out of {len(calibre_books):,} scanned).")
    print("-" * 88)

    staged_items: List[Dict[str, Any]] = []
    if args.apply:
        (staging_dir / "calibre_backups").mkdir(parents=True, exist_ok=True)
        (staging_dir / "moon_backups").mkdir(parents=True, exist_ok=True)

    for idx, m in enumerate(needs_update, start=1):
        cid = m["calibre_id"]
        local_b = calibre_by_id[cid]
        enr = m["enriched"]
        old_fname = local_b["file_name"]

        # Check if this book has a Moon+ Reader bookmark (.po / .an)
        moon_match = find_matching_moon_entry(
            moon_entries,
            [old_fname, enr.get("file_name", ""), f"{enr.get('orig_title', '')} - {enr.get('orig_author', '')}.epub"],
        )

        staged_po_bak = None
        staged_an_bak = None
        if moon_match and args.apply:
            if moon_match.get("po_path") and moon_match["po_path"].exists():
                dst_po = staging_dir / "moon_backups" / moon_match["po_path"].name
                shutil.copy2(moon_match["po_path"], dst_po)
                staged_po_bak = str(dst_po)
            if moon_match.get("an_path") and moon_match["an_path"].exists():
                dst_an = staging_dir / "moon_backups" / moon_match["an_path"].name
                shutil.copy2(moon_match["an_path"], dst_an)
                staged_an_bak = str(dst_an)

        # Backup original Calibre EPUB
        old_epub_path = Path(local_b["local_epub_path"])
        cal_bak_path = None
        if args.apply and old_epub_path.exists():
            dst_cal = staging_dir / "calibre_backups" / f"{cid}_{old_fname}"
            shutil.copy2(old_epub_path, dst_cal)
            cal_bak_path = str(dst_cal)

        removed_in_pass1 = False
        tagged_in_pass1 = False
        if args.apply and calibredb_exe:
            if args.delete_from_calibre:
                removed_in_pass1 = remove_calibre_book(calibredb_exe, calibre_dir, cid)
            else:
                tagged_in_pass1 = tag_calibre_book(calibredb_exe, calibre_dir, cid, TAG_PASS1_REMOVE)

        moon_status = "None"
        if moon_match:
            parts = []
            if moon_match.get("po_path"):
                parts.append(".po")
            if moon_match.get("an_path"):
                parts.append(".an")
            moon_status = f"FOUND ({', '.join(parts)}: {moon_match['outer_book_name']})"

        print(f"[{idx}] Calibre #{cid}: '{local_b['title']}' by '{local_b['authors']}'")
        print(f"    Current Calibre File : {old_fname}")
        print(f"    Enriched NAS File    : {enr.get('mapped_path')}")
        print(f"    Enriched Metadata    : '{enr.get('title')}' by '{enr.get('authors')}' (ISBN: {enr.get('isbn') or '—'})")
        print(f"    Moon+ Bookmark       : {moon_status}")
        if args.apply:
            if removed_in_pass1:
                print(f"    Pass 1 Action        : DELETED from Calibre #{cid} & backed up")
            elif tagged_in_pass1:
                print(f"    Pass 1 Action        : TAGGED with '{TAG_PASS1_REMOVE}' & backed up")

        staged_items.append({
            "calibre_id": cid,
            "old_calibre_title": local_b["title"],
            "old_calibre_authors": local_b["authors"],
            "old_calibre_filename": old_fname,
            "old_local_epub_path": str(old_epub_path),
            "calibre_backup_path": cal_bak_path,
            "removed_from_calibre": removed_in_pass1,
            "tagged_for_removal": tagged_in_pass1,
            "moon_outer_name": moon_match["outer_book_name"] if moon_match else None,
            "moon_po_backup": staged_po_bak or (str(moon_match["po_path"]) if (moon_match and moon_match.get("po_path")) else None),
            "moon_an_backup": staged_an_bak or (str(moon_match["an_path"]) if (moon_match and moon_match.get("an_path")) else None),
            "enriched": enr,
            "pass2_completed": False,
            "new_calibre_id": None,
            "new_calibre_filename": None,
        })

    if args.apply:
        staging_dir.mkdir(parents=True, exist_ok=True)
        manifest_data = {
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
            "calibre_dir": str(calibre_dir),
            "moon_cache_dir": str(moon_cache_dir),
            "total_staged": len(staged_items),
            "items": staged_items,
        }
        manifest_path.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")
        print("=" * 88)
        print(f" PASS 1 COMPLETE! Staged {len(staged_items):,} books to: {manifest_path}")
        print(" NEXT STEPS BEFORE PASS 2:")
        print(f"   1. In Calibre, search `tags:\"={TAG_PASS1_REMOVE}\"` with your device connected.")
        print("   2. Right-click -> 'Remove books' -> 'Remove matching books from device' (and delete in Moon+ Reader if needed).")
        print("   3. Run `python calibre_moon_sync.py pass2 --apply` to delete the old Calibre entries,")
        print("      re-add the enriched EPUBs to Calibre, and update the Moon+ Reader bookmarks!")
        print("=" * 88)
    else:
        print("=" * 88)
        print(" PASS 1 DRY-RUN COMPLETE. Re-run `python calibre_moon_sync.py pass1 --apply` to stage & backup.")
        print("=" * 88)

    return 0


def run_pass2(args: argparse.Namespace) -> int:
    """
    PASS 2:
      - Reads `staging_manifest.json` created by Pass 1
      - Removes the old Calibre book IDs from `I:\\Calibre Library` (if not already removed)
      - Re-adds the enriched `.epub` files from `U:\\Media\\books\\...` into `I:\\Calibre Library`
        with full enriched metadata and tag `ENRICH_2_SEND_TO_DEVICE`
      - Reads the exact new `.epub` filename generated by Calibre (`<NewTitle> - <NewAuthor>.epub`)
      - Writes the updated `.po` reading position and zlib-rewritten `.an` bookmark file into
        `U:\\webdav\\Apps\\Books\\.Moon+\\Cache` under the new Calibre filename!
    """
    calibre_dir = Path(args.calibre_dir)
    moon_cache_dir = Path(args.moon_cache)
    staging_dir = get_staging_dir(calibre_dir, args.staging_dir)
    manifest_path = staging_dir / "staging_manifest.json"
    calibredb_exe = find_calibredb_exe(args.calibredb)

    if not manifest_path.exists():
        print(f"Error: Staging manifest not found at {manifest_path}. Run `pass1 --apply` first.", file=sys.stderr)
        return 1

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = manifest.get("items", [])
    pending = [it for it in items if not it.get("pass2_completed")]

    mode_str = "APPLY MODE" if args.apply else "DRY-RUN PREVIEW (Pass --apply to execute Pass 2)"
    print("=" * 88)
    print(f" PASS 2: DELETE OLD CALIBRE BOOKS, RE-ADD ENRICHED EPUBS & UPDATE MOON+ BOOKMARKS [{mode_str}]")
    print("=" * 88)
    print(f" Manifest File    : {manifest_path} ({len(pending):,} pending of {len(items):,} staged)")
    print(f" Calibre Library  : {calibre_dir}")
    print(f" Moon+ Cache Dir  : {moon_cache_dir}")
    print(f" calibredb.exe    : {calibredb_exe or 'NOT FOUND'}")
    print("-" * 88)

    if args.apply and not calibredb_exe:
        print("Error: `calibredb.exe` is required for Pass 2 remove/re-add. Pass `--calibredb <path>`.", file=sys.stderr)
        return 1

    for idx, item in enumerate(pending, start=1):
        old_id = item["calibre_id"]
        old_fname = item["old_calibre_filename"]
        enr = item["enriched"]
        nas_path = Path(enr.get("mapped_path") or "")
        expected_new_fname = enr.get("expected_calibre_filename") or old_fname

        print(f"[{idx}] Old Calibre #{old_id}: '{item['old_calibre_title']}' -> Enriched: '{enr.get('title')}' by '{enr.get('authors')}'")
        if not nas_path.exists():
            print(f"    [SKIP] Enriched NAS file not found at: {nas_path}")
            continue

        if not args.apply:
            print(f"    [DRY-RUN] 1. Would remove old Calibre #{old_id} ('{old_fname}')")
            print(f"    [DRY-RUN] 2. Would re-add enriched EPUB from '{nas_path}' with tag '{TAG_PASS2_SEND}'")
            if item.get("moon_po_backup") or item.get("moon_an_backup"):
                print(f"    [DRY-RUN] 3. Would update Moon+ bookmark '{item.get('moon_outer_name')}' -> '{moon_sanitize_filename(expected_new_fname)}'")
            continue

        # 1. Remove old book from Calibre if not already removed in Pass 1
        if not item.get("removed_from_calibre"):
            remove_calibre_book(calibredb_exe, calibre_dir, old_id)
            item["removed_from_calibre"] = True

        # 2. Add enriched .epub to Calibre and apply metadata
        new_id = add_enriched_book_to_calibre(calibredb_exe, calibre_dir, nas_path, enr)
        if not new_id:
            print(f"    [ERROR] Failed to add '{nas_path}' to Calibre.")
            continue

        new_calibre_fname = get_calibre_epub_filename(calibre_dir, new_id) or expected_new_fname
        item["new_calibre_id"] = new_id
        item["new_calibre_filename"] = new_calibre_fname
        item["pass2_completed"] = True
        print(f"    -> Re-added as Calibre #{new_id}: '{new_calibre_fname}' (Tag: {TAG_PASS2_SEND})")

        # 3. Update Moon+ Reader .po and .an files in WebDAV Cache using the exact new Calibre filename
        po_src = Path(item["moon_po_backup"]) if item.get("moon_po_backup") else None
        an_src = Path(item["moon_an_backup"]) if item.get("moon_an_backup") else None
        if (po_src and po_src.exists()) or (an_src and an_src.exists()):
            m_res = write_updated_moon_files(
                moon_cache_dir=moon_cache_dir,
                source_po_path=po_src,
                source_an_path=an_src,
                new_book_filename=new_calibre_fname,
                apply_changes=True,
            )
            print(
                f"    -> Updated Moon+ Cache: '{m_res['new_outer']}' "
                f"(po={m_res['po_written']}, an={m_res['an_written']}, inner_refs={m_res['an_inner_replacements']})"
            )

    if args.apply:
        manifest["updated_at"] = datetime.now(tz=timezone.utc).isoformat()
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print("=" * 88)
        print(" PASS 2 COMPLETE!")
        print(f"   1. In Calibre, search `tags:\"={TAG_PASS2_SEND}\"` and click 'Send to device'.")
        print("   2. Open the newly imported books in Moon+ Reader — your updated `.po` reading positions")
        print("      and `.an` bookmarks/highlights are ready in `U:\\webdav\\Apps\\Books\\.Moon+\\Cache`!")
        print("=" * 88)
    else:
        print("=" * 88)
        print(" PASS 2 DRY-RUN COMPLETE. Re-run `python calibre_moon_sync.py pass2 --apply` to execute.")
        print("=" * 88)

    return 0


def run_moon_only(args: argparse.Namespace) -> int:
    """Standalone mode to check/migrate Moon+ Reader WebDAV Cache directly against MyLibrary API."""
    moon_cache_dir = Path(args.moon_cache)
    if not moon_cache_dir.exists():
        print(f"Moon+ Cache directory not found: {moon_cache_dir}", file=sys.stderr)
        return 1

    moon_entries = index_moon_cache(moon_cache_dir)
    resp = post_check_enriched(args.url, [], list(moon_entries.keys()), nas_prefix=args.nas_prefix)
    matched_moon = resp.get("matched_moon", [])

    migrations = []
    already_synced = 0
    for mm in matched_moon:
        outer_name = mm["moon_book_filename"]
        enr = mm["enriched"]
        entry = moon_entries.get(outer_name)
        if not entry:
            continue
        raw_target = enr.get("expected_calibre_filename") or enr.get("file_name")
        if not raw_target:
            continue
        target_fname = sanitize_windows_book_filename(raw_target)
        target_outer = moon_sanitize_filename(target_fname)

        if target_outer == outer_name and (not entry.get("inner_book_name") or entry["inner_book_name"] == target_fname):
            already_synced += 1
            continue

        # Check if the migrated target cache entry already exists and is up-to-date
        existing_target = moon_entries.get(target_outer)
        if existing_target:
            po_ok = (not entry.get("po_path")) or bool(existing_target.get("po_path"))
            an_ok = (not entry.get("an_path")) or (
                bool(existing_target.get("an_path"))
                and (not entry.get("inner_book_name") or existing_target.get("inner_book_name") == target_fname)
            )
            if po_ok and an_ok:
                already_synced += 1
                continue

        migrations.append((entry, target_fname, enr))

    mode_str = "APPLY MODE" if args.apply else "DRY-RUN PREVIEW"
    print("=" * 88)
    print(f" MOON+ READER STANDALONE BOOKMARK SYNC [{mode_str}]")
    print("=" * 88)
    print(
        f" Checked: {len(moon_entries):,} cache entries | Matched Enriched: {len(matched_moon):,} "
        f"| Already Synced: {already_synced:,} | Needing Filename Update: {len(migrations):,}"
    )
    print("-" * 88)

    backup_dir = moon_cache_dir / "_SyncBackup" if args.apply else None
    if backup_dir:
        backup_dir.mkdir(parents=True, exist_ok=True)

    for idx, (entry, target_fname, enr) in enumerate(migrations, start=1):
        po_p = entry.get("po_path")
        an_p = entry.get("an_path")
        if args.apply and backup_dir:
            if po_p and po_p.exists():
                shutil.copy2(po_p, backup_dir / po_p.name)
            if an_p and an_p.exists():
                shutil.copy2(an_p, backup_dir / an_p.name)

        res = write_updated_moon_files(
            moon_cache_dir=moon_cache_dir,
            source_po_path=po_p,
            source_an_path=an_p,
            new_book_filename=target_fname,
            apply_changes=args.apply,
        )
        print(f"[{idx}] {entry['outer_book_name']}  -->  {res['new_outer']}")
        print(f"    New Book Filename : {res['new_inner_filename']}")
        print(f"    Enriched NAS File : {enr.get('mapped_path')}")
        print(f"    Files Updated     : .po={res['po_written']} | .an={res['an_written']} ({res['an_inner_replacements']} inner refs)")
        print()

    return 0


def run_status(args: argparse.Namespace) -> int:
    calibre_dir = Path(args.calibre_dir)
    staging_dir = get_staging_dir(calibre_dir, args.staging_dir)
    manifest_path = staging_dir / "staging_manifest.json"
    if not manifest_path.exists():
        print(f"No staging manifest found at {manifest_path}. Run `pass1` first.")
        return 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = manifest.get("items", [])
    done = sum(1 for x in items if x.get("pass2_completed"))
    with_moon = sum(1 for x in items if x.get("moon_outer_name"))
    print("=" * 80)
    print(f" STAGING MANIFEST STATUS: {manifest_path}")
    print("=" * 80)
    print(f" Created At         : {manifest.get('created_at')}")
    print(f" Total Staged Books : {len(items):,}")
    print(f" With Moon+ Cache   : {with_moon:,}")
    print(f" Pass 2 Completed   : {done:,} / {len(items):,}")
    print(f" Pending Pass 2     : {len(items) - done:,}")
    print("=" * 80)
    return 0


def main() -> int:
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        prog="calibre_moon_sync",
        description="Two-pass Calibre Library & Moon+ Reader WebDAV Enrichment Sync Utility.",
    )
    parser.add_argument(
        "mode",
        nargs="?",
        default="pass1",
        choices=["pass1", "pass2", "status", "moon-only", "inspect-an"],
        help="Workflow stage to run: pass1 (identify/stage/tag), pass2 (delete/re-add/update bookmarks), status, moon-only, or inspect-an",
    )
    parser.add_argument("pattern", nargs="?", default="", help="Filename pattern when using `inspect-an`")
    parser.add_argument("--url", default=DEFAULT_SERVER_URL, help=f"MyLibrary base URL (default: {DEFAULT_SERVER_URL})")
    parser.add_argument("--calibre-dir", default=DEFAULT_CALIBRE_DIR, help=f"Local Calibre Library path (default: {DEFAULT_CALIBRE_DIR})")
    parser.add_argument("--moon-cache", default=DEFAULT_MOON_CACHE_DIR, help=f"Moon+ Reader WebDAV Cache path (default: {DEFAULT_MOON_CACHE_DIR})")
    parser.add_argument("--nas-prefix", default=DEFAULT_NAS_PREFIX, help=f"Mapped NAS drive prefix for /books (default: {DEFAULT_NAS_PREFIX})")
    parser.add_argument("--staging-dir", default=None, help="Optional custom directory for staging_manifest.json and backups")
    parser.add_argument("--calibredb", default=None, help="Optional explicit path to calibredb.exe")
    parser.add_argument("--delete-from-calibre", action="store_true", help="In Pass 1, immediately delete matched books from Calibre instead of tagging them first")
    parser.add_argument("--apply", action="store_true", help="Execute changes (without --apply, runs in safe DRY-RUN preview mode)")
    parser.add_argument("--batch-size", type=int, default=400, help="Batch size for API calls (default: 400)")

    args = parser.parse_args()

    if args.mode == "inspect-an":
        moon_cache_dir = Path(args.moon_cache)
        pat = (args.pattern or "").lower()
        for p in sorted(moon_cache_dir.glob("*.an")):
            if pat in p.name.lower():
                print("=" * 80)
                print(f" FILE: {p.name}")
                print("=" * 80)
                print(decompress_moon_an(p))
        return 0
    if args.mode == "status":
        return run_status(args)
    if args.mode == "moon-only":
        return run_moon_only(args)
    if args.mode == "pass2":
        return run_pass2(args)
    return run_pass1(args)


if __name__ == "__main__":
    sys.exit(main())
