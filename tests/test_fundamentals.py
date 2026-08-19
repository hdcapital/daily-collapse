"""Retry behaviour of get_fundamentals against a rate-limiting Yahoo."""
from src import fundamentals
from src.fundamentals import get_fundamentals

from .fakes import install_yf

INFO = {"AAA.AX": {"totalRevenue": 50_000_000, "marketCap": 200_000_000}}


def _rl():
    return RuntimeError("Too Many Requests. Rate limited. Try after a while.")


def test_rate_limit_retries_until_it_clears(monkeypatch):
    sleeps = []
    monkeypatch.setattr(fundamentals, "_sleep", sleeps.append)
    install_yf(monkeypatch, {}, INFO, info_errors={"AAA.AX": [_rl(), _rl()]})
    f = get_fundamentals("AAA")
    assert not f.fetch_failed
    assert f.revenue == 50_000_000
    assert sleeps == [10, 30]


def test_exhausted_retries_fail_and_trip_the_breaker(monkeypatch):
    sleeps = []
    monkeypatch.setattr(fundamentals, "_sleep", sleeps.append)
    fake = install_yf(
        monkeypatch, {}, INFO, info_errors={"AAA.AX": [_rl() for _ in range(4)], "BBB.AX": [_rl()]}
    )
    f = get_fundamentals("AAA")
    assert f.fetch_failed
    assert sleeps == [10, 30, 60]
    # Yahoo didn't relent after the full wait, so the next ticker must not
    # re-pay it: one quick attempt, no new sleeps.
    f2 = get_fundamentals("BBB")
    assert f2.fetch_failed
    assert sleeps == [10, 30, 60]
    assert fake.info_calls.count("BBB.AX") == 1


def test_success_re_arms_the_breaker(monkeypatch):
    sleeps = []
    monkeypatch.setattr(fundamentals, "_sleep", sleeps.append)
    infos = dict(INFO, **{"BBB.AX": {"totalRevenue": 1}, "CCC.AX": {"totalRevenue": 2}})
    install_yf(
        monkeypatch,
        {},
        infos,
        info_errors={"AAA.AX": [_rl() for _ in range(4)], "CCC.AX": [_rl()]},
    )
    get_fundamentals("AAA")  # trips the breaker
    assert not get_fundamentals("BBB").fetch_failed  # limiter has recovered
    f = get_fundamentals("CCC")  # retries are back: one limited call, then data
    assert not f.fetch_failed
    assert f.revenue == 2
    assert sleeps == [10, 30, 60, 10]


def test_other_errors_do_not_retry(monkeypatch):
    sleeps = []
    monkeypatch.setattr(fundamentals, "_sleep", sleeps.append)
    fake = install_yf(monkeypatch, {}, {}, info_errors={"AAA.AX": [RuntimeError("boom")]})
    f = get_fundamentals("AAA")
    assert f.fetch_failed
    assert sleeps == []
    assert fake.info_calls.count("AAA.AX") == 1


def test_empty_info_is_not_a_fetch_failure(monkeypatch):
    """A fine response with no data (e.g. delisted) keeps the old semantics:
    revenue is genuinely unknown, and the screen's include_unknown rules apply."""
    install_yf(monkeypatch, {}, {})
    f = get_fundamentals("AAA")
    assert not f.fetch_failed
    assert f.revenue is None
