#!/usr/bin/env python3
"""
MyLibrary CLI & Python Client SDK (`mylibrary_cli.py`)
======================================================
Command-line interface and programmatic client for all functions in MyLibrary:
  - Catalog Search, Filtering, Autocomplete & Book Details (1.07M+ items)
  - Metadata Editing in BookDB
  - Live Pipeline, Scanner Schedule & Queue Status (Remaining to Catalog / Enrich)
  - epubmeta Enrichment Explorer (`NEEDS_REVIEW`, `QUARANTINED`, `NO_ISBN`, `SUCCESS`, `FAILED`, `IGNORED`)
  - Interactive / Mix-and-Match Field Approval, Keep Original (`ignore`), and Re-queue (`requeue`)
  - Time-Series Enrichment History (Hourly / Daily curves & terminal bar charts)
  - Live Scanner & epubmeta Log Tailing

Environment Variables:
  - MYLIBRARY_URL         (default: http://192.168.1.43:8080/mylibrary)
  - MYLIBRARY_PATH_PREFIX (default: U:\\Media)
  - MYLIBRARY_API_TOKEN   (optional: for direct scanner sync endpoints)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

DEFAULT_BASE_URL = os.environ.get("MYLIBRARY_URL", "http://192.168.1.43:8080/mylibrary").rstrip("/")
DEFAULT_PATH_PREFIX = os.environ.get("MYLIBRARY_PATH_PREFIX", r"U:\Media")


def map_container_path(container_path: str, prefix: str = DEFAULT_PATH_PREFIX) -> str:
    """Convert container path `/books/...` to mapped host path `U:\\Media\\books\\...`."""
    if not container_path:
        return ""
    if not prefix or not container_path.startswith("/"):
        return container_path
    clean_prefix = prefix.rstrip("/\\")
    sep = "\\" if ("\\" in clean_prefix or ":" in clean_prefix) else "/"
    rel = container_path.lstrip("/").replace("/", sep).replace("\\", sep)
    return f"{clean_prefix}{sep}{rel}"


def normalize_to_container_path(user_path: str, prefix: str = DEFAULT_PATH_PREFIX) -> str:
    """Allow passing either `/books/...` or `U:\\Media\\books\\...` to CLI/MCP actions."""
    p = (user_path or "").strip()
    if not p:
        return ""
    if p.startswith("/"):
        return p
    clean_prefix = (prefix or "").rstrip("/\\")
    if clean_prefix and p.lower().startswith(clean_prefix.lower()):
        rel = p[len(clean_prefix):].lstrip("/\\").replace("\\", "/")
        return f"/{rel}"
    return p.replace("\\", "/")


class MyLibraryClient:
    """Zero-dependency HTTP client for the MyLibrary REST API."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        path_prefix: str = DEFAULT_PATH_PREFIX,
        api_token: Optional[str] = None,
        timeout: int = 30,
    ):
        self.base_url = base_url.rstrip("/")
        self.path_prefix = path_prefix
        self.api_token = api_token or os.environ.get("MYLIBRARY_API_TOKEN", "")
        self.timeout = timeout

    def _request(
        self,
        path: str,
        method: str = "GET",
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        if params:
            clean_params: List[tuple[str, str]] = []
            for k, v in params.items():
                if v is None or v == "":
                    continue
                if isinstance(v, (list, tuple)):
                    for item in v:
                        if item is not None and item != "":
                            clean_params.append((k, str(item)))
                else:
                    clean_params.append((k, str(v)))
            if clean_params:
                url = f"{url}?{urllib.parse.urlencode(clean_params)}"

        headers = {"Accept": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"

        data_bytes = None
        if json_body is not None:
            headers["Content-Type"] = "application/json"
            data_bytes = json.dumps(json_body).encode("utf-8")

        req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            try:
                err_json = json.loads(err_body)
                msg = err_json.get("error") or err_json.get("detail") or err_body
            except Exception:
                msg = err_body or str(e)
            raise RuntimeError(f"HTTP {e.code} from {url}: {msg}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Connection error to {url}: {e.reason}") from e

    # --- Status & Pipeline ---
    def get_status(self) -> Dict[str, Any]:
        return self._request("/api/status/")

    # --- Catalog Search, Suggest & Detail ---
    def search_catalog(
        self,
        query: str = "",
        item_type: Optional[str] = None,
        file_format: Optional[str] = None,
        isbn: Optional[str] = None,
        author: Optional[str] = None,
        limit: int = 25,
        offset: int = 0,
        include_cover: bool = False,
    ) -> Dict[str, Any]:
        return self._request(
            "/api/catalog/",
            params={
                "q": query,
                "type": item_type,
                "format": file_format,
                "isbn": isbn,
                "author": author,
                "limit": limit,
                "offset": offset,
                "path_prefix": self.path_prefix,
                "include_cover": "true" if include_cover else "false",
            },
        )

    def get_book(self, identifier: str, include_cover: bool = False) -> Dict[str, Any]:
        norm_id = normalize_to_container_path(identifier, self.path_prefix) if ("\\" in identifier or identifier.startswith("/")) else identifier
        enc = urllib.parse.quote(norm_id, safe="")
        return self._request(
            f"/api/catalog/{enc}/",
            params={
                "path_prefix": self.path_prefix,
                "include_cover": "true" if include_cover else "false",
            },
        )

    def update_book(self, identifier: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        enc = urllib.parse.quote(identifier.strip(), safe="")
        return self._request(f"/api/catalog/{enc}/", method="PATCH", json_body=updates)

    def suggest(self, query: str) -> List[Dict[str, Any]]:
        return self._request("/api/suggest/", params={"q": query})

    # --- epubmeta Enrichments & Review Workflow ---
    def list_enrichments(
        self,
        status: str = "ALL",
        search: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        res = self._request(
            "/api/status/items/",
            params={
                "status": status.upper(),
                "q": search,
                "limit": limit,
                "offset": offset,
            },
        )
        for item in res.get("items", []):
            item["mapped_path"] = map_container_path(item.get("file_path", ""), self.path_prefix)
        return res

    def approve_enrichment(
        self,
        file_path: str,
        selected_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        container_path = normalize_to_container_path(file_path, self.path_prefix)
        body: Dict[str, Any] = {"action": "approve", "file_path": container_path}
        if selected_metadata:
            body["selected_metadata"] = selected_metadata
        return self._request("/api/status/items/", method="POST", json_body=body)

    def ignore_enrichment(self, file_path: str) -> Dict[str, Any]:
        container_path = normalize_to_container_path(file_path, self.path_prefix)
        return self._request(
            "/api/status/items/",
            method="POST",
            json_body={"action": "ignore", "file_path": container_path},
        )

    def requeue_enrichment(self, file_path: str) -> Dict[str, Any]:
        container_path = normalize_to_container_path(file_path, self.path_prefix)
        return self._request(
            "/api/status/items/",
            method="POST",
            json_body={"action": "requeue", "file_path": container_path},
        )

    def fix_switched_batch(
        self,
        action: str = "start",
        dry_run: bool = False,
        limit: int = 0,
        only_likely: bool = False,
    ) -> Dict[str, Any]:
        api_action = (
            "stop_fix_switched"
            if action == "stop"
            else ("fix_switched_status" if action == "status" else "batch_fix_switched")
        )
        return self._request(
            "/api/enrich/",
            method="POST",
            json_body={
                "action": api_action,
                "sub_action": action,
                "dry_run": bool(dry_run),
                "limit": int(limit or 0),
                "only_likely": bool(only_likely),
            },
        )

    # --- Time-Series History & Logs ---
    def get_history(self, granularity: str = "hour") -> Dict[str, Any]:
        return self._request("/api/status/history/", params={"granularity": granularity})

    def get_logs(
        self,
        source: str = "both",
        lines: int = 60,
        grep: str = "",
    ) -> Dict[str, Any]:
        return self._request(
            "/api/logs/",
            params={"source": source, "lines": lines, "grep": grep},
        )


# ==============================================================================
# CLI Output Formatting Helpers
# ==============================================================================

def _print_status(data: Dict[str, Any]) -> None:
    sc = data.get("scanner", {})
    cat = data.get("catalog", {})
    conv = data.get("conversion", {})
    counts = conv.get("status_counts", {})

    state_str = "ACTIVE (Scanning)" if sc.get("is_scanning") else ("STANDBY" if sc.get("daemon_active") else "IDLE")
    print("=" * 76)
    print(f" MYLIBRARY PIPELINE & CATALOG STATUS   [{data.get('generated_at', '')}]")
    print("=" * 76)
    print(f" Scanner State   : {state_str}")
    print(f" Activity        : {sc.get('current_activity', '')}")
    print(f" Next Scan       : {sc.get('next_scan_at', 'N/A')} ({sc.get('next_scan_countdown', '—')}) | Interval: {sc.get('scan_interval_hours', 4)}h")
    print(f" Last Log Event  : {sc.get('last_log_update', 'N/A')}")
    print("-" * 76)
    print(" CATALOG & IMPORT METRICS (BookDB + SQLite)")
    print(f"   Catalog Items        : {cat.get('total_items', 0):,}")
    print(f"   Physical Files       : {cat.get('total_files', 0):,} (Valid: {cat.get('valid_files', 0):,} | DRM: {cat.get('drm_files', 0):,} | Invalid: {cat.get('invalid_files', 0):,})")
    print(f"   Tracked File States  : {conv.get('tracked_files_count', 0):,} (Discovered on NAS: {conv.get('discovered_files_total', 0):,})")
    print(f"   Remaining to Catalog : {conv.get('remaining_to_catalog', 0):,} {conv.get('catalog_sync_chunk_info', '')}")
    print(f"   Local Metadata Cache : {conv.get('metadata_cache_count', 0):,}")
    print(f"   Rich Metadata Cover  : {cat.get('complete_metadata_pct', 0)}% complete | ISBN: {cat.get('with_isbn', 0):,} | Covers: {cat.get('with_cover', 0):,}")
    print("-" * 76)
    print(" EPUBMETA ENRICHMENT QUEUE")
    print(f"   Total Processed      : {conv.get('total_processed', 0):,} (Success Rate: {conv.get('success_rate', 0)}%)")
    print(f"     - Enriched (OK)    : {counts.get('SUCCESS', 0):,}")
    print(f"     - Needs Review     : {counts.get('NEEDS_REVIEW', 0):,}")
    print(f"     - No ISBN (As-Is)  : {counts.get('NO_ISBN', 0):,}")
    print(f"     - Quarantined      : {counts.get('QUARANTINED', 0):,}")
    print(f"     - Failed / Ignored : {counts.get('FAILED', 0):,} / {counts.get('IGNORED', 0):,}")
    print(f"   Remaining to Enrich  : {conv.get('remaining_to_enrich', 0):,}")
    print(f"   Current Cycle Batch  : {conv.get('cycle_net_used', 0)} / {conv.get('cycle_net_cap', 2500)} lookups ({conv.get('cycle_net_remaining', 0)} left in cycle | {conv.get('cycle_inspected', 0)} inspected)")
    print(f"   Complete in BookDB   : {conv.get('skipped_complete_bookdb', 0):,} (auto-skipped)")
    print("=" * 76)


def _print_search_results(data: Dict[str, Any]) -> None:
    items = data.get("items", [])
    total = data.get("total", 0)
    offset = data.get("offset", 0)
    print(f"Found {total:,} catalog items (showing {offset + 1}..{offset + len(items)}):")
    print("-" * 90)
    for idx, it in enumerate(items, start=offset + 1):
        isbn_str = f" | ISBN: {it['isbn']}" if it.get("isbn") else (f" | ASIN: {it['asin']}" if it.get("asin") else "")
        pub_str = f" | {it['publisher']}" if it.get("publisher") else ""
        print(f"[{idx}] {it.get('title')} — {it.get('authors')} ({it.get('item_type')}){isbn_str}{pub_str}")
        print(f"    ID: {it.get('id')}")
        for f in it.get("files", []):
            flags = []
            if f.get("has_drm"):
                flags.append("DRM")
            if f.get("is_invalid"):
                flags.append("INVALID")
            flag_str = f" [{'/'.join(flags)}]" if flags else ""
            print(f"    File [{f.get('file_format')} {f.get('file_size_mb')} MB]{flag_str}: {f.get('mapped_path')}")
        print()


def _print_book_detail(data: Dict[str, Any]) -> None:
    it = data.get("item", {})
    print("=" * 84)
    print(f" {it.get('title')}")
    print("=" * 84)
    print(f" ID        : {it.get('id')}")
    print(f" Authors   : {it.get('authors')}")
    print(f" Publisher : {it.get('publisher') or '—'}")
    print(f" Pub Date  : {it.get('pub_date') or '—'}")
    print(f" ISBN/ASIN : {it.get('isbn') or '—'} / {it.get('asin') or '—'}")
    print(f" Type      : {it.get('item_type')} | Has Cover: {it.get('has_cover')}")
    if it.get("description"):
        print("-" * 84)
        print(" Description:")
        print(f"   {it['description'][:1200]}")
    print("-" * 84)
    print(f" Physical Files ({len(it.get('files', []))}):")
    for f in it.get("files", []):
        print(f"   • [{f.get('file_format')}] {f.get('mapped_path')} ({f.get('file_size_mb')} MB)")
        print(f"     Container Path : {f.get('file_path')}")
        print(f"     SHA-256        : {f.get('file_hash')}")
    if it.get("enrichment_records"):
        print("-" * 84)
        print(" epubmeta Enrichment History:")
        for rec in it["enrichment_records"]:
            print(f"   • [{rec.get('status')}] {rec.get('updated_at')} | Confidence: {rec.get('match_confidence')}%")
            print(f"     Before: {rec.get('orig_title') or '—'} by {rec.get('orig_author') or '—'} (ISBN: {rec.get('orig_isbn') or '—'})")
            print(f"     After : {rec.get('title') or '—'} by {rec.get('author') or '—'} (ISBN: {rec.get('isbn') or '—'})")
            if rec.get("error_message"):
                print(f"     Note  : {rec.get('error_message')}")
    print("=" * 84)


def _print_enrichments(data: Dict[str, Any]) -> None:
    items = data.get("items", [])
    total = data.get("total", 0)
    offset = data.get("offset", 0)
    st = data.get("status", "ALL")
    print(f"epubmeta Enrichments [Filter: {st}] — Total: {total:,} (showing {offset + 1}..{offset + len(items)}):")
    print("-" * 95)
    for idx, item in enumerate(items, start=offset + 1):
        conf = f"{item['match_confidence']}%" if item.get("match_confidence") is not None else "—"
        print(f"[{idx}] [{item.get('status')}] (Conf: {conf}) {item.get('file_name')}  [{item.get('updated_at')}]")
        print(f"    Mapped Path    : {item.get('mapped_path')}")
        print(f"    Container Path : {item.get('file_path')}")
        print(f"    BEFORE : Title='{item.get('orig_title') or '—'}' | Author='{item.get('orig_author') or '—'}' | Pub='{item.get('orig_publisher') or '—'}' | ISBN={item.get('orig_isbn') or '—'}")
        print(f"    AFTER  : Title='{item.get('title') or '—'}' | Author='{item.get('author') or '—'}' | Pub='{item.get('publisher') or '—'}' | ISBN={item.get('isbn') or '—'}")
        if item.get("error_message"):
            print(f"    Note   : {item.get('error_message')}")
        print()


def _print_history(data: Dict[str, Any], series: str = "ALL") -> None:
    labels = data.get("labels", [])
    per_period = data.get("per_period", {}).get(series, [])
    cumulative = data.get("cumulative", {}).get(series, [])
    gran = data.get("granularity", "hour")
    if not labels:
        print("No history buckets recorded yet.")
        return

    max_rate = max(per_period) if per_period else 1
    print(f"Enrichment History Over Time (Series: {series}, Granularity: {gran})")
    print("-" * 80)
    print(f"{'Bucket':<18} | {'Rate':>8} | {'Cumulative':>10} | Rate Chart")
    print("-" * 80)
    for lbl, rate, cum in zip(labels, per_period, cumulative):
        bar_len = int((rate / max_rate) * 32) if max_rate > 0 else 0
        bar = "#" * bar_len
        print(f"{lbl:<18} | {rate:>8,} | {cum:>10,} | {bar}")
    print("-" * 80)


# ==============================================================================
# Main CLI Entrypoint
# ==============================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mylibrary",
        description="CLI for MyLibrary Catalog Search, Pipeline Status, and epubmeta Enrichments.",
    )
    parser.add_argument("--url", default=DEFAULT_BASE_URL, help=f"MyLibrary base URL (default: {DEFAULT_BASE_URL})")
    parser.add_argument("--prefix", default=DEFAULT_PATH_PREFIX, help=f"Local/mapped path prefix for NAS files (default: {DEFAULT_PATH_PREFIX})")
    parser.add_argument("--json", action="store_true", help="Output raw JSON response")

    sub = parser.add_subparsers(dest="command", required=True)

    # 1. status
    sub.add_parser("status", help="Show live pipeline status, next scan countdown, and remaining catalog/enrichment counts")

    # 2. search
    p_search = sub.add_parser("search", help="Search and filter the BookDB catalog")
    p_search.add_argument("query", nargs="?", default="", help="Search query (title, author, ISBN, description)")
    p_search.add_argument("--type", dest="item_type", help="Filter by type: BOOK, MAGAZINE, KINDLE, AUDIBLE, AUDIOBOOK")
    p_search.add_argument("--format", dest="file_format", help="Filter by file format: EPUB, PDF, MOBI, AZW3, MP3, M4B")
    p_search.add_argument("--isbn", help="Filter by exact ISBN or ASIN")
    p_search.add_argument("--author", help="Filter by author name")
    p_search.add_argument("--limit", type=int, default=20, help="Number of results (default: 20, max: 200)")
    p_search.add_argument("--offset", type=int, default=0, help="Pagination offset (default: 0)")

    # 3. book
    p_book = sub.add_parser("book", help="Get full details, file locations, and enrichment history for a book")
    p_book.add_argument("identifier", help="Book UUID, ISBN, ASIN, SHA-256 hash, or file path")

    # 4. update-book
    p_upd = sub.add_parser("update-book", help="Update a catalog item's metadata in BookDB")
    p_upd.add_argument("identifier", help="Book UUID, ISBN, or ASIN")
    p_upd.add_argument("--title", help="New title")
    p_upd.add_argument("--authors", help="New author(s)")
    p_upd.add_argument("--publisher", help="New publisher")
    p_upd.add_argument("--isbn", help="New ISBN")
    p_upd.add_argument("--asin", help="New ASIN")
    p_upd.add_argument("--description", help="New description/synopsis")

    # 5. suggest
    p_sug = sub.add_parser("suggest", help="Get fast autocomplete suggestions for a title/author prefix")
    p_sug.add_argument("query", help="Prefix string (at least 2 characters)")

    # 6. enrichments
    p_enr = sub.add_parser("enrichments", help="List and search epubmeta conversion/enrichment items")
    p_enr.add_argument(
        "--status",
        default="NEEDS_REVIEW",
        choices=["ALL", "SUCCESS", "NEEDS_REVIEW", "NO_ISBN", "QUARANTINED", "FAILED", "IGNORED"],
        help="Status filter (default: NEEDS_REVIEW)",
    )
    p_enr.add_argument("--search", "-q", default="", help="Search filename, title, author, ISBN, or error note")
    p_enr.add_argument("--limit", type=int, default=25, help="Max items to return (default: 25)")
    p_enr.add_argument("--offset", type=int, default=0, help="Pagination offset (default: 0)")

    # 7. approve
    p_app = sub.add_parser(
        "approve",
        help="Approve a NEEDS_REVIEW or NO_ISBN enrichment item (supports full approval or mix-and-match field overrides)",
    )
    p_app.add_argument("file_path", help="Container path (/books/...) or mapped path (U:\\Media\\books\\...)")
    p_app.add_argument("--title", help="Override title to save")
    p_app.add_argument("--author", help="Override author to save")
    p_app.add_argument("--publisher", help="Override publisher to save")
    p_app.add_argument("--isbn", help="Override ISBN to save")
    p_app.add_argument("--description", help="Override description to save")
    p_app.add_argument("--cover", help="Override cover URL/data URI to save")

    # 8. ignore
    p_ign = sub.add_parser("ignore", help="Keep original metadata and mark item as IGNORED")
    p_ign.add_argument("file_path", help="Container path (/books/...) or mapped path (U:\\Media\\books\\...)")

    # 9. requeue
    p_req = sub.add_parser("requeue", help="Re-queue a file in scanner_state.db for the next epubmeta scan cycle")
    p_req.add_argument("file_path", help="Container path (/books/...) or mapped path (U:\\Media\\books\\...)")

    # 10. history
    p_hist = sub.add_parser("history", help="View hourly or daily enrichment history over time")
    p_hist.add_argument("--granularity", choices=["hour", "day"], default="hour", help="Time bucket granularity")
    p_hist.add_argument(
        "--series",
        choices=["ALL", "SUCCESS", "NEEDS_REVIEW", "NO_ISBN", "QUARANTINED", "FAILED", "IGNORED", "CACHE"],
        default="ALL",
        help="Metric series to chart",
    )

    # 11. logs
    p_logs = sub.add_parser("logs", help="Tail live scanner.log and epubmeta.log")
    p_logs.add_argument("--source", choices=["scanner", "epubmeta", "both"], default="both", help="Log source")
    p_logs.add_argument("--lines", type=int, default=50, help="Number of lines to tail (default: 50)")
    p_logs.add_argument("--grep", default="", help="Optional case-insensitive filter keyword")

    # 11b. fix-switched
    p_sw = sub.add_parser(
        "fix-switched",
        help="Batch check No Match items for switched Title/Author and enrich/fix verified matches",
    )
    p_sw.add_argument("--dry-run", action="store_true", help="Only check & report matches without modifying files/DB")
    p_sw.add_argument("--limit", type=int, default=0, help="Optional max candidates to check (0 = all)")
    p_sw.add_argument("--only-likely", action="store_true", help="Only check high-probability switched candidates")
    p_sw.add_argument("--status", action="store_true", help="Check current/last batch status without starting a new run")
    p_sw.add_argument("--stop", action="store_true", help="Request a running batch to stop cleanly")
    p_sw.add_argument("--wait", action="store_true", help="Poll and display live progress until batch finishes")

    # 12. help
    sub.add_parser("help", help="Show complete CLI, REST API, and MCP Server guide with examples")

    return parser


def _print_detailed_help() -> None:
    print(
        """================================================================================
 MYLIBRARY CLI, REST API & MCP SERVER — COMPLETE QUICK REFERENCE
================================================================================

1. GLOBAL OPTIONS & ENVIRONMENT VARIABLES
   --url <URL>         Base URL (default: $MYLIBRARY_URL or http://192.168.1.43:8080/mylibrary)
   --prefix <PATH>     Local NAS path prefix (default: $MYLIBRARY_PATH_PREFIX or U:\\Media)
   --json              Output raw JSON on any command for scripting

2. CLI COMMAND EXAMPLES
   • Pipeline, Next Scan Countdown & Remaining Queues:
     python mylibrary_cli.py status

   • Search the 1.15M+ BookDB Catalog:
     python mylibrary_cli.py search "Dune" --type BOOK --format EPUB --limit 10

   • Inspect a Single Book (by UUID, ISBN, ASIN, SHA-256 hash, or path):
     python mylibrary_cli.py book 29b1bc0f-e3f3-4b68-82c3-33549aed7a96

   • Update Book Metadata in BookDB:
     python mylibrary_cli.py update-book <UUID_or_ISBN> --title "..." --authors "..." --isbn "..."

   • Autocomplete Title / Author Suggestions:
     python mylibrary_cli.py suggest "Asimov"

   • List / Search epubmeta Enrichments (ALL, SUCCESS, NEEDS_REVIEW, NO_ISBN, QUARANTINED, FAILED, IGNORED):
     python mylibrary_cli.py enrichments --status NEEDS_REVIEW --limit 20
     python mylibrary_cli.py enrichments --status NO_ISBN --search "Lumley"

   • Approve an Enrichment (Full Candidate or Mix-and-Match Field Overrides):
     python mylibrary_cli.py approve "U:\\Media\\books\\...\\Book.epub" --title "Custom Title" --isbn "978..."

   • Keep Original Metadata (Mark IGNORED) or Re-queue for Next Scan Cycle:
     python mylibrary_cli.py ignore "U:\\Media\\books\\...\\Book.epub"
     python mylibrary_cli.py requeue "/books/.../Book.epub"

   • Time-Series Enrichment History & Terminal Bar Chart:
     python mylibrary_cli.py history --granularity hour --series SUCCESS

   • Tail Live Scanner & epubmeta Logs:
     python mylibrary_cli.py logs --source both --lines 50 --grep "SUCCESS"

3. REST API, OPDS 1.2 & CALIBRE CONTENT SERVER ENDPOINTS
   GET          /mylibrary/opds/                       OPDS 1.2 Root Catalog (Moon+ Reader / KOReader)
   GET          /mylibrary/opds/enriched/              Enriched EPUBs shelf (`epubmeta` verified)
   GET          /mylibrary/opds/search/?q=<query>      OpenSearch acquisition feed
   GET          /mylibrary/opds/download/<id>/<file>   Stream book + auto-migrate Moon+ WebDAV bookmarks
   GET          /mylibrary/opds/cover/<id>.jpg         Stream book cover artwork
   GET          /mylibrary/opds/ajax/books             Calibre Content Server JSON emulation
   GET|POST     /mylibrary/login/                      External web login page (auto-triggered outside LAN)
   GET          /mylibrary/api/status/                 Live pipeline, schedule & queue metrics
   GET          /mylibrary/api/catalog/                Search & filter catalog (?q=&type=&format=&isbn=&author=)
   GET|PATCH    /mylibrary/api/catalog/<identifier>/   Book detail, physical files, enrichment history & update
   POST         /mylibrary/api/calibre/check-enriched/ Batch SHA-256/ISBN/Moon+ enrichment lookup
   GET          /mylibrary/api/suggest/?q=<prefix>     Fast autocomplete suggestions
   GET|POST     /mylibrary/api/status/items/           Enrichment explorer + approve / ignore / requeue actions
   GET          /mylibrary/api/status/history/         Hourly/daily time-series (?granularity=hour|day)
   GET          /mylibrary/api/logs/                   Tail scanner.log & epubmeta.log (?source=both&lines=60)
   POST         /mylibrary/api/enrich/                 On-demand epubmeta enrichment for a book or file
   POST         /mylibrary/api/wishlist/check/         Batch/single book check for Chrome Extension (Wishlists & Book Sites)
   GET|POST     /mylibrary/api/duplicates/             Enriched Duplicates Manager scan & resolution API
   POST         /mylibrary/api/interesting/toggle/     Toggle ☆ Interesting book status
   POST         /mylibrary/api/bad-metadata/toggle/    Toggle ⚑ Bad Metadata status
   GET|POST     /mylibrary/api/cover-search/           Multi-source web cover search & apply
   GET|POST     /mylibrary/api/book-preview/           Inside-book page/cover preview & Gemini AI Vision re-evaluation

4. CALIBRE & MOON+ READER SYNC UTILITY (`calibre_moon_sync.py`)
   python calibre_moon_sync.py moon-only --apply       Migrate Moon+ WebDAV .po & .an bookmark filenames
   python calibre_moon_sync.py pass1 --apply           Pass 1: Snapshot Moon+ bookmarks & tag books in Calibre
   python calibre_moon_sync.py pass2 --apply           Pass 2: Replace Calibre books & write updated Moon+ bookmarks
   python calibre_moon_sync.py inspect-an "Death_s End"

5. MODEL CONTEXT PROTOCOL (MCP) SERVER (`mylibrary_mcp.py`)
   Add to `mcp_config.json` / `claude_desktop_config.json`:
   {
     "mcpServers": {
       "mylibrary": {
         "command": "python",
         "args": ["c:\\\\Users\\\\oscar\\\\Repo_Pers\\\\notgithub\\\\MyLibrary\\\\mylibrary_mcp.py"],
         "env": {
           "MYLIBRARY_URL": "http://192.168.1.43:8080/mylibrary",
           "MYLIBRARY_PATH_PREFIX": "U:\\\\Media"
         }
       }
     }
   }
================================================================================"""
    )


def main(argv: Optional[List[str]] = None) -> int:
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    client = MyLibraryClient(base_url=args.url, path_prefix=args.prefix)

    try:
        if args.command == "help":
            _print_detailed_help()
            return 0

        if args.command == "status":
            res = client.get_status()
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                _print_status(res)

        elif args.command == "search":
            res = client.search_catalog(
                query=args.query,
                item_type=args.item_type,
                file_format=args.file_format,
                isbn=args.isbn,
                author=args.author,
                limit=args.limit,
                offset=args.offset,
            )
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                _print_search_results(res)

        elif args.command == "book":
            res = client.get_book(args.identifier)
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                _print_book_detail(res)

        elif args.command == "update-book":
            updates = {}
            for k in ("title", "authors", "publisher", "isbn", "asin", "description"):
                val = getattr(args, k, None)
                if val is not None:
                    updates[k] = val
            res = client.update_book(args.identifier, updates)
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                print(f"Updated book {res['item']['id']} fields: {', '.join(res.get('updated_fields', []))}")

        elif args.command == "suggest":
            res = client.suggest(args.query)
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                for s in res:
                    print(f"• {s.get('title')} — {s.get('authors')} ({s.get('type')})")

        elif args.command == "enrichments":
            res = client.list_enrichments(
                status=args.status,
                search=args.search,
                limit=args.limit,
                offset=args.offset,
            )
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                _print_enrichments(res)

        elif args.command == "approve":
            sel = {}
            for k in ("title", "author", "publisher", "isbn", "description", "cover"):
                val = getattr(args, k, None)
                if val is not None:
                    sel[k] = val
            res = client.approve_enrichment(args.file_path, selected_metadata=sel if sel else None)
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                print(f"Approved: {res.get('file_path')} -> status={res.get('status')} (title='{res.get('title')}', author='{res.get('author')}', isbn='{res.get('isbn')}')")

        elif args.command == "ignore":
            res = client.ignore_enrichment(args.file_path)
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                print(f"Marked IGNORED (keeping original): {res.get('file_path')}")

        elif args.command == "requeue":
            res = client.requeue_enrichment(args.file_path)
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                print(f"Re-queued for next scan cycle: {res.get('file_path')} (deleted={res.get('deleted')})")

        elif args.command == "history":
            res = client.get_history(granularity=args.granularity)
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                _print_history(res, series=args.series)

        elif args.command == "logs":
            res = client.get_logs(source=args.source, lines=args.lines, grep=args.grep)
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                if "scanner_log" in res:
                    print("=== scanner.log ===")
                    for ln in res["scanner_log"]:
                        print(ln)
                if "epubmeta_log" in res:
                    if "scanner_log" in res:
                        print()
                    print("=== epubmeta.log ===")
                    for ln in res["epubmeta_log"]:
                        print(ln)

        elif args.command == "fix-switched":
            import time as _time_sw
            sub_act = "stop" if args.stop else ("status" if args.status else "start")
            res = client.fix_switched_batch(
                action=sub_act,
                dry_run=args.dry_run,
                limit=args.limit,
                only_likely=args.only_likely,
            )
            if args.wait and not args.status and not args.stop:
                while res.get("running"):
                    checked = res.get("checked", 0)
                    tot = res.get("candidates_total", 0)
                    fixed = res.get("matched_count" if res.get("dry_run") else "fixed_count", 0)
                    cur_it = res.get("current_item", "")
                    if not args.json:
                        print(
                            f"\r[Switched Batch] Checked {checked}/{tot} | Fixed/Matched: {fixed} | {cur_it[:55]:<55}",
                            end="",
                            flush=True,
                        )
                    _time_sw.sleep(2.0)
                    res = client.fix_switched_batch(action="status")
                if not args.json:
                    print()
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                mode_lbl = "DRY RUN" if res.get("dry_run") else "LIVE"
                state_lbl = "RUNNING" if res.get("running") else ("FINISHED" if res.get("finished") else "IDLE")
                print(f"Switched Title/Author Batch [{mode_lbl}] — State: {state_lbl}")
                print(f"  Candidates : {res.get('checked', 0):,} / {res.get('candidates_total', 0):,} checked ({res.get('likely_switched_total', 0):,} high-probability)")
                print(f"  Matched    : {res.get('matched_count', 0):,} | Fixed: {res.get('fixed_count', 0):,}")
                if res.get("current_item"):
                    print(f"  Note       : {res.get('current_item')}")
                for fx in (res.get("recent_fixes") or [])[:15]:
                    print(
                        f"  ✓ '{fx.get('orig_title')}' by '{fx.get('orig_authors')}'  ==>  "
                        f"'{fx.get('new_title')}' by {fx.get('new_authors')} ({fx.get('confidence', 0)}%)"
                    )

        return 0
    except Exception as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
