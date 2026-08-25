# Dokumen Audit & Status Perbaikan — Money Tracks V12

Dokumen ini mencatat riwayat audit sistem, status penutupan temuan teknis, dan inventarisasi masalah yang masih terbuka (*open items*).

---

## 1. Status Pekerjaan Keseluruhan

| Tahapan | Lingkup Pekerjaan | Status | Catatan / Commit Terkait |
| :--- | :--- | :--- | :--- |
| **Prompt 0** | Audit Baseline Klien (Frontend) | `SELESAI` | Audit menyeluruh UI/JS selesai |
| **Prompt 0b** | Audit Baseline Backend & Database | `SELESAI` | Audit transaksi, atomisitas, dan rekonsiliasi |
| **Prompt 1** | Perbaikan Kritis Klien (K5, S1, A1, A2, K6) | `SELESAI` | Commit `32ec96d`, `5d3c681`, `50c22a0`, `5ef77ac`, `f946646` |
| **Prompt 1b** | Perbaikan Backend & Pengawas Saldo (B3, B1) | `SELESAI` | Commit `18886f6`, `370f37c`, `50a1d6b`, `a535cbd`, `fd9ab11` |
| **Prompt 1c** | Perbaikan Rekonsiliasi Drift Ledger vs Cache | `SELESAI` | Commit `0ad8844` (Test J1–J5 lolos) |
| **Prompt 2** | Test Suite Baseline & Audit Tambahan | `SELESAI` | Commit `106bb6d` (Test A1–A4, T01–T10, R01–R05) |
| **Prompt 2b** | Perbaikan R-01, R-02, dan Fallback UI | `SELESAI` | Commit `74bf647`, `47b7172`, `e484982` |
| **Prompt 3** | Pilihan Rekening Tervalidasi & Hapus Default Diam-Diam (K3/S2) | `SELESAI` | Commit `7056a10`, `35984be` |
| **Prompt 4** | Konsistensi Rincian Hero Banner & Ringkasan (K4) | `SELESAI` | Commit `4d65d25` |
| **Prompt 5** | Integrasi Dana Dijaga ↔ Kewajiban Anti-Dobel Potong (K1) | `SELESAI` | Relasi `covers_upcoming_id`, effective commitments, status & pay atomik |
| **Prompt 6** | Pemisahan Titipan, Piutang, dan Utang (K2) | `SELESAI` | Event ledger K2, titipan/piutang/utang terisolasi |
| **Prompt 7** | Penentuan `budget_effect` & Idempotensi (S3/S4) | `SELESAI` | Idempotency key, atomic replay, budget effect derived |
| **Prompt 8** | Audit & Pemolesan UI/UX, Aksesibilitas, Responsif | `SELESAI` | Kamus istilah ID, ARIA modal, touch target 44px |
| **Prompt 9** | Perbaikan Ledger ShopeePay & Release E2E | `SELESAI` | Ledger audit, sinkronisasi transaksi ShopeePay |
| **Prompt 10** | Navigasi Menu, Saran Transaksi, Sync ShopeePay | `SELESAI` | 5 menu utama, local suggestion engine, ShopeePay final |
| **Prompt 11a** | Audit & Kontrak Arsitektur Alokasi Dana | `SELESAI` | Kontrak alokasi independen rekening di `audit-prompt11-allocation-contract.md` |
| **Prompt 11b** | Implementasi Mesin Alokasi Dana & Formula Backend | `SELESAI` | Commit `ae2cff8` (Tabel `allocation_goals`, formula backend independen) |
| **Prompt 11c** | Implementasi UI Dana, Komitmen, & Tujuan Keuangan | `SELESAI` | Commit `300d2e3` (Headline Dana Tersedia Saat Ini, tab, modal ARIA) |
| **Prompt 11d** | Migrasi Data Produksi & Verifikasi Final | `SELESAI` | Migrasi `allocation_goals` & `upcoming` sukses |
| **Prompt 11f** | Hardening Migrasi Idempoten & Baseline DB | `SELESAI` | Idempotensi `init_db()` terbukti, hash test dinamis, 21 test suite 100% hijau |
| **Prompt 12** | Insight Transaksi Berulang & Proyeksi Kas | `SELESAI` | Deteksi pola berulang, forecast 7/30 hari, indikator kebaruan rekening |
| **Prompt 13a** | Mesin Ingestion, Staging, & Review Queue | `SELESAI` | Skema staging, parser contract, deduplikasi 4-tier, approval atomik |


---

## 2. Temuan yang Sudah Ditutup (Resolved Findings)

### K5 — Zona Waktu UTC pada Tanggal Sisi Klien
- **Status**: `DITUTUP` (Commit `32ec96d`)
- **Solusi**: Mengganti panggilan `new Date().toISOString().slice(0,10)` di `markPaid` dan `saveAccount` pada `app.js` dengan `getLocalDateString()` yang berbasis Waktu Indonesia Barat (WIB, UTC+7).

### S1 — Clamp Nol pada Alokasi Harian Defisit
- **Status**: `DITUTUP` (Commit `5d3c681`)
- **Solusi**: Menghapus `Math.max(0, ...)` pada perhitungan `safeDaily`. Ketika sisa anggaran negatif, angka ditampilkan apa adanya dengan kelas warna `.bad` dan keterangan defisit anggaran.

### A1 — Eksekusi AI Release Allocation Tanpa Konfirmasi
- **Status**: `DITUTUP` (Commit `50c22a0`)
- **Solusi**: Menambahkan dialog konfirmasi eksplisit `confirm()` pada `executeAIReleaseAllocation` di `app.js` yang menyebutkan judul alokasi dan nominal rupiah sebelum dilepaskan.

### A2 — XSS / Injeksi HTML pada Proposal AI Stream Bubble
- **Status**: `DITUTUP` (Commit `5ef77ac`)
- **Solusi**: Membungkus seluruh nilai teks dinamis dari model AI (`action_proposal`) dengan fungsi `esc()` dan memvalidasi `tx_id` sebagai integer positif sebelum merender tombol tindakan.

### K6 — Penentuan Jumlah Hari Bulan Terpilih di `core.js`
- **Status**: `DITUTUP` (Commit `f946646`)
- **Solusi**: Mengekstrak logika perhitungan tanggal murni ke `core.js` (`computeSafeDaily`, `computeDailyAverage`). Menghitung jumlah hari bulan lampau secara penuh dan bulan berjalan mencakup hari ini.

### B3 — Perbandingan Kesetaraan Float Tanpa Toleransi
- **Status**: `DITUTUP` (Commit `18886f6`)
- **Solusi**: Mengganti perbandingan `diff != 0` dengan batas toleransi `abs(diff) >= 0.005` di `services.py` dan membulatkan setiap hasil `SUM()` ke dua desimal.

### B1 — Pintu Belakang Saldo & Pengawas Cache Independen
- **Status**: `DITUTUP` (Commit `370f37c`, `50a1d6b`, `a535cbd`, `fd9ab11`, `0ad8844`)
- **Solusi**:
  - Mengubah `/api/account/balance` agar mengarahkan pembaruan saldo rekening lama melalui alur audit rekonsiliasi.
  - Mengimplementasikan `reconstruct_account_balance(con, account_name)` sebagai verifikasi independen dari anchor + mutasi.
  - Memperbaiki rumus rekonsiliasi ledger ($A - E$) dan pembetulan cache ($A - C$) tanpa menerbitkan transaksi palsu jika mutasi ledger sudah cocok.

### R-01 — Isolasi Transaksi Rekonsiliasi dari Laporan Personal
- **Status**: `DITUTUP` (Commit `74bf647`)
- **Solusi**:
  - Transaksi penyesuaian rekonsiliasi menggunakan `transaction_type = 'Adjustment'`, `subtype = 'Balance Reconciliation'`, dan `budget_effect = 0.0`.
  - Menambahkan terjemahan UI `Adjustment` $\to$ `Penyesuaian` dengan pill netral.
  - Mengecualikan `Adjustment` dan deskripsi berprefix `[Rekonsiliasi]` dari agregasi pemasukan, pengeluaran, anggaran, dan *savings rate*.

### R-02 — Pemilihan Anchor Saldo Terpercaya (`snapshot_kind`)
- **Status**: `DITUTUP` (Commit `47b7172`)
- **Solusi**:
  - Menambahkan kolom `snapshot_kind` (`initial_anchor`, `manual_anchor`, `mutation`, `reconciliation`, `legacy`) pada `balance_snapshots`.
  - `reconstruct_account_balance` memprioritaskan `manual_anchor` / `initial_anchor` terbaru (`ORDER BY id DESC`) untuk menggantikan snapshot lama yang ambigu/collision.
  - Menyediakan migrasi otomatis dan skrip rollback migrasi yang aman.

### UI Safety — Blokir Rekonsiliasi saat API Gagal (Silent Fallback Removal)
- **Status**: `DITUTUP` (Commit `e484982`)
- **Solusi**:
  - `openReconcileModal` menonaktifkan tombol submit `recSubmitBtn` sebelum memanggil API.
  - Blok `catch` menetapkan `currentReconData = null`, menampilkan pesan kesalahan, dan membiarkan tombol tetap nonaktif.
  - `submitReconciliation` menolak proses jika `currentReconData` bernilai null.

### K3 / S2 — Rekening Wajib Dipilih & Hapus Default Diam-Diam
- **Status**: `DITUTUP` (Commit `7056a10`, `35984be`)
- **Solusi**: Form alokasi Dana Dijaga dan modal pembayaran kewajiban mewajibkan pilihan `<select>` rekening aktif milik (`Owned`). Validasi server menolak rekening kosong, palsu, atau tidak aktif.

### K4 — Inkonsistensi Rincian Hero Banner
- **Status**: `DITUTUP` (Commit `4d65d25`)
- **Solusi**: Menambahkan fungsi murni `componentsMatchHero()` di `core.js`. Bulan lampau diubah menjadi "Ringkasan Arus Kas" dan bulan mendatang menjadi "Ringkasan Proyeksi" tanpa tanda aritmetika formula.

### K1 — Anti-Dobel-Potong Dana Dijaga & Kewajiban
- **Status**: `DITUTUP` (Commit `6eb37bd`, `63e1529`)
- **Solusi**:
  - Menambahkan kolom relasi `covers_upcoming_id` pada `protected_allocations` dengan indeks dan FK reversibel.
  - Perhitungan `current_commitments` beralih ke *effective commitment*: $X_{\text{eff}} = \max(0, X - C)$.
  - Memusatkan helper `reduce_account_protected_contribution` dan `set_protected_allocation_status` yang idempoten.
  - Pembayaran kewajiban di `/api/upcoming/pay` mengeksekusi mutasi kas, penandaan alokasi `Spent`, penurunan `protectedSavings`, dan status `Paid` secara atomik dengan rollback guard.

### K2 — Pemisahan Titipan, Piutang, dan Utang via Event Ledger
- **Status**: `DITUTUP` (Commit `d526193`)
- **Solusi**:
  - Mengimplementasikan `debts` dan `debt_events` sebagai Event Ledger bertanda ($+1/-1$) sumber kebenaran tunggal saldo outstanding.
  - Memisahkan konsep aljabar: Titipan ($B-P-T$), Piutang (aset display non-likuid), dan Utang ($U_{\text{eff}} = \max(0, \text{Pokok} - \text{Covered})$).
  - Menambahkan relasi `upcoming.debt_id` untuk cicilan pokok utang yang dieksekusi secara atomik di `/api/upcoming/pay`.
  - Meniadakan input manual `passThroughOutstanding` dan formulir wizard yang memaksa `Expense`.
  - Menambahkan halaman UI Pihak Ketiga dengan manajemen posisi, pergerakan bertanda, pelunasan, dan koreksi/reversal.
  - *Catatan pemulihan*: Backup pra-K2 historis tidak tersedia; titik pemulihan resmi dibuat sebelum Prompt 7.

### S3 & S4 — Dampak Anggaran Otomatis (Derived) & Global Idempotency (Prompt 7b)
- **Status**: SELESAI
- **Perubahan Utama**:
  - **S3 Budget Effect**: Menghapus input nominal manual `fBudgetEffect`. Menambahkan kolom `exclude_from_budget`, `budget_exclusion_reason`, dan `budget_rule_version` ('legacy'/'derived'). Seluruh 1.126 transaksi lama dibekukan dalam aturan `legacy` sehingga total belanja historis per bulan tetap identik. Transaksi baru selalu `derived` dan nilainya diturunkan secara tunggal melalui `effective_budget_spend(row)`. Pengecualian anggaran mewajibkan alasan. 23 baris diskrepansi legacy ditandai dengan badge "Aturan lama—perlu ditinjau" di UI.
  - **S4 Idempotency Ledger**: Membuat tabel `idempotency_requests` terpusat dengan canonical SHA-256 payload hash (tanpa menyertakan header Idempotency-Key). Menerapkan proteksi idempoten di seluruh 8 endpoint wajib (create/update transaction, create allocation, create upcoming, account reconciliation, upcoming pay, create debt position, create debt event). Menyediakan helper pengiriman di klien dengan persistensi kunci selama retry.
### Prompt 8b1 — Upgrade Path Third-party & Keamanan Render Dashboard (AUD-01 & AUD-04)
- **Status**: SELESAI (P0 / Keamanan)
- **Perubahan Utama**:
  - **AUD-01 CHECK Constraint**: Memperluas constraint tabel `transactions` pada database hasil upgrade menjadi `CHECK(money_context IN ('Personal','Pass-through','Historical Research','Third-party'))` dan `CHECK(transaction_type IN ('Income','Expense','Transfer','Adjustment'))` via migrasi aman yang mempertahankan 1.126 baris, seluruh ID, canonical ID, dan total bulanan. Alur kas Titipan, Piutang, dan Utang (K2) kini dapat berjalan tanpa kendala constraint.
  - **AUD-04 Keamanan Render**: Sanitasi properti `a.title` dan `a.text` pada blok `homeActionsList` di `app.js` menggunakan fungsi `esc()` untuk mencegah eksekusi payload HTML dinamis/XSS.
  - **Verifikasi Dropdown Rekening**: Audit konkret membuktikan bahwa seluruh 7 dropdown pergerakan uang (`fFrom`, `allocAccount`, `uAccount`, `payUpcomingAccount`, `debtAccountSelect`, `evAccountSelect`, `recAccount`) sudah memfilter secara ketat rekening aktif bertipe `Owned`, sedangkan `fTo` tetap mengizinkan pihak eksternal/piutang aktif sesuai desain transfer.
  - **Recovery Point**: Backup pra-Prompt-8 dibuat di `backups/money_tracks_pre_prompt8_20260823_202654_671257.db` (SHA-256 baseline: `891ef7ed430b1fabf82043a2f21a411de827fddb57354dc5fc2de6eebc0a0768`). Hash baru pasca-migrasi: `67b51695637d060c158935465357a54931f6454221c92aec16a2863dc2b7a313`.

### Prompt 8b2 — Responsivitas Mobile dan Modal (AUD-02 & Touch Target AUD-06)
- **Status**: SELESAI (Responsivitas & Modal UX)
- **Perubahan Utama**:
  - **AUD-02 Responsivitas Mobile**: Penerapan breakpoint CSS terstruktur (`@media (max-width: 768px)`, `650px`, dan `480px`). Form grid 2-kolom otomatis menjadi 1-kolom pada mobile. Wrapper tabel `.table-wrap` mendukung horizontal scrolling touch tanpa memicu horizontal body overflow. Tipografi nilai angka menggunakan `overflow-wrap: break-word` dan font responsif `clamp()` sehingga nominal besar tidak terpotong.
  - **Modal & Keyboard Mobile**: Modal kini menggunakan `max-height: 90dvh` / `90vh`, scrolling vertikal `-webkit-overflow-scrolling: touch`, dan padding safe area `env(safe-area-inset-*)`.
  - **Touch Target (AUD-06 Bagian Mobile)**: Tombol navigasi, tombol modal, tombol close `×`, dan tombol aksi cepat diset dengan ukuran sentuh minimal 44×44px pada layar $\le 768\text{px}$.
  - **Verifikasi Visual**: 7 screenshot visual berhasil diuji dan diverifikasi menggunakan headless browser di berbagai viewport (390×844, 360×800, 1440×900).

### Prompt 8b3 — Aksesibilitas Form, Modal, Fokus, dan Feedback (AUD-03)
- **Status**: SELESAI (Aksesibilitas, Semantik & Focus Management)
- **Perubahan Utama**:
  - **Accessible Name & Label Association**: Menghubungkan 50 `<label for="id">` ke kontrol formulir masing-masing, menambahkan `aria-label` untuk kontrol tanpa label eksplisit dan tombol icon-only (seperti `.close` "Tutup modal"). Duplicate ID: 0, Broken label: 0.
  - **Semantik Modal Dialog**: Menambahkan `role="dialog"`, `aria-modal="true"`, dan `aria-labelledby` yang mengarah ke ID unik judul modal di seluruh 10 modal.
  - **Centralized Focus Trap & Restore**: Implementasi focus stack terpusat pada `openModal`/`closeModal` di `app.js` yang memfokuskan elemen pertama, mengunci rotasi fokus via `Tab`/`Shift+Tab` di modal aktif, dan mengembalikan fokus ke tombol pemicu saat ditutup via `Escape`/tombol.
  - **Feedback & Visual Access**: Kontainer notifikasi toast `role="status"` `aria-live="polite"`, indikator `:focus-visible` (outline 2px solid), dan dukungan `prefers-reduced-motion`. Rasio kontras teks utama memenuhi standar WCAG AA (13.98:1 untuk teks normal, 5.71:1 untuk muted text terhadap panel).

### Prompt 8b4 — Penyelarasan Kamus Istilah UI & Finalisasi Format Uang (AUD-05 & AUD-06)
- **Status**: SELESAI (Kamus Istilah UI & Formatter Kanonis)
- **Perubahan Utama**:
  - **Penyelarasan Istilah (§5)**: Seluruh string terlihat pengguna telah diselaraskan penuh dengan kamus istilah spesifikasi ("Dana Tersedia Digunakan", "Total Saldo Likuid", "Dana Dijaga", "Titipan", "Kewajiban Aktif", "Penyesuaian"). Enum internal basis data tetap dalam bahasa Inggris untuk kompatibilitas.
  - **Formatter & Parser Kanonis**: Helper murni `parseMoneyInput()`, `formatMoneyInput()`, `setMoneyInput()`, dan `attachLiveMoneyFormatting()` diintegrasikan di seluruh 8 kontrol input uang dengan dukungan desimal, prefill, reset, dan paste.
  - **Loading Feedback**: Status submit async visual ("Memproses...") dan pemulihan teks/label lengkap.

---

## 3. Temuan Produksi yang TELAH Diselesaikan (Prompt 9a)

### Rekonsiliasi Ledger BCA Main & Penetapan Anchor Kanonis
- **Status**: `SELESAI` (Prompt 9a)
- **Detail Resolusi**:
  - **Penyebab False Positive**: Snapshot legacy ID 1 (Rp168.821,55) sebelumnya terpilih secara fallback karena belum adanya penanda explicit `manual_anchor`. Bukti mutasi e-statement mengonfirmasi saldo anchor fisik per 22 Agustus 2026 adalah **Rp218.821,55** (snapshot ID 4).
  - **Penetapan Anchor**: Snapshot ID 4 ditetapkan sebagai `manual_anchor`, sedangkan snapshot ID 1 dipertahankan sebagai `legacy` (non-authoritative) tanpa dihapus.
  - **Koreksi Transaksi Pra-Anchor**: Menambahkan 4 mutasi terlewat sebelum 22 Agustus (Admin ShopeePay Rp500, Top up GoPay Rp20.000, Admin GoPay Rp1.000, Tarik Tunai Rp50.000). Transaksi ini tidak mengubah `current_balance` karena telah tercakup dalam anchor fisik 22 Agustus.
  - **Transaksi Post-Anchor (22–23 Agustus)**: Memasukkan 7 mutasi riil (Pijat Kung Rp135.000, Bunga Poket Rp0,25, Transfer Iuran Rp840.000, dan rangkaian transfer ShopeePay Rp750.000).
  - **Hasil Akhir**:
    - Saldo BCA Main: **Rp83.821,80** (Reconstructed $E$ = Cached $C$ = Physical $A$, selisih **Rp0,00**).
    - Tidak ada transaksi `Adjustment` atau `[Rekonsiliasi]` sintetis.
    - Zero discrepancy di seluruh suite `verify_balances`.
  - **Integritas & Hash**:
    - Pre-repair SHA-256: `67b51695637d060c158935465357a54931f6454221c92aec16a2863dc2b7a313`
    - Post-repair SHA-256: `53fa7ae22a4fb176c60e1a47f7a49f6880bc21795951451cc7cfccc2fabde3b8`
    - Backup Kanonis: `backups/money_tracks_pre_ledger_repair_20260823_221832.db` (Valid & FK clean).

---

## 4. Status Temuan UI/UX (Prompt 8)
- **AUD-01 (P0 Upgrade Path Third-party)**: `SELESAI` (Prompt 8b1)
- **AUD-04 (P1 Keamanan Render Dashboard)**: `SELESAI` (Prompt 8b1)
- **AUD-02 (P1 Responsivitas Mobile / Media Queries)**: `SELESAI` (Prompt 8b2)
- **AUD-06 (P2 Touch Target Mobile & Live Thousand-Separator)**: `SELESAI` (Prompt 8b2 & H1-H3)
- **AUD-03 (P1 Aksesibilitas Form, Modal Focus, Feedback & Contrast)**: `SELESAI` (Prompt 8b3)
- **AUD-05 (P2 Penyelarasan Kamus Istilah UI)**: `SELESAI` (Prompt 8b4)

---

## 5. Status Release Readiness & Final Verification (Prompt 9b & 9b-HP)
- **Verdict**: `FINAL RELEASE VERIFIED`
- **Manual HP Check**: `PASS` (Dashboard/navigasi normal, zero horizontal overflow, modal scrolling lancar saat keyboard virtual terbuka, tombol tidak tertutup, live format & paste nominal lancar, touch target 44px responsif).
- **Hasil Audit & Verifikasi**:
  - Baseline otoritatif & schema markers: Terverifikasi penuh.
  - Startup produksi read-only: Bersih (0 HTTP 500, 0 JS error, 0 DB mutation).
  - Full regression suite: 100% PASS (12 suite pengujian).
  - End-to-End & Invariant verification: 100% PASS pada temporary database.
  - Recovery drill: Terbukti identik & reversibel.
  - Ledger BCA Main: Tepat Rp83.821,80 (zero discrepancy).
  - SHA-256 final produksi: `53fa7ae22a4fb176c60e1a47f7a49f6880bc21795951451cc7cfccc2fabde3b8` (Identik, 100% integer).
  - Total Transaksi Aktif: 1.137 baris (`integrity_check=ok`, `foreign_key_check=[]`).

---

## 6. Penyelarasan Navigasi & Layout Rekening (Prompt 10a)
- **Status**: `SELESAI`
- **Perbaikan**:
  - **Navigasi 6 Menu Utama**: Memperbaiki mapping `goPage()` sehingga navigasi ke `Laporan` (`reports`) dan `Rekening` (`accounts`) tepat menyorot tombol masing-masing dan menerapkan `aria-current="page"`, bukan menyorot `Lainnya`. Submenu `Lainnya` (`upcoming`, `thirdparty`, `provisional`, `updates`) menyorot `Lainnya` dengan penanda item aktif.
  - **Layout Kartu Rekening**: Mengubah `.account-scroll-wrap` menjadi flex container vertikal dan menyelaraskan `.hero-acc-grid` & `.other-acc-grid` di seluruh 5 breakpoint layar (360px s/d 1920px) untuk mencegah tumpang tindih kartu atau overflow.
  - **Verifikasi Data BCA Main**: Saldo BCA Main terverifikasi konsisten Rp83.821,80 pada DB, API, dan DOM browser tanpa selisih (Rp0,00).

---

## 7. Saran Transaksi Lokal & Akses Keyboard Submenu (Prompt 10e)
- **Status**: `SELESAI`
- **Implementasi**:
  - **Saran Draft Transaksi Lokal**: Service read-only deterministik berbasis aturan lokal (`support >= 3`, `confidence >= 0.80`) untuk `category`, `account_from`, dan `account_to`. 100% offline, explainable, hanya mengisi draft form saat tombol *"Terapkan Saran"* ditekan tanpa auto-submit/auto-save.
  - **Aksesibilitas Submenu**: Seluruh item menu navigasi `#more` dikonversi menjadi elemen native `<button type="button" class="menu-list-item">` dengan indikator `focus-visible` dan dukungan navigasi keyboard penuh (`Tab`, `Enter`, `Space`).
  - **Evaluasi Holdout**: Precision 100,0% pada holdout masa depan (53/53 benar, 0 false suggestion).
  - **Integritas Database**: SHA-256 `53fa7ae22a4fb176c60e1a47f7a49f6880bc21795951451cc7cfccc2fabde3b8` 100% terjaga identik.

---

## 9. Migrasi Repositori Kanonikal ke GitHub (Prompt 10h)
- **Status**: `SELESAI`
- **Tindakan**:
  - Repositori kanonikal aktif dialihkan ke `C:\A User Main Storage\Documents\GitHub\AturUang`.
  - Repositori lama di GitHub diarsipkan dengan aman ke `AturUang_ARCHIVE_20260824_<timestamp>`.
  - Salinan di Downloads dipertahankan utuh sebagai cadangan pemulihan.
  - Dibuat launcher otomatis `START_MONEY_TRACKS.bat` dengan resolusi root dinamis `%~dp0` dan deteksi port 5050.

---

## 10. Perbaikan Invarian Dana Dijaga & Pembenahan Istilah UI (Prompt 10j)
- **Status**: `SELESAI`
- **Perbaikan**:
  - **Invarian Dana Dijaga Independen**: Menghapus coupling `min(current_balance, protected_amount)` pada `get_account_protected_contribution` dan `save_account_balance`. Dana dijaga (`protected_amount`) kini merepresentasikan target/tujuan proteksi eksplisit yang tidak tereduksi oleh pergeseran transfer internal antar-rekening milik sendiri (Owned).
  - **Proteksi Penonaktifan Rekening**: Rekening dengan `protected_amount > 0` dilarang dinonaktifkan tanpa pelepasan/pemindahan dana dijaga terlebih dahulu.
  - **Pelepasan Eksplisit**: Dana dijaga hanya berkurang lewat tindakan eksplisit: alokasi ditandai `Spent` / `Released` atau perubahan target proteksi oleh pengguna.
  - **Pembenahan Istilah UI**: "Dana Tersedia untuk Digunakan", "Dana Dijaga", "Titipan Aktif", "Rekening, Dompet & Poket", dan "Kelola Rekening". Menghapus badge inferensi "Utama" yang tidak berdasar.

---

## 11. Sinkronisasi ShopeePay, Piutang Mbak Erin & Koreksi PLN (Prompt 10K)
- **Status**: `SELESAI`
- **Tindakan & Perubahan**:
  - **Koreksi PLN 24 Agustus**: Transaksi ID 1142 diperbarui menjadi Rp94.000,00 (`Phone & Internet` / `Electricity`, `for_with_whom='Family'`, `budget_effect=94000.0`, catatan debit aktual). Transaksi ID 1143 (admin fee Rp1.500) di-soft-delete sehingga dampak kas bersih ShopeePay terpulihkan +Rp7.500,00.
  - **Piutang Mbak Erin**: Dibuat 1 posisi `Receivable` atas nama "Mbak Erin" dengan 3 debt events cash-out dari ShopeePay (23 Ags: Rp500.000 & Rp150.000, 24 Ags: Rp100.000) senilai total outstanding Rp750.000,00. Transaksi tercatat `money_context='Third-party'` dengan `budget_effect=0.0` (terisolasi dari pengeluaran pribadi).
  - **Internet Keluarga 24 Agustus**: Dicatat 1 transaksi `Expense` Rp170.000,00 dari ShopeePay ke Mbak Erin (`Phone & Internet` / `Family`, `Personal`, `budget_effect=170000.0`).
  - **Deduplikasi**: Top up ShopeePay 23–24 Agustus dan transaksi Shopee Marketplace historis terverifikasi tidak digandakan.
  - **Post-Sync Production Baseline**:
    - Saldo ShopeePay: Rp51.482,00
    - Total Aset Likuid: Rp2.379.368,80
    - Dana Dijaga: Rp2.500.000,00 (Konstan)
    - Kewajiban Aktif: Rp840.000,00 (Konstan)
    - Piutang Aktif (Mbak Erin): Rp750.000,00
    - Safe-to-Spend: -Rp960.631,20 (Badge: `Defisit`)
    - Total Pengeluaran Pribadi Agustus: Rp4.104.508,00 (+Rp162.500 net)
    - Total Transaksi Aktif: 1.149 baris (`integrity_check=ok`, `foreign_key_check=[]`)
    - SHA-256 Database Produksi Baru: `4d39297f1fe1cc3a7df7b323c57dc6f8752f83f5a86591d019822fdd884057ad`
