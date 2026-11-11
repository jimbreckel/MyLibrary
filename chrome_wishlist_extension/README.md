# MyLibrary — Book Sites, Amazon Wishlist & Anna's Archive Chrome Extension

Manifest V3 Chrome Extension that connects **Amazon Wishlists**, **Amazon Book Pages**, **Barnes & Noble**, **Springer Link**, **Goodreads**, **O'Reilly**, **No Starch Press**, and any user-added book site directly to your **NAS MyLibrary Catalog (`http://192.168.1.43:8080/mylibrary`)** and **Anna's Archive**.

---

## 1. Installation / Reloading in Google Chrome

1. Open Google Chrome and navigate to **`chrome://extensions/`**.
2. Enable **Developer mode** (toggle switch in the top-right corner).
3. Click **Load unpacked** and select:
   - Local repo folder: `c:\Users\jpbr230\Repo_Pers\notgithub\MyLibrary\chrome_wishlist_extension`
   - Or NAS share folder: `U:\MyLibrary\chrome_wishlist_extension`
4. Pin the **MyLibrary** book+checkmark icon in your Chrome toolbar for one-click access to settings and quick links.

---

## 2. Features

### A. Amazon Wishlist Scanner (`Books (Kindle)`, `Print Books`, etc.)
- **Automatic Batch Lookup (`POST /mylibrary/api/wishlist/check/`)**:
  - Scans visible wishlist items and checks each book against your NAS `BookDB` (`CatalogItem` + `FileLocation`) and `scanner_state.db`.
  - Highlights your **`Books (Kindle)`** and **`Print Books`** lists in the left wishlist sidebar.
- **Edition & Publication-Year Aware Verification (Prevents Same-Title False Positives)**:
  - Preserves edition/volume markers (`2nd Edition`, `3rd Edition`, `Vol. 2`, etc.).
  - When a wishlist item matches `BookDB` only by title/author (for example, a Kindle `B0...` ASIN without a visible print ISBN on the wishlist row), the extension automatically fetches `/dp/<ASIN>` in the background to extract the true **Print ISBN-13/10**, **Publication Year**, **Edition**, and **Subtitle**, and re-verifies with MyLibrary.
  - If MyLibrary only has an older/different edition with the same title, it marks the item **`❌ Not in MyLibrary`** and displays an informational note (`ℹ️ Different book/edition with same title in Library`) with a link to view the older edition.
  - Includes a **`✕ False Positive`** button (with **`↩ Undo`**) saved in `chrome.storage.local` to manually dismiss any match.
- **Sticky Top Wishlist Toolbar**:
  - Filter the wishlist in-place by **`Scanned`**, **`✅ In MyLibrary`**, **`❌ Not in Library`**, or **`🔎 On Anna's Archive`**.
  - **`🔄 Check Library`**: Re-verify all visible items.
  - **`⏬ Auto-Scroll Entire Wishlist`**: Smoothly scrolls to the bottom of multi-page wishlists and checks every book automatically.
  - **`📚 Check Anna's Archive (Missing)`**: Checks all missing wishlist books on Anna's Archive by **ISBN (if available) AND Title**.
  - **`📥 Export CSV`**: Exports a full CSV report of wishlist items, ISBNs, MyLibrary ownership/formats, and Anna's Archive status.

### B. Book Product Page Indicator & Action Buttons (Replaces Tampermonkey Scripts)
Automatically activates on supported book pages:
- **Amazon Books** (`amazon.com`, `.co.uk`, `.ca`, `.de`, etc. on `/dp/*` and `/gp/product/*`)
- **Barnes & Noble** (`barnesandnoble.com/w/*`)
- **Springer Link** (`link.springer.com/book/*`)
- **Goodreads**, **O'Reilly Learning**, and **No Starch Press**

On every book page, the extension adds:
1. **Inline Title Status Pill** right next to the book's `<h1>` title:
   - **`✅ In MyLibrary (EPUB, PDF)`** (green pill linking to the book in MyLibrary)
   - **`⚠️ Different Edition in MyLibrary`** or **`❌ Not in MyLibrary`** (red pill)
2. **Unified Action Panel** right below the book cover image:
   - **Live MyLibrary Status Banner** with direct **View** and **[Download EPUB/PDF]** links when owned, plus **`✕ False Positive`** toggle.
   - **`Open in Anna's Archive (<ISBN-13>)`** (blue `#0056b3` button — opens `<annasBaseUrl>/search?index=&page=1&q=<isbn13>`).
   - **`Search in Anna's Archive`** (pink `#e11d74` button — opens `<annasBaseUrl>/search?index=&page=1&q=<title>`).
   - **`🔍 Check Anna's Live`** (checks Anna's Archive in the background by ISBN + Title and displays match count, file formats, and a direct **⬇ Top Match** link).
   - **Optional `Open in Z-Library`** (green `#377458` button — toggleable in popup settings).

### C. Popup Settings, One-Click Anna's Archive Mirror Switcher & Custom Book Sites
Click the extension icon in the Chrome toolbar to:
- **Switch Anna's Archive Domain with 1 Click**: Click `.pk`, `.gl`, `.li`, `.se`, `.org`, or type any custom mirror URL. Open tabs update immediately.
- **Click Any Active Book Site Pill**: Clicking **`✓ Amazon Books & Wishlists ↗`**, **`✓ Barnes & Noble ↗`**, **`✓ Springer Link ↗`**, **`✓ Goodreads ↗`**, **`✓ O'Reilly ↗`**, **`✓ No Starch ↗`**, or any custom site badge opens that site in a new tab.
- **Add Custom Book Sites**: Expand **`➕ Add Another Book Site`** in the popup to add any publisher or bookstore domain by hostname and CSS selectors (sites with `<meta name="citation_isbn">` or Schema.org `Book` JSON-LD extract ISBN-13 automatically).
- **Or Add Built-In Adapters in Code**: Add a 10-line entry to `BUILTIN_SITE_ADAPTERS` in [`site_adapters.js`](file:///c:/Users/jpbr230/Repo_Pers/notgithub/MyLibrary/chrome_wishlist_extension/site_adapters.js).

