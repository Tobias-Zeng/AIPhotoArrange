# reorganize_by_period.py
"""
按年或按月归集已整理完成的输出目录照片。

职责：
- 扫描 TARGET_DIR 下所有 "YYYY-MM-DD-*" 事件子文件夹（02/03 的输出结构）
- 按年或按月分组，COPY（非 move）照片到新的输出目录，照片完全平铺
- 不修改原输出目录，输出到同级自动派生的 "按年归集" / "按月归集" 目录

用法：
  python reorganize_by_period.py                      # 默认按年，输入读配置，输出自动派生
  python reorganize_by_period.py --mode month         # 按月
  python reorganize_by_period.py --input <目录>       # 指定输入目录
  python reorganize_by_period.py --output <目录>      # 指定输出目录
  python reorganize_by_period.py --dry-run            # 预览不实际复制
"""
import os
import sys
import re
import argparse
import hashlib
import shutil
import logging
from datetime import datetime
from collections import defaultdict

# 本脚本位于 tools/ 子目录，需把项目根目录加入 sys.path 才能 import 根目录下的
# pipeline_config_loader（与 run_gui.py 一致的 BASE_DIR 推导逻辑）。
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pipeline_config_loader as _cfg_loader

# ==========================================
# 配置加载（仅用 common.target_dir，不依赖 LLM 相关配置）
# ==========================================
_CONFIG = _cfg_loader.load_config()
_COMMON = _CONFIG.get("common", {})

# 与项目其他脚本一致的日期事件目录命名正则：YYYY-MM-DD-事件名
_DATE_EVENT_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})-(.+)$')

# 复用项目统一维护的照片扩展名与跳过目录
SUPPORTED_EXTS = _cfg_loader.SUPPORTED_EXTS
SKIP_DIR_NAMES = _cfg_loader.SKIP_DIR_NAMES

# ==========================================
# 日志
# ==========================================
os.makedirs(os.path.join(_cfg_loader.BASE_DIR, "logs"), exist_ok=True)
log_filename = os.path.join(
    _cfg_loader.BASE_DIR, "logs",
    f"reorganize_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
)
_handlers = [logging.FileHandler(log_filename, encoding='utf-8')]
if sys.stderr is not None:
    _handlers.append(logging.StreamHandler())
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=_handlers,
)
logger = logging.getLogger(__name__)


# ==========================================
# 核心逻辑
# ==========================================
def scan_source_dir(source_dir):
    """
    扫描 source_dir 下所有 "YYYY-MM-DD-*" 子文件夹，收集照片。

    返回 [(year, month, date_str, photo_abs_path), ...]
    跳过：非目录、不符合日期前缀命名的目录、SKIP_DIR_NAMES、非照片文件。
    """
    results = []
    if not os.path.isdir(source_dir):
        return results

    skipped_dirs = []
    for name in sorted(os.listdir(source_dir)):
        full = os.path.join(source_dir, name)
        if not os.path.isdir(full):
            continue
        if name in SKIP_DIR_NAMES:
            continue
        m = _DATE_EVENT_RE.match(name)
        if not m:
            skipped_dirs.append(name)
            continue
        year, month = m.group(1), m.group(2)
        date_str = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

        # 递归收集照片（事件文件夹下可能有子目录）
        for root, dirs, files in os.walk(full):
            # 过滤跳过目录（原地修改 dirs 影响 os.walk 递归）
            dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES]
            for fn in sorted(files):
                if fn.lower().endswith(SUPPORTED_EXTS):
                    photo_path = os.path.join(root, fn)
                    results.append((year, month, date_str, photo_path))

    if skipped_dirs:
        logger.warning(
            f"⚠️ 跳过 {len(skipped_dirs)} 个不符合 YYYY-MM-DD-* 命名的目录："
            f"{', '.join(skipped_dirs[:10])}{'...' if len(skipped_dirs) > 10 else ''}"
        )
    return results


def derive_output_dir(source_dir, mode):
    """
    自动派生输出目录：在 source_dir 同级生成 "按年归集" / "按月归集"。
    例：D:\\JM照片_整理输出 -> D:\\JM照片_按年归集
    """
    parent = os.path.dirname(source_dir.rstrip(os.sep))
    base_name = os.path.basename(source_dir.rstrip(os.sep))
    suffix = "按年归集" if mode == "year" else "按月归集"
    return os.path.join(parent, f"{base_name}_{suffix}")


def period_folder_name(year, month, mode):
    """生成年或月目录名。"""
    if mode == "year":
        return year
    return f"{year}-{month}"


def resolve_conflict(dest_path, date_str, src_path):
    """
    目标路径已存在时，生成不冲突的新路径。
    策略：加 _日期_源路径hash8 后缀；仍冲突则追加序号 _2/_3。
    """
    basename = os.path.basename(dest_path)
    stem, ext = os.path.splitext(basename)
    h = hashlib.md5(os.path.abspath(src_path).encode("utf-8")).hexdigest()[:8]
    date_compact = date_str.replace("-", "")
    alt = os.path.join(os.path.dirname(dest_path), f"{stem}_{date_compact}_{h}{ext}")
    if not os.path.exists(alt):
        return alt
    n = 2
    while True:
        cand = os.path.join(os.path.dirname(dest_path), f"{stem}_{date_compact}_{h}_{n}{ext}")
        if not os.path.exists(cand):
            return cand
        n += 1


def main():
    parser = argparse.ArgumentParser(
        description="按年或按月归集已整理完成的输出目录照片（COPY，不改原目录）"
    )
    parser.add_argument(
        "--input", "-i",
        help="输入目录（02/03 的 target_dir）。省略时从 pipeline_config.yaml 读取",
    )
    parser.add_argument(
        "--mode", "-m",
        choices=["year", "month"],
        default="year",
        help="归集粒度：year（默认）或 month",
    )
    parser.add_argument(
        "--output", "-o",
        help="输出目录。省略时在输入目录同级自动派生（XXX_按年归集 / XXX_按月归集）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只预览将要执行的操作，不实际复制文件",
    )
    args = parser.parse_args()

    source_dir = args.input or _COMMON.get("target_dir", "")
    if not source_dir:
        logger.error("❌ 未指定输入目录，且配置文件中无 common.target_dir")
        sys.exit(1)
    source_dir = os.path.abspath(source_dir)
    if not os.path.isdir(source_dir):
        logger.error(f"❌ 输入目录不存在：{source_dir}")
        sys.exit(1)

    output_dir = args.output or derive_output_dir(source_dir, args.mode)
    output_dir = os.path.abspath(output_dir)

    logger.info("=" * 60)
    logger.info(f"📂 输入目录：{source_dir}")
    logger.info(f"📂 输出目录：{output_dir}")
    logger.info(f"📊 归集粒度：{'按年' if args.mode == 'year' else '按月'}")
    if args.dry_run:
        logger.info("🔍 DRY-RUN 模式：仅预览，不实际复制")
    logger.info("=" * 60)

    # 扫描
    photos = scan_source_dir(source_dir)
    if not photos:
        logger.info("📭 输入目录下没有符合 YYYY-MM-DD-* 命名的事件子文件夹或照片，无需处理")
        return

    # 统计
    periods = defaultdict(int)
    for year, month, _, _ in photos:
        periods[period_folder_name(year, month, args.mode)] += 1

    logger.info(f"📋 扫描到 {len(photos)} 张照片，分布在 {len(periods)} 个{'年' if args.mode == 'year' else '月'}份：")
    for p in sorted(periods.keys()):
        logger.info(f"   {p}: {periods[p]} 张")

    if args.dry_run:
        logger.info("✅ DRY-RUN 完成，未复制任何文件")
        return

    # 创建输出目录
    os.makedirs(output_dir, exist_ok=True)

    # 复制
    copied = 0
    conflicts = 0
    errors = 0
    for year, month, date_str, photo_path in photos:
        period_name = period_folder_name(year, month, args.mode)
        period_dir = os.path.join(output_dir, period_name)
        os.makedirs(period_dir, exist_ok=True)

        basename = os.path.basename(photo_path)
        dest = os.path.join(period_dir, basename)
        if os.path.exists(dest):
            # 同名冲突：加日期+哈希后缀
            dest = resolve_conflict(dest, date_str, photo_path)
            conflicts += 1

        try:
            shutil.copy2(photo_path, dest)
            copied += 1
        except Exception as e:
            logger.error(f"❌ 复制失败 {photo_path} -> {dest}: {e}")
            errors += 1

        if copied % 500 == 0 and copied > 0:
            logger.info(f"   已复制 {copied}/{len(photos)} 张...")

    logger.info("=" * 60)
    logger.info(f"✅ 完成！共复制 {copied} 张照片到 {output_dir}")
    logger.info(f"   同名冲突处理：{conflicts} 张（加日期+哈希后缀）")
    if errors:
        logger.warning(f"   ⚠️ 复制失败：{errors} 张")
    logger.info(f"   原输出目录未改动：{source_dir}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
