#!/usr/bin/env python3
"""DTP Static Analysis Violations Comparison Report Generator."""

from __future__ import annotations

#pdb step by step debug
import pdb


import argparse
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

import yaml

from analyzer import collect_violations
from chart_builder import generate_all_charts
from dtp_client import DTPClient
from report_excel import generate_excel_report
from report_html import generate_html_report

#define my python file 'merge_project.py'
from merge_project import list_source_projects, merge_project_violations

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def load_config(config_path: str) -> dict:
    p = Path(config_path)
    if not p.exists():
        logger.error("Config file not found: %s", config_path)
        sys.exit(1)
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# 报告文件名前缀：Friendcom_20260916_173616.html / .xlsx
REPORT_FILE_PREFIX = "Friendcom"


def build_report_base_name(timestamp: str) -> str:
    """报告文件名（不含扩展名）：Friendcom_<YYYYmmdd_HHMMSS>。"""
    return f"{REPORT_FILE_PREFIX}_{timestamp}"


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DTP Static Analysis Violations Comparison Report Generator",
    )
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to config.yaml (default: config.yaml)")
    parser.add_argument("--url", help="DTP server URL (overrides config)")
    parser.add_argument("--user", help="DTP username (overrides config)")
    parser.add_argument("--password", help="DTP password (overrides config)")
    parser.add_argument("--projects", help="Comma-separated project names to analyze (default: source projects of merge_projects)")
    parser.add_argument("--output", help="Output directory (overrides config)")
    parser.add_argument("--locale", choices=["zh", "en", "v2_zh", "v2_en", "v3_zh"], help="Report language/template: zh, en, v2_zh, v2_en, v3_zh (overrides config)")
    return parser


def main():
    parser = build_argparser()
    args = parser.parse_args()

    cfg = load_config(args.config)
    dtp_cfg = cfg.get("dtp", {})
    report_cfg = cfg.get("report", {})
    #my define merge_projects
    merge_projects = cfg.get("merge_projects", {})
    

    url = args.url or dtp_cfg.get("url", "http://localhost:9191")
    username = args.user or dtp_cfg.get("username", "admin")
    password = args.password or dtp_cfg.get("password", "admin")
    output_dir = args.output or report_cfg.get("output_dir", "./output")
    top_n_bad = report_cfg.get("top_n_bad", 3)
    top_n_good = report_cfg.get("top_n_good", 3)
    locale = args.locale or report_cfg.get("locale_friendcom", "v2_zh")

    # 抓取范围优先级：--projects > report.project_names > merge_projects 的源项目
    # 解析结果为空时 collect_violations 会回退为扫描 DTP 全部项目（很慢，易超时）
    project_names = None
    if args.projects:
        project_names = [p.strip() for p in args.projects.split(",") if p.strip()]
    elif report_cfg.get("project_names"):
        project_names = [str(p).strip() for p in report_cfg["project_names"] if str(p).strip()]
    else:
        project_names = list_source_projects(merge_projects)

    if not project_names:
        logger.warning("No project names resolved, will scan ALL projects on DTP (slow, prone to timeouts).")

    logger.info("Connecting to DTP at %s", url)
    client = DTPClient(url=url, username=username, password=password)  
    
    #pdb.set_trace()	#set breakpoint
    # with_file_count=True: 额外从 DTP 查询每个仓库"扫描的源文件数"（/staticAnalysisFiles），
    #                       用于报告摘要的"文件总数"和项目概览的"文件数"；
    # with_code_lines=True: 额外从 DTP 查询每个仓库"代码行数"（metrics 指标 METRIC.NOPLIF），
    #                       用于报告摘要的"代码总数"和项目概览的"代码行数"。
    # （main_project.py 不传这两个开关，行为不受影响）
    data = collect_violations(
        client,
        project_names=project_names,
        with_file_count=True,
        with_code_lines=True,
    )
    logger.info("Collected data for %d project(s) (raw, before merging)", len(data))
    logger.info(
        "Raw total over projects with data: scanned files=%d, code lines=%d "
        "(merged report total may be larger when one repo belongs to several groups)",
        sum(d.file_count for d in data if d.has_data),
        sum(d.code_lines for d in data if d.has_data),
    )

    # 有静态分析数据、但 DTP 该 build 未上报任何代码度量（metrics/types 为空）时，
    # 代码行数会是 0，报告中该行显示"无数据"——这里列出来，避免被当成"真的 0 行"。
    no_loc = [d.project_name for d in data if d.has_data and d.code_lines == 0]
    if no_loc:
        preview = ", ".join(no_loc[:10]) + (" ..." if len(no_loc) > 10 else "")
        logger.warning(
            "%d project(s) have static analysis data but no code-lines metric "
            "(the build reported no metrics): %s",
            len(no_loc), preview,
        )

    # 配置中的项目名必须与 DTP 项目名完全一致（merge_project 按名称精确匹配）
    if project_names:
        returned = {d.project_name for d in data}
        missing = [name for name in project_names if name not in returned]
        if missing:
            preview = ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")
            logger.warning("%d specified project(s) not found on DTP: %s", len(missing), preview)
    
    #debug spln_3p project merge
    merged_violations = merge_project_violations(data,merge_projects,)
    #print(merged_violations)
    
    #从 data 中找出所有 has_data 为真（True）的对象，组成一个新的列表 active
    active = [d for d in merged_violations if d.has_data]
    if not active:
        logger.warning("No projects with static analysis data found.")
        sys.exit(0)

    logger.info(
        "Report totals across %d merged project(s): scanned files=%d, code lines=%d",
        len(active),
        sum(d.file_count for d in active),
        sum(d.code_lines for d in active),
    )

    logger.info("Generating charts...")
    charts = generate_all_charts(merged_violations, top_n_bad=top_n_bad, top_n_good=top_n_good)

    out = Path(output_dir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 清理 output 目录下所有文件，然后重新生成
    if out.exists():
        logger.info("Cleaning output directory: %s", out.resolve())
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    logger.info("Output directory ready: %s", out.resolve())

    base_name = build_report_base_name(timestamp)

    logger.info("Generating HTML report (locale=%s)...", locale)
    html_path = generate_html_report(
        merged_violations, charts, out / f"{base_name}.html",
        dtp_url=url, locale=locale,
    )
    logger.info("HTML report: %s", html_path)

    logger.info("Generating Excel report...")
    xlsx_path = generate_excel_report(merged_violations, out / f"{base_name}.xlsx")
    logger.info("Excel report: %s", xlsx_path)

    logger.info("Done! Reports generated in %s", out.resolve())
       

if __name__ == "__main__":
    main()