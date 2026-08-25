# Rangkaian Prompt Perbaikan — Money Tracks V12

Dokumen ini berisi riwayat instruksi terstruktur (*prompt sequence*) untuk perbaikan dan penguatan Money Tracks V12.

---

## Ringkasan Progres

- [x] **Prompt 0**: Audit Klien Baseline `[SELESAI]`
- [x] **Prompt 0b**: Audit Lanjutan Backend `[SELESAI]`
- [x] **Prompt 1**: Perbaikan Kritis Sisi Klien (K5, S1, A1, A2, K6) `[SELESAI]`
- [x] **Prompt 1b**: Perbaikan Backend & Cache Guard (B3, B1) `[SELESAI]`
- [x] **Prompt 1c**: Perbaikan Rekonsiliasi Drift Ledger vs Cache `[SELESAI]`
- [x] **Prompt 2**: Acceptance Tests & Security Test Baseline `[SELESAI]`
- [x] **Prompt 2b**: Perbaikan R-01, R-02, dan UI Fallback `[SELESAI]`
- [x] **Prompt 3**: Pilihan rekening & hapus default rekening diam-diam (K3/S2) `[SELESAI]`
- [x] **Prompt 4**: Konsistensi rincian hero (K4) `[SELESAI]`
- [x] **Prompt 5**: Anti-dobel-potong Dana Dijaga dan kewajiban (K1) `[SELESAI]`
- [x] **Prompt 6**: Pemisahan titipan, piutang, dan utang (K2) `[SELESAI]`
- [x] **Prompt 7**: Penentuan `budget_effect` otomatis dan proteksi idempotensi (S3/S4) `[SELESAI]`
- [x] **Prompt 8**: Audit dan Pemolesan UI/UX `[SELESAI]`
- [x] **Prompt 9**: Perbaikan Ledger ShopeePay & Release E2E `[SELESAI]`
- [x] **Prompt 10**: Navigasi Menu, Saran Transaksi, Sync ShopeePay `[SELESAI]`
- [x] **Prompt 11a**: Audit & Kontrak Arsitektur Alokasi Dana `[SELESAI]`
- [x] **Prompt 11b**: Implementasi Mesin Alokasi Dana & Formula Backend `[SELESAI]`
- [x] **Prompt 11c**: Implementasi UI Dana, Komitmen, & Tujuan Keuangan `[SELESAI]`
- [x] **Prompt 11d**: Migrasi Data Produksi & Verifikasi Final `[SELESAI]`
- [x] **Prompt 11f**: Hardening Migrasi Idempoten & Baseline Database `[SELESAI]`
- [x] **Prompt 12**: Insight Transaksi Berulang & Proyeksi Kas `[SELESAI]`
- [x] **Prompt 13a**: Mesin Ingestion, Staging, & Review Queue `[SELESAI]`

---

## Riwayat Prompt yang Telah Selesai

### Prompt 0 — Audit Klien Baseline `[SELESAI]`
Audit frontend Money Tracks V12 untuk mendeteksi potensi bug pada penanganan tanggal, clamp alokasi harian, konfirmasi AI, sanitasi XSS innerHTML, dan pemisahan fungsi perhitungan murni.

### Prompt 0b — Audit Lanjutan Backend `[SELESAI]`
Audit backend pada `services.py`, `money_tracks_server.py`, dan `db.py` terkait atomisitas transaksi, isolasi transfer/titipan, penggunaan tipe float REAL vs toleransi, pintu belakang saldo pada `/api/account/balance`, dan metode rekonstruksi saldo dari balance snapshots.

### Prompt 1 — Perbaikan Kritis Klien `[SELESAI]`
- **K5**: Waktu lokal WIB di `markPaid` dan `saveAccount` (Commit `32ec96d`).
- **S1**: Tampilkan `safeDaily` negatif saat defisit anggaran tanpa clamp ke nol (Commit `5d3c681`).
- **A1**: Konfirmasi nama dan nominal alokasi sebelum eksekusi AI (Commit `50c22a0`).
- **A2**: Sanitasi nilai proposal AI lewat `esc()` dan validasi integer ID (Commit `5ef77ac`).
- **K6**: Ekstraksi fungsi murni perhitungan alokasi dan rata-rata ke `core.js` (Commit `f946646`).

### Prompt 1b — Perbaikan Backend & Cache Guard `[SELESAI]`
- **B3**: Batas toleransi `abs(diff) >= 0.005` untuk kesetaraan float dan pembulatan `round(SUM, 2)` (Commit `18886f6`).
- **B1**: Tutup pintu belakang `accounts.current_balance`, buat pengawas `verify_balances()`, dan isolasi test A–I (Commit `370f37c`, `50a1d6b`, `a535cbd`, `fd9ab11`).

### Prompt 1c — Perbaikan Rekonsiliasi Drift Ledger vs Cache `[SELESAI]`
- Ekstraksi `reconstruct_account_balance(con, account_name)`.
- Rumus rekonsiliasi berbasis selisih ledger $A - E$ dan perbaikan cache $A - C$ tanpa transaksi keuangan palsu.
- Hentikan backfill saldo snapshot otomatis pada startup database (Commit `0ad8844`).

### Prompt 2 — Acceptance Tests Baseline `[SELESAI]`
- Pembuatan suite pengujian Node.js murni untuk `core.js` (A1–A4).
- Pembuatan acceptance tests invarian spesifikasi T-01 s.d. T-10 dan security tests R-01 s.d. R-05 (Commit `106bb6d`).

### Prompt 2b — Perbaikan R-01, R-02, dan UI Fallback `[SELESAI]`
- **R-01**: Transaksi rekonsiliasi bertipe `Adjustment`, `subtype='Balance Reconciliation'`, `budget_effect=0`, dikecualikan dari laporan pemasukan/pengeluaran pribadi (Commit `74bf647`).
- **R-02**: Migrasi kolom `snapshot_kind` pada `balance_snapshots` dan algoritma pemilihan anchor saldo terpercaya (`manual_anchor`/`initial_anchor`) untuk mengatasi snapshot collision (Commit `47b7172`).
- **UI**: Blokir tombol submit dan cegah fallback data dummy saat API `/api/account/reconstruct` gagal (Commit `e484982`).

### Prompt 3 — Pilihan Rekening & Hapus Default Rekening Diam-Diam (K3/S2) `[SELESAI]`
- **K3**: Pilihan rekening penyimpan alokasi dana dijaga via dropdown `<select id="allocAccount">` tervalidasi (Commit `7056a10`).
- **S2**: Modal pembayaran kewajiban dengan input rekening tervalidasi dan tanggal lokal WIB, serta penghapusan seluruh fallback `|| 'BCA Main'` diam-diam (Commit `35984be`).

### Prompt 4 — Konsistensi Rincian Hero Banner (K4) `[SELESAI]`
- **K4**: Validasi `componentsMatchHero()` pada bulan berjalan, serta transformasi kartu bulan lampau ("Ringkasan Arus Kas") dan bulan mendatang ("Ringkasan Proyeksi") tanpa tanda formula aritmetika (Commit `4d65d25`).

### Prompt 5 — Anti-Dobel-Potong Dana Dijaga & Kewajiban (K1) `[SELESAI]`
- **K1**: Relasi skema `covers_upcoming_id`, kalkulasi *effective commitment*, status alokasi idempoten, dan pembayaran kewajiban atomik tanpa dobel potong (Commit `6eb37bd`, `63e1529`).

### Prompt 6 — Pemisahan Titipan, Piutang, dan Utang (K2) `[SELESAI]`
- **K2**: Event Ledger bertanda `debts` & `debt_events` sebagai sumber kebenaran tunggal saldo outstanding pihak ketiga.
- Relasi cicilan `upcoming.debt_id` terintegrasi dengan service atomik `pay_upcoming_atomic()`.
- Formula STS lengkap $\text{STS} = B - P - T - U_{\text{eff}} - X_{\text{eff}}$ dengan pembuktian live STS konstan saat cicilan pokok utang dibayar (Commit `d526193`, `5c941b2`).

### Prompt 7 — Penentuan budget_effect Otomatis & Proteksi Idempotensi (S3/S4) `[SELESAI]`
- **S3**: Skema `exclude_from_budget`, `budget_exclusion_reason`, `budget_rule_version` ('legacy'/'derived'). Pembekuan 1.126 transaksi legacy, helper tunggal `effective_budget_spend(row)` (zero budget leakage), UI checkbox dengan alasan wajib, dan badge diskrepansi pada 23 baris legacy.
- **S4**: Tabel `idempotency_requests` terpusat dengan canonical SHA-256 hash. Proteksi idempoten di seluruh 8 endpoint mutasi, serialisasi `BEGIN IMMEDIATE`, rollback penuh saat error, dan persistensi client key pada retry (Commit `a657e4a`, `7c5c195`).

### Prompt 8b1 — Upgrade Path Third-party & Keamanan Render Dashboard (AUD-01 & AUD-04) `[SELESAI]`
- **AUD-01**: Migrasi CHECK constraint tabel `transactions` pada database hasil upgrade untuk mengizinkan `Third-party` dan `Adjustment` tanpa mengubah 1.126 data historis.
- **AUD-04**: Sanitasi output HTML dinamis pada `homeActionsList` di `app.js` dengan `esc()`.
- **Verifikasi Dropdown**: Bukti audit konkret membuktikan dropdown pergerakan uang telah memfilter rekening aktif bertipe `Owned`.

### Prompt 8b2 — Responsivitas Mobile dan Modal (AUD-02 & Touch Target AUD-06) `[SELESAI]`
- **AUD-02**: Breakpoints responsive CSS (`768px`, `650px`, `480px`), layout form grid 1-kolom pada mobile, table wrapper scrolling tanpa overflow body, dan tipografi nilai angka responsif.
- **AUD-06 (Touch Target)**: Touch target 44×44px untuk navigasi, tombol modal, close `×`, dan tombol aksi mobile.
- **Modal UX**: Modal `max-height: 90dvh` / `90vh` dengan vertical scrolling dan safe area insets.

### Prompt 8b3 — Aksesibilitas Form, Modal, Fokus, dan Feedback (AUD-03) `[SELESAI]`
- **AUD-03**: 50 `<label for>` terhubung ke kontrol masing-masing (0 kontrol tanpa accessible name, 0 broken label, 0 duplicate ID). Semantik modal dialog (`role="dialog"`, `aria-modal="true"`, `aria-labelledby`). Focus stack terpusat, focus trap keyboard, restore focus ke pemicu, live region toast `role="status"`, `:focus-visible` outline, dan kepatuhan kontras warna WCAG AA.

---

### Prompt 8b4 — Penyelarasan Kamus Istilah UI dan Live Format Ribuan (AUD-05 & AUD-06) `[SELESAI]`
- **AUD-05**: Penyelarasan kamus istilah kanonis di seluruh UI visible ("Dana Tersedia Digunakan", "Total Saldo Likuid", "Dana Dijaga", "Titipan", "Kewajiban Aktif", "Penyesuaian").
- **AUD-06**: Integrasi helper murni `parseMoneyInput()`, `formatMoneyInput()`, `setMoneyInput()`, dan `attachLiveMoneyFormatting()` di 8 money fields dengan dukungan desimal, prefill, reset, dan paste.
- **Rangkaian Prompt 8**: Selesai penuh (AUD-01 s/d AUD-06 ditutup).

---

### Prompt 9a — Verifikasi Database Otoritatif dan Koreksi Ledger BCA `[SELESAI]`
- **Penetapan Anchor**: Snapshot ID 4 (Rp218.821,55) ditetapkan sebagai `manual_anchor` kanonis pada 22 Agustus 2026.
- **Koreksi Transaksi**: 4 mutasi pra-anchor (admin ShopeePay Rp500, topup GoPay Rp20.000 + admin Rp1.000, tarik tunai Rp50.000) dan 7 mutasi post-anchor (pijat Kung Rp135.000, bunga Rp0,25, transfer iuran Rp840.000, transfer ShopeePay Rp750.000).
- **Hasil Saldo**: BCA Main = Rp83.821,80, BCA Poket Tabungan = Rp1.673.000, BCA Poket Iuran = Rp840.000, BCA Poket Charger = Rp0 (inactive). Reconstruct status ok, verify_balances discrepancy = 0.
- **Integritas**: Hash awal `67b51695...`, hash akhir `53fa7ae2...`, 1.137 transaksi kanonis aktif.

### Prompt 9b & 9b-HP — Release Readiness Audit, Manual HP Test & Final Verification `[SELESAI]`
- **Verdict**: `FINAL RELEASE VERIFIED`
- **Manual HP Check**: `PASS` (Pengujian pada browser Chrome HP fisik via jaringan lokal LAN terbukti responsif, tanpa overflow, modal scrollable, format nominal live & paste lancar, touch target 44px nyaman).
- **Verifikasi Akhir**: Startup produksi bersih, full regression 12 test suites PASS, E2E lifecycle 100% PASS, recovery drill identik, ledger BCA Main Rp83.821,80, final production SHA-256 `53fa7ae22a4fb176c60e1a47f7a49f6880bc21795951451cc7cfccc2fabde3b8` 100% integer.

### Prompt 10a — Navigation State, Account Layout, dan Database Source Verification `[SELESAI]`
- **Navigasi**: Menyelaraskan mapping `goPage` untuk 6 menu utama (Beranda, Transaksi, Anggaran, Laporan, Rekening, Lainnya) dengan tepat satu active page, satu active nav, dan `aria-current="page"`. Submenu Lainnya menyorot Lainnya dengan indikator aktif.
- **Layout Rekening**: Mengubah `.account-scroll-wrap` menjadi flex container vertikal responsif, memisahkan `.hero-acc-grid` (1-3 col) dan `.other-acc-grid` (auto-fill minmax 130px) untuk mencegah kartu bertumpuk di seluruh viewport 360px–1920px.
- **Verifikasi Sumber**: Saldo BCA Main terbukti konsisten Rp83.821,80 pada DB, API, dan DOM tanpa mutasi DB.

### Prompt 10b, 10c, 10d & 10e — Local Intelligence & Submenu Keyboard A11y `[SELESAI]`
- **Audit Halaman & Decision Gate**: Inventaris 10 halaman, 10 modal, 10 form, 79 tombol, dan 33 endpoint UI terverifikasi (`audit-prompt10-pages.md`, `audit-prompt10-decision-gate.md`). ML ditolak ("ML Belum Diperlukan"); model berbasis aturan lokal deterministik disetujui.
- **Saran Draft Transaksi Lokal**: Implementasi service `suggest_transaction_draft` (`support >= 3`, `confidence >= 0.80`) untuk `category`, `account_from`, dan `account_to`. Zero cloud, 100% offline, explainable, konfirmasi wajib via tombol *"Terapkan Saran"*.
- **Aksesibilitas Submenu**: Seluruh `.menu-list-item` pada halaman `#more` dikonversi menjadi `<button type="button">` native dengan `focus-visible` dan navigasi keyboard (`Tab`, `Enter`, `Space`).
- **Holdout Accuracy**: 100,0% precision pada pengujian chronological holdout (53/53 benar, 0 false suggestion).
- **Integritas Produksi**: Hash database `53fa7ae2...` 100% utuh tanpa mutasi.

### Prompt 10f — Adversarial Verification Saran Lokal & Runtime Manual Gate `[VERIFIED]`
- **Evaluasi Holdout & Walk-Forward**: Category Precision 100,0%, Account From Precision 97,8%–99,0%, Account To Precision 96,3%–98,2% dengan 0-1 false suggestion.
- **Verifikasi Adversarial & Proteksi Edit Mode**: Bebas temporal leakage, normalisasi tahan variasi ref/prefix, saran nonaktif otomatis pada edit transaksi lama (`isEditing=true`).
- **Review Queue**: Terverifikasi konsisten 68 transaksi (`Provisional Neutral`).
- **Kinerja**: Median latency 12,2 ms (<20 ms P95), zero logging PII / zero cloud dependency.

---

## Status Proyek

### Money Tracks V12 — Production Ready & Verified
Seluruh tahapan audit, koreksi ledger, perbaikan UI/UX/A11y, pengujian end-to-end, dan verifikasi fisik HP telah selesai dengan status **FINAL RELEASE VERIFIED**. Sistem siap digunakan sepenuhnya.
