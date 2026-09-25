# Milestone B Phase 5C Semantics Spike Report (V3-STEP-B-1-SPIKE)

Date: 2026-09-25
Stage: V3-STEP-B-1-SPIKE
Role: read-only-analysis-worker
Actor: Antigravity
Base Commit: 494d78f33f4cd91c1434c3c9ea2b72fb6e0ecfaf

---

## 1. Executive Summary

This spike investigates the architectural integration of the Phase 5C Minimal Semantics Layer (`aturuang/ingestion_semantics.py`) into existing ingestion consumers: Quick Capture (`aturuang/quick_capture.py`) and Import Center (`aturuang/import_center.py`). All analysis was strictly read-only; no production code was modified. The production database remained untouched and cryptographically verified before and after all queries.

---

## 2. Scope Findings

### S1. Public API of `aturuang/ingestion_semantics.py`
- Functions:
  - `interpret_single_decision(decision: EvidenceMatchDecision, record: SafeEvidenceRecord | None, group: EconomicEventGroup | None) -> SemanticDecision`
  - `interpret_evidence_semantics(match_plan: EvidenceMatchPlan, evidence_records: Mapping[str, SafeEvidenceRecord] | Sequence[SafeEvidenceRecord]) -> SemanticInterpretationPlan`
- Purity vs Impurity:
  - Pure functions: `interpret_single_decision`, `interpret_evidence_semantics` (100% pure functional transformations, zero database access, zero filesystem IO, zero network).
  - Impure functions: None.
- Canonical Enum:
  - `TransactionSemanticType(str, Enum)`: `EXPENSE = "expense"`, `INCOME = "income"`, `INTERNAL_TRANSFER = "internal_transfer"`, `INVESTMENT_FLOW = "investment_flow"`, `AMBIGUOUS = "ambiguous"`, `UNKNOWN = "unknown"`.
- Fail-Closed Behaviors:
  - Strict type and hex64 digest invariants enforced on all dataclass fields.
  - Ambiguous match tier (`MatchTier.AMBIGUOUS`) is strictly preserved as `TransactionSemanticType.AMBIGUOUS` with `is_confirmed=False` (Rule 1).
  - Ineligible evidence or review required (`record.requires_review`) preserved as `TransactionSemanticType.UNKNOWN` with `is_confirmed=False` (Rule 2).
  - Non-transactional roles (e.g. `ACCOUNT_OBSERVATION`) or unclassified events fall back to `TransactionSemanticType.UNKNOWN` with `is_confirmed=False` (Rule 12).
  - All output decisions are deterministically ordered by `evidence_key`.

### S2. Semantic Classification Sites in `aturuang/quick_capture.py`
- Site 1 (Lines 57-59, 178-186 in `parse_quick_capture`):
  - Tokenizes input words against hardcoded keyword set `TRANSFER_VERBS = frozenset({"transfer", "tf", "trf", "kirim", "pindah"})` to set `is_transfer = True`.
- Site 2 (Lines 240-253 in `parse_quick_capture`):
  - Branching hierarchy: if `is_transfer` -> `INTERNAL_TRANSFER`; elif leading '-' -> `EXPENSE`; elif leading '+' -> `INCOME`; elif keywords `["gaji", "salary", "bonus", "cashback", "terima", "dapat", "masuk", "income"]` in text -> `INCOME`; fallback -> `EXPENSE`.
- Site 3 (Lines 83-93, 329-335 in `parse_quick_capture`):
  - Category heuristic: substring matches against `CATEGORY_KEYWORDS` table ("Main Meals", "Cafe & Drinks", "Snacks", "Groceries & Daily Needs", "Fuel", "Parking/Toll", "Transport", "Phone & Internet", "Income"); fallback to "Other / Miscellaneous".
- Site 4 (Lines 319-325, 342-343 in `parse_quick_capture`):
  - Account and ledger mutation type routing based on `tx_type` string.

### S3. Semantic Classification Sites in `aturuang/import_center.py`
- Site 1 (Lines 278, 302-309 in `_parse_csv_row`):
  - Defaults `ttype_val = "Expense"`. Looks for header column ("type", "tipe", "jenis"); substring matches "income"/"masuk"/"kredit" -> `Income`, "transfer" -> `Transfer`.
- Site 2 (Lines 346-347 in `_parse_csv_row`):
  - Direction routing: assigns `account_from = acc_from` for non-income, `account_to = acc_from` for income.
- Site 3 (Lines 324-333 in `_parse_csv_row`):
  - Status classification heuristic based on amount <= 0 (`REVIEW_REQUIRED`) or description containing "unknown"/"suspense" (`AMBIGUOUS`).
- Site 4 (Lines 405-411 in `_parse_pdf`):
  - Hardcoded `transaction_type = "Expense"`, `account_from = "BCA Main"`, `account_to = ""`, `category = "Other / Miscellaneous"`.
- Site 5 (Line 199 in `process_batch`):
  - Reconciliation mock heuristic: `matched_pairs = total_rows // 2 if total_rows > 1 else 0`. Placeholder dividing row count by 2 without executing actual cross-statement or internal transfer matching.

### S4. Replacement Plan
- Quick Capture Call Sites:
  - Replace manual verb & sign branching (lines 178-186, 240-253) with an adapter constructing a lightweight `SafeEvidenceRecord`.
  - Evaluate via `interpret_single_decision`. Map `TransactionSemanticType` to `QuickCaptureResult` transaction type (`EXPENSE`, `INCOME`, `INTERNAL_TRANSFER`).
  - Signature Mismatch: Does NOT match directly. 5C requires `EvidenceMatchDecision` and `SafeEvidenceRecord` inputs and outputs `SemanticDecision`. An adapter function `adapt_quick_capture_to_semantics()` is needed.
- Import Center Call Sites:
  - In `_parse_csv` / `_parse_pdf`, map raw parsed rows to `SafeEvidenceRecord`s.
  - Run Phase 5A `EvidenceMatcher.match_evidence()` to generate a true `EvidenceMatchPlan`.
  - Feed match plan and records into `interpret_evidence_semantics()`.
  - Replace line 199 mock `total_rows // 2` with `len(match_plan.groups)`.
  - Signature Mismatch: Does NOT match directly. 5C works on batch match plans; an orchestration pipeline step is required to bridge CSV rows -> Evidence Records -> Match Plan -> Semantics Plan.

### S5. Delta Analysis (Production Database Historical Sample)
- Connected to production database read-only (`?mode=ro`).
- Sampled 20 recent historical transactions and evaluated heuristic vs 5C semantics.
- Mismatch Count: 8/20 (40% discrepancy).
- Distinct Sanitized Mismatch Patterns:
  1. Transfer / pass-through without explicit transfer verb in description -> Heuristic: `EXPENSE`, 5C: `internal_transfer`.
  2. E-wallet Top Up without transfer verb (`Top up [ACCOUNT]`) -> Heuristic: `EXPENSE`, 5C: `internal_transfer`.
  3. Pocket closure / interest reallocation (`Bunga penutupan Poket...`) -> Heuristic: `EXPENSE`, 5C: `internal_transfer`.
  4. ATM Cardless Cash withdrawal (`Tarik tunai tanpa kartu`) -> Heuristic: `EXPENSE`, 5C: `internal_transfer`.

### S6. Test Impact Analysis
- Test Suites Evaluated:
  - `tests/test_stage7a_quick_capture.py` (17 tests)
  - `tests/test_stage7b_import_center.py` (15 tests)
  - `tests/test_universal_ingestion_phase5_semantics.py` (15 tests)
- Tests to Update in Consumers:
  1. `test_stage7a_quick_capture.py::test_fail_closed_on_ambiguous_grammar` (asserts Stage 7A diagnostic codes like `MISSING_TRANSFER_DESTINATION`).
  2. `test_stage7a_quick_capture.py::test_parse_internal_transfer_grammar` (asserts exact transaction type string format).
  3. `test_stage7b_import_center.py::test_import_center_batch_ingestion` (asserts `parsed_items == 4` and `review_required_count == 0`; under 5C, single-leg transfers without counterpart records are unconfirmed, altering status to `REVIEW_REQUIRED`).
  4. `test_stage7b_import_center.py::test_import_center_summary_generation` (batch metrics `matched_pairs` changing from `total_rows // 2` mock heuristic to actual matching group count).
- Estimated Breaking Assertions: ~5-7 individual assertion checks across 4 test cases.
- Specification Suite `test_universal_ingestion_phase5_semantics.py` requires 0 updates (15/15 PASS, authoritative contract).

---

## 3. Safety Invariants Verification
- Side Effects in 5C: False (0 DB queries, 0 file IO, 0 network operations).
- Production DB Hash Before: `8afc95829d0fa160b3d34efd6834a98aae6231262683f82ba85f01997c736421`
- Production DB Hash After: `8afc95829d0fa160b3d34efd6834a98aae6231262683f82ba85f01997c736421`
- Status: INTACT (Unmodified).
