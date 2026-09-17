#!/usr/bin/env python3
"""DTP 静态分析报告生成器 —— 图形界面（tkinter，标准库自带）。

界面元素（对应需求）：

1. 项目选择框：下拉菜单列出 config.yaml ``merge_projects`` 的全部项目（不含仓库，
   如"海外三相电表平台""印尼集中器平台"），勾选即可多选，并提供"全选/清空"；
   菜单下方实时显示已选项目与数量；点击"生成报告"后只抓取所选项目下的仓库数据；
2. "报告"按钮：点击后在后台线程执行报告生成，为每个所选项目输出一份
   .html + .xlsx 报告（生成期间界面不会卡住，可随时查看日志）；
3. 日志框：logging 以及 print/stderr 的输出全部实时显示在界面日志框里。

启动方式：

    python main_project.py         # 无参数 -> 自动打开本界面
    python main_project.py --gui   # 显式打开本界面
    python report_gui.py           # 直接运行本文件
"""

from __future__ import annotations

import io
import logging
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from main_project import generate_reports, list_project_names, load_config

# 与命令行日志格式保持一致（main_project.py 里的 logging.basicConfig）
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 日志框最多保留的行数，超出后丢弃最早的若干行，避免长时间运行占用内存
_MAX_LOG_LINES = 5000
_TRIM_LOG_LINES = 1000

# 日志框刷新间隔（毫秒）
_LOG_POLL_MS = 100


class QueueLogHandler(logging.Handler):
    """把日志记录放进队列，由界面主线程取出显示（线程安全）。"""

    def __init__(self, log_queue: "queue.Queue[str]") -> None:
        super().__init__()
        self.log_queue = log_queue
        self.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.log_queue.put(self.format(record))
        except Exception:  # pragma: no cover - 日志本身不应抛异常
            self.handleError(record)


class QueueStream(io.TextIOBase):
    """把 print()/stderr 的输出重定向到界面日志框（同时保留原输出）。"""

    def __init__(self, log_queue: "queue.Queue[str]", tag: str, original=None) -> None:
        self.log_queue = log_queue
        self.tag = tag
        self.original = original
        self._buffer = ""

    def write(self, text: str) -> int:
        if self.original is not None:
            try:
                self.original.write(text)
            except Exception:
                pass

        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip("\r")
            if line.strip():
                self.log_queue.put(f"[{self.tag}] {line}")
        return len(text)

    def flush(self) -> None:
        if self._buffer.strip():
            self.log_queue.put(f"[{self.tag}] {self._buffer.strip()}")
            self._buffer = ""
        if self.original is not None:
            try:
                self.original.flush()
            except Exception:
                pass

    def isatty(self) -> bool:  # pragma: no cover - 仅影响库的行为分支
        return False


class ReportApp:
    """主窗口。

    界面分三块：配置信息（只读）、项目选择（下拉多选）、日志框（所有输出）。
    """

    def __init__(self, root: tk.Tk, config_path: str = "config.yaml") -> None:
        self.root = root
        self.config_path = config_path

        # 后台线程 -> 主线程的消息队列，保证跨线程更新界面安全
        self.log_queue: "queue.Queue[str]" = queue.Queue()

        self.config: dict = {}
        # config.yaml merge_projects 的全部项目名（下拉候选），顺序与配置文件一致
        self.project_names: list[str] = []
        # 项目名 -> 勾选状态（1 = 选中）
        self.project_vars: dict[str, tk.IntVar] = {}

        self.worker: threading.Thread | None = None
        self.output_dir = "./output"

        self._build_ui()
        self._install_logging()
        self._reload_config(initial=True)
        self._poll_log_queue()

    # ------------------------------------------------------------------
    # 界面搭建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.root.title("DTP 静态分析报告生成器")
        self.root.geometry("960x760")
        self.root.minsize(760, 560)

        style = ttk.Style(self.root)
        for theme in ("vista", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break

        container = ttk.Frame(self.root, padding=10)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(3, weight=1)

        self._build_config_frame(container)
        self._build_project_frame(container)
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
            "trend": tk.StringVar(value="-"),
        }

        rows = (
            ("配置文件", "config"),
            ("DTP 服务器", "url"),
            ("输出目录", "output"),
            ("报告模板", "locale"),
            ("趋势构建数", "trend"),
        )
        for row_index, (label, key) in enumerate(rows):
            ttk.Label(frame, text=f"{label}：").grid(row=row_index, column=0, sticky="w", padx=(0, 6))
            ttk.Label(frame, textvariable=self.info_vars[key]).grid(row=row_index, column=1, sticky="w")

        ttk.Button(frame, text="重新加载配置", command=self._reload_config).grid(
            row=0, column=2, rowspan=5, sticky="e", padx=(10, 0)
        )

    def _build_project_frame(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="1. 项目选择（下拉列表，可多选）", padding=8)
        frame.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        toolbar = ttk.Frame(frame)
        toolbar.grid(row=0, column=0, sticky="ew")

        # 下拉按钮：点开后是可勾选的菜单（支持多选）
        self.project_menu_button = ttk.Menubutton(toolbar, text="选择项目 ▾", width=32)
        self.project_menu = tk.Menu(self.project_menu_button, tearoff=False)
        self.project_menu_button["menu"] = self.project_menu
        self.project_menu_button.pack(side="left")

        ttk.Button(toolbar, text="全选", command=self.select_all).pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="清空", command=self.clear_all).pack(side="left", padx=(4, 0))

        self.count_label = tk.StringVar(value="已选择 0 / 0 个项目")
        ttk.Label(toolbar, textvariable=self.count_label).pack(side="left", padx=(12, 0))

        ttk.Label(
            frame,
            text="从下拉菜单里勾选 config.yaml 中的项目（例如：海外三相电表平台、印尼集中器平台）；"
                 "程序只会抓取所选项目下仓库的数据。",
        ).grid(row=1, column=0, sticky="w", pady=(6, 4))

        list_frame = ttk.Frame(frame)
        list_frame.grid(row=2, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        self.selected_listbox = tk.Listbox(list_frame, selectmode="extended", height=8, activestyle="none")
        self.selected_listbox.grid(row=0, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.selected_listbox.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.selected_listbox.configure(yscrollcommand=scrollbar.set)

    def _build_action_frame(self, parent: ttk.Frame) -> None:
        frame = ttk.Frame(parent)
        frame.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        frame.columnconfigure(2, weight=1)

        self.generate_button = ttk.Button(frame, text="2. 报告", command=self.on_generate, width=16)
        self.generate_button.grid(row=0, column=0, sticky="w")

        self.progress = ttk.Progressbar(frame, mode="indeterminate", length=180)
        self.progress.grid(row=0, column=1, sticky="w", padx=(10, 0))

        self.status_var = tk.StringVar(value="就绪：请选择项目后点击“报告”按钮。")
        ttk.Label(frame, textvariable=self.status_var).grid(row=0, column=2, sticky="w", padx=(10, 0))

        ttk.Button(frame, text="打开输出目录", command=self.on_open_output).grid(row=0, column=3, sticky="e")
        ttk.Button(frame, text="保存日志", command=self.on_save_log).grid(row=0, column=4, sticky="e", padx=(6, 0))

    def _build_log_frame(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="3. 日志", padding=8)
        frame.grid(row=3, column=0, sticky="nsew", pady=(8, 0))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(frame, height=14, wrap="none", state="disabled",
                                background="#1e1e1e", foreground="#d4d4d4",
                                insertbackground="#d4d4d4", font=("Consolas", 9))
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
            initialfile="dtp_report.log",
        )
        if not path:
            return
        content = self.log_text.get("1.0", "end-1c")
        Path(path).write_text(content, encoding="utf-8")
        self.log_queue.put(f"[系统] 日志已保存：{path}")

    # ------------------------------------------------------------------
    # 配置与项目列表
    # ------------------------------------------------------------------
    def _reload_config(self, initial: bool = False) -> bool:
        """重新读取 config.yaml，刷新配置信息与下拉菜单里的项目列表。"""
        try:
            self.config = load_config(self.config_path)
        except Exception as exc:  # 文件缺失 / yaml 解析失败
            self.log_queue.put(f"[错误] 读取配置文件失败：{exc}")
            messagebox.showerror("配置错误", f"读取配置文件失败：{exc}")
            return False

        dtp_cfg = self.config.get("dtp", {}) or {}
        report_cfg = self.config.get("report", {}) or {}

        self.project_names = list_project_names(self.config.get("merge_projects", {}))
        self.output_dir = str(report_cfg.get("output_dir", "./output"))

        self.info_vars["config"].set(str(Path(self.config_path).resolve()))
        self.info_vars["url"].set(str(dtp_cfg.get("url", "-")))
        self.info_vars["output"].set(str(Path(self.output_dir).resolve()))
        self.info_vars["locale"].set(str(report_cfg.get("locale_project", "v3_zh")))
        self.info_vars["trend"].set(f"{report_cfg.get('trend_builds', 10)} 次/仓库")

        self._rebuild_project_menu()
        self.log_queue.put(
            f"[系统] {'已加载' if initial else '已重新加载'} {len(self.project_names)} 个项目"
            "（来自 config.yaml 的 merge_projects）"
        )
        return True

    def _rebuild_project_menu(self) -> None:
        """按最新的项目列表重建下拉菜单（复选框 + 全选/清空）。"""
        self.project_menu.delete(0, "end")
        self.project_vars = {}

        for name in self.project_names:
            var = tk.IntVar(value=0)
            self.project_vars[name] = var
            # 勾选/取消后立即刷新已选列表（无需关闭菜单）
            self.project_menu.add_checkbutton(label=name, variable=var, command=self._refresh_selection)

        self.project_menu.add_separator()
        self.project_menu.add_command(label="全选", command=self.select_all)
        self.project_menu.add_command(label="清空", command=self.clear_all)

        self._refresh_selection()

    # ------------------------------------------------------------------
    # 项目选择
    # ------------------------------------------------------------------
    def selected_projects(self) -> list[str]:
        """已选中的项目名列表，顺序与 config.yaml 一致。"""
        return [name for name in self.project_names if self.project_vars[name].get()]

    def select_all(self) -> None:
        for var in self.project_vars.values():
            var.set(1)
        self._refresh_selection()

    def clear_all(self) -> None:
        for var in self.project_vars.values():
            var.set(0)
        self._refresh_selection()

    def _refresh_selection(self) -> None:
        selected = self.selected_projects()

        self.selected_listbox.delete(0, "end")
        for name in selected:
            self.selected_listbox.insert("end", name)

        self.count_label.set(f"已选择 {len(selected)} / {len(self.project_names)} 个项目")
        self.project_menu_button.configure(
            text=f"选择项目 ▾（已选 {len(selected)} 个）" if selected else "选择项目 ▾"
        )

    # ------------------------------------------------------------------
    # 生成报告
    # ------------------------------------------------------------------
    def on_generate(self) -> None:
        """“报告”按钮：后台线程里生成所选项目的报告。"""
        if self.worker is not None and self.worker.is_alive():
            messagebox.showinfo("正在生成", "上一次报告还在生成中，请等待完成。")
            return

        selected = self.selected_projects()
        if not selected:
            messagebox.showwarning("未选择项目", "请先点开下拉列表勾选至少一个项目。")
            return

        # 清空上一次的日志，开始新一轮
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

        logger = logging.getLogger(__name__)
        logger.info("开始生成报告，共选择 %d 个项目：%s", len(selected), "、".join(selected))

        self.generate_button.configure(state="disabled")
        self.progress.start(30)
        self.status_var.set(f"正在生成 {len(selected)} 个项目的报告...")

        self.worker = threading.Thread(target=self._run_generation, args=(selected,), daemon=True)
        self.worker.start()

    def _run_generation(self, selected: list[str]) -> None:
        """后台线程：调用 main_project.generate_reports，结果回主线程处理。"""
        reports: list[Path] = []
        error: str | None = None
        try:
            reports = generate_reports(
                config_path=self.config_path,
                selected_projects=selected,
                output_dir=self.output_dir,
            )
        except Exception as exc:  # 网络异常、DTP 认证失败等
            error = f"{type(exc).__name__}: {exc}"
            # logger.exception 会把完整堆栈写进日志框，便于排查 DTP 连接/接口问题
            logging.getLogger(__name__).exception("生成报告失败：%s", error)
        self.root.after(0, self._on_generation_done, reports, error)

    def _on_generation_done(self, reports: list[Path], error: str | None) -> None:
        """主线程：恢复按钮状态并汇总本次结果。"""
        self.progress.stop()
        self.generate_button.configure(state="normal")

        if error:
            self.status_var.set("生成失败，请查看日志框中的错误信息。")
            messagebox.showerror("生成失败", f"生成报告失败：{error}\n\n详情见日志框。")
            return

        if not reports:
            self.status_var.set("没有生成任何报告（所选项目在 DTP 上没有静态分析数据）。")
            messagebox.showwarning("没有报告", "所选项目在 DTP 上没有可用的静态分析数据，未生成报告。")
            return

        out_dir = Path(self.output_dir).resolve()
        self.status_var.set(f"完成：已生成 {len(reports)} 份报告 -> {out_dir}")
        logging.getLogger(__name__).info("完成：%d 份报告已输出到 %s", len(reports), out_dir)
        messagebox.showinfo(
            "生成完成",
            f"已生成 {len(reports)} 个项目的报告（.html 与 .xlsx）。\n\n输出目录：\n{out_dir}",
        )

    # ------------------------------------------------------------------
    # 其它
    # ------------------------------------------------------------------
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


def open_in_file_manager(path: Path) -> None:
    """用系统文件管理器打开目录（Windows / macOS / Linux）。"""
    path = Path(path)
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:  # pragma: no cover - 取决于操作系统
        messagebox.showerror("打不开目录", f"无法打开 {path}：{exc}")


def launch_gui(config_path: str = "config.yaml") -> None:
    """创建并运行主窗口（main_project.py 无参数启动时调用）。"""
    root = tk.Tk()
    ReportApp(root, config_path=config_path)
    root.mainloop()


if __name__ == "__main__":
    launch_gui()


