package com.example.photocleaner.remote

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.media.ExifInterface
import android.net.Uri
import android.provider.MediaStore
import android.util.Log
import com.example.photocleaner.models.PhotoInfo
import java.io.File
import java.io.FileOutputStream

/**
 * 缩略图生成器
 * 
 * 核心逻辑：
 * 1. 从相册读取原图，缩放到 768px（保留长宽比）
 * 2. 用 inSampleSize 避免 OOM（先读尺寸，再采样解码）
 * 3. JPEG q=85 压缩，确保 ≥30KB（否则提高 q 到 95）
 * 4. 只复制原图时间 EXIF（隐私：不复制 GPS），Orientation 重置为 1（避免服务端二次旋转）
 * 
 * 预筛（对齐 PC 端 01a is_obvious_trash）：
 * - 原图分辨率 < 400px 视为废片，跳过
 * - 长宽比 > 3.0 视为废片，跳过
 * 被跳过的照片记录到 SkippedPhoto 列表，供 UI 提示
 */
class ThumbnailGenerator(private val context: Context) {
    
    companion object {
        private const val TAG = "ThumbnailGenerator"
        private const val MAX_SIZE = 768          // 最大边
        private const val MIN_RESOLUTION = 400    // 最小分辨率（对齐 01a）
        private const val MAX_ASPECT_RATIO = 3.0f // 最大长宽比（对齐 01a）
        private const val MIN_FILE_SIZE = 30 * 1024  // 缩略图最小 30KB
    }
    
    data class SkippedPhoto(
        val fileName: String,
        val reason: String
    )
    
    data class GenerateResult(
        val thumbnails: List<File>,      // 成功生成的缩略图文件
        val skipped: List<SkippedPhoto>  // 跳过的废片
    )
    
    /**
     * 批量生成缩略图
     * 
     * @param photos 相册照片列表
     * @param outputDir 输出目录（缩略图存放位置）
     * @param onProgress 进度回调 (current, total)
     */
    fun generateBatch(
        photos: List<PhotoInfo>,
        outputDir: File,
        onProgress: (Int, Int) -> Unit
    ): GenerateResult {
        if (!outputDir.exists()) outputDir.mkdirs()
        
        val thumbnails = mutableListOf<File>()
        val skipped = mutableListOf<SkippedPhoto>()
        
        photos.forEachIndexed { index, photo ->
            try {
                val outputFile = File(outputDir, photo.fileName)
                val skipReason = generateThumbnail(photo.uri, outputFile)
                
                if (skipReason == null) {
                    thumbnails.add(outputFile)
                } else {
                    skipped.add(SkippedPhoto(photo.fileName, skipReason))
                }
            } catch (e: Exception) {
                Log.e(TAG, "生成缩略图失败: ${photo.fileName}", e)
                skipped.add(SkippedPhoto(photo.fileName, "生成失败: ${e.message}"))
            }
            
            onProgress(index + 1, photos.size)
        }
        
        Log.i(TAG, "缩略图生成完成: 成功 ${thumbnails.size}, 跳过 ${skipped.size}")
        return GenerateResult(thumbnails, skipped)
    }
    
    /**
     * 生成单张缩略图
     * 
     * @return null 表示成功，非 null 为跳过原因
     */
    private fun generateThumbnail(srcUri: Uri, outputFile: File): String? {
        // 1. 读取原图尺寸
        val options = BitmapFactory.Options().apply {
            inJustDecodeBounds = true
        }
        context.contentResolver.openInputStream(srcUri)?.use {
            BitmapFactory.decodeStream(it, null, options)
        }
        
        val srcWidth = options.outWidth
        val srcHeight = options.outHeight
        
        if (srcWidth <= 0 || srcHeight <= 0) {
            return "无法读取图片尺寸"
        }
        
        // 2. 预筛：分辨率
        if (srcWidth < MIN_RESOLUTION || srcHeight < MIN_RESOLUTION) {
            return "分辨率过小 (${srcWidth}x${srcHeight})"
        }
        
        // 3. 预筛：长宽比
        val aspectRatio = maxOf(srcWidth, srcHeight).toFloat() / minOf(srcWidth, srcHeight)
        if (aspectRatio > MAX_ASPECT_RATIO) {
            return "长宽比异常 (${"%.1f".format(aspectRatio)})"
        }
        
        // 4. 计算 inSampleSize（避免 OOM）
        options.inSampleSize = calculateInSampleSize(srcWidth, srcHeight, MAX_SIZE)
        options.inJustDecodeBounds = false
        
        val bitmap = context.contentResolver.openInputStream(srcUri)?.use {
            BitmapFactory.decodeStream(it, null, options)
        } ?: return "解码失败"
        
        // 5. 精确缩放到 MAX_SIZE（inSampleSize 只能 2 的幂次，需二次缩放）
        val scaledBitmap = scaleBitmap(bitmap, MAX_SIZE)
        if (scaledBitmap != bitmap) {
            bitmap.recycle()
        }
        
        // 6. 压缩保存（确保 ≥30KB）
        FileOutputStream(outputFile).use { fos ->
            var quality = 85
            scaledBitmap.compress(Bitmap.CompressFormat.JPEG, quality, fos)
        }
        // 若太小，提高质量重压
        if (outputFile.length() < MIN_FILE_SIZE) {
            FileOutputStream(outputFile).use { fos ->
                scaledBitmap.compress(Bitmap.CompressFormat.JPEG, 95, fos)
            }
        }
        scaledBitmap.recycle()
        
        // 7. 复制 EXIF
        copyExif(srcUri, outputFile)
        
        return null
    }
    
    /**
     * 计算 inSampleSize（2 的幂次）
     */
    private fun calculateInSampleSize(width: Int, height: Int, reqSize: Int): Int {
        var inSampleSize = 1
        val maxDim = maxOf(width, height)
        // 采样到略大于 reqSize（保留精度余量）
        while (maxDim / inSampleSize > reqSize * 2) {
            inSampleSize *= 2
        }
        return inSampleSize
    }
    
    /**
     * 精确缩放 Bitmap 到最大边 = maxSize（保留长宽比）
     */
    private fun scaleBitmap(bitmap: Bitmap, maxSize: Int): Bitmap {
        val w = bitmap.width
        val h = bitmap.height
        val maxDim = maxOf(w, h)
        
        if (maxDim <= maxSize) return bitmap  // 已经够小
        
        val scale = maxSize.toFloat() / maxDim
        val newW = (w * scale).toInt()
        val newH = (h * scale).toInt()
        return Bitmap.createScaledBitmap(bitmap, newW, newH, true)
    }
    
    /**
     * 复制 EXIF 时间标签（服务端 01a get_photo_time 依赖），Orientation 重置为 1。
     *
     * 隐私加固：故意不复制 GPS 标签（TAG_GPS_*）。远程分析服务上传到公网，若携带
     * GPS 会连带泄露用户家庭/工作/旅行地点。服务端 01b 地理解析步骤在无 GPS 时
     * 会自然跳过，02 打分不依赖 GPS，功能不受影响。
     *
     * APP 亦未申请 ACCESS_MEDIA_LOCATION 权限，即便这里想读也拿不到 GPS 原值。
     */
    private fun copyExif(srcUri: Uri, dstFile: File) {
        try {
            val srcExif = context.contentResolver.openInputStream(srcUri)?.use {
                ExifInterface(it)
            } ?: return

            val dstExif = ExifInterface(dstFile.absolutePath)

            // 只复制时间标签（服务端时间分桶依赖）
            listOf(
                ExifInterface.TAG_DATETIME_ORIGINAL,
                ExifInterface.TAG_DATETIME,
                ExifInterface.TAG_DATETIME_DIGITIZED
            ).forEach { tag ->
                srcExif.getAttribute(tag)?.let { dstExif.setAttribute(tag, it) }
            }

            // Orientation 重置为 1：缩略图像素已是正向（Bitmap 解码不带旋转），
            // 清除旋转标记避免服务端 exif_transpose 二次旋转
            dstExif.setAttribute(ExifInterface.TAG_ORIENTATION, "1")

            dstExif.saveAttributes()
        } catch (e: Exception) {
            Log.w(TAG, "EXIF 复制失败: ${e.message}")
        }
    }
}
