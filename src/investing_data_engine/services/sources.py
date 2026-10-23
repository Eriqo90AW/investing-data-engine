"""Bounded source access. Only explicit extraction calls these functions."""
import asyncio
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
from urllib.parse import urljoin, urlparse
import httpx

from ..config import get_settings

ISSUER_HOSTS = {
 "AMZN": {"ir.aboutamazon.com", "s2.q4cdn.com"},
 "MSFT": {"www.microsoft.com"},
 "NVDA": {"investor.nvidia.com", "s201.q4cdn.com"},
 "JPM": {"www.jpmorganchase.com"},
 "WMT": {"stock.walmart.com", "corporate.walmart.com"},
}
SEC_HOSTS = {"www.sec.gov", "data.sec.gov", "www.sec.gov"}
MAX_BYTES = 25 * 1024 * 1024
_source_lock = asyncio.Lock()
_last_request = 0.0


class SourceError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def validate_url(url: str, ticker: str = "") -> str:
    p = urlparse(url)
    allowed = SEC_HOSTS | ISSUER_HOSTS.get(ticker, set())
    if p.scheme != "https" or p.hostname not in allowed or p.username or p.password or p.port not in (None, 443):
        raise SourceError("source_not_allowed", "Source URL is outside verified HTTPS hosts.")
    return url


async def download(url: str, ticker: str = "") -> bytes:
    settings = get_settings()
    if not settings.sec_contact_email:
        raise SourceError("sec_contact_required", "Configure SEC_CONTACT_EMAIL before retrieving filings.")
    global _last_request
    async with _source_lock:
        loop = asyncio.get_running_loop()
        async with httpx.AsyncClient(timeout=40, follow_redirects=False,
                                     headers={"User-Agent": f"InvestingResearch/1.0 {settings.sec_contact_email}"}) as client:
            for attempt in range(4):
                await asyncio.sleep(max(0, .25 - (loop.time() - _last_request)))
                _last_request = loop.time()
                validate_url(url, ticker)
                async with client.stream("GET", url) as r:
                    if r.status_code in (301, 302, 303, 307, 308):
                        url = validate_url(urljoin(url, r.headers.get("location", "")), ticker)
                        continue
                    if r.status_code in (429, 503) and attempt < 3:
                        await asyncio.sleep(2 ** (attempt + 1))
                        continue
                    r.raise_for_status()
                    body = bytearray()
                    async for chunk in r.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_BYTES:
                            raise SourceError("document_too_large", "Document exceeds the 25 MB limit.")
                    return bytes(body)
    raise SourceError("source_unavailable", "Source redirects or rate limits exceeded the retry limit.")


async def source_json(url: str):
    import json
    return json.loads(await download(url))


_directory = None
_directory_at = None


async def discover(identifier: str) -> dict:
    global _directory, _directory_at
    now = datetime.now(timezone.utc)
    if identifier.isdigit():
        if not 1 <= len(identifier) <= 10:
            raise SourceError("company_not_found", "A CIK has 1 to 10 digits.")
        cik = identifier.zfill(10)
        data = await source_json(f"https://data.sec.gov/submissions/CIK{cik}.json")
        return {"cik": cik, "ticker": next(iter(data.get("tickers", [])), None), "name": data["name"]}
    if _directory is None or (now - _directory_at).total_seconds() > 86400:
        _directory = await source_json("https://www.sec.gov/files/company_tickers.json")
        _directory_at = now
    item = next((x for x in _directory.values() if x["ticker"].upper() == identifier.upper()), None)
    if not item:
        raise SourceError("company_not_found", "Ticker was not found in the SEC company directory.")
    return {"cik": str(item["cik_str"]).zfill(10), "ticker": item["ticker"], "name": item["title"]}


def store_document(body: bytes, suffix: str) -> tuple[Path, str]:
    digest = sha256(body).hexdigest()
    root = get_settings().document_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / (digest + suffix)
    if not path.exists():
        temporary = root / (digest + "." + uuid4().hex + ".tmp")
        temporary.write_bytes(body)
        temporary.replace(path)
    return path, digest
