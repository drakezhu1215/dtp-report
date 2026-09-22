#!/usr/bin/env python3
"""DTP 开发者（author）代码核查报告生成器（templates/report_v4_zh.html）。

本模块是**报告引擎**（配置解析 + DTP 抓取 + 渲染编排），不含任何图形界面代码，
由统一主入口 main.py 调用：``resolve_settings`` / ``resolve_repo_names`` 解析范围，
``generate_author_reports`` 渲染报告，``format_author_table`` 打印作者清单。

**一个开发者一份报告**：从 DTP 抓取 config.yaml 中 29 个项目 / 60 个仓库的作者维度
数据，为选中的每个开发者输出一份 .html + .xlsx 报告。

数据来源（全部是 DTP REST API v1.12，详见 analyzer_author 模块说明）：

- 作者列表：``GET /staticAnalysisViolations/count?buildId=..&filterId=..
  &groupBy=author&statuses=failed,suppressed``（每个仓库每个 build 一次请求）；
- 作者违规/抑制：``...&author=<作者>&statuses=failed,suppressed``
  （DTP 不传 statuses 时只算 failed，返回体里连 suppressed 字段都没有）；
- 作者严重级别：``...&author=<作者>&groupBy=severity``。

仓库范围（由 main.py 传入的 source_projects / project_keys 决定）：
``explicit``（DTP 仓库名）> ``project_keys``（merge_projects 的项目名，取这些项目下的
仓库）> ``report.project_names`` > ``merge_projects`` 的全部源项目。

报告内容（report_v4_zh.html）：

1. 摘要：该开发者**相关仓库**的整仓"文件总数/代码总数" + 该开发者的"违规总数"，
   缺陷密度 = 违规总数 × 1000 / 代码总数（与项目报告同一公式与取整规则）；
2. 开发者概览：每个相关仓库**最后一次构建**的文件数、代码行数，以及该开发者在这次
   构建上的违规/抑制/合计/缺陷密度；
3. 严重级别明细表：每个相关仓库最后一次构建上该开发者的 Sev1~Sev5 分布；
4. 开发者违规图：各相关仓库最后一次构建上该开发者的"违规与抑制"对比条形图；
5. 开发者趋势图：每个相关仓库一张曲线图，X 轴是构建次序（build-1、build-2 ...），
   Y 轴是该开发者在这次构建上的违规数量（每个仓库取**全部构建**；构建特别多时可用
   config.yaml 的 ``report.trend_builds`` 限制成"最近 N 次"）。

性能：DTP 抓取由 main.py 统一完成（"抓一次、给所有报告复用"）——

1. ``analyzer.collect_violations``：每个仓库 3 次请求（**最新** build 的违规数、
   文件数、代码行数）；
2. ``analyzer_author.collect_author_index``：每个仓库 1 次（构建列表）+ **每次构建**
   1 次（作者分布），把"每个仓库每次构建的作者分布"缓存成
   :class:`analyzer_author.AuthorIndex`。

本模块只在渲染前补抓该开发者的严重级别（每个相关仓库 1 次请求）。
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

import yaml

from analyzer import ProjectViolations, RepoBuildTrend
from analyzer_author import (
    AuthorIndex,
    author_report_rows,
    author_report_trends,
    collect_author_repo_rows,
    collect_author_repo_trends,
    collect_author_severities,
)
from chart_builder import chart_repo_trends, chart_total_violations_bad
from dtp_client import DTPClient
from project_merge import list_source_projects
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

# 趋势数据默认取每个仓库的“全部构建”（每次构建一个点）：0 = 全部构建；
# >0 = 只取最近 N 次（构建特别多、想缩短抓取时间时用）；<0 = 不抓趋势数据。
# 命令行 --trend-builds N 可覆盖；另见 trend_max_builds()。
DEFAULT_TREND_BUILDS = 0

# 并发抓取线程数；config.yaml 的 report.max_workers 可覆盖
DEFAULT_MAX_WORKERS = 4

# Windows 文件名非法字符（含控制字符）；作者名里可能出现 '\'（域账号），必须替换
_INVALID_FILE_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def load_config(config_path: str) -> dict:
    """读取 config.yaml。

    配置文件不存在时抛 :class:`FileNotFoundError`（不直接 ``sys.exit``），
    由调用方决定如何处理；main.py 的命令行分支负责把它转成退出码 1。
    """
    p = Path(config_path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


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

    解析结果为空时 main.py 的 collect_violations 会回退为扫描 DTP 全部项目（很慢，易超时）。
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
    """清理并重建报告**根目录**，让本次运行生成的报告是目录里唯一的内容。

    根目录（config.yaml ``report.output_dir``）下是三块报告各自的子目录
    （``company/`` / ``project/`` / ``author/``，见 ``main.REPORT_SUBDIRS``），
    清空根目录等于把上一轮的三类报告一起清掉。
    """
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
        # 趋势数据：0 = 每个仓库的全部构建（默认）；>0 = 最近 N 次；<0 = 不抓
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


def trend_max_builds(settings: dict) -> int | None:
    """把运行设置里的 ``trend_builds`` 翻译成 analyzer / analyzer_author 的 ``max_builds``。

    - ``0``（默认）= 每个仓库的**全部构建** → ``None``（交给
      :meth:`dtp_client.DTPClient.get_builds` 翻页取完）；
    - ``>0`` = 只抓最近 N 次构建，原样返回；
    - ``<0`` = 不抓趋势数据：:func:`analyzer.collect_build_trends` 直接返回空，
      作者索引退化成"只取最新一次构建"（表格还是最新的数据，但没有趋势图）。
    """
    trend_builds = int(settings.get("trend_builds", DEFAULT_TREND_BUILDS))
    return None if trend_builds == 0 else trend_builds


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
    - 趋势：每个相关仓库一条曲线，Y 轴是该作者在该次构建上的违规数
      （每个仓库取全部构建，见 ``report.trend_builds``）。
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
                max_builds=trend_max_builds(settings), max_workers=settings["max_workers"],
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


def format_author_table(index: AuthorIndex) -> list[str]:
    """统一入口 ``main.py --list-authors`` 的输出：开发者名 + 相关仓库数 + 违规总数。"""
    lines = [f"DTP 上共找到 {len(index.authors)} 位开发者（相关仓库数 / 违规总数）："]
    for position, author in enumerate(index.authors, 1):
        rows = index.repos_of(author)
        lines.append(
            f"  {position:>4}. {author}  ({len(rows)} 个仓库, 违规 {sum(r.failed for r in rows):,})"
        )
    return lines
