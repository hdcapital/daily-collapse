# ASX Fall Wire 📉

Every trading day after the Sydney close, this repo scans the **official ASX
listed-companies directory**, finds every stock that fell **more than 15%**,
works out **why** (AI + web search + your data lake), and emails you a
beautifully formatted report with a short business description, market cap,
EV/Revenue and EV/EBIT for each name.

## One-command setup

With the [GitHub CLI](https://cli.github.com) installed and logged in, from this folder:

```bash
gh repo create asx-crash-scanner --private --source=. --push
```

Then add your secrets (one command each, or via repo **Settings → Secrets → Actions**):

```bash
gh secret set SMTP_HOST   --body "smtp.gmail.com"
gh secret set SMTP_PORT   --body "587"
gh secret set SMTP_USER   --body "you@gmail.com"
gh secret set SMTP_PASS   --body "your-gmail-app-password"
gh secret set EMAIL_FROM  --body "you@gmail.com"
gh secret set EMAIL_TO    --body "you@gmail.com"
gh secret set ANTHROPIC_API_KEY --body "sk-ant-..."   # enables the AI "why it fell" analysis
```

Optional S3 data-lake scan:

```bash
gh secret set AWS_ACCESS_KEY_ID --body "..."
gh secret set AWS_SECRET_ACCESS_KEY --body "..."
gh secret set AWS_DEFAULT_REGION --body "ap-southeast-2"
gh secret set DATALAKE_S3_BUCKET --body "my-research-lake"
gh secret set DATALAKE_S3_PREFIX --body "notes/"
```

Done. The workflow fires **08:30 UTC Mon–Fri** (6:30pm AEST / 7:30pm AEDT —
comfortably after the 4:12pm closing auction). Trigger a test run any time from
the **Actions** tab → *ASX Daily Crash Scan* → *Run workflow* (tick **dry run**
to get the HTML as a downloadable artifact instead of an email).

> Gmail note: use an [App Password](https://myaccount.google.com/apppasswords),
> not your normal password. Any SMTP provider works (SES, Fastmail, Resend SMTP…).

## Turning on sector exclusions

Edit `config.yaml`:

```yaml
exclude_sectors:
  enabled: true
  sectors:
    - Materials
    - Energy
```

Sector names follow the ASX directory's *GICS industry group* column. The email
footer notes which sectors were muted so you always know the filter is live.
Other knobs in the same file: `threshold_pct`, `min_market_cap_aud`,
`max_stocks_in_email`, and AI/data-lake toggles.

## What the email looks like

Each flagged stock gets a card: ticker + company name + sector, a red
**drawdown gauge** scaled to the fall, prev close → close and volume, an AI
**"Why it fell"** verdict with a confidence badge (backed by live web search of
ASX announcements plus any hits from your data lake), a one-line business
description, and a metrics strip: **Mkt cap · EV · EV/Rev · EV/EBIT**. On a
quiet day you get a short "no falls beyond threshold" note instead.

## Data lake

Two sources, both optional and merged:

* **S3** — set the `DATALAKE_*` secrets; files whose key contains the ticker are
  pulled (text formats, ≤2MB) and snippets fed to the AI.
* **Local** — drop text/markdown notes into `datalake_sample/` (or repoint
  `datalake.local_dir` in config); any file mentioning the ticker is used.

## Testing with Claude Code

Open the repo in Claude Code — `CLAUDE.md` tells it everything. Quick manual loop:

```bash
pip install -r requirements.txt
pytest -q                                # offline suite — no network, no secrets
python -m src.main --dry-run --limit 60  # small live run → open out/report.html
```

The suite fakes yfinance, `requests` and the Anthropic client (`tests/fakes.py`),
so `tests/test_e2e.py` drives the whole universe → prices → filters →
fundamentals → data lake → analysis → render path without egress. It also runs
in CI on every push and as a gate before the daily scan sends anything.

## Caveats

Prices come from Yahoo Finance and can occasionally lag or misprice illiquid
micro-caps; fundamentals coverage is patchy for small resource explorers (fields
show "—" when unavailable). Stocks whose most recent Yahoo bar predates the
latest session — halted or simply untraded names — are skipped rather than
reported with a stale move. Nothing here is financial advice.
