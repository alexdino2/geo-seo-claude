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

