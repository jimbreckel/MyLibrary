# MyLibrary — Developer & Architecture Guide

This document details the architecture, database schemas (`BookDB` PostgreSQL + `scanner_state.db` SQLite), single-pass scanner/enricher pipeline, REST API endpoints, CLI SDK (`mylibrary_cli.py`), and MCP server (`mylibrary_mcp.py`).

---

## 1. Technical Stack & Containers

- **Web & API Backend (`mylibrary-web`)**: Django 6.1 (Python 3.13), Django REST Framework, Gunicorn.
- **Catalog Database (`mylibrary-db`)**: PostgreSQL (`BookDB`) storing `CatalogItem` (`1.15M+` rows) and `FileLocation` (`1.52M+` rows) with `max_parallel_workers_per_gather = 0` configured to prevent `/dev/shm` exhaustion inside Docker.
- **Unified Scanner & Enricher (`mylibrary_scanner`)**:
  - `docker_scanner_loop.py` runs on a configurable interval (`scan_interval_hours: 4` in `nas_scanner/config.json`).
  - `sync_files.py` performs a single `os.walk` pass over `/books` and `/audiobooks`, classifies files against `scanner_state.db` (`file_states`), runs `EnrichmentSession` (`epubmeta`) on eligible `.epub` files (prioritizing `new_enrich_candidates` before `backfill_enrich_candidates`), and extracts remaining new/changed files using a `spawn` multiprocessing pool (`max_workers=2`, recreated per 250-file chunk to bound RAM under `3 GiB`).
- **AI Front-Matter Extraction (`GeminiBookExtractor`)**:
  - Uses `gemini-2.5-flash` with daily token/cost tracking stored in `scanner_state.db` (`ai_usage_daily`) and enforced via `max_daily_cost_usd` (`$3.00/day`).

---

## 2. Repository Structure

```text
MyLibrary/
├── README.md                        # Quick-start & interface overview
├── manage.py                        # Django management entrypoint
├── mylibrary_cli.py                 # Zero-dependency CLI & Python Client SDK
├── mylibrary_mcp.py                 # Zero-dependency stdio JSON-RPC 2.0 MCP Server
├── core/
│   ├── settings.py                  # Django, PostgreSQL, DRF, and token configuration
│   └── urls.py                      # Root URL routing (/mylibrary/...)
├── catalog/
│   ├── models.py                    # CatalogItem & FileLocation ORM models
│   ├── views.py                     # Web views + Status, Explorer, History, Catalog, Calibre & Login views
│   ├── opds.py                      # OPDS 1.2 Atom feeds, book/cover streaming & Calibre AJAX emulation
│   ├── security.py                  # ExternalAccessSecurityMiddleware (LAN bypass, Basic Auth, Login, Rate-Limit)
│   ├── api.py                       # Scanner sync endpoints (/api/sync/...)
│   ├── search.py                    # Indexed & multi-term catalog search logic
│   └── urls.py                      # App URL routing
├── calibre_moon_sync.py             # Standalone Two-Pass Calibre & Moon+ Reader WebDAV sync utility
├── templates/catalog/
│   ├── base.html                    # Base layout & styles
│   ├── index.html                   # Main Catalog Search UI + Help Modal
│   ├── login.html                   # External access Sign-In UI (/mylibrary/login/)
│   └── status_dashboard.html        # Pipeline Status, Time-Series Charts & Review Explorer UI
├── nas_scanner/
│   ├── config.json                  # Scanner & epubmeta configuration
│   ├── docker_scanner_loop.py       # Scheduled 4-hour scan loop daemon
│   ├── sync_files.py                # Unified single-walk scanner + epubmeta integration
│   ├── sync_audiobooks.py           # Audiobook folder scanner
│   ├── epubmeta/                    # Embedded epubmeta enrichment package
│   │   ├── scanner_integration.py   # EnrichmentSession single-pass coordinator
│   │   ├── processor.py             # BookProcessor metadata matcher & EPUB/PDF writer
│   │   ├── state.py                 # StateTracker SQLite persistence (scanner_state.db)
│   │   └── isbn/ai_extractor.py     # Gemini AI front-matter extractor with daily budget cap
│   └── utils/
│       └── metadata_parser.py       # Memory-guarded EPUB/PDF/MOBI metadata & cover parser
└── documentation/
    ├── user_documentation.md        # End-user guide (Web, Status, CLI, MCP)
    ├── developer_documentation.md   # Architecture & API specification (this file)
    └── deployment.md                # Container deployment guide
```

---

## 3. Database Schemas

### A. PostgreSQL (`BookDB`)
- **`catalog_catalogitem` (`CatalogItem`)**:
  - `id` (UUID PK), `title` (indexed), `authors` (indexed), `publisher`, `pub_date`, `isbn` (indexed), `asin` (indexed), `item_type` (`BOOK`, `MAGAZINE`, `KINDLE`, `AUDIBLE`, `AUDIOBOOK`), `description`, `cover_image_url`, `cover_thumbnail` (base64 data URI), `created_at`, `updated_at`.
- **`catalog_filelocation` (`FileLocation`)**:
  - `id` (UUID PK), `catalog_item_id` (FK), `file_path`, `file_name`, `file_size`, `file_format` (indexed), `file_hash` (SHA-256, indexed), `is_invalid` (indexed), `has_drm` (indexed), `library_name`, `scanned_at`.

### B. Unified SQLite State DB (`nas_scanner/scanner_state.db`)
- **`file_states`**:
  - `filepath` (PK), `modified_at`, `size`, `hash` (SHA-256), `synced_at`.
- **`processed_files`**:
  - `file_path` (PK), `file_name`, `file_ext`, `file_size`, `file_mtime`, `file_sha256`, `orig_sha256`, `status` (`SUCCESS`, `NEEDS_REVIEW`, `NO_ISBN`, `QUARANTINED`, `FAILED`, `IGNORED`, `HAS_DRM`), `isbn`, `title`, `author`, `orig_title`, `orig_author`, `orig_isbn`, `match_confidence`, `original_metadata_json`, `updated_metadata_json`, `backup_path`, `quarantine_path`, `error_message`, `updated_at`.
- **`metadata_cache`**:
  - Local cache of curated/resolved book metadata by cache key (`isbn:<isbn>` or normalized `title:<title>|author:<author>`).
- **`ai_usage_daily`**:
  - `usage_date` (PK, `YYYY-MM-DD`), `requests_count`, `prompt_tokens`, `candidates_tokens`, `total_tokens`, `estimated_cost_usd`, `updated_at`.

---

## 4. Complete REST API Specification

### A. Public / UI / CLI / MCP Endpoints
1. **`GET /mylibrary/api/status/` (`StatusDashboardAPIView`)**
   - Returns `{ generated_at, scanner: { is_scanning, daemon_active, current_activity, last_log_update, scan_interval_hours, next_scan_at, next_scan_iso, next_scan_in_seconds, next_scan_countdown, scanner_log_lines, epubmeta_log_lines }, catalog: { ... }, conversion: { tracked_files_count, discovered_files_total, remaining_to_catalog, catalog_sync_chunk_info, metadata_cache_count, total_processed, remaining_to_enrich, skipped_complete_bookdb, cycle_inspected, cycle_net_used, cycle_net_cap, cycle_net_remaining, status_counts, status_pcts, success_rate } }`.
2. **`GET /mylibrary/api/catalog/` (`CatalogSearchAPIView`)**
   - Query params: `q`, `type`, `format`, `isbn`, `author`, `limit` (1–200), `offset`, `path_prefix` (default `U:\Media`), `include_cover` (`true`/`false`).
   - Returns `{ total, limit, offset, query, filters, items: [...] }`.
3. **`GET | PATCH | POST /mylibrary/api/catalog/<identifier>/` (`CatalogItemDetailAPIView`)**
   - Resolves `<identifier>` by UUID, ISBN, ASIN, SHA-256 `file_hash`, or `file_path`.
   - `GET`: Returns full `CatalogItem` details, all `FileLocation` entries (with `mapped_path`), and matching `enrichment_records` from `scanner_state.db`.
   - `PATCH` / `POST`: Updates any subset of `title`, `authors`, `publisher`, `isbn`, `asin`, `description`, `item_type`, `cover_image_url`.
4. **`GET /mylibrary/api/suggest/?q=<prefix>` (`SuggestAPIView`)**
   - Returns up to 8 `{ title, authors, type }` autocomplete matches.
5. **`GET | POST /mylibrary/api/status/items/` (`ConversionItemsAPIView`)**
   - `GET`: Query `processed_files` with `?status=ALL|SUCCESS|NEEDS_REVIEW|NO_ISBN|QUARANTINED|FAILED|IGNORED&q=...&limit=50&offset=0`. Includes Before/After descriptions and cover URLs/thumbnails.
   - `POST`:
     - `{"action": "approve", "file_path": "/books/...", "selected_metadata": {"title": "...", "author": "...", "publisher": "...", "isbn": "...", "description": "...", "cover": "..."}}`
     - `{"action": "ignore", "file_path": "/books/..."}`
     - `{"action": "requeue", "file_path": "/books/..."}`
6. **`GET /mylibrary/api/status/history/?granularity=hour|day` (`ConversionHistoryAPIView`)**
   - Returns `{ granularity, labels: [...], per_period: { ALL, SUCCESS, NEEDS_REVIEW, NO_ISBN, QUARANTINED, FAILED, IGNORED, CACHE }, cumulative: { ... } }`.
7. **`GET /mylibrary/api/logs/?source=scanner|epubmeta|both&lines=60&grep=...` (`ScannerLogsAPIView`)**
   - Tails `scanner.log` and/or `epubmeta.log`.
8. **`GET | POST /mylibrary/api/calibre/check-enriched/` (`CalibreCheckEnrichedAPIView`)**
   - Batch enrichment lookup for `calibre_moon_sync.py`. Matches Calibre books (`calibre_id`, `sha256`, `isbn`, `title`, `authors`, `file_name`) and Moon+ Reader cache filenames (`moon_filenames`) against `processed_files WHERE status = 'SUCCESS'`.

### B. OPDS 1.2 & Calibre Content Server Endpoints (`catalog/opds.py`)
- `GET /mylibrary/opds/` (`OPDSRootView`) — Root OPDS 1.2 navigation catalog.
- `GET /mylibrary/opds/opensearch.xml` (`OPDSOpenSearchView`) — OpenSearch XML descriptor.
- `GET /mylibrary/opds/search/?q=<query>` (`OPDSSearchView`) — OPDS acquisition search feed.
- `GET /mylibrary/opds/enriched/` (`OPDSEnrichedFeedView`) — Acquisition feed of `epubmeta`-enriched books.
- `GET /mylibrary/opds/recent/` (`OPDSRecentFeedView`) — Recently updated books feed.
- `GET /mylibrary/opds/authors/` (`OPDSAuthorsIndexView`) — A–Z author navigation & per-author acquisition feeds.
- `GET /mylibrary/opds/formats/` (`OPDSFormatsView`) — Format navigation (`EPUB`, `PDF`, `MOBI`, `AZW3`, `MP3`, `M4B`).
- `GET /mylibrary/opds/all/` (`OPDSAllFeedView`) — Full catalog A–Z acquisition feed.
- `GET /mylibrary/opds/cover/<uuid>.jpg` (`OPDSCoverView`) — Cover image extraction & streaming.
- `GET /mylibrary/opds/download/<file_id>/<filename>` (`OPDSDownloadFileView`) — Streams book with Calibre-normalized filename (`<Title> - <Author>.epub`) and auto-migrates matching `.po` and `.an` files in `/webdav/Apps/Books/.Moon+/Cache`.
- `GET /mylibrary/opds/ajax/books`, `/mylibrary/opds/ajax/search`, `/mylibrary/opds/ajax/book/<uuid>` (`CalibreAjaxBooksView`) — Calibre Content Server JSON emulation.

### C. External Security & Authentication (`catalog/security.py`)
- **`ExternalAccessSecurityMiddleware`**:
  - Inspects `X-Forwarded-For`, `X-Real-IP`, `CF-Connecting-IP`, and `REMOTE_ADDR` against `TRUSTED_LAN_CIDRS` (`192.168.0.0/16`, `10.0.0.0/8`, `172.16.0.0/12`, `100.64.0.0/10`).
  - When accessed outside the LAN (`WEB_AUTH_MODE=external`, `OPDS_AUTH_MODE=external`), requires session authentication (`/mylibrary/login/`), HTTP Basic Authentication (`Authorization: Basic ...`), or an API key (`X-API-Key` / `?api_key=`).
  - Includes automatic brute-force rate limiting (8 failed attempts within 10 minutes locks out the client IP for 15 minutes).

### D. Scanner Sync Endpoints (`Authorization: Bearer <SCANNER_API_TOKEN>`)
- `POST /mylibrary/api/sync/file/` (`FileSyncAPIView`) — Upsert extracted file & catalog metadata.
- `GET /mylibrary/api/sync/cache/` (`CatalogCacheAPIView`) — Pull curated metadata and skip hashes for `epubmeta`.
- `POST /mylibrary/api/sync/cleanup/` (`FileCleanupAPIView`) — Prune deleted file locations and orphaned catalog items.
- `POST /mylibrary/api/sync/media/` (`MediaSyncAPIView`) — Sync Kindle/Audible cloud items.
- `POST /mylibrary/api/calibre/add/` (`CalibreAddAPIView`) — Add file to Calibre library or fallback directory.
