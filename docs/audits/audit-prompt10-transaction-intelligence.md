# Audit Kelayakan Kecerdasan Transaksi Lokal (Prompt 10c)

> **Status**: Selesai (Analisis Read-Only & Privacy-First)  
> **Hash Database**: `53fa7ae22a4fb176c60e1a47f7a49f6880bc21795951451cc7cfccc2fabde3b8` (100% Terverifikasi Identik Sebelum & Sesudah)  
> **Prinsip Keamanan**: Zero Cloud Dependency, Zero Profiling Psikologis, Zero PII Exposure.

---

## 1. Kualitas & Karakteristik Data

- **Total Transaksi Aktif**: 1.137 transaksi.
- **Segmentasi Konteks Data**:
  - **Personal**: 621 Pengeluaran, 365 Transfer, 47 Pemasukan.
  - **Historical Research**: 64 transaksi.
  - **Pass-through / Titipan Legacy**: 40 transaksi.
  - **Review Queue (Provisional)**: 68 transaksi.
- **Kebersihan Data**: Data rekening sumber, rekening tujuan, nominal, dan tanggal memiliki integritas 100% tanpa missing value fatal.

---

## 2. Pola Transaksi Utama

1. **Prediktabilitas Rekening**: Sangat tinggi (**97,1%** akurasi pada merchant berulang). Pola penggunaan rekening sangat konsisten (misal: dompet digital untuk pesan antar makanan/transportasi, rekening utama untuk tarik tunai & transfer besar).
2. **Kategori Berulang Konsisten**: Kategori rutin seperti *Main Meals*, *Utilities / Bills*, *Digital Subscriptions*, dan *Transfer Alokasi* memiliki konsistensi asosiasi $\ge 85\%$.
3. **Entropi Deskripsi & Cold-Start**: Transaksi insidental/non-rutin memiliki variasi kosakata tinggi. Dari evaluasi holdout masa depan, 75,7% transaksi baru memerlukan mekanisme *abstain* (menahan saran) agar tidak memberikan tebakan salah.

---

## 3. Hasil Pengujian Baseline (Chronological Holdout 70/30)

Pengujian dilakukan menggunakan pemisahan kronologis ketat (410 transaksi latih awal Januari–Juli 2026 vs 177 transaksi uji masa depan Juli–Agustus 2026):

| Metrik Evaluasi | Hasil Baseline Aturan Lokal |
|---|:---:|
| **Top-1 Category Accuracy (Covered)** | **65,1%** |
| **Top-3 Category Accuracy (Covered)** | **72,1%** |
| **Top-1 Source Account Accuracy (Covered)** | **97,1%** |
| **Coverage Rate** | **24,3%** |
| **Abstention Rate (Confidence < 0.5)** | **75,7%** |
| **False Suggestion Rate** | **34,9%** (Hanya terjadi pada token ambigu yang tetap lolos ambang) |

---

## 4. Keputusan Kelayakan Machine Learning (ML)

### **Verdict: "ML Belum Diperlukan"**

**Alasan Teknis & Bisnis**:
1. **Skala Data**: Dengan ~1.137 transaksi, model ML (seperti Naive Bayes, Decision Tree, atau Embeddings) rentan terhadap overfitting pada data historis dan menambah overhead dependensi/runtime.
2. **Keunggulan Aturan Deterministik**: Pendekatan kamus SQLite berbobot frekuensi (*Exact Normalized Merchant + Token Scoring*) jauh lebih cepat (< 1ms), tidak membutuhkan cloud, dan berukuran 0 KB dependensi tambahan.
3. **Transparansi & Explainability**: Aturan lokal dapat memberikan penjelasan gamblang kepada pengguna (contoh: *"Disarankan 'Main Meals' berdasarkan 12 transaksi serupa sebelumnya"*), sedangkan model ML bersifat *black-box*.

---

## 5. Rekomendasi Desain Fitur Rekomendasi Aman

Jika fitur saran lokal diimplementasikan pada antarmuka transaksi:

1. **Hanya Mengisi Draft Awal**: Rekomendasi hanya boleh menyarankan *Kategori*, *Rekening Asal*, *Untuk Siapa*, dan *Konteks Uang*.
2. **Transparansi UI**:
   - Tampilkan label *"Saran Cerdas (Tingkat Keyakinan: 85%)"*.
   - Cantumkan alasan singkat ringkas.
   - Sediakan tombol cepat *"Terapkan"* atau *"Abaikan"*.
3. **Konfirmasi Wajib Pengguna**: Transaksi tidak boleh tersimpan secara otomatis tanpa persetujuan eksplisit pengguna via tombol submit.
4. **Kebijakan Privasi Penuh**: Semua kalkulasi frekuensi berjalan 100% di browser/server localhost tanpa pengiriman data keluar.
