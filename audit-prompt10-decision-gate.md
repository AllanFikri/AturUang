# Verifikasi Audit, Profil Rekening, dan Product Decision Gate (Prompt 10d)

> **Status**: Selesai (Analisis Read-Only & Privacy-First)  
> **Hash Database Baseline**: `53fa7ae22a4fb176c60e1a47f7a49f6880bc21795951451cc7cfccc2fabde3b8` (Identik 100%)  
> **Prinsip**: Zero Cloud Dependency, Zero ML Overhead, Privacy-First Local Recommendation.

---

## 1. Rekonsiliasi Cohort Data (Saling Lepas / Mutually Exclusive)

| Cohort Transaksi | Jumlah | Keterangan |
|---|:---:|---|
| **Personal Expense** | 621 | Belanja dan pengeluaran pribadi confirmed |
| **Personal Transfer** | 295 | Transfer antar-rekening pribadi |
| **Third-party** | 71 | Titipan, Piutang, dan Utang (K1/K2) |
| **Historical Research** | 64 | Data historis komparatif (terisolasi) |
| **Personal Income** | 46 | Pemasukan pribadi confirmed |
| **Pass-through Legacy** | 40 | Data titipan lama sebelum skema K1 |
| **Deleted / Reversed** | 0 | Tidak ada transaksi terarsip aktif |
| **TOTAL KESELURUHAN** | **1.137** | **100% Terekonsiliasi Sempurna** |
| *Subset Review Queue (Provisional)* | *0* | *Seluruh transaksi telah berstatus definitif* |

---

## 2. Verifikasi Metrik Baseline & Kalibrasi Saran (Chronological Holdout 70/30)

Evaluasi dilakukan secara independen terhadap 290 transaksi uji masa depan (12 Juni – 23 Agustus 2026) dengan data latih 675 transaksi masa lalu (1 Januari – 11 Juni 2026):

| Target Prediksi | Coverage | Precision (Covered) | False Suggestions | Abstain Rate | Majority Baseline | Improvement |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| **Category** | 20,0% | **98,3%** | 1 | 80,0% | 24,8% | **+73,5%** |
| **Account From** | 35,2% | **98,0%** | 2 | 64,8% | 61,0% | **+37,0%** |
| **Account To** | 26,6% | **97,4%** | 2 | 73,4% | 16,2% | **+81,2%** |
| **For / With Whom** | 36,9% | **100,0%** | 0 | 63,1% | 97,6% | **+2,4%** |
| **Money Context** | 36,9% | **100,0%** | 0 | 63,1% | 100,0% | **0,0%** |

*Ambang Produksi Terpilih*: **Minimum Support $\ge 3$ & Confidence $\ge 0,80$** (Menghasilkan precision $\ge 97,4\%$ dengan tingkat kesalahan minimal).

---

## 3. Aturan Normalisasi Deskripsi

Aturan pembersihan string deskripsi untuk pengenalan penerima/merchant:
1. Menghapus awalan aksi (*transfer ke, bayar, top up, beli, terima*).
2. Menghapus stempel tanggal dan jam (*22/08, 13:37*).
3. Menghapus nomor referensi, nomor rekening, dan ID transaksi (*ref #123456, trx 998877*).
4. Menghapus nominal rupiah di dalam keterangan (*Rp 50.000*).
5. Menghapus tag kurung siku (*[Rekonsiliasi]*).

**Contoh Normalisasi Tersamarkan**:
1. `"Top up GoPay via BCA 06/08"` $\rightarrow$ `"gopay via"`
2. `"Bayar Pijat Kung ref 88392"` $\rightarrow$ `"pijat kung"`
3. `"Transfer ke BCA Poket Tabungan"` $\rightarrow$ `"poket tabungan"`
4. `"Tagihan PLN 22/08 50k"` $\rightarrow$ `"pln"`
5. `"Biaya admin top up ShopeePay"` $\rightarrow$ `"admin shopeepay"`

---

## 4. Product Decision Gate Matrix

| Kandidat Fitur / Arsitektur | Keputusan | Alasan & Rencana Tindak Lanjut |
|---|:---:|---|
| **Item Ganda di Menu Lainnya** | **`KEEP`** | Berfungsi sebagai *mobile navigation hub* & mempermudah discoverability pengguna di layar kecil. |
| **`linkAllocModal`** | **`KEEP`** | Menangani use-case penautan alokasi Dana Dijaga lama/existing tanpa harus membuat alokasi baru. |
| **Endpoint Server Legacy (`/api/pass_through`, `/api/statement`)** | **`DEPRECATED-BUT-RETAIN`** | Dipertahankan untuk kompatibilitas backward dan script eksternal. |
| **Submenu Keyboard Accessibility** | **`IMPLEMENT`** | Tambahkan `tabindex="0"`, handler `Enter`/`Space`, dan `role="button"` pada `.menu-list-item`. |
| **Saran Kategori (Draft Suggestion)** | **`IMPLEMENT`** | Rule-based lokal (Support $\ge 3$, Conf $\ge 0,80$, Precision 98,3%). |
| **Saran Rekening Asal & Tujuan** | **`IMPLEMENT`** | Rule-based lokal (Precision $\ge 97,4\%$, membantu pengisian transfer berulang). |
| **Saran Pihak & Konteks Uang** | **`IMPLEMENT`** | Rule-based lokal (Precision 100%). |
| **Model Machine Learning (ML)** | **`REJECT`** | Tidak diperlukan; overhead dependensi, rawan overfit, dan *black-box*. |
| **Rules-based Recommender Lokal** | **`IMPLEMENT`** | 100% offline, cepat (<1ms), transparan, hanya mengisi draft dengan konfirmasi pengguna. |
