# Claude Code guide — ASX Crash Scanner

Daily GitHub Actions pipeline: fetch ASX official list → compute daily % moves
via Yahoo Finance → flag falls > threshold → enrich (fundamentals + data-lake +
AI web-search analysis) → send a styled HTML email.

## Layout
- `src/main.py` — orchestrator + CLI (`--dry-run`, `--limit N`)
- `src/universe.py` — ASX official list (primary + fallback URL)
- `src/prices.py` — batched yfinance download, faller filter
- `src/fundamentals.py` — market cap, EV, EV/Rev, EV/EBIT
- `src/datalake.py` — S3 + local-folder context scan
- `src/analysis.py` — Anthropic API (web search tool) "why did it fall"
- `src/emailer.py` + `src/templates/email.html.j2` — report render + SMTP
- `config.yaml` — threshold, sector exclusions, caps
- `.github/workflows/daily-scan.yml` — cron 08:30 UTC Mon–Fri

## Test commands
```bash
pip install -r requirements.txt
pytest -q                                  # offline unit tests
python -m src.main --dry-run --limit 60    # small live run → out/report.html
python -m src.main --dry-run               # full universe (~2000 tickers, slow)
```
Open `out/report.html` in a browser to review email styling. A quiet-day render
can be forced by setting `threshold_pct: 99` temporarily.

## Env vars (GitHub secrets in CI, export locally to test)
Required for email: `SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASS EMAIL_FROM EMAIL_TO`
Optional: `ANTHROPIC_API_KEY` (AI analysis), `AWS_*` + `DATALAKE_S3_BUCKET`/`DATALAKE_S3_PREFIX` (S3 lake).
Everything degrades gracefully when a secret is missing — the run still produces `out/report.html`.

## Gotchas
- Yahoo tickers are `<CODE>.AX`; some illiquid micro-caps have no data → skipped silently.
- The ASX directory access token in `universe.py` is the public one behind the
  asx.com.au download button; if it rotates, update it or rely on the fallback URL.
- `pct_change` is close-vs-previous-close of daily bars; on the run day yfinance's
  last bar must be today's — the workflow runs well after the 4:12pm closing auction.
- Email HTML must stay table-based with inline styles (Gmail/Outlook strip <style>).
- Keep AI responses parseable: `analysis.SYSTEM` demands raw JSON; `_parse` regexes
  the first `{...}` block as a fence-tolerant fallback.
