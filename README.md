# CRM Intelligence Service

Deterministic FastAPI service that scans Zoho CRM and maintains deduplicated records
in the `CRM_Alerts` module. It supports SQLite for local use and PostgreSQL for
production. No ML, LLM, embedding, or vector-database features are included.

## Rules

- Account Without Contact
- Account Without Deal
- Account Without Quote
- Deal Without Quote
- Stale Account
- Stale Deal
- Incomplete Account Profile

Rules scan all source records, reopen a matching resolved alert when a condition
returns, and resolve an open/in-progress alert when the condition clears. Zoho
module, field, and related-list API names remain configurable. In particular,
verify `ZOHO_DEAL_QUOTES_RELATED_LIST` against your org's Related List Metadata;
the default `Quotes` cannot be guaranteed for every Zoho organization.

## Local Windows setup (SQLite)

```bat
cd /d "C:\Users\HariomMishra\Desktop\crm ai"
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
copy .env.example .env
alembic upgrade head
python -m pytest -q
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

For an existing unversioned database created by an older version, back it up and run
this once instead of a direct initial upgrade:

```bat
copy crm_intelligence.db crm_intelligence.pre-alembic-backup.db
alembic stamp 0001
alembic upgrade head
```

## PostgreSQL

Set `DATABASE_URL` without committing credentials:

```text
DATABASE_URL=postgresql+psycopg://user:password@host:5432/crm_intelligence
```

Then apply migrations:

```bat
alembic upgrade head
alembic current
```

SQLite receives its required thread option. PostgreSQL uses connection health
checks and a small production pool. The application does not apply SQLite-only
connection arguments to PostgreSQL.

## Configuration

Required:

```text
ZOHO_CLIENT_ID=
ZOHO_CLIENT_SECRET=
ZOHO_REFRESH_TOKEN=
ZOHO_ACCOUNTS_URL=https://accounts.zoho.in
ZOHO_API_DOMAIN=https://www.zohoapis.in
DATABASE_URL=sqlite:///./crm_intelligence.db
```

Scheduling defaults to disabled:

```text
SCHEDULE_ENABLED=false
SCHEDULE_HOUR_IST=8
```

Set `SCHEDULE_ENABLED=true` to run all rules daily at the configured Asia/Kolkata
hour. Full scans run sequentially and an overlapping full scan is skipped.

`ALERT_DESCRIPTION_FIELD`, `ALERT_RESOLVED_ON_FIELD`, and
`ALERT_DAYS_OPEN_FIELD` are optional. Leave them blank unless those fields exist in
Zoho. New alerts use `Open`; lifecycle updates use `Resolved` and safely reopen the
same Unique Key.

## API

- `GET /health` — application process health only
- `GET /ready` — database connectivity and required Zoho configuration
- `GET /scan-runs?limit=50` — recent persisted scan history
- `POST /scans/account-without-contact`
- `POST /scans/account-without-deal`
- `POST /scans/account-without-quote`
- `POST /scans/deal-without-quote`
- `POST /scans/stale-accounts`
- `POST /scans/stale-deals`
- `POST /scans/incomplete-account-profile`
- `POST /scans/all` — all rules sequentially; failures do not stop later rules

`/ready` reports Zoho as `configured`; it deliberately does not create an OAuth
token or scan CRM records on every readiness probe.

## Large datasets and API safety

Get Records and Get Related Records use numeric pages through 2,000 records and
then Zoho V8 `next_page_token` pagination. Requests retain minimal `fields=id` for
related-record existence checks. Repeated/missing tokens raise an error instead of
silently returning incomplete results. HTTP 429 and 5xx responses receive bounded
backoff; permanent validation errors do not retry. Zoho documents a 100,000-record
maximum and 24-hour token lifetime for this pagination method.

## Docker with PostgreSQL

Set a password in the shell (and keep Zoho credentials in `.env`), then start:

```bat
set POSTGRES_PASSWORD=replace_with_a_strong_password
docker compose up --build
```

The API container waits for PostgreSQL health, runs `alembic upgrade head`, and
starts Uvicorn. Stop it with:

```bat
docker compose down
```

The PostgreSQL volume is retained. Add `-v` only if you intentionally want to delete
the local database volume.

## Production command

```sh
alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
```

Before deployment, configure secrets, provision PostgreSQL, apply migrations, verify
all Zoho API/related-list names using metadata, and run controlled rule-specific
tests against the target Zoho organization before enabling the scheduler.
