"""
Universal Ingestion Stage 6-R1 — Safe Review, Apply Gate, and Recovery Engine.

Provides candidate lifecycle state machine, deterministic preview hash binding,
pre-apply automated backups via native SQLite Online Backup API, autocommit isolation
(isolation_level=None) with explicit BEGIN IMMEDIATE transactions, strict idempotency enforcement,
immutable audit trails, and backup restore recovery drills.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
from decimal import Decimal
from enum import Enum
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence
import uuid

SAFE_APPLY_CONTRACT_VERSION: str = "truth-loop-safe-apply-v1"


def _require_decimal(value: Any, field_name: str) -> Decimal:
    """Enforces pure Decimal exact arithmetic and forbids floating-point values."""
    if isinstance(value, float):
        raise TypeError(f"{field_name} must not be float; exact Decimal required")
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{field_name} must be finite Decimal")
    return value


# Register SQLite Decimal adapter and converter
sqlite3.register_adapter(Decimal, lambda d: f"{d:.2f}")


def compute_file_sha256(path: Path | str) -> str:
    """Computes SHA-256 hex digest of a file in 64KB blocks."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {p}")
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class CandidateLifecycleState(str, Enum):
    """Explicit state transitions as defined in Grand Design V2.1."""

    RECEIVED = "RECEIVED"
    PARSED = "PARSED"
    READY_FOR_CONFIRMATION = "READY_FOR_CONFIRMATION"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    CONFIRMED = "CONFIRMED"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


LIFECYCLE_STATES_COUNT: int = len(CandidateLifecycleState)

TERMINAL_STATES: frozenset[CandidateLifecycleState] = frozenset({
    CandidateLifecycleState.APPLIED,
    CandidateLifecycleState.REJECTED,
    CandidateLifecycleState.EXPIRED,
})

VALID_TRANSITIONS: dict[CandidateLifecycleState, set[CandidateLifecycleState]] = {
    CandidateLifecycleState.RECEIVED: {
        CandidateLifecycleState.PARSED,
    },
    CandidateLifecycleState.PARSED: {
        CandidateLifecycleState.READY_FOR_CONFIRMATION,
        CandidateLifecycleState.REVIEW_REQUIRED,
    },
    CandidateLifecycleState.REVIEW_REQUIRED: {
        CandidateLifecycleState.READY_FOR_CONFIRMATION,
    },
    CandidateLifecycleState.READY_FOR_CONFIRMATION: {
        CandidateLifecycleState.CONFIRMED,
        CandidateLifecycleState.REJECTED,
        CandidateLifecycleState.EXPIRED,
    },
    CandidateLifecycleState.CONFIRMED: {
        CandidateLifecycleState.APPLIED,
        CandidateLifecycleState.FAILED,
    },
    CandidateLifecycleState.FAILED: {
        CandidateLifecycleState.CONFIRMED,
        CandidateLifecycleState.READY_FOR_CONFIRMATION,
    },
    CandidateLifecycleState.APPLIED: set(),
    CandidateLifecycleState.REJECTED: set(),
    CandidateLifecycleState.EXPIRED: set(),
}


class SafeApplyError(Exception):
    """Base exception for Safe Apply operations."""


class InvalidStateTransitionError(SafeApplyError):
    """Raised when an illegal lifecycle state transition is attempted."""


class PreviewHashMismatchError(SafeApplyError):
    """Raised when preview hash does not match recalculated candidate payload."""


class IdempotencyConflictError(SafeApplyError):
    """Raised when idempotency key is reused with a conflicting payload."""


class CandidateExpiredError(SafeApplyError):
    """Raised when an expired candidate is attempted to be confirmed or applied."""


class ReviewRequiredError(SafeApplyError):
    """Raised when a candidate requiring review is attempted to be confirmed or applied without resolution."""


class BackupError(SafeApplyError):
    """Raised when pre-apply backup creation or verification fails."""


@dataclass(frozen=True)
class LedgerMutation:
    """Atomic mutation to be applied to the transactions ledger."""

    date: str
    time: str
    transaction_type: str
    amount: Decimal
    account_from: str
    account_to: str
    description: str = ""
    category: str = ""
    for_with_whom: str = "Personal / Self"
    money_context: str = "Personal"
    status: str = "Confirmed"
    canonical_id: str | None = None
    notes: str = ""
    source_refs: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.date, str) or not self.date:
            raise ValueError("date must be non-empty string YYYY-MM-DD")
        if not isinstance(self.time, str):
            raise ValueError("time must be string")
        if not isinstance(self.transaction_type, str) or self.transaction_type not in (
            "Income", "Expense", "Transfer", "Adjustment"
        ):
            raise ValueError("transaction_type must be Income, Expense, Transfer, or Adjustment")
        object.__setattr__(self, "amount", _require_decimal(self.amount, "amount"))
        if self.amount < Decimal("0.00"):
            raise ValueError("amount must be non-negative")
        if not isinstance(self.account_from, str):
            raise ValueError("account_from must be string")
        if not isinstance(self.account_to, str):
            raise ValueError("account_to must be string")


def compute_preview_hash(
    mutations: Sequence[LedgerMutation],
    participating_evidence_keys: Sequence[str],
    candidate_id: str = "",
) -> str:
    """Computes a deterministic SHA-256 preview hash binding participating evidence keys, amounts, accounts, dates, and types."""
    sorted_keys = sorted(participating_evidence_keys)
    serialized_mutations: list[dict[str, Any]] = []
    for m in mutations:
        serialized_mutations.append({
            "date": m.date,
            "time": m.time,
            "transaction_type": m.transaction_type,
            "amount": f"{m.amount:.2f}",
            "account_from": m.account_from,
            "account_to": m.account_to,
            "description": m.description,
            "category": m.category,
            "for_with_whom": m.for_with_whom,
            "money_context": m.money_context,
            "status": m.status,
            "canonical_id": m.canonical_id or "",
            "notes": m.notes,
            "source_refs": m.source_refs,
        })
    # Deterministic sorting of mutations
    serialized_mutations.sort(
        key=lambda x: (
            x["date"],
            x["time"],
            x["account_from"],
            x["account_to"],
            x["amount"],
            x["transaction_type"],
            x["canonical_id"],
        )
    )
    payload = {
        "candidate_id": candidate_id,
        "evidence_keys": sorted_keys,
        "mutations": serialized_mutations,
    }
    dumped = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


@dataclass
class ApplyCandidate:
    """Represents a candidate for ingestion review and ledger application."""

    candidate_id: str
    idempotency_key: str
    state: CandidateLifecycleState = CandidateLifecycleState.RECEIVED
    participating_evidence_keys: tuple[str, ...] = ()
    mutations: tuple[LedgerMutation, ...] = ()
    preview_hash: str = ""
    expires_at: str | None = None
    created_at: str = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())
    rejection_reason: str | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, str) or not self.candidate_id:
            raise ValueError("candidate_id must be non-empty string")
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key:
            raise ValueError("idempotency_key must be non-empty string")
        if not isinstance(self.state, CandidateLifecycleState):
            raise TypeError("state must be CandidateLifecycleState")
        self.participating_evidence_keys = tuple(sorted(self.participating_evidence_keys))
        if not self.preview_hash and self.mutations:
            self.preview_hash = compute_preview_hash(
                self.mutations, self.participating_evidence_keys, self.candidate_id
            )

    def transition_to(self, target_state: CandidateLifecycleState, reason: str = "") -> None:
        """Executes explicit, verified state transitions enforcing terminal boundaries."""
        if not isinstance(target_state, CandidateLifecycleState):
            raise TypeError(f"target_state must be CandidateLifecycleState, got {type(target_state)}")

        if self.state in TERMINAL_STATES:
            if self.state == target_state:
                return
            raise InvalidStateTransitionError(
                f"Cannot transition out of terminal state '{self.state.value}' to '{target_state.value}'"
            )

        valid_targets = VALID_TRANSITIONS.get(self.state, set())
        if target_state not in valid_targets:
            raise InvalidStateTransitionError(
                f"Illegal state transition from '{self.state.value}' to '{target_state.value}'"
            )

        self.state = target_state
        self.updated_at = dt.datetime.now(dt.timezone.utc).isoformat()
        if target_state == CandidateLifecycleState.REJECTED and reason:
            self.rejection_reason = reason
        elif target_state == CandidateLifecycleState.FAILED and reason:
            self.failure_reason = reason

    def confirm(self) -> None:
        """Confirms candidate after owner approval."""
        if self.is_expired():
            self.transition_to(CandidateLifecycleState.EXPIRED, "Confirmation token timeout")
            raise CandidateExpiredError("Cannot confirm candidate: confirmation token has expired")
        self.transition_to(CandidateLifecycleState.CONFIRMED)

    def reject(self, reason: str = "") -> None:
        """Rejects candidate upon owner decision."""
        self.transition_to(CandidateLifecycleState.REJECTED, reason)

    def expire(self) -> None:
        """Marks candidate as expired."""
        self.transition_to(CandidateLifecycleState.EXPIRED, "Token expired")

    def is_expired(self, current_time: dt.datetime | str | None = None) -> bool:
        """Checks if candidate confirmation token has expired."""
        if not self.expires_at:
            return False
        try:
            exp = dt.datetime.fromisoformat(self.expires_at)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=dt.timezone.utc)
            if current_time is None:
                now = dt.datetime.now(dt.timezone.utc)
            elif isinstance(current_time, str):
                now = dt.datetime.fromisoformat(current_time)
                if now.tzinfo is None:
                    now = now.replace(tzinfo=dt.timezone.utc)
            else:
                now = current_time
                if now.tzinfo is None:
                    now = now.replace(tzinfo=dt.timezone.utc)
            return now > exp
        except Exception:
            return False


@dataclass(frozen=True)
class AuditTrailEntry:
    """Immutable audit trail entry recording provenance of applied candidate."""

    audit_id: int | None
    candidate_id: str
    idempotency_key: str
    preview_hash: str
    applied_timestamp: str
    participating_evidence_keys: tuple[str, ...]
    applied_mutation_row_ids: tuple[int, ...]
    pre_state_hash: str
    post_state_hash: str


@dataclass(frozen=True)
class ApplyResult:
    """Outcome of safe apply execution."""

    success: bool
    candidate_id: str
    idempotency_key: str
    state: CandidateLifecycleState
    applied_row_ids: tuple[int, ...]
    preview_hash: str
    is_idempotent_replay: bool
    applied_timestamp: str
    audit_entry: AuditTrailEntry | None = None
    error_message: str | None = None


def create_pre_apply_backup(
    db_path: Path | str,
    backup_dir: Path | str | None = None,
) -> tuple[Path, str]:
    """Creates an atomic backup using SQLite native Online Backup API and validates integrity."""
    p = Path(db_path)
    if not p.exists():
        raise FileNotFoundError(f"Database file does not exist: {p}")

    b_dir = Path(backup_dir) if backup_dir else p.parent / "backups"
    b_dir.mkdir(parents=True, exist_ok=True)

    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    unique_id = uuid.uuid4().hex[:8]
    backup_file = b_dir / f"{p.name}.backup_{timestamp}_{unique_id}.db"

    # Native SQLite Online Backup API
    src_conn = sqlite3.connect(p)
    dst_conn = sqlite3.connect(backup_file)
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()

    if not backup_file.exists() or backup_file.stat().st_size == 0:
        raise BackupError(f"Backup file creation failed or empty: {backup_file}")

    # Verify backup opens cleanly and integrity is intact
    test_conn = sqlite3.connect(backup_file)
    try:
        res = test_conn.execute("PRAGMA quick_check").fetchone()
        if not res or res[0] != "ok":
            raise BackupError(f"Backup integrity check failed: {res}")
    finally:
        test_conn.close()

    backup_hash = compute_file_sha256(backup_file)
    return backup_file, backup_hash


def restore_from_backup(
    backup_path: Path | str,
    target_db_path: Path | str,
) -> bool:
    """Restores target database from an existing backup using native SQLite Online Backup API."""
    b_path = Path(backup_path)
    t_path = Path(target_db_path)

    if not b_path.exists() or b_path.stat().st_size == 0:
        raise FileNotFoundError(f"Valid backup file not found: {b_path}")

    t_path.parent.mkdir(parents=True, exist_ok=True)

    # Native SQLite Online Backup API to restore
    src_conn = sqlite3.connect(b_path)
    dst_conn = sqlite3.connect(t_path)
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()

    test_conn = sqlite3.connect(t_path)
    try:
        res = test_conn.execute("PRAGMA quick_check").fetchone()
        if not res or res[0] != "ok":
            raise BackupError(f"Restored database integrity check failed: {res}")
    finally:
        test_conn.close()

    return True


class SafeApplyEngine:
    """Deterministic, atomic Safe Apply Engine with automated backup and idempotency gate."""

    def __init__(
        self,
        db_path: Path | str,
        backup_dir: Path | str | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.backup_dir = Path(backup_dir) if backup_dir else self.db_path.parent / "backups"
        self.init_schema()

    def init_schema(self) -> None:
        """Ensures required tables for ledger, idempotency, and audit trail exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            con.execute("PRAGMA foreign_keys=ON")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    canonical_id TEXT UNIQUE,
                    date TEXT NOT NULL,
                    time TEXT NOT NULL DEFAULT '',
                    transaction_type TEXT NOT NULL CHECK(transaction_type IN ('Income','Expense','Transfer','Adjustment')),
                    amount REAL NOT NULL CHECK(amount >= 0),
                    account_from TEXT NOT NULL DEFAULT '',
                    account_to TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    for_with_whom TEXT NOT NULL DEFAULT 'Personal / Self',
                    money_context TEXT NOT NULL DEFAULT 'Personal' CHECK(money_context IN ('Personal','Pass-through','Historical Research','Third-party')),
                    settlement_kind TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'Confirmed' CHECK(status IN ('Confirmed','Auto-classified','Provisional Neutral')),
                    confidence TEXT NOT NULL DEFAULT 'high',
                    budget_effect REAL NOT NULL DEFAULT 0,
                    exclude_from_budget INTEGER NOT NULL DEFAULT 0,
                    budget_exclusion_reason TEXT NOT NULL DEFAULT '',
                    budget_rule_version TEXT NOT NULL DEFAULT 'legacy',
                    subtype TEXT NOT NULL DEFAULT '',
                    source_refs TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    manual_edited INTEGER NOT NULL DEFAULT 0,
                    is_deleted INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS safe_apply_idempotency (
                    idempotency_key TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    preview_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    applied_timestamp TEXT NOT NULL,
                    mutation_row_ids TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS safe_apply_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    preview_hash TEXT NOT NULL,
                    applied_timestamp TEXT NOT NULL,
                    participating_evidence_keys TEXT NOT NULL,
                    applied_mutation_row_ids TEXT NOT NULL,
                    pre_state_hash TEXT NOT NULL,
                    post_state_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """)
        finally:
            con.close()

    def apply(
        self,
        candidate: ApplyCandidate,
        *,
        simulated_error_at_mutation: int | None = None,
    ) -> ApplyResult:
        """Executes safe, atomic candidate application with strict idempotency and pre-apply backup."""
        # 1. Check expiration
        if candidate.state == CandidateLifecycleState.EXPIRED:
            raise CandidateExpiredError("Candidate confirmation token has expired")
        if candidate.is_expired():
            if candidate.state not in TERMINAL_STATES:
                candidate.transition_to(CandidateLifecycleState.EXPIRED, "Confirmation token expired")
            raise CandidateExpiredError("Candidate confirmation token has expired")

        # 2. Block REVIEW_REQUIRED
        if candidate.state == CandidateLifecycleState.REVIEW_REQUIRED:
            raise ReviewRequiredError("Candidate is in REVIEW_REQUIRED state and cannot be applied")

        # 3. Must be CONFIRMED
        if candidate.state != CandidateLifecycleState.CONFIRMED:
            raise InvalidStateTransitionError(
                f"Candidate must be in CONFIRMED state to apply, currently in '{candidate.state.value}'"
            )

        # 4. Preview hash verification
        recalculated_hash = compute_preview_hash(
            candidate.mutations, candidate.participating_evidence_keys, candidate.candidate_id
        )
        if recalculated_hash != candidate.preview_hash:
            candidate.transition_to(CandidateLifecycleState.FAILED, "PREVIEW_HASH_MISMATCH")
            raise PreviewHashMismatchError(
                f"PREVIEW_HASH_MISMATCH: got {recalculated_hash}, expected {candidate.preview_hash}"
            )

        # 5. Check idempotency pre-check (No backup created on idempotent replay!)
        read_con = sqlite3.connect(self.db_path, timeout=1.0)
        try:
            cur = read_con.cursor()
            cur.execute(
                "SELECT preview_hash, state, mutation_row_ids, applied_timestamp FROM safe_apply_idempotency WHERE idempotency_key = ?",
                (candidate.idempotency_key,),
            )
            row = cur.fetchone()
            if row:
                existing_hash, existing_state, existing_ids_json, existing_timestamp = row
                if existing_hash != candidate.preview_hash:
                    candidate.transition_to(CandidateLifecycleState.FAILED, "IDEMPOTENCY_CONFLICT")
                    raise IdempotencyConflictError(
                        f"IDEMPOTENCY_CONFLICT: Key '{candidate.idempotency_key}' already used with different preview hash"
                    )
                # Idempotent replay: return existing result immediately without creating ANY backup file!
                candidate.state = CandidateLifecycleState.APPLIED
                row_ids = tuple(json.loads(existing_ids_json))
                return ApplyResult(
                    success=True,
                    candidate_id=candidate.candidate_id,
                    idempotency_key=candidate.idempotency_key,
                    state=CandidateLifecycleState.APPLIED,
                    applied_row_ids=row_ids,
                    preview_hash=existing_hash,
                    is_idempotent_replay=True,
                    applied_timestamp=existing_timestamp,
                )
        finally:
            read_con.close()

        # 6. Automated Pre-Apply Backup via native SQLite backup API
        backup_file, pre_hash = create_pre_apply_backup(self.db_path, self.backup_dir)

        # 7. Atomic transaction with isolation_level=None
        applied_timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
        applied_row_ids: list[int] = []

        con = sqlite3.connect(self.db_path, timeout=1.0, isolation_level=None)
        try:
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("BEGIN IMMEDIATE")
            cur = con.cursor()

            # Double-check idempotency under lock in case of concurrent execution
            cur.execute(
                "SELECT preview_hash, mutation_row_ids, applied_timestamp FROM safe_apply_idempotency WHERE idempotency_key = ?",
                (candidate.idempotency_key,),
            )
            row = cur.fetchone()
            if row:
                existing_hash, existing_ids_json, existing_timestamp = row
                # Clean up backup file if a race resulted in idempotent replay
                backup_file.unlink(missing_ok=True)
                con.execute("ROLLBACK")
                if existing_hash != candidate.preview_hash:
                    candidate.transition_to(CandidateLifecycleState.FAILED, "IDEMPOTENCY_CONFLICT")
                    raise IdempotencyConflictError("IDEMPOTENCY_CONFLICT")
                candidate.state = CandidateLifecycleState.APPLIED
                return ApplyResult(
                    success=True,
                    candidate_id=candidate.candidate_id,
                    idempotency_key=candidate.idempotency_key,
                    state=CandidateLifecycleState.APPLIED,
                    applied_row_ids=tuple(json.loads(existing_ids_json)),
                    preview_hash=existing_hash,
                    is_idempotent_replay=True,
                    applied_timestamp=existing_timestamp,
                )

            # Insert mutations with exact Decimal string formatting (no float cast)
            for idx, mut in enumerate(candidate.mutations):
                if simulated_error_at_mutation is not None and idx == simulated_error_at_mutation:
                    raise RuntimeError(f"Simulated mid-apply error at mutation index {idx}")

                cur.execute(
                    """
                    INSERT INTO transactions (
                        date, time, transaction_type, amount, account_from, account_to,
                        description, category, for_with_whom, money_context, status,
                        notes, source_refs, canonical_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        mut.date,
                        mut.time,
                        mut.transaction_type,
                        f"{mut.amount:.2f}",
                        mut.account_from,
                        mut.account_to,
                        mut.description,
                        mut.category,
                        mut.for_with_whom,
                        mut.money_context,
                        mut.status,
                        mut.notes,
                        mut.source_refs,
                        mut.canonical_id,
                    ),
                )
                applied_row_ids.append(cur.lastrowid)

            # Record in idempotency table
            cur.execute(
                """
                INSERT INTO safe_apply_idempotency (
                    idempotency_key, candidate_id, preview_hash, state, applied_timestamp, mutation_row_ids
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.idempotency_key,
                    candidate.candidate_id,
                    candidate.preview_hash,
                    CandidateLifecycleState.APPLIED.value,
                    applied_timestamp,
                    json.dumps(applied_row_ids),
                ),
            )

            # Explicit COMMIT
            con.execute("COMMIT")
        except Exception as exc:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            candidate.transition_to(CandidateLifecycleState.FAILED, str(exc))
            raise
        finally:
            con.close()

        # Compute post-state hash of DB file
        post_hash = compute_file_sha256(self.db_path)

        # 8. Record audit entry
        audit_entry: AuditTrailEntry | None = None
        audit_con = sqlite3.connect(self.db_path, timeout=1.0, isolation_level=None)
        try:
            acur = audit_con.cursor()
            acur.execute("BEGIN IMMEDIATE")
            acur.execute(
                """
                INSERT INTO safe_apply_audit (
                    candidate_id, idempotency_key, preview_hash, applied_timestamp,
                    participating_evidence_keys, applied_mutation_row_ids, pre_state_hash, post_state_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.candidate_id,
                    candidate.idempotency_key,
                    candidate.preview_hash,
                    applied_timestamp,
                    json.dumps(list(candidate.participating_evidence_keys)),
                    json.dumps(applied_row_ids),
                    pre_hash,
                    post_hash,
                ),
            )
            audit_id = acur.lastrowid
            acur.execute("COMMIT")
            audit_entry = AuditTrailEntry(
                audit_id=audit_id,
                candidate_id=candidate.candidate_id,
                idempotency_key=candidate.idempotency_key,
                preview_hash=candidate.preview_hash,
                applied_timestamp=applied_timestamp,
                participating_evidence_keys=candidate.participating_evidence_keys,
                applied_mutation_row_ids=tuple(applied_row_ids),
                pre_state_hash=pre_hash,
                post_state_hash=post_hash,
            )
        finally:
            audit_con.close()

        candidate.transition_to(CandidateLifecycleState.APPLIED)

        return ApplyResult(
            success=True,
            candidate_id=candidate.candidate_id,
            idempotency_key=candidate.idempotency_key,
            state=CandidateLifecycleState.APPLIED,
            applied_row_ids=tuple(applied_row_ids),
            preview_hash=candidate.preview_hash,
            is_idempotent_replay=False,
            applied_timestamp=applied_timestamp,
            audit_entry=audit_entry,
        )
