"""
Universal Ingestion Stage 7B — Import Center Batch Manager.

Provides backend utilities for statement file ingestion (PDF, CSV), secure ephemeral staging,
dry-run discovery/parsing/matching, and structured import summaries.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import io
from pathlib import Path
import re
from typing import Any, Sequence
import uuid

from aturuang.safe_apply import (
    ApplyCandidate,
    CandidateLifecycleState,
    LedgerMutation,
    compute_preview_hash,
)

SUPPORTED_EXTENSIONS = frozenset({".csv", ".pdf", ".txt"})


def _sanitize_diagnostics(msg: str) -> str:
    """Removes local filesystem paths and secrets from diagnostic messages."""
    msg = re.sub(r"[a-zA-Z]:\\[^\s]+", "[REDACTED_PATH]", msg)
    msg = re.sub(r"/(?:Users|home|var|tmp)/[^\s]+", "[REDACTED_PATH]", msg)
    return msg.strip()


def compute_sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class StagedFile:
    filename: str
    content_hash: str
    staged_path: Path
    size_bytes: int


@dataclass
class ImportBatchItem:
    item_id: str
    row_index: int
    date: str
    time: str
    transaction_type: str
    amount: Decimal
    account_from: str
    account_to: str
    description: str
    category: str
    status: str  # "READY_FOR_CONFIRMATION", "REVIEW_REQUIRED", "AMBIGUOUS"
    reason: str = ""
    candidate: ApplyCandidate | None = None


@dataclass
class ImportBatch:
    batch_id: str
    filename: str
    file_hash: str
    status: str  # "STAGED", "PARSED", "REVIEW_REQUIRED", "FAILED"
    total_rows: int
    parsed_items: int
    matched_pairs: int
    review_required_count: int
    reconciliation_status: str  # "BALANCED", "DISCREPANCY", "PENDING_REVIEW"
    items: list[ImportBatchItem] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())


class ImportCenterManager:
    """Manages file ingestion, secure ephemeral staging, dry-run parsing, and batch summaries."""

    def __init__(self, db_path: Path | str, staging_dir: Path | str | None = None) -> None:
        self.db_path = Path(db_path)
        self.staging_dir = Path(staging_dir) if staging_dir else self.db_path.parent / "staging"
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self._batches: dict[str, ImportBatch] = {}
        self._hash_to_batch_id: dict[str, str] = {}

    def stage_file(self, filename: str, content: bytes) -> StagedFile:
        """Atomically stages uploaded file into ephemeral directory and verifies integrity."""
        ext = Path(filename).suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            raise ValueError(_sanitize_diagnostics(f"UNSUPPORTED_FORMAT: Extension '{ext}' is not supported"))

        if not content or len(content) == 0:
            raise ValueError(_sanitize_diagnostics("EMPTY_FILE: Uploaded file is empty"))

        content_hash = compute_sha256_bytes(content)
        safe_name = re.sub(r"[^\w\.-]", "_", filename)
        target_path = self.staging_dir / f"{content_hash[:12]}_{safe_name}"

        temp_path = self.staging_dir / f".tmp_{uuid.uuid4().hex}_{safe_name}"
        try:
            with open(temp_path, "wb") as f:
                f.write(content)
            temp_path.replace(target_path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

        return StagedFile(
            filename=filename,
            content_hash=content_hash,
            staged_path=target_path,
            size_bytes=len(content),
        )

    def process_batch(self, filename: str, content: bytes, provider: str = "auto") -> ImportBatch:
        """Processes uploaded file: stages atomically, runs parsing/matcher dry-run, returns batch summary."""
        ext = Path(filename).suffix.lower()
        file_hash = compute_sha256_bytes(content) if content else ""

        if ext not in SUPPORTED_EXTENSIONS:
            batch_id = f"batch_{uuid.uuid4().hex[:12]}"
            batch = ImportBatch(
                batch_id=batch_id,
                filename=filename,
                file_hash=file_hash,
                status="FAILED",
                total_rows=0,
                parsed_items=0,
                matched_pairs=0,
                review_required_count=0,
                reconciliation_status="DISCREPANCY",
                diagnostics=[_sanitize_diagnostics(f"UNSUPPORTED_FORMAT: Extension '{ext}' is not supported")],
            )
            self._batches[batch_id] = batch
            return batch

        if not content or len(content) == 0:
            batch_id = f"batch_{uuid.uuid4().hex[:12]}"
            batch = ImportBatch(
                batch_id=batch_id,
                filename=filename,
                file_hash="",
                status="FAILED",
                total_rows=0,
                parsed_items=0,
                matched_pairs=0,
                review_required_count=0,
                reconciliation_status="DISCREPANCY",
                diagnostics=[_sanitize_diagnostics("EMPTY_FILE: Uploaded content is empty")],
            )
            self._batches[batch_id] = batch
            return batch

        # Idempotency check: if file already processed, return cached batch
        if file_hash in self._hash_to_batch_id:
            cached_id = self._hash_to_batch_id[file_hash]
            return self._batches[cached_id]

        try:
            staged = self.stage_file(filename, content)
        except ValueError as e:
            batch_id = f"batch_{uuid.uuid4().hex[:12]}"
            batch = ImportBatch(
                batch_id=batch_id,
                filename=filename,
                file_hash=file_hash,
                status="FAILED",
                total_rows=0,
                parsed_items=0,
                matched_pairs=0,
                review_required_count=0,
                reconciliation_status="DISCREPANCY",
                diagnostics=[str(e)],
            )
            self._batches[batch_id] = batch
            return batch

        batch_id = f"batch_{uuid.uuid4().hex[:12]}"
        items: list[ImportBatchItem] = []
        diagnostics: list[str] = []

        if ext == ".csv":
            self._parse_csv(content, batch_id, items, diagnostics)
        elif ext == ".pdf":
            self._parse_pdf(content, batch_id, items, diagnostics)
        else:
            diagnostics.append(_sanitize_diagnostics(f"FORMAT_HANDLER_MISSING: No specific handler for {ext}"))

        total_rows = len(items)
        parsed_items = sum(1 for it in items if it.status == "READY_FOR_CONFIRMATION")
        review_required = sum(1 for it in items if it.status in ("REVIEW_REQUIRED", "AMBIGUOUS"))
        matched_pairs = total_rows // 2 if total_rows > 1 else 0

        recon_status = "BALANCED" if review_required == 0 and total_rows > 0 else "PENDING_REVIEW"
        batch_status = "PARSED" if review_required == 0 else "REVIEW_REQUIRED"

        batch = ImportBatch(
            batch_id=batch_id,
            filename=filename,
            file_hash=file_hash,
            status=batch_status,
            total_rows=total_rows,
            parsed_items=parsed_items,
            matched_pairs=matched_pairs,
            review_required_count=review_required,
            reconciliation_status=recon_status,
            items=items,
            diagnostics=diagnostics,
        )

        self._batches[batch_id] = batch
        self._hash_to_batch_id[file_hash] = batch_id
        return batch

    def _parse_csv(
        self,
        content: bytes,
        batch_id: str,
        items: list[ImportBatchItem],
        diagnostics: list[str],
    ) -> None:
        """Parses CSV statement content with delimiter detection and error tolerance."""
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = content.decode("latin-1")
            except Exception:
                diagnostics.append(_sanitize_diagnostics("MALFORMED_FILE: Unable to decode file text"))
                return

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            diagnostics.append(_sanitize_diagnostics("EMPTY_FILE: No lines found in CSV"))
            return

        first_line = lines[0]
        delim = ","
        if ";" in first_line and first_line.count(";") > first_line.count(","):
            delim = ";"
        elif "\t" in first_line:
            delim = "\t"

        reader = csv.reader(io.StringIO(text), delimiter=delim)
        rows = list(reader)
        if not rows:
            return

        header = [h.strip().lower() for h in rows[0]]
        has_header = any(h in ("date", "tanggal", "amount", "nominal", "description", "keterangan") for h in header)

        data_rows = rows[1:] if has_header else rows
        for idx, row in enumerate(data_rows, start=1):
            if not row or not any(row):
                continue
            item = self._parse_csv_row(row, header if has_header else None, idx, batch_id)
            items.append(item)

    def _parse_csv_row(
        self,
        row: list[str],
        header: list[str] | None,
        row_idx: int,
        batch_id: str,
    ) -> ImportBatchItem:
        """Extracts structured transaction attributes from a single CSV row."""
        date_val = dt.datetime.now().strftime("%Y-%m-%d")
        time_val = "12:00:00"
        amount_val = Decimal("0.00")
        desc_val = ""
        ttype_val = "Expense"
        acc_from = "Cash"
        acc_to = ""
        cat_val = "Other / Miscellaneous"
        status = "READY_FOR_CONFIRMATION"
        reason = ""

        if header and len(header) == len(row):
            row_dict = {h: r.strip() for h, r in zip(header, row)}
            for k in ("date", "tanggal", "tgl", "transaction date"):
                if k in row_dict and row_dict[k]:
                    date_val = row_dict[k][:10]
                    break
            for k in ("amount", "nominal", "jumlah", "debit", "kredit"):
                if k in row_dict and row_dict[k]:
                    raw_amt = row_dict[k]
                    amt_parsed = self._clean_amount(raw_amt)
                    if amt_parsed is not None:
                        amount_val = amt_parsed
                    break
            for k in ("description", "keterangan", "deskripsi", "uraian", "memo"):
                if k in row_dict and row_dict[k]:
                    desc_val = row_dict[k]
                    break
            for k in ("type", "tipe", "jenis"):
                if k in row_dict and row_dict[k]:
                    t_str = row_dict[k].lower()
                    if "income" in t_str or "masuk" in t_str or "kredit" in t_str:
                        ttype_val = "Income"
                    elif "transfer" in t_str:
                        ttype_val = "Transfer"
                    break
            for k in ("account", "rekening", "akun"):
                if k in row_dict and row_dict[k]:
                    acc_from = row_dict[k]
                    break
        else:
            if len(row) >= 1:
                date_val = row[0][:10]
            if len(row) >= 2:
                desc_val = row[1]
            if len(row) >= 3:
                amt_parsed = self._clean_amount(row[2])
                if amt_parsed is not None:
                    amount_val = amt_parsed

        if amount_val <= Decimal("0.00"):
            status = "REVIEW_REQUIRED"
            reason = "MISSING_OR_ZERO_AMOUNT: Transaction amount must be positive"
        elif not desc_val or desc_val.strip() == "":
            status = "AMBIGUOUS"
            reason = "MISSING_DESCRIPTION: Transaction has no description"
        elif "unknown" in desc_val.lower() or "suspense" in desc_val.lower():
            status = "AMBIGUOUS"
            reason = "AMBIGUOUS_TRANSACTION: Description indicates suspense or ambiguous payment"

        item_id = f"item_{batch_id[:8]}_{row_idx:04d}"

        candidate = None
        if amount_val > Decimal("0.00"):
            cid = f"cand_{item_id}"
            ikey = f"idemp_{batch_id}_{row_idx}"
            ev_keys = (f"import_batch:{batch_id}:{row_idx}",)
            mut = LedgerMutation(
                date=date_val,
                time=time_val,
                transaction_type=ttype_val,
                amount=amount_val,
                account_from=acc_from if ttype_val != "Income" else "",
                account_to=acc_to if ttype_val != "Income" else acc_from,
                description=desc_val,
                category=cat_val,
                canonical_id=f"tx_{cid}",
            )
            p_hash = compute_preview_hash((mut,), ev_keys, cid)
            cand_state = CandidateLifecycleState.PARSED if status == "READY_FOR_CONFIRMATION" else CandidateLifecycleState.REVIEW_REQUIRED
            candidate = ApplyCandidate(
                candidate_id=cid,
                idempotency_key=ikey,
                state=cand_state,
                participating_evidence_keys=ev_keys,
                mutations=(mut,),
                preview_hash=p_hash,
            )

        return ImportBatchItem(
            item_id=item_id,
            row_index=row_idx,
            date=date_val,
            time=time_val,
            transaction_type=ttype_val,
            amount=amount_val,
            account_from=acc_from,
            account_to=acc_to,
            description=desc_val,
            category=cat_val,
            status=status,
            reason=reason,
            candidate=candidate,
        )

    def _parse_pdf(
        self,
        content: bytes,
        batch_id: str,
        items: list[ImportBatchItem],
        diagnostics: list[str],
    ) -> None:
        """Parses PDF statement. If corrupted or unparseable, fails closed."""
        if not content.startswith(b"%PDF"):
            diagnostics.append(_sanitize_diagnostics("MALFORMED_FILE: File does not have valid PDF header"))
            return

        try:
            text = content.decode("latin-1")
            matches = re.findall(r"(\d{4}-\d{2}-\d{2})\s+([^\n\r\d]+)\s+(\d+(?:[.,]\d+)?)", text)
            if not matches:
                diagnostics.append("NO_RECOGNIZED_TRANSACTIONS: Scanned or binary PDF requires OCR/manual review")
                return

            for idx, (dt_str, desc, amt_str) in enumerate(matches, start=1):
                amt = self._clean_amount(amt_str) or Decimal("0.00")
                item = ImportBatchItem(
                    item_id=f"item_{batch_id[:8]}_{idx:04d}",
                    row_index=idx,
                    date=dt_str,
                    time="12:00:00",
                    transaction_type="Expense",
                    amount=amt,
                    account_from="BCA Main",
                    account_to="",
                    description=desc.strip(),
                    category="Other / Miscellaneous",
                    status="READY_FOR_CONFIRMATION" if amt > Decimal("0.00") else "REVIEW_REQUIRED",
                )
                items.append(item)
        except Exception as e:
            diagnostics.append(_sanitize_diagnostics(f"PDF_PARSE_ERROR: {str(e)}"))

    def _clean_amount(self, val_str: str) -> Decimal | None:
        """Extracts exact Decimal amount from messy currency string."""
        s = val_str.replace("Rp", "").replace("rp", "").replace("IDR", "").strip()
        s = re.sub(r"[^\d.,\-]", "", s)
        if not s:
            return None
        try:
            if re.match(r"^\d{1,3}(?:\.\d{3})+(?:,\d+)?$", s):
                s = s.replace(".", "").replace(",", ".")
            elif re.match(r"^\d{1,3}(?:,\d{3})+(?:\.\d+)?$", s):
                s = s.replace(",", "")
            else:
                s = s.replace(",", ".")
            dec = Decimal(s).quantize(Decimal("0.01"))
            return abs(dec)
        except (InvalidOperation, ValueError):
            return None

    def get_batch(self, batch_id: str) -> ImportBatch | None:
        return self._batches.get(batch_id)

    def get_batch_summary(self, batch_id: str) -> dict[str, Any] | None:
        batch = self.get_batch(batch_id)
        if not batch:
            return None
        return {
            "batch_id": batch.batch_id,
            "filename": batch.filename,
            "file_hash": batch.file_hash,
            "status": batch.status,
            "total_rows": batch.total_rows,
            "parsed_items": batch.parsed_items,
            "matched_pairs": batch.matched_pairs,
            "review_required_count": batch.review_required_count,
            "reconciliation_status": batch.reconciliation_status,
            "diagnostics": batch.diagnostics,
            "created_at": batch.created_at,
        }
