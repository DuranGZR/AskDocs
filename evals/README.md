# AskDocs — RAG Evaluation & Rust CPU Benchmark Raporu

## 1. Amacı ve Kapsamı

Bu dizin, AskDocs RAG pipeline'ının (`Rust text_processor -> Embedding -> Qdrant Search -> Context Prompt -> LLM Generation`) hız ve bellek verimliliğinin yanı sıra **retrieval isabetini** ve **Faithfulness skorunu** sayısal olarak ölçmek için oluşturulmuştur.

---

## 2. Golden Dataset ([golden_dataset.json](golden_dataset.json) — v3.0 · `rag-research`)

Toplam **24 test sorusu** (`18` pozitif + `6` unanswerable / adversarial kontrol), **`rag-research`** workspace'inde bulunan **6 rakip RAG/Retrieval makalesi (`385` chunk)** üzerine kuruludur. Tamamı aynı akademik alandan gelen 6 makale tek izole `tenant` alanına indekslenerek yüksek terminoloji örtüşmesi altında semantic ranking ve cross-lingual (TR/EN) retrieval stress testi uygulanmıştır:

| Kategori | Kaynak Doküman | Soru Sayısı | İçerik Türü |
| --- | --- | :---: | --- |
| `realm_paper` | `realm_2002_08909.pdf` | 3 | Guu et al. (2020) REALM makalesi (`56` chunk) |
| `dpr_paper` | `dpr_2004_04906.pdf` | 3 | Karpukhin et al. (2020) DPR makalesi (`64` chunk) |
| `fid_paper` | `fid_2007_01282.pdf` | 3 | Izacard &amp; Grave (2020) FiD makalesi (`25` chunk) |
| `hyde_paper` | `hyde_2212_10496.pdf` | 3 | Gao et al. (2022) HyDE makalesi (`48` chunk) |
| `selfrag_paper` | `selfrag_2310_11511.pdf` | 3 | Asai et al. (2023) Self-RAG makalesi (`122` chunk) |
| `crag_paper` | `crag_2401_15884.pdf` | 3 | Yan et al. (2024) CRAG makalesi (`70` chunk) |
| `negative_control` / `adversarial_negative` | — (Belgede yok) | 6 | 2 genel negatif kontrol (`q19`–`q20`) + 4 makale terimlerini kullanan near-miss adversarial tuzak (`q21`–`q24`) |

---

## 3. Ölçülen Metrikler

- **`Recall@K` (`K=5`):** Cevabı içeren beklenen doküman ve sayfanın, Qdrant tarafından getirilen ilk `K` sonuç içinde bulunma oranı (`18` pozitif soru üzerinden).
- **`MRR` (Mean Reciprocal Rank):** Doğru kaynağın getirildiği ilk sıranın çarpmaya göre tersinin (`1 / rank`) ortalaması. 6 farklı retrieval makalesinin aynı tenant içinde yarıştığı ortamda sıralama hassasiyetini ölçer.
- **`Faithfulness`:** Üretilen yanıttaki `[Source N]` citation geçerliliği, out-of-range veya uydurma citation olmaması, referans kavram kapsama oranı ve unanswerable sorularda halüsinasyonsuz `abstention` başarısının birleşik skoru.
- **`Keyword Coverage`:** Referans cevaptaki kritik sayısal/teknik kavramların üretilen yanıtta kapsanma oranı (Türkçe `İ` ve PDF `ﬁ` ligatür normalizasyonlu).
- **`Abstention Accuracy`:** Belgede karşılığı olmayan hem genel hem adversarial near-miss sorularda modelin bilgi uydurmadan ret verebilme oranı.
- **`AvgChunks` & `NegNoise`:** LLM context'ine giren ortalama chunk sayısı ile negatif sorularda eşiği geçerek gürültü oluşturan sorgu oranı.

---

## 4. 6-PDF `rag-research` Eval Sonuçları (`run_id=eval-20261118-193027`)

Tam rapor: [latest_eval_report.json](latest_eval_report.json)

- **Workspace & Corpus:** `rag-research` (`6` PDF · `385` chunk)
- **LLM / Embedding:** `gemini-3.5-flash-lite` / `gemini-embedding-001` (`top_k=5`, `score_threshold=0.70`)
- **Evaluation Modu:** `citation_grounded` (`24` soru — TR/EN cross-lingual)

| Metrik | Ölçülen Sonuç | Yorum |
| :--- | :---: | :--- |
| **Mean Recall@5** | **`1.0000`** (`18 / 18`) | 6 rakip makalenin tamamında beklenen sayfa Top-5 içinde bulundu |
| **Mean MRR** | **`0.7824`** | Yoğun akademik terim örtüşmesi altında yüksek ilk-sıra isabeti |
| **Mean Faithfulness** | **`0.9729`** | Geçerli `[Source N]` atıfları; sıfır uydurma referans |
| **Keyword Coverage** | **`0.9398`** | Teknik metrik ve sayısal değerlerin doğru aktarımı |
| **Abstention Accuracy** | **`1.0000`** (`6 / 6`) | Genel ve adversarial tüm tuzaklarda halüsinasyonsuz ret |

### Threshold (`score_threshold`) Sweep'i (`0.30–0.75`)

| `score_threshold` | `Recall@5` | `MRR` | `AvgChunks` | `NegNoise` |
| :---: | :---: | :---: | :---: | :---: |
| `0.30` | `1.0000` | `0.7824` | `5.00` | `1.0000` |
| `0.40` | `1.0000` | `0.7824` | `5.00` | `1.0000` |
| `0.50` | `1.0000` | `0.7824` | `5.00` | `1.0000` |
| `0.60` | `1.0000` | `0.7824` | `5.00` | `0.8333` |
| `0.65` | `1.0000` | `0.7824` | `5.00` | `0.6667` |
| `0.70` (Varsayılan) | `1.0000` | `0.7824` | `5.00` | `0.6667` |
| **`0.75` (Önerilen)** | **`1.0000`** | **`0.7824`** | **`4.44` (-%11.2)** | **`0.1667` (-%83.3)** |

* **Bulgu:** 6 akademik makalenin yoğun rekabetinde `0.70` varsayılan eşiği `Recall@5=1.0000` korurken `NegNoise`'u `%33` azaltıyor; `0.75` eşiğine çıkıldığında ise `Recall@5` ve `MRR` hiç düşmeden `AvgChunks` `4.44`'e (`%11.2` token tasarrufu) ve `NegNoise` `0.1667`'ye (`%83.3` gürültü azalması) iniyor.

---

## 5. Rust (`text_processor`) vs. Python CPU & Bellek Benchmark'ı

Tam rapor: [text_processor_benchmark.json](text_processor_benchmark.json) · Script: [`../scripts/benchmark_text_processor.py`](../scripts/benchmark_text_processor.py)

`50.00 MiB` (`49,585,446` karakter) HTML/script/tire-bölünmeli Türkçe/İngilizce korpus üzerinde `chunk_size=1200`, `chunk_overlap=150` ve `opt-level=3` release derlemesi ile (`3` tekrarlı medyan):

| Aşama | Python Medyan | Rust Medyan | Hızlanma | Python Heap Peak | Rust Py-Heap Peak | Parity |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **`clean_text`** | `4,890.17 ms` (`10.22 MiB/s`) | **`294.06 ms` (`170.03 MiB/s`)** | **`16.63x`** | `749.51 MiB` | `301.59 MiB` | `True` |
| **`chunk_text`** (`69,349` parça) | `4,934.82 ms` (`10.13 MiB/s`) | **`507.37 ms` (`98.55 MiB/s`)** | **`9.73x`** | `749.51 MiB` | **`202.12 MiB` (`3.7x` düşük)** | `True` |

---

## 6. Yeniden Çalıştırma

```bash
# 6-PDF rag-research eval + threshold sweep
uv run python scripts/evaluate_rag.py --score-threshold 0.70 --thresholds "0.30,0.40,0.50,0.60,0.65,0.70,0.75" --delay-seconds 5.0

# 50 MiB Rust vs. Python CPU & heap benchmark
uv run python scripts/benchmark_text_processor.py --size-mb 50 --iterations 3
```
