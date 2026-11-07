import os
import sys
import json
import sqlite3
import argparse
import requests
from utils.metadata_parser import extract_metadata
from utils.lock import ScannerLock, LockError

DB_PATH = 'scanner_state.db'

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
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
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
        conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
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
            print(f" Failed to sync {metadata['file_name']}. Server returned status {response.status_code}: {response.text}")
            return False
    except Exception as e:
        print(f" Error connecting to server: {e}")
        return False

def send_cleanup_to_server(server_url, api_token, library_path, active_paths):
    """Notify central server of all active paths to prune deleted books."""
    url = f"{server_url.rstrip('/')}/api/sync/cleanup/"
    headers = {
        'Authorization': f'Bearer {api_token}',
        'Content-Type': 'application/json'
    }
    payload = {
        'library_path': library_path,
        'active_paths': active_paths
    }
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        if response.status_code == 200:
            data = response.json()
            print(f" Cleaned up library index: pruned {data.get('deleted_locations', 0)} deleted locations and {data.get('deleted_catalog_items', 0)} empty catalog items.")
            return True
        else:
            print(f" Failed to perform cleanup. Server status {response.status_code}: {response.text}")
            return False
    except Exception as e:
        print(f" Error connecting to server for cleanup: {e}")
        return False

def scan_directory(directory, server_url, api_token, force=False):
    """Scan direct child directories only and sync them as audiobooks."""
    init_local_db()
    
    print(f"\nScanning Audiobook Folder: {directory}")
    
    total_files = 0
    synced_files = 0
    skipped_files = 0
    failed_files = 0
    
    # Collect immediate child directories only
    scan_items = []
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        if os.path.isdir(path):
            scan_items.append((os.path.abspath(path), name))
                
    # 1. Walk and check cache (Fast sequential check)
    files_to_parse = []
    cache = load_all_cached_states()
    
    for abspath, name in scan_items:
        total_files += 1
        try:
            stat = os.stat(abspath)
            modified_at = str(stat.st_mtime)
            size = 0  # set size to 0 for directories
                
            # Incremental check
            cached = cache.get(abspath)
            if not force and cached and cached['modified_at'] == modified_at and cached['size'] == size:
                skipped_files += 1
                continue
            
            files_to_parse.append((abspath, modified_at, size, name))
        except Exception as e:
            print(f"  Error reading stats for {name}: {e}")
            failed_files += 1

    if not files_to_parse:
        print(f"Scan Finished. Discovered: {total_files} | Synced: {synced_files} | Skipped: {skipped_files} | Failed: {failed_files}")
        return total_files, synced_files, skipped_files, failed_files

    print(f"Found {len(files_to_parse)} audiobook folders needing syncing.")
    
    # 2. Parallel Metadata Extraction and 3. Concurrent Uploads
    from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
    
    chunk_size = 1000
    total_chunks = (len(files_to_parse) + chunk_size - 1) // chunk_size
    print(f"Processing in {total_chunks} chunks of {chunk_size}...")
    
    try:
        with ProcessPoolExecutor() as executor:
            with ThreadPoolExecutor(max_workers=8) as uploader:
                for i in range(0, len(files_to_parse), chunk_size):
                    chunk = files_to_parse[i:i+chunk_size]
                    chunk_idx = (i // chunk_size) + 1
                    print(f"\n--- Processing Chunk {chunk_idx}/{total_chunks} ({len(chunk)} folders) ---", flush=True)
                    
                    parsed_metadata = {}
                    tasks = {executor.submit(process_file_metadata, (item[0], 'AUDIOBOOK')): item for item in chunk}
                    for future in as_completed(tasks):
                        item = tasks[future]
                        abspath, modified_at, size, name = item
                        try:
                            res_path, meta, error = future.result()
                            if error:
                                print(f"  Error extracting metadata for {name}: {error}")
                                failed_files += 1
                            else:
                                parsed_metadata[res_path] = (meta, modified_at, size)
                        except Exception as e:
                            print(f"  Exception extracting metadata for {name}: {e}")
                            failed_files += 1
                            
                    if not parsed_metadata:
                        continue
                        
                    # Concurrent uploads for this chunk
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
                            abspath, modified_at, size, file_hash, success = future.result()
                            if success:
                                success_batch.append((abspath, modified_at, size, file_hash))
                                synced_files += 1
                            else:
                                failed_files += 1
                        except Exception as e:
                            print(f"  Exception during upload: {e}")
                            failed_files += 1
                            
                    if success_batch:
                        update_cached_states_batch(success_batch)
    except Exception as e:
        print(f"Error during parallel chunk processing: {e}")

    # 4. Deletion Pruning Phase
    print(f"Pruning deleted files from database...")
    active_paths = [item[0] for item in scan_items]
    send_cleanup_to_server(server_url, api_token, directory, active_paths)

    print(f"Scan Finished. Discovered: {total_files} | Synced: {synced_files} | Skipped: {skipped_files} | Failed: {failed_files}")
    return total_files, synced_files, skipped_files, failed_files

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="MyLibrary Audiobook Scanner")
    parser.add_argument('--dir', help="Directory path to scan (overrides config.json)")
    parser.add_argument('--server', help="Django server URL (overrides config.json)")
    parser.add_argument('--token', help="API Security Token (overrides config.json)")
    parser.add_argument('--force', action='store_true', help="Force re-scan and sync all files")
    
    args = parser.parse_args()
    
    # Defaults
    server_url = args.server or 'http://localhost:8000'
    api_token = args.token or 'super-secret-sync-token-12345'
    folders_to_scan = []
    
    # Check for config.json
    config_path = os.path.join(os.path.dirname(__file__), 'config.json')
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
                server_url = args.server or config.get('server_url', server_url)
                api_token = args.token or config.get('api_token', api_token)
                
                if 'state_db' in config:
                    configured_path = config['state_db']
                    db_dir = os.path.dirname(configured_path)
                    if not db_dir or os.path.exists(db_dir):
                        DB_PATH = configured_path
                
                # Only load scan folders of type AUDIOBOOK
                if not args.dir:
                    folders_to_scan = [f for f in config.get('scan_folders', []) if f.get('type') == 'AUDIOBOOK']
        except Exception as e:
            print(f"Warning: Failed to parse config.json: {e}")

    # If --dir was passed, override config folders
    if args.dir:
        folders_to_scan = [{'path': args.dir}]
        
    if not folders_to_scan:
        print("Error: No audiobook directories to scan. Please specify --dir or configure scan_folders in config.json with type: AUDIOBOOK.")
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
                if not path:
                    continue
                    
                if not os.path.exists(path):
                    print(f"Error: Directory path does not exist: {path}. Skipping.")
                    total_fail += 1
                    continue
                    
                dis, sync, skip, fail = scan_directory(path, server_url, api_token, args.force)
                total_dis += dis
                total_sync += sync
                total_skip += skip
                total_fail += fail
                
            print("\n==================================")
            print("GLOBAL AUDIOBOOKS SYNC SUMMARY:")
            print(f"  Discovered: {total_dis}")
            print(f"  Synced:     {total_sync}")
            print(f"  Skipped:    {total_skip}")
            print(f"  Failed:     {total_fail}")
            print("==================================")
    except LockError as e:
        print(f"Error: {e}")
        sys.exit(1)
