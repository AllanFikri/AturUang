"""
Ingestion Bridge — Connects WatchedFolderScanner to Ingestion Framework and SafeApplyEngine.

PREREQUISITE FINDINGS:
1. NormalizedEventEnvelope (aturuang/ingestion_adapter.py:852):
   - source_document_id: str
   - source_registry_id: str
   - template_id: str
   - parser_version: str
   - source_channel: SourceChannel
   - event_role: EventRole
   - source_event_id: str | None
   - row_fingerprint: str
   - evidence_quality: ConfidenceLevel
   - parse_confidence: ConfidenceLevel
   - provenance: SourceProvenanceContract
   - payload: EvidencePayload
   - diagnostics: tuple[SafeDiagnostic, ...] = field(default_factory=tuple)

2. CashMovementEvidence (aturuang/ingestion_adapter.py:375):
   - amount: Decimal
   - currency: str
   - direction: EventDirection
   - status: SourceEventStatus
   - account_id: str | None = None
   - subaccount_id: str | None = None
   - source_account_key_raw: str | None = None
   - source_account_display_raw: str | None = None
   - destination_account_key_raw: str | None = None
   - destination_account_display_raw: str | None = None
   - occurred_at: str | None = None
   - posted_at: str | None = None
   - settlement_date: str | None = None
   - direction_raw: str | None = None
   - status_raw: str | None = None
   - counterparty_raw: str | None = None
   - counterparty_normalized: str | None = None
   - description_raw: str | None = None
   - provider_transaction_id_raw: str | None = None
   - reference_raw: str | None = None
   - reference_normalized: str | None = None
   - balance_after: Decimal | None = None
   - payment_method_raw: str | None = None
   - provider_category_raw: str | None = None
   - event_hint: str | None = None
   - payment_components: tuple[PaymentComponentEvidence, ...] = field(default_factory=tuple)

   AdapterResult (aturuang/ingestion_adapter.py:927):
   - descriptor: AdapterDescriptor
   - source_document_id: str
   - parse_status: AdapterParseStatus
   - period_status: PeriodStatus
   - events: tuple[NormalizedEventEnvelope, ...] = field(default_factory=tuple)
   - diagnostics: tuple[SafeDiagnostic, ...] = field(default_factory=tuple)
   - review_reasons: tuple[str, ...] = field(default_factory=tuple)
   - period_start: str | None = None
   - period_end: str | None = None
   - natural_document_key_candidate: str | None = None

3. SafeApplyEngine.__init__ (aturuang/safe_apply.py:419):
   - db_path: Path | str
   - backup_dir: Path | str | None = None

4. EvidenceMatchPlan (aturuang/ingestion_matching.py:371):
   - matcher_contract_version: str
   - decisions: tuple[EvidenceMatchDecision, ...]
   - groups: tuple[EconomicEventGroup, ...]
   - diagnostics: tuple[SafeDiagnostic, ...] = ()

5. MatchTier (aturuang/ingestion_matching.py:55):
   - EXACT = "EXACT"
   - STRONG = "STRONG"
   - AMBIGUOUS = "AMBIGUOUS"
   - UNMATCHED = "UNMATCHED"
   - INELIGIBLE = "INELIGIBLE"

6. discover_files (aturuang/ingestion_discovery.py:845):
   - root: str | Path
   - *
   - policy: DiscoveryPolicy | None = None

7. SqliteRegistryAuthority.__init__ (aturuang/ingestion_orchestration.py:204):
   - connection: sqlite3.Connection = field(repr=False)
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal
import hashlib
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from aturuang.ingestion_contracts import TemplateMatchStatus
from aturuang.ingestion_discovery import DiscoveryPolicy, discover_files
from aturuang.ingestion_orchestration import (
    DryRunDisposition,
    SqliteRegistryAuthority,
    build_default_adapter_catalog,
    dry_run_batch,
)
from aturuang.ingestion_registry_service import SourceDocumentReadRecord, TemplateResolution
from aturuang.review_queue_ui import ReviewQueueManager
from aturuang.safe_apply import ApplyCandidate, LedgerMutation, SafeApplyEngine, auto_apply_candidate


BRIDGE_SUPPORTED_PROVIDERS = frozenset({
    "bca",
    "jago",
    "blu",
    "gopay",
    "seabank",
    "shopeepay",
})

PROVIDER_REGISTRY_MAP: dict[str, str] = {
    "bca": "bca_statement",
    "jago": "jago_statement",
    "blu": "blu_mutation",
    "gopay": "gopay_statement",
    "seabank": "seabank_statement",
    "shopeepay": "shopeepay_mutation",
}


class BridgeRegistryAuthority(SqliteRegistryAuthority):
    """
    Registry authority extending SqliteRegistryAuthority that queries SQLite
    when migrated, and gracefully falls back to built-in provider capabilities
    and templates if the database has not yet run Universal Ingestion migrations.
    """

    FALLBACK_CAPABILITIES: dict[str, dict[str, object]] = {
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

    FALLBACK_TEMPLATES: dict[str, tuple[str, str, str]] = {
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
        self,
        content_sha256: str,
    ) -> tuple[SourceDocumentReadRecord, ...]:
        try:
            return super().lookup_exact_documents(content_sha256)
        except Exception:
            return ()

    def lookup_natural_documents(
        self,
        source_registry_id: str,
        natural_document_key: str,
    ) -> tuple[SourceDocumentReadRecord, ...]:
        try:
            return super().lookup_natural_documents(source_registry_id, natural_document_key)
        except Exception:
            return ()

    def source_capability(
        self,
        source_registry_id: str,
    ) -> Mapping[str, object] | None:
        try:
            cap = super().source_capability(source_registry_id)
            if cap is not None:
                return cap
        except Exception:
            pass
        return self.FALLBACK_CAPABILITIES.get(source_registry_id)

    def template_authority(
        self,
        *,
        source_registry_id: str,
        template_fingerprint: str,
    ) -> TemplateResolution:
        try:
            res = super().template_authority(
                source_registry_id=source_registry_id,
                template_fingerprint=template_fingerprint,
            )
            if res.status == TemplateMatchStatus.KNOWN:
                return res
        except Exception:
            pass

        rec = self.FALLBACK_TEMPLATES.get(source_registry_id)
        if rec is None:
            return TemplateResolution(status=TemplateMatchStatus.UNKNOWN_TEMPLATE)
        t_id, pv, expected_fp = rec
        if template_fingerprint == expected_fp:
            return TemplateResolution(
                status=TemplateMatchStatus.KNOWN,
                template_id=t_id,
                parser_version=pv,
            )
        return TemplateResolution(status=TemplateMatchStatus.TEMPLATE_DRIFT)


def _envelope_to_mutation(env: Any, document_id: str) -> LedgerMutation:
    """
    Map a NormalizedEventEnvelope to LedgerMutation.
    Fails open with safe defaults; never raises.
    """
    payload = getattr(env, "payload", None)

    # 1. Date & Time
    raw_datetime = (
        getattr(env, "occurred_at", None)
        or (getattr(payload, "occurred_at", None) if payload is not None else None)
        or (getattr(payload, "posted_at", None) if payload is not None else None)
        or (getattr(payload, "settlement_date", None) if payload is not None else None)
        or (getattr(payload, "date", None) if payload is not None else None)
        or getattr(env, "date", None)
    )

    date_str = ""
    time_str = ""
    if raw_datetime and isinstance(raw_datetime, str):
        cleaned = raw_datetime.strip()
        if "T" in cleaned:
            parts = cleaned.split("T", 1)
            date_str = parts[0]
            time_str = parts[1][:8]
        elif " " in cleaned:
            parts = cleaned.split(" ", 1)
            date_str = parts[0]
            time_str = parts[1][:8]
        else:
            date_str = cleaned
            time_str = ""

    if not time_str and payload is not None:
        t_attr = getattr(payload, "time", None)
        if t_attr and isinstance(t_attr, str):
            time_str = t_attr[:8]

    if not date_str:
        date_str = dt.date.today().isoformat()
    if not isinstance(time_str, str):
        time_str = ""

    # 2. Transaction Type
    dir_val = getattr(env, "direction", None)
    if dir_val is None and payload is not None:
        dir_val = getattr(payload, "direction", None)

    if hasattr(dir_val, "value"):
        dir_str = str(dir_val.value).upper()
    elif isinstance(dir_val, str):
        dir_str = dir_val.upper()
    else:
        dir_str = ""

    if dir_str == "INFLOW":
        tx_type = "Income"
    elif dir_str == "OUTFLOW":
        tx_type = "Expense"
    elif dir_str == "NEUTRAL":
        tx_type = "Transfer"
    else:
        tx_type = "Expense"

    # 3. Amount
    raw_amount = getattr(payload, "amount", None) if payload is not None else getattr(env, "amount", None)
    if raw_amount is None:
        amount = Decimal("0.00")
    elif isinstance(raw_amount, Decimal):
        amount = raw_amount
    else:
        try:
            amount = Decimal(str(raw_amount))
        except Exception:
            amount = Decimal("0.00")
    if amount < Decimal("0.00"):
        amount = abs(amount)

    # 4. Accounts
    account_from = ""
    account_to = ""
    if payload is not None:
        raw_from = getattr(payload, "account_from", None) or getattr(payload, "source_account_display_raw", None) or getattr(payload, "account_id", None) or ""
        account_from = str(raw_from)
        raw_to = getattr(payload, "account_to", None) or getattr(payload, "destination_account_display_raw", None) or ""
        account_to = str(raw_to)

    # 5. Description
    raw_desc = ""
    if payload is not None:
        raw_desc = getattr(payload, "description_raw", None) or getattr(payload, "description", None) or ""
    elif hasattr(env, "description"):
        raw_desc = getattr(env, "description", "") or ""
    description = str(raw_desc)[:500]

    # 6. Refs & Notes
    doc_prefix = str(document_id)[:16] if document_id else ""
    source_refs = f"ingestion:{doc_prefix}"
    notes = f"[BRIDGE: {doc_prefix}]"

    return LedgerMutation(
        date=date_str,
        time=time_str,
        transaction_type=tx_type,
        amount=amount,
        account_from=account_from,
        account_to=account_to,
        description=description,
        category="",
        status="Confirmed",
        source_refs=source_refs,
        notes=notes,
    )


def process_single_file(
    db_path: str,
    file_path: str,
    provider: str,
    *,
    dry_run: bool = True,
    backup_dir: str | None = None,
) -> dict[str, Any]:
    """
    Process a single statement or mutation file through the universal ingestion
    framework and safe apply engine.
    """
    prov = str(provider).lower().strip()
    if prov not in BRIDGE_SUPPORTED_PROVIDERS:
        return {
            "applied": 0,
            "queued": 0,
            "skipped": 0,
            "errors": [f"Unsupported provider: {provider}"],
            "diagnostics": [],
            "documents_processed": 0,
            "preview": [],
        }

    target_path = Path(file_path)
    if not target_path.exists() or not target_path.is_file():
        return {
            "applied": 0,
            "queued": 0,
            "skipped": 0,
            "errors": [f"File not found: {file_path}"],
            "diagnostics": [],
            "documents_processed": 0,
            "preview": [],
        }

    try:
        # 1. Discover file
        parent_dir = target_path.parent
        policy = DiscoveryPolicy()
        raw_discovery = discover_files(root=parent_dir, policy=policy)

        # Restrict discovery to only the target file by SHA-256
        target_content = target_path.read_bytes()
        target_sha = hashlib.sha256(target_content).hexdigest()
        matching_artifacts = tuple(
            a for a in raw_discovery.artifacts if a.content_sha256 == target_sha
        )
        if not matching_artifacts:
            return {
                "applied": 0,
                "queued": 0,
                "skipped": 1,
                "errors": [f"Target file not matched in discovery: {file_path}"],
                "diagnostics": [],
                "documents_processed": 0,
                "preview": [],
            }

        discovery = dataclasses.replace(
            raw_discovery,
            artifacts=matching_artifacts,
            physical_files_seen=len(matching_artifacts),
        )

        # 2 & 3. Registry and Catalog
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            registry = BridgeRegistryAuthority(connection=conn)
            catalog = build_default_adapter_catalog()

            claimed_reg_id = PROVIDER_REGISTRY_MAP.get(prov)
            claims = (
                {a.content_sha256: claimed_reg_id for a in discovery.artifacts}
                if claimed_reg_id
                else None
            )

            # 4. Dry-run batch
            batch_result = dry_run_batch(
                root=str(parent_dir),
                discovery=discovery,
                registry=registry,
                adapter_catalog=catalog,
                claimed_source_registry_ids=claims,
            )
        finally:
            conn.close()

        # 5. Process documents
        applied = 0
        queued = 0
        skipped = 0
        errors: list[str] = []
        diagnostics: list[dict[str, Any]] = []
        preview: list[dict[str, Any]] = []
        documents_processed = len(batch_result.documents)

        for diag in batch_result.diagnostics:
            diagnostics.append({
                "code": diag.code,
                "stage": getattr(diag.stage, "value", str(diag.stage)),
                "severity": getattr(diag.severity, "value", str(diag.severity)),
                "message": diag.message,
            })

        for doc in batch_result.documents:
            for d_diag in doc.diagnostics:
                diagnostics.append({
                    "code": d_diag.code,
                    "stage": getattr(d_diag.stage, "value", str(d_diag.stage)),
                    "severity": getattr(d_diag.severity, "value", str(d_diag.severity)),
                    "message": d_diag.message,
                })

            if doc.disposition != DryRunDisposition.READY_FOR_STAGING:
                skipped += 1
                continue

            if doc.adapter_result is None or not doc.adapter_result.events:
                skipped += 1
                continue

            envs = doc.adapter_result.events
            actionable_envs = [
                e for e in envs
                if getattr(getattr(e, "event_role", None), "value", str(getattr(e, "event_role", ""))) not in ("ACCOUNT_OBSERVATION", "SOURCE_SUMMARY")
            ]
            if not actionable_envs and envs:
                actionable_envs = list(envs)

            try:
                mutations = tuple(
                    _envelope_to_mutation(e, doc.source_document_id) for e in actionable_envs
                )
            except Exception as m_exc:
                errors.append(f"Failed to map mutations for {doc.source_document_id}: {m_exc}")
                continue

            if not mutations:
                skipped += 1
                continue

            try:
                candidate = ApplyCandidate(
                    candidate_id=f"ing_{doc.source_document_id[:16]}",
                    idempotency_key=f"idem_ing_{doc.content_sha256}",
                    mutations=mutations,
                    participating_evidence_keys=(doc.source_document_id,),
                )
            except Exception as c_exc:
                errors.append(f"Failed to construct candidate for {doc.source_document_id}: {c_exc}")
                continue

            if dry_run:
                preview.append({
                    "candidate_id": candidate.candidate_id,
                    "idempotency_key": candidate.idempotency_key,
                    "preview_hash": candidate.preview_hash,
                    "mutation_count": len(mutations),
                    "mutations": [
                        {
                            "date": m.date,
                            "time": m.time,
                            "transaction_type": m.transaction_type,
                            "amount": str(m.amount),
                            "account_from": m.account_from,
                            "account_to": m.account_to,
                            "description": m.description,
                            "status": m.status,
                            "source_refs": m.source_refs,
                            "notes": m.notes,
                        }
                        for m in mutations
                    ],
                })
            else:
                try:
                    engine = SafeApplyEngine(db_path, backup_dir=backup_dir)
                    match_tier = "EXACT" if (batch_result.match_plan and len(batch_result.match_plan.exact_groups) > 0) else "STRONG"
                    recon_status = "RECONCILED"
                    rev_mgr = ReviewQueueManager(db_path, backup_dir=backup_dir)

                    ok, apply_res, status = auto_apply_candidate(
                        engine,
                        candidate,
                        match_tier=match_tier,
                        reconciliation_status=recon_status,
                        review_manager=rev_mgr,
                    )
                    if ok:
                        applied += 1
                    elif status == "REVIEW_REQUIRED":
                        queued += 1
                    else:
                        skipped += 1
                except Exception as a_exc:
                    errors.append(f"SafeApply error for {doc.source_document_id}: {a_exc}")

        return {
            "applied": applied,
            "queued": queued,
            "skipped": skipped,
            "errors": errors,
            "diagnostics": diagnostics,
            "documents_processed": documents_processed,
            "preview": preview,
        }

    except Exception as exc:
        return {
            "applied": 0,
            "queued": 0,
            "skipped": 0,
            "errors": [f"Processing failed: {str(exc)}"],
            "diagnostics": [],
            "documents_processed": 0,
            "preview": [],
        }


def process_batch_files(
    db_path: str,
    file_paths: list[str],
    provider: str,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    """
    Process multiple statement or mutation files in batch.
    Aggregates per-file processing results.
    """
    combined: dict[str, Any] = {
        "applied": 0,
        "queued": 0,
        "skipped": 0,
        "errors": [],
        "diagnostics": [],
        "documents_processed": 0,
        "preview": [],
    }

    for file_path in file_paths:
        res = process_single_file(db_path, file_path, provider, dry_run=dry_run)
        combined["applied"] += res.get("applied", 0)
        combined["queued"] += res.get("queued", 0)
        combined["skipped"] += res.get("skipped", 0)
        combined["documents_processed"] += res.get("documents_processed", 0)
        combined["errors"].extend(res.get("errors", []))
        combined["diagnostics"].extend(res.get("diagnostics", []))
        combined["preview"].extend(res.get("preview", []))

    return combined
