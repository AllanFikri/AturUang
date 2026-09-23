"""
Tests for FIX-WORKER-PARSER-DATE-V1.

Verifies:
1. sender=bca@bca.co.id maps to BCA Main (and full canonical sender map)
2. sender=unknown maps to Unknown and status=Review
3. financial_class=Expense maps to Expense (and Income, Internal Transfer)
4. financial_class=Ignore maps to Review
5. financial_class=unknown maps to Review
6. event_kind=FAILED_ATTEMPT maps to Review (and ALLOCATION_MOVEMENT -> Transfer)
7. occurred_at is used to fill date and time
8. missing occurred_at routes to Review
9. malformed occurred_at routes to Review
10. valid row emits status=Ready
11. raw_event_id = row.id always
12. filtered_count unchanged after fix (fail-closed boundary preserved)
13. determinism across two runs
14. Production DB unchanged across tests
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
import unittest

EXPECTED_PROD_DB_HASH = "8afc95829d0fa160b3d34efd6834a98aae6231262683f82ba85f01997c736421"
PROD_DB_PATH = Path(r"C:\A User Main Storage\Documents\GitHub\AturUang\runtime\money_tracks.db")
INDEX_TS_PATH = Path(__file__).resolve().parent.parent / "cloud" / "worker" / "src" / "index.ts"


def get_prod_db_hash() -> str:
    if PROD_DB_PATH.exists() and PROD_DB_PATH.is_file():
        return hashlib.sha256(PROD_DB_PATH.read_bytes()).hexdigest().lower()
    return ""


def get_extracted_builder_code() -> str:
    """Extracts the candidate builder functions and canonical maps from cloud/worker/src/index.ts."""
    code = INDEX_TS_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"(export const CANONICAL_SENDER_MAP[\s\S]*?return \{\s*rawEventId,[\s\S]*?\};\s*\})",
        code,
    )
    if not match:
        raise RuntimeError("Could not find candidate builder block in cloud/worker/src/index.ts")
    return match.group(1).replace("export ", "")


def run_node_builder(row: dict) -> dict:
    """Executes buildCandidateFromRow in Node.js with the extracted implementation."""
    builder_code = get_extracted_builder_code()
    script = f"""
{builder_code}

const row = {json.dumps(row)};
const res = buildCandidateFromRow(row);
process.stdout.write(JSON.stringify(res));
"""
    proc = subprocess.run(
        ["node", "--experimental-strip-types", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def run_node_batch(rows: list[dict]) -> dict:
    """Executes a batch through buildCandidateFromRow and aggregates results + filtered_count."""
    builder_code = get_extracted_builder_code()
    script = f"""
{builder_code}

const rows = {json.dumps(rows)};
let filteredCount = 0;
const results = rows.map((row) => {{
  const built = buildCandidateFromRow(row);
  if (!built.isCandValid) {{
    filteredCount++;
  }}
  return built.item;
}});

const response = {{
  results,
  candidates: results.filter((r) => r.candidate !== undefined).map((r) => r.candidate),
  filtered_count: filteredCount,
}};
process.stdout.write(JSON.stringify(response));
"""
    proc = subprocess.run(
        ["node", "--experimental-strip-types", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


class TestWorkerParserDateV1(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        db_hash = get_prod_db_hash()
        if db_hash:
            assert db_hash == EXPECTED_PROD_DB_HASH, f"Production DB corrupted before test: {db_hash}"

    @classmethod
    def tearDownClass(cls) -> None:
        db_hash = get_prod_db_hash()
        if db_hash:
            assert db_hash == EXPECTED_PROD_DB_HASH, f"Production DB corrupted after test: {db_hash}"

    def test_01_sender_bca_maps_to_bca_main(self) -> None:
        """Requirement 1: sender=bca@bca.co.id maps to BCA Main, plus canonical sender map verification."""
        row = {
            "id": 2001,
            "message_id": "msg_001",
            "occurred_at": "2026-09-20 10:00:00",
            "minimal_raw_payload": json.dumps({
                "sender": "bca@bca.co.id",
                "event_kind": "MERCHANT_PAYMENT",
                "financial_class": "Expense",
                "amount": 50000,
            }),
        }
        res = run_node_builder(row)
        self.assertEqual(res["candAccount"], "BCA Main")

        # Full canonical mapping test
        expected_mappings = {
            "bca@bca.co.id": "BCA Main",
            "noreply@jago.com": "Jago Main",
            "no-reply@flip.id": "Flip",
            "noreply@byu.id": "blu",
            "noreply@cx.byu.id": "blu",
            "info@shopee.co.id": "Shopee",
            "info@mail.shopee.co.id": "Shopee",
            "googleplay-noreply@google.com": "Google Play",
            "no-reply@mailer-esb.com": "ESB",
        }
        for sender_email, expected_acc in expected_mappings.items():
            test_row = {
                "id": 2001,
                "message_id": "msg_map",
                "occurred_at": "2026-09-20 10:00:00",
                "minimal_raw_payload": json.dumps({
                    "sender": sender_email,
                    "event_kind": "MERCHANT_PAYMENT",
                    "financial_class": "Expense",
                    "amount": 50000,
                }),
            }
            out = run_node_builder(test_row)
            self.assertEqual(out["candAccount"], expected_acc, f"Mismatch for {sender_email}")

    def test_02_sender_unknown_maps_to_unknown_and_status_review(self) -> None:
        """Requirement 2: sender=unknown maps to Unknown and status=Review."""
        row = {
            "id": 2002,
            "message_id": "msg_002",
            "occurred_at": "2026-09-20 10:00:00",
            "minimal_raw_payload": json.dumps({
                "sender": "random_stranger@unknown.com",
                "event_kind": "MERCHANT_PAYMENT",
                "financial_class": "Expense",
                "amount": 50000,
            }),
        }
        res = run_node_builder(row)
        self.assertEqual(res["candAccount"], "Unknown")
        self.assertEqual(res["candStatus"], "Review")

    def test_03_financial_class_expense_maps_to_expense(self) -> None:
        """Requirement 3: financial_class=Expense maps to Expense (and Income, Internal Transfer)."""
        row_exp = {
            "id": 2003,
            "message_id": "msg_003_exp",
            "occurred_at": "2026-09-20 10:00:00",
            "minimal_raw_payload": json.dumps({
                "sender": "bca@bca.co.id",
                "event_kind": "MERCHANT_PAYMENT",
                "financial_class": "Expense",
                "amount": 50000,
            }),
        }
        res_exp = run_node_builder(row_exp)
        self.assertEqual(res_exp["candTxType"], "Expense")

        row_inc = {
            "id": 2004,
            "message_id": "msg_003_inc",
            "occurred_at": "2026-09-20 10:00:00",
            "minimal_raw_payload": json.dumps({
                "sender": "noreply@jago.com",
                "event_kind": "MERCHANT_PAYMENT",
                "financial_class": "Income",
                "amount": 100000,
            }),
        }
        res_inc = run_node_builder(row_inc)
        self.assertEqual(res_inc["candTxType"], "Income")

        row_trf = {
            "id": 2005,
            "message_id": "msg_003_trf",
            "occurred_at": "2026-09-20 10:00:00",
            "minimal_raw_payload": json.dumps({
                "sender": "no-reply@flip.id",
                "financial_class": "Internal Transfer",
                "amount": 250000,
            }),
        }
        res_trf = run_node_builder(row_trf)
        self.assertEqual(res_trf["candTxType"], "Transfer")

    def test_04_financial_class_ignore_maps_to_review(self) -> None:
        """Requirement 4: financial_class=Ignore maps to Review."""
        row = {
            "id": 2006,
            "message_id": "msg_004",
            "occurred_at": "2026-09-20 10:00:00",
            "minimal_raw_payload": json.dumps({
                "sender": "bca@bca.co.id",
                "event_kind": "MERCHANT_PAYMENT",
                "financial_class": "Ignore",
                "amount": 10000,
            }),
        }
        res = run_node_builder(row)
        self.assertEqual(res["candTxType"], "Review")
        self.assertEqual(res["candStatus"], "Review")

    def test_05_financial_class_unknown_maps_to_review(self) -> None:
        """Requirement 5: financial_class=unknown maps to Review."""
        row = {
            "id": 2007,
            "message_id": "msg_005",
            "occurred_at": "2026-09-20 10:00:00",
            "minimal_raw_payload": json.dumps({
                "sender": "bca@bca.co.id",
                "event_kind": "MERCHANT_PAYMENT",
                "financial_class": "UnmappedClass",
                "amount": 10000,
            }),
        }
        res = run_node_builder(row)
        self.assertEqual(res["candTxType"], "Unknown")
        self.assertEqual(res["candStatus"], "Review")

    def test_06_event_kind_failed_attempt_maps_to_review(self) -> None:
        """Requirement 6: event_kind=FAILED_ATTEMPT maps to Review (and ALLOCATION_MOVEMENT -> Transfer)."""
        row_fail = {
            "id": 2008,
            "message_id": "msg_006_fail",
            "occurred_at": "2026-09-20 10:00:00",
            "minimal_raw_payload": json.dumps({
                "sender": "bca@bca.co.id",
                "event_kind": "FAILED_ATTEMPT",
                "financial_class": "Expense",
                "amount": 76100,
            }),
        }
        res_fail = run_node_builder(row_fail)
        self.assertEqual(res_fail["candStatus"], "Review")
        self.assertEqual(res_fail["candTxType"], "Unknown")

        row_alloc = {
            "id": 2009,
            "message_id": "msg_006_alloc",
            "occurred_at": "2026-09-20 10:00:00",
            "minimal_raw_payload": json.dumps({
                "sender": "bca@bca.co.id",
                "event_kind": "ALLOCATION_MOVEMENT",
                "financial_class": "Internal Transfer",
                "amount": 200000,
            }),
        }
        res_alloc = run_node_builder(row_alloc)
        self.assertEqual(res_alloc["candTxType"], "Transfer")
        self.assertEqual(res_alloc["candStatus"], "Ready")

    def test_07_occurred_at_is_used_to_fill_date_and_time(self) -> None:
        """Requirement 7: occurred_at is used to fill date and time."""
        row = {
            "id": 2010,
            "message_id": "msg_007",
            "occurred_at": "2026-09-20 14:35:22",
            "minimal_raw_payload": json.dumps({
                "sender": "bca@bca.co.id",
                "event_kind": "MERCHANT_PAYMENT",
                "financial_class": "Expense",
                "amount": 183150,
            }),
        }
        res = run_node_builder(row)
        self.assertEqual(res["candDate"], "2026-09-20")
        self.assertEqual(res["candTime"], "14:35:22")

    def test_08_missing_occurred_at_routes_to_review(self) -> None:
        """Requirement 8: missing occurred_at routes to Review."""
        row = {
            "id": 2011,
            "message_id": "msg_008",
            "occurred_at": None,
            "minimal_raw_payload": json.dumps({
                "sender": "bca@bca.co.id",
                "event_kind": "MERCHANT_PAYMENT",
                "financial_class": "Expense",
                "amount": 50000,
            }),
        }
        res = run_node_builder(row)
        self.assertIsNone(res["candDate"])
        self.assertEqual(res["candStatus"], "Review")
        self.assertFalse(res["isCandValid"])
        self.assertIsNone(res.get("candidate"))

    def test_09_malformed_occurred_at_routes_to_review(self) -> None:
        """Requirement 9: malformed occurred_at routes to Review."""
        row = {
            "id": 2012,
            "message_id": "msg_009",
            "occurred_at": "not-a-valid-timestamp",
            "minimal_raw_payload": json.dumps({
                "sender": "bca@bca.co.id",
                "event_kind": "MERCHANT_PAYMENT",
                "financial_class": "Expense",
                "amount": 50000,
            }),
        }
        res = run_node_builder(row)
        self.assertIsNone(res["candDate"])
        self.assertEqual(res["candStatus"], "Review")
        self.assertFalse(res["isCandValid"])
        self.assertIsNone(res.get("candidate"))

    def test_10_valid_row_emits_status_ready(self) -> None:
        """Requirement 10: valid row emits status=Ready and full candidate payload."""
        row = {
            "id": 2013,
            "message_id": "msg_010",
            "occurred_at": "2026-09-20 18:00:29",
            "minimal_raw_payload": json.dumps({
                "sender": "bca@bca.co.id",
                "event_kind": "MERCHANT_PAYMENT",
                "financial_class": "Expense",
                "amount": 27000,
                "confidence": 0.95,
            }),
        }
        res = run_node_builder(row)
        self.assertEqual(res["candStatus"], "Ready")
        self.assertTrue(res["isCandValid"])
        cand = res["candidate"]
        self.assertIsNotNone(cand)
        self.assertEqual(cand["raw_event_id"], 2013)
        self.assertEqual(cand["date"], "2026-09-20")
        self.assertEqual(cand["time"], "18:00:29")
        self.assertEqual(cand["amount"], 27000)
        self.assertEqual(cand["account"], "BCA Main")
        self.assertIsNone(cand["to_account"])
        self.assertEqual(cand["tx_type"], "Expense")
        self.assertEqual(cand["status"], "Ready")
        self.assertEqual(cand["match_tier"], "EXACT")
        self.assertEqual(cand["reconciliation_status"], "RECONCILED")

    def test_11_raw_event_id_equals_row_id_always(self) -> None:
        """Requirement 11: raw_event_id = row.id always."""
        # Row with null raw_event_id
        row1 = {
            "id": 2256,
            "message_id": "msg_2256",
            "occurred_at": "2026-09-20 10:15:30",
            "raw_event_id": None,
            "minimal_raw_payload": json.dumps({
                "sender": "bca@bca.co.id",
                "event_kind": "MERCHANT_PAYMENT",
                "financial_class": "Expense",
                "amount": 200000,
            }),
        }
        res1 = run_node_builder(row1)
        self.assertEqual(res1["rawEventId"], 2256)
        self.assertEqual(res1["candidate"]["raw_event_id"], 2256)

        # Row where raw_event_id differs from row.id: row.id takes precedence
        row2 = {
            "id": 3000,
            "message_id": "msg_3000",
            "occurred_at": "2026-09-20 10:15:30",
            "raw_event_id": 9999,
            "minimal_raw_payload": json.dumps({
                "sender": "bca@bca.co.id",
                "event_kind": "MERCHANT_PAYMENT",
                "financial_class": "Expense",
                "amount": 200000,
            }),
        }
        res2 = run_node_builder(row2)
        self.assertEqual(res2["rawEventId"], 3000)
        self.assertEqual(res2["candidate"]["raw_event_id"], 3000)

    def test_12_filtered_count_unchanged_after_fix(self) -> None:
        """Requirement 12: filtered_count unchanged after fix (fail-closed boundary preserved)."""
        rows = [
            # 1. Valid row: should NOT be filtered
            {
                "id": 4001,
                "message_id": "msg_4001",
                "occurred_at": "2026-09-20 10:00:00",
                "minimal_raw_payload": json.dumps({
                    "sender": "bca@bca.co.id",
                    "event_kind": "MERCHANT_PAYMENT",
                    "financial_class": "Expense",
                    "amount": 50000,
                }),
            },
            # 2. Invalid row: amount = 0 -> filtered
            {
                "id": 4002,
                "message_id": "msg_4002",
                "occurred_at": "2026-09-20 10:00:00",
                "minimal_raw_payload": json.dumps({
                    "sender": "bca@bca.co.id",
                    "event_kind": "MERCHANT_PAYMENT",
                    "financial_class": "Expense",
                    "amount": 0,
                }),
            },
            # 3. Invalid row: fractional amount -> filtered
            {
                "id": 4003,
                "message_id": "msg_4003",
                "occurred_at": "2026-09-20 10:00:00",
                "minimal_raw_payload": json.dumps({
                    "sender": "bca@bca.co.id",
                    "event_kind": "MERCHANT_PAYMENT",
                    "financial_class": "Expense",
                    "amount": 1.42,
                }),
            },
            # 4. Invalid row: missing occurred_at -> filtered
            {
                "id": 4004,
                "message_id": "msg_4004",
                "occurred_at": None,
                "minimal_raw_payload": json.dumps({
                    "sender": "bca@bca.co.id",
                    "event_kind": "MERCHANT_PAYMENT",
                    "financial_class": "Expense",
                    "amount": 50000,
                }),
            },
            # 5. Invalid row: financial_class=Ignore -> tx_type=Review -> filtered
            {
                "id": 4005,
                "message_id": "msg_4005",
                "occurred_at": "2026-09-20 10:00:00",
                "minimal_raw_payload": json.dumps({
                    "sender": "bca@bca.co.id",
                    "event_kind": "MERCHANT_PAYMENT",
                    "financial_class": "Ignore",
                    "amount": 50000,
                }),
            },
        ]
        out = run_node_batch(rows)
        self.assertEqual(out["filtered_count"], 4)
        self.assertEqual(len(out["candidates"]), 1)
        self.assertEqual(out["candidates"][0]["raw_event_id"], 4001)

    def test_13_determinism_across_two_runs(self) -> None:
        """Requirement 13: determinism across two runs."""
        rows = [
            {
                "id": 5001,
                "message_id": "msg_5001",
                "occurred_at": "2026-09-20 10:00:00",
                "minimal_raw_payload": json.dumps({
                    "sender": "bca@bca.co.id",
                    "event_kind": "ALLOCATION_MOVEMENT",
                    "financial_class": "Internal Transfer",
                    "amount": 200000,
                }),
            },
            {
                "id": 5002,
                "message_id": "msg_5002",
                "occurred_at": "2026-09-20 12:00:00",
                "minimal_raw_payload": json.dumps({
                    "sender": "info@shopee.co.id",
                    "event_kind": "MERCHANT_PAYMENT",
                    "financial_class": "Expense",
                    "amount": 75000,
                }),
            },
        ]
        run1 = run_node_batch(rows)
        run2 = run_node_batch(rows)
        self.assertEqual(json.dumps(run1, sort_keys=True), json.dumps(run2, sort_keys=True))

    def test_14_production_db_unchanged(self) -> None:
        """Requirement 14: Production DB unchanged across tests."""
        current_hash = get_prod_db_hash()
        self.assertEqual(current_hash, EXPECTED_PROD_DB_HASH)


if __name__ == "__main__":
    unittest.main()
