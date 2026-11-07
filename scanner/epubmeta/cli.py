"""Command-line interface for epubmeta."""

from __future__ import annotations
import logging
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

import click
from dotenv import load_dotenv

from .models import BookMetadata
from .readers import get_reader, SUPPORTED_EXTENSIONS
from .isbn.extractor import extract_isbns_from_text
from .isbn.ai_extractor import GeminiBookExtractor
from .providers.open_library import OpenLibraryProvider
from .providers.google_books import GoogleBooksProvider
from .providers.amazon_goodreads import AmazonGoodreadsProvider, is_asin
from .providers.aggregator import MetadataAggregator
from .throttler import Throttler
from .state import StateTracker
from .processor import BookProcessor

load_dotenv()


def setup_logging(verbose: bool, log_file: Optional[Path] = None):
    level = logging.DEBUG if verbose else logging.INFO
    format_str = "%(asctime)s [%(levelname)s] %(message)s"
    handlers: List[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(level=level, format=format_str, datefmt="%H:%M:%S", handlers=handlers, force=True)


def find_supported_files(
    target: Path,
    recursive: bool = False,
    exclude_dirs: Optional[List[str]] = None,
    ignore_pdfs: bool = False,
    pdf_only: bool = False,
) -> List[Path]:
    """Find all .epub and .pdf files under target, skipping backup, quarantine, and excluded directories."""
    target = target.resolve()
    allowed_exts = set(SUPPORTED_EXTENSIONS)
    if ignore_pdfs:
        allowed_exts.discard(".pdf")
    if pdf_only:
        allowed_exts = {".pdf"}

    if target.is_file():
        if target.suffix.lower() in allowed_exts:
            return [target]
        return []

    excluded_lower = {
        "_quarantine",
        "bak",
        "@eadir",
        "#recycle",
        ".recycle",
        "$recycle.bin",
        ".appledouble",
        ".trashes",
        ".spotlight-v100",
    }
    if exclude_dirs:
        excluded_lower.update(d.strip().lower() for d in exclude_dirs if d and d.strip())

    files: List[Path] = []
    if recursive:
        for root, dirs, filenames in os.walk(target):
            dirs[:] = [
                d for d in dirs
                if d.lower() not in excluded_lower and not d.startswith("._")
            ]
            root_path = Path(root)
            for fname in filenames:
                if fname.startswith("._") or fname.startswith(".epubmeta_tmp_"):
                    continue
                ext = os.path.splitext(fname)[1].lower()
                if ext in allowed_exts:
                    files.append(root_path / fname)
    else:
        for p in target.iterdir():
            if p.is_file() and p.suffix.lower() in allowed_exts:
                if p.name.startswith("._") or p.name.startswith(".epubmeta_tmp_"):
                    continue
                files.append(p)
    # Process EPUB files before PDF files (higher likelihood of clean embedded/text metadata to seed the duplicate cache)
    return sorted(files, key=lambda p: (0 if p.suffix.lower() == ".epub" else 1, str(p).lower()))


@click.group(context_settings=dict(help_option_names=["-h", "--help"]))
def main():
    """epubmeta: Surgical EPUB & PDF metadata updater with rate-limiting and AI ISBN recovery."""
    pass


def format_diff_table(
    file_name: str,
    embedded: BookMetadata,
    proposed: BookMetadata,
    isbn: Optional[str],
    match_result: Optional[Any] = None,
) -> str:
    """Format a clean side-by-side preview comparing current vs proposed metadata."""
    lines = []
    lines.append("\n" + "=" * 76)
    lines.append(f"  FILE: {file_name}")
    lines.append("-" * 76)
    lines.append(f"  {'FIELD':<12} {'CURRENT EMBEDDED':<28}   {'PROPOSED NEW VALUE'}")
    lines.append("-" * 76)

    def add_row(field: str, cur_val: Optional[str], new_val: Optional[str]):
        c = (cur_val or "-")
        if len(c) > 28:
            c = c[:25] + "..."
        n = (new_val or "-")
        if len(n) > 55:
            n = n[:52] + "..."
        lines.append(f"  {field:<12} {c:<28} -> {n}")

    add_row("Title", embedded.title, proposed.title)
    add_row(
        "Authors",
        ", ".join(embedded.authors) if embedded.authors else None,
        ", ".join(proposed.authors) if proposed.authors else None,
    )
    add_row("ISBN", embedded.isbn13 or embedded.isbn10, proposed.isbn13 or isbn)
    add_row("Publisher", embedded.publisher, proposed.publisher)
    add_row("Date", embedded.published_date, proposed.published_date)

    if proposed.subjects:
        tags = ", ".join(proposed.subjects[:6])
        if len(proposed.subjects) > 6:
            tags += f" (+{len(proposed.subjects)-6} more)"
        add_row("Subjects", ", ".join(embedded.subjects[:3]) if embedded.subjects else None, tags)

    if proposed.description:
        desc = proposed.description.replace("\n", " ").strip()
        if len(desc) > 65:
            desc = desc[:62] + "..."
        lines.append(f"  {'Description':<12} {desc}")

    if match_result:
        score_tag = f"{match_result.percentage}%"
        if match_result.is_confident:
            score_tag += " (CONFIDENT MATCH)"
        else:
            score_tag += " [!] (LOW CONFIDENCE / DIFFERS SIGNIFICANTLY)"
        lines.append(f"  {'Match Score':<12} {score_tag}")

    ai_status = "Yes (Gemini AI)" if proposed.ai_used else "No (Text Scan / Provider)"
    lines.append(f"  {'AI Required':<12} {ai_status}")
    lines.append(f"  {'Source':<12} {proposed.source or 'aggregated'}")
    lines.append("=" * 76)
    return "\n".join(lines)


@main.command()
@click.argument("target", type=click.Path(exists=True, path_type=Path), default=".")
@click.option("--interactive", "-i", is_flag=True, default=False, help="Preview metadata changes and ask for confirmation before applying.")
@click.option("--update-cover", is_flag=True, default=False, help="Replace existing EPUB covers with high-res provider cover (by default, covers are only set if missing).")
@click.option("--quarantine-dir", default=None, type=click.Path(path_type=Path), help="Directory to move invalid/corrupted files to (mirrors subfolder path; default: <target>/_quarantine).")
@click.option("--no-quarantine", is_flag=True, default=False, help="Do not move invalid/corrupted files to a quarantine directory.")
@click.option("--backup-dir", default=None, type=click.Path(path_type=Path), help="Root directory for backups when --backup is enabled (mirrors subfolder path, e.g. Books_bad/bak).")
@click.option("--exclude-dir", multiple=True, help="Directory name(s) to exclude during recursive scan (repeatable, e.g. --exclude-dir 'Computer Book Code').")
@click.option("--ignore-pdfs", is_flag=True, default=False, help="Ignore .pdf files and only process .epub files.")
@click.option("--pdf-only", is_flag=True, default=False, help="Only process .pdf files (skip .epub files).")
@click.option("--pdf-require-isbn", is_flag=True, default=False, help="For PDF files, require a verifiable ISBN (or local cache hit) and skip title-only web search fallback.")
@click.option("--min-confidence", default=0.50, type=float, help="Minimum match score (0.0 - 1.0) before deferring for manual review (default: 0.50).")
@click.option("--recursive", "-r", is_flag=True, help="Scan directories recursively.")
@click.option("--dry-run", is_flag=True, help="Preview metadata without modifying files.")
@click.option("--backup/--no-backup", default=False, help="Create a .bak backup in a bak/ subdirectory before modifying.")
@click.option("--delay", default=1.0, type=float, help="Seconds to pause after catalog lookups (default: 1.0s).")
@click.option("--ai-delay", default=4.0, type=float, help="Seconds to pause after AI vision/LLM requests (default: 4.0s).")
@click.option("--batch-size", default=5, type=int, help="Number of files to process per periodic batch.")
@click.option("--batch-pause", default=30.0, type=float, help="Pause duration (seconds) between batches.")
@click.option("--max-files", default=None, type=int, help="Maximum number of files to process per run/cycle before stopping or sleeping.")
@click.option("--stop-on-limit", is_flag=True, default=False, help="Exit cleanly as soon as an API quota limit (429) is reached so a scheduler can resume later.")
@click.option("--sleep-on-limit", is_flag=True, default=False, help="Sleep and automatically resume when an API quota limit (429) or --max-files limit is reached.")
@click.option("--limit-sleep", default=3600.0, type=float, help="Seconds to sleep when --sleep-on-limit triggers (default: 3600s / 1 hour).")
@click.option("--gemini-api-key", default=None, help="Google Gemini API key for AI document inspection.")
@click.option("--google-api-key", default=None, help="Google Books API key (optional, for higher rate limits).")
@click.option("--no-db", is_flag=True, default=False, help="Stateless on-demand mode: do not read or write any database (SQLite state DB, scanner_state.db, Calibre, or MyLibrary BookDB) and run against all files in target.")
@click.option("--force", "-f", is_flag=True, help="Force re-processing of already completed files.")
@click.option("--retry-failed", is_flag=True, help="Retry files that previously failed.")
@click.option("--state-db", default=None, type=click.Path(path_type=Path), help="Path to state SQLite database.")
@click.option("--scanner-db", default=None, envvar="MYLIBRARY_SCANNER_DB", type=click.Path(path_type=Path), help="Path to MyLibrary scanner_state.db to reuse SHA-256 hashes and keep file_states synced.")
@click.option("--calibre-db", multiple=True, type=click.Path(path_type=Path), help="Path(s) to Calibre metadata.db (or library folder) to pre-seed the local metadata cache.")
@click.option("--mylibrary-url", default=None, envvar="MYLIBRARY_URL", help="MyLibrary server URL (e.g. http://localhost:8000) to pre-seed cache, skip corrupt/DRM files, and push updates.")
@click.option("--mylibrary-token", default=None, envvar="MYLIBRARY_TOKEN", help="MyLibrary API bearer token (SCANNER_API_TOKEN).")
@click.option("--skip-complete", is_flag=True, default=False, help="When using --mylibrary-url, skip files that already have complete metadata in MyLibrary BookDB.")
@click.option("--log-file", default=None, type=click.Path(path_type=Path), help="Optional log file path (ideal for unattended SSH / NAS runs).")
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose debug logging.")
def process(
    target: Path,
    interactive: bool,
    update_cover: bool,
    quarantine_dir: Optional[Path],
    no_quarantine: bool,
    backup_dir: Optional[Path],
    exclude_dir: tuple,
    ignore_pdfs: bool,
    pdf_only: bool,
    pdf_require_isbn: bool,
    min_confidence: float,
    recursive: bool,
    dry_run: bool,
    backup: bool,
    delay: float,
    ai_delay: float,
    batch_size: int,
    batch_pause: float,
    max_files: Optional[int],
    stop_on_limit: bool,
    sleep_on_limit: bool,
    limit_sleep: float,
    gemini_api_key: Optional[str],
    google_api_key: Optional[str],
    no_db: bool,
    force: bool,
    retry_failed: bool,
    state_db: Optional[Path],
    scanner_db: Optional[Path],
    calibre_db: tuple,
    mylibrary_url: Optional[str],
    mylibrary_token: Optional[str],
    skip_complete: bool,
    log_file: Optional[Path],
    verbose: bool,
):
    """Process and update metadata for EPUB and PDF files."""
    setup_logging(verbose, log_file=log_file)

    if no_quarantine:
        resolved_quarantine = None
        all_excluded_dirs = list(exclude_dir)
    else:
        resolved_quarantine = quarantine_dir or ((target if target.is_dir() else target.parent) / "_quarantine")
        all_excluded_dirs = [resolved_quarantine.name] + list(exclude_dir)
    if backup_dir:
        all_excluded_dirs.append(backup_dir.name)

    files = find_supported_files(
        target,
        recursive=recursive,
        exclude_dirs=all_excluded_dirs,
        ignore_pdfs=ignore_pdfs,
        pdf_only=pdf_only,
    )
    if not files:
        click.echo(f"No supported files (.epub, .pdf) found at: {target}")
        return

    click.echo(f"Found {len(files)} candidate file(s).")

    # Initialize State Tracker & External DBs (skipped completely when --no-db is active)
    if no_db:
        tracker = None
        scanner_bridge = None
        mylibrary_client = None
        files_to_process = list(files)
        click.secho(
            f"Stateless on-demand mode (--no-db): processing all {len(files_to_process)} file(s) with no database reads/writes.",
            fg="cyan",
        )
    else:
        tracker = StateTracker(state_db) if not dry_run else None

        # External Database Integrations (scanner_state.db, Calibre metadata.db, MyLibrary BookDB API)
        from .external_dbs import (
            ScannerStateBridge,
            MyLibraryClient,
            seed_cache_from_calibre_db,
        )

        scanner_bridge = ScannerStateBridge(scanner_db) if scanner_db else None
        mylibrary_client = None
        if mylibrary_url and mylibrary_token:
            mylibrary_client = MyLibraryClient(server_url=mylibrary_url, api_token=mylibrary_token)
            seeded_ml, skip_registered = mylibrary_client.pull_and_seed_cache(
                tracker=tracker,
                skip_complete=skip_complete,
            )
            if seeded_ml or skip_registered:
                click.echo(
                    f"MyLibrary BookDB: seeded {seeded_ml} curated book(s) into local cache; "
                    f"registered {skip_registered} corrupt/DRM/complete file(s) to skip."
                )

        if tracker and calibre_db:
            calibre_seeded = 0
            for c_db in calibre_db:
                calibre_seeded += seed_cache_from_calibre_db(c_db, tracker)
            if calibre_seeded:
                click.echo(f"Calibre metadata.db: seeded {calibre_seeded} curated book(s) into local cache.")

        # Filter files needing processing using an in-memory status map (fast for 1M+ files)
        files_to_process = []
        skipped_by_mylibrary = 0
        processed_map = tracker.load_processed_status_map() if (tracker and not force) else {}
        for f in files:
            if mylibrary_client and not force:
                skip_ml, _ = mylibrary_client.should_skip_file(f, sha256_hash=None, skip_complete=skip_complete)
                if skip_ml:
                    skipped_by_mylibrary += 1
                    continue
            if processed_map:
                status = StateTracker.lookup_status_in_map(f, processed_map)
                if status in ("SUCCESS", "QUARANTINED", "HAS_DRM"):
                    continue
                if status in ("FAILED", "NOT_FOUND", "NEEDS_REVIEW") and not retry_failed:
                    continue
            files_to_process.append(f)

        skip_msg = f"skipped {len(files) - len(files_to_process)} previously completed/reviewed"
        if skipped_by_mylibrary:
            skip_msg += f", including {skipped_by_mylibrary} corrupt/DRM/complete in MyLibrary"
        click.echo(f"Files to process: {len(files_to_process)} ({skip_msg}).")
        if not files_to_process:
            click.echo("All files are up-to-date! Use --force to reprocess or --retry-failed to retry failed/deferred files.")
            return

    raise_on_quota = bool(sleep_on_limit or stop_on_limit)

    # Providers setup
    providers = [
        OpenLibraryProvider(raise_on_quota=raise_on_quota),
        GoogleBooksProvider(api_key=google_api_key, raise_on_quota=raise_on_quota),
        AmazonGoodreadsProvider(),
    ]
    aggregator = MetadataAggregator(providers=providers)

    # AI Extractor setup
    ai = GeminiBookExtractor(api_key=gemini_api_key, raise_on_quota=raise_on_quota)
    if ai.is_available:
        click.echo("Gemini AI ISBN extraction enabled.")
    else:
        click.echo("Gemini AI key not set (set GEMINI_API_KEY or --gemini-api-key for visual/text AI fallback).")

    # Throttler
    def throttle_cb(msg: str, pause_duration: float):
        click.secho(f"\n[PAUSE] {msg}", fg="yellow")

    throttler = Throttler(
        delay_seconds=delay,
        ai_delay_seconds=ai_delay,
        batch_size=batch_size,
        batch_pause_seconds=batch_pause,
        callback=throttle_cb,
    )

    processor = BookProcessor(
        aggregator=aggregator,
        ai_extractor=ai,
        state_tracker=tracker,
        quarantine_dir=resolved_quarantine,
        backup_dir=backup_dir,
        scan_root=target if target.is_dir() else target.parent,
        dry_run=dry_run,
        backup=backup,
        update_cover=update_cover,
        min_confidence=min_confidence,
        pdf_require_isbn=pdf_require_isbn,
        scanner_bridge=scanner_bridge,
        mylibrary_client=mylibrary_client,
    )

    start_time = time.time()
    success_count = 0
    fail_count = 0
    apply_all = False
    ai_calls_count = 0
    catalog_calls_count = 0
    local_resolved_count = 0
    from datetime import datetime, timedelta
    from .exceptions import RateLimitExceededError

    seen_in_session = {str(p) for p in files_to_process}

    def rescan_for_new_files() -> int:
        """Re-scan target directories for any newly added .epub or .pdf files and merge them into queue (EPUBs first)."""
        current_files = find_supported_files(
            target,
            recursive=recursive,
            exclude_dirs=all_excluded_dirs,
            ignore_pdfs=ignore_pdfs,
            pdf_only=pdf_only,
        )
        newly_added: List[Path] = []
        for cand in current_files:
            cand_key = str(cand)
            if cand_key in seen_in_session:
                continue
            if tracker and not tracker.should_process(cand, force=force, retry_failed=retry_failed):
                seen_in_session.add(cand_key)
                continue
            seen_in_session.add(cand_key)
            newly_added.append(cand)
        if newly_added:
            # Keep remaining queue + newly added sorted with EPUBs before PDFs
            remaining = files_to_process[idx + 1 :] + newly_added
            remaining.sort(key=lambda p: (0 if p.suffix.lower() == ".epub" else 1, str(p).lower()))
            files_to_process[idx + 1 :] = remaining
            click.secho(
                f"[RESCAN] Discovered {len(newly_added)} newly added file(s) in directory tree (EPUBs prioritized first).",
                fg="cyan",
                bold=True,
            )
        return len(newly_added)

    try:
        idx = 0
        while idx < len(files_to_process):
            file_path = files_to_process[idx]
            click.echo(f"\n[{idx+1}/{len(files_to_process)}] Inspecting {file_path.name}...")

            while True:
                try:
                    reader, embedded_meta, target_meta, isbn, match_result, err = processor.prepare_metadata(file_path)
                    break
                except RateLimitExceededError as rle:
                    if stop_on_limit and not sleep_on_limit:
                        click.secho(
                            f"\n[LIMIT REACHED] {rle.provider} quota/rate limit hit ({rle}).\n"
                            f"Stopping cleanly so your scheduler or next run can resume strictly from '{file_path.name}'.",
                            fg="yellow",
                            bold=True,
                        )
                        raise SystemExit(0)
                    sleep_secs = max(rle.retry_after or 0.0, limit_sleep)
                    resume_at = datetime.now() + timedelta(seconds=sleep_secs)
                    click.secho(
                        f"\n[LIMIT REACHED] {rle.provider} quota/rate limit hit ({rle}).\n"
                        f"  -> Sleeping for {sleep_secs:.0f}s ({sleep_secs/60:.1f} min, resuming at {resume_at.strftime('%H:%M:%S')})...",
                        fg="yellow",
                        bold=True,
                    )
                    time.sleep(sleep_secs)
                    rescan_for_new_files()
                    click.secho(f"[RESUMING] Woke up! Retrying {file_path.name}...", fg="cyan", bold=True)

            if not target_meta or not reader:
                msg = err or f"No metadata found for {file_path.name}"
                fail_count += 1
                if err and "quarantine" in err.lower():
                    fail_status = "QUARANTINED"
                elif err and "drm-protected" in err.lower():
                    fail_status = "HAS_DRM"
                else:
                    fail_status = "NOT_FOUND"
                click.secho(f"  -> {fail_status}: {msg}", fg="yellow")
                if tracker and fail_status not in ("QUARANTINED", "HAS_DRM"):
                    tracker.record_failure(
                        file_path,
                        msg,
                        status=fail_status,
                        original_metadata=embedded_meta,
                    )
                idx += 1
                if idx == len(files_to_process):
                    rescan_for_new_files()
                continue

            # In unattended mode, defer low-confidence matches for manual checking
            if not interactive and match_result and not match_result.is_confident:
                msg = (
                    f"Low match confidence ({match_result.percentage}% < {int(min_confidence * 100)}%). "
                    f"Candidate differs significantly from file clues. Deferring for manual checking."
                )
                fail_count += 1
                click.secho(f"  -> DEFERRED: {msg}", fg="yellow")
                if tracker:
                    tracker.record_failure(
                        file_path,
                        msg,
                        status="NEEDS_REVIEW",
                        original_metadata=embedded_meta,
                        candidate_metadata=target_meta,
                        match_confidence=match_result.score,
                    )
                idx += 1
                if idx == len(files_to_process):
                    rescan_for_new_files()
                continue

            # Interactive preview & confirmation
            if interactive and not apply_all:
                preview = format_diff_table(file_path.name, embedded_meta, target_meta, isbn, match_result=match_result)
                click.echo(preview)

                default_choice = "y"
                if match_result and not match_result.is_confident:
                    click.secho(
                        f"\n  ⚠️ LOW MATCH CONFIDENCE ({match_result.percentage}% < {int(min_confidence * 100)}%): "
                        f"Candidate differs significantly from book clues!",
                        fg="yellow",
                        bold=True,
                    )
                    for d in match_result.details:
                        click.secho(f"     • {d}", fg="yellow")
                    default_choice = "n"

                choice = click.prompt(
                    "Apply changes? [y]es / [n]o (skip/defer) / [a]ll remaining / [q]uit",
                    type=click.Choice(["y", "n", "a", "q"], case_sensitive=False),
                    default=default_choice,
                    show_choices=False,
                ).lower()

                if choice == "q":
                    click.secho("\nQuitting as requested. Processed progress is saved.", fg="yellow", bold=True)
                    break
                elif choice == "n":
                    click.secho(f"  -> Skipped/Deferred {file_path.name}", fg="yellow")
                    if tracker:
                        tracker.record_failure(
                            file_path,
                            "Deferred by user in interactive review",
                            status="NEEDS_REVIEW",
                            original_metadata=embedded_meta,
                            candidate_metadata=target_meta,
                            match_confidence=match_result.score if match_result else None,
                        )
                    idx += 1
                    if idx == len(files_to_process):
                        rescan_for_new_files()
                    continue
                elif choice == "a":
                    apply_all = True
                    click.secho("Applying this and all remaining files automatically...", fg="cyan")

            # Apply metadata to document
            res = processor.apply_metadata(file_path, reader, target_meta, isbn)
            status = res["status"]
            was_cached = bool(target_meta and target_meta.source and target_meta.source.startswith("local-cache"))
            if status in ("SUCCESS", "DRY_RUN"):
                success_count += 1
                color = "green" if status == "SUCCESS" else "cyan"
                cache_tag = " [CACHED]" if was_cached else ""
                click.secho(f"  -> {status}{cache_tag}: {res['title']} by {res.get('author')}", fg=color)
            else:
                fail_count += 1
                click.secho(f"  -> {status}: {res['message']}", fg="red")

            # Track limiting factors and cost metrics
            was_ai_used = bool(target_meta and target_meta.ai_used and not was_cached)
            was_catalog = bool(
                target_meta
                and not was_cached
                and target_meta.source
                and any(p in target_meta.source for p in ("open-library", "google-books", "amazon-goodreads", "apple-books"))
            )
            if was_ai_used:
                ai_calls_count += 1
            elif was_catalog:
                catalog_calls_count += 1
            else:
                local_resolved_count += 1

            if idx + 1 == len(files_to_process):
                rescan_for_new_files()

            is_last = (idx == len(files_to_process) - 1)

            # Check if --max-files cycle limit is reached
            if max_files and max_files > 0 and (idx + 1) % max_files == 0 and not is_last:
                if sleep_on_limit:
                    resume_at = datetime.now() + timedelta(seconds=limit_sleep)
                    click.secho(
                        f"\n[CYCLE LIMIT REACHED] Processed {idx + 1} file(s). "
                        f"Sleeping for {limit_sleep:.0f}s ({limit_sleep/60:.1f} min, resuming at {resume_at.strftime('%H:%M:%S')})...",
                        fg="yellow",
                        bold=True,
                    )
                    time.sleep(limit_sleep)
                    rescan_for_new_files()
                    click.secho("[RESUMING] Starting next cycle...", fg="cyan", bold=True)
                    idx += 1
                    continue
                else:
                    click.secho(
                        f"\n[MAX FILES REACHED] Reached --max-files={max_files}. "
                        f"Progress saved; next scheduled run will resume from file #{idx + 2}.",
                        fg="yellow",
                        bold=True,
                    )
                    break

            # Apply adaptive throttling based on active limiting factors
            throttler.wait_for_next(
                is_last=is_last,
                ai_used=was_ai_used,
                catalog_called=was_catalog,
            )
            idx += 1
    except KeyboardInterrupt:
        if no_db:
            click.secho("\n\n[INTERRUPTED] Stopped by user (Ctrl+C).", fg="yellow", bold=True)
        else:
            click.secho(
                "\n\n[INTERRUPTED] Stopped by user (Ctrl+C). Progress is saved in the state database; re-run the same command to resume.",
                fg="yellow",
                bold=True,
            )
    except SystemExit:
        pass

    elapsed_time = time.time() - start_time
    est_ai_cost = ai_calls_count * 0.00025  # Approx ~$0.00025 per Gemini call
    click.echo("\n" + "=" * 50)
    click.secho(f"Finished processing! Success: {success_count}, Failed/Deferred: {fail_count}", bold=True)
    click.echo(f"  - Total Time:         {elapsed_time:.1f}s")
    click.echo(f"  - Local / Fast Path:  {local_resolved_count} file(s) (0.0s delay, $0.00 cost)")
    click.echo(f"  - Catalog Lookups:    {catalog_calls_count} file(s) (Polite delay, $0.00 cost)")
    click.echo(f"  - AI Vision / LLM:    {ai_calls_count} file(s) (Est. cost: ${est_ai_cost:.4f})")


@main.command("run")
@click.argument("target", type=click.Path(exists=True, path_type=Path), default=".")
@click.option("--recursive", "-r", is_flag=True, help="Scan directories recursively.")
@click.option("--interactive", "-i", is_flag=True, default=False, help="Preview metadata changes and ask for confirmation before applying.")
@click.option("--dry-run", is_flag=True, help="Preview metadata without modifying files.")
@click.option("--backup/--no-backup", default=False, help="Create a .bak backup before modifying.")
@click.option("--backup-dir", default=None, type=click.Path(path_type=Path), help="Root directory for backups when --backup is enabled.")
@click.option("--update-cover", is_flag=True, default=False, help="Replace existing EPUB covers with high-res provider cover.")
@click.option("--quarantine-dir", default=None, type=click.Path(path_type=Path), help="Directory to move invalid/corrupted files to.")
@click.option("--no-quarantine", is_flag=True, default=False, help="Do not move invalid/corrupted files to a quarantine directory.")
@click.option("--exclude-dir", multiple=True, help="Directory name(s) to exclude during recursive scan.")
@click.option("--ignore-pdfs", is_flag=True, default=False, help="Ignore .pdf files and only process .epub files.")
@click.option("--pdf-only", is_flag=True, default=False, help="Only process .pdf files (skip .epub files).")
@click.option("--pdf-require-isbn", is_flag=True, default=False, help="For PDF files, require a verifiable ISBN and skip title-only web search fallback.")
@click.option("--min-confidence", default=0.50, type=float, help="Minimum match score (0.0 - 1.0) before skipping/deferring (default: 0.50).")
@click.option("--delay", default=0.5, type=float, help="Seconds to pause after catalog lookups (default: 0.5s).")
@click.option("--ai-delay", default=4.0, type=float, help="Seconds to pause after AI vision/LLM requests (default: 4.0s).")
@click.option("--batch-size", default=15, type=int, help="Number of files to process per periodic batch (default: 15).")
@click.option("--batch-pause", default=5.0, type=float, help="Pause duration (seconds) between batches (default: 5.0s).")
@click.option("--max-files", default=None, type=int, help="Maximum number of files to process before stopping.")
@click.option("--stop-on-limit", is_flag=True, default=False, help="Exit cleanly as soon as an API quota limit (429) is reached.")
@click.option("--sleep-on-limit", is_flag=True, default=False, help="Sleep and automatically resume when an API quota limit (429) is reached.")
@click.option("--limit-sleep", default=3600.0, type=float, help="Seconds to sleep when --sleep-on-limit triggers.")
@click.option("--gemini-api-key", default=None, help="Google Gemini API key for AI document inspection.")
@click.option("--google-api-key", default=None, help="Google Books API key (optional, for higher rate limits).")
@click.option("--log-file", default=None, type=click.Path(path_type=Path), help="Optional log file path.")
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose debug logging.")
def run_ondemand(
    target: Path,
    recursive: bool,
    interactive: bool,
    dry_run: bool,
    backup: bool,
    backup_dir: Optional[Path],
    update_cover: bool,
    quarantine_dir: Optional[Path],
    no_quarantine: bool,
    exclude_dir: tuple,
    ignore_pdfs: bool,
    pdf_only: bool,
    pdf_require_isbn: bool,
    min_confidence: float,
    delay: float,
    ai_delay: float,
    batch_size: int,
    batch_pause: float,
    max_files: Optional[int],
    stop_on_limit: bool,
    sleep_on_limit: bool,
    limit_sleep: float,
    gemini_api_key: Optional[str],
    google_api_key: Optional[str],
    log_file: Optional[Path],
    verbose: bool,
):
    """Run on demand against all files in a directory without using any database."""
    assert process.callback is not None
    process.callback(
        target=target,
        interactive=interactive,
        update_cover=update_cover,
        quarantine_dir=quarantine_dir,
        no_quarantine=no_quarantine,
        backup_dir=backup_dir,
        exclude_dir=exclude_dir,
        ignore_pdfs=ignore_pdfs,
        pdf_only=pdf_only,
        pdf_require_isbn=pdf_require_isbn,
        min_confidence=min_confidence,
        recursive=recursive,
        dry_run=dry_run,
        backup=backup,
        delay=delay,
        ai_delay=ai_delay,
        batch_size=batch_size,
        batch_pause=batch_pause,
        max_files=max_files,
        stop_on_limit=stop_on_limit,
        sleep_on_limit=sleep_on_limit,
        limit_sleep=limit_sleep,
        gemini_api_key=gemini_api_key,
        google_api_key=google_api_key,
        no_db=True,
        force=True,
        retry_failed=True,
        state_db=None,
        scanner_db=None,
        calibre_db=(),
        mylibrary_url=None,
        mylibrary_token=None,
        skip_complete=False,
        log_file=log_file,
        verbose=verbose,
    )


@main.command()
@click.argument("file_path", type=click.Path(exists=True, path_type=Path))
@click.option("--gemini-api-key", default=None, help="Google Gemini API key.")
def inspect(file_path: Path, gemini_api_key: Optional[str]):
    """Inspect embedded metadata and detected ISBN in an EPUB or PDF."""
    try:
        reader = get_reader(file_path)
    except Exception as e:
        click.secho(f"Error: {e}", fg="red")
        sys.exit(1)

    click.secho(f"\n=== Inspecting: {file_path.name} ===", bold=True)

    # 1. Embedded Metadata
    click.secho("\n--- Embedded File Metadata ---", fg="cyan")
    meta = reader.read_metadata()
    click.echo(f"Title:        {meta.title}")
    click.echo(f"Authors:      {', '.join(meta.authors) if meta.authors else None}")
    click.echo(f"Publisher:    {meta.publisher}")
    click.echo(f"Date:         {meta.published_date}")
    click.echo(f"Language:     {meta.language}")
    click.echo(f"ISBN-13:      {meta.isbn13}")
    click.echo(f"ISBN-10:      {meta.isbn10}")
    click.echo(f"Subjects:     {', '.join(meta.subjects) if meta.subjects else None}")
    if meta.description:
        desc_preview = meta.description[:200] + ("..." if len(meta.description) > 200 else "")
        click.echo(f"Description:  {desc_preview}")

    # 2. Text ISBN Scan
    click.secho("\n--- Front-matter Text Scan ---", fg="cyan")
    front_text = reader.extract_front_text(max_pages=10)
    isbns = extract_isbns_from_text(front_text)
    if isbns:
        click.echo("Detected ISBN(s) from document text:")
        for isbn, label in isbns:
            click.echo(f"  - {isbn} (matched: {label})")
    else:
        click.echo("No valid ISBNs detected via regex in front text.")

    # 3. AI Extractor test if key is available
    ai = GeminiBookExtractor(api_key=gemini_api_key)
    if ai.is_available:
        click.secho("\n--- Gemini AI Analysis ---", fg="cyan")
        images = []
        if not front_text or len(front_text.strip()) < 100:
            images = reader.extract_front_images(max_pages=3)
        ai_res = ai.extract(text=front_text, images=images)
        click.echo(f"AI Detected: {ai_res}")


@main.command()
@click.argument("query")
@click.option("--google-api-key", default=None, help="Google Books API key.")
def search(query: str, google_api_key: Optional[str]):
    """Test lookup for an ISBN or title query against metadata providers."""
    providers = [
        OpenLibraryProvider(),
        GoogleBooksProvider(api_key=google_api_key),
    ]
    aggregator = MetadataAggregator(providers=providers)

    click.echo(f"Searching providers for: {query}...")
    is_isbn = query.replace("-", "").isdigit() or "978" in query
    if is_isbn:
        res = aggregator.fetch_by_isbn(query)
    else:
        res = aggregator.fetch_by_query(query)

    if not res:
        click.secho("No metadata found.", fg="red")
        return

    click.secho("\n--- Found Metadata ---", fg="green", bold=True)
    click.echo(f"Title:        {res.title}")
    click.echo(f"Authors:      {', '.join(res.authors)}")
    click.echo(f"Publisher:    {res.publisher}")
    click.echo(f"Date:         {res.published_date}")
    click.echo(f"ISBN-13:      {res.isbn13}")
    click.echo(f"Subjects:     {', '.join(res.subjects[:10])}")
    click.echo(f"Source:       {res.source}")
    if res.description:
        click.echo(f"\nDescription:\n{res.description[:400]}...")


@main.command()
@click.option("--state-db", default=None, type=click.Path(path_type=Path), help="Path to state SQLite database.")
def status(state_db: Optional[Path]):
    """Display processing summary statistics from the state database."""
    tracker = StateTracker(state_db)
    summary = tracker.get_summary()

    click.secho(f"\nState Database: {tracker.db_path}", bold=True)
    if not summary or summary.get("TOTAL", 0) == 0:
        click.echo("No files recorded in state database yet.")
        return

    click.echo("-" * 30)
    for stat, count in summary.items():
        color = "green" if stat == "SUCCESS" else ("red" if stat in ("FAILED", "NOT_FOUND") else "white")
        click.secho(f"{stat:<15}: {count}", fg=color)
    click.echo("-" * 30)


@main.command("reset-state")
@click.option("--state-db", default=None, type=click.Path(path_type=Path), help="Path to state SQLite database.")
@click.confirmation_option(prompt="Are you sure you want to clear all processing history?")
def reset_state(state_db: Optional[Path]):
    """Reset / clear the state database."""
    tracker = StateTracker(state_db)
    tracker.clear()
    click.secho("State database cleared successfully.", fg="green")


@main.command("sync-mylibrary")
@click.option("--state-db", default=None, type=click.Path(path_type=Path), help="Path to .epubmeta_state.db.")
@click.option("--scanner-db", default=None, envvar="MYLIBRARY_SCANNER_DB", type=click.Path(path_type=Path), help="Path to MyLibrary scanner_state.db to update.")
@click.option("--mylibrary-url", default=None, envvar="MYLIBRARY_URL", help="MyLibrary server URL (e.g. http://localhost:8000).")
@click.option("--mylibrary-token", default=None, envvar="MYLIBRARY_TOKEN", help="MyLibrary API bearer token (SCANNER_API_TOKEN).")
def sync_mylibrary(
    state_db: Optional[Path],
    scanner_db: Optional[Path],
    mylibrary_url: Optional[str],
    mylibrary_token: Optional[str],
):
    """Push all SUCCESS records from .epubmeta_state.db to MyLibrary BookDB and/or scanner_state.db."""
    import json
    from .external_dbs import ScannerStateBridge, MyLibraryClient, compute_full_sha256

    tracker = StateTracker(state_db)
    scanner_bridge = ScannerStateBridge(scanner_db) if scanner_db else None
    client = MyLibraryClient(mylibrary_url, mylibrary_token) if (mylibrary_url and mylibrary_token) else None

    if not scanner_bridge and not client:
        click.secho("Provide --scanner-db and/or (--mylibrary-url and --mylibrary-token).", fg="yellow")
        return

    with tracker._get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM processed_files WHERE status = 'SUCCESS'"
        ).fetchall()

    synced_api = 0
    synced_scanner = 0
    for row in rows:
        fp = Path(row["file_path"])
        if not fp.exists():
            continue
        file_sha256 = row["file_sha256"] or compute_full_sha256(fp)
        orig_sha256 = row["orig_sha256"]
        if scanner_bridge and file_sha256:
            scanner_bridge.update_file_state(fp, sha256_hash=file_sha256)
            synced_scanner += 1
        if client and file_sha256 and row["updated_metadata_json"]:
            try:
                meta = BookMetadata.from_dict(json.loads(row["updated_metadata_json"]))
                if client.push_updated_file(fp, meta, file_sha256=file_sha256, orig_sha256=orig_sha256):
                    synced_api += 1
            except Exception as e:
                click.secho(f"Failed syncing {fp.name}: {e}", fg="red")

    click.secho(
        f"Sync complete: updated {synced_scanner} row(s) in scanner_state.db, pushed {synced_api} book(s) to MyLibrary BookDB.",
        fg="green",
    )


@main.command("status")
@click.option(
    "--state-db",
    default=None,
    type=click.Path(path_type=Path),
    help="Path to .epubmeta_state.db (auto-detects U:\\Media\\Books_bad\\.epubmeta_state.db or /books_bad/.epubmeta_state.db if present).",
)
@click.option("--recent", default=10, type=int, help="Number of recent processed files to display.")
def status_cmd(state_db: Optional[Path], recent: int):
    """Display processing status, counts by state, cache size, and recent activity."""
    if state_db is None:
        for candidate in [
            Path("scanner_state.db"),
            Path("/app/scanner_state.db"),
            Path(r"U:\MyLibrary\nas_scanner\scanner_state.db"),
            Path(r"C:\Users\oscar\Repo_Pers\notgithub\MyLibrary\nas_scanner\scanner_state.db"),
            Path(r"U:\Media\Books_bad\.epubmeta_state.db"),
            Path("/volume1/common/Media/Books_bad/.epubmeta_state.db"),
            Path("/books_bad/.epubmeta_state.db"),
        ]:
            if candidate.exists():
                state_db = candidate
                break

    tracker = StateTracker(state_db)
    click.secho(f"=== epubmeta Status ({tracker.db_path}) ===", fg="cyan", bold=True)

    with tracker._get_connection() as conn:
        status_rows = conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM processed_files GROUP BY status ORDER BY cnt DESC"
        ).fetchall()
        cache_cnt = conn.execute("SELECT COUNT(*) FROM metadata_cache").fetchone()[0]
        recent_rows = conn.execute(
            "SELECT file_path, status, title, isbn, updated_at, error_message "
            "FROM processed_files ORDER BY updated_at DESC LIMIT ?",
            (recent,),
        ).fetchall()

    click.echo(f"\nMetadata Cache Entries: {cache_cnt:,}")
    click.echo("\nProcessed Files by Status:")
    if not status_rows:
        click.echo("  (none yet)")
    else:
        total = sum(r["cnt"] for r in status_rows)
        for r in status_rows:
            color = "green" if r["status"] == "SUCCESS" else ("yellow" if r["status"] in ("NEEDS_REVIEW", "NOT_FOUND") else "red")
            click.secho(f"  {r['status']:<18} {r['cnt']:>10,}", fg=color)
        click.echo(f"  {'TOTAL':<18} {total:>10,}")

    if recent_rows:
        click.echo(f"\nMost Recent {len(recent_rows)} File(s):")
        for r in recent_rows:
            name = Path(r["file_path"]).name
            st = r["status"]
            detail = r["title"] or r["error_message"] or ""
            isbn_str = f" [ISBN: {r['isbn']}]" if r["isbn"] else ""
            click.echo(f"  [{r['updated_at'][:19]}] {st:<13} {name}{isbn_str} -> {detail}")


if __name__ == "__main__":
    main()
