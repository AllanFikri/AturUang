# AturUang — Personal Finance Management

Aplikasi manajemen keuangan pribadi berbasis Vanilla JS, Python standard library, dan SQLite dengan fondasi Cloudflare Worker shadow D1.

## Struktur Direktori
- `aturuang/`: Backend Python terpusat (`server.py`, `db.py`, `services.py`, `statement.py`, `ingestion.py`) dan frontend Vanilla JS (`web/`).
- `tests/`: Seluruh suite pengujian otomatis (`tests/run_all.py`).
- `cloud/worker/`: Cloudflare Worker terisolasi (`aturuang-api`).
- `runtime/`: Database SQLite lokal (`money_tracks.db`) dan backup lokal (diabaikan Git).
- `docs/`: Dokumentasi arsitektur, audit, dan spesifikasi.
- `prompts/`: Panduan roadmap pengerjaan.

## Cara Menjalankan
```bash
# Menjalankan server aplikasi lokal (port 5050)
python launcher.py

# Menjalankan pengujian cepat (Quick Mode)
python tests/run_all.py quick

# Menjalankan seluruh test suite lengkap (Full Mode)
python tests/run_all.py full
```
