# build_api_exe.py
"""
打包 api_server.py 为独立 exe，用于作为 Windows 系统服务运行。

用法：
  python build_api_exe.py

输出：
  release/PhotoArrangeAPI.exe
"""

import os
import re
import sys
import glob
import shutil
import subprocess
from pathlib import Path


def read_version_from_memory(base_dir: Path) -> str:
    """从 PROJECT_MEMORY.md 末尾的「文档版本：vX.Y.Z」解析当前版本号。

    与 build_exe.py 保持一致：取文本中最后一个 vX.Y.Z 作为当前版本。
    解析失败则报错退出。
    """
    memory = base_dir / "PROJECT_MEMORY.md"
    if not memory.exists():
        print(f"[错误] 找不到项目记忆文件：{memory}")
        sys.exit(1)
    text = memory.read_text(encoding="utf-8")
    matches = re.findall(r"v(\d+)\.(\d+)\.(\d+)", text)
    if not matches:
        print(f"[错误] 未能从 {memory} 解析到版本号（形如 vX.Y.Z）")
        sys.exit(1)
    major, minor, patch = matches[-1]
    version = f"{major}.{minor}.{patch}"
    print(f"[版本] 从 PROJECT_MEMORY.md 读取版本号：v{version}")
    return version


def pack_portable_zip(release_dir: Path, base_dir: Path, version: str):
    """
    组装便携包并打成 zip。
    结构：
      PhotoArrangeAPI_Portable/
        api_server.dist/       (Nuitka 产物)
          PhotoArrangeAPI.exe
          api_config.yaml      (用户唯一配置文件，从 template 生成)
          prompts/             (仅 *.enc 加密提示词)
          logs/                (空目录)
        nssm.exe               (服务管理工具)
        install.bat            (便携安装脚本，相对路径)
        uninstall.bat          (便携卸载脚本)
        README.md              (说明文档)
    """
    print("\n" + "=" * 60)
    print("组装便携包...")
    print("=" * 60)

    portable = release_dir / "PhotoArrangeAPI_Portable"
    if portable.exists():
        shutil.rmtree(portable)
    portable.mkdir(parents=True)

    dist_src = release_dir / "api_server.dist"
    if not dist_src.exists():
        print(f"[错误] 找不到 {dist_src}")
        sys.exit(1)

    # 1. 复制 Nuitka 产物
    print("复制 api_server.dist ...")
    shutil.copytree(dist_src, portable / "api_server.dist")
    
    # 1a. 清理日志目录内容
    logs_dir = portable / "api_server.dist" / "logs"
    if logs_dir.exists():
        for f in logs_dir.iterdir():
            f.unlink()

    # 1b. prompts/ 目录：删除 Nuitka 打包进来的所有内容，仅保留 .enc 加密文件
    #     （Nuitka 的 --include-data-dir 会把整个目录打包，包括 .txt 明文）
    dist_prompts = portable / "api_server.dist" / "prompts"
    if dist_prompts.exists():
        shutil.rmtree(dist_prompts)
    dist_prompts.mkdir(parents=True, exist_ok=True)
    
    src_prompts = base_dir / "prompts"
    if src_prompts.is_dir():
        enc_files = sorted(src_prompts.glob("*.enc"))
        if not enc_files:
            print(f"[警告] {src_prompts} 下没有 .enc 加密提示词文件，请先运行 encrypt_prompt.py")
        for enc in enc_files:
            shutil.copy2(enc, dist_prompts / enc.name)
            print(f"  + prompts/{enc.name}")
    else:
        print(f"[警告] 未找到 prompts/ 目录：{src_prompts}")

    # 1c. api_config.yaml：从 template 生成，避免打包真实密钥
    #     删除 Nuitka 打包进来的真实配置，用 template 替换
    cfg_dist = portable / "api_server.dist" / "api_config.yaml"
    cfg_template = base_dir / "api_config.template.yaml"
    if cfg_template.exists():
        shutil.copy2(cfg_template, cfg_dist)
        print(f"  + api_config.yaml (来源: api_config.template.yaml)")
    else:
        print(f"[警告] 未找到 {cfg_template}，将使用 Nuitka 打包的配置（可能含真实密钥）")

    # 1d. pipeline_config.yaml：同样从 template 生成（双保险）。
    #     Nuitka 已按 template 打包（见 --include-data-files），此处再覆盖一次，
    #     确保 amap_key 与各云端 provider 的 api_key 绝不出现在便携包内。
    #     用户首次部署需编辑此文件填入真实 amap_key / 云端 api_key（本地
    #     ollama/lmstudio 不需要）。
    pcfg_dist = portable / "api_server.dist" / "pipeline_config.yaml"
    pcfg_template = base_dir / "pipeline_config.template.yaml"
    if pcfg_template.exists():
        shutil.copy2(pcfg_template, pcfg_dist)
        print(f"  + pipeline_config.yaml (来源: pipeline_config.template.yaml)")
    else:
        print(f"[警告] 未找到 {pcfg_template}，将使用 Nuitka 打包的配置（可能含真实密钥）")

    # 2. 复制 nssm.exe
    nssm_src = base_dir / "nssm.exe"
    if nssm_src.exists():
        shutil.copy(nssm_src, portable / "nssm.exe")
    else:
        print("[警告] 未找到 nssm.exe，便携包缺少服务管理工具")

    # 3. 复制便携脚本和文档（从已有的便携目录模板复用）
    #    这些文件由源码维护，位于 portable_template/
    tpl = base_dir / "portable_template"
    for name in ("install.bat", "uninstall.bat", "README.md"):
        src = tpl / name
        if src.exists():
            shutil.copy(src, portable / name)
        else:
            print(f"[警告] 便携模板缺少 {name}")

    # 4. 打 zip（文件名带版本号，与 GUI 包风格一致加 .x64 后缀）
    zip_name = f"PhotoArrangeAPI.{version}.Portable.x64.zip"
    zip_path = release_dir / zip_name
    if zip_path.exists():
        zip_path.unlink()
    print(f"\n打包 zip: {zip_name} ...")
    shutil.make_archive(str(zip_path.with_suffix("")), "zip",
                        root_dir=release_dir, base_dir="PhotoArrangeAPI_Portable")

    size_mb = zip_path.stat().st_size / 1024 / 1024
    print(f"\n便携包: {zip_path} ({size_mb:.1f} MB)")


def main():
    print("=" * 60)
    print("开始打包 PhotoArrange API 服务...")
    print("=" * 60)
    
    # 确保在项目根目录
    base_dir = Path(__file__).parent
    os.chdir(base_dir)
    
    # 读取版本号
    version = read_version_from_memory(base_dir)
    
    # 创建 release 目录
    release_dir = base_dir / "release"
    release_dir.mkdir(exist_ok=True)
    
    # Nuitka 打包参数
    cmd = [
        sys.executable,
        "-m", "nuitka",
        "--standalone",
        "--windows-console-mode=disable",
        "--output-dir=release",
        "--output-filename=PhotoArrangeAPI.exe",
        # 包含数据文件
        # 注意：api_config.yaml / prompts/ 不在此打包，改为在 pack_portable_zip()
        #   阶段处理，以确保仅打包 template 配置和 .enc 加密提示词（排除真实密钥
        #   与 .txt 明文提示词）
        # pipeline_config.yaml 只打包脱敏 template（源用 template，落地文件名
        #   保持 pipeline_config.yaml 以便运行时读取）；pack_portable_zip() 还会
        #   再覆盖一次做双保险，绝不把真实 amap_key / 云端 api_key 打进产物。
        #   注意：运行时只读 pipeline_config.yaml，不再另外打包 .template.yaml
        #   到 dist（避免便携包出现内容重复的两份配置）。
        "--include-data-files=pipeline_config.template.yaml=pipeline_config.yaml",
        # 核心依赖模块
        "--include-module=yaml",
        "--include-module=fastapi",
        "--include-module=uvicorn",
        "--include-module=PIL",
        "--include-module=openai",
        # stage 脚本及其依赖（关键：打包进 exe）
        "--include-module=stage01a_phash_chunker",
        "--include-module=stage01b_geo_resolver_amap",
        "--include-module=stage02_aesthetic_curator",
        "--include-module=pipeline_config_loader",
        # API 任务流水线执行器（子进程自调用入口）
        "--include-module=run_api_pipeline",
        "--include-module=api_pipeline_runner",
        "--include-module=api_task_manager",
        # 第三方依赖库
        "--include-module=imagehash",
        "--include-module=requests",
        "--include-module=numpy",
        # 主入口
        "api_server.py"
    ]
    
    print("\n执行命令:")
    print(" ".join(cmd))
    print()
    
    try:
        result = subprocess.run(cmd, check=True)
        
        print("\n" + "=" * 60)
        print("打包成功！(standalone 模式)")
        print(f"输出目录: {release_dir / 'api_server.dist'}")
        print(f"主程序: {release_dir / 'api_server.dist' / 'PhotoArrangeAPI.exe'}")
        print("=" * 60)
        
        # 组装便携包 zip
        pack_portable_zip(release_dir, base_dir, version)
        
        print("\n" + "=" * 60)
        print("全部完成！")
        print("=" * 60)
        print("\n分发方式:")
        print(f"  将 release/PhotoArrangeAPI.{version}.Portable.x64.zip 发给用户")
        print("  用户解压后编辑 api_server.dist/api_config.yaml，右键 install.bat 以管理员身份运行")
        print()
        
    except subprocess.CalledProcessError as e:
        print("\n" + "=" * 60)
        print(f"打包失败: {e}")
        print("=" * 60)
        sys.exit(1)


if __name__ == "__main__":
    main()
