"""Render the HTML report and send it via SMTP."""
from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"

CONF_COLORS = {
    "high": "#1E7F4F",
    "medium": "#B27A16",
    "low": "#64707E",
    "none-found": "#8B95A3",
}


def render(context: dict) -> str:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        # The template is email.html.j2, so match on the ".j2" suffix too —
        # extension-only matching leaves autoescape off and lets AI-sourced text
        # (which summarises web search results) inject markup into the email.
        autoescape=select_autoescape(
            enabled_extensions=("html", "htm", "xml", "j2"),
            default_for_string=True,
            default=True,
        ),
    )
    return env.get_template("email.html.j2").render(**context)


def build_context(
    rows: list[dict],
    scanned: int,
    threshold: float,
    excluded: list[str],
    report_date: str,
    total_fallers: int | None = None,
) -> dict:
    """Shape the render context. `rows` is copied, not mutated.

    `total_fallers` is how many stocks tripped the threshold before
    `max_stocks_in_email` truncated the tail; it defaults to len(rows).
    """
    fallers = []
    for r in rows:
        r = dict(r)
        r["conf_color"] = CONF_COLORS.get(r.get("confidence", "low"), "#64707E")
        # gauge width: 15% fall ≈ 30% bar, capped at 100
        r["bar_pct"] = min(100, round(abs(r["pct_change"]) * 2))
        fallers.append(r)

    total = len(fallers) if total_fallers is None else total_fallers
    worst = fallers[0] if fallers else None
    return {
        "fallers": fallers,
        "scanned": scanned,
        "threshold": f"{threshold:g}",
        "excluded_note": ", ".join(excluded) if excluded else "",
        "report_date": report_date,
        "worst_ticker": worst["ticker"] if worst else "—",
        "worst_pct": worst["pct_str"] if worst else "—",
        "total_fallers": total,
        "truncated": max(0, total - len(fallers)),
    }


def _env(name: str, default: str | None = None) -> str | None:
    """Environment lookup that treats a blank value as unset.

    GitHub Actions exports an unset secret as an empty string rather than
    omitting it, so `os.environ.get(name, fallback)` never reaches the fallback
    and downstream code gets "" — an empty From header, or int("") for the port.
    """
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def _require(name: str) -> str:
    value = _env(name)
    if not value:
        raise RuntimeError(f"{name} is not set — cannot send email")
    return value


def send(html: str, subject: str) -> None:
    host = _require("SMTP_HOST")
    port = int(_env("SMTP_PORT", "587"))
    user = _require("SMTP_USER")
    password = _require("SMTP_PASS")
    sender = _env("EMAIL_FROM") or user
    to = [a.strip() for a in _require("EMAIL_TO").split(",") if a.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg.attach(MIMEText("Your email client can't display HTML — open the report artifact instead.", "plain"))
    msg.attach(MIMEText(html, "html"))

    # 465 is implicit TLS (SMTPS); STARTTLS on it fails against most providers.
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=60) as s:
            s.login(user, password)
            s.sendmail(sender, to, msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=60) as s:
            s.starttls()
            s.login(user, password)
            s.sendmail(sender, to, msg.as_string())
    log.info("Email sent to %s", to)
