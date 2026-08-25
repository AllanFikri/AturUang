# Aturan Kerja — Money Tracks V12

Ini proyek BROWNFIELD yang sudah berjalan. Vanilla JS, tanpa build step,
backend Python terpisah, dan database SQLite. Dokumentasi ada di root repository:
`money-tracks-spec.md` dan `audit-tahap-0.md` — baca sebelum mengubah apa pun
yang menyentuh angka.

## Yang tidak boleh dilakukan

- Jangan membangun ulang aplikasi.
- Jangan mengganti stack. Tidak ada React, Vue, TypeScript, bundler, atau
  package manager baru. Aplikasi harus tetap bisa dibuka langsung.
- Jangan merapikan kode yang tidak diminta. Tidak ada refactor spontan,
  tidak ada rename variabel, tidak ada perubahan format massal.
- Jangan menambah fitur yang tidak diminta.
- Jangan menyentuh database pengguna tanpa skrip migrasi yang bisa dibatalkan.

## Yang selalu berlaku

- Bahasa UI Indonesia. Ikuti kamus istilah di `money-tracks-spec.md` §5.
- Tanggal: pakai `getLocalDateString()` / `getLocalTimeString()` yang sudah ada.
  Jangan pernah `toISOString()` untuk tanggal yang dilihat pengguna.
- Nominal: SQLite REAL dipertahankan, dibulatkan 2 desimal pada setiap penulisan
  dan `SUM()`. Perbandingan kesetaraan memakai batas toleransi `abs(diff) < 0.005`.
  (Migrasi skema ke integer sen ditunda untuk evaluasi masa depan jika skala berubah).
- Satu angka dihitung di satu tempat. Perhitungan uang tidak berada di dalam
  kode render antarmuka.
- Semua nilai yang berasal dari model AI wajib lewat `esc()` sebelum masuk
  `innerHTML`.
- Satu commit per temuan. Tulis kode temuannya di pesan commit,
  contoh: "fix(K5): pakai waktu lokal WIB di markPaid".

## Kalau ragu

Kalau spesifikasi bertentangan dengan kode yang ada, laporkan konfliknya dan
tunggu jawaban pengguna. Jangan menebak.

## Mode Hemat Token

- Jangan mencari ulang root, struktur repo, atau lokasi dokumentasi.
  File utama sudah diketahui berada di root.
- Baca `audit-tahap-0.md` bagian status dan bagian yang relevan saja.
  Jangan membaca ulang seluruh spec setiap sesi.
- Gunakan `rg` dan rentang baris spesifik. Jangan `list` atau scan semua file
  jika nama file/fungsi sudah disebut di prompt.
- Jangan menampilkan seluruh command log, seluruh file, atau full diff.
- Saat selesai, laporan maksimal 400 kata dengan format:
  1. status selesai/gagal;
  2. file yang berubah;
  3. ringkasan perubahan;
  4. test PASS/FAIL;
  5. commit hash;
  6. blocker yang masih ada.
- Tampilkan diff lengkap hanya jika diminta atau jika ada konflik/risiko.
- Saat pengembangan, jalankan test yang relevan dahulu. Jalankan seluruh
  suite hanya satu kali setelah perubahan final.
- Test lama yang seluruhnya hijau cukup dilaporkan:
  "Regression suite: PASS (nama suite)".
  Jangan salin ulang setiap baris test.
- Jangan mengulang penjelasan masalah yang sudah tertulis di audit.
- Jangan membuat ulang atau menduplikasi dokumentasi.
- Berhenti setelah scope prompt selesai.
- Penghematan token tidak boleh mengurangi backup, migrasi reversibel,
  atomisitas, sanitasi input, atau pengujian perhitungan uang.

## Format Laporan Antigravity (Plain Text Saja)

Semua laporan akhir wajib menggunakan format plain text saja.

Jangan gunakan:
1. Heading markdown yang diawali tanda pagar (#).
2. Tanda cetak tebal (**) atau miring (*).
3. Tabel markdown.
4. Code fences (backticks).
5. Blockquotes (>).
6. Emoji.
7. Tautan file://.
8. Riwayat perintah per-langkah.
9. Raw diffs.
10. Pengulangan informasi baseline yang tidak berubah.

Gunakan nomor urut sederhana dan kalimat normal. Laporan tetap boleh menyajikan detail teknis penting tanpa mengorbankan bukti demi memperpendek jawaban.
