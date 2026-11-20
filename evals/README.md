# AskDocs — RAG Evaluation & Rust CPU Benchmark Raporu

## 1. Amacı ve Kapsamı

Bu dizin, AskDocs hibrit RAG pipeline'ının (`PyMuPDF -> Rust text_processor -> Yerel TEI GPU BAAI/bge-m3 -> Qdrant 1024d HNSW -> Context Prompt -> Cloud Chat LLM`) hız ve bellek verimliliğinin yanı sıra **retrieval isabetini**, **Faithfulness skorunu** ve **hallüsinasyon reddini (Abstention)** sayısal olarak ölçmek için oluşturulmuştur.

---

## 2. Golden Dataset ([golden_dataset.json](golden_dataset.json) — v4.0 · `duran`)

Toplam **30 test sorusu** (`24` pozitif + `6` unanswerable / adversarial kontrol), **`duran`** çalışma alanında yüklü **10 farklı bilgisayar mühendisliği ders PDF'i (`753` indekslenmiş sayfa · `1,357` chunk · `1.14 MB` metin)** üzerine kuruludur. Aynı korpusta iki farklı Bilgisayar Ağları PDF'i (`Bilgisayar-Ağ-Sistemleri.pdf` ve `network_dersnotu.pdf`), iki farklı Veritabanı PDF'i (`Lecture01_309-database-management-turkce.pdf` ve `Veri-Tabani-Yonetim-Sistemleri.pdf`) ve iki farklı Python PDF'i (`hafta_01.pdf` ve `python_3_unite.pdf`) yan yana indekslenerek yüksek konu örtüşmesi altında çok dilli (TR/EN) retrieval ve sıralama hassasiyeti ölçülmüştür:

| Kategori | Kaynak Doküman | Sayfa / Parça | Soru Sayısı | İçerik Türü |
| --- | --- | :---: | :---: | --- |
| `ai_literacy` | `8cdfd9c6-b417-4937-83c8-d718fb29a398.pdf` | `32` sayfa / `37` chunk | 2 | Yapay Zekâ Okuryazarlığı (Erzurum Teknik Üniv.) |
| `computer_networks_book` | `Bilgisayar-Ağ-Sistemleri.pdf` | `65` sayfa / `118` chunk | 3 | ARPANET, MAC/LLC alt katmanları, ağ güvenliği |
| `vtys_bby309` | `Lecture01_309-database-management-turkce.pdf` | `75` sayfa / `75` chunk | 2 | BBY 309 VTYS slaytları (Hacettepe Üniv.) |
| `operating_systems` | `OS_L2IsletimSistemiYapilari.pdf` | `32` sayfa / `34` chunk | 2 | İşletim Sistemi Yapıları (Gazi Üniv.) |
| `vtys_notes` | `Veri-Tabani-Yonetim-Sistemleri.pdf` | `18` sayfa / `35` chunk | 2 | İlişkisel veritabanı modelleri ve SQL DML |
| `python_intro` | `hafta_01.pdf` | `35` sayfa / `44` chunk | 2 | Python temelleri, değişken adresleri (`id()`), listeler |
| `java_oop` | `java_notes_by_oa.pdf` | `203` sayfa / `343` chunk | 3 | Java ile Nesneye Yönelik Programlama (`static`, sınıf adları) |
| `network_notes` | `network_dersnotu.pdf` | `108` sayfa / `332` chunk | 3 | Ethernet çerçevesi, hiyerarşik topoloji, X.25 |
| `python_oop` | `python_3_unite.pdf` | `92` sayfa / `92` chunk | 2 | Python 3. Ünite Nesne Yönelimli Proje Geliştirme |
| `data_structures` | `veri-yapilari-hakan-kutucu.pdf` | `93` sayfa / `247` chunk | 3 | Veri Yapıları (Bağlı listeler, dairesel kuyruk, ağaçlar) |
| `negative_control` / `adversarial_negative` | — (Belgede yok) | — | 6 | 2 kapsam dışı kontrol (`q25`–`q26`) + 4 doküman terimlerini kullanan near-miss adversarial tuzak (`q27`–`q30`) |

---

## 3. Ölçülen Metrikler

- **`Recall@K` (`K=5`):** Cevabı içeren beklenen doküman ve sayfanın, Qdrant tarafından getirilen ilk `K` sonuç içinde bulunma oranı (`24` pozitif soru üzerinden).
- **`MRR` (Mean Reciprocal Rank):** Doğru kaynağın getirildiği ilk sıranın çarpmaya göre tersinin (`1 / rank`) ortalaması. Örtüşen 10 ders kitabı arasında sıralama hassasiyetini ölçer.
- **`Faithfulness`:** Üretilen yanıttaki `[Source N]` citation geçerliliği, out-of-range veya uydurma citation olmaması, referans kavram kapsama oranı ve unanswerable sorularda halüsinasyonsuz `abstention` başarısının birleşik skoru.
- **`Keyword Coverage`:** Referans cevaptaki kritik sayısal/teknik kavramların üretilen yanıtta kapsanma oranı (Türkçe `İ` ve PDF `ﬁ` ligatür normalizasyonlu).
- **`Abstention Accuracy`:** Belgede karşılığı olmayan hem genel hem adversarial near-miss sorularda modelin bilgi uydurmadan ret verebilme oranı.
- **`AvgChunks` & `NegNoise`:** LLM context'ine giren ortalama chunk sayısı ile negatif sorularda eşiği geçerek gürültü oluşturan sorgu oranı.

---

## 4. 10-PDF `duran` Eval Sonuçları (`run_id=eval-20261120-203718`)

Tam rapor: [latest_eval_report.json](latest_eval_report.json)

- **Workspace & Corpus:** `duran` (`10` PDF · `753` indekslenmiş sayfa · `1,357` chunk)
- **LLM / Embedding:** `gemini-3.5-flash-lite` / `BAAI/bge-m3` (`1024`-dim Yerel TEI GPU, `top_k=5`, `score_threshold=0.60`)
- **Evaluation Modu:** `citation_grounded` (`30` soru — Türkçe ve İngilizce cross-lingual)

| Metrik | Ölçülen Sonuç | Yorum |
| :--- | :---: | :--- |
| **Mean Recall@5** | **`1.0000`** (`24 / 24`) | 10 farklı ders dokümanının tamamında beklenen sayfa Top-5 içinde bulundu |
| **Mean MRR** | **`0.9097`** | Örtüşen ağ/VTYS/Python belgelerine rağmen çok yüksek ilk-sıra isabeti |
| **Mean Faithfulness** | **`0.9867`** | Yanıtlardaki tüm `[Source N]` atıfları gerçek sayfalara dayandı; sıfır uydurma referans |
| **Keyword Coverage** | **`0.9722`** | Türkçe ve İngilizce sorularda kritik teknik kavram ve sayıların %97.2'si kapsandı |
| **Abstention Accuracy** | **`1.0000`** (`6 / 6`) | Genel ve adversarial tüm tuzaklarda halüsinasyonsuz temiz ret (`0` atıf) |

### Threshold (`score_threshold`) Sweep'i (`0.30–0.70`)

| `score_threshold` | `Recall@5` | `MRR` | Ortalama Parça | Tuzak Gürültüsü (`NegNoise`) |
| :---: | :---: | :---: | :---: | :---: |
| `0.30` | `1.0000` | `0.9097` | `5.00` | `1.0000` |
| `0.40` | `1.0000` | `0.9097` | `5.00` | `0.8333` |
| `0.48` (Fallback) | `1.0000` | `0.9097` | `5.00` | `0.6667` |
| `0.55` | `1.0000` | `0.9097` | `4.62` (`-%7.6`) | `0.1667` |
| **`0.60` (Önerilen / Canlı)** | **`1.0000`** | **`0.9097`** | **`3.29` (-%34.2)** | **`0.0000` (-%100.0)** |
| `0.65` | `0.7917` | `0.7708` | `1.58` | `0.0000` |
| `0.70` | `0.6250` | `0.6042` | `0.92` | `0.0000` |

* **Bulgu:** `BAAI/bge-m3` kosinüs benzerlik dağılımında `0.60` eşiği tam Pareto optimumu üzerinde yer alır: `Recall@5` (`1.0000`) ve `MRR` (`0.9097`) skorlarını hiç kaybetmeden **6/6 tuzak sorunun tamamında gürültüyü sıfıra (`0.0000`) indirir** ve LLM bağlam token yükünü **`%34.2` azaltır** (`5.00 -> 3.29`). Eşiği `0.65`'e çekmek ise `Recall@5`'i `0.7917`'ye düşürmektedir.

---

## 5. Yerel TEI GPU (`BAAI/bge-m3`), PyMuPDF & Rust (`text_processor`) Ölçümleri

- **Yerel TEI GPU Embedding (`BAAI/bge-m3`, `float16`, RTX 3050 Ti):**
  - 30 altın soru tekil vektörleme gecikmesi: **`p50 = 17.90 ms`** (`mean = 18.25 ms`, `p95 = 21.03 ms`, `min = 15.91 ms`)
  - Tüm 10 PDF'lik `duran` korpusu (`1,357` parça, `batch=32`): **`13.73 sn`** (`98.8 parça/sn`, `782.9 MiB` TEI konteyner RAM'i)
- **PyMuPDF PDF Stream (`iter_document_pages`):** `duran` metinlerinden üretilen 100 sayfalık PDF üzerinde 5 koşum medyanı: **`37.04 ms` (`2,700.0 sayfa/sn`)**
- **Parça Boyutu Verimliliği (`duran` 10-PDF korpusu, `1,102,517` karakter / `1.14 MB`):** `1200/150` yapılandırması **`1,027` parça** üretirken `500/75` ayarı **`2,658` parça** (`2.59x` daha fazla embedding çağrısı) üretmektedir.
- **Rust (`text_processor` v0.1.0) vs. Python (`50.00 MiB` CPU & Bellek Benchmark'ı):** Tam rapor: [text_processor_benchmark.json](text_processor_benchmark.json) (`49,585,446` karakter, `chunk=1200/150`, `3` tekrar medyanı):

| Aşama | Python Medyan | Rust Medyan | Hızlanma | Python Heap Peak | Rust Py-Heap Peak | Parity |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **`clean_text`** | `4,399.84 ms` (`11.36 MiB/s`) | **`255.32 ms` (`195.83 MiB/s`)** | **`17.23x`** | `749.51 MiB` | `301.59 MiB` | `True` |
| **`chunk_text`** (`69,349` parça) | `4,337.19 ms` (`11.53 MiB/s`) | **`420.63 ms` (`118.87 MiB/s`)** | **`10.31x`** | `749.51 MiB` | **`202.12 MiB` (`3.71x` düşük)** | `True` |

---

## 6. Yeniden Çalıştırma

```bash
# 10-PDF duran eval + threshold sweep
uv run python scripts/evaluate_rag.py --dataset evals/golden_dataset.json --output evals/latest_eval_report.json --score-threshold 0.60 --thresholds "0.30,0.40,0.48,0.55,0.60,0.65,0.70" --delay-seconds 4.5

# 50 MiB Rust vs. Python CPU & heap benchmark
uv run --no-sync python scripts/benchmark_text_processor.py --size-mb 50 --iterations 3 --output evals/text_processor_benchmark.json

# 20 kullanıcılı Locust yük & Docker RAM testi
uv run python scripts/run_load_test.py --users 20 --spawn-rate 5 --duration 30s --with-mock-ai --output evals/load_test_report.json
```
