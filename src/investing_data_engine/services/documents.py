"""Evidence-preserving filing fallbacks. Candidates do not become verified facts."""
from decimal import Decimal, InvalidOperation
from io import BytesIO
import re
from .sources import SourceError, validate_url, MAX_BYTES
from ..config import get_settings


def inline_breakdowns(body: bytes, source: dict) -> list[dict]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(body, "xml")
    contexts = {}
    for c in soup.find_all(lambda t: t.name and t.name.split(":")[-1].lower() == "context"):
        dimensions = [(x.get("dimension", ""), x.get_text()) for x in c.find_all(lambda t: t.name and t.name.split(":")[-1].lower() == "explicitMember".lower())]
        end = c.find(lambda t: t.name and t.name.split(":")[-1] in ("endDate", "instant"))
        start = c.find(lambda t: t.name and t.name.split(":")[-1] == "startDate")
        if end and dimensions:
            contexts[c.get("id")] = {"end": end.get_text(), "start": start.get_text() if start else None, "dimensions": dimensions}
    result = []
    for tag in soup.find_all(lambda t: t.name and t.name.split(":")[-1].lower() == "nonfraction"):
        context = contexts.get(tag.get("contextRef") or tag.get("contextref"))
        concept = tag.get("name", "")
        if not context or not re.search(r"Revenue|SalesRevenue|OperatingIncome|Segment.*Profit", concept):
            continue
        if len(context["dimensions"]) != 1:
            continue  # A combined geography/product context cannot be presented as either total.
        axis, member = context["dimensions"][0]
        dimension = "geographic" if re.search("Geograph|Country|Region", axis) else "product_service" if re.search("Product|Service", axis) else "operating_segment" if re.search("Segment|Business", axis) else None
        if not dimension:
            continue
        try:
            text = tag.get_text().replace(",", "").replace(" ", "").strip()
            value = Decimal(text) * (Decimal(10) ** int(tag.get("scale", 0))) * (-1 if tag.get("sign") == "-" else 1)
        except (InvalidOperation, ValueError):
            continue
        if not value.is_finite() or tag.get("format", "").split(":")[-1] not in ("", "num-dot-decimal"):
            continue
        days = 0
        if context["start"]:
            from datetime import date
            days = (date.fromisoformat(context["end"]) - date.fromisoformat(context["start"])).days
        if not (60 <= days <= 120 or 330 <= days <= 400):
            continue
        unit_id = tag.get("unitRef") or tag.get("unitref")
        unit = soup.find(lambda t: t.name and t.name.split(":")[-1] == "unit" and t.get("id") == unit_id)
        if not unit or "USD" not in unit.get_text() or "divide" in str(unit):
            continue
        evidence = [{**source, "concept": concept, "context": tag.get("contextRef") or tag.get("contextref"), "method": "inline XBRL", "parser_version": "1"}]
        fact = {"label": concept.split(":")[-1], "value": str(value), "currency": "USD", "unit": "USD", "scale": "1",
                "accounting_basis": "US GAAP", "period_start": context["start"], "period_end": context["end"],
                "period_kind": "annual" if days >= 330 else "quarter", "evidence": evidence}
        label = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", member.split(":")[-1].removesuffix("Member"))
        is_revenue = bool(re.search("Revenue", concept))
        result.append({"dimension": dimension, "label": label, "revenue": fact if is_revenue else None,
                       "profit": None if is_revenue else fact, "profit_measure": None if is_revenue else fact["label"],
                       "comparable": True, "evidence": evidence, "period_end": context["end"], "period_start": context["start"]})
    # Multiple tagged alternatives for the same number must not double-count a segment.
    unique = {}
    for item in result:
        key = (item["dimension"], item["label"], item["period_start"], item["period_end"], bool(item["revenue"]), item["profit_measure"])
        previous = unique.get(key)
        field = "revenue" if item["revenue"] else "profit"
        if previous and previous[field]["value"] != item[field]["value"]:
            unique[key] = None
        elif previous:
            previous["evidence"].extend(item["evidence"])
        elif key not in unique:
            unique[key] = item
    groups = {}
    for item in (x for x in unique.values() if x):
        key = (item["dimension"], item["label"], item["period_start"], item["period_end"])
        groups.setdefault(key, []).append(item)
    combined = []
    for group in groups.values():
        revenue = next((x for x in group if x["revenue"]), None)
        profits = [x for x in group if x["profit"]]
        if profits:
            for item in profits:
                combined.append({**item, "revenue": revenue["revenue"] if revenue else None,
                                 "evidence": (revenue["evidence"] if revenue else []) + item["evidence"]})
        elif revenue:
            combined.append(revenue)
    return combined


def table_candidates(body: bytes, media_type: str, source: dict) -> list[dict]:
    """Return source-located raw tables; never infer a unit/period from an unlabeled cell."""
    if "pdf" in media_type:
        import pymupdf
        with pymupdf.open(stream=body, filetype="pdf") as doc:
            out = []
            for index, page in enumerate(doc):
                text = page.get_text()
                method = "PDF text"
                if len(text.strip()) < 30:
                    text = page.get_text(textpage=page.get_textpage_ocr(language="eng", dpi=150, tessdata=str(get_settings().ocr_tessdata_dir) if get_settings().ocr_tessdata_dir else None))
                    method = "OCR candidate, unverified"
                if re.search(r"revenue|cash flow|balance sheet|segment", text, re.I):
                    out.append({"text": text[:40000], "evidence": {**source, "page": index + 1, "method": method}})
                if len(out) >= 40:
                    break
            return out
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(body, "html.parser")
    return [{"text": table.get_text(" ", strip=True)[:40000],
             "evidence": {**source, "table": i + 1, "method": "HTML table candidate, unverified"}}
            for i, table in enumerate(soup.find_all("table")) if re.search(r"revenue|net income|cash flow|segment", table.get_text(), re.I)][:40]


async def browser_document(url: str, ticker: str) -> bytes:
    """Render only the verified source host; no third-party browser subrequests."""
    validate_url(url, ticker)
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(service_workers="block", accept_downloads=False, user_agent="Mozilla/5.0 InvestingResearch/1.0 " + (get_settings().sec_contact_email or ""))
            async def route(request_route):
                try:
                    validate_url(request_route.request.url, ticker)
                    await request_route.continue_()
                except SourceError:
                    await request_route.abort()
            await context.route("**/*", route)
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            validate_url(page.url, ticker)
            body = (await page.content()).encode()
            if len(body) > MAX_BYTES:
                raise SourceError("document_too_large", "Rendered document exceeds the limit.")
            return body
        finally:
            await browser.close()
