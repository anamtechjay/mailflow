# GCP Cloud SQL for PostgreSQL — provisioning (mailflow `state="postgresql://..."`)

Chosen host for mailflow's cursor/dedupe/DLQ bookkeeping (`stores/postgres.py`), replacing
the local-only Postgres used during development. **No code changes needed** — Cloud SQL's
standard connection method (the Auth Proxy, step 4) hands the app a plain `localhost`
Postgres, so `state="postgresql://user:pass@127.0.0.1:5432/mailflow"` works exactly like it
did against the local dev server. This also fits naturally alongside the existing Gmail
path, which already runs on GCP (Pub/Sub, `GOOGLE_APPLICATION_CREDENTIALS`).

## 1. Create the Cloud SQL instance (you likely already have a GCP project from Gmail/Pub/Sub)

```bash
gcloud sql instances create mailflow-pg \
  --database-version=POSTGRES_17 \
  --tier=db-g1-small \
  --region=<REGION> \
  --storage-size=10GB \
  --no-assign-ip \
  --network=<VPC_NETWORK>
# RECORD: instance connection name = <PROJECT_ID>:<REGION>:mailflow-pg
```
`db-g1-small` (or even the cheaper burstable `db-f1-micro`) is plenty — mailflow's tables
are tiny bookkeeping (cursor + claim rows), never email content. `--no-assign-ip` + `--network`
keeps it on a private IP inside your VPC (no public exposure); drop those two flags for a
public-IP instance if you're connecting from outside GCP (then add `--authorized-networks`
per §5 below, and always use the Auth Proxy or `sslmode=require`).

## 2. Create the database + a least-privilege runtime user (mirrors what we did locally)

```bash
gcloud sql databases create mailflow --instance=mailflow-pg
gcloud sql users create mailflow_app --instance=mailflow-pg --password=<APP_PASSWORD>
```
Run the *first* connection (which does `CREATE TABLE IF NOT EXISTS` for `cursors`/`claims`/
`dead_letters`) as a user with `CREATE` on the `mailflow` database — either the default
`postgres` admin user once, or grant `mailflow_app` schema-create rights up front:
```sql
GRANT ALL PRIVILEGES ON DATABASE mailflow TO mailflow_app;
```
(A stricter production posture: create the 3 tables once as an admin/migration user, then
revoke `CREATE` from `mailflow_app` so the running app can only `SELECT`/`INSERT`/`UPDATE`/
`DELETE` on those specific tables — not create new ones.)

## 3. Separate databases per environment

```bash
gcloud sql databases create mailflow_dev --instance=mailflow-pg
gcloud sql databases create mailflow_staging --instance=mailflow-pg
# prod: a separate instance is safer than a separate database on the same instance
gcloud sql instances create mailflow-pg-prod --database-version=POSTGRES_17 \
  --tier=db-g1-small --region=<REGION> --no-assign-ip --network=<VPC_NETWORK>
```
Same instinct as locally (dedicated `mailflow` db, never `sales_hawk_*`) — extend it so
dev/staging/prod never share a database, and prod gets its own instance for blast-radius
isolation and independent backup/maintenance windows.

## 4. Connect via the Cloud SQL Auth Proxy (recommended — zero code changes)

```bash
# download once: https://cloud.google.com/sql/docs/postgres/sql-proxy#install
./cloud-sql-proxy <PROJECT_ID>:<REGION>:mailflow-pg --port 5432
```
The proxy handles TLS + IAM auth and listens on `localhost:5432`. mailflow's `state=` DSN
then points at the proxy exactly like the local dev DB did:
```
POSTGRES_DSN=postgresql://mailflow_app:<APP_PASSWORD>@127.0.0.1:5432/mailflow
```
Run the proxy as a sidecar container (Cloud Run/GKE) or a background process (Compute
Engine/local). Alternative for a proxy-less setup: the `cloud-sql-python-connector` library
gives IAM-authenticated connections directly — not used here since it would require
`stores/postgres.py` to accept an injected connection factory instead of a raw DSN (noted
as a possible future enhancement, not needed for the Auth Proxy path).

## 5. Networking (only if not using a private IP + VPC connector)

```bash
gcloud sql instances patch mailflow-pg --authorized-networks=<YOUR_APP_EGRESS_IP>/32
```
Prefer private IP (step 1) + a
[Serverless VPC Access connector](https://cloud.google.com/sql/docs/postgres/connect-run)
if the app runs on Cloud Run — no public IP, no authorized-networks list to maintain.

## 6. Secrets

Store `POSTGRES_DSN` (or just the password) in **Secret Manager**, not a plaintext `.env`,
for staging/prod:
```bash
echo -n "<APP_PASSWORD>" | gcloud secrets create mailflow-pg-password --data-file=-
```
Cloud Run / GKE can inject a Secret Manager value directly as an env var at deploy time
(no code change needed) — e.g. Cloud Run: `--set-secrets=POSTGRES_DSN=mailflow-pg-dsn:latest`.
This is the same `env://`-style indirection mailflow already uses for
`GMAIL_CLIENT_SECRET`/`GRAPH_CLIENT_SECRET`, just resolved by the platform instead of
`mailflow.auth.load_env_file` for this particular secret.

## 7. Verify

Same shape as the local check we already ran:
```python
import os, psycopg
conn = psycopg.connect(os.environ["POSTGRES_DSN"], connect_timeout=5)
print("connected:", conn.info.dbname, "as", conn.info.user)
```
Then wire it into `connect()` exactly as documented in
[`mailflow-final-report.md`](mailflow-final-report.md) §5 — only `state=` changes; the rest
of the app code is identical to the SQLite/local path.

## Reference

- Store implementation + contract: `src/mailflow/stores/postgres.py`
- Live test suite (same cases, run against a real Postgres): `tests/test_postgres_stores.py`
  (`MAILFLOW_TEST_POSTGRES_DSN=<dsn> python -m pytest tests/test_postgres_stores.py -v`)
- `state=` resolution: `src/mailflow/config/state.py`
