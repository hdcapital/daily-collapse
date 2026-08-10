"""End-to-end runs of the orchestrator with both network dependencies faked.

This is the closest substitute for `python -m src.main --dry-run` in an
environment with no egress: it exercises universe -> prices -> filters ->
fundamentals -> data lake -> analysis -> render, for real.
"""
import sys

import pytest

from src import main

from .fakes import FakeResponse, bars, install_requests, install_yf

UNIVERSE_CSV_HEAD = "ASX code,Company name,GICs industry group,Market Cap\n"


def _universe_csv(n=600):
    """A universe big enough to pass the sanity check, with four known names."""
    known = [
        "AAA,Alpha Ltd,Materials,50000000",
        "BBB,Beta Ltd,Software & Services,200000000",
        "CCC,Gamma Ltd,Energy,100000000",
        "DDD,Delta Ltd,Banks,900000000",
    ]
    filler = [f"F{i:04d},Filler {i},Materials,1000000" for i in range(n - len(known))]
    return UNIVERSE_CSV_HEAD + "\n".join(known + filler)


HISTORIES = {
    # Alpha: -20% today
    "AAA.AX": bars([1.00, 1.00, 1.00, 1.00, 0.80], volumes=[1, 2, 3, 4, 500_000]),
    # Beta: -16% today
    "BBB.AX": bars([2.00, 2.00, 2.00, 2.00, 1.68]),
    # Gamma: -10%, under threshold
    "CCC.AX": bars([3.00, 3.00, 3.00, 3.00, 2.70]),
    # Delta: up
    "DDD.AX": bars([4.00, 4.00, 4.00, 4.00, 4.40]),
}

INFOS = {
    "AAA.AX": {
        "marketCap": 200_000_000,
        "enterpriseValue": 250_000_000,
        "totalRevenue": 50_000_000,
        "financialCurrency": "AUD",
        "longBusinessSummary": "Alpha Ltd explores for gold in Western Australia.",
        "website": "https://alpha.example.com",
    },
    "BBB.AX": {
        "marketCap": 168_000_000,
        "totalRevenue": 30_000_000,
        "financialCurrency": "AUD",
        "longBusinessSummary": "Beta Ltd sells software.",
    },
}


@pytest.fixture
def offline(monkeypatch, tmp_path):
    install_requests(monkeypatch, {"markitdigital": FakeResponse(_universe_csv())})
    install_yf(monkeypatch, HISTORIES, INFOS)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("DATALAKE_S3_BUCKET", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["src.main", *argv])
    return main.main()


def test_dry_run_writes_report(offline, monkeypatch):
    assert _run(monkeypatch, ["--dry-run"]) == 0
    html = (offline / "out" / "report.html").read_text()
    # Both fallers present, worst first; the -10% and the riser are not.
    assert "Alpha Ltd" in html and "Beta Ltd" in html
    assert "Gamma Ltd" not in html and "Delta Ltd" not in html
    assert html.index("Alpha Ltd") < html.index("Beta Ltd")
    # Fundamentals came through yfinance info.
    assert "$200.0M" in html  # market cap
    assert "5.0×" in html  # EV / Rev = 250M / 50M
    # Degraded AI (no key) still yields a description from the Yahoo summary.
    assert "explores for gold" in html.lower()
    assert "500,000" in html  # volume formatting


def test_quiet_day_renders(offline, monkeypatch, tmp_path):
    """No stock past the threshold must still produce a report, not a crash."""
    cfg = dict(main.load_config(), threshold_pct=99)
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    assert _run(monkeypatch, ["--dry-run"]) == 0
    assert "A quiet close." in (tmp_path / "out" / "report.html").read_text()


def test_limit_shrinks_the_universe(offline, monkeypatch):
    fake = sys.modules["yfinance"]
    assert _run(monkeypatch, ["--dry-run", "--limit", "2"]) == 0
    assert sum(len(c) for c in fake.download_calls) == 2


def test_sector_exclusion_end_to_end(offline, monkeypatch):
    cfg = dict(main.load_config())
    cfg["exclude_sectors"] = {"enabled": True, "sectors": ["Materials"]}
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    assert _run(monkeypatch, ["--dry-run"]) == 0
    html = (main.Path("out") / "report.html").read_text()
    assert "Alpha Ltd" not in html  # Materials, muted
    assert "Beta Ltd" in html
    assert "sectors muted: Materials" in html


def test_min_market_cap_end_to_end(offline, monkeypatch):
    cfg = dict(main.load_config())
    cfg["min_market_cap_aud"] = 1e8
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    assert _run(monkeypatch, ["--dry-run"]) == 0
    html = (main.Path("out") / "report.html").read_text()
    assert "Alpha Ltd" not in html  # $50m listed cap
    assert "Beta Ltd" in html


def test_cap_truncates_and_discloses(offline, monkeypatch):
    cfg = dict(main.load_config())
    cfg["max_stocks_in_email"] = 1
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    assert _run(monkeypatch, ["--dry-run"]) == 0
    html = (main.Path("out") / "report.html").read_text()
    assert "Alpha Ltd" in html and "Beta Ltd" not in html
    assert "2" in html  # total flagged is still reported


def test_missing_smtp_does_not_send_or_fail(offline, monkeypatch):
    """A live run without SMTP secrets should still exit 0 with a report on disk."""
    assert _run(monkeypatch, []) == 0
    assert (main.Path("out") / "report.html").exists()


def test_sends_when_smtp_configured(offline, monkeypatch):
    sent = {}
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(main.emailer, "send", lambda html, subject: sent.update(html=html, subject=subject))
    assert _run(monkeypatch, []) == 0
    assert "ASX Fall Wire" in sent["subject"]
    assert "2 stocks down >15%" in sent["subject"]


def test_subject_singular_and_quiet(offline, monkeypatch):
    sent = {}
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(main.emailer, "send", lambda html, subject: sent.update(subject=subject))
    cfg = dict(main.load_config())
    cfg["threshold_pct"] = 18
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    _run(monkeypatch, [])
    assert "1 stock down" in sent["subject"]

    cfg["threshold_pct"] = 99
    _run(monkeypatch, [])
    assert "quiet close" in sent["subject"]


def test_universe_failure_propagates(offline, monkeypatch):
    install_requests(monkeypatch, {"markitdigital": RuntimeError("403"), "asx.com.au": RuntimeError("403")})
    with pytest.raises(RuntimeError, match="Could not fetch"):
        _run(monkeypatch, ["--dry-run"])


def test_ai_enabled_path(offline, monkeypatch):
    """With a key present the analysis text must reach the rendered report."""
    import types

    class Resp:
        output_text = '{"reason":"Placement at a 30% discount.","confidence":"high","description":"Gold explorer."}'

    class FakeOpenAI:
        def __init__(self, *a, **k):
            pass

        @property
        def responses(self):
            class _R:
                def create(self, **kwargs):
                    return Resp()

            return _R()

    mod = types.ModuleType("openai")
    mod.OpenAI = FakeOpenAI
    mod.BadRequestError = type("BadRequestError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "openai", mod)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    assert _run(monkeypatch, ["--dry-run"]) == 0
    html = (main.Path("out") / "report.html").read_text()
    assert "Placement at a 30% discount." in html
    assert "HIGH" in html


def test_datalake_context_reaches_the_report(offline, monkeypatch, tmp_path):
    lake = tmp_path / "lake"
    lake.mkdir()
    (lake / "alpha-note.md").write_text("AAA — balance sheet thin, expect a placement.")
    cfg = dict(main.load_config())
    cfg["datalake"] = {"enabled": True, "local_dir": str(lake), "max_files_per_ticker": 5, "max_chars_per_file": 500}
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    assert _run(monkeypatch, ["--dry-run"]) == 0
    assert "alpha-note.md" in (main.Path("out") / "report.html").read_text()


def test_revenue_screen_hides_small_and_unknown(offline, monkeypatch):
    """Below-threshold and no-revenue names drop out, and the email says so."""
    infos = dict(INFOS)
    infos["AAA.AX"] = dict(INFOS["AAA.AX"], totalRevenue=1_000_000)  # below the bar
    infos["BBB.AX"] = {k: v for k, v in INFOS["BBB.AX"].items() if k != "totalRevenue"}
    install_yf(monkeypatch, HISTORIES, infos)
    assert _run(monkeypatch, ["--dry-run"]) == 0
    html = (main.Path("out") / "report.html").read_text()
    assert "Alpha Ltd" not in html and "Beta Ltd" not in html
    assert "A quiet close." in html
    assert "revenue screen: 2 hidden below $20M" in html


def test_revenue_screen_keeps_unknown_when_configured(offline, monkeypatch):
    infos = dict(INFOS)
    infos["BBB.AX"] = {k: v for k, v in INFOS["BBB.AX"].items() if k != "totalRevenue"}
    install_yf(monkeypatch, HISTORIES, infos)
    cfg = dict(main.load_config())
    cfg["include_unknown_revenue"] = True
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    assert _run(monkeypatch, ["--dry-run"]) == 0
    assert "Beta Ltd" in (main.Path("out") / "report.html").read_text()


def test_revenue_screen_off_by_zero(offline, monkeypatch):
    infos = dict(INFOS)
    infos["AAA.AX"] = dict(INFOS["AAA.AX"], totalRevenue=1_000_000)
    install_yf(monkeypatch, HISTORIES, infos)
    cfg = dict(main.load_config())
    cfg["min_revenue_aud"] = 0
    monkeypatch.setattr(main, "load_config", lambda: cfg)
    assert _run(monkeypatch, ["--dry-run"]) == 0
    html = (main.Path("out") / "report.html").read_text()
    assert "Alpha Ltd" in html
    assert "revenue screen" not in html


def test_screened_stocks_cost_no_ai_calls(offline, monkeypatch):
    """The screen must run before enrichment, or it wastes money on dropped names."""
    import types

    calls = []

    class Resp:
        output_text = '{"reason":"x","confidence":"low","description":"y"}'

    class FakeOpenAI:
        def __init__(self, *a, **k):
            pass

        @property
        def responses(self):
            class _R:
                def create(self, **kwargs):
                    calls.append(kwargs)
                    return Resp()

            return _R()

    mod = types.ModuleType("openai")
    mod.OpenAI = FakeOpenAI
    mod.BadRequestError = type("BadRequestError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "openai", mod)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    infos = dict(INFOS)
    infos["AAA.AX"] = dict(INFOS["AAA.AX"], totalRevenue=1_000_000)  # screened out
    install_yf(monkeypatch, HISTORIES, infos)
    assert _run(monkeypatch, ["--dry-run"]) == 0
    assert len(calls) == 1  # only Beta survived, so only one AI call


def test_config_file_is_valid():
    cfg = main.load_config()
    assert isinstance(cfg["threshold_pct"], (int, float))
    assert cfg["max_stocks_in_email"] > 0
    assert "enabled" in cfg["exclude_sectors"]
    assert "model" in cfg["ai_analysis"]
    manifests = cfg["datalake"]["manifests"]
    assert manifests["markets"] == ["asx"]
    assert manifests["prefix"].endswith("/")
