# stage03b_yearly_summary.py
"""
03b 阶段：跨年事件聚合（可选后处理）

职责：
- 扫描 TARGET_DIR 下所有 "YYYY-MM-DD-*" 事件子文件夹，提取事件名 + 照片数
- 调 LLM 批量分组：把跨年/跨日的同主题事件聚类成主题组（如"生日庆祝"）
- 过滤：主题组事件数 >= min_events_per_theme 且年份跨度 >= min_year_span
- COPY（非 move）照片到独立的聚合输出目录，保留事件子文件夹结构
- 写两层 manifest：主题级 manifest.json + 全局 yearly_aggregate_manifest.json
- 原 TARGET_DIR 完全不动，聚合目录是只读视图

设计要点：
- 默认 enabled: false，不启用时本脚本 main() 立即返回，行为与之前完全一致
- 独立 progress 文件 03b_progress_<profile>.json，不依赖 02/03a 的 progress
  （03b 只读 TARGET_DIR 文件系统）
- 独立上下文指纹：target_dir / profile / provider / model / min_events_per_theme
  / min_year_span / batch_size
- 断点续跑：completed_themes / failed_themes / scan_fingerprint
  scan_fingerprint 记录上次扫描的事件集合 md5；TARGET_DIR 新增事件后指纹变化
  -> 触发全量重聚类（聚类是全局优化的，增量会破坏已有分组）
- LLM 调用：纯文本输入（事件名+照片数列表），单层重试 + 限流退避 + 致命错误熔断
- 聚类是全局单次 LLM 调用（分批），COPY 阶段可多线程并行
"""
import os
import sys
import re
import json
import time
import shutil
import hashlib
import random
import logging
import threading
from datetime import datetime
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed

from openai import OpenAI, APITimeoutError

# ==========================================
# 1. 配置加载
# ==========================================
import pipeline_config_loader as _cfg_loader

_CONFIG = _cfg_loader.load_config()
_COMMON = _CONFIG.get("common", {})
_CURATOR = _CONFIG.get("curator", {})
_YEARLY = _CONFIG.get("yearly_summary", {})
_FILES = _cfg_loader.resolve_filenames(_CONFIG)

TARGET_DIR = os.path.normpath(_COMMON.get("target_dir", r"D:\JM照片_整理输出"))
PROFILE = _cfg_loader.get_profile(_CONFIG)

YEARLY_ENABLED = bool(_YEARLY.get("enabled", False))
# provider/model 留空时复用 curator 的配置
YEARLY_PROVIDER = (_YEARLY.get("provider") or "").strip() or _CURATOR.get("provider", "ollama")

# provider_configs 复用 curator 的（与 02/03a 共享一套密钥配置）
_DEFAULT_PROVIDER_CONFIGS = {
    "ollama": {
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key": "ollama",
        "model": "hf.co/unsloth/gemma-4-31B-it-qat-GGUF:UD-Q4_K_XL",
        "json_mode": "ollama_format",
        "supports_num_predict": True,
    },
}
PROVIDER_CONFIGS = _CURATOR.get("provider_configs") or _DEFAULT_PROVIDER_CONFIGS

if YEARLY_PROVIDER not in PROVIDER_CONFIGS:
    raise RuntimeError(f"03b 阶段：provider '{YEARLY_PROVIDER}' 不在 provider_configs 中")
_cfg = PROVIDER_CONFIGS[YEARLY_PROVIDER]
# model 留空时复用 curator.provider_configs.<provider>.model
YEARLY_MODEL = (_YEARLY.get("model") or "").strip() or _cfg.get("model", "")

MIN_EVENTS_PER_THEME = int(_YEARLY.get("min_events_per_theme", 2))
MIN_YEAR_SPAN = int(_YEARLY.get("min_year_span", 1))
BATCH_SIZE = int(_YEARLY.get("batch_size", 50))
CLUSTER_WORKERS = int(_YEARLY.get("cluster_workers", 6))
COPY_WORKERS = int(_YEARLY.get("copy_workers", 3))
KEEP_MANIFEST = bool(_YEARLY.get("keep_manifest", True))

# 输出目录：留空 = <target_dir 同级>_跨年聚合
_output_dir_cfg = (_YEARLY.get("output_dir") or "").strip()
if _output_dir_cfg:
    OUTPUT_DIR = _output_dir_cfg
else:
    _td_base = os.path.basename(TARGET_DIR.rstrip("\\/"))
    _td_parent = os.path.dirname(TARGET_DIR.rstrip("\\/"))
    OUTPUT_DIR = os.path.join(_td_parent, f"{_td_base}_跨年聚合")

# 提示词加密密钥（与 stage02 / stage03a / encrypt_prompt.py 一致）
_STATIC_PROMPT_KEY = b"hfuLfxfmqclh5cbuVmVZVGbGmb-blZtvf42_YKV3_CU="

# 提示词文件路径解析（与 stage03a 逻辑一致）
_DEFAULT_PROMPT_FILE = "prompts/prompt_yearly_summary.enc"
_prompt_rel = _YEARLY.get("prompt_file") or _DEFAULT_PROMPT_FILE
if _prompt_rel.endswith(".txt"):
    _prompt_rel = _prompt_rel[:-4] + ".enc"
PROMPT_FILE = os.path.normpath(_prompt_rel) if os.path.isabs(_prompt_rel) else os.path.normpath(os.path.join(_cfg_loader.BASE_DIR, _prompt_rel))

# 第二遍主题合并提示词
_DEFAULT_MERGE_PROMPT_FILE = "prompts/prompt_yearly_summary_merge.enc"
_merge_prompt_rel = _YEARLY.get("merge_prompt_file") or _DEFAULT_MERGE_PROMPT_FILE
if _merge_prompt_rel.endswith(".txt"):
    _merge_prompt_rel = _merge_prompt_rel[:-4] + ".enc"
MERGE_PROMPT_FILE = os.path.normpath(_merge_prompt_rel) if os.path.isabs(_merge_prompt_rel) else os.path.normpath(os.path.join(_cfg_loader.BASE_DIR, _merge_prompt_rel))

# 03b 进度文件（独立于 02/03a）
PROGRESS_FILE = os.path.join(_cfg_loader.BASE_DIR, _FILES["yearly_summary_progress_file"])

# ==========================================
# 2. 重试与熔断参数（部分可经 yearly_summary 段外置）
# ==========================================
# 聚类输出比日合并大，默认 max_tokens 更大
LLM_TIMEOUT = float(_YEARLY.get("llm_timeout", 300.0))
LLM_MAX_TOKENS = int(_YEARLY.get("llm_max_tokens", 4096))
LLM_MAX_TOKENS_RETRY = int(_YEARLY.get("llm_max_tokens_retry", 16384))
LLM_TEMPERATURE = float(_YEARLY.get("llm_temperature", 0.0))
TIMEOUT_MAX_RETRIES = int(_YEARLY.get("timeout_max_retries", 1))
TIMEOUT_RETRY_TEMP = float(_YEARLY.get("timeout_retry_temperature", 0.1))
# 截断重试升温：qwen3 在 temperature=0 贪心解码下偶发 thinking 死循环（reasoning 重复同一串直到耗尽 token），
# 单纯加大 max_tokens 无法解锁，必须升温打破决定性。0.2 为保守值，对分析质量影响极小。
TRUNCATION_RETRY_TEMP = float(_YEARLY.get("truncation_retry_temperature", 0.2))

RATE_LIMIT_MAX_RETRIES = 4
RATE_LIMIT_BACKOFF_BASE = 2.0
# 服务端瞬时错误（HTTP 5xx / 网络抖动）退避重试：独立于 429 限流计数
TRANSIENT_MAX_RETRIES = 3
TRANSIENT_BACKOFF_BASE = 3.0

CONSECUTIVE_FAILURE_LIMIT = 5

# ==========================================
# 3. 全局状态
# ==========================================
progress_lock = threading.Lock()
output_dir_lock = threading.Lock()  # 保护 OUTPUT_DIR 下的 makedirs / 文件操作
token_stats_lock = threading.Lock()

_consecutive_failures = 0
_failure_lock = threading.Lock()
_stop_event = threading.Event()

token_stats = {
    "total_input": 0, "total_output": 0, "total_calls": 0,
}

# 进度状态：main 里加载/保存
# - completed_themes: 已成功 COPY 完的主题文件夹名集合
# - failed_themes: COPY 失败的主题文件夹名集合
# - scan_fingerprint: 上次扫描的事件集合 md5（变化则全量重聚类）
progress_state = {
    "completed_themes": set(),
    "failed_themes": set(),
    "scan_fingerprint": "",
}

client = OpenAI(
    base_url=_cfg["base_url"],
    api_key=_cfg["api_key"],
    timeout=300.0,
    max_retries=0,
)

# ==========================================
# 4. 日志
# ==========================================
os.makedirs(os.path.join(_cfg_loader.BASE_DIR, "logs"), exist_ok=True)
log_filename = os.path.join(
    _cfg_loader.BASE_DIR, "logs",
    f"03b_yearly_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
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
logging.getLogger("httpx").setLevel(logging.WARNING)

# ==========================================
# 5. 上下文指纹（独立于 02/03a）
# ==========================================
# 03b 只读 TARGET_DIR 文件系统 + 写独立聚合目录，不依赖 source_dir / batches json。
# 切换 provider/model/min_events_per_theme/min_year_span/batch_size/profile/target_dir
# 都触发自愈。
_CONTEXT_FINGERPRINT_FIELDS = ("target_dir", "profile", "yearly_summary_provider",
                                "yearly_summary_model", "min_events_per_theme",
                                "min_year_span", "batch_size")


def _current_context_fingerprint() -> dict:
    return {
        "target_dir": TARGET_DIR,
        "profile": PROFILE,
        "yearly_summary_provider": YEARLY_PROVIDER,
        "yearly_summary_model": YEARLY_MODEL,
        "min_events_per_theme": MIN_EVENTS_PER_THEME,
        "min_year_span": MIN_YEAR_SPAN,
        "batch_size": BATCH_SIZE,
    }


def _archive_mismatched_progress(mismatched_fields):
    """上下文指纹不匹配时归档旧进度文件 + 清空 progress_state"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_dir = os.path.join(_cfg_loader.BASE_DIR, "_pipeline_archive", "mismatched")
    os.makedirs(archive_dir, exist_ok=True)
    base_name = os.path.basename(PROGRESS_FILE)
    archived_name = f"{base_name[:-5]}_mismatched_{stamp}.json"
    archived_path = os.path.join(archive_dir, archived_name)
    try:
        shutil.move(PROGRESS_FILE, archived_path)
    except Exception as e:
        logger.warning(f"  ⚠️ 旧 03b 进度文件归档失败：{e}（将直接覆盖）")

    logger.warning("=" * 70)
    logger.warning("⚠️ 检测到 03b 进度文件的上下文与当前运行环境不匹配，已重置进度从头跑。")
    logger.warning("   旧进度文件已归档（不删除）：")
    logger.warning(f"     {PROGRESS_FILE}")
    logger.warning(f"   -> {archived_path}")
    for field, (old_val, new_val) in mismatched_fields.items():
        logger.warning(f"   - {field}: {old_val!r} -> {new_val!r}")
    logger.warning("   如需保持旧进度，请改回上述配置后重跑；否则继续从头聚合。")
    logger.warning("=" * 70)

    progress_state["completed_themes"] = set()
    progress_state["failed_themes"] = set()
    progress_state["scan_fingerprint"] = ""


def load_progress():
    if not os.path.exists(PROGRESS_FILE):
        return
    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"03b 进度文件解析失败，将从头开始：{e}")
        return

    stored_fp = data.get("context_fingerprint")
    current_fp = _current_context_fingerprint()
    mismatched = {}
    if not isinstance(stored_fp, dict):
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
        return

    progress_state["completed_themes"] = set(data.get("completed_themes", []))
    progress_state["failed_themes"] = set(data.get("failed_themes", []))
    progress_state["scan_fingerprint"] = data.get("scan_fingerprint", "")
    ts = data.get("token_stats")
    if ts:
        token_stats["total_input"] = ts.get("total_input", 0)
        token_stats["total_output"] = ts.get("total_output", 0)
        token_stats["total_calls"] = ts.get("total_calls", 0)
    logger.info(
        f"📥 加载 03b 进度：已完成 {len(progress_state['completed_themes'])} 个主题，"
        f"已失败 {len(progress_state['failed_themes'])} 个主题"
    )


def save_progress():
    """线程安全的进度持久化（原子写）"""
    with progress_lock:
        data = {
            "completed_themes": sorted(progress_state["completed_themes"]),
            "failed_themes": sorted(progress_state["failed_themes"]),
            "scan_fingerprint": progress_state["scan_fingerprint"],
            "token_stats": token_stats,
            "context_fingerprint": _current_context_fingerprint(),
        }
        tmp = PROGRESS_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, PROGRESS_FILE)
        except Exception as e:
            logger.error(f"03b 进度保存失败：{e}")


# ==========================================
# 6. 实时速度与 ETA 监控（COPY 阶段用，与 03a 同样逻辑）
# ==========================================
PROGRESS_REPORT_INTERVAL = int(_YEARLY.get("progress_report_interval", 30))
PROGRESS_WINDOW_SECONDS = max(PROGRESS_REPORT_INTERVAL * 5, 300)

_run_stats_lock = threading.Lock()
_run_stats = {
    "start_time": None,
    "themes_done": 0,
    "total_remaining": 0,
    "events": deque(),
}
_monitor_stop = threading.Event()


def record_theme_done():
    now = time.time()
    with _run_stats_lock:
        _run_stats["themes_done"] += 1
        _run_stats["events"].append(now)


def _fmt_duration(seconds):
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
    while not _monitor_stop.wait(PROGRESS_REPORT_INTERVAL):
        now = time.time()
        with _run_stats_lock:
            themes_done = _run_stats["themes_done"]
            total_remaining = _run_stats["total_remaining"]
            start_time = _run_stats["start_time"] or now

            events = _run_stats["events"]
            cutoff = now - PROGRESS_WINDOW_SECONDS
            while events and events[0] < cutoff:
                events.popleft()
            win_events = list(events)

        window_start = max(start_time, now - PROGRESS_WINDOW_SECONDS)
        window_dt = max(now - window_start, 1e-6)
        win_rate = len(win_events) / window_dt * 60.0

        total_dt = max(now - start_time, 1e-6)
        avg_rate = themes_done / total_dt * 60.0

        remaining = max(total_remaining - themes_done, 0)

        if themes_done == 0 or avg_rate <= 0:
            logger.info(
                f"⏱️ [进度] 已处理 {themes_done}/{total_remaining} 主题 | "
                f"ETA 计算中（样本不足）"
            )
        else:
            eta_seconds = remaining / (avg_rate / 60.0)
            finish_at = datetime.fromtimestamp(now + eta_seconds).strftime("%m-%d %H:%M:%S")
            pct = themes_done / total_remaining * 100 if total_remaining else 0
            logger.info(
                f"⏱️ [进度] {themes_done}/{total_remaining} 主题 ({pct:.1f}%) | "
                f"实时 {win_rate:.1f} 主题/分 | "
                f"平均 {avg_rate:.1f} 主题/分 | "
                f"剩余 {remaining} 主题 | ETA {_fmt_duration(eta_seconds)}（预计 {finish_at} 完成）"
            )


# ==========================================
# 7. Token 统计
# ==========================================
def update_token_stats(usage):
    if not usage:
        return
    prompt_t = getattr(usage, "prompt_tokens", 0) or 0
    completion_t = getattr(usage, "completion_tokens", 0) or 0
    with token_stats_lock:
        token_stats["total_input"] += prompt_t
        token_stats["total_output"] += completion_t
        token_stats["total_calls"] += 1


def print_token_summary():
    logger.info("=" * 60)
    logger.info(f"📊 03b Token 消耗汇总（模型：{YEARLY_MODEL}，Provider：{YEARLY_PROVIDER}）")
    logger.info(f"   总调用次数:     {token_stats['total_calls']:>8,}")
    logger.info(f"   总输入:         {token_stats['total_input']:>8,}")
    logger.info(f"   总输出:         {token_stats['total_output']:>8,}")
    grand_total = token_stats["total_input"] + token_stats["total_output"]
    logger.info(f"   总 token:       {grand_total:>8,}")
    logger.info("=" * 60)


# ==========================================
# 8. 提示词加载
# ==========================================
def _load_prompt_text(enc_path):
    from cryptography.fernet import Fernet
    fernet = Fernet(_STATIC_PROMPT_KEY)
    with open(enc_path, "rb") as f:
        ciphertext = f.read()
    plaintext = fernet.decrypt(ciphertext)
    return plaintext.decode("utf-8")


# ==========================================
# 9. 工具函数
# ==========================================
_DATE_EVENT_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})-(.+)$')


def scan_target_dir(target_dir):
    """
    扫描 TARGET_DIR 下所有 "YYYY-MM-DD-*" 事件子文件夹，返回事件列表（扁平）。

    支持两种目录布局：
    1. 扁平：TARGET_DIR/YYYY-MM-DD-事件名/（02 直接输出）
    2. 年份分组：TARGET_DIR/<年份分组目录>/YYYY-MM-DD-事件名/
       （如 TARGET_DIR/XY照片_整理输出_2020/2020-01-01-xxx，用户手工按年归档后常见）

    先扫顶层，若无 YYYY-MM-DD-* 命名目录，则对每个子目录再扫一层。
    递归更深层级不扫（避免误收 _pipeline_archive 等无关目录）。

    返回 [{"event_tag": str, "date": str, "year": str, "event_name": str,
           "folder": abs_path, "photos": [abs_paths], "photo_count": int,
           "parent_group": str 或 None}, ...]
    event_tag 冲突时（不同分组下同名事件）加 parent_group 前缀消歧。
    """
    events = []
    if not os.path.isdir(target_dir):
        return events

    def _collect_event(name, full, parent_group):
        """解析单个事件目录，返回 event dict 或 None（不符合命名）"""
        m = _DATE_EVENT_RE.match(name)
        if not m:
            return None
        year_str = m.group(1)
        date_str = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        event_name = m.group(4)
        photos = []
        for root, dirs, files in os.walk(full):
            for fn in files:
                if fn.lower().endswith(('.jpg', '.jpeg', '.png')):
                    photos.append(os.path.join(root, fn))
        photos.sort()
        return {
            "event_tag": name,
            "date": date_str,
            "year": year_str,
            "event_name": event_name,
            "folder": full,
            "photos": photos,
            "photo_count": len(photos),
            "parent_group": parent_group,
        }

    seen_tags = set()

    # 第一遍：扫顶层 YYYY-MM-DD-* 目录
    for name in sorted(os.listdir(target_dir)):
        full = os.path.join(target_dir, name)
        if not os.path.isdir(full):
            continue
        ev = _collect_event(name, full, parent_group=None)
        if ev is not None:
            events.append(ev)
            seen_tags.add(ev["event_tag"])

    # 第二遍：顶层无事件目录（或顶层目录不是 YYYY-MM-DD-*）时，往下一级扫
    if not events:
        for name in sorted(os.listdir(target_dir)):
            full = os.path.join(target_dir, name)
            if not os.path.isdir(full):
                continue
            # 跳过明显无关的目录（归档/日志/隐藏目录等）
            if name.startswith('.') or name.startswith('_'):
                continue
            for sub_name in sorted(os.listdir(full)):
                sub_full = os.path.join(full, sub_name)
                if not os.path.isdir(sub_full):
                    continue
                ev = _collect_event(sub_name, sub_full, parent_group=name)
                if ev is not None:
                    # event_tag 冲突消歧：不同年份分组下可能有同名事件
                    if ev["event_tag"] in seen_tags:
                        ev["event_tag"] = f"{name}_{ev['event_tag']}"
                    events.append(ev)
                    seen_tags.add(ev["event_tag"])

    return events


def compute_scan_fingerprint(events):
    """计算事件集合的 md5 指纹。事件新增/删除/改名都会导致指纹变化。"""
    h = hashlib.md5()
    for e in sorted(events, key=lambda x: x["event_tag"]):
        h.update(f"{e['event_tag']}\x00{e['photo_count']}\x00".encode("utf-8"))
    return h.hexdigest()


def sanitize_event_tag(event_tag):
    """与 stage02/03a 同样的非法字符清理"""
    if not event_tag:
        return event_tag
    illegal = ['/', '\\', ':', '*', '?', '"', '<', '>', '|']
    cleaned = event_tag
    for ch in illegal:
        cleaned = cleaned.replace(ch, '、')
    cleaned = re.sub(r'\s+', '', cleaned)
    return cleaned[:100]


def is_fatal_api_error(err_str):
    """与 stage02/03a 一致的致命错误识别"""
    s = err_str.lower()
    fatal_patterns = [
        "insufficient_user_quota", "insufficient_quota", "insufficient balance",
        "account balance", "insufficientbalance", "balance not enough",
        "balance is insufficient", "exceeded_current_quota",
        "余额不足", "欠费", "arrears", "billing", "payment required",
        "invalid_api_key", "invalid api key", "incorrect api key",
        "unauthorized", "authentication_error",
        "model not found", "model_not_found",
        "is not supported by this model",
        " 401 ", " 402 ", " 403 ",
    ]
    return any(p in s for p in fatal_patterns)


def _on_request_success():
    global _consecutive_failures
    with _failure_lock:
        _consecutive_failures = 0


def _on_request_failure(reason):
    global _consecutive_failures
    with _failure_lock:
        _consecutive_failures += 1
        if _consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
            logger.error(
                f"  [熔断] 连续 {_consecutive_failures} 次失败 ({reason})，立即停止后续调用"
            )
            _stop_event.set()
            return True
    return False


# ==========================================
# 10. LLM 调用（单层重试 + 限流退避）
# ==========================================
def build_request_kwargs(messages, max_tokens_override=None, temperature_override=None):
    """根据 provider 配置生成请求参数。
    max_tokens_override 不为 None 时，覆盖默认的 LLM_MAX_TOKENS（用于截断重试）。
    temperature_override 不为 None 时，覆盖默认的 LLM_TEMPERATURE（用于超时重试升温）。
    """
    max_tokens = max_tokens_override if max_tokens_override is not None else LLM_MAX_TOKENS
    temperature = temperature_override if temperature_override is not None else LLM_TEMPERATURE
    kwargs = {
        "model": YEARLY_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout": LLM_TIMEOUT,
    }
    json_mode = _cfg.get("json_mode")
    if json_mode == "openai_response_format":
        kwargs["response_format"] = {"type": "json_object"}
    elif json_mode == "ollama_format":
        extra = {"format": "json"}
        if _cfg.get("supports_num_predict"):
            extra["options"] = {"num_predict": max_tokens}
        kwargs["extra_body"] = extra
    return kwargs


def call_llm_for_clustering(events, system_prompt):
    """
    调 LLM 把事件列表按主题分组（批量，多批并行）。

    输入（纯文本）：事件名列表 + 照片数，每行带编号（1-N），分批（每批 BATCH_SIZE 个）。
    输出 JSON: [{"theme": "生日庆祝", "events": [1, 15, 42]}, ...]
    （events 用编号，非完整 event_tag，大幅减少输出 token）

    多批并行执行（CLUSTER_WORKERS 线程），多批结果合并：同名 theme 自动合并。

    返回 [{"theme": str, "events": [event_tag, ...]}, ...] 或 None（失败）。
    """
    if _stop_event.is_set():
        return None

    total = len(events)
    n_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE

    # 预切分批次，每批构建 idx_to_tag 映射
    batches = []
    for batch_idx in range(n_batches):
        start = batch_idx * BATCH_SIZE
        end = min(start + BATCH_SIZE, total)
        batch = events[start:end]
        # 编号 1-N 对应 batch 内事件；idx_to_tag 把编号转回 event_tag
        idx_to_tag = {i + 1: e["event_tag"] for i, e in enumerate(batch)}
        batches.append((batch_idx, batch, idx_to_tag))

    logger.info(
        f"  [聚类] {n_batches} 批，{CLUSTER_WORKERS} 线程并行"
        f"（每批 {BATCH_SIZE} 个事件）..."
    )

    all_groups = defaultdict(list)  # theme -> [event_tag, ...]
    completed_batches = 0
    cluster_start = time.time()

    def _process_batch(batch_idx, batch, idx_to_tag):
        """处理单个批次：调 LLM + 解析，返回 batch_groups 或 None"""
        if _stop_event.is_set():
            return None

        events_text = "\n".join(
            f"{i+1}. {e['event_tag']}（{e['photo_count']} 张）"
            for i, e in enumerate(batch)
        )
        user_text = (
            f"【事件列表】（第 {batch_idx+1}/{n_batches} 批，共 {len(batch)} 个事件）：\n"
            f"{events_text}\n\n"
            f"请把这些事件按主题分组，输出 JSON。"
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]
        logger.info(f"  [批次 {batch_idx+1}/{n_batches}] 调 LLM 分组（{len(batch)} 个事件）...")

        result = _invoke_llm_with_retry_loop(messages, max_tokens_override=None,
                                             batch_label=f"批次{batch_idx+1}")
        if result is None:
            return None
        response, response_text, truncated = result

        # 截断检测 -> 用更大 max_tokens 重试一次
        if truncated and not _stop_event.is_set():
            completion_tokens = getattr(response.usage, "completion_tokens", 0) if response.usage else 0
            logger.warning(
                f"  [批次 {batch_idx+1}] LLM 输出被截断（completion_tokens={completion_tokens}），"
                f"用 max_tokens={LLM_MAX_TOKENS_RETRY}, 温度={TRUNCATION_RETRY_TEMP} 重试一次"
            )
            retry_result = _invoke_llm_with_retry_loop(
                messages, max_tokens_override=LLM_MAX_TOKENS_RETRY,
                batch_label=f"批次{batch_idx+1}",
                temperature_override=TRUNCATION_RETRY_TEMP,
            )
            if retry_result is None:
                return None
            response, response_text, truncated = retry_result
            if truncated:
                completion_tokens = getattr(response.usage, "completion_tokens", 0) if response.usage else 0
                logger.error(
                    f"  [批次 {batch_idx+1}] 重试后仍被截断（completion_tokens={completion_tokens}），放弃"
                )
                return None

        return _parse_clustering_decision(response_text, batch_idx + 1, idx_to_tag)

    # 多批并行
    with ThreadPoolExecutor(max_workers=CLUSTER_WORKERS) as pool:
        futures = {
            pool.submit(_process_batch, bidx, batch, idx_map): bidx
            for bidx, batch, idx_map in batches
        }
        for fut in as_completed(futures):
            bidx = futures[fut]
            if _stop_event.is_set():
                # 取消未开始的，已跑的让它自然返回
                break
            try:
                batch_groups = fut.result()
            except Exception as e:
                logger.error(f"  [批次 {bidx+1}] 线程异常: {e}", exc_info=True)
                return None
            if batch_groups is None:
                return None
            completed_batches += 1
            # 合并到全局 groups
            for g in batch_groups:
                theme = g["theme"]
                all_groups[theme].extend(g["events"])
            # 中间保存 token 统计（长任务防丢）
            save_progress()
            # 输出聚类进度（GUI 解析 [进度] x/N 聚类批 行驱动进度条）
            # 用"聚类批"而非"批"作为单位，避免与 02 阶段的"批"进度行混淆
            elapsed = time.time() - cluster_start
            pct = completed_batches / n_batches * 100 if n_batches else 0
            avg_rate = completed_batches / max(elapsed, 1e-6) * 60.0
            remaining = n_batches - completed_batches
            eta_seconds = remaining / (avg_rate / 60.0) if avg_rate > 0 else 0
            finish_at = datetime.fromtimestamp(time.time() + eta_seconds).strftime("%m-%d %H:%M:%S") if eta_seconds > 0 else "未知"
            logger.info(
                f"⏱️ [进度] {completed_batches}/{n_batches} 聚类批 ({pct:.1f}%) | "
                f"平均 {avg_rate:.1f} 批/分 | "
                f"剩余 {remaining} 批 | ETA {_fmt_duration(eta_seconds)}（预计 {finish_at} 完成）"
            )
            logger.info(f"  [批次 {bidx+1} 完成] 已完成 {completed_batches}/{n_batches} 批")

    if _stop_event.is_set():
        return None

    # 转换为列表
    result_groups = [
        {"theme": theme, "events": sorted(set(evts))}  # 去重（同名 theme 跨批可能有重复）
        for theme, evts in all_groups.items()
    ]
    result_groups.sort(key=lambda g: -len(g["events"]))  # 按事件数降序
    return result_groups


def _invoke_llm_with_retry_loop(messages, max_tokens_override=None, batch_label="", temperature_override=None):
    """
    单次 LLM 调用（含限流重试 + 真推理超时重试），返回 (response, response_text, truncated) 或 None。
    """
    kwargs = build_request_kwargs(
        messages,
        max_tokens_override=max_tokens_override,
        temperature_override=temperature_override,
    )
    timeout_attempt = 0
    transient_attempt = 0

    for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
        if _stop_event.is_set():
            return None
        start_time = time.time()
        try:
            response = client.chat.completions.create(**kwargs)
            usage = response.usage
            response_text = response.choices[0].message.content
            finish_reason = getattr(response.choices[0], "finish_reason", None)
            _on_request_success()
            update_token_stats(usage)
            if usage:
                logger.info(
                    f"  [{batch_label or 'LLM'}] Token 输入 {usage.prompt_tokens}, 输出 {usage.completion_tokens}"
                )
            completion_tokens = getattr(usage, "completion_tokens", 0) if usage else 0
            is_truncated = (finish_reason == "length") or (
                (not response_text or not response_text.strip()) and completion_tokens > 0
            )
            if not response_text or not response_text.strip():
                if not is_truncated:
                    logger.warning(f"  [{batch_label or 'LLM'}] LLM 空返回")
                    return None
                return response, response_text, True
            if _stop_event.is_set():
                return None
            return response, response_text, is_truncated
        except APITimeoutError:
            actual_elapsed = time.time() - start_time
            if actual_elapsed < LLM_TIMEOUT * 0.5:
                logger.warning(
                    f"  [{batch_label or 'LLM'}] ⚠️ 疑似连接故障（{actual_elapsed:.1f}s 即超时，配置 {LLM_TIMEOUT:.0f}s）"
                )
                _on_request_failure(f"fake_timeout_{actual_elapsed:.0f}s")
                return None
            if timeout_attempt < TIMEOUT_MAX_RETRIES and not _stop_event.is_set():
                logger.warning(
                    f"  [{batch_label or 'LLM'}] 推理超时（{actual_elapsed:.1f}s / 配置 {LLM_TIMEOUT:.0f}s），"
                    f"第 {timeout_attempt + 1}/{TIMEOUT_MAX_RETRIES} 次重试（升温至 {TIMEOUT_RETRY_TEMP}）"
                )
                timeout_attempt += 1
                # 若调用方已指定升温（如截断重试），沿用之；否则用超时默认升温
                kwargs = build_request_kwargs(
                    messages,
                    max_tokens_override=max_tokens_override,
                    temperature_override=temperature_override if temperature_override is not None else TIMEOUT_RETRY_TEMP,
                )
                continue
            logger.warning(
                f"  [{batch_label or 'LLM'}] 推理超时重试耗尽（{actual_elapsed:.1f}s / 配置 {LLM_TIMEOUT:.0f}s），放弃"
            )
            _on_request_failure("timeout")
            return None
        except Exception as e:
            err_str = str(e)
            if is_fatal_api_error(err_str):
                logger.error(f"  [{batch_label or 'LLM'}] ⚠️ 致命 API 错误，立即停止：{e}")
                _stop_event.set()
                return None
            is_rate_limit = (
                "429" in err_str or "Too Many Requests" in err_str
                or "rate_limit" in err_str.lower() or "TooManyRequests" in err_str
            )
            if is_rate_limit and attempt < RATE_LIMIT_MAX_RETRIES:
                wait = RATE_LIMIT_BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 1)
                logger.warning(
                    f"  [{batch_label or 'LLM'}] 触发限流，等待 {wait:.1f}s 后第 {attempt+1}/{RATE_LIMIT_MAX_RETRIES} 次重试"
                )
                time.sleep(wait)
                continue
            # 服务端瞬时错误（HTTP 5xx / 网络抖动）：独立退避重试，不计熔断
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
                    f"  [{batch_label or 'LLM'}] 服务端瞬时错误（{err_str[:80]}），等待 {wait:.1f}s 后第 {transient_attempt+1}/{TRANSIENT_MAX_RETRIES} 次重试"
                )
                time.sleep(wait)
                transient_attempt += 1
                continue
            logger.error(f"  [{batch_label or 'LLM'}] LLM 错误: {e}")
            _on_request_failure(err_str[:80])
            return None

    logger.error(f"  [{batch_label or 'LLM'}] 限流重试耗尽，放弃")
    return None


def _parse_clustering_decision(response_text, batch_num, idx_to_tag=None):
    """解析 LLM 返回的 JSON 聚类结果。

    期望格式（编号化）: [{"theme": "生日庆祝", "events": [1, 15, 42]}, ...]
    向后兼容旧格式: [{"theme": "生日庆祝", "events": ["2023-05-10-小明生日", ...]}, ...]

    idx_to_tag: 批次内编号(1-N) -> event_tag 映射，用于把 int 编号转回 event_tag。
                为 None 时按旧格式处理（events 必须是 str）。
    """
    s = response_text.strip()
    # 剥 markdown 围栏
    if s.startswith("```"):
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
        s = s.strip()
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        logger.error(f"  [批次 {batch_num}] LLM 返回非 JSON: {response_text[:200]}")
        return None
    if not isinstance(obj, list):
        # 兼容 {"groups": [...]} 包装
        if isinstance(obj, dict) and isinstance(obj.get("groups"), list):
            obj = obj["groups"]
        else:
            logger.error(f"  [批次 {batch_num}] LLM 返回非数组: {str(obj)[:200]}")
            return None
    groups = []
    for item in obj:
        if not isinstance(item, dict):
            continue
        theme = str(item.get("theme", "") or "").strip()
        evts_raw = item.get("events", [])
        if not isinstance(evts_raw, list):
            continue
        evts = []
        for e in evts_raw:
            if isinstance(e, int) and idx_to_tag is not None:
                # 编号化输出：int -> event_tag
                tag = idx_to_tag.get(e)
                if tag:
                    evts.append(tag)
                else:
                    logger.warning(f"  [批次 {batch_num}] LLM 输出编号 {e} 超出范围（1-{len(idx_to_tag)}），跳过")
            elif isinstance(e, str) and e.strip():
                # 旧格式/兼容：直接用 str
                evts.append(e.strip())
            elif isinstance(e, int) and idx_to_tag is None:
                # 编号但无映射（不应发生）：转 str 兜底
                evts.append(str(e))
        if theme and evts:
            groups.append({"theme": theme, "events": evts})
    if not groups:
        logger.warning(f"  [批次 {batch_num}] LLM 返回空聚类结果")
    return groups


# ==========================================
# 10b. 第二遍：主题合并（跨批次语义相同的主题名合并）
# ==========================================
def call_llm_for_merge_themes(groups, merge_prompt):
    """
    第二遍聚类：把第一遍产出的主题名列表丢给 LLM，让它合并语义相同的主题。

    输入：主题名 + 事件数列表（纯文本，全量一次调用）。
    输出 JSON: [{"merged_name": "数码硬件DIY维护", "themes": ["硬件设备维护", ...]}, ...]

    返回 merge_mapping: {original_theme_name: merged_name} 或 None（失败/无需合并）。
    """
    if _stop_event.is_set():
        return None
    if len(groups) < 2:
        logger.info(f"  [主题合并] 仅 {len(groups)} 个主题，跳过第二遍合并")
        return None

    # 构造主题名 + 事件数列表
    themes_text = "\n".join(
        f"{i+1}. {g['theme']}（{len(g['events'])} 个事件）"
        for i, g in enumerate(groups)
    )
    user_text = (
        f"【主题列表】（共 {len(groups)} 个主题）：\n"
        f"{themes_text}\n\n"
        f"请把语义相同的主题合并，输出 JSON。"
    )
    messages = [
        {"role": "system", "content": merge_prompt},
        {"role": "user", "content": user_text},
    ]
    logger.info(f"  [主题合并] 调 LLM 合并 {len(groups)} 个主题名...")

    result = _invoke_llm_with_retry_loop(messages, max_tokens_override=None,
                                         batch_label="主题合并")
    if result is None:
        return None
    response, response_text, truncated = result

    if truncated and not _stop_event.is_set():
        completion_tokens = getattr(response.usage, "completion_tokens", 0) if response.usage else 0
        logger.warning(
            f"  [主题合并] LLM 输出被截断（completion_tokens={completion_tokens}），"
            f"用 max_tokens={LLM_MAX_TOKENS_RETRY}, 温度={TRUNCATION_RETRY_TEMP} 重试一次"
        )
        retry_result = _invoke_llm_with_retry_loop(
            messages, max_tokens_override=LLM_MAX_TOKENS_RETRY,
            batch_label="主题合并",
            temperature_override=TRUNCATION_RETRY_TEMP,
        )
        if retry_result is None:
            return None
        response, response_text, truncated = retry_result

    return _parse_merge_themes_decision(response_text, groups)


def _parse_merge_themes_decision(response_text, groups):
    """解析第二遍 LLM 返回的主题合并 JSON。
    期望格式: [{"merged_name": "数码硬件DIY维护", "themes": ["硬件设备维护", ...]}, ...]
    返回 merge_mapping: {original_theme: merged_name} 或 None。
    """
    s = response_text.strip()
    if s.startswith("```"):
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
        s = s.strip()
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        logger.error(f"  [主题合并] LLM 返回非 JSON: {response_text[:200]}")
        return None
    if not isinstance(obj, list):
        if isinstance(obj, dict) and isinstance(obj.get("merges"), list):
            obj = obj["merges"]
        else:
            logger.error(f"  [主题合并] LLM 返回非数组: {str(obj)[:200]}")
            return None

    # 已知主题名集合（用于校验 LLM 输出的 themes 是否合法）
    known_themes = {g["theme"] for g in groups}
    merge_mapping = {}
    merged_count = 0
    for item in obj:
        if not isinstance(item, dict):
            continue
        merged_name = str(item.get("merged_name", "") or "").strip()
        themes_to_merge = item.get("themes", [])
        if not isinstance(themes_to_merge, list):
            continue
        # 只保留已知且 >=2 个的主题名
        valid_themes = [
            str(t).strip() for t in themes_to_merge
            if str(t).strip() in known_themes
        ]
        if merged_name and len(valid_themes) >= 2:
            for t in valid_themes:
                merge_mapping[t] = merged_name
            merged_count += 1

    if not merge_mapping:
        logger.info(f"  [主题合并] LLM 未返回有效合并（{len(obj)} 项，无 >=2 个已知主题的合并组）")
        return None
    logger.info(f"  [主题合并] LLM 合并 {merged_count} 组主题，涉及 {len(merge_mapping)} 个原始主题名")
    return merge_mapping


def apply_theme_merge(groups, merge_mapping):
    """
    按第二遍合并映射，把多个主题组的事件合并到统一 merged_name 下。

    groups: 第一遍聚类结果 [{"theme": str, "events": [tag, ...]}, ...]
    merge_mapping: {original_theme: merged_name}

    返回合并后的 groups（同名 theme 的事件已合并，去重）。
    """
    if not merge_mapping:
        return groups

    merged = defaultdict(set)  # final_theme -> set(event_tag)
    for g in groups:
        original = g["theme"]
        final = merge_mapping.get(original, original)  # 未在映射里的保持原名
        merged[final].update(g["events"])

    result = [
        {"theme": theme, "events": sorted(evts)}
        for theme, evts in merged.items()
    ]
    result.sort(key=lambda g: -len(g["events"]))
    return result


# ==========================================
# 11. 主题过滤与 COPY 执行
# ==========================================
def _theme_folder_name(theme, events):
    """生成主题文件夹名：<起始年>-<结束年>-<主题名>"""
    years = sorted({e["year"] for e in events})
    year_start = years[0]
    year_end = years[-1]
    if year_start == year_end:
        year_range = year_start
    else:
        year_range = f"{year_start}-{year_end}"
    return sanitize_event_tag(f"{year_range}-{theme}")


def _unique_theme_folder(theme_folder):
    """生成不冲突的主题文件夹路径。若已存在则加 _v2 / _v3 ... 后缀"""
    base = os.path.join(OUTPUT_DIR, theme_folder)
    if not os.path.exists(base):
        return base
    n = 2
    while True:
        candidate = f"{base}_v{n}"
        if not os.path.exists(candidate):
            return candidate
        n += 1


def _copy_photo_with_conflict(src, dest_folder):
    """
    把 src copy 到 dest_folder/basename(src)。同名冲突时加 _<hash8> 后缀。
    返回最终路径。
    """
    basename = os.path.basename(src)
    dest = os.path.join(dest_folder, basename)
    if not os.path.exists(dest):
        shutil.copy2(src, dest)
        return dest
    # 同名冲突（不同事件子文件夹有同名照片）：加源路径 hash 后缀
    h = hashlib.md5(os.path.abspath(src).encode("utf-8")).hexdigest()[:8]
    stem, ext = os.path.splitext(basename)
    alt = os.path.join(dest_folder, f"{stem}_{h}{ext}")
    if not os.path.exists(alt):
        shutil.copy2(src, alt)
        return alt
    # hash 名也被占用（极端罕见）：再加序号
    n = 2
    while True:
        cand = os.path.join(dest_folder, f"{stem}_{h}_{n}{ext}")
        if not os.path.exists(cand):
            shutil.copy2(src, cand)
            return cand
        n += 1


def filter_themes(groups, events_map):
    """
    过滤主题组：
    - 事件数 >= MIN_EVENTS_PER_THEME
    - 年份跨度 >= MIN_YEAR_SPAN（即至少跨 MIN_YEAR_SPAN+1 个年度）
    不满足的主题组内事件归入"未归类"。

    返回 (valid_themes, ungrouped_events)
    - valid_themes: [{"theme": str, "events": [event_dict, ...], "theme_folder": str}, ...]
    - ungrouped_events: [event_dict, ...]
    """
    valid_themes = []
    grouped_tags = set()
    for g in groups:
        theme = g["theme"]
        evts = [events_map[tag] for tag in g["events"] if tag in events_map]
        if not evts:
            continue
        grouped_tags.update(e["event_tag"] for e in evts)
        if len(evts) < MIN_EVENTS_PER_THEME:
            logger.info(
                f"  [过滤] 主题「{theme}」仅 {len(evts)} 个事件"
                f"（< min_events_per_theme={MIN_EVENTS_PER_THEME}），归入未归类"
            )
            continue
        years = sorted({e["year"] for e in evts})
        year_span = len(years) - 1
        if year_span < MIN_YEAR_SPAN:
            logger.info(
                f"  [过滤] 主题「{theme}」年份跨度 {year_span}"
                f"（< min_year_span={MIN_YEAR_SPAN}，仅 {years}），归入未归类"
            )
            continue
        theme_folder = _theme_folder_name(theme, evts)
        valid_themes.append({
            "theme": theme,
            "events": evts,
            "theme_folder": theme_folder,
        })

    # 未归类：所有不在 valid_themes 中的事件
    grouped_valid_tags = set()
    for t in valid_themes:
        grouped_valid_tags.update(e["event_tag"] for e in t["events"])
    ungrouped = [e for tag, e in events_map.items() if tag not in grouped_valid_tags]
    return valid_themes, ungrouped


def process_one_theme(theme_data, system_prompt):
    """处理一个主题的 COPY 执行 + manifest 写入"""
    if _stop_event.is_set():
        return theme_data["theme_folder"]

    theme = theme_data["theme"]
    events = theme_data["events"]
    theme_folder_name = theme_data["theme_folder"]

    logger.info(
        f"=== [主题处理] {theme_folder_name}（{len(events)} 个事件，"
        f"{sum(e['photo_count'] for e in events)} 张照片）==="
    )

    with output_dir_lock:
        theme_folder = _unique_theme_folder(theme_folder_name)
        os.makedirs(theme_folder, exist_ok=True)

    # 计算 year_range
    years = sorted({e["year"] for e in events})

    # manifest 数据
    manifest = {
        "theme": theme,
        "theme_folder": os.path.basename(theme_folder),
        "year_range": years,
        "original_events": [],
    }

    # COPY 照片：保留事件子文件夹结构
    for e in events:
        if _stop_event.is_set():
            logger.warning(f"=== [主题跳出] {theme_folder_name} 因熔断中止，部分照片未 copy ===")
            manifest["original_events"].append({
                "event_tag": e["event_tag"],
                "source_folder": e["folder"],
                "photo_count": e["photo_count"],
                "copied_count": 0,
                "photos": [],
                "partial": True,
            })
            if KEEP_MANIFEST:
                try:
                    with open(os.path.join(theme_folder, "manifest.partial.json"), "w", encoding="utf-8") as f:
                        json.dump(manifest, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass
            return theme_folder_name

        # 在主题文件夹下创建事件子文件夹
        event_subfolder = os.path.join(theme_folder, e["event_tag"])
        os.makedirs(event_subfolder, exist_ok=True)

        copied_photos = []
        for photo_path in e["photos"]:
            if _stop_event.is_set():
                logger.warning(f"=== [主题跳出] {theme_folder_name} 因熔断中止，部分照片未 copy ===")
                manifest["original_events"].append({
                    "event_tag": e["event_tag"],
                    "source_folder": e["folder"],
                    "photo_count": e["photo_count"],
                    "copied_count": len(copied_photos),
                    "photos": [os.path.basename(p) for p in copied_photos],
                    "partial": True,
                })
                if KEEP_MANIFEST:
                    try:
                        with open(os.path.join(theme_folder, "manifest.partial.json"), "w", encoding="utf-8") as f:
                            json.dump(manifest, f, ensure_ascii=False, indent=2)
                    except Exception:
                        pass
                return theme_folder_name
            try:
                final_path = _copy_photo_with_conflict(photo_path, event_subfolder)
                copied_photos.append(final_path)
            except Exception as ex:
                logger.error(f"  [{theme_folder_name}] copy 失败 {photo_path}: {ex}")

        manifest["original_events"].append({
            "event_tag": e["event_tag"],
            "source_folder": e["folder"],
            "photo_count": e["photo_count"],
            "copied_count": len(copied_photos),
            "photos": [os.path.basename(p) for p in copied_photos],
        })

    # 写主题级 manifest.json
    if KEEP_MANIFEST:
        try:
            with open(os.path.join(theme_folder, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
        except Exception as ex:
            logger.warning(f"  [{theme_folder_name}] manifest 写入失败：{ex}")

    # 标记完成
    with progress_lock:
        progress_state["completed_themes"].add(os.path.basename(theme_folder))
    save_progress()
    record_theme_done()

    logger.info(
        f"=== [主题完成] {os.path.basename(theme_folder)} "
        f"（{len(events)} 个事件，{sum(e['photo_count'] for e in events)} 张照片）==="
    )
    return os.path.basename(theme_folder)


def process_ungrouped_events(ungrouped_events):
    """把未归类事件记录到清单文件（不 COPY 照片，省时省空间）。

    未归类事件本身就是"无主题关联的散件"，复制它们没有聚合价值。
    只写一个 未归类事件清单.json 到 OUTPUT_DIR，记录每个事件的
    event_tag / source_folder / photo_count，供事后人工查阅。
    全局 yearly_aggregate_manifest.json 也会记录 ungrouped_events 列表。
    """
    if not ungrouped_events:
        return

    logger.info(f"=== [未归类] {len(ungrouped_events)} 个事件，记录到清单文件（不 COPY 照片）====")

    manifest = {
        "theme": "未归类事件",
        "theme_folder": None,  # 无实际文件夹
        "description": "未归类事件不复制照片，仅记录清单供人工查阅",
        "year_range": sorted({e["year"] for e in ungrouped_events}),
        "event_count": len(ungrouped_events),
        "total_photos": sum(e["photo_count"] for e in ungrouped_events),
        "original_events": [
            {
                "event_tag": e["event_tag"],
                "source_folder": e["folder"],
                "photo_count": e["photo_count"],
            }
            for e in ungrouped_events
        ],
    }

    list_path = os.path.join(OUTPUT_DIR, "未归类事件清单.json")
    try:
        with open(list_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        logger.info(f"📦 未归类事件清单已写入：{list_path}（{len(ungrouped_events)} 个事件，{manifest['total_photos']} 张照片）")
    except Exception as ex:
        logger.error(f"❌ 未归类事件清单写入失败：{ex}")


def write_global_manifest(groups, valid_themes, ungrouped_events, total_events):
    """写全局 yearly_aggregate_manifest.json 索引"""
    themes_summary = []
    for t in valid_themes:
        years = sorted({e["year"] for e in t["events"]})
        themes_summary.append({
            "theme": t["theme"],
            "theme_folder": t["theme_folder"],
            "event_count": len(t["events"]),
            "photo_count": sum(e["photo_count"] for e in t["events"]),
            "year_range": years,
        })

    global_manifest = {
        "generated_at": datetime.now().isoformat(),
        "source_target_dir": TARGET_DIR,
        "output_dir": OUTPUT_DIR,
        "total_events_scanned": total_events,
        "total_events_grouped": sum(t["event_count"] for t in themes_summary),
        "total_events_ungrouped": len(ungrouped_events),
        "themes": themes_summary,
        "ungrouped_events": [e["event_tag"] for e in ungrouped_events],
    }

    global_path = os.path.join(OUTPUT_DIR, "yearly_aggregate_manifest.json")
    try:
        with open(global_path, "w", encoding="utf-8") as f:
            json.dump(global_manifest, f, ensure_ascii=False, indent=2)
        logger.info(f"📦 全局索引已写入：{global_path}")
    except Exception as ex:
        logger.error(f"❌ 全局索引写入失败：{ex}")


# ==========================================
# 12. 主流程
# ==========================================
def main():
    # --only 03b 显式触发时（GUI「仅跨年聚合」模式或命令行 --only 03b），
    # run_pipeline.py 会设 AIPHOTO_FORCE_STAGE03B=1，绕过 enabled gate，
    # 让用户能在 enabled=false 时对历史归档目录单跑 03b。
    if not YEARLY_ENABLED and os.environ.get("AIPHOTO_FORCE_STAGE03B") != "1":
        logger.info("03b 阶段未启用（yearly_summary.enabled=false），跳过")
        return
    if not YEARLY_ENABLED:
        logger.info("03b 阶段 enabled=false，但收到强制运行指令（--only 03b），继续执行")

    if not os.path.exists(PROMPT_FILE):
        logger.error(f"❌ 找不到 03b 提示词文件 {PROMPT_FILE}")
        return
    try:
        system_prompt = _load_prompt_text(PROMPT_FILE)
    except Exception as e:
        logger.error(f"❌ 03b 提示词解密失败：{e}")
        return

    if not os.path.isdir(TARGET_DIR):
        logger.error(f"❌ TARGET_DIR 不存在：{TARGET_DIR}")
        return

    load_progress()

    # 扫描 TARGET_DIR
    events = scan_target_dir(TARGET_DIR)
    if not events:
        logger.info("📁 TARGET_DIR 下没有符合 YYYY-MM-DD-* 命名的事件子文件夹，无需处理")
        print_token_summary()
        return

    # 计算 scan_fingerprint，检测 TARGET_DIR 是否有新增事件
    current_fp = compute_scan_fingerprint(events)
    stored_fp = progress_state.get("scan_fingerprint", "")

    logger.info(f"========== 启动阶段三 b：跨年事件聚合（{YEARLY_MODEL}）==========")
    logger.info(f"  [Provider] {YEARLY_PROVIDER}    [Model] {YEARLY_MODEL}")
    logger.info(f"  [Source] {TARGET_DIR}")
    logger.info(f"  [Output] {OUTPUT_DIR}")
    logger.info(f"  [事件总数] {len(events)}")
    logger.info(f"  [min_events_per_theme] {MIN_EVENTS_PER_THEME} | [min_year_span] {MIN_YEAR_SPAN} | [batch_size] {BATCH_SIZE}")

    # scan_fingerprint 变化 -> 全量重聚类
    # 已 completed 的主题保留（COPY 是幂等的），但要重新聚类
    if stored_fp and stored_fp != current_fp:
        logger.warning("=" * 70)
        logger.warning("⚠️ 检测到 TARGET_DIR 事件集合已变化（上次聚合后有新增/删除事件）")
        logger.warning("   将重新进行全量聚类。已 completed 的主题文件夹保留（COPY 幂等）。")
        logger.warning(f"   旧 scan_fingerprint: {stored_fp[:16]}...")
        logger.warning(f"   新 scan_fingerprint: {current_fp[:16]}...")
        logger.warning("=" * 70)
    elif not stored_fp:
        logger.info("  [首次聚合] 无历史 scan_fingerprint，全量聚类")

    # 准备输出目录
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 1. LLM 批量聚类
    events_map = {e["event_tag"]: e for e in events}

    if _stop_event.is_set():
        logger.error("❌ 熔断已触发，跳过聚类")
        print_token_summary()
        return

    logger.info("  [聚类] 调 LLM 批量分组...")
    groups = call_llm_for_clustering(events, system_prompt)

    if _stop_event.is_set():
        logger.error("❌ 聚类过程中熔断，中止")
        print_token_summary()
        return

    if groups is None:
        logger.error("❌ LLM 聚类失败，未生成任何分组")
        print_token_summary()
        return

    logger.info(f"  [聚类完成] 第一遍 LLM 返回 {len(groups)} 个主题组")

    # 1b. 第二遍：主题合并（跨批次语义相同的主题名合并）
    merge_mapping = None
    if not _stop_event.is_set() and len(groups) >= 2 and os.path.exists(MERGE_PROMPT_FILE):
        try:
            merge_prompt = _load_prompt_text(MERGE_PROMPT_FILE)
        except Exception as e:
            logger.warning(f"  [主题合并] 提示词加载失败，跳过第二遍：{e}")
            merge_prompt = None
        if merge_prompt:
            merge_mapping = call_llm_for_merge_themes(groups, merge_prompt)
            if _stop_event.is_set():
                logger.error("❌ 主题合并过程中熔断，中止")
                print_token_summary()
                return
            if merge_mapping:
                before_count = len(groups)
                groups = apply_theme_merge(groups, merge_mapping)
                logger.info(
                    f"  [主题合并完成] {before_count} 个主题 -> {len(groups)} 个主题"
                    f"（合并了 {len(merge_mapping)} 个原始主题名）"
                )
    elif len(groups) >= 2 and not os.path.exists(MERGE_PROMPT_FILE):
        logger.warning(f"  [主题合并] 未找到提示词文件 {MERGE_PROMPT_FILE}，跳过第二遍")

    # 更新 scan_fingerprint
    with progress_lock:
        progress_state["scan_fingerprint"] = current_fp
    save_progress()

    # 2. 过滤主题组
    valid_themes, ungrouped = filter_themes(groups, events_map)

    logger.info(
        f"  [过滤后] 有效主题 {len(valid_themes)} 个，"
        f"未归类事件 {len(ungrouped)} 个"
    )

    # 3. COPY 执行（多线程）
    # 过滤掉已 completed 的主题
    pending_themes = [
        t for t in valid_themes
        if t["theme_folder"] not in progress_state["completed_themes"]
    ]
    already_done = len(valid_themes) - len(pending_themes)

    if not pending_themes and not ungrouped:
        logger.info("✅ 所有主题此前已处理完毕，无需重跑")
        # 仍写全局 manifest（可能事件集变了）
        write_global_manifest(groups, valid_themes, ungrouped, len(events))
        print_token_summary()
        return

    total_to_process = len(pending_themes)
    logger.info(f"  [COPY] 待处理 {total_to_process} 个主题（已跳过 {already_done} 个 completed），{COPY_WORKERS} 线程并行")

    start = time.time()

    with _run_stats_lock:
        _run_stats["start_time"] = start
        _run_stats["themes_done"] = 0
        _run_stats["total_remaining"] = total_to_process
    _monitor_stop.clear()
    monitor_thread = threading.Thread(target=progress_monitor, name="03b-progress-monitor", daemon=True)
    monitor_thread.start()

    try:
        with ThreadPoolExecutor(max_workers=COPY_WORKERS) as pool:
            futures = {
                pool.submit(process_one_theme, t, system_prompt): t["theme_folder"]
                for t in pending_themes
            }
            for fut in as_completed(futures):
                tf = futures[fut]
                try:
                    fut.result()
                except Exception as e:
                    logger.error(f"  [线程异常] {tf}: {e}", exc_info=True)
                    with progress_lock:
                        progress_state["failed_themes"].add(tf)
                    save_progress()
    finally:
        _monitor_stop.set()
        monitor_thread.join(timeout=5)

    # 4. 处理未归类事件（单线程，熔断则跳过）
    if ungrouped and not _stop_event.is_set():
        process_ungrouped_events(ungrouped)
    elif ungrouped and _stop_event.is_set():
        logger.warning("⚠️ 熔断已触发，跳过未归类事件 COPY")

    # 5. 写全局 manifest
    write_global_manifest(groups, valid_themes, ungrouped, len(events))

    elapsed = time.time() - start

    if _stop_event.is_set():
        logger.error("=" * 60)
        logger.error(f"⚠️ 因致命 API 错误中途停止，本次耗时 {elapsed/60:.1f} 分钟")
        logger.error("⚠️ 进度已保存，修复问题后可直接重新运行续跑")
        logger.error("=" * 60)
    else:
        completed_n = len(progress_state["completed_themes"])
        failed_n = len(progress_state["failed_themes"])
        logger.info(f"\n✅ 03b 阶段完毕！本次耗时 {elapsed/60:.1f} 分钟")
        logger.info(f"   完成 {completed_n} 主题 | 失败 {failed_n} 主题 | 未归类 {len(ungrouped)} 事件")
        if failed_n:
            logger.warning(f"   ⚠️ {failed_n} 个主题 COPY 失败，可重新运行续跑重试")

    print_token_summary()


if __name__ == "__main__":
    main()
