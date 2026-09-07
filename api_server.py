# api_server.py
"""
Homelab API 服务：接收手机端上传的缩略图，调用流水线分析，返回删除列表。

独立于 GUI 流程，通过 api_config.yaml 配置。
仅 bind ZeroTier 接口 IP，不暴露公网。

用法：
  python api_server.py                    # 用 api_config.yaml
  python api_server.py --config other.yaml
  python api_server.py --port 36600       # 命令行覆盖端口
"""

import os
import sys
import json
import yaml
import hashlib
import secrets
import logging
import time
import argparse
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
import uvicorn

# 导入任务管理和流水线运行器
from api_task_manager import TaskManager, TaskError
from api_pipeline_runner import PipelineRunner

import re

# 本地推理服务列表（需要真实连接探活，而非仅校验配置）。
# 内置默认值：作为 api_config.yaml 未配置 local_providers 时的回退（向后兼容）。
# 运行时以 api_config.yaml 的 local_providers 为准（见下方加载 config 后的赋值），
# 接入新本地服务（如 New-API）只需改配置、无需改代码。
DEFAULT_LOCAL_PROVIDERS = ['ollama', 'lmstudio', 'ollama-new']
LOCAL_PROVIDERS = DEFAULT_LOCAL_PROVIDERS


def _biz_error(status_code: int, error_code: str, message: str, **extra) -> HTTPException:
    """构造业务错误：返回明确 error_code + 中文 message，供客户端展示与判断。"""
    detail = {"error_code": error_code, "message": message}
    detail.update(extra)
    return HTTPException(status_code=status_code, detail=detail)


def _internal_error(exc: Exception) -> HTTPException:
    """
    未预期的内部错误：生成 error_id 关联日志，对外只返回通用提示，
    不泄露异常内部细节（路径、依赖版本等）。
    """
    error_id = secrets.token_hex(6)
    logger.error(f"[error_id={error_id}] 内部错误: {exc}", exc_info=True)
    return HTTPException(status_code=500, detail={
        "error_code": "internal_error",
        "message": "服务器内部错误，请稍后重试",
        "error_id": error_id,
    })

# task_id 白名单：仅允许服务端生成的格式 task_<16位hex>_<unix秒>，
# 防止客户端传入 ../ 等路径穿越片段拼进文件系统路径。
_TASK_ID_RE = re.compile(r'^task_[0-9a-f]{16}_\d+$')


def _validate_task_id(task_id: str) -> str:
    """校验 task_id 合法性；非法直接抛 404（不泄露内部路径结构）。"""
    from fastapi import HTTPException as _HTTPExc
    if not task_id or not _TASK_ID_RE.match(task_id):
        raise _HTTPExc(status_code=404, detail={
            "error_code": "task_not_found", "message": "任务不存在"})
    return task_id


# ==========================================
# 0. 兜底：修复 Windows 服务环境下的 stdout/stderr
# ==========================================

def fix_stdout_for_service():
    """
    Windows 服务 + Nuitka --windows-console-mode=disable 环境下，
    sys.stdout 和 sys.stderr 为 None，会导致任何写标准流的代码崩溃。
    在最早期用空流顶替，防止 uvicorn 等第三方库写 None 崩溃。
    """
    class NullStream:
        def write(self, s): pass
        def flush(self): pass
        def isatty(self): return False
    
    if sys.stdout is None:
        sys.stdout = NullStream()
    if sys.stderr is None:
        sys.stderr = NullStream()

# 立即执行修复
fix_stdout_for_service()


# ==========================================
# 1. 配置加载
# ==========================================

def load_api_config(config_path: str = "api_config.yaml") -> dict:
    """加载 API 配置文件，支持相对于 exe 所在目录的路径"""
    # 如果是相对路径，基于脚本（或 exe）所在目录
    if not os.path.isabs(config_path):
        # 获取 exe 或脚本所在目录
        if getattr(sys, 'frozen', False):
            # Nuitka/PyInstaller 打包后
            base_dir = os.path.dirname(sys.executable)
        else:
            # 直接运行 Python 脚本
            base_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(base_dir, config_path)
    
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    return config


# ==========================================
# 2. 模型服务健康检查
# ==========================================

def check_model_health(api_config: dict, pipeline_config: dict) -> tuple[bool, str, str, dict]:
    """
    根据当前生效的 provider 检查模型服务健康状态
    
    返回: (是否健康, provider, model, 详细信息)
    """
    import requests
    
    # 读取生效的 provider（从 pipeline_overrides 中读取，缺省 ollama）
    provider = api_config.get('pipeline_overrides', {}).get('curator', {}).get('provider', 'ollama')
    
    # 获取 provider 配置
    provider_configs = pipeline_config.get('curator', {}).get('provider_configs', {})
    if provider not in provider_configs:
        return False, provider, '', {"error": f"provider '{provider}' 未在 pipeline_config.yaml 中定义"}
    
    provider_cfg = provider_configs[provider]
    model = provider_cfg.get('model', '')
    
    # provider == 本地服务（ollama/lmstudio 等）: 连接本地服务真实探活
    if provider in LOCAL_PROVIDERS:
        health_config = api_config.get('health_check', {})
        # 优先读取该 provider 专用配置，回退到 ollama 配置（兼容未单独配置的情况）
        provider_cfg_key = provider if provider in health_config else 'ollama'
        service_cfg = health_config.get(provider_cfg_key, {})
        service_url = service_cfg.get('url', '')
        expected_version = service_cfg.get('expected_version', '')
        timeout = health_config.get('timeout', 5)
        
        if not service_url:
            return False, provider, model, {"error": f"health_check.{provider_cfg_key}.url 未配置"}
        
        # 探活鉴权：本地服务若开启了 API key（如 LM Studio 设了 key、New-API 网关），
        # /v1/models 等端点会要求 Authorization，否则返回 401 被误判为不可达。
        # 统一带上该 provider 的 api_key；ollama 用占位 key（"ollama"）带头也无害。
        probe_api_key = provider_cfg.get('api_key', '').strip()
        probe_headers = {"Authorization": f"Bearer {probe_api_key}"} if probe_api_key else {}
        
        try:
            # 根据服务类型选择探活接口：
            #   ollama              -> /api/tags（ollama 专有）
            #   lmstudio / 其他本地  -> /v1/models（OpenAI 兼容标准端点）
            if provider == 'ollama':
                response = requests.get(f"{service_url}/api/tags", timeout=timeout, headers=probe_headers)
            else:
                response = requests.get(f"{service_url}/v1/models", timeout=timeout, headers=probe_headers)
            
            if response.status_code != 200:
                return False, provider, model, {
                    "reachable": False,
                    "error": f"HTTP {response.status_code}"
                }
            
            # 检查模型是否存在（两类接口返回结构不同）
            data = response.json()
            if provider == 'ollama':
                models = [m['name'] for m in data.get('models', [])]
            else:
                # OpenAI 兼容接口返回 {"data": [{"id": "model-name"}, ...]}
                models = [m.get('id', '') for m in data.get('data', [])]
            model_available = any(model in m for m in models)
            
            # 获取版本（仅 ollama 提供 /api/version）
            version = 'unknown'
            if provider == 'ollama':
                version_response = requests.get(f"{service_url}/api/version", timeout=timeout, headers=probe_headers)
                version = version_response.json().get('version', 'unknown') if version_response.status_code == 200 else 'unknown'
            
            return model_available, provider, model, {
                "reachable": True,
                "url": service_url,
                "model_available": model_available,
                "version": version,
                "expected_version": expected_version,
                "all_models": models[:5]  # 仅返回前 5 个避免过长
            }
            
        except requests.exceptions.RequestException as e:
            return False, provider, model, {"reachable": False, "error": str(e)}
    
    # provider == 在线 API: 校验配置完整性
    else:
        base_url = provider_cfg.get('base_url', '').strip()
        api_key = provider_cfg.get('api_key', '').strip()
        
        # 校验必需字段
        config_valid = True
        issues = []
        
        if not base_url:
            config_valid = False
            issues.append("base_url 为空")
        
        if not api_key:
            config_valid = False
            issues.append("api_key 为空")
        elif api_key.startswith('sk-xxx') or len(api_key) < 10:
            config_valid = False
            issues.append("api_key 疑似占位符")
        
        if not model:
            config_valid = False
            issues.append("model 为空")
        
        details = {
            # reachable 统一表示"模型服务是否就绪"：在线 provider 即配置校验是否通过。
            # 手机端读取 model_service.reachable 判断可用性，与 ollama 分支保持一致。
            "reachable": config_valid,
            "config_valid": config_valid,
            "base_url": base_url if base_url else "(未配置)",
            "api_key_configured": bool(api_key and len(api_key) >= 10),
            "model_configured": bool(model),
        }
        
        if issues:
            details["issues"] = issues
        
        return config_valid, provider, model, details


def measure_zerotier_rtt() -> int:
    """
    测量 ZeroTier 网络延迟（模拟）
    实际场景中可以 ping 手机端或测试端点
    """
    # 简化实现：返回固定值
    # 实际可以通过 ping 或 echo 端点测量
    return 50  # 毫秒


# ==========================================
# 3. FastAPI 应用
# ==========================================

# 解析命令行参数
parser = argparse.ArgumentParser(description='PhotoArrange API Server')
parser.add_argument('--config', default='api_config.yaml', help='配置文件路径')
parser.add_argument('--port', type=int, help='覆盖配置文件中的端口')
parser.add_argument('--run-api-task', dest='run_api_task', help='内部参数：子进程模式执行流水线')
args, _ = parser.parse_known_args()

# ==========================================
# 子进程模式：执行流水线（内部调用，用户不直接使用）
# ==========================================
if args.run_api_task:
    # 子进程模式：调用 run_api_pipeline 执行流水线
    import run_api_pipeline
    run_api_pipeline.main()
    sys.exit(0)

# 加载配置
try:
    config = load_api_config(args.config)
except Exception as e:
    # 服务环境下 stdout 可能是 NullStream，写文件兜底
    try:
        _err_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(_err_dir, "api_startup_error.log"), "w", encoding="utf-8") as _f:
            _f.write(f"加载 api_config 失败: {e}\n")
    except Exception:
        pass
    sys.exit(1)

# 本地推理服务列表（配置化）：以 api_config.yaml 的 local_providers 为准，
# 缺省回退到内置 DEFAULT_LOCAL_PROVIDERS（向后兼容旧配置文件）。
# 接入新本地服务（如 New-API）只需在 api_config.yaml 的 local_providers 里
# 加上 provider key，无需改代码/重编译。
_local_providers_cfg = config.get('local_providers')
if isinstance(_local_providers_cfg, list) and _local_providers_cfg:
    LOCAL_PROVIDERS = [str(p).strip() for p in _local_providers_cfg if str(p).strip()]
else:
    LOCAL_PROVIDERS = DEFAULT_LOCAL_PROVIDERS

# 加载主 pipeline_config（供健康检查读取 provider_configs）
import pipeline_config_loader as cfg_loader
try:
    pipeline_config = cfg_loader.load_config()
except Exception as e:
    try:
        _err_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(_err_dir, "api_startup_error.log"), "a", encoding="utf-8") as _f:
            _f.write(f"加载 pipeline_config 失败: {e}\n")
    except Exception:
        pass
    sys.exit(1)

# 设置日志
log_config = config.get('logging', {})
log_level = getattr(logging, log_config.get('level', 'INFO'))
log_file = log_config.get('file', 'logs/api_server.log')

# 日志文件路径也要相对于 exe 所在目录
if not os.path.isabs(log_file):
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    log_file = os.path.join(base_dir, log_file)

os.makedirs(os.path.dirname(log_file), exist_ok=True)

# 构建日志 handlers（服务环境下不用 StreamHandler）
# 使用 RotatingFileHandler：单文件上限 10MB，保留 5 份，防异常洪泛撑爆磁盘。
from logging.handlers import RotatingFileHandler as _RotatingFileHandler
handlers = [_RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding='utf-8')]
# 只有在非服务环境（有真实 stdout）时才添加控制台输出
if hasattr(sys.stdout, 'write') and sys.stdout.__class__.__name__ != 'NullStream':
    handlers.append(logging.StreamHandler())

logging.basicConfig(
    level=log_level,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=handlers
)
logger = logging.getLogger(__name__)

# 初始化 FastAPI
# 公网部署加固：关闭交互式文档与 openapi schema 端点，减少攻击面。
app = FastAPI(
    title="PhotoArrange API",
    description="手机端远程照片分析服务",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# 说明：本服务仅供手机端（原生 HTTP 客户端）访问，不存在 Web 前端。
# CORS 只对浏览器生效，对手机端无意义；开放式 CORS（allow_origins=["*"]
# + allow_credentials=True）本身是无效且危险的组合，故不再注册 CORS 中间件。

# ==========================================
# 读取安全/限流相关配置（带默认值兜底）
# ==========================================
_storage_cfg = config.get('storage', {})
MAX_CONCURRENT_TASKS = int(_storage_cfg.get('max_concurrent_tasks', 1))
MAX_QUEUE_SIZE = int(_storage_cfg.get('max_queue_size', 2))
# 系统总容量 = 并发数 + 队列上限，超出则拒绝创建新任务（准入控制）
MAX_INFLIGHT_TASKS = MAX_CONCURRENT_TASKS + MAX_QUEUE_SIZE
MAX_UPLOAD_CHUNK_BYTES = int(_storage_cfg.get('max_upload_chunk_mb', 20)) * 1024 * 1024
MAX_TOTAL_CHUNKS = int(_storage_cfg.get('max_total_chunks', 500))
MAX_PHOTOS = int(_storage_cfg.get('max_photos', 2000))

# 认证 Token
AUTH_TOKEN = config['server'].get('auth_token', '')

# 公网部署强制要求：auth_token 必须配置，否则所有接口裸奔。缺省拒绝启动。
if not AUTH_TOKEN or len(str(AUTH_TOKEN).strip()) < 16:
    logger.critical("拒绝启动：server.auth_token 未配置或过短（要求 >= 16 字符）。"
                    "公网部署下认证 Token 是唯一的接口保护，不允许为空。")
    try:
        _err_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(_err_dir, "api_startup_error.log"), "a", encoding="utf-8") as _f:
            _f.write("拒绝启动：server.auth_token 未配置或过短（要求 >= 16 字符）\n")
    except Exception:
        pass
    sys.exit(1)

# 初始化管理器
task_manager = TaskManager(
    config['storage']['base_dir'],
    max_photos=MAX_PHOTOS,
    max_unzip_bytes=int(_storage_cfg.get('max_unzip_bytes', 500 * 1024 * 1024)),
    max_total_upload_bytes=int(_storage_cfg.get('max_total_upload_bytes', 10 * 1024 * 1024 * 1024)),
)
pipeline_runner = PipelineRunner(config, task_manager)

# 每任务并发上传分块上限（防 token 持有者并发发大量 upload_chunk 打爆内存）。
# 信号量随任务创建而建，任务终态时清理（见 _release_chunk_sem）。
import asyncio as _asyncio
_TASK_MAX_CONCURRENT_CHUNKS = 4
_task_chunk_sems: dict = {}


def _get_chunk_sem(task_id: str) -> _asyncio.Semaphore:
    """获取（或创建）指定任务的并发上传信号量。"""
    sem = _task_chunk_sems.get(task_id)
    if sem is None:
        sem = _asyncio.Semaphore(_TASK_MAX_CONCURRENT_CHUNKS)
        _task_chunk_sems[task_id] = sem
    return sem


def _release_chunk_sem(task_id: str):
    """任务终态时清理信号量，防字典无限增长。"""
    _task_chunk_sems.pop(task_id, None)

# 启动对账：服务刚启动时内存态为空，磁盘上残留的在途任务（多为上一实例被杀/
# 重启遗留的 running/queued）一律作废并清理数据，实现"启动即白纸"，防止僵尸
# 任务永久占满在途配额导致新任务被误判"队列已满"。
try:
    task_manager.reconcile_orphaned_tasks(pipeline_runner.get_live_task_ids())
except Exception as _e:
    logger.warning(f"启动对账失败（忽略，不影响服务启动）: {_e}")


# ==========================================
# 4. 认证中间件（健康检查豁免认证）
# ==========================================
# 访问控制由 socket 级绑定实现（见启动逻辑）：只在指定接口 IP 上监听，
# 未绑定的接口（如公网 IP）根本无监听 socket，连接被直接拒绝，无任何响应。
# 因此这里无需再做来源 IP 白名单。auth_token 保护除 /api/health 外的接口。

@app.middleware("http")
async def verify_auth_token(request: Request, call_next):
    """验证认证 Token（仅 /api/health 免认证，剥离内部拓扑供探测）"""
    if request.url.path == "/api/health":
        return await call_next(request)
    
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.replace("Bearer ", "").strip()
    
    # 恒定时间比较，避免时序攻击泄露 Token 长度/前缀信息。
    # AUTH_TOKEN 已在启动时强制校验非空，此处无需再判 "AUTH_TOKEN and"。
    if not secrets.compare_digest(token, AUTH_TOKEN):
        return JSONResponse(
            status_code=401,
            content={"error": "Unauthorized", "error_code": "invalid_token",
                     "message": "无效的认证 Token"}
        )
    
    return await call_next(request)


# ==========================================
# 5. API 路由
# ==========================================

@app.get("/api/health")
async def health_check():
    """
    健康检查（免认证）：仅返回服务存活状态，不泄露内部拓扑。
    
    公网部署下该端点对所有人开放，故剥离 provider/model/内网URL/版本等信息，
    防止攻击者借免认证端点侦察内网结构与模型版本。详细状态见
    /api/health/detailed（需认证）。
    """
    return {
        "status": "ok",
        "timestamp": time.time()
    }


@app.get("/api/health/detailed")
async def health_check_detailed():
    """
    详细健康检查（需认证）：返回模型服务状态 + 网络延迟。

    供已携带 token 的手机端 APP 调用，用于配置保存前/上传前探测服务与模型可用性。
    认证由全局中间件保证（该路径不在豁免列表）。
    """
    model_ok, provider, model, model_details = check_model_health(config, pipeline_config)
    rtt_ms = measure_zerotier_rtt()
    
    status = "ok" if model_ok else "degraded"
    warning = rtt_ms > 500
    
    return {
        "status": status,
        "provider": provider,
        "model": model,
        "model_service": model_details,
        "network": {
            "rtt_ms": rtt_ms,
            "warning": warning,
            "message": f"网络延迟较高 ({rtt_ms}ms)，上传可能较慢" if warning else "网络状况良好"
        },
        "timestamp": time.time()
    }


@app.post("/api/verify_token")
async def verify_token():
    """
    验证认证 Token 是否有效

    专门供客户端在配置保存前、上传前验证 token。
    该接口受认证中间件保护：
      - Token 有效：返回 200 + {"valid": true}
      - Token 无效：中间件拦截返回 401
    后续可扩展为返回 token 权限、过期时间等信息。
    """
    return {
        "valid": True,
        "message": "Token 有效"
    }


@app.post("/api/upload_chunk")
async def upload_chunk(
    task_id: Optional[str] = Form(None),
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),
    sha256: str = Form(...),
    file: UploadFile = File(...)
):
    """
    分块上传 + SHA256 校验 + zip 魔数校验
    首次上传时 task_id 为空，服务端创建新任务并返回 task_id

    安全设计：
      - 流式落盘：1MB 缓冲写临时文件，到 MAX_UPLOAD_CHUNK_BYTES 即中止（防整块入内存 DoS）
      - zip 魔数校验：首 4 字节须为 PK\\x03\\x04，非 zip 立即拒绝（防上传垃圾撑爆磁盘）
      - 每任务并发上限：同一 task_id 的 in-flight chunk 请求不超过 4 个
    """
    import tempfile
    tmp_path = None
    try:
        # 创建或获取任务（需先有 task_id 才能确定临时文件目录）
        if not task_id:
            # total_chunks sanity 校验
            if not isinstance(total_chunks, int) or total_chunks <= 0 or total_chunks > MAX_TOTAL_CHUNKS:
                raise _biz_error(400, "invalid_total_chunks",
                                 f"分块总数非法（须 1~{MAX_TOTAL_CHUNKS}）")
            # 准入控制：系统在途任务数达到总容量则拒绝创建，一个字节都不落盘。
            if task_manager.count_inflight_tasks() >= MAX_INFLIGHT_TASKS:
                raise _biz_error(503, "server_busy",
                                 "服务器繁忙，当前处理队列已满，请稍后再试")
            task_id = f"task_{secrets.token_hex(8)}_{int(time.time())}"
            task_manager.create_task(task_id, total_chunks)
            logger.info(f"创建新任务: {task_id}, 总块数: {total_chunks}")
        else:
            _validate_task_id(task_id)

        # 每任务并发上传上限（信号量在任务终态时清理）
        sem = _get_chunk_sem(task_id)
        async with sem:
            # 流式落盘到临时文件（同 task 的 chunks 目录，确保 rename 跨文件系统安全）
            chunks_dir = task_manager.get_chunks_dir(task_id)
            tmp_fd, tmp_name = tempfile.mkstemp(suffix=".tmp", dir=str(chunks_dir))
            tmp_path = Path(tmp_name)
            os.close(tmp_fd)

            total = 0
            sha = hashlib.sha256()
            magic_checked = False
            with open(tmp_path, 'wb') as tmp:
                while True:
                    buf = await file.read(1024 * 1024)  # 1MB 一块
                    if not buf:
                        break
                    # zip 魔数校验：首 4 字节须为 PK\x03\x04（仅校验首个缓冲）
                    if not magic_checked:
                        if buf[:4] != b'PK\x03\x04':
                            tmp.close()
                            tmp_path.unlink(missing_ok=True)
                            raise _biz_error(400, "invalid_chunk_format",
                                             "分块不是有效的 zip 数据")
                        magic_checked = True
                    total += len(buf)
                    if total > MAX_UPLOAD_CHUNK_BYTES:
                        tmp.close()
                        tmp_path.unlink(missing_ok=True)
                        raise _biz_error(413, "chunk_too_large",
                                         f"单个分块超过上限（{MAX_UPLOAD_CHUNK_BYTES // (1024*1024)}MB）",
                                         max_bytes=MAX_UPLOAD_CHUNK_BYTES)
                    sha.update(buf)
                    tmp.write(buf)

            # 空 body 也需拒绝（无 zip 魔数）
            if not magic_checked:
                tmp_path.unlink(missing_ok=True)
                raise _biz_error(400, "invalid_chunk_format", "分块数据为空")

            # 校验 SHA256
            if sha.hexdigest() != sha256:
                tmp_path.unlink(missing_ok=True)
                raise _biz_error(409, "sha256_mismatch", "分块校验失败，请重新上传该分块",
                                 expected=sha256, actual=sha.hexdigest())

            # 原子 rename 为正式 chunk 文件并更新任务状态
            task_manager.save_chunk_from_file(task_id, chunk_index, tmp_path)
            tmp_path = None  # 已被 rename，无需清理
            received_chunks = task_manager.get_received_chunks(task_id)

            logger.info(f"[{task_id}] 接收块 {chunk_index}/{total_chunks}, 已接收 {len(received_chunks)} 块")

            return {
                "task_id": task_id,
                "chunk_index": chunk_index,
                "accepted": True,
                "received_chunks": received_chunks
            }

    except HTTPException:
        raise
    except TaskError as e:
        raise _biz_error(400, e.error_code, e.message)
    except Exception as e:
        raise _internal_error(e)
    finally:
        # 异常路径下清理残留临时文件
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass


@app.get("/api/upload_status")
async def upload_status(task_id: str):
    """断点续传查询"""
    _validate_task_id(task_id)
    task = task_manager.get_task(task_id)
    if not task:
        raise _biz_error(404, "task_not_found", "任务不存在")
    
    return {
        "task_id": task_id,
        "received_chunks": task['received_chunks'],
        "total_chunks": task['total_chunks']
    }


@app.post("/api/finalize")
async def finalize(request: Request):
    """完成上传，启动流水线"""
    try:
        body = await request.json()
        task_id = body.get('task_id')
        # 手机端选择的提取档位（A 精华档 / B 纪念档），缺省用配置默认值
        extraction_level = str(body.get('extraction_level', '')).strip().upper()[:1]
        if extraction_level not in ('A', 'B', 'C'):
            extraction_level = None  # 交由 pipeline_runner 用配置默认值
        
        if not task_id:
            raise _biz_error(400, "task_id_required", "缺少 task_id")
        _validate_task_id(task_id)
        
        task = task_manager.get_task(task_id)
        if not task:
            raise _biz_error(404, "task_not_found", "任务不存在")
        
        # 检查是否所有块都已接收
        if len(task['received_chunks']) != task['total_chunks']:
            raise _biz_error(400, "incomplete_upload",
                             f"未接收完所有分块（{len(task['received_chunks'])}/{task['total_chunks']}），请继续上传")
        
        # 记录档位到任务（供 pipeline_runner 读取）
        if extraction_level:
            task_manager.update_progress(task_id, {"extraction_level": extraction_level})
            logger.info(f"[{task_id}] 手机端指定提取档位: {extraction_level}")
        
        # 安全解压所有块（内部做 Zip Slip / 炸弹 / 照片数上限防护）
        logger.info(f"[{task_id}] 解压分块到 photos 目录")
        task_manager.unzip_all_chunks(task_id)
        
        # 重新读取（unzip 后 photo_count 已更新）
        task = task_manager.get_task(task_id)
        
        # 标记为排队并入调度队列（调度器在有空闲槽时才真正启动流水线）
        task_manager.mark_queued(task_id)
        logger.info(f"[{task_id}] 入队等待调度")
        pipeline_runner.enqueue(task_id)
        # 上传阶段结束，释放该任务的并发上传信号量
        _release_chunk_sem(task_id)
        
        # 估算处理时间：根据 provider 类型区分（与 APP 端对齐）
        # 本地模型（ollama/lmstudio）：固定开销 56s + 4.6s/张
        # 在线 API：固定开销 26s + 1.2s/张
        current_provider = config.get('pipeline_overrides', {}).get('curator', {}).get('provider', 'ollama')
        if current_provider in LOCAL_PROVIDERS:
            estimated_time_sec = 56 + task['photo_count'] * 4.6
        else:
            estimated_time_sec = 26 + task['photo_count'] * 1.2
        estimated_time_min = int(estimated_time_sec / 60)
        queue_position = pipeline_runner.get_queue_position(task_id)
        
        return {
            "task_id": task_id,
            "status": "queued",
            "photo_count": task['photo_count'],
            "estimated_time_min": estimated_time_min,
            "queue_position": queue_position
        }
        
    except HTTPException:
        raise
    except TaskError as e:
        # 解压/照片数等业务错误：标记任务失败并返回明确原因
        task_manager.mark_failed(task_id, e.message)
        _release_chunk_sem(task_id)
        raise _biz_error(400, e.error_code, e.message)
    except Exception as e:
        raise _internal_error(e)


@app.get("/api/status/{task_id}")
async def get_status(task_id: str):
    """查询处理状态（从 task.json 读取）"""
    from datetime import datetime
    
    _validate_task_id(task_id)
    task = task_manager.get_task(task_id)
    if not task:
        raise _biz_error(404, "task_not_found", "任务不存在")
    
    # 僵尸任务检测：服务端重启后，遗留的 running 任务不会有流水线实际处理，
    # 心跳会停止更新。若心跳超过 60 秒未刷新，判定为已失效，自动标记为 failed。
    if task['status'] == 'running':
        hb = task.get('last_heartbeat')
        stale = False
        if hb:
            try:
                age = (datetime.now() - datetime.fromisoformat(hb)).total_seconds()
                if age > 60:
                    stale = True
            except Exception:
                stale = True
        else:
            # 无心跳字段（老任务或服务端重启前创建），用 updated_at 兜底判断
            try:
                age = (datetime.now() - datetime.fromisoformat(task['updated_at'])).total_seconds()
                if age > 60:
                    stale = True
            except Exception:
                stale = True
        
        if stale:
            logger.warning(f"[{task_id}] 检测到僵尸任务（心跳超时），标记为 failed")
            task_manager.mark_failed(task_id, "任务已失效：服务端可能已重启，请重新提交任务")
            task = task_manager.get_task(task_id)
    
    # 构建响应
    response = {
        "task_id": task_id,
        "status": task['status'],
    }
    
    if task['status'] == 'running':
        eta_sec = task.get('eta_sec', 0)

        # 格式化剩余时间为 "X小时Y分钟"（0 表示样本不足，前端显示"正在计算"）
        if eta_sec > 0:
            hours = eta_sec // 3600
            minutes = (eta_sec % 3600) // 60
            if hours > 0:
                eta_text = f"{hours}小时{minutes}分钟"
            elif minutes > 0:
                eta_text = f"{minutes}分钟"
            else:
                eta_text = "不到1分钟"
        else:
            eta_text = None

        response.update({
            "stage": task.get('stage'),
            "stage_name": task.get('stage_name'),
            "progress": task.get('progress_text'),
            "progress_percent": task.get('progress_percent', 0),
            "elapsed_sec": task.get('elapsed_sec', 0),
            "eta_sec": eta_sec,
            "eta_text": eta_text,                      # "X小时Y分钟"
            "finish_time": task.get('finish_time'),    # 预计完成时刻 "MM-DD HH:MM:SS"
        })
    elif task['status'] == 'completed':
        response.update({
            "delete_count": task.get('delete_count', 0),
            "keep_count": task.get('keep_count', 0),
            "result_url": f"/api/result/{task_id}"
        })
    elif task['status'] == 'failed':
        response.update({
            "error": task.get('error'),
            "partial_result": False
        })
    elif task['status'] == 'queued':
        # 排队中：附带排队位置（前面还有几个），供客户端展示
        pos = pipeline_runner.get_queue_position(task_id)
        response.update({"queue_position": pos})
    elif task['status'] == 'cancelled':
        response.update({
            "error": task.get('error', '用户已取消任务')
        })
    
    return response


@app.post("/api/cancel/{task_id}")
async def cancel_task(task_id: str):
    """
    取消任务（用户主动放弃）

    行为：
    1. 标记任务为 cancelled
    2. 若在等待队列中，从队列移除（释放调度名额）
    3. 杀死流水线子进程树（如果正在运行，用 Job Object 杀整树防孤儿）
    """
    _validate_task_id(task_id)
    task = task_manager.get_task(task_id)
    if not task:
        raise _biz_error(404, "task_not_found", "任务不存在")

    # 只能取消未结束的任务
    if task['status'] not in ('uploading', 'queued', 'running'):
        return {
            "task_id": task_id,
            "status": task['status'],
            "message": "任务已结束，无需取消"
        }

    prev_status = task['status']
    # 标记为 cancelled（先标记，让流水线监控线程感知并退出）
    task_manager.mark_cancelled(task_id)
    # 任务终止，释放并发上传信号量
    _release_chunk_sem(task_id)

    # 若任务尚在等待队列中，移除以释放调度名额
    pipeline_runner.remove_from_queue(task_id)

    # 杀死子进程（如果在运行）
    if prev_status == 'running':
        killed = pipeline_runner.kill_pipeline(task_id)
        if killed:
            logger.info(f"[{task_id}] 流水线子进程树已终止")
        else:
            logger.warning(f"[{task_id}] 未找到运行中的流水线子进程（可能已结束）")

    return {
        "task_id": task_id,
        "status": "cancelled",
        "message": "任务已取消"
    }


@app.get("/api/result/{task_id}")
async def get_result(task_id: str):
    """下载删除列表"""
    _validate_task_id(task_id)
    result_file = task_manager.get_result_file(task_id)
    if not result_file:
        raise _biz_error(404, "result_not_ready", "结果尚未就绪或任务不存在")
    
    return FileResponse(
        result_file,
        media_type="text/plain",
        filename=f"delete_list_{task_id}.txt"
    )


# ==========================================
# 7. 启动服务
# ==========================================

if __name__ == "__main__":
    import socket
    
    # 解析监听地址（向后兼容单值 host 和新格式 hosts 列表）
    hosts = config['server'].get('hosts')
    if hosts is None:
        # 向后兼容：单值 host
        host = config['server'].get('host', '0.0.0.0')
        hosts = [host]
    elif not isinstance(hosts, list):
        # 配置错误：hosts 不是列表
        logger.error("配置错误: server.hosts 必须是列表")
        sys.exit(1)
    
    port = args.port or config['server']['port']
    
    logger.info(f"=" * 60)
    logger.info(f"PhotoArrange API 服务启动")
    logger.info(f"监听地址: {', '.join(f'{h}:{port}' for h in hosts)}")
    logger.info(f"认证: {'已启用' if AUTH_TOKEN else '未启用（不推荐）'}")
    logger.info(f"存储目录: {config['storage']['base_dir']}")
    
    # 显示当前使用的模型 provider
    provider = config.get('pipeline_overrides', {}).get('curator', {}).get('provider', 'ollama')
    logger.info(f"模型 Provider: {provider}")
    logger.info(f"并发上限: {MAX_CONCURRENT_TASKS}，队列上限: {MAX_QUEUE_SIZE}，"
                f"总容量: {MAX_INFLIGHT_TASKS}")
    logger.info(f"=" * 60)

    # 后台清理线程：定期清理过期任务 + 释放僵尸上传任务名额
    import threading as _threading

    _retention_hours = config['storage'].get('task_retention_hours', 24)
    _upload_timeout_min = config['storage'].get('upload_timeout_min', 30)

    def _cleanup_loop():
        while True:
            try:
                task_manager.cleanup_old_tasks(_retention_hours, _upload_timeout_min)
                # 运行时僵尸回收：处理 running 但内存无存活记录的任务
                # （流水线子进程异常崩溃遗留），依据调度器内存态判定，零误杀。
                task_manager.reconcile_running_zombies(pipeline_runner.get_live_task_ids())
            except Exception as e:
                logger.warning(f"后台清理任务异常: {e}")
            time.sleep(300)  # 每 5 分钟清理一次

    _cleanup_thread = _threading.Thread(
        target=_cleanup_loop, name="task-cleanup", daemon=True)
    _cleanup_thread.start()
    logger.info(f"后台清理线程已启动（保留 {_retention_hours}h，上传超时 {_upload_timeout_min}min）")
    
    # 为每个地址创建并绑定 socket（socket 级多地址监听）
    # 开机自启时 ZeroTier 等虚拟网卡可能尚未就绪，导致 bind 报 WinError 10049
    # （WSAEADDRNOTAVAIL）。此处对尚未成功绑定的地址进行轮询重试：
    #   每 5 秒重试一次，最多等待 5 分钟（300 秒）。
    import time

    BIND_RETRY_INTERVAL = 5      # 每次重试间隔（秒）
    BIND_RETRY_TIMEOUT = 300     # 最长等待时间（秒）

    bound_sockets = {}           # host -> socket，记录已成功绑定的地址
    deadline = time.monotonic() + BIND_RETRY_TIMEOUT

    while True:
        for host in hosts:
            if host in bound_sockets:
                continue
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((host, port))
                sock.set_inheritable(True)
                bound_sockets[host] = sock
                logger.info(f"成功绑定 socket: {host}:{port}")
            except OSError as e:
                logger.warning(f"无法绑定 {host}:{port}: {e}（稍后重试）")

        # 全部地址均已绑定成功，结束重试
        if len(bound_sockets) == len(hosts):
            break

        # 超时判断：仍有未绑定地址，但已到达最长等待时间则停止
        if time.monotonic() >= deadline:
            pending = [h for h in hosts if h not in bound_sockets]
            logger.warning(
                f"等待 {BIND_RETRY_TIMEOUT} 秒后仍无法绑定以下地址，放弃重试: "
                f"{', '.join(f'{h}:{port}' for h in pending)}"
            )
            break

        logger.info(
            f"仍有地址未绑定，{BIND_RETRY_INTERVAL} 秒后重试"
            f"（已用时 {int(time.monotonic() - (deadline - BIND_RETRY_TIMEOUT))} 秒 / "
            f"上限 {BIND_RETRY_TIMEOUT} 秒）"
        )
        time.sleep(BIND_RETRY_INTERVAL)

    # 保持原有变量名，供后续 uvicorn 使用
    sockets = list(bound_sockets.values())

    if not sockets:
        logger.error("所有地址绑定失败，无法启动服务")
        sys.exit(1)
    
    # 配置 uvicorn 日志：重定向到文件，避免写 stdout 崩溃
    # 计算 uvicorn 访问日志路径（与 api_server.log 同目录）
    uvicorn_log_dir = os.path.dirname(log_file)
    uvicorn_access_log = os.path.join(uvicorn_log_dir, "uvicorn_access.log")
    uvicorn_error_log = os.path.join(uvicorn_log_dir, "uvicorn_error.log")
    
    # 自定义 uvicorn 日志配置（全部写文件，不用 stdout）
    uvicorn_log_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            },
            "access": {
                "format": "%(asctime)s - %(levelname)s - %(message)s",
            },
        },
        "handlers": {
            "default": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": uvicorn_error_log,
                "formatter": "default",
                "encoding": "utf-8",
                "maxBytes": 10 * 1024 * 1024,
                "backupCount": 5,
            },
            "access": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": uvicorn_access_log,
                "formatter": "access",
                "encoding": "utf-8",
                "maxBytes": 10 * 1024 * 1024,
                "backupCount": 5,
            },
        },
        "loggers": {
            "uvicorn": {
                "handlers": ["default"],
                "level": "INFO",
                "propagate": False,
            },
            "uvicorn.error": {
                "handlers": ["default"],
                "level": "INFO",
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": ["access"],
                "level": "INFO",
                "propagate": False,
            },
        },
    }
    
    logger.info(f"uvicorn 访问日志: {uvicorn_access_log}")
    logger.info(f"uvicorn 错误日志: {uvicorn_error_log}")
    
    # 使用预先绑定的 sockets 启动 uvicorn（单进程监听多个地址）
    uvicorn_config = uvicorn.Config(
        app,
        log_config=uvicorn_log_config,
        access_log=True
    )
    server = uvicorn.Server(uvicorn_config)
    server.run(sockets=sockets)
