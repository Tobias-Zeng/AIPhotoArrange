package com.example.photocleaner.remote

import android.content.Context
import android.content.SharedPreferences
import android.util.Log
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import java.io.File

/**
 * 设置持久化。
 *
 * 双存储设计（安全 + 性能折中）：
 * - securePrefs（EncryptedSharedPreferences，AES256-GCM，密钥在 Android Keystore）：
 *     存敏感字段 auth_token / homelab_address / pending_task_id
 * - prefs（普通 SharedPreferences）：存非敏感字段（开关、档位、计数等）
 *
 * 首次实例化时静默迁移：把旧明文 prefs 里的敏感字段搬到 securePrefs 后删除，用户无感。
 */
class SettingsRepository(private val context: Context) {

    private val prefs: SharedPreferences = context.getSharedPreferences(
        "photo_cleaner_settings",
        Context.MODE_PRIVATE
    )

    // 加密存储：初始化失败（极少数机型 Keystore 异常）时降级到普通 prefs，保证可用性。
    private val securePrefs: SharedPreferences = try {
        val masterKey = MasterKey.Builder(context)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        EncryptedSharedPreferences.create(
            context,
            "photo_cleaner_secure",
            masterKey,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM
        )
    } catch (e: Exception) {
        Log.w("SettingsRepository", "加密存储初始化失败，降级普通存储: ${e.message}")
        prefs
    }

    init {
        migrateSensitiveFieldsIfNeeded()
    }

    /**
     * 一次性静默迁移：旧版本把 auth_token/homelab_address/pending_task_id 存在明文 prefs，
     * 这里搬到 securePrefs 后从明文 prefs 删除。已迁移过则跳过。
     */
    private fun migrateSensitiveFieldsIfNeeded() {
        if (securePrefs === prefs) return  // 未启用加密存储，无需迁移
        if (prefs.getBoolean(KEY_MIGRATED_SECURE, false)) return

        val sensitiveKeys = listOf(KEY_HOMELAB_ADDRESS, KEY_AUTH_TOKEN, KEY_PENDING_TASK_ID)
        val editSecure = securePrefs.edit()
        val editPlain = prefs.edit()
        var moved = 0
        for (k in sensitiveKeys) {
            val v = prefs.getString(k, null)
            if (v != null) {
                editSecure.putString(k, v)
                editPlain.remove(k)
                moved++
            }
        }
        editSecure.apply()
        editPlain.putBoolean(KEY_MIGRATED_SECURE, true).apply()
        if (moved > 0) Log.i("SettingsRepository", "已迁移 $moved 个敏感字段到加密存储")
    }
    
    companion object {
        private const val KEY_USE_REMOTE_ANALYSIS = "use_remote_analysis"
        private const val KEY_HOMELAB_ADDRESS = "homelab_address"
        private const val KEY_AUTH_TOKEN = "auth_token"
        private const val KEY_CHUNK_SIZE = "chunk_size"
        private const val KEY_PENDING_TASK_ID = "pending_task_id"
        private const val KEY_PENDING_PHOTO_COUNT = "pending_photo_count"
        private const val KEY_EXTRACTION_LEVEL = "extraction_level"
        private const val KEY_PREVIEW_RESULT_TASK_ID = "preview_result_task_id"
        private const val KEY_PREVIEW_SCANNED_COUNT = "preview_scanned_count"
        private const val KEY_PREVIEW_DELETE_COUNT = "preview_delete_count"
        private const val KEY_MIGRATED_SECURE = "migrated_secure_v1"
        
        // 默认值
        // 地址/Token 默认清空：避免随 APK 分发泄露真实凭据。首次使用需用户在设置中填写。
        const val DEFAULT_HOMELAB_ADDRESS = ""
        const val DEFAULT_AUTH_TOKEN = ""
        // 分块大小固定为 50 张/块（内部常量，不再暴露给用户）。
        // 缩略图约 100KB/张，50 张 ≈ 5MB，远低于服务端单块 20MB 上限，安全且请求数合理。
        const val DEFAULT_CHUNK_SIZE = 50
        // 单次分析照片数上限，与服务端 storage.max_photos 对齐
        const val MAX_PHOTOS_PER_TASK = 2000
        const val DEFAULT_EXTRACTION_LEVEL = "A"
    }
    
    /**
     * 远程分析开关（默认关闭）
     */
    var useRemoteAnalysis: Boolean
        get() = prefs.getBoolean(KEY_USE_REMOTE_ANALYSIS, false)
        set(value) = prefs.edit().putBoolean(KEY_USE_REMOTE_ANALYSIS, value).apply()
    
    /**
     * Homelab 地址（如 "https://your-domain.example.com" 或 "http://192.168.1.4:36600"）
     * 存于加密存储：地址中可能含家庭域名，属于隐私。
     */
    var homelabAddress: String
        get() = securePrefs.getString(KEY_HOMELAB_ADDRESS, DEFAULT_HOMELAB_ADDRESS)
            ?: DEFAULT_HOMELAB_ADDRESS
        set(value) = securePrefs.edit().putString(KEY_HOMELAB_ADDRESS, value).apply()

    /**
     * 认证 Token。存于加密存储：泄露即可完整访问服务端。
     */
    var authToken: String
        get() = securePrefs.getString(KEY_AUTH_TOKEN, DEFAULT_AUTH_TOKEN) ?: DEFAULT_AUTH_TOKEN
        set(value) = securePrefs.edit().putString(KEY_AUTH_TOKEN, value).apply()
    
    /**
     * 上传分块大小（张/块）
     */
    var chunkSize: Int
        get() = prefs.getInt(KEY_CHUNK_SIZE, DEFAULT_CHUNK_SIZE)
        set(value) = prefs.edit().putInt(KEY_CHUNK_SIZE, value).apply()
    
    /**
     * 提取档位（A 精华档 / B 纪念档），同步给后端 API 按此档位处理
     */
    var extractionLevel: String
        get() = prefs.getString(KEY_EXTRACTION_LEVEL, DEFAULT_EXTRACTION_LEVEL)
            ?: DEFAULT_EXTRACTION_LEVEL
        set(value) = prefs.edit().putString(KEY_EXTRACTION_LEVEL, value).apply()
    
    /**
     * pending task_id（上次提交但未取结果的任务）
     */
    var pendingTaskId: String?
        get() = securePrefs.getString(KEY_PENDING_TASK_ID, null)
        set(value) = securePrefs.edit().putString(KEY_PENDING_TASK_ID, value).apply()
    
    /**
     * 本次分析上传的照片数量（随 pending task 一起持久化，
     * 供进程重启后结果页显示"本次共分析 X 张照片"）
     */
    var pendingPhotoCount: Int
        get() = prefs.getInt(KEY_PENDING_PHOTO_COUNT, 0)
        set(value) = prefs.edit().putInt(KEY_PENDING_PHOTO_COUNT, value).apply()
    
    /**
     * 清除 pending task
     */
    fun clearPendingTask() {
        securePrefs.edit().remove(KEY_PENDING_TASK_ID).apply()
        prefs.edit().remove(KEY_PENDING_PHOTO_COUNT).apply()
    }
    
    /**
     * 已完成的预览结果 task_id（用于进程恢复）
     */
    var previewResultTaskId: String?
        get() = prefs.getString(KEY_PREVIEW_RESULT_TASK_ID, null)
        set(value) = prefs.edit().putString(KEY_PREVIEW_RESULT_TASK_ID, value).apply()
    
    /**
     * 预览结果对应的分析照片数量（用于恢复时显示"本次共分析 X 张照片"）
     */
    var previewScannedCount: Int
        get() = prefs.getInt(KEY_PREVIEW_SCANNED_COUNT, 0)
        set(value) = prefs.edit().putInt(KEY_PREVIEW_SCANNED_COUNT, value).apply()
    
    /**
     * 预览结果匹配到的待删除照片数量（供初始页提示卡片快速展示，避免读 JSON）
     */
    var previewDeleteCount: Int
        get() = prefs.getInt(KEY_PREVIEW_DELETE_COUNT, 0)
        set(value) = prefs.edit().putInt(KEY_PREVIEW_DELETE_COUNT, value).apply()
    
    /**
     * 清除预览结果（删除 JSON 缓存文件 + SharedPreferences 标记）
     */
    fun clearPreviewResult() {
        val taskId = previewResultTaskId
        if (taskId != null) {
            val file = File(context.cacheDir, "preview_result_$taskId.json")
            if (file.exists()) {
                file.delete()
            }
        }
        prefs.edit()
            .remove(KEY_PREVIEW_RESULT_TASK_ID)
            .remove(KEY_PREVIEW_SCANNED_COUNT)
            .remove(KEY_PREVIEW_DELETE_COUNT)
            .apply()
    }
}
