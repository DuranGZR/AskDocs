# CoreRAG-Engine — RAG Kalite Değerlendirmesi ve Mimari Karar Raporu

## 1. Amacı ve Kapsamı

Bu dizin, CoreRAG-Engine RAG hattının (Embedding -> Qdrant Arama -> Prompt Oluşturma -> LLM Üretimi) hız ve maliyetinin yanı sıra **arama isabetini** ve **yanıt sadakatini** sayısal olarak ölçmek ve hiperparametreleri (`RAG_SCORE_THRESHOLD`, `CHUNK_SIZE`, `CHUNK_OVERLAP`) tahmine değil veriye dayalı belirlemek için oluşturulmuştur.

---

## 2. Altın Veri Seti ([golden_dataset.json](golden_dataset.json))

Toplam **22 test sorusu**, 3 farklı doküman türü ve 2 halüsinasyon kontrol senaryosu üzerine kuruludur:

| Kategori | Kaynak Doküman | Soru Sayısı | İçerik Türü |
| --- | --- | --- | --- |
| `hr_policy` / `hr_security` | `sirket_el_kitabi_tr.pdf` | 8 | Türkçe kurumsal politika (izin, mesai, ödenek, veri saklama) |
| `bitcoin` | `bitcoin_whitepaper.pdf` | 6 | İngilizce teknik whitepaper (TR + EN çapraz dil soruları dahil) |
| `rag_paper` | `rag_paper_2005_11401.pdf` | 6 | Yoğun akademik NLP makalesi (mimari, indeks, decoding detayları) |
| `negative_control` | — (Belgede yok) | 2 | Cevapsız tuzak sorular (`q21`: yemek kartı, `q22`: eğitim bütçesi) |

---

## 3. Ölçülen Metrikler

- **`Recall@K` (`K=5`):** Cevabı içeren doğru doküman ve sayfanın, Qdrant tarafından getirilen ilk `K` sonuç içinde bulunma oranı (yalnızca cevabı olan 20 soru üzerinden ölçülür).
- **`MRR` (Mean Reciprocal Rank):** Doğru kaynağın getirdiği sıranın (`rank`) çarpmaya göre tersinin (`1 / rank`) ortalaması. `1.0000`, doğru kaynağın her soruda **1. sırada** geldiğini gösterir.
- **`Faithfulness`:** Üretilen yanıtın `[Source N]` atıf geçerliliği, hayali/aralık dışı atıf olmaması, referans kavram kapsamı ve cevapsız sorularda halüsinasyonsuz reddetme başarısının birleşik puanı (isteğe bağlı `--llm-judge` moduyla LLM hakem puanı da alınabilir).
- **`Keyword Coverage`:** Referans cevaptaki kritik sayısal/teknik kavramların üretilen yanıtta kapsanma oranı (Türkçe `İ` birleşim noktası ve PDF `ﬁ` ligatür normalizasyonlu).
- **`Abstention Accuracy`:** Belgede karşılığı olmayan negatif sorularda modelin bilgi uydurmadan *"sağlanan belgelerde bilgi bulunmamaktadır"* diyebilme oranı.
- **`AvgChunks`:** Barajı geçip LLM bağlamına eklenen ortalama parça sayısı (prompt token maliyetini doğrudan etkiler).
- **`NegNoise`:** Cevapsız negatif sorularda barajı geçmeyi başararak modele gereksiz gürültü bağlam gönderen soru oranı (`0.0000` hedeflenir).

---

## 4. Deney Sonuçları (`run_id=eval-20261115-195101`)

Tam rapor: [latest_eval_report.json](latest_eval_report.json)

- **LLM / Embedding:** `gemini-3.5-flash-lite` / `gemini-embedding-001` (`top_k=5`)
- **Değerlendirme Modu:** `citation_grounded` (`evals` skorları Langfuse'a da işlenmiştir)

| Metrik | Ölçülen Değer |
| --- | --- |
| **Mean Recall@5** | **`1.0000`** (20 / 20) |
| **Mean MRR** | **`1.0000`** (her soruda 1. sıra) |
| **Mean Faithfulness** | **`0.9659`** |
| **Keyword Coverage** | **`0.9375`** |
| **Abstention Accuracy** | **`1.0000`** (2 / 2 doğru ret) |

### Eşik Değeri (`score_threshold`) Taraması

Aynı 22 sorgu vektörüyle Qdrant üzerinde farklı `score_threshold` adayları kıyaslanmıştır:

| `score_threshold` | `Recall@5` | `MRR` | `AvgChunks` | `NegNoise` (Gürültü) |
| --- | --- | --- | --- | --- |
| `0.30` | `1.0000` | `1.0000` | `5.00` | `1.0000` |
| `0.40` | `1.0000` | `1.0000` | `5.00` | `1.0000` |
| `0.45` | `1.0000` | `1.0000` | `5.00` | `1.0000` |
| `0.50` | `1.0000` | `1.0000` | `5.00` | `1.0000` |
| `0.60` | `1.0000` | `1.0000` | `4.95` | `1.0000` |
| **`0.70` (Seçilen)** | **`1.0000`** | **`1.0000`** | **`3.30`** | **`0.0000`** |

---

## 5. Mühendislik Kararları (Neyi Neden Seçtik?)

### 1) `RAG_SCORE_THRESHOLD`: `0.50` -> `0.70`
- **Problem:** Eski `0.50` varsayılanında cevabı belgede olmayan tuzak sorular bile benzerlik puanı olarak `0.6532` (`q22`) ve `0.6921` (`q21`) aldığı için barajı geçiyor (`NegNoise = 1.0000`) ve LLM'e her seferinde 5 alakasız parça gönderilerek girdi token maliyeti şişiyordu.
- **Karar:** Pozitif soruların en düşük ilk sıra skoru `0.7261` (`q10`) olduğundan, **`0.70`** barajı pozitif sorularda `%100` isabeti (`Recall@5 = 1.0000`, `MRR = 1.0000`) bozmadan tuzak sorulardaki gürültüyü `%100`'den `%0`'a (`NegNoise = 0.0000`) düşürdü ve gönderilen ortalama parça sayısını `5.00`'dan `3.30`'a çekerek **~%34 bağlam token tasarrufu** sağladı.

### 2) `CHUNK_SIZE = 1200` ve `CHUNK_OVERLAP = 150`
- **Gerekçe:** Başlangıçtaki `500 / 75` karakterlik parçalama, çok maddeli İK politikalarında madde başlığı ile koşul/tutarı bölebiliyor ve akademik makalelerde paragraf bağlamını daraltıyordu. `1200 / 150` yapısı hem semantik bağlam bütünlüğünü korudu hem de PDF indekslenmesi sırasında oluşan toplam parça (ve harici embedding API çağrısı) sayısını yarıdan fazla azalttı.

---

## 6. Yeniden Çalıştırma ve Kalibrasyon

Farklı bir embedding modeline (ör. yerel Ollama `nomic-embed-text` veya `bge-m3`) geçildiğinde benzerlik skoru dağılımı değişeceğinden, eşik taramasını yeniden koşup yeni modeli kalibre etmek için:

```bash
uv run python scripts/evaluate_rag.py --delay-seconds 4.2
```

Opsiyonel LLM-as-a-judge modu ile çalıştırmak için `--llm-judge` bayrağı eklenebilir.
