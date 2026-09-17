from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from dtp_client import DTPClient

logger = logging.getLogger(__name__)

# 采集“每次构建违规数”时的并发线程数。
# DTP 的 /staticAnalysisViolations/count 接口单次实测 1~3 秒（违规几十万的项目更慢），
# 逐 build 串行请求会让趋势数据采集很慢；用少量线程并发（实测 4 线程安全，
# 结果与串行完全一致，8 次请求 6.7s vs 串行 13.6s）。
_TREND_MAX_WORKERS = 4

SEVERITY_LABELS = {
    "1": "Severity 1 (Critical)",
    "2": "Severity 2 (Major)",
    "3": "Severity 3 (Moderate)",
    "4": "Severity 4 (Minor)",
    "5": "Severity 5 (Info)",
}


def calculate_defect_density(failed: int, code_lines: int) -> int:
    """缺陷密度 = 违规数量 × 1000 / 代码行数，四舍五入后取整。

    - 违规数量取未解决违规（不含 suppressed）；
    - 单位是"每千行代码（KLOC）的违规数"，所以乘 1000；
    - 代码行数 <= 0（调用方没开启 ``with_code_lines``，或 DTP 该 build
      未上报代码度量）时返回 ``0``；
    - 用 ``Decimal`` + ``ROUND_HALF_UP`` 做四舍五入，避免 Python 内置
      ``round()`` 的银行家舍入（``round(0.5) == 0``、``round(2.5) == 2``）。

    单个仓库（:attr:`ProjectViolations.defect_density`）和整个项目的摘要
    （report_html 的汇总卡片）都走这个函数，保证口径一致。
    """
    if failed <= 0 or code_lines <= 0:
        return 0

    density = (
        Decimal(failed) * 1000 / Decimal(code_lines)
    ).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return int(density)


@dataclass
class ProjectViolations:
    project_name: str
    filter_id: int
    filter_name: str
    build_id: str
    has_data: bool
    failed: int
    suppressed: int
    # 有静态分析违规的源文件数量（由 DTP 按 resource 分组统计得到）。
    # 默认 0：只有显式要求（collect_violations(with_file_count=True)）时才去查询，
    # 因此不影响 main_project.py 之类的调用方。
    file_count: int = 0
    # 代码行数合计（DTP metrics 指标值，主选 METRIC.NOPLIF 物理行数）。
    # 同样默认 0，仅在 collect_violations(with_code_lines=True) 时查询。
    code_lines: int = 0
    severity_breakdown: dict[str, int] = field(default_factory=dict)
    category_breakdown: dict[str, int] = field(default_factory=dict)

    @property
    def defect_density(self) -> int:
        """缺陷密度 = 违规数量 × 1000 / 代码行数，四舍五入后取整。

        - 违规数量 = ``failed``（报告"违规"列，未解决违规；不含 suppressed）；
        - 代码行数 = ``code_lines``（DTP metrics 物理行数合计）；
        - 计算与取整规则见 :func:`calculate_defect_density`；
        - 该项目本身没有静态分析数据（``has_data`` 为 False）时返回 0。

        合并组（merge_project.merge_project_violations）不需要额外处理：
        它合并了 failed / code_lines，本属性按合并后的值计算。
        """
        if not self.has_data:
            return 0

        return calculate_defect_density(self.failed, self.code_lines)


def collect_violations(
    client: DTPClient,
    project_names: list[str] | None = None,
    with_file_count: bool = False,
    with_code_lines: bool = False,
) -> list[ProjectViolations]:
    """抓取各项目的违规数据。

    ``with_file_count=True`` 时额外查询 "DTP 扫描的源文件数"（每个 build 多一次
    REST 请求），写入 ``ProjectViolations.file_count``；``with_code_lines=True``
    时额外查询 "代码行数"（同样每个 build 多一次 REST 请求），写入
    ``ProjectViolations.code_lines``。两个开关默认都是 False，调用方行为与以前
    完全一致。
    """
    all_projects = client.get_projects()
    name_set = set(project_names) if project_names else None

    results: list[ProjectViolations] = []
    for proj in all_projects:
        pname = proj.get("name", "")
        if name_set and pname not in name_set:
            continue

        filters = client.get_filters(project_id=proj["id"])
        default_filters = [f for f in filters if f.get("isDefaultForProject")]
        target_filters = default_filters if default_filters else filters

        for f in target_filters:
            fid = f["id"]
            try:
                builds = client.get_builds(filter_id=fid, limit=1)
            except Exception as exc:
                logger.warning("Failed to get builds for filter %s: %s", fid, exc)
                continue

            if not builds:
                logger.info("No builds for filter %s (%s)", fid, f.get("name"))
                continue

            build_id = builds[0].get("id", "")
            pv = _collect_for_build(
                client, pname, fid, f.get("name", ""), build_id,
                with_file_count=with_file_count,
                with_code_lines=with_code_lines,
            )
            results.append(pv)

    return results


def _collect_for_build(
    client: DTPClient,
    project_name: str,
    filter_id: int,
    filter_name: str,
    build_id: str,
    with_file_count: bool = False,
    with_code_lines: bool = False,
) -> ProjectViolations:
    try:
        total = client.get_violation_count(build_id=build_id, filter_id=filter_id)
    except Exception as exc:
        logger.warning("Failed to get total count for %s: %s", build_id, exc)
        return ProjectViolations(
            project_name=project_name,
            filter_id=filter_id,
            filter_name=filter_name,
            build_id=build_id,
            has_data=False,
            failed=0,
            suppressed=0,
        )

    pv = ProjectViolations(
        project_name=project_name,
        filter_id=filter_id,
        filter_name=filter_name,
        build_id=build_id,
        has_data=bool(total.get("hasData", False)),
        failed=int(total.get("failed") or 0),
        suppressed=int(total.get("suppressed") or 0),
    )

    if not pv.has_data:
        return pv

    if with_file_count:
        # 该 build 下被 DTP 静态分析扫描的源文件数（/staticAnalysisFiles 的 total）
        try:
            pv.file_count = client.get_file_count(build_id=build_id, filter_id=filter_id)
        except Exception as exc:
            logger.warning("Failed to get file count for %s: %s", build_id, exc)

    if with_code_lines:
        # 该 build 的代码行数（DTP metrics 指标聚合值，主选 METRIC.NOPLIF）
        try:
            pv.code_lines = client.get_code_lines(build_id=build_id, filter_id=filter_id)
        except Exception as exc:
            logger.warning("Failed to get code lines for %s: %s", build_id, exc)

    for group_by, target_attr in [("severity", "severity_breakdown"), ("category", "category_breakdown")]:
        try:
            resp = client.get_violation_count(build_id=build_id, filter_id=filter_id, group_by=group_by)
        except Exception as exc:
            logger.warning("Failed to get %s breakdown for %s: %s", group_by, build_id, exc)
            continue

        gb = resp.get("groupBy")
        if not gb:
            continue
        groups = gb.get("groups", [])
        breakdown: dict[str, int] = {}
        for g in groups:
            key_obj = g.get(group_by, {})
            key = str(key_obj.get("name", "Unknown")) if isinstance(key_obj, dict) else str(key_obj)
            count = int(g.get("failed") or 0)
            breakdown[key] = count
        setattr(pv, target_attr, breakdown)

    return pv


@dataclass
class BuildViolationPoint:
    """一次构建（build）的违规数据，也就是趋势图上的一个数据点。"""

    build_id: str
    # DTP /builds 返回的构建时间（ISO8601，如 "2026-09-11T09:02:29Z"），可能为空。
    build_date: str
    has_data: bool
    failed: int
    suppressed: int


@dataclass
class RepoBuildTrend:
    """一个仓库的构建趋势：若干次构建的违规数据，按构建时间从旧到新排列。

    趋势图 X 轴的 ``build-1``、``build-2`` ... 就是 ``points`` 的下标 + 1
    （见 chart_builder.chart_repo_trends）。
    """

    repo_name: str
    filter_id: int
    filter_name: str
    points: list[BuildViolationPoint] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        """是否有可用于画点的构建数据（没有任何构建数据时该仓库不出趋势图）。"""
        return any(point.has_data for point in self.points)


def collect_build_trends(
    client: DTPClient,
    repos: list[ProjectViolations],
    max_builds: int = 10,
    max_workers: int = _TREND_MAX_WORKERS,
) -> dict[str, RepoBuildTrend]:
    """采集每个仓库“最近若干次构建各自的违规数量”，供项目趋势图使用。

    - ``repos`` 通常是 :func:`collect_violations` 的返回值：一个仓库一条记录，
      记录里已经带上了该仓库对应的 ``filter_id``（DTP 里 Filter 与仓库一一对应）；
    - 每个仓库先调用 ``/builds?filterId=..&limit=max_builds`` 取最近
      ``max_builds`` 次构建，再对每次构建调用
      ``/staticAnalysisViolations/count?buildId=..&filterId=..`` 取违规数量；
    - count 接口单次实测 1~3 秒，因此每次构建一次请求，用 ``max_workers``
      个线程并发请求（``max_workers<=1`` 时退化为串行）；
    - ``max_builds<=0`` 表示不采集趋势数据，直接返回空字典（不发起任何请求）；
    - DTP 的 build 列表是“从新到旧”，这里反转为“从旧到新”，让趋势图 X 轴
      从左到右依次是 build-1（最早）、build-2 ... build-N（最新）；
    - 某个 build 的 count 请求失败、或 ``hasData=false``（该次构建没有静态分析
      数据）时跳过该数据点并记日志，不影响该仓库的其他构建；
    - 返回 ``{仓库名: RepoBuildTrend}``；同一个仓库只会查询一次，
      因为一个仓库可能同时属于多个项目组。

    返回的 :class:`RepoBuildTrend` 按 x 轴序号取点即可：
    ``points[i].failed`` 就是 ``build-(i+1)`` 的违规数量。
    """
    if max_builds <= 0 or not repos:
        return {}

    # 仓库名 -> 该仓库的数据；同一仓库（同名）只采集一次
    unique_repos: dict[str, ProjectViolations] = {}
    for repo in repos:
        unique_repos.setdefault(repo.project_name, repo)

    trends: dict[str, RepoBuildTrend] = {}
    for index, (repo_name, repo) in enumerate(unique_repos.items(), 1):
        points = _collect_build_points(client, repo, max_builds, max_workers)
        trends[repo_name] = RepoBuildTrend(
            repo_name=repo_name,
            filter_id=repo.filter_id,
            filter_name=repo.filter_name,
            points=points,
        )
        logger.info(
            "[%d/%d] Trend data for %s: %d build(s) with violation data",
            index, len(unique_repos), repo_name, len(points),
        )

    return trends


def _collect_build_points(
    client: DTPClient,
    repo: ProjectViolations,
    max_builds: int,
    max_workers: int,
) -> list[BuildViolationPoint]:
    """采集单个仓库最近 ``max_builds`` 次构建的违规数据（从旧到新排列）。"""
    try:
        builds = client.get_builds(filter_id=repo.filter_id, limit=max_builds)
    except Exception as exc:
        logger.warning("Failed to get builds for %s: %s", repo.project_name, exc)
        return []

    if not builds:
        logger.info("No builds for %s, trend chart skipped", repo.project_name)
        return []

    # DTP 返回的是“从新到旧”，反转后 X 轴从左到右即为 build-1、build-2 ...
    builds = list(reversed(builds))

    def fetch(build: dict) -> tuple[dict, dict | None]:
        build_id = str(build.get("id", ""))
        try:
            return build, client.get_violation_count(build_id=build_id, filter_id=repo.filter_id)
        except Exception as exc:
            logger.warning("Failed to get violation count for build %s: %s", build_id, exc)
            return build, None

    workers = max(1, min(max_workers, len(builds)))
    if workers == 1:
        fetched = [fetch(build) for build in builds]
    else:
        # ThreadPoolExecutor.map 保持入参顺序，因此并发后顺序仍然是“从旧到新”
        with ThreadPoolExecutor(max_workers=workers) as pool:
            fetched = list(pool.map(fetch, builds))

    points: list[BuildViolationPoint] = []
    for build, count in fetched:
        build_id = str(build.get("id", ""))
        if count is None:
            continue
        if not count.get("hasData"):
            logger.info("Build %s has no static analysis data, skipped in trend", build_id)
            continue

        points.append(
            BuildViolationPoint(
                build_id=build_id,
                build_date=str(build.get("date") or ""),
                has_data=True,
                failed=int(count.get("failed") or 0),
                suppressed=int(count.get("suppressed") or 0),
            )
        )

    return points
