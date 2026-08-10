"""Fundamentals for flagged stocks: market cap, EV, EV/Revenue, EV/EBIT, summary."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field



log = logging.getLogger(__name__)


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
    raw: dict = field(default_factory=dict)


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


def get_fundamentals(ticker: str) -> Fundamentals:
    f = Fundamentals()
    try:
        import yfinance as yf

        t = yf.Ticker(f"{ticker}.AX")
        info = t.info or {}
        f.raw = info
        f.market_cap = info.get("marketCap")
        f.enterprise_value = info.get("enterpriseValue")
        f.revenue = info.get("totalRevenue")
        f.summary = (info.get("longBusinessSummary") or "").strip()
        f.website = info.get("website") or ""
        f.ebit = _ebit_from_statements(t)
        if f.ebit is None and info.get("ebitda"):
            # last-resort proxy so the field isn't blank; flagged as EBITDA in email
            f.ebit = None

        ev = f.enterprise_value
        if ev and f.revenue and f.revenue > 0:
            f.ev_rev = ev / f.revenue
        if ev and f.ebit and f.ebit > 0:
            f.ev_ebit = ev / f.ebit
    except Exception as e:  # noqa: BLE001
        log.warning("Fundamentals failed for %s: %s", ticker, e)
    return f


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
