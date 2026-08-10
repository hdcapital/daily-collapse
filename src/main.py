"""ASX Crash Scanner — daily post-close pipeline.

Usage:
    python -m src.main                 # full run, sends email
    python -m src.main --dry-run       # renders out/report.html, no email
    python -m src.main --limit 50      # scan only first 50 tickers (fast test)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from . import analysis, datalake, emailer, prices, universe
from .fundamentals import fmt_money, fmt_ratio, get_fundamentals

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

SYD = ZoneInfo("Australia/Sydney")


def load_config() -> dict:
    with open(Path(__file__).parent.parent / "config.yaml") as f:
        return yaml.safe_load(f)


def apply_filters(fallers, uni, cfg):
    merged = fallers.merge(uni, on="ticker", how="left")
    merged["sector"] = merged["sector"].fillna("Unknown")
    merged["name"] = merged["name"].fillna(merged["ticker"])

    excluded: list[str] = []
    ex = cfg.get("exclude_sectors", {})
    if ex.get("enabled") and ex.get("sectors"):
        excluded = [s.strip() for s in ex["sectors"]]
        before = len(merged)
        merged = merged[~merged["sector"].isin(excluded)]
        log.info("Sector exclusion removed %d stocks (%s)", before - len(merged), excluded)

    min_cap = float(cfg.get("min_market_cap_aud", 0) or 0)
    if min_cap > 0 and "market_cap_listed" in merged:
        merged = merged[(merged["market_cap_listed"].isna()) | (merged["market_cap_listed"] >= min_cap)]

    return merged.reset_index(drop=True), excluded


def enrich(row, cfg) -> dict:
    t = row["ticker"]
    log.info("Enriching %s (%.1f%%)", t, row["pct_change"])
    f = get_fundamentals(t)
    lake = datalake.gather_context(t, cfg)
    a = analysis.analyse(
        ticker=t,
        name=row["name"],
        pct_change=row["pct_change"],
        date=row["date"],
        yf_summary=f.summary,
        lake_context=lake,
        cfg=cfg,
    )
    mcap = f.market_cap if f.market_cap else row.get("market_cap_listed")
    return {
        "ticker": t,
        "name": row["name"],
        "sector": row["sector"],
        "pct_change": row["pct_change"],
        "pct_str": f"{row['pct_change']:.1f}%",
        "prev_close_str": f"${row['prev_close']:,.3f}".rstrip("0").rstrip("."),
        "close_str": f"${row['close']:,.3f}".rstrip("0").rstrip("."),
        "volume_str": f"{row['volume']:,}",
        "reason": a.reason,
        "confidence": a.confidence,
        "description": a.description or "No description available.",
        "metrics": [
            ("Mkt cap", fmt_money(mcap)),
            ("EV", fmt_money(f.enterprise_value)),
            ("EV / Rev", fmt_ratio(f.ev_rev)),
            ("EV / EBIT", fmt_ratio(f.ev_ebit)),
        ],
        "lake_sources": ", ".join(h["source"] for h in lake[:3]),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="render HTML, don't email")
    ap.add_argument("--limit", type=int, default=0, help="cap universe size for testing")
    args = ap.parse_args()

    cfg = load_config()
    now_syd = datetime.now(SYD)
    report_date = now_syd.strftime("%a %d %b %Y")

    uni = universe.fetch_asx_universe()
    if args.limit:
        uni = uni.head(args.limit)

    moves = prices.daily_moves(uni["ticker"].tolist())
    fallers = prices.find_fallers(moves, cfg["threshold_pct"])
    log.info("%d fallers beyond -%s%%", len(fallers), cfg["threshold_pct"])

    filtered, excluded = apply_filters(fallers, uni, cfg)
    total_fallers = len(filtered)
    cap = int(cfg.get("max_stocks_in_email", 40))
    if total_fallers > cap:
        log.info("Capping the report at %d of %d flagged stocks (worst first)", cap, total_fallers)
    filtered = filtered.head(cap)

    rows = [enrich(r, cfg) for _, r in filtered.iterrows()]

    ctx = emailer.build_context(
        rows,
        scanned=len(moves),
        threshold=cfg["threshold_pct"],
        excluded=excluded,
        report_date=report_date,
        total_fallers=total_fallers,
    )
    html = emailer.render(ctx)

    out = Path("out")
    out.mkdir(exist_ok=True)
    (out / "report.html").write_text(html)
    log.info("Report written to out/report.html")

    if args.dry_run:
        log.info("Dry run — email not sent.")
        return 0

    n = total_fallers
    subject = (
        f"ASX Fall Wire · {report_date} · {n} stock{'s' if n != 1 else ''} down >{cfg['threshold_pct']:g}%"
        if n
        else f"ASX Fall Wire · {report_date} · quiet close"
    )
    if not os.environ.get("SMTP_HOST"):
        log.warning("SMTP secrets not set — skipping send (report still in out/report.html)")
        return 0
    emailer.send(html, subject)
    return 0


if __name__ == "__main__":
    sys.exit(main())
