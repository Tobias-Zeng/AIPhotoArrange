# api_task_manager.py
"""
任务状态磁盘持久化管理。

每个任务一个 JSON 文件：{data_dir}/tasks/{task_id}.json
防止 API 服务崩溃后丢失进度。
"""

import os
import re
import json
import time
import shutil
import logging
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# task_id 白名单：仅允许服务端生成的格式 task_<16位hex>_<unix秒>。
# 防止调用方传入含 ../ 的 task_id 拼进文件系统路径造成路径穿越。
_TASK_ID_RE = re.compile(r'^task_[0-9a-f]{16}_\d+$')


class TaskError(Exception):
    """业务级任务错误，携带稳定 error_code 供 API 层转成明确提示。"""
    def __init__(self, error_code: str, message: str):
        self.error_code = error_code
        self.message = message
        super().__init__(message)


class TaskManager:
    def __init__(self, base_dir: str, max_photos: int = 2000,
                 max_unzip_bytes: int = 500 * 1024 * 1024,
                 max_unzip_members: int = 2500,
                 max_total_upload_bytes: int = 10 * 1024 * 1024 * 1024):
        self.base_dir = Path(base_dir)
        self.tasks_dir = self.base_dir / "tasks"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.max_photos = max_photos
        self.max_unzip_bytes = max_unzip_bytes
        self.max_unzip_members = max_unzip_members
        # 单任务累计上传字节上限（防超大上传撑爆磁盘）。
        # 默认 10GB ≈ 500 块 × 20MB，与 MAX_TOTAL_CHUNKS × MAX_UPLOAD_CHUNK_BYTES 对齐。
        self.max_total_upload_bytes = max_total_upload_bytes

    def _task_dir(self, task_id: str) -> Path:
        """返回校验后的任务目录路径，防止路径穿越。"""
        if not _TASK_ID_RE.match(task_id or ''):
            raise TaskError("task_not_found", "任务不存在")
        # 二次防御：确保最终路径落在 tasks_dir 内
        d = (self.tasks_dir / task_id).resolve()
        if os.path.commonpath([str(d), str(self.tasks_dir.resolve())]) != str(self.tasks_dir.resolve()):
            raise TaskError("task_not_found", "任务不存在")
        return self.tasks_dir / task_id

    def get_chunks_dir(self, task_id: str) -> Path:
        """返回指定任务的 chunks 目录路径（供 api_server 写临时分块文件）。"""
        chunks_dir = self._task_dir(task_id) / "chunks"
        chunks_dir.mkdir(parents=True, exist_ok=True)
        return chunks_dir
    
    def create_task(self, task_id: str, total_chunks: int) -> Dict:
        """创建新任务"""
        task_dir = self._task_dir(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "chunks").mkdir(exist_ok=True)
        (task_dir / "photos").mkdir(exist_ok=True)
        (task_dir / "output").mkdir(exist_ok=True)
        
        task = {
            "task_id": task_id,
            "status": "uploading",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "photo_count": 0,
            "received_chunks": [],
            "total_chunks": total_chunks,
            "total_uploaded_bytes": 0,
            "stage": None,
            "progress_text": None,
            "progress_percent": 0,
            "elapsed_sec": 0,
            "eta_sec": 0,
            "error": None,
            "result_file": None,
            "delete_count": 0,
            "keep_count": 0,
            "last_heartbeat": None
        }
        
        self._save_task(task)
        return task
    
    def get_task(self, task_id: str) -> Optional[Dict]:
        """获取任务信息"""
        # 非法 task_id 直接视为不存在（不抛异常，兼容轮询语义）
        if not _TASK_ID_RE.match(task_id or ''):
            return None
        task_file = self.tasks_dir / task_id / "task.json"
        if not task_file.exists():
            return None
        
        with open(task_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    def save_chunk(self, task_id: str, chunk_index: int, data: bytes):
        """
        保存上传的分块（整块字节接口，向后兼容）。

        防护：
          - 拒绝重复分块（已接收的 chunk_index 重传直接报错，防涓流 DoS）
          - 累计上传字节超 max_total_upload_bytes 则拒绝（防超大上传撑爆磁盘）
        """
        task = self.get_task(task_id)
        if not task:
            raise TaskError("task_not_found", "任务不存在")
        # chunk_index 边界校验：非负且不超过声明的 total_chunks
        if not isinstance(chunk_index, int) or chunk_index < 0 or chunk_index >= task['total_chunks']:
            raise TaskError("invalid_chunk_index", "分块索引非法")
        # 拒绝重复分块：已接收的重传直接报错，防攻击者靠重复发同一块保活任务
        if chunk_index in task['received_chunks']:
            raise TaskError("chunk_already_received", "该分块已接收，请勿重复上传")

        # 累计上传字节上限校验
        total_uploaded = task.get('total_uploaded_bytes', 0) + len(data)
        if total_uploaded > self.max_total_upload_bytes:
            raise TaskError("upload_limit_exceeded",
                            f"任务累计上传已超上限（{self.max_total_upload_bytes // (1024*1024)}MB）")

        chunk_file = self._task_dir(task_id) / "chunks" / f"chunk_{chunk_index}.zip"
        with open(chunk_file, 'wb') as f:
            f.write(data)

        task['received_chunks'].append(chunk_index)
        task['received_chunks'].sort()
        task['total_uploaded_bytes'] = total_uploaded
        task['updated_at'] = datetime.now().isoformat()
        self._save_task(task)

    def save_chunk_from_file(self, task_id: str, chunk_index: int, tmp_path: Path):
        """
        流式上传落盘接口：api_server 已把数据写到 tmp_path（临时文件），
        这里原子 rename 为正式 chunk 文件并更新任务状态。

        相比 save_chunk(data: bytes) 避免整块入内存，防内存 DoS。
        """
        task = self.get_task(task_id)
        if not task:
            raise TaskError("task_not_found", "任务不存在")
        if not isinstance(chunk_index, int) or chunk_index < 0 or chunk_index >= task['total_chunks']:
            raise TaskError("invalid_chunk_index", "分块索引非法")
        if chunk_index in task['received_chunks']:
            raise TaskError("chunk_already_received", "该分块已接收，请勿重复上传")

        chunk_file = self._task_dir(task_id) / "chunks" / f"chunk_{chunk_index}.zip"
        chunk_bytes = tmp_path.stat().st_size
        total_uploaded = task.get('total_uploaded_bytes', 0) + chunk_bytes
        if total_uploaded > self.max_total_upload_bytes:
            raise TaskError("upload_limit_exceeded",
                            f"任务累计上传已超上限（{self.max_total_upload_bytes // (1024*1024)}MB）")

        # 原子 rename（同盘）。若目标已存在（极端竞态）先删除。
        if chunk_file.exists():
            chunk_file.unlink()
        tmp_path.rename(chunk_file)

        task['received_chunks'].append(chunk_index)
        task['received_chunks'].sort()
        task['total_uploaded_bytes'] = total_uploaded
        task['updated_at'] = datetime.now().isoformat()
        self._save_task(task)

    
    def get_received_chunks(self, task_id: str) -> List[int]:
        """获取已接收的分块索引列表"""
        task = self.get_task(task_id)
        return task['received_chunks'] if task else []
    
    def _safe_extract_member(self, zf: zipfile.ZipFile, member: zipfile.ZipInfo,
                             photos_dir: Path, photos_root: str) -> int:
        """
        安全解压单个 zip 成员，防 Zip Slip：
          - 跳过目录项
          - 拒绝绝对路径 / 含 .. 的成员名
          - 校验最终解压路径必须落在 photos_dir 内
        仅解压到 photos_dir 顶层（丢弃 zip 内目录结构，缩略图是扁平文件）。

        返回实际写入字节数（供调用方累计，防 zip 炸弹用攻击者可控的 header size 绕过）。
        """
        name = member.filename
        if member.is_dir():
            return 0
        # 拒绝绝对路径与盘符
        if name.startswith('/') or name.startswith('\\') or (len(name) > 1 and name[1] == ':'):
            raise TaskError("malicious_archive", "上传包含非法路径，已拒绝")
        # 只取文件名部分，丢弃目录层级（防 ../ 穿越 + 扁平化）
        base_name = os.path.basename(name.replace('\\', '/'))
        if not base_name or base_name in ('.', '..'):
            raise TaskError("malicious_archive", "上传包含非法路径，已拒绝")
        target = (photos_dir / base_name).resolve()
        # 最终路径必须在 photos_dir 内
        if os.path.commonpath([str(target), photos_root]) != photos_root:
            raise TaskError("malicious_archive", "上传包含非法路径，已拒绝")
        # 字节计数 writer：实际写入超 member.file_size ×1.1 即中止删除
        # （zip header 的 file_size 由攻击者可控，可伪造小头大流绕过累计上限，
        #   故必须按实际写入字节判断而非声明值）
        member_max_bytes = int(member.file_size * 1.1) + 1024  # 10% 滑动 + 1KB 余量
        bytes_written = 0
        with zf.open(member, 'r') as src, open(target, 'wb') as dst:
            while True:
                buf = src.read(1024 * 1024)
                if not buf:
                    break
                bytes_written += len(buf)
                if bytes_written > member_max_bytes:
                    dst.close()
                    target.unlink(missing_ok=True)
                    raise TaskError("archive_too_large",
                                    "解压成员实际大小超出声明值，疑似 zip 炸弹")
                dst.write(buf)
        return bytes_written

    def unzip_all_chunks(self, task_id: str):
        """
        安全解压所有分块到 photos 目录。

        防护：
          - Zip Slip：逐成员校验路径落在 photos_dir 内
          - Zip 炸弹：累计成员数、未压缩总大小超上限则中止
          - 解压后照片数超 max_photos 则拒绝
        """
        task = self.get_task(task_id)
        if not task:
            raise TaskError("task_not_found", "任务不存在")
        
        task_dir = self._task_dir(task_id)
        chunks_dir = task_dir / "chunks"
        photos_dir = task_dir / "photos"
        photos_root = str(photos_dir.resolve())
        
        total_bytes = 0
        total_members = 0
        
        # 解压所有块
        for chunk_index in task['received_chunks']:
            chunk_file = chunks_dir / f"chunk_{chunk_index}.zip"
            if not chunk_file.exists():
                continue
            try:
                with zipfile.ZipFile(chunk_file, 'r') as zf:
                    for member in zf.infolist():
                        if member.is_dir():
                            continue
                        total_members += 1
                        if total_members > self.max_unzip_members:
                            raise TaskError("archive_too_large",
                                            "上传内容文件数过多，已拒绝")
                        # 用实际写入字节累计，而非攻击者可控的 member.file_size
                        actual_bytes = self._safe_extract_member(zf, member, photos_dir, photos_root)
                        total_bytes += actual_bytes
                        if total_bytes > self.max_unzip_bytes:
                            raise TaskError("archive_too_large",
                                            "上传内容解压后体积过大，已拒绝")
            except zipfile.BadZipFile:
                raise TaskError("corrupt_archive", "上传分块已损坏，请重新上传")
        
        # 统计照片数量
        photo_count = len(list(photos_dir.glob("*.jpg"))) + \
                      len(list(photos_dir.glob("*.jpeg"))) + \
                      len(list(photos_dir.glob("*.png")))
        
        # 照片数上限（解压后才知道真实张数）
        if photo_count > self.max_photos:
            raise TaskError("too_many_photos",
                            f"照片数量超过上限（{photo_count} > {self.max_photos}），请减少后重试")
        
        task['photo_count'] = photo_count
        task['status'] = 'ready'
        task['updated_at'] = datetime.now().isoformat()
        self._save_task(task)
    
    def mark_queued(self, task_id: str):
        """标记任务为排队中"""
        task = self.get_task(task_id)
        if task:
            task['status'] = 'queued'
            task['updated_at'] = datetime.now().isoformat()
            self._save_task(task)
    
    def mark_running(self, task_id: str):
        """标记任务为运行中"""
        task = self.get_task(task_id)
        if task:
            task['status'] = 'running'
            task['started_at'] = datetime.now().isoformat()
            task['updated_at'] = datetime.now().isoformat()
            task['last_heartbeat'] = datetime.now().isoformat()
            self._save_task(task)
    
    def update_heartbeat(self, task_id: str):
        """更新任务心跳时间戳（流水线运行期间定期调用，防止被误判为僵尸任务）"""
        task = self.get_task(task_id)
        if task and task['status'] == 'running':
            task['last_heartbeat'] = datetime.now().isoformat()
            task['updated_at'] = datetime.now().isoformat()
            self._save_task(task)
    
    def update_progress(self, task_id: str, progress_data: Dict):
        """更新任务进度（同时刷新心跳，进度更新本身即证明流水线存活）"""
        task = self.get_task(task_id)
        if task:
            task.update(progress_data)
            task['updated_at'] = datetime.now().isoformat()
            if task['status'] == 'running':
                task['last_heartbeat'] = datetime.now().isoformat()
            self._save_task(task)

    def update_stage_info(self, task_id: str, stage: str, stage_name: str):
        """轻量上报当前阶段（供 01a/01b 等无详细进度文件的阶段用）。

        只更新 stage / stage_name，清零 02 专属的进度/ETA 字段（避免上一阶段
        或上个任务的残留值串到 01a/01b 界面）。同时刷新心跳证明流水线存活。
        """
        task = self.get_task(task_id)
        if task:
            task['stage'] = stage
            task['stage_name'] = stage_name
            # 非 02 阶段没有批次进度与 ETA，清零让 APP 走"处理中"分支
            if stage != '02':
                task['progress_text'] = None
                task['progress_percent'] = 0
                task['eta_sec'] = 0
                task['finish_time'] = None
            task['updated_at'] = datetime.now().isoformat()
            if task['status'] == 'running':
                task['last_heartbeat'] = datetime.now().isoformat()
            self._save_task(task)
    
    def mark_completed(self, task_id: str, result_file: str, delete_count: int, keep_count: int):
        """标记任务完成"""
        task = self.get_task(task_id)
        if task:
            task['status'] = 'completed'
            task['result_file'] = result_file
            task['delete_count'] = delete_count
            task['keep_count'] = keep_count
            task['completed_at'] = datetime.now().isoformat()
            task['updated_at'] = datetime.now().isoformat()
            
            if 'started_at' in task:
                elapsed = (datetime.fromisoformat(task['completed_at']) - 
                          datetime.fromisoformat(task['started_at'])).total_seconds()
                task['elapsed_sec'] = int(elapsed)
            
            self._save_task(task)
    
    def mark_failed(self, task_id: str, error: str):
        """标记任务失败"""
        task = self.get_task(task_id)
        if task:
            task['status'] = 'failed'
            task['error'] = error
            task['updated_at'] = datetime.now().isoformat()
            self._save_task(task)
    
    def mark_cancelled(self, task_id: str):
        """标记任务为已取消（用户主动放弃）"""
        task = self.get_task(task_id)
        if task:
            task['status'] = 'cancelled'
            task['error'] = '用户已取消任务'
            task['updated_at'] = datetime.now().isoformat()
            self._save_task(task)
    
    def get_result_file(self, task_id: str) -> Optional[str]:
        """获取结果文件路径"""
        task = self.get_task(task_id)
        if task and task.get('result_file'):
            # result_file 是服务端生成的固定名（delete_list_<task_id>.txt），
            # 但仍二次校验，防 task.json 被篡改注入路径穿越。
            result_name = os.path.basename(str(task['result_file']))
            result_path = self._task_dir(task_id) / result_name
            if result_path.exists():
                return str(result_path)
        return None

    def _purge_task_payload(self, task_id: str):
        """
        删除任务的大块数据子目录（chunks/photos/output），回收磁盘空间。
        保留 task.json（几百字节），使 /api/status 仍能返回优雅的 failed 状态，
        避免客户端拿到 404 走"连续查询失败"的降级路径。
        小尾巴目录由 cleanup_old_tasks 按保留期最终删除。
        """
        try:
            task_dir = self._task_dir(task_id)
        except TaskError:
            return
        for sub in ("chunks", "photos", "output"):
            p = task_dir / sub
            if p.exists():
                try:
                    shutil.rmtree(p)
                except Exception as e:
                    logger.warning(f"清理任务数据失败 {task_id}/{sub}: {e}")

    def reconcile_orphaned_tasks(self, live_ids: set):
        """
        对账"孤儿"任务：把磁盘上处于在途状态（uploading/ready/queued/running）
        但不在内存存活集合 live_ids 中的任务标记为 failed，并清理其大块数据。

        典型场景：服务重启后上一实例遗留的 running/queued 任务，内存态已丢失，
        磁盘状态永久冻结，会占满在途配额。启动时传入空集合即可全部作废（白纸）。

        error 文案含"任务已失效"，触发客户端优雅路径（不提供"重新连接"，
        引导用户重新提交）。
        """
        reconciled = 0
        for task_dir in self.tasks_dir.iterdir():
            if not task_dir.is_dir():
                continue
            if not (task_dir / "task.json").exists():
                continue
            task = self.get_task(task_dir.name)
            if not task:
                continue
            if task['status'] in ('uploading', 'ready', 'queued', 'running') \
                    and task_dir.name not in live_ids:
                logger.warning(f"对账孤儿任务 {task_dir.name}（状态 {task['status']}，"
                               f"内存无存活记录），标记 failed 并清理数据")
                self.mark_failed(task_dir.name, "服务已重启，任务已失效，请重新提交")
                self._purge_task_payload(task_dir.name)
                reconciled += 1
        if reconciled:
            logger.info(f"启动对账：共作废 {reconciled} 个孤儿任务")
        return reconciled

    def reconcile_running_zombies(self, live_ids: set):
        """
        运行时僵尸回收（清理线程周期调用）：处理"磁盘 status==running 但不在内存
        存活集合"的任务——正常运行的 running 任务必在调度器 _active 中（加锁保证），
        故不在 live_ids 中的 running 一定是流水线子进程异常崩溃遗留的僵尸，
        标记 failed 并清理数据。依据内存态判定，零时间阈值、零误杀。
        """
        for task_dir in self.tasks_dir.iterdir():
            if not task_dir.is_dir():
                continue
            if not (task_dir / "task.json").exists():
                continue
            task = self.get_task(task_dir.name)
            if not task:
                continue
            if task['status'] == 'running' and task_dir.name not in live_ids:
                logger.warning(f"回收运行时僵尸任务 {task_dir.name}"
                               f"（running 但内存无存活记录），标记 failed 并清理数据")
                self.mark_failed(task_dir.name, "任务已失效：处理进程异常终止，请重新提交")
                self._purge_task_payload(task_dir.name)

    def count_inflight_tasks(self) -> int:
        """
        统计"在途"任务数（占用系统容量名额的任务）。
        在途 = uploading / ready / queued / running。
        供 API 层做准入控制（队列满则拒绝创建新任务）。
        """
        inflight = 0
        for task_dir in self.tasks_dir.iterdir():
            if not task_dir.is_dir():
                continue
            if not (task_dir / "task.json").exists():
                continue
            task = self.get_task(task_dir.name)
            if not task:
                continue
            if task['status'] in ('uploading', 'ready', 'queued', 'running'):
                inflight += 1
        return inflight
    
    def cleanup_old_tasks(self, retention_hours: int, upload_timeout_min: int = 30,
                           upload_max_age_hours: int = 2):
        """
        清理任务：
          1. 已完成/失败/取消且超过 retention_hours 的任务，删除目录回收磁盘。
          2. uploading/ready 状态但超过 upload_timeout_min 无活动的僵尸上传任务，
             标记 failed 释放系统容量名额（用户开始上传后放弃、不再 finalize）。
          3. uploading/ready 状态超过 upload_max_age_hours（绝对年龄死线）的任务，
             无论是否有活动一律作废。防攻击者靠每 25 分钟发 1 块涓流保活任务，
             永久占用 MAX_INFLIGHT_TASKS 名额的容量 DoS。
        """
        for task_dir in self.tasks_dir.iterdir():
            if not task_dir.is_dir():
                continue
            if not (task_dir / "task.json").exists():
                continue
            
            task = self.get_task(task_dir.name)
            if not task:
                continue
            
            try:
                updated_at = datetime.fromisoformat(task['updated_at'])
            except Exception:
                continue
            age_sec = (datetime.now() - updated_at).total_seconds()
            # 绝对年龄：从 created_at 起算（防涓流 DoS）
            try:
                created_at = datetime.fromisoformat(task['created_at'])
            except Exception:
                created_at = None
            absolute_age_sec = (datetime.now() - created_at).total_seconds() if created_at else None
            
            # 僵尸上传任务：超时未完成上传，标记 failed 释放名额
            if task['status'] in ('uploading', 'ready'):
                # 绝对年龄死线：无论是否有活动，超过上限一律作废
                if absolute_age_sec is not None and absolute_age_sec > upload_max_age_hours * 3600:
                    logger.warning(f"清理超龄上传任务 {task_dir.name}"
                                   f"（超过 {upload_max_age_hours} 小时绝对死线），释放名额")
                    self.mark_failed(task_dir.name, "上传超时（超过最大允许时长），已自动作废")
                elif age_sec > upload_timeout_min * 60:
                    logger.warning(f"清理僵尸上传任务 {task_dir.name}"
                                   f"（{upload_timeout_min} 分钟无活动），释放名额")
                    self.mark_failed(task_dir.name, "上传超时未完成，已自动作废")
                continue
            
            # 已结束任务：超过保留期删除目录
            if task['status'] not in ('completed', 'failed', 'cancelled'):
                continue
            if age_sec / 3600 > retention_hours:
                try:
                    shutil.rmtree(task_dir)
                except Exception as e:
                    logger.warning(f"清理任务目录失败 {task_dir.name}: {e}")
    
    def _save_task(self, task: Dict):
        """保存任务信息到磁盘"""
        task_file = self.tasks_dir / task['task_id'] / "task.json"
        with open(task_file, 'w', encoding='utf-8') as f:
            json.dump(task, f, ensure_ascii=False, indent=2)
