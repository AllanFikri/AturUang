/**
 * AturUang — Gmail Relay Connector (Google Apps Script)
 * 
 * Membaca notifikasi transaksi bank/e-wallet baru dari Gmail secara terfilter,
 * menandatangani payload dengan HMAC-SHA256, dan mengirimkannya ke Worker AturUang.
 * 
 * TIDAK MENGGUNAKAN GOOGLE SHEETS.
 */

function relayGmailTransactions() {
  const props = PropertiesService.getScriptProperties();
  const workerUrl = props.getProperty("WORKER_URL");
  const relaySecret = props.getProperty("GMAIL_RELAY_SECRET");

  if (!workerUrl || !relaySecret) {
    console.error("Konfigurasi WORKER_URL atau GMAIL_RELAY_SECRET belum diisi di Script Properties.");
    return;
  }

  // Filter query: Hanya membaca email dari pengirim terpercaya dalam 2 hari terakhir
  const query = 'newer_than:2d (from:bca.co.id OR from:bankjago.com OR from:shopeepay.co.id OR from:gopay.co.id OR subject:"Notifikasi Transaksi" OR subject:"m-BCA")';
  const threads = GmailApp.search(query, 0, 20);

  for (let i = 0; i < threads.length; i++) {
    const messages = threads[i].getMessages();
    for (let j = 0; j < messages.length; j++) {
      const msg = messages[j];
      const messageId = msg.getId();

      // Payload minimal teredaksi
      const payload = {
        message_id: messageId,
        from: msg.getFrom(),
        subject: msg.getSubject(),
        body: msg.getPlainBody().substring(0, 2000), // Max 2000 chars
        internal_date: msg.getDate().toISOString(),
      };

      const rawBody = JSON.stringify(payload);
      const timestamp = Date.now().toString();
      const nonce = Utilities.getUuid();

      // Sign with HMAC-SHA256: timestamp.nonce.rawBody
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
          console.log("Pesan berhasil direlay: " + messageId);
        } else {
          console.warn("Gagal relay pesan " + messageId + ": HTTP " + code + " " + response.getContentText());
        }
      } catch (e) {
        console.error("Exception saat relay pesan " + messageId + ": " + e.toString());
      }
    }
  }
}
