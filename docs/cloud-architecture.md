# Arsitektur Cloud AturUang — Cloudflare Worker & D1 Shadow

## 1. Ikhtisar Arsitektur
AturUang menggunakan arsitektur hybrid terkontrol selama masa transisi Prompt 13A–13B:
- **Sumber Kebenaran Utama (Single Source of Truth)**: SQLite lokal (`money_tracks.db`) yang dikelola oleh backend Python (`money_tracks_server.py`).
- **Shadow Database**: Cloudflare D1 (`aturuang-db`) diakses melalui Cloudflare Worker (`aturuang-api`).
- **Mode Operasi Cloud**: `MODE=shadow` (Read-Only). Semua request mutasi tulis (`POST`, `PUT`, `DELETE`, `PATCH`) ditolak secara absolut dengan HTTP `503 SHADOW_READ_ONLY` untuk mencegah inkonsistensi atau *dual-write* yang tidak sah.

## 2. Threat Model & Proteksi Keamanan
1. **Otentikasi Staging**: Seluruh endpoint selain `/health` dilindungi oleh `Authorization: Bearer <STAGING_ADMIN_TOKEN>` dengan verifikasi *constant-time comparison* untuk mencegah *timing attacks*.
2. **Tanpa Kebocoran Secret**: Token rahasia, kunci D1, dan credential tidak pernah di-hardcode ke Git dan hanya dikonfigurasi melalui Cloudflare Secrets / Wrangler CLI.
3. **Privasi Data Finansial**: Error server tidak menampilkan *stack traces* atau informasi internal database kepada klien.
4. **Proteksi Injeksi SQL & Path Traversal**: Seluruh query D1 menggunakan *parameterized binding* (`stmt.bind(...)`) tanpa interpolasi string mentah.
5. **Keamanan Header**: Dilengkapi dengan `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, dan `Referrer-Policy: strict-origin-when-cross-origin`.

## 3. Runbook Migrasi & Sinkronisasi Shadow D1
### Langkah Ekspor & Migrasi:
1. Pastikan database SQLite lokal bersih dan valid:
   ```bash
   python -c "import sqlite3; con=sqlite3.connect('file:money_tracks.db?mode=ro', uri=True); print(con.execute('PRAGMA integrity_check').fetchone()[0])"
   ```
2. Jalankan skrip ekspor deterministik:
   ```bash
   python cloud/worker/scripts/export_sqlite_to_d1.py
   ```
   Skrip akan menghasilkan `cloud/worker/scripts/seed_shadow_d1.sql` yang terurut berdasarkan relasi Foreign Key dan bersifat idempoten (`INSERT OR IGNORE`).
3. Terapkan migrasi skema dan seed ke Cloudflare D1:
   ```bash
   npx wrangler d1 migrations apply aturuang-db --remote
   npx wrangler d1 execute aturuang-db --remote --file=./cloud/worker/scripts/seed_shadow_d1.sql
   ```
4. Verifikasi status dan parity melalui endpoint `/health` dan `/api/dashboard`.

## 4. Rollback Plan
Jika terjadi anomali data atau kegagalan D1:
1. SQLite lokal tetap 100% utuh karena seluruh proses ekspor hanya membaca dengan URI `mode=ro`.
2. Worker dapat segera dinonaktifkan atau dipertahankan dalam `MODE=shadow` tanpa memengaruhi operasional aplikasi lokal.
3. Database D1 dapat di-reset kapan saja dengan menjalankan migrasi ulang dari paket seed `seed_shadow_d1.sql`.
