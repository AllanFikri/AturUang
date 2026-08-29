# Phase 3 Blu Dual-Adapter Forward Design Gate

**Date:** 2026-08-29  
**Status:** FROZEN  
**Workspace:** `AturUang-ingestion-v1`  
**Base Commit:** `f2bdee0c3414ea9d8de6115b465786a1894666a0`  
**Review Branch:** `review/blu-v1-clean`  

---

## 1. Executive Summary and Architecture Decision

This document establishes the frozen specification for Phase 3 ingestion of BCA Digital (blu) statement artifacts. The blu corpus consists of two distinct document families across 25 monthly periods (2024-08 through 2026-08):
1. **blu Account Mutation Statements** (`blu_mutation` / `blu_account_mutation_v1`): Cash movement transaction statements with running balances for the primary `bluAccount`.
2. **blu Portfolio Statements** (`blu_portfolio` / `blu_portfolio_v1`): High-level multi-pocket portfolio statements showing initial balances, income, expense, and ending balances across products (`bluAccount`, `bluSaving`, `bluGether`).

### Controller Hard Gate Decisions:
- **blu Mutation Adapter (`blu-account-mutation-v1`):** READY FOR IMPLEMENTATION. 25/25 documents verified; 211 trusted cash movement candidates; 25/25 summary equations reconcile; 18 active statements expose explicit numeric account numbers (`<provider-native-account-id>`); 7 zero-activity statements omit numeric account numbers and fail closed by omitting `ACCOUNT_OBSERVATION` while emitting structurally verified `SOURCE_SUMMARY`.
- **blu Portfolio Adapter (`blu-portfolio-v1`):** IDENTITY HARD GATE TRIGGERED (`BLU_PORTFOLIO_SUBACCOUNT_IDENTITY_UNRESOLVED=True`). While top-level `bluAccount` identity is explicit across 100% of documents, child subaccounts (`bluSaving`, `bluGether`) only expose display labels (e.g., custom pocket names) without any provider-native pocket account ID or number in the PDF source. Per controller invariants, display names cannot serve as stable provider keys. Portfolio adapter implementation is STOPPED and deferred for ChatGPT architectural review.

---

## 2. Frozen Provider Contracts

### Mutation Adapter Contract
- **Adapter ID:** `blu-account-mutation-v1`
- **Source Registry ID:** `blu_mutation`
- **Template ID:** `blu_account_mutation_v1`
- **Parser Version:** `parser-v1`
- **Source Channel:** `PDF`
- **Template Fingerprint:** `2226278f9614625f9174151dad8b8da7851a7455eeec48e0cd9017a96acbcbfc`
- **Required Structural Markers:**
  - `bluAccount`
  - `Periode / Period`
  - `Keterangan / Remarks`
  - `Nominal`
  - `Sisa Saldo`

### Portfolio Adapter Contract (Frozen Definition / Deferred Implementation)
- **Adapter ID:** `blu-portfolio-v1`
- **Source Registry ID:** `blu_portfolio`
- **Template ID:** `blu_portfolio_v1`
- **Parser Version:** `parser-v1`
- **Source Channel:** `PDF`
- **Template Fingerprint:** `9c62c6e955340520eb48f527fa6db973a053fcb81afba241ba43ed599a755657`
- **Required Structural Markers:**
  - `Laporan Portofolio`
  - `Ringkasan / Summary`
  - `bluSaving/bluGether`

---

## 3. Corpus Facts and Pre-Implementation Validation

### Mutation Family (`blu_mutation`)
- **Total SHA-Unique Documents:** 25
- **Periods Covered:** 2024-08 to 2026-08 (25 consecutive monthly periods; 24 `CLOSED` + 1 `OPEN` 2026-08 partial statement)
- **Page Count Range:** 1 to 3 pages
- **Text Layer:** 25/25 ready (100%)
- **Encryption:** 0/25 encrypted (unencrypted standard PDF)
- **Layout Families:** 1 stable layout family
- **Trusted Cash Movement Candidates:** 211 transactions
- **Per-Document Transaction Range:** 0 to 28 transactions
- **Zero-Activity Documents:** 7 documents containing explicit text `Tidak ada transaksi pada periode ini`.
- **Source Summary Equations:** 25/25 checkable and 25/25 passing (`Saldo Awal + Total Masuk - Total Keluar == Saldo Akhir`).
- **Identity Anchoring:**
  - 18 active statements contain explicit table section header `bluAccount - <provider-native-account-id>`.
  - 7 zero-activity statements omit numeric account digits in the extracted text stream.
  - Fail-Closed Policy: 18 active statements emit `ACCOUNT_OBSERVATION`; 7 zero-activity statements emit `SOURCE_SUMMARY` but 0 `ACCOUNT_OBSERVATION` and 0 `CASH_MOVEMENT`.
  - Ambiguity Rule: Multiple distinct `bluAccount - <id>` headers fail closed with `BLU_MUTATION_ACCOUNT_IDENTITY_AMBIGUOUS`.

### Portfolio Family (`blu_portfolio`)
- **Total Files in Archive:** 26 (with 1 byte-identical duplicate copy).
- **Total SHA-Unique Documents:** 25
- **Periods Covered:** 2024-08 to 2026-08 (24 `CLOSED` + 1 `OPEN`)
- **Page Count Range:** 1 page (all 25 documents)
- **Top-Level Identity:** Explicit `bluAccount - <provider-native-account-id>` on all 25 documents.
- **Child Pockets Observed:** Multiple subaccount display names observed across periods.
- **Identity Hard Gate Finding:** No numeric or provider-native subaccount identifier is present in source text for child pockets. Display names are strictly forbidden as stable identity anchors.

---

## 4. Evidence Roles and Authority

| Evidence Role | Mutation Authority (`blu_mutation`) | Portfolio Authority (`blu_portfolio`) |
| :--- | :--- | :--- |
| `CASH_MOVEMENT` | **YES** (Sole transaction authority, 211 events) | **NO** (0 events) |
| `SOURCE_SUMMARY` | **YES** (Document-level bluAccount summary, 25 events) | **YES** (Overall total portfolio summary only) |
| `ACCOUNT_OBSERVATION` | **YES** (18 active docs with explicit numeric account) | **DEFERRED** (Pending subaccount identity resolution) |
| `BALANCE_SNAPSHOT` | **NO** (Preserves portfolio authority) | **YES** (Closing/current pocket & total balance snapshots) |
| `ACCOUNT_PERIOD_SUMMARY` | **NO** | **YES** (Subaccount period opening/in/out/closing) |
| `INVESTMENT_TRADE` | **NO** | **NO** |

---

## 5. Mutation Parser Grammar and Implementation Blueprint

### 5.1 Document Header and Metadata Extraction
- Customer Name: extracted from `Nama / Name\n<Customer Name>`.
- Period: extracted from `Periode / Period\n<Period String>`. Supports standard Indonesian/English month strings (e.g., `August 2024`, `01 - 28 Agu 2026 09:37`).
- Period Status: Preserves `AdapterInput.period_status` (`OPEN` for 2026-08 partial, `CLOSED` for completed monthly statements).
- Currency: IDR.

### 5.2 Summary Reconciliation
- Extracts four summary values:
  1. `Saldo Awal / Initial Balance`
  2. `Total Pemasukan / Total Income`
  3. `Total Pengeluaran / Total Expense`
  4. `Saldo Akhir / Ending Balance`
- Verifies mathematical identity: `opening_balance + total_inflow - total_outflow == closing_balance`.
- Tolerance: `< Decimal("0.005")`.

### 5.3 Transaction Row Extraction
- Table Headers: Date line format `DD Mon YYYY` (e.g. `03 Aug 2024`, `28 Agu 2026`).
- Transaction Line: Time `HH:MM`, followed by description and optional pipe-delimited native reference ID `| <ref_id>`.
- Amount Line: Description tail, amount with direction prefix (`-` for outflow / debit, unsigned for inflow / credit), and running balance `Sisa Saldo`.
- Continuity Check: Running balance sequence verified across consecutive transactions within the document (`prev_balance + direction * amount == balance_after`).

### 5.4 Zero-Activity Statement Behavior
- Detects `Tidak ada transaksi pada periode ini`.
- Validates summary equation (e.g., `opening == closing`, `inflow == 0`, `outflow == 0`).
- Emits 1 `SOURCE_SUMMARY` envelope.
- Emits 0 `CASH_MOVEMENT` envelopes.
- Emits 0 `ACCOUNT_OBSERVATION` envelopes (as numeric account ID is omitted from text layer).
- Parse status: `COMPLETED`.

---

## 6. Private Replay Expected Counts (Mutation)

- **Total Documents Replayed:** 25
- **Parse Status:** 25/25 `COMPLETED` (0 `FAILED`)
- **`CASH_MOVEMENT` Envelopes:** 211
- **`SOURCE_SUMMARY` Envelopes:** 25
- **`ACCOUNT_OBSERVATION` Envelopes:** 18
- **`BALANCE_SNAPSHOT` Envelopes:** 0
- **`ACCOUNT_PERIOD_SUMMARY` Envelopes:** 0
- **Total Normalized Event Envelopes:** 254
- **Zero-Activity Documents:** 7
- **Summary Equation Reconciled:** 25/25

---

## 7. Safe Diagnostics and Privacy Controls

- All private provider identifiers, customer names, raw descriptions, and account numbers are marked `repr=False` in data classes.
- Diagnostics use deterministic safe error codes (e.g., `BLU_MUTATION_PDF_ENCRYPTED_UNSUPPORTED`, `BLU_MUTATION_HEADER_INCOMPLETE`, `BLU_MUTATION_SUMMARY_MISMATCH`, `BLU_MUTATION_RUNNING_BALANCE_DISCONTINUITY`, `BLU_MUTATION_ACCOUNT_IDENTITY_AMBIGUOUS`).
- Natural keys and row fingerprints use deterministic SHA-256 digests.

---

## 8. Exact Focused Test Plan (Mutation: 34 Tests)

The test suite `tests/test_universal_ingestion_phase3_blu_mutation.py` implements exactly 34 `unittest` methods:
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
13. `test_13_no_filename_identity_authority`
14. `test_14_zero_activity_statement_omits_account_observation`
15. `test_15_single_page_statement_parsing`
16. `test_16_multi_page_statement_parsing_page_boundaries`
17. `test_17_repeated_page_headers_ignored`
18. `test_18_date_time_extraction_precision`
19. `test_19_inflow_transaction_direction_and_amount`
20. `test_20_outflow_transaction_direction_and_amount`
21. `test_21_locale_decimal_parsing_strictness`
22. `test_22_running_balance_extraction`
23. `test_23_native_transaction_reference_id_optional`
24. `test_24_multiline_transaction_detail_wrapping`
25. `test_25_source_summary_extraction_and_reconciliation`
26. `test_26_summary_equation_failure_fail_closed`
27. `test_27_running_balance_continuity_success`
28. `test_28_running_balance_inconsistency_fail_closed`
29. `test_29_open_period_status_preserved`
30. `test_30_deterministic_parse_output`
31. `test_31_privacy_safe_repr_no_pii_leaks`
32. `test_32_privacy_safe_diagnostics_no_raw_identifiers`
33. `test_33_no_balance_snapshot_or_period_summary_emitted`
34. `test_34_corpus_replay_candidate_preservation`
