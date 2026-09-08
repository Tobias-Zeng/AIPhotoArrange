# AIPhotoArrange · AI 家庭照片智能归档 / AI family photo curation

**本地优先 · 隐私不出户** · **Local-first · Privacy by design**

让本地视觉大模型从海量家庭照片里挑出真正值得留下的瞬间。自动去重、精华挑选、事件命名，按「日期-事件」归档。电脑上整理全库，手机在外随手清理。

_Let a local vision LLM pick the moments worth keeping from a mountain of family photos. Auto-dedup, highlight selection, event naming, archived by date-event. Curate the whole library on your PC; tidy your phone on the go._

phash 切批 → 高德反向地理编码 → 视觉大模型精华挑选与事件命名 →（可选）日报合并 → 跨年主题聚合。
_pHash batching → Amap reverse-geocoding → vision-LLM highlight curation & event naming → (optional) daily merge → (optional) cross-year theme clustering._

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
- **手机端清理 / Mobile cleanup** — 自建 API 服务 + 安卓 App，手机在外远程分析、把"非精华照片"一键删除，是项目的**重要组成**。
  _Self-hosted API + Android app: analyze remotely on the go and delete the "non-highlight" photos in one tap — a **core part** of the project._

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

> 💡 **直接用打包好的 GUI exe（见「跑 / Run」）无需本地 Python 环境**；只有从源码运行时才需要装 Python 3.10+。
> _💡 The prebuilt GUI exe (see "Run") needs **no local Python**; Python 3.10+ is only required when running from source._

- Python 3.10+（源码运行需要 / required to run from source）
- **推荐：LM Studio + `qwen3.8:27b`** — 首选的本地推理组合。**本项目所有提示词都是针对 `qwen3.8:27b` 调优过的，不建议换其它模型。**
  _Recommended: LM Studio + `qwen3.8:27b`. **All prompts in this project are tuned for `qwen3.8:27b`; other models are not recommended.**_
  - 为什么不用 Ollama 跑 Qwen：Ollama 不支持 qwen3.5 架构的**并发推理**，多 worker 会退化为串行，吞吐上不去；LM Studio 可以真正并发，配合本项目的多 worker 明显更快。
    _Why not Ollama for Qwen: Ollama doesn't support **concurrent inference** for the qwen3.5 architecture (multi-worker degrades to serial), whereas LM Studio runs true concurrency and pairs well with this project's multi-worker curation._
- **备选：Ollama + `gemma-4-31B`**（`hf.co/unsloth/gemma-4-31B-it-qat-GGUF:UD-Q4_K_XL`）— 提示词同样针对该模型调优过。
  _Alternative: Ollama + `gemma-4-31B` — prompts are tuned for this model too._
  - ⚠️ **仅当使用 gemma4 模型时**才需要把 Ollama **锁定在 v0.30.8**（禁自动更新）：v0.30.9 起的 context shift 改动会破坏 gemma4 的 vision token。用 LM Studio + Qwen 则无此限制。
    _⚠️ **Only when running gemma4** must Ollama be pinned to **v0.30.8** (the context-shift change since v0.30.9 breaks gemma4's vision tokens). This does not apply to the LM Studio + Qwen setup._
- **无本地显卡也能用：在线模型 / No local GPU? Use an online model** — 没有足够显存跑本地模型时，可直接配置在线服务商。**推荐阿里云百炼 `qwen3.7-plus`**（效果实测；提示词针对它调优过），改 `curator.provider` 为 `dashscope` 并填入 API Key 即可。照片会上传到云端服务商，介意隐私请优先本地方案。
  _No GPU / not enough VRAM? Configure an online provider instead. **Recommended: Aliyun DashScope `qwen3.7-plus` (verified in testing; prompts tuned for it)** — set `curator.provider` to `dashscope` and add your API key. Note that photos are then uploaded to the cloud provider; prefer local if privacy matters._
- **模型结论 / Bottom line**：提示词**只为上述三种模型调优过**（`qwen3.8:27b` / `gemma4:31b` / 在线 `qwen3.7-plus`）。**不建议使用其它模型**，效果可能明显下降；如坚持更换，请用自己的验证集重新校准。
  _Prompts are **only tuned for these three** (`qwen3.8:27b` / `gemma4:31b` / online `qwen3.7-plus`). **Other models are not recommended** — quality may drop noticeably; if you must switch, recalibrate on your own validation set._
- 显存 / VRAM：≥16G（本地模型 / for local models；双卡 16G+16G 或**单卡 32G** 均可跑 6 并发 / dual 16G **or a single 32G** runs 6 workers）
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

**推荐：LM Studio + Qwen / Recommended: LM Studio + Qwen**

1. 装好 LM Studio，在其中下载 `qwen3.8:27b`（本项目提示词针对它调优，建议就用这个）。
   _Install LM Studio and download `qwen3.8:27b` (the prompts are tuned for it — recommended)._
2. 打开 LM Studio 的 **Local Server**（Developer 页），加载该模型并 **Start Server**；默认监听 `http://127.0.0.1:1234/v1`（OpenAI 兼容）。
   _Open LM Studio's **Local Server** (Developer tab), load the model, and **Start Server**; it serves an OpenAI-compatible API at `http://127.0.0.1:1234/v1` by default._
3. 建议在 Server 设置里开启**并发/并行请求**，让本项目的多 worker 真正并发。
   _Enable concurrent/parallel requests in the server settings so this project's multiple workers actually run in parallel._

```bash
# 确认可达 / verify reachable
curl http://127.0.0.1:1234/v1/models
```

**备选：Ollama + gemma4 / Alternative: Ollama + gemma4**

```bash
# 开启 Ollama 并行（环境变量）很重要：本项目多 worker 依赖它才能真正并发
# / Enable Ollama parallelism (env var) — the project's multi-worker relies on it
set OLLAMA_NUM_PARALLEL=6       # 视显存而定 / depends on VRAM

ollama serve
ollama pull hf.co/unsloth/gemma-4-31B-it-qat-GGUF:UD-Q4_K_XL

# 确认可达 / verify reachable
curl http://127.0.0.1:11434/api/tags
```

> ⚠️ 用 gemma4 时记得把 Ollama 锁定在 v0.30.8（见上文环境要求）。/ Pin Ollama to v0.30.8 when using gemma4 (see Requirements).

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
  provider: "lmstudio"                # 切服务商改 provider + 对应 provider_configs 的 base_url/api_key/model
  num_workers: 6
```

> ⚠️ 切服务商**不只是改 `provider` 一行**：还要在 `curator.provider_configs` 下确认（或填写）该 provider 的 `base_url`（服务地址）、`api_key`（密钥）、`model`（模型名）。模板里这几项已给示例值，用前请改成你自己的。
> _⚠️ Switching providers is **not only** changing `provider`: you must also check/fill that provider's `base_url`, `api_key`, and `model` under `curator.provider_configs`. The template has example values; change them to yours._

> 用 LM Studio 时把 `provider` 设为 `lmstudio`（`provider_configs.lmstudio.base_url` 默认 `http://127.0.0.1:1234/v1`）；用 Ollama + gemma4 时设为 `ollama`。
> _Set `provider: lmstudio` for LM Studio (default `http://127.0.0.1:1234/v1`), or `provider: ollama` for the Ollama + gemma4 alternative._

### 4. 跑 / Run

**最简单：打包好的 GUI 可执行文件（推荐）/ Easiest: the prebuilt GUI executable (recommended)**

直接下载 [Releases](../../releases) 里的 `aiphotoarrange.exe`，双击运行，无需装 Python。在界面里填好目录、城市、Key、服务商即可一键归档。
_Grab `aiphotoarrange.exe` from [Releases](../../releases) and double-click it — no Python needed. Fill in the directories, city, key, and provider in the UI, then run._

**从源码跑图形界面 / From source (GUI):**

```bash
python run_gui.py
```

**从源码跑命令行 / From source (CLI):**

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

| 档位 / Level | 说明 / What | 大致提取率 / Rough rate | 适合谁 / For whom |
|:---:|---|:---:|---|
| **A** 精华档 / Highlights | 只保留真正拿得出手的精华，同类雷同只留 1–2 张 / keeps only truly presentable shots; 1–2 per near-duplicate group | ~20–40% | 只要真正值得留的 / only the keepers |
| **B** 纪念档 / Keepsake | 有记忆价值的都留，只剔废片与雷同重复帧 / keeps anything memorable, drops only junk & near-duplicates | ~40–80% | 中间派、怕漏 / the middle ground |
| **C** 归类档 / Classify-all | 一张都不删，全部按事件归档命名；01a 同时关闭元数据废片预筛，尽量一张不落（仅损坏文件排除）/ deletes nothing, names & files everything by event | ~100% | 只想按事件整理、全保留 / archive-all |

> 上面的百分比是**大致范围**，实际落点跟你**照片的重复度**关系很大：同一次拍摄里相似、重复的照片越多，A/B 档剔除的就越多、留下的越少；反之照片彼此独立、不重复，留下的就偏多。所以同一个 A 档，一次活动 50 张可能只留 3–5 张，而一次精心拍摄的活动可能留一半。这属正常现象。
> _The percentages are **rough ranges**; the real outcome depends heavily on **how repetitive your photos are**. More similar/burst shots in one session → A/B cull more and keep fewer; distinct, non-repetitive photos → more are kept. So the same level A might keep just 3–5 of 50 casual burst shots, yet keep half of a carefully-shot event. This is expected._

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

推理服务纯配置驱动。**切服务商要改 `curator.provider` + 该 provider 在 `curator.provider_configs` 下的 `base_url` / `api_key` / `model`**。代码内置多家 OpenAI 兼容服务商。

_Inference is config-driven. **Switching providers means changing `curator.provider` plus that provider's `base_url` / `api_key` / `model` under `curator.provider_configs`.** Built-in OpenAI-compatible providers are configured there._

内置可选项 / built-in options：`ollama`、`lmstudio`、`dashscope`（阿里百炼）、`volcengine`（火山）、`kimi`、`xiaomi_mimo`、`deepseek`、`openai_compatible`、`newapi`（本地 New-API 网关 / local gateway）。

```yaml
curator:
  provider: "dashscope"        # 例如切到云端灾备 / e.g. cloud fallback
  provider_configs:
    dashscope:
      base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
      api_key: "你的 key / your key"
      model: "qwen3.7-plus"    # ⚠️ 模型名为示例，请按你实际部署填写
```

> ⚠️ **模板中所有 `model` 字段都是示例占位值**（如 `gemma4:31b`、`qwen3.7-plus` 等），**请按你实际部署的模型名填写**。提示词只针对 `qwen3.8:27b` / `gemma4:31b` / 在线 `qwen3.7-plus` 调优过，换成其它模型效果可能下降，请用自己的验证集重新校准。
> _All `model` values in the template are placeholders. Fill in the model names you actually deploy. Prompts are tuned only for `qwen3.8:27b` / `gemma4:31b` / online `qwen3.7-plus`; other models may degrade — recalibrate on your own validation set if you switch._

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

## 📱 远程分析与手机端清理 / Remote analysis & mobile cleanup

这是项目的**重要组成**：一套**自建**的远程分析子系统，让手机相册在外也能用上同一套归档能力——**原图不出手机、服务由你自己掌控**。

_This is a **core part** of the project: a **self-hosted** remote-analysis subsystem so your phone gallery can use the same curation on the go — **originals never leave the phone, and you run the server yourself.**_

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

明文提示词放 `prompts/*.txt`（**开源公开**），运行/发布也读 `.enc` 加密版。`.enc` 加密**只是为了防止提示词被误改**，并非保密措施；改完提示词后重新加密同步：
_Plaintext prompts live in `prompts/*.txt` (**open-sourced**); runtime/release also read the `.enc` versions. The `.enc` encryption is **only to prevent accidental edits**, not secrecy; re-encrypt after editing to keep them in sync._

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
