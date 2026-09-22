#!/usr/bin/env python3
"""DTP 报告生成器 —— 图形界面（统一报告窗口）。

窗口流程（``python main.py`` 不带参数时打开）：

1. “① 连接 DTP 并获取数据”：调用 :func:`data_pipeline.fetch_all_data` 一次性把三类报告
   需要的数据抓完，进度条显示抓取进度，100% 表示数据全部就绪；数据没就绪时三个报告
   按钮都是灰的；旁边的“重新加载配置”用于改完 config.yaml 后手动重读（会作废已抓数据）；
2. 数据就绪后三块（选项卡）分别生成公司 / 项目 / 开发者报告，全部复用已抓好的数据
   （只有开发者报告会补抓“作者在各仓库的严重级别”），实现见 :mod:`report_service`；
3. 耗时操作一律放到后台线程：worker 只往队列里投递日志与回调，由主线程轮询执行，
   worker 完全不碰 Tk（窗口关掉也不会报 “main thread is not in main loop”）。

没有 tkinter 的环境（例如没装 python3-tk 的 Linux）仍可用命令行模式，见 ``main.py``。
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
from pathlib import Path

from data_pipeline import DataBundle, fetch_all_data
from report_author import load_config
from report_service import (
    REPORT_KINDS,
    REPORT_SUBDIRS,
    generate_author_reports_for,
    generate_company_report,
    generate_project_reports_for,
)

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    from gui_common import (
        LOG_POLL_MS,
        MAX_LOG_LINES,
        TRIM_LOG_LINES,
        QueueLogHandler,
        QueueStream,
        open_in_file_manager,
    )
    from gui_widgets import DualListSelector, ProgressController

    GUI_AVAILABLE = True
except ImportError:  # pragma: no cover - 取决于运行环境
    tk = ttk = filedialog = messagebox = None  # type: ignore[assignment]
    QueueLogHandler = QueueStream = None  # type: ignore[assignment]
    open_in_file_manager = None  # type: ignore[assignment]
    DualListSelector = ProgressController = None  # type: ignore[assignment]
    LOG_POLL_MS, MAX_LOG_LINES, TRIM_LOG_LINES = 100, 5000, 1000
    GUI_AVAILABLE = False

logger = logging.getLogger(__name__)


class UnifiedReportApp:
    """统一报告窗口：先点"① 连接 DTP 并获取数据"（带进度条），然后公司 / 项目 / 开发者三块各自出报告。"""

    def __init__(self, root: "tk.Tk", config_path: str = "config.yaml") -> None:
        self.root = root
        self.config_path = config_path

        # 后台线程 -> 主线程的消息队列（日志与界面回调都走队列，worker 不碰 Tk）
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        # 后台线程要执行的界面回调：worker 只把 (函数, 参数) 放进这里，由主线程轮询执行。
        # 这样 worker 完全不碰 Tk（即使窗口已经关掉也不会抛 "main thread is not in main loop"）
        self.ui_queue: "queue.Queue[tuple]" = queue.Queue()
        # 步骤 1 抓到的数据；为 None 表示还没获取数据（三个报告按钮保持灰的）
        self.bundle: DataBundle | None = None
        self.worker: threading.Thread | None = None

        # 进度条由 gui_widgets.ProgressController 管理（控件在 _build_fetch_frame 里创建）
        # 日志轮询的 after 任务（关闭窗口时取消，避免 Tk 解释器销毁后残留回调报错）
        self.log_poll_job: "str | None" = None

        self._build_ui()
        self._install_logging()
        self._reload_config(initial=True)
        self._poll_log_queue()
        self._refresh_buttons()

    # ------------------------------------------------------------------
    # 界面搭建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.root.title("DTP 报告生成器（公司 / 项目 / 开发者）")
        self.root.geometry("1120x900")
        self.root.minsize(900, 700)

        style = ttk.Style(self.root)
        for theme in ("vista", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break

        container = ttk.Frame(self.root, padding=10)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(1, weight=3)  # 三块（选项卡）
        container.rowconfigure(2, weight=2)  # 日志

        self._build_fetch_frame(container)   # row 0
        self._build_notebook(container)      # row 1
        self._build_log_frame(container)     # row 2

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_fetch_frame(self, parent: "ttk.Frame") -> None:
        frame = ttk.LabelFrame(
            parent, text="① 连接 DTP 并获取数据（三类报告共用这一份数据）", padding=8
        )
        frame.grid(row=0, column=0, sticky="ew")
        frame.columnconfigure(2, weight=1)

        self.fetch_button = ttk.Button(
            frame, text="连接 DTP 并获取数据", command=self.on_fetch_data, width=22
        )
        self.fetch_button.grid(row=0, column=0, sticky="w")

        # 进度条 + 百分比标签（抓取走真实进度，生成报告时切成来回滚动）
        self.progress_ctrl = ProgressController(frame, row=0, column=1)

        # 配置（config.yaml）在每次抓取前自动重读；这个按钮用于改完配置后手动重读，
        # 重读会作废已抓取的数据（仓库范围可能已变），需要重新点“连接 DTP 并获取数据”。
        ttk.Button(frame, text="重新加载配置", command=self._reload_config).grid(
            row=0, column=3, sticky="e", padx=(10, 0)
        )

        self.fetch_status_var = tk.StringVar(
            value="尚未获取数据：数据就绪后下面三块的报告按钮才会变成可点击。"
        )
        ttk.Label(frame, textvariable=self.fetch_status_var, foreground="#333").grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(6, 0)
        )
        self.data_summary_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.data_summary_var, foreground="#0a6a00").grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(2, 0)
        )

    def _build_notebook(self, parent: "ttk.Frame") -> None:
        frame = ttk.Frame(parent)
        frame.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        notebook = ttk.Notebook(frame)
        notebook.grid(row=0, column=0, sticky="nsew")

        company_tab = ttk.Frame(notebook, padding=10)
        project_tab = ttk.Frame(notebook, padding=10)
        author_tab = ttk.Frame(notebook, padding=10)
        notebook.add(company_tab, text=" ② 公司报告 ")
        notebook.add(project_tab, text=" ③ 项目报告 ")
        notebook.add(author_tab, text=" ④ 开发者报告 ")

        self._build_company_tab(company_tab)
        self._build_project_tab(project_tab)
        self._build_author_tab(author_tab)

    def _build_company_tab(self, tab: "ttk.Frame") -> None:
        tab.columnconfigure(0, weight=1)

        ttk.Label(
            tab,
            text="公司报告：合并 config.yaml 的 merge_projects 后做公司级汇总，"
                 "输出 Friendcom_<时间戳>.html / .xlsx。",
            wraplength=1000, justify="left",
        ).grid(row=0, column=0, sticky="w")

        self.company_summary_var = tk.StringVar(value="等待数据：先在上方点击“连接 DTP 并获取数据”。")
        ttk.Label(tab, textvariable=self.company_summary_var, foreground="#333").grid(
            row=1, column=0, sticky="w", pady=(8, 0)
        )

        self.company_button = ttk.Button(
            tab, text="公司报告", command=self.on_generate_company, width=22
        )
        self.company_button.grid(row=2, column=0, sticky="w", pady=(12, 0))

        ttk.Label(
            tab,
            text="数据未就绪时按钮是灰的；点击后在后台生成，进度和结果都写在下方日志框里。",
            foreground="#666",
        ).grid(row=3, column=0, sticky="w", pady=(10, 0))

    def _build_project_tab(self, tab: "ttk.Frame") -> None:
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        ttk.Label(
            tab,
            text="项目报告：左侧是 config.yaml 的全部项目；添加要出报告的项目后点“项目报告”，"
                 "每个项目生成一份 <项目名>_report_<时间戳>.html / .xlsx。",
            wraplength=1000, justify="left",
        ).grid(row=0, column=0, sticky="w")

        self.project_selector = DualListSelector(
            tab,
            available_title="可选项目（config.yaml 的 merge_projects）",
            selected_title="已选项目（每个项目一份报告）",
        )
        self.project_selector.frame.grid(row=1, column=0, sticky="nsew", pady=(8, 0))

        action = ttk.Frame(tab)
        action.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.project_button = ttk.Button(
            action, text="项目报告", command=self.on_generate_projects, width=22
        )
        self.project_button.grid(row=0, column=0, sticky="w")
        self.project_hint_var = tk.StringVar(value="")
        ttk.Label(action, textvariable=self.project_hint_var, foreground="#666").grid(
            row=0, column=1, sticky="w", padx=(10, 0)
        )

    def _build_author_tab(self, tab: "ttk.Frame") -> None:
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

        ttk.Label(
            tab,
            text="开发者报告：左侧是 DTP 上的全部作者；添加要出报告的开发者后点“开发者报告”，"
                 "每位作者生成一份 <作者名>_developer_report_<时间戳>.html / .xlsx。",
            wraplength=1000, justify="left",
        ).grid(row=0, column=0, sticky="w")

        manual = ttk.Frame(tab)
        manual.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(manual, text="手工添加（DTP 作者列表里还没有的人）：").pack(side="left")
        self.manual_author_var = tk.StringVar()
        manual_entry = ttk.Entry(manual, textvariable=self.manual_author_var, width=28)
        manual_entry.pack(side="left", padx=(6, 0))
        # 回车即添加，省得再去点按钮
        manual_entry.bind("<Return>", lambda _event: self.on_add_manual_author())
        ttk.Button(manual, text="添加", command=self.on_add_manual_author, width=8).pack(
            side="left", padx=(6, 0)
        )

        self.author_selector = DualListSelector(
            tab,
            available_title="可选开发者（DTP 上的全部作者）",
            selected_title="已选开发者（每位一份报告）",
        )
        self.author_selector.frame.grid(row=2, column=0, sticky="nsew", pady=(8, 0))

        action = ttk.Frame(tab)
        action.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.author_button = ttk.Button(
            action, text="开发者报告", command=self.on_generate_authors, width=22
        )
        self.author_button.grid(row=0, column=0, sticky="w")
        self.author_hint_var = tk.StringVar(value="")
        ttk.Label(action, textvariable=self.author_hint_var, foreground="#666").grid(
            row=0, column=1, sticky="w", padx=(10, 0)
        )

    def _build_log_frame(self, parent: "ttk.Frame") -> None:
        frame = ttk.LabelFrame(parent, text="日志", padding=8)
        frame.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(
            frame, height=10, wrap="none", state="disabled",
            background="#1e1e1e", foreground="#d4d4d4",
            insertbackground="#d4d4d4", font=("Consolas", 9),
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")

        y_scroll = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll = ttk.Scrollbar(frame, orient="horizontal", command=self.log_text.xview)
        x_scroll.grid(row=1, column=0, sticky="ew")
        self.log_text.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

        toolbar = ttk.Frame(frame)
        toolbar.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(toolbar, text="打开输出目录", command=self.on_open_output, width=14).pack(side="left")
        ttk.Button(toolbar, text="保存日志", command=self.on_save_log, width=12).pack(
            side="left", padx=(8, 0)
        )

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
        """把一行日志追加到日志框末尾（只在主线程调用）。"""
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        if int(self.log_text.index("end-1c").split(".")[0]) > MAX_LOG_LINES:
            self.log_text.delete("1.0", f"{TRIM_LOG_LINES + 1}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_log_queue(self) -> None:
        """定时把后台线程写入队列的内容搬到界面上（日志 + 界面回调）。"""
        try:
            while True:
                self.append_log(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        try:
            while True:
                callback, args = self.ui_queue.get_nowait()
                try:
                    callback(*args)
                except Exception:  # 单个回调出错不能拖垮轮询定时器
                    logger.exception(
                        "处理界面回调失败：%s", getattr(callback, "__name__", callback)
                    )
        except queue.Empty:
            pass
        self.log_poll_job = self.root.after(LOG_POLL_MS, self._poll_log_queue)

    def on_save_log(self) -> None:
        path = filedialog.asksaveasfilename(
            title="保存日志",
            defaultextension=".log",
            filetypes=[("日志文件", "*.log"), ("文本文件", "*.txt"), ("所有文件", "*.*")],
            initialfile="dtp_report.log",
        )
        if not path:
            return
        Path(path).write_text(self.log_text.get("1.0", "end-1c"), encoding="utf-8")
        self.log_queue.put(f"[系统] 日志已保存：{path}")

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _reload_config(self, initial: bool = False) -> bool:
        """重新读取 config.yaml，刷新项目列表（配置变更后作废已抓取的数据）。

        界面左上角没有“配置信息”面板了，配置内容不再回显；重读结果只在日志框里
        留一条记录（配置文件路径 + 项目数），方便确认读的是哪份配置。
        """
        try:
            config = load_config(self.config_path)
        except Exception as exc:  # 文件缺失 / yaml 解析失败
            self.log_queue.put(f"[错误] 读取配置文件失败：{exc}")
            if not initial:
                messagebox.showerror("配置错误", f"读取配置文件失败：{exc}")
            self.fetch_status_var.set("配置读取失败：请检查 config.yaml 后重新加载。")
            return False

        merge_projects = config.get("merge_projects", {}) or {}
        summary = f"{Path(self.config_path).resolve()}：{len(merge_projects)} 个项目"

        # 项目列表直接来自 config.yaml（不用连 DTP），配置一变就刷新
        self.project_selector.set_items([str(name) for name in merge_projects])

        if not initial and self.bundle is not None:
            # 配置变了（仓库范围可能也变了），之前抓的数据作废，避免用旧数据出报告
            self.bundle = None
            self.author_selector.set_items([])
            self._reset_progress()
            self.fetch_status_var.set("配置已变更，请重新点击“连接 DTP 并获取数据”。")
            self.log_queue.put(f"[系统] 配置已重新加载（{summary}），之前抓取的数据已作废。")
        elif not initial:
            self.log_queue.put(f"[系统] 配置已重新加载（{summary}）。")

        self._refresh_buttons()
        return True

    # ------------------------------------------------------------------
    # 进度条（步骤 1 用真实的阶段进度；生成报告时用不确定模式）
    # ------------------------------------------------------------------
    def _reset_progress(self) -> None:
        """回到 0%（配置变更 / 重新开始一轮抓取）。"""
        self.progress_ctrl.reset()

    def _start_fetch_progress(self) -> None:
        self.progress_ctrl.start_fetch()

    def _on_progress(self, percent: float, message: str) -> None:
        """主线程：worker 线程上报的抓取进度 -> 抬高进度条目标值 + 更新状态文字。"""
        self.progress_ctrl.advance(percent)
        if message:
            self.fetch_status_var.set(message)

    def _stop_progress(self) -> None:
        self.progress_ctrl.stop()

    def _start_generating_progress(self) -> None:
        """生成报告没有可量化的进度：进度条切成“来回滚动”，避免看起来像卡住。"""
        self.progress_ctrl.start_generating()

    def _stop_generating_progress(self) -> None:
        self.progress_ctrl.stop_generating()

    def _refresh_buttons(self) -> None:
        """三个报告按钮：数据已就绪且没有后台任务在跑时才可点，否则保持灰色。"""
        busy = self._busy()
        ready = self.bundle is not None and self.bundle.has_data and not busy
        state = "normal" if ready else "disabled"
        for button in (self.company_button, self.project_button, self.author_button):
            button.configure(state=state)
        self.fetch_button.configure(state="disabled" if busy else "normal")

    def _busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _clear_log(self) -> None:
        """清空日志框，开始新一轮任务（需要留档时先用上面的“保存日志”导出）。"""
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # ------------------------------------------------------------------
    # 步骤 1：连接 DTP 并获取数据
    # ------------------------------------------------------------------
    def on_fetch_data(self) -> None:
        if self._busy():
            messagebox.showinfo("正在忙", "上一个任务还在进行中，请等它结束。")
            return
        if not self._reload_config():
            return

        self._clear_log()
        self.log_queue.put("[系统] 开始连接 DTP 并获取三类报告需要的数据…")
        self.bundle = None
        self.author_selector.set_items([])
        self.company_summary_var.set("正在获取数据…（进度见上方进度条与下方日志）")
        self._start_fetch_progress()
        self.worker = threading.Thread(target=self._run_fetch, daemon=True)
        self.worker.start()
        self._refresh_buttons()

    def _run_fetch(self) -> None:
        """后台线程：抓全部数据（进度回调只投队列，由主线程执行，worker 不碰 Tk）。"""
        bundle: DataBundle | None = None
        error: str | None = None
        try:
            bundle = fetch_all_data(
                self.config_path,
                progress=lambda percent, message: self.ui_queue.put(
                    (self._on_progress, (percent, message))
                ),
            )
        except Exception as exc:  # 网络异常、DTP 认证失败、配置错误等
            error = f"{type(exc).__name__}: {exc}"
            logging.getLogger(__name__).exception("获取数据失败：%s", error)
        self.ui_queue.put((self._on_fetch_done, (bundle, error)))

    def _on_fetch_done(self, bundle: DataBundle | None, error: str | None) -> None:
        """主线程：收到抓取结果 -> 刷新三块内容与按钮状态。"""
        self._stop_progress()
        self.worker = None

        if error is not None or bundle is None:
            # 失败时不留旧数据，三个报告按钮保持灰色
            self.bundle = None
            self.fetch_status_var.set("获取数据失败，请查看日志框里的错误信息。")
            self.company_summary_var.set("等待数据：先在上方点击“连接 DTP 并获取数据”。")
            messagebox.showerror("获取数据失败", f"{error}\n\n详情见日志框。")
            self._refresh_buttons()
            return

        self.bundle = bundle
        self.progress_ctrl.set_complete()

        repo_count = len([item for item in bundle.data if item.has_data])
        self.fetch_status_var.set(
            f"数据已就绪（100%）：{repo_count} 个有数据的仓库、"
            f"{len(bundle.index.authors)} 位开发者；下面三块可以出报告了。"
        )
        self.data_summary_var.set(
            f"数据快照：{len(bundle.merge_projects)} 个项目组、{repo_count} 个有数据的仓库、"
            f"{len(bundle.index.authors)} 位开发者；输出目录 {bundle.output_dir.resolve()}"
            "（company / project / author 三个子目录）"
        )
        self.company_summary_var.set(
            f"数据已就绪：{len(bundle.data)} 条仓库记录；公司报告会合并 "
            f"{len(bundle.merge_projects)} 个项目组。"
        )
        self.project_selector.set_items([str(name) for name in bundle.merge_projects])
        self.author_selector.set_items(list(bundle.index.authors))
        self.project_hint_var.set(f"共 {len(bundle.merge_projects)} 个可选项目。")
        self.author_hint_var.set(f"共 {len(bundle.index.authors)} 位可选开发者。")
        self.log_queue.put("[系统] 数据获取完成，可以分别生成公司 / 项目 / 开发者报告了。")

        if not bundle.has_data:
            messagebox.showwarning(
                "没有数据",
                "DTP 上没有抓到任何静态分析数据（请检查 config.yaml 的仓库名与 DTP 数据），"
                "三个报告按钮会保持灰色。",
            )
        self._refresh_buttons()

    # ------------------------------------------------------------------
    # 三块（公司 / 项目 / 开发者）的报告生成
    # ------------------------------------------------------------------
    def _require_data(self) -> bool:
        """生成前的统一检查（按钮本来就只在数据就绪时可点，这里是兜底提示）。"""
        if self._busy():
            messagebox.showinfo("正在忙", "上一个任务还在进行中，请等它结束。")
            return False
        if self.bundle is None or not self.bundle.has_data:
            messagebox.showwarning("数据未就绪", "请先点击“连接 DTP 并获取数据”。")
            return False
        return True

    def on_generate_company(self) -> None:
        """“公司报告”按钮：合并后出一份公司级报告。"""
        if not self._require_data():
            return
        self._start_generation("company", [])

    def on_generate_projects(self) -> None:
        """“项目报告”按钮：为每个已选项目出一份报告；未选择时确认后按全部项目出。"""
        if not self._require_data():
            return
        selected = self.project_selector.selected_items()
        if not selected and self.bundle is not None:
            total = len(self.bundle.merge_projects)
            if not messagebox.askyesno(
                "未选择项目",
                f"当前没有选择任何项目。\n\n是否为 config.yaml 里的全部 {total} 个项目"
                f"各生成一份报告？",
            ):
                return
        self._start_generation("project", selected)

    def on_generate_authors(self) -> None:
        """“开发者报告”按钮：为每位已选开发者出一份报告；未选择时确认后按全部作者出。"""
        if not self._require_data():
            return
        selected = self.author_selector.selected_items()
        if not selected and self.bundle is not None:
            total = len(self.bundle.index.authors)
            if not messagebox.askyesno(
                "未选择开发者",
                f"当前没有选择任何开发者。\n\n是否为 DTP 上的全部 {total} 位开发者"
                f"各生成一份报告？（会比较慢）",
            ):
                return
        self._start_generation("author", selected)

    def on_add_manual_author(self) -> None:
        """“手工添加”：把 DTP 作者列表里还没有的人加入已选（走 author= 兜底查询）。"""
        name = self.manual_author_var.get().strip()
        if not name:
            messagebox.showinfo("没有输入", "请先填写开发者名（例如 hendra）。")
            return
        self.author_selector.add_item(name)
        self.manual_author_var.set("")
        self.log_queue.put(f"[选择] 已手工添加开发者：{name}")

    def _start_generation(self, kind: str, items: list[str]) -> None:
        """启动后台线程生成报告（生成期间按钮变灰、进度条切到滚动模式）。"""
        title = REPORT_KINDS[kind]
        self._clear_log()
        self.log_queue.put(f"[系统] 开始生成{title}（复用已抓取的数据快照）…")
        self._start_generating_progress()
        self.worker = threading.Thread(
            target=self._run_generation, args=(kind, items), daemon=True
        )
        self.worker.start()
        self._refresh_buttons()

    def _run_generation(self, kind: str, items: list[str]) -> None:
        """后台线程：调用本文件里的三个生成函数（都复用已抓取的数据快照）。"""
        reports: list[Path] = []
        error: str | None = None
        try:
            if kind == "company":
                reports = generate_company_report(self.bundle)
            elif kind == "project":
                reports = generate_project_reports_for(self.bundle, items or None)
            else:
                reports = generate_author_reports_for(self.bundle, items or None)
        except Exception as exc:  # 网络异常（开发者报告还要补抓严重级别）、写文件失败等
            error = f"{type(exc).__name__}: {exc}"
            logging.getLogger(__name__).exception("生成报告失败：%s", error)
        self.ui_queue.put((self._on_generation_done, (kind, reports, error)))

    def _on_generation_done(self, kind: str, reports: list[Path], error: str | None) -> None:
        """主线程：恢复界面并汇总本次结果。"""
        self._stop_generating_progress()
        self.worker = None
        title = REPORT_KINDS[kind]

        if error is not None:
            self.fetch_status_var.set(f"{title}生成失败，请查看日志框中的错误信息。")
            messagebox.showerror("生成失败", f"生成{title}失败：{error}\n\n详情见日志框。")
            self._refresh_buttons()
            return

        if not reports:
            self.fetch_status_var.set(f"{title}：没有生成任何报告（所选内容没有静态分析数据）。")
            messagebox.showwarning(
                "没有报告", f"所选内容在 DTP 上没有可用的静态分析数据，未生成{title}。"
            )
            self._refresh_buttons()
            return

        base_out = self.bundle.output_dir if self.bundle is not None else Path("./output")
        out_dir = (base_out / REPORT_SUBDIRS[kind]).resolve()
        self.fetch_status_var.set(f"完成：已生成 {len(reports)} 份{title} -> {out_dir}")
        logging.getLogger(__name__).info("完成：%d 份%s已输出到 %s", len(reports), title, out_dir)
        messagebox.showinfo(
            "生成完成",
            f"已生成 {len(reports)} 份{title}（.html 与 .xlsx）。\n\n输出目录：\n{out_dir}",
        )
        self._refresh_buttons()

    # ------------------------------------------------------------------
    # 其它
    # ------------------------------------------------------------------
    def on_open_output(self) -> None:
        out_dir = self.bundle.output_dir if self.bundle is not None else Path("./output")
        if not out_dir.exists():
            messagebox.showinfo(
                "目录不存在", f"输出目录还不存在：\n{out_dir.resolve()}\n\n请先生成报告。"
            )
            return
        open_in_file_manager(out_dir)

    def _on_close(self) -> None:
        if self._busy():
            if not messagebox.askyesno("正在忙", "还有任务在进行中，确定要退出吗？"):
                return
        self._stop_progress()
        if self.log_poll_job is not None:
            self.root.after_cancel(self.log_poll_job)
            self.log_poll_job = None
        sys.stdout = self._original_stdout
        sys.stderr = self._original_stderr
        self.root.destroy()


def launch_gui(config_path: str = "config.yaml") -> None:
    """创建并运行统一报告窗口（run_entry 调用；也是 GUI 依赖的检查点）。"""
    if not GUI_AVAILABLE:
        raise RuntimeError(
            "当前 Python 环境没有 tkinter，无法打开图形界面；请改用命令行模式，例如："
            "python main.py --all"
        )
    root = tk.Tk()
    UnifiedReportApp(root, config_path=config_path)
    root.mainloop()
