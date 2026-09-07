package com.example.photocleaner.remote

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import com.example.photocleaner.R
import kotlinx.coroutines.*
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

/**
 * 上传前台服务
 * 
 * Android 14+ 要求长时间运行的任务必须用 Foreground Service。
 * 用户切到其他 APP 或锁屏时，上传不被系统杀掉。
 * 
 * 通知栏显示：上传进度 X/N 块
 * 上传完成后自动停止服务。
 */
class UploadService : Service() {
    
    companion object {
        private const val TAG = "UploadService"
        private const val NOTIFICATION_ID = 1001
        private const val CHANNEL_ID = "upload_channel"
        
        const val EXTRA_THUMBNAILS_DIR = "thumbnails_dir"
        const val EXTRA_CHUNK_SIZE = "chunk_size"
        // 注意：base_url / auth_token / extraction_level 已改为从 SettingsRepository
        // 读取，不再通过 Intent extra 传递，避免 token 出现在系统 intent parcel/dump 中。
    }
    
    private val serviceScope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private lateinit var notificationManager: NotificationManager
    
    override fun onCreate() {
        super.onCreate()
        notificationManager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        createNotificationChannel()
    }
    
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // Android 14+ 要求立即调用 startForeground（5 秒内）
        val notification = createNotification("准备上传...", 0, 0)
        startForeground(NOTIFICATION_ID, notification)
        
        if (intent == null) {
            Log.w(TAG, "Intent is null, stopping service")
            stopSelf()
            return START_NOT_STICKY
        }
        
        // 提取参数：token / baseUrl / extractionLevel 从 SettingsRepository 读，
        // 不走 Intent extra（token 若在 extra 里会出现在系统 dump/logcat 中）
        val thumbnailsDir = intent.getStringExtra(EXTRA_THUMBNAILS_DIR) ?: run {
            Log.w(TAG, "Missing thumbnails_dir")
            stopSelf()
            return START_NOT_STICKY
        }
        val chunkSize = intent.getIntExtra(EXTRA_CHUNK_SIZE, 50)
        val settings = SettingsRepository(this)
        val baseUrl = settings.homelabAddress
        val authToken = settings.authToken
        val extractionLevel = settings.extractionLevel
        if (baseUrl.isBlank() || authToken.isBlank()) {
            Log.w(TAG, "Missing baseUrl or authToken in settings")
            stopSelf()
            return START_NOT_STICKY
        }

        // 启动上传任务
        serviceScope.launch {
            try {
                uploadThumbnails(File(thumbnailsDir), chunkSize, baseUrl, authToken, extractionLevel)
            } catch (e: Exception) {
                Log.e(TAG, "上传失败", e)
                val friendly = friendlyError(e)
                updateNotification(friendly, 0, 0, isError = true)
                // 写入错误到 SharedPreferences，供 UPLOADING 界面读取并转入 ERROR 状态
                getSharedPreferences("upload_progress", Context.MODE_PRIVATE).edit()
                    .putString("upload_error", friendly)
                    .apply()
            } finally {
                stopSelf()
            }
        }
        
        return START_NOT_STICKY
    }
    
    override fun onBind(intent: Intent?): IBinder? = null
    
    override fun onDestroy() {
        serviceScope.cancel()
        super.onDestroy()
    }
    
    /**
     * 上传缩略图（分块 + 压缩 + 断点续传）
     */
    private suspend fun uploadThumbnails(
        thumbnailsDir: File,
        chunkSize: Int,
        baseUrl: String,
        authToken: String,
        extractionLevel: String?
    ) {
        val client = HomelabClient(baseUrl, authToken)
        
        // 进度写入 SharedPreferences，供 UPLOADING 界面读取显示
        val progressPrefs = getSharedPreferences("upload_progress", Context.MODE_PRIVATE)
        
        // 1. 收集所有缩略图文件
        val thumbnails = thumbnailsDir.listFiles { file ->
            file.isFile && (file.extension == "jpg" || file.extension == "jpeg" || file.extension == "png")
        }?.toList() ?: emptyList()
        
        if (thumbnails.isEmpty()) {
            updateNotification("无缩略图可上传", 0, 0, isError = true)
            return
        }
        
        // 2. 分块
        val chunks = thumbnails.chunked(chunkSize)
        val totalChunks = chunks.size
        
        Log.i(TAG, "开始上传: ${thumbnails.size} 张缩略图，分为 $totalChunks 块")
        
        // 3. 压缩并上传每个块
        var taskId: String? = null
        
        chunks.forEachIndexed { index, files ->
            val current = index + 1
            // 写入进度到 SharedPreferences（供界面轮询读取）
            progressPrefs.edit()
                .putInt("current", current)
                .putInt("total", totalChunks)
                .apply()
            
            updateNotification("上传 $current/$totalChunks 块", current, totalChunks)
            
            // 压缩块
            val chunkData = zipFiles(files)
            
            // 上传
            val result = client.uploadChunk(taskId, index, totalChunks, chunkData)
            taskId = result.taskId
            
            if (!result.accepted) {
                throw Exception("服务端拒绝块 $index")
            }
            
            Log.i(TAG, "块 $index 上传成功 (${chunkData.size / 1024}KB)")
        }
        
        // 4. 完成上传
        val finalTaskId = taskId
        if (finalTaskId != null) {
            updateNotification("完成上传，启动处理...", totalChunks, totalChunks)
            
            try {
                // 使用传入的 extractionLevel 参数
                client.finalize(finalTaskId, extractionLevel)
                Log.i(TAG, "上传完成, task_id: $finalTaskId, extraction_level: $extractionLevel")
                
                // 保存 task_id 到 SharedPreferences（供主界面查询）
                val settings = SettingsRepository(this@UploadService)
                settings.pendingTaskId = finalTaskId
                
                // 清理进度（上传阶段结束）
                progressPrefs.edit().clear().apply()
                
                updateNotification("上传完成！", totalChunks, totalChunks, isComplete = true)
            } catch (e: Exception) {
                Log.e(TAG, "finalize 失败", e)
                // 写入错误供界面读取（结构化错误转中文提示）
                progressPrefs.edit()
                    .putString("upload_error", friendlyError(e))
                    .apply()
                throw e  // 重新抛出，由外层 catch 处理通知
            }
        }
    }
    
    /**
     * 将异常转换为面向用户的中文提示。
     * 对结构化的 ApiException 按 error_code 给出明确原因；其他异常回退到通用提示。
     */
    private fun friendlyError(e: Throwable): String {
        if (e is HomelabClient.ApiException) {
            val err = e.apiError
            return when (err.errorCode) {
                "server_busy" -> "服务器繁忙，处理队列已满，请稍后再试"
                "too_many_photos" -> err.message ?: "照片数量超过上限，请减少后重试"
                "chunk_too_large" -> "上传分块过大，请重试或联系管理员"
                "malicious_archive", "archive_too_large", "corrupt_archive" ->
                    err.message ?: "上传内容异常，请重新发起"
                "incomplete_upload" -> "上传未完成，请重试"
                "invalid_token" -> "认证失败，请检查设置中的 Token"
                "sha256_mismatch" -> "分块校验失败，请重新上传"
                else -> err.message ?: "上传失败（HTTP ${err.httpCode}）"
            }
        }
        return "上传失败: ${e.message ?: "未知错误"}"
    }

    /**
     * 压缩多个文件为 ZIP（临时文件会在返回后自动删除）
     */
    private fun zipFiles(files: List<File>): ByteArray {
        val outputFile = File.createTempFile("chunk_", ".zip", cacheDir)
        try {
            ZipOutputStream(FileOutputStream(outputFile)).use { zos ->
                files.forEach { file ->
                    val entry = ZipEntry(file.name)
                    zos.putNextEntry(entry)
                    FileInputStream(file).use { fis ->
                        fis.copyTo(zos)
                    }
                    zos.closeEntry()
                }
            }
            return outputFile.readBytes()
        } finally {
            // 清理临时 ZIP 文件
            outputFile.delete()
        }
    }
    
    /**
     * 创建通知渠道（Android 8.0+）
     */
    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "照片上传",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "远程分析上传进度"
                setShowBadge(false)
            }
            notificationManager.createNotificationChannel(channel)
        }
    }
    
    /**
     * 创建通知
     */
    private fun createNotification(
        text: String,
        current: Int,
        total: Int,
        isError: Boolean = false,
        isComplete: Boolean = false
    ): Notification {
        val builder = NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("PhotoCleaner")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_sys_upload)  // 使用系统图标
            .setOngoing(!isError && !isComplete)
            .setPriority(NotificationCompat.PRIORITY_LOW)
        
        if (total > 0 && !isError && !isComplete) {
            builder.setProgress(total, current, false)
        }
        
        if (isError) {
            builder.setSmallIcon(android.R.drawable.stat_notify_error)
        } else if (isComplete) {
            builder.setSmallIcon(android.R.drawable.stat_sys_upload_done)
        }
        
        return builder.build()
    }
    
    /**
     * 更新通知
     */
    private fun updateNotification(
        text: String,
        current: Int,
        total: Int,
        isError: Boolean = false,
        isComplete: Boolean = false
    ) {
        val notification = createNotification(text, current, total, isError, isComplete)
        notificationManager.notify(NOTIFICATION_ID, notification)
    }
}
