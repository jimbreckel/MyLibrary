# MyLibrary Catalog Server Connection Settings

Quick reference settings for adding the **MyLibrary Catalog Server** ([`core/settings.py`](file:///c:/Users/jpbr230/Repo_Pers/notgithub/MyLibrary/core/settings.py) and [`catalog/opds.py`](file:///c:/Users/jpbr230/Repo_Pers/notgithub/MyLibrary/catalog/opds.py)) to another device, e-reader, browser, or computer.

---

## 1. E-Reader / OPDS Catalog Settings (Moon+ Reader, KOReader, Calibre Sync, PocketBook)

In **Moon+ Reader** (go to **Net Library** -> tap **`+`** to add a new catalog):

| Setting | Value |
| :--- | :--- |
| **Catalog Title** | `MyLibrary NAS` |
| **Catalog URL (Home Wi-Fi / LAN)** | `http://192.168.1.43:8080/mylibrary/opds/` *(or `http://192.168.1.43:8080/mylibrary/opds/?v=2`)* |
| **Direct Port URL (if bypassing Nginx)** | `http://192.168.1.43:8000/mylibrary/opds/` |
| **Authentication (on Home LAN / Tailscale)** | None required (`OPDS_AUTH_MODE=external`) |
| **Username (External / Remote Access)** | `oscar` (configured via `AUTH_USERNAME` in `.env`) |
| **Password (External / Remote Access)** | Configured via `AUTH_PASSWORD` in `.env` |

### Direct Shelf / Feed URLs (Optional)
- **Bookmarked & Reading in Moon+:** `http://192.168.1.43:8080/mylibrary/opds/bookmarked/`
- **Purchased on Amazon / Kindle+ / Audible:** `http://192.168.1.43:8080/mylibrary/opds/amazon/`
- **Browse Enriched Books by Tag / Subject:** `http://192.168.1.43:8080/mylibrary/opds/tags/`
- **Enriched EPUBs (`epubmeta` Verified):** `http://192.168.1.43:8080/mylibrary/opds/enriched/`
- **Recently Updated & Enriched:** `http://192.168.1.43:8080/mylibrary/opds/recent/`
- **Browse by Author (A–Z):** `http://192.168.1.43:8080/mylibrary/opds/authors/`
- **Browse by Format (`EPUB`, `PDF`, `MOBI`, `AZW3`, `MP3`, `M4B`):** `http://192.168.1.43:8080/mylibrary/opds/formats/`
- **All Catalog Books (A–Z):** `http://192.168.1.43:8080/mylibrary/opds/all/`
- **OpenSearch URL:** `http://192.168.1.43:8080/mylibrary/opds/search/?q={searchTerms}`

---

## 2. Web Browser Access (Any Tablet, Phone, or PC)

- **Main Catalog Search:** `http://192.168.1.43:8080/mylibrary/`
- **Pipeline & Enrichment Status Dashboard:** `http://192.168.1.43:8080/mylibrary/status/`
- **External Login Page:** `http://192.168.1.43:8080/mylibrary/login/` (credentials configured in `.env`)

---

## 3. CLI / MCP Server Settings (Another PC / AI Agent)

If connecting [`mylibrary_cli.py`](file:///c:/Users/jpbr230/Repo_Pers/notgithub/MyLibrary/mylibrary_cli.py) or [`mylibrary_mcp.py`](file:///c:/Users/jpbr230/Repo_Pers/notgithub/MyLibrary/mylibrary_mcp.py) from another computer:

```json
{
  "mcpServers": {
    "mylibrary": {
      "command": "python",
      "args": [
        "c:\\path\\to\\MyLibrary\\mylibrary_mcp.py"
      ],
      "env": {
        "MYLIBRARY_URL": "http://192.168.1.43:8080/mylibrary",
        "MYLIBRARY_PATH_PREFIX": "U:\\Media"
      }
    }
  }
}
```

