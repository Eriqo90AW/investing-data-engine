from pathlib import Path
from decimal import Decimal
import json
import pytest
from investing_data_engine.services.financials import normalize_companyfacts
from investing_data_engine.schemas import FinancialPeriod

# Explicit source totals in USD, read from each recorded response.
@pytest.mark.parametrize("ticker,revenue,net_income",[
    ("amzn",716924000000,77670000000),
    ("msft",281724000000,101832000000),
    ("nvda",130497000000,72880000000),
    ("jpm",182447000000,57048000000),
    ("wmt",674538000000,19436000000),
])
def test_reported_fiscal_2025_totals(ticker,revenue,net_income):
    payload=json.loads((Path(__file__).parent/"fixtures"/"sec"/(ticker+".json")).read_text())
    periods,_=normalize_companyfacts(payload,requested=["2025"])
    assert len(periods)==1
    FinancialPeriod.model_validate(periods[0])
    facts={f["concept"]:f for fs in periods[0]["statements"].values() for f in fs}
    assert Decimal(facts["revenue"]["value"])==revenue
    assert Decimal(facts["net_income"]["value"])==net_income
    assert facts["revenue"]["evidence"][0]["accession"]
    assert facts["revenue"]["period_kind"]=="annual"
    assert facts["revenue"]["unit"]=="USD"

def test_amazon_ttm_disclosures_do_not_become_new_fiscal_years():
    payload=json.loads((Path(__file__).parent/"fixtures"/"sec"/"amzn.json").read_text())
    periods,_=normalize_companyfacts(payload,lookback={"annual":5,"quarterly":12})
    annual=[p for p in periods if p["period_kind"]=="annual"]
    assert all(p["period_end"].endswith("-12-31") for p in annual)
