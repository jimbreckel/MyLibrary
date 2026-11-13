import os
import sys
import json
import sqlite3
import argparse
import requests
from utils.metadata_parser import extract_metadata
from utils.lock import ScannerLock, LockError

DB_PATH = 'scanner_state.db'


def _connect_db():
    try:
        from epubmeta.pg_scanner_db import connect_scanner_db
        return connect_scanner_db(DB_PATH, timeout=30.0)
    except Exception:
        return sqlite3.connect(DB_PATH, timeout=30.0)


def process_file_metadata(args):
    abspath, item_type = args
    try:
        meta = extract_metadata(abspath)
        meta['item_type'] = item_type
        return abspath, meta, None
    except Exception as e:
        return abspath, None, str(e)


def upload_file_metadata(args):
    server_url, api_token, abspath, modified_at, size, metadata = args
    success = send_to_server(server_url, api_token, metadata)
    return abspath, modified_at, size, metadata.get('file_hash') if success else None, success


def init_local_db():
    """Initialize local SQLite DB to keep track of file sync state (incremental scanning)."""
    conn = _connect_db()
    cursor = conn.cursor()
    try:
        cursor.execute('PRAGMA journal_mode=DELETE')
        cursor.execute('PRAGMA synchronous=NORMAL')
    except Exception:
        pass
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS file_states (
            filepath TEXT PRIMARY KEY,
            modified_at TEXT,
            size INTEGER,
            hash TEXT,
            synced_at TEXT
        )
    ''')
    conn.commit()
    conn.close()


def load_all_cached_states():
    """Load all cached file states from SQLite into a memory dictionary to optimize lookups."""
    cache = {}
    try:
        conn = _connect_db()
        cursor = conn.cursor()
        cursor.execute('SELECT filepath, modified_at, size, hash FROM file_states')
        for row in cursor.fetchall():
            cache[row[0]] = {'modified_at': row[1], 'size': row[2], 'hash': row[3]}
        conn.close()
    except Exception as e:
        print(f"Warning: Failed to load cache: {e}")
    return cache


def update_cached_state(filepath, modified_at, size, file_hash):
    """Save the current sync state of a file."""
    conn = _connect_db()
    cursor = conn.cursor()
    import datetime
    now = datetime.datetime.utcnow().isoformat()
    cursor.execute('''
        INSERT INTO file_states (filepath, modified_at, size, hash, synced_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(filepath) DO UPDATE SET
            modified_at = excluded.modified_at,
            size = excluded.size,
            hash = excluded.hash,
            synced_at = excluded.synced_at
    ''', (filepath, modified_at, size, file_hash, now))
    conn.commit()
    conn.close()


def update_cached_states_batch(batch_data):
    """Save the current sync state of multiple files in a single transaction."""
    if not batch_data:
        return
    conn = _connect_db()
    cursor = conn.cursor()
    import datetime
    now = datetime.datetime.utcnow().isoformat()
    rows = [(item[0], item[1], item[2], item[3], now) for item in batch_data]
    cursor.executemany('''
        INSERT INTO file_states (filepath, modified_at, size, hash, synced_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(filepath) DO UPDATE SET
            modified_at = excluded.modified_at,
            size = excluded.size,
            hash = excluded.hash,
            synced_at = excluded.synced_at
    ''', rows)
    conn.commit()
    conn.close()


def remove_cached_states_batch(deleted_paths):
    """Remove deleted files from the local SQLite cache."""
    if not deleted_paths:
        return
    conn = _connect_db()
    cursor = conn.cursor()
    cursor.executemany('DELETE FROM file_states WHERE filepath = ?', [(p,) for p in deleted_paths])
    conn.commit()
    conn.close()


def send_to_server(server_url, api_token, metadata):
    """Send the extracted file metadata to the Django central application."""
    url = f"{server_url.rstrip('/')}/api/sync/file/"
    headers = {
        'Authorization': f'Bearer {api_token}',
        'Content-Type': 'application/json'
    }
    try:
        response = requests.post(url, json=metadata, headers=headers, timeout=30)
        if response.status_code in (200, 201):
            print(f" Successfully synced: {metadata['file_name']}")
            return True
        else:
            err_preview = (response.text or "")[:300].replace("\n", " ")
            print(f" Failed to sync {metadata['file_name']}. Server returned status {response.status_code}: {err_preview}")
            return False
    except Exception as e:
        print(f" Error connecting to server: {e}")
        return False


def send_deleted_paths_to_server(server_url, api_token, deleted_paths, batch_size=200):
    """Notify central server of deleted paths in small batches, avoiding massive payloads."""
    if not deleted_paths:
        return True
    url = f"{server_url.rstrip('/')}/api/sync/cleanup/"
    headers = {
        'Authorization': f'Bearer {api_token}',
        'Content-Type': 'application/json'
    }
    total_pruned_locations = 0
    total_pruned_catalog = 0
    success_all = True

    for i in range(0, len(deleted_paths), batch_size):
        chunk = deleted_paths[i:i + batch_size]
        payload = {'deleted_paths': chunk}
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            if response.status_code == 200:
                data = response.json()
                total_pruned_locations += data.get('deleted_locations', 0)
                total_pruned_catalog += data.get('deleted_catalog_items', 0)
            else:
                err_preview = (response.text or "")[:300].replace("\n", " ")
                print(f" Failed to prune chunk of {len(chunk)} paths. Server status {response.status_code}: {err_preview}")
                success_all = False
        except Exception as e:
            print(f" Error connecting to server for cleanup: {e}")
            success_all = False

    if total_pruned_locations > 0 or total_pruned_catalog > 0:
        print(f" Cleaned up library index: pruned {total_pruned_locations} deleted locations and {total_pruned_catalog} empty catalog items.")
    else:
        print(f" Cleaned up library index: no obsolete items to remove.")
    return success_all


def scan_directory(
    directory,
    server_url,
    api_token,
    item_type='BOOK',
    force=False,
    recursive=True,
    epubmeta_config=None,
    calibre_libraries=None,
):
    """Scan directory, enrich metadata via epubmeta in a single walk, and sync files concurrently."""
    init_local_db()

    if item_type == 'AUDIOBOOK':
        supported_extensions = {'.mp3', '.m4b', '.m4a'}
    else:
        supported_extensions = {'.epub', '.pdf', '.mobi', '.azw3'}

    print(f"\nScanning: {directory} (Type: {item_type}, Recursive: {recursive})", flush=True)

    total_files = 0
    synced_files = 0
    skipped_files = 0
    failed_files = 0

    want_enrich = bool(item_type == 'BOOK' and epubmeta_config and epubmeta_config.get('enabled', False))
    enrich_allowed_exts = {'.epub', '.pdf'}
    if epubmeta_config:
        if epubmeta_config.get('ignore_pdfs', True):
            enrich_allowed_exts.discard('.pdf')
        if epubmeta_config.get('pdf_only', False):
            enrich_allowed_exts = {'.pdf'}

    excluded_dirs = {
        '_quarantine', 'quarantine', 'bak', 'books_bad', '@eadir', '#recycle',
        '.recycle', '$recycle.bin', '.appledouble', '.trashes', '.spotlight-v100',
    }
    if epubmeta_config:
        for d in epubmeta_config.get('exclude_dirs', []):
            if d and str(d).strip():
                excluded_dirs.add(str(d).strip().lower())

    # 1. Load cached file states first and stream os.walk directly (avoiding a 1.48M-item scan_items list)
    files_to_parse = []  # list of (abspath, modified_at, size, name)
    unchanged_for_enrich = []  # list of (abspath, cached_hash) only for enrich_allowed_exts
    cache = load_all_cached_states()

    def _inspect_entry(abspath: str, name: str):
        nonlocal total_files, skipped_files, failed_files
        total_files += 1
        try:
            stat = os.stat(abspath)
            modified_at = str(stat.st_mtime)
            size = 0 if os.path.isdir(abspath) else stat.st_size

            cached = cache.pop(abspath, None)
            is_unchanged = (not force and cached and cached['modified_at'] == modified_at and cached['size'] == size)

            if is_unchanged:
                skipped_files += 1
                if want_enrich and os.path.splitext(abspath)[1].lower() in enrich_allowed_exts:
                    unchanged_for_enrich.append((abspath, cached.get('hash')))
                return

            files_to_parse.append((abspath, modified_at, size, name))
        except Exception as e:
            print(f"  Error reading stats for {name}: {e}")
            failed_files += 1

    if item_type == 'AUDIOBOOK':
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            if os.path.isdir(path):
                _inspect_entry(os.path.abspath(path), name)
    else:
        for root, dirs, files in os.walk(directory):
            dirs[:] = [d for d in dirs if d.lower() not in excluded_dirs and not d.startswith('._')]
            print(f"  Walking directory: {root}", flush=True)
            if not recursive:
                dirs.clear()
            for file in files:
                if file.startswith('._') or file.startswith('.epubmeta_tmp_'):
                    continue
                ext = os.path.splitext(file)[1].lower()
                if ext not in supported_extensions:
                    continue
                filepath = os.path.join(root, file)
                _inspect_entry(os.path.abspath(filepath), file)

    # Compute deleted paths now from remaining cache entries and free 1.48M-entry cache BEFORE EnrichmentSession loads
    norm_dir = os.path.abspath(directory)
    dir_prefix = norm_dir if norm_dir.endswith(os.sep) else norm_dir + os.sep
    deleted_paths = [
        cached_path for cached_path in cache.keys()
        if (cached_path == norm_dir or cached_path.startswith(dir_prefix))
    ]
    del cache
    import gc
    gc.collect()

    # 2. Initialize unified epubmeta session after freeing file_states cache
    enrich_session = None
    new_enrich_candidates = []
    backfill_enrich_candidates = []
    if want_enrich:
        try:
            from epubmeta.scanner_integration import EnrichmentSession
            enrich_session = EnrichmentSession(
                scanner_db_path=DB_PATH,
                server_url=server_url,
                api_token=api_token,
                epubmeta_config=epubmeta_config,
                calibre_libraries=calibre_libraries,
            )
            for abspath, _, _, _ in files_to_parse:
                if enrich_session.is_eligible_for_enrichment(abspath, cached_hash=None, force=force):
                    new_enrich_candidates.append(abspath)
            for abspath, cached_hash in unchanged_for_enrich:
                if enrich_session.is_eligible_for_enrichment(abspath, cached_hash=cached_hash, force=False):
                    backfill_enrich_candidates.append(abspath)
        except Exception as em_err:
            print(f"Warning: Could not initialize epubmeta enrichment session: {em_err}", flush=True)
            enrich_session = None
    del unchanged_for_enrich
    if enrich_session:
        enrich_session.processed_map.clear()
        if enrich_session.mylibrary_client:
            enrich_session.mylibrary_client.skip_hashes.clear()
            enrich_session.mylibrary_client.skip_rel_paths.clear()
            enrich_session.mylibrary_client.complete_hashes.clear()
            enrich_session.mylibrary_client.complete_rel_paths.clear()
    gc.collect()

    # 3. Unified epubmeta Enrichment Pass (New/changed files prioritized first, then backfill up to max_files)
    quarantined_paths = []
    if enrich_session:
        enrich_res = enrich_session.run_enrichment_batch(
            new_candidates=new_enrich_candidates,
            backfill_candidates=backfill_enrich_candidates,
            scan_root=directory,
        )
        enriched_synced_set = enrich_res.get("enriched_synced_paths", set())
        quarantined_paths = enrich_res.get("quarantined_paths", [])
        quarantined_set = set(quarantined_paths)

        if enriched_synced_set or quarantined_set:
            remaining_to_parse = []
            for item in files_to_parse:
                abspath = item[0]
                if abspath in enriched_synced_set:
                    # Already enriched on disk, hashed, synced to BookDB, and recorded in file_states!
                    synced_files += 1
                elif abspath in quarantined_set or not os.path.exists(abspath):
                    # Moved to quarantine as corrupt
                    failed_files += 1
                else:
                    remaining_to_parse.append(item)
            files_to_parse = remaining_to_parse

    # Free enrichment structures before parallel metadata extraction to minimize RAM usage
    del enrich_session
    del new_enrich_candidates
    del backfill_enrich_candidates
    import gc
    gc.collect()

    # 4. Parallel Metadata Extraction & Concurrent Uploads (with strict per-file timeout so no file can hang the scanner)
    if files_to_parse:
        print(f"Found {len(files_to_parse)} files needing metadata extraction and syncing.", flush=True)
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed

        chunk_size = 250
        total_chunks = (len(files_to_parse) + chunk_size - 1) // chunk_size
        print(f"Processing in {total_chunks} chunks of {chunk_size}...", flush=True)

        with ThreadPoolExecutor(max_workers=4) as uploader:
            for i in range(0, len(files_to_parse), chunk_size):
                chunk = files_to_parse[i:i + chunk_size]
                chunk_idx = (i // chunk_size) + 1
                print(f"\n--- Processing Chunk {chunk_idx}/{total_chunks} ({len(chunk)} files) ---", flush=True)

                parsed_metadata = {}
                for item in chunk:
                    abspath, modified_at, size, file_name = item
                    file_pool = ThreadPoolExecutor(max_workers=1)
                    try:
                        fut = file_pool.submit(process_file_metadata, (abspath, item_type))
                        res_path, meta, error = fut.result(timeout=20.0)
                        if error:
                            print(f"  Error extracting metadata for {file_name}: {error}", flush=True)
                            failed_files += 1
                        else:
                            parsed_metadata[res_path] = (meta, modified_at, size)
                    except FuturesTimeoutError:
                        print(f"  Timeout (20s) extracting metadata for {file_name}; using filename fallback.", flush=True)
                        fallback_meta = {
                            'file_name': file_name,
                            'file_path': abspath,
                            'file_size': size,
                            'file_format': os.path.splitext(file_name)[1][1:].upper() or 'BOOK',
                            'file_hash': '',
                            'title': os.path.splitext(file_name)[0],
                            'authors': 'Unknown Author',
                            'item_type': item_type,
                            'is_invalid': True,
                            'has_drm': False,
                        }
                        parsed_metadata[abspath] = (fallback_meta, modified_at, size)
                    except Exception as e:
                        print(f"  Exception extracting metadata for {file_name}: {e}", flush=True)
                        failed_files += 1
                    finally:
                        file_pool.shutdown(wait=False, cancel_futures=True)

                if not parsed_metadata:
                    continue

                upload_tasks = {
                    uploader.submit(
                        upload_file_metadata,
                        (server_url, api_token, path, data[1], data[2], data[0])
                    ): path for path, data in parsed_metadata.items()
                }

                success_batch = []
                for future in as_completed(upload_tasks):
                    path = upload_tasks[future]
                    try:
                        abspath, modified_at, size, file_hash, success = future.result(timeout=35.0)
                        if success:
                            success_batch.append((abspath, modified_at, size, file_hash))
                            synced_files += 1
                        else:
                            failed_files += 1
                    except Exception as e:
                        print(f"  Exception during upload: {e}", flush=True)
                        failed_files += 1

                if success_batch:
                    update_cached_states_batch(success_batch)

    # 5. Deletion Pruning Phase (includes any files moved to quarantine during this run)
    print(f"Pruning deleted files from database...", flush=True)
    for q_path in quarantined_paths:
        if q_path not in deleted_paths:
            deleted_paths.append(q_path)

    if deleted_paths:
        print(f"Found {len(deleted_paths)} deleted/quarantined files to prune from server and local cache.")
        if send_deleted_paths_to_server(server_url, api_token, deleted_paths):
            remove_cached_states_batch(deleted_paths)
    else:
        print(f"No deleted files found under {directory}.")

    print(f"Scan Finished. Discovered: {total_files} | Synced: {synced_files} | Skipped: {skipped_files} | Failed: {failed_files}", flush=True)
    return total_files, synced_files, skipped_files, failed_files


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="MyLibrary Unified Off-Webpage File Scanner & Metadata Enricher")
    parser.add_argument('--dir', help="Directory path to scan (overrides config.json)")
    parser.add_argument('--type', default='BOOK', choices=['BOOK', 'MAGAZINE', 'AUDIOBOOK'], help="Item type for directory (default: BOOK)")
    parser.add_argument('--no-recursive', action='store_true', help='Do not scan subdirectories')
    parser.add_argument('--no-enrich', action='store_true', help='Skip epubmeta metadata enrichment pass')
    parser.add_argument('--server', help="Django server URL (overrides config.json)")
    parser.add_argument('--token', help="API Security Token (overrides config.json)")
    parser.add_argument('--force', action='store_true', help="Force re-scan and sync all files")

    args = parser.parse_args()

    # Defaults
    server_url = args.server or 'http://localhost:8000'
    api_token = args.token or 'super-secret-sync-token-12345'
    folders_to_scan = []
    epubmeta_config = None
    calibre_libraries = []

    # Check for config.json
    config_path = os.path.join(os.path.dirname(__file__), 'config.json')
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
                server_url = args.server or config.get('server_url', server_url)
                api_token = args.token or config.get('api_token', api_token)
                epubmeta_config = config.get('epubmeta')
                calibre_libraries = config.get('calibre_libraries', [])

                if 'state_db' in config:
                    configured_path = config['state_db']
                    db_dir = os.path.dirname(configured_path)
                    if not db_dir or os.path.exists(db_dir):
                        DB_PATH = configured_path
                    else:
                        print(f"Warning: Directory for configured state_db '{db_dir}' does not exist. Falling back to local state DB '{DB_PATH}'.")

                if not args.dir:
                    folders_to_scan = config.get('scan_folders', [])
        except Exception as e:
            print(f"Warning: Failed to parse config.json: {e}")

    if args.no_enrich and epubmeta_config:
        epubmeta_config = {**epubmeta_config, 'enabled': False}

    if args.dir:
        default_rec = (args.type != 'AUDIOBOOK')
        recursive_val = False if args.no_recursive else default_rec
        folders_to_scan = [{'path': args.dir, 'type': args.type, 'recursive': recursive_val}]

    if not folders_to_scan:
        print("Error: No directories to scan. Please specify --dir or configure scan_folders in config.json.")
        sys.exit(1)

    lock_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'nas_scanner.lock')
    try:
        with ScannerLock(lock_file):
            print(f"Connecting to central server: {server_url}")

            total_dis = 0
            total_sync = 0
            total_skip = 0
            total_fail = 0

            for folder in folders_to_scan:
                path = folder.get('path')
                item_type = folder.get('type', 'BOOK')
                default_rec = (item_type != 'AUDIOBOOK')
                is_rec = False if args.no_recursive else folder.get('recursive', default_rec)

                if not path:
                    continue

                if not os.path.exists(path):
                    print(f"Error: Directory path does not exist: {path}. Skipping.")
                    total_fail += 1
                    continue

                dis, sync, skip, fail = scan_directory(
                    path,
                    server_url,
                    api_token,
                    item_type,
                    args.force,
                    is_rec,
                    epubmeta_config=epubmeta_config if item_type == 'BOOK' else None,
                    calibre_libraries=calibre_libraries,
                )
                total_dis += dis
                total_sync += sync
                total_skip += skip
                total_fail += fail

            print("\n==================================")
            print("GLOBAL SYNC SUMMARY:")
            print(f"  Discovered: {total_dis}")
            print(f"  Synced:     {total_sync}")
            print(f"  Skipped:    {total_skip}")
            print(f"  Failed:     {total_fail}")
            print("==================================")
    except LockError as e:
        print(f"Error: {e}")
        sys.exit(1)
