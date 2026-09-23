# Binance Vision Q&A — optional reference (not SoT)

**Status:** Optional community reference only. **Not** Source of Truth.  
**SoT for Jarvise:** official docs + [`binance-for-jarvise.md`](binance-for-jarvise.md) + [`api-map.json`](../../data/analytics/api-map.json).  
**Community hub:** [dev.binance.vision](https://dev.binance.vision/)  
**Captured:** 2026-09-23 (browser + public thread summaries; some scrapers are geo-restricted)

Use this file when debugging signed reads / public klines. Prefer official [Spot REST](https://developers.binance.com/en/docs/products/spot/rest-api) and [errors](https://developers.binance.com/en/docs/products/spot/errors) if anything conflicts.

**Jarvise filter:** paper-only / no order placement. Q&A below is limited to market data, signing, auth, rate limits, and spot account **read**. Order / withdraw how-tos are omitted except as denylist notes.

---

## Index of useful Vision threads

| Topic | Thread |
|-------|--------|
| FAQ hub | [API Frequently Asked Questions](https://dev.binance.vision/t/api-frequently-asked-questions/37) |
| `-2015` key / IP / permissions | [Invalid API-key, IP, or permissions](https://dev.binance.vision/t/why-do-i-see-this-error-invalid-api-key-ip-or-permissions-for-action/93) |
| `-1022` signature | [Signature for this request is not valid](https://dev.binance.vision/t/getting-this-error-data-code-1022-msg-signature-for-this-request-is-not-valid/11231) |
| `-1021` timestamp | [INVALID_TIMESTAMP ahead of server](https://dev.binance.vision/t/1021-invalid-timestamp-timestamp-ahead-of-server-time/4783) |
| Rate / IP weight | [Request limit on the API endpoints](https://dev.binance.vision/t/request-limit-on-the-api-endpoints/9275) |
| Spot balances only | [API \| Account Balances](https://dev.binance.vision/t/api-account-balances/36015) |
| More FAQs | [Topics tagged faq](https://dev.binance.vision/tag/faq) |

---

---

## Truth-oriented Q&A (community consensus → Jarvise note)

### Q: Where do Ed25519/RSA public and private keys go?

**Community / Binance docs:** Upload the **public** PEM when creating an asymmetric API key on Binance. Keep the **private** PEM only on your machine. Binance then issues an API Key string for the `X-MBX-APIKEY` header. Signatures are Base64 (case-sensitive) and must be URL-encoded in the query string.

**Jarvise note:** Put private PEM at e.g. `/opt/jarvise/secrets/binance-ed25519-prv.pem` (`chmod 600`). Set `BINANCE_API_KEY` + `BINANCE_API_PRIVATE_KEY_PATH`. Do not put private PEM in chat, git, or multiline `.env`. HMAC via `BINANCE_API_SECRET` still works as a fallback.

---

### Q: Timestamp outside recvWindow (`-1021` / recvWindow errors)

**Community answer (Vision FAQ):** Usually local clock is out of sync with Binance. Sync the OS clock (NTP). If it still fails, check `recvWindow`. Do **not** invent a custom “sync to exchangeInfo time” as your primary clock — NTP is preferred; exchange time over HTTP includes latency and can make drift worse.

**Jarvise note:** Signed `GET /api/v3/account` (P3) should use host NTP. Soft-fail the analytics panel on `-1021` rather than retry loops.

**Sources:** [FAQ #37](https://dev.binance.vision/t/api-frequently-asked-questions/37), [thread 4783](https://dev.binance.vision/t/1021-invalid-timestamp-timestamp-ahead-of-server-time/4783)

---

### Q: Signature for this request is not valid (`-1022`)

**Community answer:** The query string used to compute HMAC-SHA256 must be **byte-identical** to the query string sent (minus `signature=`). Common failures:

- Different `timestamp` when signing vs when sending (`Date.now()` called twice)
- Params included in the URL but not in the signed string (or the reverse)
- Wrong / mismatched API secret for the key
- Empty param values in the signed string
- Signing from a browser / client side (CORS + secret exposure) — do signed calls server-side only

**Jarvise note:** Build params once → `urlencode` → sign that exact string → append `&signature=`. Never put secrets in browser JS.

**Sources:** [FAQ #37](https://dev.binance.vision/t/api-frequently-asked-questions/37), [thread 11231](https://dev.binance.vision/t/getting-this-error-data-code-1022-msg-signature-for-this-request-is-not-valid/11231), [thread 1629](https://dev.binance.vision/t/signature-for-this-request-is-not-valid/1629)

---

### Q: Invalid API-key, IP, or permissions (`-2015`)

**Community answer (moderator checklist):**

1. Wrong API key string  
2. Missing `X-MBX-APIKEY` header  
3. IP whitelist mismatch (check egress IP; IPv6 quirks — try IPv4)  
4. Key lacks permission for the action (account read needs appropriate enablement; **Jarvise must never enable withdraw**)  
5. Testnet key against production URL (or vice versa)  
6. Wrong product base URL (spot vs futures)  
7. Regional endpoint mismatch (e.g. `api.binance.com` vs `api.binance.us` for US accounts)  
8. Too many keys on the account (community cites ~30 max)

**Jarvise note:** Paper OHLC uses public global `api.binance.com` (no key). P3 balance read uses production spot URL + HMAC; soft-fail UI on `-2015`. Confirm keys are spot-read capable without withdraw.

**Sources:** [thread 93](https://dev.binance.vision/t/why-do-i-see-this-error-invalid-api-key-ip-or-permissions-for-action/93), [FAQ #37](https://dev.binance.vision/t/api-frequently-asked-questions/37)

---

### Q: Why doesn’t `/api/v3/account` show Earn / staking / funding totals?

**Community answer:** `/api/v3/account` returns **spot wallet** balances only. Other products need product-specific endpoints (Earn, futures, etc.).

**Jarvise note:** Matches P3 scope — spot wallet only; no futures/earn balance APIs in this phase.

**Source:** [thread 36015](https://dev.binance.vision/t/api-account-balances/36015)

---

### Q: IP blocked / rate limits — how to avoid?

**Community answer:** Respect IP request weights. Check `GET /api/v3/exchangeInfo` and response headers such as `X-MBX-USED-WEIGHT-*`. Escalation pattern discussed in community: over-limit → `429` → temporary `418` ban → possible `403`. For heavy **market data**, Vision FAQ suggests WebSockets (does not count the same way against REST IP weight). Honor backoff; do not hammer retries on `418`/`403`.

**Jarvise note:** Public klines via ingest with modest limits; one account sync per `/analytics` load when P3 ships. WebSockets remain out of scope for Jarvise MVP. Order rate-limit headers are irrelevant — we do not place orders.

**Sources:** [FAQ #37](https://dev.binance.vision/t/api-frequently-asked-questions/37), [thread 9275](https://dev.binance.vision/t/request-limit-on-the-api-endpoints/9275)

---

### Q: Invalid symbol on klines?

**Community answer:** Symbol must exist on that market. Use `GET /api/v3/exchangeInfo` to list valid spot symbols (e.g. `EURXRP` is not a spot pair).

**Jarvise note:** Ingest should fail clearly on `-1121`; do not invent symbols.

**Source:** [FAQ thread discussion](https://dev.binance.vision/t/api-frequently-asked-questions/37) (Jul ’22 replies)

---

### Q: Rest API trading not enabled?

**Community answer:** Often means trading API maintenance — check API announcements. Not a Jarvise path while paper-only.

**Jarvise note:** Irrelevant for public klines and for read-only account; if seen on a forbidden order path, that confirms we should not be calling orders.

---

## Explicitly out of scope for this reference

- How to place / cancel / OCO orders  
- Margin borrow, futures keys, FIX/SBE trading  
- Enabling withdraw on API keys  
- Binance Customer Service account/2FA issues (Vision is for API code questions)

---

## Related Jarvise docs

- [`binance-for-jarvise.md`](binance-for-jarvise.md) — SoT filter from official intro  
- [`../superpowers/specs/2026-09-23-p3-exchange-readonly-design.md`](../superpowers/specs/2026-09-23-p3-exchange-readonly-design.md) — P3 read-only design  
- [`../../data/analytics/sources/binance-api-intro-jarvise.txt`](../../data/analytics/sources/binance-api-intro-jarvise.txt) — local intro digest  
