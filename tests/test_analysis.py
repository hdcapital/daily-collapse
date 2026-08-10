"""AI analysis tests — the OpenAI client is stubbed, no API key or network needed."""
import sys
import types

import pytest

from src import analysis

CFG = {"ai_analysis": {"enabled": True, "model": "gpt-5.6-terra", "max_web_searches_per_stock": 3}}

GOOD = '{"reason": "Placement at 30% discount.", "confidence": "high", "description": "Gold explorer."}'


class Resp:
    def __init__(self, output_text):
        self.output_text = output_text


class FakeBadRequestError(Exception):
    pass


class FakeOpenAI:
    """Stub client. `script` entries are Resp objects or exceptions to raise."""

    instances: list["FakeOpenAI"] = []
    script: list = []

    def __init__(self, *a, **kw):
        self.calls = []
        FakeOpenAI.instances.append(self)

    @property
    def responses(self):
        outer = self

        class _R:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                item = FakeOpenAI.script.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

        return _R()


def install_openai(monkeypatch, script):
    FakeOpenAI.instances.clear()
    FakeOpenAI.script = list(script)
    mod = types.ModuleType("openai")
    mod.OpenAI = FakeOpenAI
    mod.BadRequestError = FakeBadRequestError
    monkeypatch.setitem(sys.modules, "openai", mod)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
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


def test_happy_path(monkeypatch):
    install_openai(monkeypatch, [Resp(GOOD)])
    a = _analyse()
    assert a.reason == "Placement at 30% discount."
    assert a.confidence == "high"
    assert a.description == "Gold explorer."


def test_uses_responses_api_with_web_search(monkeypatch):
    install_openai(monkeypatch, [Resp(GOOD)])
    _analyse()
    kwargs = FakeOpenAI.instances[0].calls[0]
    assert kwargs["model"] == "gpt-5.6-terra"
    assert kwargs["tools"] == [{"type": "web_search"}]
    assert kwargs["max_tool_calls"] == 3
    assert kwargs["instructions"] == analysis.SYSTEM
    assert "Example Ltd" in kwargs["input"]
    assert "expect a raise" in kwargs["input"]


def test_structured_output_schema_is_strict(monkeypatch):
    install_openai(monkeypatch, [Resp(GOOD)])
    _analyse()
    fmt = FakeOpenAI.instances[0].calls[0]["text"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["strict"] is True
    assert fmt["name"] == "fall_analysis"
    schema = fmt["schema"]
    # Strict mode requires every property listed as required, and no extras.
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False
    assert schema["properties"]["confidence"]["enum"] == analysis.VALID_CONFIDENCE


def test_optional_knobs_are_omitted_by_default(monkeypatch):
    install_openai(monkeypatch, [Resp(GOOD)])
    _analyse()
    kwargs = FakeOpenAI.instances[0].calls[0]
    assert "reasoning" not in kwargs
    assert "search_context_size" not in kwargs["tools"][0]


def test_optional_knobs_are_passed_when_configured(monkeypatch):
    install_openai(monkeypatch, [Resp(GOOD)])
    cfg = {"ai_analysis": dict(CFG["ai_analysis"], reasoning_effort="high", search_context_size="low")}
    analysis.analyse("XYZ", "Example Ltd", -22.5, "2026-08-07", "summary", [], cfg)
    kwargs = FakeOpenAI.instances[0].calls[0]
    assert kwargs["reasoning"] == {"effort": "high"}
    assert kwargs["tools"][0]["search_context_size"] == "low"


def test_falls_back_when_model_rejects_the_tools(monkeypatch):
    """A model without web search / structured outputs must still answer."""
    install_openai(monkeypatch, [FakeBadRequestError("unsupported parameter: tools"), Resp(GOOD)])
    a = _analyse()
    assert a.confidence == "high"
    calls = FakeOpenAI.instances[0].calls
    assert len(calls) == 2
    assert "tools" not in calls[1] and "text" not in calls[1]


def test_fallback_failure_degrades_to_summary(monkeypatch):
    install_openai(
        monkeypatch,
        [FakeBadRequestError("unsupported"), FakeBadRequestError("still unsupported")],
    )
    a = _analyse()
    assert a.confidence == "none-found"
    assert "gold" in a.description.lower()


def test_unknown_model_degrades_to_summary(monkeypatch):
    """A 404 on the model id must not break the run."""
    install_openai(monkeypatch, [RuntimeError("404 model_not_found: gpt-5.6-terra")])
    a = _analyse()
    assert a.confidence == "none-found"
    assert "gold" in a.description.lower()


def test_fenced_json_is_tolerated(monkeypatch):
    install_openai(monkeypatch, [Resp("```json\n" + GOOD + "\n```")])
    assert _analyse().confidence == "high"


def test_prose_after_json_is_tolerated(monkeypatch):
    install_openai(monkeypatch, [Resp(GOOD + "\n\nNote: see {the announcement} for details.")])
    assert _analyse().reason == "Placement at 30% discount."


def test_unparseable_response_degrades(monkeypatch):
    install_openai(monkeypatch, [Resp("I could not find anything.")])
    a = _analyse()
    assert a.confidence == "none-found"
    assert a.description


def test_malformed_json_degrades(monkeypatch):
    install_openai(monkeypatch, [Resp('{"reason": "unterminated')])
    assert _analyse().confidence == "none-found"


def test_out_of_range_confidence_is_normalised(monkeypatch):
    install_openai(monkeypatch, [Resp('{"reason": "x", "confidence": "VERY HIGH", "description": "y"}')])
    assert _analyse().confidence == "low"


def test_disabled_by_config(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    a = analysis.analyse("XYZ", "Example Ltd", -20.0, "2026-08-07", "Explores for gold.", [], {"ai_analysis": {"enabled": False}})
    assert a.confidence == "none-found"
    assert a.description == "Explores for gold."


def test_no_api_key_skips_call(monkeypatch):
    """The secret is OPENAI_API_KEY now — an Anthropic key must not enable the path."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leftover")
    a = _analyse()
    assert a.confidence == "none-found"


def test_shorten():
    assert analysis._shorten("a b c", 2) == "a b…"
    assert analysis._shorten("", 5) == ""
    assert analysis._shorten(None, 5) == ""
