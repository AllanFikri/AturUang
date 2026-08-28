import sqlite3
import unittest

from aturuang.ingestion_registry import (
    REGISTRY_TABLES,
    drop_registry_schema,
    init_registry_schema,
    registry_table_names,
)


def sha(char: str) -> str:
    return char * 64


class TestUniversalIngestionPhase1RegistrySchema(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys = ON")

    def tearDown(self):
        self.con.close()

    def _seed_bca_source(self):
        self.con.execute(
            """
            INSERT INTO registry_institutions (
                institution_id, display_name, institution_type
            ) VALUES ('bca', 'BCA', 'BANK')
            """
        )
        self.con.execute(
            """
            INSERT INTO registry_sources (
                source_registry_id, provider_key, display_name,
                channel, institution_id
            ) VALUES (
                'bca_statement', 'bca', 'BCA Monthly Statement',
                'PDF', 'bca'
            )
            """
        )
        self.con.execute(
            """
            INSERT INTO registry_import_batches (
                import_batch_id, source_registry_id, mode,
                idempotency_key
            ) VALUES (
                'batch-bca-001', 'bca_statement', 'DRY_RUN',
                'bca-set-001'
            )
            """
        )

    def test_01_schema_is_idempotent_and_complete(self):
        init_registry_schema(self.con)
        init_registry_schema(self.con)

        tables = registry_table_names(self.con)

        self.assertEqual(tables, set(REGISTRY_TABLES))
        self.assertNotIn("transactions", tables)
        self.assertNotIn("accounts", tables)
        self.assertNotIn("import_batches", tables)

    def test_02_schema_needs_no_legacy_tables(self):
        init_registry_schema(self.con)

        all_tables = {
            row[0]
            for row in self.con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        self.assertNotIn("transactions", all_tables)
        self.assertNotIn("accounts", all_tables)
        self.assertNotIn("raw_import_events", all_tables)
        self.assertNotIn("import_candidates", all_tables)

    def test_03_source_registry_and_capabilities_support_statement_and_gmail(self):
        init_registry_schema(self.con)
        self.con.execute(
            """
            INSERT INTO registry_institutions
            VALUES ('bca', 'BCA', 'BANK', 1, CURRENT_TIMESTAMP)
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
                has_transactions, has_balances, has_native_event_id,
                near_realtime
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    'bca_statement', 'monthly_statement', 'COMPLETE',
                    1, 1, 0, 0
                ),
                (
                    'bca_gmail', 'transaction_notification', 'NEAR_REALTIME',
                    1, 0, 1, 1
                ),
            ],
        )

        gmail = self.con.execute(
            """
            SELECT *
            FROM registry_source_capabilities
            WHERE source_registry_id='bca_gmail'
            """
        ).fetchone()

        self.assertEqual(gmail["near_realtime"], 1)
        self.assertEqual(gmail["has_balances"], 0)

    def test_04_display_name_collision_is_allowed_across_stable_ids(self):
        init_registry_schema(self.con)
        self.con.executemany(
            """
            INSERT INTO registry_institutions (
                institution_id, display_name, institution_type
            ) VALUES (?, ?, ?)
            """,
            [
                ('jago', 'Bank Jago', 'BANK'),
                ('stockbit', 'Stockbit', 'BROKER'),
            ],
        )
        self.con.executemany(
            """
            INSERT INTO registry_accounts (
                account_id, institution_id, display_name,
                account_type, ownership_state
            ) VALUES (?, ?, 'Stockbit', ?, 'OWNED')
            """,
            [
                ('acct-jago-pocket', 'jago', 'SUBACCOUNT'),
                ('acct-stockbit-rdn', 'stockbit', 'RDN'),
            ],
        )

        count = self.con.execute(
            """
            SELECT COUNT(*)
            FROM registry_accounts
            WHERE display_name='Stockbit'
            """
        ).fetchone()[0]

        self.assertEqual(count, 2)

    def test_05_parent_account_must_share_institution(self):
        init_registry_schema(self.con)
        self.con.executemany(
            """
            INSERT INTO registry_institutions (
                institution_id, display_name, institution_type
            ) VALUES (?, ?, 'BANK')
            """,
            [
                ('bca', 'BCA'),
                ('jago', 'Bank Jago'),
            ],
        )
        self.con.execute(
            """
            INSERT INTO registry_accounts (
                account_id, institution_id, display_name,
                account_type, ownership_state
            ) VALUES (
                'acct-bca-main', 'bca', 'BCA Main',
                'TRANSACTIONAL', 'OWNED'
            )
            """
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                """
                INSERT INTO registry_accounts (
                    account_id, institution_id, display_name,
                    account_type, ownership_state, parent_account_id
                ) VALUES (
                    'acct-jago-child', 'jago', 'Wrong Parent',
                    'SUBACCOUNT', 'OWNED', 'acct-bca-main'
                )
                """
            )

    def test_06_account_hierarchy_cycle_is_rejected(self):
        init_registry_schema(self.con)
        self.con.execute(
            """
            INSERT INTO registry_institutions (
                institution_id, display_name, institution_type
            ) VALUES ('jago', 'Bank Jago', 'BANK')
            """
        )
        self.con.execute(
            """
            INSERT INTO registry_accounts (
                account_id, institution_id, display_name,
                account_type, ownership_state
            ) VALUES (
                'acct-a', 'jago', 'A', 'SUBACCOUNT', 'OWNED'
            )
            """
        )
        self.con.execute(
            """
            INSERT INTO registry_accounts (
                account_id, institution_id, display_name,
                account_type, ownership_state, parent_account_id
            ) VALUES (
                'acct-b', 'jago', 'B', 'SUBACCOUNT', 'OWNED', 'acct-a'
            )
            """
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                """
                UPDATE registry_accounts
                SET parent_account_id='acct-b'
                WHERE account_id='acct-a'
                """
            )

    def test_07_active_provider_slot_can_be_reused_only_after_close(self):
        init_registry_schema(self.con)
        self.con.execute(
            """
            INSERT INTO registry_institutions (
                institution_id, display_name, institution_type
            ) VALUES ('bca', 'BCA', 'BANK')
            """
        )
        self.con.execute(
            """
            INSERT INTO registry_accounts (
                account_id, institution_id, display_name,
                account_type, ownership_state, provider_account_key
            ) VALUES (
                'acct-old', 'bca', 'Uang Riset',
                'SUBACCOUNT', 'OWNED', 'poket-slot-03'
            )
            """
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                """
                INSERT INTO registry_accounts (
                    account_id, institution_id, display_name,
                    account_type, ownership_state, provider_account_key
                ) VALUES (
                    'acct-new-too-early', 'bca', 'Beli Charger',
                    'SUBACCOUNT', 'OWNED', 'poket-slot-03'
                )
                """
            )

        self.con.execute(
            "UPDATE registry_accounts SET active=0 WHERE account_id='acct-old'"
        )
        self.con.execute(
            """
            INSERT INTO registry_accounts (
                account_id, institution_id, display_name,
                account_type, ownership_state, provider_account_key
            ) VALUES (
                'acct-new', 'bca', 'Beli Charger',
                'SUBACCOUNT', 'OWNED', 'poket-slot-03'
            )
            """
        )

        self.assertIsNotNone(
            self.con.execute(
                "SELECT 1 FROM registry_accounts WHERE account_id='acct-new'"
            ).fetchone()
        )

    def test_08_legacy_account_bridge_allows_historical_name_reuse(self):
        init_registry_schema(self.con)
        self.con.execute(
            """
            INSERT INTO registry_institutions (
                institution_id, display_name, institution_type
            ) VALUES ('bca', 'BCA', 'BANK')
            """
        )
        self.con.executemany(
            """
            INSERT INTO registry_accounts (
                account_id, institution_id, display_name,
                account_type, ownership_state, active
            ) VALUES (?, 'bca', ?, 'SUBACCOUNT', 'OWNED', ?)
            """,
            [
                ('acct-old', 'Old Poket', 0),
                ('acct-new', 'New Poket', 1),
            ],
        )
        self.con.execute(
            """
            INSERT INTO registry_legacy_account_links (
                account_id, legacy_account_name, valid_from,
                valid_to, active
            ) VALUES (
                'acct-old', 'BCA Poket: Reused Name',
                '2025-01-01', '2025-12-31', 0
            )
            """
        )
        self.con.execute(
            """
            INSERT INTO registry_legacy_account_links (
                account_id, legacy_account_name, valid_from, active
            ) VALUES (
                'acct-new', 'BCA Poket: Reused Name',
                '2026-01-01', 1
            )
            """
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                """
                INSERT INTO registry_legacy_account_links (
                    account_id, legacy_account_name, valid_from, active
                ) VALUES (
                    'acct-old', 'BCA Poket: Reused Name',
                    '2026-01-02', 1
                )
                """
            )

        count = self.con.execute(
            """
            SELECT COUNT(*)
            FROM registry_legacy_account_links
            WHERE legacy_account_name='BCA Poket: Reused Name'
            """
        ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_09_import_batch_idempotency_is_scoped_to_source(self):
        init_registry_schema(self.con)
        self._seed_bca_source()

        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                """
                INSERT INTO registry_import_batches (
                    import_batch_id, source_registry_id, mode,
                    idempotency_key
                ) VALUES (
                    'batch-bca-002', 'bca_statement', 'STAGING',
                    'bca-set-001'
                )
                """
            )

    def test_10_document_source_must_match_import_batch_source(self):
        init_registry_schema(self.con)
        self.con.executemany(
            """
            INSERT INTO registry_institutions (
                institution_id, display_name, institution_type
            ) VALUES (?, ?, 'BANK')
            """,
            [
                ('bca', 'BCA'),
                ('jago', 'Bank Jago'),
            ],
        )
        self.con.executemany(
            """
            INSERT INTO registry_sources (
                source_registry_id, provider_key, display_name,
                channel, institution_id
            ) VALUES (?, ?, ?, 'PDF', ?)
            """,
            [
                ('bca_statement', 'bca', 'BCA Statement', 'bca'),
                ('jago_statement', 'jago', 'Jago Statement', 'jago'),
            ],
        )
        self.con.execute(
            """
            INSERT INTO registry_import_batches (
                import_batch_id, source_registry_id, mode, idempotency_key
            ) VALUES (
                'batch-bca', 'bca_statement', 'DRY_RUN', 'batch-key'
            )
            """
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                """
                INSERT INTO registry_source_documents (
                    source_document_id, source_registry_id, import_batch_id,
                    content_sha256, natural_document_key,
                    template_id, template_fingerprint, parser_version,
                    template_match_status, period_status
                ) VALUES (
                    'doc-wrong-source', 'jago_statement', 'batch-bca',
                    ?, 'jago:2026-07',
                    'jago_monthly_statement_v1', 'fp1', 'not-implemented',
                    'KNOWN', 'CLOSED'
                )
                """,
                (sha("a"),),
            )

    def test_11_exact_content_sha_is_global_unique(self):
        init_registry_schema(self.con)
        self._seed_bca_source()

        self.con.execute(
            """
            INSERT INTO registry_source_documents (
                source_document_id, source_registry_id, import_batch_id,
                content_sha256, natural_document_key,
                template_id, template_fingerprint, parser_version,
                template_match_status, period_status
            ) VALUES (
                'doc-1', 'bca_statement', 'batch-bca-001',
                ?, 'bca:main:2026-07',
                'bca_monthly_statement_v1', 'fp1', 'not-implemented',
                'KNOWN', 'CLOSED'
            )
            """,
            (sha("a"),),
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                """
                INSERT INTO registry_source_documents (
                    source_document_id, source_registry_id, import_batch_id,
                    content_sha256, natural_document_key,
                    template_id, template_fingerprint, parser_version,
                    template_match_status, period_status
                ) VALUES (
                    'doc-2', 'bca_statement', 'batch-bca-001',
                    ?, 'other-key',
                    'bca_monthly_statement_v1', 'fp1', 'not-implemented',
                    'KNOWN', 'CLOSED'
                )
                """,
                (sha("a"),),
            )

    def test_09_same_natural_key_different_sha_can_record_semantic_duplicate(self):
        init_registry_schema(self.con)
        self._seed_bca_source()

        self.con.execute(
            """
            INSERT INTO registry_source_documents (
                source_document_id, source_registry_id, import_batch_id,
                content_sha256, natural_document_key, semantic_sha256,
                template_id, template_fingerprint, parser_version,
                template_match_status, period_status
            ) VALUES (
                'doc-original', 'bca_statement', 'batch-bca-001',
                ?, 'bca:main:2026-07', ?,
                'bca_monthly_statement_v1', 'fp1', 'not-implemented',
                'KNOWN', 'CLOSED'
            )
            """,
            (sha("a"), sha("b")),
        )
        self.con.execute(
            """
            INSERT INTO registry_source_documents (
                source_document_id, source_registry_id, import_batch_id,
                content_sha256, natural_document_key, semantic_sha256,
                template_id, template_fingerprint, parser_version,
                template_match_status, period_status,
                identity_status, related_document_id
            ) VALUES (
                'doc-reexport', 'bca_statement', 'batch-bca-001',
                ?, 'bca:main:2026-07', ?,
                'bca_monthly_statement_v1', 'fp1', 'not-implemented',
                'KNOWN', 'CLOSED',
                'SEMANTIC_DUPLICATE', 'doc-original'
            )
            """,
            (sha("c"), sha("b")),
        )

        count = self.con.execute(
            """
            SELECT COUNT(*)
            FROM registry_source_documents
            WHERE natural_document_key='bca:main:2026-07'
            """
        ).fetchone()[0]

        self.assertEqual(count, 2)

    def test_10_unknown_template_fails_closed_without_parser_execution(self):
        init_registry_schema(self.con)
        self._seed_bca_source()

        self.con.execute(
            """
            INSERT INTO registry_source_documents (
                source_document_id, source_registry_id, import_batch_id,
                content_sha256, natural_document_key,
                template_id, template_fingerprint, parser_version,
                template_match_status, period_status,
                identity_status
            ) VALUES (
                'doc-unknown', 'bca_statement', 'batch-bca-001',
                ?, 'bca:main:2026-08:unknown',
                'UNKNOWN', 'unrecognized-fingerprint', 'not-run',
                'UNKNOWN_TEMPLATE', 'OPEN',
                'AMBIGUOUS'
            )
            """,
            (sha("d"),),
        )

        row = self.con.execute(
            """
            SELECT template_match_status, parser_version
            FROM registry_source_documents
            WHERE source_document_id='doc-unknown'
            """
        ).fetchone()

        self.assertEqual(row["template_match_status"], "UNKNOWN_TEMPLATE")
        self.assertEqual(row["parser_version"], "not-run")

    def test_11_open_and_partial_periods_are_representable(self):
        init_registry_schema(self.con)
        self._seed_bca_source()

        for document_id, digest, status in (
            ('doc-open', sha("e"), 'OPEN'),
            ('doc-partial', sha("f"), 'PARTIAL'),
        ):
            self.con.execute(
                """
                INSERT INTO registry_source_documents (
                    source_document_id, source_registry_id, import_batch_id,
                    content_sha256, natural_document_key,
                    template_id, template_fingerprint, parser_version,
                    template_match_status, period_status
                ) VALUES (?, 'bca_statement', 'batch-bca-001', ?, ?,
                    'bca_monthly_statement_v1', 'fp1', 'not-implemented',
                    'KNOWN', ?)
                """,
                (document_id, digest, f"key:{document_id}", status),
            )

        statuses = {
            row[0]
            for row in self.con.execute(
                "SELECT period_status FROM registry_source_documents"
            ).fetchall()
        }

        self.assertEqual(statuses, {"OPEN", "PARTIAL"})

    def test_12_occurrences_preserve_repeated_archive_lineage(self):
        init_registry_schema(self.con)
        self._seed_bca_source()
        self.con.execute(
            """
            INSERT INTO registry_source_documents (
                source_document_id, source_registry_id, import_batch_id,
                content_sha256, natural_document_key,
                template_id, template_fingerprint, parser_version,
                template_match_status, period_status
            ) VALUES (
                'doc-1', 'bca_statement', 'batch-bca-001',
                ?, 'bca:main:2026-07',
                'bca_monthly_statement_v1', 'fp1', 'not-implemented',
                'KNOWN', 'CLOSED'
            )
            """,
            (sha("a"),),
        )
        self.con.execute(
            """
            INSERT INTO registry_source_document_occurrences (
                source_document_id, import_batch_id, source_locator,
                parent_archive_sha256, archive_depth, member_path
            ) VALUES (
                'doc-1', 'batch-bca-001', 'archive-A/member-1',
                ?, 1, 'BCA/statement.pdf'
            )
            """,
            (sha("b"),),
        )
        self.con.execute(
            """
            INSERT INTO registry_source_document_occurrences (
                source_document_id, import_batch_id, source_locator,
                parent_archive_sha256, archive_depth, member_path
            ) VALUES (
                'doc-1', 'batch-bca-001', 'archive-B/member-1',
                ?, 2, 'nested/BCA/statement.pdf'
            )
            """,
            (sha("c"),),
        )

        count = self.con.execute(
            """
            SELECT COUNT(*)
            FROM registry_source_document_occurrences
            WHERE source_document_id='doc-1'
            """
        ).fetchone()[0]

        self.assertEqual(count, 2)

    def test_13_account_lifecycle_rename_and_reuse_have_source_evidence(self):
        init_registry_schema(self.con)
        self._seed_bca_source()
        self.con.execute(
            """
            INSERT INTO registry_source_documents (
                source_document_id, source_registry_id, import_batch_id,
                content_sha256, natural_document_key,
                template_id, template_fingerprint, parser_version,
                template_match_status, period_status
            ) VALUES (
                'doc-life', 'bca_statement', 'batch-bca-001',
                ?, 'bca:main:2026-07',
                'bca_monthly_statement_v1', 'fp1', 'not-implemented',
                'KNOWN', 'CLOSED'
            )
            """,
            (sha("a"),),
        )
        self.con.execute(
            """
            INSERT INTO registry_accounts (
                account_id, institution_id, display_name,
                account_type, ownership_state, provider_account_key, active
            ) VALUES (
                'acct-old', 'bca', 'Old',
                'SUBACCOUNT', 'OWNED', 'slot-1', 0
            )
            """
        )
        self.con.execute(
            """
            INSERT INTO registry_accounts (
                account_id, institution_id, display_name,
                account_type, ownership_state, provider_account_key, active
            ) VALUES (
                'acct-new', 'bca', 'New',
                'SUBACCOUNT', 'OWNED', 'slot-1', 1
            )
            """
        )
        self.con.execute(
            """
            INSERT INTO registry_account_lifecycle_events (
                account_id, event_type, effective_at,
                source_document_id, related_account_id
            ) VALUES (
                'acct-new', 'REUSED', '2026-07-01',
                'doc-life', 'acct-old'
            )
            """
        )

        event = self.con.execute(
            """
            SELECT event_type, source_document_id, related_account_id
            FROM registry_account_lifecycle_events
            """
        ).fetchone()

        self.assertEqual(event["event_type"], "REUSED")
        self.assertEqual(event["source_document_id"], "doc-life")
        self.assertEqual(event["related_account_id"], "acct-old")

    def test_17_reused_lifecycle_requires_same_provider_slot(self):
        init_registry_schema(self.con)
        self._seed_bca_source()
        self.con.execute(
            """
            INSERT INTO registry_source_documents (
                source_document_id, source_registry_id, import_batch_id,
                content_sha256, natural_document_key,
                template_id, template_fingerprint, parser_version,
                template_match_status, period_status
            ) VALUES (
                'doc-life-invalid', 'bca_statement', 'batch-bca-001',
                ?, 'bca:main:2026-07:invalid',
                'bca_monthly_statement_v1', 'fp1', 'not-implemented',
                'KNOWN', 'CLOSED'
            )
            """,
            (sha("b"),),
        )
        self.con.execute(
            """
            INSERT INTO registry_accounts (
                account_id, institution_id, display_name,
                account_type, ownership_state, provider_account_key, active
            ) VALUES (
                'acct-old-invalid', 'bca', 'Old Invalid',
                'SUBACCOUNT', 'OWNED', 'slot-old', 0
            )
            """
        )
        self.con.execute(
            """
            INSERT INTO registry_accounts (
                account_id, institution_id, display_name,
                account_type, ownership_state, provider_account_key, active
            ) VALUES (
                'acct-new-invalid', 'bca', 'New Invalid',
                'SUBACCOUNT', 'OWNED', 'slot-new', 1
            )
            """
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                """
                INSERT INTO registry_account_lifecycle_events (
                    account_id, event_type, effective_at,
                    source_document_id, related_account_id
                ) VALUES (
                    'acct-new-invalid', 'REUSED', '2026-07-01',
                    'doc-life-invalid', 'acct-old-invalid'
                )
                """
            )

    def test_18_account_observation_can_remain_unresolved(self):
        init_registry_schema(self.con)
        self._seed_bca_source()
        self.con.execute(
            """
            INSERT INTO registry_source_documents (
                source_document_id, source_registry_id, import_batch_id,
                content_sha256, natural_document_key,
                template_id, template_fingerprint, parser_version,
                template_match_status, period_status
            ) VALUES (
                'doc-observe', 'bca_statement', 'batch-bca-001',
                ?, 'bca:observe:2026-07',
                'bca_monthly_statement_v1', 'fp1', 'not-implemented',
                'KNOWN', 'CLOSED'
            )
            """,
            (sha("a"),),
        )
        self.con.execute(
            """
            INSERT INTO registry_account_observations (
                source_document_id, institution_id,
                observed_account_key, display_name_raw,
                ownership_state, lifecycle_state, lifecycle_evidence,
                parent_observed_account_key
            ) VALUES (
                'doc-observe', 'bca',
                'observed-slot-x', 'Unknown Poket',
                'UNKNOWN', 'UNVERIFIED', 'Needs review',
                'observed-parent-x'
            )
            """
        )

        row = self.con.execute(
            """
            SELECT ownership_state, lifecycle_state,
                   parent_observed_account_key, resolved_account_id
            FROM registry_account_observations
            """
        ).fetchone()

        self.assertEqual(row["ownership_state"], "UNKNOWN")
        self.assertEqual(row["lifecycle_state"], "UNVERIFIED")
        self.assertEqual(
            row["parent_observed_account_key"],
            "observed-parent-x",
        )
        self.assertIsNone(row["resolved_account_id"])

    def test_15_commerce_third_party_direct_cannot_be_personal_expense(self):
        init_registry_schema(self.con)
        self.con.execute(
            """
            INSERT INTO registry_institutions (
                institution_id, display_name, institution_type
            ) VALUES ('shopee', 'Shopee', 'COMMERCE')
            """
        )
        self.con.execute(
            """
            INSERT INTO registry_sources (
                source_registry_id, provider_key, display_name,
                channel, institution_id
            ) VALUES (
                'shopee_orders', 'shopee', 'Shopee Orders',
                'PDF', 'shopee'
            )
            """
        )
        self.con.execute(
            """
            INSERT INTO registry_import_batches (
                import_batch_id, source_registry_id, mode, idempotency_key
            ) VALUES (
                'batch-shopee', 'shopee_orders', 'DRY_RUN', 'orders-1'
            )
            """
        )
        self.con.execute(
            """
            INSERT INTO registry_source_documents (
                source_document_id, source_registry_id, import_batch_id,
                content_sha256, natural_document_key,
                template_id, template_fingerprint, parser_version,
                template_match_status, period_status
            ) VALUES (
                'doc-order', 'shopee_orders', 'batch-shopee',
                ?, 'shopee:order:001',
                'shopee_order_receipt_v1', 'fp-order', 'not-implemented',
                'KNOWN', 'CLOSED'
            )
            """,
            (sha("a"),),
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                """
                INSERT INTO registry_commerce_orders (
                    source_registry_id, source_document_id, order_key,
                    commerce_actor, economic_owner, payer_responsibility,
                    payment_match_status, personal_spending_effect
                ) VALUES (
                    'shopee_orders', 'doc-order', 'order-001',
                    'user', 'THIRD_PARTY', 'THIRD_PARTY_DIRECT',
                    'MATCHED', 'PERSONAL_EXPENSE'
                )
                """
            )

    def test_16_self_reimbursable_requires_pass_through_receivable(self):
        init_registry_schema(self.con)
        self.con.execute(
            """
            INSERT INTO registry_institutions (
                institution_id, display_name, institution_type
            ) VALUES ('shopee', 'Shopee', 'COMMERCE')
            """
        )
        self.con.execute(
            """
            INSERT INTO registry_sources (
                source_registry_id, provider_key, display_name,
                channel, institution_id
            ) VALUES (
                'shopee_orders', 'shopee', 'Shopee Orders',
                'PDF', 'shopee'
            )
            """
        )
        self.con.execute(
            """
            INSERT INTO registry_import_batches (
                import_batch_id, source_registry_id, mode, idempotency_key
            ) VALUES (
                'batch-shopee', 'shopee_orders', 'DRY_RUN', 'orders-1'
            )
            """
        )
        self.con.execute(
            """
            INSERT INTO registry_source_documents (
                source_document_id, source_registry_id, import_batch_id,
                content_sha256, natural_document_key,
                template_id, template_fingerprint, parser_version,
                template_match_status, period_status
            ) VALUES (
                'doc-order', 'shopee_orders', 'batch-shopee',
                ?, 'shopee:order:001',
                'shopee_order_receipt_v1', 'fp-order', 'not-implemented',
                'KNOWN', 'CLOSED'
            )
            """,
            (sha("a"),),
        )
        self.con.execute(
            """
            INSERT INTO registry_commerce_orders (
                source_registry_id, source_document_id, order_key,
                commerce_actor, economic_owner, payer_responsibility,
                payment_match_status, personal_spending_effect
            ) VALUES (
                'shopee_orders', 'doc-order', 'order-001',
                'user', 'THIRD_PARTY', 'SELF_REIMBURSABLE',
                'MATCHED', 'PASS_THROUGH_RECEIVABLE'
            )
            """
        )

        row = self.con.execute(
            """
            SELECT personal_spending_effect
            FROM registry_commerce_orders
            WHERE order_key='order-001'
            """
        ).fetchone()

        self.assertEqual(
            row["personal_spending_effect"],
            "PASS_THROUGH_RECEIVABLE",
        )

    def test_17_mixed_commerce_order_supports_line_allocations(self):
        init_registry_schema(self.con)
        self.con.execute(
            """
            INSERT INTO registry_institutions (
                institution_id, display_name, institution_type
            ) VALUES ('shopee', 'Shopee', 'COMMERCE')
            """
        )
        self.con.execute(
            """
            INSERT INTO registry_sources (
                source_registry_id, provider_key, display_name,
                channel, institution_id
            ) VALUES (
                'shopee_orders', 'shopee', 'Shopee Orders',
                'PDF', 'shopee'
            )
            """
        )
        self.con.execute(
            """
            INSERT INTO registry_import_batches (
                import_batch_id, source_registry_id, mode, idempotency_key
            ) VALUES (
                'batch-shopee', 'shopee_orders', 'DRY_RUN', 'orders-1'
            )
            """
        )
        self.con.execute(
            """
            INSERT INTO registry_source_documents (
                source_document_id, source_registry_id, import_batch_id,
                content_sha256, natural_document_key,
                template_id, template_fingerprint, parser_version,
                template_match_status, period_status
            ) VALUES (
                'doc-order', 'shopee_orders', 'batch-shopee',
                ?, 'shopee:order:001',
                'shopee_order_receipt_v1', 'fp-order', 'not-implemented',
                'KNOWN', 'CLOSED'
            )
            """,
            (sha("a"),),
        )
        cur = self.con.execute(
            """
            INSERT INTO registry_commerce_orders (
                source_registry_id, source_document_id, order_key,
                commerce_actor, economic_owner, payer_responsibility,
                payment_match_status, personal_spending_effect
            ) VALUES (
                'shopee_orders', 'doc-order', 'order-001',
                'user', 'MIXED', 'SELF',
                'MATCHED', 'MIXED_ALLOCATION'
            )
            """
        )
        order_id = cur.lastrowid
        self.con.executemany(
            """
            INSERT INTO registry_commerce_line_allocations (
                commerce_order_id, line_key, economic_owner,
                payer_responsibility, payer_actor, economic_owner_actor
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (order_id, 'line-1', 'SELF', 'SELF', 'user', 'user'),
                (
                    order_id, 'line-2', 'THIRD_PARTY',
                    'SELF_REIMBURSABLE', 'user', 'friend'
                ),
            ],
        )

        count = self.con.execute(
            """
            SELECT COUNT(*)
            FROM registry_commerce_line_allocations
            WHERE commerce_order_id=?
            """,
            (order_id,),
        ).fetchone()[0]

        self.assertEqual(count, 2)

    def test_18_drop_registry_schema_is_reversible_and_preserves_unrelated_tables(self):
        self.con.execute(
            "CREATE TABLE unrelated_table (id INTEGER PRIMARY KEY)"
        )
        init_registry_schema(self.con)
        drop_registry_schema(self.con)

        self.assertEqual(registry_table_names(self.con), set())
        self.assertIsNotNone(
            self.con.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type='table' AND name='unrelated_table'
                """
            ).fetchone()
        )


if __name__ == "__main__":
    unittest.main()
