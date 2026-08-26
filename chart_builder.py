from __future__ import annotations

import base64
import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from matplotlib import font_manager

from analyzer import ProjectViolations, SEVERITY_LABELS

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


def _plot_total_violations(active: list[ProjectViolations], title: str) -> str:
    names = [d.project_name for d in active]
    failed = [d.failed for d in active]
    suppressed = [d.suppressed for d in active]

    fig, ax = plt.subplots(figsize=(max(8, len(names) * 1.5), 6))
    x = range(len(names))
    width = 0.35
    bars_f = ax.bar([i - width / 2 for i in x], failed, width, label="Failed", color="#d32f2f")
    bars_s = ax.bar([i + width / 2 for i in x], suppressed, width, label="Suppressed", color="#ffa726")

    ax.set_xlabel("Project")
    ax.set_ylabel("Violation Count")
    ax.set_title(title)
    ax.set_xticks(list(x))
    ax.set_xticklabels(names, rotation=25, ha="right")
    ax.legend()
    ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{int(v):,}"))

    for bar in bars_f:
        h = bar.get_height()
        if h > 0:
            ax.annotate(f"{int(h):,}", xy=(bar.get_x() + bar.get_width() / 2, h),
                        xytext=(0, 3), textcoords="offset points", ha="center", fontsize=9)
    for bar in bars_s:
        h = bar.get_height()
        if h > 0:
            ax.annotate(f"{int(h):,}", xy=(bar.get_x() + bar.get_width() / 2, h),
                        xytext=(0, 3), textcoords="offset points", ha="center", fontsize=9)

    fig.tight_layout()
    return _fig_to_base64(fig)


def chart_total_violations(data: list[ProjectViolations], top_n_bad: int = 3) -> str:
    active = [d for d in data if d.has_data]
    if not active:
        return ""

    # 只显示问题数量最多的 top_n_bad 个项目（按未解决违规数 failed 降序取前 n 个）
    active = sorted(active, key=lambda d: d.failed, reverse=True)[:top_n_bad]
    return _plot_total_violations(
        active,
        f"Static Analysis Violations by Project (Top {len(active)} Worst)",
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
    )


def generate_all_charts(
    data: list[ProjectViolations],
    top_n_bad: int = 3,
    top_n_good: int = 3,
) -> dict:
    charts: dict = {}
    charts["total"] = chart_total_violations(data, top_n_bad=top_n_bad)
    charts["total_good"] = chart_total_violations_good(data, top_n_good=top_n_good)
    return charts
