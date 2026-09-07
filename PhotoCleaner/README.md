# PhotoCleaner - 照片清理 APP

> 配套 AIPhotoArrange PC 端的手机照片清理工具

## 📱 项目简介

PhotoCleaner 是一个极简的安卓手机 APP，用于批量删除 PC 端 AI 筛选后生成的非精华照片，让手机相册只保留精华照片，方便直接分享到微信/朋友圈。

**核心功能**：
- 导入 PC 端生成的删除列表（`non_highlight_photos_*.txt`）
- 匹配手机相册中的照片（文件名 + 大小双重匹配）
- 预览待删除照片（3 列网格缩略图）
- 批量删除到系统回收站（Android 11+ 30 天内可恢复）

**设计原则**：
- **极简**：只做删除，不做 AI 推理/归档/合并
- **快速**：3 步完成（导入 → 预览 → 删除），操作时间 < 1 分钟
- **安全**：移到回收站可恢复，文件名+大小双重匹配防误删
- **轻量**：APK < 5MB，无需网络，权限少

## 🎯 使用流程

```
电脑端：
  照片复制到电脑 → 运行流水线 → 生成删除列表 non_highlight_photos_*.txt
       ↓
  微信文件传输助手发送到手机
       ↓
手机端：
  打开 PhotoCleaner APP → 导入列表文件 → 匹配 100 张照片
       ↓
  预览待删除照片（网格缩略图） → 确认删除
       ↓
  系统弹窗授权 → 删除到回收站 → 完成（手机相册只剩 52 张精华）
```

## ⚙️ 技术栈

| 组件 | 技术选型 | 版本 |
|------|---------|------|
| 语言 | Kotlin | 1.9.20 |
| UI 框架 | Jetpack Compose | BOM 2024.02.00 |
| 相册访问 | MediaStore API | - |
| 图片加载 | Coil | 2.5.0 |
| 权限管理 | Accompanist Permissions | 0.32.0 |
| 删除 API | MediaStore.createTrashRequest | API 30+ |

**系统要求**：
- minSdk: 30（Android 11，2020年发布）
- targetSdk: 34（Android 14）
- 理由：`createTrashRequest`（回收站）需要 API 30+

## 🏗️ 项目结构

```
PhotoCleaner/
├── app/
│   ├── src/main/
│   │   ├── java/com/example/photocleaner/
│   │   │   ├── MainActivity.kt              # 主界面（三步流程）
│   │   │   ├── PhotoMatcher.kt              # 照片匹配模块
│   │   │   ├── PhotoDeleter.kt              # 删除执行模块
│   │   │   ├── models/
│   │   │   │   ├── PhotoInfo.kt             # 相册照片数据模型
│   │   │   │   ├── PhotoToDelete.kt         # 待删除照片数据模型
│   │   │   │   └── DeleteResult.kt          # 删除结果数据模型
│   │   │   └── ui/
│   │   │       └── theme/
│   │   │           └── Theme.kt             # Material3 主题配置
│   │   ├── res/
│   │   │   ├── values/
│   │   │   │   ├── strings.xml              # 字符串资源
│   │   │   │   └── themes.xml               # 基础主题
│   │   └── AndroidManifest.xml              # 应用清单（权限配置）
│   ├── build.gradle.kts                     # 应用构建配置
│   └── proguard-rules.pro                   # 代码混淆规则
├── build.gradle.kts                         # 项目构建配置
├── settings.gradle.kts                      # Gradle 设置
├── gradle.properties                        # Gradle 属性配置
└── .gitignore                               # Git 忽略规则
```

## 🔑 核心模块说明

### PhotoMatcher（照片匹配模块）

**职责**：
- 解析删除列表文件（`文件名|大小` 格式）
- 扫描手机相册（MediaStore 查询）
- 文件名+大小双重匹配（±1KB 容差）

**防误删机制**：
- 手机相机序号重置（拍到 9999 后从 0001 重新开始）
- 换手机后文件名重置
- 跨年同名文件（2024年 IMG_0001.jpg vs 2025年 IMG_0001.jpg）

### PhotoDeleter（删除执行模块）

**职责**：
- 批量删除照片到系统回收站
- 处理用户授权（Android 11+ 系统弹窗）
- 统计删除结果（成功/失败）

**关键特性**：
- 使用 `MediaStore.createTrashRequest` 移到回收站（30 天内可恢复）
- 用户确认后，系统自动执行删除，APP 无需手动操作

## 🛠️ 开发指南

### 环境要求

- Android Studio Hedgehog (2023.1.1) 或更高版本
- JDK 17
- Android SDK 34
- Gradle 8.2.0+

### 构建步骤

1. **克隆项目**
   ```bash
   cd AIPhotoArrange
   cd PhotoCleaner
   ```

2. **在 Android Studio 中打开项目**
   - File → Open → 选择 `PhotoCleaner` 目录
   - 等待 Gradle 同步完成

3. **连接真机或启动模拟器**
   - 真机需开启「开发者选项」和「USB 调试」
   - 模拟器需 Android 11 (API 30) 或更高版本

4. **运行应用**
   - 点击 Run 按钮（绿色三角）
   - 或使用命令：`./gradlew installDebug`

### 打包 Release APK

1. **生成签名密钥**（首次）
   ```bash
   keytool -genkey -v -keystore photocleaner.keystore -alias photocleaner -keyalg RSA -keysize 2048 -validity 10000
   ```

2. **在 Android Studio 中打包**
   - Build → Generate Signed Bundle / APK
   - 选择 APK → Next
   - 选择 keystore 文件和密码
   - Build Type 选择 `release`
   - Finish

3. **输出位置**
   ```
   app/build/outputs/apk/release/app-release.apk
   ```

### 代码规范

- 遵循 Kotlin 官方代码风格
- 使用 Jetpack Compose 声明式 UI
- 详细的代码注释（尤其是关键逻辑）
- 合理的日志输出（Log.i / Log.w / Log.e）

## 📋 待办事项（Day 2-5）

- [ ] **Day 2：单元测试**
  - PhotoMatcher 解析逻辑测试
  - 文件名+大小匹配边界测试
  
- [ ] **Day 3：UI 优化**
  - 加载状态优化（匹配过程进度提示）
  - 错误提示完善
  - 空状态占位图

- [ ] **Day 4：真机测试**
  - 小米/华为/OPPO 各一台
  - MediaStore 兼容性测试
  - 大列表性能测试（1000+ 张照片）

- [ ] **Day 5：打包发布**
  - 生成签名 APK
  - APK 体积验证 < 5MB
  - 编写用户说明文档

## 🐛 已知限制

1. **系统版本限制**：仅支持 Android 11 及以上（API 30+），覆盖 2026 年约 90%+ 设备
2. **大列表性能**：1000+ 张照片时网格滚动可能略卡，建议分批处理
3. **文件名冲突**：完全无关联的跨目录同名文件不会被双重匹配覆盖（保守策略，防误杀）

## 📄 许可证

本项目是 AIPhotoArrange 项目的配套工具，遵循相同的 AGPL-3.0 + 双重许可协议，详见主项目根目录 [`LICENSE`](../LICENSE)。商业授权请联系 tobias@sina.com。

## 📞 反馈与支持

如有问题或建议，请在 AIPhotoArrange 主项目中提 Issue。
