#!/usr/bin/env python3
"""DTP 开发者代码核查报告生成器 —— 图形界面（tkinter，标准库自带）。

界面元素（对应需求）：

1. "获取开发者列表"：后台线程调用 ``main_developer.build_author_index()``，从 DTP 抓取
   config.yaml 里全部仓库的作者（同时把"每个仓库最近 N 次构建的作者分布"缓存成
   开发者索引，之后生成报告不用再重复抓取）。抓取期间界面不会卡住，进度都写在日志框；
2. "开发者选择"（可多选、可增删）：
   - 左侧"可选开发者"列表（``selectmode=extended`` 多选）+ 搜索框（按名字过滤）；
   - 中间"添加 → / ← 移除 / 全部添加 / 全部移除"按钮；
   - 下方"手工添加"输入框：用于 DTP 作者列表里还没有的开发者（例如新同事、
     名字写法不同），这类开发者会走 ``author=`` 逐仓库查询的兜底路径；
   - 右侧"已选开发者"列表：**选择几个就生成几份报告**；
3. "生成报告"：后台线程为每个已选开发者输出一份 .html + .xlsx；
4. 日志框：logging 与 print/stderr 的输出实时显示在界面上。

启动方式：

    python main_developer.py      # 无参数 -> 自动打开本界面（report_v4_zh.html）
    python main_developer.py --gui
    python author_gui.py          # 直接运行本文件
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from author_analyzer import AuthorIndex
from main_developer import (
    DEFAULT_LOCALE,
    build_author_index,
    generate_reports,
    load_config,
)
from report_gui import QueueLogHandler, QueueStream, open_in_file_manager

# 日志框刷新间隔（毫秒）
_LOG_POLL_MS = 100

# 日志框最多保留的行数，超出后丢弃最早的若干行，避免长时间运行占用内存
_MAX_LOG_LINES = 5000
_TRIM_LOG_LINES = 1000


class AuthorReportApp:
    """开发者报告主窗口。

    界面分四块：配置信息（只读）、获取开发者列表（抓取选项）、开发者选择（双向列表）、
    操作区 + 日志框。
    """

    def __init__(
        self,
        root: tk.Tk,
        config_path: str = "config.yaml",
        locale: str | None = None,
    ) -> None:
        self.root = root
        self.config_path = config_path
        # 报告模板覆盖：None = 用 config.yaml 的 report.locale_developer；
        # main_developer.py 传 "v4_zh"（report_v4_zh.html，开发者报告）
        self.locale_override = locale

        # 后台线程 -> 主线程的消息队列，保证跨线程更新界面安全
        self.log_queue: "queue.Queue[str]" = queue.Queue()

        self.config: dict = {}
        self.output_dir = "./output"
        # 每个仓库抓取最近多少次构建（决定趋势图的数据点）
        self.trend_builds = 10
        # 并发请求线程数
        self.max_workers = 4

        # 开发者索引（"获取开发者列表"的产物，生成报告时复用）
        self.index: AuthorIndex | None = None
        # 抓索引时用的趋势构建次数（与界面上的 spinbox 不一致时需要重新抓）
        self.index_trend_builds: int | None = None
        # DTP 上全部开发者（索引里的顺序：违规多的在前）
        self.all_authors: list[str] = []
        # 已选开发者（顺序即报告生成顺序）
        self.selected_authors: list[str] = []

        self.worker: threading.Thread | None = None

        self._build_ui()
        self._install_logging()
        self._reload_config(initial=True)
        self._poll_log_queue()

    # ------------------------------------------------------------------
    # 界面搭建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.root.title("DTP 开发者代码核查报告生成器")
        self.root.geometry("1040x820")
        self.root.minsize(820, 620)

        style = ttk.Style(self.root)
        for theme in ("vista", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break

        container = ttk.Frame(self.root, padding=10)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        # 第 3 块（开发者选择）随窗口拉伸，日志框占剩余空间
        container.rowconfigure(2, weight=1)
        container.rowconfigure(4, weight=1)

        self._build_config_frame(container)
        self._build_fetch_frame(container)
        self._build_selection_frame(container)
        self._build_action_frame(container)
        self._build_log_frame(container)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_config_frame(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="配置信息（config.yaml）", padding=8)
        frame.grid(row=0, column=0, sticky="ew")
        frame.columnconfigure(1, weight=1)

        self.info_vars = {
            "config": tk.StringVar(value=str(Path(self.config_path).resolve())),
            "url": tk.StringVar(value="-"),
            "output": tk.StringVar(value="-"),
            "locale": tk.StringVar(value="-"),
        }

        rows = (
            ("配置文件", "config"),
            ("DTP 服务器", "url"),
            ("输出目录", "output"),
            ("报告模板", "locale"),
        )
        for row_index, (label, key) in enumerate(rows):
            ttk.Label(frame, text=f"{label}：").grid(row=row_index, column=0, sticky="w", padx=(0, 6))
            ttk.Label(frame, textvariable=self.info_vars[key]).grid(row=row_index, column=1, sticky="w")

        ttk.Button(frame, text="重新加载配置", command=self._reload_config).grid(
            row=0, column=2, rowspan=len(rows), sticky="e", padx=(10, 0)
        )

    def _build_fetch_frame(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="1. 开发者列表（从 DTP 抓取）", padding=8)
        frame.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        frame.columnconfigure(3, weight=1)

        self.fetch_button = ttk.Button(
            frame, text="获取开发者列表", command=self.on_fetch_authors, width=18
        )
        self.fetch_button.grid(row=0, column=0, sticky="w")

        # 抓取选项：趋势构建次数（决定趋势图的数据点）、并发线程数
        ttk.Label(frame, text="每个仓库取最近").grid(row=0, column=1, sticky="e", padx=(12, 4))
        self.trend_var = tk.StringVar(value=str(self.trend_builds))
        ttk.Spinbox(
            frame, from_=1, to=50, width=4, textvariable=self.trend_var
        ).grid(row=0, column=2, sticky="w")
        ttk.Label(frame, text="次构建").grid(row=0, column=3, sticky="w", padx=(4, 0))

        ttk.Label(frame, text="并发线程").grid(row=1, column=1, sticky="e", padx=(12, 4), pady=(6, 0))
        self.workers_var = tk.StringVar(value=str(self.max_workers))
        ttk.Spinbox(
            frame, from_=1, to=16, width=4, textvariable=self.workers_var
        ).grid(row=1, column=2, sticky="w", pady=(6, 0))

        self.fetch_status_var = tk.StringVar(value="尚未获取开发者列表")
        ttk.Label(frame, textvariable=self.fetch_status_var, foreground="#555").grid(
            row=1, column=3, sticky="w", padx=(4, 0), pady=(6, 0)
        )

    def _build_selection_frame(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(
            parent, text="2. 开发者选择（可多选：添加 / 移除 / 手工添加；选几个就生成几份报告）",
            padding=8,
        )
        frame.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(2, weight=1)
        frame.rowconfigure(4, weight=1)

        # 搜索框（按名字过滤左侧列表）
        ttk.Label(frame, text="搜索：").grid(row=0, column=0, sticky="w")
        self.search_var = tk.StringVar()
        search_entry = ttk.Entry(frame, textvariable=self.search_var)
        search_entry.grid(row=0, column=2, sticky="ew")
        self.search_var.trace_add("write", lambda *_: self.refresh_author_lists())

        # 手工添加开发者
        ttk.Label(frame, text="手工添加：").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.manual_var = tk.StringVar()
        manual_entry = ttk.Entry(frame, textvariable=self.manual_var)
        manual_entry.grid(row=1, column=2, sticky="ew", pady=(6, 0))
        manual_entry.bind("<Return>", lambda event: self.on_add_manual())

        ttk.Label(frame, text="可选开发者：").grid(row=3, column=0, sticky="w", pady=(6, 2))
        ttk.Label(frame, text="已选开发者：").grid(row=3, column=2, sticky="w", pady=(6, 2))

        # 左：可选开发者列表（多选）；右：已选开发者列表（多选）
        left = ttk.Frame(frame)
        left.grid(row=4, column=0, sticky="nsew")
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.available_list = tk.Listbox(left, selectmode="extended", activestyle="none", exportselection=False)
        self.available_list.grid(row=0, column=0, sticky="nsew")
        left_scroll = ttk.Scrollbar(left, orient="vertical", command=self.available_list.yview)
        left_scroll.grid(row=0, column=1, sticky="ns")
        self.available_list.configure(yscrollcommand=left_scroll.set)
        self.available_list.bind("<Double-Button-1>", lambda event: self.on_add_selected())

        right = ttk.Frame(frame)
        right.grid(row=4, column=2, sticky="nsew")
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        self.selected_list = tk.Listbox(right, selectmode="extended", activestyle="none", exportselection=False)
        self.selected_list.grid(row=0, column=0, sticky="nsew")
        right_scroll = ttk.Scrollbar(right, orient="vertical", command=self.selected_list.yview)
        right_scroll.grid(row=0, column=1, sticky="ns")
        self.selected_list.configure(yscrollcommand=right_scroll.set)
        self.selected_list.bind("<Double-Button-1>", lambda event: self.on_remove_selected())

        # 中：添加 / 移除 / 全部添加 / 全部移除
        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=1, sticky="ns", padx=10)
        ttk.Button(buttons, text="添加 →", command=self.on_add_selected, width=12).pack(pady=2)
        ttk.Button(buttons, text="← 移除", command=self.on_remove_selected, width=12).pack(pady=2)
        ttk.Button(buttons, text="全部添加", command=self.on_add_all, width=12).pack(pady=(12, 2))
        ttk.Button(buttons, text="全部移除", command=self.on_clear_selected, width=12).pack(pady=2)

        # 手工添加按钮 + 已选计数
        ttk.Button(frame, text="手工添加到已选", command=self.on_add_manual).grid(
            row=1, column=4, sticky="w", padx=(10, 0), pady=(6, 0)
        )
        self.selected_count_var = tk.StringVar(value="已选 0 位开发者")
        ttk.Label(frame, textvariable=self.selected_count_var).grid(
            row=3, column=4, sticky="w", padx=(10, 0)
        )

    def _build_action_frame(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="3. 生成报告", padding=8)
        frame.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        frame.columnconfigure(3, weight=1)

        self.generate_button = ttk.Button(
            frame, text="生成报告", command=self.on_generate, width=18
        )
        self.generate_button.grid(row=0, column=0, sticky="w")

        ttk.Button(frame, text="打开输出目录", command=self.on_open_output, width=14).grid(
            row=0, column=1, sticky="w", padx=(8, 0)
        )
        ttk.Button(frame, text="保存日志", command=self.on_save_log, width=12).grid(
            row=0, column=2, sticky="w", padx=(8, 0)
        )

        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(8, 0))

        self.status_var = tk.StringVar(value="就绪：先“获取开发者列表”，再添加要出报告的开发者。")
        ttk.Label(frame, textvariable=self.status_var, foreground="#333").grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(6, 0)
        )

    def _build_log_frame(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="日志", padding=8)
        frame.grid(row=4, column=0, sticky="nsew", pady=(8, 0))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(
            frame, height=12, wrap="none", state="disabled",
            background="#1e1e1e", foreground="#d4d4d4",
            insertbackground="#d4d4d4", font=("Consolas", 9),
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")

        y_scroll = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll = ttk.Scrollbar(frame, orient="horizontal", command=self.log_text.xview)
        x_scroll.grid(row=1, column=0, sticky="ew")
        self.log_text.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

    # ------------------------------------------------------------------
    # 日志：logging 与 print/stderr 全部进日志框
    # ------------------------------------------------------------------
    def _install_logging(self) -> None:
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        self.log_handler = QueueLogHandler(self.log_queue)
        root_logger.addHandler(self.log_handler)

        # print() 与三方库写到 stderr 的警告也一并显示（同时保留原来的输出）
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr
        sys.stdout = QueueStream(self.log_queue, "stdout", self._original_stdout)
        sys.stderr = QueueStream(self.log_queue, "stderr", self._original_stderr)

        self.log_queue.put(f"[系统] 日志已接入界面；配置文件：{Path(self.config_path).resolve()}")

    def append_log(self, message: str) -> None:
        """把一行日志追加到日志框末尾（仅在主线程调用）。"""
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        if int(self.log_text.index("end-1c").split(".")[0]) > _MAX_LOG_LINES:
            self.log_text.delete("1.0", f"{_TRIM_LOG_LINES + 1}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_log_queue(self) -> None:
        """定时把后台线程/日志处理器写入队列的内容搬到界面上。"""
        try:
            while True:
                self.append_log(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(_LOG_POLL_MS, self._poll_log_queue)

    def on_save_log(self) -> None:
        path = filedialog.asksaveasfilename(
            title="保存日志",
            defaultextension=".log",
            filetypes=[("日志文件", "*.log"), ("文本文件", "*.txt"), ("所有文件", "*.*")],
            initialfile="dtp_developer_report.log",
        )
        if not path:
            return
        content = self.log_text.get("1.0", "end-1c")
        Path(path).write_text(content, encoding="utf-8")
        self.log_queue.put(f"[系统] 日志已保存：{path}")

    # ------------------------------------------------------------------
    # 配置与开发者列表
    # ------------------------------------------------------------------
    def _reload_config(self, initial: bool = False) -> bool:
        """重新读取 config.yaml，刷新配置信息与抓取选项。"""
        try:
            self.config = load_config(self.config_path)
        except Exception as exc:  # 文件缺失 / yaml 解析失败
            self.log_queue.put(f"[错误] 读取配置文件失败：{exc}")
            if not initial:
                messagebox.showerror("配置错误", f"读取配置文件失败：{exc}")
            return False

        dtp_cfg = self.config.get("dtp", {}) or {}
        report_cfg = self.config.get("report", {}) or {}

        self.output_dir = str(report_cfg.get("output_dir", "./output"))
        self.trend_builds = int(report_cfg.get("trend_builds", 10))
        self.max_workers = max(1, int(report_cfg.get("max_workers", 4)))
        self.trend_var.set(str(self.trend_builds))
        self.workers_var.set(str(self.max_workers))

        self.info_vars["config"].set(str(Path(self.config_path).resolve()))
        self.info_vars["url"].set(str(dtp_cfg.get("url", "-")))
        self.info_vars["output"].set(str(Path(self.output_dir).resolve()))
        # 界面显示实际使用的模板：main_developer.py 传入的 locale 优先
        self.info_vars["locale"].set(
            str(self.locale_override or report_cfg.get("locale_developer", DEFAULT_LOCALE))
        )

        # 配置变了（仓库范围、项目列表都可能变）→ 之前抓的开发者索引作废
        if not initial and self.index is not None:
            self.index = None
            self.index_trend_builds = None
            self.fetch_status_var.set("配置已变更，请重新获取开发者列表")
            self.log_queue.put("[系统] 配置已重新加载，之前缓存的开发者数据已作废。")

        if not initial:
            self.log_queue.put("[系统] 配置已重新加载。")
        return True

    def _read_options(self) -> bool:
        """把界面上的"趋势构建次数/并发线程数"读进来（非法输入时保留原值并提示）。"""
        try:
            trend = int(str(self.trend_var.get()).strip())
            workers = int(str(self.workers_var.get()).strip())
        except ValueError:
            messagebox.showwarning("输入有误", "“次构建”和“并发线程”都必须是整数。")
            self.trend_var.set(str(self.trend_builds))
            self.workers_var.set(str(self.max_workers))
            return False

        if trend < 1 or workers < 1:
            messagebox.showwarning("输入有误", "“次构建”和“并发线程”都必须大于 0。")
            return False

        self.trend_builds = trend
        self.max_workers = workers
        return True

    def refresh_author_lists(self) -> None:
        """刷新"可选开发者 / 已选开发者"两个列表（可选列表里不再重复显示已选的）。"""
        keyword = self.search_var.get().strip().lower()

        self.available_list.delete(0, "end")
        for author in self.all_authors:
            if author in self.selected_authors:
                continue
            if keyword and keyword not in author.lower():
                continue
            self.available_list.insert("end", author)

        self.selected_list.delete(0, "end")
        for author in self.selected_authors:
            self.selected_list.insert("end", author)

        self.selected_count_var.set(f"已选 {len(self.selected_authors)} 位开发者")

    # ------------------------------------------------------------------
    # 开发者选择：添加 / 移除 / 全部添加 / 全部移除 / 手工添加
    # ------------------------------------------------------------------
    def on_add_selected(self) -> None:
        """把左侧选中的开发者移到"已选"列表。"""
        picked = [self.available_list.get(i) for i in self.available_list.curselection()]
        for author in picked:
            if author not in self.selected_authors:
                self.selected_authors.append(author)
        self.refresh_author_lists()
        if picked:
            self.log_queue.put(f"[选择] 已添加 {len(picked)} 位开发者。")

    def on_remove_selected(self) -> None:
        """把右侧选中的开发者从"已选"列表移除。"""
        picked = [self.selected_list.get(i) for i in self.selected_list.curselection()]
        for author in picked:
            if author in self.selected_authors:
                self.selected_authors.remove(author)
        self.refresh_author_lists()
        if picked:
            self.log_queue.put(f"[选择] 已移除 {len(picked)} 位开发者。")

    def on_add_all(self) -> None:
        """把左侧（当前搜索条件下）的全部开发者加入"已选"。"""
        available = list(self.available_list.get(0, "end"))
        for author in available:
            if author not in self.selected_authors:
                self.selected_authors.append(author)
        self.refresh_author_lists()
        self.log_queue.put(f"[选择] 已添加 {len(available)} 位开发者（共已选 {len(self.selected_authors)} 位）。")

    def on_clear_selected(self) -> None:
        """清空"已选"列表。"""
        removed = len(self.selected_authors)
        self.selected_authors.clear()
        self.refresh_author_lists()
        self.log_queue.put(f"[选择] 已移除全部 {removed} 位开发者。")

    def on_add_manual(self) -> None:
        """手工添加开发者（DTP 作者列表里没有的人也能出报告）。"""
        author = self.manual_var.get().strip()
        if not author:
            messagebox.showinfo("没有输入", "请先在“手工添加”输入框里填写开发者名（如 hendra）。")
            return
        if author not in self.all_authors:
            # 放进可选列表，方便之后移除；顺序保持不变（手工添加的排在最后）
            self.all_authors.append(author)
        if author not in self.selected_authors:
            self.selected_authors.append(author)
        self.manual_var.set("")
        self.refresh_author_lists()
        self.log_queue.put(f"[选择] 已手工添加开发者：{author}")

    # ------------------------------------------------------------------
    # 获取开发者列表（后台线程）
    # ------------------------------------------------------------------
    def on_fetch_authors(self) -> None:
        """点击"获取开发者列表"：后台抓取 DTP 上的全部开发者。"""
        if self.worker is not None and self.worker.is_alive():
            messagebox.showinfo("正在忙", "上一个任务还在进行中，请等它结束。")
            return
        if not self._read_options():
            return

        self._set_busy(True, "正在获取开发者列表（首次抓取需要几分钟，进度见日志框）...")
        self.worker = threading.Thread(target=self._run_fetch, daemon=True)
        self.worker.start()

    def _run_fetch(self) -> None:
        """后台线程：抓取开发者索引，结果回主线程处理。"""
        index: AuthorIndex | None = None
        error: str | None = None
        try:
            index = build_author_index(
                config_path=self.config_path,
                trend_builds=self.trend_builds,
                max_workers=self.max_workers,
            )
        except Exception as exc:  # 网络异常、DTP 认证失败等
            error = f"{type(exc).__name__}: {exc}"
            logging.getLogger(__name__).exception("获取开发者列表失败：%s", error)
        self.root.after(0, self._on_fetch_done, index, error)

    def _on_fetch_done(self, index: AuthorIndex | None, error: str | None) -> None:
        """主线程：恢复界面并刷新开发者列表。"""
        self._set_busy(False, "")

        if error:
            self.status_var.set("获取开发者列表失败，请查看日志框中的错误信息。")
            messagebox.showerror("获取失败", f"获取开发者列表失败：{error}\n\n详情见日志框。")
            return

        if index is None or not index.authors:
            self.fetch_status_var.set("未找到任何开发者")
            self.status_var.set("没有找到开发者：请检查 config.yaml 的仓库范围与 DTP 数据。")
            messagebox.showwarning("没有开发者", "没有从 DTP 找到任何开发者（相关仓库没有静态分析数据）。")
            return

        self.index = index
        self.index_trend_builds = self.trend_builds
        self.all_authors = list(index.authors)
        self.refresh_author_lists()

        self.fetch_status_var.set(f"已获取 {len(index.authors)} 位开发者")
        self.status_var.set(
            f"已获取 {len(index.authors)} 位开发者（趋势构建 {self.trend_builds} 次，"
            f"{len(index.repos)} 个仓库）。请添加要出报告的开发者后点“生成报告”。"
        )
        self.log_queue.put(f"[系统] 开发者列表已就绪：共 {len(index.authors)} 位开发者。")

    # ------------------------------------------------------------------
    # 生成报告（后台线程）
    # ------------------------------------------------------------------
    def on_generate(self) -> None:
        """点击"生成报告"：为已选的每个开发者生成一份报告。"""
        if self.worker is not None and self.worker.is_alive():
            messagebox.showinfo("正在忙", "报告还在生成中，请等它结束。")
            return
        if not self._read_options():
            return

        authors = list(self.selected_authors)
        if not authors:
            messagebox.showwarning(
                "未选择开发者",
                "请先“获取开发者列表”，然后从左侧列表添加至少一位开发者（或手工添加）。",
            )
            return

        if self.index is None:
            logging.getLogger(__name__).info(
                "尚未获取开发者列表，本次生成会先抓取开发者数据（较慢，请耐心等待）。"
            )
        elif self.index_trend_builds != self.trend_builds:
            logging.getLogger(__name__).warning(
                "趋势构建次数已从 %s 改为 %s，缓存的开发者数据作废，将重新抓取。",
                self.index_trend_builds, self.trend_builds,
            )
            self.index = None
            self.index_trend_builds = None

        self._set_busy(True, f"正在为 {len(authors)} 位开发者生成报告（进度见日志框）...")
        self.worker = threading.Thread(target=self._run_generation, args=(authors,), daemon=True)
        self.worker.start()

    def _run_generation(self, authors: list[str]) -> None:
        """后台线程：调用 main_developer.generate_reports，结果回主线程处理。"""
        reports: list[Path] = []
        error: str | None = None
        try:
            if self.index is None:
                # 没有缓存就先抓一遍（顺便把开发者列表刷新出来）
                self.index = build_author_index(
                    config_path=self.config_path,
                    trend_builds=self.trend_builds,
                    max_workers=self.max_workers,
                )
                self.index_trend_builds = self.trend_builds

            reports = generate_reports(
                config_path=self.config_path,
                selected_authors=authors,
                output_dir=self.output_dir,
                locale=self.locale_override,
                trend_builds=self.trend_builds,
                max_workers=self.max_workers,
                author_index=self.index,  # 复用缓存的开发者索引
            )
        except Exception as exc:  # 网络异常、DTP 认证失败等
            error = f"{type(exc).__name__}: {exc}"
            logging.getLogger(__name__).exception("生成报告失败：%s", error)
        self.root.after(0, self._on_generation_done, reports, error)

    def _on_generation_done(self, reports: list[Path], error: str | None) -> None:
        """主线程：恢复按钮状态并汇总本次结果。"""
        self._set_busy(False, "")

        # 生成过程中可能刚抓到开发者列表：把列表补到界面上，方便下次直接选
        if self.index is not None and not self.all_authors:
            self.all_authors = list(self.index.authors)
            self.refresh_author_lists()
            self.fetch_status_var.set(f"已获取 {len(self.all_authors)} 位开发者")

        if error:
            self.status_var.set("生成失败，请查看日志框中的错误信息。")
            messagebox.showerror("生成失败", f"生成报告失败：{error}\n\n详情见日志框。")
            return

        if not reports:
            self.status_var.set("没有生成任何报告（所选开发者在 DTP 上没有相关违规数据）。")
            messagebox.showwarning(
                "没有报告", "所选开发者在 DTP 上没有可用的静态分析数据，未生成报告。"
            )
            return

        out_dir = Path(self.output_dir).resolve()
        self.status_var.set(f"完成：已生成 {len(reports)} 份开发者报告 -> {out_dir}")
        logging.getLogger(__name__).info("完成：%d 份报告已输出到 %s", len(reports), out_dir)
        messagebox.showinfo(
            "生成完成",
            f"已生成 {len(reports)} 位开发者的报告（.html 与 .xlsx）。\n\n输出目录：\n{out_dir}",
        )

    # ------------------------------------------------------------------
    # 其它
    # ------------------------------------------------------------------
    def _set_busy(self, busy: bool, message: str) -> None:
        """切换忙碌状态：禁用按钮 + 进度条动画 + 状态栏文案。"""
        state = "disabled" if busy else "normal"
        self.fetch_button.configure(state=state)
        self.generate_button.configure(state=state)
        if busy:
            self.progress.start(60)
            if message:
                self.status_var.set(message)
        else:
            self.progress.stop()

    def on_open_output(self) -> None:
        out_dir = Path(self.output_dir)
        if not out_dir.exists():
            messagebox.showinfo("目录不存在", f"输出目录还不存在：\n{out_dir.resolve()}\n\n请先生成报告。")
            return
        open_in_file_manager(out_dir)

    def _on_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            if not messagebox.askyesno("正在生成", "报告还在生成中，确定要退出吗？"):
                return
        sys.stdout = self._original_stdout
        sys.stderr = self._original_stderr
        self.root.destroy()


def launch_author_gui(config_path: str = "config.yaml", locale: str | None = None) -> None:
    """创建并运行开发者报告主窗口（main_developer.py 无参数启动时调用）。

    ``locale`` 为 ``None`` 时使用 config.yaml 的 ``report.locale_developer``；
    main_developer.py 传 ``"v4_zh"``，让界面生成的报告使用 templates/report_v4_zh.html。
    """
    root = tk.Tk()
    AuthorReportApp(root, config_path=config_path, locale=locale)
    root.mainloop()


if __name__ == "__main__":
    launch_author_gui()
