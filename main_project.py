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
from merge_project import merge_project_violations

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


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DTP Static Analysis Violations Comparison Report Generator",
    )
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to config.yaml (default: config.yaml)")
    parser.add_argument("--url", help="DTP server URL (overrides config)")
    parser.add_argument("--user", help="DTP username (overrides config)")
    parser.add_argument("--password", help="DTP password (overrides config)")
    parser.add_argument("--projects", help="Comma-separated project names to analyze (default: all)")
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
    locale = args.locale or report_cfg.get("locale_project", "v3_zh")

    project_names = None
    if args.projects:
        project_names = [p.strip() for p in args.projects.split(",")]
    elif report_cfg.get("project_names"):
        project_names = report_cfg["project_names"] 
    
    logger.info("Connecting to DTP at %s", url)
    client = DTPClient(url=url, username=username, password=password)  
    
    #pdb.set_trace()	#set breakpoint
    data = collect_violations(client, project_names=project_names)
    logger.info("Collected data for %d project(s)", len(data))
    
    #debug spln_3p project merge
    merged_violations = merge_project_violations(data,merge_projects,)
    #print(merged_violations)
    
    #从 data 中找出所有 has_data 为真（True）的对象，组成一个新的列表 active
    active = [d for d in merged_violations if d.has_data]
    if not active:
        logger.warning("No projects with static analysis data found.")
        sys.exit(0)

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

    logger.info("Generating HTML report (locale=%s)...", locale)
    html_path = generate_html_report(
        merged_violations, charts, out / f"dtp_sa_report_{timestamp}.html",
        dtp_url=url, locale=locale,
    )
    logger.info("HTML report: %s", html_path)

    logger.info("Generating Excel report...")
    xlsx_path = generate_excel_report(merged_violations, out / f"dtp_sa_report_{timestamp}.xlsx")
    logger.info("Excel report: %s", xlsx_path)

    logger.info("Done! Reports generated in %s", out.resolve())
       

if __name__ == "__main__":
    main()