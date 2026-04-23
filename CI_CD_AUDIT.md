# CI/CD Audit — invoice-ocr

_Snapshot: 2026-04-23, after live AWS deployment of the staging stack._

This document is the **single source of truth** for what CI/CD does in this repo.  Read this end-to-end the first time, then keep it open as a reference when wiring new pipelines.

---

## 1. The 7 workflows at a glance

| Workflow file | Display name | Triggers | What it does | Cost / time |
|---|---|---|---|---|
| `fast-checks.yml` | Fast checks | every PR + every push to main | ruff lint, mypy, 57 unit + integration tests | ~1 min |
| `stack-gate.yml` | Stack + accuracy gate | PR that touches `src/**`, `pyproject.toml`, prompts, whitelists, configs | spins compose stack inside the runner, runs eval against 120-record sample, gates with `--mode smoke` | ~12 min |
| `build-push.yml` | Build + scan + push | push to main (with paths-ignore) + manual | docker buildx → multi-arch → trivy scan → push to GHCR with `:sha-<7>` and `:staging` tags | ~2-3 min |
| `deploy-staging.yml` | Deploy → staging | on `Build + scan + push` success + manual | SSH to VPS → run `ops/deploy-here.sh` → poll `/readyz` | ~30-40 sec |
| `verify-staging.yml` | Verify staging | on `Deploy → staging` success + manual | 4 parallel jobs against live VPS: smoke (3 receipts), accuracy (full 400 records, strict mode), load (3-min concurrency=4), gate (combine results) | ~10-13 min |
| `deploy-prod.yml` | Deploy → production | manual only | identical to staging but uses `environment: production` (required reviewer) | ~30-40 sec |
| `full-eval.yml` | Full test-set eval | nightly cron `7 3 * * *` (03:07 UTC = 10:07 ICT) + release tags `v*` | spins compose stack inside runner, runs eval against full 400-record test set, strict mode, persists `experiments/final_result.json` on tag | ~25 min |

---

## 2. The cascade — what fires what

```
                        ┌─ pull_request ──→ fast-checks
                        │                    └─ stack-gate (if relevant paths)
                        │
git push origin main ──┼─→ fast-checks  (informational)
                        │
                        └─→ build-push  (writes ghcr.io/...:sha-XXX + :staging)
                                  │
                                  └─ workflow_run (on success) ──→ deploy-staging
                                                                          │
                                                                          └─ workflow_run ──→ verify-staging
                                                                                                      │
                                                                                                      └─ 4 parallel jobs:
                                                                                                          • smoke  (3 fixture receipts)
                                                                                                          • accuracy (400 receipts, strict)
                                                                                                          • load (3 min @ concurrency 4)
                                                                                                          • gate (aggregate, fail if any red)

git tag v3.8 && git push origin v3.8  ──→ full-eval (full 400, persists final_result.json)
nightly  03:07 UTC                     ──→ full-eval

manual:  Actions → Deploy → production → Run workflow → (1 reviewer approval) → SSH + deploy
```

### Why some pushes don't trigger every workflow

`build-push.yml` has a `paths-ignore`:
```yaml
paths-ignore:
  - '*.md'
  - 'experiments/**'
  - '.github/workflows/full-eval.yml'
  - '.github/workflows/fast-checks.yml'
  - '.github/workflows/stack-gate.yml'
```

So a commit that **only** touches `experiments/baseline.json` or markdown will NOT rebuild the image.  That's intentional — those changes don't affect the runtime.  But it means `deploy-staging` won't auto-fire either (it cascades from build-push).  Workaround: dispatch deploy-staging manually with `image_tag=staging` to deploy the latest code from main without rebuilding.

`workflow_run` triggers also have a known GitHub quirk: **the downstream workflow file must already be on the default branch BEFORE the upstream run** for the trigger to wire up.  Newly-merged downstream workflows can take 1-2 minutes to register.

---

## 3. Where caching is — and why CI looks instant

You noticed `accuracy in 3m15s` for "400 records through Gemini".  That's because the staging stack has **two layers of cache**, both inside the worker, and `run_eval.py` POSTs to the live stack instead of bypassing it:

1. **Image pHash cache** (`whitelists/__pycache__/phash_*.json`).  When the worker receives a receipt JPEG, it computes a perceptual hash; if it has seen the same hash before, it short-circuits the Gemini call and returns the prior result.
2. **MinIO result store** + **Postgres dedup**.  Idempotency on the API surface — a re-POST of the same blob within the dedup window returns the cached job result.

Net effect: the **first** run through CI hits Gemini for ~400 calls (~$2-3 of API cost).  **Subsequent** runs against the same staging stack are essentially free, returning in 2-3 minutes because they're served from cache.  This is by design — production has the same caching; tests should see the same fast path.

If you want to **prove** Gemini is being called, flush the cache between runs:
```bash
ssh ec2-user@<vps>
sudo docker compose exec worker rm -rf /app/whitelists/__pycache__/phash_*.json
sudo docker compose exec postgres psql -U invoice -d invoice_ocr -c "TRUNCATE jobs CASCADE;"
sudo docker compose exec minio mc rb --force local/invoice-ocr  # then re-init
```

Then re-run `verify-staging` and watch your Gemini quota dashboard — you'll see ~400 fresh calls.

---

## 4. The Deployments tab

GitHub's repo Deployments tab shows entries only for workflows that declare `environment: <name>`.  We use:

- `environment: staging` on `deploy-staging.yml` → entry per deploy
- `environment: production` on `deploy-prod.yml` → entry per deploy + required reviewer protection

If you remove `environment:`, the deploy still works but the tab silently goes stale.  This is what bit us earlier today: I temporarily dropped `environment: staging` to debug a secrets-visibility issue, and the Deployments UI froze on the last failed entry while the real VPS kept getting updates.

**Source of truth is never the Deployments tab.**  It is:
1. **Live `/readyz` on the VPS**: `curl http://ec2-13-221-100-159.compute-1.amazonaws.com:8000/readyz`
2. **`/opt/invoice-ocr/.env` IMAGE pin** on the box
3. **`docker inspect` digest** of the running api container
4. **Workflow run conclusion** in the Actions tab (success = stack came up green)

---

## 5. Secrets — who has what

| Secret | Lives in | Read by |
|---|---|---|
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | repo-level GitHub secret | deploy-staging, deploy-prod, verify-staging |
| `GEMINI_API_KEY_CI` | repo-level GitHub secret | full-eval, stack-gate, verify-staging (accuracy job) |
| `GITHUB_TOKEN` (built-in) | auto-injected per run | build-push (push to GHCR) |
| `/invoice-ocr/staging/*` (5 SecureStrings) | AWS SSM Parameter Store | the EC2 instance role pulls these via `ops/pull-secrets.sh` on every deploy |
| └─ `GEMINI_API_KEY` | SSM | runtime worker (in `/opt/invoice-ocr/.env`) |
| └─ `POSTGRES_PASSWORD` | SSM | postgres + worker DSN |
| └─ `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD` | SSM | minio + worker S3 client |
| └─ `ssh-private-key` | SSM | deploy-staging (SCP/SSH the VPS) |
| └─ `ghcr-pull-pat` | SSM | `pull-secrets.sh` runs `docker login ghcr.io` so the box can pull private images |

The **EC2 instance role** (`invoice-ocr-staging-ec2-role`) grants:
- `AmazonSSMReadOnlyAccess` (read all SSM params)
- `CloudWatchAgentServerPolicy` (push CW metrics)
- `s3-models-readonly` (inline policy: read-only on `s3://invoice-ocr-models-<account>/`)

So secrets never live in env vars or git or workflow files — they originate in either GitHub Secrets (CI) or SSM (VPS) and the box pulls them via its IAM role on each deploy.

---

## 6. Image rotation — what tag is what

`build-push.yml` writes **two tags** per build:
- `ghcr.io/congnguyengithub/invoice-ocr:sha-<first7>` — immutable.  This is what deploy workflows pin to.
- `ghcr.io/congnguyengithub/invoice-ocr:staging` — moving pointer.  Always tracks the latest successful build of `main`.

`deploy-staging.yml` defaults `image_tag` input to `staging`, so the dispatch path always grabs the latest.  The `workflow_run` cascade path also reads the `:staging` tag because it triggers immediately after build-push pushes it.

For prod, we'd pin to a specific `sha-XXX` to avoid surprises (`deploy-prod.yml` supports an explicit `image_tag` input — feed it the SHA from a tagged release).

---

## 7. Per-workflow deep dive

### `fast-checks.yml` — the cheap gate
- Runs on every PR + every push to main.
- Steps: `ruff check`, `mypy src`, `pytest tests/unit tests/integration` (57 tests).
- No GHCR push, no secrets, no AWS.  Pure code hygiene.
- Failing this **does NOT** block downstream workflows because they're triggered by `push` independently.  But CI status badge goes red and PR review can require it.

### `stack-gate.yml` — the PR-time accuracy gate
- Triggers ONLY on PRs that touch runtime paths (`src/**`, `pyproject.toml`, `whitelists/**`, prompts, `docker-compose.yml`).
- Spins up the **full compose stack inside the GitHub runner** (yes, including Triton — it's CPU-only on runners, so YOLO is approximate; that's why this is "smoke" mode).
- Runs `run_eval.py` against a **120-record sample** with `--mode smoke` (looser thresholds: -2 pp floor offset, 1.5× drop multiplier).
- Total ~12 min.  This is your "is this PR safe to merge" gate.
- Why a sample, not full 400: cost (Gemini quota during PR storm) and time (12 min vs 25 min).

### `build-push.yml` — the image factory
- Push to main (excluding md/experiments/sibling workflow files).
- buildx multi-arch, then trivy CVE scan with `severity: HIGH,CRITICAL`, then push.
- Uses GHCR's `GITHUB_TOKEN` for push — no PAT needed.
- Both tags written: immutable `:sha-XXX` and moving `:staging`.

### `deploy-staging.yml` — the SSH cannon
- Wakes on build-push success (workflow_run).  Or manual dispatch with image tag.
- 4 steps:
  1. AWS CLI auth (repo secrets)
  2. Pull SSH private key from SSM, strip CR (Windows CRLF was a real footgun — fixed)
  3. Resolve the VPS DNS (currently hard-coded to the staging instance)
  4. SSH and run `sudo IMAGE=$IMAGE ENV=staging bash /opt/invoice-ocr/ops/deploy-here.sh`
- `deploy-here.sh` on the box:
  - git pulls latest main  
  - runs `pull-secrets.sh` (re-syncs `.env` + `docker login ghcr.io` + S3 model sync)
  - `docker compose pull` then `up -d`  
  - polls `/readyz` for 3 minutes; rolls back to `.previous_sha` if not green

### `verify-staging.yml` — the post-deploy quality gate
4 jobs run in parallel after deploy-staging succeeds:

| job | what | duration | failure means |
|---|---|---|---|
| `resolve-host` | discover the VPS DNS via EC2 tag | ~10s | infra lookup broken |
| `smoke` | POST 3 fixture receipts → expect HTTP 200 | ~30s | the API surface itself is broken |
| `accuracy` | run_eval.py against 400 records, mode=strict, gate against `experiments/baseline.json` | ~3-13 min (cache-dependent) | model regression or prompt regression |
| `load` | 3-min concurrency=4 against `/v1/receipts` | ~9 min | latency / throughput regression |
| `gate` | aggregates the above; fails if any are red | ~3s | same as whichever child failed |

### `deploy-prod.yml` — the human-gated release
- ONLY workflow_dispatch.  No automatic trigger.
- `environment: production` requires reviewer approval (configure in repo settings → Environments → production → required reviewers).
- Otherwise structurally identical to deploy-staging.

### `full-eval.yml` — the nightly + release sentinel
- Cron `7 3 * * *` (03:07 UTC = 10:07 ICT next morning).
- Plus tag pushes (`v*`).
- Plus workflow_dispatch.
- Spins the stack inside the runner (NOT against staging — fully isolated, repeatable).
- 400 records, strict mode.
- On a tag, also writes `experiments/final_result.json` (the canonical "this is what v3.X does" snapshot).

---

## 8. What runs against staging vs. inside the runner

**Inside the runner (no AWS, no live VPS):**
- `fast-checks` — unit/integration tests, lint
- `stack-gate` — full stack in docker-in-docker, 120 records, smoke mode
- `build-push` — buildx + scan + push
- `full-eval` — full stack in docker-in-docker, 400 records, strict mode

**Against the live staging VPS:**
- `verify-staging` — smoke + accuracy + load against `http://13.221.100.159:8000`

This split matters because:
- Runner-based workflows are **deterministic** (same image, same code) — used for gates that need reproducibility.
- VPS-based workflows are **realistic** (real Triton, real network latency, real Gemini under load) — used for "did the deploy actually work?" verification.

---

## 9. Today's debugging — what tripped us up

Recorded so we don't repeat:

1. **`environment: staging` hides repo-level secrets** when the environment exists but you haven't added secrets to it.  Either move secrets to env, or remove `environment:`.  We did the latter, then re-added `environment:` once we confirmed repo-level secrets propagate.
2. **SSH private key from SSM had CRLF** because we uploaded it from Windows.  OpenSSH refuses CRLF keys with "error in libcrypto".  Fixed by piping through `tr -d '\r'` in both the workflow and on disk.
3. **GHCR private packages need explicit auth.** Our PAT path: read-only PAT (scope `read:packages` only) → SSM → `pull-secrets.sh` runs `docker login ghcr.io`.
4. **`*.onnx` is gitignored**, so the model wasn't on the VPS clone → Triton crashed on startup.  Fixed by uploading to S3 + `aws s3 sync` step in `bootstrap-vps.sh`.
5. **`paths-ignore` skipped build-push** when a commit only touched `experiments/**` and `.github/workflows/`.  This is correct behavior but surprising; manual dispatch is the workaround for "config-only deploys".
6. **`workflow_run` cascade can take 1-2 min to register** for newly-merged downstream workflows.  Manual dispatch unblocks.
7. **Cache makes the eval look instant.** pHash + result cache mean 2nd-Nth run against the same staging stack returns near-immediately.  Real Gemini billing happens on the 1st run after a cache flush.

---

## 10. The current live state (snapshot)

```
EC2 instance          : i-0dc5216805dd3425b   t3.large   us-east-1
Public DNS            : ec2-13-221-100-159.compute-1.amazonaws.com
Public IP             : 13.221.100.159
git commit on box     : 17e40e3 (chore: bump baseline + smoke fixtures)
image pinned          : ghcr.io/congnguyengithub/invoice-ocr:staging
image digest running  : sha256:784bcf589cc063b54011ecb1e8ca7596c511596037bbeb786ec8a5de2024c458
/readyz               : { ready: true, redis: true, postgres: true, minio: true, triton: true }

Recent successful runs:
  build-push       #24841362306  (commit 17e40e3)
  deploy-staging   #24841367359  (✓ in 67s)
  verify-staging   #24841464786  (✓ in 9m34s — smoke ✓ accuracy ✓ load ✓ gate ✓)

S3 model bucket: s3://invoice-ocr-models-889947797975/yolov11n_receipt/  (versioned)
SSM secrets    : 5 SecureStrings under /invoice-ocr/staging/
```

---

## 11. Recommended next steps

In priority order:

1. **Add a daily Slack/email notification** for `full-eval` results — currently no human is in the loop until they look at the Actions tab.
2. **Set up an `environment: production` reviewer rule** with at least one required approval before prod deploys.
3. **Migrate AWS auth from access key to OIDC** (`aws-actions/configure-aws-credentials` supports this).  Lets you delete `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` from GitHub.
4. **Cache flush as a workflow_dispatch** — handy ops button to "force a real Gemini eval next time".
5. **Pin prod deploys to `:sha-XXX`, not `:staging`.** Already supported, just discipline.
6. **Cost monitoring on Gemini.** A cron that diffs daily quota would catch a runaway loop fast.
