"""
Universal Ingestion Stage 7B — Visual Review Queue Backend.

Provides deterministic functions to:
1. Retrieve pending review items (REVIEW_REQUIRED, AMBIGUOUS) with sanitized diagnostics.
2. Process Owner actions: APPROVE, REJECT, or MODIFY (recalculating preview hash).
3. Route approved candidates directly to the Stage 6 Safe Apply engine.
4. Enforces strict terminal state protections.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
from decimal import Decimal
from pathlib import Path
import re
from typing import Any

from aturuang.safe_apply import (
    ApplyCandidate,
    ApplyResult,
    CandidateLifecycleState,
    LedgerMutation,
    SafeApplyEngine,
    compute_preview_hash,
)


def _sanitize_diagnostics(msg: str) -> str:
    """Removes local filesystem paths and secrets from diagnostic messages."""
    msg = re.sub(r"[a-zA-Z]:\\[^\s]+", "[REDACTED_PATH]", msg)
    msg = re.sub(r"/(?:Users|home|var|tmp)/[^\s]+", "[REDACTED_PATH]", msg)
    return msg.strip()


TERMINAL_REVIEW_STATES = frozenset({"REJECTED", "APPLIED"})


@dataclass
class ReviewItem:
    item_id: str
    batch_id: str | None
    date: str
    time: str
    transaction_type: str
    amount: Decimal
    account_from: str
    account_to: str
    description: str
    category: str
    status: str  # "REVIEW_REQUIRED", "AMBIGUOUS", "READY_FOR_CONFIRMATION", "REJECTED", "APPLIED"
    reason: str = ""
    candidate: ApplyCandidate | None = None
    created_at: str = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())

    @property
    def preview_hash(self) -> str:
        return self.candidate.preview_hash if self.candidate else ""


class ReviewQueueManager:
    """Backend service for visual review queue, Owner approval actions, and safe apply routing."""

    def __init__(self, db_path: Path | str, backup_dir: Path | str | None = None) -> None:
        self.db_path = Path(db_path)
        self.backup_dir = Path(backup_dir) if backup_dir else self.db_path.parent / "backups"
        self.engine = SafeApplyEngine(self.db_path, self.backup_dir)
        self._items: dict[str, ReviewItem] = {}

    def add_item(self, item: ReviewItem) -> ReviewItem:
        """Registers a review item into the active queue."""
        self._items[item.item_id] = item
        return item

    def get_pending_items(self) -> list[ReviewItem]:
        """Returns pending review items (REVIEW_REQUIRED, AMBIGUOUS) with sanitized diagnostics."""
        pending = []
        for it in self._items.values():
            if it.status in ("REVIEW_REQUIRED", "AMBIGUOUS"):
                it.reason = _sanitize_diagnostics(it.reason)
                pending.append(it)
        return pending

    def get_item(self, item_id: str) -> ReviewItem | None:
        return self._items.get(item_id)

    def approve(self, item_id: str, notes: str = "") -> ReviewItem:
        """Approves a pending review item and moves it to confirmation readiness."""
        item = self._items.get(item_id)
        if not item:
            raise KeyError(f"Review item not found: {item_id}")

        if item.status in TERMINAL_REVIEW_STATES:
            raise ValueError(f"Cannot approve already terminated review item in state '{item.status}'")

        item.status = "READY_FOR_CONFIRMATION"
        if notes:
            item.reason = f"Approved: {notes}"

        if item.candidate:
            if not item.candidate.mutations and item.amount > Decimal("0.00"):
                mut = LedgerMutation(
                    date=item.date,
                    time=item.time or "12:00:00",
                    transaction_type=item.transaction_type,
                    amount=item.amount,
                    account_from=item.account_from,
                    account_to=item.account_to,
                    description=item.description or "Approved item",
                    category=item.category or "Other / Miscellaneous",
                    canonical_id=f"tx_{item.candidate.candidate_id}",
                )
                item.candidate.mutations = (mut,)
                item.candidate.preview_hash = compute_preview_hash(
                    item.candidate.mutations,
                    item.candidate.participating_evidence_keys,
                    item.candidate.candidate_id,
                )

            if item.candidate.state in (CandidateLifecycleState.PARSED, CandidateLifecycleState.REVIEW_REQUIRED):
                item.candidate.transition_to(CandidateLifecycleState.READY_FOR_CONFIRMATION)

        return item

    def reject(self, item_id: str, reason: str = "") -> ReviewItem:
        """Rejects a pending review item and marks it as terminated."""
        item = self._items.get(item_id)
        if not item:
            raise KeyError(f"Review item not found: {item_id}")

        if item.status in TERMINAL_REVIEW_STATES:
            raise ValueError(f"Cannot reject already terminated review item in state '{item.status}'")

        item.status = "REJECTED"
        item.reason = f"Rejected: {reason}" if reason else "Rejected by Owner"

        if item.candidate:
            if item.candidate.state not in (CandidateLifecycleState.APPLIED, CandidateLifecycleState.EXPIRED):
                if item.candidate.state in (CandidateLifecycleState.PARSED, CandidateLifecycleState.REVIEW_REQUIRED):
                    item.candidate.transition_to(CandidateLifecycleState.READY_FOR_CONFIRMATION)
                item.candidate.reject(reason)

        return item

    def modify(self, item_id: str, updates: dict[str, Any]) -> ReviewItem:
        """Modifies candidate parameters, validates Decimal amounts, and recalculates preview hash."""
        item = self._items.get(item_id)
        if not item:
            raise KeyError(f"Review item not found: {item_id}")

        if item.status in TERMINAL_REVIEW_STATES:
            raise ValueError(f"Cannot modify already terminated review item in state '{item.status}'")

        if "amount" in updates:
            amt = updates["amount"]
            if isinstance(amt, (int, str)):
                dec_amt = Decimal(str(amt)).quantize(Decimal("0.01"))
            elif isinstance(amt, Decimal):
                dec_amt = amt.quantize(Decimal("0.01"))
            else:
                raise TypeError(f"Invalid amount type: {type(amt)}")
            if dec_amt <= Decimal("0.00"):
                raise ValueError("Amount must be positive Decimal")
            item.amount = dec_amt

        if "description" in updates:
            item.description = str(updates["description"]).strip()
        if "category" in updates:
            item.category = str(updates["category"]).strip()
        if "account_from" in updates:
            item.account_from = str(updates["account_from"]).strip()
        if "account_to" in updates:
            item.account_to = str(updates["account_to"]).strip()
        if "transaction_type" in updates:
            ttype = str(updates["transaction_type"]).strip()
            if ttype not in ("Income", "Expense", "Transfer", "Adjustment"):
                raise ValueError(f"Invalid transaction_type: {ttype}")
            item.transaction_type = ttype

        cid = item.candidate.candidate_id if item.candidate else f"cand_{item_id}"
        ikey = item.candidate.idempotency_key if item.candidate else f"idemp_{item_id}"
        ev_keys = item.candidate.participating_evidence_keys if item.candidate else (f"review_item:{item_id}",)

        mut = LedgerMutation(
            date=item.date,
            time=item.time or "12:00:00",
            transaction_type=item.transaction_type,
            amount=item.amount,
            account_from=item.account_from,
            account_to=item.account_to,
            description=item.description,
            category=item.category,
            canonical_id=f"tx_{cid}",
        )

        new_preview_hash = compute_preview_hash((mut,), ev_keys, cid)

        item.candidate = ApplyCandidate(
            candidate_id=cid,
            idempotency_key=ikey,
            state=CandidateLifecycleState.READY_FOR_CONFIRMATION,
            participating_evidence_keys=ev_keys,
            mutations=(mut,),
            preview_hash=new_preview_hash,
        )
        item.status = "READY_FOR_CONFIRMATION"
        return item

    def confirm_and_apply(self, item_id: str) -> ApplyResult:
        """Confirms approved candidate and executes Stage 6 atomic Safe Apply."""
        item = self._items.get(item_id)
        if not item:
            raise KeyError(f"Review item not found: {item_id}")

        if item.status in TERMINAL_REVIEW_STATES and item.status != "APPLIED":
            raise ValueError(f"Cannot apply terminated review item in state '{item.status}'")

        if item.status != "READY_FOR_CONFIRMATION" and item.status != "APPLIED":
            self.approve(item_id)

        candidate = item.candidate
        if not candidate:
            raise ValueError(f"No candidate bound to review item {item_id}")

        if candidate.state == CandidateLifecycleState.APPLIED:
            candidate = ApplyCandidate(
                candidate_id=candidate.candidate_id,
                idempotency_key=candidate.idempotency_key,
                state=CandidateLifecycleState.CONFIRMED,
                participating_evidence_keys=candidate.participating_evidence_keys,
                mutations=candidate.mutations,
                preview_hash=candidate.preview_hash,
            )
        else:
            if candidate.state == CandidateLifecycleState.READY_FOR_CONFIRMATION:
                candidate.confirm()

        result = self.engine.apply(candidate)
        item.status = "APPLIED"
        return result
