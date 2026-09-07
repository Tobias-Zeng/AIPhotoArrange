package com.example.photocleaner.remote

import android.net.Uri
import android.util.Log
import android.widget.Toast
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import com.example.photocleaner.models.PhotoInfo
import com.google.accompanist.permissions.ExperimentalPermissionsApi
import com.google.accompanist.permissions.PermissionState
import com.google.accompanist.permissions.isGranted
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 远程分析状态机
 */
enum class RemoteState {
    IDLE,               // 初始：选择日期范围
    GENERATING_THUMBS,  // 生成缩略图
    CHECKING_HOMELAB,   // 检查自建服务状态
    UPLOADING,          // 上传中（前台服务）
    WAITING,            // 等待服务端处理
    COMPLETED,          // 处理完成
    ERROR               // 出错
}

/**
 * 远程分析主界面
 *
 * @param manager 业务逻辑管理器
 * @param settings 设置
 * @param permissionState 相册读取权限状态（用于扫描前主动请求授权）
 * @param onResultReady 删除列表下载完成回调（Uri, scannedPhotoCount），交由主界面进入预览流程
 * @param onBack 返回
 */
@OptIn(ExperimentalPermissionsApi::class)
@Composable
fun RemoteAnalysisScreen(
    manager: RemoteAnalysisManager,
    settings: SettingsRepository,
    permissionState: PermissionState,
    onResultReady: (Uri, Int) -> Unit,
    onBack: () -> Unit,
    onRestorePreview: () -> Unit
) {
    var state by remember { mutableStateOf(RemoteState.IDLE) }
    var progress by remember { mutableStateOf(0 to 0) }
    var statusText by remember { mutableStateOf("") }
    var warningText by remember { mutableStateOf<String?>(null) }
    var skippedPhotos by remember {
        mutableStateOf<List<ThumbnailGenerator.SkippedPhoto>>(emptyList())
    }
    var showSkippedDialog by remember { mutableStateOf(false) }
    var taskId by remember { mutableStateOf<String?>(settings.pendingTaskId) }
    var taskStatus by remember { mutableStateOf<HomelabClient.TaskStatus?>(null) }
    var scannedPhotos by remember { mutableStateOf<List<PhotoInfo>>(emptyList()) }
    var consecutiveFailures by remember { mutableStateOf(0) }
    // 重连触发计数：点击"重新连接"时自增，用于重启轮询 LaunchedEffect
    var reconnectTrigger by remember { mutableStateOf(0) }
    // 任务是否已失效（服务端返回 failed，如僵尸任务），失效后不允许"重新连接"
    var taskInvalidated by remember { mutableStateOf(false) }
    // 处理确认对话框（扫描后，检查服务后弹出）
    var showProcessConfirmDialog by remember { mutableStateOf(false) }
    var uploadPhotoCount by remember { mutableStateOf(0) }
    var uploadEstimatedTime by remember { mutableStateOf("") }
    var uploadProviderName by remember { mutableStateOf("") }  // 新增：存储 provider 供确认框显示
    // 任务已完成确认对话框
    var showCompletedTaskDialog by remember { mutableStateOf(false) }

    val scope = rememberCoroutineScope()
    val context = LocalContext.current

    // 监听APP前后台切换：暂停/恢复轮询
    val lifecycleOwner = LocalLifecycleOwner.current
    var isAppInForeground by remember { mutableStateOf(true) }

    DisposableEffect(lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            when (event) {
                Lifecycle.Event.ON_RESUME -> {
                    isAppInForeground = true
                    // 前台恢复时立即触发一次查询（递增 reconnectTrigger 重启轮询）
                    if (state == RemoteState.WAITING && taskId != null) {
                        reconnectTrigger++
                    }
                }
                Lifecycle.Event.ON_PAUSE -> {
                    isAppInForeground = false
                }
                else -> {}
            }
        }

        lifecycleOwner.lifecycle.addObserver(observer)

        onDispose {
            lifecycleOwner.lifecycle.removeObserver(observer)
        }
    }

    // 启动时若有 pending task，直接进入等待状态
    LaunchedEffect(Unit) {
        if (taskId != null) {
            state = RemoteState.WAITING
        }
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(16.dp)
    ) {
        // 已保存结果提示卡片（顶部显示，不阻碍下方操作）
        val savedTaskId = settings.previewResultTaskId
        if (savedTaskId != null && state == RemoteState.IDLE) {
            com.example.photocleaner.SavedResultPromptCard(
                scannedCount = settings.previewScannedCount,
                deleteCount = settings.previewDeleteCount,
                onRestoreClick = onRestorePreview
            )
            Spacer(modifier = Modifier.height(16.dp))
        }
        
        when (state) {
            RemoteState.IDLE -> {
                DateRangeSelector { daysAgo ->
                    // 扫描相册前先确保已获得照片读取权限，
                    // 否则新设备首次使用会因未授权而扫描不到照片
                    if (!permissionState.status.isGranted) {
                        permissionState.launchPermissionRequest()
                        return@DateRangeSelector
                    }
                    val (startMs, endMs) = manager.getDateRange(daysAgo)
                    scope.launch {
                        // 1. 扫描相册
                        state = RemoteState.GENERATING_THUMBS
                        statusText = "扫描相册中..."
                        val photos = manager.scanPhotosByDateRange(startMs, endMs)
                        scannedPhotos = photos

                        if (photos.isEmpty()) {
                            statusText = "该时间段无照片"
                            state = RemoteState.ERROR
                            return@launch
                        }

                        // 1b. 本地照片数上限校验（与服务端 max_photos 对齐，避免传完才被拒、浪费流量）
                        if (photos.size > SettingsRepository.MAX_PHOTOS_PER_TASK) {
                            statusText = "本次选择 ${photos.size} 张，超过单次上限 " +
                                "${SettingsRepository.MAX_PHOTOS_PER_TASK} 张，请缩小时间范围分批处理"
                            state = RemoteState.ERROR
                            return@launch
                        }

                        // 1c. 服务地址/Token 空值校验（默认值已清空，首次使用需在设置中填写）
                        if (settings.homelabAddress.isBlank() || settings.authToken.isBlank()) {
                            statusText = "尚未配置服务地址或 Token\n\n请先在设置中填写照片服务地址和认证 Token"
                            state = RemoteState.ERROR
                            return@launch
                        }

                        // 2. 扫描后立即检查服务 + token（提前暴露问题，避免生成缩略图后才发现服务不可用）
                        state = RemoteState.CHECKING_HOMELAB
                        statusText = "检查照片服务状态..."
                        
                        // 用详细检查（需认证）一次完成：模型探活 + token 验证 + 拿 provider。
                        // /api/health 出于公网安全已剥离 provider/model，必须走 detailed 才能拿到，
                        // 否则 provider=unknown 会导致预估耗时按在线 API 系数算、严重偏差。
                        val health = manager.checkHealthDetailed()
                        if (!health.ok) {
                            val err = health.error ?: ""
                            statusText = when {
                                err.contains("Token") ->
                                    "认证失败：Token 无效\n\n请检查设置中的 Token 配置"
                                err.contains("无法连接") || err.contains("连接") ->
                                    "照片服务无法访问，请确认服务已开机、地址正确"
                                else -> err.ifEmpty { "照片服务未就绪，请确认已开机" }
                            }
                            state = RemoteState.ERROR
                            return@launch
                        }

                        warningText = health.warning
                        
                        // 3. 获取 provider，计算精确预估时间，弹确认框
                        val provider = health.provider ?: "unknown"
                        uploadPhotoCount = photos.size
                        uploadEstimatedTime = manager.estimateProcessingTime(photos.size, provider)
                        uploadProviderName = provider
                        
                        state = RemoteState.IDLE  // 回到 IDLE 等待用户确认
                        showProcessConfirmDialog = true
                    }
                }
            }

            RemoteState.GENERATING_THUMBS -> {
                GeneratingView(progress, statusText, skippedPhotos.size) {
                    showSkippedDialog = true
                }
            }

            RemoteState.CHECKING_HOMELAB -> {
                CenteredProgress("检查照片服务状态...")
            }

            RemoteState.UPLOADING -> {
                var uploadCurrent by remember { mutableStateOf(0) }
                var uploadTotal by remember { mutableStateOf(0) }

                UploadingView(
                    warningText = warningText,
                    totalPhotos = uploadPhotoCount,
                    uploadProgress = uploadCurrent to uploadTotal,
                    onCheckPending = {
                        // 上传由前台服务处理，点击"上传已完成？"时进入等待
                        taskId = settings.pendingTaskId
                        if (taskId != null) {
                            state = RemoteState.WAITING
                        }
                    },
                    onStopUpload = {
                        scope.launch {
                            // 停止上传服务
                            manager.stopUploadService()

                            // 如果已创建 task，通知服务端取消
                            taskId?.let { tid ->
                                try {
                                    manager.cancelTask(tid)
                                } catch (e: Exception) {
                                    Log.w("RemoteAnalysisScreen", "取消服务端任务失败: ${e.message}")
                                }
                            }

                            // 清理本地
                            settings.clearPendingTask()
                            taskId = null
                            manager.cleanupThumbnails()
                            state = RemoteState.IDLE
                            statusText = ""
                        }
                    }
                )
                // 轮询上传进度 + pendingTaskId（上传服务完成后会写入）
                LaunchedEffect(Unit) {
                    val progressPrefs = context.getSharedPreferences(
                        "upload_progress", android.content.Context.MODE_PRIVATE
                    )
                    while (state == RemoteState.UPLOADING) {
                        // 兜底保护：检查上传服务是否写入错误（如认证失败）
                        val uploadError = progressPrefs.getString("upload_error", null)
                        if (uploadError != null) {
                            // 清理错误标记，避免下次误触发
                            progressPrefs.edit().remove("upload_error").apply()
                            val hint = if (uploadError.contains("401") ||
                                uploadError.contains("Unauthorized", ignoreCase = true)
                            ) {
                                "认证失败，请检查设置中的 Token 配置"
                            } else {
                                "请检查网络连接和服务状态"
                            }
                            statusText = "上传失败：$uploadError\n\n$hint"
                            state = RemoteState.ERROR
                            break
                        }

                        // 读取上传进度（UploadService 写入）
                        uploadCurrent = progressPrefs.getInt("current", 0)
                        uploadTotal = progressPrefs.getInt("total", 0)

                        val pending = settings.pendingTaskId
                        if (pending != null) {
                            taskId = pending
                            state = RemoteState.WAITING
                        }
                        delay(1000)
                    }
                }
            }

            RemoteState.WAITING -> {
                // 健壮性检查：无 taskId 时不应进入 WAITING（防止流程死循环）
                if (taskId == null) {
                    LaunchedEffect(Unit) {
                        statusText = "任务ID丢失，请返回重新提交"
                        state = RemoteState.ERROR
                    }
                    CenteredProgress("检查任务状态...")
                } else {
                    WaitingView(
                    taskId = taskId,
                    taskStatus = taskStatus,
                    onRefresh = {
                        scope.launch {
                            taskId?.let { tid ->
                                try {
                                    val status = manager.getTaskStatus(tid)
                                    taskStatus = status
                                    // 手动刷新成功，清零失败计数，恢复正常轮询
                                    consecutiveFailures = 0
                                    if (status.status == "completed") {
                                        // 下载结果
                                        val uri = manager.downloadResult(tid)
                                        // 优先用本次内存中的扫描数，回退到持久化值（进程重建场景）
                                        val analyzed = if (scannedPhotos.isNotEmpty())
                                            scannedPhotos.size else settings.pendingPhotoCount
                                        settings.clearPendingTask()
                                        // 清理缩略图释放空间
                                        manager.cleanupThumbnails()
                                        onResultReady(uri, analyzed)
                                    } else if (status.status == "failed") {
                                        val error = status.error ?: "处理失败"
                                        statusText = error
                                        // 识别服务端返回的任务失效标识（僵尸任务）
                                        if (error.contains("任务已失效") || error.contains("服务端可能已重启")) {
                                            taskInvalidated = true
                                        }
                                        state = RemoteState.ERROR
                                    }
                                } catch (e: Exception) {
                                    statusText = "查询失败: ${e.message}"
                                    state = RemoteState.ERROR
                                }
                            }
                        }
                    },
                    onStopWaiting = {
                        // 停止轮询，转入ERROR状态，允许用户选择"重新连接"或"放弃任务"
                        statusText = "已停止等待\n\n可选择重新连接尝试恢复，或放弃任务返回"
                        state = RemoteState.ERROR
                    }
                )
                // 自动轮询（连续失败5次约150秒后转ERROR，避免服务端故障时无限等待）
                // reconnectTrigger 变化时重启轮询（用于ERROR界面"重新连接"）
                LaunchedEffect(taskId, reconnectTrigger) {
                    consecutiveFailures = 0  // 重置计数器
                    while (state == RemoteState.WAITING && taskId != null) {
                        // 后台时暂停轮询：省电、避免被系统杀。
                        // 每秒检查一次前后台状态；切回前台由 ON_RESUME 递增
                        // reconnectTrigger 重启本 LaunchedEffect，立即执行一次查询。
                        if (!isAppInForeground) {
                            delay(1000)
                            continue
                        }
                        try {
                            val status = manager.getTaskStatus(taskId!!)
                            taskStatus = status
                            consecutiveFailures = 0  // 成功查询后清零
                            if (status.status == "completed") {
                                val uri = manager.downloadResult(taskId!!)
                                // 优先用本次内存中的扫描数，回退到持久化值（进程重建场景）
                                val analyzed = if (scannedPhotos.isNotEmpty())
                                    scannedPhotos.size else settings.pendingPhotoCount
                                settings.clearPendingTask()
                                // 清理缩略图释放空间
                                manager.cleanupThumbnails()
                                onResultReady(uri, analyzed)
                                 break
                            } else if (status.status == "failed") {
                                val error = status.error ?: "处理失败"
                                statusText = error
                                // 识别服务端返回的任务失效标识（僵尸任务）
                                if (error.contains("任务已失效") || error.contains("服务端可能已重启")) {
                                    taskInvalidated = true
                                }
                                state = RemoteState.ERROR
                                break
                            }
                        } catch (e: Exception) {
                            consecutiveFailures++
                            Log.w("RemoteAnalysisScreen", 
                                "轮询失败 ($consecutiveFailures/5): ${e.message}")
                            
                            // 连续失败5次后转入ERROR状态
                            if (consecutiveFailures >= 5) {
                                statusText = "服务端无响应，请检查网络或服务状态\n\n" +
                                    "错误: ${e.message}\n\n" +
                                    "提示：可尝试重新连接，或放弃任务重新提交"
                                state = RemoteState.ERROR
                                break
                            }
                        }
                        // 智能轮询间隔：与服务端更新频率匹配，避免无谓请求。
                        // stage01 预处理阶段快（几秒到十几秒），用 5s 快速捕获切换；
                        // stage02 的 ETA 每 60s 更新一次，故 02 阶段用 30s；
                        // 排队阶段变化较快，用 10s 提升响应感。
                        val delayMs = when {
                            taskStatus?.status == "queued" -> 10_000L
                            taskStatus?.stage == "01" -> 5_000L
                            taskStatus?.stage == "02" -> 30_000L
                            else -> 30_000L
                        }
                        delay(delayMs)
                    }
                }
                }  // end else (taskId != null)
            }

            RemoteState.COMPLETED -> {
                CenteredProgress("处理完成")
            }

            RemoteState.ERROR -> {
                ErrorView(
                    message = statusText,
                    hasPendingTask = taskId != null,
                    taskInvalidated = taskInvalidated,
                    onReconnect = if (taskId != null) {{
                        // 清零失败计数，增加 reconnectTrigger 重启轮询，回到等待页面
                        consecutiveFailures = 0
                        reconnectTrigger++
                        state = RemoteState.WAITING
                        statusText = ""
                    }} else null,
                    onAbandonTask = if (taskId != null) {{
                        scope.launch {
                            taskId?.let { tid ->
                                try {
                                    // 先查询最新状态（可能已处理完成，给用户查看结果的机会）
                                    val status = withContext(Dispatchers.IO) {
                                        manager.getTaskStatus(tid)
                                    }
                                    if (status.status == "completed") {
                                        // 任务已完成，弹窗确认是否查看结果
                                        showCompletedTaskDialog = true
                                        return@launch
                                    }
                                } catch (e: Exception) {
                                    // 查询失败（可能网络断开），继续放弃流程
                                    Log.w("RemoteAnalysisScreen", "查询任务状态失败: ${e.message}")
                                }

                                // 通知服务端取消
                                try {
                                    withContext(Dispatchers.IO) {
                                        manager.cancelTask(tid)
                                    }
                                } catch (e: Exception) {
                                    // 取消失败，Toast 提示用户
                                    withContext(Dispatchers.Main) {
                                        Toast.makeText(
                                            context,
                                            "服务端取消失败，已清理本地数据",
                                            Toast.LENGTH_SHORT
                                        ).show()
                                    }
                                }
                            }

                            // 清理本地（无论服务端取消是否成功）
                            settings.clearPendingTask()
                            taskId = null
                            taskInvalidated = false
                            manager.cleanupThumbnails()
                            state = RemoteState.IDLE
                            statusText = ""
                            warningText = null
                        }
                    }} else null,
                    onBackToIdle = if (taskId == null) {{
                        // 无 pending task（扫描无照片/服务不可用等场景）：只显示"返回"
                        statusText = ""
                        warningText = null
                        taskInvalidated = false
                        state = RemoteState.IDLE
                    }} else null
                )
            }
        }
    }

    // 废片详情弹窗
    if (showSkippedDialog) {
        SkippedPhotosDialog(skippedPhotos) { showSkippedDialog = false }
    }

    // 处理确认弹窗（扫描后、检查服务后弹出）
    if (showProcessConfirmDialog) {
        AlertDialog(
            onDismissRequest = { /* 强制选择 */ },
            title = { Text("准备处理") },
            text = {
                Text(
                    "本次有 $uploadPhotoCount 张照片需要处理\n" +
                    "预计耗时：$uploadEstimatedTime\n" +
                    "模型服务：$uploadProviderName\n\n" +
                    "包括生成缩略图、上传、服务端分析三个阶段"
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    showProcessConfirmDialog = false
                    // 确认后直接进缩略图生成（health 已在扫描后检查过，无需重复）
                    scope.launch {
                        state = RemoteState.GENERATING_THUMBS
                        val result = manager.generateThumbnails(scannedPhotos) { cur, total ->
                            progress = cur to total
                        }
                        skippedPhotos = result.skipped

                        if (result.thumbnails.isEmpty()) {
                            statusText = "所有照片均被预筛跳过"
                            state = RemoteState.ERROR
                            return@launch
                        }

                        // 持久化照片数
                        settings.pendingPhotoCount = scannedPhotos.size

                        // 缩略图生成完成后，直接开始上传（用户已在处理前确认）
                        manager.startUploadService()
                        state = RemoteState.UPLOADING
                    }
                }) {
                    Text("开始处理")
                }
            },
            dismissButton = {
                TextButton(onClick = {
                    showProcessConfirmDialog = false
                    state = RemoteState.IDLE
                    statusText = ""
                }) {
                    Text("取消")
                }
            }
        )
    }

    // 任务已完成确认弹窗（用户在 ERROR 界面放弃任务时，若发现任务已处理完）
    if (showCompletedTaskDialog) {
        AlertDialog(
            onDismissRequest = { showCompletedTaskDialog = false },
            title = { Text("任务已完成") },
            text = { Text("该任务已处理完成，是否查看结果？") },
            confirmButton = {
                TextButton(onClick = {
                    showCompletedTaskDialog = false
                    scope.launch {
                        try {
                            val tid = taskId!!
                            val uri = withContext(Dispatchers.IO) {
                                manager.downloadResult(tid)
                            }
                            val analyzed = if (scannedPhotos.isNotEmpty())
                                scannedPhotos.size else settings.pendingPhotoCount
                            settings.clearPendingTask()
                            manager.cleanupThumbnails()
                            onResultReady(uri, analyzed)
                        } catch (e: Exception) {
                            statusText = "下载结果失败: ${e.message}"
                            state = RemoteState.ERROR
                        }
                    }
                }) {
                    Text("查看结果")
                }
            },
            dismissButton = {
                TextButton(onClick = {
                    showCompletedTaskDialog = false
                    // 仍然放弃：只清理手机本地（服务端结果 24h 后自动清理）
                    scope.launch {
                        settings.clearPendingTask()
                        taskId = null
                        taskInvalidated = false
                        manager.cleanupThumbnails()
                        state = RemoteState.IDLE
                        statusText = ""
                        warningText = null
                    }
                }) {
                    Text("仍然放弃")
                }
            }
        )
    }

    // 返回按钮（顶部）
    if (state == RemoteState.IDLE) {
        // 由外层 Scaffold 处理返回
    }
}

// ==========================================
// 子组件
// ==========================================

@Composable
private fun DateRangeSelector(onSelect: (Int) -> Unit) {
    Column(
        modifier = Modifier.fillMaxSize(),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center
    ) {
        Text(
            "选择要分析的照片时间范围",
            style = MaterialTheme.typography.headlineSmall
        )
        Spacer(Modifier.height(8.dp))
        Text(
            "手机生成缩略图并上传照片服务分析，完成后返回删除列表",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center
        )
        Spacer(Modifier.height(32.dp))

        // 快捷按钮：1 天(今天)、2 天、3 天。值为 daysAgo（回溯天数 = 天数-1）
        val quickOptions = listOf(
            "今天" to 0,
            "最近 2 天" to 1,
            "最近 3 天" to 2
        )
        quickOptions.forEach { (label, days) ->
            Button(
                onClick = { onSelect(days) },
                modifier = Modifier
                    .fillMaxWidth(0.7f)
                    .padding(vertical = 6.dp)
            ) {
                Text(label)
            }
        }

        Spacer(Modifier.height(24.dp))
        HorizontalDivider(modifier = Modifier.fillMaxWidth(0.7f))
        Spacer(Modifier.height(16.dp))

        // 自定义范围：4 - 30 天，使用滑块选择
        var customDays by remember { mutableStateOf(4) }
        Text(
            "自定义范围",
            style = MaterialTheme.typography.titleMedium
        )
        Spacer(Modifier.height(4.dp))
        Text(
            "最近 $customDays 天",
            style = MaterialTheme.typography.headlineSmall,
            color = MaterialTheme.colorScheme.primary
        )
        Spacer(Modifier.height(8.dp))
        Slider(
            value = customDays.toFloat(),
            onValueChange = { customDays = it.toInt() },
            valueRange = 4f..30f,
            steps = 30 - 4 - 1,  // 4..30 之间的整数刻度
            modifier = Modifier.fillMaxWidth(0.7f)
        )
        Row(
            modifier = Modifier.fillMaxWidth(0.7f),
            horizontalArrangement = Arrangement.SpaceBetween
        ) {
            Text("4 天", style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            Text("30 天", style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Spacer(Modifier.height(12.dp))
        Button(
            onClick = { onSelect(customDays - 1) },  // daysAgo = 天数 - 1
            modifier = Modifier.fillMaxWidth(0.7f)
        ) {
            Text("分析最近 $customDays 天")
        }
    }
}

@Composable
private fun GeneratingView(
    progress: Pair<Int, Int>,
    statusText: String,
    skippedCount: Int,
    onShowSkipped: () -> Unit
) {
    Column(
        modifier = Modifier.fillMaxSize(),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center
    ) {
        val (cur, total) = progress
        if (total > 0) {
            LinearProgressIndicator(
                progress = { cur.toFloat() / total },
                modifier = Modifier.fillMaxWidth(0.8f)
            )
            Spacer(Modifier.height(12.dp))
            Text("生成缩略图 $cur / $total")
        } else {
            CircularProgressIndicator()
            Spacer(Modifier.height(12.dp))
            Text(statusText)
        }

        if (skippedCount > 0) {
            Spacer(Modifier.height(16.dp))
            Text(
                "已跳过 $skippedCount 张疑似废片（极小图/截图等）",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.secondary
            )
            TextButton(onClick = onShowSkipped) {
                Text("查看详情")
            }
        }
    }
}

@Composable
private fun CenteredProgress(text: String) {
    Column(
        modifier = Modifier.fillMaxSize(),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center
    ) {
        CircularProgressIndicator()
        Spacer(Modifier.height(12.dp))
        Text(text)
    }
}

@Composable
private fun UploadingView(
    warningText: String?,
    totalPhotos: Int,
    uploadProgress: Pair<Int, Int>,  // (当前分块, 总分块数)
    onCheckPending: () -> Unit,
    onStopUpload: () -> Unit
) {
    Column(
        modifier = Modifier.fillMaxSize(),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center
    ) {
        val (current, total) = uploadProgress

        if (total > 0) {
            // 显示进度条和文字
            LinearProgressIndicator(
                progress = { current.toFloat() / total },
                modifier = Modifier.fillMaxWidth(0.8f)
            )
            Spacer(Modifier.height(8.dp))
            Text("上传分块: $current/$total")
        } else {
            // 尚未开始（初始化中）
            CircularProgressIndicator()
            Spacer(Modifier.height(12.dp))
            Text("准备上传...")
        }

        if (totalPhotos > 0) {
            Spacer(Modifier.height(8.dp))
            Text(
                "共 $totalPhotos 张照片",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.primary
            )
        }
        Text(
            "上传由后台服务处理，可切到其他应用",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
        warningText?.let {
            Spacer(Modifier.height(8.dp))
            Text(
                it,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.tertiary
            )
        }
        Spacer(Modifier.height(24.dp))
        OutlinedButton(onClick = onCheckPending) {
            Text("上传已完成？点此继续")
        }
        Spacer(Modifier.height(12.dp))
        OutlinedButton(
            onClick = onStopUpload,
            colors = ButtonDefaults.outlinedButtonColors(
                contentColor = MaterialTheme.colorScheme.error
            )
        ) {
            Text("停止上传")
        }
    }
}

@Composable
private fun WaitingView(
    taskId: String?,
    taskStatus: HomelabClient.TaskStatus?,
    onRefresh: () -> Unit,
    onStopWaiting: (() -> Unit)? = null
) {
    // ---- 本地秒级计时器：平滑递增已用时、递减 ETA ----
    // 服务端 30s 更新一次真实值，本地每秒 +1/-1 做动画过渡，消除跳变感
    var localElapsedSec by remember { mutableIntStateOf(0) }
    var localEtaSec by remember { mutableIntStateOf(0) }
    
    // 同步服务端真实值到本地（taskStatus 更新时触发）
    LaunchedEffect(taskStatus?.elapsedSec, taskStatus?.etaSec) {
        taskStatus?.elapsedSec?.let { localElapsedSec = it }
        taskStatus?.etaSec?.let { localEtaSec = it }
    }
    
    // 本地秒级递增/递减（仅在 running 阶段生效）
    // 后台时暂停计时，切回前台由生命周期监听器触发手动刷新，立即同步服务端真实值
    val lifecycleOwner = LocalLifecycleOwner.current
    var isAppInForeground by remember { mutableStateOf(true) }
    
    DisposableEffect(lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            isAppInForeground = event == Lifecycle.Event.ON_RESUME
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose {
            lifecycleOwner.lifecycle.removeObserver(observer)
        }
    }
    
    LaunchedEffect(taskStatus?.status, isAppInForeground) {
        if (taskStatus?.status == "running" && isAppInForeground) {
            while (true) {
                delay(1000)
                localElapsedSec++
                if (localEtaSec > 0) {
                    localEtaSec--
                }
            }
        }
    }
    
    Column(
        modifier = Modifier.fillMaxSize(),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center
    ) {
        Text("服务端处理中", style = MaterialTheme.typography.headlineSmall)
        Spacer(Modifier.height(4.dp))
        Text(
            "Task: ${taskId ?: "-"}",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
        Spacer(Modifier.height(24.dp))

        if (taskStatus != null) {
            if (taskStatus.status == "queued") {
                // 排队中：显示前面还有几个任务
                CircularProgressIndicator()
                Spacer(Modifier.height(16.dp))
                val ahead = (taskStatus.queuePosition ?: 1) - 1
                Text(
                    if (ahead > 0) "排队中，前面还有 $ahead 个任务" else "排队中，即将开始处理",
                    style = MaterialTheme.typography.bodyLarge
                )
            } else {
                // 阶段名称（仅 02 阶段显示，01 预处理阶段不显示内部细节）
                if (taskStatus.stage == "02") {
                    taskStatus.stageName?.let {
                        Text("阶段: $it", style = MaterialTheme.typography.bodyLarge)
                    }
                }

                if (taskStatus.stage == "02") {
                    // ---- Stage 02（最耗时阶段）：进度条 + 详细 ETA ----
                    // 转圈动画：与其它界面风格统一，表明系统仍在工作
                    // （避免进度条 30s 静止时用户误以为卡死）
                    Spacer(Modifier.height(16.dp))
                    CircularProgressIndicator()

                    if (taskStatus.progressPercent > 0) {
                        Spacer(Modifier.height(16.dp))
                        LinearProgressIndicator(
                            progress = { taskStatus.progressPercent / 100f },
                            modifier = Modifier.fillMaxWidth(0.8f)
                        )
                        Spacer(Modifier.height(8.dp))
                        taskStatus.progress?.let { Text(it) }
                    }

                    Spacer(Modifier.height(16.dp))
                    if (localEtaSec > 0 && taskStatus.finishTime != null) {
                        // ETA 信息卡片：左"预计剩余"用本地倒计时、右"预计完成"用服务端值
                        EtaCard(
                            etaText = formatDuration(localEtaSec),
                            finishTime = taskStatus.finishTime
                        )
                    } else {
                        // 初期样本不足，ETA 尚未算出
                        Text(
                            "正在计算预计时间...",
                            style = MaterialTheme.typography.bodyMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                } else if (taskStatus.stage == "01") {
                    // ---- Stage 01 预处理阶段：合并显示（01a 图片去重 + 01b GPS 解析）----
                    Spacer(Modifier.height(16.dp))
                    CircularProgressIndicator()
                    Spacer(Modifier.height(12.dp))
                    Text(
                        "正在预处理照片...",
                        style = MaterialTheme.typography.bodyLarge
                    )
                    Spacer(Modifier.height(4.dp))
                    Text(
                        "(图片去重与位置解析)",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                } else {
                    // ---- 其他阶段或空阶段（启动初期）----
                    Spacer(Modifier.height(16.dp))
                    CircularProgressIndicator()
                    Spacer(Modifier.height(12.dp))
                    Text(
                        "正在处理中，请稍候...",
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }

                // 已用时间（所有阶段都显示，使用本地秒级平滑计时值）
                if (localElapsedSec > 0) {
                    Spacer(Modifier.height(12.dp))
                    Text(
                        "已用时：${formatDuration(localElapsedSec)}",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
            }
        } else {
            CircularProgressIndicator()
        }

        Spacer(Modifier.height(24.dp))
        Button(onClick = onRefresh) {
            Text("刷新状态")
        }
        
        // 增加"停止等待"按钮，允许用户主动中断轮询转入ERROR界面，
        // 在ERROR界面选择"重新连接"或"放弃任务"
        if (onStopWaiting != null) {
            Spacer(Modifier.height(12.dp))
            OutlinedButton(onClick = onStopWaiting) {
                Text("停止等待")
            }
        }
    }
}

@Composable
private fun ErrorView(
    message: String,
    hasPendingTask: Boolean = false,
    taskInvalidated: Boolean = false,
    onReconnect: (() -> Unit)? = null,
    onAbandonTask: (() -> Unit)? = null,
    onBackToIdle: (() -> Unit)? = null
) {
    Column(
        modifier = Modifier.fillMaxSize(),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center
    ) {
        Text("⚠", style = MaterialTheme.typography.displayMedium)
        Spacer(Modifier.height(12.dp))
        Text(
            message,
            style = MaterialTheme.typography.bodyLarge,
            color = MaterialTheme.colorScheme.error,
            textAlign = TextAlign.Center
        )
        Spacer(Modifier.height(24.dp))
        
        if (hasPendingTask) {
            // 有任务场景：显示"重新连接"（非僵尸）+ "放弃任务"
            // 任务已失效（僵尸任务）时不提供"重新连接"，只能放弃后重新提交；
            // 否则（网络波动/服务端临时不可用）提供"重新连接"尝试恢复
            if (!taskInvalidated && onReconnect != null) {
                Button(onClick = onReconnect) {
                    Text("重新连接")
                }
                Spacer(Modifier.height(12.dp))
            }
            // 存在未完成任务时，提供"放弃任务"入口（清除任务+清理缩略图，回到选择界面）
            if (onAbandonTask != null) {
                OutlinedButton(onClick = onAbandonTask) {
                    Text("放弃任务")
                }
            }
        } else {
            // 无任务场景（扫描无照片/服务不可用等）：只显示"返回"
            if (onBackToIdle != null) {
                Button(onClick = onBackToIdle) {
                    Text("返回")
                }
            }
        }
    }
}

@Composable
private fun SkippedPhotosDialog(
    photos: List<ThumbnailGenerator.SkippedPhoto>,
    onDismiss: () -> Unit
) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("已跳过的照片 (${photos.size})") },
        text = {
            LazyColumn(modifier = Modifier.heightIn(max = 400.dp)) {
                items(photos) { photo ->
                    Column(modifier = Modifier.padding(vertical = 4.dp)) {
                        Text(photo.fileName, style = MaterialTheme.typography.bodyMedium)
                        Text(
                            "原因: ${photo.reason}",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                }
            }
        },
        confirmButton = {
            TextButton(onClick = onDismiss) { Text("关闭") }
        }
    )
}

/**
 * ETA 信息卡片：左侧"预计剩余时间"，右侧"预计完成时刻"。
 * finishTime 为服务器本地时间字符串（"MM-DD HH:MM:SS"），直接展示。
 */
@Composable
private fun EtaCard(etaText: String, finishTime: String) {
    Card(
        modifier = Modifier.fillMaxWidth(0.85f),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.primaryContainer
        )
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(16.dp),
            horizontalArrangement = Arrangement.SpaceBetween
        ) {
            EtaColumn(
                label = "预计剩余",
                value = etaText,
                alignment = Alignment.Start
            )
            EtaColumn(
                label = "预计完成",
                value = finishTime,
                alignment = Alignment.End
            )
        }
    }
}

@Composable
private fun EtaColumn(
    label: String,
    value: String,
    alignment: Alignment.Horizontal
) {
    Column(horizontalAlignment = alignment) {
        Text(
            label,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onPrimaryContainer
        )
        Spacer(Modifier.height(4.dp))
        Text(
            value,
            style = MaterialTheme.typography.titleMedium,
            fontWeight = FontWeight.Bold,
            color = MaterialTheme.colorScheme.primary
        )
    }
}

private fun formatDuration(sec: Int): String {
    val h = sec / 3600
    val m = (sec % 3600) / 60
    val s = sec % 60
    return when {
        h > 0 -> "${h}小时${m}分钟"
        m > 0 -> "${m}分${s}秒"
        else -> "${s}秒"
    }
}
