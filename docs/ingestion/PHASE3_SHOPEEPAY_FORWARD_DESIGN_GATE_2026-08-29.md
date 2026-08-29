# Phase 3 ShopeePay Transaction History Image Ingestion - Forward Design Gate

Date: 2026-08-29
Authoritative Baseline: 681a7e09c539ff72502a294cd683701969138ba2
Target Review Branch: review/shopeepay-v1
Production DB SHA256: 2b537bbcaa6a22bbd7018630b84152a319ce352624b97f5ace41563c1561945f

---

## 1. Executive Summary & Design Scope

This document defines the frozen architecture, contract invariants, OCR boundary, and validation rules for ingesting ShopeePay transaction-history screenshot captures in AturUang Universal Ingestion Phase 3.

ShopeePay transaction-history captures are pure raster image artifacts (IMAGE media type: JPEG/PNG) without embedded text layers. Ingestion is performed via deterministic local optical character recognition and visual layout parsing to recover trusted financial events without cloud services, generative AI, or non-deterministic heuristics.

---

## 2. Frozen Corpus Truth & Historical Partitioning

The private corpus consists of 13 SHA-unique ShopeePay transaction-history screenshots spanning August 2025 through August 2026 (13 consecutive monthly periods):

- Whole Corpus Summary:
  - Total Unique Image Artifacts: 13
  - Closed Calendar Periods: 12 (2025-08 through 2026-07)
  - Open / Partial Calendar Periods: 1 (2026-08, activity through 2026-08-21)
  - Total Visible Transaction Cards: 274
  - Successful Monetary Movement Candidates: 267
  - Explicit Failed Evidence Rows: 7

- Partition 1: High-Resolution 2025 Captures (5 Artifacts):
  - Months: August 2025 through December 2025
  - Dimensions: 1220x3689 to 1220x13017 (native full-resolution mobile scrolling captures)
  - Total Visible Cards: 62
  - Successful Monetary Rows: 60
  - Explicit Failed Rows: 2
  - Optical Extraction: Viable via deterministic vertical slicing using local Windows WinRT OCR.

- Partition 2: Low-Resolution 2026 Captures (8 Artifacts):
  - Months: January 2026 through August 2026
  - Dimensions: 118x1280 to 305x1036 (downscaled mobile captures)
  - Total Visible Cards: 212
  - Successful Monetary Rows: 207
  - Explicit Failed Rows: 5 (2026-01: 1, 2026-03: 1, 2026-04: 1, 2026-07: 1, 2026-08: 1)
  - Sidecar Validation: Independently cross-checked against the private historical sidecar ledger (ShopeePay_Ledger_2026-01-01_to_2026-08-21.csv/.json) with 212/212 exact core row matches and 0 mismatches.
  - Optical Extraction: Degraded resolution requires fail-safe REVIEW_REQUIRED disposition during direct raw-image OCR execution, while validated sidecar oracle provides expected-value verification for private replay.

---

## 3. Architecture & Role Model

- Primary Production Pipeline:
  - Raw Screenshot Payload (IMAGE)
  - Ingestion Preflight (source-hint routing + media validation)
  - Deterministic Local OCR Boundary (aturuang/ingestion_image_ocr.py)
  - ShopeePay Card & Layout Parser (aturuang/ingestion_shopeepay_adapter.py)
  - Envelope Construction (CASH_MOVEMENT)
- Strict Constraints:
  - No LLM / Multimodal AI in runtime parser.
  - No network / cloud OCR APIs.
  - No generative image synthesis or hallucinated interpolation.
  - Private sidecar ledger is strictly a Private Historical Validation Oracle; it is never committed, never registered as a provider export, and never a runtime dependency of the production adapter.

---

## 4. Visual Grammar & Source-Like Evidence

### 4.1. Explicitly Recoverable Fields
- Period Filter / Month Header: E.g., 01 Nov 2025 - 30 Nov 2025 / November 2025.
- Transaction Date: Formatted as DD Month YYYY (e.g., 25 November 2025).
- Signed IDR Amount: Explicit prefix +Rp... (inflow) or -Rp... (outflow).
- Visible Description / Title: Visible merchant, counterpart, or product line (e.g., Payment, Top Up, Cashback Bonus, Transfer Sent).
- Explicit Failed Badge: Red textual Failed indicator displayed on unsuccessful transaction attempts.

### 4.2. Absent Fields & Envelope Policy
- Transaction Time: Absent from list view -> occurred_at preserves source date (YYYY-MM-DD) without fabricating time.
- Native Transaction / Reference ID: Absent from list view -> provider_transaction_id_raw = None.
- Running Wallet Balance: Absent from list view -> balance_after = None, BALANCE_SNAPSHOT = 0.
- Monthly Summary Figures: Numerical totals (Total Pemasukan / Total Pengeluaran) are not rendered in list view -> SOURCE_SUMMARY = 0, ACCOUNT_PERIOD_SUMMARY = 0.
- Account / MSISDN Identity: No wallet account number or phone number is displayed on the screen -> ACCOUNT_OBSERVATION = 0.

---

## 5. Failed Transaction & Cash Movement Policies

- Failed Transaction Handling:
  - Explicitly marked Failed rows are parsed as internal evidence for audit and reconciliation verification.
  - Failed rows are strictly excluded from CASH_MOVEMENT envelopes (0 cash movement events emitted).
  - Failed attempts do not affect wallet balances and do not receive fabricated success records.
- Normal / Successful Rows:
  - Emitted with status = SourceEventStatus.UNKNOWN.
  - Direction mapped deterministically: +Rp -> EventDirection.INFLOW, -Rp -> EventDirection.OUTFLOW.
  - Amount stored as exact positive Decimal IDR.
  - Raw direction preserved in direction_raw.
  - Semantic categories (such as expense, transfer, refund) from sidecar metadata are completely ignored.

---

## 6. Period Completeness & Status Policy

- Closed Period Rule: A monthly artifact is marked PeriodStatus.CLOSED if and only if the header date filter spans from day 1 to the actual final calendar day of that month (e.g., 01 Nov 2025 - 30 Nov 2025).
- Open Period Rule: If the date filter ends before the calendar month completes (e.g., 01 Aug 2026 - 21 Aug 2026), the artifact is marked PeriodStatus.OPEN (or PARTIAL).
- Capture completeness of the screenshot itself does not override period calendar boundaries.

---

## 7. Natural Key & Unidentified Wallet Authority

Because ShopeePay screenshot captures do not render a provider-native wallet account identifier, identity authority is established through a conservative natural document key:

- Natural Key Pattern: shopeepay_mutation:unidentified_wallet:<period> (e.g., shopeepay_mutation:unidentified_wallet:2025-11).
- Forbidden Identity Sources: Filenames, directory names, customer display names, OCR-inferred phone numbers, or sidecar annotations.
- Invariant: ACCOUNT_OBSERVATION envelopes remain 0. If multiple conflicting documents for the same period are submitted, orchestration routes them to conflict review rather than silently merging.

---

## 8. Local OCR Abstraction & Boundary Design

### 8.1. Module Boundary (aturuang/ingestion_image_ocr.py)
- Provides a clean, standalone interface extract_image_text(image_bytes, *, config=None) -> ImageOcrResult.
- Implementation utilizes Windows built-in Windows.Media.Ocr.OcrEngine (WinRT) locally with bounded execution timeouts and fail-closed error handling.
- Extensible to standard local Tesseract CLI if installed in the future.

### 8.2. Deterministic Image Slicing for Tall Screenshots
- WinRT OCR enforces a maximum dimension limit (e.g., 2600-4096 px). Tall mobile screenshots (up to 13,017 px) are partitioned into vertical slices with fixed height and deterministic vertical overlap.
- Card deduplication across slice boundaries is handled by comparing transaction dates, amounts, and vertical positions, preventing duplicate event emission.

### 8.3. Quality Gate & Fail-Safe Invariant
- High-resolution captures (width >= 1200 px) extract cleanly when sliced.
- Low-resolution downscaled captures (width < 350 px) that fail financial-critical OCR validation (ambiguous row boundaries, unreadable dates, or garbled amounts) fail safe with AdapterParseStatus.REVIEW_REQUIRED (or FAILED), emitting 0 unverified cash movements.

---

## 9. Preflight & Orchestration Integration Seam

- Preflight Routing Extension:
  - aturuang/ingestion_preflight.py is extended to support image-based template routing when claimed_source_registry_id == "shopeepay_mutation".
  - Signature shopeepay_transaction_history_image_v1 is defined with expected media IMAGE.
  - Resolution policies accommodate tall portrait aspect ratios without falsely rejecting long mobile captures.
- Trust Boundary Invariant:
  - Source hint provides routing authority only.
  - The ShopeePay adapter independently verifies layout markers (Transaction History / Transaksi Terakhir, month header, filter tabs) in OCR text before parsing financial cards.
  - Unrecognized or spoofed images immediately fail closed.

---

## 10. Implementation Deliverables & Test Design

### 10.1. File Targets
- Ingestion Adapter: aturuang/ingestion_shopeepay_adapter.py
- OCR Extraction Module: aturuang/ingestion_image_ocr.py
- Synthetic Test Fixtures: tests/fixtures/ingestion/shopeepay_transaction_history_image_v1.json
- Test Suite: tests/test_universal_ingestion_phase3_shopeepay.py

### 10.2. Focused Test Suite (Exactly 30 Unittest Methods)
1. test_01_adapter_metadata_and_descriptor: Verify registry ID, template ID, and parser version constants.
2. test_02_template_signature_and_fingerprint: Verify template fingerprint and preflight signature matching.
3. test_03_valid_high_res_jpeg_extraction: Verify parsing of clean synthetic high-resolution JPEG capture.
4. test_04_valid_png_media_extraction: Verify parsing of clean synthetic PNG capture.
5. test_05_corrupt_or_truncated_image_fails_closed: Fail closed on invalid image headers or truncated data.
6. test_06_non_shopeepay_image_rejected: Reject random images missing ShopeePay layout markers.
7. test_07_source_hint_cannot_bypass_layout_verification: Ensure claiming ShopeePay without valid layout fails closed.
8. test_08_tall_image_vertical_slicing: Verify slicing logic correctly partitions images exceeding MaxImageDimension.
9. test_09_slice_overlap_deduplication: Verify cards spanning slice boundaries are not duplicated.
10. test_10_month_header_and_filter_parsing: Verify extraction of Indonesian and English month headers.
11. test_11_closed_calendar_period_assignment: Verify complete monthly date filters receive PeriodStatus.CLOSED.
12. test_12_open_partial_calendar_period_assignment: Verify partial date filters (e.g. Aug 1-21) receive PeriodStatus.OPEN.
13. test_13_date_only_occurred_at_preservation: Verify occurred_at stores source date without fabricated time.
14. test_14_positive_amount_inflow_mapping: Verify +Rp correctly maps to EventDirection.INFLOW.
15. test_15_negative_amount_outflow_mapping: Verify -Rp correctly maps to EventDirection.OUTFLOW.
16. test_16_decimal_amount_precision: Verify IDR amounts parse into exact Decimal values without float drift.
17. test_17_multiline_description_normalization: Verify multiline merchant/transfer details normalize cleanly.
18. test_18_explicit_failed_badge_omitted_from_cash_movement: Verify failed cards emit 0 CASH_MOVEMENT events.
19. test_19_normal_transaction_status_unknown: Verify successful cards have status = SourceEventStatus.UNKNOWN.
20. test_20_zero_account_observations_emitted: Assert exactly 0 ACCOUNT_OBSERVATION envelopes emitted.
21. test_21_zero_source_summaries_emitted: Assert exactly 0 SOURCE_SUMMARY envelopes emitted.
22. test_22_zero_balance_snapshots_emitted: Assert exactly 0 BALANCE_SNAPSHOT envelopes emitted.
23. test_23_zero_provider_transaction_id_emitted: Assert provider_transaction_id_raw is None on all rows.
24. test_24_no_semantic_category_derivation: Verify absence of expense/transfer/refund classification.
25. test_25_ambiguous_amount_triggers_review_required: Unreadable amount digits trigger REVIEW_REQUIRED.
26. test_26_ambiguous_sign_triggers_review_required: Missing or ambiguous +/- sign triggers REVIEW_REQUIRED.
27. test_27_ambiguous_date_association_triggers_review_required: Card disconnected from date triggers REVIEW_REQUIRED.
28. test_28_low_resolution_ocr_quality_fail_safe: Degraded optical input fails safely to REVIEW_REQUIRED.
29. test_29_natural_document_key_unidentified_wallet: Verify deterministic natural key format.
30. test_30_diagnostics_and_repr_privacy_safety: Assert no raw OCR card text or PII leaks in diagnostics.
