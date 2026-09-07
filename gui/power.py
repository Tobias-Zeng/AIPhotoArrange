"""Windows 电源管理：阻止系统在流水线运行期间进入睡眠/休眠。

核心 API：SetThreadExecutionState，通过 ctypes 调用，无需额外依赖。
- prevent_sleep() 设置 ES_CONTINUOUS | ES_SYSTEM_REQUIRED，阻止系统睡眠
  （但允许屏幕关闭节能，适合无人值守跑流水线）。
- allow_sleep() 设置 ES_CONTINUOUS，清除上述标志、恢复默认行为。

设计要点：
- SetThreadExecutionState 是进程级、幂等的标志，重复调用无副作用；
  进程退出时 Windows 也会自动复位，显式 allow_sleep 只是良好实践。
- 非 Windows 平台函数为 no-op，保证可移植（虽然本项目只在 Win 跑）。
- 所有 ctypes 调用包 try/except 静默失败：防睡眠是锦上添花功能，
  绝不应因任何意外（API 缺失、权限问题等）影响流水线本身。
"""
from __future__ import annotations

import sys
import ctypes

# 执行状态标志位（Win32 API）
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001

# Windows 下加载 kernel32（其他平台 _kernel32 保持 None，函数走 no-op 分支）
_kernel32 = None
if sys.platform == "win32":
    try:
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # SetThreadExecutionState(ESFlags) -> DWORD（返回上一次状态，0 表示失败）
        # 签名：DWORD WINAPI SetThreadExecutionState(_In_ EXECUTION_STATE esFlags);
        _kernel32.SetThreadExecutionState.argtypes = [ctypes.c_ulong]
        _kernel32.SetThreadExecutionState.restype = ctypes.c_ulong
    except Exception:
        _kernel32 = None


def prevent_sleep() -> None:
    """阻止系统进入睡眠/休眠（允许屏幕关闭）。失败静默忽略。"""
    if _kernel32 is None:
        return
    try:
        _kernel32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
    except Exception:
        pass


def allow_sleep() -> None:
    """恢复系统默认睡眠行为。失败静默忽略。幂等，可重复调用。"""
    if _kernel32 is None:
        return
    try:
        _kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
    except Exception:
        pass
