"""Unified single-pass integration between epubmeta and MyLibrary's sync_files.py scanner."""

from __future__ import annotations
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .exceptions import RateLimitExceededError
from .external_dbs import (
    MyLibraryClient,
    ScannerStateBridge,
    seed_cache_from_calibre_db,
)
from .isbn.ai_extractor import GeminiBookExtractor
from .processor import BookProcessor
from .providers.aggregator import MetadataAggregator
from .providers.amazon_goodreads import AmazonGoodreadsProvider
from .providers.google_books import GoogleBooksProvider
from .providers.open_library import OpenLibraryProvider
from .readers import SUPPORTED_EXTENSIONS
from .state import StateTracker
from .throttler import Throttler

logger = logging.getLogger(__name__)


def _ensure_file_logger(log_file_path: Optional[str]) -> None:
    if not log_file_path:
        return
    try:
        lp = Path(log_file_path)
        lp.parent.mkdir(parents=True, exist_ok=True)
        root_logger = logging.getLogger("epubmeta")
        root_logger.setLevel(logging.INFO)
        abs_lp = str(lp.resolve())
        has_handler = any(
            isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", None) == abs_lp
            for h in root_logger.handlers
        )
        if not has_handler:
            fh = logging.FileHandler(lp, encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
            root_logger.addHandler(fh)
    except Exception as e:
        logger.debug("Could not attach epubmeta file logger (%s): %s", log_file_path, e)


class EnrichmentSession:
    """
    Coordinates single-pass metadata enrichment inside MyLibrary's `sync_files.py`.
    Consolidates state tracking into `scanner_state.db` and avoids a second directory walk.
    """

    def __init__(
        self,
        scanner_db_path: str | Path,
        server_url: str,
        api_token: str,
        epubmeta_config: Optional[Dict[str, Any]] = None,
        calibre_libraries: Optional[List[Dict[str, Any]]] = None,
    ):
        self.cfg = epubmeta_config or {}
        self.enabled = bool(self.cfg.get("enabled", False))
        self.scanner_db_path = Path(scanner_db_path).resolve()
        self.server_url = server_url
        self.api_token = api_token

        self.ignore_pdfs = bool(self.cfg.get("ignore_pdfs", True))
        self.pdf_only = bool(self.cfg.get("pdf_only", False))
        self.pdf_require_isbn = bool(self.cfg.get("pdf_require_isbn", True))
        self.skip_complete = bool(self.cfg.get("skip_complete", True))
        self.retry_failed = bool(self.cfg.get("retry_failed", False))
        self.backup = bool(self.cfg.get("backup", True))
        self.update_cover = bool(self.cfg.get("update_cover", False))
        self.min_confidence = float(self.cfg.get("min_confidence", 0.50))
        self.max_files: Optional[int] = (
            int(self.cfg["max_files"]) if self.cfg.get("max_files") else None
        )
        self.delay = float(self.cfg.get("delay", 1.0))
        self.ai_delay = float(self.cfg.get("ai_delay", 4.0))
        self.batch_size = int(self.cfg.get("batch_size", 5))
        self.batch_pause = float(self.cfg.get("batch_pause", 30.0))

        self.allowed_exts: Set[str] = set(SUPPORTED_EXTENSIONS)
        if self.ignore_pdfs:
            self.allowed_exts.discard(".pdf")
        if self.pdf_only:
            self.allowed_exts = {".pdf"}

        self.quarantine_dir: Optional[Path] = (
            Path(self.cfg["quarantine_dir"]) if self.cfg.get("quarantine_dir") else None
        )
        self.backup_dir: Optional[Path] = (
            Path(self.cfg["backup_dir"]) if self.cfg.get("backup_dir") else None
        )

        self.tracker: Optional[StateTracker] = None
        self.scanner_bridge: Optional[ScannerStateBridge] = None
        self.mylibrary_client: Optional[MyLibraryClient] = None
        self.processed_map: Dict[str, str] = {}
        self.skipped_by_mylibrary_count = 0
        self.skipped_by_state_count = 0

        if not self.enabled:
            return

        _ensure_file_logger(self.cfg.get("log_file", "logs/epubmeta.log"))

        # Use unified scanner_state.db for both file_states and epubmeta tables (processed_files, metadata_cache)
        self.tracker = StateTracker(self.scanner_db_path)
        self.scanner_bridge = ScannerStateBridge(self.scanner_db_path)

        # Auto-migrate legacy .epubmeta_state.db if present
        legacy_db_str = self.cfg.get("state_db")
        if legacy_db_str:
            migrated_p, migrated_c = self.tracker.migrate_legacy_epubmeta_db(Path(legacy_db_str))
            if migrated_p or migrated_c:
                print(
                    f"  [epubmeta] Migrated legacy state DB ({legacy_db_str} -> {self.scanner_db_path.name}): "
                    f"{migrated_p} processed file(s), {migrated_c} cached book(s).",
                    flush=True,
                )

        # Seed cache and skip sets from MyLibrary BookDB
        if self.server_url and self.api_token:
            self.mylibrary_client = MyLibraryClient(
                server_url=self.server_url,
                api_token=self.api_token,
            )
            seeded_ml, skip_reg = self.mylibrary_client.pull_and_seed_cache(
                tracker=self.tracker,
                skip_complete=self.skip_complete,
            )
            if seeded_ml or skip_reg:
                print(
                    f"MyLibrary BookDB: seeded {seeded_ml} curated book(s) into local cache; "
                    f"registered {skip_reg} corrupt/DRM/complete file(s) to skip.",
                    flush=True,
                )

        # Seed from configured Calibre libraries if present
        if calibre_libraries:
            cal_seeded = 0
            for lib in calibre_libraries:
                lib_path = lib.get("path")
                if lib_path and os.path.exists(lib_path):
                    cal_seeded += seed_cache_from_calibre_db(Path(lib_path), self.tracker)
            if cal_seeded:
                print(
                    f"Calibre metadata.db: seeded {cal_seeded} curated book(s) into local cache.",
                    flush=True,
                )

        self.processed_map = self.tracker.load_processed_status_map()

    def get_excluded_dirs(self) -> Set[str]:
        """Return lowercase directory names to exclude during os.walk."""
        excluded = {
            "_quarantine",
            "quarantine",
            "bak",
            "books_bad",
            "@eadir",
            "#recycle",
            ".recycle",
            "$recycle.bin",
            ".appledouble",
            ".trashes",
            ".spotlight-v100",
        }
        for d in self.cfg.get("exclude_dirs", []):
            if d and str(d).strip():
                excluded.add(str(d).strip().lower())
        if self.quarantine_dir:
            excluded.add(self.quarantine_dir.name.lower())
        if self.backup_dir:
            excluded.add(self.backup_dir.name.lower())
        return excluded

    def is_eligible_for_enrichment(
        self,
        abspath: str,
        cached_hash: Optional[str] = None,
        force: bool = False,
    ) -> bool:
        """
        Check during the single os.walk pass whether a file should be queued for epubmeta enrichment.
        """
        if not self.enabled:
            return False
        ext = os.path.splitext(abspath)[1].lower()
        if ext not in self.allowed_exts:
            return False
        if force:
            return True

        if self.mylibrary_client:
            skip_ml, _ = self.mylibrary_client.should_skip_file(
                Path(abspath),
                sha256_hash=cached_hash,
                skip_complete=self.skip_complete,
            )
            if skip_ml:
                self.skipped_by_mylibrary_count += 1
                return False

        if self.processed_map:
            status = StateTracker.lookup_status_in_map(abspath, self.processed_map)
            if status in ("SUCCESS", "QUARANTINED", "HAS_DRM"):
                self.skipped_by_state_count += 1
                return False
            if status in ("FAILED", "NOT_FOUND", "NEEDS_REVIEW") and not self.retry_failed:
                self.skipped_by_state_count += 1
                return False

        return True

    def run_enrichment_batch(
        self,
        new_candidates: List[str],
        backfill_candidates: List[str],
        scan_root: str | Path,
    ) -> Dict[str, Any]:
        """
        Run throttled metadata enrichment on new/modified files first, then backfill candidates
        up to `max_files`.
        Returns a summary dictionary with:
          - `enriched_synced_paths`: set of abspaths already enriched and synced to BookDB & file_states
          - `quarantined_paths`: list of abspaths moved to quarantine (to be pruned from BookDB)
          - `success_count`: number of enriched books
          - `fail_count`: number of deferred/not-found/quarantined books
        """
        enriched_synced_paths: Set[str] = set()
        quarantined_paths: List[str] = []

        if not self.enabled or (not new_candidates and not backfill_candidates):
            return {
                "enriched_synced_paths": enriched_synced_paths,
                "quarantined_paths": quarantined_paths,
                "success_count": 0,
                "fail_count": 0,
            }

        # Sort EPUBs before PDFs within each priority tier (new files first, then backfill)
        sort_key = lambda p: (0 if p.lower().endswith(".epub") else 1, p.lower())
        sorted_new = sorted(new_candidates, key=sort_key)
        sorted_backfill = sorted(backfill_candidates, key=sort_key)
        combined = [Path(p) for p in (sorted_new + sorted_backfill)]

        total_eligible = len(combined)
        if self.max_files and self.max_files > 0:
            batch_files = combined[: self.max_files]
        else:
            batch_files = combined

        print(
            f"\n--- Running Unified epubmeta Enrichment Pass ---\n"
            f"Files to process: {len(batch_files)} (out of {total_eligible} eligible: "
            f"{len(sorted_new)} new/changed, {len(sorted_backfill)} backfill; "
            f"skipped {self.skipped_by_state_count} previously processed, "
            f"{self.skipped_by_mylibrary_count} complete/DRM/invalid in MyLibrary).",
            flush=True,
        )

        gemini_key = self.cfg.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY")
        google_key = self.cfg.get("google_api_key") or os.environ.get("GOOGLE_BOOKS_API_KEY")

        providers = [
            OpenLibraryProvider(raise_on_quota=True),
            GoogleBooksProvider(api_key=google_key, raise_on_quota=True),
            AmazonGoodreadsProvider(),
        ]
        aggregator = MetadataAggregator(providers=providers)
        ai = GeminiBookExtractor(api_key=gemini_key, raise_on_quota=True)

        throttler = Throttler(
            delay_seconds=self.delay,
            ai_delay_seconds=self.ai_delay,
            batch_size=self.batch_size,
            batch_pause_seconds=self.batch_pause,
        )

        scan_root_path = Path(scan_root).resolve()
        resolved_quarantine = self.quarantine_dir or (scan_root_path / "_quarantine")

        processor = BookProcessor(
            aggregator=aggregator,
            ai_extractor=ai,
            state_tracker=self.tracker,
            quarantine_dir=resolved_quarantine,
            backup_dir=self.backup_dir,
            scan_root=scan_root_path,
            dry_run=False,
            backup=self.backup,
            update_cover=self.update_cover,
            min_confidence=self.min_confidence,
            pdf_require_isbn=self.pdf_require_isbn,
            scanner_bridge=self.scanner_bridge,
            mylibrary_client=self.mylibrary_client,
        )

        start_time = time.time()
        success_count = 0
        fail_count = 0
        ai_calls_count = 0
        catalog_calls_count = 0
        local_resolved_count = 0

        for idx, file_path in enumerate(batch_files):
            orig_abspath = str(file_path)
            print(f"[{idx + 1}/{len(batch_files)}] Inspecting {file_path.name}...", flush=True)
            try:
                reader, embedded_meta, target_meta, isbn, match_result, err = processor.prepare_metadata(file_path)
            except RateLimitExceededError as rle:
                print(
                    f"[LIMIT REACHED] {rle.provider} quota/rate limit hit ({rle}). "
                    f"Stopping enrichment pass cleanly for this cycle.",
                    flush=True,
                )
                break

            if not target_meta or not reader:
                msg = err or f"No metadata found for {file_path.name}"
                fail_count += 1
                if err and "quarantine" in err.lower():
                    fail_status = "QUARANTINED"
                    quarantined_paths.append(orig_abspath)
                elif err and "drm-protected" in err.lower():
                    fail_status = "HAS_DRM"
                else:
                    fail_status = "NOT_FOUND"
                print(f"  -> {fail_status}: {msg}", flush=True)
                if self.tracker and fail_status not in ("QUARANTINED", "HAS_DRM"):
                    self.tracker.record_failure(
                        file_path,
                        msg,
                        status=fail_status,
                        original_metadata=embedded_meta,
                    )
                continue

            if match_result and not match_result.is_confident:
                msg = (
                    f"Low match confidence ({match_result.percentage}% < {int(self.min_confidence * 100)}%). "
                    f"Deferring for manual checking."
                )
                fail_count += 1
                print(f"  -> DEFERRED: {msg}", flush=True)
                if self.tracker:
                    self.tracker.record_failure(
                        file_path,
                        msg,
                        status="NEEDS_REVIEW",
                        original_metadata=embedded_meta,
                        candidate_metadata=target_meta,
                        match_confidence=match_result.score,
                    )
                continue

            res = processor.apply_metadata(file_path, reader, target_meta, isbn)
            status = res.get("status", "FAILED")
            was_cached = bool(target_meta.source and target_meta.source.startswith("local-cache"))
            if status == "SUCCESS":
                success_count += 1
                cache_tag = " [CACHED]" if was_cached else ""
                print(f"  -> SUCCESS{cache_tag}: {res.get('title')} by {res.get('author')}", flush=True)
                if res.get("pushed_to_mylibrary"):
                    enriched_synced_paths.add(orig_abspath)
                    enriched_synced_paths.add(str(file_path.resolve()))
            else:
                fail_count += 1
                print(f"  -> {status}: {res.get('message')}", flush=True)

            was_ai_used = bool(target_meta.ai_used and not was_cached)
            was_catalog = bool(
                not was_cached
                and target_meta.source
                and any(p in target_meta.source for p in ("open-library", "google-books"))
            )
            if was_ai_used:
                ai_calls_count += 1
            elif was_catalog:
                catalog_calls_count += 1
            else:
                local_resolved_count += 1

            is_last = idx == len(batch_files) - 1
            throttler.wait_for_next(
                is_last=is_last,
                ai_used=was_ai_used,
                catalog_called=was_catalog,
            )

        elapsed = time.time() - start_time
        print(
            f"[SUMMARY] epubmeta Enrichment Finished in {elapsed:.1f}s | "
            f"Enriched: {success_count} | Deferred/NotFound: {fail_count} | "
            f"Quarantined: {len(quarantined_paths)} | "
            f"Fast/Cache: {local_resolved_count} | Catalog: {catalog_calls_count} | AI: {ai_calls_count}",
            flush=True,
        )

        return {
            "enriched_synced_paths": enriched_synced_paths,
            "quarantined_paths": quarantined_paths,
            "success_count": success_count,
            "fail_count": fail_count,
        }

