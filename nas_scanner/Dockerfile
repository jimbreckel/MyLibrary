FROM python:3.11-slim

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Env variable for loop interval (default: 12 hours)
ENV SCAN_INTERVAL_HOURS=12

CMD ["python", "docker_scanner_loop.py"]
