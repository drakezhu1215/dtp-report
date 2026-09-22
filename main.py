#!/usr/bin/env python3
"""DTP 报告生成器 —— 统一主入口（连一次 DTP，出三类报告）。

本文件是唯一的入口（连一次 DTP，一次出三类报告）：

==========  ==============================================
报告类型    内容
==========  ==============================================
公司报告    跨项目对比：合并 config.yaml 的 merge_projects
            后的公司级汇总（Friendcom_<时间戳>.html/.xlsx）
项目报告    每个 config.yaml 项目一份报告
开发者报告  每个 DTP 作者一份报告
==========  ==============================================

界面（``python main.py``，或 ``python main.py --gui``）：

1. "① 连接 DTP 并获取数据"：自动连接 DTP，一次性把三类报告需要的全部数据抓完
   （仓库违规/文件数/代码行数 -> 开发者索引 -> 构建趋势），右侧进度条显示抓取进度，
   100% 表示数据全部就绪；数据没就绪时三个报告按钮都是灰的；
2. 数据就绪后界面出现三块（选项卡），每块都能独立生成报告（都复用已抓好的数据，
   不会重新访问 DTP，只有开发者报告会补抓"作者在各仓库的严重级别"）：

   - 公司："公司报告"按钮；
   - 项目：列出 config.yaml 的全部项目，可以添加 / 移除 / 全选 / 全部移除，
     "项目报告"按钮为每个已选项目生成一份报告；
   - 开发者：列出 DTP 上的全部作者，同样可以添加 / 移除 / 全选 / 全部移除，
     "开发者报告"按钮为每位已选作者生成一份报告；

3. 三类报告都写入 config.yaml 的 ``report.output_dir``（步骤 1 开始时清空一次，
   与升级前"每次运行只保留本次报告"的行为一致），文件名互不冲突：

   ``Friendcom_<时间戳>.*`` / ``<项目名>_report_<时间戳>.*`` / ``<作者名>_developer_report_<时间戳>.*``

命令行（抓一次数据后按选中的类型出报告）：

    python main.py                                   # 无参数 -> 打开上面的界面
    python main.py --gui                             # 显式打开界面
    python main.py --all                             # 公司 + 项目 + 开发者，全都出
    python main.py --company                         # 只出公司报告
    python main.py --project-keys 海外三相电表平台,印尼集中器平台   # 只出这些项目的报告
    python main.py --authors hendra,zhangsan --max-workers 8       # 只出这些开发者的报告
    python main.py --list-authors                    # 只打印 DTP 上的作者清单，不出报告

层次：本模块是**编排层**（抓取顺序 + 进度 + 界面 + 命令行），报告生成完全复用
``report_project`` / ``report_author`` / ``project_merge`` 里的既有实现，
日志与系统集成工具来自 ``gui_common``。
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from analyzer import (
    ProjectViolations,
    RepoBuildTrend,
    collect_build_trends,
    collect_violations,
)
from analyzer_author import AuthorIndex, collect_author_index
from report_author import (
    DEFAULT_TREND_BUILDS,
    format_author_table,
    generate_author_reports,
    load_config,
    prepare_output_dir,
    resolve_repo_names,
    resolve_settings,
    split_names,
    trend_max_builds,
    warn_missing_repos,
)
from chart_builder import generate_all_charts
from dtp_client import DTPClient
from project_merge import (
    group_project_violations,
    merge_project_violations,
    unique_repo_violations,
)
from report_project import generate_project_reports
from report_excel import generate_excel_report
from report_html import generate_html_report

# 图形界面依赖：tkinter（标准库）+ gui_common（自己模块，内部也 import tkinter.messagebox）。
# 极少数环境装不出 tkinter（如没装 python3-tk 的 Linux），这时命令行模式仍然可用——
# launch_gui() 会明确提示“请改用命令行”。
try:
    import queue
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    from gui_common import (
        LOG_POLL_MS,
        MAX_LOG_LINES,
        TRIM_LOG_LINES,
        QueueLogHandler,
        QueueStream,
        open_in_file_manager,
    )

    GUI_AVAILABLE = True
except ImportError:  # pragma: no cover - 取决于运行环境
    tk = ttk = filedialog = messagebox = None  # type: ignore[assignment]
    QueueLogHandler = QueueStream = None  # type: ignore[assignment]
    open_in_file_manager = None  # type: ignore[assignment]
    LOG_POLL_MS, MAX_LOG_LINES, TRIM_LOG_LINES = 100, 5000, 1000
    GUI_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# 三类报告的默认模板，与 config.yaml 保持一致
DEFAULT_COMPANY_LOCALE = "v2_zh"
DEFAULT_PROJECT_LOCALE = "v3_zh"
DEFAULT_AUTHOR_LOCALE = "v4_zh"

# 公司报告文件名前缀：Friendcom_20260916_173616.html / .xlsx
COMPANY_REPORT_PREFIX = "Friendcom"


def build_company_base_name(timestamp: str) -> str:
    """公司报告文件名（不含扩展名）：Friendcom_<YYYYmmdd_HHMMSS>。"""
    return f"{COMPANY_REPORT_PREFIX}_{timestamp}"


@dataclass
class DataBundle:
    """步骤 1 抓到的全部数据：三类报告共用同一份快照，不再重复访问 DTP。"""

    settings: dict
    client: DTPClient
    output_dir: Path
    data: list[ProjectViolations]
    index: AuthorIndex
    trends_by_repo: dict[str, RepoBuildTrend]

    @property
    def merge_projects(self) -> dict:
        """config.yaml 的 ``merge_projects``：项目名 -> 仓库列表。"""
        return self.settings["merge_projects"]

    @property
    def report_cfg(self) -> dict:
        return self.settings["report_cfg"]

    @property
    def url(self) -> str:
        return self.settings["url"]

    @property
    def has_data(self) -> bool:
        """是否抓到可用的仓库数据（界面用它决定报告按钮能否点击）。"""
        return any(item.has_data for item in self.data)


# ---------------------------------------------------------------------------
# 步骤 1：连接 DTP，抓取三类报告需要的全部数据
# ---------------------------------------------------------------------------
def fetch_all_data(
    config_path: str = "config.yaml",
    *,
    url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    output_dir: str | None = None,
    trend_builds: int | None = None,
    max_workers: int | None = None,
    source_projects: list[str] | None = None,
    project_keys: list[str] | None = None,
    with_trends: bool = True,
    progress: Callable[[float, str], None] | None = None,
) -> DataBundle:
    """自动连接 DTP，一次性抓完三类报告需要的全部数据。

    抓取顺序（每一步都是下一步的基础）：

    1. ``collect_violations``：仓库级违规数 + 文件数 + 代码行数，全部取自每个仓库
       **最近一次构建**（公司报告 / 项目报告的基础；开发者报告也需要它，因为作者索引
       挂在仓库信息上）；
    2. ``collect_author_index``：DTP 全部作者 + 每个仓库**每次构建**的作者分布
       （开发者报告用；这是**最慢**的一步，因为每个仓库的每次构建都要请求一次）；
    3. ``collect_build_trends``：每个仓库**每次构建**的违规数（项目报告趋势图用）。

    口径：**趋势数据（每个仓库每次构建的违规数，含按作者拆分的部分）默认取全部构建；
    其它数据一律只取每个仓库最近一次构建。** 构建特别多时可用 ``trend_builds`` 限制成
    "最近 N 次"，语义见 :func:`report_author.trend_max_builds`。

    :param progress: ``progress(percent, message)`` 回调，percent 为 0~100，界面用它驱动进度条；
        为 ``None`` 时只写日志（命令行模式）。
    :param with_trends: 是否需要**仓库级**构建趋势数据（只有项目报告用；只出公司报告 /
        只列开发者清单时传 ``False`` 可以少抓一轮。开发者报告的趋势来自
        ``collect_author_index`` 的缓存，不受这个参数影响）。

    输出目录会在这里**清空并重建一次**（每次运行只保留本次生成的报告），
    之后三类报告都写进这个目录，互相不会覆盖。
    """
    def report(percent: float, message: str) -> None:
        """上报进度：先写日志，再通知界面（界面回调只是 post 到主线程，不会阻塞抓取）。"""
        logger.info("[%3d%%] %s", int(percent), message)
        if progress is not None:
            progress(percent, message)

    report(0, f"读取配置文件：{Path(config_path).resolve()}")
    settings = resolve_settings(
        config_path,
        url=url, username=username, password=password,
        output_dir=output_dir, trend_builds=trend_builds, max_workers=max_workers,
    )
    repo_names = resolve_repo_names(
        settings["report_cfg"], settings["merge_projects"],
        explicit=source_projects, project_keys=project_keys,
    )
    if not repo_names:
        # 解析不出来时 analyzer.collect_violations 会扫描 DTP 全部项目（很慢）
        logger.warning("没有解析出任何仓库名，将扫描 DTP 上的全部项目（很慢，容易超时）。")

    out = prepare_output_dir(settings["output_dir"])

    report(5, f"连接 DTP：{settings['url']}（用户 {settings['username']}）")
    client = DTPClient(
        url=settings["url"], username=settings["username"], password=settings["password"],
    )
    # get_projects() 是最轻的接口，用它确认 URL / 账号密码能通（失败会抛异常给调用方）
    dtp_projects = client.get_projects()
    logger.info(
        "DTP 连接成功：服务器上有 %d 个项目，本次扫描 %d 个仓库",
        len(dtp_projects), len(repo_names),
    )

    report(12, f"抓取仓库数据（{len(repo_names)} 个仓库最近一次构建：违规数 / 文件数 / 代码行数）...")
    data = collect_violations(
        client, project_names=repo_names, with_file_count=True, with_code_lines=True,
    )
    warn_missing_repos(data, repo_names)
    logger.info(
        "仓库数据完成：%d 条仓库记录，其中 %d 条有静态分析数据",
        len(data), len([d for d in data if d.has_data]),
    )

    # 趋势数据口径：trend_builds = 0 -> 全部构建（max_builds=None）；>0 -> 最近 N 次；
    # <0 -> 不抓趋势。scope 只用于日志/进度文案。
    trend_builds = int(settings["trend_builds"])
    max_builds = trend_max_builds(settings)
    if max_builds is None:
        scope = "全部构建"
    elif max_builds > 0:
        scope = f"最近 {max_builds} 次构建"
    else:
        scope = "最新一次构建（不抓趋势）"

    report(
        55,
        f"抓取开发者索引（每个仓库{scope}的作者分布，"
        f"并发 {settings['max_workers']} 线程）...",
    )
    index = collect_author_index(
        client, data,
        max_builds=max_builds, max_workers=settings["max_workers"],
    )
    logger.info("开发者索引完成：%d 位开发者，%d 个仓库", len(index.authors), len(index.repos))

    trends: dict[str, RepoBuildTrend] = {}
    if with_trends and trend_builds >= 0:
        report(88, f"抓取构建趋势（每个仓库{scope}的违规数）...")
        trend_repos = unique_repo_violations(
            group_project_violations(data, settings["merge_projects"])
        )
        trends = collect_build_trends(
            client, trend_repos,
            max_builds=max_builds, max_workers=settings["max_workers"],
        )
        logger.info(
            "构建趋势完成：%d 个仓库，其中 %d 个取到了趋势点",
            len(trends), len([t for t in trends.values() if t.has_data]),
        )
    else:
        logger.warning("已跳过构建趋势数据的抓取：项目报告里不会有趋势图。")

    report(100, "数据获取完成")
    return DataBundle(
        settings=settings, client=client, output_dir=out,
        data=data, index=index, trends_by_repo=trends,
    )


# ---------------------------------------------------------------------------
# 三类报告的生成（全部复用步骤 1 抓到的 DataBundle，不再重复访问 DTP）
# ---------------------------------------------------------------------------
def _ensure_out_dir(out_dir: Path | str) -> Path:
    """确保输出目录存在（生成阶段只创建、不再清空，否则会删掉刚生成的报告）。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    return out


def generate_company_report(
    bundle: DataBundle, *, out_dir: Path | str | None = None
) -> list[Path]:
    """生成公司级报告（把 config.yaml 的 merge_projects 合并后做跨项目对比）。

    合并 config.yaml 的 merge_projects 后做公司级汇总：摘要卡按"唯一仓库"去重统计，
    项目概览 / 违规警示榜 / 合规模范榜等按合并组逐行展示。
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
    )

    out = _ensure_out_dir(out_dir or bundle.output_dir)
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
    """为每个项目生成一份报告（每个项目 1 个 .html + .xlsx）。

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
    out = _ensure_out_dir(out_dir or bundle.output_dir)
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
    """为每位开发者生成一份报告（每位作者 1 个 .html + .xlsx）。

    :param selected_authors: 要出报告的开发者名列表；为 ``None``/空时对 DTP 上抓到的
        **全部作者**出报告。列表里允许有索引里没有的作者（界面上的"手工添加"），
        这类作者会走 ``author=`` 的兜底查询路径。
    """
    authors = [name for name in (selected_authors or bundle.index.authors) if name]
    if not authors:
        logger.warning("开发者报告：没有可生成报告的开发者（DTP 上没有找到任何作者）。")
        return []

    out = _ensure_out_dir(out_dir or bundle.output_dir)
    settings = dict(bundle.settings)
    settings["output_dir"] = str(out)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    logger.info("开发者报告：%d 位开发者，模板 %s", len(authors), settings["locale"])
    return generate_author_reports(
        bundle.client, bundle.index, authors, out, timestamp, settings,
    )


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
    parser.add_argument("--output", help="Output directory (overrides config)")
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


# ---------------------------------------------------------------------------
# 图形界面
# ---------------------------------------------------------------------------
# 三块（选项卡）的报告类型 -> 界面显示名
REPORT_KINDS = {
    "company": "公司报告",
    "project": "项目报告",
    "author": "开发者报告",
}


class _DualListSelector:
    """左右双向列表选择器（"项目"与"开发者"两块共用）。

    左边"可选"列表（带搜索框、可多选），中间是"添加 / 移除 / 全部添加 / 全部移除"，
    右边"已选"列表。各块的报告按钮用 :meth:`selected_items` 取当前选择。
    """

    def __init__(self, parent: "ttk.Frame", *, available_title: str, selected_title: str,
                 height: int = 11) -> None:
        self.all_items: list[str] = []
        self.selected: list[str] = []

        self.search_var = tk.StringVar()
        self.count_var = tk.StringVar(value="已选 0 / 0 项")

        frame = ttk.Frame(parent)
        self.frame = frame
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(2, weight=1)
        frame.rowconfigure(0, weight=1)

        # ---- 左：全部可选 ----
        left = ttk.LabelFrame(frame, text=available_title, padding=6)
        left.grid(row=0, column=0, sticky="nsew")
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)

        search_row = ttk.Frame(left)
        search_row.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        search_row.columnconfigure(1, weight=1)
        ttk.Label(search_row, text="搜索：").grid(row=0, column=0, sticky="w")
        ttk.Entry(search_row, textvariable=self.search_var).grid(row=0, column=1, sticky="ew")
        # 输入即过滤（不必按回车）
        self.search_var.trace_add("write", lambda *_: self.refresh())

        self.available_list = tk.Listbox(
            left, selectmode="extended", height=height, activestyle="none",
            exportselection=False,
        )
        self.available_list.grid(row=1, column=0, sticky="nsew")
        available_scroll = ttk.Scrollbar(left, orient="vertical", command=self.available_list.yview)
        available_scroll.grid(row=1, column=1, sticky="ns")
        self.available_list.configure(yscrollcommand=available_scroll.set)
        # 双击左侧 = 添加，双击右侧 = 移除（比来回点按钮顺手）
        self.available_list.bind("<Double-Button-1>", lambda _event: self.add_selected())

        # ---- 中：添加 / 移除 ----
        middle = ttk.Frame(frame)
        middle.grid(row=0, column=1, sticky="ns", padx=6)
        ttk.Button(middle, text="添加 →", command=self.add_selected, width=10).pack(pady=(0, 4))
        ttk.Button(middle, text="← 移除", command=self.remove_selected, width=10).pack(pady=4)
        ttk.Button(middle, text="全部添加", command=self.add_all, width=10).pack(pady=4)
        ttk.Button(middle, text="全部移除", command=self.remove_all, width=10).pack(pady=4)

        # ---- 右：已选 ----
        right = ttk.LabelFrame(frame, text=selected_title, padding=6)
        right.grid(row=0, column=2, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)

        self.selected_list = tk.Listbox(
            right, selectmode="extended", height=height, activestyle="none",
            exportselection=False,
        )
        self.selected_list.grid(row=0, column=0, sticky="nsew")
        selected_scroll = ttk.Scrollbar(right, orient="vertical", command=self.selected_list.yview)
        selected_scroll.grid(row=0, column=1, sticky="ns")
        self.selected_list.configure(yscrollcommand=selected_scroll.set)
        self.selected_list.bind("<Double-Button-1>", lambda _event: self.remove_selected())

        ttk.Label(frame, textvariable=self.count_var, foreground="#333").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(4, 0)
        )

    # ------------------------------------------------------------------
    # 数据
    # ------------------------------------------------------------------
    def set_items(self, items: list[str]) -> None:
        """重新设置"全部可选"列表（步骤 1 抓到数据后调用）；已选项里失效的会被丢掉。"""
        self.all_items = list(items)
        valid = set(self.all_items)
        self.selected = [name for name in self.selected if name in valid]
        self.refresh()

    def add_item(self, name: str) -> bool:
        """加一个候选项并选中它（"手工添加"用）；已存在时只保证它被选中。"""
        name = name.strip()
        if not name:
            return False
        if name not in self.all_items:
            self.all_items.append(name)
        if name not in self.selected:
            self.selected.append(name)
        self.refresh()
        return True

    def selected_items(self) -> list[str]:
        """当前的已选项（顺序 = 生成报告的先后顺序）。"""
        return list(self.selected)

    def refresh(self) -> None:
        """刷新左右两个列表（左侧隐藏已选项，并按搜索关键字过滤）。"""
        keyword = self.search_var.get().strip().lower()
        chosen = set(self.selected)

        self.available_list.delete(0, "end")
        for name in self.all_items:
            if name in chosen:
                continue
            if keyword and keyword not in name.lower():
                continue
            self.available_list.insert("end", name)

        self.selected_list.delete(0, "end")
        for name in self.selected:
            self.selected_list.insert("end", name)

        self.count_var.set(f"已选 {len(self.selected)} / {len(self.all_items)} 项")

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------
    def add_selected(self) -> None:
        for name in [self.available_list.get(i) for i in self.available_list.curselection()]:
            if name not in self.selected:
                self.selected.append(name)
        self.refresh()

    def remove_selected(self) -> None:
        for name in [self.selected_list.get(i) for i in self.selected_list.curselection()]:
            if name in self.selected:
                self.selected.remove(name)
        self.refresh()

    def add_all(self) -> None:
        """全部添加：只添加当前搜索条件下左侧可见的项（与界面显示保持一致）。"""
        for name in self.available_list.get(0, "end"):
            if name not in self.selected:
                self.selected.append(name)
        self.refresh()

    def remove_all(self) -> None:
        self.selected.clear()
        self.refresh()


class UnifiedReportApp:
    """统一报告窗口：先点"① 连接 DTP 并获取数据"（带进度条），然后公司 / 项目 / 开发者三块各自出报告。"""

    def __init__(self, root: "tk.Tk", config_path: str = "config.yaml") -> None:
        self.root = root
        self.config_path = config_path

        # 后台线程 -> 主线程的消息队列（日志与界面回调都走队列，worker 不碰 Tk）
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        # 后台线程要执行的界面回调：worker 只把 (函数, 参数) 放进这里，由主线程轮询执行。
        # 这样 worker 完全不碰 Tk（即使窗口已经关掉也不会抛 "main thread is not in main loop"）
        self.ui_queue: "queue.Queue[tuple]" = queue.Queue()
        # 步骤 1 抓到的数据；为 None 表示还没获取数据（三个报告按钮保持灰的）
        self.bundle: DataBundle | None = None
        self.worker: threading.Thread | None = None

        # 进度条（determinate）：worker 线程只抬高 progress_target，主线程的心跳定时器
        # 把 progress_value 平滑地推向 target，避免最慢的两步长时间停在同一个刻度上
        self.progress_value = 0.0
        self.progress_target = 0.0
        self.progress_job: "str | None" = None
        self.progress_mode = "idle"  # idle / fetching / generating
        # 日志轮询的 after 任务（关闭窗口时取消，避免 Tk 解释器销毁后残留回调报错）
        self.log_poll_job: "str | None" = None

        self._build_ui()
        self._install_logging()
        self._reload_config(initial=True)
        self._poll_log_queue()
        self._refresh_buttons()

    # ------------------------------------------------------------------
    # 界面搭建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.root.title("DTP 报告生成器（公司 / 项目 / 开发者）")
        self.root.geometry("1120x900")
        self.root.minsize(900, 700)

        style = ttk.Style(self.root)
        for theme in ("vista", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break

        container = ttk.Frame(self.root, padding=10)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(2, weight=3)  # 三块（选项卡）
        container.rowconfigure(3, weight=2)  # 日志

        self._build_config_frame(container)  # row 0
        self._build_fetch_frame(container)   # row 1
        self._build_notebook(container)      # row 2
        self._build_log_frame(container)     # row 3

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_config_frame(self, parent: "ttk.Frame") -> None:
        frame = ttk.LabelFrame(parent, text="配置信息（config.yaml）", padding=8)
        frame.grid(row=0, column=0, sticky="ew")
        frame.columnconfigure(1, weight=1)

        self.info_vars = {
            "config": tk.StringVar(value=str(Path(self.config_path).resolve())),
            "url": tk.StringVar(value="-"),
            "output": tk.StringVar(value="-"),
            "company": tk.StringVar(value="-"),
            "project": tk.StringVar(value="-"),
            "author": tk.StringVar(value="-"),
            "options": tk.StringVar(value="-"),
        }
        rows = (
            ("配置文件", "config"),
            ("DTP 服务器", "url"),
            ("输出目录", "output"),
            ("公司报告模板", "company"),
            ("项目报告模板", "project"),
            ("开发者报告模板", "author"),
            ("抓取选项", "options"),
        )
        for row_index, (label, key) in enumerate(rows):
            ttk.Label(frame, text=f"{label}：").grid(row=row_index, column=0, sticky="w", padx=(0, 6))
            ttk.Label(frame, textvariable=self.info_vars[key]).grid(
                row=row_index, column=1, sticky="w"
            )

        ttk.Button(frame, text="重新加载配置", command=self._reload_config).grid(
            row=0, column=2, rowspan=len(rows), sticky="e", padx=(10, 0)
        )

    def _build_fetch_frame(self, parent: "ttk.Frame") -> None:
        frame = ttk.LabelFrame(
            parent, text="① 连接 DTP 并获取数据（三类报告共用这一份数据）", padding=8
        )
        frame.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        frame.columnconfigure(2, weight=1)

        self.fetch_button = ttk.Button(
            frame, text="连接 DTP 并获取数据", command=self.on_fetch_data, width=22
        )
        self.fetch_button.grid(row=0, column=0, sticky="w")

        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=100, length=260)
        self.progress.grid(row=0, column=1, sticky="w", padx=(10, 6))

        self.progress_label = tk.StringVar(value="0%")
        ttk.Label(frame, textvariable=self.progress_label, width=5).grid(
            row=0, column=2, sticky="w"
        )

        self.fetch_status_var = tk.StringVar(
            value="尚未获取数据：数据就绪后下面三块的报告按钮才会变成可点击。"
        )
        ttk.Label(frame, textvariable=self.fetch_status_var, foreground="#333").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(6, 0)
        )
        self.data_summary_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.data_summary_var, foreground="#0a6a00").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(2, 0)
        )

    def _build_notebook(self, parent: "ttk.Frame") -> None:
        frame = ttk.Frame(parent)
        frame.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        notebook = ttk.Notebook(frame)
        notebook.grid(row=0, column=0, sticky="nsew")

        company_tab = ttk.Frame(notebook, padding=10)
        project_tab = ttk.Frame(notebook, padding=10)
        author_tab = ttk.Frame(notebook, padding=10)
        notebook.add(company_tab, text=" ② 公司报告 ")
        notebook.add(project_tab, text=" ③ 项目报告 ")
        notebook.add(author_tab, text=" ④ 开发者报告 ")

        self._build_company_tab(company_tab)
        self._build_project_tab(project_tab)
        self._build_author_tab(author_tab)

    def _build_company_tab(self, tab: "ttk.Frame") -> None:
        tab.columnconfigure(0, weight=1)

        ttk.Label(
            tab,
            text="公司报告：合并 config.yaml 的 merge_projects 后做公司级汇总，"
                 "输出 Friendcom_<时间戳>.html / .xlsx。",
            wraplength=1000, justify="left",
        ).grid(row=0, column=0, sticky="w")

        self.company_summary_var = tk.StringVar(value="等待数据：先在上方点击“连接 DTP 并获取数据”。")
        ttk.Label(tab, textvariable=self.company_summary_var, foreground="#333").grid(
            row=1, column=0, sticky="w", pady=(8, 0)
        )

        self.company_button = ttk.Button(
            tab, text="公司报告", command=self.on_generate_company, width=22
        )
        self.company_button.grid(row=2, column=0, sticky="w", pady=(12, 0))

        ttk.Label(
            tab,
            text="数据未就绪时按钮是灰的；点击后在后台生成，进度和结果都写在下方日志框里。",
            foreground="#666",
        ).grid(row=3, column=0, sticky="w", pady=(10, 0))

    def _build_project_tab(self, tab: "ttk.Frame") -> None:
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        ttk.Label(
            tab,
            text="项目报告：左侧是 config.yaml 的全部项目；添加要出报告的项目后点“项目报告”，"
                 "每个项目生成一份 <项目名>_report_<时间戳>.html / .xlsx。",
            wraplength=1000, justify="left",
        ).grid(row=0, column=0, sticky="w")

        self.project_selector = _DualListSelector(
            tab,
            available_title="可选项目（config.yaml 的 merge_projects）",
            selected_title="已选项目（每个项目一份报告）",
        )
        self.project_selector.frame.grid(row=1, column=0, sticky="nsew", pady=(8, 0))

        action = ttk.Frame(tab)
        action.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.project_button = ttk.Button(
            action, text="项目报告", command=self.on_generate_projects, width=22
        )
        self.project_button.grid(row=0, column=0, sticky="w")
        self.project_hint_var = tk.StringVar(value="")
        ttk.Label(action, textvariable=self.project_hint_var, foreground="#666").grid(
            row=0, column=1, sticky="w", padx=(10, 0)
        )

    def _build_author_tab(self, tab: "ttk.Frame") -> None:
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

        ttk.Label(
            tab,
            text="开发者报告：左侧是 DTP 上的全部作者；添加要出报告的开发者后点“开发者报告”，"
                 "每位作者生成一份 <作者名>_developer_report_<时间戳>.html / .xlsx。",
            wraplength=1000, justify="left",
        ).grid(row=0, column=0, sticky="w")

        manual = ttk.Frame(tab)
        manual.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(manual, text="手工添加（DTP 作者列表里还没有的人）：").pack(side="left")
        self.manual_author_var = tk.StringVar()
        manual_entry = ttk.Entry(manual, textvariable=self.manual_author_var, width=28)
        manual_entry.pack(side="left", padx=(6, 0))
        # 回车即添加，省得再去点按钮
        manual_entry.bind("<Return>", lambda _event: self.on_add_manual_author())
        ttk.Button(manual, text="添加", command=self.on_add_manual_author, width=8).pack(
            side="left", padx=(6, 0)
        )

        self.author_selector = _DualListSelector(
            tab,
            available_title="可选开发者（DTP 上的全部作者）",
            selected_title="已选开发者（每位一份报告）",
        )
        self.author_selector.frame.grid(row=2, column=0, sticky="nsew", pady=(8, 0))

        action = ttk.Frame(tab)
        action.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.author_button = ttk.Button(
            action, text="开发者报告", command=self.on_generate_authors, width=22
        )
        self.author_button.grid(row=0, column=0, sticky="w")
        self.author_hint_var = tk.StringVar(value="")
        ttk.Label(action, textvariable=self.author_hint_var, foreground="#666").grid(
            row=0, column=1, sticky="w", padx=(10, 0)
        )

    def _build_log_frame(self, parent: "ttk.Frame") -> None:
        frame = ttk.LabelFrame(parent, text="日志", padding=8)
        frame.grid(row=3, column=0, sticky="nsew", pady=(8, 0))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(
            frame, height=10, wrap="none", state="disabled",
            background="#1e1e1e", foreground="#d4d4d4",
            insertbackground="#d4d4d4", font=("Consolas", 9),
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")

        y_scroll = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll = ttk.Scrollbar(frame, orient="horizontal", command=self.log_text.xview)
        x_scroll.grid(row=1, column=0, sticky="ew")
        self.log_text.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

        toolbar = ttk.Frame(frame)
        toolbar.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(toolbar, text="打开输出目录", command=self.on_open_output, width=14).pack(side="left")
        ttk.Button(toolbar, text="保存日志", command=self.on_save_log, width=12).pack(
            side="left", padx=(8, 0)
        )

    # ------------------------------------------------------------------
    # 日志：logging 与 print/stderr 全部进日志框
    # ------------------------------------------------------------------
    def _install_logging(self) -> None:
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        self.log_handler = QueueLogHandler(self.log_queue)
        root_logger.addHandler(self.log_handler)

        # print() 与三方库写到 stderr 的警告也一并显示（同时保留原来的输出）
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr
        sys.stdout = QueueStream(self.log_queue, "stdout", self._original_stdout)
        sys.stderr = QueueStream(self.log_queue, "stderr", self._original_stderr)

        self.log_queue.put(f"[系统] 日志已接入界面；配置文件：{Path(self.config_path).resolve()}")

    def append_log(self, message: str) -> None:
        """把一行日志追加到日志框末尾（只在主线程调用）。"""
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        if int(self.log_text.index("end-1c").split(".")[0]) > MAX_LOG_LINES:
            self.log_text.delete("1.0", f"{TRIM_LOG_LINES + 1}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_log_queue(self) -> None:
        """定时把后台线程写入队列的内容搬到界面上（日志 + 界面回调）。"""
        try:
            while True:
                self.append_log(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        try:
            while True:
                callback, args = self.ui_queue.get_nowait()
                try:
                    callback(*args)
                except Exception:  # 单个回调出错不能拖垮轮询定时器
                    logger.exception(
                        "处理界面回调失败：%s", getattr(callback, "__name__", callback)
                    )
        except queue.Empty:
            pass
        self.log_poll_job = self.root.after(LOG_POLL_MS, self._poll_log_queue)

    def on_save_log(self) -> None:
        path = filedialog.asksaveasfilename(
            title="保存日志",
            defaultextension=".log",
            filetypes=[("日志文件", "*.log"), ("文本文件", "*.txt"), ("所有文件", "*.*")],
            initialfile="dtp_report.log",
        )
        if not path:
            return
        Path(path).write_text(self.log_text.get("1.0", "end-1c"), encoding="utf-8")
        self.log_queue.put(f"[系统] 日志已保存：{path}")

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _reload_config(self, initial: bool = False) -> bool:
        """重新读取 config.yaml，刷新配置信息、项目列表与抓取选项。"""
        try:
            config = load_config(self.config_path)
        except Exception as exc:  # 文件缺失 / yaml 解析失败
            self.log_queue.put(f"[错误] 读取配置文件失败：{exc}")
            if not initial:
                messagebox.showerror("配置错误", f"读取配置文件失败：{exc}")
            self.fetch_status_var.set("配置读取失败：请检查 config.yaml 后重新加载。")
            return False

        dtp_cfg = config.get("dtp", {}) or {}
        report_cfg = config.get("report", {}) or {}
        merge_projects = config.get("merge_projects", {}) or {}

        self.info_vars["config"].set(str(Path(self.config_path).resolve()))
        self.info_vars["url"].set(str(dtp_cfg.get("url", "-")))
        self.info_vars["output"].set(
            str(Path(str(report_cfg.get("output_dir", "./output"))).resolve())
        )
        self.info_vars["company"].set(str(report_cfg.get("locale_friendcom", DEFAULT_COMPANY_LOCALE)))
        self.info_vars["project"].set(str(report_cfg.get("locale_project", DEFAULT_PROJECT_LOCALE)))
        self.info_vars["author"].set(str(report_cfg.get("locale_developer", DEFAULT_AUTHOR_LOCALE)))
        trend_builds = int(report_cfg.get("trend_builds", DEFAULT_TREND_BUILDS))
        if trend_builds == 0:
            trend_scope = "全部构建"
        elif trend_builds > 0:
            trend_scope = f"最近 {trend_builds} 次构建"
        else:
            trend_scope = "不抓趋势数据"
        self.info_vars["options"].set(
            f"趋势数据：每个仓库{trend_scope}，"
            f"并发 {max(1, int(report_cfg.get('max_workers', 4)))} 线程"
        )

        # 项目列表直接来自 config.yaml（不用连 DTP），配置一变就刷新
        self.project_selector.set_items([str(name) for name in merge_projects])

        if not initial and self.bundle is not None:
            # 配置变了（仓库范围可能也变了），之前抓的数据作废，避免用旧数据出报告
            self.bundle = None
            self.author_selector.set_items([])
            self._reset_progress()
            self.fetch_status_var.set("配置已变更，请重新点击“连接 DTP 并获取数据”。")
            self.log_queue.put("[系统] 配置已重新加载，之前抓取的数据已作废。")
        elif not initial:
            self.log_queue.put("[系统] 配置已重新加载。")

        self._refresh_buttons()
        return True

    # ------------------------------------------------------------------
    # 进度条（步骤 1 用真实的阶段进度；生成报告时用不确定模式）
    # ------------------------------------------------------------------
    def _reset_progress(self) -> None:
        self.progress_value = 0.0
        self.progress_target = 0.0
        self.progress.configure(mode="determinate", value=0)
        self.progress_label.set("0%")

    def _start_fetch_progress(self) -> None:
        self.progress_mode = "fetching"
        self._reset_progress()
        if self.progress_job is None:
            self.progress_job = self.root.after(200, self._tick_progress)

    def _tick_progress(self) -> None:
        """主线程心跳：把 progress_value 平滑地推向 progress_target。

        抓取的两步（开发者索引、构建趋势）内部是"逐仓库"的循环，界面拿不到细粒度进度，
        用这个心跳让进度条持续走动；阶段完成时 _on_progress 会抬高目标值，
        因此进度只前进、不倒退，最后停在 100%。
        """
        self.progress_job = None
        if self.progress_mode != "fetching":
            return
        if self.progress_value < self.progress_target:
            step = max(0.2, (self.progress_target - self.progress_value) * 0.04)
            self.progress_value = min(self.progress_target, self.progress_value + step)
            self.progress.configure(value=self.progress_value)
            self.progress_label.set(f"{int(self.progress_value)}%")
        self.progress_job = self.root.after(200, self._tick_progress)

    def _on_progress(self, percent: float, message: str) -> None:
        """主线程：worker 线程上报的抓取进度 -> 抬高进度条目标值 + 更新状态文字。"""
        self.progress_target = max(self.progress_target, float(percent))
        if message:
            self.fetch_status_var.set(message)
        if self.progress_target >= 100:
            # 阶段已经到头：直接显示 100%（表示数据全部就绪）
            self.progress_value = 100.0
            self.progress.configure(value=100)
            self.progress_label.set("100%")

    def _stop_progress(self) -> None:
        if self.progress_job is not None:
            self.root.after_cancel(self.progress_job)
            self.progress_job = None
        self.progress_mode = "idle"

    def _start_generating_progress(self) -> None:
        """生成报告没有可量化的进度：进度条切成“来回滚动”，避免看起来像卡住。"""
        self._stop_progress()
        self.progress.configure(mode="indeterminate")
        self.progress.start(30)

    def _stop_generating_progress(self) -> None:
        self.progress.stop()
        self.progress.configure(mode="determinate", value=self.progress_value)
        self.progress_label.set(f"{int(self.progress_value)}%")

    def _refresh_buttons(self) -> None:
        """三个报告按钮：数据已就绪且没有后台任务在跑时才可点，否则保持灰色。"""
        busy = self._busy()
        ready = self.bundle is not None and self.bundle.has_data and not busy
        state = "normal" if ready else "disabled"
        for button in (self.company_button, self.project_button, self.author_button):
            button.configure(state=state)
        self.fetch_button.configure(state="disabled" if busy else "normal")

    def _busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _clear_log(self) -> None:
        """清空日志框，开始新一轮任务（需要留档时先用上面的“保存日志”导出）。"""
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # ------------------------------------------------------------------
    # 步骤 1：连接 DTP 并获取数据
    # ------------------------------------------------------------------
    def on_fetch_data(self) -> None:
        if self._busy():
            messagebox.showinfo("正在忙", "上一个任务还在进行中，请等它结束。")
            return
        if not self._reload_config():
            return

        self._clear_log()
        self.log_queue.put("[系统] 开始连接 DTP 并获取三类报告需要的数据…")
        self.bundle = None
        self.author_selector.set_items([])
        self.company_summary_var.set("正在获取数据…（进度见上方进度条与下方日志）")
        self._start_fetch_progress()
        self.worker = threading.Thread(target=self._run_fetch, daemon=True)
        self.worker.start()
        self._refresh_buttons()

    def _run_fetch(self) -> None:
        """后台线程：抓全部数据（进度回调只投队列，由主线程执行，worker 不碰 Tk）。"""
        bundle: DataBundle | None = None
        error: str | None = None
        try:
            bundle = fetch_all_data(
                self.config_path,
                progress=lambda percent, message: self.ui_queue.put(
                    (self._on_progress, (percent, message))
                ),
            )
        except Exception as exc:  # 网络异常、DTP 认证失败、配置错误等
            error = f"{type(exc).__name__}: {exc}"
            logging.getLogger(__name__).exception("获取数据失败：%s", error)
        self.ui_queue.put((self._on_fetch_done, (bundle, error)))

    def _on_fetch_done(self, bundle: DataBundle | None, error: str | None) -> None:
        """主线程：收到抓取结果 -> 刷新三块内容与按钮状态。"""
        self._stop_progress()
        self.worker = None

        if error is not None or bundle is None:
            # 失败时不留旧数据，三个报告按钮保持灰色
            self.bundle = None
            self.fetch_status_var.set("获取数据失败，请查看日志框里的错误信息。")
            self.company_summary_var.set("等待数据：先在上方点击“连接 DTP 并获取数据”。")
            messagebox.showerror("获取数据失败", f"{error}\n\n详情见日志框。")
            self._refresh_buttons()
            return

        self.bundle = bundle
        self.progress_value = 100.0
        self.progress_target = 100.0
        self.progress.configure(mode="determinate", value=100)
        self.progress_label.set("100%")

        repo_count = len([item for item in bundle.data if item.has_data])
        self.fetch_status_var.set(
            f"数据已就绪（100%）：{repo_count} 个有数据的仓库、"
            f"{len(bundle.index.authors)} 位开发者；下面三块可以出报告了。"
        )
        self.data_summary_var.set(
            f"数据快照：{len(bundle.merge_projects)} 个项目组、{repo_count} 个有数据的仓库、"
            f"{len(bundle.index.authors)} 位开发者；输出目录 {bundle.output_dir.resolve()}"
        )
        self.company_summary_var.set(
            f"数据已就绪：{len(bundle.data)} 条仓库记录；公司报告会合并 "
            f"{len(bundle.merge_projects)} 个项目组。"
        )
        self.project_selector.set_items([str(name) for name in bundle.merge_projects])
        self.author_selector.set_items(list(bundle.index.authors))
        self.project_hint_var.set(f"共 {len(bundle.merge_projects)} 个可选项目。")
        self.author_hint_var.set(f"共 {len(bundle.index.authors)} 位可选开发者。")
        self.log_queue.put("[系统] 数据获取完成，可以分别生成公司 / 项目 / 开发者报告了。")

        if not bundle.has_data:
            messagebox.showwarning(
                "没有数据",
                "DTP 上没有抓到任何静态分析数据（请检查 config.yaml 的仓库名与 DTP 数据），"
                "三个报告按钮会保持灰色。",
            )
        self._refresh_buttons()

    # ------------------------------------------------------------------
    # 三块（公司 / 项目 / 开发者）的报告生成
    # ------------------------------------------------------------------
    def _require_data(self) -> bool:
        """生成前的统一检查（按钮本来就只在数据就绪时可点，这里是兜底提示）。"""
        if self._busy():
            messagebox.showinfo("正在忙", "上一个任务还在进行中，请等它结束。")
            return False
        if self.bundle is None or not self.bundle.has_data:
            messagebox.showwarning("数据未就绪", "请先点击“连接 DTP 并获取数据”。")
            return False
        return True

    def on_generate_company(self) -> None:
        """“公司报告”按钮：合并后出一份公司级报告。"""
        if not self._require_data():
            return
        self._start_generation("company", [])

    def on_generate_projects(self) -> None:
        """“项目报告”按钮：为每个已选项目出一份报告；未选择时确认后按全部项目出。"""
        if not self._require_data():
            return
        selected = self.project_selector.selected_items()
        if not selected and self.bundle is not None:
            total = len(self.bundle.merge_projects)
            if not messagebox.askyesno(
                "未选择项目",
                f"当前没有选择任何项目。\n\n是否为 config.yaml 里的全部 {total} 个项目"
                f"各生成一份报告？",
            ):
                return
        self._start_generation("project", selected)

    def on_generate_authors(self) -> None:
        """“开发者报告”按钮：为每位已选开发者出一份报告；未选择时确认后按全部作者出。"""
        if not self._require_data():
            return
        selected = self.author_selector.selected_items()
        if not selected and self.bundle is not None:
            total = len(self.bundle.index.authors)
            if not messagebox.askyesno(
                "未选择开发者",
                f"当前没有选择任何开发者。\n\n是否为 DTP 上的全部 {total} 位开发者"
                f"各生成一份报告？（会比较慢）",
            ):
                return
        self._start_generation("author", selected)

    def on_add_manual_author(self) -> None:
        """“手工添加”：把 DTP 作者列表里还没有的人加入已选（走 author= 兜底查询）。"""
        name = self.manual_author_var.get().strip()
        if not name:
            messagebox.showinfo("没有输入", "请先填写开发者名（例如 hendra）。")
            return
        self.author_selector.add_item(name)
        self.manual_author_var.set("")
        self.log_queue.put(f"[选择] 已手工添加开发者：{name}")

    def _start_generation(self, kind: str, items: list[str]) -> None:
        """启动后台线程生成报告（生成期间按钮变灰、进度条切到滚动模式）。"""
        title = REPORT_KINDS[kind]
        self._clear_log()
        self.log_queue.put(f"[系统] 开始生成{title}（复用已抓取的数据快照）…")
        self._start_generating_progress()
        self.worker = threading.Thread(
            target=self._run_generation, args=(kind, items), daemon=True
        )
        self.worker.start()
        self._refresh_buttons()

    def _run_generation(self, kind: str, items: list[str]) -> None:
        """后台线程：调用本文件里的三个生成函数（都复用已抓取的数据快照）。"""
        reports: list[Path] = []
        error: str | None = None
        try:
            if kind == "company":
                reports = generate_company_report(self.bundle)
            elif kind == "project":
                reports = generate_project_reports_for(self.bundle, items or None)
            else:
                reports = generate_author_reports_for(self.bundle, items or None)
        except Exception as exc:  # 网络异常（开发者报告还要补抓严重级别）、写文件失败等
            error = f"{type(exc).__name__}: {exc}"
            logging.getLogger(__name__).exception("生成报告失败：%s", error)
        self.ui_queue.put((self._on_generation_done, (kind, reports, error)))

    def _on_generation_done(self, kind: str, reports: list[Path], error: str | None) -> None:
        """主线程：恢复界面并汇总本次结果。"""
        self._stop_generating_progress()
        self.worker = None
        title = REPORT_KINDS[kind]

        if error is not None:
            self.fetch_status_var.set(f"{title}生成失败，请查看日志框中的错误信息。")
            messagebox.showerror("生成失败", f"生成{title}失败：{error}\n\n详情见日志框。")
            self._refresh_buttons()
            return

        if not reports:
            self.fetch_status_var.set(f"{title}：没有生成任何报告（所选内容没有静态分析数据）。")
            messagebox.showwarning(
                "没有报告", f"所选内容在 DTP 上没有可用的静态分析数据，未生成{title}。"
            )
            self._refresh_buttons()
            return

        out_dir = self.bundle.output_dir.resolve() if self.bundle else Path("./output").resolve()
        self.fetch_status_var.set(f"完成：已生成 {len(reports)} 份{title} -> {out_dir}")
        logging.getLogger(__name__).info("完成：%d 份%s已输出到 %s", len(reports), title, out_dir)
        messagebox.showinfo(
            "生成完成",
            f"已生成 {len(reports)} 份{title}（.html 与 .xlsx）。\n\n输出目录：\n{out_dir}",
        )
        self._refresh_buttons()

    # ------------------------------------------------------------------
    # 其它
    # ------------------------------------------------------------------
    def on_open_output(self) -> None:
        out_dir = self.bundle.output_dir if self.bundle is not None else Path("./output")
        if not out_dir.exists():
            messagebox.showinfo(
                "目录不存在", f"输出目录还不存在：\n{out_dir.resolve()}\n\n请先生成报告。"
            )
            return
        open_in_file_manager(out_dir)

    def _on_close(self) -> None:
        if self._busy():
            if not messagebox.askyesno("正在忙", "还有任务在进行中，确定要退出吗？"):
                return
        self._stop_progress()
        if self.log_poll_job is not None:
            self.root.after_cancel(self.log_poll_job)
            self.log_poll_job = None
        sys.stdout = self._original_stdout
        sys.stderr = self._original_stderr
        self.root.destroy()


def launch_gui(config_path: str = "config.yaml") -> None:
    """创建并运行统一报告窗口（run_entry 调用；也是 GUI 依赖的检查点）。"""
    if not GUI_AVAILABLE:
        raise RuntimeError(
            "当前 Python 环境没有 tkinter，无法打开图形界面；请改用命令行模式，例如："
            "python main.py --all"
        )
    root = tk.Tk()
    UnifiedReportApp(root, config_path=config_path)
    root.mainloop()


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
        # 只有走到这里才会用 tkinter（launch_gui 内部会检查依赖是否可用）
        launch_gui(config_path=_cli_config_path(args))
        return
    main(args)


if __name__ == "__main__":
    run_entry()



