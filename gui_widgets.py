#!/usr/bin/env python3
"""DTP 报告生成器 —— 可复用的图形界面控件（gui_app 使用）。

1. :class:`DualListSelector`：左右双向列表选择器（"项目"与"开发者"两块共用）；
2. :class:`ProgressController`：进度条 + 百分比标签的创建与驱动。

本模块只 import ``tkinter``，不含业务逻辑（日志与系统集成工具在 ``gui_common``）。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk


class DualListSelector:
    """左右双向列表选择器（"项目"与"开发者"两块共用）。

    左边"可选"列表（带搜索框、可多选），中间是"添加 / 移除 / 全部添加 / 全部移除"，
    右边"已选"列表。各块的报告按钮用 :meth:`selected_items` 取当前选择。
    """

    def __init__(self, parent: "ttk.Frame", *, available_title: str, selected_title: str,
                 height: int = 11) -> None:
        self.all_items: list[str] = []
        self.selected: list[str] = []

        self.search_var = tk.StringVar()
        self.count_var = tk.StringVar(value="已选 0 / 0 项")

        frame = ttk.Frame(parent)
        self.frame = frame
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(2, weight=1)
        frame.rowconfigure(0, weight=1)

        # ---- 左：全部可选 ----
        left = ttk.LabelFrame(frame, text=available_title, padding=6)
        left.grid(row=0, column=0, sticky="nsew")
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)

        search_row = ttk.Frame(left)
        search_row.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        search_row.columnconfigure(1, weight=1)
        ttk.Label(search_row, text="搜索：").grid(row=0, column=0, sticky="w")
        ttk.Entry(search_row, textvariable=self.search_var).grid(row=0, column=1, sticky="ew")
        # 输入即过滤（不必按回车）
        self.search_var.trace_add("write", lambda *_: self.refresh())

        self.available_list = tk.Listbox(
            left, selectmode="extended", height=height, activestyle="none",
            exportselection=False,
        )
        self.available_list.grid(row=1, column=0, sticky="nsew")
        available_scroll = ttk.Scrollbar(left, orient="vertical", command=self.available_list.yview)
        available_scroll.grid(row=1, column=1, sticky="ns")
        self.available_list.configure(yscrollcommand=available_scroll.set)
        # 双击左侧 = 添加，双击右侧 = 移除（比来回点按钮顺手）
        self.available_list.bind("<Double-Button-1>", lambda _event: self.add_selected())

        # ---- 中：添加 / 移除 ----
        middle = ttk.Frame(frame)
        middle.grid(row=0, column=1, sticky="ns", padx=6)
        ttk.Button(middle, text="添加 →", command=self.add_selected, width=10).pack(pady=(0, 4))
        ttk.Button(middle, text="← 移除", command=self.remove_selected, width=10).pack(pady=4)
        ttk.Button(middle, text="全部添加", command=self.add_all, width=10).pack(pady=4)
        ttk.Button(middle, text="全部移除", command=self.remove_all, width=10).pack(pady=4)

        # ---- 右：已选 ----
        right = ttk.LabelFrame(frame, text=selected_title, padding=6)
        right.grid(row=0, column=2, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)

        self.selected_list = tk.Listbox(
            right, selectmode="extended", height=height, activestyle="none",
            exportselection=False,
        )
        self.selected_list.grid(row=0, column=0, sticky="nsew")
        selected_scroll = ttk.Scrollbar(right, orient="vertical", command=self.selected_list.yview)
        selected_scroll.grid(row=0, column=1, sticky="ns")
        self.selected_list.configure(yscrollcommand=selected_scroll.set)
        self.selected_list.bind("<Double-Button-1>", lambda _event: self.remove_selected())

        ttk.Label(frame, textvariable=self.count_var, foreground="#333").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(4, 0)
        )

    # ------------------------------------------------------------------
    # 数据
    # ------------------------------------------------------------------
    def set_items(self, items: list[str]) -> None:
        """重新设置"全部可选"列表（步骤 1 抓到数据后调用）；已选项里失效的会被丢掉。"""
        self.all_items = list(items)
        valid = set(self.all_items)
        self.selected = [name for name in self.selected if name in valid]
        self.refresh()

    def add_item(self, name: str) -> bool:
        """加一个候选项并选中它（"手工添加"用）；已存在时只保证它被选中。"""
        name = name.strip()
        if not name:
            return False
        if name not in self.all_items:
            self.all_items.append(name)
        if name not in self.selected:
            self.selected.append(name)
        self.refresh()
        return True

    def selected_items(self) -> list[str]:
        """当前的已选项（顺序 = 生成报告的先后顺序）。"""
        return list(self.selected)

    def refresh(self) -> None:
        """刷新左右两个列表（左侧隐藏已选项，并按搜索关键字过滤）。"""
        keyword = self.search_var.get().strip().lower()
        chosen = set(self.selected)

        self.available_list.delete(0, "end")
        for name in self.all_items:
            if name in chosen:
                continue
            if keyword and keyword not in name.lower():
                continue
            self.available_list.insert("end", name)

        self.selected_list.delete(0, "end")
        for name in self.selected:
            self.selected_list.insert("end", name)

        self.count_var.set(f"已选 {len(self.selected)} / {len(self.all_items)} 项")

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------
    def add_selected(self) -> None:
        for name in [self.available_list.get(i) for i in self.available_list.curselection()]:
            if name not in self.selected:
                self.selected.append(name)
        self.refresh()

    def remove_selected(self) -> None:
        for name in [self.selected_list.get(i) for i in self.selected_list.curselection()]:
            if name in self.selected:
                self.selected.remove(name)
        self.refresh()

    def add_all(self) -> None:
        """全部添加：只添加当前搜索条件下左侧可见的项（与界面显示保持一致）。"""
        for name in self.available_list.get(0, "end"):
            if name not in self.selected:
                self.selected.append(name)
        self.refresh()

    def remove_all(self) -> None:
        self.selected.clear()
        self.refresh()


class ProgressController:
    """进度条控制器：创建"进度条 + 百分比"两个控件，并负责驱动它们。

    进度条默认是 determinate 模式：worker 线程只抬高 :attr:`target`，主线程的心跳定时器
    把 :attr:`value` 平滑地推向 target，避免最慢的两步长时间停在同一个刻度上；
    生成报告时切到 indeterminate（来回滚动），避免看起来像卡住。
    """

    TICK_MS = 200

    def __init__(self, parent: "ttk.Frame", *, row: int = 0, column: int = 1,
                 length: int = 260) -> None:
        self.widget = ttk.Progressbar(parent, mode="determinate", maximum=100, length=length)
        self.widget.grid(row=row, column=column, sticky="w", padx=(10, 6))

        self.label = tk.StringVar(value="0%")
        ttk.Label(parent, textvariable=self.label, width=5).grid(
            row=row, column=column + 1, sticky="w"
        )
        # after()/after_cancel() 挂在顶层窗口上（控件所在窗口）
        self.root = parent.winfo_toplevel()

        self.value = 0.0
        self.target = 0.0
        self.job: "str | None" = None
        self.mode = "idle"  # idle / fetching / generating

    def reset(self) -> None:
        """回到 0%（重新开始一轮抓取）。"""
        self.value = 0.0
        self.target = 0.0
        self.widget.configure(mode="determinate", value=0)
        self.label.set("0%")

    def start_fetch(self) -> None:
        """开始抓取：从 0% 起走，并启动主线程心跳定时器。"""
        self.mode = "fetching"
        self.reset()
        if self.job is None:
            self.job = self.root.after(self.TICK_MS, self._tick)

    def advance(self, percent: float) -> None:
        """worker 线程上报的阶段进度：抬高目标值（进度只前进、不倒退）。"""
        self.target = max(self.target, float(percent))
        if self.target >= 100:
            self.set_complete()

    def set_complete(self) -> None:
        """直接显示 100%（数据全部就绪 / 阶段已经到头）。"""
        self.target = max(self.target, 100.0)
        self.value = 100.0
        self.widget.configure(value=100)
        self.label.set("100%")

    def stop(self) -> None:
        """停掉心跳定时器（窗口关闭、任务结束时调用）。"""
        if self.job is not None:
            self.root.after_cancel(self.job)
            self.job = None
        self.mode = "idle"

    def start_generating(self) -> None:
        """生成报告没有可量化的进度：进度条切成"来回滚动"。"""
        self.stop()
        self.widget.configure(mode="indeterminate")
        self.widget.start(30)

    def stop_generating(self) -> None:
        self.widget.stop()
        self.widget.configure(mode="determinate", value=self.value)
        self.label.set(f"{int(self.value)}%")

    def _tick(self) -> None:
        """主线程心跳：把 value 平滑地推向 target（到 100% 后停在 100%）。"""
        self.job = None
        if self.mode != "fetching":
            return
        if self.value < self.target:
            step = max(0.2, (self.target - self.value) * 0.04)
            self.value = min(self.target, self.value + step)
            self.widget.configure(value=self.value)
            self.label.set(f"{int(self.value)}%")
        self.job = self.root.after(self.TICK_MS, self._tick)
