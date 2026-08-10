"""ASX directory fetch/normalise tests."""
import pytest

from src import universe

from .fakes import ASX_CSV, LEGACY_CSV, FakeResponse, install_requests


def _big_csv(n=600):
    rows = "\n".join(f"T{i:04d},Company {i},Materials,{i * 1000}" for i in range(n))
    return "ASX code,Company name,GICs industry group,Market Cap\n" + rows


def test_primary_source_used_when_healthy(monkeypatch):
    install_requests(monkeypatch, {"markitdigital": FakeResponse(_big_csv())})
    uni = universe.fetch_asx_universe()
    assert len(uni) == 600
    assert list(uni.columns) == ["ticker", "name", "sector", "market_cap_listed"]


def test_falls_back_when_primary_errors(monkeypatch):
    install_requests(
        monkeypatch,
        {
            "markitdigital": RuntimeError("403 Forbidden"),
            "asx.com.au": FakeResponse("junk header\n\n" + _big_csv()),
        },
    )
    uni = universe.fetch_asx_universe()
    assert len(uni) == 600


def test_falls_back_when_primary_returns_too_few_rows(monkeypatch):
    """A truncated/garbage primary response must not be accepted as the universe."""
    install_requests(
        monkeypatch,
        {"markitdigital": FakeResponse(ASX_CSV), "asx.com.au": FakeResponse(_big_csv())},
    )
    uni = universe.fetch_asx_universe()
    assert len(uni) == 600


def test_raises_when_all_sources_fail(monkeypatch):
    install_requests(
        monkeypatch,
        {"markitdigital": RuntimeError("boom"), "asx.com.au": RuntimeError("boom")},
    )
    with pytest.raises(RuntimeError, match="Could not fetch"):
        universe.fetch_asx_universe()


def test_legacy_junk_header_is_stripped(monkeypatch):
    install_requests(monkeypatch, {"markitdigital": FakeResponse(LEGACY_CSV)})
    df = universe._normalise(universe._fetch_csv(universe.PRIMARY_URL))
    assert list(df["ticker"]) == ["AAA", "BBB", "CCC", "DDD", "EEE"]
    assert df.iloc[0]["name"] == "Alpha Ltd"
    assert df.iloc[0]["sector"] == "Materials"
    assert df.iloc[0]["market_cap_listed"] == 50_000_000


def test_empty_response_body_does_not_crash(monkeypatch):
    """An empty body must be treated as a failed source, not an IndexError."""
    install_requests(
        monkeypatch, {"markitdigital": FakeResponse(""), "asx.com.au": FakeResponse(_big_csv())}
    )
    assert len(universe.fetch_asx_universe()) == 600


def test_missing_sector_and_mcap_columns(monkeypatch):
    """Some ASX exports omit the GICS/market-cap columns entirely."""
    rows = "\n".join(f"T{i:04d},Company {i}" for i in range(600))
    install_requests(
        monkeypatch,
        {"markitdigital": FakeResponse("ASX code,Company name\n" + rows)},
    )
    uni = universe.fetch_asx_universe()
    assert (uni["sector"] == "Unknown").all()
    assert uni["market_cap_listed"].isna().all()


def test_bad_ticker_rows_are_dropped(monkeypatch):
    rows = "\n".join(f"T{i:04d},Company {i},Materials,1000" for i in range(600))
    csv = (
        "ASX code,Company name,GICs industry group,Market Cap\n"
        + rows
        + "\nTOOLONGCODE,Bad Ltd,Materials,1000\n,Blank Ltd,Materials,1000\n"
    )
    install_requests(monkeypatch, {"markitdigital": FakeResponse(csv)})
    uni = universe.fetch_asx_universe()
    assert "TOOLONGCODE" not in list(uni["ticker"])
    assert len(uni) == 600


def test_duplicate_tickers_collapsed(monkeypatch):
    rows = "\n".join(f"T{i:04d},Company {i},Materials,1000" for i in range(600))
    csv = "ASX code,Company name,GICs industry group,Market Cap\n" + rows + "\nT0000,Dupe Ltd,Materials,1000\n"
    install_requests(monkeypatch, {"markitdigital": FakeResponse(csv)})
    uni = universe.fetch_asx_universe()
    assert uni["ticker"].is_unique
