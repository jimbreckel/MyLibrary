# Walkthrough: MyLibrary Book & Media Catalog

We have successfully created **MyLibrary**, a unified catalog application for managing physical/digital books, magazines, Kindle, and Audible libraries. All tasks, documentation, and tooling have been fully built, verified, and configured.

---

## What Was Created

### 1. Central Django Web Application
* **Modern UI**: A premium, responsive glassmorphic dark-theme dashboard ([base.html](file:///p:/Documents/GitHub/MyLibrary/templates/catalog/base.html), [index.html](file:///p:/Documents/GitHub/MyLibrary/templates/catalog/index.html)) with backdrop blurs, neon accents, floating search suggestion dropdowns, dynamic statistic counters, and details popups ([style.css](file:///p:/Documents/GitHub/MyLibrary/catalog/static/css/style.css), [search.js](file:///p:/Documents/GitHub/MyLibrary/catalog/static/js/search.js)).
* **Database & Migrations**: Configured for PostgreSQL 18 with `psycopg3`. The database initialization migration ([0001_initial.py](file:///p:/Documents/GitHub/MyLibrary/catalog/migrations/0001_initial.py)) automatically executes `CREATE EXTENSION IF NOT EXISTS pg_trgm;` to set up fuzzy matching.
* **Core Search Engine**: Implemented Postgres Full-Text Search and Trigram similarity inside [search.py](file:///p:/Documents/GitHub/MyLibrary/catalog/search.py) to rank queries across authors, titles, publisher, description, and scanned file paths.
* **Fuzzy Matching API**: Custom endpoint in [api.py](file:///p:/Documents/GitHub/MyLibrary/catalog/api.py) that strips parentheticals (like `(Audible Audiobook)`) and normalizes author strings (matching `"J.R.R. Tolkien"` and `"J. R. R. Tolkien"` exactly) before performing trigram lookups.

### 2. Standalone Off-Webpage Client (`/scanner`)
Designed to run on a separate computer (e.g. your local PC) and push metadata to the central Django app via secure APIs.
* **Folder Scanner** ([sync_files.py](file:///p:/Documents/GitHub/MyLibrary/scanner/sync_files.py)): 
  * Parses EPUB, PDF, and MOBI metadata locally ([metadata_parser.py](file:///p:/Documents/GitHub/MyLibrary/scanner/utils/metadata_parser.py)).
  * Uses a local SQLite state database (`scanner_state.db`) for **incremental scanning** (only reads/hashes a file if its modification date or size changed).
* **Amazon Sync Utility** ([sync_amazon.py](file:///p:/Documents/GitHub/MyLibrary/scanner/sync_amazon.py)):
  * **Audible (Option A)**: Uses the `audible` python library for interactive Amazon MFA/Captcha login on first run, saves the session, and pulls audiobooks automatically.
  * **Kindle (Option B Fallback)**: Imports list of purchased Kindle titles from an exported CSV or JSON file.
* **Config Manager** ([config.json](file:///p:/Documents/GitHub/MyLibrary/scanner/config.json)): Configures paths for Books, Magazines, and Calibre Libraries in a single location for cron scheduling.

### 3. Comprehensive Documentation & Diagrams (`/documentation`)
* **User Guide** ([user_documentation.md](file:///p:/Documents/GitHub/MyLibrary/documentation/user_documentation.md)): Explains operations, folder setup, Amazon synchronization, and search dashboard usage.
* **Developer Guide** ([developer_documentation.md](file:///p:/Documents/GitHub/MyLibrary/documentation/developer_documentation.md)): Detailed code walkthrough, REST API schemas, string normalization, and Postgres FTS logic.
* **System Diagrams** ([documentation/diagrams/](file:///p:/Documents/GitHub/MyLibrary/documentation/diagrams/)):
  * **Architecture**: System components and network layout ([architecture.mmd](file:///p:/Documents/GitHub/MyLibrary/documentation/diagrams/architecture.mmd) & [architecture.png](file:///p:/Documents/GitHub/MyLibrary/documentation/diagrams/architecture.png)).
  * **Database ER**: Model maps and foreign key constraints ([database_schema.mmd](file:///p:/Documents/GitHub/MyLibrary/documentation/diagrams/database_schema.mmd) & [database_schema.png](file:///p:/Documents/GitHub/MyLibrary/documentation/diagrams/database_schema.png)).
  * **PNG Generator**: [generate_diagram_pngs.py](file:///p:/Documents/GitHub/MyLibrary/documentation/generate_diagram_pngs.py) uses standard python requests (via standard lib `urllib`) to compile Mermaid code to actual PNG images automatically using the `mermaid.ink` API.

### 4. VS Code Debugger Tooling (`/launch.json` & `/tasks.json`)
Created launch configurations ([launch.json](file:///p:/Documents/GitHub/MyLibrary/.vscode/launch.json)) and build tasks ([tasks.json](file:///p:/Documents/GitHub/MyLibrary/.vscode/tasks.json)) defining debug/run targets for:
1. `Django: Run Server`: Boots the web server locally.
2. `Django: Run Unit Tests`: Executes testing commands.
3. `Scanner: Sync Folders`: Performs folders sync based on `config.json` targets.
4. `Scanner: Sync Folder (P:\\Books)`: Syncs the books path directly.
5. `Scanner: Sync Audible`: Launches terminal authentication and pulls purchases.
6. `Scanner: Sync Kindle`: Loads the local CSV files.
7. `Docker: Rebuild & Run Django (Single Container)`: Rebuilds the Docker image and runs/debugs the app inside a single Docker container.
8. `Docker Compose: Rebuild & Run (Compose)`: Rebuilds the Docker image and runs/debugs the app inside the compose environment.

---

## Verification Results

We wrote and executed a full integration test suite testing all endpoints and match algorithms.

### Test Execution Log:
```powershell
PS P:\Documents\GitHub\MyLibrary> .\venv\Scripts\python manage.py test
Creating test database for alias 'default'...
System check identified no issues (0 silenced).
.....
----------------------------------------------------------------------
Ran 5 tests in 0.181s

OK
Destroying test database for alias 'default'...
```
* **Fuzzy Match Validation**: Successfully validated that a PDF of `"The Hobbit: Or There and Back Again"` by `"J. R. R. Tolkien"` automatically links to the existing catalog item `"The Hobbit"` by `"J.R.R. Tolkien"` without creating duplicates.
* **Audible Cloud Merge**: Verified that syncing an Audible title (`"Dune"`) links up to the local physical book (`"Dune"`), appending its ASIN, cover image, and metadata into a unified catalog view.
* **API Token Authorization**: Validated that all post endpoints block requests lacking the `Bearer SCANNER_API_TOKEN` header.

---

## How to Run the Project

### A. Central Web Application (Host or Docker)

#### Local Test Run
1. Activate virtual environment:
   ```powershell
   .\venv\Scripts\activate
   ```
2. Run Django local development server:
   ```powershell
   python manage.py runserver
   ```
3. Open http://localhost:8000 in your browser.
4. Access Django Admin at http://localhost:8000/admin (using your configured admin credentials).

#### Running in Docker
Build and deploy the containers using the command line:
```bash
docker-compose up --build
```
This boots Django running on Gunicorn binding to port `8000`, reading connection details from `.env`.

Alternatively, use the VS Code Run & Debug options:
* **Docker: Rebuild & Run Django (Single Container)**: Builds the image and runs the Django application in a standalone Docker container.
* **Docker Compose: Rebuild & Run (Compose)**: Rebuilds and deploys the entire application stack using Docker Compose.

---

### B. Running Scanners (On the remote computer)

1. Navigate to the scanner directory:
   ```bash
   cd scanner
   ```
2. Install client dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Edit target directories in `config.json`.
4. **Sync Local Folders** (Books, Magazines, Calibre Library):
   ```bash
   python sync_files.py
   ```
5. **Sync Audible Catalog** (runs interactive login on first execution, saves credentials):
   ```bash
   python sync_amazon.py audible --locale us
   ```
6. **Sync Kindle Catalog** (imports Amazon list):
   ```bash
   python sync_amazon.py kindle --file kindle_library.csv
   ```
