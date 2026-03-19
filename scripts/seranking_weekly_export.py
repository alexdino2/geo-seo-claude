#!/usr/bin/env python3
"""
Export weekly detailed keyword rankings from SE Ranking for all projects.

Auth:
- --api-token argument OR SERANKING_API_TOKEN env var

Outputs:
- data/seranking/<YYYY-MM-DD>/<project-slug>-seranking-detailed-<YYYY-MM-DD>.csv
- data/seranking/<YYYY-MM-DD>/manifest-<YYYY-MM-DD>.json
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests

API_BASE = "https://api4.seranking.com"


def _slugify(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = value.strip("-")
    return value or "project"


def _iso_to_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _today() -> date:
    return date.today()


def _latest_saturday(today: Optional[date] = None) -> date:
    ref = today or _today()
    # Monday=0 ... Saturday=5 ... Sunday=6
    days_since_saturday = (ref.weekday() - 5) % 7
    return ref - timedelta(days=days_since_saturday)


def _window_for_report_date(report_date: date) -> Tuple[date, date]:
    # Sunday -> Saturday window ending at report_date
    return report_date - timedelta(days=6), report_date


class SERankingClient:
    def __init__(self, api_token: str, timeout_seconds: float = 60.0) -> None:
        self.api_token = api_token
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Token {api_token}",
                "Accept": "application/json",
            }
        )

    def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{API_BASE}{path}"
        last_error: Optional[Exception] = None
        for attempt in range(4):
            try:
                res = self.session.request(
                    method=method,
                    url=url,
                    params=params,
                    timeout=self.timeout_seconds,
                )
                if res.status_code == 429:
                    time.sleep(1.5 + attempt)
                    continue
                res.raise_for_status()
                if not res.text.strip():
                    return None
                return res.json()
            except Exception as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(1.0 + attempt)
                    continue
                raise RuntimeError(f"SE Ranking API request failed for {path}: {exc}") from exc
        raise RuntimeError(f"SE Ranking API request failed for {path}: {last_error}")

    def list_projects(self) -> List[Dict[str, Any]]:
        data = self._request("GET", "/sites")
        if isinstance(data, list):
            return data
        raise RuntimeError("Unexpected /sites response format")

    def list_search_engines(self, site_id: int) -> List[Dict[str, Any]]:
        data = self._request("GET", f"/sites/{site_id}/search-engines")
        if isinstance(data, list):
            return data
        raise RuntimeError(f"Unexpected /sites/{site_id}/search-engines response format")

    def get_positions(
        self,
        site_id: int,
        site_engine_id: int,
        date_from: str,
        date_to: str,
    ) -> List[Dict[str, Any]]:
        params = {
            "site_engine_id": site_engine_id,
            "date_from": date_from,
            "date_to": date_to,
            "with_landing_pages": 1,
            "with_serp_features": 1,
        }
        data = self._request("GET", f"/sites/{site_id}/positions", params=params)
        if isinstance(data, list):
            return data
        raise RuntimeError(f"Unexpected /sites/{site_id}/positions response format")


def _landing_page_for_date(landing_pages: List[Dict[str, Any]], check_date: str) -> str:
    for row in landing_pages:
        if str(row.get("date", "")) == check_date:
            return str(row.get("url", "") or "")
    return ""


def _flatten_positions(
    project: Dict[str, Any],
    search_engine: Dict[str, Any],
    positions_payload: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for bucket in positions_payload:
        keywords = bucket.get("keywords") or []
        for kw in keywords:
            keyword_text = kw.get("keyword", "")
            keyword_id = kw.get("id", "")
            target_url = kw.get("url", "") or kw.get("link", "")
            tags = kw.get("tags", []) or []
            positions = kw.get("positions") or []
            landing_pages = kw.get("landing_pages") or []
            serp_features = kw.get("serp_features") or []
            serp_features_str = ",".join([str(x) for x in serp_features]) if isinstance(serp_features, list) else ""
            tags_str = ",".join([str(x) for x in tags]) if isinstance(tags, list) else ""

            for pos_row in positions:
                check_date = str(pos_row.get("date", "") or "")
                rows.append(
                    {
                        "project_id": project.get("site_id", ""),
                        "project_title": project.get("title", ""),
                        "project_url": project.get("url", ""),
                        "site_engine_id": search_engine.get("site_engine_id", ""),
                        "search_engine_id": search_engine.get("search_engine_id", ""),
                        "search_engine_name": search_engine.get("name", ""),
                        "keyword_id": keyword_id,
                        "keyword": keyword_text,
                        "keyword_target_url": target_url,
                        "check_date": check_date,
                        "position": pos_row.get("pos", ""),
                        "position_change": pos_row.get("change", ""),
                        "price": pos_row.get("price", ""),
                        "is_map": pos_row.get("is_map", ""),
                        "map_position": pos_row.get("map_position", ""),
                        "paid_position": pos_row.get("paid_position", ""),
                        "landing_page_url": _landing_page_for_date(landing_pages, check_date),
                        "serp_features": serp_features_str,
                        "tags": tags_str,
                    }
                )
    return rows


def export_weekly_rankings(
    api_token: str,
    output_root: str,
    report_date: str,
    date_from: str,
    date_to: str,
    project_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    client = SERankingClient(api_token=api_token)
    projects = client.list_projects()
    output_dir = os.path.join(output_root, report_date)
    os.makedirs(output_dir, exist_ok=True)

    project_id_filter = set(project_ids or [])
    selected_projects = []
    for p in projects:
        site_id = int(p.get("site_id", 0) or 0)
        if site_id <= 0:
            continue
        if project_id_filter and site_id not in project_id_filter:
            continue
        selected_projects.append(p)

    manifest: Dict[str, Any] = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "report_date": report_date,
        "date_from": date_from,
        "date_to": date_to,
        "projects_total": len(selected_projects),
        "projects_exported": [],
        "projects_failed": [],
    }

    for project in selected_projects:
        site_id = int(project.get("site_id", 0) or 0)
        project_title = str(project.get("title", "") or project.get("url", "") or f"site-{site_id}")
        project_slug = _slugify(project_title)
        file_path = os.path.join(output_dir, f"{project_slug}-seranking-detailed-{report_date}.csv")

        try:
            search_engines = client.list_search_engines(site_id)
            all_rows: List[Dict[str, Any]] = []
            for se in search_engines:
                site_engine_id = int(se.get("site_engine_id", 0) or 0)
                if site_engine_id <= 0:
                    continue
                payload = client.get_positions(
                    site_id=site_id,
                    site_engine_id=site_engine_id,
                    date_from=date_from,
                    date_to=date_to,
                )
                all_rows.extend(_flatten_positions(project=project, search_engine=se, positions_payload=payload))
                # Stay under API rate limits.
                time.sleep(0.25)

            fieldnames = [
                "project_id",
                "project_title",
                "project_url",
                "site_engine_id",
                "search_engine_id",
                "search_engine_name",
                "keyword_id",
                "keyword",
                "keyword_target_url",
                "check_date",
                "position",
                "position_change",
                "price",
                "is_map",
                "map_position",
                "paid_position",
                "landing_page_url",
                "serp_features",
                "tags",
            ]

            with open(file_path, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for row in all_rows:
                    writer.writerow(row)

            manifest["projects_exported"].append(
                {
                    "site_id": site_id,
                    "title": project_title,
                    "file_path": file_path,
                    "rows": len(all_rows),
                    "search_engines": len(search_engines),
                }
            )
        except Exception as exc:
            manifest["projects_failed"].append(
                {
                    "site_id": site_id,
                    "title": project_title,
                    "error": str(exc),
                }
            )

    manifest_path = os.path.join(output_dir, f"manifest-{report_date}.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    return {
        "output_dir": output_dir,
        "manifest_path": manifest_path,
        "projects_total": manifest["projects_total"],
        "projects_exported": len(manifest["projects_exported"]),
        "projects_failed": len(manifest["projects_failed"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Export SE Ranking weekly detailed ranking reports for all projects.")
    parser.add_argument("--api-token", default="", help="SE Ranking API token. Defaults to SERANKING_API_TOKEN env var.")
    parser.add_argument("--output-root", default="data/seranking", help="Base output folder.")
    parser.add_argument("--report-date", default="", help="Week ending Saturday date (YYYY-MM-DD). Defaults to latest Saturday.")
    parser.add_argument("--date-from", default="", help="Start date (YYYY-MM-DD). Defaults to Sunday before report date.")
    parser.add_argument("--date-to", default="", help="End date (YYYY-MM-DD). Defaults to report date.")
    parser.add_argument(
        "--project-ids",
        default="",
        help="Optional comma-separated site IDs to export. If omitted, exports all projects.",
    )
    args = parser.parse_args()

    api_token = args.api_token.strip() or os.getenv("SERANKING_API_TOKEN", "").strip()
    if not api_token:
        print("Missing API token. Set SERANKING_API_TOKEN or pass --api-token.", file=sys.stderr)
        return 2

    report_date_obj = _iso_to_date(args.report_date.strip()) if args.report_date.strip() else _latest_saturday()
    date_from_obj, default_date_to_obj = _window_for_report_date(report_date_obj)
    date_from_obj = _iso_to_date(args.date_from.strip()) if args.date_from.strip() else date_from_obj
    date_to_obj = _iso_to_date(args.date_to.strip()) if args.date_to.strip() else default_date_to_obj

    project_ids: Optional[List[int]] = None
    if args.project_ids.strip():
        project_ids = []
        for raw in args.project_ids.split(","):
            raw = raw.strip()
            if not raw:
                continue
            project_ids.append(int(raw))

    result = export_weekly_rankings(
        api_token=api_token,
        output_root=args.output_root,
        report_date=report_date_obj.isoformat(),
        date_from=date_from_obj.isoformat(),
        date_to=date_to_obj.isoformat(),
        project_ids=project_ids,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

