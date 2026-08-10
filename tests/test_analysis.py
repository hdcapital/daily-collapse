"""AI analysis tests — the Anthropic client is stubbed, no API key needed."""
import sys
import types

import pytest

from src import analysis

CFG = {"ai_analysis": {"enabled": True, "model": "claude-sonnet-4-6", "max_web_searches_per_stock": 3}}


class Block:
    def __init__(self, text, type_="text"):
        self.text = text
        self.type = type_


class Msg:
    def __init__(self, blocks, stop_reason="end_turn"):
        self.content = blocks
        self.stop_reason = stop_reason


class FakeAnthropic:
    """Stub client. `script` is a list of Msg objects returned in order."""

    instances: list["FakeAnthropic"] = []

    def __init__(self, *a, **kw):
        self.calls = []
        FakeAnthropic.instances.append(self)

    @property
    def messages(self):
        outer = self

        class _M:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                return outer.script.pop(0)

        return _M()


def install_anthropic(monkeypatch, script):
    FakeAnthropic.instances.clear()
    mod = types.ModuleType("anthropic")
    mod.Anthropic = FakeAnthropic
    FakeAnthropic.script = list(script)
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    return mod


def _analyse():
    return analysis.analyse(
        ticker="XYZ",
        name="Example Ltd",
        pct_change=-22.5,
        date="2026-08-07",
        yf_summary="Example Ltd explores for gold in Western Australia.",
        lake_context=[{"source": "local:notes.md", "text": "expect a raise"}],
        cfg=CFG,
    )


GOOD = '{"reason": "Placement at 30% discount.", "confidence": "high", "description": "Gold explorer."}'


def test_happy_path(monkeypatch):
    install_anthropic(monkeypatch, [Msg([Block(GOOD)])])
    a = _analyse()
    assert a.reason == "Placement at 30% discount."
    assert a.confidence == "high"
    assert a.description == "Gold explorer."


def test_fenced_json_is_tolerated(monkeypatch):
    install_anthropic(monkeypatch, [Msg([Block("```json\n" + GOOD + "\n```")])])
    assert _analyse().confidence == "high"


def test_prose_after_json_is_tolerated(monkeypatch):
    """A trailing sentence with braces must not defeat the parser."""
    text = GOOD + "\n\nNote: see {the announcement} for details."
    install_anthropic(monkeypatch, [Msg([Block(text)])])
    a = _analyse()
    assert a.reason == "Placement at 30% discount."


def test_pause_turn_is_resumed(monkeypatch):
    """Server-side web search can pause the turn; the answer must still arrive."""
    install_anthropic(
        monkeypatch,
        [Msg([Block("", "server_tool_use")], stop_reason="pause_turn"), Msg([Block(GOOD)])],
    )
    a = _analyse()
    assert a.confidence == "high"
    assert len(FakeAnthropic.instances[0].calls) == 2


def test_api_failure_degrades_to_summary(monkeypatch):
    mod = install_anthropic(monkeypatch, [])

    class Boom(FakeAnthropic):
        @property
        def messages(self):
            class _M:
                def create(self, **kwargs):
                    raise RuntimeError("529 overloaded")

            return _M()

    mod.Anthropic = Boom
    a = _analyse()
    assert a.confidence == "none-found"
    assert "gold" in a.description.lower()


def test_unparseable_response_degrades(monkeypatch):
    install_anthropic(monkeypatch, [Msg([Block("I could not find anything.")])])
    a = _analyse()
    assert a.confidence == "none-found"
    assert a.description


def test_malformed_json_degrades(monkeypatch):
    install_anthropic(monkeypatch, [Msg([Block('{"reason": "unterminated')])])
    assert _analyse().confidence == "none-found"


def test_disabled_by_config(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    a = analysis.analyse("XYZ", "Example Ltd", -20.0, "2026-08-07", "Explores for gold.", [], {"ai_analysis": {"enabled": False}})
    assert a.confidence == "none-found"
    assert a.description == "Explores for gold."


def test_no_api_key_skips_call(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    a = _analyse()
    assert a.confidence == "none-found"


def test_request_shape(monkeypatch):
    """Model, search cap and the JSON-only system prompt must reach the API."""
    install_anthropic(monkeypatch, [Msg([Block(GOOD)])])
    _analyse()
    kwargs = FakeAnthropic.instances[0].calls[0]
    assert kwargs["model"] == "claude-sonnet-4-6"
    assert kwargs["tools"][0]["name"] == "web_search"
    assert kwargs["tools"][0]["max_uses"] == 3
    assert "JSON" in kwargs["system"]
    assert "Example Ltd" in kwargs["messages"][0]["content"]
    assert "expect a raise" in kwargs["messages"][0]["content"]


def test_shorten():
    assert analysis._shorten("a b c", 2) == "a b…"
    assert analysis._shorten("", 5) == ""
    assert analysis._shorten(None, 5) == ""
