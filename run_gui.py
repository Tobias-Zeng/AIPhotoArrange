#!/usr/bin/env python3
# run_gui.py
"""
AIPhotoArrange 桌面 GUI 启动入口。

用法：
    python run_gui.py                 # 使用脚本同目录的 pipeline_config.yaml
    python run_gui.py --config xxx.yaml

打包说明（Nuitka）：
    nuitka --standalone --enable-plugin=tk-inter --include-package=customtkinter \
           --include-package=ruamel.yaml --output-dir=build run_gui.py

exe 自调用协议（Nuitka --onefile 编译后）：
    aiphotoarrange.exe                              # 正常启动 GUI
    aiphotoarrange.exe --internal-run-pipeline ...  # 进程内跑 run_pipeline.main()
    aiphotoarrange.exe --internal-stage 01a ...     # 进程内跑单个阶段脚本
    这些内部模式由 GUI/PipelineRunner 自动调用，用户不直接使用；目的是让
    onefile exe 在不依赖外部 python 解释器的前提下串联三阶段流水线。
"""
import os
import sys

# Nuitka 编译后，__file__ 指向编译产物内部目录，外部资源在 .exe 同级。
# 开发模式下保持 __file__ 行为，与 pipeline_config_loader.BASE_DIR 一致。
if "__compiled__" in globals():          # Nuitka 编译后注入的全局标志
    BASE_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
else:                                     # 开发模式
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 确保项目根目录在 sys.path 最前，便于 gui 包导入（Nuitka 编译后也兼容）
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Nuitka console=disable 模式下（GUI 不显示黑窗口），双击启动时 sys.stdout /
# sys.stderr 为 None。此处用哑对象替换，防止 GUI 启动过程中（LogTextbox
# install 之前）任何 print() / logging.StreamHandler 访问 None 而崩溃。
# 子进程模式（--internal-run-pipeline / --internal-stage）的 stdout 是 Popen
# 管道，不为 None，不受影响。
class _NullStream:
    def write(self, _s): pass
    def flush(self): pass
    def isatty(self): return False
    @property
    def encoding(self): return "utf-8"
if sys.stdout is None:
    sys.stdout = _NullStream()  # type: ignore
if sys.stderr is None:
    sys.stderr = _NullStream()  # type: ignore


# GUI 主进程未捕获异常兜底：console=disable 的 exe 里 stderr 为哑对象，异常栈
# 界面和文件都看不到。此处把异常栈打到 sys.stderr——LogTextbox.install() 之后
# sys.stderr 已被接管，异常栈会既显示在界面又落盘到 gui_console_*.log。安装在
# 内部子命令分发之前，覆盖整个 GUI 生命周期（子进程模式不受影响，见下）。
def _log_uncaught(exc_type, exc_value, exc_tb):
    import traceback
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    try:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        sys.stderr.write("\n❌ [GUI] 未捕获异常，程序即将退出：\n" + text)
        sys.stderr.flush()
    except Exception:
        sys.__excepthook__(exc_type, exc_value, exc_tb)

sys.excepthook = _log_uncaught


def _dispatch_internal():
    """处理 exe 自调用的内部子命令，返回 True 表示已处理（应退出）。

    Nuitka onefile 编译后，sys.executable 指向临时解压目录里不存在的
    python.exe，无法用于启动子进程。GUI/PipelineRunner 改用 sys.argv[0]
    （即 .exe 本身）自调用，配合这些内部子命令在子进程内执行对应逻辑，
    完全不依赖外部 python 解释器。
    """
    argv = sys.argv[1:]
    # --internal-run-pipeline [--config x] [--fresh] [--profile P] [--skip-preflight]
    # 转发到 run_pipeline.main()，在子进程内编排三阶段
    if argv and argv[0] == "--internal-run-pipeline":
        import run_pipeline
        # 去掉内部标志，剩余参数交给 run_pipeline.parse_args
        sys.argv = [sys.argv[0]] + argv[1:]
        run_pipeline.main()
        return True
    # --internal-stage <01a|01b|02> [--config x] [--profile P]
    # run_pipeline.main() 内部会识别 --internal-stage 并 import 对应阶段
    if argv and argv[0] == "--internal-stage":
        import run_pipeline
        sys.argv = [sys.argv[0]] + argv
        run_pipeline.main()
        return True
    return False


from gui.app import main  # noqa: E402

if __name__ == "__main__":
    # 先处理 exe 自调用的内部子命令；命中则执行后退出，不启动 GUI
    if _dispatch_internal():
        raise SystemExit(0)
    raise SystemExit(main())