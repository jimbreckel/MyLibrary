# Deployment Guide: Decoupled Multi-Container Setup

This document details the multi-container deployment architecture of **MyLibrary**. The system is divided into two separate, decoupled Docker layers: the **Central Web App & Services** and the **Local NAS Scanner**.

---

## 1. System Architecture

The decoupled design separates UI and catalog access from physical file storage, improving security, resource isolation, and deployment flexibility.

```mermaid
graph TD
    subgraph Local NAS (Storage & Scanning)
        Storage[("/ufi/pool-raw/syspool/Books & Audiobooks")] -->|Mounts as Read-Only Volume| Scanner["nas_scanner Container"]
        Scanner -->|Runs continuous 12-hour loops| Loop[docker_scanner_loop.py]
    end

    subgraph Central Host (Web UI & Database)
        Web["web (Django App) Container"] -->|Reads & Writes| DB[(PostgreSQL Database Container)]
    end

    Scanner -->|POST metadata /api/sync/file/| Web
    Scanner -->|POST active paths /api/sync/cleanup/| Web
```

---

## 2. Docker Layer Details

### Layer 1: Central Web App & Services
* **Role**: Serves the user interface (port `8000`), handles search requests, and manages the Postgres database.
* **Storage Mounts**: None (has no access to your raw books).
* **Location**: Can be hosted on your home server, a cloud VPS (e.g., AWS, Render), or directly in a container host.

### Layer 2: NAS Scanner (`nas_scanner`)
* **Role**: Periodically crawls your NAS shares, extracts metadata in parallel, and pushes updates to the central application.
* **Storage Mounts**: Maps your local directory shares (`P:\Books` or `/ufi/pool-raw/syspool/`) as read-only volumes.
* **Location**: Runs locally inside a container on your **Orico NAS** where the files reside.

---

## 3. Step-by-Step Deployment

### Step A: Deploying Central Services (Web & DB)
1. Copy the project folder containing the root `Dockerfile` and `docker-compose.yml` to the host server.
2. Configure settings in the `.env` file (e.g., API tokens, Postgres password).
3. Start the services:
   ```bash
   docker-compose up -d web db
   ```
4. Access the dashboard in your browser at `http://<server-ip>:8000`.

### Step B: Deploying the Scanner on the NAS
1. Build the scanner image inside the `nas_scanner/` folder:
   ```bash
   docker build -t mylibrary-scanner:latest ./nas_scanner
   ```
2. Run the scanner container, mapping your NAS storage folders:
   ```bash
   docker run -d \
     --name mylibrary-scanner \
     --restart unless-stopped \
     -v /ufi/pool-raw/syspool/Books:/books:ro \
     -v /ufi/pool-raw/syspool/audiobooks:/audiobooks:ro \
     -v /path/to/nas_scanner/config.json:/config/config.json:ro \
     -v /path/to/nas_scanner/scanner_state.db:/app/scanner_state.db \
     mylibrary-scanner:latest
   ```

*Note: The scanner container will run `docker_scanner_loop.py` continuously in the background, waking up every 12 hours to perform folder scans, upload catalog entries, and prune deleted files.*
