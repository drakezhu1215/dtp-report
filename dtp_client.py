import requests
import urllib3
from typing import Optional

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


_DEFAULT_API_VERSION = "v1.12"


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
    ) -> dict:
        params = {"buildId": build_id, "filterId": filter_id}
        if group_by:
            params["groupBy"] = group_by
        return self._get("/staticAnalysisViolations/count", params=params)
