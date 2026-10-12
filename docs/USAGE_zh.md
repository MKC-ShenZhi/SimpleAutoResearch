# 使用与配置

[English version](USAGE.md)

本文说明如何安装、配置和运行 SimpleAutoResearch。它是面向用户的实践指南；工作流概念和产物结构见 [工作流与产物](WORKFLOWS_zh.md)，完整命令表见 [CLI 参考](CLI_REFERENCE_zh.md)，TOML 字段见 [配置参考](CONFIG_REFERENCE_zh.md)。

## 从一个任务开始

`uv run simple-ar start` 询问自然语言目标，进入实验性原生工具循环。
材料用可重复的 `--source PATH` 只读挂载；公开检索需要 `--allow-network`，
进程提案需要 `--allow-execution` 并单独批准命令。额度和续接规则见[原生入口](CLI_REFERENCE_zh.md#原生任务入口实验性)，
完整跨任务验收尚未通过。

结束时的结果面板显示保存的工作区、待处理动作和账本余额。已有交付记录可能属于
当前反馈之前的版本；文件存在、终态和剩余调用次数不代表验收通过，也不保证
下一次模型请求能放入剩余token额度。

新原生任务提供授权范围内的完整工具契约，可在同一行动循环组合工作区、来源、代码、
分析、写作和出版能力。历史按需加载任务续接时保留已保存的契约选择。
工具可用不授予执行或网络权限，也不代表任务已成功。

提供 Python 项目后，原生任务可复用现有准备模块调查说明、依赖声明和入口候选，
取得概览及完整观察记录，不导入项目、安装依赖或执行测试。依赖元数据描述的是
调查进程所在解释器，不代表另行选定的任务 Python 已准备好。
最终 `write_file` 可携带明确的 `delivery`（说明、产物路径和未解决项），校验路径并成功
写入后保存同一交付清单，不再为声明交付单独调用模型。只写文件仍不会自动完成任务，
交付也不证明内容正确。

新实现可复用已有生成器，一次准备关联源码与检查；使用模型目录的 `code` 路由和同一
任务预算。原材料不变，候选、来源绑定及未解决项保存于任务中。执行仍需批准，
语法检查不能替代行为验证，也不代表任务已交付。
只给目标也可开始：代码所有者自行选择新文件，不要求用户提供仓库、协调层预列文件
或固定入口文件名。已有项目为同一选择路径补充有界库存和原文；显式文件与窗口仍可使用。
修改已有文件时，可为关联输出提供 `source_path` 和观察过的 `source_sha256`；
专业调用基于完整有界原文生成精确替换，保留其余内容，再保存独立候选。
新文件仍生成正文；原文或整体输入过大时明确拒绝，不用截断内容重写源文件。

已观察的文本文件可按来源摘要原样转入工作区，包括生成候选，组装或修订可运行项目时
不必重新发送全文。覆盖已有目标仍须当前摘要，并沿普通写入保留备份；挂载原始来源
保持只读。复制不授予执行权限，也不证明修复正确。

补充材料可使用 `simple-ar start --resume-task SESSION "结合这些笔记" --source notes/ --yes`。
访问授权也可在续接时明确追加，保留已完成成果和原预算，不必另建任务从头执行。
原生模型决策已终止且无接受回复时，可显式使用 `--resume-task SESSION --retry-model`，
沿原剩余额度继续，不重复已完成工具；保留失败和有界未知预留。
它不能处理在途操作、无界未知消耗或预算耗尽；具体条件及分开授权规则见CLI参考。

原生任务可检查有界 CSV/TSV 或记录数组 JSON 表格，调用已有确定性分析能力，
完成支持范围内的汇总及明确配对比较。分析包包含输入副本、图形和可重建结果。
行含义、单位及配对关系仍须依据材料或集中澄清，观测数量不证明样本独立或统计显著。
不支持的转换和方法进入已有代码/执行路径，并沿其授权边界处理；写作应消费实际
计算结果，而不是代替计算。

原生写作可同时使用供给文本、表格、保存结果的选定窗口及已取得的文献片段。
已有图形复制到可编辑 `manuscript.md` 旁，以相对路径引用；反馈创建新版本，
保留原稿与输入。写作工具不计算统计量或核验图形，需要这些工作时应先要求计算与核查，
不能仅因正文生成就相信其结论。出版继续消费这份正文。
图形可在路径之外明确提供图注：图注归图片，正文负责解释，出版负责编号。
导出只移除紧邻且完全相同的重复图注，不猜测删除相似论述；旧路径输入仍有效。
正文及图形应放在专用稿件目录：原生出版绑定授权的本地图形内容，盘点上限为
4096 个目录条目和 256 MB 图形，派生出版目录不作为输入。即使正文未改，图片变化
也需要新的批准；私有图片不复制，编译失败保留源码、失败版本和诊断。

需要检查 PDF 版面时，原生出版工具可在编译时显式设置 `render_preview: true`。
同一份 Markdown 派生 TeX/PDF，同一批准操作渲染全部页面，返回可交给
`inspect_images` 的图片位置。该有界预览支持最多 12 页、120 DPI，每个尺寸缩放至
不超过 1600 像素；更长文档明确报告，不偷偷截断。渲染不等于视觉审阅；请求的
预览未完成时不交付，已编译 PDF 与诊断仍保留。

### 成果偏好与结构化兼容

执行 `uv run simple-ar start --guided` 选择成果偏好。普通调研、写作、复现、改码、数据、图形和
代码偏好都与自然语言请求使用同一原生主干。提供目标及常见只读材料/项目/数据路径即可，
无需先填验证器、编辑范围或完整测量协议。需要的公开来源/进程提案访问须明确确认，
每条进程命令仍单独批准；`--yes` 只接受展示的摘要，`--prepare-only` 不调用模型或进程。
详见[偏好输入](CLI_REFERENCE_zh.md#成果复用与概念图)。
兼容由显式控制参数选择，不由偏好名称选择：

| 功能 | 需要提供 | 执行边界 |
| --- | --- | --- |
| `survey` | 显式 `--sources`/获取控制，可选论文 | 保留声明的来源政策和报告协议；需要模型 |
| `bug_fix` | 项目、允许路径、验证命令 | 受限修改与实际验证；需准备依赖 |
| `reproduction` | 论文、已有项目/数据、固定协议/命令 | 指定结论，当前环境或明确选择任务虚拟环境，不自动获取数据 |
| `writing` | 显式 `--template` 或报告控制，笔记/草稿/分析包 | 保留指定结构化写作协议 |
| `data_analysis` | 不加 `--scripted` 的显式数值/x/基线列选择，或物理表格控制 | 确定性表格快捷入口，描述统计与声明的图形；不加 `--with-report` 写作时无需 API |

`--chat`/`--resume-setup` 保留结构化技术设置，`--from-session`/`--reuse` 保留登记成果与范围复用。
即使显式提供 `--interaction checkpoints` 等默认值也会选择兼容协议，不能混用原生访问/预算参数；
figure 与 scripted 分析在创建前拒绝不兼容的结构化控制。
`--prepare-only` 只保存配置，不执行或调用模型。技术协议设置可选择 `start --chat`，用自然语言
澄清影响结果的选择，确认后进入结构化配置路径。它需要模型，会发送回复与指定材料预览，
并保存澄清用量；`--resume-setup PATH` 恢复草稿，不重开计费记录。这不是通用编程 Agent，
代码/复现的执行命令和范围仍需明确。不能与承诺不调用 API 的 `--prepare-only` 同用。
尚未确定的可选项不写成实际赋值；先回答影响结果的选择，完整确认配置仍在执行前校验。
修正后仍无效时，可以继续补充说明或输入 `stop` 保存退出。配对分析需要同一行中的
基线、候选数值列；长表的方法行须先按实际配对关系整理，工具不会自行关联或透视。

改码可用 `--project-python /path/to/project-venv/bin/python`（Windows 用
`C:\path\project-venv\Scripts\python.exe`）指定已有项目环境，无须手改 TOML。
省略仍使用当前环境；不创建环境或安装依赖。此选项也适用于原生任务执行，不改变助手本身的 Python 要求。

无 API 首次体验可执行
`uv run simple-ar research-session --config examples/data-curves/research.toml`。
已保存分析包可以用于后续写作；`report-export` 导出保存的结构化报告 attempt，原生稿件在任务中出版，见 [CLI](CLI_REFERENCE_zh.md)。

终端选择数据列前会显示列名和示例值。[配对观察案例](../examples/data-paired/README_zh.md)
无需模型即可计算同一行差值和标准误。准备复现命令前，可先用
`project-info --project PATH --output runs/preparation-NEW` 取得可复用的只读笔记：
使用说明、依赖声明、入口候选及指定数据位置；不安装、不下载、不执行项目。
静态 `[project.scripts]` 声明会在原入口读取额度内优先提供索引内 root/src 模块源码，
不要求 main guard；未知或歧义路径保留，不把入口声明当命令已安装或函数可用的证明。
静态 pyproject/requirements 声明还与当前 Python 的发行包元数据比较，不导入项目；
requirements 的安装指令、包含文件或截断行仍未解析，版本匹配不当导入或复现已成功。
也可用 `--chat --kind reproduction --project PATH --document PAPER --goal "..."`，
补读索引内源码后建议 argv/指标/协议，显示确认后才进入单独的执行确认。
这不等于自主寻找任意实现。单条 Python 命令可在 `--command` 前明确选择
`--environment venv` 和项目内相对的 `--requirements PATH`；执行时创建任务虚拟环境、
安装所选文件并 `pip check`。可显式加 `--install-project` 同次安装项目包，默认关闭，
非 editable；构建可能执行代码、联网、写源目录构建文件。默认仍用当前环境。设置本身只保存选择，
范围、用量及恢复见[配置参考](CONFIG_REFERENCE_zh.md#引导设置)。

结构化任务的 TOML、原生任务的会话分别保存已确认配置。命名连接和路由放在 `models.toml`，
凭据从环境读取；仅环境变量连接仍作为旧配置兼容方式。普通用户不必填写下面
所有高级选项。详见[引导设置](CONFIG_REFERENCE_zh.md#引导设置)。

## 环境要求

- Python 3.12 或更高版本。
- 使用 `uv` 管理依赖。
- 如果要运行 LLM 支持的 planning、notes、synthesis、report 或 code edits，需要一个 OpenAI 兼容 API key。

## 安装

数据图沿用 Matplotlib 的字体配置。中文等非拉丁标签需要安装覆盖相应字符的字体，并在
`matplotlibrc` 设置 `font.family`（两种字体均已安装时，例如 `DejaVu Sans, Noto Sans CJK SC`）。
预览版不会自动下载字体；出现缺字警告时应检查 PNG/PDF 标签，分析成功不等于视觉质量通过。
详见 [Matplotlib 字体回退说明](https://matplotlib.org/stable/users/explain/text/fonts.html)。

图结构的方法图可在执行机器选装 [Graphviz](https://graphviz.org/download/)，确保 `PATH`
中有 `dot`（Debian/Ubuntu：`sudo apt install graphviz`）。生成代码可使用成熟布局工具，
任务应通过已授权命令确认它实际可用。数据图不要求此工具，任务设置不会自动
安装；渲染后的标签和反馈箭头仍需视觉检查。

克隆当前预览分支，与 [README](../README_zh.md) 一致：

```bash
git clone --branch feat/v2.9-task-driven-research https://github.com/Wchanging/SimpleAutoResearch.git
cd SimpleAutoResearch
```

安装依赖：

```bash
uv sync
```

检查 CLI 是否可用：

```bash
uv run simple-ar --help
```

## 环境变量配置

普通多任务使用建议配置共享命名目录。将[示例](../examples/model-profiles/models.toml)
复制到 `~/.config/simple-ar/models.toml`，或私有位置并设置 `SIMPLE_AR_MODELS_CONFIG`
为其绝对路径。各 profile 保存服务商实际 URL、模型与 API 模式，`api_key_env` 引用凭据变量名，
密钥值留在环境或私有 `.env`；示例引用 `CCTQ_API_KEY`，可改为对应服务商的变量名。
项目路径、数据、解释器和限额仍属于任务输入。

设置一次 `default` 供普通文本任务使用，`code` 供代码工作使用；按需增加 `text`、`writing`、
`vision`、`image` 路由，各任务共享目录。原生写作未设独立路由时沿用任务连接。
终端中的 `start` 显示文本连接供选择，`--model profile:NAME` 可明确指定。
`--model env` 在有目录时使用 text/default 路由，无目录时使用旧环境连接，不绕过目录。
`simple-ar models` 本地检查凭据是否存在并校验目录声明，不调用 API，也不验证提供方连通性。
原生决策要求 Chat；结构化文本工作流也支持 Responses。详见[命名连接](CONFIG_REFERENCE_zh.md#命名模型连接)。

未配置目录时，兼容仅环境变量方式：
仅在尚无 `.env` 时创建；不要覆盖已有连接设置：

```bash
cp .env.example .env
```

PowerShell：

```powershell
Copy-Item .env.example .env
```

旧环境模型连接示例，须替换为服务商实际设置：

```bash
OPENAI_API_KEY=your_api_key
OPENAI_BASE_URL=https://your-provider.example/v1
SIMPLE_AR_MODEL=your_model_id
SIMPLE_AR_LLM_API=chat
SIMPLE_AR_LLM_STREAM=true
```

传输与恢复说明（按需查阅的高级参考；下列 `SIMPLE_AR_LLM_*` 连接设置用于旧环境方式，命名 profile 使用自身设置）：

- `OPENAI_API_KEY` 是旧环境 LLM 连接必需项；命名 profile 使用 `api_key_env` 引用的变量。
- `OPENAI_BASE_URL` 可以指向 OpenAI，也可以指向第三方 OpenAI 兼容 `/v1` 接口。
- 无目录时，`SIMPLE_AR_MODEL` 为 `research-session --model env` 提供模型名称；新会话省略
  `--model` 表示明确使用确定性模式。
- `SIMPLE_AR_LLM_BACKEND` 控制传输实现。默认 `openai` 使用 OpenAI Python SDK 直连；`litellm` 保留旧的 LiteLLM 兼容层，需执行 `pip install 'simple-autoresearch[litellm]'` 安装可选依赖。默认 SDK 路径不需要该额外依赖。
- `SIMPLE_AR_LLM_API` 控制请求形态。`responses` 会发送 Responses API 风格的 `instructions` 和 `input`，临时错误只在同一接口内有限重试；`chat` 会直接发送 Chat Completions 风格的 `messages`。已有的 `auto` 模式才会在 Responses 重试后再尝试 Chat，用于兼容只暴露其中一种接口的网关。
- `SIMPLE_AR_LLM_STREAM=true` 在 `SIMPLE_AR_LLM_API=chat` 时启用 Chat Completions
  流式传输；客户端会在解析前拼接 chunks，服务商提供最终 usage 时仍会记录它。Responses
  调用保持非流式。完成标记和最终用量齐全后不再等待连接关闭；中断错误显示已收字符数及
  完成/用量状态，不记录响应文本。流式可以减少兼容网关的长时间非流式连接卡顿，但不会取消服务商或客户端超时。
  流式请求默认要求不压缩传输。生成已明确完成而用量尾包失败时，保留正文并估算用量；
  未收到完成标记的残缺流仍判失败。取消请求将用量记为未知并关闭 SDK 连接；重试显示次数和等待时间。
- `SIMPLE_AR_CHAT_TOKEN_LIMIT_PARAM` 可选地指定 Chat Completions 的输出参数名：`max_tokens` 或 `max_completion_tokens`；`auto` 会在可能时根据模型名选择。
- `SIMPLE_AR_LLM_REASONING_EFFORT` 是可选的、由模型文档定义的推理强度，例如 `low` 或 `high`，只会通过 Chat Completions 的 provider 扩展字段转发。`SIMPLE_AR_LLM_REASONING_OUTPUT_TOKENS` 仅在调用方和客户端均未设置输出上限时作为兜底上限，不会覆盖显式的单次调用上限。
- `SIMPLE_AR_LLM_THINKING` 可选，通过 Chat Completions 的 `extra_body` 传入 `thinking.type=enabled` 或 `disabled`，默认不传。仅对明确支持该参数的模型启用；强制思考模型不能关闭，`disabled` 也不能同时设置 `SIMPLE_AR_LLM_REASONING_EFFORT`。
- `SIMPLE_AR_LLM_TIMEOUT_SEC` 默认是每次 provider 尝试 180 秒；慢速服务商可以设置更大的正数，只有明确设为 `0` / `off` / `none` / `unlimited` 才不向 provider 传客户端超时。
  该设置传给传输客户端，不保证持续收到事件的流在相同墙钟时间内结束；重试也会延长逻辑调用。断流的实际用量不能确认时保留未知状态，不当成已确认零费用。
- `SIMPLE_AR_MAX_OUTPUT_TOKENS` 是可选项；留空或设为 `0` / `off` / `none` / `unlimited` 时，不向 provider 传输出上限。只有你确实想限制模型输出长度时才设置正数。
- `SIMPLE_AR_LLM_RETRY_ATTEMPTS` 和 retry delay 设置控制临时 provider 错误的有限指数退避重试，例如连接中断、限流、超时、5xx 响应和 Cloudflare 524 origin timeout。
  SDK Chat 流区分发送前连接/连接池失败与可能发送后的读写失败、断流。
  明确未发送的尝试消耗一次请求、零 tokens；未知用量且无输出上限的流停止，
  不自动重发或切换接口。有上限文本在可重试时保留失败请求的保守预留。
  增加重试次数不会覆盖这一区别，也不能解决提供方容量问题。
- `SIMPLE_AR_LLM_MAX_WORKERS` 可选，限制单进程内每批模型请求的并发数；不设置则保留各阶段默认并发。服务商限流时可设为 `1`，不改变研究会话协议。
- 在线研究调用在有界 provider 重试耗尽后停止并保留失败 attempt。研究 TOML 没有
  `[llm].allow_fallback` 开关；要离线运行请设置 `[model].name = ""`，不能把确定性输出描述成
  模型生成的分析。`--no-llm` 只属于 CodeTask 原语命令，不是 research-session 参数。
- 若配置文件后来已修改，而本次只想按保存的输入原样续跑，可使用
  `simple-ar research-session --session-root PATH --model env`（或 `--model MODEL`），
  无需再次传入 `--config` 或 `--topic`。会话会恢复原目标、证据、执行条件与预算；
  显式修改仍须通过续跑检查。模型会话若省略 `--model`，CLI 会在改动保存状态前报错，
  不会静默切换到确定性模式。
- 若进程在 attempt 运行中中断且尚未保存能力结果，先确认原工作进程已经停止，再使用
  `simple-ar research-session --session-root PATH --recover-interrupted --model MODEL`。
  它会按原预算记录一次失败并沿保存的计划继续，不删除已完成证据。原进程仍可能运行时不要使用；
  也不要同时提交输入修订。
- `SIMPLE_AR_JSON_RESPONSE_FORMAT` 控制结构化 JSON 调用是否使用 provider 原生格式。默认 `auto` 尝试发送 `response_format={"type":"json_object"}`，仅在接口明确不支持时退回普通提示；普通文本调用不变。`off` 保留仅靠 prompt 和本地解析的模式，`json_object` 表示强制发送。已有 `.env` 明确写了 `off` 时继续尊重该选择，不静默覆盖。
- 价格字段只影响 usage summary 中的费用估算；不填也会记录 token。

服务商显示生成成功，不等于客户端已收到完整响应。遇到流式中断时，先核对错误中的
`content_chars`、`finish_reason`、`usage_received`，再检查运行机器的网络出口；不要只靠
增加重试或关闭超时处理持续断流。需要代理时，在实际运行进程中配置可达的 `HTTPS_PROXY`
（以及需要的 `HTTP_PROXY` / `NO_PROXY`），不要把个人电脑的 `127.0.0.1` 地址直接复制到服务器。
依赖临时 SSH 转发的运行会随转发关闭而失去通路，不能视为服务器独立部署完成。
保留原会话和用量记录，确认旧进程结束后再恢复，避免重复执行或重复计费。

命名连接使用 profile 的 `stream`、`request_timeout_sec` 和 `retry_attempts`，不再读取旧
`SIMPLE_AR_LLM_*` 连接参数；排障先用 `simple-ar models` 确认选择，不能只改 `.env` 的旧超时。
流式也可能长时间等待第一个响应头。小提示不保证低延迟；连通探针应采用实际 profile 的
超时（默认180秒），不要另外缩成很短的值后据此判定接口不可用。超时是传输等待限制，
不是总墙钟截止时间；不要通过无限等待或无限重试掩盖问题。

定位顺序是：确认运行机器/连接→鉴权与模型列表→请求发送及首个HTTP响应→首个SSE事件→
完成标记和用量→正文/JSON解析→保存产物与账本。模型列表成功只证明该查询可用。
最后用不带诊断包装的真实入口验证长输入、较长输出和恢复；诊断包装自己的错误必须与产品错误分开。

## 选择任务与最小输入

普通任务用 `start` 提供目标与材料，按需要选择工具。下列示例使用显式结构化接口：
`research-session` 接收目标、已有材料和明确的执行条件，再由 accepted plan
选择适用能力；用户不需要填写内部阶段顺序。当前支持的任务形态如下：

| 需求 | 最小配置与命令 | 不会假定什么 |
| --- | --- | --- |
| 在线综述 | `[task] outputs=["report"]`、`[research] providers=[...]`；`research-session --config survey.toml` | 不启动实验进程或代码项目 |
| 分析已有论文/笔记 | 增加 `[research] materials_only=true` 与 `[assets].papers=[...]` | 不执行网络检索或伪造检索结果 |
| 使用笔记/结果写作 | `[task] kind="writing"`、`outputs=["report"]`、`[assets].materials=[...]`、`[model] name="env"` | 不重做综合/检索/实验；分析 JSON 必须是完成的支持格式分析包 |
| 描述/绘制已有数据 | `start --kind data_analysis --data-file PATH --value-column NAME` 或[数据曲线案例](../examples/data-curves/README.md) | 确定性快捷方式：不调用模型，不推断统计结论、平滑或认证科学有效性 |
| 修复已有项目 | `[task] kind="bug_fix"` 加 `[execution] code_task_config="code_task.toml"`；运行研究配置 | 不自动引入文献或 baseline |
| 复现/测量已有脚本 | `[execution] command=["python","measure.py"]`、`cwd`、指标与有限进程预算 | 不自动发现仓库或扩展种子 |
| 改进已有研究项目 | `[task] kind="auto"` 配 `execution.code_task_config`、保护资产、benchmark 和 protocol；参见[持续学习案例](../examples/continual_learning/README.md) | 不安装依赖、下载数据或宣称科研成功 |
| 独立代码任务 | `simple-ar code-task init --config code_task.toml`，再执行 `code-task execute` | 不进入研究报告生命周期 |

下面是四个高级、彼此独立的完整研究 TOML 示例；不要把它们的分区拼接到同一个文件。
相对路径均以该 TOML 文件所在目录为基准。

### 1. 仅分析供材（不检索）

```toml
[task]
goal = "Compare the claims and limitations in the supplied papers."
outputs = ["report"]

[model]
name = "env"

[research]
materials_only = true
max_chunks = 32

[assets]
papers = ["notes/method-a.md", "notes/method-b.md"]

[report]
template = "survey"
```

在放置 `research.toml` 的目录执行 `simple-ar research-session --config research.toml`。
两个只读文件必须真实存在；不会执行 search，也不会启动实验进程。

### 2. 在线文献综述

```toml
[task]
goal = "Survey small-memory continual learning for image classification."
outputs = ["report"]

[model]
name = "env"

[research]
providers = ["openalex", "semantic_scholar", "arxiv"]
max_results = 8
max_chunks = 32
interaction = "checkpoints"

[budget]
process_invocations = 0
process_wall_seconds = 0

[report]
template = "survey"
```

在包含该文件的目录执行；它使用配置的 provider 和当前环境中的 LLM 设置，不发现代码项目，
也不启动实验。

### 3. 已有项目 bug 修复

```toml
[task]
goal = "Fix the phrase-aware classification bug described by the task file."
kind = "bug_fix"
outputs = ["bug_fix"]

[model]
name = "env"

[budget]
process_invocations = 1
process_wall_seconds = 120

[execution]
code_task_config = "examples/code_task_medium_review/configs/code_task.toml"
```

将此文件保存到仓库根目录后执行 `simple-ar research-session --config bugfix.toml`。
引用的 CodeTask TOML、其 `task.md`、项目源码、benchmark 和声明的修改范围必须存在；
实现会在隔离 workspace 中进行，bug-fix 路径不跑 baseline，也不改原始 fixture。

### 4. 直接 CPU 测量

```toml
[task]
goal = "Measure the checked-in digits benchmark without changing its source."
kind = "measurement"
outputs = ["experiments"]

[model]
name = ""

[execution]
command = ["python", "benchmark.py"]
cwd = "examples/code_task_digits_mlp/project"
timeout_sec = 60
primary_metric = "accuracy"

[budget]
process_invocations = 1
process_wall_seconds = 60
```

将此文件保存到仓库根目录后执行
`simple-ar research-session --config measurement.toml`。显式 `outputs` 已排除报告，
不要再同时传入 `--no-report`。直接 argv 使用进程
`PATH`，不应用 CodeTask 解释器策略；若 `python` 不是目标环境，请在 `command` 中写绝对解释器。
该 benchmark 是小型 CPU fixture，不是科研改进证据。
`measurement` 路径只运行已给命令并分析结果，不做文献检索、想法生成或研究设计；
需要提出候选或做有依据的比较时，应使用通常的科研路径。

四种文件都可用 `simple-ar status RUN_DIR` 查看输出路径；暂停后用
`simple-ar research-session --session-root RUN_DIR`，按文档的 decision/continuation 参数恢复。
一次执行边界只能使用直接 `execution.command` 或 `execution.code_task_config` 之一，不能同时写两套。

## Research session：从任务到证据

正式用户入口是 `research-session`。它接收任务和材料，生成短 accepted plan，
只执行计划真正需要的能力。文献摄取、CodeTask 准备/实现、实验测量、分析、报告写作和审计
各自拥有边界，应用通过显式 artifact 引用连接它们。实验命令、baseline 策略、数据集、代码范围
和资源限制仍由用户或配置提供；这是有界工作，不是无限自主循环。

如果只需要文献研究，可以同时省略实验命令和 `--code-task-config`。不提供模型时，session
会在有证据支持的 summary 处完成；提供模型时，可以继续进入 research-only 报告路径。这种
形态不会创建实验进程或 execution 预算。

适合笔记本的离线完整 smoke：

```bash
uv run python scripts/research_session_smoke.py
```

真实网络、LLM 和准备好的代码项目的低资源示例见 `examples/README.md`。遇到失败时，保留
session 目录并检查具体 attempt；不要用 fixture 结果覆盖真实失败。


## Retrieval 和 Artifact 工具

用于检查或搜索某次 run 产生的文件：

```bash
uv run simple-ar inspect runs/<run-id>
uv run simple-ar search-artifacts runs/<run-id> "accuracy"
```

参数细节见 [CLI 参考](CLI_REFERENCE_zh.md#artifact-tools)。

## Tool 和外部 Agent Handoff 预览

内部 common tool 与 agent-handoff 层是未来 Codex、Claude Code、
OpenCode、OpenAI tool calling 或 MCP adapter 的受控扩展点，不是新的默认运行路径。

当前行为：

- 注册的 tool 必须是真实可用的本地 report / experiment tool，不添加空壳 MCP stub；
- 可以导出 OpenAI-style 和 MCP-style tool schema；
- 默认权限策略是 read-only / plan-only；
- 外部 agent handoff package 写到 `runs/<run-id>/agent_handoff/<name>/`；
- backend 输出如果被收集，会进入 `runs/<run-id>/agent_outputs/<name>/`，
  仍必须经过 SimpleAutoResearch validation，才能影响 patch、result 或 report。

handoff package 会显式列出上下文、权限和期望输出：

```text
runs/<run-id>/agent_handoff/<name>/
  instructions.md           # task、backend profile、permission summary
  tool_schema.json          # 真实 tool schema
  permission_policy.json    # write/shell/network/secret policy
  artifact_handles.json     # 暴露给 backend 的 run artifacts
  expected_outputs.json     # backend 可产出的 canonical files
  workspace_manifest.json   # 紧凑 run/workspace 视图
  context/
```

外部工具仍然只是可选 strong path adapter。本地 research、report、greenfield
experiment 和 code-task workflow 不需要 Codex、Claude Code、OpenCode 或 MCP server
也能继续运行。

这个边界后面也提供可运行 backend：

- `fake`：deterministic dry-run backend，用于集成测试；
- `local_llm`：用当前 LLM 生成有边界的 review 产物；
- `codex`、`claude_code`、`opencode`、`external_cli`：可选 CLI backend。

如果要试 agent-backed greenfield generation 或 repair，可以设置
`[implementation].provider`。外部 CLI provider 还必须显式设置
`[implementation].allow_external_agent = true`。如果 executable 不在 `PATH`，或者需要
provider-specific flags，可以使用 `[implementation].agent_binary`、`.agent_args` 和
`.agent_timeout_sec`。即使开启，backend 也只能把候选文件
写到 handoff 目录；SimpleAutoResearch 会再复制到 run workspace，并继续执行原有的
code review、result guard、benchmark 或 code-task validation gate。

`[implementation].agent_mode` 是这一层唯一新增的模式开关：

- `model`：SimpleAutoResearch 仍然拥有 harness，只把有界生成交给本地/模型 backend。
- `handoff`：为 Codex、Claude Code、OpenCode 或其他外部 CLI 写出可审计 handoff package，
  再把 candidate files 收回 SimpleAutoResearch 的 gate。
- `delegated_workspace`：预留给未来“外部 harness 接管 workspace loop”的强路径。当前版本会识别
  这个值，但执行时会显式失败，不会静默降级。

run-local 只读 tools 也可以通过 MCP stdio 暴露：

```bash
uv run simple-ar tools schema --format mcp
uv run simple-ar tools call runs/<run-id> list_experiment_artifacts
uv run simple-ar tools serve-mcp runs/<run-id>
```

配置 Codex/MCP 集成时，使用
`[implementation].provider = "codex"`，并默认保持 `[implementation].agent_model = ""`，
让 Codex CLI 使用当前账号配置的默认模型。只有确认 CLI/账号支持某个模型名时，
再显式填写 `agent_model`。

## Code Task 工作流

Code Task 会把代码任务准备到一个隔离的可编辑 workspace 中，后续所有补丁或生成都只发生在这个 workspace，不修改原始项目。已有项目默认 `auto` 模式：优先为已有 commit 的 Git 项目创建 `git_worktree`，如果 Git 条件不满足则降级为受保护的 `copy`，并在 manifest 和终端输出中记录原因与下一步建议。也可以显式使用 `copy`、`git_worktree`，以及适合小型 allowlist 子集的实验性 `sparse_copy`。从零生成项目时使用 `kind = "greenfield"`，默认从 `empty` workspace 开始，并把生成项目写到 `code_task/workspace/generated_project/`。

推荐先从 TOML 配置初始化，把项目路径、benchmark 指标、workspace 模式、模型路由和编辑预算都放在一个可审核文件里。内置 standalone 示例使用 medium review pipeline：

```bash
uv run simple-ar code-task init --config examples/code_task_medium_review/configs/code_task.toml
```

这个示例会运行 `python main.py --config configs/experiment.json --show-progress`，baseline / patched run 中会打印逐轮进度行，并通过 `[execute].stream_benchmark_output = "auto"` 让 `code-task execute` 在保存 stdout/stderr 产物的同时，把 benchmark 进度转发到命令行。`auto` 模式同时兼容普通 `print` 日志和 `tqdm` 这类 carriage-return 进度输出。

`init` 会创建一个新的 run 目录，核心结构如下：

```text
runs/<run-id>/
  manifest.json                 # benchmark、workspace、environment、safety policy
  code_task/
    task.md                     # 任务说明
    workspace/                  # 隔离 copy/worktree 根目录
      ...                       # monorepo 场景下 project root 可能是其中的子目录
    meta/
      codebase_index.json       # 文件级代码索引
      repo_map.json             # 分层 repo/symbol map
      repo_map_summary.md       # 给人看的 repo-map 摘要
```

它不会运行代码、不会调用 LLM，也不会修改原始项目。

如果是 standalone 从零生成项目，也使用同一套 code-task 命令，只是不需要 `code_root`：

```bash
uv run simple-ar code-task init --kind greenfield --task-file task.md --benchmark-command "python generated_project/main.py"
uv run simple-ar code-task execute runs/<run-id> --to-step run
```

这种模式会复用 code-task 的 memory、reviewer、validation、runner 和 repair 产物，只是实现步骤不再应用 patch，而是在隔离 workspace 内生成 `generated_project/`。

生成先写入独立候选，完成后才替换当前项目。旧项目保留在旁边的
`.generated_project.previous-*`，失败候选保留为`.generated_project.candidate-*`，
便于核查与恢复；生成失败不会清空已交付目录。这些目录不是当前输出，不自动删除。
这只保护项目文件，不是规划元数据的整体事务，也没有新增会话运行时。

在规划 greenfield 实现前，`execute` 会写出 `code_task/meta/dependency_advice.json`
和 `.md`。它会扫描当前 Python 环境，把完整 installed-package snapshot 写入 JSON，
并在终端只展示和任务相关的可用库子集。内置 dependency catalog 只是语义提示，不是
白名单，因此服务器上额外安装的任务库也可以进入规划上下文。这只是建议，不会自动安装
依赖或修改环境；如果你希望模型走更强实现路径，可以按提示先手动执行 `uv add ...`，
然后重跑 execute。

如果省略或显式使用 `workspace.mode = "auto"`，已有项目会先尝试 detached git worktree。如果 Git 不可用、不在仓库内、仓库还没有 commit，或 worktree 无法安全创建，本次 run 会降级为受保护的 copy，并在 `manifest.json.workspace` 写入 `requested_mode`、`selected_mode`、`fallback_reason` 和 `user_next_steps`。

显式 `start --kind bug_fix --allow PATTERN --validate COMMAND` 协议也使用这一策略：干净 Git 项目保留已提交文件，不受复制大小限额影响；有未提交修改或非 Git 的项目复制当前状态。复制遗漏见清单，复制限额不等于源码阅读窗口，不能把复制出的子集视为完整仓库。

如果使用 `workspace.mode = "git_worktree"` 或 `--workspace-mode git_worktree`，`init` 会在 `code_task/workspace/` 创建 detached git worktree，而不是完整复制文件。这个模式要求 `code_root` 位于本地 Git 仓库中，并且仓库至少有一次 commit；`code_root` 可以是仓库根目录，也可以是 monorepo 中的项目子目录。子目录场景下，系统会在仓库根创建 worktree，并把 worktree 中对应子目录作为实际可编辑 project root，用于索引、修改和 benchmark 执行。如果目录不满足要求，CLI 会给出可操作提示，比如初始化 git、提交初始 baseline、传入正确项目路径，或者改用 `copy` 模式包含当前未提交文件状态。

如果使用 `workspace.mode = "sparse_copy"` 或 `--workspace-mode sparse_copy`，只会复制匹配 include pattern 的文件，同时始终排除 `.git`、virtualenv、`runs`、cache/build、`data`、`models`、`.env` 和 secret-like 路径。这个模式适合你明确知道需要哪些文件的小型实验；通用项目优先使用 `auto`，或者按需求显式选择 `git_worktree` / `copy`。

benchmark 最好输出稳定的数值指标行。当前支持 `name: value` 和 `METRIC name=value`；
后者更适合 generated project 或外部 agent 项目，因为它明显是 machine-readable 输出。
自定义指标推荐在 TOML 中声明解释方向。显式 CLI 参数仍然支持，适合临时实验和快速测试，
但公开使用路径建议优先用 TOML。完整参数表见
[CLI 参考](CLI_REFERENCE_zh.md#simple-ar-code-task-init)，配置 schema 见
[配置参考](CONFIG_REFERENCE_zh.md#独立-code-task-config)。

已有项目任务可以用 `[execute].baseline_policy` 控制是否先跑未修改 baseline。
默认 `auto` 会在需要比较证据时运行 baseline；baseline 很贵、或任务只是验收式目标时，
可以设为 `skip` 或 `none`；如果你已经有可信 baseline 指标，可以设为 `provided`，
并通过 `baseline_metrics_file` 提供 JSON 或 `metric=0.82` 文本文件。provided baseline
会在 summary 中标注为用户提供指标，不会伪装成本次复测结果。

### 推荐路径：TOML + Execute

普通已有项目默认只走 patch plan、批准、改码和验证，不自动生成 work plan 或批次状态。
需要任务分解时显式使用 `execute --to-step work-plan` 或 `--to-step batch`；已有 work plan
也会保留分批流程。交互执行遵循同一规则。

正常使用时，推荐把项目路径、benchmark、指标方向、模型路由和预算放进 TOML，然后用 `code-task execute` 推进。这样命令更短，但仍然保留 patch plan 和 edit proposal 两个审核点。下面示例使用 medium review pipeline 配置，覆盖多文件修改、项目配置变更、指标解析和可见 benchmark 进度。

1. 初始化 run：

```bash
uv run simple-ar code-task init --config examples/code_task_medium_review/configs/code_task.toml
```

这个命令会打印一个 run 目录，例如 `runs/20260523-xxxx-medium-review-pipeline`。下面命令中的 `runs/<run-id>` 都替换成这个实际路径即可。

`init` 会写入隔离 workspace 和静态项目地图：

```text
runs/<run-id>/
  manifest.json
  code_task/
    task.md
    workspace/
    meta/
      codebase_index.json
      repo_map.json
      repo_map_summary.md
```

`workspace/` 是唯一可编辑副本或 worktree，`task.md` 是任务说明，`meta/`
里是初始代码地图，`manifest.json` 记录 benchmark、workspace、environment
和 safety policy。

> Tip：medium review pipeline 会运行 `python main.py --config configs/experiment.json --show-progress`。执行时可以看到类似 `benchmark stdout: round 1/4 ...` 的转发行，同时完整 stdout 仍会保存到 `code_task/run/<label>/stdout.txt`。

> Note：medium 任务通常会联动 feature extraction、model scoring 和 config。示例 edit scope 允许修改 `configs/experiment.json`，因为新实现的 feature family 需要在配置中启用，benchmark 才能测到它。因此它可能生成一个已审核的 `large` batch。只有在检查 `code_task/meta/proposed_edits.json` 后，最后应用 proposal 时才应加入 `--allow-large-edits`。

2. 运行状态感知 executor：

```bash
uv run simple-ar code-task execute runs/<run-id> --config examples/code_task_medium_review/configs/code_task.toml
```

在真实终端里，这一条命令可以一路经过 plan 审核、proposal 审核、应用补丁、结构化 review、验证和
patched benchmark；每个真实审核门都会用黄色 Rich 面板提示你看什么、下一步会做什么。
如果在非交互 shell 中运行，或者你回答 `no`，它会停在当前审核门，方便你之后重跑。
第一个审核门通常会生成：

```text
code_task/
  work_plan.md
  patch_plan.md
  meta/
    environment_report.json
    review_report.json
  attempts/
    attempt-001/
      batches/
        batch-001/
          batch_state.json
  run/
    baseline/
      metrics.json
```

这时原始项目仍未被修改，workspace 也还没有应用模型 edits。

`execute` 会用 Rich 显示步骤状态，并默认连续运行到真正需要人工判断的审核门。
真实终端里的审核门会 inline 询问是否继续；非交互 shell 中会干净停住，除非显式传入
`--yes`。中途中断后，重新运行同一条 `code-task execute` 命令即可：已完成步骤会被检测并显示为
skipped，然后 workflow 从下一个需要处理的位置继续。只有在调试 primitive 步骤时才建议加
`--interactive` 逐步确认；`--yes` 会自动继续这些 interactive primitive prompts，
并且在普通 execute 模式下也会自动批准 inline 审核门。只有明确想自动审批 plan/proposal 时才使用它。使用 `--no-review-inline` 可以恢复“停住、下次再跑”的行为。

如果 LLM work-plan 或 patch-plan 返回了无法解析的 JSON，`execute` 会停在
`llm_planning_failed`，并且默认不会写入 offline fallback plan。此时直接重跑同一条
命令即可重新尝试模型调用；如果你明确想完全离线规划，使用 `--no-llm`；如果你希望
先尝试 LLM、失败后接受较弱的 deterministic fallback，再使用
`--allow-planning-fallback`。

补丁应用后，`execute` 会在静态验证前写入 `code_task/meta/review_report.json`；
patched benchmark 完成后还会写入 `code_task/meta/review_report_post_run.json`。
阻塞性发现会同步记录到 `code_task/memory/`，后续 repair prompt 可以直接利用这些
最新失败证据。
结构化 review 还会写入 `code_task/meta/review_index*.json` 和
`code_task/meta/review_clusters*.json`：前者是完整项目索引，后者是分层审查时实际给
reviewer 的语义文件组。Greenfield planning 额外写入
`code_task/meta/planning/agent_steps.jsonl`，用于排查 requirements / architecture /
interfaces / file-plan / planning-review 哪一步失败或反复回修。

对于 greenfield run，同一套 review gate 会在验证前检查生成项目。有限的审阅和运行
修复轮次根据实际诊断选择关联文件，复用代码能力生成独立、绑定原件摘要的候选。
应用仍经过 API、静态检查和快照保护；拒绝时还原整组变化，不留下半套修复。
执行器同步 `code_task/meta/code_artifacts.json` 后重新审阅和验证。旧无绑定结果只保留
历史含义，不证明当前项目已修复；显式新轮次先保留旧结果，再继续工作。生成或应用
中断后保留诊断，不盲目重放。问题仍阻塞时，执行停住并保留产物和审阅记录。

3. 在 patch-plan 审核面板出现时，阅读 `code_task/work_plan.md` 和
`code_task/patch_plan.md`。如果计划合理，输入 `yes` 继续。如果你在非交互环境运行、
回答了 `no`，或使用了 `--no-review-inline`，则可以显式批准：

```bash
uv run simple-ar code-task decide-plan runs/<run-id> --decision approve --note "reviewed"
```

4. 如果第一条 executor 命令没有已经继续到 proposal，则继续生成 edit proposal。
在 proposal 审核面板出现前，不会应用补丁：

```bash
uv run simple-ar code-task execute runs/<run-id> --config examples/code_task_medium_review/configs/code_task.toml --to-step propose-edits
```

重点审核：

- `code_task/meta/proposed_edits.json`：受控 old/new replacement。
- `code_task/meta/llm_usage_summary.json`：LLM token 用量摘要。
- 最新 `code_task/attempts/.../proposal_warnings.json`，如果存在。

受控编辑的 `controlled_patch` 来源 metadata 会记录在
`proposed_edits.json`、active batch state、`applied_edits.json` 和
`manifest.json.patch` 中。提案/应用函数不负责运行 benchmark、批准计划或写报告；这些 gate 仍由 code-task workflow 管理。

5. 在 proposal 审核面板中检查 edits。确认无误后输入 `yes`，即可应用补丁并运行验证和
patched benchmark。如果你在非交互环境运行、回答了 `no`，或使用了 `--no-review-inline`，
则显式应用：

```bash
uv run simple-ar code-task execute runs/<run-id> --config examples/code_task_medium_review/configs/code_task.toml --apply-proposed-edits --timeout 60
```

6. 查看整体状态：

```bash
uv run simple-ar status runs/<run-id>
```

关键结果文件：

```text
code_task/
  summary.md
  patch.diff
  meta/
    applied_edits.json
    validation_report.json
  run/
    patched/
      metrics.json
    comparison.json
```

`patch.diff` 和 `applied_edits.json` 说明改了什么，`validation_report.json`
说明静态检查结果，`metrics.json` 是 patched run 指标，`comparison.json`
是 baseline-vs-patched 的目标判断。

正常成功信号是 `objective_improved` 或 `objective.status = "improved"`。patched benchmark 通过并不等于任务目标一定完成；如果 objective 是 `regressed` 或 `mixed`，说明代码能跑，但指标目标没有真正达成。

7. 如果需要修复，先请求一个有限范围的 repair proposal：

```bash
uv run simple-ar code-task execute runs/<run-id> --config examples/code_task_medium_review/configs/code_task.toml --to-step repair --repair-rounds 1 --timeout 60
```

审核最新的 `code_task/repairs/repair-NNN/proposed_edits.json`，再显式应用：

```bash
uv run simple-ar code-task apply-edits runs/<run-id> --edits-file runs/<run-id>/code_task/repairs/repair-NNN/proposed_edits.json
uv run simple-ar code-task validate runs/<run-id>
uv run simple-ar code-task run runs/<run-id> --timeout 60
uv run simple-ar status runs/<run-id>
```

如果只想预览下一步，不写任何产物：

```bash
uv run simple-ar code-task execute runs/<run-id> --config examples/code_task_medium_review/configs/code_task.toml --dry-run
```

### 可选的代码地图和上下文工具

任何时候都可以刷新代码地图：

```bash
uv run simple-ar code-task map runs/<run-id>
```

`map` 会扫描当前 workspace，并刷新静态代码地图产物：

```text
code_task/
  workspace/                  # 被扫描的源码树
  meta/
    codebase_index.json       # 文件级代码索引
    repo_map.json             # 分层 repo/symbol map
    repo_map_summary.md       # 给人看的摘要
manifest.json                 # 更新 map/workspace metadata
```

它不会调用 LLM、不会安装依赖、不会运行 benchmark，也不会修改原始项目。

定位最可能相关的可编辑文件和只读证据：

```bash
uv run simple-ar code-task locate runs/<run-id> --query "improve spam keyword prediction"
```

`locate` 会写入 `code_task/meta/locate_results.json` 和
`code_task/meta/locate_results.md`。它基于 repo map 对 path、summary、
imports、role tags 和 symbols 做轻量排序，并把 editable targets 与
read-only evidence 分开。它不会调用 LLM，也不会修改文件。

构建受预算限制的 prompt context pack：

```bash
uv run simple-ar code-task context runs/<run-id> --max-files 8 --max-total-chars 20000
```

`context` 会创建 `code_task/context_packs/context-NNN/`，其中包含
`context_pack.json`、`prompt_context.md` 和 `selected_snippets.jsonl`。
这些文件记录选择了哪些源码片段、哪些文件因为预算被省略、哪些内容只作为证据
而不能被自动修改。当前如果存在 latest context pack，`plan` 会优先使用它作为
规划上下文；`propose-edits` 从所选片段开始，继续把 tests / benchmarks 作为
read-only evidence。有界补读更新当前源码窗口，不把窗口字符上限当成累计阅读额度。
初始选段不代表授权范围：新读到的文件仍按配置的编辑权限判断，有明确批次时仍受
批次目标限制。原始观察与每次提示保留的片段位置记录在 `edit_context_followup.json`。

### 手动 Primitive 路径

上面的 executor 会按状态自动调用这些 primitive command。只有在学习内部机制、调试某一步，或者刻意组装自定义流程时，才建议手动逐步运行。

先探测环境并运行未修改 baseline：

```bash
uv run simple-ar code-task map runs/<run-id>
uv run simple-ar code-task locate runs/<run-id>
uv run simple-ar code-task context runs/<run-id>
uv run simple-ar code-task probe runs/<run-id>
uv run simple-ar code-task baseline runs/<run-id> --timeout 60
uv run simple-ar code-task work-plan runs/<run-id>
uv run simple-ar code-task batch runs/<run-id> --work-item W1
```

`probe` 写入 `code_task/meta/environment_report.json`，包含 OS、Python、工具、GPU、依赖文件和 test 目录信号。同时还会写出 `resource_probe.json` 和 `resource_decision.json`，给 greenfield 和外部 agent 路径提供紧凑硬件画像。它不安装依赖，也不运行项目代码。

`baseline` 在任何补丁应用前运行记录的 benchmark command，结果存到 `code_task/run/baseline/`，包括 `execution_report.json`、`stdout.txt`、`stderr.txt` 和解析后的 `metrics.json`，并刷新 `code_task/summary.md`。

如果任务比较宽泛，或者可能需要多批次修改，可以先生成更高一层的 work plan：

```bash
uv run simple-ar code-task work-plan runs/<run-id>
uv run simple-ar code-task batch runs/<run-id> --work-item W1
```

`work-plan` 写入 `code_task/work_plan.json` 和 `code_task/work_plan.md`，
记录 work items、target files、read-only evidence、validation hints、
context requests 和 budget profiles。它不生成代码，也不修改文件。`batch`
会在 `code_task/attempts/attempt-NNN/batches/batch-NNN/` 下创建持久状态，
这是后续做多轮、分批编辑和失败恢复的基础。active batch 存在时，
edit proposal 会被限制在该 batch 的 target files 内，并写入额外的批次级
review 产物。

Work-plan item 应该是可以产生代码修改的 implementation batch，而不是单独的分析笔记。现在 prompt 会要求模型把“还需要看什么”放进 `context_request`；如果模型仍然把第一个 item 写成纯 `inspect/review/measure` 之类的分析步骤，`code-task execute` 会优先选择后面第一个真正像代码修改的 item，避免把“先了解项目”误当成 active edit batch。

如果模型把一个必须联动落地的实现拆成串行依赖链，比如 feature extraction -> scorer wiring -> config enablement，`batch` 会把这个小链条合并成一个执行批次。`work_plan.md` 仍然保留分开的审核项；实际执行范围记录在 `batch_state.json.work_item.source_work_item_ids` 和合并后的 `target_files` 中。由于这种批次可能触碰两个以上文件，通常会升级为 `large` budget profile，应用前仍需要人工审核并显式使用 `--allow-large-edits`。

生成 patch plan：

```bash
uv run simple-ar code-task plan runs/<run-id>
```

如果已有 `probe`、`validate` 或 `baseline` 产物，plan 会把这些上下文纳入模型/审核者可见信息中。`plan` 写入 `code_task/patch_plan.md`，更新 `manifest.json`，记录选择的上下文文件；它不会修改源文件。

审核并批准：

```bash
uv run simple-ar code-task decide-plan runs/<run-id> \
  --decision approve \
  --note "small scoped edit"
```

`decide-plan` 会把人工决策追加到 `code_task/meta/hitl_decisions.jsonl`，并更新 manifest 中的计划状态。

请求模型生成受控编辑 proposal：

```bash
uv run simple-ar code-task propose-edits runs/<run-id>
```

`propose-edits` 写入 `code_task/meta/proposed_edits.json`。proposal 使用 old/new 文本替换，供人工审核；它本身不会编辑 workspace。默认 tests 和 benchmark 文件是只读证据，proposal 不会给这些路径提供可编辑 snippet，后续 apply 也会再次拒绝保护路径。
proposal 也会记录 `editor.backend = "controlled_patch"`，方便后续接入其他 backend 后仍能按同一产物形态审计。
预留的 `external_agent` backend 在当前版本不能执行。它只会为未来 Codex / Claude / OpenCode adapter 构建可审查 invocation plan，其中包含 provider、command preview、blocked read patterns、timeout、network/shell permissions、log path 和 diff path。未来外部 agent 的结果也必须先变成 captured diff/proposal，再经过 SimpleAutoResearch 的 validation、benchmark 和 summary。
执行器还会在模型返回 JSON 后执行本地编辑预算检查。超预算 proposal 会写入 warnings 和 rejected edits，而不是直接应用；如果 proposal 仍在 larger review budget 内，审核 JSON 后再显式使用 `--allow-large-edits`。

应用已审核 edits：

```bash
uv run simple-ar code-task apply-edits runs/<run-id>
```

`apply-edits` 只修改 `code_task/workspace/`，写入 `code_task/patch.diff` 和 `code_task/meta/applied_edits.json`，并重建 codebase index。下一次 `execute` 会先运行结构化 reviewer，写入 `code_task/meta/review_report.json`，再进入静态验证。如果 edit 无法唯一匹配，会在写文件前停止。
`applied_edits.json` 会记录实际应用的 proposal path 和 editor backend，包括手动提供的 edits file 或 repair proposal。

验证并运行 patched benchmark：

```bash
uv run simple-ar code-task validate runs/<run-id>
uv run simple-ar code-task run runs/<run-id> --timeout 60
```

`validate` 写入 `code_task/meta/validation_report.json`，包含语法错误、危险 import/call、缺失 import warning 和文件尺寸 warning。它是静态检查，不运行 benchmark。

`run` 把 patched benchmark 存到 `code_task/run/patched/`。当 baseline 和 patched 都存在时，还会写入 `code_task/run/comparison.json`，并在 `summary.md` 中加入前后对比和下一步建议。

patched benchmark 通过不等于任务目标一定完成。现在系统会把“代码可运行”和“指标目标是否达成”分开：如果 patched benchmark 通过，但相对 baseline 指标退化，`manifest.json` 会写入 `objective.status = "regressed"`，`simple-ar status` 会显示 Objective，`summary.md` 也会引导你查看 `code_task/run/comparison.json`，而不是把它当作真正成功。

失败分析和修复 proposal：

```bash
uv run simple-ar code-task analyze-failure runs/<run-id>
uv run simple-ar code-task repair runs/<run-id>
```

`analyze-failure` 读取最近失败的 validation/benchmark evidence，写出紧凑诊断；它是确定性的，不调用 LLM。`repair` 会使用 failure analysis、最近 patch、task 和选中的源码上下文生成有限范围的 repair proposal，默认不自动应用。

显式应用审核后的 repair proposal：

```bash
uv run simple-ar code-task apply-edits runs/<run-id> \
  --edits-file runs/<run-id>/code_task/repairs/repair-001/proposed_edits.json
```

应用 repair proposal 后，`manifest.json.patch.latest_applied_proposal` 和 `code_task/meta/applied_edits.json` 会记录实际应用的是哪一份 repair proposal。后续 patched benchmark 通过后，旧的 failure-analysis 和 repair section 会被标记为 resolved，`status` 和 `summary.md` 会反映当前状态，而不是继续展示早前失败的尝试。

### Code Task 运行排错

`execute` 后没有生成 `proposed_edits.json`：

- 这是第一次 executor 调用后的正常情况。fresh run 会先停在 `approval_required`，此时应该已经有 `code_task/patch_plan.md`。
- 先审核并批准计划，再明确推进到 proposal：

```bash
uv run simple-ar code-task decide-plan runs/<run-id> --decision approve --note "reviewed"
uv run simple-ar code-task execute runs/<run-id> --config examples/code_task_medium_review/configs/code_task.toml --to-step propose-edits
```

- 可检查 `manifest.json` 中的 `plan.status` 是否为 `approved`；人工决策记录在 `code_task/meta/hitl_decisions.jsonl`。

`execute` 停在 `llm_planning_failed`：

- 这表示模型在 work-plan 或 patch-plan 阶段返回了格式错误/缺失的结构化 JSON。默认情况下不会写入 deterministic fallback plan，避免把弱计划伪装成 LLM 计划继续执行。
- 直接重跑同一条 `code-task execute ... --config ...` 命令即可从同一位置重试；已完成的前置步骤会显示为 skipped。
- 如果你想完全跳过 LLM planning，重跑时加 `--no-llm`。如果你仍想先试 LLM，但失败后接受 deterministic fallback，可以加 `--allow-planning-fallback`，或在 TOML 里设置 `[execute].allow_planning_fallback = true`。

validation 通过，但 patched benchmark 失败：

- 这说明代码语法和静态规则没问题，但行为或指标变差了。优先查看：

```bash
code_task/run/patched/execution_report.json
code_task/run/patched/stdout.txt
code_task/run/patched/stderr.txt
code_task/run/comparison.json
code_task/summary.md
```

- 请求一个有限范围的修复 proposal：

```bash
uv run simple-ar code-task execute runs/<run-id> --config examples/code_task_medium_review/configs/code_task.toml --to-step repair --repair-rounds 1 --timeout 60
```

- 审核最新的 `code_task/repairs/repair-NNN/proposed_edits.json`，再显式应用、验证和重跑：

```bash
uv run simple-ar code-task apply-edits runs/<run-id> --edits-file runs/<run-id>/code_task/repairs/repair-NNN/proposed_edits.json
uv run simple-ar code-task validate runs/<run-id>
uv run simple-ar code-task run runs/<run-id> --timeout 60
```

- 修复后 benchmark pass 不等于任务真正完成。比如修复可能只是恢复到可运行或接近 baseline；是否真的 improved 要看 `code_task/run/comparison.json`、`manifest.json.objective.status` 和 `simple-ar status`。

patched benchmark 通过，但 objective 是 `regressed` 或 `mixed`：

- 这不是运行失败，而是说明补丁没有在记录的 baseline 指标上完成目标。
- 先看 `code_task/run/comparison.json`，其中包含指标 delta、方向、主指标和保守 verdict。
- 把它当成质量失败处理：可以修改任务/计划，重新生成更聚焦的 proposal；只有当 comparison 提供了足够明确的证据时，再请求有限范围的 repair。

`apply-edits` 报 `old text was not found`：

- 发生这种情况时，workspace 文件不会被修改。原因通常是 proposal 里的 `old` 不是当前文件中的精确原文，或者模型把 unified diff 的 `+` / `-` / `@@` 等标记写进了结构化 JSON。
- 重新生成 proposal，或手工修正 JSON。每个 edit 的 `old` 必须是当前文件里唯一匹配的连续文本，`new` 是替换后的文件文本；不要在 `old` / `new` 里写 `+`、`-`、`@@`、`---`、`+++` 这类 diff 标记。
- 如果同一文件的多个 edit 位置很近，最好合并成一个更大的 old/new replacement，避免前一个 edit 让后一个 edit 的 `old` 失效。

提示需要 large-edit approval：

- 先阅读 `code_task/meta/proposed_edits.json`，以及 `code_task/meta/` 或最新 `code_task/attempts/.../batch-NNN/` 下的 `proposal_warnings.json`。
- 如果确认这是必要的大修改，再加 `--allow-large-edits`。不要只为了绕过模型输出异常而使用这个参数。

Proposal 只覆盖了联动计划的第一部分：

- 先检查 `code_task/work_plan.md` 和最新的 `code_task/attempts/.../batch_state.json`。如果计划是 feature -> model -> config 这类串行联动，active batch 应该在 `work_item.source_work_item_ids` 中列出合并的 item id，并在 `work_item.target_files` 中列出所有允许修改的文件。
- 对于旧 run，如果 batch 是在这次行为更新前创建的，可以重新创建批次并重新生成 proposal：`uv run simple-ar code-task batch runs/<run-id> --work-item W1 --force`，然后 `uv run simple-ar code-task propose-edits runs/<run-id> --force`。
- 如果合并后的 batch 被标记为 `large`，先审核完整 proposal，再决定是否使用 `--allow-large-edits`。

`uv run` 出现本地 cache 权限错误：

- 这是本机环境问题，不是 run artifact 问题。可以修复 uv cache 权限，或者在 PowerShell 中直接使用项目虚拟环境入口，例如 `.\.venv\Scripts\simple-ar.exe ...`。


## 命令设计原则

CLI 保留 primitive commands 是因为项目仍然是学习实现。每一步都应该可检查、可测试、可审核。配置文件用于缩短很长的设置命令，而不是隐藏 approval gate、artifact path、validation result、baseline run 或 benchmark evidence。
