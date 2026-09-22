#!/usr/bin/env python3
"""config.yaml 的 ``merge_projects``（项目 -> 仓库列表）合并工具。

提供三件事：列出配置里的全部源项目（list_source_projects）、按合并组分组但保留
仓库明细（group_project_violations）、把多个仓库求和成"虚拟项目"（merge_project_violations）
并去重（unique_repo_violations）。公司报告、项目报告与统一入口 main.py 都用它解析
"项目名 <-> 仓库名" 的归属关系。
"""

from collections import defaultdict
from analyzer import ProjectViolations


def list_source_projects(merge_projects: dict[str, list[str]]) -> list[str]:
    """列出 merge_projects 中所有源项目名（去重复、去首尾空白，保持配置顺序）。

    未显式指定抓取范围时，用它限定 DTP 查询的项目集合：DTP 上每个 build 都要发
    total / severity / category 三次请求，全量扫描所有项目极易触发 30s 读超时。
    """
    names: list[str] = []
    seen: set[str] = set()

    for source_projects in (merge_projects or {}).values():
        for project_name in source_projects or []:
            clean_name = str(project_name).strip()
            if clean_name and clean_name not in seen:
                seen.add(clean_name)
                names.append(clean_name)

    return names


def group_project_violations(
    violations: list[ProjectViolations],
    merge_projects: dict[str, list[str]],
) -> list[tuple[str, list[ProjectViolations]]]:
    """按配置把源项目按合并组分组（不做数值合并）。

    返回值是 ``[(组名, [该组的源项目数据, ...]), ...]``：

    - 组名（配置的 key，如 "海外三相电表平台"）作为项目名，组内元素是该项目下
      各个仓库（如 spln_3p_application ...）的原始 ProjectViolations；
    - 组内顺序与 merge_projects 中配置的源项目顺序一致；
    - DTP 上不存在的源项目被跳过；一个源项目都没抓到的组不出现在结果里。

    与 merge_project_violations 的区别：这里不把多个仓库求和成一个虚拟项目，
    供 "每个项目一份报告、报告内按仓库列明细" 的场景使用。
    """
    project_map = {
        item.project_name: item
        for item in violations
    }

    grouped: list[tuple[str, list[ProjectViolations]]] = []

    for project_name, source_projects in (merge_projects or {}).items():
        merged_items = [
            project_map[name]
            for name in (source_projects or [])
            if name in project_map
        ]

        if not merged_items:
            continue

        grouped.append((project_name, merged_items))

    return grouped


def unique_repo_violations(
    groups: list[tuple[str, list[ProjectViolations]]],
) -> list[ProjectViolations]:
    """把分组结果里出现过的仓库去重后展开成一个列表（按分组出现的先后顺序）。

    一个仓库可能同时属于多个项目组（配置里 dual_mode_cco 就同时属于
    "国网营销双模湖南扩展协议" 和 "国网营销双模能源控制器(专变)模块"）。
    报告里每个项目各画一张趋势图，但抓取趋势数据时只应该抓一次，
    所以先用本函数去重，再交给 analyzer.collect_build_trends。

    以 ``filter_id`` 判断是否为同一个仓库（DTP 里 Filter 与仓库一一对应）。
    """
    result: list[ProjectViolations] = []
    seen: set[int] = set()

    for _, repo_data in groups or []:
        for repo in repo_data or []:
            if repo.filter_id in seen:
                continue
            seen.add(repo.filter_id)
            result.append(repo)

    return result


def merge_project_violations(
    violations: list[ProjectViolations],
    merge_projects: dict[str, list[str]],
) -> list[ProjectViolations]:
    """按配置把多个源项目聚合成合并组。

    - group 名（配置的 key）作为合并后的 project_name，各数值字段求和/合并；
    - 组内一个源项目都没抓到则跳过该组；
    - 不在 merge_projects 里出现的项目会被忽略（不进入报告）。
    """

    # project_name -> ProjectViolations
    project_map = {
        item.project_name: item
        for item in violations
    }

    result: list[ProjectViolations] = []

    for new_project_name, source_projects in merge_projects.items():

        merged_items = []

        for project_name in source_projects:
            item = project_map.get(project_name)

            if item is not None:
                merged_items.append(item)

        # 配置了合并关系，但是实际没有找到任何项目
        if not merged_items:
            continue

        # ==========================
        # 1. 合并 failed / suppressed
        # ==========================

        failed = sum(
            item.failed
            for item in merged_items
        )

        suppressed = sum(
            item.suppressed
            for item in merged_items
        )

        # ==========================
        # 2. 合并 has_data
        # ==========================

        has_data = any(
            item.has_data
            for item in merged_items
        )

        # ==========================
        # 2.1 合并文件数（DTP 扫描的源文件数）
        # ==========================

        file_count = sum(
            item.file_count
            for item in merged_items
        )

        # ==========================
        # 2.2 合并代码行数（DTP metrics 指标合计）
        # ==========================

        code_lines = sum(
            item.code_lines
            for item in merged_items
        )

        # ==========================
        # 3. 合并 severity_breakdown
        # ==========================

        severity_breakdown: dict[str, int] = defaultdict(int)

        for item in merged_items:
            for severity, count in item.severity_breakdown.items():
                severity_breakdown[severity] += count

        # ==========================
        # 4. 合并 category_breakdown
        # ==========================

        category_breakdown: dict[str, int] = defaultdict(int)

        for item in merged_items:
            for category, count in item.category_breakdown.items():
                category_breakdown[category] += count

        # ==========================
        # 5. 创建新的 ProjectViolations
        # ==========================

        merged_project = ProjectViolations(
            project_name=new_project_name,

            # 合并项目没有唯一的 filter/build，
            # 这里暂时使用第一个项目的信息
            filter_id=merged_items[0].filter_id,
            filter_name=merged_items[0].filter_name,
            build_id=merged_items[0].build_id,

            has_data=has_data,
            failed=failed,
            suppressed=suppressed,
            file_count=file_count,
            code_lines=code_lines,

            severity_breakdown=dict(severity_breakdown),
            category_breakdown=dict(category_breakdown),
        )

        result.append(merged_project)

    return result