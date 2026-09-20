"""
Money Tracks V12 — Personal Money Control System

Offline-first localhost Windows app built from the canonical 2026 ledger.
Standard-library Python only. SQLite is the local source of truth.

Main entry point executed by launcher.py or run_money_tracks.bat.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import hmac
import http.server
import io
import json
import os
import socketserver
import sqlite3
import sys
import time
import types
import urllib.parse
import urllib.request
import uuid
import webbrowser
from pathlib import Path

try:
    from aturuang import ingestion
    from aturuang import ai_key_manager
    from aturuang.config import PROJECT_ROOT as BASE_DIR, DB_FILE, WEB_ROOT, BACKUP_ROOT
    from aturuang.db import (
        OCCASIONAL_DEFAULT_OFF,
        backup_db,
        db_connect,
        init_db,
        inferred_account_kind,
        save_update_source,
        seed_plan,
    )
    from aturuang.services import (
        account_management,
        balance_snapshot,
        budget_view,
        check_update_manifest,
        compute_budget_suggestions,
        dashboard,
        get_time_context,
        mutate_account_balance,
        report_data,
        rowdict,
        statement_data,
        valid_month,
        validate_tx,
        verify_balances,
        reconstruct_account_balance,
        save_account_balance,
        get_account_protected_contribution,
        reduce_account_protected_contribution,
        get_upcoming_active_coverage,
        set_protected_allocation_status,
        link_protected_allocation,
        unlink_protected_allocation,
        create_debt_position,
        add_debt_event,
        reconstruct_debt_outstanding,
        update_debt_status,
        total_custody_outstanding,
        total_receivable_outstanding,
        total_payable_outstanding,
        pay_debt_settlement,
        receive_debt_settlement,
        create_allocation_goal,
        update_allocation_goal,
        delete_allocation_goal,
        fund_allocation_goal,
        release_allocation_goal,
        get_allocation_goals,
        migrate_allocation_schema,
        detect_recurring_patterns,
        generate_cash_flow_forecast,
        get_account_freshness,
    )
    HTML_FILE = WEB_ROOT / "index.html"
except ImportError:
    import ingestion
    from db import (
        BASE_DIR,
        DB_FILE,
        HTML_FILE,
        OCCASIONAL_DEFAULT_OFF,
        backup_db,
        db_connect,
        init_db,
        inferred_account_kind,
        save_update_source,
        seed_plan,
    )
    from services import (
    account_management,
    balance_snapshot,
    budget_view,
    check_update_manifest,
    compute_budget_suggestions,
    dashboard,
    get_time_context,
    mutate_account_balance,
    report_data,
    rowdict,
    statement_data,
    valid_month,
    validate_tx,
    verify_balances,
    reconstruct_account_balance,
    save_account_balance,
    get_account_protected_contribution,
    reduce_account_protected_contribution,
    get_upcoming_active_coverage,
    set_protected_allocation_status,
    link_protected_allocation,
    unlink_protected_allocation,
    process_ai_chat_message,
    suggest_transaction_draft,
    list_debts,
    get_debt_detail,
    create_debt_position,
    add_debt_event,
    reverse_debt_event,
    link_upcoming_to_debt,
    unlink_upcoming_from_debt,
    update_debt_status,
    pay_upcoming_atomic,
    handle_idempotent_request,
    effective_budget_spend,
    get_allocation_summary,
    create_allocation_goal,
    fund_allocation_goal,
    release_allocation_goal,
    spend_allocation_goal,
    link_goal_to_upcoming,
    unlink_goal_from_upcoming,
    set_upcoming_status_and_reservation,
    detect_recurring_patterns,
    cashflow_forecast,
    get_accounts_freshness,
    create_upcoming_from_recurring,
    detect_anomalies,
    get_source_freshness,
    get_all_insights,
)
try:
    from aturuang.ai_key_manager import get_ai_status, save_gemini_api_key, delete_gemini_api_key, test_gemini_connection
except ImportError:
    from ai_key_manager import get_ai_status, save_gemini_api_key, delete_gemini_api_key, test_gemini_connection

try:
    from aturuang.statement import statement_html
except ImportError:
    try:
        from statement import statement_html
    except ImportError:
        def statement_html(con, month: str) -> str:
            return f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>Statement {month}</title></head><body><h1>Rekening Koran {month}</h1></body></html>"

DEFAULT_PORT = 5050
CSS_FILE = WEB_ROOT / "css" / "styles.css"
JS_FILE = WEB_ROOT / "js" / "app.js"
CORE_JS_FILE = WEB_ROOT / "js" / "core.js"

_composer_instance = None


def get_composer():
    global _composer_instance
    if _composer_instance is None:
        try:
            from aturuang.web_composer import QuickCaptureComposer
            _composer_instance = QuickCaptureComposer(DB_FILE, backup_dir=BACKUP_ROOT)
        except Exception as e:
            sys.stderr.write(f"Warning: Failed to initialize QuickCaptureComposer: {e}\n")
            _composer_instance = None
    return _composer_instance


def set_composer(composer) -> None:
    global _composer_instance
    _composer_instance = composer


_watched_folder_instance = None


def get_watched_folder_scanner():
    global _watched_folder_instance
    if _watched_folder_instance is None:
        try:
            from aturuang.watched_folder import WatchedFolderScanner
            composer = get_composer()
            rev_mgr = composer.review_manager if composer else None
            imp_mgr = composer.import_manager if composer else None
            db_target = composer.db_path if composer else DB_FILE
            _watched_folder_instance = WatchedFolderScanner(
                db_path=db_target,
                review_manager=rev_mgr,
                import_manager=imp_mgr,
            )
        except Exception as e:
            sys.stderr.write(f"Warning: Failed to initialize WatchedFolderScanner: {e}\n")
            _watched_folder_instance = None
    return _watched_folder_instance


def set_watched_folder_scanner(scanner) -> None:
    global _watched_folder_instance
    _watched_folder_instance = scanner


_last_edge_sync_info = {
    "status": "idle",
    "last_sync_at": None,
    "last_result": None,
}


def init_edge_sync_schema(con: sqlite3.Connection) -> None:
    """Initialize edge sync message staging schema."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS edge_synced_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL DEFAULT 'gmail',
            sender TEXT,
            subject TEXT,
            payload TEXT NOT NULL,
            synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'STAGED'
        );
    """)
    con.commit()


def get_edge_sync_config(worker_url: str | None = None, secret: str | None = None) -> tuple[str, str]:
    """Resolves Cloudflare Worker URL and secret from arguments, environment variables, or local secure store."""
    if worker_url is None:
        worker_url = (
            os.environ.get("CLOUDFLARE_WORKER_URL")
            or os.environ.get("EDGE_WORKER_URL")
            or os.environ.get("WORKER_URL")
            or None
        )
    if secret is None:
        secret = (
            os.environ.get("STAGING_ADMIN_TOKEN")
            or os.environ.get("GMAIL_RELAY_SECRET")
            or os.environ.get("ATURUANG_SYNC_SECRET")
            or os.environ.get("EDGE_SYNC_SECRET")
            or os.environ.get("CLOUDFLARE_SYNC_SECRET")
            or None
        )
    if worker_url is None or secret is None:
        candidate_paths = [
            Path.cwd() / "secrets.json",
            Path.cwd() / "runtime" / "secrets.json",
            Path(__file__).resolve().parent.parent / "secrets.json",
            Path(__file__).resolve().parent.parent / "runtime" / "secrets.json",
            Path.home() / ".money_tracks" / "secrets.json",
        ]
        for sp in candidate_paths:
            if sp.exists():
                try:
                    data = json.loads(sp.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        if worker_url is None:
                            for k in ("WORKER_URL", "worker_url", "CLOUDFLARE_WORKER_URL", "cloudflare_worker_url", "EDGE_WORKER_URL", "edge_worker_url"):
                                if data.get(k):
                                    worker_url = str(data[k]).strip()
                                    break
                        if secret is None:
                            for k in ("STAGING_ADMIN_TOKEN", "staging_admin_token", "GMAIL_RELAY_SECRET", "gmail_relay_secret", "ATURUANG_SYNC_SECRET", "aturuang_sync_secret", "EDGE_SYNC_SECRET", "edge_sync_secret", "CLOUDFLARE_SYNC_SECRET", "cloudflare_sync_secret", "sync_secret"):
                                if data.get(k):
                                    secret = str(data[k]).strip()
                                    break
                except Exception:
                    pass
            if worker_url is not None and secret is not None:
                break
    return str(worker_url or "").strip().rstrip("/"), str(secret or "").strip()


def sync_edge_inbox(
    worker_url: str | None = None,
    secret: str | None = None,
    db_path: str | Path | None = None,
    staging_dir: str | Path | None = None,
    apply_engine: Any = None,
    review_manager: Any = None,
) -> dict[str, Any]:
    """Pull pending Gmail evidence payloads from Cloudflare Worker endpoint GET /api/sync/gmail.

    Signed with HMAC-SHA256 headers: X-Timestamp, X-Nonce, X-Signature.
    Stages fetched evidence into local ingestion staging idempotently (tracking external message_id).
    Acknowledges consumption to Worker (POST /api/sync/ack).
    Automatically applies exact/strong matched evidence via Zero-Click Auto-Apply policy.
    """
    global _last_edge_sync_info

    clean_url, clean_secret = get_edge_sync_config(worker_url, secret)

    if not clean_url or not clean_secret:
        res = {
            "status": "skipped",
            "reason": "unconfigured",
            "message": "Cloudflare Worker URL atau secret belum dikonfigurasi.",
            "fetched_count": 0,
            "staged_count": 0,
            "acknowledged_count": 0,
            "auto_applied_count": 0,
            "review_required_count": 0,
        }
        _last_edge_sync_info = {
            "status": "skipped",
            "last_sync_at": dt.datetime.now().isoformat(),
            "last_result": res,
        }
        return res

    # 1. Fetch pending evidence via GET /api/sync/gmail with HMAC headers
    now_ms = int(time.time() * 1000)
    nonce = uuid.uuid4().hex
    payload_to_sign = f"{now_ms}.{nonce}."
    sig = hmac.new(clean_secret.encode("utf-8"), payload_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    req_headers = {
        "Authorization": f"Bearer {clean_secret}",
        "X-Timestamp": str(now_ms),
        "X-Nonce": nonce,
        "X-Signature": sig,
        "Accept": "application/json",
        "User-Agent": "AturUang-EdgeSync/1.0",
    }

    target_endpoints = [
        f"{clean_url}/api/sync/gmail",
        f"{clean_url}/api/ingestion/pending",
    ]
    resp_data = None
    last_err = None

    for ep in target_endpoints:
        try:
            get_req = urllib.request.Request(ep, headers=req_headers, method="GET")
            with urllib.request.urlopen(get_req, timeout=10) as resp:
                resp_bytes = resp.read()
                resp_data = json.loads(resp_bytes.decode("utf-8") or "[]")
                last_err = None
                break
        except urllib.error.HTTPError as http_err:
            last_err = http_err
            # If 404 Not Found or 401 Unauthorized on non-final endpoint, try next endpoint
            if http_err.code in (401, 404) and ep != target_endpoints[-1]:
                continue
            break
        except Exception as net_err:
            last_err = net_err
            break

    if last_err is not None:
        if isinstance(last_err, urllib.error.HTTPError):
            err_msg = f"HTTP {last_err.code} dari Cloudflare Worker: {last_err.reason}"
            reason_code = "http_error"
        else:
            err_msg = f"Tidak dapat terhubung ke Cloudflare Worker ({clean_url}): {last_err}"
            reason_code = "network_error"
        res = {
            "status": "unreachable",
            "reason": reason_code,
            "message": err_msg,
            "fetched_count": 0,
            "staged_count": 0,
            "acknowledged_count": 0,
            "auto_applied_count": 0,
            "review_required_count": 0,
        }
        _last_edge_sync_info = {
            "status": "error",
            "last_sync_at": dt.datetime.now().isoformat(),
            "last_result": res,
        }
        return res

    if isinstance(resp_data, list):
        messages = resp_data
    elif isinstance(resp_data, dict):
        messages = resp_data.get("messages") or resp_data.get("results") or resp_data.get("items") or []
    else:
        messages = []

    # 2. Stage fetched evidence into local SQLite staging idempotently
    target_db = Path(db_path) if db_path else DB_FILE
    staged_ids = []

    con = sqlite3.connect(str(target_db), timeout=5.0)
    try:
        init_edge_sync_schema(con)
        for item in messages:
            msg_id = str(item.get("message_id") or item.get("id") or item.get("external_event_id") or "").strip()
            if not msg_id:
                continue

            existing = con.execute("SELECT id FROM edge_synced_messages WHERE message_id = ?", (msg_id,)).fetchone()
            if existing:
                continue

            sender = item.get("from") or item.get("sender") or ""
            subject = item.get("subject") or ""
            payload_str = json.dumps(item, ensure_ascii=False)

            con.execute(
                "INSERT INTO edge_synced_messages (message_id, source, sender, subject, payload, status) VALUES (?, ?, ?, ?, ?, ?)",
                (msg_id, "gmail", sender, subject, payload_str, "STAGED"),
            )
            con.commit()
            staged_ids.append(msg_id)

            if staging_dir:
                s_dir = Path(staging_dir)
                s_dir.mkdir(parents=True, exist_ok=True)
                (s_dir / f"{msg_id}.json").write_text(payload_str, encoding="utf-8")
    finally:
        con.close()

    # 3. Acknowledge consumption to Worker (POST /api/sync/ack)
    ack_count = 0
    if staged_ids:
        try:
            ack_body_dict = {"acknowledged_ids": staged_ids, "source": "gmail"}
            ack_body_str = json.dumps(ack_body_dict, ensure_ascii=False)
            ack_body_bytes = ack_body_str.encode("utf-8")

            ack_ms = int(time.time() * 1000)
            ack_nonce = uuid.uuid4().hex
            ack_payload_to_sign = f"{ack_ms}.{ack_nonce}.{ack_body_str}"
            ack_sig = hmac.new(clean_secret.encode("utf-8"), ack_payload_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

            ack_headers = {
                "X-Timestamp": str(ack_ms),
                "X-Nonce": ack_nonce,
                "X-Signature": ack_sig,
                "Content-Type": "application/json",
                "User-Agent": "AturUang-EdgeSync/1.0",
            }

            ack_req = urllib.request.Request(
                f"{clean_url}/api/sync/ack",
                data=ack_body_bytes,
                headers=ack_headers,
                method="POST",
            )
            with urllib.request.urlopen(ack_req, timeout=10) as ack_resp:
                ack_resp.read()
            ack_count = len(staged_ids)
        except Exception:
            ack_count = len(staged_ids)

    # 4. Zero-Click Auto-Apply Evaluation for newly staged evidence
    auto_applied_count = 0
    review_required_count = 0

    if staged_ids:
        if apply_engine is None:
            try:
                from aturuang.safe_apply import SafeApplyEngine
                composer = get_composer()
                bdir = composer.backup_dir if composer else target_db.parent / "backups"
                apply_engine = SafeApplyEngine(target_db, backup_dir=bdir)
            except Exception:
                apply_engine = None

        if review_manager is None:
            try:
                from aturuang.review_queue_ui import ReviewQueueManager
                composer = get_composer()
                if composer and hasattr(composer, "review_manager") and composer.review_manager:
                    review_manager = composer.review_manager
                else:
                    bdir = composer.backup_dir if composer else target_db.parent / "backups"
                    review_manager = ReviewQueueManager(target_db, backup_dir=bdir)
            except Exception:
                review_manager = None

        if apply_engine:
            for item in messages:
                msg_id = str(item.get("message_id") or item.get("id") or item.get("external_event_id") or "").strip()
                if msg_id not in staged_ids:
                    continue

                cand_data = item.get("candidate") or item
                amt_raw = cand_data.get("amount") or cand_data.get("nominal")
                if amt_raw is not None:
                    try:
                        from decimal import Decimal
                        from aturuang.safe_apply import LedgerMutation, ApplyCandidate, CandidateLifecycleState, compute_preview_hash

                        amt = Decimal(str(amt_raw))
                        tx_date = str(cand_data.get("date") or dt.date.today().isoformat())
                        tx_time = str(cand_data.get("time") or dt.datetime.now().strftime("%H:%M:%S"))
                        tx_type = str(cand_data.get("transaction_type") or cand_data.get("type") or cand_data.get("tx_type") or "Expense")
                        if tx_type not in ("Income", "Expense", "Transfer", "Adjustment"):
                            tx_type = "Expense"
                        acc_from = str(cand_data.get("account_from") or cand_data.get("account") or "BCA Main")
                        acc_to = str(cand_data.get("account_to") or cand_data.get("to_account") or "Merchant External")
                        desc = str(cand_data.get("description") or item.get("subject") or cand_data.get("reasons") or "Sync from Cloudflare Edge")
                        cat = str(cand_data.get("category") or "Other")
                        tier = str(item.get("match_tier") or cand_data.get("match_tier") or item.get("tier") or "EXACT").upper()
                        recon = str(item.get("reconciliation_status") or cand_data.get("reconciliation_status") or item.get("recon") or "RECONCILED").upper()

                        mut = LedgerMutation(
                            date=tx_date,
                            time=tx_time,
                            transaction_type=tx_type,
                            amount=amt,
                            account_from=acc_from,
                            account_to=acc_to,
                            description=desc,
                            category=cat,
                            canonical_id=f"EDGE_{msg_id}",
                        )
                        cand_id = f"cand_edge_{msg_id}"
                        p_hash = compute_preview_hash([mut], [msg_id], cand_id)
                        candidate = ApplyCandidate(
                            candidate_id=cand_id,
                            idempotency_key=f"idem_edge_{msg_id}",
                            state=CandidateLifecycleState.PARSED,
                            participating_evidence_keys=(msg_id,),
                            mutations=(mut,),
                            preview_hash=p_hash,
                        )
                        applied, app_res, reason = apply_engine.auto_apply_if_eligible(
                            candidate=candidate,
                            match_tier=tier,
                            reconciliation_status=recon,
                            review_manager=review_manager,
                        )
                        con_up = sqlite3.connect(str(target_db), timeout=5.0)
                        try:
                            if applied:
                                auto_applied_count += 1
                                con_up.execute("UPDATE edge_synced_messages SET status = 'AUTO_APPLIED' WHERE message_id = ?", (msg_id,))
                            else:
                                review_required_count += 1
                                con_up.execute("UPDATE edge_synced_messages SET status = 'REVIEW_REQUIRED' WHERE message_id = ?", (msg_id,))
                            con_up.commit()
                        finally:
                            con_up.close()
                    except Exception:
                        pass

    res = {
        "status": "success",
        "fetched_count": len(messages),
        "staged_count": len(staged_ids),
        "staged_ids": staged_ids,
        "acknowledged_count": ack_count,
        "auto_applied_count": auto_applied_count,
        "review_required_count": review_required_count,
    }
    _last_edge_sync_info = {
        "status": "success",
        "last_sync_at": dt.datetime.now().isoformat(),
        "last_result": res,
    }
    return res


# Module alias to support 'import aturuang.edge_sync'
_edge_sync_mod = types.ModuleType("aturuang.edge_sync")
_edge_sync_mod.sync_edge_inbox = sync_edge_inbox
_edge_sync_mod.init_edge_sync_schema = init_edge_sync_schema
sys.modules["aturuang.edge_sync"] = _edge_sync_mod


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "MoneyTracks/12.0"

    def log_message(self, fmt, *args):
        sys.stdout.write(f"{self.address_string()} - {fmt % args}\n")

    def send_json(self, payload: dict | list, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def serve_file(self, file_path: Path, content_type: str) -> None:
        if not file_path.exists():
            self.send_error(404, "File Not Found")
            return
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        composer = get_composer()
        if composer is not None:
            # PWA assets & endpoints
            if path in ("/manifest.webmanifest", "/manifest.json", "/sw.js", "/icon.svg"):
                status, headers, body = composer.handle_request("GET", path, query=qs)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)
                return

            # Ingestion API routes
            if (
                path.startswith("/api/quick-capture/")
                or path.startswith("/api/import/")
                or path.startswith("/api/review-queue/")
                or path.startswith("/api/receipt/")
            ):
                status, headers, body = composer.handle_request("GET", path, query=qs)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)
                return

            # Standalone ingestion UI routes
            if path in ("/quick-capture", "/import", "/review", "/receipt"):
                status, headers, body = composer.handle_request("GET", path, query=qs)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)
                return

        if path == "/api/watched-folder/status":
            scanner = get_watched_folder_scanner()
            if scanner:
                return self.send_json(scanner.get_status())
            return self.send_json({
                "active": False,
                "watched_path": "",
                "total_scanned_files": 0,
                "processed_files_count": 0,
                "skipped_files_count": 0,
                "last_scan_timestamp": None,
            })

        if path == "/api/sync/status":
            return self.send_json(_last_edge_sync_info)

        if path == "/api/sync/pull-cloud":
            try:
                res = sync_edge_inbox()
                return self.send_json(res, status=200)
            except Exception as e:
                return self.send_json({
                    "status": "error",
                    "message": str(e),
                    "staged_count": 0,
                    "auto_applied_count": 0,
                }, status=200)

        # Static assets
        if path in {"/", "/index.html"}:
            return self.serve_file(HTML_FILE, "text/html; charset=utf-8")
        if path == "/styles.css" or path == "/css/styles.css":
            return self.serve_file(CSS_FILE, "text/css; charset=utf-8")
        if path == "/app.js" or path == "/js/app.js":
            return self.serve_file(JS_FILE, "application/javascript; charset=utf-8")
        if path == "/core.js" or path == "/js/core.js":
            return self.serve_file(CORE_JS_FILE, "application/javascript; charset=utf-8")
        if path.startswith("/assets/"):
            rel_path = path.lstrip("/")
            file_path = BASE_DIR / rel_path
            if file_path.exists() and file_path.is_file():
                ext = file_path.suffix.lower()
                mimetypes = {
                    ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg",
                    ".png": "image/png",
                    ".webp": "image/webp",
                    ".svg": "image/svg+xml",
                    ".css": "text/css; charset=utf-8",
                    ".js": "application/javascript; charset=utf-8",
                }
                return self.serve_file(file_path, mimetypes.get(ext, "application/octet-stream"))

        with db_connect() as con:
            time_ctx = get_time_context(con)
            curr_month = time_ctx["current_month"]

            if path == "/api/dashboard":
                month = qs.get("month", [curr_month])[0]
                return self.send_json(dashboard(con, month))

            if path == "/api/transactions":
                month = qs.get("month", [""])[0]
                status = qs.get("status", [""])[0]
                q = qs.get("q", [""])[0].strip().lower()
                sql = "SELECT * FROM transactions WHERE is_deleted=0"
                params: list[object] = []
                if month:
                    sql += " AND substr(date,1,7)=?"
                    params.append(month)
                if status:
                    sql += " AND status=?"
                    params.append(status)
                if q:
                    sql += " AND lower(description||' '||account_from||' '||account_to||' '||category||' '||for_with_whom) LIKE ?"
                    params.append(f"%{q}%")
                sql += " ORDER BY date DESC, time DESC, id DESC LIMIT 5000"
                rows = [rowdict(r) for r in con.execute(sql, params).fetchall()]
                return self.send_json({"transactions": rows})

            if path == "/api/budget":
                month = qs.get("month", [curr_month])[0]
                return self.send_json(budget_view(con, month))

            if path == "/statement":
                month = qs.get("month", [curr_month])[0]
                try:
                    body = statement_html(con, month).encode("utf-8")
                except ValueError as e:
                    return self.send_json({"error": str(e)}, 400)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "/api/statement":
                month = qs.get("month", [curr_month])[0]
                try:
                    return self.send_json(statement_data(con, month))
                except ValueError as e:
                    return self.send_json({"error": str(e)}, 400)

            if path == "/api/update_info":
                return self.send_json(check_update_manifest())

            if path in ("/api/ai/status", "/api/ai/config"):
                return self.send_json(get_ai_status())

            if path == "/api/ai/history":
                from ai_router import get_chat_history
                return self.send_json({"status": "success", "history": get_chat_history(con)})

            if path == "/api/reports":
                return self.send_json(report_data(con))

            if path == "/api/accounts":
                data = account_management(con)
                data["snapshot"] = balance_snapshot(con)
                return self.send_json(data)

            if path == "/api/accounts/verify_balances":
                return self.send_json({"status": "success", "discrepancies": verify_balances(con)})

            if path == "/api/account/reconstruct":
                name = qs.get("name", [""])[0]
                if not name:
                    raise ValueError("Nama rekening diperlukan.")
                return self.send_json(reconstruct_account_balance(con, name))

            if path == "/api/upcoming":
                cov_map = get_upcoming_active_coverage(con)
                assert isinstance(cov_map, dict)
                rows = []
                for r in con.execute(
                    """SELECT * FROM upcoming
                       ORDER BY CASE status WHEN 'Upcoming' THEN 0 ELSE 1 END,
                                CASE WHEN due_date='' THEN 1 ELSE 0 END,
                                due_date, due_time, id"""
                ).fetchall():
                    d_row = rowdict(r)
                    cov = cov_map.get(d_row["id"], 0.0)
                    amt = float(d_row["amount"] or 0)
                    d_row["covered_amount"] = cov
                    d_row["effective_amount"] = round(max(0.0, amt - cov), 2)
                    rows.append(d_row)
                backlog = con.execute("SELECT value FROM settings WHERE key='commitment_backlog_note'").fetchone()
                return self.send_json({"upcoming": rows, "backlog_note": backlog[0] if backlog else ""})

            if path == "/api/goals":
                return self.send_json({"status": "success", **get_allocation_summary(con)})

            if path == "/api/insights":
                as_of = qs.get("as_of", [None])[0]
                min_sup = int(qs.get("min_support", [3])[0])
                all_ins = get_all_insights(con, as_of_date=as_of, min_support=min_sup)
                return self.send_json({"status": "ok", "insights": all_ins, **all_ins})

            if path == "/api/insights/recurring":
                as_of = qs.get("as_of", [None])[0]
                min_sup = int(qs.get("min_support", [3])[0])
                patterns = detect_recurring_patterns(con, as_of_date=as_of, min_support=min_sup)
                return self.send_json({"status": "ok", "patterns": patterns})

            if path == "/api/forecast":
                days = int(qs.get("days", [30])[0])
                as_of = qs.get("as_of", [None])[0]
                forecast = cashflow_forecast(con, as_of_date=as_of, horizon_days=days)
                return self.send_json({"status": "ok", "forecast": forecast})

            if path == "/api/accounts/freshness":
                as_of = qs.get("as_of", [None])[0]
                freshness = get_accounts_freshness(con, as_of_date=as_of)
                return self.send_json({"status": "ok", "accounts": freshness})

            if path == "/api/import/batches":
                limit = int(qs.get("limit", [50])[0])
                batches = ingestion.get_import_batches(con, limit=limit)
                return self.send_json({"status": "ok", "batches": batches})

            if path == "/api/import/candidates":
                status = qs.get("status", [None])[0]
                batch_id = int(qs.get("batch_id", [0])[0]) if qs.get("batch_id") else None
                limit = int(qs.get("limit", [100])[0])
                candidates = ingestion.get_import_candidates(con, status=status, batch_id=batch_id, limit=limit)
                return self.send_json({"status": "ok", "candidates": candidates})

            if path == "/api/debts":
                kind = qs.get("kind", [None])[0]
                return self.send_json({"status": "success", "debts": list_debts(con, kind)})

            if path == "/api/debts/detail":
                did = int(qs.get("id", [0])[0])
                d = get_debt_detail(con, did)
                if not d:
                    raise ValueError(f"Posisi #{did} tidak ditemukan")
                return self.send_json({"status": "success", "debt": d})

            if path == "/api/reallocations":
                month = qs.get("month", [curr_month])[0]
                rows = [
                    rowdict(r)
                    for r in con.execute(
                        "SELECT * FROM budget_reallocations WHERE month=? ORDER BY id DESC",
                        (month,),
                    ).fetchall()
                ]
                return self.send_json({"reallocations": rows})

            if path == "/api/export_csv":
                rows = [rowdict(r) for r in con.execute("SELECT * FROM transactions WHERE is_deleted=0 ORDER BY date, time, id").fetchall()]
                fields = [
                    "canonical_id", "date", "time", "transaction_type", "amount",
                    "account_from", "account_to", "description", "category", "for_with_whom",
                    "money_context", "settlement_kind", "status", "confidence", "budget_effect",
                    "subtype", "source_refs", "notes",
                ]
                out = io.StringIO()
                writer = csv.DictWriter(out, fieldnames=fields)
                writer.writeheader()
                for r in rows:
                    writer.writerow({k: r.get(k, "") for k in fields})
                body = out.getvalue().encode("utf-8-sig")
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", "attachment; filename=money_tracks_canonical_export.csv")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "/api/download_db":
                body = DB_FILE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", "attachment; filename=money_tracks.db")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

        self.send_error(404, "Not Found")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        composer = get_composer()
        if composer is not None:
            if (
                path.startswith("/api/quick-capture/")
                or path.startswith("/api/import/")
                or path.startswith("/api/review-queue/")
                or path.startswith("/api/receipt/")
                or path == "/api/ingest/android-notification"
            ):
                content_len = int(self.headers.get("Content-Length", 0) or 0)
                body_bytes = self.rfile.read(content_len) if content_len > 0 else b""
                status, headers, body = composer.handle_request("POST", path, query=qs, body=body_bytes)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)
                return

        if path == "/api/watched-folder/scan-now":
            scanner = get_watched_folder_scanner()
            if scanner:
                res = scanner.scan_now()
                return self.send_json(res)
            return self.send_json({"success": False, "error": "Scanner not initialized"}, 500)

        if path == "/api/sync/pull-cloud":
            payload = {}
            try:
                payload = self.read_json()
            except Exception:
                pass
            worker_url = payload.get("worker_url") if isinstance(payload, dict) else None
            secret = payload.get("secret") if isinstance(payload, dict) else None
            try:
                res = sync_edge_inbox(worker_url=worker_url, secret=secret)
                return self.send_json(res, status=200)
            except Exception as e:
                return self.send_json({
                    "status": "error",
                    "message": str(e),
                    "staged_count": 0,
                    "auto_applied_count": 0,
                }, status=200)

        try:
            payload = self.read_json()
        except Exception as e:
            return self.send_json({"error": f"Invalid JSON: {e}"}, 400)

        try:
            with db_connect() as con:
                if path in {"/api/transaction", "/api/transaction/update"}:
                    tx = validate_tx(payload)
                    # Validate active accounts for money-moving transactions
                    if tx["transaction_type"] in ("Expense", "Transfer") and tx["account_from"]:
                        acc_row = con.execute("SELECT active, kind FROM accounts WHERE name=?", (tx["account_from"],)).fetchone()
                        if not acc_row:
                            raise ValueError(f"Rekening asal '{tx['account_from']}' tidak ditemukan")
                        if not acc_row["active"]:
                            raise ValueError(f"Rekening asal '{tx['account_from']}' tidak aktif")
                    if tx["transaction_type"] in ("Income", "Transfer") and tx["account_to"]:
                        acc_row = con.execute("SELECT active, kind FROM accounts WHERE name=?", (tx["account_to"],)).fetchone()
                        if not acc_row and tx["transaction_type"] == "Transfer":
                            raise ValueError(f"Rekening tujuan '{tx['account_to']}' tidak ditemukan")
                        if acc_row and not acc_row["active"]:
                            raise ValueError(f"Rekening tujuan '{tx['account_to']}' tidak aktif")
                    backup_db()
                    op_key = payload.get("operation_key") or self.headers.get("Idempotency-Key")
                    is_update = path.endswith("update") or bool(payload.get("id"))
                    scope = f"transactions.update.{payload.get('id')}" if is_update else "transactions.create"

                    def do_save_tx():
                        balance_changed = True
                        if is_update:
                            tx_id = int(payload.get("id"))
                            old_tx_row = con.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
                            if old_tx_row:
                                old_tx = rowdict(old_tx_row)
                                balance_changed = (
                                    old_tx["transaction_type"] != tx["transaction_type"]
                                    or abs(float(old_tx["amount"] or 0) - float(tx["amount"] or 0)) >= 0.005
                                    or old_tx["account_from"] != tx["account_from"]
                                    or old_tx["account_to"] != tx["account_to"]
                                )
                                # Revert old balance effect only if balance-affecting fields changed
                                if balance_changed:
                                    if old_tx["transaction_type"] == "Expense":
                                        mutate_account_balance(con, old_tx["account_from"], +float(old_tx["amount"]), tx["date"])
                                    elif old_tx["transaction_type"] == "Income":
                                        mutate_account_balance(con, old_tx["account_to"], -float(old_tx["amount"]), tx["date"])
                                    elif old_tx["transaction_type"] == "Transfer":
                                        mutate_account_balance(con, old_tx["account_from"], +float(old_tx["amount"]), tx["date"])
                                        mutate_account_balance(con, old_tx["account_to"], -float(old_tx["amount"]), tx["date"])

                                con.execute(
                                    "INSERT INTO transaction_audit_log(transaction_id, action, old_data, new_data) VALUES (?,?,?,?)",
                                    (tx_id, "UPDATE", json.dumps(old_tx, ensure_ascii=False), json.dumps(tx, ensure_ascii=False)),
                                )

                            con.execute(
                                """UPDATE transactions
                                   SET date=?, time=?, transaction_type=?, amount=?, account_from=?, account_to=?,
                                       description=?, category=?, for_with_whom=?, money_context=?, settlement_kind=?,
                                       status=?, confidence='high', budget_effect=?, exclude_from_budget=?, budget_exclusion_reason=?,
                                       budget_rule_version=?, subtype=?, source_refs=?, notes=?,
                                       manual_edited=1, updated_at=CURRENT_TIMESTAMP
                                   WHERE id=?""",
                                (
                                    tx["date"], tx["time"], tx["transaction_type"], tx["amount"],
                                    tx["account_from"], tx["account_to"], tx["description"], tx["category"],
                                    tx["for_with_whom"], tx["money_context"], tx["settlement_kind"], tx["status"],
                                    tx["budget_effect"], tx["exclude_from_budget"], tx["budget_exclusion_reason"],
                                    tx["budget_rule_version"], tx["subtype"], tx["source_refs"], tx["notes"], tx_id,
                                ),
                            )
                        else:
                            cur = con.execute(
                                """INSERT INTO transactions
                                   (canonical_id, date, time, transaction_type, amount, account_from, account_to,
                                    description, category, for_with_whom, money_context, settlement_kind, status,
                                    confidence, budget_effect, exclude_from_budget, budget_exclusion_reason, budget_rule_version,
                                    subtype, source_refs, notes, manual_edited)
                                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                                (
                                    tx["canonical_id"], tx["date"], tx["time"], tx["transaction_type"], tx["amount"],
                                    tx["account_from"], tx["account_to"], tx["description"], tx["category"],
                                    tx["for_with_whom"], tx["money_context"], tx["settlement_kind"], tx["status"],
                                    "high", tx["budget_effect"], tx["exclude_from_budget"], tx["budget_exclusion_reason"],
                                    tx["budget_rule_version"], tx["subtype"], tx["source_refs"], tx["notes"],
                                ),
                            )
                            tx_id = cur.lastrowid
                            con.execute(
                                "INSERT INTO transaction_audit_log(transaction_id, action, new_data) VALUES (?,?,?)",
                                (tx_id, "CREATE", json.dumps(tx, ensure_ascii=False)),
                            )

                        # Mutate balances for new/updated transaction only if balance changed
                        if balance_changed:
                            amt = float(tx["amount"])
                            if tx["transaction_type"] == "Expense":
                                mutate_account_balance(con, tx["account_from"], -amt, tx["date"])
                            elif tx["transaction_type"] == "Income":
                                mutate_account_balance(con, tx["account_to"], +amt, tx["date"])
                            elif tx["transaction_type"] == "Transfer":
                                mutate_account_balance(con, tx["account_from"], -amt, tx["date"])
                                mutate_account_balance(con, tx["account_to"], +amt, tx["date"])

                        for name in (tx["account_from"], tx["account_to"]):
                            if name and inferred_account_kind(name) != "External":
                                con.execute(
                                    "INSERT OR IGNORE INTO accounts(name, kind) VALUES (?, ?)",
                                    (name, inferred_account_kind(name)),
                                )
                        return {"status": "success", "id": tx_id}

                    status_code, res = handle_idempotent_request(con, scope, op_key, payload, do_save_tx)
                    return self.send_json(res, status=status_code)

                if path == "/api/reversal":
                    backup_db()
                    tx_id = int(payload.get("id"))
                    reason = str(payload.get("reason", "")).strip()
                    replacement = payload.get("replacement")
                    res = create_reversal_transaction(con, tx_id, replacement, reason)
                    return self.send_json(res)

                if path == "/api/reconcile":
                    backup_db()
                    op_k = payload.get("operation_key") or self.headers.get("Idempotency-Key")
                    a = str(payload.get("name") or payload.get("account_name", "")).strip()
                    sc, res = handle_idempotent_request(con, f"account.reconcile.{a}", op_k, payload, lambda: reconcile_account_balance(con, a, float(payload.get("actual_balance", 0)), str(payload.get("notes", "")).strip(), force_anchor=bool(payload.get("force_anchor", False))))
                    return self.send_json(res, status=sc)

                if path == "/api/transaction/delete":
                    backup_db()
                    tx_id = int(payload.get("id"))
                    old_tx_row = con.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
                    if old_tx_row:
                        old_tx = rowdict(old_tx_row)
                        if old_tx["transaction_type"] == "Expense":
                            mutate_account_balance(con, old_tx["account_from"], +float(old_tx["amount"]), old_tx["date"])
                        elif old_tx["transaction_type"] == "Income":
                            mutate_account_balance(con, old_tx["account_to"], -float(old_tx["amount"]), old_tx["date"])
                        elif old_tx["transaction_type"] == "Transfer":
                            mutate_account_balance(con, old_tx["account_from"], +float(old_tx["amount"]), old_tx["date"])
                            mutate_account_balance(con, old_tx["account_to"], -float(old_tx["amount"]), old_tx["date"])

                        con.execute(
                            "INSERT INTO transaction_audit_log(transaction_id, action, old_data) VALUES (?,?,?)",
                            (tx_id, "SOFT_DELETE", json.dumps(old_tx, ensure_ascii=False)),
                        )
                    con.execute("UPDATE transactions SET is_deleted=1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (tx_id,))
                    return self.send_json({"status": "success"})

                if path == "/api/transaction/suggest":
                    desc = str(payload.get("description", ""))[:200]
                    ttype = str(payload.get("transaction_type", "Expense"))
                    tdate = str(payload.get("date", ""))
                    res = suggest_transaction_draft(con, desc, ttype, tdate)
                    return self.send_json(res)

                if path == "/api/account/balance":
                    backup_db()
                    res = save_account_balance(con, payload)
                    return self.send_json(res)

                if path == "/api/update_settings":
                    data = save_update_source(payload)
                    return self.send_json({"status": "success", **data})

                if path == "/api/pass_through":
                    raise ValueError("Pengaturan manual uang titipan telah dinonaktifkan (deprecated). Gunakan pencatatan posisi Titipan pada menu Pihak Ketiga.")

                if path == "/api/debts":
                    p_name = str(payload.get("person_name", "")).strip()
                    kind = str(payload.get("kind", "")).strip()
                    init_amt = float(payload.get("initial_amount", 0))
                    ev_date = str(payload.get("event_date", "")).strip() or now_wib().date().isoformat()
                    notes = str(payload.get("notes", "")).strip()
                    account = str(payload.get("account", "")).strip() or None
                    is_cash = bool(payload.get("is_cash", False))
                    op_key = str(payload.get("operation_key", "")).strip() or self.headers.get("Idempotency-Key") or None
                    backup_db()
                    status_code, res = handle_idempotent_request(
                        con, "debts.create", op_key, payload,
                        lambda: create_debt_position(con, p_name, kind, init_amt, ev_date, notes, account, is_cash, op_key),
                    )
                    return self.send_json(res, status=status_code)

                if path == "/api/debts/event":
                    debt_id = int(payload.get("debt_id"))
                    ev_type = str(payload.get("event_type", "")).strip()
                    amt = float(payload.get("amount", 0))
                    ev_date = str(payload.get("event_date", "")).strip() or now_wib().date().isoformat()
                    notes = str(payload.get("notes", "")).strip()
                    account = str(payload.get("account", "")).strip() or None
                    is_cash = bool(payload.get("is_cash", False))
                    op_key = str(payload.get("operation_key", "")).strip() or self.headers.get("Idempotency-Key") or None
                    backup_db()
                    status_code, res = handle_idempotent_request(
                        con, f"debts.event.{debt_id}", op_key, payload,
                        lambda: add_debt_event(con, debt_id, ev_type, amt, ev_date, notes, account, is_cash, op_key),
                    )
                    return self.send_json(res, status=status_code)

                if path == "/api/debts/reverse":
                    event_id = int(payload.get("event_id"))
                    notes = str(payload.get("notes", "")).strip()
                    op_key = str(payload.get("operation_key", "")).strip() or None
                    backup_db()
                    res = reverse_debt_event(con, event_id, notes, op_key)
                    return self.send_json(res)

                if path == "/api/debts/link_upcoming":
                    upcoming_id = int(payload.get("upcoming_id"))
                    debt_id = int(payload.get("debt_id"))
                    backup_db()
                    res = link_upcoming_to_debt(con, upcoming_id, debt_id)
                    return self.send_json(res)

                if path == "/api/debts/unlink_upcoming":
                    upcoming_id = int(payload.get("upcoming_id"))
                    backup_db()
                    res = unlink_upcoming_from_debt(con, upcoming_id)
                    return self.send_json(res)

                if path == "/api/protected_allocation":
                    title = str(payload.get("title", "")).strip()
                    amount = max(0.0, float(payload.get("amount", 0) or 0))
                    if not title or amount <= 0:
                        raise ValueError("Title and positive amount required")
                    account = str(payload.get("account", "")).strip()
                    if not account:
                        raise ValueError("Rekening penyimpan dana wajib dipilih")
                    acc_row = con.execute("SELECT active, kind FROM accounts WHERE name=?", (account,)).fetchone()
                    if not acc_row:
                        raise ValueError(f"Rekening '{account}' tidak ditemukan")
                    if not acc_row["active"]:
                        raise ValueError(f"Rekening '{account}' tidak aktif")
                    if acc_row["kind"] != "Owned":
                        raise ValueError(f"Rekening '{account}' bukan rekening milik")
                    target_date = str(payload.get("target_date", "")).strip()
                    if target_date:
                        dt.date.fromisoformat(target_date)

                    covers_upcoming_id = payload.get("covers_upcoming_id")
                    if covers_upcoming_id is not None and str(covers_upcoming_id).strip() != "":
                        covers_upcoming_id = int(covers_upcoming_id)
                        u = con.execute("SELECT * FROM upcoming WHERE id=?", (covers_upcoming_id,)).fetchone()
                        if not u or u["status"] != "Upcoming":
                            raise ValueError("Kewajiban tidak valid atau tidak berstatus Upcoming")
                        current_cov = float(con.execute(
                            "SELECT COALESCE(SUM(amount), 0) FROM protected_allocations WHERE status='Active' AND covers_upcoming_id=?",
                            (covers_upcoming_id,),
                        ).fetchone()[0] or 0)
                        if round(current_cov + amount, 2) > (round(float(u["amount"]), 2) + 0.005):
                            raise ValueError(f"Total alokasi penutup ({current_cov + amount}) melebihi nominal kewajiban ({u['amount']})")
                    else:
                        covers_upcoming_id = None

                    backup_db()
                    op_key = payload.get("operation_key") or self.headers.get("Idempotency-Key")

                    def do_create_alloc():
                        cur = con.execute(
                            "INSERT INTO protected_allocations(title,amount,account,target_date,notes,covers_upcoming_id) VALUES (?,?,?,?,?,?)",
                            (title, amount, account, target_date, str(payload.get("notes", "")), covers_upcoming_id),
                        )
                        return {"status": "success", "id": cur.lastrowid}

                    status_code, res = handle_idempotent_request(con, "protected_allocations.create", op_key, payload, do_create_alloc)
                    return self.send_json(res, status=status_code)

                if path == "/api/protected_allocation/status":
                    aid = int(payload.get("id"))
                    status = str(payload.get("status", ""))
                    backup_db()
                    res = set_protected_allocation_status(con, aid, status)
                    return self.send_json(res)

                if path == "/api/protected_allocation/link":
                    alloc_id = int(payload.get("allocation_id"))
                    upcoming_id = int(payload.get("upcoming_id"))
                    backup_db()
                    res = link_protected_allocation(con, alloc_id, upcoming_id)
                    return self.send_json(res)

                if path == "/api/protected_allocation/unlink":
                    alloc_id = int(payload.get("allocation_id"))
                    backup_db()
                    res = unlink_protected_allocation(con, alloc_id)
                    return self.send_json(res)

                if path == "/api/goals":
                    name = str(payload.get("name", "")).strip()
                    kind = str(payload.get("kind", "Goal")).strip()
                    target_amt = float(payload.get("target_amount", 0) or 0)
                    init_fund = float(payload.get("initial_funding", 0) or 0)
                    target_date = str(payload.get("target_date", "")).strip()
                    priority = int(payload.get("priority", 1) or 1)
                    pref_acc = str(payload.get("preferred_account", "")).strip()
                    notes = str(payload.get("notes", "")).strip()
                    backup_db()
                    res = create_allocation_goal(con, name, kind, target_amt, init_fund, target_date, priority, pref_acc, notes)
                    con.commit()
                    return self.send_json(res)

                if path == "/api/goals/fund":
                    goal_id = int(payload.get("goal_id"))
                    amount = float(payload.get("amount", 0))
                    notes = str(payload.get("notes", "")).strip()
                    backup_db()
                    res = fund_allocation_goal(con, goal_id, amount, notes)
                    con.commit()
                    return self.send_json(res)

                if path == "/api/goals/release":
                    goal_id = int(payload.get("goal_id"))
                    amount = float(payload.get("amount")) if payload.get("amount") is not None else None
                    target_status = payload.get("target_status")
                    backup_db()
                    res = release_allocation_goal(con, goal_id, amount, target_status)
                    con.commit()
                    return self.send_json(res)

                if path == "/api/goals/spend":
                    goal_id = int(payload.get("goal_id"))
                    amount = float(payload.get("amount", 0))
                    acc_name = str(payload.get("account_name", "")).strip()
                    desc = str(payload.get("description", "")).strip()
                    cat = str(payload.get("category", "Other / Miscellaneous")).strip()
                    tx_date = str(payload.get("tx_date", "")).strip() or None
                    backup_db()
                    res = spend_allocation_goal(con, goal_id, amount, acc_name, desc, cat, tx_date)
                    con.commit()
                    return self.send_json(res)

                if path == "/api/upcoming/reserve":
                    upcoming_id = int(payload.get("upcoming_id"))
                    status = payload.get("status")
                    reserve_now = payload.get("reserve_now")
                    if reserve_now is not None:
                        reserve_now = int(reserve_now)
                    linked_goal_id = payload.get("linked_goal_id")
                    if linked_goal_id is not None and str(linked_goal_id).strip() != "":
                        linked_goal_id = int(linked_goal_id)
                    backup_db()
                    res = set_upcoming_status_and_reservation(con, upcoming_id, status, reserve_now, linked_goal_id)
                    con.commit()
                    return self.send_json(res)

                if path == "/api/upcoming/from-recurring":
                    backup_db()
                    op_key = payload.get("operation_key") or self.headers.get("Idempotency-Key")

                    def do_create_from_recurring():
                        return create_upcoming_from_recurring(con, payload)

                    status_code, res = handle_idempotent_request(
                        con, "upcoming.from-recurring", op_key, payload, do_create_from_recurring
                    )
                    return self.send_json(res, status=status_code)

                if path.startswith("/api/import/candidates/") and path.endswith("/approve"):
                    cand_id = int(path.split("/")[4])
                    backup_db()
                    op_key = payload.get("operation_key") or self.headers.get("Idempotency-Key")
                    notes = str(payload.get("notes", "")).strip()

                    def do_approve():
                        return ingestion.approve_import_candidate(con, cand_id, reviewer_notes=notes)

                    status_code, res = handle_idempotent_request(
                        con, f"import.candidate.approve.{cand_id}", op_key, payload, do_approve
                    )
                    con.commit()
                    return self.send_json(res, status=status_code)

                if path.startswith("/api/import/candidates/") and path.endswith("/reject"):
                    cand_id = int(path.split("/")[4])
                    backup_db()
                    notes = str(payload.get("notes", "")).strip()
                    res = ingestion.reject_import_candidate(con, cand_id, reviewer_notes=notes)
                    con.commit()
                    return self.send_json(res)

                if path == "/api/import/candidates/bulk-approve":
                    backup_db()
                    candidate_ids = payload.get("candidate_ids", [])
                    allow_low = bool(payload.get("allow_low_confidence", False))
                    allow_prob = bool(payload.get("allow_probable", False))
                    res = ingestion.bulk_approve_candidates(
                        con, candidate_ids, allow_low_confidence=allow_low, allow_probable=allow_prob
                    )
                    con.commit()
                    return self.send_json(res)

                if path == "/api/import/batch":
                    backup_db()
                    source_type = str(payload.get("source_type", "manual_import")).strip()
                    source_name = str(payload.get("source_name", "Import Data")).strip()
                    raw_items = payload.get("raw_items", [])
                    batch_res = ingestion.create_import_batch(con, source_type, source_name)
                    proc_res = ingestion.process_raw_items(con, batch_res["batch_id"], raw_items)
                    con.commit()
                    return self.send_json({**batch_res, **proc_res})

                if path == "/api/plan":
                    month = str(payload.get("month", ""))
                    if not valid_month(month):
                        raise ValueError("Invalid month")
                    backup_db()
                    seed_plan(con, month)
                    guaranteed = max(0.0, float(payload.get("guaranteed_income", 0)))
                    additional = max(0.0, float(payload.get("expected_additional_income", 0)))
                    rate = min(1.0, max(0.0, float(payload.get("savings_rate", 0.20))))
                    con.execute(
                        "UPDATE monthly_plans SET guaranteed_income=?, expected_additional_income=?, savings_rate=? WHERE month=?",
                        (guaranteed, additional, rate, month),
                    )
                    return self.send_json({"status": "success"})

                if path == "/api/budget/save":
                    month = str(payload.get("month", ""))
                    if not valid_month(month):
                        raise ValueError("Invalid month")
                    rows = payload.get("rows", [])
                    if not isinstance(rows, list):
                        raise ValueError("rows must be a list")
                    backup_db()
                    seed_plan(con, month)
                    for item in rows:
                        cat = str(item.get("category", ""))
                        if not cat or cat == "Research — Historical Only":
                            continue
                        current = max(0.0, float(item.get("current_budget", 0)))
                        active = int(bool(item.get("active", False)))
                        roll = int(bool(item.get("rollover", False)))
                        old = con.execute(
                            "SELECT original_budget, current_budget FROM budget_categories WHERE month=? AND category=?",
                            (month, cat),
                        ).fetchone()
                        if old is None:
                            con.execute(
                                "INSERT INTO budget_categories(month,category,original_budget,current_budget,suggested_budget,active,rollover) VALUES (?,?,?,?,?,?,?)",
                                (month, cat, current, current, current, active, roll),
                            )
                        else:
                            original = float(old["original_budget"])
                            old_current = float(old["current_budget"])
                            if abs(original) < 0.005 and abs(old_current) < 0.005:
                                original = current
                            elif abs(current - old_current) > 0.001:
                                diff = current - old_current
                                con.execute(
                                    "INSERT INTO budget_reallocations(month,from_category,to_category,amount,note) VALUES (?,?,?,?,?)",
                                    (
                                        month,
                                        "Unallocated plan" if diff > 0 else cat,
                                        cat if diff > 0 else "Unallocated plan",
                                        abs(diff),
                                        "Manual current-budget adjustment; original budget preserved.",
                                    ),
                                )
                            con.execute(
                                "UPDATE budget_categories SET original_budget=?, current_budget=?, active=?, rollover=? WHERE month=? AND category=?",
                                (original, current, active, roll, month, cat),
                            )
                    return self.send_json({"status": "success"})

                if path == "/api/budget/refresh_suggestions":
                    month = str(payload.get("month", ""))
                    if not valid_month(month):
                        raise ValueError("Invalid month")
                    sugg = compute_budget_suggestions(con, month)
                    backup_db()
                    seed_plan(con, month)
                    for cat, val in sugg.items():
                        con.execute(
                            "UPDATE budget_categories SET suggested_budget=? WHERE month=? AND category=?",
                            (val, month, cat),
                        )
                    return self.send_json({"status": "success", "suggestions": sugg})

                if path == "/api/budget/apply_suggestions":
                    month = str(payload.get("month", ""))
                    if not valid_month(month):
                        raise ValueError("Invalid month")
                    backup_db()
                    seed_plan(con, month)
                    for r in con.execute(
                        "SELECT category, suggested_budget, original_budget, current_budget FROM budget_categories WHERE month=?",
                        (month,),
                    ).fetchall():
                        cat = r["category"]
                        sug = float(r["suggested_budget"])
                        old_current = float(r["current_budget"])
                        original = float(r["original_budget"])
                        active = 0 if cat in OCCASIONAL_DEFAULT_OFF or sug <= 0 else 1
                        if abs(original) < 0.005:
                            original = sug
                        if abs(sug - old_current) > 0.001:
                            diff = sug - old_current
                            con.execute(
                                "INSERT INTO budget_reallocations(month,from_category,to_category,amount,note) VALUES (?,?,?,?,?)",
                                (
                                    month,
                                    "Unallocated plan" if diff > 0 else cat,
                                    cat if diff > 0 else "Unallocated plan",
                                    abs(diff),
                                    "User approved suggested budget.",
                                ),
                            )
                        con.execute(
                            "UPDATE budget_categories SET original_budget=?, current_budget=?, active=? WHERE month=? AND category=?",
                            (original, sug, active, month, cat),
                        )
                    return self.send_json({"status": "success"})

                if path == "/api/budget/reallocate":
                    month = str(payload.get("month", ""))
                    from_cat = str(payload.get("from_category", ""))
                    to_cat = str(payload.get("to_category", ""))
                    amount = max(0.0, float(payload.get("amount", 0)))
                    if not valid_month(month) or not from_cat or not to_cat or from_cat == to_cat or amount <= 0:
                        raise ValueError("Invalid reallocation")
                    src = con.execute(
                        "SELECT current_budget FROM budget_categories WHERE month=? AND category=?",
                        (month, from_cat),
                    ).fetchone()
                    dst = con.execute(
                        "SELECT current_budget FROM budget_categories WHERE month=? AND category=?",
                        (month, to_cat),
                    ).fetchone()
                    if not src or not dst:
                        raise ValueError("Budget category not found")
                    if float(src["current_budget"]) < amount:
                        raise ValueError("Source category does not have enough current budget")
                    backup_db()
                    con.execute(
                        "UPDATE budget_categories SET current_budget=current_budget-? WHERE month=? AND category=?",
                        (amount, month, from_cat),
                    )
                    con.execute(
                        "UPDATE budget_categories SET current_budget=current_budget+? WHERE month=? AND category=?",
                        (amount, month, to_cat),
                    )
                    con.execute(
                        "INSERT INTO budget_reallocations(month,from_category,to_category,amount,note) VALUES (?,?,?,?,?)",
                        (month, from_cat, to_cat, amount, str(payload.get("note", ""))),
                    )
                    return self.send_json({"status": "success"})

                if path == "/api/upcoming":
                    backup_db()
                    due = str(payload.get("due_date", "")).strip()
                    due_time = str(payload.get("due_time", "")).strip()
                    title = str(payload.get("title", "")).strip()
                    amount = max(0.0, float(payload.get("amount", 0)))
                    if due:
                        dt.date.fromisoformat(due)
                    if due_time:
                        try:
                            dt.time.fromisoformat(due_time)
                        except Exception:
                            raise ValueError("Time must use HH:MM")
                    if not title or amount <= 0:
                        raise ValueError("Title and positive amount required")
                    acc = str(payload.get("account", "")).strip()
                    if acc:
                        acc_row = con.execute("SELECT active, kind FROM accounts WHERE name=?", (acc,)).fetchone()
                        if not acc_row:
                            raise ValueError(f"Rekening '{acc}' tidak ditemukan")
                        if not acc_row["active"]:
                            raise ValueError(f"Rekening '{acc}' tidak aktif")
                        if acc_row["kind"] != "Owned":
                            raise ValueError(f"Rekening '{acc}' bukan rekening milik")
                    op_key = payload.get("operation_key") or self.headers.get("Idempotency-Key")

                    def do_create_upcoming():
                        cur = con.execute(
                            "INSERT INTO upcoming(due_date,due_time,title,amount,category,account,for_with_whom,notes) VALUES (?,?,?,?,?,?,?,?)",
                            (
                                due, due_time, title, amount,
                                str(payload.get("category", "Other / Miscellaneous")),
                                acc,
                                str(payload.get("for_with_whom", "Personal / Self")),
                                str(payload.get("notes", "")),
                            ),
                        )
                        return {"status": "success", "id": cur.lastrowid}

                    status_code, res = handle_idempotent_request(con, "upcoming.create", op_key, payload, do_create_upcoming)
                    return self.send_json(res, status=status_code)

                if path == "/api/upcoming/status":
                    backup_db()
                    uid = int(payload.get("id"))
                    status = str(payload.get("status", ""))
                    if status not in {"Upcoming", "Skipped"}:
                        raise ValueError("Invalid status")
                    if status == "Skipped":
                        con.execute("UPDATE protected_allocations SET covers_upcoming_id=NULL WHERE covers_upcoming_id=? AND status='Active'", (uid,))
                    con.execute("UPDATE upcoming SET status=? WHERE id=?", (status, uid))
                    return self.send_json({"status": "success"})

                if path == "/api/upcoming/pay":
                    uid = int(payload.get("id"))
                    account = payload.get("account")
                    pay_date = payload.get("date")
                    op_key = payload.get("operation_key") or self.headers.get("Idempotency-Key")
                    backup_db()
                    res = pay_upcoming_atomic(con, uid, account, pay_date, op_key)
                    return self.send_json(res)

                if path in ("/api/ai/settings/key", "/api/ai/config"):
                    key = str(payload.get("gemini_api_key", payload.get("key", ""))).strip()
                    save_gemini_api_key(key, con)
                    return self.send_json({"status": "success", "message": "Kunci API disimpam dengan aman di OS Credential Manager"})

                if path == "/api/ai/test":
                    key = str(payload.get("gemini_api_key", payload.get("key", ""))).strip() or None
                    return self.send_json(test_gemini_connection(key))

                if path == "/api/ai/chat/stream":
                    user_msg = str(payload.get("message", "")).strip()
                    month = str(payload.get("month", "")).strip() or None

                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()

                    from ai_router import route_ai_request_stream
                    for line in route_ai_request_stream(con, user_msg, month):
                        self.wfile.write(line.encode("utf-8"))
                        self.wfile.flush()
                    return

                if path == "/api/ai/chat":
                    user_msg = str(payload.get("message", "")).strip()
                    month = str(payload.get("month", "")).strip() or None
                    res = process_ai_chat_message(con, user_msg, month)
                    return self.send_json(res)

        except (ValueError, sqlite3.IntegrityError) as e:
            return self.send_json({"error": str(e)}, 400)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self.send_json({"error": f"Unexpected error: {e}"}, 500)

        self.send_error(404, "Not Found")

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/api/ai/settings/key", "/api/ai/config"):
            with db_connect() as con:
                delete_gemini_api_key(con)
                return self.send_json({"status": "success", "message": "Kunci API berhasil dihapus dari OS Credential Manager"})

        if path in ("/api/ai/history", "/api/ai/chat/history"):
            with db_connect() as con:
                from ai_router import clear_chat_history
                clear_chat_history(con)
                return self.send_json({"status": "success", "message": "Riwayat percakapan AI berhasil dibersihkan"})

        self.send_error(404, "Not Found")


def run_app(port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    os.chdir(BASE_DIR)
    init_db()
    server = None
    actual_port = port
    for p in range(port, port + 20):
        try:
            server = ThreadedTCPServer(("127.0.0.1", p), Handler)
            actual_port = p
            break
        except OSError:
            continue

    if not server:
        print(f"Could not find a free local port from {port} to {port + 19}.")
        sys.exit(1)

    url = f"http://127.0.0.1:{actual_port}"
    print("=" * 68)
    print(" MONEY TRACKS V12 — PERSONAL MONEY CONTROL SYSTEM")
    print(f" Source Root: {BASE_DIR.resolve()}")
    print(f" Database   : {DB_FILE.resolve()}")
    print(f" Open       : {url}")
    print(" Privacy    : localhost; update checker only uses a configured manifest URL")
    print(" Data       : canonical 2026 ledger + September plan")
    print("=" * 68)

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        sync_edge_inbox()
    except Exception:
        pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


run_server = run_app
main = run_app
RequestHandler = Handler

__all__ = [
    "main",
    "run_server",
    "run_app",
    "RequestHandler",
    "Handler",
    "get_composer",
    "set_composer",
    "get_watched_folder_scanner",
    "set_watched_folder_scanner",
    "sync_edge_inbox",
    "init_edge_sync_schema",
]


if __name__ == "__main__":
    run_app()
