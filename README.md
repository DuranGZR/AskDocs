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
    <code>Rust (PyO3)</code> ·
    <code>FastAPI</code> ·
    <code>Celery &amp; Redis</code> ·
    <code>Qdrant</code> ·
    <code>Gemini / OpenAI-Compat</code> ·
    <code>Langfuse</code> ·
    <code>169 Tests (163 Py + 6 Rust)</code> ·
    <code>88.6% Branch Cov</code>
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

Ask a cross-paper comparison question (English or Turkish—here queried in Turkish across English arXiv papers) and watch structured Markdown stream word-by-word with clickable citation pills, a clean copy button that strips citation markers, contextual follow-up actions (`Evet, öğrenmek isterim →`), and per-page relevance badges underneath.

<div align="center">
  <img src="docs/images/ui-chat-citations.png" alt="AskDocs — Grounded Streaming Answer &amp; Citations" width="96%" />
</div>

### `#3` / Verify Claims in the Source Inspector

Click citation `[1]` to slide out the Cupertino Source Inspector right to page 2 of `crag_2401_15884.pdf`, displaying the `78%` similarity match and highlighted terms (`CRAG`, `Self-RAG`) so every claim can be verified in seconds.

<div align="center">
  <img src="docs/images/ui-source-inspector.png" alt="AskDocs — Slide-out Source Inspector Drawer" width="96%" />
</div>

---

## Architecture & Tech Stack

<div align="center">
  <img src="docs/images/architecture.svg" alt="AskDocs System Architecture" width="100%" />
</div>

AskDocs strictly separates heavy document ingestion (**Write Path**) from low-latency conversational streaming (**Read Path**):

* **1. Upload & Index (Write Path):**
  `PDF Upload` → `FastAPI (202 Accepted)` → `Celery Worker` → `PyMuPDF + Rust Chunking` → `Qdrant Vectors`
  Large PDF uploads never stall the API: FastAPI validates the request and hands off work immediately. Workers stream pages incrementally, clean and split text (`1200` chars / `150` overlap) via the **Rust (`PyO3`)** engine, and index deduplicated vectors into Qdrant.

* **2. Ask & Verify (Read Path):**
  `Question` → `Qdrant Similarity Search` → `Security & Context Filter` → `SSE Stream + Citation Cards`
  Short follow-ups resolve against prior conversation context, and Qdrant retrieves the top 5 matching chunks (`0.70` cutoff) from the active workspace. Missing facts trigger honest abstention; grounded answers stream word-by-word with clickable `[1]`, `[2]` source badges.

### Stack Breakdown

| Layer | Choice | Why It Is Here |
| :--- | :--- | :--- |
| **API & Web UI** | FastAPI + Vanilla JS | Async REST/SSE delivery, SHA-256 API-Key auth, and a zero-framework Cupertino UI |
| **Queue & Workers** | Celery + Redis | Isolates CPU/network indexing bursts from FastAPI and tracks sliding-window rate limits |
| **PDF & Text Core** | PyMuPDF + Rust (`PyO3`) | Incremental page streaming plus a native, GIL-free Rust text cleaner and chunker |
| **Vector Store** | Qdrant | Tenant-isolated HNSW search, document scoping, and duplicate-free vector upserts |
| **LLM & Embeddings** | Gemini (OpenAI-Compat) | Connection-pooled `gemini-3.5-flash-lite` generation and `3072`-dim embeddings |
| **Telemetry** | Langfuse + PostgreSQL | Fail-open traces for retrieval scores, TTFT latency, and evaluation runs |

---

## Engineering Decisions

| Problem | Solution | Practical Impact |
| :--- | :--- | :--- |
| **Large PDFs blocking Python & CPU cores** | Rewrote text normalization and chunking in native **Rust (`PyO3`)** | **16.6x** faster cleaning, **9.7x** faster chunking, and **3.7x** lower Python RAM |
| **API freezing during file uploads** | Kept FastAPI purely async; offloaded indexing to Celery workers | Instant `202 Accepted` uploads; zero impact on concurrent chat latency |
| **Orphan files or duplicate vectors on retry** | Compensating disk cleanup (`rollback`) and deterministic `UUID5` point IDs | Zero leaked temporary files and zero duplicate chunks across retries |
| **One tenant or outage stalling everyone** | Three-state **Circuit Breakers** and atomic Redis rate limiting (`fail-open`) | Fast `503` protection on outages; Redis or Langfuse downtime never stops search |
| **LLM hallucination or prompt injection** | Calibrated `0.70` cutoff, `<context>` sanitization, and `[DONE]` stream checks | **100%** trap-question abstention; truncated streams never pollute chat history |

---

## Benchmarks & Evaluations

All metrics below are measured directly from reproducible evaluation, CPU/memory comparison, and load-test harnesses in this repository.

### 1. RAG Accuracy & Hallucination Benchmark (`rag-research` · 6 PDFs)

To test semantic disambiguation across documents with heavy terminology overlap, AskDocs is evaluated inside the `rag-research` workspace across **6 competing arXiv RAG papers (385 total chunks)** using **24 questions** (18 answerable English/Turkish queries + 6 unanswerable trap questions):

| Metric | Result | What It Means |
| :--- | :---: | :--- |
| **Source Retrieval (`Recall@5`)** | **`1.0000`** (`18 / 18`) | Every answerable query surfaced the exact expected paper & page inside Top-5 |
| **First-Rank Precision (`MRR`)** | **`0.7824`** | Correct chunks consistently ranked near the top across 6 overlapping papers |
| **Grounded Faithfulness** | **`0.9729`** | All inline `[1]`, `[2]` citations mapped to real pages with zero fabricated sources |
| **Keyword Coverage** | **`0.9398`** | Almost 94% of expected technical terms and metrics appeared accurately in EN & TR |
| **Trap Rejection (`Abstention`)** | **`1.0000`** (`6 / 6`) | All 6 unanswerable and adversarial traps were declined without hallucination |

<div align="center">
  <img src="docs/images/eval-threshold-chart.svg" alt="6-Paper RAG Calibration Sweep" width="100%" />
</div>

<details>
<summary><strong>Open similarity threshold (0.30–0.75) calibration table</strong></summary>

Run: `eval-20261118-193027` · Workspace: `rag-research` (6 PDFs / 385 chunks) · `gemini-3.5-flash-lite` + `gemini-embedding-001` · [`evals/latest_eval_report.json`](evals/latest_eval_report.json)

| Score Threshold | `Recall@5` | `MRR` | Avg Chunks | Trap Noise (`NegNoise`) |
| :---: | :---: | :---: | :---: | :---: |
| `0.30` | `1.0000` | `0.7824` | `5.00` | `1.0000` |
| `0.40` | `1.0000` | `0.7824` | `5.00` | `1.0000` |
| `0.50` | `1.0000` | `0.7824` | `5.00` | `1.0000` |
| `0.60` | `1.0000` | `0.7824` | `5.00` | `0.8333` |
| `0.65` | `1.0000` | `0.7824` | `5.00` | `0.6667` |
| `0.70` (Default) | `1.0000` | `0.7824` | `5.00` | `0.6667` |
| **`0.75` (Recommended)** | **`1.0000`** | **`0.7824`** | **`4.44` (-11.2%)** | **`0.1667` (-83.3%)** |

Tightening the cutoff to `0.75` eliminates **83.3% of trap-query noise** and reduces LLM context token cost by **11.2%** with zero loss in `Recall@5` (`1.0000`) or `MRR` (`0.7824`).

</details>

### 2. What Rust (`PyO3`) Unlocked (50 MB Text Microbenchmark)

Comparing pure Python against the compiled Rust engine ([`evals/text_processor_benchmark.json`](evals/text_processor_benchmark.json)) on an isolated 50 MB (~50 million chars) noisy text corpus with zero network or GPU latency:

| Operation | Pure Python | Rust (`PyO3`) | Gain |
| :--- | :---: | :---: | :--- |
| **Text Cleaning (`clean_text`)** | `4.89 s` (`10.2 MB/s`) | **`0.29 s` (`170.0 MB/s`)** | **16.6x faster** |
| **Text Chunking (`chunk_text` · 69k chunks)** | `4.93 s` (`10.1 MB/s`) | **`0.51 s` (`98.6 MB/s`)** | **9.7x faster** |
| **Chunking Memory Peak (Python Heap)** | `749.5 MB` | **`202.1 MB`** | **3.7x less RAM** |

* **Why such a large jump?** Python allocates hundreds of megabytes of transient Unicode copies across chained regex passes while holding the GIL.
* **Zero Lock Contention, Exact Parity:** Rust scans linearly outside the Python heap without holding the GIL, producing **100% character-identical chunks** to Python.
* **Zero Runtime Bloat:** Built in multi-stage Docker so no Rust compiler ships to production, with automatic fallback to pure Python if the compiled wheel is absent.

### 3. Load Test & PDF Stream Throughput

30-second Locust concurrency run with 20 virtual users (with mock AI isolating orchestration overhead from external API quotas) and PyMuPDF page-streaming throughput:

| Benchmark | Workload | Result | Takeaway |
| :--- | :--- | :--- | :--- |
| **Locust Load Mix** | 8 endpoints, 1,260 requests | **`43.21 RPS` · 0.0% errors** | `p50=21 ms`, `p95=120 ms`, `p99=160 ms` |
| **Container Memory** | 4 core services (`docker stats`) | **Peak `182.5 MiB` API / `297.8 MiB` Worker** | 0 OOMs and 0 restarts under mixed load |
| **PyMuPDF PDF Stream** | 100-page PDF stream | **`172.9 ms` (`578 pages/s`)** | 5-run median throughput |
| **Smart Chunk Size (`1200/150`)** | 5.33 MB sample text | **6,167 chunks** | **3x fewer** embeddings vs `500/75` (18,500) |

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

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) enforces Rust lint/safety checks, Python formatting/linting, strict `mypy` type checking, JS syntax validation, `pytest-cov` (`>= 85%` branch coverage gate across `163` Python + `6` Rust tests), and multi-stage non-root Docker Buildx verification on every push and pull request:

```bash
# Rust checks (inside text_processor/)
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test

# Python & JS formatting, lint, static types, syntax & isolated test suite (163 Py + 6 Rust = 169 passing)
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests scripts locustfile.py
node --check src/ui/app.js
uv run pytest -v --cov=src --cov-report=term-missing

# Live concurrency, 6-PDF RAG quality, Rust vs Python microbenchmark & Locust load scripts
uv run python scripts/test_queue_concurrency.py --count 10
uv run python scripts/evaluate_rag.py --score-threshold 0.70 --thresholds "0.30,0.40,0.50,0.60,0.65,0.70,0.75" --delay-seconds 5.0
uv run --no-sync python scripts/benchmark_text_processor.py --size-mb 50 --iterations 3
uv run python scripts/run_load_test.py --users 20 --spawn-rate 5 --duration 30s --with-mock-ai
```

<details>
<summary><strong>Repository Directory Tree</strong></summary>

```text
AskDocs/
├── .github/workflows/ci.yml       # Rust, Ruff, Mypy, Pytest-cov (85%+ branch) & Docker CI pipeline
├── docs/images/                   # Hero SVG, architecture SVG, eval chart & UI screenshots
├── evals/
│   ├── golden_dataset.json        # 24-question 6-PDF (rag-research) + adversarial trap eval set
│   ├── latest_eval_report.json    # Latest calibration run (Recall@5, MRR, Faithfulness)
│   ├── load_test_report.json      # 20-user Locust latency & Docker container RAM report
│   ├── text_processor_benchmark.json # 50 MiB Rust vs. Python CPU & heap microbenchmark report
│   └── README.md                  # Hyperparameter calibration & Rust benchmark notes
├── scripts/
│   ├── benchmark_text_processor.py # 50 MiB clean/chunk CPU & tracemalloc parity harness
│   ├── evaluate_rag.py            # Golden Dataset runner + threshold sweep + Langfuse logging
│   ├── run_load_test.py           # Locust orchestrator + docker stats memory sampler
│   └── test_queue_concurrency.py  # Concurrent upload & retry verification harness
├── src/
│   ├── api/                       # Auth, Redis rate limiting, DI & v1 REST/SSE routes
│   ├── core/                      # Config, circuit breakers, logging, middleware & Langfuse
│   ├── evals/                     # Metric calculators (Recall@K, MRR, Faithfulness, Locust)
│   ├── schemas/                   # Pydantic v2 request/response & SSE schemas
│   ├── services/                  # ABC contracts, storage, PyMuPDF + Rust loader, Qdrant & LLM
│   ├── tasks/                     # Celery app & idempotent indexing worker task
│   ├── ui/                        # Cupertino Light Web UI (HTML, CSS, vanilla JS)
│   └── main.py                    # FastAPI lifespan, security middlewares & UI mount
├── text_processor/
│   ├── python/text_processor/     # Typed .pyi stubs & Python package wrapper
│   ├── src/lib.rs                 # #![forbid(unsafe_code)] PyO3 cleaner & O(chunk_size) chunker
│   ├── Cargo.toml                 # abi3-py311 release/dev-opt crate manifest
│   └── pyproject.toml             # Maturin PEP 517 build configuration
├── tests/                         # 163 isolated Python unit/integration tests + 6 Rust tests
├── Dockerfile                     # 3-stage Rust wheel + Python builder -> non-root UID 10001 runtime
├── docker-compose.yml             # API, Celery Worker, Redis, Qdrant, Postgres, Langfuse
├── locustfile.py                  # Locust traffic mix across upload, poll, query, stream, delete
└── pyproject.toml                 # Dependencies, uv lock & Ruff/Mypy/Pytest/Coverage config
```

</details>
