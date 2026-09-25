# Milestone A Verification Report (V3-STEP-A-VERIFY)

Date: 2026-09-25
Stage: V3-STEP-A-VERIFY
Role: read-only-verification-worker
Actor: Antigravity
Base Commit: 494d78f33f4cd91c1434c3c9ea2b72fb6e0ecfaf

## 1. Executive Summary

This report captures read-only verification findings across scopes A1 through A6 as part of Milestone A verification. No application source code was modified, no migrations were executed, and the production database remained untouched and intact.

---

## 2. Findings by Scope

### A1. Prompt 8b3c (Accessibility / A11Y)
- Test File: `tests/test_prompt8_accessibility.py`
- Test Execution Result: PASS (10/10 passed)
- Item Breakdown:
  - (a) Error state `role="alert"`: COVERED (`aturuang/web/js/app.js` line 397 in `setFieldError`, asserted by `test_04_validation_error_live_region_and_describedby`).
  - (b) Loading state `aria-busy`: COVERED (`aturuang/web/js/app.js` line 291 sets `aria-busy="true"`, line 308 removes `aria-busy`, asserted by `test_07_accessible_sighted_loading_state_and_recovery`).
  - (c) Contrast ratio 4.5:1 check: COVERED (`aturuang/web/css/styles.css` palette colors, asserted by `test_05_placeholder_contrast_across_all_themes` and `test_06_error_text_contrast_across_all_themes` calculating WCAG contrast across Midnight, Ocean, and Emerald themes).
  - (d) Hidden modal not in Tab order: COVERED (`aturuang/web/css/styles.css` line 1239 specifies `.modal { display: none; }` and line 1246 `.modal.open { display: flex; }`; modals initialized with `aria-hidden="true"`; keyboard handler in `aturuang/web/js/app.js` lines 3106-3132 traps Tab navigation strictly to `.modal.open`, asserted by `test_09_topmost_modal_focus_and_escape`).

### A2. Prompt 8b4 (UI Terminology + Live Money Format)
- Legacy Terms Search:
  - Scanned Files: `aturuang/web/index.html`, `aturuang/web/js/app.js`
  - "Uang Aman": 0 occurrences
  - "Dana Dijaga": 0 occurrences
  - "Uang Terjaga": 0 occurrences
  - Note: Verified by `tests/test_prompt8_ui_format.py` (7/7 passed).
- Thousand Separator Input Handler:
  - Status: FOUND
  - Details: Implemented via `formatMoneyInput` and `parseMoneyInput` in `aturuang/web/js/core.js` and `attachLiveMoneyFormatting` in `aturuang/web/js/app.js`. Handlers attach to fields including `fAmount`, `uAmount`, `recActualInput`, `allocAmount`, `debtInitialAmount`, `evAmount`, `planGuaranteed`, and `planAdditional`.

### A3. Prompt 12 (Insights, Forecast & Freshness)
- Route Present: YES
  - `/api/insights` (`aturuang/server.py` line 1743)
  - `/api/insights/recurring` (`aturuang/server.py` line 1749)
  - `/api/forecast` (`aturuang/server.py` line 1755)
  - `/api/accounts/freshness` (`aturuang/server.py` line 1761)
- Service Present: YES
  - In `aturuang/services.py`: `detect_recurring_patterns` (line 2837), `cashflow_forecast` (line 2958), `get_accounts_freshness` (line 3109), `detect_anomalies` (line 3253), `get_source_freshness` (line 3329), `get_all_insights` (line 3374).
- Test Result (`tests/test_prompt12_insights.py`): 2/12 passed (10 failed with `sqlite3.OperationalError` due to missing migrated schema `accounts` / `reversal_of_id` when tests copy the raw unmigrated `runtime/money_tracks.db` in `setUp`).

### A4. Prompt 17 (Goals + Monthly Close)
- `allocation_goals` Schema Columns:
  - `id, name, kind, target_amount, allocated_amount, target_date, priority, status, preferred_account, notes, created_at, updated_at` (defined in `aturuang/db.py` line 235).
- `goal_events` Table: ABSENT (No append-only goal event table exists in schema).
- Monthly Close / Reopen Mechanism: ABSENT (No `close_month`, `reopen_month`, or status flag on `monthly_plans` table).
- Telegram Commands Handler:
  - `/rencana`: NOT_FOUND
  - `/alokasi`: NOT_FOUND
  - `/tutupbulan`: NOT_FOUND
  - (Telegram handler in `cloud/worker/src/domain.ts` only supports transfer, expense, income, and raw text).

### A5. Apps Script Cleanup State
- `scripts/flip_reparse/export_helper.gs`: ABSENT (0 bytes, file does not exist in worktree).
- `integrations/gmail-apps-script/Code.gs` for "dumpRegressionBodies": NOT_FOUND (lines: NONE).
- Stray `.gs` files referencing `export_helper`: NONE (Only `integrations/gmail-apps-script/Code.gs` exists, containing no references to `export_helper`).

### A6. Scratch Folder Inventory
- Path: `C:\A User Main Storage\Downloads\aturuang_scratch\`
- File Count: 7 files (plus 1 subdirectory `AturUang/`)
- Inventory:
  1. `GRAND_DESIGN_V3_20260925.md` — 125,243 bytes
  2. `jago-parser-v2-uncommitted-20260925.patch` — 13,983 bytes
  3. `PATTERN_V1_ARCHITECTURAL_ANALYSIS_20260925.txt` — 2,652 bytes
  4. `PATTERN_V1_AUDIT_20260925.txt` — 4,564 bytes
  5. `STORY_BIBLE_PATCH_04_20260925.txt` — 22,832 bytes
  6. `V21_AUDIT_FINDINGS_20260925.txt` — 3,251 bytes
  7. `V21_DEADCODE_CORRECTION_20260925.txt` — 4,008 bytes

---

## 3. Database Integrity
- Production DB Path: `C:\A User Main Storage\Documents\GitHub\AturUang\runtime\money_tracks.db`
- SHA256 Before: `8afc95829d0fa160b3d34efd6834a98aae6231262683f82ba85f01997c736421`
- SHA256 After: `8afc95829d0fa160b3d34efd6834a98aae6231262683f82ba85f01997c736421`
- Status: INTACT (Unmodified)
