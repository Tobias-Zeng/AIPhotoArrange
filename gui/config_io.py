"""pipeline_config.yaml 的读写封装。

核心要点：
- 用 ruamel.yaml 的 round-trip 模式读取/写回，完整保留原文件注释和结构。
- GUI 展示的是"激活 profile 合并后的有效值"，但写回时要写到正确位置：
    · source_dir / target_dir：有激活 profile 就写进 profiles.<profile>.common，
      否则写进顶层 common。这样和现有配置组织方式一致。
    · 其余可编辑项（home_city、amap_key、provider 等）一律写回各自的顶层段，
      不污染 profile 段。
- 切换 profile 时，source_dir/target_dir 显示该 profile 的有效值；其它项不受影响。

对外主要接口：
- load_for_ui(config_path)  → UIForm 数据结构
- save_from_ui(config_path, form)  → 写回 YAML
- list_profiles(config_path)
- get_city_candidates()  → 常驻城市候选列表（和高德返回城市口径一致）
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from ruamel.yaml import YAML


# 会被 profile 覆盖的字段（写回时走 profiles.<profile> 段）
_PROFILE_SCOPED = ("source_dir", "target_dir")


@dataclass
class UIForm:
    """GUI 表单数据。值为 None 表示配置里没有、用空串展示。"""
    profile: str = ""
    profiles: list = field(default_factory=list)
    source_dir: str = ""
    target_dir: str = ""
    home_city: str = ""
    amap_key: str = ""
    provider: str = "ollama"
    provider_model: str = ""
    provider_api_key: str = ""
    provider_base_url: str = ""
    num_workers: int = 6
    max_image_size: int = 768
    extraction_level: str = "A"     # 照片提取档位 A/B/C



def _new_yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _read_raw(config_path: str):
    if not config_path or not os.path.exists(config_path):
        return None, None
    y = _new_yaml()
    with open(config_path, "r", encoding="utf-8") as f:
        data = y.load(f)
    return y, data


def _apply_active_profile(cfg: dict) -> dict:
    """复刻 pipeline_config_loader._apply_active_profile 的合并逻辑（只读）。"""
    if not cfg:
        return cfg
    active = (os.environ.get("PIPELINE_PROFILE") or cfg.get("profile") or "").strip()
    cfg = dict(cfg)
    cfg["profile"] = active
    profiles = cfg.get("profiles") or {}
    overrides = profiles.get(active) if active else None
    if not overrides:
        return cfg
    mergeable = ("common", "chunker", "geo", "curator", "daily_summary", "yearly_summary")

    def deep_merge(base, override):
        result = dict(base or {})
        for k, v in (override or {}).items():
            if isinstance(v, dict) and isinstance(result.get(k), dict):
                result[k] = deep_merge(result[k], v)
            else:
                result[k] = v
        return result

    for section in mergeable:
        if section in overrides:
            cfg[section] = deep_merge(cfg.get(section, {}), overrides[section])
    return cfg


def list_profiles(config_path: str) -> list:
    """返回配置里定义的所有 profile 名。"""
    _, data = _read_raw(config_path)
    if not data:
        return []
    return list((data.get("profiles") or {}).keys())


def load_for_ui(config_path: str) -> UIForm:
    """读取配置，返回 UI 表单数据（值为激活 profile 合并后的有效值）。"""
    _, data = _read_raw(config_path)
    if not data:
        return UIForm()
    eff = _apply_active_profile(data)
    common = eff.get("common", {}) or {}
    geo = eff.get("geo", {}) or {}
    curator = eff.get("curator", {}) or {}
    provider = curator.get("provider", "ollama") or "ollama"
    pc = (curator.get("provider_configs") or {}).get(provider, {}) or {}

    return UIForm(
        profile=(eff.get("profile") or "").strip(),
        profiles=list((data.get("profiles") or {}).keys()),
        source_dir=common.get("source_dir", "") or "",
        target_dir=common.get("target_dir", "") or "",
        home_city=common.get("home_city", "") or "",
        amap_key=geo.get("amap_key", "") or "",
        provider=provider,
        provider_model=pc.get("model", "") or "",
        provider_api_key=pc.get("api_key", "") or "",
        provider_base_url=pc.get("base_url", "") or "",
        num_workers=int(curator.get("num_workers", 6)),
        max_image_size=int(curator.get("max_image_size", 768)),
        extraction_level=str(curator.get("extraction_level", "A")).strip().upper()[:1] or "A",
    )



def _ensure_path(data, dotted_path: str):
    """在 CommentedMap 里按 dotted_path 逐级创建/返回末级容器。"""
    cur = data
    for part in dotted_path.split(".")[:-1]:
        nxt = cur.get(part)
        if nxt is None or not isinstance(nxt, dict):
            from ruamel.yaml.comments import CommentedMap
            nxt = CommentedMap()
            cur[part] = nxt
        cur = nxt
    return cur


def _set_value(data, dotted_path: str, value):
    container = _ensure_path(data, dotted_path)
    key = dotted_path.split(".")[-1]
    container[key] = value


def set_active_profile(config_path: str, profile: str) -> None:
    """
    只更新顶层 profile 字段，不触碰其它任何配置项。

    专用于 GUI 切换 profile 的场景：避免把旧 profile 的 source_dir/target_dir
    误写到新 profile 段里。切换后由调用方重新 load_for_ui 读新 profile 的有效值。
    """
    y, data = _read_raw(config_path)
    if data is None:
        from ruamel.yaml.comments import CommentedMap
        data = CommentedMap()
    if y is None:
        y = _new_yaml()
    _set_value(data, "profile", profile)
    tmp = config_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        y.dump(data, f)
    os.replace(tmp, config_path)


def save_from_ui(config_path: str, form: UIForm) -> None:
    """
    把表单值写回 YAML（保留注释）。

    写回策略：
    - profile：写顶层 profile 字段。
    - source_dir / target_dir：有激活 profile → profiles.<profile>.common；
      无激活 profile → 顶层 common。
    - 其余项：写各自顶层段（common.home_city / geo.amap_key / curator.* /
      curator.provider_configs.<provider>.*）。
    """
    y, data = _read_raw(config_path)
    if data is None:
        # 文件不存在时新建一个空结构
        from ruamel.yaml.comments import CommentedMap
        data = CommentedMap()
    if y is None:
        y = _new_yaml()

    # 1) 顶层 profile
    _set_value(data, "profile", form.profile)

    # 2) profile-scoped 字段
    if form.profile and form.profile in (data.get("profiles") or {}):
        _set_value(data, f"profiles.{form.profile}.common.source_dir", form.source_dir)
        _set_value(data, f"profiles.{form.profile}.common.target_dir", form.target_dir)
    else:
        _set_value(data, "common.source_dir", form.source_dir)
        _set_value(data, "common.target_dir", form.target_dir)

    # 3) 全局字段
    _set_value(data, "common.home_city", form.home_city)
    _set_value(data, "geo.amap_key", form.amap_key)
    _set_value(data, "curator.provider", form.provider)
    _set_value(data, "curator.num_workers", int(form.num_workers))
    # max_image_size 不再由 GUI 编辑，保留配置文件中已有值（不覆盖）；
    # 仅当配置文件完全缺失该字段时补一个默认值，避免脚本读到 None。
    _curator = data.get("curator") or {}
    if _curator.get("max_image_size") is None:
        _set_value(data, "curator.max_image_size", 768)
    _level = str(form.extraction_level or "A").strip().upper()[:1] or "A"
    if _level not in ("A", "B", "C"):
        _level = "A"
    _set_value(data, "curator.extraction_level", _level)


    provider = form.provider or "ollama"
    if provider:
        _set_value(data, f"curator.provider_configs.{provider}.model", form.provider_model)
        _set_value(data, f"curator.provider_configs.{provider}.api_key", form.provider_api_key)
        _set_value(data, f"curator.provider_configs.{provider}.base_url", form.provider_base_url)

    tmp = config_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        y.dump(data, f)
    os.replace(tmp, config_path)


# 常驻城市候选列表：与高德 regeo 返回的 city 字段口径完全一致。
# 按省级行政区分组，供 GUI 二级下拉（省 → 市）使用。
# 数据来源：高德行政区划 API（district，subdistrict=2，level=city），
# 直辖市/特别行政区用 province 回退值（regeo city 字段为空时的处理逻辑）。
# 共 391 个城市。
_CITY_CANDIDATES_GROUPED = [
    ("直辖市", [
        "上海市", "北京市", "天津市", "重庆市",
    ]),
    ("特别行政区", [
        "澳门特别行政区", "香港特别行政区",
    ]),
    ("云南省", [
        "临沧市", "丽江市", "保山市", "大理白族自治州",
        "德宏傣族景颇族自治州", "怒江傈僳族自治州", "文山壮族苗族自治州", "昆明市",
        "昭通市", "普洱市", "曲靖市", "楚雄彝族自治州",
        "玉溪市", "红河哈尼族彝族自治州", "西双版纳傣族自治州", "迪庆藏族自治州",
    ]),
    ("内蒙古自治区", [
        "乌兰察布市", "乌海市", "兴安盟", "包头市",
        "呼伦贝尔市", "呼和浩特市", "巴彦淖尔市", "赤峰市",
        "通辽市", "鄂尔多斯市", "锡林郭勒盟", "阿拉善盟",
    ]),
    ("台湾省", [
        "云林县", "南投县", "台东县", "台中市",
        "台北市", "台南市", "嘉义县", "嘉义市",
        "基隆市", "宜兰县", "屏东县", "彰化县",
        "新北市", "新竹县", "新竹市", "桃园市",
        "澎湖县", "花莲县", "苗栗县", "高雄市",
    ]),
    ("吉林省", [
        "吉林市", "四平市", "延边朝鲜族自治州", "松原市",
        "白城市", "白山市", "辽源市", "通化市",
        "长春市",
    ]),
    ("四川省", [
        "乐山市", "内江市", "凉山彝族自治州", "南充市",
        "宜宾市", "巴中市", "广元市", "广安市",
        "德阳市", "成都市", "攀枝花市", "泸州市",
        "甘孜藏族自治州", "眉山市", "绵阳市", "自贡市",
        "资阳市", "达州市", "遂宁市", "阿坝藏族羌族自治州",
        "雅安市",
    ]),
    ("宁夏回族自治区", [
        "中卫市", "吴忠市", "固原市", "石嘴山市",
        "银川市",
    ]),
    ("安徽省", [
        "亳州市", "六安市", "合肥市", "安庆市",
        "宣城市", "宿州市", "池州市", "淮北市",
        "淮南市", "滁州市", "芜湖市", "蚌埠市",
        "铜陵市", "阜阳市", "马鞍山市", "黄山市",
    ]),
    ("山东省", [
        "东营市", "临沂市", "威海市", "德州市",
        "日照市", "枣庄市", "泰安市", "济南市",
        "济宁市", "淄博市", "滨州市", "潍坊市",
        "烟台市", "聊城市", "菏泽市", "青岛市",
    ]),
    ("山西省", [
        "临汾市", "吕梁市", "大同市", "太原市",
        "忻州市", "晋中市", "晋城市", "朔州市",
        "运城市", "长治市", "阳泉市",
    ]),
    ("广东省", [
        "东莞市", "中山市", "云浮市", "佛山市",
        "广州市", "惠州市", "揭阳市", "梅州市",
        "汕头市", "汕尾市", "江门市", "河源市",
        "深圳市", "清远市", "湛江市", "潮州市",
        "珠海市", "肇庆市", "茂名市", "阳江市",
        "韶关市",
    ]),
    ("广西壮族自治区", [
        "北海市", "南宁市", "崇左市", "来宾市",
        "柳州市", "桂林市", "梧州市", "河池市",
        "玉林市", "百色市", "贵港市", "贺州市",
        "钦州市", "防城港市",
    ]),
    ("新疆维吾尔自治区", [
        "乌鲁木齐市", "五家渠市", "伊犁哈萨克自治州", "克孜勒苏柯尔克孜自治州",
        "克拉玛依市", "北屯市", "博尔塔拉蒙古自治州", "双河市",
        "可克达拉市", "吐鲁番市", "和田地区", "哈密市",
        "喀什地区", "图木舒克市", "塔城地区", "巴音郭楞蒙古自治州",
        "新星市", "昆玉市", "昌吉回族自治州", "白杨市",
        "石河子市", "胡杨河市", "铁门关市", "阿克苏地区",
        "阿勒泰地区", "阿拉尔市",
    ]),
    ("江苏省", [
        "南京市", "南通市", "宿迁市", "常州市",
        "徐州市", "扬州市", "无锡市", "泰州市",
        "淮安市", "盐城市", "苏州市", "连云港市",
        "镇江市",
    ]),
    ("江西省", [
        "上饶市", "九江市", "南昌市", "吉安市",
        "宜春市", "抚州市", "新余市", "景德镇市",
        "萍乡市", "赣州市", "鹰潭市",
    ]),
    ("河北省", [
        "保定市", "唐山市", "廊坊市", "张家口市",
        "承德市", "沧州市", "石家庄市", "秦皇岛市",
        "衡水市", "邢台市", "邯郸市",
    ]),
    ("河南省", [
        "三门峡市", "信阳市", "南阳市", "周口市",
        "商丘市", "安阳市", "平顶山市", "开封市",
        "新乡市", "洛阳市", "济源市", "漯河市",
        "濮阳市", "焦作市", "许昌市", "郑州市",
        "驻马店市", "鹤壁市",
    ]),
    ("浙江省", [
        "丽水市", "台州市", "嘉兴市", "宁波市",
        "杭州市", "温州市", "湖州市", "绍兴市",
        "舟山市", "衢州市", "金华市",
    ]),
    ("海南省", [
        "万宁市", "三亚市", "三沙市", "东方市",
        "临高县", "乐东黎族自治县", "五指山市", "保亭黎族苗族自治县",
        "儋州市", "定安县", "屯昌县", "文昌市",
        "昌江黎族自治县", "海口市", "澄迈县", "琼中黎族苗族自治县",
        "琼海市", "白沙黎族自治县", "陵水黎族自治县",
    ]),
    ("湖北省", [
        "仙桃市", "十堰市", "咸宁市", "天门市",
        "孝感市", "宜昌市", "恩施土家族苗族自治州", "武汉市",
        "潜江市", "神农架林区", "荆州市", "荆门市",
        "襄阳市", "鄂州市", "随州市", "黄冈市",
        "黄石市",
    ]),
    ("湖南省", [
        "娄底市", "岳阳市", "常德市", "张家界市",
        "怀化市", "株洲市", "永州市", "湘潭市",
        "湘西土家族苗族自治州", "益阳市", "衡阳市", "邵阳市",
        "郴州市", "长沙市",
    ]),
    ("甘肃省", [
        "临夏回族自治州", "兰州市", "嘉峪关市", "天水市",
        "定西市", "平凉市", "庆阳市", "张掖市",
        "武威市", "甘南藏族自治州", "白银市", "酒泉市",
        "金昌市", "陇南市",
    ]),
    ("福建省", [
        "三明市", "南平市", "厦门市", "宁德市",
        "泉州市", "漳州市", "福州市", "莆田市",
        "龙岩市",
    ]),
    ("西藏自治区", [
        "山南市", "拉萨市", "日喀则市", "昌都市",
        "林芝市", "那曲市", "阿里地区",
    ]),
    ("贵州省", [
        "六盘水市", "安顺市", "毕节市", "贵阳市",
        "遵义市", "铜仁市", "黔东南苗族侗族自治州", "黔南布依族苗族自治州",
        "黔西南布依族苗族自治州",
    ]),
    ("辽宁省", [
        "丹东市", "大连市", "抚顺市", "朝阳市",
        "本溪市", "沈阳市", "盘锦市", "营口市",
        "葫芦岛市", "辽阳市", "铁岭市", "锦州市",
        "阜新市", "鞍山市",
    ]),
    ("陕西省", [
        "咸阳市", "商洛市", "安康市", "宝鸡市",
        "延安市", "榆林市", "汉中市", "渭南市",
        "西安市", "铜川市",
    ]),
    ("青海省", [
        "果洛藏族自治州", "海东市", "海北藏族自治州", "海南藏族自治州",
        "海西蒙古族藏族自治州", "玉树藏族自治州", "西宁市", "黄南藏族自治州",
    ]),
    ("黑龙江省", [
        "七台河市", "伊春市", "佳木斯市", "双鸭山市",
        "哈尔滨市", "大兴安岭地区", "大庆市", "牡丹江市",
        "绥化市", "鸡西市", "鹤岗市", "黑河市",
        "齐齐哈尔市",
    ]),
]

# 扁平化列表（向后兼容）
_CITY_CANDIDATES = [c for _, _cs in _CITY_CANDIDATES_GROUPED for c in _cs]


def get_city_candidates() -> list:
    """返回扁平化的城市候选列表（向后兼容）。"""
    return list(_CITY_CANDIDATES)


def get_city_candidates_grouped() -> list:
    """返回 [(province, [city, ...]), ...] 分组列表，供 GUI 二级下拉使用。"""
    return [(p, list(cs)) for p, cs in _CITY_CANDIDATES_GROUPED]


def find_province_for_city(city: str) -> str:
    """根据城市名反查所属省级分组名（加载已有配置时定位用）。"""
    for province, cities in _CITY_CANDIDATES_GROUPED:
        if city in cities:
            return province
    return ""
