package com.example.photocleaner

import android.app.Activity
import android.content.Context
import android.os.Build
import android.provider.MediaStore
import android.util.Log
import androidx.activity.result.IntentSenderRequest
import com.example.photocleaner.models.DeleteResult
import com.example.photocleaner.models.PhotoInfo

/**
 * 照片删除模块
 * 
 * 职责：
 * 1. 批量删除照片（Android 11+ createDeleteRequest）
 * 2. 处理用户授权（系统弹窗）
 * 3. 统计删除结果（成功/失败）
 * 
 * 删除方式说明（重要）：
 * 早期版本用 createTrashRequest（移到 MediaStore 回收站），但在 MIUI（小米）等
 * 定制系统上，系统相册使用自己的媒体数据库，不认 AOSP MediaStore 的 IS_TRASHED
 * 状态，导致：照片原图已被移走（缩略图变糊），但相册界面仍显示，且不进小米「最近删除」。
 * 叠加小米云同步会把状态反复同步回来，表现为「删了又像没删」。
 * 
 * 因此改用 createDeleteRequest（直接删除）：在 MIUI 上会走系统相册自己的
 * 「最近删除」（同样 30 天可恢复），能真正做到相册中消失 + 可恢复。
 */
class PhotoDeleter(private val context: Context) {
    
    companion object {
        private const val TAG = "PhotoDeleter"
    }
    
    /**
     * 批量删除照片
     * 
     * @param photos 待删除照片列表
     * @return IntentSenderRequest（需要在 Activity 中启动授权弹窗），如果系统不支持则返回 null
     * 
     * 流程说明：
     * 1. 调用此方法构造删除请求（不会立即删除）
     * 2. 在 Activity 中通过 ActivityResultLauncher 启动系统授权弹窗
     * 3. 用户确认后，系统自动执行删除（APP 无需手动操作）
     * 4. 通过 resultCode 判断是否成功
     * 
     * 使用 createDeleteRequest：
     * - 系统弹出确认框，用户确认后删除
     * - 在原生 Android 上进入系统回收站；在 MIUI 等定制系统上进入系统相册的
     *   「最近删除」，均支持 30 天内恢复
     */
    fun createDeleteRequest(photos: List<PhotoInfo>): IntentSenderRequest? {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) {
            Log.e(TAG, "系统版本过低（< Android 11），不支持批量删除")
            return null
        }
        
        if (photos.isEmpty()) {
            Log.w(TAG, "待删除照片列表为空，跳过删除请求")
            return null
        }
        
        return try {
            val uris = photos.map { it.uri }
            Log.i(TAG, "构造删除请求：${uris.size} 张照片")
            
            // createDeleteRequest 返回 PendingIntent
            // 相比 createTrashRequest，直接删除在定制系统（MIUI/ColorOS 等）上
            // 兼容性更好：照片会进入系统相册自己的「最近删除」，界面立即移除且可恢复
            val deleteRequest = MediaStore.createDeleteRequest(
                context.contentResolver,
                uris
            )
            
            IntentSenderRequest.Builder(deleteRequest.intentSender).build()
        } catch (e: Exception) {
            Log.e(TAG, "构造删除请求失败", e)
            null
        }
    }
    
    /**
     * 处理删除结果
     * 
     * @param resultCode Activity.RESULT_OK 表示用户授权成功，照片已被系统删除
     * @param photoCount 待删除照片数量
     * @return 删除结果统计
     * 
     * 注意：
     * - RESULT_OK：用户在系统弹窗点击"允许"/"移到回收站"，删除成功
     * - RESULT_CANCELED：用户点击"拒绝"/"取消"，删除未执行
     * - 系统弹窗由 Android 系统提供，APP 无法自定义样式和文案
     */
    fun handleDeleteResult(resultCode: Int, photoCount: Int): DeleteResult {
        return if (resultCode == Activity.RESULT_OK) {
            Log.i(TAG, "删除成功：$photoCount 张照片已删除")
            DeleteResult(
                successCount = photoCount,
                failCount = 0,
                failedPhotos = emptyList()
            )
        } else {
            Log.w(TAG, "删除取消：用户拒绝授权，$photoCount 张照片未删除")
            DeleteResult(
                successCount = 0,
                failCount = photoCount,
                failedPhotos = emptyList()
            )
        }
    }
    
    /**
     * 检查当前系统是否支持批量删除到回收站
     * 
     * @return true: 支持（Android 11+）, false: 不支持
     */
    fun isDeleteSupported(): Boolean {
        return Build.VERSION.SDK_INT >= Build.VERSION_CODES.R
    }
}
