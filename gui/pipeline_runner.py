"""后台流水线执行器。

核心：用 threading.Thread 跑 subprocess，subprocess 的 stdout/stderr 实时
按行 read 出来 print() 到 sys.stdout——因为 GUI 的 LogTextbox 已经接管了
sys.stdout，所以这些日志会实时出现在文本框里。绝不在 GUI 主线程里跑流水线。

为什么用 subprocess 而不是直接 import run_pipeline.main：
- run_pipeline.py 本身就用 subprocess 串联 01a/01b/02，保持一致。
- subprocess 隔离更好：子脚本里若有 sys.exit / 全局状态 / logging.basicConfig，
  不会污染 GUI 进程；Nuitka 编译后也好处理。
- 实时性靠逐行读取子进程 stdout 实现。

对外接口：
- PipelineRunner(config_path, fresh, profile, on_done)  构造
- .start()  启动后台线程
- .is_running()
- .stop()   请求停止（终止子进程；优雅退出）
"""
from __future__ import annotations

import os
import sys
import threading
import subprocess
from typing import Optional, Callable

import pipeline_config_loader as _cfg_loader
from .power import prevent_sleep, allow_sleep


# ==========================================
# Windows Job Object 绑定（杀整棵子进程树，防止孤儿进程）
# ------------------------------------------
# 背景：run_pipeline.py 在开发模式下用 subprocess.run 启动 stage01a/01b/02
# 子进程。GUI 的 stop() 若只对顶层 run_pipeline.py 调 proc.terminate()，
# Windows 上不会递归杀子进程，stage02 会变孤儿继续跑、并发写输出目录与
# 进度文件，产生重复照片与脏数据（见 v2.3.2 bug 修复）。
#
# 方案：用 Windows Job Object 绑定子进程树。一旦子进程加入 Job，它的所有
# 子孙进程自动加入同一 Job；TerminateJobObject 一刀切掉整棵树。
# 设置 JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE 后，即使 GUI 崩溃/被 taskkill，
# Job 句柄随进程退出而关闭，整树也会被回收。
#
# 兼容性：Win8+ 支持嵌套 Job（GUI 已在 Job 中也能再 Assign），本项目目标
# 系统为 Win10+，不考虑 Win7 限制。Assign 失败时降级到 proc.terminate()
# （回到修复前行为，不阻断功能）。
#
# 赛窗说明：Popen 返回到 AssignProcessToJobObject 调用之间存在微秒~毫秒级
# 窗口，理论上此窗口内子进程 spawn 的孙子会逃逸。但 run_pipeline.py 从
# 启动到 spawn stage02 需要 150ms+ 的 Python 解释器初始化，赛窗物理上
# 不可能被命中，故不采用 CREATE_SUSPENDED 方案（避免依赖 _threadhandle
# 私有属性）。
# ==========================================
import ctypes
from ctypes import wintypes

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# Job Object 信息类与限制标志
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9   # JobObjectExtendedLimitInformation
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

# CreateJobObjectW(lpJobAttributes, lpName) -> HANDLE
_kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
_kernel32.CreateJobObjectW.restype = wintypes.HANDLE

# AssignProcessToJobObject(hJob, hProcess) -> BOOL
_kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_kernel32.AssignProcessToJobObject.restype = wintypes.BOOL

# TerminateJobObject(hJob, uExitCode) -> BOOL
_kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
_kernel32.TerminateJobObject.restype = wintypes.BOOL

# SetInformationJobObject(hJob, JobObjectInfoClass, lpJobObjectInfo, cbJobObjectInfoLength) -> BOOL
_kernel32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE, wintypes.INT, wintypes.LPVOID, wintypes.DWORD,
]
_kernel32.SetInformationJobObject.restype = wintypes.BOOL

# CloseHandle(hObject) -> BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_void_p),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class PipelineRunner:
    """在独立线程里执行 run_pipeline.py，实时转发日志。"""

    def __init__(
        self,
        config_path: str,
        fresh: bool,
        profile: Optional[str],
        on_done: Optional[Callable[[int], None]] = None,
        from_stage: Optional[str] = None,
        only_stage: Optional[str] = None,
    ):
        self._config_path = config_path
        self._fresh = fresh
        self._profile = profile
        self._on_done = on_done
        # 断点续跑跳过前置阶段：非 None 时给 run_pipeline 加 --from <stage>。
        # 目前仅 GUI 在检测到 02_progress 上下文指纹匹配时传 "02"。
        self._from_stage = from_stage
        # 仅跑单个阶段：非 None 时给 run_pipeline 加 --only <stage>。
        # 目前 GUI 的「仅合并日报 (03a)」模式传 "03a"，「仅跨年聚合 (03b)」模式传 "03b"。
        self._only_stage = only_stage

        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None
        self._job_handle = None  # Windows Job Object 句柄（绑子进程树，stop 时杀整树）
        self._lock = threading.Lock()
        self._stopped = False
        self._running = False

    # ---------- 对外 ----------
    @staticmethod
    def _create_job_with_kill_on_close():
        """
        创建一个带 JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE 限制的 Job Object。

        返回 Job 句柄；任一步失败返回 None（调用方降级到 proc.terminate()）。
        KILL_ON_JOB_CLOSE 确保：即使 GUI 崩溃/被 taskkill，Job 句柄随进程
        退出而关闭时，整棵子进程树也会被操作系统回收。
        """
        try:
            job = _kernel32.CreateJobObjectW(None, None)
            if not job:
                return None
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = _kernel32.SetInformationJobObject(
                job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info), ctypes.sizeof(info),
            )
            if not ok:
                _kernel32.CloseHandle(job)
                return None
            return job
        except Exception:
            return None

    def start(self) -> bool:
        """启动后台线程。返回是否成功启动（已在跑则返回 False）。"""
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._stopped = False
        self._thread = threading.Thread(
            target=self._run, name="PipelineRunner", daemon=True
        )
        self._thread.start()
        return True

    def is_running(self) -> bool:
        return self._running

    def stop(self) -> None:
        """请求停止：终止子进程树。流水线本身支持断点续跑，下次可继续。

        优先用 Job Object 杀整树（覆盖 run_pipeline.py 的 stage 子进程孤儿场景）；
        Job 未启用或杀失败时兜底 proc.terminate()（仅杀顶层进程）。
        """
        self._stopped = True
        # 用户手动停止：立即恢复系统睡眠，不等后台线程收尾。
        # _run 的 finally 会再调一次 allow_sleep（幂等，无副作用）。
        allow_sleep()
        with self._lock:
            proc = self._proc
            job = self._job_handle
        # 优先用 Job Object 杀整树（含 run_pipeline.py 派生的所有 stage 子进程）
        if job:
            try:
                _kernel32.TerminateJobObject(job, 1)
            except Exception:
                pass
        # 兜底：terminate 顶层进程（Job 失败或未启用时）
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    def wait_stopped(self, timeout: float) -> None:
        """stop() 后等待 worker 线程真正退出（进程树已杀净）。

        供 GUI 在 _on_close 中调用：stop 后 join worker 线程，确保窗口销毁前
        子进程已被杀净，避免孤儿进程继续写输出目录。超时不阻塞（Job 句柄随
        GUI 退出关闭时 KILL_ON_JOB_CLOSE 仍会杀整树，安全）。
        """
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # ---------- 内部 ----------
    def _run(self) -> None:
        code = -1
        try:
            code = self._execute()
        except Exception as e:
            # 异常也要让用户看到
            try:
                print(f"\n❌ [GUI] 流水线执行异常：{e}", file=sys.stderr)
            except Exception:
                pass
            code = -1
        finally:
            with self._lock:
                self._running = False
            # 恢复系统睡眠（兜底：覆盖正常结束/异常/stop 所有路径；
            # stop() 已调过一次，此处幂等再调一次确保清除）。
            allow_sleep()
            # 关闭 Job Object 句柄，避免内核对象泄漏。
            # 注意：此时子进程树应已停止（正常结束或被 stop 杀掉），关闭句柄
            # 不会触发额外的 KILL_ON_JOB_CLOSE（进程已不在 Job 中）。
            job = self._job_handle
            if job:
                try:
                    _kernel32.CloseHandle(job)
                except Exception:
                    pass
                self._job_handle = None
            if self._on_done:
                try:
                    self._on_done(code)
                except Exception:
                    pass

    def _execute(self) -> int:
        base_dir = _cfg_loader.BASE_DIR
        # 流水线运行期间阻止系统睡眠（断点续跑也走这里，自动覆盖）。
        # 恢复点：stop() 立即调 + _run finally 兜底，见 allow_sleep 调用处。
        prevent_sleep()
        # Nuitka 编译后 sys.executable 指向临时解压目录里不存在的 python.exe，
        # 无法用于启动子进程。改用 sys.argv[0]（即 .exe 本身）自调用：
        #   aiphotoarrange.exe --internal-run-pipeline [--config x] [--fresh] [--profile P]
        # 由 run_gui.py 的 _dispatch_internal 转发到 run_pipeline.main()，
        # 再由 run_pipeline 用同样的自调用方式串联各阶段，全程不依赖外部
        # python 解释器。开发模式仍用 sys.executable 走原 python 子进程路径。
        _compiled = "__compiled__" in globals()
        if _compiled:
            exe = sys.argv[0]
            script_args = ["--internal-run-pipeline"]
        else:
            exe = sys.executable
            script = os.path.join(base_dir, "run_pipeline.py")
            script_args = []

        cmd = [exe] + script_args if _compiled else [exe, script]
        if self._config_path:
            cmd += ["--config", os.path.abspath(self._config_path)]
        if self._fresh:
            cmd += ["--fresh"]
        if self._profile:
            cmd += ["--profile", self._profile]
        if self._from_stage:
            cmd += ["--from", self._from_stage]
        if self._only_stage:
            cmd += ["--only", self._only_stage]

        env = os.environ.copy()
        # 保证子进程用同一份配置/profile
        if self._config_path:
            env["PIPELINE_CONFIG"] = os.path.abspath(self._config_path)
        if self._profile:
            env["PIPELINE_PROFILE"] = self._profile
        # 关键：强制子进程用 UTF-8 编码 stdout/stderr。
        # Windows 默认 cp936(GBK) 无法编码流水线脚本里的 emoji（📋▶✅❌等），
        # 会在子进程 print() 时抛 UnicodeEncodeError 直接崩溃。
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        # 关键：强制 stdout 无缓冲。
        # 当 stdout 被 subprocess 管道捕获时（非 TTY），Python 默认用块缓冲（4KB+），
        # 导致 run_pipeline.py 的 print() 输出（如 [fresh] 归档日志、阶段标题）
        # 卡在缓冲区里，直到进程退出才一次性 flush——而 01a/01b/02 子进程的
        # logging 输出走 stderr（无缓冲/行缓冲），会先出现在 GUI 日志里，
        # 造成"没有归档日志就直接开始"的假象。设置 PYTHONUNBUFFERED=1 后，
        # run_pipeline.py 及所有子进程的 print()/stdout 都立即 flush。
        env["PYTHONUNBUFFERED"] = "1"

        print("=" * 70)
        print(f"▶ [GUI] 启动流水线：{' '.join(cmd)}")
        print("=" * 70)

        # text=True + bufsize=1 行缓冲，逐行实时读取
        proc = subprocess.Popen(
            cmd,
            cwd=base_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
        )

        # 用 Windows Job Object 绑定子进程树，stop() 时可一刀杀整树
        # （含 run_pipeline.py 派生的 stage01a/01b/02 子进程），
        # 防止 proc.terminate() 只杀顶层导致 stage 子进程变孤儿。
        # Job 创建/绑定失败时降级到 proc.terminate()（回到修复前行为）。
        job = self._create_job_with_kill_on_close()
        if job:
            try:
                # 注意：proc._handle 是 subprocess.Popen 的内部属性，指向子进程
                # 的 Windows HANDLE（不是 PID）。AssignProcessToJobObject 需要 HANDLE。
                ok = _kernel32.AssignProcessToJobObject(job, int(proc._handle))
                if not ok:
                    err = ctypes.get_last_error()
                    print(f"⚠️ [GUI] Job Object 绑定子进程失败（err={err}），"
                          f"已降级为单进程 terminate（子进程可能成为孤儿）")
                    _kernel32.CloseHandle(job)
                    job = None
            except Exception as e:
                print(f"⚠️ [GUI] Job Object 绑定异常：{e}，"
                      f"已降级为单进程 terminate（子进程可能成为孤儿）")
                try:
                    _kernel32.CloseHandle(job)
                except Exception:
                    pass
                job = None
        else:
            print("⚠️ [GUI] Job Object 创建失败，已降级为单进程 terminate"
                  "（子进程可能成为孤儿）")

        with self._lock:
            self._proc = proc
            self._job_handle = job

        assert proc.stdout is not None
        for line in proc.stdout:
            if self._stopped:
                break
            # 直接 print 到当前 sys.stdout（已被 LogTextbox 接管）
            print(line, end="")
            sys.stdout.flush()

        code = proc.wait()
        if self._stopped:
            print("\n⛔ [GUI] 流水线已被用户停止（已处理进度已保存，可断点续跑）。")
        else:
            print(f"\n✅ [GUI] 流水线结束，退出码 {code}")
        return code