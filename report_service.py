#!/usr/bin/env python3
"""DTP 报告生成器 —— 报告生成层（三类报告的导出编排）。

只做"选哪些数据、写到哪个目录、用什么模板"，具体渲染全部复用既有实现
（``project_merge`` / ``chart_builder`` / ``report_html`` / ``report_excel`` /
``report_project`` / ``report_author``），数据来自 :class:`data_pipeline.DataBundle`
（不再访问 DTP）。

输出目录约定：``config.yaml`` 的 ``report.output_dir`` 是**报告根目录**，三类报告各写
自己的子目录（互不覆盖），见 :data:`REPORT_SUBDIRS` 与 :func:`report_out_dir`：

- 公司报告   -> ``<根目录>/company/Friendcom_<时间戳>.html`` + ``.xlsx``
- 项目报告   -> ``<根目录>/project/<项目名>_report_<时间戳>.html`` + ``.xlsx``
- 开发者报告 -> ``<根目录>/author/<作者名>_developer_report_<时间戳>.html`` + ``.xlsx``
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from chart_builder import generate_all_charts
from data_pipeline import DataBundle
from project_merge import (
    group_project_violations,
    merge_project_violations,
    unique_repo_violations,
)
from report_author import generate_author_reports
from report_excel import generate_excel_report
from report_html import generate_html_report
from report_project import generate_project_reports

logger = logging.getLogger(__name__)


# 公司报告 / 项目报告的默认模板，与 config.yaml 保持一致
# （开发者报告的默认模板由 report_author.resolve_settings 决定）
DEFAULT_COMPANY_LOCALE = "v2_zh"
DEFAULT_PROJECT_LOCALE = "v3_zh"

# 公司报告文件名前缀：Friendcom_20260916_173616.html / .xlsx
COMPANY_REPORT_PREFIX = "Friendcom"


def build_company_base_name(timestamp: str) -> str:
    """公司报告文件名（不含扩展名）：Friendcom_<YYYYmmdd_HHMMSS>。"""
    return f"{COMPANY_REPORT_PREFIX}_{timestamp}"


# ---------------------------------------------------------------------------
# 三类报告的生成（全部复用步骤 1 抓到的 DataBundle，不再重复访问 DTP）
# ---------------------------------------------------------------------------
def _ensure_out_dir(out_dir: Path | str) -> Path:
    """确保输出目录存在（生成阶段只创建、不再清空，否则会删掉刚生成的报告）。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    return out


# 三类报告各自的输出子目录（相对 config.yaml 的 report.output_dir）：
#   <根目录>/company/   公司报告
#   <根目录>/project/   项目报告
#   <根目录>/author/    开发者报告
REPORT_SUBDIRS = {
    "company": "company",
    "project": "project",
    "author": "author",
}


def report_out_dir(
    bundle: DataBundle, kind: str, out_dir: Path | str | None = None
) -> Path:
    """某一类报告的输出目录：``<报告根目录>/<company|project|author>``（不存在时创建）。

    :param kind: 报告类型，取 :data:`REPORT_SUBDIRS` 的键（``company`` / ``project`` / ``author``）；
    :param out_dir: **报告根目录**的覆盖值（默认用 ``bundle.output_dir``，即 config.yaml 的
        ``report.output_dir``）。传进来的是根目录，三类报告的类型子目录都会建在它下面。
    """
    base = Path(out_dir) if out_dir is not None else bundle.output_dir
    return _ensure_out_dir(base / REPORT_SUBDIRS[kind])


def generate_company_report(
    bundle: DataBundle, *, out_dir: Path | str | None = None
) -> list[Path]:
    """生成公司级报告（把 config.yaml 的 merge_projects 合并后做跨项目对比）。

    合并 config.yaml 的 merge_projects 后做公司级汇总：摘要卡按"唯一仓库"去重统计，
    项目概览 / 严重级别明细表按合并组逐行展示，违规榜（违规警示榜 + 合规模范榜）
    与缺陷密度榜（缺陷密度警示榜 + 缺陷密度模范榜）按合并组出图。

    输出到 ``<报告根目录>/company/``。
    """
    data = bundle.data
    merge_projects = bundle.merge_projects
    report_cfg = bundle.report_cfg

    merged = merge_project_violations(data, merge_projects)
    active = [item for item in merged if item.has_data]
    if not active:
        logger.warning("公司报告：没有任何项目有静态分析数据，未生成报告。")
        return []

    # 摘要口径：按唯一仓库去重（一个仓库可能同时属于多个项目组，见 config.yaml 的 dual_mode_cco）
    summary_repos = [
        item for item in unique_repo_violations(group_project_violations(data, merge_projects))
        if item.has_data
    ]
    logger.info(
        "公司报告范围：%d 个合并项目组，%d 个唯一仓库（文件数 %d，代码行数 %d）",
        len(active), len(summary_repos),
        sum(item.file_count for item in summary_repos),
        sum(item.code_lines for item in summary_repos),
    )

    charts = generate_all_charts(
        merged,
        top_n_bad=int(report_cfg.get("top_n_bad", 3)),
        top_n_good=int(report_cfg.get("top_n_good", 3)),
        # 第 5 章"缺陷密度榜"：缺陷密度最高 / 最低的前 N 个项目
        top_n_density_bad=int(report_cfg.get("top_n_density_bad", 3)),
        top_n_density_good=int(report_cfg.get("top_n_density_good", 3)),
    )

    out = report_out_dir(bundle, "company", out_dir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = build_company_base_name(timestamp)
    locale = report_cfg.get("locale_friendcom", DEFAULT_COMPANY_LOCALE)

    html_path = generate_html_report(
        merged, charts, out / f"{base_name}.html",
        dtp_url=bundle.url, locale=locale,
        summary_source=summary_repos,
    )
    logger.info("  公司报告（HTML）：%s", html_path)
    xlsx_path = generate_excel_report(merged, out / f"{base_name}.xlsx")
    logger.info("  公司报告（Excel）：%s", xlsx_path)
    return [html_path]


def generate_project_reports_for(
    bundle: DataBundle,
    selected_projects: list[str] | None = None,
    *,
    out_dir: Path | str | None = None,
) -> list[Path]:
    """为每个项目生成一份报告（每个项目 1 个 .html + .xlsx），输出到 ``<报告根目录>/project/``。

    :param selected_projects: 要出报告的项目名（config.yaml ``merge_projects`` 的 key）；
        为 ``None``/空时对配置里的**全部项目**出报告。
    """
    merge_projects = bundle.merge_projects
    if selected_projects:
        wanted = set(selected_projects)
        unknown = [name for name in selected_projects if name not in merge_projects]
        if unknown:
            logger.warning(
                "所选项目不在 config.yaml 的 merge_projects 里：%s", "、".join(unknown)
            )
        merge_projects = {
            name: repos for name, repos in merge_projects.items() if name in wanted
        }
    if not merge_projects:
        logger.warning("项目报告：没有可生成报告的项目，未生成任何报告。")
        return []

    groups = group_project_violations(bundle.data, merge_projects)
    out = report_out_dir(bundle, "project", out_dir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    locale = bundle.report_cfg.get("locale_project", DEFAULT_PROJECT_LOCALE)

    logger.info("项目报告：%d 个项目，模板 %s", len(groups), locale)
    return generate_project_reports(
        groups, bundle.trends_by_repo, out, timestamp, bundle.url, locale,
    )


def generate_author_reports_for(
    bundle: DataBundle,
    selected_authors: list[str] | None = None,
    *,
    out_dir: Path | str | None = None,
) -> list[Path]:
    """为每位开发者生成一份报告（每位作者 1 个 .html + .xlsx），输出到 ``<报告根目录>/author/``。

    :param selected_authors: 要出报告的开发者名列表；为 ``None``/空时对 DTP 上抓到的
        **全部作者**出报告。列表里允许有索引里没有的作者（界面上的"手工添加"），
        这类作者会走 ``author=`` 的兜底查询路径。
    """
    authors = [name for name in (selected_authors or bundle.index.authors) if name]
    if not authors:
        logger.warning("开发者报告：没有可生成报告的开发者（DTP 上没有找到任何作者）。")
        return []

    out = report_out_dir(bundle, "author", out_dir)
    settings = dict(bundle.settings)
    settings["output_dir"] = str(out)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    logger.info("开发者报告：%d 位开发者，模板 %s", len(authors), settings["locale"])
    return generate_author_reports(
        bundle.client, bundle.index, authors, out, timestamp, settings,
    )


# ---------------------------------------------------------------------------
# 图形界面
# ---------------------------------------------------------------------------
# 三块（选项卡）的报告类型 -> 界面显示名
REPORT_KINDS = {
    "company": "公司报告",
    "project": "项目报告",
    "author": "开发者报告",
}
