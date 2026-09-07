package com.example.photocleaner.models

/**
 * 待删除照片信息（从 PC 端生成的删除列表文件解析）
 *
 * 支持两种匹配模式：
 * - SIZE 模式（旧）：用 size 字段做「文件名 + 大小」双重匹配
 * - DATETIME 模式（新）：用 dateTaken 字段做「文件名 + 拍摄时间」匹配（±12 小时容差），
 *   跨年同名文件时间必然不同，比大小更可靠
 *
 * @param name 文件名（如 IMG_0001.jpg）
 * @param size 文件大小（字节数），SIZE 模式用；取不到时为 -1，回退纯文件名匹配
 * @param dateTaken EXIF 拍摄时间（epoch 秒），DATETIME 模式用；取不到时为 -1
 */
data class PhotoToDelete(
    val name: String,
    val size: Long = -1,
    val dateTaken: Long = -1
)
