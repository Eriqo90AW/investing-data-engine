"""Check the generated documentation and execute its examples without a database."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from investing_data_engine import db
from investing_data_engine.api import extraction, inference
from investing_data_engine.main import app
from investing_data_engine.schemas import ExtractionAccepted, ExtractionRequest, JobResponse


@pytest.fixture
def client(monkeypatch):
    # Deliberately do not enter TestClient: startup would open real DB/queue connections.
    conn = SimpleNamespace(fetchrow=AsyncMock(return_value=None), fetch=AsyncMock(return_value=[]), fetchval=AsyncMock(return_value=1))

    @asynccontextmanager
    async def acquire():
        yield conn

    monkeypatch.setattr(db, "pool", SimpleNamespace(acquire=acquire))
    yield TestClient(app), conn


def test_all_operations_have_documentation_and_examples():
    schema = app.openapi()
    expected = {
        "/": "get",
        "/health": "get",
        "/v1/extraction/financials": "post",
        "/v1/extraction/jobs/{job_id}": "get",
        "/v1/inference/companies/{identifier}/financials": "get",
        "/v1/inference/companies/{identifier}/breakdowns": "get",
        "/v1/inference/companies/{identifier}/reports": "get",
        "/v1/inference/reports/{report_id}/download": "get",
    }
    assert set(schema["paths"]) == set(expected)
    assert {tag["name"] for tag in schema["tags"]} == {"documentation", "health", "extraction", "inference"}
    for path, method in expected.items():
        operation = schema["paths"][path][method]
        assert operation["summary"] and operation["description"] and operation["tags"]
        status = "307" if path == "/" else "202" if method == "post" else "200"
        success = operation["responses"][status]
        if path == "/":
            assert "content" not in success
            assert success["headers"]["Location"]["example"] == "/docs"
        elif path.endswith("/download"):
            assert "application/json" not in success["content"]
            assert success["content"]["application/pdf"]["schema"]["format"] == "binary"
            assert "Content-Disposition" in success["headers"]
        else:
            media = success["content"]["application/json"]
            assert "example" in media or "examples" in media
        for parameter in operation.get("parameters", []):
            assert parameter["description"]


def test_extraction_examples_match_models_and_status_contract():
    schema = app.openapi()
    request_example = schema["components"]["schemas"]["ExtractionRequest"]["examples"][0]
    ExtractionRequest.model_validate(request_example)
    operation = schema["paths"]["/v1/extraction/financials"]["post"]
    assert "200" in operation["responses"] and "202" in operation["responses"]
    for status in ("200", "202"):
        ExtractionAccepted.model_validate(operation["responses"][status]["content"]["application/json"]["example"])
    examples = schema["paths"]["/v1/extraction/jobs/{job_id}"]["get"]["responses"]["200"]["content"]["application/json"]["examples"]
    for example in examples.values():
        JobResponse.model_validate(example["value"])
    assert examples["failed"]["value"]["errors"][0]["code"] == "extractor_unavailable"


@pytest.mark.parametrize("kind", ["financials", "breakdowns"])
def test_conditional_read_documentation(kind):
    operation = app.openapi()["paths"][f"/v1/inference/companies/{{identifier}}/{kind}"]["get"]
    assert "content" not in operation["responses"]["304"]
    for status in ("200", "304"):
        assert set(operation["responses"][status]["headers"]) == {"ETag", "Cache-Control"}
    assert {p["name"] for p in operation["parameters"]} == {"identifier", "period", "version", "if-none-match"}


def test_root_redirects_to_swagger_without_database(client):
    http, conn = client
    redirect = http.get("/", follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers["location"] == "/docs"
    assert redirect.content == b""
    docs = http.get("/")
    assert docs.status_code == 200
    assert docs.url.path == "/docs"
    assert "SwaggerUIBundle" in docs.text
    assert http.get("/missing-route", follow_redirects=False).status_code == 404
    conn.fetchrow.assert_not_awaited()
    conn.fetchval.assert_not_awaited()


def test_docs_and_schema_load_without_database(client):
    http, conn = client
    docs = http.get("/docs")
    assert docs.status_code == 200
    assert "SwaggerUIBundle" in docs.text and "url: '/openapi.json'" in docs.text
    assert "swagger-ui-dist" in docs.text
    response = http.get("/openapi.json")
    assert response.status_code == 200
    assert "extractor_unavailable" in response.json()["info"]["description"]
    conn.fetchrow.assert_not_awaited()
    conn.fetchval.assert_not_awaited()


def test_health_and_missing_data_examples(client):
    http, conn = client
    assert http.get("/health").json() == {"status": "ok"}
    for kind in ("financials", "breakdowns", "reports"):
        response = http.get(f"/v1/inference/companies/AMZN/{kind}")
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "data_not_available"
    assert http.get("/v1/extraction/jobs/1").json() == {"detail": {"code": "job_not_found"}}
    response = http.post("/v1/extraction/financials", json={})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "identifier"]
    response = http.get("/v1/inference/companies/AMZN/financials?period=bad")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_period_format"


@pytest.mark.parametrize("fresh", [False, True])
def test_extraction_runtime_statuses_match_docs(client, monkeypatch, fresh):
    http, _ = client
    rows = iter([
        {"id": 1, "ticker": "AMZN"},
        None if fresh else {"id": 1},
        {"dataset_version": "illustrative-v1", "financials_payload": {"periods": [{"period": "2025-Q4"}]}},
    ])
    conn = SimpleNamespace(execute=AsyncMock())

    async def execute(sql, *args):
        # Advisory locking produces no application row.
        if "pg_advisory" in sql:
            return None
        return SimpleNamespace(fetchone=AsyncMock(return_value=next(rows)))

    @asynccontextmanager
    async def transaction():
        yield

    class Connection:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *args):
            pass

    conn.execute = execute
    conn.transaction = transaction
    monkeypatch.setattr(extraction.psycopg.AsyncConnection, "connect", AsyncMock(return_value=Connection()))
    response = http.post("/v1/extraction/financials", json={"identifier": "AMZN", "periods": ["2025-Q4"]})
    assert response.status_code == (200 if fresh else 202)
    assert response.json()["status"] == ("already_fresh" if fresh else "accepted")


@pytest.mark.parametrize("kind", ["financials", "breakdowns"])
def test_published_read_and_empty_304(client, kind):
    http, conn = client
    now = datetime.now(timezone.utc)
    company = {"id": 1, **inference.COMPANY_EXAMPLE}
    period = {"period": "2025-Q4", "period_end": "2025-12-31"}
    dataset = {
        "dataset_version": "fixture-v1", "source_checked_at": now, "published_at": now, "stale_after": now,
        "financials_payload": {"periods": [period]}, "breakdowns_payload": {"periods": [period]},
    }
    conn.fetchrow.side_effect = [company, dataset, company, dataset]
    url = f"/v1/inference/companies/AMZN/{kind}?period=2025-Q4&version=fixture-v1"
    response = http.get(url)
    assert response.status_code == 200
    assert response.json()["periods"] == [period]
    assert response.json()["dataset_version"] == "fixture-v1"
    conditional = http.get(url, headers={"If-None-Match": response.headers["etag"]})
    assert conditional.status_code == 304 and conditional.content == b""
    assert conditional.headers["etag"] == response.headers["etag"]


def test_file_download_and_missing_file_errors(client, monkeypatch, tmp_path):
    http, conn = client
    path = tmp_path / "report.html"
    path.write_text("<html><body>Test report fixture</body></html>", encoding="utf-8")
    monkeypatch.setattr(inference, "get_settings", lambda: SimpleNamespace(document_dir=tmp_path))
    report = {"storage_path": str(path), "media_type": "text/html", "original_filename": "report.html"}
    conn.fetchrow.return_value = report
    response = http.get("/v1/inference/reports/1/download")
    assert response.status_code == 200
    assert response.content == path.read_bytes()
    assert response.headers["content-type"].startswith("text/html")
    assert 'attachment; filename="report.html"' == response.headers["content-disposition"]
    conn.fetchrow.return_value = {**report, "storage_path": str(tmp_path / "absent.pdf")}
    assert http.get("/v1/inference/reports/1/download").json() == {"detail": {"code": "document_not_available"}}
    conn.fetchrow.return_value = None
    assert http.get("/v1/inference/reports/1/download").json() == {"detail": {"code": "data_not_available"}}
