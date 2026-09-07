# stage03a_daily_summary.py
"""
03a 阶段：日级日报合并（可选后处理）

职责：
- 扫描 TARGET_DIR 下所有 "YYYY-MM-DD-*" 事件子文件夹，按日期分组
- 对当天多事件调 LLM 判断是否该合并 + 生成汇总名
- 扁平合并到单一文件夹（move 照片 + 删除原空文件夹 + 写 manifest.json）

设计要点：
- 默认 enabled: false，不启用时本脚本 main() 立即返回，行为与之前完全一致
- 独立 progress 文件 03a_progress_<profile>.json，不依赖 02 的 progress
  （03a 只读 TARGET_DIR 文件系统）
- 独立上下文指纹：target_dir / profile / provider / model / merge_threshold
- 断点续跑：merged_dates / skipped_dates / failed_dates 三类状态持久化
- LLM 调用：纯文本输入（事件名+照片数），单层重试 + 限流退避 + 致命错误熔断
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
_DAILY = _CONFIG.get("daily_summary", {})
_FILES = _cfg_loader.resolve_filenames(_CONFIG)

TARGET_DIR = os.path.normpath(_COMMON.get("target_dir", r"D:\JM照片_整理输出"))
PROFILE = _cfg_loader.get_profile(_CONFIG)

DAILY_ENABLED = bool(_DAILY.get("enabled", False))
# provider/model 留空时复用 curator 的配置
DAILY_PROVIDER = (_DAILY.get("provider") or "").strip() or _CURATOR.get("provider", "ollama")

# provider_configs 复用 curator 的（与 02 共享一套密钥配置）
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

if DAILY_PROVIDER not in PROVIDER_CONFIGS:
    raise RuntimeError(f"03a 阶段：provider '{DAILY_PROVIDER}' 不在 provider_configs 中")
_cfg = PROVIDER_CONFIGS[DAILY_PROVIDER]
# model 留空时复用 curator.provider_configs.<provider>.model
DAILY_MODEL = (_DAILY.get("model") or "").strip() or _cfg.get("model", "")

NUM_WORKERS = int(_DAILY.get("num_workers", 3))
MERGE_THRESHOLD = int(_DAILY.get("merge_threshold", 1))
KEEP_MANIFEST = bool(_DAILY.get("keep_manifest", True))

# 提示词加密密钥（与 stage02 / encrypt_prompt.py 一致）
_STATIC_PROMPT_KEY = b"hfuLfxfmqclh5cbuVmVZVGbGmb-blZtvf42_YKV3_CU="

# 提示词文件路径解析（与 stage02 逻辑一致）
_DEFAULT_PROMPT_FILE = "prompts/prompt_daily_summary.enc"
_prompt_rel = _DAILY.get("prompt_file") or _DEFAULT_PROMPT_FILE
if _prompt_rel.endswith(".txt"):
    _prompt_rel = _prompt_rel[:-4] + ".enc"
PROMPT_FILE = os.path.normpath(_prompt_rel) if os.path.isabs(_prompt_rel) else os.path.normpath(os.path.join(_cfg_loader.BASE_DIR, _prompt_rel))

# 03a 进度文件（独立于 02）
PROGRESS_FILE = os.path.join(_cfg_loader.BASE_DIR, _FILES["daily_summary_progress_file"])

# ==========================================
# 2. 重试与熔断参数（部分可经 daily_summary 段外置）
# ==========================================
# 对齐 stage02 下限：120s 在高并发下对 31B 模型过短，易触发真推理超时
LLM_TIMEOUT = float(_DAILY.get("llm_timeout", 300.0))
LLM_MAX_TOKENS = int(_DAILY.get("llm_max_tokens", 2048))
LLM_MAX_TOKENS_RETRY = int(_DAILY.get("llm_max_tokens_retry", 8192))  # 首次输出被截断时，重试用的更大上限
LLM_TEMPERATURE = float(_DAILY.get("llm_temperature", 0.0))
# 真推理超时重试（不计熔断）；假连接故障仍计入熔断
TIMEOUT_MAX_RETRIES = int(_DAILY.get("timeout_max_retries", 1))
TIMEOUT_RETRY_TEMP = float(_DAILY.get("timeout_retry_temperature", 0.1))  # 重试时升温（借鉴 stage02 T2）
# 截断重试升温：qwen3 在 temperature=0 贪心解码下偶发 thinking 死循环（reasoning 重复同一串直到耗尽 token），
# 单纯加大 max_tokens 无法解锁，必须升温打破决定性。0.2 为保守值，对分析质量影响极小。
TRUNCATION_RETRY_TEMP = float(_DAILY.get("truncation_retry_temperature", 0.2))

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
target_dir_lock = threading.Lock()  # 保护 TARGET_DIR 下的 makedirs / 文件操作
token_stats_lock = threading.Lock()

# 原事件文件夹删除时，可安全清理的系统/缩略图残留文件（小写，大小写不敏感匹配）
# 仅这些文件会被自动删除；其他任何残留仍触发 WARNING 以保留可见性
_SAFE_CLEANUP_FILENAMES = {
    "thumbs.db", "desktop.ini", ".ds_store",
    "ehthumbs.db", "ehthumbs_vista.db", "picthumbnail.db", "catalog.cache",
}

_consecutive_failures = 0
_failure_lock = threading.Lock()
_stop_event = threading.Event()

token_stats = {
    "total_input": 0, "total_output": 0, "total_calls": 0,
}

# 进度状态：main 里加载/保存
progress_state = {
    "merged_dates": set(),    # 已成功合并的 date_str
    "skipped_dates": set(),   # LLM 判定不合并的 date_str（避免续跑重复调用）
    "failed_dates": set(),    # 合并失败（LLM 错误 / 文件冲突等）的 date_str
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
    f"03a_daily_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
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
# 5. 上下文指纹（独立于 02）
# ==========================================
# 03a 只读 TARGET_DIR 文件系统，不依赖 source_dir / batches json，
# 所以指纹字段不含 source_dir / extraction_level 等。
# 切换 provider/model/threshold/profile/target_dir 都触发自愈。
_CONTEXT_FINGERPRINT_FIELDS = ("target_dir", "profile", "daily_summary_provider",
                                "daily_summary_model", "merge_threshold")


def _current_context_fingerprint() -> dict:
    return {
        "target_dir": TARGET_DIR,
        "profile": PROFILE,
        "daily_summary_provider": DAILY_PROVIDER,
        "daily_summary_model": DAILY_MODEL,
        "merge_threshold": MERGE_THRESHOLD,
    }


def _archive_mismatched_progress(mismatched_fields):
    """上下文指纹不匹配时归档旧进度文件 + 清空 progress_state（与 02 同样逻辑）"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_dir = os.path.join(_cfg_loader.BASE_DIR, "_pipeline_archive", "mismatched")
    os.makedirs(archive_dir, exist_ok=True)
    base_name = os.path.basename(PROGRESS_FILE)
    archived_name = f"{base_name[:-5]}_mismatched_{stamp}.json"
    archived_path = os.path.join(archive_dir, archived_name)
    try:
        shutil.move(PROGRESS_FILE, archived_path)
    except Exception as e:
        logger.warning(f"  ⚠️ 旧 03a 进度文件归档失败：{e}（将直接覆盖）")

    logger.warning("=" * 70)
    logger.warning("⚠️ 检测到 03a 进度文件的上下文与当前运行环境不匹配，已重置进度从头跑。")
    logger.warning("   旧进度文件已归档（不删除）：")
    logger.warning(f"     {PROGRESS_FILE}")
    logger.warning(f"   -> {archived_path}")
    for field, (old_val, new_val) in mismatched_fields.items():
        logger.warning(f"   - {field}: {old_val!r} -> {new_val!r}")
    logger.warning("   如需保持旧进度，请改回上述配置后重跑；否则继续从头合并。")
    logger.warning("=" * 70)

    progress_state["merged_dates"] = set()
    progress_state["skipped_dates"] = set()
    progress_state["failed_dates"] = set()


def _migrate_legacy_progress():
    """一次性迁移：若 03a_progress 不存在但旧 03_progress 存在，重命名过来。

    stage03 -> stage03a 重命名后，progress 文件名从 03_progress_<profile>.json
    改为 03a_progress_<profile>.json。旧文件内容完全兼容（字段结构未变），
    直接重命名即可继承断点续跑状态，避免重跑已合并的日期。
    """
    if os.path.exists(PROGRESS_FILE):
        return  # 新文件已存在，无需迁移
    # 推导旧文件名：把 03a_progress 替换为 03_progress
    base_dir = os.path.dirname(PROGRESS_FILE)
    cur_name = os.path.basename(PROGRESS_FILE)  # 03a_progress[_<profile>].json
    legacy_name = cur_name.replace("03a_progress", "03_progress", 1)
    legacy_path = os.path.join(base_dir, legacy_name)
    if not os.path.exists(legacy_path):
        return
    try:
        shutil.move(legacy_path, PROGRESS_FILE)
        logger.info(f"📦 一次性迁移：旧 {legacy_name} -> {cur_name}（继承断点续跑状态）")
    except Exception as e:
        logger.warning(f"⚠️ 旧进度文件迁移失败（{legacy_name} -> {cur_name}）：{e}")


def load_progress():
    _migrate_legacy_progress()
    if not os.path.exists(PROGRESS_FILE):
        return
    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"03a 进度文件解析失败，将从头开始：{e}")
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

    progress_state["merged_dates"] = set(data.get("merged_dates", []))
    progress_state["skipped_dates"] = set(data.get("skipped_dates", []))
    progress_state["failed_dates"] = set(data.get("failed_dates", []))
    ts = data.get("token_stats")
    if ts:
        token_stats["total_input"] = ts.get("total_input", 0)
        token_stats["total_output"] = ts.get("total_output", 0)
        token_stats["total_calls"] = ts.get("total_calls", 0)
    logger.info(
        f"📥 加载 03a 进度：已合并 {len(progress_state['merged_dates'])} 天，"
        f"已跳过 {len(progress_state['skipped_dates'])} 天，"
        f"已失败 {len(progress_state['failed_dates'])} 天"
    )


def save_progress():
    """线程安全的进度持久化（原子写）"""
    with progress_lock:
        data = {
            "merged_dates": sorted(progress_state["merged_dates"]),
            "skipped_dates": sorted(progress_state["skipped_dates"]),
            "failed_dates": sorted(progress_state["failed_dates"]),
            "token_stats": token_stats,
            "context_fingerprint": _current_context_fingerprint(),
        }
        tmp = PROGRESS_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, PROGRESS_FILE)
        except Exception as e:
            logger.error(f"03 进度保存失败：{e}")


# ==========================================
# 6. 实时速度与 ETA 监控（简化版，与 02 同样逻辑）
# ==========================================
PROGRESS_REPORT_INTERVAL = int(_DAILY.get("progress_report_interval", 30))
PROGRESS_WINDOW_SECONDS = max(PROGRESS_REPORT_INTERVAL * 5, 300)

_run_stats_lock = threading.Lock()
_run_stats = {
    "start_time": None,
    "dates_done": 0,
    "total_remaining": 0,
    "events": deque(),
}
_monitor_stop = threading.Event()


def record_date_done():
    now = time.time()
    with _run_stats_lock:
        _run_stats["dates_done"] += 1
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
            dates_done = _run_stats["dates_done"]
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
        avg_rate = dates_done / total_dt * 60.0

        remaining = max(total_remaining - dates_done, 0)

        if dates_done == 0 or avg_rate <= 0:
            logger.info(
                f"⏱️ [进度] 已处理 {dates_done}/{total_remaining} 日 | "
                f"ETA 计算中（样本不足）"
            )
        else:
            eta_seconds = remaining / (avg_rate / 60.0)
            finish_at = datetime.fromtimestamp(now + eta_seconds).strftime("%m-%d %H:%M:%S")
            pct = dates_done / total_remaining * 100 if total_remaining else 0
            logger.info(
                f"⏱️ [进度] {dates_done}/{total_remaining} 日 ({pct:.1f}%) | "
                f"实时 {win_rate:.1f} 日/分 | "
                f"平均 {avg_rate:.1f} 日/分 | "
                f"剩余 {remaining} 日 | ETA {_fmt_duration(eta_seconds)}（预计 {finish_at} 完成）"
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
    logger.info(f"📊 03a Token 消耗汇总（模型：{DAILY_MODEL}，Provider：{DAILY_PROVIDER}）")
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
_DATE_EVENT_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})-(.+)$')


def scan_target_dir(target_dir):
    """
    扫描 TARGET_DIR 下所有 "YYYY-MM-DD-*" 子文件夹，按日期分组。

    返回 {date_str: [{"event_tag": str, "folder": abs_path, "photos": [abs_paths]}, ...]}
    跳过：非目录、不符合日期前缀命名的目录、_FAILED_FOR_MANUAL_REVIEW 等。
    """
    events_by_date = defaultdict(list)
    if not os.path.isdir(target_dir):
        return events_by_date

    for name in sorted(os.listdir(target_dir)):
        full = os.path.join(target_dir, name)
        if not os.path.isdir(full):
            continue
        m = _DATE_EVENT_RE.match(name)
        if not m:
            continue  # 不符合 YYYY-MM-DD-xxx 命名，跳过
        date_str, event_name = m.group(1), m.group(2)
        # 收集该事件下的所有照片
        photos = []
        for root, dirs, files in os.walk(full):
            for fn in files:
                if fn.lower().endswith(('.jpg', '.jpeg', '.png')):
                    photos.append(os.path.join(root, fn))
        photos.sort()
        # 检测 manifest.json：存在说明该文件夹是 03a 阶段合并产物（非原始事件）。
        # 用于 main() 识别"已合并过的单事件天"，避免误标 skipped。
        has_manifest = os.path.isfile(os.path.join(full, "manifest.json"))
        events_by_date[date_str].append({
            "event_tag": name,  # 完整文件夹名（含日期前缀），便于后续 move
            "event_name": event_name,
            "folder": full,
            "photos": photos,
            "photo_count": len(photos),
            "has_manifest": has_manifest,
        })
    return events_by_date


def sanitize_event_tag(event_tag):
    """与 stage02 同样的非法字符清理"""
    if not event_tag:
        return event_tag
    illegal = ['/', '\\', ':', '*', '?', '"', '<', '>', '|']
    cleaned = event_tag
    for ch in illegal:
        cleaned = cleaned.replace(ch, '、')
    cleaned = re.sub(r'\s+', '', cleaned)
    return cleaned[:100]


def is_fatal_api_error(err_str):
    """与 stage02 一致的致命错误识别"""
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
        "model": DAILY_MODEL,
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


def call_llm_for_merge_decision(date_str, events, system_prompt):
    """
    调 LLM 判断当天多个事件是否该合并 + 生成汇总名。

    输入（纯文本）：
      【日期】: 2026-07-19
      【当天事件列表】:
      1. 铁山坪踏青（15 张）
      2. 铁山坪森林公园游玩（12 张）

    输出 JSON: {"should_merge": true/false, "merged_name": "...", "reason": "..."}

    返回 dict 或 None（失败）。
    """
    if _stop_event.is_set():
        return None

    events_text = "\n".join(
        f"{i+1}. {e['event_name']}（{e['photo_count']} 张）"
        for i, e in enumerate(events)
    )
    user_text = (
        f"【日期】:{date_str}\n"
        f"【当天事件列表】:\n{events_text}\n\n"
        f"请判断这些事件是否属于同一个日活动，并输出 JSON。"
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_text},
    ]
    logger.info(f"  [{date_str}] 调 LLM 判断合并（{len(events)} 个事件）...")

    # 第一次调用（默认 max_tokens）
    result = _invoke_llm_with_retry_loop(date_str, messages, max_tokens_override=None)
    if result is None:
        return None
    response, response_text, truncated = result

    # 截断检测：finish_reason==length，或空返回但 completion_tokens>0
    # -> 用更大 max_tokens 重试一次
    if truncated and not _stop_event.is_set():
        completion_tokens = getattr(response.usage, "completion_tokens", 0) if response.usage else 0
        logger.warning(
            f"  [{date_str}] LLM 输出被截断（completion_tokens={completion_tokens}），"
            f"用 max_tokens={LLM_MAX_TOKENS_RETRY}, 温度={TRUNCATION_RETRY_TEMP} 重试一次"
        )
        retry_result = _invoke_llm_with_retry_loop(
            date_str, messages,
            max_tokens_override=LLM_MAX_TOKENS_RETRY,
            temperature_override=TRUNCATION_RETRY_TEMP,
        )
        if retry_result is None:
            return None
        response, response_text, truncated = retry_result
        if truncated:
            completion_tokens = getattr(response.usage, "completion_tokens", 0) if response.usage else 0
            logger.error(
                f"  [{date_str}] 重试后仍被截断（completion_tokens={completion_tokens}），放弃"
            )
            return None

    return _parse_merge_decision(response_text, date_str)


def _invoke_llm_with_retry_loop(date_str, messages, max_tokens_override=None, temperature_override=None):
    """
    单次 LLM 调用（含限流重试 + 真推理超时重试），返回 (response, response_text, truncated) 或 None。
    truncated=True 表示输出被截断（finish_reason==length 或空返回但 completion_tokens>0）。

    超时处理（借鉴 stage02）：
    - 真推理超时（elapsed >= timeout*0.5）：按 TIMEOUT_MAX_RETRIES 重试（升温），不计熔断
    - 假连接故障（elapsed <  timeout*0.5）：计入熔断，不重试
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
                    f"  [{date_str}] Token 输入 {usage.prompt_tokens}, 输出 {usage.completion_tokens}"
                )
            completion_tokens = getattr(usage, "completion_tokens", 0) if usage else 0
            # 截断判定：finish_reason==length，或空返回但已消耗 completion_tokens（ollama 偶发空 content）
            is_truncated = (finish_reason == "length") or (
                (not response_text or not response_text.strip()) and completion_tokens > 0
            )
            if not response_text or not response_text.strip():
                if not is_truncated:
                    # 真正的空返回（非截断导致）
                    logger.warning(f"  [{date_str}] LLM 空返回")
                    return None
                # 截断导致的空返回 -> 返回 truncated=True 让上层重试
                return response, response_text, True
            if _stop_event.is_set():
                return None
            return response, response_text, is_truncated
        except APITimeoutError:
            actual_elapsed = time.time() - start_time
            # 真假超时区分：
            # - 真超时（模型推理卡顿/慢）：elapsed 会接近配置的 timeout，应重试，不计熔断
            # - 假超时（本地服务下线/网络不可达，被 SDK 包装成 APITimeoutError）：elapsed 远小于配置值
            #   这种情况重试也救不回来，必须计入熔断避免空转
            if actual_elapsed < LLM_TIMEOUT * 0.5:
                logger.warning(
                    f"  [{date_str}] ⚠️ 疑似连接故障（{actual_elapsed:.1f}s 即超时，配置 {LLM_TIMEOUT:.0f}s）"
                )
                _on_request_failure(f"fake_timeout_{actual_elapsed:.0f}s")
                return None
            # 真推理超时：按 TIMEOUT_MAX_RETRIES 重试（升温），不计熔断
            if timeout_attempt < TIMEOUT_MAX_RETRIES and not _stop_event.is_set():
                logger.warning(
                    f"  [{date_str}] 推理超时（{actual_elapsed:.1f}s / 配置 {LLM_TIMEOUT:.0f}s），"
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
                f"  [{date_str}] 推理超时重试耗尽（{actual_elapsed:.1f}s / 配置 {LLM_TIMEOUT:.0f}s），放弃"
            )
            _on_request_failure("timeout")
            return None
        except Exception as e:
            err_str = str(e)
            if is_fatal_api_error(err_str):
                logger.error(f"  [{date_str}] ⚠️ 致命 API 错误，立即停止：{e}")
                _stop_event.set()
                return None
            is_rate_limit = (
                "429" in err_str or "Too Many Requests" in err_str
                or "rate_limit" in err_str.lower() or "TooManyRequests" in err_str
            )
            if is_rate_limit and attempt < RATE_LIMIT_MAX_RETRIES:
                wait = RATE_LIMIT_BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 1)
                logger.warning(
                    f"  [{date_str}] 触发限流，等待 {wait:.1f}s 后第 {attempt+1}/{RATE_LIMIT_MAX_RETRIES} 次重试"
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
                    f"  [{date_str}] 服务端瞬时错误（{err_str[:80]}），等待 {wait:.1f}s 后第 {transient_attempt+1}/{TRANSIENT_MAX_RETRIES} 次重试"
                )
                time.sleep(wait)
                transient_attempt += 1
                continue
            logger.error(f"  [{date_str}] LLM 错误: {e}")
            _on_request_failure(err_str[:80])
            return None

    logger.error(f"  [{date_str}] 限流重试耗尽，放弃")
    return None


def _parse_merge_decision(response_text, date_str):
    """解析 LLM 返回的 JSON 决策"""
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
        logger.error(f"  [{date_str}] LLM 返回非 JSON: {response_text[:200]}")
        return None
    if not isinstance(obj, dict):
        return None
    should_merge = bool(obj.get("should_merge", False))
    merged_name = str(obj.get("merged_name", "") or "").strip()
    reason = str(obj.get("reason", "") or "").strip()
    return {
        "should_merge": should_merge,
        "merged_name": merged_name,
        "reason": reason,
    }


# ==========================================
# 11. 单日处理
# ==========================================
def _format_events_brief(events):
    """生成合并前事件摘要，用于日志统一显示。
    形如: 3 个事件 / 6 张照片 [城市休闲, 公园漫步, 街拍]
    事件名超过 5 个时，显示前 5 个 + "等 N 个"。
    """
    n = len(events)
    photos = sum(e["photo_count"] for e in events)
    names = [e["event_name"] for e in events]
    if len(names) > 5:
        names_display = ", ".join(names[:5]) + f" 等 {len(names)} 个"
    else:
        names_display = ", ".join(names)
    return f"{n} 个事件 / {photos} 张照片 [{names_display}]"


def _unique_merged_folder(target_dir, merged_name):
    """生成不冲突的合并文件夹路径。若已存在则加 _v2 / _v3 ... 后缀"""
    base = os.path.join(target_dir, merged_name)
    if not os.path.exists(base):
        return base
    n = 2
    while True:
        candidate = f"{base}_v{n}"
        if not os.path.exists(candidate):
            return candidate
        n += 1


def _move_photo_with_conflict(src, dest_folder):
    """
    把 src move 到 dest_folder/basename(src)。同名冲突时加 _<hash8> 后缀。
    返回最终路径。
    """
    basename = os.path.basename(src)
    dest = os.path.join(dest_folder, basename)
    if not os.path.exists(dest):
        shutil.move(src, dest)
        return dest
    # 同名冲突（不同事件子文件夹有同名照片）：加源路径 hash 后缀
    h = hashlib.md5(os.path.abspath(src).encode("utf-8")).hexdigest()[:8]
    stem, ext = os.path.splitext(basename)
    alt = os.path.join(dest_folder, f"{stem}_{h}{ext}")
    if not os.path.exists(alt):
        shutil.move(src, alt)
        return alt
    # hash 名也被占用（极端罕见）：再加序号
    n = 2
    while True:
        cand = os.path.join(dest_folder, f"{stem}_{h}_{n}{ext}")
        if not os.path.exists(cand):
            shutil.move(src, cand)
            return cand
        n += 1


def process_one_date(date_str, events, system_prompt):
    """处理一整天的合并判断与执行"""
    if _stop_event.is_set():
        return date_str

    logger.info(
        f"=== [日线程启动] {date_str}（{_format_events_brief(events)}）==="
    )

    # 1. 调 LLM 判断是否合并
    decision = call_llm_for_merge_decision(date_str, events, system_prompt)

    if _stop_event.is_set():
        logger.warning(f"=== [日线程跳出] {date_str} 因熔断中止 ===")
        return date_str

    if decision is None:
        # LLM 失败：标记 failed，留待人工或续跑
        logger.error(f"  [{date_str}] LLM 调用失败，标记为 failed_dates")
        with progress_lock:
            progress_state["failed_dates"].add(date_str)
        save_progress()
        record_date_done()
        return date_str

    if not decision["should_merge"]:
        # LLM 判定不合并（如"上午开会+晚上聚餐"语义无关）
        logger.info(
            f"  [{date_str}] 不合并（{_format_events_brief(events)}）：{decision.get('reason', '')}"
        )
        with progress_lock:
            progress_state["skipped_dates"].add(date_str)
        save_progress()
        record_date_done()
        logger.info(f"=== [日线程完成] {date_str} 不合并（skipped，{_format_events_brief(events)}）===")
        return date_str

    # 2. 准备合并文件夹
    merged_name = sanitize_event_tag(f"{date_str}-{decision['merged_name']}")
    if not merged_name or merged_name == date_str + "-":
        # LLM 没给名字，兜底用第一个事件名
        merged_name = sanitize_event_tag(f"{date_str}-{events[0]['event_name']}")
        logger.warning(f"  [{date_str}] LLM 未给 merged_name，兜底用第一个事件：{merged_name}")

    with target_dir_lock:
        merged_folder = _unique_merged_folder(TARGET_DIR, merged_name)
        os.makedirs(merged_folder, exist_ok=True)

    logger.info(
        f"  [{date_str}] 合并到 {os.path.basename(merged_folder)}"
        f"（{_format_events_brief(events)} | 理由：{decision.get('reason', '')}）"
    )

    # 3. 收集 manifest 数据 + move 照片
    manifest = {
        "date": date_str,
        "merged_name": os.path.basename(merged_folder),
        "llm_reason": decision.get("reason", ""),
        "original_events": [],
    }

    for e in events:
        moved_photos = []
        for photo_path in e["photos"]:
            if _stop_event.is_set():
                # 熔断中断：未 move 的照片留在原位，本日不标记 merged
                logger.warning(f"=== [日线程跳出] {date_str} 因熔断中止，部分照片未 move ===")
                # 仍然记录 manifest（已 move 的部分），但不标记 merged_dates
                manifest["original_events"].append({
                    "event_tag": e["event_tag"],
                    "photo_count": e["photo_count"],
                    "moved_count": len(moved_photos),
                    "photos": [os.path.basename(p) for p in moved_photos],
                })
                # 写一个 partial manifest 便于事后人工处理
                if KEEP_MANIFEST:
                    try:
                        with open(os.path.join(merged_folder, "manifest.partial.json"), "w", encoding="utf-8") as f:
                            json.dump(manifest, f, ensure_ascii=False, indent=2)
                    except Exception:
                        pass
                return date_str
            try:
                final_path = _move_photo_with_conflict(photo_path, merged_folder)
                moved_photos.append(final_path)
            except Exception as ex:
                logger.error(f"  [{date_str}] move 失败 {photo_path}: {ex}")
        manifest["original_events"].append({
            "event_tag": e["event_tag"],
            "photo_count": e["photo_count"],
            "moved_count": len(moved_photos),
            "photos": [os.path.basename(p) for p in moved_photos],
        })

    # 4. 删除已空的原事件子文件夹
    for e in events:
        try:
            # 仅在文件夹为空时删除（safety check）；非空说明有非照片文件残留
            os.rmdir(e["folder"])
            continue
        except OSError:
            pass
        # 文件夹非空：先清空空子目录 + 白名单系统残留文件，再 rmdir
        removed = []
        for root, dirs, files in os.walk(e["folder"], topdown=False):
            for d in dirs:
                try:
                    os.rmdir(os.path.join(root, d))
                except OSError:
                    pass
            for fn in files:
                if fn.lower() in _SAFE_CLEANUP_FILENAMES:
                    try:
                        os.remove(os.path.join(root, fn))
                        removed.append(fn)
                    except OSError:
                        pass
        if removed:
            logger.info(
                f"  [{date_str}] 清理系统残留 {len(removed)} 个"
                f"：{', '.join(sorted(set(removed)))}"
            )
        try:
            os.rmdir(e["folder"])
        except OSError as ex:
            # 仍有白名单外的文件残留，维持 WARNING 可见性
            logger.warning(f"  [{date_str}] 原事件文件夹非空未删除：{e['folder']}（{ex}）")

    # 5. 写 manifest.json
    if KEEP_MANIFEST:
        try:
            with open(os.path.join(merged_folder, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
        except Exception as ex:
            logger.warning(f"  [{date_str}] manifest 写入失败：{ex}")

    # 6. 标记完成
    with progress_lock:
        progress_state["merged_dates"].add(date_str)
    save_progress()
    record_date_done()

    logger.info(
        f"=== [日线程完成] {date_str} 已合并 -> {os.path.basename(merged_folder)} "
        f"（原 {_format_events_brief(events)}）==="
    )
    return date_str


# ==========================================
# 12. 主流程
# ==========================================
def main():
    # --only 03a 显式触发时（GUI「仅合并日报」模式或命令行 --only 03a），
    # run_pipeline.py 会设 AIPHOTO_FORCE_STAGE03A=1，绕过 enabled gate，
    # 让用户能在 enabled=false 时对历史归档目录单跑 03a。
    if not DAILY_ENABLED and os.environ.get("AIPHOTO_FORCE_STAGE03A") != "1":
        logger.info("03a 阶段未启用（daily_summary.enabled=false），跳过")
        return
    if not DAILY_ENABLED:
        logger.info("03a 阶段 enabled=false，但收到强制运行指令（--only 03a），继续执行")

    if not os.path.exists(PROMPT_FILE):
        logger.error(f"❌ 找不到 03a 提示词文件 {PROMPT_FILE}")
        return
    try:
        system_prompt = _load_prompt_text(PROMPT_FILE)
    except Exception as e:
        logger.error(f"❌ 03a 提示词解密失败：{e}")
        return

    if not os.path.isdir(TARGET_DIR):
        logger.error(f"❌ TARGET_DIR 不存在：{TARGET_DIR}")
        return

    load_progress()

    # 扫描 TARGET_DIR
    events_by_date = scan_target_dir(TARGET_DIR)
    if not events_by_date:
        logger.info("📁 TARGET_DIR 下没有符合 YYYY-MM-DD-* 命名的事件子文件夹，无需处理")
        print_token_summary()
        return

    # 过滤掉已处理日期
    pending_all = {
        d: evts for d, evts in events_by_date.items()
        if d not in progress_state["merged_dates"]
        and d not in progress_state["skipped_dates"]
        and d not in progress_state["failed_dates"]
    }

    # 分类：
    # - already_merged_dates：单事件天且该事件含 manifest.json -> 之前已合并过，
    #   标记为 merged（不重复处理，避免误标 skipped 覆盖已合并语义）
    # - single_event_dates：单事件天且无 manifest.json -> 原始单事件，标 skipped
    # - multi_event_dates：多事件天 -> 进 LLM 判断
    already_merged_dates = {
        d for d, evts in pending_all.items()
        if len(evts) <= MERGE_THRESHOLD and any(e.get("has_manifest") for e in evts)
    }
    single_event_dates = {
        d for d, evts in pending_all.items()
        if len(evts) <= MERGE_THRESHOLD and not any(e.get("has_manifest") for e in evts)
    }
    multi_event_dates = {d for d, evts in pending_all.items() if len(evts) > MERGE_THRESHOLD}

    if already_merged_dates:
        dates_list = sorted(already_merged_dates)
        logger.info(
            f"📂 {len(already_merged_dates)} 天已合并过（检测到 manifest.json），"
            f"标记为 merged：{', '.join(dates_list)}"
        )
        with progress_lock:
            progress_state["merged_dates"].update(already_merged_dates)
        save_progress()

    if single_event_dates:
        dates_list = sorted(single_event_dates)
        logger.info(
            f"📂 {len(single_event_dates)} 天仅有单事件（<= merge_threshold {MERGE_THRESHOLD}），"
            f"直接标记 skipped：{', '.join(dates_list)}"
        )
        with progress_lock:
            progress_state["skipped_dates"].update(single_event_dates)
        save_progress()

    total_dates = len(events_by_date)
    already_done = len(progress_state["merged_dates"]) + len(progress_state["skipped_dates"])
    remain = len(multi_event_dates)

    logger.info(f"========== 启动阶段三：日级日报合并（{DAILY_MODEL}，{NUM_WORKERS} 线程）==========")
    logger.info(f"  [Provider] {DAILY_PROVIDER}    [Model] {DAILY_MODEL}")
    logger.info(f"  [Target] {TARGET_DIR}")
    logger.info(f"  [日期总数] {total_dates} | [已处理] {already_done} | [本次需处理] {remain}")
    logger.info(f"  [merge_threshold] {MERGE_THRESHOLD}（当天事件数 <= 此值则直接跳过）")

    if remain == 0:
        logger.info("✅ 所有日期此前已处理完毕，无需重跑")
        print_token_summary()
        return

    logger.info(f"  [进度监控] 每 {PROGRESS_REPORT_INTERVAL}s 输出一次实时速度与 ETA")

    start = time.time()

    with _run_stats_lock:
        _run_stats["start_time"] = start
        _run_stats["dates_done"] = 0
        _run_stats["total_remaining"] = remain
    _monitor_stop.clear()
    monitor_thread = threading.Thread(target=progress_monitor, name="03-progress-monitor", daemon=True)
    monitor_thread.start()

    try:
        with ThreadPoolExecutor(max_workers=NUM_WORKERS) as pool:
            futures = {
                pool.submit(process_one_date, d, events_by_date[d], system_prompt): d
                for d in sorted(multi_event_dates)
            }
            for fut in as_completed(futures):
                d = futures[fut]
                try:
                    fut.result()
                except Exception as e:
                    logger.error(f"  [线程异常] {d}: {e}", exc_info=True)
    finally:
        _monitor_stop.set()
        monitor_thread.join(timeout=5)

    elapsed = time.time() - start

    if _stop_event.is_set():
        logger.error("=" * 60)
        logger.error(f"⚠️ 因致命 API 错误中途停止，本次耗时 {elapsed/60:.1f} 分钟")
        logger.error("⚠️ 进度已保存，修复问题后可直接重新运行续跑")
        logger.error("=" * 60)
    else:
        merged_n = len(progress_state["merged_dates"])
        skipped_n = len(progress_state["skipped_dates"])
        failed_n = len(progress_state["failed_dates"])
        logger.info(f"\n✅ 03a 阶段完毕！本次耗时 {elapsed/60:.1f} 分钟")
        logger.info(f"   合并 {merged_n} 天 | 跳过 {skipped_n} 天 | 失败 {failed_n} 天")
        if failed_n:
            logger.warning(f"   ⚠️ {failed_n} 天合并失败，可重新运行续跑重试")

    print_token_summary()


if __name__ == "__main__":
    main()
