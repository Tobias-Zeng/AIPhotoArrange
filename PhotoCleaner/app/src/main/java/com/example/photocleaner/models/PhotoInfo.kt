package com.example.photocleaner.models

import android.net.Uri

/**
 * 手机相册照片信息（从 MediaStore 查询）
 *
 * @param id MediaStore 内部 ID
 * @param uri Content URI（用于删除操作）
 * @param fileName 文件名（从 DISPLAY_NAME 字段获取，Android 11+ Scoped Storage 稳定方案）
 * @param dateTaken 拍摄时间（毫秒时间戳）
 * @param size 文件大小（字节）
 * @param thumbnailUri 缩略图 URI（可选，用于预览界面网格展示）
 * @param hasTimeWarning DATETIME 模式下时间戳偏差 > 2 秒时为 true，预览界面高亮提示人工确认
 */
data class PhotoInfo(
    val id: Long,
    val uri: Uri,
    val fileName: String,
    val dateTaken: Long,
    val size: Long,
    val thumbnailUri: Uri? = null,
    val hasTimeWarning: Boolean = false
)
