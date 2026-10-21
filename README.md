# Company financials engine

Local FastAPI service with separate explicit extraction and read-only inference APIs. Inference reads only PostgreSQL and never starts background work. The initial service foundation is present; verified issuer/SEC extraction adapters and acceptance fixtures are not included yet, so extraction jobs currently fail explicitly instead of publishing fabricated data.

## Run locally

1. Install Docker Compose and `uv`.
2. Copy `.env.example` to `.env`; set `SEC_CONTACT_EMAIL` to a monitored contact address before using the SEC.
3. Run `docker compose up --build`.
4. API docs are at http://127.0.0.1:8000/docs. The service binds only to localhost.

Postgres data persists in named volumes. A one-shot migration service installs the Procrastinate queue schema and the app schema before the API and worker start. The API mounts documents read-only; only the worker may write documents.

## API

Open [the interactive API documentation](http://127.0.0.1:8000/docs) after starting the service. All seven endpoints are grouped by health, extraction, and inference. Expand an endpoint, click **Try it out**, edit its parameters or JSON body, and click **Execute**. The page shows a curl command and the actual response status, headers, and body. Results last for the current visit; requests and polling are manual. The page uses FastAPI's existing Swagger assets and needs no frontend build.

Start with `/health`. For extraction on an empty database, use the example numeric CIK `1018724`; tickers such as `AMZN` require an existing company entry. Copy the returned `job_id` into the job endpoint to poll. The current worker reports `failed` with `extractor_unavailable`. Read financials and breakdowns after a dataset has been published; pin the same `version` for consistent reads. List reports to find IDs for file downloads.

Response examples are illustrative, not retrieved financial results. The **Server response** after Execute is live. Executing extraction can create a company entry and enqueue work. The generated contract is available at `/openapi.json`. Keep endpoint descriptions and examples current during development as required by `AGENTS.md`.

- `POST /v1/extraction/financials` accepts `{"identifier":"AMZN","periods":["2025-Q4"],"refresh":false}`. It returns 202 and a poll URL, reuses an active job, or returns 200 `already_fresh` for a fresh stored dataset.
- `GET /v1/extraction/jobs/{job_id}` reads progress and published references; polling never schedules work.
- `GET /v1/inference/companies/AMZN/financials?period=2025-Q4` and `/breakdowns` read the same published version. `?version=` pins a version; `If-None-Match` gets conditional reads and ETags.
- `GET /v1/inference/companies/AMZN/reports` and `/v1/inference/reports/{report_id}/download` expose stored report metadata and documents.
- `GET /health` checks only the local database.

Inference defaults to the latest period stored in the selected dataset. It reports explicit missing periods and never triggers extraction. Published versions remain readable after their stale threshold; `stale` is metadata.

## Environment

`SEC_CONTACT_EMAIL` is required before SEC retrieval. `NINEROUTER_API_KEY` is read by extraction workers only; `FINANCIALS_NINEROUTER_BASE_URL` defaults to `https://api.ercloud.site/v1`, model to `go-free`. Keep secrets in `.env`, which is gitignored. Never place secrets in API payloads or logs.

## Limitations

This bootstrap does not yet discover SEC filings, run Arelle/HTML/PDF/OCR/browser extraction, call 9router, implement atomic validated publication, or populate companies. Those must be delivered with fixture-backed verification before financial results are relied on. Browser downloads must use allow-listed source hosts and disable redirects or revalidate each redirect target. No recurring refresh or eviction is configured.

## Documentation checks

Install the project with test dependencies using `uv pip install -e ".[test]"`, then run `python -m pytest`. Documentation tests use an isolated application lifespan and simulated database responses; they do not require PostgreSQL or start extraction workers.
