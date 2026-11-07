import os
import datetime
import sqlite3
from django.core.management.base import BaseCommand
from django.utils.dateparse import parse_date
from django.db import transaction

def safe_fromtimestamp(ts):
    try:
        if ts < 86400:
            return datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
        return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    except (OSError, ValueError):
        return datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)

def extract_metadata_task(args):
    """Module-level task for parallel metadata extraction."""
    abspath, item_type = args
    try:
        from scanner.utils.metadata_parser import extract_metadata
        meta = extract_metadata(abspath)
        meta['item_type'] = item_type
        return abspath, meta, None
    except Exception as e:
        return abspath, None, str(e)

class Command(BaseCommand):
    help = 'Prepopulate database directly from local directory'

    def add_arguments(self, parser):
        parser.add_argument('--dir', type=str, default='P:\\Books', help='Directory to scan')
        parser.add_argument('--type', type=str, default='BOOK', choices=['BOOK', 'MAGAZINE', 'AUDIOBOOK'], help='Item type')
        parser.add_argument('--no-recursive', action='store_true', help='Do not scan subdirectories')

    def handle(self, *args, **options):
        import json
        from catalog.models import CatalogItem, FileLocation
        from catalog.search import find_similar_item
        
        directory = options['dir']
        item_type = options['type']
        no_recursive = options['no_recursive']
        
        if not os.path.exists(directory):
            self.stderr.write(self.style.ERROR(f"Directory {directory} does not exist!"))
            return
            
        self.stdout.write(self.style.SUCCESS(f"Starting prepopulate scan of: {directory} (Type: {item_type})"))
        
        # Determine state_db path from config.json
        state_db_path = 'scanner_state.db'
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        config_path = os.path.join(base_dir, 'scanner', 'config.json')
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r') as f:
                    config = json.load(f)
                    if 'state_db' in config:
                        state_db_path = config['state_db']
            except Exception as e:
                self.stdout.write(f"Warning: Could not read scanner config: {e}")
                
        is_recursive = not no_recursive
        if item_type == 'AUDIOBOOK':
            is_recursive = False
            supported_extensions = {'.mp3', '.m4b', '.m4a'}
        else:
            supported_extensions = {'.epub', '.pdf', '.mobi', '.azw3'}
            
        total_discovered = 0
        total_synced = 0
        total_skipped = 0
        total_failed = 0
        
        files_to_cache = []
        scan_items = []  # list of (abspath, name, st_mtime, st_ctime, size)
        
        # Collect filesystem files using fast os.scandir to avoid duplicate network stat calls
        if item_type == 'AUDIOBOOK':
            for name in os.listdir(directory):
                path = os.path.join(directory, name)
                if os.path.isdir(path):
                    try:
                        stat = os.stat(path)
                        scan_items.append((
                            os.path.abspath(path),
                            name,
                            stat.st_mtime,
                            stat.st_ctime,
                            0
                        ))
                    except Exception:
                        pass
        else:
            self.stdout.write("Scanning directory tree on NAS...")
            dir_count = [0]
            file_count = [0]
            
            def scan_dir_recursive(current_dir):
                try:
                    for entry in os.scandir(current_dir):
                        if entry.is_dir(follow_symlinks=False):
                            if is_recursive:
                                scan_dir_recursive(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            ext = os.path.splitext(entry.name)[1].lower()
                            if ext in supported_extensions:
                                try:
                                    stat = entry.stat()
                                    scan_items.append((
                                        os.path.abspath(entry.path),
                                        entry.name,
                                        stat.st_mtime,
                                        stat.st_ctime,
                                        stat.st_size
                                    ))
                                    file_count[0] += 1
                                except Exception:
                                    pass
                    dir_count[0] += 1
                    if dir_count[0] % 1000 == 0:
                        self.stdout.write(f"  Walked {dir_count[0]} directories, found {file_count[0]} candidate files so far...")
                except Exception:
                    pass

            scan_dir_recursive(directory)
            self.stdout.write(f"Completed directory walk: found {file_count[0]} matching files across {dir_count[0]} directories.")

        # Query existing file locations in DB under target path to optimize check speed
        target_dir_std = os.path.abspath(directory).lower()
        self.stdout.write("Fetching existing index from database...")
        db_locations = {}
        for loc in FileLocation.objects.all():
            loc_path_std = os.path.abspath(loc.file_path).lower()
            if loc_path_std == target_dir_std or loc_path_std.startswith(target_dir_std + os.sep) or (target_dir_std.endswith(os.sep) and loc_path_std.startswith(target_dir_std)):
                db_locations[loc_path_std] = loc

        files_to_parse = []
        seen_paths_std = set()

        for abspath, name, mtime, ctime, size_check in scan_items:
            total_discovered += 1
            abspath_std = abspath.lower()
            seen_paths_std.add(abspath_std)
            try:
                mtime_dt = safe_fromtimestamp(mtime)
                ctime_dt = safe_fromtimestamp(ctime)
                
                # Check DB cache skip
                existing = db_locations.get(abspath_std)
                if existing:
                    db_mtime_str = str(existing.file_modified_at.timestamp()) if existing.file_modified_at else ""
                    stat_mtime_str = str(mtime)
                    if db_mtime_str == stat_mtime_str and existing.file_size == size_check:
                        # File is unchanged in DB, skip parsing!
                        files_to_cache.append((abspath, stat_mtime_str, size_check, existing.file_hash))
                        total_skipped += 1
                        continue
                        
                files_to_parse.append((abspath, name, mtime_dt, ctime_dt, size_check))
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"  Error processing path {name}: {e}"))
                total_failed += 1

        # Extract metadata in parallel
        parsed_metadata = {}
        if files_to_parse:
            self.stdout.write(f"Extracting metadata for {len(files_to_parse)} new/modified files in parallel...")
            from concurrent.futures import ProcessPoolExecutor, as_completed
            try:
                with ProcessPoolExecutor() as executor:
                    tasks = {executor.submit(extract_metadata_task, (item[0], item_type)): item for item in files_to_parse}
                    for future in as_completed(tasks):
                        item = tasks[future]
                        abspath, name, mtime_dt, ctime_dt, size_check = item
                        try:
                            res_path, meta, error = future.result()
                            if error:
                                self.stderr.write(self.style.ERROR(f"  Error parsing {name}: {error}"))
                                total_failed += 1
                            else:
                                parsed_metadata[res_path] = (meta, mtime_dt, ctime_dt, size_check, name)
                        except Exception as e:
                            self.stderr.write(self.style.ERROR(f"  Exception parsing {name}: {e}"))
                            total_failed += 1
            except Exception as e:
                self.stdout.write(f"Parallel parsing failed, falling back to sequential: {e}")
                # Fallback to sequential
                from scanner.utils.metadata_parser import extract_metadata
                for abspath, name, mtime_dt, ctime_dt, size_check in files_to_parse:
                    try:
                        meta = extract_metadata(abspath)
                        meta['item_type'] = item_type
                        parsed_metadata[abspath] = (meta, mtime_dt, ctime_dt, size_check, name)
                    except Exception as ex:
                        self.stderr.write(self.style.ERROR(f"  Error parsing {name}: {ex}"))
                        total_failed += 1

        # Sequential DB writing phase (transaction safe)
        if parsed_metadata:
            self.stdout.write(f"Syncing metadata to database...")
            for abspath, (meta, mtime_dt, ctime_dt, size, name) in parsed_metadata.items():
                try:
                    file_hash = meta['file_hash']
                    file_format = meta['file_format']
                    
                    # Check exact hash match
                    file_loc = FileLocation.objects.filter(file_hash=file_hash).first()
                    if file_loc:
                        if file_loc.file_path != abspath:
                            file_loc.file_path = abspath
                            file_loc.file_name = name
                            file_loc.save()
                        files_to_cache.append((abspath, str(mtime_dt.timestamp()), size, file_hash))
                        total_synced += 1
                        continue
                        
                    # Check exact path match
                    file_loc = FileLocation.objects.filter(file_path=abspath).first()
                    if file_loc:
                        file_loc.file_hash = file_hash
                        file_loc.file_size = size
                        file_loc.file_name = name
                        file_loc.file_modified_at = mtime_dt
                        file_loc.is_invalid = meta.get('is_invalid', False)
                        file_loc.has_drm = meta.get('has_drm', False)
                        file_loc.save()
                        
                        # Merge metadata
                        def clean_str(val):
                            return val.replace('\x00', '') if isinstance(val, str) else val

                        item = file_loc.catalog_item
                        if meta.get('description') and not item.description:
                            item.description = clean_str(meta['description'])
                        if meta.get('cover_thumbnail') and not item.cover_thumbnail:
                            item.cover_thumbnail = meta['cover_thumbnail']
                        if meta.get('isbn') and not item.isbn:
                            item.isbn = clean_str(meta['isbn'])
                        if meta.get('publisher') and not item.publisher:
                            item.publisher = clean_str(meta['publisher'])
                        item.save()
                        
                        files_to_cache.append((abspath, str(mtime_dt.timestamp()), size, file_hash))
                        total_synced += 1
                        continue
                        
                    # Fuzzy match and link / create
                    def clean_str(val):
                        return val.replace('\x00', '') if isinstance(val, str) else val

                    title = clean_str(meta.get('title') or os.path.splitext(name)[0])
                    authors = clean_str(meta.get('authors') or 'Unknown Author')
                    isbn = clean_str(meta.get('isbn'))
                    publisher = clean_str(meta.get('publisher'))
                    description = clean_str(meta.get('description'))
                    cover_thumbnail = meta.get('cover_thumbnail')
                    pub_date_str = meta.get('pub_date')
                    pub_date = None
                    if pub_date_str:
                        try:
                            pub_date = parse_date(pub_date_str[:10])
                        except Exception:
                            pass
                            
                    item = find_similar_item(title=title, authors=authors, isbn=isbn)
                    if not item:
                        item = CatalogItem.objects.create(
                            title=title,
                            authors=authors,
                            publisher=publisher,
                            pub_date=pub_date,
                            isbn=isbn,
                            item_type=item_type,
                            description=description,
                            cover_thumbnail=cover_thumbnail
                        )
                        self.stdout.write(self.style.SUCCESS(f"  Created Catalog Item: {title} by {authors}"))
                    else:
                        updated = False
                        if isbn and not item.isbn:
                            item.isbn = isbn
                            updated = True
                        if publisher and not item.publisher:
                            item.publisher = publisher
                            updated = True
                        if description and not item.description:
                            item.description = description
                            updated = True
                        if cover_thumbnail and not item.cover_thumbnail:
                            item.cover_thumbnail = cover_thumbnail
                            updated = True
                        if updated:
                            item.save()
                        self.stdout.write(f"  Linked: '{title}' to existing item '{item.title}'")
                        
                    FileLocation.objects.create(
                        catalog_item=item,
                        file_path=clean_str(abspath),
                        file_name=clean_str(name),
                        file_size=size,
                        file_format=file_format,
                        file_created_at=ctime_dt,
                        file_modified_at=mtime_dt,
                        file_hash=file_hash,
                        is_invalid=meta.get('is_invalid', False),
                        has_drm=meta.get('has_drm', False)
                    )
                    files_to_cache.append((abspath, str(mtime_dt.timestamp()), size, file_hash))
                    total_synced += 1
                except Exception as e:
                    self.stderr.write(self.style.ERROR(f"  Error writing database record for {name}: {e}"))
                    total_failed += 1

        # Pruning Phase (Deletions / Renames)
        self.stdout.write("Checking for deleted files to prune...")
        deleted_locations_count = 0
        deleted_catalog_items_count = 0
        catalog_items_to_check = set()

        for path_std, loc in db_locations.items():
            if path_std not in seen_paths_std:
                catalog_items_to_check.add(loc.catalog_item_id)
                loc.delete()
                deleted_locations_count += 1

        if catalog_items_to_check:
            # Delete catalog items that no longer have any files associated
            orphans = CatalogItem.objects.filter(id__in=catalog_items_to_check, files__isnull=True)
            deleted_catalog_items_count, _ = orphans.delete()

        if deleted_locations_count > 0:
            self.stdout.write(self.style.SUCCESS(f"Pruned {deleted_locations_count} stale file locations and {deleted_catalog_items_count} orphaned catalog items."))

        # Write files to state DB cache
        if files_to_cache:
            self.stdout.write(f"Writing {len(files_to_cache)} file states to {state_db_path}...")
            try:
                db_dir = os.path.dirname(state_db_path)
                if db_dir and not os.path.exists(db_dir):
                    self.stdout.write(self.style.WARNING(f"Directory for state DB does not exist: {db_dir}. Skipping cache write."))
                else:
                    conn = sqlite3.connect(state_db_path)
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
                    now = datetime.datetime.utcnow().isoformat()
                    batch_args = [(item[0], item[1], item[2], item[3], now) for item in files_to_cache]
                    
                    cursor.executemany('''
                        INSERT INTO file_states (filepath, modified_at, size, hash, synced_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(filepath) DO UPDATE SET
                            modified_at = excluded.modified_at,
                            size = excluded.size,
                            hash = excluded.hash,
                            synced_at = excluded.synced_at
                    ''', batch_args)
                    conn.commit()
                    conn.close()
                    self.stdout.write(self.style.SUCCESS("Successfully wrote scanner_state.db cache!"))
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"Error writing to scanner_state.db: {e}"))
                
        self.stdout.write(self.style.SUCCESS("\nPrepopulation Complete!"))
        self.stdout.write(f"  Total Discovered: {total_discovered}")
        self.stdout.write(f"  Total Synced:     {total_synced}")
        self.stdout.write(f"  Total Skipped:    {total_skipped}")
        self.stdout.write(f"  Total Failed:     {total_failed}")
