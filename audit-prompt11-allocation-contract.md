# Kontrak Arsitektur Alokasi Dana & Tujuan Keuangan — Money Tracks V12

Dokumen ini menetapkan spesifikasi desain, kontrak domain, formula matematika, skema database, dan matriks pengujian untuk menggantikan sistem lama `protected_amount`/“Dana Dijaga” pada Prompt 11b.

---

## 1. Akar Defect Sistem Lama

1. **Coupling Lokasi Fisik Rekening vs Nilai Proteksi**:
   Pada sistem lama (Prompt 0–10), nilai perlindungan dana dilekatkan secara fisik pada kolom `accounts.protected_amount` di rekening kas tertentu (`BCA Poket: Tabungan = Rp2.500.000`). Hal ini menciptakan bias lokasi fisik: ketika terjadi transfer internal antar-rekening milik sendiri (Owned), sistem menganggap saldo tabungan berkurang atau perlu dibatasi `min(current_balance, protected_amount)`.
2. **Pencampuran Tiga Domain Finansial yang Berbeda**:
   Sistem lama mencampuradukkan:
   - Cadangan darurat tak terduga (*Emergency Reserve*),
   - Rencana belanja masa depan (*Financial Goals*),
   - Kewajiban tagihan terjadwal (*Scheduled Commitments*),
   ke dalam satu angka tunggal kasar (`Rp2.500.000 + Rp650.000`), sehingga rencana yang masih tentatif (seperti tiket bus PKL Rp650.000) memotong Dana Tersedia secara prematur.
3. **Double Counting dan Disonansi Safe-to-Spend**:
   Alokasi yang tercatat di `protected_allocations` dan tagihan yang tercatat di `upcoming` berisiko memotong Safe-to-Spend dua kali jika tidak terhubung secara atomik.

---

## 2. Keputusan Model Domain

Sistem baru memisahkan 4 pilar domain secara tegas:

```
[ ASET LIKUID (Rekening) ] = BCA Main, Poket Tabungan, Poket Iuran, ShopeePay, GoPay, Cash
         |
         +---> [ CADANGAN (Reserves) ] ---------> Dana Darurat (Emergency Fund)
         |
         +---> [ TUJUAN (Goals) ] --------------> Rencana Spesifik (Pernikahan, dsb.)
         |
         +---> [ KOMITMEN (Commitments) ] ------> Confirmed vs Tentative (reserve_now)
```

1. **Rekening (Physical Accounts)**:
   - Tempat fisik kas berada (`BCA Main`, `ShopeePay`, dll.).
   - Murni mencatat `current_balance` aset likuid.
   - Tidak memiliki atribut proteksi alokasi.
2. **Cadangan (Reserves)**:
   - Dana pengaman untuk kebutuhan darurat tak terduga.
   - Memiliki nilai dana riil yang sudah disisihkan (`allocated_amount`).
3. **Tujuan Keuangan (Financial Goals)**:
   - Rencana masa depan dengan `target_amount` dan `allocated_amount`.
   - **Invarian**: `target_amount` (pagu impian) **TIDAK** memotong Dana Tersedia. Hanya `allocated_amount` (uang riil yang sudah disisihkan) yang memotong Dana Tersedia.
4. **Komitmen (Upcoming Obligations)**:
   - Tagihan/kewajiban dengan status: `Confirmed` (pasti), `Tentative` (kemungkinan), `Paid` (lunas), `Cancelled`.
   - Status `Tentative` memiliki flag `reserve_now`:
     - `reserve_now = 0`: Rencana tentatif yang tidak memotong Dana Tersedia.
     - `reserve_now = 1`: Rencana tentatif yang sengaja dicadangkan dan memotong Dana Tersedia.

---

## 3. Skema Minimum yang Direkomendasikan

Tabel `protected_allocations` dievolusi/dimigrasikan menjadi tabel terpadu `allocation_goals`, dan tabel `upcoming` diperkaya:

### A. Tabel `allocation_goals` (Cadangan & Tujuan Keuangan)
```sql
CREATE TABLE IF NOT EXISTS allocation_goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('Emergency', 'Goal', 'General')),
    target_amount REAL NOT NULL DEFAULT 0.0,
    allocated_amount REAL NOT NULL DEFAULT 0.0,
    target_date TEXT,
    priority INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'Active' CHECK(status IN ('Active', 'Achieved', 'Released', 'Cancelled')),
    preferred_account TEXT,
    notes TEXT DEFAULT '',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### B. Penyempurnaan Kolom Tabel `upcoming`
```sql
-- Tambahan kolom pada tabel upcoming (jika belum ada):
-- status: 'Upcoming' (diperlakukan sebagai Confirmed), 'Tentative', 'Paid', 'Cancelled'
-- reserve_now: INTEGER NOT NULL DEFAULT 0 (1 = potong STS, 0 = abaikan dari STS)
-- linked_goal_id: INTEGER REFERENCES allocation_goals(id)
```

---

## 4. Formula Final Dana Tersedia (Safe-to-Spend)

$$\\text{ReservedTotal} = \\sum_{\\text{Active}} \\text{allocated\\_amount}(\\text{Emergency}) + \\sum_{\\text{Active}} \\text{allocated\\_amount}(\\text{Goals})$$

$$\\text{EffectiveConfirmedCommitments} = \\sum_{u \\in \\text{Confirmed}} \\max(0, u.\\text{amount} - \\text{AlokasiTerkait}(u))$$

$$\\text{TentativeReserved} = \\sum_{u \\in \\text{Tentative},\\, u.\\text{reserve\\_now}=1} u.\\text{amount}$$

$$\\text{DanaTersedia} = \\text{TotalAsetLikuid} - \\text{TitipanAktif} - \\text{ReservedTotal} - \\text{EffectiveConfirmedCommitments} - \\text{TentativeReserved} - \\text{EstimasiPending}$$

### Prinsip Keuangan:
1. **Zero Double Deduction**: Komitmen yang telah terhubung ke alokasi dana tidak dipotong dua kali.
2. **Netralitas Transfer**: Transfer internal antar-rekening `Owned` memiliki $\\Delta \\text{TotalAsetLikuid} = 0$, $\\Delta \\text{ReservedTotal} = 0$, sehingga $\\Delta \\text{DanaTersedia} = 0$.
3. **Isolasi Piutang**: Piutang adalah aset non-likuid dan tidak menambah Dana Tersedia.
4. **Funding Invariant**: Menambah `allocated_amount` sebesar $X$ menurunkan Dana Tersedia tepat $X$; melakukan `Release` alokasi sebesar $X$ menaikkan Dana Tersedia tepat $X$.

---

## 5. State Transition Alokasi

```
[ DRAFT / TARGET ] ---> target_amount ditetapkan, allocated_amount = 0 (STS tidak berubah)
        |
        v
  [ FUNDED ] ---------> allocated_amount += X (STS berkurang X)
        |
        +---> [ RELEASED ] ---> allocated_amount -= X, status='Released' (STS bertambah X)
        |
        +---> [ SPENT ] -------> Mutasi Expense dicatat, allocated_amount -= X (STS konstan)
        |
        +---> [ ACHIEVED ] ----> target_amount tercapai & tetap dialokasikan
```

---

## 6. Rencana Migrasi Data (Eksekusi pada Prompt 11b)

1. **Pembersihan Kolom Fisik**:
   - Set `accounts.protected_amount = 0.0` dan `accounts.protected = 0` untuk semua akun.
2. **Migrasi Data Alokasi Target**:
   - `allocation_goals` Baris 1: `name = 'Dana Darurat'`, `kind = 'Emergency'`, `target_amount = 500000.0`, `allocated_amount = 500000.0`, `status = 'Active'`.
3. **Penyelarasan Komitmen (`upcoming`)**:
   - ID 1 (IOM): `amount = 120000.0`, `status = 'Upcoming'` (Confirmed).
   - ID 2 (by.U / Paket Data): `amount = 70000.0`, `status = 'Upcoming'` (Confirmed).
   - ID 3 (Tiket Bus PKL): `amount = 650000.0`, `status = 'Tentative'`, `reserve_now = 0` (tidak mengurangi Dana Tersedia).
   - ID Baru (Langganan AI): `amount = 60000.0`, `status = 'Upcoming'` (Confirmed).
4. **Hasil Baseline Target**:
   - Total Aset Likuid: Rp2.379.368,80
   - Dana Darurat Teralokasi: Rp500.000,00
   - Komitmen Confirmed (120k + 70k + 60k): Rp250.000,00
   - Komitmen Tentative (Bus PKL 650k, reserve_now=0): Rp0,00
   - **Dana Tersedia Baru**:
     $$2.379.368,80 - 0 - 500.000 - 250.000 - 0 = \\text{Rp}1.629.368,80$$
   - Status Kondisi: **Terkendali** (Bukan Defisit semu).

---

## 7. Matriks Pengujian Penerimaan (Acceptance Test Matrix — Prompt 11b)

| No | Kasus Uji | Skenario / Aksi | Ekspektasi Invarian |
| :--- | :--- | :--- | :--- |
| 1 | `test_internal_transfer_neutrality` | Transfer Rp500k BCA Main -> ShopeePay | Aset Likuid, ReservedTotal, dan STS konstan |
| 2 | `test_target_creation_without_funding` | Buat Goal target Rp10.000.000, allocated=0 | STS tidak berkurang sama sekali |
| 3 | `test_funding_goal_decreases_sts` | Alokasikan Rp100.000 ke Goal | STS berkurang tepat Rp100.000 |
| 4 | `test_release_allocation_increases_sts` | Lepas alokasi Rp100.000 | STS bertambah tepat Rp100.000 |
| 5 | `test_confirmed_commitment_deduction` | Tambah confirmed upcoming Rp60.000 | Effective commitments naik 60k, STS turun 60k |
| 6 | `test_tentative_reserve_zero` | Tambah tentative upcoming Rp650k (reserve_now=0) | STS tidak terpotong |
| 7 | `test_tentative_reserve_active` | Ubah tentative upcoming ke reserve_now=1 | STS terpotong tepat Rp650.000 |
| 8 | `test_covered_commitment_payment` | Bayar upcoming yang terdanai alokasi | STS terbukti konstan saat kas terpotong |
| 9 | `test_receivable_isolation` | Piutang Rp750.000 tercatat di ledger | Total Aset Likuid & STS tidak bertambah |
| 10 | `test_non_negative_allocation` | Alokasi negatif atau melebihi batas ditolak | `ValueError` dibangkitkan |
| 11 | `test_atomic_rollback_on_failure` | Simulasi error saat mutasi alokasi | DB kembali 100% ke kondisi awal |
| 12 | `test_temporary_db_isolation` | Seluruh pengujian berjalan pada temporary copy | `money_tracks.db` produksi 0 mutasi |

---

## 8. Manajemen Risiko & Prosedur Rollback

1. **Risiko Split-Brain Nilai**:
   - Dicegah dengan menghapus seluruh pembacaan `accounts.protected_amount` dan mengalihkan 100% pembacaan ke `allocation_goals`.
2. **Kompatibilitas Backward**:
   - `services.py` membungkus fungsi `balance_snapshot` dan `dashboard` dengan signature yang kompatibel tanpa merusak endpoint lama.
3. **Prosedur Rollback**:
   - Backup kanonikal dibuat otomatis sebelum migrasi skema dieksekusi.
   - Skrip migrasi dirancang reversibel (dapat mengembalikan struktur tabel lama jika terjadi kegagalan).
