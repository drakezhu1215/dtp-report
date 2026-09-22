#!/usr/bin/env python3
"""DTP 报告生成器 —— 统一主入口（连一次 DTP，出三类报告）。

==========  ==============================================
报告类型    内容
==========  ==============================================
公司报告    跨项目对比：合并 config.yaml 的 merge_projects
            后的公司级汇总（Friendcom_<时间戳>.html/.xlsx）
项目报告    每个 config.yaml 项目一份报告
开发者报告  每个 DTP 作者一份报告
==========  ==============================================

两类入口（本文件只做“分发 + 命令行参数 + 命令行走流程”）：

- 图形界面：``python main.py``（或 ``python main.py --gui``）-> :func:`gui_app.launch_gui`
  （先“连接 DTP 并获取数据”，数据就绪后三块各自出报告）；
- 命令行：``python main.py --all`` 等 -> :func:`main`（抓一次数据后按选中的类型出报告）：

    python main.py                                   # 无参数 -> 打开图形界面
    python main.py --gui                             # 显式打开图形界面
    python main.py --all                             # 公司 + 项目 + 开发者，全都出
    python main.py --company                         # 只出公司报告
    python main.py --project-keys 海外三相电表平台,印尼集中器平台   # 只出这些项目的报告
    python main.py --authors hendra,zhangsan --max-workers 8       # 只出这些开发者的报告
    python main.py --list-authors                    # 只打印 DTP 上的作者清单，不出报告

三类报告写入 config.yaml 的 ``report.output_dir`` 下**各自独立的子目录**
（步骤 1 开始时只清空一次**根目录**，与升级前“每次运行只保留本次报告”的行为一致）：

- 公司报告   -> ``output/company/Friendcom_<时间戳>.html`` + ``.xlsx``
- 项目报告   -> ``output/project/<项目名>_report_<时间戳>.html`` + ``.xlsx``
- 开发者报告 -> ``output/author/<作者名>_developer_report_<时间戳>.html`` + ``.xlsx``

模块地图（main.py 只保留入口，具体实现都在各自模块里）：

=================  ==========================================================
data_pipeline.py   抓取层：``DataBundle`` + ``fetch_all_data``（连一次 DTP 抓完三类数据）
report_service.py  生成层：输出子目录约定 + 三类报告的生成（``generate_*``）
gui_app.py         界面：``UnifiedReportApp``（主窗口）+ ``launch_gui``
gui_widgets.py     界面控件：双向列表选择器 + 进度条控制器
gui_common.py      界面公共设施：日志队列 handler / 系统文件管理器
=================  ==========================================================

底层：``dtp_client``（REST 客户端）、``analyzer`` / ``analyzer_author``（数据采集）、
``project_merge`` / ``chart_builder``（合并与图表）、``report_html`` / ``report_excel`` /
``report_project`` / ``report_author``（渲染）。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from report_author import format_author_table, split_names

# 抓取与生成分别由下面两个模块实现；这里再导出一次，兼容既有的 main.xxx 引用方式
from data_pipeline import DataBundle, fetch_all_data  # noqa: F401
from report_service import (  # noqa: F401
    REPORT_KINDS,
    REPORT_SUBDIRS,
    generate_author_reports_for,
    generate_company_report,
    generate_project_reports_for,
    report_out_dir,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 命令行模式（公司 / 项目 / 开发者三类报告的参数集合）
# ---------------------------------------------------------------------------
def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DTP 报告生成器（统一入口：公司报告 + 项目报告 + 开发者报告，一次抓取三类全出）",
        epilog="不带任何参数运行 -> 打开图形界面；只给连接类参数（-c/--url/--output...）时三类报告全出。",
    )
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to config.yaml (default: config.yaml)")
    parser.add_argument("--url", help="DTP server URL (overrides config)")
    parser.add_argument("--user", help="DTP username (overrides config)")
    parser.add_argument("--password", help="DTP password (overrides config)")
    parser.add_argument(
        "--output",
        help="报告根目录（覆盖 config）：三类报告分别写入其下的 company/、project/、author/ 子目录",
    )
    parser.add_argument(
        "--trend-builds", type=int,
        help="趋势数据口径：0 = 每个仓库的全部构建（默认，每次构建一个趋势点）；"
             "N>0 = 只取最近 N 次构建；负数 = 不抓趋势数据",
    )
    parser.add_argument("--max-workers", type=int, help="并发抓取线程数")

    parser.add_argument("--company", action="store_true", help="生成公司报告（跨项目对比的公司级汇总）")
    parser.add_argument("--project-keys", help="逗号分隔的项目名（config.yaml merge_projects 的 key），生成这些项目的报告")
    parser.add_argument("--projects", help="Comma-separated DTP project/repo names to analyze (项目范围：DTP 仓库名)")
    parser.add_argument("--authors", help="逗号分隔的开发者名，生成这些开发者的报告（不给 = 抓到的全部作者）")
    parser.add_argument("--list-authors", action="store_true", help="只打印 DTP 上的作者清单，不出报告")
    parser.add_argument("--all", action="store_true", help="公司 + 项目 + 开发者报告全出")
    # 仅用于 --help 展示：真正遇到 -g/--gui 时在 run_entry 里就直接打开界面了（不会解析到这里）
    parser.add_argument("-g", "--gui", action="store_true", help="打开图形界面（等同不带任何参数运行）")
    return parser


def main(argv: list[str] | None = None) -> None:
    """命令行入口：抓一次数据，按选中的类型出报告。"""
    parser = build_argparser()
    args = parser.parse_args(argv)

    # 没指定任何报告类型时（例如只给了 -c/--output）：三类全出，与界面上"数据就绪后三块都能出"一致
    company = args.company or args.all
    projects = bool(args.project_keys or args.projects) or args.all
    authors = bool(args.authors) or args.list_authors or args.all
    if not (company or projects or authors):
        logger.info("未指定报告类型，按默认生成公司 + 项目 + 开发者三类报告。")
        company = projects = authors = True

    project_keys = split_names(args.project_keys)
    source_projects = split_names(args.projects)

    try:
        bundle = fetch_all_data(
            args.config,
            url=args.url, username=args.user, password=args.password,
            output_dir=args.output, trend_builds=args.trend_builds, max_workers=args.max_workers,
            source_projects=source_projects or None, project_keys=project_keys or None,
            # 只列开发者 / 只出公司报告时，构建趋势数据用不到，跳过可以省掉最慢的一轮抓取
            with_trends=projects and not args.list_authors,
        )
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    if args.list_authors:
        for line in format_author_table(bundle.index):
            print(line)
        return

    generated: list[Path] = []
    if company:
        generated += generate_company_report(bundle)
    if projects:
        generated += generate_project_reports_for(bundle, project_keys or None)
    if authors:
        generated += generate_author_reports_for(bundle, split_names(args.authors) or None)

    logger.info(
        "全部完成：共生成 %d 份 HTML 报告（另有同名 .xlsx）-> %s",
        len(generated), bundle.output_dir.resolve(),
    )
    for kind, enabled in (("company", company), ("project", projects), ("author", authors)):
        if enabled:
            logger.info(
                "  %s目录：%s",
                REPORT_KINDS[kind], (bundle.output_dir / REPORT_SUBDIRS[kind]).resolve(),
            )


def _gui_requested(argv: list[str]) -> bool:
    """无参数、或命令里带 --gui / -g 时打开图形界面（与其它入口的约定一致）。"""
    if not argv:
        return True
    return "--gui" in argv or "-g" in argv


def _cli_config_path(argv: list[str], default: str = "config.yaml") -> str:
    """从命令行里取 -c/--config 指定的配置文件，供图形界面使用。"""
    for index, token in enumerate(argv):
        if token in ("-c", "--config") and index + 1 < len(argv):
            return argv[index + 1]
        if token.startswith("--config="):
            return token.split("=", 1)[1]
    return default


def run_entry(argv: list[str] | None = None) -> None:
    """统一入口分发：无参数 / --gui / -g -> 图形界面，其它参数 -> 命令行模式。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if _gui_requested(args):
        # 只有走到这里才会加载界面模块与 tkinter（launch_gui 内部会再检查依赖是否可用）
        from gui_app import launch_gui

        launch_gui(config_path=_cli_config_path(args))
        return
    main(args)


if __name__ == "__main__":
    run_entry()
