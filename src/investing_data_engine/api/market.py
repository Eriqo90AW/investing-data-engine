from datetime import date, datetime, timezone
import json
import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, Path
from ..db import get_pool
from .inference import _resolve, _etag, _payload, CACHE_HEADERS, MISSING_DATA_RESPONSE, READ_DESCRIPTION
from ..schemas import MarketResponse

router = APIRouter(prefix="/v1/inference", tags=["inference"])


@router.get("/companies", summary="Search stored companies",
    description="Search the local company directory only. Does not discover companies, call sources, or start extraction. Illustrative response.",
    responses={200: {"description": "Matching stored companies.", "content": {"application/json": {"example": {"companies": []}}}}})
async def companies(query: str = Query(default="", max_length=80, description="Ticker, CIK, or part of a company name. Empty query returns up to 20 stored companies."),
                    pool: asyncpg.Pool = Depends(get_pool)):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""SELECT ticker,cik,name FROM companies
            WHERE ticker ILIKE $1 OR name ILIKE $1 OR cik LIKE $1 ORDER BY ticker NULLS LAST LIMIT 20""", "%" + query + "%")
    return {"companies": [dict(r) for r in rows]}


@router.get("/companies/{identifier}/market-data", response_model=MarketResponse,
    summary="Read stored daily price history",
    description=READ_DESCRIPTION + "\nYahoo daily OHLC with explicit adjustment metadata, corporate actions, source timezone, and coverage. The optional root ticker preserves the provider identifier stored by the worker. No live quote feed. Date bounds are inclusive.",
    responses={200: {"description": "Stored daily bars. Illustrative empty bar selection.", "headers": CACHE_HEADERS,
                     "content": {"application/json": {"example": {"company": {"ticker": "AMZN", "cik": "0001018724", "name": "Amazon"},
                     "dataset_version": "illustrative-v1", "source_checked_at": "2026-01-15T00:00:00Z", "published_at": "2026-01-15T00:00:00Z",
                     "stale": True, "provider": "Yahoo Finance via yfinance", "currency": "USD", "timezone": "America/New_York",
                     "adjustment": "auto_adjust=false", "coverage_start": "2025-01-01", "coverage_end": "2025-12-31", "bars": []}}}},
               304: {"description": "ETag matches. Empty response body.", "headers": CACHE_HEADERS},
               404: MISSING_DATA_RESPONSE, 422: {"description": "Invalid dates or reversed date bounds."}})
async def market_data(response: Response, identifier: str = Path(description="Stored ticker or numeric SEC CIK."),
                      start: date | None = Query(default=None, description="Inclusive first daily-bar date."),
                      end: date | None = Query(default=None, description="Inclusive last daily-bar date."),
                      version: str | None = Query(default=None, description="Exact price snapshot version; omit for latest."),
                      if_none_match: str | None = Header(default=None, description="ETag for the same version and date selection."),
                      pool: asyncpg.Pool = Depends(get_pool)):
    if start and end and start > end:
        raise HTTPException(422, detail={"code": "invalid_date_range"})
    async with pool.acquire() as conn:
        company = await _resolve(conn, identifier)
        row = await conn.fetchrow("""SELECT * FROM market_snapshots WHERE company_id=$1 AND ($2::text IS NULL OR dataset_version=$2)
            ORDER BY published_at DESC LIMIT 1""", company["id"], version)
    if row is None:
        raise HTTPException(404, detail={"code": "data_not_available", "required_operation": "POST /v1/extraction/market-data"})
    stale = row["stale_after"] < datetime.now(timezone.utc)
    tag = _etag(row["dataset_version"], [str(start), str(end)], "market-data:stale" if stale else "market-data:fresh")
    if if_none_match == tag:
        return Response(status_code=304, headers={"ETag": tag, "Cache-Control": "no-cache"})
    response.headers.update({"ETag": tag, "Cache-Control": "no-cache"})
    payload = dict(_payload(row["payload"]))
    payload["bars"] = [b for b in payload.get("bars", []) if (not start or b["time"] >= start.isoformat()) and (not end or b["time"] <= end.isoformat())]
    return {**payload, "company": {k: company[k] for k in ("ticker", "cik", "name")},
            "dataset_version": row["dataset_version"], "source_checked_at": row["source_checked_at"],
            "published_at": row["published_at"], "stale": stale}
