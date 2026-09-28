from __future__ import annotations

import base64
import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from matplotlib import font_manager

from analyzer import BuildViolationPoint, ProjectViolations, RepoBuildTrend, SEVERITY_LABELS

SEVERITY_ORDER = ["1", "2", "3", "4", "5"]
SEVERITY_SHORT = {
    "1": "Sev1-Critical",
    "2": "Sev2-Major",
    "3": "Sev3-Moderate",
    "4": "Sev4-Minor",
    "5": "Sev5-Info",
}

# 配置中文字体，避免中文显示为方框
_CJK_FONT_CANDIDATES = [
    "Microsoft YaHei",   # 微软雅黑
    "SimHei",            # 黑体
    "SimSun",            # 宋体
    "KaiTi",             # 楷体
    "DengXian",          # 等线
    "Noto Sans CJK SC",
    "WenQuanYi Zen Hei",
    "AR PL UMing CN",
]

def _setup_cjk_font():
    available = {f.name for f in font_manager.fontManager.ttflist}
    for font_name in _CJK_FONT_CANDIDATES:
        if font_name in available:
            plt.rcParams["font.family"] = font_name
            plt.rcParams["axes.unicode_minus"] = False
            return
    # 没有找到中文字体时，采用字体回退方案
    plt.rcParams["font.sans-serif"] = [
        f for f in _CJK_FONT_CANDIDATES if f in available
    ] + plt.rcParams.get("font.sans-serif", ["DejaVu Sans"])
    plt.rcParams["axes.unicode_minus"] = False

_setup_cjk_font()

plt.rcParams.update({
    "font.size": 11,
    "axes.titlesize": 14,
    "axes.labelsize": 12,
    "figure.dpi": 150,
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.3,
})


def _fig_to_base64(fig: plt.Figure) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    buf.seek(0)
    img_b64 = base64.b64encode(buf.read()).decode("ascii")
    plt.close(fig)
    return img_b64


def _plot_total_violations(
    active: list[ProjectViolations],
    title: str,
    x_label: str = "Project",
    failed_color: str = "#d32f2f",
    suppressed_color: str = "#ffa726",
) -> str:
    names = [d.project_name for d in active]
    failed = [d.failed for d in active]
    suppressed = [d.suppressed for d in active]

    n = len(names)
    # 对象多的时候（例如一个项目下有十几个仓库）加宽加高画布，并缩小/倾斜 X 轴标签，
    # 否则仓库名会互相重叠、看不清
    fig, ax = plt.subplots(
        figsize=(max(8.0, min(n * 1.5, 30.0)), 6.0 if n <= 12 else 8.0)
    )
    x = range(len(names))
    width = 0.35
    bars_f = ax.bar([i - width / 2 for i in x], failed, width, label="Failed", color=failed_color)
    bars_s = ax.bar([i + width / 2 for i in x], suppressed, width, label="Suppressed", color=suppressed_color)

    ax.set_xlabel(x_label)
    ax.set_ylabel("Violation Count")
    ax.set_title(title)
    ax.set_xticks(list(x))
    label_size = 11 if n <= 8 else (9 if n <= 16 else 7)
    ax.set_xticklabels(names, rotation=25 if n <= 8 else 45, ha="right", fontsize=label_size)
    ax.legend()
    ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{int(v):,}"))

    value_size = 9 if n <= 16 else 7
    for bar in bars_f:
        h = bar.get_height()
        if h > 0:
            ax.annotate(f"{int(h):,}", xy=(bar.get_x() + bar.get_width() / 2, h),
                        xytext=(0, 3), textcoords="offset points", ha="center", fontsize=value_size)
    for bar in bars_s:
        h = bar.get_height()
        if h > 0:
            ax.annotate(f"{int(h):,}", xy=(bar.get_x() + bar.get_width() / 2, h),
                        xytext=(0, 3), textcoords="offset points", ha="center", fontsize=value_size)

    fig.tight_layout()
    return _fig_to_base64(fig)


def _plot_defect_density(
    active: list[ProjectViolations],
    title: str,
    color: str = "#d32f2f",
) -> str:
    """缺陷密度（每千行代码的违规数）条形图。

    与"违规数量对比图"的区别：这里画的是密度（``ProjectViolations.defect_density``），
    因此调用方**必须先过滤掉 ``code_lines`` 为 0 的条目**——否则"没有代码行数数据"
    会被画成"密度真的很低"，把榜单带偏。

    单位与表格口径一致：``违规数 × 1000 / 代码行数``（每千行代码违规数）。
    """
    names = [d.project_name for d in active]
    density = [d.defect_density for d in active]

    n = len(names)
    fig, ax = plt.subplots(figsize=(max(8.0, min(n * 1.5, 30.0)), 6.0))
    x = list(range(n))
    bars = ax.bar(x, density, 0.5, color=color)

    ax.set_xlabel("项目 (Project)")
    ax.set_ylabel("缺陷密度 (Violations per KLOC)")
    ax.set_title(title)
    ax.set_xticks(x)
    label_size = 11 if n <= 8 else (9 if n <= 16 else 7)
    ax.set_xticklabels(names, rotation=25 if n <= 8 else 45, ha="right", fontsize=label_size)
    ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{int(v):,}"))

    value_size = 9 if n <= 16 else 7
    for bar in bars:
        h = bar.get_height()
        if h > 0:
            ax.annotate(f"{int(h):,}", xy=(bar.get_x() + bar.get_width() / 2, h),
                        xytext=(0, 3), textcoords="offset points", ha="center", fontsize=value_size)

    fig.tight_layout()
    return _fig_to_base64(fig)


def chart_total_violations_bad(
    data: list[ProjectViolations],
    top_n_bad: int = 3,
    title: str | None = None,
    x_label: str | None = None,
) -> str:
    """违规数量的分组条形图。

    默认取违规最多的 ``top_n_bad`` 个对象；``top_n_bad <= 0`` 时不截断，画全部对象
    （项目报告 / 开发者报告用它显示各自范围内的所有仓库）。

    ``title`` / ``x_label`` 为 None 时用英文默认文案（公司级报告的跨项目对比图）；
    项目报告与开发者报告会传入中文标题与
    "仓库" 作为 X 轴名。
    """
    active = [d for d in data if d.has_data]
    if not active:
        return ""

    # 按未解决违规数 failed 降序排列；top_n_bad <= 0 表示"不截断，显示全部对象"
    # （项目报告第 4 章"项目违规图"里要列出该项目下的所有仓库）
    active = sorted(active, key=lambda d: d.failed, reverse=True)
    if top_n_bad > 0:
        active = active[:top_n_bad]
    return _plot_total_violations(
        active,
        title or f"Static Analysis Violations by Project (Top {len(active)} Worst)",
        x_label=x_label or "Project",
    )


def chart_total_violations_good(data: list[ProjectViolations], top_n_good: int = 3) -> str:
    active = [d for d in data if d.has_data]
    if not active:
        return ""

    # 只显示问题数量最少的 top_n_good 个项目（按未解决违规数 failed 升序取前 n 个）
    active = sorted(active, key=lambda d: d.failed)[:top_n_good]
    return _plot_total_violations(
        active,
        f"Static Analysis Violations by Project (Top {len(active)} Best)",
        failed_color="#1976d2",
        suppressed_color="#388e3c",
    )


def _density_candidates(data: list[ProjectViolations]) -> list[ProjectViolations]:
    """能参与缺陷密度排名的条目：有静态分析数据，且**有代码行数**。

    代码行数为 0（``with_code_lines`` 未开启或 DTP 该 build 未上报 metrics）时
    ``defect_density`` 也返回 0，但那不是"密度真的低"，只是没有数据；
    如果不排除，"缺陷密度最低的模范榜"会被一堆无数据条目占满。
    """
    return [d for d in data if d.has_data and d.code_lines > 0]


def chart_defect_density_bad(
    data: list[ProjectViolations],
    top_n_bad: int = 3,
    title: str | None = None,
) -> str:
    """缺陷密度**最高**的前 ``top_n_bad`` 个项目（公司报告第 5.1 章）。

    ``top_n_bad <= 0`` 时不截断；没有"有代码行数"的条目时返回空串
    （模板会显示"暂无可显示的数据图表。"）。
    """
    active = sorted(_density_candidates(data), key=lambda d: d.defect_density, reverse=True)
    if top_n_bad > 0:
        active = active[:top_n_bad]
    if not active:
        return ""

    return _plot_defect_density(
        active,
        title or f"缺陷密度最高的前 {len(active)} 个项目（每千行代码违规数）",
        color="#d32f2f",
    )


def chart_defect_density_good(
    data: list[ProjectViolations],
    top_n_good: int = 3,
    title: str | None = None,
) -> str:
    """缺陷密度**最低**的前 ``top_n_good`` 个项目（公司报告第 5.2 章）。

    取升序前 n 个；``top_n_good <= 0`` 时不截断；没有"有代码行数"的条目时返回空串。
    """
    active = sorted(_density_candidates(data), key=lambda d: d.defect_density)
    if top_n_good > 0:
        active = active[:top_n_good]
    if not active:
        return ""

    return _plot_defect_density(
        active,
        title or f"缺陷密度最低的前 {len(active)} 个项目（每千行代码违规数）",
        # "模范榜"用蓝色（#1976d2），与第 4.2 章"合规模范榜"的配色保持一致
        color="#1976d2",
    )


def generate_all_charts(
    data: list[ProjectViolations],
    top_n_bad: int = 3,
    top_n_good: int = 3,
    top_n_density_bad: int = 3,
    top_n_density_good: int = 3,
) -> dict:
    """公司报告（跨项目对比）用到的全部图表。

    - ``total_bad`` / ``total_good``：违规数量最多 / 最少的项目对比图（第 4 章）；
    - ``density_bad`` / ``density_good``：缺陷密度最高 / 最低的项目对比图（第 5 章）。
    """
    charts: dict = {}
    charts["total_bad"] = chart_total_violations_bad(data, top_n_bad=top_n_bad)
    charts["total_good"] = chart_total_violations_good(data, top_n_good=top_n_good)
    charts["density_bad"] = chart_defect_density_bad(data, top_n_bad=top_n_density_bad)
    charts["density_good"] = chart_defect_density_good(data, top_n_good=top_n_density_good)
    return charts


def _trend_points(trend: RepoBuildTrend) -> list[BuildViolationPoint]:
    """趋势图里真正参与绘制的数据点（只保留采集到违规数据的 build）。"""
    return [point for point in trend.points if point.has_data]


def _short_date(build_date: str) -> str:
    """把 DTP 的 ISO8601 时间（2026-09-11T09:02:29Z）截成日期部分。"""
    return build_date[:10] if len(build_date) >= 10 else build_date


def _plot_repo_trend(trend: RepoBuildTrend, owner_name: str | None = None) -> str:
    """单个仓库的违规趋势 X-Y 曲线图：X 轴是构建次序，Y 轴是该次构建的违规数量。

    ``owner_name``（开发者名）非空时写进标题，用于开发者报告——同一张图在公司级 /
    项目报告里是"该仓库整体的违规趋势"，在开发者报告里是"该作者在该仓库的违规趋势"，
    标题必须能区分开（不传该参数时保持原有标题）。
    """
    points = _trend_points(trend)
    if not points:
        return ""

    # X 轴：build-1 是最早的一次构建，build-N 是最新的一次
    labels = [f"build-{i}" for i in range(1, len(points) + 1)]
    x = list(range(len(points)))
    failed = [point.failed for point in points]
    suppressed = [point.suppressed for point in points]

    fig, ax = plt.subplots(figsize=(max(8, len(points) * 0.9), 5))
    ax.plot(x, failed, marker="o", markersize=5, linewidth=2.0, color="#d32f2f",
            label="违规数量 (Failed)")
    if any(suppressed):
        ax.plot(x, suppressed, marker="s", markersize=4, linewidth=1.4, linestyle="--",
                color="#ffa726", label="已抑制 (Suppressed)")

    first_date = _short_date(points[0].build_date)
    last_date = _short_date(points[-1].build_date)
    date_range = f"（{first_date} ~ {last_date}）" if first_date and last_date else ""
    ax.set_title(
        f"{trend.repo_name} 违规趋势（{owner_name}）{date_range}" if owner_name
        else f"{trend.repo_name} 违规趋势{date_range}"
    )
    ax.set_xlabel("构建次序 (Build)")
    ax.set_ylabel("违规数量 (Violation Count)")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=45 if len(labels) > 6 else 0,
                       ha="right" if len(labels) > 6 else "center")
    ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{int(v):,}"))
    ax.grid(True, linestyle=":", color="#cccccc", alpha=0.9)
    ax.legend()

    for xi, value in zip(x, failed):
        ax.annotate(
            f"{value:,}",
            xy=(xi, value),
            xytext=(0, 6),
            textcoords="offset points",
            ha="center",
            fontsize=8,
            color="#d32f2f",
        )

    fig.tight_layout()
    return _fig_to_base64(fig)


def chart_repo_trends(trends: list[RepoBuildTrend], owner_name: str | None = None) -> list[dict]:
    """为每个仓库生成一张违规趋势图（有几个仓库就有几张图）。

    返回 ``[{"name": 仓库名, "image": base64, "build_count": 构建次数,
    "note": build 对应关系}, ...]``；仓库没有可用的构建数据时该仓库不出图
    （不会产生只有坐标轴的空图）。

    ``owner_name``（开发者名）可选：非空时画进图标题，供开发者报告
    （report_author.py）区分"某开发者在某仓库的趋势"与项目报告里的整仓趋势。
    """
    charts: list[dict] = []

    for trend in trends:
        points = _trend_points(trend)
        image = _plot_repo_trend(trend, owner_name)
        if not image:
            continue

        charts.append(
            {
                "name": trend.repo_name,
                "image": image,
                "build_count": len(points),
                # 报告里列出 build-N 与 DTP build id 的对应关系，方便回溯原始数据
                "note": "构建对应关系：" + "，".join(
                    f"build-{i} = {point.build_id}"
                    for i, point in enumerate(points, 1)
                ),
            }
        )

    return charts
