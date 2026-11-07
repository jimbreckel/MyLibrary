# Instructions: Deploying Standalone NAS Scanner

This guide walks you through deploying the `nas_scanner` container directly on your Orico NAS.

## Prerequisites
* **Docker** and **Docker Compose** installed on your Orico NAS.
* Access to the directory containing your books and audiobooks.

---

## Deployment Steps

### 1. Copy the Directory to your NAS
Copy the entire `nas_scanner` directory onto your NAS (e.g., inside `/share/apps/nas_scanner` or `/volume1/apps/nas_scanner`).

### 2. Configure environment and volumes in `docker-compose.yml`
Ensure your main `docker-compose.yml` file contains the `scanner` service and points to the correct NAS directories:

```yaml
  scanner:
    build:
      context: ./nas_scanner
      dockerfile: Dockerfile
    container_name: mylibrary_scanner
    volumes:
      # Map container files to persistent storage on the host NAS:
      - ./nas_scanner/config.json:/config/config.json
      - ./nas_scanner/scanner_state.db:/app/scanner_state.db
      
      # Map your raw media libraries:
      - /ufi/pool-raw/syspool/Books:/books
      - /ufi/pool-raw/syspool/audiobooks:/audiobooks
    environment:
      # Specify scan interval in hours (Default is 12 hours)
      - SCAN_INTERVAL_HOURS=12
    restart: always
```

### 3. Edit `config.json`
Configure the target folders and API endpoints inside `nas_scanner/config.json`:
* `server_url`: The address of your running `MyLibrary` web app. If they run on the same docker-compose stack, you can use `http://mylibrary_web:8000`.
* `api_token`: The matching authorization token configured in the Django settings.
* `scan_folders`:
  * Point `"path": "/books"` to match your mounted books folder.
  * Point `"path": "/audiobooks"` to match your mounted audiobooks folder (ensure `"type": "AUDIOBOOK"` and `"recursive": false`).

### 4. Build and Launch the Container
Run the following command in the directory containing `docker-compose.yml`:
```bash
docker compose up -d --build scanner
```

### 5. Verify Execution
Check the scanner logs to ensure it successfully reads the configuration and initiates the sync loop:
```bash
docker compose logs -f scanner
```
To trigger a manual scan outside the 12-hour schedule, you can enter the container shell and run the scripts manually:
```bash
docker compose exec scanner python sync_files.py
```
