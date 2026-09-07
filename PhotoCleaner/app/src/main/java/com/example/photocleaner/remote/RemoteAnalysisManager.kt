package com.example.photocleaner.remote

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.MediaStore
import android.util.Log
import com.example.photocleaner.models.PhotoInfo
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.util.Calendar

/**
 * 远程分析业务逻辑封装（非 UI）
 * 
 * 职责：
 * 1. 按日期范围扫描相册
 * 2. 生成缩略图
 * 3. 启动上传服务
 * 4. 轮询任务状态
 * 5. 下载删除列表
 */
class RemoteAnalysisManager(
    private val context: Context,
    private val settings: SettingsRepository
) {
    companion object {
        private const val TAG = "RemoteAnalysisManager"
    }
    
    private val client: HomelabClient
        get() = HomelabClient(settings.homelabAddress, settings.authToken)
    
    /**
     * 按日期范围扫描相册
     * 
     * @param startMs 起始时间（毫秒，含）
     * @param endMs 结束时间（毫秒，含）
     */
    fun scanPhotosByDateRange(startMs: Long, endMs: Long): List<PhotoInfo> {
        val photos = mutableListOf<PhotoInfo>()
        
        val projection = arrayOf(
            MediaStore.Images.Media._ID,
            MediaStore.Images.Media.DISPLAY_NAME,
            MediaStore.Images.Media.DATE_TAKEN,
            MediaStore.Images.Media.SIZE
        )
        
        // DATE_TAKEN 在 [startMs, endMs] 之间
        val selection = "${MediaStore.Images.Media.DATE_TAKEN} >= ? AND " +
                "${MediaStore.Images.Media.DATE_TAKEN} <= ?"
        val selectionArgs = arrayOf(startMs.toString(), endMs.toString())
        
        context.contentResolver.query(
            MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
            projection,
            selection,
            selectionArgs,
            "${MediaStore.Images.Media.DATE_TAKEN} ASC"
        )?.use { cursor ->
            val idCol = cursor.getColumnIndexOrThrow(MediaStore.Images.Media._ID)
            val nameCol = cursor.getColumnIndexOrThrow(MediaStore.Images.Media.DISPLAY_NAME)
            val dateCol = cursor.getColumnIndexOrThrow(MediaStore.Images.Media.DATE_TAKEN)
            val sizeCol = cursor.getColumnIndexOrThrow(MediaStore.Images.Media.SIZE)
            
            while (cursor.moveToNext()) {
                val id = cursor.getLong(idCol)
                val uri = android.content.ContentUris.withAppendedId(
                    MediaStore.Images.Media.EXTERNAL_CONTENT_URI, id
                )
                photos.add(
                    PhotoInfo(
                        id = id,
                        uri = uri,
                        fileName = cursor.getString(nameCol),
                        dateTaken = cursor.getLong(dateCol),
                        size = cursor.getLong(sizeCol)
                    )
                )
            }
        }
        
        Log.i(TAG, "扫描到 ${photos.size} 张照片 (范围: $startMs ~ $endMs)")
        return photos
    }
    
    /**
     * 生成缩略图
     */
    suspend fun generateThumbnails(
        photos: List<PhotoInfo>,
        onProgress: (Int, Int) -> Unit
    ): ThumbnailGenerator.GenerateResult = withContext(Dispatchers.IO) {
        val outputDir = File(context.cacheDir, "thumbnails")
        // 清空旧缩略图（释放空间）
        if (outputDir.exists()) {
            outputDir.listFiles()?.forEach { it.delete() }
        }
        
        val generator = ThumbnailGenerator(context)
        generator.generateBatch(photos, outputDir, onProgress)
    }
    
    /**
     * 下载结果后清理缩略图（释放空间）
     */
    suspend fun cleanupThumbnails() = withContext(Dispatchers.IO) {
        val outputDir = File(context.cacheDir, "thumbnails")
        if (outputDir.exists()) {
            val deleted = outputDir.listFiles()?.size ?: 0
            outputDir.deleteRecursively()
            Log.i(TAG, "清理缩略图: $deleted 个文件")
        }
    }
    
    /**
     * 轻量健康检查（免认证 /api/health）：仅确认服务存活 + 测 RTT，不含 provider/model。
     */
    suspend fun checkHealth(): HomelabClient.HealthResult = withContext(Dispatchers.IO) {
        client.healthCheck()
    }

    /**
     * 详细健康检查（需认证 /api/health/detailed）：返回 provider/model + 模型探活，
     * 同时隐式验证 token（401 -> ok=false, error="Token 无效"）。
     * 用于开始处理前的确认框：一次请求拿到 provider 以计算精确预估耗时。
     */
    suspend fun checkHealthDetailed(): HomelabClient.HealthResult = withContext(Dispatchers.IO) {
        client.healthCheckDetailed()
    }
    
    /**
     * 取消服务端任务（用户主动放弃）
     */
    suspend fun cancelTask(taskId: String) = withContext(Dispatchers.IO) {
        client.cancelTask(taskId)
    }

    /**
     * 停止上传前台服务
     */
    fun stopUploadService() {
        val intent = Intent(context, UploadService::class.java)
        context.stopService(intent)
    }

    /**
     * 估算整个处理流程的耗时区间（缩略图生成 + 上传 + 服务端分析）
     *
     * 在扫描相册后、检查服务后展示，基于实际 provider 精确估算。
     * - 缩略图生成：约 0.2~0.5 秒/张（手机端）
     * - 上传：100KB/张，最快局域网 100Mbps(≈12.5MB/s)、最慢 3Mbps(≈0.375MB/s)
     * - 服务端分析（线性模型：固定开销 + 每张系数，用两组实测点拟合）：
     *   * Ollama：实测 (3张,70s) + (152张,762s) → 固定开销 56s + 4.6s/张
     *   * 在线 API：实测 (3张,30s) + (152张,210s) → 固定开销 26s + 1.2s/张
     *   固定开销来自模型预热(ollama 加载显存)、stage01a/01b 预处理，几乎不随照片数变化。
     *   slow 边界 = fast × 1.5（整体放大，避免小批量过于乐观、大批量过于悲观）
     *
     * @param photoCount 照片数量
     * @param provider 推理提供商（ollama / 在线 API 等）
     * @return 人类可读的时间区间文本，如 "约 1 分钟 ~ 2 分钟"
     */
    fun estimateProcessingTime(photoCount: Int, provider: String): String {
        // 缩略图生成
        val thumbFast = photoCount * 0.2
        val thumbSlow = photoCount * 0.5

        // 上传
        val bytesPerPhoto = 100 * 1024L
        val totalBytes = photoCount * bytesPerPhoto
        val uploadFast = totalBytes / (12.5 * 1024 * 1024)
        val uploadSlow = totalBytes / (0.375 * 1024 * 1024)

        // 服务端分析：固定开销 + 每张系数（两组实测点线性拟合）
        val (fixedOverhead, perPhotoCoeff) = when (provider.lowercase()) {
            // 本地模型（ollama/lmstudio 等）：(3,70s) + (152,762s) 拟合
            "ollama", "lmstudio", "ollama-new" -> 56.0 to 4.6
            else     -> 26.0 to 1.2   // 在线 API (3,30s) + (152,210s) 拟合
        }
        val analyzeFast = fixedOverhead + photoCount * perPhotoCoeff
        val analyzeSlow = analyzeFast * 1.5  // slow = fast × 1.5 整体放大

        val totalFast = (thumbFast + uploadFast + analyzeFast).toInt().coerceAtLeast(1)
        val totalSlow = (thumbSlow + uploadSlow + analyzeSlow).toInt().coerceAtLeast(1)

        return formatRange(totalFast, totalSlow)
    }

    /**
     * 格式化时间区间，"约" 只出现一次；两端数值相同时只显示一个。
     * 
     * 分档规则（按 slow 边界决定单位，避免跨单位混搭）：
     * 1. slow < 60 秒：用秒（特别小才用秒），如 "约 30 ~ 50 秒"
     * 2. slow < 1 小时：用分钟，如 "约 3 ~ 5 分钟"
     * 3. fast < 1 小时 ≤ slow：临界区统一用分钟（不混搭），如 "约 50 ~ 75 分钟"
     * 4. fast ≥ 1 小时：用 "XX 小时 XX 分钟"，如 "约 1 小时 12 分钟 ~ 1 小时 56 分钟"
     */
    private fun formatRange(fastSec: Int, slowSec: Int): String {
        // 分钟数用四舍五入（比向下取整更贴近真实值，如 106 秒→2 分钟而非 1 分钟）
        val minuteRender: (Int) -> String = { "${Math.round(it / 60.0)} 分钟" }
        return when {
            // 1. 特别小：用秒
            slowSec < 60 -> rangeText(fastSec, slowSec) { "$it 秒" }
            // 2. slow 不足 1 小时：用分钟
            slowSec < 3600 -> rangeText(fastSec, slowSec, minuteRender)
            // 3. 临界区（fast 不足 1 小时，slow 已过）：统一用分钟，避免混搭
            fastSec < 3600 -> rangeText(fastSec, slowSec, minuteRender)
            // 4. 大批量：小时 + 分钟
            else -> rangeText(fastSec, slowSec) { formatSeconds(it) }
        }
    }

    /**
     * 生成区间文本："约 A ~ B"；A、B 文本相同时折叠为 "约 A"。
     * @param render 单个秒数的渲染函数
     */
    private fun rangeText(
        fastSec: Int,
        slowSec: Int,
        render: (Int) -> String
    ): String {
        val fastStr = render(fastSec)
        val slowStr = render(slowSec)
        return if (fastStr == slowStr) "约 $fastStr" else "约 $fastStr ~ $slowStr"
    }

    private fun formatSeconds(sec: Int): String {
        return when {
            sec < 60 -> "$sec 秒"
            sec < 3600 -> "${sec / 60} 分钟"
            else -> {
                val hours = sec / 3600
                val mins = (sec % 3600) / 60
                if (mins > 0) {
                    "$hours 小时 $mins 分钟"
                } else {
                    "$hours 小时"
                }
            }
        }
    }

    /**
     * 启动上传服务
     */
    fun startUploadService() {
        val thumbnailsDir = File(context.cacheDir, "thumbnails")
        // token / baseUrl / extractionLevel 由 UploadService 自行从 SettingsRepository 读，
        // 不走 Intent extra，避免 token 出现在系统 intent parcel。
        val intent = Intent(context, UploadService::class.java).apply {
            putExtra(UploadService.EXTRA_THUMBNAILS_DIR, thumbnailsDir.absolutePath)
            // 分块大小固定为常量（不再由用户配置）
            putExtra(UploadService.EXTRA_CHUNK_SIZE, SettingsRepository.DEFAULT_CHUNK_SIZE)
        }
        context.startForegroundService(intent)
    }
    
    /**
     * 查询任务状态
     */
    suspend fun getTaskStatus(taskId: String): HomelabClient.TaskStatus = 
        withContext(Dispatchers.IO) {
            client.getTaskStatus(taskId)
        }
    
    /**
     * 下载删除列表到临时文件
     * 
     * @return 下载的文件 Uri
     */
    suspend fun downloadResult(taskId: String): Uri = withContext(Dispatchers.IO) {
        val outputFile = File(context.cacheDir, "delete_list_$taskId.txt")
        client.downloadResult(taskId, outputFile)
        Uri.fromFile(outputFile)
    }
    
    /**
     * 计算日期范围的毫秒边界
     * 
     * @param daysAgo 0=今天, 1=昨天, 7=最近7天
     */
    fun getDateRange(daysAgo: Int): Pair<Long, Long> {
        val cal = Calendar.getInstance()
        // 今天的结束（23:59:59.999）
        cal.set(Calendar.HOUR_OF_DAY, 23)
        cal.set(Calendar.MINUTE, 59)
        cal.set(Calendar.SECOND, 59)
        cal.set(Calendar.MILLISECOND, 999)
        val endMs = cal.timeInMillis
        
        // daysAgo 天前的开始（00:00:00.000）
        cal.add(Calendar.DAY_OF_YEAR, -daysAgo)
        cal.set(Calendar.HOUR_OF_DAY, 0)
        cal.set(Calendar.MINUTE, 0)
        cal.set(Calendar.SECOND, 0)
        cal.set(Calendar.MILLISECOND, 0)
        val startMs = cal.timeInMillis
        
        return startMs to endMs
    }
}
