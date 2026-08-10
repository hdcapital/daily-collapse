"""Report rendering and SMTP delivery tests."""
import smtplib

import pytest

from src import emailer


def _row(ticker="AAA", pct=-20.0, **over):
    row = {
        "ticker": ticker,
        "name": "Alpha Ltd",
        "sector": "Materials",
        "pct_change": pct,
        "pct_str": f"{pct:.1f}%",
        "prev_close_str": "$1",
        "close_str": "$0.8",
        "volume_str": "100",
        "reason": "Capital raising at a deep discount.",
        "confidence": "high",
        "description": "Gold explorer in WA.",
        "metrics": [("Mkt cap", "$50.0M"), ("EV", "$40.0M"), ("EV / Rev", "—"), ("EV / EBIT", "—")],
        "lake_sources": "",
    }
    row.update(over)
    return row


def _ctx(rows, **kw):
    kw.setdefault("scanned", 2000)
    kw.setdefault("threshold", 15.0)
    kw.setdefault("excluded", [])
    kw.setdefault("report_date", "Fri 07 Aug 2026")
    return emailer.build_context(rows, **kw)


def test_renders_a_faller():
    html = emailer.render(_ctx([_row()]))
    assert "Alpha Ltd" in html and "-20.0%" in html and "AAA" in html


def test_quiet_day():
    html = emailer.render(_ctx([]))
    assert "A quiet close." in html


def test_bar_scaling_and_cap():
    assert _ctx([_row(pct=-20.0)])["fallers"][0]["bar_pct"] == 40
    assert _ctx([_row(pct=-80.0)])["fallers"][0]["bar_pct"] == 100


def test_excluded_sectors_note():
    assert "sectors muted: Energy" in emailer.render(_ctx([_row()], excluded=["Energy"]))


def test_ai_text_is_html_escaped():
    """AI output is untrusted (it summarises web search results) — it must never
    be able to inject markup into the email."""
    hostile = '<script>alert(1)</script> profit fell >50% & margins <2%'
    html = emailer.render(_ctx([_row(reason=hostile, description=hostile)]))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp;" in html


def test_company_name_is_escaped():
    html = emailer.render(_ctx([_row(name="Smith & Sons <Holdings>")]))
    assert "Smith &amp; Sons" in html
    assert "<Holdings>" not in html


def test_truncation_is_disclosed():
    """When the tail is cut, the reader must be told rather than silently shown 40."""
    ctx = _ctx([_row(f"T{i}") for i in range(40)], total_fallers=57)
    html = emailer.render(ctx)
    assert "57" in html
    assert ctx["truncated"] == 17


def test_no_truncation_note_when_complete():
    ctx = _ctx([_row()], total_fallers=1)
    assert ctx["truncated"] == 0
    assert "showing" not in emailer.render(ctx).lower()


def test_confidence_colours():
    for conf in ("high", "medium", "low", "none-found"):
        ctx = _ctx([_row(confidence=conf)])
        assert ctx["fallers"][0]["conf_color"] == emailer.CONF_COLORS[conf]
    assert _ctx([_row(confidence="weird")])["fallers"][0]["conf_color"] == "#64707E"


def test_worst_summary_fields():
    ctx = _ctx([_row("BBB", -40.0), _row("AAA", -20.0)])
    assert ctx["worst_ticker"] == "BBB"
    assert ctx["worst_pct"] == "-40.0%"


def test_build_context_does_not_mutate_caller_rows():
    rows = [_row()]
    emailer.build_context(rows, scanned=1, threshold=15.0, excluded=[], report_date="x")
    assert "bar_pct" not in rows[0]


# --------------------------------------------------------------------------- SMTP


class FakeSMTP:
    last: "FakeSMTP" = None

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.started_tls = False
        self.logged_in = None
        self.sent = None
        FakeSMTP.last = self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def sendmail(self, sender, to, body):
        self.sent = (sender, to, body)


def _smtp_env(monkeypatch, **over):
    env = {
        "SMTP_HOST": "smtp.example.com",
        "SMTP_PORT": "587",
        "SMTP_USER": "bot@example.com",
        "SMTP_PASS": "hunter2",
        "EMAIL_FROM": "wire@example.com",
        "EMAIL_TO": "a@example.com, b@example.com",
    }
    env.update(over)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_send_uses_starttls_on_587(monkeypatch):
    _smtp_env(monkeypatch)
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    emailer.send("<p>hi</p>", "Subject line")
    s = FakeSMTP.last
    assert s.started_tls and s.logged_in == ("bot@example.com", "hunter2")
    sender, to, body = s.sent
    assert sender == "wire@example.com"
    assert to == ["a@example.com", "b@example.com"]
    assert "Subject line" in body


def test_send_uses_implicit_ssl_on_465(monkeypatch):
    """Port 465 is implicit TLS — issuing STARTTLS there fails on most providers."""
    _smtp_env(monkeypatch, SMTP_PORT="465")
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP", None)  # must not be used
    emailer.send("<p>hi</p>", "Subject line")
    assert FakeSMTP.last.port == 465
    assert not FakeSMTP.last.started_tls


def test_send_defaults_from_to_user(monkeypatch):
    _smtp_env(monkeypatch)
    monkeypatch.delenv("EMAIL_FROM")
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    emailer.send("<p>hi</p>", "s")
    assert FakeSMTP.last.sent[0] == "bot@example.com"


def test_blank_email_from_falls_back_to_user(monkeypatch):
    """An unset GitHub secret arrives as "" — that must not become the From header."""
    _smtp_env(monkeypatch, EMAIL_FROM="")
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    emailer.send("<p>hi</p>", "s")
    sender, _, body = FakeSMTP.last.sent
    assert sender == "bot@example.com"
    assert "From: bot@example.com" in body


def test_blank_smtp_port_falls_back_to_587(monkeypatch):
    """int("") would raise; a blank port must fall back to the default."""
    _smtp_env(monkeypatch, SMTP_PORT="")
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    emailer.send("<p>hi</p>", "s")
    assert FakeSMTP.last.port == 587


def test_blank_required_secret_raises_clearly(monkeypatch):
    _smtp_env(monkeypatch, SMTP_PASS="")
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    with pytest.raises(RuntimeError, match="SMTP_PASS is not set"):
        emailer.send("<p>hi</p>", "s")


def test_recipients_are_trimmed_of_blanks(monkeypatch):
    _smtp_env(monkeypatch, EMAIL_TO="a@example.com, ,b@example.com,")
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    emailer.send("<p>hi</p>", "s")
    assert FakeSMTP.last.sent[1] == ["a@example.com", "b@example.com"]


def test_send_is_multipart_alternative(monkeypatch):
    _smtp_env(monkeypatch)
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    emailer.send("<p>hi</p>", "s")
    body = FakeSMTP.last.sent[2]
    assert "multipart/alternative" in body
    assert "text/plain" in body and "text/html" in body
