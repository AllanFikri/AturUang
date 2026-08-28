import unittest

from aturuang.ingestion_contracts import (
    AccountContract,
    AccountLifecycleEvent,
    AccountObservationContract,
    AccountType,
    ArchiveLineageContract,
    CommerceLineAllocation,
    CommerceOrderOwnershipContract,
    CompletenessRole,
    ConfidenceLevel,
    DocumentIdentityStatus,
    EconomicOwner,
    ImportBatchContract,
    ImportBatchMode,
    InstitutionContract,
    InstitutionType,
    LifecycleState,
    OwnershipState,
    PayerResponsibility,
    PaymentMatchStatus,
    PeriodStatus,
    PersonalSpendingEffect,
    ReferenceEvidenceContract,
    SourceCapabilityContract,
    SourceChannel,
    SourceDocumentIdentity,
    SourceProvenanceContract,
    SourceRegistryContract,
    SourceTemplateContract,
    TemplateMatchStatus,
    capability_index,
    classify_document_identity,
    match_template_fail_closed,
    validate_account_graph,
    validate_reuse_transition,
)


def sha(char: str) -> str:
    return char * 64


def document(
    *,
    document_id: str,
    content_sha: str,
    natural_key: str,
    semantic_sha: str | None = None,
    source_registry_id: str = "jago_statement",
    period_status: PeriodStatus = PeriodStatus.CLOSED,
) -> SourceDocumentIdentity:
    return SourceDocumentIdentity(
        source_document_id=document_id,
        content_sha256=content_sha,
        natural_document_key=natural_key,
        template_id="jago_monthly_statement_v1",
        template_fingerprint="jago-fingerprint-v1",
        parser_version="not-implemented",
        source_registry_id=source_registry_id,
        import_batch_id="batch-001",
        template_match_status=TemplateMatchStatus.KNOWN,
        period_status=period_status,
        semantic_sha256=semantic_sha,
    )


class TestUniversalIngestionPhase1Contracts(unittest.TestCase):
    def test_01_institution_source_and_owned_subaccount_hierarchy(self):
        institution = InstitutionContract("bca", "BCA", InstitutionType.BANK)
        source = SourceRegistryContract(
            "bca_statement",
            "bca",
            "BCA Monthly Statement",
            SourceChannel.PDF,
            institution_id=institution.institution_id,
        )
        main = AccountContract(
            account_id="acct_bca_main",
            institution_id="bca",
            display_name="BCA Main",
            account_type=AccountType.TRANSACTIONAL,
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
            provider_account_key="main",
            legacy_account_name="BCA Main",
        )
        pocket = AccountContract(
            account_id="acct_bca_poket_tabungan",
            institution_id="bca",
            display_name="Poket Tabungan",
            account_type=AccountType.SUBACCOUNT,
            ownership_state=OwnershipState.OWNED,
            ownership_confidence=ConfidenceLevel.HIGH,
            parent_account_id=main.account_id,
            provider_account_key="poket-slot-01",
            legacy_account_name="BCA Poket: Tabungan",
        )

        validate_account_graph([main, pocket])
        self.assertEqual(source.institution_id, institution.institution_id)
        self.assertEqual(pocket.parent_account_id, main.account_id)

    def test_02_rename_keeps_stable_account_identity_and_evidence(self):
        event = AccountLifecycleEvent(
            account_id="acct_jago_pocket_007",
            event_type=LifecycleState.RENAMED,
            effective_at="2026-04-01",
            source_document_id="doc-jago-2026-04",
            previous_alias="Buat Jajan",
            new_alias="Daily",
        )

        self.assertEqual(event.account_id, "acct_jago_pocket_007")
        self.assertEqual(event.source_document_id, "doc-jago-2026-04")
        self.assertNotEqual(event.previous_alias, event.new_alias)

    def test_03_reused_provider_slot_creates_new_identity(self):
        old = AccountContract(
            account_id="acct_bca_poket_old",
            institution_id="bca",
            display_name="Uang Riset",
            account_type=AccountType.SUBACCOUNT,
            ownership_state=OwnershipState.OWNED,
            provider_account_key="poket-slot-03",
            active=False,
        )
        new = AccountContract(
            account_id="acct_bca_poket_new",
            institution_id="bca",
            display_name="Beli Charger",
            account_type=AccountType.SUBACCOUNT,
            ownership_state=OwnershipState.OWNED,
            provider_account_key="poket-slot-03",
            active=True,
        )
        event = AccountLifecycleEvent(
            account_id=new.account_id,
            event_type=LifecycleState.REUSED,
            effective_at="2026-08-01",
            source_document_id="doc-bca-2026-08",
            related_account_id=old.account_id,
        )

        validate_reuse_transition(old, new, event)
        self.assertNotEqual(old.account_id, new.account_id)
        self.assertEqual(old.provider_account_key, new.provider_account_key)

    def test_04_account_graph_rejects_cycle(self):
        a = AccountContract(
            "a",
            "jago",
            "A",
            AccountType.SUBACCOUNT,
            OwnershipState.OWNED,
            parent_account_id="b",
        )
        b = AccountContract(
            "b",
            "jago",
            "B",
            AccountType.SUBACCOUNT,
            OwnershipState.OWNED,
            parent_account_id="a",
        )

        with self.assertRaises(ValueError):
            validate_account_graph([a, b])

    def test_05_account_observation_preserves_source_evidence(self):
        observation = AccountObservationContract(
            source_document_id="doc-blu-portfolio-2026-07",
            institution_id="blu",
            observed_account_key="blu-saving-slot-2",
            display_name_raw="Dana Darurat",
            ownership_state=OwnershipState.OWNED,
            lifecycle_state=LifecycleState.NEW,
            lifecycle_evidence="Observed in portfolio hierarchy",
            parent_observed_account_key="blu-main-observed",
            parent_account_id="acct_blu_main",
        )

        self.assertEqual(
            observation.source_document_id,
            "doc-blu-portfolio-2026-07",
        )
        self.assertEqual(
            observation.parent_observed_account_key,
            "blu-main-observed",
        )
        self.assertEqual(observation.lifecycle_state, LifecycleState.NEW)

    def test_06_new_closed_and_unverified_lifecycle_states_are_representable(self):
        for state in (
            LifecycleState.NEW,
            LifecycleState.CLOSED,
            LifecycleState.UNVERIFIED,
        ):
            event = AccountLifecycleEvent(
                account_id="acct-jago-pocket",
                event_type=state,
                effective_at="2026-07-31",
                source_document_id="doc-jago-2026-07",
            )
            self.assertEqual(event.event_type, state)

    def test_07_source_capabilities_cover_corpus_and_near_realtime_email(self):
        capabilities = [
            SourceCapabilityContract(
                "bca_statement",
                "monthly_statement",
                CompletenessRole.COMPLETE,
                has_transactions=True,
                has_balances=True,
                has_running_balance=True,
                has_account_hierarchy=True,
                supports_reconciliation=True,
            ),
            SourceCapabilityContract(
                "bca_gmail",
                "transaction_notification",
                CompletenessRole.NEAR_REALTIME,
                has_transactions=True,
                has_native_event_id=True,
                near_realtime=True,
            ),
            SourceCapabilityContract(
                "jago_statement",
                "monthly_statement",
                CompletenessRole.COMPLETE,
                has_transactions=True,
                has_balances=True,
                has_account_hierarchy=True,
                supports_reconciliation=True,
            ),
            SourceCapabilityContract(
                "seabank_statement",
                "e_statement",
                CompletenessRole.COMPLETE,
                has_transactions=True,
                has_balances=True,
                supports_reconciliation=True,
            ),
            SourceCapabilityContract(
                "blu_mutation",
                "account_mutation",
                CompletenessRole.COMPLETE,
                has_transactions=True,
                has_balances=True,
                has_running_balance=True,
                supports_reconciliation=True,
            ),
            SourceCapabilityContract(
                "blu_portfolio",
                "portfolio",
                CompletenessRole.SNAPSHOT,
                has_balances=True,
                has_account_hierarchy=True,
                supports_reconciliation=True,
            ),
            SourceCapabilityContract(
                "gopay_statement",
                "e_statement",
                CompletenessRole.COMPLETE,
                has_transactions=True,
                has_balances=True,
            ),
            SourceCapabilityContract(
                "shopeepay_history",
                "transaction_history",
                CompletenessRole.PARTIAL,
                has_transactions=True,
            ),
            SourceCapabilityContract(
                "stockbit_soa",
                "statement_of_account",
                CompletenessRole.COMPLETE,
                has_transactions=True,
                has_balances=True,
                supports_reconciliation=True,
            ),
            SourceCapabilityContract(
                "shopee_orders",
                "order_receipt",
                CompletenessRole.ENRICHMENT,
                supports_commerce_enrichment=True,
            ),
        ]

        by_source = capability_index(capabilities)

        self.assertEqual(len(by_source), 10)
        self.assertTrue(by_source["bca_statement"].has_account_hierarchy)
        self.assertTrue(by_source["bca_gmail"].near_realtime)
        self.assertFalse(by_source["bca_gmail"].has_balances)
        self.assertTrue(by_source["blu_portfolio"].has_balances)
        self.assertFalse(by_source["blu_portfolio"].has_transactions)
        self.assertTrue(
            by_source["shopee_orders"].supports_commerce_enrichment
        )

    def test_08_template_contract_keeps_parser_version_and_fails_closed(self):
        templates = [
            SourceTemplateContract(
                template_id="bca_monthly_statement_v1",
                source_registry_id="bca_statement",
                template_version="1",
                parser_version="not-implemented",
                document_kind="monthly_statement",
                input_type=SourceChannel.PDF,
                template_fingerprint="fingerprint-v1",
                text_layer_required=True,
            )
        ]

        matched = match_template_fail_closed(
            templates,
            source_registry_id="bca_statement",
            template_fingerprint="fingerprint-v1",
        )
        self.assertIsNotNone(matched)
        self.assertEqual(matched.parser_version, "not-implemented")

        unknown = match_template_fail_closed(
            templates,
            source_registry_id="bca_statement",
            template_fingerprint="changed-layout-v2",
        )
        self.assertIsNone(unknown)

    def test_09_unknown_template_status_is_explicit_on_source_document(self):
        doc = SourceDocumentIdentity(
            source_document_id="doc-unknown",
            content_sha256=sha("a"),
            natural_document_key="bca:unknown:2026-08",
            template_id="UNKNOWN",
            template_fingerprint="unrecognized-layout",
            parser_version="not-run",
            source_registry_id="bca_statement",
            import_batch_id="batch-001",
            template_match_status=TemplateMatchStatus.UNKNOWN_TEMPLATE,
            period_status=PeriodStatus.OPEN,
        )

        self.assertEqual(
            doc.template_match_status,
            TemplateMatchStatus.UNKNOWN_TEMPLATE,
        )

    def test_10_document_identity_exact_semantic_revision_and_ambiguous(self):
        existing = document(
            document_id="doc-jago-apr-a",
            content_sha=sha("a"),
            natural_key="jago:acct-main:2026-04",
            semantic_sha=sha("b"),
        )

        exact = document(
            document_id="doc-exact",
            content_sha=sha("a"),
            natural_key="jago:acct-main:2026-04",
            semantic_sha=sha("b"),
        )
        self.assertEqual(
            classify_document_identity([existing], exact),
            DocumentIdentityStatus.EXACT_DUPLICATE,
        )

        semantic = document(
            document_id="doc-semantic",
            content_sha=sha("c"),
            natural_key="jago:acct-main:2026-04",
            semantic_sha=sha("b"),
        )
        self.assertEqual(
            classify_document_identity([existing], semantic),
            DocumentIdentityStatus.SEMANTIC_DUPLICATE,
        )

        revision = document(
            document_id="doc-revision",
            content_sha=sha("d"),
            natural_key="jago:acct-main:2026-04",
            semantic_sha=sha("e"),
        )
        self.assertEqual(
            classify_document_identity([existing], revision),
            DocumentIdentityStatus.REVISION_OR_CONFLICT,
        )

        no_semantic = document(
            document_id="doc-no-semantic",
            content_sha=sha("f"),
            natural_key="jago:acct-main:2026-05",
        )
        ambiguous = document(
            document_id="doc-ambiguous",
            content_sha=sha("0"),
            natural_key="jago:acct-main:2026-05",
        )
        self.assertEqual(
            classify_document_identity([no_semantic], ambiguous),
            DocumentIdentityStatus.AMBIGUOUS,
        )

    def test_10_content_sha_exact_duplicate_is_global_before_source_classification(self):
        existing = document(
            document_id="doc-original",
            content_sha=sha("a"),
            natural_key="bca:acct-main:2026-07",
            source_registry_id="bca_statement",
        )
        candidate = document(
            document_id="doc-copy",
            content_sha=sha("a"),
            natural_key="unknown:copy",
            source_registry_id="unknown_source",
        )

        self.assertEqual(
            classify_document_identity([existing], candidate),
            DocumentIdentityStatus.EXACT_DUPLICATE,
        )

    def test_11_source_document_requires_frozen_metadata_and_archive_lineage(self):
        lineage = ArchiveLineageContract(
            parent_archive_sha256=sha("a"),
            archive_depth=2,
            member_path="nested/Jago Apr 2026.pdf",
        )
        doc = SourceDocumentIdentity(
            source_document_id="doc-001",
            content_sha256=sha("b"),
            natural_document_key="jago:acct-main:2026-04",
            template_id="jago_monthly_statement_v1",
            template_fingerprint="jago-fingerprint-v1",
            parser_version="not-implemented",
            source_registry_id="jago_statement",
            import_batch_id="batch-001",
            template_match_status=TemplateMatchStatus.KNOWN,
            period_status=PeriodStatus.CLOSED,
            semantic_sha256=sha("c"),
            archive_lineage=(lineage,),
        )

        self.assertEqual(doc.period_status, PeriodStatus.CLOSED)
        self.assertEqual(doc.archive_lineage[0].archive_depth, 2)

    def test_12_open_and_partial_periods_are_first_class(self):
        open_doc = document(
            document_id="doc-open",
            content_sha=sha("a"),
            natural_key="blu:acct-main:2026-08:mutation",
            period_status=PeriodStatus.OPEN,
        )
        partial_doc = document(
            document_id="doc-partial",
            content_sha=sha("b"),
            natural_key="blu:acct-main:2026-08:portfolio",
            period_status=PeriodStatus.PARTIAL,
        )

        self.assertEqual(open_doc.period_status, PeriodStatus.OPEN)
        self.assertEqual(partial_doc.period_status, PeriodStatus.PARTIAL)

    def test_13_raw_reference_and_provider_id_are_never_overwritten(self):
        evidence = ReferenceEvidenceContract(
            raw_reference="REF / 001-ABC",
            normalized_reference="REF001ABC",
            provider_transaction_id_raw="TX / RAW / 123",
        )

        self.assertEqual(evidence.raw_reference, "REF / 001-ABC")
        self.assertEqual(evidence.normalized_reference, "REF001ABC")
        self.assertEqual(
            evidence.provider_transaction_id_raw,
            "TX / RAW / 123",
        )

    def test_14_raw_locator_is_explicit_provenance(self):
        provenance = SourceProvenanceContract(
            source_document_id="doc-001",
            raw_locator="page:3/row:14",
            page_number=3,
            row_index=14,
            raw_reference="REF RAW",
            raw_description="Original description",
        )

        self.assertEqual(provenance.raw_locator, "page:3/row:14")
        self.assertEqual(provenance.page_number, 3)

    def test_15_import_batch_has_only_dry_run_or_staging_and_requires_idempotency(self):
        batch = ImportBatchContract(
            import_batch_id="batch-001",
            source_registry_id="bca_statement",
            mode=ImportBatchMode.DRY_RUN,
            idempotency_key="doc-set-001",
            document_count=3,
        )

        self.assertEqual(batch.mode, ImportBatchMode.DRY_RUN)
        self.assertEqual(
            {mode.value for mode in ImportBatchMode},
            {"DRY_RUN", "STAGING"},
        )

        with self.assertRaises(ValueError):
            ImportBatchContract(
                import_batch_id="batch-002",
                source_registry_id="bca_statement",
                mode=ImportBatchMode.STAGING,
                idempotency_key="",
            )

    def test_16_provider_and_display_name_collision_do_not_define_identity(self):
        jago = AccountContract(
            account_id="acct-jago-pocket-stockbit",
            institution_id="jago",
            display_name="Stockbit",
            account_type=AccountType.SUBACCOUNT,
            ownership_state=OwnershipState.OWNED,
            provider_account_key="jago-pocket-12",
        )
        stockbit = AccountContract(
            account_id="acct-stockbit-rdn",
            institution_id="stockbit",
            display_name="Stockbit",
            account_type=AccountType.RDN,
            ownership_state=OwnershipState.OWNED,
            provider_account_key="rdn-main",
        )

        self.assertEqual(jago.display_name, stockbit.display_name)
        self.assertNotEqual(jago.account_id, stockbit.account_id)
        self.assertNotEqual(jago.institution_id, stockbit.institution_id)

    def test_17_generic_account_model_represents_blu_wallet_rdn_and_seabank(self):
        blu_main = AccountContract(
            "acct-blu-main",
            "blu",
            "bluAccount",
            AccountType.TRANSACTIONAL,
            OwnershipState.OWNED,
        )
        blu_saving = AccountContract(
            "acct-blu-saving-1",
            "blu",
            "Dana Darurat",
            AccountType.SUBACCOUNT,
            OwnershipState.OWNED,
            parent_account_id=blu_main.account_id,
            provider_account_key="blu-saving-1",
        )
        gopay = AccountContract(
            "acct-gopay",
            "gopay",
            "GoPay",
            AccountType.WALLET,
            OwnershipState.OWNED,
        )
        rdn = AccountContract(
            "acct-stockbit-rdn",
            "stockbit",
            "Stockbit RDN",
            AccountType.RDN,
            OwnershipState.OWNED,
        )
        seabank = AccountContract(
            "acct-seabank-main",
            "seabank",
            "SeaBank",
            AccountType.TRANSACTIONAL,
            OwnershipState.OWNED,
        )

        validate_account_graph(
            [blu_main, blu_saving, gopay, rdn, seabank]
        )
        self.assertEqual(blu_saving.parent_account_id, blu_main.account_id)
        self.assertEqual(gopay.account_type, AccountType.WALLET)
        self.assertEqual(rdn.account_type, AccountType.RDN)

    def test_18_self_purchase_keeps_owner_and_payer_dimensions_separate(self):
        order = CommerceOrderOwnershipContract(
            order_key="order-self",
            commerce_actor="user",
            economic_owner=EconomicOwner.SELF,
            payer_responsibility=PayerResponsibility.SELF,
            payment_match_status=PaymentMatchStatus.MATCHED,
            personal_spending_effect=PersonalSpendingEffect.PERSONAL_EXPENSE,
            recipient="user",
            payer_actor="user",
            economic_owner_actor="user",
        )

        self.assertEqual(order.economic_owner, EconomicOwner.SELF)
        self.assertEqual(
            order.payer_responsibility,
            PayerResponsibility.SELF,
        )

    def test_19_third_party_direct_cannot_become_personal_expense(self):
        order = CommerceOrderOwnershipContract(
            order_key="order-third-party",
            commerce_actor="user",
            economic_owner=EconomicOwner.THIRD_PARTY,
            payer_responsibility=PayerResponsibility.THIRD_PARTY_DIRECT,
            payment_match_status=PaymentMatchStatus.MATCHED,
            personal_spending_effect=PersonalSpendingEffect.NO_PERSONAL_EXPENSE,
            recipient="friend",
            payer_actor="friend",
            economic_owner_actor="friend",
        )
        self.assertEqual(
            order.personal_spending_effect,
            PersonalSpendingEffect.NO_PERSONAL_EXPENSE,
        )

        with self.assertRaises(ValueError):
            CommerceOrderOwnershipContract(
                order_key="order-invalid-third-party",
                commerce_actor="user",
                economic_owner=EconomicOwner.THIRD_PARTY,
                payer_responsibility=PayerResponsibility.THIRD_PARTY_DIRECT,
                payment_match_status=PaymentMatchStatus.MATCHED,
                personal_spending_effect=PersonalSpendingEffect.PERSONAL_EXPENSE,
            )

    def test_20_self_reimbursable_is_pass_through_receivable(self):
        order = CommerceOrderOwnershipContract(
            order_key="order-reimbursable",
            commerce_actor="user",
            economic_owner=EconomicOwner.THIRD_PARTY,
            payer_responsibility=PayerResponsibility.SELF_REIMBURSABLE,
            payment_match_status=PaymentMatchStatus.MATCHED,
            personal_spending_effect=PersonalSpendingEffect.PASS_THROUGH_RECEIVABLE,
            recipient="friend",
            payer_actor="user",
            economic_owner_actor="friend",
        )

        self.assertEqual(
            order.personal_spending_effect,
            PersonalSpendingEffect.PASS_THROUGH_RECEIVABLE,
        )

    def test_21_mixed_order_requires_line_allocation(self):
        mixed = CommerceOrderOwnershipContract(
            order_key="order-mixed",
            commerce_actor="user",
            economic_owner=EconomicOwner.MIXED,
            payer_responsibility=PayerResponsibility.SELF,
            payment_match_status=PaymentMatchStatus.MATCHED,
            personal_spending_effect=PersonalSpendingEffect.MIXED_ALLOCATION,
            line_allocations=(
                CommerceLineAllocation(
                    "line-1",
                    EconomicOwner.SELF,
                    PayerResponsibility.SELF,
                    payer_actor="user",
                    economic_owner_actor="user",
                ),
                CommerceLineAllocation(
                    "line-2",
                    EconomicOwner.THIRD_PARTY,
                    PayerResponsibility.SELF_REIMBURSABLE,
                    payer_actor="user",
                    economic_owner_actor="friend",
                ),
            ),
        )

        self.assertEqual(len(mixed.line_allocations), 2)

    def test_22_unresolved_commerce_requires_review(self):
        order = CommerceOrderOwnershipContract(
            order_key="order-unknown",
            commerce_actor="user",
            economic_owner=EconomicOwner.UNKNOWN,
            payer_responsibility=PayerResponsibility.UNKNOWN,
            payment_match_status=PaymentMatchStatus.UNMATCHED,
            personal_spending_effect=PersonalSpendingEffect.REVIEW_REQUIRED,
        )

        self.assertEqual(
            order.personal_spending_effect,
            PersonalSpendingEffect.REVIEW_REQUIRED,
        )

        with self.assertRaises(ValueError):
            CommerceOrderOwnershipContract(
                order_key="order-invalid-unknown",
                commerce_actor="user",
                economic_owner=EconomicOwner.UNKNOWN,
                payer_responsibility=PayerResponsibility.UNKNOWN,
                payment_match_status=PaymentMatchStatus.UNMATCHED,
                personal_spending_effect=PersonalSpendingEffect.PERSONAL_EXPENSE,
            )


if __name__ == "__main__":
    unittest.main()
