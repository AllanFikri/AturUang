"""
Universal Ingestion Stage 7A — Local Web Composer & Preview Binding.

Provides a clean, minimalist web interface and JSON API allowing the Owner to:
1. Parse natural Indonesian financial grammar into candidates with deterministic preview hashes.
2. Confirm and apply candidates atomically through the Stage 6 Safe Apply engine.
3. Query real-time account balances and monthly summaries with zero write risk.
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


def _decimal_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return f"{obj:.2f}"
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


class QuickCaptureComposer:
    """Owner Experience MVP: Local Web Composer and Preview Binding for Quick Capture."""

    def __init__(
        self,
        db_path: Path | str,
        backup_dir: Path | str | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.backup_dir = Path(backup_dir) if backup_dir else self.db_path.parent / "backups"
        self.engine = SafeApplyEngine(self.db_path, self.backup_dir)

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
        """Confirms candidate and applies it atomically through SafeApplyEngine with pre-apply backup."""
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

    def get_balances(self) -> dict[str, Any]:
        """Returns read-only account balances."""
        return get_account_balances(self.db_path)

    def get_monthly_summary(self, year: int, month: int) -> dict[str, Any]:
        """Returns read-only monthly cash flow summary."""
        return get_monthly_summary(year, month, self.db_path)

    def get_pending_reviews(self) -> list[dict[str, Any]]:
        """Returns pending review queue."""
        return get_pending_reviews(self.db_path)

    def handle_request(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]] | None = None,
        body: bytes | str | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        """Dispatches HTTP requests for local web composer UI and JSON endpoints."""
        m = method.upper()
        p = path.rstrip("/") or "/"
        q = query or {}

        if m == "GET" and (p in ("/", "/quick-capture")):
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
        """Renders minimalist Vanilla HTML/JS interface for Quick Capture."""
        return """<!DOCTYPE html>
<html lang="id">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AturUang — Quick Capture & Saldo Cepat</title>
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
      max-width: 760px;
      margin: 0 auto;
    }
    h1 {
      font-size: 24px;
      margin-bottom: 8px;
    }
    .subtitle {
      color: var(--text-muted);
      margin-bottom: 24px;
      font-size: 14px;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px;
      margin-bottom: 20px;
    }
    input[type="text"] {
      width: 100%;
      box-sizing: border-box;
      padding: 12px 16px;
      font-size: 16px;
      background: #0f172a;
      border: 1px solid var(--border);
      border-radius: 8px;
      color: var(--text);
      outline: none;
    }
    input[type="text"]:focus {
      border-color: var(--primary);
    }
    .btn {
      display: inline-block;
      padding: 10px 20px;
      background: var(--primary);
      color: #fff;
      border: none;
      border-radius: 8px;
      cursor: pointer;
      font-weight: 600;
      font-size: 14px;
      margin-top: 12px;
    }
    .btn:hover {
      background: var(--primary-hover);
    }
    .badge {
      display: inline-block;
      padding: 4px 8px;
      border-radius: 4px;
      font-size: 12px;
      font-weight: 700;
    }
    .badge-success { background: #064e3b; color: #34d399; }
    .badge-review { background: #7c2d12; color: #fb923c; }
    .preview-table {
      width: 100%;
      border-collapse: collapse;
      margin-top: 12px;
      font-size: 14px;
    }
    .preview-table td {
      padding: 6px 0;
    }
    .preview-table td.label {
      color: var(--text-muted);
      width: 140px;
    }
    .hash {
      font-family: monospace;
      font-size: 12px;
      color: #38bdf8;
      word-break: break-all;
    }
  </style>
</head>
<body>
  <div class="container">
    <h1>Catat Cepat (Quick Capture)</h1>
    <div class="subtitle">Ketik transaksi informal: contoh "-25rb makan bakso gopay" atau "+5jt gaji bca"</div>

    <div class="card">
      <input type="text" id="captureInput" placeholder='Ketik transaksi, misal: -25rb makan bakso gopay' autocomplete="off">
      <div id="previewArea" style="margin-top: 16px; display: none;"></div>
    </div>

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

    async function loadBalances() {
      try {
        const res = await fetch('/api/quick-capture/balances');
        const data = await res.json();
        const container = document.getElementById('balancesList');
        if (!data.accounts || data.accounts.length === 0) {
          container.innerHTML = '<span style="color:var(--text-muted)">Belum ada data saldo akun.</span>';
          return;
        }
        let html = '<table class="preview-table">';
        data.accounts.forEach(a => {
          html += `<tr><td class="label">${esc(a.name)}</td><td>Rp ${esc(Number(a.current_balance || a.balance || 0).toLocaleString('id-ID'))}</td></tr>`;
        });
        html += `<tr><td class="label" style="font-weight:700;color:var(--text)">Total Saldo</td><td style="font-weight:700;color:#34d399">Rp ${esc(Number(data.total_balance || 0).toLocaleString('id-ID'))}</td></tr>`;
        html += '</table>';
        container.innerHTML = html;
      } catch (err) {
        document.getElementById('balancesList').innerText = 'Gagal memuat saldo.';
      }
    }

    async function updatePreview() {
      const q = document.getElementById('captureInput').value.trim();
      const area = document.getElementById('previewArea');
      if (!q) {
        area.style.display = 'none';
        currentPayload = null;
        return;
      }
      try {
        const res = await fetch('/api/quick-capture/preview?q=' + encodeURIComponent(q));
        const data = await res.json();
        currentPayload = data;
        area.style.display = 'block';

        if (data.status === 'SUCCESS') {
          area.innerHTML = `
            <div style="display:flex; justify-content:space-between; align-items:center;">
              <span class="badge badge-success">SIAP KONFIRMASI</span>
              <span class="hash">Hash: ${esc(data.preview_hash.substring(0, 16))}...</span>
            </div>
            <table class="preview-table">
              <tr><td class="label">Tipe Transaksi</td><td><b>${esc(data.transaction_type)}</b></td></tr>
              <tr><td class="label">Nominal</td><td><b>Rp ${esc(Number(data.amount).toLocaleString('id-ID'))}</b></td></tr>
              <tr><td class="label">Akun Asal</td><td>${esc(data.account_from || '-')}</td></tr>
              <tr><td class="label">Akun Tujuan</td><td>${esc(data.account_to || '-')}</td></tr>
              <tr><td class="label">Deskripsi</td><td>${esc(data.description)}</td></tr>
              <tr><td class="label">Kategori</td><td>${esc(data.category)}</td></tr>
            </table>
            <button class="btn" onclick="confirmAndApply()">Konfirmasi & Terapkan</button>
          `;
        } else {
          area.innerHTML = `
            <span class="badge badge-review">PERLU DITINJAU</span>
            <div style="margin-top:8px; font-size:14px; color:#fb923c;">Alasan: ${esc(data.diagnostic_reason)}</div>
          `;
        }
      } catch (err) {
        area.innerHTML = '<span style="color:var(--danger)">Gagal menghasilkan pratinjau.</span>';
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
          document.getElementById('captureInput').value = '';
          document.getElementById('previewArea').style.display = 'none';
          currentPayload = null;
          loadBalances();
        } else {
          alert('Gagal menerapkan: ' + (result.error || 'Terjadi kesalahan.'));
        }
      } catch (err) {
        alert('Kesalahan jaringan saat menerapkan transaksi.');
      }
    }

    let debounceTimer;
    document.getElementById('captureInput').addEventListener('input', () => {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(updatePreview, 250);
    });

    loadBalances();
  </script>
</body>
</html>"""
