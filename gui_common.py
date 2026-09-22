#!/usr/bin/env python3
"""DTP 报告生成器 —— 图形界面公共设施（gui_app / gui_widgets 使用）。

内容：

1. :class:`QueueLogHandler`：把 ``logging`` 记录投入 ``queue.Queue``，由 Tk 主线程取出显示；
2. :class:`QueueStream`：劫持 ``sys.stdout`` / ``sys.stderr``，把 ``print()`` 与第三方库输出
   也导入日志框（同时保留原输出）；
3. :func:`open_in_file_manager`：用系统文件管理器打开目录（Windows / macOS / Linux）；
4. 日志相关的公共常量（格式 / 刷新间隔 / 最大行数）。

只有图形界面会导入本模块（内部用到 ``tkinter`` 的 messagebox），命令行模式不会加载它；
界面实现见 ``gui_app``（主窗口）与 ``gui_widgets``（可复用控件）。
"""

from __future__ import annotations

import io
import logging
import os
import queue
import subprocess
import sys
from pathlib import Path
from tkinter import messagebox

# 与命令行日志格式保持一致（main.py 里的 logging.basicConfig）
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 日志框刷新间隔（毫秒）
LOG_POLL_MS = 100

# 日志框最多保留的行数，超出后丢弃最早的若干行，避免长时间运行占用内存
MAX_LOG_LINES = 5000
TRIM_LOG_LINES = 1000


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
