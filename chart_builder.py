from __future__ import annotations

import base64
import io
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from matplotlib import font_manager

from analyzer import ProjectViolations, SEVERITY_LABELS

SEVERITY_ORDER = ["1", "2", "3", "4", "5"]
SEVERITY_COLORS = {
    "1": "#d32f2f",
    "2": "#f57c00",
    "3": "#fbc02d",
    "4": "#1976d2",
    "5": "#388e3c",
}
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


def chart_total_violations(data: list[ProjectViolations]) -> str:
    active = [d for d in data if d.has_data]
    if not active:
        return ""

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
    ax.set_title("Static Analysis Violations by Project")
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


def chart_severity_stacked(data: list[ProjectViolations]) -> str:
    active = [d for d in data if d.has_data and d.severity_breakdown]
    if not active:
        return ""

    names = [d.project_name for d in active]
    all_sevs = set()
    for d in active:
        all_sevs.update(d.severity_breakdown.keys())
    ordered_sevs = [s for s in SEVERITY_ORDER if s in all_sevs]
    extra_sevs = sorted(all_sevs - set(SEVERITY_ORDER))
    ordered_sevs.extend(extra_sevs)

    fig, ax = plt.subplots(figsize=(max(8, len(names) * 1.5), 6))
    x = range(len(names))
    bottoms = [0] * len(names)

    for sev in ordered_sevs:
        values = [d.severity_breakdown.get(sev, 0) for d in active]
        label = SEVERITY_SHORT.get(sev, sev)
        color = SEVERITY_COLORS.get(sev, None)
        ax.bar(x, values, bottom=bottoms, label=label, color=color)
        for i, v in enumerate(values):
            if v > 0:
                ax.text(i, bottoms[i] + v / 2, f"{v:,}", ha="center", va="center", fontsize=8, color="white", fontweight="bold")
        bottoms = [b + v for b, v in zip(bottoms, values)]

    ax.set_xlabel("Project")
    ax.set_ylabel("Violation Count")
    ax.set_title("Violations by Severity Level (Stacked)")
    ax.set_xticks(list(x))
    ax.set_xticklabels(names, rotation=25, ha="right")
    ax.legend(loc="upper right", fontsize=9)
    ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{int(v):,}"))
    fig.tight_layout()
    return _fig_to_base64(fig)


def chart_severity_percentage(data: list[ProjectViolations]) -> str:
    active = [d for d in data if d.has_data and d.severity_breakdown and d.failed > 0]
    if not active:
        return ""

    names = [d.project_name for d in active]
    all_sevs = set()
    for d in active:
        all_sevs.update(d.severity_breakdown.keys())
    ordered_sevs = [s for s in SEVERITY_ORDER if s in all_sevs]
    extra_sevs = sorted(all_sevs - set(SEVERITY_ORDER))
    ordered_sevs.extend(extra_sevs)

    fig, ax = plt.subplots(figsize=(max(8, len(names) * 1.5), 6))
    x = range(len(names))
    bottoms = [0.0] * len(names)

    for sev in ordered_sevs:
        raw = [d.severity_breakdown.get(sev, 0) for d in active]
        totals = [d.failed for d in active]
        pcts = [r / t * 100 if t > 0 else 0 for r, t in zip(raw, totals)]
        label = SEVERITY_SHORT.get(sev, sev)
        color = SEVERITY_COLORS.get(sev, None)
        ax.bar(x, pcts, bottom=bottoms, label=label, color=color)
        for i, p in enumerate(pcts):
            if p > 5:
                ax.text(i, bottoms[i] + p / 2, f"{p:.1f}%", ha="center", va="center", fontsize=8, color="white", fontweight="bold")
        bottoms = [b + p for b, p in zip(bottoms, pcts)]

    ax.set_xlabel("Project")
    ax.set_ylabel("Percentage (%)")
    ax.set_title("Severity Distribution (100% Stacked)")
    ax.set_xticks(list(x))
    ax.set_xticklabels(names, rotation=25, ha="right")
    ax.legend(loc="upper right", fontsize=9, bbox_to_anchor=(1.25, 1))
    ax.set_ylim(0, 100)
    fig.tight_layout()
    return _fig_to_base64(fig)


def chart_category_top_n(data: list[ProjectViolations], top_n: int = 15) -> str:
    active = [d for d in data if d.has_data and d.category_breakdown]
    if not active:
        return ""

    cat_totals: dict[str, int] = defaultdict(int)
    for d in active:
        for cat, cnt in d.category_breakdown.items():
            cat_totals[cat] += cnt

    top_cats = sorted(cat_totals, key=cat_totals.get, reverse=True)[:top_n]
    names = [d.project_name for d in active]

    fig, ax = plt.subplots(figsize=(max(10, top_n * 0.6), max(6, len(names) * 1.2)))
    y = range(len(top_cats))
    height = 0.8 / len(active)

    cmap = plt.cm.Set2
    colors = [cmap(i / max(len(active) - 1, 1)) for i in range(len(active))]

    for idx, d in enumerate(active):
        values = [d.category_breakdown.get(cat, 0) for cat in top_cats]
        offsets = [i + idx * height for i in y]
        ax.barh(offsets, values, height=height, label=d.project_name, color=colors[idx])

    ax.set_xlabel("Violation Count")
    ax.set_ylabel("Rule Category")
    ax.set_title(f"Top {top_n} Rule Categories by Project")
    ax.set_yticks([i + height * (len(active) - 1) / 2 for i in y])
    ax.set_yticklabels(top_cats, fontsize=9)
    ax.legend(loc="lower right", fontsize=9)
    ax.xaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{int(v):,}"))
    fig.tight_layout()
    return _fig_to_base64(fig)


def chart_severity_per_project_pie(data: list[ProjectViolations]) -> list[tuple[str, str]]:
    active = [d for d in data if d.has_data and d.severity_breakdown and d.failed > 0]
    if not active:
        return []

    results: list[tuple[str, str]] = []
    for d in active:
        labels = []
        sizes = []
        colors = []
        for sev in SEVERITY_ORDER:
            cnt = d.severity_breakdown.get(sev, 0)
            if cnt > 0:
                labels.append(SEVERITY_SHORT.get(sev, sev))
                sizes.append(cnt)
                colors.append(SEVERITY_COLORS.get(sev, "#999999"))

        if not sizes:
            continue

        fig, ax = plt.subplots(figsize=(5, 5))
        wedges, texts, autotexts = ax.pie(
            sizes, labels=labels, colors=colors, autopct="%1.1f%%",
            startangle=90, pctdistance=0.75, textprops={"fontsize": 9},
        )
        for t in autotexts:
            t.set_fontsize(8)
        ax.set_title(f"{d.project_name}\nSeverity Distribution", fontsize=12)
        fig.tight_layout()
        img = _fig_to_base64(fig)
        results.append((d.project_name, img))

    return results


def generate_all_charts(data: list[ProjectViolations], top_n: int = 15) -> dict:
    charts: dict = {}
    charts["total"] = chart_total_violations(data)
    charts["severity_stacked"] = chart_severity_stacked(data)
    charts["severity_pct"] = chart_severity_percentage(data)
    charts["category_top_n"] = chart_category_top_n(data, top_n)
    charts["severity_pies"] = chart_severity_per_project_pie(data)
    return charts
