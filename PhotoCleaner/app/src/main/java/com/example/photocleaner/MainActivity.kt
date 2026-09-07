package com.example.photocleaner

import android.net.Uri
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import coil.compose.AsyncImage
import com.example.photocleaner.models.PhotoInfo
import com.example.photocleaner.remote.PreviewResultStore
import com.example.photocleaner.remote.RemoteAnalysisManager
import com.example.photocleaner.remote.RemoteAnalysisScreen
import com.example.photocleaner.remote.SettingsRepository
import com.example.photocleaner.ui.theme.PhotoCleanerTheme
import com.google.accompanist.permissions.ExperimentalPermissionsApi
import com.google.accompanist.permissions.isGranted
import com.google.accompanist.permissions.rememberPermissionState
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * MainActivity - 照片清理应用主界面
 * 
 * 两种模式：
 * 1. 本地模式（默认）：导入删除列表 -> 预览 -> 删除
 * 2. 远程分析模式：扫描相册 -> 生成缩略图 -> 上传自建服务 -> 下载删除列表 -> 预览 -> 删除
 */
class MainActivity : ComponentActivity() {
    
    private lateinit var photoMatcher: PhotoMatcher
    private lateinit var photoDeleter: PhotoDeleter
    private lateinit var settings: SettingsRepository
    
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        
        photoMatcher = PhotoMatcher(this)
        photoDeleter = PhotoDeleter(this)
        settings = SettingsRepository(this)
        
        setContent {
            PhotoCleanerTheme {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = MaterialTheme.colorScheme.background
                ) {
                    MainScreen(
                        photoMatcher = photoMatcher,
                        photoDeleter = photoDeleter,
                        settings = settings
                    )
                }
            }
        }
    }
}

/**
 * 流程步骤枚举
 */
enum class Step {
    IMPORT,    // 导入删除列表（本地模式）
    REMOTE,    // 远程分析（远程模式）
    PREVIEW,   // 预览待删除照片
    COMPLETED  // 删除完成
}

/**
 * 主界面组合式
 */
@OptIn(ExperimentalPermissionsApi::class, ExperimentalMaterial3Api::class)
@Composable
fun MainScreen(
    photoMatcher: PhotoMatcher,
    photoDeleter: PhotoDeleter,
    settings: SettingsRepository
) {
    var matchedPhotos by remember { mutableStateOf<List<PhotoInfo>>(emptyList()) }
    var scannedCount by remember { mutableStateOf(0) }
    var currentStep by remember { mutableStateOf(Step.IMPORT) }
    var isLoading by remember { mutableStateOf(false) }
    var errorMessage by remember { mutableStateOf<String?>(null) }
    var showSettings by remember { mutableStateOf(false) }
    var restoreChecked by remember { mutableStateOf(false) }
    // 预览结果是否已失效（照片被用户手动全部删除），失效后返回需清理持久化
    var previewInvalidated by remember { mutableStateOf(false) }
    // 部分照片已被手动删除时的确认信息：Pair(仍存活的照片, 已被删除的数量)
    var partialDeleteInfo by remember { mutableStateOf<Pair<List<PhotoInfo>, Int>?>(null) }
    
    // 一次性：启动时优先恢复已保存的预览结果
    LaunchedEffect(Unit) {
        val savedTaskId = settings.previewResultTaskId
        if (savedTaskId != null) {
            val restored = PreviewResultStore.loadPreview(photoMatcher.context, savedTaskId)
            if (restored != null) {
                matchedPhotos = restored.second
                scannedCount = restored.first
                currentStep = Step.PREVIEW
                restoreChecked = true
                return@LaunchedEffect
            } else {
                // 恢复失败，清理过期标记
                settings.clearPreviewResult()
            }
        }
        // 无已保存结果，按设置决定初始页面
        currentStep = if (settings.useRemoteAnalysis) Step.REMOTE else Step.IMPORT
        restoreChecked = true
    }
    
    // 响应远程分析开关：仅在初始页（IMPORT/REMOTE）时切换，避免打断 PREVIEW/COMPLETED
    LaunchedEffect(settings.useRemoteAnalysis) {
        if (restoreChecked && (currentStep == Step.IMPORT || currentStep == Step.REMOTE)) {
            currentStep = if (settings.useRemoteAnalysis) Step.REMOTE else Step.IMPORT
        }
    }
    
    // 协程作用域
    val scope = rememberCoroutineScope()
    
    // 权限申请
    val permissionState = rememberPermissionState(
        android.Manifest.permission.READ_MEDIA_IMAGES
    )
    
    // 文件选择器（选择 txt 删除列表）
    val filePickerLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.GetContent()
    ) { uri: Uri? ->
        if (uri != null) {
            isLoading = true
            errorMessage = null
            
            scope.launch(Dispatchers.IO) {
                try {
                    val (matchMode, deleteList) = photoMatcher.parseDeleteList(uri)
                    if (deleteList.isEmpty()) {
                        withContext(Dispatchers.Main) {
                            errorMessage = "删除列表为空，请检查文件内容"
                            isLoading = false
                        }
                        return@launch
                    }
                    
                    val matchResult = photoMatcher.matchPhotosInGallery(deleteList, matchMode)
                    
                    withContext(Dispatchers.Main) {
                        matchedPhotos = matchResult.matchedPhotos
                        // 本地导入模式：分析在电脑端异步完成，App 不知道分析总数，
                        // 用 0 表示"无分析总数"，预览页仅显示匹配到的删除条数
                        scannedCount = 0
                        
                        // 清理旧结果并保存新结果（供误触返回/进程回收后恢复）
                        settings.clearPreviewResult()
                        if (matchResult.matchedPhotos.isNotEmpty()) {
                            val localTaskId = "local_${System.currentTimeMillis()}"
                            settings.previewResultTaskId = localTaskId
                            settings.previewScannedCount = 0
                            settings.previewDeleteCount = matchResult.matchedPhotos.size
                            scope.launch(Dispatchers.IO) {
                                PreviewResultStore.savePreview(
                                    photoMatcher.context,
                                    localTaskId,
                                    0,
                                    matchResult.matchedPhotos
                                )
                            }
                        }
                        
                        currentStep = Step.PREVIEW
                        isLoading = false
                        
                        if (matchResult.matchedPhotos.isEmpty()) {
                            errorMessage = "未匹配到任何照片，可能手机相册中已删除"
                        }
                    }
                } catch (e: Exception) {
                    withContext(Dispatchers.Main) {
                        errorMessage = "处理失败：${e.message}"
                        isLoading = false
                    }
                }
            }
        }
    }
    
    // 删除授权
    val deleteRequestLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.StartIntentSenderForResult()
    ) { result ->
        val deleteResult = photoDeleter.handleDeleteResult(
            result.resultCode,
            matchedPhotos.size
        )
        
        if (deleteResult.successCount > 0) {
            // 删除完成，清理持久化的预览结果与 pending 任务
            settings.clearPreviewResult()
            settings.clearPendingTask()
            currentStep = Step.COMPLETED
        } else {
            errorMessage = "删除取消：未获得授权"
        }
    }
    
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("照片清理") },
                actions = {
                    IconButton(onClick = { showSettings = true }) {
                        Icon(Icons.Default.Settings, contentDescription = "设置")
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.primaryContainer,
                    titleContentColor = MaterialTheme.colorScheme.onPrimaryContainer
                )
            )
        }
    ) { padding ->
        Box(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
        ) {
            when (currentStep) {
                Step.IMPORT -> {
                    ImportStep(
                        isLoading = isLoading,
                        errorMessage = errorMessage,
                        hasPermission = permissionState.status.isGranted,
                        onRequestPermission = { permissionState.launchPermissionRequest() },
                        onImportClick = { filePickerLauncher.launch("text/plain") },
                        settings = settings,
                        onRestorePreview = {
                            val savedTaskId = settings.previewResultTaskId
                            if (savedTaskId != null) {
                                scope.launch(Dispatchers.IO) {
                                    val restored = PreviewResultStore.loadPreview(
                                        photoMatcher.context, savedTaskId
                                    )
                                    if (restored != null) {
                                        withContext(Dispatchers.Main) {
                                            matchedPhotos = restored.second
                                            scannedCount = restored.first
                                            errorMessage = null
                                            currentStep = Step.PREVIEW
                                        }
                                    } else {
                                        withContext(Dispatchers.Main) {
                                            settings.clearPreviewResult()
                                        }
                                    }
                                }
                            }
                        }
                    )
                }
                
                Step.REMOTE -> {
                    val context = photoMatcher.context
                    val manager = remember(context) {
                        RemoteAnalysisManager(
                            context = context,
                            settings = settings
                        )
                    }
                    RemoteAnalysisScreen(
                        manager = manager,
                        settings = settings,
                        permissionState = permissionState,
                        onResultReady = { uri, analyzedCount ->
                            // 远程分析完成，下载的删除列表交由本地匹配
                            scope.launch(Dispatchers.IO) {
                                val (matchMode, deleteList) = photoMatcher.parseDeleteList(uri)
                                val matchResult = photoMatcher.matchPhotosInGallery(deleteList, matchMode)
                                withContext(Dispatchers.Main) {
                                    matchedPhotos = matchResult.matchedPhotos
                                    // 远程模式下的"分析总数"是本次上传分析的照片数，
                                    // 而非整个相册扫描数（matchResult.scannedCount）
                                    scannedCount = analyzedCount
                                    
                                    // 清理旧结果并保存新结果（供误触返回/进程回收后恢复）
                                    settings.clearPreviewResult()
                                    if (matchResult.matchedPhotos.isNotEmpty()) {
                                        val taskId = settings.pendingTaskId
                                            ?: "remote_${System.currentTimeMillis()}"
                                        settings.previewResultTaskId = taskId
                                        settings.previewScannedCount = analyzedCount
                                        settings.previewDeleteCount = matchResult.matchedPhotos.size
                                        scope.launch(Dispatchers.IO) {
                                            PreviewResultStore.savePreview(
                                                photoMatcher.context,
                                                taskId,
                                                analyzedCount,
                                                matchResult.matchedPhotos
                                            )
                                        }
                                    }
                                    
                                    currentStep = Step.PREVIEW
                                }
                            }
                        },
                        onBack = {
                            currentStep = Step.IMPORT
                        },
                        onRestorePreview = {
                            val savedTaskId = settings.previewResultTaskId
                            if (savedTaskId != null) {
                                scope.launch(Dispatchers.IO) {
                                    val restored = PreviewResultStore.loadPreview(
                                        photoMatcher.context, savedTaskId
                                    )
                                    if (restored != null) {
                                        withContext(Dispatchers.Main) {
                                            matchedPhotos = restored.second
                                            scannedCount = restored.first
                                            errorMessage = null
                                            currentStep = Step.PREVIEW
                                        }
                                    } else {
                                        withContext(Dispatchers.Main) {
                                            settings.clearPreviewResult()
                                        }
                                    }
                                }
                            }
                        }
                    )
                }
                
                Step.PREVIEW -> {
                    PreviewStep(
                        photos = matchedPhotos,
                        scannedCount = scannedCount,
                        errorMessage = errorMessage,
                        previewInvalidated = previewInvalidated,
                        onConfirmClick = {
                            // 真正的"系统不支持"场景（Android 11 以下）
                            if (!photoDeleter.isDeleteSupported()) {
                                errorMessage = "系统不支持批量删除（需 Android 11+）"
                            } else {
                                // 删除前过滤：剔除用户在预览期间手动删除的失效照片
                                scope.launch(Dispatchers.IO) {
                                    val existing = photoMatcher.filterExistingPhotos(matchedPhotos)
                                    val removedCount = matchedPhotos.size - existing.size
                                    withContext(Dispatchers.Main) {
                                        when {
                                            existing.isEmpty() -> {
                                                // 全部照片已被手动删除
                                                matchedPhotos = emptyList()
                                                previewInvalidated = true
                                                errorMessage = "待删除的照片已从相册删除，请点击返回"
                                            }
                                            removedCount > 0 -> {
                                                // 部分照片已被手动删除，弹提示确认后删除剩余
                                                partialDeleteInfo = existing to removedCount
                                            }
                                            else -> {
                                                // 全部存活，直接删除
                                                val request = photoDeleter.createDeleteRequest(existing)
                                                if (request != null) {
                                                    deleteRequestLauncher.launch(request)
                                                } else {
                                                    errorMessage = "删除请求构造失败，请返回重试"
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        },
                        onBackClick = {
                            // 结果已失效（照片全被手动删除）返回时清理持久化，避免卡片残留
                            if (previewInvalidated) {
                                settings.clearPreviewResult()
                                settings.clearPendingTask()
                                previewInvalidated = false
                            }
                            currentStep = if (settings.useRemoteAnalysis) Step.REMOTE else Step.IMPORT
                            matchedPhotos = emptyList()
                            errorMessage = null
                        }
                    )
                }
                
                Step.COMPLETED -> {
                    CompletedStep(
                        deletedCount = matchedPhotos.size,
                        onResetClick = {
                            // 双保险清理（删除完成时已清理过）
                            settings.clearPreviewResult()
                            settings.clearPendingTask()
                            matchedPhotos = emptyList()
                            currentStep = if (settings.useRemoteAnalysis) Step.REMOTE else Step.IMPORT
                            errorMessage = null
                        }
                    )
                }
            }
        }
    }
    
    // 设置弹窗
    if (showSettings) {
        SettingsDialog(settings) { showSettings = false }
    }
    
    // 部分照片已被手动删除的确认弹窗
    partialDeleteInfo?.let { (existing, removedCount) ->
        AlertDialog(
            onDismissRequest = { partialDeleteInfo = null },
            title = { Text("部分照片已删除") },
            text = {
                Text("有 $removedCount 张照片已被手动删除，将删除剩余 ${existing.size} 张。")
            },
            confirmButton = {
                TextButton(onClick = {
                    partialDeleteInfo = null
                    matchedPhotos = existing
                    val request = photoDeleter.createDeleteRequest(existing)
                    if (request != null) {
                        deleteRequestLauncher.launch(request)
                    } else {
                        errorMessage = "删除请求构造失败，请返回重试"
                    }
                }) {
                    Text("删除剩余")
                }
            },
            dismissButton = {
                TextButton(onClick = { partialDeleteInfo = null }) {
                    Text("取消")
                }
            }
        )
    }
}

/**
 * 步骤 1：导入删除列表
 */
@Composable
fun ImportStep(
    isLoading: Boolean,
    errorMessage: String?,
    hasPermission: Boolean,
    onRequestPermission: () -> Unit,
    onImportClick: () -> Unit,
    settings: SettingsRepository,
    onRestorePreview: () -> Unit
) {
    Column(
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
        modifier = Modifier
            .fillMaxSize()
            .padding(24.dp)
    ) {
        // 已保存结果提示卡片（顶部显示，不阻碍下方操作）
        val savedTaskId = settings.previewResultTaskId
        if (savedTaskId != null) {
            SavedResultPromptCard(
                scannedCount = settings.previewScannedCount,
                deleteCount = settings.previewDeleteCount,
                onRestoreClick = onRestorePreview
            )
            Spacer(modifier = Modifier.height(24.dp))
        }
        
        Text(
            text = "请导入删除列表文件",
            style = MaterialTheme.typography.headlineMedium,
            color = MaterialTheme.colorScheme.onBackground
        )
        
        Spacer(modifier = Modifier.height(16.dp))
        
        Text(
            text = "从电脑生成的 non_highlight_photos_*.txt 文件中导入",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
        
        Spacer(modifier = Modifier.height(32.dp))
        
        if (isLoading) {
            CircularProgressIndicator(
                modifier = Modifier.size(48.dp),
                strokeWidth = 4.dp
            )
            Spacer(modifier = Modifier.height(16.dp))
            Text(
                text = "正在扫描相册匹配照片...",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.primary
            )
            Spacer(modifier = Modifier.height(8.dp))
            Text(
                text = "这可能需要几秒钟",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        } else {
            if (!hasPermission) {
                Text(
                    text = "需要「照片和媒体」权限才能扫描相册",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
                Spacer(modifier = Modifier.height(12.dp))
            }
            Button(
                onClick = {
                    if (hasPermission) {
                        onImportClick()
                    } else {
                        onRequestPermission()
                    }
                },
                modifier = Modifier
                    .fillMaxWidth(0.6f)
                    .height(56.dp)
            ) {
                Text(
                    text = if (hasPermission) "选择文件" else "授权并选择文件",
                    style = MaterialTheme.typography.titleMedium
                )
            }
        }
        
        // 错误提示
        errorMessage?.let { msg ->
            Spacer(modifier = Modifier.height(16.dp))
            Card(
                colors = CardDefaults.cardColors(
                    containerColor = MaterialTheme.colorScheme.errorContainer
                )
            ) {
                Text(
                    text = msg,
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onErrorContainer,
                    modifier = Modifier.padding(16.dp)
                )
            }
        }
    }
}

/**
 * 步骤 2：预览待删除照片
 */
@Composable
fun PreviewStep(
    photos: List<PhotoInfo>,
    scannedCount: Int,
    errorMessage: String?,
    previewInvalidated: Boolean,
    onConfirmClick: () -> Unit,
    onBackClick: () -> Unit
) {
    var showBackConfirmDialog by remember { mutableStateOf(false) }
    
    // 拦截系统返回键，避免误触后离开预览页
    BackHandler(enabled = true) {
        showBackConfirmDialog = true
    }
    
    // 返回确认对话框（正常情况结果会保留；失效情况告知用户照片已被手动删除）
    if (showBackConfirmDialog) {
        AlertDialog(
            onDismissRequest = { showBackConfirmDialog = false },
            title = { Text("返回确认") },
            text = {
                Text(
                    when {
                        previewInvalidated ->
                            "本次结果已处理过，但待删除的照片已被手动从相册删除。\n返回将放弃本次结果并回到初始页面。"
                        photos.isNotEmpty() ->
                            "确定要返回到初始页面吗？\n当前 ${photos.size} 张待删除照片的分析结果会保留。"
                        else ->
                            "确定要返回到初始页面吗？"
                    }
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    showBackConfirmDialog = false
                    onBackClick()
                }) {
                    Text("确定返回")
                }
            },
            dismissButton = {
                TextButton(onClick = { showBackConfirmDialog = false }) {
                    Text("留在此页")
                }
            }
        )
    }
    
    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(16.dp)
    ) {
        // 顶部统计信息
        Card(
            modifier = Modifier.fillMaxWidth(),
            colors = CardDefaults.cardColors(
                containerColor = MaterialTheme.colorScheme.secondaryContainer
            )
        ) {
            Column(
                modifier = Modifier.padding(16.dp)
            ) {
                // scannedCount > 0：远程分析模式，已知本次分析总数，显示完整统计
                // scannedCount == 0：本地导入模式，分析在电脑端完成，仅显示匹配到的删除条数
                if (scannedCount > 0) {
                    val keepCount = (scannedCount - photos.size).coerceAtLeast(0)
                    Text(
                        text = "本次共分析 $scannedCount 张照片",
                        style = MaterialTheme.typography.titleLarge,
                        color = MaterialTheme.colorScheme.onSecondaryContainer
                    )
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        text = "$keepCount 张保留，${photos.size} 张可删除",
                        style = MaterialTheme.typography.titleMedium,
                        color = MaterialTheme.colorScheme.onSecondaryContainer
                    )
                } else {
                    Text(
                        text = "匹配到 ${photos.size} 条需要删除",
                        style = MaterialTheme.typography.titleLarge,
                        color = MaterialTheme.colorScheme.onSecondaryContainer
                    )
                }
                Spacer(modifier = Modifier.height(4.dp))
                Text(
                    text = "预览待删除照片，确认后删除（可在相册「最近删除」中恢复）",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSecondaryContainer
                )
            }
        }
        
        Spacer(modifier = Modifier.height(16.dp))
        
        // 错误提示
        errorMessage?.let { msg ->
            Card(
                modifier = Modifier.fillMaxWidth(),
                colors = CardDefaults.cardColors(
                    containerColor = MaterialTheme.colorScheme.errorContainer
                )
            ) {
                Text(
                    text = msg,
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onErrorContainer,
                    modifier = Modifier.padding(12.dp)
                )
            }
            Spacer(modifier = Modifier.height(16.dp))
        }
        
        // 照片网格（3 列）
        if (photos.isEmpty()) {
            // 空状态占位
            Box(
                modifier = Modifier
                    .weight(1f)
                    .fillMaxWidth(),
                contentAlignment = Alignment.Center
            ) {
                Column(
                    horizontalAlignment = Alignment.CenterHorizontally,
                    verticalArrangement = Arrangement.Center
                ) {
                    Text(
                        text = "📷",
                        style = MaterialTheme.typography.displayMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                    Spacer(modifier = Modifier.height(16.dp))
                    Text(
                        text = "未匹配到照片",
                        style = MaterialTheme.typography.titleMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        text = "删除列表中的照片可能已不在相册中",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
            }
        } else {
            LazyVerticalGrid(
                columns = GridCells.Fixed(3),
                modifier = Modifier.weight(1f),
                contentPadding = PaddingValues(4.dp),
                horizontalArrangement = Arrangement.spacedBy(4.dp),
                verticalArrangement = Arrangement.spacedBy(4.dp)
            ) {
                items(photos) { photo ->
                    Card(
                        modifier = Modifier.aspectRatio(1f),
                        border = if (photo.hasTimeWarning) {
                            androidx.compose.foundation.BorderStroke(
                                2.dp, MaterialTheme.colorScheme.error
                            )
                        } else null
                    ) {
                        Box(modifier = Modifier.fillMaxSize()) {
                            AsyncImage(
                                model = photo.uri,
                                contentDescription = photo.fileName,
                                modifier = Modifier.fillMaxSize(),
                                contentScale = ContentScale.Crop
                            )
                            if (photo.hasTimeWarning) {
                                // 时间戳偏差较大，右上角标记提示人工确认
                                Text(
                                    text = "⚠",
                                    modifier = Modifier
                                        .align(Alignment.TopEnd)
                                        .padding(2.dp),
                                    color = MaterialTheme.colorScheme.error
                                )
        }
    }
}

                }
            }
        }
        
        Spacer(modifier = Modifier.height(16.dp))
        
        // 底部操作按钮
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            OutlinedButton(
                onClick = onBackClick,
                modifier = Modifier.weight(1f)
            ) {
                Text("返回")
            }
            
            Button(
                onClick = onConfirmClick,
                modifier = Modifier.weight(2f),
                enabled = photos.isNotEmpty()
            ) {
                Text("确认删除")
            }
        }
    }
}

/**
 * 已保存结果提示卡片（初始页顶部显示）
 */
@Composable
fun SavedResultPromptCard(
    scannedCount: Int,
    deleteCount: Int,
    onRestoreClick: () -> Unit,
    modifier: Modifier = Modifier
) {
    Card(
        modifier = modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.primaryContainer
        )
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(16.dp),
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            Row(
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    text = "📋",
                    style = MaterialTheme.typography.titleLarge
                )
                Spacer(modifier = Modifier.width(8.dp))
                Text(
                    text = "有未完成的分析结果",
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.Bold,
                    color = MaterialTheme.colorScheme.onPrimaryContainer
                )
            }
            
            Spacer(modifier = Modifier.height(8.dp))
            
            Text(
                text = if (scannedCount > 0) {
                    "上次分析了 $scannedCount 张照片，匹配到 $deleteCount 张待删除"
                } else {
                    "匹配到 $deleteCount 张待删除"
                },
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onPrimaryContainer,
                textAlign = TextAlign.Center
            )
            
            Spacer(modifier = Modifier.height(12.dp))
            
            Button(
                onClick = onRestoreClick,
                modifier = Modifier.fillMaxWidth(0.7f)
            ) {
                Text("继续查看结果")
            }
            
            Spacer(modifier = Modifier.height(8.dp))
            
            Text(
                text = "💡 开始新任务会自动放弃上次结果",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = TextAlign.Center
            )
        }
    }
}

/**
 * 步骤 3：删除完成
 */
@Composable
fun CompletedStep(
    deletedCount: Int,
    onResetClick: () -> Unit
) {
    Column(
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
        modifier = Modifier
            .fillMaxSize()
            .padding(24.dp)
    ) {
        Text(
            text = "✓",
            style = MaterialTheme.typography.displayLarge,
            color = MaterialTheme.colorScheme.primary
        )
        
        Spacer(modifier = Modifier.height(16.dp))
        
        Text(
            text = "已成功删除 $deletedCount 张照片",
            style = MaterialTheme.typography.headlineMedium,
            color = MaterialTheme.colorScheme.onBackground
        )
        
        Spacer(modifier = Modifier.height(8.dp))
        
        Text(
            text = "照片已从相册删除",
            style = MaterialTheme.typography.bodyLarge,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
        
        Spacer(modifier = Modifier.height(4.dp))
        
        Text(
            text = "30 天内可在相册「最近删除」中恢复",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
        
        Spacer(modifier = Modifier.height(32.dp))
        
        Button(
            onClick = onResetClick,
            modifier = Modifier
                .fillMaxWidth(0.6f)
                .height(56.dp)
        ) {
            Text(
                text = "重新导入",
                style = MaterialTheme.typography.titleMedium
            )
        }
    }
}
