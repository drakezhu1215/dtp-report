from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from analyzer import ProjectViolations, SEVERITY_LABELS
from chart_builder import SEVERITY_ORDER, SEVERITY_SHORT

SEVERITY_LABELS_ZH = {
    "1": "1级-致命",
    "2": "2级-严重",
    "3": "3级-中等",
    "4": "4级-轻微",
    "5": "5级-备选",
}

TEMPLATE_MAP = {
    "en": "report.html",
    "zh": "report_zh.html",
    "v2_en": "report_v2_en.html",
    "v2_zh": "report_v2_zh.html",
    "v3_zh": "report_v3_zh.html",
}


def _build_sort_key(d: ProjectViolations) -> tuple:
    """返回可比较的排序键，用于按 build_id 判断“最新构建”。

    DTP 的 build id 通常是数字字符串（如 \"12345\"），直接用字符串比较会
    出现 \"10\" < \"7\" 的错误；这里优先按数值比较，非数字再回退到字符串。
    """
    bid = d.build_id
    try:
        return (0, int(bid))
    except (TypeError, ValueError):
        return (1, str(bid))


def _build_summary(data: list[ProjectViolations]) -> dict:
    active = [d for d in data if d.has_data]
    total_failed = sum(d.failed for d in active)
    total_suppressed = sum(d.suppressed for d in active)

    highest_proj = max(active, key=lambda d: d.failed) if active else None
    highest_project = highest_proj.project_name if highest_proj else "N/A"
    highest_project_count = highest_proj.failed if highest_proj else 0

    # 从“有数据的项目”里取最新构建，避免选中无数据项目；并按数值比较 build_id
    latest_proj = max(active, key=_build_sort_key) if active else None
    latest_project = latest_proj.project_name if latest_proj else "N/A"
    
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


def generate_html_report(
    data: list[ProjectViolations],
    charts: dict,
    output_path: Path,
    dtp_url: str,
    locale: str = "zh",
    project_name: str | None = None,
) -> Path:
    """渲染 HTML 报告。

    ``data`` 决定"仓库概览"和"严重级别明细表"的明细行，同时参与"摘要"计算，
    因此传入"单个项目下的仓库列表"即可得到该项目专属的报告；
    ``project_name`` 只用于报告标题显示（为 None 时模板保持原有标题，
    main_friendcom.py 的调用行为完全不变）。
    """
    template_name = TEMPLATE_MAP.get(locale, "report_zh.html")

    template_dir = Path(__file__).parent / "templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)), autoescape=True)
    template = env.get_template(template_name)

    summary = _build_summary(data)

    # 只对明细表排序，不影响其他表（有严重级别明细的项目、按违规数降序）
    detail_data = sorted(
        [d for d in data if d.has_data and d.severity_breakdown],
        key=lambda x: x.failed,
        reverse=True,  # 从多到少
    )

    html = template.render(
        generated_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        dtp_url=dtp_url,
        project_name=project_name,  # 单个项目专属报告时用于标题展示
        data=data,  # 保持原样，给项目概览和其他表用
        detail_data=detail_data,  # 新增，专门给明细表用
        summary=summary,
        charts=charts,
        severity_order=SEVERITY_ORDER,
        severity_short=SEVERITY_SHORT,
        severity_labels=SEVERITY_LABELS,
        severity_labels_zh=SEVERITY_LABELS_ZH,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path
