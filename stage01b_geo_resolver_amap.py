# 01b_geo_resolver.py  v1.1 (高德版 + 评分体系定型)
"""
反向地理编码 v1.1：高德 regeo API + POI 多维评分

【数据源演进】
- v0.x: Nominatim + Overpass（OSM），全量 9240 张需 4+ 小时
- v1.0: 切高德 regeo，一次调用拿 admin 地址 + 周边 POI，11 分钟跑完
- v1.1: 在 v1.0 基础上重构 POI 评分体系，命中质量大幅提升

【架构要点】
- WGS-84 → GCJ-02 坐标系转换（亚毫米精度，无损），仅在调用 API 边界转换，
  alias bbox 和 cache 主键全程保持 WGS-84
- 多线程 + QPS 限速（MAX_WORKERS=3, QPS_LIMIT=2.5，匹配个人 key 上限）
- 网格去重（4 位小数 ≈ 11m），9240 张 → 2097 唯一坐标
- 缓存文件 amap_cache.json，dict O(1) 查询，原子写
- 限流码 10021 自动退避重试，致命码 (10001/10003/10044 等) 立即熔断

【alias 三态语义】
- alias.replace ：完全跳过高德，gps_text = gps_strong = alias.name（零 API 调用）
- alias.block   ：仍调高德拿行政地址，但屏蔽全部 POI，gps_strong = None
- 未命中        ：正常调高德，POI 经评分通过门槛 → gps_strong 填值，否则 None

【POI 评分体系（v1.1 重构核心）】
最终得分 = 距离分 + 大类分 + 关键字加成

距离分（递减曲线）：
    ≤30m  → 36    ≤60m  → 28    ≤100m → 18
    ≤200m → 8     >200m → 0

大类分（POI_CATEGORY_BY_NAME，基于 type 字段第一段）：
    +50  风景名胜
    +10  体育休闲服务
      0  餐饮服务
    -15  住宿服务（民宿掺水严重；真酒店开会场景走 alias）
    -20  购物服务
    -30  科教文化 / 政府机构 / 公司企业 / 公共设施 / 金融保险 / 地名地址
    -50  交通设施 / 医疗保健 / 汽车服务 / 通行设施
    -100 商务住宅
    -10  默认（未配置大类或空 type，防止"XX号建筑"门牌靠纯距离分蒙混）

关键字白名单加成（POI_STRONG_KEYWORDS）：
    +50  5A/4A/博物馆/纪念馆/美术馆/古镇/古城/古村
    （救回被科教文化大类误伤的真地标）

关键字黑名单（POI_NAME_BLACKLIST）：
    命中直接返回 -999，确保过不了任何门槛
    当前留空，按需扩充处理具体噪声 POI

门槛双轨制（POI_MIN_SCORE / _HOME）：
    外地       ：35（标准门槛，30m 风景名胜/餐饮/学校等都能过）
    家城市     ：65（更严，必须是 风景名胜 + 120m 内，压住小区附近误命中）
    HOME_CITY = "重庆市"

【关键调优历史与决策】
v1.1 调优前：所有 POI 默认 +0 分，靠 30m 内 36 分蒙过 35 门槛
    问题：写字楼/小区/培训机构/民宿大量误命中
v1.1 调优过程（9240 张样本）：
    1. 修复 typecode/type 字段名 bug，POI 大类映射首次真正生效
    2. 引入大类正负分体系，砍掉商住/教培/培训/医疗等噪声大类
    3. 引入家城市抬高门槛，处理"自家周围正好有景点"的 POI 假阳性
    4. 引入 POI 名称黑名单工具（暂空），保留对个别噪声 POI 的精确打击能力
v1.1 收敛结果：
    总命中：4768 → 3653（-23%）
    自动 POI 命中里风景名胜大类占 99.8%
    Top 30 高频 strong 全部为真景点或地标

【典型 case 处理】
- 商住楼/小区/民宿/写字楼      → 大类负分压死
- 自家周围 60 分擦边景点      → HOME 门槛 65 压死
- 真景区被误分到科教文化       → 关键字白名单救回（如博物馆）
- 出差大酒店开会，POI 错位     → alias.replace 直接锚定
- 邻居小区景点污染            → alias.block 屏蔽 POI 区域
"""


import os, sys, json, time, math, logging, threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import requests

# ==========================================
# 1. 配置
# ==========================================
# 需要每次调整的参数集中在 pipeline_config.yaml，本脚本从那里读取。
# 找不到配置文件时，退回到下面的内置默认值（向后兼容）。
import pipeline_config_loader as _cfg_loader

_CONFIG = _cfg_loader.load_config()
_COMMON = _CONFIG.get("common", {})
_GEO = _CONFIG.get("geo", {})
_FILES = _cfg_loader.resolve_filenames(_CONFIG)

# 中间文件用 BASE_DIR 绝对路径，Nuitka 编译后也指向 .exe 同级目录
BATCHES_FILE = os.path.join(_cfg_loader.BASE_DIR, _FILES["batches_file"])
AMAP_CACHE_FILE = os.path.join(_cfg_loader.BASE_DIR, _FILES["amap_cache_file"])
ALIAS_FILE = os.path.join(_cfg_loader.BASE_DIR, _FILES["geo_alias_file"])
REVIEW_FILE = os.path.join(_cfg_loader.BASE_DIR, _FILES["geo_review_file"])

AMAP_KEY = _GEO.get("amap_key", "xxxxxx")
AMAP_REGEO_URL = "https://restapi.amap.com/v3/geocode/regeo"

MAX_WORKERS = _GEO.get("max_workers", 3)     # 个人 key 默认 QPS 上限就是 3
QPS_LIMIT = _GEO.get("qps_limit", 2.5)       # 留一点点余量

TIMEOUT = 15
MAX_RETRY = 3
RETRY_BACKOFF = 2
SAVE_EVERY = 50

COORD_PRECISION = 4      # 4 位小数 ≈ 11m，网格去重精度

POI_RADIUS = 500         # regeo 搜索半径
POI_MAX_DIST = 300       # POI 入选最远距离
POI_MIN_SCORE = _GEO.get("poi_min_score", 35)       # POI 入选门槛（外地）
POI_MIN_SCORE_HOME = _GEO.get("poi_min_score_home", 65)  # 家城市门槛（更严，压住小区附近误命中）

HOME_CITY = _COMMON.get("home_city", "重庆市")     # 家城市，命中此 city 时启用 HOME 门槛


MUNICIPALITIES = {"重庆市", "北京市", "上海市", "天津市"}
# 特别行政区：高德 regeo 的 city 字段为空，需 province 回退才能匹配
SAR = {"香港特别行政区", "澳门特别行政区"}

USER_AGENT = "AIPhotoArrange/0.3.0"

# 高德致命错误码：key/配额/权限问题，命中立即停
FATAL_INFOCODES = {
    "10001",  # INVALID_USER_KEY
    "10002",  # SERVICE_NOT_AVAILABLE
    "10003",  # DAILY_QUERY_OVER_LIMIT
    "10008",  # USERKEY_PLAT_NOMATCH
    "10009",  # USER_KEY_RECYCLED
    "10014",  # INSUFFICIENT_PRIVILEGES（实际是日配额）
    "10019",  # USER_KEY_RECYCLED
    "10044",  # USER_DAILY_QUERY_OVER_LIMIT
    "10045",  # SERVICE_NOT_AVAILABLE_FOR_THIS_USERTYPE
}
# 10021 是瞬时 QPS 超限，会自动退避重试，不熔断

# POI 大类评分表（基于 type 字段第一段）
POI_CATEGORY_BY_NAME = {
    "风景名胜":            50,
    "体育休闲服务":        10,
    "餐饮服务":             0,
    "住宿服务":           -15,
    "购物服务":           -20,
    "金融保险服务":       -30,
    "科教文化服务":       -30,
    "政府机构及社会团体":  -30,
    "公司企业":           -30,
    "公共设施":           -30,
    "地名地址信息":       -30,
    "交通设施服务":       -50,
    "医疗保健服务":       -50,
    "汽车服务":           -50,
    "通行设施":           -50,
    "商务住宅":          -100,
}

# 白名单关键字（救回被大类误伤的真地标）
POI_STRONG_KEYWORDS = (
    "5A", "4A",
    "博物馆", "博物院", "纪念馆", "美术馆",
    "古镇", "古城", "古村",
)
KEYWORD_BONUS = 50

# POI 名称黑名单（精确包含子串，命中直接剔除，不参与评分）
POI_NAME_BLACKLIST = (
    # 暂留空，未来发现噪声 POI 时往这里加
)

os.makedirs(os.path.join(_cfg_loader.BASE_DIR, "logs"), exist_ok=True)
log_filename = os.path.join(
    _cfg_loader.BASE_DIR, "logs",
    f"01b_geo_resolver_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
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
# 2. WGS-84 → GCJ-02
# ==========================================
def _out_of_china(lat, lng):
    return not (73.66 < lng < 135.05 and 3.86 < lat < 53.55)

def _trans_lat(x, y):
    r = -100.0 + 2*x + 3*y + 0.2*y*y + 0.1*x*y + 0.2*math.sqrt(abs(x))
    r += (20*math.sin(6*x*math.pi) + 20*math.sin(2*x*math.pi)) * 2/3
    r += (20*math.sin(y*math.pi) + 40*math.sin(y/3*math.pi)) * 2/3
    r += (160*math.sin(y/12*math.pi) + 320*math.sin(y*math.pi/30)) * 2/3
    return r

def _trans_lng(x, y):
    r = 300.0 + x + 2*y + 0.1*x*x + 0.1*x*y + 0.1*math.sqrt(abs(x))
    r += (20*math.sin(6*x*math.pi) + 20*math.sin(2*x*math.pi)) * 2/3
    r += (20*math.sin(x*math.pi) + 40*math.sin(x/3*math.pi)) * 2/3
    r += (150*math.sin(x/12*math.pi) + 300*math.sin(x/30*math.pi)) * 2/3
    return r

def wgs84_to_gcj02(lat, lng):
    """中国境外原值返回；境内做火星偏移"""
    if _out_of_china(lat, lng):
        return lat, lng
    a = 6378245.0
    ee = 0.00669342162296594323
    dlat = _trans_lat(lng - 105.0, lat - 35.0)
    dlng = _trans_lng(lng - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * math.pi
    magic = math.sin(rad_lat)
    magic = 1 - ee * magic * magic
    sqrt_magic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrt_magic) * math.pi)
    dlng = (dlng * 180.0) / (a / sqrt_magic * math.cos(rad_lat) * math.pi)
    return lat + dlat, lng + dlng


# ==========================================
# 3. 工具
# ==========================================
def coord_key(lat, lng):
    """网格去重 key（WGS-84 空间，4 位小数 ≈ 11m）"""
    return f"{round(float(lat), COORD_PRECISION):.{COORD_PRECISION}f},{round(float(lng), COORD_PRECISION):.{COORD_PRECISION}f}"


def haversine(lat1, lng1, lat2, lng2):
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))


def has_chinese(s):
    return any('\u4e00' <= ch <= '\u9fff' for ch in s or "")


def atomic_save(path, data):
    """原子写：先写 .tmp 再 rename，避免中途断电导致 cache 损坏"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_cache():
    if not os.path.exists(AMAP_CACHE_FILE):
        return {}
    try:
        with open(AMAP_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"⚠️  缓存损坏，重建：{e}")
        return {}


def load_alias():
    if not os.path.exists(ALIAS_FILE):
        logger.warning(f"⚠️  {ALIAS_FILE} 不存在，跳过 alias")
        return []
    with open(ALIAS_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)
    # 兼容两种格式：直接 list 或 {"areas": [...]} 包装
    if isinstance(raw, dict):
        return raw.get("areas", [])
    if isinstance(raw, list):
        return raw
    logger.warning(f"⚠️  {ALIAS_FILE} 格式不识别，返回空")
    return []


def match_alias(lat, lng, aliases):
    """返回 (mode, name) 或 None。
       mode='replace' → 完整替换名字；mode='block' → 仅屏蔽 POI"""
    for a in aliases:
        bbox = a.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        lat_min, lng_min, lat_max, lng_max = bbox
        if lat_min <= lat <= lat_max and lng_min <= lng <= lng_max:
            if a.get("block"):
                return ("block", None)
            if a.get("name"):
                return ("replace", a["name"])
    return None


# ==========================================
# 4. QPS 限速器（线程安全）
# ==========================================
class RateLimiter:
    def __init__(self, qps):
        self.interval = 1.0 / qps
        self.lock = threading.Lock()
        self.next_t = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            if now < self.next_t:
                time.sleep(self.next_t - now)
                now = time.monotonic()
            self.next_t = now + self.interval


# 全局停止标志（致命错误时拉起）
_stop_event = threading.Event()
_stop_reason = [None]


# ==========================================
# 5. 高德 regeo 调用
# ==========================================
def call_amap_regeo(lat_wgs, lng_wgs, limiter):
    """
    返回 dict：{
        "ok": bool,
        "fatal": bool,           # 是否致命错误（key/配额）
        "admin": {province, city, district, township},
        "formatted": str,        # 高德拼好的完整地址
        "pois": [{name, typecode, dist}],
        "raw_infocode": str,
    }
    """
    lat_gcj, lng_gcj = wgs84_to_gcj02(lat_wgs, lng_wgs)
    params = {
        "key": AMAP_KEY,
        "location": f"{lng_gcj:.6f},{lat_gcj:.6f}",   # 高德要求 lng,lat
        "extensions": "all",
        "radius": POI_RADIUS,
        "roadlevel": 0,
        "output": "JSON",
    }

    last_err = None
    for attempt in range(1, MAX_RETRY + 1):
        if _stop_event.is_set():
            return {"ok": False, "fatal": True, "err": "stopped"}
        limiter.wait()
        try:
            r = requests.get(AMAP_REGEO_URL, params=params,
                             timeout=TIMEOUT,
                             headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            last_err = str(e)
            if attempt < MAX_RETRY:
                time.sleep(RETRY_BACKOFF * attempt)
            continue

        infocode = data.get("infocode", "")
        if data.get("status") != "1":
            if infocode in FATAL_INFOCODES:
                return {"ok": False, "fatal": True,
                        "err": f"高德致命错误 infocode={infocode} info={data.get('info')}",
                        "raw_infocode": infocode}
            last_err = f"infocode={infocode} info={data.get('info')}"
            # 10021 = QPS 瞬时超限，退避更久；其他错误用普通退避
            if infocode == "10021":
                time.sleep(2.0 + RETRY_BACKOFF * attempt)
            elif attempt < MAX_RETRY:
                time.sleep(RETRY_BACKOFF * attempt)
            continue

        regeo = data.get("regeocode") or {}
        addr_comp = regeo.get("addressComponent") or {}

        # 直辖市处理：高德 province 给"重庆市"，city 可能为空
        province = (addr_comp.get("province") or "").strip()
        city_raw = addr_comp.get("city")
        city = (city_raw if isinstance(city_raw, str) else "").strip()
        if not city and (province in MUNICIPALITIES or province in SAR):
            city = province
        district = (addr_comp.get("district") or "").strip() if isinstance(addr_comp.get("district"), str) else ""
        township = (addr_comp.get("township") or "").strip() if isinstance(addr_comp.get("township"), str) else ""

        admin = {
            "province": province,
            "city": city,
            "district": district,
            "township": township,
        }
        formatted = (regeo.get("formatted_address") or "").strip()
        if isinstance(formatted, list):
            formatted = ""

        pois_raw = regeo.get("pois") or []
        pois = []
        for p in pois_raw:
            name = (p.get("name") or "").strip()
            if not name or not has_chinese(name):
                continue
            # 高德 regeo 实际返回的是 "type" 字段（字符串"大类;中类;小类"）
            # 个别响应也会带 "typecode"（6位数字），优先用 typecode，没有就从 type 推
            typecode = (p.get("typecode") or "").strip() if isinstance(p.get("typecode"), str) else ""
            type_str = (p.get("type") or "").strip() if isinstance(p.get("type"), str) else ""
            try:
                dist = float(p.get("distance") or 9999)
            except (TypeError, ValueError):
                dist = 9999
            pois.append({
                "name": name,
                "typecode": typecode,
                "type": type_str,        # 新增：保留原始 type 字符串
                "dist": dist,
            })

        return {
            "ok": True,
            "fatal": False,
            "admin": admin,
            "formatted": formatted,
            "pois": pois,
            "raw_infocode": infocode,
        }

    return {"ok": False, "fatal": False, "err": last_err or "unknown"}


# ==========================================
# 6. POI 评分
# ==========================================
def score_poi(poi: dict) -> int:
    name = poi.get("name", "")

    # 黑名单：直接返回极低分，确保过不了任何门槛
    if any(kw in name for kw in POI_NAME_BLACKLIST):
        return -999
    
    type_str = poi.get("type", "") or ""
    dist = poi.get("dist", 9999)

    if dist <= 30:
        dist_score = 36
    elif dist <= 60:
        dist_score = 28
    elif dist <= 100:
        dist_score = 18
    elif dist <= 200:
        dist_score = 8
    else:
        dist_score = 0

    main_cat = type_str.split(";", 1)[0] if type_str else ""
    if main_cat and main_cat in POI_CATEGORY_BY_NAME:
        cat_score = POI_CATEGORY_BY_NAME[main_cat]
    else:
        # 空 type 或未配置大类：默认惩罚 -10
        # 防止"XX号建筑"门牌、未知大类靠纯距离分蒙混过关
        cat_score = -10

    kw_bonus = KEYWORD_BONUS if any(kw in name for kw in POI_STRONG_KEYWORDS) else 0

    return dist_score + cat_score + kw_bonus


def pick_best_poi(pois, admin=None):
    if not pois:
        return None
    threshold = POI_MIN_SCORE
    if admin and admin.get("city") == HOME_CITY:
        threshold = POI_MIN_SCORE_HOME
    scored = [(score_poi(p), p) for p in pois]
    scored = [(s, p) for s, p in scored if s >= threshold]
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    return scored[0][1]


# ==========================================
# 7. 文本组装
# ==========================================
def build_admin_text(admin):
    """拼接行政地址前缀。
    - HOME 城市内：city + district + township（区/县全保留）
    - 外地：city + (仅当 district 以"县/旗"结尾时保留) + township
      理由：外地的"XX区"对用户识别度低（不熟陌生城市的区划），
      "县/旗"通常本身就是有辨识度的地名，township（街道/镇/乡）一律保留
    """
    parts = []
    city = admin.get("city")
    district = admin.get("district")
    township = admin.get("township")
    is_home = (city == HOME_CITY)

    if city:
        parts.append(city)
    if district and district != city:
        if is_home or district.endswith(("县", "旗")):
            parts.append(district)
    if township:
        parts.append(township)
    return " ".join(parts)


def resolve_one(lat, lng, alias_hit, amap_result):
    """
    根据 alias 命中情况 + 高德结果，产出 (gps_text, gps_strong)
    - alias.replace 在外层已直接返回，这里不会进
    - alias.block：admin 拼接，POI 全屏蔽 → gps_strong=None
    - 未命中：admin + 最佳 POI（如有）→ gps_strong=POI 名
    """
    if not amap_result or not amap_result.get("ok"):
        return None, None

    admin_text = build_admin_text(amap_result["admin"])

    if alias_hit and alias_hit[0] == "block":
        return (admin_text or None), None

    best = pick_best_poi(amap_result["pois"], amap_result["admin"])
    if best:
        gps_text = f"{admin_text} {best['name']}".strip() if admin_text else best["name"]
        return gps_text, best["name"]

    return (admin_text or None), None


# ==========================================
# 8. 主流程
# ==========================================
def main():
    logger.info("========== 启动阶段 1B v1.0：高德 regeo + 多线程 ==========")

    if not os.path.exists(BATCHES_FILE):
        logger.error(f"❌ {BATCHES_FILE} 不存在")
        return

    with open(BATCHES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    batches = data.get("batches", [])

    aliases = load_alias()
    cache = load_cache()
    cache_lock = threading.Lock()
    save_lock = threading.Lock()
    limiter = RateLimiter(QPS_LIMIT)

    # 1) 收集所有带 GPS 的照片，按网格 key 去重
    coord_to_photos = defaultdict(list)   # key → [(batch_idx, photo_idx, lat, lng)]
    total_photos = 0
    gps_photos = 0
    for bi, batch in enumerate(batches):
        for pi, photo in enumerate(batch.get("photos", [])):
            total_photos += 1
            gps = photo.get("gps")
            if not gps or not isinstance(gps, (list, tuple)) or len(gps) != 2:
                continue
            try:
                lat = float(gps[0])
                lng = float(gps[1])
            except (TypeError, ValueError):
                continue
            gps_photos += 1
            key = coord_key(lat, lng)
            coord_to_photos[key].append((bi, pi, lat, lng))

    logger.info(f"📍 {gps_photos}/{total_photos} 张带 GPS，网格去重 {len(coord_to_photos)} 个唯一坐标")
    logger.info(f"📋 加载 {len(aliases)} 条 alias 规则")

    if not coord_to_photos:
        logger.info("没有带 GPS 的照片，直接结束")
        return

    # 2) 分类待处理坐标
    skip_alias_replace = []
    need_amap = []
    cache_hit_count = 0

    for key, items in coord_to_photos.items():
        lat, lng = items[0][2], items[0][3]
        if key in cache:
            cache_hit_count += 1
            continue
        hit = match_alias(lat, lng, aliases)
        if hit and hit[0] == "replace":
            skip_alias_replace.append((key, lat, lng, hit))
        else:
            need_amap.append((key, lat, lng, hit))

    logger.info(
        f"🗂️  缓存命中 {cache_hit_count} | "
        f"alias.replace 跳过 {len(skip_alias_replace)} | "
        f"待查高德 {len(need_amap)}"
    )

    # 3) alias.replace 直接写入 cache
    for key, lat, lng, hit in skip_alias_replace:
        cache[key] = {
            "source": "alias_replace",
            "alias_name": hit[1],
            "admin": None,
            "formatted": None,
            "pois": [],
            "ts": int(time.time()),
        }
    if skip_alias_replace:
        atomic_save(AMAP_CACHE_FILE, cache)
        logger.info(f"💾 alias.replace 写入 {len(skip_alias_replace)} 条缓存")

    # 4) 多线程查高德
    if need_amap:
        done = 0
        new_since_save = 0
        t0 = time.time()
        progress_lock = threading.Lock()

        def worker(item):
            key, lat, lng, hit = item
            if _stop_event.is_set():
                return key, None
            res = call_amap_regeo(lat, lng, limiter)
            if res.get("fatal"):
                _stop_event.set()
                _stop_reason[0] = res.get("err", "fatal")
                return key, None
            if not res.get("ok"):
                logger.warning(f"  [{key}] 失败：{res.get('err')}")
                return key, None
            return key, {
                "source": "amap",
                "alias_block": bool(hit and hit[0] == "block"),
                "admin": res["admin"],
                "formatted": res["formatted"],
                "pois": res["pois"],
                "ts": int(time.time()),
            }

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(worker, item): item for item in need_amap}
            for fut in as_completed(futures):
                key, payload = fut.result()
                with progress_lock:
                    done += 1
                    cur = done
                if payload is not None:
                    with cache_lock:
                        cache[key] = payload
                        new_since_save += 1
                if cur % 50 == 0 or cur == len(need_amap):
                    elapsed = time.time() - t0
                    rate = cur / elapsed if elapsed > 0 else 0
                    eta = (len(need_amap) - cur) / rate if rate > 0 else 0
                    logger.info(f"  [{cur}/{len(need_amap)}] {rate:.1f} req/s, ETA {eta/60:.1f}min")
                if new_since_save >= SAVE_EVERY:
                    with save_lock:
                        with cache_lock:
                            atomic_save(AMAP_CACHE_FILE, cache)
                            new_since_save = 0
                if _stop_event.is_set():
                    break

        with cache_lock:
            atomic_save(AMAP_CACHE_FILE, cache)

        elapsed = time.time() - t0
        logger.info(
            f"⏱️  高德查询完成：{done}/{len(need_amap)} 用时 {elapsed/60:.1f}min "
            f"(平均 {elapsed/max(done,1):.2f}s/个)"
        )

        if _stop_event.is_set():
            logger.error(f"💀 致命错误熔断：{_stop_reason[0]}")
            logger.error("   已查 cache 已保存，修复后可继续断点续跑")
            return

    # 5) Pass 3：把 cache 解析成 (gps_text, gps_strong) 回写 batches
    logger.info("🧮 计算 gps_text / gps_strong 并回写 batches...")
    written = 0
    strong_count = 0
    no_result = 0
    review_records = {}

    for key, items in coord_to_photos.items():
        lat, lng = items[0][2], items[0][3]
        entry = cache.get(key)

        if entry is None:
            no_result += len(items)
            for bi, pi, _, _ in items:
                batches[bi]["photos"][pi]["gps_text"] = None
                batches[bi]["photos"][pi]["gps_strong"] = None
            continue

        if entry.get("source") == "alias_replace":
            gps_text = entry["alias_name"]
            gps_strong = entry["alias_name"]
            alias_tag = "replace"
        else:
            alias_block = entry.get("alias_block", False)
            alias_hit_proxy = ("block", None) if alias_block else None
            fake_amap = {"ok": True, "admin": entry["admin"], "pois": entry.get("pois", [])}
            gps_text, gps_strong = resolve_one(lat, lng, alias_hit_proxy, fake_amap)
            alias_tag = "block" if alias_block else None

        if gps_strong:
            strong_count += len(items)

        # 重算一次最佳 POI（带 score）用于审计
        chosen = None
        if entry.get("source") != "alias_replace" and not entry.get("alias_block"):
            pois_for_pick = entry.get("pois") or []
            if pois_for_pick:
                scored = [(score_poi(p), p) for p in pois_for_pick]
                scored.sort(key=lambda x: -x[0])
                if scored and scored[0][0] >= POI_MIN_SCORE:
                    s, p = scored[0]
                    chosen = {
                        "name": p["name"], "type": p.get("type"),
                        "dist": p.get("dist"), "score": s
                    }

        # top_pois 改成按评分排序前 5（更有意义）
        all_pois = entry.get("pois") or []
        ranked = sorted(
            [{"name": p["name"], "type": p.get("type"), "dist": p.get("dist"),
            "score": score_poi(p)} for p in all_pois],
            key=lambda x: -x["score"]
        )

        review_records[key] = {
            "coord": key,
            "sample_lat": lat,
            "sample_lng": lng,
            "photo_count": len(items),
            "gps_text": gps_text,
            "gps_strong": gps_strong,
            "alias": alias_tag,
            "admin": entry.get("admin"),
            "chosen_poi": chosen,                    # 选中那个 + 分数
            "top_pois_by_score": ranked[:5],          # 按分排序前5（含分）
            "top_pois_by_dist": [                     # 按距离前5（保留对比）
                {"name": p["name"], "type": p.get("type"), "dist": p.get("dist")}
                for p in all_pois[:5]
            ],
        }

        for bi, pi, _, _ in items:
            batches[bi]["photos"][pi]["gps_text"] = gps_text
            batches[bi]["photos"][pi]["gps_strong"] = gps_strong
            written += 1

    # 6) 写回 batches + review（原子写）
    tmp = BATCHES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, BATCHES_FILE)

    review_list = sorted(review_records.values(), key=lambda x: -x["photo_count"])
    with open(REVIEW_FILE, "w", encoding="utf-8") as f:
        json.dump(review_list, f, ensure_ascii=False, indent=2)

    logger.info("========== 完成 ==========")
    logger.info(f"📝 回写照片 {written} 张（其中 gps_strong 命中 {strong_count} 张）")
    logger.info(f"❓ 无结果（已查但未拿到地址）：{no_result} 张")
    logger.info(f"📄 review 文件：{REVIEW_FILE}（按 photo_count 倒序，可作为 alias 扩充依据）")


if __name__ == "__main__":
    main()
