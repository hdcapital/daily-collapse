"""Ask an OpenAI model (with web search) why a stock sold off, blending data-lake context.

Uses the Responses API: the `web_search` built-in tool runs server-side, so a
single call covers search + reasoning + the JSON answer. Needs OPENAI_API_KEY.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)

VALID_CONFIDENCE = ["high", "medium", "low", "none-found"]

SYSTEM = (
    "You are an equity analyst covering the ASX. Be factual and terse. "
    "Never invent announcements; if no cause is found, say so plainly. "
    "Respond ONLY with a JSON object, no markdown fences, with keys: "
    "reason (string, <=60 words, why the stock fell today, cite the trigger if found), "
    "confidence (one of: high, medium, low, none-found), "
    "description (string, <=30 words, plain-English what the company does)."
)

# Structured Outputs schema — strict mode requires every property to be listed
# in `required` and `additionalProperties: false`.
SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {"type": "string", "description": "Why the stock fell today, <=60 words."},
        "confidence": {"type": "string", "enum": VALID_CONFIDENCE},
        "description": {"type": "string", "description": "What the company does, <=30 words."},
    },
    "required": ["reason", "confidence", "description"],
    "additionalProperties": False,
}


@dataclass
class Analysis:
    reason: str = "AI analysis unavailable."
    confidence: str = "none-found"
    description: str = ""


def analyse(
    ticker: str,
    name: str,
    pct_change: float,
    date: str,
    yf_summary: str,
    lake_context: list[dict],
    cfg: dict,
) -> Analysis:
    ai = cfg.get("ai_analysis", {})
    if not ai.get("enabled", True) or not os.environ.get("OPENAI_API_KEY"):
        return Analysis(description=_shorten(yf_summary))
    try:
        import openai

        client = openai.OpenAI()
        lake = "\n\n".join(f"[{h['source']}]\n{h['text']}" for h in lake_context) or "(no data-lake hits)"
        prompt = (
            f"{name} (ASX:{ticker}) closed down {pct_change:.1f}% on {date}.\n\n"
            f"Company summary on file:\n{yf_summary[:1200] or '(none)'}\n\n"
            f"Internal data-lake extracts:\n{lake[:6000]}\n\n"
            "Search the web for today's ASX announcements, trading halts, capital "
            "raisings, downgrades, drill results or news explaining the fall, then "
            "answer in the required JSON."
        )
        model = ai.get("model", "gpt-5.6-terra")
        text = _request(client, model, prompt, ai)
        return _parse(text, yf_summary)
    except Exception as e:  # noqa: BLE001
        log.warning("AI analysis failed for %s: %s", ticker, e)
        return Analysis(description=_shorten(yf_summary))


def _request(client, model: str, prompt: str, ai: dict) -> str:
    """One Responses call with web search + structured output.

    Falls back to a plain request if the model rejects the optional features,
    so a model that lacks web search or Structured Outputs still returns
    something usable rather than nothing.
    """
    import openai

    web_search: dict = {"type": "web_search"}
    if ai.get("search_context_size"):
        web_search["search_context_size"] = ai["search_context_size"]

    request = dict(
        model=model,
        instructions=SYSTEM,
        input=prompt,
        tools=[web_search],
        max_tool_calls=int(ai.get("max_web_searches_per_stock", 3)),
        text={"format": {"type": "json_schema", "name": "fall_analysis", "schema": SCHEMA, "strict": True}},
        max_output_tokens=int(ai.get("max_output_tokens", 2000)),
    )
    if ai.get("reasoning_effort"):
        request["reasoning"] = {"effort": ai["reasoning_effort"]}

    try:
        return client.responses.create(**request).output_text
    except openai.BadRequestError as e:
        log.warning(
            "Model %s rejected the web-search/structured-output request (%s); "
            "retrying without them — the answer will not be search-backed.",
            model,
            e,
        )

    plain = client.responses.create(
        model=model,
        instructions=SYSTEM,
        input=prompt,
        max_output_tokens=int(ai.get("max_output_tokens", 2000)),
    )
    return plain.output_text


def _json_objects(text: str):
    """Yield candidate ``{...}`` spans, brace-balanced and quote-aware.

    Structured Outputs should make this unnecessary, but the fallback path and
    any fenced/prose-wrapped reply still have to be parsed defensively.
    """
    depth = 0
    start = -1
    in_str = False
    escaped = False
    for i, ch in enumerate(text):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0:
                yield text[start : i + 1]


def _parse(text: str, yf_summary: str) -> Analysis:
    for candidate in _json_objects(text or ""):
        try:
            d = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict) or not (d.keys() & {"reason", "confidence", "description"}):
            continue
        confidence = str(d.get("confidence", "low")).strip().lower()
        return Analysis(
            reason=str(d.get("reason", "")).strip() or "No clear catalyst identified.",
            confidence=confidence if confidence in VALID_CONFIDENCE else "low",
            description=str(d.get("description", "")).strip() or _shorten(yf_summary),
        )
    return Analysis(description=_shorten(yf_summary))


def _shorten(summary: str, max_words: int = 30) -> str:
    words = (summary or "").split()
    return " ".join(words[:max_words]) + ("…" if len(words) > max_words else "")
