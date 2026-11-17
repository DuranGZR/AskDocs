<div align="center">

<br />

<img src="docs/images/hero-banner.svg" alt="AskDocs — Upload. Index. Verify." width="100%" />

<br />
<br />

**Upload your documents into isolated workspaces, ask questions in natural language, and verify every answer down to the exact source page.**

<p>
  <a href="#why-askdocs">Why AskDocs</a> ·
  <a href="#see-it-in-action">See it in action</a> ·
  <a href="#architecture--tech-stack">Architecture</a> ·
  <a href="#engineering-decisions">Design Choices</a> ·
  <a href="#benchmarks--evaluations">Benchmarks</a> ·
  <a href="#quickstart">Quickstart</a> ·
  <a href="#api-reference">API</a> ·
  <a href="README.tr.md">Türkçe</a>
</p>

<p>
  <sub>
    <code>Python 3.11+</code> ·
    <code>FastAPI</code> ·
    <code>Celery &amp; Redis</code> ·
    <code>Qdrant</code> ·
    <code>Gemini API</code> ·
    <code>Langfuse</code> ·
    <code>136 Tests Passing</code>
  </sub>
</p>

</div>

---

## Why AskDocs?

Turning documents into a reliable Q&A workspace takes more than dropping a vector store behind a chat box. When a user uploads a heavy PDF, the web server cannot afford to freeze while extracting pages and calling embedding APIs. Different teams need strict workspace isolation, retries should never multiply duplicate vectors, and every generated claim needs a clear trail back to its exact source page.

**AskDocs** was built from scratch without heavy framework wrappers so every stage—from streaming uploads to background indexing, retrieval scoping, and verified token streaming—stays fast, observable, and predictable.

### Core Capabilities

* **Isolated Workspaces & File Scoping:** Separate documents and queries cleanly per tenant workspace, or check individual PDFs in the sidebar to restrict search strictly to selected files.
* **Non-Blocking Ingestion:** Uploads validate file integrity immediately, return `202 Accepted`, and hand parsing, chunking, and batched embedding work off to background Celery workers.
* **Verified Citations & Source Inspector:** Answers stream live with clickable `[1]`, `[2]` badges that slide open a Source Inspector pointed at the matched page, similarity score, and highlighted passage.
* **Bilingual Follow-Ups & Guardrails:** Expands short follow-up replies cleanly from chat context, neutralizes `<context>` prompt-injection tags inside files, and abstains cleanly when evidence is missing.
* **Production Resilience & Model Portability:** Dual async/sync circuit breakers isolate vector or LLM outages, Redis rate limiting and Langfuse tracing fail open, and `.env` lets you target Gemini, OpenAI, or any OpenAI-compatible API without code changes.

---

## See it in action

Below is a three-step walkthrough inside the isolated **`rag-research`** workspace loaded with six interconnected arXiv retrieval papers (`REALM`, `DPR`, `FiD`, `HyDE`, `Self-RAG`, and `CRAG`).

### `#1` / Select a Workspace & Scope Specific Documents

Switch workspaces at the bottom left, drag and drop multiple PDFs or text files, and check specific papers (`crag_2401_15884.pdf` and `selfrag_2310_11511.pdf`) to lock retrieval solely to those sources.

<div align="center">
  <img src="docs/images/ui-workspace-upload.png" alt="AskDocs — 6-PDF Isolated rag-research Workspace &amp; Scoping" width="96%" />
</div>

### `#2` / Stream Grounded Markdown & Inline Citations

Ask a cross-paper comparison question and watch structured Markdown stream word-by-word with clickable citation pills, a clean copy button that strips citation markers, contextual follow-up actions, and per-page relevance badges underneath.

<div align="center">
  <img src="docs/images/ui-chat-citations.png" alt="AskDocs — Grounded Streaming Answer &amp; Citations" width="96%" />
</div>

### `#3` / Verify Claims in the Source Inspector

Click citation `[1]` to slide out the Cupertino Source Inspector right to page 15 of `crag_2401_15884.pdf`, displaying the `73%` similarity match and highlighted terms so every answer can be verified in seconds.

<div align="center">
  <img src="docs/images/ui-source-inspector.png" alt="AskDocs — Slide-out Source Inspector Drawer" width="96%" />
</div>

---

## Architecture & Tech Stack

<div align="center">
  <img src="docs/images/architecture.svg" alt="AskDocs System Architecture" width="100%" />
</div>

AskDocs splits heavy ingestion (**Write Path**) from low-latency conversational streaming (**Read Path**):

1. **Upload & Index (Write Path):** FastAPI checks authentication, sliding-window rate limits, and file headers (`%PDF-` or UTF-8), streams bytes to shared disk via UUID paths, dispatches a Celery task to Redis, and replies with `202 Accepted`. Workers read pages incrementally with PyMuPDF, clean formatting noise, split prose into `1200`-character overlapping chunks (`150` overlap), generate embeddings in batches of `32`, and write idempotent points into Qdrant.
2. **Ask & Verify (Read Path):** A query enters with optional document scope IDs and chat history. Short follow-up confirmations resolve against the prior assistant prompt, Qdrant fetches top-5 tenant-isolated matches at a `0.70` similarity threshold (falling back to `0.58` if empty), context snippets are fenced and sanitized, and verified SSE frames stream tokens, citations, and completion markers cleanly to the browser.

### Stack Breakdown

| Layer | Choice | Why It Is Here |
| :--- | :--- | :--- |
| **API & Web UI** | FastAPI + Vanilla JS | Async REST/SSE delivery, SHA-256 API-Key auth, security middlewares, and zero-framework Cupertino UI |
| **Queue & Workers** | Celery + Redis | Isolates CPU/network indexing bursts from FastAPI's event loop and tracks sliding-window rate limits |
| **PDF Engine** | PyMuPDF + Custom Chunker | Streaming page-by-page extraction, ligature/hyphen normalization, and sentence-aligned overlapping chunks |
| **Vector Store** | Qdrant | Tenant-indexed HNSW vector search with document filtering and deterministic `UUID5` upserts |
| **LLM & Embeddings** | Gemini (`OpenAI-Compat` via `httpx`) | Powers `gemini-3.5-flash-lite` generation and `gemini-embedding-001` (`3072`-dim) over pooled connections |
| **Telemetry** | Langfuse + PostgreSQL | Fail-open traces for retrieval scores, TTFT latency, token accounting, and evaluation runs |

<details>
<summary><strong>Under the hood: Security middlewares, atomic rate limiting &amp; fault tolerance</strong></summary>

* **Async vs. Sync isolation:** FastAPI routes strictly run async clients (`src/services/qdrant_service.py`, `src/services/llm_service.py`), while Celery prefork workers implement the exact same Clean Architecture interfaces (`src/services/base.py`) synchronously to eliminate closed event-loop errors.
* **Constant-length auth & IDOR protection:** Constant-time `hmac.compare_digest` compares fixed-length SHA-256 digests (`src/api/dependencies.py`) to avoid key-length timing leaks, `MaxBodySizeMiddleware` rejects oversized `100 MiB+` requests before multipart buffering, and vector search/deletion always require compound `(tenant_id, document_id)` filters.
* **Atomic sliding-window rate limiter:** A Redis Lua sorted-set script (`src/services/rate_limiter.py`) tracks valid traffic without appending rejected `429` calls—preventing blocked burst clients from locking themselves out indefinitely—and degrades `fail-open` under short timeouts alongside Langfuse tracing.

</details>

---

## Engineering Decisions

### Thin Custom Core & Strict Process Isolation
Instead of hiding I/O behind heavy RAG wrappers, AskDocs builds Clean Architecture service contracts directly over `httpx`, `qdrant-client`, and PyMuPDF. Keeping FastAPI purely async and running Celery prefork workers synchronously prevents event-loop corruption under load, while embedding parsers re-sort returned `/v1/embeddings` vectors by `item["index"]` rather than trusting list order.

### Zero-Leak Ingestion, Rollback & Idempotent Retries
Every handoff between web upload and background indexing handles crash points explicitly: if Redis queue dispatch fails after streaming a file to disk, the upload endpoint immediately triggers compensating cleanup (`storage.delete`); inside Celery workers, temporary files are preserved across jittered backoff retries (`max_retries=3`) rather than wiped on first error, and removed in `finally` only after final completion; deterministic `uuid5(document_id, chunk_index)` point IDs guarantee retries overwrite safely with zero duplicate vectors.

### Circuit Breakers Without Cross-Tenant Blast Radius
Dual `3-state` circuit breakers (`src/core/circuit_breaker.py`) fail fast (`503`) during real Qdrant or LLM outages without starving healthy routes. Crucially, abnormal user-level stream stops (`length`, `content_filter`) map cleanly to HTTP `422` without tripping the shared LLM circuit breaker—ensuring one truncated prompt never blocks service for other tenants.

### Calibrated Retrieval & Verifiable Stream Boundaries
Empirical evaluation tuned both context cost and safety boundaries: `1200/150` sentence-aligned chunks preserve full policy clauses while cutting embedding calls 3x; the `0.70` similarity cutoff (`0.58` gentle fallback) removes noise cleanly; `<context>` sanitization defeats indirect prompt injection; and both server and UI verify `finish_reason` plus `[DONE]` boundaries before saving responses to browser history.

---

## Benchmarks & Evaluations

All numbers below come directly from reproducible evaluation, load, and microbenchmark harnesses committed in this repository.

### 1. RAG Retrieval & Grounding Quality

To measure precision under heavy terminology overlap, AskDocs is evaluated against a **44-question Golden Dataset** ([`evals/golden_dataset.json`](evals/golden_dataset.json)) spanning **9 PDFs (`535` chunks)**—seven competing RAG research papers (`RAG`, `REALM`, `DPR`, `FiD`, `HyDE`, `Self-RAG`, `CRAG`), the Bitcoin whitepaper, and a Turkish HR handbook—alongside **6 unanswerable traps** (four reusing exact paper terminology).

| Metric | Measured Result | Interpretation |
| :--- | :---: | :--- |
| **Mean Recall@5** | **`1.0000`** (`38 / 38`) | Expected source page landed in Top-5 across every answerable question |
| **Mean MRR** | **`0.8969`** | High first-rank accuracy despite seven overlapping retrieval papers in one tenant |
| **Mean Faithfulness** | **`0.9591`** | Valid `[Source N]` grounding with zero fabricated or out-of-range references |
| **Keyword Coverage** | **`0.9386`** | Core technical terms and numeric facts captured across Turkish and English |
| **Abstention Accuracy** | **`1.0000`** (`6 / 6`) | All off-topic and adversarial near-miss queries declined without hallucination |

<div align="center">
  <img src="docs/images/eval-threshold-chart.svg" alt="Data-Driven Threshold Calibration Sweep" width="100%" />
</div>

<details>
<summary><strong>Threshold sweep (0.30–0.75) &amp; run configuration</strong></summary>

Run `eval-20261117-183520` · `gemini-3.5-flash-lite` + `gemini-embedding-001` · `top_k=5` · [`evals/latest_eval_report.json`](evals/latest_eval_report.json) · [`evals/README.md`](evals/README.md)

| `score_threshold` | `Recall@5` | `MRR` | `AvgChunks` (Sent to LLM) | `NegNoise` (6 Trap Queries) |
| :---: | :---: | :---: | :---: | :---: |
| `0.30` | `1.0000` | `0.8969` | `5.00` | `1.0000` |
| `0.40` | `1.0000` | `0.8969` | `5.00` | `1.0000` |
| `0.50` | `1.0000` | `0.8969` | `5.00` | `1.0000` |
| `0.60` | `1.0000` | `0.8969` | `4.97` | `1.0000` |
| `0.65` | `1.0000` | `0.8969` | `4.61` | `1.0000` |
| **`0.70` (Selected)** | **`1.0000`** | **`0.8969`** | **`4.11` (-17.8%)** | **`0.6667`** |
| `0.75` | `0.9211` | `0.8180` | `3.11` | `0.1667` |

At `0.70`, average context chunks sent to the model drop `17.8%` (`5.00 -> 4.11`) with zero loss in `Recall@5` or `MRR`, while pushing to `0.75` drops `Recall@5` by `7.9%` (`0.9211`). Meanwhile, the grounded system prompt declines `100%` of both off-topic and near-miss domain traps (`6/6`).

</details>

### 2. Concurrency Load & Parser Throughput

Backend concurrency (`20` users, `30s`, `--with-mock-ai` isolating orchestration from upstream API latency) and Python 3.11 single-core CPU benchmarks for [`src/services/pdf_parser.py`](src/services/pdf_parser.py):

| Benchmark | Workload | Result | Takeaway |
| :--- | :--- | :--- | :--- |
| **Locust Load Mix** | `1,260` reqs across all 8 endpoints | **`43.21 RPS` · `0.0%` errors** | `p50=21 ms`, `p95=120 ms`, `p99=160 ms` |
| **Container Memory** | `api`, `worker`, `qdrant`, `redis` (`docker stats`) | **Peak `182.5 MiB` API / `297.8 MiB` Worker** | `0` OOMs, `0` restarts under mixed traffic |
| **Text Cleaner & Chunker** | `5.33 MiB` (`5.59M` chars), `1200 / 150` | **`495.8 ms` (`10.7 MiB/s`)** | `6,167` chunks vs `18,500` on `500 / 75` (**3x fewer** embeddings) |
| **PyMuPDF PDF Stream** | `100-page` PDF (`82.1 KiB`), `1200 / 150` | **`172.9 ms` (`578 pages/s`)** | 5-run median throughput |

<details>
<summary><strong>Per-endpoint Locust breakdown &amp; Docker container RAM table</strong></summary>

Full report: [`evals/load_test_report.json`](evals/load_test_report.json)

| Endpoint | Requests | Failures | RPS | `p50` | `p95` | `p99` |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| `GET /api/v1/health` | `119` | `0.0%` | `4.08` | `13.0 ms` | `52.0 ms` | `110.0 ms` |
| `POST /api/v1/documents/upload` | `72` | `0.0%` | `2.47` | `34.0 ms` | `77.0 ms` | `130.0 ms` |
| `GET /api/v1/documents/status/{task_id}` | `145` | `0.0%` | `4.97` | `7.0 ms` | `26.0 ms` | `52.0 ms` |
| `GET /api/v1/documents` | `239` | `0.0%` | `8.20` | `13.0 ms` | `67.0 ms` | `77.0 ms` |
| `GET /api/v1/documents/tenants` | `128` | `0.0%` | `4.39` | `10.0 ms` | `30.0 ms` | `90.0 ms` |
| `POST /api/v1/query` | `291` | `0.0%` | `9.98` | `110.0 ms` | `150.0 ms` | `240.0 ms` |
| `POST /api/v1/query/stream` | `194` | `0.0%` | `6.65` | `60.0 ms` | `120.0 ms` | `140.0 ms` |
| `DELETE /api/v1/documents/{document_id}` | `72` | `0.0%` | `2.47` | `11.0 ms` | `25.0 ms` | `60.0 ms` |

| Container | Start RAM | Peak RAM | End RAM | Max CPU | OOM / Restarts |
| :--- | :---: | :---: | :---: | :---: | :---: |
| `corerag-api` | `179.6 MiB` | **`182.5 MiB`** | `182.5 MiB` | `58.23%` | `0 / 0` |
| `corerag-worker` | `244.2 MiB` | **`297.8 MiB`** | `297.8 MiB` | `49.29%` | `0 / 0` |
| `corerag-qdrant` | `86.30 MiB` | **`97.82 MiB`** | `97.73 MiB` | `11.72%` | `0 / 0` |
| `corerag-redis` | `6.53 MiB` | **`9.11 MiB`** | `8.95 MiB` | `1.67%` | `0 / 0` |

</details>

---

## Quickstart

### 1. Clone & Configure `.env`

```bash
git clone https://github.com/DuranGZR/AskDocs.git
cd AskDocs
cp .env.example .env
```

Set a strong `API_KEY` and configure your OpenAI-compatible provider credentials (`QDRANT_VECTOR_SIZE` must match your embedding model's vector dimension—e.g. `3072` for `gemini-embedding-001`):

```env
API_KEY=replace-with-a-strong-random-api-key
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
LLM_API_KEY=your-provider-api-key
LLM_MODEL=gemini-3.5-flash-lite
EMBEDDING_MODEL=gemini-embedding-001
QDRANT_VECTOR_SIZE=3072
```

*(Using OpenAI, vLLM, or another OpenAI-compatible host? Update `LLM_BASE_URL`, model names, and `QDRANT_VECTOR_SIZE` in `.env` accordingly.)*

### 2. Start the Stack

```bash
docker compose up --build
```

Services available once containers turn healthy:
* **AskDocs Web UI:** [http://localhost:8000/](http://localhost:8000/)
* **OpenAPI / Swagger Docs:** [http://localhost:8000/docs](http://localhost:8000/docs)
* **Qdrant Dashboard:** [http://localhost:6333/dashboard](http://localhost:6333/dashboard)
* **Langfuse Traces UI:** [http://localhost:3000](http://localhost:3000)

<details>
<summary><strong>Local Python development without rebuilding API images (uv)</strong></summary>

```bash
uv sync --frozen
docker compose up -d redis qdrant postgres langfuse

# Terminal 1 — FastAPI API & Web UI
uv run uvicorn src.main:app --reload --port 8000

# Terminal 2 — Celery Prefork Worker
uv run celery -A src.tasks.celery_app worker --loglevel=info --concurrency=2
```

> **Windows IPv6 note:** Docker binds Redis on `127.0.0.1:6379`. When running natively on Windows, set `REDIS_URL=redis://127.0.0.1:6379/0` (and `127.0.0.1` for Celery/Qdrant/Langfuse) instead of `localhost`.

</details>

---

## API Reference

All `/api/v1/documents/*` and `/api/v1/query*` endpoints require `X-API-Key: <API_KEY>` (or `Authorization: Bearer <API_KEY>`).

| Method & Path | Rate Limit | Purpose |
| :--- | :---: | :--- |
| `GET /api/v1/health` | Public | Concurrent probes for storage, Redis, Qdrant, and circuit breakers (`200` / `503`) |
| `POST /api/v1/documents/upload` | `5 / min` | Multipart `.pdf` / `.txt` upload with `tenant_id` (`202 Accepted` + `task_id`, `document_id`) |
| `GET /api/v1/documents/status/{task_id}` | `120 / min` | Celery indexing state (`PENDING`, `STARTED`, `RETRY`, `SUCCESS`, `FAILURE`) |
| `GET /api/v1/documents` | `120 / min` | List indexed documents and chunk counts filtered by `tenant_id` |
| `GET /api/v1/documents/tenants` | `120 / min` | Discover existing workspace `tenant_id` values via Qdrant facets |
| `DELETE /api/v1/documents/{document_id}` | `5 / min` | Delete all tenant-isolated vectors for a document |
| `POST /api/v1/query` | `20 / min` | Grounded RAG query returning JSON (`stream=false`) or SSE (`stream=true`) |
| `POST /api/v1/query/stream` | `20 / min` | Dedicated SSE endpoint supporting chat `history` and `document_ids` scoping |

<details>
<summary><strong>End-to-End cURL Workflow &amp; SSE Frame Format</strong></summary>

```bash
# 1. Upload a PDF to workspace "acme-legal"
curl -s -X POST http://localhost:8000/api/v1/documents/upload \
  -H "X-API-Key: change-me-in-local-env" \
  -F "tenant_id=acme-legal" \
  -F "file=@contract.pdf"

# 2. Poll background indexing status
curl -s http://localhost:8000/api/v1/documents/status/<task_id> \
  -H "X-API-Key: change-me-in-local-env"

# 3. Stream a grounded answer over SSE
curl -N -X POST http://localhost:8000/api/v1/query/stream \
  -H "X-API-Key: change-me-in-local-env" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "What is the termination notice period?",
    "tenant_id": "acme-legal",
    "history": []
  }'
```

Wire frames emitted by `/api/v1/query/stream`:

```text
event: token
data: {"event":"token","delta":"The termination notice period is 30 calendar days [Source 1]."}

event: citations
data: {"event":"citations","query":"...","tenant_id":"acme-legal","citations":[{"index":1,"document_id":"...","filename":"contract.pdf","page_number":4,"score":0.8421,"text":"..."}]}

event: done
data: [DONE]
```

</details>

---

## Quality Gates & Project Layout

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs formatting, linting, strict `mypy` type checking, and `136` network-isolated unit/integration tests on every push and pull request:

```bash
# Format, lint, static types, JS syntax & isolated tests (136 passing)
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests scripts locustfile.py
node --check src/ui/app.js
uv run pytest -q

# Live concurrency, RAG quality & Locust load scripts
uv run python scripts/test_queue_concurrency.py --count 10
uv run python scripts/evaluate_rag.py --delay-seconds 3.5
uv run python scripts/run_load_test.py --users 20 --spawn-rate 5 --duration 30s --with-mock-ai
```

<details>
<summary><strong>Repository Directory Tree</strong></summary>

```text
AskDocs/
├── .github/workflows/ci.yml       # Ruff, Mypy & Pytest CI pipeline
├── docs/images/                   # Hero SVG, architecture SVG, eval chart & UI screenshots
├── evals/
│   ├── golden_dataset.json        # 44-question 9-PDF + adversarial trap eval set
│   ├── latest_eval_report.json    # Latest calibration run (Recall@5, MRR, Faithfulness)
│   ├── load_test_report.json      # 20-user Locust latency & Docker container RAM report
│   └── README.md                  # Hyperparameter calibration notes
├── scripts/
│   ├── evaluate_rag.py            # Golden Dataset runner + threshold sweep + Langfuse logging
│   ├── run_load_test.py           # Locust orchestrator + docker stats memory sampler
│   └── test_queue_concurrency.py  # Concurrent upload & retry verification harness
├── src/
│   ├── api/                       # Auth, Redis rate limiting, DI & v1 REST/SSE routes
│   ├── core/                      # Config, circuit breakers, logging, middleware & Langfuse
│   ├── evals/                     # Metric calculators (Recall@K, MRR, Faithfulness, Locust)
│   ├── schemas/                   # Pydantic v2 request/response & SSE schemas
│   ├── services/                  # ABC contracts, storage, PyMuPDF parser, Qdrant & LLM clients
│   ├── tasks/                     # Celery app & idempotent indexing worker task
│   ├── ui/                        # Cupertino Light Web UI (HTML, CSS, vanilla JS)
│   └── main.py                    # FastAPI lifespan, security middlewares & UI mount
├── tests/                         # 136 isolated unit & integration tests
├── Dockerfile                     # Multi-stage non-root runtime (UID/GID 10001)
├── docker-compose.yml             # API, Celery Worker, Redis, Qdrant, Postgres, Langfuse
├── locustfile.py                  # Locust traffic mix across upload, poll, query, stream, delete
└── pyproject.toml                 # Dependencies, uv lock & Ruff/Mypy/Pytest configuration
```

</details>
