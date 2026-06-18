# Invoice OCR — CPU-Powered Receipt Extraction

Vietnamese receipt OCR pipeline using YOLOv11n detection + Google Gemini extraction with fire-and-forget async architecture.

**Performance**: ~3s p50 end-to-end, 10k/day steady state, 30k burst via horizontal scaling.

---

## Architecture

```
┌────────────────────────────────────────────────┐
│      AWS EC2 t3.xlarge (4 vCPU, 16GB RAM)      │
├────────────────────────────────────────────────┤
│  Docker Compose Stack:                         │
│  ┌────────────────────────────────────────┐   │
│  │ API (FastAPI)           :8000          │   │
│  │ Worker (4 replicas)                    │   │
│  │ Triton Server (CPU)     :8001          │   │
│  │ PostgreSQL 16           :5432          │   │
│  │ Redis 7                 :6379          │   │
│  │ Prometheus              :9090          │   │
│  │ Grafana                 :3000          │   │
│  └────────────────────────────────────────┘   │
└────────────────────────────────────────────────┘
```

### Key Features

- **Fire-and-forget API** - Returns 202 immediately, processes asynchronously
- **CPU-based inference** - Triton runs YOLOv11n in CPU mode (no GPU required)
- **Redis phash cache** - Duplicate image detection
- **Token bucket rate limiting** - Gemini API throttling
- **Stale job recovery** - Sweeper daemon reclaims stuck jobs
- **Full monitoring** - Prometheus metrics + Grafana dashboards
- **Cost-effective** - ~$143/month on AWS EC2 t3.xlarge

---

## Quick Start

### Prerequisites

- Docker + Docker Compose
- Python 3.11+ (for local dev)
- Google Gemini API key

### Local Development

1. **Clone the repository:**
```bash
git clone https://github.com/YOUR_ORG/invoice-ocr.git
cd invoice-ocr
```

2. **Create `.env` file:**
```bash
cp .env.example .env
# Edit .env and set:
# GEMINI_API_KEY=your-api-key-here
# ALLOWED_IMAGE_DOMAINS=img-campaign.gotit.vn
```

3. **Start all services:**
```bash
docker compose up -d
```

Wait ~30 seconds for Triton to load the YOLO model and workers to boot.

4. **Check services:**
```bash
docker compose ps
curl http://localhost:8000/readyz
# {"ready":true,"redis":true,"postgres":true,"triton":true}
```

5. **Submit a test receipt:**
```bash
curl -X POST http://localhost:8000/v1/receipts \
  -H "Content-Type: application/json" \
  -d '{"image_url": "https://img-campaign.gotit.vn/scanit/mini-tet-2/2024-08-30/1724993296BBcQb_blob"}'

# Response: {"job_id": "550e8400-...", "status": "PENDING"}
```

6. **Poll for result:**
```bash
# Replace JOB_ID with the returned job_id
curl http://localhost:8000/v1/receipts/{JOB_ID}
```

### Monitoring Dashboards

- **API Docs**: http://localhost:8000/docs
- **Grafana**: http://localhost:3000 (admin/admin)
- **Prometheus**: http://localhost:9090
- **API Metrics**: http://localhost:9101/metrics
- **Worker Metrics**: http://localhost:9102/metrics

---

## Pipeline Flow

```
POST /v1/receipts → Redis Queue
    ↓
Worker picks job → Status: PROCESSING
    ↓
Download from CDN (img-campaign.gotit.vn)
    ↓
Preprocess (resize, EXIF correction, phash)
    ↓
Check phash cache → Skip if duplicate
    ↓
YOLO Detection (Triton CPU inference)
    ↓
Token bucket check → Rate limit Gemini
    ↓
Gemini Extraction (structured JSON)
    ↓
Cache phash result
    ↓
Postprocess (normalize dates/money, fuzzy match)
    ↓
Save to PostgreSQL → Status: SUCCEEDED
    ↓
GET /v1/receipts/{id} → Return results
```

---

## API Endpoints

### POST /v1/receipts

Submit a receipt image URL for extraction.

**Request:**
```bash
curl -X POST http://localhost:8000/v1/receipts \
  -H "Content-Type: application/json" \
  -d '{"image_url": "https://img-campaign.gotit.vn/scanit/..."}'
```

**Response (202 Accepted):**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "PENDING",
  "message": "Job enqueued"
}
```

### GET /v1/receipts/{job_id}

Poll job status or retrieve the result.

**Response (200 - Succeeded):**
```json
{
  "name": "AEON",
  "type": "supermarket",
  "date": "2024-08-15",
  "total_money": "1356000",
  "products": [...]
}
```

**Response (202 - Processing):**
```json
{
  "job_id": "550e8400-...",
  "status": "PROCESSING"
}
```

**Response (422 - Failed Permanently):**
```json
{
  "job_id": "550e8400-...",
  "status": "FAILED_PERMANENT",
  "error_code": "no_invoice_detected"
}
```

### GET /healthz

Liveness check (always 200 if API is running).

### GET /readyz

Readiness check (200 only if Redis, PostgreSQL, Triton are healthy).

---

## Development

### Testing

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run all tests
pytest

# Run specific test suites
pytest tests/unit/ -v
pytest tests/integration/ -v

# Run with coverage
pytest --cov=src tests/
```

### Linting & Type Checking

```bash
# Lint
ruff check .

# Format
ruff format .

# Type check
mypy src/
```

### Database Migrations

```bash
# Run migrations
alembic upgrade head

# Create new migration
alembic revision --autogenerate -m "description"

# Rollback
alembic downgrade -1
```

---

## Configuration

All settings are environment variables (loaded from `.env`):

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | *(required)* | Google Gemini API key |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite-preview` | Model name |
| `PROMPT_SEMANTIC_VERSION` | `v3.7` | Prompt version |
| `POSTGRES_DSN` | `postgresql+asyncpg://...` | Database connection |
| `REDIS_URL` | `redis://redis:6379` | Redis connection |
| `TRITON_HOST` | `triton:8001` | Triton gRPC endpoint |
| `ALLOWED_IMAGE_DOMAINS` | `img-campaign.gotit.vn` | Domain allowlist |
| `WORKER_CONCURRENCY` | `4` | Async tasks per worker |
| `TOKEN_BUCKET_RPS` | `4.0` | Gemini rate limit |

See `.env.example` for full list.

---

## Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for complete AWS EC2 deployment guide.

**Quick summary:**
- Deploy on AWS EC2 t3.xlarge (~$143/month)
- Use AWS ECR for container registry
- Store secrets in AWS SSM Parameter Store
- CI/CD via GitHub Actions
- Simple rolling updates (no blue-green)

---

## Error Codes

| Code | Retry? | Meaning |
|---|---|---|
| `no_invoice_detected` | ❌ | YOLO found no receipt |
| `gemini_client_error` | ❌ | Gemini 4xx error |
| `cdn_download_failed` | ✅ | CDN timeout |
| `gemini_exhausted` | ✅ | Gemini 5xx/timeout |
| `rate_limited_locally` | ✅ | Gemini 429 |
| `stale_timeout` | ⚠️ | Job exceeded 15 min |

---

## Troubleshooting

### Queue always full (429 responses)
```bash
curl http://localhost:9101/metrics | grep ocr_api_queue_depth
docker compose logs worker | grep "worker_task_started"
```

### Worker crashes
```bash
docker compose logs worker --tail 50
docker compose down -v && docker compose up -d
```

### Database connection failures
```bash
docker compose exec postgres pg_isready -U invoice -d invoice_ocr
docker compose logs postgres
```

---

## Tech Stack

- **API**: FastAPI + Uvicorn
- **Inference**: Triton Inference Server (CPU mode) + YOLOv11n ONNX
- **LLM**: Google Gemini Flash Lite
- **Database**: PostgreSQL 16 (AsyncPG)
- **Cache/Queue**: Redis 7
- **Monitoring**: Prometheus + Grafana
- **CI/CD**: GitHub Actions → AWS ECR → EC2
- **Deployment**: Docker Compose on AWS EC2 t3.xlarge

---

**Cost**: ~$143/month | **Throughput**: 10k receipts/day | **Latency**: ~3s p50

For detailed deployment instructions, see [DEPLOYMENT.md](DEPLOYMENT.md).
