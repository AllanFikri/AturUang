// index.ts: Cloudflare Worker Entrypoint untuk AturUang (Shadow Mode & Ingestion Connectors)
import {
  Env,
  authenticateRequest,
  getSecurityHeaders,
  verifyGmailHmac,
  verifyTelegramWebhook,
  isTelegramUserAllowed,
  isGmailSenderTrusted,
} from "./auth";
import {
  getWibDate,
  getDashboard,
  getAccountsList,
  getTransactionsList,
  getGoalsSummary,
  getUpcomingList,
  getDebtsList,
  reconstructBalance,
  getInsightsSummary,
  parseGmailIntelligence,
  isCanonicalCorrelationCompatible,
  parseTelegramText,
  evaluateAutoApproval,
} from "./domain";

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method.toUpperCase();

    // CORS Preflight: Default deny non-allowed methods
    if (method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: getSecurityHeaders(),
      });
    }

    // 1. Public Health Check
    if (path === "/health") {
      const { dateStr, timeStr } = getWibDate();
      let schemaVer = 0;
      try {
        const d1Row = await env.DB.prepare(
          "SELECT COUNT(*) as ver FROM d1_migrations"
        ).first<{ ver: number }>();
        schemaVer = d1Row?.ver || 0;
      } catch {
        try {
          const verRow = await env.DB.prepare(
            "SELECT MAX(version) as ver FROM schema_migrations"
          ).first<{ ver: number }>();
          schemaVer = verRow?.ver || 0;
        } catch {
          schemaVer = 1;
        }
      }

      return new Response(
        JSON.stringify({
          status: "ok",
          mode: env.MODE || "shadow",
          schema_version: schemaVer,
          time_wib: `${dateStr} ${timeStr} WIB`,
          system: "Cloudflare Worker aturuang-api",
        }),
        {
          status: 200,
          headers: getSecurityHeaders(),
        }
      );
    }

    // =================================================================
    // 2. GMAIL INGESTION RELAY (HMAC-SHA256, Nonce Guard, Strict Sender Trust)
    // =================================================================
    if (path === "/api/ingest/gmail" && method === "POST") {
      const rawBody = await request.text();

      const hmacCheck = await verifyGmailHmac(request, rawBody, env.GMAIL_RELAY_SECRET, env.DB);
      if (!hmacCheck.valid) {
        const errCode = hmacCheck.error || "HMAC_VERIFICATION_FAILED";
        const statusCode = errCode === "REPLAY_GUARD_UNAVAILABLE" ? 503 : 401;
        const errMsg =
          errCode === "REPLAY_GUARD_UNAVAILABLE"
            ? "Layanan proteksi replay D1 tidak tersedia."
            : errCode === "NONCE_REPLAY"
            ? "Nonce replay terdeteksi."
            : "Otentikasi Gmail relay ditolak.";

        return new Response(
          JSON.stringify({
            status: "error",
            code: errCode,
            message: errMsg,
          }),
          { status: statusCode, headers: getSecurityHeaders() }
        );
      }

      try {
        const payload = JSON.parse(rawBody);
        const { message_id, from, subject, body, internal_date } = payload;

        if (!message_id) {
          return new Response(
            JSON.stringify({ status: "error", code: "BAD_REQUEST", message: "message_id wajib disertakan." }),
            { status: 400, headers: getSecurityHeaders() }
          );
        }

        // Strict evidence-backed sender trust (Fail-closed BEFORE any DB/Candidate insert)
        const senderCheck = isGmailSenderTrusted(from || "");
        if (!senderCheck.trusted) {
          return new Response(
            JSON.stringify({
              status: "error",
              code: "UNTRUSTED_GMAIL_SENDER",
              message: "Sender Gmail tidak diizinkan.",
              sender: senderCheck.email || "unknown",
            }),
            { status: 403, headers: getSecurityHeaders() }
          );
        }

        const now = getWibDate().dateStr;

        // Compute actual deterministic SHA-256 hash of payload
        const hashBuf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(rawBody));
        const payloadHash = Array.from(new Uint8Array(hashBuf))
          .map((b) => b.toString(16).padStart(2, "0"))
          .join("");

        // Idempotency + incomplete-ingestion recovery.
        // Completed Gmail messages must already have canonical evidence.
        // A raw row without evidence is an incomplete previous attempt and
        // may be safely removed/reprocessed using the same Gmail message ID.
        const existing = await env.DB.prepare(
          `SELECT
             r.id,
             r.state,
             CASE WHEN EXISTS (
               SELECT 1
               FROM canonical_event_evidence e
               WHERE e.raw_event_id = r.id
             ) THEN 1 ELSE 0 END AS has_canonical_evidence
           FROM raw_events r
           WHERE r.source = 'gmail' AND r.external_id = ?`
        ).bind(message_id).first<{
          id: number;
          state: string;
          has_canonical_evidence: number;
        }>();

        if (existing) {
          if (Number(existing.has_canonical_evidence || 0) === 1) {
            return new Response(
              JSON.stringify({
                status: "success",
                duplicate: true,
                raw_event_id: existing.id,
                message: "Email telah diproses sebelumnya (idempoten).",
              }),
              { status: 200, headers: getSecurityHeaders() }
            );
          }

          // Self-heal a partial previous Gmail ingestion.
          // ingestion_candidates cascades through raw_event_id.
          const cleanup = await env.DB.prepare(
            `DELETE FROM raw_events
             WHERE id = ?
               AND source = 'gmail'
               AND NOT EXISTS (
                 SELECT 1
                 FROM canonical_event_evidence e
                 WHERE e.raw_event_id = raw_events.id
               )`
          ).bind(existing.id).run();

          if ((cleanup.meta.changes || 0) !== 1) {
            // D1 mutation metadata is not used as the sole proof of cleanup.
            // Verify the authoritative post-delete state before failing.
            const remainingPartial = await env.DB.prepare(
              `SELECT id
               FROM raw_events
               WHERE id = ?
                 AND source = 'gmail'
               LIMIT 1`
            ).bind(existing.id).first<{ id: number }>();

            if (remainingPartial) {
              return new Response(
                JSON.stringify({
                  status: "error",
                  code: "INCOMPLETE_GMAIL_EVENT_REPAIR_FAILED",
                  message: "Event Gmail parsial tidak dapat dipulihkan dengan aman.",
                }),
                { status: 503, headers: getSecurityHeaders() }
              );
            }
          }
        }

        // Transaction Intelligence Engine v1
        const canonEv = parseGmailIntelligence(subject || "", body || "", from || "", internal_date);

        // Minimal sanitized non-PII payload
        const minimalPayload = JSON.stringify({
          sender: senderCheck.email,
          event_kind: canonEv.event_kind,
          financial_class: canonEv.financial_class,
          amount: canonEv.amount,
          confidence: canonEv.confidence,
        });

        // Insert raw event
        const rawRes = await env.DB.prepare(
          `INSERT INTO raw_events (source, external_id, occurred_at, payload_hash, parser_version, state, minimal_raw_payload)
           VALUES ('gmail', ?, ?, ?, '2.0.0', 'Parsed', ?)`
        ).bind(message_id, internal_date || now, payloadHash, minimalPayload).run();

        const rawEventId = rawRes.meta.last_row_id;

        // Insert candidate if applicable
        let candidateId: number | null = null;
        let autoStatus = "Ignored";

        if (canonEv.candidate && Number(canonEv.candidate.amount) > 0) {
          autoStatus = evaluateAutoApproval(canonEv.candidate, true);

          const candRes = await env.DB.prepare(
            `INSERT INTO ingestion_candidates
             (raw_event_id, tx_type, amount, account, to_account, category, money_context, person_name, date, time, confidence_score, status, reasons)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
          ).bind(
            rawEventId,
            canonEv.candidate.tx_type,
            canonEv.candidate.amount,
            canonEv.candidate.account,
            canonEv.candidate.to_account || null,
            canonEv.candidate.category,
            canonEv.candidate.money_context,
            canonEv.candidate.person_name || null,
            canonEv.candidate.date,
            canonEv.candidate.time || null,
            canonEv.candidate.confidence_score,
            autoStatus,
            canonEv.candidate.reasons
          ).run();

          candidateId = candRes.meta.last_row_id;
        } else if (canonEv.candidate) {
          // Fail-safe: kandidat dengan nominal nol/tidak valid
          // tetap boleh menjadi raw/canonical evidence,
          // tetapi tidak boleh masuk ingestion_candidates.
          autoStatus = "Ignored";
        }

        // Insert / correlate Canonical Financial Event (Migration 0007)
        // Fail closed: raw/candidate ingestion is NOT considered successful
        // until canonical evidence has also been persisted.
        let canonDbId: number | null = null;
        let canonicalStage = "lookup_init";

        try {
          let existingCanon: { id: number } | null = null;

          if (canonEv.transaction_reference) {
            canonicalStage = "lookup_transaction_reference";
            existingCanon = await env.DB.prepare(
              "SELECT id FROM canonical_financial_events WHERE transaction_reference = ? LIMIT 1"
            ).bind(canonEv.transaction_reference).first<{ id: number }>();
          }

          if (!existingCanon && canonEv.external_order_id) {
            canonicalStage = "lookup_external_order_id";
            existingCanon = await env.DB.prepare(
              "SELECT id FROM canonical_financial_events WHERE external_order_id = ? LIMIT 1"
            ).bind(canonEv.external_order_id).first<{ id: number }>();
          }

          if (!existingCanon) {
            canonicalStage = "lookup_event_id";
            existingCanon = await env.DB.prepare(
              "SELECT id FROM canonical_financial_events WHERE event_id = ? LIMIT 1"
            ).bind(canonEv.event_id).first<{ id: number }>();
          }

          if (existingCanon) {
            canonicalStage = "load_existing_canonical_for_guard";

            const existingCanonDetails = await env.DB.prepare(
              `SELECT
                 event_kind,
                 financial_class,
                 financial_direction,
                 amount,
                 merchant_normalized
               FROM canonical_financial_events
               WHERE id = ?
               LIMIT 1`
            ).bind(existingCanon.id).first<{
              event_kind: string;
              financial_class: string;
              financial_direction: string;
              amount: number;
              merchant_normalized: string | null;
            }>();

            if (!existingCanonDetails) {
              canonicalStage = "reject_missing_existing_canonical";
              throw new Error("CANONICAL_CORRELATION_TARGET_MISSING");
            }

            if (
              !isCanonicalCorrelationCompatible(
                canonEv,
                existingCanonDetails
              )
            ) {
              canonicalStage = "reject_incompatible_existing_canonical";
              throw new Error("CANONICAL_CORRELATION_CONFLICT");
            }
          }
          if (existingCanon) {
            canonDbId = existingCanon.id;
            canonicalStage = "insert_existing_evidence";

            const evidenceRes = await env.DB.prepare(
              `INSERT INTO canonical_event_evidence
               (canonical_event_id, raw_event_id, candidate_id, evidence_role)
               VALUES (?, ?, ?, ?)`
            ).bind(
              canonDbId,
              rawEventId,
              candidateId,
              canonEv.evidence_role
            ).run();

            if ((evidenceRes.meta.changes || 0) !== 1) {
              throw new Error("CANONICAL_EVIDENCE_INSERT_FAILED");
            }
          } else {
            canonicalStage = "insert_new_canonical_batch";
            const canonicalInsert = env.DB.prepare(
              `INSERT INTO canonical_financial_events
               (event_id, occurred_at_wib, status, event_kind, financial_class, financial_direction,
                amount, currency, fee_amount, source_account_alias, destination_account_alias,
                destination_owner_type, merchant_normalized, merchant_pan, merchant_location,
                counterparty_normalized, description_normalized, transaction_reference,
                external_order_id, confidence, recommended_action, review_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
            ).bind(
              canonEv.event_id,
              canonEv.occurred_at_wib,
              canonEv.status,
              canonEv.event_kind,
              canonEv.financial_class,
              canonEv.financial_direction,
              canonEv.amount,
              canonEv.currency,
              canonEv.fee_amount,
              canonEv.source_account_alias,
              canonEv.destination_account_alias,
              canonEv.destination_owner_type,
              canonEv.merchant_normalized,
              canonEv.merchant_pan,
              canonEv.merchant_location,
              canonEv.counterparty_normalized,
              canonEv.description_normalized,
              canonEv.transaction_reference,
              canonEv.external_order_id,
              canonEv.confidence,
              canonEv.recommended_action,
              canonEv.review_reason
            );

            // Canonical row + its evidence are one atomic D1 batch.
            // If evidence fails, the newly-created canonical row rolls back.
            const evidenceInsert = env.DB.prepare(
              `INSERT INTO canonical_event_evidence
               (canonical_event_id, raw_event_id, candidate_id, evidence_role)
               SELECT id, ?, ?, ?
               FROM canonical_financial_events
               WHERE event_id = ?`
            ).bind(
              rawEventId,
              candidateId,
              canonEv.evidence_role,
              canonEv.event_id
            );

            const canonicalBatch = await env.DB.batch([
              canonicalInsert,
              evidenceInsert,
            ]);

            canonDbId =
              canonicalBatch[0]?.meta?.last_row_id || null;

            const evidenceChanges =
              canonicalBatch[1]?.meta?.changes || 0;

            if (!canonDbId || evidenceChanges !== 1) {
              throw new Error("CANONICAL_BATCH_PERSISTENCE_FAILED");
            }
          }
        } catch (canonicalErr: any) {
          const safeStage = canonicalStage
            .toUpperCase()
            .replace(/[^A-Z0-9_]/g, "_");

          console.error(
            `GMAIL_CANONICAL_FAILURE | stage=${canonicalStage}`
          );

          return new Response(
            JSON.stringify({
              status: "error",
              code: `CANONICAL_${safeStage}`,
              message: "Canonical Gmail persistence failed.",
            }),
            { status: 500, headers: getSecurityHeaders() }
          );
        }

        // Update source freshness
        await env.DB.prepare(
          `INSERT INTO source_sync_state (source, last_attempt_at, last_success_at, last_event_at, status)
           VALUES ('gmail', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, 'OK')
           ON CONFLICT(source) DO UPDATE SET
             last_attempt_at = CURRENT_TIMESTAMP,
             last_success_at = CURRENT_TIMESTAMP,
             last_event_at = excluded.last_event_at,
             status = 'OK'`
        ).bind(internal_date || now).run();

        return new Response(
          JSON.stringify({
            status: "success",
            raw_event_id: rawEventId,
            candidate_id: candidateId,
            canonical_event_id: canonDbId,
            event_kind: canonEv.event_kind,
            financial_class: canonEv.financial_class,
            candidate_status: autoStatus,
          }),
          { status: 200, headers: getSecurityHeaders() }
        );
      } catch (err: any) {
        return new Response(
          JSON.stringify({ status: "error", code: "INTERNAL_SERVER_ERROR", message: "Terjadi kesalahan internal pada layanan backend cloud." }),
          { status: 500, headers: getSecurityHeaders() }
        );
      }
    }


    // =================================================================
    // 2b. TARGETED BCA QRIS CANONICAL REPAIR
    //
    // Purpose:
    // - repair only evidence currently collapsed into bca_qris_erensi;
    // - preserve raw_event_id and candidate_id;
    // - verify replayed Gmail payload against the original payload_hash;
    // - never delete raw_events, ingestion_candidates, or the bad canonical;
    // - fail closed on any identity/economic mismatch.
    //
    // Manual recovery only. MODE must be exactly "shadow".
    // =================================================================
    if (path === "/api/repair/bca-qris" && method === "POST") {
      const rawBody = await request.text();

      const hmacCheck = await verifyGmailHmac(
        request,
        rawBody,
        env.GMAIL_RELAY_SECRET,
        env.DB
      );

      if (!hmacCheck.valid) {
        const errCode =
          hmacCheck.error ||
          "HMAC_VERIFICATION_FAILED";

        const statusCode =
          errCode === "REPLAY_GUARD_UNAVAILABLE"
            ? 503
            : 401;

        return new Response(
          JSON.stringify({
            status: "error",
            code: errCode,
            message:
              "Otentikasi repair Gmail ditolak.",
          }),
          {
            status: statusCode,
            headers: getSecurityHeaders(),
          }
        );
      }

      if (
        String(env.MODE || "").toLowerCase() !==
        "shadow"
      ) {
        return new Response(
          JSON.stringify({
            status: "error",
            code: "BCA_QRIS_REPAIR_NOT_SHADOW",
            message:
              "Targeted repair hanya diizinkan dalam mode shadow.",
          }),
          {
            status: 409,
            headers: getSecurityHeaders(),
          }
        );
      }

      try {
        const payload = JSON.parse(rawBody);
        const action =
          String(payload?.action || "").trim();

        const badEventId =
          "bca_qris_erensi";

        // -----------------------------------------------------
        // NEXT
        //
        // D1 itself is the repair checkpoint:
        // once evidence is moved away from the bad canonical,
        // the message automatically disappears from this queue.
        // -----------------------------------------------------
        if (action === "next") {
          const rawLimit =
            Number(payload?.limit || 10);

          const limit =
            Number.isInteger(rawLimit)
              ? Math.min(
                  Math.max(rawLimit, 1),
                  25
                )
              : 10;

          const targetResult =
            await env.DB.prepare(
              `SELECT
                 r.external_id AS message_id
               FROM canonical_event_evidence e
               JOIN canonical_financial_events c
                 ON c.id = e.canonical_event_id
               JOIN raw_events r
                 ON r.id = e.raw_event_id
               WHERE c.event_id = ?
                 AND r.source = 'gmail'
               ORDER BY e.id
               LIMIT ?`
            )
            .bind(
              badEventId,
              limit
            )
            .all<{
              message_id: string;
            }>();

          const remainingRow =
            await env.DB.prepare(
              `SELECT
                 COUNT(*) AS remaining
               FROM canonical_event_evidence e
               JOIN canonical_financial_events c
                 ON c.id = e.canonical_event_id
               JOIN raw_events r
                 ON r.id = e.raw_event_id
               WHERE c.event_id = ?
                 AND r.source = 'gmail'`
            )
            .bind(badEventId)
            .first<{
              remaining: number;
            }>();

          return new Response(
            JSON.stringify({
              status: "success",
              action: "next",
              remaining:
                Number(
                  remainingRow?.remaining || 0
                ),
              message_ids:
                (targetResult.results || [])
                  .map(
                    (row) =>
                      String(
                        row.message_id || ""
                      )
                  )
                  .filter(Boolean),
            }),
            {
              status: 200,
              headers: getSecurityHeaders(),
            }
          );
        }

        if (action !== "repair") {
          return new Response(
            JSON.stringify({
              status: "error",
              code:
                "BCA_QRIS_REPAIR_INVALID_ACTION",
              message:
                "Action repair tidak dikenal.",
            }),
            {
              status: 400,
              headers: getSecurityHeaders(),
            }
          );
        }

        const {
          message_id,
          from,
          subject,
          body,
          internal_date,
        } = payload;

        if (
          !message_id ||
          !from ||
          typeof body !== "string" ||
          !internal_date
        ) {
          return new Response(
            JSON.stringify({
              status: "error",
              code:
                "BCA_QRIS_REPAIR_BAD_REQUEST",
              message:
                "Payload repair tidak lengkap.",
            }),
            {
              status: 400,
              headers: getSecurityHeaders(),
            }
          );
        }

        const senderCheck =
          isGmailSenderTrusted(from);

        if (
          !senderCheck.trusted ||
          senderCheck.email !==
            "bca@bca.co.id"
        ) {
          return new Response(
            JSON.stringify({
              status: "error",
              code:
                "BCA_QRIS_REPAIR_INVALID_SENDER",
              message:
                "Repair hanya menerima bukti BCA yang dipercaya.",
            }),
            {
              status: 403,
              headers: getSecurityHeaders(),
            }
          );
        }

        // Rebuild the exact original Gmail relay payload.
        //
        // Property order intentionally matches Code.gs:
        // message_id, from, subject, body, internal_date.
        const originalRelayBody =
          JSON.stringify({
            message_id,
            from,
            subject: subject || "",
            body,
            internal_date,
          });

        const originalHashBuffer =
          await crypto.subtle.digest(
            "SHA-256",
            new TextEncoder().encode(
              originalRelayBody
            )
          );

        const replayedPayloadHash =
          Array.from(
            new Uint8Array(
              originalHashBuffer
            )
          )
            .map(
              (b) =>
                b
                  .toString(16)
                  .padStart(2, "0")
            )
            .join("");

        // Parse with the currently deployed parser.
        const canonEv =
          parseGmailIntelligence(
            subject || "",
            body,
            from,
            internal_date
          );

        const normalizedRef =
          String(
            canonEv.transaction_reference ||
              ""
          ).trim();

        const normalizedMerchant =
          String(
            canonEv.merchant_normalized ||
              ""
          ).trim();

        const normalizedCounterparty =
          String(
            canonEv.counterparty_normalized ||
              ""
          ).trim();

        const replayBodyHasQris =
          String(body || "")
            .toLowerCase()
            .includes("qris") ||
          String(subject || "")
            .toLowerCase()
            .includes("qris");

        const validAmount =
          Number.isFinite(
            Number(canonEv.amount)
          ) &&
          Number(canonEv.amount) > 0;

        const validReference =
          Boolean(normalizedRef) &&
          normalizedRef
            .toLowerCase() !==
            "erensi";

        const isMerchantPaymentRepair =
          canonEv.status ===
            "AutoApproved" &&
          canonEv.event_kind ===
            "MERCHANT_PAYMENT" &&
          canonEv.financial_class ===
            "Expense" &&
          canonEv.financial_direction ===
            "Debit" &&
          canonEv.evidence_role ===
            "PRIMARY_PAYMENT" &&
          canonEv.candidate?.tx_type ===
            "Expense" &&
          canonEv.candidate?.status ===
            "AutoApproved" &&
          Boolean(normalizedMerchant) &&
          normalizedMerchant
            .toLowerCase() !==
            "qris merchant";

        const isTransferQrisRepair =
          canonEv.status ===
            "Pending" &&
          canonEv.event_kind ===
            "EXTERNAL_TRANSFER" &&
          canonEv.financial_class ===
            "Pending Review" &&
          canonEv.financial_direction ===
            "Debit" &&
          canonEv.evidence_role ===
            "PRIMARY_PAYMENT" &&
          canonEv.description_normalized ===
            "Transfer QRIS BCA" &&
          canonEv.candidate?.tx_type ===
            "Expense" &&
          canonEv.candidate?.status ===
            "Pending" &&
          Boolean(normalizedCounterparty) &&
          normalizedCounterparty
            .toLowerCase() !==
            "third party";

        const isFailedQrisRepair =
          canonEv.status ===
            "Ignored" &&
          canonEv.event_kind ===
            "FAILED_ATTEMPT" &&
          canonEv.financial_class ===
            "Ignore" &&
          canonEv.financial_direction ===
            "Neutral" &&
          canonEv.evidence_role ===
            "LIFECYCLE_STATUS" &&
          canonEv.candidate == null;

        if (
          !replayBodyHasQris ||
          !validAmount ||
          !validReference ||
          canonEv.event_id ===
            badEventId ||
          (
            !isMerchantPaymentRepair &&
            !isTransferQrisRepair &&
            !isFailedQrisRepair
          )
        ) {
          return new Response(
            JSON.stringify({
              status: "error",
              code:
                "BCA_QRIS_REPAIR_PARSE_REJECTED",
              message:
                "Email tidak menghasilkan semantic QRIS yang aman untuk repair.",
            }),
            {
              status: 422,
              headers: getSecurityHeaders(),
            }
          );
        }

        const expectedCandidateStatus =
          isFailedQrisRepair
            ? "Rejected"
            : String(
                canonEv.candidate?.status ||
                ""
              );

        const evidenceLookup =
          await env.DB.prepare(
            `SELECT
               e.id AS evidence_id,
               e.raw_event_id,
               e.candidate_id,
               e.evidence_role AS current_evidence_role,
               r.payload_hash,
               c.id AS current_canonical_id,
               c.event_id AS current_event_id,
               ic.amount AS candidate_amount,
               ic.status AS candidate_status,
               ic.applied_transaction_id
             FROM raw_events r
             JOIN canonical_event_evidence e
               ON e.raw_event_id = r.id
             JOIN canonical_financial_events c
               ON c.id = e.canonical_event_id
             LEFT JOIN ingestion_candidates ic
               ON ic.id = e.candidate_id
             WHERE r.source = 'gmail'
               AND r.external_id = ?
             ORDER BY e.id
             LIMIT 2`
          )
          .bind(message_id)
          .all<{
            evidence_id: number;
            raw_event_id: number;
            candidate_id:
              number | null;
            current_evidence_role:
              string;
            payload_hash: string;
            current_canonical_id:
              number;
            current_event_id:
              string;
            candidate_amount:
              number | null;
            candidate_status:
              string | null;
            applied_transaction_id:
              number | null;
          }>();

        const evidenceRows =
          evidenceLookup.results || [];

        if (evidenceRows.length !== 1) {
          return new Response(
            JSON.stringify({
              status: "error",
              code:
                "BCA_QRIS_REPAIR_EVIDENCE_MULTIPLICITY",
              message:
                "Target repair harus memiliki tepat satu evidence row.",
            }),
            {
              status:
                evidenceRows.length === 0
                  ? 404
                  : 409,
              headers: getSecurityHeaders(),
            }
          );
        }

        const current =
          evidenceRows[0];

        if (
          !current.candidate_id ||
          current.candidate_amount ===
            null ||
          !Number.isFinite(
            Number(
              current.candidate_amount
            )
          )
        ) {
          return new Response(
            JSON.stringify({
              status: "error",
              code:
                "BCA_QRIS_REPAIR_CANDIDATE_MISSING",
              message:
                "Candidate evidence tidak lengkap.",
            }),
            {
              status: 409,
              headers: getSecurityHeaders(),
            }
          );
        }

        if (
          current.applied_transaction_id !==
          null
        ) {
          return new Response(
            JSON.stringify({
              status: "error",
              code:
                "BCA_QRIS_REPAIR_CANDIDATE_ALREADY_APPLIED",
              message:
                "Candidate target sudah pernah diaplikasikan dan tidak boleh direklasifikasi otomatis.",
            }),
            {
              status: 409,
              headers: getSecurityHeaders(),
            }
          );
        }

        if (
          String(
            current.payload_hash || ""
          ).toLowerCase() !==
          replayedPayloadHash.toLowerCase()
        ) {
          return new Response(
            JSON.stringify({
              status: "error",
              code:
                "BCA_QRIS_REPAIR_PAYLOAD_HASH_MISMATCH",
              message:
                "Payload Gmail replay tidak identik dengan evidence asli.",
            }),
            {
              status: 409,
              headers: getSecurityHeaders(),
            }
          );
        }

        if (
          Math.abs(
            Number(
              current.candidate_amount
            ) -
              Number(canonEv.amount)
          ) >= 0.005
        ) {
          return new Response(
            JSON.stringify({
              status: "error",
              code:
                "BCA_QRIS_REPAIR_AMOUNT_MISMATCH",
              message:
                "Nominal hasil reparse berbeda dari candidate asli.",
            }),
            {
              status: 409,
              headers: getSecurityHeaders(),
            }
          );
        }

        // Idempotent retry.
        if (
          current.current_event_id !==
          badEventId
        ) {
          if (
            current.current_event_id ===
              canonEv.event_id &&
            current.current_evidence_role ===
              canonEv.evidence_role &&
            current.candidate_status ===
              expectedCandidateStatus &&
            current.applied_transaction_id ===
              null
          ) {
            return new Response(
              JSON.stringify({
                status: "success",
                action: "repair",
                repaired: false,
                already_repaired: true,
                raw_event_id:
                  current.raw_event_id,
                candidate_id:
                  current.candidate_id,
                canonical_event_id:
                  current.current_canonical_id,
              }),
              {
                status: 200,
                headers:
                  getSecurityHeaders(),
              }
            );
          }

          return new Response(
            JSON.stringify({
              status: "error",
              code:
                "BCA_QRIS_REPAIR_TARGET_MOVED_ELSEWHERE",
              message:
                "Evidence sudah tidak berada pada canonical target repair.",
            }),
            {
              status: 409,
              headers: getSecurityHeaders(),
            }
          );
        }

        type RepairCanonicalRow = {
          id: number;
          event_id: string;
          status: string;
          event_kind: string;
          financial_class: string;
          financial_direction: string;
          amount: number;
          merchant_normalized:
            string | null;
          counterparty_normalized:
            string | null;
        };

        const referenceLookup =
          await env.DB.prepare(
            `SELECT
               id,
               event_id,
               status,
               event_kind,
               financial_class,
               financial_direction,
               amount,
               merchant_normalized,
               counterparty_normalized
             FROM canonical_financial_events
             WHERE transaction_reference = ?
             ORDER BY id
             LIMIT 2`
          )
          .bind(
            canonEv.transaction_reference
          )
          .all<RepairCanonicalRow>();

        const referenceRows =
          referenceLookup.results || [];

        if (referenceRows.length > 1) {
          return new Response(
            JSON.stringify({
              status: "error",
              code:
                "BCA_QRIS_REPAIR_REFERENCE_MULTIPLICITY",
              message:
                "Transaction reference dimiliki lebih dari satu canonical.",
            }),
            {
              status: 409,
              headers: getSecurityHeaders(),
            }
          );
        }

        const byReference =
          referenceRows.length === 1
            ? referenceRows[0]
            : null;

        const byEventId =
          await env.DB.prepare(
            `SELECT
               id,
               event_id,
               status,
               event_kind,
               financial_class,
               financial_direction,
               amount,
               merchant_normalized,
               counterparty_normalized
             FROM canonical_financial_events
             WHERE event_id = ?
             LIMIT 1`
          )
          .bind(canonEv.event_id)
          .first<RepairCanonicalRow>();

        if (
          byReference &&
          byReference.event_id !==
            canonEv.event_id
        ) {
          return new Response(
            JSON.stringify({
              status: "error",
              code:
                "BCA_QRIS_REPAIR_REFERENCE_CONFLICT",
              message:
                "Reference sudah dimiliki canonical lain.",
            }),
            {
              status: 409,
              headers: getSecurityHeaders(),
            }
          );
        }

        if (
          byReference &&
          byEventId &&
          byReference.id !==
            byEventId.id
        ) {
          return new Response(
            JSON.stringify({
              status: "error",
              code:
                "BCA_QRIS_REPAIR_CANONICAL_AMBIGUOUS",
              message:
                "Reference dan event ID menunjuk canonical berbeda.",
            }),
            {
              status: 409,
              headers: getSecurityHeaders(),
            }
          );
        }

        const existingTarget =
          byEventId ||
          byReference;

        if (existingTarget) {
          const sameSignature =
            existingTarget.status ===
              canonEv.status &&
            existingTarget.event_kind ===
              canonEv.event_kind &&
            existingTarget.financial_class ===
              canonEv.financial_class &&
            existingTarget.financial_direction ===
              canonEv.financial_direction &&
            Math.abs(
              Number(
                existingTarget.amount
              ) -
                Number(canonEv.amount)
            ) < 0.005;

          if (!sameSignature) {
            return new Response(
              JSON.stringify({
                status: "error",
                code:
                  "BCA_QRIS_REPAIR_CANONICAL_CONFLICT",
                message:
                  "Canonical existing memiliki signature semantic berbeda.",
              }),
              {
                status: 409,
                headers:
                  getSecurityHeaders(),
              }
            );
          }

          if (
            isMerchantPaymentRepair
          ) {
            const existingMerchant =
              String(
                existingTarget
                  .merchant_normalized ||
                  ""
              )
                .trim()
                .toLowerCase();

            if (
              !existingMerchant ||
              existingMerchant ===
                "qris merchant"
            ) {
              return new Response(
                JSON.stringify({
                  status: "error",
                  code:
                    "BCA_QRIS_REPAIR_EXISTING_CANONICAL_INCOMPLETE",
                  message:
                    "Canonical payment existing belum memiliki merchant yang kuat.",
                }),
                {
                  status: 409,
                  headers:
                    getSecurityHeaders(),
                }
              );
            }

            if (
              !isCanonicalCorrelationCompatible(
                canonEv,
                existingTarget
              )
            ) {
              return new Response(
                JSON.stringify({
                  status: "error",
                  code:
                    "BCA_QRIS_REPAIR_CANONICAL_CONFLICT",
                  message:
                    "Canonical payment existing tidak kompatibel.",
                }),
                {
                  status: 409,
                  headers:
                    getSecurityHeaders(),
                }
              );
            }
          }

          if (
            isTransferQrisRepair
          ) {
            const existingCounterparty =
              String(
                existingTarget
                  .counterparty_normalized ||
                  ""
              )
                .trim()
                .toLowerCase();

            if (
              !existingCounterparty ||
              existingCounterparty ===
                "third party" ||
              existingCounterparty !==
                normalizedCounterparty
                  .toLowerCase()
            ) {
              return new Response(
                JSON.stringify({
                  status: "error",
                  code:
                    "BCA_QRIS_REPAIR_CANONICAL_CONFLICT",
                  message:
                    "Canonical transfer existing tidak memiliki counterparty yang cocok.",
                }),
                {
                  status: 409,
                  headers:
                    getSecurityHeaders(),
                }
              );
            }
          }
        }

        const candidateRepair =
          canonEv.candidate ||
          null;

        const candidateUpdate =
          candidateRepair
            ? env.DB.prepare(
                `UPDATE ingestion_candidates
                 SET tx_type = ?,
                     account = ?,
                     to_account = ?,
                     category = ?,
                     money_context = ?,
                     person_name = ?,
                     date = ?,
                     time = ?,
                     confidence_score = ?,
                     status = ?,
                     reasons = ?
                 WHERE id = ?
                   AND raw_event_id = ?
                   AND applied_transaction_id IS NULL
                   AND ABS(amount - ?) < 0.005
                   AND EXISTS (
                     SELECT 1
                     FROM canonical_event_evidence
                     WHERE id = ?
                       AND canonical_event_id = ?
                       AND raw_event_id = ?
                       AND candidate_id = ?
                   )`
              ).bind(
                candidateRepair.tx_type,
                candidateRepair.account,
                candidateRepair.to_account ??
                  null,
                candidateRepair.category,
                candidateRepair.money_context,
                candidateRepair.person_name ??
                  null,
                candidateRepair.date,
                candidateRepair.time ??
                  null,
                candidateRepair.confidence_score,
                candidateRepair.status,
                candidateRepair.reasons,
                current.candidate_id,
                current.raw_event_id,
                canonEv.amount,
                current.evidence_id,
                current.current_canonical_id,
                current.raw_event_id,
                current.candidate_id
              )
            : env.DB.prepare(
                `UPDATE ingestion_candidates
                 SET status = 'Rejected',
                     reasons = ?
                 WHERE id = ?
                   AND raw_event_id = ?
                   AND applied_transaction_id IS NULL
                   AND ABS(amount - ?) < 0.005
                   AND EXISTS (
                     SELECT 1
                     FROM canonical_event_evidence
                     WHERE id = ?
                       AND canonical_event_id = ?
                       AND raw_event_id = ?
                       AND candidate_id = ?
                   )`
              ).bind(
                "BCA QRIS: transaksi gagal; candidate lama ditolak saat targeted semantic repair.",
                current.candidate_id,
                current.raw_event_id,
                canonEv.amount,
                current.evidence_id,
                current.current_canonical_id,
                current.raw_event_id,
                current.candidate_id
              );

        if (existingTarget) {
          const moveExisting =
            env.DB.prepare(
              `UPDATE canonical_event_evidence
               SET canonical_event_id = ?,
                   evidence_role = ?
               WHERE id = ?
                 AND canonical_event_id = ?
                 AND raw_event_id = ?
                 AND candidate_id = ?`
            )
            .bind(
              existingTarget.id,
              canonEv.evidence_role,
              current.evidence_id,
              current.current_canonical_id,
              current.raw_event_id,
              current.candidate_id
            );

          await env.DB.batch([
            candidateUpdate,
            moveExisting,
          ]);
        } else {
          const canonicalInsert =
            env.DB.prepare(
              `INSERT INTO canonical_financial_events
               (event_id, occurred_at_wib, status, event_kind, financial_class, financial_direction,
                amount, currency, fee_amount, source_account_alias, destination_account_alias,
                destination_owner_type, merchant_normalized, merchant_pan, merchant_location,
                counterparty_normalized, description_normalized, transaction_reference,
                external_order_id, confidence, recommended_action, review_reason)
               SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
               WHERE EXISTS (
                 SELECT 1
                 FROM canonical_event_evidence
                 WHERE id = ?
                   AND canonical_event_id = ?
                   AND raw_event_id = ?
                   AND candidate_id = ?
               )`
            )
            .bind(
              canonEv.event_id,
              canonEv.occurred_at_wib,
              canonEv.status,
              canonEv.event_kind,
              canonEv.financial_class,
              canonEv.financial_direction,
              canonEv.amount,
              canonEv.currency,
              canonEv.fee_amount,
              canonEv.source_account_alias,
              canonEv.destination_account_alias,
              canonEv.destination_owner_type,
              canonEv.merchant_normalized,
              canonEv.merchant_pan,
              canonEv.merchant_location,
              canonEv.counterparty_normalized,
              canonEv.description_normalized,
              canonEv.transaction_reference,
              canonEv.external_order_id,
              canonEv.confidence,
              canonEv.recommended_action,
              canonEv.review_reason,
              current.evidence_id,
              current.current_canonical_id,
              current.raw_event_id,
              current.candidate_id
            );

          const moveEvidence =
            env.DB.prepare(
              `UPDATE canonical_event_evidence
               SET canonical_event_id = (
                     SELECT id
                     FROM canonical_financial_events
                     WHERE event_id = ?
                     LIMIT 1
                   ),
                   evidence_role = ?
               WHERE id = ?
                 AND canonical_event_id = ?
                 AND raw_event_id = ?
                 AND candidate_id = ?`
            )
            .bind(
              canonEv.event_id,
              canonEv.evidence_role,
              current.evidence_id,
              current.current_canonical_id,
              current.raw_event_id,
              current.candidate_id
            );

          await env.DB.batch([
            canonicalInsert,
            candidateUpdate,
            moveEvidence,
          ]);
        }

        const postRepair =
          await env.DB.prepare(
            `SELECT
               c.id AS canonical_event_id,
               c.event_id,
               c.status AS canonical_status,
               c.event_kind,
               c.financial_class,
               c.financial_direction,
               c.amount AS canonical_amount,
               e.raw_event_id,
               e.candidate_id,
               e.evidence_role,
               ic.raw_event_id AS candidate_raw_event_id,
               ic.status AS candidate_status,
               ic.applied_transaction_id
             FROM canonical_event_evidence e
             JOIN canonical_financial_events c
               ON c.id = e.canonical_event_id
             JOIN ingestion_candidates ic
               ON ic.id = e.candidate_id
             WHERE e.id = ?
             LIMIT 1`
          )
          .bind(current.evidence_id)
          .first<{
            canonical_event_id:
              number;
            event_id: string;
            canonical_status:
              string;
            event_kind: string;
            financial_class:
              string;
            financial_direction:
              string;
            canonical_amount:
              number;
            raw_event_id: number;
            candidate_id:
              number | null;
            evidence_role:
              string;
            candidate_raw_event_id:
              number;
            candidate_status:
              string;
            applied_transaction_id:
              number | null;
          }>();

        if (
          !postRepair ||
          postRepair.event_id !==
            canonEv.event_id ||
          postRepair.canonical_status !==
            canonEv.status ||
          postRepair.event_kind !==
            canonEv.event_kind ||
          postRepair.financial_class !==
            canonEv.financial_class ||
          postRepair.financial_direction !==
            canonEv.financial_direction ||
          Math.abs(
            Number(
              postRepair.canonical_amount
            ) -
              Number(canonEv.amount)
          ) >= 0.005 ||
          postRepair.raw_event_id !==
            current.raw_event_id ||
          postRepair.candidate_id !==
            current.candidate_id ||
          postRepair.evidence_role !==
            canonEv.evidence_role ||
          postRepair.candidate_raw_event_id !==
            current.raw_event_id ||
          postRepair.candidate_status !==
            expectedCandidateStatus ||
          postRepair.applied_transaction_id !==
            null
        ) {
          return new Response(
            JSON.stringify({
              status: "error",
              code:
                "BCA_QRIS_REPAIR_POSTCHECK_FAILED",
              message:
                "Post-repair canonical/evidence/candidate state tidak sesuai kontrak.",
            }),
            {
              status: 500,
              headers: getSecurityHeaders(),
            }
          );
        }

        const remainingRow =
          await env.DB.prepare(
            `SELECT
               COUNT(*) AS remaining
             FROM canonical_event_evidence e
             JOIN canonical_financial_events c
               ON c.id = e.canonical_event_id
             WHERE c.event_id = ?`
          )
          .bind(badEventId)
          .first<{
            remaining: number;
          }>();

        return new Response(
          JSON.stringify({
            status: "success",
            action: "repair",
            repaired: true,
            already_repaired: false,
            raw_event_id:
              current.raw_event_id,
            candidate_id:
              current.candidate_id,
            canonical_event_id:
              postRepair.canonical_event_id,
            remaining:
              Number(
                remainingRow?.remaining || 0
              ),
          }),
          {
            status: 200,
            headers: getSecurityHeaders(),
          }
        );
      } catch (repairError: any) {
        console.error(
          "BCA_QRIS_TARGETED_REPAIR_FAILURE"
        );

        return new Response(
          JSON.stringify({
            status: "error",
            code:
              "BCA_QRIS_REPAIR_INTERNAL_ERROR",
            message:
              "Targeted BCA QRIS repair gagal secara fail-closed.",
          }),
          {
            status: 500,
            headers: getSecurityHeaders(),
          }
        );
      }
    }
    // =================================================================
    // 3. TELEGRAM BOT WEBHOOK (X-Telegram-Bot-Api-Secret-Token, Zero Ledger Mutation)
    // =================================================================
    if (path === "/api/telegram/webhook" && method === "POST") {
      const webhookCheck = verifyTelegramWebhook(request, env.TELEGRAM_SECRET_TOKEN);
      if (!webhookCheck.valid) {
        return new Response(
          JSON.stringify({ status: "error", code: "UNAUTHORIZED", message: "Akses webhook Telegram ditolak." }),
          { status: 401, headers: getSecurityHeaders() }
        );
      }

      try {
        const rawBody = await request.text();
        const payload = JSON.parse(rawBody);

        // 3a. Handle Callback Query (Inline Keyboard Review: Approve/Reject)
        if (payload.callback_query) {
          const cb = payload.callback_query;
          const fromUser = cb.from?.id;
          if (!isTelegramUserAllowed(fromUser, env.TELEGRAM_ALLOWED_USER_ID)) {
            return new Response(
              JSON.stringify({ status: "ignored", message: "User tidak diizinkan." }),
              { status: 200, headers: getSecurityHeaders() }
            );
          }

          const callbackData = (cb.data || "").trim();
          const callbackId = cb.id ? String(cb.id) : null;
          const match = callbackData.match(/^(approve|reject):(\d+)$/);

          if (!match) {
            return new Response(
              JSON.stringify({ status: "error", code: "BAD_REQUEST", message: "Format callback tidak dikenal." }),
              { status: 400, headers: getSecurityHeaders() }
            );
          }

          const action = match[1];
          const candId = parseInt(match[2], 10);
          if (isNaN(candId) || candId <= 0) {
            return new Response(
              JSON.stringify({ status: "error", code: "BAD_REQUEST", message: "Candidate ID tidak valid." }),
              { status: 400, headers: getSecurityHeaders() }
            );
          }

          const targetStatus = action === "approve" ? "Approved" : "Rejected";
          const opKey = callbackId ? `tg_cb_${callbackId}` : `tg_cand_${candId}_${targetStatus}_${Date.now()}`;

          // Fast-path guard check for exact callback_id replay
          if (callbackId) {
            try {
              const guardRow = await env.DB.prepare(
                "SELECT callback_id, candidate_id, action FROM telegram_callback_guard WHERE callback_id = ?"
              ).bind(callbackId).first<{ callback_id: string; candidate_id: number; action: string }>();

              if (guardRow) {
                return new Response(
                  JSON.stringify({
                    status: "success",
                    idempotent: true,
                    action: guardRow.action,
                    candidate_id: guardRow.candidate_id,
                    message: "Callback query telah diproses sebelumnya (idempoten).",
                  }),
                  { status: 200, headers: getSecurityHeaders() }
                );
              }
            } catch {}
          }

          // Atomic D1 Transaction via batch()
          try {
            const batchStatements = [];

            if (callbackId) {
              batchStatements.push(
                env.DB.prepare(
                  `INSERT INTO telegram_callback_guard (callback_id, candidate_id, action)
                   SELECT ?, id, ? FROM ingestion_candidates WHERE id = ? AND status = 'Pending'`
                ).bind(callbackId, targetStatus, candId)
              );
            }

            batchStatements.push(
              env.DB.prepare(
                `INSERT INTO ingestion_audit_log (candidate_id, action, actor, details, operation_key)
                 SELECT id, ?, 'telegram_user', 'Reviewed via inline button', ?
                 FROM ingestion_candidates WHERE id = ? AND status = 'Pending'`
              ).bind(targetStatus, opKey, candId)
            );

            batchStatements.push(
              env.DB.prepare(
                "UPDATE ingestion_candidates SET status = ?, reviewed_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'Pending'"
              ).bind(targetStatus, candId)
            );

            const batchResults = await env.DB.batch(batchStatements);
            const updateResult = batchResults[batchResults.length - 1];
            const changes = updateResult?.meta?.changes || 0;

            if (changes === 1) {
              return new Response(
                JSON.stringify({
                  status: "success",
                  action: targetStatus,
                  candidate_id: candId,
                  previous_status: "Pending",
                }),
                { status: 200, headers: getSecurityHeaders() }
              );
            }
          } catch (batchErr: any) {}

          const cand = await env.DB.prepare(
            "SELECT id, status FROM ingestion_candidates WHERE id = ?"
          ).bind(candId).first<{ id: number; status: string }>();

          if (!cand) {
            return new Response(
              JSON.stringify({ status: "error", code: "NOT_FOUND", message: "Kandidat transaksi tidak ditemukan." }),
              { status: 404, headers: getSecurityHeaders() }
            );
          }

          if (cand.status === targetStatus) {
            return new Response(
              JSON.stringify({
                status: "success",
                idempotent: true,
                action: targetStatus,
                candidate_id: candId,
                message: `Kandidat #${candId} sudah berstatus '${targetStatus}' (idempoten).`,
              }),
              { status: 200, headers: getSecurityHeaders() }
            );
          }

          return new Response(
            JSON.stringify({
              status: "error",
              code: "TERMINAL_STATE_LOCKED",
              message: `Status kandidat sudah final ('${cand.status}') dan tidak dapat diubah.`,
              current_status: cand.status,
              candidate_id: candId,
            }),
            { status: 400, headers: getSecurityHeaders() }
          );
        }

        // 3b. Handle Message Input with Canonical Deterministic Idempotency
        if (payload.message) {
          const msg = payload.message;
          const fromUser = msg.from?.id;
          if (!isTelegramUserAllowed(fromUser, env.TELEGRAM_ALLOWED_USER_ID)) {
            return new Response(
              JSON.stringify({ status: "ignored", message: "User tidak diizinkan." }),
              { status: 200, headers: getSecurityHeaders() }
            );
          }

          const text = (msg.text || "").trim();

          // Commands
          if (text === "/status") {
            return new Response(
              JSON.stringify({
                status: "success",
                reply: "Konektor AturUang aktif. Mode: shadow. D1 Staging siap.",
              }),
              { status: 200, headers: getSecurityHeaders() }
            );
          }

          const externalId = payload.update_id
            ? `tg_update_${payload.update_id}`
            : msg.message_id
            ? `tg_msg_${msg.message_id}`
            : null;

          if (!externalId) {
            return new Response(
              JSON.stringify({ status: "error", code: "BAD_REQUEST", message: "Identifier pesan Telegram tidak valid." }),
              { status: 400, headers: getSecurityHeaders() }
            );
          }

          const existing = await env.DB.prepare(
            "SELECT id, state FROM raw_events WHERE source = 'telegram' AND external_id = ?"
          ).bind(externalId).first<{ id: number; state: string }>();

          if (existing) {
            return new Response(
              JSON.stringify({
                status: "success",
                duplicate: true,
                raw_event_id: existing.id,
                message: "Pesan Telegram telah diproses sebelumnya (idempoten).",
              }),
              { status: 200, headers: getSecurityHeaders() }
            );
          }

          const msgDate = msg.date ? new Date(msg.date * 1000).toISOString() : new Date().toISOString();
          const cand = parseTelegramText(text, msgDate);
          const autoStatus = evaluateAutoApproval(cand, true);

          const hashBuf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(rawBody));
          const payloadHash = Array.from(new Uint8Array(hashBuf))
            .map((b) => b.toString(16).padStart(2, "0"))
            .join("");

          const minimalPayload = JSON.stringify({ length: text.length });

          const rawRes = await env.DB.prepare(
            `INSERT INTO raw_events (source, external_id, occurred_at, payload_hash, parser_version, state, minimal_raw_payload)
             VALUES ('telegram', ?, ?, ?, '1.0.0', 'Parsed', ?)`
          ).bind(externalId, msgDate, payloadHash, minimalPayload).run();

          const rawEventId = rawRes.meta.last_row_id;

          const candRes = await env.DB.prepare(
            `INSERT INTO ingestion_candidates
             (raw_event_id, tx_type, amount, account, to_account, category, money_context, person_name, date, time, confidence_score, status, reasons)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
          ).bind(
            rawEventId,
            cand.tx_type,
            cand.amount,
            cand.account,
            cand.to_account || null,
            cand.category,
            cand.money_context,
            cand.person_name || null,
            cand.date,
            cand.time || null,
            cand.confidence_score,
            autoStatus,
            cand.reasons
          ).run();

          await env.DB.prepare(
            `INSERT INTO source_sync_state (source, last_attempt_at, last_success_at, last_event_at, status)
             VALUES ('telegram', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, 'OK')
             ON CONFLICT(source) DO UPDATE SET
               last_attempt_at = CURRENT_TIMESTAMP,
               last_success_at = CURRENT_TIMESTAMP,
               last_event_at = excluded.last_event_at,
               status = 'OK'`
          ).bind(cand.date).run();

          return new Response(
            JSON.stringify({
              status: "success",
              raw_event_id: rawEventId,
              candidate_id: candRes.meta.last_row_id,
              candidate: cand,
            }),
            { status: 200, headers: getSecurityHeaders() }
          );
        }

        return new Response(
          JSON.stringify({ status: "success", message: "Update diterima." }),
          { status: 200, headers: getSecurityHeaders() }
        );
      } catch (err: any) {
        return new Response(
          JSON.stringify({ status: "error", code: "INTERNAL_SERVER_ERROR", message: "Terjadi kesalahan internal pada layanan backend cloud." }),
          { status: 500, headers: getSecurityHeaders() }
        );
      }
    }

    // =================================================================
    // 4. AUTHENTICATION FOR STAGING API (Fail-Closed)
    // =================================================================
    const authError = authenticateRequest(request, env);
    if (authError) {
      return authError;
    }

    // =================================================================
    // 5. STAGING REVIEW API (Atomic D1 Batch Protected & Idempotent)
    // =================================================================
    // GET /api/ingestion/pending
    if (path === "/api/ingestion/pending" && method === "GET") {
      const candidates = await env.DB.prepare(
        "SELECT * FROM ingestion_candidates WHERE status = 'Pending' ORDER BY id DESC"
      ).all();
      return new Response(JSON.stringify(candidates.results), {
        status: 200,
        headers: getSecurityHeaders(),
      });
    }

    // GET /api/canonical/events (Staging inspection / historical audit)
    if (path === "/api/canonical/events" && method === "GET") {
      try {
        const events = await env.DB.prepare(
          "SELECT * FROM canonical_financial_events ORDER BY occurred_at_wib ASC, id ASC"
        ).all();
        return new Response(JSON.stringify(events.results || []), {
          status: 200,
          headers: getSecurityHeaders(),
        });
      } catch {
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: getSecurityHeaders(),
        });
      }
    }

    // POST /api/ingestion/:id/approve (Atomic D1 Batch)
    const approveMatch = path.match(/^\/api\/ingestion\/(\d+)\/approve$/);
    if (approveMatch && method === "POST") {
      const candId = parseInt(approveMatch[1], 10);
      const opKey = `admin_approve_${candId}_${Date.now()}`;

      try {
        const batchRes = await env.DB.batch([
          env.DB.prepare(
            `INSERT INTO ingestion_audit_log (candidate_id, action, actor, details, operation_key)
             SELECT id, 'Approved', 'admin_user', 'Approved in shadow mode (staging only)', ?
             FROM ingestion_candidates WHERE id = ? AND status = 'Pending'`
          ).bind(opKey, candId),
          env.DB.prepare(
            "UPDATE ingestion_candidates SET status = 'Approved', reviewed_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'Pending'"
          ).bind(candId),
        ]);

        const changes = batchRes[1]?.meta?.changes || 0;
        if (changes === 1) {
          return new Response(
            JSON.stringify({
              status: "success",
              candidate_id: candId,
              new_status: "Approved",
              mode: env.MODE || "shadow",
              ledger_updated: false,
            }),
            { status: 200, headers: getSecurityHeaders() }
          );
        }
      } catch (batchErr: any) {}

      const cand = await env.DB.prepare(
        "SELECT id, status FROM ingestion_candidates WHERE id = ?"
      ).bind(candId).first<{ id: number; status: string }>();

      if (!cand) {
        return new Response(
          JSON.stringify({ status: "error", code: "NOT_FOUND", message: "Kandidat tidak ditemukan." }),
          { status: 404, headers: getSecurityHeaders() }
        );
      }

      if (cand.status === "Approved") {
        return new Response(
          JSON.stringify({
            status: "success",
            idempotent: true,
            candidate_id: candId,
            new_status: "Approved",
            mode: env.MODE || "shadow",
            ledger_updated: false,
          }),
          { status: 200, headers: getSecurityHeaders() }
        );
      }

      return new Response(
        JSON.stringify({
          status: "error",
          code: "TERMINAL_STATE_LOCKED",
          message: `Status kandidat sudah final ('${cand.status}') dan tidak dapat diubah.`,
          candidate_id: candId,
        }),
        { status: 400, headers: getSecurityHeaders() }
      );
    }

    // POST /api/ingestion/:id/reject (Atomic D1 Batch)
    const rejectMatch = path.match(/^\/api\/ingestion\/(\d+)\/reject$/);
    if (rejectMatch && method === "POST") {
      const candId = parseInt(rejectMatch[1], 10);
      const opKey = `admin_reject_${candId}_${Date.now()}`;

      try {
        const batchRes = await env.DB.batch([
          env.DB.prepare(
            `INSERT INTO ingestion_audit_log (candidate_id, action, actor, details, operation_key)
             SELECT id, 'Rejected', 'admin_user', 'Rejected in shadow mode (staging only)', ?
             FROM ingestion_candidates WHERE id = ? AND status = 'Pending'`
          ).bind(opKey, candId),
          env.DB.prepare(
            "UPDATE ingestion_candidates SET status = 'Rejected', reviewed_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'Pending'"
          ).bind(candId),
        ]);

        const changes = batchRes[1]?.meta?.changes || 0;
        if (changes === 1) {
          return new Response(
            JSON.stringify({
              status: "success",
              candidate_id: candId,
              new_status: "Rejected",
              mode: env.MODE || "shadow",
              ledger_updated: false,
            }),
            { status: 200, headers: getSecurityHeaders() }
          );
        }
      } catch (batchErr: any) {}

      const cand = await env.DB.prepare(
        "SELECT id, status FROM ingestion_candidates WHERE id = ?"
      ).bind(candId).first<{ id: number; status: string }>();

      if (!cand) {
        return new Response(
          JSON.stringify({ status: "error", code: "NOT_FOUND", message: "Kandidat tidak ditemukan." }),
          { status: 404, headers: getSecurityHeaders() }
        );
      }

      if (cand.status === "Rejected") {
        return new Response(
          JSON.stringify({
            status: "success",
            idempotent: true,
            candidate_id: candId,
            new_status: "Rejected",
            mode: env.MODE || "shadow",
            ledger_updated: false,
          }),
          { status: 200, headers: getSecurityHeaders() }
        );
      }

      return new Response(
        JSON.stringify({
          status: "error",
          code: "TERMINAL_STATE_LOCKED",
          message: `Status kandidat sudah final ('${cand.status}') dan tidak dapat diubah.`,
          candidate_id: candId,
        }),
        { status: 400, headers: getSecurityHeaders() }
      );
    }

    // GET /api/sources/freshness
    if (path === "/api/sources/freshness" && method === "GET") {
      const freshness = await env.DB.prepare("SELECT * FROM source_sync_state").all();
      return new Response(JSON.stringify(freshness.results), {
        status: 200,
        headers: getSecurityHeaders(),
      });
    }

    // =================================================================
    // 6. SHADOW WRITE PROTECTION FOR FINANCIAL MUTATIONS
    // =================================================================
    const isShadow = (env.MODE || "shadow").toLowerCase() === "shadow";
    if (isShadow && method !== "GET" && method !== "HEAD") {
      return new Response(
        JSON.stringify({
          status: "error",
          code: "SHADOW_READ_ONLY",
          message:
            "Backend Cloudflare D1 beroperasi dalam mode shadow read-only. Seluruh mutasi tulis ditolak untuk menjaga integritas.",
        }),
        {
          status: 503,
          headers: getSecurityHeaders(),
        }
      );
    }

    // =================================================================
    // 7. READ API ROUTING (Prompt 13A Staging Parity)
    // =================================================================
    try {
      if (path === "/api/dashboard") {
        const month = url.searchParams.get("month") || undefined;
        const data = await getDashboard(env.DB, month);
        return new Response(JSON.stringify(data), {
          status: 200,
          headers: getSecurityHeaders(),
        });
      }

      if (path === "/api/accounts") {
        const data = await getAccountsList(env.DB);
        return new Response(JSON.stringify(data), {
          status: 200,
          headers: getSecurityHeaders(),
        });
      }

      if (path === "/api/transactions") {
        const limit = Math.min(parseInt(url.searchParams.get("limit") || "50", 10), 500);
        const offset = Math.max(parseInt(url.searchParams.get("offset") || "0", 10), 0);
        const month = url.searchParams.get("month") || undefined;
        const data = await getTransactionsList(env.DB, limit, offset, month);
        return new Response(JSON.stringify(data), {
          status: 200,
          headers: getSecurityHeaders(),
        });
      }

      if (path === "/api/goals") {
        const data = await getGoalsSummary(env.DB);
        return new Response(JSON.stringify(data), {
          status: 200,
          headers: getSecurityHeaders(),
        });
      }

      if (path === "/api/upcoming") {
        const data = await getUpcomingList(env.DB);
        return new Response(JSON.stringify(data), {
          status: 200,
          headers: getSecurityHeaders(),
        });
      }

      if (path === "/api/debts") {
        const data = await getDebtsList(env.DB);
        return new Response(JSON.stringify(data), {
          status: 200,
          headers: getSecurityHeaders(),
        });
      }

      if (path === "/api/reconstruct-balance") {
        const account = url.searchParams.get("account") || "";
        const asOf = url.searchParams.get("as_of") || undefined;
        if (!account) {
          return new Response(
            JSON.stringify({ status: "error", message: "Parameter account diperlukan." }),
            { status: 400, headers: getSecurityHeaders() }
          );
        }
        const data = await reconstructBalance(env.DB, account, asOf);
        return new Response(JSON.stringify(data), {
          status: 200,
          headers: getSecurityHeaders(),
        });
      }

      if (path === "/api/insights" || path === "/api/insights/recurring" || path === "/api/accounts/freshness") {
        const asOf = url.searchParams.get("as_of") || undefined;
        const data = await getInsightsSummary(env.DB, asOf);
        return new Response(JSON.stringify(data), {
          status: 200,
          headers: getSecurityHeaders(),
        });
      }

      // 404 Route Not Found
      return new Response(
        JSON.stringify({
          status: "error",
          code: "NOT_FOUND",
          message: `Endpoint '${path}' tidak ditemukan.`,
        }),
        {
          status: 404,
          headers: getSecurityHeaders(),
        }
      );
    } catch {
      return new Response(
        JSON.stringify({
          status: "error",
          code: "INTERNAL_SERVER_ERROR",
          message: "Terjadi kesalahan internal pada layanan backend cloud.",
        }),
        {
          status: 500,
          headers: getSecurityHeaders(),
        }
      );
    }
  },
};
