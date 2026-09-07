# AIPhotoArrange

本地家庭照片智能归档工具：phash 切批 → 高德反向地理编码 → LLM 精华挑选与事件命名。

> 本文件只讲怎么跑。

---

## 环境要求

- Python 3.10+
- Ollama **v0.30.8**（⚠️ 锁定版本，禁自动更新；v0.30.9 起的 context shift 改动会破坏 vision token）
- Ollama 模型：`hf.co/unsloth/gemma-4-31B-it-qat-GGUF:UD-Q4_K_XL`
- 显存建议：≥16G（双卡 16G+16G 可跑 6 并发）
- 内存：建议 8GB 以上（01a 阶段 phash 并行计算峰值约 2～3GB）
- CPU：01a 阶段会调用所有可用核心并行计算（线程数 = 核心数 - 1），此阶段 CPU 接近满载属正常
- 高德开放平台 Key（个人 Key 即可，QPS 3 上限）

## 依赖安装

```bash
pip install -r requirements.txt

主要依赖：openai、pillow、requests、imagehash、pyyaml。

配置
1. 启动 Ollama 并拉模型

ollama serve
ollama pull hf.co/unsloth/gemma-4-31B-it-qat-GGUF:UD-Q4_K_XL

确认服务可达：

curl http://192.168.x.x:11434/api/tags

2. 只改一个文件：pipeline_config.yaml

所有每次运行需要调整的参数都集中在这里，不用再进 .py 里改。常改的几项：

profile: "JM"                       # 用户/批次标识，决定中间文件后缀
common:
  source_dir: "D:\\你的照片输入目录"    # 01a 扫描、02 归档的源
  target_dir: "D:\\你的归档输出目录"    # 02 输出
  home_city: "重庆市"                 # 常驻地（01b + 02 共用）
geo:
  amap_key: "你的高德 Key"
curator:
  provider: "ollama"                 # 切服务商只改这一行
  num_workers: 6

三个脚本会自动从 pipeline_config.yaml 读取；找不到配置文件时退回脚本内置默认值。

照片提取档位（A / B / C）
在 pipeline_config.yaml 的 curator 段用 extraction_level 一项切换提取率：

curator:
  extraction_level: "A"   # A / B / C

- A 精华档：生产基线 v0.3.0，严选精华，提取率约 30%。
- B 纪念档：只剔除废片与高度雷同的重复劣质帧，只要有记录/纪念意义就保留，约 50%-80%。
- C 归类档：不做任何审美剔除，全部照片按事件归档（代码强制全保留），且 01a 会自动
  关闭元数据废片预筛（min_file_size_kb / min_resolution / max_aspect_ratio），尽量一张不落；
  仅无法读取的损坏文件仍会被排除。

各档位对应的提示词由 curator.prompt_files 映射（A=v0.3.0 / B=memory / C=classify_all），
脚本会按档位自动选择。若显式设置了 curator.prompt_file 则优先使用它（调试/向后兼容）。

⚠️ 切换 extraction_level 后必须 `python run_pipeline.py --fresh` 重跑。
断点续跑会跳过 02_progress 里已完成的批次，不切 --fresh 则旧结果不会按新档位重评。

核对提取率（按档位）
跑完后用这个工具核对实际提取率是否落在档位预期区间：

python extraction_rate_stats.py            # 统计当前激活 profile
python extraction_rate_stats.py --top 15   # 附带列出照片最多的前 15 个事件
python extraction_rate_stats.py --json     # JSON 输出，供其它工具消费

分母=进入 02 的照片数（01a 预筛后送 LLM 的数量，衡量筛选松紧度的正确口径），
分子=target_dir 里实际归档的图片数（排除 _FAILED_FOR_MANUAL_REVIEW）。
会同时给出全库口径（含被 01a 预筛剔除的废片）并提示是否符合档位预期区间。


跑流水线（一键）

用编排脚本把三阶段串起来，一条命令跑完：

# 断点续跑（保留已有进度/缓存）
python run_pipeline.py

# 全新跑：自动把旧的用户专属中间文件"归档"（移动到 _pipeline_archive/，不删除）后再跑
python run_pipeline.py --fresh

其他常用参数：

python run_pipeline.py --only 01a     # 只跑某一阶段（01a / 01b / 02）
python run_pipeline.py --from 01b     # 从某阶段跑到结尾
python run_pipeline.py --dry-run      # 只打印将执行动作，不真跑
python run_pipeline.py --fresh --reset-phash   # 连 phash 缓存一起归档重算
python run_pipeline.py --skip-preflight        # 跳过 02 推理模型可用性预检

02 预检：run_pipeline.py 默认在跑 01a/01b 之前先 ping 一把 02 配置的推理服务
（ollama / dashscope），模型不可达就立刻报错退出，避免忘开 ollama 空跑前两个阶段。
确认环境没问题或想跳过时加 --skip-preflight。

也可以照旧单独跑某个脚本（同样读 pipeline_config.yaml）：

python stage01a_phash_chunker.py
python stage01b_geo_resolver_amap.py
python stage02_aesthetic_curator.py

跑完后归档结果在 target_dir，按 YYYY-MM-DD-事件名 切分目录。

桌面 GUI 客户端（推荐）
不想每次敲命令行，用图形界面同样能完成"改配置 + 一键跑 + 看日志"：

python run_gui.py

GUI 会自动读取 pipeline_config.yaml 并把当前值填进表单（源/目标目录、常驻城市、
各 Key、Profile、Provider 等）。改完点"保存"写回 YAML；点"开始一键归档流水线"
即在后台线程执行 run_pipeline.py，实时日志框同步显示控制台输出，界面不会卡死。

技术要点：
- 界面库 customtkinter，模块化在 gui/ 目录，便于后续 Nuitka 编译打包。
- 流水线在独立 threading.Thread 里跑，绝不阻塞 GUI 主线程。
- sys.stdout / sys.stderr 被重定向到日志框，实时追加并自动滚到底部。
- 运行模式可选"断点续跑"或"全新跑 (--fresh)"。
- 右下角状态指示（● 就绪 / ● 运行中… / ● 完成 等）下方会在运行结束时显示
  警告提示：若中途某个阶段在日志里输出了 `[WARNING]` 或 `[ERROR]`，会多出一行
  橙色文字点名是哪个阶段、共几条，如 `⚠️ AI 智能归档 3 条警告，请查看日志`，
  避免警告淹没在大量日志里找不到。无警告时不显示。断点续跑 / 仅跑 03 / 全流程
  均生效，每次开跑自动清零。

打包为单文件可执行（Nuitka）

用 build_exe.py 一键打包，无需手敲 Nuitka 参数：

python build_exe.py

产物在 release/ 目录：

release/aiphotoarrange.exe      # 单文件 exe，入口为 GUI，运行时自解压、无需安装 python
release/pipeline_config.yaml    # 由 pipeline_config.template.yaml 脱敏模板生成（密钥占位 xxxxxx）
release/prompts/*.enc           # 仅加密提示词（明文 .txt 不打包）

关键设计：
- 三阶段脚本（stage01a/stage01b/stage02）随主程序一起编译，编译模式下 run_pipeline.py
  在进程内 import + 调用各阶段 main()，不启动子进程、不依赖外部 python。
- exe 自调用：GUI 后台跑流水线时用 sys.argv[0]（exe 自身）+ --internal-run-pipeline
  子命令进入编排器模式；--console=disable 下用 _NullStream 兜底防止 sys.stdout 为 None。
- run_gui.py / pipeline_config_loader.py 都做了 Nuitka 检测（__compiled__），
  编译后 BASE_DIR 指向 .exe 同级目录，源码运行时仍指向 __file__ 所在目录。

⚠️ 脚本文件名以 stage 开头、不能以数字开头（Nuitka 生成的 C 标识符限制）。改回
01a/01b/02 命名会导致编译失败。

提示词加密（生产/发布用）

提示词明文放在 prompts/*.txt，生产运行和发布版 exe 只读 prompts/*.enc 加密文件，
明文 .txt 不会打进 exe。改完提示词后必须重新加密：

python encrypt_prompt.py        # 把 prompts/*.txt 全部重新加密为 *.enc

解密在 stage02_aesthetic_curator.py 的 _load_prompt_text() 里自动完成，密钥固定
（_STATIC_PROMPT_KEY），不需要配置。开发期直接改 .txt 跑、发布前跑一次 encrypt_prompt.py。


日志输出

三个阶段都会往 logs/ 目录写独立日志文件（01a / 01b / 02 各一份），控制台同时打印。
02 还会定时打印实时速度与 ETA（基于已处理批次耗时滚动估算）。跑出问题先看 logs/，
不用怕控制台滚没了。

中断与续跑
02 支持断点续跑，直接 `python run_pipeline.py --only 02`（或 `python stage02_aesthetic_curator.py`）即可，
进度记录在 02_progress_<profile>.json，会跳过已完成的批次。

重头跑（不是续跑）时不用再手动删文件，直接：

python run_pipeline.py --fresh

--fresh 会把当前 profile 的批次/进度/未处理清单/geo review 移动到
_pipeline_archive/<profile>_<时间戳>/（不删除，避免误删还有用的数据），
跨用户共享的坐标缓存 01b_amap_cache.json 永远保留。
如果输出目录已存在且非空，会自动加 _renamed_ 时间戳后缀改名移走（如 `XX照片_整理输出` ->
`XX照片_整理输出_renamed_20260716_120000`），腾出原路径让流水线重新创建全新目录输出，
避免新旧照片混在一起。

多用户切换（多 profile）
pipeline_config.yaml 里可以同时保留任意多个 profile。profiles: 段下每个 key 就是
一个 profile，只写与共享默认值不同的部分（通常就是输入/输出目录）：

profiles:
  JM:
    common:
      source_dir: "D:\\JM照片_整理输入"
      target_dir: "D:\\JM照片_整理输出_生产v0.3.0_2024"
  XY:
    common:
      source_dir: "D:\\XY照片_整理输入"
      target_dir: "D:\\XY照片_整理输出_GEO2.1"

切换当前用户，两种方式二选一：

# 方式 1：改配置文件顶部一行
profile: "XY"

# 方式 2：命令行临时指定，不动配置文件
python run_pipeline.py --profile XY

新增一个 profile：在 profiles: 下复制一段、改 key 名和目录即可。每个 profile 还能
单独覆盖 provider / num_workers 等（写在该 profile 的 curator: 下），没写的字段自动
沿用下面的共享默认值。

> 注：GUI 切换 profile 时只读写输入/输出目录，其余参数（城市、高德 Key、大模型服务、
> 运行参数）全局共享，不会按 profile 分别保存。若需让某 profile 单独覆盖 provider 等，
> 需手工编辑 yaml。

中间文件按当前激活的 profile 自动加后缀，互不覆盖，不用再手动改名：


文件	归属	profile=JM 时的实际名
01_photo_batches*.json	用户专属	01_photo_batches_JM.json
01b_geo_review*.json	用户专属	01b_geo_review_JM.json
02_progress*.json	用户专属	02_progress_JM.json
02_unprocessed_*.txt	用户专属	..._JM.txt
01b_geo_alias.json	用户专属	优先用 01b_geo_alias_JM.json，无则回退基础文件
01b_amap_cache.json	跨用户共享（坐标缓存）	不加后缀
prompt_template_*.txt	跨用户共享	不加后缀

切到云端灾备（dashscope）
本地推理栈出问题时，改 pipeline_config.yaml：

curator:
  provider: "dashscope"

dashscope 的 key 已在 provider_configs 里配好，模型为 qwen3.7-plus。


验证 prompt 改动
改完 prompt 后跑一次回归测试：

# 跑验证集
python stage02_aesthetic_curator.py     # SOURCE_DIR 指向验证集目录

用自己的验证集对比精华挑选（pick）命中率，观察改动前后的严格命中率与模糊命中率是否稳定。

常见问题
Q: 02 脚本卡住、HTTP 不返回
A: Ollama 服务挂了或长跑后熔断。Ctrl+C 杀掉，重启 Ollama，重跑 02 即可（自动续跑）。

Q: 跑出来的命名出现"图像损坏""严重失真"剔除理由
A: Ollama 版本不对。必须用 v0.30.8，0.30.9 起的 context shift 改动会破坏 vision token。

Q: 高德返回 10021 限流
A: 01b 已内置退避重试，正常情况会自动恢复。如果持续报错，检查 Key 是否被封或并发是否被改高。

Q: 想换模型
A: 在 pipeline_config.yaml 的 curator.provider_configs 里改对应 model 字段。但 prompt 是按 31B gemma 调过的，换模型大概率要重新跑 287 验证集校准。


当前生产基线
Prompt: prompts/prompt_template_v0.3.0.txt
01a: ./stage01a_phash_chunker.py
01b: ./stage01b_geo_resolver_amap.py
02: ./stage02_aesthetic_curator.py
Ollama: v0.30.8（锁定）
模型: gemma-4-31B-it-qat-GGUF UD-Q4_K_XL
Git tag: v2.3.0-prod-2026-07-15


几点设计说明：

1. **配置改动用代码片段精确指明**，不写模糊的"修改配置文件中的相关参数"。这种写法对工具自动执行也友好。
2. **FAQ 只放真实踩过的坑**——Ollama 版本、熔断、限流——给短答案。
3. **没写"测试""部署""贡献指南"这些套话**，只写怎么跑。

---

## 许可证 / License

本项目采用 **GNU Affero 通用公共许可证 v3.0（AGPL-3.0）** 开源，完整条款见根目录 [`LICENSE`](./LICENSE) 文件。

AGPL-3.0 的核心义务：任何对本项目的修改，以及通过网络对外提供服务（例如把本项目部署为可远程访问的 API / SaaS），都必须以 AGPL-3.0 向使用者公开对应的完整源代码。

### 商业授权 / Commercial License

如果你希望在**闭源**或**商业**场景中使用本项目，且不愿受 AGPL-3.0 的开源义务约束（例如集成进闭源产品、提供闭源的在线服务），可以联系作者获取商业授权（双重许可）：

**联系邮箱：tobias@sina.com**

Copyright (c) 2026 Tobias Zeng. Licensed under AGPL-3.0 for open-source use; commercial licensing available on request.
