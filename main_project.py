#!/usr/bin/env python3
"""DTP Static Analysis Violations Report Generator (per project).

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
摘要按该项目仓库汇总，仓库概览和严重级别明细表逐仓库列出；
项目趋势图暂时仍使用合并后的全部项目数据（保持旧行为）。
"""

from __future__ import annotations

import argparse
import logging
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import yaml

from analyzer import ProjectViolations, collect_violations
from chart_builder import chart_total_violations_bad
from dtp_client import DTPClient
from report_excel import generate_excel_report
from report_html import generate_html_report

#define my python file 'merge_project.py'
from merge_project import (
    group_project_violations,
    list_source_projects,
    merge_project_violations,
)

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


def load_config(config_path: str) -> dict:
    p = Path(config_path)
    if not p.exists():
        logger.error("Config file not found: %s", config_path)
        sys.exit(1)
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DTP Static Analysis Violations Report Generator (one report per project)",
    )
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to config.yaml (default: config.yaml)")
    parser.add_argument("--url", help="DTP server URL (overrides config)")
    parser.add_argument("--user", help="DTP username (overrides config)")
    parser.add_argument("--password", help="DTP password (overrides config)")
    parser.add_argument("--projects", help="Comma-separated project names to analyze (default: source projects of merge_projects)")
    parser.add_argument("--output", help="Output directory (overrides config)")
    parser.add_argument("--locale", choices=["zh", "en", "v2_zh", "v2_en", "v3_zh"], help="Report language/template: zh, en, v2_zh, v2_en, v3_zh (overrides config)")
    return parser


def resolve_project_names(
    args: argparse.Namespace,
    report_cfg: dict,
    merge_projects: dict,
) -> list[str] | None:
    """抓取范围优先级：--projects > report.project_names > merge_projects 的源项目。

    解析结果为空时 collect_violations 会回退为扫描 DTP 全部项目（很慢，易超时）。
    """
    if args.projects:
        return [p.strip() for p in args.projects.split(",") if p.strip()]

    if report_cfg.get("project_names"):
        return [str(p).strip() for p in report_cfg["project_names"] if str(p).strip()]

    return list_source_projects(merge_projects)


def warn_missing_projects(data: list[ProjectViolations], project_names: list[str] | None) -> None:
    """配置中的项目名必须与 DTP 项目名完全一致（merge_project 按名称精确匹配）。"""
    if not project_names:
        return

    returned = {d.project_name for d in data}
    missing = [name for name in project_names if name not in returned]
    if missing:
        preview = ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")
        logger.warning("%d specified project(s) not found on DTP: %s", len(missing), preview)


def build_charts(merged_violations: list[ProjectViolations], top_n_bad: int) -> dict:
    """生成图表。

    report_v3_zh.html 已移除"合规模范榜"章节，仅需生成违规总量对比图（charts.total_bad）。
    项目趋势图暂不按项目拆分，仍使用合并后的全部项目数据。
    """
    return {"total_bad": chart_total_violations_bad(merged_violations, top_n_bad=top_n_bad)}


def prepare_output_dir(output_dir: str) -> Path:
    """清理并重建 output 目录，让本次运行生成的报告是目录里唯一的内容。"""
    out = Path(output_dir)
    if out.exists():
        logger.info("Cleaning output directory: %s", out.resolve())
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    logger.info("Output directory ready: %s", out.resolve())
    return out


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
    charts: dict,
    output_dir: Path,
    timestamp: str,
    dtp_url: str,
    locale: str,
) -> list[Path]:
    """为每个项目生成一份 HTML + Excel 报告，返回已生成的 HTML 报告路径列表。"""
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

        html_path = generate_html_report(
            repo_data, charts, output_dir / f"{base_name}.html",
            dtp_url=dtp_url, locale=locale, project_name=project_name,
        )
        logger.info("  HTML report: %s", html_path)

        xlsx_path = generate_excel_report(repo_data, output_dir / f"{base_name}.xlsx")
        logger.info("  Excel report: %s", xlsx_path)

        generated.append(html_path)

    return generated


def main():
    parser = build_argparser()
    args = parser.parse_args()

    cfg = load_config(args.config)
    dtp_cfg = cfg.get("dtp", {})
    report_cfg = cfg.get("report", {})

    merge_projects = cfg.get("merge_projects", {})
    

    url = args.url or dtp_cfg.get("url", "http://localhost:9191")
    username = args.user or dtp_cfg.get("username", "admin")
    password = args.password or dtp_cfg.get("password", "admin")
    output_dir = args.output or report_cfg.get("output_dir", "./output")
    top_n_bad = report_cfg.get("top_n_bad", 3)
    locale = args.locale or report_cfg.get("locale_project", "v3_zh")

    project_names = resolve_project_names(args, report_cfg, merge_projects)

    if not project_names:
        logger.warning("No project names resolved, will scan ALL projects on DTP (slow, prone to timeouts).")

    logger.info("Connecting to DTP at %s", url)
    client = DTPClient(url=url, username=username, password=password)  
    
    # with_file_count=True: 额外从 DTP 查询每个仓库"扫描的源文件数"（/staticAnalysisFiles），
    #                       用于报告摘要的"文件总数"和仓库概览的"文件数"；
    # with_code_lines=True: 额外从 DTP 查询每个仓库"代码行数"（metrics 指标 METRIC.NOPLIF），
    #                       用于报告摘要的"代码总数"和仓库概览的"代码行数"。
    # 两个开关都开启后，摘要/仓库概览的"缺陷密度"才有意义（= 违规数 × 1000 / 代码行数）。
    data = collect_violations(
        client,
        project_names=project_names,
        with_file_count=True,
        with_code_lines=True,
    )
    logger.info("Collected data for %d project(s)", len(data))
    logger.info(
        "Raw total over projects with data: scanned files=%d, code lines=%d",
        sum(d.file_count for d in data if d.has_data),
        sum(d.code_lines for d in data if d.has_data),
    )

    # 有静态分析数据、但 DTP 该 build 未上报任何代码度量（metrics/types 为空）时，
    # 代码行数会是 0，仓库概览里该行显示"无数据"——这里列出来，避免被当成"真的 0 行"。
    no_loc = [d.project_name for d in data if d.has_data and d.code_lines == 0]
    if no_loc:
        preview = ", ".join(no_loc[:10]) + (" ..." if len(no_loc) > 10 else "")
        logger.warning(
            "%d repo(s) have static analysis data but no code-lines metric "
            "(the build reported no metrics): %s",
            len(no_loc), preview,
        )

    warn_missing_projects(data, project_names)
    
    # 合并视图只用于项目趋势图（保持旧行为），报告明细按项目下的仓库拆分
    merged_violations = merge_project_violations(data, merge_projects)
    if not any(d.has_data for d in merged_violations):
        logger.warning("No projects with static analysis data found.")
        sys.exit(0)

    # 项目 -> 仓库列表：每个项目一份报告，报告内只含该项目下仓库的数据
    groups = group_project_violations(data, merge_projects)
    if not groups:
        logger.warning("No project matched config merge_projects, nothing to report.")
        sys.exit(0)

    logger.info("Generating charts...")
    charts = build_charts(merged_violations, top_n_bad)

    out = prepare_output_dir(output_dir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    logger.info("Generating %d project report(s) (locale=%s)...", len(groups), locale)
    generated = generate_project_reports(groups, charts, out, timestamp, url, locale)

    if not generated:
        logger.warning("No report generated (no project with static analysis data).")
        return

    logger.info("Done! %d report(s) generated in %s", len(generated), out.resolve())
       

if __name__ == "__main__":
    main()