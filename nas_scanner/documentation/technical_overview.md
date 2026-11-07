# Technical Overview: Standalone NAS Scanner

This document outlines the architecture, data flows, and mechanisms employed by the standalone `nas_scanner` container.

---

## System Architecture

The `nas_scanner` is a code-independent, lightweight worker app designed to run continuously on the NAS filesystem. It communicates with the central `MyLibrary` web application strictly through HTTP REST API endpoints.

```
+-------------------------------------------------------------+
|                         ORICO NAS                           |
|                                                             |
|   +-----------------------+     +-----------------------+   |
|   | /Books (Raw Ebooks)   |     | /Audiobooks (Folders) |   |
|   +-----------+-----------+     +-----------+-----------+   |
|               |                             |               |
|               |  Mounted                    |  Mounted      |
|               v                             v               |
|   +-----------+-----------------------------+-----------+   |
|   |                mylibrary_scanner Container          |   |
|   |                                                     |   |
|   |  +-----------------------------------------------+  |   |
|   |  |            docker_scanner_loop.py             |  |   |
|   |  |                                               |  |   |
|   |  |  +------------------+   +------------------+  |  |   |
|   |  |  |   sync_files.py  |   | sync_audiobooks.y|  |  |   |
|   |  |  +--------+---------+   +--------+---------+  |  |   |
|   |  +-----------|----------------------|------------+  |   |
|   +--------------|----------------------|---------------+   |
|                  |                      |                   |
|                  | Sync API Requests    |                   |
|                  v                      v                   |
|   +--------------+----------------------+---------------+   |
|   |                  mylibrary_web App                  |   |
|   +-----------------------------------------------------+   |
+-------------------------------------------------------------+
```

---

## Scanning & Parsing Mechanics

### 1. Incremental Change Detection (SQLite Cache)
To avoid high CPU overhead and slow network scans, the scanner maintains a local SQLite database (`scanner_state.db`).
* Before reading metadata or processing any file, the scanner checks the file's absolute path, modified time (`mtime`), and size in `scanner_state.db`.
* If they match the cached state, the file is skipped instantly.
* If changed (or missing), the file's metadata is extracted, uploaded, and the cache database is updated.

### 2. Audiobook Directory Mapping
Unlike normal books (where individual files are parsed recursively), audiobooks are scanned in a directory-based name-only mode:
* The scanner lists the immediate subdirectories of `/audiobooks`.
* Each subdirectory name is treated as the audiobook's title (e.g. `/audiobooks/Dune` -> title `Dune`).
* No internal audio files are walked or parsed, resulting in extremely fast execution. Size is registered as `0`.

### 3. Parallel Metadata Parsing
For books (EPUB, PDF, MOBI, AZW3), parsing is performed concurrently using Python's `ProcessPoolExecutor` to extract metadata (titles, authors, ISBNs, publishers). 
* If parallel processing fails or runs on a single-core machine, the scanner automatically falls back to safe sequential processing.

---

## REST API Integration
The scanner registers metadata by calling the `/api/sync/file/` REST API endpoint on the central web server.

### Security
All requests are authorized using an API token header:
```http
Authorization: Bearer <token_from_settings>
```

### JSON Request Payload (File Sync)
```json
{
  "file_name": "Dune.epub",
  "file_path": "/books/Science Fiction/Dune.epub",
  "file_size": 1048576,
  "file_format": "EPUB",
  "file_hash": "2c2476d66dc10272d4eccfb24c90d825",
  "file_created_at": "2026-07-11T12:00:00Z",
  "file_modified_at": "2026-07-11T12:00:00Z",
  "title": "Dune",
  "authors": "Frank Herbert",
  "isbn": "9780441172719",
  "item_type": "BOOK",
  "cover_thumbnail": "data:image/jpeg;base64,..."
}
```

---

## Deletion Pruning Phase

To keep the central database perfectly synchronized with changes on the NAS (such as manual file renames, relocations, or deletions), the scanner triggers a pruning phase immediately after the sync uploads complete.

### 1. Cleanup API Request
The scanner posts a summary of active filesystem paths to `/api/sync/cleanup/`:
```json
{
  "library_path": "/books",
  "active_paths": [
    "/books/Science Fiction/Dune.epub",
    "/books/Drama/Hamlet.pdf"
  ]
}
```

### 2. Central Server Processing
Upon receiving the cleanup payload, the central application:
* Identifies all `FileLocation` database entries located under the `library_path` that are **not** present in `active_paths`.
* Deletes those stale `FileLocation` database entries.
* Checks if any `CatalogItem` records are left with zero file locations. If so, they are pruned to prevent orphaned catalog listings.
* Returns a summary:
```json
{
  "status": "success",
  "deleted_locations": 1,
  "deleted_catalog_items": 0
}
```
