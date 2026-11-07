import os
import datetime
import json
import sqlite3
from django.core.management.base import BaseCommand
from catalog.models import FileLocation

class Command(BaseCommand):
    help = 'Sync PostgreSQL file locations to the local SQLite scanner_state.db cache'

    def handle(self, *args, **options):
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

        self.stdout.write(f"Reading file locations from database...")
        file_locs = list(FileLocation.objects.all())
        self.stdout.write(f"Found {len(file_locs)} file records in DB.")

        files_to_cache = []
        missing_on_disk = 0
        
        self.stdout.write("Verifying files on disk and retrieving stats...")
        for idx, loc in enumerate(file_locs):
            if idx > 0 and idx % 2000 == 0:
                self.stdout.write(f"  Processed {idx} files...")
                
            if os.path.exists(loc.file_path):
                try:
                    stat = os.stat(loc.file_path)
                    files_to_cache.append((
                        loc.file_path,
                        str(stat.st_mtime),
                        stat.st_size,
                        loc.file_hash
                    ))
                except Exception as e:
                    self.stderr.write(f"  Error reading stat for {loc.file_path}: {e}")
            else:
                missing_on_disk += 1

        if missing_on_disk > 0:
            self.stdout.write(self.style.WARNING(f"  {missing_on_disk} files in database were not found on disk."))

        if files_to_cache:
            self.stdout.write(f"Writing {len(files_to_cache)} file states to {state_db_path}...")
            try:
                db_dir = os.path.dirname(state_db_path)
                if db_dir and not os.path.exists(db_dir):
                    self.stdout.write(self.style.WARNING(f"Directory for state DB does not exist: {db_dir}. Skipping write."))
                    return
                    
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
                self.stdout.write(self.style.SUCCESS("Successfully synced scanner_state.db cache!"))
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"Error writing to scanner_state.db: {e}"))
        else:
            self.stdout.write("No files to write to cache.")
