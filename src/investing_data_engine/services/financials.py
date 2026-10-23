"""Normalize reported SEC facts. Unmapped concepts remain unavailable, never guessed."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

# Ordered aliases are alternatives, not quantities to add together.
METRICS = {
 "revenue": ("income", "Revenue", ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet", "RevenuesNetOfInterestExpense"]),
 "gross_profit": ("income", "Gross profit", ["GrossProfit"]),
 "operating_income": ("income", "Operating income", ["OperatingIncomeLoss"]),
 "net_income": ("income", "Net income", ["NetIncomeLoss", "ProfitLoss"]),
 "diluted_eps": ("income", "Diluted EPS", ["EarningsPerShareDiluted"]),
 "assets": ("balance", "Total assets", ["Assets"]),
 "liabilities": ("balance", "Total liabilities", ["Liabilities"]),
 "equity": ("balance", "Stockholders equity", ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]),
 "cash": ("balance", "Cash and equivalents", ["CashAndCashEquivalentsAtCarryingValue"]),
 "debt_current": ("balance", "Current debt", ["LongTermDebtCurrent", "ShortTermBorrowings"]),
 "debt_noncurrent": ("balance", "Noncurrent debt", ["LongTermDebtNoncurrent", "LongTermDebt"]),
 "shares": ("balance", "Common shares outstanding", ["CommonStockSharesOutstanding", "EntityCommonStockSharesOutstanding"]),
 "operating_cash_flow": ("cash_flow", "Operating cash flow", ["NetCashProvidedByUsedInOperatingActivities"]),
 "capex": ("cash_flow", "Capital expenditure", ["PaymentsToAcquirePropertyPlantAndEquipment"]),
}

def duration(row):
    return (date.fromisoformat(row["end"]) - date.fromisoformat(row["start"])).days if row.get("start") else 0


def fiscal_label(end, anchors):
    d = date.fromisoformat(end)
    # Annual ends are reported dates, including 52/53-week fiscal calendars.
    next_end = next((date.fromisoformat(x) for x in anchors if x >= end), None)
    if next_end is None and anchors:
        last = date.fromisoformat(anchors[-1])
        year = d.year + (1 if (d.month, d.day) > (last.month, last.day) else 0)
        try:
            next_end = last.replace(year=year)
        except ValueError:
            next_end = last.replace(year=year, day=28)
    if next_end is None:
        return None
    months_to_end = (next_end - d).days / 30.4375
    q = max(1, min(4, 4 - round(months_to_end / 3)))
    return f"{next_end.year}-Q{q}"


def normalize_companyfacts(payload, requested=None, lookback=None):
    concepts = {**payload.get("facts", {}).get("us-gaap", {}), **payload.get("facts", {}).get("dei", {})}
    candidates = defaultdict(list)
    anchors = set()
    for metric, (_, label, aliases) in METRICS.items():
        for rank, concept in enumerate(aliases):
            for unit, rows in concepts.get(concept, {}).get("units", {}).items():
                for r in rows:
                    if r.get("form") not in ("10-K", "10-Q", "10-K/A", "10-Q/A") or not r.get("accn"):
                        continue
                    expected = "shares" if metric == "shares" else "USD/shares" if metric == "diluted_eps" else "USD"
                    if unit != expected:
                        continue
                    try:
                        value = Decimal(str(r["val"]))
                        days = duration(r)
                    except (KeyError, ValueError, InvalidOperation):
                        continue
                    if not value.is_finite():
                        continue
                    if 330 <= days <= 400 and r["form"] in ("10-K", "10-K/A"):
                        anchors.add(r["end"])
                    candidates[metric].append({**r, "value": value, "concept": concept, "unit": unit, "rank": rank, "days": days})
    anchors = sorted(anchors)
    # Convert cumulative monetary cash/earnings flows into standalone quarters.
    # EPS and share counts are not additive and must never be subtracted.
    for metric, rows in list(candidates.items()):
        if METRICS[metric][0] == "balance" or metric == "diluted_eps":
            continue
        generated = []
        for current in rows:
            if current["days"] <= 120:
                continue
            prior = [r for r in rows if r.get("start") == current.get("start")
                     and r["end"] < current["end"] and r["rank"] == current["rank"]
                     and 60 <= (date.fromisoformat(current["end"]) - date.fromisoformat(r["end"])).days <= 120]
            if not prior:
                continue
            previous = max(prior, key=lambda r: (r["end"], r.get("filed", "")))
            start = (date.fromisoformat(previous["end"]) + timedelta(days=1)).isoformat()
            if any(r.get("start") == start and r["end"] == current["end"] for r in rows):
                continue
            # Contradictory equal-date sources cannot support a derived result.
            if any(r["value"] != previous["value"] and r.get("start") == previous.get("start")
                   and r["end"] == previous["end"] and r.get("filed") == previous.get("filed") for r in prior):
                continue
            generated.append({**current, "start": start,
                              "days": (date.fromisoformat(current["end"]) - date.fromisoformat(start)).days,
                              "value": current["value"] - previous["value"],
                              "derived_from": [current, previous]})
        rows.extend(generated)
    if not anchors:
        return [], {"missing_metrics": list(METRICS), "reason": "No supported annual fiscal anchors in US GAAP filings."}
    grouped = defaultdict(lambda: defaultdict(list))
    for metric, rows in candidates.items():
        for r in rows:
            kind = "annual" if 330 <= r["days"] <= 400 else "quarter" if 60 <= r["days"] <= 120 else "instant" if not r["days"] else "ytd"
            if kind == "annual" and r["end"] not in anchors:
                continue
            if kind == "ytd":
                continue
            period = str(date.fromisoformat(r["end"]).year) if kind == "annual" else fiscal_label(r["end"], anchors)
            if not period:
                continue
            grouped[period][metric].append((r, kind))
            if kind == "instant" and r["end"] in anchors:
                grouped[str(date.fromisoformat(r["end"]).year)][metric].append((r, kind))
    periods = []
    for period, metrics in grouped.items():
        statements = {"income": [], "balance": [], "cash_flow": []}
        flow_ends = [r["end"] for choices in metrics.values() for r, k in choices if k != "instant"]
        if not flow_ends:
            continue
        target_end = max(flow_ends)
        ends, starts = [], []
        for metric, (statement, label, _) in METRICS.items():
            choices = metrics.get(metric, [])
            # A fiscal quarter must never acquire an annual flow.
            choices = [(r, k) for r, k in choices if r["end"] == target_end and (k == "instant" or (k == "annual") == ("-Q" not in period))]
            if not choices:
                continue
            rank = min(r["rank"] for r, _ in choices)
            choices = [(r, k) for r, k in choices if r["rank"] == rank]
            r, kind = max(choices, key=lambda pair: (pair[0].get("filed", ""), pair[0]["end"]))
            # Equally recent contradictory facts are not resolved arbitrarily.
            conflicts = [other for other, _ in choices if other.get("filed") == r.get("filed") and other["end"] == r["end"] and other.get("start") == r.get("start")]
            if len({x["value"] for x in conflicts}) != 1:
                continue
            ends.append(r["end"])
            if r.get("start"):
                starts.append(r["start"])
            cik = str(payload.get("cik", "")).lstrip("0")
            acc = r["accn"].replace("-", "")
            fact = {"concept": metric, "label": label, "value": str(r["value"]), "unit": r["unit"],
                    "currency": "USD" if "USD" in r["unit"] else None, "scale": "1", "accounting_basis": "US GAAP",
                    "period_start": r.get("start"), "period_end": r["end"], "period_kind": kind,
                    "evidence": [{"accession": r["accn"], "source_url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/",
                                  "concept": r["concept"], "method": "SEC companyfacts", "parser_version": "1",
                                  "filed_at": r.get("filed")}], "null_reason": None}
            if r.get("derived_from"):
                fact["formula"] = "Cumulative flow minus preceding cumulative flow for the same fiscal-year start and unit."
                fact["evidence"] = [{
                    "accession": source["accn"], "source_url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{source['accn'].replace('-', '')}/",
                    "concept": source["concept"], "method": "SEC companyfacts, derived quarter", "parser_version": "1",
                    "period_start": source.get("start"), "period_end": source["end"], "reported_value": str(source["value"]),
                    "filed_at": source.get("filed")
                } for source in r["derived_from"]]
            statements[statement].append(fact)
        # Instant-only quarter groupings are not reported quarterly statements.
        if not starts:
            continue
        periods.append({"period": period, "period_end": max(ends), "period_start": min(starts),
                        "period_kind": "quarter" if "-Q" in period else "annual", "statements": statements})
    periods.sort(key=lambda p: (p["period_end"], p["period_kind"]))
    if lookback:
        years = [p for p in periods if p["period_kind"] == "annual"][-lookback["annual"]:]
        quarters = [p for p in periods if p["period_kind"] == "quarter"][-lookback["quarterly"]:]
        periods = sorted(years + quarters, key=lambda p: (p["period_end"], p["period_kind"]))
    elif requested and requested != ["latest"]:
        periods = [p for p in periods if p["period"] in requested]
    else:
        periods = periods[-1:]
    present = {f["concept"] for p in periods for fs in p["statements"].values() for f in fs}
    return periods, {"missing_metrics": sorted(set(METRICS) - present),
                     "note": "Standalone quarters use reported facts or explicit differences of compatible cumulative monetary flows. EPS is never subtracted."}
