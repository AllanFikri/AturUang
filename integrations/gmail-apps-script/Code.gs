/**
 * AturUang — Gmail Relay & Historical Backfill Connector (Google Apps Script)
 * 
 * Membaca notifikasi transaksi bank/e-wallet dari Gmail dengan strict exact-sender filtering,
 * menandatangani payload dengan HMAC-SHA256, dan mengirimkannya ke Worker AturUang (Mode: Shadow).
 * 
 * TIDAK MENGGUNAKAN GOOGLE SHEETS.
 */

// Exact evidence-backed senders
const TRUSTED_SENDER_QUERY = 'from:(bca@bca.co.id OR noreply@jago.com OR no-reply@flip.id OR googleplay-noreply@google.com OR noreply@byu.id OR no-reply@mailer-esb.com OR info@shopee.co.id OR info@mail.shopee.co.id OR noreply@cx.byu.id OR noreply@stockbit.com OR contactus@stockbit.com)';

const TRUSTED_SENDERS_SET = [
  "bca@bca.co.id",
  "noreply@jago.com",
  "no-reply@flip.id",
  "googleplay-noreply@google.com",
  "noreply@byu.id",
  "no-reply@mailer-esb.com",
  "info@shopee.co.id",
  "info@mail.shopee.co.id",
  "noreply@cx.byu.id",
  "noreply@stockbit.com",
  "contactus@stockbit.com"
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
 * 1b. Sync Current Month Emails
 * Menyelaraskan seluruh email transaksi bulan berjalan (tanggal 1 s.d. hari ini).
 */
function syncCurrentMonthEmails() {
  const props = PropertiesService.getScriptProperties();
  const workerUrl = props.getProperty("WORKER_URL");
  const relaySecret = props.getProperty("GMAIL_RELAY_SECRET");

  if (!workerUrl || !relaySecret) {
    throw new Error("GMAIL_RELAY_CONFIG_MISSING");
  }

  const now = new Date();
  const year = now.getFullYear();
  const month = ("0" + (now.getMonth() + 1)).slice(-2);
  const startOfMonth = year + "/" + month + "/01";

  const query = "after:" + startOfMonth + " " + TRUSTED_SENDER_QUERY;
  console.log("Menyelaraskan email bulan berjalan dengan query: " + query);
  processGmailQuery(query, workerUrl, relaySecret, 50);
}

const PDF_SYNC_PROP_PREFIX = "PDF_MSG_";
const PDF_SYNC_INDEX_KEY = "PDF_SYNC_PROCESSED_INDEX";
const MAX_PDF_SYNC_INDEX_ENTRIES = 200;

function getPdfSyncMessageState_(props, msgId) {
  const raw = props.getProperty(PDF_SYNC_PROP_PREFIX + msgId);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") {
      throw new Error("PDF_SYNC_CORRUPT_STATE");
    }
    return parsed;
  } catch (e) {
    throw new Error("PDF_SYNC_CORRUPT_STATE");
  }
}

function savePdfSyncMessageState_(props, msgId, state) {
  props.setProperty(PDF_SYNC_PROP_PREFIX + msgId, JSON.stringify(state));

  // Maintain bounded FIFO index of processed message keys to avoid unbounded property growth
  try {
    let indexList = [];
    const rawIndex = props.getProperty(PDF_SYNC_INDEX_KEY);
    if (rawIndex) {
      indexList = JSON.parse(rawIndex);
    }
    if (!Array.isArray(indexList)) {
      indexList = [];
    }
    if (indexList.indexOf(msgId) === -1) {
      indexList.push(msgId);
      if (indexList.length > MAX_PDF_SYNC_INDEX_ENTRIES) {
        const toPrune = indexList.splice(0, indexList.length - MAX_PDF_SYNC_INDEX_ENTRIES);
        for (let i = 0; i < toPrune.length; i++) {
          props.deleteProperty(PDF_SYNC_PROP_PREFIX + toPrune[i]);
        }
      }
      props.setProperty(PDF_SYNC_INDEX_KEY, JSON.stringify(indexList));
    }
  } catch (e) {
    // Non-fatal if index maintenance fails, state is already persisted
  }
}

/**
 * 1c. Sync PDF Attachments to Google Drive
 * Mengunggah lampiran PDF dari sender terpercaya ke folder Drive terkonfigurasi secara aman dan idempoten.
 */
function syncEmailPdfAttachmentsToDrive() {
  const props = PropertiesService.getScriptProperties();
  const folderId = props.getProperty("PDF_DRIVE_FOLDER_ID");

  if (!folderId || !folderId.trim()) {
    throw new Error("PDF_FOLDER_CONFIG_MISSING");
  }

  // Concurrency lock: prevent duplicate execution and race conditions
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) {
    throw new Error("PDF_SYNC_CONCURRENT_LOCK_FAILED");
  }

  let folder;
  try {
    try {
      folder = DriveApp.getFolderById(folderId.trim());
    } catch (e) {
      throw new Error("PDF_DRIVE_FOLDER_NOT_FOUND");
    }

    if (!folder) {
      throw new Error("PDF_DRIVE_FOLDER_NOT_FOUND");
    }

    const MAX_PDF_SIZE_BYTES = 15 * 1024 * 1024; // 15MB
    const MAX_ATTACHMENTS_PER_MSG = 5;
    const PDF_SYNC_LABEL = "aturuang/pdf-synced";

    let pdfLabel = GmailApp.getUserLabelByName(PDF_SYNC_LABEL);
    if (!pdfLabel) {
      try {
        pdfLabel = GmailApp.createLabel(PDF_SYNC_LABEL);
      } catch (e) {
        console.warn("Gagal membuat label " + PDF_SYNC_LABEL + ": " + e);
      }
    }

    // Query messages directly from trusted senders without excluding labelled threads,
    // so new messages in existing labelled threads are never skipped.
    const query = "has:attachment filename:pdf newer_than:30d " + TRUSTED_SENDER_QUERY;
    const threads = GmailApp.search(query, 0, 20);

    let totalUploaded = 0;
    let totalSkipped = 0;
    let messagesProcessed = 0;

    for (let t = 0; t < threads.length; t++) {
      const thread = threads[t];
      const messages = thread.getMessages();
      let threadAllMessagesCompleted = true;

      for (let m = 0; m < messages.length; m++) {
        const msg = messages[m];
        const msgId = msg.getId();
        const fromHeader = msg.getFrom();

        // Strict exact-sender validation
        if (!isSenderExactTrusted(fromHeader)) {
          continue;
        }

        // Persistent message-level idempotency source of truth
        let msgState = getPdfSyncMessageState_(props, msgId);
        if (msgState && msgState.status === "COMPLETED") {
          // Entire message has already succeeded in a prior run
          continue;
        }

        messagesProcessed++;
        const attachments = msg.getAttachments({ includeInlineImages: false });

        // Filter PDF attachments
        const pdfAttachments = [];
        for (let a = 0; a < attachments.length; a++) {
          const att = attachments[a];
          const contentType = (att.getContentType() || "").toLowerCase();
          const name = (att.getName() || "").toLowerCase();
          if (contentType.indexOf("application/pdf") !== -1 || name.endsWith(".pdf")) {
            pdfAttachments.push(att);
          }
        }

        if (pdfAttachments.length === 0) {
          // No PDF attachments in this message, mark complete
          savePdfSyncMessageState_(props, msgId, {
            status: "COMPLETED",
            completed_attachments: [],
            completed_at: new Date().toISOString()
          });
          continue;
        }

        let completedAtts = (msgState && Array.isArray(msgState.completed_attachments))
          ? msgState.completed_attachments
          : [];
        let msgAllSuccess = true;
        const totalToProcess = Math.min(pdfAttachments.length, MAX_ATTACHMENTS_PER_MSG);

        for (let pIdx = 0; pIdx < totalToProcess; pIdx++) {
          const att = pdfAttachments[pIdx];
          const pdfCount = pIdx + 1;

          const size = att.getSize();
          if (size > MAX_PDF_SIZE_BYTES) {
            console.warn("OVERSIZED_ATTACHMENT: Ukuran berkas (" + size + " bytes) melebihi batas 15MB untuk msg: " + msgId);
            msgAllSuccess = false;
            continue;
          }

          try {
            // Compute deterministic SHA-256 hash of attachment content
            const digest = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, att.getBytes());
            const hashHex = digest.map(function(b) {
              return ("0" + (b & 0xFF).toString(16)).slice(-2);
            }).join("");
            const hashPrefix = hashHex.slice(0, 12);

            // Deterministic identity: msgId + attachmentIndex + contentHash
            const attIdentity = msgId + "_att" + pdfCount + "_" + hashPrefix;
            const safeFileName = attIdentity + ".pdf";

            // Check if already completed in persistent state (skip re-upload during retry)
            if (completedAtts.indexOf(attIdentity) !== -1) {
              totalSkipped++;
              continue;
            }

            // Check Drive storage idempotency: never duplicate file if Drive upload already succeeded
            const existingFiles = folder.getFilesByName(safeFileName);
            if (existingFiles.hasNext()) {
              totalSkipped++;
            } else {
              folder.createFile(att.copyBlob().setName(safeFileName));
              totalUploaded++;
            }

            // Immediately persist attachment progress so failures or timeouts preserve partial success
            completedAtts.push(attIdentity);
            savePdfSyncMessageState_(props, msgId, {
              status: "PARTIAL",
              completed_attachments: completedAtts,
              updated_at: new Date().toISOString()
            });
          } catch (uploadErr) {
            console.error("Gagal mengunggah lampiran PDF (" + pdfCount + "): " + uploadErr);
            msgAllSuccess = false;
          }
        }

        // Only mark COMPLETED when all intended attachments have succeeded
        if (msgAllSuccess && completedAtts.length >= totalToProcess) {
          savePdfSyncMessageState_(props, msgId, {
            status: "COMPLETED",
            completed_attachments: completedAtts,
            completed_at: new Date().toISOString()
          });
        } else {
          threadAllMessagesCompleted = false;
        }
      }

      // Add label as informational marker only if all messages in thread succeeded
      if (threadAllMessagesCompleted && pdfLabel) {
        thread.addLabel(pdfLabel);
      }
    }

    console.log("Sinkronisasi PDF selesai: diunggah=" + totalUploaded + ", dilewati (idempoten)=" + totalSkipped + ", pesan=" + messagesProcessed);
    return {
      uploaded: totalUploaded,
      skipped: totalSkipped,
      messages_processed: messagesProcessed
    };
  } finally {
    lock.releaseLock();
  }
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

  // Lightweight observability for historical backfill only.
  // Log the first trusted message, then every fifth message.
  let backfillAttemptCount = 0;
  const BACKFILL_PROGRESS_EVERY = 5;

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

      if (failFast) {
        backfillAttemptCount++;

        if (
          backfillAttemptCount === 1 ||
          backfillAttemptCount % BACKFILL_PROGRESS_EVERY === 0
        ) {
          const safeSender =
            extractCleanEmail(fromHeader) || "unknown";

          const safeSubject =
            String(msg.getSubject() || "")
              .replace(/[\r\n\t|]+/g, " ")
              .replace(/\s+/g, " ")
              .trim()
              .slice(0, 72);

          console.log(
            "BACKFILL_PROGRESS" +
            " | page_offset=" +
            (startOffset || 0) +
            " | item=" +
            backfillAttemptCount +
            " | sender=" +
            safeSender +
            " | subject=" +
            (safeSubject || "(no subject)")
          );
        }
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
/**
 * =====================================================================
 * TARGETED BCA QRIS COLLAPSED-CANONICAL REPAIR
 *
 * Manual recovery only.
 *
 * D1 is the checkpoint:
 * - Worker returns only Gmail message IDs whose evidence is still
 *   attached to bca_qris_erensi.
 * - successful repair moves that evidence away;
 * - therefore repaired messages disappear automatically from "next".
 *
 * This does NOT use or modify historical backfill checkpoint/offset.
 * =====================================================================
 */

function repairBcaQrisCanary() {
  return runBcaQrisTargetedRepair_(1);
}


function repairBcaQrisCollapsedCanonical() {
  return runBcaQrisTargetedRepair_(null);
}


function runBcaQrisTargetedRepair_(maxItems) {
  const props =
    PropertiesService.getScriptProperties();

  const workerUrl =
    props.getProperty("WORKER_URL");

  const repairSecret =
    props.getProperty(
      "REPAIR_SECRET"
    );

  if (!workerUrl || !repairSecret) {
    throw new Error(
      "BCA_QRIS_REPAIR_CONFIG_MISSING"
    );
  }

  const lock =
    LockService.getScriptLock();

  if (!lock.tryLock(1000)) {
    throw new Error(
      "BCA_QRIS_REPAIR_ALREADY_RUNNING"
    );
  }

  const startedAt = Date.now();

  // Leave enough room before Apps Script hard timeout.
  const MAX_RUNTIME_MS =
    3.5 * 60 * 1000;

  let repairedThisRun = 0;

  try {
    assertBcaQrisRepairWorkerReady_(
      workerUrl
    );

    while (true) {
      if (
        Date.now() - startedAt >=
        MAX_RUNTIME_MS
      ) {
        console.log(
          "BCA_QRIS_REPAIR_RUNTIME_PAUSE" +
          " | repaired_this_run=" +
          repairedThisRun
        );

        return {
          status: "paused",
          repaired: repairedThisRun,
        };
      }

      if (
        maxItems !== null &&
        repairedThisRun >= maxItems
      ) {
        console.log(
          "BCA_QRIS_REPAIR_CANARY_PASS" +
          " | repaired=" +
          repairedThisRun
        );

        return {
          status: "canary_pass",
          repaired: repairedThisRun,
        };
      }

      const batchLimit =
        maxItems !== null
          ? 1
          : 10;

      const next =
        postBcaQrisRepair_(
          workerUrl,
          repairSecret,
          {
            action: "next",
            limit: batchLimit,
          }
        );

      const remaining =
        Number(next.remaining || 0);

      const messageIds =
        Array.isArray(
          next.message_ids
        )
          ? next.message_ids
          : [];

      if (remaining === 0) {
        console.log(
          "BCA_QRIS_REPAIR_COMPLETED" +
          " | repaired_this_run=" +
          repairedThisRun +
          " | remaining=0"
        );

        return {
          status: "completed",
          repaired:
            repairedThisRun,
          remaining: 0,
        };
      }

      if (messageIds.length === 0) {
        throw new Error(
          "BCA_QRIS_REPAIR_EMPTY_TARGET_PAGE"
        );
      }

      for (
        let i = 0;
        i < messageIds.length;
        i++
      ) {
        if (
          maxItems !== null &&
          repairedThisRun >= maxItems
        ) {
          break;
        }

        if (
          Date.now() - startedAt >=
          MAX_RUNTIME_MS
        ) {
          break;
        }

        let msg;

        try {
          msg =
            GmailApp.getMessageById(
              messageIds[i]
            );
        } catch (gmailError) {
          throw new Error(
            "BCA_QRIS_REPAIR_GMAIL_MESSAGE_UNAVAILABLE"
          );
        }

        if (!msg) {
          throw new Error(
            "BCA_QRIS_REPAIR_GMAIL_MESSAGE_UNAVAILABLE"
          );
        }

        const fromHeader =
          msg.getFrom();

        if (
          extractCleanEmail(
            fromHeader
          ) !== "bca@bca.co.id"
        ) {
          throw new Error(
            "BCA_QRIS_REPAIR_GMAIL_SENDER_MISMATCH"
          );
        }

        const messageDate =
          msg.getDate();

        const repairResult =
          postBcaQrisRepair_(
            workerUrl,
            repairSecret,
            {
              action: "repair",
              message_id:
                msg.getId(),
              from:
                fromHeader,
              subject:
                msg.getSubject(),
              body:
                msg
                  .getPlainBody()
                  .substring(
                    0,
                    2000
                  ),
              internal_date:
                messageDate.toISOString(),
            }
          );

        if (
          !repairResult ||
          (
            repairResult.repaired !==
              true &&
            repairResult
              .already_repaired !==
              true
          )
        ) {
          throw new Error(
            "BCA_QRIS_REPAIR_INVALID_SUCCESS_RESPONSE"
          );
        }

        repairedThisRun++;

        if (maxItems !== null) {
          const remainingAfter =
            repairResult.remaining !==
            undefined
              ? Number(
                  repairResult.remaining
                )
              : Math.max(
                  remaining - 1,
                  0
                );

          console.log(
            "BCA_QRIS_REPAIR_CANARY_PASS" +
            " | repaired=1" +
            " | remaining_after=" +
            remainingAfter
          );

          return {
            status: "canary_pass",
            repaired: 1,
            remaining:
              remainingAfter,
          };
        }
      }
    }
  } catch (e) {
    const rawMessage =
      String(
        e && e.message
          ? e.message
          : e || ""
      );

    const codeMatch =
      rawMessage.match(
        /BCA_QRIS_REPAIR_[A-Z0-9_]+/
      );

    const safeCode =
      codeMatch
        ? codeMatch[0]
        : "BCA_QRIS_REPAIR_UNEXPECTED_EXCEPTION";

    console.error(
      "BCA_QRIS_REPAIR_STOP" +
      " | code=" +
      safeCode
    );

    throw new Error(safeCode);
  } finally {
    lock.releaseLock();
  }
}


function assertBcaQrisRepairWorkerReady_(
  workerUrl
) {
  let response;

  try {
    response =
      UrlFetchApp.fetch(
        workerUrl.replace(
          /\/+$/,
          ""
        ) + "/health",
        {
          method: "get",
          muteHttpExceptions:
            true,
        }
      );
  } catch (healthError) {
    throw new Error(
      "BCA_QRIS_REPAIR_HEALTH_FETCH_FAILED"
    );
  }

  if (
    response.getResponseCode() !==
    200
  ) {
    throw new Error(
      "BCA_QRIS_REPAIR_HEALTH_HTTP_FAILED"
    );
  }

  let health;

  try {
    health =
      JSON.parse(
        response.getContentText()
      );
  } catch (parseError) {
    throw new Error(
      "BCA_QRIS_REPAIR_HEALTH_INVALID_JSON"
    );
  }

  if (
    !health ||
    health.status !== "ok" ||
    String(
      health.mode || ""
    ).toLowerCase() !==
      "shadow" ||
    Number(
      health.schema_version
    ) < 7
  ) {
    throw new Error(
      "BCA_QRIS_REPAIR_HEALTH_CONTRACT_FAILED"
    );
  }
}


function postBcaQrisRepair_(
  workerUrl,
  repairSecret,
  payload
) {
  const rawBody =
    JSON.stringify(payload);

  const timestamp =
    Date.now().toString();

  const nonce =
    Utilities.getUuid();

  const toSign =
    timestamp +
    "." +
    nonce +
    "." +
    rawBody;

  const signatureBytes =
    Utilities.computeHmacSha256Signature(
      toSign,
      repairSecret,
      Utilities.Charset.UTF_8
    );

  const signature =
    signatureBytes
      .map(function(byte) {
        return (
          "0" +
          (
            byte & 0xFF
          ).toString(16)
        ).slice(-2);
      })
      .join("");

  let response;

  try {
    response =
      UrlFetchApp.fetch(
        workerUrl.replace(
          /\/+$/,
          ""
        ) +
          "/api/repair/bca-qris",
        {
          method: "post",
          contentType:
            "application/json",
          payload: rawBody,
          headers: {
            "X-Signature":
              signature,
            "X-Timestamp":
              timestamp,
            "X-Nonce":
              nonce,
          },
          muteHttpExceptions:
            true,
        }
      );
  } catch (fetchError) {
    throw new Error(
      "BCA_QRIS_REPAIR_FETCH_EXCEPTION"
    );
  }

  const httpCode =
    response.getResponseCode();

  let parsed;

  try {
    parsed =
      JSON.parse(
        response.getContentText()
      );
  } catch (parseError) {
    throw new Error(
      "BCA_QRIS_REPAIR_INVALID_WORKER_JSON"
    );
  }

  if (
    httpCode !== 200 ||
    !parsed ||
    parsed.status !== "success"
  ) {
    const workerCode =
      String(
        parsed &&
        parsed.code
          ? parsed.code
          : "UNKNOWN"
      )
        .toUpperCase()
        .replace(
          /[^A-Z0-9_]/g,
          "_"
        );

    throw new Error(
      "BCA_QRIS_REPAIR_WORKER_" +
      workerCode
    );
  }

  return parsed;
}
/**
 * =====================================================================
 * BCA QRIS AUTOMATIC REPAIR ORCHESTRATOR
 *
 * Run startBcaQrisAutoRepair() ONCE.
 *
 * - D1 remains the authoritative checkpoint.
 * - Each execution is bounded by runBcaQrisTargetedRepair_().
 * - Remaining work is continued with a one-shot trigger.
 * - Completion sends email.
 * - Failure sends email and stops continuation.
 * - Historical backfill checkpoint is never used.
 * =====================================================================
 */

const BCA_AUTO_HANDLER =
  "continueBcaQrisAutoRepair_";

const BCA_AUTO_STATE =
  "BCA_QRIS_AUTO_STATE";

const BCA_AUTO_EMAIL =
  "BCA_QRIS_AUTO_EMAIL";

const BCA_AUTO_INITIAL =
  "BCA_QRIS_AUTO_INITIAL";

const BCA_AUTO_REMAINING =
  "BCA_QRIS_AUTO_REMAINING";

const BCA_AUTO_UPDATED =
  "BCA_QRIS_AUTO_UPDATED";

const BCA_AUTO_ERROR =
  "BCA_QRIS_AUTO_ERROR";

const BCA_AUTO_EXPECTED_START =
  1080;

const BCA_AUTO_DELAY_MS =
  60 * 1000;


function startBcaQrisAutoRepair() {
  const props =
    PropertiesService.getScriptProperties();

  const state =
    String(
      props.getProperty(
        BCA_AUTO_STATE
      ) || ""
    );

  if (state === "RUNNING") {
    console.log(
      "BCA_QRIS_AUTO_ALREADY_RUNNING"
    );

    return getBcaQrisAutoRepairStatus();
  }

  if (
    state === "FAILED" ||
    state === "STOPPED"
  ) {
    throw new Error(
      "BCA_QRIS_REPAIR_BLOCKED_STATE"
    );
  }

  const workerUrl =
    props.getProperty("WORKER_URL");

  const repairSecret =
    props.getProperty(
      "REPAIR_SECRET"
    );

  if (!workerUrl || !repairSecret) {
    throw new Error(
      "BCA_QRIS_REPAIR_CONFIG_MISSING"
    );
  }

  let email =
    String(
      props.getProperty(
        BCA_AUTO_EMAIL
      ) ||
      props.getProperty(
        "REPAIR_NOTIFY_EMAIL"
      ) ||
      ""
    ).trim();

  if (!email) {
    try {
      email = String(Session.getEffectiveUser().getEmail() || "").trim();
    } catch (e) {
      // Ignore if userinfo.email scope not present
    }
  }

  if (!email) {
    throw new Error(
      "BCA_QRIS_REPAIR_NOTIFY_EMAIL_UNAVAILABLE"
    );
  }

  if (
    MailApp.getRemainingDailyQuota() <
    1
  ) {
    throw new Error(
      "BCA_QRIS_REPAIR_NOTIFY_QUOTA_UNAVAILABLE"
    );
  }

  props.setProperty(
    BCA_AUTO_EMAIL,
    email
  );

  deleteBcaQrisAutoTriggers_();

  const remaining =
    getBcaQrisRemaining_(
      workerUrl,
      relaySecret
    );

  if (
    remaining !==
    BCA_AUTO_EXPECTED_START
  ) {
    return failBcaQrisAutoRepair_(
      "BCA_QRIS_REPAIR_BASELINE_MISMATCH",
      remaining
    );
  }

  props.setProperty(
    BCA_AUTO_STATE,
    "RUNNING"
  );

  props.setProperty(
    BCA_AUTO_INITIAL,
    String(remaining)
  );

  props.setProperty(
    BCA_AUTO_REMAINING,
    String(remaining)
  );

  props.setProperty(
    BCA_AUTO_UPDATED,
    new Date().toISOString()
  );

  props.deleteProperty(
    BCA_AUTO_ERROR
  );

  console.log(
    "BCA_QRIS_AUTO_STARTED" +
    " | remaining=" +
    remaining
  );

  return continueBcaQrisAutoRepair_();
}


function continueBcaQrisAutoRepair_() {
  const props =
    PropertiesService.getScriptProperties();

  if (
    props.getProperty(
      BCA_AUTO_STATE
    ) !== "RUNNING"
  ) {
    deleteBcaQrisAutoTriggers_();

    console.log(
      "BCA_QRIS_AUTO_CONTINUATION_NOOP"
    );

    return;
  }

  const workerUrl =
    props.getProperty("WORKER_URL");

  const repairSecret =
    props.getProperty(
      "REPAIR_SECRET"
    );

  if (!workerUrl || !repairSecret) {
    return failBcaQrisAutoRepair_(
      "BCA_QRIS_REPAIR_CONFIG_MISSING",
      null
    );
  }

  try {
    const before =
      getBcaQrisRemaining_(
        workerUrl,
        repairSecret
      );

    const initial =
      Number(
        props.getProperty(
          BCA_AUTO_INITIAL
        ) ||
        BCA_AUTO_EXPECTED_START
      );

    console.log(
      "BCA_QRIS_AUTO_EXECUTION_START" +
      " | repaired_total=" +
      Math.max(
        initial - before,
        0
      ) +
      " | remaining_before=" +
      before
    );

    const result =
      runBcaQrisTargetedRepair_(
        null
      );

    const after =
      getBcaQrisRemaining_(
        workerUrl,
        repairSecret
      );

    const repairedThisRun =
      Math.max(
        before - after,
        0
      );

    const repairedTotal =
      Math.max(
        initial - after,
        0
      );

    props.setProperty(
      BCA_AUTO_REMAINING,
      String(after)
    );

    props.setProperty(
      BCA_AUTO_UPDATED,
      new Date().toISOString()
    );

    console.log(
      "BCA_QRIS_AUTO_PROGRESS" +
      " | repaired_this_run=" +
      repairedThisRun +
      " | repaired_total=" +
      repairedTotal +
      " | remaining=" +
      after
    );

    if (
      after === 0 ||
      (
        result &&
        result.status === "completed"
      )
    ) {
      deleteBcaQrisAutoTriggers_();

      props.setProperty(
        BCA_AUTO_STATE,
        "COMPLETED"
      );

      props.setProperty(
        BCA_AUTO_REMAINING,
        "0"
      );

      props.setProperty(
        BCA_AUTO_UPDATED,
        new Date().toISOString()
      );

      const sent =
        sendBcaQrisAutoEmail_(
          "AturUang - BCA QRIS Repair Selesai",
          [
            "Status: COMPLETED",
            "Remaining target: 0",
            "Repaired after canary: " +
              repairedTotal,
            "Completed at: " +
              new Date().toISOString(),
            "",
            "Seluruh collapsed BCA QRIS evidence",
            "telah selesai diproses.",
            "",
            "Tahap berikutnya:",
            "final canonical integrity audit."
          ].join("\n")
        );

      console.log(
        "BCA_QRIS_AUTO_COMPLETED" +
        " | repaired_total=" +
        repairedTotal +
        " | remaining=0" +
        " | email=" +
        (
          sent
            ? "sent"
            : "failed"
        )
      );

      return;
    }

    if (after >= before) {
      return failBcaQrisAutoRepair_(
        "BCA_QRIS_REPAIR_NO_PROGRESS",
        after
      );
    }

    scheduleBcaQrisAutoTrigger_(
      after
    );
  } catch (e) {
    const code =
      extractBcaQrisSafeCode_(e);

    let remaining = null;

    try {
      remaining =
        getBcaQrisRemaining_(
          workerUrl,
          relaySecret
        );
    } catch (ignored) {
      const stored =
        props.getProperty(
          BCA_AUTO_REMAINING
        );

      if (stored !== null) {
        remaining =
          Number(stored);
      }
    }

    return failBcaQrisAutoRepair_(
      code,
      remaining
    );
  }
}


function getBcaQrisAutoRepairStatus() {
  const props =
    PropertiesService.getScriptProperties();

  const initialRaw =
    props.getProperty(
      BCA_AUTO_INITIAL
    );

  const remainingRaw =
    props.getProperty(
      BCA_AUTO_REMAINING
    );

  const initial =
    initialRaw === null
      ? null
      : Number(initialRaw);

  const remaining =
    remainingRaw === null
      ? null
      : Number(remainingRaw);

  const repaired =
    (
      initial === null ||
      remaining === null
    )
      ? null
      : Math.max(
          initial - remaining,
          0
        );

  const result = {
    state:
      props.getProperty(
        BCA_AUTO_STATE
      ) || "IDLE",
    initial_remaining:
      initial,
    repaired_total:
      repaired,
    remaining:
      remaining,
    updated_at:
      props.getProperty(
        BCA_AUTO_UPDATED
      ),
    error:
      props.getProperty(
        BCA_AUTO_ERROR
      )
  };

  console.log(
    "BCA_QRIS_AUTO_STATUS" +
    " | state=" +
    result.state +
    " | repaired_total=" +
    (
      repaired === null
        ? "unknown"
        : repaired
    ) +
    " | remaining=" +
    (
      remaining === null
        ? "unknown"
        : remaining
    ) +
    (
      result.error
        ? " | error=" +
          result.error
        : ""
    )
  );

  return result;
}


function stopBcaQrisAutoRepair() {
  const props =
    PropertiesService.getScriptProperties();

  deleteBcaQrisAutoTriggers_();

  props.setProperty(
    BCA_AUTO_STATE,
    "STOPPED"
  );

  props.setProperty(
    BCA_AUTO_UPDATED,
    new Date().toISOString()
  );

  console.log(
    "BCA_QRIS_AUTO_STOPPED" +
    " | future_continuations=disabled"
  );
}


function getBcaQrisRemaining_(
  workerUrl,
  repairSecret
) {
  const response =
    postBcaQrisRepair_(
      workerUrl,
      repairSecret,
      {
        action: "next",
        limit: 1
      }
    );

  const remaining =
    Number(
      response &&
      response.remaining
    );

  if (
    !Number.isInteger(remaining) ||
    remaining < 0
  ) {
    throw new Error(
      "BCA_QRIS_REPAIR_INVALID_REMAINING"
    );
  }

  return remaining;
}


function scheduleBcaQrisAutoTrigger_(
  remaining
) {
  deleteBcaQrisAutoTriggers_();

  ScriptApp
    .newTrigger(
      BCA_AUTO_HANDLER
    )
    .timeBased()
    .after(
      BCA_AUTO_DELAY_MS
    )
    .create();

  console.log(
    "BCA_QRIS_AUTO_RESCHEDULED" +
    " | remaining=" +
    remaining +
    " | next_run=approximately_1_minute"
  );
}


function deleteBcaQrisAutoTriggers_() {
  const triggers =
    ScriptApp.getProjectTriggers();

  for (
    let i = 0;
    i < triggers.length;
    i++
  ) {
    if (
      triggers[i]
        .getHandlerFunction() ===
      BCA_AUTO_HANDLER
    ) {
      ScriptApp.deleteTrigger(
        triggers[i]
      );
    }
  }
}


function failBcaQrisAutoRepair_(
  code,
  remaining
) {
  const props =
    PropertiesService.getScriptProperties();

  deleteBcaQrisAutoTriggers_();

  const safeCode =
    String(
      code ||
      "BCA_QRIS_REPAIR_UNEXPECTED_EXCEPTION"
    )
      .toUpperCase()
      .replace(
        /[^A-Z0-9_]/g,
        "_"
      );

  props.setProperty(
    BCA_AUTO_STATE,
    "FAILED"
  );

  props.setProperty(
    BCA_AUTO_ERROR,
    safeCode
  );

  props.setProperty(
    BCA_AUTO_UPDATED,
    new Date().toISOString()
  );

  if (
    remaining !== null &&
    Number.isFinite(
      Number(remaining)
    )
  ) {
    props.setProperty(
      BCA_AUTO_REMAINING,
      String(remaining)
    );
  }

  const sent =
    sendBcaQrisAutoEmail_(
      "AturUang - BCA QRIS Repair Gagal",
      [
        "Status: FAILED",
        "Code: " +
          safeCode,
        "Remaining target: " +
          (
            remaining === null
              ? "unknown"
              : remaining
          ),
        "Failed at: " +
          new Date().toISOString(),
        "",
        "Auto-repair dihentikan.",
        "Continuation trigger telah dibersihkan.",
        "",
        "Jangan restart sebelum error",
        "selesai didiagnosis."
      ].join("\n")
    );

  console.error(
    "BCA_QRIS_AUTO_FAILED" +
    " | code=" +
    safeCode +
    " | remaining=" +
    (
      remaining === null
        ? "unknown"
        : remaining
    ) +
    " | email=" +
    (
      sent
        ? "sent"
        : "failed"
    )
  );

  throw new Error(
    safeCode
  );
}


function extractBcaQrisSafeCode_(e) {
  const raw =
    String(
      e &&
      e.message
        ? e.message
        : e || ""
    );

  const match =
    raw.match(
      /BCA_QRIS_REPAIR_[A-Z0-9_]+/
    );

  return match
    ? match[0]
    : "BCA_QRIS_REPAIR_UNEXPECTED_EXCEPTION";
}


function sendBcaQrisAutoEmail_(
  subject,
  body
) {
  const props =
    PropertiesService.getScriptProperties();

  const email =
    String(
      props.getProperty(
        BCA_AUTO_EMAIL
      ) || ""
    ).trim();

  if (!email) {
    console.error(
      "BCA_QRIS_NOTIFY_FAILED" +
      " | code=EMAIL_UNAVAILABLE"
    );

    return false;
  }

  try {
    MailApp.sendEmail({
      to: email,
      subject: subject,
      body: body,
      name: "AturUang"
    });

    console.log(
      "BCA_QRIS_NOTIFICATION_SENT"
    );

    return true;
  } catch (e) {
    console.error(
      "BCA_QRIS_NOTIFY_FAILED" +
      " | code=MAILAPP_SEND_FAILED"
    );

    return false;
  }
}

/**
 * Resume only the known fail-closed parse-rejected run
 * after the semantic Worker fix has been deployed.
 */
function resumeBcaQrisAutoRepairAfterFix() {
  const props =
    PropertiesService.getScriptProperties();

  const state =
    String(
      props.getProperty(
        BCA_AUTO_STATE
      ) || ""
    );

  const failedCode =
    String(
      props.getProperty(
        BCA_AUTO_ERROR
      ) || ""
    );

  const initial =
    Number(
      props.getProperty(
        BCA_AUTO_INITIAL
      )
    );

  if (state !== "FAILED") {
    throw new Error(
      "BCA_QRIS_REPAIR_RESUME_STATE_MISMATCH"
    );
  }

  if (
    failedCode !==
    "BCA_QRIS_REPAIR_WORKER_BCA_QRIS_REPAIR_PARSE_REJECTED"
  ) {
    throw new Error(
      "BCA_QRIS_REPAIR_RESUME_ERROR_MISMATCH"
    );
  }

  if (initial !== 1080) {
    throw new Error(
      "BCA_QRIS_REPAIR_RESUME_INITIAL_MISMATCH"
    );
  }

  const workerUrl =
    props.getProperty(
      "WORKER_URL"
    );

  const repairSecret =
    props.getProperty(
      "REPAIR_SECRET"
    );

  if (!workerUrl || !repairSecret) {
    throw new Error(
      "BCA_QRIS_REPAIR_CONFIG_MISSING"
    );
  }

  assertBcaQrisRepairWorkerReady_(
    workerUrl
  );

  const remaining =
    getBcaQrisRemaining_(
      workerUrl,
      repairSecret
    );

  if (remaining !== 1079) {
    throw new Error(
      "BCA_QRIS_REPAIR_RESUME_BASELINE_MISMATCH"
    );
  }

  deleteBcaQrisAutoTriggers_();

  props.setProperty(
    BCA_AUTO_STATE,
    "RUNNING"
  );

  props.setProperty(
    BCA_AUTO_REMAINING,
    String(remaining)
  );

  props.setProperty(
    BCA_AUTO_UPDATED,
    new Date().toISOString()
  );

  props.deleteProperty(
    BCA_AUTO_ERROR
  );

  console.log(
    "BCA_QRIS_AUTO_RESUMED_AFTER_FIX" +
    " | repaired_total=1" +
    " | remaining=1079"
  );

  return continueBcaQrisAutoRepair_();
}
