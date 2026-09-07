# AIPhotoArrange

**本地优先的家庭照片智能归档工具** · **Local-first family photo curation**

phash 切批 → 高德反向地理编码 → 视觉大模型精华挑选与事件命名 →（可选）日报合并 → 跨年主题聚合。
照片和推理默认全部在本机完成，隐私不出门。

_pHash batching → Amap reverse-geocoding → vision-LLM highlight curation & event naming → (optional) daily merge → (optional) cross-year theme clustering. Photos and inference stay on your machine by default; nothing leaves your network._

> 🌐 中文在上，English below each section. / Chinese first, English follows in each section.

---

## ✨ 核心特性 / Highlights

- **本地优先 / Local-first** — 默认走本地 Ollama / LM Studio 推理，照片不上传任何云端。可选切换到云端服务商。
  _Runs on local Ollama / LM Studio by default; photos never leave your box. Cloud providers are opt-in._
- **五阶段流水线 / Five-stage pipeline** — 切批、地理编码、精华挑选、日报合并、跨年聚合，后两阶段可选。
  _Batching, geocoding, curation, daily merge, cross-year clustering — the last two are optional._
- **三档提取率 / Three extraction levels** — A 精华档 / B 纪念档 / C 归类档，一个开关切换严选到全留。
  _A (strict highlights) / B (keepsake) / C (classify-all): one switch from picky to keep-everything._
- **多服务商 / Multi-provider** — 纯配置切换 Ollama、LM Studio、DashScope、火山、Kimi、DeepSeek、New-API 网关等。
  _Config-only switching across Ollama, LM Studio, DashScope, Volcengine, Kimi, DeepSeek, a New-API gateway, and more._
- **桌面 GUI + CLI** — 图形界面一键跑，或命令行精细控制。
  _One-click desktop GUI, or fine-grained CLI._
- **可选的手机端清理 / Optional mobile cleanup** — 自建 API 服务 + 安卓 App，把"非精华照片"清单同步到手机一键删除。
  _Optional self-hosted API + Android app to push the "non-highlight" list to your phone for one-tap cleanup._

---

## 🧭 工作原理 / How it works

```
照片目录 / Photos
   │
  01a  phash + 时间双信号切批        pHash + time dual-signal batching
   │
  01b  高德反向地理编码（GPS→地名）  Amap reverse-geocoding (GPS → place)
   │
  02   视觉 LLM 精华挑选 + 事件命名   vision-LLM highlight pick + event naming
   │        └── 输出：YYYY-MM-DD-事件名/ 归档目录 + 非精华清单
   │            output: YYYY-MM-DD-event/ folders + non-highlight list
   ├─(可选/optional) 03a  日报合并    daily event merge
   └─(可选/optional) 03b  跨年主题聚合 cross-year theme clustering（复制到独立目录 / copies to a separate dir）
```

---

## 📦 环境要求 / Requirements

- Python 3.10+
- **Ollama v0.30.8**（⚠️ 锁定此版本，禁自动更新；v0.30.9 起的 context shift 改动会破坏 vision token）
  _Pin Ollama to v0.30.8; the context-shift change since v0.30.9 breaks vision tokens._
- Ollama 模型 / model: `hf.co/unsloth/gemma-4-31B-it-qat-GGUF:UD-Q4_K_XL`（示例，可换 / example, swappable）
- 显存 / VRAM：≥16G（双卡 16G+16G 可跑 6 并发 / dual 16G runs 6 workers）
- 内存 / RAM：≥8GB（01a phash 并行峰值约 2–3GB / peak ~2–3GB in stage 01a）
- CPU：01a 阶段吃满所有核心属正常 / stage 01a saturates all cores by design
- 高德开放平台 Key / Amap key（个人 Key 即可，QPS 3 / personal key, QPS 3 is enough）

---

## 🚀 快速开始 / Quick start

### 1. 安装依赖 / Install dependencies

```bash
pip install -r requirements.txt
```

本地流水线核心依赖：`openai`、`pillow`、`requests`、`imagehash`、`pyyaml`。
GUI 额外需要 `customtkinter`、`ruamel.yaml`；API 服务额外需要 `fastapi`、`uvicorn`、`python-multipart`。

_Core: openai, pillow, requests, imagehash, pyyaml. GUI adds customtkinter, ruamel.yaml. The optional API server adds fastapi, uvicorn, python-multipart._

### 2. 启动本地推理 / Start local inference

```bash
ollama serve
ollama pull hf.co/unsloth/gemma-4-31B-it-qat-GGUF:UD-Q4_K_XL

# 确认可达 / verify reachable
curl http://127.0.0.1:11434/api/tags
```

### 3. 生成配置 / Create your config

从模板复制一份真实配置（真实配置不纳入版本控制）：
_Copy the template into your real config (the real config is git-ignored):_

```bash
cp pipeline_config.template.yaml pipeline_config.yaml
```

然后编辑 `pipeline_config.yaml` 的关键几项 / then edit the key fields:

```yaml
profile: "profile1"                    # 当前激活的用户/批次 / active profile
profiles:
  profile1:
    common:
      source_dir: "D:\\你的照片输入目录"   # 扫描源 / input
      target_dir: "D:\\你的归档输出目录"   # 归档输出 / output
common:
  home_city: "你的常驻城市"             # 常驻地 / home city
geo:
  amap_key: "你的高德 Key"             # Amap key
curator:
  extraction_level: "A"               # A / B / C
  provider: "ollama"                  # 切服务商只改这一行 / switch provider here
  num_workers: 6
```

### 4. 跑 / Run

图形界面（推荐 / recommended）：

```bash
python run_gui.py
```

命令行一键跑全流程 / one-shot CLI:

```bash
python run_pipeline.py            # 断点续跑 / resume
python run_pipeline.py --fresh    # 全新跑（旧中间文件归档保留）/ fresh run (old intermediates archived, not deleted)
```

跑完后归档结果在 `target_dir`，按 `YYYY-MM-DD-事件名` 切分目录。
_Results land in `target_dir`, split into `YYYY-MM-DD-event` folders._

---

## 🎚️ 提取档位 A / B / C / Extraction levels

在 `curator.extraction_level` 一项切换，决定 02 阶段筛得多严：
_Set `curator.extraction_level` to control how strict stage 02 is:_

| 档位 / Level | 说明 / What | 大致提取率 / Rough rate |
|:---:|---|:---:|
| **A** 精华档 / Highlights | 生产基线，严选精华 / strict, keeps only the best | ~30% |
| **B** 纪念档 / Keepsake | 只剔废片与高度雷同重复帧，有纪念意义就留 / drops only junk & near-duplicates | ~50–80% |
| **C** 归类档 / Classify-all | 不做审美剔除，全部按事件归档；01a 同时关闭元数据废片预筛，尽量一张不落（仅损坏文件排除）/ no aesthetic culling; also disables 01a metadata pre-filter | ~100% |

各档位自动映射到对应提示词（A=v0.3.0 / B=memory / C=classify_all），由 `curator.prompt_files` 配置。
_Each level maps to its own prompt via `curator.prompt_files`._

> ⚠️ 切换档位后必须 `python run_pipeline.py --fresh` 重跑；断点续跑会跳过已完成批次，旧结果不会按新档位重评。
> _After changing the level, re-run with `--fresh`; a resume run won't re-evaluate already-processed batches._

核对实际提取率 / check the actual rate:

```bash
python extraction_rate_stats.py            # 当前 profile / active profile
python extraction_rate_stats.py --top 15   # 附列照片最多的前 15 个事件 / top 15 events
python extraction_rate_stats.py --json     # JSON 输出 / JSON output
```

---

## 🔌 服务商切换 / Switching providers

推理服务纯配置驱动，改 `curator.provider` 一行即可切换。代码内置多家 OpenAI 兼容服务商，各自的 `base_url` / `api_key` / `model` 在 `curator.provider_configs` 下配置。

_Inference is config-driven. Change `curator.provider` to switch. Built-in OpenAI-compatible providers are configured under `curator.provider_configs`._

内置可选项 / built-in options：`ollama`、`lmstudio`、`dashscope`（阿里百炼）、`volcengine`（火山）、`kimi`、`xiaomi_mimo`、`deepseek`、`openai_compatible`、`newapi`（本地 New-API 网关 / local gateway）。

```yaml
curator:
  provider: "dashscope"        # 例如切到云端灾备 / e.g. cloud fallback
  provider_configs:
    dashscope:
      base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
      api_key: "你的 key / your key"
      model: "qwen3.7-plus"    # ⚠️ 以下模型名均为示例，请按你实际部署填写
```

> ⚠️ **模板中所有 `model` 字段都是示例占位值**（如 `gemma4:31b`、`qwen3.7-plus`、`doubao-*`、`kimi-*` 等），**请按你实际部署的模型名填写**。提示词是按 31B 级视觉模型调过的，换模型建议用自己的验证集重新校准。
> _All `model` values in the template are placeholders. Fill in the model names you actually deploy. Prompts were tuned for a 31B-class vision model; recalibrate on your own validation set if you switch._

`run_pipeline.py` 在跑 02/03a/03b 前会**预检**所选服务商（连通性 + 目标模型是否存在），不可达就立即报错退出。想跳过加 `--skip-preflight`。
_Before stages 02/03a/03b, `run_pipeline.py` preflights the selected provider (reachability + model presence) and aborts if unreachable. Skip with `--skip-preflight`._

---

## 🧱 五阶段流水线 / The five stages

| 阶段 / Stage | 脚本 / Script | 作用 / What it does | 默认 / Default |
|:---:|---|---|:---:|
| 01a | `stage01a_phash_chunker.py` | phash + 时间双信号切批、元数据预筛、去重 / batching, pre-filter, dedup | 开 / on |
| 01b | `stage01b_geo_resolver_amap.py` | 高德反向地理编码，跨用户共享坐标缓存 / Amap regeo with shared cache | 开 / on |
| 02 | `stage02_aesthetic_curator.py` | 视觉 LLM 精华挑选 + 事件命名，生成归档目录与非精华清单 / curation, naming, archive | 开 / on |
| 03a | `stage03a_daily_summary.py` | 同日多事件按 LLM 判断合并、扁平化，写 `manifest.json` / same-day merge | 关 / off |
| 03b | `stage03b_yearly_summary.py` | 跨年/跨日同主题聚合，**复制**到 `<target_dir>_跨年聚合`，不动原目录 / cross-year clustering (copy) | 关 / off |

开启 03a / 03b：在配置里把 `daily_summary.enabled` / `yearly_summary.enabled` 设为 `true`，或用 `--only 03a` 对历史目录单跑一次。
_Enable 03a/03b via `daily_summary.enabled` / `yearly_summary.enabled: true`, or run once on existing folders with `--only 03a`._

常用命令 / common commands：

```bash
python run_pipeline.py --only 01a        # 只跑某阶段 / one stage
python run_pipeline.py --from 01b        # 从某阶段跑到结尾 / from a stage to the end
python run_pipeline.py --dry-run         # 只打印将执行动作 / print actions only
python run_pipeline.py --fresh           # 全新跑（旧中间文件归档保留）/ fresh
python run_pipeline.py --fresh --reset-phash   # 连 phash 缓存一起重算 / also rebuild phash cache
python run_pipeline.py --profile profile2      # 临时切 profile / switch profile
python run_pipeline.py --skip-preflight        # 跳过模型预检 / skip preflight
```

单独跑某个脚本也可（同样读 `pipeline_config.yaml`）：
_You can also run any stage script directly (it reads `pipeline_config.yaml` too)._

## 👥 多用户 / Multiple profiles

`pipeline_config.yaml` 里可同时保留多个 profile，每个只写与共享默认值不同的部分（通常就是输入/输出目录）。中间文件按激活的 profile 自动加后缀，互不覆盖。

_Keep multiple profiles in one config; each overrides only what differs from the shared defaults (usually just directories). Intermediate files are auto-suffixed per active profile._

```yaml
profiles:
  profile1:
    common:
      source_dir: "D:\\A照片_输入"
      target_dir: "D:\\A照片_输出"
  profile2:
    common:
      source_dir: "D:\\B照片_输入"
      target_dir: "D:\\B照片_输出"
```

切换 / switch：改 `profile:` 顶部一行，或 `python run_pipeline.py --profile profile2`。
每个 profile 还能单独覆盖 `provider` / `num_workers` 等（写在该 profile 的 `curator:` 下）。
_Change the top-level `profile:`, or pass `--profile`. A profile can also override `provider` / `num_workers` under its own `curator:`._

> 注 / Note：GUI 切换 profile 时只读写输入/输出目录，其余参数全局共享；要让某 profile 单独覆盖 provider 等需手工编辑 yaml。

---

## 📱 可选：远程分析与手机端清理 / Optional: remote analysis & mobile cleanup

除了本地跑，项目还带一套**自建**的远程分析子系统，让手机相册也能用上同一套归档能力——**原图不出手机、服务由你自己掌控**。

_Beyond local runs, the project ships a **self-hosted** remote-analysis subsystem so your phone gallery can use the same curation — **originals never leave the phone, and you run the server yourself.**_

**它由两部分组成 / Two parts:**

- **PhotoArrangeAPI（`api_server.py`）** — 一个 FastAPI 服务，接收手机分块上传的**缩略图**，在你自己的机器上跑同一套流水线，回传"该删哪些"的结果。核心接口包括健康检查、分块上传、任务状态查询与结果下载。
  _A FastAPI service that receives chunk-uploaded **thumbnails** from the phone, runs the same pipeline on your own machine, and returns which photos to delete. Endpoints cover health check, chunked upload, task status, and result download._

- **PhotoCleaner（`PhotoCleaner/`）** — 一个 Kotlin / Jetpack Compose 安卓 App。它把相册缩略图发给你的 PhotoArrangeAPI 分析，拿到非精华清单后在手机上**一键删除**，只留精华。支持结果本地持久化（误触返回/重启自动恢复预览）。
  _A Kotlin / Jetpack Compose Android app. It sends gallery thumbnails to your PhotoArrangeAPI, gets back the non-highlight list, and deletes them on-device with one tap — keeping only the highlights. Results persist locally (survives back-press/restart)._

**最小启动 / Minimal start（本机自用 / local, for yourself）:**

```bash
cp api_config.template.yaml api_config.yaml   # 填入你的认证 token 等 / fill in your auth token
python api_server.py
```

服务默认只监听本机/内网地址，配合 App 里填写的服务地址与认证 token 使用。
_The server binds to a local/LAN address and pairs with the address + auth token you enter in the app._

> ⚠️ **安全提示 / Security note**：这是一个需要认证的自建服务，**默认设计为仅在内网/私有网络（如你自己的局域网或 ZeroTier 等虚拟组网）访问**。请勿在没有 HTTPS、反向代理与访问控制的情况下把它直接暴露到公网。App 侧对明文 `http://` 公网地址有拦截策略以防 token 泄露。本仓库不含任何具体的公网部署配置。
> _This is an authenticated, self-hosted service **designed for private-network access only** (your LAN or an overlay network like ZeroTier). Do not expose it to the public internet without HTTPS, a reverse proxy, and access control. The app blocks cleartext `http://` public addresses to avoid leaking the token. No public-deployment config ships in this repo._

安卓 App 的构建说明见 [`PhotoCleaner/README.md`](./PhotoCleaner/README.md)。
_See [`PhotoCleaner/README.md`](./PhotoCleaner/README.md) for building the Android app._

---

## 🖥️ 桌面 GUI / Desktop GUI

```bash
python run_gui.py
```

GUI 自动读取 `pipeline_config.yaml` 并把当前值填进表单（源/目标目录、常驻城市、各 Key、Profile、Provider、提取档位等）。改完点"保存"写回 YAML，点"开始一键归档流水线"即在后台线程跑 `run_pipeline.py`，实时日志同步显示、界面不卡死。运行结束若有 `[WARNING]`/`[ERROR]` 会高亮点名是哪个阶段、共几条。

_The GUI loads `pipeline_config.yaml` into a form (directories, home city, keys, profile, provider, extraction level, …). Save writes back to YAML; the run button launches `run_pipeline.py` on a background thread with a live log pane. Any `[WARNING]`/`[ERROR]` is surfaced with the stage name and count when the run ends._

---

## 📦 打包 / Packaging (Nuitka)

项目有两个独立的打包入口，产出两个 exe：
_Two separate build entry points produce two exes:_

```bash
python build_exe.py         # → release/aiphotoarrange.exe   （桌面 GUI 应用 / desktop GUI app）
python build_api_exe.py     # → PhotoArrangeAPI.exe          （API 服务 / API server）
```

打包时配置由**脱敏模板**生成（密钥占位 `xxxxxx`），只有加密提示词 `prompts/*.enc` 进包、明文 `.txt` 不打包。
_Builds generate config from the **sanitized templates** (keys as `xxxxxx`); only encrypted `prompts/*.enc` are bundled, never the `.txt` plaintext._

> ⚠️ 阶段脚本文件名以 `stage` 开头、不能以数字开头（Nuitka 生成的 C 标识符不能以数字开头）。

---

## 🔐 提示词加密 / Prompt encryption

明文提示词放 `prompts/*.txt`（**开源公开**），运行/发布只读加密版 `prompts/*.enc`。改完提示词后重新加密：
_Plaintext prompts live in `prompts/*.txt` (**open-sourced**); runtime/release read the encrypted `prompts/*.enc`. Re-encrypt after edits:_

```bash
python encrypt_prompt.py    # 把 prompts/*.txt 全部重新生成为 *.enc
```

解密在 `stage02_aesthetic_curator.py` 的 `_load_prompt_text()` 里自动完成，使用固定内置密钥（仅用于打包混淆，非安全密钥）。
_Decryption happens automatically in `_load_prompt_text()` using a fixed built-in key (packaging obfuscation only, not a security secret)._

---

## 📁 日志与中间文件 / Logs & intermediates

- 各阶段写独立日志到 `logs/`，控制台同时打印；02 定时打印速度与 ETA。
  _Each stage writes its own log under `logs/`; stage 02 also prints throughput & ETA._
- 02 支持断点续跑，进度存 `02_progress_<profile>.json`，重跑自动跳过已完成批次。
  _Stage 02 resumes from `02_progress_<profile>.json`._
- `--fresh` 把当前 profile 的批次/进度/清单归档到 `_pipeline_archive/<profile>_<时间戳>/`（不删除），跨用户共享的 `01b_amap_cache.json` 永远保留。若输出目录已存在且非空，会自动改名移走再重建，避免新旧混淆。
  _`--fresh` archives (never deletes) per-profile intermediates into `_pipeline_archive/`, keeps the shared `01b_amap_cache.json`, and renames a non-empty existing output dir before recreating it._

---

## ❓ 常见问题 / FAQ

**Q：02 卡住、HTTP 不返回 / Stage 02 hangs, HTTP never returns**
A：Ollama 挂了或长跑后熔断。Ctrl+C，重启 Ollama，重跑 02（自动续跑）。
_Ollama died or tripped the circuit breaker. Ctrl+C, restart Ollama, re-run 02 (it resumes)._

**Q：命名出现"图像损坏""严重失真"剔除理由 / Odd "corrupted/distorted" rejections**
A：Ollama 版本不对。必须用 v0.30.8，v0.30.9 起的 context shift 改动会破坏 vision token。
_Wrong Ollama version. Use v0.30.8; the context-shift change since v0.30.9 breaks vision tokens._

**Q：高德返回 10021 限流 / Amap 10021 rate limit**
A：01b 已内置退避重试，通常自动恢复；持续报错检查 Key 是否被封或并发调太高。
_01b backs off and retries automatically; if it persists, check the key or lower concurrency._

**Q：想换模型 / Want a different model**
A：改 `curator.provider_configs` 里对应的 `model`；提示词是按 31B 级视觉模型调的，换模型建议用自己的验证集重新校准。
_Change the `model` under `curator.provider_configs`; recalibrate on your own validation set._

---

## 📜 许可证 / License

本项目采用 **GNU Affero 通用公共许可证 v3.0（AGPL-3.0）** 开源，完整条款见根目录 [`LICENSE`](./LICENSE)。
_Licensed under the **GNU Affero General Public License v3.0 (AGPL-3.0)**; full terms in [`LICENSE`](./LICENSE)._

AGPL-3.0 的核心义务：任何对本项目的修改，以及通过网络对外提供服务（例如部署为可远程访问的 API / SaaS），都必须以 AGPL-3.0 向使用者公开对应的完整源代码。
_Under AGPL-3.0, any modification — and any network service built on it (e.g. a remotely accessible API / SaaS) — must make the corresponding complete source available to users under AGPL-3.0._

### 商业授权 / Commercial license

如果你希望在**闭源**或**商业**场景中使用本项目、且不愿受 AGPL-3.0 的开源义务约束（例如集成进闭源产品、提供闭源在线服务），可联系作者获取商业授权（双重许可）。
_For **closed-source** or **commercial** use without AGPL-3.0's copyleft obligations (e.g. integrating into a proprietary product or offering a closed-source service), a commercial license is available (dual licensing)._

**联系邮箱 / Contact: tobias@sina.com**

Copyright (c) 2026 Tobias Zeng. Licensed under AGPL-3.0 for open-source use; commercial licensing available on request.
