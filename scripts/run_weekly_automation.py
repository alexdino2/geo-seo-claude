#!/usr/bin/env python3
"""
Run weekly automation end-to-end:
1) Export SE Ranking detailed ranking reports for all projects.
2) Run Future Loans weekly SEO/GEO agent report.

This wrapper is schedule-friendly for Task Scheduler, cron, or n8n.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from typing import Any, Dict, List


def _iso_to_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _latest_saturday(today: date) -> date:
    # Monday=0 ... Saturday=5 ... Sunday=6
    return today - timedelta(days=(today.weekday() - 5) % 7)


def _run_command(cmd: List[str]) -> Dict[str, Any]:
    proc = subprocess.run(cmd, text=True, capture_output=True)
    return {
        "command": cmd,
        "exit_code": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run SE Ranking export and Future Loans weekly report in one command.")
    parser.add_argument("--report-date", default="", help="Week ending Saturday date (YYYY-MM-DD). Defaults to latest Saturday.")
    parser.add_argument("--domain", required=True, help="Domain for Future Loans report (example: futureloans.com).")
    parser.add_argument("--brand-name", default="", help="Brand name for Future Loans report.")
    parser.add_argument("--metrics-current", required=True, help="Path to current Search Console metrics JSON.")
    parser.add_argument("--metrics-baseline", default="", help="Optional baseline metrics JSON.")
    parser.add_argument("--history-dir", default="", help="Optional Future Loans output/history base directory.")
    parser.add_argument("--target-keywords-file", default="", help="Optional path to target keywords file.")
    parser.add_argument("--seranking-output-root", default="data/seranking", help="Output root for SE Ranking exports.")
    parser.add_argument("--seranking-project-ids", default="", help="Optional comma-separated SE Ranking project IDs.")
    parser.add_argument("--skip-seranking", action="store_true", help="Skip the SE Ranking export step.")
    parser.add_argument("--skip-future-loans", action="store_true", help="Skip the Future Loans report step.")
    args = parser.parse_args()

    if args.skip_seranking and args.skip_future_loans:
        print("Nothing to do: both steps were skipped.", file=sys.stderr)
        return 2

    report_date_obj = _iso_to_date(args.report_date.strip()) if args.report_date.strip() else _latest_saturday(date.today())
    report_date = report_date_obj.isoformat()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    python_exe = sys.executable

    steps: List[Dict[str, Any]] = []

    if not args.skip_seranking:
        seranking_cmd = [
            python_exe,
            os.path.join(script_dir, "seranking_weekly_export.py"),
            "--report-date",
            report_date,
            "--output-root",
            args.seranking_output_root,
        ]
        if args.seranking_project_ids.strip():
            seranking_cmd.extend(["--project-ids", args.seranking_project_ids.strip()])
        seranking_result = _run_command(seranking_cmd)
        steps.append({"step": "seranking_export", **seranking_result})
        if seranking_result["exit_code"] != 0:
            print(json.dumps({"report_date": report_date, "steps": steps}, ensure_ascii=False))
            return seranking_result["exit_code"]

    if not args.skip_future_loans:
        future_cmd = [
            python_exe,
            os.path.join(script_dir, "future_loans_weekly_agent.py"),
            "--domain",
            args.domain,
            "--brand-name",
            args.brand_name,
            "--metrics-current",
            args.metrics_current,
            "--report-date",
            report_date,
        ]
        if args.metrics_baseline.strip():
            future_cmd.extend(["--metrics-baseline", args.metrics_baseline.strip()])
        if args.history_dir.strip():
            future_cmd.extend(["--history-dir", args.history_dir.strip()])
        if args.target_keywords_file.strip():
            future_cmd.extend(["--target-keywords-file", args.target_keywords_file.strip()])

        future_result = _run_command(future_cmd)
        steps.append({"step": "future_loans_weekly", **future_result})
        if future_result["exit_code"] != 0:
            print(json.dumps({"report_date": report_date, "steps": steps}, ensure_ascii=False))
            return future_result["exit_code"]

    print(json.dumps({"report_date": report_date, "steps": steps}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

