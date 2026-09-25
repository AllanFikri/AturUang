#!/usr/bin/env python3
r"""
One-off local tool to re-parse legacy Flip messages in the activation database
using the hardened Flip parser from cloud/worker/src/domain.ts (post STEP-17A).

Safety rules:
- Dry-run is the default.
- --apply is required to mutate activation DB.
- Strict prod DB SHA-256 verification before and after.
- Refuses to run if any path points to prod runtime.
- Mandatory backup before apply.
- Idempotency guard: refuses to apply if entries are already reparsed.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from typing import Any

DEFAULT_EXPECTED_PROD_HASH = "341c5f348ac3cd82732e1067f54074ea372cf9f2e9e87d491cc9f76cebe94c07"
REPARSED_MARKER = "step-17b-flip-reparse-v1"
MAX_SCOPE_LIMIT = 200

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOMAIN_TS_PATH = REPO_ROOT / "cloud" / "worker" / "src" / "domain.ts"


def compute_sha256(path: Path) -> str:
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest().lower()


def is_inside_prod_runtime(path: Path) -> bool:
    norm = str(path.resolve()).replace("\\", "/").lower()
    # AturUang/runtime/ is prod, AturUang-activation... is activation
    if "/aturuang/runtime/" in norm or norm.endswith("/aturuang/runtime"):
        if "/aturuang-activation" not in norm:
            return True
    return False


def build_reparsed_payload(
    ev: dict[str, Any] | None,
    item: dict[str, Any],
    msg_id: str,
    old_payload: dict[str, Any],
    is_pending: bool = False,
) -> dict[str, Any]:
    cand = ev.get("candidate") if ev else None
    if isinstance(cand, dict):
        payload = dict(cand)
    elif ev:
        payload = {
            "amount": ev.get("amount", 0),
            "tx_type": "Expense",
            "category": "Other / Miscellaneous",
        }
    else:
        payload = {}

    payload["message_id"] = msg_id
    payload["sender"] = item.get("from") or "no-reply@flip.id"
    payload["subject"] = item.get("subject") or ""
    payload["occurred_at"] = ev.get("occurred_at_wib") if ev else None

    for k in ("raw_event_id", "cursor", "cursor_id"):
        if k in old_payload and old_payload[k] is not None:
            payload[k] = old_payload[k]

    payload["reparsed_by"] = REPARSED_MARKER

    if is_pending:
        payload["status"] = "Review"

    return payload


def run_node_batch_parse(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not DOMAIN_TS_PATH.exists():
        raise FileNotFoundError(f"domain.ts not found at: {DOMAIN_TS_PATH}")

    domain_file_uri = DOMAIN_TS_PATH.as_uri()
    messages_json = json.dumps(messages)
    script = f"""
import('{domain_file_uri}').then(async (m) => {{
  const input = {messages_json};
  const results = [];

  for (const item of input) {{
    try {{
      const subject = item.subject || '';
      const body = item.body || '';
      const from = item.from || 'no-reply@flip.id';
      const occurredAt = item.internal_date || new Date().toISOString();
      const ev = m.parseGmailIntelligence(subject, body, from, occurredAt);
      results.push({{
        edge_id: item.edge_id,
        message_id: item.message_id,
        from,
        subject,
        success: true,
        event: ev,
        error: null,
      }});
    }} catch (err) {{
      results.push({{
        edge_id: item.edge_id,
        message_id: item.message_id,
        from: item.from || '',
        subject: item.subject || '',
        success: false,
        event: null,
        error: err.message || String(err),
      }});
    }}
  }}

  process.stdout.write(JSON.stringify(results));
}}).catch((err) => {{
  console.error(err);
  process.exit(1);
}});
"""
    proc = subprocess.run(
        ["node", "--experimental-strip-types"],
        input=script,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Node execution failed: {proc.stderr}")

    return json.loads(proc.stdout)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reparse legacy Flip messages in activation DB using hardened domain parser."
    )
    parser.add_argument("--input", required=True, help="Path to input JSON file from export_helper.gs")
    parser.add_argument("--activation-db", required=True, help="Path to activation SQLite DB")
    parser.add_argument("--prod-db", required=True, help="Path to production SQLite DB for safety check")
    parser.add_argument(
        "--expected-prod-hash",
        default=DEFAULT_EXPECTED_PROD_HASH,
        help="Expected SHA256 of production DB",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", default=None, help="Simulate reparse without modifying DB (default)")
    group.add_argument("--apply", action="store_true", default=None, help="Mutate activation DB with new entries")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    is_apply = bool(args.apply)

    input_path = Path(args.input).resolve()
    activation_db_path = Path(args.activation_db).resolve()
    prod_db_path = Path(args.prod_db).resolve()
    expected_prod_hash = args.expected_prod_hash.strip().lower()

    # -------------------------------------------------------------------------
    # SAFETY GUARD: Path verification
    # -------------------------------------------------------------------------
    if activation_db_path == prod_db_path:
        sys.stderr.write("SAFETY_ERROR: Activation DB resolves to Production DB path. Refusing to run.\n")
        return 1

    if is_inside_prod_runtime(activation_db_path):
        sys.stderr.write(f"SAFETY_ERROR: Activation DB path resolves inside production runtime: {activation_db_path}\n")
        return 1

    if is_inside_prod_runtime(input_path):
        sys.stderr.write(f"SAFETY_ERROR: Input path resolves inside production runtime: {input_path}\n")
        return 1

    # -------------------------------------------------------------------------
    # SAFETY GUARD: Production DB Hash (Start)
    # -------------------------------------------------------------------------
    try:
        prod_hash_before = compute_sha256(prod_db_path)
    except Exception as e:
        sys.stderr.write(f"PROD_DB_ERROR: Failed to read production DB: {e}\n")
        return 1

    if prod_hash_before != expected_prod_hash:
        sys.stderr.write(
            f"PROD_DB_HASH_MISMATCH: Expected {expected_prod_hash}, got {prod_hash_before}. Refusing to run.\n"
        )
        return 1

    # -------------------------------------------------------------------------
    # SAFETY GUARD: Activation DB Check
    # -------------------------------------------------------------------------
    if not activation_db_path.exists() or not activation_db_path.is_file():
        sys.stderr.write(f"ACTIVATION_DB_ERROR: Activation DB file not found: {activation_db_path}\n")
        return 1

    activation_hash_before = compute_sha256(activation_db_path)

    # -------------------------------------------------------------------------
    # Load Input JSON
    # -------------------------------------------------------------------------
    if not input_path.exists() or not input_path.is_file():
        sys.stderr.write(f"INPUT_ERROR: Input JSON file not found: {input_path}\n")
        return 1

    try:
        raw_data = json.loads(input_path.read_text(encoding="utf-8"))
    except Exception as e:
        sys.stderr.write(f"INPUT_ERROR: Failed to parse input JSON: {e}\n")
        return 1

    messages: list[dict[str, Any]] = raw_data.get("messages", [])
    if len(messages) > MAX_SCOPE_LIMIT:
        sys.stderr.write(
            f"SCOPE_LIMIT_EXCEEDED: Input JSON has {len(messages)} messages (max allowed: {MAX_SCOPE_LIMIT}).\n"
        )
        return 1

    if not messages:
        sys.stdout.write("No messages found in input JSON. Nothing to do.\n")
        return 0

    edge_ids = [m["edge_id"] for m in messages if "edge_id" in m]
    if len(edge_ids) > MAX_SCOPE_LIMIT:
        sys.stderr.write(
            f"SCOPE_LIMIT_EXCEEDED: Scope has {len(edge_ids)} edge_ids (max allowed: {MAX_SCOPE_LIMIT}).\n"
        )
        return 1

    # -------------------------------------------------------------------------
    # Load Activation DB existing records & check idempotency
    # -------------------------------------------------------------------------
    con = sqlite3.connect(str(activation_db_path))
    con.row_factory = sqlite3.Row
    try:
        existing_rows: dict[int, sqlite3.Row] = {}
        if edge_ids:
            pragma_cursor = con.execute("PRAGMA table_info(edge_synced_messages)")
            cols = {r[1] for r in pragma_cursor.fetchall()}
            synced_col = ", synced_at" if "synced_at" in cols else ""
            placeholders = ",".join("?" for _ in edge_ids)
            cursor = con.execute(
                f"SELECT id, message_id, source, sender, subject, payload, status{synced_col} FROM edge_synced_messages WHERE id IN ({placeholders})",
                edge_ids,
            )
            for row in cursor.fetchall():
                existing_rows[row["id"]] = row
    finally:
        con.close()

    # Idempotency check: refuse if any row in scope already marked as reparsed
    for eid, row in existing_rows.items():
        payload_raw = row["payload"]
        try:
            pdict = json.loads(payload_raw) if payload_raw else {}
            if pdict.get("reparsed_by") or pdict.get("reparsed") is True:
                sys.stderr.write(
                    f"IDEMPOTENCY_GUARD: edge_id {eid} (message_id {row['message_id']}) was already reparsed by '{pdict.get('reparsed_by')}'. Aborting.\n"
                )
                return 1
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Phase 2: Run Node Parser Batch
    # -------------------------------------------------------------------------
    try:
        parsed_results = run_node_batch_parse(messages)
    except Exception as e:
        sys.stderr.write(f"PARSER_INVOCATION_ERROR: {e}\n")
        return 1

    # -------------------------------------------------------------------------
    # Phase 3: Action Classification & Summary Table
    # -------------------------------------------------------------------------
    classified_items: list[dict[str, Any]] = []
    replace_count = 0
    drop_count = 0
    keep_pending_count = 0
    parse_error_count = 0

    print("=" * 105)
    print(f"{'EDGE_ID':<8} {'MESSAGE_ID':<18} {'OLD_AMT':<12} {'NEW_AMT':<12} {'OLD_KIND':<20} {'NEW_KIND':<20} {'ACTION':<12}")
    print("=" * 105)

    for item in parsed_results:
        eid = item.get("edge_id")
        msg_id = item.get("message_id", "")
        old_row = existing_rows.get(eid)

        old_amount: Any = "-"
        old_kind: Any = "-"
        if old_row and old_row["payload"]:
            try:
                op = json.loads(old_row["payload"])
                old_amount = op.get("amount", "-")
                old_kind = op.get("event_kind", "-")
            except Exception:
                pass

        if not item.get("success"):
            action = "PARSE_ERROR"
            new_amount: Any = "ERR"
            new_kind: Any = "ERR"
            parse_error_count += 1
            print(f"WARN: Parse error for message_id {msg_id}: {item.get('error')}", file=sys.stderr)
        else:
            ev = item["event"]
            new_amount = ev.get("amount", 0)
            new_kind = ev.get("event_kind", "")
            cand = ev.get("candidate")

            if new_kind in ("NON_TRANSACTION", "FAILED_ATTEMPT"):
                action = "DROP"
                drop_count += 1
            elif cand is not None:
                action = "REPLACE"
                replace_count += 1
            else:
                action = "KEEP_PENDING"
                keep_pending_count += 1

        classified_items.append({
            "edge_id": eid,
            "message_id": msg_id,
            "from": item.get("from"),
            "subject": item.get("subject"),
            "action": action,
            "event": item.get("event"),
            "old_amount": old_amount,
            "new_amount": new_amount,
            "old_kind": old_kind,
            "new_kind": new_kind,
        })

        print(f"{str(eid):<8} {str(msg_id):<18} {str(old_amount):<12} {str(new_amount):<12} {str(old_kind):<20} {str(new_kind):<20} {action:<12}")

    print("=" * 105)
    print(f"TOTALS: REPLACE={replace_count}, DROP={drop_count}, KEEP_PENDING={keep_pending_count}, PARSE_ERROR={parse_error_count}, TOTAL={len(classified_items)}")
    print(f"PROD_DB_SHA256_CHECK: {prod_hash_before} (MATCH)")

    # -------------------------------------------------------------------------
    # Dry-Run Exit
    # -------------------------------------------------------------------------
    if not is_apply:
        print("MODE: DRY_RUN (Default). No database modifications were performed.")
        # Verify prod DB hash at end
        prod_hash_after = compute_sha256(prod_db_path)
        if prod_hash_after != expected_prod_hash:
            sys.stderr.write("FATAL: Prod DB hash altered during dry-run!\n")
            return 1
        return 0

    # -------------------------------------------------------------------------
    # Phase 4: Apply Mutation (Guarded)
    # -------------------------------------------------------------------------
    print("\nMODE: APPLY REQUESTED. Beginning backup and database modification...")

    # Backup creation
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = activation_db_path.parent / f"money_tracks.db.bak-{timestamp}"
    try:
        shutil.copy2(activation_db_path, backup_path)
        if not backup_path.exists() or backup_path.stat().st_size == 0:
            raise RuntimeError("Backup file created is empty or missing.")
        print(f"BACKUP_CREATED: {backup_path} (size: {backup_path.stat().st_size} bytes)")
    except Exception as e:
        sys.stderr.write(f"BACKUP_ERROR: Failed to create backup: {e}. Aborting apply.\n")
        return 1

    con = sqlite3.connect(str(activation_db_path))
    try:
        with con:
            for item in classified_items:
                action = item["action"]
                eid = item["edge_id"]
                msg_id = item["message_id"]
                ev = item.get("event")
                old_row = existing_rows.get(eid)

                old_payload: dict[str, Any] = {}
                if old_row and old_row["payload"]:
                    try:
                        parsed_op = json.loads(old_row["payload"])
                        if isinstance(parsed_op, dict):
                            old_payload = parsed_op
                    except Exception:
                        old_payload = {}

                if action == "DROP":
                    con.execute("DELETE FROM edge_synced_messages WHERE id = ?", (eid,))
                elif action == "REPLACE":
                    payload_dict = build_reparsed_payload(
                        ev=ev,
                        item=item,
                        msg_id=msg_id,
                        old_payload=old_payload,
                        is_pending=False,
                    )
                    payload_str = json.dumps(payload_dict, ensure_ascii=False)
                    content_hash = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()

                    old_synced_at: str | None = None
                    if old_row:
                        try:
                            val = old_row["synced_at"]
                            if val is not None and str(val).strip():
                                old_synced_at = str(val).strip()
                        except (IndexError, KeyError):
                            pass

                    if not old_synced_at:
                        old_synced_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

                    # DELETE old row, INSERT new row with same message_id
                    con.execute("DELETE FROM edge_synced_messages WHERE id = ?", (eid,))
                    con.execute(
                        "INSERT INTO edge_synced_messages (message_id, source, sender, subject, payload, status, synced_at, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            msg_id,
                            "gmail",
                            item.get("from") or "no-reply@flip.id",
                            item.get("subject") or "",
                            payload_str,
                            "REVIEW_REQUIRED",
                            old_synced_at,
                            content_hash,
                        ),
                    )
                elif action == "KEEP_PENDING":
                    payload_dict = build_reparsed_payload(
                        ev=ev,
                        item=item,
                        msg_id=msg_id,
                        old_payload=old_payload,
                        is_pending=True,
                    )
                    payload_str = json.dumps(payload_dict, ensure_ascii=False)
                    content_hash = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()
                    con.execute(
                        "UPDATE edge_synced_messages SET payload = ?, status = 'REVIEW_REQUIRED', content_hash = ? WHERE id = ?",
                        (payload_str, content_hash, eid),
                    )
    except Exception as e:
        sys.stderr.write(f"APPLY_ERROR: Transaction failed: {e}\n")
        return 1
    finally:
        con.close()

    activation_hash_after = compute_sha256(activation_db_path)
    prod_hash_after = compute_sha256(prod_db_path)

    print("=" * 105)
    print("APPLY_SUCCESSFUL")
    print(f"BACKUP_PATH: {backup_path}")
    print(f"ACTIVATION_DB_BEFORE: {activation_hash_before}")
    print(f"ACTIVATION_DB_AFTER:  {activation_hash_after}")
    print(f"PROD_DB_BEFORE:        {prod_hash_before}")
    print(f"PROD_DB_AFTER:         {prod_hash_after}")
    print(f"SUMMARY: Replaced={replace_count}, Dropped={drop_count}, KeptPending={keep_pending_count}")
    print("=" * 105)

    if prod_hash_after != expected_prod_hash:
        sys.stderr.write("FATAL: Production DB altered during apply!\n")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
