# Audit Kegunaan dan Keterkaitan Setiap Halaman — Money Tracks V12

> **Tanggal Audit**: 23 Agustus 2026  
> **Status**: Selesai (Read-Only Audit)  
> **Ruang Lingkup**: 10 Halaman `.page`, 10 Modal, 10 Form, 79 Tombol, 33 Endpoint UI, 40 Endpoint Server.

---

## 1. Ringkasan Inventaris Sistem

| Kategori | Jumlah | Komponen Teridentifikasi |
|---|:---:|---|
| **Halaman Utama (`.page`)** | 6 | `home` (Beranda), `transactions` (Transaksi), `budget` (Anggaran), `reports` (Laporan), `accounts` (Rekening), `more` (Lainnya) |
| **Subhalaman (`.page`)** | 4 | `upcoming` (Kewajiban), `thirdparty` (Pihak Ketiga), `provisional` (Belum Jelas), `updates` (Pembaruan) |
| **Modal Dialog** | 10 | `txModal`, `upcomingModal`, `payUpcomingModal`, `reconcileModal`, `reversalModal`, `aiConfigModal`, `linkAllocModal`, `debtPositionModal`, `debtEventModal`, `debtDetailModal` |
| **Formulir Terdaftar** | 10 | `txForm`, `upcomingForm`, `payUpcomingForm`, `reconcileForm`, `reversalForm`, `aiChatForm`, `aiConfigForm`, `linkAllocForm`, `debtPositionForm`, `debtEventForm` |
| **Endpoint UI Aktif** | 33 | Endpoint REST yang terhubung dengan interaksi DOM pengguna |
| **Endpoint Server** | 40 | Handler route di `money_tracks_server.py` |

---

## 2. Matriks Audit per Halaman & Komponen

### A. Beranda (`#home`)
- **Tujuan Pengguna**: Ringkasan keputusan harian, status likuiditas, dan akses cepat pencatatan.
- **Informasi Terlihat**: Hero KPI *Dana Tersedia Digunakan*, status kesehatan saldo, rincian 4 kartu dana, daftar ringkas rekening/poket (3 Utama + Sekunder), 3 kartu mikro info (*Tagihan*, *Review*, *Alokasi Harian*), feed transaksi terkini, dan chart pengeluaran donat.
- **Tindakan Boleh Dilakukan**: Buka modal transaksi baru, beralih mode privasi saldo, ubah bulan, klik kartu rekening untuk rekonsiliasi, klik quick stat untuk lompat ke modul terkait.
- **Endpoint**: `GET /api/dashboard?month=YYYY-MM`, `GET /api/accounts`.
- **Efek Finansial**: Read-only (tidak memutasi angka).
- **Konfirmasi & Idempotency**: N/A (read-only).
- **Status**: **`KEEP`** (Arsitektur bersih, hero cocok matematis dengan komponen).

### B. Transaksi (`#transactions`)
- **Tujuan Pengguna**: Sumber kebenaran mutasi kas (Income, Expense, Transfer, Third-party, Adjustment).
- **Informasi Terlihat**: Tabel log transaksi lengkap (tanggal, tipe, nominal, rekening, kategori, pihak, deskripsi, status), filter pencarian teks, filter status, dan selector bulan.
- **Tindakan Boleh Dilakukan**: Tambah transaksi baru (wizard 3 langkah), edit transaksi, hapus transaksi (soft-delete), reversal transaksi dengan alasan.
- **Endpoint**: `GET /api/transactions`, `POST /api/transactions`, `POST /api/transaction/delete`, `POST /api/reversal`.
- **Efek Finansial**: Mutasi `accounts.current_balance`, pembaruan saldo rekonstruksi $E$, penyesuaian belanja kategori aktual.
- **Konfirmasi & Idempotency**: Guard idempotency key (`_pendingIdempotencyKeys`), modal konfirmasi reversal, live currency helper.
- **Status**: **`KEEP`** (Core ledger engine).

### C. Anggaran & Kategori (`#budget`)
- **Tujuan Pengguna**: Perencanaan alokasi batas belanja bulanan, monitoring realisasi, dan evaluasi sisa.
- **Informasi Terlihat**: Kartu ringkasan total anggaran vs realisasi belanja vs sisa, daftar kategori belanja dengan progress bar warna, status rollover, dan saran penyesuaian otomatis.
- **Tindakan Boleh Dilakukan**: Simpan batas anggaran kategori, refresh saran anggaran AI/heuristik, terapkan saran alokasi, lakukan realokasi antar-kategori.
- **Endpoint**: `GET /api/budget`, `POST /api/budget/save`, `GET /api/budget/refresh_suggestions`, `POST /api/budget/apply_suggestions`, `POST /api/budget/reallocate`.
- **Efek Finansial**: Memperbarui tabel `budget_limits` dan `budget_reallocations`. Tidak mengubah saldo fisik kas ($C$ / $E$), hanya membatasi alokasi belanja.
- **Konfirmasi & Idempotency**: Konfirmasi modal realokasi.
- **Status**: **`KEEP`** (Fungsi kontrol pengeluaran).

### D. Rekening & Poket (`#accounts`)
- **Tujuan Pengguna**: Pemantauan saldo per rekening fisik/dompet/poket tabungan, verifikasi kesehatan ledger, dan manajemen alokasi Dana Dijaga.
- **Informasi Terlihat**: Daftar rekening aktif & non-aktif, jenis rekening (*Owned*, *Investment*, *Pass-through*), saldo tercatat ($C$), saldo rekonstruksi ($E$), selisih rekonsiliasi, daftar alokasi Dana Dijaga aktif, dan tautan coverage kewajiban.
- **Tindakan Boleh Dilakukan**: Tambah alokasi Dana Dijaga, tautkan/lepas alokasi ke jadwal upcoming, buka modal rekonsiliasi/anchor, edit profil rekening.
- **Endpoint**: `GET /api/accounts`, `POST /api/account/balance`, `POST /api/protected_allocation`, `POST /api/protected_allocation/link`, `POST /api/protected_allocation/unlink`, `POST /api/reconcile`.
- **Efek Finansial**: Mengubah `protectedSavings`, `safeToSpend`, dan mencatat snapshot/anchor saldo.
- **Konfirmasi & Idempotency**: `reconcileForm` memvalidasi selisih $>0.005$, atomic rollback saat terjadi error rekonsiliasi.
- **Status**: **`KEEP`** (Core liquidity & safety engine).

### E. Laporan & E-Statement (`#reports`)
- **Tujuan Pengguna**: Analisis arus kas bulanan/tahunan dan cetak dokumen resmi Laporan Keuangan Pribadi.
- **Informasi Terlihat**: Ringkasan Pemasukan Bersih, Pengeluaran Pribadi, Tabungan Tersimpan, Net Arus Kas, tabel perbandingan bulanan, grafik visual breakdown kategori.
- **Tindakan Boleh Dilakukan**: Unduh/cetak e-statement HTML/PDF, pilih rentang periode analitik.
- **Endpoint**: `GET /api/reports?month=YYYY-MM`.
- **Efek Finansial**: Read-only (100% isolasi data).
- **Konfirmasi & Idempotency**: N/A (read-only).
- **Status**: **`KEEP`** (Modul laporan analitik).

### F. Kewajiban & Tagihan Rutin (`#upcoming`)
- **Tujuan Pengguna**: Pengelolaan komitmen pembayaran terjadwal (Internet, PLN, Tiket, Cicilan).
- **Informasi Terlihat**: Daftar kewajiban bulan ini (status: *Upcoming*, *Paid*, *Skipped*), nominal, tanggal jatuh tempo, rekening pembayaran default, dan status proteksi Dana Dijaga terkait.
- **Tindakan Boleh Dilakukan**: Tambah tagihan baru, bayar tagihan (membuka `payUpcomingModal`), lewati tagihan (*Skip*), hapus jadwal.
- **Endpoint**: `GET /api/upcoming`, `POST /api/upcoming`, `POST /api/upcoming/pay`, `POST /api/upcoming/status`.
- **Efek Finansial**: Pembayaran tagihan otomatis membuat transaksi `Expense` berstatus `Confirmed` dan mengurangi `currentCommitments` serta saldo kas asal tanpa menyebabkan pemotongan ganda pada STS.
- **Konfirmasi & Idempotency**: `payUpcomingForm` dengan tanggal dan rekening eksplisit.
- **Status**: **`KEEP`** (Mekanisme zero double-deduction terbukti).

### G. Pihak Ketiga: Titipan, Piutang, & Utang (`#thirdparty`)
- **Tujuan Pengguna**: Mengisolasi pergerakan uang pihak ketiga dari penghasilan dan belanja pribadi.
- **Informasi Terlihat**: Tab *Titipan (Custody)*, *Piutang (Receivable)*, *Utang (Payable)*, outstanding per orang, log event mutasi (*Opening*, *Increase*, *Settlement*, *WriteOff*).
- **Tindakan Boleh Dilakukan**: Buka posisi baru (`debtPositionModal`), catat event mutasi kas/non-kas (`debtEventModal`), lihat riwayat detail (`debtDetailModal`).
- **Endpoint**: `GET /api/debts`, `GET /api/debts/detail`, `POST /api/debts`, `POST /api/debts/event`, `POST /api/debts/reverse`.
- **Efek Finansial**: Mengubah kas fisik (`accounts`), tetapi `budget_effect = 0.0` (tidak mencemari laporan pengeluaran pribadi).
- **Konfirmasi & Idempotency**: `operation_key` guard dan rekonstruksi non-negative invariant.
- **Status**: **`KEEP`** (Arsitektur K1/K2 kanonis).

### H. Transaksi Belum Jelas / Review Queue (`#provisional`)
- **Tujuan Pengguna**: Mengamankan transaksi yang belum diketahui peruntukannya agar tidak merusak perhitungan anggaran sebelum diklarifikasi.
- **Informasi Terlihat**: Daftar transaksi bertanda status `Provisional` atau `Historical Research`.
- **Tindakan Boleh Dilakukan**: Konfirmasi kategori & pihak (mengubah menjadi `Confirmed`), hapus transaksi palsu.
- **Endpoint**: `GET /api/transactions?status=Provisional`, `POST /api/transactions`.
- **Efek Finansial**: Transaksi netral (`budget_effect = 0.0`) hingga dikonfirmasi menjadi kategori definitif.
- **Status**: **`KEEP`** (Review queue isolatif).

### I. Lainnya (`#more`) & Pengaturan
- **Tujuan Pengguna**: Pusat navigasi sekunder, pemilihan tema tampilan, dan unduh cadangan data.
- **Informasi Terlihat**: Menu list akses subhalaman, pilihan 5 tema visual, tombol unduh CSV & Database, dan kamus ringkas istilah keuangan.
- **Tindakan Boleh Dilakukan**: Navigasi ke submodul, ganti tema visual (`themeSelect`), ekspor CSV, unduh file `money_tracks.db`.
- **Endpoint**: `GET /api/export_csv`, `GET /api/download_db`.
- **Efek Finansial**: Read-only.
- **Status**: **`IMPROVE`** (Perlu pembersihan item menu duplikat yang sudah ada di top navigation).

### J. Pembaruan Aplikasi (`#updates`)
- **Tujuan Pengguna**: Pengecekan versi rilis dan konfigurasi tautan manifest pembaruan GitHub.
- **Informasi Terlihat**: Versi aktif, pesan rilis, status updater launcher, input URL manifest.
- **Tindakan Boleh Dilakukan**: Cek pembaruan manual, simpan pengaturan auto-check.
- **Endpoint**: `GET /api/update_info`, `POST /api/update_settings`.
- **Efek Finansial**: Non-finansial.
- **Status**: **`KEEP`** (Offline-first updater).

### K. Asisten AI & Konfigurasi (`#aiConfigModal` / Float Button)
- **Tujuan Pengguna**: Fitur opsional asisten percakapan cerdas keuangan.
- **Status**: **`KEEP (ISOLATED)`** (Terisolasi penuh dan aman tanpa memengaruhi logika angka finansial).

---

## 3. Matriks Trace Fungsional (UI → Backend → Data → KPI)

| Aksi Pengguna | Trigger UI | Endpoint | Service Backend | Tabel Terdampak | Perubahan KPI |
|---|---|---|---|---|---|
| **Catat Pengeluaran** | `txForm` Submit | `POST /api/transactions` | `services.validate_tx` | `transactions`, `accounts` | Saldo Kas $\downarrow$, STS $\downarrow$, Belanja $\uparrow$ |
| **Bayar Tagihan** | `payUpcomingForm` Submit | `POST /api/upcoming/pay` | `services.pay_upcoming_bill` | `upcoming`, `transactions`, `accounts` | Saldo Kas $\downarrow$, Tagihan Aktif $\downarrow$, STS Tetap |
| **Alokasi Tabungan** | `allocModal` Submit | `POST /api/protected_allocation` | `services.create_protected_allocation` | `protected_allocations` | Dana Dijaga $\uparrow$, STS $\downarrow$, Saldo Kas Tetap |
| **Terima Titipan** | `debtPositionForm` Submit | `POST /api/debts` | `services.create_debt_position` | `debts`, `debt_events`, `transactions`, `accounts` | Saldo Kas $\uparrow$, Titipan $\uparrow$, STS Tetap |
| **Rekonsiliasi Fisik** | `reconcileForm` Submit | `POST /api/reconcile` | `services.save_account_balance` | `accounts`, `transactions`, `balance_snapshots`, `audit_logs` | Saldo Kas $\pm$, Selisih $\rightarrow 0$, STS $\pm$ |
| **Realokasi Anggaran** | `reallocateModal` Submit | `POST /api/budget/reallocate` | `services.reallocate_budget` | `budget_reallocations`, `budget_limits` | Sisa Anggaran Kat A $\downarrow$, Kat B $\uparrow$ |

---

## 4. Evaluasi Temuan & Rekomendasi Prioritas

### 1. Duplikasi Menu Navigasi pada Halaman `Lainnya`
- **Temuan**: Menu list di halaman `Lainnya` (`#more`) saat ini masih mencantumkan *Kategori & Anggaran* (`goPage('budget')`) dan *Simpanan & Dana Dijaga* (`goPage('accounts')`). Padahal kedua menu ini sudah menjadi tombol menu utama di topbar.
- **Rekomendasi**: Bersihkan item duplikat tersebut dari halaman `Lainnya` agar halaman `Lainnya` fokus murni sebagai wadah subhalaman sekunder (*Pihak Ketiga*, *Kewajiban*, *Belum Jelas*, *Pembaruan*, dan *Ekspor/Tema*).
- **Status Tindakan**: **`MOVE / REMOVE DUPLICATES`**.

### 2. Endpoint Server Legacy yang Tidak Dipanggil UI
- **Temuan**: Route `GET /api/pass_through`, `GET /api/statement`, `POST /api/debts/link_upcoming`, dan `POST /api/debts/unlink_upcoming` ada di `money_tracks_server.py` namun UI sudah sepenuhnya menggunakan `/api/debts` dan `/api/protected_allocation/link`.
- **Rekomendasi**: Pertahankan handler untuk backwards-compatibility atau beri dokumentasi *legacy internal endpoint*.
- **Status Tindakan**: **`KEEP (COMPATIBILITY)`**.

### 3. Redundansi Tautan Alokasi Dana Dijaga
- **Temuan**: Terdapat modal kecil `linkAllocModal` yang dapat digabungkan langsung dengan alur pembuatan alokasi di `accounts.js`.
- **Rekomendasi**: Sederhanakan UI pembuatan alokasi agar dropdown "Tautkan ke Tagihan" tersedia langsung di form alokasi utama.
- **Status Tindakan**: **`MERGE`**.

---

## 5. Matriks Klasifikasi Status Komponen

| Komponen | Status | Alasan & Rencana Tindak Lanjut |
|---|:---:|---|
| **Beranda (`home`)** | **`KEEP`** | Esensial, responsif, KPI hero terbukti cocok matematis. |
| **Transaksi (`transactions`)** | **`KEEP`** | Inti sistem, wizard 3 langkah terbukti stabil. |
| **Anggaran (`budget`)** | **`KEEP`** | Berfungsi penuh untuk penetapan batas kategori dan saran rollover. |
| **Rekening (`accounts`)** | **`KEEP`** | Menangani saldo fisik, rekonsiliasi, dan Dana Dijaga. |
| **Laporan (`reports`)** | **`KEEP`** | Modul analisis read-only dan cetak laporan. |
| **Kewajiban (`upcoming`)** | **`KEEP`** | Mencegah double deduction komitmen bulanan. |
| **Pihak Ketiga (`thirdparty`)** | **`KEEP`** | Mengisolasi Titipan, Piutang, dan Utang dari belanja pribadi. |
| **Review Queue (`provisional`)** | **`KEEP`** | Menampung transaksi ambigu agar tidak mencemari angka. |
| **Pembaruan (`updates`)** | **`KEEP`** | Menjaga kemampuan update aplikasi offline-first. |
| **Lainnya (`more`)** | **`IMPROVE`** | Hapus duplikasi shortcut `budget` dan `accounts` yang sudah ada di top navigation. |
| **Asisten AI** | **`KEEP (ISOLATED)`** | Terisolasi penuh dan aman tanpa memengaruhi logika angka finansial. |
