# Quest DNC Checker — Project Context

## What this project is

A SaaS web application for phone-number DNC (Do Not Call) compliance scrubbing. Users upload CSV/TXT files containing phone numbers; background workers check each number against Federal DNC and State DNC registries and return a clean-number CSV. Access is credit-based; credits are purchased by card via hosted Stripe Checkout (re-added Sep 2026) or via a manual WhatsApp flow (message support, admin grants credits). PayPal was removed in Aug 2026.

---

## Tech stack


| Layer | Technology |
|---|---|
| Web framework | Django 4.2 |
| Async tasks | Celery 5 + Redis broker |
| Database | PostgreSQL 16 |
| Cache | Redis 7 |
| Static files | WhiteNoise (dev/Railway) / Nginx (Docker) |
| Payments | Stripe hosted Checkout (card) + manual WhatsApp flow / admin credit grants (PayPal removed Aug 2026) |
| Auth | Django sessions, custom `accounts.CustomUser` (email-based); optional Google OAuth 2.0 sign-in/sign-up (`accounts/views.py` `google_login`/`google_callback`, hand-rolled with `requests`) |
| Deployment | Railway (primary), Docker + Nginx (self-hosted) |

---

## Directory layout

```
quest-dnc-checker/          ← Django project root (manage.py lives here)
├── accounts/               ← Auth: CustomUser model (roles: client/agent/admin), login/register/profile views
├── agents/                 ← Agent promo code system: AgentPromoCode model, code generation, expiry task
│   ├── models.py           ← AgentPromoCode (code, sequence, status, expires_at, used_by)
│   ├── utils.py            ← generate_next_promo_code() — {LAST4}DNC26{NNNN} format
│   └── tasks.py            ← expire_promo_codes Celery beat task (hourly)
├── admin_panel/            ← Internal admin dashboard (client/ticket/payment mgmt)
├── api/                    ← Vercel WSGI wrapper (api/index.py)
├── billing/                ← Credits: pricing tiers, payment/credit-ledger models, billing page
│   ├── stripe_utils.py     ← All Stripe SDK calls (customer, Checkout Session, webhook verify)
│   ├── services.py         ← fulfil_checkout_session(): idempotent credit grant + Invoice email
│   └── views.py            ← billing_home, create_checkout, checkout_success/cancel, stripe_webhook
├── scrubber/               ← Core feature: file upload, DNC engine, Celery task
│   ├── dnc.py              ← DNC check logic; Redis result cache (bulk MGET/MSET, 7-day TTL)
│   ├── phone.py            ← Phone normalisation + file parsing
│   ├── tasks.py            ← process_scrub_job Celery task
│   ├── views.py            ← scrubber_home + job_status (AJAX) + upload handler
│   └── urls.py             ← /scrubber/ and /scrubber/status/<job_id>/
├── support/                ← Support ticket model + views
├── quest_dnc/              ← Django config package
│   ├── settings.py
│   ├── urls.py
│   ├── celery.py
│   ├── wsgi.py
│   └── asgi.py
├── templates/              ← All HTML templates
│   ├── base.html           ← Shared layout: sidebar, topbar, mobile hamburger
│   ├── base_auth.html      ← Auth pages layout (login/register)
│   ├── dashboard.html
│   ├── scrubber/home.html  ← AJAX upload + drag-drop + real-time polling
│   ├── billing/home.html
│   ├── support/home.html
│   └── admin_panel/        ← Admin dashboard templates
├── static/                 ← Project-level static files (currently CDN-only)
├── media/                  ← User uploads + result CSVs (gitignored)
├── staticfiles/            ← collectstatic output (gitignored)
├── Dockerfile
├── docker-compose.yml      ← Full stack: web + worker + beat + db + redis + nginx
├── nginx/nginx.conf        ← Nginx reverse-proxy config for Docker
├── Procfile                ← Railway: web + worker + beat
├── railway.toml
├── requirements.txt
└── .env.example
```

---

## Key models

### `accounts.CustomUser`
Custom auth user. Fields: `email` (login), `name`, `phone`, `company`, `credits` (float),
`stripe_customer_id`, `role` (CLIENT | AGENT | ADMIN). Helper: `display_name`, `is_admin`.

### `agents.AgentPromoCode`
Promo codes for agent referrals. Format: `{LASTNAME4}DNC26{NNNN}` (0001–10000 per agent).
Fields: `agent` (FK→User), `code`, `sequence`, `status` (active/expired/used), `created_at`,
`expires_at` (created_at + 7 days), `used_by` (FK→User), `used_at`.
One ACTIVE code per agent at a time. Hourly beat task `expire_promo_codes` auto-rotates codes.
Applying a valid code at signup credits the new client with 100,000 credits (on top of the
10,000 free signup credits every new account receives; see `SIGNUP_FREE_CREDITS` in settings).

### `scrubber.ScrubJob`
One file-scrub request. Fields: `job_id` (SCR-XXXXXXXX), `user`, `filename`, `file`,
`scrub_types` (JSONField list), `status` (PENDING→QUEUED→PROCESSING→COMPLETED|FAILED),
`total`, `clean`, `dnc`, `state_dnc`, `result_file`, `error_message`.

### `billing.CreditTransaction`
Immutable credit ledger. Type: PURCHASE | USAGE | REFUND | ADJUSTMENT. `amount` is
negative for USAGE (consumed credits).

### `billing.Payment`
One purchase. `provider` STRIPE | PAYPAL (legacy) | MANUAL. `stripe_session_id` (unique) is the
idempotency key for Checkout fulfilment; `stripe_pi_id` is the PaymentIntent.

### `billing.PaymentMethod`
Legacy stored cards from the original (Elements-based) Stripe integration. Model kept for
historical Payment/CreditTransaction FKs; hosted Checkout does not create new records.

---

## Scrub pipeline (Celery task)

`scrubber/tasks.py → process_scrub_job(job_id)`

1. Mark job PROCESSING
2. Parse & deduplicate phone numbers from uploaded file
3. Credit pre-flight check (non-atomic fast check)
4. Atomic credit check + batch processing through `dnc.run_checks()`
5. Write clean-number result CSV to media storage
6. Mark job COMPLETED with final counts
7. Atomic credit deduction + CreditTransaction record
8. Send completion email to user

**No Celery retries** — jobs are not idempotent (double-charge risk). On failure, job is marked FAILED and user must resubmit.

---

## Real-time status updates

- `GET /scrubber/status/<job_id>/` → JSON with current job counts + status
- Frontend polls every 3 s for any job in PENDING/QUEUED/PROCESSING state
- Toast notification fires on COMPLETED or FAILED
- Table rows update in-place (no page reload)

---

## File upload flow

- Drag-and-drop or file picker on `/scrubber/`
- AJAX POST with `X-Requested-With: XMLHttpRequest` header
- XHR `upload.progress` event drives a progress bar (0–100%)
- Server returns `{"ok": true, "job_id": "SCR-..."}` on success
- Frontend immediately inserts a new table row and starts polling

---

## Email notifications

Email is sent by the Celery worker after job completion. Production uses Brevo SMTP
(credentials from https://app.brevo.com → Settings → SMTP & API). Configure via env vars:

```
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=smtp-relay.brevo.com
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_HOST_USER=8xxxxx001@smtp-brevo.com   # Brevo SMTP login
EMAIL_HOST_PASSWORD=xsmtpsib-...           # Brevo SMTP key (not the account password)
DEFAULT_FROM_EMAIL=noreply@checkdnc.net
```

During development the default console backend prints emails to the worker log.

---

## Environment variables

See `.env.example`. Critical vars:

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Django secret key |
| `DATABASE_URL` | Full Postgres DSN (overrides `DB_*` vars) |
| `REDIS_URL` | Redis DSN for broker + cache |
| `EMAIL_HOST_USER/PASSWORD` | SMTP credentials |
| `DEBUG` | Set `False` in production |
| `ALLOWED_HOSTS` | Comma-separated hostnames |
| `CSRF_TRUSTED_ORIGINS` | Comma-separated HTTPS origins |
| `GOOGLE_CLIENT_ID/SECRET` | Google OAuth web client; empty ID hides the "Continue with Google" buttons. Redirect URI: `/accounts/google/callback/` |

---

## Running with Docker

```bash
# 1. Copy and fill in env vars
cp .env.example .env

# 2. Build images
docker compose build

# 3. Start all services (db, redis, web, worker, beat, nginx)
docker compose up -d

# 4. App available at http://localhost
```

Nginx serves static files directly from the `static_data` volume.
Media files are served via the `internal` directive (Django controls download URLs).

---

## Running locally (without Docker)

```bash
# Requires: Python 3.12, PostgreSQL, Redis running locally

pip install -r requirements.txt
cp .env.example .env   # fill in DB_* and REDIS_URL

python manage.py migrate
python manage.py createsuperuser

# Terminal 1 — Django dev server
python manage.py runserver

# Terminal 2 — Celery worker
celery -A quest_dnc worker --loglevel=info

# Terminal 3 — Celery beat (optional, for scheduled tasks)
celery -A quest_dnc beat --loglevel=info \
  --scheduler django_celery_beat.schedulers:DatabaseScheduler
```

---

## Billing

- Credit tiers: Starter $10→100K, Professional $20→250K, Enterprise $50→1M
  (defined in `PRICING_TIERS`, `billing/views.py`)
- **Card (Stripe hosted Checkout):** the modal's "Pay with Card" button POSTs the tier name to
  `/billing/checkout/`; `create_checkout_session` builds a one-off `mode=payment` session with
  `price_data` from `PRICING_TIERS` (prices are never trusted from the browser) and redirects to
  Stripe. Metadata carries `user_id`, `tier_name`, `credits`.
  - Fulfilment is `billing/services.py::fulfil_checkout_session`, called from the webhook
    (`POST /billing/webhook/`, event `checkout.session.completed`) **and** from the
    success page (`/billing/checkout/success/?session_id=`) as a fallback. It is idempotent on
    `Payment.stripe_session_id`, uses `SELECT FOR UPDATE` on the user, writes Payment +
    CreditTransaction(PURCHASE) + Invoice, and queues the invoice email via Celery.
  - The success page verifies `client_reference_id` matches the logged-in user before fulfilling.
  - Env: `STRIPE_PUBLISHABLE_KEY`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`. With the keys
    empty the modal shows the Card option as "Coming soon". Sandbox = `pk_test_/sk_test_` keys.
  - Local webhook testing: `stripe listen --forward-to localhost:8000/billing/webhook/`.
  - Tests: `billing/tests.py` (Stripe SDK mocked; run with a SQLite settings override).
- **Manual:** the modal also offers a prefilled WhatsApp chat (tier, price, account email);
  an admin then grants credits via the admin panel.
- `Payment.paypal_order_id` and `PaymentMethod.stripe_pm_id` are display-only legacy data.

---

## AppSumo licensing

App `appsumo/` implements the AppSumo Licensing API (docs.licensing.appsumo.com):

- `POST /appsumo/webhook/` — HMAC-SHA256-verified webhooks (`X-Appsumo-Signature` over
  `timestamp + body`, keyed with `APPSUMO_API_KEY`). Handles purchase/activate/upgrade/
  downgrade/deactivate; every event is logged to `AppSumoWebhookEvent`. Responds
  `{"success": true, "event": ...}`. Test events (`"test": true`) are acknowledged only.
- `GET /appsumo/redirect/` — OAuth redirect. Bare GET returns 200 (AppSumo URL validation).
  With `?code=`, exchanges it at `appsumo.com/openid/token/`, fetches the license key,
  then links it to the logged-in user or stashes it in the session and sends the buyer to
  register (login/register views call `link_pending_session_license`).
- Credits per tier come from `APPSUMO_TIER_CREDITS` (default `1:100000,2:250000,3:1000000`).
  Granting is an idempotent top-up (`sync_license_credits`): upgrades grant the tier
  difference, downgrades never claw back, deactivation (refund) removes up to what the
  license granted. Env vars: `APPSUMO_CLIENT_ID`, `APPSUMO_CLIENT_SECRET`, `APPSUMO_API_KEY`.

---

## Google sign-in

`GET /accounts/google/` stores a CSRF `state` (+ optional `?promo=` code and safe `?next=`)
in the session and redirects to Google. `GET /accounts/google/callback/` verifies the state,
exchanges the code server-side, fetches userinfo, and requires `email_verified`.
Existing users (matched by email, case-insensitive) are logged in; new users are created with
an unusable password and go through the same `accounts/services.py` signup path as the
register form (free credits, promo bonus, welcome email). A bad promo code produces a warning
rather than blocking the Google signup.

## Admin access

Users with `role=ADMIN` see an "Admin" section in the sidebar.
Admin panel routes: `/panel/` (dashboard), `/panel/clients/`, `/panel/tickets/`, `/panel/payments/`.
Django admin: `/admin/`

---

## Deployment (Railway)

- Builder: nixpacks
- Start command in `Procfile`: migrate + collectstatic + gunicorn
- Worker and beat are separate Railway services pointing at the same repo
- Redis and PostgreSQL are Railway plugins (env vars auto-injected as `REDIS_URL` / `DATABASE_URL`)
- Health check: `GET /accounts/login/`

---

## Code conventions

- No Celery retries on scrub jobs (double-charge prevention)
- Credits deducted *after* job completes, not before (user not charged for failures)
- `SELECT FOR UPDATE` used in both credit check and deduction to prevent races
- AJAX endpoints detect `X-Requested-With: XMLHttpRequest` header to return JSON vs HTML
- All templates extend `base.html`; auth pages extend `base_auth.html`
- Bootstrap 5 dark theme (`data-bs-theme="dark"`) with custom GitHub-style CSS variables
