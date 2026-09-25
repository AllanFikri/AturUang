"""
AturUang Stage 8 — Controlled Production Pilot & Progressive Backfill Harness Tests.

Comprehensive testing of:
1. Closed-month boundary isolation and single-month dry run.
2. Fail-closed checkpoint gates blocking on balance discrepancies or unresolved review items.
3. Sequential progressive backfill month-locking.
4. Pre-apply native SQLite backup creation and verification.
5. Atomic safe apply execution and audit trail generation.
6. Strict zero-silent-adjustment assertion.
7. Deterministic double-run replay proof with 0 new ledger writes.
8. Orphan backup prevention on idempotent replay.
9. Explicit rollback and restore recovery drills.
10. Multi-account statement balance conservation with Decimal precision.
11. Privacy-sanitized audit reporting without PII or absolute file paths.
12. Fail-closed protection against corrupt statement records.
13. Sequential two-month progressive historical backfill.
14. Assertion that production database remains completely untouched.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from aturuang.pilot_audit import (
    CorruptStatementError,
    IdempotencyReplayError,
    PilotAuditError,
    PilotAuditor,
    ReplayVerificationResult,
    StatementConservationError,
    StatementControlRecord,
    ZeroSilentAdjustmentError,
    sanitize_account_identifier,
    sanitize_text,
    to_decimal,
)
from aturuang.pilot_orchestrator import (
    CheckpointGateError,
    MonthBoundaryViolationError,
    MonthLockError,
    PilotMonthBatch,
    PilotOrchestrator,
    PilotStage,
    PostApplyAuditError,
    ReviewRequiredHaltError,
)
from aturuang.safe_apply import (
    ApplyCandidate,
    ApplyResult,
    CandidateLifecycleState,
    LedgerMutation,
    SafeApplyEngine,
)

EXPECTED_PRODUCTION_DB_SHA256 = "341c5f348ac3cd82732e1067f54074ea372cf9f2e9e87d491cc9f76cebe94c07"


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


def _compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


class TestStage8ProductionPilot(unittest.TestCase):
    """Test suite for Stage 8 Pilot Orchestrator and Pilot Auditor."""

    def setUp(self) -> None:
        # Verify production DB is present and unchanged before running each test
        self.prod_db_path = _find_production_db()
        self.assertEqual(
            _compute_sha256(self.prod_db_path),
            EXPECTED_PRODUCTION_DB_SHA256,
            "Production database altered before test execution!",
        )

        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.db_path = self.temp_path / "pilot_test.db"
        self.backup_dir = self.temp_path / "backups"
        self.engine = SafeApplyEngine(self.db_path, self.backup_dir)
        self.auditor = PilotAuditor(self.db_path)
        self.orchestrator = PilotOrchestrator(
            self.db_path, self.backup_dir, self.engine, self.auditor
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        # Verify production DB remains untouched after test execution
        self.assertEqual(
            _compute_sha256(self.prod_db_path),
            EXPECTED_PRODUCTION_DB_SHA256,
            "Production database altered by test execution!",
        )

    def _make_mutation(
        self,
        date: str = "2026-01-15",
        time: str = "10:30:00",
        transaction_type: str = "Expense",
        amount: Decimal = Decimal("50000.00"),
        account_from: str = "BCA-12345",
        account_to: str = "Merchant External",
        description: str = "Test Mutation",
        status: str = "Confirmed",
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
            category="Food & Beverage",
            status=status,
            canonical_id=canonical_id or f"TX_{date}_{amount}_{account_from}_{account_to}",
        )

    def _make_candidate(
        self,
        candidate_id: str = "CAND_001",
        idempotency_key: str = "IDEM_001",
        mutations: Sequence[LedgerMutation] | None = None,
        state: CandidateLifecycleState = CandidateLifecycleState.CONFIRMED,
        evidence_keys: Sequence[str] = ("EV_001",),
    ) -> ApplyCandidate:
        if mutations is None:
            mutations = [self._make_mutation()]
        return ApplyCandidate(
            candidate_id=candidate_id,
            idempotency_key=idempotency_key,
            state=state,
            participating_evidence_keys=tuple(evidence_keys),
            mutations=tuple(mutations),
        )

    def _make_statement(
        self,
        account_id: str = "BCA-12345",
        month: str = "2026-01",
        opening_balance: Decimal = Decimal("1000000.00"),
        closing_balance: Decimal = Decimal("1250000.00"),
        total_inflow: Decimal = Decimal("500000.00"),
        total_outflow: Decimal = Decimal("250000.00"),
        raw_path: str | None = None,
        owner_pii: str | None = None,
    ) -> StatementControlRecord:
        return StatementControlRecord(
            account_id=account_id,
            month=month,
            opening_balance=opening_balance,
            closing_balance=closing_balance,
            total_inflow=total_inflow,
            total_outflow=total_outflow,
            raw_path=raw_path,
            owner_pii=owner_pii,
        )

    # 1. test_pilot_single_month_dry_run
    def test_pilot_single_month_dry_run(self) -> None:
        """Ingests and stages a single closed month in isolation, verifying 0 DB mutations in dry run."""
        stmt = self._make_statement(
            account_id="BCA-12345",
            month="2026-01",
            opening_balance=Decimal("1000000.00"),
            closing_balance=Decimal("1250000.00"),
            total_inflow=Decimal("500000.00"),
            total_outflow=Decimal("250000.00"),
        )
        cand1 = self._make_candidate(
            candidate_id="CAND_JAN_01",
            idempotency_key="IDEM_JAN_01",
            mutations=[
                self._make_mutation(
                    date="2026-01-05",
                    transaction_type="Income",
                    amount=Decimal("500000.00"),
                    account_from="",
                    account_to="BCA-12345",
                    description="Salary",
                )
            ],
        )
        cand2 = self._make_candidate(
            candidate_id="CAND_JAN_02",
            idempotency_key="IDEM_JAN_02",
            mutations=[
                self._make_mutation(
                    date="2026-01-10",
                    transaction_type="Expense",
                    amount=Decimal("250000.00"),
                    account_from="BCA-12345",
                    account_to="Supermarket",
                    description="Groceries",
                )
            ],
        )

        batch = self.orchestrator.register_month("2026-01", [cand1, cand2], [stmt])
        self.assertEqual(batch.stage, PilotStage.PLANNED)

        dry_run = self.orchestrator.run_dry_run("2026-01")
        self.assertTrue(dry_run["is_clean"])
        self.assertEqual(batch.stage, PilotStage.OWNER_REVIEW)
        self.assertEqual(dry_run["total_candidates"], 2)
        self.assertEqual(dry_run["total_mutations"], 2)
        self.assertEqual(len(dry_run["discrepancies"]), 0)

        # Assert no rows were written to the database ledger during dry run
        con = sqlite3.connect(self.db_path)
        try:
            count = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            self.assertEqual(count, 0)
        finally:
            con.close()

    # 2. test_pilot_checkpoint_gate_blocks_on_discrepancy
    def test_pilot_checkpoint_gate_blocks_on_discrepancy(self) -> None:
        """Checkpoint gate blocks advancement when statement balance equation has a discrepancy."""
        # Statement has deliberate discrepancy: 1,000,000 + 500,000 - 250,000 = 1,250,000 != 1,300,000
        stmt = self._make_statement(
            account_id="BCA-12345",
            month="2026-01",
            opening_balance=Decimal("1000000.00"),
            closing_balance=Decimal("1300000.00"),  # Discrepancy of 50,000
            total_inflow=Decimal("500000.00"),
            total_outflow=Decimal("250000.00"),
        )
        cand = self._make_candidate(candidate_id="CAND_DISC", idempotency_key="IDEM_DISC")

        self.orchestrator.register_month("2026-01", [cand], [stmt])
        dry_run = self.orchestrator.run_dry_run("2026-01")
        self.assertFalse(dry_run["is_clean"])
        self.assertGreater(len(dry_run["discrepancies"]), 0)

        # Checkpoint gate must block execution
        with self.assertRaises(CheckpointGateError):
            self.orchestrator.execute_apply("2026-01")

        # Verify 0 ledger mutations written
        con = sqlite3.connect(self.db_path)
        try:
            count = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            self.assertEqual(count, 0)
        finally:
            con.close()

    # 3. test_pilot_month_advancement_locked_until_clean
    def test_pilot_month_advancement_locked_until_clean(self) -> None:
        """Month 2 cannot advance to apply before Month 1 is reconciled and applied."""
        stmt1 = self._make_statement(month="2026-01")
        cand1 = self._make_candidate(
            candidate_id="CAND_M1",
            idempotency_key="IDEM_M1",
            mutations=[
                self._make_mutation(
                    date="2026-01-10",
                    transaction_type="Income",
                    amount=Decimal("500000.00"),
                    account_to="BCA-12345",
                ),
                self._make_mutation(
                    date="2026-01-20",
                    transaction_type="Expense",
                    amount=Decimal("250000.00"),
                    account_from="BCA-12345",
                ),
            ],
        )

        stmt2 = self._make_statement(
            month="2026-02",
            opening_balance=Decimal("1250000.00"),
            closing_balance=Decimal("1350000.00"),
            total_inflow=Decimal("200000.00"),
            total_outflow=Decimal("100000.00"),
        )
        cand2 = self._make_candidate(
            candidate_id="CAND_M2",
            idempotency_key="IDEM_M2",
            mutations=[
                self._make_mutation(
                    date="2026-02-05",
                    transaction_type="Income",
                    amount=Decimal("200000.00"),
                    account_to="BCA-12345",
                ),
                self._make_mutation(
                    date="2026-02-15",
                    transaction_type="Expense",
                    amount=Decimal("100000.00"),
                    account_from="BCA-12345",
                ),
            ],
        )

        self.orchestrator.register_month("2026-01", [cand1], [stmt1])
        self.orchestrator.register_month("2026-02", [cand2], [stmt2])

        # Attempt to apply Month 2 while Month 1 is not yet applied
        with self.assertRaises(MonthLockError) as ctx:
            self.orchestrator.execute_apply("2026-02")
        self.assertIn("prior month '2026-01' is not clean and APPLIED", str(ctx.exception))

    # 4. test_pilot_pre_apply_backup_creation
    def test_pilot_pre_apply_backup_creation(self) -> None:
        """Native SQLite backup is created and verified prior to applying a batch."""
        stmt = self._make_statement(month="2026-01")
        cand = self._make_candidate(
            candidate_id="CAND_BK_01",
            idempotency_key="IDEM_BK_01",
            mutations=[
                self._make_mutation(
                    date="2026-01-10",
                    transaction_type="Income",
                    amount=Decimal("500000.00"),
                    account_to="BCA-12345",
                ),
                self._make_mutation(
                    date="2026-01-20",
                    transaction_type="Expense",
                    amount=Decimal("250000.00"),
                    account_from="BCA-12345",
                ),
            ],
        )

        self.orchestrator.register_month("2026-01", [cand], [stmt])
        res = self.orchestrator.execute_apply("2026-01")

        batch = self.orchestrator.batches["2026-01"]
        self.assertIsNotNone(batch.pre_apply_backup_path)
        self.assertTrue(batch.pre_apply_backup_path.exists())
        self.assertGreater(batch.pre_apply_backup_path.stat().st_size, 0)

        # Verify integrity of the backup file using PRAGMA quick_check
        con = sqlite3.connect(batch.pre_apply_backup_path)
        try:
            check = con.execute("PRAGMA quick_check").fetchone()
            self.assertEqual(check[0], "ok")
        finally:
            con.close()

    # 5. test_pilot_atomic_apply_execution
    def test_pilot_atomic_apply_execution(self) -> None:
        """Safe Apply engine writes batch mutations atomically with audit trail."""
        stmt = self._make_statement(month="2026-01")
        cand1 = self._make_candidate(
            candidate_id="CAND_ATM_1",
            idempotency_key="IDEM_ATM_1",
            mutations=[
                self._make_mutation(
                    date="2026-01-10",
                    transaction_type="Income",
                    amount=Decimal("500000.00"),
                    account_to="BCA-12345",
                    canonical_id="CANON_ATM_1",
                )
            ],
        )
        cand2 = self._make_candidate(
            candidate_id="CAND_ATM_2",
            idempotency_key="IDEM_ATM_2",
            mutations=[
                self._make_mutation(
                    date="2026-01-20",
                    transaction_type="Expense",
                    amount=Decimal("250000.00"),
                    account_from="BCA-12345",
                    canonical_id="CANON_ATM_2",
                )
            ],
        )

        self.orchestrator.register_month("2026-01", [cand1, cand2], [stmt])
        res = self.orchestrator.execute_apply("2026-01")
        self.assertTrue(res["success"])
        self.assertEqual(res["stage"], PilotStage.APPLIED.value)

        # Verify rows in ledger, idempotency table, and audit table
        con = sqlite3.connect(self.db_path)
        try:
            tx_count = con.execute("SELECT COUNT(*) FROM transactions WHERE is_deleted = 0").fetchone()[0]
            self.assertEqual(tx_count, 2)
            idem_count = con.execute("SELECT COUNT(*) FROM safe_apply_idempotency").fetchone()[0]
            self.assertEqual(idem_count, 2)
            audit_count = con.execute("SELECT COUNT(*) FROM safe_apply_audit").fetchone()[0]
            self.assertEqual(audit_count, 2)
        finally:
            con.close()

    # 6. test_pilot_zero_silent_adjustment_enforcement
    def test_pilot_zero_silent_adjustment_enforcement(self) -> None:
        """Rejects any hidden, synthetic, or balancing adjustment mutations."""
        stmt = self._make_statement(month="2026-01")
        cand_adj = self._make_candidate(
            candidate_id="CAND_SILENT_ADJ",
            idempotency_key="IDEM_SILENT_ADJ",
            mutations=[
                self._make_mutation(
                    date="2026-01-15",
                    transaction_type="Adjustment",  # Disallowed silent adjustment
                    amount=Decimal("10000.00"),
                    description="force balance adjustment",
                )
            ],
        )

        self.orchestrator.register_month("2026-01", [cand_adj], [stmt])

        # Dry run must catch the silent adjustment
        with self.assertRaises(ZeroSilentAdjustmentError) as ctx:
            self.orchestrator.run_dry_run("2026-01")
        self.assertIn("Silent balancing adjustment detected", str(ctx.exception))

    # 7. test_pilot_idempotency_replay_zero_mutation
    def test_pilot_idempotency_replay_zero_mutation(self) -> None:
        """Re-running an applied pilot month results in 0 additional writes to ledger."""
        stmt = self._make_statement(month="2026-01")
        cand = self._make_candidate(
            candidate_id="CAND_REPLAY_1",
            idempotency_key="IDEM_REPLAY_1",
            mutations=[
                self._make_mutation(
                    date="2026-01-10",
                    transaction_type="Income",
                    amount=Decimal("500000.00"),
                    account_to="BCA-12345",
                ),
                self._make_mutation(
                    date="2026-01-20",
                    transaction_type="Expense",
                    amount=Decimal("250000.00"),
                    account_from="BCA-12345",
                ),
            ],
        )

        self.orchestrator.register_month("2026-01", [cand], [stmt])
        self.orchestrator.execute_apply("2026-01")

        # Initial row count after apply
        con = sqlite3.connect(self.db_path)
        try:
            initial_count = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            self.assertEqual(initial_count, 2)
        finally:
            con.close()

        # Replay the entire month
        replay_res = self.orchestrator.replay_pilot_month("2026-01")
        self.assertTrue(replay_res.is_idempotent)
        self.assertEqual(replay_res.new_writes, 0)

        # Row count must remain unchanged
        con = sqlite3.connect(self.db_path)
        try:
            post_count = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            self.assertEqual(post_count, initial_count)
        finally:
            con.close()

    # 8. test_pilot_orphan_backup_prevention_on_replay
    def test_pilot_orphan_backup_prevention_on_replay(self) -> None:
        """Idempotent replay does not create any new or orphaned backup files."""
        stmt = self._make_statement(month="2026-01")
        cand = self._make_candidate(
            candidate_id="CAND_NO_ORPHAN",
            idempotency_key="IDEM_NO_ORPHAN",
            mutations=[
                self._make_mutation(
                    date="2026-01-10",
                    transaction_type="Income",
                    amount=Decimal("500000.00"),
                    account_to="BCA-12345",
                ),
                self._make_mutation(
                    date="2026-01-20",
                    transaction_type="Expense",
                    amount=Decimal("250000.00"),
                    account_from="BCA-12345",
                ),
            ],
        )

        self.orchestrator.register_month("2026-01", [cand], [stmt])
        self.orchestrator.execute_apply("2026-01")

        initial_backups = list(self.backup_dir.glob("*.db"))
        self.assertGreater(len(initial_backups), 0)

        # Execute idempotent replay
        replay_res = self.orchestrator.replay_pilot_month("2026-01")
        self.assertEqual(replay_res.new_backups, 0)

        post_backups = list(self.backup_dir.glob("*.db"))
        self.assertEqual(len(initial_backups), len(post_backups))

    # 9. test_pilot_rollback_and_restore_recovery_drill
    def test_pilot_rollback_and_restore_recovery_drill(self) -> None:
        """Post-apply audit failure automatically triggers native SQLite backup restoration."""
        stmt = self._make_statement(month="2026-01")
        cand = self._make_candidate(
            candidate_id="CAND_RECOVER",
            idempotency_key="IDEM_RECOVER",
            mutations=[
                self._make_mutation(
                    date="2026-01-10",
                    transaction_type="Income",
                    amount=Decimal("500000.00"),
                    account_to="BCA-12345",
                ),
                self._make_mutation(
                    date="2026-01-20",
                    transaction_type="Expense",
                    amount=Decimal("250000.00"),
                    account_from="BCA-12345",
                ),
            ],
        )

        # Seed pre-existing baseline state in database
        con = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            con.execute(
                "INSERT INTO transactions (canonical_id, date, transaction_type, amount, account_from, description) "
                "VALUES ('BASELINE_01', '2025-12-31', 'Income', 1000000.0, 'Initial', 'Baseline Setup')"
            )
        finally:
            con.close()

        self.orchestrator.register_month("2026-01", [cand], [stmt])

        # Patch post-apply audit to simulate a critical assertion failure
        with patch.object(
            self.auditor,
            "verify_zero_silent_adjustments",
            side_effect=[0, ZeroSilentAdjustmentError("Simulated post-apply audit failure")],
        ):
            with self.assertRaises(PostApplyAuditError):
                self.orchestrator.execute_apply("2026-01")

        # Batch stage must be ROLLED_BACK / FAILED
        batch = self.orchestrator.batches["2026-01"]
        self.assertEqual(batch.stage, PilotStage.FAILED)

        # Database must be cleanly restored to pre-apply state (only baseline transaction exists)
        con = sqlite3.connect(self.db_path)
        try:
            rows = con.execute("SELECT canonical_id FROM transactions WHERE is_deleted = 0").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0], "BASELINE_01")
        finally:
            con.close()

    # 10. test_pilot_multi_account_statement_conservation
    def test_pilot_multi_account_statement_conservation(self) -> None:
        """Validates multi-account balance conservation equations using Decimal arithmetic."""
        bca_stmt = self._make_statement(
            account_id="BCA-001",
            month="2026-01",
            opening_balance=Decimal("5000000.00"),
            closing_balance=Decimal("6000000.00"),
            total_inflow=Decimal("2000000.00"),
            total_outflow=Decimal("1000000.00"),
        )
        gopay_stmt = self._make_statement(
            account_id="GOPAY-001",
            month="2026-01",
            opening_balance=Decimal("500000.00"),
            closing_balance=Decimal("1200000.00"),
            total_inflow=Decimal("1000000.00"),
            total_outflow=Decimal("300000.00"),
        )
        blu_stmt = self._make_statement(
            account_id="BLU-001",
            month="2026-01",
            opening_balance=Decimal("1000000.00"),
            closing_balance=Decimal("500000.00"),
            total_inflow=Decimal("0.00"),
            total_outflow=Decimal("500000.00"),
        )

        mutations = [
            # BCA
            self._make_mutation(
                date="2026-01-05", transaction_type="Income", amount=Decimal("2000000.00"), account_to="BCA-001"
            ),
            self._make_mutation(
                date="2026-01-10", transaction_type="Expense", amount=Decimal("1000000.00"), account_from="BCA-001"
            ),
            # Gopay
            self._make_mutation(
                date="2026-01-08", transaction_type="Income", amount=Decimal("1000000.00"), account_to="GOPAY-001"
            ),
            self._make_mutation(
                date="2026-01-12", transaction_type="Expense", amount=Decimal("300000.00"), account_from="GOPAY-001"
            ),
            # Blu
            self._make_mutation(
                date="2026-01-20", transaction_type="Expense", amount=Decimal("500000.00"), account_from="BLU-001"
            ),
        ]

        results = self.auditor.verify_statement_conservation(
            [bca_stmt, gopay_stmt, blu_stmt], mutations, raise_on_error=True
        )
        self.assertEqual(len(results), 3)
        for r in results:
            self.assertTrue(r.is_conserved)
            self.assertEqual(r.discrepancy, Decimal("0.00"))

    # 11. test_pilot_unresolved_review_items_halt_apply
    def test_pilot_unresolved_review_items_halt_apply(self) -> None:
        """Candidates in REVIEW_REQUIRED state prevent batch application."""
        stmt = self._make_statement(month="2026-01")
        cand_review = self._make_candidate(
            candidate_id="CAND_NEEDS_REVIEW",
            idempotency_key="IDEM_NEEDS_REVIEW",
            state=CandidateLifecycleState.REVIEW_REQUIRED,
            mutations=[
                self._make_mutation(
                    date="2026-01-10",
                    transaction_type="Income",
                    amount=Decimal("500000.00"),
                    account_to="BCA-12345",
                ),
                self._make_mutation(
                    date="2026-01-20",
                    transaction_type="Expense",
                    amount=Decimal("250000.00"),
                    account_from="BCA-12345",
                ),
            ],
        )

        self.orchestrator.register_month("2026-01", [cand_review], [stmt])
        dry_run = self.orchestrator.run_dry_run("2026-01")
        self.assertFalse(dry_run["is_clean"])
        self.assertGreater(len(dry_run["review_items"]), 0)

        with self.assertRaises(ReviewRequiredHaltError):
            self.orchestrator.execute_apply("2026-01")

    # 12. test_pilot_sanitized_audit_summary
    def test_pilot_sanitized_audit_summary(self) -> None:
        """Asserts zero PII, email addresses, or absolute file paths appear in audit summary."""
        stmt = self._make_statement(
            account_id="BCA-987654321",
            month="2026-01",
            raw_path="C:\\Users\\allan\\private\\financial_stmt.pdf",
            owner_pii="Johnathan Doe, email: jdoe@private.domain, tel: 08123456789",
        )
        res = self.auditor.verify_statement_conservation([stmt])
        summary = self.auditor.generate_sanitized_audit_report(
            pilot_stage="STAGE_8_PRODUCTION_PILOT",
            month="2026-01",
            statements=[stmt],
            conservation_results=res,
            total_tx_count=10,
        )
        summary_json = json.dumps(summary)

        # Assert no PII leakage
        self.assertNotIn("Johnathan Doe", summary_json)
        self.assertNotIn("jdoe@private.domain", summary_json)
        self.assertNotIn("08123456789", summary_json)
        self.assertNotIn("Users", summary_json)
        self.assertNotIn("allan", summary_json)
        self.assertNotIn("financial_stmt.pdf", summary_json)

        # Assert account number is masked
        self.assertNotIn("987654321", summary_json)
        self.assertEqual(summary["status"], "PASSED")
        self.assertTrue(summary["balance_conservation_passed"])

    # 13. test_pilot_corrupt_statement_fails_closed
    def test_pilot_corrupt_statement_fails_closed(self) -> None:
        """Malformed or unparseable statement records fail closed and halt the batch."""
        # Non-numeric decimal
        with self.assertRaises(CorruptStatementError):
            StatementControlRecord(
                account_id="BCA-1",
                month="2026-01",
                opening_balance=Decimal("100"),
                closing_balance="NOT_A_NUMBER",  # type: ignore
                total_inflow=Decimal("10"),
                total_outflow=Decimal("5"),
            )

        # Negative total_inflow
        with self.assertRaises(CorruptStatementError):
            StatementControlRecord(
                account_id="BCA-1",
                month="2026-01",
                opening_balance=Decimal("100"),
                closing_balance=Decimal("110"),
                total_inflow=Decimal("-10.00"),
                total_outflow=Decimal("0.00"),
            )

        # Invalid month format
        with self.assertRaises(CorruptStatementError):
            StatementControlRecord(
                account_id="BCA-1",
                month="January 2026",
                opening_balance=Decimal("100"),
                closing_balance=Decimal("100"),
                total_inflow=Decimal("0"),
                total_outflow=Decimal("0"),
            )

    # 14. test_pilot_two_month_sequential_backfill
    def test_pilot_two_month_sequential_backfill(self) -> None:
        """Verifies progressive backfill through consecutive Month 1 -> Month 2 flow."""
        stmt1 = self._make_statement(month="2026-01")
        cand1 = self._make_candidate(
            candidate_id="CAND_M1",
            idempotency_key="IDEM_M1",
            mutations=[
                self._make_mutation(
                    date="2026-01-10",
                    transaction_type="Income",
                    amount=Decimal("500000.00"),
                    account_to="BCA-12345",
                    canonical_id="CANON_M1_1",
                ),
                self._make_mutation(
                    date="2026-01-20",
                    transaction_type="Expense",
                    amount=Decimal("250000.00"),
                    account_from="BCA-12345",
                    canonical_id="CANON_M1_2",
                ),
            ],
        )

        stmt2 = self._make_statement(
            month="2026-02",
            opening_balance=Decimal("1250000.00"),
            closing_balance=Decimal("1350000.00"),
            total_inflow=Decimal("200000.00"),
            total_outflow=Decimal("100000.00"),
        )
        cand2 = self._make_candidate(
            candidate_id="CAND_M2",
            idempotency_key="IDEM_M2",
            mutations=[
                self._make_mutation(
                    date="2026-02-05",
                    transaction_type="Income",
                    amount=Decimal("200000.00"),
                    account_to="BCA-12345",
                    canonical_id="CANON_M2_1",
                ),
                self._make_mutation(
                    date="2026-02-15",
                    transaction_type="Expense",
                    amount=Decimal("100000.00"),
                    account_from="BCA-12345",
                    canonical_id="CANON_M2_2",
                ),
            ],
        )

        self.orchestrator.register_month("2026-01", [cand1], [stmt1])
        self.orchestrator.register_month("2026-02", [cand2], [stmt2])

        # Apply Month 1
        res1 = self.orchestrator.execute_apply("2026-01")
        self.assertTrue(res1["success"])
        self.assertTrue(self.orchestrator.is_month_clean_and_reconciled("2026-01"))

        # Apply Month 2 now succeeds because Month 1 is clean and APPLIED
        res2 = self.orchestrator.execute_apply("2026-02")
        self.assertTrue(res2["success"])
        self.assertTrue(self.orchestrator.is_month_clean_and_reconciled("2026-02"))

        # Verify ledger has all 4 mutations
        con = sqlite3.connect(self.db_path)
        try:
            count = con.execute("SELECT COUNT(*) FROM transactions WHERE is_deleted = 0").fetchone()[0]
            self.assertEqual(count, 4)
        finally:
            con.close()

    # 15. test_production_db_untouched_during_stage8_tests
    def test_production_db_untouched_during_stage8_tests(self) -> None:
        """Asserts real production SQLite database SHA-256 remains strictly untouched."""
        prod_db = _find_production_db()
        current_hash = _compute_sha256(prod_db)
        self.assertEqual(
            current_hash,
            EXPECTED_PRODUCTION_DB_SHA256,
            f"Production DB hash mismatch: expected {EXPECTED_PRODUCTION_DB_SHA256}, got {current_hash}",
        )


if __name__ == "__main__":
    unittest.main()
