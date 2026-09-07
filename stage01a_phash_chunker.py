# 01_phash_chunker.py
import os
import re
import sys
import json
import logging
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from PIL import Image, ExifTags, ImageOps
Image.MAX_IMAGE_PIXELS = 200_000_000
import imagehash
from PIL.ExifTags import GPSTAGS  # GPS 字段名解析（兼容性兜底用）

# ==========================================
# 1. 配置区
# ==========================================

# 配置注意事项：
# 所有需要每次调整的参数都集中在 pipeline_config.yaml，本脚本从那里读取。
# 找不到配置文件时，退回到下面的内置默认值（向后兼容）。

import pipeline_config_loader as _cfg_loader

_CONFIG = _cfg_loader.load_config()
_COMMON = _CONFIG.get("common", {})
_CHUNKER = _CONFIG.get("chunker", {})
_CURATOR = _CONFIG.get("curator", {})
_FILES = _cfg_loader.resolve_filenames(_CONFIG)

# 扫描常量统一从 loader 导入（gui 的断点续跑校验也用同一份，避免逻辑漂移）
from pipeline_config_loader import (
    SKIP_DIR_NAMES, SUPPORTED_EXTS, compute_source_dir_snapshot,
    WALLPAPER_FILE_PREFIXES,
)

# 照片提取档位（与 02 共用同一配置项）。C = 归类档时关闭元数据废片预筛，
# 做到尽量一张不落；A/B 档保持原有预筛行为。取值归一化为大写单字母。
EXTRACTION_LEVEL = str(_CURATOR.get("extraction_level", "A")).strip().upper()[:1] or "A"
# 归类档：跳过 size/分辨率/长宽比预筛，仅保留"无法读取"这类硬性排除
SKIP_METADATA_PREFILTER = (EXTRACTION_LEVEL == "C")


SOURCE_DIR = os.path.normpath(_COMMON.get("source_dir", r"D:\JM照片_整理输入"))
# 中间文件用 BASE_DIR 绝对路径，Nuitka 编译后也指向 .exe 同级目录
BATCHES_FILE = os.path.join(_cfg_loader.BASE_DIR, _FILES["batches_file"])      # 👈 输出文件，照片批次分割，供02脚本使用
PHASH_CACHE_FILE = os.path.join(_cfg_loader.BASE_DIR, _FILES["phash_cache_file"])   # 👈 phash 持久化缓存

# === 切批主参数 ===
PHASH_THRESHOLD = _CHUNKER.get("phash_threshold", 22)
MERGE_THRESHOLD = _CHUNKER.get("merge_threshold", 18)
MAX_BATCH_SIZE = _CHUNKER.get("max_batch_size", 10)

# === 时间双信号 ===
BURST_SECONDS = _CHUNKER.get("burst_seconds", 90)
HARD_CUT_MINUTES = _CHUNKER.get("hard_cut_minutes", 30)
ABA_MERGE_WINDOW_MIN = _CHUNKER.get("aba_merge_window_min", 60)

# === 邻近小批合并 ===
SMALL_BATCH_MERGE = _CHUNKER.get("small_batch_merge", True)
SMALL_BATCH_SIZE = _CHUNKER.get("small_batch_size", 3)
SMALL_BATCH_GAP_MIN = _CHUNKER.get("small_batch_gap_min", 8)

# === 元数据级废片预筛 ===
MIN_FILE_SIZE = int(_CHUNKER.get("min_file_size_kb", 30)) * 1024
MIN_RESOLUTION = _CHUNKER.get("min_resolution", 400)
MAX_ASPECT_RATIO = _CHUNKER.get("max_aspect_ratio", 3.0)

# === 重复照片去重 ===
DEDUP_ENABLED = _CHUNKER.get("dedup_enabled", True)
DEDUP_PHASH_THRESHOLD = _CHUNKER.get("dedup_phash_threshold", 2)


# SKIP_DIR_NAMES / SUPPORTED_EXTS 已移至 pipeline_config_loader 统一维护，
# 供 stage01a 扫描和 gui 断点续跑校验共用（避免两份逻辑漂移）。

PRINT_HISTOGRAM = _CHUNKER.get("print_histogram", True)


# ==========================================
# 2. 日志
# ==========================================
os.makedirs(os.path.join(_cfg_loader.BASE_DIR, "logs"), exist_ok=True)
log_filename = os.path.join(
    _cfg_loader.BASE_DIR, "logs",
    f"01a_phash_chunker_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
)
# Nuitka console=disable 模式下 sys.stderr 可能为 None，StreamHandler 会报错；
# GUI 模式下 sys.stderr 已被 LogTextbox 重定向。None 时跳过 StreamHandler。
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
# 3. 时间提取
# ==========================================
def parse_folder_date(name):
    """从文件夹名前缀提取日期，支持多种格式：
    纯数字：2003-07-15 / 2003-07-15北京行 / 2003-0715 / 2003-0715旅游
            2003-07 / 2003-07暑假 / 2003 / 2003暑假
            14-08-15 / 14-0815 / 14-08（两位数年份，00-68补20，69-99补19）
    中文：  2014年08月15日 / 14年8月5日 / 2014年08月 / 14年 / 2014年8月北京行
            （两位数年份同上）
    返回 datetime 或 None。
    """
    # 中文年月日格式（优先于纯数字，避免 "2014年08月" 被 ^\d{4} 截断为 2014）
    cn_match = re.match(r'^(\d{4}|\d{2})年(?:([0-9]{1,2})月(?:([0-9]{1,2})日)?)?', name)
    if cn_match:
        y, mo, d = cn_match.groups()
        year = int(y)
        if len(y) == 2:
            year += 2000 if year <= 68 else 1900
        month = int(mo) if mo else 1
        day = int(d) if d else 1
        try:
            return datetime(year, month, day)
        except ValueError:
            pass

    # 纯数字格式（按长度从长到短，避免短模式误匹配长格式）
    patterns = [
        (r'^\d{4}-\d{2}-\d{2}', '%Y-%m-%d'),   # 2003-07-15(...)
        (r'^\d{4}-\d{4}', '%Y-%m%d'),           # 2003-0715(...)
        (r'^\d{4}-\d{2}', '%Y-%m'),             # 2003-07(...)
        (r'^\d{2}-\d{2}-\d{2}', '%y-%m-%d'),   # 14-08-15(...)
        (r'^\d{2}-\d{4}', '%y-%m%d'),           # 14-0815(...)
        (r'^\d{2}-\d{2}', '%y-%m'),             # 14-08(...)
        (r'^\d{4}', '%Y'),                       # 2003(...)
    ]
    for regex, fmt in patterns:
        m = re.match(regex, name)
        if m:
            prefix = m.group(0)
            try:
                return datetime.strptime(prefix, fmt)
            except ValueError:
                continue
    return None


def parse_pa_filename_date(filename):
    """从 PA 系列文件名提取月日编码。
    格式：P + 月(1-9为数字, A-C为10-12月) + 日(两位) + 序号
    例如：PA036443 → 10月3日，P1030644 → 1月3日
    返回 (month, day) 或 None。不含年份，年份需从目录名或 mtime 补全。
    """
    m = re.match(r'^P([1-9A-Ca-c])(\d{2})', filename)
    if not m:
        return None
    month_char = m.group(1).upper()
    if month_char in ('A', 'B', 'C'):
        month = 10 + ord(month_char) - ord('A')  # A=10, B=11, C=12
    else:
        month = int(month_char)
    day = int(m.group(2))
    if 1 <= month <= 12 and 1 <= day <= 31:
        return month, day
    return None


def _find_folder_date_in_chain(file_path, parent_folder_name):
    """从父目录及祖先目录名中查找日期，返回第一个命中的 datetime 或 None。
    依次检查：父目录 → 祖父 → 曾祖父 → 高祖父（最多向上 3 级）。
    """
    folder_dt = parse_folder_date(parent_folder_name)
    if folder_dt:
        return folder_dt
    ancestor_dir = os.path.dirname(os.path.dirname(file_path))
    for _ in range(3):
        ancestor_name = os.path.basename(ancestor_dir)
        if not ancestor_name:
            break
        folder_dt = parse_folder_date(ancestor_name)
        if folder_dt:
            return folder_dt
        parent_dir = os.path.dirname(ancestor_dir)
        if parent_dir == ancestor_dir:
            break
        ancestor_dir = parent_dir
    return None


def get_photo_time(file_path, parent_folder_name):
    filename = os.path.basename(file_path)
    try:
        with Image.open(file_path) as img:
            exif = img._getexif() if hasattr(img, '_getexif') else img.getexif()
            if exif:
                # 显式优先 DateTimeOriginal(36867，真实拍摄时间)，
                # 没有再退 DateTime(306，通常是修改/存储时间)。
                # 不能用"遍历命中即返回"：EXIF IFD 常按 tag 号升序排列，
                # 306 < 36867，会导致系统性地误取修改时间。
                dt_original = None
                dt_fallback = None
                for k, v in exif.items():
                    tag = ExifTags.TAGS.get(k)
                    if k == 36867 or tag == 'DateTimeOriginal':
                        dt_original = str(v).strip()
                        break
                    if (k == 306 or tag == 'DateTime') and dt_fallback is None:
                        dt_fallback = str(v).strip()
                raw = dt_original or dt_fallback
                if raw:
                    return datetime.strptime(raw, '%Y:%m:%d %H:%M:%S'), True
    except Exception:
        pass


    wx_match = re.search(r'(?:mmexport|wx_camera_)(\d{10,13})', filename, re.IGNORECASE)
    if wx_match:
        ts = int(wx_match.group(1)) / 1000 if len(wx_match.group(1)) == 13 else int(wx_match.group(1))
        return datetime.fromtimestamp(ts), True

    # PA 文件名日期编码（P+月+日，如 PA036443 = 10月3日）
    # 优先于目录名查找，但仅含月日，需从目录名或 mtime 补年份
    pa_md = parse_pa_filename_date(filename)
    folder_dt = None  # 延迟查找，PA 和目录名兜底共用
    if pa_md:
        pa_month, pa_day = pa_md
        folder_dt = _find_folder_date_in_chain(file_path, parent_folder_name)
        year = folder_dt.year if folder_dt else None
        if year is None:
            # 目录链无年份，从 mtime 取年份
            try:
                mtime = os.path.getmtime(file_path)
                if mtime > 631152000:  # 1990-01-01
                    year = datetime.fromtimestamp(mtime).year
            except Exception:
                pass
        if year:
            try:
                return datetime(year, pa_month, pa_day), False
            except ValueError:
                pass  # 日期不合法（如2月30日），继续向下走其它逻辑

    # 文件名日期识别（EXIF / wx / PA 均未命中时）
    # 统一覆盖所有"文件名中含日期"的格式，用 search 不锚定行首，
    # 自动兼容各种前缀（IMG_/MVIMG_/PXL_/Screenshot/photo_/WhatsApp Image 等）。
    # 日期部分：YYYYMMDD / YYYY-MM-DD / YYYY_MM_DD / YYYY.MM.DD
    # 时间部分（可选）：HHMMSS / HH.MM.SS / HH:MM:SS / HH-MM-SS
    # 日期与时间间分隔：_ / - / . / 空格 / "at "（iOS/WhatsApp 导出）
    # 非法日期（如 12345678 -> 月=56）由 datetime ValueError 天然拦截，继续走目录兜底
    # 年份合理性校验：拦截 hex/UUID 串里碰巧合法的"日期"
    # （如 img-595812251f556be49e10207fd1e3fb70.jpg -> 5958-12-25）
    fname_date_match = re.search(
        r'(\d{4})[-_.]?(\d{2})[-_.]?(\d{2})'
        r'(?:[_\-. ](?:at\s+)?(\d{2})[-.:_]?(\d{2})[-.:_]?(\d{2}))?',
        filename,
    )
    if fname_date_match:
        g = fname_date_match.groups()
        try:
            if g[3] is not None:
                dt = datetime(int(g[0]), int(g[1]), int(g[2]),
                              int(g[3]), int(g[4]), int(g[5]))
            else:
                dt = datetime(int(g[0]), int(g[1]), int(g[2]))
            # 年份范围：1900 ~ 当年。超出视为 hex 误匹配，走目录兜底。
            # 下限 1900 放宽兼容早期扫描件/老照片；上限取当年，排除未来年份
            _max_year = datetime.now().year
            if 1900 <= dt.year <= _max_year:
                return dt, True
            # 年份不合理，继续走目录兜底
        except ValueError:
            pass  # 日期不合法（如12345678、2月30日），继续向下走目录兜底

    # 目录名（父目录 + 祖先目录，老照片目录常含日期前缀，比 mtime 可靠）
    # mtime 是文件最后修改时间，老照片经过复制/迁移后 mtime 往往是最近日期，不代表拍摄时间
    if folder_dt is None:
        folder_dt = _find_folder_date_in_chain(file_path, parent_folder_name)
    if folder_dt:
        return folder_dt, False

    try:
        mtime = os.path.getmtime(file_path)
        if mtime > 631152000:  # 1990-01-01，过滤掉明显异常的旧时间戳
            return datetime.fromtimestamp(mtime), False
    except Exception:
        pass

    return datetime.fromtimestamp(0), False


# ==========================================
# 3.5  GPS 提取（手机/单反 EXIF 兼容）
# ==========================================
def extract_gps(file_path):
    """
    从 EXIF 提取 GPS 坐标，返回 [lat, lng] 十进制度，失败一律返回 None。
    
    兼容性已覆盖：
    - 标准 EXIF（iPhone、华为、佳能/尼康/索尼单反）
    - 小米部分机型（IFDRational 格式）
    - 老 Pillow 版本（DMS tuple 格式）
    - 无 GPS、0,0 占位、越界等异常情况
    """
    try:
        with Image.open(file_path) as img:
            # 路径 1：新版 PIL，标准 IFD 拿法
            gps_ifd = None
            try:
                exif = img.getexif()
                if exif:
                    gps_ifd = exif.get_ifd(0x8825)  # 0x8825 = GPSInfo
            except Exception:
                gps_ifd = None
            
            # 路径 2：老版 PIL 兜底
            if not gps_ifd:
                try:
                    raw_exif = img._getexif() if hasattr(img, '_getexif') else None
                    if raw_exif and 34853 in raw_exif:
                        gps_ifd = raw_exif[34853]
                except Exception:
                    return None
            
            if not gps_ifd:
                return None
            
            # 字段编号：1=LatRef, 2=Lat, 3=LngRef, 4=Lng
            lat_dms = gps_ifd.get(2)
            lat_ref = gps_ifd.get(1)
            lng_dms = gps_ifd.get(4)
            lng_ref = gps_ifd.get(3)
            
            if not (lat_dms and lng_dms and lat_ref and lng_ref):
                return None
            
            def to_decimal(dms, ref):
                """DMS 三元组 → 十进制度，兼容 IFDRational/Fraction/tuple/float"""
                def to_float(x):
                    # IFDRational 和 Fraction 都支持 float()
                    # tuple 形式 (num, den) 手工除
                    if isinstance(x, tuple) and len(x) == 2:
                        return x[0] / x[1] if x[1] else 0.0
                    return float(x)
                
                if len(dms) < 3:
                    return None
                d = to_float(dms[0])
                m = to_float(dms[1])
                s = to_float(dms[2])
                val = d + m / 60.0 + s / 3600.0
                ref_str = str(ref).strip().upper()
                if ref_str in ('S', 'W'):
                    val = -val
                return val
            
            lat = to_decimal(lat_dms, lat_ref)
            lng = to_decimal(lng_dms, lng_ref)
            
            if lat is None or lng is None:
                return None
            
            # 合理性过滤
            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                return None
            # 0,0 占位（很多 GPS 未定位时写 0,0 或接近 0）
            if abs(lat) < 0.001 and abs(lng) < 0.001:
                return None
            
            return [round(lat, 6), round(lng, 6)]
    except Exception:
        return None


# ==========================================
# 4. 元数据级废片预筛
# ==========================================
def is_obvious_trash(file_path):
    # 归类档（C）：不做 size/分辨率/长宽比预筛，尽量一张不落。
    # 但仍必须排除"无法打开/读取"的损坏文件——它们无法编码送 LLM，也算不出 phash。
    if SKIP_METADATA_PREFILTER:
        try:
            with Image.open(file_path) as img:
                img.verify()
        except Exception as e:
            return True, f"unreadable:{e}"
        return False, None

    try:
        size = os.path.getsize(file_path)
        if size < MIN_FILE_SIZE:
            return True, "size_too_small"
        with Image.open(file_path) as img:
            w, h = img.size
        if w < MIN_RESOLUTION or h < MIN_RESOLUTION:
            return True, "resolution_too_low"
        ratio = max(w, h) / min(w, h)
        if ratio > MAX_ASPECT_RATIO:
            return True, "extreme_aspect_ratio"
    except Exception as e:
        return True, f"unreadable:{e}"
    return False, None



# ==========================================
# 5. 递归扫描全库
# ==========================================
def scan_all_photos(source_dir):
    """递归扫描，跳过隐藏目录/系统目录/软链接。

    支持多目录：source_dir 可传 ";" 分隔的多个目录（如 "D:\\d1;D:\\d2"），
    每个子目录各自递归扫描。多目录间存在包含/重叠时，按 realpath 去重
    （同一物理文件只采集一次），避免 01a 后续 phash/dedup 收到重复文件。
    单目录（无 ";"）时行为与历史版本完全一致。
    """
    photos = []
    seen = set()  # realpath 集合，跨子目录去重（防止重叠目录导致同一文件被重复采集）
    sub_dirs = [p.strip() for p in str(source_dir).split(";") if p.strip()]
    for sub_dir in sub_dirs:
        for root, dirs, files in os.walk(sub_dir, followlinks=False):
            # 原地修改 dirs 阻止 os.walk 进入这些目录
            dirs[:] = [
                d for d in dirs
                if not d.startswith('.')
                and d not in SKIP_DIR_NAMES
                and not os.path.islink(os.path.join(root, d))
            ]
            for file in files:
                if file.lower().endswith(SUPPORTED_EXTS):
                    # 跳过壁纸/缓存文件（华为 magazine-unlock 锁屏壁纸等）
                    # 这类文件无 EXIF，文件名含 UUID/hex 会导致日期正则误匹配出
                    # "合法日期"（实测 9 个 hex 片段恰好构成合法年月日）
                    if file.lower().startswith(WALLPAPER_FILE_PREFIXES):
                        continue
                    full_path = os.path.join(root, file)
                    if os.path.islink(full_path):
                        continue
                    # 跨子目录去重：realpath 相同的文件只采集一次
                    try:
                        rp = os.path.realpath(full_path)
                    except OSError:
                        rp = full_path
                    if rp in seen:
                        continue
                    seen.add(rp)
                    photos.append(full_path)
    return photos


# ==========================================
# 6. phash 计算（带磁盘缓存）
# ==========================================
def compute_phash_image(file_path):
    try:
        with Image.open(file_path) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode != 'RGB':
                img = img.convert('RGB')
            img.thumbnail((512, 512))
            return imagehash.phash(img, hash_size=8)
    except Exception as e:
        logger.warning(f"  [phash 失败] {file_path}: {e}")
        return None


def _phash_worker_from_bytes(file_path, raw_bytes):
    """线程池 worker：从已读入内存的字节流计算 phash（不碰磁盘）。
    主线程已顺序读文件（HDD 友好），worker 只做 CPU 解码+DCT。
    返回 (file_path, phash_hex_str 或 None)。
    """
    try:
        from io import BytesIO
        with Image.open(BytesIO(raw_bytes)) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode != 'RGB':
                img = img.convert('RGB')
            img.thumbnail((512, 512))
            ph = imagehash.phash(img, hash_size=8)
        return file_path, str(ph)
    except Exception as e:
        logger.warning(f"  [phash 失败] {file_path}: {e}")
        return file_path, None


def load_phash_cache():
    if not os.path.exists(PHASH_CACHE_FILE):
        return {}
    try:
        with open(PHASH_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"phash 缓存加载失败，将重建：{e}")
        return {}


def save_phash_cache(cache):
    tmp = PHASH_CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)   # 👈 加上 indent=2
    os.replace(tmp, PHASH_CACHE_FILE)


def get_or_compute_phash(file_path, cache):
    """文件 mtime 作指纹，避免文件被替换后用旧 phash"""
    try:
        mtime = os.path.getmtime(file_path)
    except Exception:
        return compute_phash_image(file_path), False

    cached = cache.get(file_path)
    if cached and abs(cached.get("mtime", 0) - mtime) < 1.0:
        try:
            return imagehash.hex_to_hash(cached["phash"]), True
        except Exception:
            pass

    ph = compute_phash_image(file_path)
    if ph is not None:
        cache[file_path] = {"mtime": mtime, "phash": str(ph)}
    return ph, False


# ==========================================
# 6.5 重复照片去重
# ==========================================
def _name_stem(filename):
    """取文件名去扩展名部分（大小写不敏感比较用，返回小写 stem）"""
    return os.path.splitext(filename)[0].lower()


def filename_match(name_a, name_b):
    """
    判断两个文件名是否构成"重复关系"（大小写不敏感，忽略扩展名）。
    匹配规则：
      1) 同名：stem 完全相同（如 IMG_1119.JPG vs IMG_1119.jpg）
      2) 包含且后缀以分隔符开头：短 stem 是长 stem 的前缀，且长名在短 stem
         之后紧跟非数字字符（如 IMG_1119 vs IMG_1119_resized、photo vs photo_small）
    排除纯数字后缀（如 IMG_1208 vs IMG_1209 = 连拍序号，不匹配）。
    """
    sa, sb = _name_stem(name_a), _name_stem(name_b)
    if sa == sb:
        return True
    shorter, longer = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    if longer.startswith(shorter):
        after = longer[len(shorter):]
        # after 非空且首字符非数字 -> 后缀以分隔符/字母开头，视为重复变体
        if after and not after[0].isdigit():
            return True
    return False


def dedup_within_day(day_photos, phash_threshold=DEDUP_PHASH_THRESHOLD):
    """
    同日内重复照片去重。
    条件：phash 距离 ≤ phash_threshold 且 filename_match 为真。
    每组保留文件 size 最大者（原片优先），其余标记为副本丢弃。

    返回 (kept_photos, dropped_photos)。
    dropped_photos 中每个元素为 dict: {file, filename, reason, kept_file}
    """
    if len(day_photos) < 2:
        return list(day_photos), []

    n = len(day_photos)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    # 两两比较：phash 距离 + 文件名匹配，满足双条件才合并
    for i in range(n):
        pi = day_photos[i]
        if pi.get("phash") is None:
            continue
        for j in range(i + 1, n):
            pj = day_photos[j]
            if pj.get("phash") is None:
                continue
            dist = pi["phash"] - pj["phash"]
            if dist > phash_threshold:
                continue
            if not filename_match(pi["filename"], pj["filename"]):
                continue
            union(i, j)

    # 按组聚合
    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)

    kept, dropped = [], []
    for indices in groups.values():
        if len(indices) == 1:
            kept.append(day_photos[indices[0]])
            continue
        # 多张重复：保留 size 最大者（原片优先）
        members = [day_photos[i] for i in indices]
        members_sorted = sorted(
            members,
            key=lambda p: os.path.getsize(p["file"]) if os.path.exists(p["file"]) else 0,
            reverse=True,
        )
        keeper = members_sorted[0]
        kept.append(keeper)
        for m in members_sorted[1:]:
            dropped.append({
                "file": m["file"],
                "filename": m["filename"],
                "reason": "dup_of:%s" % keeper["filename"],
                "kept_file": keeper["file"],
            })
    return kept, dropped


# ==========================================
# 7. 时间 + phash 双信号切片
# ==========================================
def cluster_by_phash_and_time(daily_photos, distance_log=None):
    if not daily_photos:
        return []
    events = [[daily_photos[0]]]
    for prev, curr in zip(daily_photos, daily_photos[1:]):
        time_gap = (curr["_dt"] - prev["_dt"]).total_seconds()
        if time_gap < BURST_SECONDS:
            events[-1].append(curr)
            continue
        if time_gap > HARD_CUT_MINUTES * 60:
            events.append([curr])
            continue
        if prev["phash"] is None or curr["phash"] is None:
            events[-1].append(curr)
            continue
        dist = prev["phash"] - curr["phash"]
        if distance_log is not None:
            distance_log.append(dist)
        if dist > PHASH_THRESHOLD:
            events.append([curr])
        else:
            events[-1].append(curr)
    return events


# ==========================================
# 8. ABA 模式合并
# ==========================================
def merge_aba_segments(segments, merge_threshold=MERGE_THRESHOLD,
                       time_window_min=ABA_MERGE_WINDOW_MIN):
    if not segments:
        return []

    def seg_repr(seg):
        for p in seg:
            if p["phash"] is not None:
                return p["phash"]
        return None

    n = len(segments)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    reprs = [seg_repr(s) for s in segments]
    times = [(s[0]["_dt"], s[-1]["_dt"]) for s in segments]
    for i in range(n):
        for j in range(i + 1, n):
            if reprs[i] is None or reprs[j] is None:
                continue
            total_span_min = (times[j][1] - times[i][0]).total_seconds() / 60
            if total_span_min > time_window_min:
                continue
            if reprs[i] - reprs[j] <= merge_threshold:
                union(i, j)

    groups = defaultdict(list)
    for i, seg in enumerate(segments):
        groups[find(i)].extend(seg)
    merged = [sorted(photos, key=lambda x: x["_dt"]) for photos in groups.values()]
    merged.sort(key=lambda evt: evt[0]["_dt"])
    return merged


# ==========================================
# 9. 大事件智能二次切批
# ==========================================
def split_oversized_events(events, max_size=MAX_BATCH_SIZE):
    batches = []

    def find_best_split(evt):
        min_side = max(2, max_size // 4)
        if len(evt) < 2 * min_side:
            return len(evt) // 2 - 1
        best_idx = len(evt) // 2 - 1
        best_score = -1
        for i in range(min_side - 1, len(evt) - min_side):
            p1, p2 = evt[i], evt[i + 1]
            if p1["phash"] is None or p2["phash"] is None:
                continue
            phash_dist = p1["phash"] - p2["phash"]
            time_gap_sec = (p2["_dt"] - p1["_dt"]).total_seconds()
            score = phash_dist + min(time_gap_sec / 60, 10)
            if score > best_score:
                best_score = score
                best_idx = i
        return best_idx

    def split_recursive(evt):
        if len(evt) <= max_size:
            batches.append(evt)
            return
        idx = find_best_split(evt)
        split_recursive(evt[:idx + 1])
        split_recursive(evt[idx + 1:])

    for evt in events:
        split_recursive(evt)
    return batches


# ==========================================
# 10. 邻近小批合并
# ==========================================
def merge_small_adjacent_batches(batches, max_size=MAX_BATCH_SIZE,
                                  small_size=SMALL_BATCH_SIZE,
                                  gap_minutes=SMALL_BATCH_GAP_MIN):
    if not batches or len(batches) < 2:
        return batches
    merged = [batches[0]]
    for curr in batches[1:]:
        prev = merged[-1]
        same_day = prev[-1]["_dt"].date() == curr[0]["_dt"].date()
        small_enough = len(prev) <= small_size and len(curr) <= small_size
        within_size = len(prev) + len(curr) <= max_size
        gap = (curr[0]["_dt"] - prev[-1]["_dt"]).total_seconds() / 60
        time_close = gap <= gap_minutes
        if same_day and small_enough and within_size and time_close:
            merged[-1] = prev + curr
        else:
            merged.append(curr)
    return merged


# ==========================================
# 11. 直方图诊断
# ==========================================
def print_distance_histogram(distances):
    if not distances:
        logger.info("  [直方图] 无可统计距离")
        return
    bins = [(0, 5), (6, 10), (11, 15), (16, 20), (21, 25), (26, 30), (31, 40), (41, 64)]
    counts = Counter()
    for d in distances:
        for lo, hi in bins:
            if lo <= d <= hi:
                counts[(lo, hi)] += 1
                break
    total = len(distances)
    max_count = max(counts.values()) if counts else 1
    logger.info("=" * 60)
    logger.info(f"📈 phash 相邻距离分布（共 {total} 个间隙，仅统计中间地带）")
    logger.info("    范围      数量    占比      分布")
    for lo, hi in bins:
        c = counts.get((lo, hi), 0)
        pct = c * 100 / total if total else 0
        bar = "█" * int(c * 40 / max_count)
        logger.info(f"    {lo:>2d}-{hi:<2d}    {c:>5d}   {pct:>5.1f}%    {bar}")
    logger.info("=" * 60)


# ==========================================
# 12. 主流程
# ==========================================
def main():
    logger.info("========== 启动阶段一：phash + 时间双信号切批（递归扫描+phash缓存）==========")
    if SKIP_METADATA_PREFILTER:
        logger.info(
            f"  [档位] {EXTRACTION_LEVEL}（归类档）：已关闭元数据废片预筛"
            f"（size/分辨率/长宽比），仅排除无法读取的损坏文件"
        )
    else:
        logger.info(
            f"  [档位] {EXTRACTION_LEVEL}：启用元数据废片预筛 "
            f"(最小 {MIN_FILE_SIZE//1024}KB / 最小边 {MIN_RESOLUTION}px / 长宽比≤{MAX_ASPECT_RATIO})"
        )
    logger.info(
        f"  [参数] 切片={PHASH_THRESHOLD} | 合并={MERGE_THRESHOLD} | 单批最大={MAX_BATCH_SIZE} | "
        f"连拍={BURST_SECONDS}s | 硬切>{HARD_CUT_MINUTES}min | "

        f"ABA窗={ABA_MERGE_WINDOW_MIN}min | 小批合并={SMALL_BATCH_SIZE}张内+{SMALL_BATCH_GAP_MIN}min内"
    )

    # 第 1 步：递归扫描
    logger.info("📂 第 1/3 步：递归扫描 + 废片预筛 + 提取时间...")
    all_files = scan_all_photos(SOURCE_DIR)
    total_files = len(all_files)
    logger.info(f"   找到 {total_files} 个候选文件，开始预筛...")

    # 计算输入目录内容快照（供 02 断点续跑校验输入是否在中断期间变化）。
    # 快照 = 相对路径 + mtime + size 的 sha256，覆盖增/删/改/重命名。
    # 写入 batches json 顶层，02 的 context_fingerprint 会读它。
    source_dir_snapshot = compute_source_dir_snapshot(SOURCE_DIR)
    logger.info(f"   输入目录快照：{source_dir_snapshot['file_count']} 文件，"
                f"hash={source_dir_snapshot['hash'][:16]}...")

    all_photos, trashed = [], []
    for full_path in all_files:
        is_trash, reason = is_obvious_trash(full_path)
        if is_trash:
            trashed.append({"file": full_path, "reason": reason})
            continue
        # 取直接父目录名作为兜底"年月"提示
        parent_folder_name = os.path.basename(os.path.dirname(full_path))
        dt, is_exact = get_photo_time(full_path, parent_folder_name)
        gps = extract_gps(full_path)  # 新增
        all_photos.append({
            "file": full_path,
            "filename": os.path.basename(full_path),
            "datetime": dt.strftime('%Y-%m-%d %H:%M:%S'),
            "is_exact_time": is_exact,
            "_dt": dt,
            "gps": gps,  # 新增：[lat, lng] 或 None
        })
    logger.info(f"   共扫描 {total_files} 个文件，剔除明显废片 {len(trashed)} 张，保留 {len(all_photos)} 张")

    # 第 2 步：phash 计算（带缓存）
    logger.info(f"🔢 第 2/3 步：计算 phash ({len(all_photos)} 张，使用持久化缓存)...")
    phash_cache = load_phash_cache()
    cache_hit, cache_miss = 0, 0
    SAVE_EVERY = 200
    # 多线程并行计算 phash：缓存命中在主进程过滤，未命中的丢给线程池。
    # JPEG/PNG 解码（libjpeg/libpng，C 实现）会释放 GIL，实测 6 核机器约 3.8x 加速。
    num_workers = max(1, (os.cpu_count() or 2) - 1)

    # 第 1 遍：缓存命中过滤，命中直接赋值；未命中收集到 to_compute
    to_compute = []
    for p in all_photos:
        file_path = p["file"]
        try:
            mtime = os.path.getmtime(file_path)
        except Exception:
            mtime = None
        cached = phash_cache.get(file_path) if mtime is not None else None
        if cached and abs(cached.get("mtime", 0) - mtime) < 1.0:
            try:
                p["phash"] = imagehash.hex_to_hash(cached["phash"])
                cache_hit += 1
                continue
            except Exception:
                pass
        # 未命中（含 mtime 读取失败）：待算，记下 photo 引用与 mtime
        p["_mtime"] = mtime
        to_compute.append(p)

    # 第 2 遍：流水线并行——单线程顺序读取（HDD 友好）+ 线程池并行计算 phash。
    # 照片在机械硬盘上时，多线程并发读会导致磁头疯狂寻道、吞吐骤降。
    # 采用生产者-消费者流水线：1 个读取线程顺序读文件入队（HDD 顺序读最快），
    # 主线程的线程池同时消费上一批做 CPU 解码+DCT（释放 GIL）。
    # I/O 与 CPU 重叠，既避免寻道风暴又充分利用多核。
    total = len(all_photos)
    if to_compute:
        logger.info(
            f"   缓存命中 {cache_hit} 张，待计算 {len(to_compute)} 张，"
            f"使用 {num_workers} 线程并行..."
        )

        import threading
        from queue import Queue

        # 读取队列：读取线程放入 (photo, file_path, raw_bytes)，消费者取出计算。
        # 有界队列让读取线程在计算跟不上时自动背压，避免内存爆炸。
        # 队列容量=2：1块正在算 + 1块预读，刚好实现 I/O 与 CPU 重叠；
        # 不设更大是为了限制内存--单张均值 6.6MB，200张≈1.3GB/块，
        # 队列+计算中合计峰值约 2.6GB，16GB 内存机器安全。
        CHUNK_READ = 200
        READ_QUEUE_SIZE = 2
        read_queue = Queue(maxsize=READ_QUEUE_SIZE)
        READ_SENTINEL = None  # 读取结束标记

        def reader_thread():
            """顺序读取所有待算文件，按 chunk 放入队列。"""
            try:
                for chunk_start in range(0, len(to_compute), CHUNK_READ):
                    chunk = to_compute[chunk_start:chunk_start + CHUNK_READ]
                    chunk_items = []
                    for p in chunk:
                        fp = p["file"]
                        try:
                            with open(fp, "rb") as f:
                                raw = f.read()
                            chunk_items.append((p, fp, raw))
                        except Exception as e:
                            logger.warning(f"  [读取失败] {fp}: {e}")
                            chunk_items.append((p, fp, None))
                    read_queue.put(chunk_items)
            finally:
                read_queue.put(READ_SENTINEL)

        # 启动读取线程
        reader = threading.Thread(target=reader_thread, daemon=True)
        reader.start()

        # 主线程：从队列取 chunk，丢给线程池并行计算（读取与计算重叠）
        done_since_save = 0
        while True:
            chunk_items = read_queue.get()
            if chunk_items is READ_SENTINEL:
                break
            with ThreadPoolExecutor(max_workers=num_workers) as pool:
                futures = {
                    pool.submit(_phash_worker_from_bytes, fp, raw): p
                    for p, fp, raw in chunk_items if raw is not None
                }
                for fut in as_completed(futures):
                    p = futures[fut]
                    fp, phash_hex = fut.result()
                    if phash_hex is not None:
                        p["phash"] = imagehash.hex_to_hash(phash_hex)
                        mtime = p.pop("_mtime", None)
                        if mtime is None:
                            try:
                                mtime = os.path.getmtime(fp)
                            except Exception:
                                mtime = 0
                        phash_cache[fp] = {"mtime": mtime, "phash": phash_hex}
                    else:
                        p["phash"] = None
                        p.pop("_mtime", None)
                    cache_miss += 1
                    done_since_save += 1
                    processed = cache_hit + cache_miss
                    if done_since_save >= SAVE_EVERY or processed == total:
                        save_phash_cache(phash_cache)
                        logger.info(
                            f"   {processed}/{total} | 缓存命中 {cache_hit}, 新算 {cache_miss}"
                        )
                        done_since_save = 0
            del chunk_items
        reader.join()
    else:
        # 全部命中缓存：仍需打印一行进度（与旧逻辑 idx==len 分支一致）
        processed = cache_hit
        save_phash_cache(phash_cache)
        logger.info(f"   {processed}/{total} | 缓存命中 {cache_hit}, 新算 {cache_miss}")
    logger.info(f"   phash 完成：缓存命中 {cache_hit} / 新算 {cache_miss}")

    # 第 2.5 步：重复照片去重（按日分组，组内 phash+文件名双条件去重）
    total_deduped = 0
    if DEDUP_ENABLED:
        logger.info(
            f"🔍 第 2.5 步：重复照片去重"
            f"（phash≤{DEDUP_PHASH_THRESHOLD} 且 文件名匹配，保留 size 最大者）..."
        )
        by_date_dedup = defaultdict(list)
        for p in all_photos:
            by_date_dedup[p["_dt"].strftime('%Y-%m-%d')].append(p)
        deduped_photos = []
        deduped_records = []
        for date_str in sorted(by_date_dedup.keys()):
            day = sorted(by_date_dedup[date_str], key=lambda x: x["_dt"])
            kept, dropped = dedup_within_day(day, DEDUP_PHASH_THRESHOLD)
            if dropped:
                total_deduped += len(dropped)
                deduped_records.extend(dropped)
                logger.info(
                    f"   {date_str}: {len(day)} 张去重 -> 保留 {len(kept)} 张，"
                    f"丢弃 {len(dropped)} 张副本"
                )
                for d in dropped[:3]:
                    logger.info(f"     [丢弃] {d['filename']} ({d['reason']})")
                if len(dropped) > 3:
                    logger.info(f"     ... 共 {len(dropped)} 张")
            deduped_photos.extend(kept)
        all_photos = deduped_photos
        logger.info(
            f"   去重完成：共丢弃 {total_deduped} 张重复副本，"
            f"剩余 {len(all_photos)} 张"
        )
    else:
        deduped_records = []

    # 第 3 步：按日切批
    logger.info("✂️  第 3/3 步：按日切批 + ABA 合并 + 大事件二次切 + 小批合并...")
    by_date = defaultdict(list)
    for p in all_photos:
        by_date[p["_dt"].strftime('%Y-%m-%d')].append(p)

    final_batches = []
    daily_stats = []
    distance_log = []
    for date_str in sorted(by_date.keys()):
        day_photos = sorted(by_date[date_str], key=lambda x: x["_dt"])
        raw_segments = cluster_by_phash_and_time(day_photos, distance_log)
        day_events = merge_aba_segments(raw_segments)
        day_batches = split_oversized_events(day_events)
        before_merge = len(day_batches)
        if SMALL_BATCH_MERGE:
            day_batches = merge_small_adjacent_batches(day_batches)
        daily_stats.append(
            f"   {date_str}: {len(day_photos)} 张 → 原始 {len(raw_segments)} 段 → "
            f"合并 {len(day_events)} 事件 → 切批 {before_merge} → 终批 {len(day_batches)}"
        )
        for i, batch in enumerate(day_batches):
            final_batches.append({
                "date": date_str,
                "batch_id": f"{date_str}_{i:03d}",
                "size": len(batch),
                "photos": [
                    {
                        "file": p["file"],
                        "filename": p["filename"],
                        "datetime": p["datetime"],
                        "is_exact_time": p["is_exact_time"],
                        "gps": p.get("gps"),       # 新增
                        "gps_text": None,          # 新增：占位，01b 脚本回填
                        "gps_strong": None,        # 新增：占位，01b 脚本回填
                    } for p in batch
                ],
            })

    for line in daily_stats:
        logger.info(line)

    if final_batches:
        sizes = [b["size"] for b in final_batches]
        size_dist = Counter(sizes)
        logger.info(
            f"📊 切批统计：共 {len(final_batches)} 批 | "
            f"批大小：min={min(sizes)} max={max(sizes)} avg={sum(sizes)/len(sizes):.1f}"
        )
        logger.info(f"   批大小分布：{dict(sorted(size_dist.items()))}")

    if PRINT_HISTOGRAM:
        print_distance_histogram(distance_log)

    gps_count = sum(1 for p in all_photos if p.get("gps"))
    logger.info(f"📍 GPS 覆盖：{gps_count}/{len(all_photos)} 张（{gps_count*100/max(1,len(all_photos)):.1f}%）")

    with open(BATCHES_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "params": {
                    "phash_threshold": PHASH_THRESHOLD, "merge_threshold": MERGE_THRESHOLD,
                    "max_batch_size": MAX_BATCH_SIZE, "burst_seconds": BURST_SECONDS,
                    "hard_cut_minutes": HARD_CUT_MINUTES, "aba_merge_window_min": ABA_MERGE_WINDOW_MIN,
                    "small_batch_merge": SMALL_BATCH_MERGE, "small_batch_size": SMALL_BATCH_SIZE,
                    "small_batch_gap_min": SMALL_BATCH_GAP_MIN,
                    "dedup_enabled": DEDUP_ENABLED, "dedup_phash_threshold": DEDUP_PHASH_THRESHOLD,
                },
                "summary": {
                    "total_scanned": total_files, "kept": len(all_photos),
                    "trashed": len(trashed), "deduped": total_deduped,
                    "batch_count": len(final_batches),
                },
                # 输入目录内容快照（02 断点续跑校验用）：相对路径+mtime+size 的 sha256。
                # 02 启动时读取此值写入 context_fingerprint，续跑时与当前快照对比，
                # 不匹配则自愈（清空进度从头跑），防止中断期间改了输入目录后错误续跑。
                "source_dir_snapshot": source_dir_snapshot,
                "batches": final_batches, "trashed": trashed,
                "deduped": deduped_records if DEDUP_ENABLED else [],
            },
            f, ensure_ascii=False, indent=2,
        )
    logger.info(f"✅ 完成！输出文件：{BATCHES_FILE}")


if __name__ == "__main__":
    main()
