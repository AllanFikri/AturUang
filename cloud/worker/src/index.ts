// index.ts: Cloudflare Worker Entrypoint untuk AturUang (Shadow Mode & Ingestion Connectors)
import {
  Env,
  authenticateRequest,
  getSecurityHeaders,
  verifyGmailHmac,
  verifyTelegramWebhook,
  isTelegramUserAllowed,
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
  parseGmailNotification,
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
        const verRow = await env.DB.prepare(
          "SELECT MAX(version) as ver FROM schema_migrations"
        ).first<{ ver: number }>();
        schemaVer = verRow?.ver || 0;
      } catch {
        schemaVer = 1;
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
    // 2. GMAIL INGESTION RELAY (HMAC-SHA256, Nonce Guard, Privacy First)
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

        const now = getWibDate().dateStr;

        // Compute actual deterministic SHA-256 hash of payload
        const hashBuf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(rawBody));
        const payloadHash = Array.from(new Uint8Array(hashBuf))
          .map((b) => b.toString(16).padStart(2, "0"))
          .join("");

        // Idempotency check on raw_events
        const existing = await env.DB.prepare(
          "SELECT id, state FROM raw_events WHERE source = 'gmail' AND external_id = ?"
        ).bind(message_id).first<{ id: number; state: string }>();

        if (existing) {
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

        // Extract sanitized sender domain (e.g. bca.co.id) instead of raw personal email
        const senderDomain = String(from || "").includes("@")
          ? String(from).split("@")[1].replace(/[>]/g, "").trim().toLowerCase()
          : "unknown";

        // Parse candidate in-memory
        const cand = parseGmailNotification(subject || "", body || "", from || "", internal_date);
        const autoStatus = evaluateAutoApproval(cand, true);
        const isBca = senderDomain.includes("bca") || String(subject || "").toLowerCase().includes("bca");

        // Minimal sanitized non-PII payload: zero raw email, zero raw body, zero sensitive subject
        const minimalPayload = JSON.stringify({
          sender_domain: senderDomain,
          parser_adapter: isBca ? "bca_alert" : "generic",
          detected_type: cand.tx_type,
          confidence: cand.confidence_score,
        });

        // Insert raw event
        const rawRes = await env.DB.prepare(
          `INSERT INTO raw_events (source, external_id, occurred_at, payload_hash, parser_version, state, minimal_raw_payload)
           VALUES ('gmail', ?, ?, ?, '1.0.0', 'Parsed', ?)`
        ).bind(message_id, internal_date || now, payloadHash, minimalPayload).run();

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
          cand.to_account,
          cand.category,
          cand.money_context,
          cand.person_name,
          cand.date,
          cand.time,
          cand.confidence_score,
          autoStatus,
          cand.reasons
        ).run();

        const candidateId = candRes.meta.last_row_id;

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
    // 3. TELEGRAM BOT WEBHOOK (Atomic Durable Idempotency)
    // =================================================================
    if (path === "/api/telegram/webhook" && method === "POST") {
      const secCheck = verifyTelegramWebhook(request, env.TELEGRAM_SECRET_TOKEN);
      if (!secCheck.valid) {
        return new Response(
          JSON.stringify({ status: "error", code: "UNAUTHORIZED", message: "Secret token Telegram tidak valid." }),
          { status: 401, headers: getSecurityHeaders() }
        );
      }

      try {
        const payload = await request.json<any>();

        // 3a. Handle Callback Query (Inline Keyboard Approve/Reject) with Atomic Durable Guard
        if (payload.callback_query) {
          const cb = payload.callback_query;
          const fromUser = cb.from?.id;
          if (!isTelegramUserAllowed(fromUser, env.TELEGRAM_ALLOWED_USER_ID)) {
            return new Response(
              JSON.stringify({ status: "ignored", message: "User tidak diizinkan." }),
              { status: 200, headers: getSecurityHeaders() }
            );
          }

          const callbackId = cb.id ? String(cb.id) : null;
          const data = String(cb.data || "");
          const [action, candIdStr] = data.split(":");
          const candId = parseInt(candIdStr, 10);

          if (!candId || (action !== "approve" && action !== "reject")) {
            return new Response(
              JSON.stringify({ status: "error", code: "BAD_REQUEST", message: "Format aksi callback tidak valid." }),
              { status: 400, headers: getSecurityHeaders() }
            );
          }

          const targetStatus = action === "approve" ? "Approved" : "Rejected";

          // Step 1: Check Callback Guard for exact duplicate callback_id replay
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
            } catch {
              // Abaikan jika tabel guard belum termigrasi
            }
          }

          // Step 2: Atomic conditional state transition (WHERE status = 'Pending')
          const updateRes = await env.DB.prepare(
            "UPDATE ingestion_candidates SET status = ?, reviewed_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'Pending'"
          ).bind(targetStatus, candId).run();

          const changes = updateRes.meta?.changes || 0;

          if (changes === 1) {
            // State transition succeeded exactly once!
            if (callbackId) {
              try {
                await env.DB.prepare(
                  "INSERT INTO telegram_callback_guard (callback_id, candidate_id, action) VALUES (?, ?, ?)"
                ).bind(callbackId, candId, targetStatus).run();
              } catch {
                // Abaikan jika tabel guard belum ada
              }
            }

            await env.DB.prepare(
              "INSERT INTO ingestion_audit_log (candidate_id, action, actor, details) VALUES (?, ?, 'telegram_user', 'Reviewed via inline button')"
            ).bind(candId, targetStatus).run();

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

          // Step 3: If conditional update affected 0 rows, evaluate current state (DO NOT INSERT AUDIT)
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

          // Cross-terminal transition attempt (Approved -> Rejected or vice versa) -> Locked
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

          // Deterministic External ID from Telegram payload (update_id or message_id)
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

          // Idempotency check on raw_events
          const existing = await env.DB.prepare(
            "SELECT id, state FROM raw_events WHERE source = 'telegram' AND external_id = ?"
          ).bind(externalId).first<{ id: number; state: string }>();

          if (existing) {
            const existingCand = await env.DB.prepare(
              "SELECT id, status FROM ingestion_candidates WHERE raw_event_id = ?"
            ).bind(existing.id).first<{ id: number; status: string }>();

            return new Response(
              JSON.stringify({
                status: "success",
                duplicate: true,
                raw_event_id: existing.id,
                candidate_id: existingCand?.id || null,
                message: "Pesan Telegram telah diproses sebelumnya (idempoten).",
              }),
              { status: 200, headers: getSecurityHeaders() }
            );
          }

          // Compute deterministic SHA-256
          const hashBuf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
          const textHash = Array.from(new Uint8Array(hashBuf))
            .map((b) => b.toString(16).padStart(2, "0"))
            .join("");

          // Insert raw event
          const rawRes = await env.DB.prepare(
            `INSERT INTO raw_events (source, external_id, occurred_at, payload_hash, parser_version, state, minimal_raw_payload)
             VALUES ('telegram', ?, CURRENT_TIMESTAMP, ?, '1.0.0', 'Parsed', ?)`
          ).bind(externalId, textHash, JSON.stringify({ length: text.length })).run();

          const rawEventId = rawRes.meta.last_row_id;
          const cand = parseTelegramText(text);

          const candRes = await env.DB.prepare(
            `INSERT INTO ingestion_candidates
             (raw_event_id, tx_type, amount, account, to_account, category, money_context, person_name, date, time, confidence_score, status, reasons)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
          ).bind(
            rawEventId,
            cand.tx_type,
            cand.amount,
            cand.account,
            cand.to_account,
            cand.category,
            cand.money_context,
            cand.person_name,
            cand.date,
            cand.time,
            cand.confidence_score,
            cand.status,
            cand.reasons
          ).run();

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
    // 5. STAGING REVIEW API (Admin Token Protected & Idempotent)
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

    // POST /api/ingestion/:id/approve
    const approveMatch = path.match(/^\/api\/ingestion\/(\d+)\/approve$/);
    if (approveMatch && method === "POST") {
      const candId = parseInt(approveMatch[1], 10);
      const updateRes = await env.DB.prepare(
        "UPDATE ingestion_candidates SET status = 'Approved', reviewed_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'Pending'"
      ).bind(candId).run();

      const changes = updateRes.meta?.changes || 0;

      if (changes === 1) {
        await env.DB.prepare(
          "INSERT INTO ingestion_audit_log (candidate_id, action, actor, details) VALUES (?, 'Approved', 'admin_user', 'Approved in shadow mode (staging only)')"
        ).bind(candId).run();

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
          message: "Status kandidat sudah final ('Rejected') dan tidak dapat diubah.",
          candidate_id: candId,
        }),
        { status: 400, headers: getSecurityHeaders() }
      );
    }

    // POST /api/ingestion/:id/reject
    const rejectMatch = path.match(/^\/api\/ingestion\/(\d+)\/reject$/);
    if (rejectMatch && method === "POST") {
      const candId = parseInt(rejectMatch[1], 10);
      const updateRes = await env.DB.prepare(
        "UPDATE ingestion_candidates SET status = 'Rejected', reviewed_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'Pending'"
      ).bind(candId).run();

      const changes = updateRes.meta?.changes || 0;

      if (changes === 1) {
        await env.DB.prepare(
          "INSERT INTO ingestion_audit_log (candidate_id, action, actor, details) VALUES (?, 'Rejected', 'admin_user', 'Rejected by admin')"
        ).bind(candId).run();

        return new Response(
          JSON.stringify({
            status: "success",
            candidate_id: candId,
            new_status: "Rejected",
          }),
          { status: 200, headers: getSecurityHeaders() }
        );
      }

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
          }),
          { status: 200, headers: getSecurityHeaders() }
        );
      }

      return new Response(
        JSON.stringify({
          status: "error",
          code: "TERMINAL_STATE_LOCKED",
          message: "Status kandidat sudah final ('Approved') dan tidak dapat diubah.",
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
    } catch (err: any) {
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
