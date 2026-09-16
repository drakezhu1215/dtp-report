from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from dtp_client import DTPClient

logger = logging.getLogger(__name__)

SEVERITY_LABELS = {
    "1": "Severity 1 (Critical)",
    "2": "Severity 2 (Major)",
    "3": "Severity 3 (Moderate)",
    "4": "Severity 4 (Minor)",
    "5": "Severity 5 (Info)",
}


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
        - 单位是"每千行代码（KLOC）的违规数"，所以乘 1000；
        - 代码行数为 0（调用方没开启 ``with_code_lines``，或 DTP 该 build
          未上报代码度量）时缺省为 ``0``；该项目本身没有静态分析数据
          （``has_data`` 为 False）时同样是 0；
        - 用 ``Decimal`` + ``ROUND_HALF_UP`` 做四舍五入，避免 Python 内置
          ``round()`` 的银行家舍入（``round(0.5) == 0``、``round(2.5) == 2``）。

        合并组（merge_project.merge_project_violations）不需要额外处理：
        它合并了 failed / code_lines，本属性按合并后的值计算。
        """
        if not self.has_data or self.code_lines <= 0:
            return 0

        density = (
            Decimal(self.failed) * 1000 / Decimal(self.code_lines)
        ).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        return int(density)


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
