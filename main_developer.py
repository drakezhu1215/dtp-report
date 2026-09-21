#!/usr/bin/env python3
"""DTP 开发者（author）代码核查报告生成器（templates/report_v4_zh.html）。

**一个开发者一份报告**：从 DTP 抓取 config.yaml 中 29 个项目 / 60 个仓库的作者维度
数据，为选中的每个开发者输出一份 .html + .xlsx 报告。

数据来源（全部是 DTP REST API v1.12，详见 author_analyzer 模块说明）：

- 作者列表：``GET /staticAnalysisViolations/count?buildId=..&filterId=..
  &groupBy=author&statuses=failed,suppressed``（每个仓库每个 build 一次请求）；
- 作者违规/抑制：``...&author=<作者>&statuses=failed,suppressed``
  （DTP 不传 statuses 时只算 failed，返回体里连 suppressed 字段都没有）；
- 作者严重级别：``...&author=<作者>&groupBy=severity``。

仓库范围：``--projects``（DTP 仓库名）> ``--project-keys``（merge_projects 的项目名，
取这些项目下的仓库）> ``report.project_names`` > ``merge_projects`` 的全部源项目。

报告内容（report_v4_zh.html）：

1. 摘要：该开发者**相关仓库**的整仓"文件总数/代码总数" + 该开发者的"违规总数"，
   缺陷密度 = 违规总数 × 1000 / 代码总数（与项目报告同一公式与取整规则）；
2. 开发者概览：每个相关仓库**最后一次构建**的文件数、代码行数，以及该开发者在这次
   构建上的违规/抑制/合计/缺陷密度；
3. 严重级别明细表：每个相关仓库最后一次构建上该开发者的 Sev1~Sev5 分布；
4. 开发者违规图：各相关仓库最后一次构建上该开发者的"违规与抑制"对比条形图；
5. 开发者趋势图：每个相关仓库一张曲线图，X 轴是构建次序（build-1、build-2 ...），
   Y 轴是该开发者在这次构建上的违规数量（取最近 report.trend_builds 次构建）。

性能：抓取分两步，目的是"抓一次、给所有开发者复用"——

1. ``collect_violations``：每个仓库 3 次请求（最新 build、文件数、代码行数）；
2. ``collect_author_index``：每个仓库 1 次（构建列表）+ 每次构建 1 次（作者分布），
   把"每个仓库最近 N 次构建的作者分布"缓存成 :class:`author_analyzer.AuthorIndex`。

之后每份报告只需要再补抓该开发者的严重级别（每个相关仓库 1 次请求）。
界面（author_gui.py）会把这份索引缓存下来，所以"获取开发者列表"之后的每次生成都很快。

运行方式：

    python main_developer.py --list-authors                    # 只列出 DTP 上全部开发者
    python main_developer.py --authors "hendra,FC_MARMO\\marmo"  # 为指定开发者出报告
    python main_developer.py                                   # 无参数 -> 打开图形界面
    python main_developer.py --gui                             # 显式打开图形界面
    python main_developer.py --trend-builds 0 --authors "..."   # 不出趋势图（省时间）
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

from analyzer import ProjectViolations, RepoBuildTrend, collect_violations
from author_analyzer import (
    AuthorIndex,
    author_report_rows,
    author_report_trends,
    collect_author_index,
    collect_author_repo_rows,
    collect_author_repo_trends,
    collect_author_severities,
)
from chart_builder import chart_repo_trends, chart_total_violations_bad
from dtp_client import DTPClient
from merge_project import list_source_projects
from report_excel import generate_excel_report
from report_html import generate_html_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# 本入口（开发者报告）默认使用的报告模板：对应 report_html.TEMPLATE_MAP 的 "v4_zh"
# -> templates/report_v4_zh.html；config.yaml 的 report.locale_developer 可覆盖
DEFAULT_LOCALE = "v4_zh"

# 每个仓库默认取最近多少次构建（趋势数据）；config.yaml 的 report.trend_builds 可覆盖
DEFAULT_TREND_BUILDS = 10

# 并发抓取线程数；config.yaml 的 report.max_workers 可覆盖
DEFAULT_MAX_WORKERS = 4

# Windows 文件名非法字符（含控制字符）；作者名里可能出现 '\'（域账号），必须替换
_INVALID_FILE_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


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


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DTP Static Analysis Violations Report Generator (one report per developer/author)",
    )
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to config.yaml (default: config.yaml)")
    parser.add_argument("--url", help="DTP server URL (overrides config)")
    parser.add_argument("--user", help="DTP username (overrides config)")
    parser.add_argument("--password", help="DTP password (overrides config)")
    parser.add_argument(
        "--authors",
        help="Comma-separated developer names to report (default: ALL authors found on DTP)",
    )
    parser.add_argument(
        "--list-authors",
        action="store_true",
        help="Only list all authors found on DTP (with repo and violation totals), then exit",
    )
    parser.add_argument(
        "--projects",
        help="Comma-separated DTP repo names to scan (default: source projects of merge_projects)",
    )
    parser.add_argument(
        "--project-keys",
        help="Comma-separated project names (keys of config merge_projects): only scan the repos of these projects",
    )
    parser.add_argument("--gui", action="store_true", help="Open the graphical user interface (default when no argument is given)")
    parser.add_argument("--output", help="Output directory (overrides config)")
    parser.add_argument(
        "--locale",
        choices=["zh", "en", "v2_zh", "v2_en", "v3_zh", "v4_zh"],
        help="Report template: v4_zh is the developer report (overrides config; default: report.locale_developer = v4_zh)",
    )
    parser.add_argument(
        "--trend-builds",
        type=int,
        help="Number of latest builds per repo used by the trend charts (0 disables trend charts; overrides config report.trend_builds)",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        help="Concurrent request threads for collecting author data (overrides config report.max_workers)",
    )
    return parser


def split_names(value: str | None) -> list[str]:
    """把命令行里的 "a,b,c" 拆成名称列表（去空白、丢弃空项、保持顺序去重）。"""
    if not value:
        return []

    names: list[str] = []
    for name in value.split(","):
        clean = name.strip()
        if clean and clean not in names:
            names.append(clean)
    return names


def resolve_repo_names(
    report_cfg: dict,
    merge_projects: dict,
    explicit: list[str] | None = None,
    project_keys: list[str] | None = None,
) -> list[str]:
    """扫描范围（DTP 仓库名）的优先级：

    ``explicit``（``--projects``）> ``project_keys``（``--project-keys``，取这些项目下的
    仓库）> ``report.project_names`` > ``merge_projects`` 的全部源项目。

    解析结果为空时 collect_violations 会回退为扫描 DTP 全部项目（很慢，易超时）。
    """
    if explicit:
        return list(explicit)

    if project_keys:
        names: list[str] = []
        unknown = [name for name in project_keys if name not in (merge_projects or {})]
        if unknown:
            logger.warning(
                "Project name(s) not found in config merge_projects: %s", ", ".join(unknown)
            )
        for name in project_keys:
            for repo in (merge_projects or {}).get(name) or []:
                clean = str(repo).strip()
                if clean and clean not in names:
                    names.append(clean)
        if names:
            return names
        logger.warning("No repo resolved from --project-keys, falling back to config/default scope.")

    if report_cfg.get("project_names"):
        return [str(p).strip() for p in report_cfg["project_names"] if str(p).strip()]

    return list_source_projects(merge_projects)


def build_author_index(
    config_path: str = "config.yaml",
    source_projects: list[str] | None = None,
    project_keys: list[str] | None = None,
    url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    trend_builds: int | None = None,
    max_workers: int | None = None,
) -> AuthorIndex:
    """抓取"开发者索引"：DTP 上全部作者 + 每个仓库最近若干次构建的作者分布。

    这是**最慢的一步**（仓库数 × 构建数的请求量），界面上的"获取开发者列表"和
    命令行 ``--list-authors`` 都走它；生成报告时把返回的索引复用即可，
    不必按开发者重复抓取。

    返回的 :class:`AuthorIndex` 里 ``repos`` 就是仓库基础信息（含最新 build、
    文件数、代码行数），因此界面拿到索引后可以直接开始生成报告。
    """
    settings = resolve_settings(
        config_path,
        url=url, username=username, password=password,
        trend_builds=trend_builds, max_workers=max_workers,
    )
    repo_names = resolve_repo_names(
        settings["report_cfg"], settings["merge_projects"],
        explicit=source_projects, project_keys=project_keys,
    )

    logger.info(
        "Scanning %d repo(s) for authors (trend_builds=%d, workers=%d)...",
        len(repo_names), settings["trend_builds"], settings["max_workers"],
    )
    if not repo_names:
        logger.warning("No repo name resolved, will scan ALL projects on DTP (slow, prone to timeouts).")

    logger.info("Connecting to DTP at %s", settings["url"])
    client = DTPClient(
        url=settings["url"], username=settings["username"], password=settings["password"],
    )

    # with_file_count / with_code_lines：整仓的文件数与代码行数（DTP 没有作者维度），
    # 供开发者报告的"文件总数/代码总数"与缺陷密度分母使用
    repos = collect_violations(
        client,
        project_names=repo_names,
        with_file_count=True,
        with_code_lines=True,
    )
    logger.info("Collected repo data for %d repo(s)", len(repos))

    warn_missing_repos(repos, repo_names)

    return collect_author_index(
        client,
        repos,
        trend_builds=settings["trend_builds"],
        max_workers=settings["max_workers"],
    )


def warn_missing_repos(data: list[ProjectViolations], repo_names: list[str] | None) -> None:
    """配置里的仓库名必须与 DTP 项目名完全一致（DTP 按名称精确匹配）。"""
    if not repo_names:
        return

    returned = {d.project_name for d in data}
    missing = [name for name in repo_names if name not in returned]
    if missing:
        preview = ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")
        logger.warning("%d specified repo(s) not found on DTP: %s", len(missing), preview)


def build_author_charts(
    author: str,
    rows: list[ProjectViolations],
    trends_by_repo: dict[str, RepoBuildTrend],
) -> dict:
    """生成单个开发者报告所需的图表（第 4、5 章）。

    - ``total_bad``：该开发者各相关仓库"最后一次构建"上的违规与抑制对比条形图——
      显示**全部**相关仓库，不截断；
    - ``repo_trends``：每个相关仓库一张该作者的违规趋势曲线图（仓库没有历史构建
      数据时不出图）。
    """
    # 趋势图顺序与报告表格（仓库顺序）一致
    trends = [
        trends_by_repo[row.project_name]
        for row in rows
        if row.project_name in trends_by_repo
    ]

    repo_count_with_data = len([row for row in rows if row.has_data])

    return {
        "total_bad": chart_total_violations_bad(
            rows,
            # 显示全部相关仓库：top_n_bad <= 0 时 chart_total_violations_bad 不做截断
            top_n_bad=0,
            title=f"{author} 各仓库违规与抑制对比（共 {repo_count_with_data} 个仓库）",
            x_label="仓库 (Repository)",
        ),
        "repo_trends": chart_repo_trends(trends, owner_name=author),
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
    """把开发者名转换为 Windows/Linux 均可用的文件名片段。

    作者名里可能有 '\\'（域账号，如 ``FC_MARMO\\marmo``）、'/' 或空格，
    直接当文件名在 Windows 上会失败，这里统一替换为 '_'，并去掉首尾空白和结尾的点。
    """
    cleaned = _INVALID_FILE_NAME_CHARS.sub("_", str(name)).strip().strip(" .")
    return cleaned or "author"


def build_report_base_name(author: str, timestamp: str) -> str:
    """报告文件名（不含扩展名）：``<开发者名>_developer_report_<YYYYmmdd_HHMMSS>``。"""
    return f"{safe_file_name(author)}_developer_report_{timestamp}"


def resolve_settings(
    config_path: str,
    url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    output_dir: str | None = None,
    locale: str | None = None,
    trend_builds: int | None = None,
    max_workers: int | None = None,
) -> dict:
    """把命令行 / 界面的显式参数与 config.yaml 合并成一份运行设置（显式参数优先）。"""
    cfg = load_config(config_path)
    dtp_cfg = cfg.get("dtp", {}) or {}
    report_cfg = cfg.get("report", {}) or {}

    if trend_builds is None:
        trend_builds = int(report_cfg.get("trend_builds", DEFAULT_TREND_BUILDS))
    if max_workers is None:
        max_workers = int(report_cfg.get("max_workers", DEFAULT_MAX_WORKERS))

    return {
        "config": cfg,
        "merge_projects": cfg.get("merge_projects", {}) or {},
        "report_cfg": report_cfg,
        "url": url or dtp_cfg.get("url", "http://localhost:9191"),
        "username": username or dtp_cfg.get("username", "admin"),
        "password": password or dtp_cfg.get("password", "admin"),
        "output_dir": output_dir or report_cfg.get("output_dir", "./output"),
        # 开发者报告默认使用 report_v4_zh.html（config.yaml 的 report.locale_developer）
        "locale": locale or report_cfg.get("locale_developer", DEFAULT_LOCALE),
        "trend_builds": int(trend_builds),
        "max_workers": max(1, int(max_workers)),
    }


def generate_author_reports(
    client: DTPClient,
    index: AuthorIndex,
    authors: list[str],
    output_dir: Path,
    timestamp: str,
    settings: dict,
) -> list[Path]:
    """为每个开发者生成一份 HTML + Excel 报告，返回已生成的 HTML 报告路径列表。

    每个开发者的报告都只包含"与他相关的仓库"：

    - 仓库范围：该作者在索引里出现过的仓库；手工添加的开发者则用 ``author=``
      过滤逐仓库查询；
    - 违规/抑制：该作者在这些仓库**最后一次构建**上的数字；
    - 严重级别：逐个相关仓库补抓（``groupBy=severity&author=..``）；
    - 趋势：每个相关仓库一条曲线，Y 轴是该作者在该次构建上的违规数。
    """
    generated: list[Path] = []
    total = len(authors)

    for position, author in enumerate(authors, 1):
        rows = author_report_rows(index, author)
        trends = author_report_trends(index, author, rows)

        if not rows:
            # 手工添加的开发者不在缓存的索引里：按 author= 逐仓库查询确认相关性
            logger.info(
                "[%d/%d] %s is not in the cached author index, querying %d repo(s) with the author filter...",
                position, total, author, len(index.repos),
            )
            rows = collect_author_repo_rows(
                client, author, index.repos, max_workers=settings["max_workers"],
            )
            trends = collect_author_repo_trends(
                client, author, rows,
                max_builds=settings["trend_builds"], max_workers=settings["max_workers"],
            )

        if not rows:
            logger.warning(
                "[%d/%d] %s: no related repo with static analysis data, report skipped.",
                position, total, author,
            )
            continue

        # 严重级别只能逐个 (作者, 仓库) 查询，补抓到每一行的 severity_breakdown
        collect_author_severities(
            client, author, rows, max_workers=settings["max_workers"],
        )

        logger.info(
            "[%d/%d] Generating report for developer %s (%d repo(s), violations=%d, "
            "files=%d, code lines=%d)...",
            position, total, author, len(rows),
            sum(row.failed for row in rows),
            sum(row.file_count for row in rows),
            sum(row.code_lines for row in rows),
        )

        base_name = build_report_base_name(author, timestamp)
        charts = build_author_charts(author, rows, trends)

        html_path = generate_html_report(
            rows,
            charts,
            output_dir / f"{base_name}.html",
            dtp_url=settings["url"],
            locale=settings["locale"],
            developer_name=author,  # 标题显示开发者名，而不是项目名
        )
        logger.info("  HTML report: %s", html_path)

        xlsx_path = generate_excel_report(rows, output_dir / f"{base_name}.xlsx")
        logger.info("  Excel report: %s", xlsx_path)

        generated.append(html_path)

    return generated



def generate_reports(
    config_path: str = "config.yaml",
    selected_authors: list[str] | None = None,
    source_projects: list[str] | None = None,
    project_keys: list[str] | None = None,
    url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    output_dir: str | None = None,
    locale: str | None = None,
    trend_builds: int | None = None,
    max_workers: int | None = None,
    author_index: AuthorIndex | None = None,
) -> list[Path]:
    """执行一次完整的开发者报告生成流程（命令行与图形界面共用）。

    :param selected_authors: 要出报告的开发者名列表；``None``/空表示"全部开发者"
        （即从 config.yaml 的仓库里抓到的所有作者）；
    :param source_projects/project_keys: 扫描范围，见 :func:`resolve_repo_names`；
    :param url/username/password/output_dir/locale/trend_builds/max_workers:
        覆盖 config.yaml 的设置，为 ``None`` 时使用配置文件里的值；
    :param author_index: 已经抓好的开发者索引（界面上"获取开发者列表"之后传入，
        避免重复抓取；为 ``None`` 时本函数自己抓一遍）。索引里没有趋势数据而本次
        需要趋势图时只记一条 warning（此时报告第 5 章为空）。

    返回本次生成的 HTML 报告路径列表；没有任何开发者有数据时返回空列表
    （不调用 ``sys.exit``，方便图形界面复用）。
    """
    settings = resolve_settings(
        config_path,
        url=url, username=username, password=password, output_dir=output_dir,
        locale=locale, trend_builds=trend_builds, max_workers=max_workers,
    )
    repo_names = resolve_repo_names(
        settings["report_cfg"], settings["merge_projects"],
        explicit=source_projects, project_keys=project_keys,
    )

    logger.info("Connecting to DTP at %s", settings["url"])
    client = DTPClient(
        url=settings["url"], username=settings["username"], password=settings["password"],
    )

    index = author_index if (author_index is not None and author_index.repos) else None
    if index is None:
        if not repo_names:
            logger.warning(
                "No repo name resolved, will scan ALL projects on DTP (slow, prone to timeouts)."
            )
        # with_file_count / with_code_lines：整仓的文件数与代码行数，供摘要与缺陷密度使用
        repos = collect_violations(
            client,
            project_names=repo_names,
            with_file_count=True,
            with_code_lines=True,
        )
        logger.info("Collected repo data for %d repo(s)", len(repos))
        warn_missing_repos(repos, repo_names)

        index = collect_author_index(
            client,
            repos,
            trend_builds=settings["trend_builds"],
            max_workers=settings["max_workers"],
        )
    else:
        logger.info(
            "Reusing the cached author index: %d author(s), %d repo(s), trend_builds=%d",
            len(index.authors), len(index.repos), index.trend_builds,
        )
        if settings["trend_builds"] > 0 and index.trend_builds <= 0:
            logger.warning(
                "Cached author index was collected without trend data (trend_builds=%d): "
                "chapter 5 of the reports will be empty.",
                index.trend_builds,
            )

    authors = selected_authors or index.authors
    if not authors:
        logger.warning("No developer to report (no author found on DTP).")
        return []

    preview = ", ".join(authors[:5]) + (" ..." if len(authors) > 5 else "")
    logger.info("Generating %d developer report(s): %s", len(authors), preview)

    out = prepare_output_dir(settings["output_dir"])
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    generated = generate_author_reports(client, index, authors, out, timestamp, settings)

    if not generated:
        logger.warning("No report generated (no developer with static analysis data).")
        return []

    logger.info("Done! %d report(s) generated in %s", len(generated), out.resolve())
    return generated



def format_author_table(index: AuthorIndex) -> list[str]:
    """``--list-authors`` 的输出：开发者名 + 相关仓库数 + 违规总数。"""
    lines = [f"DTP 上共找到 {len(index.authors)} 位开发者（相关仓库数 / 违规总数）："]
    for position, author in enumerate(index.authors, 1):
        rows = index.repos_of(author)
        lines.append(
            f"  {position:>4}. {author}  ({len(rows)} 个仓库, 违规 {sum(r.failed for r in rows):,})"
        )
    return lines


def main() -> None:
    """命令行入口。"""
    parser = build_argparser()
    args = parser.parse_args()

    try:
        if args.list_authors:
            # 只列开发者：抓一遍索引（最慢的一步）后打印，不出报告
            index = build_author_index(
                config_path=args.config,
                source_projects=split_names(args.projects),
                project_keys=split_names(args.project_keys),
                url=args.url,
                username=args.user,
                password=args.password,
                trend_builds=args.trend_builds,
                max_workers=args.max_workers,
            )
            for line in format_author_table(index):
                print(line)
            return

        authors = split_names(args.authors)
        if not authors:
            logger.warning(
                "No --authors given: reports will be generated for ALL authors found on DTP."
            )

        generate_reports(
            config_path=args.config,
            selected_authors=authors or None,
            source_projects=split_names(args.projects),
            project_keys=split_names(args.project_keys),
            url=args.url,
            username=args.user,
            password=args.password,
            output_dir=args.output,
            locale=args.locale,
            trend_builds=args.trend_builds,
            max_workers=args.max_workers,
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
        from author_gui import launch_author_gui

        launch_author_gui(
            config_path=_cli_config_path(sys.argv[1:]),
            locale=DEFAULT_LOCALE,  # 开发者报告：图形界面也使用 report_v4_zh.html
        )
    else:
        main()

