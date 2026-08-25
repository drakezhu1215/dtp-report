from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from analyzer import ProjectViolations, SEVERITY_LABELS
from chart_builder import SEVERITY_ORDER, SEVERITY_SHORT

SEVERITY_LABELS_ZH = {
    "1": "严重级别1（致命）",
    "2": "严重级别2（严重）",
    "3": "严重级别3（中等）",
    "4": "严重级别4（轻微）",
    "5": "严重级别5（提示）",
}

TEMPLATE_MAP = {
    "en": "report.html",
    "zh": "report_zh.html",
    "v2_en": "report_v2_en.html",
    "v2_zh": "report_v2_zh.html",
}


def _build_summary(data: list[ProjectViolations]) -> dict:
    active = [d for d in data if d.has_data]
    total_failed = sum(d.failed for d in active)
    total_suppressed = sum(d.suppressed for d in active)

    highest_proj = max(active, key=lambda d: d.failed) if active else None
    highest_project = highest_proj.project_name if highest_proj else "N/A"
    highest_project_count = highest_proj.failed if highest_proj else 0

    latest_proj = max(data, key=lambda d: d.build_id) if data else None
    print(latest_proj,flush=True)
    latest_project = latest_proj.project_name if latest_proj else "N/A"
    print(latest_project,flush=True)
    
    cat_totals: dict[str, int] = defaultdict(int)
    for d in active:
        for cat, cnt in d.category_breakdown.items():
            cat_totals[cat] += cnt
    top_category = max(cat_totals, key=cat_totals.get) if cat_totals else "N/A"
    top_category_count = cat_totals.get(top_category, 0)

    return {
        "total_projects": len(data),
        "projects_with_data": len(active),
        "total_failed": total_failed,
        "total_suppressed": total_suppressed,
        "highest_project": highest_project,
        "highest_project_count": highest_project_count,
        "latest_project": latest_project,
        "top_category": top_category,
        "top_category_count": top_category_count,
    }


def _collect_all_categories(data: list[ProjectViolations]) -> tuple[list[str], dict[str, int]]:
    totals: dict[str, int] = defaultdict(int)
    for d in data:
        if not d.has_data:
            continue
        for cat, cnt in d.category_breakdown.items():
            totals[cat] += cnt
    sorted_cats = sorted(totals, key=totals.get, reverse=True)
    return sorted_cats, dict(totals)


def generate_html_report(
    data: list[ProjectViolations],
    charts: dict,
    output_path: Path,
    dtp_url: str,
    top_n_categories: int = 15,
    locale: str = "zh",
) -> Path:
    template_name = TEMPLATE_MAP.get(locale, "report_zh.html")

    template_dir = Path(__file__).parent / "templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)), autoescape=False)
    template = env.get_template(template_name)

    summary = _build_summary(data)
    all_categories, category_totals = _collect_all_categories(data)

    html = template.render(
        generated_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        dtp_url=dtp_url,
        data=data,
        summary=summary,
        charts=charts,
        severity_order=SEVERITY_ORDER,
        severity_short=SEVERITY_SHORT,
        severity_labels=SEVERITY_LABELS,
        severity_labels_zh=SEVERITY_LABELS_ZH,
        top_n_categories=top_n_categories,
        all_categories=all_categories,
        category_totals=category_totals,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path
