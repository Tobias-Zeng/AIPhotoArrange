# build_exe.py
# ============================================================
# AIPhotoArrange Nuitka 单文件打包脚本
# ------------------------------------------------------------
# 用途：把项目打包为可在 Windows 下独立运行的 aiphotoarrange.exe
#   python build_exe.py
#
# 产物：release/aiphotoarrange.exe
#       release/pipeline_config.yaml    （由 pipeline_config.template.yaml 脱敏模板生成）
#       release/prompts/*.enc           （仅加密提示词，明文 .txt 不打包）
#
# 设计要点：
# - 入口 run_gui.py（GUI 桌面客户端）
# - --onefile 单文件，运行时解压到临时目录，无需安装
# - 仅打包功能运行需要的代码与资源（customtkinter 主题/字体、tk-inter），
#   排除 test/ 测试代码、明文提示词、中间数据文件
# - 流水线三阶段脚本（01a/01b/02）随主程序一起编译，编译模式下由
#   run_pipeline.py 在进程内 import + main() 执行，不依赖外部 python
# ============================================================
import os
import re
import sys
import json
import shutil
import zipfile
import subprocess
import glob

# 强制 stdout/stderr 使用 UTF-8，避免在 GBK 控制台下打印 emoji（✅/❌/⚠️/📦）
# 时抛 UnicodeEncodeError 导致 collect_release() 等步骤中断
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

# 项目根目录（本脚本所在目录）
ROOT = os.path.dirname(os.path.abspath(__file__))
VENV_PYTHON = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
RELEASE_DIR = os.path.join(ROOT, "release")
BUILD_DIR = os.path.join(ROOT, "build")
EXE_NAME = "aiphotoarrange.exe"
ENTRY = os.path.join(ROOT, "run_gui.py")
PROJECT_MEMORY = os.path.join(ROOT, "PROJECT_MEMORY.md")


def read_version_from_memory():
    """从 PROJECT_MEMORY.md 末尾的「文档版本：vX.Y.Z」解析当前版本号。

    约定：文档末行形如
        文档版本：v2.4.0 / 更新于 2026-07-19 / ...
    取第一个 vX.Y.Z 作为版本号。若解析失败则报错退出。
    """
    if not os.path.exists(PROJECT_MEMORY):
        print(f"❌ 找不到项目记忆文件：{PROJECT_MEMORY}")
        sys.exit(1)
    with open(PROJECT_MEMORY, "r", encoding="utf-8") as f:
        text = f.read()
    # 匹配所有 vX.Y.Z，取最后一个（末尾「文档版本」行才是当前版本）
    matches = re.findall(r"v(\d+)\.(\d+)\.(\d+)", text)
    if not matches:
        print(f"❌ 未能从 {PROJECT_MEMORY} 解析到版本号（形如 vX.Y.Z）")
        sys.exit(1)
    major, minor, patch = matches[-1]
    version = f"{major}.{minor}.{patch}"
    print(f"🔖 从 PROJECT_MEMORY.md 读取版本号：v{version}")
    return version


def run_nuitka():
    """调用 Nuitka 编译单文件 exe 到 build/ 目录。"""
    os.makedirs(BUILD_DIR, exist_ok=True)

    cmd = [
        VENV_PYTHON, "-m", "nuitka",
        # ---------- 模式 ----------
        "--onefile",                         # 单文件 exe，运行时自解压
        "--assume-yes-for-downloads",        # 自动同意下载辅助工具（如 ccache）
        # ---------- 输出 ----------
        f"--output-dir={BUILD_DIR}",
        f"--output-filename={EXE_NAME}",
        "--remove-output",                   # 编译完清理中间 build 目录
        # ---------- 入口 ----------
        ENTRY,
        # ---------- 插件 ----------
        "--enable-plugin=tk-inter",          # tkinter / customtkinter 必需
        # ---------- 项目模块（动态 importlib.import_module 调用，
        # Nuitka 静态分析发现不了，需显式 --include-module 编译为 C）----------
        "--include-module=run_pipeline",
        "--include-module=pipeline_config_loader",
        "--include-module=stage01a_phash_chunker",
        "--include-module=stage01b_geo_resolver_amap",
        "--include-module=stage02_aesthetic_curator",
        "--include-module=stage03a_daily_summary",
        "--include-module=stage03b_yearly_summary",
        # ---------- Windows 特定 ----------
        # exe 图标：把 app.ico 嵌入 exe 的 PE 资源，资源管理器 / 任务栏 /
        # Alt-Tab 从 exe 自身取图标时显示应用图标，而非 Windows 默认图标。
        f"--windows-icon-from-ico={os.path.join(ROOT, 'app.ico')}",
        # app.ico 运行时读取：不打进 onefile 内部，而是作为外部资源放在
        # exe 同级目录（与 pipeline_config.yaml / prompts/ 一致）。gui/app.py
        # 的 _set_window_icon 通过 BASE_DIR（= exe 所在目录）定位 app.ico。
        # collect_release() 会把 app.ico 拷到 release/ 下。
        # console=disable：GUI 模式不显示黑色控制台窗口。
        # 流水线日志通过 GUI 日志框实时显示，无需控制台；
        # 异常信息写日志文件，也不依赖控制台。
        "--windows-console-mode=disable",
        # ---------- 排除测试/开发专用代码 ----------
        "--nofollow-import-to=test",
        "--nofollow-import-to=encrypt_prompt",
        "--nofollow-import-to=tools",
        # 第三方依赖（customtkinter / openai / scipy / numpy / PIL / cryptography /
        # ruamel.yaml / requests / yaml / imagehash / pywt / httpx / pydantic 等）
        # 由 standalone 模式自动发现并包含，无需显式声明。
        # ---------- 编译优化 ----------
        "--jobs=8",                          # 并行编译
    ]

    print("=" * 60)
    print("开始 Nuitka 编译（首次可能需要 20-60 分钟，取决于 CPU）...")
    print("输出目录:", BUILD_DIR)
    print("可执行文件:", os.path.join(BUILD_DIR, EXE_NAME))
    print("=" * 60)
    print(" ".join(cmd))
    print("=" * 60)

    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print(f"\n❌ Nuitka 编译失败，退出码 {result.returncode}")
        sys.exit(result.returncode)
    print(f"\n✅ Nuitka 编译成功：{os.path.join(BUILD_DIR, EXE_NAME)}")


def find_edge():
    """查找 Microsoft Edge 可执行文件路径（Win10/11 默认自带）。

    查找顺序：PROGRAMFILES(X86) -> PROGRAMFILES -> LOCALAPPDATA。
    找不到返回 None。
    """
    candidates = []
    pf_x86 = os.environ.get("PROGRAMFILES(X86)")
    pf = os.environ.get("PROGRAMFILES")
    local_app = os.environ.get("LOCALAPPDATA")
    if pf_x86:
        candidates.append(os.path.join(pf_x86, "Microsoft", "Edge", "Application", "msedge.exe"))
    if pf:
        candidates.append(os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"))
    if local_app:
        candidates.append(os.path.join(local_app, "Microsoft", "Edge", "Application", "msedge.exe"))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def html_to_pdf(html_path, pdf_path):
    """用 Microsoft Edge headless 模式把 HTML 打印为 PDF。

    依赖系统自带的 Edge（Win10/11 默认已装）。找不到 Edge 或转换失败返回 False，
    调用方应自行降级处理（跳过 PDF，不中断主流程）。
    """
    edge = find_edge()
    if not edge:
        print(f"⚠️  未找到 Microsoft Edge，无法生成 PDF：{pdf_path}")
        return False
    abs_html = os.path.abspath(html_path)
    file_url = "file:///" + abs_html.replace("\\", "/")
    # 参数说明（修复 PDF 排版变乱问题）：
    # - --no-pdf-header-footer：新版参数，干净禁用页眉页脚。旧版 --print-to-pdf-no-header
    #   会引入 ArialMT 字体槽污染 PDF。两者都禁用页眉页脚，但新版不引入额外字体。
    # - --virtual-time-budget=5000：给 CSS @page 规则和 webfont 5 秒应用时间，
    #   避免页面未完全渲染就打印导致排版错位。
    # - --run-all-compositor-stages-before-draw：确保复杂布局（flexbox/grid/渐变）
    #   的所有合成阶段完成后再打印，防止 CSS 渲染不完整。
    cmd = [
        edge,
        "--headless=new",
        "--disable-gpu",
        "--no-pdf-header-footer",
        "--virtual-time-budget=5000",
        "--run-all-compositor-stages-before-draw",
        f"--print-to-pdf={pdf_path}",
        file_url,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0:
            return True
        print(f"⚠️  Edge 生成 PDF 失败：{pdf_path}")
        if result.stderr:
            err = result.stderr.decode("utf-8", errors="replace")[:200]
            print(f"    stderr: {err}")
        return False
    except subprocess.TimeoutExpired:
        print(f"⚠️  Edge 生成 PDF 超时（60s）：{pdf_path}")
        return False
    except Exception as e:
        print(f"⚠️  Edge 生成 PDF 异常：{pdf_path}  {e}")
        return False


def count_amap_cache(path):
    """读取高德 regeo 缓存 JSON，返回其条目数量（dict 顶层 key 数）。

    文件缺失或解析失败时返回 0。用于对比开发目录与 release/ 目录下两份
    缓存哪个更"新"——条目多者视为更新（缓存是只增不减的累积型数据）。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return len(data) if isinstance(data, dict) else 0
    except (OSError, ValueError):
        return 0


def collect_release():
    """把 exe + 外部资源拷贝到 release/ 目录。"""
    os.makedirs(RELEASE_DIR, exist_ok=True)

    # 1) exe：build/aiphotoarrange.exe -> release/aiphotoarrange.exe
    src_exe = os.path.join(BUILD_DIR, EXE_NAME)
    dst_exe = os.path.join(RELEASE_DIR, EXE_NAME)
    if not os.path.exists(src_exe):
        print(f"❌ 找不到编译产物 {src_exe}")
        sys.exit(1)
    shutil.copy2(src_exe, dst_exe)
    print(f"📦 exe      -> {dst_exe}")

    # 1b) app.ico：exe 同级放置，供运行时 iconbitmap() 读取。
    #     exe 自身的 PE 图标已由 --windows-icon-from-ico 嵌入，此处副本
    #     仅为 GUI 窗口标题栏 / 任务栏图标设置（iconbitmap 读文件）。
    src_ico = os.path.join(ROOT, "app.ico")
    dst_ico = os.path.join(RELEASE_DIR, "app.ico")
    if os.path.exists(src_ico):
        shutil.copy2(src_ico, dst_ico)
        print(f"📦 图标      -> {dst_ico}")
    else:
        print(f"⚠️  未找到 {src_ico}，跳过（GUI 将使用默认图标）")

    # 2) pipeline_config.yaml：由 pipeline_config.template.yaml 脱敏模板拷贝并重命名。
    #    不再使用项目根目录的 pipeline_config.yaml（含真实 key 与本地目录），
    #    发布只发模板，避免泄露密钥/本地路径。
    src_cfg = os.path.join(ROOT, "pipeline_config.template.yaml")
    dst_cfg = os.path.join(RELEASE_DIR, "pipeline_config.yaml")
    if os.path.exists(src_cfg):
        shutil.copy2(src_cfg, dst_cfg)
        print(f"📦 配置文件  -> {dst_cfg}  (来源: pipeline_config.template.yaml)")
    else:
        print(f"⚠️  未找到 {src_cfg}，跳过")

    # 3) prompts/ 文件夹：仅拷贝 .enc 加密提示词（明文 .txt 不打包）
    src_prompts = os.path.join(ROOT, "prompts")
    dst_prompts = os.path.join(RELEASE_DIR, "prompts")
    if os.path.isdir(src_prompts):
        if os.path.exists(dst_prompts):
            shutil.rmtree(dst_prompts)
        os.makedirs(dst_prompts, exist_ok=True)
        enc_files = glob.glob(os.path.join(src_prompts, "*.enc"))
        if not enc_files:
            print(f"⚠️  {src_prompts} 下没有 .enc 加密提示词文件，请先运行 encrypt_prompt.py")
        for enc in sorted(enc_files):
            shutil.copy2(enc, os.path.join(dst_prompts, os.path.basename(enc)))
            print(f"📦 提示词    -> {os.path.join(dst_prompts, os.path.basename(enc))}")
    else:
        print(f"⚠️  未找到 prompts/ 目录：{src_prompts}")

    # 4) 01b_amap_cache.json：跨用户共享的高德 regeo 缓存。
    #    打进 zip 可避免新机器冷启动时重新调用高德 API（省 QPS、省时间）。
    #    缓存是只增不减的累积型数据（坐标 -> regeo 结果），条目多者视为更新。
    #    若 release/ 下已有版本条目数 >= 开发目录版本，保留 release/ 版本
    #    不覆盖，避免打包时把线上积累的更新缓存回退成开发目录的旧副本。
    src_amap = os.path.join(ROOT, "01b_amap_cache.json")
    dst_amap = os.path.join(RELEASE_DIR, "01b_amap_cache.json")
    if os.path.exists(src_amap):
        if os.path.exists(dst_amap):
            src_count = count_amap_cache(src_amap)
            dst_count = count_amap_cache(dst_amap)
            if dst_count >= src_count:
                size_mb = os.path.getsize(dst_amap) / 1024 / 1024
                print(f"ℹ️  保留 release 下更新的高德缓存（release={dst_count} 条 >= 开发={src_count} 条）")
                print(f"    -> {dst_amap}  ({size_mb:.1f} MB)  [未覆盖]")
                # release 版本更全，回拷到开发目录保持两边一致，
                # 避免下次打包重复对比；开发目录即成为最新副本。
                shutil.copy2(dst_amap, src_amap)
                print(f"🔄 同步回开发目录：{src_amap}  ({dst_count} 条)")
            else:
                shutil.copy2(src_amap, dst_amap)
                size_mb = os.path.getsize(dst_amap) / 1024 / 1024
                print(f"📦 高德缓存  -> {dst_amap}  ({size_mb:.1f} MB)  "
                      f"(开发={src_count} 条 > release={dst_count} 条，已覆盖)")
        else:
            shutil.copy2(src_amap, dst_amap)
            size_mb = os.path.getsize(dst_amap) / 1024 / 1024
            print(f"📦 高德缓存  -> {dst_amap}  ({size_mb:.1f} MB)")
    else:
        if os.path.exists(dst_amap):
            size_mb = os.path.getsize(dst_amap) / 1024 / 1024
            print(f"ℹ️  开发目录无高德缓存，保留 release 下的版本：{dst_amap}  ({size_mb:.1f} MB)")
            dst_count = count_amap_cache(dst_amap)
            shutil.copy2(dst_amap, src_amap)
            print(f"🔄 同步回开发目录：{src_amap}  ({dst_count} 条)")
        else:
            print(f"ℹ️  未找到 {src_amap}（高德缓存），跳过。新机器将重新调用高德 API 积累。")

    # 4b) 01b_geo_alias.template.json：GPS 别名模板（用户参考）。
    #     程序运行时只加载 01b_geo_alias.json / 01b_geo_alias_<profile>.json，
    #     不会加载 .template 文件，所以保留 template 文件名打进去是安全的，
    #     用户想用别名时复制一份改名为 01b_geo_alias.json 即可。
    #     详见 用户操作手册.html「GPS 别名模板」一节。
    src_alias_tpl = os.path.join(ROOT, "01b_geo_alias.template.json")
    dst_alias_tpl = os.path.join(RELEASE_DIR, "01b_geo_alias.template.json")
    if os.path.exists(src_alias_tpl):
        shutil.copy2(src_alias_tpl, dst_alias_tpl)
        print(f"📦 别名模板  -> {dst_alias_tpl}")
    else:
        print(f"⚠️  未找到 {src_alias_tpl}，跳过")

    # 5) 用户手册 PDF：从项目根目录的 HTML 版本用 Edge headless 打印生成。
    #    git 仓库只纳入 HTML，PDF 是构建产物，每次打包重新生成，避免 HTML/PDF 漂移。
    for manual_html in ("快速上手.html", "用户操作手册.html"):
        src_html = os.path.join(ROOT, manual_html)
        if not os.path.exists(src_html):
            print(f"⚠️  未找到 {src_html}，跳过 PDF 生成")
            continue
        pdf_name = os.path.splitext(manual_html)[0] + ".pdf"
        dst_pdf = os.path.join(RELEASE_DIR, pdf_name)
        if html_to_pdf(src_html, dst_pdf):
            size_kb = os.path.getsize(dst_pdf) / 1024
            print(f"📦 手册 PDF  -> {dst_pdf}  ({size_kb:.0f} KB)")
        else:
            print(f"⚠️  PDF 生成失败，跳过：{pdf_name}（zip 将不含此文件）")

    print("\n" + "=" * 60)
    print(f"✅ 发布目录就绪：{RELEASE_DIR}")
    print("   内容：")
    for name in sorted(os.listdir(RELEASE_DIR)):
        full = os.path.join(RELEASE_DIR, name)
        if os.path.isfile(full):
            size_mb = os.path.getsize(full) / 1024 / 1024
            print(f"     {name}  ({size_mb:.1f} MB)")
        else:
            print(f"     {name}/")
            for sub in sorted(os.listdir(full)):
                print(f"       {sub}")
    print("=" * 60)


def pack_portable_zip(version):
    """把发布产物打包成 aiphotoarrange.<version>.portable.x64.zip。

    打包内容（仅以下 6 项，不含其他 release/ 历史文件）：
      - aiphotoarrange.exe
      - app.ico（运行时窗口图标，iconbitmap 读取）
      - pipeline_config.yaml
      - 01b_amap_cache.json（若存在；高德 regeo 跨用户共享缓存，避免新机器冷启动）
      - 01b_geo_alias.template.json（GPS 别名模板，保留 template 文件名，程序不加载）
      - prompts/（整个目录，含 *.enc 加密提示词）
    产物：release/aiphotoarrange.<version>.portable.x64.zip
    """
    zip_name = f"aiphotoarrange.{version}.portable.x64.zip"
    zip_path = os.path.join(RELEASE_DIR, zip_name)

    # 待打包文件清单（相对 release/ 的 arcname，源路径绝对）
    items = []
    exe_src = os.path.join(RELEASE_DIR, EXE_NAME)
    if not os.path.exists(exe_src):
        print(f"❌ 找不到 {exe_src}，无法打包")
        sys.exit(1)
    items.append((exe_src, EXE_NAME))

    ico_src = os.path.join(RELEASE_DIR, "app.ico")
    if os.path.exists(ico_src):
        items.append((ico_src, "app.ico"))
    else:
        print(f"⚠️  未找到 {ico_src}，跳过（GUI 将使用默认图标）")

    cfg_src = os.path.join(RELEASE_DIR, "pipeline_config.yaml")
    if os.path.exists(cfg_src):
        items.append((cfg_src, "pipeline_config.yaml"))
    else:
        print(f"⚠️  未找到 {cfg_src}，跳过")

    # 01b_amap_cache.json 含开发者个人行踪（GPS -> 地址缓存），
    # 对外分发时【故意不打包】，避免泄露常去地点。朋友首次运行会用
    # 自己的高德 key 重新积累缓存。如需内部自用打包，取消下方注释即可。
    # amap_src = os.path.join(RELEASE_DIR, "01b_amap_cache.json")
    # if os.path.exists(amap_src):
    #     items.append((amap_src, "01b_amap_cache.json"))
    print("ℹ️  安全策略：01b_amap_cache.json（含个人行踪）不打入分发包")

    # GPS 别名模板（保留 template 文件名，程序不会误加载）
    alias_tpl_src = os.path.join(RELEASE_DIR, "01b_geo_alias.template.json")
    if os.path.exists(alias_tpl_src):
        items.append((alias_tpl_src, "01b_geo_alias.template.json"))
    else:
        print(f"ℹ️  未找到 {alias_tpl_src}（别名模板），跳过")

    prompts_src = os.path.join(RELEASE_DIR, "prompts")
    if os.path.isdir(prompts_src):
        for fn in sorted(os.listdir(prompts_src)):
            full = os.path.join(prompts_src, fn)
            if os.path.isfile(full):
                items.append((full, os.path.join("prompts", fn)))
    else:
        print(f"⚠️  未找到 prompts/ 目录：{prompts_src}，跳过")

    # 用户手册 PDF（中文文件名）。
    # 安全策略：公开发布的便携包只带「快速上手.pdf」，【不打包「用户操作手册.pdf」】——
    # 该手册含部署拓扑/内网信息，属于不公开文档（见 .gitignore 相应用户操作手册.html）。
    # 用户操作手册.pdf 仍会留在 release/ 目录本地自用，只是不进分发包。
    for manual in ("快速上手.pdf",):
        m_src = os.path.join(RELEASE_DIR, manual)
        if os.path.exists(m_src):
            items.append((m_src, manual))
        else:
            print(f"⚠️  未找到 {m_src}，跳过")

    # 覆盖旧 zip
    if os.path.exists(zip_path):
        os.remove(zip_path)

    print(f"\n📦 打包便携 zip：{zip_path}")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for src, arc in items:
            zf.write(src, arc)
            print(f"   + {arc}")
    size_mb = os.path.getsize(zip_path) / 1024 / 1024
    print(f"✅ zip 打包完成：{zip_path}  ({size_mb:.1f} MB)")


def generate_manuals_only():
    """仅重新生成两份用户手册 PDF（不跑 Nuitka 编译、不收集其它资源）。

    用途：调 HTML 排版或 Edge PDF 参数后快速验证效果，无需等 30 分钟编译。
    产物直接写到 release/ 下，文件名与正式打包一致。
    """
    os.makedirs(RELEASE_DIR, exist_ok=True)
    print("=" * 60)
    print("PDF-only 模式：仅生成用户手册 PDF（跳过 Nuitka 编译）")
    print("输出目录:", RELEASE_DIR)
    print("=" * 60)
    for manual_html in ("快速上手.html", "用户操作手册.html"):
        src_html = os.path.join(ROOT, manual_html)
        if not os.path.exists(src_html):
            print(f"⚠️  未找到 {src_html}，跳过 PDF 生成")
            continue
        pdf_name = os.path.splitext(manual_html)[0] + ".pdf"
        dst_pdf = os.path.join(RELEASE_DIR, pdf_name)
        if html_to_pdf(src_html, dst_pdf):
            size_kb = os.path.getsize(dst_pdf) / 1024
            print(f"📦 手册 PDF  -> {dst_pdf}  ({size_kb:.0f} KB)")
        else:
            print(f"⚠️  PDF 生成失败，跳过：{pdf_name}")
    print("=" * 60)
    print("✅ PDF-only 模式完成")
    print("=" * 60)


def main():
    # --pdf-only：仅生成两份手册 PDF，跳过 Nuitka 编译与其它资源收集。
    # 用于调 HTML 排版或 Edge PDF 参数后快速验证效果。
    if "--pdf-only" in sys.argv:
        generate_manuals_only()
        return
    version = read_version_from_memory()
    run_nuitka()
    collect_release()
    pack_portable_zip(version)


if __name__ == "__main__":
    main()
