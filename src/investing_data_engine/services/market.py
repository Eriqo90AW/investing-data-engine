"""Yahoo daily history, fetched in the worker only."""
from datetime import datetime, timezone
import math
from zoneinfo import ZoneInfo
from .sources import SourceError


def retrieve_market(ticker: str) -> dict:
    import yfinance as yf
    if not ticker:
        raise SourceError("ticker_required", "Market data requires a resolved ticker.")
    stock = yf.Ticker(ticker.replace(".", "-"))
    history = stock.history(period="5y", interval="1d", auto_adjust=False, back_adjust=False,
                            actions=True, repair=False, raise_errors=True, timeout=30)
    metadata = stock.history_metadata or {}
    local_now = datetime.now(timezone.utc).astimezone(ZoneInfo(metadata.get("exchangeTimezoneName") or "America/New_York"))
    bars = []
    for index, row in history.iterrows():
        if index.strftime("%Y-%m-%d") == local_now.date().isoformat() and (local_now.hour, local_now.minute) < (16, 15):
            continue
        values = {key: float(row[source]) for key, source in
                  (("open", "Open"), ("high", "High"), ("low", "Low"), ("close", "Close"), ("volume", "Volume"))}
        if not all(math.isfinite(x) for x in values.values()) or min(values["open"], values["high"], values["low"], values["close"]) <= 0 or values["volume"] < 0:
            continue
        if values["high"] < max(values["open"], values["close"], values["low"]) or values["low"] > min(values["open"], values["close"]):
            raise SourceError("invalid_market_data", "Source returned inconsistent OHLC values.")
        adjusted = row.get("Adj Close")
        bars.append({"time": index.strftime("%Y-%m-%d"), **values,
                     "adjusted_close": float(adjusted) if adjusted is not None and math.isfinite(float(adjusted)) else None,
                     "dividend": float(row.get("Dividends", 0)), "split": float(row.get("Stock Splits", 0))})
    bars = sorted({b["time"]: b for b in bars}.values(), key=lambda b: b["time"])
    if not bars:
        raise SourceError("market_data_not_available", "Yahoo returned no valid daily bars.")
    return {"provider": "Yahoo Finance via yfinance", "ticker": ticker,
            "currency": metadata.get("currency"), "timezone": metadata.get("exchangeTimezoneName", "America/New_York"),
            "adjustment": "Yahoo historical OHLC; auto_adjust=false, repair=false. Adjusted close stored separately. Current-session bars omitted before 16:15 exchange local time.",
            "source_url": f"https://finance.yahoo.com/quote/{ticker}/history/",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "coverage_start": bars[0]["time"], "coverage_end": bars[-1]["time"], "bars": bars}
