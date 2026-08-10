"""Offline unit tests — no network needed. Run: pytest -q"""
import pandas as pd

from src.emailer import build_context, render
from src.fundamentals import Fundamentals, fmt_money, fmt_ratio
from src.main import apply_filters
from src.prices import find_fallers


def _moves():
    return pd.DataFrame(
        [
            {"ticker": "AAA", "prev_close": 1.00, "close": 0.80, "pct_change": -20.0, "volume": 100, "date": "2026-08-07"},
            {"ticker": "BBB", "prev_close": 2.00, "close": 1.68, "pct_change": -16.0, "volume": 200, "date": "2026-08-07"},
            {"ticker": "CCC", "prev_close": 3.00, "close": 2.70, "pct_change": -10.0, "volume": 300, "date": "2026-08-07"},
            {"ticker": "DDD", "prev_close": 4.00, "close": 4.40, "pct_change": 10.0, "volume": 400, "date": "2026-08-07"},
        ]
    )


def _universe():
    return pd.DataFrame(
        [
            {"ticker": "AAA", "name": "Alpha Ltd", "sector": "Materials", "market_cap_listed": 5e7},
            {"ticker": "BBB", "name": "Beta Ltd", "sector": "Software & Services", "market_cap_listed": 2e8},
            {"ticker": "CCC", "name": "Gamma Ltd", "sector": "Energy", "market_cap_listed": 1e8},
        ]
    )


def test_threshold_filter():
    fallers = find_fallers(_moves(), 15.0)
    assert list(fallers["ticker"]) == ["AAA", "BBB"]  # sorted worst first
    assert fallers.iloc[0]["pct_change"] == -20.0


def test_sector_exclusion_on():
    cfg = {"exclude_sectors": {"enabled": True, "sectors": ["Materials"]}, "min_market_cap_aud": 0}
    filtered, excluded = apply_filters(find_fallers(_moves(), 15.0), _universe(), cfg)
    assert list(filtered["ticker"]) == ["BBB"]
    assert excluded == ["Materials"]


def test_sector_exclusion_off():
    cfg = {"exclude_sectors": {"enabled": False, "sectors": ["Materials"]}, "min_market_cap_aud": 0}
    filtered, excluded = apply_filters(find_fallers(_moves(), 15.0), _universe(), cfg)
    assert list(filtered["ticker"]) == ["AAA", "BBB"]
    assert excluded == []


def test_min_market_cap():
    cfg = {"exclude_sectors": {"enabled": False, "sectors": []}, "min_market_cap_aud": 1e8}
    filtered, _ = apply_filters(find_fallers(_moves(), 15.0), _universe(), cfg)
    assert list(filtered["ticker"]) == ["BBB"]


def test_ratio_formatting():
    assert fmt_money(1_500_000_000) == "$1.5B"
    assert fmt_money(23_400_000) == "$23.4M"
    assert fmt_money(None) == "—"
    assert fmt_ratio(4.26) == "4.3×"
    assert fmt_ratio(None) == "—"


def test_ev_ratios():
    f = Fundamentals(enterprise_value=100.0, revenue=25.0, ebit=10.0)
    f.ev_rev = f.enterprise_value / f.revenue
    f.ev_ebit = f.enterprise_value / f.ebit
    assert f.ev_rev == 4.0 and f.ev_ebit == 10.0


def _row(ticker="AAA", pct=-20.0):
    return {
        "ticker": ticker, "name": "Alpha Ltd", "sector": "Materials",
        "pct_change": pct, "pct_str": f"{pct:.1f}%",
        "prev_close_str": "$1", "close_str": "$0.8", "volume_str": "100",
        "reason": "Capital raising at a deep discount announced pre-open.",
        "confidence": "high", "description": "Gold explorer in WA.",
        "metrics": [("Mkt cap", "$50.0M"), ("EV", "$40.0M"), ("EV / Rev", "—"), ("EV / EBIT", "—")],
        "lake_sources": "",
    }


def test_template_renders_fallers():
    ctx = build_context([_row()], scanned=2000, threshold=15.0, excluded=["Energy"], report_date="Fri 07 Aug 2026")
    html = render(ctx)
    assert "Alpha Ltd" in html and "-20.0%" in html
    assert "sectors muted: Energy" in html
    assert ctx["fallers"][0]["bar_pct"] == 40  # 20% fall → 40% bar


def test_template_renders_quiet_day():
    ctx = build_context([], scanned=2000, threshold=15.0, excluded=[], report_date="Fri 07 Aug 2026")
    html = render(ctx)
    assert "A quiet close." in html


def test_bar_capped_at_100():
    ctx = build_context([_row(pct=-80.0)], scanned=10, threshold=15.0, excluded=[], report_date="x")
    assert ctx["fallers"][0]["bar_pct"] == 100
