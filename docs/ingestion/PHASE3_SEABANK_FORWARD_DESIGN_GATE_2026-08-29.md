# Phase 3 SeaBank Forward Design Gate — 2026-08-29

## 1. Frozen Source & Template Authority Contract
- `source_registry_id`: `seabank_statement`
- `template_id`: `seabank_estatement_v1`
- `parser_version`: `parser-v1`
- `adapter_id`: `seabank-monthly-statement-v1`
- `source_channel`: `SourceChannel.PDF`
- `template_fingerprint`: `fc7add5e7b3a321692fda3196b25083e3620bfcf15f4eb63313fdc3de4a99656`
- `required_markers`: `("REKENING KORAN", "RINGKASAN REKENING", "TABUNGAN - RINCIAN TRANSAKSI")`

## 2. Audited Private Corpus Constants (19-Document Ground Truth)
- `SEABANK_CORPUS_UNIQUE_COUNT`: 19
- `SEABANK_PERIOD_COUNT`: 19 (19 distinct consecutive monthly periods)
- `SEABANK_COVERAGE`: `2025-01..2026-07`
- `SEABANK_PAGE_RANGE`: `1..6` (min: 1, max: 6)
- `SEABANK_LAYOUT_FAMILIES`: 2
  - Family A (4-column layout): `2025-01..2025-03` (`TANGGAL TRANSAKSI KELUAR (IDR) MASUK (IDR)`)
  - Family B (5-column layout): `2025-04..2026-07` (`TANGGAL TRANSAKSI KELUAR (IDR) MASUK (IDR) SALDO AKHIR (IDR)`)
- `SEABANK_TRUSTED_TRANSACTION_COUNT`: 89
- `SEABANK_TRANSACTION_RANGE`: `0..39` (min: 0 in 12 zero-activity months, max: 39 in March 2025)
- `SEABANK_DOCUMENT_SUMMARY_COUNT`: 19 (100% checkable and mathematically consistent)
- `SEABANK_ACCOUNT_OBSERVATION_COUNT`: 19 (1 per document for the single authoritative account)
- `SEABANK_NO_ACTIVITY_COUNT`: 12 (zero-activity statements where opening == closing balance and 0 transactions)
- `SEABANK_DUPLICATE_FACTS`: No same-period duplicates in 19-document corpus. 19 distinct consecutive monthly periods.

## 3. Provider-Anchored Document Identity Authority
- Exact anchored label: `NO. REKENING SEABANK:\s*([0-9]{8,32})` on Page 1.
- Positive identity rule: Valid `NO. REKENING SEABANK: <ACCOUNT_NUMBER>` binds the stable provider account identity.
- Negative decoy rule: Decoy numbers outside `NO. REKENING SEABANK:` (such as customer phone numbers `1500 130`, postal codes, serial numbers `S/N S01-...`, or address numbers) MUST NOT be accepted as account identity.
- Ambiguity rule: Missing `NO. REKENING SEABANK:` or multiple conflicting account numbers in header fails closed with `SEABANK_HEADER_INCOMPLETE`.

## 4. Exact Natural Document Key Format
- `natural_document_key`: `seabank_statement:<sha256(provider_header_account_id)>:<YYYY-MM>`
- Raw account number is NEVER exposed in the key or diagnostics; only its SHA-256 digest is namespaced.

## 5. Transaction Row Grammar & Extraction
- Start of transaction table: `TABUNGAN - RINCIAN TRANSAKSI`.
- End of transaction table: `TABUNGAN - RINCIAN BUNGA & PAJAK`, `DEPOSITO - RINCIAN BUNGA & PAJAK`, `Ketentuan Umum`, or `TIDAK ADA TRANSAKSI`.
- Transaction anchor: Date token at `45 <= x <= 55` matching `^(\d{1,2})?\s*([A-Za-z]{3})$`.
- Date resolution:
  - If day is present: `YYYY-MM-DD` using statement year and transaction month/day.
  - If day is omitted (e.g. monthly interest row `JAN` or `FEB`): Resolved to the last day of the statement month (e.g. `2025-01-31`).
- Amount & Direction mapping:
  - 4-column layout (`is_5_col == False`):
    - Outflow amount at `380 <= x <= 450` -> `EventDirection.OUTFLOW`, `direction_raw = "KELUAR"`.
    - Inflow amount at `500 <= x <= 570` -> `EventDirection.INFLOW`, `direction_raw = "MASUK"`.
    - `balance_after = None`.
  - 5-column layout (`is_5_col == True`):
    - Outflow amount at `290 <= x <= 370` -> `EventDirection.OUTFLOW`, `direction_raw = "KELUAR"`.
    - Inflow amount at `400 <= x <= 480` -> `EventDirection.INFLOW`, `direction_raw = "MASUK"`.
    - Ending balance at `500 <= x <= 570` -> `balance_after`.
- Description & Category:
  - Text tokens between date and amount are parsed as counterparty/description and transaction type.

## 6. Evidence-Role Mapping
- `CASH_MOVEMENT`: Emitted for each valid transaction row (89 across corpus).
- `SOURCE_SUMMARY`: Emitted for document aggregate summary from `RINGKASAN REKENING` (19 across corpus).
- `ACCOUNT_OBSERVATION`: Emitted for the authoritative account key `NO. REKENING SEABANK:` (19 across corpus).
- Total events across 19 documents: 89 + 19 + 19 = 127 events.

## 7. Row Fingerprint Specification
- `row_fingerprint` = SHA-256 over:
  `f"{period_str}:{account_id}:{occurred_at}:{direction}:{amount}:{category}:{description}:{balance_after}"`

## 8. Fail-Closed Diagnostics
- `SEABANK_PDF_ENCRYPTED_UNSUPPORTED`: Encrypted PDF statements fail closed.
- `SEABANK_PERIOD_AMBIGUOUS`: Missing or unparseable statement period.
- `SEABANK_HEADER_INCOMPLETE`: Missing or ambiguous `NO. REKENING SEABANK:` account number.
- `SEABANK_SUMMARY_INCOMPLETE`: Malformed summary table in `RINGKASAN REKENING`.
- `SEABANK_ROW_AMOUNT_INVALID`: Malformed transaction row amounts.
- `SEABANK_STRUCTURE_TRUNCATED`: Corrupt or truncated PDF stream.
- `SEABANK_DUPLICATE_ROW_CONFLICT`: Duplicate transaction row collision.

## 9. 30-Test Method Plan
1. `test_01_exact_descriptor_template_fingerprint_and_channel`
2. `test_02_binary_payload_only_adapter_contract`
3. `test_03_sanitized_normal_statement_completes_with_expected_roles`
4. `test_04_unsupported_encrypted_pdf_fails_closed`
5. `test_05_exact_closed_statement_period_extraction`
6. `test_06_provider_anchored_stable_account_identity_positive`
7. `test_07_identity_negative_decoy_rejection`
8. `test_08_identity_conflict_or_ambiguity_fails_closed`
9. `test_09_natural_key_determinism`
10. `test_10_document_level_source_summary_mapping`
11. `test_11_opening_credit_debit_closing_decimal_typing_and_equation`
12. `test_12_account_observation_mapping`
13. `test_13_basic_transaction_row_parsed_exactly_once`
14. `test_14_exact_source_date_normalization`
15. `test_15_source_direction_evidence_controls_inflow_outflow`
16. `test_16_absolute_decimal_amount_mapping`
17. `test_17_raw_description_and_counterparty_reconstruction`
18. `test_18_raw_provider_tx_id_policy_and_source_event_id_none`
19. `test_19_running_balance_mapping_and_none_boundary`
20. `test_20_wrapped_multiline_row_handling`
21. `test_21_page_boundary_continuation_and_repeated_headers`
22. `test_22_zero_activity_statement_accepted_without_cash_movement`
23. `test_23_four_column_layout_family_a_support`
24. `test_24_five_column_layout_family_b_support`
25. `test_25_malformed_amount_fails_closed`
26. `test_26_malformed_ambiguous_period_fails_closed`
27. `test_27_truncated_pdf_stream_fails_closed`
28. `test_28_deterministic_replay_and_privacy_safe_repr`
29. `test_29_row_fingerprint_determinism_and_content_coverage`
30. `test_30_dryrun_semantic_identity_classification`
