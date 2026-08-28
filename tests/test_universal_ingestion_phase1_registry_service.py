import sqlite3
import unittest

from aturuang.ingestion_contracts import (
    DocumentIdentityStatus,
    PeriodStatus,
    SourceDocumentIdentity,
    TemplateMatchStatus,
)
from aturuang.ingestion_registry import init_registry_schema
from aturuang.ingestion_registry_service import (
    RegistryConflictError,
    RegistryNotInitializedError,
    create_or_get_import_batch,
    get_source_capability,
    link_legacy_account_name,
    record_source_document_occurrence,
    register_source_document,
    resolve_account_observation,
    resolve_legacy_account_name,
    resolve_template,
    validate_legacy_bridge_readonly,
)


def sha(char: str) -> str:
    return char * 64


class TestUniversalIngestionPhase1RegistryService(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys = ON")
        init_registry_schema(self.con)

    def tearDown(self):
        self.con.close()

    def _seed_bca(self):
        self.con.execute(
            """
            INSERT INTO registry_institutions (
                institution_id, display_name, institution_type
            ) VALUES ('bca', 'BCA', 'BANK')
            """
        )
        self.con.executemany(
            """
            INSERT INTO registry_sources (
                source_registry_id, provider_key, display_name,
                channel, institution_id
            ) VALUES (?, 'bca', ?, ?, 'bca')
            """,
            [
                ('bca_statement', 'BCA Monthly Statement', 'PDF'),
                ('bca_gmail', 'BCA Gmail Evidence', 'EMAIL'),
            ],
        )
        self.con.executemany(
            """
            INSERT INTO registry_source_capabilities (
                source_registry_id, document_kind, completeness_role,
                has_transactions, has_balances, has_running_balance,
                has_native_event_id, has_account_hierarchy,
                supports_reconciliation, near_realtime
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    'bca_statement', 'monthly_statement', 'COMPLETE',
                    1, 1, 1, 0, 1, 1, 0
                ),
                (
                    'bca_gmail', 'transaction_notification', 'NEAR_REALTIME',
                    1, 0, 0, 1, 0, 0, 1
                ),
            ],
        )
        self.con.execute(
            """
            INSERT INTO registry_source_templates (
                template_id, source_registry_id, template_version,
                parser_version, document_kind, input_type,
                template_fingerprint, text_layer_required
            ) VALUES (
                'bca_monthly_statement_v1', 'bca_statement', '1',
                'not-implemented', 'monthly_statement', 'PDF',
                'bca-fingerprint-v1', 1
            )
            """
        )

    def _create_batch(self):
        return create_or_get_import_batch(
            self.con,
            import_batch_id="batch-001",
            source_registry_id="bca_statement",
            mode="DRY_RUN",
            idempotency_key="set-001",
        )

    def _document(
        self,
        document_id,
        digest,
        natural_key,
        semantic=None,
        template_status=TemplateMatchStatus.KNOWN,
        fingerprint="bca-fingerprint-v1",
        source_registry_id="bca_statement",
        import_batch_id="batch-001",
    ):
        return SourceDocumentIdentity(
            source_document_id=document_id,
            content_sha256=digest,
            natural_document_key=natural_key,
            template_id=(
                "bca_monthly_statement_v1"
                if template_status is TemplateMatchStatus.KNOWN
                else "UNKNOWN"
            ),
            template_fingerprint=fingerprint,
            parser_version=(
                "not-implemented"
                if template_status is TemplateMatchStatus.KNOWN
                else "not-run"
            ),
            source_registry_id=source_registry_id,
            import_batch_id=import_batch_id,
            template_match_status=template_status,
            period_status=PeriodStatus.CLOSED,
            semantic_sha256=semantic,
        )

    def test_01_service_refuses_to_auto_initialize_registry(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        try:
            with self.assertRaises(RegistryNotInitializedError):
                get_source_capability(con, "bca_statement")
        finally:
            con.close()

    def test_02_capability_query_preserves_statement_vs_gmail_roles(self):
        self._seed_bca()

        statement = get_source_capability(
            self.con,
            "bca_statement",
        )
        gmail = get_source_capability(
            self.con,
            "bca_gmail",
        )

        self.assertTrue(statement["has_balances"])
        self.assertTrue(statement["supports_reconciliation"])
        self.assertTrue(gmail["near_realtime"])
        self.assertFalse(gmail["has_balances"])

    def test_03_template_exact_match_is_known(self):
        self._seed_bca()

        result = resolve_template(
            self.con,
            source_registry_id="bca_statement",
            template_fingerprint="bca-fingerprint-v1",
        )

        self.assertEqual(result.status, TemplateMatchStatus.KNOWN)
        self.assertEqual(
            result.template_id,
            "bca_monthly_statement_v1",
        )

    def test_04_changed_fingerprint_is_template_drift(self):
        self._seed_bca()

        result = resolve_template(
            self.con,
            source_registry_id="bca_statement",
            template_fingerprint="changed-layout",
        )

        self.assertEqual(
            result.status,
            TemplateMatchStatus.TEMPLATE_DRIFT,
        )
        self.assertIsNone(result.template_id)

    def test_05_source_without_template_contract_is_unknown_template(self):
        self._seed_bca()

        result = resolve_template(
            self.con,
            source_registry_id="bca_gmail",
            template_fingerprint="some-email-shape",
        )

        self.assertEqual(
            result.status,
            TemplateMatchStatus.UNKNOWN_TEMPLATE,
        )

    def test_06_import_batch_replay_is_idempotent(self):
        self._seed_bca()

        first = self._create_batch()
        second = create_or_get_import_batch(
            self.con,
            import_batch_id="different-request-id",
            source_registry_id="bca_statement",
            mode="DRY_RUN",
            idempotency_key="set-001",
        )

        self.assertEqual(
            first["import_batch_id"],
            second["import_batch_id"],
        )
        self.assertEqual(
            second["import_batch_id"],
            "batch-001",
        )

    def test_07_import_batch_replay_with_changed_mode_fails_closed(self):
        self._seed_bca()
        self._create_batch()

        with self.assertRaises(RegistryConflictError):
            create_or_get_import_batch(
                self.con,
                import_batch_id="batch-002",
                source_registry_id="bca_statement",
                mode="STAGING",
                idempotency_key="set-001",
            )

    def test_08_new_document_is_registered_once(self):
        self._seed_bca()
        self._create_batch()
        candidate = self._document(
            "doc-001",
            sha("a"),
            "bca:main:2026-07",
            sha("b"),
        )

        result = register_source_document(
            self.con,
            candidate,
        )

        self.assertEqual(
            result.identity_status,
            DocumentIdentityStatus.NEW,
        )
        self.assertTrue(result.inserted)
        count = self.con.execute(
            "SELECT COUNT(*) FROM registry_source_documents"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_09_exact_sha_replay_reuses_original_document(self):
        self._seed_bca()
        self._create_batch()

        original = self._document(
            "doc-original",
            sha("a"),
            "bca:main:2026-07",
            sha("b"),
        )
        register_source_document(self.con, original)

        copy = self._document(
            "doc-copy",
            sha("a"),
            "different-natural-key",
            sha("c"),
        )
        result = register_source_document(
            self.con,
            copy,
        )

        self.assertEqual(
            result.identity_status,
            DocumentIdentityStatus.EXACT_DUPLICATE,
        )
        self.assertFalse(result.inserted)
        self.assertEqual(
            result.source_document_id,
            "doc-original",
        )
        count = self.con.execute(
            "SELECT COUNT(*) FROM registry_source_documents"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_10_semantic_duplicate_keeps_separate_document_evidence(self):
        self._seed_bca()
        self._create_batch()

        original = self._document(
            "doc-original",
            sha("a"),
            "bca:main:2026-07",
            sha("b"),
        )
        register_source_document(self.con, original)

        reexport = self._document(
            "doc-reexport",
            sha("c"),
            "bca:main:2026-07",
            sha("b"),
        )
        result = register_source_document(
            self.con,
            reexport,
        )

        self.assertEqual(
            result.identity_status,
            DocumentIdentityStatus.SEMANTIC_DUPLICATE,
        )
        self.assertEqual(
            result.related_document_id,
            "doc-original",
        )
        self.assertTrue(result.inserted)

    def test_11_semantic_conflict_is_revision_or_conflict(self):
        self._seed_bca()
        self._create_batch()

        original = self._document(
            "doc-original",
            sha("a"),
            "bca:main:2026-07",
            sha("b"),
        )
        register_source_document(self.con, original)

        changed = self._document(
            "doc-changed",
            sha("c"),
            "bca:main:2026-07",
            sha("d"),
        )
        result = register_source_document(
            self.con,
            changed,
        )

        self.assertEqual(
            result.identity_status,
            DocumentIdentityStatus.REVISION_OR_CONFLICT,
        )
        self.assertEqual(
            result.related_document_id,
            "doc-original",
        )

    def test_12_template_drift_document_can_be_recorded_but_not_parsed(self):
        self._seed_bca()
        self._create_batch()

        drift = self._document(
            "doc-drift",
            sha("e"),
            "bca:drift:2026-08",
            template_status=TemplateMatchStatus.TEMPLATE_DRIFT,
            fingerprint="changed-layout",
        )
        result = register_source_document(
            self.con,
            drift,
        )

        row = self.con.execute(
            """
            SELECT template_match_status, parser_version
            FROM registry_source_documents
            WHERE source_document_id=?
            """,
            (result.source_document_id,),
        ).fetchone()

        self.assertEqual(
            row["template_match_status"],
            "TEMPLATE_DRIFT",
        )
        self.assertEqual(row["parser_version"], "not-run")

    def test_13_true_unknown_template_source_is_recorded_fail_closed(self):
        self._seed_bca()
        create_or_get_import_batch(
            self.con,
            import_batch_id="batch-gmail-001",
            source_registry_id="bca_gmail",
            mode="DRY_RUN",
            idempotency_key="gmail-set-001",
        )

        unknown = self._document(
            "doc-gmail-unknown",
            sha("f"),
            "bca:gmail:unknown:001",
            template_status=TemplateMatchStatus.UNKNOWN_TEMPLATE,
            fingerprint="email-shape-unregistered",
            source_registry_id="bca_gmail",
            import_batch_id="batch-gmail-001",
        )
        result = register_source_document(
            self.con,
            unknown,
        )

        row = self.con.execute(
            """
            SELECT template_match_status, parser_version
            FROM registry_source_documents
            WHERE source_document_id=?
            """,
            (result.source_document_id,),
        ).fetchone()

        self.assertEqual(row["template_match_status"], "UNKNOWN_TEMPLATE")
        self.assertEqual(row["parser_version"], "not-run")

    def test_16_document_template_claim_cannot_contradict_registry(self):
        self._seed_bca()
        self._create_batch()

        false_known = self._document(
            "doc-false-known",
            sha("0"),
            "bca:false-known:2026-08",
            template_status=TemplateMatchStatus.KNOWN,
            fingerprint="changed-layout",
        )

        with self.assertRaises(RegistryConflictError):
            register_source_document(
                self.con,
                false_known,
            )

    def test_15_occurrence_replay_is_idempotent(self):
        self._seed_bca()
        self._create_batch()
        doc = self._document(
            "doc-001",
            sha("a"),
            "bca:main:2026-07",
        )
        register_source_document(self.con, doc)

        first = record_source_document_occurrence(
            self.con,
            source_document_id="doc-001",
            import_batch_id="batch-001",
            source_locator="archive/member.pdf",
            parent_archive_sha256=sha("b"),
            archive_depth=1,
            member_path="member.pdf",
        )
        second = record_source_document_occurrence(
            self.con,
            source_document_id="doc-001",
            import_batch_id="batch-001",
            source_locator="archive/member.pdf",
            parent_archive_sha256=sha("b"),
            archive_depth=1,
            member_path="member.pdf",
        )

        self.assertEqual(
            first["occurrence_id"],
            second["occurrence_id"],
        )

    def test_16_occurrence_locator_conflict_fails_closed(self):
        self._seed_bca()
        self._create_batch()

        for document_id, digest in (
            ("doc-001", sha("a")),
            ("doc-002", sha("b")),
        ):
            register_source_document(
                self.con,
                self._document(
                    document_id,
                    digest,
                    f"key:{document_id}",
                ),
            )

        record_source_document_occurrence(
            self.con,
            source_document_id="doc-001",
            import_batch_id="batch-001",
            source_locator="same-locator",
        )

        with self.assertRaises(RegistryConflictError):
            record_source_document_occurrence(
                self.con,
                source_document_id="doc-002",
                import_batch_id="batch-001",
                source_locator="same-locator",
            )

    def test_17_occurrence_replay_with_changed_metadata_fails_closed(self):
        self._seed_bca()
        self._create_batch()
        register_source_document(
            self.con,
            self._document(
                "doc-001",
                sha("a"),
                "bca:main:2026-07",
            ),
        )
        record_source_document_occurrence(
            self.con,
            source_document_id="doc-001",
            import_batch_id="batch-001",
            source_locator="archive/member.pdf",
            parent_archive_sha256=sha("b"),
            archive_depth=1,
            member_path="member.pdf",
        )

        with self.assertRaises(RegistryConflictError):
            record_source_document_occurrence(
                self.con,
                source_document_id="doc-001",
                import_batch_id="batch-001",
                source_locator="archive/member.pdf",
                parent_archive_sha256=sha("c"),
                archive_depth=1,
                member_path="member.pdf",
            )

    def test_18_legacy_bridge_is_explicit_and_does_not_modify_legacy_account(self):
        self._seed_bca()
        self.con.execute(
            """
            CREATE TABLE accounts (
                name TEXT PRIMARY KEY,
                current_balance REAL NOT NULL,
                active INTEGER NOT NULL
            )
            """
        )
        self.con.execute(
            """
            INSERT INTO accounts
            VALUES ('BCA Main', 1234567.89, 1)
            """
        )
        self.con.execute(
            """
            INSERT INTO registry_accounts (
                account_id, institution_id, display_name,
                account_type, ownership_state, provider_account_key
            ) VALUES (
                'acct-bca-main', 'bca', 'Main',
                'TRANSACTIONAL', 'OWNED', 'main'
            )
            """
        )

        before = self.con.execute(
            """
            SELECT current_balance, active
            FROM accounts
            WHERE name='BCA Main'
            """
        ).fetchone()

        link_legacy_account_name(
            self.con,
            account_id="acct-bca-main",
            legacy_account_name="BCA Main",
            valid_from="2025-01-01",
        )

        after = self.con.execute(
            """
            SELECT current_balance, active
            FROM accounts
            WHERE name='BCA Main'
            """
        ).fetchone()

        self.assertEqual(tuple(before), tuple(after))
        self.assertEqual(
            resolve_legacy_account_name(
                self.con,
                legacy_account_name="BCA Main",
            ),
            "acct-bca-main",
        )

    def test_18_historical_legacy_name_reuse_resolves_by_date(self):
        self._seed_bca()
        self.con.execute(
            """
            CREATE TABLE accounts (
                name TEXT PRIMARY KEY,
                current_balance REAL,
                active INTEGER
            )
            """
        )
        self.con.execute(
            """
            INSERT INTO accounts
            VALUES ('BCA Poket: Reused', 0, 1)
            """
        )
        self.con.executemany(
            """
            INSERT INTO registry_accounts (
                account_id, institution_id, display_name,
                account_type, ownership_state, provider_account_key, active
            ) VALUES (?, 'bca', ?, 'SUBACCOUNT', 'OWNED', ?, ?)
            """,
            [
                ('acct-old', 'Old', 'slot-old', 0),
                ('acct-new', 'New', 'slot-new', 1),
            ],
        )
        link_legacy_account_name(
            self.con,
            account_id="acct-old",
            legacy_account_name="BCA Poket: Reused",
            valid_from="2025-01-01",
            valid_to="2025-12-31",
            active=False,
        )
        link_legacy_account_name(
            self.con,
            account_id="acct-new",
            legacy_account_name="BCA Poket: Reused",
            valid_from="2026-01-01",
            active=True,
        )

        self.assertEqual(
            resolve_legacy_account_name(
                self.con,
                legacy_account_name="BCA Poket: Reused",
                as_of_date="2025-06-01",
            ),
            "acct-old",
        )
        self.assertEqual(
            resolve_legacy_account_name(
                self.con,
                legacy_account_name="BCA Poket: Reused",
                as_of_date="2026-06-01",
            ),
            "acct-new",
        )

    def test_19_legacy_link_rejects_name_not_present_in_legacy_accounts(self):
        self._seed_bca()
        self.con.execute(
            """
            CREATE TABLE accounts (
                name TEXT PRIMARY KEY,
                current_balance REAL,
                active INTEGER
            )
            """
        )
        self.con.execute(
            """
            INSERT INTO registry_accounts (
                account_id, institution_id, display_name,
                account_type, ownership_state
            ) VALUES (
                'acct-bca-main', 'bca', 'Main',
                'TRANSACTIONAL', 'OWNED'
            )
            """
        )

        with self.assertRaises(RegistryConflictError):
            link_legacy_account_name(
                self.con,
                account_id="acct-bca-main",
                legacy_account_name="Does Not Exist",
            )

    def test_20_account_observation_resolves_by_provider_key_not_display_name(self):
        self._seed_bca()
        self._create_batch()
        register_source_document(
            self.con,
            self._document(
                "doc-observation",
                sha("a"),
                "bca:obs:2026-07",
            ),
        )
        self.con.execute(
            """
            INSERT INTO registry_accounts (
                account_id, institution_id, display_name,
                account_type, ownership_state, provider_account_key
            ) VALUES (
                'acct-bca-pocket', 'bca', 'Shared Display',
                'SUBACCOUNT', 'OWNED', 'slot-77'
            )
            """
        )
        cur = self.con.execute(
            """
            INSERT INTO registry_account_observations (
                source_document_id, institution_id,
                observed_account_key, display_name_raw,
                ownership_state, lifecycle_state, lifecycle_evidence
            ) VALUES (
                'doc-observation', 'bca',
                'slot-77', 'Some Other Display',
                'OWNED', 'UNCHANGED', 'provider slot evidence'
            )
            """
        )

        result = resolve_account_observation(
            self.con,
            cur.lastrowid,
        )

        self.assertEqual(
            result.status,
            "RESOLVED_PROVIDER_KEY",
        )
        self.assertEqual(
            result.account_id,
            "acct-bca-pocket",
        )

    def test_21_display_name_alone_never_auto_resolves_observation(self):
        self._seed_bca()
        self._create_batch()
        register_source_document(
            self.con,
            self._document(
                "doc-observation",
                sha("a"),
                "bca:obs:2026-07",
            ),
        )
        self.con.execute(
            """
            INSERT INTO registry_accounts (
                account_id, institution_id, display_name,
                account_type, ownership_state, provider_account_key
            ) VALUES (
                'acct-bca-pocket', 'bca', 'Same Name',
                'SUBACCOUNT', 'OWNED', 'real-slot'
            )
            """
        )
        cur = self.con.execute(
            """
            INSERT INTO registry_account_observations (
                source_document_id, institution_id,
                observed_account_key, display_name_raw,
                ownership_state, lifecycle_state, lifecycle_evidence
            ) VALUES (
                'doc-observation', 'bca',
                'unknown-slot', 'Same Name',
                'UNKNOWN', 'UNVERIFIED', 'display only'
            )
            """
        )

        result = resolve_account_observation(
            self.con,
            cur.lastrowid,
        )

        self.assertEqual(result.status, "UNRESOLVED")
        row = self.con.execute(
            """
            SELECT resolved_account_id
            FROM registry_account_observations
            WHERE observation_id=?
            """,
            (cur.lastrowid,),
        ).fetchone()
        self.assertIsNone(row["resolved_account_id"])

    def test_22_parent_observed_key_can_resolve_hierarchy(self):
        self._seed_bca()
        self._create_batch()
        register_source_document(
            self.con,
            self._document(
                "doc-observation",
                sha("a"),
                "bca:obs:2026-07",
            ),
        )
        self.con.executemany(
            """
            INSERT INTO registry_accounts (
                account_id, institution_id, display_name,
                account_type, ownership_state,
                parent_account_id, provider_account_key
            ) VALUES (?, 'bca', ?, ?, 'OWNED', ?, ?)
            """,
            [
                (
                    'acct-main', 'Main', 'TRANSACTIONAL',
                    None, 'main-slot'
                ),
                (
                    'acct-child', 'Child', 'SUBACCOUNT',
                    'acct-main', 'child-slot'
                ),
            ],
        )
        cur = self.con.execute(
            """
            INSERT INTO registry_account_observations (
                source_document_id, institution_id,
                observed_account_key, display_name_raw,
                ownership_state, lifecycle_state, lifecycle_evidence,
                parent_observed_account_key
            ) VALUES (
                'doc-observation', 'bca',
                'child-slot', 'Child',
                'OWNED', 'UNCHANGED', 'hierarchy evidence',
                'main-slot'
            )
            """
        )

        result = resolve_account_observation(
            self.con,
            cur.lastrowid,
        )

        self.assertEqual(result.account_id, "acct-child")
        self.assertEqual(
            result.parent_account_id,
            "acct-main",
        )

    def test_23_observed_parent_cannot_contradict_registered_hierarchy(self):
        self._seed_bca()
        self._create_batch()
        register_source_document(
            self.con,
            self._document(
                "doc-observation-conflict",
                sha("a"),
                "bca:obs:conflict",
            ),
        )
        self.con.executemany(
            """
            INSERT INTO registry_accounts (
                account_id, institution_id, display_name,
                account_type, ownership_state,
                parent_account_id, provider_account_key
            ) VALUES (?, 'bca', ?, ?, 'OWNED', ?, ?)
            """,
            [
                ('acct-parent-a', 'Parent A', 'TRANSACTIONAL', None, 'parent-a'),
                ('acct-parent-b', 'Parent B', 'TRANSACTIONAL', None, 'parent-b'),
                ('acct-child', 'Child', 'SUBACCOUNT', 'acct-parent-a', 'child'),
            ],
        )
        cur = self.con.execute(
            """
            INSERT INTO registry_account_observations (
                source_document_id, institution_id,
                observed_account_key, display_name_raw,
                ownership_state, lifecycle_state, lifecycle_evidence,
                parent_observed_account_key
            ) VALUES (
                'doc-observation-conflict', 'bca',
                'child', 'Child',
                'OWNED', 'UNCHANGED', 'contradictory hierarchy evidence',
                'parent-b'
            )
            """
        )

        with self.assertRaises(RegistryConflictError):
            resolve_account_observation(
                self.con,
                cur.lastrowid,
            )

    def test_24_bridge_audit_is_readonly(self):

        self._seed_bca()
        self.con.execute(
            """
            CREATE TABLE accounts (
                name TEXT PRIMARY KEY,
                current_balance REAL,
                active INTEGER
            )
            """
        )
        self.con.execute(
            """
            INSERT INTO accounts
            VALUES ('BCA Main', 1000000.0, 1)
            """
        )
        self.con.execute(
            """
            INSERT INTO registry_accounts (
                account_id, institution_id, display_name,
                account_type, ownership_state
            ) VALUES (
                'acct-bca-main', 'bca', 'Main',
                'TRANSACTIONAL', 'OWNED'
            )
            """
        )
        link_legacy_account_name(
            self.con,
            account_id="acct-bca-main",
            legacy_account_name="BCA Main",
        )

        before = self.con.execute(
            "SELECT * FROM accounts"
        ).fetchall()

        audit = validate_legacy_bridge_readonly(
            self.con,
        )

        after = self.con.execute(
            "SELECT * FROM accounts"
        ).fetchall()

        self.assertEqual(
            [tuple(row) for row in before],
            [tuple(row) for row in after],
        )
        self.assertEqual(audit["missing_legacy_names"], [])
        self.assertEqual(
            audit["dangling_registry_accounts"],
            [],
        )

    def test_25_bridge_audit_reports_absent_legacy_table_without_creating_it(self):
        self._seed_bca()

        audit = validate_legacy_bridge_readonly(
            self.con,
        )

        self.assertEqual(
            audit["missing_legacy_table"],
            ["accounts"],
        )
        exists = self.con.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type='table' AND name='accounts'
            """
        ).fetchone()
        self.assertIsNone(exists)

    def test_26_registry_service_never_mutates_transactions_or_account_balances(self):
        self._seed_bca()
        self.con.execute(
            """
            CREATE TABLE accounts (
                name TEXT PRIMARY KEY,
                current_balance REAL,
                active INTEGER
            )
            """
        )
        self.con.execute(
            """
            CREATE TABLE transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                amount REAL
            )
            """
        )
        self.con.execute(
            """
            INSERT INTO accounts
            VALUES ('BCA Main', 7654321.0, 1)
            """
        )
        self.con.execute(
            "INSERT INTO transactions(amount) VALUES (50000.0)"
        )
        self.con.execute(
            """
            INSERT INTO registry_accounts (
                account_id, institution_id, display_name,
                account_type, ownership_state
            ) VALUES (
                'acct-bca-main', 'bca', 'Main',
                'TRANSACTIONAL', 'OWNED'
            )
            """
        )

        tx_before = self.con.execute(
            "SELECT COUNT(*), SUM(amount) FROM transactions"
        ).fetchone()
        balance_before = self.con.execute(
            """
            SELECT current_balance
            FROM accounts
            WHERE name='BCA Main'
            """
        ).fetchone()[0]

        link_legacy_account_name(
            self.con,
            account_id="acct-bca-main",
            legacy_account_name="BCA Main",
        )
        get_source_capability(
            self.con,
            "bca_statement",
        )
        resolve_template(
            self.con,
            source_registry_id="bca_statement",
            template_fingerprint="bca-fingerprint-v1",
        )

        tx_after = self.con.execute(
            "SELECT COUNT(*), SUM(amount) FROM transactions"
        ).fetchone()
        balance_after = self.con.execute(
            """
            SELECT current_balance
            FROM accounts
            WHERE name='BCA Main'
            """
        ).fetchone()[0]

        self.assertEqual(tuple(tx_before), tuple(tx_after))
        self.assertEqual(balance_before, balance_after)


if __name__ == "__main__":
    unittest.main()
