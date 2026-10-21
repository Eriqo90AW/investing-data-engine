from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from .api import extraction, inference
from .db import lifespan_resources
from .workers import tasks  # noqa: F401 - register extraction task


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with lifespan_resources():
        yield


app = FastAPI(
    title="Company Financials Engine",
    version="0.1.0",
    lifespan=lifespan,
    description="""
Read endpoint descriptions, edit example requests with **Try it out**, and click
**Execute** to send a real request. Swagger displays the generated curl command,
request URL, response status, headers, and body. Results last for the current visit;
there is no saved request history or automatic polling.
Opening `GET /` redirects to this Swagger interface at `/docs`.

### Workflow

1. Check `GET /health` for local database connectivity.
2. Call `POST /v1/extraction/financials` with a stored ticker or numeric SEC CIK.
   A `202` response includes a `status_url`; `200 already_fresh` means stored data
   already covers the request.
3. Copy the job ID into `GET /v1/extraction/jobs/{job_id}` and execute again to poll.
4. After publication, read financials, breakdowns, or reports through inference.
   Inference only reads stored data and never starts extraction.

### Current limitation

The extraction worker has no verified source adapter. Accepted jobs currently end
with `status: failed` and `extractor_unavailable`. A new database has no tickers or
published financials; a numeric CIK can create a company entry for extraction.
Inference returns explicit missing-data errors until data has been published.

All documented response examples are **illustrative**, not retrieved financial
results. The **Server response** shown after Execute is the actual result.
Executing extraction can enqueue work or create a company entry.
""",
    openapi_tags=[
        {"name": "documentation", "description": "Open the interactive API documentation."},
        {"name": "health", "description": "Check the local database connection."},
        {"name": "extraction", "description": "Explicitly request work and read job progress."},
        {"name": "inference", "description": "Read published datasets and stored documents without scheduling work."},
    ],
    swagger_ui_parameters={"persistAuthorization": False},
)
app.include_router(extraction.router)
app.include_router(inference.router)


@app.get(
    "/",
    tags=["documentation"],
    summary="Open API documentation",
    description="Redirects the root URL to the Swagger interface at /docs. Does not query the database or start extraction.",
    response_class=RedirectResponse,
    status_code=307,
    responses={
        307: {
            "description": "Redirect to the interactive API documentation. Empty response body.",
            "headers": {
                "Location": {
                    "description": "Relative URL of the Swagger interface.",
                    "schema": {"type": "string"},
                    "example": "/docs",
                }
            },
        }
    },
)
async def root():
    return RedirectResponse(url="/docs", status_code=307)


@app.get(
    "/health",
    tags=["health"],
    summary="Check database connectivity",
    description="Runs SELECT 1 against the local PostgreSQL database. Does not check external providers or worker readiness.",
    responses={200: {"description": "Database reachable. Illustrative response.", "content": {"application/json": {"example": {"status": "ok"}}}}},
)
async def health():
    from .db import get_pool
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "ok"}
