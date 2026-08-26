# AturUang — Panduan Setup Gmail Relay & Historical Backfill

## 1. Persiapan Proyek Apps Script
1. Buka [Google Apps Script](https://script.google.com/) menggunakan akun Google Anda.
2. Buat proyek baru dengan nama **AturUang Gmail Connector**.
3. Buka file `Code.gs`, hapus kode default, dan salin seluruh isi dari `integrations/gmail-apps-script/Code.gs`.

## 2. Konfigurasi Script Properties
Di panel kiri editor Apps Script:
1. Klik **Project Settings** (ikon roda gigi ⚙️).
2. Gulir ke bawah ke bagian **Script Properties**, klik **Add script property**:
   - `WORKER_URL`: `https://aturuang-api.allanfikrimahardika.workers.dev`
   - `GMAIL_RELAY_SECRET`: Masukkan secret HMAC yang Anda konfigurasi di Cloudflare Worker secret.
3. Klik **Save script properties**.

## 3. Menjalankan Historical Backfill (2025-01-01 s.d. Sekarang)
1. Pilih fungsi `backfillGmailTransactions` di dropdown fungsi editor.
2. Klik tombol **Run** (Jalankan).
3. Berikan otorisasi akses baca Gmail saat pertama kali diminta oleh Google.
4. Apps Script akan memproses email secara bulanan (bounded windows) mulai Januari 2025 dengan strict exact senders:
   - Checkpoint bulanan otomatis disimpan di `GMAIL_BACKFILL_CHECKPOINT`.
   - Jika terjadi timeout execution (batas 6 menit Apps Script), jalankan kembali `backfillGmailTransactions` untuk melanjutkan dari checkpoint terakhir secara idempoten.
5. Untuk memeriksa status checkpoint, jalankan fungsi `getBackfillStatus()`.
6. Untuk mereset checkpoint dari awal (2025-01), jalankan fungsi `resetBackfillCheckpoint()`.

## 4. Mengaktifkan Live Relay Berkala (Opsional)
1. Buka menu **Triggers** (ikon jam ⏰) di panel kiri.
2. Klik **Add Trigger**:
   - Function to run: `relayGmailTransactions`
   - Event source: `Time-driven`
   - Type: `Minutes timer` (setiap 10 atau 15 menit).
3. Simpan trigger.

> **Catatan Keamanan:**
> Worker AturUang berada dalam `MODE=shadow` dan hanya memproses bukti transaksi ke staging queue tanpa mengubah financial ledger produksi.
