"""Price-layer tests: batching, extraction, stale bars, failure modes."""
import pandas as pd
import pytest

from src import prices

from .fakes import bars, install_yf


def test_extracts_last_session_move(monkeypatch):
    install_yf(monkeypatch, {"AAA.AX": bars([1.00, 1.00, 0.80], volumes=[10, 20, 30])})
    moves = prices.daily_moves(["AAA"])
    row = moves.iloc[0]
    assert row["ticker"] == "AAA"
    assert row["prev_close"] == 1.00
    assert row["close"] == 0.80
    assert row["pct_change"] == pytest.approx(-20.0)
    assert row["volume"] == 30


def test_ticker_with_one_bar_is_skipped(monkeypatch):
    install_yf(monkeypatch, {"AAA.AX": bars([1.00, 0.80]), "BBB.AX": bars([2.00])})
    moves = prices.daily_moves(["AAA", "BBB"])
    assert list(moves["ticker"]) == ["AAA"]


def test_missing_ticker_is_skipped(monkeypatch):
    install_yf(monkeypatch, {"AAA.AX": bars([1.00, 0.80])})
    moves = prices.daily_moves(["AAA", "NOPE"])
    assert list(moves["ticker"]) == ["AAA"]


def test_batches_are_chunked(monkeypatch):
    histories = {f"T{i:03d}.AX": bars([1.0, 0.9]) for i in range(prices.BATCH + 5)}
    fake = install_yf(monkeypatch, histories)
    prices.daily_moves([s.removesuffix(".AX") for s in histories])
    assert len(fake.download_calls) == 2
    assert len(fake.download_calls[0]) == prices.BATCH


def test_no_data_at_all_raises_clean_error(monkeypatch):
    """Every batch returning an empty frame must not leak a KeyError."""
    install_yf(monkeypatch, {})
    with pytest.raises(RuntimeError, match="No price data"):
        prices.daily_moves(["AAA", "BBB"])


def test_all_batches_failing_raises(monkeypatch):
    install_yf(monkeypatch, {"AAA.AX": bars([1.0, 0.8])}, fail_batches=True)
    with pytest.raises(RuntimeError, match="No price data"):
        prices.daily_moves(["AAA"])


def test_stale_bars_are_dropped(monkeypatch):
    """A stock halted for days must not have last week's fall reported as today's.

    AAA trades through today; HALT's last bar is three sessions earlier and its
    final move was -30%. Only AAA's move belongs in today's report.
    """
    install_yf(
        monkeypatch,
        {
            "AAA.AX": bars([1.00, 1.00, 1.00, 1.00, 0.90], start="2026-08-03"),
            "HALT.AX": bars([1.00, 0.70], start="2026-08-03"),
        },
    )
    moves = prices.daily_moves(["AAA", "HALT"])
    assert list(moves["ticker"]) == ["AAA"]
    assert moves["date"].nunique() == 1


def test_mass_stale_data_raises_instead_of_quiet_day(monkeypatch):
    """The 2026-08-19 5am incident: between Sydney midnight and Yahoo's EOD
    consolidation nearly every ticker's latest bar was the *previous* session.
    Filtering that down and reporting a quiet day is a lie — refuse instead."""
    histories = {f"T{i}.AX": bars([1.00, 0.70], start="2026-08-03") for i in range(9)}
    histories["AAA.AX"] = bars([1.00, 1.00, 1.00, 1.00, 0.90], start="2026-08-03")
    install_yf(monkeypatch, histories)
    with pytest.raises(RuntimeError, match="looks incomplete"):
        prices.daily_moves(["AAA"] + [f"T{i}" for i in range(9)])


def test_normal_stale_fraction_still_passes(monkeypatch):
    """A typical day has ~20% halted/untraded — that must keep working."""
    histories = {f"T{i}.AX": bars([1.00, 1.00, 1.00, 1.00, 0.90], start="2026-08-03") for i in range(8)}
    histories["H1.AX"] = bars([1.00, 0.70], start="2026-08-03")
    histories["H2.AX"] = bars([1.00, 0.70], start="2026-08-03")
    install_yf(monkeypatch, histories)
    moves = prices.daily_moves([f"T{i}" for i in range(8)] + ["H1", "H2"])
    assert len(moves) == 8


def test_zero_prev_close_is_skipped(monkeypatch):
    """A 0.00 prior close would make pct_change infinite — drop the ticker."""
    install_yf(monkeypatch, {"ZERO.AX": bars([0.0, 0.5]), "AAA.AX": bars([1.0, 0.8])})
    moves = prices.daily_moves(["ZERO", "AAA"])
    assert list(moves["ticker"]) == ["AAA"]


def test_find_fallers_sorted_worst_first():
    moves = pd.DataFrame(
        [
            {"ticker": "AAA", "pct_change": -16.0},
            {"ticker": "BBB", "pct_change": -40.0},
            {"ticker": "CCC", "pct_change": -15.0},
            {"ticker": "DDD", "pct_change": 5.0},
        ]
    )
    fallers = prices.find_fallers(moves, 15.0)
    assert list(fallers["ticker"]) == ["BBB", "AAA"]


def test_threshold_is_strict():
    """Config says 'down MORE than this %' — an exactly-at-threshold fall is out."""
    moves = pd.DataFrame([{"ticker": "AAA", "pct_change": -15.0}])
    assert prices.find_fallers(moves, 15.0).empty


def test_find_fallers_on_empty_frame():
    moves = pd.DataFrame(columns=["ticker", "pct_change"])
    assert prices.find_fallers(moves, 15.0).empty
