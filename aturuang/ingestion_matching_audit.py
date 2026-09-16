"""Universal Ingestion Phase 5 - Evidence Matching Audit & Private Replay Gate.

Provides deterministic, offline, read-only evidence matching audit capabilities:
- Zero committed private filenames or paths (alias-based configuration)
- Mandatory runtime master secret (no hard-coded defaults)
- Frozen inventory validation before parsing (228 PDFs, 13 images, 241 total)
- Three-pass verifiable replay (Pass A, Pass B repeated, Pass C deterministically shuffled)
- Atomic output writing for summary.json and evidence_review.csv
- Complete spreadsheet formula injection protection
- Production SQLite zero-mutation and zero-query verification via byte hashing
- Unified fail-closed gate validator
"""

from __future__ import annotations

import collections
import csv
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import sqlite3
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence

from aturuang.ingestion_contracts import (
    AccountType,
    ConfidenceLevel,
    OwnershipState,
    SourceChannel,
    TemplateMatchStatus,
)
from aturuang.ingestion_adapter import (
    DiagnosticSeverity,
    EventDirection,
    EventRole,
    NormalizedEventEnvelope,
    SourceEventStatus,
)
from aturuang.ingestion_discovery import (
    DiscoveredArtifact,
    DiscoveryResult,
    discover_files,
    read_discovered_artifact,
)
from aturuang.ingestion_orchestration import (
    DryRunBatchResult,
    DryRunDisposition,
    DryRunDocumentResult,
    ReadOnlyRegistryAuthority,
    SourceDocumentReadRecord,
    TemplateResolution,
    _validate_evidence_match_plan,
    build_default_adapter_catalog,
    dry_run_batch,
)
from aturuang.ingestion_account_discovery import AccountDiscoveryResolver
from aturuang.ingestion_identity_privacy import (
    AccountIdentityProtector,
    is_valid_protected_key,
    validate_protected_key,
)
from aturuang.ingestion_matching import (
    AccountRelationship,
    DeterministicEvidenceMatcher,
    EconomicEventGroup,
    EvidenceMatchDecision,
    EvidenceMatchPlan,
    MatchingContext,
    MatchReasonCode,
    MatchRelation,
    MatchTier,
    ProtectedAccountBinding,
    RelationshipKind,
    SafeEvidenceRecord,
    compute_evidence_key,
    make_evidence_record_from_envelope,
)

EXPECTED_PRODUCTION_DB_SHA256: str = (
    "8afc95829d0fa160b3d34efd6834a98aae6231262683f82ba85f01997c736421"
)

CANONICAL_FAMILIES: tuple[str, ...] = (
    "bca_statement",
    "jago_statement",
    "seabank_statement",
    "blu_mutation",
    "blu_portfolio",
    "gopay_statement",
    "stockbit_soa",
    "shopee_orders",
    "shopeepay_mutation",
)

EXPECTED_INVENTORY: dict[str, int] = {
    "bca_statement": 19,
    "jago_statement": 11,
    "seabank_statement": 19,
    "blu_mutation": 25,
    "blu_portfolio": 25,
    "gopay_statement": 7,
    "stockbit_soa": 10,
    "shopee_orders": 112,
    "shopeepay_mutation": 13,
}

EXPECTED_TOTAL_PDFS: int = 228
EXPECTED_TOTAL_IMAGES: int = 13
EXPECTED_TOTAL_ARTIFACTS: int = 241

CSV_FIELDNAMES: list[str] = [
    "statement_month",
    "source_registry_id",
    "event_role",
    "evidence_key",
    "match_tier",
    "match_relation",
    "group_key",
    "auto_link_eligible",
    "reason_codes",
    "review_flag",
]

VALID_RELATIONSHIP_KINDS: set[str] = {
    RelationshipKind.COMMERCE_PAYMENT.value,
    RelationshipKind.BROKER_SETTLEMENT.value,
    RelationshipKind.INVESTMENT_SETTLEMENT.value,
}


class InMemoryRegistryAuthority:
    """In-memory implementation of ReadOnlyRegistryAuthority for offline audits."""

    CAPABILITIES: dict[str, dict[str, object]] = {
        "bca_statement": {"source_registry_id": "bca_statement", "channel": "PDF"},
        "jago_statement": {"source_registry_id": "jago_statement", "channel": "PDF"},
        "seabank_statement": {"source_registry_id": "seabank_statement", "channel": "PDF"},
        "blu_mutation": {"source_registry_id": "blu_mutation", "channel": "PDF"},
        "blu_portfolio": {"source_registry_id": "blu_portfolio", "channel": "PDF"},
        "gopay_statement": {"source_registry_id": "gopay_statement", "channel": "PDF"},
        "stockbit_soa": {"source_registry_id": "stockbit_soa", "channel": "PDF"},
        "shopee_orders": {"source_registry_id": "shopee_orders", "channel": "PDF"},
        "shopeepay_mutation": {"source_registry_id": "shopeepay_mutation", "channel": "IMAGE"},
    }

    TEMPLATES: dict[str, tuple[str, str, str]] = {
        "bca_statement": (
            "bca_monthly_statement_v1",
            "parser-v1",
            "75dfe6b0d68ce4a4312f258536edaa86a28535bd0d1d0f9ef6fb22e4858d574c",
        ),
        "jago_statement": (
            "jago_monthly_statement_v1",
            "parser-v1",
            "854403d679306f21bdfcb16efa0c5fa89cc714d0b545e871bbae384196be18fb",
        ),
        "seabank_statement": (
            "seabank_estatement_v1",
            "parser-v1",
            "fc7add5e7b3a321692fda3196b25083e3620bfcf15f4eb63313fdc3de4a99656",
        ),
        "blu_mutation": (
            "blu_account_mutation_v1",
            "parser-v1",
            "2226278f9614625f9174151dad8b8da7851a7455eeec48e0cd9017a96acbcbfc",
        ),
        "blu_portfolio": (
            "blu_portfolio_v1",
            "parser-v1",
            "9c62c6e955340520eb48f527fa6db973a053fcb81afba241ba43ed599a755657",
        ),
        "gopay_statement": (
            "gopay_estatement_v1",
            "parser-v1",
            "6dcc7e3f14d52f0dc3452513fd8e7fcd8fe0fda2f725f3ae4eba0ae5ea477d48",
        ),
        "stockbit_soa": (
            "stockbit_soa_v1",
            "parser-v1",
            "5c39503419938772d4fcb0d0f75364da81736d88f149b9e0d54e7eae8ef76bee",
        ),
        "shopee_orders": (
            "shopee_order_receipt_v1",
            "parser-v1",
            "236728f9f2577d58324a986960137c799b1dfaf2258934a0ac622e9a435e21d4",
        ),
        "shopeepay_mutation": (
            "shopeepay_transaction_history_image_v1",
            "parser-v1",
            "88c190c27eb14eec045f99a070f8749ee87b2a39f01d7fbf566399cb9d630f18",
        ),
    }

    def lookup_exact_documents(
        self, content_sha256: str
    ) -> tuple[SourceDocumentReadRecord, ...]:
        return ()

    def lookup_natural_documents(
        self, source_registry_id: str, natural_document_key: str
    ) -> tuple[SourceDocumentReadRecord, ...]:
        return ()

    def source_capability(
        self, source_registry_id: str
    ) -> Mapping[str, object] | None:
        return self.CAPABILITIES.get(source_registry_id)

    def template_authority(
        self, *, source_registry_id: str, template_fingerprint: str
    ) -> TemplateResolution:
        record = self.TEMPLATES.get(source_registry_id)
        if record is None:
            return TemplateResolution(status=TemplateMatchStatus.UNKNOWN_TEMPLATE)
        t_id, pv, expected_fp = record
        if template_fingerprint == expected_fp:
            return TemplateResolution(
                status=TemplateMatchStatus.KNOWN,
                template_id=t_id,
                parser_version=pv,
            )
        return TemplateResolution(status=TemplateMatchStatus.TEMPLATE_DRIFT)


class ReadOnlyAuthorizer:
    """SQLite authorizer proving zero database mutations or schema modifications."""

    def __init__(self) -> None:
        self.mutation_attempts: list[tuple[int, Any, Any, Any, Any]] = []

    def __call__(
        self,
        action: int,
        arg1: Any,
        arg2: Any,
        dbname: Any,
        source: Any,
    ) -> int:
        deny_actions = {
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_ALTER_TABLE,
            sqlite3.SQLITE_CREATE_INDEX,
            sqlite3.SQLITE_DROP_INDEX,
            sqlite3.SQLITE_CREATE_VIEW,
            sqlite3.SQLITE_DROP_VIEW,
            sqlite3.SQLITE_CREATE_TRIGGER,
            sqlite3.SQLITE_DROP_TRIGGER,
            sqlite3.SQLITE_ATTACH,
            sqlite3.SQLITE_DETACH,
        }
        if action in deny_actions:
            self.mutation_attempts.append((action, arg1, arg2, dbname, source))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK


def compute_file_sha256(file_path: Path | str) -> str:
    """Computes SHA-256 of file bytes. Raises FileNotFoundError if missing."""
    p = Path(file_path)
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"File not found: {p}")
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def verify_production_db_bytes(db_path: Path | str | None) -> str:
    """Validates production database bytes without opening SQLite connection or SQL execution."""
    if not db_path or str(db_path).strip() == "":
        raise ValueError("Production database path must be runtime supplied; missing path is failure")
    p = Path(db_path)
    if not p.exists():
        raise FileNotFoundError(f"Production database file not found: {p.name}")
    h = compute_file_sha256(p)
    if h != EXPECTED_PRODUCTION_DB_SHA256:
        raise ValueError(
            f"Production database SHA-256 mismatch: got {h}, expected {EXPECTED_PRODUCTION_DB_SHA256}"
        )
    return h


def sanitize_csv_cell(value: Any) -> str:
    """Sanitizes a CSV cell to prevent spreadsheet formula injection.

    Trims leading whitespace and checks formula triggers: =, +, -, @, \t, \r, \n, \ufeff.
    Prepends an apostrophe if triggered.
    """
    if value is None:
        return ""
    s = str(value)
    formula_triggers = ("=", "+", "-", "@", "\t", "\r", "\n", "\ufeff")
    trimmed = s.lstrip(" \ufeff")
    if (s and s[0] in formula_triggers) or (trimmed and trimmed[0] in formula_triggers):
        return "'" + s
    return s


def compute_canonical_replay_digest(batch: DryRunBatchResult) -> str:
    """Computes a deterministic SHA-256 digest over batch matching decisions and groups."""
    if batch.match_plan is None:
        raise ValueError("Cannot compute digest for a batch with no match plan")
    p = batch.match_plan
    canonical_decisions = []
    for d in sorted(p.decisions, key=lambda item: item.evidence_key):
        canonical_decisions.append({
            "k": d.evidence_key,
            "t": d.match_tier.value,
            "r": d.match_relation.value if d.match_relation else None,
            "g": d.group_key,
            "rc": sorted([c.value for c in d.reason_codes]),
            "al": d.is_auto_link_eligible,
        })
    canonical_groups = []
    for g in sorted(p.groups, key=lambda item: item.group_key):
        canonical_groups.append({
            "gk": g.group_key,
            "t": g.match_tier.value,
            "r": g.match_relation.value,
            "m": sorted(list(g.member_evidence_keys)),
            "rc": sorted([c.value for c in g.reason_codes]),
            "al": g.is_auto_link_eligible,
        })
    payload = json.dumps(
        {"d": canonical_decisions, "g": canonical_groups},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def extract_evidence_metadata_map(
    batch: DryRunBatchResult,
) -> dict[str, dict[str, Any]]:
    """Extracts safe metadata (source_registry_id, event_role, statement_month) for each evidence key."""
    meta_map: dict[str, dict[str, Any]] = {}
    for doc in batch.documents:
        if doc.adapter_result is None or not doc.adapter_result.events:
            continue
        is_doc_review = (doc.disposition is DryRunDisposition.REVIEW_REQUIRED)
        for env in doc.adapter_result.events:
            ev_key = compute_evidence_key(
                env.source_document_id,
                env.source_registry_id,
                env.event_role,
                env.row_fingerprint,
            )
            dt = None
            payload = env.payload
            if hasattr(payload, "occurred_at") and payload.occurred_at:
                dt = str(payload.occurred_at)
            elif hasattr(payload, "posted_at") and payload.posted_at:
                dt = str(payload.posted_at)
            elif hasattr(payload, "order_date") and payload.order_date:
                dt = str(payload.order_date)
            elif hasattr(payload, "trade_date") and payload.trade_date:
                dt = str(payload.trade_date)
            elif hasattr(payload, "settlement_date") and payload.settlement_date:
                dt = str(payload.settlement_date)

            statement_month = "UNKNOWN"
            if dt and len(dt) >= 7 and dt[:4].isdigit() and dt[4] == "-" and dt[5:7].isdigit():
                statement_month = dt[:7]

            meta_map[ev_key] = {
                "source_registry_id": env.source_registry_id,
                "event_role": env.event_role.value if hasattr(env.event_role, "value") else str(env.event_role),
                "statement_month": statement_month,
                "doc_review_required": is_doc_review,
            }
    return meta_map


def write_evidence_review_csv(
    batch: DryRunBatchResult,
    output_csv_path: Path | str,
) -> Path:
    """Atomically writes evidence review CSV with 10 required columns and formula injection protection."""
    if batch.match_plan is None:
        raise ValueError("Cannot generate CSV from a batch without match plan")
    out_path = Path(output_csv_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    meta_map = extract_evidence_metadata_map(batch)
    rows: list[dict[str, str]] = []

    for d in batch.match_plan.decisions:
        meta = meta_map.get(d.evidence_key, {})
        statement_month = meta.get("statement_month", "UNKNOWN")
        source_registry_id = meta.get("source_registry_id", "UNKNOWN")
        event_role = meta.get("event_role", "UNKNOWN")

        if meta.get("doc_review_required", False):
            review_flag = "REVIEW_REQUIRED"
        elif d.match_tier is MatchTier.INELIGIBLE:
            review_flag = "INELIGIBLE"
        elif d.match_tier is MatchTier.AMBIGUOUS:
            review_flag = "AMBIGUOUS"
        else:
            review_flag = "NONE"

        reason_codes_str = ";".join(c.value for c in d.reason_codes)

        rows.append({
            "statement_month": sanitize_csv_cell(statement_month),
            "source_registry_id": sanitize_csv_cell(source_registry_id),
            "event_role": sanitize_csv_cell(event_role),
            "evidence_key": sanitize_csv_cell(d.evidence_key),
            "match_tier": sanitize_csv_cell(d.match_tier.value),
            "match_relation": sanitize_csv_cell(d.match_relation.value if d.match_relation else ""),
            "group_key": sanitize_csv_cell(d.group_key or ""),
            "auto_link_eligible": sanitize_csv_cell("true" if d.is_auto_link_eligible else "false"),
            "reason_codes": sanitize_csv_cell(reason_codes_str),
            "review_flag": sanitize_csv_cell(review_flag),
        })

    rows.sort(key=lambda r: (r["statement_month"], r["source_registry_id"], r["evidence_key"]))

    tmp_path = out_path.parent / f"{out_path.name}.tmp.{os.getpid()}"
    try:
        with open(tmp_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES, lineterminator="\n")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, out_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    return out_path


def generate_summary_dict(
    batch: DryRunBatchResult,
    matching_context: MatchingContext | None = None,
    digest: str | None = None,
    inventory_summary: dict[str, Any] | None = None,
    three_pass_verification: dict[str, Any] | None = None,
    db_integrity_info: dict[str, Any] | None = None,
    three_pass_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Builds sanitized summary dictionary containing only metadata, counts, and required aggregations."""
    if batch.match_plan is None:
        raise ValueError("Batch has no match plan")
    p = batch.match_plan
    if digest is None:
        digest = compute_canonical_replay_digest(batch)

    exact_member_ev = sum(len(g.member_evidence_keys) for g in p.exact_groups)
    strong_member_ev = sum(len(g.member_evidence_keys) for g in p.strong_groups)
    ambiguous_ev = len(p.ambiguous_evidence)
    unmatched_ev = len(p.unmatched_evidence)
    ineligible_ev = len(p.ineligible_evidence)
    total_decisions = len(p.decisions)

    ev_keys = [d.evidence_key for d in p.decisions]
    dup_ev_keys = len(ev_keys) - len(set(ev_keys))
    dup_decisions = len(p.decisions) - len({d.evidence_key: d for d in p.decisions})
    g_keys = [g.group_key for g in p.groups]
    dup_g_keys = len(g_keys) - len(set(g_keys))

    all_ev_keys_set = set(ev_keys)
    unknown_group_members = sum(
        1 for g in p.groups for m in g.member_evidence_keys if m not in all_ev_keys_set
    )

    member_to_groups: dict[str, list[str]] = collections.defaultdict(list)
    for g in p.groups:
        for m in g.member_evidence_keys:
            member_to_groups[m].append(g.group_key)
    multi_group_members = sum(1 for m, glist in member_to_groups.items() if len(glist) > 1)

    ineligible_or_review_keys = set()
    for d in p.decisions:
        if d.match_tier is MatchTier.INELIGIBLE:
            ineligible_or_review_keys.add(d.evidence_key)

    # Authority violations count:
    invalid_autolink = 0
    review_ev_autolinked = 0
    for g in p.groups:
        if g.is_auto_link_eligible:
            has_ineligible = any(m in ineligible_or_review_keys for m in g.member_evidence_keys)
            if has_ineligible:
                invalid_autolink += 1
                review_ev_autolinked += sum(1 for m in g.member_evidence_keys if m in ineligible_or_review_keys)
            if g.match_tier not in (MatchTier.EXACT, MatchTier.STRONG):
                invalid_autolink += 1

    # Aggregations
    meta_map = extract_evidence_metadata_map(batch)

    by_source_family: dict[str, int] = collections.defaultdict(int)
    by_statement_month: dict[str, int] = collections.defaultdict(int)
    by_source_registry_id: dict[str, int] = collections.defaultdict(int)
    by_event_role: dict[str, int] = collections.defaultdict(int)
    by_match_tier: dict[str, int] = collections.defaultdict(int)
    by_match_relation: dict[str, int] = collections.defaultdict(int)
    by_reason_code: dict[str, int] = collections.defaultdict(int)

    for d in p.decisions:
        meta = meta_map.get(d.evidence_key, {})
        fam = meta.get("source_registry_id", "UNKNOWN")
        month = meta.get("statement_month", "UNKNOWN")
        reg_id = meta.get("source_registry_id", "UNKNOWN")
        role = meta.get("event_role", "UNKNOWN")
        tier = d.match_tier.value
        rel = d.match_relation.value if d.match_relation else "NONE"

        by_source_family[fam] += 1
        by_statement_month[month] += 1
        by_source_registry_id[reg_id] += 1
        by_event_role[role] += 1
        by_match_tier[tier] += 1
        by_match_relation[rel] += 1

        for rc in d.reason_codes:
            by_reason_code[rc.value] += 1

    conservation_pass = (
        exact_member_ev + strong_member_ev + ambiguous_ev + unmatched_ev + ineligible_ev
        == total_decisions
    )

    rel_summary = {
        "configured": matching_context is not None and len(matching_context.relationships) > 0,
        "relationship_count": len(matching_context.relationships) if matching_context else 0,
    }

    matching_summary = {
        "evidence_total": total_decisions,
        "decisions_total": total_decisions,
        "total_groups": len(p.groups),
        "exact_groups": len(p.exact_groups),
        "exact_member_evidence": exact_member_ev,
        "strong_groups": len(p.strong_groups),
        "strong_member_evidence": strong_member_ev,
        "ambiguous_evidence": ambiguous_ev,
        "unmatched_evidence": unmatched_ev,
        "ineligible_evidence": ineligible_ev,
        "duplicate_evidence_keys": dup_ev_keys,
        "duplicate_decisions": dup_decisions,
        "duplicate_group_keys": dup_g_keys,
        "multi_group_members": multi_group_members,
        "unknown_group_members": unknown_group_members,
        "invalid_auto_link_groups": invalid_autolink,
        "review_evidence_auto_linked": review_ev_autolinked,
        "conservation_pass": conservation_pass,
    }

    # Root summary structure
    summary = {
        "inventory": inventory_summary or {},
        "document_summary": {
            "total_artifacts": batch.unique_artifact_count,
            "ready_for_staging_count": batch.ready_for_staging_count,
            "review_required_count": batch.review_required_count,
            "failed_count": batch.failed_count,
            "exact_duplicate_count": batch.exact_duplicate_count,
        },
        "matching_summary": matching_summary,
        "replay_summary": {
            **matching_summary,
            "total_artifacts": batch.unique_artifact_count,
            "ready_for_staging_count": batch.ready_for_staging_count,
            "review_required_count": batch.review_required_count,
            "failed_count": batch.failed_count,
            "replay_digest": digest,
        },
        "three_pass_verification": three_pass_verification or three_pass_info or {},
        "aggregations": {
            "by_source_family": dict(sorted(by_source_family.items())),
            "by_statement_month": dict(sorted(by_statement_month.items())),
            "by_source_registry_id": dict(sorted(by_source_registry_id.items())),
            "by_event_role": dict(sorted(by_event_role.items())),
            "by_match_tier": dict(sorted(by_match_tier.items())),
            "by_match_relation": dict(sorted(by_match_relation.items())),
            "by_reason_code": dict(sorted(by_reason_code.items())),
        },
        "database_integrity": db_integrity_info or {},
        "relationship_authority": rel_summary,
    }
    return summary


def write_summary_json(
    summary: dict[str, Any],
    output_json_path: Path | str,
) -> Path:
    """Atomically writes sanitized summary JSON."""
    out_path = Path(output_json_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.parent / f"{out_path.name}.tmp.{os.getpid()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, out_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
    return out_path


def parse_relationship_config(config_dict: Mapping[str, Any] | None) -> MatchingContext:
    """Strictly validates and builds MatchingContext from external JSON configuration."""
    if not config_dict:
        return MatchingContext(relationships=())

    if not isinstance(config_dict, Mapping):
        raise ValueError("Relationship configuration must be a JSON object mapping")

    extra_top = set(config_dict.keys()) - {"relationships"}
    if extra_top:
        raise ValueError(f"Unexpected top-level fields in relationship config: {sorted(extra_top)}")

    raw_relationships = config_dict.get("relationships", [])
    if not isinstance(raw_relationships, list):
        raise ValueError("'relationships' must be a list")

    rel_records: list[AccountRelationship] = []
    seen: set[tuple[str, str, str]] = set()

    for idx, item in enumerate(raw_relationships):
        if not isinstance(item, Mapping):
            raise ValueError(f"Relationship entry at index {idx} must be a JSON object")

        expected_fields = {"relationship_kind", "source_registry_id", "protected_account_key"}
        item_keys = set(item.keys())
        if item_keys != expected_fields:
            missing = expected_fields - item_keys
            extra = item_keys - expected_fields
            raise ValueError(
                f"Relationship entry at index {idx} invalid fields: missing={sorted(missing)}, extra={sorted(extra)}"
            )

        kind_str = item["relationship_kind"]
        source_id = item["source_registry_id"]
        protected_key = item["protected_account_key"]

        if kind_str not in VALID_RELATIONSHIP_KINDS:
            raise ValueError(f"Invalid relationship_kind: {kind_str!r} at index {idx}")

        if source_id not in CANONICAL_FAMILIES:
            raise ValueError(f"Unknown source_registry_id: {source_id!r} at index {idx}")

        validate_protected_key(protected_key, field_name="protected_account_key")

        tuple_key = (kind_str, source_id, protected_key)
        if tuple_key in seen:
            raise ValueError(f"Duplicate relationship entry detected at index {idx}: {tuple_key}")
        seen.add(tuple_key)

        rel_records.append(
            AccountRelationship(
                source_key=source_id,
                target_key=protected_key,
                relationship_kind=RelationshipKind(kind_str),
            )
        )

    return MatchingContext(relationships=tuple(rel_records))


def load_private_corpus_config(config_path: Path | str) -> dict[str, Path]:
    """Loads external corpus configuration mapping sanitized family aliases to private bundle paths."""
    p = Path(config_path)
    if not p.exists():
        raise FileNotFoundError(f"Corpus configuration not found: {p}")
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or "corpus" not in data or not isinstance(data["corpus"], dict):
        raise ValueError("Corpus configuration must contain a top-level 'corpus' object")

    corpus_raw = data["corpus"]
    configured_families = set(corpus_raw.keys())
    expected_families = set(CANONICAL_FAMILIES)
    if configured_families != expected_families:
        missing = expected_families - configured_families
        extra = configured_families - expected_families
        raise ValueError(f"Invalid corpus families: missing={sorted(missing)}, extra={sorted(extra)}")

    resolved: dict[str, Path] = {}
    for family, file_path_str in corpus_raw.items():
        fp = Path(file_path_str)
        if not fp.exists():
            raise FileNotFoundError(f"Bundle path for family {family} does not exist")
        resolved[family] = fp
    return resolved


def validate_inventory(discovered_by_family: Mapping[str, Sequence[DiscoveredArtifact]]) -> dict[str, Any]:
    """Strictly validates frozen inventory before parsing."""
    configured_families = set(discovered_by_family.keys())
    expected_families = set(CANONICAL_FAMILIES)
    if configured_families != expected_families:
        missing = expected_families - configured_families
        extra = configured_families - expected_families
        raise ValueError(f"Inventory family mismatch: missing={sorted(missing)}, extra={sorted(extra)}")

    by_family_counts: dict[str, int] = {}
    total_pdfs = 0
    total_images = 0
    all_sha256s: set[str] = set()

    for family in CANONICAL_FAMILIES:
        artifacts = discovered_by_family[family]
        count = len(artifacts)
        by_family_counts[family] = count

        expected_count = EXPECTED_INVENTORY[family]
        if count != expected_count:
            raise ValueError(
                f"Inventory count mismatch for {family}: got {count}, expected {expected_count}"
            )

        if family == "shopeepay_mutation":
            total_images += count
            for a in artifacts:
                if a.extension.lower() not in (".jpg", ".jpeg", ".png"):
                    raise ValueError(f"Unexpected non-image extension in {family}: {a.extension}")
        else:
            total_pdfs += count
            for a in artifacts:
                if a.extension.lower() != ".pdf":
                    raise ValueError(f"Unexpected non-pdf extension in {family}: {a.extension}")

        for a in artifacts:
            if a.content_sha256 in all_sha256s:
                raise ValueError(f"Duplicate content SHA-256 detected in inventory: {a.content_sha256}")
            all_sha256s.add(a.content_sha256)

    if total_pdfs != EXPECTED_TOTAL_PDFS:
        raise ValueError(f"Total PDFs mismatch: got {total_pdfs}, expected {EXPECTED_TOTAL_PDFS}")
    if total_images != EXPECTED_TOTAL_IMAGES:
        raise ValueError(f"Total images mismatch: got {total_images}, expected {EXPECTED_TOTAL_IMAGES}")
    total_artifacts = total_pdfs + total_images
    if total_artifacts != EXPECTED_TOTAL_ARTIFACTS:
        raise ValueError(f"Total artifacts mismatch: got {total_artifacts}, expected {EXPECTED_TOTAL_ARTIFACTS}")

    return {
        "by_family": by_family_counts,
        "total_artifacts": total_artifacts,
        "total_pdfs": total_pdfs,
        "total_images": total_images,
    }


def validate_replay_gate(
    pass_a: DryRunBatchResult,
    pass_b: DryRunBatchResult,
    pass_c: DryRunBatchResult,
    *,
    inventory_summary: dict[str, Any] | None = None,
    pre_sqlite_sha256: str | None = None,
    post_sqlite_sha256: str | None = None,
) -> dict[str, Any]:
    """Unified fail-closed gate validator verifying all contract and quality invariants."""
    # 1. Inventory check
    if inventory_summary is not None:
        if inventory_summary.get("total_artifacts") != EXPECTED_TOTAL_ARTIFACTS:
            raise ValueError("Gate failure: total artifacts mismatch")
        if inventory_summary.get("total_pdfs") != EXPECTED_TOTAL_PDFS:
            raise ValueError("Gate failure: total PDFs mismatch")
        if inventory_summary.get("total_images") != EXPECTED_TOTAL_IMAGES:
            raise ValueError("Gate failure: total images mismatch")

    # 2. Database integrity
    if pre_sqlite_sha256 is not None or post_sqlite_sha256 is not None:
        if pre_sqlite_sha256 != EXPECTED_PRODUCTION_DB_SHA256:
            raise ValueError("Gate failure: pre SQLite hash mismatch")
        if post_sqlite_sha256 != EXPECTED_PRODUCTION_DB_SHA256:
            raise ValueError("Gate failure: post SQLite hash mismatch")
        if pre_sqlite_sha256 != post_sqlite_sha256:
            raise ValueError("Gate failure: SQLite database was modified during replay")

    # 3. Validate each pass individually using official plan validator
    passes = [pass_a, pass_b, pass_c]
    digests = []

    for idx, batch in enumerate(passes):
        label = f"Pass {'ABC'[idx]}"
        if batch.match_plan is None:
            raise ValueError(f"Gate failure: {label} match_plan is None")
        if batch.failed_count > 0:
            raise ValueError(f"Gate failure: {label} has failed artifacts ({batch.failed_count})")

        p = batch.match_plan
        expected_keys = {d.evidence_key for d in p.decisions}
        if len(expected_keys) != len(p.decisions):
            raise ValueError(f"Gate failure: {label} contains duplicate decisions")

        # Official validator call
        _validate_evidence_match_plan(p, expected_keys)

        # Ineligible/review authority check
        ineligible_keys = {d.evidence_key for d in p.ineligible_evidence}
        for g in p.groups:
            if g.is_auto_link_eligible:
                for m in g.member_evidence_keys:
                    if m in ineligible_keys:
                        raise ValueError(f"Gate failure: {label} auto-linked ineligible evidence: {m}")
            if g.match_tier not in (MatchTier.EXACT, MatchTier.STRONG):
                raise ValueError(f"Gate failure: {label} group has invalid tier {g.match_tier}")

        # Ungrouped check
        for d in p.decisions:
            if d.group_key is None and d.match_relation is not None:
                raise ValueError(f"Gate failure: {label} ungrouped decision has match_relation")

        digests.append(compute_canonical_replay_digest(batch))

    # 4. Three-pass determinism comparison
    digest_a, digest_b, digest_c = digests
    repeat_det = (digest_a == digest_b)
    shuffle_det = (digest_a == digest_c)
    if not repeat_det:
        raise ValueError("Gate failure: Pass A and Pass B digests do not match")
    if not shuffle_det:
        raise ValueError("Gate failure: Pass A and Pass C (shuffled) digests do not match")

    # Compare decision tuples across Pass A and Pass C
    decisions_a = {d.evidence_key: (d.match_tier, d.match_relation, d.group_key, d.reason_codes, d.is_auto_link_eligible) for d in pass_a.match_plan.decisions}
    decisions_c = {d.evidence_key: (d.match_tier, d.match_relation, d.group_key, d.reason_codes, d.is_auto_link_eligible) for d in pass_c.match_plan.decisions}
    if decisions_a != decisions_c:
        raise ValueError("Gate failure: Decision mismatch between normal and shuffled pass")

    # Compare group tuples across Pass A and Pass C
    groups_a = {g.group_key: (g.match_tier, g.match_relation, sorted(g.member_evidence_keys), sorted(c.value for c in g.reason_codes), g.is_auto_link_eligible) for g in pass_a.match_plan.groups}
    groups_c = {g.group_key: (g.match_tier, g.match_relation, sorted(g.member_evidence_keys), sorted(c.value for c in g.reason_codes), g.is_auto_link_eligible) for g in pass_c.match_plan.groups}
    if groups_a != groups_c:
        raise ValueError("Gate failure: Group mismatch between normal and shuffled pass")

    return {
        "pass_a_digest": digest_a,
        "pass_b_digest": digest_b,
        "pass_c_digest": digest_c,
        "repeat_deterministic": repeat_det,
        "shuffle_deterministic": shuffle_det,
        "overall_deterministic": repeat_det and shuffle_det,
    }


def execute_three_pass_audit(
    corpus_config_path: Path | str,
    output_dir: Path | str,
    db_path: Path | str | None = None,
    master_secret: str | None = None,
    relationship_config_path: Path | str | None = None,
) -> tuple[DryRunBatchResult, Path, Path, dict[str, Any]]:
    """Executes verifiable 3-pass private corpus replay and generates sanitized outputs."""
    if not master_secret or len(master_secret) < 32:
        raise ValueError("Master secret is required and must be at least 32 characters long")
    if not db_path or str(db_path).strip() == "":
        raise ValueError("Production database path must be runtime supplied; missing path is failure")

    out_dir_path = Path(output_dir)
    out_dir_path.mkdir(parents=True, exist_ok=True)

    # 1. Pre-run database verification
    pre_db_hash = verify_production_db_bytes(db_path)

    # 2. Load corpus configuration
    corpus_map = load_private_corpus_config(corpus_config_path)

    # 3. Stage bundles into neutral family aliases
    with tempfile.TemporaryDirectory() as stage_td:
        stage_dir = Path(stage_td)
        discovered_by_family: dict[str, Sequence[DiscoveredArtifact]] = {}

        for family in CANONICAL_FAMILIES:
            src_path = corpus_map[family]
            fam_dir = stage_dir / family
            fam_dir.mkdir(parents=True, exist_ok=True)
            dest_file = fam_dir / f"{family}.zip"
            try:
                os.link(src_path, dest_file)
            except OSError:
                shutil.copyfile(src_path, dest_file)

            fam_disc = discover_files(fam_dir)
            discovered_by_family[family] = fam_disc.artifacts

        # 4. Enforce frozen inventory before parsing
        inventory_summary = validate_inventory(discovered_by_family)

        # 5. Full discovery across all staged families
        disc_full = discover_files(stage_dir)
        claims = {
            art.content_sha256: "shopeepay_mutation"
            for art in disc_full.artifacts
            if art.extension.lower() in (".jpg", ".jpeg", ".png")
        }

        registry = InMemoryRegistryAuthority()
        catalog = build_default_adapter_catalog()

        # Parse relationships if provided
        matching_context = None
        if relationship_config_path:
            p_rel = Path(relationship_config_path)
            if p_rel.exists():
                with open(p_rel, "r", encoding="utf-8") as f:
                    rel_data = json.load(f)
                matching_context = parse_relationship_config(rel_data)

        # Pass A: normal discovery order
        prot_a = AccountIdentityProtector(secret=master_secret)
        res_a = AccountDiscoveryResolver(prot_a)
        batch_a = dry_run_batch(
            stage_dir,
            disc_full,
            registry=registry,
            adapter_catalog=catalog,
            claimed_source_registry_ids=claims,
            account_resolver=res_a,
            matching_context=matching_context,
        )

        # Pass B: repeated identical invocation
        prot_b = AccountIdentityProtector(secret=master_secret)
        res_b = AccountDiscoveryResolver(prot_b)
        batch_b = dry_run_batch(
            stage_dir,
            disc_full,
            registry=registry,
            adapter_catalog=catalog,
            claimed_source_registry_ids=claims,
            account_resolver=res_b,
            matching_context=matching_context,
        )

        # Pass C: deterministic shuffled order
        shuffled_artifacts = tuple(sorted(
            disc_full.artifacts,
            key=lambda a: hashlib.sha256((a.content_sha256 + "_salt_pass_c").encode()).hexdigest(),
        ))
        disc_c = DiscoveryResult(
            artifacts=shuffled_artifacts,
            diagnostics=disc_full.diagnostics,
            archives_seen=disc_full.archives_seen,
            physical_files_seen=disc_full.physical_files_seen,
            supported_occurrences=disc_full.supported_occurrences,
        )
        prot_c = AccountIdentityProtector(secret=master_secret)
        res_c = AccountDiscoveryResolver(prot_c)
        batch_c = dry_run_batch(
            stage_dir,
            disc_c,
            registry=registry,
            adapter_catalog=catalog,
            claimed_source_registry_ids=claims,
            account_resolver=res_c,
            matching_context=matching_context,
        )

    # 6. Post-run database verification
    post_db_hash = verify_production_db_bytes(db_path)

    # 7. Unified gate validation
    gate_results = validate_replay_gate(
        batch_a,
        batch_b,
        batch_c,
        inventory_summary=inventory_summary,
        pre_sqlite_sha256=pre_db_hash,
        post_sqlite_sha256=post_db_hash,
    )

    db_integrity_info = {
        "pre_sqlite_sha256": pre_db_hash,
        "post_sqlite_sha256": post_db_hash,
        "sqlite_unchanged": (pre_db_hash == post_db_hash == EXPECTED_PRODUCTION_DB_SHA256),
    }

    # 8. Generate summary dict and atomically write outputs
    summary = generate_summary_dict(
        batch_a,
        matching_context=matching_context,
        digest=gate_results["pass_a_digest"],
        inventory_summary=inventory_summary,
        three_pass_verification=gate_results,
        db_integrity_info=db_integrity_info,
    )

    csv_path = out_dir_path / "evidence_review.csv"
    json_path = out_dir_path / "summary.json"

    write_evidence_review_csv(batch_a, csv_path)
    write_summary_json(summary, json_path)

    return batch_a, csv_path, json_path, summary
