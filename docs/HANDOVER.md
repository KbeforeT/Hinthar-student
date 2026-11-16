# Hinthar SMS — Backend Handover & Bootstrap Guide

Operational runbook for standing up a fresh environment, configuring Clerk authentication and webhooks, and bootstrapping the first administrator account.

---

## 1. Architecture Summary

- **Clerk owns identity** (sign-up, sign-in, passwords, MFA, email, username).
- **Django owns authorization** (`people.User.role`, `is_active`, `is_staff`, `is_superuser`).
- New users created via Clerk JWT sign-in (`ClerkJWTAuthentication`) or Clerk webhooks (`ClerkWebhookView`) always start with `role='pending'`, `is_staff=False`, and `is_superuser=False` until promoted by an admin or `bootstrap_admin`.
- Both `role='admin'` (used by the SMS API permission classes) and `is_staff=True` / `is_superuser=True` (used by Django `/admin/` and `payroll` views) are required for full system administration.

---

## 2. Required Environment Variables & Secrets

Copy `.env.example` to `.env` for local development, or configure these in your hosting platform (e.g. Render) for production.

### Core Django & Infrastructure
| Variable | Required | Notes |
| :--- | :--- | :--- |
| `DATABASE_URL` | Yes (prod) | PostgreSQL connection string (e.g. Neon). Falls back to local `db.sqlite3` if unset. |
| `SECRET_KEY` | Yes | Unique, high-entropy Django secret key. Never reuse the local dev placeholder in production. |
| `DEBUG` | Yes | Must be `False` in production (fails closed if unset). |
| `ALLOWED_HOSTS` | Yes | Comma-separated API hostnames (custom domain + raw platform hostname). |
| `CORS_ALLOWED_ORIGINS` | Yes | Comma-separated frontend origins (e.g. `https://your-dashboard.vercel.app`). |
| `CORS_ALLOWED_ORIGIN_REGEXES` | Optional | Comma-separated regexes (e.g. `^https://.*\.vercel\.app$` for Vercel previews). |
| `REDIS_URL` | Optional | Shared cache for Clerk JWKS and dashboard stats across multiple workers. |

### Clerk Authentication & Backend API
| Variable | Required | Where to Find / Notes |
| :--- | :--- | :--- |
| `CLERK_FRONTEND_API` | Yes | Clerk Dashboard → **API Keys** (e.g. `https://<instance>.clerk.accounts.dev` or custom Clerk domain). |
| `CLERK_JWKS_URL` | Yes | `<CLERK_FRONTEND_API>/.well-known/jwks.json`. |
| `CLERK_SECRET_KEY` | Yes | Clerk Dashboard → **API Keys** (`sk_live_...` in prod, `sk_test_...` in dev). Used by `bootstrap_admin` and admin Clerk Sync / Import / Link. |
| `CLERK_JWT_ISSUER` | Optional | Defaults to `CLERK_FRONTEND_API` if unset. Set explicitly if your JWT `iss` claim differs. |
| `CLERK_JWT_AUDIENCE` | Yes (`DEBUG=False`) | Expected `aud` claim on session JWTs (e.g. `hinthar-sms-api`). Enforced whenever `DEBUG=False`. |
| `CLERK_WEBHOOK_SECRET` | Yes (for webhooks) | Clerk Dashboard → **Webhooks** → endpoint signing secret (`whsec_...`). Distinct per Clerk instance (Dev vs Prod). |
| `ENABLE_MOCK_AUTH` | No | Keep `false` or unset in production. Only active when **both** `DEBUG=True` and `ENABLE_MOCK_AUTH=true`. |

---

## 3. Clerk Dashboard Configuration

### A. Session Token Claims (Required)
In **Clerk Dashboard → Sessions → Customize session token**, add:

```json
{
  "email": "{{user.primary_email_address}}",
  "username": "{{user.username}}",
  "aud": "hinthar-sms-api"
}
```

- `aud` must match `CLERK_JWT_AUDIENCE` on the Django backend (required when `DEBUG=False`).
- If you want real usernames synced from Clerk, enable **Username** under **User & authentication**.

### B. Webhooks Setup
1. In **Clerk Dashboard → Webhooks**, click **Add Endpoint**.
2. Set the **Endpoint URL** to the Django backend directly (not a Next.js proxy):
   ```text
   https://<your-api-host>/api/v1/webhooks/clerk/
   ```
3. Subscribe to these **User** events:
   - `user.created` — Provisions the Django `User` row as `role='pending'`.
   - `user.updated` — Syncs `email` and `username` without touching `role`; deactivates (`is_active=False`) if `banned` is `true`.
   - `user.deleted` — Soft-deactivates (`is_active=False`) the Django `User` row while preserving audit logs and linked records.
4. Copy the **Signing Secret** (`whsec_...`) into `CLERK_WEBHOOK_SECRET` on the Django host.

> **Note:** Development and Production Clerk instances each generate their own `CLERK_SECRET_KEY` and `CLERK_WEBHOOK_SECRET`. Never mix dev and prod secrets.

---

## 4. Fresh Setup & Bootstrapping the First Admin

Run these steps once after deploying to a new database:

### Step 1: Run Migrations
```bash
python manage.py migrate
```

### Step 2: Create the Admin User in Clerk
Create or sign up the initial owner/administrator account in the target Clerk instance (via Clerk Dashboard → **Users → Create user**, or by signing up through the frontend).

### Step 3: Run `bootstrap_admin`
With `CLERK_SECRET_KEY` configured in the environment, run:

```bash
# By email (recommended)
python manage.py bootstrap_admin --email admin@school.org

# Or by Clerk user ID
python manage.py bootstrap_admin --clerk-id user_2abc123...
```

For non-interactive CI/deploy scripts, you can also pass environment variables:
```bash
BOOTSTRAP_ADMIN_EMAIL=admin@school.org python manage.py bootstrap_admin
# or
BOOTSTRAP_ADMIN_CLERK_ID=user_2abc123... python manage.py bootstrap_admin
```

**What `bootstrap_admin` does:**
- Fetches the user directly from Clerk's Backend API (`CLERK_SECRET_KEY`), so it works even before webhooks or the frontend are live.
- Upserts the local `people.User` row with the real `clerk_id` (avoiding the dummy `clerk_id` / duplicate-row problem of `manage.py createsuperuser`).
- Sets `role='admin'`, `is_active=True`, `is_staff=True`, and `is_superuser=True` (pass `--no-superuser` if you only want `role='admin'` + `is_staff=True`).
- Is **idempotent** — safe to re-run without creating duplicate users.

---

## 5. Post-Bootstrap & Handover Checklist

- [ ] **Verify health check:** `GET /health/` returns `200 OK`.
- [ ] **Verify admin login:** Sign in with the bootstrapped Clerk account on the frontend and confirm access to user management.
- [ ] **Verify webhook delivery:** Create or update a test user in Clerk Dashboard and confirm `200 OK` in Clerk's Webhook Delivery Logs (`POST /api/v1/webhooks/clerk/`).
- [ ] **Add additional staff/admins:** Have new users sign up in Clerk (or import them via Admin Clerk Sync/Import), then assign their `role` (`admin`, `staff`, `terminal`, `teacher`, `student`) from the admin dashboard or `/admin/`.
- [ ] **Protect against lockout:** The backend blocks deleting or deactivating your own account or the last active admin (`people/user_guards.py`). Keep at least two active `admin` accounts in production for redundancy.
- [ ] **Rotate handover secrets:** After handover, rotate `SECRET_KEY`, `CLERK_SECRET_KEY`, `CLERK_WEBHOOK_SECRET`, and database credentials so previous maintainers no longer hold production access.
