#!/usr/bin/env python3
"""
Future Loans SEO and GEO Agent (weekly)

Inputs:
- Search Console snapshot JSON for "current week"
- Optional previous snapshot JSON for week-over-week comparisons

Outputs (persisted locally):
- Weekly markdown report
- Weekly JSON report (machine-readable)
- Persisted history snapshots to compute deltas automatically

This script is designed to be called by your n8n workflow.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

# Make sibling scripts importable
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import brand_scanner
import citability_scorer
import fetch_page
import llmstxt_generator


TIER1_CRAWLERS_DEFAULT = [
    "GPTBot",
    "OAI-SearchBot",
    "ChatGPT-User",
    "ClaudeBot",
    "PerplexityBot",
    "Google-Extended",
    "GoogleOther",
]

SECONDARY_CRAWLERS_DEFAULT = [
    "anthropic-ai",
    "CCBot",
    "Bytespider",
    "cohere-ai",
    "Applebot-Extended",
    "FacebookBot",
    "Amazonbot",
]


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def normalize_ctr_percent(ctr: Any) -> float:
    """
    Normalize CTR to percent (0-100).

    Accepts:
    - 0.0123 (ratio) -> 1.23
    - 1.23 (percent) -> 1.23
    - 123 (percent) -> 123
    """
    ctr_val = _safe_float(ctr, 0.0)
    if ctr_val <= 1.0:
        return ctr_val * 100.0
    return ctr_val


def parse_base_url(domain_or_url: str) -> str:
    """
    Return a base URL like https://example.com (always includes scheme).
    """
    domain_or_url = domain_or_url.strip()
    if domain_or_url.startswith("http://") or domain_or_url.startswith("https://"):
        parsed = urlparse(domain_or_url)
        return f"{parsed.scheme}://{parsed.netloc}"
    return f"https://{domain_or_url}"


def today_iso() -> str:
    return date.today().isoformat()


def citability_grade_from_average(avg_score: float) -> Tuple[str, str]:
    """
    Map page-level average citability to a grade/label using
    the same thresholds as score_passage().
    """
    if avg_score >= 80:
        return "A", "Highly Citable"
    if avg_score >= 65:
        return "B", "Good Citability"
    if avg_score >= 50:
        return "C", "Moderate Citability"
    if avg_score >= 35:
        return "D", "Low Citability"
    return "F", "Poor Citability"


def ensure_metrics_shape(metrics: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize expected fields:
    - metrics["date"] optional
    - metrics["pages"] list of page rows
    - metrics["queries"] list of query rows
    """
    if "pages" not in metrics:
        metrics["pages"] = []
    if "queries" not in metrics:
        metrics["queries"] = []
    if "date" not in metrics or not metrics["date"]:
        metrics["date"] = today_iso()
    return metrics


def index_pages(pages: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    by_url: Dict[str, Dict[str, Any]] = {}
    for p in pages:
        url = (p.get("url") or p.get("page") or p.get("page_url") or "").strip()
        if not url:
            continue
        by_url[url] = p
    return by_url


def index_queries(queries: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    by_query: Dict[str, Dict[str, Any]] = {}
    for q in queries:
        query = (q.get("query") or q.get("term") or q.get("keyword") or "").strip()
        if not query:
            continue
        by_query[query] = q
    return by_query


def format_rank_delta(delta: float) -> str:
    if delta > 0:
        return f"+{delta:.1f}"
    if delta < 0:
        return f"{delta:.1f}"
    return "0.0"


def opportunity_score_query(
    impressions: float,
    ctr_percent: float,
    avg_position: float,
    position_min: float,
    position_max: float,
    ctr_max: float,
) -> float:
    """
    Heuristic score for "new keyword opportunities".
    Higher when:
    - impressions are strong
    - position is in a "reachable but not top" band
    - CTR is below your CTR ceiling (means optimization can unlock clicks)
    """
    if impressions <= 0:
        return 0.0

    if avg_position < position_min or avg_position > position_max:
        return 0.0

    pos_span = max(1e-6, position_max - position_min)
    # Position factor: best when close to position_min (e.g., 4), worst near position_max
    pos_factor = (position_max - avg_position) / pos_span  # 0..1+
    pos_factor = max(0.0, min(1.0, pos_factor))

    # CTR deficit factor: 1 when ctr=0, 0 when ctr=ctr_max+
    ctr_factor = max(0.0, (ctr_max - ctr_percent) / max(1e-6, ctr_max))
    ctr_factor = max(0.0, min(1.0, ctr_factor))

    return impressions * pos_factor * ctr_factor


def compute_crawler_access_score(robots: Dict[str, Any], tier1: List[str], secondary: List[str]) -> Dict[str, Any]:
    """
    Port the GEO AI visibility scoring idea into a practical score:
    - Start at 100
    - Deduct 15 for each critical crawler blocked
    - Deduct 5 for each secondary crawler blocked
    - Deduct 10 if no sitemap is referenced
    """
    ai_status = robots.get("ai_crawler_status", {}) or {}
    sitemaps = robots.get("sitemaps", []) or []

    def is_blocked(status: str) -> bool:
        return status in ("BLOCKED", "PARTIALLY_BLOCKED", "BLOCKED_BY_WILDCARD")

    critical_blocked = []
    secondary_blocked = []

    for c in tier1:
        if is_blocked(ai_status.get(c, "")):
            critical_blocked.append(c)
    for c in secondary:
        if is_blocked(ai_status.get(c, "")):
            secondary_blocked.append(c)

    score = 100.0
    score -= 15.0 * len(critical_blocked)
    score -= 5.0 * len(secondary_blocked)
    if not sitemaps:
        score -= 10.0
    score = max(0.0, score)

    return {
        "score": round(score, 1),
        "critical_blocked": critical_blocked,
        "secondary_blocked": secondary_blocked,
        "sitemaps_found": len(sitemaps),
    }


def compute_llms_score(llms_validate: Dict[str, Any]) -> Dict[str, Any]:
    """
    Compute a practical 0-100 score using validate_llmstxt output.
    """
    exists = bool(llms_validate.get("exists", False))
    format_valid = bool(llms_validate.get("format_valid", False))
    section_count = int(llms_validate.get("section_count", 0) or 0)
    link_count = int(llms_validate.get("link_count", 0) or 0)
    full_exists = bool(llms_validate.get("full_version", {}).get("exists", False))

    if not exists:
        return {"score": 0, "reason": "absent"}
    if not format_valid:
        return {"score": 30, "reason": "present but malformed", "section_count": section_count, "link_count": link_count}

    score = 50.0
    completeness_ok = section_count >= 2 and link_count >= 5
    if completeness_ok:
        score = 70.0
    if full_exists:
        score = 90.0
    score = min(100.0, score)

    return {
        "score": int(score),
        "reason": "valid",
        "section_count": section_count,
        "link_count": link_count,
        "full_version_exists": full_exists,
    }


def compute_brand_mention_score(brand_report: Dict[str, Any]) -> Dict[str, Any]:
    """
    Score 0-100 using the weights from geo-ai-visibility.md.

    Note: Many platform checks in scripts/brand_scanner.py are "framework only"
    (no live API checks). We avoid inventing results; those platforms remain 0.
    """
    platforms = (brand_report.get("platforms") or {})

    youtube = platforms.get("youtube") or {}
    reddit = platforms.get("reddit") or {}
    wiki = platforms.get("wikipedia") or {}
    linkedin = platforms.get("linkedin") or {}
    other = platforms.get("other") or {}

    wikipedia_points = 30.0 if wiki.get("has_wikipedia_page") else 0.0
    if wiki.get("has_wikidata_entry") and not wiki.get("has_wikipedia_page"):
        wikipedia_points = 15.0

    reddit_points = 20.0 if reddit.get("has_subreddit") or reddit.get("mentioned_in_discussions") else 0.0
    youtube_points = 15.0 if youtube.get("has_channel") else 0.0
    linkedin_points = 10.0 if linkedin.get("has_company_page") else 0.0

    # "Other Platforms" script does not actually verify presence.
    other_points = 0.0

    total = wikipedia_points + reddit_points + youtube_points + linkedin_points + other_points
    total = max(0.0, min(100.0, total))

    return {
        "score": round(total, 1),
        "components": {
            "wikipedia_points": wikipedia_points,
            "reddit_points": reddit_points,
            "youtube_points": youtube_points,
            "linkedin_points": linkedin_points,
            "other_points": other_points,
        },
    }


def compute_ai_visibility_score(
    citability_score: float,
    brand_score: float,
    crawler_score: float,
    llms_score: float,
) -> Dict[str, Any]:
    ai_visibility = (citability_score * 0.35) + (brand_score * 0.30) + (crawler_score * 0.25) + (llms_score * 0.10)
    ai_visibility = max(0.0, min(100.0, ai_visibility))

    if ai_visibility <= 20:
        tier = "Critical — Virtually invisible to AI search engines"
    elif ai_visibility <= 40:
        tier = "Poor — Minimal AI discoverability"
    elif ai_visibility <= 60:
        tier = "Fair — Some AI visibility but significant gaps"
    elif ai_visibility <= 80:
        tier = "Good — Solid AI presence with room for improvement"
    else:
        tier = "Excellent — Strong AI search visibility"

    return {
        "ai_visibility_score": round(ai_visibility, 1),
        "tier": tier,
    }


def markdown_report(
    agent_name: str,
    domain: str,
    brand_name: str,
    report_date: str,
    keyword_opportunities: List[Dict[str, Any]],
    keyword_rank_changes: List[Dict[str, Any]],
    pages_losing_rank: List[Dict[str, Any]],
    low_ctr_pages: List[Dict[str, Any]],
    ai_visibility: Dict[str, Any],
    ai_gaps: List[str],
    geo_page_citability: List[Dict[str, Any]],
    baseline_date: str,
) -> str:
    lines: List[str] = []
    lines.append(f"# {agent_name} — Weekly Report")
    lines.append("")
    lines.append(f"**Domain:** {domain}")
    lines.append(f"**Brand:** {brand_name or '(not provided)'}")
    lines.append(f"**Week ending:** {report_date}")
    lines.append("")
    lines.append("---")
    lines.append("")

    lines.append("## 1) New keyword opportunities")
    lines.append("")
    lines.append("_Includes keywords with low share of your site impressions (within the snapshot), plus position/CTR reachability._")
    if not keyword_opportunities:
        lines.append("")
        lines.append("_None met your thresholds this week._")
    else:
        for i, item in enumerate(keyword_opportunities, start=1):
            lines.append(
                f"{i}. `{item['query']}` — Impr {item['impressions']}, Share {item.get('impression_share_percent', 0.0):.2f}%, Pos {item['avg_position']:.1f}, CTR {item['ctr_percent']:.2f}%"
            )
    lines.append("")

    lines.append("## 1b) Keyword position changes (vs prior Saturday)")
    if not baseline_date:
        lines.append("")
        lines.append("_Baseline snapshot not available; rank change table unavailable._")
    elif not keyword_rank_changes:
        lines.append("")
        lines.append("_No keywords met your position-delta thresholds this week._")
    else:
        for i, item in enumerate(keyword_rank_changes, start=1):
            lines.append(
                f"{i}. `{item['query']}` — Pos {item['avg_position_now']:.1f} (was {item['avg_position_base']:.1f}, Δ {format_rank_delta(item['position_delta'])}) | Impr {item['impressions_now']} | CTR {item['ctr_percent_now']:.2f}%"
            )
    lines.append("")

    lines.append("## 2) Pages losing rank")
    if not pages_losing_rank:
        lines.append("")
        lines.append("_None met your rank-drop thresholds this week._")
    else:
        for i, item in enumerate(pages_losing_rank, start=1):
            lines.append(
                f"{i}. {item['url']} — Pos {item['avg_position_now']:.1f} (Δ {format_rank_delta(item['position_delta'])}) | Impr {item['impressions_now']}"
            )
    lines.append("")

    lines.append("## 3) Pages with low CTR")
    if not low_ctr_pages:
        lines.append("")
        lines.append("_None met your low-CTR thresholds this week._")
    else:
        for i, item in enumerate(low_ctr_pages, start=1):
            lines.append(
                f"{i}. {item['url']} — CTR {item['ctr_percent']:.2f}% | Pos {item['avg_position']:.1f} | Impr {item['impressions']}"
            )
    lines.append("")

    lines.append("## 4) AI visibility gaps")
    lines.append("")
    lines.append(f"**AI Visibility Score:** {ai_visibility.get('ai_visibility_score')} / 100  \n{ai_visibility.get('tier')}")
    lines.append("")
    if ai_gaps:
        for gap in ai_gaps:
            lines.append(f"- {gap}")
    else:
        lines.append("_No major GEO gaps detected from the analyzed signals._")
    lines.append("")

    if geo_page_citability:
        lines.append("### Page-level citability checks (top targets)")
        lines.append("")
        for row in geo_page_citability:
            lines.append(
                f"- {row['url']} — Citability {row['average_citability_score']:.1f}/100 (grade {row.get('grade','')})"
            )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("_Generated by the Future Loans SEO/GEO agent._")
    lines.append("")
    return "\n".join(lines)


@dataclass
class Thresholds:
    impressions_min: float = 500
    ctr_max_percent: float = 1.5
    position_lost_min_delta: float = 2.0
    top_n_keyword_opportunities: int = 10
    top_n_rank_drops: int = 10
    top_n_low_ctr_pages: int = 10
    geo_citability_top_pages: int = 10

    # For opportunity discovery
    new_impressions_min: float = 200
    position_opportunity_min: float = 4.0
    position_opportunity_max: float = 30.0
    impressions_jump_min: float = 1.5  # multiplier vs baseline impressions

    # Low share of volume opportunities
    # Default definition: query_impressions / total_impressions_across_all_queries (within the snapshot).
    impression_share_max_percent: float = 2.0  # <=2% of total query impressions this week

    # Keyword position change table
    min_position_delta_abs: float = 1.0
    position_change_top_n: int = 50


def load_thresholds_from_args(args: argparse.Namespace) -> Thresholds:
    return Thresholds(
        impressions_min=float(args.impressions_min),
        ctr_max_percent=float(args.ctr_max_percent),
        position_lost_min_delta=float(args.position_lost_min_delta),
        top_n_keyword_opportunities=int(args.top_n_keyword_opportunities),
        top_n_rank_drops=int(args.top_n_rank_drops),
        top_n_low_ctr_pages=int(args.top_n_low_ctr_pages),
        geo_citability_top_pages=int(args.geo_citability_top_pages),
        new_impressions_min=float(args.new_impressions_min),
        position_opportunity_min=float(args.position_opportunity_min),
        position_opportunity_max=float(args.position_opportunity_max),
        impressions_jump_min=float(args.impressions_jump_min),
        impression_share_max_percent=float(args.impression_share_max_percent),
        min_position_delta_abs=float(args.min_position_delta_abs),
        position_change_top_n=int(args.position_change_top_n),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Weekly Future Loans SEO and GEO Agent")
    parser.add_argument("--domain", required=True, help="Domain or full base URL (e.g., futureloans.com or https://futureloans.com)")
    parser.add_argument("--brand-name", default="", help="Brand name for entity/brand mention checks")
    parser.add_argument("--metrics-current", required=True, help="Path to current-week Search Console snapshot JSON")
    parser.add_argument("--metrics-baseline", default="", help="Optional path to baseline snapshot JSON (previous week)")
    parser.add_argument("--report-date", default="", help="Week ending date (YYYY-MM-DD). Defaults to today's date.")
    parser.add_argument("--history-dir", default="", help="Optional base dir for persisted snapshots (defaults to ~/.future-loans-agent)")

    # Threshold overrides
    parser.add_argument("--impressions-min", default=500, help="Min impressions for page-based alerts")
    parser.add_argument("--ctr-max-percent", default=1.5, help="CTR ceiling (percent) for low-CTR detection and query opportunity heuristics")
    parser.add_argument("--position-lost-min-delta", default=2.0, help="Min average-position worsening (delta) to call a rank drop")
    parser.add_argument("--new-impressions-min", default=200, help="Min impressions for new keyword opportunities")
    parser.add_argument("--position-opportunity-min", default=4.0, help="Min avg position for opportunity band")
    parser.add_argument("--position-opportunity-max", default=30.0, help="Max avg position for opportunity band")
    parser.add_argument("--impressions-jump-min", default=1.5, help="Multiplier impressions jump vs baseline for 'new' opportunity")
    parser.add_argument("--top-n-keyword-opportunities", default=10, help="Top N keyword opportunities to include")
    parser.add_argument("--top-n-rank-drops", default=10, help="Top N pages losing rank to include")
    parser.add_argument("--top-n-low-ctr-pages", default=10, help="Top N low CTR pages to include")
    parser.add_argument("--geo-citability-top-pages", default=10, help="Max number of pages to run citability analysis on")
    parser.add_argument("--target-keywords-file", default="", help="Optional path to a text file with one keyword per line to restrict keyword analysis.")
    parser.add_argument("--impression-share-max-percent", default=2.0, help="Low share-of-volume threshold within the snapshot: query_impressions/total_impressions <= this.")
    parser.add_argument("--min-position-delta-abs", default=1.0, help="Minimum absolute avg-position delta (vs baseline) to include keyword rank-change rows.")
    parser.add_argument("--position-change-top-n", default=50, help="Max number of keywords to list in the rank-change table.")

    args = parser.parse_args()
    thresholds = load_thresholds_from_args(args)

    domain_url = parse_base_url(args.domain)
    domain_netloc = urlparse(domain_url).netloc

    report_date = args.report_date.strip() or today_iso()
    agent_name = "Future Loans SEO and GEO Agent"

    base_dir = args.history_dir.strip() or os.path.join(os.path.expanduser("~"), ".future-loans-agent")
    history_root = os.path.join(base_dir, "history", domain_netloc)
    output_root = os.path.join(base_dir, "outputs", domain_netloc, report_date)
    os.makedirs(output_root, exist_ok=True)

    current_metrics = ensure_metrics_shape(_load_json(args.metrics_current))
    baseline_metrics: Optional[Dict[str, Any]] = None
    baseline_loaded_from = ""

    if args.metrics_baseline:
        baseline_metrics = ensure_metrics_shape(_load_json(args.metrics_baseline))
        baseline_loaded_from = args.metrics_baseline
    else:
        # Auto-load most recent snapshot as baseline
        if os.path.isdir(history_root):
            candidates: List[Tuple[str, str]] = []
            for name in os.listdir(history_root):
                if not name.endswith(".json"):
                    continue
                # Expect: YYYY-MM-DD.json
                date_prefix = name[:10]
                if len(date_prefix) != 10:
                    continue
                try:
                    datetime.fromisoformat(date_prefix)
                except Exception:
                    continue
                full = os.path.join(history_root, name)
                candidates.append((full, date_prefix))

            # Sort by date asc, then pick the latest strictly before report_date.
            candidates.sort(key=lambda x: x[1])
            baseline_path = None
            for full, d in reversed(candidates):
                if d < report_date:
                    baseline_path = full
                    break
            if not baseline_path and candidates:
                baseline_path = candidates[-1][0]

            if baseline_path and os.path.abspath(baseline_path) != os.path.abspath(args.metrics_current):
                baseline_metrics = ensure_metrics_shape(_load_json(baseline_path))
                baseline_loaded_from = baseline_path

    baseline_date = ""
    if baseline_loaded_from:
        # Expect filename: YYYY-MM-DD.json
        base = os.path.basename(baseline_loaded_from)
        if len(base) >= 10 and base[0:10].count("-") == 2:
            baseline_date = base[0:10]

    # Persist current snapshot (for next week delta)
    snapshot_path = os.path.join(history_root, f"{report_date}.json")
    _write_json(snapshot_path, current_metrics)

    pages_now = index_pages(current_metrics.get("pages") or [])
    queries_now = index_queries(current_metrics.get("queries") or [])

    pages_base = index_pages(baseline_metrics.get("pages") or []) if baseline_metrics else {}
    queries_base = index_queries(baseline_metrics.get("queries") or []) if baseline_metrics else {}

    # Optional keyword seed list to restrict analysis to known/target keywords.
    target_keywords_set: set[str] = set()
    if args.target_keywords_file.strip():
        with open(args.target_keywords_file.strip(), "r", encoding="utf-8") as f:
            for line in f:
                k = line.strip()
                if not k or k.startswith("#"):
                    continue
                target_keywords_set.add(k.lower())

    # ----------------------------
    # 1) New keyword opportunities
    # ----------------------------
    keyword_opportunities: List[Dict[str, Any]] = []
    total_impressions_now = sum(
        _safe_float(q.get("impressions"), 0.0) for q in current_metrics.get("queries") or []
    )
    total_impressions_now = max(1e-6, total_impressions_now)
    for query, qrow in queries_now.items():
        if target_keywords_set and query.lower() not in target_keywords_set:
            continue

        impressions_now = _safe_float(qrow.get("impressions"), 0.0)
        avg_position_now = _safe_float(qrow.get("avg_position"), _safe_float(qrow.get("position"), 0.0))
        ctr_percent_now = normalize_ctr_percent(qrow.get("ctr"))
        share_percent_now = (impressions_now / total_impressions_now) * 100.0

        if impressions_now < thresholds.new_impressions_min:
            continue

        # "Low share of volume" opportunity.
        if share_percent_now > thresholds.impression_share_max_percent:
            continue

        score = opportunity_score_query(
            impressions=impressions_now,
            ctr_percent=ctr_percent_now,
            avg_position=avg_position_now,
            position_min=thresholds.position_opportunity_min,
            position_max=thresholds.position_opportunity_max,
            ctr_max=thresholds.ctr_max_percent,
        )
        if score <= 0:
            continue

        base_row = queries_base.get(query)
        reason = "new_query_low_share" if base_row is None else "low_share_existing_query"

        keyword_opportunities.append(
            {
                "query": query,
                "impressions": int(impressions_now),
                "avg_position": float(avg_position_now),
                "ctr_percent": float(ctr_percent_now),
                "impression_share_percent": float(share_percent_now),
                "opportunity_score": float(score),
                "reason": reason,
            }
        )

    keyword_opportunities.sort(key=lambda x: x.get("opportunity_score", 0), reverse=True)
    keyword_opportunities = keyword_opportunities[: thresholds.top_n_keyword_opportunities]

    # ----------------------------
    # Keyword position changes (vs baseline)
    # ----------------------------
    keyword_rank_changes: List[Dict[str, Any]] = []
    if baseline_metrics:
        for query, qrow in queries_now.items():
            if target_keywords_set and query.lower() not in target_keywords_set:
                continue

            impressions_now = _safe_float(qrow.get("impressions"), 0.0)
            if impressions_now < thresholds.impressions_min:
                continue

            avg_position_now = _safe_float(
                qrow.get("avg_position"), _safe_float(qrow.get("position"), 0.0)
            )
            base_row = queries_base.get(query)
            if not base_row:
                continue
            avg_position_base = _safe_float(
                base_row.get("avg_position"), _safe_float(base_row.get("position"), 0.0)
            )

            position_delta = float(avg_position_now - avg_position_base)
            if abs(position_delta) < thresholds.min_position_delta_abs:
                continue

            keyword_rank_changes.append(
                {
                    "query": query,
                    "impressions_now": int(impressions_now),
                    "ctr_percent_now": float(normalize_ctr_percent(qrow.get("ctr"))),
                    "avg_position_now": float(avg_position_now),
                    "avg_position_base": float(avg_position_base),
                    "position_delta": position_delta,
                }
            )

        keyword_rank_changes.sort(
            key=lambda x: (abs(x.get("position_delta", 0.0)), x.get("impressions_now", 0)),
            reverse=True,
        )
        keyword_rank_changes = keyword_rank_changes[: thresholds.position_change_top_n]

    # ----------------------------
    # 2) Pages losing rank
    # ----------------------------
    pages_losing_rank: List[Dict[str, Any]] = []
    for url, prow in pages_now.items():
        impressions_now = _safe_float(prow.get("impressions"), 0.0)
        if impressions_now < thresholds.impressions_min:
            continue

        avg_position_now = _safe_float(prow.get("avg_position"), _safe_float(prow.get("position"), 0.0))
        base_row = pages_base.get(url)
        if not base_row:
            continue

        avg_position_base = _safe_float(base_row.get("avg_position"), _safe_float(base_row.get("position"), 0.0))
        position_delta = float(avg_position_now - avg_position_base)

        # Positive delta => rank worsened (avg_position gets larger)
        if position_delta >= thresholds.position_lost_min_delta:
            pages_losing_rank.append(
                {
                    "url": url,
                    "impressions_now": int(impressions_now),
                    "avg_position_now": float(avg_position_now),
                    "avg_position_base": float(avg_position_base),
                    "position_delta": position_delta,
                }
            )

    pages_losing_rank.sort(key=lambda x: (x["position_delta"], x["impressions_now"]), reverse=True)
    pages_losing_rank = pages_losing_rank[: thresholds.top_n_rank_drops]

    # ----------------------------
    # 3) Pages with low CTR
    # ----------------------------
    low_ctr_pages: List[Dict[str, Any]] = []
    for url, prow in pages_now.items():
        impressions_now = _safe_float(prow.get("impressions"), 0.0)
        if impressions_now < thresholds.impressions_min:
            continue

        ctr_percent_now = normalize_ctr_percent(prow.get("ctr"))
        if ctr_percent_now <= thresholds.ctr_max_percent:
            avg_position_now = _safe_float(prow.get("avg_position"), _safe_float(prow.get("position"), 0.0))
            low_ctr_pages.append(
                {
                    "url": url,
                    "impressions": int(impressions_now),
                    "ctr_percent": float(ctr_percent_now),
                    "avg_position": float(avg_position_now),
                    "ctr_deficit": float(max(0.0, thresholds.ctr_max_percent - ctr_percent_now)),
                }
            )

    low_ctr_pages.sort(key=lambda x: (x["ctr_deficit"], x["impressions"]), reverse=True)
    low_ctr_pages = low_ctr_pages[: thresholds.top_n_low_ctr_pages]

    # ----------------------------
    # 4) AI visibility gaps
    # ----------------------------
    # Pick GEO target pages from the categories above.
    target_page_urls: List[str] = []
    for row in pages_losing_rank[: max(5, thresholds.top_n_rank_drops // 2)]:
        target_page_urls.append(row["url"])
    for row in low_ctr_pages[: max(5, thresholds.top_n_low_ctr_pages // 2)]:
        target_page_urls.append(row["url"])
    # Dedup preserving order
    seen: set[str] = set()
    target_page_urls = [u for u in target_page_urls if not (u in seen or seen.add(u))]
    target_page_urls = target_page_urls[: thresholds.geo_citability_top_pages]

    domain_root_url = domain_url
    robots = fetch_page.fetch_robots_txt(domain_root_url)
    llms_validate = llmstxt_generator.validate_llmstxt(domain_root_url)
    llms_score_row = compute_llms_score(llms_validate)

    crawler_score_row = compute_crawler_access_score(
        robots=robots,
        tier1=TIER1_CRAWLERS_DEFAULT,
        secondary=SECONDARY_CRAWLERS_DEFAULT,
    )

    brand_score_row: Dict[str, Any] = {"score": 0.0, "components": {}}
    brand_report: Optional[Dict[str, Any]] = None
    if args.brand_name.strip():
        try:
            brand_report = brand_scanner.generate_brand_report(args.brand_name.strip(), domain_netloc)
            brand_score_row = compute_brand_mention_score(brand_report)
        except Exception as e:
            brand_report = {"error": str(e)}
            brand_score_row = {"score": 0.0, "components": {"error": str(e)}}

    # Citability analysis (page-level)
    geo_page_citability: List[Dict[str, Any]] = []
    citability_total = 0.0
    citability_weight = 0.0

    # For weighting, use impressions from pages_now when available.
    for url in target_page_urls:
        t0 = time.time()
        row = pages_now.get(url) or {}
        impressions = _safe_float(row.get("impressions"), 0.0)
        weight = impressions if impressions > 0 else 1.0
        try:
            cit = citability_scorer.analyze_page_citability(url)
            avg_cit = _safe_float(cit.get("average_citability_score"), 0.0)
            grade, grade_label = citability_grade_from_average(avg_cit)
            geo_page_citability.append(
                {
                    "url": url,
                    "average_citability_score": float(avg_cit),
                    "grade": grade,
                    "grade_label": grade_label,
                    "time_seconds": round(time.time() - t0, 2),
                    "blocks_analyzed": cit.get("total_blocks_analyzed"),
                    "details": cit,
                }
            )
            citability_total += avg_cit * weight
            citability_weight += weight
        except Exception as e:
            geo_page_citability.append(
                {
                    "url": url,
                    "average_citability_score": 0.0,
                    "grade": "",
                    "time_seconds": round(time.time() - t0, 2),
                    "error": str(e),
                }
            )

    citability_score = (citability_total / citability_weight) if citability_weight > 0 else 0.0
    ai_visibility = compute_ai_visibility_score(
        citability_score=citability_score,
        brand_score=float(brand_score_row.get("score", 0.0)),
        crawler_score=float(crawler_score_row.get("score", 0.0)),
        llms_score=float(llms_score_row.get("score", 0.0)),
    )

    # Build a human-friendly gap list with prioritized issues.
    ai_gaps: List[str] = []

    llms_exists = bool(llms_validate.get("exists", False))
    if not llms_exists:
        ai_gaps.append("- Missing `llms.txt` at domain root. Create `/llms.txt` to guide AI systems to your most important pages.")
    else:
        if not bool(llms_validate.get("format_valid", False)):
            ai_gaps.append("- `llms.txt` exists but appears malformed. Fix headings/links so AI systems can parse sections and key pages reliably.")

    critical_blocked = crawler_score_row.get("critical_blocked") or []
    if critical_blocked:
        ai_gaps.append(f"- AI crawler access blockers detected for Tier-1 crawlers: {', '.join(critical_blocked)}. Update `robots.txt` to allow crawlers from those user-agents.")

    if not crawler_score_row.get("sitemaps_found", 0):
        ai_gaps.append("- No `Sitemap:` reference found in `robots.txt`. Add sitemap index URLs to improve AI discovery of your content.")

    # Page-level citability gaps
    low_cit_pages = [p for p in geo_page_citability if p.get("average_citability_score", 0.0) < 50]
    if low_cit_pages:
        sample = low_cit_pages[:5]
        ai_gaps.append("- Low AI citability on key pages (needs better answer-block structure). Examples: " + ", ".join([s["url"] for s in sample]))

    # Brand mention gap (only reliable for Wikipedia/Wikidata in current scanner)
    if brand_report:
        wiki_platform = (brand_report.get("platforms") or {}).get("wikipedia") or {}
        if not wiki_platform.get("has_wikipedia_page") and not wiki_platform.get("has_wikidata_entry"):
            ai_gaps.append("- Entity presence looks weak on Wikipedia/Wikidata (strong GEO signal). Consider earning notability and adding `sameAs` in schema.")

    # ----------------------------
    # Write outputs
    # ----------------------------
    report_md = markdown_report(
        agent_name=agent_name,
        domain=domain_netloc,
        brand_name=args.brand_name.strip(),
        report_date=report_date,
        keyword_opportunities=keyword_opportunities,
        keyword_rank_changes=keyword_rank_changes,
        pages_losing_rank=pages_losing_rank,
        low_ctr_pages=low_ctr_pages,
        ai_visibility=ai_visibility,
        ai_gaps=ai_gaps,
        geo_page_citability=geo_page_citability[: min(10, len(geo_page_citability))],
        baseline_date=baseline_date,
    )

    md_path = os.path.join(output_root, f"future-loans-weekly-report-{report_date}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(report_md)

    json_path = os.path.join(output_root, f"future-loans-weekly-report-{report_date}.json")
    json_data: Dict[str, Any] = {
        "agent": agent_name,
        "domain": domain_netloc,
        "brand_name": args.brand_name.strip(),
        "report_date": report_date,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "baseline_loaded_from": baseline_loaded_from,
        "thresholds": thresholds.__dict__,
        "inputs": {
            "metrics_current_file": args.metrics_current,
            "metrics_baseline_file": args.metrics_baseline or "",
            "metrics_snapshot_auto_baseline": bool(baseline_loaded_from),
        },
        "keyword_opportunities": keyword_opportunities,
        "keyword_rank_changes": keyword_rank_changes,
        "pages_losing_rank": pages_losing_rank,
        "low_ctr_pages": low_ctr_pages,
        "ai_visibility": {
            **ai_visibility,
            "components": {
                "citability_score": round(citability_score, 1),
                "brand_score": brand_score_row.get("score", 0.0),
                "crawler_access_score": crawler_score_row.get("score", 0.0),
                "llms_score": llms_score_row.get("score", 0.0),
            },
            "robots": robots,
            "llms_validate": llms_validate,
            "llms_score_row": llms_score_row,
            "crawler_score_row": crawler_score_row,
            "brand_report": brand_report,
            "brand_score_row": brand_score_row,
        },
        "ai_visibility_gaps": ai_gaps,
        "geo_page_citability": [
            {
                "url": r["url"],
                "average_citability_score": r.get("average_citability_score", 0.0),
                "grade": r.get("grade", ""),
                "blocks_analyzed": r.get("blocks_analyzed"),
                "error": r.get("error", None),
            }
            for r in geo_page_citability
        ],
    }
    _write_json(json_path, json_data)

    # Print a small machine-friendly summary for n8n.
    print(json.dumps({"md_path": md_path, "json_path": json_path, "ai_visibility_score": ai_visibility.get("ai_visibility_score")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

