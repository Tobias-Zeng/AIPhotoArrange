# run_api_pipeline.py
"""
API 任务流水线执行器（被 api_server.exe 自调用的子进程）

用法：
  api_server.exe --run-api-task <task_id>
  
环境变量：
  PIPELINE_CONFIG - 临时配置路径（由父进程生成）
  PIPELINE_PROFILE - remote_{task_id}
  API_TASK_ID - 任务 ID
  
执行流程：
  1. 从环境变量读取任务信息
  2. 依次执行 stage01a → stage01b → stage02
  3. 各 stage 的日志/进度文件会自动隔离（通过 profile）
  4. 执行完毕后退出，父进程读取结果
"""

import sys
import os
import logging
from pathlib import Path


def setup_logging(task_id: str):
    """配置日志输出到任务专属日志文件"""
    # 重新配置 stdout/stderr 为 utf-8，避免 GBK 控制台无法编码 emoji 崩溃。
    # 服务环境下 stdout 可能被顶替为 NullStream（无 reconfigure），需先判断。
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    
    # 日志目录：exe 同级的 logs/ 目录
    import pipeline_config_loader as cfg_loader
    log_dir = Path(cfg_loader.BASE_DIR) / "logs"
    log_dir.mkdir(exist_ok=True)
    
    log_file = log_dir / f"api_pipeline_{task_id}.log"
    
    # 只输出到文件（utf-8）。子进程 stdout 由父进程 capture，不再挂 StreamHandler，
    # 避免 GBK 控制台编码错误。文件日志已足够排查问题。
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
        ]
    )
    
    logger = logging.getLogger(__name__)
    logger.info(f"=== API Pipeline started for task {task_id} ===")
    logger.info(f"PIPELINE_CONFIG: {os.environ.get('PIPELINE_CONFIG')}")
    logger.info(f"PIPELINE_PROFILE: {os.environ.get('PIPELINE_PROFILE')}")
    
    return logger


def main():
    """执行 API 任务流水线的主入口"""
    # 从环境变量读取任务 ID
    task_id = os.environ.get('API_TASK_ID')
    if not task_id:
        print("错误：未设置环境变量 API_TASK_ID", file=sys.stderr)
        sys.exit(1)
    
    logger = setup_logging(task_id)
    
    # 初始化 TaskManager（用于上报阶段状态）
    # base_dir 由父进程通过 API_BASE_DIR 环境变量传入（storage.base_dir），
    # 与 api_server.py 的 TaskManager 指向同一个 tasks 目录。
    from api_task_manager import TaskManager
    
    base_dir = os.environ.get('API_BASE_DIR')
    task_manager = None
    if base_dir and Path(base_dir).exists():
        try:
            task_manager = TaskManager(base_dir)
        except Exception as e:
            logger.warning(f"初始化 TaskManager 失败，阶段上报将跳过: {e}")
    else:
        logger.warning(f"未设置有效的 API_BASE_DIR（{base_dir}），阶段上报将跳过")
    
    try:
        # 依次执行 stage01a → stage01b → stage02
        # 这些 stage 模块在 import 时会从环境变量读取配置
        # 01a/01b 统一上报为 stage="01"，在 APP 端合并显示"预处理"
        stages = [
            ("stage01a_phash_chunker", "01", "预处理：图片去重聚类"),
            ("stage01b_geo_resolver_amap", "01", "预处理：GPS 位置解析"),
            ("stage02_aesthetic_curator", "02", "LLM 精华挑选")
        ]
        
        for module_name, stage_code, stage_name in stages:
            # 上报阶段开始（让 APP 立即看到当前处于哪个阶段）
            if task_manager is not None:
                try:
                    logger.info(f"上报阶段: {stage_code} - {stage_name}")
                    task_manager.update_stage_info(task_id, stage_code, stage_name)
                except Exception as e:
                    logger.warning(f"阶段上报失败（不影响处理）: {e}")
            
            logger.info(f"开始执行 {stage_code} - {stage_name}")
            
            import importlib
            module = importlib.import_module(module_name)
            module.main()
            
            logger.info(f"完成 {stage_code} - {stage_name}")
        
        logger.info(f"=== API Pipeline completed for task {task_id} ===")
        sys.exit(0)
        
    except Exception as e:
        logger.error(f"流水线执行失败: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
