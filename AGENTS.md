# AGENTS.md

Working notes for agents and humans changing this repository. Read this file
before editing code. It records the invariants that were paid for with real
accounts and real failures; breaking one of them usually shows up as "the
email silently never arrives" or "the account is banned" rather than as an
exception.

Related documents:

- `docs/PERFORMANCE.md` — measured timings, Sentinel/mail tuning, and the
  benchmark procedure. Read it before changing any timing default.
- `README.md` — user-facing setup, environment variables, and the WebUI map.

---

## 1. What this project is

Regix is a protocol-only account-registration tool for OpenAI/ChatGPT. There
is no browser automation: no Playwright, no Camoufox, no headless Chrome.

The whole pipeline is HTTP:

- `curl_cffi` provides a browser-like TLS fingerprint (`impersonate=chrome146`,
  `safari260`, and so on).
- OpenAI's real Sentinel `sdk.js` is executed inside a Node VM so the
  proof-of-work token is the one a browser would produce.
- Email codes are fetched over Graph API / IMAP / Remail HTTP APIs.
- Phone verification codes come from SMS activation providers.

Three front ends drive the same backend:

| Front end | Entry point | Purpose |
| --- | --- | --- |
| WebUI | `regix-webui` / `regix.api.app:app` | Full management UI (FastAPI + Vue 3) |
| CLI | `regix` | Same services, scriptable, JSON-friendly |
| Auto loop | `regix/services/auto_loop.py` | Multi-worker batch registration |

---

## 2. Non-negotiable invariants

These are load-bearing. Each one has a documented failure mode in the code
comments; do not "clean them up" without reproducing the failure.

### 2.1 Sentinel must run the real SDK

- The Python-only synthetic PoW in older code passes OpenAI's *surface* checks
  but fails the server-side SDK validation, so the OTP mail is silently
  dropped. The registration appears to succeed and the code never arrives.
- The real path is `regix/openai/quickjs.py` (driver) plus
  `regix/openai/openai_sentinel_quickjs.js` (browser shim). The SDK is
  downloaded once per version and cached in
  `/tmp/openai-sentinel-demo/<version>/sdk.js`.
- Node >= 18 is required at runtime. The Docker image copies a single `node`
  binary; npm is not needed and must not be added.
- The wrapper **must** exit the process after flushing its JSON result. SDK
  timers otherwise keep the process alive and every Sentinel call wastes
  about 8 seconds. The regression test is `tests/test_sentinel_quickjs.py`.
- Behavior simulation exists to feed the Session Observer (SO) collector. The
  main token is produced *before* the behavior sequence, so behavior may be
  skipped only when the server did not ask for SO
  (`challenge.so.required !== true`). Never skip it when SO is required.

### 2.2 `warmup()` seeds the `oai-did` cookie

`POST /api/auth/signin/openai` behaves differently depending on whether the
chatgpt.com cookie jar already contains `oai-did`. Without it the flow returns
a NextAuth page and the next step fails with `409 invalid_state`. Measured
before the fix: 5/5 runs without `oai-did` produced 409. Do not remove or
short-circuit `warmup()`.

`warmup()` retries up to 4 times and rebuilds the session on failure, because
a fresh session means a fresh exit IP. It is the only place where rebuilding
the session is safe (see 2.4).

### 2.3 Never strip client hints

Cloudflare 403s were traced to missing `sec-ch-ua*` headers, not to the
fingerprint itself. `_navigation_headers()` and `_common_headers()` must keep
sending the full Client Hints set matching the current UA family. Removing
them reintroduces 403s that look like "the proxy is blocked".

### 2.4 TLS handshake errors are retried on the *same* session

`curl: (35) TLS connect error` occurs on roughly 5% of calls and is a
link-level hiccup, independent of fingerprint or domain. The wrapper
`_TlsRetrySession` in `regix/http/client.py` retries it in place.

**Never rebuild the session mid-flow to "fix" a TLS error.** The session
already holds the `oai-did` and CSRF cookies seeded by `warmup()`; rebuilding
it drops them and converts a transient network blip into a hard
`409 invalid_state`. Session rebuild is allowed only in `warmup()` (before
cookies exist) and in the `invalid_state` retry path (which also rotates the
device id).

### 2.5 `409 invalid_state` retry rules

`regix/openai/registration.py` retries up to 3 times. On retry it must:

1. Reuse the same mailbox (the address is already paid for).
2. Rebuild the HTTP session (new exit IP).
3. Clear `self.result.device_id` so a new `oai-did` is generated.
4. Clear `_last_sentinel_so_token`.
5. Re-run `warmup()`.

Skipping step 3 leaves the old device id with new cookies and fails every
time.

### 2.6 `tm1.openai.com` is a broken sender

That domain returns a fixed OTP (`493682`) for every account. The filter in
`regix/providers/mail/outlook.py` (`_check_from_domain`) must keep rejecting
it, otherwise every registration verifies the same bogus code.

### 2.7 Outlook: Graph and IMAP run as a race, Graph is never skipped

In this account pool the Graph token exchange fails deterministically with
`AADSTS90023` (the client has no Graph mail scope). Graph is still started:

- `Graph` and `IMAP` run concurrently in daemon threads.
- The first channel to return an OTP wins and sets a stop event.
- The loser exits at its next polling boundary.
- One channel failing does not abort the other.
- Both failing raises the IMAP error as the primary error.

Both channels poll on an independent random 3.0-4.5 s interval. Do not
"optimize" this into a serial Graph-then-IMAP sequence, and do not hard-code
the poll interval back to a fixed value.

### 2.8 TOTP secrets are single-delivery

Binding TOTP to an **existing** account (the WebUI "绑定2FA" button, `regix
bind-2fa`, `POST /api/registered/bind_2fa`) has three rules:

- `POST /backend-api/accounts/mfa/enroll` answers `401 recent_auth_required`
  for an ordinary API token. Only a **fresh Codex OAuth authorization**
  counts as recent auth, so `add_totp_2fa()` reuses
  `codex_login.login_and_exchange()` and enrolls with the access token that
  exchange returned. Do not try to enroll with the stored web token.
- That exchange **rotates the refresh token**. It must be written back
  immediately; keeping the old one leaves the account unable to mint Codex
  tokens (`refresh_token_reused`).
- Persist the new secret **before** calling
  `activate_enrollment`. Activation is irreversible and the server keeps only
  a hash, so a crash between the two steps must not lose the secret.

`regix/services/two_factor.py` enrolls, activates, and receives the secret
exactly once from the server; it cannot be fetched again. The registrar writes
it with `db.save_totp_early()` immediately after activation, before the phone
verification or the panel export, so a crash later cannot orphan the account.
Never move that write later in the flow.

The activation HTTP 200 is authoritative. The follow-up `mfa_info` read is only
a confirmation, so it polls immediately and retries after 0.4 s and 1.0 s
instead of sleeping a fixed 2 seconds.

### 2.9 Proxy quota is reserved on selection

`db.pick_proxy()` / `db.reserve_proxy()` increment `used_count` at selection
time so concurrent workers cannot pile onto one exit IP. On failure the
reservation is released (`db.proxy_release()`) and only `failed_count` is
incremented. On success the reservation is kept. Do not bypass the pool for
"quick tests", and do not increment usage twice.

Static proxies that hit Cloudflare 403 twice in a row are auto-disabled;
dynamic proxies only accumulate a counter.

### 2.10 `pooled` and `ephemeral` are independent mail-provider axes

| Provider | pooled | ephemeral | Meaning |
| --- | --- | --- | --- |
| Outlook pool | yes | no | Finite, purchased addresses; mark done/failed |
| CF catch-all | no | yes | Address is generated per run |
| iCloud relay | no | no | Fixed address, no password, so OpenAI treats it as an existing account |

`pooled` decides whether the registrar claims/marks/releases the mailbox.
`ephemeral` decides whether OpenAI treats the address as new or existing.
Conflating them caused 401s on iCloud relay. Keep both attributes explicit on
every provider.

### 2.11 The refresh-token chain can cost a second email

The Codex refresh-token exchange (`OAUTH_CODEX_RT_EXCHANGE=1`) can trigger a
second verification email. For one-shot mailboxes that is fatal, so the API
default and the frontend defaults are off; the effective value comes from the
WebUI preference `wantRefreshToken`, which the CLI reads as well. Keep the
guard that skips the exchange when `want_refresh_token` is false, and keep
the "skip and log" behavior in `auth_flow` rather than raising.

### 2.12 SMS: WhatsApp vs SMS

OpenAI routes most countries to WhatsApp. Pure SMS currently works only for
Thailand (`country_id=52` / `sms_country=52`). Warn, do not silently retry,
when auto-selection cannot find a whitelisted country.

### 2.13 Payment links: Codex token, billing country, promo fields

Three independent rules decide whether `POST
/backend-api/payments/checkout` succeeds. Check them in this order.

**1. The access token must be minted by the Codex client.** This one is not
obvious and cost hours to find. OpenAI's response to a wrong token flavour is

    HTTP 400 {"detail": "Our systems have detected unusual activity. Please try again later."}

which reads like an account-level fraud block but is not one. Decode the JWT
and look at `client_id` / `amr`:

| Token source | `client_id` claim | Result |
| --- | --- | --- |
| web session (`registered.access_token`) | `app_X8zY6vW2pQ9tR3dE7nK1jL5gH`, `amr=otp` | 400 "unusual activity" |
| Codex client (`refresh_token` exchange) | `app_EMoamEEZ73f0CkXaXp7hrann` | 200, checkout session created |

Measured on the same account back to back; all 8 accounts that have a
`refresh_token` produced links this way. So `resolve_checkout_token()` in
`regix/services/payment.py` exchanges the stored refresh token for a Codex
access token (`regix/openai/codex_token.py`) and only falls back to the stored
web token when there is no refresh token.

Accounts registered **without** the Codex chain have no refresh token and
cannot create a checkout session. They need one re-login, which is exactly the
same Codex OAuth flow the registration already runs:
`regix codex-login <email>` / `POST /api/payment/prepare` calls
`AuthFlow.run_protocol_login()` followed by `oauth_codex_rt_exchange()` and
stores the new refresh token. That login usually hits `/add-phone`, so it needs
the SMS provider and costs one number per account.

Never "fix" a 400 here by changing headers, IP, checkout mode or account data:
if `client_id` is the web client, the request is wrong no matter what else you
change. Error codes keep the two cases apart: `codex_token_missing` /
`codex_token_refresh_failed` mean the token, `checkout_risk_blocked` means a
real risk-engine answer after a Codex token was used.

**2. The billing country must match the exit IP.** A mismatch returns
`Billing country must match request country.`, mapped to
`checkout_country_mismatch`. `detect_exit_country()` resolves the exit country
through the same proxy the checkout would use; the WebUI preselects it and
warns before the request.

**3. Promo fields are two different things.** `promo_code` is a free-form
coupon the user types. `promo_campaign` is the official trial campaign
object:

```json
{"promo_campaign": {"promo_campaign_id": "plus-1-month-free",
                    "is_coupon_from_query_param": false}}
```

Both may be sent together; leave them out when empty. The payment dialog can
fill the campaign id from the account (`GET /api/payment/account/{email}`
returns `plus_check.plus_trial_campaign_id`) or accept a hand-typed value.

**Keep the account on one IP.** The registrar stores the proxy it used in
`registered.extra.proxy`. Both the payment link and the bulk Plus check fall
back to that proxy when the caller does not pass one explicitly, so an account
is not registered on one IP and exercised on another. Do not remove that
fallback.

### 2.14 A Cloudflare 403 at the OAuth init is fingerprint-level

`[3/10] OAuth 初始化完成 status=403` (the final URL stays on
`https://auth.openai.com/oauth/authorize`) is **not** an IP problem. Measured
2026-11-07 on one exit IP, one URL, only the TLS impersonation changed:

| Impersonation | Result |
| --- | --- |
| `chrome110` / `chrome136` / `chrome146` / `chrome150` | 200 |
| `safari18_0` | 200 |
| `firefox144` | 200 |
| `safari260` | **403** |

A 403 there dooms the whole registration: the next step
(`authorize/continue`) answers `409 invalid_state` because the authorize state
was never established. The `invalid_state` retry in `run_register` used to
rebuild the session and refresh the device id but **keep the same
impersonation**, so all three attempts hit the same wall (observed: three
identical 403s, then the mailbox was wasted).

Two rules follow:

- `auth_oauth_init()` rotates to the next same-family impersonate when it
  sees a 403 and retries the request in place. No mailbox, no device id and no
  exit IP is touched by that rotation.
- The `invalid_state` retry in `run_register` rotates the impersonation too.
  Without it the retry is a no-op against a fingerprint-level block.

Do not "fix" a 403 here by adding headers or by rebuilding the session: the
block follows the impersonation. The regression test is
`tests/test_oauth_fingerprint.py`.

---

## 3. Architecture

Dependencies point downward only. Routers and the CLI never talk to SQLite or
HTTP directly; they call services.

```
regix/api/          FastAPI app + routers        (thin: validate, delegate, status codes)
regix/cli/          argparse front end           (thin: parse args, format output)
regix/services/     orchestration                 (registration worker, auto loop, 2FA, plan check, export)
regix/openai/       OpenAI protocol mixins        (HTTP calls against auth/chatgpt endpoints)
regix/providers/    mail + sms integrations       (pluggable, self-describing)
regix/storage/      SQLite repositories           (facade is the only public entry point)
regix/http/         curl_cffi session factory     (TLS retry wrapper)
regix/fingerprint/  UA / client-hint / screen / timezone generation
```

Rules that follow from this layout:

- New capability lands in `services/` or `providers/` first. The router and
  the CLI are thin wrappers around it.
- A router does request validation, status-code mapping, and nothing else. It
  must not contain registration logic, quota math, or SQL.
- The CLI must not duplicate the WebUI behavior. `regix register` calls the
  same `registrar.start_registration()` the WebUI calls.
- `regix/storage/facade.py` is the only supported import path for persistence.
  Raw `sqlite3` access outside `regix/storage/` is a bug.

### 3.1 The `AuthFlow` mixin composition

`regix/openai/auth_flow.py` composes domain mixins into one object:

```python
class AuthFlow(ClientMixin, OAuthMixin, ChallengeMixin, TokenMixin,
                ProtocolMixin, SessionMixin, RegistrationMixin):
```

Each mixin owns one protocol area: `protocol.py` for navigation/signup/API
calls, `session.py` for session and cookies, `token.py` for token exchange,
`challenges.py` for phone/OTP challenges, `registration.py` for the
multi-step registration state machine, and `client.py` for the cookie header
and HTTP tracing. Keep protocol steps inside the matching mixin; do not grow
`auth_flow.py` itself.

### 3.2 The registration worker

`regix/services/registration.py` owns the run lifecycle:

- `start_registration(account, options)` spawns a daemon thread and returns a
  `run_id`.
- The worker binds a `QueueLogHandler` to that thread via a `threading.local`
  so concurrent runs do not mix each other's log lines.
- Log lines go to a queue consumed by the SSE endpoint and to
  `webui/logs/<run_id>.log`.
- Status events are pushed as `__EVENT__:<json>` lines; the API layer turns
  them into SSE `status` events.

Options accepted by `start_registration`:
`want_access_token`, `want_session_token`, `want_refresh_token`, `proxy`,
`proxy_reserved`, `otp_timeout`, `allow_existing_login`, `want_2fa`.

Per-run environment overrides are passed to `AuthFlow` as `env_overrides` and
are never written into `os.environ`. The auto loop runs several workers in
one process, and mutating the global environment leaks one worker's settings
into another.

### 3.3 Error classification

`classify_error()` in `regix/services/registration.py` maps an exception to a
category that decides what happens to the mailbox and the proxy:

| Category | Trigger examples | Registrar action |
| --- | --- | --- |
| `network` | TLS, timeout, proxy, DNS, Cloudflare, `invalid_state` | release the mailbox back to `available` |
| `mail_limited` | the mailbox itself is exhausted (one-shot mailboxes) | mark the mailbox failed, never reuse |
| `cf_blocked` | Cloudflare blocked the exit IP | release the mailbox, count the proxy failure, possibly disable a static proxy |
| `account` | the address is already registered or unusable | mark the mailbox failed, do not reuse |
| `unknown` | everything else | mark failed, keep the logs |

Do not replace this with ad-hoc string matching at the call site; the table
above is the contract between the worker and the account pool.

### 3.4 Payment links

`regix/services/payment.py` creates a Stripe checkout session for a Plus or
Team subscription and returns every link that belongs to it. The flow mirrors
the reference toolbox (`1837620622/chatgpt-specimen-toolbox`) and has three
steps:

1. `POST https://chatgpt.com/backend-api/payments/checkout` with the
   account's access token. Returns `checkout_session_id`, `client_secret`,
   `publishable_key` and `processor_entity`. `url` is usually `null` in
   `hosted` mode.
2. `POST https://api.stripe.com/v1/payment_pages/{cs_id}/init` (form
   encoded, `Authorization: Bearer <publishable_key>`). Returns
   `stripe_hosted_url`.
3. Rewrite the host `checkout.stripe.com` -> `pay.openai.com`.

Step 2 is not optional. OpenAI's `hosted` mode regularly returns a URL
without the `#fid` fragment, and a long link rebuilt from `client_secret`
alone then refuses to open. Only Stripe's own init response carries the
authoritative fragment. When the init call fails the service degrades to the
URL from step 1 (or one rebuilt from the client secret) and reports the
reason through `PaymentLink.warnings`; it does not fail the whole request.

Request body rules for step 1 (they are exact, do not add fields):

- Send: `plan_name`, `checkout_ui_mode` (`hosted`), `billing_details`
  (`country` + `currency`), `cancel_url`. Team adds `team_plan_data`
  (`workspace_name`, `price_interval`, `seat_quantity`).
- Never send: `success_url` (ChatGPT decides the return path itself, and a
  wrong one sends PayPal to a "register a new account" page),
  `entry_point` / `promo_campaign` (they corrupt the Stripe session and the
  long link stops opening), `locale` (it follows the browser), and
  `check_card_proxy` (retired field).

Links produced by `build_payment_urls()`:

| Key | Form | Use |
| --- | --- | --- |
| `openai` / `external` | `https://pay.openai.com/c/pay/{cs}#fid...` | standalone long link, opens in a clean browser |
| `stripe` | `https://checkout.stripe.com/c/pay/{cs}#fid...` | mirror of the same session on Stripe's own host |
| `internal` | `https://chatgpt.com/checkout/{entity}/{cs}` | ChatGPT wrapper, only works in the account's own logged-in browser |

`processor_entity` is `openai_ie` for the eurozone/UK and `openai_llc`
elsewhere; the response carries it, `infer_merchant_path()` is the fallback
derived from the billing country.

**Credentials never leave the backend.** The WebUI and the CLI send the
account email plus the payment options; the access token, the chatgpt.com
cookie header and the device id are read from the `registered` row inside
`generate_payment_link_for_account()`, and the proxy falls back to the proxy
the account was registered with.

Front end: `POST /api/payment/link` and `GET /api/payment/profiles` in
`regix/api/routers/payment.py`; the WebUI button lives in
`webui/frontend/src/views/Registered.vue` and opens
`webui/frontend/src/components/PaymentLinkDialog.vue`. One account per click:
a checkout session belongs to one account, so a batch would produce links
that do not match the account they are shown next to.

---

## 4. Storage

SQLite, one file, accessed through `regix/storage/facade.py`. The default path
is `webui/webui.db`; override it with `WEBUI_DB_PATH` (Docker mounts it at
`/app/data/webui.db`).

| Table | Purpose | Key columns |
| --- | --- | --- |
| `outlook_accounts` | Mailbox pool | `email`, `kind`, `status` (`available`/`in_use`/`done`/`failed`), credentials |
| `registered` | Finished accounts | `email`, `password`, `access_token`, `totp_secret`, `totp_factor_id`, `session_json`, `extra_json` |
| `runs` | One row per registration attempt | `run_id`, `email`, `status`, `error`, `error_category`, `log_path` |
| `proxy_pool` | Exit IP inventory | `proxy`, `enabled`, `max_uses`, `used_count`, `failed_count`, `proxy_type` |
| `remail_orders` | Remail mailbox orders | `order_no`, `email`, `order_status`, `service_token` |
| `settings` | Key/value backend configuration | `key`, `value` (JSON blobs for `prefs`) |

Storage rules:

- Claiming a mailbox must be atomic (`claim_account` / `claim_next` use a
  conditional `UPDATE` and re-check the row count). Two workers must never get
  the same address.
- `registered` writes are upserts keyed by email; a later save must not wipe a
  previously stored password or TOTP secret.
- New columns need a migration in `init_db()` that works on an existing
  database (see the `ALTER TABLE` pattern for `totp_secret` / `kind`).
- `session_json` and `extra_json` are stored separately on purpose: exports
  need the raw session, the UI needs the metadata.
- `device_id` is the `oai-did` of the account. It must be written to the
  `registered` row: without it a later login or API call for that account
  cannot reuse the device the account was created with. The registrar's
  result whitelist in `regix/services/registration.py` used to drop it (the
  accounts registered before 2026-11-07 have an empty `device_id`); keep it in
  the whitelist and keep the `save_registered` column mapping.
- `registered.extra.proxy` is the per-account exit IP. It can be edited from
  the WebUI (`POST /api/registered/update_proxy`) and
  `db.update_registered_proxy()` moves pool quota with it: the old proxy is
  released, the new one is reserved when it is in the pool. Do not write
  `extra_json` from a caller without going through that function, or the
  quota accounting will drift.

---

## 5. Extending the system

### 5.1 Adding a mail provider

1. Create `regix/providers/mail/<name>.py` with a class extending
   `MailProvider`.
2. Declare `kind`, `display_name`, `pooled`, `ephemeral`, `line_segments`,
   `import_hint`, and `config_fields`.
3. Implement `create_mailbox()` / `wait_for_otp()` (and `peek_otp()` if the
   provider can cheaply look before the mail is polled).
4. Implement `parse_line()` for the import format and raise
   `ImportValidationError` with a line number for malformed input.
5. Register it in `regix/providers/mail/__init__.py` (one import line).

The registry drives the WebUI form, the CLI, the import parser, and the
auto-loop mailbox claim. The core library must not change.

### 5.2 Adding an SMS provider

Extend `BaseSmsProvider` in `regix/providers/sms/base.py` and register it in
`regix/providers/sms/factory.py`. The two-phase contract is
`get_number()` then `get_code()`, followed by `report_success()` / `cancel()`.
The optional hooks `mark_code_failed`, `mark_send_failed`,
`mark_send_succeeded`, and `set_resend_callback` exist for providers that
support reuse and resend.

### 5.3 Adding an API route

Put it in the matching router under `regix/api/routers/`. Validate with a
Pydantic model, delegate to a service, and return a small JSON envelope. If
the route needs a long-running job, use `registrar.start_registration()` and
stream `GET /api/runs/{run_id}/stream`.

### 5.4 Adding a CLI command

Add it to `regix/cli/main.py` (`build_parser()` plus a `cmd_*` function). The
CLI must reuse the same services and the same storage facade as the WebUI. For
anything not worth a dedicated subcommand, use `regix api METHOD /api/...`
instead of reimplementing router logic in Python.

---

## 6. CLI

```bash
regix register --totp                 # single run from the current pool
regix register --pool-proxy           # pick an exit IP from the proxy pool
regix register <email> --no-totp      # a specific mailbox
regix status                          # pool, proxy, prefs, running tasks
regix accounts --status available
regix runs --limit 10
regix registered --limit 10
regix codex-login <email>              # one-time Codex OAuth for accounts without a RT
regix bind-2fa <email>                 # add TOTP to an account that has none (secret is kept)
regix set-proxy <email> [proxy-url]    # per-account proxy (empty clears it)
regix payment-link <email>             # Plus/Team checkout links
regix payment-link <email> --promo-campaign plus-1-month-free
regix payment-link <email> --plan team --seats 5 --interval year
regix payment-profiles                 # list the country/currency presets
regix api get /api/settings/prefs     # passthrough for everything else
regix legacy 'email----pass----cid----refresh_token'
```

- `regix register` reads the WebUI preferences by default; `--totp` and
  `--no-totp` override `want2fa`.
- The CLI masks passwords, TOTP secrets, and tokens. `--show-secrets` prints
  them in clear text, and the CLI also redacts `pw=` inside forwarded log
  lines unless `--show-secrets` is given.
- `regix legacy` (also installed as `regix-legacy`) is the old four-field
  Outlook runner. Keep it working; it is the documented escape hatch.
- `regix payment-link` reads the token from the local database, so it needs
  no credential on the command line. The billing country follows the exit IP
  (see 2.13); `--profile`/`--country` only make sense when they match it.
- The console scripts are declared in `pyproject.toml`. If you change them,
  remember that an editable install in a venv without pip keeps the old
  generated script until it is regenerated.

---

## 7. WebUI

- Backend: `regix/api/app.py` builds the FastAPI app and mounts the routers.
- Admin auth is HTTP Basic over the whole app (API, SSE, and static files)
  and is only enabled when `WEBUI_ADMIN_PASSWORD` is set. Ten failed attempts
  within five minutes returns 429. Basic auth does not encrypt; put TLS in
  front of it for public deployments.
- Frontend: Vue 3 + Element Plus + Pinia under `webui/frontend/`. Vite writes
  the build output to `webui/static/`.
- After changing the frontend you must rebuild and commit the generated
  assets:

  ```bash
  npm --prefix webui/frontend run build
  ```

  The hashed filenames change on every build, so `webui/static/index.html` and
  the asset directory must be committed together. Verify that every asset
  referenced by `webui/static/index.html` exists.
- Delivery settings are part of the page weight:
  - `GZipMiddleware(minimum_size=1024)` compresses bundles and JSON. Never
    compress SSE (`/api/auto/stream`) - buffering breaks the stream.
  - Hashed files under `/static/assets/` are served with
    `Cache-Control: public, max-age=31536000, immutable`; `index.html` is
    `no-cache` so a deploy is picked up immediately. Do not make the shell
    cacheable.
  - Element Plus CSS is imported on demand (`ElementPlusResolver({
    importStyle: 'css' })`). The full `element-plus/dist/index.css` is 358 KB;
    keeping the on-demand setup brings the shared CSS to about 100 KB.
    Programmatic components bypass template resolution, so their styles are
    imported explicitly in `main.js` (message, message-box, loading). Add the
    style import there if another programmatic component is introduced.
- The "注册结果" page carries one standalone button, "生成支付链接", which
  creates a Plus/Team checkout link for the selected account (see 3.4). It
  needs exactly one selected row; the dialog picks the payment preset that
  matches the current exit country and shows the long link, its Stripe
  mirror and the ChatGPT wrapper.

---

## 8. Configuration

Backend configuration lives in the `settings` table (via `db.get_setting`,
`db.save_mail_config`, `db.save_sms_config`), and UI preferences live in the
`prefs` JSON blob. Environment variables are for process-level switches only.

| Variable | Purpose |
| --- | --- |
| `PROXY` | Default outbound proxy URL |
| `OTP_TIMEOUT` | Email OTP wait time in seconds (floor 60 in the registrar) |
| `WEBUI_ALLOW_LOGIN` | `1` allows login fallback when the address is already registered |
| `SKIP_OAUTH_TOKEN_EXCHANGE` / `OAUTH_CODEX_RT_EXCHANGE` | Control the refresh-token chain |
| `OPENAI_SENTINEL_NODE_PATH` | Path to the `node` binary |
| `OPENAI_SENTINEL_BEHAVIOR_MS` | Sentinel behavior floor in ms (default 3400, `0` disables) |
| `OPENAI_SENTINEL_BEHAVIOR_JITTER_MS` | Behavior jitter in ms (default 250) |
| `AUTH_HTTP_TRACE` / `AUTH_TRACE_DUMP` | Per-request logging and full request/response dumps |
| `WEBUI_DB_PATH` / `WEBUI_LOG_DIR` | SQLite and log locations |
| `WEBUI_ADMIN_USERNAME` / `WEBUI_ADMIN_PASSWORD` | WebUI Basic auth |

The full table lives in `README.md`.

---

## 9. Secrets and privacy

The repository must never receive credentials.

- `.gitignore` already blocks `.env`, `*.db`, `data/`, `webui/logs/`,
  `account_*.json`, `mail.txt`, and similar. Keep it that way; if you add a new
  artifact type that can contain credentials, add an ignore rule in the same
  commit.
- Passwords, TOTP secrets, access/session/refresh tokens, mailbox refresh
  tokens, proxy credentials, SMS API keys, and OTPs must not appear in log
  lines, test output, API responses, or documentation. The registrar logs the
  generated password on failure as a last-resort recovery line; keep that
  exception and keep it out of every other path.
- API responses mask secrets by default; the CLI masks them unless
  `--show-secrets` is passed.
- When reporting a real registration, report the run id, status, field
  *lengths*, and the failure category. Never paste the values.

---

## 10. Testing

```bash
.venv/bin/python -m compileall -q regix
.venv/bin/python -m pytest
```

`tests/conftest.py` points `WEBUI_DB_PATH` at a temporary database before any
application import, so tests never touch the production database. Use the
`clean_db` fixture when a test needs real tables.

Guidance:

- Cover behavior, not source text. A few legacy contract tests still assert on
  source strings; do not add new ones.
- For timing-sensitive code, inject the clock or patch `time.sleep` and assert
  on the call sequence (see `tests/test_two_factor_timing.py`).
- For concurrency, use real threads and events, not sleeps
  (`tests/test_outlook_race.py`).
- For Node behavior, keep the test hermetic: a tiny fake `sdk.js` in a temp dir
  is enough to assert that the process exits after writing its output
  (`tests/test_sentinel_quickjs.py`).

Additional checks by change type:

| Change | Extra check |
| --- | --- |
| Sentinel wrapper | `node --check regix/openai/openai_sentinel_quickjs.js` plus a local SDK-cache benchmark |
| Frontend | `npm --prefix webui/frontend run build`, then commit `webui/static` |
| Real registration | explicit user authorization, TOTP enabled, report run id and field lengths only |

---

## 11. Performance and timing

Read `docs/PERFORMANCE.md` before touching any default timing. Summary of the
current numbers (real runs, TOTP enabled, Codex refresh token disabled):

| Milestone | Total |
| --- | ---: |
| Historical baseline | 71.2s |
| After the Node exit fix | 48.3s |
| After Sentinel timing/jitter work | 36.0s |
| After the SO skip and mail race | 31.6s |

Rules:

- Collect at least five real successful runs before changing a default
  rhythm. One run is dominated by mail arrival and network latency.
- Randomize anything a server can observe as a cadence: polling intervals,
  behavior floors, cooldowns. Keep protocol-correctness waits deterministic
  unless there is evidence.
- `OPENAI_SENTINEL_BEHAVIOR_MS=0` is a local benchmark switch only. It must
  never be used for a production registration that needs an SO token.
- Do not optimize the parts that protect correctness: the proxy check, the
  warmup, and the retry that rebuilds the session after `invalid_state`.
- Do not reuse one Node VM across registrations. SDK state and fingerprint
  isolation are worth more than the roughly 140 ms process start.

---

## 12. Deployment

- `Dockerfile` is a multi-stage build: Vue build, Python deps with a compiler
  toolchain, then a slim runtime with a single `node` binary copied from
  `node:22-slim`. The build stage verifies `node -e` so a missing shared
  library fails at build time instead of breaking Sentinel at runtime.
- The container exposes port 8765 and health-checks `/api/stats`.
- Mount the data directory so the mailbox pool and credentials survive a
  container rebuild.
- `docker compose up -d` is the normal deployment path.

---

## 13. Git and workspace hygiene

- This repository frequently has uncommitted work that belongs to the user.
  Run `git status --short` before editing, touch only the files your task
  needs, and never revert, reformat, or "fix" unrelated changes.
- Keep commits logical: one feature per commit, with a message body that
  explains the observable change rather than listing files.
- Do not rewrite published history. Rebases are only acceptable on unpushed
  local commits, and the final tree must be verified identical afterwards.
- If the worktree contains a mix of your changes and the user's, stage by
  path rather than with `git add -A`.

---

## 14. Troubleshooting map

| Symptom | Likely cause | Where to look |
| --- | --- | --- |
| Registration succeeds but no email arrives | Sentinel token rejected by deep validation | `regix/openai/quickjs.py`, `webui/logs/<run_id>.log` |
| `409 invalid_state` | missing `oai-did`, or a session was rebuilt mid-flow | `warmup()` in `regix/openai/protocol.py`, the `AuthFlow` session rebuild sites |
| Cloudflare 403 | missing client hints, or a burned static proxy | `_navigation_headers()`, `proxy_pool.cf_blocked` |
| `curl: (35)` | link-level TLS blip (about 5% of calls) | `_TlsRetrySession` in `regix/http/client.py` |
| Graph API `AADSTS90023` | expected for this pool; IMAP carries the load | `regix/providers/mail/outlook.py` |
| OTP never arrives | mailbox exhausted, or the code came from `tm1.openai.com` | `_check_from_domain`, `outlook_accounts.fail_reason` |
| Account has no TOTP secret | binding failed after registration | `regix/services/two_factor.py`, `registered.totp_secret` |
| Node process lingers after printing | wrapper stopped exiting | `regix/openai/openai_sentinel_quickjs.js` `emit()` |
| Run marked failed but the account exists | the failure happened after `POST user/register` | the registrar's "该号已生成密码" log line |

When a real run fails, the per-run log at `webui/logs/<run_id>.log` and the
`runs.error` / `runs.error_category` columns are the first two places to look.
