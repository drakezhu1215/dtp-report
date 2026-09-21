import requests
import urllib3
from typing import Optional

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


_DEFAULT_API_VERSION = "v1.12"

# "代码行数"取用的 DTP metrics 指标 id，按优先级依次尝试（取第一个有数据的）：
#   METRIC.NOPLIF   Physical Lines in Files   文件物理行数（含空行/注释）
#   METRIC.NOSLIF   Source Lines in Files     文件源代码行数
#   METRIC.NOLLOCIF Logical Lines in Files    文件逻辑行数
# 实测本环境每个 build 只上报 METRIC.NOPLIF（另 3 个指标 hasData=false），
# 故 NOPLIF 为主选，其余仅作兼容回退。
_CODE_LINES_METRIC_IDS = ("METRIC.NOPLIF", "METRIC.NOSLIF", "METRIC.NOLLOCIF")

# 开发者（author）维度查询时默认取的违规状态：未解决（failed）+ 已抑制（suppressed）。
# DTP 的 count 接口默认只算 failed，不传 statuses 时返回体里连 suppressed 字段都没有。
_DEFAULT_STATUSES = "failed,suppressed"


class DTPClient:
    def __init__(self, url: str, username: str, password: str):
        self.base_url = f"{url.rstrip('/')}/grs/api/{_DEFAULT_API_VERSION}"
        self.session = requests.Session()
        self.session.auth = (username, password)
        self.session.headers.update({"Content-Type": "application/json"})
        self.session.verify = False

    def _get(self, path: str, params: Optional[dict] = None) -> dict | list:
        url = f"{self.base_url}{path}"
        resp = self.session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_projects(self) -> list[dict]:
        return self._get("/projects")

    def get_filters(self, project_id: Optional[int] = None) -> list[dict]:
        params = {}
        if project_id is not None:
            params["projectId"] = project_id
        return self._get("/filters", params=params)

    def get_builds(self, filter_id: int, limit: int = 1) -> list[dict]:
        return self._get("/builds", params={"filterId": filter_id, "limit": limit})

    def get_violation_count(
        self,
        build_id: str,
        filter_id: int,
        group_by: Optional[str] = None,
        author: Optional[str | list[str]] = None,
        statuses: Optional[str] = None,
    ) -> dict:
        """违规计数接口（``GET /staticAnalysisViolations/count``）。

        ``group_by`` 取 ``severity`` / ``category`` / ``resource`` / ``rule`` /
        ``author`` 等（见 DTP swagger 的 ``groupBy`` 枚举）。

        ``author`` 为 DTP 的 ``author`` 查询参数：**只统计该作者（们）的违规**
        （作者名形如 ``FC_MARMO\\marmo``、``hendra``）。传 list 时 requests 会展开成
        重复参数 ``author=a&author=b``，即"命中任意一个作者"。

        ``statuses`` 是 DTP 的违规状态过滤：``failed``（默认）/ ``suppressed``，
        也可以 ``"failed,suppressed"`` 一起取——DTP 不给这个参数时只算 failed，
        返回体里也就没有 ``suppressed`` 字段，这正是报告里"抑制"列一直是 0 的原因。
        """
        params: dict = {"buildId": build_id, "filterId": filter_id}
        if group_by:
            params["groupBy"] = group_by
        if author:
            params["author"] = author
        if statuses:
            params["statuses"] = statuses
        return self._get("/staticAnalysisViolations/count", params=params)

    def get_author_counts(
        self,
        build_id: str,
        filter_id: int,
        statuses: str = _DEFAULT_STATUSES,
    ) -> tuple[bool, dict[str, dict[str, int]]]:
        """按作者分组的违规计数：``{作者名: {"failed": n, "suppressed": n}}``。

        实测返回体（v1.12）::

            GET /staticAnalysisViolations/count?buildId=..&filterId=..
                &groupBy=author&statuses=failed,suppressed

            {"build": {"id": "..."}, "hasData": true, "failed": 70496, "suppressed": 77,
             "groupBy": {"type": "author", "count": 10, "groups": [
                 {"person": {"name": "FC_MARMO\\\\marmo"}, "failed": 847, "suppressed": 0},
                 ...]}}

        - 返回值第一个元素是 ``hasData``（该 build 有没有静态分析数据）；
        - DTP 只返回"有违规/有抑制"的作者，某个作者在这次构建里一个违规都没有时
          不会出现在分组里（调用方据此把缺失点当作 0，见 author_analyzer）；
        - ``limit`` / ``offset`` 对该接口无效，一次请求就能拿到全部作者分组。
        """
        resp = self.get_violation_count(
            build_id=build_id,
            filter_id=filter_id,
            group_by="author",
            statuses=statuses,
        )

        result: dict[str, dict[str, int]] = {}
        for group in ((resp.get("groupBy") or {}).get("groups") or []):
            person = group.get("person") or {}
            name = str(person.get("name") or "").strip()
            if not name:
                continue
            result[name] = {
                "failed": int(group.get("failed") or 0),
                "suppressed": int(group.get("suppressed") or 0),
            }

        return bool(resp.get("hasData", False)), result

    def get_author_violation_count(
        self,
        build_id: str,
        filter_id: int,
        author: str,
        statuses: str = _DEFAULT_STATUSES,
    ) -> tuple[bool, dict[str, int]]:
        """单个作者在该 build 上的违规计数：``(hasData, {"failed": n, "suppressed": n})``。

        用于"手工添加的开发者"：这类作者不在本地缓存的作者索引里，需要按仓库逐个
        用 ``author=`` 参数查询才能知道它跟哪些仓库相关。
        """
        resp = self.get_violation_count(
            build_id=build_id,
            filter_id=filter_id,
            author=author,
            statuses=statuses,
        )
        return bool(resp.get("hasData", False)), {
            "failed": int(resp.get("failed") or 0),
            "suppressed": int(resp.get("suppressed") or 0),
        }

    def get_severity_counts(
        self,
        build_id: str,
        filter_id: int,
        author: Optional[str] = None,
        statuses: Optional[str] = None,
    ) -> dict[str, int]:
        """按严重级别分组的违规计数：``{"1": 285, "2": 19401, ...}``。

        ``author`` 非空时只统计该作者的违规（``groupBy=severity&author=..``），
        即"某个开发者在一个仓库的最后一次构建上的严重级别分布"。
        """
        resp = self.get_violation_count(
            build_id=build_id,
            filter_id=filter_id,
            group_by="severity",
            author=author,
            statuses=statuses,
        )

        breakdown: dict[str, int] = {}
        for group in ((resp.get("groupBy") or {}).get("groups") or []):
            severity = group.get("severity") or {}
            name = str(severity.get("name") or "").strip()
            if not name:
                continue
            breakdown[name] = int(group.get("failed") or 0)

        return breakdown

    def get_file_count(self, build_id: str, filter_id: int) -> int:
        """获取该 build + filter 下 DTP 扫描的**所有文件数**（含无违规文件）。

        主接口（实测可用）::

            GET /staticAnalysisFiles?buildId=...&filterId=...

        返回体形如::

            {"total": 363,
             "passed": {"count": 3, "resources": [...]},   # 无违规文件
             "failed": {"count": 360, "resources": [...]}} # 有违规文件

        其中 ``total`` 即扫描文件总数；``limit``/``offset``/``groupBy`` 等参数对
        该接口无效（始终返回全部资源），所以一次请求即可。

        若该接口不可用（老版本 DTP），回退到按 resource 分组统计
        （``/staticAnalysisViolations/count?groupBy=resource`` → ``groupBy.count``），
        注意回退值只统计"有违规的文件"，会小于扫描文件总数。
        """
        try:
            resp = self._get("/staticAnalysisFiles", params={"buildId": build_id, "filterId": filter_id})
        except Exception:
            return self._get_file_count_by_resource(build_id, filter_id)

        total = resp.get("total")
        if total is not None:
            return int(total)

        passed = (resp.get("passed") or {}).get("count") or 0
        failed = (resp.get("failed") or {}).get("count") or 0
        if passed or failed:
            return int(passed) + int(failed)

        return self._get_file_count_by_resource(build_id, filter_id)

    def _get_file_count_by_resource(self, build_id: str, filter_id: int) -> int:
        """回退方案：按 resource（源文件）分组统计，得到"有静态分析违规的文件数"。"""
        resp = self.get_violation_count(build_id=build_id, filter_id=filter_id, group_by="resource")
        group_by = resp.get("groupBy") or {}
        count = group_by.get("count")
        if count is None:
            count = len(group_by.get("groups") or [])
        return int(count or 0)

    def get_code_lines(self, build_id: str, filter_id: int) -> int:
        """获取该 build + filter 下的**代码行数**合计（DTP metrics 指标聚合值）。

        接口（实测可用）::

            GET /metrics/METRIC.NOPLIF?buildId=...&filterId=...

        返回体形如::

            {"metric": {"id": "METRIC.NOPLIF", "name": "Physical Lines in Files"},
             "hasData": true, "isEnabled": true,
             "build": {"id": "spln_3p_application-2026-09-11", "hasDetails": true},
             "count": 1,
             "values": [{"avg": 270.05, "min": 20.0, "max": 4578.0, "sum": 97759.0}]}

        ``values[0]["sum"]`` 就是该 build 下各文件指标值之和，即"代码行数"
        （已用 ``POST /metrics/table`` 的 level=resource 逐文件求和校验一致）。
        一次请求即可，与 :meth:`get_file_count` 同款。

        指标未上报/未启用（``hasData`` 为 false）时按
        :data:`_CODE_LINES_METRIC_IDS` 的顺序尝试下一个指标；全部拿不到时返回 0
        （报告"代码行数"列会显示"无数据"），不会抛异常。
        """
        for metric_id in _CODE_LINES_METRIC_IDS:
            try:
                resp = self._get(
                    f"/metrics/{metric_id}",
                    params={"buildId": build_id, "filterId": filter_id},
                )
            except Exception:
                continue

            if not isinstance(resp, dict) or not resp.get("hasData"):
                continue

            values = resp.get("values") or []
            if not values:
                continue

            total = values[0].get("sum")
            if total is None:
                # 极少见：values 里直接是每个资源的明细值
                total = sum(float(v.get("value") or 0) for v in values)
            return int(round(float(total)))

        return 0
