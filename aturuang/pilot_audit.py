"""
AturUang Stage 8 — Pilot Audit Module.

Reconciliation auditor, idempotency replay verifier, zero-silent-adjustment checker,
and privacy-sanitized reporting for closed-month production pilot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Sequence

from aturuang.safe_apply import (
    ApplyCandidate,
    ApplyResult,
    CandidateLifecycleState,
    LedgerMutation,
    SafeApplyEngine,
)

BALANCE_TOLERANCE = Decimal("0.005")


class PilotAuditError(Exception):
    """Base error for pilot audit operations."""


class ZeroSilentAdjustmentError(PilotAuditError):
    """Raised when an unauthorized or synthetic balancing adjustment is detected."""


class StatementConservationError(PilotAuditError):
    """Raised when statement balance conservation equation fails."""


class CorruptStatementError(PilotAuditError):
    """Raised when statement data is malformed, invalid, or unparseable."""


class IdempotencyReplayError(PilotAuditError):
    """Raised when idempotency replay fails or attempts new ledger writes."""


def to_decimal(val: Any) -> Decimal:
    """Converts a value to Decimal safely, raising CorruptStatementError on invalid values."""
    if val is None:
        raise CorruptStatementError("Amount value cannot be None")
    if isinstance(val, Decimal):
        return val
    try:
        s = str(val).strip()
        if not s:
            raise CorruptStatementError("Amount cannot be empty string")
        # Ensure it's parseable as Decimal
        d = Decimal(s)
        if not d.is_finite():
            raise CorruptStatementError(f"Amount must be a finite number: {val}")
        return d
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise CorruptStatementError(f"Invalid decimal value: '{val}'") from exc


def sanitize_account_identifier(account_id: str) -> str:
    """Masks numeric sequences in account identifiers to prevent PII leakage."""
    if not account_id:
        return "UNKNOWN_ACCOUNT"
    # Mask any numeric sequence of 5 or more digits (e.g. BCA-12345678 -> BCA-12***78)
    def _mask(match: re.Match) -> str:
        s = match.group()
        if len(s) <= 4:
            return s
        return s[:2] + "***" + s[-2:]
    return re.sub(r"\d{5,}", _mask, account_id)


def sanitize_text(text: str) -> str:
    """Removes file paths, email addresses, and potential PII patterns from text."""
    if not text:
        return ""
    # Remove email addresses
    t = re.sub(r"[\w\.-]+@[\w\.-]+", "[EMAIL_REDACTED]", text)
    # Remove Windows drive paths (e.g. C:\Users\... or C:/Users/...)
    t = re.sub(r"[a-zA-Z]:[\\/][^\s,]+", "[PATH_REDACTED]", t)
    # Remove Unix absolute paths (e.g. /home/... or /var/...)
    t = re.sub(r"(?:/[a-zA-Z0-9_\.-]+){2,}", "[PATH_REDACTED]", t)
    return t


@dataclass(frozen=True)
class StatementControlRecord:
    """Account statement control record for a closed calendar month."""

    account_id: str
    month: str  # YYYY-MM
    opening_balance: Decimal
    closing_balance: Decimal
    total_inflow: Decimal
    total_outflow: Decimal
    account_name: str | None = None
    currency: str = "IDR"
    owner_pii: str | None = None
    raw_path: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.account_id, str) or not self.account_id.strip():
            raise CorruptStatementError("account_id must be non-empty string")
        if not isinstance(self.month, str) or not re.match(r"^\d{4}-\d{2}$", self.month):
            raise CorruptStatementError(f"month must be in YYYY-MM format, got '{self.month}'")
        object.__setattr__(self, "opening_balance", to_decimal(self.opening_balance))
        object.__setattr__(self, "closing_balance", to_decimal(self.closing_balance))
        object.__setattr__(self, "total_inflow", to_decimal(self.total_inflow))
        object.__setattr__(self, "total_outflow", to_decimal(self.total_outflow))

        if self.total_inflow < Decimal("0.00"):
            raise CorruptStatementError(f"total_inflow cannot be negative: {self.total_inflow}")
        if self.total_outflow < Decimal("0.00"):
            raise CorruptStatementError(f"total_outflow cannot be negative: {self.total_outflow}")

    @property
    def calculated_closing(self) -> Decimal:
        """Calculates expected closing balance based on statement equation: Opening + Inflow - Outflow."""
        return self.opening_balance + self.total_inflow - self.total_outflow

    @property
    def discrepancy(self) -> Decimal:
        """Absolute discrepancy between declared closing balance and calculated closing balance."""
        return abs(self.closing_balance - self.calculated_closing)

    def is_conserved(self) -> bool:
        """Asserts closing balance equation conservation under tolerance."""
        return self.discrepancy < BALANCE_TOLERANCE


@dataclass(frozen=True)
class StatementConservationResult:
    """Outcome of statement balance conservation verification."""

    account_id: str
    month: str
    is_conserved: bool
    opening_balance: Decimal
    closing_balance: Decimal
    calculated_closing: Decimal
    discrepancy: Decimal
    total_inflow: Decimal
    total_outflow: Decimal
    actual_inflow_sum: Decimal = Decimal("0.00")
    actual_outflow_sum: Decimal = Decimal("0.00")
    mutation_count: int = 0
    message: str = ""


@dataclass(frozen=True)
class ReplayVerificationResult:
    """Outcome of deterministic idempotency double-run replay proof."""

    is_idempotent: bool
    total_candidates: int
    replayed_candidates: int
    new_writes: int
    new_backups: int
    verified_at: str
    message: str = ""


@dataclass(frozen=True)
class SanitizedAuditSummary:
    """Privacy-sanitized machine-readable audit report for pilot backfill."""

    pilot_stage: str
    month: str
    status: str
    total_transactions: int
    total_inflow: str
    total_outflow: str
    net_change: str
    accounts: list[dict[str, Any]]
    balance_conservation_passed: bool
    zero_silent_adjustments_passed: bool
    idempotency_replay_passed: bool
    audit_timestamp: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pilot_stage": self.pilot_stage,
            "month": self.month,
            "status": self.status,
            "total_transactions": self.total_transactions,
            "total_inflow": self.total_inflow,
            "total_outflow": self.total_outflow,
            "net_change": self.net_change,
            "accounts": self.accounts,
            "balance_conservation_passed": self.balance_conservation_passed,
            "zero_silent_adjustments_passed": self.zero_silent_adjustments_passed,
            "idempotency_replay_passed": self.idempotency_replay_passed,
            "audit_timestamp": self.audit_timestamp,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


class PilotAuditor:
    """Reconciliation auditor, idempotency verifier, and zero-silent-adjustment enforcer."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else None

    def verify_statement_conservation(
        self,
        statements: Sequence[StatementControlRecord],
        mutations: Sequence[LedgerMutation] | None = None,
        *,
        raise_on_error: bool = False,
    ) -> list[StatementConservationResult]:
        """Validates statement balance conservation equations and cross-checks mutations if supplied."""
        results: list[StatementConservationResult] = []

        for stmt in statements:
            if not isinstance(stmt, StatementControlRecord):
                raise CorruptStatementError("Statement must be an instance of StatementControlRecord")

            # 1. Check internal statement conservation
            internal_conserved = stmt.is_conserved()
            discrepancy = stmt.discrepancy

            actual_inflow = Decimal("0.00")
            actual_outflow = Decimal("0.00")
            mutation_count = 0

            # 2. Check mutations if supplied
            if mutations is not None:
                for m in mutations:
                    matched = False
                    # Inflow: Income to account_to or Transfer to account_to
                    if m.transaction_type == "Income" and (
                        m.account_to == stmt.account_id or (m.account_from == stmt.account_id and not m.account_to)
                    ):
                        actual_inflow += m.amount
                        matched = True
                    elif m.transaction_type == "Expense" and m.account_from == stmt.account_id:
                        actual_outflow += m.amount
                        matched = True
                    elif m.transaction_type == "Transfer":
                        if m.account_to == stmt.account_id:
                            actual_inflow += m.amount
                            matched = True
                        if m.account_from == stmt.account_id:
                            actual_outflow += m.amount
                            matched = True
                    if matched:
                        mutation_count += 1

                inflow_diff = abs(actual_inflow - stmt.total_inflow)
                outflow_diff = abs(actual_outflow - stmt.total_outflow)
                mutations_match = (inflow_diff < BALANCE_TOLERANCE) and (outflow_diff < BALANCE_TOLERANCE)
                conserved = internal_conserved and mutations_match
                if not mutations_match and internal_conserved:
                    discrepancy = max(inflow_diff, outflow_diff)
            else:
                conserved = internal_conserved

            msg = "Conserved" if conserved else f"Balance mismatch: discrepancy {discrepancy:.2f}"
            res = StatementConservationResult(
                account_id=stmt.account_id,
                month=stmt.month,
                is_conserved=conserved,
                opening_balance=stmt.opening_balance,
                closing_balance=stmt.closing_balance,
                calculated_closing=stmt.calculated_closing,
                discrepancy=discrepancy,
                total_inflow=stmt.total_inflow,
                total_outflow=stmt.total_outflow,
                actual_inflow_sum=actual_inflow,
                actual_outflow_sum=actual_outflow,
                mutation_count=mutation_count,
                message=msg,
            )
            results.append(res)

            if raise_on_error and not conserved:
                raise StatementConservationError(
                    f"Statement conservation failed for account '{stmt.account_id}' in month '{stmt.month}': {msg}"
                )

        return results

    def verify_zero_silent_adjustments(
        self,
        db_path: Path | str | None = None,
        month: str | None = None,
        candidate_mutations: Sequence[LedgerMutation] | None = None,
    ) -> int:
        """Strictly asserts that no synthetic balancing adjustments exist in mutations or ledger."""
        silent_tokens = (
            "synthetic adjustment",
            "force balance",
            "balancing adjustment",
            "auto-balancing",
            "dummy adjustment",
            "plug figure",
            "silent adjustment",
        )

        # 1. Check in-memory candidate mutations
        if candidate_mutations is not None:
            for m in candidate_mutations:
                if m.transaction_type == "Adjustment":
                    raise ZeroSilentAdjustmentError(
                        f"Silent balancing adjustment detected in mutation: type='{m.transaction_type}', "
                        f"amount={m.amount}, description='{m.description}'"
                    )
                desc_lower = (m.description + " " + m.notes).lower()
                for token in silent_tokens:
                    if token in desc_lower:
                        raise ZeroSilentAdjustmentError(
                            f"Synthetic balancing token '{token}' detected in mutation description/notes: '{m.description}'"
                        )

        # 2. Check in SQLite database ledger if db_path is provided
        target_db = Path(db_path) if db_path else self.db_path
        if target_db and target_db.exists():
            con = sqlite3.connect(target_db, timeout=2.0)
            try:
                cur = con.cursor()
                query = "SELECT id, date, transaction_type, amount, description, notes FROM transactions WHERE is_deleted = 0"
                params: list[Any] = []
                if month:
                    query += " AND date LIKE ?"
                    params.append(f"{month}%")

                cur.execute(query, tuple(params))
                for row_id, r_date, t_type, r_amount, r_desc, r_notes in cur.fetchall():
                    if t_type == "Adjustment":
                        raise ZeroSilentAdjustmentError(
                            f"Silent balancing adjustment detected in ledger row {row_id}: type='{t_type}', "
                            f"amount={r_amount}, description='{r_desc}'"
                        )
                    desc_lower = ((r_desc or "") + " " + (r_notes or "")).lower()
                    for token in silent_tokens:
                        if token in desc_lower:
                            raise ZeroSilentAdjustmentError(
                                f"Synthetic balancing token '{token}' detected in ledger row {row_id}: '{r_desc}'"
                            )
            finally:
                con.close()

        return 0

    def verify_idempotent_replay(
        self,
        engine: SafeApplyEngine,
        candidates: Sequence[ApplyCandidate],
    ) -> ReplayVerificationResult:
        """Re-runs pilot candidates against the applied ledger and asserts 0 writes and 0 backups."""
        if not engine.db_path.exists():
            raise FileNotFoundError(f"Database not found: {engine.db_path}")

        # Measure pre-replay state
        con = sqlite3.connect(engine.db_path, timeout=2.0)
        try:
            cur = con.cursor()
            cur.execute("SELECT COUNT(*) FROM transactions")
            pre_row_count = cur.fetchone()[0]
        finally:
            con.close()

        pre_backups = list(engine.backup_dir.glob("*.db")) if engine.backup_dir.exists() else []
        pre_backup_count = len(pre_backups)

        replayed_count = 0
        for cand in candidates:
            replay_cand = ApplyCandidate(
                candidate_id=cand.candidate_id,
                idempotency_key=cand.idempotency_key,
                state=CandidateLifecycleState.CONFIRMED,
                participating_evidence_keys=cand.participating_evidence_keys,
                mutations=cand.mutations,
                preview_hash=cand.preview_hash,
                expires_at=cand.expires_at,
            )
            result: ApplyResult = engine.apply(replay_cand)
            if not result.is_idempotent_replay:
                raise IdempotencyReplayError(
                    f"Candidate {cand.candidate_id} was expected to be an idempotent replay, but was not"
                )
            replayed_count += 1

        # Measure post-replay state
        con = sqlite3.connect(engine.db_path, timeout=2.0)
        try:
            cur = con.cursor()
            cur.execute("SELECT COUNT(*) FROM transactions")
            post_row_count = cur.fetchone()[0]
        finally:
            con.close()

        post_backups = list(engine.backup_dir.glob("*.db")) if engine.backup_dir.exists() else []
        post_backup_count = len(post_backups)

        new_writes = post_row_count - pre_row_count
        new_backups = post_backup_count - pre_backup_count

        if new_writes != 0:
            raise IdempotencyReplayError(
                f"Idempotency replay violated: {new_writes} new ledger writes occurred"
            )
        if new_backups != 0:
            raise IdempotencyReplayError(
                f"Orphan backup prevention violated: {new_backups} new backup files were created during replay"
            )

        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        return ReplayVerificationResult(
            is_idempotent=True,
            total_candidates=len(candidates),
            replayed_candidates=replayed_count,
            new_writes=0,
            new_backups=0,
            verified_at=now_iso,
            message="Double-run replay proof verified: 0 new ledger writes, 0 orphaned backups",
        )

    def generate_sanitized_audit_report(
        self,
        pilot_stage: str,
        month: str,
        statements: Sequence[StatementControlRecord],
        conservation_results: Sequence[StatementConservationResult],
        total_tx_count: int,
        silent_adj_passed: bool = True,
        replay_passed: bool = True,
    ) -> dict[str, Any]:
        """Generates a structured, privacy-sanitized audit summary dictionary without PII or raw paths."""
        all_conserved = all(cr.is_conserved for cr in conservation_results) if conservation_results else True
        status = "PASSED" if (all_conserved and silent_adj_passed and replay_passed) else "FAILED"

        total_inflow = sum((s.total_inflow for s in statements), Decimal("0.00"))
        total_outflow = sum((s.total_outflow for s in statements), Decimal("0.00"))
        net_change = total_inflow - total_outflow

        sanitized_accounts: list[dict[str, Any]] = []
        for s in statements:
            sanitized_id = sanitize_account_identifier(s.account_id)
            cr = next((r for r in conservation_results if r.account_id == s.account_id), None)
            is_cons = cr.is_conserved if cr else s.is_conserved()
            disc = cr.discrepancy if cr else s.discrepancy
            sanitized_accounts.append({
                "account_id": sanitized_id,
                "opening_balance": f"{s.opening_balance:.2f}",
                "closing_balance": f"{s.closing_balance:.2f}",
                "total_inflow": f"{s.total_inflow:.2f}",
                "total_outflow": f"{s.total_outflow:.2f}",
                "is_conserved": is_cons,
                "discrepancy": f"{disc:.2f}",
            })

        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        summary = SanitizedAuditSummary(
            pilot_stage=sanitize_text(pilot_stage),
            month=month,
            status=status,
            total_transactions=total_tx_count,
            total_inflow=f"{total_inflow:.2f}",
            total_outflow=f"{total_outflow:.2f}",
            net_change=f"{net_change:.2f}",
            accounts=sanitized_accounts,
            balance_conservation_passed=all_conserved,
            zero_silent_adjustments_passed=silent_adj_passed,
            idempotency_replay_passed=replay_passed,
            audit_timestamp=now_iso,
        )
        return summary.to_dict()
