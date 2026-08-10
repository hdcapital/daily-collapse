"""Offline stand-ins for the two network dependencies (yfinance, requests).

The scanner only ever touches Yahoo via ``yf.download`` / ``yf.Ticker`` and the
ASX directory via ``requests.get``, so faking those two is enough to drive the
whole pipeline without egress.
"""
from __future__ import annotations

import sys
import types

import pandas as pd


def bars(closes: list[float], volumes: list[int] | None = None, start: str = "2026-08-03") -> pd.DataFrame:
    """A daily OHLCV frame shaped like a single ticker's yfinance history."""
    idx = pd.bdate_range(start=start, periods=len(closes))
    vol = volumes if volumes is not None else [1000] * len(closes)
    return pd.DataFrame(
        {
            "Open": closes,
            "High": closes,
            "Low": closes,
            "Close": closes,
            "Adj Close": closes,
            "Volume": vol,
        },
        index=idx,
    )


def multi_download(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Combine per-ticker frames into the MultiIndex shape yf.download returns
    for a multi-ticker request with group_by="ticker"."""
    return pd.concat(frames, axis=1)


class FakeYF(types.ModuleType):
    """Minimal yfinance replacement.

    ``histories`` maps a Yahoo symbol (e.g. "AAA.AX") to a bars() frame.
    ``infos`` maps the same symbol to the dict ``Ticker.info`` should return.
    """

    def __init__(self, histories: dict[str, pd.DataFrame], infos: dict | None = None, fail_batches: bool = False):
        super().__init__("yfinance")
        self.histories = histories
        self.infos = infos or {}
        self.fail_batches = fail_batches
        self.download_calls: list[list[str]] = []

    def download(self, tickers, **kwargs):
        self.download_calls.append(list(tickers))
        if self.fail_batches:
            raise RuntimeError("simulated Yahoo outage")
        present = {t: self.histories[t] for t in tickers if t in self.histories}
        if not present:
            return pd.DataFrame()
        return multi_download(present)

    def Ticker(self, symbol):  # noqa: N802 - mirrors yfinance's API
        infos = self.infos

        class _T:
            @property
            def info(self):
                return infos.get(symbol, {})

            @property
            def income_stmt(self):
                return pd.DataFrame()

        return _T()


def install_yf(monkeypatch, histories, infos=None, fail_batches=False) -> FakeYF:
    fake = FakeYF(histories, infos, fail_batches)
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    return fake


ASX_CSV = """ASX code,Company name,GICs industry group,Market Cap
AAA,Alpha Ltd,Materials,50000000
BBB,Beta Ltd,Software & Services,200000000
CCC,Gamma Ltd,Energy,100000000
DDD,Delta Ltd,Banks,900000000
EEE,Halted Ltd,Materials,10000000
"""

# The legacy fallback file ships two junk lines above the real header.
LEGACY_CSV = "ASX listed companies as at 01-Aug-2026\n\n" + ASX_CSV


class FakeResponse:
    def __init__(self, text: str, status: int = 200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def install_requests(monkeypatch, responses: dict):
    """responses maps a URL substring -> FakeResponse (or an Exception to raise)."""
    import requests

    def fake_get(url, **kwargs):
        for key, resp in responses.items():
            if key in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise RuntimeError(f"unexpected URL in test: {url}")

    monkeypatch.setattr(requests, "get", fake_get)
