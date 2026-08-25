# Panduan Setup Gmail Relay (Google Apps Script)

Integrasi ini bertugas menangkap notifikasi transaksi dari Gmail secara 24/7 dan mengirimkannya ke endpoint Worker AturUang `POST /api/ingest/gmail` dengan tanda tangan HMAC-SHA256.

## Langkah Konfigurasi
1. Buka [Google Apps Script](https://script.google.com/) dan buat project baru berjudul `AturUang-Gmail-Relay`.
2. Salin isi berkas `Code.gs` dan `appsscript.json` ke editor project.
3. Buka **Project Settings** > **Script Properties**, tambahkan dua property:
   - `WORKER_URL`: URL Cloudflare Worker Anda (misal `https://aturuang-api.example.workers.dev`).
   - `GMAIL_RELAY_SECRET`: Kunci rahasia HMAC yang sama dengan rahasia yang disimpan di Wrangler Secrets Worker (`GMAIL_RELAY_SECRET`).
4. Buka menu **Triggers** (ikon jam di panel kiri) > **Add Trigger**:
   - Function to run: `relayGmailTransactions`
   - Event source: `Time-driven`
   - Type of based timer: `Minutes timer`
   - Select minute interval: `Every 5 minutes` or `Every 10 minutes`.
5. Simpan dan izinkan izin akses Google OAuth (hanya membaca Gmail `gmail.readonly` dan panggilan jaringan `script.external_request`).
