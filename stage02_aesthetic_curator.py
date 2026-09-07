# 02_aesthetic_curator.py
import os
import sys
import re
import json
import time
import base64
import shutil
import hashlib

import logging
import threading
import random
import copy
from datetime import datetime
from io import BytesIO
from collections import defaultdict, deque

from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image, ImageOps
Image.MAX_IMAGE_PIXELS = 200_000_000
from openai import OpenAI, APITimeoutError

# ==========================================
# 1. 通用配置
# ==========================================

# 配置注意事项：
# 所有需要每次调整的参数都集中在 pipeline_config.yaml，本脚本从那里读取。
# 找不到配置文件时，退回到下面的内置默认值（向后兼容）。
# 中间文件名（批次/进度/未处理清单）由 profile 自动派生，无需手动改名。
import pipeline_config_loader as _cfg_loader

_CONFIG = _cfg_loader.load_config()
_COMMON = _CONFIG.get("common", {})
_CURATOR = _CONFIG.get("curator", {})
_FILES = _cfg_loader.resolve_filenames(_CONFIG)

SOURCE_DIR = os.path.normpath(_COMMON.get("source_dir", r"D:\JM照片_整理输入"))
TARGET_DIR = os.path.normpath(_COMMON.get("target_dir", r"D:\JM照片_整理输出_生产v0.3.0_2024"))
# 上下文指纹用：profile 与 home_city 改变后，旧 02_progress 不能直接续跑
PROFILE = _cfg_loader.get_profile(_CONFIG)
HOME_CITY = _COMMON.get("home_city", "")

# ==========================================
# 照片提取档位（extraction_level）
# ------------------------------------------
# A = 精华档（生产基线 v0.3.0，提取率约 30%）
# B = 纪念档（宽松保留，提取率约 50%-80%）
# C = 归类档（不做审美筛选，全部照片按事件归档，代码强制全保留）
# ⚠️ 切换档位后必须 `python run_pipeline.py --fresh` 重跑，否则断点续跑会跳过已完成批次。
# ==========================================
EXTRACTION_LEVEL = str(_CURATOR.get("extraction_level", "A")).strip().upper()[:1] or "A"
if EXTRACTION_LEVEL not in ("A", "B", "C"):
    EXTRACTION_LEVEL = "A"

# 档位说明（仅用于日志展示）
_LEVEL_DESC = {
    "A": "精华档（约30%，严选精华）",
    "B": "纪念档（约50%-80%，宽松保留有记录意义的照片）",
    "C": "归类档（全部归档，只做事件命名不删片）",
}

# C 档：LLM 可能仍返回 is_highlight=false，这里用代码强制全保留，不依赖模型听话
FORCE_KEEP_ALL = (EXTRACTION_LEVEL == "C")

# 默认各档位对应的提示词（配置缺失时的内置兜底）
# 提示词已加密为 .enc 二进制文件，运行时在内存中解密（见 _load_prompt_text）
_DEFAULT_PROMPT_FILES = {
    "A": "prompts/prompt_template_v0.3.0.enc",
    "B": "prompts/prompt_template_v0.3.1_memory.enc",
    "C": "prompts/prompt_template_v0.3.1_classify_all.enc",
}

# 固定解密密钥（与 encrypt_prompt.py 中的 _STATIC_PROMPT_KEY 保持一致）
# Nuitka 编译后该 Key 被嵌入二进制，无法被轻易逆向提取。
_STATIC_PROMPT_KEY = b"hfuLfxfmqclh5cbuVmVZVGbGmb-blZtvf42_YKV3_CU="


def _resolve_prompt_file():
    """
    决定使用哪个提示词文件，优先级：
      1) 显式配置 curator.prompt_file（调试/向后兼容）
      2) curator.prompt_files[<档位>]
      3) 内置默认映射 _DEFAULT_PROMPT_FILES[<档位>]

    注意：返回的路径指向加密后的 .enc 文件，明文 .txt 不再参与运行时。
          返回绝对路径（基于 BASE_DIR），Nuitka 编译后也指向 .exe 同级 prompts/。
    """
    explicit = _CURATOR.get("prompt_file")
    if explicit:
        # 兼容用户在配置里仍写 .txt 的情况：自动替换为同名 .enc
        if explicit.endswith(".txt"):
            explicit = explicit[:-4] + ".enc"
        rel = explicit
    else:
        by_level = _CURATOR.get("prompt_files") or {}
        resolved = by_level.get(EXTRACTION_LEVEL)
        if resolved:
            if resolved.endswith(".txt"):
                resolved = resolved[:-4] + ".enc"
            rel = resolved
        else:
            rel = _DEFAULT_PROMPT_FILES[EXTRACTION_LEVEL]

    # 相对路径 -> 基于 BASE_DIR 的绝对路径（兼容已是绝对路径的情况）
    if os.path.isabs(rel):
        return os.path.normpath(rel)
    return os.path.normpath(os.path.join(_cfg_loader.BASE_DIR, rel))


PROMPT_FILE = _resolve_prompt_file()  # 👈 照片提取提示词（按档位自动选择，指向 .enc 密文）


def _load_prompt_text(enc_path):
    """
    读取加密的 .enc 提示词文件，在内存中实时解密并返回明文字符串。
    - enc_path: 加密提示词文件路径（prompts/*.enc）
    - 返回: 解密后的明文提示词（str）
    - 文件不存在或解密失败时抛出异常，由调用方处理
    """
    from cryptography.fernet import Fernet
    fernet = Fernet(_STATIC_PROMPT_KEY)
    with open(enc_path, "rb") as f:
        ciphertext = f.read()
    plaintext = fernet.decrypt(ciphertext)
    return plaintext.decode("utf-8")


BATCHES_FILE = os.path.join(_cfg_loader.BASE_DIR, _FILES["batches_file"])    # 👈 批次输入文件（由01脚本输出)

PROGRESS_FILE = os.path.join(_cfg_loader.BASE_DIR, _FILES["progress_file"])  # 👈 断点续跑文件，重新跑需要先删除
UNPROCESSED_LOG = os.path.join(_cfg_loader.BASE_DIR, _FILES["unprocessed_log"])  # 👈 未处理照片清单累加文件，重新跑需要先删除
FAILED_DIR_NAME = "_FAILED_FOR_MANUAL_REVIEW"              # 👈 相对名

NUM_WORKERS = _CURATOR.get("num_workers", 6)
MAX_IMAGE_SIZE = _CURATOR.get("max_image_size", 768)
# 隐藏调试入口：记录每个 provider 首次 VLM 请求参数（脱敏图片后）到
# logs/debug_first_request_<provider>_<时间戳>.json，用于排查不同 provider
# 请求参数差异。⚠️ 该日志会包含系统提示词明文，可能泄露 prompt，故不放入
# pipeline_config.yaml 配置项，仅通过设置环境变量 AIPA_DEBUG_FIRST_REQUEST=1
# 临时开启（默认关闭）。
DEBUG_LOG_FIRST_REQUEST = (
    _CURATOR.get("debug_log_first_request", False)
    or os.environ.get("AIPA_DEBUG_FIRST_REQUEST", "").strip() in ("1", "true", "True")
)

# 并发调度模式：
#   by_date  (默认)：按天分线程，同一天内串行。事件命名一致性最佳。
#   by_batch ：按批次分线程，同一天的多批也可并发。处理速度更快，但同一天
#              事件命名可能不一致（LLM 看到的事件池是调用时刻的快照，可能
#              落后于其他线程刚加进去的新事件名）。
# 切换模式会触发上下文指纹校验失败 -> 自动归档旧进度从头跑（见 _CONTEXT_FINGERPRINT_FIELDS）。
PARALLEL_MODE = str(_CURATOR.get("parallel_mode", "by_date")).strip().lower()
if PARALLEL_MODE not in ("by_date", "by_batch"):
    PARALLEL_MODE = "by_date"

# HOME_CITY：本地省/直辖市/常驻地。双重职责：
#   1) 强地点（alias_replace / POI 命中）若以此前缀开头，传给 LLM 时会去掉前缀避免命名冗余
#   2) 弱地点（纯行政地址）若以此前缀开头，会被完全屏蔽，避免"XX街道日常"这种僵硬命名
#      异地的弱地点（"成都市 锦江区 春熙路"）会保留，给模型异地判断信号
# 空字符串表示不做任何处理。
HOME_CITY = _COMMON.get("home_city", "重庆市")

# ==========================================
# 2. Provider 配置（可切换）
# ==========================================
# provider 与各家配置均来自 pipeline_config.yaml 的 curator 段。
# 若配置缺失则退回下面的内置默认值。
_DEFAULT_PROVIDER_CONFIGS = {
    "ollama": {
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key": "ollama",
        "model": "hf.co/unsloth/gemma-4-31B-it-qat-GGUF:UD-Q4_K_XL",
        "json_mode": "ollama_format",      # extra_body.format=json
        "supports_num_predict": True,
    },
    "volcengine": {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "api_key": "xxxxxx",
        "model": "doubao-seed-2-0-lite-260428",
        "json_mode": None,
        "supports_num_predict": False,
    },
    "dashscope": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": "xxxxxx",
        "model": "qwen3.7-plus",
        "json_mode": "openai_response_format",
        "supports_num_predict": False,
    },
    "kimi": {
        "base_url": "https://api.moonshot.cn/v1",
        "api_key": "xxxxxx",
        "model": "kimi-k2.6",
        "json_mode": "openai_response_format",
        "supports_num_predict": False,
    },
    "xiaomi_mimo": {
        "base_url": "https://api.xiaomimimo.com/v1",
        "api_key": "xxxxxx",
        "model": "mimo-v2.5",
        "json_mode": None,
        "supports_num_predict": False,
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "api_key": "xxxxxx",
        "model": "deepseek-v4-pro",
        "json_mode": "openai_response_format",
        "supports_num_predict": False,
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "api_key": "sk-xxxxxxxx",
        "model": "gpt-4o",
        "json_mode": "openai_response_format",
        "supports_num_predict": False,
    },
}

PROVIDER = _CURATOR.get("provider", "ollama")   # ollama / volcengine / dashscope / kimi / xiaomi_mimo / deepseek / openai
PROVIDER_CONFIGS = _CURATOR.get("provider_configs") or _DEFAULT_PROVIDER_CONFIGS


# ==========================================
# 3. 价格表（元/百万 token，请按平台实际单价更新）
# ==========================================
PRICING = {
    # 本地推理免费
    "gemma4:31b":                      {"input": 0,    "cached": 0,    "output": 0,    "currency": "RMB"},
    "qwen3.6:27b":                     {"input": 0,    "cached": 0,    "output": 0,    "currency": "RMB"},
    "hf.co/unsloth/gemma-4-31B-it-qat-GGUF:UD-Q4_K_XL": {"input": 0, "cached": 0, "output": 0, "currency": "RMB"},
    # 火山引擎（豆包视觉，参考价，请确认）
    "doubao-seed-2-0-lite-260428":     {"input": 0.6,  "cached": 0.12, "output": 3.6,  "currency": "RMB"},
    # 阿里百炼
    "qwen3.7-plus":                    {"input": 1.6,  "cached": 0.32, "output": 6.4,  "currency": "RMB"},
    "qwen3.6-flash":                   {"input": 1.2,  "cached": 0.12, "output": 7.2,  "currency": "RMB"},
    # KIMI
    "kimi-k2.6":                       {"input": 6.5,  "cached": 1.1,  "output": 27.0, "currency": "RMB"},
    "moonshot-v1-32k-vision-preview":  {"input": 5.0,  "cached": 5.0,  "output": 20.0, "currency": "RMB"},
    # deepseek
    "deepseek-v4-pro":                 {"input": 3.0,  "cached": 0.025,"output": 6.0,  "currency": "RMB"},
    # 小米
    "mimo-v2.5":                       {"input": 1.0,  "cached": 0.02, "output": 2.0,  "currency": "RMB"},
    # OpenAI
    "gpt-4o":                          {"input": 2.50, "cached": 1.25, "output": 10.0, "currency": "USD"},
}

# 三层重试参数
TIER1_TEMP, TIER1_TOK, TIER1_TIMEOUT = 0.0, 16384, 720.0
TIER2_TEMP, TIER2_TOK, TIER2_TIMEOUT = 0.1, 16384, 480.0
TIER3_TEMP, TIER3_TOK, TIER3_TIMEOUT = 0.0, 8192,  360.0
TIER3_SUB_BATCH_SIZE = 2

# 限流退避参数（429 等临时错误用，原温原档位重试）
RATE_LIMIT_MAX_RETRIES = 4       # 429 内部重试次数
RATE_LIMIT_BACKOFF_BASE = 2.0    # 秒，2 → 4 → 8 → 16 + 抖动
# 服务端瞬时错误（HTTP 5xx / 网络抖动）退避重试：独立于 429 限流计数
TRANSIENT_MAX_RETRIES = 3        # 5xx/网络错误内部重试次数
TRANSIENT_BACKOFF_BASE = 3.0     # 秒，3 → 6 → 12 + 抖动

# 致命错误熔断（欠费 / 认证失败 / 模型不可用，立即停止避免空转烧 quota）
CONSECUTIVE_FAILURE_LIMIT = 5    # 连续 N 次未知失败也触发熔断（兜底）

# ==========================================
# 4. 全局状态
# ==========================================
fail_log_lock = threading.Lock()
target_dir_lock = threading.Lock()
progress_lock = threading.Lock()
token_stats_lock = threading.Lock()
# by_batch 模式下保护 progress_state["daily_events"] 的读写：
#   - 进入 process_one_batch_standalone 时拷贝当天事件池快照（读）
#   - 批次完成后把本批新增事件名 merge 回全局（写）
# by_date 模式下不使用此锁（每天单线程，process_one_date 内部不并发）。
daily_events_lock = threading.Lock()

# 熔断相关全局状态
_consecutive_failures = 0                  # 连续未知失败计数
_failure_lock = threading.Lock()
_stop_event = threading.Event()            # 一旦 set，所有后续调用立即短路退出

# ==========================================
# 手机端删除列表（记录所有被剔除的照片原始文件名）
# ==========================================
_deleted_photos_list = []                  # 记录所有 is_highlight=False 的照片文件名
_deleted_photos_lock = threading.Lock()    # 多线程保护

token_stats = {
    "total_input": 0, "total_cached_input": 0, "total_output": 0, "total_calls": 0,
    "by_tier": {
        "T1": {"calls": 0, "input": 0, "output": 0},
        "T2": {"calls": 0, "input": 0, "output": 0},
        "T3": {"calls": 0, "input": 0, "output": 0},
    },
}

# 进度状态：在 main 里加载/保存
progress_state = {
    "completed_batches": set(),
    "daily_events": defaultdict(set),  # date_str -> set of event_tag
}

cfg = PROVIDER_CONFIGS[PROVIDER]

client = OpenAI(
    base_url=cfg["base_url"],
    api_key=cfg["api_key"],
    timeout=900.0,
    max_retries=0,
)

os.makedirs(os.path.join(_cfg_loader.BASE_DIR, "logs"), exist_ok=True)
log_filename = os.path.join(
    _cfg_loader.BASE_DIR, "logs",
    f"02_photo_sorter_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
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

# 屏蔽 httpx 成功请求日志（"HTTP Request: POST ... HTTP/1.1 200 OK"），
# 只保留 WARNING 及以上级别（连接失败、超时等错误仍会打印）。
# httpx 是 openai SDK 底层 HTTP 客户端，默认每个请求打一行 INFO，非常吵。
logging.getLogger("httpx").setLevel(logging.WARNING)

# ==========================================
# 5. 进度持久化（断点续跑）
# ==========================================
# 上下文指纹：写进 02_progress.json 顶层，用于续跑时校验"这份进度对应的运行环境
# 是否和当前一致"。任一字段不匹配都会触发自愈（旧文件归档、02 从头跑），
# 防止用户改了 source_dir / target_dir / home_city / 档位 / profile 后断点续跑
# 错乱地跳过本该重跑的批次。
# source_dir_snapshot_hash：输入目录内容快照（相对路径+mtime+size 的 sha256），
#   由 01a 写入 batches json 顶层 source_dir_snapshot.hash，02 启动时读取转写。
#   覆盖中断期间在输入目录增/删/改/重命名照片文件导致 batch_id 漂移的场景。
_CONTEXT_FINGERPRINT_FIELDS = ("source_dir", "target_dir", "extraction_level",
                                "profile", "home_city", "source_dir_snapshot_hash",
                                "parallel_mode")


def _load_source_dir_snapshot_hash():
    """
    从 batches json 读取 01a 写入的 source_dir_snapshot.hash。

    返回 hash 字符串；batches json 不存在或字段缺失时返回 None。

    注意：这里不重新扫描 source_dir，而是读取 01a 刚产出的 batches json。
    方案 B 下断点续跑时：
    - 若 GUI 跳过了 01a -> batches json 是上次的旧文件 -> hash 是旧值
      -> 02 的 load_progress 无法检测输入目录变化
      -> 这就是为什么 GUI 跳过判定必须独立扫描 source_dir（见 app.py）
    - 若未跳过 01a -> batches json 是这次的新文件 -> hash 是新值
      -> 与 02_progress 里的 hash 对比，变化则自愈
    """
    if not os.path.exists(BATCHES_FILE):
        return None
    try:
        with open(BATCHES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        snapshot = data.get("source_dir_snapshot")
        if isinstance(snapshot, dict):
            return snapshot.get("hash")
    except Exception:
        pass
    return None


def _current_context_fingerprint() -> dict:
    """构造当前运行环境的上下文指纹快照。"""
    return {
        "source_dir": SOURCE_DIR,
        "target_dir": TARGET_DIR,
        "extraction_level": EXTRACTION_LEVEL,
        "profile": PROFILE,
        "home_city": HOME_CITY,
        "source_dir_snapshot_hash": _load_source_dir_snapshot_hash(),
        "parallel_mode": PARALLEL_MODE,
    }


def _archive_mismatched_progress(mismatched_fields):
    """
    上下文指纹不匹配时的自愈动作：
    1. 把当前 02_progress_<profile>.json 移动到 _pipeline_archive/mismatched/
       并加 _mismatched_<时间戳> 后缀（不删除，便于事后追溯）。
    2. 打 WARNING 日志列出哪些字段不匹配及新旧值。
    3. 清空 progress_state（completed_batches / daily_events），02 将从头跑。
       token_stats 保留（累计统计用）。
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_dir = os.path.join(_cfg_loader.BASE_DIR, "_pipeline_archive", "mismatched")
    os.makedirs(archive_dir, exist_ok=True)
    base_name = os.path.basename(PROGRESS_FILE)
    archived_name = f"{base_name[:-5]}_mismatched_{stamp}.json"  # 去掉 .json 加后缀
    archived_path = os.path.join(archive_dir, archived_name)
    try:
        shutil.move(PROGRESS_FILE, archived_path)
    except Exception as e:
        logger.warning(f"  ⚠️ 旧进度文件归档失败：{e}（将直接覆盖）")

    logger.warning("=" * 70)
    logger.warning("⚠️ 检测到 02 进度文件的上下文与当前运行环境不匹配，已重置进度从头跑。")
    logger.warning("   旧进度文件已归档（不删除）：")
    logger.warning(f"     {PROGRESS_FILE}")
    logger.warning(f"   -> {archived_path}")
    for field, (old_val, new_val) in mismatched_fields.items():
        if field == "source_dir_snapshot_hash":
            # 输入目录内容变化：打印人类可读说明 + hash 值（小白看懂"输入目录变了"，
            # 专业用户可凭 hash 追溯具体哪次扫描的快照）
            logger.warning(f"   - 输入目录内容变化（source_dir_snapshot_hash 不匹配）")
            logger.warning(f"     可能原因：中断期间在输入目录增/删/改/重命名了照片文件")
            logger.warning(f"     旧快照 hash: {str(old_val)[:16] if old_val else '(无)'}...")
            logger.warning(f"     新快照 hash: {str(new_val)[:16] if new_val else '(无)'}...")
        else:
            logger.warning(f"   - {field}: {old_val!r} -> {new_val!r}")
    logger.warning("   如需保持旧进度，请改回上述配置后重跑；否则继续从头归档。")
    logger.warning("=" * 70)

    progress_state["completed_batches"] = set()
    progress_state["daily_events"] = defaultdict(set)


def load_progress():
    if not os.path.exists(PROGRESS_FILE):
        return
    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"进度文件解析失败，将从头开始：{e}")
        return

    # 上下文指纹校验：任一字段不匹配则自愈（旧文件归档、清空进度从头跑）。
    # 旧版本 progress 文件没有 context_fingerprint 字段，视为不匹配。
    stored_fp = data.get("context_fingerprint")
    current_fp = _current_context_fingerprint()
    mismatched = {}
    if not isinstance(stored_fp, dict):
        # 旧版本文件或字段缺失，整体视为不匹配
        for k in _CONTEXT_FINGERPRINT_FIELDS:
            mismatched[k] = ("(旧版本文件，无指纹)", current_fp[k])
    else:
        for k in _CONTEXT_FINGERPRINT_FIELDS:
            old_val = stored_fp.get(k)
            new_val = current_fp.get(k)
            if old_val != new_val:
                mismatched[k] = (old_val, new_val)

    if mismatched:
        _archive_mismatched_progress(mismatched)
        return  # 进度已清空，02 将从头跑

    progress_state["completed_batches"] = set(data.get("completed_batches", []))
    for date_str, events in data.get("daily_events", {}).items():
        progress_state["daily_events"][date_str] = set(events)
    # 恢复已剔除照片列表（断点续跑关键）：不恢复的话，续跑时被跳过的已完成批次
    # 的剔除记录会永久丢失，导致最终手机端删除列表 txt 缺项。json 把 tuple 存成
    # [filename, epoch_sec] 数组，这里转回 tuple 保持与内存追加时一致。
    # 指纹不匹配的分支已在上方 early return，走到这里的必是同上下文续跑，恢复即安全。
    with _deleted_photos_lock:
        _deleted_photos_list.clear()  # 进程内只恢复一次，clear 防御性避免重复累计
        for item in data.get("deleted_photos", []):
            if isinstance(item, (list, tuple)) and len(item) == 2:
                _deleted_photos_list.append((item[0], item[1]))
    ts = data.get("token_stats")
    if ts:
        token_stats["total_input"] = ts.get("total_input", 0)
        token_stats["total_cached_input"] = ts.get("total_cached_input", 0)
        token_stats["total_output"] = ts.get("total_output", 0)
        token_stats["total_calls"] = ts.get("total_calls", 0)
        for tier_name in ("T1", "T2", "T3"):
            if tier_name in ts.get("by_tier", {}):
                token_stats["by_tier"][tier_name].update(ts["by_tier"][tier_name])
    logger.info(
        f"📥 加载进度：已完成 {len(progress_state['completed_batches'])} 批，"
        f"涉及 {len(progress_state['daily_events'])} 天的事件池，"
        f"已剔除 {len(_deleted_photos_list)} 张照片"
    )


def save_progress():
    """线程安全的进度持久化（每批完成后调用）"""
    with progress_lock:
        # ---- 计算实时 ETA / 已用时间，写进进度文件供远程 API 读取 ----
        # 说明：progress_monitor() 后台线程也算同样的值，但只打到日志里；
        # 这里在每批完成时同步落盘，远程手机端才能通过 /api/status 拿到。
        with _run_stats_lock:
            batches_done = _run_stats["batches_done"]
            total_remaining = _run_stats["total_remaining"]
            start_time = _run_stats["start_time"] or time.time()

        now = time.time()
        total_dt = max(now - start_time, 1e-6)
        avg_batch_rate = batches_done / total_dt * 60.0  # 批/分钟
        remaining_batches = max(total_remaining - batches_done, 0)

        if batches_done > 0 and avg_batch_rate > 0:
            # ETA 用累计平均速度推算（比瞬时更稳，与 progress_monitor 保持一致）
            eta_seconds = int(remaining_batches / (avg_batch_rate / 60.0))
            finish_time = datetime.fromtimestamp(now + eta_seconds).strftime("%m-%d %H:%M:%S")
        else:
            eta_seconds = 0        # 样本不足，前端显示"正在计算"
            finish_time = None

        elapsed_sec = int(now - start_time)

        # 加锁快照剔除列表，避免与其他线程并发 append 竞态；转为二元 list 便于 JSON 序列化
        with _deleted_photos_lock:
            deleted_snapshot = [[fname, epoch] for (fname, epoch) in _deleted_photos_list]

        data = {
            "completed_batches": sorted(progress_state["completed_batches"]),
            "daily_events": {k: sorted(v) for k, v in progress_state["daily_events"].items()},
            "token_stats": token_stats,
            "context_fingerprint": _current_context_fingerprint(),
            # 已剔除照片快照（断点续跑必备）：list of [filename, epoch_sec]。
            # 续跑时若不恢复，已完成批次的剔除记录会丢失，导致手机端删除列表不完整。
            "deleted_photos": deleted_snapshot,
            # ---- 远程进度展示字段（api_pipeline_runner._monitor_progress 读取）----
            "total_batches": total_remaining,   # 本次需处理的批次总数
            "eta_sec": eta_seconds,             # 预估剩余秒数（0 = 样本不足）
            "finish_time": finish_time,         # 预计完成时刻 "MM-DD HH:MM:SS"
            "elapsed_sec": elapsed_sec,         # 已用时间（秒）
        }
        tmp = PROGRESS_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, PROGRESS_FILE)
        except Exception as e:
            logger.error(f"进度保存失败：{e}")


# ==========================================
# 5.5 实时速度与 ETA 监控
# ==========================================
# 02 脚本运行时间很长，这里用一个后台线程定时统计并输出：
#   - 实时速度：距上次报告的时间窗口内完成的批次/照片数（反映"当前"吞吐）
#   - 平均速度：本次运行开始至今的累计平均（更平稳，用于估算 ETA）
#   - ETA：按平均速度推算剩余批次还需多久，并给出预计完成时刻
PROGRESS_REPORT_INTERVAL = _CURATOR.get("progress_report_interval", 60)  # 秒

# 实时速度的滑动时间窗口（秒）。取报告间隔的若干倍，保证窗口里通常有
# 多个批次样本，算出的瞬时速度才是有意义的小数，而非"每个间隔完成几批"的整数。
PROGRESS_WINDOW_SECONDS = max(PROGRESS_REPORT_INTERVAL * 5, 300)

_run_stats_lock = threading.Lock()
_run_stats = {
    "start_time": None,     # 本次运行开始时间
    "batches_done": 0,      # 本次运行已完成批次数（不含此前断点续跑已完成的）
    "photos_done": 0,       # 本次运行已处理照片数
    "total_remaining": 0,   # 本次需处理的批次总数（启动时设定）
    # 每批完成的事件流：(完成时刻, 该批照片数)。用于按真实时间戳算实时速度，
    # 避免"窗口 == 报告间隔"导致速度恒为整数批数的问题。
    "events": deque(),
}
_monitor_stop = threading.Event()


def record_batch_done(num_photos):
    """每完成一批调用，累加计数并记录完成时刻（供监控线程算实时/平均速度）"""
    now = time.time()
    with _run_stats_lock:
        _run_stats["batches_done"] += 1
        _run_stats["photos_done"] += num_photos
        _run_stats["events"].append((now, num_photos))



def _fmt_duration(seconds):
    """把秒数格式化为可读的 h/m/s"""
    if seconds is None or seconds < 0:
        return "未知"
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h > 0:
        return f"{h}h{m:02d}m{sec:02d}s"
    if m > 0:
        return f"{m}m{sec:02d}s"
    return f"{sec}s"


def progress_monitor():
    """
    后台线程：每 PROGRESS_REPORT_INTERVAL 秒输出一次实时速度与 ETA。
    - 实时速度：基于最近 PROGRESS_WINDOW_SECONDS 秒内实际完成的批次时间戳计算，
      而非"上次报告以来的整数批数"。这样即使单线程慢速跑，速度也是有意义的小数。
    - ETA：用累计平均速度推算（比瞬时速度更稳定，避免抖动导致 ETA 跳变）
    """
    while not _monitor_stop.wait(PROGRESS_REPORT_INTERVAL):
        now = time.time()
        with _run_stats_lock:
            batches_done = _run_stats["batches_done"]
            photos_done = _run_stats["photos_done"]
            total_remaining = _run_stats["total_remaining"]
            start_time = _run_stats["start_time"] or now

            # 滑动窗口：丢弃早于 now - PROGRESS_WINDOW_SECONDS 的完成事件
            events = _run_stats["events"]
            cutoff = now - PROGRESS_WINDOW_SECONDS
            while events and events[0][0] < cutoff:
                events.popleft()
            # 拷贝一份窗口内事件供锁外计算
            win_events = list(events)

        # 实时（窗口）速度：窗口内完成的批次/照片 ÷ 窗口实际时长
        # 窗口起点取"运行开始"与"now - 窗口长度"中较晚者，保证早期不虚高
        window_start = max(start_time, now - PROGRESS_WINDOW_SECONDS)
        window_dt = max(now - window_start, 1e-6)
        win_batches = len(win_events)
        win_photos = sum(n for _, n in win_events)
        win_batch_rate = win_batches / window_dt * 60.0     # 批/分钟
        win_photo_rate = win_photos / window_dt * 60.0      # 张/分钟

        # 累计平均速度：运行开始至今
        total_dt = max(now - start_time, 1e-6)
        avg_batch_rate = batches_done / total_dt * 60.0                  # 批/分钟
        avg_photo_rate = photos_done / total_dt * 60.0                   # 张/分钟

        remaining_batches = max(total_remaining - batches_done, 0)

        if batches_done == 0 or avg_batch_rate <= 0:
            logger.info(
                f"⏱️ [进度] 已完成 {batches_done}/{total_remaining} 批 | "
                f"实时 {win_photo_rate:.1f} 张/分 | ETA 计算中（样本不足）"
            )
        else:
            eta_seconds = remaining_batches / (avg_batch_rate / 60.0)
            finish_at = datetime.fromtimestamp(now + eta_seconds).strftime("%m-%d %H:%M:%S")
            pct = batches_done / total_remaining * 100 if total_remaining else 0
            logger.info(
                f"⏱️ [进度] {batches_done}/{total_remaining} 批 ({pct:.1f}%) | "
                f"实时 {win_batch_rate:.2f} 批/分 · {win_photo_rate:.1f} 张/分"
                f"（近 {int(window_dt)}s 完成 {win_batches} 批）| "
                f"平均 {avg_batch_rate:.2f} 批/分 · {avg_photo_rate:.1f} 张/分 | "
                f"剩余 {remaining_batches} 批 | ETA {_fmt_duration(eta_seconds)}（预计 {finish_at} 完成）"
            )



# ==========================================
# 6. Token 统计
# ==========================================
def update_token_stats(usage, tier_name):
    if not usage:
        return
    prompt_t = getattr(usage, "prompt_tokens", 0) or 0
    completion_t = getattr(usage, "completion_tokens", 0) or 0
    cached_t = 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details:
        cached_t = (
            getattr(details, "cached_tokens", 0)
            or (details.get("cached_tokens", 0) if isinstance(details, dict) else 0)
            or 0
        )
    cached_t = cached_t or getattr(usage, "cache_read_input_tokens", 0) or 0

    real_input = max(prompt_t - cached_t, 0)
    with token_stats_lock:
        token_stats["total_input"] += real_input
        token_stats["total_cached_input"] += cached_t
        token_stats["total_output"] += completion_t
        token_stats["total_calls"] += 1
        tier = token_stats["by_tier"].get(tier_name)
        if tier:
            tier["calls"] += 1
            tier["input"] += prompt_t
            tier["output"] += completion_t


def print_token_summary(elapsed=None, batches_done=None, photos_done=None):
    """打印 02 阶段 Token 消耗 + 本次性能统计汇总。

    可选参数（默认 None 时不打印性能统计，用于 remain==0 早返回等无实际处理的分支）：
      elapsed      本次运行实际耗时（秒）
      batches_done 本次运行处理的批数（与 elapsed 同源，用于算速度）
      photos_done  本次运行处理的照片数（与 elapsed 同源，用于算速度）

    统计口径均"分子分母同源"：
      速度   = 本次处理量 / 本次耗时（batches_done|photos_done ÷ elapsed）
      平均token/批 = token_stats 累计 / token_stats['total_calls']（同为 token_stats 内部累计，
                     续跑时二者一起从进度文件恢复，比值始终自洽）
    """
    pricing = PRICING.get(cfg["model"])
    logger.info("=" * 60)
    logger.info(f"📊 Token 消耗汇总（模型：{cfg['model']}，Provider：{PROVIDER}）")
    logger.info(f"   总调用次数:     {token_stats['total_calls']:>12,}")
    logger.info(f"   总输入 (新):    {token_stats['total_input']:>12,}")
    logger.info(f"   总输入 (缓存):  {token_stats['total_cached_input']:>12,}")
    logger.info(f"   总输出:         {token_stats['total_output']:>12,}")
    grand_total = token_stats['total_input'] + token_stats['total_cached_input'] + token_stats['total_output']
    logger.info(f"   总 token:       {grand_total:>12,}")
    logger.info(f"   按层级:")
    for tn in ("T1", "T2", "T3"):
        t = token_stats["by_tier"][tn]
        logger.info(f"     {tn}: {t['calls']:>5} 次 | 输入 {t['input']:>10,} | 输出 {t['output']:>10,}")
    if pricing:
        cur = pricing.get("currency", "RMB")
        cost_input = token_stats["total_input"] / 1_000_000 * pricing["input"]
        cost_cached = token_stats["total_cached_input"] / 1_000_000 * pricing["cached"]
        cost_output = token_stats["total_output"] / 1_000_000 * pricing["output"]
        cost_total = cost_input + cost_cached + cost_output
        logger.info(f"   费用估算 ({cur}):")
        logger.info(f"     输入:    {cost_input:>10.4f}")
        logger.info(f"     缓存:    {cost_cached:>10.4f}")
        logger.info(f"     输出:    {cost_output:>10.4f}")
        logger.info(f"     合计:    {cost_total:>10.4f}")
    else:
        logger.info(f"   ⚠️ 未配置 {cfg['model']} 的价格表，跳过费用估算")

    # ── 本次性能统计（速度 + 平均 token/批），仅在有实际处理时打印 ──
    _print_perf_summary(elapsed, batches_done, photos_done, grand_total)
    logger.info("=" * 60)


def _print_perf_summary(elapsed, batches_done, photos_done, grand_total):
    """打印本次运行的处理速度与平均 token/批。分子分母严格同源。"""
    total_calls = token_stats["total_calls"]
    # 平均 token/批：token 累计 ÷ token 调用次数（同源，续跑也自洽）
    if total_calls > 0:
        avg_input = token_stats["total_input"] / total_calls
        avg_cached = token_stats["total_cached_input"] / total_calls
        avg_output = token_stats["total_output"] / total_calls
        avg_total = grand_total / total_calls
        logger.info(
            f"   📊 平均 token/次调用: 输入 {avg_input:,.0f} · 缓存 {avg_cached:,.0f} · "
            f"输出 {avg_output:,.0f} · 总计 {avg_total:,.0f}（基于 {total_calls:,} 次调用）"
        )
    # 处理速度：本次处理量 ÷ 本次耗时（同源）
    if elapsed and elapsed > 0 and batches_done and batches_done > 0:
        minutes = elapsed / 60.0
        batch_rate = batches_done / minutes
        photo_rate = (photos_done or 0) / minutes
        logger.info(
            f"   ⏱️ 本次处理速度: {batch_rate:.1f} 批/分 · {photo_rate:.1f} 张/分"
            f"（本次处理 {batches_done:,} 批 / {photos_done or 0:,} 张，耗时 {minutes:.1f} 分）"
        )


# ==========================================
# 7. 工具函数
# ==========================================
def resize_and_encode_image(file_path):
    """
    缩放图片到最大边 MAX_IMAGE_SIZE 并编码为 base64 data URI。
    
    优化：如果图片已经是 JPEG RGB 且尺寸合适且无旋转需求，
    直接读取原始字节，跳过二次 JPEG 压缩，避免代际损失。
    适用于手机端上传的 768px 缩略图场景。
    """
    try:
        with Image.open(file_path) as img:
            img_format = img.format
            
            # 检查是否需要旋转（Orientation != 1 表示需要）
            exif = img.getexif()
            needs_transpose = exif.get(0x0112, 1) != 1  # TAG_ORIENTATION = 0x0112
            
            # 快速路径：已是 JPEG RGB 且尺寸合适且无旋转需求
            if (img.width <= MAX_IMAGE_SIZE and img.height <= MAX_IMAGE_SIZE
                and img_format == 'JPEG' and img.mode == 'RGB'
                and not needs_transpose):
                # 直接读原始字节，跳过二次压缩
                with open(file_path, 'rb') as f:
                    raw_bytes = f.read()
                return f"data:image/jpeg;base64,{base64.b64encode(raw_bytes).decode('utf-8')}"
            
            # 常规路径：需要旋转/缩放/格式转换
            img = ImageOps.exif_transpose(img)
            if img.mode != 'RGB':
                img = img.convert('RGB')
            img.thumbnail((MAX_IMAGE_SIZE, MAX_IMAGE_SIZE))
            buffered = BytesIO()
            img.save(buffered, format="JPEG", quality=85)
            return f"data:image/jpeg;base64,{base64.b64encode(buffered.getvalue()).decode('utf-8')}"
    except Exception:
        return None


def sanitize_event_tag(event_tag):
    if not event_tag:
        return event_tag
    illegal = ['/', '\\', ':', '*', '?', '"', '<', '>', '|']
    cleaned = event_tag
    for ch in illegal:
        cleaned = cleaned.replace(ch, '、')
    cleaned = re.sub(r'\s+', '', cleaned)
    return cleaned[:100]


def resolve_gps_for_prompt(photo):
    """
    决策传给 LLM 的最终地点字符串。规则：
    - 强地点（gps_strong：景区/POI/alias_replace）：无论本地异地都用，
      本地自动去 HOME_CITY 前缀避免命名冗余（"重庆市 两江新区 铁山坪森林公园" → "两江新区 铁山坪森林公园"）
    - 弱地点（gps_text 但无 gps_strong：纯行政地址）：异地保留（"成都市 锦江区 春熙路"是有用信号），
      本地屏蔽（"两江新区 XX街道"是噪声，会污染日常活动命名）
    - 无 GPS：返回 None
    """
    strong = photo.get("gps_strong")
    text = photo.get("gps_text")

    if strong:
        # 强地点：本地去前缀，异地保留全名
        if HOME_CITY and strong.startswith(HOME_CITY):
            stripped = strong[len(HOME_CITY):].strip()
            return stripped if stripped else strong
        return strong

    if text:
        # 纯行政地址：本地屏蔽，异地保留
        if HOME_CITY and text.startswith(HOME_CITY):
            return None
        return text

    return None


def is_fatal_api_error(err_str):
    """
    识别不可恢复的 API 错误：欠费 / 认证失败 / 模型不存在 / 永久参数错误。
    命中后立即触发熔断，避免欠费场景下空转烧 quota。
    """
    s = err_str.lower()
    fatal_patterns = [
        # 欠费 / 配额耗尽
        "insufficient_user_quota", "insufficient_quota", "insufficient balance",
        "account balance", "insufficientbalance", "balance not enough",
        "balance is insufficient", "exceeded_current_quota",
        "余额不足", "欠费", "arrears", "billing", "payment required",
        # 认证失败
        "invalid_api_key", "invalid api key", "incorrect api key",
        "unauthorized", "authentication_error",
        # 模型不存在 / 永久参数错误
        "model not found", "model_not_found",
        "is not supported by this model",
        # 显式 HTTP 状态码（必须带分隔符避免误伤 token 数等场景）
        " 401 ", " 402 ", " 403 ",
    ]
    return any(p in s for p in fatal_patterns)


def _on_request_success():
    """成功调用 → 重置连续失败计数"""
    global _consecutive_failures
    with _failure_lock:
        _consecutive_failures = 0


def _on_request_failure(reason):
    """
    记一次未知失败。连续达到 CONSECUTIVE_FAILURE_LIMIT 触发熔断。
    返回是否已触发熔断。
    """
    global _consecutive_failures
    with _failure_lock:
        _consecutive_failures += 1
        if _consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
            logger.error(
                f"  [熔断] 连续 {_consecutive_failures} 次未知失败 ({reason})，立即停止后续调用"
            )
            _stop_event.set()
            return True
    return False


# ==========================================
# 8. 单次 API 调用（多 Provider 适配）
# ==========================================
def build_request_kwargs(temp, max_tok, timeout, messages):
    """根据 PROVIDER 配置生成不同形态的请求参数"""
    kwargs = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": temp,
        "max_tokens": max_tok,
        "timeout": timeout,
    }
    json_mode = cfg.get("json_mode")
    if json_mode == "openai_response_format":
        kwargs["response_format"] = {"type": "json_object"}
    elif json_mode == "ollama_format":
        extra = {"format": "json"}
        if cfg.get("supports_num_predict"):
            extra["options"] = {"num_predict": max_tok}
        kwargs["extra_body"] = extra
    return kwargs


# 诊断日志：每个 provider 首次请求参数只落盘一次
_first_request_logged = set()
_first_request_lock = threading.Lock()


def _sanitize_kwargs_for_debug(kwargs):
    """
    深拷贝 kwargs 并把 messages 里的图片 base64 数据替换为占位符，
    避免诊断日志文件里塞满巨大的 base64 字符串。原 kwargs 不受影响。
    """
    safe = copy.deepcopy(kwargs)
    for msg in safe.get("messages", []):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                url = part.get("image_url", {}).get("url", "")
                n = len(url)
                part["image_url"]["url"] = f"<image data omitted: {n} bytes>"
    return safe


def _maybe_dump_first_request(kwargs):
    """
    若 debug_log_first_request 开启，且当前 provider 尚未记录过，
    把脱敏后的请求 kwargs 写到 logs/debug_first_request_<provider>_<时间戳>.json。
    仅记录每个 provider 的首次请求。任何异常都不影响主流程。
    """
    if not DEBUG_LOG_FIRST_REQUEST:
        return
    with _first_request_lock:
        if PROVIDER in _first_request_logged:
            return
        _first_request_logged.add(PROVIDER)
    try:
        payload = {
            "provider": PROVIDER,
            "model": cfg.get("model"),
            "json_mode": cfg.get("json_mode"),
            "supports_num_predict": cfg.get("supports_num_predict"),
            "timestamp": datetime.now().isoformat(),
            "request_kwargs": _sanitize_kwargs_for_debug(kwargs),
        }
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = os.path.join(
            _cfg_loader.BASE_DIR, "logs",
            f"debug_first_request_{PROVIDER}_{ts}.json",
        )
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info(f"  [诊断] 已记录 {PROVIDER} 首次请求参数 -> {out_path}")
    except Exception as e:
        logger.warning(f"  [诊断] 记录首次请求参数失败（不影响处理）：{e}")


def call_vlm_once(batch_images, system_prompt, date_str, daily_events_str,
                  temp, max_tok, timeout, tier_label):
    """
    单次 VLM 调用。
    - 返回字符串：模型原始响应文本（成功）
    - 返回 None：本次失败（已记日志），上层应继续 T2/T3 兜底
    - 触发 _stop_event：致命错误熔断，全局停止

    注意：batch_images 元组中的 gps_text 已是 resolve_gps_for_prompt 处理后的最终值
         （强地点去前缀 / 弱地点本地屏蔽 / 异地保留），本函数不再做语义判断
    """
    # 入口短路：已熔断则不再发起请求
    if _stop_event.is_set():
        return None

    user_text = (
        f"【拍摄日期】:{date_str}\n"
        f"【当天已建事件池】:{daily_events_str}\n"
        f"这是一组按时间顺序连续拍摄的照片。请严格按照系统指令执行,"
        f"为每一张照片独立输出一条 JSON 记录,输出纯 JSON 数组。"
    )
    user_content = [{"type": "text", "text": user_text}]
    for item in batch_images:
        if len(item) == 4:
            photo_id, dt, base64_str, gps_text = item
        else:
            photo_id, dt, base64_str = item
            gps_text = None
        time_str = dt.strftime("%H:%M:%S")
        meta_line = f"照片编号: {photo_id}，时间: {time_str}"
        if gps_text:
            # gps_text 此时已是最终值，直接拼入
            meta_line += f"，地点: {gps_text}"
        user_content.append({"type": "text", "text": meta_line})
        user_content.append({"type": "image_url", "image_url": {"url": base64_str}})
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    kwargs = build_request_kwargs(temp, max_tok, timeout, messages)
    _maybe_dump_first_request(kwargs)
    logger.info(f"  [{tier_label}] 启动 (温度{temp}, {max_tok}tok, {timeout:.0f}s)...")

    transient_attempt = 0

    for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
        # 每一轮入口都检查熔断（限流退避期间可能被其他线程触发）
        if _stop_event.is_set():
            return None

        start_time = time.time()

        try:
            response = client.chat.completions.create(**kwargs)
            usage = response.usage
            response_text = response.choices[0].message.content

            # 成功 → 重置连续失败计数
            _on_request_success()

            # tier_name 抽取（从大到小匹配，避免 "T3-1" 误中 "T1"）
            tier_name = "T1"
            for t in ("T3", "T2", "T1"):
                if f"-{t}" in tier_label:
                    tier_name = t
                    break
            update_token_stats(usage, tier_name)

            if usage:
                cached_info = ""
                details = getattr(usage, "prompt_tokens_details", None)
                if details:
                    ct = getattr(details, "cached_tokens", 0) or (
                        details.get("cached_tokens", 0) if isinstance(details, dict) else 0
                    )
                    if ct:
                        cached_info = f" (缓存 {ct})"
                logger.info(
                    f"  [{tier_label}] Token 输入 {usage.prompt_tokens}{cached_info}, 输出 {usage.completion_tokens}"
                )
                if usage.completion_tokens >= max_tok - 50:
                    logger.warning(f"  [{tier_label}] 输出顶到 {max_tok}，判定死锁")
                    return None
                if not response_text or not response_text.strip():
                    logger.warning(f"  [{tier_label}] 空返回")
                    return None

                # 熔断兜底：调用期间其他线程可能已经触发熔断
                # 此时即便本次 HTTP 成功，也丢弃响应，避免在残破状态下应用结果到磁盘
                if _stop_event.is_set():
                    logger.warning(f"  [{tier_label}] 调用期间触发熔断，丢弃本次响应")
                    return None

            return response_text


        except APITimeoutError:
            actual_elapsed = time.time() - start_time
            # 真假超时区分：
            # - 真超时（模型推理卡顿/慢）：elapsed 会接近配置的 timeout，应交给 T2/T3 升温重试，不计熔断
            # - 假超时（本地服务下线/网络不可达，被 SDK 包装成 APITimeoutError）：elapsed 远小于配置值
            #   这种情况 T2/T3 重试也救不回来，必须计入熔断避免空转
            if actual_elapsed < timeout * 0.5:
                logger.warning(
                    f"  [{tier_label}] ⚠️ 疑似连接故障（{actual_elapsed:.1f}s 即超时，配置 {timeout:.0f}s）"
                )
                _on_request_failure(f"fake_timeout_{actual_elapsed:.0f}s")
            else:
                logger.warning(
                    f"  [{tier_label}] 推理超时（{actual_elapsed:.1f}s / 配置 {timeout:.0f}s），交给下层重试"
                )
            return None

        except Exception as e:
            err_str = str(e)

            # 1) 致命错误：欠费 / 认证失败 / 模型不存在 → 立即熔断
            if is_fatal_api_error(err_str):
                logger.error(f"  [{tier_label}] ⚠️ 检测到致命 API 错误，立即停止全部任务")
                logger.error(f"  [{tier_label}] 错误详情: {e}")
                _stop_event.set()
                return None

            # 2) 速率限制：原温原档位指数退避重试（不计入失败计数）
            is_rate_limit = (
                "429" in err_str
                or "RequestBurstTooFast" in err_str
                or "Too Many Requests" in err_str
                or "rate_limit" in err_str.lower()
                or "TooManyRequests" in err_str
            )
            if is_rate_limit and attempt < RATE_LIMIT_MAX_RETRIES:
                wait = RATE_LIMIT_BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 1)
                logger.warning(
                    f"  [{tier_label}] 触发限流，等待 {wait:.1f}s 后第 {attempt+1}/{RATE_LIMIT_MAX_RETRIES} 次重试"
                )
                time.sleep(wait)
                continue

            # 3) 服务端瞬时错误（HTTP 5xx / 网络抖动）：独立退避重试，不计熔断
            # 典型场景：LM Studio 瞬时崩溃返回 500 HTML 页；ollama 连接被临时切断
            is_transient = (
                "500" in err_str
                or "502" in err_str
                or "503" in err_str
                or "504" in err_str
                or "Internal Server Error" in err_str
                or "Bad Gateway" in err_str
                or "Service Unavailable" in err_str
                or "Gateway Timeout" in err_str
                or "Connection reset" in err_str
                or "Connection aborted" in err_str
                or "Connection refused" in err_str
                or "Remote end closed" in err_str
                or "RemoteProtocolError" in err_str
                or "ConnectionError" in err_str
            )
            if is_transient and transient_attempt < TRANSIENT_MAX_RETRIES and not _stop_event.is_set():
                wait = TRANSIENT_BACKOFF_BASE * (2 ** transient_attempt) + random.uniform(0, 1)
                logger.warning(
                    f"  [{tier_label}] 服务端瞬时错误（{err_str[:80]}），等待 {wait:.1f}s 后第 {transient_attempt+1}/{TRANSIENT_MAX_RETRIES} 次重试"
                )
                time.sleep(wait)
                transient_attempt += 1
                continue

            # 4) 其他未知错误：记一次失败，达到阈值也触发熔断
            logger.error(f"  [{tier_label}] API 错误: {e}")
            _on_request_failure(err_str[:80])
            return None

    # 限流重试耗尽
    logger.error(f"  [{tier_label}] 限流重试 {RATE_LIMIT_MAX_RETRIES} 次后仍失败，放弃")
    return None

# ==========================================
# 9. JSON 解析
# ==========================================
def parse_llm_json(response_text, expected_count):
    """
    鲁棒解析 LLM 返回的 JSON 数组。
    兼容：标准数组 / JSONL / 拼接对象 / ```json``` 围栏 / 单对象 / {results:[...]} 包装。
    """
    if not response_text or not response_text.strip():
        return []

    s = response_text.strip()

    # 剥掉 markdown 围栏 ```json ... ```
    if s.startswith("```"):
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
        s = s.strip()

    parsed = None

    # 第一道：整体当作合法 JSON 解析
    try:
        obj = json.loads(s)
        if isinstance(obj, list):
            parsed = obj
        elif isinstance(obj, dict):
            for key in ("results", "data", "items", "photos"):
                v = obj.get(key)
                if isinstance(v, list):
                    parsed = v
                    break
            if parsed is None:
                parsed = [obj]
    except json.JSONDecodeError:
        pass

    # 第二道：raw_decode 流式抽取顶层对象（兼容 JSONL / 拼接）
    if parsed is None:
        decoder = json.JSONDecoder()
        results = []
        i, n = 0, len(s)
        # 允许首尾出现 [ ] 包裹 + 逗号分隔
        while i < n and s[i] in " \t\r\n[":
            i += 1
        while i < n:
            while i < n and s[i] in " \t\r\n,":
                i += 1
            if i >= n or s[i] == "]":
                break
            try:
                obj, end = decoder.raw_decode(s, i)
            except json.JSONDecodeError:
                break
            if isinstance(obj, list):
                results.extend(obj)
            elif isinstance(obj, dict):
                results.append(obj)
            i = end
        if results:
            parsed = results

    if not parsed:
        logger.error(f"  [格式错误] 未返回可解析的 JSON")
        logger.error(f"  [原始响应前 400 字符]: {response_text[:400]}")
        return []

    if not isinstance(parsed, list):
        logger.error(f"  [格式错误] 解析结果非数组")
        return []

    # 事件级 JSON 误返回兜底
    if parsed and isinstance(parsed[0], dict):
        keys = set(parsed[0].keys())
        if "photo_id" not in keys and any(
            k in keys for k in ("photos", "images", "event_name", "event_description")
        ):
            logger.error(f"  [格式错误] 模型输出事件级 JSON")
            return []

    if len(parsed) == 0:
        logger.warning(f"  [静默跳过] 模型返回空数组")
    elif len(parsed) < expected_count:
        returned_ids = [r.get("photo_id") for r in parsed if isinstance(r, dict)]
        logger.warning(f"  [漏图] 期望 {expected_count} 张，仅返回 {len(parsed)} 张，已返回: {returned_ids}")
    return parsed


# ==========================================
# 10. 应用结果到文件系统
# ==========================================
def _file_md5(path, chunk=1 << 20):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _files_identical(path_a, path_b):
    """判断两个文件内容是否一致（先比大小，再比 md5）。任一读失败视为不一致。"""
    try:
        if os.path.getsize(path_a) != os.path.getsize(path_b):
            return False
        return _file_md5(path_a) == _file_md5(path_b)
    except OSError:
        return False


def safe_copy(src, event_folder, dest_filename):
    """
    把 src 复制到 event_folder/dest_filename，处理目标同名冲突（路线 A）。
    返回 (最终路径, status)：
      - "copied"       ：正常复制到原名
      - "copied_hashed"：原名被别的文件占用，改用源路径 hash 后缀复制
      - "exists_same"  ：目标已存在且就是同一文件（断点续跑幂等）→ 跳过
      - "conflict"     ：hash 名也被不同文件占用（极端罕见）→ 未复制
    """
    dest_path = os.path.join(event_folder, dest_filename)
    if not os.path.exists(dest_path):
        shutil.copy2(src, dest_path)
        return dest_path, "copied"
    # 目标已存在：先看是不是同一文件（续跑重复归档场景）
    if _files_identical(src, dest_path):
        return dest_path, "exists_same"
    # 不同文件重名：用源文件绝对路径 hash 生成唯一后缀
    h = hashlib.md5(os.path.abspath(src).encode("utf-8")).hexdigest()[:8]
    stem, ext = os.path.splitext(dest_filename)
    alt_name = f"{stem}_{h}{ext}"
    alt_path = os.path.join(event_folder, alt_name)
    if not os.path.exists(alt_path):
        shutil.copy2(src, alt_path)
        return alt_path, "copied_hashed"
    if _files_identical(src, alt_path):
        return alt_path, "exists_same"
    return alt_path, "conflict"


def apply_results(results, files_by_id, date_str, daily_events_set):
    """
    files_by_id: {photo_id -> (file_path, dt, is_exact)}，photo_id 为批内唯一内部标识
    （批内同名照片会带 #N 后缀，见 process_batch）。落盘文件名一律取源文件真实
    basename，不带内部后缀。
    """
    for res in results:
        if not isinstance(res, dict):
            continue
        photo_id = res.get("photo_id")
        is_highlight = res.get("is_highlight")
        event_tag = res.get("event_tag")
        reason = res.get("reason", "无")

        # 记录 LLM 的原始剔除决策（在 C 档 FORCE_KEEP_ALL 覆盖之前）。
        # 手机端删除列表依据"精华与否"的原始判断：即便 C 归类档在 PC 端强制全归档，
        # LLM 判定为非精华的照片仍应在手机相册中删除，只保留精华照片。
        _llm_rejected = not bool(is_highlight)

        # C 归类档：代码强制全保留，不依赖模型是否听话输出 is_highlight=true。
        # 若模型漏给 event_tag，用当天日期兜底一个通用事件名，保证照片仍能落盘归档。
        if FORCE_KEEP_ALL:
            is_highlight = True
            if not event_tag or event_tag.strip().lower() == "null":
                event_tag = f"{date_str}-未分类"
                reason = reason if reason and reason != "无" else "归类档兜底"

        # 手机端删除列表：LLM 判定非精华即记录原始文件名 + epoch秒时间戳（去掉批内 #N 去重后缀）。
        # 记录时间戳用于手机端"文件名+时间戳"双重匹配（±12小时容差），比大小更可靠（跨年同名时间必然不同）。
        if _llm_rejected:
            _entry = files_by_id.get(photo_id)
            if _entry:
                _orig_path = _entry[0]
                _dt = _entry[1]  # datetime 对象
                _real_basename = os.path.basename(_orig_path)
                # datetime 转 epoch 秒（EXIF 只有秒级精度，不用毫秒）
                _epoch_sec = int(_dt.timestamp()) if _dt else -1
                with _deleted_photos_lock:
                    _deleted_photos_list.append((_real_basename, _epoch_sec))

        if is_highlight and event_tag and event_tag.strip().lower() != "null":

            if not event_tag.startswith(date_str):
                event_tag = f"{date_str}-{event_tag.replace(date_str, '').lstrip('-')}"
            event_tag = sanitize_event_tag(event_tag)
            daily_events_set.add(event_tag)

            entry = files_by_id.get(photo_id)
            if entry:
                original_file, _dt, is_exact_time = entry
                # 落盘用源文件真实文件名（去掉批内去重的 #N 后缀）
                real_basename = os.path.basename(original_file)
                stem, ext = os.path.splitext(real_basename)
                dest_filename = real_basename if is_exact_time else f"{stem}_NoEXIF{ext}"

                event_folder = os.path.join(TARGET_DIR, event_tag)
                with target_dir_lock:
                    os.makedirs(event_folder, exist_ok=True)
                final_path, status = safe_copy(original_file, event_folder, dest_filename)
                final_name = os.path.basename(final_path)
                if status == "copied":
                    logger.info(f"  [√] 提取 -> {final_name} | {event_tag} | {reason}")
                elif status == "copied_hashed":
                    logger.info(f"  [√] 提取(重名加后缀) -> {final_name} | {event_tag} | {reason}")
                elif status == "exists_same":
                    logger.info(f"  [~] 已存在 -> {final_name}")
                else:
                    logger.warning(f"  [!] 命名冲突未解决 -> {dest_filename}（源 {original_file}）")
        else:
            logger.info(f"  [×] 剔除 -> {photo_id} | {reason}")



def log_failed_batch(failed_files, date_str, batch_id, tier):
    with fail_log_lock:
        with open(UNPROCESSED_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n--- 失败 | 日期: {date_str} | batch_id: {batch_id} | 阶段: {tier} ---\n")
            for f_path, _, _ in failed_files:
                f.write(f"{f_path}\n")


# ==========================================
# 11. 三层重试核心
# ==========================================
def process_batch(batch_files, date_str, batch_id, system_prompt, daily_events_set):
    # 入口短路：已熔断则跳过整批，不再编码图片
    if _stop_event.is_set():
        return
    if not batch_files:
        return
    logger.info(f">>> [{batch_id}] 共 {len(batch_files)} 张")
    # 编码所有图，建立 photo_id → image / file 的映射
    images_by_id = {}
    files_by_id = {}
    ordered_ids = []
    for item in batch_files:
        # 兼容老 3 元素 batch_files（无 gps_text 字段）和新 4 元素
        if len(item) == 4:
            file_path, dt, is_exact, gps_text = item
        else:
            file_path, dt, is_exact = item
            gps_text = None
        b64 = resize_and_encode_image(file_path)
        if b64 is None:
            logger.warning(f"  [编码失败] {file_path}，记入人工清单")
            log_failed_batch([(file_path, dt, is_exact)], date_str, batch_id, "encode-fail")
            continue
        # 批内去重：不同目录的同名文件（如两个 IMG_0001.jpg）若落进同一批，
        # basename 相同会导致 dict 覆盖 + 一张图静默丢失。这里给重复 basename
        # 追加 #N 后缀，保证 photo_id 在批内唯一。传给 LLM 的编号也用这个唯一 id，
        # 返回后按唯一 id 匹配；最终落盘文件名仍取源文件真实 basename（见 apply_results）。
        pid = os.path.basename(file_path)
        if pid in files_by_id:
            stem, ext = os.path.splitext(pid)
            n = 2
            while f"{stem}#{n}{ext}" in files_by_id:
                n += 1
            pid = f"{stem}#{n}{ext}"
            logger.warning(f"  [{batch_id}] 批内同名 {os.path.basename(file_path)}，内部编号改为 {pid}")

        ordered_ids.append(pid)
        images_by_id[pid] = (pid, dt, b64, gps_text)        # gps_text 已是 resolve 后的最终值
        files_by_id[pid] = (file_path, dt, is_exact)        # 值保持 3 元素，键为批内唯一 id
    if not ordered_ids:
        return

    def get_events_str():
        return json.dumps(list(daily_events_set), ensure_ascii=False) if daily_events_set else "暂无"
    def run_tier(pending, temp, tok, timeout, label):
        """跑一轮 LLM，返回本轮成功处理的 photo_id 集合"""
        sub_imgs = [images_by_id[p] for p in pending]
        # apply_results 现在按 photo_id 字典查找，这里传本轮 pending 的子字典
        sub_files = {p: files_by_id[p] for p in pending}
        resp = call_vlm_once(sub_imgs, system_prompt, date_str, get_events_str(),
                              temp, tok, timeout, label)
        results = parse_llm_json(resp, len(sub_imgs)) if resp else []
        if not results:
            return set()
        # 只接受 photo_id 在本轮 pending 中的结果，避免幻觉
        valid_set = set(pending)
        valid_results = [r for r in results
                          if isinstance(r, dict) and r.get("photo_id") in valid_set]
        apply_results(valid_results, sub_files, date_str, daily_events_set)
        return {r["photo_id"] for r in valid_results}

    pending = list(ordered_ids)
    # ---- T1 ----
    done = run_tier(pending, TIER1_TEMP, TIER1_TOK, TIER1_TIMEOUT, f"{batch_id}-T1")
    pending = [p for p in pending if p not in done]
    if not pending:
        return
    # 熔断后不再继续 T2/T3
    if _stop_event.is_set():
        return
    # ---- T2（升温重试漏的）----
    logger.warning(f"  [{batch_id}] T1 漏 {len(pending)} 张，进入 T2 升温重试")
    done = run_tier(pending, TIER2_TEMP, TIER2_TOK, TIER2_TIMEOUT, f"{batch_id}-T2")
    pending = [p for p in pending if p not in done]
    if not pending:
        return
    if _stop_event.is_set():
        return
    # ---- T3（拆 N 张子批）----
    logger.warning(f"  [{batch_id}] T2 仍漏 {len(pending)} 张，进入 T3 拆 {TIER3_SUB_BATCH_SIZE} 张/子批")
    still_failed = []
    sub_count = 0
    for i in range(0, len(pending), TIER3_SUB_BATCH_SIZE):
        if _stop_event.is_set():
            # 熔断时直接跳出，不记录失败日志：批次会在重启时整批重跑
            break
        sub_count += 1
        sub_ids = pending[i:i + TIER3_SUB_BATCH_SIZE]
        sub_done = run_tier(sub_ids, TIER3_TEMP, TIER3_TOK, TIER3_TIMEOUT,
                             f"{batch_id}-T3-{sub_count}")
        still_failed.extend([p for p in sub_ids if p not in sub_done])
    # 仅在非熔断状态下才记录 T3 失败日志
    # 熔断状态下批次未标记完成，重启会整批重跑，此时记失败日志会导致重复归档
    if still_failed and not _stop_event.is_set():
        logger.error(f"  [{batch_id}] T3 仍失败 {len(still_failed)} 张，记录到失败日志")
        failed_files = [files_by_id[p] for p in still_failed]
        log_failed_batch(failed_files, date_str, batch_id, "T3-fail")


# ==========================================
# 12. 单线程处理一整天（带断点续跑）
# ==========================================
def process_one_date(date_str, batches_for_date, system_prompt):
    # 从 progress 中恢复事件池（重要：确保跨重启的事件名复用）
    daily_events_set = set(progress_state["daily_events"].get(date_str, set()))
    completed = progress_state["completed_batches"]

    skipped = 0
    sorted_batches = sorted(batches_for_date, key=lambda b: b['batch_id'])
    for b in sorted_batches:
        if b["batch_id"] in completed:
            skipped += 1

    logger.info(
        f"=== [线程启动] {date_str}（{len(batches_for_date)} 批，已完成 {skipped} 批，剩余 {len(batches_for_date)-skipped}）"
        f" | 事件池预载 {len(daily_events_set)} 个 ==="
    )

    for batch_info in sorted_batches:
        # 每批前检查熔断状态：触发后立即跳出，且不更新 completed_batches
        # 避免熔断中"被跳过的批次"在重启续跑时被错误标记为已完成
        if _stop_event.is_set():
            logger.warning(f"=== [线程跳出] {date_str} 因熔断中止，剩余批次留待重启续跑 ===")
            return date_str

        batch_id = batch_info["batch_id"]
        if batch_id in completed:
            continue
        batch_files = []
        for p in batch_info["photos"]:
            dt = datetime.strptime(p["datetime"], '%Y-%m-%d %H:%M:%S')
            # 使用 resolve_gps_for_prompt 决策最终传给 LLM 的地点字符串
            # 强地点（景区/POI）/ 异地弱地点 都会保留；本地弱地点（"两江新区 XX街道"）屏蔽
            gps_for_prompt = resolve_gps_for_prompt(p)
            batch_files.append((p["file"], dt, p["is_exact_time"], gps_for_prompt))
        try:
            process_batch(batch_files, date_str, batch_id, system_prompt, daily_events_set)
        except Exception as e:
            logger.error(f"  [{batch_id}] 异常: {e}", exc_info=True)

        # 熔断后这批可能没真正处理（process_batch 内部短路返回），不能标记为完成
        if _stop_event.is_set():
            logger.warning(f"=== [线程跳出] {date_str} 因熔断中止，{batch_id} 未保存为已完成 ===")
            return date_str

        # 每批完成后立即持久化进度
        with progress_lock:
            progress_state["completed_batches"].add(batch_id)
            progress_state["daily_events"][date_str] = set(daily_events_set)
        save_progress()

        # 记录本次运行的实时进度（供监控线程计算速度与 ETA）
        record_batch_done(len(batch_info.get("photos", [])))

    logger.info(f"=== [线程完成] {date_str} 完毕，事件池: {len(daily_events_set)} 个 ===")
    return date_str


# ==========================================
# 12.B by_batch 模式：单批次独立处理（同一天多批可并发）
# ------------------------------------------
# 与 process_one_date 的差异：
#   - 入参是单个 batch_info，不是一整天的批次列表
#   - daily_events_set 是批内本地副本：进入时从全局 progress_state 拷贝快照（加锁），
#     批内 LLM 调用看到的"当天事件池"即此快照（可能落后于其他线程刚加的新事件名，
#     这是 by_batch 模式事件命名一致性下降的根本原因，无法完全避免）。
#   - 批次完成后把本批新增的事件名 merge 回全局（加锁）。
#   - completed_batches.add / save_progress 复用 progress_lock。
# ==========================================
def process_one_batch_standalone(date_str, batch_info, system_prompt):
    # 入口短路：已熔断则跳过
    if _stop_event.is_set():
        return (date_str, batch_info["batch_id"])

    batch_id = batch_info["batch_id"]

    # 拷贝当天事件池快照（加锁读）
    # 注意：快照后到本批 LLM 调用结束之间，其他线程可能把新事件名 merge 进全局，
    # 本批 LLM 看不到这些新名字 -> 可能起出重名。这是 by_batch 模式的已知代价。
    with daily_events_lock:
        daily_events_set = set(progress_state["daily_events"].get(date_str, set()))

    logger.info(
        f"=== [批次线程启动] {date_str} / {batch_id}（{len(batch_info.get('photos', []))} 张）"
        f" | 事件池快照 {len(daily_events_set)} 个 ==="
    )

    batch_files = []
    for p in batch_info["photos"]:
        dt = datetime.strptime(p["datetime"], '%Y-%m-%d %H:%M:%S')
        gps_for_prompt = resolve_gps_for_prompt(p)
        batch_files.append((p["file"], dt, p["is_exact_time"], gps_for_prompt))

    try:
        process_batch(batch_files, date_str, batch_id, system_prompt, daily_events_set)
    except Exception as e:
        logger.error(f"  [{batch_id}] 异常: {e}", exc_info=True)

    # 熔断后这批可能没真正处理（process_batch 内部短路返回），不能标记为完成
    if _stop_event.is_set():
        logger.warning(f"=== [批次线程跳出] {date_str} / {batch_id} 因熔断中止，未保存为已完成 ===")
        return (date_str, batch_id)

    # 把本批新增的事件名 merge 回全局，并标记批次完成、持久化进度
    with progress_lock:
        progress_state["completed_batches"].add(batch_id)
        with daily_events_lock:
            global_events = progress_state["daily_events"].setdefault(date_str, set())
            global_events.update(daily_events_set)
    save_progress()

    record_batch_done(len(batch_info.get("photos", [])))

    logger.info(f"=== [批次线程完成] {date_str} / {batch_id}，事件池累计 {len(daily_events_set)} 个 ===")
    return (date_str, batch_id)


# ==========================================
# 13. 失败照片归档
# ==========================================
def archive_failed_photos():
    if not os.path.exists(UNPROCESSED_LOG):
        logger.info("📁 无失败照片需归档")
        return
    failed_dir = os.path.join(TARGET_DIR, FAILED_DIR_NAME)
    os.makedirs(failed_dir, exist_ok=True)
    count = 0
    seen = set()
    with open(UNPROCESSED_LOG, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("---"):
                continue
            if line in seen:
                continue
            seen.add(line)
            if os.path.exists(line):
                dest = os.path.join(failed_dir, os.path.basename(line))
                if not os.path.exists(dest):
                    try:
                        shutil.copy2(line, dest)
                        count += 1
                    except Exception as e:
                        logger.warning(f"复制失败 {line}: {e}")
    logger.info(f"📁 已归档 {count} 张失败照片到 {failed_dir}")


# ==========================================
# 14. 主流程
# ==========================================
def main():
    if not os.path.exists(TARGET_DIR):
        os.makedirs(TARGET_DIR)
    if not os.path.exists(BATCHES_FILE):
        logger.error(f"❌ 找不到 {BATCHES_FILE}")
        return
    with open(BATCHES_FILE, "r", encoding="utf-8") as f:
        batches_data = json.load(f)
    if not os.path.exists(PROMPT_FILE):
        logger.error(f"❌ 找不到 {PROMPT_FILE}")
        return
    try:
        system_prompt = _load_prompt_text(PROMPT_FILE)
    except Exception as e:
        logger.error(f"❌ 提示词解密失败：{e}")
        return

    load_progress()

    batches = batches_data.get("batches", [])
    batches_by_date = defaultdict(list)
    for b in batches:
        batches_by_date[b["date"]].append(b)

    total = len(batches)
    already_done = sum(1 for b in batches if b["batch_id"] in progress_state["completed_batches"])
    remain = total - already_done

    t1_min = TIER1_TIMEOUT / 60
    t2_min = TIER2_TIMEOUT / 60
    t3_min = TIER3_TIMEOUT / 60

    logger.info(f"========== 启动阶段二：{cfg['model']} 并发智能归档（{NUM_WORKERS} 线程，调度模式 {PARALLEL_MODE}）==========")
    logger.info(f"  [提取档位] {EXTRACTION_LEVEL} - {_LEVEL_DESC.get(EXTRACTION_LEVEL, '')}")
    logger.info(f"  [提示词] {PROMPT_FILE}")
    logger.info(
        "  ⚠️ 切换 extraction_level 后请务必 `python run_pipeline.py --fresh` 重跑，"
        "否则断点续跑会跳过已完成批次，旧结果不会按新档位重评。"
    )
    logger.info(f"  [Provider] {PROVIDER}    [Model] {cfg['model']}")
    logger.info(f"  [Input] {SOURCE_DIR}     [Output] {TARGET_DIR}")

    logger.info(f"  [日期数] {len(batches_by_date)} | [批次总数] {total} | "
                f"[已完成] {already_done} | [本次需处理] {remain}")
    logger.info(
        f"  [三层重试] T1: {TIER1_TOK//1024}K/{t1_min:.0f}min | "
        f"T2: {TIER2_TOK//1024}K升温/{t2_min:.0f}min | "
        f"T3: 拆{TIER3_SUB_BATCH_SIZE}张子批/{t3_min:.0f}min"
    )

    if remain == 0:
        logger.info("✅ 所有批次此前已处理完毕，无需重跑")
        print_token_summary()  # 无实际处理，不传性能参数
        archive_failed_photos()
        return

    logger.info(f"  [进度监控] 每 {PROGRESS_REPORT_INTERVAL}s 输出一次实时速度与 ETA")

    start = time.time()

    # 启动实时速度 / ETA 监控线程：设定本次运行的基准时间与剩余总量
    with _run_stats_lock:
        _run_stats["start_time"] = start
        _run_stats["batches_done"] = 0
        _run_stats["photos_done"] = 0
        _run_stats["total_remaining"] = remain
    _monitor_stop.clear()
    monitor_thread = threading.Thread(target=progress_monitor, name="progress-monitor", daemon=True)
    monitor_thread.start()

    try:
        if PARALLEL_MODE == "by_batch":
            # by_batch 模式：把所有未完成批次平铺进线程池，同一天多批可并发
            completed = progress_state["completed_batches"]
            pending_batches = []
            for date_str, batches_for_date in sorted(batches_by_date.items()):
                for b in batches_for_date:
                    if b["batch_id"] not in completed:
                        pending_batches.append((date_str, b))
            logger.info(
                f"  [调度] by_batch 模式：{len(pending_batches)} 批待处理，"
                f"{NUM_WORKERS} 线程并发（同一天多批可并发，事件命名一致性可能下降）"
            )
            with ThreadPoolExecutor(max_workers=NUM_WORKERS) as pool:
                futures = {
                    pool.submit(process_one_batch_standalone, date_str, b, system_prompt): (date_str, b["batch_id"])
                    for date_str, b in pending_batches
                }
                for fut in as_completed(futures):
                    date_str, batch_id = futures[fut]
                    try:
                        fut.result()
                    except Exception as e:
                        logger.error(f"  [线程异常] {date_str}/{batch_id}: {e}", exc_info=True)
        else:
            # by_date 模式（默认）：按天分线程，同一天内串行
            with ThreadPoolExecutor(max_workers=NUM_WORKERS) as pool:
                futures = {
                    pool.submit(process_one_date, date_str, batches_for_date, system_prompt): date_str
                    for date_str, batches_for_date in sorted(batches_by_date.items())
                }
                for fut in as_completed(futures):
                    date_str = futures[fut]
                    try:
                        fut.result()
                    except Exception as e:
                        logger.error(f"  [线程异常] {date_str}: {e}", exc_info=True)
    finally:
        # 停止监控线程（无论正常结束还是异常/熔断都要收尾）
        _monitor_stop.set()
        monitor_thread.join(timeout=5)

    elapsed = time.time() - start


    # 根据熔断状态打印不同结束语
    if _stop_event.is_set():
        logger.error("=" * 60)
        logger.error(f"⚠️ 因致命 API 错误中途停止（欠费 / 认证失败 / 模型不可用），本次耗时 {elapsed/60:.1f} 分钟")
        logger.error("⚠️ 进度已保存到 progress.json，修复问题后可直接重新运行续跑")
        logger.error("=" * 60)
    else:
        logger.info(f"\n✅ 全部批次处理完毕！本次耗时 {elapsed/60:.1f} 分钟（{elapsed/3600:.2f} 小时）")

    with _run_stats_lock:
        _batches_done = _run_stats["batches_done"]
        _photos_done = _run_stats["photos_done"]
    print_token_summary(elapsed=elapsed, batches_done=_batches_done, photos_done=_photos_done)
    archive_failed_photos()

    # ==========================================
    # 生成手机端删除列表文件
    # ==========================================
    # 合并 02 LLM 剔除 + 01a 去重丢弃，覆盖完整删除范围。
    # 每条记录为 (文件名, epoch秒时间戳) 元组，用于手机端"文件名+时间戳"双重匹配（±12小时容差）。
    all_deleted = list(_deleted_photos_list)
    dedup_count = 0
    
    # 读取 01a 去重记录，把去重丢弃的副本也加入删除列表
    # 需要构建 file_path -> datetime 映射
    try:
        with open(BATCHES_FILE, 'r', encoding='utf-8') as f:
            batches_data = json.load(f)
        
        # 构建 file_path -> datetime对象 的映射（从所有 batches 的 photos）
        path_to_dt = {}
        for batch in batches_data.get('batches', []):
            for photo in batch.get('photos', []):
                dt_str = photo.get('datetime')
                if dt_str:
                    try:
                        dt = datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S')
                        path_to_dt[photo['file']] = dt
                    except ValueError:
                        pass  # 格式异常，跳过
        
        deduped_records = batches_data.get('deduped', [])
        for d in deduped_records:
            fn = d.get('filename')
            if fn:
                # 从映射中查找对应的 datetime
                _dpath = d.get('file')
                _dt = path_to_dt.get(_dpath)
                _epoch_sec = int(_dt.timestamp()) if _dt else -1
                all_deleted.append((fn, _epoch_sec))
                dedup_count += 1
        if dedup_count > 0:
            logger.info(f"  [01a 去重] 额外加入 {dedup_count} 张去重丢弃的副本到删除列表")
    except Exception as e:
        logger.warning(f"⚠️ 读取 01a 去重记录失败（将跳过去重副本）: {e}")
    
    # 去重（同一 (文件名, 时间戳) 只保留一条）并按文件名排序。
    # 注意：即便 all_deleted 为空（提取率 100%），也照常生成一个只含表头的
    # 空删除列表文件，确保下游（api_pipeline_runner）始终能找到结果文件，
    # 避免"删除列表文件未生成"误报，使全精华场景被当作正常完成处理。
    unique_deleted = sorted(set(all_deleted), key=lambda x: (x[0], x[1]))

    # 文件名：profile_TARGET_DIR名_时间戳.txt
    target_basename = os.path.basename(TARGET_DIR)
    # 清理文件名中的特殊字符（保留中文/字母/数字/下划线/连字符）
    target_basename_safe = re.sub(r'[^\w\u4e00-\u9fff-]', '_', target_basename)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"non_highlight_photos_{PROFILE}_{target_basename_safe}_{timestamp}.txt"

    # 输出到 TARGET_DIR 同级目录
    output_dir = os.path.dirname(TARGET_DIR)
    deleted_list_file = os.path.join(output_dir, filename)

    try:
        with open(deleted_list_file, 'w', encoding='utf-8') as f:
            # 首行标识匹配模式，手机端据此选择匹配算法
            f.write("#MATCH_MODE=DATETIME\n")
            # 输出格式：文件名|epoch秒时间戳（每行一条，用 | 分隔）
            # 示例：IMG_0001.jpg|1754630400
            for fname, epoch_sec in unique_deleted:
                f.write(f"{fname}|{epoch_sec}\n")

        logger.info(f"\n{'='*60}")
        if unique_deleted:
            logger.info(f"📱 手机端删除列表已生成：")
            logger.info(f"   文件：{deleted_list_file}")
            logger.info(f"   格式：文件名|时间戳（EXIF 拍摄时间 epoch 秒，手机端 ±12 小时容差匹配）")
            logger.info(f"   02阶段 LLM 剔除: {len(_deleted_photos_list)} 张")
            logger.info(f"   01a阶段 去重丢弃: {dedup_count} 张")
            logger.info(f"   合并去重后总计: {len(unique_deleted)} 张")
            logger.info(f"   请通过微信文件传输助手发送到手机")
            logger.info(f"   使用手机 APP (PhotoCleaner) 导入并删除这些照片")
        else:
            logger.info(f"✅ 所有照片均为精华（提取率 100%），已生成空删除列表：")
            logger.info(f"   文件：{deleted_list_file}")
            logger.info(f"   （仅含表头，无需删除任何照片）")
        logger.info(f"{'='*60}\n")
    except Exception as e:
        logger.error(f"⚠️ 生成删除列表失败：{e}")


if __name__ == "__main__":
    main()
