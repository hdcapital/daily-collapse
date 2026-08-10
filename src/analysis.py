"""Ask Claude (with web search) why a stock sold off, blending data-lake context."""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

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
        msg = client.messages.create(
            model=ai.get("model", "claude-sonnet-4-6"),
            max_tokens=800,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tools=[
                {
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": int(ai.get("max_web_searches_per_stock", 3)),
                }
            ],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return _parse(text, yf_summary)
    except Exception as e:  # noqa: BLE001
        log.warning("AI analysis failed for %s: %s", ticker, e)
        return Analysis(description=_shorten(yf_summary))


def _parse(text: str, yf_summary: str) -> Analysis:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return Analysis(description=_shorten(yf_summary))
    try:
        d = json.loads(m.group(0))
        return Analysis(
            reason=str(d.get("reason", "")).strip() or "No clear catalyst identified.",
            confidence=str(d.get("confidence", "low")),
            description=str(d.get("description", "")).strip() or _shorten(yf_summary),
        )
    except json.JSONDecodeError:
        return Analysis(description=_shorten(yf_summary))


def _shorten(summary: str, max_words: int = 30) -> str:
    words = (summary or "").split()
    return " ".join(words[:max_words]) + ("…" if len(words) > max_words else "")
