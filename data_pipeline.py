#!/usr/bin/env python3
"""DTP 报告生成器 —— 数据抓取层（步骤 1：连一次 DTP，抓完三类报告要用的数据）。

本模块只负责"抓什么、按什么顺序抓、进度怎么报"：真正的 DTP 访问在 ``dtp_client`` /
``analyzer`` / ``analyzer_author``，配置解析与输出目录准备在 ``report_author``。

抓取顺序（每一步都是下一步的基础）：

1. :func:`analyzer.collect_violations`：仓库级违规数 + 文件数 + 代码行数（每个仓库最近一次构建）；
2. :func:`analyzer_author.collect_author_index`：DTP 全部作者 + 每个仓库每次构建的作者分布；
3. :func:`analyzer.collect_build_trends`：每个仓库每次构建的违规数（项目报告趋势图用）。

口径：**趋势数据默认取全部构建，其它数据一律只取每个仓库最近一次构建。**
结果统一装进 :class:`DataBundle`，三类报告共用这一份快照，不再重复访问 DTP。

依赖方向：本模块 -> dtp_client / analyzer / analyzer_author / report_author / project_merge；
上层（``main`` / ``report_service`` / ``gui_app``）依赖本模块，反向依赖不存在。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from analyzer import (
    ProjectViolations,
    RepoBuildTrend,
    collect_build_trends,
    collect_violations,
)
from analyzer_author import AuthorIndex, collect_author_index
from dtp_client import DTPClient
from project_merge import group_project_violations, unique_repo_violations
from report_author import (
    prepare_output_dir,
    resolve_repo_names,
    resolve_settings,
    trend_max_builds,
    warn_missing_repos,
)

logger = logging.getLogger(__name__)


@dataclass
class DataBundle:
    """步骤 1 抓到的全部数据：三类报告共用同一份快照，不再重复访问 DTP。"""

    settings: dict
    client: DTPClient
    output_dir: Path
    data: list[ProjectViolations]
    index: AuthorIndex
    trends_by_repo: dict[str, RepoBuildTrend]

    @property
    def merge_projects(self) -> dict:
        """config.yaml 的 ``merge_projects``：项目名 -> 仓库列表。"""
        return self.settings["merge_projects"]

    @property
    def report_cfg(self) -> dict:
        return self.settings["report_cfg"]

    @property
    def url(self) -> str:
        return self.settings["url"]

    @property
    def has_data(self) -> bool:
        """是否抓到可用的仓库数据（界面用它决定报告按钮能否点击）。"""
        return any(item.has_data for item in self.data)


# ---------------------------------------------------------------------------
# 步骤 1：连接 DTP，抓取三类报告需要的全部数据
# ---------------------------------------------------------------------------
def fetch_all_data(
    config_path: str = "config.yaml",
    *,
    url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    output_dir: str | None = None,
    trend_builds: int | None = None,
    max_workers: int | None = None,
    source_projects: list[str] | None = None,
    project_keys: list[str] | None = None,
    with_trends: bool = True,
    progress: Callable[[float, str], None] | None = None,
) -> DataBundle:
    """自动连接 DTP，一次性抓完三类报告需要的全部数据。

    抓取顺序（每一步都是下一步的基础）：

    1. ``collect_violations``：仓库级违规数 + 文件数 + 代码行数，全部取自每个仓库
       **最近一次构建**（公司报告 / 项目报告的基础；开发者报告也需要它，因为作者索引
       挂在仓库信息上）；
    2. ``collect_author_index``：DTP 全部作者 + 每个仓库**每次构建**的作者分布
       （开发者报告用；这是**最慢**的一步，因为每个仓库的每次构建都要请求一次）；
    3. ``collect_build_trends``：每个仓库**每次构建**的违规数（项目报告趋势图用）。

    口径：**趋势数据（每个仓库每次构建的违规数，含按作者拆分的部分）默认取全部构建；
    其它数据一律只取每个仓库最近一次构建。** 构建特别多时可用 ``trend_builds`` 限制成
    "最近 N 次"，语义见 :func:`report_author.trend_max_builds`。

    :param progress: ``progress(percent, message)`` 回调，percent 为 0~100，界面用它驱动进度条；
        为 ``None`` 时只写日志（命令行模式）。
    :param with_trends: 是否需要**仓库级**构建趋势数据（只有项目报告用；只出公司报告 /
        只列开发者清单时传 ``False`` 可以少抓一轮。开发者报告的趋势来自
        ``collect_author_index`` 的缓存，不受这个参数影响）。

    报告**根目录**（config.yaml 的 ``report.output_dir``）会在这里清空并重建一次
    （每次运行只保留本次生成的报告）；三类报告分别写入其下的 ``company/`` /
    ``project/`` / ``author/`` 子目录，互相不会覆盖，见 :func:`report_out_dir`。
    """
    def report(percent: float, message: str) -> None:
        """上报进度：先写日志，再通知界面（界面回调只是 post 到主线程，不会阻塞抓取）。"""
        logger.info("[%3d%%] %s", int(percent), message)
        if progress is not None:
            progress(percent, message)

    report(0, f"读取配置文件：{Path(config_path).resolve()}")
    settings = resolve_settings(
        config_path,
        url=url, username=username, password=password,
        output_dir=output_dir, trend_builds=trend_builds, max_workers=max_workers,
    )
    repo_names = resolve_repo_names(
        settings["report_cfg"], settings["merge_projects"],
        explicit=source_projects, project_keys=project_keys,
    )
    if not repo_names:
        # 解析不出来时 analyzer.collect_violations 会扫描 DTP 全部项目（很慢）
        logger.warning("没有解析出任何仓库名，将扫描 DTP 上的全部项目（很慢，容易超时）。")

    out = prepare_output_dir(settings["output_dir"])

    report(5, f"连接 DTP：{settings['url']}（用户 {settings['username']}）")
    client = DTPClient(
        url=settings["url"], username=settings["username"], password=settings["password"],
    )
    # get_projects() 是最轻的接口，用它确认 URL / 账号密码能通（失败会抛异常给调用方）
    dtp_projects = client.get_projects()
    logger.info(
        "DTP 连接成功：服务器上有 %d 个项目，本次扫描 %d 个仓库",
        len(dtp_projects), len(repo_names),
    )

    report(12, f"抓取仓库数据（{len(repo_names)} 个仓库最近一次构建：违规数 / 文件数 / 代码行数）...")
    data = collect_violations(
        client, project_names=repo_names, with_file_count=True, with_code_lines=True,
    )
    warn_missing_repos(data, repo_names)
    logger.info(
        "仓库数据完成：%d 条仓库记录，其中 %d 条有静态分析数据",
        len(data), len([d for d in data if d.has_data]),
    )

    # 趋势数据口径：trend_builds = 0 -> 全部构建（max_builds=None）；>0 -> 最近 N 次；
    # <0 -> 不抓趋势。scope 只用于日志/进度文案。
    trend_builds = int(settings["trend_builds"])
    max_builds = trend_max_builds(settings)
    if max_builds is None:
        scope = "全部构建"
    elif max_builds > 0:
        scope = f"最近 {max_builds} 次构建"
    else:
        scope = "最新一次构建（不抓趋势）"

    report(
        55,
        f"抓取开发者索引（每个仓库{scope}的作者分布，"
        f"并发 {settings['max_workers']} 线程）...",
    )
    index = collect_author_index(
        client, data,
        max_builds=max_builds, max_workers=settings["max_workers"],
    )
    logger.info("开发者索引完成：%d 位开发者，%d 个仓库", len(index.authors), len(index.repos))

    trends: dict[str, RepoBuildTrend] = {}
    if with_trends and trend_builds >= 0:
        report(88, f"抓取构建趋势（每个仓库{scope}的违规数）...")
        trend_repos = unique_repo_violations(
            group_project_violations(data, settings["merge_projects"])
        )
        trends = collect_build_trends(
            client, trend_repos,
            max_builds=max_builds, max_workers=settings["max_workers"],
        )
        logger.info(
            "构建趋势完成：%d 个仓库，其中 %d 个取到了趋势点",
            len(trends), len([t for t in trends.values() if t.has_data]),
        )
    else:
        logger.warning("已跳过构建趋势数据的抓取：项目报告里不会有趋势图。")

    report(100, "数据获取完成")
    return DataBundle(
        settings=settings, client=client, output_dir=out,
        data=data, index=index, trends_by_repo=trends,
    )
