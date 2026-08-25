from __future__ import annotations

import logging
from dataclasses import dataclass, field

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
    severity_breakdown: dict[str, int] = field(default_factory=dict)
    category_breakdown: dict[str, int] = field(default_factory=dict)


def collect_violations(
    client: DTPClient,
    project_names: list[str] | None = None,
) -> list[ProjectViolations]:
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
            pv = _collect_for_build(client, pname, fid, f.get("name", ""), build_id)
            results.append(pv)

    return results


def _collect_for_build(
    client: DTPClient,
    project_name: str,
    filter_id: int,
    filter_name: str,
    build_id: str,
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
