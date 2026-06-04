# Invoice OCR — System Architecture

This document serves as the formal architecture draft for the Invoice OCR system. The system is designed as a high-throughput, asynchronous pipeline using a fire-and-forget pattern.

## 1. High-Level Architecture

```mermaid
graph TD
    Client[Client] -->|POST /v1/receipts| API[FastAPI Ingestion API]
    API -->|1. Create Job| DB[(PostgreSQL)]
    API -->|2. Enqueue Job ID| Redis[(Redis Queue)]
    API -->|3. HTTP 202 Accepted| Client
    
    Redis -->|4. Pop Job| Worker[Async Worker Process]
    Worker -->|5. Download Image| CDN[External Image CDN]
    Worker -->|6. gRPC Inference| Triton[Triton Inference Server]
    Worker -->|7. REST Extraction| Gemini[Google Gemini Flash]
    Worker -->|8. Update Status/Result| DB
    
    Client -->|9. GET /v1/receipts/{job_id}| API
    API -->|10. Fetch Result| DB
    DB -->|11. Return JSON| API
    API -->|12. Return Result| Client
```

## 2. Core Components

### A. Ingestion API (FastAPI)
- **Role:** Handles incoming HTTP requests and job enqueuing.
- **Workflow:**
  1. Validates the `image_url` against an allowed CDN domain list (e.g., `img-campaign.gotit.vn`).
  2. Generates a unique `job_id`.
  3. Inserts a new job record into PostgreSQL with `status: PENDING`.
  4. Pushes the `job_id` and `image_url` to a Redis LIST (`ocr:queue`).
  5. Returns HTTP 202 immediately to the client without downloading the image.

### B. Redis Queue
- **Role:** Asynchronous message broker.
- **Workflow:**
  - Acts as a simple `LIST` for fire-and-forget task delegation.
  - Decouples the fast API ingestion from the slower processing pipeline.

### C. Async Worker
- **Role:** Executes the heavy OCR pipeline.
- **Scaling:** Runs 4 processes per host, each running 4 `asyncio` tasks concurrently (16 in-flight jobs).
- **Workflow:**
  1. Pops a job from the Redis queue.
  2. Downloads the receipt image directly from the external CDN.
  3. **Detection (Triton + YOLO):** Sends the image via gRPC to the Triton server for dynamic batching and bounding box detection. Crops the image to the detected receipt area.
  4. **Extraction (Gemini):** Sends the cropped image and a structured prompt to the Google Gemini Flash Lite API.
  5. **Post-processing:** Normalizes dates, times, and amounts. Applies fuzzy matching against in-memory frozen whitelists (Store and Product names) using `rapidfuzz`.
  6. **Persistence:** Writes the final JSON payload and `status: SUCCESS` (or error states) back to PostgreSQL.

### D. Triton Inference Server
- **Role:** GPU-accelerated object detection.
- **Workflow:** Hosts the YOLOv11n model. Utilizes dynamic batching (batch size 4–8) to maximize GPU utilization when multiple workers request inferences simultaneously.

### E. PostgreSQL Database
- **Role:** Persistent state and result storage.
- **Workflow:** Stores job metadata, status (`PENDING`, `PROCESSING`, `SUCCESS`, `FAILED_PERMANENT`), error codes, and the final extracted JSON payload.

## 3. Monitoring & Observability
- **Prometheus & Grafana:** 
  - The API exposes metrics on port 9101 (e.g., queue depth, request latency).
  - The Worker exposes metrics on port 9102 (e.g., CDN download latency, Triton batch sizes, Gemini tokens used).
- **Logging:** Structured JSON logging is used across all services for trace-ability via `job_id`.

## 4. Error Handling & Sweeper
- **Transient Errors:** Network timeouts or Gemini 429s are retried with exponential backoff.
- **Permanent Errors:** Missing invoices, invalid images, or missing APIs result in a `FAILED_PERMANENT` status.
- **Sweeper:** A background task periodically scans PostgreSQL for stale jobs (e.g., stuck in `PROCESSING` for > 15 mins) and marks them as failed to prevent deadlocks.
