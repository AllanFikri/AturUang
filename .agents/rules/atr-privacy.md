# ATR Privacy Rules

## Never commit
- secrets.json, *.env, PDF, ZIP, CSV, DB, real receipts.
- HMAC keys, API tokens, Telegram bot tokens.

## Never print
- Raw private transaction rows.
- Real balances, account identifiers.
- Unredacted Gmail subjects or bodies.

## Always sanitize
- Error messages, repr(), diagnostics.
- Test fixtures visibly synthetic.
