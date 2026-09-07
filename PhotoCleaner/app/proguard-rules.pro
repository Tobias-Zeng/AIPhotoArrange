# PhotoCleaner ProGuard 规则

# 保留 Kotlin 元数据
-keepattributes *Annotation*, InnerClasses
-dontnote kotlinx.serialization.AnnotationsKt

# Compose 相关（BOM 已包含大部分规则，此处保留数据模型）
-keep class com.example.photocleaner.models.** { *; }

# Coil
-keep class coil.** { *; }

# EncryptedSharedPreferences 依赖 Google Tink。Tink 引用了以下可选依赖：
#   - com.google.errorprone.annotations.*  编译期注解，运行时不需要
#   - com.google.api.client.http.*         KeysDownloader（云 KMS 场景），本地存储不用
#   - org.joda.time.*                       KeysDownloader 时间戳，本地存储不用
#   - javax.annotation.*                    编译期
# 全部 -dontwarn 即可让 R8 通过；本地加密只用到 AES 相关子集。
-dontwarn com.google.errorprone.annotations.**
-dontwarn com.google.api.client.**
-dontwarn org.joda.time.**
-dontwarn javax.annotation.**
-keep class com.google.crypto.tink.** { *; }

# 去除 release 包中的 verbose/debug/info 日志（保留 warn/error 供家人反馈抓崩溃）。
# 防止 task_id / 文件名 / 服务响应体等信息泄露到 logcat。
-assumenosideeffects class android.util.Log {
    public static *** v(...);
    public static *** d(...);
    public static *** i(...);
}
