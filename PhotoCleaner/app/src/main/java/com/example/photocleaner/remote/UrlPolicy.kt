package com.example.photocleaner.remote

/**
 * 服务地址策略校验（B1/B2）。
 *
 * 公网安全约定：分发给家人/朋友的 APK 只允许两类服务地址：
 *   1. HTTPS（任何主机名）：面向公网 Caddy 反代，走 TLS，token 不明文
 *   2. HTTP + A/B/C 类私有地址：局域网直连服务器，不经过公网
 *      - A 类  10.x.x.x
 *      - B 类  172.16.x.x ~ 172.31.x.x
 *      - C 类  192.168.x.x
 *
 * 公网 http:// 一律拒绝：token 会在公网明文可抓包。
 *
 * 说明：Android network-security-config 不支持 CIDR，故明文白名单在代码层把关。
 */
object UrlPolicy {

    /** 校验结果：ok = 通过；否则 reason 是面向用户的中文原因。 */
    data class Result(val ok: Boolean, val reason: String? = null)

    // 单个 IP 段 0-255 的严格匹配（防止 10.999.999.999 之类被误判为私有地址）
    private const val OCTET = "(?:25[0-5]|2[0-4]\\d|1\\d\\d|[1-9]?\\d)"

    private val privateIpRegex = Regex(
        // 10.x.x.x
        "^10\\.$OCTET\\.$OCTET\\.$OCTET$|" +
        // 172.16-31.x.x
        "^172\\.(?:1[6-9]|2\\d|3[01])\\.$OCTET\\.$OCTET$|" +
        // 192.168.x.x
        "^192\\.168\\.$OCTET\\.$OCTET$"
    )

    /**
     * 校验用户在设置页输入的服务地址是否可接受。
     *
     * 判定：
     *   - 允许空字符串（保存空值即“未配置”，不校验）
     *   - 必须以 http:// 或 https:// 开头
     *   - https:// 任意主机 -> 通过
     *   - http:// 主机为私有 IP -> 通过
     *   - http:// 主机为公网/域名 -> 拒绝（会明文泄露 token）
     */
    fun validate(rawUrl: String): Result {
        val url = rawUrl.trim()
        if (url.isEmpty()) return Result(true)

        val lower = url.lowercase()
        val (scheme, rest) = when {
            lower.startsWith("https://") -> "https" to url.substring(8)
            lower.startsWith("http://")  -> "http"  to url.substring(7)
            else -> return Result(false, "地址必须以 http:// 或 https:// 开头")
        }

        // 提取 host：去掉路径、去掉端口
        val host = rest.substringBefore('/').substringBeforeLast(':')
            .let { if (it.startsWith('[')) it.trimStart('[').trimEnd(']') else it }
        if (host.isEmpty()) return Result(false, "地址缺少主机名")

        if (scheme == "https") return Result(true)

        // scheme == "http"：仅允许 A/B/C 类私有 IP
        return if (privateIpRegex.matches(host)) {
            Result(true)
        } else {
            Result(false, "公网地址必须使用 https://；http:// 仅允许 10.x / 172.16-31.x / 192.168.x 内网 IP")
        }
    }

    /**
     * 运行时强制校验（纵深防御）：在真正发起网络请求前调用。
     *
     * validate() 只在设置保存时拦一次；若地址通过迁移逻辑或异常途径落入存储，
     * 发请求前必须再断言一次，避免 token 走公网明文。校验失败抛 IllegalArgumentException。
     */
    fun enforce(rawUrl: String) {
        val r = validate(rawUrl)
        require(r.ok) { r.reason ?: "服务地址不合法" }
    }
}
