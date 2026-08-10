"""Fetch the official ASX listed-companies directory.

Primary source: the ASX company-directory CSV served via Markit Digital
(the same file behind asx.com.au "Listed companies" download button).
Fallback: the legacy static CSV.
"""
from __future__ import annotations

import io
import logging

import pandas as pd
import requests

log = logging.getLogger(__name__)

PRIMARY_URL = (
    "https://asx.api.markitdigital.com/asx-research/1.0/companies/directory/file"
    "?access_token=83ff96335c2d45a094df02a206a39ff4"
)
FALLBACK_URL = "https://www.asx.com.au/asx/research/ASXListedCompanies.csv"

HEADERS = {"User-Agent": "Mozilla/5.0 (asx-crash-scanner; +github actions)"}


def _fetch_csv(url: str) -> pd.DataFrame:
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    text = r.text
    # Legacy file has 2 junk header lines before the real header
    if "ASX code" not in text.splitlines()[0]:
        lines = text.splitlines()
        for i, line in enumerate(lines[:5]):
            if "ASX code" in line or "ASX Code" in line:
                text = "\n".join(lines[i:])
                break
    return pd.read_csv(io.StringIO(text))


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    cols = {c.lower().strip(): c for c in df.columns}

    def pick(*names: str) -> str | None:
        for n in names:
            if n in cols:
                return cols[n]
        return None

    code = pick("asx code", "asx code ")
    name = pick("company name")
    sector = pick("gics industry group", "gics_industry_group")
    mcap = pick("market cap", "market cap ")

    out = pd.DataFrame(
        {
            "ticker": df[code].astype(str).str.strip().str.upper(),
            "name": df[name].astype(str).str.strip(),
            "sector": df[sector].astype(str).str.strip() if sector else "Unknown",
            "market_cap_listed": pd.to_numeric(df[mcap], errors="coerce") if mcap else None,
        }
    )
    out = out[out["ticker"].str.fullmatch(r"[A-Z0-9]{2,6}")]
    return out.drop_duplicates(subset="ticker").reset_index(drop=True)


def fetch_asx_universe() -> pd.DataFrame:
    """Return DataFrame with columns: ticker, name, sector, market_cap_listed."""
    for url in (PRIMARY_URL, FALLBACK_URL):
        try:
            df = _normalise(_fetch_csv(url))
            if len(df) > 500:  # sanity check — the ASX has ~2000 listings
                log.info("Universe: %d companies from %s", len(df), url.split("?")[0])
                return df
        except Exception as e:  # noqa: BLE001
            log.warning("Universe source failed (%s): %s", url.split("?")[0], e)
    raise RuntimeError("Could not fetch the ASX official list from any source")
