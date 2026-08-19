"""ASX Crash Scanner — daily post-close pipeline.

Usage:
    python -m src.main                 # full run, sends email
    python -m src.main --dry-run       # renders out/report.html + meta.json, no email
    python -m src.main --send-only     # emails a report saved by a previous run
    python -m src.main --limit 50      # scan only first 50 tickers (fast test)

In CI the pipeline is split: the scan runs in the Sydney evening (while Yahoo
still serves the session's bar from its live feed) with --dry-run, and a
separate 5am job delivers the saved report with --send-only. Between Sydney
midnight and Yahoo's EOD consolidation the completed bar is missing for almost
the whole ASX, so the scan cannot run at delivery time.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import analysis, datalake, emailer, prices, universe
from .fundamentals import fmt_money, fmt_ratio, get_fundamentals

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")


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


def screen_by_revenue(candidates: list[tuple], cfg: dict) -> tuple[list[tuple], int]:
    """Drop fallers whose revenue is below `min_revenue_aud`.

    `candidates` is [(row, Fundamentals)]. Returns the survivors and how many
    were hidden, so the email can disclose the screen rather than silently
    showing a shorter list.
    """
    min_rev = float(cfg.get("min_revenue_aud", 0) or 0)
    if min_rev <= 0:
        return candidates, 0

    include_unknown = bool(cfg.get("include_unknown_revenue", False))
    kept, hidden = [], 0
    for row, f in candidates:
        if f.fetch_failed:
            # The Yahoo request errored (rate limit, outage) — that is not the
            # same as "no revenue figure exists". A confirmed faller must not
            # vanish because enrichment failed, so assume it clears the bar.
            log.info("%s: fundamentals fetch failed — kept, revenue hurdle assumed met", row["ticker"])
            kept.append((row, f))
            continue
        if f.revenue is None:
            if include_unknown:
                kept.append((row, f))
            else:
                hidden += 1
                log.info("%s hidden: no revenue figure available", row["ticker"])
            continue
        # Yahoo reports in the company's own reporting currency. Nearly all ASX
        # names report AUD; the few that don't are compared on the raw figure,
        # which is noted rather than silently converted.
        if f.financial_currency and f.financial_currency != "AUD":
            log.info(
                "%s reports revenue in %s — compared against the AUD threshold unconverted",
                row["ticker"],
                f.financial_currency,
            )
        if f.revenue >= min_rev:
            kept.append((row, f))
        else:
            hidden += 1
    if hidden:
        log.info("Revenue screen hid %d stock(s) below $%.1fM", hidden, min_rev / 1e6)
    return kept, hidden


def enrich(row, f, cfg) -> dict:
    t = row["ticker"]
    log.info("Enriching %s (%.1f%%)", t, row["pct_change"])
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
        "highlights": a.highlights,
        "lowlights": a.lowlights,
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
    ap.add_argument("--send-only", action="store_true", help="email the report saved by a previous run")
    ap.add_argument("--limit", type=int, default=0, help="cap universe size for testing")
    args = ap.parse_args()

    if args.send_only:
        return send_saved_report()

    cfg = load_config()

    uni = universe.fetch_asx_universe()
    if args.limit:
        uni = uni.head(args.limit)

    moves = prices.daily_moves(uni["ticker"].tolist())
    # Date the report by the session it covers, not the wall clock — the
    # scheduled run fires at ~5am Sydney the morning after the close.
    report_date = datetime.strptime(moves["date"].max(), "%Y-%m-%d").strftime("%a %d %b %Y")
    fallers = prices.find_fallers(moves, cfg["threshold_pct"])
    log.info("%d fallers beyond -%s%%", len(fallers), cfg["threshold_pct"])

    filtered, excluded = apply_filters(fallers, uni, cfg)

    # Fundamentals are fetched before the revenue screen and the cap, so the AI
    # calls (the expensive part) are only spent on stocks that survive both.
    log.info("Fetching fundamentals for %d candidate(s)", len(filtered))
    candidates = [(row, get_fundamentals(row["ticker"])) for _, row in filtered.iterrows()]
    candidates, hidden_by_revenue = screen_by_revenue(candidates, cfg)

    total_fallers = len(candidates)
    cap = int(cfg.get("max_stocks_in_email", 40))
    if total_fallers > cap:
        log.info("Capping the report at %d of %d flagged stocks (worst first)", cap, total_fallers)

    rows = [enrich(row, f, cfg) for row, f in candidates[:cap]]

    ctx = emailer.build_context(
        rows,
        scanned=len(moves),
        threshold=cfg["threshold_pct"],
        excluded=excluded,
        report_date=report_date,
        total_fallers=total_fallers,
        hidden_by_revenue=hidden_by_revenue,
        min_revenue=float(cfg.get("min_revenue_aud", 0) or 0),
    )
    html = emailer.render(ctx)

    n = total_fallers
    subject = (
        f"ASX Fall Wire · {report_date} · {n} stock{'s' if n != 1 else ''} down >{cfg['threshold_pct']:g}%"
        if n
        else f"ASX Fall Wire · {report_date} · quiet close"
    )

    out = Path("out")
    out.mkdir(exist_ok=True)
    (out / "report.html").write_text(html)
    # Everything the later --send-only job needs to deliver this report.
    (out / "meta.json").write_text(
        json.dumps(
            {
                "subject": subject,
                "report_date": report_date,
                "total_fallers": total_fallers,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    )
    log.info("Report written to out/report.html")

    if args.dry_run:
        log.info("Dry run — email not sent.")
        return 0

    if not os.environ.get("SMTP_HOST"):
        log.warning("SMTP secrets not set — skipping send (report still in out/report.html)")
        return 0
    emailer.send(html, subject)
    return 0


# A saved report older than this is not this morning's report — refuse to send
# it rather than replay an old session's falls (e.g. after a skipped scan run).
MAX_REPORT_AGE_HOURS = 20


def send_saved_report() -> int:
    """Deliver the report a previous --dry-run scan wrote to out/.

    The 5am delivery job runs this after downloading the evening scan's
    artifact; the scan itself cannot run at that hour (see module docstring).
    """
    out = Path("out")
    try:
        html = (out / "report.html").read_text()
        meta = json.loads((out / "meta.json").read_text())
    except FileNotFoundError as e:
        raise RuntimeError(f"No saved report to send — run the scan first ({e})") from e

    generated = datetime.fromisoformat(meta["generated_at"])
    age_hours = (datetime.now(timezone.utc) - generated).total_seconds() / 3600
    if age_hours > MAX_REPORT_AGE_HOURS:
        raise RuntimeError(
            f"Saved report ({meta['report_date']}) is {age_hours:.0f}h old — refusing to send a stale report"
        )
    emailer.send(html, meta["subject"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
