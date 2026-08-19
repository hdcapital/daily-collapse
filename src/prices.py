"""Daily price moves for the whole ASX universe via Yahoo Finance (.AX suffix)."""
from __future__ import annotations

import logging

import pandas as pd


log = logging.getLogger(__name__)

BATCH = 250  # tickers per yfinance batch request

COLUMNS = ["ticker", "prev_close", "close", "pct_change", "volume", "date"]

# On a normal day ~20% of the universe is halted or untraded and legitimately
# lags the latest session. When nearly *everything* lags, the problem is the
# data, not the market: between Sydney midnight and Yahoo's EOD consolidation
# the just-completed session's bar is missing for almost every ticker (observed
# 2026-08-19: 1828 of 1832 tickers had no bar for the session at 5:32am and
# still at 6:43am Sydney). Reporting "quiet day" off that would be a lie.
MAX_STALE_FRACTION = 0.8


def daily_moves(tickers: list[str]) -> pd.DataFrame:
    """Return DataFrame [ticker, prev_close, close, pct_change, volume, date] for the
    latest trading day. pct_change is negative for falls (e.g. -18.2)."""
    import yfinance as yf

    frames: list[pd.DataFrame] = []
    yahoo = [f"{t}.AX" for t in tickers]

    for i in range(0, len(yahoo), BATCH):
        chunk = yahoo[i : i + BATCH]
        try:
            data = yf.download(
                chunk,
                period="7d",
                interval="1d",
                group_by="ticker",
                auto_adjust=False,
                threads=True,
                progress=False,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Batch %d download failed: %s", i // BATCH, e)
            continue
        frames.append(_extract(data, chunk))
        log.info("Prices: %d / %d tickers fetched", min(i + BATCH, len(yahoo)), len(yahoo))

    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)
    out = out.reindex(columns=COLUMNS)
    if out.empty:
        raise RuntimeError("No price data retrieved for any ticker")

    out = _latest_session_only(out)
    log.info("Prices resolved for %d tickers", len(out))
    return out


def _latest_session_only(moves: pd.DataFrame) -> pd.DataFrame:
    """Keep only tickers whose most recent bar is the latest session in the batch.

    Yahoo returns a stock's last *traded* bar, so a name that has been halted or
    simply hasn't printed for days would otherwise contribute a move from an
    older session — and get reported as if it happened today.
    """
    latest = moves["date"].max()
    fresh = moves[moves["date"] == latest]
    stale = len(moves) - len(fresh)
    if stale / len(moves) > MAX_STALE_FRACTION:
        raise RuntimeError(
            f"Only {len(fresh)} of {len(moves)} tickers have a bar for {latest} — "
            "price data looks incomplete (Yahoo EOD gap?), refusing to report a quiet day on it"
        )
    if stale:
        log.info("Dropped %d ticker(s) with no bar for %s (halted or untraded)", stale, latest)
    return fresh.reset_index(drop=True)


def _extract(data: pd.DataFrame, chunk: list[str]) -> pd.DataFrame:
    rows = []
    multi = isinstance(data.columns, pd.MultiIndex)
    for y in chunk:
        try:
            if multi:
                if y not in data.columns.get_level_values(0):
                    continue
                df = data[y]
            else:
                df = data
            closes = df["Close"].dropna()
            if len(closes) < 2:
                continue
            close, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
            if prev <= 0:
                continue
            last_ts = closes.index[-1]
            rows.append(
                {
                    "ticker": y.removesuffix(".AX"),
                    "prev_close": prev,
                    "close": close,
                    "pct_change": (close / prev - 1.0) * 100.0,
                    "volume": _volume_at(df, last_ts),
                    "date": last_ts.date().isoformat(),
                }
            )
        except Exception:  # noqa: BLE001
            continue
    return pd.DataFrame(rows, columns=COLUMNS)


def _volume_at(df: pd.DataFrame, ts) -> int:
    """Volume printed on the session we computed the move for (0 if absent)."""
    try:
        v = df["Volume"].get(ts)
        return 0 if v is None or pd.isna(v) else int(v)
    except Exception:  # noqa: BLE001
        return 0


def find_fallers(moves: pd.DataFrame, threshold_pct: float) -> pd.DataFrame:
    """Stocks whose fall exceeds threshold_pct (a positive number like 15.0).

    Strictly greater, matching the wording in config.yaml and the email.
    """
    if moves.empty:
        return moves.reindex(columns=COLUMNS).iloc[0:0]
    fallers = moves[moves["pct_change"] < -abs(threshold_pct)].copy()
    return fallers.sort_values("pct_change").reset_index(drop=True)
