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
