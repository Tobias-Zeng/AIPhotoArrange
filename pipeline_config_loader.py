# pipeline_config_loader.py
"""
流水线统一配置加载器。

目标：把每次运行需要手动调整的参数（输入/输出目录、HOME_CITY、PROVIDER、
NUM_WORKERS 等）从三个处理脚本里剥离出来，集中放到 pipeline_config.yaml。
三个脚本和 run_pipeline.py 都通过本模块读取同一份配置，保证文件名、目录、
参数完全一致。

设计要点：
- profile：当前处理的用户/批次标识。用它给"用户专属"的中间文件加后缀，
  实现多用户隔离，既不用每次手动改名，也不会误删别的用户还在用的文件。
- 多 profile：配置里可以在 profiles: 段下保留任意多个 profile，每个 profile
  只写与共享默认值不同的部分（如 source_dir / target_dir）。顶层 profile 字段
  指定当前激活哪个；也可用环境变量 PIPELINE_PROFILE 或 run_pipeline.py 的
  --profile 临时覆盖。加载时把激活 profile 的覆盖项深合并进 common/chunker/
  geo/curator，脚本读取时无感知。
- 找不到配置文件时返回空 dict，脚本会退回到自身内置默认值（向后兼容）。
- 配置文件路径优先级：显式传参 > 环境变量 PIPELINE_CONFIG > 脚本同目录下的
  pipeline_config.yaml。
"""

import os
import sys

# Nuitka 编译后，__file__ 指向编译产物内部目录，而非 .exe 同级目录。
# 此时外部资源（pipeline_config.yaml / prompts/ / 中间文件 / logs/）都放在
# .exe 同级目录，用 sys.argv[0] 定位最可靠（--standalone / --onefile 均适用）。
# 开发模式下保持 __file__ 行为，外部资源在源码同级目录。
if "__compiled__" in globals():          # Nuitka 编译后注入的全局标志
    BASE_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
else:                                     # 开发模式
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_CONFIG_PATH = os.path.join(BASE_DIR, "pipeline_config.yaml")

# 会被 profile 覆盖项深合并的配置段
_MERGEABLE_SECTIONS = ("common", "chunker", "geo", "curator", "daily_summary", "yearly_summary")


def _deep_merge(base, override):
    """把 override 深合并进 base（override 优先），返回新 dict。"""
    result = dict(base or {})
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _apply_active_profile(cfg):
    """
    解析激活的 profile 并把其覆盖项合并进各配置段。
    激活优先级：环境变量 PIPELINE_PROFILE > 顶层 profile 字段。
    profiles 段不存在或未命中时，行为与单 profile 完全一致。
    """
    if not cfg:
        return cfg

    active = (os.environ.get("PIPELINE_PROFILE") or cfg.get("profile") or "").strip()
    cfg["profile"] = active  # 归一化，后续 get_profile / 文件名派生都用它

    profiles = cfg.get("profiles") or {}
    overrides = profiles.get(active) if active else None
    if not overrides:
        return cfg

    for section in _MERGEABLE_SECTIONS:
        if section in overrides:
            cfg[section] = _deep_merge(cfg.get(section, {}), overrides[section])
    return cfg


def load_config(path=None):
    """读取 YAML 配置，返回 dict。文件缺失或解析失败时抛异常由调用方兜底。"""
    path = path or os.environ.get("PIPELINE_CONFIG") or DEFAULT_CONFIG_PATH
    if not path or not os.path.exists(path):
        return {}
    import yaml  # 延迟导入，缺依赖时报错更清晰
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return _apply_active_profile(data or {})


def list_profiles(cfg):
    """返回配置里定义的所有 profile 名（用于展示/校验）。"""
    return list((cfg.get("profiles") or {}).keys())



def get_profile(cfg):
    """当前 profile（用户/批次标识），去掉首尾空白。空表示用默认文件名。"""
    return (cfg.get("profile") or "").strip()


def resolve_filenames(cfg):
    """
    根据 profile 计算各阶段读写的文件名。

    - 用户专属文件带 _<profile> 后缀：批次、phash 缓存、geo review、进度、
      未处理清单、（可选）geo alias。
    - 跨用户共享文件不加后缀：amap 坐标缓存。
    - geo_alias：优先用带 profile 后缀的文件，不存在则回退到基础 01b_geo_alias.json。
    """
    profile = get_profile(cfg)
    suffix = f"_{profile}" if profile else ""

    names = {
        "batches_file":     f"01_photo_batches{suffix}.json",
        "phash_cache_file": f"01a_phash_cache{suffix}.json",
        "geo_review_file":  f"01b_geo_review{suffix}.json",
        "amap_cache_file":  "01b_amap_cache.json",          # 跨用户共享
        "progress_file":    f"02_progress{suffix}.json",
        "unprocessed_log":  f"02_unprocessed_photos_manual_review{suffix}.txt",
        "daily_summary_progress_file": f"03a_progress{suffix}.json",
        "yearly_summary_progress_file": f"03b_progress{suffix}.json",
    }

    prof_alias = f"01b_geo_alias{suffix}.json"
    if suffix and os.path.exists(os.path.join(BASE_DIR, prof_alias)):
        names["geo_alias_file"] = prof_alias
    else:
        names["geo_alias_file"] = "01b_geo_alias.json"

    return names


# ==========================================
# 输入目录扫描常量与快照计算
# ------------------------------------------
# 这两个常量原本定义在 stage01a_phash_chunker.py，现移到此处统一维护，
# 供 stage01a 的扫描和 gui 的断点续跑校验共用单一来源（避免两份逻辑漂移）。
# ==========================================

# 递归扫描时跳过的目录名（隐藏目录/系统目录/SYNology 缩略图等）
SKIP_DIR_NAMES = {
    ".thumbnails", "_FAILED_FOR_MANUAL_REVIEW", "@eaDir",
    "Thumbs.db", "$RECYCLE.BIN", "System Volume Information",
}

# 支持的照片扩展名
SUPPORTED_EXTS = ('.jpg', '.jpeg', '.png')

# 壁纸/缓存类文件前缀黑名单（华为 EMUI 魔法锁屏壁纸等）
# 这类文件无 EXIF、文件名含 UUID/hex，日期正则会从 hex 串误匹配出"合法日期"
# （实测 2724 个壁纸文件中有 9 个 hex 片段恰好构成合法年月日），
# 且它们不是用户拍摄的照片，归档无意义，扫描时直接跳过。
# 注意：只列 magazine-unlock-；img- 前缀靠文件名日期年份合理性兜底
# （见 stage01a.get_photo_time），避免误伤 IMG- 等少见但合法的命名变体。
WALLPAPER_FILE_PREFIXES = ("magazine-unlock-",)


def compute_source_dir_snapshot(source_dir):
    """
    计算输入目录的内容快照 hash，用于检测中断期间输入目录是否变化。

    快照覆盖：相对路径 + mtime（纳秒精度）+ size。能检测：
    - 新增文件（hash 变化）
    - 删除文件（hash 变化）
    - 文件内容修改（mtime/size 变化，纳秒精度极大降低同秒内修改的碰撞概率）
    - 文件重命名（相对路径变化）

    不覆盖：同纳秒内替换为同 size 的不同内容（概率极低，可接受）。

    与 stage01a.scan_all_photos 共用扫描逻辑（同样的目录过滤、扩展名过滤、
    软链接跳过），但这里需要 stat 每个文件，比 scan_all_photos 多一次系统调用。

    支持多目录：source_dir 可传 ";" 分隔的多个目录（如 "D:\\d1;D:\\d2"），
    每个子目录各自递归扫描、relpath 以各自子目录为基准。多目录间存在包含/重叠
    时，按 realpath 去重（同一物理文件只计入一次），避免 file_count 虚高和
    重复喂 hash。单目录（无 ";"）时行为与历史版本完全一致。

    返回 {"hash": sha256 十六进制字符串, "file_count": 文件数}。
    source_dir 不存在时返回 {"hash": 空输入的 sha256, "file_count": 0}。

    性能：15 万张照片 os.walk + stat 约 10-30 秒（Windows 上 stat 是 syscall）。
    01a 调用：01a 本身要几分钟，这点开销可忽略。
    GUI 调用：断点续跑跳过判定时，相比跑完 01a/01b 的几分钟仍是巨大优化；
    GUI 在后台线程调用，扫描期间给用户提示，不冻结界面。
    """
    import hashlib
    h = hashlib.sha256()
    file_count = 0
    seen = set()  # realpath 集合，跨子目录去重（防止重叠目录导致同一文件被重复计入）
    sub_dirs = [p.strip() for p in str(source_dir).split(";") if p.strip()]
    for sub_dir in sub_dirs:
        for root, dirs, files in os.walk(sub_dir, followlinks=False):
            # 与 scan_all_photos 完全一致的目录过滤
            dirs[:] = [
                d for d in dirs
                if not d.startswith('.')
                and d not in SKIP_DIR_NAMES
                and not os.path.islink(os.path.join(root, d))
            ]
            entries = []
            for file in files:
                if not file.lower().endswith(SUPPORTED_EXTS):
                    continue
                # 与 scan_all_photos 一致：跳过壁纸/缓存文件
                if file.lower().startswith(WALLPAPER_FILE_PREFIXES):
                    continue
                full_path = os.path.join(root, file)
                if os.path.islink(full_path):
                    continue
                # 跨子目录去重：realpath 相同的文件只计入一次
                try:
                    rp = os.path.realpath(full_path)
                except OSError:
                    rp = full_path
                if rp in seen:
                    continue
                try:
                    st = os.stat(full_path)
                except OSError:
                    continue
                seen.add(rp)
                rel = os.path.relpath(full_path, sub_dir)
                # 用 \x00 分隔避免路径里出现分隔符导致碰撞
                entries.append((rel, st.st_mtime_ns, st.st_size))
            entries.sort()  # 按相对路径排序，确保顺序稳定
            for rel, mtime_ns, size in entries:
                h.update(f"{rel}\x00{mtime_ns}\x00{size}\x00".encode("utf-8"))
            file_count += len(entries)
    return {
        "hash": h.hexdigest(),
        "file_count": file_count,
    }
