package com.example.photocleaner.remote

import android.util.Log
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.security.MessageDigest
import java.util.concurrent.TimeUnit

/**
 * Homelab API 客户端
 * 
 * 封装所有 HTTP 调用，基于 OkHttp。
 * 包含认证、健康检查、分块上传、状态查询等。
 */
class HomelabClient(
    private val baseUrl: String,
    private val authToken: String
) {

    init {
        // 纵深防御：发请求前再校验一次地址策略，防止公网 http:// 明文泄露 token。
        // 非法地址直接拒绝构造（validate 已在设置保存时拦过一次，此处兜底运行时）。
        UrlPolicy.enforce(baseUrl)
    }

    companion object {
        private const val TAG = "HomelabClient"
    }
    
    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(20, TimeUnit.SECONDS)
        .writeTimeout(120, TimeUnit.SECONDS)  // 上传分块需要更长超时
        .addInterceptor { chain ->
            val original = chain.request()
            // 健康检查豁免认证
            if (original.url.encodedPath == "/api/health") {
                return@addInterceptor chain.proceed(original)
            }
            // 其他请求加 Authorization 头
            val request = original.newBuilder()
                .addHeader("Authorization", "Bearer $authToken")
                .build()
            chain.proceed(request)
        }
        .build()
    
    // ==========================================
    // 数据类
    // ==========================================
    
    data class HealthResult(
        val ok: Boolean,
        val rttMs: Int?,
        val warning: String?,
        val error: String?,
        val provider: String? = null,
        val model: String? = null
    )

    /**
     * Token 验证结果
     *
     * @param valid       token 是否有效
     * @param unreachable 是否为服务端无法访问（网络错误），用于区分“连接失败”与“token 无效”
     * @param error       错误信息（valid=false 时有值）
     */
    data class AuthResult(
        val valid: Boolean,
        val unreachable: Boolean,
        val error: String?
    )
    
    data class UploadChunkResult(
        val taskId: String,
        val accepted: Boolean,
        val receivedChunks: List<Int>
    )
    
    data class UploadStatus(
        val taskId: String,
        val receivedChunks: List<Int>,
        val totalChunks: Int
    )
    
    data class FinalizeResult(
        val taskId: String,
        val status: String,
        val photoCount: Int,
        val estimatedTimeMin: Int
    )
    
    data class TaskStatus(
        val taskId: String,
        val status: String,
        val stage: String?,
        val stageName: String?,
        val progress: String?,
        val progressPercent: Int,
        val etaSec: Int,
        // 预估剩余时间文本（如 "25分钟" / "1小时5分钟"）；样本不足时为 null。
        val etaText: String? = null,
        // 预计完成时刻（服务器本地时间，格式 "MM-DD HH:MM:SS"）；样本不足时为 null。
        val finishTime: String? = null,
        // 本次处理已用时间（秒）。
        val elapsedSec: Int = 0,
        val deleteCount: Int,
        val keepCount: Int,
        val error: String?,
        // 排队位置（status == "queued" 时有值）：1 表示下一个即将执行；
        // 前面还有 (queuePosition - 1) 个任务在排队。
        val queuePosition: Int? = null
    )

    /**
     * 结构化 API 错误。服务端统一返回 {error_code, message}。
     * @param httpCode  HTTP 状态码
     * @param errorCode 业务错误码（如 server_busy / too_many_photos / invalid_token）
     * @param message   面向用户的中文提示
     */
    data class ApiError(
        val httpCode: Int,
        val errorCode: String?,
        val message: String?
    )

    /** 携带结构化错误的异常，供上层按 errorCode 分流提示。 */
    class ApiException(val apiError: ApiError) :
        IOException(apiError.message ?: "HTTP ${apiError.httpCode}")
    
    // ==========================================
    // API 方法
    // ==========================================
    
    /**
     * 轻量健康检查（免认证）：调用 /api/health，仅确认服务存活 + 测量 RTT。
     *
     * 服务端 /api/health 出于公网安全已剥离内网拓扑（provider/model/内网URL），
     * 只返回 {status, timestamp}。模型状态改用 healthCheckDetailed()（需认证）。
     */
    fun healthCheck(): HealthResult {
        val startTime = System.currentTimeMillis()
        val request = Request.Builder()
            .url("$baseUrl/api/health")
            .get()
            .build()

        return try {
            client.newCall(request).execute().use { response ->
                val rtt = (System.currentTimeMillis() - startTime).toInt()
                if (!response.isSuccessful) {
                    return HealthResult(false, rtt, null, "HTTP ${response.code}")
                }
                val json = JSONObject(response.body?.string() ?: "{}")
                val ok = json.optString("status") == "ok"
                HealthResult(
                    ok = ok,
                    rttMs = rtt,
                    warning = if (rtt > 500) "网络延迟较高（${rtt}ms），上传可能较慢" else null,
                    error = if (ok) null else "服务未就绪"
                )
            }
        } catch (e: IOException) {
            Log.e(TAG, "健康检查失败", e)
            HealthResult(false, null, null, "无法连接: ${e.message}")
        }
    }

    /**
     * 详细健康检查（需认证）：调用 /api/health/detailed，返回模型服务状态。
     *
     * 该端点受认证中间件保护，OkHttp 拦截器会自动注入 Authorization 头。
     * 因此：2xx = token 有效且模型状态已返回；401 = token 无效。
     * 用于设置页"检查服务状态"：一次请求同时完成模型探活 + token 验证。
     */
    fun healthCheckDetailed(): HealthResult {
        val startTime = System.currentTimeMillis()
        val request = Request.Builder()
            .url("$baseUrl/api/health/detailed")
            .get()
            .build()

        return try {
            client.newCall(request).execute().use { response ->
                val rtt = (System.currentTimeMillis() - startTime).toInt()

                if (response.code == 401) {
                    return HealthResult(false, rtt, null, "Token 无效")
                }
                if (!response.isSuccessful) {
                    return HealthResult(false, rtt, null, "HTTP ${response.code}")
                }

                val json = JSONObject(response.body?.string() ?: "{}")
                val status = json.optString("status")
                val provider = json.optString("provider").takeIf { it.isNotEmpty() }
                val model = json.optString("model").takeIf { it.isNotEmpty() }
                val modelService = json.optJSONObject("model_service")
                val serviceOk = modelService?.optBoolean("reachable") ?: false
                // 使用客户端实测的往返延迟（服务端 rtt_ms 为固定占位值，不反映真实网络延迟）
                val networkWarning = if (rtt > 500) {
                    "网络延迟较高（${rtt}ms），上传可能较慢"
                } else null
                
                HealthResult(
                    ok = status == "ok" && serviceOk,
                    rttMs = rtt,
                    warning = networkWarning,
                    error = if (!serviceOk) {
                        // 模型服务不可达/未就绪时，从 model_service.error 提取错误
                        val rawError = modelService?.optString("error")?.takeIf { it.isNotEmpty() }
                        rawError?.let {
                            // 将技术错误信息转换为友好提示
                            when {
                                rawError.contains("Connection refused", ignoreCase = true) ||
                                rawError.contains("Connection reset", ignoreCase = true) ||
                                rawError.contains("Failed to establish", ignoreCase = true) -> 
                                    "模型服务连接失败"
                                rawError.contains("timeout", ignoreCase = true) ||
                                rawError.contains("timed out", ignoreCase = true) ->
                                    "模型服务响应超时"
                                else -> "模型服务不可用: $rawError"
                            }
                        } ?: "模型服务连接失败"
                    } else if (status != "ok") {
                        // API 可达、模型可达，但模型未加载/配置无效等
                        modelService?.optString("error")?.takeIf { it.isNotEmpty() }
                            ?.let { "模型服务不可用: $it" }
                            ?: "模型服务未就绪"
                    } else null,
                    provider = provider,
                    model = model
                )
            }
        } catch (e: IOException) {
            Log.w(TAG, "详细健康检查失败: ${e.message}")
            HealthResult(false, null, null, "无法连接: ${e.message}")
        }
    }

    /**
     * 验证认证 Token 是否有效
     *
     * 调用受认证中间件保护的 /api/verify_token：
     *   - 200: token 有效
     *   - 401: token 无效
     *   - 网络异常: 服务端无法访问（unreachable=true）
     */
    fun verifyAuth(): AuthResult {
        val request = Request.Builder()
            .url("$baseUrl/api/verify_token")
            .post("".toRequestBody(null))
            .build()

        return try {
            client.newCall(request).execute().use { response ->
                when (response.code) {
                    200 -> AuthResult(valid = true, unreachable = false, error = null)
                    401 -> AuthResult(valid = false, unreachable = false, error = "Token 无效")
                    else -> AuthResult(
                        valid = false,
                        unreachable = false,
                        error = "验证失败: HTTP ${response.code}"
                    )
                }
            }
        } catch (e: IOException) {
            Log.e(TAG, "验证 Token 失败", e)
            AuthResult(valid = false, unreachable = true, error = "无法连接: ${e.message}")
        }
    }
    
    /**
     * 上传分块（带 SHA256 校验）
     */
    fun uploadChunk(
        taskId: String?,
        chunkIndex: Int,
        totalChunks: Int,
        chunkData: ByteArray
    ): UploadChunkResult {
        // 计算 SHA256
        val sha256 = MessageDigest.getInstance("SHA-256")
            .digest(chunkData)
            .joinToString("") { "%02x".format(it) }
        
        // 构建 multipart 请求
        val requestBody = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart("chunk_index", chunkIndex.toString())
            .addFormDataPart("total_chunks", totalChunks.toString())
            .addFormDataPart("sha256", sha256)
            .apply {
                if (taskId != null) {
                    addFormDataPart("task_id", taskId)
                }
            }
            .addFormDataPart(
                "file",
                "chunk_$chunkIndex.zip",
                chunkData.toRequestBody("application/zip".toMediaType())
            )
            .build()
        
        val request = Request.Builder()
            .url("$baseUrl/api/upload_chunk")
            .post(requestBody)
            .build()
        
        return try {
            client.newCall(request).execute().use { response ->
                val bodyStr = response.body?.string()
                if (!response.isSuccessful) {
                    throw parseError(response.code, bodyStr)
                }
                
                val json = JSONObject(bodyStr ?: "{}")
                UploadChunkResult(
                    taskId = json.getString("task_id"),
                    accepted = json.getBoolean("accepted"),
                    receivedChunks = json.getJSONArray("received_chunks").toIntList()
                )
            }
        } catch (e: Exception) {
            // 只记异常类型和 message，避免异常 body 泄露 task_id / 服务响应细节
            Log.w(TAG, "上传分块失败: chunk=$chunkIndex reason=${e.javaClass.simpleName}")
            throw e
        }
    }
    
    /**
     * 查询上传状态（断点续传）
     */
    fun getUploadStatus(taskId: String): UploadStatus {
        val request = Request.Builder()
            .url("$baseUrl/api/upload_status?task_id=$taskId")
            .get()
            .build()
        
        return try {
            client.newCall(request).execute().use { response ->
                val bodyStr = response.body?.string()
                if (!response.isSuccessful) {
                    throw parseError(response.code, bodyStr)
                }
                
                val json = JSONObject(bodyStr ?: "{}")
                UploadStatus(
                    taskId = json.getString("task_id"),
                    receivedChunks = json.getJSONArray("received_chunks").toIntList(),
                    totalChunks = json.getInt("total_chunks")
                )
            }
        } catch (e: Exception) {
            Log.e(TAG, "查询上传状态失败", e)
            throw e
        }
    }
    
    /**
     * 完成上传，启动处理
     * 
     * @param taskId 任务ID
     * @param extractionLevel 提取档位（A 精华档 / B 纪念档），null 则用服务端默认值
     */
    fun finalize(taskId: String, extractionLevel: String? = null): FinalizeResult {
        val json = JSONObject().apply {
            put("task_id", taskId)
            extractionLevel?.let { put("extraction_level", it) }
        }
        
        val requestBody = json.toString()
            .toRequestBody("application/json".toMediaType())
        
        val request = Request.Builder()
            .url("$baseUrl/api/finalize")
            .post(requestBody)
            .build()
        
        return try {
            client.newCall(request).execute().use { response ->
                val bodyStr = response.body?.string()
                if (!response.isSuccessful) {
                    throw parseError(response.code, bodyStr)
                }
                
                val responseJson = JSONObject(bodyStr ?: "{}")
                FinalizeResult(
                    taskId = responseJson.getString("task_id"),
                    status = responseJson.getString("status"),
                    photoCount = responseJson.optInt("photo_count", 0),
                    estimatedTimeMin = responseJson.optInt("estimated_time_min", 0)
                )
            }
        } catch (e: Exception) {
            Log.e(TAG, "finalize 失败", e)
            throw e
        }
    }
    
    /**
     * 查询任务状态
     */
    fun getTaskStatus(taskId: String): TaskStatus {
        val request = Request.Builder()
            .url("$baseUrl/api/status/$taskId")
            .get()
            .build()
        
        return try {
            client.newCall(request).execute().use { response ->
                val bodyStr = response.body?.string()
                if (!response.isSuccessful) {
                    throw parseError(response.code, bodyStr)
                }
                
                val json = JSONObject(bodyStr ?: "{}")
                TaskStatus(
                    taskId = json.getString("task_id"),
                    status = json.getString("status"),
                    stage = json.optString("stage").takeIf { it.isNotEmpty() },
                    stageName = json.optString("stage_name").takeIf { it.isNotEmpty() },
                    progress = json.optString("progress").takeIf { it.isNotEmpty() },
                    progressPercent = json.optInt("progress_percent", 0),
                    etaSec = json.optInt("eta_sec", 0),
                    etaText = json.optString("eta_text").takeIf { it.isNotEmpty() },
                    finishTime = json.optString("finish_time").takeIf { it.isNotEmpty() },
                    elapsedSec = json.optInt("elapsed_sec", 0),
                    deleteCount = json.optInt("delete_count", 0),
                    keepCount = json.optInt("keep_count", 0),
                    error = json.optString("error").takeIf { it.isNotEmpty() },
                    queuePosition = if (json.has("queue_position") && !json.isNull("queue_position"))
                        json.optInt("queue_position") else null
                )
            }
        } catch (e: Exception) {
            Log.e(TAG, "查询状态失败", e)
            throw e
        }
    }
    
    /**
     * 取消任务（用户主动放弃）
     *
     * 服务端会标记任务为 cancelled 并杀死流水线子进程树。
     * 404（任务不存在）视为已取消，不抛异常。
     */
    fun cancelTask(taskId: String) {
        val request = Request.Builder()
            .url("$baseUrl/api/cancel/$taskId")
            .post("".toRequestBody(null))
            .build()

        try {
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful && response.code != 404) {
                    throw parseError(response.code, response.body?.string())
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "取消任务失败: reason=${e.javaClass.simpleName}")
            throw e
        }
    }

    /**
     * 下载删除列表到文件
     */
    fun downloadResult(taskId: String, outputFile: File) {
        val request = Request.Builder()
            .url("$baseUrl/api/result/$taskId")
            .get()
            .build()
        
        try {
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    throw parseError(response.code, response.body?.string())
                }
                response.body?.byteStream()?.use { input ->
                    outputFile.outputStream().use { output ->
                        input.copyTo(output)
                    }
                } ?: throw IOException("下载失败：响应体为空")
            }
        } catch (e: Exception) {
            Log.e(TAG, "下载结果失败", e)
            throw e
        }
    }
    
    // ==========================================
    // 工具方法
    // ==========================================
    
    private fun JSONArray.toIntList(): List<Int> {
        return (0 until length()).map { getInt(it) }
    }

    /**
     * 解析失败响应体为 ApiException。
     *
     * 服务端约定错误响应为 {"error_code": "...", "message": "..."}，
     * 但 FastAPI 的 HTTPException 会包装成 {"detail": {...}} 或 {"detail": "..."}。
     * 这里两种结构都尝试解析，取不到则回退到 HTTP 码。
     * 注意：response.body 只能读一次，调用方读取后不应再读。
     */
    private fun parseError(code: Int, bodyStr: String?): ApiException {
        var errorCode: String? = null
        var message: String? = null
        if (!bodyStr.isNullOrEmpty()) {
            try {
                val json = JSONObject(bodyStr)
                // 优先直接字段
                val root = when {
                    json.opt("detail") is JSONObject -> json.getJSONObject("detail")
                    else -> json
                }
                errorCode = root.optString("error_code").takeIf { it.isNotEmpty() }
                message = root.optString("message").takeIf { it.isNotEmpty() }
                // detail 为纯字符串的情况
                if (message == null && json.opt("detail") is String) {
                    message = json.optString("detail").takeIf { it.isNotEmpty() }
                }
            } catch (_: Exception) {
                // body 非 JSON，忽略，走回退
            }
        }
        return ApiException(ApiError(code, errorCode, message ?: "请求失败（HTTP $code）"))
    }
}
