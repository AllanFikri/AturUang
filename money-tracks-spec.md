# Spesifikasi Sistem — Money Tracks V12

Dokumen ini adalah spesifikasi teknis resmi untuk **Money Tracks V12** (versi aplikasi pembukuan kas personal dan Safe-to-Spend / Dana Tersedia).

---

## 1. Arsitektur Aktual Sistem

Money Tracks V12 adalah aplikasi lokal berbasis *client-server* yang beroperasi secara *offline-first*:

- **Frontend**: Vanilla JavaScript murni (ES6+), HTML5, CSS3.
  - Tidak menggunakan framework (tanpa React, Vue, Svelte, Next.js).
  - Tidak memerlukan *build step*, *transpiler*, *bundler* (Webpack, Vite), ataupun *package manager* (`npm`, `yarn`).
  - Berkas antarmuka dapat langsung disajikan oleh server lokal atau dibuka langsung di peramban modern.
  - Perhitungan logika murni seperti alokasi harian dan rata-rata berjalan terpusat di `core.js`.
- **Backend**: Python standard library (`http.server`, `sqlite3`, `json`, `datetime`, `pathlib`).
  - Server HTTP ringan: `money_tracks_server.py`.
  - Logika bisnis, transaksi, mutasi saldo, dan agregasi laporan: `services.py`.
  - Ekstraksi mutasi e-statement bank: `statement.py`.
  - Pengelola kunci AI dan perutean agen AI: `ai_key_manager.py`, `ai_router.py`, `financial_ai_tools.py`, `gemini_service.py`.
- **Database**: SQLite 3 (`db.py`, `money_tracks.db`).
  - Model data relasional dengan integritas kunci asing (`PRAGMA foreign_keys = ON`).
  - File database lokal tunggal dengan dukungan pencadangan otomatis sebelum mutasi kritis (`backup_db()`).

> **Catatan Arsitektur**: Arsitektur aktif adalah Vanilla JS + Python HTTP Server + SQLite. Arsitektur ini dilarang diganti atau dibangun ulang dengan stack lain.

---

## 2. Keputusan Presisi & Aturan Nominal

1. **Tipe Data Kolom**:
   - Kolom nominal uang dalam database SQLite disimpan dengan tipe data `REAL` (mempertahankan kompatibilitas skema brownfield).
2. **Pembulatan Dua Desimal**:
   - Setiap titik penulisan mutasi (`amount`, `budget_effect`) dan hasil agregasi `SUM()` wajib dibulatkan dua desimal secara konsisten:
     ```python
     round(float(value), 2)
     ```
3. **Toleransi Kesetaraan Float (0,005)**:
   - Mengingat representasi float, perbandingan kesetaraan nilai uang tidak boleh membandingkan dengan nol persis (`diff != 0`), melainkan menggunakan ambang batas toleransi:
     ```python
     if abs(diff) >= 0.005:
         # Selisih dianggap nyata
     ```
4. **Migrasi Integer Sen**:
   - Migrasi skema database ke integer sen ditunda karena biaya modifikasi menyentuh seluruh lapisan backend tanpa memberikan dampak signifikan pada skala pemakaian personal ini. Opsi integer sen dapat dievaluasi kembali di masa depan jika skala aplikasi berkembang.

---

## 3. Desain Saldo, Cache & Rekonsiliasi

### 3.1. Konsep Saldo dan Cache yang Diawasi
- **`accounts.current_balance`**: Merupakan nilai *cache* yang diperbarui secara langsung saat transaksi dicatat agar pembacaan dashboard berjalan instan. Nilai ini **bukan** otoritas kebenaran tunggal.
- **`reconstruct_account_balance(con, account_name)`**: Merupakan otoritas verifikasi independen (*single source of verification truth*). Fungsi ini merekonstruksi saldo buku yang diharapkan ($E$) berdasarkan titik jangkar (*anchor*) terpercaya ditambah seluruh mutasi transaksi setelah jangkar:
  $$E = 	ext{Balance}_{\text{anchor}} + \sum \text{Mutasi Masuk} - \sum \text{Mutasi Keluar}$$
- **`verify_balances(con)`**: Pengawas berkala (*cache supervisor*) yang membandingkan $E$ (rekonstruksi ledger) terhadap $C$ (`current_balance` cache) tanpa pernah mengubah data pengguna secara otomatis.

### 3.2. Taksonomi Snapshot (`snapshot_kind`)
Tabel `balance_snapshots` dilengkapi dengan kolom `snapshot_kind` untuk membedakan peruntukan tiap baris:
- `initial_anchor`: Saldo awal saat rekening pertama kali didaftarkan.
- `manual_anchor`: Saldo titik jangkar baru yang ditetapkan dan dikonfirmasi eksplisit oleh pengguna (`force_anchor=True`).
- `mutation`: Rekaman jejak saldo setelah terjadi transaksi rutin dari `mutate_account_balance`.
- `reconciliation`: Titik rekonsiliasi berkala setelah proses audit selesai.
- `legacy`: Baris snapshot historis yang dibuat sebelum migrasi `snapshot_kind`.

### 3.3. Algoritma Pemilihan Anchor
1. Cari anchor eksplisit terbaru dengan `snapshot_kind IN ('manual_anchor', 'initial_anchor')` diurutkan `ORDER BY id DESC LIMIT 1`.
2. Jika tidak ditemukan, *fallback* ke snapshot lama dengan `snapshot_kind IN ('legacy')` diurutkan `ORDER BY id ASC LIMIT 1`.
3. Snapshot bertipe `mutation` atau `reconciliation` **tidak boleh** dipilih sebagai baseline anchor.
4. Lakukan pemeriksaan tabrakan cap waktu (*collision check*): jika terdapat transaksi pada tanggal anchor dengan `created_at` yang persis sama dengan `created_at` anchor, tandai akun sebagai `unverifiable` sampai pengguna menetapkan `manual_anchor` baru.

### 3.4. Transaksi Rekonsiliasi (`transaction_type = "Adjustment"`)
Ketika rekonsiliasi saldo fisik ($A$) dilakukan terhadap saldo buku ($E$):
- Jika terdapat selisih ledger $|A - E| \ge 0{,}005$, sistem menerbitkan **1 transaksi rekonsiliasi**:
  - `transaction_type = "Adjustment"`
  - `amount = abs(A - E)` (selalu positif)
  - Arah: `account_to = account_name` jika $A > E$; `account_from = account_name` jika $A < E$.
  - `budget_effect = 0.0`
  - `subtype = "Balance Reconciliation"`
  - `description = "[Rekonsiliasi] Penyesuaian Saldo Audit ..."`
- **Dampak Laporan**: Transaksi `Adjustment` memengaruhi saldo rekening dan Safe-to-Spend, namun **dikecualikan** dari agregasi pemasukan pribadi, pengeluaran pribadi, anggaran bulanan, dan perhitungan *savings rate*.
- **Rumus Penyesuaian**: Selisih ledger dihitung dari $A - E$, bukan dari $A - C$.

---

## 4. Invarian Sistem (§9 & Prompt 11)

1. **INV-1 (Integritas Saldo & Rekonstruksi)**:
   Setiap saldo akun harus dapat dibuktikan dari snapshot awal terpercaya ditambah mutasi transaksi yang valid.
2. **INV-2 (Formula Kanonikal Dana Tersedia Saat Ini / Safe to Spend)**:
   $$\text{Dana Tersedia} = \text{Total Aset Likuid} - \text{Titipan Aktif} - \text{Dana Darurat} - \text{Komitmen Pasti} - \text{Dana Tujuan Teralokasi} - \text{Tentatif Dicadangkan} - \text{Pengeluaran Pending}$$
   - **Independensi Rekening**: Alokasi dana (`allocation_goals`) adalah pagu/tujuan proteksi murni yang independen dari letak fisik rekening kas. Transfer antar-rekening milik sendiri (`Owned`) bernilai netral matematis dan tidak mengubah Dana Tersedia.
   - **Komitmen Pasti**: Kewajiban berstatus `Upcoming` / `Confirmed` yang belum dibayar mengurangi Dana Tersedia.
   - **Rencana Tentatif**: Kewajiban berstatus `Tentative` hanya mengurangi Dana Tersedia jika `reserve_now = 1`. Jika `reserve_now = 0`, kewajiban ditampilkan sebagai rencana tanpa memotong dana bebas.
   - **Tujuan Keuangan & Dana Darurat**: Hanya nominal yang sudah teralokasi (`allocated_amount`) yang memotong Dana Tersedia.
   - **Pelepasan Eksplisit**: Dana Darurat/Tujuan hanya berkurang lewat aksi eksplisit: pelepasan alokasi (`release_allocation_goal`) atau pembelanjaan alokasi (`spend_allocation_goal`).
   - **Perilaku Defisit**: Jika total aset likuid tidak mencukupi, Dana Tersedia bernilai negatif dan antarmuka menampilkan status `Defisit`.
   - **Pihak Ketiga (K2 Event Ledger)**: Saldo posisi pihak ketiga direkonstruksi dari $\sum(\text{effect} \times \text{amount})$. Titipan ($T$) memotong Dana Tersedia langsung; Piutang ($R$) sebagai aset non-likuid; Utang ($U_{\text{eff}} = \max(0, \text{Pokok} - \text{Covered})$).
   - **Invarian Pembayaran**: Pembayaran kewajiban memotong kas dan melunasi kewajiban terjadwal sehingga Dana Tersedia terbukti konstan:
     $$\text{STS}' = (B - A) - (C - A) = B - C = \text{STS}_{\text{sebelum}}$$
3. **INV-3 (Pengecualian Transfer)**:
   Transaksi `Transfer` hanya memindahkan dana antar-rekening sendiri dan tidak boleh dihitung sebagai pendapatan atau pengeluaran.
4. **INV-4 (Pemisahan Pihak Ketiga / Third-party & Legacy Pass-through)**:
   Pergerakan dana pihak ketiga (`money_context='Third-party'` atau `Pass-through`) tidak mempengaruhi anggaran belanja pribadi (`budget_effect=0`).
5. **INV-5 (Presisi Dua Desimal & Toleransi Float)**:
   Setiap mutasi disimpan dengan presisi 2 desimal dan perbandingan uang mematuhi toleransi 0,005.
6. **INV-6 (Waktu Lokal WIB)**:
   Batas tanggal, hari dalam bulan, dan pencatatan transaksi diselaraskan dengan Waktu Indonesia Barat (WIB, UTC+7). Dilarang memakai `toISOString().slice(0,10)` untuk tanggal yang ditampilkan ke pengguna.
7. **INV-7 (Sanitasi Masukan AI)**:
   Semua data dan teks proposal yang berasal dari model AI wajib melalui fungsi `esc()` sebelum dimasukkan ke dalam elemen DOM/`innerHTML`.

---

## 5. Kamus Istilah Antarmuka (§9.7 & Prompt 11)

| Istilah Kode / Sistem | Terjemahan Antarmuka (ID) | Keterangan Tampilan |
| :--- | :--- | :--- |
| `Income` | Pemasukan | Pill hijau (`.pill.income`) |
| `Expense` | Pengeluaran | Pill merah (`.pill.expense`) |
| `Transfer` | Transfer | Pill biru (`.pill.transfer`) |
| `Adjustment` | Penyesuaian | Pill abu-abu/netral (`.pill.adjustment`) |
| `Confirmed` | Sudah Dicek | Status transaksi terverifikasi |
| `Auto-classified` | Dikenali Otomatis | Status transaksi hasil klasifikasi otomatis |
| `Provisional Neutral`| Belum Jelas - Tidak Dihitung | Transaksi penampung/sementara |
| `Upcoming` | Belum Dibayar | Status kewajiban aktif |
| `Paid` | Sudah Dibayar | Status kewajiban lunas |
| `Tentative` | Tentatif | Status rencana kewajiban tentatif |
| `Safe-to-Spend` | Dana Tersedia Saat Ini | Metrik utama kebebasan belanja harian |
| `Emergency Fund` | Dana Darurat | Pagu dana darurat murni teralokasi |
| `Confirmed Commitment` | Komitmen Pasti | Kewajiban pasti terjadwal |
| `Tentative Commitment` | Rencana Tentatif | Rencana pengeluaran belum pasti |
| `Financial Goal` | Tujuan Keuangan | Target alokasi tabungan masa depan |
| `Pass-through / Custody` | Titipan Aktif | Dana titipan pihak ketiga |

---

## 6. Acceptance & Security Test Suite (§11)

Suite pengujian otomatis meliputi:
- **Test A–I & J1–J5**: Verifikasi independen integritas saldo, deteksi selisih, proteksi collision, dan audit log (`test_verify_balances.py`).
- **Test A1–A4 & UI1–UI2**: Kemurnian perhitungan `core.js` dan keamanan UI modal rekonsiliasi (`test_core_js.py`).
- **Test T-01..T-10 & R-01..R-05**: Acceptance & Security test invarian spesifikasi (`test_acceptance_and_security.py`).
- **Test S3-01..07 & S4-08..15**: Budget effect canonical rule, isolasi non-personal, global idempotency requests, dan recovery (`test_prompt7_s3_s4.py`).
- **Test Prompt 11 Suite**: Pengujian mesin alokasi dana berbasis tujuan (`test_prompt11_allocations.py`) dan pengujian UI/flow alokasi (`test_prompt11_ui.py`).

---

## 7. Saran Draft Transaksi Lokal & Akses Keyboard Submenu (§12)

- **Mekanisme Rekomendasi**:
  - Berbasis aturan (*rule-based*) deterministik lokal 100% offline (Zero Cloud / Zero ML Dependency).
  - Target prediksi: `category`, `account_from`, `account_to`.
  - Ambang batas: `support >= 3` dan `confidence >= 0.80` per field secara independen.
  - Sifat saran: Hanya mengisi draft form; tidak pernah memposting atau menyimpan transaksi otomatis (*user confirmation required*).
- **Aksesibilitas Submenu**: Seluruh `.menu-list-item` menggunakan elemen native `<button type="button">` dengan navigasi `Tab`, `Enter`, `Space`, dan `focus-visible`.

---

## 8. Migrasi Data Alokasi Dana (Prompt 11d)

- **Status Deprecasi**: Kolom `accounts.protected` dan `accounts.protected_amount` telah didepresiasi penuh (dikosongkan ke `0`) dan tidak lagi menjadi sumber formula.
- **Tabel Otoritas Alokasi**: `allocation_goals` menjadi otoritas tunggal untuk alokasi dana darurat dan tujuan finansial.
- **Konfigurasi Awal Produksi**:
  - Dana Darurat: Rp500.000,00 (Kind: Emergency, Status: Active)
  - IOM semester ini: Rp120.000,00 (Upcoming, Status: Upcoming, reserve_now: 1)
  - Renew by.U data package: Rp70.000,00 (Upcoming, Status: Upcoming, reserve_now: 1)
  - Langganan AI: Rp60.000,00 (Upcoming, Status: Upcoming, reserve_now: 1)
  - Tiket Bus / Perjalanan PKL: Rp650.000,00 (Upcoming, Status: Tentative, reserve_now: 0)
- **Hash Basis Data Produksi & Milestone Historis**:
  - Baseline Audit 11a: `4d39297f1fe1cc3a7df7b323c57dc6f8752f83f5a86591d019822fdd884057ad` (checkpoint historis sebelum WAL truncate)
  - Backup Pra-migrasi 11d: `54eafec81236eb76e0796ad3bea607c8e529171cea30008ce6696830093de6d8` (`backups/money_tracks_pre_prompt11_migration_20260825_004405.db`)
  - Pasca-migrasi 11d: `fc882e0f05e224b8d2b26627b1ecda7ead2747be62a255e3ffa1f753150da84d` (checkpoint sesaat setelah migrasi DDL)
  - **Baseline Final Produksi Pasca-Startup (Prompt 11f, 12, 13a)**: `1fa3f6d5d7c87b6dd865ed34afc4458bc5dd8b6601b606a783faaa6306d1f976`
- **Aturan Isolasi Hash Test (Prompt 11f)**:
  - Test umum dilarang meng-hardcode SHA-256 produksi tertentu.
  - Test membandingkan hash DB produksi sebelum dan sesudah test (`self.assertEqual(actual_hash, self.prod_hash_before)`) untuk menjamin zero mutation.
  - Idempotensi `init_db()` terbukti 100% no-op secara skema, data, dan file hash.
- **Otomatisasi & Insight Transaksi Berulang (Prompt 12)**:
  - Deteksi pola berulang deterministik offline (`detect_recurring_patterns`) mengelompokkan merchant, menghitung support (min 3), median nominal & interval, serta variasi (CV).
  - Klasifikasi: `Langganan / Tagihan` (CV nominal <= 10%), `Pola Belanja`, dan `Abstain`.
  - Proyeksi Kas 7/30 Hari (`cashflow_forecast`) memisahkan `Proyeksi Pasti` (Upcoming Confirmed) dan `Proyeksi Perkiraan` (+ Pola Berulang akurasi tinggi).
  - Indikator Kebaruan Catatan Rekening (`get_accounts_freshness`): label terstandarisasi (`Baru diperbarui`, `Perlu diperiksa`, `Belum pernah diverifikasi`) tanpa asumsi keliru bahwa saldo pasti benar.
- **Pipeline Ingestion & Staging (Prompt 13a)**:
  - Pipeline kanonis 6 tahap: `SOURCE → RAW EVENT → PARSED CANDIDATE → DEDUPLICATION → REVIEW → TRANSACTION`.
  - Skema staging terisolasi: `import_batches`, `raw_import_events`, `import_candidates`.
  - Sanitasi & redaksi ketat pada raw payload (menghapus nomor kartu, OTP, token/password).
  - Parser contract provider (`BaseProviderParser`) dengan implementasi BCA, ShopeePay, Jago, dan CSV.
  - Deduplikasi 4-tier (external event ID, source_refs, exact match date/amount/account, probable +-1 day).
  - Approval atomik memposting transaksi kanonis, memutasi saldo akun, dan mencatat audit log.
