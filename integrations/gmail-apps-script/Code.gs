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

  const startYM = startYearMonth || props.getProperty("GMAIL_BACKFILL_CHECKPOINT") || "2025-01";
  const now = new Date();
  const currentYM = now.getFullYear() + "-" + ("0" + (now.getMonth() + 1)).slice(-2);
  const endYM = endYearMonth || currentYM;

  console.log("Memulai Backfill Gmail dari " + startYM + " s.d. " + endYM);

  let current = parseYearMonth(startYM);
  const target = parseYearMonth(endYM);

  let totalProcessed = 0;

  while (compareYearMonth(current, target) <= 0) {
    const ymStr = formatYearMonth(current);
    const nextYM = getNextMonth(current);
    const afterDate = ymStr + "-01";
    const beforeDate = formatYearMonth(nextYM) + "-01";

    const query = 'after:' + afterDate + ' before:' + beforeDate + ' ' + TRUSTED_SENDER_QUERY;
    console.log("Memproses jendela: " + query);

    // Enforce monthly bounds again at message level because Gmail search
    // returns threads and getMessages() may include messages outside the window.
    const startDate = new Date(current.year, current.month - 1, 1, 0, 0, 0, 0);
    const endDate = new Date(nextYM.year, nextYM.month - 1, 1, 0, 0, 0, 0);

    const count = processGmailQuery(
      query,
      workerUrl,
      relaySecret,
      100,
      startDate,
      endDate
    );

    totalProcessed += count;

    // Simpan checkpoint bulan berikutnya
    props.setProperty("GMAIL_BACKFILL_CHECKPOINT", formatYearMonth(nextYM));
    console.log("Jendela " + ymStr + " selesai (" + count + " pesan). Checkpoint: " + formatYearMonth(nextYM));

    current = nextYM;
  }

  console.log("Backfill selesai. Total pesan terproses: " + totalProcessed);
}

function getBackfillStatus() {
  const props = PropertiesService.getScriptProperties();
  const cp = props.getProperty("GMAIL_BACKFILL_CHECKPOINT") || "Belum dimulai (default 2025-01)";
  console.log("Status Checkpoint Backfill Saat Ini: " + cp);
  return cp;
}

function resetBackfillCheckpoint() {
  const props = PropertiesService.getScriptProperties();
  props.deleteProperty("GMAIL_BACKFILL_CHECKPOINT");
  console.log("Checkpoint backfill telah direset ke 2025-01.");
}

/**
 * Core processor loop (Message-level inspection)
 */
function processGmailQuery(query, workerUrl, relaySecret, maxThreads, startDate, endDate) {
  const threads = GmailApp.search(query, 0, maxThreads);
  let processedCount = 0;

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

      // Enforce message-level strict sender trust
      if (!isSenderExactTrusted(fromHeader)) {
        continue;
      }

      const messageId = msg.getId();
      const payload = {
        message_id: messageId,
        from: fromHeader,
        subject: msg.getSubject(),
        body: msg.getPlainBody().substring(0, 2000), // Minimal excerpt
        internal_date: messageDate.toISOString(),
      };

      const rawBody = JSON.stringify(payload);
      const timestamp = Date.now().toString();
      const nonce = Utilities.getUuid();

      // Sign with HMAC-SHA256
      const toSign = timestamp + "." + nonce + "." + rawBody;
      const signatureBytes = Utilities.computeHmacSha256Signature(toSign, relaySecret);
      const signature = signatureBytes.map(function(byte) {
        return ('0' + (byte & 0xFF).toString(16)).slice(-2);
      }).join('');

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
        const response = UrlFetchApp.fetch(workerUrl + "/api/ingest/gmail", options);
        const code = response.getResponseCode();
        if (code === 200) {
          processedCount++;
        } else if (code === 403) {
          console.warn("Ditolak (403 Untrusted Sender): " + fromHeader);
        }
      } catch (e) {
        console.error("Exception saat relay message " + messageId + ": " + e.toString());
      }
    }
  }

  return processedCount;
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
