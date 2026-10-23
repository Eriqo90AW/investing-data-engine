from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Path as ApiPath, Query, Response
from fastapi.responses import FileResponse
import asyncpg

from ..config import get_settings
from ..db import get_pool
from ..schemas import FinancialsResponse, BreakdownsResponse

router = APIRouter(prefix="/v1/inference", tags=["inference"])

# Documentation metadata only. Examples do not seed or publish datasets.
COMPANY_EXAMPLE = {"ticker": "AMZN", "cik": "0001018724", "name": "Amazon.com, Inc."}
DATASET_EXAMPLE = {
    "company": COMPANY_EXAMPLE, "dataset_version": "illustrative-v1",
    "source_checked_at": "2026-01-15T00:00:00Z", "published_at": "2026-01-15T00:00:00Z",
    "stale": True, "periods": [], "missing_periods": ["2025-Q4"],
}
CACHE_HEADERS = {
    "ETag": {"description": "Opaque validator. Copy the complete value, including quotes, to If-None-Match for the same URL.", "schema": {"type": "string"}},
    "Cache-Control": {"description": "Always no-cache; revalidate before reuse.", "schema": {"type": "string", "example": "no-cache"}},
}
MISSING_DATA_RESPONSE = {
    "description": "Company or published dataset not found. No extraction is started. Illustrative responses.",
    "content": {"application/json": {"examples": {
        "company": {"summary": "Company not stored", "value": {"detail": {
            "code": "data_not_available", "identifier": "AMZN", "required_operation": "POST /v1/extraction/financials",
        }}},
        "dataset": {"summary": "Dataset or version not published", "value": {"detail": {
            "code": "data_not_available", "required_operation": "POST /v1/extraction/financials",
        }}},
    }}},
}
PERIOD_ERROR_RESPONSE = {
    "description": "Invalid query/path types or period selection. Illustrative period-selection errors; type errors use FastAPI's detail array.",
    "content": {"application/json": {"examples": {
        "format": {"summary": "Invalid period format", "value": {"detail": {"code": "invalid_period_format", "accepted": ["YYYY", "YYYY-QN", "latest"]}}},
        "selection": {"summary": "Duplicate periods or more than 20", "value": {"detail": {"code": "invalid_period_selection", "maximum": 20}}},
        "latest": {"summary": "Latest mixed with other periods", "value": {"detail": {"code": "latest_must_be_requested_alone"}}},
    }}},
}
PERIOD_DESCRIPTION = "Repeat period for multiple values, e.g. ?period=2025-Q3&period=2025-Q4. Up to 20 unique values: YYYY, YYYY-Q1 through YYYY-Q4, or latest alone; years 1900 through 2099. Omit for the latest stored period. Unavailable requested periods appear in missing_periods."
READ_DESCRIPTION = """Read only the selected published dataset. Never schedules extraction or calls external sources.
Omit version to select the most recently published dataset, or pin its dataset_version.
Use the same version for financials and breakdowns to read a consistent publication.
Stale datasets remain readable; stale is metadata, not an error or a refresh trigger.
Pass the previous ETag as If-None-Match for the same selection to receive 304 with no body.
Examples are illustrative and do not imply that this database contains financial results."""


def _etag(version: str, selection: list[str], kind: str) -> str:
    material = json.dumps([kind, version, selection], separators=(",", ":"))
    return '"' + sha256(material.encode()).hexdigest() + '"'


async def _resolve(conn: asyncpg.Connection, identifier: str):
    row = await conn.fetchrow(
        "SELECT id, cik, ticker, name FROM companies WHERE lower(ticker)=lower($1) OR cik=$2",
        identifier, identifier.lstrip("0").zfill(10) if identifier.isdigit() else "",
    )
    if row is None:
        raise HTTPException(404, detail={"code": "data_not_available", "identifier": identifier, "required_operation": "POST /v1/extraction/financials"})
    return dict(row)


async def _published(conn: asyncpg.Connection, company_id: int, version: str | None):
    row = await conn.fetchrow(
        """SELECT dataset_version, source_checked_at, published_at, stale_after,
                  financials_payload, breakdowns_payload
           FROM published_datasets
           WHERE company_id=$1 AND status='published'
             AND ($2::text IS NULL OR dataset_version=$2)
           ORDER BY published_at DESC LIMIT 1""", company_id, version,
    )
    if row is None:
        raise HTTPException(404, detail={"code": "data_not_available", "required_operation": "POST /v1/extraction/financials"})
    return row


def _select(payload: dict, periods: list[str]) -> tuple[list[dict], list[str]]:
    rows = payload.get("periods", [])
    if not periods or "latest" in periods:
        available = sorted(rows, key=lambda x: x.get("period_end", ""))
        return (available[-1:] if available else []), ([] if available else ["latest"])
    indexed = {x.get("period"): x for x in rows}
    return [indexed[p] for p in periods if p in indexed], [p for p in periods if p not in indexed]


def _validate_periods(periods: list[str]) -> None:
    if len(periods) > 20 or len(set(periods)) != len(periods):
        raise HTTPException(422, detail={"code": "invalid_period_selection", "maximum": 20})
    if "latest" in periods and len(periods) > 1:
        raise HTTPException(422, detail={"code": "latest_must_be_requested_alone"})
    if any(period != "latest" and not re.fullmatch(r"(?:20|19)\d{2}(?:-Q[1-4])?", period) for period in periods):
        raise HTTPException(422, detail={"code": "invalid_period_format", "accepted": ["YYYY", "YYYY-QN", "latest"]})


def _payload(value):
    return json.loads(value) if isinstance(value, str) else value


@router.get(
    "/companies/{identifier}/financials", response_model=FinancialsResponse, response_model_exclude_unset=True, summary="Read published financials",
    description=READ_DESCRIPTION + "\nlatest_stored_period is the greatest stored period_end date, not a YYYY-QN label. available_periods lists selectable periods for this publication. Each period has named income, balance and cash_flow statement arrays with canonical concepts and evidence. completeness is stored coverage metadata.",
    responses={
        200: {"description": "Published financials; requested periods may be missing. Illustrative response.", "headers": CACHE_HEADERS,
              "content": {"application/json": {"example": {**DATASET_EXAMPLE, "completeness": {}, "latest_stored_period": "2025-09-30"}}}},
        304: {"description": "ETag matches. Empty response body.", "headers": CACHE_HEADERS},
        404: MISSING_DATA_RESPONSE, 422: PERIOD_ERROR_RESPONSE,
    },
)
async def financials(response: Response,
                     identifier: str = ApiPath(description="Stored ticker, case-insensitive, or numeric SEC CIK.", examples=["AMZN"]),
                     period: list[str] = Query(default=[], description=PERIOD_DESCRIPTION, examples=[["2025-Q4"]]),
                     version: str | None = Query(default=None, description="Exact dataset_version returned by a previous read. Omit for the newest publication."),
                     if_none_match: str | None = Header(default=None, description="Copy the quoted ETag from the previous response for this URL. A match returns 304 without JSON."),
                     pool: asyncpg.Pool = Depends(get_pool)):
    _validate_periods(period)
    async with pool.acquire() as conn:
        company = await _resolve(conn, identifier)
        row = await _published(conn, company["id"], version)
    payload = _payload(row["financials_payload"])
    selected, missing = _select(payload, period)
    stale = row["stale_after"] < datetime.now(timezone.utc)
    tag = _etag(row["dataset_version"], period, "financials:stale" if stale else "financials:fresh")
    if if_none_match == tag:
        return Response(status_code=304, headers={"ETag": tag, "Cache-Control": "no-cache"})
    response.headers["ETag"] = tag
    response.headers["Cache-Control"] = "no-cache"
    return {"company": {k: company[k] for k in ("ticker", "cik", "name")}, "dataset_version": row["dataset_version"],
            "source_checked_at": row["source_checked_at"], "published_at": row["published_at"], "stale": stale,
            "available_periods": _available(payload), "completeness": payload.get("completeness", {}), "periods": selected, "missing_periods": missing,
            "latest_stored_period": max((p.get("period_end", "") for p in payload.get("periods", [])), default=None)}


@router.get(
    "/companies/{identifier}/breakdowns", response_model=BreakdownsResponse, response_model_exclude_unset=True, summary="Read published revenue and profit breakdowns",
    description=READ_DESCRIPTION + "\nReturns stored segment, product/service, or geographic breakdowns when available; no missing profit is inferred.",
    responses={
        200: {"description": "Published breakdowns; requested periods may be missing. Illustrative response.", "headers": CACHE_HEADERS,
              "content": {"application/json": {"example": DATASET_EXAMPLE}}},
        304: {"description": "ETag matches. Empty response body.", "headers": CACHE_HEADERS},
        404: MISSING_DATA_RESPONSE, 422: PERIOD_ERROR_RESPONSE,
    },
)
async def breakdowns(response: Response,
                     identifier: str = ApiPath(description="Stored ticker, case-insensitive, or numeric SEC CIK.", examples=["AMZN"]),
                     period: list[str] = Query(default=[], description=PERIOD_DESCRIPTION, examples=[["2025-Q4"]]),
                     version: str | None = Query(default=None, description="Exact dataset_version. Pin the same version as financials for a consistent publication."),
                     if_none_match: str | None = Header(default=None, description="Copy the quoted ETag from the previous response for this URL. A match returns 304 without JSON."),
                     pool: asyncpg.Pool = Depends(get_pool)):
    _validate_periods(period)
    async with pool.acquire() as conn:
        company = await _resolve(conn, identifier)
        row = await _published(conn, company["id"], version)
    payload = _payload(row["breakdowns_payload"])
    selected, missing = _select(payload, period)
    stale = row["stale_after"] < datetime.now(timezone.utc)
    tag = _etag(row["dataset_version"], period, "breakdowns:stale" if stale else "breakdowns:fresh")
    if if_none_match == tag:
        return Response(status_code=304, headers={"ETag": tag, "Cache-Control": "no-cache"})
    response.headers["ETag"] = tag
    response.headers["Cache-Control"] = "no-cache"
    return {"company": {k: company[k] for k in ("ticker", "cik", "name")}, "dataset_version": row["dataset_version"],
            "source_checked_at": row["source_checked_at"], "published_at": row["published_at"],
            "available_periods": _available(payload), "stale": stale, "periods": selected, "missing_periods": missing}


@router.get(
    "/companies/{identifier}/reports", summary="List stored company reports",
    description="Read report metadata ordered by filed_at, newest first. A stored company with no reports returns an empty reports array. Copy a report id into the download endpoint. Never fetches or extracts documents. Examples are illustrative.",
    responses={
        200: {"description": "Stored report metadata. Illustrative response.", "content": {"application/json": {"example": {
            "company": COMPANY_EXAMPLE, "reports": [{"id": 1, "form": "10-Q", "filed_at": "2025-10-31", "period_end": "2025-09-30",
                "source_url": "https://www.sec.gov/Archives/", "document_sha256": "0" * 64}],
        }}}},
        404: {"description": "Company not stored. Illustrative response.", "content": {"application/json": {"example": {
            "detail": {"code": "data_not_available", "identifier": "AMZN", "required_operation": "POST /v1/extraction/financials"},
        }}}},
    },
)
async def reports(identifier: str = ApiPath(description="Stored ticker, case-insensitive, or numeric SEC CIK.", examples=["AMZN"]), pool: asyncpg.Pool = Depends(get_pool)):
    async with pool.acquire() as conn:
        company = await _resolve(conn, identifier)
        rows = await conn.fetch("SELECT id, form, filed_at, period_end, source_url, document_sha256 FROM reports WHERE company_id=$1 ORDER BY filed_at DESC", company["id"])
    return {"company": {k: company[k] for k in ("ticker", "cik", "name")}, "reports": [dict(r) for r in rows]}


@router.get(
    "/reports/{report_id}/download", summary="Download a stored report", response_class=FileResponse,
    description="Return the stored file as an attachment using its original filename and stored media type, such as application/pdf or text/html. The body is file bytes, not JSON. Swagger may offer a Download file link. Missing metadata or an unavailable file returns JSON with 404. Never retrieves a remote document.",
    responses={
        200: {"description": "Stored document bytes. Illustrative media types; actual Content-Type comes from report metadata.",
              "headers": {"Content-Disposition": {"description": "Attachment with the original filename. Illustrative example.", "schema": {"type": "string", "example": 'attachment; filename="report.pdf"'}}},
              "content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}},
                          "text/html": {"schema": {"type": "string", "format": "binary"}}}},
        404: {"description": "Missing report metadata or unavailable stored file. Illustrative responses.", "content": {"application/json": {"examples": {
            "report": {"value": {"detail": {"code": "data_not_available"}}},
            "file": {"value": {"detail": {"code": "document_not_available"}}},
        }}}},
    },
)
async def download_report(report_id: int = ApiPath(description="Numeric id from the company reports endpoint.", examples=[1]), pool: asyncpg.Pool = Depends(get_pool)):
    async with pool.acquire() as conn:
        report = await conn.fetchrow("SELECT storage_path, media_type, original_filename FROM reports WHERE id=$1", report_id)
    if report is None:
        raise HTTPException(404, detail={"code": "data_not_available"})
    root = get_settings().document_dir.resolve()
    path = Path(report["storage_path"]).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(404, detail={"code": "document_not_available"})
    return FileResponse(path, media_type=report["media_type"], filename=report["original_filename"])








def _available(payload):
    return [{k: p[k] for k in ("period", "period_end", "period_start", "period_kind") if k in p} for p in payload.get("periods", [])]
