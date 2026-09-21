# PhotoArrange API 服务 - 便携版

远程照片分析服务，用于手机 APP 上传照片进行 AI 整理。**完全独立运行**，无需 Python 环境和源码。

## 目录结构

```
PhotoArrangeAPI_Portable/
├── api_server.dist/          # API 服务主程序目录
│   ├── PhotoArrangeAPI.exe   # 主程序
│   ├── api_config.yaml       # 服务配置（监听地址 / auth_token / 存储路径）
│   ├── pipeline_config.yaml  # 流水线配置（amap_key / 云端 provider 的 api_key）
│   ├── prompts/              # 加密提示词（*.enc）
│   ├── logs/                 # 日志目录
│   └── [依赖库...]
├── nssm.exe                  # Windows 服务管理工具
├── install.bat               # 安装脚本
├── uninstall.bat             # 卸载脚本
└── README.md                 # 本文件
```

## 安装步骤

### 1. 配置文件（必需）

编辑 `api_server.dist/api_config.yaml`：

```yaml
# 服务监听（socket 级绑定：只在列出的接口 IP 上监听，其余接口无任何响应）
server:
  hosts:
    - "10.x.x.x"        # 改为你的 ZeroTier IP 或局域网 IP（可只留一个）
    - "192.168.x.x"     # 局域网 IP（改为你的实际 IP）
  port: 36600
  auth_token: "your-token-here"  # 改为你自己的认证 token（建议 32+ 字符随机串，公网部署下为空会拒绝启动）

# 临时目录（上传的缩略图与流水线中间文件）
storage:
  base_dir: "D:/AIPhotoArrange_api"  # 临时文件存储位置，自动创建（安装脚本会自动授予服务账户写权限）

# 健康检查（本地推理服务探活；provider 取值见 pipeline_config.yaml 的 provider_configs）
health_check:
  timeout: 5
  ollama:
    url: "http://192.168.x.x:11434"    # Ollama 服务地址（provider 用 ollama 时）
  lmstudio:
    url: "http://192.168.x.x:21234"    # LM Studio 服务地址（provider 用 lmstudio 时）
  newapi:
    url: "http://127.0.0.1:3000"       # New-API 网关地址（provider 用 newapi 时）
```

**重要**：
- 确保本地推理服务（Ollama / LM Studio）已启动并在 `pipeline_config.yaml` 里配好对应 provider；`health_check` 段的 url 要与 provider 匹配（provider 名不在 `local_providers` 列表时仅校验配置完整性，不发起调用）
- `storage.base_dir` 目录会自动创建，安装脚本会授予服务账户写权限；若自定义了路径，需自行确保 `svc_photoarrange` 账户可写
- 如果没有高德地图 API key，stage01b 地理编码会跳过（不影响核心功能）

### 1b. 流水线配置（按需）

便携包内的 `api_server.dist/pipeline_config.yaml` 为**脱敏模板**，所有密钥均为占位符 `xxxxxx`。按你实际使用的模型服务填写：

- **仅用本地模型**（`api_config.yaml` 的 `pipeline_overrides.curator.provider` 设为 `ollama` / `lmstudio`）：无需填任何 api_key，可直接使用。
- **使用云端 provider**（volcengine / deepseek / kimi / dashscope 等）：需在 `pipeline_config.yaml` 的 `curator.provider_configs.<provider>.api_key` 填入真实 key，否则该 provider 调用会失败。
- **需要地理编码**（stage01b）：在 `pipeline_config.yaml` 填入高德地图 `amap_key`；留空则跳过地理编码，不影响核心整理功能。

### 2. 安装服务

**右键** `install.bat` → **以管理员身份运行**

脚本会：
1. 自动检测当前目录（无需修改路径）
2. 把程序复制到 `%ProgramFiles%\PhotoArrangeAPI`（本机管理员才可改写的加固目录）
3. 创建低权服务账户 `svc_photoarrange` 并用 NSSM 注册 Windows 服务
4. 配置为开机自启并启动服务

**首次安装**会提示你为 `svc_photoarrange` 账户设置一个密码（密码不落盘，仅用于绑定服务）。

**重复运行 = 就地升级（幂等）**：账户和服务都已存在时，重跑 `install.bat` **不会再要密码**、不会卸载重装服务，只会停服务 → 覆盖新二进制 → 重新收紧目录 ACL（含给工作目录授予写权限）→ 刷新服务参数 → 重启服务。**升级版本时直接拿新便携包重跑安装脚本即可。**

> 安装目录的 ACL 由脚本自动维护：安装根目录对服务账户只读（防篡改），仅 `api_server.dist` 工作目录授予写权限（流水线要写 phash 缓存 / 进度文件 / 日志），任务数据目录 `D:\AIPhotoArrange_api` 也自动授权。

### 3. 验证安装

浏览器打开：`http://你的IP:36600/api/health`

成功返回（该端点免认证，仅返回存活状态，不泄露内部信息）：
```json
{
  "status": "ok",
  "timestamp": 1757912345.678
}
```

查看模型等详细状态用 `/api/health/detailed`（**需认证**：请求头带 `Authorization: Bearer <auth_token>`）：
```json
{
  "status": "ok",
  "model_ok": true,
  "provider": "ollama",
  "model": "..."
}
```
模型服务不通时 `status` 为 `"degraded"`，`model_ok` 为 `false`。

## 卸载

**右键** `uninstall.bat` → **以管理员身份运行**

## 管理命令

```batch
# 启动服务
net start PhotoArrangeAPI

# 停止服务
net stop PhotoArrangeAPI

# 查看状态
sc query PhotoArrangeAPI
```

## 日志位置

- API 服务日志：`api_server.dist/logs/api_server.log`
- uvicorn 访问日志：`api_server.dist/logs/uvicorn_access.log`
- uvicorn 错误日志：`api_server.dist/logs/uvicorn_error.log`
- nssm 服务日志：`api_server.dist/logs/api_service_stdout.log` / `api_service_stderr.log`

## 手机 APP 配置

1. 手机安装 PhotoArrange APP
2. 在设置中填入：
   - 服务器地址：`http://你的IP:36600`
   - 认证 Token：与 `api_config.yaml` 中 `auth_token` 一致

## 常见问题

### 服务启动失败（错误 3）

1. 查看日志 `%ProgramFiles%\PhotoArrangeAPI\api_server.dist\logs\`（api_service_stderr.log / api_server.log）确认错误
2. 确认本地推理服务已启动且 `api_config.yaml` 的 `health_check` 配置正确
3. 检查存储目录权限（可重跑 `install.bat` 自动修复，见下条）

### 手机连接超时

1. 确认服务已启动（`net start PhotoArrangeAPI`）
2. 检查防火墙是否允许端口 36600
3. 确认手机和电脑在同一 ZeroTier 网络或局域网

### 模型检测失败（/api/health/detailed 显示 degraded）

1. 确认本地推理服务（Ollama / LM Studio）已启动
2. 检查 `api_config.yaml` 中 `health_check` 段与所配 provider 匹配的 `url` 是否正确（如 provider 用 `lmstudio` 则检查 `health_check.lmstudio.url`）
3. 确认 `pipeline_config.yaml` 中该 provider 的 `model` 与推理服务实际加载的模型名一致

### 服务运行报 PermissionError（写 phash 缓存 / 进度文件 / 日志失败）

常见于服务装在 `Program Files` 后目录权限不完整。**管理员身份重跑 `install.bat` 即可**：脚本会自动补齐工作目录与任务数据目录对服务账户的写权限（幂等，不需要密码，不卸载重装）。

### 照片处理失败

1. 查看 `api_server.dist/logs/uvicorn_error.log` 
2. 检查 `storage.base_dir` 目录的写入权限
3. 确认上传的照片格式正确（jpg/png）

## 技术支持

- 项目地址：https://github.com/your-repo/PhotoArrange
- 问题反馈：提交 GitHub Issue
