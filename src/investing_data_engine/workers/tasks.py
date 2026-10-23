import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from uuid import uuid4
import procrastinate

from ..config import get_settings
from ..db import get_pool
from ..queue import app
from ..services.sources import SourceError, download, source_json, store_document, discover, validate_url
from ..services.financials import normalize_companyfacts
from ..services.documents import inline_breakdowns, table_candidates, browser_document
from ..services.fallbacks import html_facts, pdf_facts, merge_verified
from ..services.financials import METRICS
from ..services.market import retrieve_market
from ..schemas import FinancialPeriod, BreakdownPeriod, MarketResponse


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


async def progress(job_id, stage):
    async with get_pool().acquire() as conn:
        await conn.execute("UPDATE extraction_jobs SET progress=$2::jsonb WHERE id=$1",
                           job_id, json.dumps({"stage": stage}))


async def financial_payload(company, job):
    cik = company["cik"]
    await progress(job["id"], "SEC company facts")
    facts = await source_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
    options = decoded(job["options"])
    periods, completeness = normalize_companyfacts(facts, job["requested_periods"], options.get("lookback"))
    if not periods:
        raise SourceError("data_not_available", "No supported financial periods were found for this selection.")
    await progress(job["id"], "source reports and business breakdowns")
    submissions = await source_json(f"https://data.sec.gov/submissions/CIK{cik}.json")
    recent = submissions.get("filings", {}).get("recent", {})
    needed = {e["accession"] for p in periods for fs in p["statements"].values() for f in fs for e in f["evidence"]}
    def index(rows):
        return {acc: {k: values[i] for k, values in rows.items() if isinstance(values, list) and len(values) > i}
                for i, acc in enumerate(rows.get("accessionNumber", []))}
    filings = index(recent)
    for older in submissions.get("filings", {}).get("files", [])[:10]:
        if needed.issubset(filings):
            break
        name = older.get("name", "")
        if "/" not in name and name.endswith(".json"):
            filings.update(index(await source_json(f"https://data.sec.gov/submissions/{name}")))
    reports, breakdowns, errors = [], [], []
    fallback_candidates = []
    for accession in sorted(needed):
        info = filings.get(accession)
        if not info or not info.get("primaryDocument"):
            errors.append({"code": "report_not_available", "accession": accession})
            continue
        name = info["primaryDocument"]
        if "/" in name or "\\" in name:
            continue
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{name}"
        try:
            body = await download(url, company["ticker"] or "")
            path, digest = store_document(body, ".html")
            evidence = {"accession": accession, "source_url": url, "document_sha256": digest}
            fallback_candidates.extend(await asyncio.to_thread(html_facts, body, periods, evidence))
            items = await asyncio.to_thread(inline_breakdowns, body, evidence)
            breakdowns.extend(items)
            candidates = await asyncio.to_thread(table_candidates, body, "text/html", evidence)
            reports.append({"accession": accession, "form": info["form"], "filed_at": info.get("filingDate"),
                            "period_end": info.get("reportDate"), "url": url, "path": str(path), "hash": digest,
                            "filename": name, "candidates": len(candidates), "media_type": "text/html"})
        except Exception as exc:
            errors.append({"code": getattr(exc, "code", "report_retrieval_failed"), "accession": accession})
    merge_verified(periods, fallback_candidates)
    if company["ticker"] in IR_PAGES and (completeness["missing_metrics"] or not breakdowns):
        await progress(job["id"], "issuer document fallbacks")
        try:
            extra = await issuer_fallbacks(company, periods)
            reports.extend(extra)
        except Exception as exc:
            errors.append({"code": getattr(exc, "code", "issuer_fallback_unavailable"), "message": "Issuer fallback could not be verified."})
    present = {f["concept"] for p in periods for fs in p["statements"].values() for f in fs}
    completeness["missing_metrics"] = sorted(set(METRICS) - present)
    report_map = {r["accession"]: r for r in reports if r["accession"]}
    for p in periods:
        for fs in p["statements"].values():
            for f in fs:
                for e in f["evidence"]:
                    report = report_map.get(e["accession"])
                    if report:
                        e.update(source_url=report["url"], document_sha256=report["hash"])
    breakdown_periods = []
    for p in periods:
        items = [{k: v for k, v in b.items() if k not in ("period_start", "period_end")}
                 for b in breakdowns if b["period_end"] == p["period_end"] and b["period_start"] == p["period_start"]]
        breakdown_periods.append({"period": p["period"], "period_end": p["period_end"],
                                 "period_kind": p["period_kind"], "items": items})
    completeness["report_errors"] = errors
    completeness["fallback_note"] = "Unverified document table/OCR candidates are not published as financial values."
    return {"periods": periods, "completeness": completeness}, {"periods": breakdown_periods}, reports, errors


async def execute_job(job_id):
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            job = await conn.fetchrow("SELECT * FROM extraction_jobs WHERE id=$1 FOR UPDATE", job_id)
            if job is None or job["status"] not in ("queued", "running"):
                return
            await conn.execute("UPDATE extraction_jobs SET status='running', started_at=coalesce(started_at,now()), progress=$2::jsonb WHERE id=$1",
                               job_id, json.dumps({"stage": "retrieval"}))
            company = await conn.fetchrow("SELECT id,cik,ticker,name FROM companies WHERE id=$1", job["company_id"])
    try:
        if not company["ticker"] or company["name"].startswith("CIK "):
            identity = await discover(company["cik"])
            async with pool.acquire() as conn:
                await conn.execute("UPDATE companies SET ticker=coalesce(ticker,$2),name=$3 WHERE id=$1", company["id"], identity["ticker"], identity["name"])
            company = {**dict(company), **identity}
        now = datetime.now(timezone.utc)
        version = str(uuid4())
        if job["kind"] == "market-data":
            await progress(job_id, "Yahoo daily prices")
            payload = await asyncio.to_thread(retrieve_market, company["ticker"])
            MarketResponse.model_validate({**payload,
                "company": {key: company[key] for key in ("ticker", "cik", "name")},
                "dataset_version": version, "source_checked_at": now,
                "published_at": now, "stale": False})
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("""INSERT INTO market_snapshots(company_id,extraction_job_id,dataset_version,source_checked_at,stale_after,payload)
                        VALUES($1,$2,$3,$4,$5,$6::jsonb)""", company["id"], job_id, version, now, now + timedelta(hours=24), json.dumps(payload))
                    await conn.execute("UPDATE extraction_jobs SET status='published', completed_at=now(),progress=$2::jsonb WHERE id=$1",
                                       job_id, json.dumps({"stage": "published"}))
            return
        financials, breakdowns, reports, errors = await asyncio.wait_for(financial_payload(company, job), get_settings().extraction_timeout_seconds)
        for period in financials["periods"]:
            FinancialPeriod.model_validate(period)
        for period in breakdowns["periods"]:
            BreakdownPeriod.model_validate(period)
        async with pool.acquire() as conn:
            async with conn.transaction():
                ids = {}
                for r in reports:
                    rid = await conn.fetchval("""INSERT INTO reports(company_id,cik,accession_number,form,filed_at,period_end,source_url,
                        document_sha256,storage_path,media_type,original_filename,parser_version)
                        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'1')
                        ON CONFLICT(company_id,document_sha256) DO UPDATE SET source_url=EXCLUDED.source_url RETURNING id""",
                        company["id"], company["cik"], r["accession"], r["form"],
                        datetime.fromisoformat(r["filed_at"]).date() if r["filed_at"] else None,
                        datetime.fromisoformat(r["period_end"]).date() if r["period_end"] else None,
                        r["url"], r["hash"], r["path"], r.get("media_type", "text/html"), r["filename"])
                    ids[r["hash"]] = rid
                    if r["accession"]:
                        ids[r["accession"]] = rid
                for payload in (financials, breakdowns):
                    def attach(value):
                        if isinstance(value, dict):
                            if value.get("accession") in ids:
                                value["report_id"] = ids[value["accession"]]
                            elif value.get("document_sha256") in ids:
                                value["report_id"] = ids[value["document_sha256"]]
                            for v in value.values():
                                attach(v)
                        elif isinstance(value, list):
                            for v in value:
                                attach(v)
                    attach(payload)
                dataset_id = await conn.fetchval("""INSERT INTO published_datasets(company_id,extraction_job_id,dataset_version,status,
                    source_checked_at,published_at,stale_after,financials_payload,breakdowns_payload)
                    VALUES($1,$2,$3,'published',$4,$4,$5,$6::jsonb,$7::jsonb) RETURNING id""",
                    company["id"], job_id, version, now, now + timedelta(days=7), json.dumps(financials), json.dumps(breakdowns))
                for period in financials["periods"]:
                    for statement, facts in period["statements"].items():
                        for fact in facts:
                            await conn.execute("""INSERT INTO facts(dataset_id,statement,concept,label,value,unit,currency,scale,accounting_basis,
                                period_start,period_end,period_kind,evidence,formula,null_reason)
                                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14::jsonb,$15)""",
                                dataset_id, statement, fact["concept"], fact["label"], Decimal(fact["value"]) if fact["value"] is not None else None,
                                fact.get("unit"), fact.get("currency"), fact.get("scale"), fact.get("accounting_basis"),
                                datetime.fromisoformat(fact["period_start"]).date() if fact.get("period_start") else None,
                                datetime.fromisoformat(fact["period_end"]).date(), fact["period_kind"],
                                json.dumps(fact["evidence"]), json.dumps({"description": fact["formula"]}) if fact.get("formula") else None, fact.get("null_reason"))
                await conn.execute("UPDATE extraction_jobs SET status=$2,completed_at=now(),progress=$3::jsonb,errors=$4::jsonb WHERE id=$1",
                    job_id, "partial" if errors or financials["completeness"]["missing_metrics"] else "published",
                    json.dumps({"stage": "published"}), json.dumps(errors))
    except Exception as exc:
        code = getattr(exc, "code", "retrieval_failed")
        message = str(exc)[:300] if isinstance(exc, SourceError) else "Source retrieval or validation failed. Previous publications remain available."
        async with pool.acquire() as conn:
            await conn.execute("UPDATE extraction_jobs SET status='failed', completed_at=now(),errors=$2::jsonb,progress=$3::jsonb WHERE id=$1",
                job_id, json.dumps([{"code": code, "message": message}]), json.dumps({"stage": "failed"}))


@app.task(queue="extraction", retry=procrastinate.RetryStrategy(max_attempts=3))
async def extract_company_financials(job_id: int):
    await execute_job(job_id)


IR_PAGES = {
    "AMZN": "https://ir.aboutamazon.com/quarterly-results/default.aspx",
    "MSFT": "https://www.microsoft.com/en-us/Investor/earnings/",
    "NVDA": "https://investor.nvidia.com/financial-info/quarterly-results-and-annual-reports/default.aspx",
    "JPM": "https://www.jpmorganchase.com/ir/annual-report",
    "WMT": "https://stock.walmart.com/financials/quarterly-results/default.aspx",
}


async def issuer_fallbacks(company, periods):
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin, urlparse
    ticker = company["ticker"]
    page_url = IR_PAGES[ticker]
    body = await download(page_url, ticker)
    # Browser rendering is a document-discovery fallback, never an anti-bot bypass.
    soup = BeautifulSoup(body, "html.parser")
    links = [urljoin(page_url, a.get("href", "")) for a in soup.find_all("a") if ".pdf" in a.get("href", "").lower()]
    if not links:
        body = await browser_document(page_url, ticker)
        soup = BeautifulSoup(body, "html.parser")
        links = [urljoin(page_url, a.get("href", "")) for a in soup.find_all("a") if ".pdf" in a.get("href", "").lower()]
    reports = []
    candidates = []
    for url in list(dict.fromkeys(links))[:3]:
        try:
            validate_url(url, ticker)
        except SourceError:
            continue
        content = await download(url, ticker)
        if not content.startswith(b"%PDF"):
            continue
        path, digest = store_document(content, ".pdf")
        evidence = {"source_url": url, "document_sha256": digest}
        candidates.extend(await asyncio.to_thread(pdf_facts, content, periods, evidence))
        reports.append({"accession": None, "form": "Issuer report", "filed_at": None, "period_end": None,
                        "url": url, "path": str(path), "hash": digest, "filename": path.name, "media_type": "application/pdf"})
    merge_verified(periods, candidates)
    return reports
