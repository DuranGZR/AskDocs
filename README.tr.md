<div align="center">

<br />

<img src="docs/images/hero-banner.svg" alt="AskDocs — Upload. Index. Verify." width="100%" />

<br />
<br />

**Dokümanlarınızı izole workspace'lere yükleyin, doğal dille sorular sorun ve üretilen her yanıtı birebir kaynak sayfasıyla doğrulayın.**

<p>
  <a href="#neden-askdocs">Neden AskDocs</a> ·
  <a href="#canlı-arayüz">Canlı Arayüz</a> ·
  <a href="#mimari-ve-tech-stack">Mimari</a> ·
  <a href="#tasarım-kararları">Tasarım Kararları</a> ·
  <a href="#benchmark-ve-ölçümler">Benchmark</a> ·
  <a href="#hızlı-kurulum">Kurulum</a> ·
  <a href="#api-referansı">API</a> ·
  <a href="README.md">English</a>
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

## Neden AskDocs?

Belgelerle sohbet eden bir sistem kurmak, yalnızca bir vector DB'nin önüne LLM bağlamaktan ibaret değildir. Büyük bir PDF yüklendiğinde API sunucusu sayfaları parse edip embedding üretmeye çalışırken diğer tüm istekleri kilitleyebilir. Farklı projelerin dokümanları aynı koleksiyonda karışabilir, geçici ağ hatalarındaki retry'lar Qdrant'ta duplicate vektörler üretebilir ve kullanıcı ekrandaki cevabın belgenin hangi sayfasından geldiğini göremez.

**AskDocs**, opak framework wrapper'ları yerine her katmanı şeffaf, hızlı ve öngörülebilir bir RAG platformu olarak sıfırdan geliştirildi.

### Öne Çıkan Özellikler

* **Tenant Workspace İzolasyonu ve Document Scoping:** Her belge ve sorgu ayrı bir `tenant` workspace'ine izole edilir; dilerseniz sol panelden belirli PDF'leri seçip aramayı yalnızca seçtiğiniz belgelerle daraltabilirsiniz.
* **Non-Blocking Arka Plan İndeksleme:** Upload isteği dosya doğrulamasını yapıp anında `202 Accepted` döner; PDF parsing, temizleme, chunking ve batch embedding üretimi arka plandaki Celery worker'larına devredilir.
* **Inline Citation'lar ve Source Inspector:** Yanıtlar SSE ile canlı gelirken her iddia tıklanabilir `[1]`, `[2]` badge'leri taşır. Bir badge'e tıklandığında sağdan açılan Source Inspector sizi doğrudan ilgili sayfaya, similarity score'una ve highlight edilmiş pasaja götürür.
* **Follow-Up Zekâsı ve Guardrail'ler:** Kısa takip sorularını önceki sohbet bağlamından çözer, belge içindeki `<context>` prompt-injection etiketlerini nötralize eder ve kanıt yoksa bilgi uydurmadan reddeder (`abstention`).
* **Resilience ve OpenAI-Compatible Esneklik:** Async ve sync Circuit Breaker'lar Qdrant veya LLM kesintilerini izole eder, Redis Rate Limiter ve Langfuse observability'si kesintilerde `fail-open` çalışır; `.env` üzerinden Gemini veya herhangi bir OpenAI-compatible API kolayca bağlanır.

---

## Canlı Arayüz

Aşağıdaki üç adımda arXiv'den alınan altı retrieval makalesinin (`REALM`, `DPR`, `FiD`, `HyDE`, `Self-RAG` ve `CRAG`) diğer workspace'lerden tamamen yalıtılmış **`rag-research`** alanındaki canlı kullanımını görebilirsiniz.

### `#1` / Workspace Seçimi ve Document Scoping

Sol alttan `tenant` workspace'leri arasında geçiş yapın, çoklu PDF veya düz metin yükleyin ve yalnızca `crag_2401_15884.pdf` ile `selfrag_2310_11511.pdf` dosyalarını işaretleyerek aramayı bu iki kaynakla sınırlandırın.

<div align="center">
  <img src="docs/images/ui-workspace-upload.png" alt="AskDocs — 6 PDF'li İzole rag-research Workspace ve Scoping" width="96%" />
</div>

### `#2` / Grounded Streaming ve Inline Citation'lar

Seçili makaleleri kıyaslayan bir soru sorduğunuzda yanıt yapılandırılmış Markdown formatında stream edilir. Her iddia tıklanabilir citation pill'leri, ham etiketleri temizleyen kopyalama butonu, follow-up önerisi ve sayfa/benzerlik kartlarıyla sunulur.

<div align="center">
  <img src="docs/images/ui-chat-citations.png" alt="AskDocs — Citation Destekli SSE Streaming Yanıtı" width="96%" />
</div>

### `#3` / Source Inspector ile Doğrulama

İlk citation badge'ine tıkladığınızda sağdan açılan Cupertino Source Inspector paneli, sizi doğrudan `crag_2401_15884.pdf` belgesinin 15. sayfasına, `%73` match skoruna ve highlight edilmiş pasajlara götürür.

<div align="center">
  <img src="docs/images/ui-source-inspector.png" alt="AskDocs — Sağdan Açılan Source Inspector Paneli" width="96%" />
</div>

---

## Mimari ve Tech Stack

<div align="center">
  <img src="docs/images/architecture.svg" alt="AskDocs System Architecture" width="100%" />
</div>

Sistem ağır doküman işleme yükünü (**Write Path**) düşük gecikmeli kullanıcı sorgularından (**Read Path**) net biçimde ayırır:

1. **Upload ve İndeksleme (Write Path):** FastAPI API key, sliding-window rate limit ve dosya header (`%PDF-` veya UTF-8) doğrulamasını yaptıktan sonra dosyayı UUID adıyla shared disk'e stream eder, işi Redis üzerinden Celery queue'ya atar ve hemen `202 Accepted` döner. Worker süreci PyMuPDF ile sayfaları okur, format gürültüsünü temizler, metni `1200` karakterlik `150` overlap'li chunk'lara böler, `32`'lik batch'ler halinde embed edip Qdrant'a idempotent şekilde upsert eder.
2. **Sorgulama ve SSE Streaming (Read Path):** İstek opsiyonel `document_ids` filtresi ve chat history ile gelir. Kısa onay/takip soruları önceki bağlamla çözülür; Qdrant aktif `tenant` içinde `0.70` similarity threshold'uyla top-5 chunk getirir (sonuç boş dönerse `0.58` fallback threshold'u devreye girer). Context guard ve injection filtrelerinden geçen parçalar doğrulamalı SSE stream'i olarak tarayıcıya iletilir.

### Tech Stack Özeti

| Katman | Tercih | Projedeki Rolü |
| :--- | :--- | :--- |
| **API & Web UI** | FastAPI + Vanilla JS | Async REST/SSE delivery, SHA-256 API-Key auth, güvenlik middleware'leri ve Cupertino Web UI |
| **Queue & Worker'lar** | Celery + Redis | Ağır PDF/embedding yükünü FastAPI event-loop'undan ayırır ve sliding-window rate limit tutar |
| **PDF Parsing & Chunking** | PyMuPDF + Custom Chunker | Streaming sayfa okuma, tire/ligatür temizliği ve cümle sınırlarına hizalı overlap'li chunking |
| **Vector Search** | Qdrant | `tenant` bazlı indekslenmiş HNSW arama, document scoping ve duplicate'leri önleyen `UUID5` upsert'i |
| **LLM & Embedding** | Gemini (`OpenAI-Compat` / `httpx`) | Connection-pooled HTTP üzerinden `gemini-3.5-flash-lite` üretimi ve `3072`-dim embedding |
| **Observability** | Langfuse + PostgreSQL | Retrieval skorları, TTFT latency, token hesabı ve eval run'larının `fail-open` trace kaydı |

<details>
<summary><strong>Arka plan detayları: Security middleware'leri, atomic rate limit ve fault tolerance</strong></summary>

* **Async vs. Sync ayrımı:** FastAPI route'ları strictly async istemciler (`src/services/qdrant_service.py`, `src/services/llm_service.py`) çalıştırırken Celery `prefork` worker'ları aynı Clean Architecture interface'lerinin (`src/services/base.py`) senkron implementasyonunu kullanır; böylece closed event-loop hatası yaşanmaz.
* **Constant-length auth & IDOR koruması:** Constant-time `hmac.compare_digest` öncesi anahtarlar sabit `32` baytlık SHA-256 hash'ine dönüştürülerek (`src/api/dependencies.py`) uzunluk temelli timing sızıntısı önlenir; `MaxBodySizeMiddleware` `100 MiB` üstü istekleri multipart parse edilmeden önce reddeder ve vector arama/silme işlemleri daima compound `(tenant_id, document_id)` filtresiyle çalışır.
* **Atomic Lua sliding-window rate limiter:** Redis sorted-set Lua script'i (`src/services/rate_limiter.py`), kota dolduğunda gelen `429` burst isteklerini sete eklemeden geri çevirir (istemciyi sonsuz kilide sokmaz) ve Redis ya da Langfuse kesintilerinde kısa timeout ile `fail-open` çalışır.

</details>

---

## Tasarım Kararları

### Thin Custom Core ve Async / Sync Süreç Ayrımı
Opak RAG framework wrapper'ları yerine doğrudan `httpx`, `qdrant-client` ve PyMuPDF üzerine kurulu ince servis katmanı, stream memory tüketimini ve SSE hata akışını tam kontrol altında tutar. FastAPI'yi tamamen async bırakıp Celery `prefork` worker'larını senkron çalıştırmak concurrent yük altında event-loop çakışmalarını sıfırlar; embedding parser ise `/v1/embeddings` cevabında dönen vektörleri liste sırasına güvenmeden `item["index"]` değerine göre yeniden sıralar.

### Sıfır Dosya Sızıntılı Upload, Rollback ve Idempotent Retry
Upload ile arka plan indexing arasındaki tüm olası kopma noktaları düşünülmüştür: dosya shared disk'e stream edildikten hemen sonra Redis queue gönderimi başarısız olursa endpoint otomatik telafi silmesi (`storage.delete`) çalıştırır; worker içinde retry gerektiren geçici bir hata yaşandığında geçici dosya sonraki jitter'lı exponential backoff denemeleri (`max_retries=3`) okuyabilsin diye korunur ve yalnızca kesin bitişte (`finally`) temizlenir; `uuid5(document_id, chunk_index)` point ID'leri ise retry'larda Qdrant'a duplicate vector yazılmasını imkânsız kılar.

### Tenant Bazlı Etki Alanını Sınırlayan Circuit Breaker'lar
Async ve sync `3-state` Circuit Breaker'lar (`src/core/circuit_breaker.py`), gerçek Qdrant veya LLM kesintilerinde `503` ile sistemi korur. Kritik nokta olarak; tekil kullanıcı kaynaklı erken duruşlar (`length`, `content_filter`) HTTP `422` olarak sınıflandırılır ve ortak Circuit Breaker hatası sayılmaz—böylece bir kullanıcının token limitine takılan sorgusu diğer tenant'ların trafiğini durdurmaz.

### Calibrated Chunking, Injection Guard ve Stream Doğrulaması
`1200 / 150` karakterlik cümle hizalı chunking anlam bütünlüğünü korurken embedding çağrı sayısını **3 kat azaltır**. `0.70` similarity threshold'u (`0.58` fallback ile) context gürültüsünü keser, `<context>` redaksiyonu belge içi indirect prompt injection'ı engeller ve hem sunucu hem tarayıcı `finish_reason` ile `[DONE]` sınırlarını doğrulayarak yarıda kesilen yanıtların chat history'yi kirletmesini önler.

---

## Benchmark ve Ölçümler

Aşağıdaki tüm metrikler repodaki tekrarlanabilir evaluation, load test ve CPU benchmark script'lerinden ölçülmüştür.

### 1. RAG Retrieval & Faithfulness Değerlendirmesi

Ortak terminoloji paylaşan dokümanlar arasında gerçek semantic ayrıştırma gücünü test etmek için sistem; **7 adet RAG/retrieval makalesi** (`RAG`, `REALM`, `DPR`, `FiD`, `HyDE`, `Self-RAG`, `CRAG`), Bitcoin whitepaper'ı ve Türkçe İK el kitabından oluşan **9 PDF (`535` chunk)** ile **44 soruluk Golden Dataset** ([`evals/golden_dataset.json`](evals/golden_dataset.json)) üzerinde test edildi. Soruların `6`'sı belge terimlerini kullanarak halüsinasyonu tetiklemeyi hedefleyen unanswerable / adversarial sorulardan oluşur.

| Metrik | Ölçülen Sonuç | Anlamı |
| :--- | :---: | :--- |
| **Mean Recall@5** | **`1.0000`** (`38 / 38`) | Cevaplanabilir tüm sorularda beklenen doküman ve sayfa Top-5 içinde geldi |
| **Mean MRR** | **`0.8969`** | Aynı alandaki 7 makale rekabetine rağmen yüksek ilk-sıra ranking kalitesi |
| **Mean Faithfulness** | **`0.9591`** | Geçerli `[Source N]` grounding uyumu; out-of-range veya uydurma atıf sıfır |
| **Keyword Coverage** | **`0.9386`** | Türkçe ve İngilizce dokümanlardaki kritik teknik kavram/sayı kapsamı |
| **Abstention Accuracy** | **`1.0000`** (`6 / 6`) | Genel ve near-miss adversarial tüm negatif sorularda halüsinasyonsuz ret |

<div align="center">
  <img src="docs/images/eval-threshold-chart.svg" alt="Data-Driven Threshold Calibration Sweep" width="100%" />
</div>

<details>
<summary><strong>0.30–0.75 threshold sweep tablosu ve run parametreleri</strong></summary>

Run: `eval-20261117-183520` · `gemini-3.5-flash-lite` + `gemini-embedding-001` · `top_k=5` · [`evals/latest_eval_report.json`](evals/latest_eval_report.json) · [`evals/README.md`](evals/README.md)

| `score_threshold` | `Recall@5` | `MRR` | `AvgChunks` (LLM'e Giden) | `NegNoise` (6 Negatif Sorgu) |
| :---: | :---: | :---: | :---: | :---: |
| `0.30` | `1.0000` | `0.8969` | `5.00` | `1.0000` |
| `0.40` | `1.0000` | `0.8969` | `5.00` | `1.0000` |
| `0.50` | `1.0000` | `0.8969` | `5.00` | `1.0000` |
| `0.60` | `1.0000` | `0.8969` | `4.97` | `1.0000` |
| `0.65` | `1.0000` | `0.8969` | `4.61` | `1.0000` |
| **`0.70` (Seçilen)** | **`1.0000`** | **`0.8969`** | **`4.11` (-%17.8)** | **`0.6667`** |
| `0.75` | `0.9211` | `0.8180` | `3.11` | `0.1667` |

`0.70` threshold'u hem `Recall@5` (`1.0000`) hem `MRR` (`0.8969`) değerlerini korurken LLM'e giden ortalama chunk sayısını **`%17.8` düşürür** (`5.00 -> 4.11`). `0.75`'e çıkmak ise pozitif sorularda `Recall@5`'i `%7.9` düşürür (`0.9211`).

</details>

### 2. Load Test ve Parser / Chunker Hızı

Dış LLM kota latency'sini izole etmek için `--with-mock-ai` moduyla yürütülen 20 concurrent user Locust load testi (`30s`) ve Python 3.11 üzerinde [`src/services/pdf_parser.py`](src/services/pdf_parser.py) CPU benchmark sonuçları:

| Benchmark | Kapsam | Sonuç | Özet Not |
| :--- | :--- | :--- | :--- |
| **Locust Load Test** | `8` endpoint, toplam `1,260` request | **`43.21 RPS` · `%0.0` hata** | `p50=21 ms`, `p95=120 ms`, `p99=160 ms` |
| **Container RAM Kararlılığı** | `api`, `worker`, `qdrant`, `redis` (`docker stats`) | **Peak `182.5 MiB` API / `297.8 MiB` Worker** | Karma trafikte `0` OOM, `0` restart |
| **Text Cleaning & Chunking** | `5.33 MiB` (`5.59M` char), `1200 / 150` | **`495.8 ms` (`10.7 MiB/sn`)** | `500 / 75` ayarına (`18,500`) göre **3x daha az** (`6,167`) embedding |
| **PyMuPDF Streaming Parser** | `100 sayfa` PDF (`82.1 KiB`), `1200 / 150` | **`172.9 ms` (`578 sayfa/sn`)** | 5 run medyan hızı |

<details>
<summary><strong>Endpoint bazlı Locust latency ve Docker container RAM tablosunu aç</strong></summary>

Tam rapor: [`evals/load_test_report.json`](evals/load_test_report.json)

| Endpoint | Request | Hata | RPS | `p50` | `p95` | `p99` |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| `GET /api/v1/health` | `119` | `%0.0` | `4.08` | `13.0 ms` | `52.0 ms` | `110.0 ms` |
| `POST /api/v1/documents/upload` | `72` | `%0.0` | `2.47` | `34.0 ms` | `77.0 ms` | `130.0 ms` |
| `GET /api/v1/documents/status/{task_id}` | `145` | `%0.0` | `4.97` | `7.0 ms` | `26.0 ms` | `52.0 ms` |
| `GET /api/v1/documents` | `239` | `%0.0` | `8.20` | `13.0 ms` | `67.0 ms` | `77.0 ms` |
| `GET /api/v1/documents/tenants` | `128` | `%0.0` | `4.39` | `10.0 ms` | `30.0 ms` | `90.0 ms` |
| `POST /api/v1/query` | `291` | `%0.0` | `9.98` | `110.0 ms` | `150.0 ms` | `240.0 ms` |
| `POST /api/v1/query/stream` | `194` | `%0.0` | `6.65` | `60.0 ms` | `120.0 ms` | `140.0 ms` |
| `DELETE /api/v1/documents/{document_id}` | `72` | `%0.0` | `2.47` | `11.0 ms` | `25.0 ms` | `60.0 ms` |

| Container | Başlangıç RAM | Peak RAM | Bitiş RAM | Max CPU | OOM / Restart |
| :--- | :---: | :---: | :---: | :---: | :---: |
| `corerag-api` | `179.6 MiB` | **`182.5 MiB`** | `182.5 MiB` | `58.23%` | `0 / 0` |
| `corerag-worker` | `244.2 MiB` | **`297.8 MiB`** | `297.8 MiB` | `49.29%` | `0 / 0` |
| `corerag-qdrant` | `86.30 MiB` | **`97.82 MiB`** | `97.73 MiB` | `11.72%` | `0 / 0` |
| `corerag-redis` | `6.53 MiB` | **`9.11 MiB`** | `8.95 MiB` | `1.67%` | `0 / 0` |

</details>

---

## Hızlı Kurulum

### 1. Klonlama ve `.env` Ayarları

```bash
git clone https://github.com/DuranGZR/AskDocs.git
cd AskDocs
cp .env.example .env
```

`.env` içinde güçlü bir `API_KEY` ve OpenAI-compatible provider bilgilerinizi tanımlayın (`QDRANT_VECTOR_SIZE` seçilen embedding modelinin vektör boyutuyla eşleşmelidir; örneğin `gemini-embedding-001` için `3072`):

```env
API_KEY=guclu-bir-rastgele-api-anahtari
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
LLM_API_KEY=saglayici-api-anahtariniz
LLM_MODEL=gemini-3.5-flash-lite
EMBEDDING_MODEL=gemini-embedding-001
QDRANT_VECTOR_SIZE=3072
```

*(OpenAI, vLLM veya başka bir OpenAI-compatible API kullanacaksanız `LLM_BASE_URL`, model adları ve `QDRANT_VECTOR_SIZE` değerlerini kendi sağlayıcınıza göre güncelleyebilirsiniz.)*

### 2. Docker Compose ile Başlatma

```bash
docker compose up --build
```

Container'lar ayağa kalktığında açılan servisler:
* **AskDocs Web UI:** [http://localhost:8000/](http://localhost:8000/)
* **OpenAPI / Swagger Docs:** [http://localhost:8000/docs](http://localhost:8000/docs)
* **Qdrant Dashboard:** [http://localhost:6333/dashboard](http://localhost:6333/dashboard)
* **Langfuse Observability UI:** [http://localhost:3000](http://localhost:3000)

<details>
<summary><strong>Image build etmeden lokal geliştirme (uv)</strong></summary>

```bash
uv sync --frozen
docker compose up -d redis qdrant postgres langfuse

# Terminal 1 — FastAPI API & Web UI
uv run uvicorn src.main:app --reload --port 8000

# Terminal 2 — Celery Prefork Worker
uv run celery -A src.tasks.celery_app worker --loglevel=info --concurrency=2
```

> **Windows IPv6 notu:** Docker Redis'i `127.0.0.1:6379` üzerinden bind ettiği için Windows üzerinde doğrudan çalıştırırken `localhost` yerine `127.0.0.1` (`REDIS_URL=redis://127.0.0.1:6379/0`) kullanın.

</details>

---

## API Referansı

`/api/v1/documents/*` ve `/api/v1/query*` altındaki tüm endpoint'ler `X-API-Key: <API_KEY>` (veya `Authorization: Bearer <API_KEY>`) header'ı gerektirir.

| Method & Path | Rate Limit | Açıklama |
| :--- | :---: | :--- |
| `GET /api/v1/health` | Public | Storage, Redis, Qdrant ve Circuit Breaker health probe'ları (`200` / `503`) |
| `POST /api/v1/documents/upload` | `5 / dk` | `tenant_id` ile multipart `.pdf` / `.txt` upload (`202 Accepted` + `task_id`, `document_id`) |
| `GET /api/v1/documents/status/{task_id}` | `120 / dk` | Celery task durumu (`PENDING`, `STARTED`, `RETRY`, `SUCCESS`, `FAILURE`) |
| `GET /api/v1/documents` | `120 / dk` | Aktif `tenant` içindeki indekslenmiş doküman ve chunk sayıları |
| `GET /api/v1/documents/tenants` | `120 / dk` | Qdrant facet'leri üzerinden mevcut `tenant` workspace'lerini listeleme |
| `DELETE /api/v1/documents/{document_id}` | `5 / dk` | İlgili dokümanın `tenant`-izole tüm vektörlerini silme |
| `POST /api/v1/query` | `20 / dk` | Citation destekli RAG sorgusu (`stream` parametresine göre JSON veya SSE) |
| `POST /api/v1/query/stream` | `20 / dk` | Chat history ve opsiyonel `document_ids` scoping destekli SSE streaming endpoint'i |

<details>
<summary><strong>Uçtan uca cURL örneği ve SSE frame formatı</strong></summary>

```bash
# 1. "acme-legal" workspace'ine PDF yükle
curl -s -X POST http://localhost:8000/api/v1/documents/upload \
  -H "X-API-Key: change-me-in-local-env" \
  -F "tenant_id=acme-legal" \
  -F "file=@sozlesme.pdf"

# 2. Background indexing durumunu sorgula
curl -s http://localhost:8000/api/v1/documents/status/<task_id> \
  -H "X-API-Key: change-me-in-local-env"

# 3. SSE üzerinden citation'lı yanıt stream et
curl -N -X POST http://localhost:8000/api/v1/query/stream \
  -H "X-API-Key: change-me-in-local-env" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Fesih bildirim süresi kaç gündür?",
    "tenant_id": "acme-legal",
    "history": []
  }'
```

`/api/v1/query/stream` tarafından gönderilen ham SSE frame çıktısı:

```text
event: token
data: {"event":"token","delta":"Fesih bildirim süresi 30 takvim günüdür [Source 1]."}

event: citations
data: {"event":"citations","query":"...","tenant_id":"acme-legal","citations":[{"index":1,"document_id":"...","filename":"sozlesme.pdf","page_number":4,"score":0.8421,"text":"..."}]}

event: done
data: [DONE]
```

</details>

---

## Quality Gates ve Proje Yapısı

CI pipeline ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) her push ve PR'da format, lint, strict `mypy` type check ve network'ten izole `136` testi otomatik doğrular:

```bash
# Format, lint, static type check, JS syntax ve izole testler (136 passed)
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests scripts locustfile.py
node --check src/ui/app.js
uv run pytest -q

# Live concurrency, RAG evaluation ve Locust load script'leri
uv run python scripts/test_queue_concurrency.py --count 10
uv run python scripts/evaluate_rag.py --delay-seconds 3.5
uv run python scripts/run_load_test.py --users 20 --spawn-rate 5 --duration 30s --with-mock-ai
```

<details>
<summary><strong>Proje dizin ağacı</strong></summary>

```text
AskDocs/
├── .github/workflows/ci.yml       # Ruff, Mypy & Pytest CI pipeline
├── docs/images/                   # Hero SVG, mimari SVG, eval chart ve UI ekran görüntüleri
├── evals/
│   ├── golden_dataset.json        # 44 soruluk 9-PDF + adversarial trap eval seti
│   ├── latest_eval_report.json    # Son calibration raporu (Recall@5, MRR, Faithfulness)
│   ├── load_test_report.json      # 20 kullanıcılı Locust latency & Docker RAM raporu
│   └── README.md                  # Hyperparameter kalibrasyon notları
├── scripts/
│   ├── evaluate_rag.py            # Golden Dataset runner + threshold sweep + Langfuse loglama
│   ├── run_load_test.py           # Locust runner + docker stats memory profiler
│   └── test_queue_concurrency.py  # Concurrent upload & retry test script'i
├── src/
│   ├── api/                       # API-Key auth, Redis rate limit, DI ve v1 endpoint'leri
│   ├── core/                      # Config, Circuit Breaker, logging, middleware ve Langfuse
│   ├── evals/                     # Metric calculator'lar (Recall@K, MRR, Faithfulness, Locust)
│   ├── schemas/                   # Pydantic v2 request/response ve SSE schema'ları
│   ├── services/                  # ABC interface'leri, storage, PyMuPDF parser, Qdrant ve LLM
│   ├── tasks/                     # Celery app ve idempotent indexing worker task'ı
│   ├── ui/                        # Cupertino Light Web UI (HTML, CSS, vanilla JS)
│   └── main.py                    # FastAPI lifespan, güvenlik middleware'leri ve UI mount
├── tests/                         # 136 izole unit & integration testi
├── Dockerfile                     # Multi-stage non-root runtime (UID/GID 10001)
├── docker-compose.yml             # API, Celery Worker, Redis, Qdrant, Postgres, Langfuse
├── locustfile.py                  # Upload, poll, query, stream, delete Locust load senaryosu
└── pyproject.toml                 # Dependencies, uv lock ve Ruff/Mypy/Pytest ayarları
```

</details>
