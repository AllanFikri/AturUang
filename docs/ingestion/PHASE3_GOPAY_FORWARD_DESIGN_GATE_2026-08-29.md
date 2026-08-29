# Phase 3 GoPay E-Statement Forward Design Gate

**Date:** 2026-08-29  
**Status:** REVISED & FROZEN  
**Workspace:** `AturUang-ingestion-v1`  
**Base Commit:** `13b8a09f892f04c69469cad9f36ebc55fea21973`  
**Review Branch:** `review/gopay-v1`  

---

## 1. Executive Summary and Architecture Decision

This document establishes the revised specification for Phase 3 ingestion of GoPay electronic monthly statement (e-statement) artifacts. The GoPay statement corpus consists of 7 consecutive monthly periods (2026-01 through 2026-07).

### Controller Hard Gate Decisions:
- **GoPay Statement Adapter (`gopay-estatement-v1`):** 7/7 documents verified; 21 total transaction blocks (19 IDR cash movements + 2 GoPay Coins loyalty events); 7 SOURCE_SUMMARY envelopes; 7 ACCOUNT_OBSERVATION envelopes; 33 total normalized events; 2 zero-activity statements (2026-02, 2026-03); 0 balance snapshots; 0 account period summaries.
- **Parse Outcomes Across Corpus:** `COMPLETED = 6`, `REVIEW_REQUIRED = 1`, `FAILED = 0`.
- **Split Payment Net Cash Policy:** GoPay supports mixed payments (`GoPay Saldo` + `GoPay Coins`), where 1 Coin = Rp1 redemption value. When a transaction is marked as mixed payment, the printed amount represents the gross purchase price. For documents with a uniquely resolvable split transaction, the net cash component (`gross_amount - total_coins_used`) is assigned as the `CASH_MOVEMENT` amount (the actual IDR deducted from GoPay Saldo). `PaymentComponentEvidence` records both the net cash component and the redeemed Coins value.
- **Loyalty Points Separation Rule:** GoPay Coins are loyalty points, not IDR cash. Only the 19 IDR cash transactions emit `EventRole.CASH_MOVEMENT`. GoPay Coins loyalty rows (cashback rewards, promo points) are parsed internally for structure and summary validation, emitting ZERO normalized financial events.
- **Incomplete Rowset / Review Policy:** For statements where visible transaction row sums do not match source summary totals due to unlisted external transactions (e.g., April 2026 where top-up is listed but an outflow row is absent), the adapter retains all trusted explicit evidence, preserves the source summary, emits no fabricated transactions, and returns `AdapterParseStatus.REVIEW_REQUIRED` with diagnostic `GOPAY_SOURCE_SUMMARY_ROWSET_MISMATCH`.
- **Status Mapping Rule:** Transaction status in GoPay statements represents implicit historical posting. Normalized transaction status is mapped to `SourceEventStatus.UNKNOWN`.
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
- **Strict IDR Summary Reconciled:** 6/7 documents strictly reconcile (`calc_inflow == incoming_total` and `calc_net_outflow == outgoing_total`)
- **Source-Level Rowset Inconsistency:** 1/7 documents (April 2026) returns `REVIEW_REQUIRED` due to unlisted outflow row
- **Identity Anchoring:**
  - Structural anchor: Header Line 2 immediately following Customer Name on Line 1 and preceding Email on Line 3.
  - Matches provider-native registered mobile number format `^\+62\d{8,13}$`.
  - 7/7 documents emit `ACCOUNT_OBSERVATION` with observed provider account key.
  - Ambiguity Rule: Multiple distinct anchored wallet numbers fail closed with `GOPAY_ACCOUNT_IDENTITY_AMBIGUOUS`.

---

## 4. Evidence Roles and Authority

| Evidence Role | Authority | Target Event Count | Notes |
| :--- | :--- | :--- | :--- |
| `CASH_MOVEMENT` | **YES** | 19 events | Emitted ONLY for explicit IDR cash transactions |
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
- Header Line 2: `<Provider Native Wallet Key>` (matches `^\+62\d{8,13}$`).
- Header Line 3: `<Customer Email>`.
- Period Start & End: Normalized to ISO `YYYY-MM-DD`.
- Structural Identity Validation: Only MSISDN at exact header position is authority. Phone-like numbers elsewhere are rejected as decoys.
  - 0 candidates: omit `ACCOUNT_OBSERVATION`.
  - 1 candidate: valid wallet key.
  - >1 distinct candidates: fail closed with `GOPAY_ACCOUNT_IDENTITY_AMBIGUOUS`.

### 5.2 Summary Section Extraction
- `Total Coins didapatkan`: parsed integer (internal validation).
- `Total Coins dipakai`: parsed integer (internal validation).
- `Total pemasukan`: parsed IDR Decimal (`incoming_total`).
- `Total pengeluaran`: parsed IDR Decimal (`outgoing_total`).
- `opening_balance` = `None`, `closing_balance` = `None`.

### 5.3 Split Payment & Net Cash Calculation
- Split Payment Detection: When payment methods include both `GoPay Saldo` and `GoPay Coins`.
- Deterministic Resolution (1 split row):
  - `cash_component = gross_amount - Decimal(total_coins_used)`
  - `CashMovementEvidence.amount = cash_component`
  - `PaymentComponentEvidence`:
    - Component 1: `GoPay Saldo`, amount = `cash_component`, currency = `IDR`
    - Component 2: `GoPay Coins`, amount = `Decimal(total_coins_used)`, currency = `IDR`
- Ambiguous Resolution (>1 split rows with aggregate coins):
  - Return `REVIEW_REQUIRED` with `GOPAY_SPLIT_PAYMENT_ALLOCATION_AMBIGUOUS`.

### 5.4 Transaction Row Extraction
- Table starts after header row: `Tanggal Transaksi ID transaksi Metode pembayaran Jumlah`.
- Date & Time:
  - Line 1: `DD/MM/YYYY` (validated with calendar limits)
  - Line 2: `HH:MM` (validated with 24-hour time limits)
  - Missing/invalid date/time -> fail closed with `GOPAY_ROW_DATETIME_INVALID`.
- Duplicate Provider Transaction ID:
  - Identical row evidence -> deduplicated (emitted once).
  - Conflicting row evidence -> fail closed with `GOPAY_DUPLICATE_TRANSACTION_ID_CONFLICT`.
- Amount & Direction:
  - `-Rp...` -> `EventDirection.OUTFLOW`, `direction_raw="KELUAR"`
  - `Rp...` -> `EventDirection.INFLOW`, `direction_raw="MASUK"`
  - `GoPay Coins ...` -> Coins loyalty event (excluded from financial CASH_MOVEMENT events)
- Status: `SourceEventStatus.UNKNOWN`.
- `occurred_at`: ISO `YYYY-MM-DDTHH:MM:00`.
- `balance_after`: `None`.

---

## 6. Private Replay Expected Counts

- **Total Documents Replayed:** 7
- **Parse Status:** `COMPLETED = 6`, `REVIEW_REQUIRED = 1`, `FAILED = 0`
- **`CASH_MOVEMENT` Envelopes:** 19
- **`SOURCE_SUMMARY` Envelopes:** 7
- **`ACCOUNT_OBSERVATION` Envelopes:** 7
- **`BALANCE_SNAPSHOT` Envelopes:** 0
- **`ACCOUNT_PERIOD_SUMMARY` Envelopes:** 0
- **`INVESTMENT_TRADE` Envelopes:** 0
- **Total Normalized Event Envelopes:** 33
- **Zero-Activity Documents:** 2
- **Strict IDR Summary Reconciled:** 6/7
- **Summary Rowset Mismatch:** 1
- **Coins Rows Parsed Internally:** 2
- **Coins Financial Events Emitted:** 0
- **Split Payment Rows Resolved:** 2
- **Ambiguous Split Payment Docs:** 0

---

## 7. Safe Diagnostics and Privacy Controls

- All private provider identifiers, customer names, raw descriptions, and phone numbers are marked `repr=False` in data classes.
- Diagnostics use deterministic safe error codes:
  - `GOPAY_PDF_ENCRYPTED_UNSUPPORTED`
  - `GOPAY_STRUCTURE_TRUNCATED`
  - `GOPAY_PERIOD_AMBIGUOUS`
  - `GOPAY_SUMMARY_MISMATCH`
  - `GOPAY_SOURCE_SUMMARY_ROWSET_MISMATCH`
  - `GOPAY_ACCOUNT_IDENTITY_AMBIGUOUS`
  - `GOPAY_ROW_DATETIME_INVALID`
  - `GOPAY_ROW_AMOUNT_INVALID`
  - `GOPAY_DUPLICATE_TRANSACTION_ID_CONFLICT`
  - `GOPAY_SPLIT_PAYMENT_ALLOCATION_AMBIGUOUS`
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
21. `test_21_payment_method_raw_and_components_extracted`
22. `test_22_wrapped_multiline_transaction_block`
23. `test_23_status_unknown_not_inferred_posted`
24. `test_24_coins_row_excluded_from_cash_movement`
25. `test_25_idr_source_summary_extraction`
26. `test_26_idr_summary_reconciliation_success`
27. `test_27_idr_summary_mismatch_fail_closed`
28. `test_28_zero_activity_statement_parsing`
29. `test_29_privacy_safe_repr_and_diagnostics`
30. `test_30_exact_roles_and_no_balance_or_period_summary`
