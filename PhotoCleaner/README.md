# PhotoCleaner — 照片清理 APP / Photo cleanup app

> 配套 AIPhotoArrange 的安卓端照片清理工具 / The Android companion app for AIPhotoArrange
>
> 🌐 中文在上，English follows in each section.

## 📱 项目简介 / Overview

PhotoCleaner 是 AIPhotoArrange 的安卓端伴侣 APP。它的**主打能力是远程分析**：手机相册的照片**缩略图**直传到你自建的 PhotoArrangeAPI 服务，在你自己的机器上跑同一套 AI 精华挑选流水线，拿回"该删哪些"的清单后一键删除到系统回收站——**原图始终不出手机**。这样无需先把照片导到电脑，手机相册就能就地瘦身、只留精华。

_PhotoCleaner is the Android companion to AIPhotoArrange. **Its primary feature is remote analysis**: the app uploads gallery **thumbnails** directly to your self-hosted PhotoArrangeAPI, which runs the same AI curation pipeline on your own machine and returns the "what to delete" list — **originals never leave the phone**. No need to copy photos to a PC first; you clean the gallery in place and keep only the highlights._

**特别适合谁 / Perfect for**

一次外出就拍下**大量照片**的摄影师、连拍党、旅游/街拍爱好者——刚从一场活动、一趟旅行、一次街拍回来，相册里躺着几百上千张，需要**快速筛出值得留的精华**、批量清掉仓鼠囤积。用 PhotoCleaner，几分钟内完成"上传分析 → 预览 → 一键删除"，不用回电脑挨个翻。

_Photographers who come home with **hundreds of shots** from a single outing — burst shooters, travelers, street photographers. You just got back from an event, a trip, or a walk with a few hundred to a thousand frames and need to **quickly find the keepers** and bulk-trim the hoard. With PhotoCleaner, "analyze → preview → one-tap delete" takes minutes — no need to flip through them back on your PC._

**隐私关键点 / A privacy note（重要）**

手机上传到自建服务的**只有缩略图，且不含任何 GPS 信息**——App 生成缩略图时故意剔除 EXIF 里的位置标签（也拿不到系统位置的媒体权限）。因此手机端分析**不会**把你的拍摄地点传到服务端，服务端的流水线也会在无 GPS 时**自动跳过 01b 地理解析阶段**；而 02 的精华分析、事件命名**完全不依赖 GPS**，所以**隐私与精华挑选效果互不影响**。

_Only **thumbnails, with no GPS** are uploaded. The app strips EXIF location tags when generating thumbnails (it also lacks the media-location permission), so your shooting locations never leave the phone. The server-side pipeline **auto-skips the 01b geocoding stage** when GPS is absent, and stage 02's highlight analysis and event naming **don't depend on GPS at all** — **so privacy protection doesn't affect curation quality**._

**主功能 · 远程分析（联网）/ Primary · Remote analysis (networked)**
App 生成缩略图 → 分块上传到自建服务 → 服务端分析 → 回传非精华清单 → 预览 → 一键删除。分析结果本地持久化，误触返回/重启可自动恢复预览。
_Thumbnails → chunked upload → server-side analysis → non-highlight list → preview → one-tap delete. Results persist locally and survive back-press/restart._

> ℹ️ 远程分析的服务端（PhotoArrangeAPI）说明、最小启动方式与安全须知见主项目 [`README.md`](../README.md) 的「远程分析与手机端清理」章节。**该服务须认证，且设计为仅在内网/私有网络访问；App 会拦截明文 `http://` 公网地址以防 token 泄露。**
> _For the server side (PhotoArrangeAPI), minimal setup, and security notes, see the "Remote analysis & mobile cleanup" section in the main [`README.md`](../README.md). **The service is authenticated and intended for private-network access only; the app blocks cleartext `http://` public addresses to avoid leaking the token.**_

**附加功能 · 导入清单（本地、无需联网）/ Secondary · Import list (local, offline)**
如果你已经在电脑端跑完流水线，也可以把生成的 `non_highlight_photos_*.txt` 发到手机，App 导入后在本地匹配并删除，全程不联网。适合不想搭建服务、直接复用电脑端结果的场景。
_If you've already run the PC pipeline, you can also send the generated `non_highlight_photos_*.txt` to your phone, import it, and match & delete locally with no network at all — handy when you'd rather reuse PC-side results than run a server._

## ✨ 核心功能 / Features

- **远程分析（主功能）**：缩略图直传自建 PhotoArrangeAPI，原图不出手机，结果本地持久化 / **remote analysis (primary)**: upload thumbnails to your self-hosted API, originals stay on-device, results persist locally
- 缩略图网格预览待删除照片 / grid thumbnail preview of deletion candidates
- 批量删除到系统回收站（Android 11+ 30 天内可恢复）/ batch delete to trash (recoverable for 30 days)
- 相册照片匹配（文件名 + 大小双重匹配，±1KB 容差，防误删）/ gallery matching by filename + size (±1KB)
- **附加**：导入 PC 端删除清单（`non_highlight_photos_*.txt`）本地删除 / **secondary**: import the PC-side delete list for offline deletion

**设计原则 / Principles：**

- **远程分析优先**：无需先把照片导到电脑，手机就地完成分析与清理 / remote-analysis first: clean in place without copying to a PC
- **隐私**：只上传缩略图，原图不出手机；服务仅内网、拦截明文公网地址 / privacy: only thumbnails leave the device; server is LAN-only
- **安全**：移到回收站可恢复；文件名+大小双重匹配防误删 / safe: recoverable trash + dual-key matching
- **轻量**：APK < 5MB / lightweight APK

## ⚙️ 技术栈 / Tech stack

| 组件 / Component | 选型 / Choice | 版本 / Version |
|------|---------|------|
| 语言 / Language | Kotlin | 1.9.20 |
| UI | Jetpack Compose | BOM 2024.02.00 |
| 相册访问 / Gallery | MediaStore API | — |
| 图片加载 / Image loading | Coil | 2.5.0 |
| 权限 / Permissions | Accompanist Permissions | 0.32.0 |
| 网络（远程分析）/ Networking | OkHttp | 4.12.0 |
| JSON | org.json | 20231013 |
| 加密存储（服务地址/token）/ Encrypted storage | androidx.security-crypto | 1.1.0-alpha06 |
| 删除 API / Delete API | MediaStore.createTrashRequest | API 30+ |

**系统要求 / Requirements：** minSdk 30（Android 11）· targetSdk 34（Android 14）。回收站 `createTrashRequest` 需要 API 30+。
_minSdk 30 / targetSdk 34; the trash API requires API 30+._

## 🏗️ 项目结构 / Project layout

```
PhotoCleaner/
├── app/
│   ├── src/main/
│   │   ├── java/com/example/photocleaner/
│   │   │   ├── MainActivity.kt              # 主界面 / main screen
│   │   │   ├── PhotoMatcher.kt              # 照片匹配 / matching
│   │   │   ├── PhotoDeleter.kt              # 删除执行 / deletion
│   │   │   ├── SettingsDialog.kt            # 设置（服务地址/token）/ settings
│   │   │   ├── models/                      # 数据模型 / data models
│   │   │   ├── remote/                      # 远程分析 / remote analysis
│   │   │   │   ├── RemoteAnalysisManager.kt #   分析编排 / orchestration
│   │   │   │   ├── RemoteAnalysisScreen.kt  #   分析界面 / UI
│   │   │   │   ├── HomelabClient.kt         #   API 客户端 / API client
│   │   │   │   ├── UploadService.kt         #   分块上传 / chunked upload
│   │   │   │   ├── ThumbnailGenerator.kt    #   缩略图生成 / thumbnails
│   │   │   │   ├── SettingsRepository.kt    #   加密存储 / encrypted prefs
│   │   │   │   ├── PreviewResultStore.kt    #   结果持久化 / result persistence
│   │   │   │   └── UrlPolicy.kt             #   地址安全校验 / URL guard
│   │   │   └── ui/theme/Theme.kt            # Material3 主题 / theme
│   │   ├── res/
│   │   │   └── xml/network_security_config.xml  # 网络安全策略 / network policy
│   │   └── AndroidManifest.xml
│   ├── src/test/                            # 单元测试 / unit tests
│   │   └── .../PhotoMatcherTest.kt, remote/UrlPolicyTest.kt
│   ├── build.gradle.kts
│   └── proguard-rules.pro
├── build.gradle.kts
├── settings.gradle.kts
├── gradle.properties
├── build_apk.ps1                            # 一键打包脚本 / one-shot build script
└── local.properties.example                 # 签名凭据模板 / signing config template
```

## 🔑 核心模块 / Key modules

### remote/ — 远程分析（主功能）/ remote analysis (primary)

`RemoteAnalysisManager` 编排整个流程，`ThumbnailGenerator` 生成缩略图，`UploadService` 分块上传到 `HomelabClient` 指向的 PhotoArrangeAPI，`PreviewResultStore` 持久化结果。服务地址与认证 token 经 `SettingsRepository`（EncryptedSharedPreferences）加密存储；`UrlPolicy` 在发请求前强制校验地址，拒绝明文 `http://` 公网地址以防 token 泄露。
_`RemoteAnalysisManager` orchestrates the flow; `ThumbnailGenerator` builds thumbnails; `UploadService` chunk-uploads to the PhotoArrangeAPI via `HomelabClient`; `PreviewResultStore` persists results. The address and auth token are stored encrypted via `SettingsRepository`; `UrlPolicy` validates the URL before every request and rejects cleartext `http://` public addresses._

### PhotoDeleter — 删除执行 / deletion

用 `MediaStore.createTrashRequest` 批量移到系统回收站（30 天内可恢复）。用户在系统弹窗确认后由系统执行删除，App 无需持有敏感删除权限。
_Uses `MediaStore.createTrashRequest` to move photos to the system trash (recoverable for 30 days). The system performs the deletion after a user consent dialog._

### PhotoMatcher — 照片匹配（附加）/ matching (secondary)

导入清单模式使用：解析删除清单（`文件名|大小` 格式），扫描相册（MediaStore 查询），按文件名 + 大小双重匹配（±1KB 容差）。双重匹配用于防止误删：相机序号重置、换机文件名重置、跨年同名文件等场景下，单靠文件名会误伤。
_Used by the import-list mode: parses the delete list, queries MediaStore, and matches by filename + size (±1KB) to avoid deleting unrelated look-alikes (camera counter resets, cross-year same-name files, etc.)._
_`RemoteAnalysisManager` orchestrates the flow; `ThumbnailGenerator` builds thumbnails; `UploadService` chunk-uploads to the PhotoArrangeAPI via `HomelabClient`; `PreviewResultStore` persists results. The server address and auth token are stored encrypted via `SettingsRepository`; `UrlPolicy` validates the URL before every request and rejects cleartext `http://` public addresses._

## 🛠️ 构建 / Build

### 环境 / Prerequisites

- Android Studio（Hedgehog 2023.1.1 或更高）/ or newer
- JDK 17
- Android SDK 34 · Gradle 8.2+

### 签名配置 / Signing config

复制 `local.properties.example` 为 `local.properties`（已被 `.gitignore` 忽略），填入你自己的签名凭据：
_Copy `local.properties.example` to `local.properties` (git-ignored) and fill in your own signing credentials:_

```properties
RELEASE_STORE_FILE=photocleaner.keystore
RELEASE_STORE_PASSWORD=<你的-keystore-密码 / your keystore password>
RELEASE_KEY_ALIAS=photocleaner
RELEASE_KEY_PASSWORD=<你的-key-密码 / your key password>
```

首次需生成 keystore / generate a keystore first:

```bash
keytool -genkey -v -keystore photocleaner.keystore -alias photocleaner -keyalg RSA -keysize 2048 -validity 10000
```

> ⚠️ 缺少 `local.properties` 或凭据时，release 构建会自动降级为 debug 签名（方便本地/CI 构建），但不可用于正式分发。
> _Without `local.properties` or credentials, release builds fall back to debug signing — fine for local/CI, not for distribution._

### 打包 Release APK / Build the release APK

推荐用仓库自带脚本一键打包（内含轮询心跳、超时兜底、文件占用重试，并自动复制为带版本号的文件名）：
_Recommended — use the bundled script (heartbeat polling, timeout guard, file-lock retry, auto version-named copy):_

```powershell
powershell -ExecutionPolicy Bypass -File build_apk.ps1
```

产物 / outputs:

```
app/build/outputs/apk/release/app-release.apk
PhotoCleaner-v<版本号 / version>-release.apk     # 脚本自动复制 / auto-copied by the script
```

> 脚本默认按本机的 JDK 17 与 Gradle 8.2 路径查找，若你的环境不同，编辑 `build_apk.ps1` 顶部的 `$JavaHome` / `$GradleBat` 即可。也可退回到 Android Studio 的 Build → Generate Signed Bundle / APK 手动打包。
> _The script looks for JDK 17 and Gradle 8.2 at default local paths; adjust `$JavaHome` / `$GradleBat` at the top of `build_apk.ps1` if yours differ. You can also build manually via Android Studio's Generate Signed Bundle / APK._

### 调试运行 / Run a debug build

```bash
./gradlew installDebug
```

## 🐛 已知限制 / Known limitations

1. 仅支持 Android 11+（API 30+，回收站 API 要求）/ Android 11+ only (trash API requirement)
2. 超大清单（1000+ 张）网格滚动可能略卡，建议分批 / very large lists (1000+) may scroll less smoothly; batch them
3. 完全无关联的跨目录同名文件不会被匹配覆盖（保守策略，防误杀）/ unrelated same-name files across folders are deliberately not matched

## 📄 许可证 / License

本项目是 AIPhotoArrange 的配套工具，遵循相同的 **AGPL-3.0 + 双重许可**协议，详见主项目根目录 [`LICENSE`](../LICENSE)。商业授权请联系 **tobias@sina.com**。
_This is a companion tool of AIPhotoArrange under the same **AGPL-3.0 + dual license**; see [`LICENSE`](../LICENSE). For commercial licensing, contact **tobias@sina.com**._

## 📞 反馈 / Feedback

请在 AIPhotoArrange 主项目提 Issue。/ Please open an issue in the main AIPhotoArrange project.
