# Phase 3 GoPay E-Statement Forward Design Gate

**Date:** 2026-08-29  
**Status:** FROZEN  
**Workspace:** `AturUang-ingestion-v1`  
**Base Commit:** `13b8a09f892f04c69469cad9f36ebc55fea21973`  
**Review Branch:** `review/gopay-v1`  

---

## 1. Executive Summary and Architecture Decision

This document establishes the frozen specification for Phase 3 ingestion of GoPay electronic monthly statement (e-statement) artifacts. The GoPay statement corpus consists of 7 consecutive monthly periods (2026-01 through 2026-07).

### Controller Hard Gate Decisions:
- **GoPay Statement Adapter (`gopay-estatement-v1`):** READY FOR IMPLEMENTATION. 7/7 documents verified; 21 total transaction blocks (19 IDR cash movements + 2 GoPay Coins loyalty events); 7/7 IDR source summary totals reconcile with transaction cash sums; 7/7 documents expose explicit registered mobile phone number as provider-native wallet key; 2 zero-activity statements (2026-02, 2026-03) verified; 0 balance snapshots; 0 account period summaries.
- **Loyalty Points Separation Rule:** GoPay Coins are loyalty points, not IDR cash. Only the 19 IDR cash transactions emit `EventRole.CASH_MOVEMENT`. GoPay Coins transactions (e.g., reward cashback, promo coins) are parsed internally for structure validation and coins summary reconciliation, but emit ZERO normalized financial events.
- **Status Mapping Rule:** Transaction status in GoPay statements represents implicit historical posting. Per controller specification, normalized transaction status is mapped to `SourceEventStatus.UNKNOWN`.
- **No Running Balance:** GoPay e-statements omit opening balance, closing balance, and per-row running balance. `balance_after` is set to `None`.

---

## 2. Frozen Provider Contract

- **Adapter ID:** `gopay-estatement-v1`
- **Source Registry ID:** `gopay_statement`
- **Template ID:** `gopay_estatement_v1`
- **Parser Version:** `parser-v1`
- **Source Channel:** `PDF`
- **Template Fingerprint:** `6dcc7e3f14d52f0dc3452513fd8e7fcd8fe0fda2f725f3ae4eba0ae5ea477d48`
- **Required Structural Markers:**
  - `E-statement`
  - `Periode transaksi`
  - `Tanggal Transaksi`
  - `ID transaksi`
  - `Metode pembayaran`
  - `Jumlah`

---

## 3. Corpus Facts and Pre-Implementation Validation

- **Total SHA-Unique Documents:** 7
- **Periods Covered:** 2026-01 to 2026-07 (7 consecutive monthly periods; all `CLOSED`)
- **Page Count Range:** 1 page (all 7 documents are single page)
- **Text Layer:** 7/7 ready (100%)
- **Encryption:** 0/7 encrypted (unencrypted standard PDF)
- **Layout Families:** 1 stable layout family
- **Trusted Transaction Blocks:** 21 total (19 IDR cash transactions + 2 GoPay Coins loyalty events)
- **Per-Document Transaction Range:** 0 to 7 transactions
- **Zero-Activity Documents:** 2 documents (2026-02, 2026-03)
- **Source Summary Reconciliation:** 7/7 checkable and 7/7 passing (`Total pemasukan` and `Total pengeluaran` match cash transaction sums with Coins splits)
- **Identity Anchoring:**
  - Explicit header anchor: Line 2 immediately following Customer Name on Line 1 and preceding Email on Line 3.
  - Matches provider-native registered mobile number format `^\+62\d{8,13}$`.
  - 7/7 documents emit `ACCOUNT_OBSERVATION` with observed provider account key.
  - Ambiguity Rule: Multiple distinct anchored wallet numbers fail closed with `GOPAY_ACCOUNT_IDENTITY_AMBIGUOUS`.

---

## 4. Evidence Roles and Authority

| Evidence Role | Authority | Target Event Count | Notes |
| :--- | :--- | :--- | :--- |
| `CASH_MOVEMENT` | **YES** | 19 events | Emitted ONLY for explicit IDR transactions |
| `SOURCE_SUMMARY` | **YES** | 7 events | IDR `incoming_total` and `outgoing_total`; `opening_balance=None`, `closing_balance=None` |
| `ACCOUNT_OBSERVATION` | **YES** | 7 events | 1 per document anchored on registered mobile wallet key |
| `BALANCE_SNAPSHOT` | **NO** | 0 events | GoPay statements do not provide balance snapshots |
| `ACCOUNT_PERIOD_SUMMARY` | **NO** | 0 events | GoPay statements do not provide balance continuity |
| `INVESTMENT_TRADE` | **NO** | 0 events | |

---

## 5. Parser Grammar and Implementation Blueprint

### 5.1 Document Header and Identity Extraction
- Header Line 0: `E-statement Halaman <N> dari <M> Periode transaksi : <Start> - <End>`.
- Header Line 1: `<Customer Name>`.
- Header Line 2: `<Provider Native Wallet Key>` (e.g., `+628...`).
- Header Line 3: `<Customer Email>`.
- Period Start & End: Normalized to ISO `YYYY-MM-DD`.
- Wallet Identity Validation: Collect all structurally anchored MSISDN candidates matching `^\+62\d{8,13}$`.
  - 0 candidates: omit `ACCOUNT_OBSERVATION`.
  - 1 candidate: valid wallet key.
  - >1 distinct candidates: fail closed with `GOPAY_ACCOUNT_IDENTITY_AMBIGUOUS`.

### 5.2 Summary Section Extraction
- `Total Coins didapatkan`: parsed integer (internal validation).
- `Total Coins dipakai`: parsed integer (internal validation).
- `Total pemasukan`: parsed IDR Decimal (`incoming_total`).
- `Total pengeluaran`: parsed IDR Decimal (`outgoing_total`).
- `opening_balance` = `None`, `closing_balance` = `None`.

### 5.3 Transaction Row Extraction
- Table starts after header row: `Tanggal Transaksi ID transaksi Metode pembayaran Jumlah`.
- Multi-line transaction block structure:
  - Line 1: `DD/MM/YYYY`
  - Line 2: `HH:MM`
  - Line 3: Description + Transaction Reference prefix
  - Line 4: Provider Order/Transaction ID
  - Line 5 (and optional Line 6): Payment Method(s) + Amount (`-Rp...` / `Rp...` / Coins)
- Amount & Direction:
  - `-Rp...` -> `EventDirection.OUTFLOW`, `direction_raw="KELUAR"`
  - `Rp...` -> `EventDirection.INFLOW`, `direction_raw="MASUK"`
  - `GoPay Coins ...` -> Coins loyalty event (excluded from financial CASH_MOVEMENT events)
- Split Payment Handling: When a transaction is paid with both `GoPay Saldo` and `GoPay Coins`, the cash amount reflects the net IDR amount deducted from GoPay Saldo.
- Status: `SourceEventStatus.UNKNOWN` (implicit historical posting).
- `occurred_at`: ISO `YYYY-MM-DDTHH:MM:00`.
- `balance_after`: `None`.

### 5.4 Zero-Activity Statement Behavior
- Zero transaction rows under table header.
- `Total pemasukan == 0` and `Total pengeluaran == 0`.
- Emits 1 `SOURCE_SUMMARY` envelope.
- Emits 1 `ACCOUNT_OBSERVATION` envelope (if explicit wallet identity exists).
- Emits 0 `CASH_MOVEMENT` envelopes.
- Parse status: `COMPLETED`.

---

## 6. Private Replay Expected Counts

- **Total Documents Replayed:** 7
- **Parse Status:** 7/7 `COMPLETED` (0 `FAILED`)
- **`CASH_MOVEMENT` Envelopes:** 19
- **`SOURCE_SUMMARY` Envelopes:** 7
- **`ACCOUNT_OBSERVATION` Envelopes:** 7
- **`BALANCE_SNAPSHOT` Envelopes:** 0
- **`ACCOUNT_PERIOD_SUMMARY` Envelopes:** 0
- **`INVESTMENT_TRADE` Envelopes:** 0
- **Total Normalized Event Envelopes:** 33
- **Zero-Activity Documents:** 2
- **IDR Summary Equation Reconciled:** 7/7
- **Coins Rows Parsed Internally:** 2
- **Coins Financial Events Emitted:** 0

---

## 7. Safe Diagnostics and Privacy Controls

- All private provider identifiers, customer names, raw descriptions, and phone numbers are marked `repr=False` in data classes.
- Diagnostics use deterministic safe error codes (e.g., `GOPAY_PDF_ENCRYPTED_UNSUPPORTED`, `GOPAY_STRUCTURE_TRUNCATED`, `GOPAY_PERIOD_AMBIGUOUS`, `GOPAY_SUMMARY_MISMATCH`, `GOPAY_ACCOUNT_IDENTITY_AMBIGUOUS`, `GOPAY_ROW_AMOUNT_INVALID`).
- Natural keys and row fingerprints use deterministic SHA-256 digests.

---

## 8. Exact Focused Test Plan (30 Tests)

The test suite `tests/test_universal_ingestion_phase3_gopay.py` implements exactly 30 `unittest` methods:
1. `test_01_descriptor_and_constants`
2. `test_02_exact_template_fingerprint`
3. `test_03_authority_mismatch_source_registry`
4. `test_04_authority_mismatch_template_id`
5. `test_05_authority_mismatch_parser_version`
6. `test_06_authority_mismatch_source_channel`
7. `test_07_binary_payload_required`
8. `test_08_encrypted_pdf_fail_closed`
9. `test_09_zero_page_pdf_fail_closed`
10. `test_10_active_identity_positive_anchoring`
11. `test_11_identity_negative_decoy_rejection`
12. `test_12_conflicting_identity_ambiguity_fail_closed`
13. `test_13_repeated_same_identity_accepted`
14. `test_14_no_filename_identity_authority`
15. `test_15_period_extraction_precision`
16. `test_16_datetime_extraction_precision`
17. `test_17_idr_inflow_transaction_direction_and_amount`
18. `test_18_idr_outflow_transaction_direction_and_amount`
19. `test_19_decimal_parsing_strictness`
20. `test_20_native_transaction_reference_extracted`
21. `test_21_payment_method_raw_extracted`
22. `test_22_wrapped_multiline_transaction_block`
23. `test_23_status_unknown_not_inferred_posted`
24. `test_24_coins_row_excluded_from_cash_movement`
25. `test_25_idr_source_summary_extraction`
26. `test_26_idr_summary_reconciliation_success`
27. `test_27_idr_summary_mismatch_fail_closed`
28. `test_28_zero_activity_statement_parsing`
29. `test_29_privacy_safe_repr_and_diagnostics`
30. `test_30_exact_roles_and_no_balance_or_period_summary`
