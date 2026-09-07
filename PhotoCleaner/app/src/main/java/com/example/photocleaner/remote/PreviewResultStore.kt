package com.example.photocleaner.remote

import android.content.Context
import android.net.Uri
import android.util.Log
import com.example.photocleaner.models.PhotoInfo
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * 预览结果持久化存储
 *
 * 将服务端分析后匹配到的待删除照片列表序列化为 JSON 缓存文件，
 * 使得用户误触返回或进程被系统回收后，能够自动恢复到预览页面，
 * 避免重新提交并等待漫长的服务端分析。
 *
 * 缓存文件路径：context.cacheDir/preview_result_<taskId>.json
 */
object PreviewResultStore {

    private const val TAG = "PreviewResultStore"

    private fun resultFile(context: Context, taskId: String): File =
        File(context.cacheDir, "preview_result_$taskId.json")

    /**
     * 保存预览结果到缓存文件
     *
     * @param taskId 任务标识（远程为真实 task_id，本地为 local_<timestamp>）
     * @param scannedCount 分析总数（远程模式为上传分析数，本地模式为 0）
     * @param photos 匹配到的待删除照片列表
     */
    suspend fun savePreview(
        context: Context,
        taskId: String,
        scannedCount: Int,
        photos: List<PhotoInfo>
    ) = withContext(Dispatchers.IO) {
        try {
            val jsonArray = JSONArray()
            photos.forEach { photo ->
                val obj = JSONObject().apply {
                    put("id", photo.id)
                    put("uri", photo.uri.toString())
                    put("fileName", photo.fileName)
                    put("dateTaken", photo.dateTaken)
                    put("size", photo.size)
                    put("thumbnailUri", photo.thumbnailUri?.toString() ?: "")
                    put("hasTimeWarning", photo.hasTimeWarning)
                }
                jsonArray.put(obj)
            }

            val root = JSONObject().apply {
                put("scannedCount", scannedCount)
                put("photos", jsonArray)
            }

            resultFile(context, taskId).writeText(root.toString())
            Log.i(TAG, "已保存预览结果: taskId=$taskId, 照片数=${photos.size}")
        } catch (e: Exception) {
            Log.e(TAG, "保存预览结果失败: ${e.message}", e)
        }
    }

    /**
     * 从缓存文件加载预览结果
     *
     * @return Pair<scannedCount, photos>，文件不存在或解析失败返回 null
     */
    suspend fun loadPreview(
        context: Context,
        taskId: String
    ): Pair<Int, List<PhotoInfo>>? = withContext(Dispatchers.IO) {
        try {
            val file = resultFile(context, taskId)
            if (!file.exists()) return@withContext null

            val root = JSONObject(file.readText())
            val scannedCount = root.getInt("scannedCount")
            val jsonArray = root.getJSONArray("photos")
            val photos = mutableListOf<PhotoInfo>()

            for (i in 0 until jsonArray.length()) {
                val obj = jsonArray.getJSONObject(i)
                val thumbnailUriStr = obj.optString("thumbnailUri", "")
                photos.add(
                    PhotoInfo(
                        id = obj.getLong("id"),
                        uri = Uri.parse(obj.getString("uri")),
                        fileName = obj.getString("fileName"),
                        dateTaken = obj.getLong("dateTaken"),
                        size = obj.getLong("size"),
                        thumbnailUri = if (thumbnailUriStr.isEmpty()) null else Uri.parse(thumbnailUriStr),
                        hasTimeWarning = obj.getBoolean("hasTimeWarning")
                    )
                )
            }

            Log.i(TAG, "已恢复预览结果: taskId=$taskId, 照片数=${photos.size}")
            scannedCount to photos
        } catch (e: Exception) {
            Log.e(TAG, "加载预览结果失败: ${e.message}", e)
            null
        }
    }
}
