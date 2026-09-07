"""实时日志文本框 + sys.stdout/stderr 重定向。

技术要点：
- 自定义 TextWriter：实现 write() / flush()，把写入的文本通过回调推给 GUI。
- 线程安全：后台线程写 stdout 时，TextWriter 只负责把文本塞进线程安全的队列；
  GUI 主线程通过 after() 轮询队列、批量 insert，绝不在后台线程直接操作 Tk 控件。
- 自动滚动到底部；超过缓冲上限自动裁剪旧行，防止长跑爆内存。
"""
from __future__ import annotations

import os
import sys
import queue
import threading
from datetime import datetime
from typing import Callable, Optional

try:
    import customtkinter as ctk
except ImportError as _e:  # pragma: no cover
    raise SystemExit("缺少依赖 customtkinter，请先 pip install customtkinter") from _e

import pipeline_config_loader as _cfg_loader


class _QueuedWriter:
    """sys.stdout/stderr 的替代实现：写文本进队列，不碰 Tk 控件。

    除了塞进线程安全队列供 GUI 主线程渲染外，还会同步把原始文本 tee 一份到
    磁盘（write_through 回调）——这样界面上能看到的每一个字节都落盘，包括子进程
    stderr（PIL 的 DecompressionBombWarning 等 warnings.warn 走 stderr 不经
    logging，各阶段 logs/0Xa_*.log 搜不到）、GUI 侧的 ⚠️/❌ 打印、未捕获异常栈。
    落盘在后台线程写入即发生，不依赖 60ms 轮询，故 GUI 崩溃也不丢诊断日志。
    """

    def __init__(
        self,
        q: "queue.Queue",
        stream_name: str,
        write_through: Optional[Callable[[str, str], None]] = None,
    ):
        self._q = q
        self._name = stream_name
        self._write_through = write_through

    def write(self, s: str) -> int:
        if not s:
            return 0
        self._q.put((self._name, s))
        if self._write_through is not None:
            # 落盘失败绝不能影响界面显示，回调内部已自行吞异常兜底
            self._write_through(self._name, s)
        return len(s)

    def flush(self) -> None:  # noqa: D401
        # 队列模式无需 flush，消费端持续轮询
        return None

    def isatty(self) -> bool:
        return False

    def fileno(self):
        raise OSError("redirected stream has no fileno")

    @property
    def encoding(self):
        return "utf-8"


class LogTextbox(ctk.CTkTextbox):
    """带纵向滚动条的只读日志文本框。

    用法：
        log = LogTextbox(parent)
        log.pack(...)
        log.install()   # 安装 stdout/stderr 重定向
        log.uninstall() # 还原（关闭窗口前调用）
    """

    MAX_LINES = 8000  # 超过则裁剪旧行，控制内存

    def __init__(self, master, **kwargs):
        kwargs.setdefault("wrap", "word")
        super().__init__(master, **kwargs)
        self._queue: "queue.Queue" = queue.Queue()
        self._lock = threading.Lock()
        self._installed = False
        self._old_stdout: Optional[object] = None
        self._old_stderr: Optional[object] = None
        self._poll_id: Optional[str] = None
        # 标记当前是否正被用户手动滚动查看（暂停自动滚到底）
        self._user_scrolled = False
        # 行钩子：每渲染出一整行文本就回调一次（主线程内调用，可安全更新 UI）。
        # 用于「进度条解析」等旁路消费，不影响日志内容本身。
        self._line_hook: Optional[object] = None
        self._line_buf = ""
        # 控制台全量镜像落盘：句柄 + 写锁（stdout/stderr 两个 writer 并发写）。
        # 每次 install() 建一个 gui_console_YYYYMMDD_HHMMSS.log；uninstall() 关闭。
        self._log_fp = None
        self._log_lock = threading.Lock()
        # stderr 来源行落盘时加 [stderr] 前缀，便于事后区分警告来源（PIL 警告走
        # stderr）；界面文本框不显示该前缀。按行加：仅在“行首”插入，避免把一行
        # 中途 flush 的多次 write 拆成多个前缀。
        self._file_at_line_start = True


    # ---------- 控制台全量落盘 ----------
    def _open_console_log(self) -> None:
        """在 logs/ 下按时间戳新建一份 GUI 控制台全量镜像日志。

        失败静默降级（不阻断 GUI 启动）：句柄留空，write_through 变空操作。
        """
        try:
            log_dir = os.path.join(_cfg_loader.BASE_DIR, "logs")
            os.makedirs(log_dir, exist_ok=True)
            path = os.path.join(
                log_dir, f"gui_console_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
            )
            # 行缓冲：崩溃时尽量少丢；errors=replace 兜底罕见编码异常。
            self._log_fp = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
            banner = (
                f"{'=' * 70}\n"
                f"GUI 控制台会话开始 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"（此文件是界面日志框的全量镜像，[stderr] 前缀标记 stderr 来源行）\n"
                f"{'=' * 70}\n"
            )
            self._log_fp.write(banner)
            self._log_fp.flush()
        except Exception:
            self._log_fp = None

    def _write_console_file(self, stream_name: str, text: str) -> None:
        """线程安全地把一段文本写入控制台镜像文件。

        来源标记加前缀规则：
        - stderr：每个物理行行首加 [stderr]
        - manual：每个物理行行首加 [manual]（GUI 侧旁路日志，如切换 profile）
        - stdout：不加前缀

        由后台线程（写 stdout/stderr）和主线程（append）直接调用，故必须加锁。
        任何异常都吞掉，绝不影响界面显示。
        """
        fp = self._log_fp
        if fp is None or not text:
            return
        try:
            with self._log_lock:
                if stream_name == "stderr":
                    out = self._prefix_lines(text, "[stderr] ")
                elif stream_name == "manual":
                    out = self._prefix_lines(text, "[manual] ")
                else:
                    out = text
                fp.write(out)
        except Exception:
            pass

    def _prefix_lines(self, text: str, prefix: str) -> str:
        """给文本的每个物理行行首加指定前缀。

        跨多次 write 的半行用 self._file_at_line_start 跟踪，避免把一行中途
        flush 的片段各加一次前缀。注意：行首状态在 stdout/stderr/manual 三个
        来源间共享，若它们交错写，行首状态可能不完美，但对诊断无实质影响。
        """
        parts = text.split("\n")
        buf = []
        for i, seg in enumerate(parts):
            is_last = i == len(parts) - 1
            if self._file_at_line_start and (seg or not is_last):
                buf.append(prefix)
            buf.append(seg)
            if not is_last:
                buf.append("\n")
                self._file_at_line_start = True
            else:
                # 末段后无换行：若 seg 非空说明停在行中途
                self._file_at_line_start = (seg == "")
        return "".join(buf)

    # ---------- 重定向安装 ----------
    def install(self) -> None:
        if self._installed:
            return
        self._open_console_log()
        self._old_stdout = sys.stdout
        self._old_stderr = sys.stderr
        sys.stdout = _QueuedWriter(self._queue, "stdout", self._write_console_file)  # type: ignore
        sys.stderr = _QueuedWriter(self._queue, "stderr", self._write_console_file)  # type: ignore
        self._installed = True
        self._poll()

    def uninstall(self) -> None:
        if not self._installed:
            return
        if self._old_stdout is not None:
            sys.stdout = self._old_stdout
        if self._old_stderr is not None:
            sys.stderr = self._old_stderr
        self._installed = False
        if self._poll_id is not None:
            try:
                self.after_cancel(self._poll_id)
            except Exception:
                pass
            self._poll_id = None
        # 关闭控制台镜像文件（flush + close），避免截断/句柄泄漏。
        with self._log_lock:
            fp = self._log_fp
            self._log_fp = None
        if fp is not None:
            try:
                fp.write(
                    f"{'=' * 70}\nGUI 控制台会话结束 "
                    f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n{'=' * 70}\n"
                )
                fp.flush()
                fp.close()
            except Exception:
                pass

    # ---------- 行钩子 ----------
    def set_line_hook(self, hook) -> None:
        """注册一个「整行文本」回调，主线程内调用。hook(line: str) -> None。

        传入的 line 不含结尾换行。用于进度解析等旁路消费，不影响日志显示。
        """
        self._line_hook = hook

    def _feed_line_hook(self, text: str) -> None:
        """把新写入的文本按换行切成整行，逐行喂给钩子。"""
        if not self._line_hook or not text:
            return
        self._line_buf += text
        while "\n" in self._line_buf:
            line, self._line_buf = self._line_buf.split("\n", 1)
            try:
                self._line_hook(line)
            except Exception:
                pass

    # ---------- 手动追加（非 stdout 来源也可用） ----------
    def append(self, text: str, tag: Optional[str] = None) -> None:
        """追加一段文本到日志框（界面+文件）。

        用于 GUI 侧的切换 profile/档位等旁路日志，tag="manual" 落盘时加 [manual]
        前缀，区别于子进程的 stdout/stderr。
        """
        if not text:
            return
        self._queue.put(("manual", (text, tag)))
        # 同步落盘，加 [manual] 前缀标记来源（与界面 tag 对应）
        self._write_console_file("manual", text)

    def clear(self) -> None:
        self.configure(state="normal")
        self.delete("1.0", "end")
        self.configure(state="disabled")

    # ---------- 轮询消费 ----------
    def _poll(self) -> None:
        try:
            batch = []
            while True:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            if batch:
                self._render(batch)
        finally:
            self._poll_id = self.after(60, self._poll)

    def _render(self, batch) -> None:
        self.configure(state="normal")
        try:
            self.tag_config("stderr", foreground="#ff6b6b")
            self.tag_config("manual", foreground="#7aa2f7")
            for kind, payload in batch:
                if kind == "manual":
                    text, tag = payload
                    self.insert("end", text, tag or None)
                    self._feed_line_hook(text)
                else:
                    tag = "stderr" if kind == "stderr" else None
                    self.insert("end", payload, tag)
                    self._feed_line_hook(payload)
            self._trim_if_needed()
            self._auto_scroll()
        finally:
            self.configure(state="disabled")

    def _trim_if_needed(self) -> None:
        try:
            total = int(self.index("end-1c").split(".")[0])
        except Exception:
            return
        if total > self.MAX_LINES:
            cut = total - self.MAX_LINES
            self.delete("1.0", f"{cut}.0")

    def _auto_scroll(self) -> None:
        try:
            yview = self.yview()[1]
        except Exception:
            return
        if yview > 0.98 or not self._user_scrolled:
            self.see("end")