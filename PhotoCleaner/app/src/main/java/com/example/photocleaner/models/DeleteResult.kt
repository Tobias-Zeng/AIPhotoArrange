package com.example.photocleaner.models

/**
 * 删除操作结果统计
 *
 * @param successCount 成功删除的照片数量
 * @param failCount 删除失败的照片数量
 * @param failedPhotos 失败照片的文件名列表（用于详细错误提示）
 */
data class DeleteResult(
    val successCount: Int,
    val failCount: Int,
    val failedPhotos: List<String>
)
