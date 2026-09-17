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

- 摘要按该项目仓库汇总，仓库概览和严重级别明细表逐仓库列出；
- 第 4 章"项目违规图"：该项目下全部有数据的仓库的违规数量对比条形图（不截断）；
- 第 5 章"项目趋势图"：各仓库违规趋势曲线图——有几个仓库就画几张，X 轴是构建次序
      （build-1、build-2 ...），Y 轴是那次构建的违规数量；
      趋势数据由 analyzer.collect_build_trends 从 DTP 逐仓库、逐 build 采集，
      每个仓库取最近 report.trend_builds 次构建（默认 10 次）。

运行方式：

    python main_project.py                 # 无参数 -> 打开图形界面（report_gui.py）
    python main_project.py --gui           # 显式打开图形界面
    python main_project.py --project-keys 海外三相电表平台,印尼集中器平台   # 命令行指定项目
    python main_project.py                 # （带任意其它参数时为命令行模式）

界面里用户从下拉列表中多选 config.yaml 的项目（不含仓库），点击"报告"按钮后
只抓取这些项目下的仓库数据，为每个项目生成一份 .html + .xlsx 报告，
所有日志输出到界面的日志框中。
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

from analyzer import (
    ProjectViolations,
    RepoBuildTrend,
    collect_build_trends,
    collect_violations,
)
from chart_builder import chart_repo_trends, chart_total_violations_bad
from dtp_client import DTPClient
from report_excel import generate_excel_report
from report_html import generate_html_report

#define my python file 'merge_project.py'
from merge_project import (
    group_project_violations,
    list_source_projects,
    merge_project_violations,
    unique_repo_violations,
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
    """读取 config.yaml。

    配置文件不存在时抛 :class:`FileNotFoundError`（不再直接 ``sys.exit``），
    这样图形界面可以捕获并弹窗提示；命令行入口 ``main()`` 负责把它转成退出码 1。
    """
    p = Path(config_path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def list_project_names(merge_projects: dict) -> list[str]:
    """列出 config.yaml 中 ``merge_projects`` 的全部项目名（不含仓库）。

    返回的是配置里项目的 key（如 "海外三相电表平台"、"印尼集中器平台"），
    也就是图形界面项目下拉列表的候选项，顺序与配置文件一致。
    """
    return [str(name) for name in (merge_projects or {})]


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DTP Static Analysis Violations Report Generator (one report per project)",
    )
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to config.yaml (default: config.yaml)")
    parser.add_argument("--url", help="DTP server URL (overrides config)")
    parser.add_argument("--user", help="DTP username (overrides config)")
    parser.add_argument("--password", help="DTP password (overrides config)")
    parser.add_argument("--projects", help="Comma-separated DTP project/repo names to analyze (default: source projects of merge_projects)")
    parser.add_argument(
        "--project-keys",
        help="Comma-separated project names to report (keys of config merge_projects, same meaning as the GUI selection)",
    )
    parser.add_argument("--gui", action="store_true", help="Open the graphical user interface (default when no argument is given)")
    parser.add_argument("--output", help="Output directory (overrides config)")
    parser.add_argument("--locale", choices=["zh", "en", "v2_zh", "v2_en", "v3_zh"], help="Report language/template: zh, en, v2_zh, v2_en, v3_zh (overrides config)")
    parser.add_argument(
        "--trend-builds",
        type=int,
        help="Number of latest builds per repo used by the trend charts (0 disables trend charts; overrides config report.trend_builds)",
    )
    return parser


def split_names(value: str | None) -> list[str]:
    """把命令行里的 "a,b,c" 拆成名称列表（去空白、丢弃空项）。"""
    if not value:
        return []
    return [name.strip() for name in value.split(",") if name.strip()]


def resolve_project_names(
    report_cfg: dict,
    merge_projects: dict,
    explicit: list[str] | None = None,
) -> list[str]:
    """抓取范围优先级：explicit（--projects）> report.project_names > merge_projects 的源项目。

    解析结果为空时 collect_violations 会回退为扫描 DTP 全部项目（很慢，易超时）。
    """
    if explicit:
        return list(explicit)

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


def generate_reports(
    config_path: str = "config.yaml",
    selected_projects: list[str] | None = None,
    source_projects: list[str] | None = None,
    url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    output_dir: str | None = None,
    locale: str | None = None,
    trend_builds: int | None = None,
) -> list[Path]:
    """执行一次完整的报告生成流程（命令行与图形界面共用）。

    :param config_path: config.yaml 路径；
    :param selected_projects: 用户选中的项目名（``merge_projects`` 的 key，如
        "海外三相电表平台"）。只抓取这些项目下的仓库，并且只为这些项目出报告；
        为 ``None``/空时按配置 ``report.project_names``、否则按 ``merge_projects``
        的全部源项目抓取（与升级前的命令行行为一致）；
    :param source_projects: 直接指定 DTP 仓库名（命令行 ``--projects``），优先级最高；
    :param url/username/password/output_dir/locale/trend_builds: 覆盖 config.yaml 的设置，
        为 ``None`` 时使用配置文件里的值。

    返回本次生成的 HTML 报告路径列表；没有任何项目有数据时返回空列表
    （不再调用 ``sys.exit``，方便图形界面复用）。
    """
    cfg = load_config(config_path)
    dtp_cfg = cfg.get("dtp", {}) or {}
    report_cfg = cfg.get("report", {}) or {}
    merge_projects = cfg.get("merge_projects", {}) or {}

    url = url or dtp_cfg.get("url", "http://localhost:9191")
    username = username or dtp_cfg.get("username", "admin")
    password = password or dtp_cfg.get("password", "admin")
    output_dir = output_dir or report_cfg.get("output_dir", "./output")
    locale = locale or report_cfg.get("locale_project", "v3_zh")
    # 趋势图每个仓库取最近多少次构建（0 或负数 = 不生成趋势图）
    if trend_builds is None:
        trend_builds = int(report_cfg.get("trend_builds", 10))

    if source_projects:
        # 命令行 --projects：直接给 DTP 仓库名，报告范围仍是 merge_projects 的全部项目
        project_names: list[str] = list(source_projects)
    elif selected_projects:
        # 界面/命令行选择项目：报告范围与抓取范围都收窄到选中的项目
        selected_set = set(selected_projects)
        unknown = [name for name in selected_projects if name not in merge_projects]
        if unknown:
            logger.warning(
                "Selected project(s) not found in config merge_projects: %s",
                ", ".join(unknown),
            )
        merge_projects = {
            name: repos for name, repos in merge_projects.items() if name in selected_set
        }
        if not merge_projects:
            logger.error("None of the selected projects exists in config merge_projects, nothing to do.")
            return []
        project_names = list_source_projects(merge_projects)
    else:
        project_names = resolve_project_names(report_cfg, merge_projects)

    logger.info(
        "Report scope: %d selected project(s), %d repo(s) on DTP",
        len(merge_projects), len(project_names),
    )

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
    
    # 合并视图只用于“整体是否完全没有数据”的判断（不发起额外请求）；
    # 报告明细和趋势图都按项目下的仓库拆分
    merged_violations = merge_project_violations(data, merge_projects)
    if not any(d.has_data for d in merged_violations):
        logger.warning("No projects with static analysis data found.")
        return []

    # 项目 -> 仓库列表：每个项目一份报告，报告内只含该项目下仓库的数据
    groups = group_project_violations(data, merge_projects)
    if not groups:
        logger.warning("No project matched config merge_projects, nothing to report.")
        return []

    # 趋势数据：每个仓库取最近 trend_builds 次构建的违规数量（逐 build 一次请求，
    # 单次 1~3 秒，所以这里用多线程并发；同一仓库只抓一次，即使它属于多个项目）
    if trend_builds > 0:
        trend_repos = unique_repo_violations(groups)
        logger.info(
            "Collecting trend data for %d repo(s) (latest %d build(s) each)...",
            len(trend_repos), trend_builds,
        )
        trend_started = datetime.now()
        trends_by_repo = collect_build_trends(
            client, trend_repos, max_builds=trend_builds,
        )
        logger.info(
            "Trend data ready: %d repo(s), %d repo(s) with trend points, %.1fs elapsed",
            len(trends_by_repo),
            len([t for t in trends_by_repo.values() if t.has_data]),
            (datetime.now() - trend_started).total_seconds(),
        )
    else:
        logger.warning("Trend charts disabled (trend_builds=%d), reports will contain no trend chart.", trend_builds)
        trends_by_repo = {}

    out = prepare_output_dir(output_dir)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    logger.info("Generating %d project report(s) (locale=%s)...", len(groups), locale)
    generated = generate_project_reports(
        groups, trends_by_repo, out, timestamp, url, locale,
    )

    if not generated:
        logger.warning("No report generated (no project with static analysis data).")
        return []

    logger.info("Done! %d report(s) generated in %s", len(generated), out.resolve())
    return generated


def main() -> None:
    """命令行入口（保持升级前的参数与行为）。"""
    parser = build_argparser()
    args = parser.parse_args()

    try:
        generate_reports(
            config_path=args.config,
            selected_projects=split_names(args.project_keys),
            source_projects=split_names(args.projects),
            url=args.url,
            username=args.user,
            password=args.password,
            output_dir=args.output,
            locale=args.locale,
            trend_builds=args.trend_builds,
        )
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        sys.exit(1)


def _gui_requested(argv: list[str]) -> bool:
    """无参数、或命令里带 --gui / -g 时启动图形界面。"""
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


if __name__ == "__main__":
    if _gui_requested(sys.argv[1:]):
        # 延迟导入：命令行模式下不需要加载 tkinter
        from report_gui import launch_gui

        launch_gui(config_path=_cli_config_path(sys.argv[1:]))
    else:
        main()
       

if __name__ == "__main__":
    main()