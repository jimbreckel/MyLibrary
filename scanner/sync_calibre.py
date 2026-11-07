import os
import sys
import json
import sqlite3
import argparse
import requests
import datetime
from utils.metadata_parser import extract_metadata
from utils.lock import ScannerLock, LockError

DB_PATH = 'scanner_state.db'

def process_file_metadata(args):
    abspath, book_meta = args
    try:
        meta = extract_metadata(abspath)
        # Merge calibre metadata with extracted file metadata
        merged = {**meta}
        # Override file-extracted metadata with calibre-curated metadata if present
        if book_meta.get('title'):
            merged['title'] = book_meta['title']
        if book_meta.get('authors'):
            merged['authors'] = book_meta['authors']
        if book_meta.get('isbn'):
            merged['isbn'] = book_meta['isbn']
        if book_meta.get('publisher'):
            merged['publisher'] = book_meta['publisher']
        if book_meta.get('description'):
            merged['description'] = book_meta['description']
        if book_meta.get('pub_date'):
            merged['pub_date'] = book_meta['pub_date']
        if book_meta.get('asin'):
            merged['asin'] = book_meta['asin']
            
        merged['item_type'] = 'BOOK'
        merged['library_name'] = book_meta['library_name']
        merged['library_path'] = book_meta['library_path']
        
        # Check if cover.jpg exists in the same directory as the Calibre file
        cover_path = os.path.join(os.path.dirname(abspath), 'cover.jpg')
        if os.path.exists(cover_path):
            try:
                from utils.metadata_parser import generate_thumbnail
                with open(cover_path, 'rb') as f:
                    cover_bytes = f.read()
                thumbnail = generate_thumbnail(cover_bytes)
                if thumbnail:
                    merged['cover_thumbnail'] = thumbnail
            except Exception:
                pass
                
        return abspath, merged, None
    except Exception as e:
        return abspath, None, str(e)

def upload_file_metadata(args):
    server_url, api_token, abspath, modified_at, size, metadata = args
    success = send_to_server(server_url, api_token, metadata)
    return abspath, modified_at, size, metadata.get('file_hash') if success else None, success

def init_local_db():
    """Initialize local SQLite DB to keep track of file sync state."""
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
            print(f" Successfully synced: {metadata['file_name']} (Library: {metadata.get('library_name', 'Unknown')})")
            return True
        else:
            print(f" Failed to sync {metadata['file_name']}. Server returned status {response.status_code}: {response.text}")
            return False
    except Exception as e:
        print(f" Error connecting to server: {e}")
        return False

def dict_factory(cursor, row):
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d

def scan_calibre_library(library_path, library_name, server_url, api_token, force=False):
    """Read Calibre's metadata.db and sync its contents to the central database."""
    init_local_db()
    
    db_file = os.path.join(library_path, 'metadata.db')
    if not os.path.exists(db_file):
        print(f"Error: Calibre database not found at {db_file}")
        return 0, 0, 0, 0
        
    print(f"\nScanning Calibre Library: '{library_name}' at '{library_path}'")
    
    # Connect to Calibre SQLite database
    calibre_conn = sqlite3.connect(db_file)
    calibre_conn.row_factory = dict_factory
    calibre_cursor = calibre_conn.cursor()
    
    # Query book details from Calibre
    calibre_cursor.execute('''
        SELECT 
            b.id AS book_id,
            b.title,
            b.pubdate,
            b.path AS book_dir_path,
            c.text AS description,
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
             WHERE i.book = b.id AND i.type = 'isbn' LIMIT 1) AS isbn,
            (SELECT i.val 
             FROM identifiers i 
             WHERE i.book = b.id AND i.type = 'amazon' LIMIT 1) AS asin
        FROM books b
        LEFT JOIN comments c ON b.id = c.book
    ''')
    books = calibre_cursor.fetchall()
    
    total_files = 0
    synced_files = 0
    skipped_files = 0
    failed_files = 0
    
    files_to_parse = []
    cache = load_all_cached_states()
    
    # Iterate through books to find physical format files
    for book in books:
        book_id = book['book_id']
        book_dir = book['book_dir_path']
        
        # Clean up publication date (ignore default 0101-01-01)
        pub_date_str = book['pubdate']
        pub_date = None
        if pub_date_str and not pub_date_str.startswith(('0101', '0001')):
            pub_date = pub_date_str[:10]
            
        book_meta = {
            'title': book['title'],
            'authors': book['authors'] or 'Unknown Author',
            'isbn': book['isbn'],
            'publisher': book['publisher'],
            'description': book['description'],
            'pub_date': pub_date,
            'asin': book['asin'],
            'library_name': library_name,
            'library_path': library_path
        }
        
        # Query files associated with this book
        calibre_cursor.execute('SELECT format, name FROM data WHERE book = ?', (book_id,))
        formats = calibre_cursor.fetchall()
        
        for fmt in formats:
            format_ext = fmt['format'].lower()
            file_base_name = fmt['name']
            
            # Construct the relative/absolute file path
            rel_path = os.path.join(book_dir, f"{file_base_name}.{format_ext}")
            abspath = os.path.abspath(os.path.join(library_path, rel_path))
            
            if not os.path.exists(abspath):
                # File missing on disk
                continue
                
            total_files += 1
            
            try:
                stat = os.stat(abspath)
                modified_at = str(stat.st_mtime)
                size = stat.st_size
                
                # Check cache
                cached = cache.get(abspath)
                if not force and cached and cached['modified_at'] == modified_at and cached['size'] == size:
                    skipped_files += 1
                    continue
                    
                files_to_parse.append((abspath, modified_at, size, book_meta))
            except Exception as e:
                print(f"  Error reading file stats for {abspath}: {e}")
                failed_files += 1
                
    calibre_conn.close()
    
    if not files_to_parse:
        print(f"Calibre Sync Finished. Discovered: {total_files} | Synced: {synced_files} | Skipped: {skipped_files} | Failed: {failed_files}")
        return total_files, synced_files, skipped_files, failed_files
        
    print(f"Found {len(files_to_parse)} Calibre files needing metadata extraction and syncing.")
    
    # Parallel Metadata Extraction and Concurrent Uploads
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
                    print(f"\n--- Processing Chunk {chunk_idx}/{total_chunks} ({len(chunk)} files) ---", flush=True)
                    
                    parsed_metadata = {}
                    tasks = {executor.submit(process_file_metadata, (item[0], item[3])): item for item in chunk}
                    for future in as_completed(tasks):
                        item = tasks[future]
                        abspath, modified_at, size, _ = item
                        try:
                            res_path, meta, error = future.result()
                            if error:
                                print(f"  Error processing metadata for {os.path.basename(abspath)}: {error}")
                                failed_files += 1
                            else:
                                parsed_metadata[res_path] = (meta, modified_at, size)
                        except Exception as e:
                            print(f"  Exception processing metadata for {os.path.basename(abspath)}: {e}")
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
                            print(f"  Exception during upload of {os.path.basename(path)}: {e}")
                            failed_files += 1
                            
                    if success_batch:
                        update_cached_states_batch(success_batch)
    except Exception as e:
        print(f"Error during parallel chunk processing: {e}")
        
    print(f"Calibre Sync Finished. Discovered: {total_files} | Synced: {synced_files} | Skipped: {skipped_files} | Failed: {failed_files}")
    return total_files, synced_files, skipped_files, failed_files

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="MyLibrary Calibre Library Scanner")
    parser.add_argument('--path', help="Path to Calibre library folder (overrides config.json)")
    parser.add_argument('--name', default="Calibre Library", help="Name of the Calibre library")
    parser.add_argument('--server', help="Django server URL (overrides config.json)")
    parser.add_argument('--token', help="API Security Token (overrides config.json)")
    parser.add_argument('--force', action='store_true', help="Force re-scan and sync all files")
    
    args = parser.parse_args()
    
    server_url = args.server or 'http://localhost:8000'
    api_token = args.token or 'super-secret-sync-token-12345'
    libraries_to_scan = []
    
    # Load config.json
    config_path = os.path.join(os.path.dirname(__file__), 'config.json')
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
                server_url = args.server or config.get('server_url', server_url)
                api_token = args.token or config.get('api_token', api_token)
                
                # Load custom DB path if configured
                if 'state_db' in config:
                    configured_path = config['state_db']
                    db_dir = os.path.dirname(configured_path)
                    if not db_dir or os.path.exists(db_dir):
                        DB_PATH = configured_path
                    else:
                        print(f"Warning: Directory for configured state_db '{db_dir}' does not exist. Falling back to local state DB '{DB_PATH}'.")
                
                # Load Calibre libraries if --path was not specified
                if not args.path:
                    libraries_to_scan = config.get('calibre_libraries', [])
        except Exception as e:
            print(f"Warning: Failed to parse config.json: {e}")
            
    # If --path was passed, override config libraries
    if args.path:
        libraries_to_scan = [{'path': args.path, 'name': args.name}]
        
    if not libraries_to_scan:
        print("No Calibre libraries configured to scan. Skipping.")
        sys.exit(0)
        
    lock_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'nas_scanner.lock')
    try:
        with ScannerLock(lock_file):
            print(f"Connecting to central server: {server_url}")
            
            total_dis = 0
            total_sync = 0
            total_skip = 0
            total_fail = 0
            
            for lib in libraries_to_scan:
                path = lib.get('path')
                name = lib.get('name', 'Calibre Library')
                
                if not path:
                    continue
                    
                if not os.path.exists(path):
                    print(f"Error: Calibre library directory does not exist: {path}. Skipping.")
                    total_fail += 1
                    continue
                    
                dis, sync, skip, fail = scan_calibre_library(path, name, server_url, api_token, args.force)
                total_dis += dis
                total_sync += sync
                total_skip += skip
                total_fail += fail
                
            print("\n==================================")
            print("GLOBAL CALIBRE SYNC SUMMARY:")
            print(f"  Discovered: {total_dis}")
            print(f"  Synced:     {total_sync}")
            print(f"  Skipped:    {total_skip}")
            print(f"  Failed:     {total_fail}")
            print("==================================")
    except LockError as e:
        print(f"Error: {e}")
        sys.exit(1)
