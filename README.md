# Construction Corner Ops Dashboard — Data Pipeline

Pulls Roller booking/payment/membership data every 6 hours, aggregates it
into a single `data/summary.json`, and commits if changed. The dashboard
itself lives as a Claude.ai artifact and reads `summary.json` +
`qb_closed.json` over HTTPS from this repo (raw.githubusercontent.com).

## Setup (one-time, ~5 minutes)

### 1. Create an empty **public** GitHub repo

Public is required so the artifact can fetch `raw.githubusercontent.com/...`
without auth. The data is non-sensitive (aggregated counts, no PII beyond
booking names already visible in your Roller venue).

### 2. Push this code to `main`

```bash
git init
git add .
git commit -m "Initial scaffold"
git branch -M main
git remote add origin https://github.com/{user}/{repo}.git
git push -u origin main
```

### 3. Add Actions secrets

Repo → Settings → Secrets and variables → Actions → **New repository
secret**. Values come from your existing `config/roller.env`:

| Name                   | Example                                |
| ---------------------- | -------------------------------------- |
| `ROLLER_CLIENT_ID`     | (your client id)                       |
| `ROLLER_CLIENT_SECRET` | (your client secret)                   |
| `ROLLER_API_BASE`      | `https://api.roller.app` (or whatever) |

### 4. Run the workflow once manually

Actions tab → "Refresh dashboard data" → **Run workflow** → branch `main` →
green button. First run takes 3–6 minutes (no token cache yet, full
backfill across all resources). When it finishes you'll see a new commit
on `main` adding `data/summary.json`.

If anything fails, click into the run and read the logs — the most likely
issue is a bad/missing secret.

### 5. Return to the Claude.ai chat where you ran this rebuild

Tell Claude: **"`data/summary.json` is live in the repo: github.com/{user}/{repo}"**.
Claude will then build the dashboard artifact in that chat.

**Bookmark that chat.** Opening the bookmark = opening the dashboard.

## Monthly maintenance: add a closed month

After each month's books close in QuickBooks, append one entry to
`data/qb_closed.json`:

```json
{
  "ym": "2026-05",
  "label": "May 26",
  "revenue": 12345.67,
  "cogs": 1234.56,
  "gross": 11111.11,
  "expenses": 9999.99,
  "net": 1111.12
}
```

Commit and push. The dashboard picks it up on next load. The closed-month
rule means: months strictly before the current month use these firm
actuals; the current month uses MTD + a blended projection from
`summary.json`.

## Architecture

```
.github/workflows/refresh.yml   cron 0 */6 * * *  +  workflow_dispatch
pipeline/
  roller_pull.py                OAuth, paginated pulls, 7 date-windowed
                                + 1 single-date + 5 reference resources.
                                Honors ROLLER_CACHE_DIR for output dir.
  build_summary.py              Aggregator. Reads ROLLER_CACHE_DIR,
                                writes to SUMMARY_OUT (default
                                data/summary.json).
  requirements.txt              Empty — scripts are stdlib-only.
data/
  summary.json                  Written by CI every 6h.
  qb_closed.json                Hand-edited monthly.
```

The 15+ Roller intermediate JSONs (bookingitems, bookingpayments, products,
tickets, customers, giftcards, discounts, gx_scores, membership_redemptions,
modifiers, reporting_categories, roles, devices, _meta) live at
`/tmp/roller_cache` during CI and are preserved across runs via
`actions/cache`. They are never committed. The OAuth token also lives
there and is reused while valid (24h lifetime, refreshed in-script).

## Local dev

If you want to run the pipeline locally (e.g. for debugging):

```bash
export ROLLER_CLIENT_ID=...
export ROLLER_CLIENT_SECRET=...
export ROLLER_API_BASE=https://api.roller.app
python pipeline/roller_pull.py
python pipeline/build_summary.py
```

By default this writes intermediate JSONs to `data/cache/` (gitignored)
and the final output to `data/summary.json`. Override either with:

```bash
ROLLER_CACHE_DIR=/tmp/roller_cache SUMMARY_OUT=/tmp/summary.json \
  python pipeline/build_summary.py
```

## Troubleshooting

**Workflow fails on first run with "Auth failed".** Check that all three
secrets are set and that `ROLLER_API_BASE` is the bare URL (no trailing
slash).

**Workflow runs but no commit happens.** That's expected when nothing
changed since the last run — see the "summary.json unchanged" log line.

**Dashboard shows stale data.** The artifact's freshness banner shows the
age of `summary.json`. If it's > 12h old, look at the Actions tab for
recent failures. GitHub Actions cron is best-effort and can skip runs on
quiet repos — clicking *Run workflow* manually always works.
