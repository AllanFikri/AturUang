/**
 * AturUang — Gmail Relay & Historical Backfill Connector (Google Apps Script)
 * 
 * Membaca notifikasi transaksi bank/e-wallet dari Gmail dengan strict exact-sender filtering,
 * menandatangani payload dengan HMAC-SHA256, dan mengirimkannya ke Worker AturUang (Mode: Shadow).
 * 
 * TIDAK MENGGUNAKAN GOOGLE SHEETS.
 */

// Exact evidence-backed senders
const TRUSTED_SENDER_QUERY = 'from:(bca@bca.co.id OR noreply@jago.com OR no-reply@flip.id OR googleplay-noreply@google.com OR noreply@byu.id OR no-reply@mailer-esb.com OR info@shopee.co.id OR info@mail.shopee.co.id OR noreply@cx.byu.id)';

const TRUSTED_SENDERS_SET = [
  "bca@bca.co.id",
  "noreply@jago.com",
  "no-reply@flip.id",
  "googleplay-noreply@google.com",
  "noreply@byu.id",
  "no-reply@mailer-esb.com",
  "info@shopee.co.id",
  "info@mail.shopee.co.id",
  "noreply@cx.byu.id"
];

function extractCleanEmail(fromHeader) {
  if (!fromHeader) return "";
  const match = fromHeader.match(/<([^>]+)>/);
  const raw = match ? match[1] : fromHeader;
  return raw.trim().toLowerCase();
}

function isSenderExactTrusted(fromHeader) {
  const clean = extractCleanEmail(fromHeader);
  return TRUSTED_SENDERS_SET.indexOf(clean) !== -1;
}

/**
 * 1. Live Periodic Relay (Jalankan via Time-driven trigger tiap 5-15 menit)
 */
function relayGmailTransactions() {
  const props = PropertiesService.getScriptProperties();
  const workerUrl = props.getProperty("WORKER_URL");
  const relaySecret = props.getProperty("GMAIL_RELAY_SECRET");

  if (!workerUrl || !relaySecret) {
    console.error("Konfigurasi WORKER_URL atau GMAIL_RELAY_SECRET belum diisi di Script Properties.");
    return;
  }

  const query = 'newer_than:2d ' + TRUSTED_SENDER_QUERY;
  processGmailQuery(query, workerUrl, relaySecret, 30);
}

/**
 * 2. Historical Backfill (2025-01-01 s.d. Sekarang)
 * Bounded monthly windows, resumable checkpoint di Script Properties.
 */
function backfillGmailTransactions(startYearMonth, endYearMonth) {
  const props = PropertiesService.getScriptProperties();
  const workerUrl = props.getProperty("WORKER_URL");
  const relaySecret = props.getProperty("GMAIL_RELAY_SECRET");

  if (!workerUrl || !relaySecret) {
    console.error("Konfigurasi WORKER_URL atau GMAIL_RELAY_SECRET belum diisi di Script Properties.");
    return;
  }

  const startYM =
    startYearMonth ||
    props.getProperty("GMAIL_BACKFILL_CHECKPOINT") ||
    "2025-01";

  const now = new Date();
  const currentYM =
    now.getFullYear() +
    "-" +
    ("0" + (now.getMonth() + 1)).slice(-2);

  const endYM = endYearMonth || currentYM;

  // Small pages reduce the chance of Apps Script timing out mid-page.
  // Offset is persisted only after a whole page succeeds.
  const PAGE_SIZE = 25;

  // Consumer Apps Script executions have a limited runtime.
  // Stop voluntarily before the hard timeout and continue next run.
  const MAX_RUNTIME_MS = 4 * 60 * 1000;
  const startedAt = Date.now();

  console.log(
    "Memulai Backfill Gmail dari " + startYM + " s.d. " + endYM
  );

  let current = parseYearMonth(startYM);
  const target = parseYearMonth(endYM);
  let totalProcessed = 0;

  while (compareYearMonth(current, target) <= 0) {
    const ymStr = formatYearMonth(current);
    const nextYM = getNextMonth(current);
    const afterDate = ymStr + "-01";
    const beforeDate = formatYearMonth(nextYM) + "-01";

    const query =
      'after:' +
      afterDate +
      ' before:' +
      beforeDate +
      ' ' +
      TRUSTED_SENDER_QUERY;

    // Enforce the monthly window again at message level because
    // Gmail search returns threads, whose messages can cross month bounds.
    const startDate = new Date(
      current.year,
      current.month - 1,
      1,
      0,
      0,
      0,
      0
    );

    const endDate = new Date(
      nextYM.year,
      nextYM.month - 1,
      1,
      0,
      0,
      0,
      0
    );

    const storedOffsetMonth =
      props.getProperty("GMAIL_BACKFILL_OFFSET_MONTH");

    let offset = 0;

    if (storedOffsetMonth === ymStr) {
      const rawOffset =
        props.getProperty("GMAIL_BACKFILL_OFFSET");

      if (
        rawOffset === null ||
        !/^\d+$/.test(rawOffset)
      ) {
        console.error(
          "BACKFILL_INVALID_OFFSET" +
          " | month=" +
          ymStr
        );

        throw new Error(
          "BACKFILL_INVALID_OFFSET"
        );
      }

      offset = parseInt(rawOffset, 10);

      if (
        !Number.isFinite(offset) ||
        offset < 0
      ) {
        console.error(
          "BACKFILL_INVALID_OFFSET" +
          " | month=" +
          ymStr
        );

        throw new Error(
          "BACKFILL_INVALID_OFFSET"
        );
      }
    } else if (storedOffsetMonth) {
      console.error(
        "BACKFILL_OFFSET_MONTH_MISMATCH" +
        " | expected=" +
        ymStr +
        " | stored=" +
        storedOffsetMonth
      );

      throw new Error(
        "BACKFILL_OFFSET_MONTH_MISMATCH"
      );
    } else {
      props.setProperty(
        "GMAIL_BACKFILL_OFFSET_MONTH",
        ymStr
      );

      props.setProperty(
        "GMAIL_BACKFILL_OFFSET",
        "0"
      );
    }

    console.log(
      "Memproses jendela " +
      ymStr +
      " mulai thread offset " +
      offset
    );

    while (true) {
      if (
        Date.now() - startedAt >=
        MAX_RUNTIME_MS
      ) {
        console.log(
          "BACKFILL_RUNTIME_PAUSE" +
          " | month=" +
          ymStr +
          " | offset=" +
          offset
        );

        return;
      }

      const page = processGmailQueryPage(
        query,
        workerUrl,
        relaySecret,
        PAGE_SIZE,
        offset,
        startDate,
        endDate,
        true
      );

      totalProcessed += page.processedCount;

      // Never advance the page if a trusted message failed to relay.
      // Successful messages from this page are safe to retry because
      // Worker ingestion is idempotent by Gmail message ID.
      if (page.failedCount > 0) {
        const failureCode =
          page.failureCode ||
          "BACKFILL_RELAY_FAILURE";

        console.error(
          "BACKFILL_RELAY_STOP" +
          " | month=" +
          ymStr +
          " | offset=" +
          offset +
          " | code=" +
          failureCode +
          (
            page.workerCode
              ? " | worker_code=" +
                page.workerCode
              : ""
          )
        );

        throw new Error(
          failureCode
        );
      }

      // Empty or short page means the month has been exhausted.
      if (
        page.threadCount === 0 ||
        page.threadCount < PAGE_SIZE
      ) {
        props.setProperty(
          "GMAIL_BACKFILL_CHECKPOINT",
          formatYearMonth(nextYM)
        );

        props.deleteProperty("GMAIL_BACKFILL_OFFSET_MONTH");
        props.deleteProperty("GMAIL_BACKFILL_OFFSET");

        console.log(
          "Jendela " +
          ymStr +
          " selesai. Checkpoint bulan berikutnya: " +
          formatYearMonth(nextYM)
        );

        current = nextYM;
        break;
      }

      // The entire page succeeded, so advancing the thread cursor is safe.
      offset += page.threadCount;

      props.setProperty(
        "GMAIL_BACKFILL_OFFSET_MONTH",
        ymStr
      );

      props.setProperty(
        "GMAIL_BACKFILL_OFFSET",
        String(offset)
      );

      console.log(
        "Page " +
        ymStr +
        " selesai. Offset berikutnya: " +
        offset
      );

      // Resume safely on the next manual/triggered execution.
      if (Date.now() - startedAt >= MAX_RUNTIME_MS) {
        console.log(
          "BACKFILL_RUNTIME_PAUSE" +
          " | month=" +
          ymStr +
          " | offset=" +
          offset
        );
        return;
      }
    }

    if (Date.now() - startedAt >= MAX_RUNTIME_MS) {
      console.log(
        "BACKFILL_RUNTIME_PAUSE" +
        " | next_checkpoint=" +
        formatYearMonth(current)
      );
      return;
    }
  }

  console.log(
    "BACKFILL_COMPLETED" +
    " | through=" +
    endYM +
    " | next_checkpoint=" +
    formatYearMonth(current) +
    " | successful_messages_this_run=" +
    totalProcessed
  );
}

/**
 * Historical Backfill v2.
 *
 * Resume dari checkpoint resmi dan proses closed historical
 * months sampai 2026-07.
 *
 * FAIL-CLOSED:
 * - concurrent run ditolak;
 * - persistent BLOCK latch setelah error;
 * - Worker harus sehat + MODE=shadow;
 * - schema >= 7;
 * - checkpoint/offset harus konsisten;
 * - current/open month ditolak;
 * - non-200 pertama langsung stop;
 * - network exception pertama langsung stop;
 * - malformed HTTP 200 langsung stop;
 * - runtime pause adalah normal dan resumable.
 */
function backfillHistoricalClosedMonthsV2() {
  const START_YM = "2025-03";
  const END_YM = "2026-07";
  const MIN_SCHEMA_VERSION = 7;

  const props =
    PropertiesService.getScriptProperties();

  const lock =
    LockService.getScriptLock();

  if (!lock.tryLock(1000)) {
    console.error(
      "BACKFILL_ALREADY_RUNNING"
    );
    return;
  }

  try {
    const blockedCode =
      props.getProperty(
        "GMAIL_BACKFILL_V2_BLOCKED_CODE"
      );

    if (blockedCode) {
      console.error(
        "BACKFILL_BLOCKED" +
        " | code=" +
        blockedCode +
        " | clear only after diagnosis"
      );

      return;
    }

    const workerUrl =
      props.getProperty("WORKER_URL");

    const relaySecret =
      props.getProperty(
        "GMAIL_RELAY_SECRET"
      );

    if (!workerUrl || !relaySecret) {
      backfillV2Throw_(
        "BACKFILL_CONFIG_MISSING"
      );
    }

    const checkpoint =
      props.getProperty(
        "GMAIL_BACKFILL_CHECKPOINT"
      );

    if (
      !checkpoint ||
      !isValidYearMonth_(checkpoint)
    ) {
      backfillV2Throw_(
        "BACKFILL_INVALID_CHECKPOINT"
      );
    }

    const checkpointYM =
      parseYearMonth(checkpoint);

    const startYM =
      parseYearMonth(START_YM);

    const cutoffYM =
      parseYearMonth(END_YM);

    if (
      compareYearMonth(
        checkpointYM,
        startYM
      ) < 0
    ) {
      backfillV2Throw_(
        "BACKFILL_INVALID_CHECKPOINT",
        "checkpoint before validated start"
      );
    }

    const completedCheckpoint =
      formatYearMonth(
        getNextMonth(cutoffYM)
      );

    const offsetMonth =
      props.getProperty(
        "GMAIL_BACKFILL_OFFSET_MONTH"
      );

    const rawOffset =
      props.getProperty(
        "GMAIL_BACKFILL_OFFSET"
      );

    if (
      offsetMonth &&
      offsetMonth !== checkpoint
    ) {
      backfillV2Throw_(
        "BACKFILL_OFFSET_MONTH_MISMATCH"
      );
    }

    if (offsetMonth) {
      if (
        rawOffset === null ||
        !/^\d+$/.test(rawOffset)
      ) {
        backfillV2Throw_(
          "BACKFILL_INVALID_OFFSET"
        );
      }
    } else if (rawOffset !== null) {
      backfillV2Throw_(
        "BACKFILL_INVALID_OFFSET",
        "offset exists without offset month"
      );
    }

    if (
      compareYearMonth(
        checkpointYM,
        cutoffYM
      ) > 0
    ) {
      if (
        checkpoint ===
          completedCheckpoint &&
        !offsetMonth &&
        rawOffset === null
      ) {
        console.log(
          "BACKFILL_COMPLETED" +
          " | through=" +
          END_YM +
          " | checkpoint=" +
          checkpoint
        );

        return;
      }

      backfillV2Throw_(
        "BACKFILL_INVALID_CHECKPOINT",
        "checkpoint beyond expected completion"
      );
    }

    const scriptTimezone =
      Session.getScriptTimeZone();

    if (
      scriptTimezone !==
      "Asia/Jakarta"
    ) {
      backfillV2Throw_(
        "BACKFILL_TIMEZONE_MISMATCH"
      );
    }

    const currentYM =
      Utilities.formatDate(
        new Date(),
        "Asia/Jakarta",
        "yyyy-MM"
      );

    if (
      compareYearMonth(
        cutoffYM,
        parseYearMonth(currentYM)
      ) >= 0
    ) {
      backfillV2Throw_(
        "BACKFILL_CUTOFF_NOT_CLOSED"
      );
    }

    const healthUrl =
      workerUrl.replace(/\/+$/, "") +
      "/health";

    let healthResponse;

    try {
      healthResponse =
        UrlFetchApp.fetch(
          healthUrl,
          {
            method: "get",
            muteHttpExceptions: true,
          }
        );
    } catch (healthError) {
      backfillV2Throw_(
        "BACKFILL_HEALTH_FETCH_EXCEPTION"
      );
    }

    const healthCode =
      healthResponse.getResponseCode();

    if (healthCode !== 200) {
      backfillV2Throw_(
        "BACKFILL_HEALTH_HTTP_FAILURE",
        "HTTP " + healthCode
      );
    }

    let health;

    try {
      health = JSON.parse(
        healthResponse.getContentText()
      );
    } catch (parseError) {
      backfillV2Throw_(
        "BACKFILL_INVALID_HEALTH_RESPONSE"
      );
    }

    if (
      !health ||
      health.status !== "ok"
    ) {
      backfillV2Throw_(
        "BACKFILL_INVALID_HEALTH_RESPONSE"
      );
    }

    if (
      String(
        health.mode || ""
      ).toLowerCase() !== "shadow"
    ) {
      backfillV2Throw_(
        "BACKFILL_WORKER_NOT_SHADOW"
      );
    }

    const schemaVersion =
      Number(
        health.schema_version
      );

    if (
      !Number.isFinite(schemaVersion) ||
      schemaVersion <
        MIN_SCHEMA_VERSION
    ) {
      backfillV2Throw_(
        "BACKFILL_SCHEMA_TOO_OLD"
      );
    }

    console.log(
      "BACKFILL_V2_START" +
      " | checkpoint=" +
      checkpoint +
      " | cutoff=" +
      END_YM +
      " | mode=shadow" +
      " | schema=" +
      schemaVersion
    );

    return backfillGmailTransactions(
      checkpoint,
      END_YM
    );
  } catch (e) {
    const rawMessage =
      String(
        e && e.message
          ? e.message
          : e || ""
      );

    const codeMatch =
      rawMessage.match(
        /BACKFILL_[A-Z0-9_]+/
      );

    const safeCode =
      codeMatch
        ? codeMatch[0]
        : "BACKFILL_UNEXPECTED_EXCEPTION";

    props.setProperty(
      "GMAIL_BACKFILL_V2_BLOCKED_CODE",
      safeCode
    );

    props.setProperty(
      "GMAIL_BACKFILL_V2_BLOCKED_AT",
      new Date().toISOString()
    );

    console.error(
      "BACKFILL_V2_BLOCKED" +
      " | code=" +
      safeCode
    );

    throw new Error(
      safeCode
    );
  } finally {
    lock.releaseLock();
  }
}


/**
 * HANYA jalankan setelah error sudah didiagnosis.
 *
 * Ini hanya menghapus circuit-breaker latch.
 * Checkpoint dan offset TIDAK direset.
 */
function clearHistoricalBackfillV2Block() {
  const props =
    PropertiesService.getScriptProperties();

  const previousCode =
    props.getProperty(
      "GMAIL_BACKFILL_V2_BLOCKED_CODE"
    );

  if (!previousCode) {
    console.log(
      "BACKFILL_BLOCK_CLEAR_NOOP"
    );
    return;
  }

  props.deleteProperty(
    "GMAIL_BACKFILL_V2_BLOCKED_CODE"
  );

  props.deleteProperty(
    "GMAIL_BACKFILL_V2_BLOCKED_AT"
  );

  console.log(
    "BACKFILL_BLOCK_CLEARED" +
    " | previous_code=" +
    previousCode +
    " | checkpoint and offset preserved"
  );
}


function backfillV2Throw_(
  code,
  detail
) {
  console.error(
    code +
    (
      detail
        ? " | " + detail
        : ""
    )
  );

  throw new Error(code);
}


function isValidYearMonth_(value) {
  return /^\d{4}-(0[1-9]|1[0-2])$/.test(
    String(value || "")
  );
}

function getBackfillStatus() {
  const props = PropertiesService.getScriptProperties();

  const checkpoint =
    props.getProperty("GMAIL_BACKFILL_CHECKPOINT") ||
    "Belum dimulai (default 2025-01)";

  const offsetMonth =
    props.getProperty("GMAIL_BACKFILL_OFFSET_MONTH");

  const offset =
    props.getProperty("GMAIL_BACKFILL_OFFSET") || "0";

  const status = offsetMonth
    ? checkpoint +
      " | sedang memproses " +
      offsetMonth +
      " dari thread offset " +
      offset
    : checkpoint;

  console.log(
    "Status Checkpoint Backfill Saat Ini: " + status
  );

  return status;
}
function resetBackfillCheckpoint() {
  const props = PropertiesService.getScriptProperties();

  props.deleteProperty("GMAIL_BACKFILL_CHECKPOINT");
  props.deleteProperty("GMAIL_BACKFILL_OFFSET_MONTH");
  props.deleteProperty("GMAIL_BACKFILL_OFFSET");

  console.log(
    "Checkpoint dan offset backfill telah direset ke default 2025-01."
  );
}
/**
 * Core processor loop (Message-level inspection)
 */
function processGmailQuery(
  query,
  workerUrl,
  relaySecret,
  maxThreads,
  startDate,
  endDate
) {
  const page = processGmailQueryPage(
    query,
    workerUrl,
    relaySecret,
    maxThreads,
    0,
    startDate,
    endDate
  );

  return page.processedCount;
}


/**
 * Process one Gmail thread page.
 *
 * Historical backfill supplies a non-zero thread offset.
 * Live relay uses processGmailQuery(), which always starts at offset 0.
 */
function processGmailQueryPage(
  query,
  workerUrl,
  relaySecret,
  maxThreads,
  startOffset,
  startDate,
  endDate,
  failFast
) {
  const threads = GmailApp.search(
    query,
    startOffset || 0,
    maxThreads
  );

  let processedCount = 0;
  let failedCount = 0;

  for (let i = 0; i < threads.length; i++) {
    const messages = threads[i].getMessages();

    for (let j = 0; j < messages.length; j++) {
      const msg = messages[j];
      const messageDate = msg.getDate();

      // Historical backfill: inclusive start, exclusive end.
      // Live relay leaves startDate/endDate undefined.
      if (startDate && messageDate < startDate) {
        continue;
      }

      if (endDate && messageDate >= endDate) {
        continue;
      }

      const fromHeader = msg.getFrom();

      // Enforce message-level strict sender trust.
      if (!isSenderExactTrusted(fromHeader)) {
        continue;
      }

      const messageId = msg.getId();

      const payload = {
        message_id: messageId,
        from: fromHeader,
        subject: msg.getSubject(),
        body: msg.getPlainBody().substring(0, 2000),
        internal_date: messageDate.toISOString(),
      };

      const rawBody = JSON.stringify(payload);
      const timestamp = Date.now().toString();
      const nonce = Utilities.getUuid();

      const toSign =
        timestamp +
        "." +
        nonce +
        "." +
        rawBody;

      const signatureBytes =
        Utilities.computeHmacSha256Signature(
          toSign,
          relaySecret,
          Utilities.Charset.UTF_8
        );

      const signature = signatureBytes
        .map(function(byte) {
          return (
            "0" +
            (byte & 0xFF).toString(16)
          ).slice(-2);
        })
        .join("");

      const options = {
        method: "post",
        contentType: "application/json",
        payload: rawBody,
        headers: {
          "X-Signature": signature,
          "X-Timestamp": timestamp,
          "X-Nonce": nonce,
        },
        muteHttpExceptions: true,
      };

      try {
        const response =
          UrlFetchApp.fetch(
            workerUrl +
              "/api/ingest/gmail",
            options
          );

        const code =
          response.getResponseCode();

        if (code === 200) {
          if (failFast) {
            let workerResult;

            try {
              workerResult =
                JSON.parse(
                  response.getContentText()
                );
            } catch (parseError) {
              failedCount++;

              console.error(
                "BACKFILL_INVALID_WORKER_RESPONSE" +
                " | HTTP 200" +
                " | Gmail message ID " +
                messageId
              );

              return {
                processedCount:
                  processedCount,
                threadCount:
                  threads.length,
                failedCount:
                  failedCount,
                failureCode:
                  "BACKFILL_INVALID_WORKER_RESPONSE",
                workerCode: null,
                httpStatus: 200,
                messageId: messageId,
              };
            }

            if (
              !workerResult ||
              workerResult.status !==
                "success"
            ) {
              failedCount++;

              console.error(
                "BACKFILL_INVALID_WORKER_RESPONSE" +
                " | HTTP 200" +
                " | Gmail message ID " +
                messageId
              );

              return {
                processedCount:
                  processedCount,
                threadCount:
                  threads.length,
                failedCount:
                  failedCount,
                failureCode:
                  "BACKFILL_INVALID_WORKER_RESPONSE",
                workerCode: null,
                httpStatus: 200,
                messageId: messageId,
              };
            }
          }

          processedCount++;
        } else {
          failedCount++;

          let workerCode =
            "UNKNOWN_WORKER_ERROR";

          try {
            const workerError =
              JSON.parse(
                response.getContentText()
              );

            workerCode =
              workerError.code ||
              "NO_ERROR_CODE";
          } catch (parseError) {
            workerCode =
              "UNPARSEABLE_ERROR_RESPONSE";
          }

          console.error(
            "BACKFILL_HTTP_FAILURE" +
            " | HTTP " +
            code +
            " | Worker code=" +
            workerCode +
            " | Gmail message ID " +
            messageId
          );

          if (failFast) {
            return {
              processedCount:
                processedCount,
              threadCount:
                threads.length,
              failedCount:
                failedCount,
              failureCode:
                "BACKFILL_HTTP_FAILURE",
              workerCode:
                workerCode,
              httpStatus:
                code,
              messageId:
                messageId,
            };
          }
        }
      } catch (e) {
        failedCount++;

        console.error(
          "BACKFILL_FETCH_EXCEPTION" +
          " | Gmail message ID " +
          messageId +
          " | " +
          e.toString()
        );

        if (failFast) {
          return {
            processedCount:
              processedCount,
            threadCount:
              threads.length,
            failedCount:
              failedCount,
            failureCode:
              "BACKFILL_FETCH_EXCEPTION",
            workerCode: null,
            httpStatus: null,
            messageId: messageId,
          };
        }
      }
    }
  }

  return {
    processedCount: processedCount,
    threadCount: threads.length,
    failedCount: failedCount,
    failureCode: null,
    workerCode: null,
    httpStatus: null,
    messageId: null,
  };
}
// Date helpers
function parseYearMonth(ymStr) {
  const parts = ymStr.split("-");
  return { year: parseInt(parts[0], 10), month: parseInt(parts[1], 10) };
}

function formatYearMonth(ym) {
  return ym.year + "-" + ("0" + ym.month).slice(-2);
}

function getNextMonth(ym) {
  if (ym.month === 12) {
    return { year: ym.year + 1, month: 1 };
  }
  return { year: ym.year, month: ym.month + 1 };
}

function compareYearMonth(a, b) {
  if (a.year !== b.year) return a.year - b.year;
  return a.month - b.month;
}
