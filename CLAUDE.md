# Claude Code guide — ASX Crash Scanner

Daily GitHub Actions pipeline: fetch ASX official list → compute daily % moves
via Yahoo Finance → flag falls > threshold → enrich (fundamentals + data-lake +
AI web-search analysis) → send a styled HTML email.

The pipeline is **split in CI**: `daily-scan.yml` scans in the Sydney evening
(10:00 UTC) and saves `out/` as an artifact; `daily-send.yml` emails it at
5am Sydney (19:00 UTC) via `--send-only`. The scan cannot run at delivery time
— see the Yahoo EOD gap in Gotchas.

## Layout
- `src/main.py` — orchestrator + CLI (`--dry-run`, `--limit N`)
- `src/universe.py` — ASX official list (primary + fallback URL)
- `src/prices.py` — batched yfinance download, faller filter
- `src/fundamentals.py` — market cap, EV, EV/Rev, EV/EBIT
- `src/datalake.py` — S3 + local-folder context scan
- `src/analysis.py` — OpenAI Responses API (web_search tool) "why did it fall"
- `src/emailer.py` + `src/templates/email.html.j2` — report render + SMTP
- `config.yaml` — threshold, sector exclusions, revenue screen, caps
- `.github/workflows/daily-scan.yml` — evening scan, cron 10:00 UTC Mon–Fri
  (8pm AEST); renders `out/report.html` + `out/meta.json`, uploads artifact
- `.github/workflows/daily-send.yml` — morning delivery, cron 19:00 UTC Mon–Fri
  (5am AEST); downloads the day's scan artifact, `python -m src.main --send-only`

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
- **Yahoo EOD gap**: while it is still the session's calendar day in Sydney,
  Yahoo serves that day's bar from its live feed; after Sydney midnight the bar
  vanishes from `period="7d"` daily history until Yahoo's EOD consolidation
  (2026-08-19: 1828/1832 tickers had no session bar at 5:32am AND 6:43am
  Sydney). Hence the scan runs in the Sydney evening and the email is delivered
  next morning by `--send-only` from the saved artifact. Never move the scan
  past Sydney midnight.
- `pct_change` is close-vs-previous-close of daily bars, and the report is
  **dated by the session** (`moves["date"].max()`), not the wall clock.
  `prices._latest_session_only` enforces freshness: tickers whose last bar
  predates the newest bar in the batch are dropped, so a halted stock can't
  report a week-old fall — and if more than `MAX_STALE_FRACTION` of tickers
  look stale it raises instead, because that pattern means broken data, and
  filtering it would email a false "quiet close".
- `daily-send.yml` picks the **newest successful scan run of the UTC day** —
  a manual `--limit` test scan left as the day's last run would become the
  morning email. `send_saved_report` also refuses reports older than
  `MAX_REPORT_AGE_HOURS` (20h), so a skipped scan can't replay yesterday.
- `find_fallers` is strict (`< -threshold`), matching "more than X%" in the config,
  README and email copy.
- Fundamentals are fetched in `main.main` *before* `screen_by_revenue` and the cap,
  so AI calls are only spent on stocks that survive both. Don't move the
  `get_fundamentals` call back inside `enrich`.
- `datalake.scan_s3` prefers the lake's own **manifest index** (from the
  hdcapital/market-ingestion repo): `market-data/manifests/<market>/<date>.jsonl`
  has one line per document with the ticker field. Constant request count
  forever; document keys are MD5 hashes, so the filename-matching walk finds
  **nothing** in that lake — never route lake reads back through the walk.
  The walk survives only as a fallback for manifest-less buckets.
- `datalake` lists the S3 bucket **once per process** (`_listing_cache`) and reuses it
  for every ticker; listing per ticker made the lake scan dominate run time (~25s ×
  40 stocks). Keep any new S3 code path going through `_bucket_listing`.
- `datalake.max_age_days` drops stale objects **as the listing streams past** —
  ListObjectsV2 has no server-side date filter, so this bounds memory and the cap,
  not the walk time. The age is part of the listing cache key. It is deliberately
  S3-only: local files are re-checked-out each run, so their mtimes are worthless.
- `datalake._walk` parallelises the listing: one Delimiter="/" probe finds the
  top-level folders, then each folder subtree is listed concurrently
  (MAX_LIST_WORKERS). Flat buckets and >MAX_FANOUT_FOLDERS layouts fall back to
  the sequential walk. Results are **sorted before caching** so which notes reach
  the AI never depends on thread timing — keep that sort. The request count still
  grows with the bucket; DATALAKE_S3_PREFIX is the only true lever on walk time,
  and a GROWTH_WARN_OBJECTS warning nags when the bucket passes 250k objects.
- Email HTML is autoescaped — `select_autoescape` must keep matching the `.j2`
  suffix, or AI/web-search text can inject markup into the report.
- Email HTML must stay table-based with inline styles (Gmail/Outlook strip <style>).
- Keep AI responses parseable: `analysis.SCHEMA` is sent as a strict Structured
  Output *and* `analysis.SYSTEM` demands raw JSON, so the fallback path still
  works; `_parse` scans brace-balanced `{...}` spans as a fence-tolerant backstop.
- The analysis prompt keeps the search **general** (any same-day news can be the
  cause) but names results/guidance explicitly among the candidates — a prompt
  that omitted earnings missed HSN's FY26 results on 2026-08-19 — and tells the
  model to fill `highlights`/`lowlights` only from something the company itself
  released that day; the email renders them as a "From today's announcement"
  block only when non-empty. `_str_list` caps and sanitises the arrays because
  the no-tools fallback isn't schema-checked.
- `get_fundamentals` retries Yahoo rate limits (RETRY_DELAYS pauses) with a
  process-wide breaker: once one ticker burns all retries, later tickers get a
  single quick attempt until a success re-arms it. A failed request sets
  `Fundamentals.fetch_failed` and `screen_by_revenue` then **fails open** —
  a confirmed faller must not vanish because enrichment errored (the 2026-08-19
  HSN incident: rate-limited fundamentals made 21 fallers read as
  revenue-unknown and the email said quiet day). `fetch_failed` is distinct
  from an answered-but-empty info dict, which still counts as unknown revenue.
  Tests never really sleep: `tests/conftest.py` autouse-patches
  `fundamentals._sleep` and resets the breaker.
- `analysis._request` retries once without `tools`/`text` if the model rejects
  them, so an ID that lacks web search or Structured Outputs still answers —
  watch the Actions log for that warning, it means answers aren't search-backed.
