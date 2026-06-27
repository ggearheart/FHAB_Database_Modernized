# Deployment (Render)

Host the staff web app on the web with a managed PostgreSQL + PostGIS database, using the
[`render.yaml`](../render.yaml) blueprint. This path is intended as a **public demo** for
stakeholders; see "Production hardening" for what a real agency deployment needs.

## What the blueprint provisions

- **`fhab-db`** — a managed PostgreSQL (PostGIS enabled by the schema's `CREATE EXTENSION`).
- **`fhab-web`** — a Python web service running `gunicorn wsgi:app`, with automatic HTTPS.

The web service's start command runs [`scripts/deploy_setup.py`](../scripts/deploy_setup.py)
first (idempotent): it applies the schema + access control and bootstraps the admin account.

## Steps

1. The repo is already public: <https://github.com/ggearheart/FHAB_Database_Modernized>.
2. In Render → **New +** → **Blueprint** → pick this repo. Render reads `render.yaml`.
3. Set two env vars on the **fhab-web** service before the first deploy:
   - `ADMIN_EMAIL` — the first administrator's login.
   - `ADMIN_PASSWORD` — a strong temporary password (change it after first sign-in).
   (`DATABASE_URL` and `SECRET_KEY` are wired automatically.)
4. Deploy. On boot, `deploy_setup` creates the tables, enables PostGIS, and seeds the admin.
5. Open the service URL, sign in as the admin, and create staff accounts under **Accounts**.

## Loading reference data (optional)

The app runs without it, but the geospatial features need the reference layers. From the
Render **Shell** (or any host with `DATABASE_URL` set):

```bash
python scripts/fetch_reference_data.py && python scripts/init_db.py --load   # CA FHAB open data
python scripts/fetch_ceden_stations.py                                       # station registry
python scripts/fetch_huc12.py && python scripts/init_db.py --huc12 data/raw/huc12.geojson
```

> Note: the free database tier is small; the HUC-12 layer (~4,700 polygons) and full open-data
> load are sizeable. For a light demo you can skip these and enter reports directly.

## How access control works in the hosted setup

The app connects as the database's app user (from `DATABASE_URL`). That user **owns** the
tables (so it bypasses RLS by default) but is **not** a superuser, so `access_control.sql`
runs `GRANT fhab_app TO current_user`, letting the app `SET ROLE fhab_app` for data
operations — that's how `acting_as` enforces Row-Level Security per logged-in user. Admin
account management runs as the app user directly (gated to `program_admin` in the app).

## Production hardening (beyond the demo)

1. **Dedicated, least-privilege DB role.** Have the web app log in as a *non-owning* role
   (member of `fhab_app`, with DML only on `app_user`/`user_role`/`role`) so RLS is always on
   with no owner-bypass path. Reserve the owning/superuser role for migrations and loaders.
2. **Migrations.** `schema.sql` is create-only; adopt a migration tool (Alembic / sqitch /
   plain versioned SQL) so a live database evolves without resets.
3. **Auth.** Replace local passwords with SSO/SAML against Water Boards identity; enforce
   `SESSION_COOKIE_SECURE`, `HttpOnly`, and `SameSite`; rotate `SECRET_KEY` via secrets.
4. **Backups, monitoring, and a paid tier** (the free Render Postgres expires and sleeps).
5. **Agency hosting.** A real system would run on the Water Boards' own cloud (Azure/AWS) with
   their networking and compliance controls; the Docker-portable path supports that.
