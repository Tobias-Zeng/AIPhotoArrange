package com.example.photocleaner

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.example.photocleaner.remote.HomelabClient
import com.example.photocleaner.remote.SettingsRepository
import com.example.photocleaner.remote.UrlPolicy
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 设置弹窗
 */
@Composable
fun SettingsDialog(
    settings: SettingsRepository,
    onDismiss: () -> Unit
) {
    var useRemote by remember { mutableStateOf(settings.useRemoteAnalysis) }
    var homelabAddr by remember { mutableStateOf(settings.homelabAddress) }
    var authToken by remember { mutableStateOf(settings.authToken) }
    var extractionLevel by remember { mutableStateOf(settings.extractionLevel) }

    // 服务状态检查
    var checking by remember { mutableStateOf(false) }
    var checkResult by remember { mutableStateOf<String?>(null) }
    var checkResultIsError by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("设置") },
        text = {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(vertical = 8.dp)
            ) {
                // 远程分析总开关
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .clickable { useRemote = !useRemote }
                        .padding(vertical = 8.dp),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Column(modifier = Modifier.weight(1f)) {
                        Text(
                            "远程分析",
                            style = MaterialTheme.typography.bodyLarge
                        )
                        Text(
                            "上传照片服务分析",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                    Switch(
                        checked = useRemote,
                        onCheckedChange = { useRemote = it }
                    )
                }
                
                if (useRemote) {
                    HorizontalDivider(modifier = Modifier.padding(vertical = 8.dp))
                    
                    // 服务地址
                    OutlinedTextField(
                        value = homelabAddr,
                        onValueChange = { homelabAddr = it },
                        label = { Text("服务地址") },
                        placeholder = { Text("https://your-domain.example.com") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true
                    )
                    
                    Spacer(Modifier.height(8.dp))
                    
                    // 认证 Token
                    OutlinedTextField(
                        value = authToken,
                        onValueChange = { authToken = it },
                        label = { Text("认证 Token") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true
                    )

                    Spacer(Modifier.height(8.dp))

                    // 分块大小已固定为 50 张/块（内部常量），不再暴露给用户调整：
                    // 过大易触发单块大小上限/上传超时/内存压力，过小则拖慢上传。

                    // 检查服务状态按钮
                    OutlinedButton(
                        onClick = {
                            checkResult = null
                            checkResultIsError = false
                            checking = true
                            scope.launch {
                                val result = withContext(Dispatchers.IO) {
                                    try {
                                        val client = HomelabClient(homelabAddr, authToken)
                                        
                                        // 连续采样 3 次 rtt，取中位数（避免首次冷启动虚高）。
                                        // /api/health 免认证，无 token 也能测网络延迟。
                                        val rttSamples = mutableListOf<Int>()
                                        repeat(3) { i ->
                                            val startTime = System.currentTimeMillis()
                                            client.healthCheck()
                                            val rtt = (System.currentTimeMillis() - startTime).toInt()
                                            rttSamples.add(rtt)
                                            if (i < 2) delay(200)  // 间隔 200ms
                                        }
                                        val medianRtt = rttSamples.sorted()[1]  // 中位数

                                        // 详细检查（需认证）：一次请求同时完成模型探活 + token 验证。
                                        //   401 -> token 无效；连接失败 -> 服务端不可达；其余错误 -> 模型未就绪
                                        val health = client.healthCheckDetailed()
                                        if (!health.ok) {
                                            checkResultIsError = true
                                            return@withContext health.error ?: "服务不可用"
                                        }

                                        // 通过，构建结果（合并延迟提示为单行）
                                        checkResultIsError = false
                                        if (medianRtt > 500) {
                                            // 延迟过高：单行警告（后续用橙色显示）
                                            return@withContext "服务可用，但网络延迟较高（${medianRtt}ms），上传可能较慢"
                                        } else {
                                            // 正常：绿色
                                            return@withContext "服务可用（延迟 ${medianRtt}ms）"
                                        }
                                        
                                    } catch (e: Exception) {
                                        checkResultIsError = true
                                        return@withContext e.message ?: "检查失败"
                                    }
                                }
                                checkResult = result
                                checking = false
                            }
                        },
                        enabled = !checking && homelabAddr.isNotBlank(),
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        if (checking) {
                            CircularProgressIndicator(
                                modifier = Modifier.size(16.dp),
                                strokeWidth = 2.dp
                            )
                            Spacer(Modifier.width(8.dp))
                            Text("检查中...")
                        } else {
                            Text("检查服务状态")
                        }
                    }

                    // 检查结果显示
                    checkResult?.let { result ->
                        Spacer(Modifier.height(4.dp))
                        
                        // 判断是否为"延迟过高"警告
                        val isWarning = result.contains("延迟较高")
                        val (icon, color) = when {
                            checkResultIsError -> "✗" to MaterialTheme.colorScheme.error
                            isWarning -> "⚠" to MaterialTheme.colorScheme.tertiary  // 橙色
                            else -> "✓" to MaterialTheme.colorScheme.primary        // 绿色
                        }
                        
                        Text(
                            "$icon $result",
                            style = MaterialTheme.typography.bodySmall,
                            color = color
                        )
                    }

                    Spacer(Modifier.height(8.dp))

                    // 提取档位
                    Column(modifier = Modifier.fillMaxWidth()) {
                        Text(
                            "提取档位",
                            style = MaterialTheme.typography.bodyMedium,
                            modifier = Modifier.padding(bottom = 4.dp)
                        )
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.spacedBy(8.dp)
                        ) {
                            FilterChip(
                                selected = extractionLevel == "A",
                                onClick = { extractionLevel = "A" },
                                label = { Text("A 精华档") },
                                modifier = Modifier.weight(1f),
                                colors = FilterChipDefaults.filterChipColors(
                                    selectedContainerColor = MaterialTheme.colorScheme.primary,
                                    selectedLabelColor = MaterialTheme.colorScheme.onPrimary
                                )
                            )
                            FilterChip(
                                selected = extractionLevel == "B",
                                onClick = { extractionLevel = "B" },
                                label = { Text("B 纪念档") },
                                modifier = Modifier.weight(1f),
                                colors = FilterChipDefaults.filterChipColors(
                                    selectedContainerColor = MaterialTheme.colorScheme.primary,
                                    selectedLabelColor = MaterialTheme.colorScheme.onPrimary
                                )
                            )
                        }
                        Text(
                            if (extractionLevel == "A") "只留最精彩的（约 20~40%）"
                            else "有意义的都保留（约 40~80%）",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(top = 4.dp)
                        )
                    }
                    
                }
            }
        },
        confirmButton = {
            TextButton(onClick = {
                // 开启远程分析时校验地址：release/debug 都执行，避免家人误填公网 http:// 泄露 token。
                if (useRemote) {
                    val v = UrlPolicy.validate(homelabAddr)
                    if (!v.ok) {
                        checkResult = v.reason
                        checkResultIsError = true
                        return@TextButton
                    }
                }
                // 保存设置
                settings.useRemoteAnalysis = useRemote
                settings.homelabAddress = homelabAddr.trim()
                settings.authToken = authToken.trim()
                settings.extractionLevel = extractionLevel
                onDismiss()
            }) {
                Text("保存")
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text("取消")
            }
        }
    )
}
