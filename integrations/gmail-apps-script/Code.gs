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
      offset = parseInt(
        props.getProperty("GMAIL_BACKFILL_OFFSET") || "0",
        10
      );

      if (!Number.isFinite(offset) || offset < 0) {
        offset = 0;
      }
    } else {
      props.setProperty("GMAIL_BACKFILL_OFFSET_MONTH", ymStr);
      props.setProperty("GMAIL_BACKFILL_OFFSET", "0");
    }

    console.log(
      "Memproses jendela " +
      ymStr +
      " mulai thread offset " +
      offset
    );

    while (true) {
      const page = processGmailQueryPage(
        query,
        workerUrl,
        relaySecret,
        PAGE_SIZE,
        offset,
        startDate,
        endDate
      );

      totalProcessed += page.processedCount;

      // Never advance the page if a trusted message failed to relay.
      // Successful messages from this page are safe to retry because
      // Worker ingestion is idempotent by Gmail message ID.
      if (page.failedCount > 0) {
        console.error(
          "Backfill dihentikan pada " +
          ymStr +
          " offset " +
          offset +
          ": " +
          page.failedCount +
          " pesan gagal direlay."
        );

        throw new Error(
          "Gmail historical backfill relay failure; offset dipertahankan untuk retry."
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
          "Backfill dipause sebelum batas runtime. " +
          "Jalankan kembali untuk resume dari " +
          ymStr +
          " offset " +
          offset +
          "."
        );
        return;
      }
    }

    if (Date.now() - startedAt >= MAX_RUNTIME_MS) {
      console.log(
        "Backfill dipause setelah menyelesaikan bulan. " +
        "Jalankan kembali untuk melanjutkan dari checkpoint."
      );
      return;
    }
  }

  console.log(
    "Backfill selesai. Total pesan sukses pada eksekusi ini: " +
    totalProcessed
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
  endDate
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
          relaySecret
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
        const response = UrlFetchApp.fetch(
          workerUrl + "/api/ingest/gmail",
          options
        );

        const code = response.getResponseCode();

        if (code === 200) {
          processedCount++;
        } else if (code === 403) {
          // A thread can contain a message from another sender.
          // Strict sender filtering is intentional, so this is not
          // treated as a historical backfill failure.
          console.warn(
            "Ditolak (403 Untrusted Sender): " +
            fromHeader
          );
        } else {
          failedCount++;

          console.error(
            "Relay HTTP " +
            code +
            " untuk Gmail message ID " +
            messageId
          );
        }
      } catch (e) {
        failedCount++;

        console.error(
          "Exception saat relay message " +
          messageId +
          ": " +
          e.toString()
        );
      }
    }
  }

  return {
    processedCount: processedCount,
    threadCount: threads.length,
    failedCount: failedCount,
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
