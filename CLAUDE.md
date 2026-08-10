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
- `src/analysis.py` — OpenAI Responses API (web_search tool) "why did it fall"
- `src/emailer.py` + `src/templates/email.html.j2` — report render + SMTP
- `config.yaml` — threshold, sector exclusions, revenue screen, caps
- `.github/workflows/daily-scan.yml` — cron 08:30 UTC Mon–Fri

## Test commands
```bash
pip install -r requirements.txt
pytest -q                                  # offline suite (no network/secrets)
python -m src.main --dry-run --limit 60    # small live run → out/report.html
python -m src.main --dry-run               # full universe (~2000 tickers, slow)
```
`tests/fakes.py` stubs yfinance, `requests` and the OpenAI client;
`tests/test_e2e.py` runs the whole orchestrator against them, so the pipeline is
fully testable with no egress. Add coverage there when changing `main.py`.
Open `out/report.html` in a browser to review email styling. A quiet-day render
can be forced by setting `threshold_pct: 99` temporarily.

## Env vars (GitHub secrets in CI, export locally to test)
Required for email: `SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASS EMAIL_FROM EMAIL_TO`
Optional: `OPENAI_API_KEY` (AI analysis), `AWS_*` + `DATALAKE_S3_BUCKET`/`DATALAKE_S3_PREFIX` (S3 lake).
Everything degrades gracefully when a secret is missing — the run still produces `out/report.html`.

## Gotchas
- Yahoo tickers are `<CODE>.AX`; some illiquid micro-caps have no data → skipped silently.
- The ASX directory access token in `universe.py` is the public one behind the
  asx.com.au download button; if it rotates, update it or rely on the fallback URL.
- `pct_change` is close-vs-previous-close of daily bars; on the run day yfinance's
  last bar must be today's — the workflow runs well after the 4:12pm closing auction.
  `prices._latest_session_only` enforces this: tickers whose last bar predates the
  newest bar in the batch are dropped, so a halted stock can't report a week-old fall.
- `find_fallers` is strict (`< -threshold`), matching "more than X%" in the config,
  README and email copy.
- Fundamentals are fetched in `main.main` *before* `screen_by_revenue` and the cap,
  so AI calls are only spent on stocks that survive both. Don't move the
  `get_fundamentals` call back inside `enrich`.
- `datalake` lists the S3 bucket **once per process** (`_listing_cache`) and reuses it
  for every ticker; listing per ticker made the lake scan dominate run time (~25s ×
  40 stocks). Keep any new S3 code path going through `_bucket_listing`.
- Email HTML is autoescaped — `select_autoescape` must keep matching the `.j2`
  suffix, or AI/web-search text can inject markup into the report.
- Email HTML must stay table-based with inline styles (Gmail/Outlook strip <style>).
- Keep AI responses parseable: `analysis.SCHEMA` is sent as a strict Structured
  Output *and* `analysis.SYSTEM` demands raw JSON, so the fallback path still
  works; `_parse` scans brace-balanced `{...}` spans as a fence-tolerant backstop.
- `analysis._request` retries once without `tools`/`text` if the model rejects
  them, so an ID that lacks web search or Structured Outputs still answers —
  watch the Actions log for that warning, it means answers aren't search-backed.
