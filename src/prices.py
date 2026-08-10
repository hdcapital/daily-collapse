"""Daily price moves for the whole ASX universe via Yahoo Finance (.AX suffix)."""
from __future__ import annotations

import logging

import pandas as pd


log = logging.getLogger(__name__)

BATCH = 250  # tickers per yfinance batch request


def daily_moves(tickers: list[str]) -> pd.DataFrame:
    """Return DataFrame [ticker, prev_close, close, pct_change, volume] for the
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

    if not frames:
        raise RuntimeError("No price data retrieved")
    out = pd.concat(frames, ignore_index=True)
    log.info("Prices resolved for %d tickers", len(out))
    return out


def _extract(data: pd.DataFrame, chunk: list[str]) -> pd.DataFrame:
    rows = []
    multi = isinstance(data.columns, pd.MultiIndex)
    for y in chunk:
        try:
            df = data[y] if multi else data
            closes = df["Close"].dropna()
            if len(closes) < 2:
                continue
            close, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
            if prev <= 0:
                continue
            vol = df["Volume"].dropna()
            rows.append(
                {
                    "ticker": y.removesuffix(".AX"),
                    "prev_close": prev,
                    "close": close,
                    "pct_change": (close / prev - 1.0) * 100.0,
                    "volume": int(vol.iloc[-1]) if len(vol) else 0,
                    "date": closes.index[-1].date().isoformat(),
                }
            )
        except Exception:  # noqa: BLE001
            continue
    return pd.DataFrame(rows)


def find_fallers(moves: pd.DataFrame, threshold_pct: float) -> pd.DataFrame:
    """Stocks whose fall exceeds threshold_pct (a positive number like 15.0)."""
    fallers = moves[moves["pct_change"] <= -abs(threshold_pct)].copy()
    return fallers.sort_values("pct_change").reset_index(drop=True)
