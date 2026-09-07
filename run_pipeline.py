# run_pipeline.py
"""
一键串联三阶段流水线：01a 切批 → 01b 反向地理编码 → 02 LLM 智能归档。

所有参数从 pipeline_config.yaml 读取，不需要再进 .py 手动改。

用法：
  python run_pipeline.py                 # 断点续跑（保留所有已有进度/缓存）
  python run_pipeline.py --fresh         # 全新跑：先把用户专属中间文件"归档"（不删除），再跑
  python run_pipeline.py --only 01a      # 只跑某一阶段（01a / 01b / 02）
  python run_pipeline.py --from 01b      # 从某阶段开始跑到结尾
  python run_pipeline.py --config other.yaml   # 指定别的配置文件

设计原则：
- --fresh 不删除任何文件，只把它们移动到 _pipeline_archive/<profile>_<时间戳>/，
  避免误删还有用的数据。amap 坐标缓存跨用户共享，任何时候都不动。
- --fresh 还会检查输出目录：如果已存在且非空，自动加 _renamed_ 时间戳后缀
  改名移走（不删除），腾出原路径让流水线重新创建全新目录输出。
- phash 缓存按"文件路径+mtime"寻址，全新跑时保留能显著加速，默认不归档它
  （加 --reset-phash 才归档）。
"""

import os
import sys
import shutil
import argparse
import subprocess
from datetime import datetime

import pipeline_config_loader as cfg_loader
from pipeline_config_loader import BASE_DIR

# 流水线运行期间阻止 Windows 睡眠（仅 Windows 生效，其他平台 no-op）。
# 命令行直跑入口（main）用 try/finally 包裹阶段循环确保恢复；
# --internal-stage 调试入口早期 return 不走防睡眠逻辑。
try:
    from gui.power import prevent_sleep, allow_sleep
except Exception:
    def prevent_sleep() -> None: pass
    def allow_sleep() -> None: pass

# Nuitka 编译后，sys.executable 指向临时解压目录里并不存在的 python.exe，
# 无法用于启动子进程。编译模式下流水线编排器改用「进程内 import + main()」
# 直接执行各阶段（见 run_stage / _run_stage_inprocess），不再启动子进程，
# 全程不依赖任何外部 python 解释器。开发模式仍用 sys.executable 走原
# python 子进程路径（隔离更好、便于单阶段调试）。
_COMPILED = "__compiled__" in globals()
# 开发模式子进程可执行文件（编译模式不走 subprocess，此处仅开发模式用）
_SUBPROC_EXE = sys.executable

STAGES = [
    ("01a", "stage01a_phash_chunker.py",     "切批（phash + 时间双信号）"),
    ("01b", "stage01b_geo_resolver_amap.py", "反向地理编码（高德 regeo）"),
    ("02",  "stage02_aesthetic_curator.py",  "LLM 精华挑选与事件命名"),
    ("03a", "stage03a_daily_summary.py",     "日级日报合并（LLM 汇总事件名）"),
    ("03b", "stage03b_yearly_summary.py",    "跨年事件聚合（LLM 主题聚类）"),
]
STAGE_IDS = [s[0] for s in STAGES]
# 各阶段对应的模块名（用于 --internal-stage 进程内执行）
# ⚠️ 模块名必须以字母开头，否则 Nuitka 生成的 C 标识符非法（C 标识符不能以数字开头）。
_STAGE_MODULES = {
    "01a": "stage01a_phash_chunker",
    "01b": "stage01b_geo_resolver_amap",
    "02":  "stage02_aesthetic_curator",
    "03a": "stage03a_daily_summary",
    "03b": "stage03b_yearly_summary",
}


def parse_args():
    p = argparse.ArgumentParser(description="AIPhotoArrange 一键流水线")
    p.add_argument("--config", default=None, help="配置文件路径（默认 pipeline_config.yaml）")
    p.add_argument("--profile", default=None,
                   help="临时指定激活的 profile（覆盖配置文件里的 profile 字段），不改文件")
    p.add_argument("--fresh", action="store_true",
                   help="全新跑：先归档用户专属中间文件（不删除）再执行")

    p.add_argument("--reset-phash", action="store_true",
                   help="配合 --fresh：连 phash 缓存一起归档（默认保留以加速）")
    p.add_argument("--only", choices=STAGE_IDS, default=None,
                   help="只运行指定的单个阶段")
    p.add_argument("--from", dest="from_stage", choices=STAGE_IDS, default=None,
                   help="从指定阶段开始运行到结尾")
    p.add_argument("--dry-run", action="store_true",
                   help="只打印将要执行的动作，不真正运行/归档")
    p.add_argument("--skip-preflight", action="store_true",
                   help="跳过 02 推理模型可用性预检（默认会先检查模型再启动流水线）")
    # 内部参数：Nuitka 编译后 exe 自调用进入单阶段执行模式（用户不直接使用）
    p.add_argument("--internal-stage", choices=STAGE_IDS, default=None,
                   help=argparse.SUPPRESS)
    return p.parse_args()



def select_stages(args, cfg):
    if args.only:
        # --only 03a/03b 时即使 enabled=false 也允许显式跑（便于手动调试）
        return [s for s in STAGES if s[0] == args.only]
    if args.from_stage:
        idx = STAGE_IDS.index(args.from_stage)
        stages = STAGES[idx:]
    else:
        stages = list(STAGES)
    # 默认流程下：若 daily_summary.enabled != true，剔除 03a 阶段
    # （03a 脚本内部也会再次检查 enabled，双重保险）
    daily = (cfg or {}).get("daily_summary") or {}
    if not daily.get("enabled", False):
        stages = [s for s in stages if s[0] != "03a"]
    # 默认流程下：若 yearly_summary.enabled != true，剔除 03b 阶段
    # （03b 脚本内部也会再次检查 enabled，双重保险）
    yearly = (cfg or {}).get("yearly_summary") or {}
    if not yearly.get("enabled", False):
        stages = [s for s in stages if s[0] != "03b"]
    return stages


def archive_fresh_files(cfg, reset_phash, dry_run):
    """
    把本 profile 的"用户专属"中间文件移动到带时间戳的归档目录。
    不删除、不触碰跨用户共享的 amap 缓存。
    """
    files = cfg_loader.resolve_filenames(cfg)
    profile = cfg_loader.get_profile(cfg) or "default"

    # 全新跑要归档的"过程文件"（每次运行都会重新生成）。
    # 注意：01b_geo_alias.json 是用户手工维护的 GPS 别名输入，不是过程文件，
    #      任何时候都不归档、不删除；amap 坐标缓存跨用户共享，同样保留。
    targets = [
        files["batches_file"],
        files["geo_review_file"],
        files["progress_file"],
        files["unprocessed_log"],
        files["daily_summary_progress_file"],
        files["yearly_summary_progress_file"],
    ]

    if reset_phash:
        targets.append(files["phash_cache_file"])

    existing = [f for f in targets if os.path.exists(os.path.join(BASE_DIR, f))]
    if not existing:
        print("  [fresh] 没有需要归档的旧中间文件，直接开跑。")
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_dir = os.path.join(BASE_DIR, "_pipeline_archive", f"{profile}_{stamp}")
    print(f"  [fresh] 归档 {len(existing)} 个旧中间文件到 {os.path.relpath(archive_dir, BASE_DIR)}")
    for f in existing:
        print(f"          - {f}")
    print(f"  [fresh] 保留跨用户共享的坐标缓存：{files['amap_cache_file']}")
    if not reset_phash:
        print(f"  [fresh] 保留 phash 缓存以加速（如需重算加 --reset-phash）")

    if dry_run:
        return

    os.makedirs(archive_dir, exist_ok=True)
    for f in existing:
        shutil.move(os.path.join(BASE_DIR, f), os.path.join(archive_dir, f))


def archive_target_dir_if_exists(cfg, config_path, dry_run):
    """
    全新跑时检查输出目录：如果已存在且非空，把旧目录加 _renamed_ 时间戳后缀
    改名移走（不删除），腾出原路径让流水线重新创建全新空目录输出。

    - 只在 target_dir 实际存在且非空时才改名（不存在或空则什么都不做）。
    - 不修改配置文件：target_dir 路径不变，流水线后续会在原路径创建新目录。
    """
    common = cfg.get("common", {}) if cfg else {}
    target_dir = common.get("target_dir", "")
    if not target_dir:
        return

    if not os.path.exists(target_dir):
        return

    # 空目录不需要改名，直接用
    try:
        if not os.listdir(target_dir):
            return
    except OSError:
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archived_dir = f"{target_dir}_renamed_{stamp}"

    print(f"  [fresh] 输出目录已存在，改名移走避免重合：")
    print(f"          {target_dir}")
    print(f"        -> {archived_dir}")

    if dry_run:
        return

    # 把旧目录改名移走，腾出原路径供本次全新输出使用
    try:
        os.rename(target_dir, archived_dir)
    except OSError as e:
        print(f"  [fresh] ⚠️ 输出目录改名失败：{e}，将使用原目录继续。")


def preflight_check_curator_model(cfg):
    """
    02 阶段启动前的推理模型可用性预检。

    目的：避免忘开本地 ollama 推理机时，流水线跑完 01a/01b 才在 02 报错，
         人已离开却没发现。这里在启动任何阶段之前先探测 02 配置的模型服务，
         连不上或模型不存在就立即报错、终止，不再空跑前面的阶段。

    返回 (ok: bool, message: str)。
    """
    curator = (cfg or {}).get("curator", {}) if cfg else {}
    provider = curator.get("provider", "ollama")
    provider_configs = curator.get("provider_configs") or {}
    pc = provider_configs.get(provider)
    if not pc:
        return False, f"02 配置里找不到 provider「{provider}」对应的 provider_configs 段"

    base_url = pc.get("base_url")
    api_key = pc.get("api_key")
    model = pc.get("model")
    if not base_url or not model:
        return False, f"provider「{provider}」的 base_url 或 model 未配置完整"

    try:
        from openai import OpenAI
    except Exception as e:
        return False, f"未安装 openai 库，无法预检：{e}"

    # 用较短超时快速判断服务是否在线；探测失败即视为不可用。
    client = OpenAI(base_url=base_url, api_key=api_key or "none",
                    timeout=15.0, max_retries=0)

    try:
        models = client.models.list()
        available = {m.id for m in getattr(models, "data", [])}
    except Exception as e:
        return False, (
            f"无法连接推理服务 {base_url}（provider={provider}）。\n"
            f"     常见原因：本地 ollama 推理机未开机 / 服务未启动 / 网络不通。\n"
            f"     错误详情：{e}"
        )

    # 能列出模型时，进一步确认目标模型已加载/存在。
    # 本地 ollama 若模型没 pull 会导致 02 直接失败，这里也拦下来。
    if available and model not in available:
        sample = ", ".join(sorted(available)[:8])
        return False, (
            f"推理服务已连通，但目标模型「{model}」不在可用模型列表中。\n"
            f"     provider={provider}，base_url={base_url}\n"
            f"     服务上可用模型（部分）：{sample or '(空)'}\n"
            f"     请确认模型名拼写，或在推理机上 pull/加载该模型。"
        )

    return True, f"推理服务在线，模型「{model}」可用（provider={provider}）"


def _run_stage_inprocess(stage_id):
    """在当前进程内直接执行某个阶段（Nuitka 编译后使用）。

    import 对应阶段模块并调用其 main()，模块顶层配置在 import 时读取，
    行为与开发模式 subprocess 调脚本一致。全程不启动子进程、不依赖
    外部 python 解释器。

    日志隔离：三个阶段脚本都在模块顶层调用 logging.basicConfig() 装各自的
    FileHandler，但 basicConfig 只在 root logger 无 handler 时生效。进程内
    模式下 01a 先装好 handler 后，01b/02 的 basicConfig 变成 no-op，它们的
    FileHandler 不会装上，日志会全写到 01a 的文件里。这里在每阶段 import 前
    清空 root logger 的所有 handler，强制该阶段的 basicConfig 重新生效，
    确保每个阶段的日志写到各自独立的文件。
    """
    import logging
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

    mod_name = _STAGE_MODULES[stage_id]
    import importlib
    mod = importlib.import_module(mod_name)
    mod.main()


def run_stage(stage_id, script, desc, config_path, profile, dry_run):
    print("=" * 70)
    print(f"▶ 阶段 {stage_id}：{desc}")
    print(f"  脚本：{script}")

    print("=" * 70)
    if dry_run:
        print("  [dry-run] 跳过实际执行")
        return 0

    # 设置环境变量，保证各阶段脚本（import 时）读到同一份配置/profile。
    # 注意：进程内执行时环境变量直接影响 import 时的 load_config()，
    # 必须在 import 阶段模块之前设置好。
    if config_path:
        os.environ["PIPELINE_CONFIG"] = os.path.abspath(config_path)
    if profile is not None:
        os.environ["PIPELINE_PROFILE"] = profile

    if _COMPILED:
        # Nuitka 编译后：直接在当前进程内执行阶段，不启动子进程。
        # 这样整个流水线编排器在单进程内依次跑完 01a/01b/02，无需
        # 多次 onefile 解压，也不依赖任何外部 python 解释器。
        try:
            _run_stage_inprocess(stage_id)
            return 0
        except SystemExit as e:
            # 阶段脚本里若有 sys.exit，转成退出码
            return e.code if isinstance(e.code, int) else 1
        except Exception as e:
            print(f"  [阶段异常] {stage_id}: {e}", file=sys.stderr)
            return 1
    else:
        # 开发模式：用当前 python 解释器跑阶段脚本（独立子进程，隔离更好）
        env = os.environ.copy()
        if config_path:
            env["PIPELINE_CONFIG"] = os.path.abspath(config_path)
        if profile is not None:
            env["PIPELINE_PROFILE"] = profile
        result = subprocess.run([_SUBPROC_EXE, os.path.join(BASE_DIR, script)],
                                cwd=BASE_DIR, env=env)
        return result.returncode


def main():
    args = parse_args()

    # ------------------------------------------------------------------
    # 内部单阶段执行模式：可由 exe 自调用进入（手动单阶段调试用）。
    # import 对应阶段模块并调用其 main()，模块顶层配置在 import 时读取。
    # 正常流水线编排（--internal-run-pipeline）走下面的 run_stage，编译
    # 模式下也是进程内 import + main()，不启动子进程。
    # ------------------------------------------------------------------
    if args.internal_stage:
        mod_name = _STAGE_MODULES[args.internal_stage]
        import importlib
        mod = importlib.import_module(mod_name)
        mod.main()
        return

    # --profile 临时覆盖：设进环境变量，本进程 load_config 和子进程都会读到
    if args.profile:
        os.environ["PIPELINE_PROFILE"] = args.profile

    # 读取配置（也用于校验 + fresh 归档时算文件名）
    try:
        cfg = cfg_loader.load_config(args.config)
    except Exception as e:
        print(f"❌ 配置文件读取失败：{e}")
        sys.exit(1)

    if not cfg:
        print("⚠️  未找到 pipeline_config.yaml，脚本将使用各自内置默认值。")
    else:
        active = cfg_loader.get_profile(cfg)
        all_profiles = cfg_loader.list_profiles(cfg)
        if args.profile and all_profiles and args.profile not in all_profiles:
            print(f"⚠️  --profile {args.profile} 未在 profiles: 段定义，"
                  f"将只作为文件名后缀、不套用覆盖。已定义：{', '.join(all_profiles) or '(无)'}")
        common = cfg.get("common", {})
        print(f"📋 profile: {active or '(默认，无后缀)'}"
              + (f"   可选：{', '.join(all_profiles)}" if all_profiles else ""))
        print(f"   输入目录: {common.get('source_dir')}")
        print(f"   输出目录: {common.get('target_dir')}")
        print(f"   provider: {cfg.get('curator', {}).get('provider')}")


    stages = select_stages(args, cfg)

    # 02/03a/03b 推理模型预检：只要本次会跑到 02 或 03a 或 03b，就在启动前先探测模型服务。
    # 避免忘开本地 ollama 推理机时，白跑完 01a/01b 才在 02 报错。
    # dry-run 不真正执行、--skip-preflight 显式跳过时不检查。
    will_run_02 = any(s[0] == "02" for s in stages)
    will_run_03a = any(s[0] == "03a" for s in stages)
    will_run_03b = any(s[0] == "03b" for s in stages)
    if (will_run_02 or will_run_03a or will_run_03b) and not args.dry_run and not args.skip_preflight:
        print("🔍 推理模型预检中……")
        ok, msg = preflight_check_curator_model(cfg)
        if not ok:
            print(f"❌ 推理模型不可用，流水线未启动：\n     {msg}")
            print("   请开机/启动推理服务后重试；确认无需检查可加 --skip-preflight 跳过。")
            sys.exit(2)
        print(f"✅ {msg}")

    if args.fresh:
        # fresh 只对将要跑的阶段涉及的文件做归档；简单起见统一归档全部用户专属文件
        archive_fresh_files(cfg, args.reset_phash, args.dry_run)
        # 如果输出目录已存在，改名避免新旧照片混在一起
        archive_target_dir_if_exists(cfg, args.config, args.dry_run)


    print(f"\n将依次运行阶段：{', '.join(s[0] for s in stages)}\n")

    # --only 03a/03b 模式下，stage 脚本的 main() 会因 enabled=false
    # 直接 return。设置环境变量强制跳过该 gate，让 GUI 的「仅合并日报 (03a)」
    # /「仅跨年聚合 (03b)」能在 enabled=false 时对历史归档目录单跑。
    # 非 --only 03a/03b 模式（fresh/resume）下显式清除，防止父进程残留值污染，
    # 确保全流程严格遵循 config 里 enabled 的设置。
    if args.only == "03a":
        os.environ["AIPHOTO_FORCE_STAGE03A"] = "1"
    else:
        os.environ.pop("AIPHOTO_FORCE_STAGE03A", None)
    if args.only == "03b":
        os.environ["AIPHOTO_FORCE_STAGE03B"] = "1"
    else:
        os.environ.pop("AIPHOTO_FORCE_STAGE03B", None)

    # 阶段循环期间阻止系统睡眠（含断点续跑 --from 子集场景）。
    # try/finally 确保：正常结束、阶段失败 sys.exit（SystemExit 走 finally）、
    # Ctrl+C（KeyboardInterrupt 走 finally）、异常都能恢复睡眠。
    prevent_sleep()
    try:
        for stage_id, script, desc in stages:
            code = run_stage(stage_id, script, desc, args.config, args.profile, args.dry_run)

            if code != 0:
                print(f"\n❌ 阶段 {stage_id} 以退出码 {code} 结束，流水线中止。")
                print("   修复问题后可用 --from {} 从该阶段续跑。".format(stage_id))
                sys.exit(code)

        print("\n✅ 流水线全部阶段执行完毕。")
    finally:
        allow_sleep()


if __name__ == "__main__":
    main()
