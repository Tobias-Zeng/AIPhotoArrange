# api_pipeline_runner.py
"""
封装调用现有流水线，source_dir 指向临时缩略图目录。

核心逻辑：
1. 生成临时 pipeline_config（继承主配置，覆盖 source_dir/target_dir/profile）
2. 设置环境变量 API_TASK_ID（供 stage02 写日志时关联 task）
3. 后台线程执行：
   - stage01a.main()  ->  生成 batches json
   - stage01b.main()  ->  回写 GPS
   - stage02.main()   ->  生成归档目录 + 删除列表 txt
4. 实时读取 02_progress_remote_{task_id}.json 更新 task 进度
5. 完成后读取删除列表 txt，存为 task 结果

注意：
- 不跑 03a/03b（远程分析只需删除列表）
- target_dir 设为临时目录下的 output/ 子目录（归档目录不持久保留）
- profile 用 "remote_{task_id}" 隔离中间文件
- 流水线跑完后清理临时目录（保留删除列表 txt）
"""

import os
import sys
import json
import yaml
import time
import ctypes
import subprocess
import threading
import logging
from ctypes import wintypes
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict
from collections import deque

import pipeline_config_loader as cfg_loader


logger = logging.getLogger(__name__)


# ==========================================
# Windows Job Object 绑定（杀整棵子进程树，防止孤儿进程）
# ------------------------------------------
# 严格照搬 gui/pipeline_runner.py 的成熟方案（见 v2.3.2 孤儿进程 bug 修复）。
# run_api_pipeline 子进程会派生 stage01a/01b/02 子进程，若只对顶层 exe 调
# terminate()，Windows 不递归杀子进程，stage02 会变孤儿继续跑、并发写输出
# 目录与进度文件，产生重复照片与脏数据。
#
# 方案：Job Object 绑定子进程树，TerminateJobObject 一刀切整树；
# 设 JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE 后，主服务崩溃时整树也随句柄关闭回收。
# ==========================================
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

_kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
_kernel32.CreateJobObjectW.restype = wintypes.HANDLE
_kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
_kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
_kernel32.TerminateJobObject.restype = wintypes.BOOL
_kernel32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE, wintypes.INT, wintypes.LPVOID, wintypes.DWORD,
]
_kernel32.SetInformationJobObject.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_void_p),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _create_job_with_kill_on_close():
    """
    创建带 JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE 的 Job Object（照搬 GUI）。
    返回 Job 句柄；任一步失败返回 None（调用方降级 proc.terminate()）。
    """
    try:
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _kernel32.SetInformationJobObject(
            job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info), ctypes.sizeof(info),
        )
        if not ok:
            _kernel32.CloseHandle(job)
            return None
        return job
    except Exception:
        return None


class PipelineRunner:
    def __init__(self, api_config: Dict, task_manager):
        self.api_config = api_config
        self.task_manager = task_manager
        self.base_dir = Path(api_config['storage']['base_dir'])
        self.pipeline_overrides = api_config.get('pipeline_overrides', {})
        
        # 加载主 pipeline_config（从 exe 内嵌或当前目录读取）
        self.main_config = cfg_loader.load_config()
        
        # 运行中的子进程映射（task_id -> (Popen, job_handle)），供 kill_pipeline 查表
        self._running_processes = {}
        self._proc_lock = threading.Lock()

        # ==========================================
        # 并发调度器（可配置并发数，当前默认 1）
        # ------------------------------------------
        # 设计为 N 并发：max_concurrent_tasks 控制同时运行的流水线数量。
        # finalize 不再直接启动流水线，而是 enqueue() 入队；调度器在有空闲槽时
        # 才真正启动。每条流水线结束（完成/失败/取消）后释放槽并唤醒调度器。
        # 当前配置 N=1，但代码结构支持任意 N，放大只需改配置无需改代码。
        # ==========================================
        storage_cfg = api_config.get('storage', {})
        self.max_concurrent = int(storage_cfg.get('max_concurrent_tasks', 1))
        # 等待队列（FIFO），元素为 task_id
        self._queue = deque()
        # 当前运行中的任务集合（用于计数与排队位置计算）
        self._active = set()
        # 调度锁 + 条件变量（保护 _queue / _active）
        self._sched_lock = threading.Lock()
        self._sched_cond = threading.Condition(self._sched_lock)
        # 启动后台调度线程（守护线程，随主进程退出）
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop, name="pipeline-scheduler", daemon=True)
        self._scheduler_thread.start()

    def enqueue(self, task_id: str):
        """
        将任务加入等待队列并唤醒调度器。
        任务应已处于 queued 状态（由 API 层 mark_queued）。
        """
        with self._sched_cond:
            if task_id not in self._queue and task_id not in self._active:
                self._queue.append(task_id)
                logger.info(f"[{task_id}] 已入队，当前队列长度 {len(self._queue)}，"
                            f"运行中 {len(self._active)}")
            self._sched_cond.notify()

    def get_live_task_ids(self) -> set:
        """
        返回内存中"存活"的任务 id 集合：等待队列 + 运行中。
        加锁保证与调度器的 popleft/_active.add 原子操作一致，
        供任务对账/僵尸回收判定（磁盘在途但不在此集合 = 孤儿/僵尸）。
        """
        with self._sched_lock:
            return set(self._queue) | set(self._active)

    def get_queue_position(self, task_id: str) -> Optional[int]:
        """返回任务在等待队列中的位置（1 表示下一个执行）；不在队列返回 None。"""
        with self._sched_lock:
            if task_id in self._queue:
                return list(self._queue).index(task_id) + 1
            return None

    def remove_from_queue(self, task_id: str) -> bool:
        """从等待队列移除任务（用于取消尚未开始运行的任务）。返回是否移除成功。"""
        with self._sched_lock:
            if task_id in self._queue:
                self._queue.remove(task_id)
                logger.info(f"[{task_id}] 已从等待队列移除（取消）")
                return True
            return False

    def _scheduler_loop(self):
        """
        后台调度循环：有空闲槽且队列非空时，出队并启动流水线。
        每条流水线在结束时调用 _release_slot() 释放槽并唤醒本循环。
        """
        while True:
            with self._sched_cond:
                # 等待：直到有空闲槽且队列非空
                while not (len(self._active) < self.max_concurrent and self._queue):
                    self._sched_cond.wait()
                task_id = self._queue.popleft()
                # 跳过已被取消的任务（取消可能发生在入队后、调度前）
                task = self.task_manager.get_task(task_id)
                if not task or task['status'] != 'queued':
                    logger.info(f"[{task_id}] 出队时状态非 queued（{task.get('status') if task else 'None'}），跳过")
                    continue
                self._active.add(task_id)
            # 在锁外启动流水线线程
            thread = threading.Thread(
                target=self._run_pipeline, args=(task_id,), daemon=True)
            thread.start()

    def _release_slot(self, task_id: str):
        """流水线结束后释放运行槽并唤醒调度器拉取下一个任务。"""
        with self._sched_cond:
            self._active.discard(task_id)
            self._sched_cond.notify()

    def start_pipeline(self, task_id: str):
        """
        兼容旧接口：直接后台启动流水线（不经调度器）。
        新流程应使用 enqueue()。保留此方法供内部/测试直接调用。
        """
        thread = threading.Thread(target=self._run_pipeline, args=(task_id,), daemon=True)
        thread.start()
    
    def kill_pipeline(self, task_id: str) -> bool:
        """
        杀死指定任务的流水线子进程树（用户主动取消时调用）。

        严格照搬 GUI 停止范式：优先 TerminateJobObject 杀整树，
        兜底 proc.terminate() 杀顶层。返回 True=找到并终止，False=未找到。
        """
        with self._proc_lock:
            entry = self._running_processes.get(task_id)
        if not entry:
            return False
        proc, job = entry

        # 优先用 Job Object 杀整树（含 run_api_pipeline 派生的所有 stage 子进程）
        if job:
            try:
                _kernel32.TerminateJobObject(job, 1)
            except Exception as e:
                logger.warning(f"[{task_id}] TerminateJobObject 失败: {e}")
        # 兜底：terminate 顶层进程
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception as e:
                logger.warning(f"[{task_id}] proc.terminate 失败: {e}")
        return True
    
    def _run_pipeline(self, task_id: str):
        """执行流水线的核心逻辑"""
        try:
            self.task_manager.mark_running(task_id)
            
            task_dir = self.base_dir / "tasks" / task_id
            photos_dir = task_dir / "photos"
            output_dir = task_dir / "output"
            
            # 读取手机端指定的提取档位（finalize 时写入 task）
            task = self.task_manager.get_task(task_id)
            extraction_level = (task or {}).get('extraction_level')
            
            # 生成临时 pipeline_config
            temp_config_path = self._generate_temp_config(
                task_id, photos_dir, output_dir, extraction_level
            )
            
            # 设置环境变量（stage 脚本通过环境变量读取配置）
            env = os.environ.copy()
            env['PIPELINE_CONFIG'] = str(temp_config_path)
            env['PIPELINE_PROFILE'] = f"remote_{task_id}"
            env['API_TASK_ID'] = task_id
            
            logger.info(f"[{task_id}] 开始流水线处理（subprocess 自调用）")
            start_time = time.time()
            
            # 02 阶段进度监控线程（子进程会写进度文件，本进程读取）
            progress_thread = threading.Thread(
                target=self._monitor_progress,
                args=(task_id,),
                daemon=True
            )
            progress_thread.start()
            
            # subprocess 自调用执行流水线（照搬 GUI 范式）。
            # 每个任务是独立子进程，stage 模块在子进程中干净 import，
            # 日志/配置/状态天然隔离，无需 importlib.reload() 的脏 hack。
            # 返回 Popen + Job 句柄，注册进程表供 kill_pipeline 取消。
            proc, job = self._run_pipeline_subprocess(task_id, temp_config_path)
            with self._proc_lock:
                self._running_processes[task_id] = (proc, job)
            
            # 等待子进程结束（正常完成 / 用户取消杀树 / 崩溃）
            try:
                returncode = proc.wait(timeout=7200)  # 2 小时超时保护
            except subprocess.TimeoutExpired:
                logger.error(f"[{task_id}] 子进程超时（2 小时），终止")
                self.kill_pipeline(task_id)
                raise Exception("Pipeline execution timeout (2 hours)")
            
            elapsed = time.time() - start_time
            
            # 检查是否被用户取消（kill_pipeline 已把状态标记为 cancelled）
            cur = self.task_manager.get_task(task_id)
            if cur and cur['status'] == 'cancelled':
                logger.info(f"[{task_id}] 流水线已被用户取消（耗时 {elapsed/60:.1f} 分钟）")
                return
            
            if returncode != 0:
                logger.error(f"[{task_id}] 子进程失败 (exit code {returncode})")
                raise Exception(f"Pipeline subprocess failed with exit code {returncode}")
            
            logger.info(f"[{task_id}] 流水线完成，耗时 {elapsed/60:.1f} 分钟")
            
            # 读取删除列表
            delete_list_path = self._find_delete_list(task_id)
            if not delete_list_path:
                # 兜底：stage02 可能因异常未生成文件，创建一个空删除列表（全精华场景）
                logger.warning(f"[{task_id}] 未找到删除列表文件，创建空列表（假定全精华）")
                result_file = f"delete_list_{task_id}.txt"
                result_path = task_dir / result_file
                with open(result_path, 'w', encoding='utf-8') as f:
                    f.write("#MATCH_MODE=DATETIME\n")
                delete_count, keep_count = 0, self._count_photos(photos_dir)
            else:
                # 统计删除/保留数量
                delete_count, keep_count = self._count_results(task_id, delete_list_path, photos_dir)
                
                # 复制删除列表到任务目录
                result_file = f"delete_list_{task_id}.txt"
                result_path = task_dir / result_file
                import shutil
                shutil.copy(delete_list_path, result_path)
            
            self.task_manager.mark_completed(task_id, result_file, delete_count, keep_count)
            
        except Exception as e:
            logger.error(f"[{task_id}] 流水线失败: {e}", exc_info=True)
            # 已被取消的任务不覆盖为 failed
            cur = self.task_manager.get_task(task_id)
            if not (cur and cur['status'] == 'cancelled'):
                self.task_manager.mark_failed(task_id, str(e))
        finally:
            # 清理进程映射 + 关闭 Job Object 句柄（避免内核对象泄漏）
            with self._proc_lock:
                entry = self._running_processes.pop(task_id, None)
            if entry:
                _, job_handle = entry
                if job_handle:
                    try:
                        _kernel32.CloseHandle(job_handle)
                    except Exception:
                        pass
            # 释放调度槽，唤醒调度器拉取下一个排队任务（无论成功/失败/取消）
            self._release_slot(task_id)
    
    def _generate_temp_config(self, task_id: str, photos_dir: Path, output_dir: Path,
                              extraction_level: str = None) -> Path:
        """生成临时 pipeline_config.yaml"""
        config = dict(self.main_config)
        
        # 覆盖关键路径
        config['profile'] = f"remote_{task_id}"
        if 'common' not in config:
            config['common'] = {}
        config['common']['source_dir'] = str(photos_dir)
        config['common']['target_dir'] = str(output_dir)
        
        # 深合并 pipeline_overrides
        config = cfg_loader._deep_merge(config, self.pipeline_overrides)
        
        # 手机端指定档位覆盖 pipeline_overrides 的默认档位
        if extraction_level:
            if 'curator' not in config:
                config['curator'] = {}
            config['curator']['extraction_level'] = extraction_level
        
        # 写入临时配置文件
        temp_config_path = self.base_dir / "tasks" / task_id / f"config_remote_{task_id}.yaml"
        with open(temp_config_path, 'w', encoding='utf-8') as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
        
        return temp_config_path
    
    def _run_pipeline_subprocess(self, task_id: str, temp_config_path: Path):
        """
        启动子进程执行流水线，返回 (Popen, job_handle)。
        
        照搬 GUI pipeline_runner.py 范式：
        1. 创建 Job Object（设 KILL_ON_JOB_CLOSE）
        2. Popen 启动子进程
        3. AssignProcessToJobObject 绑定子进程树
        4. 返回 (proc, job)，供调用方 wait() + 注册进程表供 kill_pipeline 查询
        
        调用方式：
          PhotoArrangeAPI.exe --run-api-task <task_id>
        
        环境变量传递：
          PIPELINE_CONFIG - 临时配置路径
          PIPELINE_PROFILE - remote_{task_id}
          API_TASK_ID - 任务 ID
        """
        # 确定可执行文件路径（照搬 GUI/PipelineRunner 范式）
        if "__compiled__" in globals():
            # Nuitka 编译模式：sys.argv[0] 指向 PhotoArrangeAPI.exe
            exe_path = sys.argv[0]
        else:
            # 开发模式：用 python 解释器
            exe_path = sys.executable
        
        # 构建命令行
        if "__compiled__" in globals():
            # 编译模式：直接调用 exe
            cmd = [exe_path, "--run-api-task", task_id]
        else:
            # 开发模式：python api_server.py --run-api-task <task_id>
            cmd = [exe_path, "api_server.py", "--run-api-task", task_id]
        
        # 设置环境变量（子进程从环境变量读取配置）
        env = os.environ.copy()
        env['PIPELINE_CONFIG'] = str(temp_config_path)
        env['PIPELINE_PROFILE'] = f"remote_{task_id}"
        env['API_TASK_ID'] = task_id
        # 传递 base_dir，供 run_api_pipeline.py 初始化 TaskManager
        env['API_BASE_DIR'] = str(self.base_dir)
        
        logger.info(f"[{task_id}] 启动子进程: {' '.join(cmd)}")
        
        # 创建 Job Object（照搬 GUI，防止孤儿进程）
        job = _create_job_with_kill_on_close()
        if not job:
            logger.warning(f"[{task_id}] Job Object 创建失败，已降级（可能产生孤儿进程）")
        
        # 启动子进程
        proc = subprocess.Popen(
            cmd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace'
        )
        
        # 绑定子进程到 Job（照搬 GUI 代码）
        if job:
            try:
                # proc._handle 是 subprocess.Popen 的内部属性，指向子进程 Windows HANDLE
                ok = _kernel32.AssignProcessToJobObject(job, int(proc._handle))
                if not ok:
                    err = ctypes.get_last_error()
                    logger.warning(f"[{task_id}] Job Object 绑定失败 (err={err})，已降级")
                    _kernel32.CloseHandle(job)
                    job = None
            except Exception as e:
                logger.warning(f"[{task_id}] Job Object 绑定异常: {e}")
                try:
                    _kernel32.CloseHandle(job)
                except Exception:
                    pass
                job = None
        
        return proc, job
    
    def _monitor_progress(self, task_id: str):
        """监控流水线进度，从 02_progress_remote_{task_id}.json 读取。"""
        profile = f"remote_{task_id}"
        # 进度文件写在 cfg_loader.BASE_DIR（exe 同目录 / 源码同目录）
        progress_file = Path(cfg_loader.BASE_DIR) / f"02_progress_{profile}.json"
        
        last_update = None
        while True:
            time.sleep(5)  # 每 5 秒检查一次
            
            task = self.task_manager.get_task(task_id)
            if not task or task['status'] != 'running':
                break
            
            # 每轮循环刷新心跳：证明流水线子进程仍在运行，
            # 供 /api/status 判断任务是否为僵尸（服务端重启遗留的 running 任务）
            self.task_manager.update_heartbeat(task_id)
            
            if progress_file.exists():
                try:
                    with open(progress_file, 'r', encoding='utf-8') as f:
                        progress_data = json.load(f)
                    
                    # 避免重复更新
                    if progress_data != last_update:
                        # 提取进度信息
                        # 注意：stage02 写入的 completed_batches 是"已完成批次 ID 列表"，
                        # 不是整数计数，这里取 len 得到已完成批次数。
                        completed_raw = progress_data.get('completed_batches', 0)
                        completed = len(completed_raw) if isinstance(completed_raw, list) else completed_raw
                        total = progress_data.get('total_batches', 1)
                        percent = int(completed / total * 100) if total > 0 else 0
                        
                        update = {
                            'stage': '02',
                            'stage_name': 'LLM 精华挑选',
                            'progress_text': f"{completed}/{total} 批",
                            'progress_percent': percent,
                            'eta_sec': progress_data.get('eta_sec', 0),
                            'finish_time': progress_data.get('finish_time'),      # 预计完成时刻
                            'elapsed_sec': progress_data.get('elapsed_sec', 0),   # 已用时间
                        }
                        
                        self.task_manager.update_progress(task_id, update)
                        last_update = progress_data
                        
                except Exception as e:
                    logger.warning(f"[{task_id}] 读取进度失败: {e}")
    
    def _find_delete_list(self, task_id: str) -> Optional[Path]:
        """查找生成的删除列表文件（stage02 输出到 TARGET_DIR 同级，即 task_dir）"""
        task_dir = self.base_dir / "tasks" / task_id
        
        # 查找匹配的删除列表文件
        pattern = f"non_highlight_photos_remote_{task_id}_*.txt"
        matches = list(task_dir.glob(pattern))
        
        if matches:
            return matches[0]
        
        # 兜底：检查 cfg_loader.BASE_DIR（exe 同目录）
        base_matches = list(Path(cfg_loader.BASE_DIR).glob(pattern))
        if base_matches:
            return base_matches[0]
        
        return None
    
    def _count_photos(self, photos_dir: Path) -> int:
        """统计照片总数"""
        total_count = len(list(photos_dir.glob("*.jpg"))) + \
                      len(list(photos_dir.glob("*.jpeg"))) + \
                      len(list(photos_dir.glob("*.png")))
        return total_count
    
    def _count_results(self, task_id: str, delete_list_path: Path, photos_dir: Path) -> tuple:
        """统计删除和保留数量"""
        # 统计删除列表中的数量（跳过 #MATCH_MODE 开头的行）
        delete_count = 0
        with open(delete_list_path, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip() and not line.startswith('#'):
                    delete_count += 1
        
        # 统计照片总数
        total_count = self._count_photos(photos_dir)
        
        keep_count = max(0, total_count - delete_count)
        
        return delete_count, keep_count
