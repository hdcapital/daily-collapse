"""Ask Claude (with web search) why a stock sold off, blending data-lake context."""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

MAX_CONTINUATIONS = 4  # guards against a pause_turn loop that never settles
VALID_CONFIDENCE = {"high", "medium", "low", "none-found"}

SYSTEM = (
    "You are an equity analyst covering the ASX. Be factual and terse. "
    "Never invent announcements; if no cause is found, say so plainly. "
    "Respond ONLY with a JSON object, no markdown fences, with keys: "
    "reason (string, <=60 words, why the stock fell today, cite the trigger if found), "
    "confidence (one of: high, medium, low, none-found), "
    "description (string, <=30 words, plain-English what the company does)."
)


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
    if not ai.get("enabled", True) or not os.environ.get("ANTHROPIC_API_KEY"):
        return Analysis(description=_shorten(yf_summary))
    try:
        import anthropic

        client = anthropic.Anthropic()
        lake = "\n\n".join(f"[{h['source']}]\n{h['text']}" for h in lake_context) or "(no data-lake hits)"
        prompt = (
            f"{name} (ASX:{ticker}) closed down {pct_change:.1f}% on {date}.\n\n"
            f"Company summary on file:\n{yf_summary[:1200] or '(none)'}\n\n"
            f"Internal data-lake extracts:\n{lake[:6000]}\n\n"
            "Search the web for today's ASX announcements, trading halts, capital "
            "raisings, downgrades, drill results or news explaining the fall, then "
            "answer in the required JSON."
        )
        request = dict(
            model=ai.get("model", "claude-sonnet-4-6"),
            max_tokens=800,
            system=SYSTEM,
            tools=[
                {
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": int(ai.get("max_web_searches_per_stock", 3)),
                }
            ],
        )
        messages = [{"role": "user", "content": prompt}]
        text = ""
        # The server-side web-search loop can stop with `pause_turn` before it has
        # written an answer; re-sending the turn resumes it where it left off.
        for _ in range(MAX_CONTINUATIONS):
            msg = client.messages.create(messages=messages, **request)
            text += "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            if getattr(msg, "stop_reason", None) != "pause_turn":
                break
            messages = [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": msg.content},
            ]
        else:
            log.warning("AI analysis for %s still paused after %d turns", ticker, MAX_CONTINUATIONS)
        return _parse(text, yf_summary)
    except Exception as e:  # noqa: BLE001
        log.warning("AI analysis failed for %s: %s", ticker, e)
        return Analysis(description=_shorten(yf_summary))


def _json_objects(text: str):
    """Yield candidate ``{...}`` spans, brace-balanced and quote-aware.

    A single greedy regex spans from the first ``{`` to the last ``}``, so any
    stray braces in prose after the object (common when the model appends a
    citation) break the parse.
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
