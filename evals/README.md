# AskDocs — RAG Evaluation ve Calibration Raporu

## 1. Amacı ve Kapsamı

Bu dizin, AskDocs RAG pipeline'ının (`Embedding -> Qdrant Search -> Context Prompt -> LLM Generation`) hız ve maliyetinin yanı sıra **retrieval isabetini** ve **Faithfulness skorunu** sayısal olarak ölçmek ve hyperparameter'ları (`RAG_SCORE_THRESHOLD`, `CHUNK_SIZE`, `CHUNK_OVERLAP`) tahmine değil gerçek veriye göre belirlemek için oluşturulmuştur.

---

## 2. Golden Dataset ([golden_dataset.json](golden_dataset.json) — v2.0)

Toplam **44 test sorusu** (`38` pozitif + `6` unanswerable / adversarial kontrol), **9 gerçek PDF dokümanı (`535` chunk)** üzerine kuruludur. Aynı terminolojiyi paylaşan 7 adet RAG/Retrieval makalesi (`DPR`, `REALM`, `RAG`, `FiD`, `HyDE`, `Self-RAG`, `CRAG`) tek Qdrant `tenant` koleksiyonuna yüklenerek gerçek dokümanlar arası semantic rekabet ve cross-lingual (TR/EN) retrieval stress testi uygulanmıştır:

| Kategori | Kaynak Doküman | Soru Sayısı | İçerik Türü |
| --- | --- | :---: | --- |
| `hr_policy` / `hr_security` | `sirket_el_kitabi_tr.pdf` | 8 | Türkçe kurumsal politika (`11` chunk) |
| `bitcoin` | `bitcoin_whitepaper.pdf` | 6 | Teknik whitepaper + TR/EN cross-lingual (`63` chunk) |
| `rag_paper` | `rag_paper_2005_11401.pdf` | 6 | Lewis et al. (2020) RAG makalesi (`76` chunk) |
| `realm_paper` | `realm_2002_08909.pdf` | 3 | Guu et al. (2020) REALM makalesi (`56` chunk) |
| `dpr_paper` | `dpr_2004_04906.pdf` | 3 | Karpukhin et al. (2020) DPR makalesi (`64` chunk) |
| `fid_paper` | `fid_2007_01282.pdf` | 3 | Izacard &amp; Grave (2020) FiD makalesi (`25` chunk) |
| `hyde_paper` | `hyde_2212_10496.pdf` | 3 | Gao et al. (2022) HyDE makalesi (`48` chunk) |
| `selfrag_paper` | `selfrag_2310_11511.pdf` | 3 | Asai et al. (2023) Self-RAG makalesi (`122` chunk) |
| `crag_paper` | `crag_2401_15884.pdf` | 3 | Yan et al. (2024) CRAG makalesi (`70` chunk) |
| `negative_control` / `adversarial_negative` | — (Belgede yok) | 6 | 2 genel negatif kontrol (`q21`–`q22`) + 4 makale terimlerini kullanan near-miss adversarial soru (`q41`–`q44`) |

---

## 3. Ölçülen Metrikler

- **`Recall@K` (`K=5`):** Cevabı içeren beklenen doküman ve sayfanın, Qdrant tarafından getirilen ilk `K` sonuç içinde bulunma oranı (`38` pozitif soru üzerinden).
- **`MRR` (Mean Reciprocal Rank):** Doğru kaynağın getirdiği ilk rank değerinin çarpmaya göre tersinin (`1 / rank`) ortalaması. 7 farklı RAG makalesinin aynı koleksiyonda yarıştığı ortamda semantic ranking kalitesini hassas biçimde gösterir.
- **`Faithfulness`:** Üretilen yanıttaki `[Source N]` citation geçerliliği, out-of-range veya uydurma citation olmaması, reference kavram coverage'ı ve unanswerable sorularda halüsinasyonsuz `abstention` başarısının birleşik skoru (`--llm-judge` opsiyonu da desteklenir).
- **`Keyword Coverage`:** Referans cevaptaki kritik sayısal/teknik kavramların üretilen yanıtta kapsanma oranı (Türkçe `İ` ve PDF `ﬁ` ligatür normalizasyonlu).
- **`Abstention Accuracy`:** Belgede karşılığı olmayan hem genel hem adversarial near-miss sorularda modelin bilgi uydurmadan ret verebilme oranı.
- **`AvgChunks`:** Threshold'u geçip LLM context'ine eklenen ortalama chunk sayısı (token maliyet göstergesi).
- **`NegNoise`:** Unanswerable sorularda threshold'u geçmeyi başararak LLM'e gereksiz chunk gönderen negatif soru oranı.

---

## 4. Deney Sonuçları (`run_id=eval-20261117-183520`)

Tam rapor: [latest_eval_report.json](latest_eval_report.json)

- **LLM / Embedding:** `gemini-3.5-flash-lite` / `gemini-embedding-001` (`top_k=5`, `score_threshold=0.70`)
- **Evaluation Modu:** `citation_grounded` (44 soru + eval trace/skorları Langfuse'a loglanmıştır)

| Metrik | v1 (3 PDF · 22 Soru) | v2 (9 Overlapping PDF · 44 Soru) |
| --- | :---: | :---: |
| **Mean Recall@5** | `1.0000` (`20/20`) | **`1.0000`** (`38 / 38`) |
| **Mean MRR** | `1.0000` | **`0.8969`** (7 RAG makalesi rekabeti altında) |
| **Mean Faithfulness** | `0.9659` | **`0.9591`** |
| **Keyword Coverage** | `0.9375` | **`0.9386`** |
| **Abstention Accuracy** | `1.0000` (`2/2`) | **`1.0000`** (`6 / 6` — 4 adversarial dahil) |

### Genişletilmiş Threshold (`score_threshold`) Sweep'i

Aynı `44` sorgu vektörüyle Qdrant üzerindeki `535` chunk içinde `0.30`..`0.75` arası threshold değerleri kıyaslanmıştır:

| `score_threshold` | `Recall@5` | `MRR` | `AvgChunks` | `NegNoise` |
| :---: | :---: | :---: | :---: | :---: |
| `0.30` | `1.0000` | `0.8969` | `5.00` | `1.0000` |
| `0.40` | `1.0000` | `0.8969` | `5.00` | `1.0000` |
| `0.50` | `1.0000` | `0.8969` | `5.00` | `1.0000` |
| `0.60` | `1.0000` | `0.8969` | `4.97` | `1.0000` |
| `0.65` | `1.0000` | `0.8969` | `4.61` | `1.0000` |
| **`0.70` (Seçilen)** | **`1.0000`** | **`0.8969`** | **`4.11`** | **`0.6667`** |
| `0.75` | `0.9211` | `0.8180` | `3.11` | `0.1667` |

---

## 5. Tasarım ve Kalibrasyon Kararları

### 1) `RAG_SCORE_THRESHOLD = 0.70` (Pareto Kırılma Noktası)
- **Bulgu:** `0.65` ve altında tüm negatif sorular (`NegNoise = 1.0000`) LLM'e gereksiz chunk gönderdi. `0.70`'te genel negatif sorular (`q21`, `q22`) sıfır chunk ile elenirken alan terimlerini paylaşan near-miss adversarial sorular (`q41`–`q44`, skor `0.7221`–`0.7531`) system prompt sayesinde **`%100` (`6/6`) halüsinasyonsuz** reddedildi; ayrıca gönderilen chunk sayısı `5.00 -> 4.11`'e (**~%18 token tasarrufu**) düştü.
- **Neden `0.75` değil?** `0.75` eşiği `NegNoise`'u `0.1667`'ye indirse de pozitif sorularda `Recall@5`'i `1.0000 -> 0.9211`'e, `MRR`'ı `0.8969 -> 0.8180`'e düşürerek gerçek kaynakları elemeye başladı. Bu yüzden `0.70`, recall kaybı başlamadan önceki optimum kırılma noktası olarak seçildi.

### 2) `CHUNK_SIZE = 1200` ve `CHUNK_OVERLAP = 150`
- **Gerekçe:** `500 / 75` karakterlik küçük chunking, çok maddeli politika metinlerinde başlığı koşul/tutardan koparabiliyor ve akademik makalelerde paragraf context'ini daraltıyordu. `1200 / 150` yapısı hem context bütünlüğünü korudu hem de aynı metinde üretilen chunk (ve embedding API çağrısı) sayısını **3 kat** azalttı (`5.33 MiB` CPU benchmark'ında `18,500 -> 6,167` chunk).

---

## 6. Yeniden Çalıştırma ve Kalibrasyon

```bash
uv run python scripts/evaluate_rag.py --score-threshold 0.70 --thresholds "0.30,0.40,0.50,0.60,0.65,0.70,0.75" --delay-seconds 3.5
```

Opsiyonel LLM-as-a-judge modu ile çalıştırmak için `--llm-judge` flag'i eklenebilir.
