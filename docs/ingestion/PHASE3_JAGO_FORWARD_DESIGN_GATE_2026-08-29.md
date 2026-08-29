# Phase 3 Jago Forward Design Gate

Date: 2026-08-29
Status: FROZEN

## 1. Executive Summary & Audited Corpus Facts

The Phase 3 Bank Jago monthly statement adapter implementation builds upon the audited private 11-PDF corpus (10 distinct monthly periods covering 2025-10 through 2026-07). All 11 documents are unencrypted, have standard text layers, and match the exact template fingerprint `854403d679306f21bdfcb16efa0c5fa89cc714d0b545e871bbae384196be18fb`.

Corpus structural metrics:
- 11 SHA-unique PDF documents across 10 statement periods (2025-10..2026-07).
- Exactly one semantic duplicate pair in period 2026-04 with different binary byte hashes but 100% identical financial payloads and metadata.
- 5 stable provider pocket identities (e.g. `103558407045`, `112419553900`, etc.), with pocket counts ranging from 2 to 5 per monthly statement.
- 35 total pocket summary sections across the 11 documents, with 35/35 mathematical balance consistency equations (`opening_balance + total_inflow - total_outflow == closing_balance`).
- 12 zero-activity pocket sections where no transaction rows occur during the period (`Saldo Sebelumnya == Saldo Akhir`), which are valid source observations and period summaries.
- 314 trusted transaction candidate rows (7 to 54 per document), each having explicit minute-precision timestamps, provider transaction IDs (`ID# ...`), explicit signed amounts (`+` or `-`), and explicit running balances.
- 22 unambiguous internal pocket-to-pocket transfer pairs, represented as paired rows across sending and receiving pockets sharing identical transaction IDs, timestamps, and amounts.

## 2. Adapter Descriptor Authority

```python
adapter_id = "jago-monthly-statement-v1"
source_registry_id = "jago_statement"
template_id = "jago_monthly_statement_v1"
parser_version = "parser-v1"
source_channel = SourceChannel.PDF
```

Template Fingerprint:
`854403d679306f21bdfcb16efa0c5fa89cc714d0b545e871bbae384196be18fb`

Anchor tokens:
- `Laporan Keuangan Bulanan`
- `RINGKASAN SALDO DALAM RUPIAH`
- `KANTONG PERSONAL`
- `Tanggal & Waktu`

## 3. Semantic Evidence Mapping (Evidence-Only Authority)

The adapter is strictly evidence-only. It extracts explicit facts from the source PDF without performing economic classification, ledger mutation, budgeting categorization, reconciliation, or network calls.

### 3.1 Document-Level Natural Key
Natural document key candidate:
`jago_statement:<sha256(provider_header_account_id)>:<YYYY-MM>`
- Derived deterministically from the stable provider-native customer account identifier in the page 1 header.
- Salted/hashed with SHA-256 to ensure zero private account number disclosure.
- Identical across the April 2026 different-byte duplicate pair; distinct across all distinct monthly statement periods.

### 3.2 Document-Level Aggregate Summary
Emitted as `EventRole.SOURCE_SUMMARY` with payload `SourceSummaryEvidence`:
- `currency = "IDR"`
- `period_start` / `period_end`
- `opening_balance`, `incoming_total`, `outgoing_total`, `closing_balance` extracted from the page 1 summary table (`RINGKASAN SALDO DALAM RUPIAH`).

### 3.3 Pocket Hierarchy and Lifecycle Evidence
For each pocket section present in the statement:
Emitted as `EventRole.ACCOUNT_OBSERVATION` with payload `ObservedAccountEvidence`:
- `observed_provider_account_key` = raw stable provider pocket ID (e.g. `103558407045`).
- `display_name_raw` = raw source pocket display name (e.g. `Kantong Utama`).
- `institution_id = "jago"`.
- `parent_observed_key` = raw stable document/header account identifier.
- `provider_state_raw` = complete source status and start-date phrase as printed (e.g. `Akun Aktif, mulai 02 Oct 2025`).
- `period_start` / `period_end` = statement period.

### 3.4 Pocket Period Summary
For each pocket section:
Emitted as `EventRole.ACCOUNT_PERIOD_SUMMARY` with payload `AccountPeriodSummaryEvidence`:
- `observed_provider_account_key` = raw stable provider pocket ID.
- `currency = "IDR"`.
- `period_start` / `period_end` = statement period.
- `opening_balance`, `incoming_total`, `outgoing_total`, `closing_balance` extracted from the pocket header balance block.
- Zero-activity pockets emit this summary with 0 transaction rows.

### 3.5 Cash Movement Evidence
For each transaction row:
Emitted as `EventRole.CASH_MOVEMENT` with payload `CashMovementEvidence`:
- `amount` = absolute `Decimal` value.
- `currency = "IDR"`.
- `direction` = `EventDirection.INFLOW` for `+`, `EventDirection.OUTFLOW` for `-`.
- `status = SourceEventStatus.POSTED`.
- `occurred_at` = ISO formatted string `YYYY-MM-DDTHH:MM:00`.
- `direction_raw` = `"+"` or `"-"`.
- `provider_transaction_id_raw` = provider-native transaction ID (e.g. `2561427859`).
- `source_event_id = None`.
- `provider_category_raw` = raw transaction type string (e.g. `Transfer Masuk`, `Pindah Kantong`, `QRIS Pembayaran`).
- `description_raw` = raw note string if present, else `None`.
- `balance_after` = parsed running balance `Decimal`.
- Current-pocket endpoint mapping:
  - INFLOW: `destination_account_key_raw = current_pocket_id`, `source_account_display_raw = raw_source_dest`.
  - OUTFLOW: `source_account_key_raw = current_pocket_id`, `destination_account_display_raw = raw_source_dest`.
- `row_fingerprint` = SHA-256 over period, pocket ID, timestamp, direction, amount, provider transaction ID, type, source/dest, note, and running balance.

### 3.6 Internal Pocket Moves
- Paired internal transfers between pockets in the same document retain BOTH `CASH_MOVEMENT` rows.
- Valid pairs share the same provider transaction ID, amount, and timestamp across two distinct pocket IDs.
- Incompatible duplicate transaction IDs fail closed with `JAGO_DUPLICATE_ROW_CONFLICT`.

## 4. Parser Grammar & Multi-Page Layout Rules

- Binary-payload only with `pypdf` extraction.
- Parsing is state-machine based, tracking the active pocket scope across page breaks.
- Handles wrapped multiline source/destination text, multiline notes, and tab (`\t`) delimiters.
- Ignores repeated table headers (`Tanggal & Waktu`, `Sumber/Tujuan`, etc.) across page breaks.
- Fail closed with safe diagnostic codes on malformed groups, unreadable text, or encrypted PDFs.

## 5. Safe Diagnostic Blueprint

- `JAGO_PERIOD_AMBIGUOUS`
- `JAGO_HEADER_INCOMPLETE`
- `JAGO_POCKET_IDENTITY_UNSTABLE`
- `JAGO_SUMMARY_INCOMPLETE`
- `JAGO_ROW_AMOUNT_INVALID`
- `JAGO_ROW_DATE_AMBIGUOUS`
- `JAGO_ROW_DIRECTION_AMBIGUOUS`
- `JAGO_STRUCTURE_TRUNCATED`
- `JAGO_DUPLICATE_ROW_CONFLICT`
- `JAGO_PDF_ENCRYPTED_UNSUPPORTED`

## 6. Implementation Scope and Test Blueprint

Implementation is bounded to exactly THREE files:
1. `aturuang/ingestion_jago_adapter.py`
2. `tests/test_universal_ingestion_phase3_jago.py` (exactly 32 test methods)
3. `tests/fixtures/ingestion/jago_statement_v1.json`

Target test total:
- 245 baseline + 32 Jago = 277 total universal ingestion tests PASS.
- Replay across all 11 private corpus PDFs must achieve 11/11 COMPLETED, 0 REVIEW_REQUIRED, 0 FAILED, 395 total trusted events (314 cash movements, 35 account observations, 35 account period summaries, 11 source summaries).
