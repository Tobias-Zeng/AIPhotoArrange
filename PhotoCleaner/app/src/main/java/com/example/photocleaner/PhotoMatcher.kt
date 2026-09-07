package com.example.photocleaner

import android.content.ContentUris
import android.content.Context
import android.net.Uri
import android.provider.MediaStore
import android.util.Log
import com.example.photocleaner.models.PhotoInfo
import com.example.photocleaner.models.PhotoToDelete
import kotlin.math.abs

/**
 * 匹配结果
 * @param matchedPhotos 匹配到的待删除照片列表
 * @param scannedCount 扫描的相册照片总数
 */
data class MatchResult(
    val matchedPhotos: List<PhotoInfo>,
    val scannedCount: Int
)

/**
 * 照片匹配模块
 * 
 * 职责：
 * 1. 解析 PC 端生成的删除列表文件（支持 SIZE 和 DATETIME 两种模式）
 * 2. 扫描手机相册（MediaStore 查询）
 * 3. 根据模式进行匹配：
 *    - SIZE 模式：文件名 + 大小（±1KB 容差）
 *    - DATETIME 模式：文件名 + 拍摄时间（±12 小时容差，覆盖时区偏差）
 */
class PhotoMatcher(val context: Context) {
    
    companion object {
        private const val TAG = "PhotoMatcher"
        // SIZE 模式：文件大小匹配容差 ±1KB
        private const val SIZE_TOLERANCE = 1024L
        // DATETIME 模式：时间戳匹配容差 ±12 小时（覆盖时区偏差和跨年同名）
        private const val DATE_TOLERANCE_MS = 43200000L  // 12 * 3600 * 1000
    }
    
    /**
     * 匹配模式枚举
     */
    enum class MatchMode {
        SIZE,       // 旧：文件名|大小
        DATETIME    // 新：文件名|epoch秒时间戳
    }
    
    /**
     * 解析删除列表文件
     * 
     * @param uri 文件 URI（用户通过文件选择器选择）
     * @return Pair(匹配模式, 待删除照片列表)
     * 
     * 文件格式示例：
     * 
     * DATETIME 模式（新）：
     * ```
     * #MATCH_MODE=DATETIME
     * IMG_0001.jpg|1754630400
     * IMG_0002.jpg|1754630405
     * ```
     * 
     * SIZE 模式（旧，向后兼容）：
     * ```
     * IMG_0001.jpg|2456789
     * IMG_0002.jpg|3456789
     * ```
     * 或
     * ```
     * #MATCH_MODE=SIZE
     * IMG_0001.jpg|2456789
     * ```
     */
    fun parseDeleteList(uri: Uri): Pair<MatchMode, List<PhotoToDelete>> {
        val lines = mutableListOf<String>()
        
        try {
            context.contentResolver.openInputStream(uri)?.use { input ->
                input.bufferedReader().useLines { lineSeq ->
                    lines.addAll(lineSeq.toList())
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "读取文件失败", e)
            return Pair(MatchMode.SIZE, emptyList())
        }
        
        // 检测模式
        val mode = detectMode(lines)
        Log.i(TAG, "检测到匹配模式: $mode")
        
        // 解析数据行
        val list = mutableListOf<PhotoToDelete>()
        lines.forEachIndexed { index, line ->
            val trimmed = line.trim()
            
            // 跳过空行和注释行
            if (trimmed.isEmpty() || trimmed.startsWith("#")) {
                return@forEachIndexed
            }
            
            // 解析 "文件名|值" 格式
            val parts = trimmed.split('|')
            if (parts.size == 2) {
                val name = parts[0].trim()
                val value = parts[1].trim().toLongOrNull() ?: -1
                
                if (name.isNotEmpty()) {
                    when (mode) {
                        MatchMode.SIZE -> list.add(PhotoToDelete(name, size = value))
                        MatchMode.DATETIME -> {
                            // epoch 秒转毫秒
                            val dateTakenMs = if (value > 0) value * 1000 else -1
                            list.add(PhotoToDelete(name, dateTaken = dateTakenMs))
                        }
                    }
                } else {
                    Log.w(TAG, "第 ${index + 1} 行：文件名为空，跳过")
                }
            } else {
                Log.w(TAG, "第 ${index + 1} 行格式错误（需要 '文件名|值'）")
            }
        }
        
        Log.i(TAG, "解析删除列表完成：共 ${list.size} 条记录")
        return Pair(mode, list)
    }
    
    /**
     * 检测匹配模式
     */
    private fun detectMode(lines: List<String>): MatchMode {
        // 查找首行标识
        val firstLine = lines.firstOrNull()?.trim() ?: ""
        
        when {
            firstLine == "#MATCH_MODE=DATETIME" -> return MatchMode.DATETIME
            firstLine == "#MATCH_MODE=SIZE" -> return MatchMode.SIZE
        }
        
        // 自动检测：查找第一行数据
        val firstDataLine = lines.firstOrNull { 
            !it.trim().isEmpty() && !it.trim().startsWith("#") && "|" in it 
        }
        
        if (firstDataLine != null) {
            val parts = firstDataLine.split('|')
            if (parts.size == 2) {
                val value = parts[1].trim().toLongOrNull() ?: 0
                // epoch 秒时间戳通常 > 10^9（约 2001 年）
                // 文件大小通常 < 10^9（1GB）
                if (value > 1000000000L) {
                    return MatchMode.DATETIME
                }
            }
        }
        
        // 默认旧模式
        return MatchMode.SIZE
    }
    
    /**
     * 扫描手机相册并匹配待删除照片
     * 
     * @param deleteList 待删除照片列表
     * @param mode 匹配模式
     * @return 匹配到的照片信息列表
     * 
     * 匹配规则：
     * SIZE 模式：
     * - 文件名必须完全一致
     * - 文件大小在 ±1KB 容差范围内（或 PC 端未提供大小时仅比对文件名）
     * 
     * DATETIME 模式：
     * - 文件名必须完全一致
     * - 拍摄时间在 ±12 小时容差范围内（覆盖时区偏差）
     * - 时间偏差 > 2 秒但 < 12 小时的，标记 hasTimeWarning 供 UI 高亮提示
     */
    fun matchPhotosInGallery(
        deleteList: List<PhotoToDelete>,
        mode: MatchMode
    ): MatchResult {
        val matched = mutableListOf<PhotoInfo>()
        var totalScanned = 0
        
        Log.i(TAG, "开始扫描相册，模式: $mode, 待匹配照片数：${deleteList.size}")
        
        try {
            // 查询 MediaStore
            val projection = arrayOf(
                MediaStore.Images.Media._ID,
                MediaStore.Images.Media.DISPLAY_NAME,
                MediaStore.Images.Media.DATE_TAKEN,
                MediaStore.Images.Media.SIZE
            )
            
            val cursor = context.contentResolver.query(
                MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
                projection,
                null,
                null,
                "${MediaStore.Images.Media.DATE_TAKEN} DESC"
            )
            
            cursor?.use {
                val idColumn = it.getColumnIndexOrThrow(MediaStore.Images.Media._ID)
                val nameColumn = it.getColumnIndexOrThrow(MediaStore.Images.Media.DISPLAY_NAME)
                val dateColumn = it.getColumnIndexOrThrow(MediaStore.Images.Media.DATE_TAKEN)
                val sizeColumn = it.getColumnIndexOrThrow(MediaStore.Images.Media.SIZE)
                
                var scannedCount = 0
                while (it.moveToNext()) {
                    scannedCount++
                    
                    val fileName = it.getString(nameColumn)
                    val fileDate = it.getLong(dateColumn)
                    val fileSize = it.getLong(sizeColumn)
                    
                    // 查找匹配的删除项
                    val matchedItem = deleteList.firstOrNull { item -> item.name == fileName }
                    
                    if (matchedItem != null) {
                        val (isMatch, hasWarning) = when (mode) {
                            MatchMode.SIZE -> {
                                val sizeMatched = matchedItem.size == -1L || 
                                                 abs(fileSize - matchedItem.size) < SIZE_TOLERANCE
                                Pair(sizeMatched, false)
                            }
                            MatchMode.DATETIME -> {
                                if (matchedItem.dateTaken <= 0) {
                                    // PC 端未提供时间戳，回退纯文件名匹配
                                    Pair(true, false)
                                } else {
                                    val timeDiff = abs(fileDate - matchedItem.dateTaken)
                                    val matched = timeDiff < DATE_TOLERANCE_MS
                                    val warning = matched && timeDiff > 2000  // > 2 秒偏差
                                    Pair(matched, warning)
                                }
                            }
                        }
                        
                        if (isMatch) {
                            val id = it.getLong(idColumn)
                            val uri = ContentUris.withAppendedId(
                                MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
                                id
                            )
                            
                            matched.add(
                                PhotoInfo(
                                    id = id,
                                    uri = uri,
                                    fileName = fileName,
                                    dateTaken = fileDate,
                                    size = fileSize,
                                    hasTimeWarning = hasWarning
                                )
                            )
                            
                            if (mode == MatchMode.SIZE) {
                                Log.d(TAG, "SIZE 匹配：$fileName (期望:${matchedItem.size}, 实际:$fileSize)")
                            } else {
                                val timeDiff = if (matchedItem.dateTaken > 0) 
                                    abs(fileDate - matchedItem.dateTaken) / 1000 else 0
                                Log.d(TAG, "DATETIME 匹配：$fileName (时间偏差:${timeDiff}秒${if (hasWarning) " ⚠️" else ""})")
                            }
                        } else {
                            if (mode == MatchMode.SIZE) {
                                Log.d(TAG, "文件名匹配但大小不符：$fileName (期望:${matchedItem.size}, 实际:$fileSize)")
                            } else {
                                val timeDiff = abs(fileDate - matchedItem.dateTaken) / 1000
                                Log.d(TAG, "文件名匹配但时间不符：$fileName (偏差:${timeDiff}秒 > 12小时)")
                            }
                        }
                    }
                }
                
                totalScanned = scannedCount
                Log.i(TAG, "相册扫描完成：共扫描 $scannedCount 张照片，匹配到 ${matched.size} 张")
            }
        } catch (e: Exception) {
            Log.e(TAG, "扫描相册失败", e)
        }
        
        return MatchResult(matchedPhotos = matched, scannedCount = totalScanned)
    }
    
    /**
     * 过滤出仍存在于相册中的照片
     * 
     * 用户可能在结果预览页停留期间（或返回后再回来）手动删除了部分/全部照片，
     * 导致 matchedPhotos 中的 URI 失效。删除前先用 _ID 批量查询 MediaStore，
     * 只保留仍存活的照片，避免对失效 URI 构造删除请求而抛异常。
     * 
     * @param photos 待校验的照片列表
     * @return 仍存在于相册中的照片列表（保持原顺序）
     */
    fun filterExistingPhotos(photos: List<PhotoInfo>): List<PhotoInfo> {
        if (photos.isEmpty()) return emptyList()
        
        val existingIds = mutableSetOf<Long>()
        
        try {
            val ids = photos.map { it.id }
            // 构造 _ID IN (?,?,...) 查询
            val placeholders = ids.joinToString(",") { "?" }
            val selection = "${MediaStore.Images.Media._ID} IN ($placeholders)"
            val selectionArgs = ids.map { it.toString() }.toTypedArray()
            
            context.contentResolver.query(
                MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
                arrayOf(MediaStore.Images.Media._ID),
                selection,
                selectionArgs,
                null
            )?.use { cursor ->
                val idColumn = cursor.getColumnIndexOrThrow(MediaStore.Images.Media._ID)
                while (cursor.moveToNext()) {
                    existingIds.add(cursor.getLong(idColumn))
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "校验照片存活状态失败", e)
            // 查询失败时保守返回原列表，让后续删除流程正常处理
            return photos
        }
        
        val existing = photos.filter { it.id in existingIds }
        Log.i(TAG, "存活校验：原 ${photos.size} 张，仍存在 ${existing.size} 张，" +
            "已被手动删除 ${photos.size - existing.size} 张")
        return existing
    }
}
