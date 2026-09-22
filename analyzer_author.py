"""DTP 开发者（author）维度数据采集。

数据来源与口径（全部经实测确认，DTP REST API v1.12）：

1. **作者列表**由"按作者分组的违规计数"得到::

       GET /staticAnalysisViolations/count?buildId=..&filterId=..
           &groupBy=author&statuses=failed,suppressed

   返回体里的 ``groupBy.groups[].person.name`` 就是作者（形如 ``FC_MARMO\\marmo``、
   ``hendra``、``A23006643``）。DTP 只列出"有违规/有抑制"的作者，所以某个仓库某次
   构建上零违规的作者不会出现——这正是"与作者有关的仓库"的判定依据，也是把缺失点
   当作 0（而不是当作没有数据）的依据。``limit``/``offset`` 对该接口无效，一次请求
   就能拿到该 build 的全部作者分组。

2. **抑制数**必须显式带上 ``statuses``：DTP 的 count 接口默认只统计 ``failed``，
   不传 ``statuses`` 时返回体里连 ``suppressed`` 字段都没有（这也是历史报告里
   "抑制"列一直是 0 的原因）。本项目统一用 ``statuses=failed,suppressed``。

3. **严重级别**只能按"一个分组维度 + 一个作者"查::

       GET /staticAnalysisViolations/count?...&groupBy=severity&author=<作者>

   因此严重级别要逐个 (作者, 仓库) 查询，见 :func:`collect_author_severities`。

4. **文件数 / 代码行数**在 DTP 里没有作者维度（``/staticAnalysisFiles``、
   ``/metrics/METRIC.NOPLIF`` 都是整个 build 的），所以报告里这两个数字是
   "该仓库整仓"的数字：摘要里的"文件总数/代码总数"= 该作者相关仓库的整仓合计，
   "违规总数"= 该作者的违规合计，缺陷密度 = 违规总数 × 1000 / 代码总数
   （与 :func:`analyzer.calculate_defect_density` 同一个公式与取整规则）。

一次抓取的产物是 :class:`AuthorIndex`：它把“每个仓库每次构建的作者违规分布”
缓存在内存里（默认取**全部构建**，可用 ``max_builds`` 限制次数），之后为任意多个
开发者出报告都只额外需要严重级别查询，不用再遍历构建。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from analyzer import BuildViolationPoint, ProjectViolations, RepoBuildTrend
from dtp_client import DTPClient

logger = logging.getLogger(__name__)

# 采集开发者索引时的并发线程数。与 analyzer._TREND_MAX_WORKERS 同理：
# DTP 的 count 接口单次 1~3 秒，少量线程并发（实测 4 线程安全）能把
# “60 个仓库 × 每个仓库的全部构建”的抓取时间压到可以接受的范围内。
_DEFAULT_MAX_WORKERS = 4


@dataclass
class RepoBuildAuthors:
    """某个仓库某一次构建上的"作者 -> 违规/抑制"分布。"""

    build_id: str
    # DTP /builds 返回的构建时间（ISO8601，如 "2026-09-11T09:02:29Z"），可能为空
    build_date: str
    # 该 build 是否有静态分析数据（DTP count 返回体的 hasData）
    has_data: bool
    # 作者名 -> {"failed": n, "suppressed": n}；DTP 只返回有违规/有抑制的作者
    authors: dict[str, dict[str, int]] = field(default_factory=dict)

    def stats_of(self, author: str) -> dict[str, int]:
        """取该作者的计数；作者不在这次构建的分组里时返回 0（作者这次没违规）。"""
        return self.authors.get(author) or {"failed": 0, "suppressed": 0}


@dataclass
class AuthorIndex:
    """开发者索引：一次较慢的抓取，之后为任意多个开发者出报告都不用再抓。

    - :attr:`repos`：仓库基础信息（来自 :func:`analyzer.collect_violations`，
      带 ``filter_id`` / 最新 ``build_id`` / 文件数 / 代码行数）；
    - :attr:`repo_builds`：``仓库名 -> [RepoBuildAuthors, ...]``，顺序与 DTP
      ``/builds`` 一致（**从新到旧**）；
    - :attr:`authors`：全部开发者名，按"违规总数从多到少"排序（界面列表顺序）；
    - :attr:`max_builds`：抓取时每个仓库取了多少次构建的违规数（``None`` = 全部
      构建；``1`` = 只取了最新一次，此时没有趋势数据，报告第 5 章也就没有曲线图）。
    """

    repos: list[ProjectViolations] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    repo_builds: dict[str, list[RepoBuildAuthors]] = field(default_factory=dict)
    max_builds: int | None = None

    # ------------------------------------------------------------------
    # 派生视图
    # ------------------------------------------------------------------
    def latest_build(self, repo_name: str) -> RepoBuildAuthors | None:
        """该仓库最近一次"有静态分析数据"的构建（没有则返回 None）。"""
        for build in self.repo_builds.get(repo_name, []):
            if build.has_data:
                return build
        return None

    def has_author(self, repo_name: str, author: str) -> bool:
        """该作者是否与该仓库有关（在抓取到的任意一次构建里出现过）。"""
        for build in self.repo_builds.get(repo_name, []):
            if author in build.authors:
                return True
        return False

    def repos_of(self, author: str) -> list[ProjectViolations]:
        """与该作者有关的所有仓库，顺序与 config.yaml 中仓库出现的顺序一致。

        每行的字段口径：

        - ``failed`` / ``suppressed``：该作者在该仓库**最后一次构建**上的违规/抑制数；
        - ``file_count`` / ``code_lines``：该仓库整仓的数字（DTP 没有作者维度的
          文件数与代码行数），用于摘要的"文件总数/代码总数"和缺陷密度分母；
        - ``has_data``：该仓库有静态分析数据时为 True（此处必为 True）；
        - ``severity_breakdown`` 留空，由 :func:`collect_author_severities` 补抓。
        """
        rows: list[ProjectViolations] = []

        for repo in self.repos:
            if not self.has_author(repo.project_name, author):
                continue

            latest = self.latest_build(repo.project_name)
            stats = latest.stats_of(author) if latest else {"failed": 0, "suppressed": 0}

            rows.append(
                ProjectViolations(
                    project_name=repo.project_name,
                    filter_id=repo.filter_id,
                    filter_name=repo.filter_name,
                    build_id=(latest.build_id if latest else repo.build_id),
                    has_data=True,
                    failed=int(stats.get("failed") or 0),
                    suppressed=int(stats.get("suppressed") or 0),
                    file_count=repo.file_count,
                    code_lines=repo.code_lines,
                )
            )

        return rows

    def trend_of(self, author: str, repo: ProjectViolations) -> RepoBuildTrend:
        """该作者在该仓库的趋势（从旧到新排列，供趋势图使用）。

        - 只保留该仓库**有静态分析数据**的构建；
        - 作者没有出现在某次构建的分组里 → 记为 0（该作者那次构建没有违规），
          这样曲线不会因为"作者某次零违规"而断档；
        - 仓库名 / filter 信息沿用 ``repo``；X 轴的 build-1、build-2 ... 由
          ``chart_builder.chart_repo_trends`` 按顺序生成。
        """
        # DTP /builds 是从新到旧，趋势图 X 轴要求从旧到新，所以这里反转
        points: list[BuildViolationPoint] = []

        for build in reversed(self.repo_builds.get(repo.project_name, [])):
            if not build.has_data:
                continue
            stats = build.stats_of(author)
            points.append(
                BuildViolationPoint(
                    build_id=build.build_id,
                    build_date=build.build_date,
                    has_data=True,
                    failed=int(stats.get("failed") or 0),
                    suppressed=int(stats.get("suppressed") or 0),
                )
            )

        return RepoBuildTrend(
            repo_name=repo.project_name,
            filter_id=repo.filter_id,
            filter_name=repo.filter_name,
            points=points,
        )


def _safe_builds(client: DTPClient, repo: ProjectViolations, limit: int | None) -> list[dict]:
    """取一个仓库的构建列表（``limit=None`` = 全部构建；失败只记日志，不影响其它仓库）。"""
    try:
        return client.get_builds(filter_id=repo.filter_id, limit=limit) or []
    except Exception as exc:
        logger.warning("Failed to get builds for %s: %s", repo.project_name, exc)
        return []


def _safe_author_counts(
    client: DTPClient,
    repo: ProjectViolations,
    build_id: str,
) -> tuple[bool, dict[str, dict[str, int]]]:
    """取一个构建的作者分布（失败时返回 ``(False, {})``，不影响其它构建）。"""
    try:
        return client.get_author_counts(build_id=build_id, filter_id=repo.filter_id)
    except Exception as exc:
        logger.warning(
            "Failed to get author breakdown for %s @ %s: %s",
            repo.project_name, build_id, exc,
        )
        return False, {}


def collect_author_index(
    client: DTPClient,
    repos: list[ProjectViolations],
    max_builds: int | None = None,
    max_workers: int = _DEFAULT_MAX_WORKERS,
) -> AuthorIndex:
    """抓取"开发者索引"：每个仓库**每次构建**（默认全部构建）的作者违规分布。

    :param repos: 仓库列表，通常是 :func:`analyzer.collect_violations` 的返回值
        （一个仓库一条记录，带 ``filter_id``）。同一个 ``filter_id`` 只抓一次。
    :param max_builds: 每个仓库取多少次构建的违规数：``None``（默认）= **全部构建**；
        正整数 = 只取最近 N 次（构建特别多、想缩短抓取时间时用）；``<= 0`` = 只取
        最新一次（能拿到作者列表，但没有趋势数据）。
    :param max_workers: 并发线程数（``<= 1`` 时退化为串行）。

    请求量 = ``仓库数``（取构建列表）+ ``仓库数 × 构建数``（每次构建一次
    ``groupBy=author``，一次请求就能拿到该次构建下全部作者的违规数）；抓一次可给
    所有开发者复用，所以 report_author.py 与界面都缓存这个索引。
    """
    if not repos:
        return AuthorIndex(repos=[], authors=[], repo_builds={}, max_builds=max_builds)

    # 同一个仓库（同 filter_id）只抓一次：config.yaml 里一个仓库可能属于多个项目组
    unique_repos: list[ProjectViolations] = []
    seen_filter_ids: set[int] = set()
    for repo in repos:
        if repo.filter_id in seen_filter_ids:
            continue
        seen_filter_ids.add(repo.filter_id)
        unique_repos.append(repo)

    # max_builds：None = 全部构建（交给 DTPClient.get_builds 翻页）；
    # 正整数 = 最近 N 次；<= 0 = 只取最新一次（够出表格，但没有趋势数据）
    if max_builds is None or int(max_builds) > 0:
        limit: int | None = max_builds
    else:
        limit = 1
    workers = max(1, min(int(max_workers), max(1, len(unique_repos))))

    # 阶段 1：并发取每个仓库的构建列表（DTP 返回"从新到旧"）
    logger.info(
        "Collecting author data for %d repo(s) (%s)...",
        len(unique_repos),
        "all builds" if limit is None else f"latest {limit} build(s)",
    )
    with ThreadPoolExecutor(max_workers=workers) as pool:
        build_lists = list(pool.map(lambda r: _safe_builds(client, r, limit), unique_repos))

    # 阶段 2：把所有 (仓库, build) 摊平成一个任务表并发抓取，充分利用线程
    tasks: list[tuple[ProjectViolations, dict]] = [
        (repo, build)
        for repo, builds in zip(unique_repos, build_lists)
        for build in builds
    ]

    def fetch(task: tuple[ProjectViolations, dict]) -> tuple[ProjectViolations, dict, bool, dict]:
        repo, build = task
        build_id = str(build.get("id", ""))
        has_data, authors = _safe_author_counts(client, repo, build_id)
        return repo, build, has_data, authors

    if tasks:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            fetched = list(pool.map(fetch, tasks))
    else:
        fetched = []

    repo_builds: dict[str, list[RepoBuildAuthors]] = {}
    author_totals: dict[str, int] = {}

    for repo, build, has_data, authors in fetched:
        repo_builds.setdefault(repo.project_name, []).append(
            RepoBuildAuthors(
                build_id=str(build.get("id", "")),
                build_date=str(build.get("date") or ""),
                has_data=has_data,
                authors=authors,
            )
        )
        for name, stats in authors.items():
            author_totals[name] = author_totals.get(name, 0) + int(stats.get("failed") or 0)

    # 违规多的排在前面，界面里一眼就能看到"违规大户"；同分按名字排序保证顺序稳定
    authors_sorted = sorted(author_totals, key=lambda name: (-author_totals[name], name))

    logger.info(
        "Author data ready: %d author(s) over %d repo(s), %d build(s) scanned",
        len(authors_sorted), len(repo_builds), len(fetched),
    )

    return AuthorIndex(
        repos=unique_repos,
        authors=authors_sorted,
        repo_builds=repo_builds,
        max_builds=max_builds,
    )


def author_report_rows(index: AuthorIndex, author: str) -> list[ProjectViolations]:
    """从索引里取出"某个开发者的报告明细行"（每个相关仓库一行）。

    行内容即报告第 2 章"开发者概览"与第 3 章"严重级别明细表"的数据源；
    严重级别还需要按仓库补抓，见 :func:`collect_author_severities`。
    """
    return index.repos_of(author) if index else []


def author_report_trends(
    index: AuthorIndex,
    author: str,
    rows: list[ProjectViolations],
) -> dict[str, RepoBuildTrend]:
    """从索引里取出该开发者在各仓库的趋势（``{仓库名: RepoBuildTrend}``）。

    仓库顺序与 ``rows``（报告表格顺序）一致；抓索引时每个仓库只取了 1 次构建
    （``max_builds == 1``，即只看最新一次构建）时返回空字典，报告第 5 章就没有曲线图。
    """
    if not index or index.max_builds == 1:
        return {}

    trends: dict[str, RepoBuildTrend] = {}
    for repo in rows:
        trend = index.trend_of(author, repo)
        if trend.has_data:
            trends[repo.project_name] = trend
    return trends


def collect_author_severities(
    client: DTPClient,
    author: str,
    rows: list[ProjectViolations],
    max_workers: int = _DEFAULT_MAX_WORKERS,
) -> None:
    """补抓"该作者在各仓库最后一次构建上的严重级别分布"，写回每行的 severity_breakdown。

    DTP 一次只能按一个维度分组，所以严重级别必须逐个 (作者, 仓库) 查询
    （``groupBy=severity&author=..``）；``rows`` 通常只有几个到几十行，并发后很快。
    某一行查询失败时该行的 ``severity_breakdown`` 保持为空（模板里显示 0），
    不影响其它行。
    """
    if not rows:
        return

    workers = max(1, min(int(max_workers), len(rows)))

    def fetch(row: ProjectViolations) -> tuple[ProjectViolations, dict[str, int]]:
        try:
            return row, client.get_severity_counts(
                build_id=row.build_id,
                filter_id=row.filter_id,
                author=author,
            )
        except Exception as exc:
            logger.warning(
                "Failed to get severity breakdown for %s @ %s (%s): %s",
                row.project_name, row.build_id, author, exc,
            )
            return row, {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for row, breakdown in pool.map(fetch, rows):
            row.severity_breakdown = breakdown


def collect_author_repo_rows(
    client: DTPClient,
    author: str,
    repos: list[ProjectViolations],
    max_workers: int = _DEFAULT_MAX_WORKERS,
) -> list[ProjectViolations]:
    """兜底路径：按 ``author=`` 逐个仓库查询，找出"手工添加的开发者"相关的仓库。

    界面上允许手工添加 DTP 作者列表里没有的开发者（新同事、名字写法不同等），
    这类作者不在缓存的索引里，只能逐个仓库问 DTP："这个作者在该仓库最后一次构建上
    有多少违规"。只有违规或抑制大于 0 的仓库才会进入报告。

    返回行的字段口径与 :meth:`AuthorIndex.repos_of` 完全一致。
    """
    candidates = [repo for repo in repos if repo.has_data]
    if not candidates:
        return []

    workers = max(1, min(int(max_workers), len(candidates)))

    def fetch(repo: ProjectViolations) -> ProjectViolations | None:
        try:
            has_data, stats = client.get_author_violation_count(
                build_id=repo.build_id,
                filter_id=repo.filter_id,
                author=author,
            )
        except Exception as exc:
            logger.warning(
                "Failed to query author %s for %s: %s", author, repo.project_name, exc,
            )
            return None

        if not has_data:
            return None

        failed = int(stats.get("failed") or 0)
        suppressed = int(stats.get("suppressed") or 0)
        if failed <= 0 and suppressed <= 0:
            return None

        return ProjectViolations(
            project_name=repo.project_name,
            filter_id=repo.filter_id,
            filter_name=repo.filter_name,
            build_id=repo.build_id,
            has_data=True,
            failed=failed,
            suppressed=suppressed,
            file_count=repo.file_count,
            code_lines=repo.code_lines,
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        fetched = [row for row in pool.map(fetch, candidates) if row is not None]

    # 保持仓库在 config.yaml 里的顺序
    order = {repo.project_name: index for index, repo in enumerate(candidates)}
    fetched.sort(key=lambda row: order.get(row.project_name, len(order)))

    return fetched


def collect_author_repo_trends(
    client: DTPClient,
    author: str,
    rows: list[ProjectViolations],
    max_builds: int | None = None,
    max_workers: int = _DEFAULT_MAX_WORKERS,
) -> dict[str, RepoBuildTrend]:
    """兜底路径的趋势数据：手工添加的开发者没有缓存，逐 build 按作者查询。

    与 :func:`analyzer.collect_build_trends` 的唯一差别是每次构建的计数都带上
    ``author=`` 过滤，因此得到的是"该作者在该仓库每次构建上的违规数"。
    默认 ``max_builds=None`` 取该仓库的**全部构建**；``>0`` 只取最近 N 次；
    ``<=0`` 不采集（返回空字典）。
    """
    if (max_builds is not None and max_builds <= 0) or not rows:
        return {}

    workers = max(1, min(int(max_workers), len(rows)))

    def fetch_trend(row: ProjectViolations) -> tuple[str, RepoBuildTrend]:
        return row.project_name, _collect_author_build_points(client, author, row, max_builds)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(fetch_trend, rows))

    trends: dict[str, RepoBuildTrend] = {}
    for repo_name, trend in results:
        if trend.has_data:
            trends[repo_name] = trend
    return trends


def _collect_author_build_points(
    client: DTPClient,
    author: str,
    repo: ProjectViolations,
    max_builds: int | None,
) -> RepoBuildTrend:
    """逐 build 采集"该作者"在一个仓库构建上的违规数（``max_builds=None`` = 全部构建）。"""
    try:
        builds = client.get_builds(filter_id=repo.filter_id, limit=max_builds)
    except Exception as exc:
        logger.warning("Failed to get builds for %s: %s", repo.project_name, exc)
        return RepoBuildTrend(repo.project_name, repo.filter_id, repo.filter_name, [])

    points: list[BuildViolationPoint] = []

    # DTP 返回"从新到旧"，趋势图 X 轴要求从旧到新，所以反转
    for build in reversed(builds or []):
        build_id = str(build.get("id", ""))
        try:
            has_data, stats = client.get_author_violation_count(
                build_id=build_id,
                filter_id=repo.filter_id,
                author=author,
            )
        except Exception as exc:
            logger.warning(
                "Failed to get violation count for %s @ %s: %s",
                repo.project_name, build_id, exc,
            )
            continue

        if not has_data:
            continue

        points.append(
            BuildViolationPoint(
                build_id=build_id,
                build_date=str(build.get("date") or ""),
                has_data=True,
                failed=int(stats.get("failed") or 0),
                suppressed=int(stats.get("suppressed") or 0),
            )
        )

    return RepoBuildTrend(repo.project_name, repo.filter_id, repo.filter_name, points)
