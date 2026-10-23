"""Deterministic research acceptance tests; all financial figures below are illustrative fixtures."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

import pytest
from fastapi.testclient import TestClient
from investing_data_engine import db
from investing_data_engine.main import app
from investing_data_engine.schemas import ExtractionRequest, FinancialPeriod
from investing_data_engine.services.financials import normalize_companyfacts
from investing_data_engine.services.fallbacks import verified_table, merge_verified
from investing_data_engine.services.documents import inline_breakdowns
from investing_data_engine.services.sources import validate_url, SourceError
from investing_data_engine.workers import tasks

def row(value, start, end, form="10-K", filed="2026-02-01", accession="0000000001-26-000001"):
    return {"val": value, "start": start, "end": end, "form": form, "filed": filed, "accn": accession}

def fixture():
    return {"cik": 1, "facts": {"us-gaap": {
      "Revenues": {"units": {"USD": [
        row(1000,"2025-01-01","2025-12-31"),
        row(200,"2025-01-01","2025-03-31","10-Q"),
        row(450,"2025-01-01","2025-06-30","10-Q"),
        row(720,"2025-01-01","2025-09-30","10-Q"),
      ]}},
      "EarningsPerShareDiluted": {"units": {"USD/shares":[row(10,"2025-01-01","2025-12-31"),row(7,"2025-01-01","2025-09-30","10-Q")]}},
      "Assets": {"units": {"USD":[{"val":2000,"end":"2025-12-31","form":"10-K","filed":"2026-02-01","accn":"0000000001-26-000001"}]}},
    }}}

def test_standalone_quarters_derive_cumulative_money_but_never_eps():
    periods, coverage=normalize_companyfacts(fixture(),lookback={"annual":5,"quarterly":12})
    values={p["period"]:{f["concept"]:f for fs in p["statements"].values() for f in fs} for p in periods}
    assert [Decimal(values[f"2025-Q{q}"]["revenue"]["value"]) for q in range(1,5)] == [200,250,270,280]
    assert "diluted_eps" not in values["2025-Q4"]
    assert len(values["2025-Q4"]["revenue"]["evidence"]) == 2
    assert values["2025-Q4"]["revenue"]["formula"]
    assert values["2025"]["assets"]["period_kind"] == "instant"
    for p in periods:
        FinancialPeriod.model_validate(p)

def test_non_december_fiscal_calendar_and_missing_selection():
    payload=fixture()
    payload["facts"]["us-gaap"]["Revenues"]["units"]["USD"]=[
       row(1000,"2024-07-01","2025-06-30"),row(210,"2024-07-01","2024-09-30","10-Q")]
    periods,_=normalize_companyfacts(payload,requested=["2025-Q1"])
    assert len(periods)==1 and periods[0]["period_end"]=="2024-09-30"
    empty,_=normalize_companyfacts(payload,requested=["2023-Q2"])
    assert empty==[]

def test_contradictory_facts_and_foreign_currency_are_not_silently_selected():
    payload=fixture()
    payload["facts"]["us-gaap"]["Revenues"]["units"]["USD"].append(row(9999,"2025-01-01","2025-12-31"))
    periods,_=normalize_companyfacts(payload,requested=["2025"])
    assert not any(f["concept"]=="revenue" for p in periods for fs in p["statements"].values() for f in fs)
    payload["facts"]["us-gaap"]["Revenues"]["units"]={"EUR":[row(1000,"2025-01-01","2025-12-31")]}
    _,coverage=normalize_companyfacts(payload,lookback={"annual":5,"quarterly":12})
    assert "revenue" in coverage["missing_metrics"]

def test_conflicting_period_selectors_rejected():
    with pytest.raises(ValueError):
        ExtractionRequest(identifier="AMZN",periods=["2025"],lookback={"annual":5,"quarterly":12})

@pytest.mark.parametrize("url",["http://www.sec.gov/a","https://www.sec.gov.evil.test/a","https://127.0.0.1/a","https://www.sec.gov:444/a","https://user@www.sec.gov/a"])
def test_source_host_boundaries(url):
    with pytest.raises(SourceError):
        validate_url(url,"AMZN")
    assert validate_url("https://www.sec.gov/Archives/a","AMZN")
    assert validate_url("https://ir.aboutamazon.com/a","AMZN")

def test_html_units_periods_and_ocr_require_verification():
    periods=[{"period":"2025","period_start":"2025-01-01","period_end":"2025-12-31","period_kind":"annual","statements":{"income":[]}}]
    rows=[["","2025","2024"],["Revenue","100","90"]]
    assert verified_table(rows,"in millions",periods,{})==[]
    assert verified_table(rows,"USD in millions",periods,{},ocr=True)==[]
    candidates=verified_table(rows,"USD in millions",periods,{})
    assert candidates[0]["fact"]["value"]=="100000000"
    merge_verified(periods,candidates)
    assert verified_table(rows,"USD in millions",periods,{},ocr=True)
    assert verified_table(rows,"USD in millions nine months",periods,{})==[]

def test_inline_segment_extraction_preserves_period_unit_and_profit_measure():
    body=b'''<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi">
    <xbrli:context id="s"><xbrli:entity><xbrli:segment><xbrldi:explicitMember dimension="us-gaap:StatementBusinessSegmentsAxis">fixture:CloudMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity><xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-12-31</xbrli:endDate></xbrli:period></xbrli:context>
    <xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
    <ix:nonFraction name="us-gaap:Revenues" contextRef="s" unitRef="usd" scale="6">120</ix:nonFraction>
    <ix:nonFraction name="us-gaap:OperatingIncomeLoss" contextRef="s" unitRef="usd" scale="6">20</ix:nonFraction></html>'''
    items=inline_breakdowns(body,{"source_url":"https://www.sec.gov/Archives/fixture"})
    assert len(items)==1
    assert items[0]["dimension"]=="operating_segment"
    assert items[0]["revenue"]["value"]=="120000000"
    assert items[0]["profit_measure"]=="OperatingIncomeLoss"
    assert items[0]["profit"]["value"]=="20000000"

@pytest.fixture
def market_client(monkeypatch):
    now=datetime.now(timezone.utc)
    company={"id":1,"ticker":"AMZN","cik":"0001018724","name":"Illustrative company"}
    snapshot={"dataset_version":"price-v1","source_checked_at":now,"published_at":now,"stale_after":now-timedelta(hours=1),
      "payload":json.dumps({"provider":"fixture","currency":"USD","timezone":"America/New_York","adjustment":"fixture",
        "coverage_start":"2025-01-01","coverage_end":"2025-01-03",
        "bars":[{"time":"2025-01-02","open":9,"high":11,"low":8,"close":10,"volume":100},{"time":"2025-01-03","open":10,"high":12,"low":9,"close":11,"volume":120}]})}
    conn=SimpleNamespace(fetchrow=AsyncMock(side_effect=lambda sql,*args:company if "FROM companies" in sql else snapshot),fetch=AsyncMock(return_value=[]))
    @asynccontextmanager
    async def acquire():
        yield conn
    monkeypatch.setattr(db,"pool",SimpleNamespace(acquire=acquire))
    return TestClient(app),conn

def test_stored_market_reads_are_conditional_and_bounded(market_client):
    client,conn=market_client
    url="/v1/inference/companies/AMZN/market-data?start=2025-01-03"
    result=client.get(url)
    assert result.status_code==200 and result.json()["stale"]
    assert len(result.json()["bars"])==1
    unchanged=client.get(url,headers={"If-None-Match":result.headers["etag"]})
    assert unchanged.status_code==304 and unchanged.content==b""
    assert client.get("/v1/inference/companies/AMZN/market-data?start=2025-02-01&end=2025-01-01").status_code==422

async def test_worker_source_failure_preserves_prior_publication(monkeypatch):
    job={"id":1,"company_id":1,"status":"queued","kind":"market-data"}
    company={"id":1,"ticker":"AMZN","cik":"0001018724","name":"Fixture"}
    conn=SimpleNamespace(fetchrow=AsyncMock(side_effect=[job,company]),execute=AsyncMock())
    @asynccontextmanager
    async def transaction():
        yield
    @asynccontextmanager
    async def acquire():
        yield conn
    conn.transaction=transaction
    monkeypatch.setattr(tasks,"get_pool",lambda:SimpleNamespace(acquire=acquire))
    def fail(ticker):
        raise SourceError("rate_limited","Source rate limit")
    monkeypatch.setattr(tasks,"retrieve_market",fail)
    await tasks.execute_job(1)
    statements=[call.args[0] for call in conn.execute.await_args_list]
    assert not any("INSERT INTO market_snapshots" in sql or "DELETE" in sql for sql in statements)
    assert "status='failed'" in statements[-1]

async def test_worker_market_publication_and_terminal_job_are_atomic(monkeypatch):
    job={"id":1,"company_id":1,"status":"queued","kind":"market-data"}
    company={"id":1,"ticker":"AMZN","cik":"0001018724","name":"Fixture"}
    conn=SimpleNamespace(fetchrow=AsyncMock(side_effect=[job,company]),execute=AsyncMock())
    transactions=[]
    @asynccontextmanager
    async def transaction():
        transactions.append("begin")
        yield
        transactions.append("commit")
    @asynccontextmanager
    async def acquire():
        yield conn
    conn.transaction=transaction
    monkeypatch.setattr(tasks,"get_pool",lambda:SimpleNamespace(acquire=acquire))
    monkeypatch.setattr(tasks,"retrieve_market",lambda ticker:{"bars":[]})
    await tasks.execute_job(1)
    statements=[call.args[0] for call in conn.execute.await_args_list]
    assert any("INSERT INTO market_snapshots" in sql for sql in statements)
    assert "status='published'" in statements[-1]
    assert transactions==["begin","commit","begin","commit"]


def test_etag_invalidates_when_snapshot_becomes_stale(market_client):
    client,conn=market_client
    snapshot=conn.fetchrow.side_effect("SELECT * FROM market_snapshots",1,None)
    snapshot["stale_after"]=datetime.now(timezone.utc)+timedelta(hours=1)
    url="/v1/inference/companies/AMZN/market-data"
    fresh=client.get(url)
    assert fresh.json()["stale"] is False
    snapshot["stale_after"]=datetime.now(timezone.utc)-timedelta(seconds=1)
    stale=client.get(url,headers={"If-None-Match":fresh.headers["etag"]})
    assert stale.status_code==200 and stale.json()["stale"] is True
    assert stale.headers["etag"]!=fresh.headers["etag"]
