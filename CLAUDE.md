# Quest DNC Checker — Project Context

## What this project is

A SaaS web application for phone-number DNC (Do Not Call) compliance scrubbing. Users upload CSV/TXT files containing phone numbers; background workers check each number against Federal DNC, State DNC, and TCPA Litigator registries and return a clean-number CSV. Access is credit-based; credits are purchased via Stripe.

---

## Tech stack


| Layer | Technology |
|---|---|
| Web framework | Django 4.2 |
| Async tasks | Celery 5 + Redis broker |
| Database | PostgreSQL 16 |
| Cache | Redis 7 |
| Static files | WhiteNoise (dev/Railway) / Nginx (Docker) |
| Payments | Stripe (PaymentIntent + SetupIntent) |
| Auth | Django sessions, custom `accounts.CustomUser` (email-based) |
| Deployment | Railway (primary), Docker + Nginx (self-hosted) |

---

## Directory layout

```
quest-dnc-checker/          ← Django project root (manage.py lives here)
├── accounts/               ← Auth: CustomUser model, login/register/profile views
├── admin_panel/            ← Internal admin dashboard (client/ticket/payment mgmt)
├── api/                    ← Vercel WSGI wrapper (api/index.py)
├── billing/                ← Credits, Stripe PaymentIntent/SetupIntent, webhooks
├── scrubber/               ← Core feature: file upload, DNC engine, Celery task
│   ├── dnc.py              ← DNC check logic (federal/state/litigator)
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
`stripe_customer_id`, `role` (CLIENT | ADMIN). Helper: `display_name`, `is_admin`.

### `scrubber.ScrubJob`
One file-scrub request. Fields: `job_id` (SCR-XXXXXXXX), `user`, `filename`, `file`,
`scrub_types` (JSONField list), `status` (PENDING→QUEUED→PROCESSING→COMPLETED|FAILED),
`total`, `clean`, `dnc`, `state_dnc`, `litigator`, `result_file`, `error_message`.

### `billing.CreditTransaction`
Immutable credit ledger. Type: PURCHASE | USAGE | REFUND | ADJUSTMENT. `amount` is
negative for USAGE (consumed credits).

### `billing.PaymentMethod`
Stored Stripe payment cards linked to a user.

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

Email is sent by the Celery worker after job completion. Configure via env vars:

```
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_HOST_USER=you@example.com
EMAIL_HOST_PASSWORD=app-password
DEFAULT_FROM_EMAIL=noreply@questdnc.com
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
| `STRIPE_PUBLISHABLE_KEY` | Stripe frontend key |
| `STRIPE_SECRET_KEY` | Stripe backend key |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook signing secret |
| `EMAIL_HOST_USER/PASSWORD` | SMTP credentials |
| `DEBUG` | Set `False` in production |
| `ALLOWED_HOSTS` | Comma-separated hostnames |
| `CSRF_TRUSTED_ORIGINS` | Comma-separated HTTPS origins |

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

## Billing / Stripe

- Credit tiers: Starter $20→100K, Professional $50→500K, Enterprise $75→1M
- `billing/stripe_utils.py` — PaymentIntent creation, SetupIntent for card saving
- `billing/webhooks.py` — handles `payment_intent.succeeded` (credits top-up) and `setup_intent.succeeded` (card save)
- Stripe webhook endpoint: `POST /billing/webhook/` (CSRF-exempt)

---

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
