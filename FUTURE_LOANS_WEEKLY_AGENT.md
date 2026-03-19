# Future Loans SEO and GEO Agent (Weekly)

This repo includes a weekly runner that productizes the existing GEO scoring into a repeatable output for:

1. New keyword opportunities
1b. Keyword position changes vs prior Saturday (rank deltas)
2. Pages losing rank
3. Pages with low CTR
4. AI visibility gaps (using GEO signals: citability, crawler access, `llms.txt`, and brand/entity checks where available)

## Where outputs go

By default the agent writes to:

- `~/.future-loans-agent/history/<domain>/YYYY-MM-DD.json` (persisted snapshots)
- `~/.future-loans-agent/outputs/<domain>/<YYYY-MM-DD>/future-loans-weekly-report-<YYYY-MM-DD>.md`
- `~/.future-loans-agent/outputs/<domain>/<YYYY-MM-DD>/future-loans-weekly-report-<YYYY-MM-DD>.json`

You can override the base folder with `--history-dir`.

## Input schema (metrics JSON)

Your n8n workflow should produce a JSON file shaped like this:

```json
{
  "date": "2026-03-19",
  "pages": [
    {
      "url": "https://example.com/some-page",
      "impressions": 1234,
      "clicks": 56,
      "ctr": 0.0456,
      "avg_position": 8.3
    }
  ],
  "queries": [
    {
      "query": "future loans",
      "impressions": 500,
      "clicks": 30,
      "ctr": 0.06,
      "avg_position": 12.4
    }
  ]
}
```

Notes:
- `ctr` can be either `0-1` ratio or `0-100` percent; the agent normalizes it.
- `avg_position` is the Search Console average position.
- `queries` and `pages` are optional but strongly recommended for all four outputs.

## Run manually (CLI)

Example:

```powershell
python scripts\future_loans_weekly_agent.py `
  --domain "futureloans.com" `
  --brand-name "Future Loans" `
  --metrics-current "C:\path\to\metrics-current.json" `
  --report-date "2026-03-19"
```

If you do not pass `--metrics-baseline`, the agent auto-uses the most recent snapshot saved in `history/` as the baseline for deltas.

## Threshold knobs (most important)

- `--impressions-min` (default `500`) : min impressions for page alerts
- `--ctr-max-percent` (default `1.5`) : low CTR threshold
- `--position-lost-min-delta` (default `2.0`) : rank drop threshold (avg_position worsened by at least this)
- `--geo-citability-top-pages` (default `10`) : number of pages to run citability checks on (most expensive step)
- `--impression-share-max-percent` (default `2.0`) : "low share of volume" threshold. Calculated as `query_impressions / total_query_impressions_in_snapshot` (percent).
- `--min-position-delta-abs` (default `1.0`) : include keyword in the rank-change table only if `abs(avg_position_now - avg_position_baseline) >= this`.
- `--position-change-top-n` (default `50`) : max keywords listed in the rank-change table.
- `--target-keywords-file` (optional) : path to a text file with one keyword per line to restrict analysis (opportunities + rank-change table).

## n8n weekly scheduling

Recommended n8n pattern:
1. Cron trigger weekly (recommended: run on Sunday after the window closes)
2. Build two date ranges:
   - `report_date` = the ending Saturday date of the reporting window (`YYYY-MM-DD`)
   - Current window = Sunday `report_date - 6 days` through Saturday `report_date`
   - Baseline window = Sunday `(report_date - 13 days)` through Saturday `(report_date - 7 days)`
3. Search Console module pulls latest snapshot for the *current* window (queries + pages) and writes:
   - `metrics-current.json`
4. Execute command step runs `python scripts/future_loans_weekly_agent.py ...` with:
   - `--report-date <ending Saturday date>`
   - (optionally) `--metrics-baseline` if you want to provide the prior snapshot explicitly; otherwise the agent auto-loads the prior Saturday snapshot from `history/`.
5. Use the agent output paths (`.md` / `.json`) to send email/Slack or update Google Sheets

If you want, paste your current n8n output format (sample JSON), and I’ll adapt the agent thresholds + mapping so the four sections match your definitions precisely.

## SE Ranking weekly pull (all projects)

Use this runbook every week to pull the latest detailed ranking report from `seranking.com` for every project.

1. Log in at `https://seranking.com`.
2. Open `Projects`.
3. For each project:
   - Open the project dashboard.
   - Go to `Rankings` (or `Detailed` rankings view).
   - Set the date to the most recent completed week ending Saturday.
   - Export the detailed ranking report (CSV or XLSX).
4. Save each export using this naming pattern:
   - `<project-slug>-seranking-detailed-<YYYY-MM-DD>.csv`
5. Store files in:
   - `data/seranking/<YYYY-MM-DD>/`
6. Confirm every active project has one file for the same report date before running downstream scripts.

### Optional automation notes

- Keep SE Ranking credentials in your secure vault and inject at runtime (do not commit credentials to git).
- If automating with n8n or a browser runner, loop through the full project list and retry failed exports once.
- Add a completion check that compares exported file count with active project count in SE Ranking.

## Full automation (SE Ranking + weekly GEO report)

This repo now includes two scripts:

- `scripts/seranking_weekly_export.py`
  - Pulls detailed weekly ranking data for all SE Ranking projects via API.
  - Writes project CSV files to `data/seranking/<YYYY-MM-DD>/`.
- `scripts/run_weekly_automation.py`
  - Runs SE Ranking export first, then runs `scripts/future_loans_weekly_agent.py`.
  - Use this as the single scheduled entrypoint.

### 1) One-time setup

Set your SE Ranking API token in an environment variable (recommended):

```powershell
setx SERANKING_API_TOKEN "your_api_token_here"
```

Get the token from SE Ranking account settings: `Settings -> API`.

### 2) Run end-to-end manually

```powershell
python scripts\run_weekly_automation.py `
  --domain "futureloans.com" `
  --brand-name "Future Loans" `
  --metrics-current "C:\path\to\metrics-current.json"
```

Optional flags:

- `--report-date YYYY-MM-DD` (defaults to latest Saturday)
- `--metrics-baseline C:\path\to\metrics-baseline.json`
- `--history-dir C:\path\to\future-loans-agent`
- `--target-keywords-file C:\path\to\target-keywords.txt`
- `--seranking-project-ids "123,456"` (limit SE Ranking export to specific project IDs)

### 3) Schedule weekly (Windows Task Scheduler)

- Trigger: weekly, Sunday morning (after Saturday reporting window closes).
- Action:
  - Program/script: `python`
  - Add arguments:

```text
scripts\run_weekly_automation.py --domain "futureloans.com" --brand-name "Future Loans" --metrics-current "C:\path\to\metrics-current.json"
```

If your Search Console snapshot is produced by n8n, schedule n8n first, then run this command when `metrics-current.json` is ready.

## Python script run instructions

### Local run (manual)

1. Create and activate a virtual environment (recommended).
2. Install dependencies:

```powershell
pip install -r requirements.txt
```

3. Set SE Ranking token:

```powershell
setx SERANKING_API_TOKEN "your_api_token_here"
```

4. Run the weekly automation script:

```powershell
python scripts\run_weekly_automation.py `
  --domain "futureloans.com" `
  --brand-name "Future Loans" `
  --metrics-current "C:\path\to\metrics-current.json"
```

### Script outputs

- SE Ranking CSV exports:
  - `data/seranking/<YYYY-MM-DD>/`
- Future Loans report snapshots and outputs:
  - `~/.future-loans-agent/history/<domain>/`
  - `~/.future-loans-agent/outputs/<domain>/<YYYY-MM-DD>/`

## Cloud Run recommendation

For unattended weekly runs in Google Cloud, use **Cloud Run Jobs** (recommended for scheduled batch scripts) instead of a long-running Cloud Run service.

### Why Cloud Run Jobs

- Runs on-demand and exits (perfect for weekly automation scripts).
- Works cleanly with Cloud Scheduler.
- No need to build an HTTP server wrapper around your script.

### Deploy as a Cloud Run Job (high-level)

1. Add a container image for this repo (Python + dependencies + scripts).
2. Push the image to Artifact Registry.
3. Create a Cloud Run Job that executes:

```bash
python scripts/run_weekly_automation.py --domain futureloans.com --brand-name "Future Loans" --metrics-current /workspace/data/metrics/metrics-current.json
```

4. Add required environment variables/secrets:
   - `SERANKING_API_TOKEN`
   - Any credentials needed for metrics ingestion step (if done inside the job).
5. Trigger weekly with Cloud Scheduler:
   - Schedule: Sunday after reporting window closes.
   - Target: `gcloud run jobs execute <job-name>`.

### Important Cloud Run note about `metrics-current.json`

`run_weekly_automation.py` expects `--metrics-current` to exist. In Cloud Run, use one of these patterns:

- Build `metrics-current.json` in a prior n8n/job step, then pass it in via mounted storage.
- Or extend the job container to fetch Search Console data first, write the metrics JSON, then run `run_weekly_automation.py`.

If you want, I can add a production-ready `Dockerfile` + `cloudbuild.yaml` + exact `gcloud` commands next.
