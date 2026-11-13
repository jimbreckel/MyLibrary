# MyLibrary — Unified Ebook & Media Catalog, Enricher, OPDS/Calibre Server, CLI & MCP Server

**MyLibrary** is a full-stack digital library catalog, automated metadata enrichment pipeline (`epubmeta`), **OPDS 1.2 & Calibre Content Server emulator**, REST API, Command-Line Interface ([`mylibrary_cli.py`](file:///c:/Users/oscar/Repo_Pers/notgithub/MyLibrary/mylibrary_cli.py)), Model Context Protocol server ([`mylibrary_mcp.py`](file:///c:/Users/oscar/Repo_Pers/notgithub/MyLibrary/mylibrary_mcp.py)), and Calibre/Moon+ Reader sync suite ([`calibre_moon_sync.py`](file:///c:/Users/oscar/Repo_Pers/notgithub/MyLibrary/calibre_moon_sync.py)) managing **1,152,640+ catalog items** and **1,518,702+ physical files** on your NAS.

---

## Quick Links & Access Points

| Interface | URL / Entrypoint | Purpose |
| :--- | :--- | :--- |
| **Web Catalog Search** | `http://192.168.1.43:8080/mylibrary/` | Search all 1.15M+ books, magazines, Kindle & Audible titles by title, author, ISBN, ASIN, publisher, or filename, with one-click **✨ Enrich Now** on-demand enrichment |
| **Pipeline & Status Dashboard** | `http://192.168.1.43:8080/mylibrary/status/` | Live scanner status, next-scan countdown, remaining queues, interactive time-series charts, and `epubmeta` review explorer |
| **OPDS 1.2 & Calibre Content Server** | `http://192.168.1.43:8080/mylibrary/opds/?v=2` | Native e-reader catalog for **Moon+ Reader**, KOReader, Calibre Sync, and PocketBook with **automatic on-demand enrichment on download** and Moon+ WebDAV bookmark migration |
| **Enriched Duplicates Manager** | `http://192.168.1.43:8080/mylibrary/duplicates/` | Finds duplicate copies of the same format and verified content, automatically scoring the best copy to keep while protecting unfinished Moon+ Reader bookmarks |
| **Chrome Extension (Book Sites, Wishlists & Anna's Archive)** | [`chrome_wishlist_extension/`](file:///c:/Users/jpbr230/Repo_Pers/notgithub/MyLibrary/chrome_wishlist_extension) | Checks Amazon Wishlists (highlighting `🏷️ ON SALE` price drops) & Book Pages across Amazon, Barnes & Noble, Simon & Schuster, Springer Link, Goodreads, O'Reilly, No Starch Press, and custom sites against MyLibrary and Anna's Archive |
| **Amazon Kindle, Kindle+ & Audible Sync** | [`scanner/sync_amazon.py`](file:///c:/Users/jpbr230/Repo_Pers/notgithub/MyLibrary/scanner/sync_amazon.py) | Automated Google Chrome sync for Amazon Kindle purchases, Kindle+ (KU/Prime), Kindle Bookmarks/Highlights, and Audible Audiobooks |
| **External Web Login** | `http://192.168.1.43:8080/mylibrary/login/` | Automatic security guard when accessing MyLibrary outside your home LAN (with HTTP Basic Auth for OPDS/APIs) |
| **Calibre & Moon+ Sync Utility** | [`calibre_moon_sync.py`](file:///c:/Users/jpbr230/Repo_Pers/notgithub/MyLibrary/calibre_moon_sync.py) | Standalone utility (`moon-only`, `pass1`, `pass2`, `status`, `inspect-an`) for syncing desktop `I:\Calibre Library` and `U:\webdav\Apps\Books\.Moon+\Cache` |
| **CLI & Python SDK** | [`mylibrary_cli.py`](file:///c:/Users/jpbr230/Repo_Pers/notgithub/MyLibrary/mylibrary_cli.py) | Terminal commands (`status`, `search`, `book`, `update-book`, `suggest`, `enrichments`, `approve`, `ignore`, `requeue`, `history`, `logs`, `help`) |
| **MCP Server** | [`mylibrary_mcp.py`](file:///c:/Users/jpbr230/Repo_Pers/notgithub/MyLibrary/mylibrary_mcp.py) | 11 MCP Tools & 2 Resources over stdio JSON-RPC 2.0 for AI agents (Antigravity, Claude Desktop, Cursor) |
| **REST API** | `/mylibrary/api/...` | JSON endpoints for catalog search, book details/editing, pipeline status, enrichment review, batch Calibre/Moon+ lookup, history curves, and logs |

---

## 1. Web Dashboards & External Security

### A. Catalog Search & On-Demand Enrichment (`/mylibrary/`)
- **Full-Catalog Search Across All Books:** Search across titles, authors, ISBN/ASINs, publishers, and physical filenames (`files__file_name`) with instant autocomplete (`/mylibrary/api/suggest/`) and structured search operators (`author:`, `title:`, `cat:`, `tag:`, `pub:`, `isbn:`, `format:`, `year:`, `is:enriched`, `is:unenriched`, `is:interesting`, `is:badmeta`, `missing:cover`, `-exclude`, `sort:newest`).
- **One-Click On-Demand Enrichment (`✨ Enrich Now`):**
  - Click **`✨ Enrich Now`** directly on any book card or inside **View Details** (or click **`✨ Enrich File`** on a specific file location) to run `epubmeta` on demand (`POST /mylibrary/api/enrich/`).
  - Or check **"Enrich search results on demand"** beneath the search bar (or append `+enrich` to your search query) to automatically enrich top unenriched search results as you search!
  - Rewrites the `.epub` OPF / `.pdf` XMP metadata on the NAS (with backup in `/books_bad/bak`), records `SUCCESS` in `scanner_state.db`, and updates the card's title, author, ISBN, cover, and synopsis in-place.
- **Cover Intelligence & White-Border Auto-Crop:**
  - Automatically detects and crops excessive white/light borders around book covers in PDFs, EPUBs, and downloaded web covers (with background batch repair available via `GET /mylibrary/api/suggest/?repair_covers=start`).
  - Automatically prefers **full-color covers** over grayscale/black-and-white scans during Web Cover Search (`🖼 Find Cover`) and `epubmeta` enrichment.
  - Displays read-only **Tags** at the bottom of both the **Book Details Modal** and the **Enrichment Review Modal**.
- **Virtual Bookshelves:** Browse `Interesting Books`, `Bad Metadata`, `Bookmarked & Reading in Moon+` (with `100% Read`, `Unread <100%`, and `Reading 1–99%` progress filters), `Highlights & Notes (.an)`, `Enriched Books`, `Missing / Text Cover`, `Enriched Duplicates`, `Needs Enriching`, `In Review`, `Removed Protection (nodrm)`, **`Packt Books`** (`Computer Mags & FAQ/978`, sorted by most recent file modification date first), `Moved to toMags`, `Recently Published (1 Yr)`, `Purchased on Amazon`, `Kindle+ (KU / Prime)`, `Amazon Bookmarked`, `Purchased on Audible`, `Amazon Only (Not on NAS)`, and `Matched to NAS Books`.
- **Comma-Formatted Metrics & Faceted Sidebar Filters:** Filter by **Catalog Type** (`BOOK`, `MAGAZINE`, `KINDLE`, `AUDIBLE`, `AUDIOBOOK`), **File Format** (`EPUB`, `PDF`, `MOBI`, `AZW3`, `MP3`, `M4B`), **Enriched Tags**, and **Enriched Collections**.

### B. Conversion & Import Status Dashboard (`/mylibrary/status/`)
- **Live Pipeline Activity Banner:**
  - Shows whether the scanner is **Active** (scanning) or in **Standby** (waiting between 4-hour cycles).
  - Displays the **Next Scan** timestamp in your local browser time with a live 1-second ticking countdown.
- **Remaining Queue Counters (Comma-Formatted):**
  - **Remaining to Catalog:** Shows `0` when all `1,527,029` discovered NAS files are indexed in `file_states` / `BookDB`, or live chunk progress (`Chunk X/Y`) when new files are being extracted.
  - **Remaining to Enrich:** Live countdown of eligible `.epub` files remaining in the backfill queue (`~594,417`), current cycle lookup progress (`1,840 / 2,500`), and count of files already complete in `BookDB` (`269,011`).
  - **Priority Handling for New Books:** Newly added or modified files in `/books` or `/audiobooks` automatically jump to the front of the queue on the very next scan cycle.
- **Interactive Time-Series Charts:**
  - Click any number on the **`epubmeta Enrichments`** card (`All Processed`, `Enriched`, `Review`, `No ISBN`, `Quarantine`) or **`Local Metadata Cache`** to open the Chart.js history modal.
  - Toggle between **Cumulative Total** (line/area chart) and **Per-Hour / Day Rate** (bar chart), **Hourly** vs. **Daily** buckets, or **Compare All** series on one graph.
- **epubmeta Enrichments & Quarantine Explorer:**
  - Filter by status (`All`, `Enriched`, `Needs Review`, `No ISBN`, `Quarantined`, `Failed`, `Ignored`) and search by filename, path, title, author, or ISBN.
  - **Configurable Path Prefix (`U:\Media`):** Automatically maps container paths (`/books/...`) to your local Windows network drive (`U:\Media\books\...`) with one-click **Copy Full Path** and **Copy Folder** buttons.
  - **Mix-and-Match Review Modal:** View **Before vs. After** cover art, title, author, publisher, ISBN, and description side-by-side. Click any individual field on either side to cherry-pick which fields to keep, edit values directly in the **Selected Fields to Save** box, and click **Approve Selected Fields**, **Keep Original (Ignore)**, or **Re-queue for Next Scan**.

### C. External Network Security ([`catalog/security.py`](file:///c:/Users/oscar/Repo_Pers/notgithub/MyLibrary/catalog/security.py))
- **Zero Friction on Home LAN (`WEB_AUTH_MODE=external`, `OPDS_AUTH_MODE=external`):** Requests from `192.168.x.x`, `10.x.x.x`, `172.16-31.x.x`, or Tailscale (`100.64.0.0/10`) open directly without a login prompt.
- **Automatic Protection Outside Your Network:** When accessed externally (via reverse proxy, port forward, or Cloudflare Tunnel):
  - **Web browsers** are redirected (`HTTP 302`) to `/mylibrary/login/`.
  - **OPDS e-readers & API clients** receive `HTTP 401 Unauthorized` (`WWW-Authenticate: Basic realm="MyLibrary OPDS & Catalog"`), allowing Moon+ Reader to sign in with your `AUTH_USERNAME` and `AUTH_PASSWORD`.
  - **Brute-Force Lockout:** Automatically blocks any IP for 15 minutes after 8 failed password attempts.

---

## 2. OPDS 1.2 & Calibre Content Server (`http://192.168.1.43:8080/mylibrary/opds/?v=2`)

MyLibrary natively emulates an **OPDS 1.2 Catalog** and **Calibre Content Server** ([`catalog/opds.py`](file:///c:/Users/oscar/Repo_Pers/notgithub/MyLibrary/catalog/opds.py)) so physical tablets/phones running **Moon+ Reader** can browse, search all books, and download books directly from the NAS without needing desktop Calibre:
- **OPDS Shelves:**
  - `Interesting Books (Starred to Read/Download)` (`/mylibrary/opds/interesting/`)
  - `Flagged: Bad Metadata` (`/mylibrary/opds/bad-metadata/`)
  - `Bookmarked & Reading in Moon+` (`/mylibrary/opds/bookmarked/`)
  - `Purchased on Amazon, Kindle+ & Audible` (`/mylibrary/opds/amazon/`, including `Amazon Only (Not on NAS)` and `Matched to NAS Books`)
  - `Packt Books (Computer Mags & FAQ / 978 — Most Recent File Date First)` (`/mylibrary/opds/packt/`)
  - `Browse Enriched Books by Tag / Subject` (`/mylibrary/opds/tags/`)
  - `Enriched EPUBs (epubmeta Verified)` (`/mylibrary/opds/enriched/`)
  - `Recently Updated & Enriched Books` (`/mylibrary/opds/recent/`)
  - `Browse by Author (A-Z)` (`/mylibrary/opds/authors/`)
  - `Browse by Format (EPUB, PDF, MOBI, AZW3, MP3, M4B)` (`/mylibrary/opds/formats/`)
  - `All Catalog Books (A-Z)` (`/mylibrary/opds/all/`)
  - `OpenSearch Across All Books` (`/mylibrary/opds/search/?q=...` — searches title, author, ISBN, ASIN, publisher, and filename; append `+enrich` to enrich top unenriched search results on demand!)
- **Automatic On-Demand Enrichment on Download + Moon+ Reader Bookmark Migration:**
  - When you download any `.epub` or `.pdf` book (`/mylibrary/opds/download/<id>/<Title - Author.epub>`), if it is not already enriched in `scanner_state.db`, MyLibrary **automatically runs `epubmeta` on-demand enrichment on the fly**, updates the `.epub` OPF metadata on disk and `BookDB`, and streams the newly enriched file with the Calibre-normalized filename (`<Title> - <Author>.epub`).
  - You can also tap the explicit **`✨ Enrich & Download`** acquisition link (`?enrich=1`) on any unenriched book entry.
  - At download time, MyLibrary automatically checks `/webdav/Apps/Books/.Moon+/Cache` (`U:\webdav\Apps\Books\.Moon+\Cache`) and copies/rewrites any matching `.po` (reading position) and `zlib`-compressed `.an` (highlights/bookmarks) files to the new filename!

---

## 3. Calibre & Moon+ Reader Sync Utility (`calibre_moon_sync.py`)

For updating a desktop Calibre library (`I:\Calibre Library`) and/or migrating Moon+ Reader WebDAV bookmarks (`U:\webdav\Apps\Books\.Moon+\Cache`):

```powershell
# 1. Migrate Moon+ Reader WebDAV bookmarks (.po and zlib .an) for all enriched books (dry-run first, then --apply)
python calibre_moon_sync.py moon-only
python calibre_moon_sync.py moon-only --apply

# 2. Two-Pass Desktop Calibre + Device Sync Workflow:
#    Pass 1: Snapshot Moon+ bookmarks into _EnrichStaging and tag outdated books in Calibre (ENRICH_1_REMOVE_FROM_DEVICE)
python calibre_moon_sync.py pass1 --apply
#    (Remove tagged books from your connected device in Calibre)
#    Pass 2: Replace old Calibre entries with enriched EPUBs and write updated Moon+ bookmarks to WebDAV
python calibre_moon_sync.py pass2 --apply

# 3. Inspect decompressed contents of any Moon+ Reader .an bookmark file
python calibre_moon_sync.py inspect-an "Death_s End"
```

---

## 4. Command-Line Interface (`mylibrary_cli.py`)

Run `python mylibrary_cli.py help` from anywhere. Add `--json` to any command for machine-readable JSON output.

```powershell
python mylibrary_cli.py help
python mylibrary_cli.py status
python mylibrary_cli.py search "Dune" --format EPUB --limit 10
python mylibrary_cli.py book 29b1bc0f-e3f3-4b68-82c3-33549aed7a96
python mylibrary_cli.py update-book 29b1bc0f-e3f3-4b68-82c3-33549aed7a96 --authors "Frank Herbert"
python mylibrary_cli.py suggest "Asimov"
python mylibrary_cli.py enrichments --status NEEDS_REVIEW --limit 20
python mylibrary_cli.py approve "U:\Media\books\...\Book.epub" --title "Custom Title" --isbn "978..."
python mylibrary_cli.py ignore "/books/..."
python mylibrary_cli.py requeue "/books/..."
python mylibrary_cli.py history --granularity hour --series SUCCESS
python mylibrary_cli.py logs --source both --lines 40 --grep "SUCCESS"
```

---

## 5. Model Context Protocol (MCP) Server (`mylibrary_mcp.py`)

`mylibrary_mcp.py` is a zero-dependency stdio JSON-RPC 2.0 MCP server exposing 11 tools and 2 resources.

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

---

## 6. Documentation Index
- **[User Documentation](file:///c:/Users/oscar/Repo_Pers/notgithub/MyLibrary/documentation/user_documentation.md)** — Complete guide to the Web UI, Status Dashboard, OPDS Server, Moon+ Reader Device Migration Plan, CLI, and MCP Server.
- **[Developer & Architecture Guide](file:///c:/Users/oscar/Repo_Pers/notgithub/MyLibrary/documentation/developer_documentation.md)** — System architecture, database schemas (`BookDB` + `scanner_state.db`), OPDS/Security internals, REST API specification, and scanner internals.
- **[Deployment Guide](file:///c:/Users/oscar/Repo_Pers/notgithub/MyLibrary/documentation/deployment.md)** — Docker Compose and NAS deployment instructions.
