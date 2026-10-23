"""Document fallback acceptance cases with constructed source tables, never financial examples."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pymupdf
import pytest
from investing_data_engine.services import documents, sources
from investing_data_engine.services.fallbacks import pdf_facts, verified_table

def test_pdf_text_fallback_keeps_page_evidence():
    doc=pymupdf.open()
    page=doc.new_page()
    page.insert_text((50,60),"Revenue in USD millions, with source evidence retained on this page.")
    body=doc.tobytes()
    doc.close()
    tables=documents.table_candidates(body,"application/pdf",{"document_sha256":"fixture"})
    assert len(tables)==1 and tables[0]["evidence"]["page"]==1
    assert tables[0]["evidence"]["method"]=="PDF text"

def test_ocr_candidates_need_independent_corroboration(monkeypatch):
    page=SimpleNamespace(get_text=lambda **kwargs:"USD in millions\n  2025  2024\nRevenue  100  90" if kwargs else "",
                         get_textpage_ocr=lambda **kwargs:SimpleNamespace())
    class Document:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def __iter__(self): return iter([page])
    monkeypatch.setattr(pymupdf,"open",lambda **kwargs:Document())
    periods=[{"period":"2025","period_start":"2025-01-01","period_end":"2025-12-31","period_kind":"annual","statements":{"income":[]}}]
    assert pdf_facts(b"fixture",periods,{})==[]
    periods[0]["statements"]["income"].append({"concept":"revenue","value":"100000000"})
    result=pdf_facts(b"fixture",periods,{})
    assert result and result[0]["fact"]["evidence"][0]["method"]=="corroborated OCR table"

async def test_redirect_is_revalidated_before_following(monkeypatch):
    seen=[]
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302,headers={"location":"https://127.0.0.1/private"})
    real_client=httpx.AsyncClient
    monkeypatch.setattr(sources,"get_settings",lambda:SimpleNamespace(sec_contact_email="test@example.invalid"))
    monkeypatch.setattr(sources.httpx,"AsyncClient",lambda **kwargs:real_client(transport=httpx.MockTransport(handler),**kwargs))
    with pytest.raises(sources.SourceError,match="outside verified"):
        await sources.download("https://www.sec.gov/Archives/fixture")
    assert len(seen)==1

async def test_browser_blocks_unapproved_subrequests_and_closes(monkeypatch):
    from playwright import async_api
    state={}
    browser=SimpleNamespace()
    context=SimpleNamespace()
    page=SimpleNamespace(url="https://ir.aboutamazon.com/fixture",content=AsyncMock(return_value="<html>fixture</html>"),goto=AsyncMock())
    async def capture(pattern,handler):
        state["route"]=handler
    context.route=capture
    context.new_page=AsyncMock(return_value=page)
    browser.new_context=AsyncMock(return_value=context)
    browser.close=AsyncMock()
    class Runtime:
        async def __aenter__(self):return SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
        async def __aexit__(self,*args):pass
    monkeypatch.setattr(async_api,"async_playwright",lambda:Runtime())
    body=await documents.browser_document(page.url,"AMZN")
    assert b"fixture" in body
    route=SimpleNamespace(request=SimpleNamespace(url="https://private.invalid/a"),continue_=AsyncMock(),abort=AsyncMock())
    await state["route"](route)
    route.abort.assert_awaited_once()
    route.continue_.assert_not_awaited()
    browser.close.assert_awaited_once()


def test_scanned_pdf_ocr_keeps_page_and_method():
    import shutil
    from investing_data_engine.config import get_settings
    if not get_settings().ocr_tessdata_dir and not shutil.which("tesseract"):
        pytest.skip("Install Tesseract or configure FINANCIALS_OCR_TESSDATA_DIR for native OCR verification.")
    source=pymupdf.open()
    page=source.new_page()
    page.insert_text((60,90),"Revenue in USD millions, source evidence 123",fontsize=18)
    image=page.get_pixmap(matrix=pymupdf.Matrix(2,2)).tobytes("png")
    scanned=pymupdf.open()
    scan=scanned.new_page()
    scan.insert_image(scan.rect,stream=image)
    body=scanned.tobytes()
    scanned.close()
    source.close()
    candidates=documents.table_candidates(body,"application/pdf",{"document_sha256":"scanned-fixture"})
    assert candidates and "Revenue" in candidates[0]["text"]
    assert candidates[0]["evidence"]["page"]==1
    assert candidates[0]["evidence"]["method"]=="OCR candidate, unverified"
