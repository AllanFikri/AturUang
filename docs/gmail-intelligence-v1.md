# AturUang — Gmail Transaction Intelligence v1 & Canonical Event Model

## 1. Arsitektur & Prinsip Keamanan
- **Strict Evidence-Backed Senders**: Hanya memproses email dari sender resmi bank & penyedia layanan terbukti:
  - Primary / Transactional: `bca@bca.co.id`, `noreply@jago.com`, `no-reply@flip.id`, `googleplay-noreply@google.com`, `noreply@byu.id`, `no-reply@mailer-esb.com`.
  - Secondary / Order-Lifecycle: `info@shopee.co.id`, `info@mail.shopee.co.id`, `noreply@cx.byu.id`.
  - Mengabaikan sender marketing (contoh: `no-reply@marketing.go-jek.com`) dan menolak seluruh sender yang tidak cocok (HTTP 403 `UNTRUSTED_GMAIL_SENDER`).
- **Privacy & Data Minimization**:
  - Zero raw email body persistence di D1/Git/log.
  - Minimal payload hanya menyimpan metadata non-PII (sender, detected_kind, confidence).
  - Masking nomor rekening dan detail sensitif.
- **Shadow Mode Only**:
  - Seluruh ingestion dan canonical events hanya tersimpan di staging queue D1.
  - Zero write / zero mutation pada production SQLite financial ledger.

## 2. Model Canonical Financial Event
Satu transaksi keuangan dapat dibuktikan oleh beberapa email (Multi-Evidence Graph):
- `canonical_financial_events`:
  - `event_id`, `occurred_at_wib`, `status`, `event_kind`, `financial_class`, `amount`, `fee_amount`, `source_account_alias`, `destination_account_alias`, `destination_owner_type`, `merchant_normalized`, `merchant_pan`, `confidence`.
- `canonical_event_evidence`:
  - Menghubungkan `canonical_event_id` dengan `raw_event_id` dan `candidate_id` dengan peran bukti (`PRIMARY_PAYMENT`, `SECONDARY_RECEIPT`, `INVOICE`, `LIFECYCLE_STATUS`, `INTERMEDIARY`).

## 3. Semantik Klasifikasi Finansial (Ownership First)
1. **BCA QRIS**:
   - Status Berhasil -> `MERCHANT_PAYMENT` -> `Expense` (Confidence 0.95).
   - Status Gagal -> `FAILED_ATTEMPT` -> `Ignore` (Tanpa kandidat ledger).
2. **BCA Tarik Tunai Tanpa Kartu (Cardless)**:
   - `CASH_WITHDRAWAL` -> `Internal Transfer` (`BCA Main` -> `Cash`), bukan Expense.
3. **BCA Poket**:
   - `ALLOCATION_MOVEMENT` -> `Internal Transfer` (`BCA Main` -> `BCA Poket: ...`), bukan Expense.
4. **BCA -> ShopeePay (VA 122...)**:
   - `TOPUP` -> `Internal Transfer` (`BCA Main` -> `ShopeePay`), bukan Expense.
5. **ShopeePay -> Jago (SELF)**:
   - `OWN_TRANSFER` -> `Internal Transfer` (`ShopeePay` -> `Jago Main`), bukan Income.
6. **Jago <-> RDN Stockbit**:
   - `INVESTMENT_MOVEMENT` -> `Internal Transfer`, bukan Expense/Income biasa.
7. **Flip Intermediary**:
   - `BCA -> Fliptech -> Penerima` diperlakukan sebagai 1 alur transfer dengan referensi Flip.
8. **ESB / Shopee Invoice**:
   - Berperan sebagai `SECONDARY_RECEIPT` / `INVOICE` yang memperkaya bukti debit bank tanpa menduplikasi pengeluaran.
9. **Google Play / by.U**:
   - Transaksi sukses -> `DIGITAL_PURCHASE` -> `Expense`.
   - Notifikasi kuota / masa aktif / pembatalan langganan -> `NON_TRANSACTION` -> `Ignore`.

## 4. Historical Audit (Gmail vs Ledger)
Script `tools/audit_gmail_vs_ledger.py` menjalankan audit read-only terhadap database lokal untuk mendeteksi:
- `CLASSIFICATION_CONFLICT` (misal Top-up / Tarik Tunai tercatat keliru sebagai Expense).
- `POSSIBLE_LEDGER_DUPLICATE` (pencatatan ganda debit bank + struk invoice).
- `MATCHED_EXACT` dan `MATCHED_LIKELY`.
- `LEDGER_EVENT_WITHOUT_EMAIL_EVIDENCE` (transaksi cash/manual yang sah).
