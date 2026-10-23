from datetime import date, datetime
from decimal import Decimal
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Lookback(StrictModel):
    annual: int = Field(default=5, ge=1, le=5)
    quarterly: int = Field(default=12, ge=1, le=12)


class ExtractionRequest(StrictModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "examples": [{"identifier": "1018724", "periods": ["2025-Q4"], "refresh": False}]
    })
    identifier: str = Field(
        min_length=1, max_length=32, pattern=r"^[A-Za-z0-9.\-]+$",
        description="Ticker, case-insensitive, or numeric SEC CIK. Explicit retrieval can resolve new tickers through SEC; source configuration or access errors return 503.",
        examples=["1018724", "AMZN"],
    )
    periods: list[str] = Field(
        default_factory=list, max_length=20,
        description="Up to 20 distinct periods: YYYY, YYYY-Q1 through YYYY-Q4, or latest alone. Years must be 1900 through 2099. Omit or pass [] for latest.",
        examples=[["2025-Q4"], ["latest"]],
    )
    refresh: bool = Field(
        default=False,
        description="Request extraction even if stored data is fresh. An identical active job is still reused.",
    )

    lookback: Lookback | None = Field(default=None, description="Discover the most recent completed fiscal annual/quarterly periods. Mutually exclusive with periods.")

    @model_validator(mode="after")
    def validate_period_selection(self):
        if self.lookback and self.periods:
            raise ValueError("lookback and explicit periods are mutually exclusive")
        if len(set(self.periods)) != len(self.periods):
            raise ValueError("periods must not contain duplicates")
        if "latest" in self.periods and len(self.periods) > 1:
            raise ValueError("latest must be requested by itself")
        for period in self.periods:
            if not (period == "latest" or re.fullmatch(r"(?:20|19)\d{2}(?:-Q[1-4])?", period)):
                raise ValueError("periods must use latest, YYYY, or YYYY-QN")
        return self


class ExtractionAccepted(StrictModel):
    status: Literal["accepted", "already_fresh"]
    job_id: str | None = None
    status_url: str | None = None
    dataset_versions: list[str] = Field(default_factory=list)


class JobResponse(StrictModel):
    job_id: str
    status: str
    progress: dict[str, Any] = Field(default_factory=dict)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    published_datasets: list[dict[str, Any]] = Field(default_factory=list)
    published_market_data: list[dict[str, Any]] = Field(default_factory=list)
    kind: str = "financials"


class Fact(StrictModel):
    label: str
    value: Decimal | None
    currency: str | None = None
    scale: str | None = None
    unit: str | None = None
    accounting_basis: str | None = None
    period_start: date | None = None
    period_end: date
    period_kind: Literal["quarter", "ytd", "annual", "instant"]
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    null_reason: str | None = None

    @model_validator(mode="after")
    def explain_missing_value(self):
        if self.value is None and not self.null_reason:
            raise ValueError("a null fact requires a reason")
        return self


class Breakdown(StrictModel):
    dimension: Literal["operating_segment", "product_service", "geographic"]
    label: str
    revenue: Fact | None = None
    profit_measure: str | None = None
    profit: Fact | None = None
    comparable: bool = True
    evidence: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def never_infer_profit(self):
        if self.profit is not None and not self.profit_measure:
            raise ValueError("reported profit requires its disclosed measure name")
        return self


class MarketExtractionRequest(StrictModel):
    identifier: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9.\-]+$", description="Stored ticker or CIK; new identifiers resolve through the SEC directory.")
    refresh: bool = Field(default=False, description="Retrieve again even if the stored daily snapshot is fresh.")


class Company(StrictModel):
    ticker: str | None
    cik: str | None
    name: str


class StatementFact(Fact):
    concept: str
    formula: str | None = None


class FinancialPeriod(StrictModel):
    period: str
    period_end: date
    period_start: date | None = None
    period_kind: str | None = None
    statements: dict[str, list[StatementFact]] = Field(default_factory=dict)


class BreakdownPeriod(StrictModel):
    period: str
    period_end: date
    period_kind: str | None = None
    items: list[Breakdown] = Field(default_factory=list)


class DatasetResponse(StrictModel):
    company: Company
    dataset_version: str
    source_checked_at: datetime
    published_at: datetime
    stale: bool
    missing_periods: list[str]
    available_periods: list[dict[str, Any]] = Field(default_factory=list)


class FinancialsResponse(DatasetResponse):
    periods: list[FinancialPeriod]
    completeness: dict[str, Any] = Field(default_factory=dict)
    latest_stored_period: str | None = None


class BreakdownsResponse(DatasetResponse):
    periods: list[BreakdownPeriod]


class PriceBar(StrictModel):
    time: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    adjusted_close: float | None = None
    dividend: float = 0
    split: float = 0


class MarketResponse(StrictModel):
    ticker: str | None = Field(default=None, description="Provider ticker stored with the price snapshot. Company identity remains available under company.")
    company: Company
    dataset_version: str
    source_checked_at: datetime
    published_at: datetime
    stale: bool
    provider: str
    currency: str | None
    timezone: str
    adjustment: str
    coverage_start: str
    coverage_end: str
    bars: list[PriceBar]
    source_url: str | None = None
    retrieved_at: str | None = None
