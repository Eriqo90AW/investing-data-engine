# Company financials engine

Local FastAPI service with separate explicit extraction and read-only inference APIs. Inference reads only PostgreSQL and never starts background work. Explicit worker jobs retrieve SEC US GAAP financials, source filings, and Yahoo daily history. Inference remains read-only. Unsupported disclosures and ambiguous document tables are reported as coverage gaps.

## Run locally

1. Install Docker Compose.
2. Copy `.env.example` to `.env`; set a nonempty `POSTGRES_PASSWORD` and set `SEC_CONTACT_EMAIL` to a monitored contact address before using the SEC. Use an alphanumeric database password because Compose embeds it in a connection URL.
3. Run `docker compose -f docker-compose.yml -f docker-compose.local.yml up --build`.
4. Open http://127.0.0.1:9090/ to be redirected to the API docs at `/docs`. The API is published on host port `9090`, mapped to container port `8000`.

Postgres data persists in named volumes. A one-shot migration service initializes the Procrastinate queue schema if absent and applies the app schema before the API and worker start. Initialization runs in one transaction under a database advisory lock, so failed initialization rolls back and repeated or concurrent deployments do not reinstall existing schemas. Procrastinate version upgrades still require its version-specific migrations. The API mounts documents read-only; only the worker may write documents.

## Deploy on Coolify

Create a Git-backed application from this repository with build pack **Docker Compose**, base directory `/`, and Compose location `/docker-compose.yml`. Load only that file; `docker-compose.local.yml` additionally publishes PostgreSQL on localhost for local development and must not be included in the Coolify deployment. Build and start commands are already defined in the Dockerfile and Compose services.

Set `POSTGRES_PASSWORD` to a long random alphanumeric value before the first deployment. Set `SEC_CONTACT_EMAIL` and optionally `NINEROUTER_API_KEY`. The Compose file supplies the internal database URL to the migration, API, and worker containers. The worker also receives `FINANCIALS_NINEROUTER_BASE_URL` and `FINANCIALS_NINEROUTER_MODEL` from Compose variables or their defaults. Changing the password variable after database initialization does not change the existing PostgreSQL role password; rotate that role and the variable together.

Assign a domain only to the `api` service, targeting internal port `8000`, for example `https://financials.example.com:8000` in Coolify's domain field. Point DNS to the deployment server. Public clients then use `https://financials.example.com` without `:8000`. The base Compose file publishes the API on host port `9090`, mapped to internal port `8000`; PostgreSQL has no host port mapping. A Cloudflare Tunnel running directly on the server can target `http://127.0.0.1:9090`. A tunnel in a separate Docker bridge network must target the server's reachable IP on port `9090`. The API has no built-in authentication; restrict access through your private network or an authenticated gateway before making it publicly reachable.

Expected startup order is PostgreSQL healthy, migration container exited with code 0, then API and worker running. The completed migration container is expected to stay stopped. The API health check uses Python's standard HTTP client against `/health`; no curl installation is required. Check `/health`, `/docs`, and `/openapi.json` after deploying. Health checks verify database connectivity, not worker readiness or external extraction.

Keep the `postgres-data` and `documents` named volumes across deployments and back them up. The API mounts documents read-only; the worker mounts the same volume read-write. PostgreSQL, API, and worker restart automatically unless explicitly stopped. The worker image includes extraction dependencies, Chromium, and Tesseract. Set SEC_CONTACT_EMAIL before source retrieval.

## API

Open [the interactive API documentation](http://127.0.0.1:9090/docs) after starting the service. Opening `GET /` returns a `307` redirect to `/docs`. The redirect is documented under documentation; the data and health endpoints are grouped by health, extraction, and inference. Expand an endpoint, click **Try it out**, edit its parameters or JSON body, and click **Execute**. The page shows a curl command and the actual response status, headers, and body. Results last for the current visit; requests and polling are manual. The page uses FastAPI's existing Swagger assets and needs no frontend build.

Start with `/health`. For extraction on an empty database, use the example numeric CIK `1018724`; tickers such as `AMZN` require an existing company entry. Copy the returned `job_id` into the job endpoint to poll. The worker reports progress, then publishes a complete or partial dataset, or returns a source/configuration error without replacing existing data. Read financials and breakdowns after a dataset has been published; pin the same `version` for consistent reads. List reports to find IDs for file downloads.

Response examples are illustrative, not retrieved financial results. The **Server response** after Execute is live. Executing extraction can create a company entry and enqueue work. The generated contract is available at `/openapi.json`. Keep endpoint descriptions and examples current during development as required by `AGENTS.md`.

- `POST /v1/extraction/financials` accepts `{"identifier":"AMZN","periods":["2025-Q4"],"refresh":false}`. It returns 202 and a poll URL, reuses an active job, or returns 200 `already_fresh` for a fresh stored dataset.
- `GET /v1/extraction/jobs/{job_id}` reads progress and published references; polling never schedules work.
- `GET /v1/inference/companies/AMZN/financials?period=2025-Q4` and `/breakdowns` read the same published version. `?version=` pins a version; `If-None-Match` gets conditional reads and ETags.
- `GET /v1/inference/companies/AMZN/reports` and `/v1/inference/reports/{report_id}/download` expose stored report metadata and documents.
- `GET /health` checks only the local database.

Inference defaults to the latest period stored in the selected dataset. It reports explicit missing periods and never triggers extraction. Published versions remain readable after their stale threshold; `stale` is metadata.

## Environment

`SEC_CONTACT_EMAIL` is required before SEC retrieval. `NINEROUTER_API_KEY` is read by extraction workers only; `FINANCIALS_NINEROUTER_BASE_URL` defaults to `https://api.ercloud.site/v1`, model to `go-free`. Keep secrets in `.env`, which is gitignored. Never place secrets in API payloads or logs.

## Research workflow

- `POST /v1/extraction/financials` now resolves new tickers through the SEC directory during explicit retrieval. The optional `lookback: {"annual": 5, "quarterly": 12}` selects recent completed fiscal periods and cannot be combined with explicit `periods`.
- `GET /v1/inference/companies?query=AMZN` searches stored company identities only.
- `POST /v1/extraction/market-data` accepts `{"identifier":"AMZN","refresh":false}`. It requests five years of daily Yahoo history through yfinance. Use only for personal research; Yahoo/yfinance availability and rate limits can interrupt retrieval.
- `GET /v1/inference/companies/AMZN/market-data` reads the stored snapshot, supports inclusive `start` and `end`, exact `version`, and conditional ETags.
- Financials and breakdowns return `available_periods` for their publication. Request the same version across both endpoints. Price versions are independent.
- Jobs expose `kind`, `published_datasets`, and `published_market_data`. Check progress manually. No GET triggers discovery or extraction.

Financial statements preserve source units and exact reporting dates. Monetary YTD flows can produce standalone quarters only through explicit compatible differences with both input sources attached. EPS is never subtracted. Source evidence includes accession, filing location, parser, document hash and stored report ID when available.

Source documents are retrieved over bounded, identified HTTPS requests. Inline XBRL preserves disclosed segment dimensions; HTML and PDF adapters require explicit unit, scale, period and metric labels. OCR candidates require independent corroboration. Browser rendering is restricted to verified hosts and is never used to bypass source access blocks. Issuer fallbacks are configured for AMZN, MSFT, NVDA, JPM and WMT. Unsupported layouts stay unavailable.

## Limitations

This release supports the mapped domestic US GAAP concepts in 10-K/10-Q filings. It does not claim universal issuer, IFRS, foreign-filer, PDF or OCR coverage. Ambiguous custom concepts and combined dimension contexts are excluded. Document fallback hosts are explicitly listed; adding a new issuer requires tests and host review.

No recurring refresh, automatic polling, live quote streaming, trading, eviction or paid data service is configured. 9router settings remain optional configuration and are not used to manufacture financial values. Existing datasets remain readable when stale or after a failed refresh.

API health checks test PostgreSQL connectivity only, not provider availability, worker readiness, browser installation or OCR installation. The test suite uses illustrative fixtures and mocked database connections. Recorded acceptance fixtures cover real fiscal-2025 SEC totals for AMZN, MSFT, NVDA, JPM and WMT. Deployment acceptance must additionally run migrations against PostgreSQL and verify the deployed worker. Native OCR tests can use FINANCIALS_OCR_TESSDATA_DIR pointing to installed Tesseract English language data; the container installs Tesseract itself.

## Documentation checks

Install the project with test dependencies using `uv pip install -e ".[test]"`, then run `python -m pytest`. Documentation tests use an isolated application lifespan and simulated database responses; they do not require PostgreSQL or start extraction workers.
