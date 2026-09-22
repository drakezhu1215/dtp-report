#!/usr/bin/env python3
"""DTP Static Analysis Violations Report Generator (per project).

本模块是**报告引擎**（DTP 数据 -> 报告渲染编排），不含任何图形界面代码，
由统一主入口 main.py 调用：``build_project_charts`` 出图、``generate_project_reports``
逐项目渲染 .html + .xlsx。

config.yaml 的 merge_projects 定义 "项目 -> 仓库列表" 的归属关系，例如：

    merge_projects:
      海外三相电表平台:
        - spln_3p_application
        - spln_3p_bootloader
        - spln_3p_kernel
        - spln_3p_metrology

每个项目（merge_projects 的 key）输出一份独立报告：

    海外三相电表平台_report_20260916_091708.html / .xlsx

报告内容只包含该项目下各仓库（spln_3p_application 等）的数据：

- 摘要按该项目仓库汇总，项目概览和严重级别明细表逐仓库列出；
- 第 4 章"项目违规图"：该项目下全部有数据的仓库的违规数量对比条形图（不截断）；
- 第 5 章"项目趋势图"：各仓库违规趋势曲线图——有几个仓库就画几张，X 轴是构建次序
  （build-1、build-2 ...），Y 轴是那次构建的违规数量；
  趋势数据由 analyzer.collect_build_trends 从 DTP 逐仓库、逐 build 采集，
  每个仓库取**全部构建**（每次构建一个点）；构建特别多时可用 config.yaml 的
  report.trend_builds 限制成"最近 N 次"。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from analyzer import ProjectViolations, RepoBuildTrend
from chart_builder import chart_repo_trends, chart_total_violations_bad
from report_excel import generate_excel_report
from report_html import generate_html_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Windows 文件名非法字符（含控制字符）；合并项目名里可能出现 '/'，必须替换
_INVALID_FILE_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# 项目名 -> 该项目下的仓库列表
ProjectGroups = list[tuple[str, list[ProjectViolations]]]


def build_project_charts(
    project_name: str,
    repo_data: list[ProjectViolations],
    trends_by_repo: dict[str, RepoBuildTrend],
) -> dict:
    """生成单个项目报告所需的图表。

    - ``total_bad``：该项目下各仓库的违规数量对比条形图——**显示全部有数据的仓库**，
      不截断（只有一个仓库时也照常出图）；
    - ``repo_trends``：该项目每个仓库一张违规趋势曲线图——有几个仓库就画几张，
      X 轴是构建次序（build-1、build-2 ...），Y 轴是那次构建的违规数量；
      仓库没有历史构建数据时该仓库不出图（report_v3_zh.html 的 5 章按顺序渲染）。
    """
    # 该项目下的仓库顺序与报告表格一致（merge_projects 里配置的顺序）
    trends = [
        trends_by_repo[repo.project_name]
        for repo in repo_data
        if repo.project_name in trends_by_repo
    ]

    repo_count_with_data = len([d for d in repo_data if d.has_data])

    return {
        "total_bad": chart_total_violations_bad(
            repo_data,
            # 显示全部仓库：top_n_bad <= 0 时 chart_total_violations_bad 不做截断
            top_n_bad=0,
            title=f"{project_name} 各仓库违规数量对比（共 {repo_count_with_data} 个仓库）",
            x_label="仓库 (Repository)",
        ),
        "repo_trends": chart_repo_trends(trends),
    }


def safe_file_name(name: str) -> str:
    """把项目名转换为 Windows/Linux 均可用的文件名片段。

    合并项目名里可能含 '/'（如 "国网多APP平台III型专变优化方案(22规范/24规范)"），
    直接当文件名在 Windows 上会失败，这里统一替换为 '_'，并去掉首尾空白和结尾的点。
    """
    cleaned = _INVALID_FILE_NAME_CHARS.sub("_", str(name)).strip().strip(" .")
    return cleaned or "project"


def build_report_base_name(project_name: str, timestamp: str) -> str:
    """报告文件名（不含扩展名）：<项目名>_report_<YYYYmmdd_HHMMSS>。"""
    return f"{safe_file_name(project_name)}_report_{timestamp}"


def generate_project_reports(
    groups: ProjectGroups,
    trends_by_repo: dict[str, RepoBuildTrend],
    output_dir: Path,
    timestamp: str,
    dtp_url: str,
    locale: str,
) -> list[Path]:
    """为每个项目生成一份 HTML + Excel 报告，返回已生成的 HTML 报告路径列表。

    图表按项目分别生成：条形图包含该项目下的**全部仓库**，趋势图只取该项目下的仓库，
    因此每份报告里的趋势图数量 = 该项目里有历史构建数据的仓库数量。
    """
    generated: list[Path] = []
    total = len(groups)

    for index, (project_name, repo_data) in enumerate(groups, 1):
        if not any(item.has_data for item in repo_data):
            logger.warning(
                "[%d/%d] %s: no static analysis data, skipped.",
                index, total, project_name,
            )
            continue

        base_name = build_report_base_name(project_name, timestamp)
        logger.info(
            "[%d/%d] Generating report for %s (%d repo(s))...",
            index, total, project_name, len(repo_data),
        )

        charts = build_project_charts(project_name, repo_data, trends_by_repo)
        logger.info(
            "  Charts: 1 repo bar chart (%d repo(s)) + %d repo trend chart(s) for %s",
            len([d for d in repo_data if d.has_data]),
            len(charts["repo_trends"]), project_name,
        )

        html_path = generate_html_report(
            repo_data, charts, output_dir / f"{base_name}.html",
            dtp_url=dtp_url, locale=locale, project_name=project_name,
        )
        logger.info("  HTML report: %s", html_path)

        xlsx_path = generate_excel_report(repo_data, output_dir / f"{base_name}.xlsx")
        logger.info("  Excel report: %s", xlsx_path)

        generated.append(html_path)

    return generated
