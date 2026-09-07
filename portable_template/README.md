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
# 服务器配置
server:
  host: "10.x.x.x"     # 改为你的 ZeroTier IP 或局域网 IP
  port: 36600
  auth_token: "your-token-here"  # 改为你自己的认证 token

# ZeroTier 配置
zerotier:
  interface_ip: "10.x.x.x"  # 与 server.host 保持一致

# 存储目录
storage:
  base_dir: "D:/AIPhotoArrange_api"  # 临时文件存储位置，自动创建

# Ollama 配置
health_check:
  ollama_url: "http://192.168.x.x:11434"  # Ollama 服务地址
  model: "你的模型名称"
```

**重要**：
- 确保 Ollama 服务已启动并配置好模型
- `storage.base_dir` 目录会自动创建，确保有写入权限
- 如果没有高德地图 API key，stage01b 地理编码会跳过（不影响核心功能）

### 1b. 流水线配置（按需）

便携包内的 `api_server.dist/pipeline_config.yaml` 为**脱敏模板**，所有密钥均为占位符 `xxxxxx`。按你实际使用的模型服务填写：

- **仅用本地模型**（`api_config.yaml` 中 `provider: ollama` 或 `lmstudio`）：无需填任何 api_key，可直接使用。
- **使用云端 provider**（volcengine / deepseek / kimi / dashscope 等）：需在 `pipeline_config.yaml` 的 `curator.provider_configs.<provider>.api_key` 填入真实 key，否则该 provider 调用会失败。
- **需要地理编码**（stage01b）：在 `pipeline_config.yaml` 填入高德地图 `amap_key`；留空则跳过地理编码，不影响核心整理功能。

### 2. 安装服务

**右键** `install.bat` → **以管理员身份运行**

脚本会：
1. 自动检测当前目录（无需修改路径）
2. 安装 Windows 服务
3. 配置为开机自启
4. 启动服务

### 3. 验证安装

浏览器打开：`http://你的IP:36600/api/health`

成功返回类似：
```json
{
  "status": "ok",
  "ollama": {
    "reachable": true,
    "model_available": true
  }
}
```

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

1. 查看 `api_server.dist/logs/api_server.log` 确认错误
2. 确认 Ollama 服务已启动且配置正确
3. 检查存储目录权限

### 手机连接超时

1. 确认服务已启动（`net start PhotoArrangeAPI`）
2. 检查防火墙是否允许端口 36600
3. 确认手机和电脑在同一 ZeroTier 网络或局域网

### Ollama 检测失败

1. 确认 Ollama 服务已启动
2. 检查 `api_config.yaml` 中 `health_check.ollama_url` 是否正确
3. 确认模型名称与 Ollama 中已下载的模型一致

### 照片处理失败

1. 查看 `api_server.dist/logs/uvicorn_error.log` 
2. 检查 `storage.base_dir` 目录的写入权限
3. 确认上传的照片格式正确（jpg/png）

## 技术支持

- 项目地址：https://github.com/your-repo/PhotoArrange
- 问题反馈：提交 GitHub Issue
