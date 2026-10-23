"""Conservative document-table adapters. Headers, units and periods must be explicit."""
from decimal import Decimal, InvalidOperation
from datetime import date
import re
from .financials import METRICS
from ..config import get_settings

LABELS = {
 "revenue": {"revenue", "revenues", "net sales", "total net sales", "total revenues", "total revenue"},
 "gross_profit": {"gross profit", "gross margin"},
 "operating_income": {"operating income", "income from operations", "operating income loss"},
 "net_income": {"net income", "net income loss"},
 "assets": {"total assets"}, "liabilities": {"total liabilities"},
 "equity": {"total stockholders equity", "total shareholders equity"},
 "cash": {"cash and cash equivalents"},
 "operating_cash_flow": {"net cash provided by operating activities", "net cash provided by used in operating activities"},
 "capex": {"purchases of property and equipment", "payments to acquire property plant and equipment"},
}


def clean_label(text):
    return re.sub(r"[^a-z ]", "", text.lower()).strip()


def parse_number(text):
    text = text.strip().replace(",", "").replace("$", "").replace(" ", "")
    if not re.fullmatch(r"\(?-?\d+(?:\.\d+)?\)?", text):
        return None
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        value = Decimal(text)
        return value if value.is_finite() else None
    except InvalidOperation:
        return None


def verified_table(rows, context, periods, evidence, ocr=False):
    """Return facts only when exactly one date column matches and no period ambiguity exists."""
    if not re.search(r"\bUSD\b|U\.?S\.? dollars", context, re.I):
        return []
    scale_match = re.search(r"(?:in |of )(millions|thousands|billions)", context, re.I)
    if not scale_match:
        return []  # Never assume that a document printed values in base units.
    scale = {"millions": 1000000, "thousands": 1000, "billions": 1000000000}[scale_match[1].lower()]
    if re.search(r"nine months|six months|year.to.date", context, re.I):
        return []
    is_quarter = bool(re.search("three months", context, re.I))
    header = next((row for row in rows[:8] if sum(bool(re.fullmatch(r"(?:19|20)\d{2}", str(x).strip())) for x in row) >= 1), None)
    if header is None:
        return []
    years = [str(x).strip() for x in header if re.fullmatch(r"(?:19|20)\d{2}", str(x).strip())]
    if len(set(years)) != len(years):
        return []
    result = []
    for row in rows:
        if len(row) < 2:
            continue
        label = clean_label(str(row[0]))
        metric = next((key for key, aliases in LABELS.items() if label in aliases), None)
        if not metric:
            continue
        cells = [parse_number(str(x)) for x in row[1:] if str(x).strip() not in ("", "$")]
        if len(cells) != len(years):
            continue
        for period in periods:
            if (period["period_kind"] == "quarter") != is_quarter:
                continue
            if is_quarter:
                end = date.fromisoformat(period["period_end"])
                normalized_context = re.sub(r"\s+", " ", context.lower()).replace(",", "")
                dated = [end.isoformat(), end.strftime("%B %d %Y").lower(), end.strftime("%B %d %Y").lower().replace(" 0", " ")]
                if not any(text in normalized_context for text in dated):
                    continue
            end_year = period["period_end"][:4]
            if end_year not in years:
                continue
            value = cells[years.index(end_year)]
            if value is None:
                continue
            statement, title, _ = METRICS[metric]
            existing = {f["concept"]: f for f in period["statements"].get(statement, [])}
            # OCR is never sufficient without independent matching structured evidence.
            structured = existing.get(metric)
            if ocr and (not structured or Decimal(str(structured["value"])) != value * scale):
                continue
            result.append({"period": period["period"], "statement": statement, "fact": {
                "concept": metric, "label": title, "value": str(value * scale), "unit": "USD", "currency": "USD", "scale": "1",
                "accounting_basis": "US GAAP", "period_start": None if statement == "balance" else period.get("period_start"),
                "period_end": period["period_end"], "period_kind": "instant" if statement == "balance" else period["period_kind"],
                "evidence": [{**evidence, "table_label": str(row[0]), "printed_value": str(value), "printed_scale": scale,
                              "method": "corroborated OCR table" if ocr else evidence.get("method", "explicit document table"),
                              "parser_version": "1"}], "null_reason": None}})
    return result


def html_facts(body, periods, evidence):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(body, "html.parser")
    result = []
    for index, table in enumerate(soup.find_all("table")):
        rows = [[cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"], recursive=False)]
                for row in table.find_all("tr")]
        # Scope the unit and duration to this table and its immediately preceding heading.
        prefix = []
        for sibling in table.previous_siblings:
            text = sibling.get_text(" ", strip=True) if hasattr(sibling, "get_text") else str(sibling).strip()
            if text:
                prefix.append(text)
            if len(" ".join(prefix)) > 1200 or len(prefix) >= 3:
                break
        context = " ".join(reversed(prefix)) + " " + table.get_text(" ", strip=True)[:1500]
        result.extend(verified_table(rows, context, periods, {**evidence, "table": index + 1, "method": "HTML table"}))
    return result


def pdf_facts(body, periods, evidence):
    import pymupdf
    result = []
    with pymupdf.open(stream=body, filetype="pdf") as document:
        for index, page in enumerate(document):
            if index >= 80:
                break
            text = page.get_text()
            if len(text.strip()) < 30:
                text = page.get_text(textpage=page.get_textpage_ocr(language="eng", dpi=150, tessdata=str(get_settings().ocr_tessdata_dir) if get_settings().ocr_tessdata_dir else None))
                rows = [re.split(r"\s{2,}", line.strip()) for line in text.splitlines()]
                result.extend(verified_table(rows, text, periods, {**evidence, "page": index + 1}, ocr=True))
            else:
                for number, table in enumerate(page.find_tables().tables):
                    result.extend(verified_table(table.extract(), text[:3000], periods, {**evidence, "page": index + 1, "table": number + 1, "method": "PDF table"}))
    return result


def merge_verified(periods, candidates):
    """Retain structured sources, reject contradictory candidate duplicates."""
    grouped = {}
    for candidate in candidates:
        key = (candidate["period"], candidate["fact"]["concept"])
        grouped.setdefault(key, []).append(candidate)
    for (period, concept), items in grouped.items():
        if len({x["fact"]["value"] for x in items}) != 1:
            continue
        target = next((p for p in periods if p["period"] == period), None)
        if target is None:
            continue
        facts = target["statements"].setdefault(items[0]["statement"], [])
        existing = next((f for f in facts if f["concept"] == concept), None)
        if existing:
            if Decimal(str(existing["value"])) == Decimal(items[0]["fact"]["value"]):
                existing["evidence"].extend(items[0]["fact"]["evidence"])
        else:
            facts.append(items[0]["fact"])
