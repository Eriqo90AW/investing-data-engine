from fastapi import APIRouter, Depends, HTTPException, Path, Response
import asyncpg
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import json
from ..services.sources import discover, SourceError

from ..config import get_settings
from ..db import get_pool
from ..schemas import ExtractionAccepted, ExtractionRequest, JobResponse, MarketExtractionRequest
from ..workers.tasks import extract_company_financials

router = APIRouter(prefix="/v1/extraction", tags=["extraction"])


@router.post(
    "/financials", response_model=ExtractionAccepted, status_code=202,
    summary="Request financial extraction",
    description="""Explicitly enqueue extraction, reuse an identical active job, or return fresh stored dataset versions.
Copy the returned job_id into the polling endpoint. A 202 means accepted, not published.
The worker retrieves SEC company facts and source reports. Partial publication includes explicit coverage gaps; source errors appear on the job.
Examples are illustrative. New tickers can be resolved through the SEC directory during this explicit request.""",
    responses={
        202: {"description": "Job queued or active job reused. Illustrative response.", "content": {"application/json": {"example": {
            "status": "accepted", "job_id": "1", "status_url": "/v1/extraction/jobs/1", "dataset_versions": [],
        }}}},
        200: {"model": ExtractionAccepted, "description": "Fresh stored dataset covers the requested periods. Illustrative response.", "content": {"application/json": {"example": {
            "status": "already_fresh", "job_id": None, "status_url": None, "dataset_versions": ["illustrative-v1"],
        }}}},
        503: {"description": "SEC directory unavailable or SEC contact configuration missing."},
        404: {"description": "Unknown ticker or unsupported new CIK. Illustrative response.", "content": {"application/json": {"example": {
            "detail": {"code": "company_not_found", "identifier": "AMZN"},
        }}}},
        422: {"description": "Invalid body, extra fields, duplicate periods, invalid period format, or latest mixed with other periods. Illustrative response.", "content": {"application/json": {"example": {
            "detail": [{"type": "missing", "loc": ["body", "identifier"], "msg": "Field required", "input": {}}],
        }}}},
    },
)
async def request_financials(request: ExtractionRequest, response: Response, pool: asyncpg.Pool = Depends(get_pool)):
    return await enqueue(request, response, "financials")


@router.post("/market-data", response_model=ExtractionAccepted, status_code=202, summary="Request daily price retrieval",
    description="Explicitly enqueue five years of Yahoo daily history, reuse an identical active job, or return a fresh snapshot. No paid key is required. Source and response-schema validation failures appear on the job without replacing prior publications. Snapshots are validated against the read response before publication. Examples are illustrative.",
    responses={202: {"description": "Daily retrieval queued.", "content": {"application/json": {"example": {"status": "accepted", "job_id": "2", "status_url": "/v1/extraction/jobs/2", "dataset_versions": []}}}},
               200: {"description": "Stored snapshot remains fresh.", "content": {"application/json": {"example": {"status": "already_fresh", "job_id": None, "status_url": None, "dataset_versions": ["illustrative-price-v1"]}}}}, 404: {"description": "Company could not be resolved."}, 503: {"description": "SEC directory unavailable or contact configuration missing."}})
async def request_market(request: MarketExtractionRequest, response: Response):
    return await enqueue(ExtractionRequest(identifier=request.identifier, refresh=request.refresh), response, "market-data")


async def enqueue(request: ExtractionRequest, response: Response, kind: str):
    identifier_cik = request.identifier.lstrip("0").zfill(10) if request.identifier.isdigit() else ""
    requested_periods = request.periods or ["latest"]
    options = {"lookback": request.lookback.model_dump()} if request.lookback else {}

    # Use one psycopg transaction for the domain job and Procrastinate queue insert.
    # This prevents orphaned queued requests if the API process exits between writes.
    async with await psycopg.AsyncConnection.connect(
        get_settings().database_url, row_factory=dict_row
    ) as conn:
        async with conn.transaction():
            company = await (await conn.execute(
                "SELECT id, ticker FROM companies WHERE lower(ticker)=lower(%s) OR cik=%s",
                (request.identifier, identifier_cik),
            )).fetchone()
            if company is None and request.identifier.isdigit() and 1 <= len(request.identifier) <= 10:
                company = await (await conn.execute(
                    """INSERT INTO companies(cik, ticker, name) VALUES (%s, NULL, %s)
                       ON CONFLICT (cik) DO UPDATE SET cik=EXCLUDED.cik
                       RETURNING id, ticker""",
                    (identifier_cik, f"CIK {identifier_cik}"),
                )).fetchone()
            if company is None:
                try:
                    discovered = await discover(request.identifier)
                except SourceError as exc:
                    raise HTTPException(404 if exc.code == "company_not_found" else 503, detail={"code": exc.code, "message": str(exc)}) from exc
                except Exception as exc:
                    raise HTTPException(503, detail={"code": "source_unavailable", "message": "SEC directory could not be reached."}) from exc
                company = await (await conn.execute(
                    """INSERT INTO companies(cik,ticker,name) VALUES(%s,%s,%s)
                       ON CONFLICT(cik) DO UPDATE SET ticker=coalesce(companies.ticker,EXCLUDED.ticker),name=EXCLUDED.name RETURNING id,ticker""",
                    (discovered["cik"], discovered["ticker"], discovered["name"]))).fetchone()

            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (company["id"],))
            active = await (await conn.execute(
                """SELECT id FROM extraction_jobs
                   WHERE company_id=%s AND status IN ('queued','running')
                     AND requested_periods=%s AND refresh=%s AND kind=%s AND options=%s
                   ORDER BY created_at DESC LIMIT 1""",
                (company["id"], requested_periods, request.refresh, kind, Jsonb(options)),
            )).fetchone()
            if active:
                response.status_code = 202
                return ExtractionAccepted(status="accepted", job_id=str(active["id"]), status_url=f"/v1/extraction/jobs/{active['id']}")

            if not request.refresh and kind == "market-data":
                fresh_market = await (await conn.execute("SELECT dataset_version FROM market_snapshots WHERE company_id=%s AND stale_after>now() ORDER BY published_at DESC LIMIT 1", (company["id"],))).fetchone()
                if fresh_market:
                    response.status_code = 200
                    return ExtractionAccepted(status="already_fresh", dataset_versions=[fresh_market["dataset_version"]])
            if not request.refresh and kind == "financials":
                fresh = await (await conn.execute(
                    """SELECT dataset_version, financials_payload FROM published_datasets
                       WHERE company_id=%s AND status='published' AND stale_after > now()
                       ORDER BY published_at DESC LIMIT 1""",
                    (company["id"],),
                )).fetchone()
                if fresh:
                    payload = fresh["financials_payload"]
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                    available = {p.get("period") for p in payload.get("periods", [])}
                    requested_available = (
                        bool(available) if requested_periods == ["latest"]
                        else set(requested_periods).issubset(available)
                    )
                    if request.lookback:
                        rows = payload.get("periods", [])
                        requested_available = (sum(p.get("period_kind") == "annual" for p in rows) >= request.lookback.annual
                            and sum(p.get("period_kind") == "quarter" for p in rows) >= request.lookback.quarterly)
                    if requested_available:
                        response.status_code = 200
                        return ExtractionAccepted(status="already_fresh", dataset_versions=[fresh["dataset_version"]])

            job = await (await conn.execute(
                """INSERT INTO extraction_jobs(company_id, requested_periods, refresh, kind, options, status)
                   VALUES (%s,%s,%s,%s,%s,'queued') RETURNING id""",
                (company["id"], requested_periods, request.refresh, kind, Jsonb(options)),
            )).fetchone()
            await extract_company_financials.configure(connection=conn).defer_async(job_id=job["id"])

    response.status_code = 202
    return ExtractionAccepted(status="accepted", job_id=str(job["id"]), status_url=f"/v1/extraction/jobs/{job['id']}")


@router.get(
    "/jobs/{job_id}", response_model=JobResponse,
    summary="Read extraction job progress",
    description="Read status, progress, errors, and published dataset references. Polling never schedules work. Execute manually to poll again. Examples are illustrative; failed jobs retain previous publications.",
    responses={
        200: {"description": "Current job state. Illustrative responses.", "content": {"application/json": {"examples": {
            "queued": {"summary": "Queued job", "value": {"job_id": "1", "status": "queued", "progress": {}, "errors": [], "published_datasets": []}},
            "failed": {"summary": "Source configuration missing", "value": {
                "job_id": "1", "status": "failed", "progress": {"stage": "failed", "completed": 0},
                "errors": [{"code": "sec_contact_required", "message": "Configure SEC_CONTACT_EMAIL before retrieving filings."}], "published_datasets": [],
            }},
            "published": {"summary": "Illustrative future publication", "value": {
                "job_id": "1", "status": "published", "progress": {}, "errors": [],
                "published_datasets": [{"dataset_version": "illustrative-v1", "published_at": "2026-01-15T00:00:00Z"}],
            }},
        }}}},
        404: {"description": "Job not found. Illustrative response.", "content": {"application/json": {"example": {"detail": {"code": "job_not_found"}}}}},
    },
)
async def get_job(job_id: int = Path(description="Numeric job ID returned by extraction.", examples=[1]), pool: asyncpg.Pool = Depends(get_pool)):
    async with pool.acquire() as conn:
        job = await conn.fetchrow("SELECT id,status,progress,errors,kind FROM extraction_jobs WHERE id=$1", job_id)
        if job is None:
            raise HTTPException(404, detail={"code": "job_not_found"})
        datasets = await conn.fetch("SELECT dataset_version, published_at FROM published_datasets WHERE extraction_job_id=$1 AND status='published' ORDER BY published_at", job_id)
    progress = json.loads(job["progress"]) if isinstance(job["progress"], str) else job["progress"]
    errors = json.loads(job["errors"]) if isinstance(job["errors"], str) else job["errors"]
    return {"published_market_data": [dict(r) for r in await _market_references(pool, job_id)], "kind": job.get("kind", "financials"), "job_id": str(job["id"]), "status": job["status"], "progress": progress or {}, "errors": errors or [], "published_datasets": [dict(r) for r in datasets]}



async def _market_references(pool, job_id):
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT dataset_version,published_at FROM market_snapshots WHERE extraction_job_id=$1 ORDER BY published_at", job_id)
