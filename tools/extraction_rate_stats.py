# extraction_rate_stats.py
"""
按档位统计照片提取率。

用途：切换 curator.extraction_level（A 精华档 / B 纪念档 / C 归类档）跑完 02 后，
用本脚本核对实际提取率是否落在预期区间（A≈30% / B≈50%-80% / C≈100%）。

数据口径（都基于当前激活 profile，自动从 pipeline_config.yaml 派生文件名/目录）：
- 分母有两种：
    · 入库口径（进入 02 的照片数）：01_photo_batches*.json 里 batches 的照片总数，
      即 01a 元数据预筛之后、真正送 LLM 的数量。这是衡量"筛选松紧度"的正确分母。
    · 全库口径（扫描到的照片数）：01a summary.total_scanned，含被元数据预筛剔除的废片。
      C 归类档会关闭元数据预筛，此时两个口径基本一致。
- 分子（实际归档的照片数）：target_dir 下所有图片文件数，排除失败兜底目录
  _FAILED_FOR_MANUAL_REVIEW。这是磁盘上真实产出，比日志统计更可靠。

用法：
    python extraction_rate_stats.py                 # 统计当前激活 profile
    python extraction_rate_stats.py --profile JM    # 临时指定 profile
    python extraction_rate_stats.py --top 15        # 额外列出照片数最多的前 N 个事件
    python extraction_rate_stats.py --json          # 以 JSON 输出（供其它工具消费）
"""

import os
import sys
import json
import argparse

# 本脚本位于 tools/ 子目录，需把项目根目录加入 sys.path 才能 import 根目录下的
# pipeline_config_loader（与 run_gui.py 一致的 BASE_DIR 推导逻辑）。
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pipeline_config_loader as cfg_loader

# 与 02_aesthetic_curator.py 保持一致
FAILED_DIR_NAME = "_FAILED_FOR_MANUAL_REVIEW"
IMAGE_EXTS = (".jpg", ".jpeg", ".png")

# 各档位的预期提取率区间（入库口径），仅用于给出"是否符合预期"的提示。
# A 档目标约 30%，但小样本（如 287 验证集）实测可到 ~41%，上界放到 45% 避免误报。
LEVEL_EXPECTED = {
    "A": (0.20, 0.45, "精华档"),
    "B": (0.50, 0.80, "纪念档"),
    "C": (0.99, 1.00, "归类档"),
}



def count_batches_photos(batches_path):
    """读取 01a 批次文件，返回 (入库照片数, 全库扫描数, 预筛剔除数, 批次数)。"""
    if not os.path.exists(batches_path):
        return None
    with open(batches_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    batches = data.get("batches", []) or []
    in_pipeline = sum(len(b.get("photos", []) or []) for b in batches)
    summary = data.get("summary", {}) or {}
    total_scanned = summary.get("total_scanned")
    trashed = summary.get("trashed")
    return {
        "in_pipeline": in_pipeline,
        "total_scanned": total_scanned,
        "trashed": trashed,
        "batch_count": len(batches),
    }


def count_archived_photos(target_dir):
    """
    递归统计 target_dir 下已归档的图片数，排除失败兜底目录。
    返回 (总数, {事件名: 张数})。事件名取 target_dir 下的一级子目录名。
    """
    if not os.path.isdir(target_dir):
        return None, {}
    total = 0
    by_event = {}
    for entry in os.scandir(target_dir):
        if not entry.is_dir():
            continue
        if entry.name == FAILED_DIR_NAME:
            continue
        cnt = 0
        for root, dirs, files in os.walk(entry.path):
            for fn in files:
                if fn.lower().endswith(IMAGE_EXTS):
                    cnt += 1
        by_event[entry.name] = cnt
        total += cnt
    return total, by_event


def count_failed_photos(target_dir):
    """统计失败兜底目录里的图片数（不计入提取率，仅作参考）。"""
    failed_dir = os.path.join(target_dir, FAILED_DIR_NAME)
    if not os.path.isdir(failed_dir):
        return 0
    cnt = 0
    for root, dirs, files in os.walk(failed_dir):
        for fn in files:
            if fn.lower().endswith(IMAGE_EXTS):
                cnt += 1
    return cnt


def _pct(numer, denom):
    if not denom:
        return None
    return numer / denom


def build_report(cfg):
    files = cfg_loader.resolve_filenames(cfg)
    common = cfg.get("common", {}) or {}
    curator = cfg.get("curator", {}) or {}
    profile = cfg_loader.get_profile(cfg)
    level = str(curator.get("extraction_level", "A")).strip().upper()[:1] or "A"
    if level not in ("A", "B", "C"):
        level = "A"

    base_dir = cfg_loader.BASE_DIR
    batches_path = os.path.join(base_dir, files["batches_file"])
    target_dir = common.get("target_dir", "")

    batches = count_batches_photos(batches_path)
    archived, by_event = count_archived_photos(target_dir)
    failed = count_failed_photos(target_dir) if target_dir else 0

    report = {
        "profile": profile or "(默认)",
        "extraction_level": level,
        "level_name": LEVEL_EXPECTED.get(level, (None, None, ""))[2],
        "batches_file": files["batches_file"],
        "target_dir": target_dir,
        "batches_found": batches is not None,
        "target_found": archived is not None,
        "in_pipeline": batches["in_pipeline"] if batches else None,
        "total_scanned": batches["total_scanned"] if batches else None,
        "trashed": batches["trashed"] if batches else None,
        "batch_count": batches["batch_count"] if batches else None,
        "archived": archived,
        "failed": failed,
        "rate_in_pipeline": None,
        "rate_total_scanned": None,
        "expected_range": LEVEL_EXPECTED.get(level, (None, None, ""))[:2],
        "within_expected": None,
        "by_event": by_event,
    }

    if batches and archived is not None:
        report["rate_in_pipeline"] = _pct(archived, batches["in_pipeline"])
        if batches["total_scanned"]:
            report["rate_total_scanned"] = _pct(archived, batches["total_scanned"])
        lo, hi = report["expected_range"]
        r = report["rate_in_pipeline"]
        if r is not None and lo is not None:
            report["within_expected"] = (lo <= r <= hi)

    return report


def print_report(report, top_n=0):
    def fmt_pct(v):
        return f"{v*100:.1f}%" if v is not None else "N/A"

    print("=" * 64)
    print("📊 照片提取率统计")
    print("=" * 64)
    print(f"  Profile          : {report['profile']}")
    lvl = report["extraction_level"]
    print(f"  提取档位         : {lvl} {report['level_name']}")
    print(f"  批次文件         : {report['batches_file']}")
    print(f"  归档目录         : {report['target_dir']}")
    print("-" * 64)

    if not report["batches_found"]:
        print(f"  ⚠️ 找不到批次文件，无法统计分母。请先跑 01a。")
        print("=" * 64)
        return
    if not report["target_found"]:
        print(f"  ⚠️ 找不到归档目录，无法统计分子。请先跑 02。")
        print("=" * 64)
        return

    ts = report["total_scanned"]
    tr = report["trashed"]
    print(f"  扫描到照片(全库) : {ts if ts is not None else 'N/A'}"
          + (f"（元数据预筛剔除 {tr}）" if tr is not None else ""))
    print(f"  进入 02(入库)    : {report['in_pipeline']}  |  批次数 {report['batch_count']}")
    print(f"  实际归档(提取)   : {report['archived']}")
    if report["failed"]:
        print(f"  失败兜底(未计入) : {report['failed']}")
    print("-" * 64)

    lo, hi = report["expected_range"]
    print(f"  提取率(入库口径) : {fmt_pct(report['rate_in_pipeline'])}"
          f"   = 归档 {report['archived']} / 入库 {report['in_pipeline']}")
    if report["rate_total_scanned"] is not None:
        print(f"  提取率(全库口径) : {fmt_pct(report['rate_total_scanned'])}"
              f"   = 归档 {report['archived']} / 全库 {report['total_scanned']}")

    if lo is not None:
        verdict = report["within_expected"]
        mark = "✅ 符合预期" if verdict else "⚠️ 偏离预期"
        print(f"  预期区间({lvl}档)  : {lo*100:.0f}%-{hi*100:.0f}%   {mark}")
    print("=" * 64)

    if top_n and report["by_event"]:
        print(f"\n📁 照片数最多的前 {top_n} 个事件：")
        items = sorted(report["by_event"].items(), key=lambda kv: kv[1], reverse=True)
        for name, cnt in items[:top_n]:
            print(f"    {cnt:>5d}  {name}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="按档位统计照片提取率")
    parser.add_argument("--profile", default=None,
                        help="临时指定激活的 profile（覆盖配置文件里的 profile 字段）")
    parser.add_argument("--top", type=int, default=0,
                        help="额外列出照片数最多的前 N 个事件")
    parser.add_argument("--json", action="store_true",
                        help="以 JSON 输出（供其它工具消费）")
    args = parser.parse_args(argv)

    if args.profile:
        # 与 run_pipeline.py 一致：用环境变量覆盖激活 profile，不改配置文件
        os.environ["PIPELINE_PROFILE"] = args.profile

    cfg = cfg_loader.load_config()
    if not cfg:
        print("❌ 找不到 pipeline_config.yaml，无法统计。")
        return 1

    report = build_report(cfg)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report, top_n=args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
