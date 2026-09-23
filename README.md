# Arbitrage System

Public, sanitized source snapshot of the local arbitrage monitoring system.

## Scope

- Exchange announcements and local news service.
- Futures/spot spread and funding monitoring.
- Astro status, scan rules, DEX mappings and depth validation.
- DEX history, local service supervision and supporting tests.

Some shared research models and helpers remain for compatibility. Their presence
does not mean their historical interfaces or background jobs are enabled.
Retired manual-order gateways and Windows order entry are not included.

## Privacy Boundary

This repository starts with new Git history. It excludes production environment
files, credentials, account data, databases, trading records, logs, backups,
research documents, private deployment metadata and built bundles.

Personal hostnames, IP addresses and local home paths have been replaced with
reserved example values. Deployment scripts are reference templates and require
local configuration before use. No real server details should be committed.
The environment example lists keys without real configuration values.

This is a code backup, not a production data backup or ready-to-run deployment.
Do not point a test checkout at production storage or enable trading credentials.
Public visibility means every commit and future upload can be read by anyone.
Review every future diff for secrets and personal data before uploading. No
automated deployment workflow is included.

## Development

Python 3.12 and Node.js with npm are required. The local news service additionally
uses Node.js `node:sqlite` and requires a compatible recent Node.js version.

```sh
python3.12 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements-dev.txt
cd frontend
npm ci
npm run build
```

Configure local environment variables using `.env.example` as a key reference.
Use a disposable `STOCK_REVIEW_DATA_ROOT` and `STOCK_REVIEW_DATA_DIR` for tests.
Run backend and launcher tests separately to avoid duplicate test module names.

```sh
cd backend
PYTHONPATH=. .venv/bin/python -m pytest tests
cd ..
backend/.venv/bin/python -m pytest launcher/tests
```

The news worker and news UI have separate dependency manifests. The checked-in
worker configuration omits private cloud resource IDs; supply those locally.

## Upload Checks

Run Gitleaks against both the working tree and complete Git history before push.
Also inspect personal addresses, user paths, cloud account/resource identifiers,
test fixtures, binaries and commit author metadata. A clean scan is not an
absolute guarantee that no sensitive information exists.
