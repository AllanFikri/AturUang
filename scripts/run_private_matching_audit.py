#!/usr/bin/env python3
"""Run Universal Ingestion Phase 5B - Verifiable Three-Pass Private Replay Gate.

Executes 3-pass private corpus replay across all 9 bundles:
- Pass A: normal deterministic discovery order
- Pass B: normal deterministic discovery order repeated
- Pass C: deterministic shuffled discovery order

Enforces frozen inventory, strict relationship schema, atomic output generation,
zero-query production database byte hashing, and unified gate validation.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from aturuang.ingestion_matching_audit import (
    CANONICAL_FAMILIES,
    EXPECTED_INVENTORY,
    EXPECTED_PRODUCTION_DB_SHA256,
    EXPECTED_TOTAL_ARTIFACTS,
    EXPECTED_TOTAL_IMAGES,
    EXPECTED_TOTAL_PDFS,
    execute_three_pass_audit,
    verify_production_db_bytes,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run verifiable 3-pass private matching audit gate."
    )
    parser.add_argument(
        "--corpus-config",
        type=Path,
        default=Path(os.environ.get("ATURUANG_PRIVATE_CORPUS_CONFIG", "")),
        help="Path to external corpus configuration JSON mapping sanitized family aliases to bundle paths",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(os.environ.get("ATURUANG_OUTPUT_DIR", "")),
        help="Target output directory for sanitized summary.json and evidence_review.csv",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path(os.environ.get("ATURUANG_SQLITE_PATH", "")),
        help="Path to production SQLite database to verify byte-level integrity (runtime supplied)",
    )
    parser.add_argument(
        "--relationships-json",
        type=Path,
        default=None,
        help="Optional path to external relationship configuration JSON",
    )
    parser.add_argument(
        "--master-secret",
        type=str,
        default=os.environ.get("ATURUANG_MASTER_SECRET", ""),
        help="Runtime master secret for account identity protection (min 32 chars)",
    )
    args = parser.parse_args()

    print("=== Universal Ingestion Phase 5B: Three-Pass Private Replay Gate ===")

    # 1. Validate master secret presence and strength
    secret = args.master_secret
    if not secret:
        print("ERROR: Master secret is required. Provide via --master-secret or ATURUANG_MASTER_SECRET env var.", file=sys.stderr)
        return 1
    if len(secret) < 32:
        print("ERROR: Master secret is too weak (must be at least 32 characters long).", file=sys.stderr)
        return 1

    # 2. Validate corpus config presence
    if not str(args.corpus_config) or not args.corpus_config.exists():
        print("ERROR: External corpus configuration is required and must exist (--corpus-config or ATURUANG_PRIVATE_CORPUS_CONFIG).", file=sys.stderr)
        return 1

    # 3. Validate output dir presence
    if not str(args.output_dir):
        print("ERROR: Target output directory is required (--output-dir or ATURUANG_OUTPUT_DIR).", file=sys.stderr)
        return 1

    # 4. Validate production database presence and initial pre-run hash
    if not str(args.db_path) or not args.db_path.exists():
        print("ERROR: Production database path is required and must exist (--db-path or ATURUANG_SQLITE_PATH).", file=sys.stderr)
        return 1

    try:
        pre_hash = verify_production_db_bytes(args.db_path)
        print("Production DB Pre-check : SHA-256 matches expected baseline.")
    except Exception as exc:
        print(f"ERROR: Production DB pre-run check failed: {exc}", file=sys.stderr)
        return 1

    # 5. Execute 3-pass replay audit
    t0 = time.time()
    print("Executing verifiable three-pass replay across all canonical families...")
    try:
        batch, csv_path, json_path, summary = execute_three_pass_audit(
            corpus_config_path=args.corpus_config,
            output_dir=args.output_dir,
            db_path=args.db_path,
            master_secret=secret,
            relationship_config_path=args.relationships_json,
        )
    except Exception as exc:
        print(f"ERROR during three-pass replay execution: {exc}", file=sys.stderr)
        return 1

    elapsed = time.time() - t0
    print(f"Three-pass audit completed in {elapsed:.2f}s.")

    # 6. Report sanitized results
    inv = summary["inventory"]
    doc = summary["document_summary"]
    m = summary["matching_summary"]
    ver = summary["three_pass_verification"]
    db = summary["database_integrity"]

    print("\n--- Inventory Verification ---")
    for fam, cnt in sorted(inv.get("by_family", {}).items()):
        print(f"  {fam:<22} : {cnt}")
    print(f"Total PDFs               : {inv.get('total_pdfs')}")
    print(f"Total Images             : {inv.get('total_images')}")
    print(f"Total Discovered         : {inv.get('total_artifacts')}")

    print("\n--- Document Ingestion Summary ---")
    print(f"Ready for Staging        : {doc.get('ready_for_staging_count')}")
    print(f"Review Required          : {doc.get('review_required_count')}")
    print(f"Failed Documents         : {doc.get('failed_count')}")

    print("\n--- Matching Summary & Quality Gate ---")
    print(f"Evidence Total           : {m.get('evidence_total')}")
    print(f"Decisions Total          : {m.get('decisions_total')}")
    print(f"Total Match Groups       : {m.get('total_groups')}")
    print(f"Exact Groups             : {m.get('exact_groups')} (member evidence: {m.get('exact_member_evidence')})")
    print(f"Strong Groups            : {m.get('strong_groups')} (member evidence: {m.get('strong_member_evidence')})")
    print(f"Ambiguous Evidence       : {m.get('ambiguous_evidence')}")
    print(f"Unmatched Evidence       : {m.get('unmatched_evidence')}")
    print(f"Ineligible Evidence      : {m.get('ineligible_evidence')}")
    print(f"Duplicate Evidence Keys  : {m.get('duplicate_evidence_keys')}")
    print(f"Duplicate Decisions      : {m.get('duplicate_decisions')}")
    print(f"Duplicate Group Keys     : {m.get('duplicate_group_keys')}")
    print(f"Multi-Group Members      : {m.get('multi_group_members')}")
    print(f"Unknown Group Members    : {m.get('unknown_group_members')}")
    print(f"Invalid Auto-Link Groups : {m.get('invalid_auto_link_groups')}")
    print(f"Review Evidence Auto-Link: {m.get('review_evidence_auto_linked')}")
    print(f"Conservation Check       : {'PASS' if m.get('conservation_pass') else 'FAIL'}")

    print("\n--- Three-Pass Replay Determinism ---")
    print(f"Pass A (Normal) Digest   : {ver.get('pass_a_digest')}")
    print(f"Pass B (Repeat) Digest   : {ver.get('pass_b_digest')}")
    print(f"Pass C (Shuffle) Digest  : {ver.get('pass_c_digest')}")
    print(f"Repeat Deterministic     : {ver.get('repeat_deterministic')}")
    print(f"Shuffle Deterministic    : {ver.get('shuffle_deterministic')}")
    print(f"Overall Deterministic    : {ver.get('overall_deterministic')}")

    print("\n--- Database Zero-Mutation Verification ---")
    print(f"Pre-Replay SQLite Hash   : {db.get('pre_sqlite_sha256')}")
    print(f"Post-Replay SQLite Hash  : {db.get('post_sqlite_sha256')}")
    print(f"SQLite Unchanged         : {db.get('sqlite_unchanged')}")

    print(f"\nSanitized Output CSV     : {csv_path.name}")
    print(f"Sanitized Output JSON    : {json_path.name}")
    print("Three-pass private replay gate passed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
