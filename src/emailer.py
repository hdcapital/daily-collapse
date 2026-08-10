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
        autoescape=select_autoescape(["html"]),
    )
    return env.get_template("email.html.j2").render(**context)


def build_context(rows: list[dict], scanned: int, threshold: float, excluded: list[str], report_date: str) -> dict:
    for r in rows:
        r["conf_color"] = CONF_COLORS.get(r.get("confidence", "low"), "#64707E")
        # gauge width: 15% fall ≈ 30% bar, capped at 100
        r["bar_pct"] = min(100, round(abs(r["pct_change"]) * 2))
    worst = rows[0] if rows else None
    return {
        "fallers": rows,
        "scanned": scanned,
        "threshold": f"{threshold:g}",
        "excluded_note": ", ".join(excluded) if excluded else "",
        "report_date": report_date,
        "worst_ticker": worst["ticker"] if worst else "—",
        "worst_pct": worst["pct_str"] if worst else "—",
    }


def send(html: str, subject: str) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    sender = os.environ.get("EMAIL_FROM", user)
    to = [a.strip() for a in os.environ["EMAIL_TO"].split(",")]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg.attach(MIMEText("Your email client can't display HTML — open the report artifact instead.", "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(host, port, timeout=60) as s:
        s.starttls()
        s.login(user, password)
        s.sendmail(sender, to, msg.as_string())
    log.info("Email sent to %s", to)
