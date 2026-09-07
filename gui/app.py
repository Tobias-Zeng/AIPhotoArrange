"""AIPhotoArrange 主窗口。

布局：
- 左侧（配置区）：路径/城市/Key/Profile/Provider 表单 + 保存按钮
- 右侧（日志区）：实时日志框
- 底部（运行控制）：运行模式单选 + 大按钮"开始一键归档流水线" + 停止按钮

线程模型：
- 流水线在 PipelineRunner 的后台线程里跑，主线程只负责 UI。
- 完成回调通过 after() 切回主线程更新按钮状态，避免跨线程操作控件。
"""
from __future__ import annotations

import os
import re
import sys
import threading
from tkinter import filedialog, messagebox
from typing import Optional


try:
    import customtkinter as ctk
except ImportError as _e:  # pragma: no cover
    raise SystemExit("缺少依赖 customtkinter，请先 pip install customtkinter") from _e

from . import config_io
from .log_widget import LogTextbox
from .pipeline_runner import PipelineRunner
import pipeline_config_loader as _cfg_loader

# 默认配置文件路径：和 run_pipeline.py 一致，.exe 同级目录
BASE_DIR = _cfg_loader.BASE_DIR
DEFAULT_CONFIG_PATH = os.path.join(BASE_DIR, "pipeline_config.yaml")


ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")


def _enable_dpi_awareness() -> None:
    """Windows 下开启进程 DPI 感知，消除高分屏上的字体发虚/模糊，
    并让标题栏在运行时切换桌面缩放比例时跟随缩放。

    未开启时，Windows 会把整窗按位图放大，文字变糊。开启后由 Tk 按真实
    DPI 渲染，字体清晰。仅在 Windows 生效，其它平台静默跳过；任何异常都
    不影响启动。必须在创建 Tk 根窗口之前调用。

    优先级说明：
    - PerMonitorV2（Win10 1607+）：系统在运行时 DPI 变化时自动重缩放
      非客户区（标题栏/边框/系统按钮）。customtkinter 内部用的是 v1
      （SetProcessDpiAwareness(2)），v1 不自动重缩放标题栏，导致切换
      桌面缩放比例时标题栏保持首次打开的尺寸。此处抢先设 V2，CTk.__init__
      里随后调的 SetProcessDpiAwareness(2) 会静默失败（感知只能设一次），
      V2 保持生效。
    - 退回 v1（PROCESS_PER_MONITOR_DPI_AWARE，Win8.1+）：客户区清晰，
      标题栏不跟随运行时 DPI 变化（与改前行为一致）。
    - 再退回 System DPI aware（Vista+）。
    """
    if sys.platform != "win32":
        return
    import ctypes
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = (HANDLE)-4
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass  # 旧系统 (< Win10 1607) 无此 API，退回 v1
    try:
        # 2 = PROCESS_PER_MONITOR_DPI_AWARE（Win8.1+）
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            # 退回：System DPI aware（Vista+）
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class MultiDirDialog(ctk.CTkToplevel):
    """多文件夹选择对话框。

    tkinter 的 filedialog.askdirectory 不支持多选，本对话框用"逐个追加 +
    列表移除"的方式实现多目录选择。确定后返回 ";".join 的目录列表。

    打开时用当前 source_var 的值按 ";" 拆分预填列表，关闭时通过回调
    把结果写回（取消则不改动原值）。
    """

    def __init__(self, master, initial_value: str = "", on_confirm=None):
        super().__init__(master)
        self.title("选择多个源文件夹")
        self.geometry("640x480")
        self.minsize(560, 380)
        # 模态：抢焦点、置顶
        self.transient(master)
        self.grab_set()

        self._on_confirm = on_confirm
        self._dirs: list[str] = [
            p.strip() for p in (initial_value or "").split(";") if p.strip()
        ]
        # 记录本次对话框内上次添加的目录，作为下次 askdirectory 的初始目录，
        # 方便用户连续选择同一父目录下的多个子目录。
        self._last_added = ""

        self._build_ui()
        # 确保窗口居中到父窗口附近
        self.after(10, lambda: self.focus_force())

    def _build_ui(self) -> None:
        # 顶部工具栏：添加 + 清空
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=12, pady=(12, 6))

        ctk.CTkButton(
            top, text="+ 添加文件夹", width=140, height=32,
            command=self._on_add,
        ).pack(side="left", padx=(0, 8))

        # 次级按钮用浅灰实底 + 深色文字，避免 transparent 在浅色主题下看不清
        ctk.CTkButton(
            top, text="清空", width=80, height=32,
            fg_color=("#9ca3af", "#4b5563"),
            hover_color=("#6b7280", "#374151"),
            text_color=("#1f2937", "#ffffff"),
            command=self._on_clear,
        ).pack(side="left")

        hint = ctk.CTkLabel(
            self,
            text="点击「添加文件夹」逐个选择，每个文件夹都会递归扫描所有子文件夹。",
            anchor="w",
        )
        hint.pack(fill="x", padx=14, pady=(0, 4))

        # 中部：可滚动目录列表，每行一个目录 + 移除按钮
        self._list_frame = ctk.CTkScrollableFrame(self, label_text="")
        self._list_frame.pack(fill="both", expand=True, padx=12, pady=6)
        self._render_list()

        # 底部：确定 / 取消
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=12, pady=(6, 12))

        ctk.CTkButton(
            bottom, text="取消", width=100, height=34,
            fg_color=("#9ca3af", "#4b5563"),
            hover_color=("#6b7280", "#374151"),
            text_color=("#1f2937", "#ffffff"),
            command=self._on_cancel,
        ).pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            bottom, text="确定", width=100, height=34,
            command=self._on_ok,
        ).pack(side="right")

    def _render_list(self) -> None:
        """刷新目录列表显示。"""
        for child in self._list_frame.winfo_children():
            child.destroy()
        if not self._dirs:
            ctk.CTkLabel(
                self._list_frame,
                text="（尚未添加任何文件夹）",
                anchor="center",
            ).pack(fill="x", pady=20)
            return
        for i, d in enumerate(self._dirs):
            row = ctk.CTkFrame(self._list_frame, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(
                row, text=d, anchor="w",
            ).pack(side="left", fill="x", expand=True, padx=(0, 8))
            # 移除按钮用浅灰实底，默认状态清晰可辨
            ctk.CTkButton(
                row, text="移除", width=60, height=26,
                fg_color=("#9ca3af", "#4b5563"),
                hover_color=("#6b7280", "#374151"),
                text_color=("#1f2937", "#ffffff"),
                command=lambda idx=i: self._on_remove(idx),
            ).pack(side="right")

    def _on_add(self) -> None:
        # 初始目录优先用上次选择目录的父目录（方便连续选同一父目录下的多个子目录），
        # 首次添加或上次取消时回退到工作目录。
        d = filedialog.askdirectory(initialdir=self._last_added or os.getcwd())
        if d:
            # 归一化为系统分隔符（Windows 下 D:/x -> D:\x），与 config 既有风格一致
            d = os.path.normpath(d)
            # 去重：已存在的目录不再重复添加
            if d not in self._dirs:
                self._dirs.append(d)
            # 记住本次选择目录的父目录，作为下次弹框的初始目录
            self._last_added = os.path.dirname(d)
            self._render_list()

    def _on_remove(self, idx: int) -> None:
        if 0 <= idx < len(self._dirs):
            self._dirs.pop(idx)
            self._render_list()

    def _on_clear(self) -> None:
        self._dirs = []
        self._render_list()

    def _on_ok(self) -> None:
        # 归一化所有目录的分隔符（Windows 下转为反斜杠，与 config 既有风格一致）
        norm_dirs = [os.path.normpath(d) for d in self._dirs]
        # 嵌套目录检测：若存在父子关系，提示用户（重复文件已由扫描层去重，不阻止）
        nested = self._detect_nested(norm_dirs)
        if nested:
            lines = ["以下目录存在嵌套关系，重复照片在扫描时会自动去重：\n"]
            for parent, child in nested:
                lines.append(f"  • {child}\n    （已包含在 {parent} 中）")
            messagebox.showinfo("目录嵌套提示", "\n".join(lines))
        result = ";".join(norm_dirs)
        if self._on_confirm:
            try:
                self._on_confirm(result)
            except Exception:
                pass
        self.grab_release()
        self.destroy()

    @staticmethod
    def _detect_nested(dirs: list) -> list:
        """检测目录列表中的父子嵌套关系。

        返回 [(parent, child), ...]，parent 是包含 child 的父目录。
        用 os.path.commonpath 判断：child 的前缀等于 parent 即视为嵌套。
        """
        nested = []
        for i, a in enumerate(dirs):
            try:
                ra = os.path.realpath(a)
            except OSError:
                ra = a
            for j, b in enumerate(dirs):
                if i == j:
                    continue
                try:
                    rb = os.path.realpath(b)
                except OSError:
                    rb = b
                if ra == rb:
                    continue
                try:
                    common = os.path.commonpath([ra, rb])
                except ValueError:
                    # 跨盘符等无法比较的情况，跳过
                    continue
                # ra 是 rb 的父目录（commonpath == ra 且 ra 比 rb 短）
                if common == ra and len(ra) < len(rb):
                    nested.append((a, b))
        return nested

    def _on_cancel(self) -> None:
        self.grab_release()
        self.destroy()


class App(ctk.CTk):
    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH):
        super().__init__()
        self._set_window_icon()
        self.title("AIPhotoArrange 照片归档流水线")
        self.geometry("1180x760")
        self.minsize(1000, 640)

        self._config_path = config_path
        self._runner: PipelineRunner | None = None
        self._start_maximized = True  # 启动后自动最大化，CTk 缩放重算后仍需重新应用

        # 表单变量
        self._profile_var = ctk.StringVar()
        self._source_var = ctk.StringVar()
        self._target_var = ctk.StringVar()
        self._home_city_var = ctk.StringVar()
        self._province_var = ctk.StringVar()
        self._amap_key_var = ctk.StringVar()
        self._provider_var = ctk.StringVar()
        self._provider_model_var = ctk.StringVar()
        self._provider_api_key_var = ctk.StringVar()
        self._provider_base_url_var = ctk.StringVar()
        self._num_workers_var = ctk.StringVar(value="6")
        self._extraction_level_var = ctk.StringVar(value="A 精华档 ~20-40% 只要最精彩的")
        # 运行模式：fresh=全新跑 / resume=断点续跑 / only03a=仅合并日报(03a) / only03b=仅跨年聚合(03b)
        self._run_mode_var = ctk.StringVar(value="fresh")

        # 右侧进度概览状态：由日志行旁路解析驱动，不改变脚本输出本身。
        self._stage_order = ["01a", "01b", "02", "03a", "03b"]
        self._stage_names = {
            "01a": "照片切批",
            "01b": "地理编码",
            "02":  "AI 智能归档",
            "03a": "日报合并",
            "03b": "跨年聚合",
        }
        self._stage_states: dict[str, str] = {sid: "pending" for sid in self._stage_order}
        self._current_stage: str | None = None
        self._progress_total_batches: int | None = None
        self._progress_done_batches: int | None = None

        # 警告/错误计数（按阶段）：行钩子旁路解析 [WARNING]/[ERROR] 日志行，
        # 流水线结束时在右下角状态标签下方多显示一行提示，避免警告淹没在日志里。
        self._warn_stages: dict[str, int] = {}
        self._warn_total: int = 0

        # 状态点动画：running/stopping 时用旋转字符，其余状态静态 ●
        self._status_kind: str = "ready"
        self._status_text: str = "就绪"
        self._spinner_idx: int = 0
        self._spinner_after_id: Optional[str] = None

        self._build_ui()
        self._load_config_into_form()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # Tk 回调（事件/after/command）里的未捕获异常默认只打到真实 stderr，
        # console=disable 的 exe 里会被吞掉。改由此钩子打到已被 LogTextbox 接管的
        # stderr，异常栈既显示在界面又落盘到 gui_console_*.log。
        self.report_callback_exception = self._log_tk_exception
        # 初始最大化 + 3秒后释放标志，避免影响用户后续手动还原窗口
        self.after(100, self._maximize_window)
        self.after(3000, self._disable_start_maximized)

    def _set_window_icon(self) -> None:
        """设置窗口标题栏 / 任务栏图标为项目自带的 app.ico。

        覆盖 customtkinter 默认的羽毛笔图标。customtkinter 在 CTk.__init__
        里用 after(200) 延迟设置自带图标，且仅在用户未调用 iconbitmap 时
        才设。此处显式调 iconbitmap 会把 _iconbitmap_method_called 置 True，
        customtkinter 的延迟回调便不再覆盖。

        路径解析：BASE_DIR 已处理好开发模式（源码同级）与 Nuitka 打包模式
        （exe 同级）的差异，直接拼接 app.ico 即可。找不到时静默跳过，不
        阻止 GUI 启动（退回 customtkinter 默认图标或系统默认图标）。
        """
        ico_path = os.path.join(BASE_DIR, "app.ico")
        try:
            if os.path.isfile(ico_path):
                self.iconbitmap(ico_path)
        except Exception:
            pass

    def _disable_start_maximized(self) -> None:
        """3 秒后关闭启动最大化标志，之后不再覆盖用户的窗口状态操作。"""
        self._start_maximized = False

    def _maximize_window(self) -> None:
        """最大化窗口，兼容 Windows/Linux/macOS。"""
        self.update_idletasks()
        if sys.platform == "win32":
            self.state("zoomed")
        else:
            self.attributes("-zoomed", True)

    def _set_scaling(self, new_widget_scaling, new_window_scaling):
        """覆写 CTk 的 _set_scaling：父类会调用 geometry() 重设窗口尺寸，
        导致 zoomed 状态丢失（125%/150% 等 DPI 缩放场景必现）。
        在父类调用后立即重新应用最大化。"""
        super()._set_scaling(new_widget_scaling, new_window_scaling)
        if getattr(self, "_start_maximized", False):
            self._maximize_window()

    def _set_scaled_min_max(self):
        """覆写 CTk 的 _set_scaled_min_max：该方法在 _set_scaling 后延迟
        1 秒执行，可能再次影响窗口状态，同样重新应用最大化。"""
        super()._set_scaled_min_max()
        if getattr(self, "_start_maximized", False):
            self._maximize_window()

    # 提取档位：下拉展示文案 <-> 配置里存的单字母，双向映射
    _LEVEL_LABELS = {
        "A": "A 精华档 ~20-40% 只要最精彩的",
        "B": "B 纪念档 ~40-80% 有意义都留",
        "C": "C 归类档 ~100% 只分类不删",
    }

    def _level_code_from_label(self, label: str) -> str:
        """下拉展示文案 → 配置单字母。"""
        for code, text in self._LEVEL_LABELS.items():
            if text == label:
                return code
        c = (label or "A").strip().upper()[:1]
        return c if c in ("A", "B", "C") else "A"


    # =========================================================
    # UI 构建
    # =========================================================
    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # 顶部标题栏（带图标 + 副标题的现代头部）
        top = ctk.CTkFrame(self, corner_radius=0, height=68,
                           fg_color=("#2563eb", "#17233b"))
        top.grid(row=0, column=0, sticky="ew")
        top.grid_propagate(False)
        top.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(top, text="📷", font=ctk.CTkFont(size=28)).grid(
            row=0, column=0, rowspan=2, padx=(22, 12), pady=12)
        ctk.CTkLabel(
            top, text="AIPhotoArrange",
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color="#ffffff",
        ).grid(row=0, column=1, pady=(13, 0), sticky="sw")
        ctk.CTkLabel(
            top, text="照片智能归档流水线 · 一键整理你的照片库",
            font=ctk.CTkFont(size=12),
            text_color=("#dbeafe", "#9fb8dc"),
        ).grid(row=1, column=1, pady=(0, 13), sticky="nw")

        # 主体：左右两栏
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=14, pady=14)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        # 左：配置卡片（与右侧日志卡片同形式：圆角卡片 + 紧凑标题 + 内容区）
        left_card = ctk.CTkFrame(body, corner_radius=12, width=500)
        left_card.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        left_card.grid_propagate(False)
        left_card.grid_rowconfigure(1, weight=1)
        left_card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(left_card, text="⚙  参数配置", anchor="w",
                     font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, padx=12, pady=(8, 2), sticky="w")
        self._left = ctk.CTkScrollableFrame(
            left_card, corner_radius=0, fg_color="transparent",
        )
        self._left.grid(row=1, column=0, sticky="nsew", padx=4, pady=(2, 8))
        self._build_config_form(self._left)

        # 右：日志卡片
        right = ctk.CTkFrame(body, corner_radius=12)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_rowconfigure(0, weight=1)
        right.grid_columnconfigure(0, weight=1)
        self._build_log_area(right)

        # 底部运行控制
        bottom = ctk.CTkFrame(self, corner_radius=0, height=96)
        bottom.grid(row=2, column=0, sticky="ew")
        bottom.grid_propagate(False)
        bottom.grid_columnconfigure(4, weight=1)
        self._build_run_controls(bottom)

    # 统一控件尺寸，保证各行对齐、观感一致
    _ENTRY_H = 34
    # 标签列统一宽度：_make_section 给 col0 设 minsize，_add_label 的 hint wraplength
    # 也用它。四个 section 的 col0 强制等宽 -> 所有输入框左边缘对齐、宽度一致。
    # 取 160：让输入框更宽更好用，hint 文字在此宽度内换行；四区 col0 严格相等。
    _LABEL_COL_W = 160
    _HINT_COLOR = ("#6b7280", "#8b95a5")   # 说明文字：柔和灰
    _SECTION_TITLE_COLOR = ("#2563eb", "#5b8def")

    def _make_section(self, parent, icon: str, title: str) -> "ctk.CTkFrame":
        """在滚动区里放一张分组卡片，返回卡片内用于摆放字段的 body 容器。"""
        card = ctk.CTkFrame(parent, corner_radius=10,
                            fg_color=("#f4f6fb", "#20293a"))
        card.pack(fill="x", padx=10, pady=(4, 5))

        ctk.CTkLabel(
            card, text=f"{icon}  {title}",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=self._SECTION_TITLE_COLOR, anchor="w",
        ).pack(fill="x", padx=12, pady=(8, 2))

        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=12, pady=(0, 7))
        # col0 设 minsize 强制四个 section 的标签列等宽，所有输入框左边缘对齐、
        # 宽度一致；col1/col2 都设 weight=1 等宽拉伸，常驻城市双下拉也等宽占满。
        body.grid_columnconfigure(0, minsize=self._LABEL_COL_W)
        body.grid_columnconfigure(1, weight=1)
        body.grid_columnconfigure(2, weight=1)
        return body

    def _add_label(self, body, row: int, text: str, hint: str) -> int:
        """在某行左侧放「字段名 + 灰色小字说明」，返回该字段控件应占用的行号。

        字段名占一行，说明文字紧贴其下，控件放在右侧跨两行，视觉上更整齐。
        """
        ctk.CTkLabel(
            body, text=text, anchor="w",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).grid(row=row, column=0, padx=(0, 8), pady=(4, 0), sticky="w")
        if hint:
            ctk.CTkLabel(
                body, text=hint, anchor="w", justify="left",
                wraplength=self._LABEL_COL_W,
                font=ctk.CTkFont(size=12), text_color=self._HINT_COLOR,
            ).grid(row=row + 1, column=0, padx=(0, 8), pady=(0, 4), sticky="w")
        return row

    def _build_config_form(self, parent) -> None:
        # ============ Profile · 路径设置 ============
        # 以下三项（Profile、源文件夹、归档目标）随 Profile 切换；
        # 其余 section 为全局共享参数，不随 Profile 变化。
        sec = self._make_section(parent, "🗂", "Profile · 路径设置")
        self._add_label(sec, 0, "运行 Profile", "选择当前处理的照片批次；切换后仅载入该批次的输入/输出目录，其余参数全局共享")
        self._profile_menu = ctk.CTkOptionMenu(
            sec, variable=self._profile_var, values=[], height=self._ENTRY_H,
            command=self._on_profile_change,
        )
        self._profile_menu.grid(row=0, column=1, rowspan=2, columnspan=2, padx=0, pady=3, sticky="ew")

        self._add_label(sec, 2, "源文件夹路径", "存放待整理原始照片的文件夹（支持多个，每行一个；点「浏览」可逐个添加）")
        # 源文件夹用多行 Textbox 显示，每个目录占一行，避免多个 ; 分隔路径在单行里显示不全。
        # _source_var 仍是数据真相源（存 ; 分隔的单行串）：var 变化 -> 按行拆分刷新 Textbox；
        # Textbox 编辑失焦 -> 按行合并回写 var。加 border_width=2 与 CTkEntry 视觉粗细一致，
        # row 与 label 同行、rowspan=2 顶部对齐（与其余字段一致，避免输入框上方留白）。
        self._source_box = ctk.CTkTextbox(
            sec, height=self._ENTRY_H * 3, wrap="none",
            border_width=2, border_color=("#9aa3b5", "#565b6e"),
        )
        self._source_box.grid(
            row=2, column=1, rowspan=2, columnspan=2, padx=0, pady=3, sticky="ew")
        ctk.CTkButton(sec, text="浏览", width=80, height=self._ENTRY_H,
                       command=lambda: self._browse_multi_dirs(self._source_var)).grid(
            row=4, column=1, columnspan=2, padx=0, pady=(0, 3), sticky="e")
        # 初始填充 + 同步：var -> textbox
        self._sync_source_box()
        self._source_var.trace_add("write", lambda *_: self._sync_source_box())
        # textbox -> var（失焦时回写，避免编辑中途覆盖）
        self._source_box.bind("<FocusOut>", lambda *_: self._sync_source_var())

        self._add_label(sec, 5, "归档目标路径", "整理后照片输出到的文件夹")
        ctk.CTkEntry(sec, textvariable=self._target_var, height=self._ENTRY_H).grid(
            row=5, column=1, rowspan=2, columnspan=2, padx=0, pady=3, sticky="ew")
        ctk.CTkButton(sec, text="浏览", width=80, height=self._ENTRY_H,
                       command=lambda: self._browse_dir(self._target_var)).grid(
            row=7, column=1, columnspan=2, padx=0, pady=(0, 3), sticky="e")

        # ============ 地理位置 ============
        sec = self._make_section(parent, "📍", "地理位置")
        self._add_label(sec, 0, "常驻城市", "你的常住地。家附近会用更严的地标识别、日常地名也会被弱化，外出景点则保留全名，让照片命名更自然")
        self._city_groups = config_io.get_city_candidates_grouped()
        province_names = [p for p, _ in self._city_groups]
        self._province_menu = ctk.CTkOptionMenu(
            sec, variable=self._province_var, values=province_names,
            height=self._ENTRY_H, command=self._on_province_change,
        )
        self._province_menu.grid(row=0, column=1, rowspan=2, padx=(0, 6), pady=3, sticky="ew")
        self._city_menu = ctk.CTkOptionMenu(
            sec, variable=self._home_city_var, values=[], height=self._ENTRY_H,
            command=self._on_city_change,
        )
        self._city_menu.grid(row=0, column=2, rowspan=2, padx=0, pady=3, sticky="ew")

        self._add_label(sec, 2, "高德 Key", "高德地图 API 密钥，用于把照片 GPS 坐标转成地名")
        self._amap_entry = ctk.CTkEntry(
            sec, textvariable=self._amap_key_var, show="•", height=self._ENTRY_H,
        )
        self._amap_entry.grid(row=2, column=1, rowspan=2, columnspan=2, padx=0, pady=3, sticky="ew")

        # ============ AI大模型服务 ============
        sec = self._make_section(parent, "🤖", "AI大模型服务")
        self._add_label(sec, 0, "大模型服务商", "选择照片分析所用的大模型提供方")
        self._provider_menu = ctk.CTkOptionMenu(
            sec, variable=self._provider_var, values=[], height=self._ENTRY_H,
            command=self._on_provider_change,
        )
        self._provider_menu.grid(row=0, column=1, rowspan=2, columnspan=2, padx=0, pady=3, sticky="ew")

        self._add_label(sec, 2, "模型名", "调用的具体模型名ID，需支持图像识别")
        ctk.CTkEntry(sec, textvariable=self._provider_model_var, height=self._ENTRY_H).grid(
            row=2, column=1, rowspan=2, columnspan=2, padx=0, pady=3, sticky="ew")

        self._add_label(sec, 4, "大模型 API Key", "大模型服务商鉴权密钥")
        self._provider_key_entry = ctk.CTkEntry(
            sec, textvariable=self._provider_api_key_var, show="•", height=self._ENTRY_H,
        )
        self._provider_key_entry.grid(row=4, column=1, rowspan=2, columnspan=2, padx=0, pady=3, sticky="ew")

        self._add_label(sec, 6, "大模型服务地址", "大模型接口地址（base_url）")
        ctk.CTkEntry(sec, textvariable=self._provider_base_url_var, height=self._ENTRY_H).grid(
            row=6, column=1, rowspan=2, columnspan=2, padx=0, pady=3, sticky="ew")

        # ============ 运行参数 ============
        sec = self._make_section(parent, "⚡", "运行参数")
        self._add_label(sec, 0, "并发线程数", "同时处理的照片批次数")
        ctk.CTkEntry(sec, textvariable=self._num_workers_var,
                     height=self._ENTRY_H).grid(
            row=0, column=1, rowspan=2, columnspan=2, padx=0, pady=3, sticky="ew")

        self._add_label(sec, 2, "照片提取档位", "决定最终保留照片的比例松紧")
        ctk.CTkOptionMenu(
            sec, variable=self._extraction_level_var, height=self._ENTRY_H,
            values=list(self._LEVEL_LABELS.values()),
            command=self._on_extraction_level_change,
        ).grid(row=2, column=1, rowspan=2, columnspan=2, padx=0, pady=3, sticky="ew")

        # 档位提示：切档后必须 --fresh 重跑
        ctk.CTkLabel(
            sec,
            text="⚠ 切换档位后请用「全新跑 (--fresh)」重跑，否则已完成批次不会按新档位重评。",
            anchor="w", justify="left", wraplength=430,
            font=ctk.CTkFont(size=12),
            text_color=("#b45309", "#e0a350"),
        ).grid(row=4, column=0, columnspan=3, padx=0, pady=(0, 2), sticky="w")

        # ============ 保存按钮 ============
        self._save_btn = ctk.CTkButton(
            parent, text="💾  保存配置到 pipeline_config.yaml",
            height=40, font=ctk.CTkFont(size=14, weight="bold"),
            command=self._on_save,
        )
        self._save_btn.pack(fill="x", padx=10, pady=(5, 8))

    def _build_log_area(self, parent) -> None:
        bar = ctk.CTkFrame(parent, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        bar.grid_rowconfigure(1, weight=1)
        bar.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(bar, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew")
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text="📜  实时日志", anchor="w",
                     font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, padx=4, pady=4, sticky="w")
        ctk.CTkButton(head, text="清空", width=72, height=30, corner_radius=8,
                       fg_color="transparent", border_width=1,
                       text_color=("#374151", "#c7cfda"),
                       border_color=("#c9d0dc", "#3a4658"),
                       hover_color=("#e5e9f0", "#2b3446"),
                       command=self._on_clear_log).grid(row=0, column=1, padx=4, pady=4)

        self.log = LogTextbox(bar, height=400)
        self.log.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        self.log.install()
        # 注册行钩子：旁路解析脚本已有的阶段/进度/ETA 日志，驱动下方进度条
        self.log.set_line_hook(self._on_log_line)

        # 进度概览卡片（在日志下方）
        self._build_progress_panel(bar)

    def _build_progress_panel(self, parent) -> None:
        """日志下方的阶段进度概览：三个阶段徽标 + 进度条 + ETA/预计完成。

        进度数据全部来自对脚本已有日志行的解析（阶段标题、01b/02 的 ETA 行），
        不新增任何 ETA 计算，也不改变日志内容本身。
        """
        card = ctk.CTkFrame(parent, corner_radius=10,
                            fg_color=("#f4f6fb", "#20293a"))
        card.grid(row=2, column=0, sticky="ew", padx=4, pady=(6, 4))
        card.grid_columnconfigure(0, weight=1)

        # 顶行：标题 + 三个阶段徽标
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=12, pady=(9, 2))
        top.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(top, text="🚦  阶段进度", anchor="w",
                     font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=0, column=0, sticky="w")

        chips = ctk.CTkFrame(top, fg_color="transparent")
        chips.grid(row=0, column=1, sticky="e")
        self._stage_chips: dict[str, ctk.CTkLabel] = {}
        for i, sid in enumerate(self._stage_order):
            chip = ctk.CTkLabel(
                chips, text="", width=96, height=24, corner_radius=8,
                font=ctk.CTkFont(size=11, weight="bold"),
            )
            chip.grid(row=0, column=i, padx=4)
            self._stage_chips[sid] = chip

        # 进度条 + 百分比
        barrow = ctk.CTkFrame(card, fg_color="transparent")
        barrow.grid(row=1, column=0, sticky="ew", padx=12, pady=(2, 2))
        barrow.grid_columnconfigure(0, weight=1)
        self._progress_bar = ctk.CTkProgressBar(barrow, height=14, corner_radius=7)
        self._progress_bar.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self._progress_bar.set(0)
        self._progress_pct_lbl = ctk.CTkLabel(
            barrow, text="0%", width=52, anchor="e",
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self._progress_pct_lbl.grid(row=0, column=1, sticky="e")

        # 详情行：当前阶段说明 + ETA/预计完成
        self._progress_detail_lbl = ctk.CTkLabel(
            card, text="等待开始…", anchor="w", justify="left",
            font=ctk.CTkFont(size=11), text_color=self._HINT_COLOR,
        )
        self._progress_detail_lbl.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 9))

        self._refresh_stage_chips()

    # 阶段徽标配色：pending 灰 / running 蓝 / done 绿 / skipped 暗灰（未启用跳过）
    _CHIP_COLORS = {
        "pending": (("#e5e9f0", "#2b3446"), ("#6b7280", "#8b95a5")),
        "running": (("#2563eb", "#2563eb"), ("#ffffff", "#ffffff")),
        "done":    (("#16a34a", "#16a34a"), ("#ffffff", "#ffffff")),
        "skipped": (("#9ca3af", "#4b5563"), ("#ffffff", "#e5e7eb")),
    }

    def _refresh_stage_chips(self) -> None:
        for sid in self._stage_order:
            chip = self._stage_chips.get(sid)
            if chip is None:
                continue
            state = self._stage_states.get(sid, "pending")
            fg, tc = self._CHIP_COLORS.get(state, self._CHIP_COLORS["pending"])
            mark = {"pending": "○", "running": "▶", "done": "✓", "skipped": "∅"}.get(state, "○")
            chip.configure(
                text=f"{mark} {sid} {self._stage_names.get(sid, sid)}",
                fg_color=fg, text_color=tc,
            )

    # =========================================================
    # 进度解析：只读脚本已有的日志行，不新增任何 ETA 计算
    # =========================================================
    # 阶段标题：run_pipeline.py 里的  "▶ 阶段 01a：..."
    _RE_STAGE = re.compile(r"▶\s*阶段\s*(01a|01b|02|03a|03b)\b")
    # 01a 步骤标记：  "📂 第 1/3 步：..." / "🔢 第 2/3 步：..." / "✂️ 第 3/3 步：..."
    _RE_01A_STEP = re.compile(r"第\s*([123])/3\s*步")
    # 01a phash 计算进度（最耗时的循环）：  "   200/1234 | 缓存命中 100, 新算 100"
    _RE_01A_PHASH = re.compile(r"(\d+)/(\d+)\s*\|\s*缓存命中")
    # 01b 进度：  "[cur/total] 1.2 req/s, ETA 3.4min"
    _RE_01B = re.compile(
        r"\[(\d+)/(\d+)\].*?ETA\s*([\d.]+)\s*min", re.IGNORECASE
    )

    # 02 进度（完整）：  "⏱️ [进度] 12/345 批 (3.5%) ... ETA 1h2m（预计 06-12 18:30:00 完成）"
    _RE_02_FULL = re.compile(
        r"\[进度\]\s*(\d+)/(\d+)\s*批\s*\(([\d.]+)%\).*?"
        r"ETA\s*(.+?)（预计\s*(.+?)\s*完成）"
    )
    # 02 进度（样本不足）：  "⏱️ [进度] 已完成 3/345 批 | ... ETA 计算中（样本不足）"
    _RE_02_WARMUP = re.compile(r"\[进度\]\s*已完成\s*(\d+)/(\d+)\s*批")

    # 03 进度（完整）：  "⏱️ [进度] 3/15 日 (20.0%) | ... ETA 5m（预计 06-12 18:30:00 完成）"
    _RE_03_FULL = re.compile(
        r"\[进度\]\s*(\d+)/(\d+)\s*日\s*\(([\d.]+)%\).*?"
        r"ETA\s*(.+?)（预计\s*(.+?)\s*完成）"
    )
    # 03 进度（样本不足）：  "⏱️ [进度] 已处理 3/15 日 | ... ETA 计算中（样本不足）"
    _RE_03_WARMUP = re.compile(r"\[进度\]\s*已处理\s*(\d+)/(\d+)\s*日")

    # 03b 进度（完整）：  "⏱️ [进度] 3/15 主题 (20.0%) | ... ETA 5m（预计 ... 完成）"
    _RE_03B_FULL = re.compile(
        r"\[进度\]\s*(\d+)/(\d+)\s*主题\s*\(([\d.]+)%\).*?"
        r"ETA\s*(.+?)（预计\s*(.+?)\s*完成）"
    )
    # 03b 进度（样本不足）：  "⏱️ [进度] 已处理 3/15 主题 | ... ETA 计算中（样本不足）"
    _RE_03B_WARMUP = re.compile(r"\[进度\]\s*已处理\s*(\d+)/(\d+)\s*主题")
    # 03b 聚类阶段进度（完整）：  "⏱️ [进度] 3/14 聚类批 (21.4%) | ... ETA 5m（预计 ... 完成）"
    _RE_03B_CLUSTER_FULL = re.compile(
        r"\[进度\]\s*(\d+)/(\d+)\s*聚类批\s*\(([\d.]+)%\).*?"
        r"ETA\s*(.+?)（预计\s*(.+?)\s*完成）"
    )
    # 03b 聚类阶段进度（部分，无完整 ETA）：  "⏱️ [进度] 0/14 聚类批 (0.0%) | ..."
    _RE_03B_CLUSTER_PARTIAL = re.compile(r"\[进度\]\s*(\d+)/(\d+)\s*聚类批\s*\(([\d.]+)%\)")

    # 01a 三个子步骤的进度楼层（全局进度百分比下限）：
    #   1 扫描 -> 2 phash -> 3 切批，子进度只升不降。
    #   phash 实时进度映射到 [step2, step3) = [0.15, 0.90) 区间，
    #   避免其 done/total 从 ~0% 起步时压低已设的 15% 楼层造成回退。
    _01A_STEP_FLOOR = {1: 0.05, 2: 0.15, 3: 0.90}

    # 警告/错误行：匹配 logging 格式里的 "[WARNING]" / "[ERROR]" 级别标记。
    # 阶段脚本统一用 '%(asctime)s [%(levelname)s] %(message)s' 格式输出，
    # WARNING/ERROR 行经 sys.stderr 重定向进 GUI 日志框，并过行钩子。
    _RE_WARN = re.compile(r"\[(?:WARNING|ERROR)\]")

    def _on_log_line(self, line: str) -> None:
        """行钩子：主线程内被调用。解析阶段切换与进度/ETA，更新进度面板。"""
        try:
            self._parse_progress_line(line)
        except Exception:
            # 解析永不能影响日志显示
            pass

    def _parse_progress_line(self, line: str) -> None:
        # 1) 阶段标题：切换当前阶段，前面的阶段标记为完成
        m = self._RE_STAGE.search(line)
        if m:
            self._enter_stage(m.group(1))
            return

        # 1b) 警告/错误计数：匹配 logging 的 [WARNING]/[ERROR] 级别标记，
        #     归属到当前阶段（编排器先 print "▶ 阶段 XX" 再跑该阶段脚本，
        #     故警告到来时 _current_stage 必然已设置；防御性兜底归入 "?"）。
        #     不 return：少数警告行可能同时带进度信息，交由后续分支继续解析。
        if self._RE_WARN.search(line):
            sid = self._current_stage or "?"
            self._warn_stages[sid] = self._warn_stages.get(sid, 0) + 1
            self._warn_total += 1

        # 2) 01a phash 计算进度（最耗时的循环，如 "200/1234 | 缓存命中 ..."）
        #    子进度映射到 [step2, step3) = [0.15, 0.90) 区间，单调递增，
        #    避免起步 done/total≈0 时压低第 2 步的 15% 楼层造成回退。
        m = self._RE_01A_PHASH.search(line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            frac = (done / total) if total else 0
            lo = self._01A_STEP_FLOOR[2]   # 0.15
            hi = self._01A_STEP_FLOOR[3]   # 0.90
            frac = lo + (hi - lo) * frac
            self._set_progress(frac, f"{frac*100:.1f}%")
            self._set_progress_detail(f"照片切批：计算 phash {done}/{total} 张")
            return

        # 2b) 01a 步骤标记（扫描/计算/切批三步，给出阶段内文字反馈）
        m = self._RE_01A_STEP.search(line)
        if m:
            step = int(m.group(1))
            step_names = {1: "递归扫描 + 废片预筛", 2: "计算 phash", 3: "按日切批 + 合并"}
            # 01a 的扫描/切批步骤没有逐项 ETA 日志，给进度条一个保守的阶段内最低值，
            # 避免 01a 已开始但 GUI 长时间显示 0%。phash 循环进度会继续精确刷新。
            floor = self._01A_STEP_FLOOR.get(step, 0.0)
            self._set_progress(floor, f"{floor*100:.0f}%")
            self._set_progress_detail(
                f"照片切批：第 {step}/3 步 · {step_names.get(step, '')}"
            )
            return


        # 3) 02 完整进度行（含 ETA 与预计完成时刻）
        m = self._RE_02_FULL.search(line)

        if m:
            done, total = int(m.group(1)), int(m.group(2))
            pct = float(m.group(3))
            eta = m.group(4).strip()
            finish = m.group(5).strip()
            self._progress_done_batches, self._progress_total_batches = done, total
            self._set_progress(pct / 100.0, f"{pct:.1f}%")
            self._set_progress_detail(
                f"AI 智能归档：{done}/{total} 批 · 剩余 ETA {eta} · 预计 {finish} 完成"
            )
            return

        # 3) 02 预热行（样本不足，暂无 ETA）
        m = self._RE_02_WARMUP.search(line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            frac = (done / total) if total else 0
            self._set_progress(frac, f"{frac*100:.1f}%")
            self._set_progress_detail(
                f"AI 智能归档：{done}/{total} 批 · ETA 计算中（样本不足）"
            )
            return

        # 3b) 03 完整进度行（含 ETA 与预计完成时刻）
        m = self._RE_03_FULL.search(line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            pct = float(m.group(3))
            eta = m.group(4).strip()
            finish = m.group(5).strip()
            self._set_progress(pct / 100.0, f"{pct:.1f}%")
            self._set_progress_detail(
                f"日报合并：{done}/{total} 日 · 剩余 ETA {eta} · 预计 {finish} 完成"
            )
            return

        # 3c) 03 预热行（样本不足，暂无 ETA）
        m = self._RE_03_WARMUP.search(line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            frac = (done / total) if total else 0
            self._set_progress(frac, f"{frac*100:.1f}%")
            self._set_progress_detail(
                f"日报合并：{done}/{total} 日 · ETA 计算中（样本不足）"
            )
            return

        # 3d) 03b 完整进度行（含 ETA 与预计完成时刻）-- COPY 阶段，映射到 50%-100%
        m = self._RE_03B_FULL.search(line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            pct = float(m.group(3))
            eta = m.group(4).strip()
            finish = m.group(5).strip()
            # COPY 阶段映射到 50%-100% 区间
            frac = 0.5 + (pct / 100.0) * 0.5
            self._set_progress(frac, f"{frac*100:.1f}%")
            self._set_progress_detail(
                f"跨年聚合：复制主题 {done}/{total} · 剩余 ETA {eta} · 预计 {finish} 完成"
            )
            return

        # 3e) 03b 预热行（样本不足，暂无 ETA）-- COPY 阶段
        m = self._RE_03B_WARMUP.search(line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            frac_inner = (done / total) if total else 0
            frac = 0.5 + frac_inner * 0.5
            self._set_progress(frac, f"{frac*100:.1f}%")
            self._set_progress_detail(
                f"跨年聚合：复制主题 {done}/{total} · ETA 计算中（样本不足）"
            )
            return

        # 3f) 03b 聚类阶段完整进度 -- 映射到 0%-50%
        m = self._RE_03B_CLUSTER_FULL.search(line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            pct = float(m.group(3))
            eta = m.group(4).strip()
            finish = m.group(5).strip()
            # 聚类阶段映射到 0%-50% 区间
            frac = (pct / 100.0) * 0.5
            self._set_progress(frac, f"{frac*100:.1f}%")
            self._set_progress_detail(
                f"跨年聚合：LLM 聚类 {done}/{total} 批 · 剩余 ETA {eta} · 预计 {finish} 完成"
            )
            return

        # 3g) 03b 聚类阶段部分进度（无 ETA 或 ETA 未知）-- 映射到 0%-50%
        m = self._RE_03B_CLUSTER_PARTIAL.search(line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            pct = float(m.group(3))
            frac = (pct / 100.0) * 0.5
            self._set_progress(frac, f"{frac*100:.1f}%")
            self._set_progress_detail(
                f"跨年聚合：LLM 聚类 {done}/{total} 批 · ETA 计算中"
            )
            return

        # 4) 01b 进度行（含 req/s 与 ETA 分钟）
        m = self._RE_01B.search(line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            eta_min = float(m.group(3))
            frac = (done / total) if total else 0
            self._set_progress(frac, f"{frac*100:.1f}%")
            self._set_progress_detail(
                f"地理编码：{done}/{total} · 剩余 ETA {eta_min:.1f} 分钟"
            )
            return

    def _enter_stage(self, sid: str) -> None:
        """进入某阶段：把它之前的阶段全标记完成，它本身标记运行中。"""
        if sid not in self._stage_order:
            return
        idx = self._stage_order.index(sid)
        for i, s in enumerate(self._stage_order):
            if i < idx:
                # skipped（未启用跳过）的阶段保持 skipped，不强制改成 done
                if self._stage_states.get(s) != "skipped":
                    self._stage_states[s] = "done"
            elif i == idx:
                self._stage_states[s] = "running"
            else:
                # 后续阶段若已被标记 skipped（03 未启用），保留 skipped 状态
                # 不重置为 pending，避免流水线结束时被误标成 done（绿勾）
                if self._stage_states.get(s) != "skipped":
                    self._stage_states[s] = "pending"
        self._current_stage = sid
        self._refresh_stage_chips()
        # 阶段刚开始，进度条归零等待该阶段自己的进度行刷新
        self._set_progress(0, "0%")
        self._set_progress_detail(f"进入阶段 {sid} · {self._stage_names.get(sid, sid)}…")

    def _set_progress(self, frac: float, pct_text: str) -> None:
        try:
            frac = max(0.0, min(1.0, float(frac)))
        except Exception:
            frac = 0.0
        self._progress_bar.set(frac)
        self._progress_pct_lbl.configure(text=pct_text)

    def _set_progress_detail(self, text: str) -> None:
        self._progress_detail_lbl.configure(text=text)

    def _reset_progress(self) -> None:
        """开跑前复位进度面板。"""
        for sid in self._stage_order:
            self._stage_states[sid] = "pending"
        self._current_stage = None
        self._progress_total_batches = None
        self._progress_done_batches = None
        # 复位警告计数（每次开跑前清零，避免上一次运行的提示残留）
        self._warn_stages = {}
        self._warn_total = 0
        self._refresh_warn_hint()

        mode = self._run_mode_var.get()
        if mode == "only03a":
            # 「仅合并日报 (03a)」模式：01a/01b/02/03b 不跑，直接标 skipped；
            # 03a 是唯一要跑的阶段，标 pending。不看 daily_summary.enabled
            # （--only 03a 会绕过 enabled 检查，便于对历史归档目录单独跑 03a）。
            for sid in ("01a", "01b", "02", "03b"):
                self._stage_states[sid] = "skipped"
        elif mode == "only03b":
            # 「仅跨年聚合 (03b)」模式：01a/01b/02/03a 不跑，直接标 skipped；
            # 03b 是唯一要跑的阶段，标 pending。不看 yearly_summary.enabled
            # （--only 03b 会绕过 enabled 检查，便于对历史归档目录单独跑 03b）。
            for sid in ("01a", "01b", "02", "03a"):
                self._stage_states[sid] = "skipped"
        else:
            # fresh / resume 模式：03a/03b 阶段默认 disabled，若配置里
            # daily_summary.enabled != true，把 03a 标记为 skipped；
            # yearly_summary.enabled != true，把 03b 标记为 skipped（显示 ∅ 灰色），
            # 跑流水线时 run_pipeline 也会跳过它。
            try:
                import pipeline_config_loader as _pcl
                _cfg = _pcl.load_config(self._config_path)
                _daily = (_cfg or {}).get("daily_summary") or {}
                if not _daily.get("enabled", False):
                    self._stage_states["03a"] = "skipped"
                _yearly = (_cfg or {}).get("yearly_summary") or {}
                if not _yearly.get("enabled", False):
                    self._stage_states["03b"] = "skipped"
            except Exception:
                pass
        self._refresh_stage_chips()
        self._set_progress(0, "0%")
        self._set_progress_detail("准备启动流水线…")

    def _finish_progress(self, ok: bool) -> None:
        """流水线结束：成功则全部阶段标完成、进度拉满。"""
        if ok:
            for sid in self._stage_order:
                # skipped（未启用跳过）的阶段保持 skipped，不强制改成 done
                if self._stage_states.get(sid) != "skipped":
                    self._stage_states[sid] = "done"
            self._refresh_stage_chips()
            self._set_progress(1.0, "100%")
            self._set_progress_detail("全部阶段执行完毕 ✓")
        else:
            # 失败/停止：把当前运行中的阶段留在 running，给出提示
            self._set_progress_detail("已结束（未全部完成，可断点续跑）")

    def _build_run_controls(self, parent) -> None:
        # 运行模式三选一
        mode_frame = ctk.CTkFrame(parent, fg_color="transparent")
        mode_frame.grid(row=0, column=0, padx=16, pady=16, sticky="w")
        ctk.CTkLabel(mode_frame, text="运行模式：",
                     font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, padx=(0, 8))
        ctk.CTkRadioButton(
            mode_frame, text="全新跑 (--fresh)", variable=self._run_mode_var, value="fresh"
        ).grid(row=0, column=1, padx=6)
        ctk.CTkRadioButton(
            mode_frame, text="断点续跑", variable=self._run_mode_var, value="resume"
        ).grid(row=0, column=2, padx=6)
        ctk.CTkRadioButton(
            mode_frame, text="仅合并日报 (03a)", variable=self._run_mode_var, value="only03a"
        ).grid(row=0, column=3, padx=6)
        ctk.CTkRadioButton(
            mode_frame, text="仅跨年聚合 (03b)", variable=self._run_mode_var, value="only03b"
        ).grid(row=0, column=4, padx=6)

        # 状态标签 + 警告提示（放右侧，紧贴右下角）：用一个竖向 frame
        # 把"● 完成"状态点和下方的"⚠️ xx阶段有警告"提示叠在一起。
        status_frame = ctk.CTkFrame(parent, fg_color="transparent")
        status_frame.grid(row=0, column=3, padx=12, pady=16, sticky="e")

        self._status_lbl = ctk.CTkLabel(
            status_frame, text="● 就绪", anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=("#16a34a", "#4ade80"),
        )
        self._status_lbl.grid(row=0, column=0, sticky="e")

        # 警告提示标签：默认隐藏，仅在运行结束且中途有 [WARNING]/[ERROR] 时显示。
        self._warn_lbl = ctk.CTkLabel(
            status_frame, text="", anchor="w",
            font=ctk.CTkFont(size=12),
            text_color=("#d97706", "#e0a350"),  # 橙色，与 stopping 状态一致
        )
        # 初始不占位；有警告时再 grid 出来，避免空行撑高底部控件栏。
        self._warn_lbl.grid_remove()

        # 停止按钮
        self._stop_btn = ctk.CTkButton(
            parent, text="⏹  停止", width=120, height=48, corner_radius=10,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="#dc2626", hover_color="#b91c1c", state="disabled",
            command=self._on_stop,
        )
        self._stop_btn.grid(row=0, column=1, padx=8, pady=16)

        # 大按钮
        self._run_btn = ctk.CTkButton(
            parent, text="▶  开始一键归档流水线",
            font=ctk.CTkFont(size=16, weight="bold"),
            height=52, width=280, corner_radius=12,
            fg_color="#16a34a", hover_color="#15803d",
            command=self._on_run,
        )
        self._run_btn.grid(row=0, column=2, padx=8, pady=16)

    # =========================================================
    # 配置加载 / 保存
    # =========================================================
    def _load_config_into_form(self) -> None:
        try:
            form = config_io.load_for_ui(self._config_path)
        except Exception as e:
            messagebox.showerror("配置读取失败", f"{e}\n路径：{self._config_path}")
            return

        # profile 下拉
        profiles = form.profiles or []
        self._profile_menu.configure(values=profiles if profiles else ["(无)"])
        self._profile_var.set(form.profile or "")

        self._source_var.set(form.source_dir)
        self._target_var.set(form.target_dir)
        self._home_city_var.set(form.home_city)
        # 根据已保存的城市反查省份，联动二级下拉
        province = config_io.find_province_for_city(form.home_city)
        self._province_var.set(province)
        self._update_city_menu_for_province(province)

        self._amap_key_var.set(form.amap_key)

        # provider 下拉（用配置里实际有的 provider_configs key）
        providers = self._read_provider_names()
        self._provider_menu.configure(values=providers if providers else ["ollama"])
        self._provider_var.set(form.provider or "ollama")
        self._provider_model_var.set(form.provider_model)
        self._provider_api_key_var.set(form.provider_api_key)
        self._provider_base_url_var.set(form.provider_base_url)
        self._num_workers_var.set(form.num_workers)
        # 照片提取档位：把配置里的单字母映射回下拉展示文案
        self._extraction_level_var.set(
            self._LEVEL_LABELS.get(form.extraction_level, self._LEVEL_LABELS["A"])
        )

    def _read_provider_names(self) -> list:
        """从原始配置读 provider_configs 的 key 列表（避免依赖合并后的数据）。"""

        try:
            y, data = config_io._read_raw(self._config_path)
            if not data:
                return []
            pcs = (
                (data.get("curator") or {}).get("provider_configs") or {}
            )
            return list(pcs.keys())
        except Exception:
            return []

    def _collect_form(self) -> config_io.UIForm:
        raw = (self._num_workers_var.get() or "").strip()
        try:
            nw = int(raw)
            if nw < 1:
                raise ValueError
        except Exception:
            nw = 6
            self.log.append(
                f"⚠ [GUI] 并发线程数「{raw}」非法，已临时使用默认值 6；"
                f"请改为正整数后重新保存。\n",
                tag="manual",
            )
        # max_image_size 已从 GUI 移除，保留配置文件中的值（save 时不覆盖）；
        # 此处填 0 占位，config_io.save_from_ui 会跳过该字段。
        return config_io.UIForm(
            profile=self._profile_var.get().strip(),
            profiles=config_io.list_profiles(self._config_path),
            source_dir=self._source_var.get().strip(),
            target_dir=self._target_var.get().strip(),
            home_city=self._home_city_var.get().strip(),
            amap_key=self._amap_key_var.get().strip(),
            provider=self._provider_var.get().strip() or "ollama",
            provider_model=self._provider_model_var.get().strip(),
            provider_api_key=self._provider_api_key_var.get().strip(),
            provider_base_url=self._provider_base_url_var.get().strip(),
            num_workers=nw,
            max_image_size=0,
            extraction_level=self._level_code_from_label(self._extraction_level_var.get()),
        )


    def _on_save(self) -> None:
        form = self._collect_form()
        try:
            config_io.save_from_ui(self._config_path, form)
        except Exception as e:
            messagebox.showerror("配置保存失败", f"{e}")
            return
        self.log.append(f"💾 [GUI] 配置已保存到 {self._config_path}\n", tag="manual")
        messagebox.showinfo("已保存", "配置已写回 pipeline_config.yaml")

    # =========================================================
    # 交互回调
    # =========================================================
    def _on_province_change(self, _choice: str) -> None:
        """切换省份：更新城市下拉的候选列表。"""
        province = self._province_var.get()
        self._update_city_menu_for_province(province)
        # 自动选中该省第一个城市（避免显示旧省份的城市）
        cities = self._city_menu.cget("values")
        if cities:
            self._home_city_var.set(cities[0])

    def _on_city_change(self, _choice: str) -> None:
        """切换常驻城市：记录日志。省份联动会自动 set 城市从而触发本回调。"""
        city = self._home_city_var.get()
        if city:
            self.log.append(f"🔄 [GUI] 切换常驻城市：{city}\n", tag="manual")

    def _on_extraction_level_change(self, _choice: str) -> None:
        """切换照片提取档位：记录日志，并提醒需用「全新跑」重跑。"""
        label = self._extraction_level_var.get()
        code = self._level_code_from_label(label)
        self.log.append(
            f"🔄 [GUI] 切换提取档位：{code} 档（{label}）—— 请用「全新跑 (--fresh)」重跑，否则已完成批次不会按新档位重评\n",
            tag="manual")

    def _update_city_menu_for_province(self, province: str) -> None:
        """根据省份名更新城市下拉的候选列表。"""
        for p, cities in self._city_groups:
            if p == province:
                self._city_menu.configure(values=cities)
                return
        self._city_menu.configure(values=[])

    def _browse_dir(self, var: ctk.StringVar) -> None:
        d = filedialog.askdirectory(initialdir=var.get() or os.getcwd())
        if d:
            # 归一化为系统分隔符（Windows 下 D:/x -> D:\x），与 MultiDirDialog 及 config 既有风格一致
            var.set(os.path.normpath(d))

    def _browse_multi_dirs(self, var: ctk.StringVar) -> None:
        """打开多文件夹选择对话框，确定后把 ";".join 的结果写回 var。

        源文件夹支持多选（每个都会递归扫描所有子文件夹），结果用 ";" 分隔
        存入同一个 source_dir 字符串字段，下游 scan_all_photos /
        compute_source_dir_snapshot 已支持解析 ";" 分隔的多目录。
        """
        def _on_confirm(result: str) -> None:
            var.set(result)
        MultiDirDialog(
            master=self,
            initial_value=var.get(),
            on_confirm=_on_confirm,
        )

    def _sync_source_box(self) -> None:
        """_source_var -> Textbox 同步：var 变化时把值按 ; 拆分多行写进 Textbox。

        var 存的是 ";" 分隔的单行串（与配置/下游一致），Textbox 里每个目录
        占一行（换行符 \\n）便于查看。用标志位防止 Textbox 回写 var 时又
        触发本回调形成循环。
        """
        if not hasattr(self, "_source_box"):
            return
        # 把 ; 分隔串拆成多行（strip + 过滤空行），与 Textbox 当前内容比对
        val = self._source_var.get()
        lines = [p.strip() for p in val.split(";") if p.strip()]
        want = "\n".join(lines)
        current = self._source_box.get("1.0", "end-1c")
        if current == want:
            return
        self._syncing_source = True
        try:
            self._source_box.delete("1.0", "end")
            if want:
                self._source_box.insert("1.0", want)
        finally:
            self._syncing_source = False

    def _sync_source_var(self) -> None:
        """Textbox -> _source_var 同步：用户编辑 Textbox 失焦时按行合并回写 var。

        Textbox 里每行一个目录，合并成 ";" 分隔的单行串写回 var，使
        _collect_form 读 var.get() 能拿到 ";" 串（与配置/下游一致）。
        用标志位防止 var trace 又触发 _sync_source_box 抢光标。
        """
        if getattr(self, "_syncing_source", False):
            return
        raw = self._source_box.get("1.0", "end-1c")
        # 按行拆分，strip 每行，过滤空行，合并为 ; 分隔串
        lines = [p.strip() for p in raw.split("\n") if p.strip()]
        want = ";".join(lines)
        if self._source_var.get() != want:
            self._source_var.set(want)

    def _on_profile_change(self, _choice: str) -> None:
        """切换 profile：只改顶层 profile 字段，再仅载入新 profile 的输入/输出目录。

        全局参数（城市、高德 Key、大模型服务、运行参数）保留表单当前值不动，
        符合「Profile 只切换输入/输出目录，其余参数全局共享」的口径；也避免
        用户在表单里改了但没保存的全局参数被磁盘旧值冲掉。

        注意：不能用 save_from_ui 保存当前表单，否则会把旧 profile 的
        source_dir/target_dir 误写到新 profile 段里。用 set_active_profile
        只改 profile 字段，目录从配置文件重新读取。
        """
        new_profile = self._profile_var.get().strip()
        try:
            config_io.set_active_profile(self._config_path, new_profile)
            # 读新 profile 合并后的有效值，只取目录两项覆盖表单
            form = config_io.load_for_ui(self._config_path)
        except Exception as e:
            self.log.append(f"⚠️ [GUI] 切换 profile 失败：{e}\n", tag="manual")
            return
        self._source_var.set(form.source_dir)
        self._target_var.set(form.target_dir)
        self.log.append(f"🔄 [GUI] 切换到 profile：{new_profile or '(默认)'}（已载入该批次的输入/输出目录，全局参数不变）\n",
                        tag="manual")

    def _on_provider_change(self, _choice: str) -> None:
        """切换 provider：重新加载该 provider 的连接配置。"""
        try:
            form = config_io.load_for_ui(self._config_path)
            # 强制用新选中的 provider 读一次
            form.provider = self._provider_var.get()
            # 重新读该 provider 的配置项
            y, data = config_io._read_raw(self._config_path)
            if data:
                pc = (
                    (data.get("curator") or {}).get("provider_configs") or {}
                ).get(form.provider, {}) or {}
                form.provider_model = pc.get("model", "") or ""
                form.provider_api_key = pc.get("api_key", "") or ""
                form.provider_base_url = pc.get("base_url", "") or ""
        except Exception:
            return
        self._provider_model_var.set(form.provider_model)
        self._provider_api_key_var.set(form.provider_api_key)
        self._provider_base_url_var.set(form.provider_base_url)
        self.log.append(f"🔄 [GUI] 切换到大模型服务商：{form.provider}\n", tag="manual")

    def _on_clear_log(self) -> None:
        self.log.clear()

    # =========================================================
    # 运行控制
    # =========================================================
    def _on_run(self) -> None:
        if self._runner and self._runner.is_running():
            return

        mode = self._run_mode_var.get()
        form = self._collect_form()

        # 三种模式都先保存配置，保证子进程（stage03a/03b 在 import 时读配置文件）
        # 读到最新的 target_dir / profile 等值。save_from_ui 只写
        # common/geo/curator 段，不碰 daily_summary/yearly_summary.enabled，安全。
        try:
            config_io.save_from_ui(self._config_path, form)
        except Exception as e:
            if not messagebox.askyesno("配置保存失败", f"{e}\n仍要继续运行吗？"):
                return

        # 「仅合并日报 (03a)」/「仅跨年聚合 (03b)」模式：不校验 source_dir、不走指纹校验，
        # 仅基于当前表单的 target_dir 单跑对应阶段。
        if mode == "only03a":
            self._on_run_only03a(form)
            return
        if mode == "only03b":
            self._on_run_only03b(form)
            return

        # fresh / resume 模式：校验必填
        if not form.source_dir or not form.target_dir:
            messagebox.showwarning("参数缺失", "请先填写源文件夹和归档目标路径")
            return

        self._reset_progress()
        self._set_running_ui(True)
        # 断点续跑模式（resume）下，若 02_progress 上下文指纹与当前表单完全匹配，
        # 则跳过 01a/01b 直接从 02 续跑，避免白白重跑前置阶段。
        # 任何不确定情况（文件不存在/解析失败/指纹缺失或部分不匹配）都不跳过，
        # 走全流程让 02 自己的 load_progress 处理自愈。
        if mode == "resume":
            # 第一步：5 字段快速校验（毫秒级，同步）
            stored_fp = self._quick_fingerprint_check(form)
            if stored_fp is not None:
                # 5 字段匹配，需进一步校验输入目录内容快照（耗时 10-30 秒）
                # 放后台线程扫描，期间给用户提示，扫描完再启动流水线
                self._async_check_source_dir_and_start(form, stored_fp)
                return
        # fresh / 5 字段不匹配 -> 直接全流程启动
        self._start_runner(form, from_stage=None)

    def _on_run_only03a(self, form: config_io.UIForm) -> None:
        """「仅合并日报 (03a)」模式：基于已归档目录单跑 03a 阶段。

        - 配置已由 _on_run 统一保存（含 target_dir），子进程读到最新值
        - 不校验 source_dir、不走指纹校验
        - 检查 target_dir 存在且含至少一个 YYYY-MM-DD-* 子目录
        - 自动清空 03a_progress 里的 failed_dates（一并重试，不弹窗）
        - 01a/01b/02/03b 标记为 skipped，03a 标记为 pending
        """
        target_dir = form.target_dir.strip()
        if not target_dir:
            messagebox.showwarning("参数缺失", "请先填写归档目标路径")
            return
        if not os.path.isdir(target_dir):
            messagebox.showwarning(
                "目录不存在",
                f"归档目标路径不存在：\n{target_dir}\n\n请先跑完 01+02 生成事件子文件夹。"
            )
            return
        # 扫描 target_dir 是否含至少一个 YYYY-MM-DD-* 子目录
        import re as _re
        date_event_re = _re.compile(r'^\d{4}-\d{2}-\d{2}-.+$')
        has_event = any(
            date_event_re.match(name) and os.path.isdir(os.path.join(target_dir, name))
            for name in os.listdir(target_dir)
        )
        if not has_event:
            messagebox.showwarning(
                "无事件可合并",
                f"归档目录下没有 YYYY-MM-DD-* 命名的事件子文件夹：\n{target_dir}\n\n"
                f"请先跑完 02 阶段生成事件子文件夹。"
            )
            return

        # 自动清空 failed_dates（一并重试，不弹窗）
        self._clear_03a_failed_dates(form.profile)

        self._reset_progress()
        self._set_running_ui(True)
        self._start_runner(form, from_stage=None, only_stage="03a")

    def _on_run_only03b(self, form: config_io.UIForm) -> None:
        """「仅跨年聚合 (03b)」模式：基于已归档目录单跑 03b 阶段。

        - 配置已由 _on_run 统一保存（含 target_dir），子进程读到最新值
        - 不校验 source_dir、不走指纹校验
        - 检查 target_dir 存在且含至少一个 YYYY-MM-DD-* 子目录（支持年份分组布局：
          顶层是年份分组目录、事件目录在下一级）
        - 自动清空 03b_progress 里的 failed_themes（一并重试，不弹窗）
        - 01a/01b/02/03a 标记为 skipped，03b 标记为 pending
        """
        target_dir = form.target_dir.strip()
        if not target_dir:
            messagebox.showwarning("参数缺失", "请先填写归档目标路径")
            return
        if not os.path.isdir(target_dir):
            messagebox.showwarning(
                "目录不存在",
                f"归档目标路径不存在：\n{target_dir}\n\n请先跑完 01+02 生成事件子文件夹。"
            )
            return
        # 扫描 target_dir 是否含至少一个 YYYY-MM-DD-* 子目录（支持年份分组布局）
        import re as _re
        date_event_re = _re.compile(r'^\d{4}-\d{2}-\d{2}-.+$')
        has_event = any(
            date_event_re.match(name) and os.path.isdir(os.path.join(target_dir, name))
            for name in os.listdir(target_dir)
        )
        if not has_event:
            # 顶层无事件目录，往下一级扫（年份分组目录）
            for name in os.listdir(target_dir):
                sub_dir = os.path.join(target_dir, name)
                if not os.path.isdir(sub_dir) or name.startswith('.') or name.startswith('_'):
                    continue
                try:
                    has_event = any(
                        date_event_re.match(sub) and os.path.isdir(os.path.join(sub_dir, sub))
                        for sub in os.listdir(sub_dir)
                    )
                except OSError:
                    pass
                if has_event:
                    break
        if not has_event:
            messagebox.showwarning(
                "无事件可聚合",
                f"归档目录下没有 YYYY-MM-DD-* 命名的事件子文件夹：\n{target_dir}\n\n"
                f"请先跑完 02 阶段生成事件子文件夹。"
            )
            return

        # 自动清空 failed_themes（一并重试，不弹窗）
        self._clear_03b_failed_themes(form.profile)

        self._reset_progress()
        self._set_running_ui(True)
        self._start_runner(form, from_stage=None, only_stage="03b")

    def _clear_03a_failed_dates(self, profile: str) -> None:
        """读 03a_progress_<profile>.json，若 failed_dates 非空则清空写回。

        用于「仅合并日报 (03a)」模式：让之前失败的日期一并重试。
        03a 脚本的 main() 会过滤掉 failed_dates，必须先清空才能重跑。
        """
        import json as _json
        try:
            cfg = _cfg_loader.load_config(self._config_path)
            files = _cfg_loader.resolve_filenames(cfg)
            progress_path = os.path.join(_cfg_loader.BASE_DIR, files["daily_summary_progress_file"])
        except Exception as e:
            print(f"⚠️ [GUI] 读取 03a 进度文件路径失败，跳过 failed_dates 清空：{e}")
            return
        if not os.path.exists(progress_path):
            return  # 无进度文件，首次跑 03a，无需清空
        try:
            with open(progress_path, "r", encoding="utf-8") as f:
                data = _json.load(f)
        except Exception as e:
            print(f"⚠️ [GUI] 解析 03a 进度文件失败，跳过 failed_dates 清空：{e}")
            return
        failed = data.get("failed_dates") or []
        if not failed:
            return  # 无失败日期，无需清空
        data["failed_dates"] = []
        try:
            tmp = progress_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, progress_path)
            print(f"🔄 [GUI] 已清空 03a 进度里的 failed_dates（{len(failed)} 天），将一并重试：{', '.join(failed)}")
        except Exception as e:
            print(f"⚠️ [GUI] 写回 03a 进度文件失败，failed_dates 未清空：{e}")

    def _clear_03b_failed_themes(self, profile: str) -> None:
        """读 03b_progress_<profile>.json，若 failed_themes 非空则清空写回。

        用于「仅跨年聚合 (03b)」模式：让之前失败的主题一并重试。
        03b 脚本的 main() 会过滤掉 failed_themes，必须先清空才能重跑。
        """
        import json as _json
        try:
            cfg = _cfg_loader.load_config(self._config_path)
            files = _cfg_loader.resolve_filenames(cfg)
            progress_path = os.path.join(_cfg_loader.BASE_DIR, files["yearly_summary_progress_file"])
        except Exception as e:
            print(f"⚠️ [GUI] 读取 03b 进度文件路径失败，跳过 failed_themes 清空：{e}")
            return
        if not os.path.exists(progress_path):
            return  # 无进度文件，首次跑 03b，无需清空
        try:
            with open(progress_path, "r", encoding="utf-8") as f:
                data = _json.load(f)
        except Exception as e:
            print(f"⚠️ [GUI] 解析 03b 进度文件失败，跳过 failed_themes 清空：{e}")
            return
        failed = data.get("failed_themes") or []
        if not failed:
            return  # 无失败主题，无需清空
        data["failed_themes"] = []
        try:
            tmp = progress_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, progress_path)
            print(f"🔄 [GUI] 已清空 03b 进度里的 failed_themes（{len(failed)} 个），将一并重试：{', '.join(failed)}")
        except Exception as e:
            print(f"⚠️ [GUI] 写回 03b 进度文件失败，failed_themes 未清空：{e}")

    def _start_runner(self, form: config_io.UIForm, from_stage: Optional[str],
                      only_stage: Optional[str] = None) -> None:
        """实际启动 PipelineRunner。"""
        # 异步校验输入目录后状态会停在"校验输入目录中…"，启动子进程前重置回"运行中…"。
        self._set_status("运行中…", "running")
        self._runner = PipelineRunner(
            config_path=self._config_path,
            fresh=(self._run_mode_var.get() == "fresh"),
            profile=form.profile or None,
            on_done=lambda code: self.after(0, lambda: self._on_pipeline_done(code)),
            from_stage=from_stage,
            only_stage=only_stage,
        )
        self._runner.start()

    def _quick_fingerprint_check(self, form: config_io.UIForm) -> Optional[dict]:
        """
        断点续跑模式下的快速指纹校验（5 字段，毫秒级）。

        判定条件（全部满足才返回 stored fingerprint 字典）：
        1. 02_progress_<profile>.json 存在；
        2. 文件可解析为 JSON；
        3. 顶层 context_fingerprint 字典存在；
        4. source_dir / target_dir / extraction_level / profile / home_city
           5 个字段全部与当前表单一致。

        任一不满足返回 None（不跳过，走全流程）。
        source_dir_snapshot_hash 字段的校验不在这里做（需扫描 source_dir，
        耗时 10-30 秒），由 _async_check_source_dir_and_start 在后台线程完成。
        """
        import json as _json
        try:
            cfg = _cfg_loader.load_config(self._config_path)
            files = _cfg_loader.resolve_filenames(cfg)
            progress_path = os.path.join(_cfg_loader.BASE_DIR, files["progress_file"])
        except Exception:
            return None
        if not os.path.exists(progress_path):
            return None
        try:
            with open(progress_path, "r", encoding="utf-8") as f:
                data = _json.load(f)
        except Exception as e:
            print(f"⚠️ [GUI] 读取 02 进度文件失败，不跳过前置阶段：{e}")
            return None
        fp = data.get("context_fingerprint")
        if not isinstance(fp, dict):
            # 旧版本文件或字段缺失，让 02 自己自愈
            return None

        current = {
            "source_dir": form.source_dir,
            "target_dir": form.target_dir,
            "extraction_level": form.extraction_level,
            "profile": form.profile,
            "home_city": form.home_city,
        }
        for k, new_val in current.items():
            if fp.get(k) != new_val:
                # 指纹不匹配，不跳过；02 的 load_progress 会自愈归档旧文件
                return None
        return fp

    def _async_check_source_dir_and_start(self, form: config_io.UIForm, stored_fp: dict) -> None:
        """
        后台线程扫描 source_dir 算内容快照，与旧 progress 里的
        source_dir_snapshot_hash 对比。扫描完成后在 GUI 主线程启动流水线。

        扫描期间给用户提示（"正在校验输入目录..."），不冻结界面。
        一定要等扫描出结果再启动流水线（用户要求）。

        - 快照匹配 -> from_stage="02"（跳过 01a/01b）
        - 快照不匹配 / 旧 progress 无此字段 / 扫描异常 -> from_stage=None（全流程）
          （走全流程让 01a 重算 batches，02 的 load_progress 会自愈归档旧 progress）
        """
        stored_hash = stored_fp.get("source_dir_snapshot_hash")
        if not stored_hash:
            # 旧版本 progress 无此字段，保守走全流程
            print("⚠️ [GUI] 旧进度文件无输入目录快照字段，走全流程（01a 重算 + 02 自愈）")
            self._start_runner(form, from_stage=None)
            return

        print("=" * 70)
        print("🔄 [GUI] 5 项指纹匹配，正在扫描输入目录校验内容是否变化...")
        print("        （大目录可能需要 10-30 秒，请稍候）")
        print("=" * 70)
        self._set_status("校验输入目录中…", "running")

        def _scan_and_start():
            try:
                snapshot = _cfg_loader.compute_source_dir_snapshot(form.source_dir)
                current_hash = snapshot.get("hash")
            except Exception as e:
                print(f"⚠️ [GUI] 扫描输入目录异常：{e}，走全流程")
                self.after(0, lambda: self._start_runner(form, from_stage=None))
                return

            if current_hash == stored_hash:
                print("=" * 70)
                print(f"🔄 [GUI] 输入目录内容未变化，跳过 01a/01b，直接从 02 续跑。")
                print(f"        （快照 {snapshot['file_count']} 文件，hash={current_hash[:16]}...）")
                print("=" * 70)
                self.after(0, lambda: self._start_runner(form, from_stage="02"))
            else:
                print("=" * 70)
                print(f"⚠️ [GUI] 输入目录内容自上次中断后已变化，走全流程：")
                print(f"        旧快照 hash: {stored_hash[:16]}...")
                print(f"        新快照 hash: {current_hash[:16] if current_hash else '(无)'}..."
                      f"（{snapshot['file_count']} 文件）")
                print(f"        将走全流程（01a 重算批次 + 02 自愈重跑）")
                print("=" * 70)
                self.after(0, lambda: self._start_runner(form, from_stage=None))

        threading.Thread(target=_scan_and_start, name="SourceDirSnapshotScan", daemon=True).start()


    # 状态提示配色：不同状态用不同颜色的圆点，一眼看出运行情况
    _STATUS_COLORS = {
        "ready":   ("#16a34a", "#4ade80"),   # 就绪：绿
        "running": ("#2563eb", "#5b8def"),   # 运行中：蓝
        "stopping": ("#d97706", "#e0a350"),  # 停止中：橙
        "done":    ("#16a34a", "#4ade80"),   # 完成：绿
        "error":   ("#dc2626", "#f87171"),   # 异常：红
    }
    # 运行中/停止中时状态点改为旋转的盲文字符，给用户"还活着"的视觉反馈
    _SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    _SPINNER_INTERVAL = 120  # 毫秒

    def _set_status(self, text: str, kind: str = "ready") -> None:
        self._status_kind = kind
        self._status_text = text
        # 取消旧的动画回调，避免上一轮 running 的 after 残留
        if self._spinner_after_id is not None:
            self.after_cancel(self._spinner_after_id)
            self._spinner_after_id = None
            self._spinner_idx = 0
        if kind in ("running", "stopping"):
            self._spinner_idx = 0
            self._spin_status()
        else:
            self._status_lbl.configure(
                text=f"● {text}",
                text_color=self._STATUS_COLORS.get(kind, self._STATUS_COLORS["ready"]),
            )

    def _spin_status(self) -> None:
        frame = self._SPINNER_FRAMES[self._spinner_idx % len(self._SPINNER_FRAMES)]
        self._status_lbl.configure(
            text=f"{frame} {self._status_text}",
            text_color=self._STATUS_COLORS.get(
                self._status_kind, self._STATUS_COLORS["ready"]
            ),
        )
        self._spinner_idx += 1
        self._spinner_after_id = self.after(self._SPINNER_INTERVAL, self._spin_status)

    def _refresh_warn_hint(self) -> None:
        """根据本次运行的警告计数，刷新右下角警告提示标签。

        无警告时隐藏；有警告时显示一行 "⚠️ xx阶段有 N 条警告，请查看日志"。
        多阶段时列出各阶段名称，单阶段直接点名。
        """
        if not self._warn_total:
            self._warn_lbl.configure(text="")
            self._warn_lbl.grid_remove()
            return

        names = []
        for sid in self._stage_order:
            n = self._warn_stages.get(sid)
            if n:
                label = self._stage_names.get(sid, sid)
                names.append(f"{label} {n} 条")
        # 兜底：警告发生在 _current_stage 未设置时（归入 "?"），无法对应已知阶段
        extra = self._warn_stages.get("?")
        if extra:
            names.append(f"其他 {extra} 条")

        if len(names) == 1:
            detail = names[0]
        else:
            detail = "、".join(names)
        text = f"⚠️ {detail}警告，请查看日志"
        self._warn_lbl.configure(text=text)
        self._warn_lbl.grid(row=1, column=0, sticky="e")

    def _on_stop(self) -> None:
        if self._runner and self._runner.is_running():
            self._runner.stop()
            self._set_status("正在停止…", "stopping")

    def _on_pipeline_done(self, code: int) -> None:
        self._set_running_ui(False)
        self._finish_progress(code == 0)
        if code == 0:
            self._set_status("完成", "done")
        else:
            self._set_status(f"结束（退出码 {code}）", "error")
        # 无论成功/失败，只要中途有警告/错误就在状态下方提示一行
        self._refresh_warn_hint()

    def _set_running_ui(self, running: bool) -> None:
        if running:
            self._run_btn.configure(state="disabled", text="运行中…")
            self._stop_btn.configure(state="normal")
            self._set_status("运行中…", "running")
            # 开跑时清空上一次的警告提示（计数已在 _reset_progress 里清零）
            self._warn_lbl.configure(text="")
            self._warn_lbl.grid_remove()
        else:
            self._run_btn.configure(state="normal", text="▶  开始一键归档流水线")
            self._stop_btn.configure(state="disabled")
            if self._status_kind in ("running", "stopping"):
                self._set_status("就绪", "ready")
                # 回到就绪态时一并隐藏警告提示
                self._warn_lbl.configure(text="")
                self._warn_lbl.grid_remove()

    # =========================================================
    # 关闭
    # =========================================================
    def _log_tk_exception(self, exc_type, exc_value, exc_tb) -> None:
        """Tk 回调异常钩子：把异常栈打到已被接管的 stderr（显示+落盘）。"""
        import traceback
        try:
            text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            sys.stderr.write("\n❌ [GUI] 界面回调异常：\n" + text)
            sys.stderr.flush()
        except Exception:
            pass

    def _on_close(self) -> None:
        # 取消状态点动画回调，避免窗口销毁后 after 触发报错
        if self._spinner_after_id is not None:
            self.after_cancel(self._spinner_after_id)
            self._spinner_after_id = None
        if self._runner and self._runner.is_running():
            if not messagebox.askyesno(
                "流水线运行中", "流水线正在运行，确定退出吗？\n（子进程会被终止，已处理进度已保存）"
            ):
                return
            self._runner.stop()
            # 等 worker 线程把进程树杀净并退出，避免窗口已销毁但子进程仍在跑。
            # Job Object kill 是同步内核操作，进程几乎立即死亡，worker 线程
            # 几毫秒内退出。3 秒兜底：超时也强制 destroy（Job 句柄随 GUI 退出
            # 关闭，KILL_ON_JOB_CLOSE 仍会杀整树，安全）。
            self._runner.wait_stopped(3.0)
        self.log.uninstall()
        self.destroy()


def main(argv: list | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    # 关键：创建 Tk 根窗口前先开 DPI 感知，否则高分屏字体会被系统位图放大而发虚
    _enable_dpi_awareness()
    config_path = DEFAULT_CONFIG_PATH
    # 允许 --config 覆盖
    if "--config" in argv:
        i = argv.index("--config")
        if i + 1 < len(argv):
            config_path = argv[i + 1]
    app = App(config_path=config_path)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())