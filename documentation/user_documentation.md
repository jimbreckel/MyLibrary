# MyLibrary — User Guide

Welcome to the **MyLibrary** User Documentation. This guide covers the Central Web Catalog, the Conversion & Import Status Dashboard (`epubmeta`), the Command-Line Interface (`mylibrary_cli.py`), and the Model Context Protocol Server (`mylibrary_mcp.py`).

---

## 1. System Overview

MyLibrary consists of four integrated components:
1. **Central Catalog Web Application (`/mylibrary/`)**: A Django + PostgreSQL web application indexing **1.15M+ unique catalog items** and **1.52M+ physical files** across Books, Magazines, Kindle, Audible, and Audiobooks.
2. **Conversion & Import Status Dashboard (`/mylibrary/status/`)**: Real-time observability, interactive time-series charts, and manual review/approval workflow for `epubmeta` enrichment and NAS scanning.
3. **Unified NAS Scanner & Enricher (`nas_scanner`)**: A background container (`docker_scanner_loop.py` + `sync_files.py` + `epubmeta`) that walks `/books` and `/audiobooks` every 4 hours, prioritizes new/modified files first, enriches `.epub` metadata via Open Library, Google Books, Amazon/Goodreads, and Gemini AI (with a hard daily cost cap), and syncs changes to `BookDB`.
4. **CLI (`mylibrary_cli.py`) & MCP Server (`mylibrary_mcp.py`)**: Full command-line and AI-agent interfaces to every catalog, status, review, and log function.

---

## 2. Central Web Catalog (`/mylibrary/`)

Access the main catalog at **`http://192.168.1.43:8080/mylibrary/`**.

### Search, Filtering & On-Demand Enrichment
- **Full-Catalog Search Across All Books**: Search by title, author, ISBN, ASIN, publisher, or physical filename (`files__file_name`).
- **One-Click On-Demand Enrichment (`✨ Enrich Now`)**:
  - Click **`✨ Enrich Now`** on any book card or inside **View Details** (or **`✨ Enrich File`** on a specific file location) to immediately run `epubmeta` on the physical `.epub` / `.pdf` file on the NAS, rewrite its embedded OPF metadata, and update its cover, ISBN, author, and synopsis in `BookDB`.
  - Check **"Enrich search results on demand"** beneath the search bar (or append `+enrich` to your search query) to automatically enrich top unenriched search results right as you search!
- **Instant Autocomplete**: Typing 2+ characters displays matching title and author suggestions.
- **Virtual Bookshelves & Faceted Filters**: Browse `Bookmarked & Reading in Moon+`, `Highlights & Notes (.an)`, `Enriched EPUBs`, `Purchased on Amazon`, `Kindle+`, `Amazon Bookmarked`, `Purchased on Audible`, `Amazon Only (Not on NAS)`, and `Matched to NAS Books`, or filter by **Catalog Type**, **File Format**, **Enriched Tags**, and **Enriched Collections**.
- **Book Details Modal**: Click any card to view its cover art, synopsis, publisher, publication date, ISBN/ASIN, Amazon ownership/bookmark banner, and all physical file copies on the NAS (with **Download**, **Enrich File**, and **Add to Calibre** buttons).

---

## 3. Pipeline & Enrichment Status Dashboard (`/mylibrary/status/`)

Access the live status dashboard at **`http://192.168.1.43:8080/mylibrary/status/`** (or click **Status Dashboard** in the top bar of the main catalog).

### A. Live Pipeline Activity & Next Scan Countdown
- **Scanner Status Pill**: Shows **`Scanner: Active`** while a scan is running or **`Scanner: Standby`** during the 4-hour sleep window between scans.
- **Next Scan Pill**: Displays the exact scheduled start time of the next scan in your local browser timezone alongside a **live 1-second ticking countdown** (e.g., `Next Scan: 06:48:59 PM (in 3h 12m 40s)`).
- **15-Second Auto-Refresh**: Automatically polls `/mylibrary/api/status/` every 15 seconds to update all KPIs, progress counters, and terminal logs without losing your active tab or scroll position.

### B. Remaining to Catalog & Remaining to Enrich Counters
- **Tracked File States (`SQLite`) Card**:
  - **`Remaining to Catalog`**: Shows `0` when all `1.52M+` discovered NAS files are cataloged in `file_states` and `BookDB`. When new files are discovered and being extracted in chunks, it displays the live remaining count and active chunk (`Chunk X/Y`).
  - **`Discovered on NAS`**: Total valid files found across `/books` and `/audiobooks`.
- **`epubmeta Enrichments` Card**:
  - **`Remaining to Enrich`**: Live count of eligible `.epub` files remaining in the backfill queue (`~596k`), ticking down as each file is inspected.
  - **`Cycle Batch`**: Shows external catalog/AI lookups used in the current scan cycle out of the `max_files: 2500` batch cap (`e.g., 686 / 2500 (1814 left)`).
  - **`Complete in BookDB`**: Files automatically skipped (`435,368`) because `BookDB` already has complete metadata (ISBN + description + author).
- **Priority Handling for New Books**: Whenever new or modified files are added to `/books` or `/audiobooks`, `sync_files.py` places them in `new_enrich_candidates` ahead of the backfill queue so they are cataloged and enriched **first** on the very next scan cycle.

### C. Interactive Time-Series History Charts
Click any number on the **`epubmeta Enrichments`** card (`All Processed`, `Enriched`, `Review`, `No ISBN`, `Quarantine`) or the **`Local Metadata Cache`** card to open the interactive **Enrichment & Processing History Over Time** modal:
- **Series Selector Pills**: Switch between `All Processed`, `Enriched`, `Review`, `No ISBN`, `Quarantine`, `Metadata Cache`, or **`Compare All`** (overlays all curves on one chart).
- **View Mode Toggle**:
  - **Cumulative Total**: Smooth area/line chart showing total enriched books growing over time.
  - **Per-Hour / Day Rate**: Bar chart showing processing throughput per hour or per day.
- **Granularity Toggle**: Switch between **Hourly** and **Daily** buckets.
- **Table Jump**: Click **`Browse Matching Files in Table Below ↓`** at the bottom of the chart modal to jump straight to the filtered file list.

### D. Enrichments & Quarantine Explorer (Review & Mix-and-Match Approval)
In the **Enrichments & Quarantine Explorer** table:
1. **Status Filter Pills**:
   - **`All`** — All processed records in `scanner_state.db`.
   - **`Enriched (`SUCCESS`)`** — Confident matches automatically written to the `.epub` and synced to `BookDB`.
   - **`Needs Review (`NEEDS_REVIEW`)`** — Plausible candidate matches with low confidence (`20%–49%`) deferred for your review.
   - **`No ISBN (`NO_ISBN`)`** — Books where no ISBN or confident catalog entry existed online; left untouched as-is so they don't clutter your review queue.
   - **`Quarantined (`QUARANTINED`)`** — Corrupt/unreadable archives moved safely to `/books_bad/quarantine`.
   - **`Failed` / `Ignored`** — Transient errors or items where you chose to keep the original metadata.
2. **Configurable Path Prefix (`U:\Media`)**:
   - Set your local mapped drive prefix (default `U:\Media`, saved in `localStorage`) so container paths like `/books/Science Fiction/...` are displayed and copied as `U:\Media\books\Science Fiction\...`.
3. **Detail & Mix-and-Match Review Modal**:
   - Click any row to open the side-by-side **Original Metadata (Before)** vs. **Enriched Metadata (After)** comparison, including **Before & After Cover Art** and **Before & After Description**.
   - **Click-to-Pick Fields**: Click any individual field (`Cover`, `Title`, `Author`, `Publisher`, `ISBN`, `Description`) in either the left (**Original**) or right (**Enriched**) column to cherry-pick which version to keep (highlighted in green with `✓ KEEP`).
   - **Editable Merged Preview**: Edit any of the **Selected Fields to Save** directly in the input boxes before saving.
   - **Actions**:
     - **`Approve Selected Fields`**: Saves your chosen/edited fields to both `scanner_state.db` (`SUCCESS`) and `BookDB` (`CatalogItem`).
     - **`Keep Original (Ignore)`**: Marks the file as `IGNORED` and keeps its original metadata.
     - **`Re-queue for Next Scan`**: Removes the file from `processed_files` so `epubmeta` re-scans it on the next cycle.

---

## 4. Command-Line Interface (`mylibrary_cli.py`)

`mylibrary_cli.py` provides terminal access to every function in MyLibrary using only the Python standard library.

### Environment Variables & Global Flags
- `--url` / `MYLIBRARY_URL`: Base URL (default: `http://192.168.1.43:8080/mylibrary`)
- `--prefix` / `MYLIBRARY_PATH_PREFIX`: Local path prefix for NAS files (default: `U:\Media`)
- `--json`: Output raw JSON on any command for scripting or piping

### Command Reference
```powershell
# Show detailed CLI help and examples
python mylibrary_cli.py help

# 1. Pipeline, scanner schedule, and remaining catalog/enrichment counts
python mylibrary_cli.py status

# 2. Search the catalog
python mylibrary_cli.py search "Dune" --type BOOK --format EPUB --limit 10

# 3. Inspect a book by UUID, ISBN, ASIN, SHA-256 hash, or file path
python mylibrary_cli.py book 29b1bc0f-e3f3-4b68-82c3-33549aed7a96

# 4. Update a book's metadata in BookDB
python mylibrary_cli.py update-book 29b1bc0f-e3f3-4b68-82c3-33549aed7a96 --title "Dune" --authors "Frank Herbert"

# 5. Fast autocomplete suggestions
python mylibrary_cli.py suggest "Foundation"

# 6. List or search epubmeta enrichments
python mylibrary_cli.py enrichments --status NEEDS_REVIEW --limit 20
python mylibrary_cli.py enrichments --status NO_ISBN --search "Lumley"

# 7. Approve an enrichment (full candidate or mix-and-match field overrides)
python mylibrary_cli.py approve "U:\Media\books\Science Fiction\NEC\Necroscope II_Vamphyri! - Brian Lumley.epub" \
  --title "Necroscope II: Vamphyri!" --isbn "9781466817715"

# 8. Keep original metadata (mark IGNORED)
python mylibrary_cli.py ignore "U:\Media\books\Science Fiction\NEC\Necroscope II_Vamphyri! - Brian Lumley.epub"

# 9. Re-queue a file for the next epubmeta scan cycle
python mylibrary_cli.py requeue "/books/Science Fiction/NEC/Necroscope II_Vamphyri! - Brian Lumley.epub"

# 10. View hourly or daily enrichment history with terminal bar chart
python mylibrary_cli.py history --granularity hour --series SUCCESS

# 11. Tail live scanner.log and epubmeta.log
python mylibrary_cli.py logs --source both --lines 50 --grep "SUCCESS"
```

---

## 5. Model Context Protocol (MCP) Server (`mylibrary_mcp.py`)

`mylibrary_mcp.py` exposes all MyLibrary functions as native MCP tools and resources over stdio JSON-RPC 2.0.

### Configuration (`mcp_config.json` / `claude_desktop_config.json`)
```json
{
  "mcpServers": {
    "mylibrary": {
      "command": "python",
      "args": [
        "c:\\Users\\oscar\\Repo_Pers\\notgithub\\MyLibrary\\mylibrary_mcp.py"
      ],
      "env": {
        "MYLIBRARY_URL": "http://192.168.1.43:8080/mylibrary",
        "MYLIBRARY_PATH_PREFIX": "U:\\Media"
      }
    }
  }
}
```

### Available MCP Tools
| Tool Name | Description |
| :--- | :--- |
| `mylibrary_get_status` | Live scanner state, next scan countdown, remaining catalog/enrichment counts, and KPIs |
| `mylibrary_search_catalog` | Search and filter the 1.15M+ item catalog (`query`, `item_type`, `file_format`, `isbn`, `author`, `limit`, `offset`) |
| `mylibrary_get_book` | Full book metadata, synopsis, physical file locations, and enrichment history by UUID, ISBN, ASIN, hash, or path |
| `mylibrary_update_book` | Update `title`, `authors`, `publisher`, `isbn`, `asin`, or `description` in BookDB |
| `mylibrary_suggest` | Fast title/author autocomplete suggestions |
| `mylibrary_list_enrichments` | List/search `epubmeta` records (`NEEDS_REVIEW`, `QUARANTINED`, `NO_ISBN`, `SUCCESS`, `FAILED`, `IGNORED`, `ALL`) |
| `mylibrary_approve_enrichment` | Approve an enrichment item with optional mix-and-match field selection (`title`, `author`, `publisher`, `isbn`, `description`, `cover`) |
| `mylibrary_ignore_enrichment` | Keep original metadata and mark an item as `IGNORED` |
| `mylibrary_requeue_enrichment` | Re-queue a file in `scanner_state.db` for the next scan cycle |
| `mylibrary_get_enrichment_history` | Get hourly or daily time-series enrichment metrics (`per_period` and `cumulative`) |
| `mylibrary_get_logs` | Tail `scanner.log` and `epubmeta.log` with optional line count and keyword filter |

---

## 6. OPDS 1.2 & Calibre Content Server (`/mylibrary/opds/`)

MyLibrary includes a built-in **OPDS 1.2 Content Server** and **Calibre Content Server JSON emulator** ([`catalog/opds.py`](file:///c:/Users/oscar/Repo_Pers/notgithub/MyLibrary/catalog/opds.py)) so physical tablets and e-readers (such as **Moon+ Reader**, KOReader, Calibre Sync, Librera, and PocketBook) can browse, search, and download books directly from the NAS without needing desktop Calibre.

### A. OPDS Endpoints
- **Root Catalog (Add this URL in Moon+ Reader):** `http://192.168.1.43:8080/mylibrary/opds/`
- **Enriched EPUBs Shelf (`epubmeta` Verified):** `http://192.168.1.43:8080/mylibrary/opds/enriched/`
- **Recently Updated & Enriched Books:** `http://192.168.1.43:8080/mylibrary/opds/recent/`
- **Browse by Author (A–Z):** `http://192.168.1.43:8080/mylibrary/opds/authors/`
- **Browse by Format (`EPUB`, `PDF`, `MOBI`, `AZW3`, `MP3`, `M4B`):** `http://192.168.1.43:8080/mylibrary/opds/formats/`
- **All Catalog Books (A–Z):** `http://192.168.1.43:8080/mylibrary/opds/all/`
- **OpenSearch Search Feed:** `http://192.168.1.43:8080/mylibrary/opds/search/?q=<query>`

### B. Automatic On-Demand Enrichment on Download & Moon+ Reader Bookmark Migration
When you download a book from MyLibrary OPDS (`/mylibrary/opds/download/<id>/<Title - Author.epub>`):
1. **Automatic On-Demand Enrichment**: If the `.epub` or `.pdf` file is not already enriched (`status = 'SUCCESS'` in `scanner_state.db`), MyLibrary automatically runs `epubmeta` enrichment on the fly, rewrites the embedded OPF metadata in `/books` (with backup in `/books_bad/bak`), and updates `scanner_state.db` and `BookDB` before streaming the file! You can also tap the explicit **`✨ Enrich & Download`** acquisition link (`?enrich=1`) or append `+enrich` to any OPDS search query (`/mylibrary/opds/search/?q=...+enrich`).
2. **Calibre-Normalized Filename**: The server streams the physical `.epub` from `/volume1/common/Media/Books` using the exact Calibre filename convention (`<Title> - <FirstAuthor>.epub`).
3. **Automatic Moon+ Bookmark Migration**: At the moment of download, MyLibrary inspects your Moon+ Reader WebDAV Cache (`/volume1/common/webdav/Apps/Books/.Moon+/Cache`, mapped as `U:\webdav\Apps\Books\.Moon+\Cache`) and automatically copies/rewrites any existing `.po` (reading position) and `zlib`-compressed `.an` (highlights/bookmarks) files to match the new Calibre filename!

---

## 7. External Network Security (`/mylibrary/login/` & HTTP Basic Auth)

MyLibrary is protected by [`ExternalAccessSecurityMiddleware`](file:///c:/Users/oscar/Repo_Pers/notgithub/MyLibrary/catalog/security.py):
- **On Your Home LAN (`192.168.1.x` or Tailscale `100.64.x.x`):** By default (`WEB_AUTH_MODE=external`, `OPDS_AUTH_MODE=external`), local requests do not require a login prompt.
- **Outside Your Home Network (Public Internet / Reverse Proxy / Cloudflare Tunnel):**
  - **Web Pages (`/mylibrary/`, `/mylibrary/status/`)** automatically redirect to `/mylibrary/login/`.
  - **OPDS & REST APIs (`/mylibrary/opds/`, `/mylibrary/api/`)** require HTTP Basic Authentication (`Username` / `Password` in Moon+ Reader) or an API key (`X-API-Key` / `?api_key=`).
  - **Brute-Force Protection:** Automatically locks out any IP address for 15 minutes after 8 failed password attempts.
  - **Credentials:** Configured via `AUTH_USERNAME` and `AUTH_PASSWORD` in `.env` (or any active Django user account). To require login even on your local LAN, set `WEB_AUTH_MODE=always` and `OPDS_AUTH_MODE=always` in `.env`.

---

## 8. Step-by-Step Plan: Converting a Physical Device's Moon+ Reader to Use MyLibrary OPDS

Follow this step-by-step plan to safely transition one of your Android tablets/phones from manual desktop Calibre USB/Wi-Fi syncing to **MyLibrary OPDS + WebDAV**, removing the old local books on the device without losing a single reading position or bookmark:

### Phase 1: Sync & Back Up Your Current Reading Positions to WebDAV (Before Deleting Anything)
1. **On the Physical Device (Moon+ Reader):**
   - Open **Moon+ Reader** -> tap **Options (Left Menu -> Options)** -> **Cloud Backup / Sync -> WebDAV**.
   - Tap **Sync Now** (or **Upload**) one final time so the device pushes its latest `.po` (reading position) and `.an` (bookmarks/highlights) files to `U:\webdav\Apps\Books\.Moon+\Cache`.
2. **On Your PC (Backup & Pre-Migrate WebDAV Bookmarks):**
   - Run the Moon+ WebDAV bookmark sync in [`calibre_moon_sync.py`](file:///c:/Users/oscar/Repo_Pers/notgithub/MyLibrary/calibre_moon_sync.py):
     ```powershell
     python calibre_moon_sync.py moon-only --apply
     ```
   - This automatically backs up all current `.po` and `.an` files into `U:\webdav\Apps\Books\.Moon+\Cache\_SyncBackup` and creates updated `.po` and `.an` files for any books whose titles/filenames changed during `epubmeta` enrichment.

### Phase 2: Remove the Old Books from the Physical Device
1. **In Moon+ Reader (`My Shelf` & `My Files`):**
   - Open **My Shelf** in Moon+ Reader.
   - To remove individual books or all old Calibre-synced books:
     - **Option A (Clean Slate for the Device's Local Books Folder):** Open your Android File Manager (or Moon+ Reader -> **My Files**) and navigate to your local books folder on the tablet (e.g., `/sdcard/Books/` or `/storage/emulated/0/Books/` where Calibre previously placed `.epub` files) and delete the old `.epub` files.
     - **Option B (Inside Moon+ Reader):** Long-press a book (or tap the 3-dot menu -> **Manage Books** -> **Select All**), tap **Delete**, and check both **"Remove from My shelf"** and **"Delete File"**.
   - **Important Note About WebDAV Bookmarks:** Deleting the `.epub` file on your Android device **does NOT delete** your `.po` or `.an` bookmark files on the NAS WebDAV server (`U:\webdav\Apps\Books\.Moon+\Cache`), and you also have a full backup in `_SyncBackup`.

### Phase 3: Connect Moon+ Reader to the MyLibrary OPDS Server
1. Open **Moon+ Reader** on your device and go to **Net Library** (in the left navigation drawer).
2. Tap the **`+` (Add new catalog)** button at the top right:
   - **Catalog title:** `MyLibrary NAS`
   - **Catalog URL (on Home Wi-Fi):** `http://192.168.1.43:8080/mylibrary/opds/`
     *(If connecting from outside your home network via your router/static IP, enter `http://<YOUR_STATIC_PUBLIC_IP>:8080/mylibrary/opds/` and fill in your `AUTH_USERNAME` and `AUTH_PASSWORD`).*
3. Tap **OK**, then tap **MyLibrary NAS** to open your server catalog!
4. You will immediately see your shelves:
   - **Bookmarked & Reading in Moon+** *(Dynamic virtual shelf of all books with active Moon+ WebDAV bookmarks/reading progress)*
   - **Browse by Enriched Tags & Collections** *(All subject tags & folder collections from enriched EPUBs)*
   - **Enriched EPUBs (epubmeta Verified)**
   - **Recently Updated & Enriched Books**
   - **Browse by Author (A–Z)**
   - **Browse by Format (EPUB, PDF, MOBI, Audiobook)**
   - **All Catalog Books (A–Z)**
   - Plus the **Search (magnifying glass)** icon at the top right to search all 1.15M+ books.

### Phase 4: Download Books & Resume Reading Seamlessly
1. Browse or search for any book in **Net Library -> MyLibrary NAS** and tap **Download**.
2. MyLibrary streams the enriched `.epub` to your device as `<Title> - <Author>.epub` and automatically ensures the matching `.po` and `.an` bookmark files exist in `/webdav/Apps/Books/.Moon+/Cache`.
3. Tap **Read** when the download completes. Moon+ Reader automatically checks your WebDAV cache for `<Title> - <Author>.epub.po` / `.an` and prompts to jump straight to your saved reading position and highlights!

---

## 9. Configuring Your Netgear Router for Secure External Access (Fixed Public IP)

Because you have a **fixed public IP**, you can access the MyLibrary Web UI, Status Dashboard, and OPDS Server from anywhere outside your home network while keeping your database and NAS protected.

### Step 1: Reserve Static LAN IP `192.168.1.43` for Your NAS
1. Log into your Netgear Router admin page (`http://192.168.1.1` or `http://routerlogin.net`).
2. Go to **Advanced -> Setup -> LAN Setup -> Address Reservation**.
3. Ensure your NAS MAC address is reserved to **`192.168.1.43`** so its internal IP never changes.

### Step 2: Add the Port Forwarding Rule on Your Netgear Router
You have two port-forwarding options depending on whether you want to expose **only MyLibrary** or the **DevStack Nginx Gateway**:

- **Option A — Recommended: Forward an External High Port Directly to `mylibrary-web` (or Nginx)**
  - Go to **Advanced -> Advanced Setup -> Port Forwarding / Port Triggering**.
  - Click **Add Custom Service**:
    - **Service Name:** `MyLibrary-External`
    - **Protocol:** `TCP`
    - **External Port Range:** `8080` (or a custom high port like `18080` to avoid automated internet port scanners)
    - **Internal Port Range:** `8080` (DevStack Nginx, which passes `X-Forwarded-For` to `mylibrary-web`)
    - **Internal IP Address:** `192.168.1.43`
  - Click **Apply**.
  - *(Security Note: **Never** forward PostgreSQL `5433`, Redis `6379`, pgAdmin `5050`, or SSH `22` to the public internet).*

- **Option B — HTTPS with TLS Encryption (Synology Reverse Proxy / Nginx TLS)**
  - If your NAS has Synology **Control Panel -> Login Portal -> Advanced -> Reverse Proxy** (or a Let's Encrypt certificate):
    1. Create a Reverse Proxy rule from **HTTPS (`443` or `8443`)** -> **Destination `http://localhost:8080`**.
    2. Forward external TCP port `8443` (or `443`) on your Netgear router to `192.168.1.43`.
    3. This encrypts your password and book downloads over TLS (`https://<YOUR_FIXED_PUBLIC_IP>:8443/mylibrary/`).

### Step 3: Set Your External Password in `.env`
In `/volume1/common/MyLibrary/.env`, you can customize your login credentials at any time:
```ini
WEB_AUTH_MODE=external
OPDS_AUTH_MODE=external
AUTH_USERNAME=oscar
AUTH_PASSWORD=YourStrongPasswordHere
```
- **On Home Wi-Fi (`192.168.1.x`):** Zero login prompts (`http://192.168.1.43:8080/mylibrary/`).
- **Outside Your Network (`http://<YOUR_FIXED_PUBLIC_IP>:8080/mylibrary/`):**
  - Opening the web page in a browser redirects to `/mylibrary/login/`.
  - Connecting from Moon+ Reader (`http://<YOUR_FIXED_PUBLIC_IP>:8080/mylibrary/opds/`) prompts for your `AUTH_USERNAME` and `AUTH_PASSWORD` via HTTP Basic Auth and blocks any IP for 15 minutes after 8 failed attempts.


