from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from analyzer import ProjectViolations, SEVERITY_LABELS, calculate_defect_density
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
    "v4_zh": "report_v4_zh.html",
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


def _build_summary(
    data: list[ProjectViolations],
    summary_source: list[ProjectViolations] | None = None,
) -> dict:
    """汇总报告摘要。

    ``data`` 是报告里逐行列出的数据；``summary_source`` 可选，只用来决定
    \"文件总数 / 代码总数 / 违规总数 / 已抑制 / 缺陷密度\" 这几张汇总卡的**统计口径**。

    公司级报告（main.py 的 ``generate_company_report``）传的是"唯一仓库列表"
    （按 filter_id 去重后的仓库）：一个仓库可能同时属于多个项目组（如 config.yaml 的
    dual_mode_cco 同时属于"国网营销双模湖南扩展协议" 和 "国网营销双模能源控制器(专变)模块"），
    项目概览表里它会按组各列一行，但公司级的总量不能把这些文件/代码行重复计入。
    ``summary_source`` 为 None 时按 ``data`` 统计（项目报告 / 开发者报告的调用行为不变）。

    其余字段（total_projects / projects_with_data / highest_project / latest_project
    / top_category）仍然描述\"报告里逐行列出的条目\"，不受 ``summary_source`` 影响。
    """
    active = [d for d in data if d.has_data]
    # 汇总卡的数据来源：未指定 summary_source 时就是逐行列出的数据本身
    totals = [
        d for d in (summary_source if summary_source is not None else data) if d.has_data
    ]

    total_failed = sum(d.failed for d in totals)
    total_suppressed = sum(d.suppressed for d in totals)
    # 文件总数 = 去重后各仓库的文件数合计；
    # file_count 由 collect_violations(with_file_count=True) 从 DTP /staticAnalysisFiles 取得
    total_files = sum(d.file_count for d in totals)
    # 代码总数 = 去重后各仓库的代码行数合计；
    # code_lines 由 collect_violations(with_code_lines=True) 从 DTP metrics 指标取得
    total_code_lines = sum(d.code_lines for d in totals)
    # 核查总数 = 各相关仓库的构建（Build）数合计；build_count 由开发者报告相关路径
    # （analyzer_author.repos_of / collect_author_repo_rows）填充，其它报告恒为 0
    total_builds = sum(d.build_count for d in totals)
    # 项目级缺陷密度 = 违规总数 × 1000 / 代码总数，四舍五入取整；
    # 与单个仓库的 defect_density 用同一个函数，口径一致
    total_defect_density = calculate_defect_density(total_failed, total_code_lines)

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
        "total_files": total_files,
        "total_code_lines": total_code_lines,
        "total_builds": total_builds,
        "total_defect_density": total_defect_density,
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
    summary_source: list[ProjectViolations] | None = None,
    developer_name: str | None = None,
    related_projects: int | None = None,
) -> Path:
    """渲染 HTML 报告。

    ``data`` 决定"项目概览"和"严重级别明细表"的明细行，同时参与"摘要"计算，
    因此传入"单个项目下的仓库列表"即可得到该项目专属的报告；
    ``project_name`` 只用于报告标题显示（为 None 时模板保持原有标题）；
    ``summary_source`` 可选，只用来决定摘要里"文件总数 / 代码总数 / 违规总数 /
    缺陷密度"等汇总卡的统计口径——公司级报告传"唯一仓库列表"（按
    ``filter_id`` 去重），避免一个仓库属于多个项目组时被重复计入总量；
    为 None 时摘要仍按 ``data`` 统计；
    ``developer_name`` 只在开发者报告（report_v4_zh.html）里用：非空时标题变成
    "``<开发者>`` 开发者代码核查报告"，``project_name`` 优先，两者都不传时保持原样；
    ``related_projects`` 可选，只在开发者报告里用：与该开发者相关的**项目**数
    （config.yaml ``merge_projects`` 里包含其相关仓库的项目组个数），写入
    ``summary["related_projects"]``；未传时为 0（其它模板不使用该字段）。
    """
    template_name = TEMPLATE_MAP.get(locale, "report_zh.html")

    template_dir = Path(__file__).parent / "templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)), autoescape=True)
    template = env.get_template(template_name)

    summary = _build_summary(data, summary_source)
    # 与开发者相关的项目数（项目 = config.yaml merge_projects 的组名），
    # 由 report_author.count_related_projects 计算后传入；其它报告不使用
    summary["related_projects"] = int(related_projects or 0)

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
        developer_name=developer_name,  # 单个开发者专属报告时用于标题展示
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
