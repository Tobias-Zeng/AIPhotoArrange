import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// 从 local.properties 读取签名凭据（不入 git）。缺失时 release 降级到 debug 签名，
// 保证本地/CI 构建不因缺凭据失败。字段见 local.properties.example。
val localProps = Properties().apply {
    val f = rootProject.file("local.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}
val hasReleaseSigning = localProps.getProperty("RELEASE_STORE_PASSWORD") != null &&
        rootProject.file(localProps.getProperty("RELEASE_STORE_FILE", "photocleaner.keystore")).exists()

android {
    namespace = "com.example.photocleaner"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.example.photocleaner"
        minSdk = 30
        targetSdk = 34
        versionCode = 29
        versionName = "1.1.27"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        vectorDrawables {
            useSupportLibrary = true
        }
    }

    signingConfigs {
        if (hasReleaseSigning) {
            create("release") {
                storeFile = rootProject.file(
                    localProps.getProperty("RELEASE_STORE_FILE", "photocleaner.keystore")
                )
                storePassword = localProps.getProperty("RELEASE_STORE_PASSWORD")
                keyAlias = localProps.getProperty("RELEASE_KEY_ALIAS", "photocleaner")
                keyPassword = localProps.getProperty("RELEASE_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            // 开启代码压缩与资源压缩，控制 APK 体积 < 5MB
            isMinifyEnabled = true
            isShrinkResources = true
            // 有 local.properties 签名配置则用 release keystore，否则降级 debug 签名
            signingConfig = if (hasReleaseSigning) {
                signingConfigs.getByName("release")
            } else {
                signingConfigs.getByName("debug")
            }
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        compose = true
    }

    composeOptions {
        // 对应 Kotlin 1.9.20
        kotlinCompilerExtensionVersion = "1.5.4"
    }

    packaging {
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
        }
    }
}

dependencies {
    // Compose BOM 统一管理版本
    val composeBom = platform("androidx.compose:compose-bom:2024.02.00")
    implementation(composeBom)
    androidTestImplementation(composeBom)

    // 核心 AndroidX
    implementation("androidx.core:core-ktx:1.12.0")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.7.0")
    implementation("androidx.activity:activity-compose:1.8.2")

    // Compose UI
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")

    // 图片加载 - Coil（Compose 原生支持）
    implementation("io.coil-kt:coil-compose:2.5.0")

    // 权限管理 - Accompanist
    implementation("com.google.accompanist:accompanist-permissions:0.32.0")

    // 网络请求 - OkHttp（远程分析上传）
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    // JSON 解析（API 响应）
    implementation("org.json:json:20231013")

    // 加密存储 - EncryptedSharedPreferences（保护 auth_token / 服务地址）
    implementation("androidx.security:security-crypto:1.1.0-alpha06")

    // 测试
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.robolectric:robolectric:4.11.1")
    testImplementation("androidx.test:core:1.5.0")
    androidTestImplementation("androidx.test.ext:junit:1.1.5")
    androidTestImplementation("androidx.test.espresso:espresso-core:3.5.1")
    androidTestImplementation("androidx.compose.ui:ui-test-junit4")

    debugImplementation("androidx.compose.ui:ui-tooling")
    debugImplementation("androidx.compose.ui:ui-test-manifest")
}
