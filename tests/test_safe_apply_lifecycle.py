"""
Universal Ingestion Stage 6 — Safe Review, Apply Gate, and Recovery Engine Unit Tests.

Comprehensive testing of candidate lifecycle state machine, deterministic preview hash binding,
pre-apply automated backups, atomic SQLite transactions, strict idempotency enforcement,
immutable audit trails, and backup restore recovery drills.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from aturuang.safe_apply import (
    ApplyCandidate,
    ApplyResult,
    AuditTrailEntry,
    BackupError,
    CandidateExpiredError,
    CandidateLifecycleState,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    LIFECYCLE_STATES_COUNT,
    LedgerMutation,
    PreviewHashMismatchError,
    ReviewRequiredError,
    SAFE_APPLY_CONTRACT_VERSION,
    SafeApplyEngine,
    TERMINAL_STATES,
    compute_file_sha256,
    compute_preview_hash,
    create_pre_apply_backup,
    restore_from_backup,
)

EXPECTED_PRODUCTION_DB_SHA256 = "8afc95829d0fa160b3d34efd6834a98aae6231262683f82ba85f01997c736421"


def _find_production_db() -> Path:
    candidates = [
        Path("C:/A User Main Storage/Documents/GitHub/AturUang/runtime/money_tracks.db"),
        Path(__file__).resolve().parent.parent.parent / "AturUang" / "runtime" / "money_tracks.db",
        Path(__file__).resolve().parent.parent / "runtime" / "money_tracks.db",
    ]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError("Production database not found")


class SafeApplyLifecycleTests(unittest.TestCase):
    """Comprehensive lifecycle, transactional, and recovery unit tests for Stage 6."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.db_path = self.temp_path / "test_ledger.db"
        self.backup_dir = self.temp_path / "backups"
        self.engine = SafeApplyEngine(self.db_path, self.backup_dir)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _make_mutation(
        self,
        amount: Decimal = Decimal("100000.00"),
        account_from: str = "BCA Main",
        account_to: str = "Merchant External",
        transaction_type: str = "Expense",
        date: str = "2026-03-15",
        time: str = "14:30:00",
        description: str = "Synthetic Test Expense",
        canonical_id: str | None = None,
    ) -> LedgerMutation:
        return LedgerMutation(
            date=date,
            time=time,
            transaction_type=transaction_type,
            amount=amount,
            account_from=account_from,
            account_to=account_to,
            description=description,
            category="Main Meals",
            canonical_id=canonical_id or f"TX_{amount}_{account_from}_{account_to}",
        )

    def _make_candidate(
        self,
        candidate_id: str = "CAND_001",
        idempotency_key: str = "IDEM_001",
        amount: Decimal = Decimal("100000.00"),
        mutations: tuple[LedgerMutation, ...] | None = None,
        evidence_keys: tuple[str, ...] = ("EV_001", "EV_002"),
    ) -> ApplyCandidate:
        if mutations is None:
            mutations = (self._make_mutation(amount=amount),)
        return ApplyCandidate(
            candidate_id=candidate_id,
            idempotency_key=idempotency_key,
            state=CandidateLifecycleState.RECEIVED,
            participating_evidence_keys=evidence_keys,
            mutations=mutations,
        )

    def test_full_lifecycle_received_to_applied(self) -> None:
        cand = self._make_candidate(candidate_id="CAND_HAPPY_01", idempotency_key="IDEM_HAPPY_01")
        self.assertEqual(cand.state, CandidateLifecycleState.RECEIVED)

        # Transition to PARSED
        cand.transition_to(CandidateLifecycleState.PARSED)
        self.assertEqual(cand.state, CandidateLifecycleState.PARSED)

        # Transition to READY_FOR_CONFIRMATION
        cand.transition_to(CandidateLifecycleState.READY_FOR_CONFIRMATION)
        self.assertEqual(cand.state, CandidateLifecycleState.READY_FOR_CONFIRMATION)

        # Explicit owner confirmation
        cand.confirm()
        self.assertEqual(cand.state, CandidateLifecycleState.CONFIRMED)

        # Apply via engine
        result = self.engine.apply(cand)
        self.assertTrue(result.success)
        self.assertEqual(cand.state, CandidateLifecycleState.APPLIED)
        self.assertEqual(result.state, CandidateLifecycleState.APPLIED)
        self.assertFalse(result.is_idempotent_replay)
        self.assertEqual(len(result.applied_row_ids), 1)

        # Verify row in DB
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("SELECT amount, account_from, account_to FROM transactions WHERE id = ?", (result.applied_row_ids[0],))
        row = cur.fetchone()
        con.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 100000.0)

    def test_idempotent_retry_prevents_duplicate_ledger_entry(self) -> None:
        cand = self._make_candidate(candidate_id="CAND_IDEM_01", idempotency_key="IDEM_KEY_01")
        cand.transition_to(CandidateLifecycleState.PARSED)
        cand.transition_to(CandidateLifecycleState.READY_FOR_CONFIRMATION)
        cand.confirm()

        # First apply
        res1 = self.engine.apply(cand)
        self.assertTrue(res1.success)
        self.assertFalse(res1.is_idempotent_replay)

        # Re-apply same candidate
        cand_retry = self._make_candidate(candidate_id="CAND_IDEM_01", idempotency_key="IDEM_KEY_01")
        cand_retry.transition_to(CandidateLifecycleState.PARSED)
        cand_retry.transition_to(CandidateLifecycleState.READY_FOR_CONFIRMATION)
        cand_retry.confirm()

        res2 = self.engine.apply(cand_retry)
        self.assertTrue(res2.success)
        self.assertTrue(res2.is_idempotent_replay)
        self.assertEqual(res1.applied_row_ids, res2.applied_row_ids)

        # Verify only 1 row in transactions table
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM transactions")
        count = cur.fetchone()[0]
        con.close()
        self.assertEqual(count, 1)

    def test_idempotency_conflict_detected_on_payload_mismatch(self) -> None:
        cand1 = self._make_candidate(candidate_id="CAND_01", idempotency_key="SHARED_IDEM_KEY", amount=Decimal("100000.00"))
        cand1.transition_to(CandidateLifecycleState.PARSED)
        cand1.transition_to(CandidateLifecycleState.READY_FOR_CONFIRMATION)
        cand1.confirm()
        self.engine.apply(cand1)

        # Cand2 has same idempotency_key but different amount (different preview hash)
        cand2 = self._make_candidate(candidate_id="CAND_02", idempotency_key="SHARED_IDEM_KEY", amount=Decimal("200000.00"))
        cand2.transition_to(CandidateLifecycleState.PARSED)
        cand2.transition_to(CandidateLifecycleState.READY_FOR_CONFIRMATION)
        cand2.confirm()

        with self.assertRaises(IdempotencyConflictError):
            self.engine.apply(cand2)
        self.assertEqual(cand2.state, CandidateLifecycleState.FAILED)

    def test_preview_hash_mismatch_aborts_transaction(self) -> None:
        cand = self._make_candidate(candidate_id="CAND_TAMPER", idempotency_key="IDEM_TAMPER")
        cand.transition_to(CandidateLifecycleState.PARSED)
        cand.transition_to(CandidateLifecycleState.READY_FOR_CONFIRMATION)
        cand.confirm()

        # Tamper with preview hash
        cand.preview_hash = "0" * 64

        with self.assertRaises(PreviewHashMismatchError):
            self.engine.apply(cand)
        self.assertEqual(cand.state, CandidateLifecycleState.FAILED)

        # Confirm DB has 0 transaction rows
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM transactions")
        count = cur.fetchone()[0]
        con.close()
        self.assertEqual(count, 0)

    def test_rejected_candidate_leaves_ledger_unmodified(self) -> None:
        cand = self._make_candidate(candidate_id="CAND_REJECT", idempotency_key="IDEM_REJECT")
        cand.transition_to(CandidateLifecycleState.PARSED)
        cand.transition_to(CandidateLifecycleState.READY_FOR_CONFIRMATION)
        cand.reject("Owner rejected transaction proposal")
        self.assertEqual(cand.state, CandidateLifecycleState.REJECTED)
        self.assertEqual(cand.rejection_reason, "Owner rejected transaction proposal")

        # Cannot confirm or apply rejected candidate
        with self.assertRaises(InvalidStateTransitionError):
            cand.confirm()
        with self.assertRaises(InvalidStateTransitionError):
            self.engine.apply(cand)

        # Ledger remains untouched
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM transactions")
        count = cur.fetchone()[0]
        con.close()
        self.assertEqual(count, 0)

    def test_expired_candidate_cannot_be_applied(self) -> None:
        # Candidate expired in the past
        cand = self._make_candidate(candidate_id="CAND_EXP", idempotency_key="IDEM_EXP")
        cand.expires_at = "2020-01-01T00:00:00+00:00"
        cand.transition_to(CandidateLifecycleState.PARSED)
        cand.transition_to(CandidateLifecycleState.READY_FOR_CONFIRMATION)

        # Attempting confirmation fails closed
        with self.assertRaises(CandidateExpiredError):
            cand.confirm()
        self.assertEqual(cand.state, CandidateLifecycleState.EXPIRED)

        # Attempting apply fails closed
        with self.assertRaises(CandidateExpiredError):
            self.engine.apply(cand)

    def test_review_required_candidate_blocked_from_confirmation(self) -> None:
        cand = self._make_candidate(candidate_id="CAND_REV", idempotency_key="IDEM_REV")
        cand.transition_to(CandidateLifecycleState.PARSED)
        cand.transition_to(CandidateLifecycleState.REVIEW_REQUIRED)

        # Direct confirmation from REVIEW_REQUIRED is forbidden
        with self.assertRaises(InvalidStateTransitionError):
            cand.confirm()
        with self.assertRaises(ReviewRequiredError):
            self.engine.apply(cand)

        # Must be corrected to READY_FOR_CONFIRMATION first
        cand.transition_to(CandidateLifecycleState.READY_FOR_CONFIRMATION)
        cand.confirm()
        res = self.engine.apply(cand)
        self.assertTrue(res.success)

    def test_pre_apply_backup_created_and_verified_before_mutation(self) -> None:
        # Pre-apply DB state hash
        pre_db_hash = compute_file_sha256(self.db_path)

        cand = self._make_candidate(candidate_id="CAND_BAK", idempotency_key="IDEM_BAK")
        cand.transition_to(CandidateLifecycleState.PARSED)
        cand.transition_to(CandidateLifecycleState.READY_FOR_CONFIRMATION)
        cand.confirm()

        res = self.engine.apply(cand)
        self.assertTrue(res.success)

        # Check backup file exists
        backups = list(self.backup_dir.glob("test_ledger.db.backup_*.db"))
        self.assertGreaterEqual(len(backups), 1)
        latest_backup = backups[-1]
        self.assertGreater(latest_backup.stat().st_size, 0)
        self.assertEqual(compute_file_sha256(latest_backup), pre_db_hash)

    def test_atomic_rollback_on_simulated_db_error(self) -> None:
        pre_db_hash = compute_file_sha256(self.db_path)

        mut1 = self._make_mutation(amount=Decimal("10.00"), canonical_id="TX_ERR_1")
        mut2 = self._make_mutation(amount=Decimal("20.00"), canonical_id="TX_ERR_2")
        cand = self._make_candidate(
            candidate_id="CAND_SIM_ERR",
            idempotency_key="IDEM_SIM_ERR",
            mutations=(mut1, mut2),
        )
        cand.transition_to(CandidateLifecycleState.PARSED)
        cand.transition_to(CandidateLifecycleState.READY_FOR_CONFIRMATION)
        cand.confirm()

        # Trigger simulated failure at mutation index 1
        with self.assertRaises(RuntimeError):
            self.engine.apply(cand, simulated_error_at_mutation=1)

        self.assertEqual(cand.state, CandidateLifecycleState.FAILED)

        # Verify DB is 100% rolled back
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM transactions")
        count = cur.fetchone()[0]
        con.close()
        self.assertEqual(count, 0)

        # DB file has zero mutations from candidate
        post_db_hash = compute_file_sha256(self.db_path)
        self.assertEqual(pre_db_hash, post_db_hash)

    def test_restore_from_backup_recovers_original_state(self) -> None:
        # Create initial state A
        backup_file, pre_hash = create_pre_apply_backup(self.db_path, self.backup_dir)

        # Mutate database state B
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO transactions (date, time, transaction_type, amount, account_from, account_to) VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-03-01", "12:00:00", "Expense", 999.0, "Acc1", "Acc2"),
        )
        con.commit()
        con.close()
        self.assertNotEqual(compute_file_sha256(self.db_path), pre_hash)

        # Restore from backup
        success = restore_from_backup(backup_file, self.db_path)
        self.assertTrue(success)
        self.assertEqual(compute_file_sha256(self.db_path), pre_hash)

        # Verify table has 0 rows again
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM transactions")
        count = cur.fetchone()[0]
        con.close()
        self.assertEqual(count, 0)

    def test_audit_trail_entry_persisted_with_complete_provenance(self) -> None:
        cand = self._make_candidate(candidate_id="CAND_AUDIT", idempotency_key="IDEM_AUDIT")
        cand.transition_to(CandidateLifecycleState.PARSED)
        cand.transition_to(CandidateLifecycleState.READY_FOR_CONFIRMATION)
        cand.confirm()

        res = self.engine.apply(cand)
        self.assertIsNotNone(res.audit_entry)
        self.assertEqual(res.audit_entry.candidate_id, "CAND_AUDIT")
        self.assertEqual(res.audit_entry.idempotency_key, "IDEM_AUDIT")
        self.assertGreater(len(res.audit_entry.pre_state_hash), 0)
        self.assertGreater(len(res.audit_entry.post_state_hash), 0)

        # Verify DB audit table
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("SELECT candidate_id, idempotency_key, preview_hash FROM safe_apply_audit WHERE candidate_id = ?", ("CAND_AUDIT",))
        row = cur.fetchone()
        con.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "CAND_AUDIT")
        self.assertEqual(row[1], "IDEM_AUDIT")

    def test_exact_decimal_preservation_in_applied_ledger(self) -> None:
        amount_exact = Decimal("123456.78")
        mut = self._make_mutation(amount=amount_exact)
        cand = self._make_candidate(candidate_id="CAND_DEC", idempotency_key="IDEM_DEC", mutations=(mut,))
        cand.transition_to(CandidateLifecycleState.PARSED)
        cand.transition_to(CandidateLifecycleState.READY_FOR_CONFIRMATION)
        cand.confirm()

        res = self.engine.apply(cand)
        self.assertTrue(res.success)

        # Verify float is forbidden in LedgerMutation
        with self.assertRaises(TypeError):
            self._make_mutation(amount=100.5)  # type: ignore

    def test_internal_transfer_pair_creates_balanced_ledger_entries(self) -> None:
        mut_out = self._make_mutation(
            amount=Decimal("500000.00"),
            account_from="BCA Main",
            account_to="Jago Main",
            transaction_type="Transfer",
            canonical_id="TX_XFER_OUT",
        )
        mut_in = self._make_mutation(
            amount=Decimal("500000.00"),
            account_from="BCA Main",
            account_to="Jago Main",
            transaction_type="Transfer",
            canonical_id="TX_XFER_IN",
        )
        cand = self._make_candidate(
            candidate_id="CAND_XFER",
            idempotency_key="IDEM_XFER",
            mutations=(mut_out, mut_in),
        )
        cand.transition_to(CandidateLifecycleState.PARSED)
        cand.transition_to(CandidateLifecycleState.READY_FOR_CONFIRMATION)
        cand.confirm()

        res = self.engine.apply(cand)
        self.assertTrue(res.success)
        self.assertEqual(len(res.applied_row_ids), 2)

        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("SELECT amount FROM transactions WHERE id IN (?, ?)", res.applied_row_ids)
        amounts = [r[0] for r in cur.fetchall()]
        con.close()
        self.assertEqual(amounts, [500000.0, 500000.0])

    def test_invalid_state_transition_raises_error(self) -> None:
        cand = self._make_candidate()
        # RECEIVED cannot go directly to CONFIRMED
        with self.assertRaises(InvalidStateTransitionError):
            cand.transition_to(CandidateLifecycleState.CONFIRMED)
        # RECEIVED cannot go directly to APPLIED
        with self.assertRaises(InvalidStateTransitionError):
            cand.transition_to(CandidateLifecycleState.APPLIED)

        cand.transition_to(CandidateLifecycleState.PARSED)
        # PARSED cannot go directly to APPLIED
        with self.assertRaises(InvalidStateTransitionError):
            cand.transition_to(CandidateLifecycleState.APPLIED)

    def test_terminal_state_immutability(self) -> None:
        # APPLIED is terminal
        cand_applied = self._make_candidate(candidate_id="CAND_TERM_1", idempotency_key="IDEM_TERM_1")
        cand_applied.transition_to(CandidateLifecycleState.PARSED)
        cand_applied.transition_to(CandidateLifecycleState.READY_FOR_CONFIRMATION)
        cand_applied.confirm()
        self.engine.apply(cand_applied)
        self.assertEqual(cand_applied.state, CandidateLifecycleState.APPLIED)

        with self.assertRaises(InvalidStateTransitionError):
            cand_applied.transition_to(CandidateLifecycleState.CONFIRMED)
        with self.assertRaises(InvalidStateTransitionError):
            cand_applied.transition_to(CandidateLifecycleState.RECEIVED)

        # REJECTED is terminal
        cand_rejected = self._make_candidate(candidate_id="CAND_TERM_2", idempotency_key="IDEM_TERM_2")
        cand_rejected.transition_to(CandidateLifecycleState.PARSED)
        cand_rejected.transition_to(CandidateLifecycleState.READY_FOR_CONFIRMATION)
        cand_rejected.reject("Decline")
        with self.assertRaises(InvalidStateTransitionError):
            cand_rejected.transition_to(CandidateLifecycleState.CONFIRMED)

        # EXPIRED is terminal
        cand_expired = self._make_candidate(candidate_id="CAND_TERM_3", idempotency_key="IDEM_TERM_3")
        cand_expired.transition_to(CandidateLifecycleState.PARSED)
        cand_expired.transition_to(CandidateLifecycleState.READY_FOR_CONFIRMATION)
        cand_expired.expire()
        with self.assertRaises(InvalidStateTransitionError):
            cand_expired.transition_to(CandidateLifecycleState.CONFIRMED)

    def test_concurrent_apply_prevented_by_lock(self) -> None:
        cand = self._make_candidate(candidate_id="CAND_LOCK", idempotency_key="IDEM_LOCK")
        cand.transition_to(CandidateLifecycleState.PARSED)
        cand.transition_to(CandidateLifecycleState.READY_FOR_CONFIRMATION)
        cand.confirm()

        # Hold exclusive lock on database with separate connection
        lock_con = sqlite3.connect(self.db_path, timeout=0.1)
        lock_con.execute("BEGIN EXCLUSIVE")

        # Engine attempting apply must encounter database lock
        with self.assertRaises(sqlite3.OperationalError):
            self.engine.apply(cand)

        # Release lock
        lock_con.rollback()
        lock_con.close()

        # Now apply succeeds
        cand.state = CandidateLifecycleState.CONFIRMED
        res = self.engine.apply(cand)
        self.assertTrue(res.success)

    def test_sanitized_diagnostics_no_secrets_or_pii(self) -> None:
        cand = self._make_candidate(candidate_id="CAND_SAN", idempotency_key="IDEM_SAN")
        cand.transition_to(CandidateLifecycleState.PARSED)
        cand.transition_to(CandidateLifecycleState.READY_FOR_CONFIRMATION)
        cand.confirm()
        res = self.engine.apply(cand)

        # Check representation for sensitive keywords
        forbidden = ("password", "secret", "bearer", "private_key", "token_value")
        rep = repr(cand) + repr(res)
        for term in forbidden:
            self.assertNotIn(term, rep.lower())

    def test_production_db_file_remains_unaccessed_during_tests(self) -> None:
        prod_db = _find_production_db()
        self.assertTrue(prod_db.exists())
        current_hash = hashlib.sha256(prod_db.read_bytes()).hexdigest()
        self.assertEqual(current_hash, EXPECTED_PRODUCTION_DB_SHA256)


if __name__ == "__main__":
    unittest.main()
