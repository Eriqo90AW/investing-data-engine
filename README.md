# Company financials engine

Local FastAPI service with separate explicit extraction and read-only inference APIs. Inference reads only PostgreSQL and never starts background work. The initial service foundation is present; verified issuer/SEC extraction adapters and acceptance fixtures are not included yet, so extraction jobs currently fail explicitly instead of publishing fabricated data.

## Run locally

1. Install Docker Compose.
2. Copy `.env.example` to `.env`; set a nonempty `POSTGRES_PASSWORD` and set `SEC_CONTACT_EMAIL` to a monitored contact address before using the SEC. Use an alphanumeric database password because Compose embeds it in a connection URL.
3. Run `docker compose -f docker-compose.yml -f docker-compose.local.yml up --build`.
4. API docs are at http://127.0.0.1:9090/docs. The API is published on host port `9090`, mapped to container port `8000`.

Postgres data persists in named volumes. A one-shot migration service initializes the Procrastinate queue schema if absent and applies the app schema before the API and worker start. Initialization runs in one transaction under a database advisory lock, so failed initialization rolls back and repeated or concurrent deployments do not reinstall existing schemas. Procrastinate version upgrades still require its version-specific migrations. The API mounts documents read-only; only the worker may write documents.

## Deploy on Coolify

Create a Git-backed application from this repository with build pack **Docker Compose**, base directory `/`, and Compose location `/docker-compose.yml`. Load only that file; `docker-compose.local.yml` additionally publishes PostgreSQL on localhost for local development and must not be included in the Coolify deployment. Build and start commands are already defined in the Dockerfile and Compose services.

Set `POSTGRES_PASSWORD` to a long random alphanumeric value before the first deployment. Set `SEC_CONTACT_EMAIL` and optionally `NINEROUTER_API_KEY`. The Compose file supplies the internal database URL to the migration, API, and worker containers. The worker also receives `FINANCIALS_NINEROUTER_BASE_URL` and `FINANCIALS_NINEROUTER_MODEL` from Compose variables or their defaults. Changing the password variable after database initialization does not change the existing PostgreSQL role password; rotate that role and the variable together.

Assign a domain only to the `api` service, targeting internal port `8000`, for example `https://financials.example.com:8000` in Coolify's domain field. Point DNS to the deployment server. Public clients then use `https://financials.example.com` without `:8000`. The base Compose file publishes the API on host port `9090`, mapped to internal port `8000`; PostgreSQL has no host port mapping. A Cloudflare Tunnel running directly on the server can target `http://127.0.0.1:9090`. A tunnel in a separate Docker bridge network must target the server's reachable IP on port `9090`. The API has no built-in authentication; restrict access through your private network or an authenticated gateway before making it publicly reachable.

Expected startup order is PostgreSQL healthy, migration container exited with code 0, then API and worker running. The completed migration container is expected to stay stopped. The API health check uses Python's standard HTTP client against `/health`; no curl installation is required. Check `/health`, `/docs`, and `/openapi.json` after deploying. Health checks verify database connectivity, not worker readiness or external extraction.

Keep the `postgres-data` and `documents` named volumes across deployments and back them up. The API mounts documents read-only; the worker mounts the same volume read-write. PostgreSQL, API, and worker restart automatically unless explicitly stopped. Deploying the stack does not add extraction capabilities; jobs still fail with `extractor_unavailable` as described below.

## API

Open [the interactive API documentation](http://127.0.0.1:9090/docs) after starting the service. All seven endpoints are grouped by health, extraction, and inference. Expand an endpoint, click **Try it out**, edit its parameters or JSON body, and click **Execute**. The page shows a curl command and the actual response status, headers, and body. Results last for the current visit; requests and polling are manual. The page uses FastAPI's existing Swagger assets and needs no frontend build.

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
