"""Fundamentals for flagged stocks: market cap, EV, EV/Revenue, EV/EBIT, summary."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field



log = logging.getLogger(__name__)

# Yahoo throttles bursts of per-ticker info requests (the batched price download
# alone can trip it), and once tripped every call fails instantly. Waiting long
# enough usually clears it, so rate-limited fetches retry with these pauses.
RETRY_DELAYS = (10, 30, 60)  # seconds before attempt 2, 3, 4

_sleep = time.sleep  # patched in tests

# Circuit breaker: only the first rate-limited ticker pays the full retry wait.
# If Yahoo still refuses after RETRY_DELAYS, later tickers get one attempt each
# (cheap — a limited call fails in ~50ms) instead of re-waiting per ticker.
# Any successful fetch re-arms the retries.
_limiter_exhausted = False


@dataclass
class Fundamentals:
    market_cap: float | None = None
    enterprise_value: float | None = None
    revenue: float | None = None
    ebit: float | None = None
    ev_rev: float | None = None
    ev_ebit: float | None = None
    summary: str = ""
    website: str = ""
    financial_currency: str = ""
    raw: dict = field(default_factory=dict)
    # True when the Yahoo request itself failed (rate limit, network, …) — as
    # opposed to a fine response that simply carries no revenue figure. The
    # revenue screen treats the two differently.
    fetch_failed: bool = False


def _ebit_from_statements(t) -> float | None:
    try:
        inc = t.income_stmt  # annual, most recent column first
        if inc is None or inc.empty:
            return None
        for row in ("EBIT", "Operating Income"):
            if row in inc.index:
                v = inc.loc[row].dropna()
                if len(v):
                    return float(v.iloc[0])
    except Exception:  # noqa: BLE001
        pass
    return None


def _is_rate_limit(e: Exception) -> bool:
    s = str(e).lower()
    return "rate limit" in s or "too many requests" in s or "429" in s


def _fetch(ticker: str, f: Fundamentals) -> None:
    import yfinance as yf

    t = yf.Ticker(f"{ticker}.AX")
    info = t.info or {}
    f.raw = info
    f.market_cap = info.get("marketCap")
    f.enterprise_value = info.get("enterpriseValue")
    f.revenue = info.get("totalRevenue")
    f.summary = (info.get("longBusinessSummary") or "").strip()
    f.website = info.get("website") or ""
    f.financial_currency = (info.get("financialCurrency") or "").strip().upper()
    f.ebit = _ebit_from_statements(t)

    ev = f.enterprise_value
    if ev and f.revenue and f.revenue > 0:
        f.ev_rev = ev / f.revenue
    if ev and f.ebit and f.ebit > 0:
        f.ev_ebit = ev / f.ebit


def get_fundamentals(ticker: str) -> Fundamentals:
    global _limiter_exhausted
    attempts = 1 if _limiter_exhausted else 1 + len(RETRY_DELAYS)
    for attempt in range(attempts):
        f = Fundamentals()
        try:
            _fetch(ticker, f)
            _limiter_exhausted = False
            return f
        except Exception as e:  # noqa: BLE001
            if _is_rate_limit(e) and attempt + 1 < attempts:
                delay = RETRY_DELAYS[attempt]
                log.warning("Fundamentals rate-limited for %s — retrying in %ds", ticker, delay)
                _sleep(delay)
                continue
            if _is_rate_limit(e):
                _limiter_exhausted = True
            log.warning("Fundamentals failed for %s: %s", ticker, e)
            f.fetch_failed = True
            return f
    return Fundamentals(fetch_failed=True)  # unreachable; attempts >= 1


def fmt_money(v: float | None) -> str:
    if v is None:
        return "—"
    a = abs(v)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if a >= div:
            return f"${v / div:,.1f}{unit}"
    return f"${v:,.0f}"


def fmt_ratio(v: float | None) -> str:
    return "—" if v is None else f"{v:,.1f}×"
