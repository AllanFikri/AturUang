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

    // CORS Preflight
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
    // 2. GMAIL INGESTION RELAY (HMAC-SHA256 Protected)
    // =================================================================
    if (path === "/api/ingest/gmail" && method === "POST") {
      const rawBody = await request.text();
      const secret = env.GMAIL_RELAY_SECRET || "aturuang_gmail_relay_secret_default";

      const hmacCheck = await verifyGmailHmac(request, rawBody, secret);
      if (!hmacCheck.valid) {
        return new Response(
          JSON.stringify({
            status: "error",
            code: hmacCheck.error || "HMAC_VERIFICATION_FAILED",
            message: "Otentikasi Gmail relay gagal.",
          }),
          { status: 401, headers: getSecurityHeaders() }
        );
      }

      try {
        const payload = JSON.parse(rawBody);
        const { message_id, from, subject, body, internal_date } = payload;

        if (!message_id) {
          return new Response(
            JSON.stringify({ status: "error", message: "message_id wajib disertakan." }),
            { status: 400, headers: getSecurityHeaders() }
          );
        }

        const now = getWibDate().dateStr;

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

        // Insert raw event
        const rawRes = await env.DB.prepare(
          `INSERT INTO raw_events (source, external_id, occurred_at, payload_hash, parser_version, state, minimal_raw_payload)
           VALUES ('gmail', ?, ?, ?, '1.0.0', 'Parsed', ?)`
        ).bind(message_id, internal_date || now, "sha256_hash", JSON.stringify({ from, subject })).run();

        const rawEventId = rawRes.meta.last_row_id;

        // Parse candidate
        const cand = parseGmailNotification(subject || "", body || "", from || "", internal_date);
        const autoStatus = evaluateAutoApproval(cand, true);

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
          JSON.stringify({ status: "error", code: "INTERNAL_SERVER_ERROR", message: `Gagal memproses email: ${err.message}` }),
          { status: 500, headers: getSecurityHeaders() }
        );
      }
    }

    // =================================================================
    // 3. TELEGRAM BOT WEBHOOK
    // =================================================================
    if (path === "/api/telegram/webhook" && method === "POST") {
      const secCheck = verifyTelegramWebhook(request, env.TELEGRAM_SECRET_TOKEN);
      if (!secCheck.valid) {
        return new Response(
          JSON.stringify({ status: "error", message: "Secret token Telegram tidak valid." }),
          { status: 401, headers: getSecurityHeaders() }
        );
      }

      try {
        const payload = await request.json<any>();

        // Handle Callback Query (Inline Keyboard Approve/Reject)
        if (payload.callback_query) {
          const cb = payload.callback_query;
          const fromUser = cb.from?.id;
          if (!isTelegramUserAllowed(fromUser, env.TELEGRAM_ALLOWED_USER_ID)) {
            return new Response(
              JSON.stringify({ status: "ignored", message: "User tidak diizinkan." }),
              { status: 200, headers: getSecurityHeaders() }
            );
          }

          const data = String(cb.data || "");
          const [action, candIdStr] = data.split(":");
          const candId = parseInt(candIdStr, 10);

          if (candId && (action === "approve" || action === "reject")) {
            const newStatus = action === "approve" ? "Approved" : "Rejected";
            await env.DB.prepare(
              "UPDATE ingestion_candidates SET status = ?, reviewed_at = CURRENT_TIMESTAMP WHERE id = ?"
            ).bind(newStatus, candId).run();

            await env.DB.prepare(
              "INSERT INTO ingestion_audit_log (candidate_id, action, actor, details) VALUES (?, ?, 'telegram_user', 'Reviewed via inline button')"
            ).bind(candId, newStatus).run();

            return new Response(
              JSON.stringify({ status: "success", action: newStatus, candidate_id: candId }),
              { status: 200, headers: getSecurityHeaders() }
            );
          }
        }

        // Handle Message Input
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
          const msgId = msg.message_id ? String(msg.message_id) : `tg_${Date.now()}`;

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

          // Handle structured text
          const rawRes = await env.DB.prepare(
            `INSERT INTO raw_events (source, external_id, occurred_at, payload_hash, parser_version, state, minimal_raw_payload)
             VALUES ('telegram', ?, CURRENT_TIMESTAMP, 'tg_hash', '1.0.0', 'Parsed', ?)`
          ).bind(msgId, JSON.stringify({ text })).run();

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
          JSON.stringify({ status: "error", code: "INTERNAL_SERVER_ERROR", message: err.message }),
          { status: 500, headers: getSecurityHeaders() }
        );
      }
    }

    // =================================================================
    // 4. AUTHENTICATION FOR STAGING API
    // =================================================================
    const authError = authenticateRequest(request, env);
    if (authError) {
      return authError;
    }

    // =================================================================
    // 5. STAGING REVIEW API (Admin Token Protected)
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
      // In MODE=shadow: update review status in staging, DO NOT mutate transactions ledger!
      await env.DB.prepare(
        "UPDATE ingestion_candidates SET status = 'Approved', reviewed_at = CURRENT_TIMESTAMP WHERE id = ?"
      ).bind(candId).run();

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

    // POST /api/ingestion/:id/reject
    const rejectMatch = path.match(/^\/api\/ingestion\/(\d+)\/reject$/);
    if (rejectMatch && method === "POST") {
      const candId = parseInt(rejectMatch[1], 10);
      await env.DB.prepare(
        "UPDATE ingestion_candidates SET status = 'Rejected', reviewed_at = CURRENT_TIMESTAMP WHERE id = ?"
      ).bind(candId).run();

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
