"""
Universal Ingestion Stage 7C — Local Web Composer, Import Center, Review Queue & Receipt OCR.

Provides a clean, minimalist web interface and JSON API allowing the Owner to:
1. Quick Capture natural Indonesian financial grammar into candidates with deterministic preview hashes.
2. Ingest statement files (CSV, PDF) via Import Center batch manager.
3. Review, approve, reject, or modify ambiguous items in the Visual Review Queue.
4. Process, review, correct, and apply physical receipts via Receipt OCR Review (Stage 7C).
5. Confirm and apply candidates atomically through the Stage 6 Safe Apply engine.
6. Query real-time account balances and monthly summaries with zero write risk.
"""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
from typing import Any

from aturuang.safe_apply import (
    ApplyCandidate,
    ApplyResult,
    CandidateLifecycleState,
    LedgerMutation,
    SafeApplyEngine,
    compute_preview_hash,
)
from aturuang.quick_capture import QuickCaptureResult, parse_quick_capture
from aturuang.query_service import get_account_balances, get_monthly_summary, get_pending_reviews
from aturuang.import_center import ImportCenterManager, ImportBatch, ImportBatchItem
from aturuang.review_queue_ui import ReviewQueueManager, ReviewItem
from aturuang.receipt_ocr import ReceiptOCRManager, ReceiptDraft


def _decimal_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return f"{obj:.2f}"
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


class QuickCaptureComposer:
    """Owner Experience MVP: Local Web Composer, Import Center, Review Queue, and Receipt OCR."""

    def __init__(
        self,
        db_path: Path | str,
        backup_dir: Path | str | None = None,
        staging_dir: Path | str | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.backup_dir = Path(backup_dir) if backup_dir else self.db_path.parent / "backups"
        self.staging_dir = Path(staging_dir) if staging_dir else self.db_path.parent / "staging"
        self.engine = SafeApplyEngine(self.db_path, self.backup_dir)
        self.import_manager = ImportCenterManager(self.db_path, staging_dir=self.staging_dir)
        self.review_manager = ReviewQueueManager(self.db_path, backup_dir=self.backup_dir)
        self.receipt_manager = ReceiptOCRManager(self.db_path, backup_dir=self.backup_dir)

    def preview(self, text: str, default_account: str = "Cash") -> dict[str, Any]:
        """Parses quick capture text, builds candidate, and returns structured preview with hash."""
        qc_res = parse_quick_capture(text, default_account=default_account)
        candidate = qc_res.candidate

        mutations_data = []
        if candidate and candidate.mutations:
            for m in candidate.mutations:
                mutations_data.append({
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

        return {
            "status": qc_res.status,
            "raw_text": qc_res.raw_text,
            "diagnostic_reason": qc_res.diagnostic_reason,
            "transaction_type": qc_res.transaction_type,
            "amount": f"{qc_res.amount:.2f}" if qc_res.amount is not None else None,
            "account_from": qc_res.account_from,
            "account_to": qc_res.account_to,
            "description": qc_res.description,
            "category": qc_res.category,
            "date": qc_res.date,
            "time": qc_res.time,
            "candidate_id": candidate.candidate_id if candidate else None,
            "idempotency_key": candidate.idempotency_key if candidate else None,
            "preview_hash": candidate.preview_hash if candidate else None,
            "participating_evidence_keys": list(candidate.participating_evidence_keys) if candidate else [],
            "mutations": mutations_data,
            "_candidate": candidate,
        }

    def confirm_and_apply(self, candidate_or_payload: ApplyCandidate | dict[str, Any]) -> ApplyResult:
        """Confirms candidate and safely applies mutations through Stage 6 engine."""
        if isinstance(candidate_or_payload, ApplyCandidate):
            candidate = candidate_or_payload
        elif isinstance(candidate_or_payload, dict):
            if "_candidate" in candidate_or_payload and isinstance(candidate_or_payload["_candidate"], ApplyCandidate):
                candidate = candidate_or_payload["_candidate"]
            else:
                cid = candidate_or_payload["candidate_id"]
                ikey = candidate_or_payload["idempotency_key"]
                preview_hash = candidate_or_payload.get("preview_hash", "")

                mutations_raw = candidate_or_payload.get("mutations", [])
                mutations = []
                for m in mutations_raw:
                    mutations.append(
                        LedgerMutation(
                            date=m["date"],
                            time=m.get("time", ""),
                            transaction_type=m["transaction_type"],
                            amount=Decimal(str(m["amount"])),
                            account_from=m.get("account_from", ""),
                            account_to=m.get("account_to", ""),
                            description=m.get("description", ""),
                            category=m.get("category", "Other / Miscellaneous"),
                            for_with_whom=m.get("for_with_whom", "Personal / Self"),
                            money_context=m.get("money_context", "Personal"),
                            status=m.get("status", "Confirmed"),
                            canonical_id=m.get("canonical_id"),
                            notes=m.get("notes", ""),
                            source_refs=m.get("source_refs", ""),
                        )
                    )

                evidence_keys = tuple(candidate_or_payload.get("participating_evidence_keys", (f"quick_capture:{cid}",)))
                calc_hash = compute_preview_hash(tuple(mutations), evidence_keys, cid)
                if preview_hash and preview_hash != calc_hash:
                    raise ValueError(f"Preview hash mismatch: got {calc_hash}, payload had {preview_hash}")

                candidate = ApplyCandidate(
                    candidate_id=cid,
                    idempotency_key=ikey,
                    state=CandidateLifecycleState.PARSED,
                    participating_evidence_keys=evidence_keys,
                    mutations=tuple(mutations),
                    preview_hash=calc_hash,
                )
        else:
            raise TypeError("candidate_or_payload must be ApplyCandidate or dict")

        if candidate.state == CandidateLifecycleState.APPLIED:
            candidate = ApplyCandidate(
                candidate_id=candidate.candidate_id,
                idempotency_key=candidate.idempotency_key,
                state=CandidateLifecycleState.CONFIRMED,
                participating_evidence_keys=candidate.participating_evidence_keys,
                mutations=candidate.mutations,
                preview_hash=candidate.preview_hash,
            )
        elif candidate.state == CandidateLifecycleState.PARSED:
            candidate.transition_to(CandidateLifecycleState.READY_FOR_CONFIRMATION)
            candidate.confirm()
        elif candidate.state == CandidateLifecycleState.READY_FOR_CONFIRMATION:
            candidate.confirm()

        return self.engine.apply(candidate)

    # ------------------ Import Center Operations ------------------

    def upload_file(self, filename: str, content: bytes, provider: str = "auto") -> dict[str, Any]:
        """Uploads and processes a file batch, registering any ambiguous or review-needed items."""
        batch = self.import_manager.process_batch(filename, content, provider=provider)
        for it in batch.items:
            if it.status in ("REVIEW_REQUIRED", "AMBIGUOUS"):
                rev_item = ReviewItem(
                    item_id=it.item_id,
                    batch_id=batch.batch_id,
                    date=it.date,
                    time=it.time,
                    transaction_type=it.transaction_type,
                    amount=it.amount,
                    account_from=it.account_from,
                    account_to=it.account_to,
                    description=it.description,
                    category=it.category,
                    status=it.status,
                    reason=it.reason,
                    candidate=it.candidate,
                )
                self.review_manager.add_item(rev_item)

        return self.import_manager.get_batch_summary(batch.batch_id) or {}

    def get_import_batch(self, batch_id: str) -> dict[str, Any] | None:
        return self.import_manager.get_batch_summary(batch_id)

    # ------------------ Review Queue Operations ------------------

    def get_review_queue(self) -> list[dict[str, Any]]:
        """Returns active review queue items."""
        items = self.review_manager.get_pending_items()
        out = []
        for it in items:
            out.append({
                "item_id": it.item_id,
                "batch_id": it.batch_id,
                "date": it.date,
                "time": it.time,
                "transaction_type": it.transaction_type,
                "amount": f"{it.amount:.2f}",
                "account_from": it.account_from,
                "account_to": it.account_to,
                "description": it.description,
                "category": it.category,
                "status": it.status,
                "reason": it.reason,
                "preview_hash": it.preview_hash,
            })
        return out

    def dispatch_review_action(
        self,
        action: str,
        item_id: str,
        updates: dict[str, Any] | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        """Executes review queue action: approve, reject, modify, or apply."""
        act = action.lower().strip()
        if act == "approve":
            item = self.review_manager.approve(item_id, notes=notes)
            return {"success": True, "status": item.status, "item_id": item.item_id}
        elif act == "reject":
            item = self.review_manager.reject(item_id, reason=notes)
            return {"success": True, "status": item.status, "item_id": item.item_id}
        elif act == "modify":
            item = self.review_manager.modify(item_id, updates or {})
            return {
                "success": True,
                "status": item.status,
                "item_id": item.item_id,
                "preview_hash": item.preview_hash,
                "amount": f"{item.amount:.2f}",
            }
        elif act == "apply":
            apply_res = self.review_manager.confirm_and_apply(item_id)
            return {
                "success": apply_res.success,
                "state": apply_res.state.value,
                "applied_row_ids": list(apply_res.applied_row_ids),
                "is_idempotent_replay": apply_res.is_idempotent_replay,
                "preview_hash": apply_res.preview_hash,
            }
        else:
            raise ValueError(f"Unknown review action: {action}")

    # ------------------ Receipt OCR Operations ------------------

    def process_receipt_upload(self, filename: str, content: bytes) -> dict[str, Any]:
        """Runs local OCR extraction, formats draft, and returns reviewable JSON."""
        draft = self.receipt_manager.process_receipt(filename, content)
        return {
            "status": draft.status,
            "receipt_id": draft.receipt_id,
            "filename": draft.filename,
            "image_hash": draft.image_hash,
            "merchant": draft.merchant,
            "date": draft.date,
            "time": draft.time,
            "amount": f"{draft.amount:.2f}",
            "category": draft.category,
            "account_from": draft.account_from,
            "field_confidences": draft.field_confidences,
            "reason": draft.reason,
            "preview_hash": draft.preview_hash,
        }

    def confirm_receipt_draft(
        self,
        receipt_id: str,
        corrections: dict[str, Any] | None = None,
        action: str = "apply",
        reason: str = "",
    ) -> dict[str, Any]:
        """Applies corrections and routes draft candidate to Safe Apply engine."""
        act = action.lower().strip()
        if act == "reject":
            draft = self.receipt_manager.reject(receipt_id, reason=reason)
            return {"success": True, "status": draft.status, "receipt_id": receipt_id}

        res = self.receipt_manager.confirm_and_apply(receipt_id, corrections=corrections)
        return {
            "success": res.success,
            "status": "APPLIED",
            "applied_count": len(res.applied_row_ids),
            "applied_row_ids": list(res.applied_row_ids),
            "preview_hash": res.preview_hash,
            "is_idempotent_replay": res.is_idempotent_replay,
            "receipt_id": receipt_id,
        }

    # ------------------ Query Services ------------------

    def get_balances(self) -> dict[str, Any]:
        return get_account_balances(self.db_path)

    def get_monthly_summary(self, year: int, month: int) -> dict[str, Any]:
        return get_monthly_summary(year, month, self.db_path)

    def get_pending_reviews(self) -> list[dict[str, Any]]:
        return get_pending_reviews(self.db_path)

    # ------------------ HTTP Request Dispatcher ------------------

    def handle_request(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]] | None = None,
        body: bytes | str | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        """Dispatches HTTP requests for local web composer UI, Import Center, Review Queue, and Receipt OCR."""
        m = method.upper()
        p = path.rstrip("/") or "/"
        q = query or {}

        if m == "GET" and (p in ("/", "/quick-capture", "/import", "/review", "/receipt")):
            html_content = self.render_html()
            return 200, {"Content-Type": "text/html; charset=utf-8"}, html_content.encode("utf-8")

        if m == "GET" and p == "/api/quick-capture/preview":
            raw_q = q.get("q", [""])[0]
            def_acc = q.get("account", ["Cash"])[0]
            res = self.preview(raw_q, default_account=def_acc)
            out_res = {k: v for k, v in res.items() if not k.startswith("_")}
            return 200, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(out_res, default=_decimal_default).encode("utf-8")

        if m == "POST" and p == "/api/quick-capture/apply":
            payload: dict[str, Any] = {}
            if body:
                raw_str = body.decode("utf-8") if isinstance(body, bytes) else body
                payload = json.loads(raw_str)
            try:
                apply_res = self.confirm_and_apply(payload)
                out = {
                    "success": apply_res.success,
                    "candidate_id": apply_res.candidate_id,
                    "preview_hash": apply_res.preview_hash,
                    "applied_row_ids": list(apply_res.applied_row_ids),
                    "is_idempotent_replay": apply_res.is_idempotent_replay,
                    "applied_timestamp": apply_res.applied_timestamp,
                }
                return 200, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(out).encode("utf-8")
            except Exception as e:
                err_out = {"success": False, "error": str(e)}
                return 400, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(err_out).encode("utf-8")

        # Import Center HTTP endpoints
        if m == "POST" and p == "/api/import/upload":
            try:
                payload: dict[str, Any] = {}
                if body:
                    raw_str = body.decode("utf-8") if isinstance(body, bytes) else body
                    payload = json.loads(raw_str)
                filename = payload.get("filename", "upload.csv")
                content_str = payload.get("content", "")
                content_bytes = content_str.encode("utf-8") if isinstance(content_str, str) else b""
                summary = self.upload_file(filename, content_bytes)
                return 200, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(summary).encode("utf-8")
            except Exception as e:
                return 400, {"Content-Type": "application/json; charset=utf-8"}, json.dumps({"success": False, "error": str(e)}).encode("utf-8")

        if m == "GET" and p == "/api/import/batch":
            batch_id = q.get("id", [""])[0]
            summary = self.get_import_batch(batch_id)
            if not summary:
                return 404, {"Content-Type": "application/json; charset=utf-8"}, json.dumps({"error": "Batch not found"}).encode("utf-8")
            return 200, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(summary).encode("utf-8")

        # Review Queue HTTP endpoints
        if m == "GET" and p == "/api/review-queue/pending":
            items = self.get_review_queue()
            return 200, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(items).encode("utf-8")

        if m == "POST" and p == "/api/review-queue/action":
            try:
                payload: dict[str, Any] = {}
                if body:
                    raw_str = body.decode("utf-8") if isinstance(body, bytes) else body
                    payload = json.loads(raw_str)
                action = payload.get("action", "")
                item_id = payload.get("item_id", "")
                updates = payload.get("updates")
                notes = payload.get("notes", "")
                res = self.dispatch_review_action(action, item_id, updates=updates, notes=notes)
                return 200, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(res).encode("utf-8")
            except Exception as e:
                return 400, {"Content-Type": "application/json; charset=utf-8"}, json.dumps({"success": False, "error": str(e)}).encode("utf-8")

        # Receipt OCR HTTP endpoints (Stage 7C)
        if m == "POST" and p == "/api/receipt/upload":
            try:
                filename = "receipt.png"
                content_bytes = b""
                if body:
                    if isinstance(body, bytes):
                        # Check if JSON or raw bytes
                        try:
                            payload = json.loads(body.decode("utf-8"))
                            filename = payload.get("filename", "receipt.png")
                            c_raw = payload.get("content", "")
                            content_bytes = c_raw.encode("latin-1") if isinstance(c_raw, str) else bytes(c_raw)
                        except Exception:
                            filename = q.get("filename", ["receipt.png"])[0]
                            content_bytes = body
                    else:
                        payload = json.loads(body)
                        filename = payload.get("filename", "receipt.png")
                        c_raw = payload.get("content", "")
                        content_bytes = c_raw.encode("latin-1") if isinstance(c_raw, str) else bytes(c_raw)

                out = self.process_receipt_upload(filename, content_bytes)
                return 200, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(out).encode("utf-8")
            except Exception as e:
                return 400, {"Content-Type": "application/json; charset=utf-8"}, json.dumps({"success": False, "error": str(e)}).encode("utf-8")

        if m == "POST" and p == "/api/receipt/confirm":
            try:
                payload: dict[str, Any] = {}
                if body:
                    raw_str = body.decode("utf-8") if isinstance(body, bytes) else body
                    payload = json.loads(raw_str)
                receipt_id = payload.get("receipt_id", "")
                corrections = payload.get("corrections")
                action = payload.get("action", "apply")
                reason = payload.get("reason", "")
                out = self.confirm_receipt_draft(receipt_id, corrections=corrections, action=action, reason=reason)
                return 200, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(out).encode("utf-8")
            except Exception as e:
                return 400, {"Content-Type": "application/json; charset=utf-8"}, json.dumps({"success": False, "error": str(e)}).encode("utf-8")

        if m == "GET" and p == "/api/quick-capture/balances":
            bals = self.get_balances()
            return 200, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(bals, default=_decimal_default).encode("utf-8")

        if m == "GET" and p == "/api/quick-capture/monthly-summary":
            import datetime as dt
            now = dt.datetime.now()
            year = int(q.get("year", [str(now.year)])[0])
            month = int(q.get("month", [str(now.month)])[0])
            summ = self.get_monthly_summary(year, month)
            return 200, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(summ, default=_decimal_default).encode("utf-8")

        if m == "GET" and p == "/api/quick-capture/pending":
            items = self.get_pending_reviews()
            return 200, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(items, default=_decimal_default).encode("utf-8")

        return 404, {"Content-Type": "text/plain"}, b"Not Found"

    def render_html(self) -> str:
        """Renders minimalist Vanilla HTML/JS interface with Quick Capture, Import Center, Review Queue, and Receipt OCR."""
        return """<!DOCTYPE html>
<html lang="id">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AturUang — Pusat Impor & Catat Cepat</title>
  <style>
    :root {
      --bg: #0f172a;
      --card: #1e293b;
      --border: #334155;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --primary: #10b981;
      --primary-hover: #059669;
      --danger: #ef4444;
      --warning: #f59e0b;
      --font: system-ui, -apple-system, sans-serif;
    }
    body {
      margin: 0;
      padding: 24px;
      font-family: var(--font);
      background-color: var(--bg);
      color: var(--text);
    }
    .container {
      max-width: 840px;
      margin: 0 auto;
    }
    .nav-tabs {
      display: flex;
      gap: 12px;
      margin-bottom: 24px;
      border-bottom: 1px solid var(--border);
      padding-bottom: 12px;
    }
    .tab-btn {
      background: none;
      border: none;
      color: var(--text-muted);
      font-size: 15px;
      font-weight: 600;
      cursor: pointer;
      padding: 8px 16px;
      border-radius: 8px;
    }
    .tab-btn.active {
      background: var(--card);
      color: var(--primary);
    }
    .tab-pane {
      display: none;
    }
    .tab-pane.active {
      display: block;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px;
      margin-bottom: 20px;
    }
    input[type="text"], input[type="number"], input[type="date"], textarea {
      width: 100%;
      box-sizing: border-box;
      padding: 12px 16px;
      font-size: 15px;
      background: #0f172a;
      border: 1px solid var(--border);
      border-radius: 8px;
      color: var(--text);
      outline: none;
    }
    .btn {
      display: inline-block;
      padding: 8px 16px;
      background: var(--primary);
      color: #fff;
      border: none;
      border-radius: 8px;
      cursor: pointer;
      font-weight: 600;
      font-size: 13px;
    }
    .btn-danger { background: var(--danger); }
    .badge {
      display: inline-block;
      padding: 4px 8px;
      border-radius: 4px;
      font-size: 12px;
      font-weight: 700;
    }
    .badge-success { background: #064e3b; color: #34d399; }
    .badge-review { background: #7c2d12; color: #fb923c; }
    .table {
      width: 100%;
      border-collapse: collapse;
      margin-top: 12px;
      font-size: 14px;
    }
    .table td, .table th {
      padding: 8px 6px;
      border-bottom: 1px solid var(--border);
      text-align: left;
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="nav-tabs">
      <button class="tab-btn active" onclick="switchTab('quick')">Catat Cepat</button>
      <button class="tab-btn" onclick="switchTab('import')">Pusat Impor</button>
      <button class="tab-btn" onclick="switchTab('review')">Antrean Tinjauan</button>
      <button class="tab-btn" onclick="switchTab('receipt')">Pindai Struk</button>
    </div>

    <!-- Tab 1: Catat Cepat -->
    <div id="tab-quick" class="tab-pane active">
      <div class="card">
        <input type="text" id="captureInput" placeholder='Ketik transaksi, misal: -25rb makan bakso gopay' autocomplete="off">
        <div id="previewArea" style="margin-top: 16px; display: none;"></div>
      </div>
    </div>

    <!-- Tab 2: Pusat Impor -->
    <div id="tab-import" class="tab-pane">
      <div class="card">
        <h2 style="font-size: 18px; margin-top: 0;">Unggah Berkas Mutasi (CSV / PDF)</h2>
        <input type="file" id="fileInput" accept=".csv,.pdf">
        <button class="btn" style="margin-top:12px;" onclick="uploadFile()">Unggah & Analisis</button>
        <div id="importResult" style="margin-top: 16px; display: none;"></div>
      </div>
    </div>

    <!-- Tab 3: Antrean Tinjauan -->
    <div id="tab-review" class="tab-pane">
      <div class="card">
        <h2 style="font-size: 18px; margin-top: 0;">Antrean Tinjauan Pemilik</h2>
        <div id="reviewList">Memuat antrean...</div>
      </div>
    </div>

    <!-- Tab 4: Pindai Struk -->
    <div id="tab-receipt" class="tab-pane">
      <div class="card">
        <h2 style="font-size: 18px; margin-top: 0;">Pindai Struk Belanja (OCR)</h2>
        <input type="file" id="receiptFileInput" accept=".png,.jpg,.jpeg">
        <button class="btn" style="margin-top:12px;" onclick="uploadReceipt()">Pindai Struk (OCR)</button>
        <div id="receiptResult" style="margin-top: 16px; display: none;"></div>
      </div>
    </div>

    <!-- Saldo Terkini -->
    <div class="card">
      <h2 style="font-size: 18px; margin-top: 0;">Saldo Akun Terkini</h2>
      <div id="balancesList">Memuat saldo...</div>
    </div>
  </div>

  <script>
    function esc(s) {
      if (!s) return '';
      return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    let currentPayload = null;
    let activeReceipt = null;

    function switchTab(t) {
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
      if (t === 'quick') {
        document.querySelector('.tab-btn:nth-child(1)').classList.add('active');
        document.getElementById('tab-quick').classList.add('active');
      } else if (t === 'import') {
        document.querySelector('.tab-btn:nth-child(2)').classList.add('active');
        document.getElementById('tab-import').classList.add('active');
      } else if (t === 'review') {
        document.querySelector('.tab-btn:nth-child(3)').classList.add('active');
        document.getElementById('tab-review').classList.add('active');
        loadReviewQueue();
      } else if (t === 'receipt') {
        document.querySelector('.tab-btn:nth-child(4)').classList.add('active');
        document.getElementById('tab-receipt').classList.add('active');
      }
    }

    async function loadBalances() {
      try {
        const res = await fetch('/api/quick-capture/balances');
        const data = await res.json();
        const container = document.getElementById('balancesList');
        if (!data.accounts || data.accounts.length === 0) {
          container.innerHTML = '<span style="color:var(--text-muted)">Belum ada data saldo akun.</span>';
          return;
        }
        let html = '<table class="table">';
        data.accounts.forEach(a => {
          html += `<tr><td>${esc(a.name)}</td><td>Rp ${esc(Number(a.current_balance || a.balance || 0).toLocaleString('id-ID'))}</td></tr>`;
        });
        html += `<tr><td><b>Total Saldo</b></td><td style="color:#34d399"><b>Rp ${esc(Number(data.total_balance || 0).toLocaleString('id-ID'))}</b></td></tr>`;
        html += '</table>';
        container.innerHTML = html;
      } catch (err) {
        document.getElementById('balancesList').innerText = 'Gagal memuat saldo.';
      }
    }

    async function updatePreview() {
      const inputEl = document.getElementById('captureInput');
      if (!inputEl) return;
      const q = inputEl.value.trim();
      const area = document.getElementById('previewArea');
      if (!q) {
        if (area) area.style.display = 'none';
        currentPayload = null;
        return;
      }
      try {
        const res = await fetch('/api/quick-capture/preview?q=' + encodeURIComponent(q));
        const data = await res.json();
        currentPayload = data;
        if (!area) return;
        area.style.display = 'block';

        if (data.status === 'SUCCESS') {
          area.innerHTML = `
            <div style="display:flex; justify-content:space-between; align-items:center;">
              <span class="badge badge-success">SIAP KONFIRMASI</span>
              <span class="hash">Hash: ${esc(data.preview_hash.substring(0, 16))}...</span>
            </div>
            <table class="table" style="margin-top:10px;">
              <tr><td>Tipe Transaksi</td><td><b>${esc(data.transaction_type)}</b></td></tr>
              <tr><td>Nominal</td><td><b>Rp ${esc(Number(data.amount).toLocaleString('id-ID'))}</b></td></tr>
              <tr><td>Akun Asal</td><td>${esc(data.account_from || '-')}</td></tr>
              <tr><td>Akun Tujuan</td><td>${esc(data.account_to || '-')}</td></tr>
              <tr><td>Deskripsi</td><td>${esc(data.description)}</td></tr>
              <tr><td>Kategori</td><td>${esc(data.category)}</td></tr>
            </table>
            <button class="btn" style="margin-top:12px;" onclick="confirmAndApply()">Konfirmasi & Terapkan</button>
          `;
        } else {
          area.innerHTML = `
            <span class="badge badge-review">PERLU DITINJAU</span>
            <div style="margin-top:8px; font-size:14px; color:#fb923c;">Alasan: ${esc(data.diagnostic_reason)}</div>
          `;
        }
      } catch (err) {
        if (area) area.innerHTML = '<span style="color:var(--danger)">Gagal menghasilkan pratinjau.</span>';
      }
    }

    async function confirmAndApply() {
      if (!currentPayload || currentPayload.status !== 'SUCCESS') return;
      try {
        const res = await fetch('/api/quick-capture/apply', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(currentPayload)
        });
        const result = await res.json();
        if (result.success) {
          alert('Transaksi berhasil diterapkan secara aman!');
          const inputEl = document.getElementById('captureInput');
          if (inputEl) inputEl.value = '';
          const area = document.getElementById('previewArea');
          if (area) area.style.display = 'none';
          currentPayload = null;
          loadBalances();
        } else {
          alert('Gagal menerapkan: ' + (result.error || 'Terjadi kesalahan.'));
        }
      } catch (err) {
        alert('Kesalahan jaringan saat menerapkan transaksi.');
      }
    }

    async function uploadFile() {
      const fin = document.getElementById('fileInput');
      if (!fin || !fin.files || fin.files.length === 0) {
        alert('Pilih berkas terlebih dahulu.');
        return;
      }
      const file = fin.files[0];
      const formData = new FormData();
      formData.append('file', file);
      const resDiv = document.getElementById('importResult');
      if (resDiv) {
        resDiv.style.display = 'block';
        resDiv.innerHTML = '<span>Sedang mengunggah dan menganalisis berkas...</span>';
      }
      try {
        const res = await fetch('/api/import/upload', {
          method: 'POST',
          body: formData
        });
        const data = await res.json();
        if (resDiv) {
          if (res.ok && data.status === 'SUCCESS') {
            resDiv.innerHTML = `
              <div style="color:var(--primary); font-weight:600; margin-bottom:8px;">Berkas berhasil diproses!</div>
              <table class="table">
                <tr><td>Batch ID</td><td><code>${esc(data.batch_id || '-')}</code></td></tr>
                <tr><td>Total Transaksi</td><td>${esc(data.total_discovered || data.total_transactions || 0)}</td></tr>
                <tr><td>Status Rekonsiliasi</td><td>${esc(data.reconciliation_status || data.status || '-')}</td></tr>
              </table>
            `;
            loadBalances();
          } else {
            resDiv.innerHTML = `<span style="color:var(--danger)">Gagal memproses: ${esc(data.error || data.diagnostic_reason || 'Terjadi kesalahan')}</span>`;
          }
        }
      } catch (err) {
        if (resDiv) resDiv.innerHTML = '<span style="color:var(--danger)">Kesalahan jaringan saat mengunggah.</span>';
      }
    }

    async function loadReviewQueue() {
      try {
        const res = await fetch('/api/review-queue/pending');
        const items = await res.json();
        const container = document.getElementById('reviewList');
        if (!items || items.length === 0) {
          container.innerHTML = '<span style="color:var(--text-muted)">Tidak ada transaksi yang memerlukan tinjauan.</span>';
          return;
        }
        let html = '<table class="table"><tr><th>Tanggal</th><th>Deskripsi</th><th>Nominal</th><th>Alasan</th><th>Aksi</th></tr>';
        items.forEach(it => {
          html += `<tr>
            <td>${esc(it.date)}</td>
            <td>${esc(it.description)}</td>
            <td>Rp ${esc(Number(it.amount).toLocaleString('id-ID'))}</td>
            <td style="color:#fb923c;">${esc(it.reason)}</td>
            <td>
              <button class="btn" onclick="reviewAction('approve', '${esc(it.item_id)}')">Setujui</button>
              <button class="btn btn-danger" onclick="reviewAction('reject', '${esc(it.item_id)}')">Tolak</button>
            </td>
          </tr>`;
        });
        html += '</table>';
        container.innerHTML = html;
      } catch (err) {
        document.getElementById('reviewList').innerText = 'Gagal memuat antrean tinjauan.';
      }
    }

    async function reviewAction(action, itemId) {
      try {
        const res = await fetch('/api/review-queue/action', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({action: action, item_id: itemId})
        });
        const data = await res.json();
        if (data.success) {
          alert('Aksi berhasil dijalankan.');
          loadReviewQueue();
          loadBalances();
        } else {
          alert('Gagal: ' + (data.error || 'Terjadi kesalahan.'));
        }
      } catch (err) {
        alert('Kesalahan jaringan.');
      }
    }

    async function uploadReceipt() {
      const fin = document.getElementById('receiptFileInput');
      if (!fin || !fin.files || fin.files.length === 0) {
        alert('Pilih berkas struk terlebih dahulu.');
        return;
      }
      const file = fin.files[0];
      const formData = new FormData();
      formData.append('file', file);
      const resDiv = document.getElementById('receiptResult');
      if (resDiv) {
        resDiv.style.display = 'block';
        resDiv.innerHTML = '<span>Sedang memindai teks struk (OCR)...</span>';
      }
      try {
        const res = await fetch('/api/receipt/upload?filename=' + encodeURIComponent(file.name), {
          method: 'POST',
          body: file
        });
        const data = await res.json();
        activeReceipt = data;
        if (resDiv) {
          if (res.ok && data.receipt_id) {
            resDiv.innerHTML = `
              <div style="color:var(--primary); font-weight:600; margin-bottom:8px;">Hasil Pemindaian Struk</div>
              <table class="table">
                <tr><td>Merchant</td><td><input type="text" id="rcptMerchant" value="${esc(data.merchant)}"></td></tr>
                <tr><td>Tanggal</td><td><input type="date" id="rcptDate" value="${esc(data.date)}"></td></tr>
                <tr><td>Nominal (Rp)</td><td><input type="number" step="0.01" id="rcptAmount" value="${esc(data.amount)}"></td></tr>
                <tr><td>Akun Pembayaran</td><td><input type="text" id="rcptAccount" value="${esc(data.account_from || 'Cash')}"></td></tr>
                <tr><td>Kategori</td><td><input type="text" id="rcptCategory" value="${esc(data.category || 'Shopping')}"></td></tr>
              </table>
              <button class="btn" style="margin-top:12px;" onclick="confirmReceipt()">Konfirmasi & Terapkan</button>
            `;
          } else {
            resDiv.innerHTML = `<span style="color:var(--danger)">Gagal memindai: ${esc(data.error || data.reason || 'Terjadi kesalahan')}</span>`;
          }
        }
      } catch (err) {
        if (resDiv) resDiv.innerHTML = '<span style="color:var(--danger)">Kesalahan jaringan saat memindai struk.</span>';
      }
    }

    async function confirmReceipt() {
      if (!activeReceipt) return;
      const corrections = {
        merchant: document.getElementById('rcptMerchant').value,
        date: document.getElementById('rcptDate').value,
        amount: document.getElementById('rcptAmount').value,
        account_from: document.getElementById('rcptAccount').value,
        category: document.getElementById('rcptCategory').value
      };
      try {
        const res = await fetch('/api/receipt/confirm', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            receipt_id: activeReceipt.receipt_id,
            corrections: corrections,
            action: 'apply'
          })
        });
        const data = await res.json();
        if (data.success) {
          alert('Struk berhasil dikonfirmasi dan diterapkan!');
          document.getElementById('receiptResult').style.display = 'none';
          document.getElementById('receiptFileInput').value = '';
          activeReceipt = null;
          loadBalances();
        } else {
          alert('Gagal menerapkan: ' + (data.error || 'Terjadi kesalahan.'));
        }
      } catch (err) {
        alert('Kesalahan jaringan.');
      }
    }

    let debounceTimer;
    const captureInputEl = document.getElementById('captureInput');
    if (captureInputEl) {
      captureInputEl.addEventListener('input', () => {
        clearTimeout(debounceTimer);
        debounceTimer = setTimeout(updatePreview, 250);
      });
    }

    loadBalances();
  </script>
</body>
</html>"""
